"""面談の成果物のオフラインテスト(既定はファイルに書き出さないこと、日時が日本時間になること)。実 API は呼ばない。"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import subprocess
import sys
from types import SimpleNamespace

from resume_draft.schema import JST, ResumeDraft
from voice_resume import config
from voice_resume.outputs import write_outputs

TRANSCRIPT = [
    {"role": "assistant", "text": "これまでの経験を教えてください。"},
    {"role": "user", "text": "コンビニで1年半働きました。"},
]


class _FakeClient:
    def __init__(self):
        self.responses = SimpleNamespace(parse=self._parse)

    async def _parse(self, **kwargs):
        return SimpleNamespace(output_parsed=ResumeDraft(job_summary="コンビニで1年半勤務。"))


def test_stamp_and_created_at_use_japan_time(tmp_path, monkeypatch):
    """日本の深夜 0:57 の会話が UTC(前日 15:57)のファイル名にならない。

    UTC のままだと9時間ずれるので、マシンのタイムゾーンによらず不合格になる。
    """
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    monkeypatch.setattr(config, "WRITE_FILES", True)  # 開発で書き出すとき
    before = dt.datetime.now(JST).replace(microsecond=0)
    result = asyncio.run(write_outputs(TRANSCRIPT, client=_FakeClient()))
    after = dt.datetime.now(JST)

    stamp_dt = dt.datetime.strptime(result["stamp"], "%Y%m%d-%H%M%S").replace(tzinfo=JST)
    assert before <= stamp_dt <= after

    created_at = result["resume"]["created_at"]
    assert created_at.endswith("+09:00")
    assert before <= dt.datetime.fromisoformat(created_at) <= after

    # ファイル名にも同じ日本時間のスタンプが入る
    names = sorted(p.name for p in tmp_path.iterdir())
    assert f"resume-draft-{result['stamp']}.json" in names
    assert f"resume-draft-{result['stamp']}.md" in names


def test_by_default_nothing_is_written_but_the_draft_is_returned(tmp_path, monkeypatch):
    """既定では文字起こし・下書き・求人の提案をファイルに書き出さない(docs/design/privacy.md の 3.3)。
    下書きは返り値に入り、トーク(dip の状態)へ渡せる。"""
    out = tmp_path / "voice-resume"
    monkeypatch.setattr(config, "OUT_DIR", out)
    monkeypatch.setattr(config, "WRITE_FILES", False)
    result = asyncio.run(write_outputs(TRANSCRIPT, client=_FakeClient(), proposed_jobs=[{"job_id": "b0301"}]))
    assert result["files"] == []
    assert not out.exists()  # 置き場のディレクトリも作らない
    assert result["markdown"].startswith("# 履歴書(下書き)")
    assert result["resume"]["resume"]["job_summary"] == "コンビニで1年半勤務。"
    assert result["proposal"]["proposed_job_ids"] == ["b0301"]


def _write_files_setting(value: str | None) -> str:
    """VOICE_RESUME_WRITE_FILES を value にした新しいプロセスで config.WRITE_FILES を読む。"""
    env = {k: v for k, v in os.environ.items() if k != "VOICE_RESUME_WRITE_FILES"}
    if value is not None:
        env["VOICE_RESUME_WRITE_FILES"] = value
    return subprocess.run(
        [sys.executable, "-c", "from voice_resume import config; print(config.WRITE_FILES)"],
        cwd=config.ROOT, env=env, capture_output=True, text=True, check=True, timeout=60,
    ).stdout.strip()


def test_writing_files_is_off_unless_the_setting_is_1():
    """書き出しは VOICE_RESUME_WRITE_FILES=1 のときだけ(開発用)。無い、空、1 以外は書かない。"""
    assert _write_files_setting(None) == "False"
    assert _write_files_setting("") == "False"
    assert _write_files_setting("0") == "False"
    assert _write_files_setting("true") == "False"
    assert _write_files_setting("1") == "True"
