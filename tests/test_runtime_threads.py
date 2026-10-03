"""再起動後に古い会話スレッドへ戻らないこと(会話番号をチェックポイントから復元する)。LLM・MCP は使わない。"""

from __future__ import annotations

import asyncio

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from line_runtime.runtime import LineRuntime


async def _write_checkpoint(saver, thread_id: str) -> None:
    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    await saver.aput(cfg, empty_checkpoint(), {}, {})


def test_conversation_number_survives_restart(tmp_path):
    db = str(tmp_path / "ckpt.db")

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:  # 1回目の起動: 会話を2つ進めた
            rt = LineRuntime()
            rt._saver = saver
            for _ in range(2):
                await rt.new_conversation("u")
                await _write_checkpoint(saver, f"u:{await rt._current('u')}")
            assert await rt._current("u") == 2

        async with AsyncSqliteSaver.from_conn_string(db) as saver:  # 再起動: 続きは u:2、新しい会話は u:3
            rt = LineRuntime()
            rt._saver = saver
            assert await rt._current("u") == 2
            await rt.new_conversation("u")
            assert await rt._current("u") == 3
            assert await rt._current("someone-else") == 0

    asyncio.run(scenario())


def test_numbers_come_from_the_largest_thread_and_erasing_touches_only_that_user(tmp_path):
    """会話の番号は、そのユーザーの最大の番号(閉じた後は番号が1番から連続しない)。名前が前方で重なる別のユーザー
    (u1・u:x)の会話は数えず、消さない。消すのは1つのトランザクションで、keep の会話は残す。"""
    db = str(tmp_path / "ckpt.db")

    async def scenario():
        async with AsyncSqliteSaver.from_conn_string(db) as saver:
            rt = LineRuntime()
            rt._saver = saver
            for t in ("u:3", "u:10", "u1:99", "u:x:50", "u;:7", "v:1"):
                await _write_checkpoint(saver, t)
            assert await rt._current("u") == 10
            assert await rt._current("u:x") == 50
            assert await rt._current("w") == 0

            await rt._erase("u", keep=10)
            rt._threads.clear()
            assert await rt._current("u") == 10
            assert await saver.aget_tuple({"configurable": {"thread_id": "u:3"}}) is None
            await rt._erase("u")
            rt._threads.clear()
            assert await rt._current("u") == 0
            for t in ("u1:99", "u:x:50", "u;:7", "v:1"):
                assert await saver.aget_tuple({"configurable": {"thread_id": t}}) is not None

    asyncio.run(scenario())
