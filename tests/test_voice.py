"""音声相談(voice-resume)との連携のテスト。LLM は使わない(task 40 / task 92)。

音声相談はトークの上のシートで開く: シートが POST /api/voice/start で接続トークンを受け取り(本人の
Cookie のユーザーだけ。会話数を1つ使う)、開いている間だけ POST /api/voice/wait で結果を待つ(ユーザーIDは本文)。
voice-resume が面談の結果を /api/voice/result に共有鍵つきで送ると、今の会話に AI の返信として続く(再読み込みでも残る)。
トークに出る求人は dip の求人データから作り直す(dip に無い求人はトークに出さない)。
"""

from __future__ import annotations

import pytest

from dip_mcp import logic
from dip_mcp.data import State, load_jobs


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.json")
    s.reset()
    return s


DRAFT = "# 履歴書の下書き\n\n- カフェで1年勤務\n"


def _voice_jobs():
    """dip の実データの求人2件を、voice-resume が送る形(カード)で。"""
    jobs = load_jobs()
    ids = sorted(jobs)[:2]
    return [{"job_id": i, "title": jobs[i].title, "reasons": ["夕方の勤務"]} for i in ids], ids


# ----------------------------- logic -----------------------------


def test_record_voice_result_rebuilds_cards_from_dip_jobs_only(state):
    """カードは dip の求人データから作り直し、dip に無い求人ID(でっち上げ)は捨てる。"""
    jobs, ids = _voice_jobs()
    fake = {"job_id": "zzz999", "title": "存在しない求人", "hourly_wage": 9999}
    r = logic.record_voice_result(state, "sim-new", jobs + [fake], DRAFT, ["out/resume.md"])
    assert r["cards"] == 2 and r["dropped_job_ids"] == ["zzz999"]
    results = state.user("sim-new")["voice_results"]
    cards = results[0]["cards"]
    assert [c["job_id"] for c in cards] == ids
    # カードの中身(時給・駅など)は dip の求人データの値。理由は voice 側の検索時のものを使う
    assert cards[0]["pay"] == load_jobs()[ids[0]].pay.model_dump()
    assert cards[0]["reasons"] == ["夕方の勤務"]


def test_voice_results_since_returns_what_came_after_the_sheet_opened(state):
    jobs, _ = _voice_jobs()
    logic.record_voice_result(state, "sim-new", jobs, DRAFT)
    since = logic.voice_result_count(state, "sim-new")  # シートを開いた時点
    assert since == 1 and logic.voice_results_since(state, "sim-new", since) == []
    logic.record_voice_result(state, "sim-new", [], DRAFT + "2")
    (got,) = logic.voice_results_since(state, "sim-new", since)
    assert got["id"] == 1 and got["draft_markdown"] == DRAFT + "2"
    # 下書きの表示ページからは id で読める
    assert logic.voice_result(state, "sim-new", 0)["draft_markdown"] == DRAFT
    assert logic.voice_result(state, "sim-new", 99) is None


def test_record_voice_result_without_jobs_keeps_the_draft(state):
    """0件のまま終わった相談でも、下書きだけはトークへ戻せる。"""
    r = logic.record_voice_result(state, "sim-new", [], DRAFT)
    assert r["cards"] == 0
    (got,) = logic.voice_results_since(state, "sim-new", 0)
    assert got["cards"] == [] and got["draft_markdown"] == DRAFT


def test_record_voice_result_keeps_only_safe_preferences(state):
    """まとめに使う希望は、保存と同じ決まり: 決まった項目だけ・個人情報らしい値は捨てる。"""
    logic.record_voice_result(state, "sim-new", [], DRAFT, preferences={
        "job_type": "カフェ", "min_wage": 1200, "area": "みほん町3-5に住んでいます", "phone": "090-1234-5678", "skills": " "})
    (got,) = logic.voice_results_since(state, "sim-new", 0)
    assert got["preferences"] == {"job_type": "カフェ", "min_wage": "1200"}


# ----------------------------- server(入口とAPI) -----------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # モデルは作るが呼ばない
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    from fastapi.testclient import TestClient

    from line_runtime import config, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    monkeypatch.setenv(server.INTERNAL_KEY_ENV, INTERNAL_KEY)
    server.state.reset()
    with TestClient(server.app) as c:
        c.get("/")  # 画面を開いた(匿名IDの Cookie が発行される)
        yield c


INTERNAL_KEY = "offline-test-internal-key-offline-test"
WORKER = {"Authorization": f"Bearer {INTERNAL_KEY}"}  # 音声ワーカー(サーバー間の経路)の共有鍵


