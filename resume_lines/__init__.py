"""書類の行の決まり(docs/prd/resume-arcade.md): 数字の検出、数字の空欄、事実を作っていないかの確かめ。

ほかのパッケージに依存しない(dip_mcp と resume_arcade の両方が使い、dip_mcp が resume_arcade や line_runtime を
import する形を作らないため)。LLM は使わない。

- 数字の検出(find_metric)は、ゲームの共通部品 line_runtime/static/arcade/arcade-ja.js の findMetric を Python に写したもの。
  日付(2022年、3月、5日)は成果の数字と数えない
- 事実の確かめ(check_numbers / quote_in)は、AI が書いた文の数字と引用が、本人の言葉(sources)にあるかを見る。
  比べる前に、両方を NFKC でそろえ、桁の区切り(1,200 と 1200)をそろえる。漢数字(三割、半分)は元の検出と同じ形だけを
  数字として扱い、書いたままの形で比べる(三割を 30% と言い換えたら、無い数字として空欄になる)
"""

from __future__ import annotations

import re
import unicodedata

# ----------------------------- 数字の空欄 -----------------------------

PLACEHOLDER = "[+X% ← 数字を入れる]"
PLACEHOLDER_YEN = "[X円 ← 数字を入れる]"
PLACEHOLDER_COUNT = "[N件 ← 数字を入れる]"
PLACEHOLDER_WHEN = "[時期 ← 数字を入れる]"
PLACEHOLDER_RE = re.compile(r"\[[^\]]*(?:数字を入れる|add your number)[^\]]*\]")


def has_placeholder(text: str) -> bool:
    return PLACEHOLDER_RE.search(str(text or "")) is not None


def placeholder_count(text: str) -> int:
    return len(PLACEHOLDER_RE.findall(str(text or "")))


# ----------------------------- 文字をそろえる -----------------------------


