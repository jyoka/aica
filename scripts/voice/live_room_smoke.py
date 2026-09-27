"""LiveKit の実経路の煙テスト(マイク無し環境用)。

ワーカー(`python -m voice_resume.agent dev`)を起動した状態で実行する。
LiveKit Cloud の部屋に参加者として入り、合成音声で1問だけ話して、
アドバイザーの音声が返ってくるか(= 転送・STT・ターン判定・TTS の実経路)を確かめる:

    ./.venv/bin/python -m voice_resume.agent dev   # 別タブで待受
    ./.venv/bin/python scripts/voice/live_room_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # isort: skip

import numpy as np
from livekit import api, rtc
from openai import AsyncOpenAI

from voice_resume import config

SAMPLE_RATE = 24000
FRAME_MS = 10
USER_LINE = "こんにちは。カフェの仕事を探しています。"


async def synth(text: str) -> bytes:
    client = AsyncOpenAI()
    resp = await client.audio.speech.create(
        model="gpt-4o-mini-tts", voice="alloy", input=text, response_format="pcm"
    )
    return resp.content


async def feed(source: rtc.AudioSource, pcm: bytes) -> None:
    """PCM を 10ms ずつリアルタイム速度で流す(マイクの代わり)。"""
    samples_per_frame = SAMPLE_RATE * FRAME_MS // 1000
    step = samples_per_frame * 2
    for i in range(0, len(pcm) - step + 1, step):
        frame = rtc.AudioFrame(
            data=pcm[i : i + step], sample_rate=SAMPLE_RATE,
            num_channels=1, samples_per_channel=samples_per_frame,
        )
        await source.capture_frame(frame)


async def run() -> None:
    config.load_env()
    import os

    url = os.environ.get("LIVEKIT_URL")
    if not url:
        raise SystemExit("LIVEKIT_URL がありません(.env)")
    room_name = f"{config.LIVEKIT_ROOM_PREFIX}-smoke-{uuid.uuid4().hex[:6]}"
    token = (
        api.AccessToken()
        .with_identity("smoke-user")
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )

    agent_speech_secs = 0.0
    speech_done = asyncio.Event()

    room = rtc.Room()

    @room.on("track_subscribed")
    def _on_track(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.create_task(read_audio(track))

    async def read_audio(track) -> None:
        nonlocal agent_speech_secs
        stream = rtc.AudioStream(track)
        async for ev in stream:
            f = ev.frame
            samples = np.frombuffer(bytes(f.data), dtype=np.int16)
            if len(samples) and np.abs(samples).mean() > 100:  # 無音でないフレームだけ数える
                agent_speech_secs += len(samples) / f.sample_rate
                if agent_speech_secs > 1.5:  # 1.5秒ぶん話したら「話した」と見なす
                    speech_done.set()

    await room.connect(url, token)
    print(f"部屋に接続: {room_name}(ワーカーがいれば数秒でアドバイザーが入室)")

    source = rtc.AudioSource(SAMPLE_RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )

    # 1) アドバイザーの挨拶を待つ
    try:
        await asyncio.wait_for(speech_done.wait(), timeout=45)
    except TimeoutError:
        raise SystemExit("NG: アドバイザーの挨拶の音声が届かない(ワーカーは起動しているか)")
    print(f"挨拶を受信({agent_speech_secs:.1f}s)。挨拶が終わるのを待って1問話します…")
    await asyncio.sleep(10)  # 挨拶の続きを待つ(途中で話して割り込みにしない)

    # 2) 1問話して、返答が来るか
    agent_speech_secs = 0.0
    speech_done.clear()
    await feed(source, await synth(USER_LINE))
    try:
        await asyncio.wait_for(speech_done.wait(), timeout=45)
    except TimeoutError:
        raise SystemExit("NG: 発話への返答の音声が届かない")
    print(f"OK: 返答を受信({agent_speech_secs:.1f}s)。実経路(転送・STT・ターン判定・TTS)は動いている")
    await room.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
