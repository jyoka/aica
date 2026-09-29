"""Web: 本番のトーク画面(/)+ 開発者画面(/dev: シナリオ起動・dip の状態・処理の流れ)+ LIFF 画面(dip 側のモック)。

本番の / は求職者が使うトーク1画面。シナリオを選ばせず、開いたブラウザの匿名ユーザー(匿名ID そのもの)について
最後のやりとりから30分以内なら今の会話を続け(/api/talk/history で履歴を描き直す。LLM は呼ばない)、
過ぎていれば dip が初回訪問か再訪かを決め(/api/talk/open)、AI 側の1通目から新しい会話を始める。
/dev のシナリオは評価と同じ一覧(evals/scenarios.json)。どれも AI 側の1通目から始まる。

    ./.venv/bin/python -m line_runtime.server   # http://127.0.0.1:8030

LIFF 画面と状態確認 API は本来 dip が持つ Web 画面なので、dip の State を直接使う。
エージェントとの会話は LineRuntime(MCP 経由)だけを通る。

マルチユーザー: 初回アクセスでブラウザごとに匿名ユーザーIDを発行し、Cookie で保持する(認証・招待制はしない)。
本番の / は匿名ID そのものをユーザーIDにする。/dev のシナリオはブラウザ専用のユーザー「匿名ID:シナリオユーザー」で始めるので、複数の知人が同時に使っても
会話・長期メモリ・行動履歴・費用台帳が混線しない。

会話数の上限(line_runtime.quota): 1人1日あたりのターン数に上限があり、超えたら LLM を呼ばずに丁寧に打ち切る。
"""

from __future__ import annotations

import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel

from dip_mcp import logic
from dip_mcp.data import State, load_jobs
from evals.scenarios import load_scenarios, prepare
from line_runtime import config
from line_runtime.quota import LIMIT_MESSAGE, DailyQuota, daily_limit, limit_reply
from line_runtime.resume_confirm import resume_bubbles
from line_runtime.runtime import EVENTS, LineRuntime
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
    """このブラウザの匿名ユーザーID。無ければ発行して Cookie に保存する(再アクセスで同じ ID になる)。"""
    uid = _anon(request)
    if uid is None:
        uid = f"w-{secrets.token_hex(8)}"
        response.set_cookie(ANON_COOKIE, uid, max_age=ANON_COOKIE_MAX_AGE, samesite="lax", httponly=True)
    return uid


def _require_own(request: Request, user_id: str) -> None:
    """Cookie を持つブラウザは、自分のユーザー(匿名IDそのもの、または「匿名ID:シナリオユーザー」)だけを扱える。
    Cookie の無い呼び出し(テスト・評価・voice-resume のバックエンド)は従来どおり user_id を信頼する。"""
    anon = _anon(request)
    if anon and user_id != anon and not user_id.startswith(f"{anon}:"):
        raise HTTPException(403, "このブラウザのユーザーIDではありません")


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
    app.state.quota = DailyQuota(daily_limit(), config.DATA_DIR / "quota.json")
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
async def me(request: Request, response: Response):
    """このブラウザの匿名ユーザーID(無ければ発行)。"""
    return {"user_id": _issue(request, response)}


@app.post("/api/talk/open")
async def talk_open(request: Request, response: Response):
    """本番のトーク画面を開いた: ユーザーは匿名ID そのもの(シナリオ用ユーザーは使わない)。
    - 最後のやりとりから30分以内: 今の会話を続ける(event は None)。画面は /api/talk/history で履歴を描き直す。
      LLM は呼ばず、会話数も使わない。
    - それ以外: dip がそのユーザーの状態から初回訪問か再訪かを決める。続けて画面がそのイベントで
      /api/event/stream を呼び、AI 側の1通目(挨拶)で新しい会話が始まる。開いた記録は挨拶が返ったときに残す
      (返す途中で接続が切れて残せなかったら、次に開いたとき、保存された挨拶から補う)。
    thread は画面が表示する会話の番号(発言のときに送り返す。新しい会話なら挨拶のストリームの thread で置き換わる)。
    状態を初期に戻すことはしない。"""
    uid = _issue(request, response)
    rt = app.state.rt
    async with rt.user_lock(uid):  # 進行中のターンと交差しないように
        conv = await rt.conversation(uid)
    if conv["greeted"] and logic.talk_entry(state, uid) == "visit":
        # 挨拶は保存されたが、画面に返す途中で接続が切れて記録できなかった: 会った相手なので記録を補う
        logic.record_talk_open(state, uid)
    if conv["last_at"] is not None and clock() - conv["last_at"] <= TALK_RESUME_WINDOW:
        return {"user": uid, "event": None, "thread": conv["thread"]}
    return {"user": uid, "event": logic.talk_entry(state, uid), "thread": conv["thread"]}


