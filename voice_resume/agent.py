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
from voice_resume.advisor import ResumeAdvisor, _post_json
from voice_resume.dip import DipMCP
from voice_resume.jobs import DEFAULT_TRACK
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
    """面談を始める前の準備。dip の要約と、今の雇用区分の playbook(音声版)を読んで、アドバイザーと最初の発話への
    指示を組み立てる。"""
    # 会話の前に dip の要約を読み、今の雇用区分(search_track)、前に話した希望・相談で分かったこと
    # (stated_preferences)、前の会話の相談のまとめ(consultation_summary)、トークでアップロードした履歴書
    # (uploaded_resume)があれば確認形で使う(LINE 側のプロンプトと同じ要約・同じ決まり。同じことを聞き直さない)
    stated: dict | None = None
    resume: dict | None = None
    consultation: dict | None = None
    track = DEFAULT_TRACK
    if dip is not None and user_id:
        try:
            summary = await dip.get_user_conversation_summary(user_id)
            stated = summary.get("stated_preferences") or None
            resume = summary.get("uploaded_resume") or None
            consultation = summary.get("consultation_summary") or None
            track = summary.get("search_track") or DEFAULT_TRACK
        except Exception as exc:  # noqa: BLE001 - 要約が読めなくても面談は始める
            logger.warning("get_user_conversation_summary 失敗(希望・履歴書の確認なしで続行): %s", exc)
    # 相談の進め方は、チャットと同じく dip が配る区分ごとの playbook(音声版)。読めなければ最小の進め方で始める
    playbook: str | None = None
    if dip is not None:
        try:
            playbook = (await dip.get_counseling_playbook(track, "voice")).get("playbook") or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_counseling_playbook 失敗(最小の進め方で続行): %s", exc)
    logger.info("面談を開始: user_id=%s 区分=%s 前回の希望=%s 相談のまとめ=%s 履歴書=%s playbook=%s",
                user_id or "(不明)", track, stated or "なし", "あり" if consultation else "なし",
                "あり" if resume else "なし", "あり" if playbook else "なし")

    advisor = ResumeAdvisor(dip=dip, room=room, user_id=user_id, stated_preferences=stated, uploaded_resume=resume,
                            consultation_summary=consultation, track=track, playbook=playbook)
    return advisor, greeting_instructions(stated, resume, track)


async def report_session(room: str, user_id: str, state: str, poster=None) -> bool:
    """部屋のジョブの始まり(joined: 本人が入った)と終わり(ended)を、トーク(line_runtime の /api/voice/session)へ
    知らせる。共有鍵を付ける。トークがその人の別の部屋を知っていれば False(このジョブは止める)。
    届かなければ True(面談は続ける。トークは接続トークンを発行するときに 1 人 1 部屋を確かめ、時間でも部屋を終わったとみなす)。"""
    try:
        got = await (poster or _post_json)(f"{config.TALK_URL}/api/voice/session",
                                           {"user_id": user_id, "room": room, "state": state})
    except Exception as exc:  # noqa: BLE001
        logger.warning("トークへ部屋の%sを知らせられません: %s", "始まり" if state == "joined" else "終わり", exc)
        return True
    return (got or {}).get("allowed", True) is not False


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

    if user_id:  # トークの部屋: ジョブが終わるまで、その人は次の部屋を開けない(line_runtime.voice_rooms)
        if not await report_session(ctx.room.name, user_id, "joined"):
            logger.warning("同じ人の別の部屋が終わっていないので、この部屋の相談は始めない: room=%s", ctx.room.name)
            ctx.shutdown(reason="the person already has another voice room")
            return

        async def _ended() -> None:
            await report_session(ctx.room.name, user_id, "ended")

        ctx.add_shutdown_callback(_ended)

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
    # 本人が抜けてセッションが閉じたら、部屋が閉じるのを待たずにジョブも終える(プロセスと dip MCP の子プロセスを返し、
    # トークへ ended を知らせる)。close は話しかけの途中の発話を止めた後に出るので、ここで終えても失うものは無い
    session.on("close", lambda _ev: ctx.shutdown(reason="agent session closed"))
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
                        "finalize_interview を呼んで(summary に相談のまとめを入れて)下書きを保存し、短くお礼を述べて締めてください。"
                    )
                )
            )

    ctx.room.on("data_received", _on_data)

    await session.generate_reply(instructions=greeting)


def job_load(worker) -> float:
    """ワーカーの負荷: 動いているジョブの数 / MAX_JOBS。load_threshold=1 と合わせて、MAX_JOBS 個で満員を報告する。"""
    return len(worker.active_jobs) / max(config.MAX_JOBS, 1)


def worker_options() -> agents.WorkerOptions:
    """ワーカーの設定。同時に受けるジョブは config.MAX_JOBS まで(dev でも。livekit-agents の既定は、dev では上限なし、
    start では CPU 70%)。ジョブのプロセスのメモリが config.JOB_MEMORY_LIMIT_MB を超えたら、そのジョブを止める。

    温めておくプロセスの数も MAX_JOBS にする。livekit-agents は、動いているジョブが 0 個の間、受付中(起動前)の
    ジョブ 1 件の負荷を load_threshold / num_idle_processes と見積もる。dev の既定の 0 のままだと 1 件で満員になり、
    最初のジョブが起動するまでの数秒に来た 2 人目を断ってしまう。"""
    max_jobs = max(config.MAX_JOBS, 1)
    return agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, load_fnc=job_load,
                                load_threshold=1.0, num_idle_processes=max_jobs,
                                job_memory_limit_mb=config.JOB_MEMORY_LIMIT_MB)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agents.cli.run_app(worker_options())
