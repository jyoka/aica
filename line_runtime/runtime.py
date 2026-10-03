"""LINE 側ランタイム: イベント(トーク・初回訪問・再訪・dip からのプッシュ)を受けて会話を進める。

- dip の MCP サーバーとは1本のセッションを張りっぱなしにする(呼び出しごとに子プロセスを起こさない)。
- 会話は AsyncSqliteSaver に保存。入口となるイベント(トーク以外)が来たら新しい会話を始め、
  その入口に合う dip のシナリオを使う。トークの続きはそのまま同じ会話。
- こちらから話しかけるのは初回訪問・再訪の挨拶と dip からのプッシュだけ。プッシュしてよいかは dip が決め
  (dip_mcp.logic.push_trigger)、ここには許可されたものが根拠の事実つきで来る。LINE 側は判断しない。
- LLM の呼び出しは費用台帳(data/costs.jsonl)に1回1行、誰の会話に使ったか(user_id)つきで記録する。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Self

from langchain_core.messages import AIMessage, HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.prompts import load_mcp_prompt
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from dip_mcp.logic import liff_url
from line_runtime import config
from line_runtime.agent import LineReply, build_agent
from line_runtime.ledger import Ledger
from line_runtime.voice_reply import draft_button

_SERDE = JsonPlusSerializer(allowed_msgpack_modules=[
    ("line_runtime.agent", "LineReply"), ("line_runtime.agent", "Button"),
])

# イベント種別 -> (入口, エージェントへの入力。None ならユーザーの発言そのもの)
# 種別: user = ユーザーの発言で始まる / greet = トークを開いた(こちらから挨拶する) / push = dip からのプッシュ依頼
# / upload = 本人が履歴書をアップロードした(読み取り結果は dip に保存済み。本人の1ターンとして今の会話に入る)
EVENTS = {
    "message": ("A", "user"),
    "resume_upload": ("A", "upload"),
    "visit": ("visit", "greet"),
    "revisit": ("revisit", "greet"),
    "form_exit": ("D", "push"),
}
# リッチメニューは既存運用のまま触らない(入口にしない)。メニュー相当の選択肢はトーク内のクイックリプライで出す。
STARTS_NEW_CONVERSATION = {e for e, (_, kind) in EVENTS.items() if kind in ("greet", "push")}
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
    if kind == "upload":
        if not facts:
            raise ValueError(f"{event} には読み取り結果の facts が必要です")
        if facts.get("empty"):
            return (f"[イベント: 本人が{facts['file']}をアップロード] 読み取れた項目がありませんでした(dip には保存して"
                    f"いません)。トークには次の案内を出しました: {json.dumps(facts['shown'], ensure_ascii=False)} "
                    "この案内は繰り返さず、読み取れなかった前提で、経歴か希望を1問だけ聞いて会話を続けてください。")
        return (f"[イベント: 本人が{facts['file']}をアップロード] 読み取り結果は dip に保存済みで、トークには次の確認を"
                f"出しました: {json.dumps(facts['shown'], ensure_ascii=False)} "
                "この確認は繰り返さず、読み取り結果を見て会話を続けてください。")
    if facts is None:
        raise ValueError(f"{event} は dip が許可したプッシュなので facts が必要です")
    return f"[イベント: dip からのプッシュ依頼 {event}] 根拠の事実: {json.dumps(facts, ensure_ascii=False)} {FIRST_MESSAGE}"


# 音声相談の結果をトークに続けたターンの、本人側の記録(LLM は呼ばない。次のターンから LLM が読む文脈になる)
VOICE_EVENT = ("[イベント: 本人が音声相談を終えた] 音声で聞いた希望・経験は dip に保存済み(要約の stated_preferences)。"
               "トークには次の返信を出しました。同じことを聞き直さず、続きから会話してください。")


def voice_event(reply: dict) -> str:
    """音声相談の結果のターンの本人側の記録。トークに出した求人(ID と名前)も書き、次のターンで本人が
    カードの「詳しく見る」を押したときに、どの求人の話かを LLM が分かるようにする。"""
    jobs = [{"job_id": c["job_id"], "title": c["title"]} for c in reply.get("cards", [])]
    return VOICE_EVENT + (f" 提案した求人: {json.dumps(jobs, ensure_ascii=False)}" if jobs else "")


def dip_connection() -> dict:
    if config.DIP_MCP_URL:
        return {"transport": "streamable_http", "url": config.DIP_MCP_URL}
    return {"transport": "stdio", "command": sys.executable, "args": ["-m", "dip_mcp.server"],
            "cwd": str(config.ROOT), "env": dict(os.environ)}


class LineRuntime:
    def __init__(self, model=None, in_memory: bool = False, ledger: Ledger | None = None) -> None:
        self._model = model
        self._in_memory = in_memory
        self.ledger = ledger  # 費用台帳(LLM 呼び出し1回1行、user_id つき)。None なら __aenter__ で既定を作る
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
        if self.ledger is None:
            self.ledger = Ledger(None if self._in_memory else config.DATA_DIR / "costs.jsonl")
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

    async def current_thread(self, user_id: str) -> int:
        """そのユーザーの今の会話番号(まだ会話が無ければ 0)。"""
        return await self._current(user_id)

    async def conversation(self, user_id: str) -> dict:
        """今の会話: 番号・最後に保存した時刻(最後のやりとり)・画面に描き直す履歴・挨拶が返ったか。LLM は呼ばない。
        返信が1つも無い会話(挨拶が失敗したなど)は、時刻を None にする(続ける相手がいない)。
        greeted: トークを開いた挨拶で始まり、その返信が保存されている(画面に届いたかは問わない)。"""
        n = await self._current(user_id)
        tup = await self._saver.aget_tuple(self._cfg(user_id, n)) if n else None
        messages = (await self.agent.aget_state(self._cfg(user_id, n))).values.get("messages", []) if tup else []
        turns = history(messages, user_id)
        last_at = None
        if tup and any(t["bot"] for t in turns):
            last_at = datetime.fromisoformat(tup.checkpoint["ts"])
        first = next((m.content for m in messages if m.type == "human"), None)
        greeted = first in {event_input(e, None, None) for e in GREETINGS} and turns[0]["bot"] is not None
        return {"thread": n, "last_at": last_at, "turns": turns, "greeted": greeted}

    async def add_voice_result(self, user_id: str, reply: dict) -> bool:
        """音声相談の結果を、今の会話に AI の返信として続ける(LLM は呼ばない)。画面に描き直せるよう、返信は
        そのまま本人側の記録に添える(history がそれを返す)。まだ会話が無ければ何もしない(False)。"""
        async with self._locks[user_id]:
            n = await self._current(user_id)
            if not n:
                return False
            shown = LineReply(bubbles=reply["bubbles"], quick_replies=reply["quick_replies"],
                              job_ids=[c["job_id"] for c in reply["cards"]])
            human = HumanMessage(voice_event(reply), additional_kwargs={"voice": reply})
            update = {"messages": [human, AIMessage(shown.model_dump_json())]}
            if not (await self.agent.aget_state(self._cfg(user_id, n))).values.get("messages"):
                update |= {"entry": "A", "line_user_id": user_id}  # 閉じた後の空の会話: 結果のターンから始まる
            await self.agent.aupdate_state(self._cfg(user_id, n), update, as_node="model")
            return True

    async def close_conversation(self, user_id: str) -> None:
        """今の会話を閉じる(プロフィールで履歴書を消したとき)。それまでの会話 1..n のチェックポイントをすべて消して
        空の会話に置き換え、さらに空の会話を1つ進めて今の会話にする。前の会話の履歴(アップロードの要点・要約のツール
        結果)は描き直せず、次の会話の文脈にも入らず、サーバーの DB にも残らない。番号は戻さず進めるので、古い会話を
        開いたままのタブの発言は stale になる。空の会話は挨拶が返っていない会話と同じ扱い(続けずに新しい会話から
        始まる)。LLM は呼ばない。呼び出し元が user_lock の中で呼ぶ(進行中のターンと交差しないように)。"""
        n = await self._current(user_id)
        if not n:
            return
        for k in range(1, n + 1):
            await self._saver.adelete_thread(f"{user_id}:{k}")
        # 空のチェックポイントで番号を埋める(再起動後も _current が番号を数え直せるように。数え方は1番から連続)
        for k in range(1, n + 2):
            await self.agent.aupdate_state(self._cfg(user_id, k), {"messages": []}, as_node="model")
        self._threads[user_id] = n + 1

    async def forget(self, user_id: str) -> None:
        """そのユーザーの会話(チェックポイント)をすべて消す(プロフィールの「すべて消す」)。次の会話は1番から。
        呼び出し元が user_lock の中で呼ぶ(進行中のターンと交差しないように)。"""
        for n in range(1, await self._current(user_id) + 1):
            await self._saver.adelete_thread(f"{user_id}:{n}")
        self._threads[user_id] = 0

    async def handle(self, user_id: str, event: str, text: str | None = None, facts: dict | None = None,
                     talk_thread: int | None = None, notice: str | None = None) -> dict:
        """1ターン進めて、返信(render の結果)を返す。途中経過が要らない呼び出し元(評価・API)向け。
        会話が替わっていて送らなかったとき(talk_thread を参照)は {"stale": True, "thread": 今の会話番号}。"""
        reply = None
        async for ev in self.handle_stream(user_id, event, text, facts, talk_thread, notice):
            if ev["kind"] in ("reply", "stale"):
                reply = {k: v for k, v in ev.items() if k != "kind"} | ({"stale": True} if ev["kind"] == "stale" else {})
        return reply

    async def handle_stream(self, user_id: str, event: str, text: str | None = None, facts: dict | None = None,
                            talk_thread: int | None = None, notice: str | None = None):
        """handle と同じ1ターンを、途中経過を順に出しながら進める(シミュレーター画面のリアルタイム表示用)。
        出すもの: llm(LLM の呼び出し1回: 時間・トークン・キャッシュ・呼んだツール)/ tool_start(ツールの実行が始まる:
        名前と引数。画面が「何をしているか」を出すために使う)/ tools(ツールの実行が終わった: 時間・引数・結果の要約)/
        reply(最後の返信。会話を保存し直した状態から作る)。
        talk_thread: 本番のトーク画面が表示している会話の番号(/dev・評価は None)。ユーザーの発言なのに今の会話が
        これと違う(別のタブで新しい会話が始まった)ときは、発言を会話に入れず LLM も呼ばずに stale(今の会話番号)を
        出して終わる。None でなければ、LLM を呼ぶ前に thread(このターンの会話番号)を出す(挨拶が途中で失敗しても、
        画面は新しい会話の番号を知って、次の発言を stale にしない)。
        notice: 本人が画面(プロフィール画面・トークの切り替えの表示のボタン)で覚えていることを変えたときの知らせ(dip_mcp.profile.notice)。会話の続きの
        ターンなら、本人の入力の直前に置く(画面の履歴には出さない)。新しい会話を始めるターンでは使わない
        (最初に要約を読むので要らない)。
        ターン自体は裏の処理として最後まで進める。画面との接続が途中で切れて受け取りをやめても、ツールを呼んだのに
        結果が無い状態で会話が保存されることはない(そうなると次のターンが API に拒否される)。"""
        if event not in EVENTS:
            raise ValueError(f"unknown event: {event}")
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(self._turn(queue, user_id, event, text, facts, talk_thread, notice))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
        await task  # ターンの中で起きた例外はここで呼び出し元に伝わる

    async def _turn(self, queue: asyncio.Queue, user_id: str, event: str, text: str | None, facts: dict | None,
                    talk_thread: int | None, notice: str | None = None) -> None:
        try:
            entry = EVENTS[event][0]
            content = event_input(event, text, facts)
            async with self._locks[user_id]:
                if event in STARTS_NEW_CONVERSATION:
                    await self.new_conversation(user_id)
                n = await self._current(user_id)
                if talk_thread is not None and event not in STARTS_NEW_CONVERSATION and n != talk_thread:
                    queue.put_nowait({"kind": "stale", "thread": n})
                    return
                if talk_thread is not None:
                    queue.put_nowait({"kind": "thread", "thread": n})
                cfg = self._cfg(user_id, n)
                snap = await self.agent.aget_state(cfg)
                first = not (snap.values or {}).get("messages")
                # アップロードは本人の1ターン。画面に描き直すため、ファイルの表示名と確認の吹き出しを添えて保存する
                # (ファイル名は受け取らないので残らない)
                human = HumanMessage(content, additional_kwargs={"upload": facts} if EVENTS[event][1] == "upload" else {})
                payload = {"messages": [human]}
                if notice and not first and event not in STARTS_NEW_CONVERSATION:
                    payload = {"messages": [HumanMessage(notice, additional_kwargs={"profile_notice": True}), human]}
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
                                usage = _usage(m)
                                model_name = (getattr(m, "response_metadata", None) or {}).get("model_name") or config.MODEL
                                self.ledger.record(user_id, event, model_name, usage)
                                queue.put_nowait({"kind": "llm", "ms": ms, **usage,
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
    if "playbook" in result:
        return f"playbook: {result.get('track')}({result.get('channel')})"
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


# 給与の項目(pay。v1 の会話の記録では hourly_wage)は見ない: どちらの形の記録でも求人カードとして拾うため
CARD_KEYS = {"job_id", "title", "employer", "shift", "station", "walk_min", "reasons"}


def _cards_in(value) -> list[dict]:
    """dip が返したデータの中にある求人カード(dip_mcp.logic.job_card の形)をすべて拾う。"""
    if isinstance(value, dict):
        found = [value] if CARD_KEYS <= value.keys() else []
        return found + [c for v in value.values() for c in _cards_in(v)]
    if isinstance(value, list):
        return [c for v in value for c in _cards_in(v)]
    return []


def _reply_of(msg) -> LineReply | None:
    """会話に保存された最後の AI メッセージ(構造化返信の JSON)を LineReply に戻す。読めなければ None。"""
    content = msg.content
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text")
    try:
        return LineReply.model_validate_json(content)
    except (TypeError, ValueError):
        return None


def history(messages: list, user_id: str) -> list[dict]:
    """会話を、画面に描き直せる形にする(再読み込みで同じ会話を続けるとき)。1ターン1件:
    user(ユーザーの発言。挨拶・プッシュ・アップロードのイベントなら None)と bot(そのターンの返信。render と同じ形、
    返信が保存されていなければ None)。イベントで始まる会話でも、イベントは最初の1件だけ。
    アップロードのターンには upload(file: 「履歴書(PDF)」のような表示名、shown: 読み取り結果の確認の吹き出し、
    view_url: そのアップロードの読み取り結果の画面)も付く。音声相談の結果のターン(add_voice_result)は、保存した返信を
    そのまま bot にする。カードは render と同じく、そのターンまでに dip のツールが返した求人だけ。
    本人のデータの画面の URL(読み取り結果・下書き)は会話の記録から取らず、ここで組み立てる(ユーザーIDを載せない形。
    ユーザーID入りの URL を保存していた古い記録も、返すときには載せない)。
    プロフィールを変えた知らせ(profile_notice)は AI への入力なので、画面には出さない(前のターンの区切りにだけ使う)。"""
    bounds = [i for i, m in enumerate(messages) if m.type == "human"]
    humans = [i for i in bounds if not messages[i].additional_kwargs.get("profile_notice")]
    turns = []
    for k, start in enumerate(humans):
        end = next((b for b in bounds if b > start), len(messages))
        last = messages[end - 1]
        reply = _reply_of(last) if last.type == "ai" and not last.tool_calls else None
        content = messages[start].content
        # 会話を始めたイベント(挨拶・プッシュ)は最初の1件だけ。途中で本人が同じ書き出しで書いても発言として出す
        user = None if k == 0 and isinstance(content, str) and content.startswith("[イベント:") else content
        upload = messages[start].additional_kwargs.get("upload")
        voice = messages[start].additional_kwargs.get("voice")
        if upload or voice:
            user = None
        if upload:
            rid = upload.get("upload_id")
            upload = {k2: v for k2, v in upload.items() if k2 not in ("view_url", "upload_id")} | {
                "view_url": liff_url("/liff/resume", user_id, rid=rid) if rid is not None and not upload.get("empty")
                else None}
        bot = None
        if voice:
            has_draft = bool(voice.get("buttons")) and voice.get("voice_result") is not None
            bot = voice | {"buttons": [draft_button(user_id, voice["voice_result"])] if has_draft else []}
        elif reply is not None:
            bot = {k2: v for k2, v in render({"messages": messages[:end], "structured_response": reply}).items()
                   if k2 != "tools"}
        turns.append({"user": user, "bot": bot} | ({"upload": upload} if upload else {}))
    return turns


def track_switch(turn_tools: list[dict]) -> dict | None:
    """そのターンで雇用区分が切り替わったなら、dip が記録した切り替え(set_search_track の recorded の、id・元の区分・
    今の区分・取り消しか)。画面はこれで AI の返信の前に切り替えの表示を出す。返信文からは推測しない。
    1ターンに何度か切り替えたら、最後のもの(表示のボタンが取り消せるのは直前の切り替えだけなので)。切り替わらなければ None。"""
    recs = [t["result"]["recorded"] for t in turn_tools if t["name"] == "set_search_track"
            and isinstance(t["result"], dict) and t["result"].get("changed") and "id" in (t["result"].get("recorded") or {})]
    if not recs:
        return None
    return {k: recs[-1][k] for k in ("id", "from", "track", "undo")}


def render(state: dict) -> dict:
    """構造化返信を、チャネル(Web の LINE 風 UI / 将来の LINE Messaging API)が描画できる形にする。"""
    reply: LineReply = state.get("structured_response") or LineReply(bubbles=["(返信を生成できませんでした)"])
    msgs = state["messages"]
    last_human = max(i for i, m in enumerate(msgs) if m.type == "human")
    # カードにできるのは dip のツールが返した求人(と、音声相談の結果として dip の求人データから作り直したカード)だけ。
    # モデルが作った求人は出さない。
    sources = [_tool_payload(m) for m in msgs if m.type == "tool"]
    sources += [m.additional_kwargs["voice"]["cards"] for m in msgs if m.type == "human" and "voice" in m.additional_kwargs]
    jobs: dict[str, dict] = {}
    for c in (c for src in sources for c in _cards_in(src)):
        jobs.setdefault(c["job_id"], c)  # 最初に返ったもの(たいてい recommend_jobs の、条件に沿った理由つき)を使う
    args = {tc["id"]: tc["args"] for m in msgs[last_human:] if m.type == "ai" for tc in (m.tool_calls or [])}
    turn_tools = [{"name": m.name, "args": args.get(m.tool_call_id, {}), "result": _tool_payload(m)}
                  for m in msgs[last_human:] if m.type == "tool"]
    return {
        "entry": state.get("entry"),
        "track_switch": track_switch(turn_tools),
        "bubbles": reply.bubbles,
        "quick_replies": reply.quick_replies,
        "cards": [jobs[j] for j in reply.job_ids if j in jobs],
        "unknown_job_ids": [j for j in reply.job_ids if j not in jobs],
        # ボタンは dip の画面(LIFF)の URL だけを通す。モデルが作った URL は表示しない。
        "buttons": [b.model_dump() for b in reply.buttons if b.url.startswith("/liff/")],
        "actions": list(dict.fromkeys(reply.actions)),  # 画面が「ファイルを選ぶ」「話して伝える」のボタンにする
        "tools": turn_tools,
    }
