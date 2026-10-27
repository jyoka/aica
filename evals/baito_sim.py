"""バイト探しの相棒(dip_mcp/baito)の検査: PRD v4.1 の参照資料の単体チェックとシミュレーターで、aica の移植版を回す。

    ./.venv/bin/python -m evals.baito_sim                 # 単体チェック 131 件 + 20 ペルソナ(モック LLM。費用 0)
    ./.venv/bin/python -m evals.baito_sim --real --runs 3 --ledger evals/results/budget-baito.jsonl --limit-usd 2
                                                          # P3: 実 LLM で 18 ペルソナ × runs 回(費用がかかる)

モック(既定): docs/prd/baito_harness を一時ディレクトリに写し、BAITO_HARNESS=aica_port(移植版。docs/prd/baito_harness/
aica_port.py)で `unit_checks_v4.py` と `simulate.py v4` を回す。写しの中で回すので、基準値(results_v4.json・transcripts/)は
上書きしない(レビュー M-10)。PRD §13-1 の P4 の条件で、§11-5 の基準値(results_v4.json)と比べる(acceptance)。
pytest は tests/test_baito_harness.py がこれを回す。

実 LLM(--real。PRD §13-1 の P3、レビュー B-3 の解釈):
- 対象は 20 ペルソナのうち、LLM の誤りを模す malformed・spam_high を除く 18 人。本人の発言はペルソナの台本のまま。
  モデルは line_runtime/baito_chat.py(トークと同じ LLM 層)で呼ぶ
- M-drop の「正しい更新」は、台本に付いた抽出(sim_v4 の extraction)とする。sim_v4 の dropped がそのまま照らす
- 率の母数は段ごとの「1 回目の呼び出し」(やり直しを除く)。合格は 1 回目で検証を通った数。代替はやり直しても通らなかった数
- 基準は §16-1 の 12(生成合格率 95% 以上、代替率 2% 以下、抽出合格率 98% 以上)と、§4-1 の合否指標がすべて 0
"""

from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "docs" / "prd" / "baito_harness"
PORT = "aica_port"
NORMAL = ("hs16", "univ", "homemaker", "office", "nopref", "unclear", "late_seg", "no_seg")  # §11-2 の通常ペルソナ
MOCK_ONLY = ("malformed", "spam_high")  # LLM の誤りを模すペルソナ(実 LLM では回さない)
TIMEOUT = 180  # 単体チェック・シミュレーターそれぞれの上限(秒。ふだんは数秒)
P3_TARGETS = {"generate_pass": 0.95, "fallback": 0.02, "extract_pass": 0.98}  # §16-1 の 12


def _env() -> dict:
    return dict(os.environ, BAITO_HARNESS=PORT, AICA_ROOT=str(ROOT), PYTHONDONTWRITEBYTECODE="1")


def run_reference(timeout: int = TIMEOUT) -> dict:
    """参照資料の写しで、移植版の単体チェックと 20 ペルソナを回す。基準値(写す前の results_v4.json)も返す。"""
    with tempfile.TemporaryDirectory(prefix="baito-v4-") as tmp:
        work = Path(tmp) / "baito_harness"
        shutil.copytree(REF, work, ignore=shutil.ignore_patterns("__pycache__"))
        unit = subprocess.run([sys.executable, "unit_checks_v4.py"], cwd=work, env=_env(), capture_output=True, check=False,
                              text=True, timeout=timeout)
        sim = subprocess.run([sys.executable, "simulate.py", "v4"], cwd=work, env=_env(), capture_output=True, text=True, check=False,
                             timeout=timeout)
        results = json.loads((work / "results_v4.json").read_text(encoding="utf-8")) if sim.returncode == 0 else None
        transcripts = {p.name: p.read_text(encoding="utf-8") for p in sorted((work / "transcripts").glob("v4_*.txt"))}
    lines = unit.stdout.strip().splitlines()
    return {"unit_returncode": unit.returncode, "unit_summary": lines[-1] if lines else "",
            "unit_fails": [x for x in lines if x.startswith("FAIL")], "sim_returncode": sim.returncode,
            "sim_stdout": sim.stdout, "sim_stderr": sim.stderr[-2000:], "results": results,
            "baseline": json.loads((REF / "results_v4.json").read_text(encoding="utf-8")), "transcripts": transcripts}


