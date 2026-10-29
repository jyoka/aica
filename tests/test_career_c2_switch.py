"""入り口を移す(設計メモ docs/design/conversation-layer.md §4.5、§4.15、§5 の 6 枚目。task 359)。

実 LLM なし。正社員の c2 の相談のツール(dip_mcp.career.session)と、本物のランタイムと dip の MCP で確かめる(偽のモデル)。
- 確かめる場面: career の switch-*(c2)。baito-switch-hold と baito-switch-also-fulltime は、層の決まり(dip_mcp.convo.switch と
  応じ方の計画)に場面の発言と読みを当てて確かめる(アルバイト・パートの相談を層に載せるのは 7 枚目。v4 は今のまま)
- c2 の「とりあえずバイトでいい。週3くらいで」: 引き継ぎの記録(閉じたときの値と、発言の中の値)を書き、確かめた値を保存して閉じる。
  移る先の v4 は層の版でないので、今と同じくトークの AI がこのターンを受ける
- 移る強い頼み(EXIT_REQUEST)と移るボタンは読む段を呼ばない。打ち消し(EXIT_HOLD)は移らない
- ほかの入り口から移ってくる(switch_in): 記録の値を入れ(同じ意味は元の確かさ、近い意味は推測)、区分を正社員にし、
  移ったことの相づちと最初の問い。ランタイムは、トークの AI を挟まずに同じターンで始める
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from functools import partial

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from dip_mcp.baito import session as baito_session
from dip_mcp.career import c2 as C2
from dip_mcp.career import harness as H
from dip_mcp.career import session
from dip_mcp.convo import plan as P
from dip_mcp.convo import switch as SW
from dip_mcp.data import State, now
from line_runtime.line_channel import line_user
from tests.test_career_c2 import FakeReadLLM, act, say_question, scripted
from tests.test_career_chat import current_qid, start_script
from tests.test_consult import ConsultModel
from tests.test_handle import reply
from tests.test_line_data import _say
from tests.test_line_webhook import RAW_USER, _server, line_id_key  # noqa: F401  line_id_key はフィクスチャ

UID = "w-0000000000000c2b"
LABEL = C2.ENTRY.switch[0]["label"]  # 「アルバイト・パートで探す」
NOTE = C2.ENTRY.switch[0]["note"]
CASUAL = "とりあえずバイトでいい。週3くらいで"
CONSIDER = "バイトも並行して探そうかな"


@pytest.fixture(autouse=True)
def c2_on(monkeypatch):
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c2")
    monkeypatch.delenv(logic.BAITO_HARNESS_FLAG, raising=False)
    monkeypatch.delenv(H.FORM_ENV, raising=False)  # 形 C(既定)


def run(st, text, script, uid=UID):
    """発言 1 つを、ランタイムと同じ呼び直しで進める(呼んだ段の名前も返す)。"""
    outputs, stages = [], []
    r = session.turn(st, uid, text, None)
    while r.get("status") == "llm":
        stages.append(r["call"]["stage"])
        outputs.append({"stage": r["call"]["stage"], "output": script(r["call"])})
        r = session.turn(st, uid, text, outputs)
    return r, stages


def switch_in(st, text, script=say_question, uid=UID):
    outputs, calls = [], []
    r = session.switch_in(st, uid, text, None)
    while r.get("status") == "llm":
        calls.append(r["call"])
        assert SW.KEY in st.user(uid) or not calls  # 呼び直しの間は、記録を消さない
        outputs.append({"stage": r["call"]["stage"], "output": script(r["call"])})
        r = session.switch_in(st, uid, text, outputs)
    return r, calls


def to_area(st, uid=UID):
    session.start(st, uid)
    run(st, "同じ仕事で上へ", say_question, uid)
    run(st, "年収を上げたい", say_question, uid)
    assert current_qid(st, uid) == "hf_area"


# ----------------------------- 確かめる場面 -----------------------------
def test_c2_passes_the_career_switch_scenes_and_hands_the_v4_move_to_the_talk_ai():
    """career の switch-*: 考えているだけなら移らずにボタン、引き継いだ勤務地は聞き直さない。「とりあえずバイトでいい」は、
    移る先が v4(層の版でない)なので、今と同じくトークの AI がこのターンを受ける(移る先と区分はトークの AI しだいで、モックでは判定しない)。"""
    from evals import scenes as S
    from evals.scenes import checks as K
    from evals.scenes import runner as R

    ids = [s["id"] for s in S.load_scenes() if s["entry"] == "career" and s["category"] == "switch"]
    assert sorted(ids) == ["switch-career-also-baito", "switch-career-carried-area", "switch-career-to-baito"]
    rows = {r["scene"]: r for r in R.run(["career"], {"career": "c2"}, ids)}
    bad = {(sid, x["check"]): x["detail"] for sid, r in rows.items() for x in r["results"] if x["status"] == K.FAIL}
    assert not bad, bad
    for sid in ("switch-career-also-baito", "switch-career-carried-area"):
        assert all(x["status"] == K.PASS for x in rows[sid]["results"] if not x.get("judge")), sid
    moved = rows["switch-career-to-baito"]["replies"][0]
    assert (moved["how"], moved["closed"], moved["llm_calls"]) == ("talk", "switch", 2)  # 読む段 1 回 + トークの AI
    offered = rows["switch-career-also-baito"]["replies"][0]
    assert offered["choices"][0] == LABEL and NOTE in offered["text"]
    # 今の版の c1 は、考えているだけの発言にボタンを出さない(移るのはトークの AI。今のまま)
    c1 = {r["scene"]: r for r in R.run(["career"], {"career": "c1"}, ["switch-career-also-baito"])}
    assert [x["status"] for x in c1["switch-career-also-baito"]["results"] if x["check"] == "offers_entries"] == [K.FAIL]


def scene(sid):
    from evals import scenes as S

    return next(s for s in S.load_scenes() if s["id"] == sid)


def test_layer_rules_on_the_baito_switch_scenes():
    """baito-switch-hold(「正社員じゃなくてバイトで探したい」)は移らない。baito-switch-also-fulltime(「正社員も考えてるんだよね」)は、
    移らずに正社員へ移るボタンを出し、数えない。層の決まりに、場面の発言と読みと、アルバイト・パートの頼みと打ち消しの正規表現を当てる。"""
    hold = scene("baito-switch-hold")["says"][0]
    assert not SW.requested(hold["text"], baito_session.EXIT_REQUEST, baito_session.EXIT_HOLD)
    assert SW.wanted(hold["reading"], ["career"]) is None and SW.offered(hold["reading"], ["career"]) == []
    also = scene("baito-switch-also-fulltime")["says"][0]
    assert not SW.requested(also["text"], baito_session.EXIT_REQUEST, baito_session.EXIT_HOLD)
    assert SW.wanted(also["reading"], ["career"]) is None
    p = P.respond(also["reading"], entries=("career",))
    assert (p.offer, p.next, p.count, p.offtrack, p.ack) == (["career"], "keep", "free", False, "short")
    assert P.respond(also["reading"]).offer == []  # 移れない入り口へのボタンは出さない
    to = scene("baito-switch-to-fulltime")["says"][0]
    assert SW.requested(to["text"], baito_session.EXIT_REQUEST, baito_session.EXIT_HOLD)
    assert SW.wanted(to["reading"], ["career"]) == "career"


# ----------------------------- c2 から移る -----------------------------
def test_casual_switch_writes_the_handoff_saves_what_is_confirmed_and_closes(tmp_path):
    """「とりあえずバイトでいい。週3くらいで」(読む段の switch の want): 発言の中の週 3 も捨てず、閉じたときの全部の値(推測も)と
    一緒に引き継ぎの記録に残す。確かめた値は保存する。v4 へは直に移らない(direct でない)。区分はトークの AI が書くので、層は書かない。"""
    st = State(tmp_path / "state.json")
    to_area(st)
    run(st, "東京で500万以上", scripted({"東京で500万以上": [act("answer", slot="area", text="東京"),
                                                          act("add", slot="min_annual_salary", text="500万以上", number=500)]}))
    track = logic.search_track(st.user(UID))
    r, stages = run(st, CASUAL, scripted({CASUAL: [act("switch", to="baito", strength="want"),
                                                   act("add", slot="work_hours", text="週3くらい")]}))
    assert stages == ["read"] and r["active"] is False
    assert (r["ended"], r["switch_to"], r["direct"]) == ("switch", "baito", False)
    assert not session.active(st, UID)
    rec = st.user(UID)[SW.KEY]
    assert (rec["from"], rec["to"], rec["said"]) == ("career", "baito", CASUAL)
    assert rec["acts"] == [{"act": "add", "slot": "work_hours", "text": "週3くらい"}]
    assert (rec["slots"]["area"]["text"], rec["slots"]["area"]["confidence"]) == ("東京", "high")
    assert rec["slots"]["min_annual_salary"]["confidence"] == "low"  # 推測も残す(保存はしない)
    assert rec["slots"]["work_hours"]["text"] == "週3くらい"
    prefs = logic.stated_preferences(st.user(UID))
    assert prefs["area"]["value"] == "東京" and "min_annual_salary" not in prefs
    assert logic.search_track(st.user(UID)) == track


def test_exit_request_moves_without_the_read_stage_but_a_negation_does_not(tmp_path):
    """移る強い頼み(EXIT_REQUEST)は、読む段を呼ばずに移る(c1 は閉じてトークの AI へ。c2 も v4 へはトークの AI)。
    打ち消し(「バイトじゃなくて」。EXIT_HOLD)は移らず、読む段で読む。"""
    st = State(tmp_path / "state.json")
    to_area(st)
    neg = "バイトじゃなくて正社員で探したい"
    r, stages = run(st, neg, scripted({neg: []}))
    assert r["active"] is True and stages[0] == "read" and SW.KEY not in st.user(UID)
    r, stages = run(st, "やっぱりバイトで探したい", scripted({}))
    assert stages == [] and (r["active"], r["ended"], r["switch_to"]) == (False, "switch", "baito")
    assert st.user(UID)[SW.KEY]["acts"] == []


def test_considering_adds_the_switch_button_and_the_note_once_and_the_button_moves(tmp_path):
    """「バイトも並行して探そうかな」(consider): 移らず、数えず、同じ問いに移るボタンを前に足す。注意は相談で 1 回だけ。
    今の問いの候補は外さない。ボタンを押せば、読む段を呼ばずに移る。"""
    st = State(tmp_path / "state.json")
    to_area(st)
    h = session._load(st, UID)
    turns, first = h.turns, list(h.current["choices"])
    consider = scripted({CONSIDER: [act("switch", to="baito", strength="consider")]})
    r, _ = run(st, CONSIDER, consider)
    assert r["active"] and r["counted"] is False and current_qid(st, UID) == "hf_area"
    assert r["choices"] == [LABEL, *first] and NOTE in r["reply"]
    assert session._load(st, UID).turns == turns and session._load(st, UID).current["choices"] == first
    r, _ = run(st, CONSIDER, consider)
    assert r["choices"][0] == LABEL and NOTE not in r["reply"]  # 注意は 1 回だけ
    r, stages = run(st, LABEL, scripted({}))
    assert stages == [] and (r["active"], r["switch_to"]) == (False, "baito")


def test_crisis_beats_the_switch_request(tmp_path):
    st = State(tmp_path / "state.json")
    to_area(st)
    r, stages = run(st, "もう死にたい。バイトで探したい", scripted({}))
    assert r["active"] is True and stages == [] and r["reply"] == C2.CRISIS_TEXT and SW.KEY not in st.user(UID)


# ----------------------------- c2 へ移ってくる -----------------------------
def handoff(st, uid=UID, **kw):
    """アルバイト・パートの層の版(v5。7 枚目)が書く形の記録。"""
    rec = SW.record("baito", "career", kw.pop("slots", {
        "near.area": {"status": "filled", "text": "東京", "value": "東京", "confidence": "high", "from": "near"},
        "days_per_week": {"status": "filled", "text": "週5日以上", "value": "5+", "confidence": "high", "from": "button"},
        "age_band": {"status": "filled", "text": "18歳以上", "value": "18_plus", "confidence": "high", "from": "button"}}),
        kw.pop("acts", []), kw.pop("said", "やっぱり正社員で探したい"), kw.pop("at", now(uid)), kw.pop("prior", None))
    SW.write(st, uid, rec)
    return rec


def test_switch_in_reads_the_handoff_switches_the_track_and_asks_the_first_question(tmp_path):
    """switch_in: 勤務地(同じ意味)は元の確かさのまま、日数(近い意味)は働ける時間に推測で入れ、聞き直さない。年齢は写さない。
    区分を正社員にして切り替えの記録を返す。返事は移ったことの相づちと最初の問い(言う段 1 回)。記録は最後まで進んだ回に消す。"""
    st = State(tmp_path / "state.json")
    logic.set_search_track(st, UID, "アルバイト・パート", evidence="バイトを探したい")
    handoff(st)
    r, calls = switch_in(st, "やっぱり正社員で探したい", lambda call: {"reply": "正社員で探しますね。" + say_question(call)["reply"]})
    assert [c["stage"] for c in calls] == ["say"] and r["status"] == "reply" and r["counted"] is False
    assert json.loads(re.search(r"計画\(JSON\): (\{.*\})", calls[0]["system"]).group(1))["about"] == ["正社員で探します"]
    assert calls[0]["model_text"] == "やっぱり正社員で探したい"
    h = session._load(st, UID)
    assert (h.slots["area"]["text"], h.slots["area"]["confidence"], h.slots["area"]["from"]) == ("東京", "high", "baito")
    # 働ける時間はブランクから戻る人の欄なので、区分が決まるまで pending に置く(c1 の set_slot と同じ)
    assert (h.pending["work_hours"]["text"], h.pending["work_hours"]["confidence"]) == ("週5日以上", "low")
    assert "age_band" not in {**h.slots, **h.pending}
    assert h.came_from["from"] == "baito" and "said" not in h.came_from
    assert r["question"] == "q_segment" and r["reply"].startswith("正社員で探しますね。") and r["switched"] == {"from": "baito"}
    assert logic.search_track(st.user(UID)) == "正社員"
    assert r["track_switch"]["from"] == "アルバイト・パート" and r["track_switch"]["track"] == "正社員"
    assert SW.KEY not in st.user(UID)
    asked = []
    while (qid := current_qid(st, UID)) not in (None, "review"):
        asked.append(qid)
        q = H.qmap(session._load(st, UID), H.BANK)[qid]
        run(st, next(c for c in q["choices"] if c not in (H.MORE, H.PASS, H.DONT_KNOW)), say_question)
    assert "hf_area" not in asked  # 引き継いだ勤務地を聞き直さない


def test_switch_in_falls_back_to_the_fixed_ack_and_refuses_when_not_on_the_layer(tmp_path, monkeypatch):
    st = State(tmp_path / "state.json")
    handoff(st)
    r, _ = switch_in(st, "やっぱり正社員で", lambda call: {"reply": "???"})  # 言う段が 2 回ともだめ: 決まった組み立て
    assert r["reply"].startswith("正社員で探します。")
    assert session.switch_in(st, UID, "やっぱり正社員で") == {"active": False}  # 進めている相談がある
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    st2 = State(tmp_path / "state2.json")
    assert session.switch_in(st2, UID, "やっぱり正社員で") == {"active": False}  # 層の版でない(トークの AI に渡す)


def test_talk_ai_start_also_reads_a_handoff_for_career_but_not_an_old_or_foreign_one(tmp_path):
    st = State(tmp_path / "state.json")
    handoff(st, at=now(UID) - SW.TTL - timedelta(minutes=1))  # 期限を過ぎた記録は読まない
    out = session.start(st, UID)
    assert "area" not in session._load(st, UID).slots and "area" not in out["prefilled"]
    session.end(st, UID)
    assert SW.KEY not in st.user(UID)  # 新しい会話(end)で消える
    SW.write(st, UID, SW.record("career", "baito", {}, [], "", now(UID)))  # 正社員に宛てていない記録
    session.start(st, UID)
    assert SW.KEY in st.user(UID) and "area" not in session._load(st, UID).slots
    session.end(st, UID)
    handoff(st)
    out = session.start(st, UID)
    assert session._load(st, UID).slots["area"]["text"] == "東京" and "area" in out["prefilled"]
    assert SW.KEY not in st.user(UID)


def test_going_back_restores_the_values_of_the_first_entry(tmp_path):
    """正社員 → アルバイト・パート → すぐ正社員: 移った先の記録(prior)で、正社員の推測の値も戻る(取り消せる。§4.5)。"""
    st = State(tmp_path / "state.json")
    to_area(st)
    run(st, "東京で500万以上", scripted({"東京で500万以上": [act("answer", slot="area", text="東京"),
                                                          act("add", slot="min_annual_salary", text="500万以上", number=500)]}))
    run(st, "やっぱりバイトで探したい", scripted({}))
    first = st.user(UID).pop(SW.KEY)
    handoff(st, slots={}, said="やっぱり正社員", prior=first)  # v5 が、受け取った記録を prior に付けて戻す
    switch_in(st, "やっぱり正社員")
    h = session._load(st, UID)
    assert (h.slots["min_annual_salary"]["number"], h.slots["min_annual_salary"]["confidence"]) == (500, "low")
    assert h.slots["area"]["text"] == "東京" and h.segment == "same_path"


def test_carry_maps_only_the_entry_definition_and_never_converts_numbers():
    rec = SW.record("baito", "career", {
        "hourly_wage_min": {"status": "filled", "text": "1,200円以上", "value": 1200, "confidence": "high"},
        "days_per_week": {"status": "filled", "text": "週3〜4日", "confidence": "high"},
        "time_slots": {"status": "filled", "text": "朝・昼", "confidence": "high"}}, [], "", now(UID))
    got = SW.carry(rec, "career", C2.ENTRY.carry)
    assert set(got) == {"work_hours"}  # 時給は年収に換算しない(対応が無い)
    assert (got["work_hours"]["text"], got["work_hours"]["confidence"]) == ("週3〜4日、朝・昼", "low")


def test_entry_definition_checks_the_switch_and_carry_shape(tmp_path):
    from dip_mcp.convo import entry as E

    d = json.loads((H.HERE / "entry.json").read_text(encoding="utf-8"))
    for broken, word in ((lambda d: d["switch"][0].pop("track"), "移る先"),
                         (lambda d: d["carry"]["baito"].__setitem__("x", {"slot": "nope", "how": "same"}), "carry"),
                         (lambda d: d["carry"].__setitem__("career", {"area": {"slot": "area", "how": "same"}}), "carry"),
                         (lambda d: d["carry"]["baito"]["near.area"].__setitem__("how", "guess"), "carry")):
        bad = json.loads(json.dumps(d))
        broken(bad)
        p = tmp_path / "entry.json"
        p.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        (tmp_path / d["bank"]).write_text((H.HERE / d["bank"]).read_text(encoding="utf-8"), encoding="utf-8")
        (tmp_path / d["facts"]).write_text((H.HERE / d["facts"]).read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(ValueError, match=word):
            E.load(p, H.ALL_SLOTS, ("career", "baito"), C2.TEXTS)


# ----------------------------- ランタイム -----------------------------
def test_line_webhook_c2_casual_switch_to_v4_goes_through_the_talk_ai(tmp_path, monkeypatch):
    """本番の入口(LINE の Webhook)で、c2 の相談の途中の「とりあえずバイトでいい」: 読む段の後に相談を閉じ、トークの AI がこのターンを
    受ける(今と同じ。v4 は層の版でない)。記録は残り、トークの AI の会話には本人の発言が入る。"""
    uid = line_user(RAW_USER)
    llm = FakeReadLLM()
    llm.READINGS = {**FakeReadLLM.READINGS, CASUAL: [act("switch", to="baito", strength="want"),
                                                    act("add", slot="work_hours", text="週3くらい")]}
    model = ConsultModel(script=[*start_script(uid), reply(["アルバイト・パートで探しましょう"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=llm))
    with TestClient(server.app) as client:
        _say(client, "正社員で転職したいので相談したい", 1)
        assert session.active(server.state, uid)
        _say(client, CASUAL, 2)
        texts = [m["text"] for m in fake.sent()[-1][1]["messages"] if m["type"] == "text"]
    st = State(tmp_path / "state.json")
    assert texts == ["アルバイト・パートで探しましょう"] and not session.active(st, uid)
    assert st.user(uid)[SW.KEY]["acts"][0]["text"] == "週3くらい"
    assert [stage for stage, _, _ in llm.calls][-1] == "read"


def test_runtime_starts_the_layer_destination_in_the_same_turn_without_the_talk_ai(tmp_path, monkeypatch):
    """移る先が層の版(c2)なら、ランタイムはトークの AI を呼ばずに、同じターンで移る先の相談を始める(switch_in)。返信は移る先の
    最初の問いで、区分の切り替えの表示が付く。移る元はアルバイト・パートの層の版(v5。7 枚目)の代わりに、baito_turn の結果を差し替える。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    st = State(tmp_path / "state.json")
    logic.set_search_track(st, UID, "アルバイト・パート", evidence="バイトを探したい")
    handoff(st)
    model = ConsultModel(script=[reply(["(トークの AI は呼ばれない)"])])
    llm = FakeReadLLM()
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=model, in_memory=True, career_llm=llm) as rt:
            real = rt._dip

            async def dip(name, args):
                if name == "baito_turn":
                    return {"active": False, "ended": "switch", "switch_to": "career", "direct": True}
                return await real(name, args)
            rt._dip = dip
            return await rt.handle(UID, "message", "やっぱり正社員で探したい")

    out = asyncio.run(go())
    st = State(tmp_path / "state.json")
    h = session._load(st, UID)
    assert h is not None and h.current["qid"] == "q_segment" and h.slots["area"]["text"] == "東京"
    assert model.script and [stage for stage, _, _ in llm.calls] == ["say"]  # トークの AI を呼ばない。言う段 1 回
    assert out["quick_replies"] == h.current["choices"] and out["track_switch"]["track"] == "正社員"
    assert logic.search_track(st.user(UID)) == "正社員" and SW.KEY not in st.user(UID)


