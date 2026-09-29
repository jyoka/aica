"""評価の実 API の費用台帳(PRD aica-v2-fulltime の予算 $5)。1回の実行の1シナリオごとに1行の JSON(JSONL)を足す。

台帳はリポジトリに入れる(evals/budget.jsonl)。E・F・H のタスクは別の worktree で走るので、累計を引き継ぐには
コミットされた台帳が要る。評価の実行は、シナリオを始める前に毎回累計を見て、LIMIT_USD に達していたら止まる
(残りの計測を減らすか、ジャッジを安いモデルのままにするかをユーザーと決める)。
embedding の比較など評価以外の実 API も、同じ台帳に1行足す(record を呼ぶ)。

    ./.venv/bin/python -m evals.budget   # 累計と、実行ごとの内訳を出す
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

LEDGER = Path(__file__).resolve().parent / "budget.jsonl"
BUDGET_USD = 5.0  # PRD 全体の実 API の予算
LIMIT_USD = 4.0   # 累計がこれに達したら、評価を止めてユーザーに相談する


class BudgetExceeded(RuntimeError):
    pass


def rows(path: Path = LEDGER) -> list[dict]:
    if not Path(path).is_file():
        return []
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def spent(path: Path = LEDGER) -> float:
    return round(sum(r.get("cost_usd") or 0.0 for r in rows(path)), 6)


def record(path: Path, run: str, cost_usd: float, **detail) -> float:
    """1行足して、足した後の累計を返す。"""
    row = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "run": run, "cost_usd": round(cost_usd, 6), **detail}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return spent(path)


def check(path: Path = LEDGER, limit: float = LIMIT_USD) -> float:
    """累計が上限に達していれば BudgetExceeded。達していなければ累計を返す。"""
    total = spent(path)
    if total >= limit:
        raise BudgetExceeded(f"費用台帳の累計 ${total:.4f} が ${limit:.2f} に達したので止めます"
                             f"(PRD の予算 ${BUDGET_USD:.2f})。残りの計測をどうするかユーザーに相談してください。")
    return total


def line(path: Path = LEDGER) -> str:
    """実行ごとに出す累計の1行。"""
    total = spent(path)
    return f"費用台帳の累計 ${total:.4f} / 止める額 ${LIMIT_USD:.2f}(予算 ${BUDGET_USD:.2f}、残り ${LIMIT_USD - total:.4f})"


def main() -> None:
    by_run: dict[str, float] = defaultdict(float)
    for r in rows():
        by_run[r["run"]] += r.get("cost_usd") or 0.0
    for run, cost in by_run.items():
        print(f"{run:<32} ${cost:.4f}")
    print(line())


if __name__ == "__main__":
    main()
