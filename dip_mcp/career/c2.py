"""正社員ヒアリングの c2(CAREER_HARNESS_VERSION=c2)。会話の層(dip_mcp/convo/)に載せ替えていく版。

設計は docs/design/conversation-layer.md(§5 の 2 枚目まで)と ADR-0011。c1(dip_mcp/career/harness.py)は変えずに残し、
この版は c1 の決める段(問いの選び方、予算、まとめ、結果)と言う段をそのまま使う。c1 と違うのは次のところ:

- 守りの前処理(§4.11): ボタンでない文は、読む段の前に危ない言葉の表(dip_mcp.convo.guard)と照らす。当たれば、モデルを呼ばず、
  数えずに決まった文(相談窓口。前後の文は入り口の定義 entry.json の丁寧語)を返す。次の発言が「続ける」なら同じ問いから続ける。
  表に無い言い方は、読む段の crisis でも同じ文を返す
- 読む段(§4.3): 1 つの intent でなく、act の並びで読む(llm.read。プロンプトは dip_mcp/prompts/convo/read_ja.txt に
  正社員の決まり prompt_read_ja.txt をつなぐ)。直前のやりとり(状態の convo。dip_mcp.convo.state)も渡す。
  出力は dip_mcp.convo.acts.validate_reading で確かめる(値は今の発言の部分文字列、数は発言に数字で書いてあるものだけ)
- 値の当て方(§4.4。dip_mcp.convo.values): 今の問いの欄には、本人の文の全部でなく、その欄の部分を入れる。ほかの欄の値は、
  雑談や逆質問と一緒でも捨てない(空なら推測(low)、文の欄に値があれば言い足しとしてつなぐ)。直した値は high で置きかえる。
  取り消した欄は空にする。迷っている値は low で入れて進む。今の問いに答えずにほかの欄の値だけを言ったときは、同じ問いを出し直す
- 個人情報(§4.11): 伏せた印と、読む段が示した氏名・要配慮の部分は、値から除き、返事に出たらやり直す
- ボタンから推せるほかの欄(§4.7 の 1): 入り口の定義の infer。推測(low)で入れ、聞かずにまとめで確かめる
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dip_mcp.career import harness as H
from dip_mcp.convo import acts as A
from dip_mcp.convo import entry as E
from dip_mcp.convo import guard
from dip_mcp.convo import state as CS
from dip_mcp.convo import values as V
from dip_mcp.convo.turn import call_llm
from dip_mcp.convo.turn import render_prompt as _render

VERSION = "c2"
ENTRIES = ("career", "baito")  # 入り口の名前(移る先の確かめ)
ENTRY = E.load(H.HERE / "entry.json", H.ALL_SLOTS, ENTRIES, ("crisis_lead", "crisis_tail", "crisis_resume"))
CRISIS_TEXT = guard.crisis_text(ENTRY.texts["crisis_lead"], ENTRY.texts["crisis_tail"])
CRISIS_FLAG = "crisis_shown"  # 前のターンで窓口を案内した印(1 ターンだけ残す)
READ_PROMPTS = [H.PROMPT_DIR.parent / "convo" / "read_ja.txt", H.PROMPT_DIR / "prompt_read_ja.txt"]


@dataclass
class State(H.State):
    version: str = VERSION
    convo: dict = field(default_factory=CS.new)  # 層の覚え(直前のやりとりと返信。dip_mcp.convo.state)


def new_state(form=None, prefill=None):
    """空の状態(c1 の new_state と同じ。版と層の覚えだけが違う)。"""
    return State.from_dict(H.new_state(form, prefill).to_dict() | {"version": VERSION})


def render_prompt(stage, variables):
    """読む段(read)は層の骨組みに正社員の決まりをつないだもの。言う段は c1 のまま。"""
    if stage == "read":
        return _render(READ_PROMPTS, variables)
    return H.render_prompt(stage, variables)


def match_button(state, text):
    return H.match_button(state, text)


def _log(state):
    return lambda kind, **kw: H.ev(state, kind, **kw)


# ----------------------------- ターン -----------------------------
def start(state, bank=H.BANK):
    """最初の問い(c1 と同じ)。初めの 1 回だけ、出した文を直前のやりとりに覚える。"""
    first = state.current is None
    out = H.start(state, bank)
    if first:
        CS.replied(state.convo, out["reply"])
    return out


def turn(state, user_text, bank=H.BANK, llm=None):
    """本人の発言 1 通を処理する(返す形は c1 の turn と同じ)。ハーネスの中の例外は、ターンの始めの状態に戻して今のものを出し直す。
    終わりに、発言(氏名と要配慮の部分は伏せる)と返信を直前のやりとりに覚える。"""
    H._init_volatile(state)
    state._spans = []
    snap = state.to_dict()
    try:
        out = _turn(state, user_text, bank, llm)
    except Exception as e:  # noqa: BLE001
        restored = State.from_dict(snap)
        state.__dict__.clear()
        state.__dict__.update(restored.__dict__)
        H._init_volatile(state, reset=True)
        state._spans = []
        H.ev(state, "internal_error", error=type(e).__name__)
        return H._represent(state, bank, counted=False)  # 氏名の部分が分からないので、発言を覚えない
    if isinstance(user_text, str) and H.nfkc(user_text):
        CS.heard(state.convo, guard.mask(user_text, state._spans))
        CS.replied(state.convo, out["reply"])
    return out


def read_payload(state, bank, user_text):
    """読む段の入力。c1 の抽出の入力に、欄の説明、移れる入り口、直前のやりとりを足したもの。"""
    p = H.extract_payload(state, bank, user_text)
    labels = {s: H.SLOTS[s]["label"] for s in H.ALL_SLOTS}
    return {"entry": ENTRY.title, "phase": p["phase"], "segment": state.segment, "mode": p["mode"], "question": p["question"],
            "choices": p["choices"], "targets": {t: labels.get(t, t) for t in p["targets"]},
            "slots": {s: f"{labels[s]}。{ENTRY.slots[s]}" for s in H.active_slots(state)},
            "segments": {s: H.SEGMENT_JA[s] for s in H.SEGMENTS},
            "switch": [{"to": s["to"], "label": s["label"]} for s in ENTRY.switch],
            "recent": CS.recent_lines(state.convo), "user_text": user_text}


def validator(user_text):
    def check(raw):
        return A.validate_reading(raw, user_text, H.ALL_SLOTS, [s["to"] for s in ENTRY.switch], None, H.SEGMENTS)
    return check


def _take_crisis(state):
    shown = CRISIS_FLAG in state.flags
    if shown:
        state.flags.remove(CRISIS_FLAG)
    return shown


def _crisis(state):
    """危ない言葉: 問いを進めず(数えず、モデルを呼ばない)、決まった文だけを返す。今の問いはそのまま残す。"""
    H._flag(state, CRISIS_FLAG)
    H.ev(state, "crisis_guidance")
    return H._out(state, [CRISIS_TEXT], [], counted=False) | {"crisis": True}  # 形 D の部品を付けない(session)


def _turn(state, user_text, bank, llm):
    text = user_text if isinstance(user_text, str) else ""
    state._user_text = text
    state._progress = []
    if state.current is None:
        H.advance(state, bank)
    if state.closed or state.result:
        return H._stored(state)
    if not H.nfkc(text):
        H.ev(state, "empty_input")
        return H._represent(state, bank, counted=False)
    state.said.append(text)
    cur = state.current
    q = H.qmap(state, bank).get(cur["qid"])
    button = match_button(state, text)
    button = H.LEGACY_BUTTONS.get(button, button)
    fix = H.fix_targets(state).get(H.nfkc(text))
    state.seconds += H.BUTTON_SECONDS if button or fix else H.type_seconds(text)
    crisis_open = _take_crisis(state)
    if button is None and fix is None:  # 守りの前処理。押したボタンの文は答えとして読む
        if guard.crisis_hit(text):
            return _crisis(state)
        if crisis_open and guard.wants_resume(text):
            H.ev(state, "crisis_resume")
            return H._represent(state, bank, counted=False, lead=ENTRY.texts["crisis_resume"] + "\n")
    if button == H.LATER or (button is None and fix is None and H.nfkc(text) in H.END_WORDS):
        return H._close(state, "later")
    if cur["mode"] == "start":  # 形 D の「始める」: 最初の問いを出す。打った文は、最初の問いへの答えとして読む
        H.advance(state, bank)
        if button is not None:
            H.ev(state, "started")
            return H._represent(state, bank, counted=False)
        cur = state.current
        q = H.qmap(state, bank).get(cur["qid"])
        button = match_button(state, text)
    if button == H.MORE and q is not None:
        return H._more(state, q)
    if fix is not None:  # 形 D のまとめの「直す」: その欄の問いを出す(数えない。c1 と同じ)
        fq = H.fix_question(state, bank, fix)
        if fq is not None and H.can_fix(state):
            state.current = H.ask_current(fq, "fix", slot=fix)
            H.ev(state, "fix", slot=fix, qid=fq["id"])
            return H._represent(state, bank, counted=False, lead=H.FIX_LEAD)
        H.ev(state, "fix_refused", slot=fix)
        H._back_to_review(state)
        return H._represent(state, bank, counted=False, lead=H.FIX_LIMIT_LEAD)
    reading = None
    if button is None:
        reading = call_llm(llm.read, read_payload(state, bank, text), validator(text), _log(state), "read")
        state._spans = A.spans(reading)
        state._user_text = guard.mask(text, state._spans)  # 言う段に、氏名と要配慮の部分を渡さない
    if A.has(reading, "crisis"):  # 表に無い危ない言い方(§6 の 15)。読む段の 1 回のほかは呼ばない
        return _crisis(state)
    if A.has(reading, "later", "stop"):
        if q is not None and cur["mode"] != "review":
            _apply(state, q, reading, text, retry=False)  # 「あとで」と一緒に打った答えも残す(次は聞かない)
        return H._close(state, "later")
    if talk_only(reading):  # 雑談、逆質問、感情など、値も操作も無い発言: 状態を変えずに今のものを出し直す
        return _aside(state, bank, llm)
    if q is None and cur["mode"] != "review":
        return H._represent(state, bank, counted=False)
    if A.has(reading, "more") and not _values(reading, q) and cur["mode"] not in ("review", "start"):  # 「ほかには?」と打った
        return H._more(state, q)
    if cur["mode"] == "fix":
        H._count(state, "review")
        if button == H.FIX_KEEP:
            H.ev(state, "fix_kept", slot=cur["slot"], by="keep")
        else:
            _apply_fix(state, q, cur["slot"], button, reading, text)
        H._back_to_review(state)
        return _say(state, bank, llm)
    if cur["mode"] == "review":
        return _review(state, bank, llm, button, reading, text)
    return _ask_turn(state, bank, llm, q, button, reading, text)


TALK_ONLY = {*A.TALK, "emotion", "switch", "go_back", "restart", "danger", *A.SPAN_ACTS}  # 値でも操作でもない act


def talk_only(reading):
    """値も操作も無い発言か(act が 1 つ以上あり、どれも話・感情・入り口・守りの部分)。go_back と switch の行き先は 3 枚目と 6 枚目。"""
    names = [a["act"] for a in (reading or {}).get("acts", [])]
    return bool(names) and all(n in TALK_ONLY for n in names)


def _ask_turn(state, bank, llm, q, button, reading, text):
    """問いへの発言。答えたら数えて次へ、ほかの欄だけなら同じ問いを出し直す。"""
    cur = state.current
    ops = V.plan(reading, q["targets"])
    if button is None and not _values(reading, q) and A.has(reading, "clarify"):
        if state.clarified.count(cur["qid"]) < H.MAX_CLARIFY:
            H._clarify(state, q)
            return _say(state, bank, llm, counted=False)
        H.ev(state, "clarify_unanswered", qid=q["id"])
        return _next(state, bank, llm, q, skipped=H.CLARIFY_SKIP)
    before = {k: dict(v) for k, v in state.slots.items()}
    if button is not None:
        if button in q.get("escape", []):
            H._apply_escape(state, q, button)
        else:
            H._apply_choice(state, q, button)
            _infer(state, q, button)
        return _next(state, bank, llm, q, before)
    got = _apply(state, q, reading, text)
    if got["answered"]:
        return _next(state, bank, llm, q, before)
    if any(o["op"] == "escape" for o in ops):
        H._apply_escape(state, q, H.DONT_KNOW if A.has(reading, "dont_know") and H.DONT_KNOW in q.get("escape", [])
                        else H.PASS)
        return _next(state, bank, llm, q, before)
    if A.has(reading, "show_result"):
        H._flag(state, "show_result")
        return _next(state, bank, llm, q, before)
    if got["retry"]:  # 数の問いで数が読めない 1 回目: 数えずに、ボタンで聞き直す(c1 と同じ)
        return _retry(state, bank, llm, q)
    if got["fixed"] and not got["added"]:  # 直した・取り消した: 数えずに、今の問いを出し直す(§4.6)
        return _say(state, bank, llm, counted=False, aside=True)
    if got["added"] or (reading and reading["acts"]):  # ほかの欄の値だけ(雑談や逆質問と一緒でも): 同じ問いを出し直す
        return _aside(state, bank, llm)
    if q["id"] not in state.retried:  # 何も読めない 1 回目: 数えずに、ボタンで聞き直す(c1 と同じ)
        return _retry(state, bank, llm, q)
    kept = guard.scrub(text, state._spans)  # 2 回目: 本人の文を low で入れ、まとめで確かめる(伏せた印と氏名は除く)
    if kept:
        H._give_up(state, q, kept)
    return _next(state, bank, llm, q, before)


def _retry(state, bank, llm, q):
    state.retried += [q["id"], f"num:{q['id']}"]
    state.current = dict(state.current, mode="retry")
    return _say(state, bank, llm, counted=False)


def _values(reading, q):
    """値を当てる操作(パスと「わからない」のほか)があるか。"""
    return any(o["op"] != "escape" for o in V.plan(reading, q["targets"] if q else []))


def _next(state, bank, llm, q, before=None, skipped=None):
    """数えて、次の問いへ進む(c1 の _turn の後半と同じ)。"""
    H._count(state, q["phase"])
    phase_before = state.phase
    H.advance(state, bank)
    if state.form != "B":
        H._progress(state, before if before is not None else dict(state.slots), phase_before)
    if skipped:
        state._progress = [skipped, *state._progress][-2:]
    return _say(state, bank, llm)


def _review(state, bank, llm, button, reading, text):
    """まとめへの発言(c1 の apply_review。直した欄は、act の欄の部分だけを high で入れる)。"""
    H._count(state)
    if button is None and reading is None:
        key = f"review{state.review_rounds + 1}"
        if key not in state.retried:
            state.retried.append(key)
            H._uncount(state)
            return H._represent(state, bank, counted=False, lead=H.RETRY_TEXT)
        H.apply_review(state, None, button_ok=False, user_text=text, promote=False)
    else:
        ext = None
        if reading is not None:
            planned = V.plan(reading, [])
            ops = [o for o in planned if o["op"] in ("add", "correct", "unsure") and o["slot"] != "segment"]
            seg = next((o["act"].get("value") for o in planned if o["slot"] == "segment"), None)
            ack = "all" if A.has(reading, "confirm") else "partial" if ops else "none"
            ext = {"slot_updates": [{"slot": o["slot"], "text": o["text"], "number": o["act"].get("number"),
                                     "unit": o["act"].get("unit"), "choice": None} for o in ops],
                   "ng_updates": _ng(reading, state), "segment": seg, "review_ack": ack}
        H.apply_review(state, ext, button_ok=button == H.REVIEW_OK, user_text=guard.scrub(text, state._spans))
    if A.has(reading, "show_result"):
        state._need_more_review = False
    H.advance(state, bank)
    return _say(state, bank, llm)


# ----------------------------- 値を当てる -----------------------------
def _ng(reading, state):
    ng = (reading or {}).get("extra", {}).get("ng") or []
    ng = [ng] if isinstance(ng, str) else ng if isinstance(ng, list) else []
    return [x for x in (guard.scrub(n, state._spans) for n in ng if isinstance(n, str)) if x]


def _entry(state, q, o, conf):
    a = o["act"]
    return H.entry("filled", o["text"], number=a.get("number"), unit=a.get("unit"), confidence=conf,
                   frm=f"{a['act']}:{q['id']}")


def _choice_ok(q, a, said):
    """今の問いの答えが、ボタンと同じ意味か(c1 の _apply_text と同じ決まり)。数の区切りは、ラベルの数が発言に数字で書いてあるときだけ。"""
    number_kind = H.SLOTS.get(a["slot"], {}).get("kind") == "number"
    choice = a.get("choice")
    return choice in q["choices"] and not (number_kind and "number" in a) \
        and (not number_kind or H.digits_in(choice) <= H.digits_in(said))


def _apply(state, q, reading, said, retry=True):
    """読んだ値を当てる(§4.4)。-> {"answered": 今の問いの欄が埋まった, "added": ほかの欄に入れた,
    "fixed": 直した・取り消した, "retry": 数の問いで数が読めず、聞き直すことにした}。"""
    got = {"answered": False, "added": False, "fixed": False, "retry": False}
    if reading is None:
        return got
    if q["targets"] == ["__ng__"]:
        n = len(state.user_ng)
        H._apply_ng(state, {"ng_updates": _ng(reading, state)}, state._user_text)
        got["answered"] = len(state.user_ng) > n
    else:
        H._apply_ng(state, {"ng_updates": _ng(reading, state)}, state._user_text)
    direct = q["phase"] in H.DIRECT
    pri = H.active_slots(state)
    for o in V.plan(reading, q["targets"]):
        slot, a, op = o["slot"], o["act"], o["op"]
        if op == "escape":
            continue
        if slot == "segment":
            H.set_segment(state, a.get("value"), "choice" if q["id"] == "q_segment" else "free")
            got["answered"] = got["answered"] or (q["id"] == "q_segment" and state.segment != "unknown")
            continue
        if op == "retract":
            gone = state.slots.pop(slot, None) or state.pending.pop(slot, None)
            H.ev(state, "retracted", slot=slot, had=gone is not None)
            got["fixed"] = True
            continue
        old = state.slots.get(slot) if slot in pri else state.pending.get(slot)
        if op == "set":
            if _choice_ok(q, a, said):
                H._apply_choice(state, q, a["choice"], text=o["text"], src="free")
                _infer(state, q, a["choice"])
                got["answered"] = True
                continue
            if H.SLOTS[slot]["kind"] == "number" and "number" not in a and retry and f"num:{q['id']}" not in state.retried:
                got["retry"] = True  # 数の問いで数が読めない: ボタンで聞き直す(1 回だけ。c1 と同じ)
                continue
            conf = V.confidence(op, direct)
            if H.SLOTS[slot]["kind"] == "number" and "number" not in a:
                conf = "low"  # 聞き直しても数が読めなかった: 本人の言葉を low で入れ、まとめで確かめる
            H.set_slot(state, slot, _entry(state, q, o, conf))
            got["answered"] = True
        elif op == "add":
            got["added"] = True
            if old is None:
                H.set_slot(state, slot, _entry(state, q, o, V.confidence(op, direct)))
            elif old["status"] != "filled" or H.SLOTS[slot]["kind"] != "number":
                e = _entry(state, q, o, "low")
                if old["status"] == "filled":
                    e = dict(old, text=V.merge(old.get("text"), o["text"]), confidence="low", frm=e["from"])
                _put(state, slot, e, pri)
                H.ev(state, "added", slot=slot)
            else:
                H.ev(state, "kept_existing", slot=slot)
        elif op == "correct":
            H.set_slot(state, slot, _entry(state, q, o, V.confidence(op, direct)))
            H.ev(state, "corrected", slot=slot)
            got["fixed"] = True
            got["answered"] = got["answered"] or o["current"]
        elif op == "unsure":
            _put(state, slot, _entry(state, q, o, V.confidence(op, direct)), pri)
            got["answered"] = got["answered"] or o["current"]
            got["added"] = got["added"] or not o["current"]
    return got


def _put(state, slot, e, pri):
    """確かさによらず置きかえる(言い足しと迷い。前の high を low にしてよい: 本人が変えたので、まとめで確かめ直す)。"""
    if slot in pri:
        state.slots[slot] = e
    else:
        state.pending[slot] = e
    H.ev(state, "set", slot=slot, status=e["status"], confidence=e["confidence"], frm=e["from"])


def _infer(state, q, label):
    """ボタンから推せるほかの欄(入り口の定義の infer)を、空のときだけ推測(low)で入れる。"""
    for slot, text in (ENTRY.infer.get(q["id"]) or {}).get(label, {}).items():
        if H.is_open(state.slots.get(slot)) and slot not in state.pending:
            H.set_slot(state, slot, H.entry("filled", text, confidence="low", frm=f"infer:{q['id']}"))


def _apply_fix(state, q, slot, button, reading, text):
    """形 D のまとめで選んだ欄への答え(c1 の _apply_fix と同じ決まり。文の答えは、act の欄の部分を入れる)。"""
    old = state.slots.pop(slot, None)
    escape = None
    if button is not None and button in q.get("escape", []):
        escape = button
    elif button is None and A.has(reading, "pass", "dont_know"):
        escape = H.DONT_KNOW if A.has(reading, "dont_know") and H.DONT_KNOW in q.get("escape", []) else H.PASS
    if escape == H.DONT_KNOW:
        state.slots[slot] = H.entry("dont_know", H.DONT_KNOW, frm=f"fix:{q['id']}")
    elif escape is not None:
        state.slots[slot] = H.entry("passed", H.PASS, H.NOPREF if slot in H.CONDITION_SLOTS else None, frm=f"fix:{q['id']}")
    elif button is not None:
        H._apply_choice(state, q, button)
    elif reading is not None:
        _apply(state, q, reading, text, retry=False)
    e = state.slots.get(slot)
    if e is None:
        if old is not None:
            state.slots[slot] = old
        H.ev(state, "fix_kept", slot=slot)
        return
    e["confidence"], e["from"] = "high", f"fix:{q['id']}"
    H.ev(state, "fixed", slot=slot, status=e["status"])


# ----------------------------- 言う -----------------------------
def _aside(state, bank, llm):
    """答えでない発言: 今のものを出し直す(c1 の _aside と同じ数え方)。"""
    state.aside_count += 1
    counted = state.aside_count > H.MAX_ASIDES and state.turns < H.MAX_TURNS - H.REVIEW_RESERVE \
        and state.current["mode"] not in ("review", "result", "fix")
    if counted:
        H._count(state, "aside")
    if state.current["mode"] in ("review", "result"):
        return H._represent(state, bank, counted=counted, lead=H.ASIDE_FALLBACK)
    return _say(state, bank, llm, counted=counted, aside=True)


def _say(state, bank, llm, counted=True, aside=False):
    """言う段(c1 の _say と同じ)。返事に伏せた印・氏名・要配慮の部分が出たら、やり直す(だめなら問いの文だけ)。"""
    cur = state.current
    if cur["mode"] == "review":
        card = state.last_card
        return H._out(state, H.review_bubbles(state, card), cur["choices"], counted=counted, card=card["lines"])
    if cur["mode"] == "result":
        return H._finish(state, counted)
    v = H.view(state, bank, aside=aside)
    v["model_text"] = state._user_text  # 言う段のモデルに本人の発言として渡す文(氏名と要配慮の部分を伏せたもの。runtime)

    def _vr(raw):
        r, err = H.validate_reply(raw, v)
        if r is not None and guard.leaks(r, state._spans):
            return None, ["personal_echo"]
        return r, ([err] if err else [])
    reply = call_llm(llm.generate, v, _vr, _log(state), "generate") if llm is not None else None
    if reply is None:
        reply = v["text"]
    return H._out(state, [H.bubble(v, reply)], v["choices"], counted=counted)
