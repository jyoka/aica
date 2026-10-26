"""CLI: 履歴書ファイルを渡すと構造化 JSON を書き出す。

使い方(リポジトリ直下から):
    ./.venv/bin/python -m resume_intake.cli resume_intake/samples/01_clean.pdf
    ./.venv/bin/python -m resume_intake.cli 履歴書.jpg -o 読み取り結果.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from line_runtime.config import load_env
from resume_intake import config
from resume_intake.extract import default_client, extract_draft, finalize


async def run(path: Path, out: Path | None, model: str | None) -> int:
    load_env()
    draft, usage = await extract_draft(path, default_client(), model=model)
    result = finalize(draft)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out is None:
        config.OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = config.OUT_DIR / f"{path.stem}.json"
    out.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(
        f"保存先: {out} / tokens: in={usage.input_tokens} out={usage.output_tokens} / 費用目安: ${usage.cost_usd:.4f}",
        file=sys.stderr,
    )
    empty = type(draft)()
    if draft.model_dump(exclude={"unconfirmed"}) == empty.model_dump(exclude={"unconfirmed"}):
        print("注意: 読み取れた項目がありません。ファイルが履歴書か、写りを確認してください。", file=sys.stderr)
        for note in draft.unconfirmed:
            print(f"  - {note}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="履歴書(PDF / JPEG / PNG)を LLM(既定 Anthropic)で読み取り、構造化 JSON にする")
    parser.add_argument("file", type=Path, help="履歴書ファイル(PDF / JPEG / PNG)")
    parser.add_argument("-o", "--out", type=Path, default=None, help="出力先 JSON(既定: data/resume_intake/<ファイル名>.json)")
    parser.add_argument("--model", default=None, help=f"モデル(既定: {config.MODEL}。プロバイダは RESUME_INTAKE_PROVIDER)")
    args = parser.parse_args(argv)
    if not args.file.is_file():
        parser.error(f"ファイルが見つかりません: {args.file}")
    return asyncio.run(run(args.file, args.out, args.model))


if __name__ == "__main__":
    raise SystemExit(main())
