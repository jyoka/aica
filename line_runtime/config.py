"""LINE 側ランタイム(スタンドイン)の設定。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ENV = ROOT / ".env"

# LLM のプロバイダ(anthropic / openai)。LINE 側エージェント・模擬ユーザー・ジャッジ・ゲームの AI・データ生成の既定モデルも
# これで決まる。既定は anthropic(task 235。料金は gpt-6-sol と同じ入力 $2 / 出力 $10 per 1M)。openai にすれば元に戻る。
# 履歴書の読み取り・音声相談・意味検索は、この値によらず OpenAI(resume_intake、voice_resume、dip_mcp/embedding.py)
PROVIDER = os.environ.get("LINE_SIM_PROVIDER", "anthropic")
MODEL = os.environ.get("LINE_SIM_MODEL", {"openai": "gpt-6-sol", "anthropic": "claude-sonnet-5-5"}[PROVIDER])
EFFORT = os.environ.get("LINE_SIM_EFFORT", "low")
# dip 側 MCP サーバー。既定は子プロセス(stdio)で起動。DIP_MCP_URL を与えると HTTP で接続する。
DIP_MCP_URL = os.environ.get("DIP_MCP_URL")
DATA_DIR = Path(os.environ.get("LINE_SIM_DATA_DIR", ROOT / "data"))


def load_env() -> None:
    """リポジトリ直下の .env を os.environ へ(既存値は上書きしない)。"""
    if not REPO_ENV.is_file():
        return
    for line in REPO_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
