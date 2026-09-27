"""費用台帳: LLM の呼び出し1回を、誰の会話に使ったか(user_id)つきで1行の JSON(JSONL)に記録する。

複数の知人が同時に使う前提なので、費用の行には必ずユーザーIDが付く(誰の利用にいくらかかったかを後から数えられる)。
料金表はここが正で、評価の計測(evals/meter.py)も同じ表を使う。
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

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


def price(model: str) -> tuple[float, float]:
    for prefix, p in PRICES.items():
        if model.startswith(prefix):
            return p
    return (0.0, 0.0)


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0,
             web_searches: int = 0) -> float:
    """input_tokens はキャッシュの読み書きを除いた分。"""
    pin, pout = price(model)
    return ((input_tokens + 1.25 * cache_write + 0.1 * cache_read) * pin
            + output_tokens * pout) / 1e6 + web_searches * WEB_SEARCH_PER_CALL


class Ledger:
    """1呼び出し1行の費用台帳。path が None なら記録はメモリだけ(テスト・in_memory の実行用)。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def record(self, user_id: str, event: str, model: str, usage: dict) -> dict:
        """usage は runtime._usage の形(input_tokens はキャッシュの読み書きを含む合計。無ければ 0 扱い)。"""
        read, write = usage.get("cache_read") or 0, usage.get("cache_write") or 0
        total_in, out = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
        row = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "user_id": user_id, "event": event,
               "model": model, "input_tokens": total_in, "output_tokens": out,
               "cache_read": read, "cache_write": write,
               "cost_usd": round(cost_usd(model, max(0, total_in - read - write), out, write, read), 6)}
        with self._lock:
            self.rows.append(row)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
