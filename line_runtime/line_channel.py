"""LINE 公式アカウント(Messaging API)の口: Web のトーク画面に並ぶ 2 つ目のチャネル(task 177)。

POST /line/webhook(line_runtime.server)が使う部品。FastAPI には依存しない。
- 署名: X-Line-Signature は、チャネルシークレットで本文そのままを HMAC-SHA256 にかけた値の base64(verify_signature)。
- ユーザー: LINE の userId から aica のユーザーID「l-<ハッシュ16桁>」を作る(line_user。鍵 AICA_ID_KEY つきの HMAC-SHA256、
  task 183・ADR-0008)。匿名ユーザーID(w-…)とも
  シナリオのユーザー(fixtures の名前、「匿名ID:シナリオユーザー」)とも重ならない。userId そのものはどこにも保存しない
  (返信と push の宛先にだけ、その webhook を処理する間メモリで使う)。LINE の設定があるのに鍵が無い・短いときは
  起動で止める(check_id_key)。
- 再送: 同じ webhookEventId には 2 回答えない(SeenEvents。ID は data/line_events.json に置き、再起動しても覚えている)。
- 返信: runtime.render の結果を LINE のメッセージにする(to_messages)。1 回 5 メッセージまで。
- 送信: LineApi(入力中の表示・返信・push・linkToken の発行)。HTTP は httpx(すでに入っている依存。LINE の SDK は足さない)。
- アカウント連携(task 182): 決まった文(LINK_TEXT / UNLINK_TEXT。リッチメニューのボタンが送る文)で始める・解除する。
  dip のログインの画面へのボタン(link_messages)と、dip がログインの後に本人を送る LINE の画面の URL(account_link_url)。
- LIFF(task 203): LINE のアプリで開く LIFF 画面の本人。LIFF の ID トークンを LINE ログインの検証の API で確かめ(LineLogin)、
  sub からトークと同じ line_user の ID を作る。確かめた本人は署名つきの Cookie の値(liff_session)で覚える。
  LINE_LIFF_ID と LINE_LIFF_CHANNEL_ID(前の名前 LINE_LOGIN_CHANNEL_ID も読む)の両方があるときだけ使う(liff_configured)。
  LINE のボタンは LIFF URL になる(liff_url_of)。
- LIFF で使う操作(task 204): 返信の actions(履歴書を選ぶ・音声相談を始める)は、LIFF で開けるなら LIFF 画面へのボタン
  (LIFF_ACTIONS)、開けなければ今までどおり断りの文(ACTION_NOTES)。レジュメ・アーケードのボタンは LIFF で開けるときだけ出す。
  LINE で使えるもの(liff_features)はエージェントにも伝える(line_runtime.agent の窓口)。
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
import time
from pathlib import Path
from typing import Self
from urllib.parse import quote, urlencode

import httpx

log = logging.getLogger(__name__)

SECRET_ENV = "LINE_CHANNEL_SECRET"
TOKEN_ENV = "LINE_CHANNEL_ACCESS_TOKEN"
PUBLIC_URL_ENV = "AICA_PUBLIC_URL"
ID_KEY_ENV = "AICA_ID_KEY"
MIN_ID_KEY_BYTES = 32  # 鍵の長さの下限(UTF-8 のバイト数)

API_BASE = "https://api.line.me"
ACCOUNT_LINK_URL = "https://access.line.me/dialog/bot/accountLink"  # dip がログインの後に本人を送る LINE の画面
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


def account_link_url(link_token: str, nonce: str, base: str = ACCOUNT_LINK_URL) -> str:
    return f"{base}?{urlencode({'linkToken': link_token, 'nonce': nonce})}"


class IdKeyError(RuntimeError):
    """LINE のユーザーIDを作る鍵(AICA_ID_KEY)が無い・短い。"""


def id_key() -> bytes:
    """LINE のユーザーIDを作る aica 専用の鍵。無い・MIN_ID_KEY_BYTES より短いときは IdKeyError。"""
    key = os.environ.get(ID_KEY_ENV, "").strip().encode()
    if len(key) < MIN_ID_KEY_BYTES:
        why = "がありません" if not key else f"が短すぎます({len(key)} バイト。{MIN_ID_KEY_BYTES} バイト以上にしてください)"
        raise IdKeyError(f"{ID_KEY_ENV} {why}。LINE のユーザーIDを作る鍵です。.env に書いてください"
                         ' (作り方の例: python -c "import secrets; print(secrets.token_hex(32))")')
    return key


def check_id_key() -> None:
    """起動のときに呼ぶ。LINE の設定(シークレットとトークン)か LIFF の設定があるのに鍵が無い・短ければ IdKeyError で止める。
    どちらの設定も無ければ、鍵は要らない(何もしない)。"""
    if configured() or liff_configured():
        id_key()
    elif liff_id() or login_channel_id():
        log.warning("%s と %s の片方しか無いので、LIFF を使いません", LIFF_ID_ENV, LIFF_CHANNEL_ENV)


def line_user(user_id: str, key: bytes | None = None) -> str:
    """LINE の userId から aica のユーザーID「l-<16桁hex>」を作る(同じ鍵と userId なら毎回同じ)。
    LINE のユーザーIDを作るのはこの関数だけ。HMAC-SHA256 で、鍵は aica 専用の AICA_ID_KEY(key を与えなければ環境変数から)。
    鍵を知らなければ、userId を知っていても l-… を作れない(データが漏れても誰のものか突き止められない)。
    チャネルシークレットは鍵に使わない(作り直すと全員の ID が変わる)。鍵を変えると全員の ID が変わり、前のデータとつながらない。"""
    return "l-" + hmac.new(key or id_key(), user_id.encode(), hashlib.sha256).hexdigest()[:16]


# ----------------------------- LIFF(LINE のアプリで開く画面の本人) -----------------------------
# LINE のアプリで開いた LIFF 画面(/liff/line/…)は、LIFF SDK の ID トークンを LINE の検証の API で確かめ、その sub(LINE の userId)から
# トークと同じ line_user の ID を作る。確かめた本人は、署名つきの Cookie(liff_session)で 1 時間だけ覚える。

LIFF_ID_ENV = "LINE_LIFF_ID"
# LIFF アプリを置いたチャネルの ID(ID トークンの aud)。LINE ミニアプリのチャネルなら、その環境(開発中・審査用・公開済み)のチャネルの ID。
# 前は LINE ログインのチャネルだけを考えて LINE_LOGIN_CHANNEL_ID と呼んでいた。前の名前も読む(新しい名前が先)
LIFF_CHANNEL_ENV = "LINE_LIFF_CHANNEL_ID"
LOGIN_CHANNEL_ENV = "LINE_LOGIN_CHANNEL_ID"
LIFF_URL = "https://liff.line.me"
LIFF_PREFIX = "/liff/line"  # LIFF アプリのエンドポイント URL のパス。LINE のアプリで開く画面は、すべてこの下
VERIFY_PATH = "/oauth2/v2.1/verify"
ID_TOKEN_ISSUER = "https://access.line.me"
RAW_LINE_ID = re.compile(r"U[0-9a-f]{32}")  # LINE の userId の形
MAX_ID_TOKEN_CHARS = 4096
LIFF_SESSION_SECONDS = 3600
LIFF_PAGES = ("/liff/apply", "/liff/resume", "/liff/voice/draft")  # LIFF_PREFIX の下にもある画面(line_runtime.server)
ARCADE_PATH = "/liff/arcade"  # レジュメ・アーケード(ハブと各ゲーム)。LIFF_PREFIX の下にもある。LIFF で開けなければ LINE に出さない
# 返信の actions → LINE で開く LIFF 画面(LIFF_PREFIX の下のパス)とボタンの文言。Web のトークの「ファイルを選ぶ」「話して伝える」の代わり
LIFF_ACTIONS = {
    "resume_upload": ("📄 履歴書・職務経歴書を送る", "resume/upload"),
    "voice_consult": ("🎤 音声で相談する(約5分)", "voice"),
}
LIVEKIT_ENVS = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")  # 音声相談に要る(voice_resume.livekit_token と同じ)


def liff_id() -> str:
    return os.environ.get(LIFF_ID_ENV, "").strip()


def login_channel_id() -> str:
    """LIFF アプリを置いたチャネルの ID(LINE_LIFF_CHANNEL_ID。無ければ前の名前の LINE_LOGIN_CHANNEL_ID)。"""
    new, old = os.environ.get(LIFF_CHANNEL_ENV, "").strip(), os.environ.get(LOGIN_CHANNEL_ENV, "").strip()
    if new and old and new != old:
        log.warning("%s と %s が違うので、%s を使います", LIFF_CHANNEL_ENV, LOGIN_CHANNEL_ENV, LIFF_CHANNEL_ENV)
    return new or old


def liff_configured() -> bool:
    """LIFF ID とチャネル ID の両方があるときだけ LIFF を使う(無ければ /liff/line/… と
    POST /api/liff/… は 404、LINE のボタンは今までどおり AICA_PUBLIC_URL の画面を開く)。"""
    return bool(liff_id() and login_channel_id())


def voice_configured() -> bool:
    """音声相談(LiveKit)の設定があるか。"""
    return all(os.environ.get(k) for k in LIVEKIT_ENVS)


def liff_features() -> tuple[str, ...]:
    """LINE のトークから LIFF 画面で使えるもの: resume_upload(履歴書を読み込む)、voice_consult(音声相談。LiveKit の
    設定も要る)、arcade(レジュメ・アーケード)。LIFF の設定が無ければ空(どれも LINE では使えない)。"""
    if not liff_configured():
        return ()
    return ("resume_upload",) + (("voice_consult",) if voice_configured() else ()) + ("arcade",)


def _is_arcade(path: str) -> bool:
    p = path.partition("?")[0]
    return p == ARCADE_PATH or p.startswith(ARCADE_PATH + "/")


def liff_page_url(subpath: str) -> str:
    """LIFF_PREFIX の下の画面(subpath。例 "voice")を開く LIFF URL(https://liff.line.me/<LIFF ID>/<subpath>)。
    LINE はエンドポイント URL(…/liff/line)の後ろに subpath を付けて開く。LINE ミニアプリの LIFF ID でも、この形で開ける。"""
    return f"{LIFF_URL}/{liff_id()}/{subpath}"


def liff_url_of(path: str) -> str | None:
    """LIFF 画面のパス(/liff/…)を、LINE のアプリで開く LIFF URL(https://liff.line.me/<LIFF ID>/…)にする。
    LINE はエンドポイント URL(…/liff/line)の後ろに …の部分を付けて開く(/liff/apply?job=x → /liff/line/apply?job=x)。
    LIFF の設定が無い、または LIFF_PREFIX の下に無い画面なら None。"""
    if not liff_configured() or (path.partition("?")[0] not in LIFF_PAGES and not _is_arcade(path)):
        return None
    return liff_page_url(path.removeprefix("/liff/"))


class LineLogin:
    """LINE ログインの API(ID トークンの検証)。transport はテストで偽物の LINE に差し替える。"""

    def __init__(self, channel_id: str, base_url: str = API_BASE, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.channel_id = channel_id
        self._base_url, self._transport = base_url, transport

    async def verify(self, id_token: str, now: float | None = None) -> str | None:
        """ID トークンを LINE に確かめてもらい(署名・発行元・aud・期限)、本人の LINE の userId(sub)を返す。
        確かめられなければ None。LINE の答えも鵜呑みにせず、発行元・aud・期限・sub の形をもう一度見る。
        トークンと userId はログに出さない。"""
        if not self.channel_id or not id_token or len(id_token) > MAX_ID_TOKEN_CHARS:
            return None
        try:
            async with httpx.AsyncClient(base_url=self._base_url, transport=self._transport, timeout=10.0) as c:
                r = await c.post(VERIFY_PATH, data={"id_token": id_token, "client_id": self.channel_id})
        except httpx.HTTPError as e:
            log.warning("LINE の ID トークンの検証を呼べませんでした: %s", type(e).__name__)
            return None
        try:
            claims = r.json()
        except ValueError:
            claims = None
        if not isinstance(claims, dict):
            claims = {}
        if not r.is_success:
            log.warning("LINE が ID トークンを断りました(%d): %s", r.status_code, str(claims.get("error_description", ""))[:80])
            return None
        sub, exp = claims.get("sub"), claims.get("exp")
        now = time.time() if now is None else now
        if (claims.get("iss") != ID_TOKEN_ISSUER or str(claims.get("aud")) != self.channel_id
                or not isinstance(exp, int | float) or exp <= now or not isinstance(sub, str) or not RAW_LINE_ID.fullmatch(sub)):
            log.warning("LINE の検証の答えが、このチャネルの有効な ID トークンではありません")
            return None
        return sub


def _session_key() -> bytes:
    """LIFF の Cookie の署名の鍵。AICA_ID_KEY から用途を分けて作る(ID を作る HMAC と同じ値にしない)。"""
    return hmac.new(id_key(), b"aica-liff-session", hashlib.sha256).digest()


def _session_sig(body: str) -> str:
    return hmac.new(_session_key(), body.encode(), hashlib.sha256).hexdigest()


def liff_session(user: str, now: float | None = None) -> str:
    """LIFF で確かめた本人(l-…)を覚える Cookie の値「l-….<期限の UNIX 秒>.<署名>」。"""
    body = f"{user}.{int((time.time() if now is None else now) + LIFF_SESSION_SECONDS)}"
    return f"{body}.{_session_sig(body)}"


def liff_session_user(value: str | None, now: float | None = None) -> str | None:
    """Cookie の値から本人(l-…)。形が違う・署名が合わない・期限切れなら None。"""
    user, _, rest = (value or "").partition(".")
    exp, _, sig = rest.partition(".")
    if not LINE_ID.fullmatch(user) or not exp.isdigit() or not sig:
        return None
    if not hmac.compare_digest(sig.encode(), _session_sig(f"{user}.{exp}").encode()):
        return None
    return user if int(exp) > (time.time() if now is None else now) else None


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

# actions(Web の画面の「ファイルを選ぶ」「話して伝える」)を LIFF で開けないときは、文で案内する(守り。エージェントには
# LINE で使えないと伝えてあるので、ふつうは付かない)
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


def _buttons_message(links: list[dict]) -> dict:
    """リンク({label, uri})をボタンの並んだ Flex にする。"""
    return {"type": "flex", "altText": _clip("リンク: " + "、".join(b["label"] for b in links), 400),
            "contents": {"type": "bubble", "size": "kilo", "body": {
                "type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                    {"type": "button", "style": "link", "height": "sm",
                     "action": {"type": "uri", "label": _clip(b["label"], 20), "uri": b["uri"]}}
                    for b in links]}}}


def _quick_reply(labels: list[str]) -> dict:
    return {"items": [{"type": "action", "action": {"type": "message", "label": _clip(q, 20), "text": _clip(q, 300)}}
                      for q in labels[:13]]}


def _labeled(items: list[str]) -> list[str]:
    """空・空白だけの文言を除く(LINE は空のラベルや本文を断り、そのメッセージ全体が届かなくなる)。"""
    return [x.strip() for x in items if isinstance(x, str) and x.strip()]


def to_messages(reply: dict, base_url: str | None = None) -> list[dict]:
    """render の結果(上限の案内 limit_reply も同じ形)を、LINE に送るメッセージ(5 つまで)にする。
    - bubbles → テキスト
    - actions → LIFF で使えれば(liff_features)LIFF 画面へのボタン(LIFF_ACTIONS)。使えなければ、吹き出しの後ろに断りの文
    - cards → Flex の carousel(render が dip のツールの返した求人だけにしている)
    - buttons(/liff/…)→ LIFF URL にできれば LIFF URL(liff_url_of)、できなければ公開 URL(base_url。既定は AICA_PUBLIC_URL)
      へのリンク。どちらも無ければ出さない。レジュメ・アーケードは LIFF URL にできるときだけ出す(公開 URL では本人にならない)
    - quick_replies → 最後のメッセージのクイックリプライ
    5 つを超えるときは、テキストを後ろから 1 つにまとめる(求人とリンクは残す)。"""
    base = public_url() if base_url is None else base_url.strip().rstrip("/")
    features = liff_features()
    actions = [a for a in dict.fromkeys(reply.get("actions") or []) if a in LIFF_ACTIONS]
    texts = _labeled(reply.get("bubbles") or [])
    texts += [ACTION_NOTES[a] for a in actions if a not in features]
    flex = []
    if reply.get("cards"):
        flex.append(_cards_message(reply["cards"]))
    buttons = [{"label": b["label"].strip(), "url": b["url"]} for b in reply.get("buttons") or []
               if str(b.get("url", "")).startswith("/liff/") and _labeled([b.get("label")])]
    if arcade := [b for b in buttons if _is_arcade(b["url"]) and "arcade" not in features]:
        log.warning("LIFF の設定が無いので、レジュメ・アーケードのボタン %d 個を LINE に出しません", len(arcade))
        buttons = [b for b in buttons if b not in arcade]
    if not base.startswith(("https://", "http://")):  # 公開 URL が無ければ、LIFF URL にできるボタンだけ出す
        dropped = [b for b in buttons if not liff_url_of(b["url"])]
        if dropped:
            log.warning("%s が無い(か http(s):// で始まらない)ので、LIFF 画面のボタン %d 個を LINE に出しません",
                        PUBLIC_URL_ENV, len(dropped))
        buttons = [b for b in buttons if b not in dropped]
    links = [{"label": b["label"], "uri": liff_url_of(b["url"]) or base + b["url"]} for b in buttons]
    links += [{"label": LIFF_ACTIONS[a][0], "uri": liff_page_url(LIFF_ACTIONS[a][1])} for a in actions if a in features]
    if links:
        flex.append(_buttons_message(links))
    room = MAX_MESSAGES - len(flex)
    if len(texts) > room:
        texts = [*texts[: room - 1], "\n\n".join(texts[room - 1:])]
    messages = [_text(t) for t in texts] + flex
    if not messages:  # 送るものが無い(吹き出しが空): 入力中の表示のまま黙らない
        messages = [_text(FAILED_TEXT)]
    if quick := _labeled(reply.get("quick_replies") or []):
        messages[-1]["quickReply"] = _quick_reply(quick)
    return messages


# ----------------------------- アカウント連携 -----------------------------

LINK_TEXT = "アカウント連携"  # 本人がこの文を送ると連携を始める(リッチメニューのボタンが送る文)
UNLINK_TEXT = "連携解除"
LINK_LOGIN_PATH = "/dip/login"  # dip のログインの画面(偽物。line_runtime.server)
LINK_INTRO = "dip にログインすると、この LINE と dip の会員がつながり、閲覧や応募の状況をふまえて相談できます。リンクの期限は 10 分です。"
LINKED_TEXT = "dip の会員とつながりました。閲覧や応募の状況をふまえて相談できます。解除するときは「連携解除」と送ってください。"
ALREADY_LINKED_TEXT = "この LINE は、もう dip の会員とつながっています。解除するときは「連携解除」と送ってください。"
LINK_FAILED_TEXT = "dip の会員とつなげませんでした。もう一度「アカウント連携」と送って、最初からやり直してください。"
LINK_UNAVAILABLE_TEXT = "いまは dip の会員とつなぐことができません。少し時間をおいて、もう一度送ってください。"
UNLINKED_TEXT = "dip の会員との連携を解除しました。もう一度つなぐときは「アカウント連携」と送ってください。"
NOT_LINKED_TEXT = "この LINE は、dip の会員とつながっていません。つなぐときは「アカウント連携」と送ってください。"


def link_command(text: str) -> str | None:
    """連携を始める・解除する決まった文なら "link" / "unlink"(前後の空白は見ない)。ほかは None(ふつうの発言)。"""
    return {LINK_TEXT: "link", UNLINK_TEXT: "unlink"}.get(text.strip())


def link_messages(base: str, link_token: str) -> list[dict]:
    """dip のログインの画面(linkToken つき)へのボタン。"""
    url = f"{base}{LINK_LOGIN_PATH}?{urlencode({'linkToken': link_token})}"
    return [_text(LINK_INTRO),
            {"type": "flex", "altText": "dip にログインして連携する", "contents": {"type": "bubble", "size": "kilo", "body": {
                "type": "box", "layout": "vertical", "contents": [
                    {"type": "button", "style": "primary", "color": "#06c755", "height": "sm",
                     "action": {"type": "uri", "label": "dip にログイン", "uri": url}}]}}}]


def texts(*items: str) -> list[dict]:
    return [_text(t) for t in items]


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

    async def link_token(self, user_id: str) -> str | None:
        """アカウント連携の linkToken を発行してもらう(10 分、1 回だけ使える)。もらえなければ None。"""
        path = f"/v2/bot/user/{quote(user_id, safe='')}/linkToken"
        try:
            r = await self._client.post(path)
        except httpx.HTTPError as e:
            log.warning("LINE API linkToken を呼べませんでした: %s", type(e).__name__)
            return None
        if not r.is_success:
            log.warning("LINE API linkToken が %d を返しました", r.status_code)
            return None
        try:
            token = r.json().get("linkToken")
        except (ValueError, AttributeError):
            token = None
        return token if isinstance(token, str) and token else None

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
