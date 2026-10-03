"""会話数の上限とユーザー別費用集計(task 59)、IP ごと・全体の上限(task 112)。実 LLM なし(呼ばれた回数を数える偽のモデル)。"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from functools import partial

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

from dip_mcp.data import State
from line_runtime import ledger as ledger_mod
from line_runtime.quota import (
    DEFAULT_IP_IDS,
    DEFAULT_IP_LIMIT,
    DEFAULT_LIMIT,
    DEFAULT_TOTAL_LIMIT,
    IP_LIMIT_MESSAGE,
    JST,
    LIMIT_MESSAGE,
    TOTAL_LIMIT_MESSAGE,
    DailyQuota,
    daily_limit,
    ip_ids_limit,
    ip_limit,
    total_limit,
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


LIVEKIT = {"LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k",
           "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret"}
QUOTA_ENVS = ("AICA_DAILY_TURN_LIMIT_PER_IP", "AICA_DAILY_NEW_IDS_PER_IP", "AICA_DAILY_TURN_LIMIT_TOTAL", "AICA_TRUST_PROXY")


def _server(tmp_path, monkeypatch, limit: str | None, **env: str):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")
    for k in QUOTA_ENVS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
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
        client.get("/dev")
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
        anon = client.get("/api/me").json()["user_id"]
        assert client.post("/api/event", json={"user_id": f"{anon}:sim-exit", "event": "message", "text": "hi"}).json()["limited"]
        assert client.post("/api/event", json={"user_id": f"{anon}:made-up", "event": "message", "text": "hi"}).status_code == 403
        # 別の人(別のブラウザ。ターンを進めるので lifespan と同じクライアントの Cookie を替える)は使える
        mine = dict(client.cookies)
        client.cookies.clear()
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        assert client.post("/api/event", json={"user_id": uid, "event": "message", "text": "hi"}).json()["bubbles"] == [
            "はい、どうぞ。"]
        assert model.calls == 3
        client.cookies.clear()
        client.cookies.update(mine)

        # 日付(日本時間)が変わると再び使える
        clock.now += timedelta(minutes=15)
        assert _stream(client, user, "message", "今日も")[-1]["bubbles"] == ["はい、どうぞ。"]
        assert model.calls == 4

    # 数は保存されるので、再起動しても当日の分は消えない
    q = DailyQuota(2, tmp_path / "quota.json", clock=clock)
    assert q.used(user) == 1


def test_voice_start_counts_against_the_daily_quota(tmp_path, monkeypatch):
    """音声相談を始める(接続トークンの発行)も実 API を使うので1ターン扱い: 上限に達したら発行せずに案内だけ返す。"""
    server, model = _server(tmp_path, monkeypatch, "1")
    for k, v in LIVEKIT.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        client.get("/dev")
        user = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        r = client.post("/api/voice/start", json={"user_id": user})
        assert r.status_code == 200 and r.json()["token"]  # 1回目は発行(1ターン消費)
        assert server.app.state.quota.used(user) == 1
        r = client.post("/api/voice/start", json={"user_id": user})
        assert r.status_code == 429 and r.json()["detail"] == LIMIT_MESSAGE
        # テキストの会話も同じ枠なので、もう使えない
        assert client.post("/api/event", json={"user_id": user, "event": "message", "text": "hi"}).json()["limited"]
        assert model.calls == 0


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
        client.get("/")
        u = client.get("/api/me").json()["user_id"]
        assert client.post("/api/event", json={"user_id": u, "event": "message", "text": " "}).status_code == 400
        assert client.post("/api/event", json={"user_id": u, "event": "message", "text": "hi"}).json()["bubbles"] == [
            "はい、どうぞ。"]
        assert model.calls == 1


def _new_browser(client) -> str:
    """Cookie を作り直したブラウザ(GET / を開かず、正しい形式の匿名IDを自分で作る。スクリプトで回すのと同じ)。"""
    uid = f"w-{secrets.token_hex(8)}"
    client.cookies.clear()
    client.cookies.set("aica_uid", uid)
    return uid


def _say(client, uid, headers=None) -> dict:
    r = client.post("/api/event", json={"user_id": uid, "event": "message", "text": "hi"}, headers=headers)
    assert r.status_code == 200
    return r.json()


def test_rotating_cookies_from_one_ip_stops_at_the_ip_limit(tmp_path, monkeypatch):
    """Cookie を毎回作り直しても、同じ IP からのターンの合計で止まり、それ以上 LLM を呼ばない。"""
    server, model = _server(tmp_path, monkeypatch, "2", AICA_DAILY_TURN_LIMIT_PER_IP="5", **LIVEKIT)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        for _ in range(5):
            assert _say(client, _new_browser(client))["bubbles"] == ["はい、どうぞ。"]
        assert model.calls == 5
        # 6人目(新しい Cookie)からは、どの経路でも LLM を呼ばずに IP の上限の案内
        uid = _new_browser(client)
        r = _say(client, uid)
        assert r["limited"] and r["bubbles"] == [IP_LIMIT_MESSAGE]
        assert _stream(client, uid, "message", "hi")[-1]["bubbles"] == [IP_LIMIT_MESSAGE]
        assert client.post("/api/event", json={"user_id": uid, "event": "form_exit"}).json()["sent"] is False
        r = client.post("/api/voice/start", json={"user_id": uid})
        assert r.status_code == 429 and r.json()["detail"] == IP_LIMIT_MESSAGE
        assert model.calls == 5
        # 止めたターンは、人・IP・全体のどれにも数えない
        quota = server.app.state.quota
        assert quota.used(uid) == 0 and quota.ip_used("testclient") == 5 and quota.total_used() == 5


def test_new_ids_from_one_ip_are_capped_and_do_not_grow_the_state_file(tmp_path, monkeypatch):
    """1つの IP から1日に使える匿名IDの数を超えると、挨拶も返さず、状態ファイルにユーザーが増えない。"""
    server, model = _server(tmp_path, monkeypatch, None, AICA_DAILY_NEW_IDS_PER_IP="2")
    from fastapi.testclient import TestClient

    def users() -> set[str]:
        return set(json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["users"])

    def open_and_greet(client) -> list[dict]:
        opened = client.post("/api/talk/open").json()
        r = client.post("/api/event/stream", json={"user_id": opened["user"], "event": opened["event"],
                                                   "thread": opened["thread"]})
        return [json.loads(x) for x in r.text.splitlines()]

    with TestClient(server.app) as client:
        first, second = _new_browser(client), None
        assert open_and_greet(client)[-1]["bubbles"] == ["はい、どうぞ。"]
        second = _new_browser(client)
        assert open_and_greet(client)[-1]["bubbles"] == ["はい、どうぞ。"]
        before = users()
        assert {first, second} <= before
        third = _new_browser(client)
        last = open_and_greet(client)[-1]
        assert last["limited"] and last["bubbles"] == [IP_LIMIT_MESSAGE]
        assert users() == before and third not in users() and model.calls == 2
        assert third not in json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))["counts"]
        # ターンを使わずに状態へ書く API(シナリオの開始・プロフィールの書き換え)も、新しい匿名IDでは書かない
        for _ in range(3):
            _new_browser(client)
            for r in (client.post("/api/scenarios/F-follow-new/start"),
                      client.put("/api/profile/track", json={"track": "正社員"}),
                      client.put("/api/profile/items/consultation_summary", json={"value": "飲食で探している"}),
                      client.delete("/api/profile/saved/J-001")):
                assert r.status_code == 429 and r.json()["detail"] == IP_LIMIT_MESSAGE
        assert users() == before and model.calls == 2
        # すでに使った匿名IDは、同じ IP から続けて使える(ターンも、シナリオの開始・プロフィールの書き換えも)
        client.cookies.clear()
        client.cookies.set("aica_uid", first)
        assert _say(client, first)["bubbles"] == ["はい、どうぞ。"]
        assert client.post("/api/scenarios/F-follow-new/start").status_code == 200
        assert client.put("/api/profile/track", json={"track": "正社員"}).status_code == 200


def test_admit_registers_a_new_id_without_counting_a_turn():
    """admit は IP の匿名IDの数だけを確かめ、空きがあれば0ターンで加える(ターンの上限は見ない・数えない)。"""
    q = DailyQuota(1, ip_ids=2, ip_limit=1, total_limit=1)
    assert q.take("w-a", "1.1.1.1")  # 人・IP・全体のターンの上限はすべて使い切った
    assert q.admit("w-a", "1.1.1.1") is None and q.admit("w-b:sim", "1.1.1.1") is None
    assert q.admit("w-b", "1.1.1.1") is None  # 同じ人(シナリオユーザーは匿名IDにまとめる)
    assert q.admit("w-c", "1.1.1.1") == IP_LIMIT_MESSAGE
    assert q.admit("w-c", "2.2.2.2") is None  # 別の IP には空きがある
    assert q.used("w-b") == 0 and q.ip_used("1.1.1.1") == 1 and q.total_used() == 1
    assert DailyQuota(1).admit("w-z", "1.1.1.1") is None  # 匿名IDの数の上限なし(0)


def test_turns_from_many_ips_stop_at_the_total_limit(tmp_path, monkeypatch):
    """別々の IP(プロキシの後ろ)からの合計が、全体の上限で止まる。案内は全体の上限と分かる文。"""
    server, model = _server(tmp_path, monkeypatch, None, AICA_DAILY_TURN_LIMIT_TOTAL="3", AICA_TRUST_PROXY="1")
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        for i in range(3):
            assert _say(client, _new_browser(client), {"X-Forwarded-For": f"203.0.113.{i}"})["bubbles"] == [
                "はい、どうぞ。"]
        r = _say(client, _new_browser(client), {"X-Forwarded-For": "203.0.113.99"})
        assert r["limited"] and r["bubbles"] == [TOTAL_LIMIT_MESSAGE] and "本日はたくさんのご利用があり" in r["bubbles"][0]
        assert model.calls == 3 and server.app.state.quota.ip_used("203.0.113.99") == 0


def test_spoofed_forwarded_for_is_ignored_unless_the_proxy_is_trusted(tmp_path, monkeypatch):
    """AICA_TRUST_PROXY が無ければ X-Forwarded-For を見ない(書き換えても IP の上限を迂回できない)。
    あれば最後の値(プロキシが付けた値)で数え、本人が先頭に書いた値は使わない。"""
    server, model = _server(tmp_path, monkeypatch, None, AICA_DAILY_TURN_LIMIT_PER_IP="2")
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        for i in range(2):
            _say(client, _new_browser(client), {"X-Forwarded-For": f"198.51.100.{i}"})
        r = _say(client, _new_browser(client), {"X-Forwarded-For": "198.51.100.7"})
        assert r["bubbles"] == [IP_LIMIT_MESSAGE] and model.calls == 2
        assert server.app.state.quota.ip_used("testclient") == 2

    server, model = _server(tmp_path / "proxied", monkeypatch, None, AICA_DAILY_TURN_LIMIT_PER_IP="2",
                            AICA_TRUST_PROXY="1")
    with TestClient(server.app) as client:
        quota = server.app.state.quota
        for i in range(2):  # 先頭を毎回書き換えても、プロキシが付けた最後の値で数える
            _say(client, _new_browser(client), {"X-Forwarded-For": f"10.0.0.{i}, 192.0.2.1"})
        assert quota.ip_used("192.0.2.1") == 2 and quota.ip_used("10.0.0.0") == 0
        assert _say(client, _new_browser(client), {"X-Forwarded-For": "10.0.0.9, 192.0.2.1"})["limited"]
        # 別の IP(プロキシが付けた値が違う)は使える
        assert _say(client, _new_browser(client), {"X-Forwarded-For": "192.0.2.2"})["bubbles"] == ["はい、どうぞ。"]
        assert model.calls == 3 and quota.ip_used("testclient") == 0


def test_ip_and_total_limits_come_from_env_and_zero_means_unlimited(monkeypatch):
    for k in QUOTA_ENVS:
        monkeypatch.delenv(k, raising=False)
    assert (ip_limit(), ip_ids_limit(), total_limit()) == (DEFAULT_IP_LIMIT, DEFAULT_IP_IDS, DEFAULT_TOTAL_LIMIT)
    assert (DEFAULT_IP_LIMIT, DEFAULT_IP_IDS, DEFAULT_TOTAL_LIMIT) == (150, 20, 1000)
    monkeypatch.setenv("AICA_DAILY_TURN_LIMIT_PER_IP", "7")
    monkeypatch.setenv("AICA_DAILY_NEW_IDS_PER_IP", "3")
    monkeypatch.setenv("AICA_DAILY_TURN_LIMIT_TOTAL", "0")
    assert (ip_limit(), ip_ids_limit(), total_limit()) == (7, 3, 0)

    q = DailyQuota(0, ip_limit=0, ip_ids=0, total_limit=-1)
    assert all(q.take(f"w-{i}", "1.1.1.1") for i in range(200))
    q = DailyQuota(0, ip_limit=2, ip_ids=0, total_limit=0)
    assert q.take("w-1", "a") and q.take("w-2", "a") and not q.take("w-3", "a") and q.take("w-3", "b")
    assert q.take("w-4")  # IP を渡さない呼び出し(評価など)は IP の上限を見ない


def test_a_blocked_turn_counts_nowhere_and_refund_returns_all_three():
    q = DailyQuota(1, ip_limit=3, ip_ids=2, total_limit=4)
    assert q.try_take("w-1:sim-new", "a") is None
    assert q.try_take("w-1:sim-exit", "a") == LIMIT_MESSAGE  # 匿名IDの上限: IP にも全体にも数えない
    assert (q.used("w-1"), q.ip_used("a"), q.total_used()) == (1, 1, 1)
    assert q.try_take("w-2", "a") is None
    assert q.try_take("w-3", "a") == IP_LIMIT_MESSAGE  # この IP から3つ目の匿名ID
    assert (q.used("w-3"), q.ip_used("a"), q.total_used()) == (0, 2, 2)
    q.refund("w-2", "a")  # 返すと、人・IP・全体のすべてが戻る
    assert (q.used("w-2"), q.ip_used("a"), q.total_used()) == (0, 1, 1)
    assert q.try_take("w-3", "a") == IP_LIMIT_MESSAGE  # 使われた匿名IDとしては残る(状態に書かれたかもしれない)
    assert q.try_take("w-2", "a") is None and q.try_take("w-4", "b") is None and q.try_take("w-5", "c") is None
    assert q.try_take("w-6", "d") == TOTAL_LIMIT_MESSAGE and q.total_used() == 4


def test_ip_counts_are_saved_and_reset_on_the_next_day(tmp_path):
    clock = Clock(datetime(2026, 9, 27, 23, 50, tzinfo=JST))
    path = tmp_path / "quota.json"
    q = DailyQuota(0, path, clock=clock, ip_limit=1)
    assert q.take("w-1", "a") and not q.take("w-2", "a")
    assert not DailyQuota(0, path, clock=clock, ip_limit=1).take("w-3", "a")  # 再起動しても当日の分は残る
    clock.now += timedelta(minutes=15)  # 日本時間の日付が変わると数え直す(IP も消える)
    assert q.take("w-2", "a") and "w-1" not in json.loads(path.read_text(encoding="utf-8"))["ips"]["a"]


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
