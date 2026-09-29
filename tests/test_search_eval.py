"""検索評価セット(evals/search_eval_set.json)と再現率の計算のオフラインテスト。実 API は呼ばない。

求人ベクトルは fixtures/job_vectors.json、問い合わせのベクトルは evals/search_eval.py record で記録した
fixtures/search_query_vectors.json を使う。評価器(relevant_ids・recall_at_k)の負例も置く
(正しい結果を取りこぼし扱いにしないこと)。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from dip_mcp import embedding, logic
from dip_mcp.data import EMPLOYMENT_TYPES, State, load_jobs
from evals import search_eval

# task 105 の前の設定(text-embedding-3-small・512 次元・給与まで入れた文面・下限 0.30・重み 6)の再現率は 0.551
BASELINE_RECALL = 0.551
CHOSEN_RECALL = 0.75


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
    assert 30 <= len(cases) <= 40 and len({c["id"] for c in cases}) == len(cases)
    assert {c.get("employment_type") for c in cases} == {*EMPLOYMENT_TYPES, None}  # 両方の区分と未定を含む
    assert {c["kind"] for c in cases} == {"言い換え", "否定", "経歴"}
    for c in cases:
        exp = c["expected"]
        assert set(exp) <= {"job_ids", "job_types", "perks", "time_bands"}, c["id"]
        assert set(exp.get("job_ids", ())) <= set(jobs), c["id"]  # 無い求人を正解にしない
        types = logic.JOB_TYPES + logic.FULLTIME_JOB_TYPES
        assert set(exp.get("job_types", ())) <= set(types), c["id"]
        assert search_eval.relevant_ids(c, jobs), f"{c['id']} の正解が0件"


def test_recorded_query_vectors_match_the_job_vectors():
    """モデル・次元を変えて求人ベクトルだけ作り直したら、問い合わせも記録し直す(evals/search_eval.py record)。"""
    rec, meta = search_eval.recorded_query_vectors(), embedding._vectors()
    assert (rec["model"], rec["dimensions"]) == (meta["model"], meta["dimensions"]) == (embedding.MODEL, embedding.DIMENSIONS)
    assert {c["q"] for c in search_eval.load_cases()} <= set(rec["queries"])
    assert all(len(v) == rec["dimensions"] for v in rec["queries"].values())


# ----------------------------- 今の設定の質 -----------------------------

def test_recall_is_above_the_baseline(report):
    assert report["recall_at_3"] >= CHOSEN_RECALL > BASELINE_RECALL
    assert report["zero_hits"] == 0  # 下限で正解ごと落として0件にしない


@pytest.mark.parametrize("cid", ["quiet-ap", "quiet-any"])
def test_quiet_work_query_ranks_quiet_jobs_not_call_centers(report, cid):
    """「人と話さず黙々と」で、コールセンターではなく入力・検品などの仕事が上位に出る(2026-09-27 の不具合)。"""
    r = _case(report, cid)
    assert sum(ok for _, _, ok in r["top"]) >= 2, r["top"]
    assert all(t != "コールセンター" for _, t, _ in r["top"]), r["top"]


@pytest.mark.parametrize("cid", ["move-ap", "move-any"])
def test_move_body_query_ranks_physical_jobs(report, cid):
    """「体を動かしたい」が0件にならず、倉庫・配送・設営などが上位3件を占める(2026-09-27 の不具合)。"""
    r = _case(report, cid)
    assert len(r["top"]) == 3 and all(ok for _, _, ok in r["top"]), r["top"]
    assert {t for _, t, _ in r["top"]} <= {"倉庫・軽作業", "配送・デリバリー", "イベントスタッフ", "物流"}


def test_fulltime_move_body_and_quiet_queries_current_behavior(report):
    """正社員の「体を動かしたい」「人と話さず黙々と」の今の結果を固定する(意図どおりではない。下の xfail を参照)。
    体を動かす方は物流(f1004)が1位、黙々との方はコールセンターが出ない、までは満たす。"""
    move, quiet = _case(report, "move-ft"), _case(report, "quiet-ft")
    assert move["top"][0][0] == "f1004", move["top"]
    assert all(t != "コールセンター" for _, t, _ in quiet["top"]) and any(ok for _, _, ok in quiet["top"]), quiet["top"]


@pytest.mark.xfail(strict=True, reason="正社員の求人はどれも類似度 0.18〜0.23 で、embedding では体を使う仕事と"
                   "デスクの仕事を分けられない(task 105)。求人の属性(体を使う度合い・人と話す度合い)を足す案で直す")
@pytest.mark.parametrize("cid", ["move-ft", "quiet-ft"])
def test_fulltime_move_body_and_quiet_queries_rank_intended_jobs(report, cid):
    r = _case(report, cid)
    assert len(r["top"]) == 3 and sum(ok for _, _, ok in r["top"]) >= 2, r["top"]


def test_employment_type_filter_is_kept_in_semantic_search(report):
    by_id = {j.job_id: j for j in load_jobs().values()}
    for r in report["cases"]:
        if r["employment_type"]:
            assert all(by_id[i].employment_type == r["employment_type"] for i, _, _ in r["top"]), r["id"]


def test_eval_falls_back_to_substring_when_embedding_is_unavailable():
    """API が使えない(embed_query が None)ときは部分一致だけで測る(検索は止めない)。"""
    rep = search_eval.evaluate({})  # どの問い合わせにもベクトルが無い = 意味検索なし
    assert _case(rep, "move-ap")["top"] == []  # 言い換えは部分一致では拾えない(従来どおり)
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
