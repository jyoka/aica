"""dip の時計の台本テスト(R4、ADR-0004)。実 LLM なし、実 API なし(履歴書の読み取りはスタブ)。

- 本番のユーザー(匿名ユーザーID `w-…`)は実際の時刻: 保存、希望の保存日、履歴書のアップロード、音声相談の結果、
  区分の切り替えの時刻と、90 日、7 日、3 日の判定
- シナリオユーザー(`w-…:sim-new` など)と評価のユーザー(`sim-new` など)は、今までどおり固定の時計 SIM_NOW
  (fixtures の「2時間前に離脱」「9/19 に応募」を毎回同じ会話として再現するため)
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from dip_mcp.data import JST, SIM_NOW, State, now, parse_ts
from evals.scenarios import load_scenarios, prepare
from tests.test_resume_upload import _open, _upload
from tests.test_resume_upload import app as upload_app  # noqa: F401  フィクスチャ
from tests.test_voice import INTERNAL_KEY, WORKER


@pytest.fixture
def app(upload_app, monkeypatch):  # noqa: F811
    """Web サーバー(状態は一時ディレクトリ、履歴書の読み取りはスタブ)。音声ワーカーの共有鍵も置く。"""
    monkeypatch.setenv(upload_app.INTERNAL_KEY_ENV, INTERNAL_KEY)
    return upload_app


def _real(ts: str, before: datetime, after: datetime) -> bool:
    return before <= parse_ts(ts) <= after


# ----------------------------- 本番のユーザー: 実際の時刻 -----------------------------


def test_production_user_records_use_real_time(app):
    """本番のトーク画面のユーザーが希望を直す、区分を切り替える、履歴書を上げる、音声相談を終える、求人を保存すると、
    dip に残る時刻は実際の時刻(前は、どれも SIM_NOW の 2026-09-23 12:00 になっていた)。"""
    with TestClient(app.app) as client:
        uid = _open(client)
        before = datetime.now(JST)
        assert client.put("/api/profile/items/area", json={"value": "新宿"}).status_code == 200
        assert client.put("/api/profile/track", json={"track": "正社員"}).status_code == 200
        assert _upload(client, uid).status_code == 200
        r = TestClient(app.app).post("/api/voice/result", json={"user_id": uid, "draft_markdown": "# 履歴書"},
                                     headers=WORKER)
        assert r.status_code == 200
        logic.save_job(app.state, uid, "b0101")  # トークの「保存」(dip の MCP の save_job と同じ)
        after = datetime.now(JST)

    u = app.state.user(uid)
    stamps = {"希望の保存日": u["preferences"]["area"]["at"], "区分の切り替え": u["track_history"][-1]["at"],
              "履歴書のアップロード": u["resume_upload"]["uploaded_at"], "音声相談の結果": u["voice_results"][-1]["at"],
              "保存": u["saves"][-1]["at"], "プロフィールの知らせ": u["profile_changes"][-1]["at"]}
    assert {k: v for k, v in stamps.items() if not _real(v, before, after)} == {}
    # 画面に出す日付も今日(前は 9/23)
    today = f"{after.month}/{after.day}"
    assert {j["saved_at"] for j in logic.saved_jobs(app.state, uid)} <= {today, f"{before.month}/{before.day}"}


def test_production_user_periods_count_from_real_time(app):
    """本番のユーザーの期間の判定(90 日、7 日、3 日)も実際の時刻から数える。2 日前の応募は「2 日前」、
    100 日前の応募は 90 日を過ぎたので出さない(前は SIM_NOW から数えたので、日数が実際とずれていた)。"""
    with TestClient(app.app) as client:
        uid = _open(client)
    real = datetime.now(JST)

    def seed(u):
        u["dip_id"] = "dip-prod-1"  # 連携済みの本番のユーザー(プッシュの判定まで進むように)
        u["applications"] = [{"job_id": "b0101", "applied_at": (real - timedelta(days=100)).isoformat(), "status": "不採用"},
                             {"job_id": "b0102", "applied_at": (real - timedelta(days=2)).isoformat(),
                              "status": "応募済み(店舗からの連絡待ち)"}]
        u["form_exits"] = [{"job_id": "b0103", "exited_at": (real - timedelta(hours=5)).isoformat(),
                            "completed": {"氏名": "架空"}, "remaining": ["電話番号"]}]
        u["contacts"] = [{"at": (real - timedelta(days=4)).isoformat(), "channel": "LINE AI通知", "message": "form_exit"}]

    app.state.update(uid, seed)
    assert [(a["job_id"], a["days_ago"]) for a in logic.recent_applications(app.state, uid)] == [("b0102", 2)]
    assert logic.conversation_summary(app.state, uid)["form_exit"]["hours_ago"] == 5
    # 最後の接触は 4 日前なので、3 日の間隔を過ぎている(前は SIM_NOW から見て「未来の接触」で、ずっと断っていた)
    assert logic.push_trigger(app.state, uid, "form_exit")["allowed"] is True
    assert _real(app.state.user(uid)["contacts"][-1]["at"], real, datetime.now(JST))


def test_production_user_new_listings_count_from_the_fixture_clock(app):
    """新着の絞り込みは、本番のユーザーでも SIM_NOW から数える。掲載開始の日付は fixtures のシミュレーションの値で、
    SIM_NOW から見て書いてあるため(実際の時刻から数えると、本番のユーザーには新着がいつも 0 件になる)。"""
    with TestClient(app.app) as client:
        uid = _open(client)
    for days in (3, 7, 14):
        got = logic.recommend(app.state, uid, posted_within_days=days)
        assert got["total"] > 0
        assert got["total"] == logic.recommend(app.state, None, posted_within_days=days)["total"]


# ----------------------------- シナリオユーザーと評価のユーザー: 固定の時計 -----------------------------


def test_scenario_user_keeps_the_fixed_clock(app):
    """開発者画面のシナリオユーザー(匿名ID:シナリオユーザー)は、今までどおり SIM_NOW で記録し、SIM_NOW から数える。"""
    with TestClient(app.app) as client:
        anon = _open(client)
        started = client.post("/api/scenarios/B-exit/start").json()
        user = started["user"]
        assert user == f"{anon}:sim-exit"
        # fixtures の「2時間前に離脱」は SIM_NOW から見た時刻
        assert logic.conversation_summary(app.state, user)["form_exit"]["hours_ago"] == 2
        r = client.post("/liff/apply", data={"job": logic.form_exit(app.state, user)["job_id"], "scenario_user": "sim-exit"})
        assert r.status_code == 200
        assert _upload(client, user).status_code == 200
    logic.save_preferences(app.state, user, {"area": "新宿"})
    logic.set_search_track(app.state, user, "正社員", "本人が「正社員で探したい」と話した")
    logic.save_job(app.state, user, "b0101")
    u = app.state.user(user)
    stamps = [u["applications"][-1]["applied_at"], u["resume_upload"]["uploaded_at"], u["preferences"]["area"]["at"],
              u["track_history"][-1]["at"], u["saves"][-1]["at"]]
    assert {parse_ts(s) for s in stamps} == {SIM_NOW}


def test_eval_users_keep_the_fixed_clock(tmp_path):
    """評価のユーザー(fixtures のユーザーそのもの)と、ユーザーの無い検索は SIM_NOW。評価を毎回同じ会話にするため。"""
    state = State(tmp_path / "state.json")
    state.reset()
    for sc in load_scenarios():
        prepare(state, sc)
    assert logic.conversation_summary(state, "sim-applied")["last_application"]["days_ago"] == 4  # 9/19 に応募
    assert logic.conversation_summary(state, "sim-exit")["form_exit"]["hours_ago"] == 2
    assert logic.push_trigger(state, "sim-exit", "form_exit")["allowed"] is True
    assert parse_ts(state.user("sim-exit")["contacts"][-1]["at"]) == SIM_NOW
    logic.save_job(state, "sim-new", "b0101")
    assert parse_ts(state.user("sim-new")["saves"][-1]["at"]) == SIM_NOW
    # 新着の絞り込み(掲載開始が SIM_NOW の前日の求人)も SIM_NOW から数える
    assert logic.recommend(state, None, posted_within_days=3)["total"] > 0


def test_which_users_use_the_fixed_clock():
    assert now("sim-new") == now("w-0123456789abcdef:sim-new") == now(None) == SIM_NOW
    before = datetime.now(JST)
    assert before <= now("w-0123456789abcdef") <= datetime.now(JST)
