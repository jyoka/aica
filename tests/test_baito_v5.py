"""アルバイト・パートの相談の v5(会話の層に載せ替えた版。dip_mcp/baito/v5.py、設計メモ docs/design/conversation-layer.md §5 の 7 枚目)。

実 LLM なし。台本の LLM と偽のモデルで、相談のツール(dip_mcp.baito.session)と、本物のランタイムと dip の MCP を回す。
- 確かめる場面: baito の全部、baito-switch-to-fulltime(移る先は正社員の c2)、switch-career-to-baito(正社員の c2 から v5 へ)
- PRD の結果の検査(設計メモ §4.13、§6 の 9): 20 ペルソナ(evals.baito_sim --version v5)と、未成年の NG、深夜を出さない、中学生以下に
  推薦しない、金額を言わない、カードに無い職種を言わない、闇バイトの注意。turn() の形とプロンプトの原文の検査は v4 だけに残す
- 始めるときに希望の項目と引き継ぎの記録を読む、「やめる」「あとで」、入り口を移す(LINE の Webhook で、同じターンに移る)、スタンプ
- 引き継ぎの記録が、LINE の「データを消す」と Web の「すべて消す」で一緒に消える(jyoka/tasks#359 の Decision)
"""

from __future__ import annotations

import asyncio
import json
import re
from functools import partial
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from dip_mcp.baito import harness as H
from dip_mcp.baito import session
from dip_mcp.baito import v5 as V5
from dip_mcp.career import session as career_session
from dip_mcp.convo import switch as SW
from dip_mcp.data import State, now
from evals import baito_sim
from evals.scenes import checks as K
from evals.scenes import runner as R
from line_runtime import baito_chat
from line_runtime.agent import PINNED_USER
from line_runtime.line_channel import line_user
from tests.test_career_c2 import FakeReadLLM, act, say_question
from tests.test_career_chat import USAGE, start_script
from tests.test_consult import ConsultModel
from tests.test_handle import call, reply
from tests.test_line_data import _ask, _say, _tap
from tests.test_line_webhook import RAW_USER, _event, _post, _server, line_id_key  # noqa: F401  line_id_key はフィクスチャ
from tests.test_revisit import greet
from tests.test_talk_page import app, open_talk, stream  # noqa: F401  app はフィクスチャ

UID = "w-00000000000000b5"
CASUAL = "とりあえずバイトでいい。週3くらいで"
QM = H.qmap(H.BANK)


@pytest.fixture(autouse=True)
def v5_on(monkeypatch):
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v5")
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c2")
    monkeypatch.delenv("CAREER_HARNESS_FORM", raising=False)


def script(readings=None):
    """台本の LLM(相談のツールの呼び直しの 1 回分): 読む段は readings の act の並び(無ければ空)、言う段は場面の一覧と同じ台本。"""
    def out(c):
        if c["stage"] == "read":
            said = re.search(r"本人の今の発言: (.*)", c["system"]).group(1).strip()
            return {"acts": (readings or {}).get(said, []), "hook": None, "mentions": [], "ng": []}
        return R.BaitoV5().write(c, "OK！")
    return out


def run(st, text, scr=None, uid=UID):
    """発言 1 つを、ランタイムと同じ呼び直しで進める(呼んだ段の名前も返す)。"""
    scr = scr or script()
    outputs, stages = [], []
    r = session.turn(st, uid, text, None)
    while r.get("status") == "llm":
        stages.append(r["call"]["stage"])
        outputs.append({"stage": r["call"]["stage"], "output": scr(r["call"])})
        r = session.turn(st, uid, text, outputs)
    return r, stages


def harness(st, uid=UID) -> V5.State:
    h = session._load(st, uid)
    assert h is not None and h.version == "v5"
    return h


def walk(st, uid=UID, limit=40) -> list[str]:
    """ボタンの先頭(まとめは「これでOK」)を押し続け、出した問いの id を返す。"""
    asked = []
    for _ in range(limit):
        h = session._load(st, uid)
        if h is None:
            break
        cur = h.current
        asked.append(cur["qid"])
        said = H.REVIEW_OK if cur["mode"] == "review" else "18歳以上" if cur["qid"] == "hf_age" else cur["choices"][0]
        r, _ = run(st, said, uid=uid)
        if not r.get("active") or r["status"] == "result":
            break
    return asked


# ----------------------------- 確かめる場面 -----------------------------
def test_v5_passes_every_baito_scene():
    rows = R.run(["baito"], {"baito": "v5"})
    assert rows and all(r["error"] is None for r in rows)
    code = [(r["scene"], x["check"], x["status"], x["detail"]) for r in rows for x in r["results"] if not x.get("judge")]
    assert [c for c in code if c[2] != K.PASS] == []  # モックで判定しない条件も無い(閉じてトークの AI に渡すターンが無い)
    by = {r["scene"]: r for r in rows}
    # 「やっぱり正社員で探したい」は、読む段を呼ばずに、同じターンで正社員の c2 に移る(言う段 1 回)
    sw = by["baito-switch-to-fulltime"]["replies"][0]
    assert (sw["how"], sw["switched"], sw["llm_calls"]) == ("harness", {"from": "baito", "to": "career"}, 1)
    # 「やめる」は決まった文で閉じる(トークの AI を挟まない)
    stop = by["baito-flow-stop"]["replies"][0]
    assert (stop["how"], stop["closed"], stop["llm_calls"]) == ("harness", "exit", 0)
    # 値を読めない「うん」は「受け取れなかった」と言わず、違う言い方で聞き直す
    un = by["baito-short-un"]["replies"][0]
    assert V5.ENTRY.texts["retry"] in un["text"] and QM["hf_days"]["alt_text"] in un["text"]


def test_v4_still_reads_the_same_scenes_as_before():
    """今の版(v4)は変えていない: 層の版だけが受ける場面(直す、「あとで」、移るボタン)は v4 では今までどおり落ちる。"""
    rows = {r["scene"]: r for r in R.run(["baito"], {"baito": "v4"},
                                          ["baito-correct-days", "baito-flow-later", "baito-switch-also-fulltime"])}
    for sid, check in (("baito-correct-days", "slot"), ("baito-flow-later", "closed"),
                       ("baito-switch-also-fulltime", "offers_entries")):
        assert any(x["check"] == check and x["status"] == K.FAIL for x in rows[sid]["results"]), sid


def test_switch_career_to_baito_is_judged_again_with_v5():
    """正社員の c2 から「とりあえずバイトでいい。週3くらいで」: v5 は層の版なので、トークの AI を挟まず同じターンで移る。
    発言の中の週 3 は日数に推測で入り、日数を聞き直さない。勤務地(横浜)は都県の名前が無いので近くで探すのエリアには入れない。"""
    row = R.run(["career"], {"career": "c2", "baito": "v5"}, ["switch-career-to-baito"])[0]
    assert all(x["status"] == K.PASS for x in row["results"] if not x.get("judge")), row["results"]
    rep = row["replies"][0]
    assert (rep["how"], rep["switched"], rep["llm_calls"]) == ("harness", {"from": "career", "to": "baito"}, 2)  # 読む段 + 言う段
    snap = rep["states"]["baito"]
    assert snap["qid"] == H.QUICK_PICK and "near.area" not in snap["slots"]
    assert (snap["slots"]["days_per_week"]["value"], snap["slots"]["days_per_week"]["confidence"]) == ("3-4", "low")


# ----------------------------- PRD の結果の検査(v5 のテスト) -----------------------------
def test_the_20_personas_pass_the_prd_acceptance_on_v5():
    """evals.baito_sim --version v5: 20 ペルソナ(モック)が、v4 と同じ基準値と合否指標で合格する(未成年の違反、結果の違反、
    取りこぼし、ループ、15 ターン超えがどれも 0)。"""
    r, bad = baito_sim.check(version="v5")
    assert bad == [], bad
    by = {m["persona"]: m for m in r["results"]["v4"]}
    assert len(by) == 20 and all(m["reached"] for m in by.values())
    assert all(not m["minor_violation"] and not m["result_violation"] for m in by.values())
    assert by["jhs"]["blocked"] == "under_15" and by["jhs"]["recs"] == []  # 中学生以下には推薦しない
    assert "深夜シフト" in by["hs16"]["derived_ng"] and by["danger"]["danger_warned"]


