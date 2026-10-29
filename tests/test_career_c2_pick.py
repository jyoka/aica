"""正社員ヒアリングの c2 の、問いの選び方と候補(設計メモ docs/design/conversation-layer.md §4.7、§5 の 5 枚目。task 358)。

実 LLM なし。偽の LLM と台本の LLM で確かめる。
- 確かめる場面(evals/scenes/)の条件が c2 で通る。モックの回帰(evals.career_sim の 6 人、evals.career_choices)が c1 より悪くならない
- 群とスコア: フェーズの上限をやめ、全部の問いから選ぶ。話題のおまけは同じ優先度の問いの間だけで効く。区切りの 1 行は群が変わったとき
- ボタンのほかの欄への効き目は、空の欄にだけ推測(low)で入れ、ラベルは問いの欄の値になる
- 候補の決まり: どの候補も問いの欄の値になる。数の問いに、分からない基準と比べる候補(「今と同じくらい」)を置かない
- 本人の言葉の候補: 本人が言った言葉を最初のページの頭に 1 つ。ボタンの字数の決まりを守る。押せば問いの欄の値になる
"""

from __future__ import annotations

import json

import pytest

from dip_mcp.career import c2 as C2
from dip_mcp.career import harness as H
from dip_mcp.convo import entry as E
from dip_mcp.convo import pick as P
from dip_mcp.convo import state as CS
from tests.test_convo import CAREER_ENTRY

CARD5_SCENES = ("multi-infer-raise-target", "multi-career-in-why-now", "t326-salary-reask", "t326-more-lead", "flow-more-typed",
                "short-ok-ambiguous")
LATER_CARD = {("t326-salary-reask", "ack_varies")}  # 相づちの言い回しを散らすのは 4 枚目のカード(§4.8)
RELATIVE = ("今と同じ", "今より")  # 数の問いの候補に置かない、本人の今の値と比べる言い方(本人の今の値は聞いていない)


class Script:
    """読む段は台本の act を順に返す(尽きたら act なし)。言う段は問いの文をそのまま返す。"""

    def __init__(self, *readings):
        self.readings = list(readings)
        self.calls: list[str] = []

    def read(self, payload):
        self.calls.append("read")
        r = self.readings.pop(0) if self.readings else {"acts": []}
        return json.dumps({"acts": [], "hook": None, "mentions": [], "ng": []} | r, ensure_ascii=False)

    def generate(self, view):
        self.calls.append("generate")
        return json.dumps({"reply": view["text"]}, ensure_ascii=False)


def asking(qid, segment="same_path", form="C", known=(), **state):
    """qid を出したところの c2 の状態。known の欄は確かめた値で埋め、その問いは出したことにする。"""
    st = C2.new_state(form=form)
    st.segment = segment
    for slot in known:
        st.slots[slot] = H.entry("filled", f"{slot}の値")
    qs = H.questions(st, H.BANK)
    st.asked = [q["id"] for q in qs if set(q["targets"]) & set(known)]
    for k, v in state.items():
        setattr(st, k, v)
    q = H.qmap(st, H.BANK)[qid]
    st.phase = q["phase"]
    C2._present(st, q)
    return st


HARD_SAME = ("why_now", "area", "min_annual_salary", "start_timing", "raise_target")
CARDS = tuple(H.card_key(n, f) for n in (1, 2) for f in H.CARD_FIELDS)


# ----------------------------- 場面の一覧とモックの回帰 -----------------------------
def test_c2_passes_the_card_5_scenes():
    from evals.scenes import checks as K
    from evals.scenes import runner as R

    rows = {r["scene"]: r for r in R.run(["career"], {"career": "c2"}, list(CARD5_SCENES))}
    assert set(rows) == set(CARD5_SCENES)
    bad = {(sid, x["check"]) for sid, r in rows.items() for x in r["results"] if not x.get("judge") and x["status"] != K.PASS}
    assert bad == LATER_CARD
    # c1 は二択の「どっちでも」で同じ欄の二択を続ける(§2.6 の 8)。c2 は「こだわりなし」にして続けない
    c1 = R.run(["career"], {"career": "c1"}, ["short-ok-ambiguous"])[0]
    assert {x["check"] for x in c1["results"] if x["status"] == K.FAIL} == {"slot", "not_asks"}


