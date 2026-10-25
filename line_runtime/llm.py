"""SDK を直接呼ぶところ(模擬ユーザー・ジャッジ・データ生成)の、プロバイダの違いを吸収する。

エージェントは langchain 経由(agent.build_model)なのでここは使わない。effort は OpenAI の reasoning だけに効く
(Anthropic 側は従来どおり thinking なし)。OpenAI には応答を残さない(store=False。docs/design/privacy.md の 3.2)。
"""

from __future__ import annotations

from line_runtime import config


def client():
    if config.PROVIDER == "openai":
        from openai import AsyncOpenAI

        return AsyncOpenAI()
    from anthropic import AsyncAnthropic

    return AsyncAnthropic()


def _is_openai(client) -> bool:
    return hasattr(client, "responses")


async def parse(client, *, model: str, system: str, messages: list[dict], schema, max_tokens: int, effort: str = "none",
                with_usage: bool = False):
    """構造化出力(schema のインスタンス)を返す。with_usage なら (結果, 使った量) を返す。使った量は費用台帳
    (ledger.Ledger.record)の形で、input_tokens はキャッシュの読み書きを含む合計。"""
    if _is_openai(client):
        resp = await client.responses.parse(model=model, instructions=system, input=messages, text_format=schema,
                                            max_output_tokens=max_tokens, reasoning={"effort": effort}, store=False)
        return (resp.output_parsed, usage_of(resp)) if with_usage else resp.output_parsed
    resp = await client.messages.parse(model=model, max_tokens=max_tokens, system=system, messages=messages,
                                       output_format=schema)
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
    resp = await client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages)
    return "".join(b.text for b in resp.content if b.type == "text")
