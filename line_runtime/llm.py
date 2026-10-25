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


async def parse(client, *, model: str, system: str, messages: list[dict], schema, max_tokens: int, effort: str = "none"):
    """構造化出力(schema のインスタンス)を返す。"""
    if _is_openai(client):
        resp = await client.responses.parse(model=model, instructions=system, input=messages, text_format=schema,
                                            max_output_tokens=max_tokens, reasoning={"effort": effort}, store=False)
        return resp.output_parsed
    resp = await client.messages.parse(model=model, max_tokens=max_tokens, system=system, messages=messages,
                                       output_format=schema)
    return resp.parsed_output


async def text(client, *, model: str, system: str, messages: list[dict], max_tokens: int, effort: str = "none") -> str:
    if _is_openai(client):
        resp = await client.responses.create(model=model, instructions=system, input=messages,
                                             max_output_tokens=max_tokens, reasoning={"effort": effort}, store=False)
        return resp.output_text
    resp = await client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages)
    return "".join(b.text for b in resp.content if b.type == "text")
