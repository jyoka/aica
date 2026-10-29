"""入り口の定義の読み込み(設計メモ docs/design/conversation-layer.md §4.1)。

入り口ごとに違うもの(欄の説明、質問バンク、移る先、決まった文など)は、データ(dip_mcp/<入り口>/entry.json)にする。
形はここで確かめる(知らない欄、文の無い問い、ボタンに無い候補の効き目、足りない決まった文)。壊れていれば ValueError で、
読み込むとき(dip の起動)に止まる。今の版で読むのは、正社員の c2 だけ。

entry.json の形:
- entry: 入り口の名前(career / baito)。title: 読む段に渡す入り口の名前(「正社員ヒアリング」)
- bank: 質問バンクのファイル(entry.json と同じフォルダ。問いの形は入り口のまま。層は targets と text と候補だけを見る)
- slots: {欄: 読む段に渡す説明}。入り口の欄のすべてに置く
- switch: 移れる入り口 [{"to", "label"}]。label は移るボタンの文(§4.5。使うのは 6 枚目のカード)
- infer: ボタンから推せるほかの欄 {問いの id: {ボタン: {欄: 値の文}}}(§4.7 の 1。推測(low)で入れ、まとめで確かめる)。
  質問バンクの choice_effects に書いた、問いの欄でない欄への効き目も同じく推測で入れる(入り口のハーネスが当てる)
- groups: 問いの群 [{"name", "phases": [質問バンクのフェーズ], "line": 区切りの 1 行か null}](§4.7。並びが群の順)。
  質問バンクのどのフェーズも、ちょうど 1 つの群に入る
- topics: 話題 {名前: [欄]}(§4.7 の話題のおまけ)。1 つの欄がいくつの話題に入ってもよい
- mention_questions: 本人の言葉の候補を足す問いの id(§4.7。職歴カードはひな形の id)
- texts: 決まった文 {名前: 文か、行のリスト}。入り口が求める名前は required_texts で渡す
- facts: 逆質問に答える事実のファイル(entry.json と同じフォルダ。無くてよい)。aica 全体の事実に足して読む(dip_mcp.convo.facts)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from dip_mcp.convo import facts as F

CARD_MARK = "{n}"  # 職歴カードのひな形の欄(career{n}.role)。1 枚目に広げて確かめる
SPECIAL_TARGETS = ("segment", "__ng__")  # 欄でない問いの的(区分、避けたいこと)


@dataclass(frozen=True)
class Entry:
    entry: str
    title: str
    bank: list
    slots: dict
    switch: list = field(default_factory=list)
    infer: dict = field(default_factory=dict)
    texts: dict = field(default_factory=dict)
    facts: dict = field(default_factory=lambda: F.load(None))
    groups: list = field(default_factory=list)
    topics: dict = field(default_factory=dict)
    mention_questions: tuple = ()


def _choices(q: dict) -> list[str]:
    return [c for page in q.get("pages") or [q.get("choices") or []] for c in page]


def check_bank(name: str, bank, slots) -> None:
    """質問バンクの形: どの問いにも id と文と候補があり、的が入り口の欄で、効き目がボタンにある。"""
    if not isinstance(bank, list) or not bank:
        raise ValueError(f"{name}: 質問バンクが空")
    ids = set()
    for q in bank:
        qid = q.get("id")
        if not qid or qid in ids:
            raise ValueError(f"{name}: 問いの id が無いか重なる({qid})")
        ids.add(qid)
        if not str(q.get("text") or "").strip():
            raise ValueError(f"{name}: 問い {qid} に文が無い")
        targets = q.get("targets") or []
        bad = [t for t in targets if t not in SPECIAL_TARGETS and t.replace(CARD_MARK, "1") not in slots]
        if not targets or bad:
            raise ValueError(f"{name}: 問い {qid} の知らない的 {bad or targets}")
        choices = _choices(q)
        if not choices:
            raise ValueError(f"{name}: 問い {qid} に候補が無い")
        stray = [c for c in (q.get("choice_effects") or {}) if c not in choices]
        if stray:
            raise ValueError(f"{name}: 問い {qid} の効き目がボタンに無い {stray}")


def load(path: Path, slots, entries=(), required_texts=()) -> Entry:
    """入り口の定義を読み、形を確かめる。slots は入り口の欄、entries は入り口の名前の全部(移る先の確かめ)。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    name = d.get("entry")
    if not name or not d.get("title"):
        raise ValueError(f"{path}: entry と title が要る")
    bank = json.loads((Path(path).parent / d["bank"]).read_text(encoding="utf-8"))
    slots = set(slots)
    check_bank(name, bank, slots)
    described = d.get("slots") or {}
    unknown, missing = sorted(set(described) - slots), sorted(slots - set(described))
    if unknown or missing or any(not str(v).strip() for v in described.values()):
        raise ValueError(f"{name}: 欄の説明が合わない(知らない欄 {unknown}、説明の無い欄 {missing})")
    for s in d.get("switch") or []:
        if s.get("to") not in entries or s.get("to") == name or not str(s.get("label") or "").strip():
            raise ValueError(f"{name}: 移る先 {s}")
    qs = {q["id"]: q for q in bank}
    for qid, by_label in (d.get("infer") or {}).items():
        if qid not in qs:
            raise ValueError(f"{name}: infer の知らない問い {qid}")
        for label, values in by_label.items():
            if label not in _choices(qs[qid]):
                raise ValueError(f"{name}: infer の {qid} にボタン {label} が無い")
            bad = [s for s, v in values.items() if s not in slots or not str(v).strip()]
            if bad:
                raise ValueError(f"{name}: infer の {qid} {label} の欄 {bad}")
    groups = d.get("groups") or []
    phases = [p for g in groups for p in g.get("phases") or []]
    if groups and (len(phases) != len(set(phases)) or set(phases) != {q.get("phase") for q in bank}
                   or any(not str(g.get("name") or "").strip() or not g.get("phases") for g in groups)):
        raise ValueError(f"{name}: 群が質問バンクのフェーズと合わない({phases})")
    topics = d.get("topics") or {}
    for t, members in topics.items():
        bad = [s for s in members or [] if s not in slots]
        if not members or bad:
            raise ValueError(f"{name}: 話題 {t} の欄 {bad or members}")
    mention_questions = tuple(d.get("mention_questions") or ())
    stray = [m for m in mention_questions if m not in qs]
    if stray:
        raise ValueError(f"{name}: 本人の言葉の候補を足す問いが質問バンクに無い {stray}")
    texts = d.get("texts") or {}
    lacking = [t for t in required_texts if not texts.get(t)]
    if lacking:
        raise ValueError(f"{name}: 決まった文が無い {lacking}")
    facts = F.load(Path(path).parent / d["facts"] if d.get("facts") else None)
    return Entry(entry=name, title=d["title"], bank=bank, slots=described, switch=list(d.get("switch") or []),
                 infer=d.get("infer") or {}, texts=texts, facts=facts, groups=groups, topics=topics,
                 mention_questions=mention_questions)
