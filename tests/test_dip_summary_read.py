"""dip のツールが行動を「1 人 1 行の要約」だけから読むことのテスト(task 181、ADR-0006)。LLM は使わない。

    ./.venv/bin/python -m pytest -q tests/test_dip_summary_read.py

tests/fixtures/dip_tools_main.json は、要約に切り替える前(main、task 180 の時点)のツールの結果。シナリオの 5 人で
同じ呼び出しをして、as_of の追加を除いて同じになることを確かめる。求人カードは求人データから作るので、
行動に関わる項目(job_id・理由・点数)だけを比べる。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dip_mcp import accounts, behavior, events, logic, profile, server, summarize
from dip_mcp.data import JST, ROOT, SIM_NOW, State, load_users
from scripts import gen_dip_events

MAIN = ROOT / "tests" / "fixtures" / "dip_tools_main.json"
RECOMMENDS = [{}, {"area": "新宿", "time_band": "夜"}, {"job_type": "飲食ホール"}, {"employment_type": "正社員"},
              {"job_type": "コンビニ", "area": "東京都内"}, {"exclude_ids": ["a001", "a002"]}]


def _slim_recommend(r: dict) -> dict:
    return {**r, "jobs": [{k: j[k] for k in ("job_id", "reasons", "match_score", "score_breakdown")} for j in r["jobs"]]}


def _slim_saved(jobs: list[dict]) -> list[dict]:
    return [{"job_id": j["job_id"], "saved_at": j["saved_at"]} for j in jobs]


def _slim_profile(p: dict) -> dict:
    return {**p, "saved_jobs": _slim_saved(p["saved_jobs"])}


def reads(state: State, uid: str) -> dict:
    """行動を読むツール(と、プロフィール画面)の結果。"""
    return {
        "conversation_summary": logic.conversation_summary(state, uid),
        "recent_applications": logic.recent_applications(state, uid),
        "form_exit": logic.form_exit(state, uid),
        "application_status": logic.application_status(state, uid),
        "application_status_a001": logic.application_status(state, uid, "a001"),
        "recommend": [_slim_recommend(logic.recommend(state, uid, **a)) for a in RECOMMENDS],
        "relax": logic.relax(state, uid, area="上野", time_band="早朝", job_type="塾講師・家庭教師"),
        "saved_jobs": _slim_saved(logic.saved_jobs(state, uid)),
        "talk_entry": logic.talk_entry(state, uid),
        "profile": _slim_profile(profile.view(state, uid)),
    }


def _fresh(mkdir) -> State:
    s = State(Path(mkdir()) / "state.json")
    s.reset()
    return s


def writes(uid: str, mkdir) -> dict:
    """会話の中で書いた直後に読んだ結果(書き込みごとに初期状態から。mkdir は空のディレクトリを作る関数)。"""
    out = {}
    s = _fresh(mkdir)
    out["save_job"] = logic.save_job(s, uid, "b0101")
    out["after_save"] = reads(s, uid)
    s = _fresh(mkdir)
    out["push"] = [logic.push_trigger(s, uid, "form_exit"), logic.push_trigger(s, uid, "form_exit")]
    out["after_push"] = logic.conversation_summary(s, uid)
    s = _fresh(mkdir)
    out["resume_form"] = logic.resume_form(s, uid, "a002", {"希望開始日": "来週から", "電話番号": "090-1234-5678"})
    out["after_resume_form"] = {"form_exit": logic.form_exit(s, uid), "summary": logic.conversation_summary(s, uid)}
    s = _fresh(mkdir)
    logic.submit_application(s, uid, "a002")
    out["after_submit"] = reads(s, uid)
    s = _fresh(mkdir)
    out["unsave"] = profile.unsave_job(s, uid, "a003")
    out["after_unsave"] = {"saved_jobs": _slim_saved(logic.saved_jobs(s, uid)),
                           "summary": logic.conversation_summary(s, uid), "profile": _slim_profile(profile.view(s, uid))}
    return out


def results(mkdir=tempfile.mkdtemp) -> dict:
    return {uid: {"reads": reads(_fresh(mkdir), uid), "writes": writes(uid, mkdir)} for uid in sorted(load_users())}


# main の後に足した、行動の読み方と関係ない項目(task 188 のゲームの下書きとゲームの URL、task 233 の条件の問いの選択肢の候補)。比べない
LATER = ("arcade_draft", "arcade_url", "arcade_drafts", "condition_options")


def _plain(x, drop: tuple[str, ...] = ("as_of",)):
    """JSON に通した形(タプルをリストに)から、比べない鍵(と LATER)を落とす。要約の note からは、後で足した説明
    (logic.ARCADE_NOTE と logic.CONDITION_NOTE)を外す。"""
    if isinstance(x, dict):
        return {k: (v.removesuffix(logic.CONDITION_NOTE).removesuffix(logic.ARCADE_NOTE)
                    if k == "note" and isinstance(v, str) else _plain(v, drop))
                for k, v in x.items() if k not in drop + LATER}
    if isinstance(x, list):
        return [_plain(v, drop) for v in x]
    return x


@pytest.fixture(scope="module")
def now_and_main(tmp_path_factory):
    new = results(lambda: tmp_path_factory.mktemp("state"))
    return json.loads(json.dumps(new, ensure_ascii=False)), json.loads(MAIN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("uid", sorted(load_users()))
def test_tools_return_the_same_as_main_except_as_of(now_and_main, uid):
    """シナリオの 5 人で、行動を読むツールの結果が main(要約に切り替える前)と同じ。違うのは as_of を足したことだけ。"""
    new, main = now_and_main
    assert _plain(new[uid]["reads"]) == main[uid]["reads"]


@pytest.mark.parametrize("uid", sorted(load_users()))
def test_writes_in_the_conversation_read_back_as_on_main_except_the_tendency(now_and_main, uid):
    """会話の中で書いた直後に読む結果も main と同じ。ただし傾向(tendency)は次のバッチまで変わらない(ADR-0006)ので、
    傾向と、傾向で決まる推薦の並び(recommend)は比べない。"""
    new, main = now_and_main
    drop = ("as_of", "tendency", "recommend")
    assert _plain(new[uid]["writes"], drop) == _plain(main[uid]["writes"], drop)


# ----------------------------- 元データを読まない -----------------------------


def _event_count(state: State) -> int:
    with closing(sqlite3.connect(behavior.stores(state)[0].path)) as db:
        return db.execute("SELECT COUNT(*) FROM events").fetchone()[0]


TOOL_ARGS = {
    "get_user_conversation_summary": {},
    "get_user_recent_applications": {},
    "get_user_form_exit": {},
    "recommend_jobs": {"area": "新宿"},
    "relax_conditions": {"area": "上野", "time_band": "早朝"},
    "answer_job_question": {"question": "まかないはありますか", "job_id": "a001"},
    "get_application_status": {},
    "resume_application_form": {"job_id": "a002", "answers": {"希望開始日": "来週から"}},
    "save_job": {"job_id": "b0101"},
    "save_user_preferences": {"preferences": {"area": "新宿"}},
    "set_search_track": {"track": "アルバイト・パート", "evidence": "バイトを探しています"},
    "get_counseling_playbook": {"track": "未定"},
    "record_conversation_outcome": {"outcome": "consult", "reason": "相談した", "confidence": 0.8},
}


def test_tools_never_read_the_raw_events(tmp_path, monkeypatch):
    """元データを読む関数を、呼ばれたら落ちるものに差し替えても、どの MCP ツールも(書くツールも)動く。
    書くツールは元データに追記する(追記は読み出しではない)。"""
    state = State(tmp_path / "state.json")
    state.reset()
    before = _event_count(state)  # 要約のバッチを 1 回回した状態(準備)。ここから先は元データを読ませない
    monkeypatch.setattr(server, "state", state)

    def forbidden(*args, **kwargs):
        raise AssertionError("dip のツールが行動の元データを読んだ")

    for name in ("scan", "last_seq", "count", "member_ids"):
        monkeypatch.setattr(events.EventStore, name, forbidden)
    monkeypatch.setattr(events, "load_scenario_events", forbidden)
    monkeypatch.setattr(behavior, "load_scenario_events", forbidden)
    monkeypatch.setattr(summarize, "run", forbidden)

    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert tools == set(TOOL_ARGS)  # ツールを足したら、ここにも足して確かめる
    for uid in sorted(load_users()):
        for name, args in TOOL_ARGS.items():
            if name in ("answer_job_question", "get_counseling_playbook"):
                asyncio.run(server.mcp.call_tool(name, args))
            else:
                asyncio.run(server.mcp.call_tool(name, {"line_user_id": uid, **args}))
        logic.push_trigger(state, uid, "form_exit")
        logic.submit_application(state, uid, "a002")
        profile.view(state, uid)
        profile.unsave_job(state, uid, "b0101")
    monkeypatch.undo()
    # 連携している 4 人が書いた分(保存、離脱した人のフォームの答えと接触、応募、保存の取り消し)だけ増えた
    assert _event_count(state) == before + 4 * 3 + 2


# ----------------------------- 会話の中で書いたこと -----------------------------


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.json")
    s.reset()
    return s


def test_a_saved_job_is_there_right_after_saving_while_the_summary_waits_for_the_batch(state):
    """保存した直後の会話で、保存が無かったことにならない。要約の行は次のバッチまで変わらない。"""
    summaries = behavior.stores(state)[1]
    row = summaries.get("D1001")
    assert logic.save_job(state, "sim-yoru", "b0301") == {"saved": "b0301", "saves_total": 1}
    assert summaries.get("D1001") == row  # 要約はそのまま
    assert [j["job_id"] for j in logic.saved_jobs(state, "sim-yoru")] == ["b0301"]
    assert logic.conversation_summary(state, "sim-yoru")["saves_30d"] == 1
    assert [j["job_id"] for j in profile.view(state, "sim-yoru")["saved_jobs"]] == ["b0301"]
    assert logic.save_job(state, "sim-yoru", "b0301")["saves_total"] == 1  # 2 回目は書かない

    # 次のバッチで要約に入る。重ねた分とは二重に数えない
    summarize.run(*behavior.stores(state), SIM_NOW)
    assert [s["job_id"] for s in summaries.get("D1001")["saves"]] == ["b0301"]
    assert logic.conversation_summary(state, "sim-yoru")["saves_30d"] == 1
    logic.save_job(state, "sim-yoru", "b0302")  # 書いたとき、要約に入った分を会話の側の記録から落とす
    assert [e["job_id"] for e in state.user("sim-yoru")[behavior.WRITES]] == ["b0302"]
    assert [j["job_id"] for j in logic.saved_jobs(state, "sim-yoru")] == ["b0302", "b0301"]


def test_writes_of_one_scenario_copy_do_not_show_for_another(state):
    """同じシナリオから始めた 2 人(ブラウザごとのユーザー)は同じ要約を読むが、会話の中で書いた分は本人にだけ重なる。"""
    a, b = "w-aaaaaaaaaaaaaaaa:sim-exit", "w-bbbbbbbbbbbbbbbb:sim-exit"
    for uid in (a, b):
        state.reset_user(uid, template="sim-exit")
    logic.save_job(state, a, "b0101")
    logic.submit_application(state, a, "a002")
    assert logic.saved_jobs(state, b) == [] and logic.form_exit(state, b)["job_id"] == "a002"
    assert logic.form_exit(state, a) is None and [x["job_id"] for x in logic.recent_applications(state, a)] == ["a002"]
    assert "a002" not in [j["job_id"] for j in logic.recommend(state, a, job_type="コンビニ", limit=3)["jobs"]]
    assert "a002" in [j["job_id"] for j in logic.recommend(state, b, job_type="コンビニ", area="中野")["jobs"]]
    state.reset_user(a, template="sim-exit")  # 初期に戻すと、書いた分は読めなくなる(要約の時点に戻る)
    assert logic.saved_jobs(state, a) == [] and logic.form_exit(state, a)["job_id"] == "a002"


def test_an_unlinked_user_keeps_writes_only_on_the_conversation_side(state):
    """dip と連携していない人は会員がいないので、元データには書かない。保存は会話の側の記録から読める。"""
    before = _event_count(state)
    logic.save_job(state, "sim-new", "b0101")
    assert _event_count(state) == before
    assert [j["job_id"] for j in logic.saved_jobs(state, "sim-new")] == ["b0101"]
    assert logic.conversation_summary(state, "sim-new")["linked"] is False


def _linked_member(state: State, did: str, rows: list[tuple]) -> str:
    """元データに会員 did とそのイベント(kind, 何時間前, job_id, data)を入れ、SIM_NOW で要約のバッチを回し、
    固定の時計のユーザーをその会員につなぐ。つないだユーザーIDを返す。"""
    events, summaries = behavior.stores(state)
    events.add_members([(did, SIM_NOW - timedelta(days=400))])
    events.extend((did, kind, SIM_NOW - timedelta(hours=h), job, data) for kind, h, job, data in rows)
    summarize.run(events, summaries, SIM_NOW)
    uid = f"w-{did.lower():0>16}:sim-new"
    state.reset_user(uid, template="sim-new")
    accounts.of(state).set_link(uid, did)
    return uid


def test_eleven_saves_all_show_on_the_screen_and_ten_go_to_the_agent(state):
    """保存 11 件の会員: 画面(保存した求人、プロフィール)は 11 件。エージェントに渡しうる一覧は新しい順に 10 件と、ほかに 1 件。
    いちばん古い保存も、取り消すと元データに書き、AI に知らせ、次のバッチで消える。"""
    jobs = sorted(logic.load_jobs())[:11]
    uid = _linked_member(state, "D9001", [("save", len(jobs) - i, j, {}) for i, j in enumerate(jobs)])
    newest_first = jobs[::-1]
    assert [j["job_id"] for j in logic.saved_jobs(state, uid)] == newest_first
    assert [j["job_id"] for j in profile.view(state, uid)["saved_jobs"]] == newest_first
    agent = summarize.for_agent(behavior.view(state, uid))
    assert [s["job_id"] for s in agent["saves"]] == newest_first[:10] and agent["saves_more"] == 1

    profile.unsave_job(state, uid, jobs[0])
    assert jobs[0] not in [j["job_id"] for j in logic.saved_jobs(state, uid)]
    assert [c["what"] for c in state.user(uid)["profile_changes"]] == [f"求人「{logic.load_jobs()[jobs[0]].title}」の保存"]
    summarize.run(*behavior.stores(state), SIM_NOW)
    assert behavior.stores(state)[1].get("D9001")["saves_total"] == 10
    before = _event_count(state)
    profile.unsave_job(state, uid, jobs[0])  # 保存していない求人の取り消しは書かない
    assert _event_count(state) == before


def test_a_member_with_more_than_30_applications_is_not_recommended_any_applied_job(state):
    """応募 31 件以上の会員にも、応募済みの求人はすすめない(要約の応募済みの求人 ID は全件。90 日より前の応募も)。"""
    jobs = logic.load_jobs()
    applied = sorted(jobs)[:40]
    uid = _linked_member(state, "D9002", [("apply", 24 * (i + 1), j, {"status": "応募済み"})
                                          for i, j in enumerate(applied)])
    r = logic.recommend(state, uid, limit=len(jobs))
    assert r["total"] == len(jobs) - len(applied)
    assert not {j["job_id"] for j in r["jobs"]} & set(applied)
    oldest = jobs[applied[-1]]  # 40 日前に応募した求人。同じ職種で探しても出ない
    assert oldest.job_id not in [j["job_id"] for j in logic.recommend(state, uid, job_type=oldest.job_type,
                                                                      limit=len(jobs))["jobs"]]


def test_the_status_of_an_application_older_than_the_five_the_agent_gets_is_found_by_job_id(state):
    """応募 8 件の会員: エージェントに渡す一覧は新しい順に 5 件と、ほかに 3 件。6 件目より前に応募した求人の選考状況も、
    job_id を渡せば引ける。"""
    applied = sorted(logic.load_jobs())[:8]  # 古い順に応募した
    uid = _linked_member(state, "D9003", [("apply", 24 * (8 - i), j, {"status": f"選考中{i}"})
                                          for i, j in enumerate(applied)])
    recent = logic.recent_applications(state, uid)
    assert [a["job_id"] for a in recent[:5]] == applied[::-1][:5]
    assert recent[5]["more"] == 3 and "ほかに 3 件" in recent[5]["note"] and len(recent) == 6
    status = logic.application_status(state, uid)
    assert [a["job_id"] for a in status["applications"]] == applied[::-1][:5] and status["more"] == 3
    first = logic.application_status(state, uid, applied[0])
    assert [(a["job_id"], a["status"]) for a in first["applications"]] == [(applied[0], "選考中0")]
    assert "more" not in first
    assert len(profile.view(state, uid)["applications"]) == 8  # プロフィールの応募の履歴は全件


def test_summary_tells_as_of_when_the_behavior_was_summarized(state):
    s = logic.conversation_summary(state, "sim-exit")
    assert s["as_of"] == SIM_NOW.isoformat()
    assert logic.application_status(state, "sim-applied")["as_of"] == SIM_NOW.isoformat()
    assert "as_of" not in logic.conversation_summary(state, "sim-new")  # 連携していない人には行動が無い
    prompt = (ROOT / "dip_mcp" / "prompts" / "baitoru_assistant.md").read_text(encoding="utf-8")
    assert "as_of" in prompt


def test_dip_id_is_looked_up_in_one_place(state, monkeypatch):
    """LINE のユーザーIDから dip_id を引くのは behavior.dip_id だけ(認証のカードで連携の表に差し替える)。
    差し替えると、どのツールもその表で引いた会員の要約を読む。"""
    monkeypatch.setattr(behavior, "dip_id", lambda st, uid: {"w-cccccccccccccccc:sim-new": "D1002"}.get(uid))
    assert logic.conversation_summary(state, "w-cccccccccccccccc:sim-new")["segment"] == "form_exit"
    assert logic.form_exit(state, "w-cccccccccccccccc:sim-new")["job_id"] == "a002"
    assert logic.conversation_summary(state, "sim-exit")["linked"] is False  # 表に無い人は未連携
    for module in ("logic.py", "profile.py", "server.py"):
        assert '"dip_id"' not in (ROOT / "dip_mcp" / module).read_text(encoding="utf-8"), module


def test_unsaving_counts_down_saves_30d_from_as_of_when_now_is_after_as_of(state):
    """要約の 30 日の保存の数は as_of から数えてある。本人の今が as_of より後(毎晩のバッチの本番では普通)でも、
    取り消した保存が as_of から 30 日以内なら数を減らし、30 日より前なら減らさない。この会話で重ねた保存は足した分を引く。"""
    events, summaries = behavior.stores(state)
    as_of = datetime.now(JST) - timedelta(days=1)
    events.add_members([("D9100", as_of - timedelta(days=60))])
    events.extend([("D9100", "save", as_of - timedelta(days=40), "a001", {}),
                   ("D9100", "save", as_of - timedelta(days=30), "a002", {})])
    summarize.run(events, summaries, as_of)
    uid = "w-eeeeeeeeeeeeeeee"  # 本番のユーザー(実際の時計)を、その会員につなぐ
    accounts.of(state).set_link(uid, "D9100")
    assert logic.conversation_summary(state, uid)["saves_30d"] == 1

    profile.unsave_job(state, uid, "a001")  # as_of から 40 日前の保存は、もともと数に入っていない
    assert logic.conversation_summary(state, uid)["saves_30d"] == 1
    profile.unsave_job(state, uid, "a002")  # as_of から 30 日前の保存は数に入っている(取り消しの時刻からは 31 日)
    s = logic.conversation_summary(state, uid)
    assert (s["saves_30d"], s["segment"]) == (0, "active")
    assert logic.saved_jobs(state, uid) == []

    logic.save_job(state, uid, "b0101")
    assert logic.conversation_summary(state, uid)["saves_30d"] == 1
    profile.unsave_job(state, uid, "b0101")
    assert logic.conversation_summary(state, uid)["saves_30d"] == 0


def test_a_dip_id_missing_from_the_raw_members_keeps_writes_only_on_the_conversation_side(tmp_path):
    """dip_id はあるのに元データの会員に無い人(シナリオの会員を入れずに作った元データ、連携の表と dip のずれ)が書いても
    落ちない。連携していない人と同じく、会話の側の記録にだけ残り、その会話の中では読める。"""
    gen_dip_events.main(["--events", str(tmp_path / events.EVENTS_PATH.name), "--members", "5", "--months", "1",
                         "--no-scenarios"])
    state = State(tmp_path / "state.json")
    state.reset()
    before = _event_count(state)
    assert logic.save_job(state, "sim-yoru", "b0101") == {"saved": "b0101", "saves_total": 1}
    logic.push_trigger(state, "sim-exit", "form_exit")
    logic.submit_application(state, "sim-exit", "a002")
    assert _event_count(state) == before
    assert [j["job_id"] for j in logic.saved_jobs(state, "sim-yoru")] == ["b0101"]
    assert [e["seq"] for e in state.user("sim-yoru")[behavior.WRITES]] == [None]


def test_another_process_does_not_read_the_summary_while_it_is_being_prepared(tmp_path, monkeypatch):
    """初めての準備で要約のバッチを回しているあいだに、別のプロセス(ここでは別の State と別のスレッド)が読みに来ても、
    作りかけの空の要約を読まず、準備が終わるのを待って同じ要約を読む。"""
    load_jobs = summarize.load_jobs

    def slow():
        time.sleep(1)
        return load_jobs()

    monkeypatch.setattr(summarize, "load_jobs", slow)
    a, b = State(tmp_path / "state.json"), State(tmp_path / "state.json")
    a.reset()
    preparing = threading.Thread(target=behavior.stores, args=(a,))
    preparing.start()
    time.sleep(0.5)
    try:
        s = logic.conversation_summary(b, "sim-exit")
    finally:
        preparing.join(timeout=30)
    assert not preparing.is_alive()
    assert (s["segment"], s["as_of"]) == ("form_exit", SIM_NOW.isoformat())


if __name__ == "__main__":  # golden を作り直す(main のコードで実行したもの。要約に切り替えた後で作り直さない)
    MAIN.write_text(json.dumps(results(), ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
