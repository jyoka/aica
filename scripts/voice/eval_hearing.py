"""ヒアリングの深掘り回数と誘導質問を、台本の本人役 × 実 API で測る(タスク 38)。

2026-09-26 の実会話で、経験の深掘りが3回続き(上限は1回)、適性を聞いて答えを
誘導する質問(「〜はご自身に合いそうですか?」)が出た。同じ本人の答えの台本で
会話の脳(gpt-6-sol)だけを本番と同じ指示で回し、直る前後の割合を測る:

    ./.venv/bin/python scripts/voice/eval_hearing.py --runs 5 --label before

STT / TTS / LiveKit は通さない。求人の提案に入る前(資格・免許の質問)で打ち切るので
ツールも呼ばれない。結果(全会話・集計・概算費用)は results/hearing-eval-<label>-<時刻>.md。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # isort: skip

from openai import AsyncOpenAI

from dip_mcp.logic import counseling_playbook
from voice_resume import config
from voice_resume.flow import count_experience_probes, find_leading_questions
from voice_resume.prompts import advisor_instructions, greeting_instructions

RESULTS = config.RESULTS_DIR
MAX_ASSISTANT_TURNS = 12

# 本番と同じ指示: アルバイト・パートの面談(区分の音声版の playbook つき)と、その最初の発話への指示
TRACK = "アルバイト・パート"
ADVISOR_INSTRUCTIONS = advisor_instructions(TRACK, counseling_playbook(TRACK, "voice")["playbook"])
GREETING = greeting_instructions(track=TRACK)

# gpt-6-sol 入出力の単価(USD/1M tokens)。scripts/e2e_audio_session.py と同じ
PRICE_IN, PRICE_OUT = 2.00, 10.00


# 希望条件の質問への答え。上から順に見て、1つの話題には1回だけ答える
_WISH_RULES = (
    ("area", ("エリア", "駅", "あたり", "場所", "どこ"), "中野のあたりがいいです"),
    ("job_type", ("探し", "職種"), "コンビニがいいです"),
    ("days", ("週", "日数"), "週3日くらいです"),
    ("time_band", ("曜日", "時間帯", "何時", "時間"), "平日の夕方がいいです"),
    ("wage", ("時給",), "1200円くらいだと嬉しいです"),
)


class ScriptedUser:
    """2026-09-26 の会話と同じ答えを返す本人役(ファミレスのキッチンで調理をしていた人)。

    アドバイザーの質問文(発話の中の質問の文だけ)にキーワードで答えを割り当てる。
    「週3日くらいですね。時給は?」のような相づち込みの発話で誤答しないため。
    資格・免許を聞かれたら台本は終わり(経験と「できること」の段階まで見れば足りる)。
    """

    def __init__(self) -> None:
        self._said_famires = False
        self._said_kitchen = False
        self._answered: set[str] = set()

    @staticmethod
    def _question_part(text: str) -> str:
        """発話のうち、質問・依頼の文だけを取り出す(無ければ全文)。"""
        sentences = re.split(r"(?<=[。！!?？])", text)
        asks = [s for s in sentences if re.search(r"[?？]|か[。]?\s*$|ください", s)]
        return "".join(asks) or text

    def reply(self, question: str) -> str | None:
        q = self._question_part(question)
        if "資格" in q or "免許" in q:
            return None
        if not self._said_famires:
            for topic, keywords, answer in _WISH_RULES:
                if topic not in self._answered and any(k in q for k in keywords):
                    self._answered.add(topic)
                    return answer
            if any(k in q for k in ("経験", "働いたこと", "これまで", "したこと", "仕事をされて")):
                self._said_famires = True
                return "あ、ファミレス"
            return "特にこだわりはないです"
        # 経験(ファミレス)を答えたあと: 深掘りにも事実で短く答える
        if not self._said_kitchen and ("キッチン" in q or "ホール" in q):
            self._said_kitchen = True
            return "キッチンです"
        if any(k in q for k in ("調理", "盛り付け", "片付け", "仕込み", "作業")):
            return "調理をしていました"
        if any(k in q for k in ("合いそう", "得意", "向い", "好き")):
            return "はい、たぶん"
        if any(k in q for k in ("まとめ", "違うところ", "違い", "合ってい", "よろしい")):
            return "はい、合っています"
        return "キッチンで調理をしていました"


async def run_once(client: AsyncOpenAI, run_no: int) -> dict:
    """1会話を回して、文字起こしと使用トークンを返す。"""
    user = ScriptedUser()
    messages: list[dict] = [
        {"role": "system", "content": ADVISOR_INSTRUCTIONS},
        {"role": "system", "content": GREETING},
    ]
    transcript: list[dict] = []
    tokens = {"in": 0, "out": 0}
    for _ in range(MAX_ASSISTANT_TURNS):
        resp = await client.chat.completions.create(
            model=config.LLM_MODEL,
            reasoning_effort=config.LLM_REASONING_EFFORT,
            messages=messages,
        )
        if resp.usage:
            tokens["in"] += resp.usage.prompt_tokens
            tokens["out"] += resp.usage.completion_tokens
        text = (resp.choices[0].message.content or "").strip()
        messages.append({"role": "assistant", "content": text})
        transcript.append({"role": "assistant", "text": text})
        answer = user.reply(text)
        if answer is None:  # 資格の質問まで来たら台本は終わり
            break
        messages.append({"role": "user", "content": answer})
        transcript.append({"role": "user", "text": answer})
    print(f"run {run_no}: assistant {sum(1 for t in transcript if t['role'] == 'assistant')} 発話")
    return {"transcript": transcript, "tokens": tokens}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--label", default="eval", help="結果ファイル名に付く印(before / after)")
    args = parser.parse_args()

    config.load_env()
    client = AsyncOpenAI()
    runs = await asyncio.gather(*(run_once(client, i + 1) for i in range(args.runs)))

    rows = []
    for i, run in enumerate(runs, 1):
        probes = count_experience_probes(run["transcript"])
        leading = find_leading_questions(run["transcript"])
        rows.append({"run": i, "probes": probes, "leading": leading})
    ok_runs = sum(1 for r in rows if r["probes"] <= 1)
    leading_total = sum(len(r["leading"]) for r in rows)
    tokens_in = sum(r["tokens"]["in"] for r in runs)
    tokens_out = sum(r["tokens"]["out"] for r in runs)
    cost = tokens_in * PRICE_IN / 1e6 + tokens_out * PRICE_OUT / 1e6

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    lines = [
        f"# ヒアリング評価({args.label})",
        "",
        f"- 日時: {stamp} UTC / LLM: {config.LLM_MODEL}(reasoning={config.LLM_REASONING_EFFORT})",
        "- 台本: 2026-09-26 の実会話と同じ本人の答え(ファミレスのキッチンで調理)。資格・免許の質問で打ち切り",
        f"- 経験の深掘りが1回以内: {ok_runs}/{args.runs} 回",
        f"- 誘導する質問(適性・好みを先回りして聞く形): 合計 {leading_total} 回",
        f"- 費用: ${cost:.4f}(in {tokens_in} / out {tokens_out} tokens)",
        "",
    ]
    for row, run in zip(rows, runs):
        lines.append(f"## run {row['run']}(深掘り {row['probes']} 回, 誘導 {len(row['leading'])} 回)")
        lines.append("")
        for hit in row["leading"]:
            lines.append(f"- 誘導: {hit}")
        if row["leading"]:
            lines.append("")
        lines.extend(
            f"**{'本人' if t['role'] == 'user' else 'アドバイザー'}**: {t['text']}\n"
            for t in run["transcript"]
        )
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"hearing-eval-{args.label}-{stamp}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"深掘り1回以内: {ok_runs}/{args.runs} / 誘導: {leading_total} 回 / 費用: ${cost:.4f}")
    print(f"結果: {out}")


if __name__ == "__main__":
    asyncio.run(main())
