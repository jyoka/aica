"""入口イベント(初回訪問 / 再訪)と再訪の挨拶の台本テスト(実 LLM なし、dip の MCP は本物)。

挨拶は偽のモデルが、ランタイムから受け取ったものだけで決める: システムプロンプトに入った dip の入口のシナリオ
(入口V / 入口R)と、get_user_conversation_summary の結果(前に話した希望 / 最近の閲覧)。
なので「挨拶が変わる」ことは、イベント → 入口のシナリオ → dip のプロフィールと行動履歴 のつながりを確かめている。
"""

from __future__ import annotations

import json
import re

from langchain_core.messages import AIMessage

from dip_mcp import logic
from dip_mcp.data import State
from evals.checks import conversation_checks
from line_runtime.runtime import _tool_payload
from scenarios import load_scenarios, prepare
from tests.test_handle import ScriptedModel, call, reply, run

SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
MENU = ["求人を探す", "応募履歴を確認", "使い方", "今はいい"]


class Greeter(ScriptedModel):
    """script を順に返す。callable の項目は、それまでのメッセージを見て返事を作る。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        item = self.script.pop(0)
        msg = item(messages) if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _this_turn(messages) -> list:
    last_human = max(i for i, m in enumerate(messages) if m.type == "human")
    return messages[last_human + 1:]


def _tool_result(messages, name: str) -> dict:
    return next(_tool_payload(m) for m in reversed(_this_turn(messages)) if m.type == "tool" and m.name == name)


def greet(messages) -> AIMessage:
    """dip の入口のシナリオどおりに挨拶する(初回訪問はメニュー、再訪は要約を読んでから)。"""
    system = messages[0].content
    uid = re.search(r"LINE ユーザーID: ([^(\s]+)", system).group(1)
    if "入口V(初回訪問)" in system:
        return reply(["こんにちは。バイト探しをお手伝いします。何をしますか?"], quick_replies=MENU)
    assert "入口R(再訪)" in system
    if not any(m.type == "tool" for m in _this_turn(messages)):
        return call("get_user_conversation_summary", {"line_user_id": uid}, "sum")
    summary = _tool_result(messages, "get_user_conversation_summary")
    prefs = {k: v["value"] for k, v in (summary.get("stated_preferences") or {}).items()}
    if prefs:
        said = "・".join(prefs[k] for k in ("area", "time_band", "job_type") if k in prefs)
        return reply([f"おかえりなさい。前回は{said}でしたが、今回も同じですか?"],
                     quick_replies=["同じで探す", "少し変えたい", "今はいい"])
    viewed = (summary.get("recent_views") or {}).get("by_job_type") or {}
    if viewed:
        top = max(viewed, key=viewed.get)
        return reply([f"おかえりなさい。最近は{top}のお仕事を見ていましたか?"],
                     quick_replies=["そう、探したい", "ほかの仕事", "今はいい"])
    return reply(["おかえりなさい。何をしますか?"], quick_replies=MENU)


def propose(messages) -> AIMessage:
    """このターンに recommend_jobs が返した求人をそのままカードにする。"""
    jobs = _tool_result(messages, "recommend_jobs")["jobs"]
    return reply(["条件に合う求人です。気になるものはありますか?"], job_ids=[j["job_id"] for j in jobs],
                 quick_replies=["詳しく見る", "ほかの求人", "今はいい"])


def search(uid: str, save: dict, query: dict) -> AIMessage:
    """本人が言ったことを保存しつつ、その条件で探す(並列のツール呼び出し)。"""
    return AIMessage(content="", tool_calls=[
        {"name": "save_user_preferences", "args": {"line_user_id": uid, "preferences": save}, "id": "save",
         "type": "tool_call"},
        {"name": "recommend_jobs", "args": {"line_user_id": uid, **query}, "id": "rec", "type": "tool_call"}])


def close(uid: str) -> list[AIMessage]:
    return [call("record_conversation_outcome", {"line_user_id": uid, "outcome": "propose",
                                                 "reason": "求人を提案した", "confidence": 0.8}, "rec_out"),
            reply(["承知しました。またいつでもどうぞ。"], quick_replies=["求人を探す", "使い方"])]


# ----------------------------- トーク UI: 初回訪問と再訪で挨拶が変わる -----------------------------


def test_talk_ui_greets_first_visit_and_revisit_differently(tmp_path, monkeypatch):
    """画面と同じ呼び方(シナリオを始める → /api/event/stream)で、入口ごとに挨拶が変わる。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    from fastapi.testclient import TestClient

    from line_runtime import config, runtime, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    model = Greeter(script=[greet] * 5)  # 初回訪問 1回 + 再訪 2回ずつ(要約を読む → 挨拶)
    monkeypatch.setattr(server, "LineRuntime", lambda: runtime.LineRuntime(model=model, in_memory=True))

    with TestClient(server.app) as client:
        client.get("/")  # ブラウザと同じく匿名ユーザーIDの Cookie を持つ
        listed = {s["id"]: s for s in client.get("/api/scenarios").json()}
        assert listed["F-follow-new"]["event"] == "visit"
        assert listed["R-revisit"]["event"] == listed["R-revisit-browsing"]["event"] == "revisit"

        def open_talk(scenario_id: str) -> dict:
            started = client.post(f"/api/scenarios/{scenario_id}/start").json()
            r = client.post("/api/event/stream", json={"user_id": started["user"], "event": started["event"]})
            evs = [json.loads(line) for line in r.text.splitlines()]
            assert evs[0] == {"kind": "start", "event": started["event"], "entry": started["event"],
                              "event_kind": "greet", "model": config.MODEL}
            return evs[-1]

        first, back_prefs, back_views = (open_talk(s) for s in ("F-follow-new", "R-revisit", "R-revisit-browsing"))

    assert first["entry"] == "visit" and first["tools"] == []  # 初回訪問は履歴を読まずにメニュー
    assert first["bubbles"] == ["こんにちは。バイト探しをお手伝いします。何をしますか?"]
    assert first["quick_replies"] == MENU
    for r in (back_prefs, back_views):  # 再訪は要約(プロフィールと行動履歴)を読んでから挨拶する
        assert r["entry"] == "revisit" and [t["name"] for t in r["tools"]] == ["get_user_conversation_summary"]
    assert back_prefs["bubbles"] == ["おかえりなさい。前回は渋谷・日中・カフェでしたが、今回も同じですか?"]
    assert back_views["bubbles"] == ["おかえりなさい。最近はカフェのお仕事を見ていましたか?"]
    assert model.script == []


