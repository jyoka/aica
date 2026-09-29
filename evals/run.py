"""シナリオ評価: 模擬ユーザー(LLM)と LINE 側ランタイムを会話させ、設計書のルールと KPI で採点する。

各シナリオはそのユーザーの dip の状態を初期に戻してから走る(評価全体で独立した一時ファイル)。
シナリオの一覧は画面と共通(scenarios.json)。どれも AI 側の1通目(挨拶・プッシュ)から始まる(evals/scenarios.py)。
指標の cost_usd・llm_calls はランタイム側の LLM 呼び出しだけ(模擬ユーザーとジャッジは除く)。

各シナリオを --repeat 回(既定1)くり返し、集計と比較は evalkit(リポジトリ直下の evalkit/)に任せる。
結果は1回ごとに書き出す(途中で止めても、そこまでの結果は残る)。実 API を使うので費用がかかる。

    ./.venv/bin/python -m evals.run [--only B-exit,T-side-job] [--judge] [--repeat 2] [--max-turns 8]

費用(PRD aica-v2-fulltime の予算 $5):
- エージェントは本番のモデル(line_runtime.config.MODEL)。模擬ユーザーとジャッジは安いモデル(SIM_MODEL・JUDGE_MODEL)。
- 1会話のエージェントのターンは --max-turns(既定8)まで。シナリオの max_turns が小さければそちら。
- 実行ごとに費用台帳(evals/budget.jsonl、エージェント・模擬ユーザー・ジャッジの合計)の累計を出し、
  シナリオを始める前に累計が $4 に達していたら止める(evals/budget.py)。

事実性(作り話)と雇用区分の切り替え(set_search_track)は evals/checks.py がコードで判定する。
--judge は相談の深さ(目的・掘り下げ・選択肢・提案理由を根拠の発言つき)とトーンを LLM ジャッジで見る(evals/judge.py)。
    python3 evalkit/aggregate.py diff evals/results/sim-A.json evals/results/sim-B.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "evalkit"))
from aggregate import summary_md  # isort: skip

from evals import budget
from line_runtime import config, llm

# 模擬ユーザーとジャッジは安いモデル。OpenAI の料金表(https://developers.openai.com/api/docs/pricing、2026-09-29 確認)
# で gpt-6-luna は入力 $0.10 / 出力 $0.50(1M トークン)で、本番の gpt-6-sol($2.00 / $10.00)の 1/20。
# 構造化出力と Responses API に対応している。Anthropic の場合は Haiku 4.5
SIM_MODEL = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-4-5"}
JUDGE_MODEL = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-4-5"}
MAX_TURNS = 8  # 1会話のエージェントのターンの上限(費用の上限)

SIM_SYSTEM = """あなたは LINE で仕事探し(アルバイト・パートから正社員まで)のアシスタントと話しているユーザーです。次の設定だけを根拠に、短い話し言葉(1〜2文)で返します。
- 選択肢(クイックリプライ)が合っていれば、その文言をそのまま使う。
- 設定にないことを聞かれたら「特にない」「わからない」など正直に答える。
- 会話を終える台詞を言ったら end を true にする。アシスタントが会話を終えた(もう質問していない)ときも end を true にしてよい。
あなたの設定:
{persona}"""


ACTION_LABEL = {"resume_upload": "ファイルを選ぶ(履歴書)", "voice_consult": "話して伝える(音声・約5分)"}


def _format_agent(r: dict) -> str:
    parts = list(r["bubbles"])
    for c in r["cards"]:
        parts.append(f"[求人カード] {c['title']} / {c.get('employment_type', '')} / {c['pay_text']} / {c['shift']} / "
                     f"理由: {'・'.join(c.get('reasons', []))}")
    for b in r["buttons"]:
        parts.append(f"[ボタン] {b['label']}")
    for a in r.get("actions", []):
        parts.append(f"[操作] {ACTION_LABEL.get(a, a)}")
    if r["quick_replies"]:
        parts.append("[選択肢] " + " / ".join(r["quick_replies"]))
    return "\n".join(parts)


class SimTurn(BaseModel):
    text: str = Field(description="ユーザーとして送る返信(1〜2文)")
    end: bool = Field(description="この返信で会話を終えるなら true")


async def sim_reply(client, persona: str, transcript: list[dict]) -> dict:
    """模擬ユーザーの次の発言。構造化出力で受け取り(自由記述の JSON は壊れやすい)、空なら1回だけ取り直す。"""
    # どのシナリオも AI 側の1通目から始まるので、模擬ユーザーから見て user から始まる
    msgs = [{"role": "user" if t["who"] == "agent" else "assistant", "content": t["text"]} for t in transcript]
    for _ in range(2):
        turn = await llm.parse(client, model=SIM_MODEL[config.PROVIDER], max_tokens=1000, schema=SimTurn,
                               system=SIM_SYSTEM.format(persona=persona), messages=msgs)
        if turn and turn.text.strip():
            return turn.model_dump()
    return {"text": "", "end": True}


def _usage(meter, marks: list[tuple[int, int]]) -> dict:
    """1会話のエージェント側の呼び出しの集計(コスト・呼び出し回数・キャッシュのヒット率)。"""
    from evals.meter import Meter

    sub = Meter()
    sub.calls = [c for a, b in marks for c in meter.calls[a:b]]
    u = sub.summary()
    return {"cost_usd": u["cost_usd"], "llm_calls": u["llm_calls"], "cache_hit_rate": u["cache_hit_rate"]}


def _wants_upload(sc: dict, r: dict, n_turns: int) -> bool:
    """履歴書を持つペルソナ(upload_resume)がアップロードするか: AI が履歴書の操作ボタンを出したら、そのとき。
    upload_by_turn のターンまでに出なければ、そこでアップロードする(アップロード後の1問を必ず見るため)。"""
    return "resume_upload" in (r.get("actions") or []) or n_turns >= sc.get("upload_by_turn", 10 ** 6)


async def run_scenario(rt, client, meter, sc: dict, use_judge: bool, max_turns: int = MAX_TURNS) -> dict:
    from dip_mcp import logic
    from dip_mcp.data import State
    from evals.checks import conversation_checks
    from evals.judge import judge
    from evals.scenarios import load_resume, prepare, upload_resume
    from line_runtime.runtime import EVENTS

    prepare(State(), sc)  # 新しい会話は最初のイベント(visit・revisit・プッシュ)が始める
    push_facts = None
    if EVENTS[sc["event"]][1] == "push":  # 本番と同じく、プッシュしてよいかは dip が決める
        gate = logic.push_trigger(State(), sc["user"], sc["event"])
        if not gate["allowed"]:
            raise RuntimeError(f"dip がプッシュを許可しなかった: {gate['reason']}")
        push_facts = gate["facts"]
    cap = min(sc.get("max_turns", max_turns), max_turns)
    transcript, turns, user_msgs, seconds, marks = [], [], [], [], []
    upload = None

    async def agent_turn(event: str, text: str | None, facts: dict | None = None) -> None:
        k = meter.mark()
        t0 = time.perf_counter()
        r = await rt.handle(sc["user"], event, text, facts)
        seconds.append(time.perf_counter() - t0)
        marks.append((k, meter.mark()))  # このターンのエージェント側の呼び出し(模擬ユーザーはこの後に呼ぶ)
        turns.append(r)
        transcript.append({"who": "agent", "text": _format_agent(r)})

    user_msgs.append(None)  # 1通目は AI 側から(本人の発言なし)
    await agent_turn(sc["event"], None, push_facts)
    script = sc.get("script") or []
    while len(turns) < cap:
        if sc.get("upload_resume") and upload is None and _wants_upload(sc, turns[-1], len(turns)):
            facts = upload_resume(State(), sc)  # 本番の /api/resume/.../upload と同じく dip に保存してから
            upload = {"turn": len(turns), "shown": facts["shown"]}
            transcript.append({"who": "user", "text": f"[{facts['file']}をアップロード]"})
            user_msgs.append(None)
            await agent_turn("resume_upload", None, facts)
            continue
        step = len(turns) - 1
        if step < len(script):  # 台本があるシナリオは、その返信を使う(確認したい操作を確実に起こすため)
            nxt = {"text": script[step], "end": step == len(script) - 1}
        else:
            nxt = await sim_reply(client, sc["persona"], transcript)
        text = (nxt.get("text") or "").strip()
        if not text:  # 模擬ユーザーが何も返さなかった = 会話を離れた
            break
        transcript.append({"who": "user", "text": text})
        user_msgs.append(text)
        await agent_turn("message", text)
        if nxt.get("end"):
            break
    result = {"id": sc["id"], "user": sc["user"], "push_facts": push_facts,
              **conversation_checks(sc, turns, user_msgs, push_facts, upload),
              "upload_turn": upload["turn"] + 1 if upload else None,
              "latency_p50": round(statistics.median(seconds), 1), "latency_max": round(max(seconds), 1),
              **_usage(meter, marks), "transcript": transcript,
              "tools": [[t["name"] for t in r["tools"]] for r in turns]}
    if use_judge:
        resume = sc.get("upload_resume") or sc.get("seed_resume")
        result["judge"] = await judge(client, JUDGE_MODEL[config.PROVIDER], transcript,
                                      load_resume(resume)["resume"] if resume else None)
    return result


# 良い向き(evalkit の diff で改善/悪化を判定するのに使う)
DIRECTIONS = {"branch_within_3": "up", "outcome_recorded": "up", "outcome_as_expected": "up", "violations": "down",
              "fabrications": "down", "track_ok": "up", "cache_hit_rate": "up", "llm_calls": "down",
              "latency_p50": "down", "latency_max": "down", "cost_usd": "down",
              "depth_purpose": "up", "depth_dig": "up", "depth_options": "up", "depth_reasons": "up",
              "tone": "up", "design_fit": "up"}


def to_run(r: dict, rep: int) -> dict:
    """run_scenario の結果を evalkit の1行に。"""
    from evals.judge import depth_metrics

    if "error" in r:
        return {"case": r["id"], "rep": rep, "status": "error", "error": r["error"]}
    j = r.get("judge") or {}
    metrics = {k: r[k] for k in ("branch_within_3", "outcome_recorded", "outcome_as_expected", "track_ok")}
    metrics |= {"violations": len(r["violations"]), "fabrications": len(r["fabrications"]), "turns": r["turns"],
                "action_offers": len(r["action_offers"]),
                **{k: r[k] for k in ("latency_p50", "latency_max", "cost_usd", "llm_calls", "cache_hit_rate")},
                **depth_metrics(j), **{k: j.get(k) for k in ("tone", "design_fit")}}
    return {"case": r["id"], "rep": rep, "status": "ok", "metrics": metrics, "detail": r}


def report(doc: dict) -> str:
    from evals.judge import DEPTH_LABELS

    runs = [r for r in doc["runs"] if r["status"] == "ok"]
    tag = lambda r: f"{r['case']}#{r['rep'] + 1}"
    c = doc["config"]
    judge_model = f"`{c.get('judge_model')}`" if c["judge"] else "なし"
    lines = [
        f"# LINE AI アシスタント シミュレーション評価({datetime.now():%Y-%m-%d %H:%M})",
        "",
        (f"- LINE 側モデル: `{c['model']}`、模擬ユーザー: `{c.get('sim_model')}`、ジャッジ: {judge_model}、"
         f"ターン上限: {c.get('max_turns')}"),
        (f"- この実行の費用(エージェント・模擬ユーザー・ジャッジ): ${doc.get('spent_usd', 0):.4f}"
         f"、費用台帳の累計: ${doc.get('ledger_total_usd', 0):.4f}"
         + ("(上限に達したので途中で止めた)" if doc.get("stopped") else "")),
        summary_md(doc),
        "## ルール違反",
        "",
    ]
    lines += [f"- **{tag(r)}** {v}" for r in runs for v in r["detail"]["violations"]] or ["- なし"]
    lines += ["", "## 雇用区分の切り替え(set_search_track)", ""]
    for r in runs:
        for t in r["detail"].get("track_checks") or []:
            lines.append(f"- **{tag(r)}** 期待「{t['track']}」(T{t['due']}まで): {t['status']}"
                         + (f"(T{t['called_turn']} で「{t['called_track']}」)" if t["called_turn"] else ""))
    lines += ["", "## 履歴書・音声の提案(actions)が出たターン", ""]
    lines += [f"- **{tag(r)}** " + ", ".join(f"T{a['turn']}: {'/'.join(a['actions'])}" for a in offers)
              + (f"(アップロード T{r['detail']['upload_turn']})" if r["detail"].get("upload_turn") else "")
              for r in runs if (offers := r["detail"].get("action_offers"))] or ["- なし"]
    lines += ["", "## ジャッジの所見(相談の深さ)", ""]
    for r in runs:
        j = r["detail"].get("judge")
        if not j:
            continue
        lines.append(f"- **{tag(r)}**: {j.get('comment', '')}(tone {j.get('tone')} / design_fit {j.get('design_fit')})")
        for k, label in DEPTH_LABELS.items():
            d = j["depth"][k]
            quoted = "" if d["quoted"] or not d["evidence"] else "(根拠が会話ログに無い)"
            lines.append(f"  - {label}: {d['verdict']} 「{d['evidence']}」{quoted}")
    lines += ["", "## 会話ログ", ""]
    for r in runs:
        d = r["detail"]
        lines.append(f"### {tag(r)}({d['user']})")
        for t in d["transcript"]:
            who = "AI" if t["who"] == "agent" else "ユーザー"
            lines.append(f"- {who}: " + t["text"].replace("\n", " / "))
        lines.append(f"- (dip ツール: {' | '.join(', '.join(x) for x in d['tools'])})")
        lines.append("")
    return "\n".join(lines) + "\n"


def select(scenarios: list[dict], only: str | None) -> list[dict]:
    """--only(カンマ区切りのシナリオID)で絞る。知らない ID は、費用を使う前にエラーにする。"""
    if not only:
        return scenarios
    ids = [x.strip() for x in only.split(",") if x.strip()]
    unknown = [x for x in ids if x not in {sc["id"] for sc in scenarios}]
    if unknown:
        raise SystemExit(f"--only に知らないシナリオ: {unknown}(ある ID: {[sc['id'] for sc in scenarios]})")
    return [sc for sc in scenarios if sc["id"] in ids]


async def run_cases(cases: list[tuple[dict, int]], run_one, ledger: Path, run_name: str, log=print) -> bool:
    """(シナリオ, 回) を順に回す。各シナリオを始める前に費用台帳の累計を見て、上限に達していたら止める(True を返す)。
    run_one(sc, rep) はその1回にかかった費用(エージェント・模擬ユーザー・ジャッジの合計)を返す。
    失敗した回でも、払った分は台帳に載せる。"""
    for sc, rep in cases:
        try:
            total = budget.check(ledger)
        except budget.BudgetExceeded as e:
            log(f"!! {e}")
            return True
        log(f"== {sc['id']} #{rep + 1}(累計 ${total:.4f})")
        cost = await run_one(sc, rep)
        budget.record(ledger, run_name, cost, case=sc["id"], rep=rep)
        log(f"   この回 ${cost:.4f} / {budget.line(ledger)}")
    return False


async def main(only: str | None, use_judge: bool, repeat: int, max_turns: int, ledger: Path = budget.LEDGER) -> int:
    from evals.meter import Meter
    from evals.scenarios import load_scenarios
    from line_runtime.runtime import LineRuntime

    scenarios = select(load_scenarios(), only)
    print(budget.line(ledger), flush=True)
    budget.check(ledger)  # 上限に達していたら、ランタイムを起動する前に止める
    meter = Meter().install()
    client = llm.client()
    runs = []
    out = HERE / "results" / f"sim-{datetime.now():%Y%m%d-%H%M%S}"
    doc = {"harness": "line-sim",
           "config": {"provider": config.PROVIDER, "model": config.MODEL, "sim_model": SIM_MODEL[config.PROVIDER],
                      "judge": use_judge, "judge_model": JUDGE_MODEL[config.PROVIDER] if use_judge else None,
                      "max_turns": max_turns, "repeat": repeat},
           "directions": DIRECTIONS, "runs": runs, "spent_usd": 0.0}
    out.parent.mkdir(exist_ok=True)

    def save() -> None:  # 1回ごとに上書き保存(止めても払った分の結果が残る)
        doc["ledger_total_usd"] = budget.spent(ledger)
        out.with_suffix(".json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))
        out.with_suffix(".md").write_text(report(doc))

    async with LineRuntime(in_memory=True) as rt:
        async def run_one(sc: dict, rep: int) -> float:
            k = meter.mark()
            try:
                r = await run_scenario(rt, client, meter, sc, use_judge, max_turns)
            except Exception as e:  # noqa: BLE001  1件の失敗で評価全体を止めない
                print(f"   FAILED: {type(e).__name__}: {e}", flush=True)
                r = {"id": sc["id"], "error": f"{type(e).__name__}: {e}"}
            else:
                print(json.dumps({x: r[x] for x in ("turns", "branch_turn", "outcomes", "track_ok", "violations",
                                                      "latency_p50", "cost_usd")}, ensure_ascii=False), flush=True)
            cost = meter.summary(k)["cost_usd"]
            doc["spent_usd"] = round(doc["spent_usd"] + cost, 6)
            runs.append(to_run(r, rep))
            return cost

        cases = [(sc, rep) for rep in range(repeat) for sc in scenarios]
        stopped = await run_cases(cases, lambda sc, rep: _saving(run_one(sc, rep), save), ledger, out.name,
                                  log=lambda m: print(m, flush=True))
    doc["stopped"] = stopped
    save()
    print(f"この実行 ${doc['spent_usd']:.4f} / {budget.line(ledger)}")
    print(f"-> {out}.md")
    return 2 if stopped else 0


async def _saving(coro, save) -> float:
    cost = await coro
    save()
    return cost


if __name__ == "__main__":
    from line_runtime.config import load_env

    load_env()
    # dip の状態は評価ごとの一時ファイルに(開発中の data/state.json を汚さない)。MCP 子プロセスにも継承される。
    os.environ["DIP_STATE_PATH"] = str(Path(tempfile.mkdtemp()) / "state.json")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="回すシナリオの ID(カンマ区切り)")
    ap.add_argument("--judge", action="store_true", help="相談の深さの LLM ジャッジも回す(安いモデル)")
    ap.add_argument("--repeat", type=int, default=1, help="各シナリオの実行回数(ばらつきを見るときだけ増やす)")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS, help="1会話のエージェントのターンの上限")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(main(args.only, args.judge, args.repeat, args.max_turns)))
    except budget.BudgetExceeded as e:
        print(f"!! {e}")
        sys.exit(2)
