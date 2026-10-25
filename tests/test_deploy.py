"""Render への公開(task 64): ヘルスチェック、Cookie の Secure、コンテナと Blueprint の設定がコードと合っていること。

コンテナを作って動かす確かめは scripts/deploy_smoke.py(docker と実 API を使うので、テストには入れない)。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from tests.test_multiuser import _env

ROOT = Path(__file__).resolve().parents[1]


def test_health_check_answers_without_a_cookie_and_does_not_issue_one(tmp_path, monkeypatch):
    server = _env(tmp_path, monkeypatch)
    with TestClient(server.app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert server.ANON_COOKIE not in r.cookies  # 数秒ごとのヘルスチェックで匿名ユーザーIDを増やさない


def test_the_cookie_is_secure_only_when_served_over_https(tmp_path, monkeypatch):
    server = _env(tmp_path, monkeypatch)
    with TestClient(server.app) as client:
        monkeypatch.delenv(server.SECURE_COOKIE_ENV, raising=False)
        plain = client.get("/").headers["set-cookie"].lower()
        assert "httponly" in plain and "samesite=lax" in plain and "secure" not in plain  # 手元の http でも送り返される
        client.cookies.clear()
        monkeypatch.setenv(server.SECURE_COOKIE_ENV, "1")
        secure = client.get("/").headers["set-cookie"].lower()
        assert "; secure" in secure and "httponly" in secure


def test_the_image_and_the_blueprint_match_the_code():
    """Render の設定(render.yaml)と image(Dockerfile)が、ヘルスチェックの口と状態の置き場に合っている。"""
    svc = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))["services"][0]
    assert svc["runtime"] == "docker" and svc["plan"] == "starter"
    assert svc["healthCheckPath"] == "/healthz"
    assert svc["numInstances"] == 1  # 会話数の上限(quota.json)は 1 つのプロセスで数える
    env = {e["key"]: e for e in svc["envVars"]}
    assert env["AICA_TRUST_PROXY"]["value"] == "1"
    assert env["OPENAI_API_KEY"] == {"key": "OPENAI_API_KEY", "sync": False}  # 値はリポジトリに書かない
    assert env["VOICE_RESUME_STT_PROVIDER"]["value"] == "deepgram"  # 本番の声は OpenAI に送らない
    # 状態と出力の既定の置き場はすべてリポジトリ直下の data/。image では /app/data で、そこにディスクをつなぐ
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "WORKDIR /app\n" in dockerfile and "AICA_SECURE_COOKIE=1" in dockerfile
    assert svc["disk"]["mountPath"] == "/app/data"
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and "data/" in ignored  # API キーと手元の会話を image に入れない


def test_the_container_does_not_let_uvicorn_rewrite_the_client_ip():
    """IP の上限は server._client_ip が AICA_TRUST_PROXY で決める。起動に --forwarded-allow-ips を付けない。"""
    start = (ROOT / "scripts" / "container_start.sh").read_text(encoding="utf-8")
    command_lines = [x for x in start.splitlines() if not x.lstrip().startswith("#")]
    assert not any("forwarded-allow-ips" in x.lower() or "FORWARDED_ALLOW_IPS" in x for x in command_lines)
    assert "exec python -m line_runtime.server" in start
