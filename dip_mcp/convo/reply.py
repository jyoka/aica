"""言う段: 返事の部品を 1 つの吹き出しに組み、モデルの返事を確かめる(設計メモ docs/design/conversation-layer.md §4.8)。

返事は、決まった順の部品の並び(PARTS): 相づち(ack)→ 逆質問の答え(answer)→ 注意(note)→ 区切り(phase_line)→ 問い(ask)。
何を並べるか(相づちの種類、答える事実、注意)は応じ方の計画(dip_mcp.convo.plan)で決まり、入り口のハーネスがそれを言う段の計画
(new_plan)にする。モデルが書くのは相づちと答えの言い方だけ。

- 問いの文は、質問バンクの言い方(phrasings。1 つ目は問いの text、続けて variants)から選ぶ。モデルはそのどれかを一字一句そのまま使う。
  同じ問いを出し直すときは、前に使った言い方を避ける(pick)
- 注意と区切りは、データの文をそのまま、ハーネスが相づちと問いの間に置く。モデルの返事に無くてもよく、あれば同じ場所にあるかを見る
- モデルの返事の確かめ(validate): 問いの言い方が一字一句あるか、相づちが 1 文で ack_max 字までか、ほめ言葉(本人の発言をなぞった言葉は除く)、問いが 2 つ、
  出どころの無い数、氏名などをなぞっていないか、相づちの終わり 4 字が直前の 2 つと同じでないか(「〜ですね。」が 3 回続かない)、
  直前の返信と同じ文が無いか、吹き出しの字数
- だめならモデルを 1 回呼び直し、それでもだめなら決まった組み立て(fallback): 入り口のデータの相づち(種類ごとに 3 つ、順に回す)+
  注意 + 区切り + 問い。直した・戻ったターン(計画の about)は、その文を先に試す。相づちも答えも無い計画は、モデルを呼ばずに決まった組み立てにする(needs_model)
"""

from __future__ import annotations

import re

from dip_mcp.convo import guard
from dip_mcp.convo import plan as P
from dip_mcp.convo.text import digits_in, nfkc
from dip_mcp.convo.turn import parse_json

PARTS = P.PARTS
LINE_PARTS = ("note", "phase_line")  # 1 行ずつ置く部品(ほかは同じ行につなぐ)
ACK_KINDS = P.ACKS  # 相づちの種類(応じ方の計画の ack。入り口のデータの相づちの組も同じ種類で置く)
BUBBLE_MAX = 80
ANSWER_BUBBLE_MAX = 120  # 逆質問の答えがあるときだけ(§6 の 20)
ACK_MAX = 40  # 相づちは 1 文、40 字まで
ACK_MIN = 4  # 相づちに使える字数がこれより少なければ、相づちを付けない
ANSWER_SENTENCES = 3  # 答えのある返事の、問いの前の文の数の上限(相づち 1 + 答え 2)
TAIL = 4  # 相づちの終わりを比べる字数
PRAISE_RE = re.compile(r"すごい|凄い|素晴らし|すばらし|さすが|立派|優秀|見事|えらい|偉い|素敵|すてき|いいですね|頼もし|感心")
BLAME_RE = re.compile(r"読み取れ|理解できませんでした|わかりませんでした|分かりませんでした")  # 本人を責めて聞こえる言い方
# 漢数字の数(場面の一覧の evals/scenes/checks.py と同じ): 単位が続く並び(「五年」「三人」)か、位の字を含む 2 字以上の並び
KANJI_NUM_RE = re.compile(r"[〇一二三四五六七八九十百千万億兆]+(?=\s*(?:円|万|人|社|件|年|か月|ヶ月|カ月|時間|時|日|歳|才|%|割|倍|名|回|分))"
                          r"|[〇一二三四五六七八九十百千万億兆]*[十百千万億兆][〇一二三四五六七八九十百千万億兆]+"
                          r"|[〇一二三四五六七八九]+[十百千万億兆]")
