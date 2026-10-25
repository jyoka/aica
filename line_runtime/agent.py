"""LINE 側ランタイム(LY Runtime のスタンドイン)のエージェント。

LINE が持つのは「会話の声・安全・入口」。事業者(dip)の業務知識は一切持たず、MCP で受け取った
Prompt(シナリオ)と Tool だけで動く。ここにバイトル固有のルールを書かないことが、このシミュレーションの肝。
"""

from __future__ import annotations

from typing import Literal, NotRequired

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.agents.structured_output import ProviderStrategy
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
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
- actions: トークの操作を差し出すボタン(0〜2個)。resume_upload = 履歴書・職務経歴書のファイルを選ぶ、
  voice_consult = 音声相談を始める。事業者のシナリオが案内する場面で、下の「窓口」で使えるときだけ付ける。quick_replies(押すと本人の発言として
  送られる選択肢)とは別で、押しても発言は送られない。
"""


class Button(BaseModel):
    label: str = Field(description="ボタンの文言(20字以内)")
    url: str = Field(description="事業者のツールが返した URL")


class LineReply(BaseModel):
    bubbles: list[str] = Field(description="送る吹き出し(1〜3個)")
    quick_replies: list[str] = Field(default_factory=list, description="クイックリプライ(0〜4個、各20字以内)")
    job_ids: list[str] = Field(default_factory=list, description="求人カードとして表示する求人ID(0〜3件)")
    buttons: list[Button] = Field(default_factory=list, description="画面を開くボタン")
    actions: list[Literal["resume_upload", "voice_consult"]] = Field(
        default_factory=list, description="トークの操作(resume_upload: 履歴書を選ぶ / voice_consult: 音声相談を始める)")


class LineState(AgentState):
    entry: NotRequired[str]
    line_user_id: NotRequired[str]
    channel: NotRequired[str]  # 今の窓口(CHANNELS)。無ければ web
    channel_features: NotRequired[list[str]]  # LINE のトークから LIFF 画面で使えるもの(line_channel.liff_features)


# 今の窓口。web = Web のトーク画面(LINE 風の画面)、line = LINE 公式アカウントのトーク
CHANNELS = ("web", "line")
WEB_NOTE = "Web のトーク画面(LINE 風の画面)。actions の resume_upload と voice_consult は、トークの中のボタンになり、この画面で使える。"
LINE_NOTE = ("LINE のトーク。このトークには文の発言だけが届く(ファイル・写真・音声は届かない)。"
             "ファイルや写真をこのトークに送るようには言わない。")
# LINE で使えるもの → 使えるときの案内 / 使えないときの案内
LINE_FEATURES = {
    "resume_upload": ("resume_upload を付けると、履歴書・職務経歴書のファイルを読み込む LINE の画面(LIFF)を開くボタンになる。",
                      "履歴書・職務経歴書のファイルの読み込みは、LINE では使えない。勧めず、resume_upload も付けない。経歴は文で聞く。"),
    "voice_consult": ("voice_consult を付けると、音声相談の LINE の画面(LIFF)を開くボタンになる。",
                      "音声相談は、LINE では使えない。勧めず、voice_consult も付けない。"),
    "arcade": (("summary の arcade_url(レジュメ・アーケード)は、LINE の画面(LIFF)で開くボタンになる。ゲームの AI と下書きの保存も"
                "この本人として使え、保存するとこのトークに知らせが届く。"),
               "レジュメ・アーケード(/liff/arcade)は、LINE では開けない。勧めず、ボタンも付けない。"),
}


def channel_note(channel: str | None, features: list[str] | tuple[str, ...] = ()) -> str:
    """システムプロンプトに付ける、今の窓口と、そこで使えるもの・使えないもの。"""
    if channel != "line":
        return WEB_NOTE
    return "\n- ".join([LINE_NOTE, *(on if k in features else off for k, (on, off) in LINE_FEATURES.items())])


class ScenarioMiddleware(AgentMiddleware):
    """会話の入口に応じた dip のシナリオ(MCP の Prompt)と、今の窓口(Web / LINE)と、ユーザーIDをシステムプロンプトに付ける。
    どれも会話の途中で変わらないので、プロンプトキャッシュを壊さない(窓口はシナリオの後に置き、シナリオまでは
    どの窓口の会話でも同じにする)。"""

    state_schema = LineState

    def __init__(self, scenarios: dict[str, str]) -> None:
        super().__init__()
        self.scenarios = scenarios

    async def awrap_model_call(self, request: ModelRequest, handler):
        entry = request.state.get("entry", "A")
        uid = request.state.get("line_user_id", "")
        note = channel_note(request.state.get("channel"), request.state.get("channel_features") or ())
        text = (f"{PLATFORM_PROMPT}\n# 事業者(dip)の対話シナリオ\n{self.scenarios.get(entry, self.scenarios['A'])}\n"
                f"# 窓口\n{note}\n"
                f"# このユーザー\nLINE ユーザーID: {uid}(ツールの line_user_id に使う)\n")
        return await handler(request.override(system_message=SystemMessage(text)))


class PinUserMiddleware(AgentMiddleware):
    """dip のツールの line_user_id を、この会話のユーザー(state の line_user_id)に固定する。
    LLM が別のユーザーIDを渡しても(プロンプトインジェクションなど)、他人の状態を読み書きさせない。
    line_user_id を引数に持つツールだけが対象。会話のユーザーが分からなければ、ツールを呼ばずにエラーを返す。"""

    state_schema = LineState

    async def awrap_tool_call(self, request, handler):
        tool = request.tool
        if tool is None or "line_user_id" not in (tool.args or {}):
            return await handler(request)
        uid = (request.state or {}).get("line_user_id")
        if not uid:
            return ToolMessage(content='{"error": "この会話のユーザーが分かりません"}',
                               tool_call_id=request.tool_call["id"], name=request.tool_call["name"], status="error")
        call = request.tool_call | {"args": {**(request.tool_call.get("args") or {}), "line_user_id": uid}}
        return await handler(request.override(tool_call=call))


# 1ターン(本人の入力1つ)にモデルを呼べる回数。評価の記録(evals/baseline の sim-*)では、1ターンのツールの呼び出しは
# 多くて5回(モデルは多くて6回)、平均すると1ターンにモデルを2回ほど呼ぶ。ふつうの会話は止めず、止まらないときの費用に
# 天井をかける(1回の出力は最大 16000 トークンなので、1ターンの出力も 16 万トークンまで)
MAX_MODEL_CALLS_PER_TURN = 10
LAST_CALL_NOTE = ("\n# このターンの残り\nこのターンにモデルを呼べるのは、これが最後です。ツールは呼ばず、"
                  "ここまでに分かったことで返信を返してください。\n")
TURN_LIMIT_REPLY = LineReply(bubbles=["すみません、うまくまとめられませんでした。", "もう一度、短く聞いていただけますか?"])


class TurnCallLimitMiddleware(AgentMiddleware):
    """1ターンのモデルの呼び出しを max_calls 回までにする(ツールを呼び続けて止まらないときの費用の天井)。
    数えるのは、最後の本人の入力より後の AI メッセージ(モデルの呼び出し1回で1つ増える。会話の記録から数えるので、
    ターンをまたいで持ち越さない)。最後の1回にはツールを呼ばずに返信するよう添え書きし、それでもツールを呼んだら
    モデルを呼ばずに TURN_LIMIT_REPLY でターンを終える(ふつうの返信と同じ形なので、画面にも会話の記録にもそのまま残る。
    LLM を呼んでいない印に additional_kwargs の turn_limit を付ける)。
    添え書きはシナリオの後に足すので、ScenarioMiddleware の内側に置く。"""

    def __init__(self, max_calls: int = MAX_MODEL_CALLS_PER_TURN) -> None:
        super().__init__()
        self.max_calls = max_calls

    async def awrap_model_call(self, request: ModelRequest, handler):
        msgs = request.state.get("messages", [])
        last_human = max((i for i, m in enumerate(msgs) if m.type == "human"), default=-1)
        done = sum(1 for m in msgs[last_human + 1:] if m.type == "ai")
        if done >= self.max_calls:
            msg = AIMessage(TURN_LIMIT_REPLY.model_dump_json(), additional_kwargs={"turn_limit": True})
            return ModelResponse(result=[msg], structured_response=TURN_LIMIT_REPLY)
        if done == self.max_calls - 1:
            request = request.override(system_message=SystemMessage(f"{request.system_message.content}{LAST_CALL_NOTE}"))
        return await handler(request)


def build_model(model: str | None = None, effort: str | None = None) -> BaseChatModel:
    if config.PROVIDER == "openai":
        # Chat Completions は reasoning とツール呼び出しを併用できないので Responses API を使う。キャッシュは自動。
        # OpenAI に応答を残さない(store=False。docs/design/privacy.md の 3.2)。前の応答の ID は使わず、毎回会話の全文を送る。
        # 残さない応答の reasoning は ID で参照できないので、暗号化した中身を受け取り、次の呼び出しでそのまま送り返す
        return ChatOpenAI(model=model or config.MODEL, max_tokens=16000, use_responses_api=True,
                          reasoning={"effort": effort or config.EFFORT},
                          store=False, include=["reasoning.encrypted_content"])
    return ChatAnthropic(model=model or config.MODEL, max_tokens=16000, thinking={"type": "adaptive"},
                         output_config={"effort": effort or config.EFFORT})


def build_agent(tools, scenarios: dict[str, str], model=None, checkpointer=None):
    return create_agent(
        model or build_model(),
        tools,
        # シナリオを差し込んでから(外側)、キャッシュ指定を付ける(内側)。OpenAI のモデルには何もしない。
        # 1ターンのモデルの呼び出しの回数に上限をかける(シナリオの後に最後の1回の添え書きを足す)。
        # ツールの line_user_id は会話のユーザーに固定する(LLM の渡す値は信じない)
        middleware=[ScenarioMiddleware(scenarios), TurnCallLimitMiddleware(),
                    AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"), PinUserMiddleware()],
        response_format=ProviderStrategy(LineReply),
        state_schema=LineState,
        checkpointer=checkpointer,
        name="line-ai-mode",
    )
