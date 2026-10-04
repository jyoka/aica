"""後続の確かめ(task 109): 実 LLM で「電話はしたくない」の扱いと、音声で電話が中心の求人を外す流れを見る小さな計測。

シナリオ評価(evals/run.py)の15本には電話を避けたい人がいないので、task 126・127 の PR レビューで頼まれたことを
ここで見る。チャットは evals.run.run_scenario をそのまま使い(LINE 側エージェントは本番のモデル)、
音声は LiveKit の AgentSession(文字のターン。STT・TTS なし)に本番と同じ advisor と dip の MCP をつなぐ。
本人の発言は模擬ユーザー(evals.run.SIM_MODEL の安いモデル)がペルソナから作る。
職務経歴書の下書きとトークへの送信は差し替える(ここで見ることと関係がなく、費用を使わない)。

費用は evals/run.py と同じ台帳(evals/budget.jsonl)に1本ごとに載せ、始める前に累計が $4 に達していたら止める。

    ./.venv/bin/python -m evals.probe [--only P-chat-phone,V-phone-night]

見ること(チャット):
- 「電話はしたくない」の発言に答えたターンで max_talk_level を付けずに、接客まで避けたいかを1問で確かめたか(問いの言い方も)
- 答えに応じて、最初の recommend_jobs が max_talk_level=2 か付けないかに分かれたか。カードにコールセンターが出たか
- 「電話も接客もしたくない」では聞き返さずに max_talk_level=2 を付けたか
見ること(音声):
- 上の2つに加え、propose_jobs の1回目の結果にコールセンターがあったとき、読み上げずに exclude_ids で呼び直したか
- 発話に求人ID(b0903 など)が出たか。exclude_ids を LLM が付け忘れた回数(advisor が補った回数)
- 外した後に0件で、前の提案(読み上げていない求人を含むか)がトークに届いた回数と、そのときの伝え方
- 本人が前の求人に戻りたがったときの答え方(「さっきの求人も送ります」のような事実と違う発言)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from evals import budget

HERE = Path(__file__).resolve().parent
CALL_CENTER = "コールセンター"
BAITO = "アルバイト・パート"

# 理由や細かい条件を聞かれ続けて提案まで届かない(2026-10-04 の1回目は音声で7ターン聞かれ続けた)ので、早めに探してもらう
_FAST = "理由や、職種・日数・時給などの細かい条件を聞かれたら「特にないので、この条件で求人を探してください」と答える。"
_END = _FAST + "求人を見せられたら「ありがとう、考えます」と言って終える。"
CHAT_PROBES = [
    {"id": "P-chat-phone", "title": "電話だけを避けたい(レジや接客は大丈夫)・深夜",
     "persona": "今日初めてこのトークを開いた。選択肢は選ばず、最初に「深夜のアルバイトを探しています。電話はしたくないです」"
                "と書く(この言い方のまま)。お客様と話すレジや接客は大丈夫。エリアはどこでもいい。" + _END,
     "expect_talk_level": None},
    {"id": "P-chat-phone-nigate", "title": "言い換え「電話が苦手」・接客も苦手",
     "persona": "今日初めてこのトークを開いた。選択肢は選ばず、最初に「新宿でアルバイトを探しています。電話が苦手です」"
                "と書く(この言い方のまま)。接客も苦手で、お客様と話す仕事は避けたい。時間帯はいつでもいい。" + _END,
     "expect_talk_level": 2},
    {"id": "P-chat-phone-iya", "title": "言い換え「電話対応は嫌」・レジは大丈夫",
     "persona": "今日初めてこのトークを開いた。選択肢は選ばず、最初に「夜にアルバイトを探しています。電話対応は嫌です」"
                "と書く(この言い方のまま)。レジや接客は大丈夫。エリアはどこでもいい。" + _END,
     "expect_talk_level": None},
    {"id": "P-chat-phone-and-service", "title": "一度に「電話も接客もしたくない」",
     "persona": "今日初めてこのトークを開いた。選択肢は選ばず、最初に「新宿でアルバイトを探しています。電話も接客もしたくないです」"
                "と書く(この言い方のまま)。時間帯はいつでもいい。" + _END,
     "expect_talk_level": 2, "no_extra_question": True},
    {"id": "P-chat-phone-fulltime", "title": "正社員の区分で「電話はしたくない」(確認の問いの言い方)",
     "persona": "今日初めてこのトークを開いた。選択肢は選ばず、最初に「正社員の仕事を探しています。電話はしたくないです」と書く"
                "(この言い方のまま)。経歴は不動産会社の営業事務を5年。転職の理由は、今の会社は残業が多いから。"
                "お客様や取引先と会って話すのは大丈夫。勤務地は東京。年収は400万以上。" + _END,
     "expect_talk_level": None},
]
VOICE_PROBES = [
    {"id": "V-phone-night", "title": "音声: 電話だけを避けたい・深夜(コールセンターが上位3件に入る)・ほかには・前の求人に戻りたい",
     "persona": "音声で仕事探しの相談をしている。最初に「深夜で探したいです。電話はしたくないです」と言う(この言い方のまま)。"
                "お客様と話すレジや接客は大丈夫。エリアはどこでもいい。" + _FAST + "求人を聞いたら、一度だけ「ほかにはありますか?」と聞く。"
                "その次に「やっぱり、さっきの最初の求人に戻りたいです」と言う。その後は「以上です」と言って終える。"},
    {"id": "V-phone-sit", "title": "音声: 電話だけを避けたい・深夜 → 座ってできる仕事(当たるのはコールセンターだけ)",
     "persona": "音声で仕事探しの相談をしている。最初に「深夜で探したいです。電話はしたくないです」と言う(この言い方のまま)。"
                "お客様と話すレジや接客は大丈夫。エリアはどこでもいい。" + _FAST + "求人を聞いたら「座ってできる仕事がいいです」と言って"
                "条件を変える。その後は「以上です」と言って終える。"},
    {"id": "V-phone-and-service", "title": "音声: 一度に「電話も接客もしたくない」",
     "persona": "音声で仕事探しの相談をしている。最初に「深夜で探したいです。電話も接客もしたくないです」と言う(この言い方のまま)。"
                "エリアはどこでもいい。" + _FAST + "求人を聞いたら「以上です」と言って終える。",
     "expect_talk_level": 2, "no_extra_question": True},
]
CHAT_MAX_TURNS = 5
VOICE_MAX_TURNS = 10
PHONE = re.compile(r"電話")
SERVICE_Q = re.compile(r"接客|レジ|お客様|お客さん|取引先|人と話す")
JOB_ID = re.compile(r"\b[abf]\d{3,4}\b|求人ID")
BACK_PROMISE = re.compile(r"(さっき|先ほど|前|最初)の[^。]{0,12}(も|を)[^。]{0,8}(送|お送り|届|出し|お出し|ご案内し)ます")
# 本人が前に出た求人に戻りたがった発話(外した求人はこの面談では出せない。task 127 の再レビュー)
BACK_REQUEST = re.compile(r"(さっき|先ほど|前|最初)の[^。]{0,8}(求人|お仕事|仕事)?[^。]{0,6}(戻|がいい|が気にな|にしたい)")


def _questions(texts: list[str]) -> list[str]:
    return [s for t in texts for s in re.split(r"(?<=[?？])", t) if s.strip().endswith(("?", "？"))]


# ----------------------------- チャット -----------------------------


def chat_observation(sc: dict, r: dict) -> dict:
    """チャットの1本から、電話の扱いを読み出す(turn_log のツールの args と返信)。"""
    turns, said = r["turn_log"], r["user_msgs"]
    k = next((i for i, m in enumerate(said) if m and PHONE.search(m)), None)
    searches = [(i, t["args"]) for i, t0 in enumerate(turns) for t in t0["tools"] if t["name"] == "recommend_jobs"]
    first = next(((i, a) for i, a in searches if k is not None and i >= k), None)
    # 確かめの問いは、電話の発言に答えたターンから最初の検索のターンまでのどこか(先に別のことを聞いてもよい)
    upto = first[0] if first else len(turns) - 1
    asked = [(i, q) for i in range(k, upto + 1) for q in _questions(turns[i]["bubbles"])
             if SERVICE_Q.search(q)] if k is not None else []
    cards = [c for t in turns for c in t["cards"]]
    return {
        "phone_turn": k + 1 if k is not None else None,
        "searched_on_phone_turn": bool(first and first[0] == k),
        "confirm_question": asked[0][1] if asked else None,
        "confirm_turn": asked[0][0] + 1 if asked else None,
        "first_search_turn": first[0] + 1 if first else None,
        "first_search_args": first[1] if first else None,
        "talk_level": (first[1].get("max_talk_level") if first else None),
        "talk_level_as_expected": (first[1].get("max_talk_level") == sc.get("expect_talk_level")) if first else None,
        "call_center_cards": [c["title"] for c in cards if c.get("job_type") == CALL_CENTER],
        "search_arg_violations": r.get("search_arg_violations", []),
    }


async def run_chat(probes: list[dict], ledger: Path, results: list[dict], save) -> bool:
    from evals.meter import Meter
    from evals.run import run_cases, run_scenario
    from line_runtime import llm
    from line_runtime.runtime import LineRuntime

    meter = Meter().install()
    client = llm.client()
    async with LineRuntime(in_memory=True) as rt:
        async def run_one(p: dict, _rep: int) -> None:
            sc = {"user": "sim-new", "event": "visit", "expect_outcome": ["propose", "consult"],
                  "max_turns": CHAT_MAX_TURNS} | p
            try:
                r = await run_scenario(rt, client, meter, sc, use_judge=False, max_turns=CHAT_MAX_TURNS)
            except Exception as e:  # noqa: BLE001  1本の失敗で全体を止めない
                results.append({"id": p["id"], "channel": "chat", "error": f"{type(e).__name__}: {e}"})
                return
            results.append({"id": p["id"], "channel": "chat", "title": p["title"],
                            "observation": chat_observation(sc, r), "cost_usd": r["cost_usd"],
                            "violations": r["violations"], "transcript": r["transcript"], "turn_log": r["turn_log"]})

        return await run_cases([(p, 0) for p in probes], run_one, lambda: meter.summary()["cost_usd"], ledger,
                               f"probe-{datetime.now():%Y%m%d-%H%M%S}-chat", log=lambda m: print(m, flush=True),
                               after=save)


# ----------------------------- 音声 -----------------------------


class _NoPoster:
    """トークへの送信の代わりに、送る中身を覚える(届いた提案を数えるため)。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, url: str, payload: dict) -> None:
        self.calls.append(payload)


