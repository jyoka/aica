"""音声の会話の LLM(AgentSession の llm)を作る。プロバイダは config.PROVIDER(既定 anthropic。task 236)。

- anthropic: LiveKit Agents の Anthropic のプラグイン(livekit-plugins-anthropic。livekit-agents と同じ版)。
  思考は config.LLM_REASONING_EFFORT で決める(line_runtime.llm.thinking_kwargs。トークと同じ決まり)。既定の none は、
  Claude Sonnet 5.5 では thinking between_tools + effort low(思考を切る一番軽い設定。disabled は 400)、
  Claude Haiku 5.5 では disabled + low。プラグインは思考のブロックを読み上げず、次の呼び出しにも送り返さないので、
  深く考えさせる(adaptive)より、思考を切って速く返す方が合う。
  キャッシュの区切りは、ツールの最後、system の最後、最後の返事とその前の本人の発話(プラグインの caching="ephemeral")。
  音声の指示(区分の playbook つき)は長いので、2 回目の返事から前置きをキャッシュから読む(読みは入力の 0.1 倍)
- openai: 前のとおり gpt-6-sol の Chat Completions(store=False で応答を残さない。docs/design/privacy.md の 3.2)
"""

from __future__ import annotations

from typing import Any

# プラグインは読み込むときに livekit-agents に登録する(メインのスレッドで読む決まり)ので、ワーカーの起動のときに読む
from livekit.agents.types import NOT_GIVEN
from livekit.agents.utils import is_given
from livekit.plugins import anthropic as lk_anthropic
from livekit.plugins import openai as lk_openai

from line_runtime import llm as line_llm
from voice_resume import config


class ClaudeLLM(lk_anthropic.LLM):
    """プラグインの LLM に、思考の設定を足したもの。プラグインは thinking と output_config を引数に持たないので、
    毎回の呼び出し(chat)の extra_kwargs に入れる(messages.create の引数になる)。"""

    def __init__(self, *, thinking: dict, **kw: Any) -> None:
        super().__init__(**kw)
        self.thinking_kwargs = thinking

    def chat(self, *, extra_kwargs=NOT_GIVEN, **kw: Any):
        extra = dict(self.thinking_kwargs) | (dict(extra_kwargs) if is_given(extra_kwargs) else {})
        return super().chat(extra_kwargs=extra, **kw)


def create_llm():
    """AgentSession に渡す LLM。Anthropic の鍵(ANTHROPIC_API_KEY)が無ければ、プラグインが ValueError で止める。"""
    if config.PROVIDER == "openai":
        # Chat Completions の既定も残さないが、OpenAI に応答を残さないことを明示する(docs/design/privacy.md の 3.2)
        return lk_openai.LLM(model=config.LLM_MODEL, reasoning_effort=config.LLM_REASONING_EFFORT, store=False)
    return ClaudeLLM(
        model=config.LLM_MODEL,
        max_tokens=config.LLM_MAX_TOKENS,
        caching="ephemeral",
        thinking=line_llm.thinking_kwargs(config.LLM_MODEL, config.LLM_REASONING_EFFORT),
        # ツールの形を strict にしない。strict の形は省ける引数をすべて null との union にするので、アドバイザーのツール
        # (省ける引数が多い)では union の引数が 47 個になり、Anthropic の上限(16 個)を超えて毎回 400 になる
        # (2026-10-26 に実 API で確かめた)。引数はツールの中で確かめる(propose_jobs など)
        _strict_tool_schema=False,
    )
