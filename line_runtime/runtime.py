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

from dip_mcp.logic import ENTRIES, VOICE_ROOM_ARG, liff_url
from line_runtime import config
from line_runtime.agent import LineReply, build_agent
from line_runtime.arcade_reply import arcade_history_reply
from line_runtime.ledger import Ledger
from line_runtime.line_channel import ARCADE_LABEL
from line_runtime.voice_reply import voice_history_reply
from resume_draft.pii import REDACTED, redact_personal

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
# LINE のリッチメニュー(scripts/line_rich_menu.py)は新しいイベントを作らない: 音声相談・履歴書・ゲームは LIFF 画面を直に開き、
# 「求人を探す」は本人の発言(message)になる。メニュー相当の選択肢はトーク内のクイックリプライで出す。
STARTS_NEW_CONVERSATION = {e for e, (_, kind) in EVENTS.items() if kind in ("greet", "push")}
FIRST_MESSAGE = "ユーザーはまだ何も書いていません。こちらから最初のメッセージを送ってください。"
GREETINGS = {"visit": "初回訪問(はじめてトークを開いた)", "revisit": "再訪(トークを開き直した)"}
GREET_ENTRIES = {EVENTS[e][0] for e in GREETINGS}  # 挨拶で始まった会話の入口(会話の state の entry)
SUMMARY_TOOL = "get_user_conversation_summary"  # 挨拶の入口のボタンを決めるために読む dip の要約(task 237)
# 会話の記録の表(AsyncSqliteSaver のスキーマ)。1人の会話を消すときは、これらから1つのトランザクションで消す
CONVERSATION_TABLES = ("checkpoints", "writes")


def _threads_of(user_id: str) -> tuple[int, str, tuple]:
    """SQL の条件: そのユーザーの会話の行(thread_id が「ユーザーID:番号」)。範囲の比較で主キーの索引を使い、
    番号の部分が数字だけの行に絞る(ユーザー「u」の会話に、ユーザー「u:x」の会話「u:x:1」を含めない)。
    返すのは (番号の始まる位置(substr の引数), 条件, 条件の引数)。"""
    start = len(user_id) + 2
    cond = ("thread_id > ? AND thread_id < ? AND substr(thread_id, ?) <> '' "
            "AND substr(thread_id, ?) NOT GLOB '*[^0-9]*'")
    return start, cond, (f"{user_id}:", f"{user_id};", start, start)


