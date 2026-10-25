"""費用台帳: LLM の呼び出し1回を、誰の会話に使ったか(user_id)つきで1行の JSON(JSONL)に記録する。

複数の知人が同時に使う前提なので、費用の行には必ずユーザーIDが付く(誰の利用にいくらかかったかを後から数えられる)。
料金表はここが正で、評価の計測(evals/meter.py)も同じ表を使う。
ユーザーIDを持つのは ID_TTL(90 日)まで。過ぎた行は、期間で消す掃除(line_runtime.purge)が日ごと(日本時間)の
合計の 1 行(user_id は FOLDED、calls に元の行の数)に置き換える(fold_old。docs/design/privacy.md の 3.3)。
本人が「すべて消す」をしたら、その人の行のユーザーIDを FOLDED に置き換える(forget。金額とトークン数は残す)。
外した行も、90 日を過ぎたら日ごとの合計にまとめる。

ユーザー別・日別の集計(運営者向け、日付は日本時間・ユーザーは会話数の上限と同じ人単位):

    ./.venv/bin/python -m line_runtime.ledger                    # data/costs.jsonl を集計して表で出す
    ./.venv/bin/python -m line_runtime.ledger --date 2026-09-27  # その日だけ
    ./.venv/bin/python -m line_runtime.ledger --by-user-id --json  # シナリオユーザーごとに分けて JSON で
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import warnings
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from line_runtime.quota import person, today

# $/1M tokens (input, output)。キャッシュ書込 1.25x / 読込 0.1x(GPT-6 も同じ)、web search は $10/1k 回。
# モデル名は、いちばん長く一致する名前で引く(claude-sonnet-5-5 を claude-sonnet-5 の値で数えないように)。
# claude-sonnet-5-5 と claude-haiku-5-5 は 2026-10-26 の Anthropic の公表価格(Sonnet 5.5 のキャッシュの読みは $0.20 = 0.1x)
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-5-5": (0.1, 0.5),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-5": (5.0, 25.0),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6-luna": (0.1, 0.5),
}
# プロンプト(キャッシュの読み書きを含む入力の合計)が長いときの料金: モデル → (この長さを超えたら, (input, output))。
# Claude Haiku 5.5 は 100K トークンを超えると $0.50 / $2.50
LONG_PROMPT_PRICES = {"claude-haiku-5-5": (100_000, (0.5, 2.5))}
WEB_SEARCH_PER_CALL = 0.01
ID_TTL = timedelta(days=90)  # 行にユーザーIDを残す期間。過ぎたら日ごとの合計にする
FOLDED = "deleted"  # 日ごとの合計の行と、「すべて消す」をした人の行の user_id(どの人の分かは残さない)
DAILY_TOTAL = "daily_total"  # 日ごとの合計の行の event
_TOTALS = ("input_tokens", "output_tokens", "cache_read", "cache_write")


_UNPRICED: set[str] = set()


def price(model: str) -> tuple[float, float]:
    """料金表に無いモデルは $0 で数える。台帳が過少になるので、モデルごとに1回だけ警告を出す
    (モデル名を変えたときに PRICES を足し忘れていないか気づくため)。"""
    known = [prefix for prefix in PRICES if model.startswith(prefix)]
    if known:
        return PRICES[max(known, key=len)]
    if model not in _UNPRICED:
        _UNPRICED.add(model)
        warnings.warn(f"料金表(line_runtime/ledger.py の PRICES)に無いモデル {model!r} の費用を $0 で数えます",
                      stacklevel=2)
    return (0.0, 0.0)


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0,
             web_searches: int = 0) -> float:
    """input_tokens はキャッシュの読み書きを除いた分。"""
    pin, pout = price(model)
    long = next((v for k, v in LONG_PROMPT_PRICES.items() if model.startswith(k)), None)
    if long and input_tokens + cache_write + cache_read > long[0]:
        pin, pout = long[1]
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


    def fold_old(self, now: datetime) -> int:
        """now から ID_TTL より前の行を、日ごとの合計の行に置き換える(ファイルとメモリの両方)。ユーザーIDを外した行の数を返す。
        ファイルは全体を書き直してから置き換える(追記と同じロックの中なので、その間の記録は失わない)。"""
        cutoff = now - ID_TTL
        with self._lock:
            self.rows, n = fold(self.rows, cutoff)
            if self.path and self.path.is_file():
                before = load(self.path)
                rows, n = fold(before, cutoff)
                if rows is not before:
                    self._rewrite(rows)
        return n

    def forget(self, person_id: str) -> int:
        """「すべて消す」: その人(quota.person。シナリオユーザーも含む)の行のユーザーIDを FOLDED に置き換える
        (ファイルとメモリの両方)。金額、トークン数、時刻、イベント、モデルは残す。置き換えた行の数を返す。"""
        def drop(rows: list[dict]) -> tuple[list[dict], int]:
            hit = [r.get("user_id") != FOLDED and person(r.get("user_id") or "") == person_id for r in rows]
            return [r | {"user_id": FOLDED} if h else r for r, h in zip(rows, hit, strict=True)], sum(hit)

        with self._lock:
            self.rows, n = drop(self.rows)
            if self.path and self.path.is_file():
                rows, n = drop(load(self.path))
                if n:
                    self._rewrite(rows)
        return n

    def rows_of(self, person_id: str) -> list[dict]:
        """その人(シナリオユーザーも含む)の行(プロフィールの「データを書き出す」)。ファイルがあればファイルから読む。"""
        with self._lock:
            rows = load(self.path) if self.path else list(self.rows)
        return [r for r in rows if r.get("user_id") != FOLDED and person(r.get("user_id") or "") == person_id]

    def _rewrite(self, rows: list[dict]) -> None:
        """ファイルを一時ファイルに書いてから置き換える。self._lock の中で呼ぶ。"""
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        tmp.replace(self.path)


def fold(rows: list[dict], cutoff: datetime) -> tuple[list[dict], int]:
    """cutoff より前の行を、日ごと(日本時間)の合計の 1 行にまとめる。前にまとめた合計の行も、同じ日なら 1 行にまとめ直す。
    「すべて消す」でユーザーIDを外した行(user_id が FOLDED の 1 回の呼び出しの行)もまとめる。
    (まとめた後の行, ユーザーIDを外した行の数)。まとめる行が無ければ rows をそのまま(同じ list を)返す。"""
    old = [r for r in rows if datetime.fromisoformat(r["at"]) < cutoff]
    n = sum(1 for r in old if r.get("user_id") != FOLDED)
    if not any(r.get("event") != DAILY_TOTAL for r in old):
        return rows, 0
    days: dict[str, list[dict]] = defaultdict(list)
    for r in old:
        days[today(datetime.fromisoformat(r["at"]))].append(r)
    totals = [{"at": max(r["at"] for r in rs), "user_id": FOLDED, "event": DAILY_TOTAL, "model": "",
               "calls": sum(r.get("calls", 1) for r in rs), **{k: sum(r.get(k) or 0 for r in rs) for k in _TOTALS},
               "cost_usd": round(sum(r.get("cost_usd") or 0.0 for r in rs), 6)} for _, rs in sorted(days.items())]
    return totals + [r for r in rows if datetime.fromisoformat(r["at"]) >= cutoff], n


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
        g["calls"] += r.get("calls", 1)  # 日ごとの合計の行(fold)は、まとめた行の数
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
