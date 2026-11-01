"""python -m evals.scenes: 場面の一覧を回して、コードの条件を判定する(費用 0。evals/scenes/runner.py)。
--real は実モデルで回し、採点の条件もジャッジで判定する(費用がかかる。evals/scenes/real.py。設計メモ §5 の 9 枚目)。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from evals import scenes as S
from evals.scenes import runner as R


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=R.__doc__.splitlines()[0])
    ap.add_argument("--entry", choices=list(S.ENTRIES), action="append",
                    help="回す入り口(何回でも。既定は全部)")
    ap.add_argument("--version", action="append",
                    help="入り口の版。「入り口=版」(何回でも。例: --version career=c2 --version baito=v5。入り口を移す先の版にもなる)か、"
                    "--entry を 1 つだけ渡すときの版だけ。既定は今の版: " + "、".join(f"{e} {v}" for e, v in R.CURRENT.items()))
    ap.add_argument("--scene", action="append", help="回す場面の id(何回でも)")
    ap.add_argument("-v", "--verbose", action="store_true", help="すべての条件と、返信を出す")
    ap.add_argument("--log", help="結果の JSON(results.json)と点(score.md)を書くディレクトリ")
    ap.add_argument("--real", action="store_true",
                    help="実モデルで回す(費用がかかる。--ledger と --limit-usd が要る。結果は evals/results/scenes-real-<tag>.jsonl)")
    ap.add_argument("--repeat", type=int, default=1, help="--real のとき、場面の一覧を何回回すか")
    ap.add_argument("--ledger", type=Path, help="--real の費用台帳(このカードのもの。evals/budget-conversation-layer.jsonl)")
    ap.add_argument("--limit-usd", type=float, help="--real で、台帳の累計がこの額に達したら止める")
    ap.add_argument("--read-model", help="--real で、読む段だけをこのモデルにする(例: claude-haiku-5-5。既定は本番と同じ)")
    ap.add_argument("--tag", help="--real の結果の名前(既定は日時)")
    ap.add_argument("--no-judge", action="store_true", help="--real で、採点の条件を判定しない(ジャッジの費用を使わない)")
    args = ap.parse_args(argv)
    versions = {}
    for v in args.version or []:
        if "=" in v:
            entry, _, ver = v.partition("=")
            if entry not in S.ENTRIES:
                ap.error(f"--version の入り口 {entry} は {', '.join(S.ENTRIES)} のどれか")
            versions[entry] = ver
        elif not args.entry or len(args.entry) != 1:
            ap.error("版だけの --version は --entry を 1 つだけ渡すときに使う(ほかは「入り口=版」)")
        else:
            versions[args.entry[0]] = v
    versions = versions or None
    if args.real:
        return _real(ap, args, versions)
    try:
        rows = R.run(args.entry, versions, args.scene)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    text = R.report(rows, args.verbose)
    print(text)
    if args.log:
        d = Path(args.log)
        d.mkdir(parents=True, exist_ok=True)
        (d / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        (d / "score.md").write_text(R.report(rows, verbose=True) + "\n", encoding="utf-8")
    return 1 if any(r["error"] for r in rows) else 0


def _real(ap, args, versions) -> int:
    from datetime import datetime

    from evals import budget
    from evals.scenes import real
    from line_runtime.config import load_env

    if args.ledger is None or args.limit_usd is None:
        ap.error("--real には --ledger と --limit-usd が要る")
    if args.repeat < 1:
        ap.error("--repeat は 1 以上")
    for e, v in (versions or {}).items():
        R.driver(e, v)  # 回せない版は、費用を使う前に止める
    load_env()  # 実モデルの鍵(リポジトリ直下の .env。環境変数があればそちら)。引数を確かめた後に読む
    try:
        _, stopped = real.run(args.entry, versions, args.scene, args.repeat, args.ledger, args.limit_usd, args.read_model,
                              args.tag or f"{datetime.now():%Y%m%d-%H%M%S}", judge=not args.no_judge,
                              log=lambda m: print(m, flush=True))
    except budget.BudgetExceeded as e:
        print(f"!! {e}", file=sys.stderr)
        return 2
    return 2 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
