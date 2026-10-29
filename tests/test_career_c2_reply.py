"""正社員ヒアリングの c2 の言う段(会話の層。dip_mcp/convo/reply.py、設計メモ docs/design/conversation-layer.md §4.8、§5 の 4 枚目)。

実 LLM なし。本人から見た流れを、LINE の Webhook と本物のランタイムと dip の MCP で通す(偽の LINE、偽のモデル)。偽のモデルの言う段は、
わざと単調な相づち(本人の言葉 +「ですね。」)を書く(jyoka/tasks#326 のトーク 1、3、5 の「〜ですね。」の代わり)。
- 相づちの終わりが 3 回続く返事は捨て、入り口の定義の決まった相づちで組む
- 区切りの行は、相づちの後、問いの前
- 読めない「はい」と、数の読めない年収には、モデルを呼ばずに、責めない注意と違う言い方の問いで出し直す(「読み取れなかった」と言わない)
- 雑談から戻るときも、前と違う言い方で同じ問いを出す
ほかに、場面の一覧(evals/scenes/)の 4 枚目の場面と、言う段のプロンプト、入り口の定義の言い方と調子を確かめる。
"""

from __future__ import annotations

import json
import re
from functools import partial
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from dip_mcp.career import c2 as C2
from dip_mcp.career import harness as H
from dip_mcp.career import session
from dip_mcp.convo import reply as RP
from dip_mcp.data import State
from line_runtime.career_chat import SCHEMAS
from line_runtime.line_channel import line_user
from tests.test_career_c2 import UID, act, run
from tests.test_career_chat import USAGE, current_qid, start_script
from tests.test_consult import ConsultModel
from tests.test_line_data import _say
from tests.test_line_webhook import RAW_USER, _server, line_id_key  # noqa: F401  line_id_key はフィクスチャ

SAY_JSON = re.compile(r"次に聞くこと\(JSON\): (\{.*\})")
NAMED_WHY = "みほん太郎です。評価に不満があって"


@pytest.fixture(autouse=True)
def c2_on(monkeypatch):
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c2")
    monkeypatch.delenv(H.FORM_ENV, raising=False)  # 形 C(既定)


def next_question(system: str) -> dict:
    return json.loads(SAY_JSON.search(system).group(1))


class MonotoneLLM:
    """読む段: READINGS の act の並び(無ければ、今の質問の欄への答え)。言う段: 本人の言葉 +「ですね。」+ 勧められた問いの言い方。"""

    READINGS: ClassVar[dict] = {"はい": [], "最近ジム通い始めた": [act("chitchat")]}

    def __init__(self):
        self.calls: list[tuple[str, str, list[dict]]] = []

    async def __call__(self, stage, system, messages):
        self.calls.append((stage, system, [dict(m) for m in messages]))
        said = messages[-1]["content"]
        if stage == "say":
            return {"reply": f"{said[:10]}ですね。" + next_question(system)["text"]}, USAGE
        targets = list(json.loads(re.search(r"この質問が埋めたい欄: (\{.*\})", system).group(1)))
        acts = self.READINGS.get(said)
        acts = [act("answer", slot=targets[0], text=said)] if acts is None else acts
        return SCHEMAS["read"](acts=acts, hook=None, mentions=[], ng=[]).model_dump(), USAGE


