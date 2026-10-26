"""resume-intake の設定。プロバイダ・モデル名・出力先。

読み取りのモデル(task 236 から既定は Anthropic):
- anthropic: claude-sonnet-5-5(PDF は document、画像は image のブロックで受ける。入力 $2 / 出力 $10 per 1M tokens。
  2026-10-26 の Anthropic の資料)
- openai: gpt-6-sol(画像入力と PDF 入力(input_file)に対応。料金は同じ。2026-09 の OpenAI の資料)
プロバイダは RESUME_INTAKE_PROVIDER、無ければトークと同じ LINE_SIM_PROVIDER、それも無ければ anthropic。
.env の読み込みは line_runtime.config.load_env(リポジトリ直下の .env)を使う。
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
# line_runtime.config.DATA_DIR と同じ場所(dip_mcp からも import されるので line_runtime には依存しない)
DATA_DIR = Path(os.environ.get("LINE_SIM_DATA_DIR", HERE.parent / "data"))
SAMPLES = HERE / "samples"

PROVIDER = os.environ.get("RESUME_INTAKE_PROVIDER") or os.environ.get("LINE_SIM_PROVIDER") or "anthropic"
MODEL = os.environ.get("RESUME_INTAKE_MODEL", {"openai": "gpt-6-sol"}.get(PROVIDER, "claude-sonnet-5-5"))
# 読み取りの思考の深さ(Anthropic だけ)。書いてあることを写すだけなので思考を切る(none。Claude Sonnet 5.5 では
# thinking between_tools + effort low。line_runtime.llm.thinking_kwargs)。節約のため
EFFORT = os.environ.get("RESUME_INTAKE_EFFORT", "none")
# CLI・サンプル検証の出力先(data/ 配下なので git には入らない)
OUT_DIR = Path(os.environ.get("RESUME_INTAKE_OUT_DIR", DATA_DIR / "resume_intake"))

# 費用目安の計算用(claude-sonnet-5-5 と gpt-6-sol の公表価格。どちらも同じ。USD / 1M tokens。
# RESUME_INTAKE_MODEL で別の料金のモデルにしたときは目安がずれる。費用台帳は line_runtime/ledger.py の料金表で数える)
PRICE_INPUT_PER_MTOK = 2.0
PRICE_OUTPUT_PER_MTOK = 10.0
# トークからアップロードできる大きさの上限(写真の履歴書でも十分な大きさ)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
