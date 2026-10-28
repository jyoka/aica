"""正社員のヒアリングのハーネス(dip_mcp/career)の回帰: モックの本人役 6 人 × 形 B、C、D(費用 0。実 API を呼ばない)。

    ./.venv/bin/python -m evals.career_sim                      # 表と合否。最後に「すべて合格」
    ./.venv/bin/python -m evals.career_sim --log evidence/career_sim   # 同じ。結果の JSON と会話ログを DIR にも書く

設計メモ docs/design/fulltime-hearing.md の §6.2 の表の「モック ○」の行(完了率、推定時間、ターン、HARD が空、wasted、
予算で止まること)と、PRD v4.1 の合否指標(§4-1)と v3 の受け入れチェック(§11.3 の 1〜3、5〜7)を正社員に置き換えたものを見る。

- 本人役(MockActor): 1/5 の事実表(evals/personas/fulltime_hearing)から答える。合うボタンがあれば押し、無ければ事実表の文を
  打つ(数は「120社」のように単位の頭を付けて)。疲れたら(§6.1、3 分を超えたら)打つ文を 10 字までにする。
  事実表に無い欄はパス。p4 は自由に答える問いに 2 回まで「特にない」。場面の二択と逆選択は crc32 で決めたものを押す
- 時間といらいらは 1/5 の Clock(evals/fulltime_hearing/actor.py)で数える。いらいらはコードで数えられるもの
  (吹き出しが 80 字を超えた、もう答えた欄を聞かれた)だけ。5 分を超えるか、いらいらが 3 回で離脱
- 読み取り(OracleLLM): 本人役が言いたかったことを、そのまま正解として返す。モックで確かめられるのはハーネスの動きだけ(§6.2)
- 履歴書を先に送っている人(p5)は、resume.filled の欄を事実表の値で prefill する(1/5 の ScriptGood と同じ)
- result に着いたら、職歴カードから職務経歴書の下書きを作る(dip_mcp/career/draft.py。task 247)。関門「数字を作った: 0 件」(§6.4)を
  コードで見る: 下書きに書く欄の数字が、本人役の発言(押したボタンのラベルも)と履歴書の欄に無ければ made_up_number に数える。
  下書きの空欄の数が、結果の「空欄はNつ」、形 D の完成のカルーセルの「空欄Nつ」、職歴カードの空欄(harness.blank_cards)と空欄に戻した
  数字の和のどれかと違えば draft_blanks に数える
- 形 D(task 246)は、そのターンの Flex(dip_mcp/career/ui.py が選び、line_runtime/career_ui.py が作る)の中の字も本人役が読む
  (§6.1)。「始める」の 1 枚は「始める」を押す。Flex の字数(職歴カード 60 字、帯 24 字、プロフィールカード 80 字)も見る。
  帯を切った形 D は run_one(band=False)
"""

from __future__ import annotations

import argparse
import json
import sys
import zlib
from collections import Counter
from pathlib import Path

from dip_mcp.career import draft as D
from dip_mcp.career import harness as H
from dip_mcp.career import ui as UI
from evals.fulltime_hearing import judge as J
from evals.fulltime_hearing.actor import SHORT_ANSWER, AIReply, Clock, fact_text
from evals.fulltime_hearing.personas import hard_keys, load_personas
from line_runtime import career_ui, progress_ui
from resume_lines import blank_count, check_numbers

FORMS = ("B", "C", "D")
MAX_STEPS = 60  # 1 会話の処理の上限(PRD §4-1 の M-loop)
NOTHING = "特にない"
# 合否の指標(どれも 0 で合格)。PRD v4.1 §4-1 と v3 §11.3 の置き換え
GATES = ("crash", "loop", "not_reached", "left", "hard_empty", "low_hard", "no_review", "no_segment_q", "wasted",
         "repeated", "irrelevant", "dropped", "spam_hard_high", "phase_mismatch", "over_max_turns", "budget_violation",
         "form_violation", "ui_violation", "reask", "made_up_number", "draft_blanks")


def _crc(*parts) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


