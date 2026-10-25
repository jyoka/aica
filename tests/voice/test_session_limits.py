"""1 回の音声相談の上限(セキュリティ監査 run-1 の 4 番、review-2026-10 の R6)のオフラインテスト。

entrypoint を、偽の STT・LLM・TTS・AgentSession・JobContext・dip と、時計を進められるイベントループで動かす。
実 API(LiveKit、OpenAI、Deepgram、Cartesia)は呼ばない。load_env が読む鍵にはダミー値を入れ、本物の .env は読まない。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import ClassVar

import pytest
from livekit import rtc

from voice_resume import config

DUMMY_KEYS = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "OPENAI_API_KEY",
              "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "AICA_INTERNAL_KEY")
END = SimpleNamespace(data=json.dumps({"type": "end"}).encode("utf-8"))


class FakeClockLoop(asyncio.SelectorEventLoop):
    """loop.time() を advance で進められるループ。asyncio.sleep / wait_for はこの時計で起きる。"""

    def __init__(self) -> None:
        super().__init__()
        self._offset = 0.0

    def time(self) -> float:
        return super().time() + self._offset

    def advance(self, seconds: float) -> None:
        self._offset += seconds


class FakeHandle:
    """SpeechHandle の代役。本物と同じく、コルーチンではなく await できるもの。"""

    def __init__(self, kind: str, instructions: str | None, allow_interruptions=None) -> None:
        self.kind = kind
        self.instructions = instructions
        self.allow_interruptions = allow_interruptions
        self.interrupted = False
        self._done = asyncio.get_running_loop().create_future()

    def finish(self) -> None:
        if not self._done.done():
            self._done.set_result(None)

    def done(self) -> bool:
        return self._done.done()

    def interrupt(self, *, force: bool = False) -> FakeHandle:
        self.interrupted = True
        self.finish()
        return self

    async def wait_for_playout(self) -> None:
        await asyncio.shield(self._done)

    def __await__(self):
        return self.wait_for_playout().__await__()


class FakeSession(rtc.EventEmitter):
    """AgentSession の代役。返答(LLM + TTS)を作るたびに replies に残す。"""

    instances: ClassVar[list[FakeSession]] = []
    finish_replies = True  # False なら返答が終わらない(締めの返答が詰まったとき)

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self.kwargs = kwargs
        self.replies: list[FakeHandle] = []
        self.started = False
        self.closed = False
        FakeSession.instances.append(self)

    async def start(self, agent, room, record=None) -> None:
        self.agent, self.room, self.started = agent, room, True
        self.record = record

    def generate_reply(self, *, instructions=None, allow_interruptions=None, **_kw) -> FakeHandle:
        if self.closed:
            raise RuntimeError("AgentSession isn't running")
        handle = FakeHandle("generate_reply", instructions, allow_interruptions)
        self.replies.append(handle)
        if FakeSession.finish_replies:
            handle.finish()
        return handle

    async def aclose(self) -> None:
        self.closed = True
        self.emit("close", SimpleNamespace(reason="user_initiated"))


class FakeRoom(rtc.EventEmitter):
    """rtc.Room の代役。emit は本物と同じ rtc.EventEmitter(コールバックの TypeError は投げ直す)。"""

    name = "voice-resume-test"

    def __init__(self) -> None:
        super().__init__()
        self.published: list[dict] = []

        async def publish_data(data, **_kw):
            self.published.append(json.loads(data))

        self.local_participant = SimpleNamespace(publish_data=publish_data)


class FakeJobContext:
    """JobContext の代役。shutdown と delete_room を、呼ばれた順にも記録する。"""

    def __init__(self) -> None:
        self.room = FakeRoom()
        self.proc = SimpleNamespace(userdata={"vad": object()})
        self.shutdown_callbacks: list = []
        self.shutdown_reasons: list[str] = []
        self.deleted_rooms = 0
        self.events: list[str] = []

    async def connect(self) -> None:
        pass

    async def wait_for_participant(self):
        return SimpleNamespace(metadata=json.dumps({"user_id": "w-0000000000000000"}))

    def add_shutdown_callback(self, cb) -> None:
        self.shutdown_callbacks.append(cb)

    def shutdown(self, reason: str = "user requested") -> None:
        self.shutdown_reasons.append(reason)
        self.events.append("shutdown")

    def delete_room(self, room_name: str | None = None):
        self.deleted_rooms += 1
        self.events.append("delete_room")
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        return fut


class FakeDip:
    async def start(self) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def get_user_conversation_summary(self, user_id: str) -> dict:
        return {}

    async def get_counseling_playbook(self, track: str, channel: str) -> dict:
        return {"playbook": "テスト用の進め方"}


@pytest.fixture
def agent_mod(tmp_path, monkeypatch):
    """偽の部品を差し込んだ voice_resume.agent。鍵はダミー、.env と data/ は tmp_path を指す。"""
    for k in DUMMY_KEYS:
        monkeypatch.setenv(k, "offline-test")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")  # 本物の .env を読まない
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "voice-resume")
    from voice_resume import agent

    FakeSession.instances = []
    FakeSession.finish_replies = True
    monkeypatch.setattr(agent, "DipMCP", FakeDip)
    monkeypatch.setattr(agent, "AgentSession", FakeSession)
    monkeypatch.setattr(agent, "create_stt", lambda: SimpleNamespace(fake="stt"))
    monkeypatch.setattr(agent, "openai", SimpleNamespace(LLM=lambda **kw: SimpleNamespace(fake="llm", **kw)))
    monkeypatch.setattr(agent, "cartesia", SimpleNamespace(TTS=lambda **kw: SimpleNamespace(fake="tts", **kw)))
    monkeypatch.setattr(agent, "MultilingualModel", lambda: SimpleNamespace(fake="turn-detector"))
    monkeypatch.setattr(agent, "_post_json", talk_reports(agent))  # トークへの知らせは送らずに残す
    return agent


def talk_reports(agent):
    """トーク(line_runtime)への部屋の始まりと終わりの知らせ(_post_json)の代役。知らせを agent.reported に残し、許可する。"""
    agent.reported = []

    async def post(url, payload):
        agent.reported.append(payload["state"])
        return {"allowed": True}

    return post


async def run_shutdown_callbacks(ctx: FakeJobContext) -> None:
    """ctx.shutdown の後に livekit-agents がすること(登録された shutdown コールバックを呼ぶ)。"""
    await asyncio.gather(*(cb() for cb in ctx.shutdown_callbacks))


def run_on_fake_clock(scenario) -> None:
    loop = FakeClockLoop()
    try:
        loop.run_until_complete(scenario(loop))
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def start_session(agent_mod) -> tuple[FakeJobContext, FakeSession]:
    ctx = FakeJobContext()
    await agent_mod.entrypoint(ctx)
    await settle()  # 上限のタイマーが時計を読むまで進める
    (session,) = FakeSession.instances
    return ctx, session


def press_end(ctx: FakeJobContext, times: int) -> int:
    """「面談を終える」のデータパケットを times 回届け、emit が投げ直した TypeError の数を返す。"""
    errors = 0
    for _ in range(times):
        try:
            ctx.room.emit("data_received", END)
        except TypeError:
            errors += 1
    return errors


# ----------------------------- 相談の長さの上限 -----------------------------
# 直す前は、偽の時計を 60 分進めてもセッションが閉じなかった(このファイルの最初の版で再現した)。


def end_replies(session: FakeSession) -> list[FakeHandle]:
    return [h for h in session.replies if "面談を終える" in (h.instructions or "")]


def time_up_replies(session: FakeSession) -> list[FakeHandle]:
    return [h for h in session.replies if "時間の上限" in (h.instructions or "")]


def test_session_keeps_nothing_on_livekit_and_openai(agent_mod):
    """LiveKit Cloud に録音・文字起こし・ログを残さず(record=False)、会話の LLM も OpenAI に応答を残さない(store=False)。
    record を渡さないと、LiveKit Cloud のプロジェクトの設定に従う(docs/design/privacy.md の 3.2)。"""

    async def scenario(loop):
        _ctx, session = await start_session(agent_mod)
        assert session.started and session.record is False
        assert session.kwargs["llm"].store is False

    run_on_fake_clock(scenario)


def test_session_closes_at_the_time_limit(agent_mod):
    """上限(既定 10 分)の手前では閉じず、上限で締めの返答を 1 回作り、セッションを閉じて部屋を消す。"""
    assert config.MAX_SESSION_MINUTES == 10

    async def scenario(loop):
        ctx, session = await start_session(agent_mod)
        assert session.started and session.kwargs["stt"].fake == "stt"
        assert session.kwargs["llm"].fake == "llm" and session.kwargs["tts"].fake == "tts"
        assert len(session.replies) == 1  # 最初の挨拶
        loop.advance(10 * 60 - 1)
        await settle()
        assert not session.closed and ctx.shutdown_reasons == []

        loop.advance(1)
        await settle()
        (reply,) = time_up_replies(session)
        assert "10分" in reply.instructions and "finalize_interview" in reply.instructions
        assert reply.allow_interruptions is False  # 締めの返答は本人の発話で割り込ませない
        assert session.closed
        assert ctx.deleted_rooms == 1 and ctx.shutdown_reasons == ["voice session time limit"]

        loop.advance(60 * 60)  # 閉じた後は何も起きない
        await settle()
        assert len(session.replies) == 2 and ctx.shutdown_reasons == ["voice session time limit"]

    run_on_fake_clock(scenario)


def test_time_limit_deletes_the_room_before_the_job_ends_and_reports_ended(agent_mod):
    """時間切れで閉じるとき、セッションの close でジョブを先に終えない(task 202 の close のハンドラは、締めている間は
    何もしない)。部屋を消してからジョブを終え、ジョブの終わりにトークへ ended を知らせる(その人は次の部屋を開ける)。"""

    async def scenario(loop):
        ctx, session = await start_session(agent_mod)
        assert agent_mod.reported == ["joined"]
        loop.advance(10 * 60)
        await settle()
        assert session.closed
        assert ctx.events == ["delete_room", "shutdown"]
        assert ctx.shutdown_reasons == ["voice session time limit"]

        await run_shutdown_callbacks(ctx)
        assert agent_mod.reported == ["joined", "ended"]

    run_on_fake_clock(scenario)


def test_time_limit_follows_the_setting(agent_mod, monkeypatch):
    """上限の長さは VOICE_RESUME_MAX_SESSION_MINUTES(config.MAX_SESSION_MINUTES)で変えられる。"""
    monkeypatch.setattr(config, "MAX_SESSION_MINUTES", 3)

    async def scenario(loop):
        ctx, session = await start_session(agent_mod)
        loop.advance(3 * 60)
        await settle()
        assert "3分" in time_up_replies(session)[0].instructions
        assert session.closed and ctx.deleted_rooms == 1

    run_on_fake_clock(scenario)


def test_stuck_closing_reply_is_cut_and_the_session_still_closes(agent_mod):
    """締めの返答が終わらなくても(LLM が詰まる)、待つのは CLOSING_REPLY_TIMEOUT までで、止めて閉じる。"""
    FakeSession.finish_replies = False

    async def scenario(loop):
        ctx = FakeJobContext()
        entry = asyncio.ensure_future(agent_mod.entrypoint(ctx))
        await settle()
        (session,) = FakeSession.instances
        session.replies[0].finish()  # 挨拶だけは終わる
        await entry
        await settle()

        loop.advance(10 * 60)
        await settle()
        (reply,) = time_up_replies(session)
        assert not session.closed and not reply.interrupted  # 締めの返答を待っている

        loop.advance(agent_mod.CLOSING_REPLY_TIMEOUT)
        await settle()
        assert reply.interrupted and session.closed and ctx.shutdown_reasons == ["voice session time limit"]

    run_on_fake_clock(scenario)


def test_session_closed_by_the_user_stops_the_timer(agent_mod):
    """本人が切って session が先に閉じたら、上限の時刻になっても返答を作らず、部屋も消さない。
    ジョブはすぐに終える(task 202: プロセスと dip MCP の子プロセスを返し、トークへ ended を知らせる)。"""

    async def scenario(loop):
        ctx, session = await start_session(agent_mod)
        await session.aclose()
        assert ctx.shutdown_reasons == ["agent session closed"]
        loop.advance(60 * 60)
        await settle()
        assert time_up_replies(session) == []
        assert ctx.deleted_rooms == 0 and ctx.shutdown_reasons == ["agent session closed"]

    run_on_fake_clock(scenario)


# ----------------------------- 締めの返答は 1 回だけ -----------------------------
# 直す前は、end パケット 20 回で締めの返答が 20 回出た(このファイルの最初の版で再現した)。
# あわせて、本物の generate_reply は SpeechHandle を返すので asyncio.create_task に渡すと毎回 TypeError になっていた。


def test_end_packet_20_times_gives_one_reply(agent_mod):
    async def scenario(loop):
        ctx, session = await start_session(agent_mod)
        assert press_end(ctx, 20) == 0  # TypeError も出ない
        await settle()
        assert len(end_replies(session)) == 1
        assert len(session.replies) == 2  # 挨拶 + 締め
        session.agent.finalized = {"files": []}  # 締めの返答で finalize_interview を呼んだ(LLM は偽物なので代わりに置く)

        # 上限の時刻には、下書きを保存済みなので、もう締めの返答を作らずに閉じる
        loop.advance(10 * 60)
        await settle()
        assert time_up_replies(session) == [] and len(session.replies) == 2
        assert session.closed and ctx.shutdown_reasons == ["voice session time limit"]

    run_on_fake_clock(scenario)


def test_unsaved_end_reply_is_followed_by_the_time_up_reply(agent_mod):
    """「面談を終える」の返答が下書きを保存せずに終わったら(割り込まれた、LLM がツールを呼ばなかった)、
    end パケットをまた送っても返答は作らないが、上限の時刻に時間切れの締めの返答を作って下書きを残させる。"""

    async def scenario(loop):
        ctx, session = await start_session(agent_mod)
        press_end(ctx, 1)
        await settle()
        assert session.agent.finalized is None
        press_end(ctx, 5)
        await settle()
        assert len(end_replies(session)) == 1

        loop.advance(10 * 60)
        await settle()
        assert len(time_up_replies(session)) == 1 and session.closed

    run_on_fake_clock(scenario)


def test_time_limit_waits_for_the_end_reply_in_progress(agent_mod):
    """上限の時刻に「面談を終える」の返答が途中なら、それを待ち、保存できていれば時間切れの返答は作らない。"""
    FakeSession.finish_replies = False

    async def scenario(loop):
        ctx = FakeJobContext()
        entry = asyncio.ensure_future(agent_mod.entrypoint(ctx))
        await settle()
        (session,) = FakeSession.instances
        session.replies[0].finish()
        await entry
        await settle()

        press_end(ctx, 1)
        loop.advance(10 * 60)
        await settle()
        (end_reply,) = end_replies(session)
        assert not session.closed and time_up_replies(session) == []

        session.agent.finalized = {"files": []}
        end_reply.finish()
        await settle()
        assert time_up_replies(session) == [] and session.closed and not end_reply.interrupted

    run_on_fake_clock(scenario)


def test_end_packet_after_the_time_limit_reply_gives_no_reply(agent_mod):
    """時間切れの締めの返答を作った後の end パケットにも、返答を作らない。"""
    FakeSession.finish_replies = False

    async def scenario(loop):
        ctx = FakeJobContext()
        entry = asyncio.ensure_future(agent_mod.entrypoint(ctx))
        await settle()
        (session,) = FakeSession.instances
        session.replies[0].finish()
        await entry
        await settle()

        loop.advance(10 * 60)
        await settle()
        press_end(ctx, 5)
        await settle()
        assert end_replies(session) == [] and len(time_up_replies(session)) == 1

    run_on_fake_clock(scenario)


def test_other_data_packets_are_ignored(agent_mod):
    """end 以外のパケット(壊れた JSON、オブジェクトでない JSON)では返答を作らず、例外も出さない。"""

    async def scenario(loop):
        ctx, session = await start_session(agent_mod)
        for data in (b"not json", b"[1, 2]", b'"end"', json.dumps({"type": "other"}).encode()):
            ctx.room.emit("data_received", SimpleNamespace(data=data))
        await settle()
        assert len(session.replies) == 1

    run_on_fake_clock(scenario)