def test_mock_regression_on_c2_is_not_worse_than_c1():
    """evals.career_sim の 6 人 × 形 B〜D: c2 も関門がすべて 0。完了率は下がらず、正しく取れた事実は 0.3 項目以上減らない(§4.10)。"""
    from evals import career_sim

    c1 = {(r["form"], r["persona"]): r["metrics"] for r in career_sim.run_all()}
    c2 = {(r["form"], r["persona"]): r["metrics"] for r in career_sim.run_all(version="c2")}
    assert set(c1) == set(c2) and len(c2) == 18
    assert [(k, career_sim.failures(m)) for k, m in c2.items() if career_sim.failures(m)] == []

    def facts(rows):
        return sum(int(m["facts"].split("/")[0]) for m in rows.values()) / len(rows)

    assert sum(m["completed"] for m in c2.values()) >= sum(m["completed"] for m in c1.values())
    assert facts(c2) >= facts(c1) - 0.3
    assert all(m["llm_calls"].get("extract", 0) == 0 for m in c2.values())  # c2 は読む段(read)で読む


@pytest.mark.parametrize("name", ["candidates", "skip_end", "more", "vague", "clarify", "pdm", "short_tempered"])
def test_career_choices_pass_on_c2(name):
    from evals import career_choices

    assert career_choices.CASES[name]("c2") == []


# ----------------------------- 群とスコア -----------------------------
def test_c2_picks_from_every_question_without_phase_caps():
    """二択を 2 つ聞いた後: c1 はフェーズの上限(2)で逆選択へ移る。c2 は上限を見ず、同じ優先度(LOW)の問いを群の中のバンクの順で選ぶ。"""
    known = (*HARD_SAME, *CARDS, "work_style", "direction")
    st = asking("sc_monday", known=known, turns=12, phase_turns={"scenario_game": 2})
    st.asked += ["sc_wednesday"]
    c1 = H.State.from_dict(st.to_dict() | {"version": "c1"})
    H.advance(c1, H.BANK)
    assert c1.current["qid"] == "rp_general"
    C2.advance(st, H.BANK)
    assert st.current["qid"] == "sc_big_task"


def test_topic_bonus_breaks_ties_only_within_the_same_priority():
    """ブランクから戻る人が勤務地の問いに「リモートがいい」とも言った: 働き方の話題の HARD(働ける時間)を、バンクの順で先の年収より先に聞く。
    MID の働き方の問いは、HARD が残っているうちは、おまけが付いても出さない。"""
    st = asking("hf_area", segment="return", known=("why_now",))
    out = C2.turn(st, "大阪で、リモートがいいな", H.BANK, Script(
        {"acts": [{"act": "answer", "slot": "area", "text": "大阪"}, {"act": "add", "slot": "work_style", "text": "リモートがいい"}]}))
    assert out["qid"] == "hf_work_hours"
    st = asking("hf_area", segment="return", known=("why_now",))
    C2.turn(st, "大阪", H.BANK, Script({"acts": [{"act": "answer", "slot": "area", "text": "大阪"}]}))
    assert st.current["qid"] == "hf_salary"  # 話題が無ければバンクの順
    st = asking("hf_area", known=("why_now", "area"))
    st._topics = {"働き方"}
    assert C2.pick_question(st, H.BANK)["id"] == "hf_salary"  # 働き方の MID より、おまけの無い HARD
    assert P.bonus(["work_style"], {"働き方"}, C2.ENTRY.topics) == P.TOPIC_BONUS < H.HARD - H.CARD1


def test_phase_line_comes_when_the_group_changes():
    """区切りの 1 行は群が変わったとき(入り口の定義の line)。好みの群の中(二択 → 逆選択)では出さない。"""
    st = asking("card_result2", known=(*HARD_SAME, *CARDS[:5]), turns=10)
    out = C2.turn(st, "売上が伸びた", H.BANK, Script())
    assert out["qid"] == "sc_wednesday" and out["bubbles"][0].split("\n")[:2] == ["✓職歴2「売上が伸びた」3/3", "あと少しで完成です"]
    assert "次は二択の質問です" not in out["reply"]
    st = asking("sc_big_task", known=(*HARD_SAME, *CARDS, "work_style", "direction"), turns=10)
    out = C2.turn(st, "チームで分担して進める", H.BANK, Script())
    assert out["qid"] == "rp_general" and not any(line in out["reply"] for line in H.PHASE_LINES.values())


