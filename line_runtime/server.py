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
「匿名ID:シナリオユーザー」で始めるので、複数の知人が同時に使っても会話・長期メモリ・行動履歴・費用台帳が混線しない。

ユーザーの分離の守り(task 99):
- ブラウザから呼ぶ経路は、正しい形式の Cookie が必須(無い・不正なら 401)。扱えるのは Cookie の匿名IDのユーザーだけ
  (他人のユーザーIDを本文に入れても 403)。ID を名乗るだけでは読めない・話せない・会話数の上限も迂回できない。
- 本人のデータを指す URL にユーザーIDを載せない。API はユーザーIDを本文で受け取り、画面(LIFF)は Cookie から本人を決める。
- サーバー間の経路(音声ワーカー → POST /api/voice/result)は、Cookie ではなく共有鍵(AICA_INTERNAL_KEY)で守る。

会話数の上限(line_runtime.quota): 1日あたりのターン数に、匿名IDごと・IP ごと・全体の上限があり、超えたら LLM を呼ばずに
丁寧に打ち切る。IP ごとの上限で、Cookie を作り直して匿名IDごとの上限を迂回する道を閉じる(IP の取り方は _client_ip、
IPv6 は /64 にまとめて数える)。全体の匿名IDの数の上限で、IP をいくつ持っていても状態ファイルにユーザーが増え続けない。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from dip_mcp import logic, profile
from dip_mcp.data import State, load_jobs
from evals.scenarios import load_scenarios, prepare
from line_runtime import config
from line_runtime.quota import (
    DailyQuota,
    daily_limit,
    ip_ids_limit,
    ip_limit,
    limit_reply,
    total_ids_limit,
    total_limit,
)
from line_runtime.resume_confirm import INTRO as RESUME_INTRO
from line_runtime.resume_confirm import resume_bubbles
from line_runtime.runtime import EVENTS, LineRuntime
from line_runtime.voice_reply import voice_reply
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


LIMIT_REASON = "本日の会話数の上限に達している"

# 本番のトーク画面: 最後のやりとりからこの時間内に開き直したら(再読み込み・タブの開き直し)、今の会話を続ける
TALK_RESUME_WINDOW = timedelta(minutes=30)


def clock() -> datetime:
    """今の時刻(テストで差し替える)。会話の最後のやりとり(チェックポイントの保存時刻、UTC)と比べる。"""
    return datetime.now(UTC)


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
    app.state.quota = DailyQuota(daily_limit(), config.DATA_DIR / "quota.json", ip_limit=ip_limit(),
                                 ip_ids=ip_ids_limit(), total_limit=total_limit(), total_ids=total_ids_limit())
    app.state.voice_ready = {}  # ユーザー → 音声相談の結果が届いたことを待っているシートへ知らせる Condition
    app.state.unread_uploads = {}  # ユーザー → 読み取れなかった直前のアップロード(続くターンの facts)
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
    if conv["last_at"] is not None and clock() - conv["last_at"] <= TALK_RESUME_WINDOW:
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
        _track_status(user_id, t["bot"])
    return {"thread": conv["thread"], "turns": conv["turns"], "search_track": logic.search_track(state.user(user_id))}


def _track_status(user_id: str, reply: dict | None) -> dict | None:
    """返信の切り替えの表示(track_switch)に、今の状態(取り消せるか・取り消したか)を dip の記録から足す。"""
    sw = (reply or {}).get("track_switch")
    if sw:
        sw["status"] = profile.switch_status(state, user_id, sw["id"])
    return reply


class UndoIn(BaseModel):
    user_id: str
    id: str  # 取り消す切り替えの記録の id(返信の track_switch.id)


