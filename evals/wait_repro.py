"""断っていないのに wait を記録する問題を、台本つきで何度も回して発生率を測る。

模擬ユーザー(LLM)を使わず、ユーザーの返信を台本で固定する。LINE 側エージェントだけが揺れる。
record_conversation_outcome の引数は LineRuntime.handle_stream のイベント(kind: "tools")から取る
(評価の結果ファイルには残らないため)。

    ./.venv/bin/python -m evals.wait_repro --case consider --n 10 --tag check
    ./.venv/bin/python -m evals.wait_repro --case B-exit --n 3 --tag check    # 正しく断ったときは記録されること

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

# 台本つきのケース。expect_refusal が False のものは、wait が記録されたら誤記録(wrongful_wait)。
CASES = {
    "visit-no": {"user": "sim-new", "event": "visit",
                 "script": ["今はいい"], "expect_refusal": True},
    "B-exit": {"user": "sim-exit", "event": "form_exit",
               "script": ["今はいい"], "expect_refusal": True},
    # 「考えます」は迷いのサイン: 迷いのポイントを聞いて続ける(wait にしない)。その後に「今はいい」なら記録する
    "consider": {"user": "sim-new", "event": "visit",
                 "script": ["求人を探す", "渋谷で日中のカフェ。未経験です", "ありがとう、考えます"], "expect_refusal": False},
    "consider-then-no": {"user": "sim-new", "event": "visit",
                         "script": ["求人を探す", "渋谷で日中のカフェ。未経験です", "ありがとう、考えます", "今はいい"],
                         "expect_refusal": True},
}


async def run_once(rt, meter, case: dict) -> dict:
    from dip_mcp import logic
    from dip_mcp.data import State
    from evals.checks import REFUSAL
    from evals.scenarios import prepare
    from line_runtime.runtime import EVENTS

    prepare(State(), {"user": case["user"], "event": case["event"]})
    facts = None
    if EVENTS[case["event"]][1] == "push":
        gate = logic.push_trigger(State(), case["user"], case["event"])
        if not gate["allowed"]:
            raise RuntimeError(f"dip がプッシュを許可しなかった: {gate['reason']}")
        facts = gate["facts"]
    k = meter.mark()
    turns, event, text = [], case["event"], None
    msgs = case["script"]
    for step in range(len(case["script"]) + 1):
        calls, reply = [], None
        async for ev in rt.handle_stream(case["user"], event, text, facts):
            if ev["kind"] == "tools":
                calls += [{"name": c["name"], "args": c["args"], "summary": c["summary"]} for c in ev["calls"]]
            elif ev["kind"] == "reply":
                reply = ev
        facts = None
        turns.append({"user": text, "bubbles": reply["bubbles"], "quick_replies": reply["quick_replies"],
                      "calls": calls})
        if step < len(case["script"]):
            event, text = "message", case["script"][step]
    outcomes = [{"turn": i + 1, "user": t["user"], **{a: c["args"].get(a) for a in ("outcome", "reason", "confidence")},
                 "effect": c["summary"]}
                for i, t in enumerate(turns) for c in t["calls"] if c["name"] == "record_conversation_outcome"]
    # ツールが受け付けた(記録された)wait だけを数える。安全弁に拒否された呼び出しは数えない
    recorded = [o for o in outcomes if o["outcome"] == "wait" and "記録:" in (o["effect"] or "")]
    refused = any(REFUSAL.search(m or "") for m in msgs)
    return {"turns": turns, "outcomes": outcomes,
            "wait_recorded": bool(recorded),
            "wrongful_wait": bool(recorded) and not refused,  # evals/checks.py の「断られていないのに wait」と同じ基準
            "wait_attempted": any(o["outcome"] == "wait" for o in outcomes),
            "cost_usd": meter.summary(k)["cost_usd"]}


async def main(case_id: str, n: int, tag: str) -> None:
    from evals.meter import Meter
    from line_runtime import config
    from line_runtime.runtime import LineRuntime

    meter = Meter().install()
    case = CASES[case_id]
    doc = {"case": case_id, "tag": tag, "model": config.MODEL, "effort": config.EFFORT,
           "script": case, "runs": []}
    out = HERE / "results" / f"wait-repro-{tag}-{case_id}-{datetime.now():%Y%m%d-%H%M%S}.json"  # run.py と同じ(ローカル時刻のファイル名)
    async with LineRuntime(in_memory=True) as rt:
        for i in range(n):
            try:
                r = await run_once(rt, meter, case)
            except Exception as e:  # noqa: BLE001  1件の失敗で全体を止めない
                r = {"error": f"{type(e).__name__}: {e}"}
            doc["runs"].append(r)
            ok = [x for x in doc["runs"] if "error" not in x]
            doc["summary"] = {"runs": len(ok), "errors": len(doc["runs"]) - len(ok),
                              "wrongful_wait": sum(x["wrongful_wait"] for x in ok),
                              "wait_recorded": sum(x["wait_recorded"] for x in ok),
                              "wait_attempted": sum(x["wait_attempted"] for x in ok),
                              "cost_usd": round(meter.summary()["cost_usd"], 4)}
            out.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
            print(f"[{tag} {case_id} {i + 1}/{n}] " + (r.get("error") or json.dumps(
                {"outcomes": [(o["turn"], o["outcome"], o["reason"]) for o in r["outcomes"]],
                 "last": r["turns"][-1]["bubbles"], "cost": r["cost_usd"]}, ensure_ascii=False)), flush=True)
    print(f"-> {out}  {json.dumps(doc['summary'])}")


if __name__ == "__main__":
    from line_runtime.config import load_env

    load_env()
    os.environ["DIP_STATE_PATH"] = str(Path(tempfile.mkdtemp()) / "state.json")
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=list(CASES), default="consider")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    asyncio.run(main(args.case, args.n, args.tag))
