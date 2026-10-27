"""アルバイトの相談を 1 タップで始めるときの、決まった対応の表(設計メモ docs/design/baito-quick-start.md)。

時間帯のボタン(§6.2): ボタンの言葉 → ハーネスの time_slots の値 → 求人の検索(logic.recommend)の time_band。
recommend の time_band は求人の time_bands の言葉と文字が一致するかだけを見るので、ボタンの言葉はこの表で直してから渡す
(そのまま渡すと「朝」「昼」は 0 件になる)。未成年(15〜17 歳、ひみつ、年齢が分からない)には「夜」と「深夜」のボタンを出さない。
時間帯は複数選べる(task 287): ボタンを押すたびに足し、[これで探す] で決める。[どれでもOK] は出しているボタンを全部選んで決める。
検索は、選んだ時間帯のどれか 1 つに合う求人を出す(logic.recommend の time_band に list で渡す)。
"""

from __future__ import annotations

from typing import NamedTuple


class TimeButton(NamedTuple):
    slot: str  # ハーネスの time_slots の値(harness.SLOTS)
    time_band: str  # 求人の検索の time_band(求人の time_bands の言葉)
    adult_only: bool  # 18 歳以上だけに出す


# 設計メモ §6.2 の表。並びはボタンを出す順
TIME_BUTTONS = {
    "朝": TimeButton("morning", "早朝", False),
    "昼": TimeButton("daytime", "日中", False),
    "夕方": TimeButton("evening", "夕方", False),
    "夜": TimeButton("evening", "夜", True),
    "深夜": TimeButton("late_night", "深夜", True),
}


ANY_BUTTON = "どれでもOK"  # 出している時間帯のボタンを全部選んで、すぐ探す
DONE_BUTTON = "これで探す"  # 選んだ時間帯で決める(1 つ以上選んだ後に出す)
RESET_BUTTON = "選び直す"  # 選んだ時間帯を空にする(1 つ以上選んだ後に出す)


def time_buttons(minor: bool) -> list[str]:
    """出す時間帯のボタン。未成年には 18 歳以上だけのボタン(夜・深夜)を出さない。"""
    return [b for b, t in TIME_BUTTONS.items() if not (minor and t.adult_only)]


def time_slot(button: str) -> str:
    """ボタン → ハーネスの time_slots の値。"""
    return TIME_BUTTONS[button].slot


def search_time_band(button: str) -> str:
    """ボタン → 求人の検索の time_band。"""
    return TIME_BUTTONS[button].time_band


def time_slots(buttons: list[str]) -> list[str]:
    """ボタン(複数) → ハーネスの time_slots の値(重ねない。「夕方」と「夜」はどちらも evening)。"""
    return list(dict.fromkeys(time_slot(b) for b in buttons))


def search_time_bands(buttons: list[str]) -> list[str]:
    """ボタン(複数) → 求人の検索の time_band(求人の time_bands の言葉の list。どれか 1 つに合えば出す)。"""
    return list(dict.fromkeys(search_time_band(b) for b in buttons))
