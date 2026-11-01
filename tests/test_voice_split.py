"""音声ワーカーを Web サービスと別のサービスで動かす(review-2026-10.md の R9)。

ワーカーは事業者の状態を持つ Web のコンテナの事業者の MCP サーバーに HTTP でつなぎ(PROVIDER_MCP_URL)、外に開いた口は共有鍵
(AICA_INTERNAL_KEY)で守る。本物の provider_mcp.server を別プロセスで起動して確かめる。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from voice_resume import config as voice_config
from voice_resume.provider import ProviderMCP

ROOT = Path(__file__).resolve().parents[1]
KEY = "k" * 43
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _provider_http(tmp_path, host: str, key: str | None):
    """provider_mcp.server を HTTP で起動する。(プロセス, ポート)。開くまで待つ(10 秒で諦める)。"""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k != "AICA_INTERNAL_KEY"}
    env |= {"PROVIDER_STATE_PATH": str(tmp_path / "state.json"), **({"AICA_INTERNAL_KEY": key} if key else {})}
    proc = subprocess.Popen([sys.executable, "-m", "provider_mcp.server", "--http", str(port), "--host", host],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return proc, port
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError(f"事業者が開きませんでした: {proc.communicate(timeout=5)[1]}")


@pytest.fixture
def provider_server(tmp_path):
    proc, port = _provider_http(tmp_path, "0.0.0.0", KEY)
    yield port
    proc.terminate()
    proc.wait(timeout=10)


def test_the_provider_opened_beyond_localhost_answers_only_with_the_shared_key(provider_server):
    url = f"http://127.0.0.1:{provider_server}/mcp"
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": KEY}):
        assert httpx.post(url, json=INIT, headers=MCP_HEADERS | headers, timeout=10).status_code == 401
    r = httpx.post(url, json=INIT, headers=MCP_HEADERS | {"Authorization": f"Bearer {KEY}"}, timeout=10)
    assert r.status_code == 200 and "jobs-mcp" in r.text


def test_the_provider_does_not_open_beyond_localhost_without_a_key(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "AICA_INTERNAL_KEY"} | {"PROVIDER_STATE_PATH": str(tmp_path / "state.json")}
    done = subprocess.run([sys.executable, "-m", "provider_mcp.server", "--http", str(_free_port()), "--host", "0.0.0.0"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode != 0 and "AICA_INTERNAL_KEY" in done.stderr


def test_the_provider_on_localhost_still_needs_no_key(tmp_path):
    """手元で PROVIDER_MCP_URL を使うときの今までの形(127.0.0.1、鍵なし)は変えない。"""
    proc, port = _provider_http(tmp_path, "127.0.0.1", None)
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/mcp", json=INIT, headers=MCP_HEADERS, timeout=10)
        assert r.status_code == 200
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_the_worker_reads_and_writes_the_state_of_the_provider_it_is_pointed_at(provider_server, tmp_path, monkeypatch):
    """PROVIDER_MCP_URL があると、ワーカーは子プロセスを作らず、その事業者(別サービスの Web のコンテナ)の状態を読み書きする。"""
    monkeypatch.setattr(voice_config, "PROVIDER_MCP_URL", f"http://127.0.0.1:{provider_server}/mcp")
    monkeypatch.setenv("AICA_INTERNAL_KEY", KEY)
    monkeypatch.setenv("PROVIDER_STATE_PATH", str(tmp_path / "elsewhere.json"))  # 子プロセスの事業者ならこちらに書く
    uid = "w-6666666666666666"

    async def run():
        provider = ProviderMCP()
        await provider.start()
        try:
            saved = await provider.save_user_preferences(uid, {"area": "渋谷"})
            return saved, await provider.get_user_conversation_summary(uid)
        finally:
            await provider.aclose()

    saved, summary = asyncio.run(run())
    assert "error" not in saved and summary["stated_preferences"]["area"]["value"] == "渋谷"
    assert uid in (tmp_path / "state.json").read_text(encoding="utf-8")
    assert not (tmp_path / "elsewhere.json").exists()


def test_the_worker_without_the_key_cannot_reach_the_provider(provider_server, monkeypatch):
    monkeypatch.setattr(voice_config, "PROVIDER_MCP_URL", f"http://127.0.0.1:{provider_server}/mcp")
    monkeypatch.delenv("AICA_INTERNAL_KEY", raising=False)

    async def run():
        provider = ProviderMCP()
        try:
            await provider.start()
        finally:
            await provider.aclose()

    with pytest.raises(Exception):  # noqa: B017 - 401 は mcp の中で別の例外に包まれる
        asyncio.run(run())


def test_the_worker_service_points_at_the_web_service(tmp_path):
    """scripts/voice_worker_start.sh は、Render が写した Web サービスの名前とポートから、結果と事業者の URL を作る。"""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "python").write_text('#!/bin/sh\nexec "$PYTHON_REAL" -c \'import json, os, sys; print(json.dumps({'
                                 '"argv": sys.argv[1:], **{k: os.environ[k] for k in '
                                 '("VOICE_RESUME_TALK_URL", "PROVIDER_MCP_URL", "HF_HOME")}}))\' "$@"\n')
    (fake / "python").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "PYTHON_REAL": sys.executable,
           "AICA_WEB_HOSTPORT": "aica-abcd:10000", "AICA_PROVIDER_PORT": "8020"}
    out = subprocess.run(["sh", str(ROOT / "scripts" / "voice_worker_start.sh")], env=env, capture_output=True,
                         text=True, timeout=30, check=True).stdout
    got = json.loads(out)
    assert got == {"argv": ["-m", "voice_resume.agent", "start"], "VOICE_RESUME_TALK_URL": "http://aica-abcd:10000",
                   "PROVIDER_MCP_URL": "http://aica-abcd:8020/mcp", "HF_HOME": "/app/models"}


def test_a_call_while_the_web_provider_is_down_fails_within_the_deadline_instead_of_hanging(tmp_path, monkeypatch):
    """Web サービスを入れ替える間(ディスクつきなので止まる)に相談中の事業者の呼び出しが来ても、待ち続けずに期限で失敗する。"""
    proc, port = _provider_http(tmp_path, "0.0.0.0", KEY)
    monkeypatch.setattr(voice_config, "PROVIDER_MCP_URL", f"http://127.0.0.1:{port}/mcp")
    monkeypatch.setenv("AICA_INTERNAL_KEY", KEY)

    async def run():
        provider = ProviderMCP(call_timeout=2)
        await provider.start()
        try:
            await provider.get_user_conversation_summary("w-8888888888888888")
            proc.kill()
            proc.wait(timeout=10)
            started = time.monotonic()
            with pytest.raises(Exception):  # noqa: B017 - 期限切れか、切れた接続のエラー
                await asyncio.wait_for(provider.get_user_conversation_summary("w-8888888888888888"), 20)
            return time.monotonic() - started
        finally:
            await provider.aclose()

    assert asyncio.run(run()) < 10


def test_connecting_to_a_provider_that_does_not_answer_gives_up_quickly(monkeypatch):
    """事業者の口が応えない(つながるが返事が無い)と、部屋に入る前に長く待たない。求人の提案なしで相談を続ける。"""
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    monkeypatch.setattr(voice_config, "PROVIDER_MCP_URL", f"http://127.0.0.1:{silent.getsockname()[1]}/mcp")

    async def run():
        provider = ProviderMCP(start_timeout=1)
        started = time.monotonic()
        try:
            with pytest.raises(Exception):  # noqa: B017
                await asyncio.wait_for(provider.start(), 20)
            return time.monotonic() - started
        finally:
            await provider.aclose()

    try:
        assert asyncio.run(run()) < 5
    finally:
        silent.close()