def post_result(client, body: dict):
    """音声ワーカーと同じく、共有鍵を付けて結果を送る(Cookie は使わない)。"""
    from fastapi.testclient import TestClient

    return TestClient(client.app).post("/api/voice/result", json=body, headers=WORKER)


def post_session(client, body: dict):
    """音声ワーカーと同じく、共有鍵を付けて部屋の始まり・終わりを知らせる。"""
    from fastapi.testclient import TestClient

    return TestClient(client.app).post("/api/voice/session", json=body, headers=WORKER)


LIVEKIT = {"LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k",
           "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret"}


@pytest.fixture
def livekit(monkeypatch):
    for k, v in LIVEKIT.items():
        monkeypatch.setenv(k, v)


def test_voice_start_issues_a_token_only_for_the_browsers_own_user(client, livekit):
    """接続トークンは line_runtime が、Cookie の匿名ユーザーID のユーザーにだけ発行する(他人は 403、Cookie なしは 401)。
    有効期限は数分(同じトークンで入り直して、会話数を使わずに音声セッションを重ねられないように)。"""
    import jwt
    from fastapi.testclient import TestClient

    from voice_resume.agent import participant_user_id

    uid = client.get("/api/me").json()["user_id"]
    scoped = client.post("/api/scenarios/F-follow-new/start").json()["user"]  # /dev のシナリオユーザー
    for user in (uid, scoped):
        r = client.post("/api/voice/start", json={"user_id": user})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["url"] == LIVEKIT["LIVEKIT_URL"] and body["since"] == 0
        claims = jwt.decode(body["token"], options={"verify_signature": False})
        assert claims["sub"] == user and participant_user_id(type("P", (), {"metadata": claims["metadata"]})()) == user
        assert 0 < claims["exp"] - claims["nbf"] <= 10 * 60  # 既定の6時間ではなく数分
        # 同じ人の部屋は同時に 1 つまで(tests/test_voice_concurrency.py)。ワーカーが終わりを知らせてから次を始める
        post_session(client, {"user_id": user, "room": body["room"], "state": "ended"})

    other = TestClient(client.app)  # 別のブラウザ
    other.get("/")
    ub = other.get("/api/me").json()["user_id"]
    assert client.post("/api/voice/start", json={"user_id": ub}).status_code == 403
    assert client.post("/api/voice/start", json={"user_id": f"{ub}:sim-new"}).status_code == 403
    assert client.post("/api/voice/wait", json={"user_id": ub, "since": 0, "timeout": 0}).status_code == 403
    nc = TestClient(client.app)  # Cookie の無い呼び出しにも発行しない(実 API の費用につながる)
    assert nc.post("/api/voice/start", json={"user_id": uid}).status_code == 401
    assert nc.post("/api/voice/start", json={"user_id": "sim-new"}).status_code == 401


def test_voice_start_without_livekit_keys_spends_no_turn(client, monkeypatch):
    for k in LIVEKIT:
        monkeypatch.delenv(k, raising=False)
    uid = client.get("/api/me").json()["user_id"]
    r = client.post("/api/voice/start", json={"user_id": uid})
    assert r.status_code == 503 and "LIVEKIT_URL" in r.json()["detail"]
    assert client.app.state.quota.used(uid) == 0


def test_the_old_voice_tab_and_polling_are_gone(client):
    """別タブの会話ページへの入口(/liff/voice)と、常時の問い合わせ(/pull)は無い。"""
    from line_runtime import config

    uid = client.get("/api/me").json()["user_id"]
    assert client.get("/liff/voice", params={"user": uid}, follow_redirects=False).status_code == 404
    assert client.post(f"/api/voice/{uid}/pull").status_code in (404, 405)
    assert client.post(f"/api/voice/{uid}/start").status_code in (404, 405)  # URL にユーザーIDを載せる入口も無い
    core = (config.ROOT / "line_runtime" / "static" / "talk-core.js").read_text(encoding="utf-8")
    assert "setInterval" not in core and "/pull" not in core and "window.open" not in core
    assert "[音声相談]" not in core
    assert "[音声相談]" not in (config.ROOT / "line_runtime" / "static" / "dev.html").read_text(encoding="utf-8")