def prefill(persona: dict) -> dict:
    """履歴書の読み取りで埋まる欄(事実表の値)。"""
    facts = {f["key"]: f for f in persona["facts"]}
    out = {}
    for k in (persona.get("resume") or {}).get("filled", []):
        f = facts[k]
        num = f.get("value") if f["kind"] == "number" else None
        out[k] = {"text": fact_text(f), "number": num, "unit": J.unit_head(f.get("unit")) if num is not None else None}
    return out


class OracleLLM:
    """読み取りは、本人役が言いたかったこと(meaning)をそのまま返す。生成は質問の文をそのまま返す。"""

    def __init__(self):
        self.meaning: dict = {}
        self.returned: list[dict] = []
        self.calls = Counter()

    def extract(self, payload):
        self.calls["extract"] += 1
        m = self.meaning
        self.returned += [dict(u) for u in m.get("slots", [])]
        return json.dumps({"slot_updates": m.get("slots", []), "ng_updates": m.get("ng", []), "segment": m.get("segment"),
                           "intent": m.get("intent", "answer"), "review_ack": m.get("review_ack")}, ensure_ascii=False)

    def generate(self, view):
        self.calls["generate"] += 1
        return json.dumps({"reply": view["text"]}, ensure_ascii=False)


class FlakyOracleLLM(OracleLLM):
    """壊れた出力を混ぜる読み取り(テストの負荷用): 文の答えごとに、1 回目の呼び出しは JSON でない文を返す。
    ハーネスは 1 回やり直すので、答えは 2 回目で読める。"""

    def __init__(self):
        super().__init__()
        self._broken = False

    def extract(self, payload):
        self._broken = not self._broken
        if self._broken:
            self.calls["broken"] += 1
            return "すみません、うまく読めませんでした"
        return super().extract(payload)


class MockActor:
    """事実表から答える本人役。answer() は (送る文, ボタンか, 読み取りの正解)。"""

    def __init__(self, persona: dict):
        self.p = persona
        self.facts = {f["key"]: f for f in persona["facts"]}
        self.other = persona.get("other", {})
        self.nothing_left = 2 if persona["id"] == "p4-short" else 0
        self.answered: set[str] = set(prefill(persona))

    def asked_again(self, state, bank) -> list[str]:
        """今の問いの欄のうち、もう答えた(履歴書で送った)もの。"""
        cur = state.current or {}
        if cur.get("mode") not in ("ask",):
            return []
        q = H.qmap(state, bank).get(cur["qid"]) or {}
        return [t for t in q.get("targets", []) if t in self.answered]

    def answer(self, state, bank, out: dict, tired: bool) -> tuple[str, bool, dict]:
        cur = state.current
        buttons = out["choices"]
        if cur["mode"] == "start":
            return H.START_GO, True, {}
        if cur["mode"] == "review":
            return H.REVIEW_OK, True, {"review_ack": "all"}
        q = H.qmap(state, bank)[cur["qid"]]
        shown = [c for c in q["choices"] if c in buttons]  # 画面に出ている答えの候補(「他の候補」は押さない)
        if q["id"] == "q_segment":
            want = self.other.get("今のあなたに近いもの")
            return (want, True, {}) if want in buttons else (H.PASS, True, {})
        if q["phase"] in ("scenario_game", "reverse_pick"):
            return shown[_crc(self.p["id"], q["id"]) % len(shown)], True, {}
        t = q["targets"][0]
        self.answered.add(t)
        f = self.facts.get(t)
        if f is None:  # 事実表に無い(MID、無い職歴): 「そのほかのこと」にあれば打ち、無ければパス
            o = {"direction": "次にやりたいこと"}.get(t)
            if o and self.other.get(o) and self.other[o] != NOTHING:
                return self._type(t, self.other[o], tired)
            if shown and q["kind"] != "text":  # 文の問いの候補(無い職歴の職種など)は押さずにスキップ
                return shown[_crc(self.p["id"], q["id"]) % len(shown)], True, {}
            return H.PASS, True, {}
        if self.nothing_left and q["kind"] == "text" and not t.startswith("career"):
            self.nothing_left -= 1
            return NOTHING, False, {"intent": "pass"}
        if f["kind"] == "dont_know":
            return (H.DONT_KNOW, True, {}) if H.DONT_KNOW in buttons else (H.DONT_KNOW, False, {"intent": "dont_know"})
        if f["kind"] == "number":
            for b in shown:
                eff = q["choice_effects"].get(b, {}).get(t) or {}
                lo, hi = (eff.get("range") or (None, None))
                if lo is not None and J.same_unit(eff.get("unit"), f["unit"]) and f["value"] >= lo and (hi is None or f["value"] <= hi):
                    return b, True, {}
            text = f"{f['value']:g}{J.unit_head(f['unit'])}"
            return text, False, {"slots": [{"slot": t, "text": text, "number": f["value"], "unit": J.unit_head(f["unit"])}]}
        if f["value"] in buttons:
            return f["value"], True, {}
        return self._type(t, f["value"], tired)

    @staticmethod
    def _type(slot: str, text: str, tired: bool) -> tuple[str, bool, dict]:
        if tired and len(text) > SHORT_ANSWER:
            text = text[:SHORT_ANSWER]
        return text, False, {"slots": [{"slot": slot, "text": text}]}


