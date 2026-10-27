"""求人の検索で未成年の決まりを強制する(設計メモ docs/design/baito-quick-start.md §6.3)と、時間帯のボタンの対応(§6.2)の
オフラインテスト。実 API は呼ばない。

- 未成年(minor)のとき、深夜の帯・22 時より後に終わる・5 時より前に始まる・時刻の読めない shift・physical_level 3 の求人が出ない
- 成人のとき(minor を付けない・false)は今までと同じ結果
- relax は未成年の決まりを外す候補にしない
- 引数の名前が dip(logic・MCP の recommend_jobs / relax_conditions)と音声(voice_resume/jobs.py)でそろっている
- 時間帯のボタン → ハーネスの time_slots → 検索の time_band が §6.2 の表どおり
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from dip_mcp import embedding, logic, server
from dip_mcp.baito import harness as H
from dip_mcp.baito import quick_start as Q
from dip_mcp.data import State, load_jobs
from voice_resume import jobs as voice_jobs

# 未成年に出してよい求人(17:00〜22:00、深夜の帯なし、physical_level 2)を元に、1 つずつ決まりに当たる求人を作る
BASE_ID = "b0403"
FAKE = {
    "t-late": {"shift": "18:00〜翌1:00"},
    "t-23": {"shift": "17:00〜23:00"},
    "t-early": {"shift": "4:00〜9:00"},
    "t-unread": {"shift": "シフトは相談で決めます"},
    "t-heavy": {"physical_level": 3},
    "t-night-band": {"time_bands": ["夜", "深夜"]},
    "t-ok": {"shift": "17:00〜22:00"},
}


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


@pytest.fixture
def fake_jobs(monkeypatch):
    """本物の求人に、決まりの境目の求人を足す。"""
    real = load_jobs()
    base = real[BASE_ID]
    fakes = {k: base.model_copy(update={"job_id": k, **v}) for k, v in FAKE.items()}
    monkeypatch.setattr(logic, "load_jobs", lambda: real | fakes)
    return fakes


def _every(state, **kw) -> set[str]:
    """条件に合う求人を全部(上位3件ずつ除きながら集める)。"""
    seen: set[str] = set()
    while True:
        got = {c["job_id"] for c in logic.recommend(state, None, exclude_ids=sorted(seen), **kw)["jobs"]}
        if not got:
            return seen
        seen |= got


def test_base_job_is_one_a_minor_can_take():
    base = load_jobs()[BASE_ID]
    assert base.shift == "17:00〜22:00" and "深夜" not in base.time_bands and base.physical_level < 3
    assert logic.minor_ok(base)


@pytest.mark.parametrize(("shift", "ranges"), [
    ("18:00〜翌1:00", [(18 * 60, 25 * 60)]),
    ("22:00〜5:00", [(22 * 60, 29 * 60)]),  # 「翌」が無くても、終わりが始まりより前なら翌日
    ("17:00〜翌1:00(実働7時間・休憩60分)", [(17 * 60, 25 * 60)]),
    ("10:00〜16:00 / 15:00〜21:00", [(10 * 60, 16 * 60), (15 * 60, 21 * 60)]),
    ("15:00〜21:00の間で90分〜120分", [(15 * 60, 21 * 60)]),
    ("シフトは相談で決めます", []),
])
def test_shift_ranges(shift, ranges):
    assert logic.shift_ranges(shift) == ranges


def test_minor_search_drops_late_early_unreadable_heavy_and_night_band_jobs(state, fake_jobs):
    adult, minor = _every(state, area="東京"), _every(state, area="東京", minor=True)
    assert set(fake_jobs) <= adult  # 成人には全部出る
    assert "t-ok" in minor and BASE_ID in minor  # 17:00〜22:00 は出る
    assert not ({"t-late", "t-23", "t-early", "t-unread", "t-heavy", "t-night-band"} & minor)
    jobs = load_jobs()
    assert {"a001", "b0303"} <= adult - minor  # 本物の「18:00〜翌1:00」の求人
    for jid in minor - set(fake_jobs):
        j = jobs[jid]
        assert "深夜" not in j.time_bands and j.physical_level < 3
        assert all(5 * 60 <= s and e <= 22 * 60 for s, e in logic.shift_ranges(j.shift)) and logic.shift_ranges(j.shift)


def test_minor_search_covers_every_job_and_both_employment_types(state):
    """どの求人も、未成年の決まり(minor_ok)を通るものだけが出る。正社員の求人も同じ。"""
    jobs = load_jobs()
    minor = _every(state, minor=True)
    assert minor == {k for k, j in jobs.items() if logic.minor_ok(j)}
    assert any(jobs[k].employment_type == "正社員" for k in minor)
    assert not any("フレックス" in jobs[k].shift for k in minor)  # 18 歳未満にフレックスは使えない(安全側)


@pytest.mark.parametrize("cond", [{}, {"area": "新宿"}, {"time_band": "深夜"}, {"min_physical_level": 3},
                                  {"employment_type": "正社員"}, {"job_type": "コンビニ", "area": "東京"}])
def test_adult_search_is_unchanged(state, cond):
    """minor を付けない・false のときは今までと同じ(深夜・重い作業の求人も出る)。"""
    assert logic.recommend(state, None, **cond, minor=False) == logic.recommend(state, None, **cond)
    assert _every(state, **cond, minor=False) == _every(state, **cond)
    assert logic.relax(state, None, **cond, minor=False) == logic.relax(state, None, **cond)


def test_adult_still_sees_late_night_and_heavy_jobs(state):
    jobs = load_jobs()
    every = _every(state)
    assert every == set(jobs)  # 条件なしなら全部の求人
    assert any("深夜" in jobs[k].time_bands for k in every) and any(jobs[k].physical_level == 3 for k in every)
    assert logic.recommend(state, None, time_band="深夜")["total"] > 0
    assert logic.recommend(state, None, time_band="深夜", minor=True)["total"] == 0


def test_relax_never_offers_removing_the_minor_rule(state):
    """未成年の決まりで減った件数は、どの外す案にも数えない(「未成年を外すと N 件」を出さない)。"""
    cond = {"area": "東京", "min_physical_level": 3}
    adult, minor = logic.relax(state, None, **cond), logic.relax(state, None, **cond, minor=True)
    assert minor["total_now"] == logic.recommend(state, None, **cond, minor=True)["total"] < adult["total_now"]
    assert all(o["remove"] != logic.MINOR_ARG for o in minor["options"])
    for o in minor["options"]:  # 外したあとの件数にも、未成年の決まりが効いている
        after = {**cond, o["remove"]: None}
        assert o["total_after"] == logic.recommend(state, None, **after, minor=True)["total"]
    # 深夜の帯で探して 0 件なら、外す案は時間帯だけ。外した後の件数も未成年の決まりのまま
    assert logic.relax(state, None, time_band="深夜", minor=True) == {"total_now": 0, "options": [
        {"remove": "time_band", "current": "深夜", "total_after": logic.recommend(state, None, minor=True)["total"]}]}


def test_minor_arg_names_match_between_dip_mcp_and_voice(state, monkeypatch):
    assert voice_jobs.MINOR_ARG == logic.MINOR_ARG == "minor"
    for fn in (logic.recommend, logic.relax, server.recommend_jobs, server.relax_conditions,
               voice_jobs.build_recommend_args):
        assert logic.MINOR_ARG in inspect.signature(fn).parameters, fn.__name__
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("recommend_jobs", "relax_conditions"):
        prop = tools[name].inputSchema["properties"][logic.MINOR_ARG]
        assert prop["type"] == "boolean" and "未成年" in prop["description"]
    monkeypatch.setattr(server, "state", state)
    assert server.recommend_jobs(time_band="深夜", minor=True)["total"] == 0
    relaxed = server.relax_conditions("sim-new", time_band="深夜", minor=True)
    assert relaxed["total_now"] == 0 and [o["remove"] for o in relaxed["options"]] == ["time_band"]
    # 音声: true のときだけ引数に入れ、ゆるめる条件にはしない
    assert voice_jobs.build_recommend_args(minor=True) == {"minor": True}
    assert voice_jobs.build_recommend_args(minor=False) == {}
    assert logic.MINOR_ARG not in voice_jobs.RELAXABLE
    with pytest.raises(ValueError):
        voice_jobs.split_relaxed({"minor": True, "area": "新宿"}, ["minor"])


# ----------------------------- 時間帯のボタン(§6.2) -----------------------------

def test_time_buttons_follow_the_memo_table():
    table = {  # ボタン: (ハーネスの time_slots, 検索の time_band, 18 歳以上だけ)
        "朝": ("morning", "早朝", False),
        "昼": ("daytime", "日中", False),
        "夕方": ("evening", "夕方", False),
        "夜": ("evening", "夜", True),
        "深夜": ("late_night", "深夜", True),
    }
    assert {b: tuple(t) for b, t in Q.TIME_BUTTONS.items()} == table
    assert list(Q.TIME_BUTTONS) == list(table)
    for b, (slot, band, _) in table.items():
        assert Q.time_slot(b) == slot and Q.search_time_band(b) == band
    assert Q.time_buttons(minor=False) == ["朝", "昼", "夕方", "夜", "深夜"]
    assert Q.time_buttons(minor=True) == ["朝", "昼", "夕方"]


def test_time_button_values_are_ones_the_harness_and_search_know(state):
    assert {t.slot for t in Q.TIME_BUTTONS.values()} <= set(H.SLOTS["time_slots"]["values"])
    assert {t.time_band for t in Q.TIME_BUTTONS.values()} == set(voice_jobs.TIME_BANDS)
    for b in Q.TIME_BUTTONS:  # 直した言葉で探すと求人が出る(ボタンの言葉のままの「朝」「昼」は 0 件)
        assert logic.recommend(state, None, time_band=Q.search_time_band(b))["total"] > 0, b
    assert logic.recommend(state, None, time_band="朝")["total"] == 0
    for b in Q.time_buttons(minor=True):  # 未成年に出すボタンは、未成年の決まりで探しても求人が出る
        assert logic.recommend(state, None, time_band=Q.search_time_band(b), minor=True)["total"] > 0, b


def test_several_time_bands_find_jobs_that_match_any_of_them(state):
    """time_band を list で渡すと、どれか 1 つに合う求人を出す(task 287)。1 つの文字は今までと同じ。"""
    def ids(**kw):
        seen: set[str] = set()
        while got := {c["job_id"] for c in logic.recommend(state, None, exclude_ids=sorted(seen), **kw)["jobs"]}:
            seen |= got
        return seen

    day, evening = ids(time_band="日中"), ids(time_band="夕方")
    both = ids(time_band=["日中", "夕方"])
    assert both == day | evening and len(both) > max(len(day), len(evening))
    assert logic.recommend(state, None, time_band=["日中"])["total"] == len(day)
    assert logic.recommend(state, None, time_band=[])["total"] == logic.recommend(state, None)["total"]
    assert Q.search_time_bands(["夕方", "夜", "夕方"]) == ["夕方", "夜"] and Q.time_slots(["夕方", "夜"]) == ["evening"]
    # 理由は、合った時間帯の 1 つ(求人の帯にある方。頼んだ順の先に合ったもの)
    jobs = load_jobs()
    shown = 0
    for c in logic.recommend(state, None, time_band=["深夜", "日中"], employment_type="アルバイト・パート")["jobs"]:
        bands = jobs[c["job_id"]].time_bands
        for r in c["reasons"]:
            if "の勤務(" in r:
                assert r.split("の勤務(")[0] == next(x for x in ("深夜", "日中") if x in bands), c
                shown += 1
    assert shown
    # relax は list の時間帯も外す候補にする。未成年の決まりはそのまま
    r = logic.relax(state, None, area="埼玉", time_band=["早朝"], min_talk_level=3, minor=True)
    assert r["options"][0]["remove"] == "time_band" and r["options"][0]["current"] == ["早朝"]


def test_mcp_recommend_jobs_takes_several_time_bands(state, monkeypatch):
    """トークの AI も、保存した時間帯(「昼・夕方〜夜」)を list で渡して探せる(task 287)。"""
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("recommend_jobs", "relax_conditions"):
        prop = tools[name].inputSchema["properties"]["time_band"]
        assert {"string", "array"} <= {t.get("type") for t in prop["anyOf"]}, name
        assert "どれか 1 つ" in prop["description"], name
    monkeypatch.setattr(server, "state", state)
    r = server.recommend_jobs(time_band=["日中", "夕方"], employment_type="アルバイト・パート")
    assert r["total"] == logic.recommend(state, None, time_band=["日中", "夕方"], employment_type="アルバイト・パート")["total"]
    jobs = load_jobs()
    assert r["jobs"] and all({"日中", "夕方"} & set(jobs[c["job_id"]].time_bands) for c in r["jobs"])
