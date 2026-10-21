"""LINE 公式アカウント(Messaging API)の口: Web のトーク画面に並ぶ 2 つ目のチャネル(task 177)。

POST /line/webhook(line_runtime.server)が使う部品。FastAPI には依存しない。
- 署名: X-Line-Signature は、チャネルシークレットで本文そのままを HMAC-SHA256 にかけた値の base64(verify_signature)。
- ユーザー: LINE の userId から aica のユーザーID「l-<ハッシュ16桁>」を作る(line_user)。匿名ユーザーID(w-…)とも
  シナリオのユーザー(fixtures の名前、「匿名ID:シナリオユーザー」)とも重ならない。userId そのものはどこにも保存しない
  (返信と push の宛先にだけ、その webhook を処理する間メモリで使う)。
- 再送: 同じ webhookEventId には 2 回答えない(SeenEvents。ID は data/line_events.json に置き、再起動しても覚えている)。
- 返信: runtime.render の結果を LINE のメッセージにする(to_messages)。1 回 5 メッセージまで。
- 送信: LineApi(入力中の表示・返信・push)。HTTP は httpx(すでに入っている依存。LINE の SDK は足さない)。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Self

import httpx

log = logging.getLogger(__name__)

SECRET_ENV = "LINE_CHANNEL_SECRET"
TOKEN_ENV = "LINE_CHANNEL_ACCESS_TOKEN"
PUBLIC_URL_ENV = "AICA_PUBLIC_URL"

API_BASE = "https://api.line.me"
MAX_MESSAGES = 5  # 1 回の返信・push で送れるメッセージの数(Messaging API の上限)
LOADING_SECONDS = 60  # 入力中の表示の長さ(5〜60 秒。返信を送ると消える)
LINE_ID = re.compile(r"l-[0-9a-f]{16}")
OK, REJECTED, UNREACHABLE, UNKNOWN = "ok", "rejected", "unreachable", "unknown"  # LineApi の呼び出しの結果


def channel_secret() -> str:
    return os.environ.get(SECRET_ENV, "").strip()


def access_token() -> str:
    return os.environ.get(TOKEN_ENV, "").strip()


def public_url() -> str:
    """LINE から開くリンク(LIFF 画面のボタン)の前に付ける公開 URL(例 https://xxxx.trycloudflare.com)。無ければ空。"""
    return os.environ.get(PUBLIC_URL_ENV, "").strip().rstrip("/")


def configured() -> bool:
    """シークレットとトークンの両方があるときだけ LINE の口を開く(無ければ /line/webhook は 404)。"""
    return bool(channel_secret() and access_token())


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """X-Line-Signature が、本文そのままの HMAC-SHA256(鍵はチャネルシークレット)の base64 と合うか。"""
    if not secret or not signature:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest())
    return hmac.compare_digest(expected, signature.encode())


def line_user(user_id: str) -> str:
    """LINE の userId から aica のユーザーID「l-<16桁hex>」を作る(同じ userId なら毎回同じ)。
    userId は推測できない 128 ビットの乱数なので、ハッシュから元の userId には戻せない。鍵は使わない
    (チャネルシークレットを作り直しても、同じ人が同じユーザーのまま)。"""
    return "l-" + hashlib.sha256(f"aica-line:{user_id}".encode()).hexdigest()[:16]


class SeenEvents:
    """受けた webhookEventId(再送で同じイベントに 2 回答えないため)。新しい順に KEEP 件だけ覚える。
    path が None ならメモリだけ。ID は LINE がイベントごとに付ける乱数で、ユーザーは分からない。"""

    KEEP = 2000

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._ids: list[str] = []
        if path and path.is_file():
            try:
                self._ids = [str(x) for x in json.loads(path.read_text(encoding="utf-8"))]
            except (ValueError, TypeError):  # 壊れていても Web サーバーは起動する(覚えていた ID だけを捨てる)
                log.warning("%s が読めないので、受けた webhook のイベントを忘れて始めます", path)
        self._set = set(self._ids)

    def first_time(self, event_id: str) -> bool:
        """初めて受けた ID なら覚えて True。受けたことがあれば False(処理の前に呼ぶので、処理中の再送も止まる)。"""
        with self._lock:
            if event_id in self._set:
                return False
            self._ids = [*self._ids, event_id][-self.KEEP:]
            self._set = set(self._ids)
            if self.path:  # 一時ファイルから置き換える(書く途中で落ちても、前の中身が残る)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(self._ids), encoding="utf-8")
                os.replace(tmp, self.path)
            return True


