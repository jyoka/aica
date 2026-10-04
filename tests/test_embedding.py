"""求人の意味検索(embedding)のオフラインテスト。実 API は呼ばない(問い合わせの埋め込みは差し替える)。

求人側のベクトルは fixtures/job_vectors.json の保存済みのもの(scripts/gen_job_vectors.py が作る)を使う。
"""

from __future__ import annotations

import json

import pytest

from dip_mcp import embedding, logic
from dip_mcp.data import State, load_jobs


@pytest.fixture(autouse=True)
def _no_real_api(monkeypatch):
    """テスト中は API を呼ばせない(呼んだら失敗)。使うテストは monkeypatch で上書きする。"""
    embedding._query_cache.clear()
    monkeypatch.setattr(embedding, "_api_embed",
                        lambda *a, **kw: pytest.fail("テストから実 API(_api_embed)を呼んではいけない"))
    yield
    embedding._query_cache.clear()


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.json")
    s.reset()
    return s


def _stored_vector(job_id: str) -> list[float]:
    return embedding._vectors()["jobs"][job_id]["v"]


def test_job_vectors_file_is_in_sync_with_the_job_data():
    """求人データを変えたら scripts/gen_job_vectors.py で作り直す(ずれたままにしない)。"""
    data = json.loads(embedding.VECTORS_PATH.read_text(encoding="utf-8"))
    jobs = load_jobs()
    assert data["model"] == embedding.MODEL and data["dimensions"] == embedding.DIMENSIONS
    assert set(data["jobs"]) == set(jobs)
    stale = [jid for jid, j in jobs.items() if data["jobs"][jid]["hash"] != embedding.job_hash(j)]
    assert stale == [], f"求人データが変わっている: {stale}。scripts/gen_job_vectors.py で作り直す"
    assert all(len(r["v"]) == data["dimensions"] for r in data["jobs"].values())


def test_semantic_keywords_match_without_word_overlap(state, monkeypatch):
    # 問い合わせの埋め込みを、倉庫求人 b0701 の保存済みベクトルに差し替える(= 意味が b0701 に一番近い問い合わせ)
    monkeypatch.setattr(embedding, "_api_embed", lambda *a, **kw: _stored_vector("b0701"))
    r = logic.recommend(state, None, keywords="もくもくと働きたい")  # 求人の文面のどこにも無い言い方
    assert r["total"] >= 1 and r["jobs"][0]["job_id"] == "b0701"
    b = r["jobs"][0]["score_breakdown"]
    assert b["keywords"] == logic.SIM_WEIGHT  # 類似度1.0(自分自身)× 重み
    assert round(sum(b.values()), 2) == r["jobs"][0]["match_score"]


def test_semantic_keywords_keep_substring_hits(state, monkeypatch):
    # 意味の近さで選んでも、従来の部分一致(まかない = perks の文言そのもの)は落とさない
    monkeypatch.setattr(embedding, "_api_embed", lambda *a, **kw: _stored_vector("b1201"))  # 事務の求人に近い問い合わせ
    with_sem = logic.recommend(state, None, keywords="まかない")["total"]
    substr = sum(1 for j in load_jobs().values() if logic._keyword_hit(j, "まかない"))
    assert with_sem >= substr


def _fail_api(*_a, **_kw):
    raise ConnectionError("offline")


def test_keywords_fall_back_to_substring_when_the_api_fails(state, monkeypatch):
    monkeypatch.setattr(embedding, "_api_embed", _fail_api)
    r = logic.recommend(state, None, keywords="まかない")  # 部分一致で当たる
    assert r["total"] >= 1 and all("keywords" not in j["score_breakdown"] for j in r["jobs"])
    miss = logic.recommend(state, None, keywords="人と話さない仕事")  # 部分一致では当たらない言い換え
    assert miss["total"] == 0  # 意味検索が使えないときは従来どおり(検索は失敗しない)


def test_query_embedding_is_cached_within_the_process(state, monkeypatch):
    calls = []
    monkeypatch.setattr(embedding, "_api_embed", lambda *a, **kw: calls.append(1) or _stored_vector("b0701"))
    logic.recommend(state, None, keywords="もくもく")
    logic.recommend(state, None, keywords="もくもく")
    assert len(calls) == 1
    # 失敗は覚えない(次の検索で意味検索に戻れる)
    monkeypatch.setattr(embedding, "_api_embed", _fail_api)
    assert embedding.embed_query("別の問い合わせ") is None
    monkeypatch.setattr(embedding, "_api_embed", lambda *a, **kw: _stored_vector("b0701"))
    assert embedding.embed_query("別の問い合わせ") is not None


def test_missing_vectors_file_means_substring_only(state, monkeypatch, tmp_path):
    monkeypatch.setattr(embedding, "VECTORS_PATH", tmp_path / "nai.json")
    embedding._vectors.cache_clear()
    try:
        assert embedding.embed_query("まかない") is None  # ファイルが無ければ API を呼ばずに部分一致へ
        assert logic.recommend(state, None, keywords="まかない")["total"] >= 1
    finally:
        embedding._vectors.cache_clear()


def test_keyword_searches_are_traced_only_when_the_eval_asks(state, monkeypatch, tmp_path):
    """評価(evals/run.py)が DIP_KEYWORDS_TRACE を渡したときだけ、keywords の検索ごとに最高値 - 中央値と、意味検索を
    使ったか(SIM_PEAK)を1行ずつ残す(task 109)。ツールの結果は変えない。"""
    trace = tmp_path / "keywords.jsonl"
    monkeypatch.setattr(embedding, "_api_embed", lambda *a, **kw: _stored_vector("b0701"))
    plain = logic.recommend(state, None, keywords="もくもくと働きたい")
    assert not trace.exists()
    monkeypatch.setenv("DIP_KEYWORDS_TRACE", str(trace))
    assert logic.recommend(state, None, keywords="もくもくと働きたい") == plain
    monkeypatch.setattr(embedding, "_api_embed", _fail_api)
    logic.recommend(state, None, keywords="まかない")  # API が使えないとき: peak は None、部分一致だけ
    near, offline = [json.loads(x) for x in trace.read_text(encoding="utf-8").splitlines()]
    assert near["keywords"] == "もくもくと働きたい" and near["semantic"] and near["peak"] >= logic.SIM_PEAK
    assert near["kept"] == plain["total"] and near["candidates"] == len(load_jobs())
    assert offline == {"keywords": "まかない", "peak": None, "semantic": False, "candidates": len(load_jobs()),
                       "kept": offline["word_hits"], "word_hits": offline["word_hits"]} and offline["kept"] >= 1
