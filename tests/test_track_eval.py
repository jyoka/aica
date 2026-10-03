"""評価の雇用区分の判定(evals/checks.py)。set_search_track の呼び出しを、シナリオの期待(expect_tracks)と突き合わせる。
LLM は使わない。正しい振る舞いを違反にしない負例も並べる(v1 で評価器の偽陽性を3回くり返した反省)。
"""

from __future__ import annotations

import pytest

from evals import checks
from evals.checks import (
    conversation_checks,
    greeting_track_violations,
    track_expectations,
    tracks_by_turn,
    turn_violations,
    upload_violations,
)

BAITO, SEISHAIN = "アルバイト・パート", "正社員"


def t(bubbles=("どうしますか?",), switch=None, error=False, changed=True, actions=()):
    """1ターン。switch を与えると、そのターンで set_search_track を呼んだことにする。"""
    tools = []
    if switch:
        result = {"error": "track は…"} if error else {"track": switch, "changed": changed}
        tools.append({"name": "set_search_track", "args": {"track": switch, "evidence": "…"}, "result": result})
    return {"bubbles": list(bubbles), "quick_replies": ["今はいい"], "cards": [], "buttons": [], "tools": tools,
            "actions": list(actions)}


def statuses(expects, turns, msgs, **sc):
    return [r["status"] for r in track_expectations({"expect_tracks": expects, **sc}, turns, msgs)]


MSGS = [None, "転職を考えていて、正社員の仕事を相談したい", "営業を4年です", "ありがとう、考えます"]
FT = [{"track": SEISHAIN, "on_said": "正社員|転職"}]


def test_switch_on_the_expected_turn_to_the_expected_track_is_ok():
    assert statuses(FT, [t(), t(switch=SEISHAIN), t(), t()], MSGS) == ["ok"]


def test_no_switch_is_missing():
    assert statuses(FT, [t(), t(), t(), t()], MSGS) == ["missing"]


def test_switch_to_another_track_is_wrong_track():
    assert statuses(FT, [t(), t(switch=BAITO), t(), t()], MSGS) == ["wrong_track"]


def test_switch_after_the_expected_turn_is_late():
    rows = track_expectations({"expect_tracks": FT}, [t(), t(), t(switch=SEISHAIN), t()], MSGS)
    assert [(r["status"], r["due"], r["called_turn"]) for r in rows] == [("late", 2, 3)]


def test_fixed_turn_expectation():
    assert statuses([{"track": SEISHAIN, "turn": 3}], [t(), t(), t(switch=SEISHAIN), t()], MSGS) == ["ok"]
    assert statuses([{"track": SEISHAIN, "turn": 2}], [t(), t(), t(switch=SEISHAIN), t()], MSGS) == ["late"]


def test_switch_on_a_strong_signal_before_the_explicit_words_is_ok():
    # 年収の話(強い合図)で、「正社員」と言う前に切り替えるのは正しい
    msgs = [None, "年収を上げたい", "正社員で探したいです"]
    assert statuses([{"track": SEISHAIN, "on_said": "正社員"}], [t(), t(switch=SEISHAIN), t()], msgs) == ["ok"]


def test_failed_or_no_op_calls_do_not_count_as_a_switch():
    assert statuses(FT, [t(), t(switch=SEISHAIN, error=True), t(), t()], MSGS) == ["missing"]
    assert statuses(FT, [t(), t(switch=SEISHAIN, changed=False), t(), t()], MSGS) == ["missing"]


def test_expected_words_never_said_is_not_the_agents_fault():
    rows = track_expectations({"expect_tracks": FT}, [t(), t()], [None, "こんにちは"])
    assert [r["status"] for r in rows] == ["no_trigger"]
    r = conversation_checks({"expect_outcome": ["propose"], "expect_tracks": FT}, [t(), t()], [None, "こんにちは"])
    assert r["track_ok"] is None and not any("切り替え" in v for v in r["violations"])


def test_switch_when_none_is_expected_is_unexpected():
    # 区分が記録済みの再訪・履歴書だけ: 切り替えないのが正しい
    sc = {"expect_tracks": [], "seed_track": BAITO}
    assert statuses([], [t(), t(switch=SEISHAIN)], [None, "カフェを探したい"], seed_track=BAITO) == ["unexpected"]
    r = conversation_checks({**sc, "expect_outcome": ["propose"]}, [t(), t()], [None, "カフェを探したい"])
    assert r["track_ok"] is True and not any("切り替え" in v for v in r["violations"])


SWITCH = [{"track": BAITO, "on_said": "バイト"}, {"track": SEISHAIN, "on_said": "やっぱり正社員"}]
SWITCH_MSGS = [None, "渋谷あたりでカフェのバイトを探しています", "やっぱり正社員で探したいです", "350万以上"]


def test_two_switches_in_order_are_both_ok():
    turns = [t(), t(switch=BAITO), t(switch=SEISHAIN), t()]
    assert statuses(SWITCH, turns, SWITCH_MSGS) == ["ok", "ok"]


def test_skipping_the_first_switch_is_missing_then_ok():
    turns = [t(), t(), t(switch=SEISHAIN), t()]
    assert statuses(SWITCH, turns, SWITCH_MSGS) == ["missing", "ok"]