def test_voice_cards_stay_usable_in_later_turns():
    """音声相談の結果のカードは、次のターンの返信でも(dip のツールを呼び直さずに)カードとして出せる。
    LLM には求人のID と名前が伝わる。"""
    from langchain_core.messages import AIMessage, HumanMessage

    from line_runtime.agent import LineReply
    from line_runtime.runtime import render, voice_event
    from line_runtime.voice_reply import voice_reply

    jobs, ids = _voice_jobs()
    cards = [logic.job_card(load_jobs()[i], ["夕方の勤務"]) for i in ids]
    reply = voice_reply("u", {"id": 0, "cards": cards, "draft_markdown": "", "preferences": {}})
    event = voice_event(reply)
    assert ids[0] in event and jobs[0]["title"] in event
    msgs = [HumanMessage(event, additional_kwargs={"voice": reply}), AIMessage("{}"), HumanMessage("1件目を詳しく")]
    r = render({"messages": msgs, "structured_response": LineReply(bubbles=["こちらです。"], job_ids=[ids[0], "zzz999"])})
    assert [c["job_id"] for c in r["cards"]] == [ids[0]] and r["unknown_job_ids"] == ["zzz999"]


def test_wait_returns_only_the_result_of_its_own_room(client):
    """シートは自分の面談(部屋)の結果だけを受け取る。前に閉じたシートの面談の結果は読み飛ばす(会話には入っている)。"""
    uid = client.get("/api/me").json()["user_id"]
    post_result(client, {"user_id": uid, "draft_markdown": DRAFT, "room": "voice-resume-old"})
    got = client.post("/api/voice/wait", json={"user_id": uid, "since": 0, "room": "voice-resume-new", "timeout": 0.2}).json()
    assert got == {"results": [], "next": 1}
    post_result(client, {"user_id": uid, "draft_markdown": DRAFT, "room": "voice-resume-new"})
    got = client.post("/api/voice/wait", json={"user_id": uid, "since": 1, "room": "voice-resume-new", "timeout": 0}).json()
    assert [r["voice_result"] for r in got["results"]] == [1] and got["next"] == 2


def test_wait_returns_nothing_until_a_result_arrives(client):
    uid = client.get("/api/me").json()["user_id"]
    r = client.post("/api/voice/wait", json={"user_id": uid, "since": 0, "timeout": 0.2})
    assert r.status_code == 200 and r.json() == {"results": [], "next": 0}


def test_voice_result_round_trip_over_the_api(client):
    """voice-resume が結果を送る → シートの wait が受け取る(開いた後に届いた分だけ)→ 下書きページが開ける。"""
    from fastapi.testclient import TestClient

    jobs, ids = _voice_jobs()
    uid = client.get("/api/me").json()["user_id"]
    r = post_result(client, {
        "user_id": uid, "jobs": jobs, "draft_markdown": DRAFT, "files": ["out/resume.md"],
        "preferences": {"job_type": "カフェ", "time_band": "夕方"},
    })
    assert r.status_code == 200 and r.json()["cards"] == 2
    assert r.json()["talk"] is False  # まだ会話が無いユーザー: トークには続けない(dip には預かる)

    got = client.post("/api/voice/wait", json={"user_id": uid, "since": 0, "timeout": 0}).json()
    assert got["next"] == 1
    (reply,) = got["results"]
    assert [c["job_id"] for c in reply["cards"]] == ids
    assert reply["bubbles"][0] == "音声でお話しした内容をまとめました。\n職種: カフェ\n時間帯: 夕方"
    # ユーザーIDは無い。document_kind の無い結果(format_version 1.0 の頃の下書き)は履歴書
    assert reply["buttons"] == [{"label": "履歴書の下書きを見る", "url": "/liff/voice/draft?rid=0"}]
    assert all(len(b) <= 80 for b in reply["bubbles"]) and not any("[音声相談]" in b for b in reply["bubbles"])
    # 受け取った後(next から)は、次の結果が届くまで空
    assert client.post("/api/voice/wait", json={"user_id": uid, "since": 1, "timeout": 0}).json()["results"] == []

    page = client.get("/liff/voice/draft", params={"rid": 0})  # 本人は Cookie から決まる
    assert page.status_code == 200 and "カフェで1年勤務" in page.text
    assert client.get("/liff/voice/draft", params={"rid": 9}).status_code == 404
    other = TestClient(client.app)
    other.get("/")
    assert other.get("/liff/voice/draft", params={"rid": 0}).status_code == 404  # 別のブラウザには見えない
    assert other.get("/liff/voice/draft", params={"rid": 0, "user": uid}).status_code == 404  # URL で名乗っても同じ
    assert TestClient(client.app).get("/liff/voice/draft", params={"rid": 0}).status_code == 401
