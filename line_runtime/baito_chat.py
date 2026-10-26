"""アルバイト・パートの相談(バイト探しの相棒。dip_mcp/baito)のターンで、モデルを呼ぶところ。

何を聞くか・いつ終えるかは dip のハーネスが決め(dip のツール baito_turn)、ここはハーネスが返したシステムプロンプトで
モデルを呼び、決まった JSON(BaitoOutput。PRD §8.7)を受け取るだけ。モデルの JSON は構造化出力で強制する。
選択肢(クイックリプライ)はモデルの choices ではなく、ハーネスの choices を使う(バンクの原文と NG の絞り込みのとおり)。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from line_runtime import config, llm

MAX_TOKENS = 4000  # 結果(条件カード + おすすめ3つ)が入る長さ
HISTORY_MESSAGES = 8  # モデルに渡す、直前のやりとり(リアクションと、言ったことの繰り返しを避けるため)


class SlotUpdate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    slot: str = Field(description="スロット名(質問の choice_effects にあるもの)")
    value: str | int | bool = Field(description="値(質問の choice_effects にある値。無ければ本人の言葉)")
    confidence: Literal["high", "low"]
    from_: str = Field(alias="from", description="質問の ID(review なら review)")


class BaitoOutput(BaseModel):
    """モデルが毎ターン返す JSON(PRD §9.1 の出力。ng_removals は §12-3 のために足した任意のキー)。"""

    reply: str = Field(description="本人に送る文")
    choices: list[str] = Field(description="次に聞く質問の選択肢(そのまま)")
    slot_updates: list[SlotUpdate] = Field(description="本人の最新の発言から読み取った条件")
    ng_updates: list[str] = Field(description="新しい NG")
    segment: Literal["student", "homemaker", "side_job", "unknown"]
    ng_removals: list[str] = Field(default_factory=list, description="本人が取り消した NG")

    def dump(self) -> dict:
        return self.model_dump(by_alias=True)


# (システムプロンプト, メッセージ) → (モデルの JSON, 使った量)。テストは偽物を渡す
BaitoLLM = Callable[[str, list[dict]], Awaitable[tuple[BaitoOutput, dict]]]


def default_llm() -> BaitoLLM:
    """LINE_SIM_PROVIDER のモデル(エージェントと同じ)を、思考を切って呼ぶ(1 ターンで 1〜2 回呼ぶので速さを優先)。"""
    client = None

    async def call(system: str, messages: list[dict]) -> tuple[BaitoOutput, dict]:
        nonlocal client
        client = client or llm.client()
        out, usage = await llm.parse(client, model=config.MODEL, system=system, messages=messages, schema=BaitoOutput,
                                     max_tokens=MAX_TOKENS, effort="none", with_usage=True)
        if out is None:
            raise ValueError("バイトの相談のモデルが決まった JSON を返しませんでした")
        return out, usage

    return call


def history(messages: list) -> list[dict]:
    """会話の記録(LangChain のメッセージ)を、モデルに渡す user / assistant の交互の列にする(直前の HISTORY_MESSAGES 件)。
    本人の発言と、画面に出した返信の吹き出しだけ。ツールの呼び出しと結果、AI への知らせ(profile_notice)は渡さない。"""
    out: list[dict] = []
    for m in messages:
        if m.type == "human" and not m.additional_kwargs.get("profile_notice") and isinstance(m.content, str):
            role, text = "user", m.content
        elif m.type == "ai" and not getattr(m, "tool_calls", None):
            role, text = "assistant", _bubbles(m.content)
        else:
            continue
        if not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + text
        else:
            out.append({"role": role, "content": text})
    out = out[-HISTORY_MESSAGES:]
    while out and out[0]["role"] != "user":  # Anthropic は user から始める
        out.pop(0)
    return out


def _bubbles(content) -> str:
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    try:
        return "\n".join(json.loads(content).get("bubbles", []))
    except (TypeError, ValueError, AttributeError):
        return content if isinstance(content, str) else ""
