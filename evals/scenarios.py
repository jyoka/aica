"""シナリオの一覧(scenarios.json)と、シナリオを始める前の準備。画面(line_runtime.server)と評価(evals.run)で共通。

どのシナリオも AI 側の1通目から始まる: 友だち追加への挨拶・dip のプッシュは LINE 側 AI が作り、
配信への返信(crm_reply)は dip の CRM 配信(LLM なし)を1通目にして、ユーザーがそれに返信する。
"""

from __future__ import annotations

import json
from pathlib import Path

from dip_mcp import logic
from dip_mcp.data import State

SCENARIOS = Path(__file__).resolve().parent / "scenarios.json"


def load_scenarios() -> list[dict]:
    return json.loads(SCENARIOS.read_text(encoding="utf-8"))


def prepare(state: State, sc: dict, user: str | None = None) -> dict | None:
    """そのユーザーの dip の状態を初期に戻す(前の配信やプッシュが接触として残ると、3日間プッシュが止まる)。
    再訪のシナリオなら前に本人が話した希望を入れる。1通目が dip の配信なら、配信してその中身を返す。
    user を与えると、シナリオ用ユーザーの初期状態をそのIDに写して始める(ブラウザごとの匿名ユーザー用)。"""
    user = user or sc["user"]
    state.reset_user(user, template=sc["user"])
    if sc.get("seed_preferences"):
        logic.save_preferences(state, user, sc["seed_preferences"])
    if sc["event"] != "crm_reply":
        return None
    broadcast = logic.crm_broadcast(state, user)
    if not broadcast["sent"]:
        raise RuntimeError(f"dip が配信しなかった: {broadcast['reason']}")
    return broadcast