def test_scenario_pass_is_no_preference_and_the_same_slot_is_not_asked_again():
    st = asking("sc_wednesday", known=(*HARD_SAME, *CARDS), turns=10)
    out = C2.turn(st, "どっちでもいい", H.BANK, Script({"acts": [{"act": "pass"}]}))
    e = st.slots["work_style"]
    assert (e["status"], e["value"], e["confidence"]) == ("passed", H.NOPREF, "low")
    assert out["qid"] == "sc_monday" and "sc_deadline" not in st.asked
    st = asking("sc_wednesday", known=(*HARD_SAME, *CARDS), turns=10)
    C2.turn(st, H.PASS, H.BANK, Script())  # ボタンのスキップは c1 のまま(欄を空にし、ほかの問いで聞ける)
    assert "work_style" not in st.slots


# ----------------------------- ほかの欄への効き目 -----------------------------
def test_button_infers_another_slot_as_a_guess_only_when_it_is_empty():
    st = asking("ib_why_now")
    C2.turn(st, "年収を上げたい", H.BANK, Script())
    assert (st.slots["why_now"]["confidence"], st.slots["raise_target"]["confidence"]) == ("high", "low")
    st = asking("ib_why_now")
    st.slots["raise_target"] = H.entry("filled", "役職")
    C2.turn(st, "年収を上げたい", H.BANK, Script())
    assert st.slots["raise_target"]["text"] == "役職"


def test_choice_effect_on_another_slot_is_a_guess_and_the_label_still_answers():
    """質問バンクの choice_effects がほかの欄に効くとき(§4.4): c1 は問いのフェーズの確かさ(high)で入れ、ラベルを問いの欄に入れない。
    c2 はほかの欄を推測(low)で入れ、ラベルを問いの欄の値にする。"""
    bank = [dict(q, choice_effects={"働き方": {"work_style": {"value": ["remote"]}}}) if q["id"] == "ib_why_now" else q
            for q in H.BANK]
    st = asking("ib_why_now")
    C2.turn(st, "働き方", bank, Script())
    assert (st.slots["why_now"]["text"], st.slots["why_now"]["confidence"]) == ("働き方", "high")
    assert (st.slots["work_style"]["value"], st.slots["work_style"]["confidence"]) == (["remote"], "low")
    c1 = H.new_state()
    c1.segment = "same_path"
    H._present(c1, H.qmap(c1, bank)["ib_why_now"])
    H.turn(c1, "働き方", bank, None)
    assert "why_now" not in c1.slots and c1.slots["work_style"]["confidence"] == "high"


# ----------------------------- 候補の決まり -----------------------------
def every_candidate():
    for seg in (*H.SEGMENTS, "unknown"):
        st = C2.new_state(form="C")
        st.segment = seg
        for q in H.questions(st, H.BANK):
            for label in q["choices"]:
                yield seg, q, label


def test_every_candidate_becomes_a_value_of_its_question():
    """どの候補も、押せば問いの欄の値になる(値にならない候補を置かない。§4.7)。区分の問いは区分が、逆選択は避けたいことが決まる。"""
    bad = []
    for seg, q, label in every_candidate():
        st = C2.new_state(form="C")
        st.segment = "unknown" if q["id"] == "q_segment" else seg
        C2._apply_choice(st, q, label)
        t = q["targets"][0]
        got = st.segment != "unknown" if t == "segment" else label in st.user_ng if t == "__ng__" else \
            (st.slots.get(t) or st.pending.get(t) or {}).get("status") == "filled"
        if not got:
            bad.append(f"{seg}/{q['id']}/{label}")
    assert bad == []


def test_number_candidates_do_not_compare_with_an_unknown_value():
    """数の問いの候補に、本人の今の値と比べる言い方(「今と同じくらい」「今より上げたい」)を置かない(#326 の C)。区切りは数の範囲を持つ。"""
    bad = []
    for seg, q, label in every_candidate():
        if q["kind"] != "number":
            continue
        eff = q["choice_effects"].get(label, {}).get(q["targets"][0], {})
        if any(w in label for w in RELATIVE) or H.digits_in(label) and not (eff.get("range") or eff.get("nopref")):
            bad.append(f"{seg}/{q['id']}/{label}")
    assert bad == []


# ----------------------------- 本人の言葉の候補 -----------------------------
def test_word_the_person_said_heads_the_first_page_and_answers_when_pressed():
    st = C2.new_state(form="C")
    st.segment = "change"
    CS.mentioned(st.convo, ["法人営業"])
    q = H.qmap(st, H.BANK)["hf_use_experience"]
    st.phase = q["phase"]
    C2._present(st, q)
    ch = st.current["choices"]
    assert ch[0] == "法人営業" and ch[1:5] == q["pages"][0] and sum(H.chars(c) for c in ch) <= H.LABELS_MAX
    out = C2.turn(st, "法人営業", H.BANK, Script())
    e = st.slots["use_experience"]
    assert (e["text"], e["confidence"], e["from"]) == ("法人営業", "high", "button:hf_use_experience")
    assert out["qid"] != "hf_use_experience"


