"""LineRuntime.handle を、決められた順に返事をする偽のモデルで通しで動かす(実 LLM なし、dip の MCP は本物)。"""

from __future__ import annotations

import asyncio
import json
import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedModel(BaseChatModel):
    """script の AIMessage を順に返す。"""

    script: list = []  # noqa: RUF012  pydantic の項目

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def call(name: str, args: dict, id_: str = "t1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": id_, "type": "tool_call"}])


def reply(bubbles: list[str], job_ids=(), quick_replies=("今はいい",)) -> AIMessage:
    return AIMessage(content=json.dumps({"bubbles": bubbles, "quick_replies": list(quick_replies),
                                         "job_ids": list(job_ids), "buttons": []}, ensure_ascii=False))


def run(model, steps, tmp_path):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            return [await rt.handle(*s) for s in steps]

    return asyncio.run(go())


def test_recommended_jobs_come_back_as_cards(tmp_path):
    model = ScriptedModel(script=[
        call("recommend_jobs", {"line_user_id": "sim-new", "area": "渋谷", "time_band": "日中"}),
        reply(["渋谷・日中で3件です。"], job_ids=["b0601", "b1202", "a003"]),
    ])
    (r,) = run(model, [("sim-new", "message", "渋谷で日中")], tmp_path)
    assert [c["job_id"] for c in r["cards"]] == ["b0601", "b1202", "a003"]
    assert r["unknown_job_ids"] == [] and r["tools"][0]["name"] == "recommend_jobs"


def test_cards_survive_parallel_tool_calls_and_multiple_turns(tmp_path):
    both = AIMessage(content="", tool_calls=[
        {"name": "save_user_preferences", "args": {"line_user_id": "sim-new", "preferences": {"area": "渋谷"}},
         "id": "t1", "type": "tool_call"},
        {"name": "recommend_jobs", "args": {"line_user_id": "sim-new", "area": "渋谷", "time_band": "日中"},
         "id": "t2", "type": "tool_call"}])
    model = ScriptedModel(script=[
        reply(["どこで働きたいですか?"]),
        both,
        reply(["渋谷・日中で3件です。"], job_ids=["b0601", "b1202", "a003"]),
        call("answer_job_question", {"question": "仕事内容", "job_id": "b1202"}, "t3"),
        reply(["チャット中心の事務です。"], job_ids=["b1202"]),
    ])
    r1, r2, r3 = run(model, [("sim-new", "visit", None), ("sim-new", "message", "渋谷周りでいい"),
                              ("sim-new", "message", "オフィス事務")], tmp_path)
    assert r1["cards"] == []
    assert [c["job_id"] for c in r2["cards"]] == ["b0601", "b1202", "a003"]
    assert [c["job_id"] for c in r3["cards"]] == ["b1202"]


def test_ima_wa_ii_ends_the_conversation_politely(tmp_path):
    """「今はいい」の台本: 質問や求人を続けず、wait を記録して丁寧に終える(配信の停止処理はもう無い)。"""
    from evals.checks import conversation_checks

    model = ScriptedModel(script=[
        reply(["こんにちは。バイト探しをお手伝いします。何かお探しですか?"],
              quick_replies=["求人を探す", "応募履歴を確認", "使い方", "今はいい"]),
        call("record_conversation_outcome",
             {"line_user_id": "sim-new", "outcome": "wait", "reason": "今はいい", "confidence": 0.9}),
        reply(["承知しました。またいつでもお声がけください。"], quick_replies=["求人を探す", "使い方"]),
    ])
    r1, r2 = run(model, [("sim-new", "visit", None), ("sim-new", "message", "今はいい")], tmp_path)
    rec = next(t for t in r2["tools"] if t["name"] == "record_conversation_outcome")
    assert rec["result"]["recorded"]["outcome"] == "wait"  # 記録されるだけで、配信の抑制のような副作用はない
    checks = conversation_checks({"expect_outcome": ["wait"]}, [r1, r2], [None, "今はいい"])
    assert checks["outcome_as_expected"] and checks["violations"] == []


