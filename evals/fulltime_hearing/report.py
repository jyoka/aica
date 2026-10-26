"""指標と関門の報告(設計メモ §6.4)。形ごとの表、ペルソナごとの表、合格の線、良くなったとみなす差。"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean

from evals.fulltime_hearing.actor import IRRITATIONS, SHORT_ANSWER

# 合格の線(§6.4)
FACTS_RATE_MIN = 0.7
COMPLETION_MIN = 0.8
SECONDS_MAX = {True: 180.0, False: 300.0}  # 履歴書あり / なし
TYPED_MAX = 150
LEAVE_LABELS = {"time": "時間", "irritation": "いらいら", "turn_cap": "ターン上限", "no_reply": "返さなかった",
                "ai_closed": "AI が閉じた", "declined": "本人が断った"}
STATUS_LABELS = {"filled": "", "dont_know": "覚えていない", "passed": "パス", "missing": "取れていない"}


def _m(xs: list[float]) -> float | None:
    return mean(xs) if xs else None  # 丸めは表に出すときだけ(良くなったかは丸める前の値で決める)


def _fmt(x, suffix: str = "") -> str:
    if x is None:
        return "-"
    return f"{round(x, 1):g}{suffix}" if isinstance(x, float) else f"{x}{suffix}"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{round(x * 100)}%"


def gate_counts(runs: list[dict]) -> dict:
    """関門ごとの件数(どれも 0 が合格)。wasted を数えない形(A)は None。"""
    wasted = [r["gates"]["wasted"] for r in runs if r["gates"]["wasted"] is not None]
    return {"made_up_numbers": sum(len(r["gates"]["made_up_numbers"]) for r in runs),
            "wasted": sum(wasted) if wasted else None,
            "two_questions": sum(len(r["gates"]["two_questions_turns"]) for r in runs),
            "leading": sum(len(r["gates"]["leading_turns"]) for r in runs),
            "hard_empty": sum(1 for r in runs if r["gates"]["hard_empty"])}


def summarize(runs: list[dict]) -> dict:
    """1 つの形(かペルソナ)の会話をまとめる。"""
    ms = [r["metrics"] for r in runs]
    total = sum(m["facts_total"] for m in ms)
    done = [m for m in ms if m["completed"]]
    leaves = defaultdict(int)
    for m in ms:
        if m["left"]:
            leaves[m["left"]] += 1
    return {
        "n": len(ms),
        "facts_rate": round(sum(m["facts_correct"] for m in ms) / total, 3) if total else None,
        "facts_correct_mean": _m([m["facts_correct"] for m in ms]),
        "facts_total_mean": _m([m["facts_total"] for m in ms]),
        "completion": round(len(done) / len(ms), 3) if ms else None,
        "left": sum(leaves.values()), "leaves": dict(leaves),
        "seconds_resume": _m([m["seconds"] for m in done if m["has_resume"]]),
        "seconds_no_resume": _m([m["seconds"] for m in done if not m["has_resume"]]),
        "seconds_all": _m([m["seconds"] for m in ms]),
        "typed_chars": _m([m["typed_chars"] for m in ms]),
        "button_ratio": _m([m["button_ratio"] * 100 for m in ms if m["button_ratio"] is not None]),
        "irritations": _m([m["irritations"] for m in ms]),
        "ai_turns": _m([m["ai_turns"] for m in ms]),
        "tired_ignored": sum(m["tired_ignored"] for m in ms),
        "invalid_buttons": sum(m["invalid_buttons"] for m in ms),
        "gates": gate_counts(runs),
    }


def verdicts(s: dict) -> dict:
    """合格の線ごとの判定(True / False / 測れない None)。関門は 1 つでも破れば False。"""
    t_ok = [s[k] <= SECONDS_MAX[resume] for k, resume in (("seconds_resume", True), ("seconds_no_resume", False))
            if s[k] is not None]
    gates = [v for v in s["gates"].values() if v is not None]
    return {"facts": None if s["facts_rate"] is None else s["facts_rate"] >= FACTS_RATE_MIN,
            "completion": None if s["completion"] is None else s["completion"] >= COMPLETION_MIN,
            "seconds": all(t_ok) if t_ok else None,
            "typed": None if s["typed_chars"] is None else s["typed_chars"] <= TYPED_MAX,
            "gates": all(v == 0 for v in gates)}


def improved(base: dict, other: dict) -> bool | None:
    """良くなったとみなす差(§6.4): 正しく取れた事実の平均が 1 項目以上増え、離脱した会話が増えない。"""
    if base["facts_correct_mean"] is None or other["facts_correct_mean"] is None:
        return None
    return other["facts_correct_mean"] - base["facts_correct_mean"] >= 1 and other["left"] <= base["left"]


def by(runs: list[dict], *keys: str) -> dict:
    out: dict = defaultdict(list)
    for r in runs:
        out[tuple(r[k] for k in keys) if len(keys) > 1 else r[keys[0]]].append(r)
    return out


def _yes(v: bool | None) -> str:
    return "-" if v is None else "○" if v else "×"


def report(doc: dict) -> str:
    runs = [r for r in doc["runs"] if r.get("status") == "ok"]
    c = doc["config"]
    forms = [f for f in c["forms"] if any(r["form"] == f for r in runs)]
    lines = [
        f"# 正社員のヒアリングの評価({doc['started_at']})",
        "",
        f"- 形: {', '.join(c['forms'])}、ペルソナ: {', '.join(c['personas'])}、回数: {c['repeat']}",
        f"- 本人役: `{c['sim_model']}`、ジャッジ: `{c['judge_model']}`(文の判定 {c['same_samples']} 回)",
        f"- この実行の費用: ${doc.get('spent_usd', 0):.4f}、台帳 `{c['ledger']}` の累計: ${doc.get('ledger_total_usd', 0):.4f}"
        f" / 止める額 ${c['limit_usd']:.2f}" + {"budget": "(止める額に達したので途中で止めた)",
                                              "interrupted": "(実行を途中で止めた)"}.get(doc.get("stopped"), ""),
        f"- 失敗した会話: {sum(1 for r in doc['runs'] if r.get('status') == 'error')} 件",
        "",
        "## 形ごと",
        "",
        ("| 形 | 会話 | 正しく取れた事実 | 平均(項目) | 完了率 | 離脱 | 推定時間 履歴書あり / なし(完了) | 打った字数 | ボタン% "
        "| いらいら | 数字を作った | wasted | 2 つ聞いた | 誘導 | HARD が空 |"),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    sums = {f: summarize(by(runs, "form")[f]) for f in forms}
    for f, s in sums.items():
        g = s["gates"]
        leaves = "、".join(f"{LEAVE_LABELS[k]} {v}" for k, v in s["leaves"].items()) or "0"
        lines.append(f"| {f} | {s['n']} | {_pct(s['facts_rate'])} "
                     f"| {_fmt(s['facts_correct_mean'])} / {_fmt(s['facts_total_mean'])} "
                     f"| {_pct(s['completion'])} | {leaves} "
                     f"| {_fmt(s['seconds_resume'], '秒')} / {_fmt(s['seconds_no_resume'], '秒')} | {_fmt(s['typed_chars'])} "
                     f"| {_fmt(s['button_ratio'])} | {_fmt(s['irritations'])} | {g['made_up_numbers']} | {_fmt(g['wasted'])} "
                     f"| {g['two_questions']} | {g['leading']} | {g['hard_empty']} |")
    lines += ["", "## 合格の線", "",
              (f"正しく取れた事実 {FACTS_RATE_MIN:.0%} 以上、完了率 {COMPLETION_MIN:.0%} 以上、推定時間は履歴書あり "
              f"{SECONDS_MAX[True]:.0f} 秒・なし {SECONDS_MAX[False]:.0f} 秒以内(完了した会話の平均)、打った字数 {TYPED_MAX} 字以内"
              "(平均)、関門はどれも 0 件。誘導・面接口調・2 つ聞く・数字を作ったは、実 API の会話でだけ意味がある(§6.2)。"
              "PRD v3 の受け入れチェック(§11.3)の置き換えは、まだ数えていない。"), "",
              "| 形 | 事実 | 完了率 | 推定時間 | 打った字数 | 関門 |", "|---|---|---|---|---|---|"]
    for f, s in sums.items():
        v = verdicts(s)
        lines.append(f"| {f} | {_yes(v['facts'])} | {_yes(v['completion'])} | {_yes(v['seconds'])} | {_yes(v['typed'])} "
                     f"| {_yes(v['gates'])} |")
    if len(sums) > 1:
        base = forms[0]
        lines += ["", f"## {base} との差(良くなったとみなす: 正しく取れた事実の平均が 1 項目以上増え、離脱が増えない)", "",
                  "| 形 | 事実の平均の差 | 離脱の差 | 良くなった |", "|---|---|---|---|"]
        for f in forms[1:]:
            b, o = sums[base], sums[f]
            diff = None if o["facts_correct_mean"] is None or b["facts_correct_mean"] is None else \
                round(o["facts_correct_mean"] - b["facts_correct_mean"], 1)
            lines.append(f"| {f} | {_fmt(diff)} | {o['left'] - b['left']} | {_yes(improved(b, o))} |")
    lines += ["", "## ペルソナごと", "",
              "| ペルソナ | 形 | 会話 | 正しく取れた事実(平均 / 項目) | 完了 | 離脱 | 推定時間(平均) | 打った字数 | いらいら |",
              "|---|---|---|---|---|---|---|---|---|"]
    for (pid, f), rs in sorted(by(runs, "persona", "form").items(), key=lambda x: (x[0][0], forms.index(x[0][1]))):
        s = summarize(rs)
        leaves = "、".join(f"{LEAVE_LABELS[k]} {v}" for k, v in s["leaves"].items()) or "0"
        lines.append(f"| {pid} | {f} | {s['n']} | {_fmt(s['facts_correct_mean'])} / {_fmt(s['facts_total_mean'])} "
                     f"| {sum(1 for r in rs if r['metrics']['completed'])}/{s['n']} | {leaves} | {_fmt(s['seconds_all'], '秒')} "
                     f"| {_fmt(s['typed_chars'])} | {_fmt(s['irritations'])} |")
    lines += ["", "## 本人役の崩れ(記録だけ)", "",
              f"- 疲れたのに {SHORT_ANSWER + 1} 字以上打った: {sum(s['tired_ignored'] for s in sums.values())} 回",
              f"- ラベルに無いボタンを押そうとした: {sum(s['invalid_buttons'] for s in sums.values())} 回",
              "", "## 会話ごと", ""]
    for r in runs:
        lines += conversation_md(r)
    return "\n".join(lines) + "\n"


def conversation_md(r: dict) -> list[str]:
    m, g = r["metrics"], r["gates"]
    end = "完了" if m["completed"] else f"離脱({LEAVE_LABELS.get(m['left'], m['left'])})"
    out = [(f"### {r['form']} / {r['persona']} #{r['rep'] + 1}: {end}、{m['seconds']} 秒、"
           f"正しく取れた事実 {m['facts_correct']}/{m['facts_total']}"),
           "", f"- 打った字数 {m['typed_chars']}、ボタン {_pct(m['button_ratio'])}、"
           f"AI の返信 {m['ai_turns']} 回" + ("(事実は会話の記録から取り出した)" if r.get("extracted") else "")]
    out += [f"- いらいら T{x['turn']}: {IRRITATIONS[x['kind']]}「{x['evidence']}」" for x in r["irritations"]]
    if g["made_up_numbers"]:
        out.append(f"- 数字を作った: {'、'.join(g['made_up_numbers'])}")
    if g["hard_empty"]:
        out.append(f"- HARD が空のまま result: {', '.join(g['hard_empty'])}")
    if g["leading_turns"]:
        out.append(f"- 誘導: {', '.join(f'T{t}' for t in g['leading_turns'])}")
    out += ["", "| 項目 | 事実表 | 取れた | 正しい |", "|---|---|---|---|"]
    for f in r["facts"]:
        got = f["got"] or {}
        shown = got.get("text") or STATUS_LABELS.get(got.get("status"), "")
        if got.get("status") in ("dont_know", "passed") and got.get("text"):
            shown += f"({STATUS_LABELS[got['status']]})"
        votes = f"(判定 {'/'.join('同じ' if v else '違う' for v in f['votes'])})" if f.get("votes") else ""
        out.append(f"| {f['label']} | {f['fact']} | {shown or '-'} | {_yes(f['correct'])}{votes} |")
    out += ["", "会話:", ""]
    for row in r["turns"]:
        out.append(f"- AI T{row['turn']}({row['seconds']} 秒): " + " / ".join(
            [*row["reply"]["bubbles"], *(f"[カード] {x}" for x in row["reply"]["flex"])]
            + ([f"[ボタン] {' / '.join(row['reply']['buttons'])}"] if row["reply"]["buttons"] else [])))
        if a := row.get("actor"):
            out.append(f"- 本人: {'[ボタン] ' if a['pressed'] else ''}{a['text']}" + ("(疲れ)" if a["tired"] else ""))
    out.append("")
    return out
