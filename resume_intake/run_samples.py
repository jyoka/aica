"""サンプル履歴書3種を実 API で読み取り、結果と正解との比較を results/ に残す。

実 API を呼ぶので手動で実行する(テストからは呼ばない)。結果は data/resume_intake/ に書く:
    ./.venv/bin/python -m resume_intake.run_samples
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from openai import AsyncOpenAI

from line_runtime.config import load_env
from resume_intake import config
from resume_intake.extract import extract_draft, finalize

EXPECTED = config.SAMPLES / "expected"
# 正解ファイルがあるサンプルだけ回す(04_unreadable.jpg は CLI で個別に確認する)
SAMPLES = [p for p in sorted(config.SAMPLES.glob("0*.*")) if (EXPECTED / f"{p.stem}.json").is_file()]
RESULTS = config.OUT_DIR


def compare(expected: dict, actual: dict) -> list[tuple[str, bool, str]]:
    """resume の項目ごとに正解と比べる。(項目名, 一致したか, メモ)を返す。"""
    rows = []
    for key, exp in expected.items():
        act = actual.get(key)
        if exp == act:
            rows.append((key, True, ""))
        else:
            note = f"期待: {json.dumps(exp, ensure_ascii=False)} / 実際: {json.dumps(act, ensure_ascii=False)}"
            rows.append((key, False, note))
    return rows


async def main() -> None:
    load_env()
    client = AsyncOpenAI()
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    lines = [f"# サンプル読み取り結果({stamp} UTC, model={config.MODEL})", ""]
    total_cost = 0.0
    for path in SAMPLES:
        draft, usage = await extract_draft(path, client)
        result = finalize(draft)
        out = RESULTS / f"{path.stem}.result.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        total_cost += usage.cost_usd
        lines += [
            f"## {path.name}",
            "",
            f"- tokens: in={usage.input_tokens} out={usage.output_tokens}, 費用目安: ${usage.cost_usd:.4f}",
            "- 項目ごとの比較(完全一致か):",
            "",
        ]
        expected = json.loads((EXPECTED / f"{path.stem}.json").read_text(encoding="utf-8"))
        for key, ok, note in compare(expected, result["resume"]):
            mark = "一致" if ok else "不一致"
            lines.append(f"  - {key}: {mark}" + (f" ({note})" if note else ""))
        lines.append("")
    lines += [f"## 合計費用目安: ${total_cost:.4f}", ""]
    report = RESULTS / f"accuracy-{stamp}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {report}")
    print(f"total cost: ${total_cost:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
