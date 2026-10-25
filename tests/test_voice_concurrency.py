"""音声セッションの同時数の上限のテスト(task 202、セキュリティ監査 run-1 の要確認 5 番)。実 API は呼ばない。

1 人が音声の部屋を同時にいくつも開けると、部屋ごとに worker のジョブ(プロセス)と dip MCP の子プロセスが 1 つずつでき、
同じマシンでほかの利用者の音声や Web の処理を圧迫しうる。
- Web: POST /api/voice/start は、その人(quota.person と同じ単位)に、まだ終わっていない部屋があれば 409 で断る。
- worker: 同時に受けるジョブの数に上限がある(dev で起動しても満員を報告する)。
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from dip_mcp.data import State

LIVEKIT = {"LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k",
           "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret"}
INTERNAL_KEY = "offline-test-internal-key-offline-test"
# load_env が .env から読む鍵。先にダミー値を入れておけば、.env があっても本物の値は入らない(setdefault)
DUMMY_KEYS = {**LIVEKIT, "OPENAI_API_KEY": "offline-test", "ANTHROPIC_API_KEY": "offline-test",
              "DEEPGRAM_API_KEY": "offline-test", "CARTESIA_API_KEY": "offline-test",
              "AICA_INTERNAL_KEY": INTERNAL_KEY, "AICA_ID_KEY": "offline-test-id-key-offline-test-id-key"}


@pytest.fixture
def offline_env(tmp_path, monkeypatch):
    for k, v in DUMMY_KEYS.items():
        monkeypatch.setenv(k, v)
    for k in ("LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    from line_runtime import config as line_config
    from voice_resume import config as voice_config

    monkeypatch.setattr(line_config, "REPO_ENV", tmp_path / "no.env")  # 本物の .env を読まない
    monkeypatch.setattr(voice_config, "ENV_FILE", tmp_path / "no.env")


@pytest.fixture
def client(tmp_path, monkeypatch, offline_env):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    from fastapi.testclient import TestClient

    from line_runtime import config, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)  # 本物の data/ を使わない
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()
    with TestClient(server.app) as c:
        c.get("/")  # 画面を開いた(匿名IDの Cookie が発行される)
        yield c


WORKER = {"Authorization": f"Bearer {INTERNAL_KEY}"}  # 音声ワーカー(サーバー間の経路)の共有鍵


def _start(client, user):
    return client.post("/api/voice/start", json={"user_id": user})


def _worker(client, path, body):
    """音声ワーカーと同じく、Cookie を使わずに共有鍵を付けて送る。"""
    from fastapi.testclient import TestClient

    return TestClient(client.app).post(path, json=body, headers=WORKER)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(client):
    c = Clock()
    client.app.state.voice_rooms.clock = c
    return c


# ----------------------------- Web: 1 人が開ける部屋は同時に 1 つまで -----------------------------


def test_second_start_is_refused_while_the_first_room_is_open(client):
    """同じ Cookie から 3 回続けて始めると、1 回目だけ 200。2 回目以降は会話数を使わずに 409
    (直す前は 3 回とも 200 で、別々の部屋が返っていた)。同じ Cookie のシナリオユーザーも同じ人として断る。"""
    from fastapi.testclient import TestClient

    from line_runtime.server import VOICE_BUSY_MESSAGE

    uid = client.get("/api/me").json()["user_id"]
    rs = [_start(client, uid) for _ in range(3)]
    assert [r.status_code for r in rs] == [200, 409, 409]
    assert rs[1].json()["detail"] == VOICE_BUSY_MESSAGE
    assert client.app.state.quota.used(uid) == 1
    scoped = client.post("/api/scenarios/F-follow-new/start").json()["user"]  # 「匿名ID:シナリオユーザー」
    assert _start(client, scoped).status_code == 409

    other = TestClient(client.app)  # 別のブラウザ(別の人)は断られない
    other.get("/")
    assert _start(other, other.get("/api/me").json()["user_id"]).status_code == 200


def test_a_finished_room_lets_the_person_start_again(client):
    """前の部屋が終われば(ワーカーがジョブの終わりを知らせたら)、また始められる。
    結果が届いただけでは終わりにしない: 本人は「面談を終える」で結果を送らせた後も、接続したままジョブを持ち続けられる
    (「すべて消す」の後で結果を 410 で断ったときも同じ)。"""
    uid = client.get("/api/me").json()["user_id"]
    room = _start(client, uid).json()["room"]
    assert _worker(client, "/api/voice/session", {"user_id": uid, "room": room, "state": "joined"}).json() == {
        "allowed": True}
    assert _worker(client, "/api/voice/result", {"user_id": uid, "draft_markdown": "# 下書き", "room": room}).status_code == 200
    assert _start(client, uid).status_code == 409
    assert client.post("/api/profile/forget").json() == {"forgotten": True}
    r = _worker(client, "/api/voice/result", {"user_id": uid, "draft_markdown": "# 下書き", "room": room})
    assert r.status_code == 410
    assert _start(client, uid).status_code == 409

    assert _worker(client, "/api/voice/session", {"user_id": uid, "room": room, "state": "ended"}).json() == {
        "allowed": True}
    assert _start(client, uid).status_code == 200


def test_rejoining_an_old_room_while_another_is_open_is_refused(client):
    """前に発行した接続トークン(期限は 5 分)で、終わった部屋に入り直すと、LiveKit は部屋を作り直して新しいジョブを始める。
    その人に別の部屋があれば、joined に allowed: false を返して記録しない(ワーカーはそのジョブを止める)。"""
    uid = client.get("/api/me").json()["user_id"]
    old = _start(client, uid).json()["room"]
    _worker(client, "/api/voice/session", {"user_id": uid, "room": old, "state": "joined"})
    _worker(client, "/api/voice/session", {"user_id": uid, "room": old, "state": "ended"})
    new = _start(client, uid).json()["room"]
    assert _worker(client, "/api/voice/session", {"user_id": uid, "room": old, "state": "joined"}).json() == {
        "allowed": False}
    assert client.app.state.voice_rooms.open_room(uid) == new
    assert _worker(client, "/api/voice/session", {"user_id": uid, "room": new, "state": "joined"}).json() == {
        "allowed": True}  # 自分の部屋に入るのは通る(入り直しも)
    assert _worker(client, "/api/voice/session", {"user_id": uid, "room": new, "state": "joined"}).json() == {
        "allowed": True}


def test_an_unused_room_ends_when_its_token_can_no_longer_join(client, clock):
    """だれも入らなかった部屋は、接続トークンの期限(+余裕)が過ぎたら終わったとみなす(もう入れない)。
    入った部屋は、終わりの知らせが無くても HOLD_MAX が過ぎたら終わったとみなす(ジョブがメモリの上限で止められたなど)。"""
    from line_runtime.voice_rooms import HOLD_MAX, JOIN_WINDOW
    from voice_resume import agent, config
    from voice_resume.livekit_token import TOKEN_TTL

    assert JOIN_WINDOW > TOKEN_TTL and HOLD_MAX > JOIN_WINDOW
    # ワーカーが相談を締めて閉じるまで(時間の上限 + 締めの返答を 2 回待つ)より長い。短いと相談の途中で 2 つ目を開ける
    assert HOLD_MAX.total_seconds() > config.MAX_SESSION_MINUTES * 60 + 2 * agent.CLOSING_REPLY_TIMEOUT
    uid = client.get("/api/me").json()["user_id"]
    assert _start(client, uid).status_code == 200
    clock.now += JOIN_WINDOW.total_seconds() - 1
    assert _start(client, uid).status_code == 409
    clock.now += 2
    room = _start(client, uid).json()["room"]

    _worker(client, "/api/voice/session", {"user_id": uid, "room": room, "state": "joined"})
    clock.now += JOIN_WINDOW.total_seconds() + 1  # 入った部屋は、トークンの期限が過ぎても続いている
    assert _start(client, uid).status_code == 409
    clock.now += HOLD_MAX.total_seconds()
    assert _start(client, uid).status_code == 200


def test_expired_rooms_of_people_who_never_come_back_are_dropped(client, clock):
    """二度と来ない人の、期限の過ぎた部屋の記録はメモリに残り続けない(だれかが部屋を発行したときに捨てる)。"""
    from fastapi.testclient import TestClient

    from line_runtime.voice_rooms import JOIN_WINDOW

    uid = client.get("/api/me").json()["user_id"]
    assert _start(client, uid).status_code == 200
    clock.now += JOIN_WINDOW.total_seconds() + 1
    other = TestClient(client.app)
    other.get("/")
    ub = other.get("/api/me").json()["user_id"]
    assert _start(other, ub).status_code == 200
    assert list(client.app.state.voice_rooms._rooms) == [ub]


def test_a_joined_report_after_a_restart_restores_the_limit(client):
    """Web プロセスを再起動すると記録は消えるが、ワーカーが入ったと知らせた部屋は記録し直し、次の部屋は断る。"""
    uid = client.get("/api/me").json()["user_id"]
    _worker(client, "/api/voice/session", {"user_id": uid, "room": "voice-resume-before", "state": "joined"})
    assert _start(client, uid).status_code == 409


def test_the_session_route_needs_the_shared_key(client):
    """部屋の始まり・終わりの知らせはサーバー間の経路: 共有鍵が無ければ 401(本人が自分の部屋を終わったことにできない)。"""
    uid = client.get("/api/me").json()["user_id"]
    room = _start(client, uid).json()["room"]
    body = {"user_id": uid, "room": room, "state": "ended"}
    assert client.post("/api/voice/session", json=body).status_code == 401
    assert client.post("/api/voice/session", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert _start(client, uid).status_code == 409


# ----------------------------- worker: 部屋の始まりと終わりを知らせる -----------------------------


class FakeRoom:
    def __init__(self, name):
        self.name = name

    def on(self, *_args):
        pass


class FakeCtx:
    """agents.JobContext の、entrypoint が使うところだけ。"""

    def __init__(self, user_id, room):
        self.room = FakeRoom(room)
        self.proc = type("Proc", (), {"userdata": {"vad": None}})()
        self.shutdown_callbacks = []
        self.shutdowns = []
        self._participant = type("P", (), {"metadata": f'{{"user_id": "{user_id}"}}'})()

    async def connect(self):
        pass

    async def wait_for_participant(self):
        return self._participant

    def add_shutdown_callback(self, cb):
        self.shutdown_callbacks.append(cb)

    def shutdown(self, reason=""):
        self.shutdowns.append(reason)


class FakeSession:
    started: ClassVar[list] = []

    def __init__(self, **_kwargs):
        self.handlers = {}

    def on(self, event, cb):
        self.handlers.setdefault(event, []).append(cb)

    def emit(self, event, ev=None):
        for cb in self.handlers.get(event, []):
            cb(ev)

    async def start(self, **_kwargs):
        FakeSession.started.append(self)

    async def generate_reply(self, **_kwargs):
        pass


@pytest.fixture
def fake_worker(client, monkeypatch):
    """entrypoint を実 API なしで動かす。トークへの知らせ(_post_json)は、テストのサーバーへ共有鍵つきで同じ形で送る。"""
    from voice_resume import agent

    sent = []

    async def poster(url, payload):
        assert url.endswith("/api/voice/session")
        sent.append((payload["room"], payload["state"]))
        r = _worker(client, "/api/voice/session", payload)
        assert r.status_code == 200, r.text
        return r.json()

    async def no_advisor(dip, user_id, room=None):
        return object(), "挨拶"

    class NoDip:
        async def start(self):
            raise OSError("テストでは dip MCP の子プロセスを起動しない")

    monkeypatch.setattr(agent, "_post_json", poster)
    monkeypatch.setattr(agent, "prepare_advisor", no_advisor)
    monkeypatch.setattr(agent, "DipMCP", NoDip)
    monkeypatch.setattr(agent, "AgentSession", FakeSession)
    monkeypatch.setattr(FakeSession, "started", [])
    for name in ("create_stt", "MultilingualModel"):
        monkeypatch.setattr(agent, name, lambda *a, **k: None)
    monkeypatch.setattr(agent.openai, "LLM", lambda *a, **k: None)
    monkeypatch.setattr(agent.cartesia, "TTS", lambda *a, **k: None)
    return agent, sent


def test_the_worker_reports_the_room_so_the_person_cannot_open_another(client, clock, fake_worker):
    """ワーカーのジョブは、本人が入ったら joined、ジョブを終えたら ended をトークへ知らせる(共有鍵つき)。
    その間、その人は次の部屋を開けない。セッションが閉じたら(本人が抜けたら)ジョブも終える。実 API は呼ばない。"""
    import asyncio

    from line_runtime.voice_rooms import JOIN_WINDOW

    agent, sent = fake_worker
    uid = client.get("/api/me").json()["user_id"]
    room = _start(client, uid).json()["room"]
    ctx = FakeCtx(uid, room)
    asyncio.run(agent.entrypoint(ctx))
    assert sent == [(room, "joined")] and len(FakeSession.started) == 1
    clock.now += JOIN_WINDOW.total_seconds() + 1  # トークンの期限が過ぎても、入った部屋は終わっていない
    assert _start(client, uid).status_code == 409

    FakeSession.started[0].emit("close")  # 本人が抜けてセッションが閉じた
    assert ctx.shutdowns == ["agent session closed"]

    async def shutdown():
        for cb in ctx.shutdown_callbacks:
            await cb()

    asyncio.run(shutdown())
    assert sent == [(room, "joined"), (room, "ended")]
    assert _start(client, uid).status_code == 200


def test_the_worker_stops_a_job_for_a_second_room_of_the_same_person(client, fake_worker):
    """古い接続トークンで終わった部屋に入り直しても、その人に別の部屋があれば、ジョブは相談を始めずに止まる。"""
    import asyncio

    agent, sent = fake_worker
    uid = client.get("/api/me").json()["user_id"]
    _start(client, uid)
    ctx = FakeCtx(uid, "voice-resume-old")
    asyncio.run(agent.entrypoint(ctx))
    assert sent == [("voice-resume-old", "joined")]
    assert ctx.shutdowns and FakeSession.started == [] and ctx.shutdown_callbacks == []


def test_report_session_does_not_stop_the_interview_when_the_talk_is_down(offline_env):
    """トークへ知らせられなくても、面談は続ける(トークは時間で部屋を終わったとみなす)。"""
    import asyncio

    from voice_resume.agent import report_session

    async def down(url, payload):
        raise OSError("connection refused")

    assert asyncio.run(report_session("voice-resume-x", "w-0000000000000001", "joined", poster=down)) is True


# ----------------------------- worker: 同時に受けるジョブの数の上限 -----------------------------


def _captured_worker_options(monkeypatch):
    """python -m voice_resume.agent と同じく __main__ として動かし、run_app に渡る WorkerOptions を受け取る。"""
    import runpy
    import sys

    from livekit.agents import cli

    got = []
    monkeypatch.setattr(cli, "run_app", got.append)
    monkeypatch.delitem(sys.modules, "voice_resume.agent", raising=False)  # 読み込み済みのモジュールとは別に動かす
    runpy.run_module("voice_resume.agent", run_name="__main__")
    (opts,) = got
    return opts


# 既定の 4 と、1 / MAX_JOBS の足し算が丸めで境界を越える値(6, 7 など)。2 の累乗(1, 2, 8)も含める
MAX_JOBS_CASES = [1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 49]


@pytest.mark.parametrize("max_jobs", MAX_JOBS_CASES)
def test_the_worker_caps_concurrent_jobs_even_in_dev(offline_env, monkeypatch, max_jobs):
    """dev で起動しても、同時に受けるジョブは MAX_JOBS まで(直す前は dev の load_threshold が無限大で、満員を報告しなかった)。
    ジョブのプロセスのメモリにも上限がある(直す前は 0 = 上限なし)。"""
    import math

    from livekit.agents.worker import ServerEnvOption

    from voice_resume import config

    monkeypatch.setattr(config, "MAX_JOBS", max_jobs)
    opts = _captured_worker_options(monkeypatch)
    for devmode in (True, False):
        threshold = ServerEnvOption.getvalue(opts.load_threshold, devmode)
        assert not math.isinf(threshold) and threshold <= 1  # start(本番)は 1 以下でないと起動で止まる

    def load(n):
        return opts.load_fnc(type("W", (), {"active_jobs": [object()] * n})())

    assert 0 < config.MAX_JOBS
    assert load(config.MAX_JOBS - 1) < threshold <= load(config.MAX_JOBS)  # MAX_JOBS 個目で満員
    assert opts.job_memory_limit_mb > 0


@pytest.mark.parametrize("max_jobs", MAX_JOBS_CASES)
@pytest.mark.parametrize("devmode", [True, False])
def test_the_worker_accepts_up_to_max_jobs_requests_at_once(offline_env, monkeypatch, devmode, max_jobs):
    """ジョブがまだ起動していない間(受付中)の依頼も数えて、MAX_JOBS 件までは受け、それを超えたら断る。
    直す前の版は、dev ではジョブ 0 個 + 受付中 1 件で満員になり、ほぼ同時に来た 2 人目を断っていた。
    負荷を 1 / MAX_JOBS で数えていた版は、MAX_JOBS が 6 などのとき、丸めで MAX_JOBS + 1 件目を受けていた。"""
    from livekit.agents.worker import AgentServer

    from voice_resume import config

    monkeypatch.setattr(config, "MAX_JOBS", max_jobs)
    opts = _captured_worker_options(monkeypatch)
    jobs = []
    monkeypatch.setattr(AgentServer, "active_jobs", property(lambda self: jobs))
    server = AgentServer.from_server_options(opts)
    server._devmode = devmode

    def available(running, reserved):
        jobs[:] = [object()] * running
        server._worker_load = opts.load_fnc(server)
        server._reserved_slots = reserved
        return server._is_available()

    m = config.MAX_JOBS
    for running in range(m):
        for reserved in range(m - running):
            assert available(running, reserved), (running, reserved)  # 合わせて MAX_JOBS 件目までは受ける
        assert not available(running, m - running), running  # MAX_JOBS 件を超える依頼は断る
    assert not available(m, 0)
