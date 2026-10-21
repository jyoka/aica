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
