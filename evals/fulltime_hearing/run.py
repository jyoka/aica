"""正社員のヒアリングの評価を回す(設計メモ docs/design/fulltime-hearing.md §6)。使い方は evals/fulltime_hearing/README.md。

    ./.venv/bin/python -m evals.fulltime_hearing.run --form A --persona all --repeat 2 --limit-usd 2

実 API を使う(本人役とジャッジ。形 A はエージェントも)。費用は台帳(既定 evals/budget-fulltime-hearing.jsonl)に
1 会話ごとに 1 行足し、会話を始める前に累計が --limit-usd に達していたら止める(evals.run.run_cases と同じ)。
結果は evals/results/fulltime-hearing-<日時>-<形>.{json,md} に 1 会話ごとに上書き保存する(途中で止めても残る)。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from evals import budget
from evals.fulltime_hearing import judge
from evals.fulltime_hearing.conversation import MAX_AI_TURNS, run_conversation
from evals.fulltime_hearing.forms import FORMS, check_forms
from evals.fulltime_hearing.personas import load_personas, select
from evals.fulltime_hearing.report import report
from evals.run import JUDGE_MODEL, SIM_MODEL, run_cases
from line_runtime import config, llm

HERE = Path(__file__).resolve().parent.parent
LEDGER = HERE / "budget-fulltime-hearing.jsonl"


async def main(forms: list[str], persona_ids: str | None, repeat: int, limit: float, ledger: Path = LEDGER,
               max_turns: int = MAX_AI_TURNS, same_samples: int = judge.SAME_SAMPLES, client=None,
               out_dir: Path = HERE / "results", log=print) -> int:
    """回して結果を書く。止める額に達して止めたら 2、最後まで回したら 0。client を渡すと(テスト)費用を数えない。"""
    check_forms(forms)
    personas = select(load_personas(), persona_ids)
    log(budget.line(ledger, limit))
    budget.check(ledger, limit)  # 上限に達していたら、ランタイムを起動する前に止める
    if client is None:
        from evals.meter import Meter

        meter = Meter().install()
        client = llm.client()

        def spent() -> float:
            return meter.summary()["cost_usd"]
    else:
        def spent() -> float:
            return 0.0
    sim_model, judge_model = SIM_MODEL[config.PROVIDER], JUDGE_MODEL[config.PROVIDER]
    started = datetime.now()
    # 形の名前も入れる(別の形を同じ秒に並べて回すと、同じファイルに上書きし合っていた。task 362)
    out = Path(out_dir) / f"fulltime-hearing-{started:%Y%m%d-%H%M%S}-{'_'.join(forms)}"
    runs: list[dict] = []
    doc = {"harness": "fulltime-hearing", "started_at": f"{started:%Y-%m-%d %H:%M}",
           "config": {"forms": forms, "personas": [p["id"] for p in personas], "repeat": repeat,
                      "provider": config.PROVIDER, "model": config.MODEL, "sim_model": sim_model,
                      "judge_model": judge_model, "same_samples": same_samples, "max_turns": max_turns,
                      "ledger": str(ledger), "limit_usd": limit},
           "runs": runs, "spent_usd": 0.0, "stopped": None}
    out.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        doc["spent_usd"] = spent()
        doc["ledger_total_usd"] = budget.spent(ledger)
        out.with_suffix(".json").write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
        out.with_suffix(".md").write_text(report(doc))

    async with contextlib.AsyncExitStack() as stack:
        makers = {f: await stack.enter_async_context(FORMS[f]()) for f in forms}

        async def run_one(case: dict, rep: int) -> None:
            base = {"form": case["form"], "persona": case["persona"]["id"], "rep": rep}
            try:
                r = await run_conversation(makers[case["form"]](case["persona"]), case["persona"], client, sim_model,
                                           judge_model, max_turns, same_samples)
            except Exception as e:  # noqa: BLE001  1 件の失敗で全体を止めない
                log(f"   FAILED: {type(e).__name__}: {e}")
                runs.append(base | {"status": "error", "error": f"{type(e).__name__}: {e}"})
                return
            m = r["metrics"]
            log(f"   {'完了' if m['completed'] else '離脱 ' + str(m['left'])}、{m['seconds']} 秒、"
                f"事実 {m['facts_correct']}/{m['facts_total']}、いらいら {m['irritations']}")
            runs.append(base | {"status": "ok"} | r)

        cases = [({"id": f"{f}/{p['id']}", "form": f, "persona": p}, rep)
                 for rep in range(repeat) for f in forms for p in personas]
        try:
            stopped = await run_cases(cases, run_one, spent, ledger, out.name, log=log, after=save, limit=limit)
        except BaseException:
            doc["stopped"] = "interrupted"
            save()
            raise
    doc["stopped"] = "budget" if stopped else None
    save()
    log(f"この実行 ${doc['spent_usd']:.4f} / {budget.line(ledger, limit)}")
    log(f"-> {out}.md")
    return 2 if stopped else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m evals.fulltime_hearing.run",
                                 description="正社員のヒアリングの評価(本人役とジャッジは実 API)")
    ap.add_argument("--form", required=True,
                    help=f"比べる形(カンマ区切り。最初の形と差を比べる)。ある形: {', '.join(FORMS)}")
    ap.add_argument("--persona", default="all", help="ペルソナの id(カンマ区切り)か all")
    ap.add_argument("--repeat", type=int, default=1, help="形 × ペルソナごとの回数(§6.6 は 2)")
    ap.add_argument("--ledger", type=Path, default=LEDGER, help="費用台帳(既定 evals/budget-fulltime-hearing.jsonl)")
    ap.add_argument("--limit-usd", type=float, required=True, help="台帳の累計がこの額に達したら止める(Issue に書いた上限)")
    ap.add_argument("--max-turns", type=int, default=MAX_AI_TURNS, help="1 会話の AI の返信の上限(費用の歯止め)")
    ap.add_argument("--same-samples", type=int, default=judge.SAME_SAMPLES, help="文が同じ意味かを判定する回数")
    args = ap.parse_args(argv)
    args.forms = [f.strip() for f in args.form.split(",") if f.strip()]
    if not args.forms:
        ap.error("--form に形を 1 つ以上")
    if args.repeat < 1:
        ap.error("--repeat は 1 以上")
    if args.max_turns < 1:
        ap.error("--max-turns は 1 以上")
    if args.same_samples < 1:
        ap.error("--same-samples は 1 以上")
    return args


if __name__ == "__main__":
    from line_runtime.config import load_env

    load_env()
    # 形 A の dip の状態は実行ごとの一時ファイルに(開発中の data/state.json を汚さない)
    tmp = Path(tempfile.mkdtemp())
    os.environ["DIP_STATE_PATH"] = str(tmp / "state.json")
    a = parse_args()
    try:
        sys.exit(asyncio.run(main(a.forms, a.persona, a.repeat, a.limit_usd, a.ledger, a.max_turns, a.same_samples,
                                  log=lambda m: print(m, flush=True))))
    except budget.BudgetExceeded as e:
        print(f"!! {e}")
        sys.exit(2)
