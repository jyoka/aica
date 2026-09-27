"""シナリオ評価: 模擬ユーザー(LLM)と LINE 側ランタイムを会話させ、設計書のルールと KPI で採点する。

各シナリオはそのユーザーの dip の状態を初期に戻してから走る(評価全体で独立した一時ファイル)。
シナリオの一覧は画面と共通(scenarios.json)。どれも AI 側の1通目(挨拶・プッシュ)から始まる(evals/scenarios.py)。
計測するのはランタイム側の LLM 呼び出しだけ(模擬ユーザーとジャッジは除外)。

各シナリオを --repeat 回(既定1)くり返し、集計と比較は evalkit(リポジトリ直下の evalkit/)に任せる。
結果は1回ごとに書き出す(途中で止めても、そこまでの結果は残る)。実 API を使うので費用がかかる。

    ./.venv/bin/python -m evals.run [--only B-exit] [--judge] [--repeat 3]

事実性(作り話)は evals/checks.py がコードで判定する。LLM ジャッジは既定でオフで、トーンや設計の
主観的な部分を見たいときだけ --judge で回す(gpt-6-sol / Opus 5 を呼ぶので費用がかかる)。
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

from line_runtime import config, llm

SIM_MODEL = {"openai": "gpt-6-sol", "anthropic": "claude-sonnet-5"}
JUDGE_MODEL = {"openai": "gpt-6-sol", "anthropic": "claude-opus-5"}
MAX_TURNS = 6

SIM_SYSTEM = """あなたは LINE でアルバイト探しのアシスタントと話しているユーザーです。次の設定だけを根拠に、短い話し言葉(1文)で返します。
- 選択肢(クイックリプライ)が合っていれば、その文言をそのまま使う。
- 設定にないことを聞かれたら「特にない」「わからない」など正直に答える。
- 会話を終える台詞を言ったら end を true にする。アシスタントが会話を終えた(もう質問していない)ときも end を true にしてよい。
あなたの設定:
{persona}"""

JUDGE_SYSTEM = """あなたは LINE の AI アシスタントの会話品質を審査します。会話ログと、アシスタントが使った事業者ツールの結果を見て採点します。
各項目 1〜5 の整数。JSON のみを出力(前置き禁止)。
{
  "factuality": <ツール結果にない行動履歴・求人情報・事実を語っていないか。1件でも作り話があれば3以下>,
  "fabrications": ["<作り話の具体例。無ければ空>"],
  "tone": <敬体・短文・絵文字なし・押しつけがましくないか>,
  "design_fit": <先に理解し1問だけ聞き、3ターン以内に 提案/相談/支援/待つ のどれかに進み、拒否を即尊重する、という設計の狙いどおりか>,
  "comment": "<1〜2文>"
}"""


def _format_agent(r: dict) -> str:
    parts = list(r["bubbles"])
    for c in r["cards"]:
        parts.append(f"[求人カード] {c['title']} / 時給{c['hourly_wage']}円 / {c['shift']} / 理由: {'・'.join(c.get('reasons', []))}")
    for b in r["buttons"]:
        parts.append(f"[ボタン] {b['label']}")
    if r["quick_replies"]:
        parts.append("[選択肢] " + " / ".join(r["quick_replies"]))
    return "\n".join(parts)


class SimTurn(BaseModel):
    text: str = Field(description="ユーザーとして送る返信(1文)")
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


async def judge(client, transcript: list[dict], turns: list[dict], facts: dict | None = None) -> dict:
    log = "\n".join(f"{'アシスタント' if t['who'] == 'agent' else 'ユーザー'}: {t['text']}" for t in transcript)
    tools = [{"name": t["name"], "result": t["result"]} for r in turns for t in r["tools"]]
    user = f"## 会話ログ\n{log}\n\n## 事業者ツールの結果\n{json.dumps(tools, ensure_ascii=False)[:12000]}"
    if facts:  # プッシュの根拠の事実も事業者が渡した情報(これが無いと、本物の事実を作り話と判定してしまう)
        user += f"\n\n## 事業者がプッシュで渡した情報\n{json.dumps(facts, ensure_ascii=False)}"
    text = await llm.text(client, model=JUDGE_MODEL[config.PROVIDER], max_tokens=3000, effort="low",
                          system=JUDGE_SYSTEM, messages=[{"role": "user", "content": user}])
    return json.loads(text[text.index("{"):text.rindex("}") + 1])


def _usage(meter, marks: list[tuple[int, int]]) -> dict:
    """1会話のエージェント側の呼び出しの集計(コスト・呼び出し回数・キャッシュのヒット率)。"""
    from evals.meter import Meter

    sub = Meter()
    sub.calls = [c for a, b in marks for c in meter.calls[a:b]]
    u = sub.summary()
    return {"cost_usd": u["cost_usd"], "llm_calls": u["llm_calls"], "cache_hit_rate": u["cache_hit_rate"]}


async def run_scenario(rt, client, meter, sc: dict, use_judge: bool) -> dict:
    from dip_mcp import logic
    from dip_mcp.data import State
    from evals.checks import conversation_checks
    from evals.scenarios import prepare
    from line_runtime.runtime import EVENTS

    prepare(State(), sc)  # 新しい会話は最初のイベント(visit・revisit・プッシュ)が始める
    facts = push_facts = None
    if EVENTS[sc["event"]][1] == "push":  # 本番と同じく、プッシュしてよいかは dip が決める
        gate = logic.push_trigger(State(), sc["user"], sc["event"])
        if not gate["allowed"]:
            raise RuntimeError(f"dip がプッシュを許可しなかった: {gate['reason']}")
        facts = push_facts = gate["facts"]
    transcript, turns, user_msgs, seconds, marks = [], [], [], [], []
    given = push_facts  # dip がこの会話の前に渡した情報(事実性の照合の根拠)
    event, text = sc["event"], None
    for _ in range(MAX_TURNS):
        if text is not None:
            transcript.append({"who": "user", "text": text})
        user_msgs.append(text)
        k = meter.mark()
        t0 = time.perf_counter()
        r = await rt.handle(sc["user"], event, text, facts)
        facts = None
        seconds.append(time.perf_counter() - t0)
        marks.append((k, meter.mark()))  # このターンのエージェント側の呼び出し(模擬ユーザーはこの後に呼ぶ)
        turns.append(r)
        transcript.append({"who": "agent", "text": _format_agent(r)})
        script = sc.get("script") or []
        step = len(turns) - 1
        if step < len(script):  # 台本があるシナリオは、その返信を使う(確認したい操作を確実に起こすため)
            nxt = {"text": script[step], "end": step == len(script) - 1}
        else:
            nxt = await sim_reply(client, sc["persona"], transcript)
        event, text = "message", (nxt.get("text") or "").strip()
        if not text:  # 模擬ユーザーが何も返さなかった = 会話を離れた
            break
        if nxt.get("end"):
            transcript.append({"who": "user", "text": text})
            user_msgs.append(text)
            k = meter.mark()
            t0 = time.perf_counter()
            r = await rt.handle(sc["user"], event, text)
            seconds.append(time.perf_counter() - t0)
            marks.append((k, meter.mark()))  # このターンのエージェント側の呼び出し(模擬ユーザーはこの後に呼ぶ)
            turns.append(r)
            transcript.append({"who": "agent", "text": _format_agent(r)})
            break
    result = {"id": sc["id"], "user": sc["user"], "push_facts": push_facts,
              **conversation_checks(sc, turns, user_msgs, given),
              "latency_p50": round(statistics.median(seconds), 1), "latency_max": round(max(seconds), 1),
              **_usage(meter, marks), "transcript": transcript,
              "tools": [[t["name"] for t in r["tools"]] for r in turns]}
    if use_judge:
        result["judge"] = await judge(client, transcript, turns, given)
    return result


# 良い向き(evalkit の diff で改善/悪化を判定するのに使う)
DIRECTIONS = {"branch_within_3": "up", "outcome_recorded": "up", "outcome_as_expected": "up", "violations": "down",
              "fabrications": "down", "cache_hit_rate": "up", "llm_calls": "down",
              "latency_p50": "down", "latency_max": "down", "cost_usd": "down",
              "factuality": "up", "tone": "up", "design_fit": "up"}


def to_run(r: dict, rep: int) -> dict:
    """run_scenario の結果を evalkit の1行に。"""
    if "error" in r:
        return {"case": r["id"], "rep": rep, "status": "error", "error": r["error"]}
    j = r.get("judge") or {}
    metrics = {k: r[k] for k in ("branch_within_3", "outcome_recorded", "outcome_as_expected")}
    metrics |= {"violations": len(r["violations"]), "fabrications": len(r["fabrications"]), "turns": r["turns"],
                **{k: r[k] for k in ("latency_p50", "latency_max", "cost_usd", "llm_calls", "cache_hit_rate")},
                **{k: j.get(k) for k in ("factuality", "tone", "design_fit")}}
    return {"case": r["id"], "rep": rep, "status": "ok", "metrics": metrics, "detail": r}


def report(doc: dict) -> str:
    runs = [r for r in doc["runs"] if r["status"] == "ok"]
    tag = lambda r: f"{r['case']}#{r['rep'] + 1}"
    lines = [
        f"# LINE AI アシスタント シミュレーション評価({datetime.now():%Y-%m-%d %H:%M})",
        "",
        f"- LINE 側モデル: `{doc['config']['model']}`、ジャッジ: {'あり' if doc['config']['judge'] else 'なし'}",
        summary_md(doc),
        "## ルール違反",
        "",
    ]
    lines += [f"- **{tag(r)}** {v}" for r in runs for v in r["detail"]["violations"]] or ["- なし"]
    lines += ["", "## ジャッジの所見", ""]
    for r in runs:
        j = r["detail"].get("judge")
        if j:
            fab = f" 作り話: {' / '.join(j['fabrications'])}" if j.get("fabrications") else ""
            lines.append(f"- **{tag(r)}**: {j.get('comment', '')}{fab}")
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


async def main(only: str | None, use_judge: bool, repeat: int) -> None:
    from evals.meter import Meter
    from evals.scenarios import load_scenarios
    from line_runtime.runtime import LineRuntime

    meter = Meter().install()
    client = llm.client()
    scenarios = load_scenarios()
    runs = []
    doc = {"harness": "line-sim", "config": {"provider": config.PROVIDER, "model": config.MODEL, "judge": use_judge},
           "directions": DIRECTIONS, "runs": runs}
    out = HERE / "results" / f"sim-{datetime.now():%Y%m%d-%H%M%S}"
    out.parent.mkdir(exist_ok=True)

    def save() -> None:  # 1回ごとに上書き保存(止めても払った分の結果が残る)
        out.with_suffix(".json").write_text(json.dumps(doc, ensure_ascii=False, indent=2))
        out.with_suffix(".md").write_text(report(doc))

    async with LineRuntime(in_memory=True) as rt:
        for rep in range(repeat):
            for sc in scenarios:
                if only and sc["id"] != only:
                    continue
                print(f"== {sc['id']} #{rep + 1}/{repeat}", flush=True)
                try:
                    r = await run_scenario(rt, client, meter, sc, use_judge)
                except Exception as e:  # noqa: BLE001  1件の失敗で評価全体を止めない
                    print(f"   FAILED: {type(e).__name__}: {e}", flush=True)
                    r = {"id": sc["id"], "error": f"{type(e).__name__}: {e}"}
                else:
                    print(json.dumps({x: r[x] for x in ("turns", "branch_turn", "outcomes", "outcome_as_expected",
                                                          "violations", "latency_p50", "cost_usd")},
                                     ensure_ascii=False), flush=True)
                runs.append(to_run(r, rep))
                save()
    print(f"-> {out}.md")


if __name__ == "__main__":
    from line_runtime.config import load_env

    load_env()
    # dip の状態は評価ごとの一時ファイルに(開発中の data/state.json を汚さない)。MCP 子プロセスにも継承される。
    os.environ["DIP_STATE_PATH"] = str(Path(tempfile.mkdtemp()) / "state.json")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--judge", action="store_true", help="LLM ジャッジも回す(gpt-6-sol / Opus 5、費用がかかる)")
    ap.add_argument("--repeat", type=int, default=1, help="各シナリオの実行回数(ばらつきを見るときだけ増やす)")
    args = ap.parse_args()
    asyncio.run(main(args.only, args.judge, args.repeat))