def test_line_webhook_c2_varies_acks_keeps_the_phase_line_order_and_reasks_in_other_words(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    llm = MonotoneLLM()
    model = ConsultModel(script=start_script(uid))
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=llm))
    st = server.state

    def say(text, n):
        before = len(llm.calls)
        assert _say(client, text, n).status_code == 200
        texts = [m["text"] for m in fake.sent()[-1][1]["messages"] if m["type"] == "text"]
        return texts[-1], [c[0] for c in llm.calls[before:]]

    with TestClient(server.app) as client:
        _say(client, "正社員で転職したいので相談したい", 1)
        assert current_qid(st, uid) == "q_segment"
        r1, _ = say("同じ仕事で上へ", 2)
        r2, _ = say("年収を上げたい", 3)
        r3, stages3 = say("東京23区", 4)
        assert current_qid(st, uid) == "hf_salary"
        r4, stages4 = say("はい", 5)
        r5, stages5 = say("最近ジム通い始めた", 6)
    salary = C2.phrasings(H.qmap(H.new_state(), H.BANK)["hf_salary"], H.BANK)
    assert r1 == "同じ仕事で上へですね。今回、仕事を探そうと思ったきっかけは?"
    # 区切りの行は、相づちの後、問いの前(#326 の D)
    assert r2.split("\n") == ["年収を上げたいですね。", H.PHASE_LINES["hard_filter"], "働きたい場所はどこですか?(駅や市の名前で)"]
    # 3 回目の「〜ですね。」は 2 回とも捨て、入り口の定義の決まった相づちで組む(#326 の ack_varies)
    assert stages3 == ["say", "say"] and r3 == C2.ENTRY.tone["acks"]["short"][0] + salary[0]
    # 「はい」: モデルの言う段を呼ばず(読む段だけ)、責めない注意と、違う言い方の問い
    assert stages4 == ["read"] and r4 == C2.ENTRY.texts["retry_number"] + "\n" + salary[1]
    # 雑談から戻る: また違う言い方(前の言い方を避ける)
    assert stages5 == ["read", "say"] and r5 == "最近ジム通い始めたですね。" + salary[2]
    assert not any("読み取れなかった" in r for r in (r1, r2, r3, r4, r5))
    h = session._load(st, uid)
    assert h.convo["acks"] == [None, "最近ジム通い始めたですね。"] and h.current["said"] == salary[2]


def test_line_webhook_c2_say_stage_history_is_the_masked_one(tmp_path, monkeypatch):
    """言う段のモデルに渡す直前のやりとりは、ハーネスが伏せて覚えたもの。前のターンで書いた氏名を、後のターンの言う段に渡さない
    (ランタイムの会話の記録には、本人の発言がそのまま残っている)。"""
    uid = line_user(RAW_USER)
    llm = MonotoneLLM()
    llm.READINGS = {NAMED_WHY: [act("name", span="みほん太郎"), act("answer", slot="why_now", text="評価に不満")]}
    model = ConsultModel(script=start_script(uid))
    server, _ = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=llm))
    with TestClient(server.app) as client:
        _say(client, "正社員で転職したいので相談したい", 1)
        _say(client, "同じ仕事で上へ", 2)
        _say(client, NAMED_WHY, 3)
        n = len(llm.calls)
        _say(client, "東京23区", 4)
    (stage, _, msgs), = llm.calls[n:]
    assert stage == "say" and msgs[-1]["content"] == "東京23区"
    said = [m["content"] for m in msgs]
    assert not any("みほん太郎" in m for m in said) and any("…です。評価に不満があって" in m for m in said)
    assert any("働きたい場所" in m for m in said)  # 前の返信も渡す


# ----------------------------- 言う段の入力 -----------------------------
def to_area(st):
    session.start(st, UID)
    run(st, "同じ仕事で上へ", lambda call: {"reply": next_question(call["system"])["text"]})


def test_say_prompt_carries_tone_phrasings_recent_acks_and_lines(tmp_path):
    st = State(tmp_path / "state.json")
    to_area(st)
    _, calls = run(st, "年収を上げたい", lambda call: {"reply": "年収ですね。" + next_question(call["system"])["text"]})
    system = calls[0]["system"]
    q = next_question(system)
    assert q["id"] == "hf_area" and q["text"] == q["phrasings"][0] == "働きたい場所はどこですか?(駅や市の名前で)"
    assert len(q["phrasings"]) == 3  # 新しい問いは、どの言い方でもよい(勧めるのは問いの文)
    assert C2.ENTRY.tone["style"] in system and "- 相づちの後に、システムが入れる行: 次は希望の条件です" in system
    assert "- 相づち: " + C2.ACK_JA["short"] in system and "{{" not in system
    _, calls = run(st, "大阪かな、雨の日は通勤つらいけど", lambda call: {
        "acts": [act("chitchat")], "hook": "雨の日は通勤つらい", "mentions": [], "ng": []} if call["stage"] == "read"
        else {"reply": "雨の日の通勤は大変ですよね。" + next_question(call["system"])["text"]})
    system = calls[1]["system"]
    q = next_question(system)
    assert q["text"] != "働きたい場所はどこですか?(駅や市の名前で)" and "働きたい場所はどこですか?(駅や市の名前で)" not in q["phrasings"]
    assert "- 直前の相づち(終わり方を変える): 年収ですね。" in system and "- 相づちに使ってよい本人の言葉: 雨の日は通勤つらい" in system
    assert "- 相づち: " + C2.ACK_JA["light"] in system


