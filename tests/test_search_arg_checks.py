"""避けたいこと・やりたいことを検索の引数に当てたかの機械判定(evals/checks.py の search_arg_violations。task 109)。LLM は使わない。

task 111 のレビュー: 避けたいことを keywords に入れていないか、属性で言える希望を属性の引数に当てたか。
task 126 の後続: 電話だけを避けたい答えで max_talk_level を付けていないか。
どれも正しい振る舞いを違反にしない負例を添える(v1 で評価器の偽陽性を3回繰り返した)。
"""

from __future__ import annotations

import pytest

from evals.checks import conversation_checks, search_arg_violations

CONFIRM = "お客様と話すレジや接客は大丈夫ですか?"


def turn(bubbles, *searches):
    return {"bubbles": bubbles, "quick_replies": ["今はいい"], "cards": [], "buttons": [], "actions": [],
            "tools": [{"name": "recommend_jobs", "args": args, "result": {"total": 3, "jobs": []}} for args in searches]}


def _v(said: list[str], turns: list[dict]) -> list[str]:
    return search_arg_violations(turns, [None, *said])


# ----------------------------- (1) keywords に避けたいこと -----------------------------


@pytest.mark.parametrize("kw", ["接客はしたくない", "立ち仕事を避けたい", "人と話さない", "電話対応なし", "力仕事以外",
                                "ノルマがない", "残業のない職場"])  # 「〜がない」「〜のない」も避けたいこと(task 130)
def test_avoids_put_into_keywords_are_flagged(kw):
    v = _v(["探しています"], [turn(["こんにちは"]), turn(["探しますね。"], {"keywords": kw})])
    assert any("keywords に避けたいこと" in x and kw in x for x in v)


@pytest.mark.parametrize("kw", ["コーヒー", "黙々と作業", "経験なし", "未経験", "残業少ない", "子どもに勉強を教える",
                                "人間関係が良い", "まかない付き", "まかない アットホームな職場",
                                # 求められない条件と本人のことは、避けたいことではない(task 130)
                                "資格なし", "免許なし", "スキルなし", "経験がない", "分からないことは教えてもらえる"])
def test_wants_and_plain_conditions_in_keywords_are_not_flagged(kw):
    assert _v(["探しています"], [turn(["こんにちは"]), turn(["探しますね。"], {"keywords": kw})]) == []


# ----------------------------- (2) 属性で言える希望を引数に当てたか -----------------------------


@pytest.mark.parametrize(("said", "args"), [
    ("接客はしたくないです", {"max_talk_level": 2}),
    ("接客はしたくないです", {"max_talk_level": 1}),
    ("人と話すのが苦手です", {"max_talk_level": 1}),
    ("体を動かしたいです", {"min_physical_level": 3}),
    ("立ち仕事は避けたいです", {"max_physical_level": 1}),
    ("人と話す仕事がしたい", {"min_talk_level": 3}),
])
def test_wishes_mapped_to_the_attribute_argument_are_fine(said, args):
    assert _v([said], [turn(["こんにちは"]), turn(["探しますね。"], args)]) == []


@pytest.mark.parametrize(("said", "args", "arg"), [
    ("接客はしたくないです", {"keywords": "接客なしの仕事"}, "max_talk_level"),
    ("接客はしたくないです", {}, "max_talk_level"),
    ("人と話すのが苦手です", {"max_talk_level": 2}, "max_talk_level"),  # 苦手は 1(2 では接客の少ない店頭も残る)
    ("体を動かしたいです", {"keywords": "体を動かす"}, "min_physical_level"),
])
def test_wishes_not_mapped_to_the_attribute_argument_are_flagged(said, args, arg):
    v = _v([said], [turn(["こんにちは"]), turn(["探しますね。"], args)])
    assert any(f"recommend_jobs の {arg} に当てていない" in x for x in v)


def test_only_the_first_search_after_the_wish_is_judged():
    """0件で本人と相談して属性を外して探し直すのは、正しい振る舞い(違反にしない)。"""
    turns = [turn(["こんにちは"]), turn(["探しますね。"], {"max_talk_level": 2}),
             turn(["人と話す量の条件を外すと見つかります。外しますか?"]), turn(["外して探しますね。"], {"area": "渋谷"})]
    assert _v(["接客はしたくないです", "はい", "お願いします"], turns) == []


def test_no_wish_said_means_nothing_to_map():
    assert _v(["渋谷でカフェ"], [turn(["こんにちは"]), turn(["探しますね。"], {"job_type": "カフェ"})]) == []


# ----------------------------- (3) 電話だけなら max_talk_level を付けない -----------------------------


@pytest.mark.parametrize("phone", ["電話はしたくないです", "電話が苦手です", "電話対応は嫌です"])
def test_phone_only_answer_with_a_talk_level_is_flagged(phone):
    turns = [turn(["こんにちは"]), turn(["探しますね。"], {"max_talk_level": 2})]
    assert any("電話だけを避けたい答えで" in x for x in _v([phone], turns))
    # 1問で確かめて「レジや接客は大丈夫」と言われたのに付けた
    turns = [turn(["こんにちは"]), turn([CONFIRM]), turn(["探しますね。"], {"max_talk_level": 2})]
    assert any("電話だけを避けたい答えで" in x for x in _v([phone, "レジや接客は大丈夫です"], turns))


@pytest.mark.parametrize(("said", "args"), [
    (["電話はしたくないです", "接客も苦手です"], {"max_talk_level": 2}),
    (["電話はしたくないです", "はい、避けたいです"], {"max_talk_level": 2}),
    (["電話はしたくないです", "レジや接客は大丈夫です"], {}),
    (["電話も接客もしたくないです", "渋谷で"], {"max_talk_level": 2}),
])
def test_phone_answers_handled_as_the_prompt_says_are_fine(said, args):
    turns = [turn(["こんにちは"]), turn([CONFIRM]), turn(["探しますね。"], args)]
    assert _v(said, turns) == []


def test_search_arg_violations_are_reported_apart_from_violations():
    """基準(2026-09-29)に無い判定なので、violations に混ぜない(基準と比べられるように)。"""
    turns = [turn(["こんにちは"]), turn(["探しますね。"], {"keywords": "接客はしたくない"})]
    r = conversation_checks({"expect_outcome": ["propose"]}, turns, [None, "接客はしたくないです"])
    assert r["search_arg_violations"] and not any("keywords" in v for v in r["violations"])
