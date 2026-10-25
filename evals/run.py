"""シナリオ評価: 模擬ユーザー(LLM)と LINE 側ランタイムを会話させ、設計書のルールと KPI で採点する。

各シナリオはそのユーザーの dip の状態を初期に戻してから走る(評価全体で独立した一時ファイル)。
シナリオの一覧は画面と共通(scenarios.json)。どれも AI 側の1通目(挨拶・プッシュ)から始まる(evals/scenarios.py)。
指標の cost_usd・llm_calls はランタイム側の LLM 呼び出しだけ(模擬ユーザーとジャッジは除く)。

各シナリオを --repeat 回(既定1)くり返し、集計と比較は evalkit(リポジトリ直下の evalkit/)に任せる。
結果は1回ごとに書き出す(途中で止めても、そこまでの結果は残る)。実 API を使うので費用がかかる。

    ./.venv/bin/python -m evals.run [--only B-exit,T-side-job] [--judge] [--repeat 2] [--max-turns 12]
    ./.venv/bin/python -m evals.run --only F-follow-new --channel line --features resume_upload,arcade

窓口: 既定は Web のトーク画面(--channel web)。--channel line は LINE のトークとして回す(エージェントの「窓口」が LINE になり、
--features の LIFF 画面で使えるもの(line_channel.liff_features と同じ名前)だけを使ってよいと伝える。既定は 3 つとも)。
返信は render の結果のまま判定する(LINE のメッセージにする to_messages はオフラインのテストが見る)。

費用(PRD aica-v2-fulltime の予算 $5):
- エージェントは本番のモデル(line_runtime.config.MODEL)。模擬ユーザーとジャッジは安いモデル(SIM_MODEL・JUDGE_MODEL)。
- 1会話のエージェントのターンは、シナリオの max_turns(無ければ SCENARIO_TURNS = 8)まで。相談のシナリオは 12。
  --max-turns(既定12)は全体の上限で、シナリオの値より小さければそちら。上限で切れた会話は結果の cut_at_cap に出る。
- 実行ごとに費用台帳(evals/budget.jsonl、エージェント・模擬ユーザー・ジャッジの合計)の累計を出し、
  シナリオを始める前に累計が $4 に達していたら止める(evals/budget.py)。
- PRD とは別の予算のカード(例: 会話の改善 (2/3) の $1)は、--ledger でそのカードの台帳を、--limit-usd で止める額を渡す。

事実性(作り話)と雇用区分の切り替え(set_search_track)は evals/checks.py がコードで判定する。
--judge は相談の深さ(目的・掘り下げ・選択肢・提案理由を根拠の発言つき)とトーンを LLM ジャッジで見る(evals/judge.py)。
ジャッジは1つの会話を --judge-samples 回(既定2)判定してまとめる(点数の揺れを抑える。1回あたり約 $0.0003)。
深さの4項目は、相談・ペルソナのシナリオ(scenarios.json の measure_depth)でだけ数える(ほかは None)。
    python3 evalkit/aggregate.py diff evals/results/sim-A.json evals/results/sim-B.json

ジャッジの指示(evals/judge.py)を直したときは、保存済みの会話をジャッジだけで判定し直せる(費用はジャッジの分だけ):
    ./.venv/bin/python -m evals.run --rejudge evals/baseline/sim-20260929-100418.json
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
from line_runtime import config, line_channel, llm

# 模擬ユーザーとジャッジは安いモデル。OpenAI の料金表(https://developers.openai.com/api/docs/pricing、2026-09-29 確認)
# で gpt-6-luna は入力 $0.10 / 出力 $0.50(1M トークン)で、本番の gpt-6-sol($2.00 / $10.00)の 1/20。
# 構造化出力と Responses API に対応している。Anthropic の場合は Haiku 4.5
SIM_MODEL = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-4-5"}
JUDGE_MODEL = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-4-5"}
MAX_TURNS = 12  # 1会話のエージェントのターンの、評価全体の上限(費用の上限。--max-turns)
# シナリオに max_turns が無いときの上限。相談のシナリオ(相談から提案、その後の反応まで見る)だけ scenarios.json で 12 にする
SCENARIO_TURNS = 8
CHANNELS = ("web", "line")  # line_runtime.agent.CHANNELS
CHANNEL_FEATURES = ("resume_upload", "voice_consult", "arcade")  # --channel line の既定(LIFF と LiveKit の設定がある)

SIM_SYSTEM = """あなたは LINE で仕事探し(アルバイト・パートから正社員まで)のアシスタントと話しているユーザーです。次の設定だけを根拠に、短い話し言葉(1〜2文)で返します。
- 選択肢(クイックリプライ)が合っていれば、その文言をそのまま使う。
- 設定にないことを聞かれたら「特にない」「わからない」など正直に答える。
- 会話を終える台詞を言ったら end を true にする。アシスタントが会話を終えた(もう質問していない)ときも end を true にしてよい。
あなたの設定:
{persona}"""


# 模擬ユーザーとジャッジに見せる操作ボタンの文言。Web はトーク画面の文言(talk-core.js の絵文字を除く)、
# LINE は LINE のボタンの文言(line_channel.LIFF_ACTIONS。挨拶の本文と同じ言い方)
ACTION_LABEL = {"web": {"resume_upload": "ファイルを選ぶ(履歴書)", "voice_consult": "話して伝える(音声・約5分)"},
                "line": {a: label for a, (label, _) in line_channel.LIFF_ACTIONS.items()}}


def _format_agent(r: dict, channel: str = "web") -> str:
    parts = list(r["bubbles"])
    for c in r["cards"]:
        parts.append(f"[求人カード] {c['title']} / {c.get('employment_type', '')} / {c['pay_text']} / {c['shift']} / "
                     f"理由: {'・'.join(c.get('reasons', []))}")
    for b in r["buttons"]:
        parts.append(f"[ボタン] {b['label']}")
    for a in r.get("actions", []):
        parts.append(f"[操作] {ACTION_LABEL[channel].get(a, a)}")
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


def _trace_size() -> int:
    """keywords の検索の記録(DIP_KEYWORDS_TRACE、dip_mcp.logic._trace_keywords)の今の大きさ。"""
    path = os.environ.get("DIP_KEYWORDS_TRACE")
    return Path(path).stat().st_size if path and Path(path).is_file() else 0


def _trace_since(offset: int) -> list[dict]:
    """offset より後に足された keywords の検索の記録(この会話の分。シナリオは順に1本ずつ回す)。"""
    path = os.environ.get("DIP_KEYWORDS_TRACE")
    if not path or not Path(path).is_file():
        return []
    with open(path, encoding="utf-8") as f:
        f.seek(offset)
        return [json.loads(x) for x in f.read().splitlines() if x.strip()]


def _wants_upload(sc: dict, r: dict, n_turns: int) -> bool:
    """履歴書を持つペルソナ(upload_resume)がアップロードするか: AI が履歴書の操作ボタンを出したら、そのとき。
    upload_by_turn のターンまでに出なければ、そこでアップロードする(アップロード後の1問を必ず見るため)。
    1通目(挨拶)の入口の案内は数えない(ペルソナは挨拶の選択肢を選ばずに書き始める。経験を聞く場面の提案で上げる)。"""
    offered = n_turns > 1 and "resume_upload" in (r.get("actions") or [])
    return offered or n_turns >= sc.get("upload_by_turn", 10 ** 6)


async def run_scenario(rt, client, meter, sc: dict, use_judge: bool, max_turns: int = MAX_TURNS,
                       judge_samples: int | None = None, channel: str = "web",
                       features: tuple[str, ...] = ()) -> dict:
    from dip_mcp import logic
    from dip_mcp.data import State
    from evals.checks import conversation_checks, conversation_end
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
    cap = min(sc.get("max_turns", SCENARIO_TURNS), max_turns)
    trace_at = _trace_size()
    transcript, turns, user_msgs, seconds, marks = [], [], [], [], []
    upload = None

    async def agent_turn(event: str, text: str | None, facts: dict | None = None) -> None:
        k = meter.mark()
        t0 = time.perf_counter()
        r = await rt.handle(sc["user"], event, text, facts, channel=channel, features=features)
        seconds.append(time.perf_counter() - t0)
        marks.append((k, meter.mark()))  # このターンのエージェント側の呼び出し(模擬ユーザーはこの後に呼ぶ)
        turns.append(r)
        transcript.append({"who": "agent", "text": _format_agent(r, channel)})

    user_msgs.append(None)  # 1通目は AI 側から(本人の発言なし)
    await agent_turn(sc["event"], None, push_facts)
    script = sc.get("script") or []
    ended_by_user = False  # 模擬ユーザー(か台本)が終えたか、何も返さなかった(False のまま上限に達したら、上限で切れた)
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
            # 台本の最後で会話を終える。script_then_persona のシナリオは、台本の後を模擬ユーザーが続ける
            nxt = {"text": script[step], "end": step == len(script) - 1 and not sc.get("script_then_persona")}
        else:
            nxt = await sim_reply(client, sc["persona"], transcript)
        text = (nxt.get("text") or "").strip()
        if not text:  # 模擬ユーザーが何も返さなかった = 会話を離れた
            ended_by_user = True
            break
        transcript.append({"who": "user", "text": text})
        user_msgs.append(text)
        await agent_turn("message", text)
        if nxt.get("end"):
            ended_by_user = True
            break
    result = {"id": sc["id"], "user": sc["user"], "push_facts": push_facts, "channel": channel, "features": list(features),
              **conversation_checks(sc, turns, user_msgs, push_facts, upload, channel, features),
              **conversation_end(len(turns), cap, ended_by_user),
              "upload_turn": upload["turn"] + 1 if upload else None,
              "latency_p50": round(statistics.median(seconds), 1), "latency_max": round(max(seconds), 1),
              **_usage(meter, marks), "transcript": transcript,
              "tools": [[t["name"] for t in r["tools"]] for r in turns],
              # 評価器を直したあとでオフラインで再判定するための生の材料(ツールの args と result を含む。recheck)
              "turn_log": turns, "user_msgs": user_msgs, "upload": upload,
              # keywords の検索ごとの最高値 - 中央値と、意味の近さを使ったか(SIM_PEAK の余裕を見る。dip が記録する)
              "keyword_searches": _trace_since(trace_at)}
    if use_judge:
        resume = sc.get("upload_resume") or sc.get("seed_resume")
        result["judge"] = await judge(client, JUDGE_MODEL[config.PROVIDER], transcript,
                                      load_resume(resume)["resume"] if resume else None,
                                      **({"samples": judge_samples} if judge_samples else {}))
    return result


# 良い向き(evalkit の diff で改善/悪化を判定するのに使う)
DIRECTIONS = {"branch_within_3": "up", "outcome_recorded": "up", "outcome_as_expected": "up", "violations": "down",
              "search_arg_violations": "down", "call_overlaps": "down", "cut_at_cap": "down", "sim_leaks": "down",
              "greeting_line_messages": "down",
              "fabrications": "down", "track_ok": "up", "cache_hit_rate": "up", "llm_calls": "down",
              "latency_p50": "down", "latency_max": "down", "cost_usd": "down",
              "depth_purpose": "up", "depth_dig": "up", "depth_options": "up", "depth_reasons": "up",
              "tone": "up", "design_fit": "up"}


def depth_of(case: str, judge_result: dict | None) -> dict:
    """深さの4項目の指標。相談・ペルソナのシナリオ(measure_depth)だけで数え、ほかのシナリオは None
    (B-exit・pii などの相談でない会話が「満たさない」で数えられて、全体の率が薄まらないように)。"""
    from evals.judge import DEPTH_ITEMS, depth_metrics
    from evals.scenarios import measures_depth

    return depth_metrics(judge_result) if measures_depth(case) else dict.fromkeys(DEPTH_ITEMS.values())


def to_run(r: dict, rep: int) -> dict:
    """run_scenario の結果を evalkit の1行に。"""
    if "error" in r:
        return {"case": r["id"], "rep": rep, "status": "error", "error": r["error"]}
    j = r.get("judge") or {}
    metrics = {k: r[k] for k in ("branch_within_3", "outcome_recorded", "outcome_as_expected", "track_ok")}
    metrics |= {"violations": len(r["violations"]), "fabrications": len(r["fabrications"]), "turns": r["turns"],
                "search_arg_violations": len(r.get("search_arg_violations", [])),
                "call_overlaps": len(r.get("call_overlaps", [])),
                "action_offers": len(r["action_offers"]),
                "cut_at_cap": r.get("cut_at_cap"), "sim_leaks": len(r.get("sim_leaks", [])),
                # 初回訪問の挨拶が LINE で何通か(LINE の初回訪問だけ。ほかは None)
                "greeting_line_messages": r["line_messages"][0] if r.get("line_messages")
                and (r.get("turn_log") or [{}])[0].get("entry") == "visit" else None,
                **{k: r[k] for k in ("latency_p50", "latency_max", "cost_usd", "llm_calls", "cache_hit_rate")},
                **depth_of(r["id"], j), **{k: j.get(k) for k in ("tone", "design_fit")}}
    return {"case": r["id"], "rep": rep, "status": "ok", "metrics": metrics, "detail": r}


STOPPED_NOTE = {True: "(上限に達したので途中で止めた)", "budget": "(上限に達したので途中で止めた)",
                "interrupted": "(実行を途中で止めた)"}


def report(doc: dict) -> str:
    from evals.judge import DEPTH_LABELS

    runs = [r for r in doc["runs"] if r["status"] == "ok"]
    tag = lambda r: f"{r['case']}#{r['rep'] + 1}"
    c = doc["config"]
    judge_model = (f"`{c.get('judge_model')}`(1会話 {c.get('judge_samples', 1)} 回判定)" if c["judge"] else "なし")
    lines = [
        f"# LINE AI アシスタント シミュレーション評価({datetime.now():%Y-%m-%d %H:%M})",
        "",
        (f"- LINE 側モデル: `{c['model']}`、模擬ユーザー: `{c.get('sim_model')}`、ジャッジ: {judge_model}、"
         f"ターン上限: {c.get('max_turns')}"),
        (f"- この実行の費用({'ジャッジだけ' if doc.get('rejudge') else 'エージェント・模擬ユーザー・ジャッジ'}): "
         f"${doc.get('spent_usd', 0):.4f}"
         f"、費用台帳の累計: ${doc.get('ledger_total_usd', 0):.4f}"
         + STOPPED_NOTE.get(doc.get("stopped"), "")),
        summary_md(doc),
        "## ルール違反",
        "",
    ]
    lines += [f"- **{tag(r)}** {v}" for r in runs for v in r["detail"]["violations"]] or ["- なし"]
    lines += ["", "## 検索の引数(避けたいこと・やりたいこと。違反とは別に数える)", ""]
    lines += [f"- **{tag(r)}** {v}" for r in runs for v in r["detail"].get("search_arg_violations", [])] or ["- なし"]
    lines += ["", "## 呼び出しの重複(save・playbook・検索。違反とは別に数える)", ""]
    lines += [f"- **{tag(r)}** {v}" for r in runs for v in r["detail"].get("call_overlaps", [])] or ["- なし"]
    lines += ["", "## ターンの上限で切れた会話(その後の提案や反応を見ていない)", ""]
    lines += [f"- **{tag(r)}** T{r['detail']['turns']} で上限({r['detail']['turn_cap']})に達して切れた"
              for r in runs if r["detail"].get("cut_at_cap")] or ["- なし"]
    lines += ["", "## 模擬ユーザーが設定の指示文を発言にした(評価の崩れ。エージェントの違反ではない)", ""]
    lines += [f"- **{tag(r)}** {v}" for r in runs for v in r["detail"].get("sim_leaks", [])] or ["- なし"]
    lines += ["", "## keywords の検索(最高値 - 中央値。SIM_PEAK 未満は部分一致だけ)", ""]
    lines += [f"- **{tag(r)}** 「{k['keywords']}」 {k['peak']} → {'意味検索' if k['semantic'] else '部分一致だけ'}"
              f"(候補 {k['candidates']} → {k['kept']} 件)"
              for r in runs for k in r["detail"].get("keyword_searches", [])] or ["- なし"]
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
            if d.get("purpose_evidence") is False and d["verdict"] == "満たした":
                quoted += "(根拠が本人の発言の直後にその話を受けた発言でないので、指標では満たさない)"
            if k in (j.get("split") or []):
                quoted += f"(回ごとの判定が割れた: {' / '.join(s['depth'][k]['verdict'] for s in j['samples'])})"
            lines.append(f"  - {label}: {d['verdict']} 「{d['evidence']}」{quoted}")
    if doc.get("rejudge"):
        lines += ["", f"## 元の判定({doc['rejudge']['from']})との差(相談の深さと design_fit)", "",
                  "会話は元の結果のまま、ジャッジだけを今の evals/judge.py で判定し直した。上の費用はジャッジの分だけ。", ""]
        lines += rejudge_diff(doc)
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


def recheck(detail: dict, sc: dict | None = None) -> dict:
    """保存した結果の detail(turn_log・user_msgs・upload)から、今の evals/checks.py で機械判定をやり直す(実 API を使わない)。
    detail に turn_log が無い結果(2026-09-29 の基準など、保存を始める前のもの)は再判定できない。"""
    from evals.checks import conversation_checks
    from evals.scenarios import load_scenarios

    if "turn_log" not in detail:
        raise ValueError(f"{detail.get('id')}: turn_log が保存されていない結果は再判定できない")
    sc = sc or next(x for x in load_scenarios() if x["id"] == detail["id"])
    return conversation_checks(sc, detail["turn_log"], detail["user_msgs"], detail.get("push_facts"), detail.get("upload"),
                               detail.get("channel", "web"), detail.get("features") or ())


def _resume_of(case: str, scenarios: list[dict]) -> dict | None:
    """ジャッジに渡す本人の履歴書(run_scenario と同じく upload_resume か seed_resume)。"""
    from evals.scenarios import load_resume

    sc = next((x for x in scenarios if x["id"] == case), {})
    name = sc.get("upload_resume") or sc.get("seed_resume")
    return load_resume(name)["resume"] if name else None


def rejudged_run(r: dict, new_judge: dict) -> dict:
    """保存した1回の結果を、新しいジャッジの判定に差し替える(元の判定は detail.judge_before に残す)。"""
    detail = r["detail"] | {"judge": new_judge, "judge_before": r["detail"].get("judge")}
    metrics = r["metrics"] | depth_of(r["case"], new_judge) | {k: new_judge.get(k) for k in ("tone", "design_fit")}
    return r | {"metrics": metrics, "detail": detail}


def rejudge_diff(doc: dict) -> list[str]:
    """判定し直した結果の、元の判定との差(深さの4項目と design_fit)の Markdown。変わった回だけ出す。"""
    from evals.judge import DEPTH_LABELS

    lines = []
    for r in doc["runs"]:
        before, after = (r.get("detail") or {}).get("judge_before"), (r.get("detail") or {}).get("judge")
        if r["status"] != "ok" or not before or not after:
            continue
        changes = [f"{label}: {before['depth'][k]['verdict']} → {after['depth'][k]['verdict']}"
                   for k, label in DEPTH_LABELS.items() if before["depth"][k]["verdict"] != after["depth"][k]["verdict"]]
        if before.get("design_fit") != after.get("design_fit"):
            changes.append(f"design_fit: {before.get('design_fit')} → {after.get('design_fit')}")
        if changes:
            lines.append(f"- **{r['case']}#{r['rep'] + 1}** " + "、".join(changes))
    return lines or ["- なし"]


async def rejudge(src: Path, ledger: Path = budget.LEDGER, judge_samples: int | None = None,
                  limit: float = budget.LIMIT_USD) -> int:
    """保存済みの結果の会話(各 run の transcript)を、今の evals/judge.py のジャッジだけで判定し直す。
    エージェントと模擬ユーザーは呼ばない(実 API はジャッジの安いモデルだけ)。費用は1回ごとに台帳に載せ、
    $4 の停止も同じ。結果は evals/results/<元の名前>-rejudge-<日時>.{json,md} に別ファイルで書く。"""
    from evals.judge import JUDGE_SAMPLES, judge
    from evals.meter import Meter
    from evals.scenarios import load_scenarios

    samples = judge_samples or JUDGE_SAMPLES
    doc = json.loads(Path(src).read_text(encoding="utf-8"))
    print(budget.line(ledger, limit), flush=True)
    budget.check(ledger, limit)
    meter = Meter().install()
    client = llm.client()
    model = JUDGE_MODEL[config.PROVIDER]
    scenarios = load_scenarios()
    out = HERE / "results" / f"{Path(src).stem}-rejudge-{datetime.now():%Y%m%d-%H%M%S}"
    new = doc | {"runs": list(doc["runs"]), "spent_usd": 0.0, "stopped": None,
                 "config": doc["config"] | {"judge": True, "judge_model": model, "judge_samples": samples},
                 "rejudge": {"from": Path(src).name, "judge_model": model}}
    out.parent.mkdir(exist_ok=True)

    def spent() -> float:
        return meter.summary()["cost_usd"]

    def save() -> None:
        new["spent_usd"] = spent()
        new["ledger_total_usd"] = budget.spent(ledger)
        out.with_suffix(".json").write_text(json.dumps(new, ensure_ascii=False, indent=2, default=str))
        out.with_suffix(".md").write_text(report(new))

    async def run_one(sc: dict, rep: int) -> None:
        r = new["runs"][sc["index"]]
        try:
            j = await judge(client, model, r["detail"]["transcript"], _resume_of(r["case"], scenarios), samples)
        except Exception as e:  # noqa: BLE001  1件の失敗で全体を止めない(その回は元の判定のまま)
            print(f"   FAILED: {type(e).__name__}: {e}", flush=True)
            r["detail"]["rejudge_error"] = f"{type(e).__name__}: {e}"
            return
        new["runs"][sc["index"]] = rejudged_run(r, j)

    cases = [({"id": r["case"], "index": i}, r["rep"]) for i, r in enumerate(doc["runs"]) if r["status"] == "ok"]
    try:
        stopped = await run_cases(cases, run_one, spent, ledger, out.name, log=lambda m: print(m, flush=True),
                                  after=save, limit=limit)
    except BaseException:
        new["stopped"] = "interrupted"
        save()
        raise
    new["stopped"] = "budget" if stopped else None
    save()
    print(f"この実行 ${new['spent_usd']:.4f} / {budget.line(ledger, limit)}")
    print(f"-> {out}.md")
    return 2 if stopped else 0


def select(scenarios: list[dict], only: str | None) -> list[dict]:
    """--only(カンマ区切りのシナリオID)で絞る。知らない ID は、費用を使う前にエラーにする。"""
    if not only:
        return scenarios
    ids = [x.strip() for x in only.split(",") if x.strip()]
    unknown = [x for x in ids if x not in {sc["id"] for sc in scenarios}]
    if unknown:
        raise SystemExit(f"--only に知らないシナリオ: {unknown}(ある ID: {[sc['id'] for sc in scenarios]})")
    return [sc for sc in scenarios if sc["id"] in ids]


async def run_cases(cases: list[tuple[dict, int]], run_one, spent, ledger: Path, run_name: str, log=print,
                    after=None, limit: float = budget.LIMIT_USD) -> bool:
    """(シナリオ, 回) を順に回す。各シナリオを始める前に費用台帳の累計を見て、上限に達していたら止める(True を返す)。
    spent() はこのプロセスがここまでに払った額(エージェント・模擬ユーザー・ジャッジの合計)で、その回の前後の差を台帳に載せる。
    run_one が失敗しても、途中で止められても(Ctrl-C の KeyboardInterrupt・CancelledError)、払った分は finally で載せてから
    例外をそのまま上げる。after() は台帳に載せたあとに毎回呼ぶ(結果の保存)。"""
    for sc, rep in cases:
        try:
            total = budget.check(ledger, limit)
        except budget.BudgetExceeded as e:
            log(f"!! {e}")
            return True
        log(f"== {sc['id']} #{rep + 1}(累計 ${total:.4f})")
        before, done = spent(), False
        try:
            await run_one(sc, rep)
            done = True
        finally:
            cost = spent() - before
            budget.record(ledger, run_name, cost, case=sc["id"], rep=rep, **({} if done else {"interrupted": True}))
            log(f"   この回 ${cost:.4f}{'' if done else '(途中で止めた)'} / {budget.line(ledger, limit)}")
        if after:
            after()
    return False


async def main(only: str | None, use_judge: bool, repeat: int, max_turns: int, ledger: Path = budget.LEDGER,
               judge_samples: int | None = None, channel: str = "web", features: tuple[str, ...] = CHANNEL_FEATURES,
               limit: float = budget.LIMIT_USD) -> int:
    from evals.judge import JUDGE_SAMPLES
    from evals.meter import Meter
    from evals.scenarios import load_scenarios
    from line_runtime.runtime import LineRuntime

    scenarios = select(load_scenarios(), only)
    features = tuple(features) if channel == "line" else ()
    print(budget.line(ledger, limit), flush=True)
    budget.check(ledger, limit)  # 上限に達していたら、ランタイムを起動する前に止める
    meter = Meter().install()
    client = llm.client()
    runs = []
    out = HERE / "results" / f"sim-{datetime.now():%Y%m%d-%H%M%S}"
    doc = {"harness": "line-sim",
           "config": {"provider": config.PROVIDER, "model": config.MODEL, "sim_model": SIM_MODEL[config.PROVIDER],
                      "judge": use_judge, "judge_model": JUDGE_MODEL[config.PROVIDER] if use_judge else None,
                      "judge_samples": (judge_samples or JUDGE_SAMPLES) if use_judge else None,
                      "max_turns": max_turns, "repeat": repeat, "channel": channel, "features": list(features)},
           "directions": DIRECTIONS, "runs": runs, "spent_usd": 0.0, "stopped": None}
    out.parent.mkdir(exist_ok=True)

    def spent() -> float:  # このプロセスの全呼び出し(エージェント・模擬ユーザー・ジャッジ)の合計
        return meter.summary()["cost_usd"]

    def save() -> None:  # 1回ごとに上書き保存(止めても払った分の結果が残る)
        doc["spent_usd"] = spent()
        doc["ledger_total_usd"] = budget.spent(ledger)
        out.with_suffix(".json").write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
        out.with_suffix(".md").write_text(report(doc))

    async with LineRuntime(in_memory=True) as rt:
        async def run_one(sc: dict, rep: int) -> None:
            try:
                r = await run_scenario(rt, client, meter, sc, use_judge, max_turns, judge_samples, channel, features)
            except Exception as e:  # noqa: BLE001  1件の失敗で評価全体を止めない
                print(f"   FAILED: {type(e).__name__}: {e}", flush=True)
                r = {"id": sc["id"], "error": f"{type(e).__name__}: {e}"}
            else:
                print(json.dumps({x: r[x] for x in ("turns", "ended_by", "branch_turn", "outcomes", "track_ok",
                                                      "violations", "sim_leaks", "latency_p50", "cost_usd")},
                                 ensure_ascii=False), flush=True)
            runs.append(to_run(r, rep))

        cases = [(sc, rep) for rep in range(repeat) for sc in scenarios]
        try:
            stopped = await run_cases(cases, run_one, spent, ledger, out.name, log=lambda m: print(m, flush=True),
                                      after=save, limit=limit)
        except BaseException:  # Ctrl-C など。台帳には run_cases が載せたので、結果にも止めたことを書いて上げ直す
            doc["stopped"] = "interrupted"
            save()
            raise
    doc["stopped"] = "budget" if stopped else None
    save()
    print(f"この実行 ${doc['spent_usd']:.4f} / {budget.line(ledger, limit)}")
    print(f"-> {out}.md")
    return 2 if stopped else 0


if __name__ == "__main__":
    from line_runtime.config import load_env

    load_env()
    # dip の状態は評価ごとの一時ファイルに(開発中の data/state.json を汚さない)。MCP 子プロセスにも継承される。
    tmp = Path(tempfile.mkdtemp())
    os.environ["DIP_STATE_PATH"] = str(tmp / "state.json")
    os.environ["DIP_KEYWORDS_TRACE"] = str(tmp / "keywords.jsonl")
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="回すシナリオの ID(カンマ区切り)")
    ap.add_argument("--judge", action="store_true", help="相談の深さの LLM ジャッジも回す(安いモデル)")
    ap.add_argument("--repeat", type=int, default=1, help="各シナリオの実行回数(ばらつきを見るときだけ増やす)")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS,
                    help="1会話のエージェントのターンの、全体の上限(シナリオの max_turns、無ければ 8 より小さければこちら)")
    ap.add_argument("--judge-samples", type=int, default=None,
                    help="ジャッジが1つの会話を判定する回数(既定 evals.judge.JUDGE_SAMPLES = 2。まとめ方は evals/judge.py)")
    ap.add_argument("--channel", choices=CHANNELS, default="web", help="会話の窓口(web: Web のトーク画面 / line: LINE のトーク)")
    ap.add_argument("--features", default=",".join(CHANNEL_FEATURES),
                    help="--channel line で LIFF 画面で使えるもの(カンマ区切り。resume_upload / voice_consult / arcade。空なら LIFF 無し)")
    ap.add_argument("--ledger", type=Path, default=budget.LEDGER, help="費用台帳(既定は PRD aica-v2-fulltime の evals/budget.jsonl)")
    ap.add_argument("--limit-usd", type=float, default=None,
                    help=f"台帳の累計がこの額に達したら止める(既定 ${budget.LIMIT_USD:.0f}。--ledger を変えたら必ず渡す)")
    ap.add_argument("--rejudge", type=Path, metavar="RESULT_JSON",
                    help="保存済みの結果の会話を、ジャッジだけで判定し直す(エージェントと模擬ユーザーは呼ばない)")
    args = ap.parse_args()
    if args.ledger.resolve() != budget.LEDGER.resolve() and args.limit_usd is None:  # 別の予算のカードが PRD の止める額($4)まで使わないように
        ap.error("--ledger を変えたら、そのカードの止める額を --limit-usd で渡してください")
    limit = budget.LIMIT_USD if args.limit_usd is None else args.limit_usd
    try:
        if args.rejudge:
            sys.exit(asyncio.run(rejudge(args.rejudge, args.ledger, args.judge_samples, limit)))
        features = tuple(f.strip() for f in args.features.split(",") if f.strip())
        if unknown := [f for f in features if f not in CHANNEL_FEATURES]:
            raise SystemExit(f"--features に知らないもの: {unknown}(ある名前: {list(CHANNEL_FEATURES)})")
        sys.exit(asyncio.run(main(args.only, args.judge, args.repeat, args.max_turns, args.ledger, args.judge_samples,
                                  args.channel, features, limit)))
    except budget.BudgetExceeded as e:
        print(f"!! {e}")
        sys.exit(2)