def test_reverse_question_is_answered_only_from_the_facts_or_with_the_fixed_text(tmp_path):
    """逆質問だけのターン: 相づちを付けず、渡した事実(facts.json。3 枚目のカード)だけで答えの部品を書かせる。答えを書かない返事は
    2 回とも落とし、決まった文(「お答えできません」)で答える(黙って流さない)。答えのある吹き出しは 120 字まで。"""
    st = State(tmp_path / "state.json")
    to_area(st)
    run(st, "年収を上げたい", lambda call: {"reply": next_question(call["system"])["text"]})
    reading = {"acts": [act("ask_advice")], "hook": None, "mentions": [], "ng": []}
    fact = "求人はヒアリングの後に、条件に合うものを出せます。"
    r, calls = run(st, "営業職って今どれくらい求人ありますか", lambda call: reading if call["stage"] == "read" else {
        "reply": fact + next_question(call["system"])["text"]})
    assert [c["stage"] for c in calls] == ["read", "say"] and r["counted"] is False
    assert "- 逆質問への答え: 答える。" in calls[1]["system"] and fact[:-1] in calls[1]["system"]
    assert r["parts"][0] == ["answer", fact] and [k for k, _ in r["parts"]] == ["answer", "ask"]
    r, calls = run(st, "営業職って今どれくらい求人ありますか", lambda call: reading if call["stage"] == "read" else {
        "reply": next_question(call["system"])["text"]})
    assert [c["stage"] for c in calls] == ["read", "say", "say"]  # 答えの無い返事: やり直して、決まった文
    assert r["parts"][0] == ["answer", C2.ENTRY.texts["no_answer"]] and [k for k, _ in r["parts"]] == ["answer", "ask"]
    # 感情と一緒の逆質問: 相づちと答えをモデルが書く(1 文目が相づち)
    reading = {"acts": [act("ask_advice"), act("emotion", feeling="anxious")], "hook": None, "mentions": [], "ng": []}
    r, calls = run(st, "不安で。求人ってあります?", lambda call: reading if call["stage"] == "read" else {
        "reply": "不安なお気持ち、受け止めました。" + fact + next_question(call["system"])["text"]})
    assert [k for k, _ in r["parts"]] == ["ack", "answer", "note", "ask"] and r["parts"][1][1] == fact
    assert r["parts"][2][1] == C2.ENTRY.texts["note_pause"]  # 3 回続いた脱線: 「中断」の注意(3 枚目)は答えの後、問いの前
    assert RP.chars(r["reply"]) <= RP.ANSWER_BUBBLE_MAX


def test_reshown_question_after_the_crisis_text_has_no_retry_text_and_keeps_its_wording(tmp_path):
    """聞き直しの後に危ない言葉と「続ける」: 同じ問いを、モデルを呼ばずに、前と同じ言い方で出す(c1 の「読み取れなかったので…」を付けない)。"""
    st = State(tmp_path / "state.json")
    to_area(st)
    run(st, "年収を上げたい", lambda call: {"reply": next_question(call["system"])["text"]})
    run(st, "東京23区", lambda call: {"reply": next_question(call["system"])["text"]})
    r, _ = run(st, "高めがいいです", lambda call: {"acts": [act("answer", slot="min_annual_salary", text="高め")], "hook": None,
                                                    "mentions": [], "ng": []})
    shown = RP.ask_of(r["parts"])
    assert r["reply"].startswith(C2.ENTRY.texts["retry_number"]) and shown != "年収はいくら以上を考えていますか?"
    run(st, "もう死にたい", lambda call: pytest.fail("モデルを呼ばない"))
    r, calls = run(st, "続ける", lambda call: pytest.fail("モデルを呼ばない"))
    assert r["reply"] == C2.ENTRY.texts["crisis_resume"] + "\n" + shown and calls == []
    assert session._load(st, UID).current["said"] == shown


