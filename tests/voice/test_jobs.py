"""希望条件 → recommend_jobs の引数づくりのテスト(オフライン・実 API なし)。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from voice_resume import config
from voice_resume.advisor import ResumeAdvisor
from voice_resume.jobs import (
    AREA_PHRASES,
    AREAS,
    FULLTIME_JOB_TYPES,
    FULLTIME_ONLY_KEYS,
    JOB_TYPES,
    TIME_BANDS,
    TRACKS,
    build_recommend_args,
    spoken_job_line,
)

DIP_LOGIC = Path(config.ROOT) / "dip_mcp" / "logic.py"


def _line_sim_assign(name: str) -> ast.expr:
    """dip 側の代入(JOB_TYPES など)を、コードを import せずソースから読む。"""
    tree = ast.parse(DIP_LOGIC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value
    raise AssertionError(f"dip_mcp/logic.py の {name} が見つからない")


def test_job_types_match_line_sim():
    """職種の一覧が dip 側(dip_mcp/logic.py の JOB_TYPES)と完全一致している(PR #25)。"""
    assert JOB_TYPES == tuple(ast.literal_eval(_line_sim_assign("JOB_TYPES")))


def test_areas_match_line_sim():
    """エリアの一覧が dip 側(dip_mcp/logic.py の AREAS)と完全一致している(task 33)。"""
    assert AREAS == tuple(ast.literal_eval(_line_sim_assign("AREAS")))


def test_area_phrases_match_line_sim_group_keys():
    """広い言い方の一覧が dip 側(dip_mcp/logic.py の AREA_GROUPS のキー)と完全一致している。"""
    keys = tuple(ast.literal_eval(k) for k in _line_sim_assign("AREA_GROUPS").keys)
    assert AREA_PHRASES == keys


def test_preference_labels_match_line_sim_preference_keys():
    """保存できる希望の項目と呼び名が dip 側(dip_mcp/logic.py の PREFERENCE_KEYS)と一致している(task 40)。"""
    from voice_resume.prompts import PREFERENCE_LABELS

    assert PREFERENCE_LABELS == ast.literal_eval(_line_sim_assign("PREFERENCE_KEYS"))


def test_dip_pref_keys_cover_all_line_sim_preference_keys():
    """保存方向も dip 側と同期している: save_user_preferences に渡す項目(_DIP_PREF_KEYS)が
    dip の PREFERENCE_KEYS の全項目を、同じ並びでカバーしている(experience を落とさない)。
    正社員の条件(FULLTIME_PREFERENCE_KEYS)も、音声が正社員の面談をするので受け取る(aica v2 の G)。
    正社員のチャットのヒアリングだけが聞く項目(CAREER_PREFERENCE_KEYS。task 255)は、面談では保存しない。"""
    career = ast.literal_eval(_line_sim_assign("CAREER_PREFERENCE_KEYS"))
    assert ResumeAdvisor._DIP_PREF_KEYS == tuple(k for k in ast.literal_eval(_line_sim_assign("PREFERENCE_KEYS"))
                                                 if k not in career)
    assert FULLTIME_ONLY_KEYS == tuple(ast.literal_eval(_line_sim_assign("FULLTIME_PREFERENCE_KEYS")))
    # 面談で聞く項目は propose_jobs と save_user_preferences の引数としても受け取れる(受け取れないと保存に乗らない)
    import inspect

    for tool in (ResumeAdvisor.propose_jobs, ResumeAdvisor.save_user_preferences):
        params = inspect.signature(tool).parameters
        for key in ResumeAdvisor._DIP_PREF_KEYS:
            assert key in params, (tool.__name__, key)


def test_fulltime_job_types_and_tracks_match_line_sim():
    """正社員の職種の一覧と雇用区分が dip 側(FULLTIME_JOB_TYPES・TRACKS)と完全一致している。"""
    assert FULLTIME_JOB_TYPES == tuple(ast.literal_eval(_line_sim_assign("FULLTIME_JOB_TYPES")))
    assert TRACKS == tuple(ast.literal_eval(_line_sim_assign("TRACKS")))
    doc = ResumeAdvisor.propose_jobs.__doc__ or ""
    assert all(t in doc for t in FULLTIME_JOB_TYPES)


def test_build_args_follow_the_track():
    """区分が決まっていれば employment_type をその区分にし、職種はその区分の一覧だけ。未定なら両方の一覧から選べ、
    employment_type を付けない(両方の区分から探す)。"""
    args = build_recommend_args(job_type="企画・PM", area="渋谷", min_annual_salary=500, track="正社員")
    assert args == {"job_type": "企画・PM", "area": "渋谷", "min_annual_salary": 500, "employment_type": "正社員"}
    assert build_recommend_args(job_type="カフェ", track="アルバイト・パート")["employment_type"] == "アルバイト・パート"
    assert build_recommend_args(job_type="企画・PM") == {"job_type": "企画・PM"}  # 未定
    with pytest.raises(ValueError, match="正社員の一覧にありません"):
        build_recommend_args(job_type="カフェ", track="正社員")
    with pytest.raises(ValueError):
        build_recommend_args(min_annual_salary=0, track="正社員")


def test_job_types_listed_in_tool_description():
    """LLM に見えるツール説明に、12職種と時間帯の一覧がそのまま書かれている。"""
    doc = ResumeAdvisor.propose_jobs.__doc__ or ""
    for job_type in JOB_TYPES:
        assert job_type in doc
    for band in TIME_BANDS:
        assert band in doc


def test_areas_listed_in_tool_description():
    """LLM に見えるツール説明に、11の地名と「東京都内」「どこでも」の扱いが書かれている。"""
    doc = ResumeAdvisor.propose_jobs.__doc__ or ""
    for area in AREAS:
        assert area in doc
    assert "東京都内" in doc and "どこでも" in doc and "そのまま渡す" in doc


def test_build_args_from_script_wishes():
    """台本で聞いた希望(カフェ・夕方・週2・時給1100)から検索の引数が作られる。"""
    args = build_recommend_args(job_type="カフェ", area=None, time_band="夕方", min_wage=1100)
    assert args == {"job_type": "カフェ", "time_band": "夕方", "min_wage": 1100}


def test_build_args_drops_unspecified():
    assert build_recommend_args() == {}
    assert build_recommend_args(area="中野") == {"area": "中野"}


def test_build_args_passes_area_through_for_dip_to_validate():
    """「東京都内」のような広い言い方や駅名は、言い換えずそのまま dip 側へ渡す(task 33)。"""
    assert build_recommend_args(area="東京都内") == {"area": "東京都内"}
    assert build_recommend_args(area="みなとみらい駅") == {"area": "みなとみらい駅"}


def test_build_args_rejects_unknown_job_type():
    with pytest.raises(ValueError, match="一覧にありません"):
        build_recommend_args(job_type="喫茶店")


def test_build_args_rejects_unknown_time_band():
    with pytest.raises(ValueError, match="一覧にありません"):
        build_recommend_args(time_band="昼過ぎ")


def test_build_args_rejects_bad_wage():
    with pytest.raises(ValueError):
        build_recommend_args(min_wage=0)


def test_spoken_job_line_is_short_single_sentence():
    job = {
        "job_id": "b0304", "title": "カフェホールスタッフ(週2日〜)", "employer": "喫茶モデラート",
        "job_type": "カフェ", "area": "中野", "station": "中野駅", "walk_min": 4,
        "pay": {"kind": "時給", "min": 1120, "max": None}, "pay_text": "時給1120円", "shift": "10:00-20:00", "days_min": 2,
        "perks": ["まかない"], "reasons": ["週2日から入れます"],
    }
    line = spoken_job_line(job)
    assert "カフェ" in line
    assert "中野駅" in line
    assert "時給1120円" in line
    assert "週2日から入れます" in line
    assert line.endswith("です。")
    assert len(line) <= 60  # 音声で1件を短く読める長さ

