"""一覧に無い職種名で recommend_jobs が0件になる問題(F-follow-new#1, sim-20260925-215735)を、台本つきで何度も回して確かめる。

模擬ユーザー(LLM)を使わず、ユーザーの返信を台本で固定する。LINE 側エージェントだけが揺れる。
recommend_jobs / relax_conditions の引数は LineRuntime.handle_stream のイベント(kind: "tools")から取る
(評価の結果ファイルには残らないため)。wait_repro.py と同じ作り。

    ./.venv/bin/python -m evals.jobtype_repro --n 5 --tag check

実 API を使うので費用がかかる。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# F-follow-new#1 の手順1〜3。渋谷のカフェの求人は a003 と b0303 の2件
CASE = {"user": "sim-new", "event": "visit", "text": None, "script": ["求人を探す", "飲食・カフェ", "渋谷"]}
EXPECT = {"a003", "b0303"}
SEARCH_TOOLS = ("recommend_jobs", "relax_conditions")


async def run_once(rt, meter) -> dict:
    from dip_mcp.data import State
    from evals.scenarios import prepare

    prepare(State(), {"user": CASE["user"], "event": CASE["event"]})
    k = meter.mark()
    turns, event, text = [], CASE["event"], CASE["text"]
    for step in range(len(CASE["script"]) + 1):
        calls, reply = [], None
        async for ev in rt.handle_stream(CASE["user"], event, text, None):
            if ev["kind"] == "tools":
                calls += [{"name": c["name"], "args": c["args"], "summary": c["summary"]} for c in ev["calls"]]
            elif ev["kind"] == "reply":
                reply = ev
        turns.append({"user": text, "bubbles": reply["bubbles"], "quick_replies": reply["quick_replies"],
                      "card_ids": [c["job_id"] for c in reply.get("cards", [])], "calls": calls})
        if step < len(CASE["script"]):
            event, text = "message", CASE["script"][step]
    searches = [{"turn": i + 1, "name": c["name"], "job_type": c["args"].get("job_type"), "area": c["args"].get("area"),
                 "result": c["summary"]}
                for i, t in enumerate(turns) for c in t["calls"] if c["name"] in SEARCH_TOOLS]
    shown = sorted({j for t in turns for j in t["card_ids"]})
    return {"turns": turns, "searches": searches, "shown": shown, "proposed_shibuya_cafe": bool(EXPECT & set(turns[-1]["card_ids"])),  # 「渋谷」への返答で
            "cost_usd": meter.summary(k)["cost_usd"]}


async def main(n: int, tag: str) -> None:
    from evals.meter import Meter
    from line_runtime import config
    from line_runtime.runtime import LineRuntime

    meter = Meter().install()
    doc = {"tag": tag, "model": config.MODEL, "effort": config.EFFORT, "script": CASE, "runs": []}
    out = HERE / "results" / f"jobtype-repro-{tag}-{datetime.now():%Y%m%d-%H%M%S}.json"  # run.py と同じ(ローカル時刻のファイル名)
    async with LineRuntime(in_memory=True) as rt:
        for i in range(n):
            try:
                r = await run_once(rt, meter)
            except Exception as e:  # noqa: BLE001  1件の失敗で全体を止めない
                r = {"error": f"{type(e).__name__}: {e}"}
            doc["runs"].append(r)
            ok = [x for x in doc["runs"] if "error" not in x]
            doc["summary"] = {"runs": len(ok), "errors": len(doc["runs"]) - len(ok),
                              "proposed_shibuya_cafe": sum(x["proposed_shibuya_cafe"] for x in ok),
                              "cost_usd": round(meter.summary()["cost_usd"], 4)}
            out.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
            print(f"[{tag} {i + 1}/{n}] " + (r.get("error") or json.dumps(
                {"searches": [(s["turn"], s["name"], s["job_type"], s["area"], s["result"]) for s in r["searches"]],
                 "shown": r["shown"], "last": r["turns"][-1]["bubbles"], "cost": r["cost_usd"]}, ensure_ascii=False)),
                flush=True)
    print(f"-> {out}  {json.dumps(doc['summary'])}")


if __name__ == "__main__":
    from line_runtime.config import load_env

    load_env()
    os.environ["DIP_STATE_PATH"] = str(Path(tempfile.mkdtemp()) / "state.json")
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    asyncio.run(main(args.n, args.tag))
