"""質問バンク(48問)をデータ(bank.json)から読む。プロンプトには質問を書かない(PRD §13 の P0-4)。

正は docs/prd/baito_harness/bank.py。ユーザーに見せる質問文と選択肢は原文のまま(tests/test_baito_harness.py が
1字ずつ照らし合わせる)。形は PRD §7.1 のとおり:
{"id", "phase", "segments", "targets", "text", "choices", "choice_effects"}。choices は choice_effects のキーの順。
choice_effects の値は {slot: value}、選ばれたら NG に足す "__ng__": [..]、segment を決める "__segment__": seg。
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

BANK_PATH = Path(__file__).resolve().parent / "bank.json"
PHASES = ("icebreak", "hard_filter", "scenario_game", "reverse_pick", "fill_gaps")
SEGMENTS = ("all", "student", "homemaker", "side_job")


def validate(bank: list[dict]) -> list[str]:
    """PRD §7.1 の質問の要件に合わないところ(合っていれば空)。1問1テーマ(targets がある)、選択肢は2〜4個、
    choice_effects の各選択肢が target のスロット(か __ng__ / __segment__)を決めること。"""
    errors, seen = [], set()
    for q in bank:
        qid = q.get("id")
        if qid in seen:
            errors.append(f"{qid}: id が重複")
        seen.add(qid)
        if q.get("phase") not in PHASES:
            errors.append(f"{qid}: phase が {q.get('phase')}")
        if not q.get("segments") or any(s not in SEGMENTS for s in q["segments"]):
            errors.append(f"{qid}: segments が {q.get('segments')}")
        if not q.get("targets") or not q.get("text"):
            errors.append(f"{qid}: targets か text が空")
        effects = q.get("choice_effects") or {}
        if not 2 <= len(effects) <= 4:
            errors.append(f"{qid}: 選択肢が {len(effects)} 個")
        for choice, eff in effects.items():
            slots = {k for k in eff if not k.startswith("__")}
            if not eff or not slots <= set(q.get("targets", [])):
                errors.append(f"{qid}: 「{choice}」の choice_effects が targets {q.get('targets')} に無い {sorted(slots)}")
    return errors


@cache
def _load() -> tuple[str, ...]:
    data = json.loads(BANK_PATH.read_text(encoding="utf-8"))
    bank = [q | {"choices": list(q["choice_effects"])} for q in data]
    if errors := validate(bank):
        raise ValueError("質問バンクが PRD §7.1 の要件に合いません: " + "; ".join(errors))
    return tuple(json.dumps(q, ensure_ascii=False) for q in bank)


def load_bank() -> list[dict]:
    """質問バンク(呼ぶたびに新しい list。呼び出し側が変えても次の呼び出しに残らない)。"""
    return [json.loads(q) for q in _load()]
