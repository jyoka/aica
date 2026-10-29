"""入り口の定義の読み込み(設計メモ docs/design/conversation-layer.md §4.1)。

入り口ごとに違うもの(欄の説明、質問バンク、移る先、決まった文など)は、データ(dip_mcp/<入り口>/entry.json)にする。
形はここで確かめる(知らない欄、文の無い問い、言い方の足りない問い、ボタンに無い候補の効き目、足りない決まった文、調子)。壊れていれば ValueError で、
読み込むとき(dip の起動)に止まる。読むのは、正社員の c2 とアルバイト・パートの v5 だけ。

entry.json の形:
- entry: 入り口の名前(career / baito)。title: 読む段に渡す入り口の名前(「正社員ヒアリング」)
- bank: 質問バンクのファイル(entry.json と同じフォルダ。問いの形は入り口のまま。層は targets と text と variants と候補だけを見る)。
  どの問いにも、text のほかの言い方を variants に 1〜2 通り置く(言い方は合わせて 2〜3 通り。§4.8。同じ問いを出し直すときに回す)
- variants: 質問バンクを書き換えられない入り口(アルバイト・パートの bank_v4.json は PRD の原文)の、問いの言い方 {問いの id: [言い方]}。
  読み込むときに、質問バンクの問いの写しに足す(ファイルは変えない)。バンクに variants がある問いには置かない
- slots: {欄: 読む段に渡す説明}。入り口の欄のすべてに置く
- switch: 移れる入り口 [{"to", "label", "track", "note"}](§4.5)。label は移るボタンの文、track は移った先の雇用区分
  (logic.TRACKS。移った先が層の版なら、層が区分を書く)、note は「考えている」(consider)ときに 1 回だけ添える注意の 1 行
- carry: ほかの入り口から引き継ぐ値の対応 {元の入り口: {元の欄: {"slot": この入り口の欄, "how": same / near}}}(§4.5。
  dip_mcp.convo.switch.carry)。同じ意味は元の確かさのまま、近い意味は推測(low)で入れる。書いていない欄は引き継がない
- infer: ボタンから推せるほかの欄 {問いの id: {ボタン: {欄: 値の文}}}(§4.7 の 1。推測(low)で入れ、まとめで確かめる)。
  質問バンクの choice_effects に書いた、問いの欄でない欄への効き目も同じく推測で入れる(入り口のハーネスが当てる)
- groups: 問いの群 [{"name", "phases": [質問バンクのフェーズ], "line": 区切りの 1 行か null}](§4.7。並びが群の順)。
  質問バンクのどのフェーズも、ちょうど 1 つの群に入る
- topics: 話題 {名前: [欄]}(§4.7 の話題のおまけ)。1 つの欄がいくつの話題に入ってもよい
- mention_questions: 本人の言葉の候補を足す問いの id(§4.7。職歴カードはひな形の id)
- texts: 決まった文 {名前: 文か、行のリスト}。入り口が求める名前は required_texts で渡す
- tone: 言い回しの調子(§4.8)。style は言う段のプロンプトに入れる調子の説明、examples は相づちの例、
  acks は決まった組み立ての相づち {種類(dip_mcp.convo.reply.ACK_KINDS): [1 文, ...]}(種類ごとに 3 つまで、順に回す)
- facts: 逆質問に答える事実のファイル(entry.json と同じフォルダ。無くてよい)。aica 全体の事実に足して読む(dip_mcp.convo.facts)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from dip_mcp.convo import facts as F
from dip_mcp.convo import reply as R
from dip_mcp.convo import switch as SW

CARD_MARK = "{n}"  # 職歴カードのひな形の欄(career{n}.role)。1 枚目に広げて確かめる
SPECIAL_TARGETS = ("segment", "__ng__")  # 欄でない問いの的(区分、避けたいこと)
VARIANTS = (1, 2)  # text のほかの言い方の数
ACKS_MAX = 3  # 決まった組み立ての相づちの、種類ごとの数
MARK_RE = re.compile(r"\{\w+\}")  # 問いの文の差し込み({which} など)


@dataclass(frozen=True)
class Entry:
    entry: str
    title: str
    bank: list
    slots: dict
    switch: list = field(default_factory=list)
    carry: dict = field(default_factory=dict)
    infer: dict = field(default_factory=dict)
    texts: dict = field(default_factory=dict)
    tone: dict = field(default_factory=dict)
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
        check_variants(name, q)


def with_variants(name: str, bank: list, variants) -> list:
    """入り口の定義の variants を、質問バンクの問いの写しに足す(variants が無ければバンクのまま)。"""
    if variants is None:
        return bank
    ids = {q.get("id") for q in bank}
    stray = sorted(set(variants) - ids) if isinstance(variants, dict) else ["(形が dict でない)"]
    both = [q["id"] for q in bank if q.get("id") in variants and q.get("variants")] if not stray else []
    if stray or both:
        raise ValueError(f"{name}: variants の問い {stray or both}(質問バンクに無いか、バンクに言い方がある)")
    return [dict(q, variants=variants[q["id"]]) if q.get("id") in variants else q for q in bank]


def check_variants(name: str, q: dict) -> None:
    """問いの言い方: text のほかに 1〜2 通り。重ならず、問いは 1 つまで、差し込みは text にあるものだけ。"""
    v = q.get("variants")
    if not isinstance(v, list) or len(v) not in VARIANTS or not all(isinstance(x, str) and x.strip() for x in v):
        raise ValueError(f"{name}: 問い {q['id']} に言い方(variants)が 1〜2 通り無い")
    if len({q["text"], *v}) != len(v) + 1:
        raise ValueError(f"{name}: 問い {q['id']} の言い方が重なる")
    marks = set(MARK_RE.findall(q["text"]))
    for x in v:
        if len(R.QMARK_RE.findall(x)) > 1 or not set(MARK_RE.findall(x)) <= marks:
            raise ValueError(f"{name}: 問い {q['id']} の言い方 {x}(問いは 1 つ、差し込みは text にあるものだけ)")


def check_tone(name: str, tone) -> None:
    """言い回しの調子: style と、相づちの種類ごとの決まった相づち(1 文、ACK_MAX 字まで、ほめない、問わない)。"""
    if not isinstance(tone, dict) or not str(tone.get("style") or "").strip():
        raise ValueError(f"{name}: 調子(tone.style)が無い")
    acks = tone.get("acks") or {}
    missing = [k for k in R.ACK_KINDS if not acks.get(k)]
    if missing or set(acks) - set(R.ACK_KINDS):
        raise ValueError(f"{name}: 決まった相づち(tone.acks)の種類 {missing or sorted(set(acks) - set(R.ACK_KINDS))}")
    for k, xs in acks.items():
        bad = [x for x in xs if not isinstance(x, str) or len(R.sentences(x)) != 1 or R.chars(x) > R.ACK_MAX
               or R.PRAISE_RE.search(x) or R.QMARK_RE.search(x)]
        if len(xs) > ACKS_MAX or bad:
            raise ValueError(f"{name}: 決まった相づち {k} {bad or xs}")


def load(path: Path, slots, entries=(), required_texts=()) -> Entry:
    """入り口の定義を読み、形を確かめる。slots は入り口の欄、entries は入り口の名前の全部(移る先の確かめ)。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    name = d.get("entry")
    if not name or not d.get("title"):
        raise ValueError(f"{path}: entry と title が要る")
    bank = json.loads((Path(path).parent / d["bank"]).read_text(encoding="utf-8"))
    bank = with_variants(name, bank, d.get("variants"))
    slots = set(slots)
    check_bank(name, bank, slots)
    described = d.get("slots") or {}
    unknown, missing = sorted(set(described) - slots), sorted(slots - set(described))
    if unknown or missing or any(not str(v).strip() for v in described.values()):
        raise ValueError(f"{name}: 欄の説明が合わない(知らない欄 {unknown}、説明の無い欄 {missing})")
    for s in d.get("switch") or []:
        if s.get("to") not in entries or s.get("to") == name or any(not str(s.get(k) or "").strip()
                                                                    for k in ("label", "track", "note")):
            raise ValueError(f"{name}: 移る先 {s}")
    SW.check_carry(name, d.get("carry") or {}, slots, entries)
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
    check_tone(name, d.get("tone"))
    facts = F.load(Path(path).parent / d["facts"] if d.get("facts") else None)
    return Entry(entry=name, title=d["title"], bank=bank, slots=described, switch=list(d.get("switch") or []),
                 carry=d.get("carry") or {}, infer=d.get("infer") or {}, texts=texts, tone=d["tone"], facts=facts, groups=groups, topics=topics,
                 mention_questions=mention_questions)
