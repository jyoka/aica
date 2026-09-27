"""Anthropic SDK の全 messages.create と、OpenAI SDK の全 responses.create / parse の呼び出しを計測する。

anthropic 1.x の非同期クライアントの with_raw_response(langchain-anthropic が使う)では parse() がコルーチンに
なったので、await してから usage を読む。

anthropic の Messages / AsyncMessages をクラスレベルで包むので、SDK を直接呼ぶ模擬ユーザー・ジャッジも
langchain-anthropic 経由のエージェントも同じ台帳に載る(OpenAI の Responses / AsyncResponses も同様)。計測専用 - 本番コードからは import しない。
"""

from __future__ import annotations

import inspect
import threading
import time
from dataclasses import asdict, dataclass

from anthropic.resources.beta.messages import AsyncMessages as AsyncBetaMessages
from anthropic.resources.beta.messages import Messages as BetaMessages
from anthropic.resources.messages import AsyncMessages, Messages
from openai.resources.responses import AsyncResponses, Responses

# $/1M tokens (input, output)。キャッシュ書込 1.25x / 読込 0.1x(GPT-6 も同じ)、web search は $10/1k 回。
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-5": (5.0, 25.0),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6-luna": (0.1, 0.5),
}
WEB_SEARCH_PER_CALL = 0.01


def _price(model: str) -> tuple[float, float]:
    for prefix, p in PRICES.items():
        if model.startswith(prefix):
            return p
    return (0.0, 0.0)


@dataclass
class Call:
    model: str
    seconds: float
    input_tokens: int
    output_tokens: int
    cache_write: int
    cache_read: int
    web_searches: int

    @property
    def cost(self) -> float:
        pin, pout = _price(self.model)
        return (
            (self.input_tokens + 1.25 * self.cache_write + 0.1 * self.cache_read) * pin
            + self.output_tokens * pout
        ) / 1e6 + self.web_searches * WEB_SEARCH_PER_CALL


class Meter:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._lock = threading.Lock()

    def _record(self, model: str, seconds: float, resp) -> None:
        if hasattr(resp, "parse") and not hasattr(resp, "usage"):  # with_raw_response(langchain-anthropic)
            resp = resp.parse()
        u = getattr(resp, "usage", None)
        if hasattr(u, "input_tokens_details"):  # OpenAI: input_tokens はキャッシュの読み書きを含む合計
            d = u.input_tokens_details
            read, write = d.cached_tokens or 0, getattr(d, "cache_write_tokens", 0) or 0
            call = Call(model=model, seconds=seconds, input_tokens=u.input_tokens - read - write,
                        output_tokens=u.output_tokens, cache_write=write, cache_read=read, web_searches=0)
            with self._lock:
                self.calls.append(call)
            return
        stu = getattr(u, "server_tool_use", None)
        call = Call(
            model=model,
            seconds=seconds,
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_write=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            cache_read=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            web_searches=int(getattr(stu, "web_search_requests", 0) or 0),
        )
        with self._lock:
            self.calls.append(call)

    def install(self) -> Meter:
        for cls in (Messages, BetaMessages):
            cls.create = self._wrap_sync(cls.create)
        for cls in (AsyncMessages, AsyncBetaMessages):
            cls.create = self._wrap_async(cls.create)
        Responses.create, Responses.parse = self._wrap_sync(Responses.create), self._wrap_sync(Responses.parse)
        AsyncResponses.create = self._wrap_async(AsyncResponses.create)
        AsyncResponses.parse = self._wrap_async(AsyncResponses.parse)
        return self

    def _wrap_sync(self, orig):
        meter = self

        def create(self_, *args, **kwargs):
            t0 = time.perf_counter()
            resp = orig(self_, *args, **kwargs)
            if not kwargs.get("stream"):
                meter._record(kwargs.get("model", "?"), time.perf_counter() - t0, resp)
            return resp

        return create

    def _wrap_async(self, orig):
        meter = self

        async def acreate(self_, *args, **kwargs):
            t0 = time.perf_counter()
            resp = await orig(self_, *args, **kwargs)
            if not kwargs.get("stream"):
                parsed = resp
                if hasattr(resp, "parse") and not hasattr(resp, "usage"):
                    parsed = resp.parse()
                    if inspect.isawaitable(parsed):
                        parsed = await parsed  # 結果は SDK 側でキャッシュされ、呼び出し元の parse() も同じものを返す
                meter._record(kwargs.get("model", "?"), time.perf_counter() - t0, parsed)
            return resp

        return acreate

    def mark(self) -> int:
        return len(self.calls)

    def summary(self, since: int = 0) -> dict:
        calls = self.calls[since:]
        by_model: dict[str, int] = {}
        for c in calls:
            by_model[c.model] = by_model.get(c.model, 0) + 1
        total_in = sum(c.input_tokens + c.cache_write + c.cache_read for c in calls)
        cache_read = sum(c.cache_read for c in calls)
        return {
            "llm_calls": len(calls),
            "by_model": by_model,
            "input_tokens": total_in,  # キャッシュの書き込み・読み込みを含む入力の合計
            "cache_write_tokens": sum(c.cache_write for c in calls),
            "cache_read_tokens": cache_read,
            # 入力のうちキャッシュから読めた割合。読めた分は 0.1 倍の料金になる
            "cache_hit_rate": round(cache_read / total_in, 3) if total_in else None,
            "output_tokens": sum(c.output_tokens for c in calls),
            "web_searches": sum(c.web_searches for c in calls),
            "cost_usd": round(sum(c.cost for c in calls), 4),
        }

    def dump(self, since: int = 0) -> list[dict]:
        return [asdict(c) | {"cost": round(c.cost, 5)} for c in self.calls[since:]]
