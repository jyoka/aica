"""out/ への書き出しのオフラインテスト(日時が日本時間になること)。実 API は呼ばない。"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

from voice_resume import config
from voice_resume.outputs import write_outputs
from voice_resume.schema import JST, ResumeDraft

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
