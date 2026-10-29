"""場面の一覧のコードで判定する条件(evals/scenes/__init__.py の CODE_CHECKS。設計メモ docs/design/conversation-layer.md §3.1)。

判定するのは、場面を回した記録(runner.run_scene の戻り値)。記録の形:
- replies: 本人の発言ごとの返信。{"text", "bubbles", "choices", "counted", "llm_calls", "how", "closed", "states", "profile",
  "parts"?}。how は返事を書いたもの(harness: 相談のハーネス、talk: トークの AI、dropped: 返事なし)。
  closed は相談を閉じた理由(later / result / exit / track。閉じていなければ None)。states は入り口ごとの状態の写し
  ({entry: snap}。snap は runner.career_snap / baito_snap)。profile は dip の共通の値(区分 track と希望の項目)。
  parts は返事の部品の並び([[kind, text]]。新しい層が返すとき。無ければ texts から読み分ける)
- texts: 入り口の決まった文(runner.Texts)。部品の読み分け(問いの文、区切りの行、注意の文)と、数の出どころに使う
- talk_mock: 返事の文が、モックのトークの AI のもの(返事の文を見る条件は判定しない)

結果は 1 条件 1 つの {"check", "at", "status", "detail"}。status は pass / fail / skip(モックでは判定できない。理由は detail)。
決まらない条件(_undecided): 相談を閉じてトークの AI がそのターンを受けたときの、返事の文とトークの AI のツールしだいの条件と、
逆質問の答えの部品。返事が無いターン(スタンプ)は、返事を前提にする条件(NEEDS_REPLY)だけ不合格にする。
"""

from __future__ import annotations

import json
import re

from evals import scenes as S

PASS, FAIL, SKIP = "pass", "fail", "skip"
# 返事の文とボタンを見る条件(トークの AI が返事を書いたターンでは、モックでは判定しない)
REPLY_CHECKS = {"keeps_question", "reply_contains", "reply_not_contains", "choices_include", "choices_exclude",
                "choices_ascending", "order", "ack_varies", "no_repeat", "no_made_up_number", "bubble_max", "one_question",
                "offers_entries"}
# 返事があることを前提にする条件(返事が無ければ不合格。「〜を含まない」「字数」などは、返事が無くても満たす)
NEEDS_REPLY = {"keeps_question", "reply_contains", "choices_include", "choices_ascending", "order", "offers_entries"}
# 相談を閉じてトークの AI に渡したターンでは、トークの AI しだいの条件(モックでは判定しない)
HANDED_CHECKS = {"switches_to", "stays", "profile", "offers_entries"}
# 移る先の入り口を始めるボタンの言葉(入り口の定義の「移る先」の文。設計メモ §4.1、§4.13)。ボタンのどれかがこれを含めば出した
OFFER_WORDS = {"career": ("正社員",), "baito": ("アルバイト", "バイト", "パート")}
SENTENCE_RE = re.compile(r"(?<=[。！!？?])|\n")
AMOUNT_RE = re.compile(r"(\d[\d,]*)\s*(万円|円)")
QMARK_RE = re.compile(r"[?？]")


def nfkc(s) -> str:
    return S.nfkc(s)


def digits(text: str) -> set[str]:
    return S._digits(text)


