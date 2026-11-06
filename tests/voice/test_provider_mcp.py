"""事業者側 MCP サーバー(provider_mcp)との結合テスト。

外部の有料 API は呼ばない(ローカルの stdio サーバーを子プロセスで起動するだけ)。
同じ venv の python で provider_mcp.server を起動する。
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from voice_resume.provider import ProviderMCP


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """事業者の子プロセスが開発者の data/state.json を読み書きしないよう、状態を隔離する。"""
    monkeypatch.setenv("PROVIDER_STATE_PATH", str(tmp_path / "state.json"))


def test_recommend_jobs_over_mcp_with_script_wishes():
    """台本の希望(カフェ・夕方・時給1100)で、MCP 経由で最大3件の求人が返る。"""

    async def run():
        provider = ProviderMCP()
        await provider.start()
        try:
            ok = await provider.recommend_jobs(job_type="カフェ", time_band="夕方", min_wage=1100)
            bad = await provider.recommend_jobs(job_type="喫茶店")
        finally:
            await provider.aclose()
        return ok, bad

    ok, bad = asyncio.run(run())
    assert "jobs" in ok and 1 <= len(ok["jobs"]) <= 3
    assert all(j["job_type"] == "カフェ" and j["pay"]["min"] >= 1100 for j in ok["jobs"])
    assert all(j["employment_type"] == "アルバイト・パート" for j in ok["jobs"])  # 音声はアルバイト・パートだけを探す
    # 一覧に無い職種は事業者側でエラーになる(PR #25)
    assert "error" in bad


def test_zero_hit_conditions_recover_within_two_searches():
    """2026-09-26 の会話の条件(東京都内・日中・カフェ・時給1400で0件)の再現(タスク34)。

    1回目の検索は0件 → relax_conditions が「エリアを外すと見つかる」を返す →
    エリアを外した2回目の検索で求人が出る。検索は2回以内で済む。
    """

    async def run():
        provider = ProviderMCP()
        await provider.start()
        try:
            first = await provider.recommend_jobs(
                job_type="カフェ", area="東京都内", time_band="日中", min_wage=1400
            )
            relax = await provider.relax_conditions(
                job_type="カフェ", area="東京都内", time_band="日中", min_wage=1400
            )
            second = await provider.recommend_jobs(job_type="カフェ", time_band="日中", min_wage=1400)
        finally:
            await provider.aclose()
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

    事業者の状態は PROVIDER_STATE_PATH で隔離する(リポジトリの state.json を汚さない)。
    """
    monkeypatch.setenv("PROVIDER_STATE_PATH", str(tmp_path / "state.json"))

    async def run():
        provider = ProviderMCP()
        await provider.start()
        try:
            saved = await provider.save_user_preferences(
                "voice-e2e-user",
                {"job_type": "カフェ", "area": "東京都内", "min_wage": 1200,
                 "skills": "レジ打ちと品出し", "certifications": "普通自動車免許"},
            )
            rejected = await provider.save_user_preferences(
                "voice-e2e-user",
                # 個人情報らしい値は事業者側が弾く(新しい項目 skills でも同じ)
                {"area": "090-1234-5678", "skills": "調理 連絡は taro@example.com まで"},
            )
            summary = await provider.get_user_conversation_summary("voice-e2e-user")
            outcome = await provider.record_conversation_outcome(
                "voice-e2e-user", "propose", "音声相談で求人3件を提案した", 1.0
            )
        finally:
            await provider.aclose()
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
    """voice-resume からは wait を事業者に送れない(断っていない人を wait と記録しない)。"""
    provider = ProviderMCP()  # start 前でも引数の検査は先に走る
    with pytest.raises(ValueError):
        asyncio.run(provider.record_conversation_outcome("u", "wait", "今はいい", 1.0))


def test_recommend_jobs_with_wide_area_from_voice_conversation():
    """会話ログ(2026-09-26)の条件(東京都内・日中・カフェ)で、黙って0件にならず求人が返る(task 33)。"""

    async def run():
        provider = ProviderMCP()
        await provider.start()
        try:
            ok = await provider.recommend_jobs(area="東京都内", job_type="カフェ", time_band="日中")
            bad = await provider.recommend_jobs(area="原宿")
        finally:
            await provider.aclose()
        return ok, bad

    ok, bad = asyncio.run(run())
    assert ok.get("jobs") and ok["total"] >= 1
    # 一覧に無いエリアは 0件ではなくエラーと一覧になる
    assert "error" in bad and "原宿" in bad["error"] and bad["areas"]


# --- 閉じる処理(task 240) ---
# livekit-agents は終了のコールバック(ctx.add_shutdown_callback(provider.aclose))を、
# start() を呼んだ entrypoint とは別のタスクで呼ぶ。そのときも MCP をエラーなく閉じ、
# 事業者の子プロセスを残さないことを確かめる。

