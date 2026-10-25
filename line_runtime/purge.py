"""期間で消す掃除(docs/design/privacy.md の 3.3)。いつも Web プロセスの中で走る(起動時と 1 日 1 回。line_runtime.server)。

消すもの(数は 3.3 の推奨。使われ方を見て変えてよい):
- 会話(data/line_checkpoints.db): 会話(thread_id)ごとに、最後のやりとりから CONVERSATION_TTL(30 日)
- 使われなくなった人(data/state.json の行と連携の表): 最後の利用から 180 日。会話、dip の状態、音声のファイル、
  Web プロセスのメモリ(未読のアップロード、音声の待ち)の順に片付ける(「すべて消す」の server.profile_forget と同じ順)。
  「すべて消す」と違い、持ち越す値も残さない(dip_mcp.retention)
- 「すべて消す」で持ち越した部屋の名前: 消した時刻から 1 日
- 費用台帳(data/costs.jsonl)のユーザーID: 90 日を過ぎた行を日ごとの合計にする(line_runtime.ledger.fold_old)
- 最後に SQLite(会話と連携の表)を VACUUM する

ターンと交差しないよう、会話とユーザーは、そのユーザーの user_lock の中で消す。user_lock・会話の番号のキャッシュ・
未読のアップロードは Web プロセスのメモリにあるので、別のプロセスからは消さない。手で走らせる CLI は、
Web プロセスの内部の口(POST /api/internal/purge。AICA_INTERNAL_KEY で守る)を呼ぶだけ:

    ./.venv/bin/python -m line_runtime.purge                       # http://127.0.0.1:8030 の Web プロセスで掃除する
    ./.venv/bin/python -m line_runtime.purge --url https://…       # 公開した Web プロセスで

ログには件数だけを出す(ユーザーIDも値も出さない。3.4)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack
from datetime import timedelta

from dip_mcp import accounts, retention
from dip_mcp.data import State
from line_runtime.runtime import LineRuntime

CONVERSATION_TTL = timedelta(days=30)  # 会話を消すまで(最後のやりとりから)
EVERY = timedelta(days=1)  # 定期の掃除の間隔
INTERNAL_PATH = "/api/internal/purge"
DEFAULT_URL = "http://127.0.0.1:8030"
# uvicorn が INFO を出すロガーの子にする(ほかのロガーは、uvicorn の下では WARNING より下を出さない)
log = logging.getLogger("uvicorn.error").getChild("purge")


async def purge(rt: LineRuntime, state: State, *, scenario_users: Iterable[str], unread_uploads: dict,
                voice_ready: dict, remove_files: Callable[[list[str]], None]) -> dict:
    """1 回の掃除。消した数を返す。
    scenario_users: /dev のシナリオユーザーの名前(人と一緒に、その人の「匿名ID:シナリオユーザー」の会話も消す)。
    unread_uploads・voice_ready: Web プロセスのメモリ(消した人の分を外す)。remove_files: 音声の下書きのファイルを消す。
    ファイルの読み書き(state.json、費用台帳、連携の表)はスレッドで行う(その間もほかの要求とヘルスチェックに答える)。"""
    now = retention.clock()
    cutoff = now - CONVERSATION_TTL
    conversations = 0
    for thread in await rt.old_threads(cutoff):
        async with rt.user_lock(thread.rpartition(":")[0]):
            conversations += await rt.erase_old_thread(thread, cutoff)

    swept = await asyncio.to_thread(retention.sweep, state)
    removed: dict = {"people": [], "users": [], "files": []}
    if swept["expired"]:
        targets = sorted({uid for who, uids in swept["expired"].items()
                          for uid in (who, *uids, *(f"{who}:{s}" for s in scenario_users))})
        async with AsyncExitStack() as locks:  # 「すべて消す」と同じく、名前の順に取り、進行中のターンが終わってから消す
            for uid in targets:
                await locks.enter_async_context(rt.user_lock(uid))
            # 対象を決めた後に使われた人(ロックを待つ間のターンなど)は外す
            people = await asyncio.to_thread(retention.still_expired, state, list(swept["expired"]))
            doomed = [uid for uid in targets if retention.person(uid) in people]
            for uid in doomed:  # 会話を消してから dip の状態を消す(server.profile_forget と同じ順)
                await rt.forget(uid)
            removed = await asyncio.to_thread(retention.remove_expired, state, people)
        for uid in doomed:
            unread_uploads.pop(uid, None)
            voice_ready.pop(uid, None)
        remove_files(removed["files"])

    ledger_ids = await asyncio.to_thread(rt.ledger.fold_old, now)
    await rt.vacuum()
    await asyncio.to_thread(accounts.of(state).vacuum)
    counts = {"conversations": conversations, "users": len(removed["people"]), "forgotten_rooms": swept["rooms_cleared"],
              "ledger_rows": ledger_ids}
    log.info("期間で消す掃除: 会話 %d 件、ユーザー %d 人、持ち越した部屋の名前 %d 人分、費用台帳のユーザーID %d 行を消した",
             *counts.values())
    return counts


def main(argv: list[str] | None = None) -> int:
    """手で掃除を走らせる: Web プロセスの内部の口を呼ぶだけ(このプロセスでは何も消さない)。"""
    import httpx

    from line_runtime import config

    ap = argparse.ArgumentParser(description="期間を過ぎた会話・ユーザー・費用台帳のユーザーIDを、Web プロセスに消させる")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"Web プロセスの URL(既定 {DEFAULT_URL})")
    args = ap.parse_args(argv)
    config.load_env()
    key = os.environ.get("AICA_INTERNAL_KEY", "").strip()  # 前後の空白は除く(Web プロセスと同じ)
    if not key:
        print("AICA_INTERNAL_KEY がありません(Web プロセスと同じ値を .env か環境変数に)", file=sys.stderr)
        return 2
    r = httpx.post(args.url.rstrip("/") + INTERNAL_PATH, headers={"Authorization": f"Bearer {key}"}, timeout=600)
    if r.status_code != 200:
        print(f"掃除できませんでした: HTTP {r.status_code}", file=sys.stderr)
        return 1
    print(json.dumps(r.json(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