# ----------------------------- 再訪(希望あり) -----------------------------


def test_revisit_with_preferences_confirms_and_takes_only_the_difference(tmp_path):
    """前回の希望を確認形で出し、差分(時間帯だけ夕方)を答えると提案が変わる。保存済みの項目は聞き直さない。"""
    sc = SCENARIOS["R-revisit"]
    uid = sc["user"]
    state = State(tmp_path / "state.json")
    prepare(state, sc)
    model = Greeter(script=[
        greet, greet,
        # 差分だけを保存し、残りは前の希望(渋谷・カフェ)のまま探す
        search(uid, {"time_band": "夕方"}, {"area": "渋谷", "time_band": "夕方", "job_type": "カフェ"}),
        propose,
        *close(uid),
        greet, greet,  # もう一度トークを開き直す
    ])
    user_msgs = [None, "同じで、時間帯だけ夕方にしたい", "ありがとう、考えます"]
    turns = run(model, [(uid, "revisit", None), (uid, "message", user_msgs[1]), (uid, "message", user_msgs[2]),
                        (uid, "revisit", None)], tmp_path)
    t1, t2, _, again = turns

    assert t1["bubbles"] == ["おかえりなさい。前回は渋谷・日中・カフェでしたが、今回も同じですか?"]
    rec = next(t for t in t2["tools"] if t["name"] == "recommend_jobs")
    assert rec["args"] == {"line_user_id": uid, "area": "渋谷", "time_band": "夕方", "job_type": "カフェ"}
    assert t2["cards"] and [c["job_id"] for c in t2["cards"]] == [j["job_id"] for j in rec["result"]["jobs"]]
    from dip_mcp.data import load_jobs

    jobs = load_jobs()
    assert all("夕方" in jobs[c["job_id"]].time_bands and jobs[c["job_id"]].job_type == "カフェ" for c in t2["cards"])
    # 本人が話した希望は差分だけ更新され、次の再訪の確認にそのまま出る
    prefs = {k: v["value"] for k, v in logic.stated_preferences(State(tmp_path / "state.json").user(uid)).items()}
    assert prefs == {"job_type": "カフェ", "area": "渋谷", "time_band": "夕方"}
    assert again["bubbles"] == ["おかえりなさい。前回は渋谷・夕方・カフェでしたが、今回も同じですか?"]

    checks = conversation_checks(sc, turns[:3], user_msgs)
    assert checks["violations"] == [] and checks["outcome_as_expected"] and checks["branch_within_3"]


# ----------------------------- 再訪(行動履歴のみ) -----------------------------


def test_revisit_with_only_browsing_mentions_it_once_without_listing_jobs(tmp_path):
    """話した希望の無い人には、最近の閲覧に1回だけ触れる。求人名・件数・カードを並べない。"""
    sc = SCENARIOS["R-revisit-browsing"]
    uid = sc["user"]
    prepare(State(tmp_path / "state.json"), sc)
    said = {"area": "渋谷", "time_band": "日中", "job_type": "カフェ"}
    model = Greeter(script=[greet, greet, search(uid, said, said), propose, *close(uid)])
    user_msgs = [None, "そう、渋谷で日中のカフェを探したい", "ありがとう、考えます"]
    turns = run(model, [(uid, "revisit", None), (uid, "message", user_msgs[1]), (uid, "message", user_msgs[2])],
                tmp_path)
    t1, t2, t3 = turns

    summary = t1["tools"][0]["result"]
    assert summary["stated_preferences"] == {} and summary["recent_views"]["count_30d"] == 3  # 行動履歴だけの人
    assert t1["bubbles"] == ["おかえりなさい。最近はカフェのお仕事を見ていましたか?"]
    assert t1["cards"] == [] and not re.search(r"\d+\s*件", t1["bubbles"][0])
    assert t2["cards"] and all("見てい" not in b for b in t2["bubbles"] + t3["bubbles"])  # 触れるのは1回だけ

    checks = conversation_checks(sc, turns, user_msgs)
    assert checks["violations"] == [] and checks["outcome_as_expected"]