@app.get("/api/talk/history/{user_id}")
async def talk_history(user_id: str, request: Request):
    """本番のトーク画面に描き直す、今の会話の履歴(吹き出し・求人カード・選択肢)。LLM は呼ばない。
    読めるのは本人の匿名ID(Cookie)だけ(Cookie の無い呼び出しも断る)。"""
    if _anon(request) != user_id:
        raise HTTPException(403, "このブラウザのユーザーIDではありません")
    rt = app.state.rt
    async with rt.user_lock(user_id):
        conv = await rt.conversation(user_id)
    return {"thread": conv["thread"], "turns": conv["turns"]}


@app.get("/api/saved/{user_id}")
async def saved(user_id: str, request: Request):
    """そのユーザーが保存した求人(本番のトーク画面の「保存した求人」)。"""
    _require_own(request, user_id)
    return {"jobs": logic.saved_jobs(state, user_id)}


@app.get("/api/scenarios")
async def scenarios():
    return [{k: sc.get(k) for k in ("id", "title", "desc", "user", "event")} for sc in load_scenarios()]


@app.post("/api/scenarios/{scenario_id}/start")
async def start_scenario(scenario_id: str, request: Request):
    """シナリオを最初から始める: そのユーザーの dip の状態を初期に戻す。続けて画面が /api/event/stream を呼び、
    AI 側の1通目(挨拶・プッシュ)が届く。新しい会話は、どのシナリオでも最初のイベント(visit・revisit・プッシュ)が
    始めるので、ここでは始めない(ここでも始めると会話番号が1つ飛び、再起動後に前の会話へ戻ってしまう)。
    進行中のターンが終わってから初期化する(終わったターンの記録が、初期化した状態に書き込まれないように)。
    Cookie を持つブラウザには、そのブラウザ専用のユーザー「匿名ID:シナリオユーザー」を作って始める
    (同じシナリオを2人が同時に使っても混線しない)。"""
    sc = next((s for s in load_scenarios() if s["id"] == scenario_id), None)
    if sc is None:
        raise HTTPException(404, f"シナリオ {scenario_id} はない")
    anon = _anon(request)
    user = f"{anon}:{sc['user']}" if anon else sc["user"]
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
    quota = app.state.quota
    facts = _upload_facts(body)
    if kind == "push":
        # 上限に達していたら、接触として記録する前(dip に聞く前)に送らないと決める
        if not quota.available(body.user_id):
            return {"sent": False, "reason": LIMIT_REASON}
        # ここは dip のバックエンドの代わり: 行動イベントを検知し、プッシュしてよいかを dip が決める。
        gate = logic.push_trigger(state, body.user_id, body.event)
        if not gate["allowed"]:
            return {"sent": False, "reason": gate["reason"]}
        facts = gate["facts"]
    stale = await _stale(body)
    if stale is not None:  # 会話数を使う前に断る(runtime もターンの排他の中で同じ照合をする)
        return {"stale": True, "thread": stale}
    if not quota.take(body.user_id):
        return limit_reply(entry)
    try:
        r = await app.state.rt.handle(body.user_id, body.event, body.text, facts, talk_thread=body.thread)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if r and r.get("stale"):  # 事前の照合の後に別のタブが会話を始めていた: LLM は呼んでいないので会話数を返す
        quota.refund(body.user_id)
    _record_open(body, r)
    return r


@app.post("/api/event/stream")
async def event_stream(body: EventIn, request: Request):
    """1ターンの途中経過を1行1件の JSON で順に返す(画面のリアルタイム表示用)。
    start → (プッシュなら gate)→ llm / tool_start / tools … → reply。途中で失敗したら error。"""
    _require_own(request, body.user_id)
    if body.event not in EVENTS:
        raise HTTPException(400, f"event は {list(EVENTS)} のいずれか")
    _require_text(body)
    upload = _upload_facts(body)

    async def lines():
        entry, kind = EVENTS[body.event]
        yield _line({"kind": "start", "event": body.event, "entry": entry, "event_kind": kind, "model": config.MODEL})
        facts = upload
        quota = app.state.quota
        if kind == "push":
            if not quota.available(body.user_id):
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
        if not quota.take(body.user_id):  # 上限を超えたら LLM を呼ばずに案内だけ返す
            yield _line({"kind": "reply", **limit_reply(entry)})
            return
        try:
            async for ev in app.state.rt.handle_stream(body.user_id, body.event, body.text, facts, talk_thread=body.thread):
                if ev["kind"] == "reply":
                    _record_open(body, ev)
                elif ev["kind"] == "stale":  # 事前の照合の後に別のタブが会話を始めていた: 会話数を返す
                    quota.refund(body.user_id)
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


