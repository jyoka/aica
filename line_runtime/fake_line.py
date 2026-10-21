"""偽物の LINE プラットフォームの、アカウント連携のところ(task 182)。実 LINE は呼ばない。

本物の LINE がすることを、同じ決まりで再現する:
- linkToken の発行(POST /v2/bot/user/{userId}/linkToken): ユーザーごとに出す。LINK_TOKEN_TTL(10 分)で切れ、1 回だけ使える
- accountLink の画面(https://access.line.me/dialog/bot/accountLink?linkToken=…&nonce=…): 本人が開くと、bot の webhook に
  accountLink のイベント(link.result と link.nonce)を送る。linkToken が切れている・使い終わっている・開いた人が linkToken の
  ユーザーでないときは result が failed。知らない linkToken は、だれの連携か分からないので webhook を送らない

使うところ:
- テスト: LineApi の偽物の API(api_response)と、accountLink の画面を開いたときに LINE が送る webhook(open_account_link と
  signed_webhook)
- 開発者画面: Web の LINE 風の画面の匿名ユーザーの連携(line_runtime.server の /api/dev/link と /fake-line/…)。
  Web のユーザーには LINE の webhook が届かないので、サーバーが同じ処理を直接呼ぶ
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

LINK_TOKEN_TTL = timedelta(minutes=10)
LINK_TOKEN_PATH = re.compile(r"/v2/bot/user/([^/]+)/linkToken")


class FakeLinePlatform:
    """linkToken を出して覚え、accountLink の画面を開いたときのイベントを作る。スレッドから同時に使ってよい。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._tokens: dict[str, dict] = {}  # linkToken → {"user": userId, "expires": 時刻, "used": bool}

    def issue_link_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            self._tokens = {k: v for k, v in self._tokens.items() if v["expires"] > now - LINK_TOKEN_TTL}
            self._tokens[token] = {"user": user_id, "expires": now + LINK_TOKEN_TTL, "used": False}
        return token

    def knows(self, link_token: str) -> bool:
        with self._lock:
            return link_token in self._tokens

    def open_account_link(self, link_token: str, nonce: str, opener: str) -> dict | None:
        """opener(LINE にログインしている人)が accountLink の画面を開いた: LINE が bot に送る accountLink のイベント。
        知らない linkToken なら None。linkToken はここで使い終わりになる(失敗でも)。"""
        with self._lock:
            t = self._tokens.get(link_token)
            if t is None:
                return None
            ok = not t["used"] and t["expires"] > self.clock() and opener == t["user"]
            t["used"] = True
        return account_link_event(t["user"], nonce, ok)

    def api_response(self, request: httpx.Request) -> httpx.Response | None:
        """偽物の Messaging API のうち、linkToken の発行に答える(ほかのパスは None。呼び出し元が答える)。"""
        m = LINK_TOKEN_PATH.fullmatch(request.url.path)
        if request.method != "POST" or m is None:
            return None
        return httpx.Response(200, json={"linkToken": self.issue_link_token(m[1])})


def account_link_event(user_id: str, nonce: str, ok: bool) -> dict:
    """accountLink の webhook のイベント(Messaging API の形)。"""
    return {"type": "accountLink", "mode": "active", "timestamp": int(time.time() * 1000),
            "webhookEventId": secrets.token_hex(13).upper(), "deliveryContext": {"isRedelivery": False},
            "replyToken": secrets.token_hex(16), "source": {"type": "user", "userId": user_id},
            "link": {"result": "ok" if ok else "failed", "nonce": nonce}}


def signed_webhook(secret: str, events: list[dict], destination: str = "Ubot") -> tuple[bytes, dict]:
    """LINE が bot に送る webhook の本文と、署名(X-Line-Signature)つきのヘッダー。"""
    body = json.dumps({"destination": destination, "events": events}, ensure_ascii=False).encode()
    signature = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return body, {"content-type": "application/json", "x-line-signature": signature}
