"""要約のバッチ。行動の元データ(dip_mcp/events.py)から、会員 1 人 1 行の状況の要約を全員分作り直す。

    ./.venv/bin/python -m dip_mcp.summarize [--events data/dip_events.db] [--summaries data/dip_summaries.db]
                                            [--as-of 2026-09-23T12:00:00+09:00]

要約に入れるのは、今の dip のツールが使っている事実だけ(閲覧・応募・保存・離脱・接触と傾向)と、訪問。
エージェントに渡すのは要約だけになる前提で、リストの件数と文字の長さに上限を置き、1 行が SUMMARY_MAX_CHARS を超えない(ADR-0005)。
期間の判定(90 日、30 日、7 日)は as_of から数える。要約を読む側は、自分の「今」でもう一度絞ってよい。
as_of は時刻と、取り込んだイベントの最後の番号(through_seq)の組。バッチの後に足したイベントは、次のバッチまで要約に入らない。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime
from pathlib import Path

# logic の属性はバッチを回すときに引く(logic → behavior → summarize の順に読み込まれても循環しないように)
from dip_mcp import logic
from dip_mcp.data import JST, ROOT, BaitoJob, load_jobs, now
from dip_mcp.events import EVENTS_PATH, EventStore, aware_time, iso

SUMMARIES_PATH = ROOT / "data" / "dip_summaries.db"

# 要約の長さの上限。件数で切り、自由な文字列(選考の状態、接触の文、フォームの項目名)は字数で切る
RECENT_VIEWS = 5
APPLICATIONS = 5
APPLIED_JOB_IDS = 30
SAVES = 10
FORM_FIELDS = 8
FIELD_MAX = 12
TEXT_MAX = 30
SUMMARY_MAX_CHARS = 3500
# 期間(logic と同じ日数)。応募と閲覧の一覧は 90 日(HISTORY_DAYS)、閲覧と保存と訪問の数は 30 日、離脱は 7 日
RECENT_DAYS = 30
FORM_EXIT_DAYS = 7

SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
    dip_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    through_seq INTEGER NOT NULL,
    summary TEXT NOT NULL
);
"""


def fold(events: Iterable[dict]) -> dict:
    """時刻の順のイベントを、今の dip の状態のユーザーと同じ形(applications、views、saves、form_exits、contacts)と
    訪問の時刻(visits)にたたむ。保存の取り消し、フォームの残りの項目への答え、応募したフォームの離脱の消し込み、
    選考の状態の変化をここで反映する。"""
    u = {"visits": [], "views": [], "applications": [], "saves": [], "form_exits": [], "contacts": []}
    for e in events:
        kind, job_id, at, d = e["kind"], e["job_id"], e["at"], e["data"]
        if kind == "visit":
            u["visits"].append(at)
        elif kind == "view":
            u["views"].append({"job_id": job_id, "at": at})
        elif kind == "save":
            if job_id not in {s["job_id"] for s in u["saves"]}:
                u["saves"].append({"job_id": job_id, "at": at})
        elif kind == "unsave":
            u["saves"] = [s for s in u["saves"] if s["job_id"] != job_id]
        elif kind == "form_exit":
            u["form_exits"] = [x for x in u["form_exits"] if x["job_id"] != job_id]
            u["form_exits"].append({"job_id": job_id, "exited_at": at, "completed": list(d.get("completed", [])),
                                    "remaining": list(d.get("remaining", []))})
        elif kind == "form_fill":
            for x in u["form_exits"]:
                if x["job_id"] == job_id:
                    filled = [f for f in d.get("fields", []) if f in x["remaining"]]
                    x["completed"] += filled
                    x["remaining"] = [f for f in x["remaining"] if f not in filled]
        elif kind == "apply":
            u["form_exits"] = [x for x in u["form_exits"] if x["job_id"] != job_id]
            u["applications"].append({"job_id": job_id, "applied_at": at, "status": d.get("status", ""),
                                      "status_at": at})
        elif kind == "status":
            app = next((a for a in reversed(u["applications"]) if a["job_id"] == job_id), None)
            if app:
                app |= {"status": d.get("status", ""), "status_at": at}
        elif kind == "contact":
            u["contacts"].append({"at": at, "channel": d.get("channel", ""), "message": d.get("message", "")})
        # inquiry(問い合わせ)は今の dip のツールが使わないので、要約に入れない
    return u


def _clip(s: str, n: int = TEXT_MAX) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _fields(names: list[str]) -> list[str]:
    return [_clip(f, FIELD_MAX) for f in names[:FORM_FIELDS]]


