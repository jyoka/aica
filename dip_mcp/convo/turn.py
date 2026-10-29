"""1 ターンの呼び直しの部品(設計メモ docs/design/conversation-layer.md §4.1 の「状態の器と呼び直し」)。

入り口のハーネスは、モデルを直に呼ばない(dip は LLM を呼ばない。HLD 1.1)。呼ぶ側がモデルの出力を集めて呼び直す形
(dip_mcp.baito.session.ReplayLLM)で、ここはその中の 1 回の受け取り方を持つ。
- call_llm: 出力を確かめ、だめなら 1 回だけやり直し、それでもだめなら None(呼ぶ側が決まった文に倒す)
- match_button: 打った文が、出しているボタン(と、この問いで前に見せた候補)と同じか。ボタンの答えは読む段を呼ばない
- render_prompt: プロンプトのファイルをつなぎ、{{変数}} を埋める
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from dip_mcp.convo.text import nfkc

LLM_RETRIES = 1


def parse_json(raw):
    """モデルの出力(dict か JSON の文。前後の ``` や文が付いていてもよい)を dict にする。読めなければ None。"""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    s = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    for cand in (s, (re.search(r"\{.*\}", s, flags=re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            d = json.loads(cand)
            return d if isinstance(d, dict) else None
        except ValueError:
            continue
    return None


def call_llm(fn, payload, validator, log, stage, retries=LLM_RETRIES):
    """モデルを呼んで出力を確かめる。validator(raw) -> (結果, 問題)。結果が None なら、retries 回までやり直す。
    log(kind, **kw) は入り口の記録(ev)。モデルと通信の失敗でも落とさない(失敗 1 回として数える)。"""
    probs_all = []
    for attempt in range(1 + retries):
        try:
            raw = fn(payload)
        except Exception as e:  # noqa: BLE001  モデル・通信の失敗で落とさない(失敗 1 回として数える)
            raw = None
            probs_all.append(f"exception:{type(e).__name__}")
        res, probs = validator(raw)
        if res is not None:
            if probs:
                log(f"{stage}_repaired", problems=probs)
            if attempt:
                log(f"{stage}_retry_ok")
            return res
        probs_all += probs if isinstance(probs, list) else [probs]
    log(f"{stage}_fallback", problems=probs_all)
    return None


def match_button(current: dict | None, text) -> str | None:
    """今のボタンか、この問いで前に見せた候補(「他の候補」で隠れた前のページ。LINE は前のボタンを消すので、打ち直した)と同じ文。"""
    t = nfkc(text)
    cur = current or {}
    for c in [*cur.get("choices", []), *cur.get("shown", [])]:
        if nfkc(c) == t:
            return c
    return None


def render_prompt(paths: list[Path], variables: dict) -> str:
    """ファイルをつないで {{変数}} を埋める。足りない変数があれば KeyError。文字列でない値は JSON にする。"""
    text = "\n\n".join(p.read_text(encoding="utf-8") for p in paths)
    names = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)))
    missing = [n for n in names if n not in variables]
    if missing:
        raise KeyError(f"prompt variables missing: {missing}")
    for n in names:
        val = variables[n]
        if not isinstance(val, str):
            val = json.dumps(val, ensure_ascii=False, default=str)
        text = text.replace("{{" + n + "}}", val)
    return text
