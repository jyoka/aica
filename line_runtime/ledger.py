"""費用台帳: LLM の呼び出し1回を、誰の会話に使ったか(user_id)つきで1行の JSON(JSONL)に記録する。

複数の知人が同時に使う前提なので、費用の行には必ずユーザーIDが付く(誰の利用にいくらかかったかを後から数えられる)。
料金表はここが正で、評価の計測(evals/meter.py)も同じ表を使う。

ユーザー別・日別の集計(運営者向け、日付は日本時間・ユーザーは会話数の上限と同じ人単位):

    ./.venv/bin/python -m line_runtime.ledger                    # data/costs.jsonl を集計して表で出す
    ./.venv/bin/python -m line_runtime.ledger --date 2026-09-27  # その日だけ
    ./.venv/bin/python -m line_runtime.ledger --by-user-id --json  # シナリオユーザーごとに分けて JSON で
"""

from __future__ import annotations

import argparse
import json
import threading
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from line_runtime.quota import person, today

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


def load(path: Path) -> list[dict]:
    if not Path(path).is_file():
        return []
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def summarize(rows: list[dict], by_person: bool = True) -> list[dict]:
    """台帳の行を (日付(日本時間), ユーザー) ごとにまとめる。by_person なら「匿名ID:シナリオユーザー」を匿名IDにまとめる。"""
    groups: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    for r in rows:
        g = groups[(today(datetime.fromisoformat(r["at"])), person(r["user_id"]) if by_person else r["user_id"])]
        g["calls"] += 1
        g["input_tokens"] += r.get("input_tokens") or 0
        g["output_tokens"] += r.get("output_tokens") or 0
        g["cost_usd"] += r.get("cost_usd") or 0.0
    return [{"date": d, "user_id": u, **g, "cost_usd": round(g["cost_usd"], 6)} for (d, u), g in sorted(groups.items())]


def main(argv: list[str] | None = None) -> None:
    from line_runtime import config

    ap = argparse.ArgumentParser(description="費用台帳をユーザー別・日別(日本時間)に集計する")
    ap.add_argument("--path", type=Path, default=config.DATA_DIR / "costs.jsonl", help="費用台帳(JSONL)")
    ap.add_argument("--date", help="この日(YYYY-MM-DD、日本時間)だけ出す")
    ap.add_argument("--by-user-id", action="store_true", help="「匿名ID:シナリオユーザー」を匿名IDにまとめない")
    ap.add_argument("--json", action="store_true", help="JSON で出す")
    args = ap.parse_args(argv)
    out = [s for s in summarize(load(args.path), by_person=not args.by_user_id) if not args.date or s["date"] == args.date]
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    if not out:
        print(f"記録なし({args.path})")
        return
    width = max(len("user_id"), *(len(s["user_id"]) for s in out))
    print(f"{'date':<10}  {'user_id':<{width}}  {'calls':>6}  {'input':>10}  {'output':>8}  {'cost_usd':>10}")
    for s in out:
        print(f"{s['date']:<10}  {s['user_id']:<{width}}  {s['calls']:>6}  {s['input_tokens']:>10}  "
              f"{s['output_tokens']:>8}  {s['cost_usd']:>10.4f}")
    print(f"{'total':<10}  {'':<{width}}  {sum(s['calls'] for s in out):>6}  {sum(s['input_tokens'] for s in out):>10}  "
          f"{sum(s['output_tokens'] for s in out):>8}  {sum(s['cost_usd'] for s in out):>10.4f}")


if __name__ == "__main__":
    main()