# 事業者の子プロセスの pid をファイルに書いてから provider_mcp.server を動かす起動コマンド
_PID_WRAPPER = """
import os, runpy, sys
open(sys.argv[1], "w").write(str(os.getpid()))
sys.argv = sys.argv[:1]
runpy.run_module("provider_mcp.server", run_name="__main__", alter_sys=True)
"""

# stdin が閉じても終わらず、SIGTERM も無視する事業者(閉じる処理が止まったときの再現)
_STUCK_WRAPPER = """
import os, runpy, signal, sys, time
open(sys.argv[1], "w").write(str(os.getpid()))
sys.argv = sys.argv[:1]
signal.signal(signal.SIGTERM, signal.SIG_IGN)
runpy.run_module("provider_mcp.server", run_name="__main__", alter_sys=True)
time.sleep(60)
"""


def _wrap_provider_command(monkeypatch, tmp_path, script: str = _PID_WRAPPER):
    pid_file = tmp_path / "provider.pid"
    monkeypatch.setattr(
        "voice_resume.provider.config.provider_server_command", lambda: [sys.executable, "-c", script, str(pid_file)]
    )
    return pid_file


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_aclose_from_another_task_like_livekit_shutdown(tmp_path, monkeypatch):
    """start() と別のタスクから aclose() しても、cancel scope のエラーを出さずに閉じ、子プロセスも残らない。"""
    pid_file = _wrap_provider_command(monkeypatch, tmp_path)

    async def run():
        provider = ProviderMCP()
        # entrypoint のタスクで start して、そのタスクは先に終わる(livekit と同じ)
        await asyncio.create_task(provider.start())
        jobs = await provider.recommend_jobs(job_type="カフェ")
        pid = int(pid_file.read_text())
        assert _alive(pid)
        # 終了のコールバックは別のタスクで呼ばれる(job_proc_lazy_main の job_shutdown_callback)
        await asyncio.wait_for(asyncio.create_task(provider.aclose()), 10)
        return jobs, pid

    jobs, pid = asyncio.run(run())
    assert jobs.get("jobs")
    assert not _alive(pid)


def test_aclose_after_entrypoint_task_was_cancelled(tmp_path, monkeypatch):
    """相談の途中でジョブが止められ、entrypoint のタスクが取り消されても、別のタスクから閉じられる。"""
    pid_file = _wrap_provider_command(monkeypatch, tmp_path)

    async def run():
        provider = ProviderMCP()
        started = asyncio.Event()

        async def entrypoint():
            await provider.start()
            started.set()
            await asyncio.sleep(3600)  # 相談の途中

        entry = asyncio.create_task(entrypoint())
        await asyncio.wait_for(started.wait(), 30)
        entry.cancel()
        await asyncio.gather(entry, return_exceptions=True)
        pid = int(pid_file.read_text())
        await asyncio.wait_for(asyncio.create_task(provider.aclose()), 10)
        return pid

    pid = asyncio.run(run())
    assert not _alive(pid)


def test_aclose_is_bounded_when_provider_does_not_exit(tmp_path, monkeypatch):
    """事業者が stdin を閉じても SIGTERM でも終わらないとき、aclose は期限で打ち切り、子プロセスを止める。"""
    pid_file = _wrap_provider_command(monkeypatch, tmp_path, _STUCK_WRAPPER)

    async def run():
        provider = ProviderMCP(close_timeout=0.5)
        await asyncio.create_task(provider.start())
        pid = int(pid_file.read_text())
        loop = asyncio.get_running_loop()
        began = loop.time()
        await asyncio.wait_for(asyncio.create_task(provider.aclose()), 10)
        return pid, loop.time() - began

    pid, elapsed = asyncio.run(run())
    # mcp の stdio_client だけだと 2 秒待って SIGTERM、さらに 2 秒で SIGKILL。期限(0.5 秒)で先に打ち切る
    assert elapsed < 2.0
    assert not _alive(pid)


def test_start_cancelled_before_connected_leaves_no_child(tmp_path, monkeypatch):
    """つなぐ途中で entrypoint が取り消されても、事業者の子プロセスを残さない。"""
    pid_file = _wrap_provider_command(monkeypatch, tmp_path)

    async def run():
        provider = ProviderMCP()
        entry = asyncio.create_task(provider.start())
        for _ in range(3000):  # 子プロセスが立ち上がるまで(最大 30 秒)
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.01)
        entry.cancel()
        result = (await asyncio.gather(entry, return_exceptions=True))[0]
        await asyncio.wait_for(asyncio.create_task(provider.aclose()), 10)  # 二度目の片付けは何もしない
        return int(pid_file.read_text()), result

    pid, result = asyncio.run(run())
    if not isinstance(result, asyncio.CancelledError):
        pytest.skip("取り消す前に接続が終わった")
    assert not _alive(pid)
