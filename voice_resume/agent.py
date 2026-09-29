"""LiveKit ワーカー。ブラウザ(マイク)⇔ アドバイザーの音声会話をつなぐ。

    ./.venv/bin/python -m voice_resume.agent download-files   # 初回のみ(VAD / turn detector のモデル)
    ./.venv/bin/python -m voice_resume.agent dev              # LiveKit Cloud に接続して待受

構成: Deepgram STT(nova-3 日本語 + バイト用語の keyterm)→ OpenAI gpt-6-sol
→ Cartesia TTS(日本語ボイス・少し遅め)。
ターンの判定は Silero VAD + LiveKit turn detector(多言語)。「えーと」で考えている間は
打ち切らず、終わり判定は少し待つ(config.MIN/MAX_ENDPOINTING_DELAY)。
"""

from __future__ import annotations

import asyncio
import json
import logging

from livekit import agents
from livekit.agents import AgentSession
from livekit.plugins import cartesia, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from voice_resume import config
from voice_resume.advisor import ResumeAdvisor
from voice_resume.dip import DipMCP
from voice_resume.prompts import greeting_instructions
from voice_resume.stt import create_stt

config.load_env()

logger = logging.getLogger("voice-resume.agent")


def prewarm(proc: agents.JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


def participant_user_id(participant) -> str | None:
    """参加者の metadata(JSON の {"user_id": ...})からユーザーID(匿名ユーザーID のスコープ)を読む。

    トークの音声相談のシートから入ると、line_runtime が接続トークンに載せる(voice_resume/livekit_token.py)。
    metadata の無い参加者は None(dip に何も書かない相談になる)。
    """
    try:
        meta = json.loads(participant.metadata or "{}")
    except (TypeError, ValueError):
        return None
    uid = meta.get("user_id") if isinstance(meta, dict) else None
    return str(uid) if uid else None


async def prepare_advisor(dip, user_id: str | None, room=None) -> tuple[ResumeAdvisor, str]:
    """面談を始める前の準備。dip の要約を読んでアドバイザーと最初の発話への指示を組み立てる。"""
    # 会話の前に dip の要約を読み、前に話した希望・相談で分かったこと(stated_preferences)、前の会話の
    # 相談のまとめ(consultation_summary)、トークでアップロードした履歴書(uploaded_resume)があれば確認形で使う
    # (LINE 側のプロンプトと同じ要約・同じ決まり。同じことを聞き直さない)
    stated: dict | None = None
    resume: dict | None = None
    consultation: dict | None = None
    if dip is not None and user_id:
        try:
            summary = await dip.get_user_conversation_summary(user_id)
            stated = summary.get("stated_preferences") or None
            resume = summary.get("uploaded_resume") or None
            consultation = summary.get("consultation_summary") or None
        except Exception as exc:  # noqa: BLE001 - 要約が読めなくても面談は始める
            logger.warning("get_user_conversation_summary 失敗(希望・履歴書の確認なしで続行): %s", exc)
    logger.info("面談を開始: user_id=%s 前回の希望=%s 相談のまとめ=%s 履歴書=%s", user_id or "(不明)",
                stated or "なし", "あり" if consultation else "なし", "あり" if resume else "なし")

    advisor = ResumeAdvisor(dip=dip, room=room, user_id=user_id, stated_preferences=stated, uploaded_resume=resume,
                            consultation_summary=consultation)
    return advisor, greeting_instructions(stated, resume)


async def entrypoint(ctx: agents.JobContext) -> None:
    # 求人の提案: dip 側 MCP サーバー(dip_mcp)へ stdio でつなぐ。失敗しても面談は続ける
    dip: DipMCP | None = DipMCP()
    try:
        await dip.start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("dip MCP サーバーに接続できません(求人提案なしで続行): %s", exc)
        dip = None
    if dip is not None:
        ctx.add_shutdown_callback(dip.aclose)

    # どのユーザーの相談かを、参加者の metadata から受け取る(トークから入ったとき)
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    user_id = participant_user_id(participant)

    advisor, greeting = await prepare_advisor(dip, user_id, room=ctx.room)
    session = AgentSession(
        stt=create_stt(),
        llm=openai.LLM(model=config.LLM_MODEL, reasoning_effort=config.LLM_REASONING_EFFORT),
        tts=cartesia.TTS(
            model=config.TTS_MODEL,
            voice=config.TTS_VOICE,
            language=config.TTS_LANGUAGE,
            speed=config.TTS_SPEED,
            word_timestamps=False,  # 日本語 + sonic 系では未対応
        ),
        vad=ctx.proc.userdata["vad"],
        # 「えーと」で考えている途中に打ち切らない: 多言語 turn detector +
        # 終わり判定の待ちを長めにする。エコーによる誤割り込みは
        # min_duration を長めに + 誤割り込みからの自動再開で抑える
        turn_handling={
            "turn_detection": MultilingualModel(),
            "endpointing": {
                "min_delay": config.MIN_ENDPOINTING_DELAY,
                "max_delay": config.MAX_ENDPOINTING_DELAY,
            },
            "interruption": {
                "min_duration": config.MIN_INTERRUPTION_DURATION,
                "resume_false_interruption": True,
                "false_interruption_timeout": config.FALSE_INTERRUPTION_TIMEOUT,
            },
        },
    )
    await session.start(agent=advisor, room=ctx.room)

    # 画面の「面談を終える」ボタン → 下書きを保存して締める
    def _on_data(packet) -> None:
        try:
            payload = json.loads(bytes(packet.data).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        if payload.get("type") == "end":
            asyncio.create_task(
                session.generate_reply(
                    instructions=(
                        "本人が画面の「面談を終える」を押しました。まだ保存していなければ "
                        "finalize_interview を呼んで下書きを保存し、短くお礼を述べて締めてください。"
                    )
                )
            )

    ctx.room.on("data_received", _on_data)

    await session.generate_reply(instructions=greeting)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