def test_switching_to_fulltime_too_early_is_wrong_track():
    # 本人が「バイトを探している」と言ったターンに正社員へ切り替えた
    turns = [t(), t(switch=SEISHAIN), t(), t()]
    assert statuses(SWITCH, turns, SWITCH_MSGS) == ["wrong_track", "missing"]


def test_switching_back_after_the_last_expectation_is_unexpected():
    turns = [t(), t(switch=BAITO), t(switch=SEISHAIN), t(switch=BAITO)]
    assert statuses(SWITCH, turns, SWITCH_MSGS) == ["ok", "ok", "unexpected"]


def test_violations_name_each_failure():
    r = conversation_checks({"expect_outcome": ["propose"], "expect_tracks": FT}, [t(), t(switch=BAITO), t(), t()],
                            MSGS)
    assert r["track_ok"] is False
    assert any("「正社員」を期待したのに「アルバイト・パート」へ切り替えた" in v for v in r["violations"])


def test_scenarios_without_expectations_are_not_judged():
    r = conversation_checks({"expect_outcome": ["propose"]}, [t(), t(switch=SEISHAIN)], [None, "正社員で"])
    assert r["track_ok"] is None and r["track_checks"] == []


# ----------------------------- 区分ごとの形式の決まり -----------------------------


def test_tracks_by_turn_follow_the_switches():
    assert tracks_by_turn([t(), t(switch=SEISHAIN), t()]) == ["未定", SEISHAIN, SEISHAIN]
    assert tracks_by_turn([t(), t()], BAITO) == [BAITO, BAITO]


def test_format_rules_can_be_relaxed_per_track(monkeypatch):
    long = "あ" * 100 + "ですか?"
    monkeypatch.setitem(checks.FORMAT_RULES, SEISHAIN, {"questions_max": 2, "bubble_max": 120, "branch_by": 5})
    two = ["経験を教えてください。どんな場面でしたか?", "ほかにありますか?"]
    assert turn_violations(0, t([long]), SEISHAIN) == []
    assert turn_violations(0, t(two), SEISHAIN) == []
    # アルバイト・パートは v1 のまま(80字・1問)
    assert any("80字以内" in v for v in turn_violations(0, t([long]), BAITO))
    assert any("1問まで" in v for v in turn_violations(0, t(two), BAITO))
    # 会話の中で正社員に切り替えたターンから緩む
    r = conversation_checks({"expect_outcome": ["consult"]}, [t([long]), t([long], switch=SEISHAIN)], [None, "正社員で"])
    assert [v for v in r["violations"] if "字" in v] == ["T1: 吹き出しが104字(80字以内)"]


def test_parttime_and_undecided_rules_are_unchanged_from_v1_and_fulltime_is_relaxed():
    # 値は F(チャットの playbook)で決めた。アルバイト・パートと未定は v1 のまま、正社員だけ緩める
    assert checks.FORMAT_RULES[BAITO] == checks.FORMAT_RULES["未定"] == {"questions_max": 1, "bubble_max": 80,
                                                                        "branch_by": 3}
    assert checks.FORMAT_RULES[SEISHAIN] == {"questions_max": 1, "bubble_max": 120, "branch_by": 6}


# ----------------------------- 区分つきの再訪の挨拶 -----------------------------

REVISIT = {"seed_track": BAITO, "expect_track_in_greeting": True}


@pytest.mark.parametrize("greeting", [
    "おかえりなさい。前回はアルバイトで探していましたが、最近見ていたカフェのお仕事で探しますか?",
    "おかえりなさい。最近はカフェのバイトを見ていましたか?",
])
def test_greeting_that_puts_track_and_activity_in_one_question_is_fine(greeting):
    assert greeting_track_violations(REVISIT, [t([greeting])]) == []


def test_greeting_without_the_track_or_reasking_it_is_a_violation():
    assert greeting_track_violations(REVISIT, [t(["おかえりなさい。最近はカフェのお仕事を見ていましたか?"])])
    reask = t(["おかえりなさい。アルバイトと正社員、どちらで探しますか?"])
    assert any("聞き直した" in v for v in greeting_track_violations(REVISIT, [reask]))


# ----------------------------- 履歴書のアップロードの後の1問 -----------------------------

SHOWN = ["職歴: みほん商事・正社員、みほんソフト・正社員", "スキル: 法人営業、プロダクト企画"]


def test_one_question_after_an_upload_is_fine():
    turns = [t(), t(["法人営業とPMのご経験ですね。その経験を活かせる正社員のお仕事で探しますか?"])]
    assert upload_violations(turns, {"turn": 1, "shown": SHOWN}) == []


def test_after_an_upload_two_questions_repeating_or_switching_track_are_violations():
    two = [t(), t(["ご経験を確認しました。正社員で探しますか?", "希望の勤務地はどこですか?"])]
    assert any("1問" in v for v in upload_violations(two, {"turn": 1, "shown": SHOWN}))
    repeat = [t(), t([SHOWN[0], "この経験を活かしますか?"])]
    assert any("くり返した" in v for v in upload_violations(repeat, {"turn": 1, "shown": SHOWN}))
    switched = [t(), t(["正社員のお仕事で考えますね。どの職種が気になりますか?"], switch=SEISHAIN)]
    assert any("履歴書の内容だけ" in v for v in upload_violations(switched, {"turn": 1, "shown": SHOWN}))