@app.post("/api/talk/track/undo")
async def talk_track_undo(body: UndoIn, request: Request):
    """トークの切り替えの表示のボタン: その切り替えを取り消して元の区分に戻す(dip に取り消しとして記録。LLM は呼ばず、
    会話数も使わない)。AI には次の本人のターンの前に知らせる(_profile_notice)。直前の切り替えでなければ 409。
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


@app.post("/api/event")
async def event(body: EventIn, request: Request):
    _require_own(request, body.user_id)
    if body.event not in EVENTS:
        raise HTTPException(400, f"event は {list(EVENTS)} のいずれか")
    _require_text(body)
    entry, kind = EVENTS[body.event]
    quota, ip = app.state.quota, _client_ip(request)
    facts = _upload_facts(body)
    if kind == "push":
        # 上限に達していたら、接触として記録する前(dip に聞く前)に送らないと決める
        if not quota.available(body.user_id, ip):
            return {"sent": False, "reason": LIMIT_REASON}
        # ここは dip のバックエンドの代わり: 行動イベントを検知し、プッシュしてよいかを dip が決める。
        gate = logic.push_trigger(state, body.user_id, body.event)
        if not gate["allowed"]:
            return {"sent": False, "reason": gate["reason"]}
        facts = gate["facts"]
    stale = await _stale(body)
    if stale is not None:  # 会話数を使う前に断る(runtime もターンの排他の中で同じ照合をする)
        return {"stale": True, "thread": stale}
    limited = quota.try_take(body.user_id, ip)
    if limited is not None:
        return limit_reply(entry, limited)
    changes, notice = _profile_notice(body)
    try:
        r = await app.state.rt.handle(body.user_id, body.event, body.text, facts, talk_thread=body.thread,
                                      notice=notice)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if r and r.get("stale"):  # 事前の照合の後に別のタブが会話を始めていた: LLM は呼んでいないので会話数を返す
        quota.refund(body.user_id, ip)
    _record_open(body, r, changes)
    if r and not r.get("stale") and r.get("sent") is not False:
        r["search_track"] = logic.search_track(state.user(body.user_id))
    return _track_status(body.user_id, r)


@app.post("/api/event/stream")
async def event_stream(body: EventIn, request: Request):
    """1ターンの途中経過を1行1件の JSON で順に返す(画面のリアルタイム表示用)。
    start → (プッシュなら gate)→ llm / tool_start / tools … → reply。途中で失敗したら error。"""
    _require_own(request, body.user_id)
    if body.event not in EVENTS:
        raise HTTPException(400, f"event は {list(EVENTS)} のいずれか")
    _require_text(body)
    upload = _upload_facts(body)
    ip = _client_ip(request)

    async def lines():
        entry, kind = EVENTS[body.event]
        yield _line({"kind": "start", "event": body.event, "entry": entry, "event_kind": kind, "model": config.MODEL})
        facts = upload
        quota = app.state.quota
        if kind == "push":
            if not quota.available(body.user_id, ip):
                yield _line({"kind": "reply", "sent": False, "reason": LIMIT_REASON})
                return
            gate = logic.push_trigger(state, body.user_id, body.event)
            yield _line({"kind": "gate", **gate})
            if not gate["allowed"]:
                yield _line({"kind": "reply", "sent": False, "reason": gate["reason"]})
                return
            facts = gate["facts"]
        stale = await _stale(body)
        if stale is not None:  # 別のタブで新しい会話が始まっていた: 発言を混ぜず、会話数も使わない
            yield _line({"kind": "stale", "thread": stale})
            return
        limited = quota.try_take(body.user_id, ip)
        if limited is not None:  # 上限を超えたら LLM を呼ばずに案内だけ返す
            yield _line({"kind": "reply", **limit_reply(entry, limited)})
            return
        changes, notice = _profile_notice(body)
        try:
            async for ev in app.state.rt.handle_stream(body.user_id, body.event, body.text, facts, talk_thread=body.thread,
                                                       notice=notice):
                if ev["kind"] == "reply":
                    _record_open(body, ev, changes)
                    _track_status(body.user_id, ev)["search_track"] = logic.search_track(state.user(body.user_id))
                elif ev["kind"] == "stale":  # 事前の照合の後に別のタブが会話を始めていた: 会話数を返す
                    quota.refund(body.user_id, ip)
                yield _line(ev)
        except Exception as e:  # noqa: BLE001  API・MCP のエラーも画面に出す(応答を返し始めた後なので HTTP のエラーにはできない)
            yield _line({"kind": "error", "message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(lines(), media_type="application/x-ndjson")


async def _stale(body: EventIn) -> int | None:
    """本番のトーク画面からの発言で、今の会話が画面の会話と違えば今の会話番号(違わなければ None)。"""
    if body.thread is None or EVENTS[body.event][1] not in ("user", "upload"):
        return None
    n = await app.state.rt.current_thread(body.user_id)
    return n if n != body.thread else None


def _profile_notice(body: EventIn) -> tuple[int, str | None]:
    """プロフィール画面で変えたことのうち、まだ AI に渡していないもの: (件数, 知らせの文)。
    新しい会話を始めるイベント(挨拶・プッシュ)では渡さない(最初に要約を読み直すので、返ったら渡し終えた扱い)。"""
    changes = profile.pending_changes(state, body.user_id)
    if not changes or EVENTS[body.event][1] not in ("user", "upload"):
        return len(changes), None
    return len(changes), profile.notice(state, body.user_id, changes)


def _record_open(body: EventIn, reply: dict | None, changes: int = 0) -> None:
    """本番のトーク画面の挨拶が返ったら、開いたことを dip に記録する(次からは再訪)。
    読み取れなかったアップロードに続くターンが返ったら、その事実は使い終わり(次のアップロードのターンに持ち越さない)。
    返信が返ったら、そのターンで渡したプロフィールの変更の知らせ(changes 件)も渡し終えた。"""
    if not reply or reply.get("stale"):
        return
    profile.take_changes(state, body.user_id, changes)
    if body.thread is not None and EVENTS[body.event][1] == "greet":
        logic.record_talk_open(state, body.user_id)
    if EVENTS[body.event][1] == "upload":
        app.state.unread_uploads.pop(body.user_id, None)


def _upload_facts(body: EventIn) -> dict | None:
    """履歴書のアップロードのターンに渡す事実: ファイルの表示名・トークに出した確認の吹き出し・読み取り結果の番号。
    どれも dip に保存した読み取り結果から作る(画面からは受け取らない。ファイル名はどこにも無い)。
    会話の記録にそのまま残るので、読み取り結果の画面の URL は入れない(履歴を返すときに番号から組み立てる)。
    直前のアップロードが読み取れなかった(empty)ときは、そのとき出した案内と empty を渡す(dip には保存していない)。
    読み取り結果が無ければ、会話数を使う前に断る。"""
    if EVENTS[body.event][1] != "upload":
        return None
    unread = app.state.unread_uploads.get(body.user_id)
    if unread is not None:  # 直前のアップロードは読み取れなかった: その前提で AI が1問返す
        return unread
    rec = logic.resume_upload(state, body.user_id)
    if rec is None:
        raise HTTPException(400, "アップロードした履歴書がありません")
    return {"file": rec.get("file") or RESUME_LABEL["application/pdf"], "shown": resume_bubbles(rec["resume"]),
            "upload_id": rec.get("id")}


def _require_text(body: EventIn) -> None:
    """空の発言は、会話数を使う前に断る(上限の1回分を無駄にしない)。"""
    if EVENTS[body.event][1] == "user" and not (body.text or "").strip():
        raise HTTPException(400, f"{body.event} には空でない text が必要です")


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


class UserIn(BaseModel):
    """ユーザーを指す API の本文(ユーザーIDは URL に載せない)。"""
    user_id: str


@app.post("/api/dip")
async def dip_view(body: UserIn, request: Request):
    """dip 側から見たユーザー状態(デバッグ用): 要約と記録された会話結果。"""
    user_id = body.user_id
    _require_own(request, user_id)
    u = state.user(user_id)
    return {"summary": logic.conversation_summary(state, user_id), "outcomes": u.get("outcomes", []),
            "saves": u.get("saves", []), "applications": u.get("applications", []),
            "last_contact": (u.get("contacts") or [None])[-1]}


# ----------------------------- わたしのプロフィール(覚えていることを見る・直す・消す) -----------------------------
# 本番のトーク画面の下のナビ「プロフィール」。読むのも変えるのも、本人の匿名ID(Cookie)のユーザーだけ
# (パスにユーザーIDは載せない。Cookie が無い・不正なら 401)。中身は dip_mcp.profile。変えたことは次の本人のターンの前に AI へ渡す(_profile_notice)。


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
    """履歴書の読み取り結果を消し、今の会話を閉じる(会話に残るアップロードの要点・要約の結果を、画面にも AI にも
    もう出さない)。次にトークを開くと、30分以内でも新しい会話(挨拶)から始まる。古い会話を開いたままのタブの発言は
    stale になる。LLM は呼ばず、会話数も使わない。消すものが無ければ会話は閉じない。"""
    user_id = _profile_writer(request)
    rt = app.state.rt
    async with rt.user_lock(user_id):  # 進行中のターンが終わってから消して閉じる
        if profile.delete_resume(state, user_id)["found"]:
            await rt.close_conversation(user_id)
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
    """すべて消す: dip の状態(長期メモリ・保存・応募・トークを開いた記録)と会話(チェックポイント)を消す。
    次に開くと初回訪問として始まる。会話数の上限の数と費用台帳は消さない(消すと上限を逃れられるため)。"""
    user_id = _owner(request)
    rt = app.state.rt
    async with rt.user_lock(user_id):  # 進行中のターンが終わってから消す
        out = profile.forget_all(state, user_id)
        await rt.forget(user_id)
    app.state.unread_uploads.pop(user_id, None)
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
DRAFT_MAX = 20_000  # 職務経歴書の下書きの長さの上限(字)。普通の下書きは数千字


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
    return issue_token(user_id) | {"since": logic.voice_result_count(state, user_id)}


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
    """音声アドバイザーの結果(話した希望・提案した求人・職務経歴書の下書き)を預かり、今の会話に AI の返信として
    続ける(再読み込みしても残る)。待っているシートがあれば知らせる。
    呼ぶのは voice-resume のワーカー(サーバー間の経路)。Cookie ではなく共有鍵(AICA_INTERNAL_KEY)で守る
    (匿名IDを知っているだけでは、他人の会話に結果のターンを足せない)。"""
    _require_internal(request)
    out = logic.record_voice_result(state, body.user_id, body.jobs, body.draft_markdown, body.files, body.preferences,
                                    body.room)
    rec = logic.voice_results_since(state, body.user_id, out["recorded"])[0]
    out["talk"] = await app.state.rt.add_voice_result(body.user_id, voice_reply(body.user_id, rec))
    cond = _voice_ready(body.user_id)
    async with cond:
        cond.notify_all()
    return out


@app.get("/liff/voice/draft", response_class=HTMLResponse)
async def liff_voice_draft(rid: int, request: Request, scenario_user: str | None = None):
    """音声相談で作った職務経歴書の下書きを表示する(トークの「職務経歴書の下書きを見る」のリンク先)。
    本人は Cookie から決める(URL にユーザーIDは無い)。"""
    user = _page_user(request, scenario_user)
    r = logic.voice_result(state, user, rid)
    if r is None or not r.get("draft_markdown"):
        return HTMLResponse(_page("職務経歴書の下書き", "<p>この下書きは見つかりません。</p>"), status_code=404)
    body = ("<p>音声の相談から作った下書きです。内容は必ずご自身で確認・修正してください。</p>"
            f"<pre style='white-space:pre-wrap;background:#fff;border-radius:10px;padding:12px;font-size:13px'>"
            f"{escape(r['draft_markdown'])}</pre>")
    return _page("職務経歴書の下書き", body)


# ----------------------------- 履歴書のアップロード(resume-intake) -----------------------------


# トークに出すファイルの表示名(ファイル名は氏名が入りがちなので出さない。種類だけ)
RESUME_LABEL = {"application/pdf": "履歴書(PDF)", "image/jpeg": "履歴書(画像)", "image/png": "履歴書(画像)"}


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
    """読み取り結果(resume_intake.schema.ResumeDraft の形)を読める HTML に。書いてある項目だけ出す。"""
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


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{margin:0;font-family:-apple-system,"Hiragino Sans",sans-serif;background:#f4f6f8;color:#111}}
header{{background:#06c755;color:#fff;padding:12px 16px;font-weight:700}}main{{padding:16px;max-width:480px;margin:auto}}
.job{{background:#fff;border-radius:10px;padding:12px;font-weight:700}}label{{display:block;margin:12px 0;font-size:14px}}
input{{display:block;width:100%;box-sizing:border-box;margin-top:6px;padding:10px;font-size:16px;border:1px solid #ccd;border-radius:8px}}
button{{width:100%;padding:12px;border:0;border-radius:8px;background:#06c755;color:#fff;font-size:16px;font-weight:700}}</style>
</head><body><header>{title}(LIFF・dip の画面)</header><main>{body}</main></body></html>"""


if __name__ == "__main__":
    import uvicorn

    # X-Forwarded-For は _client_ip だけが AICA_TRUST_PROXY に従って読む(uvicorn には書き換えさせない)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LINE_SIM_PORT", "8030")), proxy_headers=False)
