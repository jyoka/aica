"""アルバイトの相談を 1 タップで始めるときの、決まった対応の表(設計メモ docs/design/baito-quick-start.md)。

時間帯のボタン(§6.2): ボタンの言葉 → ハーネスの time_slots の値 → 求人の検索(logic.recommend)の time_band。
recommend の time_band は求人の time_bands の言葉と文字が一致するかだけを見るので、ボタンの言葉はこの表で直してから渡す
(そのまま渡すと「朝」「昼」は 0 件になる)。未成年(15〜17 歳、ひみつ、年齢が分からない)には「夜」と「深夜」のボタンを出さない。
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


def time_buttons(minor: bool) -> list[str]:
    """出す時間帯のボタン。未成年には 18 歳以上だけのボタン(夜・深夜)を出さない。"""
    return [b for b, t in TIME_BUTTONS.items() if not (minor and t.adult_only)]


def time_slot(button: str) -> str:
    """ボタン → ハーネスの time_slots の値。"""
    return TIME_BUTTONS[button].slot


def search_time_band(button: str) -> str:
    """ボタン → 求人の検索の time_band。"""
    return TIME_BUTTONS[button].time_band
