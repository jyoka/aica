"""Web: 本番のトーク画面(/)+ 開発者画面(/dev: シナリオ起動・dip の状態・処理の流れ)+ LIFF 画面(dip 側のモック)。

本番の / は求職者が使うトーク1画面。シナリオを選ばせず、開いたブラウザの匿名ユーザー(匿名ID そのもの)について
最後のやりとりから30分以内なら今の会話を続け(/api/talk/history で履歴を描き直す。LLM は呼ばない)、
過ぎていれば dip が初回訪問か再訪かを決め(/api/talk/open)、AI 側の1通目から新しい会話を始める。
/dev のシナリオは評価と同じ一覧(evals/scenarios.json)。どれも AI 側の1通目から始まる。

    ./.venv/bin/python -m line_runtime.server   # http://127.0.0.1:8030

LIFF 画面と状態確認 API は本来 dip が持つ Web 画面なので、dip の State を直接使う。
エージェントとの会話は LineRuntime(MCP 経由)だけを通る。

マルチユーザー: 画面を開いたとき(GET / と GET /dev)にブラウザごとに匿名ユーザーIDを発行し、Cookie で保持する
(認証・招待制はしない)。本番の / は匿名ID そのものをユーザーIDにする。/dev のシナリオはブラウザ専用のユーザー
「匿名ID:シナリオユーザー」で始めるので、複数の知人が同時に使っても会話・プロフィール・行動履歴・費用台帳が混線しない。

ユーザーの分離の守り(task 99):
- ブラウザから呼ぶ経路は、正しい形式の Cookie が必須(無い・不正なら 401)。扱えるのは Cookie の匿名IDのユーザーだけ
  (他人のユーザーIDを本文に入れても 403)。ID を名乗るだけでは読めない・話せない・会話数の上限も迂回できない。
- 本人のデータを指す URL にユーザーIDを載せない。API はユーザーIDを本文で受け取り、画面(LIFF)は Cookie から本人を決める。
- サーバー間の経路(音声ワーカー → POST /api/voice/result と /api/voice/session)は、Cookie ではなく共有鍵(AICA_INTERNAL_KEY)で守る。

会話数の上限(line_runtime.quota): 1日あたりのターン数に、匿名IDごと・IP ごと・全体の上限があり、超えたら LLM を呼ばずに
丁寧に打ち切る。IP ごとの上限で、Cookie を作り直して匿名IDごとの上限を迂回する道を閉じる(IP の取り方は _client_ip、
IPv6 は /64 にまとめて数える)。全体の匿名IDの数の上限で、IP をいくつ持っていても状態ファイルにユーザーが増え続けない。

LINE 公式アカウント(task 177): POST /line/webhook が 2 つ目のチャネル。LINE_CHANNEL_SECRET と LINE_CHANNEL_ACCESS_TOKEN が
あるときだけ開く(無ければ 404)。そのときは LINE のユーザーIDを作る鍵 AICA_ID_KEY も要る(無い・短ければ起動で止まる。
ADR-0008)。署名(X-Line-Signature)が合わなければ 400。テキストのメッセージは Web と同じターンの処理(line_runtime.turn)に流し、
友だち追加(follow)では初回の挨拶を返す。部品は line_runtime.line_channel。

LINE のアカウント連携(task 182、ADR-0007): 本人が「アカウント連携」と送ると、bot が LINE から linkToken をもらい、dip の
ログインの画面(GET/POST /dip/login。dip の画面の偽物)へのボタンを返す。ログインできると dip が nonce を作り、本人を LINE の
accountLink の画面に送る。LINE から accountLink の webhook が来たら、nonce から会員を引いて連携の表に書く(dip_mcp.accounts)。
「連携解除」で外す。Web の LINE 風の画面の匿名ユーザーは、開発者画面から同じ流れを試せる(/api/dev/link。LINE の役は
偽物の LINE(line_runtime.fake_line)がして、accountLink の画面は /fake-line/… に置く)。

LINE で見る、消す(docs/design/privacy.md の 3.7): 「データを見る」と送ると、プロフィール画面と同じ中身をテキストで返す。
「データを消す」と送ると、消す範囲と確かめのクイックリプライ(postback)を返し、「すべて消す」を押したら Web の「すべて消す」と
同じ処理(_forget_all)をする。どちらも LLM を呼ばず、会話数も数えない。友だち解除(unfollow。ブロック)では消さない
(間違えて押すこともあり、解除すれば前の続きから使えるため。使われなくなったユーザーの期間で消す掃除に任せる)。

レジュメ・アーケード(task 187、188。docs/prd/resume-arcade.md): ゲームの画面(/liff/arcade…)は Cookie を発行も要求もしない。
ハブに出す 2 本は、AI と下書きの API(/api/arcade/start、/ai、/save)を Cookie の本人として呼ぶ。画面は指示の文を送らず、
LLM はサーバーが resume_arcade で呼ぶ(LINE_SIM_PROVIDER)。費用の上限は遊びの番号ごと(dip_mcp.arcade)と人ごとの 1 日の回数。

LINE のアプリで開く LIFF 画面(task 203): LINE_LIFF_ID とチャネル ID があるときだけ、/liff/line/… を開く(無ければ 404)。
ここは Cookie の匿名ユーザーIDを見ず、LIFF の ID トークンを LINE に確かめてもらった LINE のユーザー(トークと同じ l-…)だけを本人にする
(POST /api/liff/session が確かめて、署名つきの Cookie aica_liff を返す)。Web のブラウザの /liff/… は今までどおり Cookie の匿名ユーザーID。
チャネル ID の名前は LINE_LIFF_CHANNEL_ID(前の名前 LINE_LOGIN_CHANNEL_ID も読む。task 204)。

LINE のトークから LIFF 画面で使う操作(task 204): 返信の actions は LINE では LIFF 画面へのボタンになり、音声相談(/liff/line/voice)、
履歴書の読み込み(/liff/line/resume/upload)、レジュメ・アーケード(/liff/line/arcade)を LINE の本人として開く。音声相談の結果と、
履歴書の確認と続きの返事は、その本人の LINE のトークに push で届く。push の宛先(LINE の userId)は、操作を始めるときに LIFF の
ID トークンをもう一度確かめて受け取り、届けるまでメモリにだけ置く(保存しない)。ゲームの AI と下書きの API は今も Cookie の
匿名ユーザーIDの本人で呼ぶので、LIFF の中のゲームは AI なしの簡易分析で遊べて、保存はできない(dip も LINE のユーザーには
ゲームの URL を渡さない。dip_mcp.arcade.game_url)。

LIFF の中でマイクを試す画面(task 205): /liff/line/mic-check。LINE のアプリの LIFF 画面で、音声相談に要るマイク(getUserMedia)と
WebRTC が使えるかを人が実機で確かめる。結果(使えたか、エラー名、音の大きさ、LIFF の環境)だけを POST /api/liff/mic-check が
ログに 1 行書く。声もその文字起こしも送らない・残さない。本人を決めない(本人のデータを読み書きしない)。

利用規約とプライバシーポリシー(task 207): GET /legal/terms と GET /legal/privacy。原文は docs/legal/ の Markdown で、
line_runtime.legal が HTML にする。Cookie を発行も要求もしない。トーク画面と LIFF 画面の下から開ける(legal.footer_html)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import time
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dip_mcp import accounts, arcade, behavior, logic, profile, retention
from dip_mcp.data import State, load_jobs
from evals.scenarios import load_scenarios, prepare
from line_runtime import config, fake_line, legal, line_channel, llm, purge, turn
from line_runtime.arcade_reply import arcade_reply
from line_runtime.log_policy import quiet_sdk_loggers
from line_runtime.quota import (
    DailyQuota,
    daily_limit,
    ip_ids_limit,
    ip_limit,
    total_ids_limit,
    total_limit,
)
from line_runtime.resume_confirm import INTRO as RESUME_INTRO
from line_runtime.resume_confirm import RESUME_LABEL, resume_bubbles
from line_runtime.runtime import LineRuntime
from line_runtime.voice_reply import voice_reply
from line_runtime.voice_rooms import JOIN_WINDOW, ActiveVoiceRooms
from resume_arcade import draft as arcade_draft
from resume_arcade import schemas as arcade_schemas
from resume_arcade import stages as arcade_stages
from resume_draft.schema import document_kind_of
from resume_intake import config as resume_config
from resume_intake.extract import extract_bytes, finalize, media_type

STATIC = config.ROOT / "line_runtime" / "static"
state = State()

# ブラウザごとの匿名ユーザーID(Cookie)。ブラウザ側には ID しか置かず、状態はすべてサーバー側でこの ID にひも付く。
ANON_COOKIE = "aica_uid"
ANON_ID = re.compile(r"w-[0-9a-f]{16}")
# 最後に画面を開いた日から 180 日(使われなくなった人の dip の状態を消す期間と同じ。dip_mcp.retention.USER_TTL)
ANON_COOKIE_MAX_AGE = 180 * 24 * 3600


def _anon(request: Request) -> str | None:
    v = request.cookies.get(ANON_COOKIE, "")
    return v if ANON_ID.fullmatch(v) else None


# https で公開するとき(Render など)だけ 1 にする。そのときだけ Cookie に Secure を付ける(http では送り返されなくなるので、
# 手元の http://127.0.0.1 では付けない)。コンテナの image は 1 にしてある(Dockerfile)。
SECURE_COOKIE_ENV = "AICA_SECURE_COOKIE"


def _issue(request: Request, response: Response) -> str:
    """このブラウザの匿名ユーザーID。無ければ発行して Cookie に保存する(再アクセスで同じ ID になる)。
    あれば同じ ID で Cookie を書き直し、期限を今日から ANON_COOKIE_MAX_AGE に延ばす(毎日使う人が 181 日目に ID を
    失わないように。期限は「最後に画面を開いた日から」になる。docs/design/privacy.md の 3.3 の注)。
    発行と書き直しは画面を開いたとき(GET / と GET /dev)だけ。API は発行も書き直しもしない(Cookie が無ければ 401)。"""
    uid = _anon(request) or f"w-{secrets.token_hex(8)}"
    response.set_cookie(ANON_COOKIE, uid, max_age=ANON_COOKIE_MAX_AGE, samesite="lax", httponly=True,
                        secure=os.environ.get(SECURE_COOKIE_ENV, "").strip() == "1")
    return uid


def _owner(request: Request) -> str:
    """このブラウザの匿名ユーザーID(Cookie)。Cookie が無い・形式が不正なら 401(ID を名乗るだけでは使えない)。"""
    anon = _anon(request)
    if anon is None:
        raise HTTPException(401, "このブラウザのユーザーが分かりません。トーク画面を開き直してください")
    return anon


def _scenario_users() -> set[str]:
    """開発者画面のシナリオユーザー(「匿名ID:シナリオユーザー」の後ろ半分に使えるもの)。"""
    return {sc["user"] for sc in load_scenarios()}


def _require_own(request: Request, user_id: str) -> None:
    """ブラウザは、Cookie の匿名IDのユーザー(匿名IDそのもの、または「匿名ID:シナリオユーザー」)だけを扱える。
    Cookie が無い・不正なら 401、他人のユーザーIDなら 403。シナリオユーザーは scenarios.json にあるものだけ
    (1つの Cookie から任意の名前のユーザーを作って、状態ファイルやメモリを増やせないように)。"""
    anon = _owner(request)
    if user_id == anon:
        return
    owner, _, scenario_user = user_id.partition(":")
    if owner != anon or scenario_user not in _scenario_users():
        raise HTTPException(403, "このブラウザのユーザーIDではありません")


def _page_user(request: Request, scenario_user: str | None) -> str:
    """dip の画面(LIFF)を開いたユーザー。URL にはユーザーIDを載せず(dip_mcp.logic.liff_url)、Cookie の匿名IDで
    本人を決める。開発者画面のシナリオなら、URL の scenario_user を付けた「匿名ID:シナリオユーザー」。"""
    anon = _owner(request)
    if not scenario_user:
        return anon
    if scenario_user not in _scenario_users():
        raise HTTPException(400, "scenario_user が不正です")
    return f"{anon}:{scenario_user}"


# サーバー間の経路(音声ワーカー → POST /api/voice/result)の共有鍵。ブラウザの Cookie ではなくこれで守る。
# 設定が無ければ、その経路はすべて断る(鍵なしで開けない)。
INTERNAL_KEY_ENV = "AICA_INTERNAL_KEY"


def _require_internal(request: Request) -> None:
    """Authorization: Bearer <AICA_INTERNAL_KEY> の呼び出しだけを通す(無い・違う・サーバーに鍵が無いなら 401)。"""
    key = os.environ.get(INTERNAL_KEY_ENV, "")
    got = request.headers.get("authorization", "")
    if not key or not secrets.compare_digest(got.encode(), f"Bearer {key}".encode()):
        raise HTTPException(401, "サーバー間の共有鍵がありません")


# プロキシ(Render など)の後ろに置くときだけ 1 にする。そのときだけ X-Forwarded-For の最後の値(プロキシが付けた、
# 直前の接続元)を本人の IP として使う。それより前の値は本人が自由に書けるので見ない。
TRUST_PROXY_ENV = "AICA_TRUST_PROXY"


def _client_ip(request: Request) -> str:
    """会話数の IP ごとの上限を数える IP。既定は接続元(request.client.host)で、X-Forwarded-For は見ない(詐称できない)。
    IPv6 を /64 にまとめるのは DailyQuota(quota.ip_key)。"""
    if os.environ.get(TRUST_PROXY_ENV, "").strip() == "1":
        forwarded = ",".join(request.headers.getlist("x-forwarded-for")).split(",")[-1].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


def _admit(request: Request, user_id: str) -> None:
    """ターンを使わずに状態へ書く API の前に呼ぶ。この IP から使える匿名IDの数を超えるなら 429(状態に書かせない。
    Cookie を作り直して状態ファイルにユーザーを増やし続けられないように)。"""
    msg = app.state.quota.admit(user_id, _client_ip(request))
    if msg is not None:
        raise HTTPException(429, msg)


def _profile_writer(request: Request) -> str:
    """プロフィールを書き換える本人(Cookie の匿名ID)。この IP の匿名IDの数を超えるなら 429。
    書き換えは最後の利用として記録する(期間で消す掃除が数える。dip_mcp.retention)。"""
    user_id = _owner(request)
    _admit(request, user_id)
    retention.touch(state, user_id)
    return user_id


# 最後のやりとりからこの時間内なら、今の会話を続ける(本番のトーク画面の開き直し・LINE の発言。talk_continues)
TALK_RESUME_WINDOW = timedelta(minutes=30)


def clock() -> datetime:
    """今の時刻(テストで差し替える)。会話の最後のやりとり(チェックポイントの保存時刻、UTC)と比べる。"""
    return datetime.now(UTC)


def talk_continues(conv: dict) -> bool:
    """今の会話(LineRuntime.conversation の結果)を続けるか: 最後のやりとりから TALK_RESUME_WINDOW 以内なら続ける。
    返信の無い会話(last_at が None)は続けない。Web のトーク画面を開いたときと、LINE のテキストのメッセージで同じ決まりを使う。"""
    return conv["last_at"] is not None and clock() - conv["last_at"] <= TALK_RESUME_WINDOW


class EventIn(BaseModel):
    user_id: str
    event: str
    text: str | None = None  # 長さの上限(turn.MAX_TEXT_CHARS)は turn.accept が確かめる(LINE の発言と共通)
    # 本番のトーク画面が表示している会話の番号(/api/talk/open が返す)。/dev・評価は送らない。
    # 送られたら: 発言は今の会話がこれと同じときだけ受け付ける(違えば stale)/ 挨拶が返ったら開いた記録を残す
    thread: int | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.load_env()
    line_channel.check_id_key()  # LINE の設定があるのに AICA_ID_KEY が無い・短ければ、ここで止める
    app.state.quota = DailyQuota(daily_limit(), config.DATA_DIR / "quota.json", ip_limit=ip_limit(),
                                 ip_ids=ip_ids_limit(), total_limit=total_limit(), total_ids=total_ids_limit())
    app.state.voice_ready = {}  # ユーザー → 音声相談の結果が届いたことを待っているシートへ知らせる Condition
    app.state.voice_rooms = ActiveVoiceRooms()  # 人 → 終わっていない音声相談の部屋(同時に 1 つまで)
    app.state.unread_uploads = {}  # ユーザー → 読み取れなかった直前のアップロード(続くターンの facts)
    app.state.line_seen = line_channel.SeenEvents(config.DATA_DIR / "line_events.json")  # LINE の再送に 2 回答えない
    app.state.line_locks = defaultdict(asyncio.Lock)  # LINE のユーザー → その人のイベントを届いた順に 1 つずつ
    app.state.line_forget_asks = {}  # LINE のユーザー → 「データを消す」の確かめ(番号、期限の monotonic 秒)。1 回だけ使える
    app.state.line_voice_rooms = {}  # LIFF から始めた音声相談の部屋 → (LINE の userId, 始めた時刻)。結果を push する宛先。メモリだけ
    app.state.fake_line = fake_line.FakeLinePlatform()  # 開発者画面から試す、Web の匿名ユーザーのアカウント連携の LINE の役
    async with LineRuntime() as rt:
        app.state.rt = rt
        app.state.purge_lock = asyncio.Lock()  # 起動時・定期・内部の口の掃除を重ねない
        await _purge_logged()  # 起動時の掃除(落ちても起動は止めない)
        daily = asyncio.create_task(_purge_daily())
        try:
            yield
        finally:
            daily.cancel()
            with suppress(asyncio.CancelledError):  # 走っている掃除を止めてから会話の DB を閉じる
                await daily


async def _purge() -> dict:
    """期間で消す掃除(line_runtime.purge)。この Web プロセスの中で、ユーザーごとに user_lock を取って消す。"""
    async with app.state.purge_lock:
        return await purge.purge(app.state.rt, state, scenario_users=_scenario_users(),
                                 unread_uploads=app.state.unread_uploads, voice_ready=app.state.voice_ready,
                                 remove_files=_remove_voice_files)


async def _purge_logged() -> None:
    try:
        await _purge()
    except Exception as e:  # noqa: BLE001  掃除の失敗で Web プロセスを止めない(次の回にやり直す)
        purge.log.error("期間で消す掃除が失敗しました: %s", type(e).__name__)


async def _purge_daily() -> None:
    """1 日 1 回の掃除(起動時の掃除の EVERY 後から)。"""
    while True:
        await asyncio.sleep(purge.EVERY.total_seconds())
        await _purge_logged()


app = FastAPI(title="LINE AI assistant simulator", lifespan=lifespan)


@app.post(purge.INTERNAL_PATH)
async def internal_purge(request: Request):
    """期間で消す掃除を今すぐ走らせる(手で走らせる CLI python -m line_runtime.purge が呼ぶ)。消した数を返す。
    Cookie ではなく共有鍵(AICA_INTERNAL_KEY)で守る。消す処理は定期の掃除と同じく、この Web プロセスの中で行う。"""
    _require_internal(request)
    return await _purge()


@app.get("/healthz")
async def healthz():
    """ヘルスチェック(Render が数秒ごとに呼ぶ)。起動を終えていれば 200。LLM も dip も呼ばず、Cookie も発行しない。"""
    return {"status": "ok"}


@app.get("/")
async def index(request: Request):
    """本番のトーク画面(求職者が使う1画面)。初回アクセスならこのブラウザの匿名ユーザーIDを発行する(Cookie)。"""
    resp = FileResponse(STATIC / "talk.html")
    _issue(request, resp)
    return resp


@app.get("/dev")
async def dev(request: Request):
    """開発者画面(評価・説明用): シナリオの起動・dip から見た状態・構成図と処理の流れ。"""
    resp = FileResponse(STATIC / "dev.html")
    _issue(request, resp)
    return resp


SHARED_STATIC = {"talk-core.js", "talk-core.css"}  # / と /dev が共通に使うトークの部品
LIFF_STATIC = {"mic-check.js"}  # LIFF の中でマイクを試す画面(/liff/line/mic-check)


@app.get("/static/{name}")
async def shared_static(name: str):
    """トークルームの共通部品(吹き出し・求人カード・返信の描画・「+」メニュー・履歴書・音声)と、マイクを試す画面の部品。"""
    if name not in SHARED_STATIC | LIFF_STATIC:
        raise HTTPException(404)
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})


@app.get("/legal/{name}", response_class=HTMLResponse)
async def legal_page(name: str):
    """利用規約(terms)とプライバシーポリシー(privacy)。原文は docs/legal/。だれでも開ける(Cookie を発行も要求もしない)。"""
    if name not in legal.DOCS:
        raise HTTPException(404)
    return HTMLResponse(legal.page(name), headers={"Cache-Control": "no-cache"})


# レジュメ・アーケード(docs/prd/resume-arcade.md): ドット絵のゲームで職務経歴書を作る LIFF 画面。ゲームの名前はファイル名と同じ。
# 画面そのものは Cookie を発行も要求もしない(本人のデータを読み書きしない)。ハブに出す 2 本は、AI と保存の API(/api/arcade/…。
# 下の「レジュメ・アーケードの AI と下書き」)を、ほかの LIFF 画面と同じく Cookie の匿名IDの本人として呼ぶ。
ARCADE = STATIC / "arcade"
ARCADE_SHIPPED = arcade.GAMES  # ハブに出す(案内の部品がそろい、AI と保存がつながったもの)
ARCADE_GAMES = ARCADE_SHIPPED + (  # 残りは開発者画面からだけリンクする(AI を使わず、本人のデータも読み書きしない)
    "career-quest", "dungeon-of-past-jobs", "career-pet", "career-fighter", "deck-of-deeds", "star-log")
ARCADE_STATIC = {"arcade-ja.js", "arcade-ai.js"}  # ゲームが共通に使う部品(日本語の答えの判定、AI と保存の API を呼ぶ部品)


@app.get("/static/arcade/{name}")
async def arcade_static(name: str):
    """レジュメ・アーケードの共通部品。"""
    if name not in ARCADE_STATIC:
        raise HTTPException(404)
    return FileResponse(ARCADE / name, headers={"Cache-Control": "no-cache"})


@app.get("/liff/arcade")
async def liff_arcade():
    """レジュメ・アーケードのハブ(ゲームを選ぶ画面)。"""
    return FileResponse(ARCADE / "resume-arcade.html", headers={"Cache-Control": "no-cache"})


@app.get("/liff/arcade/draft", response_class=HTMLResponse)
async def liff_arcade_draft(rid: int, request: Request, scenario_user: str | None = None):
    """ゲームで作った下書きを見て、直す画面(トークの「下書きを見る」、プロフィールのリンク先)。本人は Cookie から決める。
    rid はその下書きの番号: ゲームごとに最新の 1 件だけを持つので、あとで同じゲームで作り直したか消した下書きのリンクは開かない。
    「draft」はゲームの名前に使わない(このルートを {game} より先に登録する)。"""
    user = _page_user(request, scenario_user)
    rec = arcade.draft(state, user, rid)
    if rec is None:
        return HTMLResponse(_page("ゲームで作った下書き", "<p>この下書きは見つかりません。同じゲームで作り直したか、消した"
                                  "下書きです。最新の下書きは、プロフィールから開けます。</p>"), status_code=404)
    return _page(f"{rec['document_kind']}の下書き", _arcade_draft_html(rec, scenario_user))


@app.post("/liff/arcade/draft", response_class=HTMLResponse)
async def liff_arcade_draft_edit(request: Request, rid: int = Form(...),
                                 markdown: str = Form("", max_length=arcade_draft.MARKDOWN_MAX),
                                 scenario_user: str = Form("")):
    """下書きの画面で本人が直した書類を保存する(保存と同じ決まり: scrub_pii、長さの上限、Resume Draft の形)。"""
    user = _page_user(request, scenario_user or None)
    _admit(request, user)
    rec = arcade.draft(state, user, rid)
    if rec is None or not markdown.strip():
        return HTMLResponse(_page("ゲームで作った下書き", "<p>この下書きは見つかりません。</p>"), status_code=404)
    out = profile.edit_arcade_draft(state, user, rid, arcade_draft.to_envelope(markdown, rec["document_kind"]))
    if "error" in out:
        return HTMLResponse(_page("ゲームで作った下書き", "<p>この下書きは見つかりません。</p>"), status_code=404)
    return _page(f"{out['document_kind']}の下書き", "<p class='job'>保存しました。</p>" + _arcade_draft_html(out, scenario_user))


def _arcade_draft_html(rec: dict, scenario_user: str | None) -> str:
    """下書きの画面の中身: 書類(Markdown のまま)、確かめたい点(数字の空欄のある行)、直す欄。"""
    todo = "".join(f"<li>{escape(x)}</li>" for x in rec["resume"]["unconfirmed"])
    return (f"<p>ゲーム({escape(arcade.GAME_NAMES.get(rec['game'], rec['game']))})で、あなたの答えから作った"
            f"{escape(rec['document_kind'])}の下書きです。[ ] の部分は、あなたにしか書けない数字です。"
            "氏名と連絡先は保存していません。内容は必ずご自身で確認・修正してください。</p>"
            + (f"<h3 style='font-size:14px;margin:14px 0 4px'>確かめたい点</h3><ul style='font-size:13px'>{todo}</ul>"
               if todo else "")
            + "<pre id='draft-md' style='white-space:pre-wrap;background:#fff;border-radius:10px;padding:12px;"
              f"font-size:13px'>{escape(rec['markdown'])}</pre>"
            "<form method='post' action='/liff/arcade/draft'>"
            f"<input type='hidden' name='rid' value='{rec['id']}'>"
            f"<input type='hidden' name='scenario_user' value='{escape(scenario_user or '')}'>"
            "<label for='draft-edit'>書類を直す(数字の空欄を埋める、行を書き換える)</label>"
            "<textarea id='draft-edit' name='markdown' rows='16' style='width:100%;box-sizing:border-box;font-size:14px;"
            f"padding:8px;border:1px solid #ccd;border-radius:8px'>{escape(rec['markdown'])}</textarea>"
            "<button>直した内容を保存する</button></form>")


@app.get("/liff/arcade/{game}")
async def liff_arcade_game(game: str):
    """レジュメ・アーケードの 1 本のゲーム。知らない名前は 404(「draft」は (3/3) の下書きの画面に取っておく)。"""
    if game not in ARCADE_GAMES:
        return HTMLResponse(_page("レジュメ・アーケード", '<p>このゲームは見つかりません。<a href="/liff/arcade">ハブに戻る</a></p>'),
                            status_code=404)
    return FileResponse(ARCADE / f"{game}.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/me")
async def me(request: Request):
    """このブラウザの匿名ユーザーID(発行はしない。Cookie が無ければ 401)。"""
    return {"user_id": _owner(request)}


@app.post("/api/talk/open")
async def talk_open(request: Request):
    """本番のトーク画面を開いた: ユーザーは匿名ID そのもの(シナリオ用ユーザーは使わない)。
    - 最後のやりとりから30分以内: 今の会話を続ける(event は None)。画面は /api/talk/history で履歴を描き直す。
      LLM は呼ばず、会話数も使わない。
    - それ以外: dip がそのユーザーの状態から初回訪問か再訪かを決める。続けて画面がそのイベントで
      /api/event/stream を呼び、AI 側の1通目(挨拶)で新しい会話が始まる。開いた記録は挨拶が返ったときに残す
      (返す途中で接続が切れて残せなかったら、次に開いたとき、保存された挨拶から補う)。
    thread は画面が表示する会話の番号(発言のときに送り返す。新しい会話なら挨拶のストリームの thread で置き換わる)。
    状態を初期に戻すことはしない。
    Cookie(GET / で発行)が無ければ 401。ここでは ID を発行せず、dip の状態にも書かない(書くのは挨拶が返ったとき)。"""
    uid = _owner(request)
    rt = app.state.rt
    async with rt.user_lock(uid):  # 進行中のターンと交差しないように
        conv = await rt.conversation(uid)
    if conv["greeted"] and logic.talk_entry(state, uid) == "visit":
        # 挨拶は保存されたが、画面に返す途中で接続が切れて記録できなかった: 会った相手なので記録を補う
        logic.record_talk_open(state, uid)
    track = logic.search_track(state.user(uid))  # ヘッダーの「正社員で相談中」
    if talk_continues(conv):
        return {"user": uid, "event": None, "thread": conv["thread"], "search_track": track}
    return {"user": uid, "event": logic.talk_entry(state, uid), "thread": conv["thread"], "search_track": track}


@app.get("/api/talk/history")
async def talk_history(request: Request):
    """本番のトーク画面に描き直す、今の会話の履歴(吹き出し・求人カード・選択肢)。LLM は呼ばない。
    ユーザーは Cookie の匿名ID(URL には載せない)。"""
    user_id = _owner(request)
    rt = app.state.rt
    async with rt.user_lock(user_id):
        conv = await rt.conversation(user_id)
    for t in conv["turns"]:
        turn.track_status(state, user_id, t["bot"])
    return {"thread": conv["thread"], "turns": conv["turns"], "search_track": logic.search_track(state.user(user_id))}


class UndoIn(BaseModel):
    user_id: str
    id: str  # 取り消す切り替えの記録の id(返信の track_switch.id)


@app.post("/api/talk/track/undo")
async def talk_track_undo(body: UndoIn, request: Request):
    """トークの切り替えの表示のボタン: その切り替えを取り消して元の区分に戻す(dip に取り消しとして記録。LLM は呼ばず、
    会話数も使わない)。AI には次の本人のターンの前に知らせる(turn.profile_notice)。直前の切り替えでなければ 409。
    本人の Cookie のユーザー(/dev の「匿名ID:シナリオユーザー」も)だけ。進行中のターンが終わってから変える。"""
    _require_own(request, body.user_id)
    _admit(request, body.user_id)
    retention.touch(state, body.user_id)
    async with app.state.rt.user_lock(body.user_id):
        r = profile.undo_switch(state, body.user_id, body.id)
    if "error" in r:
        raise HTTPException(409, r["message"])
    return {"search_track": r["track"], "undone": r["undone"], "status": "undone"}


@app.get("/api/saved")
async def saved(request: Request):
    """このブラウザのユーザー(Cookie の匿名ID)が保存した求人(本番のトーク画面の「保存した求人」)。"""
    return {"jobs": logic.saved_jobs(state, _owner(request))}


@app.get("/api/scenarios")
async def scenarios():
    return [{k: sc.get(k) for k in ("id", "title", "desc", "user", "event")} for sc in load_scenarios()]


@app.post("/api/scenarios/{scenario_id}/start")
async def start_scenario(scenario_id: str, request: Request):
    """シナリオを最初から始める: そのユーザーの dip の状態を初期に戻す。続けて画面が /api/event/stream を呼び、
    AI 側の1通目(挨拶・プッシュ)が届く。新しい会話は、どのシナリオでも最初のイベント(visit・revisit・プッシュ)が
    始めるので、ここでは始めない(ここでも始めると会話番号が1つ飛び、再起動後に前の会話へ戻ってしまう)。
    進行中のターンが終わってから初期化する(終わったターンの記録が、初期化した状態に書き込まれないように)。
    そのブラウザ専用のユーザー「匿名ID:シナリオユーザー」を作って始める(同じシナリオを2人が同時に使っても
    混線しない)。Cookie が無ければ 401(fixtures のシナリオユーザーそのものは初期化させない)。"""
    anon = _owner(request)
    sc = next((s for s in load_scenarios() if s["id"] == scenario_id), None)
    if sc is None:
        raise HTTPException(404, f"シナリオ {scenario_id} はない")
    user = f"{anon}:{sc['user']}"
    _admit(request, user)
    async with app.state.rt.user_lock(user):
        prepare(state, sc, user)
    retention.touch(state, user)
    return {"user": user, "event": sc["event"]}


def _deps(request: Request) -> turn.TurnDeps:
    """1ターンの受け付けに使うもの。state は要求のたびに読む(テストが server.state を差し替える)。"""
    return turn.TurnDeps(state=state, quota=app.state.quota, rt=app.state.rt, unread_uploads=app.state.unread_uploads,
                         ip=_client_ip(request))


@app.post("/api/event")
async def event(body: EventIn, request: Request):
    """1ターンを進めて、最後の返信だけを返す。受け付けとターンの決まりは line_runtime.turn。"""
    _require_own(request, body.user_id)
    deps = _deps(request)
    try:
        t = turn.accept(deps, body.user_id, body.event, body.text, body.thread)
        return await turn.reply_of(deps, t)
    except turn.BadTurn as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/event/stream")
async def event_stream(body: EventIn, request: Request):
    """1ターンの途中経過を1行1件の JSON で順に返す(画面のリアルタイム表示用)。
    start → (プッシュなら gate)→ llm / tool_start / tools … → reply。途中で失敗したら error。
    受け付けとターンの決まりは line_runtime.turn。"""
    _require_own(request, body.user_id)
    deps = _deps(request)
    try:
        t = turn.accept(deps, body.user_id, body.event, body.text, body.thread)
    except turn.BadTurn as e:  # 応答を返し始める前なので 400 にできる
        raise HTTPException(400, str(e)) from e

    async def lines():
        yield _line({"kind": "start", "event": t.event, "entry": t.entry, "event_kind": t.kind, "model": config.MODEL})
        try:
            async for ev in turn.run(deps, t):
                yield _line(ev)
        except (turn.BadTurn, turn.TurnFailed) as e:  # API・MCP のエラーも画面に出す(応答を返し始めた後なので HTTP のエラーにはできない)
            yield _line({"kind": "error", "message": _error_text(e)})

    return StreamingResponse(lines(), media_type="application/x-ndjson")


def _error_text(e: Exception) -> str:
    """stream の error の行の文。turn が包む前の例外の名前と文にする(開発者画面に原因の型を出す)。"""
    cause = e.__cause__ or e
    return f"{type(cause).__name__}: {cause}"


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


class UserIn(BaseModel):
    """ユーザーを指す API の本文(ユーザーIDは URL に載せない)。"""
    user_id: str


@app.post("/api/dip")
async def dip_view(body: UserIn, request: Request):
    """dip 側から見たユーザー状態(デバッグ用): 要約と記録された会話結果。保存・応募・最後の接触は、dip のツールと同じく
    行動の要約に、この会話で書いた分を重ねたもの(dip_mcp.behavior.view。新しい順)。"""
    user_id = body.user_id
    _require_own(request, user_id)
    u, b = state.user(user_id), behavior.view(state, user_id)
    return {"summary": logic.conversation_summary(state, user_id), "outcomes": u.get("outcomes", []),
            "saves": b["saves"], "applications": b["applications"], "last_contact": b["last_contact"],
            "dip_id": behavior.dip_id(state, user_id)}


# ----------------------------- レジュメ・アーケードの AI と下書き -----------------------------
# ゲームの画面(ハブに出す 2 本)が呼ぶ。画面は LLM を直接呼ばない。送るのはゲームの名前、段、本人の答え(まとめの段では書類の行)
# だけで、指示の文と出力の形はサーバー(resume_arcade)が持つ。本人は、ほかの LIFF 画面と同じく Cookie の匿名ID
# (開発者画面のシナリオなら scenario_user を付けた「匿名ID:シナリオユーザー」)。Cookie が無ければ 401。
# - start: 遊びの番号を返す(1 人 1 ゲームにつき最新の 1 件。dip_mcp.arcade)。AI を使えるときだけターンを 1 つ数える
# - ai: 有効な番号の遊びで、回数が残っていれば 1 回 LLM を呼ぶ。だめなら LLM を呼ばずに「簡易分析で続ける」を返す
#   (ok: false。ターンも費用台帳も増えない)。画面はその段を端末の簡単な判定で続ける
# - save: 本人の有効な番号の遊びの書類を、下書きとして dip に保存し、今の会話に「下書きができました」を足す(LLM は呼ばない)
# 答えの全文はログに出さない(有無と長さだけ)。

ARCADE_AI_ENV = "AICA_ARCADE_AI"  # 0 にすると AI を止める(すべて簡易分析)
ARCADE_DAILY_ENV = "AICA_ARCADE_DAILY_AI_PLAYS"
DEFAULT_ARCADE_DAILY = 3  # 1 人 1 日の AI つきの遊び
ARCADE_DAILY_KIND = "arcade_ai_play"
PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def arcade_client():
    """ゲームの AI に使う LLM のクライアント(LINE_SIM_PROVIDER。テストでは台本のスタブに差し替える)。"""
    return llm.client()


def _arcade_daily_limit() -> int:
    raw = os.environ.get(ARCADE_DAILY_ENV, "").strip()
    return int(raw) if raw else DEFAULT_ARCADE_DAILY


def _arcade_ai_off_reason(user: str) -> str | None:
    """AI つきで始められない理由(止めている、API キーが無い、1 日の回数を使い切った)。始められれば None。"""
    if os.environ.get(ARCADE_AI_ENV, "1").strip() == "0":
        return "disabled"
    if not os.environ.get(PROVIDER_KEYS.get(config.PROVIDER, ""), "").strip():
        return "no_key"
    limit = _arcade_daily_limit()
    if 0 < limit <= app.state.quota.daily_used(ARCADE_DAILY_KIND, user):
        return "daily_limit"
    return None


class ArcadeStartIn(BaseModel):
    game: str
    scenario_user: str | None = None


class ArcadeAiIn(BaseModel):
    game: str
    play: str = Field("", max_length=64)
    stage: str
    data: dict = {}
    scenario_user: str | None = None


class ArcadeSaveIn(BaseModel):
    game: str
    play: str = Field("", max_length=64)
    markdown: str = Field(max_length=arcade_draft.MARKDOWN_MAX)
    document_kind: str | None = None  # 雇用区分が未定のときだけ、本人が選んだ書類の種類
    scenario_user: str | None = None


def _arcade_game(game: str) -> str:
    if game not in arcade.GAMES:
        raise HTTPException(404, "このゲームは AI と保存につながっていません")
    return game


def _resume_jobs(user: str) -> list[dict]:
    """履歴書の読み取り結果の職歴(勤務先、役職、期間)。ゲームの職場の初期値にする(4 つまで)。"""
    rec = logic.resume_upload(state, user)
    jobs = (rec or {}).get("resume", {}).get("work_history") or []
    return [{"company": w["organization"], "title": w["role"], "dates": w["period"]} for w in jobs
            if w.get("organization")][:arcade_schemas.JOBS_MAX]


@app.post("/api/arcade/start")
async def arcade_start(body: ArcadeStartIn, request: Request):
    """遊びを始める: 遊びの番号を返す(同じゲームの前の番号は無効になる)。ターン数の上限、1 日の AI つきの遊びの上限、
    AI を止める設定、API キーが無いときは、断らずに AI なし(ai: false)の番号を返す(簡易分析で始め、保存はできる)。
    Cookie が無い(トーク画面を開かずにゲームを開いた)ときも 401 にせず、番号なし(play: None)を返す(画面は簡易分析で
    遊べて、保存のボタンを出さない。ゲームを開くたびにブラウザのコンソールに 401 を出さないため)。"""
    game = _arcade_game(body.game)
    if _anon(request) is None:
        return {"play": None, "ai": False, "reason": "no_user", "track": None, "document_kind": None, "jobs": []}
    user = _page_user(request, body.scenario_user)
    _admit(request, user)
    reason = _arcade_ai_off_reason(user)
    if reason is None:
        if app.state.quota.try_take(user, _client_ip(request)) is not None:
            reason = "turn_limit"
        elif not app.state.quota.take_daily(ARCADE_DAILY_KIND, user, _arcade_daily_limit()):
            app.state.quota.refund(user, _client_ip(request))
            reason = "daily_limit"
    rec = arcade.start_play(state, user, game, ai=reason is None)
    retention.touch(state, user)  # ゲームを始めたのも最後の利用(期間で消す掃除が数える)
    return {"play": rec["id"], "ai": rec["ai"], "reason": reason, "track": logic.search_track(state.user(user)),
            "document_kind": arcade.document_kind(state, user), "jobs": _resume_jobs(user)}


def _offline(reason: str) -> dict:
    return {"ok": False, "offline": True, "reason": reason}


@app.post("/api/arcade/ai")
async def arcade_ai(body: ArcadeAiIn, request: Request):
    """ゲームの段を 1 回、AI で進める。だめなとき(番号が無い・ほかの人の番号・AI なしの遊び・24 時間を過ぎた・回数を使い切った・
    まとめの行が多すぎる・形が違う・LLM が失敗した)は ok: false で、画面はその段を簡易分析で続ける。"""
    user = _page_user(request, body.scenario_user)
    st = arcade_stages.stage(body.game, body.stage)
    if st is None:
        return _offline("unknown_stage")
    try:
        d = arcade_stages.prepare(st, body.data)
    except ValidationError:
        return _offline("bad_input")
    if st.kind == "summary" and arcade_stages.summary_chars(d) > arcade_schemas.SUMMARY_MAX:
        return _offline("too_long")
    why = arcade.use_ai(state, user, body.game, body.play, st.kind, keep=st.keep)
    if why is not None:
        return _offline(why)
    try:
        out = await arcade_stages.run(arcade_client(), st, d)
    except Exception as e:  # noqa: BLE001  API のエラーでも、画面は簡易分析で続ける
        line_channel.log.warning("ゲームの AI が失敗しました(%s %s:%s): %s", user, body.game, body.stage, type(e).__name__)
        return _offline("error")
    app.state.rt.ledger.record(user, f"arcade:{body.game}:{body.stage}", out.model, out.usage)
    arcade.record_replaced(state, user, out.replaced)
    return {"ok": True, "result": out.result, "replaced": out.replaced}


@app.post("/api/arcade/save")
async def arcade_save(body: ArcadeSaveIn, request: Request):
    """できた書類を下書きとして保存する(そのゲームの前の下書きを置き換える)。本人の有効な遊びの番号が要る(無い・ほかの人の
    番号なら 403)。書類の種類は雇用区分で決め、未定なら本人が選んだもの(選んでいなければ 400)。雇用区分は変えない。
    今の会話があれば「下書きができました」を AI の返信として足す(LLM は呼ばない。会話が無ければ足さない)。"""
    game = _arcade_game(body.game)
    user = _page_user(request, body.scenario_user)
    if arcade.play(state, user, game, body.play) is None:
        raise HTTPException(403, "この遊びの書類は保存できません。「Markdownでコピー」で書き写してください")
    kind = arcade.document_kind(state, user, body.document_kind)
    if kind is None:
        raise HTTPException(400, "職務経歴書にするか、履歴書の自己PRにするかを選んでください")
    if not body.markdown.strip():
        raise HTTPException(400, "書類が空です")
    rec = arcade.save_draft(state, user, game, body.play, arcade_draft.to_envelope(body.markdown, kind))
    if rec is None:
        raise HTTPException(403, "この遊びの書類は保存できません。「Markdownでコピー」で書き写してください")
    retention.touch(state, user)
    talk = await app.state.rt.add_arcade_result(user, arcade_reply(user, rec))
    return {"draft": rec["id"], "view_url": arcade.draft_url(user, rec["id"]), "document_kind": kind,
            "blanks": arcade.blanks(rec), "talk": talk}


# ----------------------------- LINE 公式アカウント(Messaging API) -----------------------------
# 2 つ目のチャネル。ユーザーは LINE の userId から作った「l-<ハッシュ>」(line_channel.line_user。userId は保存しない)。
# 本番のユーザーなので dip の時計は実際の時刻(data.uses_sim_clock に当たらない)。会話数は LINE のユーザーごとと全体で数え、
# IP ごとには数えない(どの人の webhook も LINE のサーバーから届く)。
# webhook にはすぐ 200 を返し、ターンは応答の後に進める(LLM が遅くても LINE が待ちきれずに再送しないように)。


def line_api() -> line_channel.LineApi:
    """LINE の API(テストでは偽物の LINE API に差し替える)。"""
    return line_channel.LineApi(line_channel.access_token())


def line_login() -> line_channel.LineLogin:
    """LINE ログインの API(LIFF の ID トークンの検証。テストでは偽物の LINE に差し替える)。"""
    return line_channel.LineLogin(line_channel.login_channel_id())


def _line_kind(e: dict) -> str | None:
    """答えるイベント: 1 対 1 のトークの、空でないテキストのメッセージ(message。決まった文なら連携の開始 link、
    解除 unlink、データを見る view_data、データを消す forget_data)、友だち追加(follow)、アカウント連携の結果(account_link)、
    「データを消す」の確かめのクイックリプライ(postback。forget_confirm、forget_cancel)。ほかは None(無視する)。
    standby(ほかのチャネルが応答を受け持っている間)のイベントには答えない。
    友だち解除(unfollow)も None: 受けても消さない(docs/design/privacy.md の 3.7。期間で消す掃除に任せる)。"""
    src = e.get("source") or {}
    if src.get("type") != "user" or not src.get("userId") or e.get("mode", "active") != "active":
        return None
    msg = e.get("message") or {}
    if e.get("type") == "message" and msg.get("type") == "text" and str(msg.get("text") or "").strip():
        text = str(msg["text"])
        return line_channel.link_command(text) or line_channel.data_command(text) or "message"
    if e.get("type") == "accountLink" and isinstance(e.get("link"), dict):
        return "account_link"
    if e.get("type") == "postback" and (pb := line_channel.forget_postback(str((e.get("postback") or {}).get("data") or ""))):
        return pb[0]
    return "follow" if e.get("type") == "follow" else None


@app.post("/line/webhook")
async def line_webhook(request: Request, background: BackgroundTasks):
    """LINE プラットフォームからの webhook。設定が無ければ 404、署名が合わなければ 400(どちらもエージェントを動かさない)。
    同じ webhookEventId(再送)は受けたことがあれば無視する。答えるイベントは応答の後に処理する。"""
    if not line_channel.configured():
        raise HTTPException(404)
    body = await request.body()
    if not line_channel.verify_signature(line_channel.channel_secret(), body, request.headers.get("x-line-signature")):
        raise HTTPException(400, "署名が合いません")
    try:
        events = json.loads(body).get("events") or []
    except (ValueError, AttributeError) as e:
        raise HTTPException(400, "本文が webhook の形ではありません") from e
    todo = [e for e in events if isinstance(e, dict) and _line_kind(e)
            and (not e.get("webhookEventId") or app.state.line_seen.first_time(e["webhookEventId"]))]
    if todo:
        background.add_task(_line_handle, todo)
    return {}


async def _line_handle(events: list[dict]) -> None:
    """webhook のイベントを、LINE のユーザーごとに届いた順で処理する(ユーザーどうしは並べて進める)。"""
    by_user: dict[str, list[dict]] = {}
    for e in events:
        by_user.setdefault(e["source"]["userId"], []).append(e)
    await asyncio.gather(*(_line_user_events(uid, evs) for uid, evs in by_user.items()))


async def _line_user_events(line_uid: str, events: list[dict]) -> None:
    """1 人の LINE ユーザーのイベント: 入力中の表示を出し、ターンを進め、返信トークで返す(返せなければ push)。
    line_uid(LINE の userId)はここでだけ宛先に使い、会話にも dip の状態にも渡さない。"""
    uid = line_channel.line_user(line_uid)
    # 同じ人の webhook どうしも、届いた順に 1 つずつ(入力中の表示を待つ間に、後の発言が先にターンへ入らないように)
    async with app.state.line_locks[uid], line_api() as api:
        for e in events:
            await api.loading(line_uid)
            try:
                messages = await _line_messages(api, line_uid, uid, _line_kind(e), e)
            except Exception as err:  # noqa: BLE001  1 件の失敗で、残りのイベントと本人への返事を止めない
                # 型名だけ(トレースバックの最後の行の例外の文に、LLM の API のエラーの文が入りうる)
                line_channel.log.error("LINE のイベントを処理できませんでした(%s): %s", uid, type(err).__name__)
                messages = line_channel.to_messages({"bubbles": [line_channel.FAILED_TEXT]})
            if messages:
                await api.send(e.get("replyToken"), line_uid, messages)


async def _line_messages(api: line_channel.LineApi, line_uid: str, uid: str, kind: str, e: dict) -> list[dict] | None:
    """1 件のイベントに返す LINE のメッセージ。アカウント連携(開始・結果・解除)と、データを見る、消すは LLM を使わず、
    会話数も数えない。ほかは会話のターン(返すものが無ければ None)。"""
    if kind in ("view_data", "forget_data", "forget_confirm", "forget_cancel"):
        return await _line_data(uid, kind, e)
    if kind == "account_link":
        return line_channel.texts(account_link(uid, e["link"]))
    if kind == "link":
        return await _line_link_start(api, line_uid, uid)
    if kind == "unlink":
        return line_channel.texts(line_channel.UNLINKED_TEXT if _unlink(uid) else line_channel.NOT_LINKED_TEXT)
    reply = await _line_turn(uid, kind, e)
    return line_channel.to_messages(reply) if reply is not None else None


async def _line_link_start(api: line_channel.LineApi, line_uid: str, uid: str) -> list[dict]:
    """連携を始める: LINE から linkToken をもらい、だれに出したかを覚えて、dip のログインの画面へのボタンを返す。
    もう連携していれば、そう伝える。公開 URL(ボタンの前に付ける)が無い・linkToken がもらえなければ、始められないと伝える。"""
    if behavior.dip_id(state, uid):
        return line_channel.texts(line_channel.ALREADY_LINKED_TEXT)
    base = line_channel.public_url()
    if not base.startswith(("https://", "http://")):
        line_channel.log.warning("%s が無いので、アカウント連携を始められません", line_channel.PUBLIC_URL_ENV)
        return line_channel.texts(line_channel.LINK_UNAVAILABLE_TEXT)
    token = await api.link_token(line_uid)
    if token is None:
        return line_channel.texts(line_channel.LINK_UNAVAILABLE_TEXT)
    accounts.of(state).record_link_token(uid, token)
    return line_channel.link_messages(base, token)


async def _line_data(uid: str, kind: str, e: dict) -> list[dict]:
    """データを見る、消す(docs/design/privacy.md の 3.7)。消すのは、直前の「データを消す」で出した確かめ(1 回だけ、
    FORGET_CONFIRM_SECONDS まで)の「すべて消す」が押されたときだけ。範囲は Web の「すべて消す」と同じ(_forget_all)。"""
    asks = app.state.line_forget_asks
    now = time.monotonic()
    for u in [u for u, (_, until) in asks.items() if until <= now]:  # 期限の切れた確かめを捨てる(覚えすぎない)
        del asks[u]
    if kind == "view_data":
        rt = app.state.rt
        async with rt.user_lock(uid):  # 進行中のターンが書き終えてから読む
            talked = await rt.current_thread(uid) > 0
            view = _profile_view(uid)
        return line_channel.data_view_messages(view, talked=talked, linked=behavior.dip_id(state, uid) is not None)
    if kind == "forget_data":
        nonce = secrets.token_urlsafe(16)
        asks[uid] = (nonce, now + line_channel.FORGET_CONFIRM_SECONDS)
        return line_channel.forget_ask_messages(nonce)
    if kind == "forget_cancel":
        asks.pop(uid, None)
        return line_channel.texts(line_channel.FORGET_CANCELLED_TEXT)
    _, nonce = line_channel.forget_postback(str(e["postback"]["data"]))
    if uid not in asks or not secrets.compare_digest(asks[uid][0], nonce):  # 前の確かめ: 今の確かめは残す
        return line_channel.texts(line_channel.FORGET_EXPIRED_TEXT)
    del asks[uid]
    await _forget_all(uid)
    return line_channel.texts(line_channel.FORGOT_TEXT)


def account_link(uid: str, link: dict) -> str:
    """accountLink のイベントの link(result と nonce)で、連携の表に書く。本人に伝える文を返す。
    呼ぶのは、署名を確かめた LINE の webhook と、開発者画面の偽物の LINE(Web の匿名ユーザー)。
    result が ok でなければ nonce を使えなくして、連携しない。nonce が無い・知らない・使い終わった・期限切れ・
    別のユーザーに出した linkToken のものなら、連携しない(dip_mcp.accounts.complete_link)。"""
    nonce = link.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return line_channel.LINK_FAILED_TEXT
    if link.get("result") != "ok":
        accounts.of(state).burn_nonce(nonce)
        return line_channel.LINK_FAILED_TEXT
    r = accounts.of(state).complete_link(uid, nonce)
    if "error" in r:
        line_channel.log.warning("アカウント連携をしませんでした(%s): %s", uid, r["error"])
        return line_channel.LINK_FAILED_TEXT
    profile.link_changed(state, uid, linked=True)
    return line_channel.LINKED_TEXT


def _unlink(uid: str) -> str | None:
    """連携を解除する(連携の表から外す)。未連携の扱いに戻る: 要約を読まず、会員に書いた分も重ねない(behavior.view)。
    会話の途中なら次のターンの前に AI へ知らせる。外した dip_id(連携していなければ None)。"""
    gone = accounts.of(state).unlink(uid)
    if gone:
        profile.link_changed(state, uid, linked=False)
    return gone


async def _line_turn(uid: str, kind: str, e: dict) -> dict | None:
    """Web と同じターンの処理(turn.accept → turn.reply_of)。follow は dip が初回訪問か再訪かを決めて挨拶する。
    テキストは、Web のトーク画面を開いたときと同じ決まり(talk_continues)で、最後のやりとりから 30 分以内なら今の会話に
    続け、それより空いたら(返信のある会話がまだ無いときも)新しい会話を本人の発言から始める(挨拶は挟まない)。
    upload は LIFF 画面から履歴書を読み込んだ本人の 1 ターン(resume_upload。続け方はテキストと同じ決まり)。
    エージェントには LINE の窓口と、LIFF 画面で使えるもの(line_channel.liff_features)を伝える。
    受け付けないイベント(空白だけの発言など)は None(何も返さない)。ターンの途中の失敗は、謝りの文を返す。"""
    rt = app.state.rt
    deps = turn.TurnDeps(state=state, quota=app.state.quota, rt=rt, unread_uploads=app.state.unread_uploads, ip=None)
    if kind == "follow":
        event, text, new = logic.talk_entry(state, uid), None, False
    else:
        async with rt.user_lock(uid):  # 進行中のターンと交差しないように(talk_open と同じ)
            conv = await rt.conversation(uid)
        event, text = ("resume_upload", None) if kind == "upload" else ("message", e["message"].get("text"))
        new = not talk_continues(conv)
    try:
        t = turn.accept(deps, uid, event, text, None, new_conversation=new, channel="line",
                        features=line_channel.liff_features())
    except turn.BadTurn:
        return None
    try:
        reply = await turn.reply_of(deps, t)
    except (turn.BadTurn, turn.TurnFailed) as err:
        line_channel.log.warning("LINE のターンが失敗しました(%s): %s", uid, type(err.__cause__ or err).__name__)
        return {"bubbles": [line_channel.FAILED_TEXT]}
    if reply is not None and t.kind == "greet" and not reply.get("limited"):
        logic.record_talk_open(state, uid)  # 挨拶が返った: 次に友だち追加し直したら再訪
    return reply


# ----------------------------- dip のログイン(アカウント連携。dip の画面の偽物) -----------------------------
# LINE のトークのボタン(と開発者画面)から linkToken つきで開く。ログインできたら dip が nonce を作り、LINE の accountLink の
# 画面に送る(linkToken が偽物の LINE のものなら、このサーバーの /fake-line/… に送る)。会員はクッキーで決めない(dip の会員の
# ログインで決める)。ログインを続けてまちがえると、しばらくログインできない(dip_mcp.accounts)。

LINK_TOKEN_FORM = re.compile(r"[A-Za-z0-9_\-]{8,128}")
FAKE_ACCOUNT_LINK_PATH = "/fake-line/dialog/bot/accountLink"
LOGIN_MESSAGES = {"bad": "ログインID かパスワードがちがいます。",
                  "locked": f"続けてまちがえたので、{accounts.LOCK.seconds // 60} 分ほどログインできません。時間をおいてやり直してください。"}


def _login_form(link_token: str, message: str = "") -> str:
    note = f"<p style='color:#b3261e'>{escape(message)}</p>" if message else ""
    return (f"{note}<p>dip の会員のログインID とパスワードでログインすると、LINE と dip の会員がつながります。</p>"
            f"<form method='post' action='{line_channel.LINK_LOGIN_PATH}'>"
            f"<input type='hidden' name='linkToken' value='{escape(link_token)}'>"
            "<label>ログインID<input name='login_id' autocomplete='username' required></label>"
            "<label>パスワード<input name='password' type='password' autocomplete='current-password' required></label>"
            "<button>ログインして連携する</button></form>")


def _bad_link_page() -> HTMLResponse:
    return HTMLResponse(_page("dip にログイン", "<p>このリンクは使えません。LINE のトークから、もう一度「アカウント連携」と"
                                               "送ってください。</p>", legal_links=False), status_code=400)


@app.get(line_channel.LINK_LOGIN_PATH, response_class=HTMLResponse)
async def dip_login_page(link_token: Annotated[str, Query(alias="linkToken")] = ""):
    if not LINK_TOKEN_FORM.fullmatch(link_token):
        return _bad_link_page()
    return _page("dip にログイン", _login_form(link_token), legal_links=False)


@app.post(line_channel.LINK_LOGIN_PATH)
async def dip_login(link_token: Annotated[str, Form(alias="linkToken")] = "", login_id: Annotated[str, Form()] = "",
                    password: Annotated[str, Form()] = ""):
    """ログインできたら nonce を作って会員と linkToken に結び、LINE の accountLink の画面へ送る(303)。
    できなければ理由を出してもう一度(ID とパスワードのどちらがちがうかは言わない)。"""
    if not LINK_TOKEN_FORM.fullmatch(link_token):
        return _bad_link_page()
    acc = accounts.of(state)
    r = await asyncio.to_thread(acc.login, login_id, password)  # ハッシュの計算は遅いので、ほかの要求を止めない
    if "error" in r:
        return HTMLResponse(_page("dip にログイン", _login_form(link_token, LOGIN_MESSAGES[r["error"]]), legal_links=False),
                            status_code=429 if r["error"] == "locked" else 401)
    nonce = acc.issue_nonce(r["dip_id"], link_token)
    base = FAKE_ACCOUNT_LINK_PATH if app.state.fake_line.knows(link_token) else line_channel.ACCOUNT_LINK_URL
    return RedirectResponse(line_channel.account_link_url(link_token, nonce, base), status_code=303)


# ----------------------------- 開発者画面から試すアカウント連携(偽物の LINE) -----------------------------
# Web の LINE 風の画面(/)の匿名ユーザー(Cookie の匿名ID)を、LINE のときと同じ流れで dip の会員につなぐ。
# bot が LINE に linkToken を頼むところと、accountLink の画面と webhook は偽物の LINE(app.state.fake_line)がする。


@app.get("/api/dev/link")
async def dev_link(request: Request):
    uid = _owner(request)
    return {"user_id": uid, "dip_id": behavior.dip_id(state, uid)}


@app.post("/api/dev/link/start")
async def dev_link_start(request: Request):
    """連携を始める: 偽物の LINE が linkToken を出し、bot がだれに出したかを覚える。開く dip のログインの画面の URL を返す。
    もう連携していれば 409。"""
    uid = _owner(request)
    _admit(request, uid)
    if behavior.dip_id(state, uid):
        raise HTTPException(409, line_channel.ALREADY_LINKED_TEXT)
    token = app.state.fake_line.issue_link_token(uid)
    accounts.of(state).record_link_token(uid, token)
    return {"url": f"{line_channel.LINK_LOGIN_PATH}?{urlencode({'linkToken': token})}"}


@app.post("/api/dev/link/unlink")
async def dev_link_unlink(request: Request):
    uid = _owner(request)
    return {"unlinked": _unlink(uid)}


@app.get(FAKE_ACCOUNT_LINK_PATH, response_class=HTMLResponse)
async def fake_line_account_link(request: Request, link_token: Annotated[str, Query(alias="linkToken")] = "",
                                  nonce: str = ""):
    """偽物の LINE の accountLink の画面。開いた人(Cookie の匿名ID)が linkToken のユーザーなら、LINE が webhook で送る
    accountLink のイベントと同じものを、署名を確かめた後の webhook と同じ処理(account_link)に渡す。"""
    opener = _owner(request)
    event = app.state.fake_line.open_account_link(link_token, nonce, opener)
    if event is None:
        return HTMLResponse(_page("アカウント連携", "<p>このリンクは使えません。</p>", "偽物の LINE", legal_links=False), status_code=404)
    text = account_link(event["source"]["userId"], event["link"])
    return _page("アカウント連携", f"<p>{escape(text)}</p><p><a href='/'>トークに戻る</a></p>", "偽物の LINE",
                 legal_links=False)


# ----------------------------- わたしのプロフィール(覚えていることを見る・直す・消す) -----------------------------
# 本番のトーク画面の下のナビ「プロフィール」。読むのも変えるのも、本人の匿名ID(Cookie)のユーザーだけ
# (パスにユーザーIDは載せない。Cookie が無い・不正なら 401)。中身は dip_mcp.profile。変えたことは次の本人のターンの前に AI へ渡す(turn.profile_notice)。


class ProfileEditIn(BaseModel):
    value: str


def _profile_view(user_id: str) -> dict:
    """画面に出す形。履歴書は要点(トークの確認と同じ作り)と「読み取り結果を見る」、下書きは表示ページの URL を足す。"""
    v = profile.view(state, user_id)
    rec = v.pop("resume")
    v["resume"] = None if rec is None else {
        "uploaded": logic._md(rec["uploaded_at"]),
        "file": rec.get("file") or "", "view_url": logic.liff_url("/liff/resume", user_id, rid=rec.get("id", 0)),
        "highlights": [x for b in resume_bubbles(rec["resume"]) for x in b.removeprefix(RESUME_INTRO).split("\n") if x]}
    for d in v["voice_drafts"]:
        d["view_url"] = logic.liff_url("/liff/voice/draft", user_id, rid=d["id"])
    for d in v["arcade_drafts"]:
        d["view_url"] = arcade.draft_url(user_id, d["id"])
    v["arcade_url"] = arcade.game_url(user_id)  # 「ゲームで書類を作る」(ハブ)
    return v


def _checked(result: dict) -> dict:
    """直せなかった(個人情報らしい値・長すぎる値・知らない項目)ら 400 で理由を返す。"""
    if "error" in result:
        raise HTTPException(404 if result["error"] == "not_found" else 400, result.get("message") or result["error"])
    return result


def _remove_voice_files(files: list[str]) -> None:
    """音声相談で書き出した下書きのファイルを消す。音声の出力先(voice_resume.config.OUT_DIR)の中のものだけ。"""
    from voice_resume import config as voice_config

    root = voice_config.OUT_DIR.resolve()
    for f in files:
        p = Path(f).resolve()
        if p.is_relative_to(root) and p.is_file():
            p.unlink()


@app.get("/api/profile")
async def profile_get(request: Request):
    user_id = _owner(request)
    return _profile_view(user_id)


@app.put("/api/profile/items/{key}")
async def profile_edit(key: str, body: ProfileEditIn, request: Request):
    """項目を本人の言葉で直す。key は stated_preferences の項目か consultation_summary。"""
    user_id = _profile_writer(request)
    if key == "consultation_summary":
        _checked(profile.edit_summary(state, user_id, body.value))
    else:
        _checked(profile.edit_item(state, user_id, key, body.value))
    return _profile_view(user_id)


@app.delete("/api/profile/items/{key}")
async def profile_delete(key: str, request: Request):
    user_id = _profile_writer(request)
    if key == "consultation_summary":
        profile.delete_summary(state, user_id)
    else:
        _checked(profile.delete_item(state, user_id, key))
    return _profile_view(user_id)


class TrackIn(BaseModel):
    track: str


@app.put("/api/profile/track")
async def profile_track(body: TrackIn, request: Request):
    user_id = _profile_writer(request)
    _checked(profile.set_track(state, user_id, body.track))
    return _profile_view(user_id)


@app.delete("/api/profile/track")
async def profile_track_delete(request: Request):
    user_id = _profile_writer(request)
    profile.delete_track(state, user_id)
    return _profile_view(user_id)


@app.delete("/api/profile/resume")
async def profile_resume_delete(request: Request):
    """履歴書の読み取り結果を消し、今の会話を閉じる(それまでの会話の記録をすべて消すので、アップロードの要点・要約の
    結果は画面にも AI にも出ず、サーバーにも残らない)。会話で保存した内容(経験など)は消さない(プロフィールの各項目から
    消せる)。次にトークを開くと、30分以内でも新しい会話(挨拶)から始まる。古い会話を開いたままのタブの発言は
    stale になる。LLM は呼ばず、会話数も使わない。消すものが無ければ会話は閉じない。
    会話を閉じてから読み取り結果を消す(閉じる途中で失敗しても履歴書が残るので、もう一度押せば閉じられる)。"""
    user_id = _profile_writer(request)
    rt = app.state.rt
    async with rt.user_lock(user_id):  # 進行中のターンが終わってから閉じて消す
        if state.user(user_id).get("resume_upload") is not None:
            await rt.close_conversation(user_id)
            profile.delete_resume(state, user_id)
    app.state.unread_uploads.pop(user_id, None)
    return _profile_view(user_id)


@app.delete("/api/profile/voice/{result_id}")
async def profile_voice_delete(result_id: int, request: Request):
    user_id = _profile_writer(request)
    _remove_voice_files(_checked(profile.delete_voice_draft(state, user_id, result_id))["files"])
    return _profile_view(user_id)


@app.delete("/api/profile/arcade/{draft_id}")
async def profile_arcade_delete(draft_id: int, request: Request):
    user_id = _profile_writer(request)
    _checked(profile.delete_arcade_draft(state, user_id, draft_id))
    return _profile_view(user_id)


@app.delete("/api/profile/saved/{job_id}")
async def profile_unsave(job_id: str, request: Request):
    user_id = _profile_writer(request)
    profile.unsave_job(state, user_id, job_id)
    return _profile_view(user_id)


@app.post("/api/profile/forget")
async def profile_forget(request: Request):
    """すべて消す: dip の状態(保存した求人・応募を含むプロフィール、閲覧、トークを開いた記録など)と会話(チェックポイント)を消す。
    次に開くと初回訪問として始まる。会話数の上限の数と費用台帳は消さない(消すと上限を逃れられるため)。
    同じ Cookie の /dev のシナリオ利用者(「匿名ID:シナリオユーザー」)の記録も、本人の記録なので一緒に消す。
    会話を消してから dip の状態を消す(会話を消す途中で失敗すれば何も消えておらず、dip の状態を消すところで失敗しても
    続きを開ける会話は残っていない。どちらも、もう一度押せばやり直せる)。"""
    await _forget_all(_owner(request))
    return {"forgotten": True}


async def _forget_all(anon: str) -> None:
    """すべて消す(Web のプロフィール画面と、LINE の「データを消す」の確かめ)。anon は本人のユーザーID(w-… か l-…)。"""
    rt = app.state.rt
    # 本人と、同じ Cookie の /dev のシナリオ利用者(dip に記録がある人と、シナリオの一覧の人の両方)。
    # どのユーザーも、進行中のターンが終わってから消す
    users = sorted({anon, *(u for u in state.user_ids() if u.startswith(f"{anon}:")),
                    *(f"{anon}:{s}" for s in _scenario_users())})
    async with AsyncExitStack() as locks:
        for uid in users:
            await locks.enter_async_context(rt.user_lock(uid))
        for uid in users:
            await rt.forget(uid)
        out = profile.forget_all(state, anon)
    for uid in users:
        app.state.unread_uploads.pop(uid, None)
    _remove_voice_files(out["files"])


# ----------------------------- 音声相談(voice-resume との連携) -----------------------------
# トークの「話して伝える」/「+」→「音声で相談する」は、同じ画面の上に重ねるシート(talk-core.js)で開く。
# シートは POST /api/voice/start で LiveKit の接続トークンを受け取り、ブラウザ ⇔ LiveKit Cloud ⇔
# 音声ワーカー(voice_resume.agent)で話す。面談が終わるとワーカーが POST /api/voice/result に結果を送り
# (サーバー間の経路なので共有鍵 AICA_INTERNAL_KEY で守る)、ここがそれを今の会話に AI の返信として続ける。
# シートは開いている間だけ POST /api/voice/wait で待ち、届いたら閉じて同じ返信をトークに描く(常時の問い合わせはしない)。
# ユーザーIDは本文で受け取り、URL には載せない。
# 1 人が開ける部屋は同時に 1 つまで(部屋ごとにワーカーのジョブと dip MCP の子プロセスができる。line_runtime.voice_rooms)。
# ワーカーは本人が部屋に入ったとき・ジョブを終えたときに POST /api/voice/session で知らせる(共有鍵つき)。
# 結果(/api/voice/result)が届いただけでは部屋を終わりにしない(結果を送らせた後も、接続したままジョブを持ち続けられるので)。

VOICE_WAIT_MAX = 30.0  # 1回の待ちの上限(秒)。シートはこれを繰り返す
DRAFT_MAX = 20_000  # 下書き(履歴書・職務経歴書)の長さの上限(字)。普通の下書きは数千字


class VoiceWaitIn(BaseModel):
    user_id: str
    since: int
    room: str | None = None
    timeout: float = 25.0


class VoiceResultIn(BaseModel):
    """voice-resume が面談の終わりに送ってくる結果。求人は dip のカードの形(job_id で照合し直す)。
    user_id は「音声で相談」を押したトークのユーザー(匿名ユーザーID、または「匿名ID:シナリオユーザー」)。
    preferences は音声で本人が話した希望(トークに出す「話した内容のまとめ」に使う)。"""
    user_id: str
    jobs: list[dict] = []
    draft_markdown: str = Field("", max_length=DRAFT_MAX)
    files: list[str] = []
    preferences: dict = {}
    room: str | None = None  # 面談の LiveKit の部屋(シートは自分の部屋の結果だけを受け取る)
    document_kind: str | None = None  # 下書きの書類(履歴書 / 職務経歴書)。無いときは履歴書


class VoiceSessionIn(BaseModel):
    """voice-resume のワーカーが知らせる、部屋のジョブの始まり(joined: 本人が入った)と終わり(ended)。"""
    user_id: str
    room: str
    state: Literal["joined", "ended"]


VOICE_BUSY_MESSAGE = ("前の音声相談がまだ終わっていません。前の通話を閉じてから、もう一度お試しください"
                      f"(つながらないまま閉じたときは、{round(JOIN_WINDOW.total_seconds() / 60)} 分ほどで開けます)。")


def _voice_ready(user_id: str) -> asyncio.Condition:
    """そのユーザーの音声相談の結果が届いたことを、待っているシートへ知らせる。"""
    return app.state.voice_ready.setdefault(user_id, asyncio.Condition())


@app.post("/api/voice/start")
async def voice_start(body: UserIn, request: Request):
    """音声相談のシートを開いた: LiveKit の部屋に入る接続トークンを発行する(本人のブラウザにだけ)。
    音声セッションも実 API を使うので、会話数を1ターン使う(上限なら発行せず 429)。トークンの有効期限は数分
    (同じトークンで入り直して、会話数を使わずに音声セッションを重ねられないように)。
    since は、このシートが待つ結果の番号(開く前に届いていた結果は受け取らない)。
    その人(シナリオユーザーも匿名IDにまとめる)に終わっていない部屋があれば、会話数を使わずに 409
    (1 人が同時にいくつも部屋を開いて、ワーカーのジョブを持ち続けられないように)。"""
    _require_own(request, body.user_id)
    _voice_ready_or_503()
    return _voice_token(body.user_id, _client_ip(request))


VOICE_NOT_READY = "音声相談の準備ができていません(LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET)"


def _voice_ready_or_503() -> None:
    """LiveKit の設定が無ければ 503(会話数を使う前に断る)。"""
    from voice_resume.livekit_token import configured

    if not configured():
        raise HTTPException(503, VOICE_NOT_READY)


def _voice_token(user_id: str, ip: str | None) -> dict:
    """終わっていない部屋があれば会話数を使わずに 409。会話数を 1 つ使い(上限なら 429)、LiveKit の部屋の接続トークンを発行する。
    Web と LIFF 画面で共通。"""
    from voice_resume.livekit_token import issue_token

    rooms = app.state.voice_rooms
    if rooms.open_room(user_id) is not None:  # 確かめてから記録するまで await を挟まない(同時の呼び出しも 1 つだけ通る)
        raise HTTPException(409, VOICE_BUSY_MESSAGE)
    limited = app.state.quota.try_take(user_id, ip)
    if limited is not None:
        raise HTTPException(429, limited)
    retention.touch(state, user_id)  # 音声相談を始めたのも最後の利用
    token = issue_token(user_id)
    rooms.issue(user_id, token["room"])
    logic.record_voice_room(state, user_id, token["room"])  # 「すべて消す」の前に始めた面談かを、結果が届いたときに見分ける
    return token | {"since": logic.voice_result_count(state, user_id)}


@app.post("/api/voice/wait")
async def voice_wait(body: VoiceWaitIn, request: Request):
    """音声相談のシートが開いている間だけ呼ぶ: since 番以降に届いた、この部屋(room。/start が返す)の結果を
    最大 timeout 秒待って返す(届かなければ空)。results は今の会話に続けた返信と同じ形(画面は renderReply で描く)。
    next は次に待つときの since(この部屋でない結果も読み飛ばす。そちらは会話に入っているので再読み込みで出る)。"""
    _require_own(request, body.user_id)
    return await _voice_wait(body.user_id, body)


async def _voice_wait(user_id: str, body: VoiceWaitIn) -> dict:
    """since 番以降に届いた、この部屋の結果を最大 timeout 秒待って返す(Web と LIFF 画面で共通)。"""
    since, room = body.since, body.room
    # NaN(本文の `NaN` は json.loads が通す)は max/min の丸めをすり抜けて wait_for → loop.call_at に届き、プロセスで共有する
    # タイマーの山の順序を壊すので断る。モデルの検証にしないのは、既定の 422 が入力の nan を JSON にできず 500 になるため
    if math.isnan(body.timeout):
        raise HTTPException(422, "timeout は数値で指定してください")
    timeout = min(max(body.timeout, 0.0), VOICE_WAIT_MAX)  # Infinity は上限、負と -Infinity は 0
    cond = _voice_ready(user_id)
    async with cond:
        try:
            await asyncio.wait_for(
                cond.wait_for(lambda: bool(logic.voice_results_since(state, user_id, since, room))), timeout)
        except TimeoutError:
            pass
    results = logic.voice_results_since(state, user_id, since, room)
    return {"results": [voice_reply(user_id, r) for r in results],
            "next": max([since] + [r["id"] + 1 for r in logic.voice_results_since(state, user_id, since)])}


@app.post("/api/voice/result")
async def voice_result_in(body: VoiceResultIn, request: Request, background: BackgroundTasks):
    """音声アドバイザーの結果(話した希望・提案した求人・履歴書か職務経歴書の下書き)を預かり、今の会話に AI の返信として
    続ける(再読み込みしても残る)。待っているシートがあれば知らせる。
    呼ぶのは voice-resume のワーカー(サーバー間の経路)。Cookie ではなく共有鍵(AICA_INTERNAL_KEY)で守る
    (匿名IDを知っているだけでは、他人の会話に結果のターンを足せない)。
    面談の途中で本人が「すべて消す」をしていたら(その前に発行した部屋の結果)、預からずに 410 を返す
    (消した後に dip の記録を作り直さない。ワーカーは 410 を見て、会話の結果も記録しない)。
    LINE の LIFF 画面から始めた面談(部屋が line_voice_rooms にある)なら、同じ返信をその本人の LINE のトークに push する
    (応答の後に送る。out["line"] は送ることにしたか)。"""
    _require_internal(request)
    if logic.voice_room_forgotten(state, body.user_id, body.room):
        _line_voice_to(body.room)  # 届けない宛先は忘れる
        raise HTTPException(410, "この面談の前に、本人が覚えていることをすべて消しました")
    retention.touch(state, body.user_id)  # 音声相談の結果も最後の利用(期間で消す掃除が数える)
    out = logic.record_voice_result(state, body.user_id, body.jobs, body.draft_markdown, body.files, body.preferences,
                                    body.room, body.document_kind)
    rec = logic.voice_results_since(state, body.user_id, out["recorded"])[0]
    track = logic.search_track(state.user(body.user_id))  # 音声で区分が切り替わっていれば、次のターンの AI に伝える
    reply = voice_reply(body.user_id, rec, track)
    out["talk"] = await app.state.rt.add_voice_result(body.user_id, reply)
    to = _line_voice_to(body.room)  # 記録してから取り出す(途中で落ちてワーカーが送り直しても、宛先が残る)
    out["line"] = to is not None and line_channel.line_user(to[0]) == body.user_id and line_channel.configured()
    if out["line"]:
        background.add_task(_line_push, body.user_id, to[0], reply)
    cond = _voice_ready(body.user_id)
    async with cond:
        cond.notify_all()
    return out


@app.post("/api/voice/session")
async def voice_session(body: VoiceSessionIn, request: Request):
    """音声ワーカーが、部屋のジョブの始まり(joined: 本人が入った)と終わり(ended)を知らせる(サーバー間の経路。共有鍵で守る)。
    joined の部屋は、終わりの知らせが届くまで、その人の終わっていない部屋として数える(line_runtime.voice_rooms)。
    その人に別の部屋があれば allowed: false(ワーカーはそのジョブを止める。古い接続トークンで入り直した部屋など)。"""
    _require_internal(request)
    rooms = app.state.voice_rooms
    if body.state == "joined":
        return {"allowed": rooms.joined(body.user_id, body.room)}
    rooms.release(body.user_id, body.room)
    return {"allowed": True}


@app.get("/liff/voice/draft", response_class=HTMLResponse)
async def liff_voice_draft(rid: int, request: Request, scenario_user: str | None = None):
    """音声相談で作った下書きを表示する(トークの「履歴書の下書きを見る」「職務経歴書の下書きを見る」のリンク先)。
    題は下書きの書類(document_kind)に合わせる。本人は Cookie から決める(URL にユーザーIDは無い)。"""
    return _voice_draft_page(_page_user(request, scenario_user), rid)


def _voice_draft_page(user: str, rid: int) -> HTMLResponse | str:
    r = logic.voice_result(state, user, rid)
    if r is None or not r.get("draft_markdown"):
        return HTMLResponse(_page("下書き", "<p>この下書きは見つかりません。</p>"), status_code=404)
    kind = document_kind_of(r)
    body = (f"<p>音声の相談から作った{kind}の下書きです。内容は必ずご自身で確認・修正してください。</p>"
            f"<pre style='white-space:pre-wrap;background:#fff;border-radius:10px;padding:12px;font-size:13px'>"
            f"{escape(r['draft_markdown'])}</pre>")
    return _page(f"{kind}の下書き", body)


# ----------------------------- 履歴書のアップロード(resume-intake) -----------------------------


def resume_client():
    """履歴書の読み取りに使う OpenAI のクライアント(テストでは固定応答のスタブに差し替える)。"""
    from openai import AsyncOpenAI

    return AsyncOpenAI()


@app.post("/api/resume/upload")
async def resume_upload(request: Request, user_id: Annotated[str, Form()], file: Annotated[UploadFile, File()],
                        thread: int | None = None):
    """トークからアップロードされた履歴書(PDF / JPEG / PNG)を読み取り、そのユーザーの dip の状態に保存する。
    ファイルはディスクに置かず、ファイル名も保存しない。personal は常に空欄、文字列中の電話・メール・郵便番号は除く。
    保存した結果は get_user_conversation_summary の uploaded_resume として、同じユーザーの会話から参照される。
    アップロードは本人の1ターン: 画面は本人側に「履歴書(PDF)」の吹き出しを出し、ここが返す確認を AI 側に出してから、
    /api/event/stream の resume_upload で会話を続ける(定型文を本人の発言として送らない)。
    thread: 本番のトーク画面が表示している会話の番号。別のタブで会話が替わっていたら、読み取らず会話数も使わずに 409。"""
    _require_own(request, user_id)
    return await _read_resume(user_id, file, _client_ip(request), thread)


async def _read_resume(user_id: str, file: UploadFile, ip: str | None, thread: int | None = None) -> dict:
    """履歴書を読み取って dip に保存し、トークに出す確認を返す(Web のトークと LIFF 画面で共通)。会話数を 1 つ使う。"""
    name = file.filename or ""
    try:
        label = RESUME_LABEL[media_type(name)]
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    data = await file.read(resume_config.MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(400, "ファイルが空です")
    if len(data) > resume_config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"ファイルが大きすぎます({resume_config.MAX_UPLOAD_BYTES // (1024 * 1024)}MB まで)")
    if thread is not None and await app.state.rt.current_thread(user_id) != thread:
        raise HTTPException(409, "別の画面で新しい会話が始まっています")
    limited = app.state.quota.try_take(user_id, ip)
    if limited is not None:  # アップロードも1ターン扱い: 上限を超えたら読み取らずに案内だけ返す
        raise HTTPException(429, limited)
    retention.touch(state, user_id)
    try:
        draft, usage = await extract_bytes(name, data, resume_client())
    except Exception as e:  # API のエラーは画面に理由を出す
        raise HTTPException(502, f"履歴書を読み取れませんでした: {type(e).__name__}") from e
    app.state.rt.ledger.record(user_id, "resume_upload", resume_config.MODEL,
                               {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens})
    result = finalize(draft)
    empty = draft.model_dump(exclude={"unconfirmed"}) == type(draft)().model_dump(exclude={"unconfirmed"})
    if empty:  # 読めなかったファイルで、前に読み取れた結果を上書きしない
        bubbles = resume_bubbles(result["resume"], empty=True)
        # 画面は続けて resume_upload のイベントで会話を進める(AI が読み取れなかった前提で1問返す)
        app.state.unread_uploads[user_id] = {"file": label, "shown": bubbles, "empty": True}
        return {"result": result, "empty": True, "view_url": None, "file": label, "bubbles": bubbles}
    app.state.unread_uploads.pop(user_id, None)
    rec = logic.save_resume_upload(state, user_id, result, file=label)
    # 直前にプロフィールで履歴書を消していても、その知らせは渡さない(新しい履歴書まで「使わない」と読まれないように)
    profile.resume_uploaded(state, user_id)
    # トークに出す確認は要点だけ(80字以内の吹き出し)。全文は view_url の「読み取り結果を見る」で見せる。
    # 画面は続けて resume_upload のイベントで会話を進める(AI が確認を踏まえて次の1問を返す)
    return {"result": rec, "empty": False, "view_url": logic.liff_url("/liff/resume", user_id, rid=rec.get("id", 0)),
            "file": label, "bubbles": resume_bubbles(rec["resume"])}


@app.get("/liff/resume", response_class=HTMLResponse)
async def liff_resume(rid: int, request: Request, scenario_user: str | None = None):
    """アップロードした履歴書の読み取り結果を表示する(トークの「読み取り結果を見る」のリンク先)。
    本人は Cookie から決める(URL にユーザーIDは無い)。rid はそのターンでアップロードした読み取り結果の番号:
    保存しているのは最新の1件だけなので、あとで別の履歴書をアップロードしていたら、古いターンのリンクは
    新しい結果を開かずに、置き換わったことを伝える。"""
    return _resume_page(_page_user(request, scenario_user), rid)


def _resume_page(user: str, rid: int) -> HTMLResponse | str:
    rec = logic.resume_upload(state, user)
    if rec is None:
        return HTMLResponse(_page("履歴書の読み取り結果", "<p>読み取った履歴書はありません。</p>"), status_code=404)
    if rec.get("id", 0) != rid:
        return HTMLResponse(_page("履歴書の読み取り結果", "<p>この読み取り結果は、あとからアップロードした履歴書で"
                                  "置き換わりました。最新の読み取り結果は、そのアップロードのリンクから見られます。</p>"),
                            status_code=404)
    return _page("履歴書の読み取り結果", _resume_html(rec["resume"]))


def _resume_html(r: dict) -> str:
    """読み取り結果(resume_draft.schema.ResumeDraft の形)を読める HTML に。書いてある項目だけ出す。"""
    def line(*parts: str) -> str:
        return escape(" ".join(x for x in parts if x))

    pref = r["preferences"]
    sec: list[tuple[str, list[str]]] = [  # 各行はエスケープ済みの HTML
        ("職務要約", [line(r["job_summary"])] if r["job_summary"] else []),
        ("自己PR", [line(r["self_pr"])] if r["self_pr"] else []),
        ("学歴", [line(e["period"], e["school"], e["status"]) for e in r["education"]]),
        ("職歴", [line(w["period"], w["organization"], w["employment_type"], w["role"])
                  + "".join(f"<br>・{escape(d)}" for d in w["duties"] + w["achievements"]) for w in r["work_history"]]),
        ("その他の経験", [line(o["kind"], o["title"], o["description"]) for o in r["other_experience"]]),
        ("スキル", [line(x) for x in r["skills"]]),
        ("資格・免許", [line(c["name"], c["acquired"]) for c in r["certifications"]]),
        ("希望", [line(x) for x in pref["desired_work"] + pref["conditions"] + pref["notes"]]),
        ("読み取れなかった点", [line(x) for x in r["unconfirmed"]]),
    ]
    rows = "".join(
        f"<h3 style='font-size:14px;margin:14px 0 4px'>{t}</h3><ul style='margin:0;padding-left:18px;font-size:13px'>"
        + "".join(f"<li>{v}</li>" for v in items) + "</ul>"
        for t, items in sec if items)
    return ("<p>アップロードした履歴書から読み取った内容です。氏名・住所・電話番号・メールアドレスは保存していません。"
            "内容は必ずご自身で確認してください。</p>" + (rows or "<p>読み取れた項目はありませんでした。</p>"))


# ----------------------------- LIFF(dip の応募フォーム画面のモック) -----------------------------

@app.get("/liff/apply", response_class=HTMLResponse)
async def liff_form(job: str, request: Request, scenario_user: str | None = None):
    """入力途中の応募フォームの続き。本人は Cookie から決める(URL にユーザーIDは無い。dip_mcp.logic.liff_url)。"""
    return _apply_form_page(_page_user(request, scenario_user), job, scenario_user)


# 応募フォームの体験(求人は架空のデモ。docs/legal/terms.md の第 1 条)。欄は残し、架空の値を最初から入れておく。
# 本物の情報を入れないよう画面に書き、送られた値は保存しない(docs/legal/privacy-policy.md の 2)。
DEMO_FORM_VALUES = {"氏名": "山田 太郎", "電話番号": "090-0000-0000", "メールアドレス": "taro@example.com",
                    "住所": "みほん市みほん町1-2-3", "生年月日": "2000-01-01", "年齢": "26", "希望開始日": "すぐに"}
DEMO_FORM_OTHER = "架空の値"  # 上に無い項目
APPLY_DEMO_NOTE = ("架空の求人のデモです。応募しても、実際の応募にはなりません。欄には架空の値を入れてあります。"
                   "本物の情報は入れないでください。送った値は保存しません。")
APPLY_DONE_TEXT = "応募の体験を終えました。架空の求人なので、実際の応募にはなりません。店舗から連絡は来ません。"


def _apply_form_page(user: str, job: str, scenario_user: str | None = None) -> HTMLResponse | str:
    j = load_jobs().get(job)
    exit_ = logic.form_exit(state, user)
    if j is None or exit_ is None or exit_["job_id"] != job:
        return HTMLResponse(_page("応募フォーム", "<p>入力途中の応募はありません。</p>"), status_code=404)
    fields = "".join(
        f'<label>{escape(f)}<input name="f_{escape(f)}" required value="{escape(DEMO_FORM_VALUES.get(f, DEMO_FORM_OTHER))}">'
        "</label>" for f in exit_["remaining_fields"])
    body = (f"<p class='demo'>{escape(APPLY_DEMO_NOTE)}</p>"
            f"<p class='job'>{escape(j.title)}<br><small>{escape(j.employer)}</small></p>"
            f"<p>入力済み: {escape('、'.join(exit_['completed_fields']))}</p>"
            f"<form method='post'><input type='hidden' name='scenario_user' value='{escape(scenario_user or '')}'>"
            f"<input type='hidden' name='job' value='{escape(job)}'>{fields}<button>応募する</button></form>")
    return _page("応募の続き", body)


@app.post("/liff/apply", response_class=HTMLResponse)
async def liff_submit(request: Request, job: str = Form(...), scenario_user: str = Form("")):
    return _apply_submit(_page_user(request, scenario_user or None), job)


def _apply_submit(user: str, job: str) -> HTMLResponse | str:
    exit_ = logic.form_exit(state, user)
    if exit_ is None or exit_["job_id"] != job:  # 入力途中の応募が無ければ確定しない(状態にも書かない)
        return HTMLResponse(_page("応募フォーム", "<p>入力途中の応募はありません。</p>"), status_code=404)
    # 入力値は受け取っても読まない・保存しない(架空のデモ)。応募の確定だけを dip の状態に反映する。
    logic.submit_application(state, user, job)
    return _page("応募の体験", f"<p>{escape(APPLY_DONE_TEXT)}</p><p>このタブを閉じてトークに戻れます。</p>")


# ----------------------------- LINE のアプリで開く LIFF 画面(/liff/line/…) -----------------------------
# LIFF アプリのエンドポイント URL は <公開 URL>/liff/line。LINE のボタン(line_channel.liff_url_of)から開くと、LINE は
# /liff/line/<画面>?… を開く。ここでは Cookie の匿名ユーザーID(aica_uid)を見ない: 本人は LIFF の ID トークンを LINE に
# 確かめてもらった LINE のユーザー(l-…。トークと同じ line_user)だけ。確かめた本人は署名つきの Cookie(aica_liff)で覚え、
# Cookie が無い・期限切れなら、まず LIFF SDK で ID トークンを取ってくる画面(_liff_shell)を返す。LIFF の設定が無ければ 404。

LIFF_COOKIE = "aica_liff"
LIFF_SDK = "https://static.line-scdn.net/liff/edge/2/sdk.js"
LIFF_FAILED_TEXT = "LINE で本人を確かめられませんでした。LINE のトークから、もう一度開いてください。"


class LiffSessionIn(BaseModel):
    id_token: str = Field(..., max_length=line_channel.MAX_ID_TOKEN_CHARS)


def _liff_page_user(request: Request) -> str | None:
    """LINE のアプリで開いた画面の本人(Cookie aica_liff の l-…)。無い・不正・期限切れなら None。LIFF の設定が無ければ 404。"""
    if not line_channel.liff_configured():
        raise HTTPException(404)
    return line_channel.liff_session_user(request.cookies.get(LIFF_COOKIE))


def _liff_shell(title: str) -> HTMLResponse:
    """LIFF SDK で ID トークンを取り、POST /api/liff/session で本人を確かめてから、同じ画面を読み直す。
    中身(本人のデータ)は載せない。確かめられなければ、そう出して止まる(匿名ユーザーには切り替えない)。
    URL に liff.state があるとき(LINE が最初にエンドポイント URL を開いたとき)は、liff.init が本来の画面へ送る。"""
    conf = json.dumps({"liffId": line_channel.liff_id(), "failed": LIFF_FAILED_TEXT}, ensure_ascii=False).replace("<", "\\u003c")
    body = f"""<p id="liff-status">LINE で本人を確かめています…</p>
