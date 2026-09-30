"""評価結果の集計と比較(line-sim の評価ハーネスが使う)。標準ライブラリだけで書く(どの venv からでも動くように)。

結果ファイルの形(各ハーネスがこの形で書く):

    {
      "harness": "line-sim",
      "config": {"model": "...", ...},          # 比較するときに見出しに出す
      "directions": {"violations": "down", ...}, # "up" = 高いほど良い / "down" = 低いほど良い
      "runs": [
        {"case": "A-yoru", "rep": 0, "status": "ok", "metrics": {"violations": 1, "branch_within_3": true, ...},
         "detail": {...}},                      # detail は各ハーネス固有(会話ログなど)。ここでは読まない
        {"case": "pii", "rep": 1, "status": "error", "error": "TimeoutError: ..."}
      ]
    }

- 実行エラー(status=error)は失敗として採点しない。件数を別に出す。
- 値が None の指標(例: 推薦が出なかった回の「推薦までの秒数」)はその回を除いて平均する。
- 区間は 95%。割合は Wilson 区間、数値は平均 ± 1.96 * 標準誤差。
- diff は両方にあるケースだけで比べる(片方にしかないケースで平均が動かないように)。
  |差| が 1.96 * sqrt(seA^2 + seB^2) を超えたものだけ改善・悪化とし、超えないものは「差なし」とする。
  どちらかが1回しか実行していない、またはノイズ幅が 0 のときは「判定不可」。

    python3 evalkit/aggregate.py summary FILE
    python3 evalkit/aggregate.py diff BEFORE AFTER
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path

Z = 1.96


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _values(runs: list[dict], metric: str) -> list[float]:
    return [float(r["metrics"][metric]) for r in runs if r["metrics"].get(metric) is not None]


def _is_rate(runs: list[dict], metric: str) -> bool:
    return all(isinstance(r["metrics"][metric], bool) for r in runs if r["metrics"].get(metric) is not None)


def stat(values: list[float], rate: bool) -> dict:
    """n / 平均 / 標準誤差 / 95%区間 / 最小・最大。"""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "se": None, "lo": None, "hi": None, "min": None, "max": None}
    mean = sum(values) / n
    if rate:
        # Wilson 区間(全部成功/全部失敗でも幅がつぶれない)。se は区間幅から逆算して diff で使う。
        center = (mean + Z * Z / (2 * n)) / (1 + Z * Z / n)
        half = Z * math.sqrt(mean * (1 - mean) / n + Z * Z / (4 * n * n)) / (1 + Z * Z / n)
        lo, hi, se = center - half, center + half, half / Z
    else:
        se = statistics.stdev(values) / math.sqrt(n) if n > 1 else 0.0
        lo, hi = mean - Z * se, mean + Z * se
    return {"n": n, "mean": mean, "se": se, "lo": lo, "hi": hi, "min": min(values), "max": max(values)}


def summarize(doc: dict) -> dict:
    """全体と case ごとの集計。"""
    ok = [r for r in doc["runs"] if r.get("status", "ok") == "ok"]
    metrics = list(dict.fromkeys(m for r in ok for m in r["metrics"]))
    cases = list(dict.fromkeys(r["case"] for r in doc["runs"]))
    rate = {m: _is_rate(ok, m) for m in metrics}
    return {
        "metrics": metrics,
        "rate": rate,
        "overall": {m: stat(_values(ok, m), rate[m]) for m in metrics},
        "by_case": {c: {m: stat(_values([r for r in ok if r["case"] == c], m), rate[m]) for m in metrics}
                    for c in cases},
        "reps": max((r["rep"] for r in doc["runs"]), default=-1) + 1,
        "cases": cases,
        "errors": [r for r in doc["runs"] if r.get("status", "ok") != "ok"],
    }


def _fmt(s: dict, rate: bool) -> str:
    if s["mean"] is None:
        return "-"
    if rate:
        return f"{s['mean'] * 100:.0f}%"
    return f"{s['mean']:.3g}" if abs(s["mean"]) < 1 else f"{s['mean']:.1f}"


def _fmt_ci(s: dict, rate: bool) -> str:
    if s["mean"] is None:
        return "-"
    if rate:
        return f"{s['mean'] * 100:.0f}% [{s['lo'] * 100:.0f}-{s['hi'] * 100:.0f}]"
    return f"{_fmt(s, rate)} ±{Z * s['se']:.2g}"


def summary_md(doc: dict) -> str:
    """集計の Markdown(全体は 95%区間つき、case ごとは平均と min-max)。"""
    s = summarize(doc)
    n_runs = len(doc["runs"])
    lines = [
        f"- {len(s['cases'])} ケース × {s['reps']} 回 = {n_runs} 回実行(実行エラー {len(s['errors'])} 回は集計から除外)",
        "",
        "| 指標 | 全体(95%区間) | 良い向き |",
        "|---|---|---|",
    ]
    arrow = {"up": "高い", "down": "低い"}
    for m in s["metrics"]:
        lines.append(f"| {m} | {_fmt_ci(s['overall'][m], s['rate'][m])} | "
                     f"{arrow.get(doc.get('directions', {}).get(m), '-')} |")
    lines += ["", "| ケース | " + " | ".join(s["metrics"]) + " |", "|---" * (len(s["metrics"]) + 1) + "|"]
    for c in s["cases"]:
        cells = []
        for m in s["metrics"]:
            st = s["by_case"][c][m]
            spread = "" if st["n"] < 2 or s["rate"][m] or st["min"] == st["max"] else f" ({st['min']:.3g}-{st['max']:.3g})"
            cells.append(_fmt(st, s["rate"][m]) + spread)
        lines.append(f"| {c} | " + " | ".join(cells) + " |")
    for e in s["errors"]:
        lines.append(f"- 実行エラー {e['case']}#{e['rep']}: {str(e.get('error', ''))[:120]}")
    return "\n".join(lines) + "\n"


def diff(a: dict, b: dict) -> dict:
    """a(前)→ b(後)の差。指標ごとの全体差と、悪化した case の一覧。全体差は両方にあるケースだけで計算する。"""
    fa, fb = summarize(a), summarize(b)
    ca, cb = fa["cases"], fb["cases"]
    shared = [c for c in cb if c in ca]
    only = lambda d: {**d, "runs": [r for r in d["runs"] if r["case"] in shared]}
    sa, sb = summarize(only(a)), summarize(only(b))
    directions = {**a.get("directions", {}), **b.get("directions", {})}
    rows = []
    for m in [m for m in sb["metrics"] if m in sa["metrics"]]:
        x, y = sa["overall"][m], sb["overall"][m]
        if x["mean"] is None or y["mean"] is None:
            continue
        delta = y["mean"] - x["mean"]
        noise = Z * math.sqrt(x["se"] ** 2 + y["se"] ** 2)
        sign = {"up": 1, "down": -1}.get(directions.get(m), 0)
        if sign and (min(x["n"], y["n"]) < 2 or noise == 0):
            verdict = "判定不可"  # ばらつきが測れていないので、どんな差でも判定しない
        else:
            # 差がノイズ幅の内なら「差なし」(回数が少ないとばらつきの判断は弱いので、はっきりした差だけを改善・悪化にする)
            verdict = "-" if sign == 0 else "差なし" if abs(delta) <= noise else ("改善" if delta * sign > 0 else "悪化")
        rows.append({"metric": m, "rate": sb["rate"][m], "before": x, "after": y, "delta": delta,
                     "noise": noise, "verdict": verdict})
    worse = []
    for c in [c for c in sb["cases"] if c in sa["cases"]]:
        for m, sign in ((m, {"up": 1, "down": -1}.get(directions.get(m), 0)) for m in sb["metrics"]):
            x, y = sa["by_case"][c].get(m), sb["by_case"][c].get(m)
            if sign and x and y and x["mean"] is not None and y["mean"] is not None and (y["mean"] - x["mean"]) * sign < 0:
                worse.append({"case": c, "metric": m, "rate": sb["rate"][m], "before": x, "after": y})
    return {"rows": rows, "worse_cases": worse, "errors": (len(fa["errors"]), len(fb["errors"])),
            "shape": ((len(ca), sa["reps"]), (len(cb), sb["reps"])),
            "only_before": [c for c in ca if c not in shared], "only_after": [c for c in cb if c not in shared]}


def diff_md(a: dict, b: dict, name_a: str = "前", name_b: str = "後") -> str:
    d = diff(a, b)
    (ca, ra), (cb, rb) = d["shape"]
    lines = [
        f"# 評価の比較: {name_a} → {name_b}",
        "",
        f"- 前: `{json.dumps(a.get('config', {}), ensure_ascii=False)}`({ca} ケース × {ra} 回、実行エラー {d['errors'][0]})",
        f"- 後: `{json.dumps(b.get('config', {}), ensure_ascii=False)}`({cb} ケース × {rb} 回、実行エラー {d['errors'][1]})",
        (
            "- 判定は |差| が 95% のノイズ幅を超えたときだけ。「差なし」は差がノイズ内、「-」は良い向きが決まっていない指標。"
            "「判定不可」は1回しか実行していないなど、ばらつきが測れていない指標。"
        ),
        "- 全体の数字は、両方にあるケースだけで計算している。",
    ]
    if d["only_before"] or d["only_after"]:
        lines.append(f"- 注意: 前だけにあるケース {d['only_before'] or 'なし'} / 後だけにあるケース "
                     f"{d['only_after'] or 'なし'}(全体の数字から除外)")
    if d["errors"][0] != d["errors"][1]:
        lines.append("- 注意: 実行エラーの回数が違う。エラーで落ちた回が偏っていると、残った回の平均が有利/不利になる")
    lines += ["", "| 指標 | 前 | 後 | 差 | ノイズ幅 | 判定 |", "|---|---|---|---|---|---|"]
    for r in sorted(d["rows"], key=lambda r: {"悪化": 0, "改善": 1}.get(r["verdict"], 2)):
        k = 100 if r["rate"] else 1
        unit = "pt" if r["rate"] else ""
        lines.append(f"| {r['metric']} | {_fmt(r['before'], r['rate'])} | {_fmt(r['after'], r['rate'])} | "
                     f"{r['delta'] * k:+.3g}{unit} | ±{r['noise'] * k:.2g}{unit} | {r['verdict']} |")
    lines += ["", "## 悪化したケース(ノイズ判定なし、確認用)", ""]
    lines += [f"- {w['case']} / {w['metric']}: {_fmt(w['before'], w['rate'])} → {_fmt(w['after'], w['rate'])}"
              for w in d["worse_cases"]] or ["- なし"]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> None:
    if len(argv) == 2 and argv[0] == "summary":
        print(summary_md(load(argv[1])), end="")
    elif len(argv) == 3 and argv[0] == "diff":
        print(diff_md(load(argv[1]), load(argv[2]), Path(argv[1]).name, Path(argv[2]).name), end="")
    else:
        raise SystemExit(__doc__.split("\n\n")[-1])


if __name__ == "__main__":
    main(sys.argv[1:])
