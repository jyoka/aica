"""LINE 側ランタイム(スタンドイン)の設定。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ENV = ROOT / ".env"

# LLM のプロバイダ(openai / anthropic)。LINE 側エージェント・模擬ユーザー・ジャッジ・データ生成の既定モデルもこれで決まる。
PROVIDER = os.environ.get("LINE_SIM_PROVIDER", "openai")
MODEL = os.environ.get("LINE_SIM_MODEL", {"openai": "gpt-6-sol", "anthropic": "claude-opus-5"}[PROVIDER])
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
