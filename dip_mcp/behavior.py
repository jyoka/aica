"""dip のツールが、本人の行動を読むところと書くところ(ADR-0006)。LLM は使わない。

- 読む(view): 会員 1 人 1 行の状況の要約(summarize.SummaryStore)に、会話の側から書いたイベントのうち要約にまだ
  入っていないもの(State の 1 人の dict の dip_writes)を重ねる。行動の元データ(events.EventStore)は読まない
- 書く(record): 元データにイベントとして追記し、同じイベントを dip_writes にも残す。要約は次のバッチまで変わらない。
  重ねるのは書いた事実(保存、保存の取り消し、フォームの項目への答え、応募、接触)だけで、傾向(tendency)と
  閲覧・訪問の数はバッチを待つ

元データと要約は State と同じディレクトリに置く(data/state.json なら data/dip_events.db と data/dip_summaries.db。
events.py と summarize.py の既定と同じ)。要約が無ければ、元データ(無ければシナリオのユーザーの分を入れて作る)から
SIM_NOW で要約のバッチを 1 回回す。dip の夜のバッチがもう回った状態から始めるための準備で、ツールの読み出しとは別。
"""

from __future__ import annotations

import copy
import fcntl
import os
import sqlite3
from datetime import datetime
from functools import cache
from pathlib import Path

from dip_mcp import summarize
from dip_mcp.data import SIM_NOW, State, now
from dip_mcp.events import EVENTS_PATH, KINDS, EventStore, iso, load_scenario_events

WRITES = "dip_writes"  # 1 人の dict の、会話の側から dip に書いたイベント(要約に入るまでのあいだ重ねる)
LOCK_FILE = "dip_events.lock"


def dip_id(state: State, line_user_id: str | None) -> str | None:
    """LINE のユーザーIDから dip の会員ID(dip_id)を引く、ただ 1 か所。今はシミュレーションの状態の 1 人の dict の
    dip_id(fixtures/users.json から写したもの)を見る。認証のカードで、LINE の連携の表に差し替える。"""
    if not line_user_id:
        return None
    return state.user(line_user_id).get("dip_id")


def _prepare(folder: Path) -> None:
    """要約が無ければ、元データ(無ければシナリオのユーザーの分を入れて作る)から SIM_NOW で要約のバッチを 1 回回す。
    要約は別の名前のファイルに作ってから置き換える。stores はロックを取らずに要約のファイルがあるかだけを見るので、
    作っている途中の空の要約を、別のプロセス(会話のサーバーと dip の MCP の子プロセス)に読ませないため。"""
    events_path, summaries_path = folder / EVENTS_PATH.name, folder / summarize.SUMMARIES_PATH.name
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / LOCK_FILE, "w") as lock:  # 会話のサーバーと dip の MCP の子プロセスが同時に準備しないように
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not summaries_path.exists():
            fresh = not events_path.exists()
            events = EventStore(events_path)
            if fresh:
                load_scenario_events(events)
            building = summaries_path.with_name(summaries_path.name + ".building")
            building.unlink(missing_ok=True)  # 前に途中で止まった分
            summarize.run(events, summarize.SummaryStore(building), SIM_NOW)
            os.replace(building, summaries_path)


@cache
def _open(folder: Path) -> tuple[EventStore, summarize.SummaryStore]:
    return EventStore(folder / EVENTS_PATH.name), summarize.SummaryStore(folder / summarize.SUMMARIES_PATH.name)


def stores(state: State) -> tuple[EventStore, summarize.SummaryStore]:
    """その State の元データと要約。要約のファイルが無ければ(初めて、または開発者が消したとき)準備する。"""
    folder = state.path.parent.resolve()
    if not (folder / summarize.SUMMARIES_PATH.name).exists():
        _prepare(folder)
    return _open(folder)


def _pending(e: dict, summary: dict) -> bool:
    """会話の側から書いたイベントが、まだ要約に入っていないか。要約に入るのは、番号が through_seq 以下で時刻が as_of 以下の
    イベント(summarize.run)。連携していない人のイベントは元データに無い(seq が None)ので、ずっと重ねる。"""
    if e["seq"] is None or e["seq"] > summary["through_seq"]:
        return True
    return summary["as_of"] is not None and e["at"] > summary["as_of"]


def _days_ago(ts: str, as_of: str) -> int:
    """as_of から暦日で数える(logic._days_ago と同じ数え方。要約の 30 日の数は as_of から数えてある)。"""
    at = datetime.fromisoformat(as_of)
    return (at.date() - datetime.fromisoformat(ts).astimezone(at.tzinfo).date()).days


