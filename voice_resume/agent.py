"""LiveKit ワーカー。ブラウザ(マイク)⇔ アドバイザーの音声会話をつなぐ。

    ./.venv/bin/python -m voice_resume.agent download-files   # 初回のみ(VAD / turn detector のモデル)
    ./.venv/bin/python -m voice_resume.agent dev              # LiveKit Cloud に接続して待受

構成: Deepgram STT(nova-3 日本語 + バイト用語の keyterm)→ Anthropic Claude Sonnet 5.5(VOICE_RESUME_PROVIDER=openai
なら OpenAI gpt-6-sol。voice_resume/voice_llm.py)→ Cartesia TTS(日本語ボイス・少し遅め)。
ターンの判定は Silero VAD + LiveKit turn detector(多言語)。「えーと」で考えている間は
打ち切らず、終わり判定は少し待つ(config.MIN/MAX_ENDPOINTING_DELAY)。
1 回の相談には上限がある: 長さは config.MAX_SESSION_MINUTES で、時間が来たら締めて閉じる。「面談を終える」(end パケット)
への返答は 1 回だけ(SessionGuard)。web の調べものの回数は advisor が config.MAX_WEB_SEARCHES で止める。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from livekit import agents
from livekit.agents import AgentSession
from livekit.plugins import cartesia, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from line_runtime import line_channel
from line_runtime.log_policy import quiet_sdk_loggers
from voice_resume import config, voice_llm
from voice_resume.advisor import ResumeAdvisor, _post_json, post_failure
from voice_resume.dip import DipMCP
from voice_resume.jobs import DEFAULT_TRACK
from voice_resume.prompts import greeting_instructions
from voice_resume.stt import create_stt

config.load_env()

logger = logging.getLogger("voice-resume.agent")

# 締めの返答(下書きの保存とお礼)を待つ上限(秒)。時間切れで締めるとき、これを過ぎたら返答を止めて閉じる
CLOSING_REPLY_TIMEOUT = 90.0

END_INSTRUCTIONS = (
    "本人が画面の「面談を終える」を押しました。まだ保存していなければ "
    "finalize_interview を呼んで(summary に相談のまとめを入れて)下書きを保存し、短くお礼を述べて締めてください。"
)


def time_up_instructions(minutes: float) -> str:
    return (
        f"相談の時間の上限({minutes:g}分)になりました。まだ保存していなければ finalize_interview を呼んで"
        "(summary に相談のまとめを入れて)下書きを保存し、時間になったので今回はここで終わることを伝えて、"
        "短くお礼を述べて締めてください。新しい質問はしないこと。"
    )


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
            logger.warning("get_user_conversation_summary 失敗(希望・履歴書の確認なしで続行): %s", type(exc).__name__)
    # 相談の進め方は、チャットと同じく dip が配る区分ごとの playbook(音声版)。読めなければ最小の進め方で始める
    playbook: str | None = None
    if dip is not None:
        try:
            playbook = (await dip.get_counseling_playbook(track, "voice")).get("playbook") or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_counseling_playbook 失敗(最小の進め方で続行): %s", type(exc).__name__)
    # ログには値を出さない(あるかないかと項目の数だけ。docs/design/privacy.md の 3.4)
    logger.info("面談を開始: user_id=%s 区分=%s 前回の希望=%s 相談のまとめ=%s 履歴書=%s playbook=%s",
                user_id or "(不明)", track, f"あり({len(stated)} 項目)" if stated else "なし",
                "あり" if consultation else "なし", "あり" if resume else "なし", "あり" if playbook else "なし")

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
        logger.error("トークへ部屋の%sを知らせられません: %s", "始まり" if state == "joined" else "終わり",
                     post_failure(exc))
        return True
    return (got or {}).get("allowed", True) is not False


class SessionGuard:
    """1 回の音声相談の上限。時間が来たら締めて閉じる。「面談を終える」(end パケット)への返答は 1 回だけ作る。

    上限が無いと、本人がつながっている限り STT・LLM・TTS が動き続け、end パケットを送るたびに返答が出る
    (セキュリティ監査 run-1 の 4 番、review-2026-10 の R6)。接続トークンの期限は部屋に入る期限で、相談の長さではない。

    時間が来たら、締めの返答が途中ならそれを待ち、まだ下書きを保存していないか、締めの返答をまだ作っていなければ
    時間切れの締めの返答を作る(「面談を終える」の返答が割り込まれて保存できなかったときも、下書きを残す)。
    """

    def __init__(self, ctx, session, advisor, *, max_seconds: float,
                 closing_timeout: float = CLOSING_REPLY_TIMEOUT) -> None:
        self._ctx = ctx
        self._session = session
        self._advisor = advisor  # finalized(下書きを保存したか)を見る
        self._max_seconds = max_seconds
        self._closing_timeout = closing_timeout
        self._closing = None  # 締めの返答の SpeechHandle。end パケットの返答は、これがあれば作らない
        self._timer: asyncio.Task | None = None
        self._closing_down = False  # 自分で閉じている最中(session の close で自分のタイマーを止めない)

    def start(self) -> None:
        self._timer = asyncio.create_task(self._close_at_limit())
        # 本人が切って session が先に閉じたら、タイマーも止める
        self._session.on("close", lambda *_: self._cancel_timer())
        self._ctx.add_shutdown_callback(self.aclose)

    def on_end(self) -> None:
        """画面の「面談を終える」。締めの返答は相談ごとに 1 回だけ(2 回目からの end パケットは返答を作らない)。"""
        if self._closing is not None or self._closing_down:
            logger.info("end パケットを無視(締めの返答は作成済み)")
            return
        try:
            self._closing = self._session.generate_reply(instructions=END_INSTRUCTIONS)
        except RuntimeError as exc:  # session が閉じかけている
            logger.warning("締めの返答を作れません: %s", type(exc).__name__)

    @property
    def closing_down(self) -> bool:
        """時間切れで自分で閉じている最中か。"""
        return self._closing_down

    async def aclose(self) -> None:
        self._cancel_timer()

    def _cancel_timer(self) -> None:
        if self._timer is not None and not self._timer.done() and not self._closing_down:
            self._timer.cancel()

    async def _close_at_limit(self) -> None:
        await asyncio.sleep(self._max_seconds)
        minutes = self._max_seconds / 60
        logger.info("相談の時間の上限(%g分)になったので締めます", minutes)
        if self._closing is not None and not self._closing.done():
            await self._wait_for(self._closing)
        if self._closing is None or self._advisor.finalized is None:
            try:
                self._closing = self._session.generate_reply(
                    instructions=time_up_instructions(minutes), allow_interruptions=False)
            except RuntimeError as exc:
                logger.warning("時間切れの締めの返答を作れません: %s", type(exc).__name__)
            else:
                await self._wait_for(self._closing)
        await self._close("voice session time limit")

    async def _wait_for(self, handle) -> None:
        try:
            await asyncio.wait_for(handle.wait_for_playout(), self._closing_timeout)
        except TimeoutError:
            logger.warning("締めの返答が %g 秒で終わらないので止めます", self._closing_timeout)
            handle.interrupt(force=True)

    async def _close(self, reason: str) -> None:
        self._closing_down = True
        try:
            await self._session.aclose()
        except Exception as exc:  # noqa: BLE001 - 閉じるのに失敗しても部屋は消す
            logger.error("AgentSession を閉じられませんでした: %s", type(exc).__name__)
        # 部屋を消して本人の接続も切る(画面は「切れた」を受けて、結果を待つ表示に移る)
        await self._ctx.delete_room()
        self._ctx.shutdown(reason=reason)


async def entrypoint(ctx: agents.JobContext) -> None:
    # 求人の提案: dip 側 MCP サーバー(dip_mcp)へ stdio でつなぐ。失敗しても面談は続ける
    dip: DipMCP | None = DipMCP()
    try:
        await dip.start()
    except Exception as exc:  # noqa: BLE001
        logger.warning("dip MCP サーバーに接続できません(求人提案なしで続行): %s", type(exc).__name__)
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
        llm=voice_llm.create_llm(),
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
    guard = SessionGuard(ctx, session, advisor, max_seconds=config.MAX_SESSION_MINUTES * 60)
    # 本人が抜けてセッションが閉じたら、部屋が閉じるのを待たずにジョブも終える(プロセスと dip MCP の子プロセスを返し、
    # トークへ ended を知らせる)。close は話しかけの途中の発話を止めた後に出るので、ここで終えても失うものは無い。
    # 時間切れで guard が閉じているときは何もしない(guard が部屋を消してからジョブを終える)
    session.on("close", lambda _ev: guard.closing_down or ctx.shutdown(reason="agent session closed"))
    # LiveKit Cloud に録音、文字起こし、ログを残さない(Agent Observability。docs/design/privacy.md の 3.2)。
    # 指定しないとプロジェクトの設定に従う
    await session.start(agent=advisor, room=ctx.room, record=False)
    guard.start()

    # 画面の「面談を終える」ボタン → 下書きを保存して締める(返答は 1 回だけ)
    def _on_data(packet) -> None:
        try:
            payload = json.loads(bytes(packet.data).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        if isinstance(payload, dict) and payload.get("type") == "end":
            guard.on_end()

    ctx.room.on("data_received", _on_data)

    await session.generate_reply(instructions=greeting)


def _job_unit() -> float:
    """ジョブ 1 個の負荷。MAX_JOBS 以上の最小の 2 の累乗の逆数にする。負荷も上限もこの倍数なので、livekit-agents の
    足し算と比べ合いが丸めずに済む(1 / MAX_JOBS だと、6 のとき 1/6 + 5 * (1/6) が 1 より小さくなり、7 件目を受ける)。"""
    return 1 / (1 << (max(config.MAX_JOBS, 1) - 1).bit_length())


def job_load(worker) -> float:
    """ワーカーの負荷: 動いているジョブの数 * _job_unit()。load_threshold と合わせて、MAX_JOBS 個で満員を報告する。"""
    return len(worker.active_jobs) * _job_unit()


def worker_options() -> agents.WorkerOptions:
    """ワーカーの設定。同時に受けるジョブは config.MAX_JOBS まで(dev でも。livekit-agents の既定は、dev では上限なし、
    start では CPU 70%)。上限の負荷は MAX_JOBS * _job_unit()(1 以下。start は 1 を超えると起動で止まる)。
    ジョブのプロセスのメモリが config.JOB_MEMORY_LIMIT_MB を超えたら、そのジョブを止める。

    温めておくプロセスの数も MAX_JOBS にする。livekit-agents は、動いているジョブが 0 個の間、受付中(起動前)の
    ジョブ 1 件の負荷を load_threshold / num_idle_processes と見積もる。dev の既定の 0 のままだと 1 件で満員になり、
    最初のジョブが起動するまでの数秒に来た 2 人目を断ってしまう。MAX_JOBS にすると、見積もりは _job_unit() ちょうどになる。"""
    max_jobs = max(config.MAX_JOBS, 1)
    return agents.WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm, load_fnc=job_load,
                                load_threshold=max_jobs * _job_unit(), num_idle_processes=max_jobs,
                                job_memory_limit_mb=config.JOB_MEMORY_LIMIT_MB)


def configure_logging(argv: list[str]) -> None:
    """ワーカーのログの設定。本番(コンテナが使う start)では、SDK のロガーを WARNING にする(docs/design/privacy.md の 3.4)。
    livekit-agents と Anthropic、OpenAI の SDK は INFO や DEBUG で、送った本文や応答を出しうる。手元の dev と console は
    livekit-agents の既定のまま(SDK の INFO も出る)。

    livekit-agents の CLI は、この後でレベルの無いプラグインのロガーを INFO にし、後から読み込んだプラグイン
    (Deepgram の STT はジョブの中で読む)のロガーも登録のときに INFO にする。なので、登録のときにこちらが先に
    WARNING にする。ジョブのプロセスは、作るときのこのプロセスのロガーのレベルを写す。"""
    logging.basicConfig(level=logging.INFO)
    if argv[:1] != ["start"]:
        return
    from livekit.agents.plugin import Plugin

    quiet_sdk_loggers()
    Plugin.emitter.on("plugin_registered", lambda p: p.logger and p.logger.setLevel(logging.WARNING))


def check_settings(argv: list[str]) -> None:
    """起動のときに呼ぶ。音声相談の設定があるのに AICA_INTERNAL_KEY が無い・短ければ InternalKeyError で止める
    (Web サーバーの lifespan と同じ line_channel.check_internal_key)。鍵が無いと、相談は最後まで進むが、
    このワーカーが送る結果が Web サーバーに 401 で断られる。モデルを取るだけの download-files では確かめない。"""
    if argv[:1] != ["download-files"]:
        line_channel.check_internal_key()


def main(argv: list[str]) -> None:
    configure_logging(argv)
    try:
        check_settings(argv)
    except line_channel.InternalKeyError as err:
        sys.exit(f"音声ワーカーを起動しません: {err}")
    agents.cli.run_app(worker_options())


if __name__ == "__main__":
    main(sys.argv[1:])
