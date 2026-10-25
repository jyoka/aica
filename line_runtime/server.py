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
- サーバー間の経路(音声ワーカー → POST /api/voice/result)は、Cookie ではなく共有鍵(AICA_INTERNAL_KEY)で守る。

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
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from collections import defaultdict
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Annotated
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
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from dip_mcp import accounts, behavior, logic, profile
from dip_mcp.data import State, load_jobs
from evals.scenarios import load_scenarios, prepare
from line_runtime import config, fake_line, line_channel, turn
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
from resume_draft.schema import document_kind_of
from resume_intake import config as resume_config
from resume_intake.extract import extract_bytes, finalize, media_type

STATIC = config.ROOT / "line_runtime" / "static"
state = State()

# ブラウザごとの匿名ユーザーID(Cookie)。ブラウザ側には ID しか置かず、状態はすべてサーバー側でこの ID にひも付く。
ANON_COOKIE = "aica_uid"
ANON_ID = re.compile(r"w-[0-9a-f]{16}")
ANON_COOKIE_MAX_AGE = 400 * 24 * 3600  # ブラウザが Cookie を保持できる上限(約400日)いっぱい


def _anon(request: Request) -> str | None:
    v = request.cookies.get(ANON_COOKIE, "")
    return v if ANON_ID.fullmatch(v) else None


def _issue(request: Request, response: Response) -> str:
    """このブラウザの匿名ユーザーID。無ければ発行して Cookie に保存する(再アクセスで同じ ID になる)。
    発行するのは画面を開いたとき(GET / と GET /dev)だけ。API は発行しない(Cookie が無ければ 401)。"""
    uid = _anon(request)
    if uid is None:
        uid = f"w-{secrets.token_hex(8)}"
        response.set_cookie(ANON_COOKIE, uid, max_age=ANON_COOKIE_MAX_AGE, samesite="lax", httponly=True)
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
    """プロフィールを書き換える本人(Cookie の匿名ID)。この IP の匿名IDの数を超えるなら 429。"""
    user_id = _owner(request)
    _admit(request, user_id)
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
    text: str | None = None
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
    app.state.unread_uploads = {}  # ユーザー → 読み取れなかった直前のアップロード(続くターンの facts)
    app.state.line_seen = line_channel.SeenEvents(config.DATA_DIR / "line_events.json")  # LINE の再送に 2 回答えない
    app.state.line_locks = defaultdict(asyncio.Lock)  # LINE のユーザー → その人のイベントを届いた順に 1 つずつ
    app.state.fake_line = fake_line.FakeLinePlatform()  # 開発者画面から試す、Web の匿名ユーザーのアカウント連携の LINE の役
    async with LineRuntime() as rt:
        app.state.rt = rt
        yield


app = FastAPI(title="LINE AI assistant simulator", lifespan=lifespan)


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


@app.get("/static/{name}")
async def shared_static(name: str):
    """トークルームの共通部品(吹き出し・求人カード・返信の描画・「+」メニュー・履歴書・音声)。"""
    if name not in SHARED_STATIC:
        raise HTTPException(404)
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-cache"})


# レジュメ・アーケード(docs/prd/resume-arcade.md): ドット絵のゲームで職務経歴書を作る LIFF 画面。いまは AI を使わない簡易分析だけで、
# 本人のデータを読み書きしないので、本人を決めない(Cookie を発行も要求もしない)。ゲームの名前はファイル名と同じ。
ARCADE = STATIC / "arcade"
ARCADE_SHIPPED = ("the-case-of-you", "harvest-of-work")  # ハブに出す(案内の部品がそろったもの)
ARCADE_GAMES = ARCADE_SHIPPED + (  # 残りは開発者画面からだけリンクする
    "career-quest", "dungeon-of-past-jobs", "career-pet", "career-fighter", "deck-of-deeds", "star-log")
ARCADE_STATIC = {"arcade-ja.js"}  # ゲームが共通に使う部品(日本語の答えの判定)


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