def acceptance(port: dict, base: dict) -> list[str]:
    """PRD §13-1 の P4 の 1: §4-1 の合否指標がすべて 0、§4-2 の「はい」の 4 項目が §11-5 の基準値の範囲内。"""
    bad = []
    agg = port["v4_agg"]
    for k, v in agg.items():
        if k == "max_turns":
            if v > 15:
                bad.append(f"max_turns={v}")
        elif v:
            bad.append(f"{k}={v}")
    by = {m["persona"]: m for m in port["v4"]}
    base_by = {m["persona"]: m for m in base["v4"]}
    for name in NORMAL:
        m, b = by[name], base_by[name]
        if m["turns"] > 15 or abs(m["turns"] - b["turns"]) > 1:  # Q-turns
            bad.append(f"{name}: turns={m['turns']}(基準 {b['turns']})")
        if m["per_phase"]["scenario_game"] < 3:  # Q-scenario
            bad.append(f"{name}: scenario={m['per_phase']['scenario_game']}")
    mid = sum(len(by[n]["mid_empty_end"]) for n in NORMAL)  # Q-mid
    if mid > 11:
        bad.append(f"mid_empty_end={mid}(基準 9+2)")
    bad += [f"{m['persona']}: review_rounds={m['review_rounds']}" for m in port["v4"] if m["review_rounds"] > 2]  # Q-review
    return bad


def check(timeout: int = TIMEOUT) -> tuple[dict, list[str]]:
    """モックの検査の全部: 単体チェックが全件合格し、シミュレーターが落ちず、acceptance に問題が無い。"""
    r = run_reference(timeout)
    bad = [] if r["unit_returncode"] == 0 else [f"unit_checks: {r['unit_summary']}", *r["unit_fails"]]
    if r["results"] is None:
        bad.append(f"simulate.py v4 の終了コード {r['sim_returncode']}: {r['sim_stderr']}")
    else:
        bad += acceptance(r["results"], r["baseline"])
    return r, bad


# ----------------------------- P3: 実 LLM -----------------------------


def _import_sim(work: Path):
    """写しの sim_v4 を、移植版を検査する形で import する(BAITO_HARNESS は import の前に入れる)。"""
    os.environ.update(BAITO_HARNESS=PORT, AICA_ROOT=str(ROOT))
    sys.path.insert(0, str(work))
    for name in ("sim_v4", "bank_v4", "catalog_v4", PORT):
        sys.modules.pop(name, None)
    return importlib.import_module("sim_v4")


class StageCounter:
    """段ごとの 1 回目の呼び出しと、その合否・代替を数える(ハーネスの _call_llm を包む。やり直しで消えるログに頼らない)。"""

    def __init__(self, harness):
        self.harness = harness
        self.orig = harness._call_llm
        self.stats: dict[str, dict] = {}

    def __enter__(self):
        def counted(fn, payload, validator, state, stage):
            calls = [0]

            def fn2(p):
                calls[0] += 1
                return fn(p)
            res = self.orig(fn2, payload, validator, state, stage)
            s = self.stats.setdefault(stage, {"first": 0, "first_ok": 0, "fallback": 0, "calls": 0})
            s["first"] += 1
            s["calls"] += calls[0]
            s["first_ok"] += int(res is not None and calls[0] == 1)
            s["fallback"] += int(res is None)
            return res
        self.harness._call_llm = counted
        return self

    def __exit__(self, *exc):
        self.harness._call_llm = self.orig


def rates(stats: dict) -> dict:
    gen = [stats.get(k, {}) for k in ("generate", "result")]
    first = sum(s.get("first", 0) for s in gen)
    ex = stats.get("extract", {})
    return {"generate_pass": sum(s.get("first_ok", 0) for s in gen) / first if first else None,
            "fallback": sum(s.get("fallback", 0) for s in gen) / first if first else None,
            "extract_pass": ex["first_ok"] / ex["first"] if ex.get("first") else None}


def p3_ok(r: dict, agg: dict) -> list[str]:
    bad = [f"{k}={v}" for k, v in agg.items() if k != "max_turns" and v] + ([f"max_turns={agg['max_turns']}"]
                                                                          if agg.get("max_turns", 0) > 15 else [])
    for k, target in P3_TARGETS.items():
        v = r.get(k)
        if v is None or (v < target if k != "fallback" else v > target):
            bad.append(f"{k}={v}(基準 {'≦' if k == 'fallback' else '≧'} {target})")
    return bad


