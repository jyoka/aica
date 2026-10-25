"""音声相談の設定(LIVEKIT_*)があるのに内部の鍵(AICA_INTERNAL_KEY)が無い・短いと、Web サーバーも音声ワーカーも起動で止まる(task 218)。

鍵が無いまま動くと、相談は最後まで進むが、ワーカーが送る結果(POST /api/voice/session と /api/voice/result)が 401 で断られ、
まとめと下書きがトークに届かない。音声相談の設定が無ければ、鍵が無くても今までどおり起動する。
実 API(LiveKit、OpenAI、Deepgram、Cartesia、LINE)は呼ばない。鍵はダミー値で、本物の .env は読まない
(.env の場所を、無いファイルに替える)。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from dip_mcp.data import State
from line_runtime import line_channel

ROOT = Path(__file__).resolve().parents[1]
LIVEKIT = {"LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k",
           "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret"}
NO_LIVEKIT = dict.fromkeys(LIVEKIT, "")
GOOD_KEY = "offline-test-internal-key-" + "0" * 17  # 43 文字(secrets.token_urlsafe(32) と同じ長さ)
# LINE と LIFF の設定は無し(AICA_ID_KEY の確かめは別のテスト。test_line_webhook)
NO_LINE = dict.fromkeys(("LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "LINE_LIFF_ID", "LINE_LIFF_CHANNEL_ID",
                         "LINE_LOGIN_CHANNEL_ID"), "")
SHORT = "x" * 31


@pytest.fixture
def server(tmp_path, monkeypatch):
    """Web サーバー。state と data は tmp、.env の場所も tmp(本物の .env は読まない)。"""
    from line_runtime import config, server

    for k, v in {"OPENAI_API_KEY": "offline-test", "ANTHROPIC_API_KEY": "offline-test", **NO_LINE}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("LINE_SIM_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "REPO_ENV", tmp_path / "missing.env")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()
    return server


def _set(monkeypatch, env: dict[str, str]) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)


@pytest.mark.parametrize(("key", "why"), [("", "がありません"), (SHORT, "が短すぎます(31 文字"),
                                          ("  " + SHORT + "\n", "が短すぎます(31 文字")])
def test_voice_settings_without_a_long_enough_internal_key_stop_the_server_at_startup(server, monkeypatch, key, why):
    from fastapi.testclient import TestClient

    _set(monkeypatch, {**LIVEKIT, "AICA_INTERNAL_KEY": key})
    with pytest.raises(line_channel.InternalKeyError, match=re.escape(f"AICA_INTERNAL_KEY {why}")) as err, TestClient(server.app):
        pass
    msg = str(err.value)
    assert ".env" in msg and "secrets.token_urlsafe(32)" in msg  # どこに書くか、作り方も出す
    assert "LIVEKIT_URL" in msg  # なぜ要るのか(音声相談の設定があるから)


def test_voice_settings_without_the_key_variable_at_all_stop_the_server(server, monkeypatch):
    from fastapi.testclient import TestClient

    _set(monkeypatch, LIVEKIT)
    monkeypatch.delenv("AICA_INTERNAL_KEY", raising=False)
    with pytest.raises(line_channel.InternalKeyError, match="AICA_INTERNAL_KEY がありません"), TestClient(server.app):
        pass


@pytest.mark.parametrize(("env", "note"), [({**LIVEKIT, "AICA_INTERNAL_KEY": GOOD_KEY}, "鍵がある"),
                                           ({**LIVEKIT, "AICA_INTERNAL_KEY": "x" * 32}, "32 文字ちょうど"),
                                           ({**NO_LIVEKIT, "AICA_INTERNAL_KEY": ""}, "音声相談の設定が無い"),
                                           ({**LIVEKIT, "LIVEKIT_API_SECRET": "", "AICA_INTERNAL_KEY": ""},
                                            "音声相談の設定が欠けている")])
def test_the_server_starts_as_before(server, monkeypatch, env, note):
    from fastapi.testclient import TestClient

    _set(monkeypatch, env)
    with TestClient(server.app) as client:
        assert client.get("/").status_code == 200, note


@pytest.mark.parametrize("raw", [GOOD_KEY, f"  {GOOD_KEY}\t"])
def test_the_internal_route_still_checks_the_key_that_the_server_started_with(server, monkeypatch, raw):
    """起動を通った鍵で、ワーカーの経路(POST /api/voice/session)は今までどおり鍵を照合する(違う鍵は 401)。
    前後の空白は、起動の確かめと同じく除いて比べる(ワーカーも除いて送る。空白つきで起動を通ったのに 401 にしない)。"""
    from fastapi.testclient import TestClient

    _set(monkeypatch, {**LIVEKIT, "AICA_INTERNAL_KEY": raw})
    body = {"user_id": "w-0000000000000001", "room": "voice-resume-x", "state": "joined"}
    with TestClient(server.app) as client:
        wrong = client.post("/api/voice/session", json=body, headers={"Authorization": "Bearer " + "y" * 43})
        right = client.post("/api/voice/session", json=body, headers={"Authorization": f"Bearer {GOOD_KEY}"})
    assert wrong.status_code == 401 and right.status_code == 200


# ----------------------------- 音声ワーカー(python -m voice_resume.agent) -----------------------------

# livekit-agents の CLI(run_app)だけを差し替えて、本物と同じ `python -m voice_resume.agent dev` の入口を動かす。
# 起動まで来たら印を出して終わる(LiveKit にはつながない)
WORKER = """
import runpy, sys
from pathlib import Path
from livekit.agents import cli
from voice_resume import config
config.ENV_FILE = Path(sys.argv[2])  # 本物の .env は読まない
cli.run_app = lambda opts: print("WORKER-STARTED", flush=True)
sys.argv = ["voice_resume.agent", sys.argv[1]]
runpy.run_module("voice_resume.agent", run_name="__main__", alter_sys=True)
"""


def _worker(tmp_path: Path, env: dict[str, str], command: str = "dev") -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("LIVEKIT_", "AICA_"))}
    missing_env = str(tmp_path / "missing.env")
    return subprocess.run([sys.executable, "-c", WORKER, command, missing_env], check=False, cwd=ROOT, capture_output=True,
                          text=True, timeout=120, env={**base, **NO_LINE, **env})


@pytest.mark.parametrize(("key", "why"), [("", "がありません"), (SHORT, "が短すぎます")])
def test_voice_settings_without_a_long_enough_internal_key_stop_the_worker_at_startup(tmp_path, key, why):
    got = _worker(tmp_path, {**LIVEKIT, "AICA_INTERNAL_KEY": key})
    assert got.returncode == 1
    assert "WORKER-STARTED" not in got.stdout
    assert f"AICA_INTERNAL_KEY {why}" in got.stderr and "secrets.token_urlsafe(32)" in got.stderr
    assert "Traceback" not in got.stderr  # 1 つのメッセージで止まる


@pytest.mark.parametrize(("env", "command"), [({**LIVEKIT, "AICA_INTERNAL_KEY": GOOD_KEY}, "dev"),
                                              ({**LIVEKIT, "AICA_INTERNAL_KEY": GOOD_KEY}, "start"),
                                              ({**NO_LIVEKIT, "AICA_INTERNAL_KEY": ""}, "dev"),
                                              ({**LIVEKIT, "AICA_INTERNAL_KEY": ""}, "download-files")])
def test_the_worker_starts_as_before(tmp_path, env, command):
    """正しい鍵があるか、音声相談の設定が無ければ起動する。モデルを取るだけの download-files は鍵を確かめない
    (コンテナはワーカーを動かす前に取る。scripts/container_start.sh)。"""
    got = _worker(tmp_path, env, command)
    assert got.returncode == 0, got.stderr
    assert "WORKER-STARTED" in got.stdout
