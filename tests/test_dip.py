"""dip 側(MCP サーバー)のオフラインテスト。LLM は使わない。

    ./.venv/bin/python -m pytest -q
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from dip_mcp import behavior, logic
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


def test_recommend_mixing_both_employment_types_does_not_score_time_band(state):
    """区分が未定で両方の区分を並べるときは、時間帯の傾向を点にしない(正社員の求人には時間帯が無く、必ず0点で下に偏る)。"""
    mixed = logic.recommend(state, "sim-yoru", limit=3)
    assert mixed["total"] > 0 and all(j["score_breakdown"]["time_band"] == 0.0 for j in mixed["jobs"])
    # 負例: アルバイト・パートだけを並べるときは、今までどおり時間帯の傾向を点にする
    ap = logic.recommend(state, "sim-yoru", employment_type="アルバイト・パート")
    assert ap["jobs"][0]["score_breakdown"]["time_band"] == 1.0
    # 負例: 区分が未定でも、職種で絞って候補がアルバイト・パートだけなら点にする
    one = logic.recommend(state, "sim-yoru", job_type="飲食ホール")
    assert one["jobs"][0]["score_breakdown"]["time_band"] == 1.0


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


# ----------------------------- 推薦の理由に本人の言葉(task 229) -----------------------------


@pytest.fixture
def offline_keywords(monkeypatch):
    """keywords の意味検索の API を呼ばせない(部分一致だけ)。意味検索を使うテストは _api_embed を上書きする。"""
    from dip_mcp import embedding

    def _fail(*_a, **_kw):
        raise ConnectionError("offline")

    embedding._query_cache.clear()
    monkeypatch.setattr(embedding, "_api_embed", _fail)
    yield embedding
    embedding._query_cache.clear()


def test_reasons_tell_the_users_experience_with_a_fixed_word(state, offline_keywords):
    # 経験「カフェでホールを2年」を保存し、経験と希望「カフェ」で探した。渋谷のカフェホール(a003)の職種は「カフェ」
    logic.save_preferences(state, "u-story", {"experience": "カフェでホールを2年", "job_type": "カフェ"})
    r = logic.recommend(state, "u-story", area="渋谷", keywords="カフェでホールを2年 カフェ")
    card = next(j for j in r["jobs"] if j["job_id"] == "a003")
    assert card["reasons"] == ["カフェのご経験を活かせます", "渋谷エリア"]


def test_reasons_use_only_what_the_user_said_about_themselves(state, offline_keywords):
    # 同じ keywords でも、やりたいこと・経験として保存された言葉が無ければ(条件の職種だけなら)今までどおり
    logic.save_preferences(state, "u-cond", {"job_type": "カフェ"})
    for uid in ("u-cond", None):
        card = next(j for j in logic.recommend(state, uid, area="渋谷", keywords="カフェでホールを2年 カフェ")["jobs"]
                    if j["job_id"] == "a003")
        assert not any("お話" in x or "ご経験" in x for x in card["reasons"])


def test_reasons_stay_as_before_when_no_word_of_the_user_is_in_the_job(state, offline_keywords, monkeypatch):
    # 意味が近いだけで選ばれた求人(b0701 の倉庫。「もくもくと働きたい」に職種の語は無い)には、本人の話とのつながりを作らない
    monkeypatch.setattr(offline_keywords, "_api_embed",
                        lambda *a, **kw: offline_keywords._vectors()["jobs"]["b0701"]["v"])
    logic.save_preferences(state, "u-quiet", {"wants": "もくもくと働きたい"})
    r = logic.recommend(state, "u-quiet", keywords="もくもくと働きたい")
    jobs, acts = logic.load_jobs(), behavior.view(state, "u-quiet")
    assert r["jobs"][0]["job_id"] == "b0701"
    for card in r["jobs"]:
        assert card["reasons"] == logic._reasons(jobs[card["job_id"]], None, None, None, acts, jobs, logic.now("u-quiet"))


def test_reasons_without_keywords_are_unchanged(state):
    r = logic.recommend(state, "sim-yoru", area="新宿", time_band="夜")
    assert r["jobs"][0]["reasons"][0] == "7月に応募した飲食ホールと同系統"
    assert not any("お話" in x or "ご経験" in x for j in r["jobs"] for x in j["reasons"])


def test_reason_vocab_is_the_words_of_the_job_type():
    # 語彙は職種の名前の語だけ。どの職種にもまたがる広い語(スタッフ・管理・サービス・技術)は入れない
    # 「・」で切った部分の全体(職種の名前そのもの)も入れて、先に置く(task 229 の 3 回目の自動レビュー)
    assert logic.reason_vocab("飲食ホール") == ["飲食ホール", "飲食", "ホール"]
    assert logic.reason_vocab("アパレル販売") == ["アパレル販売", "アパレル", "販売"]
    assert logic.reason_vocab("オフィス事務") == ["オフィス事務", "事務"]
    assert logic.reason_vocab("イベントスタッフ") == ["イベントスタッフ", "イベント"]
    assert logic.reason_vocab("事務・管理") == ["事務"]
    assert logic.reason_vocab("企画・PM") == ["企画", "PM"]
    assert logic.reason_vocab("カフェ") == ["カフェ"]
    vocab = {w for t in logic.JOB_TYPES + logic.FULLTIME_JOB_TYPES for w in logic.reason_vocab(t)}
    assert not vocab & {"未経験", "経験", "スタッフ", "オフィス", "管理", "サービス", "技術"}


def _reason(job_id, keywords, **said):
    return logic._keyword_reason(logic.load_jobs()[job_id], keywords, said)


@pytest.mark.parametrize(("job_id", "keywords", "said", "reason"), [
    ("a003", "カフェ", {"wants": "カフェ"}, "カフェの仕事をしたいというお話に合います"),
    ("a003", "渋谷 カフェ", {"wants": "渋谷のカフェで働きたい"}, "カフェの仕事をしたいというお話に合います"),
    ("a001", "居酒屋のホールを1年やっていました", {"experience": "居酒屋のホールを1年やっていました"}, "ホールのご経験を活かせます"),
    ("a002", "コンビニ", {"wants": "コンビニ。夜勤ムリ"}, "コンビニの仕事をしたいというお話に合います"),  # 語の全体で見る
    # やりたいことを経験より先に見る(経験の「カフェは未経験」は使わない)
    ("a003", "カフェ 未経験", {"wants": "カフェで働きたい", "experience": "カフェは未経験"}, "カフェの仕事をしたいというお話に合います"),
    ("f0101", "営業 PM", {"experience": "法人営業ではなく営業事務", "wants": "営業"}, "営業の仕事をしたいというお話に合います"),
    # 職種の名前そのものを話して探したとき。長い語を先に見る(task 229 の 3 回目の自動レビュー)
    ("b0601", "アパレル販売", {"experience": "アパレル販売を2年"}, "アパレル販売のご経験を活かせます"),
    ("b0601", "アパレル販売を2年", {"experience": "アパレル販売を2年"}, "アパレル販売のご経験を活かせます"),
    ("b1201", "オフィス事務", {"experience": "オフィス事務を3年"}, "オフィス事務のご経験を活かせます"),
    ("b1101", "イベントスタッフ", {"wants": "イベントスタッフをやりたい"}, "イベントスタッフの仕事をしたいというお話に合います"),
    ("b0101", "飲食ホール", {"experience": "飲食ホールを1年"}, "飲食ホールのご経験を活かせます"),
    ("b0601", "アパレル 販売", {"experience": "アパレルで販売を2年"}, "アパレルのご経験を活かせます"),  # 分けて話せば今までどおり
    # 避けたいことの「アパレル販売」は、語彙にアパレル販売の無い職種(スーパー・販売)の「販売」を止めない
    ("b0501", "販売", {"wants": "販売の仕事", "avoids": "アパレル販売はもうやりたくない"}, "販売の仕事をしたいというお話に合います"),
])
def test_keyword_reason_is_a_fixed_sentence_with_a_word_of_the_vocab(job_id, keywords, said, reason):
    assert _reason(job_id, keywords, **said) == reason


@pytest.mark.parametrize(("job_id", "keywords", "said"), [
    # task 229 の自動レビューの例。語彙に無い語(夜勤・接客)と、長い語の一部(飲食未経験の飲食)は理由にしない
    ("a002", "コンビニ 夜勤", {"wants": "夜勤ムリ"}),
    ("a002", "コンビニ 夜勤", {"wants": "夜勤ナシ"}),
    ("a002", "夜勤", {"wants": "夜勤なし"}),
    ("b0101", "未経験 ホール", {"experience": "飲食未経験"}),
    ("b0101", "未経験 飲食", {"experience": "飲食未経験"}),
    ("b0601", "未経験 ホール", {"experience": "飲食未経験", "wants": "ホール"}),  # アパレルの職種にホールは無い
    ("b0505", "未経験 カフェ", {"experience": "接客未経験"}),
    ("a002", "コンビニ", {"wants": "コンビニムリ"}),  # カタカナどうしで続く語は語の全体ではない
    ("a003", "カフェ", {"experience": "カフェは未経験"}),  # 経験の項目に未経験の印があれば見ない
    # task 229 の 2 回目の自動レビューの例。否定が別の句にあっても、経験の項目を丸ごと見ない
    ("b0105", "未経験 飲食", {"experience": "飲食 未経験"}),
    ("a003", "未経験 カフェ", {"experience": "カフェ、未経験"}),
    ("a003", "未経験 カフェ", {"experience": "カフェは、未経験です"}),
    ("b0105", "未経験 ホール", {"experience": "ホール/未経験"}),
    ("b0505", "販売", {"experience": "販売を3年、飲食は未経験"}),  # 経験のある句があっても、迷ったら付けない
    # 未経験と言わずに初めてと話したとき
    ("a003", "カフェ", {"experience": "カフェは初めて"}),
    ("a003", "カフェ", {"experience": "カフェははじめてです"}),
    ("a003", "カフェ", {"experience": "カフェは経験ゼロ"}),
    ("a003", "カフェ", {"experience": "カフェはしたことがなくて"}),
    ("a003", "カフェ", {"experience": "カフェで働いたことがなく"}),
    ("a003", "カフェ", {"experience": "カフェはまだ"}),
    ("a003", "カフェ", {"experience": "カフェは初心者"}),
    ("a003", "カフェ", {"experience": "カフェ経験なし"}),
    ("a003", "カフェ", {"experience": "カフェで働いたことはない"}),  # 経験と言い切る形なので、否定の印のある句も見ない
    ("b0505", "販売", {"experience": "販売はしたことがありません"}),
    ("a001", "飲食", {"experience": "飲食はやったことがなかった"}),
    # 避けたいこと・不安にある語は、前向きの項目にあっても使わない
    ("a002", "コンビニ", {"wants": "コンビニ", "avoids": "コンビニの夜勤"}),
    ("a003", "カフェ", {"experience": "カフェで2年", "concerns": "カフェは立ち仕事がきつい"}),
    # keywords に無い語(意味が近いだけで選ばれた)、前向きの項目に無い語、条件の項目だけ
    ("a003", "居酒屋", {"wants": "カフェで働きたい"}),
    ("a003", "カフェ", {"experience": "居酒屋のホール"}),
    ("a003", "カフェ", {"job_type": "カフェ", "area": "渋谷"}),
    ("a003", "カフェ", {}),
    ("a003", None, {"wants": "カフェ"}),
    ("a003", "", {"wants": "カフェ"}),
    ("a003", "カフェホール", {"wants": "カフェホール"}),  # 長い語の一部(カフェホールのカフェ)は語の全体ではない
    # 職種の名前の全体でも、未経験や初めての印があれば付けない。避けたいことに一部の語があっても付けない
    ("b0601", "未経験 アパレル販売", {"experience": "アパレル販売未経験"}),
    ("b0601", "アパレル販売", {"experience": "アパレル販売は初めて"}),
    ("b1201", "オフィス事務", {"experience": "オフィス事務はしたことがない"}),
    ("b0601", "アパレル販売", {"wants": "アパレル販売", "avoids": "アパレルはノルマがきつそう"}),
    ("b0601", "販売", {"experience": "アパレル販売を2年"}),  # keywords に職種の名前の全体が無く、販売は長い語の一部
    # 短い語を当てるとき、それを含む職種の名前の全体が避けたいことにあっても付けない(task 229 の 4 回目の自動レビュー)
    ("b0605", "販売", {"wants": "販売の仕事", "avoids": "アパレル販売はもうやりたくない"}),
    ("b0101", "ホール", {"experience": "ホールを2年", "avoids": "飲食ホールはもう嫌"}),
    # 当てる語と重ならない語(販売を当てるときのアパレル)が避けたいことにあっても、その職種の求人には付けない(5 回目)
    ("b0605", "販売", {"wants": "販売の仕事", "avoids": "アパレルはもう嫌"}),
    ("b0101", "飲食 キッチン", {"wants": "飲食で働きたい", "avoids": "ホールは苦手"}),
    # 避けたいことの語が長い語の頭にあるとき(飲食店の飲食、コンビニムリのコンビニ)も付けない
    ("b0101", "ホール", {"experience": "ホールを2年", "avoids": "飲食店はもう嫌"}),
    ("a002", "コンビニ", {"wants": "コンビニ", "concerns": "コンビニムリかも"}),
])
def test_keyword_reason_is_none_unless_a_vocab_word_was_said_and_searched(job_id, keywords, said):
    assert _reason(job_id, keywords, **said) is None


def test_no_card_gets_a_made_up_reason_for_a_first_timer(state, offline_keywords):
    # 自動レビューの E2E: 経験「飲食未経験」、keywords は Prompt の決まりどおり「未経験」を入れる
    for uid, prefs, kw in [("u-new", {"experience": "飲食未経験", "wants": "ホール"}, "未経験 ホール"),
                           ("u-new2", {"experience": "接客未経験"}, "未経験 カフェ"),
                           ("u-new3", {"experience": "飲食 未経験"}, "未経験 飲食"),
                           ("u-n2", {"wants": "コンビニ。夜勤ムリ"}, "コンビニ 夜勤")]:
        logic.save_preferences(state, uid, prefs)
        for card in logic.recommend(state, uid, area="渋谷" if uid != "u-n2" else None, keywords=kw, limit=10)["jobs"]:
            assert not any("「未経」" in x or "夜勤" in x or "接客" in x for x in card["reasons"]), card  # 待遇の「未経験OK」は前から
            # 付くのは、やりたいことに語の全体である職種の語(ホール・コンビニ)と、その職種の求人だけ
            for x in card["reasons"]:
                if "お話" in x or "ご経験" in x:
                    assert (x, card["job_type"]) in {("ホールの仕事をしたいというお話に合います", "飲食ホール"),
                                                     ("コンビニの仕事をしたいというお話に合います", "コンビニ")}


def test_reasons_tell_the_job_type_the_user_named_as_experience(state, offline_keywords):
    # 3 回目の自動レビューの E2E: 職種の名前そのものを経験として話し、keywords にも入れて探した
    for uid, exp, kw, word in [("u-apparel", "アパレル販売を2年", "アパレル販売", "アパレル販売"),
                               ("u-office", "オフィス事務を3年", "オフィス事務", "オフィス事務")]:
        logic.save_preferences(state, uid, {"experience": exp})
        cards = logic.recommend(state, uid, keywords=kw, job_type=word)["jobs"]
        assert cards and all(c["job_type"] == word for c in cards)
        assert all(f"{word}のご経験を活かせます" in c["reasons"] for c in cards), cards


def test_attribute_reason_stays_first_and_the_users_words_come_next(state):
    job, jobs = logic.load_jobs()["a003"], logic.load_jobs()
    reasons = logic._reasons(job, "渋谷", None, None, behavior.view(state, None), jobs, logic.now(None),
                             {"max_talk_level": 3}, "カフェ", {"wants": "カフェ"})
    assert reasons == [logic.TALK_REASONS[job.talk_level], "カフェの仕事をしたいというお話に合います"]


def test_users_words_take_the_place_of_the_second_attribute_reason(state):
    # 属性の理由が2つあるときも、本人の話は1つ目の次。2つまでの制限で消えるのは2つ目の属性の理由
    job, jobs = logic.load_jobs()["a003"], logic.load_jobs()
    args = (job, "渋谷", None, None, behavior.view(state, None), jobs, logic.now(None),
            {"max_talk_level": 3, "max_physical_level": 2})
    assert logic._reasons(*args) == [logic.TALK_REASONS[job.talk_level], logic.PHYSICAL_REASONS[job.physical_level]]
    assert logic._reasons(*args, "カフェ", {"wants": "カフェ"}) == [
        logic.TALK_REASONS[job.talk_level], "カフェの仕事をしたいというお話に合います"]


def test_no_story_reason_on_a_job_type_the_user_wants_to_avoid(state, offline_keywords):
    # 5 回目の自動レビューの E2E: 避けたいことに職種の一部の語(アパレル)があり、別の語(販売)で探した
    logic.save_preferences(state, "u-avoid", {"wants": "販売の仕事", "avoids": "アパレルはもう嫌"})
    cards = logic.recommend(state, "u-avoid", keywords="販売", limit=10)["jobs"]
    story = "販売の仕事をしたいというお話に合います"
    assert {c["job_type"] for c in cards} >= {"アパレル販売", "スーパー・販売"}, cards
    for c in cards:
        assert (story in c["reasons"]) == (c["job_type"] == "スーパー・販売"), c


# ----------------------------- 支援・記録 -----------------------------


def test_resume_form_rejects_pii_and_returns_liff(state):
    r = logic.resume_form(state, "sim-exit", "a002", {"希望開始日": "来週から", "電話番号": "090-1234-5678"})
    assert r["remaining_fields"] == ["電話番号"] and r["rejected_pii"] == ["電話番号"]
    assert r["liff_url"].startswith("/liff/apply")
    assert logic.form_exit(state, "sim-exit")["completed_fields"] == ["氏名", "年齢", "希望開始日"]
    # dip に書くのは答えた項目名だけ(form_fill のイベント)。値は持たない
    assert state.user("sim-exit")[behavior.WRITES][-1] | {"seq": None, "at": None} == {
        "seq": None, "at": None, "kind": "form_fill", "job_id": "a002", "data": {"fields": ["希望開始日"]}, "dip_id": "D1002"}


def test_resume_form_rejects_pii_in_values_of_other_fields(state):
    r = logic.resume_form(state, "sim-exit", "a002", {"希望開始日": "来週 090-1234-5678に電話ください"})
    assert r["rejected_pii"] == ["希望開始日"]
    assert "希望開始日" not in logic.form_exit(state, "sim-exit")["completed_fields"]
    assert not state.user("sim-exit").get(behavior.WRITES)  # 何も書かない


@pytest.mark.parametrize("value", ["自宅は渋谷区神南1-2-3", "みほん市みほん町3丁目に住んでいます", "住所 みほん町３－５",
                                   "東京都みほん市1-2-3在住"])
def test_resume_form_rejects_address_like_values(state, value):
    """住所らしい値(住んでいる所を言う言葉と一緒のもの)は、項目名が個人情報の項目でなくても受け取らない
    (希望の保存・まとめと同じ決まり)。住む言葉の無い地名や番地の形だけでは住所とみなさない(task 211)。"""
    r = logic.resume_form(state, "sim-exit", "a002", {"希望開始日": value})
    assert r["rejected_pii"] == ["希望開始日"] and "希望開始日" in r["remaining_fields"]
    assert r["liff_url"].startswith("/liff/apply") and "個人情報" in r["note"]
    assert "希望開始日" not in logic.form_exit(state, "sim-exit")["completed_fields"]
    assert not state.user("sim-exit").get(behavior.WRITES)  # 「入力済み」にしない


@pytest.mark.parametrize("value", ["2026-10-01から", "1-2週間後", "来週から"])
def test_resume_form_accepts_dates_and_ranges_that_are_not_addresses(state, value):
    r = logic.resume_form(state, "sim-exit", "a002", {"希望開始日": value})
    assert r["rejected_pii"] == [] and "希望開始日" not in r["remaining_fields"]


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
    # 「考えます」は断りではない(お礼つきなら会話の終わりとして締め、迷いを言っていれば迷いのポイントを聞く)
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


# ----------------------------- 本人が話した希望(プロフィール) -----------------------------


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
    r = logic.save_preferences(state, "sim-yoru", {"min_wage": 1200, "days_per_week": 3, "area": "自宅は渋谷区神南1-2-3"})
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
    assert behavior.view(state, "sim-exit")["last_contact"]["channel"] == "LINE AI通知"
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
