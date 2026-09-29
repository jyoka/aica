"""正社員の求人データとスキーマ(aica v2 の A、ADR-0002)のテスト。実 LLM なし、dip の MCP は本物。

- 求人は1つの形に雇用区分(employment_type)と給与(pay {kind: 時給|月給|年収, min, max}、円)を持つ
- 職種の一覧は区分ごと。区分ごとに一覧の中の完全一致
- recommend_jobs は employment_type と min_annual_salary(万円)で絞れ、区分が未定なら両方から出す
- カードは区分に合わせて給与を出す(時給 / 年収・月給の幅)。本番の / と開発者画面の /dev で同じ部品(talk-core.js)

画面を実際に開くテスト(Playwright)は、playwright と Chromium が入っているときだけ動く。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

import pytest
from langchain_core.outputs import ChatGeneration, ChatResult

from dip_mcp import logic
from dip_mcp.data import ROOT, State, annual_pay_max, load_jobs, pay_text
from line_runtime.runtime import _tool_payload
from tests.test_handle import ScriptedModel, call, reply
from tests.test_talk_actions import (  # noqa: F401  browser はフィクスチャ
    _free_port,
    browser,
)
from tests.test_talk_page import (  # noqa: F401  app はフィクスチャ
    app,
    open_talk,
    stream,
)


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.json")
    s.reset()
    return s


def _jobs(employment_type):
    return [j for j in load_jobs().values() if j.employment_type == employment_type]


# ----------------------------- 求人データ -----------------------------


def test_part_time_jobs_keep_their_hourly_wage_in_pay():
    part = _jobs("アルバイト・パート")
    assert len(part) == 63
    for j in part:
        assert j.pay.kind == "時給" and j.pay.max is None and 1000 <= j.pay.min <= 3000
        assert j.time_bands and j.days_min  # アルバイト・パートの項目はそのまま
        assert j.work_style is None and j.holidays is None and not j.required_experience
    assert load_jobs()["a001"].pay.min == 1350 and pay_text(load_jobs()["a001"].pay) == "時給1350円"


def test_about_forty_fictional_fulltime_jobs_in_the_same_areas():
    full = _jobs("正社員")
    assert 36 <= len(full) <= 44
    assert {j.area for j in full} <= set(logic.AREAS)
    for j in full:
        assert j.pay.kind in ("年収", "月給") and j.pay.max and j.pay.max > j.pay.min
        assert j.time_bands == [] and j.days_min is None  # アルバイト・パート専用の項目は空
        assert j.work_style and j.holidays and j.employment_terms and j.drawback and j.description
    # 経歴の厚い人(PM・法人営業・多言語)に合う求人がある
    text = lambda j: " ".join([j.title, *j.required_experience, *j.preferred_experience])
    assert any(j.job_type == "企画・PM" and "プロジェクト" in text(j) for j in full)
    assert any(j.job_type == "営業" and "法人営業" in text(j) for j in full)
    assert sum(1 for j in full if re.search("英語|中国語|韓国語|多言語", text(j))) >= 5


def test_job_types_are_exact_per_employment_type():
    for employment_type, types in logic.JOB_TYPES_BY_EMPLOYMENT.items():
        found = {j.job_type for j in _jobs(employment_type)}
        assert found == set(types), employment_type
    assert len(logic.FULLTIME_JOB_TYPES) == 10
    assert all(len([j for j in _jobs("正社員") if j.job_type == t]) >= 3 for t in logic.FULLTIME_JOB_TYPES)


# ----------------------------- recommend_jobs -----------------------------


def test_recommend_filters_by_employment_type(state):
    for employment_type in ("アルバイト・パート", "正社員"):
        r = logic.recommend(state, None, employment_type=employment_type, limit=3)
        assert r["total"] == len(_jobs(employment_type))
        assert all(j["employment_type"] == employment_type for j in r["jobs"])


def test_undecided_recommends_from_both_employment_types(state):
    total = len(load_jobs())
    assert logic.recommend(state, None)["total"] == total
    assert logic.recommend(state, None, employment_type="未定")["total"] == total  # 未定は指定なしと同じ
    r = logic.recommend(state, None, area="新宿")
    kinds = {j.employment_type for j in load_jobs().values() if j.area == "新宿"}
    assert r["total"] == sum(1 for j in load_jobs().values() if j.area == "新宿") and kinds == {"アルバイト・パート", "正社員"}
    assert {j["employment_type"] for j in r["jobs"]} == {"アルバイト・パート", "正社員"}  # 上位3件に両方が並ぶ


def test_min_annual_salary_keeps_only_fulltime_jobs_that_reach_it(state):
    jobs = load_jobs()
    r = logic.recommend(state, None, min_annual_salary=600)
    ids = {j.job_id for j in jobs.values() if (annual_pay_max(j.pay) or 0) >= 6_000_000}
    assert r["total"] == len(ids) and r["total"] > 0
    assert all(j["job_id"] in ids and j["employment_type"] == "正社員" for j in r["jobs"])
    # 月給は12か月分で見る(f0104 は池袋の営業で月給26万〜35万円 = 420万円): 400万円なら届き、500万円なら届かない
    ids_at = lambda man: [c["job_id"] for c in logic.recommend(state, None, area="池袋", job_type="営業", min_annual_salary=man)["jobs"]]
    assert ids_at(400) == ["f0104"] and ids_at(500) == []
    assert logic.recommend(state, None, min_annual_salary=2000)["total"] == 0


def test_part_time_conditions_do_not_bring_fulltime_jobs(state):
    for kw in ({"min_wage": 1000}, {"time_band": "日中"}):
        r = logic.recommend(state, None, **kw)
        assert r["total"] > 0 and all(j["employment_type"] == "アルバイト・パート" for j in r["jobs"])
        assert r["total"] == logic.recommend(state, None, employment_type="アルバイト・パート", **kw)["total"]


def test_job_type_must_be_in_the_list_of_that_employment_type(state):
    assert logic.recommend(state, None, job_type="営業")["total"] == 4  # 区分なしなら、どちらかの一覧にあればよい
    r = logic.recommend(state, None, job_type="営業", employment_type="アルバイト・パート")
    assert "jobs" not in r and "アルバイト・パート" in r["error"]
    assert r["job_types"] == {"アルバイト・パート": list(logic.JOB_TYPES)}
    r = logic.recommend(state, None, job_type="カフェ", employment_type="正社員")
    assert r["job_types"] == {"正社員": list(logic.FULLTIME_JOB_TYPES)}
    r = logic.recommend(state, None, employment_type="契約社員")
    assert "jobs" not in r and r["employment_types"] == ["アルバイト・パート", "正社員"]


def test_relax_suggests_lowering_the_annual_salary_but_keeps_the_employment_type(state):
    r = logic.relax(state, "sim-new", area="大宮", employment_type="正社員", min_annual_salary=900)
    assert r["total_now"] == 0
    assert {o["remove"] for o in r["options"]} <= {"area", "min_annual_salary"}
    assert any(o["remove"] == "min_annual_salary" for o in r["options"])


def test_fulltime_card_has_pay_work_style_and_holidays(state):
    card = logic.recommend(state, None, employment_type="正社員", job_type="企画・PM", area="新宿")["jobs"][0]
    assert card["job_id"] == "f0201" and card["employment_type"] == "正社員"
    assert card["pay"] == {"kind": "年収", "min": 6_500_000, "max": 9_500_000} and card["pay_text"] == "年収650万〜950万円"
    assert card["days_min"] is None and card["work_style"]["remote"] == "可" and card["holidays"]["annual_days"] == 125
    assert "hourly_wage" not in card
    part = logic.recommend(state, None, job_type="カフェ")["jobs"][0]
    assert part["pay"]["kind"] == "時給" and part["pay_text"].startswith("時給") and "work_style" not in part


def test_answer_material_for_a_fulltime_job_uses_pay_and_the_fulltime_items():
    r = logic.answer_material("リモートワークはできますか", "f0102")
    facts = " ".join(h["a"] for h in r["hits"])
    assert "リモートワーク一部可" in facts
    assert r["job"]["pay"] == {"kind": "年収", "min": 5_500_000, "max": 8_000_000}
    r = logic.answer_material("年間休日と試用期間を教えて", "f0102")
    facts = " ".join(h["a"] for h in r["hits"])
    assert "年間休日123日" in facts and "試用期間: 3か月" in facts
    assert any("時給1350円" in h["a"] for h in logic.answer_material("時給と最寄り駅からの徒歩", "b0101")["hits"])


def test_recommend_jobs_tool_explains_employment_type_and_min_annual_salary():
    from dip_mcp import server

    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("recommend_jobs", "relax_conditions"):
        props = tools[name].inputSchema["properties"]
        assert "search_track" in props["employment_type"]["description"] and "未定" in props["employment_type"]["description"]
        assert "万円" in props["min_annual_salary"]["description"]
        assert all(t in props["job_type"]["description"] for t in logic.FULLTIME_JOB_TYPES + logic.JOB_TYPES)
    assert "両方の区分" in tools["recommend_jobs"].description


# ----------------------------- 会話(台本)と画面 -----------------------------

MENU = ["求人を探す", "まず相談する", "今はいい"]


class Recruiter(ScriptedModel):
    """dip の Prompt どおりに動く偽のモデル(何度でも返す): 「正社員で年収600万以上」で正社員の求人を、
    「どちらも見たい」で区分を指定せずに両方の求人を、recommend_jobs が返したものだけカードにする。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.respond(messages))])

    def respond(self, messages):
        uid = re.search(r"LINE ユーザーID: ([^(\s]+)", messages[0].content).group(1)
        last = max(i for i, m in enumerate(messages) if m.type == "human")
        human, turn = messages[last].content, messages[last + 1:]
        search = {"正社員で年収600万以上": {"employment_type": "正社員", "min_annual_salary": 600, "area": "新宿"},
                  "どちらも見たい": {"area": "新宿"}}.get(human)
        if search is None:
            return reply(["こんにちは。お仕事探しをお手伝いします。何をしますか?"], quick_replies=MENU)
        if not any(m.type == "tool" for m in turn):
            return call("recommend_jobs", {"line_user_id": uid, **search}, "rec")
        jobs = next(_tool_payload(m) for m in turn if m.type == "tool")["jobs"]
        return reply([f"{len(jobs)}件あります。"], job_ids=[j["job_id"] for j in jobs], quick_replies=["ほかも見たい", "今はいい"])


