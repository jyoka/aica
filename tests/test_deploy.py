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


VOICE_SECRETS = ("ANTHROPIC_API_KEY", "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "DEEPGRAM_API_KEY",
                 "CARTESIA_API_KEY")


def test_the_image_and_the_blueprint_match_the_code():
    """Render の設定(render.yaml)と image(Dockerfile)が、ヘルスチェックの口と状態の置き場に合っている。"""
    web = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))["services"][0]
    assert web["runtime"] == "docker" and web["plan"] == "standard"
    assert web["healthCheckPath"] == "/healthz"
    assert web["numInstances"] == 1  # 会話数の上限(quota.json)は 1 つのプロセスで数える
    env = {e["key"]: e for e in web["envVars"]}
    assert env["AICA_TRUST_PROXY"]["value"] == "1"
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):  # トーク、履歴書は Anthropic(既定)、意味検索の埋め込みは OpenAI
        assert env[key] == {"key": key, "sync": False}  # 値はリポジトリに書かない
    # 音声ワーカーは aica-voice だけで動かし、このコンテナでは動かさない(消すと前の 1 が残るので、0 を書く)。
    # 待機のプロセスの数は、1 に戻したときのために残す。相談 1 件の上限は既定のまま
    assert env["AICA_VOICE_WORKER"]["value"] == "0" and env["AICA_DIP_PORT"]["value"].isdigit()
    assert env["VOICE_RESUME_MAX_JOBS"]["value"] == "2" and "VOICE_RESUME_JOB_MEMORY_LIMIT_MB" not in env
    for key in VOICE_SECRETS:  # 鍵はこのサービスにだけ入れ、aica-voice はここから写す
        assert env[key] == {"key": key, "sync": False}
    # 状態と出力の既定の置き場はすべてリポジトリ直下の data/。image では /app/data で、そこにディスクをつなぐ
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "WORKDIR /app\n" in dockerfile and "AICA_SECURE_COOKIE=1" in dockerfile
    assert web["disk"]["mountPath"] == "/app/data"
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and "data/" in ignored  # API キーと手元の会話を image に入れない


def test_the_voice_worker_is_a_service_of_its_own_that_reaches_the_web_service():
    """音声ワーカー(aica-voice)は同じ image の別サービス。Web サービスの名前、dip のポート、共有鍵を写し、ディスクは持たない。"""
    web, voice = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))["services"]
    assert voice["type"] == "worker" and voice["runtime"] == "docker" and "disk" not in voice
    assert voice["dockerfilePath"] == web["dockerfilePath"] and voice["region"] == web["region"]
    assert (ROOT / voice["dockerCommand"]).is_file()
    env = {e["key"]: e for e in voice["envVars"]}
    web_env = {e["key"] for e in web["envVars"]}
    copied = {k: e["fromService"] for k, e in env.items() if "fromService" in e}
    assert copied["AICA_WEB_HOSTPORT"] == {"name": web["name"], "type": "web", "property": "hostport"}
    for key in ("AICA_DIP_PORT", "AICA_INTERNAL_KEY"):
        assert copied[key] == {"name": web["name"], "type": "web", "envVarKey": key} and key in web_env
    assert env["VOICE_RESUME_STT_PROVIDER"]["value"] == "deepgram"  # 本番の声は OpenAI に送らない
    assert env["AICA_VOICE_MODELS"]["value"] == "1" and "ARG AICA_VOICE_MODELS" in (ROOT / "Dockerfile").read_text()
    assert int(env["VOICE_RESUME_MAX_JOBS"]["value"]) >= 1 and float(env["VOICE_RESUME_JOB_MEMORY_LIMIT_MB"]["value"]) > 0
    # Blueprint を更新するときは sync: false の値を聞かれないので、新しいサービスの鍵は Web サービスから写す
    for key in VOICE_SECRETS:
        assert copied[key] == {"name": web["name"], "type": "web", "envVarKey": key} and key in web_env
    assert not [k for k, e in env.items() if "sync" in e]


def test_the_container_does_not_let_uvicorn_rewrite_the_client_ip():
    """IP の上限は server._client_ip が AICA_TRUST_PROXY で決める。起動に --forwarded-allow-ips を付けない。"""
    start = (ROOT / "scripts" / "container_start.sh").read_text(encoding="utf-8")
    command_lines = [x for x in start.splitlines() if not x.lstrip().startswith("#")]
    assert not any("forwarded-allow-ips" in x.lower() or "FORWARDED_ALLOW_IPS" in x for x in command_lines)
    assert "exec python -m line_runtime.server" in start


def test_the_image_does_not_trust_the_leaked_emudhra_certificates():
    """eMudhra の中間証明書の漏洩(Render の通告、2026-10-27)。build した image の版は scripts/check_image_certs.py で確かめる。"""
    import certifi

    lines = [x.strip() for x in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    assert "certifi>=2026.10.19" in lines

    def version(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in v.split("."))

    assert version(certifi.__version__) >= (2026, 10, 19)  # 今の環境(CI)にも直った版が入っている
    # OS の CA の束(aiohttp を使う LiveKit などが読む)は、ベースの image のままにせず、build のたびに Debian の今の版を入れる
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    install = next(x for x in dockerfile.splitlines() if x.startswith("RUN apt-get update") and "apt-get install" in x)
    assert "ca-certificates" in install.split("apt-get install", 1)[1].split("&&")[0].split()
