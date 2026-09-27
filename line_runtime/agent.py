"""LINE 側ランタイム(LY Runtime のスタンドイン)のエージェント。

LINE が持つのは「会話の声・安全・入口」。事業者(dip)の業務知識は一切持たず、MCP で受け取った
Prompt(シナリオ)と Tool だけで動く。ここにバイトル固有のルールを書かないことが、このシミュレーションの肝。
"""

from __future__ import annotations

from typing import NotRequired

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.structured_output import ProviderStrategy
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from line_runtime import config

PLATFORM_PROMPT = """あなたは LINE 公式アカウントの AI アシスタント(AIモード)です。トークルームでユーザーと1対1で話します。
事業者から渡された「対話シナリオ」とツールに従って会話します。事業者のツールが返した事実以外を、事実として話しません。

LINE の共通ルール:
- 違法・危険・差別的な依頼には応じない。
- ユーザーの個人情報(電話番号・住所・氏名など)をトークで聞き出さない。
- 返信は LINE のメッセージ形式で返す(下の「返信の形式」)。

返信の形式:
- bubbles: 送る吹き出し(1〜3個)。1つの吹き出しは短く。
- quick_replies: タップで返せる選択肢(0〜4個、各20字以内)。
- job_ids: 求人カード(Flex)として表示する求人ID(0〜3件)。このターンまでに事業者のツールが返した求人のものだけ。
- buttons: 画面を開くボタン(事業者のツールが返した URL だけ)。
"""


class Button(BaseModel):
    label: str = Field(description="ボタンの文言(20字以内)")
    url: str = Field(description="事業者のツールが返した URL")


class LineReply(BaseModel):
    bubbles: list[str] = Field(description="送る吹き出し(1〜3個)")
    quick_replies: list[str] = Field(default_factory=list, description="クイックリプライ(0〜4個、各20字以内)")
    job_ids: list[str] = Field(default_factory=list, description="求人カードとして表示する求人ID(0〜3件)")
    buttons: list[Button] = Field(default_factory=list, description="画面を開くボタン")


class LineState(AgentState):
    entry: NotRequired[str]
    line_user_id: NotRequired[str]
    push_facts: NotRequired[dict]  # dip が許可したプッシュの根拠の事実(カードの描画にも使う)


class ScenarioMiddleware(AgentMiddleware):
    """会話の入口に応じた dip のシナリオ(MCP の Prompt)と、ユーザーIDをシステムプロンプトに付ける。
    どちらも会話の途中で変わらないので、プロンプトキャッシュを壊さない。"""

    state_schema = LineState

    def __init__(self, scenarios: dict[str, str]) -> None:
        super().__init__()
        self.scenarios = scenarios

    async def awrap_model_call(self, request: ModelRequest, handler):
        entry = request.state.get("entry", "A")
        uid = request.state.get("line_user_id", "")
        text = (f"{PLATFORM_PROMPT}\n# 事業者(dip)の対話シナリオ\n{self.scenarios.get(entry, self.scenarios['A'])}\n"
                f"# このユーザー\nLINE ユーザーID: {uid}(ツールの line_user_id に使う)\n")
        return await handler(request.override(system_message=SystemMessage(text)))


def build_model(model: str | None = None, effort: str | None = None) -> BaseChatModel:
    if config.PROVIDER == "openai":
        # Chat Completions は reasoning とツール呼び出しを併用できないので Responses API を使う。キャッシュは自動
        return ChatOpenAI(model=model or config.MODEL, max_tokens=16000, use_responses_api=True,
                          reasoning={"effort": effort or config.EFFORT})
    return ChatAnthropic(model=model or config.MODEL, max_tokens=16000, thinking={"type": "adaptive"},
                         output_config={"effort": effort or config.EFFORT})


def build_agent(tools, scenarios: dict[str, str], model=None, checkpointer=None):
    return create_agent(
        model or build_model(),
        tools,
        # シナリオを差し込んでから(外側)、キャッシュ指定を付ける(内側)。OpenAI のモデルには何もしない。
        middleware=[ScenarioMiddleware(scenarios), AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore")],
        response_format=ProviderStrategy(LineReply),
        state_schema=LineState,
        checkpointer=checkpointer,
        name="line-ai-mode",
    )