def clean(text) -> str:
    """NFKC で全角の数字と英字を半角にし、空白をまとめる(arcade-ja.js の clean と同じ)。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(text or ""))).strip()


def _fold(text) -> str:
    """比べる形: NFKC、空白を除く、桁の区切りのカンマを外す(1,200 → 1200)。"""
    t = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)


# ----------------------------- 数字の検出(日付は数えない) -----------------------------

UNIT_JA = (r"(?:%|パーセント|ポイント|割|倍|円|ドル|人|名|件|時間|分|秒|日|週間|か月|ヶ月|カ月|ヵ月|年|社|店舗|回|個|本|ページ|台|"
           r"か国|カ国|ヶ国|言語)")
UNIT_EN = (r"(?:percent\b|x\b|×|k\b|m\b|bn\b|million|billion|thousand|ms\b|s\b|secs?\b|seconds?|minutes?|mins?\b|hours?|hrs?\b|"
           r"days?|weeks?|months?|years?|users?|customers?|clients?|people|members|engineers|developers|teams?|projects?|"
           r"tickets?|deals?|leads?|accounts|times|apps|downloads?|installs?|visits?|visitors?|views?|sign-?ups?|points?|pts\b|"
           r"pt\b|stars?|reviews?|orders?|bookings?|requests?|countries|languages|issues|bugs?|pages?|stores?|schools?|"
           r"students?|patients?|calls?|emails?|items?|units?|usd|eur|gbp|jpy|yen|dollars?|euros?)")
MAG = r"(?:万|億|千|百)?"
NUM = r"\d[\d,.]*"
METRIC_RE = re.compile(rf"(?:[$¥€£]\s?{NUM}\s?(?:k|m|bn|万|億)?|{NUM}\s?{MAG}{UNIT_JA}|{NUM}\s?{UNIT_EN})", re.IGNORECASE)
KANJI_RE = re.compile(r"(?:[一二三四五六七八九十]+|半)(?:割|倍)|[0-9一二三四五六七八九十]+分の[0-9一二三四五六七八九十]+|半分|半減|倍増"
                      r"|\b(?:doubled|tripled|halved|quadrupled)\b", re.IGNORECASE)
NUMU = rf"(?:[$¥]\s?)?{NUM}\s?{MAG}(?:{UNIT_JA}|ms\b|s\b|k\b|m\b|x\b)?"
RANGE_RE = re.compile(rf"(?:from\s+)?{NUMU}\s*(?:から|→|->|⇒|〜|~|to)\s*(?:約|およそ|about\s+)?\s*{NUMU}", re.IGNORECASE)
RANK_RE = re.compile(r"\b(?:top|rank(?:ed)?|no\.)\s?\d+|#\d+|\d+\s?位", re.IGNORECASE)


def is_dateish(m: str, after: str = "") -> bool:
    """4 桁の年(2022年、2020s)と、暦の日付(3月、5日、9時)は成果の数字ではない。after は数字のすぐ後ろの文字。"""
    return bool(re.fullmatch(r"(19|20)\d\d\s?(年|s)?", m) or re.fullmatch(r"\d{1,2}\s?(?:月|日|時)", m)
                or (re.fullmatch(r"\d+", m) and re.match(r"[月日時]", after or "")))


def find_metric(text, bare_numbers: bool = True) -> str | None:
    """答えの中の成果の数字を 1 つ返す(無ければ None)。範囲(61%から68%)、単位つきの数、漢数字(3割、半分)、順位の順に探す。
    bare_numbers が True なら、単位の無い 2 以上の数も数字と数える。"""
    t = clean(text)
    rg = RANGE_RE.search(t)
    if rg and re.search(r"\d.*\d", rg.group(0)) and not re.match(r"(19|20)\d\d", rg.group(0)):
        return re.sub(r"^from\s+", "", rg.group(0), flags=re.IGNORECASE).strip()
    for m in METRIC_RE.finditer(t):
        s = m.group(0).strip()
        if not is_dateish(s, t[m.end():]):
            return s
    if k := KANJI_RE.search(t):
        return k.group(0)
    if r := RANK_RE.search(t):
        return r.group(0)
    if bare_numbers:
        for m in re.finditer(r"\d[\d,.]*", t):
            n = m.group(0)
            if is_dateish(n, t[m.end():]):
                continue
            try:
                if float(n.replace(",", "").rstrip(".")) >= 2:
                    return n
            except ValueError:
                continue
    return None


def has_metric(text, bare_numbers: bool = True) -> bool:
    return find_metric(text, bare_numbers) is not None


def is_vague(text, min_chars: int = 0, metric: str = "required") -> bool:
    """あいまいな答えか(arcade-ja.js の isVague の日本語の側)。metric: required は数字が無ければあいまい、
    enough は短くても数字があればよい、ignore は長さだけで見る。"""
    t = clean(text)
    if not t:
        return True
    short = len(t.replace(" ", "")) < min_chars
    if metric == "ignore":
        return short
    num = has_metric(t)
    return short and not num if metric == "enough" else not num


# ----------------------------- 事実の確かめ -----------------------------

# 生成した文の中の数字のかたまり(前の通貨記号・「約」と、後ろの単位まで)。漢数字は KANJI_RE と同じ形だけ
_NUMBER_SPAN = re.compile(rf"(?:約|およそ)?[$¥€£]?\s?(\d+(?:[.,]\d+)*)\s?{MAG}(?:{UNIT_JA}|{UNIT_EN})?", re.IGNORECASE)
_YEN_UNITS = re.compile(r"[円¥$€£]|ドル|usd|yen|dollar", re.IGNORECASE)
_PERCENT_UNITS = re.compile(r"%|パーセント|ポイント|割|倍|percent|pt", re.IGNORECASE)


def _placeholder_for(span: str, number: str, after: str) -> str:
    if is_dateish(clean(span), after) or re.search(r"(19|20)\d\d\s*年", span):
        return PLACEHOLDER_WHEN
    if _YEN_UNITS.search(span):
        return PLACEHOLDER_YEN
    if _PERCENT_UNITS.search(span):
        return PLACEHOLDER
    return PLACEHOLDER_COUNT


def _number_in(number: str, folded_sources: str) -> bool:
    n = _fold(number)
    return re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", folded_sources) is not None


def check_numbers(text, sources: list[str] | str) -> tuple[str, int]:
    """text の数字のうち、sources(本人の答え・書類の行)に無いものを数字の空欄に置き換える。
    (置き換えた後の文, 置き換えた数)。すでにある数字の空欄の中は見ない。"""
    s = str(text or "")
    if not s:
        return s, 0
    folded = _fold(" ".join(sources) if isinstance(sources, list) else sources)
    out, replaced, last = [], 0, 0
    keep = [m.span() for m in PLACEHOLDER_RE.finditer(s)]

    def inside(i: int) -> bool:
        return any(a <= i < b for a, b in keep)

    spans: list[tuple[int, int, str]] = []
    for m in KANJI_RE.finditer(s):
        if not inside(m.start()) and _fold(m.group(0)) not in folded:
            spans.append((m.start(), m.end(), PLACEHOLDER))
    norm = unicodedata.normalize("NFKC", s)
    same_length = len(norm) == len(s)  # NFKC で字数が変わらないときだけ、そろえた文で探す(全角の数字も拾う)
    for m in _NUMBER_SPAN.finditer(norm if same_length else s):
        if inside(m.start()) or any(a <= m.start() < b for a, b, _ in spans):
            continue
        if not _number_in(m.group(1), folded):
            spans.append((m.start(), m.end(), _placeholder_for(m.group(0), m.group(1), s[m.end():m.end() + 1])))
    for start, end, ph in sorted(spans):
        if start < last:
            continue
        out += [s[last:start], ph]
        last = end
        replaced += 1
    return ("".join(out) + s[last:]) if replaced else s, replaced


def quote_in(quote, sources: list[str] | str) -> bool:
    """引用が本人の言葉(sources)にそのままあるか(NFKC、空白、桁の区切り、末尾の句点はそろえて比べる)。"""
    q = _fold(quote).strip("「」\"'。.…")
    if not q:
        return False
    return q in _fold(" ".join(sources) if isinstance(sources, list) else sources)