# ----------------------------- 返信を LINE のメッセージにする -----------------------------

# actions(Web の画面の「ファイルを選ぶ」「話して伝える」)は、LINE ではまだ使えないので文で案内する
ACTION_NOTES = {
    "resume_upload": "履歴書・職務経歴書のファイルは、LINE ではまだ受け取れません。経歴や希望は、このトークに文で書いて教えてください。",
    "voice_consult": "音声での相談は、LINE ではまだ使えません。このトークに文で書いて教えてください。",
}
FAILED_TEXT = "すみません、うまく返信できませんでした。少し時間をおいて、もう一度送ってください。"


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _text(s: str) -> dict:
    return {"type": "text", "text": _clip(s, 5000)}


def _card_bubble(c: dict) -> dict:
    """求人カード(dip_mcp.logic.job_card の形)を Flex の bubble に。ボタンは Web のカードと同じ文を本人の発言として送る。"""
    fulltime = c.get("employment_type") == "正社員"
    meta = " · ".join(x for x in ("正社員" if fulltime else "", c.get("job_type") or "", c.get("area") or "") if x)
    pay = c.get("pay_text") or (f"時給{c['hourly_wage']}円" if c.get("hourly_wage") else "")  # v1 の記録は hourly_wage
    station = f"{str(c.get('station') or '').removesuffix('駅')}駅 徒歩{c.get('walk_min')}分" if c.get("station") else ""
    tags = [c.get("shift") or "", f"週{c['days_min']}日〜" if c.get("days_min") else "", station]
    reasons = " / ".join(c.get("reasons") or [])
    body = [
        {"type": "text", "text": meta, "size": "xs", "color": "#888888"} if meta else None,
        {"type": "text", "text": c["title"], "weight": "bold", "size": "md", "wrap": True},
        {"type": "text", "text": c["employer"], "size": "sm", "color": "#555555", "wrap": True} if c.get("employer") else None,
        {"type": "text", "text": pay, "weight": "bold", "color": "#06c755", "wrap": True} if pay else None,
        {"type": "text", "text": " / ".join(t for t in tags if t), "size": "xs", "wrap": True} if any(tags) else None,
        {"type": "text", "text": f"おすすめ: {reasons}", "size": "xs", "color": "#555555", "wrap": True} if reasons else None,
    ]

    def button(label: str, text: str, style: str) -> dict:
        return {"type": "button", "style": style, "height": "sm", "color": "#06c755" if style == "primary" else None,
                "action": {"type": "message", "label": label, "text": _clip(text, 300)}}

    footer = [button("詳しく見る", f"「{c['title']}」について詳しく教えて", "primary"),
              button("保存", f"「{c['title']}」({c['job_id']})を保存して", "secondary")]
    return {"type": "bubble", "size": "kilo",
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [b for b in body if b]},
            "footer": {"type": "box", "layout": "vertical", "spacing": "sm",
                       "contents": [{k: v for k, v in b.items() if v is not None} for b in footer]}}


def _cards_message(cards: list[dict]) -> dict:
    alt = "求人のご案内: " + "、".join(c["title"] for c in cards)
    return {"type": "flex", "altText": _clip(alt, 400),
            "contents": {"type": "carousel", "contents": [_card_bubble(c) for c in cards[:12]]}}


def _buttons_message(buttons: list[dict], base: str) -> dict:
    """LIFF 画面のボタン(/liff/...)を、公開 URL へのリンクのボタンにする。"""
    return {"type": "flex", "altText": _clip("リンク: " + "、".join(b["label"] for b in buttons), 400),
            "contents": {"type": "bubble", "size": "kilo", "body": {
                "type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                    {"type": "button", "style": "link", "height": "sm",
                     "action": {"type": "uri", "label": _clip(b["label"], 20), "uri": base + b["url"]}}
                    for b in buttons]}}}


def _quick_reply(labels: list[str]) -> dict:
    return {"items": [{"type": "action", "action": {"type": "message", "label": _clip(q, 20), "text": _clip(q, 300)}}
                      for q in labels[:13]]}


def _labeled(items: list[str]) -> list[str]:
    """空・空白だけの文言を除く(LINE は空のラベルや本文を断り、そのメッセージ全体が届かなくなる)。"""
    return [x.strip() for x in items if isinstance(x, str) and x.strip()]