def run_real(runs: int, ledger: Path, limit_usd: float, call=None, only: list[str] | None = None) -> dict:
    """P3 の測定。call は line_runtime.baito_chat.default_llm() と同じ形(テストは偽物を渡す)。"""
    from evals import budget
    from line_runtime import baito_chat, config
    from line_runtime.ledger import cost_usd

    with tempfile.TemporaryDirectory(prefix="baito-p3-") as tmp:
        work = Path(tmp) / "baito_harness"
        shutil.copytree(REF, work, ignore=shutil.ignore_patterns("__pycache__"))
        cwd, env = os.getcwd(), {k: os.environ.get(k) for k in ("BAITO_HARNESS", "AICA_ROOT")}
        os.chdir(work)
        try:
            sim = _import_sim(work)
            H = sim.H
            call = call or baito_chat.default_llm()
            personas = [p for p in sim.PERSONAS if p.name not in MOCK_ONLY and (not only or p.name in only)]
            made: list[baito_chat.SyncLLM] = []

            def real_llm(mode=None):
                llm = baito_chat.SyncLLM(call, H.render_prompt, H.prompt_stage)
                llm.next_extract, llm.mode = {}, None  # sim_v4 が台本の抽出を入れる(実 LLM は読まない。M-drop の正解)
                made.append(llm)
                return llm
            sim.MockLLM = real_llm
            # sim_v4 は 1 会話を 5 秒で打ち切る(モック用)。実 LLM では 1 会話 10 分までにする
            sim.signal = types.SimpleNamespace(signal=signal.signal, SIGALRM=signal.SIGALRM,
                                               alarm=lambda s: signal.alarm(600 if s else 0))
            res, spent, stopped = [], 0.0, False
            with StageCounter(H) as counter:
                for rep in range(runs):
                    for p in personas:
                        try:
                            budget.check(ledger, limit_usd)
                        except budget.BudgetExceeded:
                            stopped = True
                            break
                        log: list[str] = []
                        m = sim.run(copy.deepcopy(p), log)
                        usage = made[-1].used
                        made[-1].close()
                        cost = sum(cost_usd(config.MODEL, u.get("input_tokens", 0) - u.get("cache_read", 0)
                                            - u.get("cache_write", 0), u.get("output_tokens", 0), u.get("cache_write", 0),
                                            u.get("cache_read", 0)) for u in usage)
                        spent += cost
                        budget.record(ledger, "baito-p3", cost, case=p.name, rep=rep)
                        res.append(m | {"rep": rep, "llm_calls": len(usage), "cost_usd": round(cost, 4), "log": log})
                        print(f"{p.name:11} rep={rep} turns={m['turns']} reached={m['reached']} calls={len(usage)} "
                              f"${cost:.4f}", flush=True)
                    if stopped:
                        break
            stats = counter.stats
        finally:
            os.chdir(cwd)
            sys.path.remove(str(work))
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    agg = {"crash": sum(1 for m in res if m["crash"]), "loop": sum(1 for m in res if m["loop"]),
           "not_reached": [m["persona"] for m in res if not m["reached"]],
           **{k: sum(len(m[k]) for m in res) for k in ("minor_violation", "result_violation", "silent_empty_hard",
                                                         "irrelevant", "wasted", "repeated", "spam_hard_high",
                                                         "silent_low_hard")},
           "dropped": sum(m["dropped"] for m in res),
           "phase_turns_mismatch": sum(1 for m in res if m["phase_turns_mismatch"]),
           "phase_cap_violation": sum(1 for m in res if m["phase_cap_violation"]),
           "over_max_turns": sum(1 for m in res if m["over_max_turns"]),
           "max_turns": max((m["turns"] for m in res), default=0)}
    r = rates(stats)
    return {"runs": runs, "conversations": len(res), "stopped_by_budget": stopped, "spent_usd": round(spent, 4),
            "stats": stats, "rates": r, "agg": agg, "problems": p3_ok(r, agg), "results": res}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--real", action="store_true", help="P3: 実 LLM で回す(費用がかかる。--ledger と --limit-usd が要る)")
    ap.add_argument("--runs", type=int, default=3, help="--real のとき、1 ペルソナを何回回すか(PRD は 3)")
    ap.add_argument("--only", help="--real のとき、回すペルソナ(カンマ区切り)")
    ap.add_argument("--ledger", type=Path, help="--real の費用台帳(このカード用のものを渡す)")
    ap.add_argument("--limit-usd", type=float, help="--real で、台帳の累計がこの額に達したら止める")
    a = ap.parse_args(argv)
    if a.real:
        if a.ledger is None or a.limit_usd is None:
            ap.error("--real には --ledger と --limit-usd が要る")
        out = run_real(a.runs, a.ledger, a.limit_usd, only=a.only.split(",") if a.only else None)
        dest = ROOT / "evals" / "results" / f"baito-p3-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(json.dumps({k: out[k] for k in ("conversations", "spent_usd", "rates", "agg", "problems")},
                         ensure_ascii=False, default=str))
        print(f"結果: {dest}")
        return 1 if out["problems"] else 0
    t = time.perf_counter()
    r, bad = check()
    print(r["unit_summary"])
    print(r["sim_stdout"].split("=== v3")[0].strip())
    print("すべて合格" if not bad else "不合格:\n- " + "\n- ".join(bad), f"({time.perf_counter() - t:.1f}s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
