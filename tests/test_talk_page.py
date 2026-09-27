"""本番のトーク画面(/)と開発者画面(/dev)の台本テスト(task 90。実 LLM なし、dip の MCP は本物)。

本番の / はシナリオを選ばせない: 開いたブラウザの匿名ID そのものをユーザーにして、dip がそのユーザーの状態から
初回訪問か再訪かを決め(/api/talk/open)、画面はそのイベントで /api/event/stream を呼んで AI 側の1通目を受け取る。
ここでは画面と同じ呼び方で、挨拶・ユーザーの分離・会話数の上限・保存した求人を確かめる。
"""

from __future__ import annotations

import json
import re

import pytest

from dip_mcp.data import State
from tests.test_handle import call, reply
from tests.test_revisit import Greeter, greet


@pytest.fixture
def app(tmp_path, monkeypatch):
    """server を一時ディレクトリの状態で動かす。set_model で偽のモデルを差し込む。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # モデルは作るが呼ばない(偽のモデルを差し込まないとき)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    from line_runtime import config, runtime, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)  # 会話数の数(quota.json)・会話を本物の data/ に書かない
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()

    def set_model(model):
        monkeypatch.setattr(server, "LineRuntime", lambda: runtime.LineRuntime(model=model, in_memory=True))

    server.set_model = set_model
    yield server
    del server.set_model


def open_talk(client) -> tuple[dict, dict]:
    """画面を開いたときと同じ: /api/talk/open → そのイベントで /api/event/stream。(open の結果, 最後の返信)"""
    opened = client.post("/api/talk/open").json()
    r = client.post("/api/event/stream", json={"user_id": opened["user"], "event": opened["event"]})
    assert r.status_code == 200
    evs = [json.loads(line) for line in r.text.splitlines()]
    assert evs[0]["kind"] == "start" and evs[0]["event"] == opened["event"]
    return opened, evs[-1]


# ----------------------------- 開くと、初回訪問 / 再訪の挨拶から始まる -----------------------------


def test_first_open_greets_as_a_first_visit_and_reopening_greets_as_a_revisit(app):
    from fastapi.testclient import TestClient

    model = Greeter(script=[greet] * 4)  # 初回訪問 1回 + 再訪 2回(要約を読む → 挨拶)+ 別ブラウザの初回訪問 1回
    app.set_model(model)
    with TestClient(app.app) as client:
        # Cookie なし: 画面を開いた最初の呼び出しで匿名IDが発行され、初回訪問の挨拶から始まる
        assert app.ANON_COOKIE not in client.cookies
        opened, first = open_talk(client)
        uid = client.cookies[app.ANON_COOKIE]
        assert opened == {"user": uid, "event": "visit"}
        assert first["entry"] == "visit" and first["tools"] == []
        assert first["bubbles"] == ["こんにちは。バイト探しをお手伝いします。何をしますか?"]

        # Cookie あり(同じブラウザで開き直す): 同じユーザーの再訪。要約を読んでから挨拶する
        opened, again = open_talk(client)
        assert opened == {"user": uid, "event": "revisit"}
        assert again["entry"] == "revisit" and [t["name"] for t in again["tools"]] == ["get_user_conversation_summary"]
        assert again["bubbles"] == ["おかえりなさい。何をしますか?"]

        # 別のブラウザ(Cookie なし)は別の匿名ユーザーの初回訪問
        client.cookies.clear()  # 別ブラウザ相当(ターンを進めるので lifespan と同じクライアントを使う)
        opened, first_b = open_talk(client)
        assert opened["event"] == "visit" and opened["user"] != uid
        assert first_b["entry"] == "visit"
    assert model.script == []


def test_production_user_is_the_anonymous_id_itself_and_starts_empty(app):
    """本番のユーザーIDは匿名ID そのもの(fixtures 由来のシナリオ用ユーザーではない)。状態は空から始まる。"""
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        client.get("/")
        uid = client.cookies[app.ANON_COOKIE]
        opened = client.post("/api/talk/open").json()
    assert opened["user"] == uid and re.fullmatch(r"w-[0-9a-f]{16}", uid)  # 「匿名ID:sim-…」ではない
    u = app.state.user(uid)
    assert u["dip_id"] is None and u["views"] == u["saves"] == u["applications"] == u["outcomes"] == []
    assert len(u["talk_opens"]) == 1


def test_production_user_cannot_read_or_talk_as_another_user(app):
    """他人のユーザーIDの状態は読めない・その人として話せない(既存の 403 の決まり)。"""
    from fastapi.testclient import TestClient

    with TestClient(app.app) as a:
        b = TestClient(app.app)
        ua = a.post("/api/talk/open").json()["user"]
        ub = b.post("/api/talk/open").json()["user"]
        assert ua != ub
        assert a.get(f"/api/saved/{ua}").status_code == 200
        assert a.get(f"/api/saved/{ub}").status_code == 403
        assert a.get(f"/api/dip/{ub}").status_code == 403
        assert a.post("/api/event/stream", json={"user_id": ub, "event": "revisit"}).status_code == 403
        assert a.post("/api/event", json={"user_id": ub, "event": "message", "text": "hi"}).status_code == 403
        assert a.post(f"/api/voice/{ub}/pull").status_code == 403
        # open は自分の Cookie のユーザーしか開けない(ユーザーIDを受け取らない)
        assert a.post("/api/talk/open", json={"user": ub}).json()["user"] == ua


def test_daily_limit_applies_to_the_production_talk(app, monkeypatch):
    """会話数の上限は本番の / にも効く: 上限を超えた挨拶は LLM を呼ばずに案内だけ返す。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "1")
    model = Greeter(script=[greet])  # 1回目の挨拶だけ
    app.set_model(model)
    with TestClient(app.app) as client:
        _, first = open_talk(client)
        _, limited = open_talk(client)
        uid = client.cookies[app.ANON_COOKIE]
        assert client.app.state.quota.used(uid) == 1
    assert first["entry"] == "visit" and "limited" not in first
    assert limited["limited"] is True and limited["entry"] == "revisit"
    assert model.script == []


