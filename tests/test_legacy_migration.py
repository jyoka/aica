"""旧い名前のデータの移行(provider_mcp/legacy.py)。実際のファイルを一時ディレクトリに置き、起動の入口から通す。

旧い接頭辞は架空の語(acme)。旧い形のファイルは、この中の SQL と JSON で作る(コードの中に旧い形を残さないので、ここにだけ書く)。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from provider_mcp import accounts, behavior, legacy, profile
from provider_mcp.data import State

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "acme"
USER = "l-linked"
UNLINKED = "l-unlinked"
MEMBER = "D9001"
PASSWORD = "secret-pass-9001"
AS_OF = "2026-10-01T12:00:00+09:00"
PREFERENCES = {"area": {"value": "渋谷", "at": "2026-09-30T10:00:00+09:00"}}

ACCOUNTS_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE logins (acme_id TEXT PRIMARY KEY, login_id TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL);
CREATE TABLE login_failures (login_id TEXT PRIMARY KEY, failures INTEGER NOT NULL, last_failed_at TEXT NOT NULL, locked_until TEXT);
CREATE TABLE nonces (nonce_hash TEXT PRIMARY KEY, acme_id TEXT NOT NULL, link_token_hash TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE link_tokens (link_token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE links (user_id TEXT PRIMARY KEY, acme_id TEXT NOT NULL, linked_at TEXT NOT NULL);
"""
EVENTS_SQL = """
CREATE TABLE members (acme_id TEXT PRIMARY KEY, registered_at TEXT NOT NULL);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    acme_id TEXT NOT NULL REFERENCES members(acme_id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    job_id TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX events_by_member ON events(acme_id, at);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""
SUMMARIES_SQL = """
CREATE TABLE summaries (acme_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, through_seq INTEGER NOT NULL, summary TEXT NOT NULL);
"""


def _summary() -> dict:
    return {"acme_id": MEMBER, "as_of": AS_OF, "through_seq": 2, "last_visit": "2026-09-30T10:00:00+09:00", "visits_30d": 1,
            "recent_views": [{"job_id": "b0101", "at": "2026-09-30T10:01:00+09:00"}],
            "views_30d": {"count": 1, "by_job_type": {"カフェ": 1}},
            "applications": [{"job_id": "b0102", "applied_at": "2026-09-29T10:00:00+09:00", "status": "応募済み",
                              "status_at": "2026-09-29T10:00:00+09:00"}],
            "applied_job_ids": ["b0102"], "saves": [], "saves_30d": 0, "saves_total": 0, "form_exit": None,
            "last_contact": None, "tendency": {}}


def _old_folder(folder: Path) -> Path:
    """旧い名前のファイル一式(会員のログイン、連携、行動のイベントと要約、state の中の書いた分、チェックポイント)。"""
    folder.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(folder / "acme_accounts.db")) as db, db:
        db.executescript(ACCOUNTS_SQL)
        db.execute("INSERT INTO meta VALUES ('seeded', '2026-09-01T00:00:00+00:00')")
        db.execute("INSERT INTO logins VALUES (?, ?, ?)", (MEMBER, f"member@{PREFIX}.test", accounts.hash_password(PASSWORD)))
        db.execute("INSERT INTO links VALUES (?, ?, ?)", (USER, MEMBER, "2026-09-01T00:00:00+00:00"))
    with closing(sqlite3.connect(folder / "acme_events.db")) as db, db:
        db.executescript(EVENTS_SQL)
        db.execute("INSERT INTO members VALUES (?, ?)", (MEMBER, "2026-01-01T00:00:00+09:00"))
        db.execute("INSERT INTO events (acme_id, at, kind, job_id, data) VALUES (?, ?, 'view', 'b0101', '{}')",
                   (MEMBER, "2026-09-30T10:01:00+09:00"))
        db.execute("INSERT INTO events (acme_id, at, kind, job_id, data) VALUES (?, ?, 'apply', 'b0102', ?)",
                   (MEMBER, "2026-09-29T10:00:00+09:00", json.dumps({"status": "応募済み"}, ensure_ascii=False)))
    with closing(sqlite3.connect(folder / "acme_summaries.db")) as db, db:
        db.executescript(SUMMARIES_SQL)
        db.execute("INSERT INTO summaries VALUES (?, ?, ?, ?)",
                   (MEMBER, AS_OF, 2, json.dumps(_summary(), ensure_ascii=False, separators=(",", ":"))))
    (folder / "acme_events.lock").write_text("", encoding="utf-8")
    writes = [
        # 要約より後に、連携中に書いた保存(through_seq より後の番号で、見えるはず)
        {"seq": 3, "at": "2026-10-02T09:00:00+09:00", "kind": "save", "job_id": "b0103", "data": {}, "acme_id": MEMBER},
        # 連携する前に書いた分(会員 None)と、前の会員に書いた分(重ねない)
        {"seq": None, "at": "2026-10-02T09:30:00+09:00", "kind": "save", "job_id": "b0104", "data": {}, "acme_id": None},
        {"seq": 9, "at": "2026-10-02T09:40:00+09:00", "kind": "save", "job_id": "b0199", "data": {}, "acme_id": "D0000"},
    ]
    state = {"users": {
        USER: {"line_user_id": USER, "acme_writes": writes, "preferences": PREFERENCES, "conversation_summary": "カフェを探している"},
        UNLINKED: {"line_user_id": UNLINKED, "acme_writes": [
            {"seq": None, "at": "2026-10-02T09:00:00+09:00", "kind": "save", "job_id": "b0105", "data": {}, "acme_id": None}]},
    }}
    (folder / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_checkpoints(folder / "line_checkpoints.db")
    return folder


def _write_checkpoints(path: Path) -> None:
    """本物の LangGraph のチェックポイントのファイル(ランタイムが使うのと同じ保存の形)。"""
    import asyncio

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def run():
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            cp = empty_checkpoint()
            cp["channel_values"] = {"messages": [
                HumanMessage(content="カフェを探しています"),
                AIMessage(content="", tool_calls=[{"name": "recommend_jobs", "args": {"line_user_id": USER}, "id": "c1", "type": "tool_call"}]),
                ToolMessage(content=json.dumps({"acme_id": MEMBER, "jobs": []}), tool_call_id="c1", name="recommend_jobs"),
                AIMessage(content="渋谷のカフェを見てみましょう"),
            ], "line_user_id": USER}
            cfg = {"configurable": {"thread_id": f"{USER}:1", "checkpoint_ns": ""}}
            await saver.aput(cfg, cp, {"source": "loop", "step": 1}, {})

    asyncio.run(run())


def _read_checkpoint_messages(path: Path) -> list[str]:
    import asyncio

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def run():
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            tup = await saver.aget_tuple({"configurable": {"thread_id": f"{USER}:1"}})
            return [m.content for m in tup.checkpoint["channel_values"]["messages"]]

    return asyncio.run(run())


def _snapshot(state: State) -> dict:
    """移した後に読む結果(ログイン、連携、行動、書いた分)。"""
    acc = accounts.of(state)
    return {
        "login": acc.login(f"member@{legacy.NEW}.test", PASSWORD),
        "bad": acc.login(f"member@{legacy.NEW}.test", "wrong"),
        "linked": behavior.provider_id(state, USER),
        "unlinked": behavior.provider_id(state, UNLINKED),
        "view": behavior.view(state, USER),
        "view_unlinked": behavior.view(state, UNLINKED),
        "writes": state.user(USER)[behavior.WRITES],
    }


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.delenv(legacy.ENV, raising=False)
    behavior._open.cache_clear()
    yield _old_folder(tmp_path / "data")
    behavior._open.cache_clear()


def _migrated(folder: Path, monkeypatch) -> State:
    monkeypatch.setenv(legacy.ENV, PREFIX)
    legacy.prepare(folder / "state.json")
    return State(folder / "state.json")


def _bytes(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file() and p.name != legacy.LOCK_FILE}


def _columns(path: Path, table: str) -> list[str]:
    with closing(sqlite3.connect(path)) as db:
        return [r[1] for r in db.execute(f"PRAGMA table_info({table})")]


def test_the_same_results_are_read_after_the_move(folder, monkeypatch):
    state = _migrated(folder, monkeypatch)
    got = _snapshot(state)

    assert got["login"] == {"provider_id": MEMBER}  # ハッシュは変わらないので、同じパスワードで入れる
    assert got["bad"] == {"error": "bad"}
    assert got["linked"] == MEMBER
    assert got["unlinked"] is None
    view = got["view"]
    assert view["provider_id"] == MEMBER
    assert view["as_of"] == AS_OF
    assert view["applied_job_ids"] == ["b0102"]  # 要約から
    assert [s["job_id"] for s in view["saves"]] == ["b0104", "b0103"]  # 書いた分を重ねる。前の会員の分は重ねない
    assert view["recent_views"] == _summary()["recent_views"]
    assert [s["job_id"] for s in got["view_unlinked"]["saves"]] == ["b0105"]
    assert [w["provider_id"] for w in got["writes"]] == [MEMBER, None, "D0000"]
    assert state.user(USER)["preferences"] == PREFERENCES  # ほかの値はそのまま


def test_old_names_are_gone_and_new_files_have_the_new_columns(folder, monkeypatch):
    _migrated(folder, monkeypatch)
    assert not list(folder.glob("acme*"))
    for path, table in ((folder / "provider_accounts.db", "links"), (folder / "provider_accounts.db", "logins"),
                        (folder / "provider_accounts.db", "nonces"), (folder / "provider_events.db", "members"),
                        (folder / "provider_events.db", "events"), (folder / "provider_summaries.db", "summaries")):
        assert "provider_id" in _columns(path, table)
        assert not [c for c in _columns(path, table) if c.startswith(PREFIX)]
    assert PREFIX not in (folder / "state.json").read_text(encoding="utf-8")
    assert (folder / "provider_events.lock").exists()


def test_the_event_log_is_still_append_only_and_keeps_its_rows(folder, monkeypatch):
    state = _migrated(folder, monkeypatch)
    events, _ = behavior.stores(state)
    assert events.member_ids() == [MEMBER]
    seq = events.append(MEMBER, "visit", datetime(2026, 10, 3, tzinfo=UTC))
    assert seq == 3
    with closing(sqlite3.connect(folder / "provider_events.db")) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE events SET kind = 'x'")
    with closing(sqlite3.connect(folder / "provider_events.db")) as db:
        db.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):  # 会員の表への外部キーも新しい列を指す
            db.execute("INSERT INTO events (provider_id, at, kind) VALUES ('nobody', 'x', 'visit')")


def test_old_json_keys_inside_the_append_only_log_are_also_moved(folder, monkeypatch):
    """イベントの data に接頭辞のキーがある行も直す。追記だけのトリガーは外して戻す。"""
    with closing(sqlite3.connect(folder / "acme_events.db")) as db, db:
        db.execute("INSERT INTO events (acme_id, at, kind, data) VALUES (?, 'x', 'contact', ?)", (MEMBER, '{"acme_note":"あ"}'))
    _migrated(folder, monkeypatch)
    with closing(sqlite3.connect(folder / "provider_events.db")) as db:
        assert db.execute("SELECT data FROM events WHERE kind = 'contact'").fetchone()[0] == '{"provider_note":"あ"}'
        triggers = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert triggers == {"events_no_update", "events_no_delete"}


def test_running_twice_changes_nothing(folder, monkeypatch):
    state = _migrated(folder, monkeypatch)
    before = _bytes(folder)
    assert legacy.prepare(folder / "state.json") == {"files": 0, "json": 0}
    assert _bytes(folder) == before
    assert _snapshot(state)["linked"] == MEMBER


def test_the_conversation_checkpoints_stay_readable_and_untouched(folder, monkeypatch):
    before = (folder / "line_checkpoints.db").read_bytes()
    _migrated(folder, monkeypatch)
    assert (folder / "line_checkpoints.db").read_bytes() == before
    assert _read_checkpoint_messages(folder / "line_checkpoints.db") == [
        "カフェを探しています", "", json.dumps({"acme_id": MEMBER, "jobs": []}), "渋谷のカフェを見てみましょう"]


def test_a_failure_at_any_step_can_be_continued(folder, monkeypatch):
    """移す 1 歩ごとに止めて(新しい接続、名前の変更、JSON の書き込み)、もう一度動かすと、全部を通したときと同じになる。"""
    good = folder.parent / "good"
    shutil.copytree(folder, good)
    monkeypatch.setenv(legacy.ENV, PREFIX)
    legacy.prepare(good / "state.json")
    want = _snapshot(State(good / "state.json"))
    behavior._open.cache_clear()

    real_move, real_write, real_sqlite = legacy._move, legacy._write_json, legacy._migrate_sqlite
    steps = 0

    def counted(real):
        def run(*a):
            nonlocal steps
            steps += 1
            if steps == stop_at:
                raise OSError("disk went away")
            return real(*a)
        return run

    monkeypatch.setattr(legacy, "_move", counted(real_move))
    monkeypatch.setattr(legacy, "_write_json", counted(real_write))
    monkeypatch.setattr(legacy, "_migrate_sqlite", counted(real_sqlite))
    tried = 0
    for stop_at in range(1, 30):
        steps = 0
        work = folder.parent / f"work{stop_at}"
        shutil.copytree(folder, work)
        try:
            legacy.prepare(work / "state.json")
        except OSError:
            tried += 1
            assert (work / legacy.IN_PROGRESS).exists()  # 止まった印が残る
            monkeypatch.delenv(legacy.ENV)
            with pytest.raises(legacy.MigrationError, match="途中で止まって"):  # 環境変数を消しても、半分のデータでは動かない
                legacy.prepare(work / "state.json")
            monkeypatch.setenv(legacy.ENV, PREFIX)
            steps = 10_000  # 次は止めない
            legacy.prepare(work / "state.json")
        else:
            break  # 全部の歩を通った
        assert not (work / legacy.IN_PROGRESS).exists()
        assert not list(work.glob("acme*"))
        behavior._open.cache_clear()
        assert _snapshot(State(work / "state.json")) == want
        behavior._open.cache_clear()
    assert tried >= 5  # SQLite 3 つの中身と名前、ほかのファイル、JSON


def test_a_failure_inside_a_sqlite_transaction_leaves_it_as_it_was(folder, monkeypatch):
    real = legacy._rewrite_json_columns

    def boom(db, table, prefix):
        real(db, table, prefix)
        raise OSError("disk went away")

    monkeypatch.setattr(legacy, "_rewrite_json_columns", boom)
    monkeypatch.setenv(legacy.ENV, PREFIX)
    with pytest.raises(OSError):
        legacy.prepare(folder / "state.json")
    assert "acme_id" in _columns(folder / "acme_accounts.db", "links")  # 列の名前も元のまま
    monkeypatch.undo()
    monkeypatch.setenv(legacy.ENV, PREFIX)
    state = State(folder / "state.json")
    legacy.prepare(state.path)
    assert accounts.of(state).provider_id(USER) == MEMBER


def test_without_the_variable_the_old_data_stops_the_start_and_changes_nothing(folder):
    before = _bytes(folder)
    with pytest.raises(legacy.MigrationError, match="空のデータで動き始めない"):
        legacy.prepare(folder / "state.json")
    assert _bytes(folder) == before


def test_the_commands_stop_with_a_reason_when_the_variable_is_missing(folder):
    """起動の入口(コンテナの起動の最初のコマンドと MCP サーバー)を、本当のプロセスで動かす。Web プロセスは下の別のテスト。"""
    env = {"PATH": "/usr/bin:/bin", "PROVIDER_STATE_PATH": str(folder / "state.json")}
    before = _bytes(folder)
    for args in (["-m", "provider_mcp.legacy"], ["-m", "provider_mcp.server", "--http", "18999"]):
        done = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, check=False)
        assert done.returncode == 1, done.stderr
        assert "空のデータで動き始めない" in done.stderr
        assert legacy.ENV in done.stderr
        assert done.stdout == ""  # MCP の stdio を汚さない
    assert _bytes(folder) == before


def test_the_web_process_stops_before_listening_when_the_variable_is_missing(folder):
    code = "from line_runtime import server; server.main()"
    env = {"PATH": "/usr/bin:/bin", "PROVIDER_STATE_PATH": str(folder / "state.json"), "LINE_SIM_DATA_DIR": str(folder)}
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=90,
                          stdin=subprocess.DEVNULL, check=False)
    assert done.returncode == 1, done.stderr[-500:]
    assert "空のデータで動き始めない" in done.stderr


def test_the_container_moves_the_data_before_it_starts_anything():
    start = (ROOT / "scripts" / "container_start.sh").read_text(encoding="utf-8")
    at = start.index("python -m provider_mcp.legacy")
    assert at < start.index("voice_resume.agent")
    assert at < start.index("provider_mcp.server")
    assert at < start.index("exec python -m line_runtime.server")


def test_the_prefix_is_not_written_in_the_blueprint():
    assert legacy.ENV not in (ROOT / "render.yaml").read_text(encoding="utf-8")


def test_a_wrong_prefix_does_not_start_with_empty_data(folder, monkeypatch):
    monkeypatch.setenv(legacy.ENV, "other")
    before = _bytes(folder)
    with pytest.raises(legacy.MigrationError, match="正しい値"):
        legacy.prepare(folder / "state.json")
    assert _bytes(folder) == before


@pytest.mark.parametrize("value", ["provider", "../x", "A B", "a;drop", "1a"])
def test_a_prefix_that_cannot_be_a_name_is_refused(folder, monkeypatch, value):
    monkeypatch.setenv(legacy.ENV, value)
    with pytest.raises(legacy.MigrationError, match=legacy.ENV):
        legacy.prepare(folder / "state.json")


def test_a_trailing_underscore_in_the_prefix_is_fine(folder, monkeypatch):
    monkeypatch.setenv(legacy.ENV, f"{PREFIX}_")
    legacy.prepare(folder / "state.json")
    assert (folder / "provider_accounts.db").exists()


def test_another_files_prefix_does_not_touch_the_data(folder, monkeypatch):
    """line も値の形としては通るが、旧いデータの名前ではない。会話のファイルや line_user_id を動かさない。"""
    (folder / "line_events.json").write_text('{"line_x": 1}', encoding="utf-8")
    before = _bytes(folder)
    monkeypatch.setenv(legacy.ENV, "line")
    with pytest.raises(legacy.MigrationError, match="正しい値"):
        legacy.prepare(folder / "state.json")
    assert _bytes(folder) == before


def test_another_files_prefix_after_the_move_does_nothing(folder, monkeypatch):
    state = _migrated(folder, monkeypatch)
    before = _bytes(folder)
    monkeypatch.setenv(legacy.ENV, "line")
    assert legacy.prepare(state.path) == {"files": 0, "json": 0}
    assert _bytes(folder) == before


def test_a_different_value_after_a_failure_does_not_clear_the_mark(folder, monkeypatch):
    """途中で止まった後に値を打ち間違えても、印を消さず、半分のデータで動き出さない。"""
    real = legacy._move
    monkeypatch.setattr(legacy, "_move", lambda *a: (_ for _ in ()).throw(OSError("disk went away")))
    monkeypatch.setenv(legacy.ENV, PREFIX)
    with pytest.raises(OSError):
        legacy.prepare(folder / "state.json")
    monkeypatch.setattr(legacy, "_move", real)
    monkeypatch.setenv(legacy.ENV, "acmee")
    with pytest.raises(legacy.MigrationError, match="値が違います"):
        legacy.prepare(folder / "state.json")
    assert (folder / legacy.IN_PROGRESS).exists()
    monkeypatch.delenv(legacy.ENV)
    with pytest.raises(legacy.MigrationError, match="途中で止まって"):
        legacy.prepare(folder / "state.json")
    monkeypatch.setenv(legacy.ENV, PREFIX)
    legacy.prepare(folder / "state.json")
    assert accounts.of(State(folder / "state.json")).provider_id(USER) == MEMBER


def test_a_notice_of_a_link_change_waiting_in_the_state_still_asks_to_read_again(folder, monkeypatch):
    """まだ AI に渡していない知らせの項目名は、コードが比べる値。移した後も、読み直しの指示が出る。"""
    data = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    change = {"at": "2026-10-02T09:00:00+09:00", "action": "つないだ", "what": f"{PREFIX} の会員との連携"}
    data["users"][USER]["profile_changes"] = [change]
    (folder / "state.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    state = _migrated(folder, monkeypatch)
    changes = state.user(USER)["profile_changes"]
    assert changes[0]["what"] == profile.LINK_LABEL
    text = profile.notice(state, USER, changes)
    assert "アカウント連携で" in text and "get_user_conversation_summary を読み直す" in text and PREFIX not in text


def test_failure_counts_on_the_old_domain_do_not_block_the_move(folder, monkeypatch):
    """ログインに失敗した数は、会員がいない ID でも数える。新旧のドメインの行が両方あっても止まらず、期限つきの数は移さない。"""
    with closing(sqlite3.connect(folder / "acme_accounts.db")) as db, db:
        for login in (f"x@{PREFIX}.test", "x@provider.test"):
            db.execute("INSERT INTO login_failures VALUES (?, 1, '2026-09-01T00:00:00+00:00', NULL)", (login,))
    _migrated(folder, monkeypatch)
    with closing(sqlite3.connect(folder / "provider_accounts.db")) as db:
        assert db.execute("SELECT login_id FROM login_failures").fetchall() == [("x@provider.test",)]


def test_a_login_id_that_collides_stops_without_changing_anything(folder, monkeypatch):
    with closing(sqlite3.connect(folder / "acme_accounts.db")) as db, db:
        db.execute("INSERT INTO logins VALUES ('D9002', 'member@provider.test', 'x')")
    monkeypatch.setenv(legacy.ENV, PREFIX)
    with pytest.raises(legacy.MigrationError, match="ぶつかりました"):
        legacy.prepare(folder / "state.json")
    assert "acme_id" in _columns(folder / "acme_accounts.db", "logins")


def test_a_json_file_with_the_old_prefix_is_moved_and_fixed_once(folder, monkeypatch):
    (folder / "acme_extra.json").write_text('{"acme_x": 1}', encoding="utf-8")
    _migrated(folder, monkeypatch)
    assert not (folder / "acme_extra.json").exists()
    assert json.loads((folder / "provider_extra.json").read_text(encoding="utf-8")) == {"provider_x": 1}


def test_both_old_and_new_files_stop_without_losing_either(folder, monkeypatch):
    accounts.Accounts(folder / "provider_accounts.db")  # 新しい名前のファイルが先にある
    before = _bytes(folder)
    monkeypatch.setenv(legacy.ENV, PREFIX)
    with pytest.raises(legacy.MigrationError, match="両方あります"):
        legacy.prepare(folder / "state.json")
    assert _bytes(folder) == before


def test_an_empty_new_lock_does_not_block_the_move(folder, monkeypatch):
    (folder / "provider_events.lock").write_text("", encoding="utf-8")
    _migrated(folder, monkeypatch)
    assert not (folder / "acme_events.lock").exists()


def test_an_empty_new_file_does_not_replace_the_old_data(folder, monkeypatch):
    """空の新しいファイルがあっても、中身のある旧いファイルを捨てない。"""
    (folder / "provider_accounts.db").write_bytes(b"")
    state = _migrated(folder, monkeypatch)
    assert accounts.of(state).provider_id(USER) == MEMBER


def test_a_first_start_makes_new_data_so_the_next_start_is_not_refused(tmp_path, monkeypatch):
    monkeypatch.delenv(legacy.ENV, raising=False)
    state_path = tmp_path / "fresh" / "state.json"
    assert legacy.prepare(state_path) is None
    assert (state_path.parent / "provider_accounts.db").exists()
    State(state_path)._write({"users": {}})  # 使い始めて state.json ができた後の起動
    assert legacy.prepare(state_path) is None


def test_new_data_starts_without_the_variable(folder, monkeypatch):
    _migrated(folder, monkeypatch)
    monkeypatch.delenv(legacy.ENV)
    assert legacy.prepare(folder / "state.json") is None


def test_state_with_old_keys_and_no_databases_moves_and_starts(tmp_path, monkeypatch):
    """SQLite がまだ無くても、JSON を移せたら止めない(値は合っている)。"""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"users": {"u": {"acme_writes": []}}}), encoding="utf-8")
    monkeypatch.setenv(legacy.ENV, PREFIX)
    assert legacy.prepare(path) == {"files": 0, "json": 1}
    assert json.loads(path.read_text(encoding="utf-8")) == {"users": {"u": {"provider_writes": []}}}
    assert (tmp_path / "provider_accounts.db").exists()


def test_a_broken_state_file_stops_the_move_before_anything_changes(folder, monkeypatch):
    (folder / "state.json").write_text("{", encoding="utf-8")
    before = _bytes(folder)
    monkeypatch.setenv(legacy.ENV, PREFIX)
    with pytest.raises(legacy.MigrationError, match="JSON"):
        legacy.prepare(folder / "state.json")
    assert _bytes(folder) == before


def test_other_json_files_are_moved_too_and_broken_ones_are_left(folder, monkeypatch):
    (folder / "quota.json").write_text('{"acme_x": 1}', encoding="utf-8")
    (folder / "line_events.json").write_text("not json", encoding="utf-8")
    _migrated(folder, monkeypatch)
    assert json.loads((folder / "quota.json").read_text(encoding="utf-8")) == {"provider_x": 1}
    assert (folder / "line_events.json").read_text(encoding="utf-8") == "not json"