# ----------------------------- 返事の部品 -----------------------------
def split_parts(text: str, texts) -> list[list[str]]:
    """返事を部品の並び [[kind, text]] に読み分ける(新しい層が parts を返さない今の版のため)。
    問いの文(texts.questions)は ask、区切りの行(texts.phase_lines と「✓」で始まる行)は phase_line、注意の文(texts.notes)は
    note。ほかの文は ack にまとめる(相づちと逆質問の答えは文の形で見分けられないので、answer は parts を返す層だけが出せる)。"""
    t = nfkc(text)
    spans: list[tuple[int, int, str]] = []

    def free(a, b):
        return all(b <= s or a >= e for s, e, _ in spans)
    for kind, pool in (("ask", texts.questions), ("phase_line", texts.phase_lines), ("note", texts.notes)):
        for p in sorted({nfkc(x) for x in pool if nfkc(x)}, key=len, reverse=True):
            i = t.find(p)
            while i >= 0:
                if free(i, i + len(p)):
                    spans.append((i, i + len(p), kind))
                i = t.find(p, i + 1)
    for m in re.finditer(r"^✓[^\n]*", t, flags=re.MULTILINE):
        if free(m.start(), m.end()):
            spans.append((m.start(), m.end(), "phase_line"))
    spans.sort()
    out, pos = [], 0
    for s, e, kind in spans:
        if t[pos:s].strip():
            out.append(["ack", t[pos:s].strip()])
        out.append([kind, t[s:e]])
        pos = e
    if t[pos:].strip():
        out.append(["ack", t[pos:].strip()])
    merged: list[list[str]] = []
    for kind, x in out:
        if merged and merged[-1][0] == kind:
            merged[-1][1] += "\n" + x
        else:
            merged.append([kind, x])
    return merged


def parts_of(reply: dict, texts) -> list[list[str]]:
    return reply.get("parts") or split_parts(reply["text"], texts)


def ack_of(parts: list[list[str]]) -> str | None:
    return next((x for kind, x in parts if kind == "ack"), None)


def _agent_turns(ctx) -> list[list[list[str]]]:
    """history のアシスタントの発言と、場面の返信を、部品の並びにしたもの(順に)。"""
    out = [split_parts(t["text"], ctx["texts"]) for t in ctx["scene"].get("history") or [] if t["who"] == "agent"]
    return out + [parts_of(r, ctx["texts"]) for r in ctx["replies"] if r["how"] != "dropped"]


# ----------------------------- 状態 -----------------------------
def _snap(ctx, i: int, c: dict) -> dict | None:
    entry = c.get("entry", ctx["scene"]["entry"])
    return ctx["replies"][i]["states"].get(entry)


def _slot(snap: dict | None, slot: str) -> dict | None:
    return ((snap or {}).get("slots") or {}).get(slot)


def _asking(ctx, i: int, c: dict) -> tuple[dict | None, str | None]:
    """その入り口が次に聞くこと。聞いていなければ (None, 理由)。"""
    snap = _snap(ctx, i, c)
    if snap is None:
        return None, f"{c.get('entry', ctx['scene']['entry'])} の相談が無い"
    if not snap["active"]:
        return None, "相談が閉じている"
    return snap, None


def _number_ok(e: dict, n) -> bool:
    lo = (e.get("range") or [None])[0]
    return e.get("number") == n or lo == n or e.get("value") == n


# ----------------------------- 条件 -----------------------------
def _asks(ctx, i, c):
    snap, why = _asking(ctx, i, c)
    if snap is None:
        return False, why
    if "qid" in c:
        return snap["qid"] == c["qid"], f"次の問い {snap['qid']}"
    return c["slot"] in snap["targets"], f"次の問い {snap['qid']}({','.join(snap['targets'])})"


def _not_asks(ctx, i, c):
    snap, why = _asking(ctx, i, c)
    if snap is None:
        return False, why
    hit = [s for s in c["slots"] if s in snap["targets"]]
    return not hit, f"次の問い {snap['qid']}" + (f" が {','.join(hit)} を聞く" if hit else "")


def _keeps_question(ctx, i, c):
    snap, why = _asking(ctx, i, c)
    if snap is None:
        return False, why
    r = ctx["replies"][i]
    asking = ctx["scene"]["asking"]
    if snap["qid"] != asking:
        return False, f"次の問いが {snap['qid']}"
    said = [p for p in snap.get("phrasings") or [snap["question"]] if p]  # 会話の層の版は、言い方(variants)のどれでもよい
    if not said or not any(nfkc(p) in nfkc(r["text"]) for p in said):
        return False, "問いの文が返事に無い"
    missing = [x for x in snap["first_choices"] if x not in r["choices"]]
    return not missing, f"候補のボタンが無い: {missing}" if missing else "同じ問いと候補"


