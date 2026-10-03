"""会話数の上限: 1日あたりのターン数(LLM を呼ぶ1往復)を数え、上限を超えたら LLM を呼ばずに打ち切る。

API 費用の暴走を防ぐ防波堤。数える単位は「ターン」(ユーザーの発言・挨拶・プッシュで LLM を呼ぶ1回)。
入口ごとの「会話」(visit / revisit / プッシュで始まるスレッド)で数えると、1つの会話の中で発言を続ければ
いくらでも LLM を呼べてしまうので、費用に比例するターンで数える。

上限は3つあり、この順で確かめる。どれかで止めたら、どれも数えない(LLM も呼ばない)。
1. 匿名IDごと(LINE_SIM_DAILY_TURN_LIMIT、既定 50): 1人あたりの1日の費用に上限をかける。
2. IP ごと: ターンの合計(AICA_DAILY_TURN_LIMIT_PER_IP、既定 150)と、その日に使われた匿名IDの数
   (AICA_DAILY_NEW_IDS_PER_IP、既定 20)。Cookie を作り直して匿名IDの上限を迂回する道を閉じ、
   状態ファイルにユーザーが増え続けないようにする。ターンを使わずに状態へ書く API も admit で匿名IDの数を確かめる。
3. 全体(AICA_DAILY_TURN_LIMIT_TOTAL、既定 1000): その日の全ユーザーのターンの合計。1日の費用の天井。

- ユーザーは人(ブラウザ)単位: 「匿名ID:シナリオユーザー」は匿名IDにまとめる(シナリオを替えても上限は共有)。
- 日付は日本時間で区切る。日付が変わると数え直す(翌日には回復する)。
- 数は data/quota.json に保存する(再起動しても当日の分は消えない)。IP も当日の分だけ置き、日付が変わると消える。
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
IP_LIMIT_ENV = "AICA_DAILY_TURN_LIMIT_PER_IP"
DEFAULT_IP_LIMIT = 150  # 1つの IP から1日150ターン。家族や同じ回線の数人(1人50ターン × 3人)分
IP_IDS_ENV = "AICA_DAILY_NEW_IDS_PER_IP"
DEFAULT_IP_IDS = 20  # 1つの IP から1日に使える匿名IDは20まで。同じ回線の数人がブラウザを替えても足りる
TOTAL_LIMIT_ENV = "AICA_DAILY_TURN_LIMIT_TOTAL"
DEFAULT_TOTAL_LIMIT = 1000  # 全体で1日1000ターン(本番モデルで1ターン約 $0.05 なので1日約 $50 が天井)
LIMIT_MESSAGE = ("たくさんご利用いただきありがとうございます。本日のご利用回数の上限に達したため、"
                 "今日はここまでとさせてください。明日になるとまたご利用いただけます。")
IP_LIMIT_MESSAGE = ("たくさんご利用いただきありがとうございます。この回線からの本日のご利用回数の上限に達したため、"
                    "今日はここまでとさせてください。明日になるとまたご利用いただけます。")
TOTAL_LIMIT_MESSAGE = ("本日はたくさんのご利用があり、サービス全体のご利用回数の上限に達したため、"
                       "今日はここまでとさせてください。明日になるとまたご利用いただけます。")


def _env_limit(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def daily_limit() -> int:
    """環境変数 LINE_SIM_DAILY_TURN_LIMIT(未設定なら 50)。0 以下なら上限なし。"""
    return _env_limit(LIMIT_ENV, DEFAULT_LIMIT)


def ip_limit() -> int:
    """環境変数 AICA_DAILY_TURN_LIMIT_PER_IP(未設定なら 150)。0 以下なら上限なし。"""
    return _env_limit(IP_LIMIT_ENV, DEFAULT_IP_LIMIT)


def ip_ids_limit() -> int:
    """環境変数 AICA_DAILY_NEW_IDS_PER_IP(未設定なら 20)。0 以下なら上限なし。"""
    return _env_limit(IP_IDS_ENV, DEFAULT_IP_IDS)


def total_limit() -> int:
    """環境変数 AICA_DAILY_TURN_LIMIT_TOTAL(未設定なら 1000)。0 以下なら上限なし。"""
    return _env_limit(TOTAL_LIMIT_ENV, DEFAULT_TOTAL_LIMIT)


def person(user_id: str) -> str:
    """上限を数える単位。ブラウザのシナリオユーザー「匿名ID:シナリオユーザー」は匿名IDにまとめる。"""
    return user_id.split(":", 1)[0]


def today(now: datetime) -> str:
    return now.astimezone(JST).date().isoformat()


def _over(limit: int, n: int) -> bool:
    return 0 < limit <= n


class DailyQuota:
    """当日のターン数(人ごと・IP ごと・全体)。path が None ならメモリだけ。clock は差し替えられる(テストで日付を進める)。
    IP ごと・全体の上限は既定で無し(0)。サーバーは環境変数の値を渡す。ip を渡さない呼び出しは IP の上限を見ない。"""

    def __init__(self, limit: int, path: Path | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(JST), *,
                 ip_limit: int = 0, ip_ids: int = 0, total_limit: int = 0) -> None:
        self.limit, self.ip_limit, self.ip_ids, self.total_limit = limit, ip_limit, ip_ids, total_limit
        self.path = Path(path) if path else None
        self.clock = clock
        self._lock = threading.Lock()
        # counts: 人 → ターン数。ips: IP → {人 → その IP からのターン数}(キーの数がその IP から使われた匿名IDの数)
        self._day, self._counts, self._ips = "", {}, {}
        if self.path and self.path.is_file():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self._day, self._counts, self._ips = saved.get("day", ""), saved.get("counts", {}), saved.get("ips", {})

    def _roll(self) -> None:
        d = today(self.clock())
        if d != self._day:  # 日付が変わったら数え直す
            self._day, self._counts, self._ips = d, {}, {}

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"day": self._day, "counts": self._counts, "ips": self._ips},
                                            ensure_ascii=False), encoding="utf-8")

    def _blocked(self, key: str, ip: str | None) -> str | None:
        """上限に達していれば案内の文(匿名ID → IP → 全体の順に確かめる)。達していなければ None。ロックの中で呼ぶ。"""
        if _over(self.limit, self._counts.get(key, 0)):
            return LIMIT_MESSAGE
        if ip is not None:
            ids = self._ips.get(ip, {})
            if key not in ids and _over(self.ip_ids, len(ids)):  # この IP から初めて使う匿名IDは、数に空きがあるときだけ
                return IP_LIMIT_MESSAGE
            if _over(self.ip_limit, sum(ids.values())):
                return IP_LIMIT_MESSAGE
        if _over(self.total_limit, sum(self._counts.values())):
            return TOTAL_LIMIT_MESSAGE
        return None

    def used(self, user_id: str) -> int:
        with self._lock:
            self._roll()
            return self._counts.get(person(user_id), 0)

    def ip_used(self, ip: str) -> int:
        with self._lock:
            self._roll()
            return sum(self._ips.get(ip, {}).values())

    def total_used(self) -> int:
        with self._lock:
            self._roll()
            return sum(self._counts.values())

    def blocked(self, user_id: str, ip: str | None = None) -> str | None:
        """今1ターン使えなければ、その上限の案内の文。使えるなら None(使いはしない)。"""
        with self._lock:
            self._roll()
            return self._blocked(person(user_id), ip)

    def admit(self, user_id: str, ip: str) -> str | None:
        """ターンを使わずに状態へ書く API(シナリオの開始・プロフィールの書き換え)の前に呼ぶ。この IP から初めて使う
        匿名IDなら、数に空きがあるときだけ使われた匿名IDとして0ターンで加える。空きが無ければ IP の上限の案内の文
        (呼び出し側は状態に書かない)。ターンの上限は見ない(数えもしない)。"""
        with self._lock:
            self._roll()
            key = person(user_id)
            ids = self._ips.get(ip, {})
            if key in ids:
                return None
            if _over(self.ip_ids, len(ids)):
                return IP_LIMIT_MESSAGE
            self._ips.setdefault(ip, {})[key] = 0
            self._save()
            return None

    def available(self, user_id: str, ip: str | None = None) -> bool:
        return self.blocked(user_id, ip) is None

    def try_take(self, user_id: str, ip: str | None = None) -> str | None:
        """1ターン分を使う(人・IP・全体のすべてに数える)。どれかの上限に達していたら、どれにも数えずに
        その上限の案内の文を返す(呼び出し側は LLM を呼ばない)。使えたら None。"""
        with self._lock:
            self._roll()
            key = person(user_id)
            msg = self._blocked(key, ip)
            if msg is not None:
                return msg
            self._counts[key] = self._counts.get(key, 0) + 1
            if ip is not None:
                ids = self._ips.setdefault(ip, {})
                ids[key] = ids.get(key, 0) + 1
            self._save()
            return None

    def take(self, user_id: str, ip: str | None = None) -> bool:
        """1ターン分を使う。上限に達していたら使わずに False(呼び出し側は LLM を呼ばない)。"""
        return self.try_take(user_id, ip) is None

    def refund(self, user_id: str, ip: str | None = None) -> None:
        """take で使った1ターン分を返す(LLM を呼ばずに終わったとき。例: 別のタブで会話が替わっていた)。
        take と同じ ip を渡す。その IP から使われた匿名IDとしては残す(状態に書かれたかもしれないので)。"""
        with self._lock:
            self._roll()
            key = person(user_id)
            if self._counts.get(key, 0) > 0:
                self._counts[key] -= 1
                ids = self._ips.get(ip, {}) if ip is not None else {}
                if ids.get(key, 0) > 0:
                    ids[key] -= 1
                self._save()


def limit_reply(entry: str | None, message: str = LIMIT_MESSAGE) -> dict:
    """上限を超えたときの返信(render と同じ形。LLM は呼ばない)。message はどの上限か(try_take が返す文)。"""
    return {"entry": entry, "bubbles": [message], "quick_replies": [], "cards": [], "unknown_job_ids": [],
            "buttons": [], "actions": [], "tools": [], "limited": True}
