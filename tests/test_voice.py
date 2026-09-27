"""音声相談(voice-resume)との連携のテスト。LLM は使わない(task 40)。

voice-resume が面談の結果を /api/voice/result に送り、トークが pull で受け取って描く。
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
    assert cards[0]["hourly_wage"] == load_jobs()[ids[0]].hourly_wage
    assert cards[0]["reasons"] == ["夕方の勤務"]


def test_pull_voice_results_returns_undelivered_once(state):
    jobs, _ = _voice_jobs()
    logic.record_voice_result(state, "sim-new", jobs, DRAFT)
    pulled = logic.pull_voice_results(state, "sim-new")
    assert len(pulled) == 1 and pulled[0]["draft_markdown"] == DRAFT
    assert logic.pull_voice_results(state, "sim-new") == []  # 2回目は空(受け取り済み)
    # 受け取り済みでも、下書きの表示ページからは id で読める
    assert logic.voice_result(state, "sim-new", pulled[0]["id"])["draft_markdown"] == DRAFT
    assert logic.voice_result(state, "sim-new", 99) is None


def test_record_voice_result_without_jobs_keeps_the_draft(state):
    """0件のまま終わった相談でも、下書きだけはトークへ戻せる。"""
    r = logic.record_voice_result(state, "sim-new", [], DRAFT)
    assert r["cards"] == 0
    pulled = logic.pull_voice_results(state, "sim-new")
    assert pulled[0]["cards"] == [] and pulled[0]["draft_markdown"] == DRAFT


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
    server.state.reset()
    with TestClient(server.app) as c:
        yield c


def test_voice_entry_redirects_to_the_voice_page_with_the_user(client):
    """トークの「音声で相談」→ /liff/voice が voice-resume の会話ページへユーザーIDつきで送る。"""
    r = client.get("/liff/voice", params={"user": "sim-new"}, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/?user=sim-new")


def test_voice_result_round_trip_over_the_api(client):
    """voice-resume が結果を送る → トークが pull で1回だけ受け取る → 下書きページが開ける。"""
    jobs, ids = _voice_jobs()
    r = client.post("/api/voice/result", json={
        "line_user_id": "sim-new", "jobs": jobs, "draft_markdown": DRAFT, "files": ["out/resume.md"],
    })
    assert r.status_code == 200 and r.json()["cards"] == 2

    pulled = client.post("/api/voice/sim-new/pull").json()["results"]
    assert [c["job_id"] for c in pulled[0]["cards"]] == ids
    assert client.post("/api/voice/sim-new/pull").json()["results"] == []

    page = client.get("/liff/voice/draft", params={"user": "sim-new", "rid": pulled[0]["id"]})
    assert page.status_code == 200 and "カフェで1年勤務" in page.text
    assert client.get("/liff/voice/draft", params={"user": "sim-new", "rid": 9}).status_code == 404
