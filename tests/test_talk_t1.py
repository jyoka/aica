"""トークの AI の t1(TALK_LAYER_VERSION=t1。設計メモ docs/design/conversation-layer.md §4.13、§5 の 8 枚目。task 361)。

実 LLM なし。本物のランタイムと dip の MCP を、偽のトークの AI と偽のハーネスのモデルで動かす。
- 確かめる場面: talk の全部(6)。t1 でコードの条件がすべて合格する。t0(今の動き)の点は変わらない
- 守りの前処理: 相談の外の「もう死にたい」に、トークの AI を呼ばずに相談窓口の決まった文(LINE の Webhook から)。t0 は今までどおりモデルが受ける
- start_* への発言の受け渡し: トークの AI が text を渡すと、dip は始めずに引き継ぎの記録を書き、ランタイムが同じターンで移る先の
  switch_in を呼ぶ。移る先はその発言を読む段で読み、値を推測で入れて、聞かずに最初の問いへ。トークの AI が言い換えた文は読まない
- プロンプト: 事実と、入り口を選ぶボタン(入り口の定義の移る先の文)と、text の決まり。音声相談の指示: 事実と危ない言葉の決まった文
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from dip_mcp.baito import session as baito_session
from dip_mcp.career import harness as H
from dip_mcp.career import session
from dip_mcp.convo import facts as F
from dip_mcp.convo import guard, talk
from dip_mcp.convo import switch as SW
from dip_mcp.data import State
from line_runtime.line_channel import line_user
from tests.test_career_c2 import FakeReadLLM, act
from tests.test_consult import ConsultModel
from tests.test_handle import call, reply
from tests.test_line_data import _say
from tests.test_line_webhook import RAW_USER, _server, line_id_key  # noqa: F401  line_id_key はフィクスチャ

UID = "w-00000000000000t1"
CRISIS = "仕事見つからなくてもう死にたい"
WANT = "正社員で年収500万以上の仕事を探したい"
SALARY = [act("add", slot="min_annual_salary", text="年収500万以上", number=500, unit="万")]


@pytest.fixture(autouse=True)
def t1_on(monkeypatch):
    monkeypatch.setenv(logic.TALK_LAYER_FLAG, "t1")
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c2")
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v5")
    monkeypatch.delenv(H.FORM_ENV, raising=False)  # 形 C(既定)


def start_career(text: str | None, uid: str = UID) -> list:
    """トークの AI: 正社員に切り替え、playbook を読み、本人の発言を渡して相談を始める(並べて呼ぶ)。最後の返信は置き換わる。"""
    args = {"line_user_id": uid} | ({"text": text} if text is not None else {})
    msg = call("set_search_track", {"line_user_id": uid, "track": "正社員", "evidence": "正社員で探したい"}, "t0")
    msg.tool_calls = [*msg.tool_calls,
                      {"name": "get_counseling_playbook", "args": {"track": "正社員", "channel": "chat"}, "id": "pb", "type": "tool_call"},
                      {"name": "start_career_consultation", "args": args, "id": "st", "type": "tool_call"}]
    return [msg, reply(["正社員で探しますね。"], quick_replies=[])]


def handle(tmp_path, monkeypatch, model, says, career_llm=None, baito_llm=None):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    from line_runtime.runtime import LineRuntime, history

    async def go():
        async with LineRuntime(model=model, in_memory=True, career_llm=career_llm, baito_llm=baito_llm) as rt:
            outs = [await rt.handle(UID, "message", s) for s in says]
            values = (await rt.agent.aget_state(rt._cfg(UID, await rt._current(UID)))).values
            return outs, {"turns": history(values.get("messages", []), UID)}  # 画面に描き直す形

    return asyncio.run(go())


def read_texts(llm) -> list[str]:
    return [re.search(r"本人の今の発言: (.*)", system).group(1).strip() for stage, system, _ in llm.calls if stage == "read"]


# ----------------------------- 場面の一覧 -----------------------------
def test_talk_scenes_pass_every_code_check_on_t1_and_t0_keeps_its_score():
    from evals.scenes import checks as K
    from evals.scenes import runner as R

    rows = R.run(["talk"], {"talk": "t1"})
    assert len(rows) == 6
    for row in rows:
        code = [x for x in row["results"] if not x.get("judge")]
        assert code and all(x["status"] == K.PASS for x in code), (row["scene"], row["results"])
    t = R.tally(rows)["talk t1"]
    assert (t["scenes_all_pass"], t["scenes_judged"], t["pass"], t["fail"], t["skip_code"]) == (6, 6, 20, 0, 0)
    crisis = next(r for r in rows if r["scene"] == "talk-crisis")["replies"][0]
    assert crisis["how"] == "guard" and crisis["llm_calls"] == 0
    old = R.tally(R.run(["talk"], {"talk": "t0"}))["talk t0"]  # 今の動き(task 354 の点のまま)
    assert (old["scenes_all_pass"], old["scenes_judged"], old["pass"], old["fail"]) == (2, 3, 3, 2)


# ----------------------------- 守りの前処理 -----------------------------
def test_line_webhook_crisis_outside_a_consultation_gets_the_fixed_text_without_the_talk_ai(tmp_path, monkeypatch):
    """本番の入口(LINE の Webhook)で、相談の外の「もう死にたい」: トークの AI を呼ばずに、相談窓口の決まった文(丁寧語)を返す。
    相談は始めない。次の発言はトークの AI が受ける。"""
    uid = line_user(RAW_USER)
    model = ConsultModel(script=[reply(["ゆっくりで大丈夫です。"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _say(client, CRISIS, 1)
        texts = "\n".join(m["text"] for m in fake.sent()[-1][1]["messages"] if m["type"] == "text")
        assert len(model.script) == 1  # トークの AI を呼んでいない
        assert all(x in texts for x in guard.CRISIS["contacts"]) and "まもろうよ こころ" in texts
        assert "話してくださって" in texts and "いつでもここで続けられます" in texts
        assert not session.active(server.state, uid) and not baito_session.active(server.state, uid)
        _say(client, "ありがとう", 2)
        assert not model.script  # 次の発言はトークの AI


def test_crisis_turn_is_kept_in_the_conversation_and_the_next_turn_can_use_tools(tmp_path, monkeypatch):
    """会話の最初のターンが危ない言葉でも、会話にユーザーと入口を書く(次のターンのツールが、この人の状態を読める)。"""
    model = ConsultModel(script=[call("get_user_conversation_summary", {"line_user_id": "me"}), reply(["はい。"])])
    outs, convo = handle(tmp_path, monkeypatch, model, [CRISIS, "続きを話したい"])
    assert outs[0]["bubbles"] == [talk.CRISIS_TEXT] and outs[0]["guard"] == "crisis" and outs[0]["quick_replies"] == []
    summary = next(t for t in outs[1]["tools"] if t["name"] == "get_user_conversation_summary")["result"]
    assert "error" not in summary
    assert [t["user"] for t in convo["turns"]] == [CRISIS, "続きを話したい"]
    assert convo["turns"][0]["bot"]["bubbles"] == [talk.CRISIS_TEXT]


def test_t0_still_hands_the_crisis_to_the_talk_ai(tmp_path, monkeypatch):
    monkeypatch.delenv(logic.TALK_LAYER_FLAG)
    model = ConsultModel(script=[reply(["(今の動き: モデルが受ける)"])])
    outs, _ = handle(tmp_path, monkeypatch, model, [CRISIS])
    assert outs[0]["bubbles"] == ["(今の動き: モデルが受ける)"] and not model.script and "guard" not in outs[0]


# ----------------------------- start_* に本人の発言を渡す -----------------------------
def test_talk_ai_passes_the_words_and_c2_reads_them_in_the_same_turn(tmp_path, monkeypatch):
    """「正社員で年収500万以上の仕事を探したい」: トークの AI が start_career_consultation に発言を渡す。ランタイムが同じターンで
    c2 を始め、c2 は読む段で年収を読み、推測で入れて聞かずに区分の問いへ。返信は c2 の最初の問い(トークの AI の文は出ない)。
    次の発言は c2 が受ける。会話の記録では、このターンは本人の発言 1 つと c2 の返信。"""
    llm = FakeReadLLM()
    llm.READINGS = {WANT: SALARY}
    model = ConsultModel(script=start_career(WANT))
    outs, convo = handle(tmp_path, monkeypatch, model, [WANT, "同じ仕事で上へ"], career_llm=llm)
    st = State(tmp_path / "state.json")
    h = session._load(st, UID)
    salary = h.slots.get("min_annual_salary") or h.pending.get("min_annual_salary")
    assert salary["number"] == 500 and salary["confidence"] == "low" and salary["from"] == SW.TALK
    assert "正社員で探しますね。" not in outs[0]["bubbles"] and "今の状況" in "".join(outs[0]["bubbles"])
    assert outs[0]["track_switch"]["track"] == "正社員" and "career" in outs[0]
    assert [stage for stage, _, _ in llm.calls][:2] == ["read", "say"] and read_texts(llm)[0] == WANT
    assert not model.script and "career" in outs[1]  # 2 つ目の発言は c2(トークの AI を呼ばない)
    assert "hf_salary" not in h.asked and SW.KEY not in st.user(UID)
    assert [t["user"] for t in convo["turns"]] == [WANT, "同じ仕事で上へ"]
    assert convo["turns"][0]["bot"]["bubbles"] == outs[0]["bubbles"]


def test_words_the_person_did_not_write_are_not_read(tmp_path, monkeypatch):
    """トークの AI が発言を言い換えた(本人の発言に無い文を渡した)ら、このターンの本人の発言を読む(作った条件を値にしない)。"""
    llm = FakeReadLLM()
    llm.READINGS = {"正社員で探したい": [act("chitchat")], "正社員で年収900万以上": SALARY}
    model = ConsultModel(script=start_career("正社員で年収900万以上"))
    handle(tmp_path, monkeypatch, model, ["正社員で探したい"], career_llm=llm)
    assert read_texts(llm) == ["正社員で探したい"]
    h = session._load(State(tmp_path / "state.json"), UID)
    assert not (h.slots.get("min_annual_salary") or h.pending.get("min_annual_salary"))


def test_on_the_button_turn_the_earlier_words_are_read(tmp_path, monkeypatch):
    """区分を聞く 1 問のボタン(「正社員で探す」)を押したターンは、前に条件を書いた発言を渡して読ませる。"""
    first = "年収500万以上で探したい"
    llm = FakeReadLLM()
    llm.READINGS = {first: [act("add", slot="min_annual_salary", text="年収500万以上", number=500, unit="万")]}
    ask = reply(["どちらで探しますか?"], quick_replies=talk.entry_buttons())
    model = ConsultModel(script=[ask, *start_career(first)])
    outs, _ = handle(tmp_path, monkeypatch, model, [first, "正社員で探す"], career_llm=llm)
    assert outs[0]["quick_replies"] == ["アルバイト・パートで探す", "正社員で探す"]
    assert read_texts(llm) == [first]
    h = session._load(State(tmp_path / "state.json"), UID)
    assert (h.slots.get("min_annual_salary") or h.pending.get("min_annual_salary"))["number"] == 500


def test_start_without_words_or_t1_or_on_c1_and_v4_starts_as_before(tmp_path, monkeypatch):
    st = State(tmp_path / "state.json")
    out = session.start(st, UID)  # text なし: 今までどおりその場で始める
    assert out["started"] and out["question"]["id"] and SW.KEY not in st.user(UID)
    session._drop(st, UID)
    monkeypatch.delenv(logic.TALK_LAYER_FLAG)
    out = session.start(st, UID, text=WANT)  # t1 でなければ、c2 でも text を使わない(ほかの呼び手は今のまま)
    assert out["started"] and session._load(st, UID).version == "c2" and SW.KEY not in st.user(UID)
    session._drop(st, UID)
    monkeypatch.setenv(logic.TALK_LAYER_FLAG, "t1")
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    out = session.start(st, UID, text=WANT)  # c1 は text を使わない
    assert out["started"] and session._load(st, UID).version == "c1" and SW.KEY not in st.user(UID)
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")
    out = baito_session.start(st, "w-00000000000000b4", text="土日だけバイトしたい")
    assert out["started"] and baito_session._load(st, "w-00000000000000b4").version == "v4"


def test_c2_start_with_words_writes_an_unread_record_and_switch_in_reads_it_first(tmp_path):
    """dip の中: c2 の start(text)は始めずに、トークの AI からの記録(読んでいない)を書く。switch_in は読む段を先に呼び、
    読んだ値の act を記録に入れてから当てる(呼び直しの間は記録を消さない)。氏名は値にも言う段にも渡さない。"""
    st = State(tmp_path / "state.json")
    said = "みほん太郎です。正社員で年収500万以上の仕事を探したい"
    out = session.start(st, UID, text=said)
    assert out == {"started": False, "active": False, "switch_to": "career", "direct": True, "note": session.READ_NOTE}
    rec = st.user(UID)[SW.KEY]
    assert rec["from"] == SW.TALK and rec["read"] and rec["said"] == said and not session.active(st, UID)
    outputs, stages = [], []
    r = session.switch_in(st, UID, said, None)
    while r.get("status") == "llm":
        stages.append(r["call"]["stage"])
        if r["call"]["stage"] == "read":
            out = {"acts": [act("name", span="みほん太郎"), *SALARY], "hook": None, "mentions": [], "ng": []}
        else:
            assert "みほん太郎" not in r["call"]["model_text"]
            out = {"reply": "年収500万以上ですね。今の状況に近いのは?"}  # 引き継いだ値を相づちに入れる(task 379)
        assert SW.KEY in st.user(UID)
        outputs.append({"stage": r["call"]["stage"], "output": out})
        r = session.switch_in(st, UID, said, outputs)
    assert stages == ["read", "say"] and r["status"] == "reply" and r["switched"] == {"from": SW.TALK}
    h = session._load(st, UID)
    assert (h.slots.get("min_annual_salary") or h.pending.get("min_annual_salary"))["confidence"] == "low"
    assert SW.KEY not in st.user(UID) and logic.search_track(st.user(UID)) == "正社員"


def test_v5_start_with_words_reads_the_days_and_never_takes_the_age(tmp_path):
    st = State(tmp_path / "state.json")
    said = "16歳で土日だけバイトしたい"
    out = baito_session.start(st, UID, text=said)
    assert out["switch_to"] == "baito" and out["direct"] and not baito_session.active(st, UID)
    reading = {"acts": [act("add", slot="age_band", text="16歳", value="15-17"),
                        act("add", slot="days_per_week", text="土日だけ", value="1-2")], "hook": None, "mentions": [], "ng": []}
    outputs = []
    r = baito_session.switch_in(st, UID, said, None)
    while r.get("status") == "llm":
        stage = r["call"]["stage"]
        out = reading if stage == "read" else {"reply": json.loads(re.search(r"次に聞くこと\(JSON\): (\{.*\})",
                                                                             r["call"]["system"]).group(1))["text"]}
        outputs.append({"stage": stage, "output": out})
        r = baito_session.switch_in(st, UID, said, outputs)
    h = baito_session._load(st, UID)
    assert outputs[0]["stage"] == "read" and r["status"] == "reply"
    assert h.slots["days_per_week"]["value"] == "1-2" and h.slots["days_per_week"]["confidence"] == "low"
    assert not (h.slots.get("age_band") or {}).get("value")  # 年齢は v4 の決まりで、問いで聞く
    assert logic.search_track(st.user(UID)) == "アルバイト・パート"


def test_start_tools_show_the_words_argument_only_on_t1(monkeypatch):
    from line_runtime.runtime import START_TEXT_ARG, chat_tools

    def tools():
        return [SimpleNamespace(name=n, args_schema={"properties": {"line_user_id": {}, START_TEXT_ARG: {}}})
                for n in ("start_career_consultation", "start_baito_consultation", "save_user_preferences")]
    shown = {t.name: set(t.args_schema["properties"]) for t in chat_tools(tools())}
    assert all(START_TEXT_ARG in v for v in shown.values())
    monkeypatch.delenv(logic.TALK_LAYER_FLAG)
    hidden = {t.name: set(t.args_schema["properties"]) for t in chat_tools(tools())}
    assert START_TEXT_ARG not in hidden["start_career_consultation"] | hidden["start_baito_consultation"]
    assert START_TEXT_ARG in hidden["save_user_preferences"]  # 相談を始めるツールのほかは変えない


# ----------------------------- プロンプト -----------------------------
def test_prompt_adds_the_facts_the_entry_buttons_and_the_words_rule_only_on_t1(monkeypatch):
    from dip_mcp import server

    text = server.baitoru_assistant("A")
    assert "「アルバイト・パートで探す」「正社員で探す」" in text  # 入り口の定義の移る先の文
    assert all(x in text for x in F.COMMON["ask_service"] + F.COMMON["ask_advice"])
    assert "start_career_consultation の text" in text
    from evals.scenes.runner import ENTRY_BUTTONS_RE

    assert re.findall(r"「([^」]+)」", ENTRY_BUTTONS_RE.search(text).group(1)) == talk.entry_buttons()
    monkeypatch.delenv(logic.TALK_LAYER_FLAG)
    assert "会話の層の決まり" not in server.baitoru_assistant("A")


def test_prompt_starts_the_consultation_without_asking_conditions_and_names_only_the_tools_it_shows(monkeypatch):
    # task 377: 実モデルのトークの AI は、dip の Prompt の「条件だけなら相談に入らずエリアを聞く」に従い、相談を始めなかった。
    # t1 は、区分が決まって探したいと言ったら同じターンで相談を始める決まりを、その決まりより先にする
    from dip_mcp import server

    text = server.baitoru_assistant("A")
    assert "エリアや時間帯などの条件を聞かず、求人も探さずに、同じターンで相談を始める" in text
    assert "アルバイト・パートは start_baito_consultation、正社員は start_career_consultation" in text
    assert "より先にする" in text.split("### 相談を始めるとき")[1]
    # 正社員は、始めた発言の条件を先に保存しない(c2 が保存した値を確かめた値として入れ、発言の年収を読まないため)
    assert "start_career_consultation に text を渡すターンは、その発言の条件(年収・勤務地・働き方・時期)を save_user_preferences で保存しない" in text
    monkeypatch.delenv(logic.BAITO_HARNESS_FLAG)  # アルバイト・パートのハーネスが無ければ、そのツールは見せないので決まりにも書かない
    assert "(正社員は start_career_consultation)" in talk.start_rule()
    monkeypatch.delenv(logic.CAREER_HARNESS_FLAG)
    assert talk.start_rule() == ""


def test_entry_buttons_come_from_the_entry_definitions():
    from dip_mcp.baito import v5
    from dip_mcp.career import c2

    assert talk.entry_buttons() == [c2.ENTRY.switch[0]["label"], v5.ENTRY.switch[0]["label"]]
    assert talk.crisis_reply("ふつうの話") is None and talk.crisis_reply("消えたい") == talk.CRISIS_TEXT


# ----------------------------- 音声相談の指示 -----------------------------
def test_voice_instructions_get_the_facts_and_the_crisis_text_only_on_t1(monkeypatch):
    from voice_resume import prompts as VP

    on = VP.advisor_instructions("正社員", "(playbook)")
    assert VP.CRISIS_SPOKEN in on and all(x in on for x in VP.AICA_FACTS) and "死にたい" in on
    assert on.index("## 危ない言葉") < on.index("## 相談の playbook")
    monkeypatch.delenv(logic.TALK_LAYER_FLAG)
    off = VP.advisor_instructions("正社員", "(playbook)")
    assert "## 危ない言葉" not in off and VP.CRISIS_SPOKEN not in off and "aica について聞かれたら" not in off


def test_voice_copies_match_the_dip_facts_and_crisis_table():
    """voice_resume は dip のコードを読み込まないので、写しが dip のデータと同じかを確かめる。"""
    from voice_resume import prompts as VP

    assert VP.TALK_LAYER_FLAG == logic.TALK_LAYER_FLAG and VP.TALK_LAYER_VERSIONS == logic.TALK_LAYER_VERSIONS
    assert list(VP.AICA_FACTS) == F.COMMON["ask_service"] and list(VP.ADVICE_FACTS) == F.COMMON["ask_advice"]
    assert list(VP.CRISIS_WORDS) == guard.CRISIS["words"]
    for line in guard.CRISIS["contacts"]:  # 窓口の名前と番号(読み上げの形)
        name, number = re.match(r"・(?:#)?(.+?) (0[\d-]+)", line).groups()
        assert name.removeprefix("18歳までなら ") in VP.CRISIS_SPOKEN and number in VP.CRISIS_SPOKEN


def _switch_in(mod, st, said, script):
    outputs, stages = [], []
    r = mod.switch_in(st, UID, said, None)
    while r.get("status") == "llm":
        stages.append(r["call"]["stage"])
        outputs.append({"stage": r["call"]["stage"], "output": script(r["call"])})
        r = mod.switch_in(st, UID, said, outputs)
    return r, stages


def test_opening_words_get_the_same_guard_as_a_normal_turn(tmp_path):
    """始めた発言にも、ふつうのターンと同じ守り: 表に無い危ない言い方(読む段の crisis)には、最初の問いの代わりに窓口の文
    (言う段を呼ばない)。v5 の闇バイトの言葉(DANGER_RE)には、読む段を呼ばずに注意文と入口のカード。v5 の表の言葉も読む段を呼ばない。"""
    st = State(tmp_path / "state.json")
    said = "もう全部終わりにしたい。正社員で探したい"
    session.start(st, UID, text=said)
    r, stages = _switch_in(session, st, said, lambda c: {"acts": [act("crisis")], "hook": None, "mentions": [], "ng": []})
    assert stages == ["read"] and r["reply"] == session.C2.CRISIS_TEXT and r["choices"] == [] and "ui" not in r
    h = session._load(st, UID)
    assert session.C2.CRISIS_FLAG in h.flags and h.current["qid"] == "q_segment"  # 「続ける」で最初の問いから

    uid = "w-0000000000000t1b"
    for words, expect in (("日払いで受け子のバイトしたい", baito_session.H.NOTICE["danger"]),
                          ("バイト探してるけど消えたい", baito_session.H.CRISIS_TEXT)):
        baito_session.end(st, uid)
        baito_session._drop(st, uid)
        assert baito_session.start(st, uid, text=words)["switch_to"] == "baito"
        r = baito_session.switch_in(st, uid, words, None)  # 読む段を呼ばない
        assert r["status"] == "reply" and r["reply"].startswith(expect.split("\n")[0]) and r["counted"] is False
