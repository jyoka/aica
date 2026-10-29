"""会話の層で使う、文のそろえ方と数の読み方(どの入り口でも同じ)。"""

from __future__ import annotations

import re
import unicodedata

KATA_TO_HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}  # ァ..ヶ -> ぁ..ゖ(長音の「ー」はそのまま)


def nfkc(s) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip()


def kana_fold(s) -> str:
    """NFKC にそろえ、カタカナをひらがなにする(危ない言葉と「続ける」を読む前)。
    「シニタイ」「ｼﾆﾀｲ」「しにたい」が同じ形になるので、言葉の一覧はひらがなと漢字の形だけで持てる。"""
    return nfkc(s).translate(KATA_TO_HIRA)


def digits_in(text) -> set[str]:
    """文に数字で書いてある数(全角は半角に、桁の区切りの , は外す)。"""
    return set(re.findall(r"\d+(?:\.\d+)?", nfkc(text).replace(",", "")))


def number_in_text(number, text) -> bool:
    """数が文に数字で書いてあるか(数を作らない)。"""
    if number is None or isinstance(number, bool) or not isinstance(number, (int, float)):
        return False
    s = str(int(number)) if float(number).is_integer() else str(number)
    return s in digits_in(text)
