"""期間で消す(task 208、docs/design/privacy.md の 3.3)の台本テスト。実 LLM なし、dip の MCP は本物。

画面と同じ呼び方で使い、時計(dip_mcp.retention.clock)を差し替えて、Web プロセスの掃除(起動時・1 日 1 回・内部の口)が
各期間の境目で消す・消さないを確かめる。
- 会話: 最後のやりとりから 30 日はちょうどまで残り、過ぎたら消える。会話の DB は VACUUM され、ファイルに ID が残らない
- 使われなくなった人: 最後の利用から 180 日はちょうどまで残り、過ぎたらシナリオユーザーの行、連携、会話、音声のファイル、
  費用台帳のユーザーIDごと消える(state.json に行が残らない)。途中で使えば、そこから数え直す
- 「すべて消す」で持ち越した行: 部屋の名前は 1 日、行は 180 日で外れる
- last_seen_at の無い行は、最初の掃除の時刻から数える。fixtures のユーザーそのものは消さない
- 費用台帳: 90 日を過ぎた行は日ごとの合計の 1 行になり、合計の金額は変わらない
- Cookie は 180 日で、画面を開くたびに同じ ID で書き直す
- 掃除は進行中のターン(user_lock)を待つ。CLI は共有鍵つきで内部の口を呼ぶだけ
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from dip_mcp import accounts, logic, retention
from dip_mcp.data import State
from line_runtime import ledger, purge
from tests.test_revisit import Greeter, greet
from tests.test_talk_page import WORKER, app, open_talk  # noqa: F401  app はフィクスチャ

T0 = datetime.now(UTC).replace(microsecond=0)
DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)
ROOT_USER = "w-00000000000000a1"  # 架空の匿名ID(画面を通さずに状態を書くテスト用)


@pytest.fixture
def served(app, monkeypatch):  # noqa: F811
    """会話と費用台帳をファイルに書く本物の LineRuntime(VACUUM の後のファイルの中身を確かめるため)と、差し替えた時計。"""
    from line_runtime import runtime

    model = Greeter(script=[greet] * 20)
    monkeypatch.setattr(app, "LineRuntime", lambda: runtime.LineRuntime(model=model))
    clock = {"now": T0}
    monkeypatch.setattr(retention, "clock", lambda: clock["now"])
    from voice_resume import config as voice_config

    monkeypatch.setattr(voice_config, "OUT_DIR", app.config.DATA_DIR / "voice-out")
    app.clock_at = lambda at: clock.__setitem__("now", at)
    yield app
    del app.clock_at


def _client(server):
    from fastapi.testclient import TestClient

    return TestClient(server.app)


def _purge(client) -> dict:
    r = client.post(purge.INTERNAL_PATH, headers=WORKER)
    assert r.status_code == 200, r.text
    return r.json()


def _users_on_disk(server) -> dict:
    return json.loads(server.state.path.read_text(encoding="utf-8"))["users"]


def _links(server) -> dict:
    with sqlite3.connect(accounts.of(server.state).path) as db:
        return dict(db.execute("SELECT user_id, dip_id FROM links").fetchall())


def _db_bytes(server, name: str) -> bytes:
    """data/ のファイルの中身(SQLite の WAL と journal も含める)。"""
    files = [server.config.DATA_DIR / f"{name}{suffix}" for suffix in ("", "-wal", "-journal")]
    return b"".join(f.read_bytes() for f in files if f.is_file())


# ----------------------------- Cookie -----------------------------


def test_cookie_lasts_180_days_and_every_open_rewrites_the_same_id(served):
    with _client(served) as client:
        first = client.get("/")
        uid = first.cookies[served.ANON_COOKIE]
        assert "max-age=15552000" in first.headers["set-cookie"].lower()  # 180 日
        for path in ("/", "/dev"):  # 画面を開くたびに、同じ ID で期限を今日から 180 日に延ばす
            again = client.get(path)
            assert again.cookies[served.ANON_COOKIE] == uid
            assert "max-age=15552000" in again.headers["set-cookie"].lower()
        assert "set-cookie" not in client.post("/api/talk/open").headers  # API は書き直さない


# ----------------------------- 会話(30 日) -----------------------------


def test_conversation_is_kept_until_30_days_after_the_last_exchange_and_then_erased_and_vacuumed(served):
    with _client(served) as client:
        opened, _ = open_talk(client)
        uid = opened["user"]
        last_at = client.portal.call(served.app.state.rt.conversation, uid)["last_at"]
        assert uid.encode() in _db_bytes(served, "line_checkpoints.db")

        served.clock_at(last_at + timedelta(days=30))  # ちょうど 30 日: 残す
        assert _purge(client)["conversations"] == 0
        assert client.get("/api/talk/history").json()["turns"]

        served.clock_at(last_at + timedelta(days=30, microseconds=1))  # 過ぎた: 消す
        assert _purge(client)["conversations"] == 1
        assert client.get("/api/talk/history").json()["turns"] == []
        assert uid.encode() not in _db_bytes(served, "line_checkpoints.db")  # VACUUM したので、空き領域と WAL にも残らない
        assert uid in _users_on_disk(served)  # 会話を消しただけ。ユーザー(180 日)は残る
        assert client.post("/api/talk/open").json()["event"] == "revisit"  # 次は新しい会話から


# ----------------------------- 使われなくなった人(180 日) -----------------------------


def test_user_unused_for_180_days_is_removed_whole_with_scenario_users_links_files_and_ledger_ids(served):
    with _client(served) as client:
        opened, _ = open_talk(client)  # ターン: 最後の利用は T0
        uid = opened["user"]
        assert client.put("/api/profile/items/area", json={"value": "渋谷"}).status_code == 200
        scenario = client.post("/api/scenarios/R-revisit-browsing/start").json()["user"]  # 連携済みのシナリオユーザー
        draft = served.config.DATA_DIR / "voice-out" / "draft.md"
        draft.parent.mkdir(parents=True)
        draft.write_text("# 下書き", encoding="utf-8")
        assert client.post("/api/voice/result", headers=WORKER,
                           json={"user_id": uid, "draft_markdown": "# 下書き", "files": [str(draft)]}).status_code == 200
        assert _users_on_disk(served)[uid][retention.LAST_SEEN] == T0.isoformat()
        assert scenario in _links(served)

        served.clock_at(T0 + timedelta(days=180))  # ちょうど 180 日: 残す
        assert _purge(client)["users"] == 0
        assert {uid, scenario} <= set(_users_on_disk(served))

        served.clock_at(T0 + timedelta(days=180) + SECOND)  # 過ぎた: 持ち越す値も残さず、行を丸ごと外す
        assert _purge(client)["users"] == 1
        assert uid not in served.state.path.read_text(encoding="utf-8")  # state.json に行が残らない(#189 のレビュー)
        assert scenario not in _links(served)
        assert not draft.exists()
        for name in ("line_checkpoints.db", "costs.jsonl", "dip_accounts.db"):
            assert uid.encode() not in _db_bytes(served, name), name
        assert "sim-browsing" in _users_on_disk(served)  # fixtures のユーザーそのものは消さない
        assert client.get("/api/profile").json()["sections"][1]["items"][0]["value"] is None  # 戻っても新規


def test_removing_a_user_also_erases_newer_conversations_memory_and_link_tokens(served):
    """180 日を過ぎた人に 30 日より新しい会話が残っていても(最後の利用に数えないプッシュの会話など)、人と一緒に消える。
    Web プロセスのメモリ(未読のアップロード、会話の番号)と、bot が出した linkToken の行も残さない。"""
    with _client(served) as client:
        served.clock_at(T0 - timedelta(days=181))  # 最後の利用は 181 日前。会話そのものは今(実際の時刻)
        uid = open_talk(client)[0]["user"]
        rt = served.app.state.rt
        served.app.state.unread_uploads[uid] = {"file": "履歴書(PDF)", "shown": [], "empty": True}
        accounts.of(served.state).record_link_token(uid, "fake-link-token-0001")
        assert client.portal.call(rt.current_thread, uid) == 1

        served.clock_at(T0)
        out = _purge(client)
        assert out["conversations"] == 0 and out["users"] == 1  # 会話は 30 日の掃除ではなく、人と一緒に消えた
        assert uid not in rt._threads and uid not in served.app.state.unread_uploads
        for name in ("line_checkpoints.db", "dip_accounts.db"):
            assert uid.encode() not in _db_bytes(served, name), name


def test_any_use_restarts_the_180_days(served):
    with _client(served) as client:
        uid = open_talk(client)[0]["user"]
        served.clock_at(T0 + timedelta(days=100))
        assert client.put("/api/profile/items/area", json={"value": "渋谷"}).status_code == 200  # プロフィールの操作
        served.clock_at(T0 + timedelta(days=181))
        assert _purge(client)["users"] == 0
        served.clock_at(T0 + timedelta(days=280))
        assert _purge(client)["users"] == 0
        served.clock_at(T0 + timedelta(days=280) + SECOND)
        assert _purge(client)["users"] == 1
        assert uid not in _users_on_disk(served)


def test_turns_profile_and_voice_results_record_the_last_use(served):
    with _client(served) as client:
        uid = open_talk(client)[0]["user"]
        for days, act in ((1, lambda: client.post("/api/event/stream", json={"user_id": uid, "event": "revisit"})),
                          (2, lambda: client.delete("/api/profile/items/area")),
                          (3, lambda: client.post("/api/voice/result", headers=WORKER,
                                                  json={"user_id": uid, "draft_markdown": "# 下書き"}))):
            served.clock_at(T0 + timedelta(days=days))
            assert act().status_code == 200
            assert _users_on_disk(served)[uid][retention.LAST_SEEN] == (T0 + timedelta(days=days)).isoformat()


# ----------------------------- 「すべて消す」で持ち越した行 -----------------------------


def test_rows_kept_on_forget_lose_the_rooms_after_1_day_and_the_row_after_180_days(served):
    with _client(served) as client:
        uid = open_talk(client)[0]["user"]
        logic.record_voice_room(served.state, uid, "aica-room-1")  # 消す前に始めた面談の部屋
        client.post("/api/voice/result", headers=WORKER, json={"user_id": uid, "draft_markdown": "# 下書き"})
        served.clock_at(T0 + DAY)
        assert client.post("/api/profile/forget").status_code == 200
        row = _users_on_disk(served)[uid]
        forgot = T0 + DAY
        assert row["forgotten_voice_rooms"] == ["aica-room-1"] and row[retention.LAST_SEEN] == forgot.isoformat()
        late = {"user_id": uid, "room": "aica-room-1", "draft_markdown": "# 下書き"}

        served.clock_at(forgot + DAY)  # ちょうど 1 日: 部屋の名前を残す(消す前の面談の結果を断る)
        assert _purge(client)["forgotten_rooms"] == 0
        assert client.post("/api/voice/result", headers=WORKER, json=late).status_code == 410

        served.clock_at(forgot + DAY + SECOND)  # 過ぎた: 部屋の名前を外す。連番は行と一緒に残す
        assert _purge(client)["forgotten_rooms"] == 1
        row = _users_on_disk(served)[uid]
        assert "forgotten_voice_rooms" not in row and row["voice_result_seq"] == 1

        served.clock_at(forgot + timedelta(days=180))
        assert _purge(client)["users"] == 0 and uid in _users_on_disk(served)
        served.clock_at(forgot + timedelta(days=180) + SECOND)
        assert _purge(client)["users"] == 1 and uid not in _users_on_disk(served)


# ----------------------------- last_seen_at の無い行、fixtures のユーザー -----------------------------


def test_rows_without_last_seen_count_from_the_first_purge_and_fixture_users_are_never_removed(served):
    served.state.update(ROOT_USER, lambda u: u.__setitem__("preferences", {"area": {"value": "渋谷", "at": "x"}}))
    served.state.update(f"{ROOT_USER}:sim-new", lambda u: None)
    with _client(served) as client:  # 起動時の掃除が、最初に見つけた時刻を書く
        users = _users_on_disk(served)
        assert users[ROOT_USER][retention.LAST_SEEN] == T0.isoformat()
        assert users[f"{ROOT_USER}:sim-new"][retention.LAST_SEEN] == T0.isoformat()
        assert retention.LAST_SEEN not in users["sim-new"]

        served.clock_at(T0 + timedelta(days=180))
        assert _purge(client)["users"] == 0
        served.clock_at(T0 + timedelta(days=180) + SECOND)
        assert _purge(client)["users"] == 1
        users = _users_on_disk(served)
        assert ROOT_USER not in users and f"{ROOT_USER}:sim-new" not in users
        served.clock_at(T0 + timedelta(days=3650))
        _purge(client)
        assert {"sim-new", "sim-exit", "sim-browsing"} <= set(_users_on_disk(served))


# ----------------------------- 費用台帳(90 日) -----------------------------


def test_ledger_rows_older_than_90_days_become_one_daily_total_without_user_ids(tmp_path):
    path = tmp_path / "costs.jsonl"
    now = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)  # 日本時間の 12:00
    cutoff = now - ledger.ID_TTL
    rows = [{"at": a.isoformat(), "user_id": u, "event": "message", "model": "gpt-6-sol", "input_tokens": 100,
             "output_tokens": 10, "cache_read": 0, "cache_write": 0, "cost_usd": 0.25}
            for a, u in ((cutoff - timedelta(hours=2), "w-00000000000000b1"),
                         (cutoff - SECOND, "w-00000000000000b2:sim-new"),
                         (cutoff - DAY, "w-00000000000000b1"),
                         (cutoff, "w-00000000000000b3"))]  # ちょうど 90 日: ユーザーIDを残す
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    before = ledger.summarize(ledger.load(path))
    led = ledger.Ledger(path)

    assert led.fold_old(now) == 3
    after = ledger.load(path)
    assert [r["user_id"] for r in after] == [ledger.FOLDED, ledger.FOLDED, "w-00000000000000b3"]
    assert [r["calls"] for r in after[:2]] == [1, 2]  # 日本時間の日ごとに 1 行
    assert "w-00000000000000b1" not in path.read_text(encoding="utf-8")
    assert "w-00000000000000b2" not in path.read_text(encoding="utf-8")
    total = lambda s: (sum(x["calls"] for x in s), round(sum(x["cost_usd"] for x in s), 6))
    assert total(ledger.summarize(after)) == total(before)  # 金額と回数は変わらない
    assert led.fold_old(now) == 0 and ledger.load(path) == after  # 2 回目は何も変えない

    led.record("w-00000000000000b3", "message", "gpt-6-sol", {"input_tokens": 1, "output_tokens": 1})
    led.fold_old(now + 2 * ledger.ID_TTL)  # 前にまとめた日の行も、まとめ直して日ごとに 1 行
    days = [ledger.today(datetime.fromisoformat(r["at"])) for r in ledger.load(path)]
    assert len(days) == len(set(days)) and all(r["user_id"] == ledger.FOLDED for r in ledger.load(path))
    assert led.rows and all(r["user_id"] == ledger.FOLDED for r in led.rows)  # メモリの行も同じ


# ----------------------------- いつ、どこで走るか -----------------------------


def test_purge_runs_at_startup_and_then_every_day(served, monkeypatch):
    served.state.update(ROOT_USER, lambda u: u.__setitem__(retention.LAST_SEEN, (T0 - timedelta(days=181)).isoformat()))
    runs = []
    real = purge.purge

    async def counted(*args, **kwargs):
        runs.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(purge, "purge", counted)
    monkeypatch.setattr(purge, "EVERY", timedelta(seconds=0.05))
    with _client(served):
        assert ROOT_USER not in _users_on_disk(served)  # 起動時の掃除で、もう消えている
        deadline = time.monotonic() + 10
        while len(runs) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
    assert len(runs) >= 3  # 起動時 + 定期


def test_purge_waits_for_the_users_turn_to_finish(served):
    with _client(served) as client:
        uid = open_talk(client)[0]["user"]
        served.clock_at(T0 + timedelta(days=181))
        release, held = threading.Event(), threading.Event()

        async def turn_in_progress():
            async with served.app.state.rt.user_lock(uid):
                held.set()
                await asyncio.to_thread(release.wait, 10)

        client.portal.start_task_soon(turn_in_progress)
        assert held.wait(10)
        result: dict = {}
        worker = threading.Thread(target=lambda: result.update(_purge(client)))
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive() and uid in _users_on_disk(served)  # ターンが終わるまで消さない
        release.set()
        worker.join(10)
        assert not worker.is_alive() and result["users"] == 1 and uid not in _users_on_disk(served)


def test_internal_purge_needs_the_shared_key(served):
    with _client(served) as client:
        assert client.post(purge.INTERNAL_PATH).status_code == 401
        assert client.post(purge.INTERNAL_PATH, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert set(_purge(client)) == {"conversations", "users", "forgotten_rooms", "ledger_rows"}


def test_cli_only_calls_the_internal_endpoint_of_the_web_process(served, monkeypatch, capsys):
    import httpx

    with _client(served) as client:
        sent = {}

        def post(url, headers, timeout):
            sent.update(url=url, headers=headers)
            return client.post(url.removeprefix("http://aica.example"), headers=headers)

        monkeypatch.setattr(httpx, "post", post)
        assert purge.main(["--url", "http://aica.example/"]) == 0
        assert sent["url"] == "http://aica.example" + purge.INTERNAL_PATH
        assert json.loads(capsys.readouterr().out)["users"] == 0
        monkeypatch.delenv("AICA_INTERNAL_KEY")
        monkeypatch.setattr(served.config, "load_env", lambda: None)
        assert purge.main([]) == 2  # 鍵が無ければ呼ばない


def test_state_rows_are_grouped_by_person_for_the_period(tmp_path, monkeypatch):
    """シナリオユーザーの行が新しければ、その人の行も残す(期間は人ごとに、いちばん新しい最後の利用で数える)。"""
    state = State(tmp_path / "state.json")
    state.reset()
    clock = {"now": T0}
    monkeypatch.setattr(retention, "clock", lambda: clock["now"])
    retention.touch(state, ROOT_USER)
    clock["now"] = T0 + timedelta(days=100)
    retention.touch(state, f"{ROOT_USER}:sim-new")
    retention.touch(state, "sim-new")  # fixtures のユーザーそのものには書かない
    assert retention.LAST_SEEN not in state.user("sim-new")
    clock["now"] = T0 + timedelta(days=181)
    assert retention.sweep(state)["expired"] == {}
    clock["now"] = T0 + timedelta(days=281)
    assert retention.sweep(state)["expired"] == {ROOT_USER: [ROOT_USER, f"{ROOT_USER}:sim-new"]}
    assert retention.still_expired(state, [ROOT_USER, "w-00000000000000a2"]) == [ROOT_USER]
    out = retention.remove_expired(state, [ROOT_USER])
    assert out["people"] == [ROOT_USER] and out["users"] == [ROOT_USER, f"{ROOT_USER}:sim-new"]
