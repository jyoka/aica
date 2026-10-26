"""1 つの会話を回して、指標と関門を出す(設計メモ §6.1、§6.4)。

毎ターン: AI の返信を読む(時間を足す)→ ジャッジがラベルを付ける → いらいらを数える → result か離脱かを決める
→ 本人役が返す(ボタンか文。時間を足す)→ 形に送る。離脱を決めるのはコード(actor.Clock)。
result の返信を読んで 5 分を超えても、result に着いたので完了に数える。
会話の後で、取れた値(形 A は会話の記録から取り出す)を事実表と比べ、関門をコードで数える。
"""

from __future__ import annotations

import json
from dataclasses import asdict

from evals.fulltime_hearing import judge
from evals.fulltime_hearing.actor import (
    LEAVE_TEXT,
    SHORT_ANSWER,
    Clock,
    actor_turn,
    chars,
    render,
    resolve,
)
from evals.fulltime_hearing.personas import hard_keys

MAX_AI_TURNS = 30  # 1 会話の AI の返信の上限(費用の歯止め。ふつうは 5 分の上限で先に止まる)


def made_up_numbers(saved: list[str], user_texts: list[str], allowed: str = "") -> list[str]:
    """保存した文の数字のうち、本人の発言(押したボタンのラベルを含む)にも、本人の履歴書(allowed)にも無いもの。"""
    said: set[float] = set()
    for t in [*user_texts, allowed]:
        said |= judge.numbers_in_text(t)
    return [f"「{s}」の {n:g}" for s in saved for n in sorted(judge.numbers_in_text(s) - said)]


def _resume(persona: dict) -> dict | None:
    """先に送った履歴書の読み取り結果(evals.scenarios.load_resume の形)。無ければ None。"""
    if not persona.get("resume"):
        return None
    from evals.scenarios import load_resume

    return load_resume(persona["resume"]["file"])


async def run_conversation(form, persona: dict, client, sim_model: str, judge_model: str,
                           max_turns: int = MAX_AI_TURNS, same_samples: int = judge.SAME_SAMPLES) -> dict:
    clock = Clock()
    transcript: list[dict] = []
    turns: list[dict] = []
    user_msgs: list[dict] = []
    completed = False
    reply = await form.start()
    for n in range(1, max_turns + 1):
        transcript.append({"who": "agent", "text": render(reply)})
        read_s = clock.read(reply)
        labels = await judge.label_turn(client, judge_model, transcript)
        new = clock.irritate(n, reply, labels)
        row = {"turn": n, "reply": asdict(reply), "read_seconds": round(read_s, 1), "labels": labels,
               "irritations": new, "seconds": round(clock.seconds, 1)}
        turns.append(row)
        if reply.done:
            completed = True
            break
        if reply.closed:  # result に着かずに会話が閉じた(形 A の wait か、本人の断り)。完了に数えない
            clock.left = reply.closed
            break
        if why := clock.should_leave():
            clock.left = why
            if why == "irritation":
                transcript.append({"who": "user", "text": LEAVE_TEXT})
            break
        if n == max_turns:  # AI の返信の上限。本人役と形にもう 1 回送らない(読まずに捨てる返信に費用を払わない)
            clock.left = "turn_cap"
            break
        tired = clock.tired
        t = await actor_turn(client, sim_model, persona, transcript, tired)
        text, pressed = resolve(t, reply.buttons)
        if not text:
            clock.left = "no_reply"
            break
        spent = clock.press() if pressed else clock.type(text)
        row["actor"] = {"text": text, "pressed": pressed, "tired": tired, "seconds": round(spent, 1),
                        "invalid_button": bool(t.button.strip()) and not pressed,
                        # 疲れたのに長く打った(本人役の崩れ。記録だけ。ボタンが「パス」だけのときに短く打つのはよい)
                        "tired_ignored": tired and not pressed and chars(text) > SHORT_ANSWER}
        transcript.append({"who": "user", "text": f"[ボタン] {text}" if pressed else text})
        user_msgs.append({"text": text, "pressed": pressed})
        if clock.should_leave() == "time":  # 打っているうちに 5 分を超えた
            clock.left = "time"
            break
        reply = await form.send(text, pressed)

    captured = form.captured()
    extracted = captured is None
    if extracted:
        resume = _resume(persona)
        captured = await judge.extract_facts(client, judge_model, transcript, persona, resume and resume["resume"])
    facts = await judge.score_facts(client, judge_model, persona, captured, same_samples)
    correct = sum(1 for f in facts if f["correct"])
    hard_empty = ([k for k in hard_keys(persona) if (captured.get(k) or {"status": "missing"})["status"] == "missing"]
                  if completed else [])
    gates = {
        "made_up_numbers": made_up_numbers(form.saved_texts(), [m["text"] for m in user_msgs], json.dumps(_resume(persona) or {}, ensure_ascii=False)),
        "wasted": form.wasted(),
        "two_questions_turns": [r["turn"] for r in turns if r["labels"].get("two_questions")],
        "leading_turns": [r["turn"] for r in turns if r["labels"].get("leading")],
        "hard_empty": hard_empty,
    }
    presses, typed = clock.presses, clock.typed_replies
    metrics = {
        "facts_correct": correct, "facts_total": len(facts), "facts_rate": round(correct / len(facts), 3),
        "completed": completed, "left": clock.left, "seconds": round(clock.seconds, 1),
        "typed_chars": clock.typed_chars, "button_ratio": round(presses / (presses + typed), 3) if presses + typed else None,
        "ai_turns": len(turns), "irritations": len(clock.irritations),
        "tired_ignored": sum(1 for r in turns if (r.get("actor") or {}).get("tired_ignored")),
        "invalid_buttons": sum(1 for r in turns if (r.get("actor") or {}).get("invalid_button")),
        "has_resume": bool(persona.get("resume")),
    }
    return {"metrics": metrics, "gates": gates, "facts": facts, "captured": captured, "extracted": extracted,
            "irritations": clock.irritations, "turns": turns, "transcript": transcript}
