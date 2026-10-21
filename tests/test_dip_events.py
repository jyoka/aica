"""dip の行動の元データ、生成器、要約のバッチのテスト。LLM は使わない。

    ./.venv/bin/python -m pytest -q tests/test_dip_events.py
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from datetime import timedelta

import pytest

from dip_mcp import summarize
from dip_mcp.data import SIM_NOW, load_jobs, load_users
from dip_mcp.events import KINDS, EventStore, load_scenario_events
from scripts import gen_dip_events


def _dump(store: EventStore) -> tuple[list, list]:
    with closing(sqlite3.connect(store.path)) as db:
        return (db.execute("SELECT * FROM members ORDER BY dip_id").fetchall(),
                db.execute("SELECT * FROM events ORDER BY seq").fetchall())


@pytest.fixture
def scenario(tmp_path):
    """シナリオのユーザーだけの元データと、SIM_NOW で作った要約。"""
    events = EventStore(tmp_path / "events.db")
    load_scenario_events(events)
    summaries = summarize.SummaryStore(tmp_path / "summaries.db")
    summarize.run(events, summaries, SIM_NOW)
    return events, summaries


# ----------------------------- 生成器 -----------------------------


def test_generator_makes_the_same_data_from_the_same_seed(tmp_path):
    a, b, c = (EventStore(tmp_path / f"{n}.db") for n in "abc")
    gen_dip_events.generate(a, 40, 2, seed=7)
    gen_dip_events.generate(b, 40, 2, seed=7)
    gen_dip_events.generate(c, 40, 2, seed=8)
    assert _dump(a) == _dump(b)
    assert _dump(a)[1] and _dump(a) != _dump(c)


def test_generator_makes_1000_members_for_6_months_and_the_batch_rebuilds_them_all(tmp_path):
    events = EventStore(tmp_path / "events.db")
    n = gen_dip_events.generate(events, 1000, 6)
    assert n == events.count() > 50_000
    members, rows = _dump(events)
    assert len(members) == 1000
    times = [r[2] for r in rows]
    assert min(times) >= (SIM_NOW - timedelta(days=180)).isoformat() and max(times) <= SIM_NOW.isoformat()
    # 保存の取り消しとフォームの残りの項目への答えは、画面とトークの操作で書く。生成器は作らない
    assert {r[3] for r in rows} == set(KINDS) - {"unsave", "form_fill"}

    summaries = summarize.SummaryStore(tmp_path / "summaries.db")
    r = summarize.run(events, summaries, SIM_NOW)
    assert r["members"] == 1000 and r["through_seq"] == events.last_seq()
    assert max(summaries.agent_lengths()) <= summarize.SUMMARY_MAX_CHARS

    # 人ごとの偏り: よく見る職種は、閲覧の多い人ほど閲覧の大きな割合を占める(12 の職種に均等なら 1/12)
    jobs = load_jobs()
    by_member: dict[str, Counter] = {}
    for _, dip_id, _, kind, job_id, _ in rows:
        if kind == "view":
            by_member.setdefault(dip_id, Counter())[jobs[job_id].job_type] += 1
    shares = [c.most_common(1)[0][1] / c.total() for c in by_member.values() if c.total() >= 30]
    assert len(shares) > 100 and sorted(shares)[len(shares) // 2] > 0.3
    # 応募しない人と、応募する人がいる
    applied = {r[1] for r in rows if r[3] == "apply"}
    assert 100 < len(applied) < 900


# ----------------------------- シナリオのユーザー -----------------------------


# 要約から読むツールの結果が、users.json に行動を持っていたとき(main)と同じことは、
# tests/test_dip_summary_read.py の test_tools_return_the_same_as_main_except_as_of で確かめる


def test_scenario_members_are_the_linked_users_of_users_json(scenario):
    events, _ = scenario
    assert events.member_ids() == sorted(u["dip_id"] for u in load_users().values() if u["dip_id"])


# ----------------------------- as_of -----------------------------


def test_events_added_after_a_batch_wait_for_the_next_batch(scenario):
    events, summaries = scenario
    before = summaries.get("D1005")
    seq = events.append("D1005", "save", SIM_NOW - timedelta(hours=1), "b0601")
    later = events.append("D1005", "view", SIM_NOW + timedelta(hours=1), "b0302")

    # 次のバッチまでは、足したイベントは要約に入らない。through_seq より後の番号なので as_of で分かる
    assert summaries.get("D1005") == before
    assert before["through_seq"] < seq

    summarize.run(events, summaries, SIM_NOW)
    s = summaries.get("D1005")
    assert s["through_seq"] == later
    assert [x["job_id"] for x in s["saves"]] == ["b0601", "a003"]
    assert s["views_30d"]["count"] == before["views_30d"]["count"]  # as_of より後の時刻の閲覧はまだ入らない

    summarize.run(events, summaries, SIM_NOW + timedelta(hours=2))
    assert summaries.get("D1005")["views_30d"]["count"] == before["views_30d"]["count"] + 1


# ----------------------------- 元データと要約の形 -----------------------------


def test_events_are_append_only(scenario):
    events, _ = scenario
    with closing(sqlite3.connect(events.path)) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE events SET job_id = 'x' WHERE seq = 1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM events")
    with pytest.raises(ValueError):
        events.append("D1001", "purchase", SIM_NOW)


def test_fold_applies_unsave_status_changes_and_apply_after_a_form_exit(tmp_path):
    events = EventStore(tmp_path / "events.db")
    events.add_members([("D1", SIM_NOW - timedelta(days=60))])

    def t(hours: int):
        return SIM_NOW - timedelta(hours=hours)

    events.extend([
        ("D1", "save", t(50), "a001", {}), ("D1", "save", t(49), "a002", {}), ("D1", "unsave", t(48), "a001", {}),
        ("D1", "form_exit", t(40), "a003", {"completed": ["氏名"], "remaining": ["電話番号"]}),
        ("D1", "form_exit", t(39), "a002", {"completed": [], "remaining": ["年齢", "希望開始日", "電話番号"]}),
        ("D1", "form_fill", t(38), "a002", {"fields": ["希望開始日", "知らない項目"]}),
        ("D1", "apply", t(30), "a003", {"status": gen_dip_events.APPLIED}),
        ("D1", "status", t(20), "a003", {"status": "採用"}),
        ("D1", "form_exit", t(10), "a001", {"completed": [], "remaining": ["氏名"]}),
        ("D1", "contact", t(9), None, {"channel": "LINE AI通知", "message": "form_exit"}),
        ("D1", "inquiry", t(8), "a001", {"topic": "シフト"}),
    ])
    summaries = summarize.SummaryStore(tmp_path / "summaries.db")
    summarize.run(events, summaries, SIM_NOW)
    s = summaries.get("D1")
    assert [x["job_id"] for x in s["saves"]] == ["a002"]
    assert s["form_exit"]["job_id"] == "a001"  # 応募したフォームの離脱は消え、残った離脱のうち最後のもの
    assert s["saves_total"] == 1
    assert fold_exits(events)["a002"] == {"completed": ["希望開始日"], "remaining": ["年齢", "電話番号"]}
    assert [(a["job_id"], a["status"], a["status_at"]) for a in s["applications"]] == [
        ("a003", "採用", t(20).isoformat())]
    assert s["last_contact"] == {"at": t(9).isoformat(), "channel": "LINE AI通知", "message": "form_exit"}


def fold_exits(events: EventStore) -> dict:
    """元データをたたんだ離脱(job_id → 入力済みと残りの項目)。"""
    (_, evs), = events.scan(events.last_seq(), SIM_NOW.isoformat())
    return {x["job_id"]: {"completed": x["completed"], "remaining": x["remaining"]}
            for x in summarize.fold(evs)["form_exits"]}


def test_summary_keeps_every_application_and_save_and_the_agent_part_stays_under_the_cap(tmp_path):
    """応募 500 件・保存 300 件の会員でも、要約のバッチは止まらない。要約の 1 行には応募(90 日)と保存と応募済みの
    求人 ID を全件入れ、上限 SUMMARY_MAX_CHARS はエージェントに渡しうる分(for_agent)にかける。"""
    events = EventStore(tmp_path / "events.db")
    events.add_members([("D1", SIM_NOW - timedelta(days=60)), ("D2", SIM_NOW - timedelta(days=60))])
    job_ids = sorted(load_jobs())
    long = "とても長い選考の状態の説明" * 10
    rows = []
    for i in range(500):
        at, job = SIM_NOW - timedelta(minutes=500 - i), job_ids[i % len(job_ids)]
        rows += [("D1", "visit", at, None, {}), ("D1", "view", at, job, {}),
                 ("D1", "apply", at, f"x{i:04d}", {"status": long}), ("D1", "status", at, f"x{i:04d}", {"status": long}),
                 ("D1", "form_exit", at, job, {"completed": [long] * 30, "remaining": [long] * 30}),
                 ("D1", "contact", at, None, {"channel": long, "message": long})]
        if i < 300:
            rows.append(("D1", "save", at, f"s{i:04d}", {}))
    rows.append(("D2", "save", SIM_NOW - timedelta(hours=1), "a001", {}))
    events.extend(rows)
    summaries = summarize.SummaryStore(tmp_path / "summaries.db")
    assert summarize.run(events, summaries, SIM_NOW)["members"] == 2
    s = summaries.get("D1")
    assert len(s["recent_views"]) == summarize.RECENT_VIEWS
    assert len(s["applications"]) == len(s["applied_job_ids"]) == 500
    assert s["applications"][0]["job_id"] == s["applied_job_ids"][0] == "x0499"  # 新しい順
    assert len(s["applications"][0]["status"]) == summarize.TEXT_MAX
    assert len(s["saves"]) == s["saves_total"] == 300 and s["saves"][0]["job_id"] == "s0299"
    assert len(s["form_exit"]["completed"]) == summarize.FORM_FIELDS
    assert s["views_30d"]["count"] == 500

    agent = summarize.for_agent(s)
    assert [a["job_id"] for a in agent["applications"]] == [f"x{i:04d}" for i in range(499, 494, -1)]
    assert (len(agent["saves"]), agent["applications_more"], agent["saves_more"]) == (summarize.AGENT_SAVES, 495, 290)
    assert "applied_job_ids" not in agent
    assert max(summaries.agent_lengths()) <= summarize.SUMMARY_MAX_CHARS < max(summaries.lengths())
    assert summarize.for_agent(summaries.get("D2"))["saves_more"] == 0


def test_the_batch_stops_when_the_part_for_the_agent_is_over_the_cap(monkeypatch):
    """上限を超えるのはエージェントに渡しうる分の決め方の誤りなので、バッチを止める(1 行の長さでは止めない)。"""
    s = summarize.build_summary("D1", [], SIM_NOW, 0, {})
    monkeypatch.setattr(summarize, "SUMMARY_MAX_CHARS", 10)
    with pytest.raises(ValueError, match="エージェントに渡す要約"):
        summarize.dumps(s)


# ----------------------------- コマンド -----------------------------


def test_commands_generate_then_summarize(tmp_path, capsys):
    ev, su = tmp_path / "events.db", tmp_path / "summaries.db"
    gen_dip_events.main(["--members", "20", "--months", "1", "--events", str(ev)])
    with pytest.raises(SystemExit):  # イベントは追記だけなので、上書きは --replace のときだけ
        gen_dip_events.main(["--members", "20", "--events", str(ev)])
    summarize.main(["--events", str(ev), "--summaries", str(su)])
    out = capsys.readouterr().out
    assert "24 人の要約を作り直しました(as_of 2026-09-23T12:00:00+09:00" in out  # 生成した 20 人とシナリオの 4 人
    assert summarize.SummaryStore(su).get("D1002")["form_exit"]["job_id"] == "a002"
    with pytest.raises(SystemExit):
        summarize.main(["--events", str(tmp_path / "missing.db"), "--summaries", str(su)])


def test_times_without_a_timezone_are_rejected_before_anything_is_written(tmp_path, capsys):
    # マシンの現地時刻として読むと、TZ によって要約に入るイベントが変わる。コマンドは引数のエラーにし、ファイルを作らない
    ev, su = tmp_path / "events.db", tmp_path / "summaries.db"
    with pytest.raises(SystemExit):
        gen_dip_events.main(["--members", "3", "--end", "2026-09-23T12:00:00", "--events", str(ev)])
    assert "タイムゾーンをつけてください" in capsys.readouterr().err
    assert not ev.exists()

    gen_dip_events.main(["--members", "3", "--months", "1", "--events", str(ev)])
    with pytest.raises(SystemExit):
        summarize.main(["--events", str(ev), "--summaries", str(su), "--as-of", "2026-09-23T12:00:00"])
    assert "タイムゾーンをつけてください" in capsys.readouterr().err
    assert not su.exists()
    with pytest.raises(ValueError, match="タイムゾーンの無い時刻"):
        summarize.run(EventStore(ev), summarize.SummaryStore(su), SIM_NOW.replace(tzinfo=None))
