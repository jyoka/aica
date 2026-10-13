"""LINE 側ランタイム(スタンドイン)の設定。"""

from __future__ import annotations

import os
from pathlib import Path

# .env の読み込みは aica_env の 1 か所。呼び出し元が config.load_env() で使えるよう、ここからも出す
from aica_env import load_env  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]

# LLM のプロバイダ(openai / anthropic)。LINE 側エージェント・模擬ユーザー・ジャッジ・データ生成の既定モデルもこれで決まる。
PROVIDER = os.environ.get("LINE_SIM_PROVIDER", "openai")
MODEL = os.environ.get("LINE_SIM_MODEL", {"openai": "gpt-6-sol", "anthropic": "claude-opus-5"}[PROVIDER])
EFFORT = os.environ.get("LINE_SIM_EFFORT", "low")
# dip 側 MCP サーバー。既定は子プロセス(stdio)で起動。DIP_MCP_URL を与えると HTTP で接続する。
DIP_MCP_URL = os.environ.get("DIP_MCP_URL")
DATA_DIR = Path(os.environ.get("LINE_SIM_DATA_DIR", ROOT / "data"))