# ----------------------------- LINE 公式アカウント(Messaging API) -----------------------------
# 2 つ目のチャネル。ユーザーは LINE の userId から作った「l-<ハッシュ>」(line_channel.line_user。userId は保存しない)。
# 本番のユーザーなので dip の時計は実際の時刻(data.uses_sim_clock に当たらない)。会話数は LINE のユーザーごとと全体で数え、
# IP ごとには数えない(どの人の webhook も LINE のサーバーから届く)。
# webhook にはすぐ 200 を返し、ターンは応答の後に進める(LLM が遅くても LINE が待ちきれずに再送しないように)。


def line_api() -> line_channel.LineApi:
    """LINE の API(テストでは偽物の LINE API に差し替える)。"""
    return line_channel.LineApi(line_channel.access_token())


def _line_kind(e: dict) -> str | None:
    """答えるイベント: 1 対 1 のトークの、空でないテキストのメッセージ(message。決まった文なら連携の開始 link と
    解除 unlink)、友だち追加(follow)、アカウント連携の結果(account_link)。ほかは None(無視する)。
    standby(ほかのチャネルが応答を受け持っている間)のイベントには答えない。"""
    src = e.get("source") or {}
    if src.get("type") != "user" or not src.get("userId") or e.get("mode", "active") != "active":
        return None
    msg = e.get("message") or {}
    if e.get("type") == "message" and msg.get("type") == "text" and str(msg.get("text") or "").strip():
        return line_channel.link_command(str(msg["text"])) or "message"
    if e.get("type") == "accountLink" and isinstance(e.get("link"), dict):
        return "account_link"
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
            except Exception:  # noqa: BLE001  1 件の失敗で、残りのイベントと本人への返事を止めない
                line_channel.log.exception("LINE のイベントを処理できませんでした(%s)", uid)
                messages = line_channel.to_messages({"bubbles": [line_channel.FAILED_TEXT]})
            if messages:
                await api.send(e.get("replyToken"), line_uid, messages)