def _slot_check(ctx, i, c):
    e = _slot(_snap(ctx, i, c), c["slot"])
    if e is None:
        return False, "欄が空"
    bad = []
    if "contains" in c and nfkc(c["contains"]) not in nfkc(e.get("text") or ""):
        bad.append(f"字「{c['contains']}」が無い")
    mixed = [x for x in c.get("not_contains", []) if nfkc(x) in nfkc(e.get("text") or "")]
    if mixed:
        bad.append(f"ほかの欄の言葉 {mixed} が混じる「{e.get('text')}」")
    if "value" in c:
        v = e.get("value")
        if not (v == c["value"] or (isinstance(v, list) and c["value"] in v)):
            bad.append(f"value {v}")
    if "status" in c and e.get("status") != c["status"]:
        bad.append(f"status {e.get('status')}")
    if "confidence" in c and e.get("confidence") != c["confidence"]:
        bad.append(f"confidence {e.get('confidence')}")
    if "number" in c and not _number_ok(e, c["number"]):
        bad.append(f"number {e.get('number')} range {e.get('range')}")
    return not bad, "; ".join(bad) or f"「{e.get('text')}」({e.get('confidence')})"


def _slot_not_high(ctx, i, c):
    e = _slot(_snap(ctx, i, c), c["slot"])
    return e is None or e.get("confidence") == "low", "空か推測" if e is None or e.get("confidence") == "low" else \
        f"確かめた値「{e.get('text')}」"


def _slot_open(ctx, i, c):
    e = _slot(_snap(ctx, i, c), c["slot"])
    return e is None, "空" if e is None else f"「{e.get('text')}」({e.get('status')}, {e.get('confidence')})"


def _slot_unchanged(ctx, i, c):
    e = _slot(_snap(ctx, i, c), c["slot"]) or {}
    k = (ctx["scene"].get("known") or {})[c["slot"]]
    want = {"status": k.get("status", "filled"), "confidence": k.get("confidence", "high")}
    want |= {"value": k["value"]} if "value" in k else {"text": k.get("text", "")}
    got = {key: e.get(key) for key in want}
    return got == want, f"今は {got}"


def _no_slot_contains(ctx, i, c):
    r = ctx["replies"][i]
    pool = [json.dumps({k: snap.get(k) for k in ("slots", "pending", "notes")}, ensure_ascii=False)
            for snap in r["states"].values()] + [json.dumps(r["profile"], ensure_ascii=False)]
    hit = [x for x in c["texts"] if any(x in p for p in pool)]
    return not hit, f"値に {hit}" if hit else "どの値にも無い"


def _profile(ctx, i, c):
    v = ctx["replies"][i]["profile"].get(c["key"])
    if not v:
        return False, f"{c['key']} が無い"
    return ("contains" not in c or c["contains"] in str(v)), f"{c['key']}「{v}」"


def _reply_contains(ctx, i, c):
    t = nfkc(ctx["replies"][i]["text"])
    return any(nfkc(x) in t for x in c["any"]), "含む" if any(nfkc(x) in t for x in c["any"]) else f"どれも無い: {c['any']}"


def _reply_not_contains(ctx, i, c):
    t = nfkc(ctx["replies"][i]["text"])
    hit = [x for x in c["texts"] if nfkc(x) in t]
    return not hit, f"含む: {hit}" if hit else "含まない"


def _choices_include(ctx, i, c):
    missing = [x for x in c["labels"] if x not in ctx["replies"][i]["choices"]]
    return not missing, f"無い: {missing}" if missing else "ある"


def _choices_exclude(ctx, i, c):
    hit = [x for x in c["labels"] if x in ctx["replies"][i]["choices"]]
    return not hit, f"ある: {hit}" if hit else "無い"


def amounts(labels: list[str]) -> list[int]:
    """ボタンの金額(「こだわらない」など金額の無いラベルは数えない)。"""
    out = []
    for x in labels:
        m = AMOUNT_RE.search(nfkc(x))
        if m:
            out.append(int(m.group(1).replace(",", "")))
    return out


def _choices_ascending(ctx, i, c):
    a = amounts(ctx["replies"][i]["choices"])
    if not a:
        return False, "金額のボタンが無い"
    return a == sorted(a), f"金額の順 {a}"


