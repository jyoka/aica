"""雇用区分の記録と、発話からの切り替え(set_search_track、ADR-0001)の台本テスト。実 LLM なし、dip の MCP は本物。

合図の強弱の判断は LLM の仕事で、ここでは台本がその判断を演じる。確かめるのは、
(1) 切り替えの合図の決まりが dip の Prompt から LINE 側エージェントに届いていること、
(2) 台本どおりに呼んだ set_search_track が区分・根拠・取り消しを dip に記録し、要約の search_track に出ること、
(3) 切り替えない場面(弱い合図・履歴書だけ)で区分が未定のまま残ること、(4) 次の会話(再訪)に区分が引き継がれること、
(5) 他人のユーザーとして記録できないこと。LLM が合図を正しく見分けるかは、評価(実 API)で測る。
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from dip_mcp import logic
from dip_mcp.data import State
from evals.checks import conversation_checks, turn_violations
from evals.scenarios import load_scenarios
from line_runtime.agent import PINNED_USER
from line_runtime.runtime import _tool_payload
from resume_draft.schema import ResumeDraft
from tests.test_handle import ScriptedModel, call, reply, run

SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
UID = "sim-new"
MENU = ["求人を探す", "まず相談する", "応募履歴を確認", "今はいい"]
WELCOME = reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU)
CONFIRM = "正社員の仕事と、空いた時間のアルバイト、どちらを考えていますか?"


class TrackModel(ScriptedModel):
    """script を順に返す。callable の項目は、それまでのメッセージを見て返事を作る。受け取ったシステムプロンプトも残す。"""

    systems: list = []  # noqa: RUF012  pydantic の項目

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        if messages and messages[0].type == "system":
            self.systems.append(messages[0].content)
        item = self.script.pop(0)
        msg = item(messages) if callable(item) else item
        return ChatResult(generations=[ChatGeneration(message=msg)])


def switch(track: str, evidence: str, undo: bool = False, uid: str = UID) -> AIMessage:
    args = {"line_user_id": uid, "track": track, "evidence": evidence} | ({"undo": True} if undo else {})
    return call("set_search_track", args, "track")


def summary(tmp_path, uid: str = UID) -> dict:
    return logic.conversation_summary(State(tmp_path / "state.json"), uid)


def tool_names(turn: dict) -> list[str]:
    return [t["name"] for t in turn["tools"]]


def _this_turn(messages) -> list:
    last_human = max(i for i, m in enumerate(messages) if m.type == "human")
    return messages[last_human + 1:]


def _summary_in_turn(messages) -> dict:
    return next(_tool_payload(m) for m in reversed(_this_turn(messages))
                if m.type == "tool" and m.name == "get_user_conversation_summary")


# ----------------------------- dip のツールと要約 -----------------------------


def test_new_user_starts_undecided_and_set_search_track_records_track_and_evidence(tmp_path):
    state = State(tmp_path / "state.json")
    for uid in ("sim-new", "sim-browsing"):  # 未連携の新規も、行動履歴のある人も、初回は未定
        s = logic.conversation_summary(state, uid)
        assert s["search_track"] == "未定" and s["track_history"] == []

    r = logic.set_search_track(state, UID, "正社員", "  正社員で探したい ")
    assert r["changed"] and r["track"] == "正社員"
    s = logic.conversation_summary(state, UID)
    assert s["search_track"] == "正社員"
    assert s["track_history"] == [{"when": "9/23", "from": "未定", "track": "正社員", "evidence": "正社員で探したい",
                                   "undo": False}]
    assert "search_track" in s["note"]
    # 今と同じ区分なら記録しない(履歴が重複で埋まらない)
    assert logic.set_search_track(state, UID, "正社員", "正社員でお願いします") == {"track": "正社員", "changed": False}
    assert len(logic.conversation_summary(state, UID)["track_history"]) == 1


def test_set_search_track_rejects_unknown_track_missing_evidence_and_strips_contact_details(tmp_path):
    state = State(tmp_path / "state.json")
    assert "error" in logic.set_search_track(state, UID, "契約社員", "契約社員で")  # 3つの区分だけ
    assert "error" in logic.set_search_track(state, UID, "正社員", "  ")  # 根拠の発言なしには切り替えない
    assert logic.conversation_summary(state, UID)["search_track"] == "未定"
    logic.set_search_track(state, UID, "正社員", "正社員希望です。090-1234-5678 に連絡ください")
    (h,) = logic.conversation_summary(state, UID)["track_history"]
    assert "090" not in h["evidence"] and "[個人情報を削除]" in h["evidence"]


def test_set_search_track_strips_addresses_from_the_evidence(tmp_path):
    """根拠の発言も、希望の保存・まとめと同じ住所の決まりで伏せる(住所らしい所は、区切りまでの語ごと)。"""
    state = State(tmp_path / "state.json")
    r = logic.set_search_track(state, UID, "正社員", "渋谷区神南1-2-3に住んでいて、正社員で探したい")
    assert r["recorded"]["evidence"] == "[個人情報を削除]、正社員で探したい"
    (h,) = logic.conversation_summary(state, UID)["track_history"]
    assert "神南" not in h["evidence"] and "1-2-3" not in h["evidence"]
    # 住所でない数字の幅は伏せない
    r = logic.set_search_track(state, UID, "アルバイト・パート", "週3-4日、時給1200-1500円でバイトしたい")
    assert r["recorded"]["evidence"] == "週3-4日、時給1200-1500円でバイトしたい"


def test_undo_returns_to_the_previous_track_and_is_recorded(tmp_path):
    state = State(tmp_path / "state.json")
    assert "error" in logic.set_search_track(state, UID, "未定", "取り消し", undo=True)  # 取り消すものが無い
    logic.set_search_track(state, UID, "アルバイト・パート", "空いた時間にバイトを")
    logic.set_search_track(state, UID, "正社員", "正社員で探したい")
    # 取り消しの track は「切り替える前の区分」。違う区分を渡したら記録しない
    assert "アルバイト・パート" in logic.set_search_track(state, UID, "未定", "やっぱり違う", undo=True)["error"]
    r = logic.set_search_track(state, UID, "アルバイト・パート", "やっぱり正社員じゃなくていい", undo=True)
    assert r["changed"] and r["recorded"]["undo"] and r["recorded"]["from"] == "正社員"
    s = logic.conversation_summary(state, UID)
    assert s["search_track"] == "アルバイト・パート"
    assert [(h["from"], h["track"], h["undo"]) for h in s["track_history"]] == [
        ("未定", "アルバイト・パート", False), ("アルバイト・パート", "正社員", False), ("正社員", "アルバイト・パート", True)]
    # 取り消しの取り消しはしない(2回目の取り消しは記録しない)
    assert "error" in logic.set_search_track(state, UID, "正社員", "やっぱり", undo=True)


# ----------------------------- 台本: 切り替わる / 切り替わらない -----------------------------


@pytest.mark.parametrize(("said", "track"), [
    ("正社員で探したい", "正社員"),  # 明示
    ("転職を考えています", "正社員"),  # 明示
    ("年収は450万円以上で、キャリアを積める仕事がいい", "正社員"),  # 強い合図(年収・キャリア)
    ("学校と両立できるシフトで、時給1200円以上がいい", "アルバイト・パート"),  # 強い合図(シフト・時給・学校との両立)
])
def test_explicit_or_strong_signal_switches_in_one_turn(tmp_path, said, track):
    label = "正社員" if track == "正社員" else "アルバイト"
    model = TrackModel(script=[
        WELCOME,
        switch(track, said),
        reply([f"{label}のお仕事で考えますね。", "いま一番大事にしたいことは何ですか?"],
              quick_replies=["仕事内容", "働く場所", "それ以外", "今はいい"]),
    ])
    t1, t2 = run(model, [(UID, "visit", None), (UID, "message", said)], tmp_path)

    system = model.systems[-1]  # 合図の決まりが dip の Prompt から届いている
    assert "## 雇用区分" in system and "set_search_track" in system
    assert "年収・キャリア・転職の時期の話 → 正社員" in system and "シフト・時給・学校との両立の話 → アルバイト・パート" in system
    assert tool_names(t1) == [] and tool_names(t2) == ["set_search_track"]
    assert t2["tools"][0]["result"]["changed"] is True
    s = summary(tmp_path)
    assert s["search_track"] == track
    assert [(h["from"], h["track"], h["evidence"]) for h in s["track_history"]] == [("未定", track, said)]
    assert turn_violations(1, t2) == []


def test_weak_signal_asks_one_question_instead_of_switching(tmp_path):
    """「経験を活かしたい」では切り替えず、1問で確かめる。答えを根拠に切り替える。"""
    weak, answer = "経験を活かしたい", "正社員の仕事を考えています"
    model = TrackModel(script=[
        WELCOME,
        reply(["経験を活かしたいのですね。", CONFIRM], quick_replies=["正社員", "空いた時間のアルバイト", "それ以外", "今はいい"]),
        switch("正社員", answer),
        call("get_counseling_playbook", {"track": "正社員", "channel": "chat"}, "pb"),
        reply(["正社員のお仕事で考えますね。", "これまでの経験で、特に活かしたいのはどんなことですか?"],
              quick_replies=["それ以外", "今はいい"]),
    ])
    user_msgs = [None, weak, answer]
    turns = run(model, [(UID, "visit", None), (UID, "message", weak), (UID, "message", answer)], tmp_path)
    _, t2, t3 = turns

    assert "弱い・混在した合図" in model.systems[-1] and CONFIRM in model.systems[-1]
    assert "set_search_track" not in tool_names(t2)  # 弱い合図のターンでは切り替えない
    assert CONFIRM in t2["bubbles"] and "".join(t2["bubbles"]).count("?") == 1  # 確認は1問だけ
    assert tool_names(t3) == ["set_search_track", "get_counseling_playbook"]  # 切り替えたターンで playbook を読み直す
    s = summary(tmp_path)  # 記録は答えのターンの1件だけで、根拠は弱い合図ではなく答え
    assert s["search_track"] == "正社員"
    assert [h["evidence"] for h in s["track_history"]] == [answer]
    # 確認の1問は、評価の形式判定で違反にならない(負例)
    assert conversation_checks(SCENARIOS["F-follow-new"], turns, user_msgs)["violations"] == []


def _career_resume() -> dict:
    """正社員の経歴が厚い、架空の履歴書の読み取り結果。"""
    draft = ResumeDraft(
        job_summary="みほん商事で8年、法人営業とプロジェクトマネジメントを担当。",
        work_history=[{"period": "2017年4月〜2025年3月", "organization": "みほん商事", "employment_type": "正社員",
                       "role": "法人営業・PM", "duties": ["法人営業", "新規事業のPM"], "achievements": ["年間売上を1.5倍にした"]}],
        skills=["英語", "中国語"],
    )
    return {"format_version": "1.0", "created_at": "2026-09-23T11:00:00+09:00", "resume": draft.model_dump()}


def test_resume_alone_does_not_switch(tmp_path):
    """正社員の経歴が厚い履歴書を読んでも、本人が話すまで区分は未定のまま。確認の1問で聞く。"""
    logic.save_resume_upload(State(tmp_path / "state.json"), UID, _career_resume())

    def after_summary(messages) -> AIMessage:
        s = _summary_in_turn(messages)
        assert s["uploaded_resume"]["work_history"][0]["employment_type"] == "正社員"
        assert s["search_track"] == "未定"
        return reply(["履歴書では、法人営業とPMを8年されていたんですね。", CONFIRM],
                     quick_replies=["正社員", "空いた時間のアルバイト", "それ以外", "今はいい"])

    model = TrackModel(script=[
        WELCOME,
        call("get_user_conversation_summary", {"line_user_id": UID}, "sum"),
        after_summary,
    ])
    _, t2 = run(model, [(UID, "visit", None), (UID, "message", "履歴書をアップロードしました")], tmp_path)

    assert "履歴書(uploaded_resume)の内容・行動履歴・stated_preferences だけでは切り替えない" in model.systems[-1]
    assert tool_names(t2) == ["get_user_conversation_summary"]
    s = summary(tmp_path)
    assert s["search_track"] == "未定" and s["track_history"] == []


def test_undo_in_conversation_returns_to_the_previous_track(tmp_path):
    """アルバイトで探していた人が正社員に切り替え、次のターンで取り消す。元の区分に戻り、取り消しが記録される。"""
    logic.set_search_track(State(tmp_path / "state.json"), UID, "アルバイト・パート", "空いた時間にバイトを探したい")

    def undo_from_history(messages) -> AIMessage:
        last = _summary_in_turn(messages)["track_history"][-1]
        return switch(last["from"], "やっぱり正社員じゃなくていい", undo=True)

    model = TrackModel(script=[
        switch("正社員", "正社員で探したい"),
        reply(["正社員のお仕事で考えますね。", "どんなお仕事を考えていますか?"], quick_replies=["それ以外", "今はいい"]),
        call("get_user_conversation_summary", {"line_user_id": UID}, "sum"),
        undo_from_history,
        reply(["承知しました。アルバイトに戻して考えますね。", "働きたい時間帯はありますか?"],
              quick_replies=["日中", "夕方", "それ以外", "今はいい"]),
    ])
    _, t2 = run(model, [(UID, "message", "正社員で探したい"), (UID, "message", "やっぱり正社員じゃなくていい")], tmp_path)

    assert "undo=true" in model.systems[-1]
    undo = next(t for t in t2["tools"] if t["name"] == "set_search_track")
    assert undo["args"]["undo"] is True and undo["result"]["changed"] is True
    s = summary(tmp_path)
    assert s["search_track"] == "アルバイト・パート"
    assert [(h["track"], h["undo"]) for h in s["track_history"]] == [
        ("アルバイト・パート", False), ("正社員", False), ("アルバイト・パート", True)]
    assert s["track_history"][-1]["evidence"] == "やっぱり正社員じゃなくていい"


# ----------------------------- 台本: 再訪で区分が引き継がれる -----------------------------


def greet_from_track(messages) -> AIMessage:
    """再訪の入口のシナリオどおり、要約を読んでから、記録された区分の確認形の1問で挨拶する。"""
    system = messages[0].content
    assert "入口R(再訪)" in system
    if not any(m.type == "tool" for m in _this_turn(messages)):
        uid = PINNED_USER  # system prompt にユーザーIDは無い。LINE 側(PinUserMiddleware)が置き換える
        return call("get_user_conversation_summary", {"line_user_id": uid}, "sum")
    track = _summary_in_turn(messages)["search_track"]
    if track == "未定":
        return reply(["おかえりなさい。何をしますか?"], quick_replies=MENU)
    label = "正社員" if track == "正社員" else "アルバイト"
    return reply([f"おかえりなさい。前回は{label}のお仕事で考えていましたが、今回も{label}で探しますか?"],
                 quick_replies=[f"{label}で探す", "変えたい", "今はいい"])


def test_revisit_greeting_starts_from_the_recorded_track(tmp_path):
    """1回目の会話で正社員に切り替えた人が、トークを開き直すと、正社員の確認から始まる(区分を聞き直さない)。"""
    model = TrackModel(script=[
        WELCOME,
        switch("正社員", "転職を考えています"),
        reply(["正社員のお仕事で考えますね。", "転職で一番変えたいことは何ですか?"], quick_replies=["それ以外", "今はいい"]),
        greet_from_track, greet_from_track,
    ])
    *_, again = run(model, [(UID, "visit", None), (UID, "message", "転職を考えています"), (UID, "revisit", None)],
                    tmp_path)

    assert "search_track が未定でなければ、記録された区分から始める" in model.systems[-1]
    assert again["entry"] == "revisit" and tool_names(again) == ["get_user_conversation_summary"]
    assert again["tools"][0]["result"]["search_track"] == "正社員"
    assert again["bubbles"] == ["おかえりなさい。前回は正社員のお仕事で考えていましたが、今回も正社員で探しますか?"]
    assert model.script == []


# ----------------------------- 他人のユーザーとして記録できない -----------------------------


def test_another_browser_cannot_switch_someone_elses_track(tmp_path, monkeypatch):
    """ブラウザ A の会話で切り替えた区分は A のユーザーにだけ残る。B は A のユーザーとして話せない(=記録できない)。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    from fastapi.testclient import TestClient

    from line_runtime import config, runtime, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()
    model = TrackModel(script=[])
    monkeypatch.setattr(server, "LineRuntime", lambda: runtime.LineRuntime(model=model, in_memory=True))

    with TestClient(server.app) as a:
        b = TestClient(server.app)  # 別ブラウザ(Cookie を共有しない)
        a.get("/dev"), b.get("/dev")  # 画面を開いたときと同じく、ブラウザごとの匿名ユーザーIDの Cookie を持つ
        ua = a.post("/api/scenarios/F-follow-new/start").json()["user"]
        ub = b.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert ua != ub

        model.script += [switch("正社員", "正社員で探したい", uid=ua),  # A の匿名ユーザーIDが決まってから台本を用意する
                         reply(["正社員のお仕事で考えますね。", "どんなお仕事を考えていますか?"], quick_replies=["今はいい"])]
        r = a.post("/api/event", json={"user_id": ua, "event": "message", "text": "正社員で探したい"})
        assert r.status_code == 200 and [t["name"] for t in r.json()["tools"]] == ["set_search_track"]

        # B が A のユーザーとして話そうとしても、モデルを呼ぶ前に断られる
        for path in ("/api/event", "/api/event/stream"):
            denied = b.post(path, json={"user_id": ua, "event": "message", "text": "やっぱりバイトで"})
            assert denied.status_code == 403
        assert b.post("/api/dip", json={"user_id": ua}).status_code == 403  # 区分を覗くこともできない
        assert model.script == []

    assert summary(tmp_path, ua)["search_track"] == "正社員"
    assert summary(tmp_path, ub)["search_track"] == "未定"
    assert json.dumps(summary(tmp_path, ub), ensure_ascii=False).count("正社員で探したい") == 0