def to_messages(reply: dict, base_url: str | None = None) -> list[dict]:
    """render の結果(上限の案内 limit_reply も同じ形)を、LINE に送るメッセージ(5 つまで)にする。
    - bubbles → テキスト。actions はその後ろに案内の文として足す
    - cards → Flex の carousel(render が dip のツールの返した求人だけにしている)
    - buttons(/liff/…)→ 公開 URL(base_url。既定は AICA_PUBLIC_URL)へのリンク。公開 URL が無ければ出さない
    - quick_replies → 最後のメッセージのクイックリプライ
    5 つを超えるときは、テキストを後ろから 1 つにまとめる(求人とリンクは残す)。"""
    base = public_url() if base_url is None else base_url.strip().rstrip("/")
    texts = _labeled(reply.get("bubbles") or [])
    texts += [ACTION_NOTES[a] for a in reply.get("actions") or [] if a in ACTION_NOTES]
    flex = []
    if reply.get("cards"):
        flex.append(_cards_message(reply["cards"]))
    buttons = [{"label": b["label"].strip(), "url": b["url"]} for b in reply.get("buttons") or []
               if str(b.get("url", "")).startswith("/liff/") and _labeled([b.get("label")])]
    if buttons and base.startswith(("https://", "http://")):
        flex.append(_buttons_message(buttons, base))
    elif buttons:
        log.warning("%s が無い(か http(s):// で始まらない)ので、LIFF 画面のボタン %d 個を LINE に出しません",
                    PUBLIC_URL_ENV, len(buttons))
    room = MAX_MESSAGES - len(flex)
    if len(texts) > room:
        texts = [*texts[: room - 1], "\n\n".join(texts[room - 1:])]
    messages = [_text(t) for t in texts] + flex
    if not messages:  # 送るものが無い(吹き出しが空): 入力中の表示のまま黙らない
        messages = [_text(FAILED_TEXT)]
    if quick := _labeled(reply.get("quick_replies") or []):
        messages[-1]["quickReply"] = _quick_reply(quick)
    return messages


# ----------------------------- LINE の API -----------------------------


class LineApi:
    """Messaging API の呼び出し(入力中の表示・返信・push)。transport はテストで偽物の LINE API に差し替える。"""

    def __init__(self, token: str, base_url: str = API_BASE, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=10.0,
                                         headers={"Authorization": f"Bearer {token}"})

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict) -> str:
        """結果: OK(届いた)/ REJECTED(LINE が断った。届いていない)/ UNREACHABLE(つながらなかった。届いていない)/
        UNKNOWN(送った後に応答が来なかった。届いたかもしれない)。本文やユーザーIDはログに出さない。"""
        try:
            r = await self._client.post(path, json=body)
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            log.warning("LINE API %s につながりませんでした: %s", path, type(e).__name__)
            return UNREACHABLE
        except httpx.HTTPError as e:
            log.warning("LINE API %s の応答がありませんでした: %s", path, type(e).__name__)
            return UNKNOWN
        if r.is_success:
            return OK
        log.warning("LINE API %s が %d を返しました", path, r.status_code)
        return REJECTED

    async def loading(self, user_id: str, seconds: int = LOADING_SECONDS) -> str:
        """トークに「入力中」の表示を出す(1 対 1 のトークだけ。返信を送ると消える)。"""
        return await self._post("/v2/bot/chat/loading/start", {"chatId": user_id, "loadingSeconds": seconds})

    async def reply(self, reply_token: str, messages: list[dict]) -> str:
        return await self._post("/v2/bot/message/reply", {"replyToken": reply_token, "messages": messages})

    async def push(self, user_id: str, messages: list[dict]) -> str:
        return await self._post("/v2/bot/message/push", {"to": user_id, "messages": messages})

    async def send(self, reply_token: str | None, user_id: str, messages: list[dict]) -> str | None:
        """返信トークで返し、届いていないと分かったとき(期限切れで断られた・つながらなかった)だけ push で返す
        (応答が来なかったときは、届いたかもしれないので送り直さない。2 回届かないように)。
        LINE がメッセージの形を断ったら、謝りの文だけを push する(入力中の表示のまま黙らない)。
        どれで届いたか("reply" / "push" / "fallback")、届かなかった・分からなければ None。"""
        if reply_token:
            r = await self.reply(reply_token, messages)
            if r == OK:
                return "reply"
            if r == UNKNOWN:
                return None
        p = await self.push(user_id, messages)
        if p == OK:
            return "push"
        fallback = [_text(FAILED_TEXT)]
        if p == REJECTED and messages != fallback and await self.push(user_id, fallback) == OK:
            return "fallback"
        return None