def _order(ctx, i, c):
    got = [k for k, _ in parts_of(ctx["replies"][i], ctx["texts"])]
    seen = [k for k in got if k in c["parts"]]
    return seen == c["parts"], f"部品の順 {got}"


def _ack_varies(ctx, i, c):
    acks = [ack_of(p) for p in _agent_turns(ctx)]
    tails = [a.rstrip()[-4:] if a else None for a in acks]
    n = c["turns"]
    for j in range(len(tails) - n + 1):
        w = tails[j:j + n]
        if w[0] is not None and len(set(w)) == 1:
            return False, f"相づちの終わり「{w[0]}」が {n} 回続く: {acks[j:j + n]}"
    return True, f"相づちの終わり {tails}"


def _no_repeat(ctx, i, c):
    turns = []
    for p in _agent_turns(ctx):
        sents = set()
        for kind, x in p:
            if kind != "ask":
                sents |= {s.strip() for s in SENTENCE_RE.split(x) if s.strip()}
        turns.append(sents)
    n = c["turns"]
    for j in range(len(turns) - n + 1):
        same = set.intersection(*turns[j:j + n])
        if same:
            return False, f"同じ文が {n} 回続く: {sorted(same)}"
    return True, "続かない"


# 漢数字の数(「五百万円」「千二百円」)。単位が続くか、位の字(十百千万億兆)を含む 2 字以上の並びだけ(「一言」「一緒」は数えない)
KANJI_NUM_RE = re.compile(r"[〇一二三四五六七八九十百千万億兆]+(?=\s*(?:円|万|人|社|件|年|か月|ヶ月|カ月|時間|時|日|歳|才|%|割|倍|名|回|分))"
                          r"|[〇一二三四五六七八九十百千万億兆]*[十百千万億兆][〇一二三四五六七八九十百千万億兆]+"
                          r"|[〇一二三四五六七八九]+[十百千万億兆]")


def kanji_numbers(text: str) -> set[str]:
    return set(KANJI_NUM_RE.findall(nfkc(text)))


def number_sources(ctx, i) -> list[str]:
    """返事で言ってよい数の出どころ(CODE_CHECKS の no_made_up_number): 本人の発言、前の会話、分かっている値、共通の値、
    この返信のボタンと今の問いの候補、渡した事実。"""
    s, r = ctx["scene"], ctx["replies"][i]
    pool = [x["text"] for x in s["says"]] + [t["text"] for t in s.get("history") or []]
    pool += [json.dumps(s.get("known") or {}, ensure_ascii=False), json.dumps(s.get("profile") or {}, ensure_ascii=False)]
    pool += list(r["choices"]) + [c for snap in r["states"].values() for c in snap.get("first_choices") or []]
    pool += [f for c in s["expect"] if c["check"] == "answers_from_facts" for f in c["facts"]]
    return [nfkc(x) for x in pool]


def said_by_harness(text: str, ctx, i) -> str:
    """返事から、入り口の決まった文(問いの文、区切り、注意、決まった文、危ない言葉の文)と、進み具合の行の数の印
    (texts.strip)を除いたもの。決まった文の数(窓口の番号、「3つだけ」)は作った数ではない。"""
    t, texts = nfkc(text), ctx["texts"]
    fixed = [*texts.questions, *texts.phase_lines, *texts.notes, *texts.fixed, *texts.crisis,
             *(snap.get("question") or "" for snap in ctx["replies"][i]["states"].values())]
    for x in sorted({nfkc(f) for f in fixed if nfkc(f)}, key=len, reverse=True):
        t = t.replace(x, "\n")
    for pat in texts.strip:
        t = re.sub(pat, " ", t)
    return t


def _no_made_up_number(ctx, i, c):
    rest = said_by_harness(ctx["replies"][i]["text"], ctx, i)
    pool = number_sources(ctx, i)
    made = sorted(digits(rest) - set().union(*map(digits, pool)))
    made += sorted(k for k in kanji_numbers(rest) if not any(k in p for p in pool))
    return not made, f"出どころの無い数 {made}" if made else "どの数にも出どころがある"


