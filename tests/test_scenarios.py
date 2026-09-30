"""シナリオの一覧(evals/scenarios.json)と、シナリオを始める準備(evals/scenarios.py)。実 LLM なし。

どのシナリオも AI 側の1通目(初回訪問・再訪の挨拶、dip のプッシュ)から始まること、
同じシナリオを何度始めても前回の接触でプッシュが止まらないことを確かめる。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from dip_mcp import logic
from dip_mcp.data import State
from evals.scenarios import load_scenarios, prepare
from line_runtime.runtime import EVENTS

SCENARIOS = load_scenarios()


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.json")
    s.reset()
    return s


def test_scenario_list_has_titles_and_no_user_first_start():
    ids = [sc["id"] for sc in SCENARIOS]
    assert len(ids) == len(set(ids))
    assert set(ids) == {"F-follow-new", "B-exit", "pii", "Q-job-question", "R-revisit", "R-revisit-browsing",  # 新規ユーザー系
                        "C-consult-propose", "C-consult-only", "C-consult-switch",  # 相談モード
                        # aica v2 の E: 区分つきの再訪と、雇用区分・相談の深さを見るペルソナ
                        "R-revisit-track", "T-career-fulltime", "T-side-job", "T-student-first", "T-blank-parent",
                        "T-switch-to-fulltime"}
    for sc in SCENARIOS:
        assert sc["title"] and sc["desc"] and sc["event"] in EVENTS
        assert sc["event"] != "message"  # ユーザーのメッセージ待ちから始まるものは無い


@pytest.mark.parametrize("sc", SCENARIOS, ids=[sc["id"] for sc in SCENARIOS])
def test_every_scenario_starts_from_the_ai_side_even_twice(state, sc):
    for _ in range(2):  # 2回目も、1回目のプッシュが接触として残って止まることがない
        prepare(state, sc)
        kind = EVENTS[sc["event"]][1]
        if kind == "push":
            gate = logic.push_trigger(state, sc["user"], sc["event"])
            assert gate["allowed"], gate
        else:
            assert kind == "greet"


def test_reset_user_leaves_other_users_alone(state):
    logic.push_trigger(state, "sim-exit", "form_exit")
    logic.save_preferences(state, "sim-new", {"area": "渋谷"})
    state.reset_user("sim-exit")
    state.reset_user("sim-unknown")  # fixtures に無い人でも落ちない
    assert state.user("sim-exit")["contacts"] == []
    assert logic.stated_preferences(state.user("sim-new"))  # ほかの人の状態は残る


def test_revisit_scenario_seeds_preferences_after_reset(state):
    sc = next(s for s in SCENARIOS if s["id"] == "R-revisit")
    logic.save_preferences(state, "sim-new", {"area": "池袋"})  # 前のシナリオで話した希望は消えて、種だけになる
    prepare(state, sc)
    prefs = logic.stated_preferences(state.user("sim-new"))
    assert {k: v["value"] for k, v in prefs.items()} == sc["seed_preferences"]


def test_start_endpoint_resets_the_user(tmp_path, monkeypatch):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # モデルは作るが呼ばない
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    from fastapi.testclient import TestClient

    from line_runtime import config, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()
    with TestClient(server.app) as client:
        listed = client.get("/api/scenarios").json()
        assert [s["id"] for s in listed] == [sc["id"] for sc in SCENARIOS]
        assert client.post("/api/scenarios/R-revisit/start").status_code == 401  # Cookie なしでは始められない
        client.get("/dev")
        anon = client.get("/api/me").json()["user_id"]
        r = client.post("/api/scenarios/R-revisit/start").json()
        assert r == {"user": f"{anon}:sim-new", "event": "revisit"}  # このブラウザ専用のシナリオユーザー
        assert logic.stated_preferences(server.state.user(r["user"]))  # 再訪の種(前に話した希望)が入る
        # 一度プッシュした後でも、始め直せばまたプッシュできる
        exit_user = f"{anon}:sim-exit"
        client.post("/api/scenarios/B-exit/start")
        assert logic.push_trigger(server.state, exit_user, "form_exit")["allowed"]
        client.post("/api/scenarios/B-exit/start")
        assert logic.push_trigger(server.state, exit_user, "form_exit")["allowed"]
        assert client.post("/api/scenarios/nope/start").status_code == 404


# ----------------------------- 評価ハーネスを偽のモデルで最後まで -----------------------------


class _FakeMessages:
    """模擬ユーザー: 毎回ひとこと返して会話を終える。"""

    async def parse(self, **kwargs):
        from evals.run import SimTurn

        return SimpleNamespace(parsed_output=SimTurn(text="ありがとう、今日はこれで", end=True))


def test_eval_harness_runs_every_scenario_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from evals.meter import Meter
    from evals.run import run_scenario
    from line_runtime.runtime import LineRuntime

    class AlwaysAsk(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "always-ask"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            msg = AIMessage(content=json.dumps({"bubbles": ["どうしますか?"], "quick_replies": ["今はいい"],
                                                "job_ids": [], "buttons": []}, ensure_ascii=False))
            return ChatResult(generations=[ChatGeneration(message=msg)])

    client = SimpleNamespace(messages=_FakeMessages())

    async def go():
        async with LineRuntime(model=AlwaysAsk(), in_memory=True) as rt:
            return [await run_scenario(rt, client, Meter(), sc, use_judge=False) for sc in SCENARIOS]

    results = asyncio.run(go())
    for sc, r in zip(SCENARIOS, results, strict=True):
        assert r["turns"] >= 2, sc["id"]
        assert r["transcript"][0]["who"] == "agent", sc["id"]  # 1通目は AI 側


def test_scenarios_after_a_server_restart_start_a_fresh_conversation(tmp_path, monkeypatch):
    """シナリオを何度か始めてからサーバーを再起動しても、次のシナリオは前の会話に積まれない。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    from fastapi.testclient import TestClient

    from line_runtime import config, runtime, server
    from tests.test_handle import ScriptedModel, reply

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)  # 会話は一時フォルダの SQLite に(再起動をまたいで残す)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    model = ScriptedModel(script=[reply(["こんにちは。何を探しますか?"]) for _ in range(3)])
    monkeypatch.setattr(server, "LineRuntime", lambda: runtime.LineRuntime(model=model))

    def start_visit(client):
        user = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        client.post("/api/event", json={"user_id": user, "event": "visit"}).json()
        return user

    browser = {}  # 再起動をまたいで同じブラウザ(Cookie)
    with TestClient(server.app) as client:
        client.get("/dev")
        browser = dict(client.cookies)
        start_visit(client)
        start_visit(client)
    with TestClient(server.app, cookies=browser) as client:  # 再起動
        user = start_visit(client)
        rt = server.app.state.rt

        async def history():
            cfg = rt._cfg(user, await rt._current(user))
            return [m.type for m in (await rt.agent.aget_state(cfg)).values["messages"]]

        assert client.portal.call(history) == ["human", "ai"]  # 前のシナリオの会話が混ざっていない
