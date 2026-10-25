"""ログに値を出さない決まり(docs/design/privacy.md の 3.4、LLD の 8 章)。Web プロセスと音声ワーカーが共用する。

ログに出してよいのは、ユーザーID、イベントや処理の種類、件数、値があるかないか、例外の型名だけ。
本人の発言、希望、文字起こし、ファイル名、例外の文は出さない(API のエラーの文に、送った本文が入ることがある)。
SDK は INFO や DEBUG で送った本文や応答を出しうるので、本番では SDK のロガーを WARNING にする。
"""

from __future__ import annotations

import logging

# 本番で WARNING にする SDK のロガー(下のロガーも含む)。livekit は livekit-agents の本体とプラグイン。
# aiosqlite(会話の保存)は DEBUG で、読み書きした会話の中身を出す
SDK_LOGGERS = ("livekit", "openai", "anthropic", "httpx", "httpx2", "httpcore", "httpcore2", "aiosqlite",
               "langchain", "langgraph", "langsmith", "mcp")


def _is_sdk_logger(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in SDK_LOGGERS)


def quiet_sdk_loggers() -> None:
    """SDK のロガーと、もうある下のロガーを WARNING にする。

    下のロガーにもレベルを付ける: livekit-agents の CLI は、レベルの無い(NOTSET の)自分のロガーとプラグインのロガーを
    起動のレベル(INFO)にするので、親だけ WARNING にしても効かない。"""
    names = [n for n in logging.Logger.manager.loggerDict if _is_sdk_logger(n)]
    for name in (*SDK_LOGGERS, *names):
        logging.getLogger(name).setLevel(logging.WARNING)
