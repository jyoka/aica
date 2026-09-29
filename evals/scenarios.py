"""シナリオの一覧(scenarios.json)と、シナリオを始める前の準備。画面(line_runtime.server)と評価(evals.run)で共通。

どのシナリオも AI 側の1通目から始まる: 初回訪問・再訪の挨拶と、dip が許可したプッシュのどちらか。
ペルソナの履歴書(evals/personas/*.json)はすべて架空。
"""

from __future__ import annotations

import json
from pathlib import Path

from dip_mcp import logic
from dip_mcp.data import State

HERE = Path(__file__).resolve().parent
SCENARIOS = HERE / "scenarios.json"
PERSONAS = HERE / "personas"
RESUME_FILE = "職務経歴書(PDF)"  # トークに出す表示名(種類だけ。ファイル名は受け取らない)
# 再訪の種として記録する区分の根拠(前の会話で本人が話したという扱い)
SEED_TRACK_EVIDENCE = "(評価の種: 前の会話で本人が話した区分)"


def load_scenarios() -> list[dict]:
    return json.loads(SCENARIOS.read_text(encoding="utf-8"))


def load_resume(name: str) -> dict:
    """ペルソナの架空の履歴書(読み取り結果の形。resume_intake.schema.envelope と同じ)。"""
    return json.loads((PERSONAS / f"{name}.json").read_text(encoding="utf-8"))


def upload_resume(state: State, sc: dict, user: str | None = None) -> dict:
    """会話の途中で本人が履歴書をアップロードしたことにする(sc["upload_resume"])。
    本番の /api/resume/.../upload と同じく dip に保存し、resume_upload のイベントに渡す事実を返す。"""
    from line_runtime.resume_confirm import resume_bubbles

    user = user or sc["user"]
    rec = logic.save_resume_upload(state, user, load_resume(sc["upload_resume"]), file=RESUME_FILE)
    return {"file": rec["file"], "shown": resume_bubbles(rec["resume"]), "view_url": f"/liff/resume?user={user}"}


def prepare(state: State, sc: dict, user: str | None = None) -> None:
    """そのユーザーの dip の状態を初期に戻す(前のプッシュが接触として残ると、3日間プッシュが止まる)。
    再訪のシナリオなら前に本人が話した希望(seed_preferences)と雇用区分(seed_track)を入れる。
    seed_resume は、前にアップロード済みの履歴書(会話の前から要約の uploaded_resume にある)。
    user を与えると、シナリオ用ユーザーの初期状態をそのIDに写して始める(ブラウザごとの匿名ユーザー用)。"""
    user = user or sc["user"]
    state.reset_user(user, template=sc["user"])
    if sc.get("seed_preferences"):
        logic.save_preferences(state, user, sc["seed_preferences"])
    if sc.get("seed_track"):
        logic.set_search_track(state, user, sc["seed_track"], SEED_TRACK_EVIDENCE)
    if sc.get("seed_resume"):
        logic.save_resume_upload(state, user, load_resume(sc["seed_resume"]), file=RESUME_FILE)
