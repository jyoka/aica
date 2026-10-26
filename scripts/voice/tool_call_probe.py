"""「探します」「調べます」と言った同じターンでツールが呼ばれるかの実測プローブ(タスク 36)。

2026-09-26 の実会話で、アドバイザーが「時給の下限を外して探します」と言ったのに
propose_jobs を呼ばず、本人が促すまで検索されなかった。その場面(と調べものの場面)を
台本で再現し、実 LLM(voice_resume.voice_llm.create_llm。既定は claude-sonnet-5-5)に対して「同じターンでツールが呼ばれた回数 / 試行回数」を測る。

LLM 以外は呼ばない: 求人検索(dip MCP)と web 検索はスタブ。1試行 = セッションを作り、
実会話から起こした履歴を chat_ctx に入れ、引き金の発話1つを session.run で流す。

    ./.venv/bin/python scripts/voice/tool_call_probe.py --trials 5

終わりに試行ごとの結果・成功率・トークン量と概算費用($2/M in, $10/M out)を出す。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # isort: skip

from livekit.agents import AgentSession, llm
from livekit.agents.voice.run_result import ChatMessageEvent, FunctionCallEvent

from voice_resume import config, voice_llm
from voice_resume.advisor import ResumeAdvisor

PRICE_IN_PER_M = 2.00  # claude-sonnet-5-5 と gpt-6-sol の USD/1M input tokens(キャッシュの読みも同じ単価で数える概算)
PRICE_OUT_PER_M = 10.00  # claude-sonnet-5-5 と gpt-6-sol の USD/1M output tokens

# 問題が出た実会話(2026-09-26、旧リポジトリの voice-resume/out/transcript-20260925-155708.json)の
# 発話 0〜39 の verbatim(STT の誤変換もそのまま)+ その裏で実際に起きたツール呼び出し。
# この直後の発話40「まあいっか 一旦、地球の 加減を外して」が問題の引き金。
NO_JOBS_OUTPUT = (
    "条件に合う求人が見つかりませんでした。条件を1つゆるめると見つかるかもしれない、"
    "と伝えてください(例: エリアや時給)。"
)
RELAX_WAGE_HISTORY = [
    ("assistant", ("こんにちは、バイトルのキャリアアドバイザーです。5分ほどで希望と経験を伺い、"
     "最後に求人の提案と履歴書の下書きをお出しします。どんなバイトを探していますか?")),
    ("user", "決めてないな。"),
    ("assistant", "まだ決まっていなくて大丈夫です。働きたいエリアや最寄り駅はありますか?"),
    ("user", "東京都内であれば大丈夫"),
    ("assistant", "東京都内ですね。働ける曜日や時間帯はありますか?"),
    ("user", "週3回 午後"),
    ("assistant", "週3回、午後ですね。希望する時給の下限はありますか?こだわりがなければ「なし」で大丈夫です。"),
    ("user", "東京平均値はどのぐらい"),
    ("assistant", "少し調べますね。"),
    ("tool_call", "wage_average", '{"area": "東京"}',
     "東京都のアルバイト・パートの平均時給は1428円です(マイナビ 2026年8月調査)。"),
    ("assistant", ("マイナビの2026年8月の調査では、東京都のアルバイト・パートの平均時給は1428円です。"
     "希望する時給の下限はありますか?")),
    ("user", "基本平均時給 が ちょっと 高め"),
    ("assistant", "東京の平均より少し高めですね。時給1500円以上を目安にしてよいですか?"),
    ("user", "まそのぐらい。"),
    ("assistant", "時給1500円以上で探しますね。これまでに、バイトや仕事の経験はありますか?"),
    ("user", "あるよ"),
    ("assistant", "ありがとうございます。どんな仕事をされていましたか?"),
    ("user", "の 料理"),
    ("assistant", "料理のお仕事ですね。続きをどうぞ。"),
    ("user", "あ、ファミレス"),
    ("assistant", "ファミレスでのお仕事ですね。担当していたのは、キッチンですか?"),
    ("user", "はい。"),
    ("assistant", "ファミレスのキッチンをされていたなら、調理や盛り付け、片付けの中で経験のある作業はありますか?"),
    ("user", "調理かな"),
    ("assistant", ("調理の経験があるのですね。料理を作る仕事や、決まった手順に沿って進める仕事は、"
     "ご自身に合いそうですか?")),
    ("user", "アウトも"),
    ("assistant", "すみません、「アウトも」はどんな意味でしょうか?続きをどうぞ。"),
    ("user", "と思う"),
    ("assistant", ("料理を作る仕事などが合いそう、ということですね。持っている資格や免許はありますか?"
     "なければ「なし」で大丈夫です。")),
    ("user", "ない"),
    ("assistant", ("資格や免許はなしですね。東京都内で週3回、午後、時給1500円以上を希望し、"
     "ファミレスのキッチンで調理をした経験がある、という内容で合っていますか?")),
    ("user", "合ってる"),
    ("tool_call", "propose_jobs",
     '{"job_type": "飲食キッチン", "area": "東京", "time_band": "日中", "min_wage": 1500}',
     NO_JOBS_OUTPUT),
    ("assistant", "今の条件では、合う求人が見つかりませんでした。時給の下限を少し下げて、もう一度探してもよいですか?"),
    ("user", "あ、いいよ"),
    ("assistant", "ありがとうございます。時給の下限を1400円にして探してもよいですか?"),
    ("user", "はい"),
    ("tool_call", "propose_jobs",
     '{"job_type": "飲食キッチン", "area": "東京", "time_band": "日中", "min_wage": 1400}',
     NO_JOBS_OUTPUT),
    ("assistant", "時給1400円以上でも、該当する求人は見つかりませんでした。今回は求人なしで下書きを保存しますか?"),
    ("user", "逆にどういう条件であれば見つかる"),
    ("assistant", ("今の検索結果だけでは、どの条件なら見つかるかは断定できません。"
     "まず時給の下限を外して、ほかの条件はそのままで探してもよいですか?")),
    ("user", "逆に 教 と か"),
    ("user", "でも他の 条件もないか"),
]

RESEARCH_HISTORY = [
    ("assistant", ("こんにちは、バイトルのキャリアアドバイザーです。5分ほどで希望と経験を伺い、"
     "最後に求人の提案と履歴書の下書きをお出しします。どんなバイトを探していますか?")),
    ("user", "飲食のホールを考えています"),
    ("assistant", "飲食ホールですね。働きたいエリアか最寄り駅はありますか?"),
]

SCENARIOS = {
    # 実会話の発話40: 条件を変えて探すと言う場面 → 同じターンで propose_jobs が呼ばれるべき
    "relax_wage": {
        "history": RELAX_WAGE_HISTORY,
        "user_input": "まあいっか 一旦、地球の 加減を外して",  # STT の誤変換ごと verbatim
        "expected_tools": {"propose_jobs"},
    },
    # 会話の途中の調べもの → 「少し調べますね」と同じターンで web_research が呼ばれるべき
    "research": {
        "history": RESEARCH_HISTORY,
        "user_input": "その前に、ホールの仕事って実際どんなことをするのか調べてもらえますか",
        "expected_tools": {"web_research"},
    },
}


class StubDip:
    """recommend_jobs を記録して0件を返す(実会話と同じ「見つからない」状況)。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def recommend_jobs(self, **args) -> dict:
        self.calls.append(args)
        return {"total": 0, "jobs": []}


