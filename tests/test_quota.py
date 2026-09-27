"""会話数の上限とユーザー別費用集計(task 59)。実 LLM なし(呼ばれた回数を数える偽のモデル)。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import partial

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

from dip_mcp.data import State
from line_runtime import ledger as ledger_mod
from line_runtime.quota import (
    DEFAULT_LIMIT,
    JST,
    LIMIT_MESSAGE,
    DailyQuota,
    daily_limit,
)
from tests.test_handle import reply


class CountingModel(BaseChatModel):
    """呼ばれるたびに数えて、決まった返信を返す。"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "counting"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=reply(["はい、どうぞ。"]))])


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _server(tmp_path, monkeypatch, limit: str | None):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")
    if limit is None:
        monkeypatch.delenv("LINE_SIM_DAILY_TURN_LIMIT", raising=False)
    else:
        monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", limit)
    from line_runtime import config, server
    from line_runtime.runtime import LineRuntime

    model = CountingModel()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, in_memory=True))
    server.state.reset()
    return server, model


def _stream(client, user, event, text=None) -> list[dict]:
    r = client.post("/api/event/stream", json={"user_id": user, "event": event, "text": text})
    assert r.status_code == 200
    return [json.loads(x) for x in r.text.splitlines()]


def test_over_the_limit_gets_a_polite_notice_without_calling_the_llm_and_recovers_next_day(tmp_path, monkeypatch):
    server, model = _server(tmp_path, monkeypatch, "2")
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        clock = Clock(datetime(2026, 9, 27, 23, 50, tzinfo=JST))
        server.app.state.quota.clock = clock
        user = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert client.post("/api/event", json={"user_id": user, "event": "visit"}).json()["bubbles"] == ["はい、どうぞ。"]
        assert _stream(client, user, "message", "渋谷で")[-1]["bubbles"] == ["はい、どうぞ。"]
        assert model.calls == 2
        rows = len(server.app.state.rt.ledger.rows)

        # 3回目: 丁寧な案内が返り、LLM は呼ばれない(費用台帳も増えない)。どちらの API でも同じ
        r = client.post("/api/event", json={"user_id": user, "event": "message", "text": "もっと"}).json()
        assert r["bubbles"] == [LIMIT_MESSAGE] and r["limited"] and r["cards"] == []
        last = _stream(client, user, "message", "もっと")[-1]
        assert last["kind"] == "reply" and last["bubbles"] == [LIMIT_MESSAGE]
        # プッシュも送らない(dip に接触として記録させない)
        assert client.post("/api/event", json={"user_id": user, "event": "form_exit"}).json()["sent"] is False
        assert model.calls == 2 and len(server.app.state.rt.ledger.rows) == rows
        # 同じブラウザのシナリオユーザーを替えても、上限は人(匿名ID)単位で共有
        assert client.post("/api/event", json={"user_id": f"{user}:x", "event": "message", "text": "hi"}).json()["limited"]
        # 別の人は使える
        assert client.post("/api/event", json={"user_id": "other", "event": "message", "text": "hi"}).json()["bubbles"] == [
            "はい、どうぞ。"]
        assert model.calls == 3

        # 日付(日本時間)が変わると再び使える
        clock.now += timedelta(minutes=15)
        assert _stream(client, user, "message", "今日も")[-1]["bubbles"] == ["はい、どうぞ。"]
        assert model.calls == 4

    # 数は保存されるので、再起動しても当日の分は消えない
    q = DailyQuota(2, tmp_path / "quota.json", clock=clock)
    assert q.used(user) == 1


def test_limit_comes_from_env_with_a_default(monkeypatch):
    monkeypatch.delenv("LINE_SIM_DAILY_TURN_LIMIT", raising=False)
    assert daily_limit() == DEFAULT_LIMIT > 0
    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "7")
    assert daily_limit() == 7


def test_zero_limit_means_unlimited_and_the_count_is_per_person():
    q = DailyQuota(0)
    assert all(q.take("u") for _ in range(100))
    q = DailyQuota(1)
    assert q.take("w-1:a") and not q.take("w-1:b") and q.take("w-2")


def test_empty_message_is_rejected_before_counting(tmp_path, monkeypatch):
    server, model = _server(tmp_path, monkeypatch, "1")
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        assert client.post("/api/event", json={"user_id": "u", "event": "message", "text": " "}).status_code == 400
        assert client.post("/api/event", json={"user_id": "u", "event": "message", "text": "hi"}).json()["bubbles"] == [
            "はい、どうぞ。"]
        assert model.calls == 1


def test_costs_summarized_per_user_per_day_in_one_command(tmp_path, capsys):
    rows = [
        # 日本時間の日付で区切る(UTC 15:30 は翌日 00:30)
        {"at": "2026-09-27T01:00:00+00:00", "user_id": "w-aaaaaaaaaaaaaaaa:sim-new", "input_tokens": 100,
         "output_tokens": 10, "cost_usd": 0.001},
        {"at": "2026-09-27T02:00:00+00:00", "user_id": "w-aaaaaaaaaaaaaaaa:sim-exit", "input_tokens": 200,
         "output_tokens": 20, "cost_usd": 0.002},
        {"at": "2026-09-27T15:30:00+00:00", "user_id": "w-aaaaaaaaaaaaaaaa:sim-new", "input_tokens": 50,
         "output_tokens": 5, "cost_usd": 0.0005},
        {"at": "2026-09-27T03:00:00+00:00", "user_id": "sim-new", "input_tokens": 1, "output_tokens": 1,
         "cost_usd": 0.0001},
    ]
    path = tmp_path / "costs.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    ledger_mod.main(["--path", str(path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert [(s["date"], s["user_id"], s["calls"], s["cost_usd"]) for s in out] == [
        ("2026-09-27", "sim-new", 1, 0.0001),
        ("2026-09-27", "w-aaaaaaaaaaaaaaaa", 2, 0.003),
        ("2026-09-28", "w-aaaaaaaaaaaaaaaa", 1, 0.0005),
    ]
    assert out[1]["input_tokens"] == 300 and out[1]["output_tokens"] == 30

    ledger_mod.main(["--path", str(path), "--by-user-id", "--date", "2026-09-27", "--json"])
    assert [s["user_id"] for s in json.loads(capsys.readouterr().out)] == [
        "sim-new", "w-aaaaaaaaaaaaaaaa:sim-exit", "w-aaaaaaaaaaaaaaaa:sim-new"]

    ledger_mod.main(["--path", str(path)])  # 表でも出る(合計行つき)
    table = capsys.readouterr().out.splitlines()
    assert table[0].split()[:2] == ["date", "user_id"] and table[-1].startswith("total") and "0.0036" in table[-1]
