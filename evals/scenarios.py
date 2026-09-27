"""シナリオの一覧(scenarios.json)と、シナリオを始める前の準備。画面(line_runtime.server)と評価(evals.run)で共通。

どのシナリオも AI 側の1通目から始まる: 初回訪問・再訪の挨拶と、dip が許可したプッシュのどちらか。
"""

from __future__ import annotations

import json
from pathlib import Path

from dip_mcp import logic
from dip_mcp.data import State

SCENARIOS = Path(__file__).resolve().parent / "scenarios.json"


def load_scenarios() -> list[dict]:
    return json.loads(SCENARIOS.read_text(encoding="utf-8"))


def prepare(state: State, sc: dict, user: str | None = None) -> None:
    """そのユーザーの dip の状態を初期に戻す(前のプッシュが接触として残ると、3日間プッシュが止まる)。
    再訪のシナリオなら前に本人が話した希望を入れる。
    user を与えると、シナリオ用ユーザーの初期状態をそのIDに写して始める(ブラウザごとの匿名ユーザー用)。"""
    user = user or sc["user"]
    state.reset_user(user, template=sc["user"])
    if sc.get("seed_preferences"):
        logic.save_preferences(state, user, sc["seed_preferences"])
