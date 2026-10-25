"""ゲームの段を 1 回、aica の LLM のプロバイダで進める(指示を組み立てる → 構造化出力で呼ぶ → 事実を確かめる)。

段の種類(kind)は費用の上限の数え方: reaction はゲームの中の反応(安いモデル、1 回の遊びで REACTIONS_MAX 回まで)、
summary は証拠のまとめと最後のまとめ(LINE_SIM_MODEL、1 回の遊びで SUMMARIES_MAX 回)。回数を数えるのは dip の関数
(dip_mcp.arcade)で、ここは 1 回呼ぶだけ。

事実の確かめ(resume_lines.check_numbers / quote_in): AI が書いた文の数字が本人の言葉(sources)に無ければ数字の空欄に
置き換え、引用が無ければ捨てる。置き換えた数と捨てた数を replaced で返す(成功の指標の「事実でない数字」)。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from line_runtime import config, llm
from resume_arcade import prompts
from resume_arcade import schemas as s
from resume_draft.pii import redact_pii
from resume_lines import check_numbers, quote_in

QUICK_MODEL = {"openai": "gpt-6-luna", "anthropic": "claude-haiku-5-5"}  # 安いモデル(Claude Haiku 5.5 は $0.10 / $0.50)
QUICK_MODEL_ENV = "AICA_ARCADE_QUICK_MODEL"  # ゲームの中の反応のモデルだけを替える(決まっていないことの 5)


def model_for(tier: str) -> str:
    if tier == "quick":
        return os.environ.get(QUICK_MODEL_ENV, "").strip() or QUICK_MODEL.get(config.PROVIDER, QUICK_MODEL["openai"])
    return config.MODEL


@dataclass(frozen=True)
class Stage:
    game: str
    name: str
    kind: str  # reaction / summary
    tier: str  # quick / default
    inp: type[BaseModel]
    out: type[BaseModel]
    prompt: Callable[[dict], tuple[str, str]]
    check: Callable[[dict, dict], int]  # (結果, 入力) → 置き換えた数。結果はその場で直す
    max_tokens: int
    keep: int = 0  # この段の後に残しておくまとめの回数(証拠のまとめが、最後のまとめの分を使い切らないように)


@dataclass
class Outcome:
    result: dict
    usage: dict
    model: str
    replaced: int


# ----------------------------- 事実の確かめ -----------------------------


def _fix(obj: dict, key: str, sources: list[str]) -> int:
    obj[key], n = check_numbers(obj.get(key, ""), sources)
    return n


def _fix_list(items: list, sources: list[str]) -> tuple[list, int]:
    out, n = [], 0
    for x in items:
        fixed, k = check_numbers(x, sources)
        out.append(fixed)
        n += k
    return out, n


def _quotes(items: list[str], sources: list[str], allowed: tuple[str, ...] = ()) -> tuple[list[str], int]:
    kept = [q for q in items if q in allowed or quote_in(q, sources)]
    return kept, len(items) - len(kept)


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [x for v in value.values() for x in _strings(v)]
    if isinstance(value, list):
        return [x for v in value for x in _strings(v)]
    return []


# 事実の確かめの元は、本人が書いた欄だけ。行の番号(e1、j1)や強みのキーを入れると、「5人」の 5 が「e5」に見つかってしまう
INTAKE_KEYS = ("title", "years", "target", "tools", "edu")
CASE_JOB_KEYS = ("company", "title", "dates")
HARVEST_JOB_KEYS = ("company", "title", "years")
LINE_KEYS = ("text", "quote", "metric")


def _fields(obj: dict | None, keys: tuple[str, ...]) -> list[str]:
    return [str(v) for k in keys if (v := (obj or {}).get(k))]


def _lines(lines: list[dict] | None) -> list[str]:
    return [x for line in lines or [] for x in _fields(line, LINE_KEYS)]


def _case_sources(d: dict) -> list[str]:
    return _fields(d.get("intake"), INTAKE_KEYS) + _fields(d.get("job"), CASE_JOB_KEYS) + list(d.get("known") or [])


def _lab_sources(d: dict) -> list[str]:
    return _lines(d["cards"])


def _verdict_sources(d: dict) -> list[str]:
    out = _fields(d.get("intake"), INTAKE_KEYS)
    for j in d.get("jobs") or []:
        out += _fields(j, CASE_JOB_KEYS) + _lines(j.get("lines"))
    return out + _lines(d.get("weaknesses")) + list(d.get("clusters") or [])


def _final_sources(d: dict) -> list[str]:
    out = [str(d.get(k) or "") for k in ("target_role", "family", "education")]
    for j in d.get("jobs") or []:
        out += _fields(j, HARVEST_JOB_KEYS) + _lines(j.get("lines"))
    return out


def _check_opening(r: dict, d: dict) -> int:
    return _fix(r, "line", _case_sources(d))


def _check_reaction(r: dict, d: dict) -> int:
    said = [d.get("answer", ""), d.get("first_answer", "")]
    n = _fix(r, "line", said + _case_sources(d))
    for e in r.get("evidence") or []:
        n += _fix(e, "fact", said) + _fix(e, "title", said)
        if e.get("metric") and not quote_in(e["metric"], said):
            e["metric"], n = "", n + 1
        if e.get("quote") and not quote_in(e["quote"], said):
            e["quote"], n = "", n + 1
    return n


def _check_lab(r: dict, d: dict) -> int:
    ids = {c["id"] for c in d["cards"]}
    sources = _lab_sources(d)
    n = 0
    for c in r.get("clusters") or []:
        c["ids"] = [i for i in c.get("ids") or [] if i in ids]
        n += _fix(c, "why", sources) + _fix(c, "name", sources)
    r["clusters"] = [c for c in r.get("clusters") or [] if c["ids"]]
    return n


def _check_judgements(items: list[dict], sources: list[str]) -> int:
    n = 0
    for j in items:
        n += _fix(j, "verdict", sources) + _fix(j, "name", sources)
        j["exhibits"], k = _quotes(j.get("exhibits") or [], sources)
        n += k
    return n


def _check_verdict(r: dict, d: dict) -> int:
    sources = _verdict_sources(d)
    n = _fix(r, "monologue", sources)
    n += _check_judgements(r.get("strengths") or [], sources) + _check_judgements(r.get("weaknesses") or [], sources)
    res = r.get("resume") or {}
    for k in ("headline", "summary", "self_pr", "education"):
        n += _fix(res, k, sources)
    for x in res.get("experience") or []:
        x["bullets"], k = _fix_list(x.get("bullets") or [], sources)
        n += k
    res["skills"], k = _fix_list(res.get("skills") or [], sources)
    return n + k


def _harvest_turn_sources(d: dict) -> list[str]:
    return _strings({k: d.get(k) for k in ("job", "title", "story", "earlier", "answer", "target_role")})


def _check_turn(r: dict, d: dict) -> int:
    sources = _harvest_turn_sources(d)
    return _fix(r, "say", sources) + _fix(r, "ask", sources)


def _check_final(r: dict, d: dict) -> int:
    sources = _final_sources(d)
    n = sum(_fix(r, k, sources) for k in ("headline", "summary", "self_pr"))
    for x in r.get("experience") or []:
        x["bullets"], k = _fix_list(x.get("bullets") or [], sources)
        n += k
    r["skills"], k = _fix_list(r.get("skills") or [], sources)
    n += k
    for item in (r.get("strengths") or []) + (r.get("weaknesses") or []):
        n += _fix(item, "title", sources) + (_fix(item, "why", sources) if "why" in item else 0)
        if item.get("evidence") and item["evidence"] != prompts.NO_MEM and not quote_in(item["evidence"], sources):
            item["evidence"], n = "", n + 1
    return n


STAGES = {(st.game, st.name): st for st in (
    Stage("the-case-of-you", "opening", "reaction", "quick", s.CaseOpeningIn, s.Opening, prompts.case_opening,
          _check_opening, 600),
    Stage("the-case-of-you", "reaction", "reaction", "quick", s.CaseReactionIn, s.Reaction, prompts.case_reaction,
          _check_reaction, 1200),
    Stage("the-case-of-you", "lab", "summary", "quick", s.CaseLabIn, s.Lab, prompts.case_lab, _check_lab, 1500, keep=1),
    Stage("the-case-of-you", "verdict", "summary", "default", s.CaseVerdictIn, s.Verdict, prompts.case_verdict,
          _check_verdict, 8000),
    Stage("harvest-of-work", "turn", "reaction", "quick", s.HarvestTurnIn, s.Turn, prompts.harvest_turn, _check_turn,
          600),
    Stage("harvest-of-work", "final", "summary", "default", s.HarvestFinalIn, s.HarvestFinal, prompts.harvest_final,
          _check_final, 8000),
)}
GAMES = tuple(dict.fromkeys(g for g, _ in STAGES))


def stage(game: str, name: str) -> Stage | None:
    return STAGES.get((game, name))


def _redact(value):
    """AI に送る前に、答えの中の電話番号とメールを伏せる(resume_draft.pii の決まり)。"""
    if isinstance(value, str):
        return redact_pii(value, "[連絡先]")
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def prepare(st: Stage, raw: dict) -> dict:
    """画面が送ったものを、段の入力の形にする(長すぎる文字列は切る。形が違えば pydantic.ValidationError)。
    電話番号とメールはここで伏せる(LLM にも、事実の確かめの元にも残さない)。"""
    return _redact(st.inp.model_validate(raw).model_dump())


def summary_chars(d: dict) -> int:
    """まとめの段で送られた文字数の合計(上限 schemas.SUMMARY_MAX を超えたら LLM を呼ばない)。"""
    return sum(len(x) for x in _strings(d))


async def run(client, st: Stage, d: dict) -> Outcome:
    """1 回呼ぶ。d は prepare の結果。結果は事実の確かめの後のもの。"""
    system, user = st.prompt(d)
    model = model_for(st.tier)
    effort = "none" if st.tier == "quick" else config.EFFORT
    parsed, usage = await llm.parse(client, model=model, system=system, messages=[{"role": "user", "content": user}],
                                    schema=st.out, max_tokens=st.max_tokens, effort=effort, with_usage=True)
    if parsed is None:
        raise ValueError("構造化出力が空でした")
    result = parsed.model_dump()
    replaced = st.check(result, d)
    return Outcome(result, usage, model, replaced)