def _ui_problems(out: dict, state) -> list[str]:
    bad = [f"吹き出し {H.chars(b)} 字" for b in out["bubbles"] if H.chars(b) > H.BUBBLE_MAX]
    ch = out["choices"]
    if len(ch) > H.QUICK_MAX or any(len(c) > H.LABEL_MAX for c in ch):
        bad.append(f"ボタン {ch}")
    if sum(H.chars(c) for c in ch) > H.LABELS_MAX:
        bad.append(f"ボタンのラベル {sum(H.chars(c) for c in ch)} 字")
    if out.get("card") is not None:
        n = H.chars(state.last_card["lead"]) + sum(H.chars(x) for x in out["card"])
        if n > H.CARD_MAX:
            bad.append(f"まとめ {n} 字")
    return bad


def _flex_problems(part: dict | None, built: dict | None) -> list[str]:
    """形 D の Flex の字数(§3.3、hearing-progress-ui.md §3)と、1 ターンに 0〜1 つ。"""
    if not part:
        return []
    if not built or not built.get("flex"):
        return [f"Flex を作れない: {part['kind']}"]
    flex = built["flex"]
    n = sum(len(t) for t in progress_ui.flex_texts(flex))
    bad = [] if 0 < len(flex.get("altText", "")) <= 400 else ["altText"]
    if part["kind"] == UI.CAREER_CARD and sum(H.chars(t) for t in career_ui.flex_lines(flex)) > UI.CAREER_CARD_MAX:
        bad.append(f"職歴カード {n} 字")
    if part["kind"] == UI.BAND and len((b := part["band"])["chip"] + b["value"] + (b["more"] or "")) > UI.BAND_MAX:
        bad.append("帯")
    if part["kind"] == UI.CARD and progress_ui.flex_chars(flex) > progress_ui.CARD_MAX:
        bad.append("プロフィールカード")
    return bad


