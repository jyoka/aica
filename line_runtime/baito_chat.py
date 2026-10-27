"""アルバイト・パートの相談(バイト探しの相棒。dip_mcp/baito)のターンで、モデルを呼ぶところ(PRD v4.1 §9 の LLM 層)。

何を聞くか・いつ終えるかは dip のハーネスが決める(dip のツール baito_turn)。ハーネスがモデルの出力を求めたら
(status=llm)、ここがそのシステムプロンプトでモデルを呼び、段ごとに決まった JSON を受け取る:
  extract(Stage1) = ExtractOut、say / review = ReplyOut({"reply"})、result = ResultOut。構造化出力で形を強制する。
出力の中身(スロットの値・確信度・返事の文)を確かめるのはハーネス(validate_extract / validate_reply / validate_result)。
API のタイムアウトとやり直しの間隔はここ(SDK)の受け持ちで、ハーネスは失敗を 1 回と数えるだけ(PRD §8-4)。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, Field

from line_runtime import config, llm

MAX_TOKENS = 2000  # 結果(ひとこととおすすめ 3 つ)が入る長さ
HISTORY_MESSAGES = 8  # 生成段に渡す、直前のやりとり(リアクションと、言ったことの繰り返しを避けるため)


class SlotUpdate(BaseModel):
    slot: str = Field(description="allowed_slots のキー")
    value: str | list[str] | None = Field(description="allowed_slots の値(キーか表示名)。合うものが無ければ本人の言葉そのまま")
    evidence: str | None = Field(description="発言から言い換えずに抜き出した部分")


class ExtractOut(BaseModel):
    """抽出(Stage1)の出力(PRD §8-5)。confidence は持たない(確信度はハーネスが決める)。"""

    slot_updates: list[SlotUpdate]
    ng_updates: list[str]
    ng_removals: list[str]
    segment: Literal["student", "homemaker", "side_job", "other"] | None
    intent: Literal["answer", "clarify", "show_result", "restart", "aside", "danger"]
    review_ack: Literal["all", "partial", "none"] | None


class ReplyOut(BaseModel):
    """生成(Stage3)の出力。"""

    reply: str


class Recommendation(BaseModel):
    job_id: str = Field(description="candidates の job_id")
    reason: str = Field(description="なぜ合うか(40字まで。金額・会社名を書かない)")


class BaitoLLMError(ValueError):
    """モデルが決まった JSON を返さなかった。使った量(usage)は持っておく(費用台帳に入れるため)。
    正社員の相談(line_runtime.career_chat)も同じ例外を使う。"""

    def __init__(self, usage: dict, what: str = "バイトの相談"):
        super().__init__(f"{what}のモデルが決まった JSON を返しませんでした")
        self.usage = usage


class ResultOut(BaseModel):
    """結果の出力(PRD §8-7)。"""

    reply: str
    recommendations: list[Recommendation]


SCHEMAS = {"extract": ExtractOut, "say": ReplyOut, "review": ReplyOut, "result": ResultOut}

# (段, システムプロンプト, メッセージ) → (モデルの JSON, 使った量)。テストは偽物を渡す
BaitoLLM = Callable[[str, str, list[dict]], Awaitable[tuple[dict, dict]]]


def default_llm(schemas: dict | None = None, what: str = "バイトの相談") -> BaitoLLM:
    """LINE_SIM_PROVIDER のモデル(エージェントと同じ)を、思考を切って呼ぶ(1 ターンで最大 4 回呼ぶので速さを優先)。
    schemas は段ごとの出力の形(既定はアルバイト・パートの SCHEMAS。正社員は line_runtime.career_chat.SCHEMAS)。"""
    client = None
    schemas = schemas or SCHEMAS

    async def call(stage: str, system: str, messages: list[dict]) -> tuple[dict, dict]:
        nonlocal client
        client = client or llm.client()
        out, usage = await llm.parse(client, model=config.MODEL, system=system, messages=messages, schema=schemas[stage],
                                     max_tokens=MAX_TOKENS, effort="none", with_usage=True)
        if out is None:
            raise BaitoLLMError(usage, what)
        return out.model_dump(), usage

    return call


def messages_for(stage: str, user_text: str, convo: list[dict] | None = None) -> list[dict]:
    """モデルに渡すメッセージ。抽出は本人の今の発言だけ(会話から推測で埋めない。PRD §9-3 の 1)。
    生成は直前のやりとりに今の発言を足したもの(脱線への1文の返事と、繰り返しを避けるため。§9-1)。"""
    said = user_text or "(はじめ)"
    if stage == "extract" or not convo:
        return [{"role": "user", "content": said}]
    out = [dict(m) for m in convo]
    if out[-1]["role"] == "user":
        out[-1]["content"] += "\n" + said
    else:
        out.append({"role": "user", "content": said})
    return out


class SyncLLM:
    """ハーネスの llm(extract / generate)を、同期で実モデルにつなぐ(PRD §13-1 P3 の測定。evals/baito_sim.py --real)。
    call は default_llm() と同じ形。used に段ごとの呼び出しと使った量を残す。
    生成段には、トーク(runtime の _harness_turn)と同じく直前のやりとりを渡す。呼ぶ側が 1 ターンごとに said で覚えさせる
    (渡さないと、ボタンの答えが前の質問と切り離されて読まれる。task 242 の P3 で、お店の行列の質問の答えが希死念慮と読まれ、
    生成の代替の原因だった。その選択肢は task 256 で「今すぐ抜けたい」にした)。"""

    def __init__(self, call: BaitoLLM, render_prompt, prompt_stage):
        self.call = call
        self.render_prompt = render_prompt
        self.prompt_stage = prompt_stage
        self.used: list[dict] = []
        self.convo: list[dict] = []
        self._loop = asyncio.new_event_loop()

    def said(self, role: str, text: str) -> None:
        """画面に出たやりとり(本人の発言・返信)を 1 つ覚える。"""
        _append(self.convo, role, text)

    def _run(self, stage: str, system: str, user_text: str):
        msgs = messages_for(stage, user_text, recent(self.convo))
        out, usage = self._loop.run_until_complete(self.call(stage, system, msgs))
        self.used.append({"stage": stage, **usage})
        return json.dumps(out, ensure_ascii=False)

    def extract(self, payload: dict):
        return self._run("extract", self.render_prompt("extract", payload), payload.get("user_text", ""))

    def generate(self, view: dict):
        stage = self.prompt_stage(view)
        return self._run(stage, self.render_prompt(stage, view), view.get("user_text", ""))

    def close(self) -> None:
        self._loop.close()


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
        _append(out, role, text)
    return recent(out)


def recent(convo: list[dict]) -> list[dict]:
    """直前の HISTORY_MESSAGES 件を、user から始まる形で返す(Anthropic は user から始める)。"""
    out = [dict(m) for m in convo[-HISTORY_MESSAGES:]]
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _append(out: list[dict], role: str, text: str) -> None:
    """同じ役が続いたら 1 つにまとめる(user と assistant を交互にする)。"""
    if not text:
        return
    if out and out[-1]["role"] == role:
        out[-1]["content"] += "\n" + text
    else:
        out.append({"role": role, "content": text})


def _bubbles(content) -> str:
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    try:
        return "\n".join(json.loads(content).get("bubbles", []))
    except (TypeError, ValueError, AttributeError):
        return content if isinstance(content, str) else ""
