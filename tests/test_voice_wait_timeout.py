"""POST /api/voice/wait の timeout に NaN を送っても、イベントループのタイマーに NaN を渡さない(task 196)。

Starlette の Request.json() は json.loads なので、本文の `NaN` / `Infinity` / `-Infinity` を受け付ける。
NaN は max/min の丸めをすり抜けて asyncio.wait_for(→ loop.call_at)に届き、プロセスで共有するタイマーの山の順序を壊しうる。
NaN は 422 で断り、ほかの値(Infinity・-Infinity・負・ふつうの値)はこれまでどおり 0〜VOICE_WAIT_MAX に丸める。
実 API は呼ばない(鍵はダミー、state と data は tmp)。
"""

from __future__ import annotations

import asyncio
import math

import pytest

from dip_mcp.data import State

DUMMY_ENV = {
    "OPENAI_API_KEY": "offline-test",
    "ANTHROPIC_API_KEY": "offline-test",
    "LINE_CHANNEL_SECRET": "offline-test",
    "LINE_CHANNEL_ACCESS_TOKEN": "offline-test",
    "AICA_ID_KEY": "offline-test-id-key-offline-test-id-key",
    "AICA_INTERNAL_KEY": "offline-test-internal-key",
    "LIVEKIT_URL": "wss://example.livekit.cloud",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret",
}


class _WaitForSpy:
    """server モジュールの asyncio の代わり。wait_for に渡った timeout を記録する(ほかはそのまま asyncio)。
    実際のループには有限の値(最大 0.05 秒)だけを渡し、直す前の NaN でテストのループを壊さない。"""

    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, aw, timeout):
        self.timeouts.append(timeout)
        safe = timeout if math.isfinite(timeout) else 0.0
        return await asyncio.wait_for(aw, min(safe, 0.05))


@pytest.fixture
def client(tmp_path, monkeypatch):
    for k, v in DUMMY_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("LINE_SIM_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from line_runtime import config, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()
    with TestClient(server.app) as c:
        c.get("/")  # 画面を開いた(匿名IDの Cookie が発行される)
        yield c


@pytest.fixture
def spy(monkeypatch):
    from line_runtime import server

    s = _WaitForSpy()
    monkeypatch.setattr(server, "asyncio", s)
    return s


def _wait_raw(client, timeout_literal: str):
    """ブラウザ以外の呼び出しと同じく、生の JSON 本文で送る(NaN などは json= では作れない)。"""
    uid = client.get("/api/me").json()["user_id"]
    body = f'{{"user_id":"{uid}","since":0,"timeout":{timeout_literal}}}'
    return client.post("/api/voice/wait", content=body, headers={"Content-Type": "application/json"})


def test_nan_timeout_is_rejected_and_never_reaches_the_event_loop(client, spy):
    r = _wait_raw(client, "NaN")
    # 直す前は (200, [nan]): 丸めをすり抜けた NaN が wait_for(→ loop.call_at)に届いていた
    assert (r.status_code, spy.timeouts) == (422, [])
    assert r.json() == {"detail": "timeout は数値で指定してください"}  # 応答は JSON にできる(入力の nan を返さない)


@pytest.mark.parametrize(("literal", "expected"), [
    ("Infinity", 30.0),  # 上限に丸める
    ("-Infinity", 0.0),
    ("-5", 0.0),
    ("25", 25.0),
    ("100", 30.0),
])
def test_other_timeouts_are_clamped_as_before(client, spy, literal, expected):
    r = _wait_raw(client, literal)
    assert r.status_code == 200 and r.json() == {"results": [], "next": 0}
    assert spy.timeouts == [expected]


def test_default_timeout_is_unchanged(client, spy):
    uid = client.get("/api/me").json()["user_id"]
    r = client.post("/api/voice/wait", json={"user_id": uid, "since": 0})
    assert r.status_code == 200 and spy.timeouts == [25.0]