def test_handle_stream_reports_llm_and_tool_steps_before_the_reply(tmp_path):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    model = ScriptedModel(script=[
        call("recommend_jobs", {"line_user_id": "sim-new", "area": "渋谷", "time_band": "日中"}),
        reply(["渋谷・日中で3件です。"], job_ids=["b0601"]),
    ])

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            return [ev async for ev in rt.handle_stream("sim-new", "message", "渋谷で日中")]

    evs = asyncio.run(go())
    assert [e["kind"] for e in evs] == ["llm", "tool_start", "tools", "llm", "reply"]
    assert evs[0]["tool_calls"] == ["recommend_jobs"]
    # tool_start は実行の始まり(画面が「探しています…」を出す)。引数ごと届き、終わりの tools で消す
    assert evs[1]["calls"] == [{"name": "recommend_jobs", "args": {"line_user_id": "sim-new", "area": "渋谷", "time_band": "日中"}}]
    assert evs[2]["calls"][0]["name"] == "recommend_jobs" and evs[2]["calls"][0]["args"]["area"] == "渋谷"
    assert "件を返す" in evs[2]["calls"][0]["summary"]
    assert [c["job_id"] for c in evs[-1]["cards"]] == ["b0601"]


def test_talk_screen_handles_tool_start_and_has_per_tool_wait_labels():
    """トーク画面が tool_start を受けて「何をしているか」を出し、tools で消す(ツールごとの一言がある)。"""
    from line_runtime import config

    html = (config.ROOT / "line_runtime" / "static" / "talk.html").read_text(encoding="utf-8")
    assert 'ev.kind === "tool_start"' in html
    assert "ぴったりの求人を探しています" in html  # recommend_jobs
    assert "ほかの求人を探しています" in html  # recommend_jobs + exclude_ids(次の候補)
    assert "条件を見直して探しています" in html  # relax_conditions
    assert html.index('ev.kind === "tool_start"') < html.index('ev.kind === "tools"')  # 出してから消す


def test_stream_endpoint_reports_a_denied_push_without_calling_the_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # モデルは作るが呼ばない
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    from fastapi.testclient import TestClient

    from dip_mcp.data import State
    from line_runtime import server

    server.state = State(tmp_path / "state.json")
    server.state.reset()
    with TestClient(server.app) as client:
        r = client.post("/api/event/stream", json={"user_id": "sim-new", "event": "form_exit"})
        evs = [json.loads(line) for line in r.text.splitlines()]
    assert [e["kind"] for e in evs] == ["start", "gate", "reply"]
    assert evs[1]["allowed"] is False and "未連携" in evs[1]["reason"]
    assert evs[2]["sent"] is False


def test_stream_endpoint_turns_a_failure_after_tool_start_into_an_error_event(tmp_path, monkeypatch):
    """ツールが始まった後に落ちても error イベントが届く(画面は「探しています…」を出しっぱなしにしない)。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # モデルは作るが呼ばない
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    from fastapi.testclient import TestClient

    from line_runtime import server

    class FailingRuntime:
        """ツールの実行が始まった直後に落ちるランタイム(タイムアウト・MCP 断の代わり)。"""

        async def handle_stream(self, user_id, event, text=None, facts=None):
            yield {"kind": "llm", "ms": 1, "tool_calls": ["recommend_jobs"]}
            yield {"kind": "tool_start", "calls": [{"name": "recommend_jobs", "args": {"area": "渋谷"}}]}
            raise RuntimeError("MCP が落ちた")

    with TestClient(server.app) as client:
        client.app.state.rt = FailingRuntime()
        r = client.post("/api/event/stream", json={"user_id": "sim-new", "event": "message", "text": "渋谷で"})
        evs = [json.loads(line) for line in r.text.splitlines()]
    assert [e["kind"] for e in evs] == ["start", "llm", "tool_start", "error"]
    assert "MCP が落ちた" in evs[-1]["message"]


def test_turn_finishes_even_if_the_client_stops_listening(tmp_path):
    """画面との接続が切れて途中で受け取りをやめても、ターンは最後まで進む(ツールの結果が無いまま保存されない)。"""
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    model = ScriptedModel(script=[
        call("recommend_jobs", {"line_user_id": "sim-new", "area": "渋谷"}),
        reply(["3件です。"], job_ids=["b0601"]),
        reply(["ほかの条件も探せます。"]),
    ])

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            stream = rt.handle_stream("sim-new", "message", "渋谷で")
            first = await stream.__anext__()  # 最初の LLM の呼び出しだけ受け取って、切断する
            await stream.aclose()
            second = await rt.handle("sim-new", "message", "ほかには?")  # 前のターンが終わるのを待ってから進む
            cfg = rt._cfg("sim-new", await rt._current("sim-new"))
            msgs = (await rt.agent.aget_state(cfg)).values["messages"]
            return first, second, [m.type for m in msgs]

    first, second, types = asyncio.run(go())
    assert first["kind"] == "llm"
    assert second["bubbles"] == ["ほかの条件も探せます。"]
    assert types == ["human", "ai", "tool", "ai", "human", "ai"]  # ツールの結果まで保存されている
