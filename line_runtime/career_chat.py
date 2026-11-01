"""正社員のヒアリング(dip_mcp/career)のターンで、モデルを呼ぶところ。形はアルバイト・パートの line_runtime/baito_chat.py と同じ。

何を聞くか・いつ終えるかは dip のハーネスが決める(dip のツール career_turn)。ハーネスがモデルの出力を求めたら
(status=llm)、ここがそのシステムプロンプトでモデルを呼び、段ごとに決まった JSON を受け取る:
  extract(Stage1) = ExtractOut、say(Stage3) = ReplyOut({"reply"})。c2(会話の層。dip_mcp/career/c2.py)の読む段は
  read = ReadOut(発言の働き act の並び)。まとめと結果はハーネスが作るので、モデルは呼ばない。
出力の中身(発言の部分文字列か、数が発言にあるか、ほめ言葉、問いが 2 つ)を確かめるのはハーネス(validate_extract / validate_reply)。
会話の記録の読み方(history)とメッセージの組み立て(messages_for)は baito_chat と同じものを使う。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from line_runtime import baito_chat
from line_runtime.baito_chat import BaitoLLM, ReplyOut


class SlotUpdate(BaseModel):
    slot: str = Field(description="使ってよい欄(allowed_slots)のキー")
    text: str = Field(description="発言から一字一句そのまま抜き出した部分")
    number: float | None = Field(description="発言に数字で書いてある数だけ。無ければ null")
    unit: str | None = Field(description="発言のままの単位(人、件、社、年、か月、万円 など)。無ければ null")
    choice: str | None = Field(description="発言がボタンのどれかと同じ意味のときだけ、そのラベル。無ければ null")


class ExtractOut(BaseModel):
    """読み取り(Stage1)の出力(dip_mcp/prompts/career/prompt_extract_ja.txt)。確信度はハーネスが決める。"""

    slot_updates: list[SlotUpdate]
    ng_updates: list[str]
    segment: Literal["same_path", "change", "return", "early"] | None
    intent: Literal["answer", "pass", "dont_know", "later", "clarify", "aside", "show_result", "more"]
    review_ack: Literal["all", "partial", "none"] | None


ActName = Literal["answer", "add", "correct", "unsure", "retract", "dont_know", "pass", "more", "clarify", "go_back", "show_result",
                  "restart", "later", "stop", "confirm", "switch", "chitchat", "ask_service", "ask_advice", "story", "complaint",
                  "falsify", "crisis", "danger", "emotion", "sensitive", "name"]


class ReadAct(BaseModel):
    """発言の働き 1 つ(dip_mcp/convo/acts.py の ACTS)。使わない引数は null。"""

    act: ActName
    slot: str | None = Field(description="値の act、retract、dont_know、pass の欄のキー(欄の一覧)。区分なら segment")
    text: str | None = Field(description="値の act: 発言から一字一句そのまま抜き出した、その欄の部分")
    number: float | None = Field(description="発言に数字で書いてある数だけ")
    unit: str | None = Field(description="発言のままの単位")
    choice: str | None = Field(description="今の質問への答えがボタンと同じ意味のときだけ、そのラベル")
    value: str | None = Field(description="slot が segment のときの区分のキー")
    to: str | None = Field(description="switch: 移れる入り口の名前")
    strength: Literal["want", "consider"] | None = Field(description="switch: 移りたいか、考えているか")
    feeling: Literal["anxious", "angry", "tired", "sad"] | None = Field(description="emotion: 気持ち")
    span: str | None = Field(description="sensitive と name: 発言の部分文字列")


class ReadOut(BaseModel):
    """会話の層の読む段の出力(CAREER_HARNESS_VERSION=c2。dip_mcp/prompts/convo/read_ja.txt と
    dip_mcp/prompts/career/prompt_read_ja.txt)。中身を確かめるのはハーネス(dip_mcp.convo.acts.validate_reading)。"""

    acts: list[ReadAct]
    hook: str | None = Field(description="相づちに使える本人の言葉(15 字まで)")
    mentions: list[str] = Field(description="候補に使える本人の言葉(1 つ 10 字まで)")
    ng: list[str] = Field(description="避けたいこと(発言からそのまま)")


SCHEMAS = {"extract": ExtractOut, "read": ReadOut, "say": ReplyOut}
WHAT = "正社員の相談"


def default_llm() -> BaitoLLM:
    return baito_chat.default_llm(SCHEMAS, WHAT)
