"""リポジトリ直下の .env を os.environ へ読み込む。

どのパッケージにも依存しない。line_runtime、voice_resume、resume_intake(と、line_runtime.config 経由の evals、scripts)が
同じものを使う。前は line_runtime と voice_resume に同じ関数があり、resume_intake は line_runtime から借りていた
(review-2026-10.md の R13。借りていたことで line_runtime と resume_intake が互いに import していた)。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    """.env を os.environ へ(既存値は上書きしない)。ファイルが無ければ何もしない。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
