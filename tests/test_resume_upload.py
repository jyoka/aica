"""トークからの履歴書アップロード(resume-intake の移植, task 63)。実 API は呼ばない。

アップロード → 読み取り(OpenAI はスタブ)→ dip の状態に保存 → 読み取り結果の確認ページ、と
保存した読み取り結果を同じユーザーの会話が get_user_conversation_summary で参照することを確かめる。
個人情報(personal・電話・メール・郵便番号・ファイル名)がどこにも残らないことも見る。
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from dip_mcp import logic
from dip_mcp.data import State
from resume_intake.schema import PERSONAL_KEYS, ResumeDraft
from tests.test_handle import ScriptedModel, call, reply
from tests.test_resume_intake import CLEAN, StubClient

PII = ("090-0000-1111", "misaki.sato@example.com", "100-0000", "03-1234-5678")
FILENAME = "佐藤美咲_履歴書.pdf"  # ファイル名には氏名が入りがち。保存しない
U1 = "w-1111111111111111:sim-new"
U2 = "w-2222222222222222:sim-new"


def clean_draft() -> ResumeDraft:
    return ResumeDraft.model_validate(json.loads(CLEAN.read_text(encoding="utf-8")))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # モデルは作るが呼ばない
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    from line_runtime import config, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()
    stub = StubClient(clean_draft(), usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    monkeypatch.setattr(server, "resume_client", lambda: stub)
    server.stub = stub
    return server


def _upload(client, user, name=FILENAME, data=b"%PDF-1.4 dummy"):
    return client.post(f"/api/resume/{user}/upload", files={"file": (name, data, "application/octet-stream")})


# ----------------------------- アップロード → 確認 -----------------------------


def test_upload_reads_saves_and_shows_the_result(app, tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        client.get("/api/me")
        user = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        r = _upload(client, user)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["empty"] is False
        assert body["view_url"] == f"/liff/resume?user={user}"
        # スタブに PDF がそのまま渡っている(ファイルはディスクに置かずバイト列で読む)
        part = app.stub.calls[0]["input"][0]["content"][0]
        assert part["type"] == "input_file" and part["file_data"].startswith("data:application/pdf;base64,")
        # 応答にも個人情報は無い(トークに出す確認の材料)
        assert all(v == "" for v in body["result"]["personal"].values())
        assert not any(p in r.text for p in PII)
        assert body["result"]["resume"]["work_history"][0]["organization"] == "コンビニみほん駅前店"

        page = client.get(body["view_url"])
        assert page.status_code == 200
        assert "コンビニみほん駅前店" in page.text and "みほん県立第一高等学校" in page.text
        assert not any(p in page.text for p in PII)
        # 左の「dip から見た状態」にも出る
        assert client.get(f"/api/dip/{user}").json()["summary"]["uploaded_resume"]["uploaded"] == "9/23"
        # 費用台帳にアップロード分の行が付く(誰の利用か分かる)
        rows = [json.loads(x) for x in (tmp_path / "costs.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["event"] == "resume_upload" and rows[-1]["user_id"] == user and rows[-1]["cost_usd"] > 0

    saved = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert not any(p in saved for p in PII)
    assert FILENAME not in saved and "佐藤" not in saved


def test_upload_rejects_unsupported_files_and_other_users(app):
    from fastapi.testclient import TestClient

    with TestClient(app.app) as a:
        a.get("/api/me")  # 画面を開いたブラウザ(Cookie あり)
        mine = a.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert _upload(a, mine, name="resume.docx").status_code == 400
        assert _upload(a, mine, data=b"").status_code == 400
        b = TestClient(app.app)  # 別のブラウザ
        b.get("/api/me")
        other = b.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert _upload(a, other).status_code == 403  # 他人のトークに履歴書を差し込めない
        assert a.get("/liff/resume", params={"user": other}).status_code == 403
        assert a.get("/liff/resume", params={"user": mine}).status_code == 404  # まだアップロードしていない
    assert app.stub.calls == []  # どれも読み取り(API)まで行かない


def test_upload_counts_against_the_daily_quota(app, monkeypatch):
    """アップロードも1ターン扱い: 上限に達したら OpenAI を呼ばずに案内だけ返す(費用の防波堤を迂回させない)。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "1")
    with TestClient(app.app) as client:
        client.get("/api/me")
        user = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert _upload(client, user).status_code == 200  # 1回目は読み取れて、ターンを1つ使う
        assert app.app.state.quota.used(user) == 1
        r = _upload(client, user)
        assert r.status_code == 429
        assert "上限" in r.json()["detail"]
    assert len(app.stub.calls) == 1  # 2回目は読み取り(API)まで行かない


