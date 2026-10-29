"""守りの前処理(設計メモ docs/design/conversation-layer.md §4.2、§4.11)。どの入り口でも、モデルを呼ぶ前に見る。

- 危ない言葉(希死念慮・自傷): 本人の文を crisis.json の言葉と照らす(crisis_hit)。当たれば、入り口はモデルを呼ばずに決まった文
  (crisis_text。相談窓口はどの入り口も同じ、前後の文は入り口の調子)を返す。言葉と窓口は、アルバイト・パートの v4 が task 256 で
  作った表(dip_mcp/baito/crisis_v4.json)と同じ。v4 もこの関数を読み込む(v4 の動きは変えない)
- 窓口を案内した次の発言が「続ける」か(wants_resume。「続けたくない」「大丈夫じゃない」は当たらない)
- 個人情報: ランタイムが伏せた印(REDACTED。line_runtime.turn.accept)と、読む段が示した氏名と要配慮の部分(act の name と
  sensitive の span)は、値にも返事にも出さない。値から除くのが scrub、返事に出ていないかを見るのが leaks

自由に打った文を正規表現で読まない(ADR-0009、ADR-0010)の例外は、ここの言葉の表だけ(§4.3)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from dip_mcp.convo.text import kana_fold, nfkc
from resume_draft.pii import REDACTED

HERE = Path(__file__).resolve().parent
SOURCE_MARK = "{source}"  # 入り口の文に窓口の出典(CRISIS["source"])を差し込む印


def load_crisis() -> dict:
    """危ない言葉と相談窓口(crisis.json)。窓口は厚生労働省「まもろうよ こころ」(source)で checked の日に確かめたもの。"""
    return json.loads((HERE / "crisis.json").read_text(encoding="utf-8"))


def words_re(words) -> re.Pattern:
    return re.compile("|".join(map(re.escape, map(kana_fold, words))))


CRISIS = load_crisis()
CRISIS_RE = words_re(CRISIS["words"])
RESUME_RE = words_re(CRISIS["continue_words"])
RESUME_NOT_RE = words_re(CRISIS["continue_not"])


def crisis_hit(text) -> bool:
    """本人の自由入力に、希死念慮・自傷の言葉があるか。文は kana_fold にそろえてから照らす。"""
    return bool(CRISIS_RE.search(kana_fold(text)))


def wants_resume(text) -> bool:
    """窓口を案内した次の発言が「続ける」か(「続けたくない」「大丈夫じゃない」は当たらない)。"""
    t = kana_fold(text)
    return bool(RESUME_RE.search(t)) and not RESUME_NOT_RE.search(t)


def crisis_text(lead: list[str], tail: list[str]) -> str:
    """危ない言葉への決まった文: 入り口の受け止め(lead)→ 共通の相談窓口 → 入り口の続け方(tail)。
    入り口の文の {source} には、窓口の出典(「まもろうよ こころ」の URL)が入る。"""
    lines = [*lead, *CRISIS["contacts"], *tail]
    return "\n".join(x.replace(SOURCE_MARK, CRISIS["source"]) for x in lines)


# ----------------------------- 個人情報 -----------------------------
EDGE_RE = re.compile(r"^[\s、。,.，．・:：/／]+|[\s、。,.，．・:：/／]+$")


def hidden(spans=()) -> list[str]:
    """値にも返事にも出さない字: 伏せた印と、読む段が示した氏名・要配慮の部分(長い順。短い部分が長い部分の中で先に消えないように)。"""
    words = {nfkc(s) for s in spans if isinstance(s, str) and nfkc(s)}
    return [REDACTED, *sorted(words, key=len, reverse=True)]


def scrub(text, spans=()) -> str:
    """値から、伏せた印と氏名・要配慮の部分を除く。残る言葉が無ければ ""(呼ぶ側はその値を入れない)。"""
    t = nfkc(text)
    for w in hidden(spans):
        t = t.replace(w, " ")
    t = re.sub(r"\s+", " ", t)
    return EDGE_RE.sub("", t).strip()


def mask(text, spans=()) -> str:
    """覚えておく発言と、言う段に渡す発言から、氏名・要配慮の部分を「…」にする(伏せた印はそのまま)。"""
    t = nfkc(text)
    for w in hidden(spans)[1:]:
        t = t.replace(w, "…")
    return t


def leaks(reply, spans=()) -> list[str]:
    """返事に出ている、出してはいけない字。"""
    r = nfkc(reply)
    return [w for w in hidden(spans) if w in r]