class LLM:
    """ハーネスを直に回す偽の LLM: 読む段は readings、言う段は gen(無ければ場面の台本)。呼んだ段を数える。"""

    def __init__(self, readings=None, gen=None):
        self.readings, self.gen, self.calls = readings or {}, gen, []

    def read(self, payload):
        self.calls.append("read")
        return json.dumps({"acts": self.readings.get(payload["user_text"], []), "hook": None, "mentions": [], "ng": []},
                          ensure_ascii=False)

    def generate(self, view):
        self.calls.append("generate")
        if self.gen is not None and "plan" in view:
            return json.dumps({"reply": self.gen + view["text"]}, ensure_ascii=False)
        return json.dumps(R.BaitoV5().write({"stage": V5.prompt_stage(view), "system": V5.render_prompt(V5.prompt_stage(view), view)},
                                            "OK！"), ensure_ascii=False)


def at(qid, segment="student", age=None):
    st = V5.new_state()
    V5.start(st, H.BANK)
    st.segment = segment
    if age:
        H.apply_update(st, "age_band", age, "high", "t")
    st.asked.append("hf_age")  # 年齢はもう聞いた(答えていなければ未成年として扱う)
    q = QM[qid]
    st.phase = q["phase"]
    H._present(st, q, "ask")
    return st


def test_minor_rules_hold_on_v5_turns():
    """年齢が分からない・未成年: 深夜のボタンを出さず、打った深夜は入れない(注意を出す)。ルールの NG が付く。"""
    st = at("hf_days")
    r = V5.turn(st, "週1〜2日", H.BANK, LLM())
    assert st.current["qid"] == "hf_time" and "深夜もOK" not in r["view"]["choices"] and H.minor_mode(st)
    assert st.derived_ng == H.DERIVED_NG_MINOR
    said = "夕方と深夜がいい"
    r = V5.turn(st, said, H.BANK, LLM({said: [act("answer", slot="time_slots", text="夕方と深夜", value=["evening", "late_night"])]}))
    assert st.slots["time_slots"]["value"] == ["evening"] and H.NOTICE["late_night_removed"] in r["reply"]
    st = at("hf_age")
    r = V5.turn(st, "中学生以下", H.BANK, LLM())
    assert r["done"] and st.blocked == "under_15" and st.result["recommendations"] == []
    assert H.NOTICE["under_15"] in r["reply"]


def test_v5_replies_never_say_amounts_or_jobs_not_on_the_card():
    """モデルの相づちが金額(扶養の 103 万)やカードに無い職種(居酒屋)を言ったら、2 回とも捨てて決まった組み立てにする。"""
    for said in ("扶養は103万円までだよ。", "居酒屋もあるよ。"):
        st = at("hf_days", age="18_plus")
        llm = LLM(gen=said)
        r = V5.turn(st, "週3〜4日", H.BANK, llm)
        assert said not in r["reply"] and llm.calls == ["generate", "generate"], said
        assert any(e["kind"] == "generate_fallback" for e in st.log)
        assert r["reply"].endswith(QM[st.current["qid"]]["text"])


# ----------------------------- 代替の理由(task 376。9/10 の実モデルの P3 で多かったもの) -----------------------------
class SayModel:
    """言う段だけを台本で書くモデル。write(view, n) が n 回目(1 から)の相づちを返し、問いの文を足す。呼んだ view を残す。"""

    def __init__(self, write, readings=None):
        self.write, self.readings, self.views = write, readings or {}, []

    def read(self, payload):
        return json.dumps({"acts": self.readings.get(payload["user_text"], []), "hook": None, "mentions": [], "ng": []},
                          ensure_ascii=False)

    def generate(self, view):
        self.views.append(view)
        return json.dumps({"reply": self.write(view, len(self.views)) + view["text"]}, ensure_ascii=False)


def _after_two_acks(qid, segment, acks):
    st = at(qid, segment=segment, age="18_plus")
    for a in acks:
        V5.CS.heard(st.convo, "x")
        V5.CS.replied(st.convo, a + "y", a)
    return st


def _kinds(st):
    return [e["kind"] for e in st.log if e["kind"].startswith("generate")]


def test_ack_ending_repeated_a_third_time_is_retried_with_the_reason():
    """「〜んだね。」が 2 回続いた後、モデルがまた「〜んだね。」と書く(9/10 の P3 で、やり直しも同じ入力なので同じ返事になり、
    代替に倒れた)。やり直しには、1 回目の返事と落ちた理由(終わりの「んだね。」)と、使える決まった相づちを渡す。"""
    acks = ["9〜14時くらいなんだね。", "週3〜4日なんだね。"]
    stubborn = SayModel(lambda v, n: "けっこうあるんだね。")
    st = _after_two_acks("hf_leave", "homemaker", acks)
    r = V5.turn(st, "けっこうある", H.BANK, stubborn)
    assert _kinds(st) == ["generate_fallback"] and "んだね" not in r["reply"]  # 聞かないモデルは、今までどおり決まった組み立て
    first, again = stubborn.views
    prompt = V5.render_prompt("say", first)
    assert "直前の相づちの終わり: 「んだね。」。" in prompt and "やり直し: (なし)" in prompt
    assert "けっこうあるんだね。" in again["retry"] and "「んだね。」" in again["retry"]
    assert f"「{V5.ENTRY.tone['acks']['short'][0]}」をそのまま使ってよい" in again["retry"]
    listens = SayModel(lambda v, n: "けっこうあるんだね。" if v["retry"] == "(なし)" else "けっこうある、了解！")
    st = _after_two_acks("hf_leave", "homemaker", acks)
    r = V5.turn(st, "けっこうある", H.BANK, listens)
    assert _kinds(st) == ["generate_retry_ok"] and r["reply"].startswith("けっこうある、了解!")


def test_tone_does_not_prime_the_same_ending():
    """入り口の調子と例の文が、同じ終わり方(「〜なんだね」)を勧めない。例の文の終わりは、どれも違う。"""
    tone = V5.ENTRY.tone
    assert "「〜なんだね」" not in tone["style"] and "続けない" in tone["style"]
    tails = [V5.RP.tail(x) for x in tone["examples"]]
    assert len(set(tails)) == len(tails)
    assert len({V5.RP.tail(x) for xs in tone["acks"].values() for x in xs}) > 10


def test_echoing_the_job_tapped_in_reverse_pick_is_not_a_job_off_the_card():
    """逆選択で押した職種をなぞる相づち(「工場の軽作業はナシ、了解！」)は、勧めていないので落とさない(9/10 の P3 で 8 ターンが
    job_not_on_card で代替になった)。押していない職種は、打った文の語(「塾」)や次の問いのボタンにあっても、今までどおり落とす。"""
    tapped = "工場の軽作業（立ち仕事）"
    st = at("rp_homemaker", segment="homemaker", age="18_plus")
    llm = SayModel(lambda v, n: "工場の軽作業はナシ、了解！")
    r = V5.turn(st, tapped, H.BANK, llm)
    assert len(llm.views) == 1 and _kinds(st) == [] and r["reply"].startswith("工場の軽作業はナシ、了解!")
    st = at("rp_side_job", segment="side_job", age="18_plus")
    r = V5.turn(st, "倉庫ピッキング", H.BANK, SayModel(lambda v, n: "倉庫はやめておくね。"))
    assert _kinds(st) == [] and r["reply"].startswith("倉庫はやめておくね。")
    st = at("rp_homemaker", segment="homemaker", age="18_plus")
    llm = SayModel(lambda v, n: "カフェなら座れるよ。")
    r = V5.turn(st, tapped, H.BANK, llm)
    assert _kinds(st) == ["generate_fallback"] and "カフェ" not in r["reply"]
    assert "職種の名前を書かない" in llm.views[1]["retry"]
    said = "塾があるから平日は夜だけ"
    st = at("hf_time", age="18_plus")
    evening = {said: [act("answer", slot="time_slots", text="夜", value=["evening"])]}
    r = V5.turn(st, said, H.BANK, SayModel(lambda v, n: "塾講師なら夜でもいけるね。", evening))
    assert _kinds(st) == ["generate_fallback"] and "塾講師" not in r["reply"]
    rp_buttons = QM["rp_side_job"]["choices"]  # 次の問いのボタン(押していない)の職種
    assert V5._baito_check([["ack", "イベント設営もあるよ。"]], {"choices": rp_buttons, "user_text": "x"}) == "job_not_on_card"
    assert V5._baito_check([["ack", "イベント設営はナシだね。"]], {"choices": []}, tapped="イベント設営") is None