def run_one(persona: dict, form: str, bank=None, llm=None, roundtrip=False, band=True) -> dict:
    """1 つの会話。roundtrip=True は、毎ターンの前に状態を JSON にして戻す(dip が毎回保存した状態から始めるのと同じ)。
    band=False は帯を切った形 D(dip_mcp.career.ui.BAND_ENV と同じ)。"""
    bank = bank or H.BANK
    st = H.new_state(form=form, prefill=prefill(persona))
    llm, actor, clock = llm or OracleLLM(), MockActor(persona), Clock()
    transcript, ui, presented, parts = [], [], Counter(), []
    crash = loop = False
    reask = 0
    mem = UI.fresh_mem(st, band)
    try:
        out = H.start(st, bank)
        part = UI.start_payload(st)
        UI.add_read(st, part)
        for step in range(MAX_STEPS + 1):
            if step == MAX_STEPS:
                loop = True
                break
            built = career_ui.build(part)
            flex = career_ui.flex_lines(built["flex"]) if built else []
            parts.append(part and part["kind"])
            transcript.append(f"AI({st.phase}/{(st.current or {}).get('qid')}): " + out["reply"].replace("\n", " / ")
                              + (f"  [ボタン] {' / '.join(out['choices'])}" if out["choices"] else "")
                              + (f"  [{part['kind']}] {' / '.join(flex)}" if part else ""))
            ui += _ui_problems(out, st) + _flex_problems(part, built)
            reply = AIReply(bubbles=out["bubbles"], buttons=out["choices"], flex=flex)
            clock.read(reply)
            again = actor.asked_again(st, bank)
            if again:
                reask += 1
                clock.irritations.append({"turn": step, "kind": "reask", "evidence": ",".join(again)})
            clock.irritate(step, reply, {})
            if out["done"]:
                break
            if why := clock.should_leave():
                clock.left = why
                break
            text, pressed, meaning = actor.answer(st, bank, out, clock.tired)
            clock.press() if pressed else clock.type(text)
            transcript.append(f"本人: {'[ボタン] ' if pressed else ''}{text}")
            llm.meaning = meaning
            if roundtrip:
                st = H.State.from_dict(json.loads(json.dumps(st.to_dict(), ensure_ascii=False)))
            prev = UI.before(st)
            out = H.turn(st, text, bank, llm)
            part, mem = UI.update(st, mem, prev, out)
            key = ((st.current or {}).get("qid"), (st.current or {}).get("mode"), st.turns)
            presented[key] += 1
            if presented[key] >= 5:
                loop = True
                break
    except Exception as e:  # noqa: BLE001
        crash = True
        transcript.append(f"CRASH: {type(e).__name__}: {e}")
    return {"persona": persona["id"], "form": form, "state": st, "clock": clock, "llm": llm, "transcript": transcript,
            "ui": ui, "crash": crash, "loop": loop, "reask": reask, "parts": parts,
            "complete": part["complete"] if part and part["kind"] == UI.COMPLETE else None}


def metrics(r: dict, persona: dict) -> dict:
    st, clock = r["state"], r["clock"]
    asks = [e for e in st.log if e["kind"] == "ask"]
    cap = H.captured(st)
    reached = st.result is not None
    completed = reached and not clock.left
    hk = hard_keys(persona)
    stop = H.TARGET_SECONDS[st.has_resume] - H.REVIEW_TIME_RESERVE
    seen = Counter(e["qid"] for e in asks if e["mode"] == "ask")
    budget = []
    for e in asks:
        only_hard = bool(e["hard"]) and set(e["hard"]) == set(e["targets"])
        if only_hard and e["turns"] >= H.HARD_LAST_TURN or not only_hard and (
                e["turns"] >= H.MAX_TURNS - H.REVIEW_RESERVE or e["seconds"] > stop):
            budget.append(e["qid"])
    dropped = []
    for u in r["llm"].returned:
        if cap.get(u["slot"], {}).get("status") == "missing" and u["slot"] not in st.pending and \
                not any(e.get("slot") == u["slot"] and e["kind"] in ("kept_high", "kept_existing", "pending") for e in st.log):
            dropped.append(u["slot"])
    facts = []
    for f in persona["facts"]:
        got = cap.get(f["key"])
        v = J.code_verdict(f, got)
        if v is None:
            fact = fact_text(f)
            v = bool(got["text"]) and (got["text"] in fact or fact in got["text"])
        facts.append(bool(v))
    pri = H.active_slots(st)
    made_up = draft_blanks = 0
    if reached:  # 下書き(§6.4 の関門「数字を作った」)。空欄に戻した数と、戻した後にまだ本人の言葉に無い数字
        md, replaced = D.build(st)
        made_up = replaced + check_numbers(md, D.sources(st))[1]
        n = blank_count(md)  # 数字を空欄に戻した欄も、結果の「空欄はNつ」に入る
        c = r["complete"]  # 形 D の完成のカルーセルの「空欄Nつ」も同じ数
        draft_blanks = int(n != (st.result or {}).get("blanks") or n != len(H.blank_cards(st)) + replaced
                           or (c is not None and c["blanks"] != n))
    m = {
        "crash": int(r["crash"]), "loop": int(r["loop"]), "not_reached": int(not reached and not r["crash"]),
        "left": int(bool(clock.left)),
        "hard_empty": [k for k in hk if cap[k]["status"] == "missing"] if reached else [],
        "low_hard": [k for k in hk if cap[k].get("confidence") == "low"] if reached else [],
        "no_review": int(reached and st.review_rounds < 1),
        "no_segment_q": int("q_segment" not in st.asked),
        "wasted": [e["qid"] for e in asks if not e["open"] and any(t in pri for t in e["targets"])],
        "repeated": [q for q, n in seen.items() if n > 1],
        "irrelevant": [e["qid"] for e in asks if "all" not in e["segments"] and e["segment"] not in e["segments"]],
        "dropped": dropped,
        "spam_hard_high": [k for k in hk if cap[k].get("confidence") == "high" and str(cap[k].get("from", "")).startswith(
            ("free_other", "choice:sc_", "choice:rp_"))],
        "phase_mismatch": int(sum(st.phase_turns.values()) != st.turns or any(
            n > H.PHASE_CAP.get(p, 99) for p, n in st.phase_turns.items())),
        "over_max_turns": int(st.turns > H.MAX_TURNS),
        "budget_violation": budget,
        "form_violation": [e["qid"] for e in asks if st.form == "B" and e["phase"] in ("scenario_game", "reverse_pick")],
        "ui_violation": r["ui"], "reask": r["reask"], "made_up_number": made_up, "draft_blanks": draft_blanks,
        # 記録だけ
        "completed": completed, "turns": st.turns, "seconds": round(st.seconds, 1), "actor_seconds": round(clock.seconds, 1),
        "stop_reason": st.stop_reason, "review_rounds": st.review_rounds, "typed_chars": clock.typed_chars,
        "button_ratio": round(clock.presses / max(1, clock.presses + clock.typed_replies), 2),
        "facts": f"{sum(facts)}/{len(facts)}", "blank_cards": len(H.blank_cards(st)),
        "phase_turns": dict(st.phase_turns), "llm_calls": dict(r["llm"].calls),
        "flex": dict(Counter(k for k in r["parts"] if k)),
    }
    return m


