"""逆質問に答える事実のデータ(設計メモ docs/design/conversation-layer.md §4.6、§4.11。ADR-0003)。

言う段が逆質問(act の ask_service、ask_advice)に答えるときは、ここで渡す事実だけを使う。事実はデータにだけ置き、運営者が確かめる。
aica 全体の事実は dip_mcp/convo/facts.json に 1 つ置き、入り口の facts.json(入り口の定義 entry.json の facts)は足すだけ。

facts.json の形:
- ask_service: aica とこの相談についての事実(誰か、何に使うか、保存の期間と消し方、求人は架空のデモであること)
- ask_advice: 求人、会社、相場を聞かれたときの事実(相場の数は持たない。答えないことも事実として書く)
- where(入り口だけ。無くてよい): {入り口の区切りの名前: 「いまは〜を聞いています」の文}。「あと何問?」に、今どこかで答える
- known(入り口だけ。無くてよい): 前の相談や履歴書で分かっている値があるときに足す文(「聞かずに進めます」)
- checked: 運営者が確かめた日(まだなら null)。note、sources は人が読む説明
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
KINDS = ("ask_service", "ask_advice")  # 事実の種類(act の名前と同じ)
FACT_MAX = 80  # 1 つの事実の字数(言う段の入力を長くしない。逆質問の答えの吹き出しは 120 字まで)


def _lines(name: str, kind: str, v) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or any(not isinstance(x, str) or not x.strip() or len(x) > FACT_MAX for x in v):
        raise ValueError(f"{name}: 事実 {kind} は {FACT_MAX} 字までの文のリスト")
    return [x.strip() for x in v]


def read(path: Path) -> dict:
    """1 つの facts.json を読み、形を確かめる。壊れていれば ValueError(読み込むとき = dip の起動で止まる)。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    out = {k: _lines(str(path), k, d.get(k)) for k in KINDS}
    where = d.get("where") or {}
    if not isinstance(where, dict) or any(not isinstance(v, str) or not v.strip() for v in where.values()):
        raise ValueError(f"{path}: where は {{区切りの名前: 文}}")
    known = d.get("known")
    if known is not None and (not isinstance(known, str) or not known.strip()):
        raise ValueError(f"{path}: known は文")
    return out | {"where": dict(where), "known": known, "checked": d.get("checked")}


COMMON = read(HERE / "facts.json")


def load(path: Path | None) -> dict:
    """入り口の事実(path。無ければ aica 全体の事実だけ)。種類ごとに、aica 全体の事実の後に入り口の事実を足す(同じ文は 1 つ)。"""
    mine = read(path) if path is not None else {k: [] for k in KINDS} | {"where": {}, "known": None, "checked": None}
    out = {k: list(dict.fromkeys([*COMMON[k], *mine[k]])) for k in KINDS}
    return out | {"where": mine["where"], "known": mine["known"], "checked": {"common": COMMON["checked"], "entry": mine["checked"]}}


def pick(facts: dict, kinds, where: str | None = None, known: bool = False) -> list[str]:
    """逆質問に渡す事実: 聞かれた種類(kinds。act の名前)の事実と、今どこか(where。ask_service のとき)と、
    分かっている値があればその文(known。ask_service のとき)。"""
    kinds = [k for k in KINDS if k in set(kinds)]
    out = []
    if "ask_service" in kinds:
        if where and facts["where"].get(where):
            out.append(facts["where"][where])
        if known and facts["known"]:
            out.append(facts["known"])
    for k in kinds:
        out += facts[k]
    return list(dict.fromkeys(out))