def test_praise_is_retried_with_the_word_and_the_persons_own_words_are_not_praise():
    """「よし、燃えてきた」に「頼もしいね」と書く(9/10 の P3 と、task 376 の実モデルの試しで 2 回とも)。やり直しには、その言葉を
    渡す。本人が言った言葉(「すごい」)をなぞるのは、ほめ言葉にしない。"""
    st = at("sc_line", age="18_plus")
    llm = SayModel(lambda v, n: "よし、燃えてきたって、頼もしいね。" if n == 1 else "燃えてきた、了解！")
    r = V5.turn(st, "よし、燃えてきた", H.BANK, llm)
    assert _kinds(st) == ["generate_retry_ok"] and "ほめ言葉「頼もし」" in llm.views[1]["retry"]
    assert r["reply"].startswith("燃えてきた、了解!")  # 返事は NFKC にそろう
    said = "すごい混んでる店はちょっと"
    st = at("sc_line", age="18_plus")
    calm = {said: [act("answer", slot="pace", text=said, value="calm")]}
    r = V5.turn(st, said, H.BANK, SayModel(lambda v, n: "すごい混んでる店は苦手かぁ。", calm))
    assert _kinds(st) == [] and r["reply"].startswith("すごい混んでる店は苦手かぁ。")


def test_the_retry_note_is_rebuilt_the_same_way_when_the_runtime_replays_the_turn(tmp_path):
    """ランタイムは出力を集めて相談のツールを呼び直す(session.ReplayLLM)。やり直しのプロンプトも、呼び直しで同じに作られ、
    2 回目の出力が使われる。"""
    st = State(tmp_path / "s.json")
    session.start(st, UID, quick=False)
    systems = []

    def scr(c):
        if c["stage"] == "say":
            systems.append(c["system"])
            return R.BaitoV5().write(c, "頼もしいね。" if "やり直し: (なし)" in c["system"] else "OK！")
        return script()(c)
    r, stages = run(st, "旅行・遊び", scr)
    assert stages == ["say", "say"] and "頼もし" not in r["reply"] and r["reply"].startswith("OK!")
    assert "ほめ言葉「頼もし」" in systems[-1] and harness(st).current["qid"] == "q_segment"


def test_every_retry_reason_makes_a_note():
    """やり直しの指示は、どの理由でも作れる(文は str.format に通すので、波かっこで落ちると呼び直さずに代替になる)。"""
    st = _after_two_acks("hf_leave", "homemaker", ["9〜14時くらいなんだね。", "週3〜4日なんだね。"])
    _, rp = V5.view(st, H.BANK, V5.P.Plan(ack="short"))
    for why, text in V5.RETRY_JA.items():
        for raw in (None, "", "not json", '{"reply": "けっこうあるんだね。"}'):
            note = V5.retry_note(raw, why, rp, st.convo, V5.ENTRY.tone["acks"])
            assert text.split("{")[0] in note, (why, raw)
    assert 'JSON {"reply": "..."} の形でない' in V5.retry_note(None, "no_reply", rp, st.convo, V5.ENTRY.tone["acks"])


@pytest.mark.parametrize("first", ["", '{"reply": "けっこう', TimeoutError("timeout")])
def test_empty_broken_or_failed_first_reply_is_retried_with_the_model(first):
    """1 回目が空の返事、途中で切れた JSON、通信の失敗(no_reply)でも、やり直しでモデルをもう 1 回呼び、2 回目の返事を使う。"""
    class Flaky(SayModel):
        def generate(self, view):
            if not self.views:
                self.views.append(view)
                if isinstance(first, Exception):
                    raise first
                return first
            return super().generate(view)
    llm = Flaky(lambda v, n: "けっこうある、了解！")
    st = at("hf_leave", segment="homemaker", age="18_plus")
    r = V5.turn(st, "けっこうある", H.BANK, llm)
    assert len(llm.views) == 2 and _kinds(st) == ["generate_retry_ok"] and r["reply"].startswith("けっこうある、了解!")
    assert "JSON" in llm.views[1]["retry"]


def test_danger_words_get_the_notice_without_the_model_and_are_not_counted():
    st = at("fg_wage", age="18_plus")
    llm = LLM()
    r = V5.turn(st, "受け取るだけで日給5万のやつってどう？", H.BANK, llm)
    assert H.NOTICE["danger"] in r["reply"] and QM["fg_wage"]["text"] in r["reply"]
    assert llm.calls == [] and not r["counted"] and st.turns == 0 and r["view"]["aside"] == "danger"


def test_typed_age_keeps_the_task_258_rule():
    """年齢ははっきりした形だけ読む。「中2」は、読む段が中学生以下と読んでも、ボタンで 1 回確かめる。2 回目も「中学」なら止める。"""
    st = at("hf_age")
    llm = LLM({"中2です": [act("answer", slot="age_band", text="中2", value="under_15")]})
    V5.turn(st, "中2です", H.BANK, llm)
    assert (st.blocked, st.current["qid"], st.current["mode"]) == (None, "hf_age", "reask") and "age_jhs" in st.flags
    r = V5.turn(st, "だから中学生だって", H.BANK, llm)
    assert st.blocked == "under_15" and H.NOTICE["under_15"] in r["reply"]
    st = at("hf_days")
    V5.turn(st, "17歳です", H.BANK, LLM({"17歳です": [act("add", slot="age_band", text="17歳", value="15_17")]}))
    assert st.slots["age_band"]["value"] == "15_17"  # ほかの問いでも、はっきりした形なら読む(v4 と同じ)


# ----------------------------- 相談のツール -----------------------------
def test_new_v5_consultation_reads_saved_preferences_and_the_handoff(tmp_path):
    """v5 で始めるとき、前の相談で保存した希望(時給、エリア)は確かめた値として入れ、正社員から引き継いだ働ける時間(週 3)は推測で
    日数に入れる。どちらも聞き直さない。記録は読んだら消す。都県でない勤務地(横浜)はエリアに入れない。"""
    st = State(tmp_path / "state.json")
    logic.save_preferences(st, UID, {"min_wage": "1200", "area": "東京"})
    SW.write(st, UID, SW.record("career", "baito", {"area": {"status": "filled", "text": "横浜", "confidence": "high"}},
                                [{"act": "add", "slot": "work_hours", "text": "週3くらい"}], CASUAL, now(UID)))
    out = session.start(st, UID, quick=False)
    assert out["started"] and SW.KEY not in st.user(UID)
    h = harness(st)
    assert (h.slots["hourly_wage_min"]["value"], h.slots["hourly_wage_min"]["confidence"]) == (1200, "high")
    assert (h.slots["days_per_week"]["value"], h.slots["days_per_week"]["confidence"]) == ("3-4", "low")
    assert h.near["area"] == "東京"
    asked = walk(st)
    assert "hf_days" not in asked and "fg_wage" not in asked and "review" in asked
    assert not session.active(st, UID)


def test_stop_and_later_close_with_a_fixed_text_and_keep_what_was_confirmed(tmp_path):
    st = State(tmp_path / "state.json")
    session.start(st, UID, quick=False)
    for said in ("旅行・遊び", "学生", "18歳以上"):
        run(st, said)
    while harness(st).current["qid"] != "hf_days":
        run(st, harness(st).current["choices"][0])
    run(st, "週3〜4日")
    r, stages = run(st, "やめる")
    assert stages == [] and (r["status"], r["closed"], r["reply"]) == ("result", "exit", V5.ENTRY.texts["exit"])
    assert r["choices"] == session.RESULT_CHOICES and not session.active(st, UID)
    prefs = logic.stated_preferences(st.user(UID))
    assert prefs["days_per_week"]["value"] == "週3〜4日" and "purpose" not in prefs  # 確かめた値(high)だけ。推測の目的は保存しない
    session.start(st, UID, quick=False)
    assert harness(st).slots["days_per_week"]["from"] == V5.SAVED
    assert "hf_days" not in walk(st)  # 保存した日数は聞かない
    session.start(st, UID, quick=False)
    said = "また今度にする"
    r, stages = run(st, said, script({said: [act("later")]}))
    assert stages == ["read"] and r["closed"] == "later" and "続き" in r["reply"]


