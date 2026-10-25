"""計測(evals/meter.py)のキャッシュの集計。LLM は使わない。"""

from __future__ import annotations

import pytest

from evals.meter import Call, Meter


def call(inp, write, read):
    return Call(model="claude-opus-5", seconds=1.0, input_tokens=inp, output_tokens=10, cache_write=write,
                cache_read=read, web_searches=0)


def test_summary_reports_cache_tokens_and_hit_rate():
    m = Meter()
    m.calls = [call(100, 900, 0), call(50, 0, 950)]  # 1回目はキャッシュを書き、2回目は読む
    s = m.summary()
    assert s["cache_write_tokens"] == 900 and s["cache_read_tokens"] == 950
    assert s["input_tokens"] == 2000
    assert s["cache_hit_rate"] == round(950 / 2000, 3)


def test_summary_without_calls_has_no_hit_rate():
    s = Meter().summary()
    assert s["llm_calls"] == 0 and s["cache_hit_rate"] is None


def test_openai_usage_splits_cache_out_of_input_tokens():
    from types import SimpleNamespace

    m = Meter()
    usage = SimpleNamespace(input_tokens=2000, output_tokens=10,
                            input_tokens_details=SimpleNamespace(cached_tokens=1500, cache_write_tokens=300))
    m._record("gpt-6-sol", 1.0, SimpleNamespace(usage=usage))
    c = m.calls[0]
    assert (c.input_tokens, c.cache_read, c.cache_write) == (200, 1500, 300)
    assert m.summary()["input_tokens"] == 2000
    assert round(c.cost, 6) == round((200 + 1.25 * 300 + 0.1 * 1500) * 2.0 / 1e6 + 10 * 10.0 / 1e6, 6)


def test_unknown_model_is_counted_as_zero_with_one_warning(monkeypatch):
    """料金表に無いモデルは $0 で数える(台帳が過少になる)ので、モデルごとに1回だけ警告する。知っているモデルでは警告しない。"""
    import warnings

    from line_runtime import ledger

    monkeypatch.setattr(ledger, "_UNPRICED", set())
    with pytest.warns(UserWarning, match="gpt-7-nova"):
        assert ledger.cost_usd("gpt-7-nova", 1000, 1000) == 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ledger.price("gpt-7-nova")  # 2回目は警告しない
        assert ledger.price("gpt-6-sol-2026-09-01") == (2.0, 10.0)


def test_claude_sonnet_and_haiku_5_5_are_priced_with_cache_reads_and_writes():
    """既定のモデル(task 235)の料金。名前は一番長く一致するもので引く(claude-sonnet-5-5 を claude-sonnet-5 で数えない)。
    キャッシュの読みは 0.1 倍(Sonnet 5.5 なら $0.20)、書き込みは 1.25 倍。Haiku 5.5 はプロンプトが 100K を超えると高い料金。"""
    from line_runtime import ledger

    assert ledger.price("claude-sonnet-5-5") == (2.0, 10.0) and ledger.price("claude-haiku-5-5") == (0.1, 0.5)
    sonnet = ledger.cost_usd("claude-sonnet-5-5", 1000, 100, cache_write=2000, cache_read=30000)
    assert sonnet == pytest.approx((1000 * 2.0 + 2000 * 2.5 + 30000 * 0.2 + 100 * 10.0) / 1e6)
    assert ledger.cost_usd("claude-haiku-5-5", 100_000, 1000) == pytest.approx((100_000 * 0.1 + 1000 * 0.5) / 1e6)
    assert ledger.cost_usd("claude-haiku-5-5", 90_000, 1000, cache_read=20_000) == pytest.approx(
        (90_000 * 0.5 + 20_000 * 0.05 + 1000 * 2.5) / 1e6)  # 合計 110K のプロンプト
    row = ledger.Ledger().record("w-0000000000000001", "message", "claude-sonnet-5-5",
                                 {"input_tokens": 33000, "output_tokens": 100, "cache_read": 30000, "cache_write": 2000})
    assert row["cost_usd"] == pytest.approx(sonnet, abs=1e-6)


def test_meter_counts_anthropic_parse_calls(monkeypatch):
    """模擬ユーザーとジャッジ(llm.parse → messages.parse)も計測に載る(parse は create を通らない)。"""
    import asyncio
    from types import SimpleNamespace

    from anthropic.resources import messages
    from anthropic.resources.beta import messages as beta
    from openai.resources.responses import AsyncResponses, Responses

    # install はクラスを書き換えるので、テストの後に元へ戻す
    for cls in (messages.Messages, messages.AsyncMessages, beta.Messages, beta.AsyncMessages, Responses, AsyncResponses):
        for name in ("create", "parse"):
            monkeypatch.setattr(cls, name, getattr(cls, name))

    async def fake_parse(self, **kwargs):
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=900))

    monkeypatch.setattr(messages.AsyncMessages, "parse", fake_parse)
    m = Meter().install()
    asyncio.run(messages.AsyncMessages.parse(None, model="claude-sonnet-5-5", max_tokens=10, messages=[]))
    assert [(c.model, c.input_tokens, c.cache_read) for c in m.calls] == [("claude-sonnet-5-5", 100, 900)]