QUESTION_END = ("", "。", "?")  # 問いの言い方の後ろに付いてよいもの(句点の無い言い方に、モデルが付けたとき)
QMARK_RE = re.compile(r"[?？]")
SENTENCE_RE = re.compile(r"(?<=[。！!？?])")


def chars(s) -> int:
    """字数(空白を数えない)。"""
    return len(re.sub(r"\s", "", str(s)))


def sentences(text) -> list[str]:
    return [s.strip() for s in SENTENCE_RE.split(nfkc(text)) if s.strip()]


def tail(text) -> str | None:
    t = nfkc(text or "").rstrip()
    return t[-TAIL:] if t else None


# ----------------------------- 計画 -----------------------------
def pick(phrasings: list[str], avoid: str | None) -> tuple[str, list[str]]:
    """(勧める言い方, 使ってよい言い方)。avoid は、同じ問いを出し直すときに前に使った言い方(新しい問いなら None)。
    出し直しでは、前の言い方の次のもの(順に回す)を勧め、前の言い方は使わせない。"""
    if not avoid or len(phrasings) < 2:
        return phrasings[0], list(phrasings)
    i = phrasings.index(avoid) if avoid in phrasings else -1
    allowed = [p for p in phrasings if p != avoid]
    return phrasings[(i + 1) % len(phrasings)], allowed


def new_plan(ask: dict, ack: str | None = None, hook: str | None = None, answer: dict | None = None,
             note=(), phase_line=(), choices=(), said: str = "", hidden=(), about=()) -> dict:
    """言う段の計画。ask は {"qid", "mode", "text": 勧める言い方, "phrasings": 使ってよい言い方}。ack は相づちの種類(ACK_KINDS か None)、
    answer は逆質問の答え({"facts": [渡す事実], "fallback": 決まった文})。渡す事実が無ければ、モデルに書かせずに決まった文で答える
    (fixed_answer)。note と phase_line は決まった文の行。choices はボタン(数の出どころ)、said は本人の発言(伏せたもの)、
    hidden は返事に出さない部分(氏名と要配慮)。about は相づちで触れること(計画の about。「勤務地を直しました」)。相づちの字数は、吹き出しの字数から決まった部品の字数を引いた残り(ACK_MAX まで)。"""
    fixed_answer = answer.get("fallback") if answer and not answer.get("facts") else None
    answer = answer if answer and answer.get("facts") else None
    limit = ANSWER_BUBBLE_MAX if answer or fixed_answer else BUBBLE_MAX
    fixed = chars("".join([fixed_answer or "", *note, *phase_line])) + max(chars(p) for p in ask["phrasings"])
    ack_max = min(ACK_MAX, limit - fixed)
    if ack_max < ACK_MIN:
        ack = None
    return {"ack": ack if ack in ACK_KINDS else None, "hook": hook, "answer": answer, "fixed_answer": fixed_answer,
            "note": list(note), "phase_line": list(phase_line), "ask": ask, "limit": limit, "ack_max": max(ack_max, 0),
            "choices": list(choices), "said": said, "hidden": list(hidden), "facts": list((answer or {}).get("facts", [])),
            "about": list(about)}


def needs_model(plan: dict) -> bool:
    """モデルに書かせるものがあるか(相づちか、渡す事実のある逆質問の答え)。無ければ決まった組み立てだけで返す。"""
    return bool(plan["ack"] or plan["answer"])


# ----------------------------- 組み立て -----------------------------
def render(parts: list[list[str]]) -> str:
    """部品の並びを 1 つの吹き出しにする。注意と区切りは 1 行ずつ、相づち・答え・問いは同じ行につなぐ。"""
    lines: list[str] = []
    inline = False
    for kind, text in parts:
        if kind in LINE_PARTS or not inline:
            lines.append(text)
        else:
            lines[-1] += text
        inline = kind not in LINE_PARTS
    return "\n".join(lines)