async def _line_messages(api: line_channel.LineApi, line_uid: str, uid: str, kind: str, e: dict) -> list[dict] | None:
    """1 件のイベントに返す LINE のメッセージ。アカウント連携(開始・結果・解除)は LLM を使わず、会話数も数えない。
    ほかは会話のターン(返すものが無ければ None)。"""
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
    受け付けないイベント(空白だけの発言など)は None(何も返さない)。ターンの途中の失敗は、謝りの文を返す。"""
    rt = app.state.rt
    deps = turn.TurnDeps(state=state, quota=app.state.quota, rt=rt, unread_uploads=app.state.unread_uploads, ip=None)
    if kind == "follow":
        event, text, new = logic.talk_entry(state, uid), None, False
    else:
        async with rt.user_lock(uid):  # 進行中のターンと交差しないように(talk_open と同じ)
            conv = await rt.conversation(uid)
        event, text, new = "message", e["message"].get("text"), not talk_continues(conv)
    try:
        t = turn.accept(deps, uid, event, text, None, new_conversation=new)
    except turn.BadTurn:
        return None
    try:
        reply = await turn.reply_of(deps, t)
    except (turn.BadTurn, turn.TurnFailed) as err:
        line_channel.log.warning("LINE のターンが失敗しました(%s): %s", uid, _error_text(err))
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
                                               "送ってください。</p>"), status_code=400)


@app.get(line_channel.LINK_LOGIN_PATH, response_class=HTMLResponse)
async def dip_login_page(link_token: Annotated[str, Query(alias="linkToken")] = ""):
    if not LINK_TOKEN_FORM.fullmatch(link_token):
        return _bad_link_page()
    return _page("dip にログイン", _login_form(link_token))


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
        return HTMLResponse(_page("dip にログイン", _login_form(link_token, LOGIN_MESSAGES[r["error"]])),
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
        return HTMLResponse(_page("アカウント連携", "<p>このリンクは使えません。</p>", "偽物の LINE"), status_code=404)
    text = account_link(event["source"]["userId"], event["link"])
    return _page("アカウント連携", f"<p>{escape(text)}</p><p><a href='/'>トークに戻る</a></p>", "偽物の LINE")


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
    anon = _owner(request)
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
    return {"forgotten": True}


# ----------------------------- 音声相談(voice-resume との連携) -----------------------------
# トークの「話して伝える」/「+」→「音声で相談する」は、同じ画面の上に重ねるシート(talk-core.js)で開く。
# シートは POST /api/voice/start で LiveKit の接続トークンを受け取り、ブラウザ ⇔ LiveKit Cloud ⇔
# 音声ワーカー(voice_resume.agent)で話す。面談が終わるとワーカーが POST /api/voice/result に結果を送り
# (サーバー間の経路なので共有鍵 AICA_INTERNAL_KEY で守る)、ここがそれを今の会話に AI の返信として続ける。
# シートは開いている間だけ POST /api/voice/wait で待ち、届いたら閉じて同じ返信をトークに描く(常時の問い合わせはしない)。
# ユーザーIDは本文で受け取り、URL には載せない。

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


def _voice_ready(user_id: str) -> asyncio.Condition:
    """そのユーザーの音声相談の結果が届いたことを、待っているシートへ知らせる。"""
    return app.state.voice_ready.setdefault(user_id, asyncio.Condition())


@app.post("/api/voice/start")
async def voice_start(body: UserIn, request: Request):
    """音声相談のシートを開いた: LiveKit の部屋に入る接続トークンを発行する(本人のブラウザにだけ)。
    音声セッションも実 API を使うので、会話数を1ターン使う(上限なら発行せず 429)。トークンの有効期限は数分
    (同じトークンで入り直して、会話数を使わずに音声セッションを重ねられないように)。
    since は、このシートが待つ結果の番号(開く前に届いていた結果は受け取らない)。"""
    user_id = body.user_id
    _require_own(request, user_id)
    from voice_resume.livekit_token import configured, issue_token

    if not configured():  # 会話数を使う前に断る
        raise HTTPException(503, "音声相談の準備ができていません(LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET)")
    limited = app.state.quota.try_take(user_id, _client_ip(request))
    if limited is not None:
        raise HTTPException(429, limited)
    token = issue_token(user_id)
    logic.record_voice_room(state, user_id, token["room"])  # 「すべて消す」の前に始めた面談かを、結果が届いたときに見分ける
    return token | {"since": logic.voice_result_count(state, user_id)}


@app.post("/api/voice/wait")
async def voice_wait(body: VoiceWaitIn, request: Request):
    """音声相談のシートが開いている間だけ呼ぶ: since 番以降に届いた、この部屋(room。/start が返す)の結果を
    最大 timeout 秒待って返す(届かなければ空)。results は今の会話に続けた返信と同じ形(画面は renderReply で描く)。
    next は次に待つときの since(この部屋でない結果も読み飛ばす。そちらは会話に入っているので再読み込みで出る)。"""
    user_id, since, room = body.user_id, body.since, body.room
    _require_own(request, user_id)
    timeout = min(max(body.timeout, 0.0), VOICE_WAIT_MAX)
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
async def voice_result_in(body: VoiceResultIn, request: Request):
    """音声アドバイザーの結果(話した希望・提案した求人・履歴書か職務経歴書の下書き)を預かり、今の会話に AI の返信として
    続ける(再読み込みしても残る)。待っているシートがあれば知らせる。
    呼ぶのは voice-resume のワーカー(サーバー間の経路)。Cookie ではなく共有鍵(AICA_INTERNAL_KEY)で守る
    (匿名IDを知っているだけでは、他人の会話に結果のターンを足せない)。
    面談の途中で本人が「すべて消す」をしていたら(その前に発行した部屋の結果)、預からずに 410 を返す
    (消した後に dip の記録を作り直さない。ワーカーは 410 を見て、会話の結果も記録しない)。"""
    _require_internal(request)
    if logic.voice_room_forgotten(state, body.user_id, body.room):
        raise HTTPException(410, "この面談の前に、本人が覚えていることをすべて消しました")
    out = logic.record_voice_result(state, body.user_id, body.jobs, body.draft_markdown, body.files, body.preferences,
                                    body.room, body.document_kind)
    rec = logic.voice_results_since(state, body.user_id, out["recorded"])[0]
    track = logic.search_track(state.user(body.user_id))  # 音声で区分が切り替わっていれば、次のターンの AI に伝える
    out["talk"] = await app.state.rt.add_voice_result(body.user_id, voice_reply(body.user_id, rec, track))
    cond = _voice_ready(body.user_id)
    async with cond:
        cond.notify_all()
    return out


@app.get("/liff/voice/draft", response_class=HTMLResponse)
async def liff_voice_draft(rid: int, request: Request, scenario_user: str | None = None):
    """音声相談で作った下書きを表示する(トークの「履歴書の下書きを見る」「職務経歴書の下書きを見る」のリンク先)。
    題は下書きの書類(document_kind)に合わせる。本人は Cookie から決める(URL にユーザーIDは無い)。"""
    user = _page_user(request, scenario_user)
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
    limited = app.state.quota.try_take(user_id, _client_ip(request))
    if limited is not None:  # アップロードも1ターン扱い: 上限を超えたら読み取らずに案内だけ返す
        raise HTTPException(429, limited)
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
    user = _page_user(request, scenario_user)
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
    user = _page_user(request, scenario_user)
    j = load_jobs().get(job)
    exit_ = logic.form_exit(state, user)
    if j is None or exit_ is None or exit_["job_id"] != job:
        return HTMLResponse(_page("応募フォーム", "<p>入力途中の応募はありません。</p>"), status_code=404)
    fields = "".join(
        f'<label>{escape(f)}<input name="f_{escape(f)}" required placeholder="(シミュレーションなので架空の値で)"></label>'
        for f in exit_["remaining_fields"])
    body = (f"<p class='job'>{escape(j.title)}<br><small>{escape(j.employer)}</small></p>"
            f"<p>入力済み: {escape('、'.join(exit_['completed_fields']))}</p>"
            f"<form method='post'><input type='hidden' name='scenario_user' value='{escape(scenario_user or '')}'>"
            f"<input type='hidden' name='job' value='{escape(job)}'>{fields}<button>応募する</button></form>")
    return _page("応募の続き", body)


@app.post("/liff/apply", response_class=HTMLResponse)
async def liff_submit(request: Request, job: str = Form(...), scenario_user: str = Form("")):
    user = _page_user(request, scenario_user or None)
    exit_ = logic.form_exit(state, user)
    if exit_ is None or exit_["job_id"] != job:  # 入力途中の応募が無ければ確定しない(状態にも書かない)
        return HTMLResponse(_page("応募フォーム", "<p>入力途中の応募はありません。</p>"), status_code=404)
    # 入力値は保存しない(シミュレーション)。応募の確定だけを dip の状態に反映する。
    logic.submit_application(state, user, job)
    return _page("応募完了", "<p>応募しました。店舗からの連絡をお待ちください。</p><p>このタブを閉じてトークに戻れます。</p>")


def _page(title: str, body: str, site: str = "LIFF・dip の画面") -> str:
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{margin:0;font-family:-apple-system,"Hiragino Sans",sans-serif;background:#f4f6f8;color:#111}}
header{{background:#06c755;color:#fff;padding:12px 16px;font-weight:700}}main{{padding:16px;max-width:480px;margin:auto}}
.job{{background:#fff;border-radius:10px;padding:12px;font-weight:700}}label{{display:block;margin:12px 0;font-size:14px}}
input{{display:block;width:100%;box-sizing:border-box;margin-top:6px;padding:10px;font-size:16px;border:1px solid #ccd;border-radius:8px}}
button{{width:100%;padding:12px;border:0;border-radius:8px;background:#06c755;color:#fff;font-size:16px;font-weight:700}}</style>
</head><body><header>{title}({site})</header><main>{body}</main></body></html>"""


if __name__ == "__main__":
    import uvicorn

    # X-Forwarded-For は _client_ip だけが AICA_TRUST_PROXY に従って読む(uvicorn には書き換えさせない)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LINE_SIM_PORT", "8030")), proxy_headers=False)
