"""LiveKit の接続トークンの発行。トーク(line_runtime.server の POST /api/voice/{user}/start)が使う。

音声そのものは ブラウザ ⇔ LiveKit Cloud ⇔ ワーカー(voice_resume.agent)が運ぶ。
API キー(LIVEKIT_API_KEY/SECRET)はサーバー側に置き、ブラウザには部屋ごとの接続トークンだけを渡す。
トークンを誰に発行してよいか(Cookie の匿名ユーザーID と同じユーザーか)は呼び出し側が確かめる。
"""

from __future__ import annotations

import json
import os
import uuid

from voice_resume import config


class LiveKitNotConfigured(RuntimeError):
    """LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET のどれかが無い。"""


def configured() -> bool:
    return all(os.environ.get(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"))


def issue_token(user_id: str) -> dict:
    """ブラウザが LiveKit の部屋に参加するためのトークンを発行する(部屋は毎回新しく作る)。

    user_id(トークのユーザー。匿名ユーザーID、または「匿名ID:シナリオユーザー」)を参加者の identity と
    metadata に載せ、ワーカー(voice_resume.agent.participant_user_id)がどのユーザーの相談かを知る
    (希望の保存と、トークへの結果の返送に使う)。
    """
    if not configured():
        raise LiveKitNotConfigured(
            "LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET がありません(リポジトリ直下の .env に置く)")
    from livekit import api

    room = f"{config.LIVEKIT_ROOM_PREFIX}-{uuid.uuid4().hex[:8]}"
    token = (
        api.AccessToken()
        .with_identity(user_id)
        .with_name("本人")
        .with_metadata(json.dumps({"user_id": user_id}))
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    return {"url": os.environ["LIVEKIT_URL"], "token": token, "room": room}