def _fixed_lines(plan: dict) -> list[str]:
    """ハーネスが置く決まった文(相づちと問いの間に、この順)。"""
    return [*([plan["fixed_answer"]] if plan.get("fixed_answer") else []), *plan["note"], *plan["phase_line"]]


def _fixed_parts(plan: dict, ask_text: str) -> list[list[str]]:
    return [*([["answer", plan["fixed_answer"]]] if plan.get("fixed_answer") else []), *(["note", x] for x in plan["note"]),
            *(["phase_line", x] for x in plan["phase_line"]), ["ask", ask_text]]


def fixed_ack(plan: dict, recent: dict, acks: dict, turn: bool = True) -> str | None:
    """決まった相づちを 1 つ選ぶ。acks は入り口のデータの相づち({種類: [文, ...]})。recent は層の覚え(dip_mcp.convo.state)で、
    種類ごとの回し方の位置もここに覚える(turn が False なら、位置を進めずに見るだけ)。直前の相づちと終わりが重なるもの、
    直前の返信と同じ文、字数を超えるものは飛ばす。計画に触れること(about)が 1 つあれば、その文を先に試す(「勤務地を直しました。」)。"""
    kind = plan["ack"]
    pool = list(acks.get(kind) or []) if kind else []
    about = plan.get("about") or []
    said = f"{about[0]}。" if kind and len(about) == 1 else None
    if said and chars(said) <= plan["ack_max"] and not repeats(said, recent):
        return said
    rot = recent.setdefault("rotation", {}) if turn and pool else recent.get("rotation") or {}
    start = rot.get(kind, 0)
    for k in range(len(pool)):
        cand = pool[(start + k) % len(pool)]
        if chars(cand) <= plan["ack_max"] and not repeats(cand, recent):
            if turn:
                rot[kind] = (start + k + 1) % len(pool)
            return cand
    return None


def fallback(plan: dict, recent: dict, acks: dict) -> list[list[str]]:
    """決まった組み立て: 決まった相づち(fixed_ack)+ 答えの決まった文 + 注意 + 区切り + 問い。"""
    ack = fixed_ack(plan, recent, acks)
    parts = [["ack", ack]] if ack else []
    if plan["answer"] and plan["answer"].get("fallback"):
        parts.append(["answer", plan["answer"]["fallback"]])
    return parts + _fixed_parts(plan, plan["ask"]["text"])


# ----------------------------- 確かめ -----------------------------
def repeats(ack: str, recent: dict) -> str | None:
    """相づちが、直前の返信と重なるか。終わり 4 字が直前の 2 つの相づちと同じ(3 回続く)か、直前の返信と同じ文があるか。"""
    prev = list(recent.get("acks") or [])[-2:]
    t = tail(ack)
    if len(prev) == 2 and t and all(tail(p) == t for p in prev):
        return "ack_repeats"
    last = (recent.get("replies") or [None])[-1]
    if last and set(sentences(ack)) & set(sentences(last)):
        return "repeats_last"
    return None


ECHO = 2  # ほめ言葉をなぞりと見るのに、本人の発言と同じでなければならない前か後ろの字数


def praised(free: str, said: str = "") -> str | None:
    """ほめ言葉 -> 見つけた言葉か None。本人の発言をなぞった言葉(「すごい混んでる店は苦手」の「すごい混ん」)は除く。
    言葉だけでなく、前か後ろの ECHO 字も本人の発言と同じときだけなぞりと見る(「すごい残業」の後の「すごいですね」はほめ言葉)。"""
    own = nfkc(said or "")

    def echoed(m) -> bool:
        a, b = m.start(), m.end()
        return bool(own) and ((a >= ECHO and free[a - ECHO:b] in own) or (b + ECHO <= len(free) and free[a:b + ECHO] in own))
    return next((m.group(0) for m in PRAISE_RE.finditer(free) if not echoed(m)), None)


