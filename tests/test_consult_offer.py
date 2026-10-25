"""相談のペルソナの評価の期待(task 133)。実 LLM なし、実 API なし。

H の評価(evals/baseline/sim-20261004-113109.json)で、相談・ペルソナの outcome_as_expected が 2/5 だった。
相談の決まり(5、6問で求人を見るかを聞く)に従うと、ターン上限 8 では結果の記録に届かないことがある。
そこで相談から始まるペルソナ(T-* のうち consult_mode のもの)の期待を、「結果を記録した」から
「T8 までに求人を見るかを聞いた(または探した)」に変えた(ユーザーの決定、2026-10-04)。ここでは、
(a) 判定(evals.checks.jobs_offer_turn)の正例・負例、(b) シナリオの期待の付け方、
(c) H の保存済みの会話で判定し直した結果(T-career-fulltime と T-student-first が合格、T-blank-parent が不合格)
を確かめる。
2026-10-26(task 225)で、相談の問いを4問までにした(4問目までに求人を見るかを聞く)。相談のペルソナは本人の1回目の発言で
相談に入る(T2 が1問目)ので、期限 expect_offer_by は T5 にした。(c) は task 133 のときの期限(T8)で判定し直したものとして残す。
同じ task 225 で、事情を話して探し始めた T-side-job と T-blank-parent は相談に入れないことにした(利用者の決定 2026-10-25、
その読み方 2026-10-26)。足りない条件だけを聞き(条件の問いは2問まで)、T4 までに探すので、期限は T4 にした。
"""

from __future__ import annotations

import json

import pytest

from dip_mcp.data import ROOT
from evals.checks import (
    CONSULT_QUESTIONS_MAX,
    SITUATION_QUESTIONS_MAX,
    conversation_checks,
    jobs_offer_turn,
)
from evals.run import SCENARIO_TURNS, recheck
from evals.scenarios import load_scenarios

H_BASELINE = ROOT / "evals" / "baseline" / "sim-20261004-113109.json"
SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
CONSULT_PERSONAS = ("T-career-fulltime", "T-student-first")
# 事情を話して探し始める(相談に入れない)ペルソナ
SITUATION_PERSONAS = ("T-side-job", "T-blank-parent")
# task 133 のときに相談から始まったペルソナ(H の見直しで当時の期限 T8 を当てる)
H_CONSULT_PERSONAS = ("T-career-fulltime", "T-side-job", "T-student-first", "T-blank-parent")
ASK = ["それ以外", "今はいい"]
OFFER = ["求人を見る", "もう少し相談したい", "今はいい"]


def turn(bubbles: list[str], tools: tuple = (), cards: tuple = (), quick_replies: list | None = None) -> dict:
    return {"bubbles": bubbles, "tools": [{"name": n, "args": {}, "result": {}} for n in tools],
            "cards": list(cards), "quick_replies": quick_replies or ASK, "buttons": [], "actions": []}


def hearing(n: int) -> list[dict]:
    """求人を見るかを聞かないヒアリングの問いが n ターン(1通目の挨拶を含む)。"""
    return [turn(["はじめまして。今日は何から始めますか?"], quick_replies=["求人を探す", "まず相談する", "今はいい"])] + [
        turn([f"ありがとうございます。{k}つ目を伺います。そう思ったのは、どんな場面でしたか?"]) for k in range(2, n + 1)]


# ----------------------------- (a) 判定の正例・負例 -----------------------------


@pytest.mark.parametrize("question", [
    "この内容で求人を見ますか？",
    "平日夕方・中野か高円寺で整理しました。この条件で求人を見ますか?",
    "覚えることの多さも気にしながら、求人を見ますか？",
    "この内容で探してみますか?",
    "この条件で求人をご紹介しましょうか?",
    "求人を探してみませんか?",
    "この条件で求人をお送りしましょうか?",
    "求人をご覧になりますか?",
    "この内容で求人を出しますか?",
])
def test_asking_whether_to_see_jobs_counts_as_an_offer(question):
    turns = hearing(6) + [turn([question], quick_replies=OFFER)]
    assert jobs_offer_turn(turns) == 7


@pytest.mark.parametrize("tools, cards", [
    (("recommend_jobs",), ()),
    (("relax_conditions",), ()),
    ((), ({"job_id": "j1"},)),
])
def test_searching_counts_as_an_offer_even_without_asking(tools, cards):
    turns = hearing(4) + [turn(["池袋の夕方の求人です。"], tools=tools, cards=cards)]
    assert jobs_offer_turn(turns) == 5


@pytest.mark.parametrize("question", [
    "どんな求人を探していますか?",  # 疑問詞のある問いは希望を聞くヒアリング
    "どのあたりで探しますか?",
    "気になる求人はありますか?",
    "求人を見る前に、もう1つ伺ってもいいですか?",  # 読点をまたいで別のことを聞く
    "事務の経験を活かせる仕事を考えたいですか?",  # T-blank-parent の T7
    "今、お仕事を探している理由を教えていただけますか?",
    "求人を探すのに不安はありますか?",  # 動詞のあとに別の述語(ありますか・分かりますか)が続く
    "求人を探す上で希望はありますか?",
    "求人の見つけ方は分かりますか?",
    "求人を探していますか?",
])
def test_hearing_questions_are_not_an_offer(question):
    assert jobs_offer_turn(hearing(6) + [turn([question])]) is None


