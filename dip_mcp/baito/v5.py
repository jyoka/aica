"""アルバイト・パートの相談の v5(BAITO_HARNESS_VERSION=v5)。会話の層(dip_mcp/convo/)に載せ替えた版。

設計は docs/design/conversation-layer.md(§4.13 の v5、§5 の 7 枚目)と ADR-0011。v4(dip_mcp/baito/harness.py。PRD の参照実装の移植)は
変えずに残し、この版は v4 の質問バンクの原文(bank_v4.json)、欄と区切り、注意文(NOTICE)、危ない言葉の表と文(crisis_v4.json)、
未成年と闇バイトの決まり、入口のカード、近くで探す、職種の結果、まとめ(review)と結果をそのまま使う。v4 と違うのは次のところ:

- 読む段(§4.3): 1 つの intent でなく、act の並びで読む(llm.read。プロンプトは dip_mcp/prompts/convo/read_ja.txt に
  アルバイト・パートの決まり prompt_read_v5_ja.txt をつなぐ)。値の act は区切りのキーで返し、区切りにあるかを確かめる
  (v4 の normalize_value で表示名もキーに直す)。直前のやりとり(状態の convo)も渡す
- 値の当て方(§4.4): 答えとほかの欄の値は v4 の当て方(apply_update と確信度の決まり)のまま。直した値(correct)は high で置きかえ、
  取り消し(retract)は空にし、迷い(unsure)は low で入れて進む。今の問いに答えずにほかの欄の値だけを言ったときは、値を入れて同じ問いを
  出し直す(脱線と同じ数え方)。直すだけのターンは数えずに今の問いを出し直す。「わからない」は欄を空のまま、「特にない」は
  「こだわりなし」にして次へ。値を読めない「うん」は、数えずに 1 回だけ違う言い方で聞き直す。年齢は v4 の決まり(task 258。はっきりした
  形だけ読み、読めなければボタンで 1 回聞き直し、「中学」の答えが 2 回続けば止める)を、読んだ値にもそのまま当てる
- 応じ方の計画(§4.6。dip_mcp.convo.plan): 雑談、逆質問、感情、不満、事実と違う頼み、別の入り口を考えているなど、値も操作も無い発言は
  計画のとおりに応じる。逆質問には事実のデータ(facts.json と dip_mcp/convo/facts.json)だけを渡す。答えでないターンが続いたときと
  疲れたと言ったときは「中断」の注意を 1 回だけ添え、疲れたときは HARD のほかを聞かずにまとめへ進む(急ぐ)
- 言う段(§4.8。dip_mcp.convo.reply): 問いのターンは、計画を返事の部品(相づち → 答え → 注意 → 区切り → 問い)に組む。問いの文は、質問バンクの
  原文と言い方(入り口の定義の variants。言い換えのある問いは PRD の alt_text)から選ぶ。v4 の注意文(NOTICE)と決まり文句(保留を捨てた、
  区切りに寄せた)は注意の場所に入れる。返事の確かめに、v4 の決まり(金額を言わない、入れていない答えと数を言わない、カードに無い職種を
  言わない)を足す。まとめと結果、入口のカード、近くで探すの返事は v4 のまま(プロンプトも PRD の原文)
- 問いの選び方(§4.7): フェーズの順と上限は v4 のまま(PRD の流れ)。同じフェーズの中で、今の話題の問いにおまけ(入り口の定義の topics)。
  ほかの入り口から引き継いだ値は、推測でも聞き直さない(まとめで確かめる)
- 入り口を移す(§4.5。dip_mcp.convo.switch): 「やっぱり正社員で探したい」(移る強い頼み EXIT_REQUEST か、読む段の switch の want)は、
  発言の中の値も当ててから閉じる(返す形の switch。引き継ぎの記録を書いて移るのは session)。「正社員も考えてる」(consider)は移らずに、
  移るボタンを今の問いのボタンの前に足し、注意を 1 回だけ添える。ほかの入り口から移ってきた相談は、記録の値を入れる(take_handoff)
- 「あとで」と「やめる」(§4.6): 決まった文で閉じる(トークの AI を挟まない)。「あとで」と一緒に打った答えも入れる。
  そこまでの確かめた値は session が保存し、次の相談は保存した値を聞かずに始める(prefill)
- LINE のスタンプ(§4.12): ランタイムが「[スタンプ]」の文にして渡す。読む段を呼ばずに雑談として読む
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field

from dip_mcp.baito import harness as H
from dip_mcp.convo import acts as A
from dip_mcp.convo import entry as E
from dip_mcp.convo import facts as F
from dip_mcp.convo import guard
from dip_mcp.convo import pick as PK
from dip_mcp.convo import plan as P
from dip_mcp.convo import reply as RP
from dip_mcp.convo import state as CS
from dip_mcp.convo import switch as SW
from dip_mcp.convo.guard import crisis_hit, wants_resume
from dip_mcp.convo.turn import call_llm, parse_json
from dip_mcp.convo.turn import render_prompt as _render

VERSION = "v5"
ENTRIES = ("career", "baito")  # 入り口の名前(移る先の確かめ)
NEAR_AREA = "near.area"  # 近くで探すのエリア(state.near の area。区切りのキーは NEAR_AREAS の都県)
SLOTS = (*H.SLOTS, NEAR_AREA)
TEXTS = ("retry", "retry_review", "no_answer", "switched_in", "later", "exit", *(f"note_{n}" for n in P.NOTES))
ACK_JA = {"short": "本人の答えを受け取る、短い相づち", "done": "何をしたか(計画の about)を、1 文で短く言う",
          "light": "雑談に軽く 1 文で乗る",
          "empathy": "気持ちを受け止める(励まし、根拠の無い約束、助言をしない。人やお店を評価しない。強い言葉をなぞらない)",
          "apology": "聞き方への不満に、短く謝る", None: "付けない"}
ENTRY = E.load(H.HERE / "entry.json", SLOTS, ENTRIES, TEXTS)
VARIANTS = {q["id"]: q.get("variants") or [] for q in ENTRY.bank}
TARGETS = tuple(s["to"] for s in ENTRY.switch)  # 移れる入り口
CRISIS_TEXT = H.CRISIS_TEXT  # 危ない言葉の文は v4 のまま(利用者の確認待ち。ADR-0009)
READ_PROMPTS = [H.PROMPT_DIR.parent / "convo" / "read_ja.txt", H.PROMPT_DIR / "prompt_read_v5_ja.txt"]
SAY_PROMPTS = [H.PROMPT_DIR.parent / "convo" / "say_ja.txt"]
ASKING = ("ask", "reask", "retry", "clarify")  # 質問バンクの問いを出している mode(言い方を選び、移るボタンを足せる)
LAYER_MODES = (*ASKING, "confirm", "pick")  # 層の組み立てで返事を組む mode(ほかは v4 の返事)
PAUSE_FLAG = "pause_noted"  # 「中断」の注意を添えた印(相談で 1 回だけ)
SWITCH_FLAG = "switch_noted"  # 移れることの注意を添えた印(移る先ごとに 1 回だけ。「switch_noted:career」)
HURRY = "hurry"  # 疲れたと言った印: この後は HARD のほかを聞かない(pick_question)
CARRIED = "handoff:"  # ほかの入り口から引き継いだ値の from(「handoff:career」。聞き直さず、まとめで確かめる)
SAVED = "saved"  # 前の相談で保存した値の from(prefill)
LATER_WORDS = {"中断", "中断する", "相談を中断", "あとで", "また今度"}  # 読む段を呼ばずに「あとで」にする言葉(正社員の END_WORDS と同じ形)
QUICK_MAX = 13  # LINE のクイックリプライの数の上限
# 区切りの欄の値(区切りのキー)。好みの欄は「こだわりなし」も値にできる(v4 の NOPREF。事実の欄は apply_update が受けない)
VALUES = {s: {*v["values"], *([H.NOPREF] if v["kind"] == "pref" else [])} for s, v in H.SLOTS.items()} | \
    {NEAR_AREA: {k for k, v in H.NEAR_AREAS.items() if v}}
# 前の相談で保存した希望の項目(dip_mcp.logic.PREFERENCE_KEYS。session.result_preferences が書く) -> 欄(prefill)
PREFILL = {"days_per_week": "days_per_week", "time_band": "time_slots", "min_wage": "hourly_wage_min", "purpose": "purpose"}
# 希望の項目 -> その項目を作る欄(session.result_preferences)。「あとで」の続きで、閉じた後に直した項目の欄を外す(refresh_saved)
FEEDS = {"days_per_week": ("days_per_week",), "time_band": ("time_slots", "available_hours", "weekday_or_weekend"),
         "min_wage": ("hourly_wage_min",), "purpose": ("purpose",)}


@dataclass
class State(H.State):
    version: str = VERSION
    convo: dict = field(default_factory=CS.new)  # 層の覚え(直前のやりとりと返信。dip_mcp.convo.state)
    offtrack: int = 0  # 続いた答えでないターンの数(計画の offtrack。答えて次へ進むと 0)
    came_from: dict = None  # ほかの入り口から移ってきたときの引き継ぎの記録(すぐ戻るときに、元の入り口の値を戻すため。§4.5)
    closed: str = None  # 「あとで」(later)・「やめる」(exit)・別の入り口へ移る(switch)で閉じた
    saved_area: str = None  # 前の相談で保存したエリアから入れた近くで探すのエリア(閉じるときに書き戻さない)
    saved_ng: list = field(default_factory=list)  # 「あとで」で閉じたときに避けたいことに保存した本人の NG(閉じるときに書き戻さない)
    kept: dict = None  # 「あとで」の続きで、相談の状態が合っている希望の項目の文(kept_texts。もう一度「あとで」で閉じる前に読み直すため)


def new_state(quick=False):
    """空の状態(v4 の new_state と同じ。版と層の覚えだけが違う)。"""
    st = State()
    H.recompute_derived(st)
    if quick:
        st.flags.append(H.QUICK_FLAG)
        H._quick_entry(st)
    if hasattr(st, "_new_notices"):
        del st._new_notices
    return st


def render_prompt(stage, variables):
    """読む段(read)は層の骨組みにアルバイト・パートの決まりをつないだもの。層の言う段(計画 plan のある say)は層の骨組み。
    まとめ、結果、入口のカードと近くで探すのリアクションは v4 のプロンプト(PRD の原文)。"""
    if stage == "read":
        return _render(READ_PROMPTS, variables)
    if stage == "say" and "plan" in variables:
        return _render(SAY_PROMPTS, variables)
    return H.render_prompt(stage, variables)


prompt_stage = H.prompt_stage


def _log(state):
    return lambda kind, **kw: H.ev(state, kind, **kw)


def start(state, bank=H.BANK):
    """最初の問い(v4 の start と同じ。ターンに数えない)。最初の 1 回だけ、出した文を直前のやりとりに覚える。"""
    H._init_volatile(state)
    state._new_notices = []
    if state.current is None:
        advance(state, bank)
    v = H.view(state, bank)
    if not state.convo.get("recent") and v.get("text"):
        CS.replied(state.convo, v["text"])
    return v


def resume(state, bank=H.BANK):
    v = start(state, bank)
    v["resumed"] = True
    return v


# ----------------------------- 問いの選び方(§4.7) -----------------------------
def _gap(state, slot):
    """問いのスコアの空き具合(v4 の gap_weight)。ほかの入り口から引き継いだ値は、推測でも聞き直さない(まとめで確かめる。§4.5)。"""
    e = state.slots.get(slot)
    if H.is_filled(e) and str(e.get("from", "")).startswith(CARRIED):
        return 0.0
    return H.gap_weight(e)


def pick_question(state, bank, phase):
    """v4 の pick_question(フェーズの中で、優先度 × 空き具合のいちばん高い問い)に、今の話題のおまけを足したもの。
    疲れたと言った後(HURRY)は、HARD の欄だけを数え、二択と逆選択は出さない。"""
    pri = H.active_slots(state)
    hurry = HURRY in state.flags
    if phase == "hard_filter" and "hf_age" not in state.asked and H.gap_weight(state.slots.get("age_band")) > 0:
        return H.qmap(bank)["hf_age"], "ask"  # 年齢を必ず最初に聞く(v4 のまま)
    if hurry and phase in ("scenario_game", "reverse_pick"):
        return None, None
    phases = {phase, "hard_filter"} if phase == "fill_gaps" else {phase}
    touched = getattr(state, "_topics", set())
    best, best_score, best_mode = None, 0.0, "ask"
    for q in bank:  # 同点はバンクの並び順(「より大きい」だけで比べる)
        if q["phase"] not in phases or not H._segment_ok(q, state):
            continue
        if q["id"] == "q_segment":
            if state.segment == "unknown" and q["id"] not in state.asked and H.SEGMENT_SCORE > best_score:
                best, best_score, best_mode = q, H.SEGMENT_SCORE, "ask"
            continue
        mode = "ask"
        if q["id"] in state.asked:
            if phase == "fill_gaps" and q["phase"] in H.DIRECT and all(
                    state.attempts.get(t, 0) < H.MAX_ATTEMPTS for t in q["targets"] if t in pri):
                mode = "reask"
            else:
                continue
        targets = [t for t in q["targets"] if t in pri and (not hurry or pri[t] == H.HARD)]
        if phase in H.DIRECT:
            targets = [t for t in targets if state.attempts.get(t, 0) < H.MAX_ATTEMPTS]
        elif phase == "scenario_game":
            targets = [t for t in targets if state.topic_count.get(t, 0) < H.TOPIC_CAP]
        if phase == "reverse_pick":
            score = 1.0
        else:
            score = sum(pri[t] * _gap(state, t) for t in targets)
            if score > 0 and "all" not in q["segments"]:
                score += 0.5
            if score > 0:
                score += PK.bonus(targets, touched, ENTRY.topics)
        if len(H.visible_choices(q, state)) < 2:
            continue
        if score > best_score:
            best, best_score, best_mode = q, score, mode
    return (best, best_mode) if best else (None, None)


def advance(state, bank):
    """次に出すものを決めて state.current に入れる(v4 の advance と同じ。問いはこの版の pick_question で選ぶ)。"""
    if state.pending_segment and state.phase != "result":
        np = H.next_phase(state, bank)
        if np in ("review", "result") or H._confirm_used(state):
            H.ev(state, "confirm_skipped", pending=state.pending_segment)
            state.pending_segment = None
        elif np == "reverse_pick":
            pass
        else:
            state.phase = np
            state.phase_turns.setdefault(np, 0)
            H._flag(state, "segment_asked")
            state.current = {"qid": "confirm_segment", "mode": "confirm", "choices": list(H.qmap(bank)["q_segment"]["choices"])}
            if state.pending:
                H._flag(state, "held_asked")
                state.current.update(choices=list(H._held_q(bank)["choices"]), held=sorted(state.pending))
            return
    if H._ask_held(state, bank):
        return
    for _ in range(len(H.ORDER) + 2):
        np = H.next_phase(state, bank)
        state.phase = np
        if np in ("review", "result"):
            break
        q, mode = pick_question(state, bank, np)
        if q:
            H._present(state, q, mode)
            return
        state.phase = H.ORDER[H.ORDER.index(np) + 1]
        if state.phase == "review":
            break
        state.phase_turns.setdefault(state.phase, 0)
    else:
        state.phase = "review"
    if state.phase == "review":
        state.current = {"qid": "review", "mode": "review", "choices": [H.REVIEW_OK]}
        state.last_card = H.build_card(state)
    else:
        H.finalize(state)
        state.current = {"qid": "result", "mode": "result", "choices": []}


# ----------------------------- 読む(§4.3) -----------------------------
def _values_ja(slot):
    if slot == NEAR_AREA:
        return " / ".join(sorted(VALUES[NEAR_AREA]))
    return " / ".join(f"{k}={H.value_ja(slot, k)}" for k in H.SLOTS[slot]["values"])


def read_payload(state, bank, user_text):
    """読む段の入力。v4 の抽出の入力(今の問い、ボタン、使ってよい欄)に、欄の説明と区切りのキー、移れる入り口、直前のやりとりを足したもの。"""
    p = H.extract_payload(state, bank, user_text)
    labels = {s: H.SLOTS[s]["label"] for s in H.SLOTS} | {NEAR_AREA: "エリア", "segment": "今の立場"}
    allowed = [*p["allowed_slots"], NEAR_AREA]
    return {"entry": ENTRY.title, "phase": p["phase"], "segment": state.segment, "mode": (state.current or {}).get("mode"),
            "question": p["question"], "choices": p["choices"], "targets": {t: labels.get(t, t) for t in p["targets"]},
            "slots": {s: f"{labels[s]}。{ENTRY.slots[s]}。値: {_values_ja(s)}" for s in allowed},
            "segments": {s: H.SEGMENT_JA[s] for s in H.SEGMENTS},
            "switch": [{"to": s["to"], "label": s["label"]} for s in ENTRY.switch],
            "recent": CS.recent_lines(state.convo), "user_text": user_text}


def _normalized(a):
    """値の act の value を区切りのキーにそろえる(表示名や「週3」も v4 の normalize_value で読む)。読めなければそのまま(検証で落ちる)。"""
    if isinstance(a, dict) and a.get("act") in A.VALUE_ACTS and a.get("slot") in H.SLOTS and a.get("value") is not None:
        nv = H.normalize_value(a["slot"], a["value"])
        if nv is not None:
            return dict(a, value=nv)
    return a


def validator(user_text):
    def check(raw):
        d = parse_json(raw)
        if isinstance(d, dict) and isinstance(d.get("acts"), list):
            d = dict(d, acts=[_normalized(a) for a in d["acts"]])
        return A.validate_reading(d if d is not None else raw, user_text, SLOTS, TARGETS, VALUES, H.SEGMENTS)
    return check


def _ng(reading, state):
    ng = (reading or {}).get("extra", {}).get("ng") or []
    ng = [ng] if isinstance(ng, str) else ng if isinstance(ng, list) else []
    return [x for x in (guard.scrub(n, state._spans) for n in ng if isinstance(n, str)) if x]


def to_ext(reading, state):
    """読んだ act を、v4 の抽出の形(slot_updates、segment、intent、review_ack)にする。年齢の決まり、区切りの境目、まとめ、区分の
    確かめは v4 の作りをそのまま使うため。slot_updates の op は値の act の名前(answer / add / correct / unsure)。"""
    acts = (reading or {}).get("acts", [])
    ups = [{"slot": a["slot"], "value": copy.deepcopy(a["value"]), "evidence": a["text"], "op": a["act"]} for a in acts
           if a["act"] in A.VALUE_ACTS and a["slot"] in H.SLOTS]
    seg = next((a["value"] for a in acts if a["act"] in A.VALUE_ACTS and a["slot"] == "segment"), None)
    intent = "show_result" if A.has(reading, "show_result") else "clarify" if A.has(reading, "clarify") else "answer"
    ack = "all" if A.has(reading, "confirm") else "partial" if ups or seg else "none"
    return {"slot_updates": ups, "ng_updates": _ng(reading, state), "ng_removals": [], "segment": seg, "intent": intent,
            "review_ack": ack}


TALK_ONLY = {*A.TALK, "emotion", "switch", "go_back", *A.SPAN_ACTS}  # 値でも操作でもない act


def talk_only(reading):
    """値も操作も無い発言か(act が 1 つ以上あり、どれも話・感情・入り口・守りの部分)。"""
    names = [a["act"] for a in (reading or {}).get("acts", [])]
    return bool(names) and all(n in TALK_ONLY for n in names)


# ----------------------------- ターン -----------------------------
def _init_turn(state):
    """そのターン限りの層の値: 氏名と要配慮の部分、値を言った話題、読んだもの、押したボタン、返事の相づち、聞き直したか、
    聞き返しに説明したか、ターンの始めの問い。"""
    state._spans, state._topics, state._reading, state._button, state._ack = [], set(), None, None, None
    state._retry = state._clarify = False
    state._slots_before = copy.deepcopy(state.slots)  # 値を言った欄(話題のおまけ)を見分けるため
    state._qid_before = (state.current or {}).get("qid")


def turn(state, user_text, bank=H.BANK, llm=None, catalog=None, search=None):
    """本人の発言 1 通を処理する(返す形は v4 の turn と同じ {reply, view, done, counted}。層の組み立ての返事には parts、
    閉じたときは closed、別の入り口へ移るときは switch が付く)。ハーネスの中の例外は、ターンの始めの状態に戻して今のものを出し直す。
    終わりに、発言(氏名と要配慮の部分は伏せる)と返信を直前のやりとりに覚える。"""
    H._init_volatile(state)
    _init_turn(state)
    snap = state.to_dict()
    try:
        out = _turn(state, user_text, bank, llm, catalog, search)
    except Exception as e:  # noqa: BLE001
        restored = State.from_dict(snap)
        state.__dict__.clear()
        state.__dict__.update(restored.__dict__)
        H._init_volatile(state, reset=True)
        _init_turn(state)
        H.ev(state, "internal_error", error=type(e).__name__)
        if state.result:
            return {"reply": state.result["text"], "view": H.view(state, bank), "done": True, "counted": False}
        return _represent(state, bank, counted=False)
    if isinstance(user_text, str) and H.nfkc(user_text):
        CS.heard(state.convo, guard.mask(user_text, state._spans))
        CS.replied(state.convo, out["reply"], state._ack)
    return out


def _q(state, bank):
    cur = state.current or {}
    if cur.get("qid") == "confirm_segment":
        return H._held_q(bank) if cur.get("held") else H.qmap(bank).get("q_segment")
    return H.qmap(bank).get(cur.get("qid"))


def _words():
    """やめる言葉と、正社員へ移る強い頼み(EXIT_REQUEST の正社員の部分。求人を見たい頼みは session が今と同じく相談の外へ出す)と、
    打ち消しと後回し(dip_mcp.baito.session が持つ。session がこのモジュールを読み込むので、使うときに読む)。"""
    from dip_mcp.baito.session import CAREER_REQUEST_RE, EXIT_HOLD, EXIT_WORDS

    return EXIT_WORDS, CAREER_REQUEST_RE, EXIT_HOLD


def asked_to_switch(state, text):
    """読む段を呼ばずに移る先が決まる発言 -> 移る先か None。移るボタンの文か、まとめの前の移る強い頼み(打ち消しと後回しの EXIT_HOLD に
    当たるものは除く。v4 の session._exits と同じく、中学生以下で止めた後は移らない)。"""
    t = H.nfkc(text)
    for s in ENTRY.switch:
        if t == H.nfkc(s["label"]):
            return s["to"]
    if state.phase in ("review", "result") or state.blocked:
        return None
    _, request, hold = _words()
    return next((to for to in TARGETS if to == "career" and SW.requested(text, request, hold)), None)


def _turn(state, user_text, bank, llm, catalog, search=None):
    state._new_notices, state._discarded, state._snapped = [], [], []
    text = user_text if isinstance(user_text, str) else ""
    state._user_text = text
    if state.current is None:
        advance(state, bank)
    if state.phase == "result" and state.result:
        return _post_result(state, text, bank, llm, catalog)
    cur = state.current
    q = _q(state, bank)
    if not H.nfkc(text):
        H.ev(state, "empty_input")
        return _represent(state, bank, counted=False)
    buttons = H.match_button(state, bank, text)
    sticker = buttons is None and H.nfkc(text) == A.STICKER
    crisis_open = H._take_crisis(state)
    if buttons is None:  # 守りの前処理(v4 と同じ。選択肢の文字そのものは答えとして読む)
        if crisis_hit(text):
            return H._crisis(state, bank)
        if crisis_open and wants_resume(text):
            H.ev(state, "crisis_resume")
            return _represent(state, bank, counted=False, lead=H.CRISIS["resume_lead"])
    if buttons is None and H.DANGER_RE.search(H.nfkc(text)):
        if state.phase in H.QUICK_PHASES:  # 入口では数えない(v4 と同じ)
            H._flag(state, "danger_warned")
            H.ev(state, "danger_skip", where=state.phase)
            return H._quick_reply(state, bank, H.NOTICE["danger"])
        return _danger(state, bank, llm, catalog)
    exit_words, _, _ = _words()
    if buttons is None and (text.strip() in exit_words or H.nfkc(text) in LATER_WORDS):
        return _close(state, bank, "exit" if text.strip() in exit_words else "later")
    to = asked_to_switch(state, text) if buttons is None else None
    if to:  # 移るボタンか、移る強い頼み(読む段を呼ばない)
        return _switch(state, bank, q, SW.want_reading(to), text, to)
    if state.phase in H.QUICK_PHASES:  # 入口のカード、職種の結果、近くで探す: v4 のまま(ボタンだけ読む)
        if state.phase == H.NEAR_JOBS and buttons and buttons[0] == H.NEAR_MORE:
            H.ev(state, "near_more")  # 今の流れへ(v4 と同じ)。最初の問いは層の組み立てで出す
            state.phase, state.current = "icebreak", None
            advance(state, bank)
            state._button = buttons[0]
            return _say(state, bank, llm, catalog, counted=False)
        return H._quick_turn(state, text, bank, llm, catalog, search)
    before = {s: copy.deepcopy(state.slots.get(s)) for s in H.BUCKETS}
    reading = None
    if sticker:  # スタンプは雑談(読む段を呼ばない)
        reading = A.sticker_reading()
    elif buttons is None:
        reading = call_llm(llm.read, read_payload(state, bank, text), validator(text), _log(state), "read")
        state._spans = A.spans(reading)
        state._user_text = guard.mask(text, state._spans)  # 言う段に、氏名と要配慮の部分を渡さない
    state._reading, state._button = reading, (buttons[0] if buttons and len(buttons) == 1 else None)
    if A.has(reading, "crisis"):  # 表に無い危ない言い方(§6 の 15)
        return H._crisis(state, bank)
    if A.has(reading, "danger"):  # 闇バイトらしい話(DANGER_RE の後ろの網)
        return _danger(state, bank, llm, catalog)
    if reading is not None:  # 候補に使える本人の言葉を覚える(氏名・要配慮の部分と伏せた印を含むものは覚えない)
        CS.mentioned(state.convo, [w for w in reading["mentions"] if guard.scrub(w, state._spans) == w])
    to = SW.wanted(reading, TARGETS)
    if to:  # 別の入り口へ移る(§4.5)
        return _switch(state, bank, q, reading, text, to)
    if A.has(reading, "later", "stop"):
        if q is not None and cur["mode"] in ASKING:
            _apply_plain(state, bank, q, _ages(state, cur, q, to_ext(reading, state), text, buttons), text)
        return _close(state, bank, "exit" if A.has(reading, "stop") else "later")
    if A.has(reading, "restart"):
        return _restart(state, bank, llm, catalog)
    ext = to_ext(reading, state) if reading is not None else None
    if buttons is None and not sticker:
        ext = _ages(state, cur, q, ext, text, buttons)
    if talk_only(reading) and not (ext and (ext["slot_updates"] or ext["segment"])):
        if cur["mode"] in ("review", "pick", "confirm"):  # まとめと確かめの問いへの脱線は v4 と同じ(同じものを出し直す)
            return _aside(state, bank, llm, catalog, P.Plan(ack="light", next="keep", count="aside", offtrack=True))
        return _respond(state, bank, llm, catalog, q, P.respond(reading, entries=TARGETS))
    intent = (ext or {}).get("intent", "answer")
    # ---- 数えるターン(v4 と同じ)
    H._count(state)
    mode = cur["mode"]
    if mode == "pick":
        return _pick_answer(state, bank, llm, catalog, cur, text, buttons, ext, intent, before)
    if mode == "review":
        return _review(state, bank, llm, catalog, text, buttons, ext, intent, before)
    return _ask(state, bank, llm, catalog, cur, q, text, buttons, reading, ext, intent, before)


def _ages(state, cur, q, ext, text, buttons):
    """年齢の決まり(v4 の task 258): 年齢の問いへの答えは _age_answer、ほかは _read_typed_age(はっきりした形だけ読む)。"""
    if buttons is not None:
        return ext
    if H._asks_age(cur, q):
        return H._age_answer(state, cur, ext, text)
    return H._read_typed_age(state, ext, text, buttons=cur["mode"] == "review")


def _ask(state, bank, llm, catalog, cur, q, text, buttons, reading, ext, intent, before):
    """問いと区分の確かめへの発言(v4 の _turn の後半)。読んだ act の当て方を足した。"""
    mode = cur["mode"]
    held_confirm = mode == "confirm" and bool(cur.get("held"))
    asks_age = H._asks_age(cur, q)
    if buttons is not None:
        H._apply_effects(state, bank, q, buttons, "confirm" if mode == "confirm" else "choice")
        if mode == "confirm":
            state.pending_segment = None
    elif ext is None:  # 読む段が 2 回とも使えなかった
        if mode == "confirm":  # 05-B8: 読めなければ今のセグメントのまま
            H.ev(state, "confirm_unparsed", kept=state.segment)
            state.pending_segment = None
        elif cur["qid"] not in state.retried:  # 1 回目: 数えずに 1 回だけ聞き直す(「受け取れなかった」とは言わない)
            H._seg_words(state, text)
            if "age_band" in q["targets"]:
                H._age_asked_again(state)
            return _retry(state, bank, llm, catalog, cur)
        else:
            H.ev(state, "extract_gave_up", qid=cur["qid"])
            H._seg_words(state, text)
            if asks_age:
                H.ev(state, "age_unread_kept_minor", slot="age_band")
    elif intent == "clarify" and mode != "confirm" and cur["qid"] not in state.clarified \
            and state.clarify_used < H.MAX_CLARIFY and H.clarify_room(state):
        state.clarified.append(cur["qid"])
        state.clarify_used += 1
        H._seg_words(state, text)
        state.current = dict(cur, mode="clarify")
        state._clarify = True
        return _say(state, bank, llm, catalog)
    else:
        if intent == "clarify":
            H.ev(state, "clarify_suppressed", qid=cur["qid"])
        if mode == "confirm":
            H._apply_ext(state, bank, ext, q=q, text=text, review=False, confirm=True)
            state.pending_segment = None
        else:
            got = _apply(state, bank, q, ext, reading, text, cur, intent)
            if got["pick"]:  # 境目の答え(v4 の task 268): 2 つの区切りをボタンで 1 回だけ聞き直す
                H._ask_pick(state, got["pick"])
                return _say(state, bank, llm, catalog)
            if not got["answered"] and intent != "show_result" and not asks_age:
                if got["fixed"] and not got["added"]:  # 直した・取り消した: 数えずに、今の問いを出し直す(§4.6)
                    H._uncount(state)
                    p = P.respond(reading, fixed=True, entries=TARGETS)
                    p.about = got["did"]
                    return _say(state, bank, llm, catalog, counted=False, aside=True, plan=p)
                if got["added"] or any(a["act"] in TALK_ONLY for a in (reading or {}).get("acts", [])):
                    # ほかの欄の値だけ(雑談や逆質問と一緒でも): 同じ問いを出し直す(脱線と同じ数え方)
                    H._uncount(state)
                    H._seg_words(state, text)
                    return _aside(state, bank, llm, catalog, P.respond(reading, entries=TARGETS))
                if cur["qid"] not in state.retried:  # 値を読めない「うん」の 1 回目: 数えずに、違う言い方で聞き直す
                    H._seg_words(state, text)
                    return _retry(state, bank, llm, catalog, cur)
                H.ev(state, "extract_gave_up", qid=cur["qid"])
            H._seg_words(state, text)  # task 267: 違う区分の言葉があれば次のターンで確かめる
        if mode != "confirm" and H._age_unread(state, cur, q, ext):  # task 258: 未成年として扱ったまま、ボタンで 1 回だけ聞き直す
            if cur["qid"] not in state.retried and H.clarify_room(state):
                state.retried.append(cur["qid"])
                H._age_asked_again(state)
                H.ev(state, "age_reask", slot="age_band")
                state.current = dict(cur, mode="reask")
                return _say(state, bank, llm, catalog, plan=P.Plan(ack=None))
            if "age_jhs" in state.flags:  # 聞き直す余地が無い「中学」の答えは、その場で中学生以下として止める
                H._apply_ext(state, bank, H._jhs_stop(state, None, text), q=q, text=text, review=False)
            else:
                H.ev(state, "age_unread_kept_minor", slot="age_band")
    if held_confirm:  # 区分を確かめた: 表にあれば入った。無い・決まらなければ捨てる
        H._settle_held(state, force=True)
        if state.phase == "review":
            state._need_more_review = bool(cur.get("need_review") or state.conflicts or state.pending_segment) or \
                any(H.gap_weight(state.slots.get(s)) > 0 for s in H.hard_slots(state))
    if intent == "show_result":
        H._flag(state, "show_result")
    return _next(state, bank, llm, catalog, plan=P.respond(reading, answered=True, entries=TARGETS) if reading is not None else None)


def _next(state, bank, llm, catalog, plan=None):
    """数えたターンの後: 次の問いへ進む。答えでないターンの続きは 0 に戻す。値を言った欄の話題に、次の問いのおまけを付ける。"""
    state.offtrack = 0
    old = getattr(state, "_slots_before", None) or {}
    stated = [s for s, e in state.slots.items() if e and e != old.get(s) and H.is_filled(e)
              and not str(e.get("from", "")).startswith(("derived", "pass:", CARRIED, SAVED))]
    state._topics = PK.touched_topics(stated, ENTRY.topics)
    advance(state, bank)
    return _say(state, bank, llm, catalog, plan=plan)


def _retry(state, bank, llm, catalog, cur):
    """読めない答えの 1 回目(数えた後に呼ぶ): 数えずに、押すだけで答えられることを添えて、違う言い方で聞き直す
    (v4 の「うまく受け取れなかった」の代わり)。"""
    state.retried.append(cur["qid"])
    H._uncount(state)
    state.current = dict(cur, mode="retry")
    state._retry = True
    return _say(state, bank, llm, catalog, counted=False)


def _apply_plain(state, bank, q, ext, text):
    """答えとほかの欄の値を v4 の当て方(_apply_ext)で入れる(「あとで」と一緒の答え、移る発言の中の値)。直す・迷う・取り消しも入れる。"""
    if ext is None:
        return
    plain = [u for u in ext["slot_updates"] if u.get("op") in (None, "answer", "add")]
    H._apply_ext(state, bank, dict(ext, slot_updates=plain), q=q, text=text, review=False)
    for u in ext["slot_updates"]:
        if u.get("op") in ("correct", "unsure"):
            _replace(state, u["slot"], u["value"], "high" if u["op"] == "correct" else "low", f"{u['op']}:{q['id']}")


def _replace(state, slot, value, conf, frm):
    """前の値(high でも)を置きかえる(本人が直した・迷っている)。決まりで受け付けない値(未成年の深夜など)なら前の値に戻す。"""
    active = slot in H.active_slots(state)
    old = (state.slots if active else state.pending).pop(slot, None)
    conflict = state.conflicts.pop(slot, None)
    got = H.apply_update(state, slot, value, conf, frm)
    if got == "rejected" and old is not None:
        (state.slots if active else state.pending)[slot] = old
        if conflict:
            state.conflicts[slot] = conflict
    return got


def _apply(state, bank, q, ext, reading, text, cur, intent):
    """読んだ値を当てる(§4.4)。-> {"answered": 今の問いの欄に値かパス, "added": ほかの欄に入れた, "fixed": 直した・取り消した,
    "did": 相づちで触れること, "pick": 境目の聞き直し}。答えとほかの欄の値は v4 の当て方(_apply_ext)、直す・迷う・取り消し・
    パス・「わからない」はここ。"""
    got = {"answered": False, "added": False, "fixed": False, "did": [], "pick": None}
    targets = [t for t in q["targets"] if t != "segment"]
    plain = [u for u in ext["slot_updates"] if u.get("op") in (None, "answer", "add")]
    special = [u for u in ext["slot_updates"] if u.get("op") in ("correct", "unsure")]
    sub = dict(ext, slot_updates=plain)
    if intent != "show_result" and not H._asks_age(cur, q) and H.clarify_room(state):
        got["pick"] = H._bucket_hold(state, sub, text, q=q)
    H._apply_ext(state, bank, sub, q=q, text=text, review=False)
    H._note_snaps(state, sub, text, {s: state._slots_before.get(s) for s in H.BUCKETS})
    if got["pick"] and state.blocked:
        H._release_pick(state, got["pick"])
        got["pick"] = None
    seg_q = "segment" in q["targets"]
    got["answered"] = bool(got["pick"]) or any(u["slot"] in targets for u in sub["slot_updates"]) or \
        (seg_q and bool(ext.get("segment")))
    got["added"] = any(u["slot"] not in targets for u in sub["slot_updates"]) or (not seg_q and bool(ext.get("segment")))
    for u in special:
        s, op = u["slot"], u["op"]
        if _replace(state, s, u["value"], "high" if op == "correct" else "low", f"{op}:{q['id']}") == "rejected":
            continue  # 決まりで受け付けない値(未成年の深夜だけ、など): 前の値のまま(直したとは言わない)
        H.ev(state, "corrected" if op == "correct" else "unsure", slot=s)
        if op == "correct":
            got["fixed"] = True
            got["did"].append(f"{H.SLOTS[s]['label']}を「{H.value_ja(s, (state.slots.get(s) or {}).get('value'))}」に直したよ")
        if s in targets:
            got["answered"] = True
        elif op == "unsure":
            got["added"] = True
    for a in (reading or {}).get("acts", []):
        if a["act"] == "retract" and a["slot"] == "age_band":
            H.ev(state, "retract_ignored", slot="age_band")  # 年齢は v4 の決まり(はっきりした形)でだけ変える(取り消しで止めを外さない)
        elif a["act"] == "retract" and a["slot"] in H.SLOTS:
            gone = state.slots.pop(a["slot"], None) or state.pending.pop(a["slot"], None)
            state.conflicts.pop(a["slot"], None)
            H.ev(state, "retracted", slot=a["slot"], had=gone is not None)
            got["fixed"] = True
            got["did"].append(f"{H.SLOTS[a['slot']]['label']}を取り消したよ")
        elif a["act"] in A.VALUE_ACTS and a["slot"] == NEAR_AREA and not state.near.get("area"):
            state.near["area"] = a["value"]  # 「東京で探したい」: 近くで探すのエリア(推測ではなく本人が言った都県)
            H.ev(state, "near_area_said", value=a["value"])
            got["added"] = True
    escapes = _escapes(reading, targets)
    if escapes and not got["answered"]:
        got["answered"] = True
        if any(o["act"]["act"] == "pass" for o in escapes):  # 「特にない」: 好みの欄を「こだわりなし」に
            for t in targets:
                if H.SLOTS[t]["kind"] == "pref" and not H.is_filled(state.slots.get(t)):
                    H.apply_update(state, t, H.NOPREF, H.confidence_for("free_current", state.phase, t, False), f"pass:{q['id']}")
        H.ev(state, "escaped", qid=q["id"], how=escapes[0]["act"]["act"])
    return got


def _escapes(reading, targets):
    """今の問いのパスと「わからない」(欄を書いていないか、今の問いの欄)。"""
    return [{"act": a} for a in (reading or {}).get("acts", []) if a["act"] in ("pass", "dont_know")
            and a.get("slot") in (None, *targets)]


def _pick_answer(state, bank, llm, catalog, cur, text, buttons, ext, intent, before):
    """境目の聞き直しへの答え(v4 の _pick_answer と同じ決まり。返事は層の組み立て)。"""
    s = cur["slot"]
    old = copy.deepcopy(state.slots.get(s))
    hard_changed = False
    if buttons is not None:
        H.apply_update(state, s, cur["values"][cur["choices"].index(buttons[0])], "high", "choice:bucket_pick")
        H.ev(state, "bucket_picked", slot=s, value=state.slots[s]["value"])
    else:
        if ext is None and cur.get("review") and H._review_jhs(state, text):
            ext = H._jhs_stop(state, None, text)
        ups = [u for u in (ext or {}).get("slot_updates", []) if u["slot"] == s and u["value"] is not None]
        rest = dict(ext, slot_updates=[u for u in ext["slot_updates"] if u["slot"] != s]) if ext else None
        if rest and cur.get("review"):
            hard_changed = H._apply_review_updates(state, rest)[1]
        elif rest:
            H._apply_ext(state, bank, rest, q=None, text=text, review=False)
        H._note_snaps(state, rest, text, before)
        H.ev(state, "bucket_pick_typed", slot=s, read=bool(ups) and intent in ("answer", "show_result"))
        if ups and intent in ("answer", "show_result"):
            H.apply_update(state, s, ups[-1]["value"], "high", "free:bucket_pick")
        else:
            H._release_pick(state, cur)
        if H.is_filled(state.slots.get(s)) and state.slots[s] != old and s not in state._snapped:
            state._snapped.append(s)
    if intent == "show_result":
        H._flag(state, "show_result")
    if cur.get("review"):
        e = state.slots.get(s)
        changed = H.active_slots(state).get(s) == H.HARD and H.is_filled(old) and old["confidence"] == "high" and \
            H.is_filled(e) and e["value"] != old["value"]
        last = bool(state.review_log and state.review_log[-1]["hard_changed"])
        H.update_block(state)
        state._need_more_review = "show_result" not in state.flags and H._review_need(state, last or changed or hard_changed)
    state.current = None
    return _next(state, bank, llm, catalog)


def _review(state, bank, llm, catalog, text, buttons, ext, intent, before):
    """まとめへの発言(v4 の review の決まりのまま。読めない 1 回目の文だけ、入り口の定義の文にした)。"""
    if buttons is None and ext is None:
        key = f"review{state.review_rounds + 1}"
        if key not in state.retried:  # 1 回目: 数えずにカードを出し直す
            state.retried.append(key)
            H._uncount(state)
            H.ev(state, "review_reply_unparsed", retry=True)
            if H._review_jhs(state, text):
                state.retried.append(key + "_jhs")
                H._flag(state, "age_jhs")
                H.ev(state, "age_jhs", extract_failed=True)
            return H._represent(state, bank, counted=False, prefix=ENTRY.texts["retry_review"])
        H.ev(state, "review_reply_unparsed", retry=False)
        if H._review_jhs(state, text) or key + "_jhs" in state.retried:
            ext = H._jhs_stop(state, None, text)
    if buttons is None and ext is None:
        H.apply_review(state, None, button_ok=False, promote=False)
    else:
        pick = H._bucket_hold(state, ext, text, review=True) if buttons is None and intent != "show_result" \
            and state.turns < H.MAX_TURNS else None
        H._apply_ext(state, bank, ext, q=None, text=text, review=True)
        H.apply_review(state, ext, button_ok=bool(buttons), hold=[pick["slot"]] if pick else ())
        H._note_snaps(state, ext, text, before)
        if pick and (state.blocked or state._need_more_review and state.turns + 2 > H.MAX_TURNS):
            H._release_pick(state, pick)
            pick = None
        if pick:
            H._ask_pick(state, pick)
            return _say(state, bank, llm, catalog)
    if intent == "show_result":
        state._need_more_review = False
        H._flag(state, "show_result")
    advance(state, bank)
    return _say(state, bank, llm, catalog)


def _post_result(state, text, bank, llm, catalog):
    """結果を出した後の発言(v4 の _post_result と同じ決まり。読む段は act で読む)。中学生以下で止めた人の年齢の訂正(はっきりした形だけ)と、
    やり直しのほかは相談に戻らない。"""
    v = H.view(state, bank)
    stored = {"reply": H.POST_RESULT_TEXT + "\n" + state.result["text"], "view": v, "done": True, "counted": False}
    if not H.nfkc(text):
        return stored
    crisis_open = H._take_crisis(state)
    if crisis_hit(text):
        return H._crisis(state, bank, done=True)
    if crisis_open and wants_resume(text):
        H.ev(state, "crisis_resume")
        return stored
    if H.DANGER_RE.search(H.nfkc(text)):
        H._flag(state, "danger_warned")
        H.ev(state, "danger_skip", where="post_result")
        return {"reply": H.NOTICE["danger"], "view": v, "done": True, "counted": False}
    exit_words, _, _ = _words()
    if text.strip() in exit_words or H.nfkc(text) in LATER_WORDS:  # 中学生以下で止めた後も「やめる」で閉じられる(v4 と同じ)
        return _close(state, bank, "exit" if text.strip() in exit_words else "later")
    reading = call_llm(llm.read, read_payload(state, bank, text), validator(text), _log(state), "read")
    if A.has(reading, "crisis"):
        return H._crisis(state, bank, done=True)
    if A.has(reading, "restart"):
        return _restart(state, bank, llm, catalog)
    if A.has(reading, "danger"):
        H._flag(state, "danger_warned")
        H.ev(state, "danger_skip", where="post_result")
        return {"reply": H.NOTICE["danger"], "view": v, "done": True, "counted": False}
    if state.blocked:
        band = H.typed_age(text)
        if band in ("15_17", "18_plus"):
            if state.turns < H.MAX_TURNS:
                H._count(state)
            H.apply_update(state, "age_band", band, "high", "free:post_result")
            state.result = None
            state.unresolved = {}
            state.review_rounds = 0
            state.last_card = None
            state.flags = [f for f in state.flags if f != "show_result"]
            state.phase = "hard_filter"
            H.ev(state, "unblocked", value=band)
            advance(state, bank)
            return _say(state, bank, llm, catalog)
    H.ev(state, "post_result_message", acts=[a["act"] for a in (reading or {}).get("acts", [])])
    return stored


def _restart(state, bank, llm, catalog):
    """最初から(v4 の _restart と同じ。層の覚えも新しくする)。"""
    restarts = state.restarts + 1
    user_text = state._user_text
    quick = H.QUICK_FLAG in state.flags and not (state.blocked or H.jhs_unresolved(state))
    fresh = new_state(quick=quick)
    fresh.restarts = restarts
    state.__dict__.clear()
    state.__dict__.update(fresh.__dict__)
    H._init_volatile(state, reset=True)
    _init_turn(state)
    state._user_text = user_text
    H.ev(state, "restart", n=restarts)
    if quick:
        return H._quick_reply(state, bank, H.QUICK_AGAIN_TEXT) | {"view": H.view(state, bank) | {"restarted": True}}
    advance(state, bank)
    return _say(state, bank, llm, catalog, counted=False, extra={"restarted": True})


def _close(state, bank, why):
    """「あとで」(later)と「やめる」(exit): 決まった文で閉じる(モデルを呼ばない。そこまでの確かめた値を保存するのは session)。"""
    state.closed = why
    H.ev(state, "closed", why=why)
    v = dict(H.view(state, bank), choices=[])
    v.pop("quick", None)
    return {"reply": ENTRY.texts["later" if why == "later" else "exit"], "view": v, "done": True, "counted": False, "closed": why}


# ----------------------------- 応じ方の計画(§4.6) -----------------------------
def _respond(state, bank, llm, catalog, q, p):
    """値も操作も無い発言への応じ方(計画の next)。聞き方への不満だけなら、今の欄を空のまま閉じて次へ(数える)。
    前の問いに戻る(go_back)は v5 では受けず、同じ問いを出し直す。"""
    if p.next == "skip" and state.current["mode"] in ASKING and q is not None:
        H.ev(state, "skipped_on_complaint", qid=q["id"])
        H._count(state)
        return _next(state, bank, llm, catalog, plan=p)
    if p.next == "back":
        p = P.Plan(ack="short", next="keep", count="free")
    return _aside(state, bank, llm, catalog, p)


def _danger(state, bank, llm, catalog):
    """闇バイトらしい話: 注意文(NOTICE の danger)と同じ問い。モデルを呼ばず、脱線と同じく 3 回まで数えない(v4 と同じ)。"""
    H._flag(state, "danger_warned")
    H.ev(state, "danger_skip")
    counted = H._count_aside(state)
    if counted and state.turns >= H.MAX_TURNS:
        advance(state, bank)
        return _say(state, bank, llm, catalog, counted=True, extra={"aside": "danger"})
    r = _represent(state, bank, counted=counted, lead=H.NOTICE["danger"], aside=True)
    r["view"]["aside"] = "danger"
    return r


def _aside(state, bank, llm, catalog, plan=None):
    """答えでない発言: 今のものを出し直す(v4 の _aside と同じ数え方。計画の count が free なら数えない)。答えでないターンが続いたときと、
    疲れたと言ったときは「中断」の注意を 1 回だけ添える。疲れたときは、この後 HARD のほかを聞かない(急ぐ)。"""
    p = plan or P.Plan(ack="light", next="keep", count="aside", offtrack=True)
    counted = H._count_aside(state) if p.count == "aside" else False
    if counted and state.turns >= H.MAX_TURNS:
        advance(state, bank)
        return _say(state, bank, llm, catalog, counted=True, extra={"aside": "aside"})
    if p.offtrack:
        state.offtrack += 1
    if p.hurry and HURRY not in state.flags:
        H._flag(state, HURRY)
        H.ev(state, "hurry")
    if P.pause_due(p, state.offtrack, PAUSE_FLAG in state.flags):
        p.notes = [*p.notes, "pause"]
        H._flag(state, PAUSE_FLAG)
    if state.current["mode"] not in LAYER_MODES:  # まとめと結果は v4 の返事(脱線の後に同じものを出し直す)
        return H._say(state, bank, llm, catalog, counted=counted, extra={"aside": "aside"}, aside=True)
    return _say(state, bank, llm, catalog, counted=counted, extra={"aside": "aside"}, aside=True, plan=p)


def _offer(state, q, p, choices, notes):
    """別の入り口を考えている(計画の offer): 移るボタンを今の問いのボタンの前に足し、移れることの注意を移る先ごとに相談で 1 回だけ添える。
    今の問いの候補は外さない(正社員の c2 と同じ)。"""
    offer = [s for s in ENTRY.switch if s["to"] in p.offer]
    for s in offer:
        flag = f"{SWITCH_FLAG}:{s['to']}"
        if flag not in state.flags:
            notes.append(s["note"])
            H._flag(state, flag)
    if not offer:
        return choices
    labels = [s["label"] for s in offer]
    return [*labels, *[c for c in choices if c not in labels]][:QUICK_MAX]


# ----------------------------- 入り口を移す(§4.5) -----------------------------
def _switch(state, bank, q, reading, text, to):
    """別の入り口へ移る(switch の want): 発言の中の値も当ててから閉じる(数えない。モデルは読む段のほかに呼ばない)。
    返す形の switch は {to, acts: 発言の中の値の act, said: 氏名と要配慮の部分を伏せた発言}。記録を書いて移るのは session。"""
    cur = state.current or {}
    if q is not None and cur.get("mode") in ASKING and reading is not None:
        _apply_plain(state, bank, q, _ages(state, cur, q, to_ext(reading, state), text, None), text)
    acts = [a for a in SW.value_acts(reading, lambda t: guard.scrub(t, state._spans)) if a.get("slot") != "age_band"]  # 年齢は渡さない
    state.closed = "switch"
    H.ev(state, "switch", to=to, acts=len(acts))
    v = dict(H.view(state, bank), choices=[])
    v.pop("quick", None)
    return {"reply": "", "view": v, "done": True, "counted": False,
            "switch": {"to": to, "acts": acts, "said": state._user_text}}


def handoff_slots(state):
    """引き継ぎの記録に残す欄(閉じたときの全部の値。low も、出どころつき)。値の字は区切りの表示名。年齢は正社員へ渡さない(§4.11)。
    近くで探すのエリアは near.area。"""
    out = {}
    for s, e in {**state.pending, **state.slots}.items():
        if s == "age_band" or not H.is_filled(e):
            continue
        out[s] = {"status": "filled", "text": H.value_ja(s, e["value"]), "value": copy.deepcopy(e["value"]),
                  "confidence": e.get("confidence") or "low", "from": e.get("from")}
    area = state.near.get("area")
    if H.NEAR_AREAS.get(area):
        out[NEAR_AREA] = {"status": "filled", "text": area, "value": area, "confidence": "high", "from": "near"}
    return out


def _carried_values(slot, e):
    """引き継いだ値 -> [(欄, 区切りのキー)]。前に引き継いだこの入り口の記録(prior)は値のまま。ほかの入り口の文は、区切りに読めるとき
    だけ(地名から都県を推さない。働ける時間の文は、日数と時間帯の両方を読む。数を作らない)。"""
    if e.get("value") is not None:
        return [(slot, e["value"])]
    text = H.nfkc(e.get("text") or "")
    if slot == NEAR_AREA:
        return [(slot, k) for k in sorted(VALUES[NEAR_AREA]) if k in text][:1]
    out = []
    for s in ((slot, "time_slots") if slot == "days_per_week" else (slot,)):
        v = H.normalize_value(s, text)
        if v is not None and v != H.NOPREF:
            out.append((s, v))
    return out


def _changed_by_rules(slot, value):
    """未成年の決まり(年齢が分からない間も当たる)で変わる値か: 深夜を含む時間帯、体力仕事もOK。引き継ぎと前の相談の値からは入れず、
    年齢を聞いた後に問いで聞く(黙って深夜を落とした値を、確かめた値として残さない)。"""
    return (slot == "time_slots" and isinstance(value, list) and "late_night" in value) or (slot == "physical" and value == "heavy_ok")


def take_handoff(state, rec):
    """ほかの入り口からの引き継ぎの記録(dip_mcp.convo.switch)の値を入れる -> 入れた欄。空の欄にだけ入れる(前の相談で保存した値を
    上書きしない)。同じ意味の値は元の確かさのまま、近い意味の値は推測(low)。どちらも聞き直さず、まとめで確かめる(_gap)。年齢は写さない。"""
    if not rec:
        return []
    got = []
    for slot, e in SW.carry(rec, ENTRY.entry, ENTRY.carry).items():
        for s, value in _carried_values(slot, e):
            if s == "age_band":
                continue
            if s == NEAR_AREA:
                if not state.near.get("area"):
                    state.near["area"] = value
                    got.append(s)
                continue
            if H.is_filled(state.slots.get(s)) or s in state.pending or _changed_by_rules(s, value):
                continue
            if H.apply_update(state, s, value, e.get("confidence") or "low", f"{CARRIED}{rec['from']}") in ("set", "pending"):
                got.append(s)
    state.came_from = {k: v for k, v in rec.items() if k not in ("prior", "said")}
    H.ev(state, "handoff", source=rec["from"], slots=got)
    return got


def _saved_text(slot, value):
    """欄の値を、希望の項目に保存するときの文にしたもの(session.result_preferences と同じ形)。"""
    return str(value) if slot == "hourly_wage_min" else H.value_ja(slot, value)


def prefill(state, prefs):
    """前の相談で保存した希望の項目(dip の stated_preferences)を入れる -> 入れた欄(§4.15 の「v5 では、始めるときに希望の項目を読む」)。
    保存した値は確かめた値なので high で入れ、聞かない(from は saved。閉じるときに書き戻さない)。入れるのは、区切りの値を保存の文に
    戻すと元の文と同じになるものだけ(「1300」を 1,200 円以上に、「朝、週末」を朝に丸めて、確かめた値にしない)。
    近くで探すのボタンに無いエリアも入れない。"""
    got = []
    for key, slot in PREFILL.items():
        v = str((prefs.get(key) or {}).get("value") or "").strip()
        nv = H.normalize_value(slot, v) if v else None
        if nv is None or nv == H.NOPREF or H.is_filled(state.slots.get(slot)) or _changed_by_rules(slot, nv) \
                or _saved_text(slot, nv) != v:
            continue
        if H.apply_update(state, slot, nv, "high", SAVED) == "set":
            got.append(slot)
    area = H.nfkc((prefs.get("area") or {}).get("value") or "")
    key = next((k for k, label in H.NEAR_AREAS.items() if label and label == area), None)
    if key and not state.near.get("area"):
        state.near["area"] = state.saved_area = key
        got.append(NEAR_AREA)
    return got


def ng_items(text):
    """避けたいことの保存の文を、NG の並びにする(session._finish が「、」でつないで保存する)。"""
    return [x for x in re.split(r"[、,，]\s*", text or "") if x]


def kept_texts(prefs):
    """「あとで」の続きで読み直す希望の項目(FEEDS の項目、エリア、避けたいこと)の、今の保存の文。"""
    return {k: (prefs.get(k) or {}).get("value") for k in (*FEEDS, "area", "avoids")}


def mark_saved(state, prefs):
    """「あとで」で閉じて保存した後(session。prefs は保存した後の希望の項目): 保存した項目を作った欄(high)と近くで探すのエリアを、
    前の相談で保存した値にする。本人の NG は、避けたいことに入ったもの(字数の上限で落ちたものは入れない)を saved_ng に覚える
    (続きから聞いて閉じるときに、答え直していない欄と保存した NG を書き戻さない。その間にプロフィール画面で直した・消した値を戻さないため)。"""
    for slots in FEEDS.values():
        for s in slots:
            e = state.slots.get(s)
            if H.is_filled(e) and e.get("confidence") == "high":
                e["from"] = SAVED
    if H.NEAR_AREAS.get(state.near.get("area")):
        state.saved_area = state.near["area"]
    stored = ng_items((prefs.get("avoids") or {}).get("value"))
    state.saved_ng = [n for n in state.user_ng if n in stored]


def refresh_saved(state, prefs, kept):
    """「あとで」の続きから始めるとき(と、続きをもう一度「あとで」で閉じる前。session._pause): 閉じた後にプロフィール画面で直した・
    消した項目(kept は閉じたとき、または続きを始めたときの保存の文)は、その項目を作った前の値(from が saved か derived の欄)を外し、
    今の値を読み直す(prefill。読めなければ後で聞く)。避けたいことを直した・消したら、保存した本人の NG(saved_ng)のうち今の保存の文に
    無いものを外す(まとめと結果に出さない。ルールの NG は年齢から作り直す)。読み直した文を state.kept に覚える。"""
    if (prefs.get("avoids") or {}).get("value") != kept.get("avoids"):
        now = ng_items((prefs.get("avoids") or {}).get("value"))
        gone = [n for n in state.saved_ng if n not in now]
        state.user_ng = [n for n in state.user_ng if n not in gone]
        state.saved_ng = [n for n in state.saved_ng if n in now]
    for key, slots in FEEDS.items():
        if (prefs.get(key) or {}).get("value") == kept.get(key):
            continue
        for s in slots:
            if str((state.slots.get(s) or {}).get("from", "")).startswith((SAVED, "derived")):
                del state.slots[s]
    if (prefs.get("area") or {}).get("value") != kept.get("area") and state.saved_area \
            and state.near.get("area") == state.saved_area:
        state.near.pop("area", None)
        state.saved_area = None
    prefill(state, prefs)
    state.kept = kept_texts(prefs)


def read_opening(state, bank, llm, said):
    """トークの AI が始めた相談(t1。§4.5)の、始めた発言を読む段で読む -> (値の act(引き継ぎの記録の形), 言う段に渡す発言, 守り)。
    まだ問いを出していないので、どの欄の値も、ほかの欄の値として読む。年齢は入れない(年齢は v4 の決まりで問いで聞く。_switch と同じ)。
    氏名と要配慮の部分は値から除き、言う段に渡す発言では伏せる。読めなければ値は無し(入口のカードから)。守りは、ふつうのターンと同じく、
    危ない言葉の表か闇バイトの言葉(DANGER_RE)に当たれば読む段を呼ばずに、読む段が crisis か danger と読んでも、"crisis" か "danger"
    (最初の問いの代わりに、窓口の文か注意文。opening_guard)。"""
    H._init_volatile(state)
    _init_turn(state)
    if not H.nfkc(said):
        return [], said, None
    if crisis_hit(said):
        return [], said, "crisis"
    if H.DANGER_RE.search(H.nfkc(said)):
        return [], said, "danger"
    reading = call_llm(llm.read, read_payload(state, bank, said), validator(said), _log(state), "read")
    spans = A.spans(reading)
    acts = [a for a in SW.value_acts(reading, lambda t: guard.scrub(t, spans)) if a.get("slot") != "age_band"]
    stop = "crisis" if A.has(reading, "crisis") else "danger" if A.has(reading, "danger") else None
    return ([] if stop == "danger" else acts), guard.mask(said, spans), stop


def opening_guard(state, bank, llm, stop):
    """始めた発言が危ない言葉か闇バイトの話だったときの最初の返事(ふつうのターンの守りと同じ)。最初の問い(入口のカードか年齢)を
    選んでおき、危ない言葉には窓口の文だけ(「続ける」でそこから)、闇バイトの話には注意文と最初の問いを返す。どちらも数えない。"""
    H._init_volatile(state)
    _init_turn(state)
    state._new_notices, state._discarded, state._snapped = [], [], []
    if state.current is None:
        advance(state, bank)
    if stop == "crisis":
        return H._crisis(state, bank)
    if state.phase in H.QUICK_PHASES:  # 入口では数えない(v4 と同じ)
        H._flag(state, "danger_warned")
        H.ev(state, "danger_skip", where=state.phase)
        return H._quick_reply(state, bank, H.NOTICE["danger"])
    return _danger(state, bank, llm, None)


def enter(state, bank=H.BANK, llm=None, said=""):
    """ほかの入り口から同じターンで移ってきた相談の、最初の返事。最初の問い(入口のカード。止めた印のある人は年齢)を、移ったことの
    相づち(入り口の定義の switched_in を計画の about に)と一緒に 1 つの吹き出しにする(言う段 1 回。数えない)。
    said は移る発言(伏せたもの)。言う段に渡し、直前のやりとりに覚える。"""
    H._init_volatile(state)
    _init_turn(state)
    state._new_notices = []
    state._user_text = said
    if state.current is None:
        advance(state, bank)
    out = _say(state, bank, llm, None, counted=False,
               plan=P.Plan(ack="done", about=[ENTRY.texts["switched_in"]], count="free"))
    if H.nfkc(said):
        CS.heard(state.convo, said)
    CS.replied(state.convo, out["reply"], state._ack)
    return out


# ----------------------------- 言う(§4.8) -----------------------------
def phrasings(q):
    """問いの言い方: 1 つ目は質問バンクの原文、続けて入り口の定義の variants(言い換えのある問いは PRD の alt_text)。"""
    return list(dict.fromkeys([q["text"], *VARIANTS.get(q["id"], [])]))


def _known(state):
    return any(str(e.get("from", "")).startswith((SAVED, CARRIED)) for e in state.slots.values() if e)


def _represent(state, bank, counted, lead=None, aside=False):
    """今のものを、モデルを呼ばずに出し直す。問いは層の組み立て(前置きの文を注意の場所に)、ほかは v4 の _represent。"""
    if (state.current or {}).get("mode") not in LAYER_MODES:
        return H._represent(state, bank, counted=counted, lead=[lead] if lead else None, aside=aside)
    return _say(state, bank, None, None, counted=counted, quiet=True, lead=lead, aside=aside)


def _baito_check(parts, v):
    """v4 の返事の決まりを、モデルが書いた文(相づちと答え)に当てる: 金額を言わない、入れていない答えと数を言わない、
    カードに無い職種を言わない。-> 問題か None。"""
    free = "".join(x for kind, x in parts if kind in ("ack", "answer"))
    if not free:
        return None
    if H.MONEY_RE.search(H.nfkc(free)):
        return "money_amount"
    if H._mentions_unkept(free, v.get("unkept")):
        return "unkept_mentioned"
    if H._says_unsaid(free, v.get("unsaid")):
        return "unsaid_value"
    if H._names_other_jobs(free, {"jobs": (v.get("quick") or {}).get("jobs") or []}):
        return "job_not_on_card"
    return None


def _say(state, bank, llm, catalog, counted=True, extra=None, aside=False, plan=None, quiet=False, lead=None):
    """言う段。問いと確かめの問いは、計画の部品を 1 つの吹き出しに組む(dip_mcp.convo.reply)。モデルに書かせるもの(相づちか逆質問の
    答え)があればモデルを呼び、返事を確かめる。だめなら 1 回やり直し、それでもだめなら決まった組み立て。まとめ、結果、入口のカードと
    近くで探すは v4 の言う段(計画 plan を渡された入口のカードは、移ってきた最初の返事なので層の組み立て)。"""
    cur = state.current
    mode = cur["mode"]
    if mode not in LAYER_MODES and not (mode in H.QUICK_PHASES and (plan is not None or quiet)):
        return H._say(state, bank, llm, catalog, counted=counted, extra=extra, aside=aside)
    p = plan or P.Plan(ack=None if mode == "clarify" else "short")
    v, rp = view(state, bank, p, aside=aside, quiet=quiet, lead=lead)
    if extra:
        v.update(extra)
    H.ev(state, "plan", **{k: x for k, x in p.to_dict().items() if k != "facts" and x})

    def _check(raw):
        parts, why = RP.validate(raw, rp, state.convo)
        if parts is not None:
            why = _baito_check(parts, v)
            parts = None if why else parts
        return parts, ([why] if why else [])
    parts = None
    if llm is not None and RP.needs_model(rp):
        parts = call_llm(llm.generate, v, _check, _log(state), "generate")
    if parts is None:
        parts = RP.fallback(rp, state.convo, ENTRY.tone["acks"])
    if mode in ASKING:
        cur["said"] = RP.ask_of(parts)  # 次に同じ問いを出し直すときは、この言い方を避ける
    state._ack = RP.ack_of(parts)
    return {"reply": RP.render(parts), "view": v, "done": False, "counted": counted, "parts": parts}


def view(state, bank, p, aside=False, quiet=False, lead=None):
    """言う段の入力(v4 の view に、計画とプロンプトの変数を足したもの)-> (view, 言う段の計画)。v4 の注意文と決まり文句(保留を捨てた、
    区切りに寄せた)は注意の部品。model_text と model_history は、言う段のモデルに渡す本人の発言と直前のやりとり(伏せたもの)。"""
    v = H.view(state, bank, aside=aside)
    cur = state.current
    mode = cur.get("mode")
    q = _q(state, bank)
    notes = [*v["notices"], *H._discard_lead(state), *H._snap_lead(state)]
    kind, answer = p.ack, None
    if mode in ASKING and q is not None and mode != "clarify":
        ps = phrasings(q)
        again = getattr(state, "_retry", False) or mode == "reask" or cur.get("qid") == getattr(state, "_qid_before", None)
        text, allowed = RP.pick(ps, (cur.get("said") or ps[0]) if again else None)
        if quiet:  # 決まった出し直し(「続ける」など): 脱線から戻るのではないので、前と同じ言い方
            text = cur.get("said") if cur.get("said") in ps else ps[0]
            allowed = [text]
        if getattr(state, "_retry", False):  # 読めなかった答え: 責めずに、押すだけで答えられることを添えて、違う言い方で
            notes.append(ENTRY.texts["retry"])
            kind = None
    else:  # 聞き返しの言い換え、区分の確かめ、境目の二択、入口のカード: v4 の文のまま
        text = v.get("text") or ""
        allowed = [text]
        if mode == "clarify" and getattr(state, "_clarify", False):
            kind = None
    ask = {"qid": cur.get("qid"), "mode": mode, "text": text, "phrasings": allowed}
    if p.answer:  # 逆質問: 渡す事実だけで答える。事実が無ければ決まった文
        p.facts = F.pick(ENTRY.facts, p.answer, where=state.phase, known=_known(state))
        answer = {"facts": p.facts, "fallback": ENTRY.texts["no_answer"]}
    notes += [ENTRY.texts[f"note_{n}"] for n in p.notes]
    v["choices"] = _offer(state, q, p, v["choices"], notes) if mode in ASKING else v["choices"]
    reading, button = getattr(state, "_reading", None), getattr(state, "_button", None)
    hook = (reading or {}).get("hook") or (button if button and len(button) <= A.HOOK_MAX else None)
    hidden = guard.hidden(getattr(state, "_spans", []))
    if quiet:  # モデルを呼ばない出し直し: 前置きの文、v4 の注意文と問いだけ
        kind, answer = None, None
        notes = [*([lead] if lead else []), *[n for n in notes if n != lead]]
    rp = RP.new_plan(ask, kind, hook, answer, notes, [], v["choices"], state._user_text, hidden, p.about)
    convo = state.convo
    ack = ACK_JA[rp["ack"]] + (f"({'。'.join(p.about)})" if rp["ack"] == "done" and p.about else "")
    v |= {"text": ask["text"], "user_text": state._user_text, "model_text": state._user_text, "model_history": CS.messages(convo),
          "entry": ENTRY.title, "tone": ENTRY.tone["style"], "examples": " / ".join(ENTRY.tone.get("examples") or []) or "(なし)",
          "hook": rp["hook"] or "(なし)", "ack": ack, "ack_max": rp["ack_max"],
          "plan": json.dumps(p.for_say() | {"ack": rp["ack"]}, ensure_ascii=False),
          "answer": (f"答える。使ってよい事実(これだけ。無いことは「答えられない」と言う): {rp['facts']}" if rp["answer"]
                     else f"書かない(システムが「{rp['fixed_answer']}」と答える)" if rp["fixed_answer"] else "しない"),
          "recent_acks": " / ".join(a for a in convo.get("acks", []) if a) or "(なし)",
          "last_reply": ((convo.get("replies") or [""])[-1] or "(なし)").replace("\n", " / "),
          "lines": " / ".join(notes) or "(なし)",
          "next_question": json.dumps({"id": ask["qid"], "mode": v["mode"], "text": ask["text"], "phrasings": ask["phrasings"]},
                                      ensure_ascii=False)}
    return v, rp
