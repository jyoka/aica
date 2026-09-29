"""dip 側(MCP サーバー)のオフラインテスト。LLM は使わない。

    ./.venv/bin/python -m pytest -q
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from dip_mcp import logic
from dip_mcp.data import ROOT, State


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.json")
    s.reset()
    return s


# ----------------------------- 理解(要約) -----------------------------


@pytest.mark.parametrize("user,segment", [
    ("sim-yoru", "browsing"), ("sim-exit", "form_exit"),
    ("sim-applied", "applied_waiting"), ("sim-new", "unlinked"),
    ("never-seen", "unlinked"),
])
def test_summary_segments(state, user, segment):
    assert logic.conversation_summary(state, user)["segment"] == segment


def test_summary_does_not_list_viewed_job_names(state):
    s = logic.conversation_summary(state, "sim-yoru")
    viewed_titles = ["居酒屋ホールスタッフ", "深夜バル", "キッチンスタッフ"]
    assert s["recent_views"]["count_30d"] == 3
    assert not any(t in str(s["recent_views"]) for t in viewed_titles)  # 件数と職種の傾向だけ
    assert s["last_application"]["applied"] == "7/14"


def test_applied_days_counted_in_calendar_days(state):
    assert logic.conversation_summary(state, "sim-applied")["last_application"]["days_ago"] == 4  # 9/19 → 9/23


# ----------------------------- 推薦 -----------------------------


def test_recommend_max_three_with_reasons_and_history(state):
    r = logic.recommend(state, "sim-yoru", area="新宿", time_band="夜")
    assert 1 <= len(r["jobs"]) <= 3
    assert all(j["reasons"] for j in r["jobs"])
    assert r["jobs"][0]["reasons"][0] == "7月に応募した飲食ホールと同系統"


def test_recommend_returns_match_score_and_breakdown_in_rank_order(state):
    jobs = logic.recommend(state, "sim-yoru", area="新宿", time_band="夜")["jobs"]
    scores = [j["match_score"] for j in jobs]
    assert scores == sorted(scores, reverse=True)  # 点数の高い順 = いまの並び順
    b = jobs[0]["score_breakdown"]
    assert set(b) == {"job_type", "time_band", "area", "station", "wage"}
    assert round(sum(b.values()), 2) == jobs[0]["match_score"]
    assert b["job_type"] == 2.0  # 本人の傾向の1番目の職種(飲食ホール)


def test_recommend_excludes_applied_and_excluded_jobs(state):
    ids = lambda **kw: [j["job_id"] for j in logic.recommend(state, "sim-applied", **kw)["jobs"]]
    assert "a001" not in ids(area="新宿", job_type="飲食ホール")  # 応募済み
    first = ids(area="新宿")
    assert first[0] not in ids(area="新宿", exclude_ids=[first[0]])


def test_relax_suggests_which_condition_to_drop(state):
    r = logic.relax(state, "sim-yoru", area="高円寺", time_band="深夜", job_type="カフェ")
    assert r["total_now"] == 0 and r["options"] and r["options"][0]["total_after"] > 0


def test_job_types_list_matches_the_job_data():
    # 職種の一覧は雇用区分ごと。区分ごとに、求人の職種と一覧が完全に一致する(PR #25 以来の決まりを区分ごとに)
    jobs = logic.load_jobs().values()
    for employment_type, types in logic.JOB_TYPES_BY_EMPLOYMENT.items():
        assert set(types) == {j.job_type for j in jobs if j.employment_type == employment_type}
    assert logic.JOB_TYPES_BY_EMPLOYMENT["アルバイト・パート"] == logic.JOB_TYPES
    assert not set(logic.JOB_TYPES) & set(logic.FULLTIME_JOB_TYPES)  # 同じ名前の職種を区分をまたいで使わない


@pytest.mark.parametrize("job_type", ["飲食・カフェ", "飲食", "カフェ店員"])
def test_recommend_and_relax_return_the_job_type_list_for_unknown_job_type(state, job_type):
    # F-follow-new#1: エージェントが選択肢の文言(飲食・カフェ)を渡し、渋谷にカフェが2件あるのに0件と答えた
    for r in (logic.recommend(state, "sim-new", area="渋谷", job_type=job_type),
              logic.relax(state, "sim-new", area="渋谷", job_type=job_type)):
        assert "jobs" not in r and "total_now" not in r  # 0件として返さない
        assert job_type in r["error"]
        assert r["job_types"] == {"アルバイト・パート": list(logic.JOB_TYPES), "正社員": list(logic.FULLTIME_JOB_TYPES)}


def test_areas_list_matches_the_job_data():
    assert set(logic.AREAS) == {j.area for j in logic.load_jobs().values()}
    assert set(logic.TOKYO_AREAS) == set(logic.AREAS) - {"横浜", "川崎", "大宮"}  # 神奈川・埼玉を除いた東京都の地名


@pytest.mark.parametrize("area", ["原宿", "東京都内23区", "関東"])
def test_recommend_and_relax_return_the_area_list_for_unknown_area(state, area):
    # voice-resume の会話(2026-09-26)で、一覧に無いエリアがどんな条件でも黙って0件になった
    for r in (logic.recommend(state, "sim-new", area=area, job_type="カフェ"),
              logic.relax(state, "sim-new", area=area, job_type="カフェ")):
        assert "jobs" not in r and "total_now" not in r  # 0件として返さない
        assert area in r["error"] and r["areas"] == list(logic.AREAS)


def test_recommend_expands_wide_area_phrases(state):
    # 会話(2026-09-26)の最後の条件(東京都内・日中・カフェ)。エリアを外せば5件あった
    r = logic.recommend(state, None, area="東京都内", job_type="カフェ", time_band="日中")
    assert r["total"] >= 1 and all(j["area"] in logic.TOKYO_AREAS for j in r["jobs"])
    # 「都内」に神奈川(横浜・川崎)・埼玉(大宮)の求人を混ぜない
    tokyo = logic.recommend(state, None, area="都内", job_type="カフェ", limit=3)
    assert tokyo["total"] < logic.recommend(state, None, job_type="カフェ")["total"]
    # 「どこでも」はエリアの絞り込みなしと同じ
    assert (logic.recommend(state, None, area="どこでも", job_type="カフェ")["total"]
            == logic.recommend(state, None, job_type="カフェ")["total"])


def test_recommend_matches_station_names_with_or_without_eki(state):
    # 駅名は「駅」の有無をそろえて照合する(求人データの station は表記が揺れている)
    assert logic.recommend(state, None, area="みなとみらい")["total"] >= 1
    assert (logic.recommend(state, None, area="新宿駅")["total"]
            == logic.recommend(state, None, area="新宿")["total"])


def test_recommend_matches_job_type_exactly(state):
    assert {j["job_id"] for j in logic.recommend(state, None, area="渋谷", job_type="カフェ")["jobs"]} == {"a003", "b0303"}
    assert all(j["job_type"] == "飲食ホール" for j in logic.recommend(state, None, job_type="飲食ホール")["jobs"])


# ----------------------------- 支援・記録 -----------------------------


def test_resume_form_rejects_pii_and_returns_liff(state):
    r = logic.resume_form(state, "sim-exit", "a002", {"希望開始日": "来週から", "電話番号": "090-1234-5678"})
    assert r["remaining_fields"] == ["電話番号"] and r["rejected_pii"] == ["電話番号"]
    assert r["liff_url"].startswith("/liff/apply")
    stored = state.user("sim-exit")["form_exits"][0]
    assert stored["completed"]["希望開始日"] == "来週から" and "電話番号" not in stored["completed"]


def test_resume_form_rejects_pii_in_values_of_other_fields(state):
    r = logic.resume_form(state, "sim-exit", "a002", {"希望開始日": "来週 090-1234-5678に電話ください"})
    assert r["rejected_pii"] == ["希望開始日"]
    assert "希望開始日" not in state.user("sim-exit")["form_exits"][0]["completed"]


def test_outcome_reason_is_scrubbed_of_pii(state):
    logic.record_outcome(state, "sim-yoru", "consult", "電話は090-1234-5678、メールは a.b@example.com", 0.5)
    reason = state.user("sim-yoru")["outcomes"][-1]["reason"]
    assert "090-1234-5678" not in reason and "example.com" not in reason


def test_liff_submit_turns_exit_into_application(state):
    logic.submit_application(state, "sim-exit", "a002")
    assert logic.form_exit(state, "sim-exit") is None
    assert logic.conversation_summary(state, "sim-exit")["segment"] == "applied_waiting"


def test_wait_outcome_is_recorded_without_side_effects(state):
    r = logic.record_outcome(state, "sim-yoru", "wait", "今はいい", 0.8)
    assert r["recorded"]["outcome"] == "wait"
    assert state.user("sim-yoru")["outcomes"][-1]["reason"] == "今はいい"
    assert logic.conversation_summary(state, "sim-yoru")["segment"] == "browsing"  # 記録しても状況は変わらない
    assert "error" in logic.record_outcome(state, "sim-yoru", "spam", "", 1)
    assert "error" in logic.record_outcome(state, "sim-yoru", "stop", "もう送らないで", 0.9)  # stop は無くなった


def test_wait_without_refusal_words_is_not_recorded(state):
    # 「別のこと」を選んだだけの人を wait にしない(断っていない人を wait と記録しない。spike 2026-09-25)
    r = logic.record_outcome(state, "sim-yoru", "wait", "別のことを選んだ。内容は未確認", 0.6)
    assert "error" in r and not state.user("sim-yoru").get("outcomes")
    # 「考えます」は迷いのサインで、断りではない(迷いのポイントを聞いて支援する)
    assert "error" in logic.record_outcome(state, "sim-yoru", "wait", "ありがとう、考えます", 0.6)
    assert logic.record_outcome(state, "sim-yoru", "wait", "もう送らないで", 0.9)["recorded"]["outcome"] == "wait"
    assert logic.record_outcome(state, "sim-yoru", "consult", "面接の質問", 0.8)["recorded"]["outcome"] == "consult"


# ----------------------------- 案件への質問(案件単位の FAQ) -----------------------------


def test_job_question_is_answered_from_that_jobs_faq_with_source():
    r = logic.answer_material("制服はありますか?", "a003")
    top = r["hits"][0]
    assert top["source"] == "案件の FAQ" and "エプロン" in top["a"]
    assert "note" not in r and r["card"]["job_id"] == "a003"
    # 別の案件の FAQ(a001 の法被)は混ざらない
    assert not any("法被" in h["a"] for h in r["hits"])


def test_job_question_falls_back_to_job_description_then_general_faq():
    desc = logic.answer_material("スペシャルティコーヒーのお店ですか?", "a003")
    sources = [h["source"] for h in desc["hits"]]
    assert "求人の説明" in sources and "全体の FAQ" not in sources[:sources.index("求人の説明")]
    general = logic.answer_material("日払いはできますか?", "a003")
    assert any(h["source"] == "全体の FAQ" and h.get("id") == "faq-daily-pay" for h in general["hits"])


def test_job_question_with_no_basis_says_so():
    r = logic.answer_material("ペットを連れて行けますか?", "a003")
    assert r["hits"] == [] and "情報にない" in r["note"]


def test_job_faq_does_not_promise_perks_the_job_lacks():
    """案件の FAQ が、求人の待遇(perks)に無いこと(未経験OK・髪色自由)を、あるかのように答えていない。"""
    from dip_mcp.data import load_job_faq, load_jobs

    jobs = load_jobs()
    bad = []
    for job_id, faq in load_job_faq().items():
        perks = jobs[job_id].perks
        for f in faq:
            if "未経験OK" not in perks and any(w in f["a"] for w in ("未経験の方も歓迎", "未経験でも大丈夫", "未経験の方がほとんど")):
                bad.append((job_id, f["a"]))
            if "髪色自由" not in perks and any(w in f["a"] for w in ("髪色は問いません", "髪色は自由", "髪色やネイルの厳しい規定は特に")):
                bad.append((job_id, f["a"]))
    assert bad == []


def test_common_hiragana_words_alone_do_not_match():
    # 「ありますか」だけでは、どの FAQ にも当たらない(半角・全角の「?」どちらでも)
    assert logic.answer_material("ありますか?", "a003")["hits"] == []
    assert logic.answer_material("ありますか？", "b0401")["hits"] == []
    r = logic.answer_material("ペットを連れて行けますか？", "b0401")
    assert r["hits"] == [] and "情報にない" in r["note"]


def test_a_reworded_question_about_a_job_points_to_the_jobs_own_data():
    # LLM が「時給」を長い文に言い換えると検索は当たらない。それでも求人の項目(時給1550円)で答えさせる
    r = logic.answer_material("イタリアンバルのキッチンスタッフの時給はいくらですか", "b0205")
    assert r["hits"] == [] and r["job"]["pay"]["min"] == 1550 and "job(" in r["note"]
    assert "job(" not in logic.answer_material("ペットを連れて行けますか?")["note"]  # 求人の指定が無ければ従来どおり


def test_hiragana_only_question_still_finds_the_answer():
    r = logic.answer_material("まかないはある?", "a001")
    assert r["hits"] and r["hits"][0]["source"] == "案件の FAQ" and "まかない" in r["hits"][0]["a"]


def test_a_strong_match_is_not_pushed_out_by_single_word_matches():
    # 「時間」の1語だけ重なる FAQ より、勤務時間そのものが先に来る
    assert logic.answer_material("勤務時間は?", "a003")["hits"][0]["a"].startswith("勤務時間:")


def test_the_jobs_own_answer_comes_before_the_general_faq():
    top = logic.answer_material("交通費は出る?", "b0604")["hits"][0]
    assert top["source"] != "全体の FAQ"


# ----------------------------- 本人が話した希望(長期メモリ) -----------------------------


def test_preferences_are_saved_and_returned_in_summary_next_time(state):
    r = logic.save_preferences(state, "sim-yoru", {"time_band": "夜", "area": "新宿", "min_wage": "1200"})
    assert r["saved"] == ["time_band", "area", "min_wage"]
    prefs = logic.conversation_summary(state, "sim-yoru")["stated_preferences"]
    assert prefs["time_band"]["value"] == "夜" and prefs["min_wage"]["value"] == "1200" and prefs["area"]["saved"] == "9/23"
    logic.save_preferences(state, "sim-yoru", {"area": "渋谷"})  # 言い直したら上書き
    assert logic.conversation_summary(state, "sim-yoru")["stated_preferences"]["area"]["value"] == "渋谷"


def test_preferences_for_unlinked_user_are_kept_by_line_id(state):
    logic.save_preferences(state, "sim-new", {"job_type": "カフェ"})
    s = logic.conversation_summary(state, "sim-new")
    assert s["linked"] is False and s["stated_preferences"]["job_type"]["value"] == "カフェ"


def test_preferences_accept_numbers_and_reject_addresses(state):
    r = logic.save_preferences(state, "sim-yoru", {"min_wage": 1200, "days_per_week": 3, "area": "渋谷区神南1-2-3"})
    assert r["saved"] == ["min_wage", "days_per_week"] and r["rejected"] == ["area"]
    assert logic.conversation_summary(state, "sim-yoru")["stated_preferences"]["min_wage"]["value"] == "1200"


def test_skills_and_certifications_are_saved_as_preferences(state):
    """「できること」「資格」も本人の言葉のまま保存され、次の要約に出る(task 41)。"""
    r = logic.save_preferences(state, "sim-yoru", {"skills": "レジ打ちと品出し", "certifications": "普通自動車免許"})
    assert r["saved"] == ["skills", "certifications"] and r["rejected"] == []
    prefs = logic.conversation_summary(state, "sim-yoru")["stated_preferences"]
    assert prefs["skills"]["value"] == "レジ打ちと品出し"
    assert prefs["certifications"]["value"] == "普通自動車免許"


def test_skills_and_certifications_reject_pii_values(state):
    """新しい項目でも、電話番号・メール・住所らしい値を弾く決まりはそのまま効く(task 41)。"""
    r = logic.save_preferences(state, "sim-yoru", {
        "skills": "調理 連絡は 090-1234-5678 まで",
        "certifications": "取得証明は taro@example.com に",
    })
    assert r["saved"] == [] and set(r["rejected"]) == {"skills", "certifications"}
    assert "skills" not in state.user("sim-yoru").get("preferences", {})


def test_preferences_reject_unknown_keys_and_pii(state):
    r = logic.save_preferences(state, "sim-yoru", {"電話番号": "090-1234-5678", "area": "新宿 090-1234-5678", "time_band": "夜"})
    assert r["saved"] == ["time_band"] and set(r["rejected"]) == {"電話番号", "area"}
    assert "電話番号" not in state.user("sim-yoru")["preferences"]


# ----------------------------- プッシュ(dip の行動イベント → こちらから話しかける) -----------------------------


def test_push_denied_for_unlinked_and_unknown_trigger(state):
    assert not logic.push_trigger(state, "sim-new", "form_exit")["allowed"]  # 未連携
    assert not logic.push_trigger(state, "sim-yoru", "spam")["allowed"]


def test_push_frequency_cap_and_contact_is_recorded(state):
    r = logic.push_trigger(state, "sim-exit", "form_exit")
    assert r["allowed"]
    assert state.user("sim-exit")["contacts"][-1]["channel"] == "LINE AI通知"
    again = logic.push_trigger(state, "sim-exit", "form_exit")
    assert not again["allowed"] and "日以内" in again["reason"]


def test_push_needs_a_fact_to_talk_about(state):
    assert not logic.push_trigger(state, "sim-applied", "form_exit")["allowed"]  # 途中のフォームがない


def test_push_form_exit_facts(state):
    r = logic.push_trigger(state, "sim-exit", "form_exit")
    assert r["allowed"] and r["facts"]["job_id"] == "a002" and "電話番号" in r["facts"]["remaining_fields"]


def test_recommend_can_limit_to_new_postings(state):
    ids = [j["job_id"] for j in logic.recommend(state, "sim-yoru", posted_within_days=2)["jobs"]]
    assert set(ids) == {"b0102", "b0103", "b0205"}


def test_new_postings_keep_reasons_from_past_applications(state):
    # 新着だけに絞っても、7月に応募した飲食ホールと同系統、という本人の履歴に基づく理由は付く
    b0103 = next(j for j in logic.recommend(state, "sim-yoru", posted_within_days=2)["jobs"] if j["job_id"] == "b0103")
    assert "7月に応募した飲食ホールと同系統" in b0103["reasons"]


# ----------------------------- MCP 境界 -----------------------------


def test_mcp_server_exposes_tools_and_scenario_prompt(tmp_path):
    """実際の MCP クライアント(stdio)でサーバーに接続し、ツール一覧・ツール呼び出し・Prompt を確認する。"""
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langchain_mcp_adapters.prompts import load_mcp_prompt
    from langchain_mcp_adapters.tools import load_mcp_tools

    env = {**os.environ, "DIP_STATE_PATH": str(tmp_path / "state.json")}
    client = MultiServerMCPClient({"dip": {"transport": "stdio", "command": sys.executable,
                                           "args": ["-m", "dip_mcp.server"], "cwd": str(ROOT), "env": env}})

    async def go():
        async with client.session("dip") as session:
            tools = {t.name: t for t in await load_mcp_tools(session)}
            summary = await tools["get_user_conversation_summary"].ainvoke({"line_user_id": "sim-applied"})
            prompt = await load_mcp_prompt(session, "baitoru_assistant", arguments={"entry": "D"})
            return tools, summary, prompt

    tools, summary, prompt = asyncio.run(go())
    assert {"get_user_conversation_summary", "recommend_jobs", "resume_application_form",
            "save_user_preferences", "record_conversation_outcome"} <= set(tools)
    assert "applied_waiting" in str(summary)
    assert "応募フォーム離脱" in prompt[0].content and "1回の返信で質問は1つ" in prompt[0].content


def test_search_tools_list_the_job_types_in_the_job_type_description():
    from dip_mcp import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("recommend_jobs", "relax_conditions"):
        desc = tools[name].inputSchema["properties"]["job_type"]["description"]
        assert all(t in desc for t in logic.JOB_TYPES) and "1つずつ検索" in desc


def test_search_tools_list_the_areas_in_the_area_description():
    from dip_mcp import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("recommend_jobs", "relax_conditions"):
        desc = tools[name].inputSchema["properties"]["area"]["description"]
        assert all(a in desc for a in logic.AREAS)
        assert all(g in desc for g in logic.AREA_GROUPS)  # 「東京都内」「どこでも」などの広い言い方