def test_statement_without_question_and_answering_a_question_are_not_an_offer():
    turns = hearing(5) + [
        turn(["求人を見る前に、条件を整理しますね。働ける時間帯はいつですか?"]),
        # 不安に資料で答える(answer_job_question)のは求人を探したことにならない
        turn(["パソコン作業は求人によって異なります。", "特にどんな作業が気になりますか?"], tools=("answer_job_question",)),
    ]
    assert jobs_offer_turn(turns) is None


def test_offer_by_the_due_turn_passes_and_a_later_one_fails():
    sc = {"expect_offer_by": 8, "consult_mode": True}
    said = [None] * 9
    asked_t8 = hearing(7) + [turn(["この内容で求人を見ますか?"], quick_replies=OFFER)]
    r = conversation_checks(sc, asked_t8, said)
    # 結果(record_conversation_outcome)を記録していなくても、T8 で聞いていれば期待どおり
    assert r["outcome_as_expected"] and r["offer_turn"] == 8 and not r["outcome_recorded"]
    asked_t9 = hearing(8) + [turn(["この内容で求人を見ますか?"], quick_replies=OFFER)]
    r = conversation_checks(sc, asked_t9, said + [None])
    assert not r["outcome_as_expected"] and r["offer_turn"] == 9
    never = conversation_checks(sc, hearing(8), said)
    assert not never["outcome_as_expected"] and never["offer_turn"] is None


def test_other_scenarios_still_expect_the_recorded_outcome():
    sc = {"expect_outcome": ["propose"]}
    asked = hearing(3) + [turn(["この内容で求人を見ますか?"], quick_replies=OFFER)]
    r = conversation_checks(sc, asked, [None] * 4)
    assert r["offer_turn"] == 4 and not r["outcome_as_expected"]  # 聞いただけでは、結果の期待は満たさない


# ----------------------------- (b) シナリオの期待 -----------------------------


def test_consult_personas_expect_an_offer_by_the_fourth_question():
    for case in CONSULT_PERSONAS:
        sc = SCENARIOS[case]
        # 本人の1回目の発言(U1)で相談に入り、T2 が1問目。4問目(T5)までに求人を見るかを聞く(task 225。前は T8)。
        # ターンの上限(task 222 で相談のシナリオは 12)より前に、提案と反応まで見られる
        assert sc["consult_mode"] and sc["expect_offer_by"] == 1 + CONSULT_QUESTIONS_MAX == 5, case
        assert sc["expect_offer_by"] < sc.get("max_turns", SCENARIO_TURNS), case
        assert "expect_outcome" not in sc, case  # 結果の記録は期待しない(期待を2つ持たない)
    # 相談から始まらないペルソナと、相談の C-* は、これまでどおり記録した結果で見る
    for case in ("T-switch-to-fulltime", "C-consult-propose", "C-consult-only", "C-consult-switch"):
        assert SCENARIOS[case]["expect_outcome"] and "expect_offer_by" not in SCENARIOS[case], case
    # 事情を話して探し始めた人は相談に入れず、U1 の答え(T2)と U2 の答え(T3)で足りない条件を聞き(2問まで)、T4 までに探す
    for case in SITUATION_PERSONAS:
        sc = SCENARIOS[case]
        assert sc["expect_no_consult"] and not sc.get("consult_mode") and not sc.get("measure_depth"), case
        assert sc["expect_offer_by"] == 2 + SITUATION_QUESTIONS_MAX == 4, case
        assert "expect_outcome" not in sc and sc["expect_offer_by"] < sc.get("max_turns", SCENARIO_TURNS), case
    assert [sc["id"] for sc in SCENARIOS.values() if sc.get("expect_offer_by")] == list(H_CONSULT_PERSONAS)


# ----------------------------- (c) H の保存済みの会話で判定し直す(費用なし) -----------------------------


def test_h_conversations_rechecked_with_the_new_expectation():
    runs = {r["case"]: r for r in json.loads(H_BASELINE.read_text(encoding="utf-8"))["runs"] if r["status"] == "ok"}
    before = {case: runs[case]["metrics"]["outcome_as_expected"] for case in (*H_CONSULT_PERSONAS, "T-switch-to-fulltime")}
    # task 133 のときの期限(T8)で判定し直す(今のシナリオの期限は T5、T-side-job と T-blank-parent は T4)
    after = {case: recheck(runs[case]["detail"], SCENARIOS[case] | ({"expect_offer_by": 8} if case in H_CONSULT_PERSONAS else {}))
             for case in before}
    assert before == {"T-career-fulltime": False, "T-side-job": True, "T-student-first": False,
                      "T-blank-parent": False, "T-switch-to-fulltime": True}
    assert {case: r["outcome_as_expected"] for case, r in after.items()} == {
        "T-career-fulltime": True,  # T7 で「この内容で求人を見ますか?」、T8 で探した(0件)
        "T-side-job": True,  # T6 で聞き、T7 で提案した
        "T-student-first": True,  # T7 で「この条件で求人を見ますか?」(本人は「もう少し相談したい」)
        "T-blank-parent": False,  # T8 まで求人を見るかを聞かず、探してもいない
        "T-switch-to-fulltime": True,  # 相談から始まらないので、これまでどおり propose の記録で見る
    }
    assert {case: r["offer_turn"] for case, r in after.items()} == {
        "T-career-fulltime": 7, "T-side-job": 6, "T-student-first": 7, "T-blank-parent": None,
        "T-switch-to-fulltime": 2}
    # 結果を記録しなかった3本のうち、2本は決まりどおりに聞いていた
    assert [case for case, r in after.items() if not r["outcome_recorded"]] == [
        "T-career-fulltime", "T-student-first", "T-blank-parent"]
