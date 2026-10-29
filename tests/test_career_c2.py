"""正社員ヒアリングの c2(会話の層。dip_mcp/career/c2.py、設計メモ docs/design/conversation-layer.md §5 の 2 枚目)。

実 LLM なし。本人から見た流れを、LINE の Webhook と本物のランタイムと dip の MCP で通す(偽の LINE、偽のモデル)。
- 1 回の発言の複数の値: 勤務地の問いに「東京で500万以上、リモートがいい」→ 勤務地は「東京」だけ。年収とリモートは推測で入り、
  年収の問いを聞かない。まとめの「これでOK」で確かめた値として保存する
- 危ない言葉: モデルを呼ばずに相談窓口の文(丁寧語)。「続ける」で同じ問いに戻る
- 読む段には、今の発言だけをメッセージで渡し、直前のやりとりはシステムプロンプトに入れる
- LINE のスタンプ: c2 の相談の間は「[スタンプ]」の文として受け、読む段を呼ばずに同じ問いを出し直す(c1 の相談の間は今までどおり答えない)
ほかに、始めた相談は始めた版のまま進むこと、c1 は変わらないこと、返事に氏名が出たらやり直すことを、相談のツールで確かめる。
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
from dip_mcp.convo import guard
from dip_mcp.data import State
from line_runtime import baito_chat
from line_runtime.career_chat import SCHEMAS, ReadOut
from line_runtime.line_channel import line_user
from tests.test_career_chat import USAGE, answer_for, current_qid, start_script
from tests.test_consult import ConsultModel
from tests.test_line_data import _say
from tests.test_line_webhook import RAW_USER, _event, _post, _server, line_id_key  # noqa: F401  line_id_key はフィクスチャ

MULTI = "東京で500万以上、リモートがいい"
CRISIS = "仕事つらすぎてもう死にたい"
NAMED = "みほん太郎です。物流会社で現場リーダー"
TEXT = {"ib_why_now": "評価の仕組みが合わないから", "hf_area": MULTI, "card_role1": NAMED,
        "card_result1": "誤出荷が半分になった", "card_role2": "倉庫の作業スタッフ", "card_result2": "手順書を作った",
        "fg_direction": "現場の改善"}
NULLS = dict.fromkeys(("slot", "text", "number", "unit", "choice", "value", "to", "strength", "feeling", "span"))


def act(name, **kw):
    return NULLS | {"act": name} | kw


@pytest.fixture(autouse=True)
def c2_on(monkeypatch):
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c2")
    monkeypatch.delenv(H.FORM_ENV, raising=False)  # 形 C(既定)


class FakeReadLLM:
    """読む段: 今の質問の欄に、本人の発言を答えとして返す。READINGS にある発言は、その act の並び。言う段は質問の原文。
    出力は本物と同じ型(ReadOut)を通す。"""

    READINGS: ClassVar[dict] = {MULTI: [act("answer", slot="area", text="東京"),
                        act("add", slot="min_annual_salary", text="500万以上", number=500, unit="万"),
                        act("add", slot="work_style", text="リモートがいい")],
                NAMED: [act("name", span="みほん太郎"), act("answer", slot="career1.role", text="物流会社で現場リーダー")]}

    def __init__(self):
        self.calls: list[tuple[str, str, list[dict]]] = []

    async def __call__(self, stage, system, messages):
        self.calls.append((stage, system, [dict(m) for m in messages]))
        if stage == "say":
            q = json.loads(re.search(r"次に聞くこと\(JSON\): (\{.*\})", system).group(1))
            return {"reply": q["text"]}, USAGE
        assert stage == "read"
        targets = list(json.loads(re.search(r"この質問が埋めたい欄: (\{.*\})", system).group(1)))
        said = re.search(r"本人の今の発言: (.*)", system).group(1).strip()
        acts = self.READINGS.get(said) or [act("answer", slot=targets[0], text=said)]
        return SCHEMAS["read"](acts=acts, hook=None, mentions=[], ng=[]).model_dump(), USAGE


def test_line_webhook_c2_reads_several_values_guards_the_crisis_and_saves_only_each_slots_part(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    llm = FakeReadLLM()
    model = ConsultModel(script=start_script(uid))
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=llm))
    st = server.state
    asked, crisis_done = [], False
    with TestClient(server.app) as client:
        assert _say(client, "正社員で転職したいので相談したい", 1).status_code == 200
        assert st.user(uid)[session.KEY]["harness"]["version"] == "c2"
        n = 2
        while (qid := current_qid(st, uid)) is not None and n < 40:
            asked.append(qid)
            if qid == "hf_timing" and not crisis_done:  # 途中で危ない言葉を書く → 窓口の文。モデルを呼ばない
                before = len(llm.calls)
                _say(client, CRISIS, n)
                texts = "\n".join(m["text"] for m in fake.sent()[-1][1]["messages"] if m["type"] == "text")
                assert all(x in texts for x in guard.CRISIS["contacts"]) and "まもろうよ こころ" in texts
                assert "話してくださって" in texts and len(llm.calls) == before
                assert current_qid(st, uid) == "hf_timing"
                _say(client, "続ける", n + 1)
                texts = "\n".join(m["text"] for m in fake.sent()[-1][1]["messages"] if m["type"] == "text")
                assert texts.startswith(C2.ENTRY.texts["crisis_resume"]) and len(llm.calls) == before
                crisis_done, n = True, n + 2
                continue
            last = fake.sent()[-1][1]["messages"][-1]
            quick = [i["action"]["text"] for i in last.get("quickReply", {}).get("items", [])]
            said = TEXT.get(qid) or answer_for(qid, quick)
            _say(client, said, n)
            n += 1
            if qid == "hf_area":  # 1 回の発言の 3 つの値: 勤務地は「東京」だけ、ほかは推測
                slots = st.user(uid)[session.KEY]["harness"]["slots"]
                assert (slots["area"]["text"], slots["area"]["confidence"]) == ("東京", "high")
                assert (slots["min_annual_salary"]["number"], slots["min_annual_salary"]["confidence"]) == (500, "low")
                assert slots["work_style"]["text"] == "リモートがいい"
        assert crisis_done and current_qid(st, uid) is None
        texts = [m["text"] for m in fake.sent()[-1][1]["messages"] if m["type"] == "text"]
        assert texts[-1].startswith(H.RESULT_TEXT)
    assert "hf_salary" not in asked and "fg_work_style" not in asked  # 言った値を聞き直さない
    prefs = logic.stated_preferences(State(tmp_path / "state.json").user(uid))
    assert prefs["area"]["value"] == "東京"  # 文の全部でなく、勤務地の部分だけ
    assert prefs["min_annual_salary"]["value"] == "500万以上"  # まとめの「これでOK」で確かめた
    # 読む段: メッセージは今の発言だけ。直前のやりとり(前の問い)はシステムプロンプトに入る
    reads = [(system, msgs) for stage, system, msgs in llm.calls if stage == "read"]
    system, msgs = next((s, m) for s, m in reads if m[-1]["content"] == MULTI)
    assert msgs == [{"role": "user", "content": MULTI}]
    recent = system.split("直前のやりとり(古い順):", 1)[1].split("- 本人の今の発言", 1)[0]
    assert "本人: " + TEXT["ib_why_now"] in recent and "働きたい場所" in recent
    # 氏名を書いたターン: 言う段のモデルに渡す今の発言は伏せる。値にも入れない
    i = next(i for i, (stage, _, msgs) in enumerate(llm.calls) if stage == "read" and msgs[-1]["content"] == NAMED)
    stage, system, msgs = llm.calls[i + 1]
    assert stage == "say" and msgs[-1]["content"].endswith("…です。物流会社で現場リーダー") and "みほん太郎" not in system
    assert State(tmp_path / "state.json").user(uid)["career_profile"]["items"]["career1.role"]["text"] == "物流会社で現場リーダー"


# ----------------------------- 相談のツール(dip_mcp.career.session)で -----------------------------
UID = "w-0000000000000c2a"


def run(st, text, script):
    """発言 1 つを、ランタイムと同じ呼び直しで進める。script(call) がモデルの出力を返す。"""
    outputs, calls = [], []
    r = session.turn(st, UID, text, None)
    while r.get("status") == "llm":
        calls.append(r["call"])
        outputs.append({"stage": r["call"]["stage"], "output": script(r["call"])})
        r = session.turn(st, UID, text, outputs)
    return r, calls


def say_question(call):
    q = json.loads(re.search(r"次に聞くこと\(JSON\): (\{.*\})", call["system"]).group(1))
    return {"reply": q["text"]}


def test_started_consultation_keeps_its_version_when_the_flag_changes(tmp_path, monkeypatch):
    st = State(tmp_path / "state.json")
    assert session.start(st, UID)["started"] is True
    assert session.harness_of(session._load(st, UID)) is C2
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    session.turn(st, UID, "同じ仕事で上へ", None)  # ボタン
    assert session._load(st, UID).version == "c2"  # 始めた版のまま
    session._drop(st, UID)
    session.start(st, UID)
    assert session._load(st, UID).version == "c1"
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c9")
    assert logic.career_harness_version() is None and not logic.career_harness_on()


def test_crisis_with_a_stop_word_is_answered_not_closed(tmp_path):
    """c2 は、やめる頼みと一緒に書かれた危ない言葉でも閉じずに窓口の文を返す(c1 は閉じてトークの AI に渡す)。"""
    st = State(tmp_path / "state.json")
    session.start(st, UID)
    r, calls = run(st, "もう消えたい。やめる", lambda call: pytest.fail("モデルを呼ばない"))
    assert r["active"] is True and r["reply"] == C2.CRISIS_TEXT and r["choices"] == [] and calls == []
    assert r["counted"] is False and session.active(st, UID)


def test_reply_that_echoes_a_name_is_redone_and_falls_back_to_the_question(tmp_path):
    st = State(tmp_path / "state.json")
    session.start(st, UID)
    run(st, "同じ仕事で上へ", say_question)
    said = "みほん太郎です。評価に不満があって"

    def script(call):
        if call["stage"] == "read":
            return {"acts": [act("name", span="みほん太郎"), act("answer", slot="why_now", text="評価に不満")], "hook": None,
                    "mentions": [], "ng": []}
        return {"reply": "みほん太郎さん、" + say_question(call)["reply"]}
    r, calls = run(st, said, script)
    assert [c["stage"] for c in calls] == ["read", "say", "say"]  # 名前をなぞった返事は 2 回とも捨てる
    assert "みほん太郎" not in r["reply"] and r["status"] == "reply"
    h = session._load(st, UID)
    assert h.slots["why_now"]["text"] == "評価に不満"
    assert h.convo["recent"][-2] == {"who": "user", "text": "…です。評価に不満があって"}  # 覚える発言も伏せる
    assert any(e["kind"] == "generate_fallback" and "personal_echo" in e["problems"] for e in h.log)


def test_read_stage_output_is_checked_against_what_was_said(tmp_path):
    """読む段が発言に無い字や数を返したら、その値は入れない(数を作らない)。働きの読めない出力は 1 回やり直す。"""
    st = State(tmp_path / "state.json")
    session.start(st, UID)
    run(st, "同じ仕事で上へ", say_question)
    run(st, "年収を上げたい", say_question)  # ボタン。次は勤務地
    outs = iter([{"bad": 1}, {"acts": [act("answer", slot="area", text="横浜"),
                                        act("add", slot="min_annual_salary", text="たくさん", number=800)],
                              "hook": None, "mentions": [], "ng": []}])
    _, calls = run(st, "たくさん欲しい。大阪で", lambda call: next(outs) if call["stage"] == "read" else say_question(call))
    h = session._load(st, UID)
    assert "area" not in h.slots  # 「横浜」は発言に無い
    assert h.slots["min_annual_salary"]["number"] is None  # 800 は発言に無い
    assert [c["stage"] for c in calls] == ["read", "read", "say"]


def test_c1_still_reads_with_the_old_extract_stage(tmp_path, monkeypatch):
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    st = State(tmp_path / "state.json")
    session.start(st, UID)
    run(st, "同じ仕事で上へ", say_question)
    _, calls = run(st, "評価に不満", lambda call: {"slot_updates": [], "ng_updates": [], "segment": None, "intent": "aside",
                                                   "review_ack": None} if call["stage"] == "extract" else say_question(call))
    assert [c["stage"] for c in calls] == ["extract", "say"]
    assert "convo" not in st.user(UID)[session.KEY]["harness"]


def test_read_stage_messages_carry_only_the_current_utterance():
    convo = [{"role": "user", "content": "前の発言"}, {"role": "assistant", "content": "前の返信"}]
    assert baito_chat.messages_for("read", "今の発言", convo) == [{"role": "user", "content": "今の発言"}]
    assert baito_chat.messages_for("say", "今の発言", convo)[-1] == {"role": "user", "content": "今の発言"}
    assert len(baito_chat.messages_for("say", "今の発言", convo)) == 3


def test_read_out_type_names_every_act():
    from typing import get_args

    from dip_mcp.convo import acts as A

    names = get_args(ReadOut.model_fields["acts"].annotation.__args__[0].model_fields["act"].annotation)
    assert set(names) == set(A.ACTS)


# ----------------------------- 場面の一覧(evals/scenes/) -----------------------------
CARD2_SAFETY = {"safety-crisis", "safety-health", "safety-pii-redacted", "safety-full-name", "safety-third-party-name", "chat-grumble"}


def card2_scene(s: dict) -> bool:
    """2 枚目のカードで確かめる場面(設計メモ §5): career の multi-*、correct-*(go-back を除く)と、守りの場面と、愚痴。"""
    sid = s["id"]
    return s["entry"] == "career" and (sid.startswith("multi-") or (sid.startswith("correct-") and sid != "correct-go-back")
                                       or sid in CARD2_SAFETY)


def test_c2_passes_every_code_check_of_the_card_2_scenes():
    from evals import scenes as S
    from evals.scenes import checks as K
    from evals.scenes import runner as R

    ids = [s["id"] for s in S.load_scenes() if card2_scene(s)]
    assert len(ids) == 19
    ids.append("emotion-long-story")  # ほかの欄の言葉が混じらない条件を足した場面(jyoka/tasks#354 の Decision)
    rows = {r["scene"]: r for r in R.run(["career"], {"career": "c2"}, ids)}
    bad = {sid: [x for x in r["results"] if not x.get("judge") and x["status"] != K.PASS] for sid, r in rows.items()}
    assert {sid: b for sid, b in bad.items() if b} == {}
    crisis = rows["safety-crisis"]["replies"][0]
    assert crisis["llm_calls"] == 0 and crisis["text"] == C2.CRISIS_TEXT
    # c1 は今の問いの欄に本人の文の全部を入れるので、ほかの欄の言葉が混じる(jyoka/tasks#354 の Decision で足した条件)
    c1 = {r["scene"]: r for r in R.run(["career"], {"career": "c1"}, ["multi-area-salary-remote", "emotion-long-story"])}
    for sid, slot in (("multi-area-salary-remote", "area"), ("emotion-long-story", "why_now")):
        got = [x for x in c1[sid]["results"] if x["check"] == "slot" and "not_contains" in x["args"]]
        assert [x["status"] for x in got] == [K.FAIL] and got[0]["args"]["slot"] == slot


def reading_of(*acts):
    return lambda call: {"acts": list(acts), "hook": None, "mentions": [], "ng": []} if call["stage"] == "read" \
        else say_question(call)


def to_salary(st):
    session.start(st, UID)
    run(st, "同じ仕事で上へ", say_question)
    run(st, "年収を上げたい", say_question)
    run(st, "東京23区", say_question)
    assert current_qid(st, UID) == "hf_salary"


def test_salary_without_a_number_is_asked_again_with_buttons_once_then_kept_as_a_guess(tmp_path):
    """数の問いに数の無い答え: 1 回目は数えずにボタンで聞き直し(c1 と同じ)、2 回目は本人の言葉を推測で入れて進む。"""
    st = State(tmp_path / "state.json")
    to_salary(st)
    r, _ = run(st, "高めがいいです", reading_of(act("answer", slot="min_annual_salary", text="高め")))
    h = session._load(st, UID)
    assert h.current["mode"] == "retry" and r["counted"] is False and "min_annual_salary" not in h.slots
    assert r["reply"].startswith(C2.ENTRY.texts["retry_number"]) and "読み取れなかった" not in r["reply"]  # 責めずに聞き直す
    r, _ = run(st, "高めがいいです", reading_of(act("answer", slot="min_annual_salary", text="高め")))
    h = session._load(st, UID)
    assert (h.slots["min_annual_salary"]["text"], h.slots["min_annual_salary"]["confidence"]) == ("高め", "low")
    assert r["counted"] is True and current_qid(st, UID) != "hf_salary"


def test_unreadable_answer_given_up_keeps_no_redacted_mark(tmp_path):
    st = State(tmp_path / "state.json")
    to_salary(st)
    for _ in range(2):
        run(st, "[個人情報を削除]", reading_of())
    h = session._load(st, UID)
    assert "min_annual_salary" not in h.slots and current_qid(st, UID) != "hf_salary"
    assert "[個人情報を削除]" not in json.dumps(h.slots, ensure_ascii=False)


def test_say_stage_gets_the_masked_utterance_for_the_model(tmp_path):
    """言う段の呼び出しには、氏名と要配慮の部分を伏せた発言(model_text)が入る。ランタイムはそれを本人の発言としてモデルに渡す。"""
    st = State(tmp_path / "state.json")
    to_salary(st)
    _, calls = run(st, "うつで休んでたので500万以上で", reading_of(
        act("sensitive", span="うつで休んでた"), act("answer", slot="min_annual_salary", text="500万以上", number=500)))
    say = next(c for c in calls if c["stage"] == "say")
    assert say["model_text"] == "…ので500万以上で" and "うつ" not in say["system"]
    assert "model_text" not in calls[0]  # 読む段は、本人の発言のまま読む


def test_crisis_turn_in_form_d_gets_no_flex(tmp_path, monkeypatch):
    monkeypatch.setenv(H.FORM_ENV, "D")
    st = State(tmp_path / "state.json")
    session.start(st, UID)
    run(st, H.START_GO, say_question)
    run(st, "同じ仕事で上へ", say_question)
    r, calls = run(st, "もう死にたい", say_question)
    assert r["reply"] == C2.CRISIS_TEXT and "ui" not in r and calls == []


# ----------------------------- 3 枚目: 応じ方の計画(§4.6) -----------------------------
def sticker(n: int) -> dict:
    e = _event("message", event_id=f"01S{n}", token=f"reply-token-s{n}")
    e["message"] = {"type": "sticker", "id": f"9{n}", "packageId": "446", "stickerId": "1988"}
    return e


@pytest.mark.parametrize("version", ["c2", "c1"])
def test_line_sticker_is_a_message_only_during_a_c2_consultation(tmp_path, monkeypatch, version):
    """c2 の相談の間のスタンプは「[スタンプ]」の文: 読む段を呼ばず、数えずに同じ問いとボタンを返す。c1 の相談の間は返事をしない。"""
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, version)
    uid = line_user(RAW_USER)
    llm = FakeReadLLM()
    model = ConsultModel(script=start_script(uid))
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=llm))
    st = server.state
    with TestClient(server.app) as client:
        assert _post(client, [sticker(0)]).status_code == 200
        assert fake.sent() == []  # 相談の外(トーク)のスタンプには、今までどおり返事をしない
        _say(client, "正社員で転職したいので相談したい", 1)
        h = session._load(st, uid)
        assert h.version == version
        before, sent, turns = len(llm.calls), len(fake.sent()), h.turns
        assert _post(client, [sticker(2)]).status_code == 200
        if version == "c1":
            assert len(fake.sent()) == sent and len(llm.calls) == before
            return
        last = fake.sent()[-1][1]["messages"][-1]
        quick = [i["action"]["text"] for i in last.get("quickReply", {}).get("items", [])]
    h = session._load(State(tmp_path / "state.json"), uid)
    q = H.qmap(h, H.BANK)[h.current["qid"]]
    assert any(x in last["text"] for x in C2.phrasings(q, H.BANK)) and quick == h.current["choices"] and h.turns == turns
    assert [stage for stage, _, _ in llm.calls[before:]] == ["say"]  # 読む段を呼ばない
    assert h.convo["recent"][-2] == {"who": "user", "text": "[スタンプ]"}


def say_with(text):
    """言う段の偽のモデル: 決まった前置き(text)+ 問いの文。"""
    return lambda call: {"reply": text + say_question(call)["reply"]}


def scripted(acts_by_text, say=say_question):
    def script(call):
        if call["stage"] == "read":
            said = re.search(r"本人の今の発言: (.*)", call["system"]).group(1).strip()
            return {"acts": acts_by_text[said], "hook": None, "mentions": [], "ng": []}
        return say(call)
    return script


def plan_of(call):
    return json.loads(re.search(r"計画\(JSON\): (\{.*\})", call["system"]).group(1))


def test_service_question_gets_only_the_facts_and_a_longer_bubble(tmp_path):
    """「これ何に使うの?」: 言う段に事実(aica 全体と入り口)だけを渡し、相づちなしで答えさせる。吹き出しは 120 字まで。
    事実に無い数は落とし、事実の数(180日)は言ってよい。数えない。"""
    st = State(tmp_path / "state.json")
    to_salary(st)
    long_answer = "集めた内容は、職務経歴書の下書きと、条件に合う求人を探すのに使います。保存した内容は、最後に使った日から180日で消えます。LINE では「データを消す」で消せます。"
    r, calls = run(st, "これ何に使うの?", scripted({"これ何に使うの?": [act("ask_service")]}, say_with(long_answer)))
    plan = plan_of(calls[1])
    assert plan["ack"] is None and plan["answer"] is True
    assert plan["facts"][0] == "いまは希望の条件を聞いています" and "求人は架空のデモで、実際の応募にはなりません" in plan["facts"]
    assert all(f in C2.ENTRY.facts["ask_service"] or f in C2.ENTRY.facts["where"].values() for f in plan["facts"])
    assert r["reply"].startswith(long_answer) and 80 < H.chars(r["reply"]) <= C2.ANSWER_BUBBLE_MAX
    assert r["counted"] is False and current_qid(st, UID) == "hf_salary"
    r, calls = run(st, "これ何に使うの?", scripted({"これ何に使うの?": [act("ask_service")]}, say_with("相場は450万です。")))
    assert [c["stage"] for c in calls] == ["read", "say", "say"] and "450" not in r["reply"]  # 事実に無い数: 問いの文だけ
    h = session._load(st, UID)
    assert any(e["kind"] == "generate_fallback" and "made_up_number" in e["problems"] for e in h.log)


def test_three_offtrack_turns_add_the_pause_note_once(tmp_path):
    st = State(tmp_path / "state.json")
    to_salary(st)
    chat = scripted({"雨だね": [act("chitchat")], "500万以上": [act("answer", slot="min_annual_salary", text="500万以上",
                                                                    number=500)]})
    note = C2.ENTRY.texts["note_pause"]
    replies = [run(st, "雨だね", chat)[0]["reply"] for _ in range(4)]
    assert [note in x for x in replies] == [False, False, True, False]  # 3 回目に 1 回だけ
    lines = replies[2].split("\n")
    assert lines.index(note) == len(lines) - 2  # 注意は問いの前(相づちの後、区切りの前)
    h = session._load(st, UID)
    assert h.offtrack == 4 and h.turns == 3 + 1  # to_salary の 3 ターンと、4 回目の脱線(MAX_ASIDES = 3 を超えた分は数える)
    run(st, "500万以上", chat)
    assert session._load(st, UID).offtrack == 0  # 答えて進んだら続きは 0 から


def test_tired_adds_the_pause_note_and_asks_no_more_than_hard(tmp_path):
    """「疲れた…」: 同じ問いに「中断」の注意を添え(数えない)、この後は HARD のほかの問いを聞かずにまとめへ進む。"""
    st = State(tmp_path / "state.json")
    session.start(st, UID)
    for said in ("同じ仕事で上へ", "年収を上げたい", "東京23区", "600万円以上", "すぐ"):  # HARD を全部ボタンで答える
        run(st, said, say_question)
    assert current_qid(st, UID) == "card_role1"
    r, calls = run(st, "疲れた…", scripted({"疲れた…": [act("emotion", feeling="tired")]}))
    assert C2.ENTRY.texts["note_pause"] in r["reply"] and r["counted"] is False and current_qid(st, UID) == "card_role1"
    assert plan_of(calls[1])["ack"] == "empathy" and H.HURRY in session._load(st, UID).flags
    run(st, "営業", say_question)  # 今の問いには答える。次は HARD のほかを聞かない
    h = session._load(st, UID)
    assert h.current["qid"] == "review" and h.stop_reason == "hurry"


def test_go_back_asks_the_previous_question_to_fix_then_returns(tmp_path):
    st = State(tmp_path / "state.json")
    to_salary(st)
    back = scripted({"前の質問に戻りたい": [act("go_back")], "やっぱり横浜": [act("answer", slot="area", text="横浜")]})
    r, calls = run(st, "前の質問に戻りたい", back)
    h = session._load(st, UID)
    assert (h.current["qid"], h.current["mode"], h.current["resume"]) == ("hf_area", "back", "hf_salary")
    assert r["counted"] is False and H.FIX_KEEP in r["choices"] and "東京23区" in r["choices"]
    assert plan_of(calls[1])["about"] == ["勤務地の質問に戻ります"]
    r, calls = run(st, "やっぱり横浜", back)
    h = session._load(st, UID)
    assert (h.slots["area"]["text"], h.slots["area"]["confidence"]) == ("横浜", "high")
    assert h.current["qid"] == "hf_salary" and r["counted"] is False and h.turns == 3
    assert plan_of(calls[1])["about"] == ["勤務地を直しました"]
    run(st, "前の質問に戻りたい", back)
    r, _ = run(st, H.FIX_KEEP, say_question)  # 「そのまま」: 変えずに戻る
    h = session._load(st, UID)
    assert h.slots["area"]["text"] == "横浜" and h.current["qid"] == "hf_salary"


def test_complaint_alone_closes_the_slot_and_moves_on(tmp_path):
    st = State(tmp_path / "state.json")
    to_salary(st)
    r, calls = run(st, "同じこと何回聞くの?", scripted({"同じこと何回聞くの?": [act("complaint")]}))
    h = session._load(st, UID)
    assert r["counted"] is True and "min_annual_salary" not in h.slots and h.current["qid"] != "hf_salary"
    assert plan_of(calls[1])["ack"] == "apology"


def test_falsify_is_refused_with_the_fixed_note_and_not_counted(tmp_path):
    st = State(tmp_path / "state.json")
    to_salary(st)
    r, calls = run(st, "盛って書いといて", scripted({"盛って書いといて": [act("falsify")]}))
    h = session._load(st, UID)
    assert C2.ENTRY.texts["note_falsify"] in r["reply"] and r["counted"] is False and h.aside_count == 0
    assert [c["stage"] for c in calls] == ["read"] and current_qid(st, UID) == "hf_salary"  # 相づちなし: 言う段を呼ばない(4 枚目)


def test_c1_say_prompt_has_no_plan(tmp_path, monkeypatch):
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    st = State(tmp_path / "state.json")
    session.start(st, UID)
    _, calls = run(st, "同じ仕事で上へ", say_question)
    assert "計画(JSON)" not in calls[0]["system"]


CARD3_ONE = {"safety-falsify", "safety-harassment", "safety-refuse-salary", "correct-go-back"}


def card3_scene(s: dict) -> bool:
    """3 枚目のカードで確かめる場面(設計メモ §5): career の chat-*、ask-*、emotion-*、flow-* と、守りの 3 つ、前の問いに戻る。"""
    sid = s["id"]
    return s["entry"] == "career" and (sid.split("-")[0] in ("chat", "ask", "emotion", "flow") or sid in CARD3_ONE)


def test_c2_passes_every_code_check_of_the_card_3_scenes():
    from evals import scenes as S
    from evals.scenes import checks as K
    from evals.scenes import runner as R

    ids = [s["id"] for s in S.load_scenes() if card3_scene(s)]
    assert len(ids) == 35
    rows = R.run(["career"], {"career": "c2"}, ids)
    bad = {(r["scene"], x["check"]): x["detail"] for r in rows for x in r["results"]
           if not x.get("judge") and x["status"] != K.PASS}
    assert not bad, bad  # chat-three-in-a-row の ack_varies(相づちの言い回し)は 4 枚目(task 357)を取り込んで合格する
    c1 = {(r["scene"], x["check"]) for r in R.run(["career"], {"career": "c1"}, ids) for x in r["results"]
          if not x.get("judge") and x["status"] != K.PASS}
    assert {("chat-sticker", "keeps_question"), ("correct-go-back", "asks"), ("emotion-tired", "reply_contains")} <= c1
