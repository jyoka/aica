"""ローカルサーバー。会話ページの配信と、LiveKit の接続トークン発行。

    ./.venv/bin/uvicorn voice_resume.server:app --port 8090
    → http://127.0.0.1:8090 を開く → 「面談をはじめる」→ マイク許可 → 話す

音声そのものは ブラウザ ⇔ LiveKit Cloud ⇔ ワーカー(voice_resume.agent)が運ぶ。
このサーバーは API キー(LIVEKIT_API_KEY/SECRET)を預かり、ブラウザには
部屋ごとの接続トークンだけを渡す。
"""

from __future__ import annotations

import json
import os
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from livekit import api
from pydantic import BaseModel

from voice_resume import config

config.load_env()

app = FastAPI(title="voice-resume")

_STATIC = config.ROOT / "voice_resume" / "static"


class TokenIn(BaseModel):
    # トーク(line_runtime)の「音声で相談」から入ったときのユーザーID(匿名ユーザーID のスコープ
    # 「匿名ID:シナリオユーザー」)。単体で開いたときは無し
    user_id: str | None = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.post("/api/token")
async def create_token(body: TokenIn | None = None) -> dict:
    """ブラウザが LiveKit の部屋に参加するためのトークンを発行する(部屋は毎回新しく作る)。

    user_id が来たら参加者の identity と metadata に載せ、ワーカー(voice_resume.agent)が
    どのユーザーの相談かを知る(希望の保存と、トークへの結果の返送に使う)。
    """
    url = os.environ.get("LIVEKIT_URL")
    if not url or not os.environ.get("LIVEKIT_API_KEY") or not os.environ.get("LIVEKIT_API_SECRET"):
        raise HTTPException(
            500, "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET がありません(リポジトリ直下の .env に置く)"
        )
    user_id = body.user_id if body else None
    room = f"{config.LIVEKIT_ROOM_PREFIX}-{uuid.uuid4().hex[:8]}"
    token = (
        api.AccessToken()
        .with_identity(user_id or "guest")
        .with_name("本人")
        .with_metadata(json.dumps({"user_id": user_id}) if user_id else "")
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    return {"url": url, "token": token, "room": room, "user_id": user_id}
