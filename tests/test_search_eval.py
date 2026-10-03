"""検索評価セット(evals/search_eval_set.json)と再現率の計算のオフラインテスト。実 API は呼ばない。

求人ベクトルは fixtures/job_vectors.json、問い合わせのベクトルは evals/search_eval.py record で記録した
fixtures/search_query_vectors.json を使う。評価器(relevant_ids・recall_at_k・unrelated_ok)の負例も置く
(正しい結果を取りこぼし・不合格扱いにしないこと)。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from dip_mcp import embedding, logic
from dip_mcp.data import EMPLOYMENT_TYPES, State, load_jobs, load_users
from evals import search_eval

# task 105 の前の設定(text-embedding-3-small・512 次元・給与まで入れた文面・下限 0.30・重み 6)の再現率は 0.551。
# task 105 で選んだ設定は 0.753(task 111 はこれを下回らない)
BASELINE_RECALL = 0.551
TASK105_RECALL = 0.753
ARGS = {*logic.ATTRIBUTE_ARGS, *logic.FULLTIME_STYLE_ARGS, "min_annual_salary"}


@pytest.fixture(autouse=True)
def _no_real_api(monkeypatch):
    embedding._query_cache.clear()
    monkeypatch.setattr(embedding, "_api_embed",
                        lambda *a, **kw: pytest.fail("テストから実 API(_api_embed)を呼んではいけない"))
    yield
    embedding._query_cache.clear()


@pytest.fixture(scope="module")
def report():
    return search_eval.evaluate(search_eval.recorded_query_vectors()["queries"])


def _case(report, cid):
    return next(r for r in report["cases"] if r["id"] == cid)


# ----------------------------- 評価セットと記録の整合 -----------------------------

def test_eval_set_is_well_formed():
    jobs, cases = load_jobs(), search_eval.load_cases()
    assert 30 <= len(cases) <= 50 and len({c["id"] for c in cases}) == len(cases)
    assert {c.get("employment_type") for c in cases} == {*EMPLOYMENT_TYPES, None}  # 両方の区分と未定を含む
    assert {c["kind"] for c in cases} == {"言い換え", "否定", "経歴", "条件", search_eval.UNRELATED}
    for c in cases:
        exp = c["expected"]
        assert set(c.get("args", {})) <= ARGS, c["id"]  # recommend の引数だけ
        assert c.get("user") is None or c["user"] in load_users(), c["id"]
        if c["kind"] == search_eval.UNRELATED:
            assert exp == {} and search_eval.case_keywords(c), c["id"]  # 正解を持たず、keywords で探す
            continue
        assert set(exp) <= {"job_ids", "job_types", "perks", "time_bands", "remote", "weekends_holidays",
                            "max_overtime_hours", "min_annual_salary"}, c["id"]
        assert set(exp.get("job_ids", ())) <= set(jobs), c["id"]  # 無い求人を正解にしない
        types = logic.JOB_TYPES + logic.FULLTIME_JOB_TYPES
        assert set(exp.get("job_types", ())) <= set(types), c["id"]
        assert search_eval.relevant_ids(c, jobs), f"{c['id']} の正解が0件"
        assert search_eval.case_keywords(c) or c.get("args"), c["id"]  # 何かで探す


def test_recorded_query_vectors_match_the_job_vectors():
    """モデル・次元を変えて求人ベクトルだけ作り直したら、問い合わせも記録し直す(evals/search_eval.py record --all)。
    評価セットに keywords の問い合わせを足したら記録する(evals/search_eval.py record)。"""
    rec, meta = search_eval.recorded_query_vectors(), embedding._vectors()
    assert (rec["model"], rec["dimensions"]) == (meta["model"], meta["dimensions"]) == (embedding.MODEL, embedding.DIMENSIONS)
    assert search_eval.eval_queries() <= set(rec["queries"])
    assert all(len(v) == rec["dimensions"] for v in rec["queries"].values())


# ----------------------------- 今の設定の質 -----------------------------

def test_recall_is_above_the_baseline(report):
    assert report["recall_at_3"] >= TASK105_RECALL > BASELINE_RECALL
    assert report["recall_at_3_task105"] >= TASK105_RECALL  # task 105 の問い合わせだけでも下回らない
    assert report["zero_hits"] == 0  # 下限で正解ごと落として0件にしない


@pytest.mark.parametrize("cid", ["quiet-ap", "quiet-any"])
def test_quiet_work_query_ranks_quiet_jobs_not_call_centers(report, cid):
    """「人と話さず黙々と」で、コールセンターではなく入力・検品などの仕事が上位に出る(2026-09-27 の不具合)。"""
    r = _case(report, cid)
    assert sum(ok for _, _, ok in r["top"]) >= 2, r["top"]
    assert all(t != "コールセンター" for _, t, _ in r["top"]), r["top"]


def _jobs_of(r):
    jobs = load_jobs()
    return [jobs[i] for i, _, _ in r["top"]]


@pytest.mark.parametrize("cid", ["move-ap", "move-any", "move-ft"])
def test_move_body_query_ranks_physical_jobs(report, cid):
    """「体を動かしたい」(min_physical_level=3)が0件にならず、体を使う仕事だけが上位3件に並び、2件以上が正解
    (2026-09-27 の不具合。正社員は task 105 で embedding では分けられず、task 111 で属性で直した)。"""
    r = _case(report, cid)
    assert len(r["top"]) == 3 and sum(ok for _, _, ok in r["top"]) >= 2, r["top"]
    assert all(j.physical_level == 3 for j in _jobs_of(r)), r["top"]
    assert not {t for _, t, _ in r["top"]} & {"オフィス事務", "コールセンター", "企画・PM", "エンジニア", "クリエイティブ"}


def test_fulltime_quiet_query_ranks_quiet_jobs(report):
    """正社員の「人と話さず黙々と」(max_talk_level=1)で、画面や検査に向かう仕事が上位に出る(task 105 の strict xfail を直した)。"""
    r = _case(report, "quiet-ft")
    assert len(r["top"]) == 3 and sum(ok for _, _, ok in r["top"]) >= 2, r["top"]
    assert all(j.talk_level == 1 for j in _jobs_of(r)) and not {t for _, t, _ in r["top"]} & {"営業", "販売・サービス"}


def test_sit_query_ranks_seated_jobs_not_event_setup(report):
    """「立ち仕事は避けたい」(max_physical_level=1)で、会場設営・搬入補助のような立ち仕事が出ない(task 105 では 0.00)。"""
    r = _case(report, "sit-ap")
    assert len(r["top"]) == 3 and sum(ok for _, _, ok in r["top"]) >= 2, r["top"]
    assert all(j.physical_level == 1 for j in _jobs_of(r)), r["top"]
    assert not {t for _, t, _ in r["top"]} & {"イベントスタッフ", "倉庫・軽作業", "飲食ホール", "コンビニ"}


def test_no_service_query_ranks_jobs_without_customers(report):
    """「接客はしたくない」(max_talk_level=2)で、飲食ホール・コンビニ・販売など接客の仕事が出ない(task 105 では上位3件がすべて飲食ホール)。"""
    r = _case(report, "no-service-ap")
    assert len(r["top"]) == 3 and sum(ok for _, _, ok in r["top"]) >= 2, r["top"]
    assert all(j.talk_level <= 2 for j in _jobs_of(r)), r["top"]
    assert not {t for _, t, _ in r["top"]} & {"飲食ホール", "カフェ", "コンビニ", "アパレル販売", "コールセンター"}


def test_no_talk_query_ranks_quiet_jobs_not_service_or_call_centers(report):
    """「人と話すのが苦手」(max_talk_level=1)で、接客やコールセンターが出ない(task 105 では 0.33 で、両方が入っていた)。"""
    r = _case(report, "no-talk-ap")
    assert len(r["top"]) == 3 and all(ok for _, _, ok in r["top"]), r["top"]
    assert all(j.talk_level == 1 for j in _jobs_of(r))
    assert not {t for _, t, _ in r["top"]} & {"コールセンター", "飲食ホール", "イベントスタッフ", "塾講師・家庭教師"}


@pytest.mark.parametrize("cid", ["remote-ft", "zaitaku-ft"])
def test_remote_query_ranks_remote_jobs(report, cid):
    """「家で働きたい」「在宅で働きたい」(remote=可)で、原則在宅の求人だけが上位3件に並ぶ(task 105 では 0.33)。"""
    r = _case(report, cid)
    assert len(r["top"]) == 3 and r["recall"] == 1.0, r["top"]
    assert all(j.work_style.remote == "可" for j in _jobs_of(r))


def test_weekends_and_overtime_queries_keep_only_matching_jobs(report):
    weekends, overtime = _case(report, "weekends-ft"), _case(report, "overtime-ft")
    assert len(weekends["top"]) == 3 and all(j.holidays.weekends_holidays for j in _jobs_of(weekends))
    assert len(overtime["top"]) == 3 and all(j.work_style.overtime_hours <= 10 for j in _jobs_of(overtime))


def test_salary_query_does_not_rank_jobs_whose_middle_is_below_the_ask(report):
    """「年収500万以上」で、年収の幅の中央値が500万円に届かない求人が上位に出ない(2026-10-03 のユーザーの決定)。"""
    r = _case(report, "salary-ft")
    assert len(r["top"]) == 3 and r["recall"] == 1.0
    assert all(search_eval._annual_mid(j) >= 5_000_000 for j in _jobs_of(r))


def test_unrelated_queries_return_zero_or_follow_the_tendency(report):
    """求人の情報に無い希望(「宇宙飛行士になりたい」「人間関係が良い職場」など)で、近いだけの求人が職種の傾向より上に並ばない。"""
    rows = [r for r in report["cases"] if r["kind"] == search_eval.UNRELATED]
    assert len(rows) >= 3 and all(r["ok"] for r in rows), [(r["id"], r["top"]) for r in rows]
    assert any(r["top"] for r in rows)  # 0件で合格するだけでなく、当たった求人の並びも確かめている(meal-athome-ap)
    assert report["unrelated_ok"] == f"{len(rows)}/{len(rows)}"


def test_unrelated_query_lets_relax_suggest_removing_the_keywords():
    """無関係な希望で0件になったら、relax が keywords を外す案を出す(task 105 では近いだけの10件が出て、この案が出なかった)。"""
    s = State(Path(tempfile.mkdtemp()) / "s.json")
    s.reset()
    q = search_eval.recorded_query_vectors()["queries"]
    with search_eval.patched(lambda t: tuple(q[t]) if t in q else None):
        r = logic.relax(s, "sim-yoru", keywords="宇宙飛行士になりたい", employment_type="アルバイト・パート")
    assert r["total_now"] == 0 and r["options"][0]["remove"] == "keywords" and r["options"][0]["total_after"] > 0


def test_unrelated_checker_accepts_correct_rankings_and_rejects_noise_on_top():
    """unrelated_ok の負例: 0件・傾向どおりの並び(keywords の点が同じ・無い)は合格、近いだけの求人が傾向の上なら不合格。"""
    card = lambda base, kw=None: {"match_score": base + (kw or 0.0),
                                  "score_breakdown": {"job_type": base} | ({"keywords": kw} if kw is not None else {})}
    assert search_eval.unrelated_ok([])  # 0件は合格
    assert search_eval.unrelated_ok([card(3.0), card(2.0), card(0.5)])  # 意味検索を使わず傾向の順
    assert search_eval.unrelated_ok([card(3.0, 6.0), card(2.0, 6.5), card(0.5, 7.0)])  # keywords の点があっても傾向の順なら合格
    assert search_eval.unrelated_ok([card(2.0), card(2.0)])  # 同点は合格
    assert not search_eval.unrelated_ok([card(0.5, 9.0), card(3.0, 6.0)])  # 傾向の低い求人が keywords の点で上に来た


def test_employment_type_filter_is_kept_in_semantic_search(report):
    by_id = {j.job_id: j for j in load_jobs().values()}
    for r in report["cases"]:
        if r["employment_type"]:
            assert all(by_id[i].employment_type == r["employment_type"] for i, _, _ in r["top"]), r["id"]


def test_eval_falls_back_to_substring_when_embedding_is_unavailable():
    """API が使えない(embed_query が None)ときは部分一致だけで測る(検索は止めない)。属性・働き方・休日の引数は
    embedding を使わないので、API が使えなくても同じ結果になる。"""
    rep = search_eval.evaluate({})  # どの問い合わせにもベクトルが無い = 意味検索なし
    assert _case(rep, "bike-ap")["top"] == []  # keywords の言い換えは部分一致では拾えない(従来どおり)
    with_api = search_eval.evaluate(search_eval.recorded_query_vectors()["queries"])
    for c in search_eval.load_cases():
        if c.get("args") and not search_eval.case_keywords(c):
            assert _case(rep, c["id"])["top"] == _case(with_api, c["id"])["top"], c["id"]
    assert all("keywords" not in j["score_breakdown"] for c in search_eval.load_cases()[:5]
               for j in _recommend(c))


def _recommend(case):
    s = State(Path(tempfile.mkdtemp()) / "s.json")
    s.reset()
    with search_eval.patched(lambda _t: None):
        return search_eval.run_case(s, case)


# ----------------------------- 評価器(負例つき) -----------------------------

def test_recall_counts_a_full_top3_as_perfect_even_with_many_answers():
    # 正解が9件あっても、上位3件がすべて正解なら 1.0(取りこぼし扱いにしない)
    assert search_eval.recall_at_k(["a", "b", "c"], {"a", "b", "c", "d", "e", "f", "g", "h", "i"}) == 1.0


def test_recall_with_a_single_answer():
    assert search_eval.recall_at_k(["x", "y", "a"], {"a"}) == 1.0  # 3位でも当たり
    assert search_eval.recall_at_k(["x", "y", "z", "a"], {"a"}) == 0.0  # 4位以下は数えない
    assert search_eval.recall_at_k([], {"a"}) == 0.0  # 0件は0


def test_recall_with_fewer_results_than_k():
    # 正解2件・結果2件でどちらも正解なら 1.0(3件に満たないことを減点しない)
    assert search_eval.recall_at_k(["a", "b"], {"a", "b"}) == 1.0
    with pytest.raises(ValueError):
        search_eval.recall_at_k(["a"], set())


def test_relevant_ids_respect_the_employment_type():
    jobs = load_jobs()
    cases = {c["id"]: c for c in search_eval.load_cases()}
    ap, anyt = search_eval.relevant_ids(cases["quiet-ap"], jobs), search_eval.relevant_ids(cases["quiet-any"], jobs)
    assert all(jobs[i].employment_type == "アルバイト・パート" for i in ap)
    assert ap < anyt and any(jobs[i].employment_type == "正社員" for i in anyt)  # 未定なら両方の区分が正解
    # 職種・待遇・時間帯で決めた正解は、その条件に当たる求人をすべて含む
    teach = search_eval.relevant_ids(cases["teach-kids-ap"], jobs)
    assert teach == {j.job_id for j in jobs.values() if j.job_type == "塾講師・家庭教師"}
    meal = search_eval.relevant_ids(cases["meal-ap"], jobs)
    assert meal == {j.job_id for j in jobs.values() if "まかない" in j.perks and j.employment_type == "アルバイト・パート"}


def test_patched_restores_the_real_settings():
    before = (embedding.embed_query, embedding.job_text, logic.SIM_FLOOR, logic.SIM_WEIGHT)
    with search_eval.patched(lambda _t: None, SIM_FLOOR=0.0, SIM_WEIGHT=1):
        assert logic.SIM_FLOOR == 0.0 and logic.SIM_WEIGHT == 1
    assert (embedding.embed_query, embedding.job_text, logic.SIM_FLOOR, logic.SIM_WEIGHT) == before
