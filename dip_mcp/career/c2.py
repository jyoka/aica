"""正社員ヒアリングの c2(CAREER_HARNESS_VERSION=c2)。会話の層(dip_mcp/convo/)に載せ替えていく版。

設計は docs/design/conversation-layer.md(§5 の 2 枚目と 4 枚目まで)と ADR-0011。c1(dip_mcp/career/harness.py)は変えずに残し、
この版は c1 の決める段(問いの選び方、予算、まとめ、結果)をそのまま使う。c1 と違うのは次のところ:

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
- 言う段(§4.8。dip_mcp.convo.reply): 返事を部品(相づち → 答え → 注意 → 区切り → 問い)の並びに組む。プロンプトは層の骨組み
  dip_mcp/prompts/convo/say_ja.txt に、入り口の定義の調子(tone)を差し込む。モデルが書くのは相づちと逆質問の答えだけで、問いの文は
  質問バンクの言い方(text と variants)から選ぶ。同じ問いを出し直すときは、前に使った言い方を避ける。返事がだめなら 1 回やり直し、
  それでもだめなら決まった組み立て(入り口の定義の相づちを順に回す)。相づちが要らないターン(聞き直し、聞き返しの説明)はモデルを呼ばない。
  「読み取れなかった」とは言わない(聞き直しの注意は入り口の定義の texts)。「他の候補」の前置きも texts から作る
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from dip_mcp.career import harness as H
from dip_mcp.convo import acts as A
from dip_mcp.convo import entry as E
from dip_mcp.convo import guard
from dip_mcp.convo import reply as RP
from dip_mcp.convo import state as CS
from dip_mcp.convo import values as V
from dip_mcp.convo.turn import call_llm
from dip_mcp.convo.turn import render_prompt as _render

VERSION = "c2"
ENTRIES = ("career", "baito")  # 入り口の名前(移る先の確かめ)
TEXTS = ("crisis_lead", "crisis_tail", "crisis_resume", "retry", "retry_number", "retry_review", "no_answer", "more_lead",
         "more_lead_number", "more_done")
# 言う段のプロンプトに渡す、相づちの種類の説明(dip_mcp.convo.reply.ACK_KINDS)
ACK_JA = {"answer": "本人の答えを受け取る、短い相づち", "correct": "直した・取り消したことを、何を直したか分かるように短く受け取る",
          "chitchat": "雑談に軽く 1 文で乗る", "emotion": "気持ちを受け止める(励まし、約束、助言をしない)",
          "complaint": "聞き方への不満に、短く詫びる(かしこまった謝罪の決まり文句にしない)", None: "付けない"}
ENTRY = E.load(H.HERE / "entry.json", H.ALL_SLOTS, ENTRIES, TEXTS)
CRISIS_TEXT = guard.crisis_text(ENTRY.texts["crisis_lead"], ENTRY.texts["crisis_tail"])
CRISIS_FLAG = "crisis_shown"  # 前のターンで窓口を案内した印(1 ターンだけ残す)
READ_PROMPTS = [H.PROMPT_DIR.parent / "convo" / "read_ja.txt", H.PROMPT_DIR / "prompt_read_ja.txt"]
SAY_PROMPTS = [H.PROMPT_DIR.parent / "convo" / "say_ja.txt"]


@dataclass
class State(H.State):
    version: str = VERSION
    convo: dict = field(default_factory=CS.new)  # 層の覚え(直前のやりとりと返信。dip_mcp.convo.state)


def new_state(form=None, prefill=None):
    """空の状態(c1 の new_state と同じ。版と層の覚えだけが違う)。"""
    return State.from_dict(H.new_state(form, prefill).to_dict() | {"version": VERSION})


def render_prompt(stage, variables):
    """読む段(read)は層の骨組みに正社員の決まりをつないだもの。言う段(say)は層の骨組みに、入り口の調子を差し込んだもの。"""
    if stage == "read":
        return _render(READ_PROMPTS, variables)
    if stage == "say":
        return _render(SAY_PROMPTS, variables)
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
    _init_turn(state)
    snap = state.to_dict()
    try:
        out = _turn(state, user_text, bank, llm)
    except Exception as e:  # noqa: BLE001
        restored = State.from_dict(snap)
        state.__dict__.clear()
        state.__dict__.update(restored.__dict__)
        H._init_volatile(state, reset=True)
        _init_turn(state)
        H.ev(state, "internal_error", error=type(e).__name__)
        return H._represent(state, bank, counted=False)  # 氏名の部分が分からないので、発言を覚えない。c1 の決まった出し直し
    if isinstance(user_text, str) and H.nfkc(user_text):
        CS.heard(state.convo, guard.mask(user_text, state._spans))
        CS.replied(state.convo, out["reply"], state._ack)
    return out


def _init_turn(state):
    """そのターン限りの層の値: 氏名と要配慮の部分、読んだもの、押したボタン、返事の相づち、聞き直したか、聞き返しに説明したか、
    ターンの始めの問い。"""
    state._spans, state._reading, state._button, state._ack = [], None, None, None
    state._retry = state._clarify = False
    state._qid_before = (state.current or {}).get("qid")


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
        return _represent(state, bank, counted=False)
    state.said.append(text)
    cur = state.current
    q = H.qmap(state, bank).get(cur["qid"])
    button = match_button(state, text)
    button = H.LEGACY_BUTTONS.get(button, button)
    state._button = button
    fix = H.fix_targets(state).get(H.nfkc(text))
    state.seconds += H.BUTTON_SECONDS if button or fix else H.type_seconds(text)
    crisis_open = _take_crisis(state)
    if button is None and fix is None:  # 守りの前処理。押したボタンの文は答えとして読む
        if guard.crisis_hit(text):
            return _crisis(state)
        if crisis_open and guard.wants_resume(text):
            H.ev(state, "crisis_resume")
            return _represent(state, bank, counted=False, lead=ENTRY.texts["crisis_resume"])
    if button == H.LATER or (button is None and fix is None and H.nfkc(text) in H.END_WORDS):
        return H._close(state, "later")
    if cur["mode"] == "start":  # 形 D の「始める」: 最初の問いを出す。打った文は、最初の問いへの答えとして読む
        H.advance(state, bank)
        if button is not None:
            H.ev(state, "started")
            return _represent(state, bank, counted=False)
        cur = state.current
        q = H.qmap(state, bank).get(cur["qid"])
        button = state._button = match_button(state, text)
    if button == H.MORE and q is not None:
        return _more(state, q)
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
        reading = state._reading = call_llm(llm.read, read_payload(state, bank, text), validator(text), _log(state), "read")
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
        return _represent(state, bank, counted=False)
    if A.has(reading, "more") and not _values(reading, q) and cur["mode"] not in ("review", "start"):  # 「ほかには?」と打った
        return _more(state, q)
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
            state._clarify = True
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
    state._retry = True
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
            return H._represent(state, bank, counted=False, lead=ENTRY.texts["retry_review"])
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


def _say(state, bank, llm, counted=True, aside=False, quiet=False, lead=None):
    """言う段(§4.8)。まとめと結果は c1 の決まった文。問いは、計画(view)の部品を 1 つの吹き出しに組む。モデルに書かせるもの
    (相づちか逆質問の答え)があればモデルを呼び、返事を確かめる(dip_mcp.convo.reply.validate。氏名などをなぞったら、やり直す)。
    だめなら 1 回やり直し、それでもだめなら決まった組み立て。返す形は c1 と同じで、部品の並び(parts)が付く。"""
    cur = state.current
    if cur["mode"] == "review":
        card = state.last_card
        return H._out(state, H.review_bubbles(state, card), cur["choices"], counted=counted, card=card["lines"])
    if cur["mode"] == "result":
        return H._finish(state, counted)
    if cur["mode"] != "clarify" and H.qmap(state, bank).get(cur.get("qid")) is None:  # 今の問いが無い(区分が変わったなど)
        return H._represent(state, bank, counted=counted, lead=lead)
    v, plan = view(state, bank, aside=aside, quiet=quiet, lead=lead)

    def _check(raw):
        parts, why = RP.validate(raw, plan, state.convo)
        return parts, ([why] if why else [])
    parts = None
    if llm is not None and RP.needs_model(plan):
        parts = call_llm(llm.generate, v, _check, _log(state), "generate")
    if parts is None:
        parts = RP.fallback(plan, state.convo, ENTRY.tone["acks"])
    if cur["mode"] != "clarify":
        cur["said"] = RP.ask_of(parts)  # 次に同じ問いを出し直すときは、この言い方を避ける
    state._ack = RP.ack_of(parts)
    return H._out(state, [RP.render(parts)], v["choices"], counted=counted) | {"parts": parts}


ASK_MODES = ("ask", "retry", "clarify")  # 層の組み立てで出す問いの mode(ほかの start、review、fix、result、closed は c1 のまま)


def _represent(state, bank, counted, lead=None):
    """今のものを、モデルを呼ばずに出し直す(c1 の _represent)。問いは層の組み立てで出す(前と違う言い方。c1 の聞き直しの文を付けない)。"""
    if (state.current or {}).get("mode") not in ASK_MODES:
        return H._represent(state, bank, counted=counted, lead=lead)
    return _say(state, bank, None, counted=counted, quiet=True, lead=lead)


def phrasings(q, bank):
    """問いの言い方: 1 つ目は問いの文、続けて質問バンクの variants。区分ごとの文(text_by_segment)や、履歴書から入った仕事の言葉を
    引く文(text_known)に変わった問いは、その文だけ(variants は既定の文の言い換えなので)。"""
    n = q.get("card")
    raw = next((b for b in bank if b["id"] == (q["id"][:-len(str(n))] if n else q["id"])), q)
    which = "直近の" if n == 1 else "その前の"
    if q["text"] != raw["text"].replace("{which}", which):
        return [q["text"]]
    return [q["text"], *(x.replace("{which}", which) for x in raw.get("variants") or [])]


def ack_kind(reading, button, aside):
    """相づちの種類(dip_mcp.convo.reply.ACK_KINDS)。逆質問だけのターンは相づちを付けず、答えを書かせる。"""
    if A.has(reading, "complaint"):
        return "complaint"
    if A.has(reading, "emotion"):
        return "emotion"
    if not aside or button is not None:
        return "answer"
    if A.has(reading, "chitchat", "story"):
        return "chitchat"
    if A.has(reading, "ask_service", "ask_advice"):
        return None
    if A.has(reading, "correct", "retract") and not A.has(reading, "add", "unsure"):
        return "correct"
    return "answer"


def facts():
    """逆質問に答えてよい事実(§4.6 の facts.json。3 枚目のカードで足す。無ければ「お答えできません」と言う)。"""
    return []


def view(state, bank, aside=False, quiet=False, lead=None):
    """言う段の入力(c1 の view に、計画とプロンプトの変数を足したもの)-> (view, 計画)。model_text は、言う段のモデルに本人の発言として
    渡す文、model_history は直前のやりとり(どちらも氏名と要配慮の部分を伏せたもの。ランタイムが、会話の記録の代わりに使う)。
    quiet はモデルを呼ばない出し直し(相づちも答えも付けず、lead を注意の場所に置く)。"""
    v = H.view(state, bank, aside=aside)
    cur = state.current
    q = H.qmap(state, bank).get(cur.get("qid"))
    mode = cur.get("mode")
    reading, button = getattr(state, "_reading", None), getattr(state, "_button", None)
    notes, lines = [], []
    for x in getattr(state, "_progress", None) or []:  # 飛ばした問いは注意、フェーズの区切りと職歴の「✓」は区切りの場所(§4.12)
        (lines if x in H.PHASE_LINES.values() or x.startswith("✓") else notes).append(x)
    kind = ack_kind(reading, button, aside or mode == "aside")
    answer = None
    if mode == "clarify":  # 聞き返しへの説明(質問バンクの explain)。説明したターンは相づちを付けない
        ask = {"qid": cur["qid"], "mode": mode, "text": cur["text"], "phrasings": [cur["text"]]}
        kind = None if getattr(state, "_clarify", False) else kind
    else:
        ps = phrasings(q, bank)
        retry = getattr(state, "_retry", False)
        again = retry or cur.get("qid") == getattr(state, "_qid_before", None)
        text, allowed = RP.pick(ps, (cur.get("said") or ps[0]) if again else None)
        if quiet:  # 決まった出し直し(「続ける」など): 脱線から戻るのではないので、前と同じ言い方
            text = cur.get("said") if cur.get("said") in ps else ps[0]
            allowed = [text]
        ask = {"qid": cur["qid"], "mode": mode, "text": text, "phrasings": allowed}
        if retry:  # 読めなかった答え: 責めずに、押すだけで答えられることを添えて、違う言い方で出し直す
            notes.append(ENTRY.texts["retry_number" if q.get("kind") == "number" else "retry"])
            kind = None
        elif A.has(reading, "ask_service", "ask_advice"):  # 逆質問(ほかの働きと一緒でも): 答えの部品
            answer = {"facts": facts(), "fallback": ENTRY.texts["no_answer"]}
    hook = (reading or {}).get("hook") or (button if button and len(button) <= A.HOOK_MAX else None)
    hidden = guard.hidden(getattr(state, "_spans", []))
    if quiet:  # モデルを呼ばない出し直し(危ない言葉の後の「続ける」など): 前置きの文と問いだけ
        kind, answer = None, None
        notes = [lead] if lead else []
    plan = RP.new_plan(ask, kind, hook, answer, notes, lines, v["choices"], v["user_text"], hidden)
    convo = state.convo
    v |= {"text": ask["text"], "model_text": v["user_text"], "model_history": CS.messages(convo), "entry": ENTRY.title, "tone": ENTRY.tone["style"],
          "examples": " / ".join(ENTRY.tone.get("examples") or []) or "(なし)", "hook": plan["hook"] or "(なし)",
          "ack": ACK_JA[plan["ack"]], "ack_max": plan["ack_max"],
          "answer": (f"答える。使ってよい事実(これだけ。無いことは「お答えできません」と言う): {plan['facts']}" if plan["answer"]
                     else f"書かない(システムが「{plan['fixed_answer']}」と答える)" if plan["fixed_answer"] else "しない"),
          "recent_acks": " / ".join(a for a in convo.get("acks", []) if a) or "(なし)",
          "last_reply": ((convo.get("replies") or [""])[-1] or "(なし)").replace("\n", " / "),
          "lines": " / ".join([*notes, *lines]) or "(なし)",
          "next_question": json.dumps({"id": ask["qid"], "mode": v["mode"], "text": ask["text"], "phrasings": ask["phrasings"]},
                                      ensure_ascii=False)}
    return v, plan


def _more(state, q):
    """「他の候補」: 見せていない候補の次のページ(c1 の _more と同じ)。前置きは入り口の定義の文で、問いの短い名前から作る
    (モデルを呼ばない。数えない)。出し尽くしたら最初のページに戻す。"""
    cur = state.current
    i, cands = H.next_page(q, cur)
    if i is None:
        cur.update(page=len(q["pages"]), choices=H.page_choices(q, q["pages"][0], cur["mode"], more=False))
        lead = ENTRY.texts["more_done"]
    else:
        cur.update(page=i, choices=H.page_choices(q, cands, cur["mode"]))
        cur["shown"] = list(dict.fromkeys([*cur.get("shown", []), *H._shown_now(q, cur["choices"])]))
        lead = RP.more_lead(ENTRY.texts, q["topic"], q.get("kind"))
    H.ev(state, "more", qid=q["id"], page=cur["page"], done=i is None)
    return H._out(state, [lead], cur["choices"], counted=False)
