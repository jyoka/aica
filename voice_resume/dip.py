"""dip 側 MCP サーバー(このリポジトリの dip_mcp = baitoru-mcp のシミュレーション)への薄いクライアント。

`python -m dip_mcp.server`(stdio)を子プロセスとして起動し、MCP でつなぐ。DIP_MCP_URL があれば、子プロセスは作らず、
その URL の dip(Render では Web サービスのコンテナで動く。scripts/container_start.sh)に HTTP でつなぐ。
読み取りは `recommend_jobs` / `relax_conditions` / `get_user_conversation_summary` /
`get_counseling_playbook`(雇用区分ごとの音声版の playbook)。
トークから入った相談(ユーザーID が分かるとき)だけ、書き込みも使う:
雇用区分の切り替え(`set_search_track`)、本人が話した希望の保存(`save_user_preferences`)と
会話結果・相談のまとめの記録(`record_conversation_outcome`)。書き込みには通話の部屋(room)を付け、
dip は面談の途中で本人が「すべて消す」をした部屋の書き込みを断る(docs/design/privacy.md の 3.8 の案 A)。
単体で動かすとき(ユーザー不明)はタスク 31 の決まりのまま読み取りだけで、dip の状態は書き換えない。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import timedelta

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from line_runtime import line_channel
from voice_resume import config

logger = logging.getLogger("voice-resume.dip")

# relax_conditions は dip 側で user_id が必須。ユーザーが分からないときは固定の
# 匿名IDを渡す(dip 側は未知のIDを「新規ユーザー」として読み取るだけで、記録は書かない)。
ANON_USER_ID = "voice-resume"

# 閉じるときに MCP の接続を持つタスクの終わりを待つ秒数。過ぎたらそのタスクの cancel scope を取り消す
# (anyio は取り消された子プロセスを kill する)。取り消した後も最大 _CANCEL_WAIT 秒だけ待つ。
# 合わせて livekit のジョブの終了の期限(shutdown_process_timeout の既定 10 秒)より短くする
CLOSE_TIMEOUT = 3.0
_CANCEL_WAIT = 2.0
# つなぐまでと、1 回の呼び出しの返事を待つ秒数。HTTP の dip(Web サービスのコンテナ)は、Web サービスを入れ替える間に止まる。
# 止まっている間の呼び出しは、mcp が返事を待ったまま起こさないことがあるので、期限で切る(相談はその道具なしで続く)
START_TIMEOUT = 10.0
CALL_TIMEOUT = 15.0


class DipMCP:
    """dip のツールのうち voice-resume が使うものだけを公開する MCP クライアント。"""

    def __init__(self, close_timeout: float = CLOSE_TIMEOUT, start_timeout: float = START_TIMEOUT,
                 call_timeout: float = CALL_TIMEOUT) -> None:
        self._session: ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._scope: anyio.CancelScope | None = None
        self._close_timeout = close_timeout
        self._start_timeout = start_timeout
        self._call_timeout = call_timeout

    async def start(self) -> None:
        # stdio_client と ClientSession(中は anyio の cancel scope)は、入ったタスクと同じタスクで
        # 出ないといけない。livekit は終了のコールバック(aclose)を別のタスクで呼ぶので、
        # 接続は専用のタスクが持ち、aclose はそのタスクに知らせて終わるのを待つ
        ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._hold(ready), name="dip-mcp")
        try:
            self._session = await asyncio.wait_for(ready, self._start_timeout)
        except BaseException:
            await self._close(grace=0)  # つなぐ途中で失敗・取り消し: 待たずに止めて、子プロセスを残さない
            raise
        logger.info("dip MCP サーバーに接続: %s", config.DIP_MCP_URL or " ".join(config.dip_server_command()))

    async def _hold(self, ready: asyncio.Future[ClientSession]) -> None:
        """MCP の接続を持つタスクの本体。つないで ready に渡し、閉じる知らせが来たら同じタスクの中で閉じる。"""
        with anyio.CancelScope() as self._scope:
            try:
                async with _transport() as (read, write), ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=self._call_timeout)) as session:
                    await session.initialize()
                    if not ready.done():  # start() が取り消されていれば ready も取り消し済み
                        ready.set_result(session)
                    await self._stop.wait()
            except Exception as exc:  # noqa: BLE001 - つなぐ前の失敗は start() へ、後の失敗はログだけ
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    logger.warning("dip MCP の接続でエラー: %s", type(exc).__name__)
        if not ready.done():  # つなぐ前に取り消された
            ready.cancel()

    async def recommend_jobs(self, **args) -> dict:
        """条件に合う求人(最大3件)。返り値は dip の {"total", "jobs"} か {"error", ...}。
        雇用区分は args の employment_type(今の区分。未定なら渡さず、両方の区分から探す)。"""
        result = await self._call("recommend_jobs", args)
        return _tool_result_dict(result)

    async def relax_conditions(self, **args) -> dict:
        """条件を1つずつ外すと何件になるか。返り値は dip の {"total_now", "options"}(読み取りだけ)。"""
        result = await self._call("relax_conditions", {"line_user_id": ANON_USER_ID, **args})
        return _tool_result_dict(result)

    async def get_counseling_playbook(self, track: str, channel: str = "voice") -> dict:
        """雇用区分の相談の playbook(音声版)。返り値は dip の {"track", "channel", "rules", "playbook"} か {"error"}。"""
        result = await self._call("get_counseling_playbook", {"track": track, "channel": channel})
        return _tool_result_dict(result)

    async def set_search_track(self, user_id: str, track: str, evidence: str, room: str | None = None) -> dict:
        """本人の発話を根拠に雇用区分を切り替えて、dip に記録する(evidence は本人の発言そのまま)。
        room はこの通話の部屋(「すべて消す」の前に始めた部屋なら、dip は記録せず error を返す)。"""
        result = await self._call(
            "set_search_track", {"line_user_id": user_id, "track": track, "evidence": evidence, **_room(room)}
        )
        return _tool_result_dict(result)

    async def get_user_conversation_summary(self, user_id: str) -> dict:
        """ユーザーの状況要約(チャットと同じもの)。前に本人が話した希望と相談で分かったことは stated_preferences、
        前の会話の相談のまとめは consultation_summary、アップロードした履歴書は uploaded_resume に入っている。"""
        result = await self._call("get_user_conversation_summary", {"line_user_id": user_id})
        return _tool_result_dict(result)

    async def save_user_preferences(self, user_id: str, preferences: dict, room: str | None = None) -> dict:
        """本人が会話で話した希望を、本人の言葉のまま dip に保存する(推測は渡さない)。
        room はこの通話の部屋(「すべて消す」の前に始めた部屋なら、dip は何も保存せず error を返す)。"""
        result = await self._call(
            "save_user_preferences", {"line_user_id": user_id, "preferences": preferences, **_room(room)}
        )
        return _tool_result_dict(result)

    async def record_conversation_outcome(
        self, user_id: str, outcome: str, reason: str, confidence: float, summary: str | None = None,
        room: str | None = None,
    ) -> dict:
        """会話の結果と相談のまとめ(summary。200字以内)を記録する。voice-resume からは propose / consult だけを記録する
        (wait は本人が断ったときだけ。断っていない人を wait と記録しない決まりを守る)。room は set_search_track と同じ。"""
        if outcome not in ("propose", "consult"):
            raise ValueError("voice-resume から記録できる outcome は propose / consult だけ")
        result = await self._call("record_conversation_outcome", {
            "line_user_id": user_id, "outcome": outcome, "reason": reason,
            "confidence": confidence, **({"summary": summary} if summary else {}), **_room(room),
        })
        return _tool_result_dict(result)

    async def _call(self, name: str, args: dict):
        if self._session is None:
            raise RuntimeError("DipMCP.start() が呼ばれていません")
        return await self._session.call_tool(name, args)

    async def aclose(self) -> None:
        """接続を持つタスクに閉じるよう知らせ、終わるのを待つ(どのタスクから呼んでもよい)。
        期限までに終わらなければ取り消して、子プロセスを止める。2回目以降は何もしない。"""
        await self._close(self._close_timeout)

    async def _close(self, grace: float) -> None:
        task, self._task = self._task, None
        self._session = None
        if task is None:
            return
        self._stop.set()
        done, _ = await asyncio.wait({task}, timeout=grace)
        if done:
            return
        if grace > 0:
            logger.warning("dip MCP の接続が %g 秒で閉じないので取り消します", grace)
        if self._scope is not None:
            # 一度きりの task.cancel() と違い、cancel scope の取り消しは中の await すべてに効く
            # (anyio は子プロセスの終わりを待つところで取り消されると kill する)
            self._scope.cancel()
        else:
            task.cancel()  # まだ動き始めていない
        done, _ = await asyncio.wait({task}, timeout=_CANCEL_WAIT)
        if not done:
            logger.error("dip MCP の接続を取り消しても %g 秒で終わりません", _CANCEL_WAIT)


@asynccontextmanager
async def _transport():
    """dip への接続の (read, write)。DIP_MCP_URL があれば HTTP(共有鍵を付ける)、無ければ stdio の子プロセス。"""
    if config.DIP_MCP_URL:
        key = os.environ.get(line_channel.INTERNAL_KEY_ENV, "").strip()
        headers = {"Authorization": f"Bearer {key}"} if key else None
        async with create_mcp_http_client(headers=headers) as http, \
                streamable_http_client(config.DIP_MCP_URL, http_client=http) as (read, write, _):
            yield read, write
        return
    command = config.dip_server_command()
    params = StdioServerParameters(
        # 環境変数を渡す(line_runtime と同じ)。テストは DIP_STATE_PATH で状態を隔離する
        command=command[0], args=command[1:], cwd=str(config.ROOT), env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write):
        yield read, write


def _room(room: str | None) -> dict:
    """書き込むツールに付ける部屋の引数(部屋が分からなければ付けない)。"""
    return {"room": room} if room else {}


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
