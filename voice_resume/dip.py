"""dip 側 MCP サーバー(このリポジトリの dip_mcp = baitoru-mcp のシミュレーション)への薄いクライアント。

`python -m dip_mcp.server`(stdio)を子プロセスとして起動し、MCP でつなぐ。
読み取りは `recommend_jobs` / `relax_conditions` / `get_user_conversation_summary`。
トークから入った相談(ユーザーID が分かるとき)だけ、書き込みも使う:
本人が話した希望の保存(`save_user_preferences`)と会話結果の記録
(`record_conversation_outcome`)。単体で動かすとき(ユーザー不明)は
タスク 31 の決まりのまま読み取りだけで、dip の状態は書き換えない。
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from voice_resume import config

logger = logging.getLogger("voice-resume.dip")

# relax_conditions は dip 側で user_id が必須。ユーザーが分からないときは固定の
# 匿名IDを渡す(dip 側は未知のIDを「新規ユーザー」として読み取るだけで、記録は書かない)。
ANON_USER_ID = "voice-resume"


class DipMCP:
    """dip のツールのうち voice-resume が使うものだけを公開する MCP クライアント。"""

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        command = config.dip_server_command()
        params = StdioServerParameters(
            # 環境変数を渡す(line_runtime と同じ)。テストは DIP_STATE_PATH で状態を隔離する
            command=command[0], args=command[1:], cwd=str(config.ROOT), env=dict(os.environ)
        )
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        logger.info("dip MCP サーバーに接続: %s", " ".join(command))

    async def recommend_jobs(self, **args) -> dict:
        """条件に合う求人(最大3件)。返り値は dip の {"total", "jobs"} か {"error", ...}。"""
        if self._session is None:
            raise RuntimeError("DipMCP.start() が呼ばれていません")
        result = await self._session.call_tool("recommend_jobs", args)
        return _tool_result_dict(result)

    async def relax_conditions(self, **args) -> dict:
        """条件を1つずつ外すと何件になるか。返り値は dip の {"total_now", "options"}(読み取りだけ)。"""
        if self._session is None:
            raise RuntimeError("DipMCP.start() が呼ばれていません")
        result = await self._session.call_tool(
            "relax_conditions", {"line_user_id": ANON_USER_ID, **args}
        )
        return _tool_result_dict(result)

    async def get_user_conversation_summary(self, user_id: str) -> dict:
        """ユーザーの状況要約(チャットと同じもの)。前に本人が話した希望と相談で分かったことは stated_preferences、
        前の会話の相談のまとめは consultation_summary、アップロードした履歴書は uploaded_resume に入っている。"""
        result = await self._call("get_user_conversation_summary", {"line_user_id": user_id})
        return _tool_result_dict(result)

    async def save_user_preferences(self, user_id: str, preferences: dict) -> dict:
        """本人が会話で話した希望を、本人の言葉のまま dip に保存する(推測は渡さない)。"""
        result = await self._call(
            "save_user_preferences", {"line_user_id": user_id, "preferences": preferences}
        )
        return _tool_result_dict(result)

    async def record_conversation_outcome(
        self, user_id: str, outcome: str, reason: str, confidence: float
    ) -> dict:
        """会話の結果を記録する。voice-resume からは propose / consult だけを記録する
        (wait は本人が断ったときだけ。断っていない人を wait と記録しない決まりを守る)。"""
        if outcome not in ("propose", "consult"):
            raise ValueError("voice-resume から記録できる outcome は propose / consult だけ")
        result = await self._call("record_conversation_outcome", {
            "line_user_id": user_id, "outcome": outcome, "reason": reason,
            "confidence": confidence,
        })
        return _tool_result_dict(result)

    async def _call(self, name: str, args: dict):
        if self._session is None:
            raise RuntimeError("DipMCP.start() が呼ばれていません")
        return await self._session.call_tool(name, args)

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None


def _tool_result_dict(result) -> dict:
    """MCP の CallToolResult から dict を取り出す(structuredContent 優先、text の JSON も許す)。"""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
    return {"error": "ツールの結果を読み取れませんでした"}
