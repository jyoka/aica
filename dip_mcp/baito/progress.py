"""アルバイト・パートの相談で、プロフィールが育つのを見せるための「進み具合」(設計メモ docs/design/hearing-progress-ui.md §4・§5)。

何を数えるか、どのターンにプロフィールカードを付けるかは、ここ(dip のハーネスの側)が決める。LINE 側のランタイムは数えない
(line_runtime/progress_ui.py が、ここで作った progress から Flex を 0〜1 つ選ぶ)。ハーネスの本体(harness.py)は変えず、
session.turn が turn() の前後の状態からここを呼ぶ。ターンをまたいで覚えること(埋まったと数えた項目と、カードを出したか)は、
相談の状態(1 人の dict の "baito")の "progress" に置く(mem)。

- 全体(分母): 今の区分の表(SLOT_PRIORITY。区分が unknown のままなら COMMON)の HARD と MID。LOW と NG は入れない
- 埋まった(分子): 分母の項目のうち、一度でも値が入ったもの。「こだわりなし」も入れる。後で値が消えても(review の矛盾を
  「未定」にした、ルールで深夜を外した)埋まったままに数える。区分が変わったときだけ、新しい表で数え直す(新しい表に無い項目は
  数えないので、分子が減ることがある)
- 出すのは hard_filter の最初のターン(返事が hard_filter 以降の質問のターン)から。icebreak の間は出さない。
  入口のカードと職種の結果(quick_pick・quick_result。task 265、設計メモ baito-quick-start.md §7)と、「近くで探す」の 3 問と
  求人(near_*。task 266)の間も出さず、数えもしない(入口の答えは選び直せる。選び直して外した項目を「未定」にしないため)。
  入口のカードから始めた相談(QUICK_FLAG)は、[もっと絞る] で icebreak に入ったターンから出す(§7。3 問の答えがもう埋まっている)
- milestone: hard_filter(か、それより前)から scenario_game・reverse_pick・fill_gaps に移った最初のターン。1 回の相談で 1 回。
  review・result に直接移ったとき(「結果を見せて」、ターンの予算)は付けない(review が同じ役を持つ)
- 中学生以下で止めたターンと、希死念慮の言葉に窓口を案内したターンには出さない
"""

from __future__ import annotations

from dip_mcp.baito import harness as H

GROUPS = (("条件", H.HARD), ("働き方", H.MID))
NG_GROUP = "NG"
AFTER_HF = ("scenario_game", "reverse_pick", "fill_gaps")
# 値の表示名だけでは何の項目か分からないもの(「好き」「OK」「どちらでも」)は、プロフィールに表示名を付けて出す
LABELED = {"customer_facing", "school_permission", "exam_flex", "sudden_leave", "fuyou", "side_job_allowed", "energy_left",
           "remote", "weekday_or_weekend"}
RULE_TAIL = "（ルール）"  # ルールの NG に付ける(review のカード render_card と同じ)


def table(h: H.State) -> list[str]:
    """数える項目(分母)。表のいちばん上から: HARD、MID の順(同じ重みは表の順)。"""
    pri = H.active_slots(h)
    return sorted((s for s, p in pri.items() if p >= H.MID), key=lambda s: -pri[s])


def _inferred(e: dict) -> bool:
    """推し量った値か(「?」を付ける)。確信度が low のもの(場面の二択・逆選択、別の質問への文の答え)。
    icebreak のボタン(「バイト代、何に使いたい？」)は本人が選んだ答えなので、low でも推し量ったとはしない。"""
    if e.get("confidence") != "low":
        return False
    frm = str(e.get("from") or "")
    q = H.qmap(H.BANK).get(frm.split(":", 1)[1]) if frm.startswith("choice:") else None
    return not (q and q["phase"] == "icebreak")


def short_value(slot: str, v) -> str:
    """プロフィールカードに出す値。表示名だけで分かるものは表示名、分からないものと「こだわりなし」は項目名を前に付ける。"""
    text = H.value_ja(slot, v)
    if slot in LABELED or v == H.NOPREF or (slot == "age_band" and v == "declined"):
        return f"{H.SLOTS[slot]['label']} {text}"
    return text