def test_fulltime_search_in_the_talk_returns_fulltime_cards_and_undecided_gets_both(app):  # noqa: F811
    from fastapi.testclient import TestClient

    app.set_model(Recruiter())
    with TestClient(app.app) as client:
        opened, greeting = open_talk(client)
        r = stream(client, opened["user"], "message", greeting["thread"], "正社員で年収600万以上")[-1]
        assert r["kind"] == "reply" and r["cards"]
        assert all(c["employment_type"] == "正社員" and annual_pay_max_of(c) >= 6_000_000 for c in r["cards"])
        assert r["cards"][0]["pay_text"].startswith("年収")
        r = stream(client, opened["user"], "message", greeting["thread"], "どちらも見たい")[-1]
        assert {c["employment_type"] for c in r["cards"]} == {"アルバイト・パート", "正社員"}


def annual_pay_max_of(card: dict) -> int:
    p = card["pay"]
    return (p["max"] or p["min"]) * (12 if p["kind"] == "月給" else 1)


@pytest.fixture
def served(app):  # noqa: F811
    import threading
    import time

    import uvicorn

    app.set_model(Recruiter())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(10)


def _check_cards(page) -> None:
    """正社員は「正社員」の印と年収の幅・働き方のタグ、アルバイト・パートは時給と週の日数(/ と /dev で同じ)。"""
    page.fill("#t", "どちらも見たい")
    page.locator("#send").click()
    page.locator(".cards .jc").first.wait_for()
    cards = page.locator(".cards").last.locator(".jc")
    texts = [cards.nth(i).inner_text() for i in range(cards.count())]
    full = [t for t in texts if "正社員" in t]
    part = [t for t in texts if "正社員" not in t]
    assert full and part
    assert all(re.search(r"(年収|月給)\s*\d+万〜\d+万円", t) for t in full)
    assert all("時給" not in t and "週" not in t for t in full)
    assert any(re.search(r"リモート|フレックス|残業|年間休日", t) for t in full)
    assert all(re.search(r"時給\s*[\d,]+円", t) and "日〜" in t for t in part)


def test_talk_screen_draws_pay_by_employment_type(served, browser):  # noqa: F811
    page = browser.new_page()
    page.goto(served + "/")
    page.locator("#log .chips button", has_text="求人を探す").wait_for()
    _check_cards(page)
    page.close()


def test_dev_screen_draws_pay_by_employment_type(served, browser):  # noqa: F811
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(served + "/dev")
    page.locator('#scenarios .act[data-id="C-consult-propose"]').click()
    page.locator("#log .chips button", has_text="求人を探す").wait_for()
    _check_cards(page)
    page.close()


def test_mcp_server_serves_fulltime_jobs_over_stdio(tmp_path):
    """本物の dip MCP サーバー(子プロセス)越しに、正社員の求人を区分と年収で絞れる。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "dip_mcp.server"], cwd=str(ROOT),
                                       env={"DIP_STATE_PATH": str(tmp_path / "s.json"), "PATH": ""})
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("recommend_jobs", {"employment_type": "正社員", "min_annual_salary": 700})
            return json.loads(res.content[0].text)

    out = asyncio.run(go())
    assert out["total"] > 0 and all(j["employment_type"] == "正社員" for j in out["jobs"])
