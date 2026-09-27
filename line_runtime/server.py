"""シミュレーター(Web): LINE 風トークルーム + シナリオ起動 + LIFF 画面(dip 側のモック)。

シナリオは評価と同じ一覧(evals/scenarios.json)。どれも AI 側の1通目から始まる。

    ./.venv/bin/python -m line_runtime.server   # http://127.0.0.1:8030

LIFF 画面と状態確認 API は本来 dip が持つ Web 画面なので、dip の State を直接使う。
エージェントとの会話は LineRuntime(MCP 経由)だけを通る。

マルチユーザー: 初回アクセスでブラウザごとに匿名ユーザーIDを発行し、Cookie で保持する(認証・招待制はしない)。
シナリオはブラウザ専用のユーザー「匿名ID:シナリオユーザー」で始めるので、複数の知人が同時に使っても
会話・長期メモリ・行動履歴・費用台帳が混線しない。

会話数の上限(line_runtime.quota): 1人1日あたりのターン数に上限があり、超えたら LLM を呼ばずに丁寧に打ち切る。
"""

from __future__ import annotations

import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from html import escape

from fastapi import FastAPI, Form, HTTPException, Request, Response
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
from line_runtime.quota import DailyQuota, daily_limit, limit_reply
from line_runtime.runtime import EVENTS, LineRuntime

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


class EventIn(BaseModel):
    user_id: str
    event: str
    text: str | None = None


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
    """トーク画面。初回アクセスならこのブラウザの匿名ユーザーIDを発行する(Cookie)。"""
    resp = FileResponse(STATIC / "talk.html")
    _issue(request, resp)
    return resp


@app.get("/api/me")
async def me(request: Request, response: Response):
    """このブラウザの匿名ユーザーID(無ければ発行)。"""
    return {"user_id": _issue(request, response)}


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
    facts = None
    if kind == "push":
        # 上限に達していたら、接触として記録する前(dip に聞く前)に送らないと決める
        if not quota.available(body.user_id):
            return {"sent": False, "reason": LIMIT_REASON}
        # ここは dip のバックエンドの代わり: 行動イベントを検知し、プッシュしてよいかを dip が決める。
        gate = logic.push_trigger(state, body.user_id, body.event)
        if not gate["allowed"]:
            return {"sent": False, "reason": gate["reason"]}
        facts = gate["facts"]
    if not quota.take(body.user_id):
        return limit_reply(entry)
    try:
        return await app.state.rt.handle(body.user_id, body.event, body.text, facts)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/event/stream")
async def event_stream(body: EventIn, request: Request):
    """1ターンの途中経過を1行1件の JSON で順に返す(画面のリアルタイム表示用)。
    start → (プッシュなら gate)→ llm / tool_start / tools … → reply。途中で失敗したら error。"""
    _require_own(request, body.user_id)
    if body.event not in EVENTS:
        raise HTTPException(400, f"event は {list(EVENTS)} のいずれか")
    _require_text(body)

    async def lines():
        entry, kind = EVENTS[body.event]
        yield _line({"kind": "start", "event": body.event, "entry": entry, "event_kind": kind, "model": config.MODEL})
        facts = None
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
        if not quota.take(body.user_id):  # 上限を超えたら LLM を呼ばずに案内だけ返す
            yield _line({"kind": "reply", **limit_reply(entry)})
            return
        try:
            async for ev in app.state.rt.handle_stream(body.user_id, body.event, body.text, facts):
                yield _line(ev)
        except Exception as e:  # noqa: BLE001  API・MCP のエラーも画面に出す(応答を返し始めた後なので HTTP のエラーにはできない)
            yield _line({"kind": "error", "message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(lines(), media_type="application/x-ndjson")


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
    """voice-resume が面談の終わりに送ってくる結果。求人は dip のカードの形(job_id で照合し直す)。"""
    line_user_id: str
    jobs: list[dict] = []
    draft_markdown: str = ""
    files: list[str] = []


@app.post("/api/voice/result")
async def voice_result_in(body: VoiceResultIn, request: Request):
    """音声アドバイザーの結果(提案した求人・履歴書の下書き)を預かる。トークが pull で受け取る。
    呼ぶのは voice-resume のバックエンド(Cookie なし)。Cookie を持つブラウザが他人のトークへ
    結果を差し込めないよう、ここも所有チェックを通す。"""
    _require_own(request, body.line_user_id)
    return logic.record_voice_result(state, body.line_user_id, body.jobs, body.draft_markdown, body.files)


@app.post("/api/voice/{user_id}/pull")
async def voice_pull(user_id: str, request: Request):
    """未受け取りの音声相談の結果を返し、受け取り済みにする(トークの画面が定期的に呼ぶ)。"""
    _require_own(request, user_id)
    return {"results": logic.pull_voice_results(state, user_id)}


@app.get("/liff/voice")
async def liff_voice(user: str, request: Request):
    """トークの「音声で相談」ボタンの飛び先。voice-resume の会話ページへユーザーIDつきで送る。"""
    _require_own(request, user)
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
