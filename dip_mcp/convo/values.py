"""決める 1: 値を当てる(設計メモ docs/design/conversation-layer.md §4.4)。act ごとの当て方と確かさの決まり。

層が決めるのは、どの欄に、どう当て、どの確かさにするか(plan)まで。欄の値の形(正社員の entry、アルバイト・パートの区切りのキー)と、
当てた後の入り口の決まり(未成年など)は入り口が受け持つ。

| act | 操作 | 確かさ |
|---|---|---|
| answer(今の問いの欄) | set: その欄に入れる | 直接聞く問いなら high、場面の二択などは low(入り口が決める) |
| answer・add(ほかの欄) | add: 空なら入れる。文の欄に値があれば言い足しとしてつなぐ | low(つないだ値も、前の値と low の低い方 = low) |
| correct | correct: 直した値に置きかえる | high(本人がはっきり直した。HARD も) |
| retract | retract: 欄を空にする | - |
| unsure | unsure: 本人の文のまま入れる(今ここで決めさせず、まとめで確かめる) | low |
| pass・dont_know(今の問い) | escape: 今の問いを飛ばす(入り口の今の決まり) | - |

値の act は、雑談や逆質問や別の入り口の話と同じ発言にあっても捨てない。氏名と要配慮の部分(act の name と sensitive)は、
値から除いてから当てる(guard.scrub)。除いて何も残らなければ当てない。
"""

from __future__ import annotations

from dip_mcp.convo import acts as A
from dip_mcp.convo.guard import scrub

JOIN = "、"  # 言い足しをつなぐ字


def plan(reading: dict | None, targets) -> list[dict]:
    """読む段の出力 -> 当てる操作の並び [{"op", "slot", "act", "text"}]。取り消しを先に(「取り消して、こっちで」の順に当てる)。
    targets は今の問いの欄。pass と dont_know は、欄が無いか今の問いの欄のときだけ escape にする(ほかの欄は空のまま)。"""
    targets = set(targets or ())
    hide = A.spans(reading)
    ops = []
    for a in (reading or {}).get("acts", []):
        name = a["act"]
        if name in A.VALUE_ACTS:
            text = scrub(a["text"], hide)
            if not text:
                continue
            current = a["slot"] in targets
            op = {"answer": "set", "add": "set", "correct": "correct", "unsure": "unsure"}[name]
            if op == "set" and not current:
                op = "add"
            ops.append({"op": op, "slot": a["slot"], "act": a, "text": text, "current": current})
        elif name == "retract":
            ops.append({"op": "retract", "slot": a["slot"], "act": a, "text": "", "current": a["slot"] in targets})
        elif name in ("pass", "dont_know") and a.get("slot") in (None, *targets):
            ops.append({"op": "escape", "slot": None, "act": a, "text": "", "current": True})
    return sorted(ops, key=lambda o: o["op"] != "retract")


def confidence(op: str, direct: bool) -> str:
    """当てる値の確かさ。direct は、今の問いが直接聞く問いか(入り口が決める)。"""
    if op == "set":
        return "high" if direct else "low"
    if op == "correct":
        return "high"
    return "low"


def merge(old: str, new: str) -> str:
    """言い足しをつなぐ。前の値にもう入っていれば前の値のまま。"""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or new in old:
        return old or new
    if old in new:
        return new
    return old + JOIN + new