class _NoDraft:
    """職務経歴書の下書き(responses.parse)を呼ばない。"""

    def __init__(self):
        from resume_intake.schema import ResumeDraft

        async def parse(**_kw):
            return SimpleNamespace(output_parsed=ResumeDraft(job_summary="(計測のため下書きは作らない)"))
        self.responses = SimpleNamespace(parse=parse)


def _items(history) -> list[dict]:
    """AgentSession の履歴を、発話・ツールの呼び出し・結果の並びに(読み上げとツールの前後を見るため)。"""
    out = []
    for it in history.items:
        if it.type == "message":
            out.append({"kind": "say" if it.role == "assistant" else "user", "text": it.text_content or ""})
        elif it.type == "function_call":
            out.append({"kind": "call", "name": it.name, "args": json.loads(it.arguments or "{}")})
        elif it.type == "function_call_output":
            out.append({"kind": "output", "name": it.name, "text": it.output})
    return out


def voice_observation(sc: dict, items: list[dict], attempts: list[dict], delivered: list[dict]) -> dict:
    """音声の1本から、電話の扱いとコールセンターを外す流れを読み出す。"""
    from dip_mcp.data import load_jobs

    jobs = load_jobs()
    cc = {jid for jid, j in jobs.items() if j.job_type == CALL_CENTER}
    k = next((i for i, x in enumerate(items) if x["kind"] == "user" and PHONE.search(x["text"])), None)
    calls = [(i, x) for i, x in enumerate(items) if x["kind"] == "call" and x["name"] == "propose_jobs"]
    first = next(((i, x) for i, x in calls if k is not None and i > k), None)
    # 電話の発話から最初の propose_jobs までのアシスタントの問い(先に別のことを聞いてもよい)。
    # 音声は「〜ですか。」と句点で聞くので、疑問符が無くても「か。」で終わる文を問いに数える
    upto = first[0] if first else len(items)
    asked = [q for x in items[(k or 0) + 1:upto] if x["kind"] == "say"
             for q in re.split(r"(?<=[?？。])", x["text"]) if SERVICE_Q.search(q) and re.search(r"か[?？。]?$", q.strip())
             ] if k is not None else []
    # 結果にコールセンターがあった propose_jobs の後、次の propose_jobs の呼び出しまでに読み上げたか
    read_before_recall, recalled = 0, 0
    for n, (i, _x) in enumerate(calls):
        out = next((y for y in items[i + 1:] if y["kind"] == "output" and y["name"] == "propose_jobs"), None)
        if not out or not re.search(rf"{CALL_CENTER}のお仕事", out["text"]):
            continue
        end = calls[n + 1][0] if n + 1 < len(calls) else len(items)
        said = " ".join(y["text"] for y in items[i + 1:end] if y["kind"] == "say")
        recalled += n + 1 < len(calls) and "exclude_ids" in calls[n + 1][1]["args"]
        read_before_recall += bool(re.search(rf"{CALL_CENTER}|オペレーター|駆けつけ", said))
    ever_excluded = False
    forgot = 0
    for _i, x in calls:
        if ever_excluded and not x["args"].get("exclude_ids"):
            forgot += 1
        ever_excluded = ever_excluded or bool(x["args"].get("exclude_ids"))
    zero_after_exclude = [a for a in attempts if a["found"] == 0 and a.get("exclude_ids")]
    sent = [j["job_id"] for p in delivered for j in p.get("jobs", [])]
    return {
        "phone_turn_item": k,
        "confirm_question": asked[0] if asked else None,
        "first_propose_args": first[1]["args"] if first else None,
        "talk_level": first[1]["args"].get("max_talk_level") if first else None,
        "propose_calls": [x["args"] for _i, x in calls],
        "call_center_in_results_then_recalled": recalled,
        "call_center_read_before_recall": read_before_recall,
        "job_id_spoken": [x["text"] for x in items if x["kind"] == "say" and JOB_ID.search(x["text"])],
        "exclude_ids_forgotten_by_llm": forgot,
        "zero_after_exclude": len(zero_after_exclude),
        "zero_after_exclude_with_previous_proposal": sum(1 for a in zero_after_exclude
                                                         if any(b["proposed_job_ids"] for b in attempts[:attempts.index(a)])),
        "delivered_job_ids": sent,
        "delivered_call_centers": [j for j in sent if j in cc],
        "back_promise": [x["text"] for x in items if x["kind"] == "say" and BACK_PROMISE.search(x["text"])],
        # 戻りたがった発話と、その後のアシスタントの発話(届く求人と合っているかは delivered_job_ids と見比べる)
        "back_requests": [{"said": x["text"], "replies": [y["text"] for y in items[i + 1:] if y["kind"] == "say"]}
                          for i, x in enumerate(items) if x["kind"] == "user" and BACK_REQUEST.search(x["text"])],
    }