def _item(h: H.State, slot: str, seen: set) -> dict:
    e = h.slots.get(slot)
    filled = H.is_filled(e)
    return {"label": H.SLOTS[slot]["label"], "value": short_value(slot, e["value"]) if filled else None,
            "inferred": filled and _inferred(e), "undecided": not filled and slot in seen}


def groups(h: H.State, seen: set) -> list[dict]:
    """プロフィールのグループ: 条件(HARD)、働き方(MID)、NG(本人の NG と、ルールの NG に「（ルール）」)。LOW は出さない。
    項目の value が None なら、まだ答えていない(undecided なら、一度埋まって「未定」になった)。"""
    tbl = table(h)
    pri = H.active_slots(h)
    out = [{"name": name, "items": [_item(h, s, seen) for s in tbl if pri[s] == p]} for name, p in GROUPS]
    ng = [{"value": n} for n in h.user_ng] + [{"value": n + RULE_TAIL} for n in h.derived_ng if n not in h.user_ng]
    out.append({"name": NG_GROUP, "items": ng})
    return out


def fresh_mem(h: H.State) -> dict:
    return {"seen": [], "card": False, "restarts": h.restarts}


def before(h: H.State) -> dict:
    """turn() の前に覚えておくこと。filled は、覚えが無い相談(この UI の前に始めた相談)の数え始めに使う。"""
    return {"phase": h.phase, "result": bool(h.result), "restarts": h.restarts,
            "filled": [s for s in table(h) if H.is_filled(h.slots.get(s))]}


def update(h: H.State, mem: dict | None, prev: dict, reply: dict) -> tuple[dict | None, dict]:
    """turn() の後の状態から、このターンの progress と、次のターンに持ち越す mem を返す。progress は、出さないターンなら None。
    reply は turn() の戻り値(done・view)。"""
    if not mem:  # 覚えが無い(この UI の前に始めた相談): turn() の前に埋まっていた項目から数え始める(全部を「いま埋まった」にしない)
        mem = dict(fresh_mem(h), seen=list(prev.get("filled", [])) if prev.get("restarts") == h.restarts else [])
    elif mem.get("restarts") != h.restarts:  # 最初から(やり直し)なら数え直す
        mem = fresh_mem(h)
    if h.phase in H.QUICK_PHASES:  # 入口の答えは選び直せるので、入口の間は数えない(icebreak に入ったターンから数える)
        return None, mem
    tbl = table(h)
    old = set(mem["seen"]) & set(tbl)  # 区分が変わったら、新しい表に無い項目は数えない
    seen = old | {s for s in tbl if H.is_filled(h.slots.get(s))}
    mem = dict(mem, seen=[s for s in tbl if s in seen])
    crisis = bool((reply.get("view") or {}).get("crisis"))
    if (h.phase == "icebreak" and H.QUICK_FLAG not in h.flags) or h.blocked or crisis:
        return None, mem
    milestone = not mem["card"] and prev["phase"] in ("icebreak", "hard_filter") and h.phase in AFTER_HF
    if milestone:
        mem["card"] = True
    complete = bool(reply.get("done") and h.result and not prev["result"])
    added = []
    for s in tbl:
        if s in seen and s not in old:
            e = h.slots[s]
            added.append({"label": H.SLOTS[s]["label"], "value": H.value_ja(s, e["value"]), "inferred": _inferred(e)})
    jobs = []
    if complete:
        jobs = [{"name": H.CATALOG_BY_ID[r["job_id"]]["name"], "reason": r["reason"]}
                for r in h.result["recommendations"] if r["job_id"] in H.CATALOG_BY_ID]
    return {"filled": len(seen), "total": len(tbl), "added": added, "groups": groups(h, seen),
            "milestone": milestone, "review": h.phase == "review", "complete": complete, "jobs": jobs}, mem