def stub_research_client():
    async def create(**kwargs):
        return SimpleNamespace(
            output_text="ホールのお仕事は、お客様のご案内と注文取り、料理の提供、片付けが中心です。"
        )

    return SimpleNamespace(responses=SimpleNamespace(create=create))


async def run_trial(scenario: dict) -> tuple[bool, list[str], str, dict]:
    """1試行。返り値: (同じターンで期待ツールが呼ばれたか, 呼ばれたツール名, 最初の発話, トークン量)。"""
    advisor = ResumeAdvisor(dip=StubDip(), research_client_factory=stub_research_client)
    session = AgentSession(
        llm=voice_llm.create_llm()
    )
    await session.start(agent=advisor)
    ctx = advisor.chat_ctx.copy()
    for i, entry in enumerate(scenario["history"]):
        if entry[0] == "tool_call":
            _, name, arguments, output = entry
            call_id = f"probe_{i}"
            ctx.items.append(llm.FunctionCall(call_id=call_id, name=name, arguments=arguments))
            ctx.items.append(
                llm.FunctionCallOutput(call_id=call_id, name=name, output=output, is_error=False)
            )
        else:
            role, text = entry
            ctx.add_message(role=role, content=text)
    await advisor.update_chat_ctx(ctx)

    result = await session.run(user_input=scenario["user_input"])

    called = [e.item.name for e in result.events if isinstance(e, FunctionCallEvent)]
    first_reply = next(
        (e.item.text_content or "" for e in result.events if isinstance(e, ChatMessageEvent)), ""
    )
    tokens = {"in": 0, "out": 0}
    for usage in session.usage.model_usage:
        if getattr(usage, "type", "") == "llm_usage":
            tokens["in"] += usage.input_tokens
            tokens["out"] += usage.output_tokens
    await session.aclose()
    ok = scenario["expected_tools"] <= set(called)
    return ok, called, first_reply, tokens


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--scenario", choices=[*SCENARIOS], default=None)
    args = parser.parse_args()

    config.load_env()
    import os

    key = "OPENAI_API_KEY" if config.PROVIDER == "openai" else "ANTHROPIC_API_KEY"
    if not os.environ.get(key):
        raise SystemExit(f"{key} がありません(.env を確認)")

    from livekit.agents.utils import http_context

    total = {"in": 0, "out": 0}
    async with http_context.open():
        for name in [args.scenario] if args.scenario else list(SCENARIOS):
            scenario = SCENARIOS[name]
            hits = 0
            print(f"\n== {name}: 「{scenario['user_input']}」 ==")
            for i in range(1, args.trials + 1):
                ok, called, reply, tokens = await run_trial(scenario)
                hits += ok
                total["in"] += tokens["in"]
                total["out"] += tokens["out"]
                print(f"[{i}/{args.trials}] {'OK' if ok else 'NG'} "
                      f"ツール={called or 'なし'} 発話: {reply[:60]}")
            print(f"→ 同じターンでツールが呼ばれた: {hits}/{args.trials}")

    cost = total["in"] * PRICE_IN_PER_M / 1e6 + total["out"] * PRICE_OUT_PER_M / 1e6
    print(f"\nLLM({config.LLM_MODEL}): in {total['in']} / out {total['out']} tokens ≒ ${cost:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