# ----------------------------- 保存した求人 -----------------------------


def test_saved_jobs_place_shows_what_this_user_saved_in_the_talk(app):
    """トークで保存した求人が「保存した求人」(/api/saved/{自分})に出る。他のユーザーの一覧には出ない。"""
    from fastapi.testclient import TestClient

    holder: dict = {}

    def save(messages):
        return call("save_job", {"line_user_id": holder["uid"], "job_id": "a003"}, "save")

    model = Greeter(script=[greet, save, reply(["保存しました。"])])
    app.set_model(model)
    with TestClient(app.app) as client:
        opened, _ = open_talk(client)
        holder["uid"] = uid = opened["user"]
        assert client.get(f"/api/saved/{uid}").json() == {"jobs": []}
        r = client.post("/api/event/stream", json={"user_id": uid, "event": "message", "text": "a003 を保存して"})
        assert json.loads(r.text.splitlines()[-1])["bubbles"] == ["保存しました。"]
        jobs = client.get(f"/api/saved/{uid}").json()["jobs"]
        other = TestClient(app.app)
        other_uid = other.post("/api/talk/open").json()["user"]
        assert other.get(f"/api/saved/{other_uid}").json() == {"jobs": []}
    assert [j["job_id"] for j in jobs] == ["a003"]
    assert {"title", "employer", "hourly_wage", "shift", "station", "walk_min", "saved_at"} <= jobs[0].keys()
    assert model.script == []


# ----------------------------- 画面: / はトークだけ、/dev は今までの3列 -----------------------------


def test_root_is_the_talk_only_screen_and_dev_keeps_the_simulator(app):
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        root = client.get("/")
        dev = client.get("/dev")
    assert root.status_code == dev.status_code == 200
    page = root.text
    # dip の状態・構成図・トレース・シナリオ一覧は出さない(呼びもしない)
    for dev_only in ('id="scenarios"', 'id="state"', 'id="trace"', "<svg", "/api/scenarios", "/api/dip/", "restart"):
        assert dev_only not in page, dev_only
    # 開いたら dip に初回訪問か再訪かを聞いて始める。ナビは3つの置き場
    assert "/api/talk/open" in page
    for place in ("トーク", "保存した求人", "プロフィール", "準備中", "/api/saved/"):
        assert place in page, place
    assert 'id="resume"' in page and 'id="voice"' in page  # ヘッダーの2つのボタンは今のまま(置き場を変えるのは次の task)
    # /dev は今までの3列(シナリオの起動・状態・構成図と処理の流れ)
    for part in ('id="scenarios"', 'id="state"', 'id="trace"', "/api/scenarios/", "/api/dip/"):
        assert part in dev.text, part