def build_summary(dip_id: str, events: list[dict], as_of: datetime, through_seq: int,
                  jobs: dict[str, BaitoJob]) -> dict:
    """1 人の状況の要約。events は as_of までの、時刻の順のイベント。"""
    _days_ago, _recent = logic._days_ago, logic._recent
    u = fold(events)
    views_30d = _recent(u["views"], "at", as_of, RECENT_DAYS)
    apps = sorted(_recent(u["applications"], "applied_at", as_of, logic.HISTORY_DAYS),
                  key=lambda a: a["applied_at"], reverse=True)
    exits = [x for x in u["form_exits"] if _days_ago(x["exited_at"], as_of) <= FORM_EXIT_DAYS]
    exit_ = exits[-1] if exits else None  # dip のツールが使うのは最後の離脱だけ
    applied = list(dict.fromkeys(a["job_id"] for a in reversed(u["applications"])))
    contact = u["contacts"][-1] if u["contacts"] else None
    return {
        "dip_id": dip_id,
        "as_of": iso(as_of),
        "through_seq": through_seq,
        "last_visit": u["visits"][-1] if u["visits"] else None,
        "visits_30d": sum(1 for v in u["visits"] if _days_ago(v, as_of) <= RECENT_DAYS),
        "recent_views": [{"job_id": v["job_id"], "at": v["at"]}
                         for v in reversed(_recent(u["views"], "at", as_of, logic.HISTORY_DAYS))][:RECENT_VIEWS],
        "views_30d": {"count": len(views_30d), "by_job_type": dict(Counter(
            jobs[v["job_id"]].job_type for v in views_30d if v["job_id"] in jobs))},
        "applications": [{"job_id": a["job_id"], "applied_at": a["applied_at"], "status": _clip(a["status"]),
                          "status_at": a["status_at"]} for a in apps[:APPLICATIONS]],
        "applied_job_ids": applied[:APPLIED_JOB_IDS],
        "saves": [{"job_id": s["job_id"], "at": s["at"]} for s in reversed(u["saves"])][:SAVES],
        "saves_30d": len(_recent(u["saves"], "at", as_of, RECENT_DAYS)),
        "saves_total": len(u["saves"]),
        "form_exit": ({"job_id": exit_["job_id"], "exited_at": exit_["exited_at"],
                       "completed": _fields(exit_["completed"]), "remaining": _fields(exit_["remaining"])}
                      if exit_ else None),
        "last_contact": ({"at": contact["at"], "channel": _clip(contact["channel"]),
                          "message": _clip(contact["message"])} if contact else None),
        "tendency": logic.preferences(u, jobs, as_of),
    }


def empty_summary(dip_id: str | None) -> dict:
    """要約の行が無い人(dip と連携していない人、バッチの後に会員になった人)の、行動の無い要約。as_of は None。"""
    return build_summary(dip_id, [], now(None), 0, {}) | {"as_of": None}


def dumps(summary: dict) -> str:
    """要約の 1 行(JSON)。上限を超えたら止める(件数と字数で切っているので、超えるのは上限の決め方の誤り)。"""
    text = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    if len(text) > SUMMARY_MAX_CHARS:
        raise ValueError(f"{summary['dip_id']} の要約が {len(text)} 字で、上限 {SUMMARY_MAX_CHARS} 字を超えた")
    return text


class SummaryStore:
    """状況の要約。会員 1 人 1 行。バッチが全員分をまとめて置き換える(読む側は前の版か次の版のどちらかを見る)。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or SUMMARIES_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(SCHEMA)

    def replace_all(self, rows: Iterable[tuple[str, str, int, str]]) -> int:
        """(dip_id, as_of, through_seq, summary) で全員分を 1 つのトランザクションで置き換え、行の数を返す。"""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM summaries")
            db.executemany("INSERT INTO summaries (dip_id, as_of, through_seq, summary) VALUES (?, ?, ?, ?)", rows)
            return db.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]

    def get(self, dip_id: str) -> dict | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT summary FROM summaries WHERE dip_id = ?", (dip_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def lengths(self) -> list[int]:
        """各行の要約の字数(上限の確かめ用)。"""
        with closing(sqlite3.connect(self.path)) as db:
            return [len(r[0]) for r in db.execute("SELECT summary FROM summaries")]


def run(events: EventStore, summaries: SummaryStore, as_of: datetime | None = None) -> dict:
    """全員の要約を作り直す。as_of の既定はシミュレーションの時計 SIM_NOW(元データの時刻が SIM_NOW から見て書いてあるため)。"""
    if as_of is not None and as_of.tzinfo is None:
        raise ValueError(f"タイムゾーンの無い時刻: {as_of}")  # マシンの現地時刻として読まない(events.iso と同じ決まり)
    started = time.perf_counter()
    as_of = (as_of or now(None)).astimezone(JST)  # 暦日の数え方(logic._days_ago)を元データの時刻と同じ日本時間にする
    through_seq, until, jobs = events.last_seq(), iso(as_of), load_jobs()
    rows = ((dip_id, until, through_seq, dumps(build_summary(dip_id, evs, as_of, through_seq, jobs)))
            for dip_id, evs in events.scan(through_seq, until))
    members = summaries.replace_all(rows)
    return {"as_of": until, "through_seq": through_seq, "members": members,
            "seconds": round(time.perf_counter() - started, 2)}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="行動の元データから、会員 1 人 1 行の状況の要約を全員分作り直す")
    p.add_argument("--events", type=Path, default=EVENTS_PATH, help="行動の元データ(SQLite)")
    p.add_argument("--summaries", type=Path, default=SUMMARIES_PATH, help="状況の要約(SQLite)")
    p.add_argument("--as-of", type=aware_time, default=None,
                   help="この時刻までのイベントで作る(タイムゾーンつき。既定は SIM_NOW)")
    a = p.parse_args(argv)
    if not a.events.is_file():
        p.error(f"{a.events} がありません(python -m scripts.gen_dip_events で作る)")
    r = run(EventStore(a.events), SummaryStore(a.summaries), a.as_of)
    print(f"{r['members']} 人の要約を作り直しました(as_of {r['as_of']}、イベント {r['through_seq']} 番まで、"
          f"{r['seconds']} 秒)")


if __name__ == "__main__":
    main()
