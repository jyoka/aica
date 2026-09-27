"""STT(文字起こし)の作り分け。agent.py と scripts/e2e_audio_session.py が共用する。

既定は Deepgram nova-3(日本語)+ バイト用語の keyterm 登録(タスク 37 の比較で決定。
results/stt-eval-*.md)。環境変数で戻せる:

- VOICE_RESUME_STT_PROVIDER: deepgram(既定)/ openai
- VOICE_RESUME_STT_MODEL: nova-3(既定)。nova-2 に戻す、openai なら gpt-4o-transcribe など
- VOICE_RESUME_STT_KEYTERMS: 用語のカンマ区切り(空にすると登録しない)
"""

from __future__ import annotations

from livekit.agents import stt as lk_stt

from voice_resume import config

# OpenAI STT に渡す音声の説明(keyterm 相当のヒント)
_OPENAI_PROMPT_TEMPLATE = "アルバイト探しの相談の日本語音声です。{terms}などの言葉が出ます。"


def create_stt() -> lk_stt.STT:
    """config の STT 設定(provider・モデル・言語・用語)から STT を作る。"""
    if config.STT_PROVIDER == "openai":
        from livekit.plugins import openai as lk_openai

        prompt = None
        if config.STT_KEYTERMS:
            prompt = _OPENAI_PROMPT_TEMPLATE.format(terms="、".join(config.STT_KEYTERMS))
        return lk_openai.STT(
            model=config.STT_MODEL,
            language=config.STT_LANGUAGE,
            **({"prompt": prompt} if prompt else {}),
        )
    if config.STT_PROVIDER != "deepgram":
        raise ValueError(f"VOICE_RESUME_STT_PROVIDER が不正です: {config.STT_PROVIDER}")

    from livekit.plugins import deepgram

    kwargs = {}
    # keyterm は nova-3 系のみ(nova-2 に戻したときはプラグインが拒むので渡さない)
    if config.STT_KEYTERMS and config.STT_MODEL.startswith("nova-3"):
        kwargs["keyterm"] = list(config.STT_KEYTERMS)
    return deepgram.STT(model=config.STT_MODEL, language=config.STT_LANGUAGE, **kwargs)