def test_word_that_does_not_fit_replaces_the_last_candidate_and_other_questions_get_none():
    st = C2.new_state(form="C")
    st.segment = "early"
    CS.mentioned(st.convo, ["店長代理としての経験"])  # 10 字: 続けたいことの最初のページに足すと 40 字を超える
    q = H.qmap(st, H.BANK)["hf_keep"]
    C2._present(st, q)
    ch = st.current["choices"]
    assert ch[0] == "店長代理としての経験" and q["pages"][0][-1] not in ch
    assert sum(H.chars(c) for c in ch) <= H.LABELS_MAX and len(ch) <= H.QUICK_MAX
    C2._present(st, H.qmap(st, H.BANK)["hf_stop"])  # やめたいことの問いには足さない
    assert "店長代理としての経験" not in st.current["choices"]


def test_word_already_used_for_another_job_is_not_offered_again():
    st = C2.new_state(form="C")
    st.segment = "same_path"
    st.slots["career1.role"] = H.entry("filled", "法人営業", confidence="low")
    CS.mentioned(st.convo, ["介護", "法人営業"])
    C2._present(st, H.qmap(st, H.BANK)["card_role2"])
    assert st.current["choices"][0] == "介護" and "法人営業" not in st.current["choices"]


def test_words_are_remembered_from_reading_without_names():
    st = asking("ib_why_now", segment="change")
    C2.turn(st, "みほん太郎です。接客が長いので活かしたい", H.BANK, Script(
        {"acts": [{"act": "name", "span": "みほん太郎"}, {"act": "answer", "slot": "why_now", "text": "接客が長いので活かしたい"}],
         "mentions": ["みほん太郎", "接客"]}))
    assert st.convo["mentions"] == ["接客"]
    CS.mentioned(st.convo, ["営業", "接客"])
    assert st.convo["mentions"] == ["営業", "接客"]  # 同じ言葉は新しい方へ
    old = C2.State.from_dict({k: v for k, v in st.to_dict().items() if k != "convo"} | {"convo": {"recent": [], "replies": []}})
    CS.mentioned(old.convo, ["接客"])  # mentions の無い前の状態でも覚えられる
    assert old.convo["mentions"] == ["接客"]


def test_put_first_moves_a_later_candidate_and_refuses_what_cannot_fit():
    pages = [["営業", "事務"], ["企画", "経理"]]
    assert P.put_first(pages, "企画", [], len, 40, 13, 20) == [["企画", "営業", "事務"], ["経理"]]
    assert P.put_first(pages, "営業", [], len, 40, 13, 20) is None  # もう最初のページにある
    assert P.put_first(pages, "あ" * 21, [], len, 40, 13, 20) is None
    assert P.put_first([["営業"]], "企画の仕事", ["x" * 36], len, 40, 13, 20) is None  # 候補が 1 つも残らない


# ----------------------------- 入り口の定義 -----------------------------
@pytest.mark.parametrize("change, match", [
    (lambda d: d["groups"].pop(), "群"),
    (lambda d: d["groups"][0]["phases"].append("hard_filter"), "群"),
    (lambda d: d["topics"].update(nope=["no_such_slot"]), "話題"),
    (lambda d: d["mention_questions"].append("q_nope"), "本人の言葉"),
])
def test_entry_groups_topics_and_mention_questions_are_checked(tmp_path, change, match):
    d = json.loads(CAREER_ENTRY.read_text(encoding="utf-8"))
    change(d)
    (tmp_path / "bank.json").write_text((CAREER_ENTRY.parent / "bank.json").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "entry.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        E.load(tmp_path / "entry.json", H.ALL_SLOTS, ("career", "baito"), ("crisis_lead", "crisis_tail", "crisis_resume"))


def test_career_entry_groups_cover_the_bank_in_the_designed_order():
    assert [g["name"] for g in C2.ENTRY.groups] == ["はじめ", "条件", "職歴", "好み"]
    assert [g["line"] for g in C2.ENTRY.groups[1:]] == ["次は希望の条件です", "次は職歴です", "あと少しで完成です"]
