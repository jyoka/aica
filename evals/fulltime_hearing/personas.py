"""正社員のヒアリングの評価のペルソナ 6 人と事実表(設計メモ docs/design/fulltime-hearing.md §6.3)。

1 人 1 ファイル(evals/personas/fulltime_hearing/*.json)。どれも架空。
- facts: 数える事実表(§6.4)。why_now、HARD の条件、職歴カードの欄。key は次の名前にそろえる(2/5 のモックの本人役と、
  形 B〜D の終わりの状態も同じ key で返す)。kind は text(文。ジャッジが同じ意味かを見る)/ number(value と unit)/
  dont_know(本当に覚えていない)。
- other: 数えないが、聞かれたら答えること(MID と LOW の好み、NG、古い職歴など)。本人役は facts と other からだけ答える。
- resume: 履歴書を先に送っている人だけ。file は evals/personas からの読み取り結果の名前(evals.scenarios.load_resume)、
  filled は読み取りで埋まる key(形 B〜D は high として入れ、聞かない)。
"""

from __future__ import annotations

import json
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "personas" / "fulltime_hearing"

# 条件表の HARD(§3.2)と、segment だけの HARD(§3.1)。unknown は segment だけの HARD なし
COMMON_HARD = ("why_now", "area", "min_annual_salary", "start_timing")
SEGMENT_HARD = {
    "same_path": ("raise_target",),
    "change": ("change_reason", "use_experience"),
    "return": ("work_hours", "blank_period"),
    "early": ("keep_doing", "stop_doing"),
    "unknown": (),
}
CARD_FIELDS = ("role", "scale", "result")
KINDS = ("text", "number", "dont_know")
# 報告と本人役に見せる、key の名前
LABELS = {
    "why_now": "なぜ今", "area": "勤務地", "min_annual_salary": "年収の下限", "start_timing": "働き始める時期",
    "raise_target": "上げたいもの", "change_reason": "変えたい理由", "use_experience": "活かしたい経験",
    "work_hours": "働ける時間", "blank_period": "ブランクの期間", "keep_doing": "続けたいこと", "stop_doing": "やめたいこと",
    "role": "何をしていたか", "scale": "どのくらい", "result": "どうなったか",
}


def label(key: str) -> str:
    """key の表示名。職歴カードは「職歴1 どのくらい」。"""
    if key.startswith("career"):
        n, field = key.removeprefix("career").split(".")
        return f"職歴{n} {LABELS[field]}"
    return LABELS[key]


def hard_keys(persona: dict) -> tuple[str, ...]:
    """本当の segment で数える HARD の key(§6.4 の関門「HARD が空のまま result に着いた」)。"""
    return COMMON_HARD + SEGMENT_HARD[persona["segment"]]


def validate(p: dict) -> None:
    keys = [f["key"] for f in p["facts"]]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{p['id']}: 事実表の key が重なっている")
    for k in hard_keys(p):
        if k not in keys:
            raise ValueError(f"{p['id']}: HARD の {k} が事実表に無い")
    for f in p["facts"]:
        if f["kind"] not in KINDS:
            raise ValueError(f"{p['id']}: {f['key']} の kind {f['kind']}")
        if f["key"].startswith("career"):
            n, field = f["key"].removeprefix("career").split(".")
            if not n.isdigit() or field not in CARD_FIELDS:
                raise ValueError(f"{p['id']}: 職歴カードの key {f['key']}")
        elif f["key"] not in LABELS:
            raise ValueError(f"{p['id']}: 知らない key {f['key']}")
        if f["kind"] == "number" and not isinstance(f.get("value"), int | float):
            raise ValueError(f"{p['id']}: {f['key']} の value が数でない")
    for k in (p.get("resume") or {}).get("filled", []):
        if k not in keys:
            raise ValueError(f"{p['id']}: 履歴書で埋まる {k} が事実表に無い")


def load_personas() -> list[dict]:
    """6 人を id の順(p1〜p6)に。"""
    out = []
    for f in sorted(DIR.glob("p*.json")):
        p = json.loads(f.read_text(encoding="utf-8"))
        validate(p)
        out.append(p)
    return out


def select(personas: list[dict], only: str | None) -> list[dict]:
    """--persona(カンマ区切りの id か、all)で絞る。知らない id は費用を使う前にエラー。"""
    if not only or only == "all":
        return personas
    ids = [x.strip() for x in only.split(",") if x.strip()]
    unknown = [x for x in ids if x not in {p["id"] for p in personas}]
    if unknown:
        raise SystemExit(f"--persona に知らない id: {unknown}(ある id: {[p['id'] for p in personas]})")
    return [p for p in personas if p["id"] in ids]