def _voice_cost(session) -> float:
    from line_runtime.ledger import cost_usd

    total = 0.0
    for u in session.usage.model_usage:
        if getattr(u, "type", "") != "llm_usage":
            continue
        cached = u.input_cached_tokens
        total += cost_usd(u.model, max(0, u.input_tokens - cached), u.output_tokens, 0, cached)
    return total


async def run_voice(probes: list[dict], ledger: Path, results: list[dict], save) -> bool:
    from livekit.agents import AgentSession
    from livekit.plugins import openai as lk_openai

    from dip_mcp.data import State
    from evals.meter import Meter
    from evals.run import run_cases, sim_reply
    from evals.scenarios import prepare
    from line_runtime import llm
    from voice_resume import config as vconfig
    from voice_resume.agent import prepare_advisor
    from voice_resume.dip import DipMCP

    meter = Meter().install()  # 模擬ユーザー(line_runtime.llm)の分
    client = llm.client()
    paid = {"voice": 0.0}

    async def run_one(p: dict, rep: int) -> None:
        uid = f"probe-{p['id']}"
        prepare(State(), {"user": "sim-new"}, user=uid)  # 区分は未定・希望なしから
        dip = DipMCP()
        await dip.start()
        session = None
        try:
            advisor, greeting = await prepare_advisor(dip, uid)
            poster = _NoPoster()
            advisor._talk_poster = poster
            advisor._draft_client_factory = _NoDraft
            session = AgentSession(llm=lk_openai.LLM(model=vconfig.LLM_MODEL,
                                                     reasoning_effort=vconfig.LLM_REASONING_EFFORT))
            await session.start(agent=advisor)
            await session.generate_reply(instructions=greeting)
            transcript = []
            for _ in range(VOICE_MAX_TURNS):
                said = [x for x in _items(session.history) if x["kind"] in ("say", "user")]
                transcript = [{"who": "agent" if x["kind"] == "say" else "user", "text": x["text"]} for x in said]
                nxt = await sim_reply(client, p["persona"], transcript)
                text = (nxt.get("text") or "").strip()
                if not text:
                    break
                await session.run(user_input=text)
                if nxt.get("end"):
                    break
            await session.aclose()
            items = _items(session.history)
            results.append({"id": p["id"], "channel": "voice", "title": p["title"],
                            "observation": voice_observation(p, items, advisor.search_attempts, poster.calls)
                            | {"talk_level_as_expected": None},
                            "items": items, "search_attempts": advisor.search_attempts})
            obs = results[-1]["observation"]
            if obs["first_propose_args"] is not None:
                obs["talk_level_as_expected"] = obs["talk_level"] == p.get("expect_talk_level")
        except Exception as e:  # noqa: BLE001
            results.append({"id": p["id"], "channel": "voice", "error": f"{type(e).__name__}: {e}"})
        finally:
            if session is not None:
                cost = _voice_cost(session)
                paid["voice"] += cost
                if results and results[-1]["id"] == p["id"]:
                    results[-1]["cost_usd"] = round(cost, 4)
            await dip.aclose()

    return await run_cases([(p, 0) for p in probes], run_one,
                           lambda: meter.summary()["cost_usd"] + paid["voice"], ledger,
                           f"probe-{datetime.now():%Y%m%d-%H%M%S}-voice", log=lambda m: print(m, flush=True), after=save)