def overlay(summary: dict, events: list[dict]) -> dict:
    """要約に、まだ入っていないイベント(時刻の順)を重ねる。形は要約のまま(summarize.fold と同じ決まりで、書いた事実だけ)。"""
    s = copy.deepcopy(summary)
    added = set()  # ここで重ねた保存。saves_30d に 1 足してある
    for e in events:
        kind, job_id, at, d = e["kind"], e["job_id"], e["at"], e["data"]
        if kind == "save":
            if job_id not in {x["job_id"] for x in s["saves"]}:
                s["saves"].insert(0, {"job_id": job_id, "at": at})
                s["saves_30d"] += 1
                s["saves_total"] += 1
                added.add(job_id)
        elif kind == "unsave":
            gone = next((x for x in s["saves"] if x["job_id"] == job_id), None)
            if gone:
                s["saves"].remove(gone)
                s["saves_total"] = max(0, s["saves_total"] - 1)
                # 重ねた保存は足した分を引く。要約から来た保存は、要約と同じく as_of から数えて 30 日以内なら数に入っている
                # (読む人の今や取り消しの時刻から数えると、今が as_of より後の人で数が減らない)
                if job_id in added:
                    added.discard(job_id)
                    s["saves_30d"] = max(0, s["saves_30d"] - 1)
                elif s["as_of"] and _days_ago(gone["at"], s["as_of"]) <= summarize.RECENT_DAYS:
                    s["saves_30d"] = max(0, s["saves_30d"] - 1)
        elif kind == "form_fill":
            x = s["form_exit"]
            if x and x["job_id"] == job_id:
                filled = [f for f in d.get("fields", []) if f in x["remaining"]]
                x["completed"] = [*x["completed"], *filled]
                x["remaining"] = [f for f in x["remaining"] if f not in filled]
        elif kind == "apply":
            if s["form_exit"] and s["form_exit"]["job_id"] == job_id:
                s["form_exit"] = None
            s["applications"].insert(0, {"job_id": job_id, "applied_at": at, "status": d.get("status", ""),
                                         "status_at": at})
            s["applied_job_ids"] = [job_id, *(j for j in s["applied_job_ids"] if j != job_id)]
        elif kind == "contact":
            s["last_contact"] = {"at": at, "channel": d.get("channel", ""), "message": d.get("message", "")}
    return s


def _summary(state: State, did: str | None) -> dict:
    return (stores(state)[1].get(did) if did else None) or summarize.empty_summary(did)


def view(state: State, line_user_id: str | None) -> dict:
    """本人の行動: 要約に、まだ入っていない dip_writes を重ねたもの(形は要約と同じ。as_of は要約を作った時刻)。
    連携していない人と、要約の行がまだ無い人は、行動の無い要約(as_of は None)に重ねる。"""
    did = dip_id(state, line_user_id)
    s = _summary(state, did)
    writes = state.user(line_user_id).get(WRITES, []) if line_user_id else []
    return overlay(s, [e for e in writes if _pending(e, s)])


def record(state: State, line_user_id: str, kind: str, job_id: str | None = None, **data) -> dict:
    """会話の側から dip に書く 1 件。連携している人は元データに追記し(番号 seq がつく)、どちらの人も dip_writes に残す。
    連携していない人は dip に会員がいないので、元データには書かず dip_writes にだけ残る(seq は None)。
    dip_id はあっても元データの会員に無い人(シナリオの会員を入れずに作った元データ、連携の表と dip のずれ)も、
    連携していない人と同じく dip_writes にだけ残す。要約に入ったイベントは、ここで dip_writes から落とす。"""
    if kind not in KINDS:
        raise ValueError(f"知らないイベントの種類: {kind}")
    at = iso(now(line_user_id))
    did = dip_id(state, line_user_id)
    seq = None
    if did:
        try:
            seq = stores(state)[0].append(did, kind, at, job_id, **data)
        except sqlite3.IntegrityError:  # 会員の表に無い dip_id(元データの外部キー)
            pass
    e = {"seq": seq, "at": at, "kind": kind, "job_id": job_id, "data": data}
    s = _summary(state, did)
    state.update(line_user_id, lambda u: u.__setitem__(WRITES, [*(x for x in u.get(WRITES, []) if _pending(x, s)), e]))
    return e