def test_unreadable_upload_is_marked_empty_and_keeps_the_earlier_result(app, monkeypatch):
    """読めなかったファイルは「読み取れなかった」と返すだけで、前に読み取れた結果を上書きしない。"""
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        assert _upload(client, "sim-new").json()["empty"] is False
        monkeypatch.setattr(app, "resume_client",
                            lambda: StubClient(ResumeDraft(unconfirmed=["画像が不鮮明で読み取れなかった"])))
        r = _upload(client, "sim-new", name="scan.jpg", data=b"\xff\xd8 dummy")
        assert r.status_code == 200 and r.json()["empty"] is True and r.json()["view_url"] is None
        assert r.json()["result"]["resume"]["unconfirmed"] == ["画像が不鮮明で読み取れなかった"]
        kept = logic.resume_upload(app.state, "sim-new")["resume"]
        assert kept["work_history"][0]["organization"] == "コンビニみほん駅前店"


# ----------------------------- 保存の防御(読み取り側に重ねる) -----------------------------


def test_save_resume_upload_blanks_personal_and_scrubs_pii(tmp_path):
    """読み取り側をすり抜けた値が来ても、dip に保存する時点で personal は空欄・文字列中の連絡先は消える。"""
    state = State(tmp_path / "state.json")
    leaked = {"format_version": "1.0", "source": "upload", "created_at": "2026-09-23T12:00:00+09:00",
              "personal": {"name": "佐藤 美咲", "phone": "090-0000-1111", "email": "misaki.sato@example.com"},
              "resume": clean_draft().model_dump()}
    rec = logic.save_resume_upload(state, U1, leaked)
    assert set(rec["personal"]) == set(PERSONAL_KEYS) and all(v == "" for v in rec["personal"].values())
    dumped = json.dumps(state.user(U1), ensure_ascii=False)
    assert not any(p in dumped for p in PII) and "佐藤" not in dumped
    assert "レジ" in dumped


# ----------------------------- 同じユーザーの会話から参照される -----------------------------


def test_uploaded_resume_is_referenced_in_the_same_users_conversation(tmp_path):
    """アップロードした読み取り結果は、同じユーザーの会話の要約(uploaded_resume)に載り、別ユーザーには載らない。
    dip の MCP は本物(子プロセス)、LLM は決められた順に返事をする偽物。"""
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    state = State(tmp_path / "state.json")
    logic.save_resume_upload(state, U1, {"format_version": "1.0", "resume": clean_draft().model_dump()})

    model = ScriptedModel(script=[
        call("get_user_conversation_summary", {"line_user_id": U1}),
        reply(["履歴書ではコンビニでレジと品出しをされていたんですね。近い仕事で探しますか?"]),
        call("get_user_conversation_summary", {"line_user_id": U2}),
        reply(["どんなお仕事を探していますか?"]),
    ])

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            r1 = await rt.handle(U1, "message", "履歴書をアップロードしました。この内容をふまえて相談させてください。")
            r2 = await rt.handle(U2, "message", "こんにちは")
            return r1, r2

    r1, r2 = asyncio.run(go())
    mine = r1["tools"][0]["result"]["uploaded_resume"]
    assert mine["work_history"][0]["organization"] == "コンビニみほん駅前店"
    assert mine["work_history"][0]["duties"][:2] == ["レジ", "品出し"]
    assert mine["skills"] == ["接客"] and mine["uploaded"] == "9/23"
    assert "other_experience" not in mine  # 空の項目は要約に載せない
    assert not any(p in json.dumps(r1["tools"], ensure_ascii=False) for p in PII)
    assert "uploaded_resume" in r1["tools"][0]["result"]["note"]  # 使い方の注意も一緒に渡る
    assert r2["tools"][0]["result"]["uploaded_resume"] is None  # 別ユーザーには出ない


def test_prompt_tells_the_agent_to_use_the_uploaded_resume():
    from dip_mcp.server import baitoru_assistant

    for entry in ("A", "visit", "revisit", "D"):
        text = baitoru_assistant(entry)
        assert "uploaded_resume" in text and "履歴書をアップロードしました" in text
