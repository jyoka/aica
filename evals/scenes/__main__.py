"""python -m evals.scenes: 場面の一覧を回して、コードの条件を判定する(費用 0。evals/scenes/runner.py)。"""

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
    ap.add_argument("--version", help="入り口の版(--entry を 1 つだけ渡すとき。既定は今の版: "
                    + "、".join(f"{e} {v}" for e, v in R.CURRENT.items()) + ")")
    ap.add_argument("--scene", action="append", help="回す場面の id(何回でも)")
    ap.add_argument("-v", "--verbose", action="store_true", help="すべての条件と、返信を出す")
    ap.add_argument("--log", help="結果の JSON(results.json)と点(score.md)を書くディレクトリ")
    args = ap.parse_args(argv)
    if args.version and (not args.entry or len(args.entry) != 1):
        ap.error("--version は --entry を 1 つだけ渡すときに使う")
    versions = {args.entry[0]: args.version} if args.version else None
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


if __name__ == "__main__":
    sys.exit(main())
