"""実 API での音声会話 E2E(マイク無し環境用)。

本人役の発話を TTS(OpenAI gpt-4o-mini-tts)で音声合成し、本番と同じ部品で1会話を回す:
- Deepgram STT(nova-3, ja + バイト用語の keyterm)で文字起こし(livekit プラグインの recognize)
- ResumeAdvisor(Claude Sonnet 5.5。VOICE_RESUME_PROVIDER=openai なら gpt-6-sol + dip MCP の recommend_jobs)をテキストターンで実行
- アドバイザーの返答を Cartesia TTS(sonic-3.5 日本語ボイス・少し遅め)で音声合成
- 終わりに finalize_interview → 履歴書の下書き・希望条件・求人IDが out/ に出る

LiveKit(WebRTC の転送・ターン判定)だけはここでは通らないので、ブラウザの手順
(README)で耳で確かめる。成果物は results/ に残る:

    ./.venv/bin/python scripts/voice/e2e_audio_session.py

--user <ユーザーID> を付けると、トークの「音声で相談」から入った相談として通す
(dip の要約を読み、希望を保存し、結果をトークへ戻す。line_runtime.server を 8030 で起動し、
画面でシナリオを始めたユーザー(例 `w-…:sim-new`)を渡すとトークに結果が届く):

    ./.venv/bin/python scripts/voice/e2e_audio_session.py --user sim-new
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # isort: skip

from livekit import rtc
from livekit.agents import AgentSession
from livekit.plugins import cartesia
from openai import AsyncOpenAI

from resume_draft.schema import JST
from voice_resume import config, voice_llm
from voice_resume.agent import prepare_advisor
from voice_resume.dip import DipMCP
from voice_resume.flow import verify_flow
from voice_resume.outputs import history_transcript
from voice_resume.stt import create_stt

RESULTS = config.RESULTS_DIR
FIXTURE = config.ROOT / "tests" / "voice" / "fixtures" / "script_transcript.json"
SAMPLE_RATE = 24000

# 価格(2026-09 時点の各社公式 pricing。1会話の実測量に掛けて概算する)
PRICE = {
    "llm_in_per_m": 2.00,  # claude-sonnet-5-5 と gpt-6-sol の入力 USD/1M tokens(キャッシュの読みも同じ単価で数える概算)
    "llm_out_per_m": 10.00,  # claude-sonnet-5-5 と gpt-6-sol の出力 USD/1M tokens
    "deepgram_per_min": 0.0077,  # nova-3 streaming USD/分の正価(live と同じ経路の単価)
    "cartesia_per_1k_chars": 0.04,  # Cartesia クレジット(1文字=1クレジット)のプラン換算の目安
    "user_tts_text_per_m": 0.60,  # gpt-4o-mini-tts(本人役の合成・検証用のみ)
    "user_tts_audio_per_m": 12.00,
}


def user_lines() -> list[str]:
    """台本(tests/fixtures/script_transcript.json)の本人の発話。最初に挨拶を足す。"""
    script = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ["こんにちは。アルバイトを探していて、相談したいです。"] + [
        t["text"] for t in script if t["role"] == "user"
    ]


async def synth_user_pcm(client: AsyncOpenAI, text: str) -> bytes:
    """本人役の発話を 24kHz/16bit mono PCM にする(検証用。本番はマイク)。"""
    resp = await client.audio.speech.create(
        model="gpt-4o-mini-tts", voice="alloy", input=text, response_format="pcm"
    )
    return resp.content


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm)


async def run(user: str | None = None) -> None:
    config.load_env()
    import os

    # 本人役の声の合成は OpenAI の TTS(プロバイダの設定によらない)
    for key in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY") + (() if config.PROVIDER == "openai" else ("ANTHROPIC_API_KEY",)):
        if not os.environ.get(key):
            raise SystemExit(f"{key} がありません(.env を確認)")

    # livekit プラグインをワーカー外(スクリプト)で使うための http セッション
    from livekit.agents.utils import http_context

    async with http_context.open():
        await _run_conversation(user)


async def _run_conversation(user: str | None = None) -> None:

    openai_client = AsyncOpenAI()
    stt = create_stt()
    tts = cartesia.TTS(
        model=config.TTS_MODEL,
        voice=config.TTS_VOICE,
        language=config.TTS_LANGUAGE,
        speed=config.TTS_SPEED,
        word_timestamps=False,  # 日本語 + sonic 系では未対応
        sample_rate=SAMPLE_RATE,
    )

    dip = DipMCP()
    await dip.start()
    # トークから入った相談として通すとき: ワーカー(voice_resume.agent)と同じ準備で、会話の前に
    # dip の要約から前回の希望と、トークでアップロードした履歴書を読み、確認形で使わせる
    if user:
        summary = await dip.get_user_conversation_summary(user)
        print(f"ユーザー: {user} / 前回の希望: {summary.get('stated_preferences') or 'なし'}"
              f" / 履歴書: {'あり' if summary.get('uploaded_resume') else 'なし'}")
    advisor, _ = await prepare_advisor(dip, user)

    session = AgentSession(
        llm=voice_llm.create_llm()
    )

    await session.start(agent=advisor)

    stt_audio_secs = 0.0
    lines = user_lines()
    for i, line in enumerate(lines, 1):
        pcm = await synth_user_pcm(openai_client, line)
        stt_audio_secs += len(pcm) / (SAMPLE_RATE * 2)
        frame = rtc.AudioFrame(
            data=pcm, sample_rate=SAMPLE_RATE, num_channels=1,
            samples_per_channel=len(pcm) // 2,
        )
        stt_event = await stt.recognize(buffer=frame)
        stt_text = stt_event.alternatives[0].text.strip() if stt_event.alternatives else ""
        print(f"[{i}/{len(lines)}] 台本: {line[:24]}… / STT: {stt_text[:24]}…")
        if not stt_text:
            stt_text = line  # 文字起こしが空でも会話は続ける(結果に記録される)
        await session.run(user_input=stt_text)

    # 台本の最後で本人が終わりを告げている。まだ保存されていなければ締めを促す
    if advisor.finalized is None:
        await session.run(user_input="今日はここまでで大丈夫です。下書きをお願いします。")

    transcript = history_transcript(session.history)
    llm_tokens = {"in": 0, "out": 0}
    for usage in session.usage.model_usage:
        if getattr(usage, "type", "") == "llm_usage":
            llm_tokens["in"] += usage.input_tokens
            llm_tokens["out"] += usage.output_tokens
    await session.aclose()
    await dip.aclose()

    # ---- アドバイザーの声(Cartesia・少し遅め)を合成して残す。live と同じ設定 ----
    assistant_lines = [t["text"] for t in transcript if t["role"] == "assistant"]
    tts_chars = sum(len(x) for x in assistant_lines)
    voice_pcm = bytearray()
    stream = tts.synthesize("。".join(assistant_lines[:3]))  # 冒頭3発話ぶんを耳の確認用に
    async for chunk in stream:
        voice_pcm.extend(bytes(chunk.frame.data))
    await tts.aclose()
    await stt.aclose()

    # ---- 流れの確認(オフラインのチェッカーと同じもの)----
    ok, found = verify_flow(transcript)

    # ---- 費用(実測量 × 単価)----
    llm_cost = llm_tokens["in"] * PRICE["llm_in_per_m"] / 1e6 + llm_tokens["out"] * PRICE["llm_out_per_m"] / 1e6
    stt_cost = stt_audio_secs / 60 * PRICE["deepgram_per_min"]
    cartesia_cost = tts_chars / 1000 * PRICE["cartesia_per_1k_chars"]
    draft_in = sum(len(t["text"]) for t in transcript) // 2 + 1500  # 下書き生成の概算(usage 非取得)
    draft_cost = draft_in * PRICE["llm_in_per_m"] / 1e6 + 2000 * PRICE["llm_out_per_m"] / 1e6
    user_tts_cost = (
        sum(len(x) for x in lines) / 4 * PRICE["user_tts_text_per_m"] / 1e6
        + stt_audio_secs / 60 * 1200 * PRICE["user_tts_audio_per_m"] / 1e6
    )
    total = llm_cost + stt_cost + cartesia_cost + draft_cost + user_tts_cost

    # ---- 保存 ----
    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"transcript-{stamp}.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULTS / f"transcript-{stamp}.md").write_text(
        "# 音声会話の文字起こし(E2E・実 API)\n\n"
        f"- 日時: {stamp} JST / STT: Deepgram {config.STT_MODEL}(ja) / "
        f"LLM: {config.LLM_MODEL} / TTS: Cartesia {config.TTS_MODEL}(speed={config.TTS_SPEED})\n"
        "- 本人役の音声は TTS(gpt-4o-mini-tts)で合成(マイク無し環境のため)。"
        "本人の行は Deepgram の文字起こし結果\n"
        f"- 流れの確認(希望→経験→できること→資格→確認→求人の提案): "
        f"{'OK' if ok else 'NG'}(検出: {' → '.join(found)})\n\n"
        + "\n\n".join(f"**{'本人' if t['role'] == 'user' else 'アドバイザー'}**: {t['text']}" for t in transcript)
        + "\n", encoding="utf-8")
    if advisor.finalized:
        (RESULTS / f"resume-draft-{stamp}.json").write_text(
            json.dumps(advisor.finalized["resume"], ensure_ascii=False, indent=2), encoding="utf-8")
        (RESULTS / f"resume-draft-{stamp}.md").write_text(advisor.finalized["markdown"], encoding="utf-8")
        (RESULTS / f"job-proposal-{stamp}.json").write_text(
            json.dumps(advisor.finalized["proposal"], ensure_ascii=False, indent=2), encoding="utf-8")
    write_wav(RESULTS / f"advisor-voice-{stamp}.wav", bytes(voice_pcm))
    (RESULTS / f"cost-{stamp}.md").write_text(
        "# E2E 1回の費用(実測量から計算)\n\n"
        f"- LLM({config.LLM_MODEL}): ${llm_cost:.4f}(in {llm_tokens['in']} / out {llm_tokens['out']} tokens)\n"
        f"- STT(Deepgram {config.STT_MODEL}, {stt_audio_secs:.1f}s): ${stt_cost:.4f}\n"
        f"- TTS(Cartesia {config.TTS_MODEL}, {tts_chars}文字, $0.04/1k文字換算): ${cartesia_cost:.4f}\n"
        f"- 下書き生成({config.DRAFT_MODEL}, 概算): ${draft_cost:.4f}\n"
        f"- 本人役の音声合成(gpt-4o-mini-tts・検証用のみ): ${user_tts_cost:.4f}\n"
        f"- LiveKit: この経路では未使用(ブラウザ経由の会話は Cloud 無料枠 5,000 接続分/月)\n"
        f"- 合計: ${total:.4f}\n", encoding="utf-8")

    print(f"保存: results/*-{stamp}.* / 流れ: {'OK' if ok else 'NG'} / 費用 ${total:.4f}")
    if advisor.finalized:
        print("out/:", ", ".join(advisor.finalized["files"]))
    if user:
        print("トークへの返送:", "OK" if advisor.delivered_to_talk else "失敗(line_runtime.server は起動している?)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="トークから入った相談として通すユーザーID(例: sim-new)")
    asyncio.run(run(ap.parse_args().user))