def test_clarify_and_more_do_not_call_the_model(tmp_path):
    st = State(tmp_path / "state.json")
    to_area(st)
    run(st, "年収を上げたい", lambda call: {"reply": next_question(call["system"])["text"]})
    r, calls = run(st, "駅の名前じゃないとだめ?", lambda call: {"acts": [act("clarify")], "hook": None, "mentions": [], "ng": []})
    assert [c["stage"] for c in calls] == ["read"] and r["parts"] == [["ask", H.qmap(H.new_state(), H.BANK)["hf_area"]["explain"]]]
    run(st, "東京23区", lambda call: {"reply": next_question(call["system"])["text"]})
    r, calls = run(st, H.MORE, lambda call: pytest.fail("モデルを呼ばない"))
    assert r["reply"] == "年収のほかの区切りです。" and calls == []  # #326 の A。入り口の定義の文から作る


# ----------------------------- 入り口の定義: 言い方と調子 -----------------------------
def test_every_question_has_two_or_three_phrasings_that_fit_the_bubble():
    """どの問いも言い方が 2〜3 通り。区切りの行と相づち(ACK_MIN 字)を足しても 80 字に入る。問いの印は 1 つまで。"""
    for form in H.FORMS:
        for seg in H.SEGMENTS:
            s = H.new_state(form)
            s.segment = seg
            for q in H.questions(s, H.BANK):
                ps = C2.phrasings(q, H.BANK)
                known = q["text"] != next(b for b in H.BANK if q["id"].startswith(b["id"]))["text"].replace(
                    "{which}", "直近の" if q.get("card") == 1 else "その前の")
                assert len(ps) == (1 if known else 3) and len(set(ps)) == len(ps), q["id"]
                for p in ps:
                    assert "{" not in p and len(RP.QMARK_RE.findall(p)) <= 1, p
                    assert RP.chars(p) + max(map(RP.chars, H.PHASE_LINES.values())) + RP.ACK_MIN <= RP.BUBBLE_MAX, p


def test_fallback_acks_of_the_entry_are_polite_and_end_differently():
    acks = C2.ENTRY.tone["acks"]
    assert set(acks) == set(RP.ACK_KINDS)
    for kind, xs in acks.items():
        assert len(xs) == 3 and len({RP.tail(x) for x in xs}) == 3, kind  # 順に回せば、終わりが 3 回続かない
        assert all(x.endswith("。") and "!" not in x for x in xs), kind


# ----------------------------- 場面の一覧(evals/scenes/) -----------------------------
# 4 枚目の場面のうち、ほかのカードの仕事だった条件(3 回目の脱線に「中断」を添える 3 枚目、二択の「どっちでも」をこだわりなしにする
# 5 枚目)は、3 枚目と 5 枚目(task 356、358)を取り込んで合格するようになった


def card4_scene(s: dict) -> bool:
    sid = s["id"]
    return s["entry"] == "career" and (sid.startswith(("t326-", "short-")) or sid in {
        "correct-vague-why", "chat-three-in-a-row", "emotion-angry-reask", "emotion-long-story"})


def test_c2_passes_the_code_checks_of_the_card_4_scenes():
    from evals import scenes as S
    from evals.scenes import checks as K
    from evals.scenes import runner as R

    ids = [s["id"] for s in S.load_scenes() if card4_scene(s)]
    assert len(ids) == 16
    rows = {r["scene"]: r for r in R.run(["career"], {"career": "c2"}, ids)}
    bad = {(sid, x["check"]) for sid, r in rows.items() for x in r["results"] if not x.get("judge") and x["status"] != K.PASS}
    assert not bad, bad
    # 1 枚目の点で c1 が落ちた条件(相づちの終わりが 3 回続く、「はい」への「読み取れなかった」)は、c2 では通る
    c1 = {r["scene"]: r for r in R.run(["career"], {"career": "c1"}, ["t326-salary-reask", "short-hai"])}
    assert {(sid, x["check"]) for sid, r in c1.items() for x in r["results"] if x["status"] == K.FAIL} == {
        ("t326-salary-reask", "ack_varies"), ("short-hai", "reply_not_contains")}
    order = rows["t326-order"]["replies"][0]
    assert [k for k, _ in order["parts"]] == ["ack", "phase_line", "ask"] and order["llm_calls"] == 1