def failures(m: dict) -> list[str]:
    return [f"{k}={m[k]}" for k in GATES if m[k]]


def run_all(personas=None, forms=FORMS) -> list[dict]:
    rows = []
    for form in forms:
        for p in personas or load_personas():
            r = run_one(p, form)
            rows.append({"persona": p["id"], "form": form, "metrics": metrics(r, p), "transcript": r["transcript"],
                         "summary": H.summary(r["state"])})
    return rows


def table(rows: list[dict]) -> str:
    head = "| 形 | ペルソナ | 完了 | ターン | 推定時間(ハーネス/本人役) | 止まった理由 | 配分 | 打った字 | ボタン | 事実(参考) | 空欄 | 合否 |"
    out = [head, "|" + "---|" * 12]
    for r in rows:
        m = r["metrics"]
        pt = "/".join(str(m["phase_turns"].get(p, 0)) for p in ("icebreak", "hard_filter", "career_cards", "scenario_game",
                                                                 "reverse_pick", "fill_gaps", "review"))
        bad = failures(m)
        out.append(f"| {r['form']} | {r['persona']} | {'○' if m['completed'] else '×'} | {m['turns']} | "
                   f"{m['seconds']:.0f}/{m['actor_seconds']:.0f} 秒 | {m['stop_reason']} | {pt} | {m['typed_chars']} | "
                   f"{m['button_ratio']:.0%} | {m['facts']} | {m['blank_cards']} | {'合格' if not bad else ', '.join(bad)} |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log", help="結果の JSON と会話ログを書くディレクトリ")
    args = ap.parse_args(argv)
    rows = run_all()
    print("配分は icebreak/hard_filter/career_cards/scenario_game/reverse_pick/fill_gaps/review のターン")
    print(table(rows))
    bad = [(r["form"], r["persona"], failures(r["metrics"])) for r in rows if failures(r["metrics"])]
    if args.log:
        d = Path(args.log)
        d.mkdir(parents=True, exist_ok=True)
        (d / "results.json").write_text(json.dumps([{k: v for k, v in r.items() if k != "transcript"} for r in rows],
                                                   ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        for r in rows:
            (d / f"{r['form']}_{r['persona']}.txt").write_text("\n".join(r["transcript"]) + "\n", encoding="utf-8")
    for form, pid, b in bad:
        print(f"不合格: {form} {pid}: {', '.join(b)}")
    print("すべて合格" if not bad else f"不合格 {len(bad)} 件")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
