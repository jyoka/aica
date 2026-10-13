"""resume-intake の設定。モデル名・出力先。

モデルは 2026-09 時点の公式ドキュメント(developers.openai.com/api/docs/models,
同 guides/pdf-files)で確認したもの:
- 読み取り: gpt-6-sol(画像入力と PDF 入力(input_file)に対応。line_runtime の
  既定 OpenAI モデルと同じ。入力 $2 / 出力 $10 per 1M tokens)
.env の読み込みは aica_env.load_env(リポジトリ直下の .env)を使う。
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
# line_runtime.config.DATA_DIR と同じ場所(dip_mcp からも import されるので line_runtime には依存しない)
DATA_DIR = Path(os.environ.get("LINE_SIM_DATA_DIR", HERE.parent / "data"))
SAMPLES = HERE / "samples"

MODEL = os.environ.get("RESUME_INTAKE_MODEL", "gpt-6-sol")
# CLI・サンプル検証の出力先(data/ 配下なので git には入らない)
OUT_DIR = Path(os.environ.get("RESUME_INTAKE_OUT_DIR", DATA_DIR / "resume_intake"))

# 費用目安の計算用(2026-09 時点の gpt-6-sol の公表価格。USD / 1M tokens)
PRICE_INPUT_PER_MTOK = 2.0
PRICE_OUTPUT_PER_MTOK = 10.0
# トークからアップロードできる大きさの上限(写真の履歴書でも十分な大きさ)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
