"""LINE 側ランタイム: イベント(トーク・初回訪問・再訪・dip からのプッシュ)を受けて会話を進める。

- dip の MCP サーバーとは1本のセッションを張りっぱなしにする(呼び出しごとに子プロセスを起こさない)。
- 会話は AsyncSqliteSaver に保存。入口となるイベント(トーク以外)が来たら新しい会話を始め、
  その入口に合う dip のシナリオを使う。トークの続きはそのまま同じ会話。
- こちらから話しかけるのは初回訪問・再訪の挨拶と dip からのプッシュだけ。プッシュしてよいかは dip が決め
  (dip_mcp.logic.push_trigger)、ここには許可されたものが根拠の事実つきで来る。LINE 側は判断しない。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from contextlib import AsyncExitStack
from typing import Self

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.prompts import load_mcp_prompt
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from line_runtime import config
from line_runtime.agent import LineReply, build_agent

_SERDE = JsonPlusSerializer(allowed_msgpack_modules=[
    ("line_runtime.agent", "LineReply"), ("line_runtime.agent", "Button"),
])

# イベント種別 -> (入口, エージェントへの入力。None ならユーザーの発言そのもの)
# 種別: user = ユーザーの発言で始まる / greet = トークを開いた(こちらから挨拶する) / push = dip からのプッシュ依頼
EVENTS = {
    "message": ("A", "user"),
    "visit": ("visit", "greet"),
    "revisit": ("revisit", "greet"),
    "form_exit": ("D", "push"),
}
# リッチメニューは既存運用のまま触らない(入口にしない)。メニュー相当の選択肢はトーク内のクイックリプライで出す。
STARTS_NEW_CONVERSATION = {e for e, (_, kind) in EVENTS.items() if e != "message"}
FIRST_MESSAGE = "ユーザーはまだ何も書いていません。こちらから最初のメッセージを送ってください。"
GREETINGS = {"visit": "初回訪問(はじめてトークを開いた)", "revisit": "再訪(トークを開き直した)"}


def event_input(event: str, text: str | None, facts: dict | None) -> str:
    """エージェントへの入力。ユーザー起点は発言そのもの、こちら起点はイベントと根拠の事実。"""
    kind = EVENTS[event][1]
    if kind == "user":
        if not (text or "").strip():
            raise ValueError(f"{event} には空でない text が必要です")
        return text
    if kind == "greet":
        return f"[イベント: {GREETINGS[event]}] {FIRST_MESSAGE}"
    if facts is None:
        raise ValueError(f"{event} は dip が許可したプッシュなので facts が必要です")
    return f"[イベント: dip からのプッシュ依頼 {event}] 根拠の事実: {json.dumps(facts, ensure_ascii=False)} {FIRST_MESSAGE}"


def dip_connection() -> dict:
    if config.DIP_MCP_URL:
        return {"transport": "streamable_http", "url": config.DIP_MCP_URL}
    return {"transport": "stdio", "command": sys.executable, "args": ["-m", "dip_mcp.server"],
            "cwd": str(config.ROOT), "env": dict(os.environ)}


class LineRuntime:
    def __init__(self, model=None, in_memory: bool = False) -> None:
        self._model = model
        self._in_memory = in_memory
        self._stack = AsyncExitStack()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._threads: dict[str, int] = {}  # ユーザーごとの現在の会話番号(初回参照時にチェックポイントから復元)
        self._saver = None
        self._running: set[asyncio.Task] = set()  # 進行中のターン(受け取り手が切れても最後まで進める)
        self.agent = None
        self.tools = []

    async def __aenter__(self) -> Self:
        client = MultiServerMCPClient({"dip": dip_connection()})
        session = await self._stack.enter_async_context(client.session("dip"))
        self.tools = await load_mcp_tools(session)
        scenarios = {}
        for entry in EVENTS.values():
            msgs = await load_mcp_prompt(session, "baitoru_assistant", arguments={"entry": entry[0]})
            scenarios[entry[0]] = msgs[0].content
        if self._in_memory:
            path = ":memory:"
        else:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            path = str(config.DATA_DIR / "line_checkpoints.db")
        saver = await self._stack.enter_async_context(AsyncSqliteSaver.from_conn_string(path))
        saver.serde = _SERDE
        self._saver = saver
        self.agent = build_agent(self.tools, scenarios, self._model, saver)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._running:  # 進行中のターンを終えてから MCP のセッションを閉じる
            await asyncio.gather(*self._running, return_exceptions=True)
        await self._stack.aclose()

    @staticmethod
    def _cfg(user_id: str, n: int) -> dict:
        return {"configurable": {"thread_id": f"{user_id}:{n}"}}

    async def _current(self, user_id: str) -> int:
        """現在の会話番号。再起動後は、チェックポイントに残っている最後の会話から続ける(古い会話に戻らない)。"""
        if user_id not in self._threads:
            n = 0
            while await self._saver.aget_tuple(self._cfg(user_id, n + 1)):
                n += 1
            self._threads[user_id] = n
        return self._threads[user_id]

    def user_lock(self, user_id: str) -> asyncio.Lock:
        """そのユーザーのターンと同じ排他。ターンの途中で dip の状態を初期に戻さないために使う。"""
        return self._locks[user_id]

    async def new_conversation(self, user_id: str) -> None:
        self._threads[user_id] = await self._current(user_id) + 1

    async def handle(self, user_id: str, event: str, text: str | None = None, facts: dict | None = None) -> dict:
        """1ターン進めて、返信(render の結果)を返す。途中経過が要らない呼び出し元(評価・API)向け。"""
        reply = None
        async for ev in self.handle_stream(user_id, event, text, facts):
            if ev["kind"] == "reply":
                reply = {k: v for k, v in ev.items() if k != "kind"}
        return reply

    async def handle_stream(self, user_id: str, event: str, text: str | None = None, facts: dict | None = None):
        """handle と同じ1ターンを、途中経過を順に出しながら進める(シミュレーター画面のリアルタイム表示用)。
        出すもの: llm(LLM の呼び出し1回: 時間・トークン・キャッシュ・呼んだツール)/ tool_start(ツールの実行が始まる:
        名前と引数。画面が「何をしているか」を出すために使う)/ tools(ツールの実行が終わった: 時間・引数・結果の要約)/
        reply(最後の返信。会話を保存し直した状態から作る)。
        ターン自体は裏の処理として最後まで進める。画面との接続が途中で切れて受け取りをやめても、ツールを呼んだのに
        結果が無い状態で会話が保存されることはない(そうなると次のターンが API に拒否される)。"""
        if event not in EVENTS:
            raise ValueError(f"unknown event: {event}")
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(self._turn(queue, user_id, event, text, facts))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
        await task  # ターンの中で起きた例外はここで呼び出し元に伝わる

    async def _turn(self, queue: asyncio.Queue, user_id: str, event: str, text: str | None, facts: dict | None) -> None:
        try:
            entry = EVENTS[event][0]
            content = event_input(event, text, facts)
            async with self._locks[user_id]:
                if event in STARTS_NEW_CONVERSATION:
                    await self.new_conversation(user_id)
                cfg = self._cfg(user_id, await self._current(user_id))
                snap = await self.agent.aget_state(cfg)
                first = not (snap.values or {}).get("messages")
                payload = {"messages": [{"role": "user", "content": content}]}
                if first:
                    payload |= {"entry": entry, "line_user_id": user_id}
                args: dict[str, dict] = {}
                t = time.perf_counter()
                async for chunk in self.agent.astream(payload, cfg, stream_mode="updates"):
                    now = time.perf_counter()
                    ms, t = round((now - t) * 1000), now
                    for node, update in chunk.items():
                        msgs = update.get("messages", []) if isinstance(update, dict) else []
                        if node == "model":
                            for m in msgs:
                                calls = getattr(m, "tool_calls", None) or []
                                for c in calls:
                                    args[c["id"]] = c["args"]
                                queue.put_nowait({"kind": "llm", "ms": ms, **_usage(m),
                                                  "tool_calls": [c["name"] for c in calls]})
                                if calls:
                                    # ツールの実行はここから始まる(終わりは tools イベント)。
                                    # 画面はこれで「何をしているか」の表示を出し、tools / reply / error で消す
                                    queue.put_nowait({"kind": "tool_start", "calls": [
                                        {"name": c["name"], "args": c["args"]} for c in calls]})
                        elif node == "tools":
                            queue.put_nowait({"kind": "tools", "ms": ms, "calls": [
                                {"name": m.name, "args": args.get(m.tool_call_id, {}), "summary": _summary(_tool_payload(m))}
                                for m in msgs if m.type == "tool"]})
                state = (await self.agent.aget_state(cfg)).values
            queue.put_nowait({"kind": "reply", **render(state)})
        finally:
            queue.put_nowait(None)


def _usage(msg) -> dict:
    """LLM の呼び出し1回のトークン数。input_tokens はキャッシュの読み書きを含む合計。"""
    u = getattr(msg, "usage_metadata", None) or {}
    d = u.get("input_token_details") or {}
    # langchain-anthropic は、有効期限つきの内訳(ephemeral_5m / 1h)があると cache_creation を 0 にするので、内訳を足す
    write = (d.get("ephemeral_5m_input_tokens") or 0) + (d.get("ephemeral_1h_input_tokens") or 0) or d.get("cache_creation")
    return {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
            "cache_read": d.get("cache_read"), "cache_write": write}


def _summary(result) -> str:
    """ツールの結果を1行に(画面の処理の流れに出す)。"""
    if not isinstance(result, dict):
        return str(result)[:60]
    if "error" in result:
        return f"エラー: {result['error']}"
    if "jobs" in result:
        return f"{len(result['jobs'])} 件を返す(条件に合う {result.get('total', '?')} 件)"
    if "segment" in result:
        return f"状況: {result['segment']}"
    if "recorded" in result:
        return f"記録: {result['recorded'].get('outcome')}"
    if "saved" in result:
        return f"保存: {', '.join(map(str, result['saved'])) or 'なし'}"
    return ", ".join(list(result)[:4])


def _tool_payload(msg) -> object:
    """MCP ツールの結果(テキストブロックに入った JSON)を取り出す。"""
    content = msg.content
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content


CARD_KEYS = {"job_id", "title", "employer", "hourly_wage", "shift", "station", "walk_min", "reasons"}


def _cards_in(value) -> list[dict]:
    """dip が返したデータの中にある求人カード(dip_mcp.logic.job_card の形)をすべて拾う。"""
    if isinstance(value, dict):
        found = [value] if CARD_KEYS <= value.keys() else []
        return found + [c for v in value.values() for c in _cards_in(v)]
    if isinstance(value, list):
        return [c for v in value for c in _cards_in(v)]
    return []


def render(state: dict) -> dict:
    """構造化返信を、チャネル(Web の LINE 風 UI / 将来の LINE Messaging API)が描画できる形にする。"""
    reply: LineReply = state.get("structured_response") or LineReply(bubbles=["(返信を生成できませんでした)"])
    msgs = state["messages"]
    last_human = max(i for i, m in enumerate(msgs) if m.type == "human")
    # カードにできるのは dip のツールが返した求人だけ。モデルが作った求人は出さない。
    sources = [_tool_payload(m) for m in msgs if m.type == "tool"]
    jobs: dict[str, dict] = {}
    for c in (c for src in sources for c in _cards_in(src)):
        jobs.setdefault(c["job_id"], c)  # 最初に返ったもの(たいてい recommend_jobs の、条件に沿った理由つき)を使う
    args = {tc["id"]: tc["args"] for m in msgs[last_human:] if m.type == "ai" for tc in (m.tool_calls or [])}
    turn_tools = [{"name": m.name, "args": args.get(m.tool_call_id, {}), "result": _tool_payload(m)}
                  for m in msgs[last_human:] if m.type == "tool"]
    return {
        "entry": state.get("entry"),
        "bubbles": reply.bubbles,
        "quick_replies": reply.quick_replies,
        "cards": [jobs[j] for j in reply.job_ids if j in jobs],
        "unknown_job_ids": [j for j in reply.job_ids if j not in jobs],
        # ボタンは dip の画面(LIFF)の URL だけを通す。モデルが作った URL は表示しない。
        "buttons": [b.model_dump() for b in reply.buttons if b.url.startswith("/liff/")],
        "tools": turn_tools,
    }
