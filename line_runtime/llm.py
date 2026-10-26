"""SDK を直接呼ぶところ(模擬ユーザー・ジャッジ・ゲームの AI・データ生成・音声相談の下書き・履歴書の読み取り)の、
プロバイダの違いを吸収する。

エージェントは langchain 経由(agent.build_model)なのでここは使わない。OpenAI には応答を残さない(store=False。
docs/design/privacy.md の 3.2)。
effort は OpenAI では reasoning、Anthropic では思考(thinking)と output_config.effort にする(thinking_kwargs)。
Anthropic にはキャッシュの区切りを 2 つ付ける(cached_system と top-level の cache_control)。system の後の区切りは、
同じ指示で本人の答えだけが変わる呼び出し(ゲームの段、データ生成)が指示を読むため。最後の区切りは、会話が伸びる
模擬ユーザーと、同じ会話を 2 回見るジャッジが、前の呼び出しの分を読むため。512 トークンより短い前置きはキャッシュされない。
1 回しか送らない中身(履歴書のファイル、相談の文字起こし)は、parse の cache=False で区切りを付けない
(書き込みは入力の 1.25 倍で、読まれなければ高くなるだけ)。
"""

from __future__ import annotations

import os

from line_runtime import config

# Anthropic のキャッシュの区切り。5 分のキャッシュ(書き込みは入力の 1.25 倍、読みは 0.1 倍。line_runtime/ledger.py)
CACHE_CONTROL = {"type": "ephemeral"}
# 思考を切れるモデルと、切るときの thinking の値(2026-10 の Anthropic の資料)。Claude Sonnet 5.5 は disabled が 400 で、
# 一番軽いのは between_tools(ほかの欄は付けない)。Claude Haiku 5.5 は disabled を受ける。どちらも effort high 以下だけ
THINKING_OFF = {"claude-sonnet-5-5": {"type": "between_tools"}, "claude-haiku-5-5": {"type": "disabled"}}
# effort(output_config.effort)を受けるモデル。ほか(Claude Haiku 4.5 など)は effort を送ると 400 なので、何も付けない
EFFORT_MODELS = ("claude-sonnet-5", "claude-haiku-5", "claude-opus-5", "claude-fable-5")


def anthropic_key() -> bool:
    """Anthropic の鍵があるか(ANTHROPIC_API_KEY か ANTHROPIC_AUTH_TOKEN。SDK はどちらでも読む)。"""
    return any(os.environ.get(k, "").strip() for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))


def client(provider: str | None = None):
    """provider(anthropic / openai)の非同期のクライアント。省くと LINE_SIM_PROVIDER(config.PROVIDER)。"""
    if (provider or config.PROVIDER) == "openai":
        from openai import AsyncOpenAI

        return AsyncOpenAI()
    from anthropic import AsyncAnthropic

    return AsyncAnthropic()


def _is_openai(client) -> bool:
    return hasattr(client, "responses")


def cached_system(system: str) -> list[dict]:
    """Anthropic の system を、最後にキャッシュの区切りを付けたブロックにする。"""
    return [{"type": "text", "text": system, "cache_control": CACHE_CONTROL}]


def thinking_kwargs(model: str, effort: str) -> dict:
    """Anthropic に送る thinking と output_config(effort)。effort は OpenAI の reasoning と同じ名前で受ける。
    "none" は思考を切る(切れないモデルは思考を一番浅く: effort low)。ほかは思考を任せて(adaptive。thinking を
    送らないのと同じ)、その effort にする。400 になる組み合わせ(Sonnet 5.5 の disabled、xhigh 以上で思考を切る)は作らない。"""
    if not model.startswith(EFFORT_MODELS):
        return {}
    if effort != "none":
        return {"output_config": {"effort": effort}}
    off = next((v for k, v in THINKING_OFF.items() if model.startswith(k)), None)
    return {"output_config": {"effort": "low"}} | ({"thinking": dict(off)} if off else {})


async def parse(client, *, model: str, system: str, messages: list[dict], schema, max_tokens: int, effort: str = "none",
                with_usage: bool = False, cache: bool = True):
    """構造化出力(schema のインスタンス)を返す。with_usage なら (結果, 使った量) を返す。使った量は費用台帳
    (ledger.Ledger.record)の形で、input_tokens はキャッシュの読み書きを含む合計。
    Anthropic は構造化出力(output_config.format。SDK が output_format の型から作る)で受け取り、ツールの強制
    (tool_choice の any / tool。Claude Sonnet 5.5 では 400)は使わない。cache=False なら Anthropic にキャッシュの区切りを付けない。"""
    if _is_openai(client):
        resp = await client.responses.parse(model=model, instructions=system, input=messages, text_format=schema,
                                            max_output_tokens=max_tokens, reasoning={"effort": effort}, store=False)
        return (resp.output_parsed, usage_of(resp)) if with_usage else resp.output_parsed
    # messages.parse は top-level の cache_control を引数に持たないので、本文に足す(messages.create は持つ)
    cached = {"system": cached_system(system), "extra_body": {"cache_control": CACHE_CONTROL}} if cache else {"system": system}
    resp = await client.messages.parse(model=model, max_tokens=max_tokens, messages=messages, output_format=schema,
                                       **cached, **thinking_kwargs(model, effort))
    return (resp.parsed_output, usage_of(resp)) if with_usage else resp.parsed_output


def usage_of(resp) -> dict:
    """SDK の応答のトークン数を、費用台帳の形にする。OpenAI の input_tokens はキャッシュを含む合計、Anthropic の
    input_tokens はキャッシュを除いた分なので、読み書きを足して合計にそろえる。"""
    u = getattr(resp, "usage", None)
    if u is None:
        return {}
    out = getattr(u, "output_tokens", 0) or 0
    if hasattr(u, "cache_read_input_tokens") or hasattr(u, "cache_creation_input_tokens"):
        read = getattr(u, "cache_read_input_tokens", 0) or 0
        write = getattr(u, "cache_creation_input_tokens", 0) or 0
        return {"input_tokens": (getattr(u, "input_tokens", 0) or 0) + read + write, "output_tokens": out,
                "cache_read": read, "cache_write": write}
    details = getattr(u, "input_tokens_details", None)
    return {"input_tokens": getattr(u, "input_tokens", 0) or 0, "output_tokens": out,
            "cache_read": getattr(details, "cached_tokens", 0) or 0, "cache_write": 0}


async def text(client, *, model: str, system: str, messages: list[dict], max_tokens: int, effort: str = "none") -> str:
    if _is_openai(client):
        resp = await client.responses.create(model=model, instructions=system, input=messages,
                                             max_output_tokens=max_tokens, reasoning={"effort": effort}, store=False)
        return resp.output_text
    resp = await client.messages.create(model=model, max_tokens=max_tokens, system=cached_system(system), messages=messages,
                                        cache_control=CACHE_CONTROL, **thinking_kwargs(model, effort))
    return "".join(b.text for b in resp.content if b.type == "text")