# ----------------------------- 結果 -----------------------------


def report(doc: dict) -> str:
    lines = [f"# 電話の扱いの計測(実 LLM、{doc['at']})", "",
             f"- この実行の費用: ${doc.get('spent_usd', 0):.4f}、費用台帳の累計: ${doc.get('ledger_total_usd', 0):.4f}"
             + ("(上限に達したので途中で止めた)" if doc.get("stopped") else ""), ""]
    for r in doc["results"]:
        lines.append(f"## {r['id']}({r['channel']}): {r.get('title', '')}")
        if "error" in r:
            lines += [f"- エラー: {r['error']}", ""]
            continue
        lines += [f"- {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in r["observation"].items()]
        lines.append(f"- 費用: ${r.get('cost_usd', 0):.4f}")
        lines.append("- 会話:")
        if r["channel"] == "chat":
            lines += ["  - " + ("AI" if t["who"] == "agent" else "本人") + ": " + t["text"].replace("\n", " / ")
                      for t in r["transcript"]]
        else:
            for x in r["items"]:
                if x["kind"] in ("say", "user"):
                    lines.append("  - " + ("AI" if x["kind"] == "say" else "本人") + ": " + x["text"].replace("\n", " / "))
                elif x["kind"] == "call":
                    lines.append(f"  - (ツール {x['name']} {json.dumps(x['args'], ensure_ascii=False)})")
        lines.append("")
    return "\n".join(lines) + "\n"


async def main(only: str | None, ledger: Path = budget.LEDGER) -> int:
    ids = {x.strip() for x in only.split(",")} if only else None
    known = {p["id"] for p in CHAT_PROBES + VOICE_PROBES}
    if ids and (unknown := ids - known):
        raise SystemExit(f"--only に知らない ID: {sorted(unknown)}(ある ID: {sorted(known)})")
    chat = [p for p in CHAT_PROBES if not ids or p["id"] in ids]
    voice = [p for p in VOICE_PROBES if not ids or p["id"] in ids]
    print(budget.line(ledger), flush=True)
    budget.check(ledger)
    out = HERE / "results" / f"probe-{datetime.now():%Y%m%d-%H%M%S}"
    out.parent.mkdir(exist_ok=True)
    start = budget.spent(ledger)
    doc = {"at": f"{datetime.now():%Y-%m-%d %H:%M}", "results": [], "stopped": None}

    def save() -> None:
        doc["ledger_total_usd"] = budget.spent(ledger)
        doc["spent_usd"] = round(doc["ledger_total_usd"] - start, 4)
        out.with_suffix(".json").write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str))
        out.with_suffix(".md").write_text(report(doc))

    stopped = False
    try:
        # 音声を先に(task 127 の後続はどれも音声で、予算の残りが少ないときに先に見たい)
        if voice:
            stopped = await run_voice(voice, ledger, doc["results"], save)
        if chat and not stopped:
            stopped = await run_chat(chat, ledger, doc["results"], save)
    except BaseException:
        doc["stopped"] = "interrupted"
        save()
        raise
    doc["stopped"] = "budget" if stopped else None
    save()
    print(f"この実行 ${doc['spent_usd']:.4f} / {budget.line(ledger)}")
    print(f"-> {out}.md")
    return 2 if stopped else 0


if __name__ == "__main__":
    from line_runtime.config import load_env

    load_env()
    tmp = Path(tempfile.mkdtemp())
    os.environ["DIP_STATE_PATH"] = str(tmp / "state.json")
    os.environ["DIP_KEYWORDS_TRACE"] = str(tmp / "keywords.jsonl")
    os.environ.setdefault("VOICE_RESUME_OUT_DIR", str(tmp / "voice-out"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="回す ID(カンマ区切り)")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(main(args.only)))
    except budget.BudgetExceeded as e:
        print(f"!! {e}")
        sys.exit(2)