def _numbers_ok(free: str, plan: dict) -> bool:
    pool = " ".join([plan["said"], *plan["choices"], *map(str, plan["facts"]), *_fixed_lines(plan)])
    if digits_in(free) - digits_in(pool):
        return False
    return all(k in nfkc(pool) for k in KANJI_NUM_RE.findall(free))


def validate(raw, plan: dict, recent: dict) -> tuple[list[list[str]] | None, str | None]:
    """モデルの返事 -> (部品の並び, None) か (None, 問題)。返事は {"reply": 文}(か文だけ)。
    相づちと答えは問いの言い方より前の文。注意と区切りの文は、あれば問いの前に順に並んでいること(無ければハーネスが足す)。"""
    d = parse_json(raw)
    reply = d.get("reply") if d else (raw if isinstance(raw, str) and not raw.strip().startswith("{") else None)
    if not isinstance(reply, str) or not reply.strip():
        return None, "no_reply"
    r = nfkc(reply)
    found = [(r.rfind(nfkc(p)), p) for p in plan["ask"]["phrasings"] if nfkc(p) in r]
    if not found:
        return None, "question_missing"
    at, ask_text = max(found, key=lambda x: (x[0], len(x[1])))
    if r[at + len(nfkc(ask_text)):].strip() not in QUESTION_END:
        return None, "text_after_question"
    head = r[:at]
    pos = 0
    lines = [nfkc(x) for x in _fixed_lines(plan)]
    for line in lines:  # モデルが書き写した決まった文: 順に並んでいれば外す
        i = head.find(line, pos)
        if i < 0:
            continue
        if pos and head[pos:i].strip():
            return None, "text_between_parts"
        head = head[:i] + "\n" * len(line) + head[i + len(line):]
        pos = i + len(line)
    if pos and head[pos:].strip():
        return None, "text_between_parts"
    free = re.sub(r"\s*\n\s*", "", head).strip()
    if any(line in free for line in lines):  # 順の違う場所に書き写した(吹き出しに 2 回出る)
        return None, "text_between_parts"
    if free and not needs_model(plan):
        return None, "extra_text"
    sents = sentences(free)
    if plan["answer"]:
        if len(sents) > ANSWER_SENTENCES:
            return None, "too_much_extra"
    elif len(sents) > 1 or chars(free) > plan["ack_max"]:
        return None, "too_much_extra"
    if praised(free, plan["said"]):
        return None, "praise"
    if BLAME_RE.search(free):
        return None, "blame"
    if QMARK_RE.search(free):
        return None, "two_questions"
    if not _numbers_ok(free, plan):
        return None, "made_up_number"
    if guard.leaks(free, plan["hidden"]):
        return None, "personal_echo"
    ack, answer = free, ""
    if plan["answer"]:
        ack, answer = (sents[0], "".join(sents[1:])) if plan["ack"] and len(sents) > 1 else ("", free)
        if not answer:
            return None, "answer_missing"
    if ack and (why := repeats(ack, recent)):
        return None, why
    parts = ([["ack", ack]] if ack else []) + ([["answer", answer]] if answer else []) + _fixed_parts(plan, ask_text)
    if chars(render(parts)) > plan["limit"]:
        return None, "too_long"
    return parts, None


def ack_of(parts: list[list[str]]) -> str | None:
    return next((x for kind, x in parts if kind == "ack"), None)


def ask_of(parts: list[list[str]]) -> str | None:
    return next((x for kind, x in parts if kind == "ask"), None)


# ----------------------------- 「他の候補」 -----------------------------
def more_lead(texts: dict, topic: str, kind: str | None) -> str:
    """「他の候補」の吹き出し(モデルを呼ばない)。問いの短い名前から作る(「年収のほかの区切りです。」。jyoka/tasks#326 の A)。
    数の問いは区切り(texts の more_lead_number)、ほかは候補(more_lead)。"""
    template = texts.get("more_lead_number") if kind == "number" and texts.get("more_lead_number") else texts["more_lead"]
    return template.replace("{topic}", topic)