def event_input(event: str, text: str | None, facts: dict | None) -> str:
    """エージェントへの入力。ユーザー起点は発言(電話番号・メール・住所らしい所を伏せた文)、こちら起点はイベントと根拠の事実。
    発言は Web と LINE では turn.accept が伏せ済み。ここでも伏せるのは、評価などが handle を直に呼んでも、伏せない文が
    LLM と会話の保存に届かないように(redact_personal は当たる所が無くなるまで伏せるので、伏せた文をもう一度伏せても変わらない)。"""
    kind = EVENTS[event][1]
    if kind == "user":
        if not (text or "").strip():
            raise ValueError(f"{event} には空でない text が必要です")
        return redact_personal(text)
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
    カードの「詳しく見る」を押したときに、どの求人の話かを LLM が分かるようにする。
    音声で雇用区分が切り替わることもあるので、面談の後の区分も書く(違えば playbook を読み直させる)。"""
    jobs = [{"job_id": c["job_id"], "title": c["title"]} for c in reply.get("cards", [])]
    track = reply.get("search_track")
    return (VOICE_EVENT + (f" 提案した求人: {json.dumps(jobs, ensure_ascii=False)}" if jobs else "")
            + (f" 音声相談の後の雇用区分(search_track)は「{track}」。この会話で使っていた区分と違えば、次の返信の前に"
               f" get_counseling_playbook を「{track}」で読み直し、求人を探すときの employment_type もそれに合わせる。"
               if track else ""))


# ゲームで下書きを保存したターンの、本人側の記録(LLM は呼ばない。次のターンから LLM が読む文脈になる)
ARCADE_EVENT = ("[イベント: 本人がゲーム(レジュメ・アーケード)で書類の下書きを保存した] 下書きは dip に保存済み"
                "(要約の arcade_draft)。トークには次の返信を出しました。同じことを聞き直さず、続きから会話してください。")


def arcade_event(reply: dict) -> str:
    return ARCADE_EVENT + f" 書類: {reply.get('document_kind')}、数字の空欄: {reply.get('blanks', 0)}か所。"


def usable_entries(channel: str | None, features: list[str] | tuple[str, ...] = ()) -> tuple[str, ...]:
    """窓口で使える入口(dip_mcp.logic.ENTRIES の並び)。LINE は features(line_channel.liff_features)にあるものだけ。
    Web は features が空なら 3 つとも(評価と開発者画面の既定。agent.channel_note と同じ決まり)、空でなければ features
    (本番のトーク画面の line_channel.web_features。音声相談は LiveKit の設定があるときだけ)。"""
    if channel != "line" and not features:
        return ENTRIES
    return tuple(e for e in ENTRIES if e in features)


def greeting_entries(summary: dict, channel: str | None, features: list[str] | tuple[str, ...] = ()) -> dict:
    """挨拶に付ける入口のボタン(task 237): 本人がまだやり終えていない(summary の used_entries に無い)入口のうち、
    窓口で使えるもの。音声相談と履歴書は actions、ゲームは buttons(summary の arcade_url。無ければ出さない)。
    3 つともやり終えていれば、どちらも空(LINE はリッチメニューから入れる)。"""
    used = set(summary.get("used_entries") or ())
    left = [e for e in usable_entries(channel, features) if e not in used]
    url = summary.get("arcade_url")
    return {"actions": [e for e in left if e != "arcade"],
            "buttons": [{"label": ARCADE_LABEL, "url": url}] if "arcade" in left and url else []}


def is_arcade_entry(url: str) -> bool:
    """ゲーム(レジュメ・アーケード)の入口のボタンの URL か(ハブと各ゲーム。下書きの画面 /liff/arcade/draft は入口ではない)。"""
    path = url.partition("?")[0]
    return (path == "/liff/arcade" or path.startswith("/liff/arcade/")) and not path.startswith("/liff/arcade/draft")


def dip_connection() -> dict:
    if config.DIP_MCP_URL:
        return {"transport": "streamable_http", "url": config.DIP_MCP_URL}
    return {"transport": "stdio", "command": sys.executable, "args": ["-m", "dip_mcp.server"],
            "cwd": str(config.ROOT), "env": dict(os.environ)}


def chat_tools(tools: list) -> list:
    """トークの AI に渡す dip のツール。音声ワーカーだけが渡す部屋の引数(VOICE_ROOM_ARG)は、AI に見せない。"""
    for tool in tools:
        schema = tool.args_schema
        if isinstance(schema, dict) and VOICE_ROOM_ARG in schema.get("properties", {}):
            props = {k: v for k, v in schema["properties"].items() if k != VOICE_ROOM_ARG}
            tool.args_schema = schema | {"properties": props}
    return tools


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
        self.tools = chat_tools(await load_mcp_tools(session))
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

    async def _read_summary(self, user_id: str) -> dict:
        """dip の要約(get_user_conversation_summary)を、エージェントと同じ MCP のツールで読む(LLM は呼ばない)。"""
        tool = next(t for t in self.tools if t.name == SUMMARY_TOOL)
        msg = await tool.ainvoke({"type": "tool_call", "name": SUMMARY_TOOL, "args": {"line_user_id": user_id},
                                  "id": "greeting-entries"})
        summary = _tool_payload(msg)
        if not isinstance(summary, dict):
            raise TypeError(f"dip の要約を読めませんでした: {str(summary)[:200]}")
        return summary

    @staticmethod
    def _cfg(user_id: str, n: int) -> dict:
        return {"configurable": {"thread_id": f"{user_id}:{n}"}}

    async def _current(self, user_id: str) -> int:
        """現在の会話番号。再起動後は、チェックポイントに残っている最大の番号の会話から続ける(古い会話に戻らない。
        会話を閉じた後は、前の会話を消して空の会話だけが残るので、番号は1番から連続しない)。"""
        if user_id not in self._threads:
            start, cond, args = _threads_of(user_id)
            await self._saver.setup()
            async with self._saver.lock, self._saver.conn.execute(
                    f"SELECT MAX(CAST(substr(thread_id, ?) AS INTEGER)) FROM checkpoints WHERE {cond}",
                    (start, *args)) as cur:
                (n,) = await cur.fetchone()
            self._threads[user_id] = n or 0
        return self._threads[user_id]

    async def _erase(self, user_id: str, keep: int | None = None) -> None:
        """そのユーザーの会話の記録(チェックポイントと書き込み)を1つのトランザクションで消す。keep の番号の会話は残す。
        途中で失敗したら巻き戻す(落ちたときは SQLite が捨てる)ので、全部消えるか何も消えないかのどちらか。"""
        saver = self._saver
        await saver.setup()
        _, cond, args = _threads_of(user_id)
        if keep is not None:
            cond, args = f"{cond} AND thread_id <> ?", (*args, f"{user_id}:{keep}")
        async with saver.lock, saver.conn.cursor() as cur:  # lock: ほかのユーザーの書き込みの commit を挟まない
            try:
                for table in CONVERSATION_TABLES:
                    await cur.execute(f"DELETE FROM {table} WHERE {cond}", args)
                await saver.conn.commit()
            except BaseException:
                await saver.conn.rollback()
                raise

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
        greeted: トークを開いた挨拶で始まり、その返信が保存されている(画面に届いたかは問わない)。挨拶で始まったかは
        会話を始めたときの入口(state の entry)で決める(挨拶の文言を変えても、前の文言の挨拶を見分けられる)。
        entry の無い古い記録だけ、最初の入力を今の挨拶文と比べる。"""
        n = await self._current(user_id)
        tup = await self._saver.aget_tuple(self._cfg(user_id, n)) if n else None
        values = (await self.agent.aget_state(self._cfg(user_id, n))).values if tup else {}
        messages = values.get("messages", [])
        turns = history(messages, user_id, values.get("greeting_entries"))
        last_at = None
        if tup and any(t["bot"] for t in turns):
            last_at = datetime.fromisoformat(tup.checkpoint["ts"])
        if values.get("entry") is not None:
            opened = values["entry"] in GREET_ENTRIES
        else:  # entry を残す前の古い記録
            first = next((m.content for m in messages if m.type == "human"), None)
            opened = first in {event_input(e, None, None) for e in GREETINGS}
        greeted = opened and bool(turns) and turns[0]["bot"] is not None
        return {"thread": n, "last_at": last_at, "turns": turns, "greeted": greeted}

    async def add_voice_result(self, user_id: str, reply: dict) -> bool:
        """音声相談の結果を、今の会話に AI の返信として続ける(LLM は呼ばない)。画面に描き直せるよう、返信は
        そのまま本人側の記録に添える(history がそれを返す)。まだ会話が無ければ何もしない(False)。"""
        return await self._add_reply(user_id, reply, voice_event(reply), "voice")

    async def add_arcade_result(self, user_id: str, reply: dict) -> bool:
        """ゲームで保存した下書きの知らせ(line_runtime.arcade_reply)を、今の会話に AI の返信として続ける
        (LLM は呼ばない。add_voice_result と同じ作り)。まだ会話が無ければ何もしない(False)。"""
        return await self._add_reply(user_id, reply, arcade_event(reply), "arcade")

    async def _add_reply(self, user_id: str, reply: dict, event_text: str, key: str) -> bool:
        async with self._locks[user_id]:
            n = await self._current(user_id)
            if not n:
                return False
            shown = LineReply(bubbles=reply["bubbles"], quick_replies=reply["quick_replies"],
                              job_ids=[c["job_id"] for c in reply["cards"]])
            human = HumanMessage(event_text, additional_kwargs={key: reply})
            update = {"messages": [human, AIMessage(shown.model_dump_json())]}
            if not (await self.agent.aget_state(self._cfg(user_id, n))).values.get("messages"):
                update |= {"entry": "A", "line_user_id": user_id}  # 閉じた後の空の会話: 結果のターンから始まる
            await self.agent.aupdate_state(self._cfg(user_id, n), update, as_node="model")
            return True

    async def close_conversation(self, user_id: str) -> None:
        """今の会話を閉じる(プロフィールで履歴書を消したとき)。空の会話 n+1 を書いて今の会話にし、それまでの会話
        1..n のチェックポイントを1つのトランザクションですべて消す。前の会話の履歴(アップロードの要点・要約のツール
        結果)は描き直せず、次の会話の文脈にも入らず、サーバーの DB にも残らない。番号は戻さず進める(再起動後も
        _current が最大の番号の n+1 から数える)ので、古い会話を開いたままのタブの発言は stale になる。空の会話は
        挨拶が返っていない会話と同じ扱い(続けずに新しい会話から始まる)。
        空の会話を先に書くので、消す途中で落ちても今の会話は空の n+1 のまま(前の会話は全部残るか全部消えるかで、
        残っても文脈には戻らない。もう一度閉じれば消える)。LLM は呼ばない。
        呼び出し元が user_lock の中で呼ぶ(進行中のターンと交差しないように)。"""
        n = await self._current(user_id)
        if not n:
            return
        await self.agent.aupdate_state(self._cfg(user_id, n + 1), {"messages": []}, as_node="model")
        self._threads[user_id] = n + 1
        await self._erase(user_id, keep=n + 1)

    async def forget(self, user_id: str) -> None:
        """そのユーザーの会話(チェックポイント)をすべて、1つのトランザクションで消す(プロフィールの「すべて消す」と、
        期間で消す掃除のユーザー)。次の会話は1番から。途中で失敗したら何も消えていない(もう一度押せばやり直せる)。
        呼び出し元が user_lock の中で呼ぶ(進行中のターンと交差しないように)。"""
        await self._erase(user_id)
        self._threads.pop(user_id, None)  # 次に参照したとき、残りの会話(無いので 0)から数え直す

    async def conversations_of(self, person_id: str) -> list[dict]:
        """その人の会話のすべて(プロフィールの「データを書き出す」)。シナリオユーザー(「匿名ID:シナリオユーザー」)の会話も入る。
        1 つの会話は {user_id, thread, last_at, turns}。turns は画面に描き直す形(history)。本人の会話を先に、番号の順。LLM は呼ばない。"""
        await self._saver.setup()
        async with self._saver.lock, self._saver.conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id > ? AND thread_id < ?",
                (f"{person_id}:", f"{person_id};")) as cur:
            threads = [row[0] for row in await cur.fetchall()]
        out = []
        for thread_id in threads:
            user_id, _, n = thread_id.rpartition(":")
            if not n.isdigit():
                continue
            cfg = {"configurable": {"thread_id": thread_id}}
            values = (await self.agent.aget_state(cfg)).values
            at = await self._last_at(thread_id)
            out.append({"user_id": user_id, "thread": int(n), "last_at": at.isoformat() if at else None,
                        "turns": history(values.get("messages", []), user_id, values.get("greeting_entries"))})
        return sorted(out, key=lambda c: (c["user_id"] != person_id, c["user_id"], c["thread"]))

    async def _last_at(self, thread_id: str) -> datetime | None:
        """その会話の最後のやりとり(最新のチェックポイントの保存時刻、UTC)。会話が無ければ None。"""
        tup = await self._saver.aget_tuple({"configurable": {"thread_id": thread_id}})
        return datetime.fromisoformat(tup.checkpoint["ts"]) if tup else None

    async def old_threads(self, cutoff: datetime) -> list[str]:
        """最後のやりとりが cutoff より前の会話の thread_id(期間で消す掃除が、会話ごとに消す)。"""
        await self._saver.setup()
        async with self._saver.lock, self._saver.conn.execute("SELECT DISTINCT thread_id FROM checkpoints") as cur:
            threads = [row[0] for row in await cur.fetchall()]
        return [t for t in threads if (at := await self._last_at(t)) is not None and at < cutoff]

    async def erase_old_thread(self, thread_id: str, cutoff: datetime) -> bool:
        """その会話の記録を1つのトランザクションで消す。消す前に、最後のやりとりが今も cutoff より前かを確かめ直す
        (掃除が対象を決めた後に続いていたら消さない)。今の会話を消しても番号は戻さない(_threads はそのまま。
        次にトークを開くと、返信の無い会話として新しい会話から始まる)。
        呼び出し元が、その会話のユーザーの user_lock の中で呼ぶ。"""
        at = await self._last_at(thread_id)
        if at is None or at >= cutoff:
            return False
        saver = self._saver
        async with saver.lock, saver.conn.cursor() as cur:
            try:
                for table in CONVERSATION_TABLES:
                    await cur.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
                await saver.conn.commit()
            except BaseException:
                await saver.conn.rollback()
                raise
        return True

    async def vacuum(self) -> None:
        """会話の DB を詰める(消した行がファイルの空き領域に残らないように。期間で消す掃除の後)。
        AsyncSqliteSaver は WAL で書くので、VACUUM の後に WAL も本体に書き戻して空にする(WAL のファイルにも残さない)。"""
        await self._saver.setup()
        async with self._saver.lock:
            await self._saver.conn.execute("VACUUM")
            await self._saver.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    async def handle(self, user_id: str, event: str, text: str | None = None, facts: dict | None = None,
                     talk_thread: int | None = None, notice: str | None = None, new_conversation: bool = False,
                     channel: str = "web", features: tuple[str, ...] = ()) -> dict:
        """1ターン進めて、返信(render の結果)を返す。途中経過が要らない呼び出し元(評価・API)向け。
        会話が替わっていて送らなかったとき(talk_thread を参照)は {"stale": True, "thread": 今の会話番号}。"""
        reply = None
        async for ev in self.handle_stream(user_id, event, text, facts, talk_thread, notice, new_conversation,
                                           channel, features):
            if ev["kind"] in ("reply", "stale"):
                reply = {k: v for k, v in ev.items() if k != "kind"} | ({"stale": True} if ev["kind"] == "stale" else {})
        return reply

    async def handle_stream(self, user_id: str, event: str, text: str | None = None, facts: dict | None = None,
                            talk_thread: int | None = None, notice: str | None = None, new_conversation: bool = False,
                            channel: str = "web", features: tuple[str, ...] = ()):
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
        new_conversation: 発言でも新しい会話を始める(LINE の発言で、最後のやりとりから間が空いたとき)。
        channel・features: 今の窓口(agent.CHANNELS)と、LINE のトークから LIFF 画面で使えるもの。毎ターン会話の state に書き、
        エージェントのシステムプロンプトの「窓口」になる(line_runtime.agent.channel_note)。
        ターン自体は裏の処理として最後まで進める。画面との接続が途中で切れて受け取りをやめても、ツールを呼んだのに
        結果が無い状態で会話が保存されることはない(そうなると次のターンが API に拒否される)。"""
        if event not in EVENTS:
            raise ValueError(f"unknown event: {event}")
        queue: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(self._turn(queue, user_id, event, text, facts, talk_thread, notice, new_conversation,
                                              channel, features))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
        await task  # ターンの中で起きた例外はここで呼び出し元に伝わる

    async def _turn(self, queue: asyncio.Queue, user_id: str, event: str, text: str | None, facts: dict | None,
                    talk_thread: int | None, notice: str | None = None, new_conversation: bool = False,
                    channel: str = "web", features: tuple[str, ...] = ()) -> None:
        try:
            entry = EVENTS[event][0]
            content = event_input(event, text, facts)
            starts = new_conversation or event in STARTS_NEW_CONVERSATION
            async with self._locks[user_id]:
                if starts:
                    await self.new_conversation(user_id)
                n = await self._current(user_id)
                if talk_thread is not None and not starts and n != talk_thread:
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
                if notice and not first and not starts:
                    payload = {"messages": [HumanMessage(notice, additional_kwargs={"profile_notice": True}), human]}
                if first:
                    payload |= {"entry": entry, "line_user_id": user_id}
                if EVENTS[event][1] == "greet":
                    # 挨拶の入口のボタンは LLM に任せず、dip の要約(やり終えた入口)と窓口から決める(task 237)。
                    # 会話の state に残し、render がこのターンの返信にだけ付ける(画面の描き直しも同じボタンになる)
                    payload |= {"greeting_entries": greeting_entries(await self._read_summary(user_id), channel, features)}
                payload |= {"channel": channel, "channel_features": list(features)}
                args: dict[str, dict] = {}
                t = time.perf_counter()
                async for chunk in self.agent.astream(payload, cfg, stream_mode="updates"):
                    now = time.perf_counter()
                    ms, t = round((now - t) * 1000), now
                    for node, update in chunk.items():
                        msgs = update.get("messages", []) if isinstance(update, dict) else []
                        if node == "model":
                            for m in msgs:
                                if m.additional_kwargs.get("turn_limit"):
                                    continue  # 1ターンの呼び出しの上限で、LLM を呼ばずに返した返信(費用台帳に載せない)
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


def history(messages: list, user_id: str, greeting: dict | None = None) -> list[dict]:
    """会話を、画面に描き直せる形にする(再読み込みで同じ会話を続けるとき)。1ターン1件:
    user(ユーザーの発言。挨拶・プッシュ・アップロードのイベントなら None)と bot(そのターンの返信。render と同じ形、
    返信が保存されていなければ None)。イベントで始まる会話でも、イベントは最初の1件だけ。
    アップロードのターンには upload(file: 「履歴書(PDF)」のような表示名、shown: 読み取り結果の確認の吹き出し、
    view_url: そのアップロードの読み取り結果の画面)も付く。音声相談の結果のターン(add_voice_result)は、保存した返信を
    そのまま bot にする。カードは render と同じく、そのターンまでに dip のツールが返した求人だけ。
    本人のデータの画面の URL(読み取り結果・下書き)は会話の記録から取らず、ここで組み立てる(ユーザーIDを載せない形。
    ユーザーID入りの URL を保存していた古い記録も、返すときには載せない)。
    プロフィールを変えた知らせ(profile_notice)は AI への入力なので、画面には出さない(前のターンの区切りにだけ使う)。
    発言に伏せた所(REDACTED)があれば redacted を付ける(画面が「電話番号などは送らないようにしました」と出す)。
    greeting は会話の state の greeting_entries(挨拶で始まった会話の、コードが付けた入口のボタン)。挨拶の返信にだけ付く(render)。"""
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
        arcade = messages[start].additional_kwargs.get("arcade")
        if upload or voice or arcade:
            user = None
        if upload:
            rid = upload.get("upload_id")
            upload = {k2: v for k2, v in upload.items() if k2 not in ("view_url", "upload_id")} | {
                "view_url": liff_url("/liff/resume", user_id, rid=rid) if rid is not None and not upload.get("empty")
                else None}
        bot = None
        if voice:
            bot = voice_history_reply(user_id, voice)  # URL は今の形で組み直し、書類の呼び方は下書きにそろえる
        elif arcade:
            bot = arcade_history_reply(user_id, arcade)
        elif reply is not None:
            bot = {k2: v for k2, v in render({"messages": messages[:end], "structured_response": reply,
                                              "greeting_entries": greeting}).items() if k2 != "tools"}
        redacted = isinstance(user, str) and REDACTED in user
        turns.append({"user": user, "bot": bot} | ({"upload": upload} if upload else {})
                     | ({"redacted": True} if redacted else {}))
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
    # ボタンは dip の画面(LIFF)の URL だけを通す。モデルが作った URL は表示しない。
    buttons = [b.model_dump() for b in reply.buttons if b.url.startswith("/liff/")]
    actions = list(dict.fromkeys(reply.actions))
    greeting = state.get("greeting_entries")
    gave_up = msgs[-1].type == "ai" and msgs[-1].additional_kwargs.get("turn_limit")  # 呼び出しの上限で返した謝り
    if greeting is not None and not gave_up and last_human == min(i for i, m in enumerate(msgs) if m.type == "human"):
        # 挨拶のターン(挨拶で始まった会話の最初の返信。task 237): 入口のボタンはコードが決めたものにする。モデルが付けた
        # 入口(音声相談・履歴書・ゲーム)は使わない(やり終えた入口や、モデルが落とした入口を、そのまま出さない)。
        # 呼び出しの上限で返した謝り(agent.TURN_LIMIT_REPLY)には付けない(謝りの問いがボタンの見出しにならないように)
        actions = list(greeting["actions"])
        buttons = list(greeting["buttons"]) + [b for b in buttons if not is_arcade_entry(b["url"])]
    return {
        "entry": state.get("entry"),
        "track_switch": track_switch(turn_tools),
        "bubbles": reply.bubbles,
        "quick_replies": reply.quick_replies,
        "cards": [jobs[j] for j in reply.job_ids if j in jobs],
        "unknown_job_ids": [j for j in reply.job_ids if j not in jobs],
        "buttons": buttons,
        "actions": actions,  # 画面が「ファイルを選ぶ」「話して伝える」のボタンにする
        "tools": turn_tools,
    }
