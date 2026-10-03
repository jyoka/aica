"""求人の属性(人と話す量・体を使う量)と、正社員の働き方・休日の絞り込み(task 111)のオフラインテスト。実 API は呼ばない。

- recommend / relax が属性・働き方・休日の引数で絞り、API が使えないとき(部分一致だけ)も同じに動く
- keywords に「在宅」「土日祝休み」が入ったときも、API が使えないときの部分一致で当たる
- 意味の近い求人が無い問い合わせ(求人の情報に無い希望)は、近いだけの求人を出さず部分一致だけにする
- 引数の名前が dip(logic・MCP の recommend_jobs / relax_conditions)と音声(voice_resume/jobs.py・propose_jobs)でそろっている
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from dip_mcp import embedding, logic, server
from dip_mcp.data import PHYSICAL_LEVELS, TALK_LEVELS, State, load_jobs
from evals import search_eval
from voice_resume import jobs as voice_jobs
from voice_resume.advisor import ResumeAdvisor

ALL_ARGS = (*logic.ATTRIBUTE_ARGS, *logic.FULLTIME_STYLE_ARGS)


@pytest.fixture(autouse=True)
def _no_real_api(monkeypatch):
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


def _offline(monkeypatch):
    def fail(*_a, **_kw):
        raise ConnectionError("offline")
    monkeypatch.setattr(embedding, "_api_embed", fail)


def _ids(r: dict) -> set[str]:
    return {c["job_id"] for c in r["jobs"]}


def _every(state, **kw) -> set[str]:
    """条件に合う求人を全部(上位3件ずつ除きながら集める)。"""
    seen: set[str] = set()
    while True:
        got = _ids(logic.recommend(state, None, exclude_ids=sorted(seen), **kw))
        if not got:
            return seen
        seen |= got


# ----------------------------- 求人データ -----------------------------

def test_every_job_has_both_attributes():
    jobs = load_jobs()
    assert len(jobs) == 103
    assert all(j.talk_level in TALK_LEVELS and j.physical_level in PHYSICAL_LEVELS for j in jobs.values())
    # どちらの区分にも、どの段階の求人もある(どの値で絞っても片方の区分が空にならない)
    for et in ("アルバイト・パート", "正社員"):
        js = [j for j in jobs.values() if j.employment_type == et]
        assert {j.talk_level for j in js} == {1, 2, 3} and {j.physical_level for j in js} == {1, 2, 3}, et


def test_attributes_are_not_in_the_embedding_text():
    """属性は埋め込みの文面に入れない(求人ベクトルを作り直さずに済む。古さの照合は tests/test_embedding.py)。"""
    j = load_jobs()["b0701"]
    assert embedding.job_hash(j) == embedding.job_hash(j.model_copy(update={"talk_level": 3, "physical_level": 1}))


# ----------------------------- 属性の絞り込み -----------------------------

@pytest.mark.parametrize(("kw", "ok"), [
    ({"max_talk_level": 1}, lambda j: j.talk_level == 1),
    ({"max_talk_level": 2}, lambda j: j.talk_level <= 2),
    ({"min_talk_level": 3}, lambda j: j.talk_level == 3),
    ({"max_physical_level": 1}, lambda j: j.physical_level == 1),
    ({"max_physical_level": 2}, lambda j: j.physical_level <= 2),
    ({"min_physical_level": 3}, lambda j: j.physical_level == 3),
])
def test_attribute_filters_keep_exactly_the_matching_jobs(state, kw, ok):
    jobs = load_jobs()
    want = {jid for jid, j in jobs.items() if ok(j)}
    assert logic.recommend(state, None, **kw)["total"] == len(want)
    assert _ids(logic.recommend(state, None, **kw)) <= want


def test_attribute_filters_work_for_both_employment_types_and_combine(state):
    jobs = load_jobs()
    both = _every(state, max_physical_level=1, max_talk_level=1)
    assert both == {jid for jid, j in jobs.items() if j.physical_level == 1 and j.talk_level == 1}
    assert {jobs[i].employment_type for i in both} == {"アルバイト・パート", "正社員"}  # 区分が未定なら両方から


def test_attribute_reason_is_shown_first(state):
    r = logic.recommend(state, None, max_physical_level=1, employment_type="アルバイト・パート")
    assert all(c["reasons"][0] == logic.PHYSICAL_REASONS[1] for c in r["jobs"])
    r = logic.recommend(state, None, max_talk_level=2, employment_type="アルバイト・パート")
    jobs = load_jobs()
    assert all(c["reasons"][0] == logic.TALK_REASONS[jobs[c["job_id"]].talk_level] for c in r["jobs"])
    # 属性で絞らない検索の理由は今までどおり(属性の一言を足さない)
    plain = logic.recommend(state, None, employment_type="アルバイト・パート")
    assert not any(set(c["reasons"]) & {*logic.PHYSICAL_REASONS.values(), *logic.TALK_REASONS.values()} for c in plain["jobs"])


@pytest.mark.parametrize("kw", [{"max_talk_level": 0}, {"min_physical_level": 4}, {"max_talk_level": True},
                                {"min_talk_level": 3, "max_talk_level": 1}, {"remote": "不可"},
                                {"remote": "在宅"}, {"max_overtime_hours": -1}])
def test_bad_attribute_values_return_an_error_not_zero_hits(state, kw):
    r = logic.recommend(state, None, **kw)
    assert "jobs" not in r and "error" in r
    assert "error" in logic.relax(state, "sim-new", **kw)


def test_unknown_filter_names_are_rejected(state):
    with pytest.raises(TypeError):
        logic.recommend(state, None, max_talk=1)
    with pytest.raises(TypeError):
        logic.relax(state, "sim-new", standing=False)


# ----------------------------- 正社員の働き方・休日 -----------------------------

def test_remote_weekends_and_overtime_filters(state):
    jobs = load_jobs()
    ft = {jid: j for jid, j in jobs.items() if j.work_style}
    assert _every(state, remote="可") == {jid for jid, j in ft.items() if j.work_style.remote == "可"}
    assert _every(state, remote="一部可") == {jid for jid, j in ft.items() if j.work_style.remote in ("可", "一部可")}
    assert _every(state, weekends_holidays=True) == {jid for jid, j in ft.items() if j.holidays.weekends_holidays}
    assert _every(state, max_overtime_hours=10) == {jid for jid, j in ft.items() if j.work_style.overtime_hours <= 10}
    # 正社員の条件なので、アルバイト・パートの求人は出ない。false は「絞らない」
    assert logic.recommend(state, None, remote="一部可", employment_type="アルバイト・パート")["total"] == 0
    assert logic.recommend(state, None, weekends_holidays=False)["total"] == len(jobs)


def test_relax_offers_removing_attribute_and_style_filters(state):
    r = logic.relax(state, "sim-new", employment_type="正社員", remote="可", area="大宮")
    assert r["total_now"] == 0
    assert {o["remove"] for o in r["options"]} == {"remote", "area"}
    r = logic.relax(state, "sim-new", employment_type="アルバイト・パート", max_physical_level=1, job_type="飲食ホール")
    assert r["total_now"] == 0 and any(o["remove"] == "max_physical_level" for o in r["options"])
    # 「残業なし」(0時間)も外す候補になる(0 を「指定なし」と取り違えない)
    r = logic.relax(state, "sim-new", employment_type="正社員", max_overtime_hours=0)
    assert r["total_now"] == 0 and r["options"][0]["remove"] == "max_overtime_hours" and r["options"][0]["current"] == 0


# ----------------------------- API が使えないとき -----------------------------

def test_attribute_filters_do_not_need_the_api(state, monkeypatch):
    _offline(monkeypatch)
    r = logic.recommend(state, None, max_talk_level=1, employment_type="アルバイト・パート")
    assert r["total"] > 0 and all("keywords" not in c["score_breakdown"] for c in r["jobs"])


def test_substring_fallback_matches_work_style_and_holiday_words(state, monkeypatch):
    """keywords に「在宅」「土日祝休み」「フレックス」が入っても、API が使えないときの部分一致で当たる。"""
    _offline(monkeypatch)
    jobs = load_jobs()
    remote = {jid for jid, j in jobs.items() if j.work_style and j.work_style.remote != "不可"}
    assert _every(state, keywords="在宅") == remote
    weekends = {jid for jid, j in jobs.items() if j.holidays and j.holidays.weekends_holidays}
    # 求人の文面に「土日祝は完全に休み」とあるアルバイトの求人(b0204)は、文面には一致しないので入らない
    assert _every(state, keywords="土日祝休み") == weekends
    assert _every(state, keywords="フレックス") == {jid for jid, j in jobs.items() if j.work_style and j.work_style.flex}
    # 従来の部分一致は変わらない(まかない = perks の文言、言い換えは当たらない)
    assert logic.recommend(state, None, keywords="まかない")["total"] > 0
    assert logic.recommend(state, None, keywords="人と話さない仕事")["total"] == 0


# ----------------------------- 意味の近い求人が無い問い合わせ -----------------------------

def _recorded(monkeypatch, text: str):
    vec = search_eval.recorded_query_vectors()["queries"][text]
    monkeypatch.setattr(embedding, "_api_embed", lambda *a, **kw: vec)


@pytest.mark.parametrize("text", ["宇宙飛行士になりたい", "人間関係が良い職場", "アットホームな職場"])
def test_unrelated_wish_does_not_bring_jobs_that_are_only_near(state, monkeypatch, text):
    """求人を分けられない問い合わせ(最高値が中央値から SIM_PEAK 抜けていない)は、部分一致だけにする。"""
    _recorded(monkeypatch, text)
    r = logic.recommend(state, "sim-yoru", keywords=text)
    assert r["total"] == 0 and "max_talk_level" in r["keywords_note"]  # 引数で探し直す案内が付く


def test_paraphrase_sent_as_keywords_gets_a_note_to_use_the_attribute_args(state, monkeypatch):
    """会話の指示に反して「体を動かしたい」を keywords に入れると0件になる(求人を分けられない問い合わせ)。
    そのときは、属性の引数で探し直す案内を返す。案内どおりに探し直せば当たる。"""
    _recorded(monkeypatch, "体を動かしたい")
    r = logic.recommend(state, None, keywords="体を動かしたい", employment_type="アルバイト・パート")
    assert r["total"] == 0 and "min_physical_level" in r["keywords_note"]
    assert logic.recommend(state, None, min_physical_level=3, employment_type="アルバイト・パート")["total"] > 0


def test_keywords_note_is_only_for_the_unmatched_zero_case(state, monkeypatch):
    _offline(monkeypatch)  # API が使えないときの0件は、意味の近さを測れていないので案内しない
    assert "keywords_note" not in logic.recommend(state, None, keywords="宇宙飛行士")
    _recorded(monkeypatch, "コーヒーが好き")
    embedding._query_cache.clear()
    assert "keywords_note" not in logic.recommend(state, None, keywords="コーヒーが好き")


def test_unrelated_wish_still_keeps_substring_hits(state, monkeypatch):
    """近さを使わないときも、文面に書いてある言葉(b1205 の「アットホームな雰囲気」)の部分一致は落とさない。"""
    _recorded(monkeypatch, "アットホームな職場")
    embedding._query_cache.clear()
    r = logic.recommend(state, None, keywords="アットホーム")  # 記録した「アットホームな職場」のベクトルを使う
    assert _ids(r) == {"b1205"} and "keywords" not in r["jobs"][0]["score_breakdown"]


def test_related_wish_still_uses_semantic_search(state, monkeypatch):
    _recorded(monkeypatch, "コーヒーが好き")
    r = logic.recommend(state, None, keywords="コーヒーが好き", employment_type="アルバイト・パート")
    assert r["jobs"] and all(c["job_type"] == "カフェ" for c in r["jobs"])
    assert all(c["score_breakdown"]["keywords"] > 0 for c in r["jobs"])


# ----------------------------- dip と音声の引数の同期 -----------------------------

def test_voice_argument_table_matches_dip():
    """音声(voice_resume/jobs.py)が持つ引数の名前と値の一覧が、dip(dip_mcp/logic.py)と同じ。"""
    assert voice_jobs.ATTRIBUTE_ARGS == logic.ATTRIBUTE_ARGS
    assert voice_jobs.FULLTIME_STYLE_ARGS == logic.FULLTIME_STYLE_ARGS
    assert voice_jobs.LEVELS == logic.LEVELS
    assert voice_jobs.REMOTE_CHOICES == tuple(logic.REMOTE_CHOICES)
    assert set(ALL_ARGS) <= set(voice_jobs.RELAXABLE) <= set(voice_jobs.CONDITION_LABELS)
    assert set(inspect.signature(voice_jobs.build_recommend_args).parameters) >= set(ALL_ARGS)


def test_voice_propose_jobs_takes_the_attribute_args():
    """音声の相談(アルバイト・パートと正社員の両方)で、属性の引数を受け取って dip に渡せる。"""
    params = inspect.signature(ResumeAdvisor.propose_jobs).parameters
    assert set(logic.ATTRIBUTE_ARGS) <= set(params)
    doc = ResumeAdvisor.propose_jobs.__doc__ or ""
    assert all(k in doc for k in logic.ATTRIBUTE_ARGS)


def test_voice_build_recommend_args_validates_and_drops_empty_values():
    args = voice_jobs.build_recommend_args(max_talk_level=1, min_physical_level=3, remote="可",
                                           weekends_holidays=True, max_overtime_hours=10)
    assert args == {"max_talk_level": 1, "min_physical_level": 3, "remote": "可", "weekends_holidays": True,
                    "max_overtime_hours": 10}
    assert voice_jobs.build_recommend_args(weekends_holidays=False) == {}
    for bad in ({"max_talk_level": 4}, {"remote": "不可"}, {"max_overtime_hours": -1}):
        with pytest.raises(ValueError):
            voice_jobs.build_recommend_args(**bad)
    line = voice_jobs.relax_option_line({"remove": "weekends_holidays", "current": True, "total_after": 9})
    assert line == "- 土日祝休みを外すと9件(relax: weekends_holidays)"


def test_mcp_tools_take_every_attribute_and_style_arg(state, monkeypatch):
    """MCP の recommend_jobs / relax_conditions が引数を受け取り、logic に渡す(説明に値の意味がある)。"""
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("recommend_jobs", "relax_conditions"):
        props = tools[name].inputSchema["properties"]
        assert set(ALL_ARGS) <= set(props), name
    props = tools["recommend_jobs"].inputSchema["properties"]
    assert all(TALK_LEVELS[n] in props["max_talk_level"]["description"] for n in TALK_LEVELS)
    assert all(PHYSICAL_LEVELS[n] in props["max_physical_level"]["description"] for n in PHYSICAL_LEVELS)
    assert "中央値" in props["min_annual_salary"]["description"]
    monkeypatch.setattr(server, "state", state)
    r = server.recommend_jobs(max_physical_level=1, max_talk_level=1, employment_type="正社員")
    jobs = load_jobs()
    assert r["jobs"] and all(jobs[c["job_id"]].physical_level == 1 and jobs[c["job_id"]].talk_level == 1 for c in r["jobs"])
    assert server.relax_conditions("sim-new", remote="可", area="大宮")["total_now"] == 0