def _record_open(body: EventIn, reply: dict | None) -> None:
    """本番のトーク画面の挨拶が返ったら、開いたことを dip に記録する(次からは再訪)。"""
    if body.thread is not None and EVENTS[body.event][1] == "greet" and reply and not reply.get("stale"):
        logic.record_talk_open(state, body.user_id)


def _upload_facts(body: EventIn) -> dict | None:
    """履歴書のアップロードのターンに渡す事実: ファイルの表示名・トークに出した確認の吹き出し・読み取り結果の画面。
    どれも dip に保存した読み取り結果から作る(画面からは受け取らない。ファイル名はどこにも無い)。
    読み取り結果が無ければ、会話数を使う前に断る。"""
    if EVENTS[body.event][1] != "upload":
        return None
    rec = logic.resume_upload(state, body.user_id)
    if rec is None:
        raise HTTPException(400, "アップロードした履歴書がありません")
    return {"file": rec.get("file") or RESUME_LABEL["application/pdf"], "shown": resume_bubbles(rec["resume"]),
            "view_url": f"/liff/resume?user={body.user_id}"}


def _require_text(body: EventIn) -> None:
    """空の発言は、会話数を使う前に断る(上限の1回分を無駄にしない)。"""
    if EVENTS[body.event][1] == "user" and not (body.text or "").strip():
        raise HTTPException(400, f"{body.event} には空でない text が必要です")