def _bubble_max(ctx, i, c):
    over = [len(re.sub(r"\s", "", b)) for b in ctx["replies"][i]["bubbles"]]
    return all(n <= c["chars"] for n in over), f"吹き出しの字数 {over}"


def _one_question(ctx, i, c):
    n = len(QMARK_RE.findall(ctx["replies"][i]["text"]))
    return n <= 1, f"問いの印 {n} つ"


def _counted(ctx, i, c):
    r = ctx["replies"][i]
    return bool(r["counted"]), "数えた" if r["counted"] else "数えない"


def _not_counted(ctx, i, c):
    r = ctx["replies"][i]
    return not r["counted"], "数えない" if not r["counted"] else "数えた"


def _closed(ctx, i, c):
    why = ctx["replies"][i]["closed"]
    if why is None:
        return False, "閉じない"
    return ("why" not in c or c["why"] == why), f"閉じた({why})"


def _not_closed(ctx, i, c):
    why = ctx["replies"][i]["closed"]
    return why is None, "閉じない" if why is None else f"閉じた({why})"


def _phase(ctx, i, c):
    snap = _snap(ctx, i, c)
    got = (snap or {}).get("phase")
    return got == c["phase"], f"フェーズ {got}"


def crisis_marks(texts) -> list[str]:
    """危ない言葉への決まった文の印: 相談窓口の行(どの入り口でも同じ窓口を出す。設計メモ §4.11)。"""
    return [x for x in texts.crisis if x.startswith("・")]


def _crisis_reply(ctx, i, c):
    r = ctx["replies"][i]
    missing = [x for x in crisis_marks(ctx["texts"]) if nfkc(x) not in nfkc(r["text"])]
    if missing:
        return False, "相談窓口の決まった文が無い"
    return r["llm_calls"] == 0, f"モデルの呼び出し {r['llm_calls']} 回"


def _llm_calls_max(ctx, i, c):
    r = ctx["replies"][i]
    note = "(トークの AI が受けたので、少なくとも 1 回)" if r["how"] == "talk" and not ctx.get("talk_mock") else ""
    return r["llm_calls"] <= c["n"], f"モデルの呼び出し {r['llm_calls']} 回{note}"


def _active(r: dict) -> list[str]:
    return [e for e, snap in r["states"].items() if snap["active"]]


def _switches_to(ctx, i, c):
    r, entry = ctx["replies"][i], ctx["scene"]["entry"]
    active = _active(r)
    if c["entry"] in active and entry not in active:
        return True, f"{c['entry']} に移った"
    if r["how"] == "talk" and entry != "talk":
        return False, "相談を閉じてトークの AI に渡した(移る先はトークの AI しだい)"
    return False, f"移らない(相談中: {active or 'なし'})"


def _stays(ctx, i, c):
    r, entry = ctx["replies"][i], ctx["scene"]["entry"]
    active = _active(r)
    if entry == "talk":
        return not active, "相談を始めない" if not active else f"相談を始めた: {active}"
    ok = active == [entry] and r["closed"] is None
    return ok, f"相談中: {active or 'なし'}" + (f"、閉じた({r['closed']})" if r["closed"] else "")


def _offers_entries(ctx, i, c):
    ch = ctx["replies"][i]["choices"]
    missing = [e for e in c["entries"] if not any(w in x for x in ch for w in OFFER_WORDS[e])]
    return not missing, f"移るボタンが無い: {missing}(ボタン {ch})" if missing else "移るボタンがある"


CHECKS = {
    "asks": _asks, "not_asks": _not_asks, "keeps_question": _keeps_question, "slot": _slot_check,
    "slot_not_high": _slot_not_high, "slot_open": _slot_open, "slot_unchanged": _slot_unchanged,
    "no_slot_contains": _no_slot_contains, "profile": _profile, "reply_contains": _reply_contains,
    "reply_not_contains": _reply_not_contains, "choices_include": _choices_include, "choices_exclude": _choices_exclude,
    "choices_ascending": _choices_ascending, "order": _order, "ack_varies": _ack_varies, "no_repeat": _no_repeat,
    "no_made_up_number": _no_made_up_number, "bubble_max": _bubble_max, "one_question": _one_question, "counted": _counted,
    "not_counted": _not_counted, "closed": _closed, "not_closed": _not_closed, "phase": _phase,
    "crisis_reply": _crisis_reply, "llm_calls_max": _llm_calls_max, "switches_to": _switches_to, "stays": _stays,
    "offers_entries": _offers_entries,
}
WHOLE = {"ack_varies", "no_repeat"}  # 会話の全体を見る(at を使わない)


