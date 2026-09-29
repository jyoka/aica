"""dip 側 MCP サーバー(dip_mcp)との結合テスト。

外部の有料 API は呼ばない(ローカルの stdio サーバーを子プロセスで起動するだけ)。
同じ venv の python で dip_mcp.server を起動する。
"""

from __future__ import annotations

import asyncio

import pytest

from voice_resume.dip import DipMCP


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """dip の子プロセスが開発者の data/state.json を読み書きしないよう、状態を隔離する。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))


def test_recommend_jobs_over_mcp_with_script_wishes():
    """台本の希望(カフェ・夕方・時給1100)で、MCP 経由で最大3件の求人が返る。"""

    async def run():
        dip = DipMCP()
        await dip.start()
        try:
            ok = await dip.recommend_jobs(job_type="カフェ", time_band="夕方", min_wage=1100)
            bad = await dip.recommend_jobs(job_type="喫茶店")
        finally:
            await dip.aclose()
        return ok, bad

    ok, bad = asyncio.run(run())
    assert "jobs" in ok and 1 <= len(ok["jobs"]) <= 3
    assert all(j["job_type"] == "カフェ" and j["pay"]["min"] >= 1100 for j in ok["jobs"])
    assert all(j["employment_type"] == "アルバイト・パート" for j in ok["jobs"])  # 音声はアルバイト・パートだけを探す
    # 一覧に無い職種は dip 側でエラーになる(PR #25)
    assert "error" in bad


def test_zero_hit_conditions_recover_within_two_searches():
    """2026-09-26 の会話の条件(東京都内・日中・カフェ・時給1400で0件)の再現(タスク34)。

    1回目の検索は0件 → relax_conditions が「エリアを外すと見つかる」を返す →
    エリアを外した2回目の検索で求人が出る。検索は2回以内で済む。
    """

    async def run():
        dip = DipMCP()
        await dip.start()
        try:
            first = await dip.recommend_jobs(
                job_type="カフェ", area="東京都内", time_band="日中", min_wage=1400
            )
            relax = await dip.relax_conditions(
                job_type="カフェ", area="東京都内", time_band="日中", min_wage=1400
            )
            second = await dip.recommend_jobs(job_type="カフェ", time_band="日中", min_wage=1400)
        finally:
            await dip.aclose()
        return first, relax, second

    first, relax, second = asyncio.run(run())
    assert first == {"total": 0, "jobs": []}
    assert relax["total_now"] == 0
    # 外すと増える条件は複数あり得る(職種・時給など)。本人が選ぶ材料としてエリアが入っていればよい
    removes = {o["remove"]: o for o in relax["options"]}
    assert "area" in removes
    assert removes["area"]["current"] == "東京都内"
    assert removes["area"]["total_after"] >= 1
    # エリアを外した2回目の検索で求人が出る
    assert second["total"] >= 1 and second["jobs"]


def test_saved_wishes_show_up_in_the_next_conversation_summary(tmp_path, monkeypatch):
    """音声で話した希望を保存すると、次の会話の要約(stated_preferences)に本人の言葉のまま出る(task 40)。

    dip の状態は DIP_STATE_PATH で隔離する(リポジトリの state.json を汚さない)。
    """
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))

    async def run():
        dip = DipMCP()
        await dip.start()
        try:
            saved = await dip.save_user_preferences(
                "voice-e2e-user",
                {"job_type": "カフェ", "area": "東京都内", "min_wage": 1200,
                 "skills": "レジ打ちと品出し", "certifications": "普通自動車免許"},
            )
            rejected = await dip.save_user_preferences(
                "voice-e2e-user",
                # 個人情報らしい値は dip 側が弾く(新しい項目 skills でも同じ)
                {"area": "090-1234-5678", "skills": "調理 連絡は taro@example.com まで"},
            )
            summary = await dip.get_user_conversation_summary("voice-e2e-user")
            outcome = await dip.record_conversation_outcome(
                "voice-e2e-user", "propose", "音声相談で求人3件を提案した", 1.0
            )
        finally:
            await dip.aclose()
        return saved, rejected, summary, outcome

    saved, rejected, summary, outcome = asyncio.run(run())
    assert set(saved["saved"]) == {"job_type", "area", "min_wage", "skills", "certifications"}
    assert set(rejected["rejected"]) == {"area", "skills"}
    stated = summary["stated_preferences"]
    assert {k: v["value"] for k, v in stated.items()} == {
        "job_type": "カフェ", "area": "東京都内", "min_wage": "1200",
        "skills": "レジ打ちと品出し", "certifications": "普通自動車免許",
    }
    assert outcome["recorded"]["outcome"] == "propose"


def test_record_conversation_outcome_refuses_wait_client_side():
    """voice-resume からは wait を dip に送れない(断っていない人を wait と記録しない)。"""
    dip = DipMCP()  # start 前でも引数の検査は先に走る
    with pytest.raises(ValueError):
        asyncio.run(dip.record_conversation_outcome("u", "wait", "今はいい", 1.0))


def test_recommend_jobs_with_wide_area_from_voice_conversation():
    """会話ログ(2026-09-26)の条件(東京都内・日中・カフェ)で、黙って0件にならず求人が返る(task 33)。"""

    async def run():
        dip = DipMCP()
        await dip.start()
        try:
            ok = await dip.recommend_jobs(area="東京都内", job_type="カフェ", time_band="日中")
            bad = await dip.recommend_jobs(area="原宿")
        finally:
            await dip.aclose()
        return ok, bad

    ok, bad = asyncio.run(run())
    assert ok.get("jobs") and ok["total"] >= 1
    # 一覧に無いエリアは 0件ではなくエラーと一覧になる
    assert "error" in bad and "原宿" in bad["error"] and bad["areas"]
