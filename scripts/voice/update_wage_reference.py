"""平均時給の参考データ(全国・東京)を web で調べ直して保存する。年1回くらい手で回す。

    ./.venv/bin/python scripts/voice/update_wage_reference.py

実 API(OpenAI の web_search。会話の調べものと違い、プロバイダの設定によらず OpenAI)を使うので費用がかかる(1回 $0.05 前後)。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # isort: skip

from openai import AsyncOpenAI

from voice_resume import config
from voice_resume.wages import (
    REFERENCE_PATH,
    answer_from_reference,
    fetch_reference,
    save_reference,
)


async def main() -> None:
    config.load_env()
    ref = await fetch_reference(AsyncOpenAI(), config.WAGE_UPDATE_MODEL, dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date().isoformat())
    save_reference(ref)
    print(f"-> {REFERENCE_PATH}")
    print(answer_from_reference(ref, None))


if __name__ == "__main__":
    asyncio.run(main())