def indices(c: dict, n: int) -> list[int]:
    at = c.get("at")
    return list(range(n)) if at == "all" else [n - 1 if at is None else at]


def _undecided(name: str, c: dict, ctx: dict, i: int) -> str | None:
    """モックでは決まらない条件なら、その理由。"""
    r, entry = ctx["replies"][i], ctx["scene"]["entry"]
    if r["how"] == "talk" and entry != "talk":  # 相談を閉じて、トークの AI がこのターンを受けた
        if name in REPLY_CHECKS:
            return "返事はトークの AI が書く(モックでは判定しない)"
        if name in HANDED_CHECKS or (name in S.STATE_CHECKS and c.get("entry", entry) != entry):
            return "トークの AI がどのツールを呼ぶかで決まる(区分の切り替え、相談を始める、希望の保存)"
        if name == "llm_calls_max" and r["llm_calls"] <= c["n"]:
            return "トークの AI の呼び出しの数はモックでは決まらない(少なくとも 1 回)"
    if name == "order" and "answer" in c["parts"] and not r.get("parts"):
        return "逆質問の答え(answer)は、部品の並びを返す層だけが見分けられる(文の形からは相づちと分けられない)"
    return None


def judge_code(c: dict, ctx: dict, mock_only: set[str] | None = None) -> dict:
    """1 つのコードの条件を判定する。mock_only は、モックで判定する条件(ほかは skip。トークの AI の場面)。
    at が all のときは、どれか 1 つの返信が不合格なら不合格、そうでなく決まらない返信があれば skip。"""
    name, replies = c["check"], ctx["replies"]
    out = {"check": name, "at": c.get("at"), "args": {k: v for k, v in c.items() if k not in ("check", "at")}}
    if mock_only is not None and name not in mock_only:
        return out | {"status": SKIP, "detail": "トークの AI しだい(モックでは守りの前処理と stays だけを見る。9 枚目で実モデルで見る)"}
    idx = [len(replies) - 1] if name in WHOLE else indices(c, len(replies))
    many = len(idx) > 1
    details, skipped = [], []
    for i in idx:
        r = replies[i]
        why = _undecided(name, c, ctx, i)
        if why:
            skipped.append(f"返信 {i}: {why}")
            continue
        if name in NEEDS_REPLY and r["how"] == "dropped":
            return out | {"status": FAIL, "detail": f"返信 {i}: 返事が無い({r.get('note', '')})"}
        ok, detail = CHECKS[name](ctx, i, c)
        details.append(f"{i}: {detail}" if many else detail)
        if not ok:
            return out | {"status": FAIL, "detail": details[-1]}
    if skipped:
        return out | {"status": SKIP, "detail": " / ".join(skipped)}
    return out | {"status": PASS, "detail": " / ".join(details)}


def evaluate(scene: dict, record: dict, mock_only: set[str] | None = None) -> list[dict]:
    """場面のコードの条件を全部判定する(採点の条件は judge.py。ここでは skip で返す)。"""
    ctx = {"scene": scene, "replies": record["replies"], "texts": record["texts"], "talk_mock": record.get("talk_mock")}
    out = []
    for c in scene["expect"]:
        if S.is_code(c):
            out.append(judge_code(c, ctx, mock_only))
        else:
            out.append({"check": c["check"], "at": c.get("at"), "args": {k: v for k, v in c.items() if k not in ("check", "at")},
                        "status": SKIP, "detail": "採点の条件(judge.py のジャッジで見る。実モデルの回は 9 枚目)", "judge": True})
    return out