<script src="{LIFF_SDK}" charset="utf-8"></script>
<script>
(async () => {{
  const conf = {conf};
  const status = document.getElementById("liff-status");
  const fail = (msg) => {{ status.textContent = msg || conf.failed; }};
  if (!window.liff) return fail("LINE の画面の部品を読み込めませんでした。通信を確かめて、もう一度開いてください。");
  try {{
    await liff.init({{ liffId: conf.liffId, withLoginOnExternalBrowser: true }});
  }} catch (e) {{
    return fail();
  }}
  if (new URLSearchParams(location.search).has("liff.state")) {{  // liff.init が本来の画面へ送る
    setTimeout(() => fail(), 10000);  // 送られないまま止まらない
    return;
  }}
  // ログインや読み直しから、10 秒以内にまたこの画面に戻ったら、繰り返さずに止める
  const key = "aica-liff-retry:" + location.pathname + location.search;
  const retried = Date.now() - Number(sessionStorage.getItem(key) || 0) < 10000;
  if (!liff.isLoggedIn()) {{
    if (retried) return fail();
    sessionStorage.setItem(key, String(Date.now()));
    return liff.login({{ redirectUri: location.href }});
  }}
  const idToken = liff.getIDToken();
  if (!idToken) return fail();
  let r;
  try {{
    r = await fetch("/api/liff/session", {{
      method: "POST", credentials: "same-origin",
      headers: {{ "Content-Type": "application/json" }}, body: JSON.stringify({{ id_token: idToken }}),
    }});
  }} catch (e) {{
    return fail();
  }}
  if (!r.ok) return fail();
  if (retried) return fail();  // 読み直しても Cookie が付かず、またこの画面になった
  sessionStorage.setItem(key, String(Date.now()));
  location.reload();
}})();
</script>"""
    return HTMLResponse(_page(title, body), headers={"Cache-Control": "no-store"})


def _liff_unauthorized(title: str) -> HTMLResponse:
    return HTMLResponse(_page(title, f"<p>{escape(LIFF_FAILED_TEXT)}</p>"), status_code=401)


@app.post("/api/liff/session")
async def liff_session(body: LiffSessionIn, request: Request):
    """LIFF の ID トークンを LINE の検証の API で確かめ(署名・aud・期限)、sub からトークと同じ LINE のユーザーID(l-…)を作って、
    その本人の Cookie(aica_liff。ブラウザを閉じるまで、長くても 1 時間)を返す。確かめられなければ 401 で、前の Cookie も消す(匿名ユーザーにも切り替えない)。
    本文は JSON だけ(ほかのサイトのフォームから、別人のトークンで本人を入れ替えさせないように。JSON は事前の確認が要る)。"""
    if not line_channel.liff_configured():
        raise HTTPException(404)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON で送ってください")
    sub = await line_login().verify(body.id_token)
    if sub is None:
        resp = JSONResponse({"detail": LIFF_FAILED_TEXT}, status_code=401)
        resp.delete_cookie(LIFF_COOKIE, secure=True, httponly=True, samesite="lax")
        return resp
    resp = JSONResponse({"ok": True})
    # ブラウザを閉じたら消える Cookie(max_age なし)。開いている間も、値の期限(1 時間)を過ぎたら確かめ直す
    resp.set_cookie(LIFF_COOKIE, line_channel.liff_session(line_channel.line_user(sub)),
                    secure=True, httponly=True, samesite="lax")
    return resp


@app.get(line_channel.LIFF_PREFIX, response_class=HTMLResponse)
@app.get(f"{line_channel.LIFF_PREFIX}/", response_class=HTMLResponse, include_in_schema=False)
async def liff_line_entry(request: Request):
    """LIFF アプリのエンドポイント URL。LINE はまずここを liff.state つきで開き、liff.init が本来の画面へ送る。"""
    user = _liff_page_user(request)
    if user is None or "liff.state" in request.query_params:
        return _liff_shell("LINE")
    return _page("LINE", "<p>LINE で本人を確かめました。このタブを閉じてトークに戻れます。</p>")


@app.get(f"{line_channel.LIFF_PREFIX}/resume", response_class=HTMLResponse)
async def liff_line_resume(rid: int, request: Request):
    user = _liff_page_user(request)
    return _liff_shell("履歴書の読み取り結果") if user is None else _resume_page(user, rid)


@app.get(f"{line_channel.LIFF_PREFIX}/voice/draft", response_class=HTMLResponse)
async def liff_line_voice_draft(rid: int, request: Request):
    user = _liff_page_user(request)
    return _liff_shell("下書き") if user is None else _voice_draft_page(user, rid)


@app.get(f"{line_channel.LIFF_PREFIX}/apply", response_class=HTMLResponse)
async def liff_line_form(job: str, request: Request):
    user = _liff_page_user(request)
    return _liff_shell("応募の続き") if user is None else _apply_form_page(user, job)


@app.post(f"{line_channel.LIFF_PREFIX}/apply", response_class=HTMLResponse)
async def liff_line_submit(request: Request, job: str = Form(...)):
    """LINE のアプリで開いた応募フォームの送信。本人の Cookie が無い・期限切れなら 401(確定しない)。"""
    user = _liff_page_user(request)
    return _liff_unauthorized("応募フォーム") if user is None else _apply_submit(user, job)


# ----------------------------- LINE のトークから LIFF 画面で使う操作(task 204) -----------------------------
# 返信の actions は、LINE では LIFF 画面へのボタンになる(line_channel.LIFF_ACTIONS)。画面は LIFF の本人(Cookie aica_liff)で開き、
# 操作を始める API(音声相談の開始・履歴書の読み込み)は、LIFF の ID トークンをもう一度確かめて、結果を push する LINE の宛先(sub)を
# 受け取る。sub は届けるまでメモリにだけ置き、保存もログもしない。結果はその本人の LINE のトークに push で届く。

LINE_VOICE_ROOM_SECONDS = 3600  # LIFF から始めた音声相談の部屋の宛先を覚えておく長さ(面談は長くて 10 分。結果が来なければ忘れる)
LIFF_SENT_TEXT = "まとめを LINE のトークに送りました。トークに戻って続きを話せます。"
LIFF_LATER_TEXT = "まとめができたら、LINE のトークに届きます。トークに戻ってお待ちください。"
LIFF_SHOWN_TEXT = "まとめをこの画面に出しました。"  # Messaging API の設定が無い(LINE のトークには送れない)
LIFF_NOT_STARTED_TEXT = "音声相談を始められませんでした。もう一度お試しください。"
LIFF_IN_TALK_TEXT = "求人のくわしい話は、LINE のトークで聞けます。"


class LiffVoiceStartIn(BaseModel):
    user_id: str
    id_token: str = Field(..., max_length=line_channel.MAX_ID_TOKEN_CHARS)


def _liff_api_user(request: Request, user_id: str) -> str:
    """LIFF 画面から呼ぶ API の本人(Cookie aica_liff の l-…)。Cookie が無い・期限切れなら 401、本文のユーザーが違えば 403。
    LIFF の設定が無ければ 404。"""
    user = _liff_page_user(request)
    if user is None:
        raise HTTPException(401, LIFF_FAILED_TEXT)
    if user_id != user:
        raise HTTPException(403, "この LINE のユーザーではありません")
    return user


async def _liff_sub(user: str, id_token: str) -> str:
    """LIFF の ID トークンを LINE に確かめてもらい、本人(user)の LINE の userId(push の宛先)を返す。
    確かめられない・別の人のトークンなら 401。"""
    sub = await line_login().verify(id_token)
    if sub is None or line_channel.line_user(sub) != user:
        raise HTTPException(401, LIFF_FAILED_TEXT)
    return sub


def _joined(first: dict, then: dict | None) -> dict:
    """2 つの返信を、1 回の push で送る 1 つの返信にする(先の吹き出しとボタンの後に、続きの返信)。"""
    then = then or {}
    return {"bubbles": [*first.get("bubbles", []), *then.get("bubbles", [])], "cards": then.get("cards") or [],
            "buttons": [*first.get("buttons", []), *then.get("buttons", [])], "actions": then.get("actions") or [],
            "quick_replies": then.get("quick_replies") or []}


async def _line_push(uid: str, to: str, reply: dict) -> None:
    """返信(render の形)を、LINE のトークに push する。その人の webhook のイベントと同じ順番待ちに並ぶ。"""
    async with app.state.line_locks[uid], line_api() as api:
        if await api.push(to, line_channel.to_messages(reply)) != line_channel.OK:
            line_channel.log.warning("LINE のトークに結果を届けられませんでした(%s)", uid)


async def _line_upload_turn(uid: str, to: str, shown: dict) -> None:
    """LIFF 画面で読み込んだ履歴書の続き: 読み取り結果の確認(shown)と、エージェントの続きの返事(resume_upload のターン)を、
    1 回の push で LINE のトークに届ける。ターンが受け付けられなければ確認だけ届ける。"""
    async with app.state.line_locks[uid], line_api() as api:
        await api.loading(to)
        try:
            reply = await _line_turn(uid, "upload", {})
        except Exception as err:  # noqa: BLE001  確認だけでも届ける
            # 型名だけ(トレースバックの最後の行の例外の文に、LLM の API のエラーの文が入りうる)
            line_channel.log.error("履歴書の続きのターンを進められませんでした(%s): %s", uid, type(err).__name__)
            reply = {"bubbles": [line_channel.FAILED_TEXT]}
        if await api.push(to, line_channel.to_messages(_joined(shown, reply))) != line_channel.OK:
            line_channel.log.warning("LINE のトークに履歴書の続きを届けられませんでした(%s)", uid)


@app.post("/api/liff/voice/start")
async def liff_voice_start(body: LiffVoiceStartIn, request: Request):
    """LIFF 画面(/liff/line/voice)の音声相談を始める: Web の /api/voice/start と同じく接続トークンを発行し(会話数を 1 つ使う)、
    部屋 → LINE の宛先を覚える(面談の結果を、その本人の LINE のトークに push するため)。"""
    user = _liff_api_user(request, body.user_id)
    _voice_ready_or_503()
    to = await _liff_sub(user, body.id_token)
    token = _voice_token(user, None)  # LINE のユーザーは IP ごとには数えない
    _forget_old_voice_rooms()
    app.state.line_voice_rooms[token["room"]] = (to, time.monotonic())
    return token


def _forget_old_voice_rooms() -> None:
    """LINE_VOICE_ROOM_SECONDS より前に始めた音声相談の部屋の宛先を忘れる(結果が来なかった面談の LINE の userId を持ち続けない)。"""
    rooms, now = app.state.line_voice_rooms, time.monotonic()
    for room in [r for r, (_, at) in rooms.items() if now - at > LINE_VOICE_ROOM_SECONDS]:
        del rooms[room]


def _line_voice_to(room: str | None) -> tuple[str, float] | None:
    """LIFF から始めた音声相談の部屋の宛先を取り出す(取り出したら忘れる。古いものは先に忘れる)。無ければ None。"""
    _forget_old_voice_rooms()
    return app.state.line_voice_rooms.pop(room, None) if room else None


def _liff_reply(r: dict) -> dict:
    """LIFF 画面に描く返信: ボタン(/liff/…)を LIFF の本人のまま開く /liff/line/… にする(Web の画面は Cookie の匿名ユーザーなので開けない)。"""
    return r | {"buttons": [b | {"url": line_channel.LIFF_PREFIX + b["url"].removeprefix("/liff")} for b in r.get("buttons", [])]}


@app.post("/api/liff/voice/wait")
async def liff_voice_wait(body: VoiceWaitIn, request: Request):
    """LIFF 画面の音声相談のシートが開いている間だけ呼ぶ(Web の /api/voice/wait と同じ)。"""
    out = await _voice_wait(_liff_api_user(request, body.user_id), body)
    return out | {"results": [_liff_reply(r) for r in out["results"]]}


@app.post("/api/liff/resume/upload")
async def liff_resume_upload(background: BackgroundTasks, request: Request, user_id: Annotated[str, Form()],
                             id_token: Annotated[str, Form(max_length=line_channel.MAX_ID_TOKEN_CHARS)],
                             file: Annotated[UploadFile, File()]):
    """LIFF 画面(/liff/line/resume/upload)から履歴書を読み込む: Web の /api/resume/upload と同じく読み取って保存し(会話数を 1 つ使う)、
    応答の後に、確認とエージェントの続きの返事(resume_upload のターン。会話数をもう 1 つ使う)を、その本人の LINE のトークに push する。
    画面には確認だけを返す(talk: LINE のトークに続きを送るか。Messaging API の設定が無ければ送らない)。"""
    user = _liff_api_user(request, user_id)
    to = await _liff_sub(user, id_token)
    out = await _read_resume(user, file, None)
    view = line_channel.liff_url_of(out["view_url"]) if out["view_url"] else None
    talk = line_channel.configured()
    if talk:
        shown = {"bubbles": out["bubbles"], "buttons": [{"label": "読み取り結果を見る", "url": out["view_url"]}] if view else []}
        background.add_task(_line_upload_turn, user, to, shown)
    else:
        line_channel.log.warning("LINE の設定が無いので、履歴書の続きを LINE のトークに送りません")
    return {"empty": out["empty"], "bubbles": out["bubbles"], "file": out["file"], "view_url": view, "talk": talk}


def _liff_conf(user: str, **extra: str) -> str:
    """LIFF 画面の script に渡す値(LIFF ID・本人の aica のユーザーID・文言)。LINE の userId は載せない。"""
    conf = {"liffId": line_channel.liff_id(), "user": user, "failed": LIFF_FAILED_TEXT} | extra
    return json.dumps(conf, ensure_ascii=False).replace("<", "\\u003c")


LIFF_SDK_MISSING = "LINE の画面の部品を読み込めませんでした。通信を確かめて、もう一度開いてください。"
TALK_VARS = """:root { --room: #8fa9d3; --head: #2f3a4f; --me: #95e05b; --bot: #fff; --text: #1a1d23; --sub: #5f6775; --faint: #8a93a3;
  --chip: #1f5fd1; --chip-line: #c9d6ee; --ok: #06c755; --ok-dark: #05a346; --ok-soft: #e7f8ee; --bg: #e4e8ef; --line: #e3e7ee; --soft: #f3f5f8; }"""


def _liff_voice_page(user: str) -> HTMLResponse:
    """LINE のアプリで開く音声相談の画面。Web のトークと同じ音声相談のシート(talk-core.js)を、LIFF の API で開く。
    面談のまとめは、この画面と LINE のトークの両方に出る(トークへは /api/voice/result が push する)。"""
    talk = line_channel.configured()  # LINE のトークにも届くか(Messaging API の設定があるか)
    conf = _liff_conf(user, sent=LIFF_SENT_TEXT if talk else LIFF_SHOWN_TEXT, later=LIFF_LATER_TEXT if talk else "",
                      notStarted=LIFF_NOT_STARTED_TEXT, inTalk=LIFF_IN_TALK_TEXT, sdk=LIFF_SDK_MISSING)
    html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>音声で相談</title>
<link rel="stylesheet" href="/static/talk-core.css">
<style>{TALK_VARS}
body{{margin:0;font-family:-apple-system,"Hiragino Sans",sans-serif;background:var(--room);color:var(--text)}}
header{{background:#06c755;color:#fff;padding:12px 16px;font-weight:700}}main{{padding:16px;max-width:480px;margin:auto}}
.intro{{background:#fff;border-radius:10px;padding:12px;font-size:14px;line-height:1.6}}
.go{{width:100%;margin:12px 0;padding:12px;border:0;border-radius:8px;background:#06c755;color:#fff;font-size:16px;font-weight:700}}
.go[hidden]{{display:none}}#liff-status{{color:#fff;font-size:13px}}
#log{{display:flex;flex-direction:column;gap:6px}}.row{{display:flex;gap:8px;align-items:flex-end}}
.avatar{{width:32px;height:32px;border-radius:50%;background:#fff;color:#d0021b;font-weight:800;display:flex;align-items:center;justify-content:center}}
.col{{display:flex;flex-direction:column;gap:4px;max-width:80%}}.who,.time{{font-size:11px;color:#fff}}
.bubble{{background:#fff;padding:8px 12px;border-radius:16px;font-size:14.5px;white-space:pre-wrap}}
.liff a{{display:block;background:#fff;border-radius:10px;padding:10px;color:var(--chip);margin-left:40px}}
.cards{{display:flex;gap:8px;overflow-x:auto}}.jc{{flex:none;width:236px;background:#fff;border-radius:14px;padding:10px}}
{legal.FOOTER_CSS}.legal,.legal a{{color:#fff}}</style>
</head><body><header>🎤 音声で相談(LINE)</header><main>
<p class="intro">アドバイザーと話しながら、希望と経験を5分ほどで伺います。終わると、話した内容のまとめと求人が LINE のトークに届きます。
氏名・住所・電話番号は聞きません。イヤホンの利用がおすすめです。</p>
<button class="go" id="voice-open" type="button" disabled>音声相談を始める</button>
<p id="liff-status"></p><div id="log"></div><form id="talk-form" hidden></form>
<button class="go" id="liff-back" type="button" hidden>LINE のトークに戻る</button>
</main>
{legal.footer_html()}
<script src="{LIFF_SDK}" charset="utf-8"></script>
<script src="/static/talk-core.js"></script>
<script>
(async () => {{
  const conf = {conf};
  const status = document.getElementById("liff-status"), open = document.getElementById("voice-open");
  const back = document.getElementById("liff-back");
  if (!window.liff) {{ status.textContent = conf.sdk; return; }}
  try {{ await liff.init({{ liffId: conf.liffId }}); }} catch (e) {{ status.textContent = conf.failed; return; }}
  const talk = createTalk({{
    log: document.getElementById("log"), form: document.getElementById("talk-form"),
    getUser: () => conf.user, isBusy: () => false, setBusy: () => {{}}, fire: async () => {{}},
    send: () => {{ status.textContent = conf.inTalk; }},  // 求人カードと選択肢のボタン: 続きはトークで
    voiceApi: {{ start: "/api/liff/voice/start", wait: "/api/liff/voice/wait", extra: () => ({{ id_token: liff.getIDToken() }}) }},
    onVoiceClosed: (done, started) => {{
      if (!started) {{ status.textContent = conf.notStarted; return; }}  // 始められなかった: もう一度押せる
      talk.disableChips(); status.textContent = done ? conf.sent : conf.later; open.hidden = true; back.hidden = false;
    }},
  }});
  open.disabled = false;
  open.onclick = () => talk.openVoice();
  back.onclick = () => liff.closeWindow();
}})();
</script></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _liff_upload_page(user: str) -> HTMLResponse:
    """LINE のアプリで開く履歴書の読み込みの画面。読み取った確認をここに出し、続きは LINE のトークに届く。"""
    conf = _liff_conf(user, sdk=LIFF_SDK_MISSING, reading="読み取っています。そのままお待ちください…",
                      pick="ファイルを選んでください。", sent="続きを LINE のトークに送ります。トークに戻ってください。",
                      done="読み取りました。")
    mb = resume_config.MAX_UPLOAD_BYTES // (1024 * 1024)
    body = f"""<p>履歴書か職務経歴書のファイル(PDF / JPEG / PNG、{mb}MB まで)を選ぶと、読み取った内容で LINE のトークの相談を続けます。
氏名・住所・電話番号・メールアドレスは保存しません。</p>
<input type="file" id="resume-file" accept=".pdf,.jpg,.jpeg,.png,application/pdf,image/jpeg,image/png">
<button id="resume-send" type="button" disabled>読み込む</button>
<p id="liff-status"></p><div id="resume-result"></div>
<button id="liff-back" type="button" hidden>LINE のトークに戻る</button>
<script src="{LIFF_SDK}" charset="utf-8"></script>
<script>
(async () => {{
  const conf = {conf};
  const status = document.getElementById("liff-status"), send = document.getElementById("resume-send");
  const input = document.getElementById("resume-file"), result = document.getElementById("resume-result");
  const back = document.getElementById("liff-back");
  if (!window.liff) {{ status.textContent = conf.sdk; return; }}
  try {{ await liff.init({{ liffId: conf.liffId }}); }} catch (e) {{ status.textContent = conf.failed; return; }}
  back.onclick = () => liff.closeWindow();
  send.disabled = false;
  send.onclick = async () => {{
    const f = input.files[0];
    if (!f) {{ status.textContent = conf.pick; return; }}
    send.disabled = true; status.textContent = conf.reading;
    const fd = new FormData();
    fd.append("user_id", conf.user); fd.append("id_token", liff.getIDToken() || ""); fd.append("file", f);
    try {{
      const r = await fetch("/api/liff/resume/upload", {{ method: "POST", body: fd, credentials: "same-origin" }});
      let d = null; try {{ d = await r.json(); }} catch (_) {{}}
      if (!r.ok) {{ status.textContent = (d && typeof d.detail === "string" && d.detail) || conf.failed; send.disabled = false; return; }}
      result.replaceChildren(...d.bubbles.map((b) => {{ const p = document.createElement("p"); p.className = "job"; p.textContent = b; return p; }}));
      status.textContent = d.talk ? conf.sent : conf.done;
      input.hidden = send.hidden = true; back.hidden = false;
    }} catch (e) {{ status.textContent = conf.failed; send.disabled = false; }}
  }};
}})();
</script>"""
    return HTMLResponse(_page("履歴書を読み込む", body), headers={"Cache-Control": "no-store"})


@app.get(f"{line_channel.LIFF_PREFIX}/voice", response_class=HTMLResponse)
async def liff_line_voice(request: Request):
    user = _liff_page_user(request)
    return _liff_shell("音声で相談") if user is None else _liff_voice_page(user)


@app.get(f"{line_channel.LIFF_PREFIX}/resume/upload", response_class=HTMLResponse)
async def liff_line_resume_upload(request: Request):
    user = _liff_page_user(request)
    return _liff_shell("履歴書を読み込む") if user is None else _liff_upload_page(user)


@app.get(f"{line_channel.LIFF_PREFIX}/arcade")
async def liff_line_arcade(request: Request):
    """LINE のアプリで開くレジュメ・アーケードのハブ(LIFF の本人を確かめてから。ゲームへのリンクも /liff/line/arcade/… になる)。"""
    return _liff_shell("レジュメ・アーケード") if _liff_page_user(request) is None else await liff_arcade()


@app.get(f"{line_channel.LIFF_PREFIX}/arcade/{{game}}")
async def liff_line_arcade_game(game: str, request: Request):
    return _liff_shell("レジュメ・アーケード") if _liff_page_user(request) is None else await liff_arcade_game(game)


# ----------------------------- LIFF の中でマイクを試す画面(/liff/line/mic-check、task 205) -----------------------------
# LINE のアプリの LIFF 画面で、音声相談に要るマイク(getUserMedia)と WebRTC が使えるかを、人が実機で確かめる。
# 画面(static/mic-check.js)はマイクの許可を求め、使えたか・断られたか・エラー名、音の入力の大きさ、LIFF の環境を出し、
# 結果だけを POST /api/liff/mic-check に送る。声もその文字起こしも送らない(受け口は決まった項目の外を 422 で断る)。
# 本人のデータを読み書きしないので、本人を決めない(Cookie を発行も要求もしない)。LIFF の設定が無ければ 404。

mic_log = logging.getLogger("line_runtime.mic_check")
mic_log.setLevel(logging.INFO)
if not mic_log.handlers:  # Web サーバーはログの設定をしない(WARNING より下は出ない)ので、この 1 行だけは標準エラーに出す
    _mic_handler = logging.StreamHandler()
    _mic_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    mic_log.addHandler(_mic_handler)
LIVEKIT_CLIENT = "https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js"  # 音声相談と同じ(talk-core.js)
MIC_CHECK_DAILY_LIMIT = 500  # 1 日にログに書く数の上限(本人を決めない口なので、ログをあふれさせない)
_mic_checks: dict[str, int] = {}
_ErrorName = Annotated[str, Field(max_length=40, pattern=r"^[A-Za-z0-9_]*$")]  # エラーの名前(NotAllowedError、LIFF の INIT_FAILED や 400 など)
_Version = Annotated[str, Field(max_length=32, pattern=r"^[0-9A-Za-z._-]*$")]


class MicCheckIn(BaseModel):
    """マイクを試した結果。決まった項目だけを受ける(ほかの項目があれば 422。声や文字起こしを載せる場所を作らない)。"""
    model_config = ConfigDict(extra="forbid")

    mic: Literal["allowed", "denied", "error", "unsupported"]
    mic_error: _ErrorName = ""
    level: float = Field(0, ge=0, le=1)  # 試した間の音の入力の大きさの最大(0 から 1)
    audio_state: Literal["running", "suspended", "closed", "interrupted", ""] = ""  # 音を測った AudioContext の状態
    webrtc: Literal["ok", "error", "unsupported"]
    webrtc_error: _ErrorName = ""
    candidates: int = Field(0, ge=0, le=99)  # 手元の ICE の候補の数(外の STUN/TURN は使わない)
    livekit: Literal["supported", "unsupported", "not_loaded"]
    os: Literal["ios", "android", "web", ""] = ""  # liff.getOS()
    in_client: bool | None = None  # liff.isInClient()
    line_version: _Version = ""  # liff.getLineVersion()
    liff_version: _Version = ""  # liff.getVersion()
    view_type: Literal["compact", "tall", "full", "frame", "full-flex", ""] = ""  # liff.getContext().viewType
    liff_error: _ErrorName = ""  # liff.init が失敗したときのエラーの名前
    secure_context: bool
    user_agent: str = Field("", max_length=300)


@app.get(f"{line_channel.LIFF_PREFIX}/mic-check", response_class=HTMLResponse)
async def liff_line_mic_check():
    """LIFF の中でマイクを試す画面。LINE では https://liff.line.me/<LIFF ID>/mic-check で開く。"""
    if not line_channel.liff_configured():
        raise HTTPException(404)
    conf = json.dumps({"liffId": line_channel.liff_id(), "livekitClient": LIVEKIT_CLIENT}).replace("<", "\\u003c")
    body = f"""<p>音声相談で使うマイクと通話の部品が、この画面で使えるかを確かめます。声は送らず、残しません。
送るのは結果(使えたか、エラーの名前、音の大きさ、LINE の版など)だけです。</p>
<button type="button" id="mic-start">マイクを試す</button>
<p id="mic-status" aria-live="polite">「マイクを試す」を押すと、マイクの許可を求めます。5 秒ほど声を出してください。</p>
<meter id="mic-level" min="0" max="1" value="0" style="width:100%;height:24px"></meter>
<pre id="mic-result" style="white-space:pre-wrap;background:#fff;border-radius:10px;padding:12px;font-size:13px"></pre>
<p id="mic-external-wrap" hidden><button type="button" id="mic-external">外のブラウザで同じ確認を開く</button></p>
<script>window.MIC_CHECK = {conf};</script>
<script src="{LIFF_SDK}" charset="utf-8"></script>
<script src="/static/mic-check.js"></script>"""
    return HTMLResponse(_page("マイクの確認", body), headers={"Cache-Control": "no-store"})


@app.post("/api/liff/mic-check")
async def liff_mic_check_log(body: MicCheckIn, request: Request):
    """マイクを試した結果を、サーバーのログに 1 行だけ書く(本人、IP は書かない)。JSON の本文だけ(ほかのサイトのフォームから書かせない)。"""
    if not line_channel.liff_configured():
        raise HTTPException(404)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "JSON で送ってください")
    day = datetime.now(UTC).date().isoformat()
    if _mic_checks.get(day, 0) >= MIC_CHECK_DAILY_LIMIT:
        raise HTTPException(429, "今日はこれ以上記録できません")
    if day not in _mic_checks:  # 前の日の数は捨てる
        _mic_checks.clear()
    _mic_checks[day] = _mic_checks.get(day, 0) + 1
    mic_log.info("LIFF のマイクの確認: %s", json.dumps(body.model_dump(), ensure_ascii=False))
    return {"ok": True}


def _page(title: str, body: str, site: str = "LIFF・dip の画面", legal_links: bool = True) -> str:
    """LIFF と dip の画面の枠。aica の画面(legal_links)なら、下にテスト用の公開であることと、規約とポリシーへのリンクを置く。
    dip のログインと偽物の LINE の画面は、aica ではない役の画面なので置かない。"""
    footer = legal.footer_html() if legal_links else ""
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{margin:0;font-family:-apple-system,"Hiragino Sans",sans-serif;background:#f4f6f8;color:#111}}
header{{background:#06c755;color:#fff;padding:12px 16px;font-weight:700}}main{{padding:16px;max-width:480px;margin:auto}}
.job{{background:#fff;border-radius:10px;padding:12px;font-weight:700}}label{{display:block;margin:12px 0;font-size:14px}}
input{{display:block;width:100%;box-sizing:border-box;margin-top:6px;padding:10px;font-size:16px;border:1px solid #ccd;border-radius:8px}}
button{{width:100%;padding:12px;border:0;border-radius:8px;background:#06c755;color:#fff;font-size:16px;font-weight:700}}
.demo{{background:#fff7e6;border-radius:10px;padding:10px 12px;font-size:13px;line-height:1.6}}{legal.FOOTER_CSS}</style>
</head><body><header>{title}({site})</header><main>{body}</main>{footer}</body></html>"""


def main() -> None:
    import uvicorn

    # SDK(OpenAI、Anthropic、httpx など)の INFO や DEBUG のログに、送った本文が出ないように(docs/design/privacy.md の 3.4)
    quiet_sdk_loggers()
    # X-Forwarded-For は _client_ip だけが AICA_TRUST_PROXY に従って読む(uvicorn には書き換えさせない)。
    # ワーカーは 1 つ(会話数の上限と状態を 1 つのプロセスで持つ)。コンテナでは LINE_SIM_HOST=0.0.0.0(scripts/container_start.sh)
    uvicorn.run(app, host=os.environ.get("LINE_SIM_HOST", "127.0.0.1"), port=int(os.environ.get("LINE_SIM_PORT", "8030")),
                proxy_headers=False)


if __name__ == "__main__":
    main()