def _line(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


@app.get("/api/dip/{user_id}")
async def dip_view(user_id: str, request: Request):
    """dip 側から見たユーザー状態(デバッグ用): 要約と記録された会話結果。"""
    _require_own(request, user_id)
    u = state.user(user_id)
    return {"summary": logic.conversation_summary(state, user_id), "outcomes": u.get("outcomes", []),
            "saves": u.get("saves", []), "applications": u.get("applications", []),
            "last_contact": (u.get("contacts") or [None])[-1]}


# ----------------------------- 音声相談(voice-resume との連携) -----------------------------

class VoiceResultIn(BaseModel):
    """voice-resume が面談の終わりに送ってくる結果。求人は dip のカードの形(job_id で照合し直す)。
    user_id は「音声で相談」を押したトークのユーザー(匿名ユーザーID のスコープ「匿名ID:シナリオユーザー」)。"""
    user_id: str
    jobs: list[dict] = []
    draft_markdown: str = ""
    files: list[str] = []


@app.post("/api/voice/result")
async def voice_result_in(body: VoiceResultIn, request: Request):
    """音声アドバイザーの結果(提案した求人・履歴書の下書き)を預かる。トークが pull で受け取る。
    呼ぶのは voice-resume のバックエンド(Cookie なし)。Cookie を持つブラウザが他人のトークへ
    結果を差し込めないよう、ここも所有チェックを通す。"""
    _require_own(request, body.user_id)
    return logic.record_voice_result(state, body.user_id, body.jobs, body.draft_markdown, body.files)


@app.post("/api/voice/{user_id}/pull")
async def voice_pull(user_id: str, request: Request):
    """未受け取りの音声相談の結果を返し、受け取り済みにする(トークの画面が定期的に呼ぶ)。"""
    _require_own(request, user_id)
    return {"results": logic.pull_voice_results(state, user_id)}


@app.get("/liff/voice")
async def liff_voice(user: str, request: Request):
    """トークの「音声で相談」ボタンの飛び先。voice-resume の会話ページへユーザーIDつきで送る。
    音声セッションも実 API を使うので、会話数上限を1ターン消費してから通す。"""
    _require_own(request, user)
    if not app.state.quota.take(user):  # 上限を超えたら音声ページへ送らずに案内だけ返す
        return HTMLResponse(_page("音声相談", f"<p>{LIMIT_MESSAGE}</p>"), status_code=429)
    return RedirectResponse(f"{config.VOICE_RESUME_URL}/?user={user}")


@app.get("/liff/voice/draft", response_class=HTMLResponse)
async def liff_voice_draft(user: str, rid: int, request: Request):
    """音声相談で作った履歴書の下書きを表示する(トークの結果メッセージのリンク先)。"""
    _require_own(request, user)
    r = logic.voice_result(state, user, rid)
    if r is None or not r.get("draft_markdown"):
        return HTMLResponse(_page("履歴書の下書き", "<p>この下書きは見つかりません。</p>"), status_code=404)
    body = ("<p>音声の相談から作った下書きです。内容は必ずご自身で確認・修正してください。</p>"
            f"<pre style='white-space:pre-wrap;background:#fff;border-radius:10px;padding:12px;font-size:13px'>"
            f"{escape(r['draft_markdown'])}</pre>")
    return _page("履歴書の下書き", body)


# ----------------------------- 履歴書のアップロード(resume-intake) -----------------------------


# トークに出すファイルの表示名(ファイル名は氏名が入りがちなので出さない。種類だけ)
RESUME_LABEL = {"application/pdf": "履歴書(PDF)", "image/jpeg": "履歴書(画像)", "image/png": "履歴書(画像)"}


def resume_client():
    """履歴書の読み取りに使う OpenAI のクライアント(テストでは固定応答のスタブに差し替える)。"""
    from openai import AsyncOpenAI

    return AsyncOpenAI()


@app.post("/api/resume/{user_id}/upload")
async def resume_upload(user_id: str, request: Request, file: Annotated[UploadFile, File()], thread: int | None = None):
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
    if not app.state.quota.take(user_id):  # アップロードも1ターン扱い: 上限を超えたら読み取らずに案内だけ返す
        raise HTTPException(429, LIMIT_MESSAGE)
    try:
        draft, usage = await extract_bytes(name, data, resume_client())
    except Exception as e:  # API のエラーは画面に理由を出す
        raise HTTPException(502, f"履歴書を読み取れませんでした: {type(e).__name__}") from e
    app.state.rt.ledger.record(user_id, "resume_upload", resume_config.MODEL,
                               {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens})
    result = finalize(draft)
    empty = draft.model_dump(exclude={"unconfirmed"}) == type(draft)().model_dump(exclude={"unconfirmed"})
    if empty:  # 読めなかったファイルで、前に読み取れた結果を上書きしない
        return {"result": result, "empty": True, "view_url": None, "file": label,
                "bubbles": resume_bubbles(result["resume"], empty=True)}
    rec = logic.save_resume_upload(state, user_id, result, file=label)
    # トークに出す確認は要点だけ(80字以内の吹き出し)。全文は view_url の「読み取り結果を見る」で見せる。
    # 画面は続けて resume_upload のイベントで会話を進める(AI が確認を踏まえて次の1問を返す)
    return {"result": rec, "empty": False, "view_url": f"/liff/resume?user={user_id}", "file": label,
            "bubbles": resume_bubbles(rec["resume"])}


@app.get("/liff/resume", response_class=HTMLResponse)
async def liff_resume(user: str, request: Request):
    """アップロードした履歴書の読み取り結果を表示する(トークの「読み取り結果を見る」のリンク先)。"""
    _require_own(request, user)
    rec = logic.resume_upload(state, user)
    if rec is None:
        return HTMLResponse(_page("履歴書の読み取り結果", "<p>読み取った履歴書はありません。</p>"), status_code=404)
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
async def liff_form(user: str, job: str, request: Request):
    _require_own(request, user)
    j = load_jobs().get(job)
    exit_ = logic.form_exit(state, user)
    if j is None or exit_ is None or exit_["job_id"] != job:
        return HTMLResponse(_page("応募フォーム", "<p>入力途中の応募はありません。</p>"), status_code=404)
    fields = "".join(
        f'<label>{escape(f)}<input name="f_{escape(f)}" required placeholder="(シミュレーションなので架空の値で)"></label>'
        for f in exit_["remaining_fields"])
    body = (f"<p class='job'>{escape(j.title)}<br><small>{escape(j.employer)}</small></p>"
            f"<p>入力済み: {escape('、'.join(exit_['completed_fields']))}</p>"
            f"<form method='post'><input type='hidden' name='user' value='{escape(user)}'>"
            f"<input type='hidden' name='job' value='{escape(job)}'>{fields}<button>応募する</button></form>")
    return _page("応募の続き", body)


@app.post("/liff/apply", response_class=HTMLResponse)
async def liff_submit(request: Request, user: str = Form(...), job: str = Form(...)):
    _require_own(request, user)
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

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LINE_SIM_PORT", "8030")))
