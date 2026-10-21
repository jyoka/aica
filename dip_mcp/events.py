"""dip の行動の元データ(シミュレーション)。dip の会員と、行動のイベントのログを SQLite に持つ。

本番では dip の会員基盤とアクセスログが持つもの。イベントは追記だけで、書き換えも削除もしない(トリガーで止める)。
エージェントに渡すのは、ここから要約のバッチ(dip_mcp/summarize.py)が作る 1 人 1 行の状況の要約だけ(ADR-0005)。
時刻は日本時間の ISO 8601 で持つ(並べ替えを文字列の比較でできるように、書くときに日本時間にそろえる)。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path

from dip_mcp.data import FIXTURES, JST, ROOT

EVENTS_PATH = ROOT / "data" / "dip_events.db"
SCENARIO_EVENTS = FIXTURES / "dip_events.json"

# イベントの種類。job_id は visit と contact では None。data に入れるものは右のとおり
KINDS = {
    "visit": "サイトへの訪問",
    "view": "求人の閲覧",
    "save": "求人の保存",
    "unsave": "保存の取り消し",
    "form_exit": "応募フォームの離脱。data: completed(入力済みの項目名)、remaining(残りの項目名)。値は持たない",
    "form_fill": "離脱した応募フォームの残りの項目に、トークで答えた。data: fields(答えた項目名)。値は持たない",
    "apply": "応募。data: status(応募したときの選考の状態)",
    "status": "選考の状態の変化。data: status",
    "inquiry": "本人から求人への問い合わせ。data: topic",
    "contact": "dip から本人への接触(今の contacts。プッシュの記録)。data: channel、message",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    dip_id TEXT PRIMARY KEY,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    dip_id TEXT NOT NULL REFERENCES members(dip_id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    job_id TEXT,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_by_member ON events(dip_id, at);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


def iso(at: datetime | str) -> str:
    """日本時間の ISO 8601(秒まで)。タイムゾーンの無い時刻は受け取らない。"""
    d = datetime.fromisoformat(at) if isinstance(at, str) else at
    if d.tzinfo is None:
        raise ValueError(f"タイムゾーンの無い時刻: {at}")
    return d.astimezone(JST).isoformat(timespec="seconds")


def aware_time(text: str) -> datetime:
    """コマンドの引数の時刻(argparse の type)。タイムゾーンの無い時刻は、何かを書く前に引数のエラーにする。"""
    d = datetime.fromisoformat(text)
    if d.tzinfo is None:
        raise argparse.ArgumentTypeError(f"タイムゾーンをつけてください(例 2026-09-23T12:00:00+09:00): {text}")
    return d


class EventStore:
    """会員とイベントのログ。append は 1 件ずつ、extend はまとめて(生成器とシナリオの読み込み)書く。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or EVENTS_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def add_members(self, members: Iterable[tuple[str, datetime | str]]) -> None:
        with closing(self._connect()) as db, db:
            db.executemany("INSERT INTO members (dip_id, registered_at) VALUES (?, ?)",
                           ((dip_id, iso(at)) for dip_id, at in members))

    def append(self, dip_id: str, kind: str, at: datetime | str, job_id: str | None = None, **data) -> int:
        """イベントを 1 件足し、番号(seq)を返す。"""
        with closing(self._connect()) as db, db:
            return db.execute("INSERT INTO events (dip_id, at, kind, job_id, data) VALUES (?, ?, ?, ?, ?)",
                              _row(dip_id, kind, at, job_id, data)).lastrowid

    def extend(self, events: Iterable[tuple[str, str, datetime | str, str | None, dict]]) -> None:
        """(dip_id, kind, at, job_id, data) をまとめて足す。"""
        with closing(self._connect()) as db, db:
            db.executemany("INSERT INTO events (dip_id, at, kind, job_id, data) VALUES (?, ?, ?, ?, ?)",
                           (_row(*e) for e in events))

    def member_ids(self) -> list[str]:
        with closing(self._connect()) as db:
            return [r[0] for r in db.execute("SELECT dip_id FROM members ORDER BY dip_id")]

    def last_seq(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]

    def count(self) -> int:
        with closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def scan(self, through_seq: int, until: str) -> Iterator[tuple[str, list[dict]]]:
        """会員ごとに、番号が through_seq 以下で時刻が until 以下のイベントを時刻の順に返す。
        イベントの無い会員も空のリストで返す(要約は全員 1 行ずつ作る)。"""
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT m.dip_id, e.seq, e.at, e.kind, e.job_id, e.data FROM members m"
                " LEFT JOIN events e ON e.dip_id = m.dip_id AND e.seq <= ? AND e.at <= ?"
                " ORDER BY m.dip_id, e.at, e.seq", (through_seq, until))
            dip_id, batch = None, []
            for member, seq, at, kind, job_id, data in rows:
                if member != dip_id:
                    if dip_id is not None:
                        yield dip_id, batch
                    dip_id, batch = member, []
                if seq is not None:
                    batch.append({"seq": seq, "at": at, "kind": kind, "job_id": job_id, "data": json.loads(data)})
            if dip_id is not None:
                yield dip_id, batch


def _row(dip_id: str, kind: str, at: datetime | str, job_id: str | None, data: dict) -> tuple:
    if kind not in KINDS:
        raise ValueError(f"知らないイベントの種類: {kind}")
    return dip_id, iso(at), kind, job_id, json.dumps(data, ensure_ascii=False, sort_keys=True)


def load_scenario_events(store: EventStore, path: Path = SCENARIO_EVENTS) -> None:
    """シナリオのユーザー(fixtures/users.json で dip ID のある人)の会員とイベントを元データに入れる。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    store.add_members((m["dip_id"], m["registered_at"]) for m in data["members"])
    store.extend((e["dip_id"], e["kind"], e["at"], e.get("job_id"), e.get("data", {})) for e in data["events"])