def _to_hf_time(st):
    """q_money、q_segment、hf_age、hf_school、hf_days にボタンで答え、hf_time を聞いているところまで進める。"""
    session.start(st, UID, quick=False)
    for said in ("旅行・遊び", "学生", "18歳以上"):
        run(st, said)
    while harness(st).current["qid"] != "hf_days":
        run(st, harness(st).current["choices"][0])
    run(st, "週3〜4日")
    assert harness(st).current["qid"] == "hf_time"


def test_later_resumes_from_the_question_it_left_and_does_not_ask_again(tmp_path):
    """「あとで」は「続きから聞くね」と返す(設計メモ §4.6)。次に始めると、閉じたときの問い(hf_time)から続け、答えた問いを聞き直さない。
    答えた日数(high)は閉じたときに保存し、続きの後の結果でも同じ値のまま。"""
    st = State(tmp_path / "state.json")
    _to_hf_time(st)
    asked = list(harness(st).asked)
    r, stages = run(st, "あとで")
    assert stages == [] and r["closed"] == "later" and "続き" in r["reply"] and r["saved"] == ["days_per_week"]
    assert not session.active(st, UID) and session.PAUSED_KEY in st.user(UID)
    out = session.start(st, UID, quick=False)
    assert out["started"] and out["question"]["id"] == "hf_time" and session.PAUSED_KEY not in st.user(UID)
    h = harness(st)
    assert h.asked == asked and h.slots["school_permission"]["value"] == "ok" and h.segment == "student"
    rest = walk(st)
    assert rest[0] == "hf_time" and not set(rest) & {"q_money", "q_segment", "hf_age", "hf_school", "hf_days"}
    assert logic.stated_preferences(st.user(UID))["days_per_week"]["value"] == "週3〜4日"


def test_later_rereads_what_was_changed_on_the_profile_page_while_paused(tmp_path):
    """「あとで」の後にプロフィール画面で日数を直したら、続きはその値を読み直し、閉じるときに前の値に戻さない。
    区切りに読めない値(「週4日」)に直したら、前の値を入れたままにしない。"""
    st = State(tmp_path / "state.json")
    _to_hf_time(st)
    run(st, "あとで")
    logic.save_preferences(st, UID, {"days_per_week": "週5日以上"})
    session.start(st, UID, quick=False)
    assert (harness(st).slots["days_per_week"]["value"], harness(st).slots["days_per_week"]["from"]) == ("5+", V5.SAVED)
    logic.save_preferences(st, UID, {"days_per_week": "週1〜2日"})  # 相談の途中に、もう一度直す
    run(st, "やめる")
    assert logic.stated_preferences(st.user(UID))["days_per_week"]["value"] == "週1〜2日"
    st2 = State(tmp_path / "state2.json")
    _to_hf_time(st2)
    run(st2, "あとで")
    logic.save_preferences(st2, UID, {"days_per_week": "週4日"})
    session.start(st2, UID, quick=False)
    assert "days_per_week" not in harness(st2).slots


def test_later_then_switching_in_from_career_continues_the_paused_consultation(tmp_path):
    """「あとで」の後に正社員の相談から移ってきたら、取っておいた相談の続き(hf_time)に移る。"""
    st = State(tmp_path / "state.json")
    _to_hf_time(st)
    run(st, "あとで")
    SW.write(st, UID, SW.record("career", "baito", {}, [], CASUAL, now(UID)))
    scr, outputs = script(), []
    r = session.switch_in(st, UID, CASUAL)
    while r.get("status") == "llm":
        outputs.append({"stage": r["call"]["stage"], "output": scr(r["call"])})
        r = session.switch_in(st, UID, CASUAL, outputs)
    assert r["status"] == "reply" and r["question"] == "hf_time" and r["switched"] == {"from": "career"}
    assert session.PAUSED_KEY not in st.user(UID) and SW.KEY not in st.user(UID)
    assert harness(st).slots["days_per_week"]["value"] == "3-4"


def test_forget_all_also_erases_the_paused_consultation(tmp_path):
    from dip_mcp import profile

    st = State(tmp_path / "state.json")
    _to_hf_time(st)
    run(st, "あとで")
    profile.forget_all(st, UID)
    assert session.PAUSED_KEY not in st.user(UID)
    assert session.start(st, UID, quick=False)["question"]["id"] == "q_money"


def test_saved_preferences_are_neither_rounded_nor_written_back(tmp_path):
    """前の相談で保存した希望は、区切りに丸めずに読めるものだけ入れ(「1300」「朝、週末」は入れない)、入れたまま閉じても書き戻さない。
    相談の途中にプロフィール画面で直した値も、閉じるときに戻さない(正社員の session._finish と同じ)。"""
    st = State(tmp_path / "state.json")
    logic.save_preferences(st, UID, {"min_wage": "1300", "time_band": "朝、週末", "days_per_week": "週3〜4日",
                                     "area": "東京"})
    session.start(st, UID, quick=False)
    h = harness(st)
    assert "hourly_wage_min" not in h.slots and "time_slots" not in h.slots
    assert (h.slots["days_per_week"]["from"], h.near["area"]) == (V5.SAVED, "東京")
    for said in ("旅行・遊び", "学生", "18歳以上"):
        run(st, said)
    logic.save_preferences(st, UID, {"days_per_week": "週1〜2日", "area": "神奈川"})  # 相談の途中にプロフィール画面で直す
    r, _ = run(st, "やめる")
    assert r["closed"] == "exit"
    prefs = {k: v["value"] for k, v in logic.stated_preferences(st.user(UID)).items()}
    assert prefs == {"min_wage": "1300", "time_band": "朝、週末", "days_per_week": "週1〜2日", "area": "神奈川"}


def test_saved_time_band_is_kept_when_untouched_and_rebuilt_only_when_answered(tmp_path):
    """保存した時間帯(「夕方〜夜」)は入れて聞かず、そのまま閉じても書き直さない。区切りに読めない「夕方〜夜、土日」は入れない。"""
    st = State(tmp_path / "state.json")
    logic.save_preferences(st, UID, {"time_band": "夕方〜夜"})
    session.start(st, UID, quick=False)
    assert harness(st).slots["time_slots"] == {"value": ["evening"], "confidence": "high", "from": V5.SAVED}
    run(st, "やめる")
    assert logic.stated_preferences(st.user(UID))["time_band"]["value"] == "夕方〜夜"
    logic.save_preferences(st, UID, {"time_band": "夕方〜夜、土日"})
    session.start(st, UID, quick=False)
    assert "time_slots" not in harness(st).slots
    run(st, "やめる")
    assert logic.stated_preferences(st.user(UID))["time_band"]["value"] == "夕方〜夜、土日"


def test_switch_to_career_writes_the_handoff_without_age_and_moves_directly(tmp_path):
    """「やっぱり正社員で探したい」(移る強い頼み): 読む段を呼ばずに閉じ、記録(近くで探すのエリアと日数。年齢は入れない)を書く。
    正社員が層の版(c2)なら direct。正社員の switch_in が記録を読む。"""
    st = State(tmp_path / "state.json")
    session.start(st, UID, quick=False)
    h = harness(st)
    h.near["area"] = "東京"
    for s, v in (("age_band", "18_plus"), ("days_per_week", "5+")):
        H.apply_update(h, s, v, "high", "t")
    session._save(st, UID, h)
    r, stages = run(st, "やっぱり正社員で探したい")
    assert stages == [] and (r["active"], r["ended"], r["switch_to"], r["direct"]) == (False, "switch", "career", True)
    rec = st.user(UID)[SW.KEY]
    assert rec["from"] == "baito" and rec["slots"]["near.area"]["text"] == "東京" and "age_band" not in rec["slots"]
    assert logic.stated_preferences(st.user(UID))["days_per_week"]["value"] == "週5日以上"
    outputs = []
    out = career_session.switch_in(st, UID, "やっぱり正社員で探したい")
    while out.get("status") == "llm":
        outputs.append({"stage": "say", "output": say_question(out["call"])})
        out = career_session.switch_in(st, UID, "やっぱり正社員で探したい", outputs)
    assert out["active"] and career_session._load(st, UID).slots["area"]["text"] == "東京" and SW.KEY not in st.user(UID)


