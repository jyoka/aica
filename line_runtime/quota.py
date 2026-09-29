"""会話数の上限: 1ユーザー・1日あたりのターン数(LLM を呼ぶ1往復)を数え、上限を超えたら LLM を呼ばずに打ち切る。

API 費用の暴走を防ぐ防波堤。数える単位は「ターン」(ユーザーの発言・挨拶・プッシュで LLM を呼ぶ1回)。
入口ごとの「会話」(visit / revisit / プッシュで始まるスレッド)で数えると、1つの会話の中で発言を続ければ
いくらでも LLM を呼べてしまうので、費用に比例するターンで数える。

- ユーザーは人(ブラウザ)単位: 「匿名ID:シナリオユーザー」は匿名IDにまとめる(シナリオを替えても上限は共有)。
- 日付は日本時間で区切る。日付が変わると数え直す(翌日には回復する)。
- 数は data/quota.json に保存する(再起動しても当日の分は消えない)。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
LIMIT_ENV = "LINE_SIM_DAILY_TURN_LIMIT"
DEFAULT_LIMIT = 50  # 1人1日50ターン。普通に試す分には十分で、1人あたりの1日の費用に上限がかかる
LIMIT_MESSAGE = ("たくさんご利用いただきありがとうございます。本日のご利用回数の上限に達したため、"
                 "今日はここまでとさせてください。明日になるとまたご利用いただけます。")


def daily_limit() -> int:
    """環境変数 LINE_SIM_DAILY_TURN_LIMIT(未設定なら 50)。0 以下なら上限なし。"""
    raw = os.environ.get(LIMIT_ENV, "").strip()
    return int(raw) if raw else DEFAULT_LIMIT


def person(user_id: str) -> str:
    """上限を数える単位。ブラウザのシナリオユーザー「匿名ID:シナリオユーザー」は匿名IDにまとめる。"""
    return user_id.split(":", 1)[0]


def today(now: datetime) -> str:
    return now.astimezone(JST).date().isoformat()


class DailyQuota:
    """ユーザーごとの当日のターン数。path が None ならメモリだけ。clock は差し替えられる(テストで日付を進める)。"""

    def __init__(self, limit: int, path: Path | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(JST)) -> None:
        self.limit = limit
        self.path = Path(path) if path else None
        self.clock = clock
        self._lock = threading.Lock()
        self._day, self._counts = "", {}
        if self.path and self.path.is_file():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self._day, self._counts = saved.get("day", ""), saved.get("counts", {})

    def _roll(self) -> None:
        d = today(self.clock())
        if d != self._day:  # 日付が変わったら数え直す
            self._day, self._counts = d, {}

    def used(self, user_id: str) -> int:
        with self._lock:
            self._roll()
            return self._counts.get(person(user_id), 0)

    def available(self, user_id: str) -> bool:
        return self.limit <= 0 or self.used(user_id) < self.limit

    def take(self, user_id: str) -> bool:
        """1ターン分を使う。上限に達していたら使わずに False(呼び出し側は LLM を呼ばない)。"""
        with self._lock:
            self._roll()
            key = person(user_id)
            n = self._counts.get(key, 0)
            if 0 < self.limit <= n:
                return False
            self._counts[key] = n + 1
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps({"day": self._day, "counts": self._counts}, ensure_ascii=False),
                                     encoding="utf-8")
            return True


    def refund(self, user_id: str) -> None:
        """take で使った1ターン分を返す(LLM を呼ばずに終わったとき。例: 別のタブで会話が替わっていた)。"""
        with self._lock:
            self._roll()
            key = person(user_id)
            if self._counts.get(key, 0) > 0:
                self._counts[key] -= 1
                if self.path:
                    self.path.write_text(json.dumps({"day": self._day, "counts": self._counts}, ensure_ascii=False),
                                         encoding="utf-8")


def limit_reply(entry: str | None) -> dict:
    """上限を超えたときの返信(render と同じ形。LLM は呼ばない)。"""
    return {"entry": entry, "bubbles": [LIMIT_MESSAGE], "quick_replies": [], "cards": [], "unknown_job_ids": [],
            "buttons": [], "actions": [], "tools": [], "limited": True}