def test_scene_runner_moves_directly_to_a_layer_destination(monkeypatch):
    """場面の一覧も、ランタイムと同じく層の版へは同じターンで移る(baito-switch-to-fulltime。移る元の v5 は 7 枚目なので、
    v4 の turn を、記録を書いて閉じる形に差し替える)。移った先で勤務地を聞き直さず、区分の問いから始める。"""
    from evals.scenes import checks as K
    from evals.scenes import runner as R

    def v5_like(state, uid, text, llm_outputs=None):
        h = baito_session._load(state, uid)
        slots = {"near.area": {"status": "filled", "text": h.near["area"], "confidence": "high", "from": "near"}}
        rec = SW.record("baito", "career", slots, [], text, now(uid))
        baito_session._finish(state, uid, h, partial=True)
        SW.write(state, uid, rec)
        return {"active": False, "ended": "switch", "switch_to": "career", "direct": True}
    monkeypatch.setattr(baito_session, "turn", v5_like)
    row = R.run(["baito"], {"baito": "v4"}, ["baito-switch-to-fulltime"])[0]
    got = {(x["check"], x["args"].get("key") or x["args"].get("slot") or x["args"].get("qid")): x["status"]
           for x in row["results"] if not x.get("judge")}
    assert set(got.values()) == {K.PASS}, row["results"]
    assert row["replies"][0]["how"] == "harness" and row["replies"][0]["switched"] == {"from": "baito", "to": "career"}