def test_considering_fulltime_offers_the_button_once_and_the_button_moves(tmp_path):
    st = State(tmp_path / "state.json")
    session.start(st, UID, quick=False)
    said = "正社員も考えてるんだよね"
    consider = script({said: [act("switch", to="career", strength="consider")]})
    r, _ = run(st, said, consider)
    label, note = V5.ENTRY.switch[0]["label"], V5.ENTRY.switch[0]["note"]
    assert r["choices"][0] == label and note in r["reply"] and r["counted"] is False
    assert r["choices"][1:] == QM["q_money"]["choices"]
    r, _ = run(st, said, consider)
    assert r["choices"][0] == label and note not in r["reply"]  # 注意は 1 回だけ
    r, stages = run(st, label)
    assert stages == [] and r["switch_to"] == "career"


def test_switch_in_refuses_when_not_on_the_layer_or_already_consulting(tmp_path, monkeypatch):
    st = State(tmp_path / "state.json")
    session.start(st, UID)
    assert session.switch_in(st, UID, CASUAL) == {"active": False}  # 進めている相談がある
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")
    st2 = State(tmp_path / "state2.json")
    assert session.switch_in(st2, UID, CASUAL) == {"active": False}  # 層の版でない(トークの AI に渡す)
    assert logic.layer_on("baito") is False and logic.baito_harness_on()


def test_started_consultations_keep_their_version(tmp_path, monkeypatch):
    """始めた相談は始めた版のまま進む(状態の version)。v4 の相談は v4 のハーネス、v5 の相談は v5 のハーネス。"""
    st = State(tmp_path / "state.json")
    session.start(st, UID, quick=False)
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")
    r, _ = run(st, "旅行・遊び")
    assert harness(st).version == "v5" and r.get("parts")
    st2 = State(tmp_path / "state2.json")
    session.start(st2, UID, quick=False)
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v5")
    r, _ = run(st2, "旅行・遊び")
    assert session._load(st2, UID).version == "v4" and "parts" not in r


def test_read_prompt_and_schema():
    st = at("hf_days")
    p = V5.read_payload(st, H.BANK, "週3くらい")
    text = V5.render_prompt("read", p)
    assert "{{" not in text and "3-4=週3〜4日" in text and "value" in text
    out = baito_chat.SCHEMAS["read"](acts=[act("answer", slot="time_slots", text="夕方", value=["evening"]),
                                           act("add", slot="hourly_wage_min", text="1200", value=1200)],
                                     hook=None, mentions=[], ng=[]).model_dump()
    got, probs = V5.validator("夕方で、1200以上")(out)
    assert probs == [] and [a["value"] for a in got["acts"]] == [["evening"], 1200]
    got, probs = V5.validator("週3")({"acts": [act("answer", slot="days_per_week", text="週3", value="週3〜4日")]})
    assert got["acts"][0]["value"] == "3-4"  # 表示名もキーに直す
    got, probs = V5.validator("駅近")({"acts": [act("add", slot="commute", text="駅近", value="駅近")]})
    assert got["acts"] == [] and probs == ["value_not_in_buckets:commute"]


# ----------------------------- ランタイム(LINE の Webhook) -----------------------------
class FakeBaitoLLM:
    """v5 の言う段(層のプロンプト)は問いの文だけを返す。読む段は READINGS。呼んだ段を残す。"""

    READINGS: ClassVar[dict] = {}

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, stage, system, messages):
        self.calls.append(stage)
        if stage == "read":
            said = re.search(r"本人の今の発言: (.*)", system).group(1).strip()
            return {"acts": self.READINGS.get(said, []), "hook": None, "mentions": [], "ng": []}, USAGE
        q = json.loads(re.search(r"次に聞くこと\(JSON\): (\{.*\})", system).group(1))
        return {"reply": q["text"]}, USAGE


def career_llm():
    llm = FakeReadLLM()
    llm.READINGS = {**FakeReadLLM.READINGS, CASUAL: [act("switch", to="baito", strength="want"),
                                                    act("add", slot="work_hours", text="週3くらい")]}
    return llm


def test_line_webhook_c2_casual_switch_to_v5_moves_in_the_same_turn_without_the_talk_ai(tmp_path, monkeypatch):
    """本番の入口(LINE の Webhook)で、c2 の相談の途中の「とりあえずバイトでいい。週3くらいで」: トークの AI を呼ばずに、同じターンで
    v5 の相談が入口のカードから始まる。週 3 は日数に推測で入り、区分はアルバイト・パートになり、記録は消える。"""
    uid = line_user(RAW_USER)
    model = ConsultModel(script=[*start_script(uid), reply(["(トークの AI は呼ばれない)"])])
    baito = FakeBaitoLLM()
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=career_llm(), baito_llm=baito))
    with TestClient(server.app) as client:
        _say(client, "正社員で転職したいので相談したい", 1)
        assert career_session.active(server.state, uid)
        _say(client, CASUAL, 2)
        texts = [m.get("text", "") for m in fake.sent()[-1][1]["messages"]]
    st = State(tmp_path / "state.json")
    h = harness(st, uid)
    assert model.script and baito.calls == ["say"]  # トークの AI を呼ばない。v5 の言う段 1 回
    assert h.current["qid"] == H.QUICK_PICK and any(H.QUICK_TEXT in t for t in texts)
    assert (h.slots["days_per_week"]["value"], h.slots["days_per_week"]["from"]) == ("3-4", "handoff:career")
    assert logic.search_track(st.user(uid)) == "アルバイト・パート" and SW.KEY not in st.user(uid)
    assert not career_session.active(st, uid)


def baito_start_script() -> list:
    return [call("set_search_track", {"line_user_id": PINNED_USER, "track": "アルバイト・パート", "evidence": "バイトを探したい"}, "t0"),
            call("get_counseling_playbook", {"track": "アルバイト・パート", "channel": "chat"}, "pb"),
            call("start_baito_consultation", {"line_user_id": PINNED_USER}, "st"),
            reply(["一緒に探そう！"])]


def sticker(n: int) -> dict:
    e = _event("message", event_id=f"01S{n}", token=f"reply-token-s{n}")
    e["message"] = {"type": "sticker", "id": "1", "packageId": "446", "stickerId": "1988"}
    return e


def test_line_sticker_during_a_v5_consultation_is_passed_to_the_harness(tmp_path, monkeypatch):
    """v5 の相談の間のスタンプは「[スタンプ]」の文としてハーネスに渡る(モデルを呼ばない)。相談の外のスタンプには今までどおり答えない。"""
    uid = line_user(RAW_USER)
    model = ConsultModel(script=baito_start_script())
    baito = FakeBaitoLLM()
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, baito_llm=baito))
    with TestClient(server.app) as client:
        _post(client, [sticker(0)])
        assert fake.sent() == []  # 相談の外のスタンプ: 答えない
        _say(client, "バイトを探したい", 1)
        assert session.on_layer(server.state, uid)
        n = len(fake.sent())
        _post(client, [sticker(1)])
        assert len(fake.sent()) == n + 1
        texts = [m.get("text", "") for m in fake.sent()[-1][1]["messages"]]
    assert baito.calls == [] and any(H.QUICK_TAP_TEXT in t for t in texts)


# ----------------------------- 引き継ぎの記録を消す(jyoka/tasks#359 の Decision) -----------------------------
@pytest.fixture
def v4_destination(monkeypatch):
    """移る先のアルバイト・パートを今の版(v4)にする: 正社員の c2 が書いた記録は、トークの AI が受けるので読まれずに残る。"""
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")


def test_line_forget_data_also_erases_the_handoff(tmp_path, monkeypatch, v4_destination):
    """LINE で c2 の相談から移ると言い、記録が残っている間に「データを消す」→「すべて消す」: 記録も消える。"""
    uid = line_user(RAW_USER)
    model = ConsultModel(script=[*start_script(uid), reply(["アルバイト・パートで探しましょう"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=career_llm()))
    with TestClient(server.app) as client:
        _say(client, "正社員で転職したいので相談したい", 1)
        _say(client, CASUAL, 2)
        rec = server.state.user(uid).get(SW.KEY)
        assert rec and rec["said"] == CASUAL and rec["acts"][0]["text"] == "週3くらい"
        data = _ask(client, fake, 3)
        assert SW.KEY in server.state.user(uid)  # 確かめるまでは消さない
        _tap(client, data, 4)
        assert SW.KEY not in server.state.user(uid)
    assert SW.KEY not in State(tmp_path / "state.json").user(uid)


def test_web_forget_all_also_erases_the_handoff(app, monkeypatch, v4_destination):  # noqa: F811
    """Web のトークで同じく移ると言い、プロフィール画面の「すべて消す」: 記録も消える。"""
    from line_runtime import runtime

    model = ConsultModel(script=[greet, *start_script(PINNED_USER), reply(["アルバイト・パートで探しましょう"])])
    monkeypatch.setattr(app, "LineRuntime", lambda: runtime.LineRuntime(model=model, in_memory=True, career_llm=career_llm()))
    with TestClient(app.app) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        stream(client, uid, "message", th, "正社員で転職したいので相談したい")
        assert career_session.active(app.state, uid)
        stream(client, uid, "message", th, CASUAL)
        assert app.state.user(uid).get(SW.KEY, {}).get("said") == CASUAL
        assert client.post("/api/profile/forget").status_code == 200
        assert SW.KEY not in app.state.user(uid)


def test_runtime_direct_switch_from_v5_to_c2_reads_the_written_handoff(tmp_path, monkeypatch):
    """ランタイム(本物、dip の MCP も本物)で、v5 の相談の途中の「やっぱり正社員で探したい」: トークの AI も読む段も呼ばずに、同じターンで
    正社員の c2 が始まる(言う段 1 回)。v5 が書いた記録の勤務地(近くで探すのエリア)は聞き直さない。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    st = State(tmp_path / "state.json")
    session.start(st, UID, quick=False)
    h = harness(st)
    h.near["area"] = "東京"
    session._save(st, UID, h)
    logic.set_search_track(st, UID, "アルバイト・パート", evidence="バイトを探したい")
    model = ConsultModel(script=[reply(["(トークの AI は呼ばれない)"])])
    llm = FakeReadLLM()
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=model, in_memory=True, career_llm=llm, baito_llm=FakeBaitoLLM()) as rt:
            return await rt.handle(UID, "message", "やっぱり正社員で探したい")

    out = asyncio.run(go())
    st = State(tmp_path / "state.json")
    c = career_session._load(st, UID)
    assert c is not None and c.current["qid"] == "q_segment" and c.slots["area"]["text"] == "東京"
    assert model.script and [stage for stage, _, _ in llm.calls] == ["say"]
    assert out["track_switch"]["track"] == "正社員" and not session.active(st, UID) and SW.KEY not in st.user(UID)


# ----------------------------- 敵対的なレビューで見つけたこと -----------------------------
def test_asking_for_jobs_still_leaves_to_the_talk_ai_and_does_not_switch_to_career(tmp_path):
    """「求人を見せて」は v4 と同じく相談の外へ(トークの AI が求人を出す)。正社員へは移らず、記録も書かない。"""
    st = State(tmp_path / "state.json")
    session.start(st, UID, quick=False)
    for said in ("求人を見せて", "バイトの求人を見たい"):
        assert V5.asked_to_switch(harness(st), said) is None, said
    r = session.turn(st, UID, "求人を見せて")
    assert (r["active"], r["ended"]) == (False, "exit") and SW.KEY not in st.user(UID)


def test_a_stopped_junior_high_student_can_still_leave_with_stop_words(tmp_path):
    """中学生以下で止めた後(結果の後も相談を閉じない)も、「やめる」で閉じられる(モデルを呼ばない)。止めた印は残る。"""
    st = State(tmp_path / "state.json")
    session.start(st, UID, quick=False)
    while harness(st).current["qid"] != "hf_age":
        run(st, harness(st).current["choices"][0])
    r, _ = run(st, "中学生以下")
    assert r.get("blocked") == "under_15" and session.active(st, UID)
    r, stages = run(st, "やめる")
    assert stages == [] and r["closed"] == "exit" and not session.active(st, UID)
    assert st.user(UID).get(session.BLOCK_KEY)


def test_saved_or_carried_late_night_is_not_dropped_silently(tmp_path):
    """前の相談の「夕方、深夜」は、年齢が分からないうちは入れない(深夜を黙って落とした値を確かめた値にしない)。時間帯は年齢の後に聞く。"""
    st = State(tmp_path / "state.json")
    logic.save_preferences(st, UID, {"time_band": "夕方、深夜", "days_per_week": "週3〜4日"})
    session.start(st, UID, quick=False)
    h = harness(st)
    assert "time_slots" not in h.slots and h.slots["days_per_week"]["value"] == "3-4" and h.notices_sent == []
    rec = SW.record("career", "baito", {}, [{"act": "add", "slot": "work_hours", "text": "夕方と深夜"}], "", now(UID))
    h2 = V5.new_state()
    V5.take_handoff(h2, rec)
    assert "time_slots" not in h2.slots


def test_retracting_the_age_does_not_lift_the_stop_and_the_handoff_has_no_age(tmp_path):
    st = at("sc_line", age="under_15")
    assert st.blocked == "under_15"
    said = "さっきの年齢はなしで"
    V5.turn(st, said, H.BANK, LLM({said: [act("retract", slot="age_band")]}))
    assert st.slots["age_band"]["value"] == "under_15" and st.blocked == "under_15"
    st = State(tmp_path / "state.json")
    session.start(st, UID, quick=False)
    said = "17歳だけど、とりあえず正社員で探したいかも"
    r, _ = run(st, said, script({said: [act("add", slot="age_band", text="17歳", value="15_17"),
                                        act("switch", to="career", strength="want")]}))
    assert r["switch_to"] == "career" and all(a.get("slot") != "age_band" for a in st.user(UID)[SW.KEY]["acts"])


# ----------------------------- 「あとで」の後に本人が消した値(jyoka/tasks#374) -----------------------------
class LayerBaitoLLM:
    """ランタイムの v5 の LLM。読む段は何も読まない(ボタンと決まった言葉だけで進める)。言う段、まとめ、結果は場面の一覧と同じ台本。"""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, stage, system, messages):
        self.calls.append(stage)
        if stage == "read":
            return {"acts": [], "hook": None, "mentions": [], "ng": []}, USAGE
        return R.BaitoV5().write({"stage": stage, "system": system}, "OK！"), USAGE


NG_PICK = "コールセンター"  # rp_student で選ぶと、本人の NG「電話対応」が入る
NG = "電話対応"


def _tap_for(h: V5.State) -> str:
    """利用者が押すボタン: 入口のカードは「どれもピンと来ない」、年齢は 18 歳以上、学生、逆から選ぶ問いは NG_PICK、まとめは「これでOK」。"""
    cur = h.current
    if cur["mode"] == "review":
        return H.REVIEW_OK
    pick = {H.QUICK_PICK: "どれもピンと来ない", "near_age": "18歳以上", "hf_age": "18歳以上", "q_segment": "学生",
            "rp_student": NG_PICK}
    return pick.get(cur["qid"], cur["choices"][0])


def _web_say(client, st, uid, th, text) -> str:
    """Web のトーク画面で 1 つ送り、届いた返信の文(吹き出しをつないだもの)を返す。"""
    evs = stream(client, uid, "message", th, text)
    return "\n".join(str(e.get("text") or "") for e in evs) + json.dumps(evs, ensure_ascii=False)


def test_web_profile_deleting_the_ng_after_later_keeps_it_deleted_in_the_continued_result(app, monkeypatch):  # noqa: F811
    """Web のトーク画面で v5 の相談を始め、NG(「電話対応」)を答えて「あとで」で取っておく。プロフィール画面で NG と勤務地を消し、
    トークで相談を続けて終える: まとめにも保存にも、消した NG と勤務地が戻らない(jyoka/tasks#374)。"""
    from line_runtime import runtime

    model = ConsultModel(script=[greet, *baito_start_script(),
                                 call("start_baito_consultation", {"line_user_id": PINNED_USER}, "st2"),
                                 reply(["続きからいこう！"])])
    baito = LayerBaitoLLM()
    monkeypatch.setattr(app, "LineRuntime", lambda: runtime.LineRuntime(model=model, in_memory=True, baito_llm=baito))
    with TestClient(app.app) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        st = app.state
        stream(client, uid, "message", th, "バイトを探したい")
        while harness(st, uid).current["qid"] != "rp_student":
            _web_say(client, st, uid, th, _tap_for(harness(st, uid)))
        _web_say(client, st, uid, th, NG_PICK)
        assert harness(st, uid).user_ng == [NG]
        _web_say(client, st, uid, th, "あとで")
        assert not session.active(st, uid) and session.PAUSED_KEY in st.user(uid)
        prefs = logic.stated_preferences(st.user(uid))
        assert (prefs["avoids"]["value"], prefs["area"]["value"]) == (NG, "東京")
        for key in ("avoids", "area"):  # プロフィール画面の「消す」
            assert client.delete(f"/api/profile/items/{key}").status_code == 200
        stream(client, uid, "message", th, "バイトの相談の続きをしたい")
        h = harness(st, uid)
        assert h.current["qid"] == "fg_wage" and NG not in h.user_ng and not h.near.get("area")
        seen = []
        while session.active(st, uid):
            seen.append(_web_say(client, st, uid, th, _tap_for(harness(st, uid))))
    assert model.script == [] and baito.calls
    assert seen and all(NG not in s for s in seen)  # まとめのカードの NG にも、結果にも出ない
    prefs = logic.stated_preferences(app.state.user(uid))
    assert "avoids" not in prefs and "area" not in prefs and prefs["days_per_week"]["value"] == "週1〜2日"


def test_line_forget_data_after_later_erases_the_paused_consultation_and_the_ng(tmp_path, monkeypatch):
    """LINE で v5 の相談を始め、NG を答えて「あとで」。「データを消す」→「すべて消す」の後に始め直すと、取っておいた相談の続きでなく
    最初の入口のカードから始まり、消した NG も入っていない(jyoka/tasks#374)。"""
    uid = line_user(RAW_USER)
    model = ConsultModel(script=[*baito_start_script(), *baito_start_script()])
    baito = LayerBaitoLLM()
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, baito_llm=baito))
    with TestClient(server.app) as client:
        st, n = server.state, 1
        _say(client, "バイトを探したい", n)
        while harness(st, uid).current["qid"] != "rp_student":
            n += 1
            _say(client, _tap_for(harness(st, uid)), n)
        _say(client, NG_PICK, n + 1)
        _say(client, "あとで", n + 2)
        assert session.PAUSED_KEY in st.user(uid) and logic.stated_preferences(st.user(uid))["avoids"]["value"] == NG
        _tap(client, _ask(client, fake, n + 3), n + 4)
        assert session.PAUSED_KEY not in st.user(uid) and not logic.stated_preferences(st.user(uid))
        _say(client, "バイトを探したい", n + 5)
        h = harness(st, uid)
        assert h.current["qid"] == H.QUICK_PICK and h.user_ng == [] and h.asked == []
    assert session.PAUSED_KEY not in State(tmp_path / "state.json").user(uid)


def _later_with_ng(st):
    """NG_PICK で本人の NG を答えた後に「あとで」で取っておく(保存の避けたいことは NG)。"""
    _to_hf_time(st)
    while harness(st).current["qid"] != "rp_student":
        run(st, harness(st).current["choices"][0])
    run(st, NG_PICK)
    r, _ = run(st, "あとで")
    assert r["closed"] == "later" and logic.stated_preferences(st.user(UID))["avoids"]["value"] == NG


def test_ng_deleted_during_the_continued_consultation_is_not_written_back(tmp_path):
    """「あとで」の続きを始めた後(相談の途中)にプロフィール画面で NG を消しても、閉じるときに書き戻さない。続きで新しく話した NG は足す。"""
    from dip_mcp import profile

    st = State(tmp_path / "state.json")
    _later_with_ng(st)
    session.start(st, UID, quick=False)
    assert harness(st).user_ng == [NG] and harness(st).saved_ng == [NG]
    profile.delete_item(st, UID, "avoids")
    said = "夜勤は無理"

    def scr(c):  # 読む段が本人の NG「夜勤」を読む
        return script()(c) | ({"ng": ["夜勤"]} if c["stage"] == "read" else {})

    run(st, said, scr)
    assert harness(st).user_ng == [NG, "夜勤"]
    run(st, "やめる")
    assert logic.stated_preferences(st.user(UID))["avoids"]["value"] == "夜勤"


def test_ng_edited_on_the_profile_page_while_paused_keeps_only_what_is_left(tmp_path):
    """「あとで」の後に避けたいことを直したら、続きは今の文に残った NG だけを持つ。残っていれば、そのまま書き戻さない(文を変えない)。"""
    from dip_mcp import profile

    st = State(tmp_path / "state.json")
    _later_with_ng(st)
    profile.edit_item(st, UID, "avoids", f"力仕事、{NG}")
    session.start(st, UID, quick=False)
    assert harness(st).user_ng == [NG]
    run(st, "やめる")
    assert logic.stated_preferences(st.user(UID))["avoids"]["value"] == f"力仕事、{NG}"
    st2 = State(tmp_path / "state2.json")
    _later_with_ng(st2)
    profile.edit_item(st2, UID, "avoids", "力仕事")
    session.start(st2, UID, quick=False)
    assert harness(st2).user_ng == [] and harness(st2).saved_ng == []
    run(st2, "やめる")
    assert logic.stated_preferences(st2.user(UID))["avoids"]["value"] == "力仕事"


def test_ng_deleted_during_the_continued_consultation_stays_deleted_after_later_again(tmp_path):
    """続きの途中にプロフィール画面で NG と日数を消し、もう一度「あとで」で取っておく。次の続きにも、閉じた後の保存にも、消した NG と
    日数が戻らない(jyoka/tasks#374)。"""
    from dip_mcp import profile

    st = State(tmp_path / "state.json")
    _later_with_ng(st)
    session.start(st, UID, quick=False)
    for key in ("avoids", "days_per_week"):
        profile.delete_item(st, UID, key)
    r, _ = run(st, "あとで")
    assert r["closed"] == "later" and not {"avoids", "days_per_week"} & set(logic.stated_preferences(st.user(UID)))
    session.start(st, UID, quick=False)
    h = harness(st)
    assert NG not in h.user_ng and h.saved_ng == [] and "days_per_week" not in h.slots
    run(st, "やめる")
    assert not {"avoids", "days_per_week"} & set(logic.stated_preferences(st.user(UID)))


def test_ng_edited_during_the_continued_consultation_is_not_added_back_with_a_new_ng(tmp_path):
    """続きの途中に避けたいことを直し(「電話対応」を「力仕事」に)、同じ続きで新しい NG を話して閉じる: 直した文に新しい NG を足し、
    外した NG は戻さない(jyoka/tasks#374)。"""
    from dip_mcp import profile

    st = State(tmp_path / "state.json")
    _later_with_ng(st)
    session.start(st, UID, quick=False)
    profile.edit_item(st, UID, "avoids", "力仕事")

    def scr(c):  # 読む段が本人の NG「夜勤」を読む
        return script()(c) | ({"ng": ["夜勤"]} if c["stage"] == "read" else {})

    run(st, "夜勤は無理", scr)
    run(st, "やめる")
    assert logic.stated_preferences(st.user(UID))["avoids"]["value"] == "夜勤、力仕事"


# ----------------------------- 本人が消した値を、取っておいた相談からもその時点で消す(jyoka/tasks#375) -----------------------------
WAGE = "1,200円以上"
ITEMS = ("avoids", "area", "days_per_week", "time_band", "min_wage")  # 消す項目: NG、勤務地、日数、時間帯、時給


def _web_later_with_items(client, st, uid, th) -> dict:
    """Web のトーク画面で v5 の相談を始め、NG(逆から選ぶ問いの NG_PICK)と時給(WAGE)まで答えて「あとで」で取っておく。保存した希望の項目。"""
    stream(client, uid, "message", th, "バイトを探したい")
    while harness(st, uid).current["qid"] != "fg_wage":
        _web_say(client, st, uid, th, _tap_for(harness(st, uid)))
    _web_say(client, st, uid, th, WAGE)
    _web_say(client, st, uid, th, "あとで")
    assert not session.active(st, uid) and session.PAUSED_KEY in st.user(uid)
    return logic.stated_preferences(st.user(uid))


def _stored(client, uid) -> str:
    """「データを書き出す」の dip の状態(保存しているそのままの形)を、文字にしたもの。"""
    return json.dumps(client.get("/api/profile/export").json()["stored"]["users"][uid], ensure_ascii=False)


def test_web_profile_deleting_items_after_later_erases_them_from_the_paused_consultation(app, monkeypatch):  # noqa: F811
    """Web のトーク画面で v5 の相談の NG、勤務地、日数、時間帯、時給を答えて「あとで」。プロフィール画面でそれを消した直後に、「データを書き出す」と
    保存のファイル(取っておいた相談を含む)のどこにも、消した値が無い。続きの後の結果にも、消した NG と勤務地が戻らない(jyoka/tasks#375)。"""
    from line_runtime import runtime

    model = ConsultModel(script=[greet, *baito_start_script(),
                                 call("start_baito_consultation", {"line_user_id": PINNED_USER}, "st2"),
                                 reply(["続きからいこう！"])])
    baito = LayerBaitoLLM()
    monkeypatch.setattr(app, "LineRuntime", lambda: runtime.LineRuntime(model=model, in_memory=True, baito_llm=baito))
    with TestClient(app.app) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        st = app.state
        prefs = _web_later_with_items(client, st, uid, th)
        values = [prefs[k]["value"] for k in ITEMS]
        assert values == [NG, "東京", "週1〜2日", "朝", "1200"]
        before = _stored(client, uid)
        assert all(v in before for v in values)  # 消す前は、取っておいた相談にも値がある
        for key in ITEMS:  # プロフィール画面の「消す」
            assert client.delete(f"/api/profile/items/{key}").status_code == 200
        after = _stored(client, uid)
        p = st.user(uid)[session.PAUSED_KEY]
        on_disk = json.dumps(State(app.state.path).user(uid), ensure_ascii=False)
        assert all(v not in after and v not in on_disk for v in values)
        h = p["harness"]
        assert NG not in h["user_ng"] + h["saved_ng"] and "area" not in h["near"] and "time" not in h["near"]
        assert not {"days_per_week", "time_slots", "hourly_wage_min"} & set(h["slots"])
        assert all(v is None for v in p["kept"].values()) and h["kept"] == p["kept"]
        assert all(e.get("slot") not in ("days_per_week", "time_slots", "hourly_wage_min") or not {"value", "values"} & set(e)
                   for e in h["log"])
        qid = h["current"]["qid"]
        stream(client, uid, "message", th, "バイトの相談の続きをしたい")
        assert harness(st, uid).current["qid"] == qid and NG not in harness(st, uid).user_ng  # 続きは閉じたときの問いから
        seen = []
        while session.active(st, uid):
            seen.append(_web_say(client, st, uid, th, _tap_for(harness(st, uid))))
    assert model.script == [] and seen and all(NG not in s for s in seen)
    prefs = logic.stated_preferences(app.state.user(uid))
    assert "avoids" not in prefs and "area" not in prefs


def test_web_forget_all_after_later_erases_the_paused_consultation(app, monkeypatch):  # noqa: F811
    """Web のトーク画面で NG を答えて「あとで」。プロフィール画面の「すべて消す」で、取っておいた相談そのものが消える(jyoka/tasks#375)。"""
    from line_runtime import runtime

    model = ConsultModel(script=[greet, *baito_start_script()])
    baito = LayerBaitoLLM()
    monkeypatch.setattr(app, "LineRuntime", lambda: runtime.LineRuntime(model=model, in_memory=True, baito_llm=baito))
    with TestClient(app.app) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        _web_later_with_items(client, app.state, uid, th)
        assert client.post("/api/profile/forget").status_code == 200
        assert session.PAUSED_KEY not in app.state.user(uid) and NG not in json.dumps(app.state.user(uid), ensure_ascii=False)
    assert session.PAUSED_KEY not in State(app.state.path).user(uid)


def test_editing_an_item_after_later_replaces_the_old_value_in_the_paused_consultation(tmp_path, monkeypatch):
    """「あとで」の後にプロフィール画面で日数を直し、トーク(トークの AI の save_user_preferences)で避けたいことを言い直す: 取っておいた相談は
    その時点で前の値(週3〜4日、電話対応)を持たず、直した日数を入れている。続きの結果でも直した値のまま(jyoka/tasks#375)。"""
    from dip_mcp import profile
    from dip_mcp import server as dip_server

    st = State(tmp_path / "state.json")
    _later_with_ng(st)
    profile.edit_item(st, UID, "days_per_week", "週1〜2日")
    p = st.user(UID)[session.PAUSED_KEY]
    assert p["harness"]["slots"]["days_per_week"] == {"value": "1-2", "confidence": "high", "from": V5.SAVED}
    assert p["kept"]["days_per_week"] == "週1〜2日" and "週3〜4日" not in json.dumps(p, ensure_ascii=False)
    monkeypatch.setattr(dip_server, "state", st)
    assert dip_server.save_user_preferences(UID, {"avoids": "力仕事"})["saved"] == ["avoids"]
    p = st.user(UID)[session.PAUSED_KEY]
    assert NG not in json.dumps(p, ensure_ascii=False) and p["kept"]["avoids"] == "力仕事"
    session.start(st, UID, quick=False)
    run(st, "やめる")
    prefs = logic.stated_preferences(st.user(UID))
    assert (prefs["days_per_week"]["value"], prefs["avoids"]["value"]) == ("週1〜2日", "力仕事")


def test_unrelated_profile_change_keeps_the_paused_consultation_as_is(tmp_path):
    """取っておいた相談が読まない項目(やりたいこと)を直しても、取っておいた相談は変えない(直前のやりとりも残す)。"""
    from dip_mcp import profile

    st = State(tmp_path / "state.json")
    _later_with_ng(st)
    before = json.dumps(st.user(UID)[session.PAUSED_KEY], ensure_ascii=False)
    profile.edit_item(st, UID, "wants", "人と話す仕事")
    profile.delete_item(st, UID, "wants")
    assert json.dumps(st.user(UID)[session.PAUSED_KEY], ensure_ascii=False) == before


def test_profile_change_rebuilds_the_review_card_and_drops_an_unreadable_paused_consultation(tmp_path):
    """まとめのカードを出したまま取っておいた相談は、NG を消すとカードを作り直す(消した NG を載せない)。取っておいた状態が読めなければ、
    「消す」は失敗させず、取っておいた相談ごと消す(jyoka/tasks#375)。"""
    from dip_mcp import profile

    st = State(tmp_path / "state.json")
    _later_with_ng(st)

    def put_card(u):
        h = V5.State.from_dict(u[session.PAUSED_KEY]["harness"])
        h.last_card = H.build_card(h)
        u[session.PAUSED_KEY]["harness"] = h.to_dict()

    st.update(UID, put_card)
    assert st.user(UID)[session.PAUSED_KEY]["harness"]["last_card"]["ng_user"] == [NG]
    profile.delete_item(st, UID, "avoids")
    p = st.user(UID)[session.PAUSED_KEY]
    assert p["harness"]["last_card"]["ng_user"] == [] and NG not in json.dumps(p, ensure_ascii=False)
    st.update(UID, lambda u: u[session.PAUSED_KEY]["harness"].__setitem__("slots", "読めない"))
    assert profile.delete_item(st, UID, "days_per_week") == {"deleted": "days_per_week"}
    assert session.PAUSED_KEY not in st.user(UID) and "days_per_week" not in st.user(UID)["preferences"]


def test_profile_change_drops_handed_over_values_and_plan_texts_from_the_paused_consultation(tmp_path):
    """取っておいた相談が正社員から引き継いだ値(came_from。近くで探すのエリアに無い「横浜」、取っておいた相談が読まない働き方)と、
    計画の記録の文(「週の日数を…に直したよ」)も、本人が項目を消した時点で消す(jyoka/tasks#375)。"""
    from dip_mcp import profile

    st = State(tmp_path / "state.json")
    _later_with_ng(st)
    came = {"from": "career", "to": "baito", "slots": {"area": {"text": "横浜", "confidence": "high"},
                                                         "work_style": {"text": "リモート中心", "confidence": "high"}}, "acts": []}

    def put(u):
        h = u[session.PAUSED_KEY]["harness"]
        h["came_from"] = came
        h["log"].append({"t": 9, "kind": "plan", "ack": "short", "about": ["週の日数を「週3〜4日」に直したよ"]})

    st.update(UID, put)
    logic.save_preferences(st, UID, {"area": "横浜", "work_style": "リモート中心"})
    profile.delete_item(st, UID, "work_style")
    h = st.user(UID)[session.PAUSED_KEY]["harness"]
    assert h["came_from"] is None and "リモート中心" not in json.dumps(h, ensure_ascii=False)
    profile.delete_item(st, UID, "days_per_week")
    h = st.user(UID)[session.PAUSED_KEY]["harness"]
    assert "週3〜4日" not in json.dumps(h, ensure_ascii=False) and {"kind": "plan", "t": 9, "ack": "short"} in h["log"]
