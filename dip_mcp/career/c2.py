"""正社員ヒアリングの c2(CAREER_HARNESS_VERSION=c2)。会話の層(dip_mcp/convo/)に載せ替えていく版。

設計は docs/design/conversation-layer.md(§5 の 2 枚目から 5 枚目まで)と ADR-0011。c1(dip_mcp/career/harness.py)は変えずに残し、
この版は c1 の予算、まとめ、結果をそのまま使う。c1 と違うのは次のところ:

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
- ボタンから推せるほかの欄(§4.7 の 1、§4.4): 入り口の定義の infer と、質問バンクの choice_effects のうち問いの欄でない欄。
  どちらも空の欄にだけ推測(low)で入れ、聞かずにまとめで確かめる(c1 は効き目のどの欄にも問いのフェーズの確かさを使う)
- 問いの選び方(§4.7。dip_mcp.convo.pick): フェーズの順と上限(ORDER、PHASE_CAP)をやめ、全部の問いから
  スコア(c1 の優先度 + 今の話題のおまけ)のいちばん高いものを選ぶ。同点は群(入り口の定義の groups)の順、次にバンクの並び。
  区切りの 1 行は、問いの群が変わったときに出す(群の line)。予算(can_ask)、最初の問い、まとめは c1 のまま
- 本人の言葉の候補(§4.7): 職歴の仕事、活かしたい経験、続けたいことの問い(入り口の定義の mention_questions)は、本人がこの会話で
  言った言葉(読む段の mentions)を最初のページの頭に 1 つ足す。押せば、その言葉が問いの欄の値になる
- 場面の二択に「どっちでも」(打った pass。§4.4): その欄を「こだわりなし」(推測)にし、同じ欄の二択を続けない
- 応じ方の計画(§4.6。dip_mcp.convo.plan): 読んだ act と当てた結果から、返事の部品(相づちの種類、逆質問の答えの事実、注意の 1 行)と、
  次の問い(選び直す、同じ問い、前の問い、今の欄を閉じて次へ)と、数え方を決める。逆質問には事実のデータ(facts.json と
  dip_mcp/convo/facts.json)だけを渡し、そのターンの吹き出しは 120 字まで。注意の文は入り口の定義の texts をハーネスがそのまま入れる
  (相づちの後、区切りの前)
- 脱線と急ぐ(§4.6): 答えでないターンが 2 回続いたら、3 回目に「中断」の注意を添える。疲れたと言ったら同じ注意を添え、HARD のほかを
  聞かずにまとめへ進む(H.HURRY)。どちらも相談で 1 回だけ。前の問いに戻る(go_back)は、前の問いを直す問いとして出し(数えない)、
  答えたら元の問いに戻る。聞き方への不満だけのターンは、今の欄を空のまま閉じて次へ(数える)。事実と違う頼みは、決まった文で断る(数えない)
- LINE のスタンプ(§4.12): ランタイムが「[スタンプ]」の文にして渡す。読む段を呼ばずに雑談として読む
- 入り口を移す(§4.5。dip_mcp.convo.switch): 「とりあえずバイトでいい」(読む段の switch の want)は、発言の中の値も当ててから
  相談を閉じる(返す形の switch。引き継ぎの記録を書いて移るのは session)。移る強い頼み(session の EXIT_REQUEST。打ち消しと後回しの
  EXIT_HOLD を除く)と移るボタンの文は、読む段を呼ばずに want にする。「バイトも考えてる」(consider)は移らずに、移るボタンを今の問いの
  ボタンの前に足し、入り口の定義の注意を 1 回だけ添える(数えない)。ほかの入り口から移ってきた相談は、引き継ぎの記録の値を入れ
  (take_handoff。同じ意味は元の確かさのまま、近い意味は推測)、最初の問いを移ったことの相づちと一緒に出す(enter)
- 言う段(§4.8。dip_mcp.convo.reply): 計画を、返事の部品(相づち → 答え → 注意 → 区切り → 問い)の並びに組む。プロンプトは層の骨組み
  dip_mcp/prompts/convo/say_ja.txt に、計画と入り口の定義の調子(tone)を差し込む。モデルが書くのは相づちと逆質問の答えだけで、問いの文は
  質問バンクの言い方(text と variants)から選ぶ。同じ問いを出し直すときは、前に使った言い方を避ける。返事がだめなら 1 回やり直し、
  それでもだめなら決まった組み立て(入り口の定義の相づちを順に回す)。相づちも答えも要らないターン(聞き直し、聞き返しの説明、事実と違う頼み)は
  モデルを呼ばない。「読み取れなかった」とは言わない(聞き直しの注意は入り口の定義の texts)。「他の候補」の前置きも texts から作る
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from dip_mcp.baito.session import EXIT_HOLD
from dip_mcp.career import harness as H
from dip_mcp.convo import acts as A
from dip_mcp.convo import entry as E
from dip_mcp.convo import facts as F
from dip_mcp.convo import guard
from dip_mcp.convo import pick as PK
from dip_mcp.convo import plan as P
from dip_mcp.convo import reply as RP
from dip_mcp.convo import state as CS
from dip_mcp.convo import switch as SW
from dip_mcp.convo import values as V
from dip_mcp.convo.turn import call_llm
from dip_mcp.convo.turn import render_prompt as _render

VERSION = "c2"
ENTRIES = ("career", "baito")  # 入り口の名前(移る先の確かめ)
TEXTS = ("crisis_lead", "crisis_tail", "crisis_resume", "retry", "retry_number", "retry_review", "no_answer", "more_lead",
         "more_lead_number", "more_done", "switched_in", *(f"note_{n}" for n in P.NOTES))
# 言う段のプロンプトに渡す、相づちの種類の説明(応じ方の計画の ack。dip_mcp.convo.plan.ACKS)
ACK_JA = {"short": "本人の答えを受け取る、短い相づち", "done": "何をしたか(計画の about)を、1 文で短く言う",
          "light": "雑談に軽く 1 文で乗る",
          "empathy": "気持ちを受け止める(励まし、根拠の無い約束、助言をしない。人や会社を評価しない。強い言葉をなぞらない)",
          "apology": "聞き方への不満に、短く詫びる(かしこまった謝罪の決まり文句にしない)", None: "付けない"}
ENTRY = E.load(H.HERE / "entry.json", H.ALL_SLOTS, ENTRIES, TEXTS)
CRISIS_TEXT = guard.crisis_text(ENTRY.texts["crisis_lead"], ENTRY.texts["crisis_tail"])
CRISIS_FLAG = "crisis_shown"  # 前のターンで窓口を案内した印(1 ターンだけ残す)
PAUSE_FLAG = "pause_noted"  # 「中断」の注意を添えた印(相談で 1 回だけ)
SWITCH_FLAG = "switch_noted"  # 移れることの注意を添えた印(移る先ごとに、相談で 1 回だけ。「switch_noted:baito」)
TARGETS = tuple(s["to"] for s in ENTRY.switch)  # 移れる入り口
READ_PROMPTS = [H.PROMPT_DIR.parent / "convo" / "read_ja.txt", H.PROMPT_DIR / "prompt_read_ja.txt"]
SAY_PROMPTS = [H.PROMPT_DIR.parent / "convo" / "say_ja.txt"]
ANSWER_BUBBLE_MAX = RP.ANSWER_BUBBLE_MAX  # 逆質問に答えるターンの吹き出しの字数(§4.8、§6 の 20)
ASKING = ("ask", "retry", "clarify")  # 問いを出している(前の問いに戻れる、不満で閉じられる。層の組み立てで出す問いの mode)


@dataclass
class State(H.State):
    version: str = VERSION
    convo: dict = field(default_factory=CS.new)  # 層の覚え(直前のやりとりと返信。dip_mcp.convo.state)
    offtrack: int = 0  # 続いた答えでないターンの数(計画の offtrack。答えて次へ進むと 0)
    came_from: dict = None  # ほかの入り口から移ってきたときの引き継ぎの記録(すぐ戻るときに、元の入り口の値を戻すため。§4.5)


def new_state(form=None, prefill=None):
    """空の状態(c1 の new_state と同じ。版と層の覚えだけが違う)。"""
    return State.from_dict(H.new_state(form, prefill).to_dict() | {"version": VERSION})


def render_prompt(stage, variables):
    """読む段(read)は層の骨組みに正社員の決まりをつないだもの。言う段(say)は層の骨組みに、計画と入り口の調子を差し込んだもの。"""
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
    """最初の問い(c1 の start と同じ。問いはこの版の advance で選ぶ)。初めの 1 回だけ、読む時間を数え、出した文を直前のやりとりに覚える。"""
    H._init_volatile(state)
    first = state.current is None
    if first and state.form in H.UI_FORMS:  # 形 D: 先に「始める」の 1 枚
        state.current = {"qid": "start", "mode": "start", "choices": [H.START_GO, H.LATER]}
    elif first:
        advance(state, bank)
    lead = (H.START_LEAD_UI if state.form in H.UI_FORMS else H.START_LEAD) if state.turns == 0 else None
    out = H._represent(state, bank, counted=False, lead=lead, read=False)
    if first:
        state.seconds += H.read_seconds(out["bubbles"] + out["choices"])
        CS.replied(state.convo, out["reply"])
    return out


# ----------------------------- 問いの選び方(§4.7) -----------------------------
def pick_question(state, bank, budget=True):
    """まだ出していない全部の問いから、スコア(c1 の優先度 + 今の話題のおまけ)のいちばん高いものを選ぶ。同点は群の順、次にバンクの並び。"""
    touched = getattr(state, "_topics", set())
    scored = []
    for i, q in enumerate(H.questions(state, bank)):
        if q["phase"] not in H.PRE_REVIEW or q["id"] in state.asked:
            continue
        s = H.score(state, q, budget)
        if s > 0:
            s += PK.bonus(q["targets"], touched, ENTRY.topics)
        scored.append((s, PK.group_index(q["phase"], ENTRY.groups), i, q))
    return PK.best(scored)


def advance(state, bank):
    """次に出すものを決めて state.current に入れる(c1 の advance の、フェーズの順と上限を群とスコアに替えたもの)。
    state.phase は出した問いのフェーズまで進め、戻さない(後から segment が決まって出す HARD は、c1 と同じく今のフェーズのまま)。
    出す問いが無いか、ターンの上限か「まとめて」なら、まとめ(と結果)へ。"""
    if state.phase in H.PRE_REVIEW and state.turns < H.MAX_TURNS and "show_result" not in state.flags:
        q = pick_question(state, bank)
        if q is not None:
            if H.ORDER.index(q["phase"]) > H.ORDER.index(state.phase):
                state.phase = q["phase"]
            _present(state, q)
            return
        state.phase = "review"
    else:
        state.phase = H.next_phase(state)
    if state.phase == "review":
        if state.stop_reason is None:
            state.stop_reason = H._why_stop(state, bank)
            H.ev(state, "stop", reason=state.stop_reason, seconds=round(state.seconds, 1))
        state.last_card = H.build_card(state)
        state.current = {"qid": "review", "mode": "review", "choices": [H.REVIEW_OK]}
    else:
        state.current = {"qid": "result", "mode": "result", "choices": []}


def _template(q):
    """職歴カードの問いはひな形の id(card_role1 -> card_role)。"""
    return q["id"][: -len(str(q["card"]))] if q.get("card") else q["id"]


def _with_word(q, word):
    """本人の言葉を最初のページの頭に置いた問い。ボタンの決まりに入らなければ None。"""
    pages = PK.put_first(q["pages"], word, H.page_choices(q, [], "ask"), H.chars, H.LABELS_MAX, H.QUICK_MAX, H.LABEL_MAX)
    return H._with_choices(dict(q, pages=pages)) if pages else None


def _mention(state, q):
    """問いに足す本人の言葉 -> (言葉, 足した問い)。新しい言葉から。同じ種類の欄(職歴の仕事なら、ほかの職歴の仕事)の値になった言葉は使わない。"""
    if _template(q) not in ENTRY.mention_questions:
        return None, None
    fields = {t.split(".")[-1] for t in q["targets"]}
    taken = {H.nfkc(e.get("text") or "") for s, e in state.slots.items() if e and s.split(".")[-1] in fields}
    for word in reversed(state.convo.get("mentions", [])):
        if word in taken:
            continue
        got = _with_word(q, word)
        if got is not None:
            return word, got
    return None, None


def _present(state, q):
    """問いを出す(c1 の _present)。本人の言葉の候補を足したときは、その言葉を state.current に覚える(_q が同じ問いを作り直す)。"""
    word, got = _mention(state, q)
    H._present(state, got or q)
    if word:
        state.current["mention"] = word
        H.ev(state, "mention", qid=q["id"], word=word)


def _q(state, bank):
    """今出している問い(本人の言葉の候補を足したなら、足した形)。"""
    cur = state.current or {}
    q = H.qmap(state, bank).get(cur.get("qid"))
    if q is not None and cur.get("mention"):
        q = _with_word(q, cur["mention"]) or q
    return q


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
    """そのターン限りの層の値: 氏名と要配慮の部分、値を言った話題、読んだもの、押したボタン、返事の相づち、聞き直したか、
    聞き返しに説明したか、ターンの始めの問い。"""
    state._spans, state._topics, state._reading, state._button, state._ack = [], set(), None, None, None
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
        advance(state, bank)
    if state.closed or state.result:
        return H._stored(state)
    if not H.nfkc(text):
        H.ev(state, "empty_input")
        return _represent(state, bank, counted=False)
    state.said.append(text)
    cur = state.current
    q = _q(state, bank)
    button = match_button(state, text)
    button = H.LEGACY_BUTTONS.get(button, button)
    state._button = button
    fix = H.fix_targets(state).get(H.nfkc(text))
    sticker = button is None and fix is None and H.nfkc(text) == A.STICKER
    state.seconds += H.BUTTON_SECONDS if button or fix or sticker else H.type_seconds(text)
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
        advance(state, bank)
        if button is not None:
            H.ev(state, "started")
            return _represent(state, bank, counted=False)
        cur = state.current
        q = _q(state, bank)
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
    if sticker:  # スタンプは雑談(読む段を呼ばない)
        reading = A.sticker_reading()
    elif button is None and (to := asked_to_switch(state, text)):  # 移るボタンか、移る強い頼み(読む段を呼ばない)
        reading = SW.want_reading(to)
    elif button is None:
        reading = call_llm(llm.read, read_payload(state, bank, text), validator(text), _log(state), "read")
        state._spans = A.spans(reading)
        state._user_text = guard.mask(text, state._spans)  # 言う段に、氏名と要配慮の部分を渡さない
    state._reading = reading
    if A.has(reading, "crisis"):  # 表に無い危ない言い方(§6 の 15)。読む段の 1 回のほかは呼ばない
        return _crisis(state)
    if reading is not None:  # 候補に使える本人の言葉を覚える(氏名・要配慮の部分と伏せた印を含むものは覚えない)
        CS.mentioned(state.convo, [w for w in reading["mentions"] if guard.scrub(w, state._spans) == w])
    to = SW.wanted(reading, TARGETS)
    if to:  # 別の入り口へ移る(§4.5)
        return _switch(state, q, reading, text, to)
    if A.has(reading, "later", "stop"):
        if q is not None and cur["mode"] != "review":
            _apply(state, q, reading, text, retry=False)  # 「あとで」と一緒に打った答えも残す(次は聞かない)
        return H._close(state, "later")
    if talk_only(reading):  # 雑談、逆質問、感情、不満、前の問いに戻る、別の入り口を考えているなど、値も操作も無い発言: 応じ方の計画のとおり
        return _respond(state, bank, llm, q, P.respond(reading, entries=TARGETS))
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
    if cur["mode"] == "back":
        return _back(state, bank, llm, q, button, reading, text)
    if cur["mode"] == "review":
        return _review(state, bank, llm, button, reading, text)
    return _ask_turn(state, bank, llm, q, button, reading, text)


TALK_ONLY = {*A.TALK, "emotion", "switch", "go_back", "restart", "danger", *A.SPAN_ACTS}  # 値でも操作でもない act


def talk_only(reading):
    """値も操作も無い発言か(act が 1 つ以上あり、どれも話・感情・入り口・守りの部分)。移る(want)は、ここの前に受け持つ。"""
    names = [a["act"] for a in (reading or {}).get("acts", [])]
    return bool(names) and all(n in TALK_ONLY for n in names)


def _respond(state, bank, llm, q, p):
    """値も操作も無い発言への応じ方(計画の next)。前の問いに戻る、不満で今の欄を閉じるのは、問いを出しているときだけ。"""
    if state.current["mode"] in ASKING and q is not None:
        if p.next == "back":
            return _go_back(state, bank, llm, q, p)
        if p.next == "skip":  # 聞き方への不満だけ: 今の欄を空のまま閉じて次へ(数える。まとめで「未定」と出る)
            H.ev(state, "skipped_on_complaint", qid=q["id"])
            return _next(state, bank, llm, q, plan=p)
    return _aside(state, bank, llm, p)


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
            _apply_choice(state, q, button)
        return _next(state, bank, llm, q, before)
    got = _apply(state, q, reading, text)
    done = P.respond(reading, answered=True, entries=TARGETS)
    if got["answered"]:
        return _next(state, bank, llm, q, before, plan=done)
    if any(o["op"] == "escape" for o in ops):
        label = H.DONT_KNOW if A.has(reading, "dont_know") and H.DONT_KNOW in q.get("escape", []) else H.PASS
        H._apply_escape(state, q, label)
        if label == H.PASS and q["phase"] not in H.DIRECT:
            _no_pref(state, q)
        return _next(state, bank, llm, q, before, plan=done)
    if A.has(reading, "show_result"):
        H._flag(state, "show_result")
        return _next(state, bank, llm, q, before, plan=done)
    if got["retry"]:  # 数の問いで数が読めない 1 回目: 数えずに、ボタンで聞き直す(c1 と同じ)
        return _retry(state, bank, llm, q)
    if got["fixed"] and not got["added"]:  # 直した・取り消した: 数えずに、今の問いを出し直す(§4.6)
        p = P.respond(reading, fixed=True, entries=TARGETS)
        p.about = got["did"]
        return _say(state, bank, llm, counted=False, aside=True, plan=p)
    if got["added"] or (reading and reading["acts"]):  # ほかの欄の値だけ(雑談や逆質問と一緒でも): 同じ問いを出し直す
        return _aside(state, bank, llm, P.respond(reading, entries=TARGETS))
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


def _next(state, bank, llm, q, before=None, skipped=None, plan=None):
    """数えて、次の問いへ進む(c1 の _turn の後半と同じ)。答えでないターンの続きは 0 に戻す。"""
    state.offtrack = 0
    H._count(state, q["phase"])
    phase_before = state.phase
    before = before if before is not None else dict(state.slots)
    state._topics = PK.touched_topics(_stated(state, before), ENTRY.topics)
    advance(state, bank)
    if state.form != "B":
        _progress(state, before, phase_before)
    if skipped:
        state._progress = [skipped, *state._progress][-2:]
    return _say(state, bank, llm, plan=plan)


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
    advance(state, bank)
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
    got = {"answered": False, "added": False, "fixed": False, "retry": False, "did": []}
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
            got["did"].append(f"{H.SLOTS[slot]['label']}を取り消しました")
            continue
        old = state.slots.get(slot) if slot in pri else state.pending.get(slot)
        if op == "set":
            if _choice_ok(q, a, said):
                _apply_choice(state, q, a["choice"], text=o["text"], src="free")
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
            got["did"].append(f"{H.SLOTS[slot]['label']}を「{o['text']}」に直しました")
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


def _own(q, key):
    """効き目の欄が、問いの欄か区分・避けたいこと(問いそのものの答え)か。"""
    return key in q["targets"] or key.startswith("__")


def _apply_choice(state, q, label, text=None, src="button", infer=True):
    """候補の効き目を当てる(§4.4、§4.7 の 1)。問いの欄への効き目は c1 の _apply_choice のまま(問いのフェーズの確かさ)。
    ほかの欄への効き目(質問バンクの choice_effects と入り口の定義の infer)は、infer=True のとき、空の欄にだけ推測(low)で入れる。
    効き目がほかの欄にしか無い候補も、ラベルが問いの欄の値になる(候補はどれも問いの欄の値になる)。"""
    effects = q["choice_effects"].get(label)
    if effects is not None and any(not _own(q, k) for k in effects):
        own = {k: v for k, v in effects.items() if _own(q, k)}
        rest = {c: e for c, e in q["choice_effects"].items() if c != label}
        q = dict(q, choice_effects=rest | ({label: own} if own else {}))
    H._apply_choice(state, q, label, text=text, src=src)
    if infer:
        _infer(state, q, label, {k: v for k, v in (effects or {}).items() if not _own(q, k)})


def _infer(state, q, label, effects):
    """ボタンから推せるほかの欄を、空のときだけ推測(low)で入れる。effects は質問バンクの、問いの欄でない欄への効き目。
    入り口の定義の infer は {欄: 値の文}。"""
    frm = f"infer:{q['id']}"
    for slot, text in (ENTRY.infer.get(q["id"]) or {}).get(label, {}).items():
        effects = effects | {slot: {"text": text}}
    for slot, eff in effects.items():
        if slot not in H.ALL_SLOTS or not H.is_open(state.slots.get(slot)) or slot in state.pending:
            continue
        if "value" in eff:
            v = eff["value"]
            ja = "・".join(H.SLOTS[slot]["values"].get(x, x) for x in v) if isinstance(v, list) else H.SLOTS[slot]["values"].get(v, v)
            e = H.entry("filled", ja, v, confidence="low", frm=frm)
        elif eff.get("nopref"):
            e = H.entry("filled", label, H.NOPREF, confidence="low", frm=frm)
        else:
            e = H.entry("filled", eff.get("text") or label, None if "text" in eff else label, rng=eff.get("range"),
                        unit=eff.get("unit"), confidence="low", frm=frm)
        H.set_slot(state, slot, e)


def _no_pref(state, q):
    """場面の二択に「どっちでも」と打った(pass): 空の欄を「こだわりなし」(推測)にし、同じ欄の二択を続けない(§4.4)。
    条件の欄でない欄は、c1 のパスと同じく値なし(まとめで「スキップ」)。"""
    pri = H.active_slots(state)
    for t in q["targets"]:
        if t in pri and H.is_open(state.slots.get(t)):
            H.set_slot(state, t, H.entry("passed", H.PASS, H.NOPREF if t in H.CONDITION_SLOTS else None, confidence="low",
                                         frm=f"pass:{q['id']}"))


def _stated(state, before):
    """このターンに本人が値を言った欄(推したほかの欄とパスを除く)。話題のおまけ(§4.7)に使う。"""
    return [s for s, e in state.slots.items() if e != before.get(s) and e.get("status") == "filled"
            and not str(e.get("from", "")).startswith("infer:")]


def _progress(state, before, phase_before):
    """形 C の「✓職歴」の行(c1 の _progress)と、問いの群が変わったときの区切りの 1 行(入り口の定義の群の line。§4.7)。"""
    H._progress(state, before, state.phase)  # 職歴の行だけ(フェーズは変わっていないとして、c1 の区切りの行を出さない)
    g = PK.group_index(state.phase, ENTRY.groups)
    if g != PK.group_index(phase_before, ENTRY.groups) and g < len(ENTRY.groups) and ENTRY.groups[g].get("line"):
        state._progress = [*state._progress, ENTRY.groups[g]["line"]][-2:]


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
        _apply_choice(state, q, button, infer=False)
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


# ----------------------------- 入り口を移す(§4.5) -----------------------------
def _requests():
    """移る強い頼み {移る先: 正規表現}(今の EXIT_REQUEST。dip_mcp.career.session が持ち、session がこのモジュールを読み込むので、
    使うときに読む)。"""
    from dip_mcp.career.session import EXIT_REQUEST

    return {"baito": EXIT_REQUEST}


def asked_to_switch(state, text):
    """読む段を呼ばずに移る先が決まる発言 -> 移る先か None。移るボタンの文(入り口の定義の label)か、まとめの前の移る強い頼み
    (打ち消しと後回しの EXIT_HOLD に当たるものは除く。「バイトじゃなくて」は答えとして読む)。"""
    t = H.nfkc(text)
    for s in ENTRY.switch:
        if t == H.nfkc(s["label"]):
            return s["to"]
    if state.phase in ("review", "result"):
        return None
    return next((to for to, rx in _requests().items() if to in TARGETS and SW.requested(text, rx, EXIT_HOLD)), None)


def _switch(state, q, reading, text, to):
    """別の入り口へ移る(switch の want): 発言の中の値も当ててから、相談を閉じる(数えない。モデルは読む段のほかに呼ばない)。
    返す形の switch は {to, acts: 発言の中の値の act, said: 氏名と要配慮の部分を伏せた発言}。引き継ぎの記録を書き、移る先へ渡すのは
    session(dip_mcp.career.session.turn)。"""
    if q is not None and state.current["mode"] in ASKING:
        _apply(state, q, reading, text, retry=False)  # 「とりあえずバイトでいい。週3くらいで」の週 3 も捨てない(§4.4)
    acts = SW.value_acts(reading, lambda t: guard.scrub(t, state._spans))
    state.closed, state.phase = "switch", "result"
    state.current = {"qid": "closed", "mode": "closed", "choices": []}
    H.ev(state, "switch", to=to, acts=len(acts))
    return H._out(state, [], [], counted=False, read=False) | {"switch": {"to": to, "acts": acts, "said": state._user_text}}


def take_handoff(state, rec):
    """ほかの入り口からの引き継ぎの記録(dip_mcp.convo.switch)の値を入れる -> 入れた欄。空の欄にだけ入れる(前の相談で確かめて
    保存した値と履歴書の値を上書きしない)。同じ意味の値は元の確かさのまま、近い意味の値は推測(low)。どちらも聞き直さず、
    low はまとめで確かめる(HARD でも。spam_hard_high の例外)。記録は、すぐ元の入り口へ戻るときのために覚える(came_from)。"""
    if not rec:
        return []
    got = []
    for slot, e in SW.carry(rec, ENTRY.entry, ENTRY.carry).items():
        if slot not in H.ALL_SLOTS or not H.is_open(state.slots.get(slot)) or slot in state.pending:
            continue
        H.set_slot(state, slot, H.entry(e.get("status") or "filled", e["text"], e.get("value"), e.get("number"), e.get("range"),
                                        e.get("unit"), e.get("confidence") or "low", e.get("from") or rec["from"]))
        got.append(slot)
    state.came_from = {k: v for k, v in rec.items() if k not in ("prior", "said")}
    H.ev(state, "handoff", source=rec["from"], slots=got)
    return got


def read_opening(state, bank, llm, said):
    """トークの AI が始めた相談(t1。§4.5)の、始めた発言を読む段で読む -> (値の act(引き継ぎの記録の形), 言う段に渡す発言, 守り)。
    まだ問いを出していないので、どの欄の値も、ほかの欄の値として読む。氏名と要配慮の部分は値から除き、言う段に渡す発言では伏せる。
    読めなければ値は無し(最初の問いから聞く)。守りは、危ない言葉の表に当たるか(読む段を呼ばない)、読む段が crisis と読んだら "crisis"
    (ふつうのターンと同じく、最初の問いの代わりに窓口の文を返す。opening_guard)。"""
    H._init_volatile(state)
    _init_turn(state)
    if not H.nfkc(said):
        return [], said, None
    if guard.crisis_hit(said):
        return [], said, "crisis"
    reading = call_llm(llm.read, read_payload(state, bank, said), validator(said), _log(state), "read")
    spans = A.spans(reading)
    acts = SW.value_acts(reading, lambda t: guard.scrub(t, spans))
    return acts, guard.mask(said, spans), "crisis" if A.has(reading, "crisis") else None


def opening_guard(state, bank, stop):
    """始めた発言が危ない言葉だったときの最初の返事: 最初の問いを選んでおき(「続ける」でそこから)、窓口の文だけを返す(数えない)。"""
    H._init_volatile(state)
    _init_turn(state)
    if state.current is None:
        advance(state, bank)
    return _crisis(state)


def enter(state, bank=H.BANK, llm=None, said=""):
    """ほかの入り口から同じターンで移ってきた相談の、最初の返事。引き継いだ値で埋まった問いを飛ばして最初の問いを選び、
    移ったことの相づち(入り口の定義の switched_in を計画の about に)と問いを 1 つの吹き出しにする(言う段 1 回。数えない)。
    形 D でも「始める」の 1 枚は出さない(本人はもう話している)。said は移る発言(伏せたもの)。言う段に渡し、直前のやりとりに覚える。"""
    H._init_volatile(state)
    _init_turn(state)
    state._user_text = said
    advance(state, bank)
    out = _say(state, bank, llm, counted=False, plan=P.Plan(ack="done", about=[ENTRY.texts["switched_in"]], count="free"))
    if H.nfkc(said):
        CS.heard(state.convo, said)
    CS.replied(state.convo, out["reply"], state._ack)
    return out


def _offer(state, q, p, choices, notes):
    """別の入り口を考えている(計画の offer): 移るボタンを今の問いのボタンの前に足し、移れることの注意を移る先ごとに相談で 1 回だけ
    添える。-> ボタン。今の問いの候補は外さない(1 回押せば答えられるように。§4.6)ので、ラベルの合計の字数(LABELS_MAX)は、
    まとめの「直す」の「そのまま」と同じく、移るボタンの分だけ超えてよい。ボタンの数(QUICK_MAX)は超えない(超えるなら候補の後ろを外す)。"""
    offer = [s for s in ENTRY.switch if s["to"] in p.offer]
    for s in offer:
        flag = f"{SWITCH_FLAG}:{s['to']}"
        if flag not in state.flags:
            notes.append(s["note"])
            H._flag(state, flag)
    if not offer:
        return choices
    labels = [s["label"] for s in offer]
    cands = [c for c in choices if q is not None and c in q["choices"]]
    tail = [c for c in choices if c not in cands]
    return [*labels, *cands[:max(1, H.QUICK_MAX - len(labels) - len(tail))], *tail]


# ----------------------------- 前の問いに戻る -----------------------------
def _slot_of(q):
    return next((t for t in q["targets"] if t in H.ALL_SLOTS), None)


def _previous(state, bank):
    """直前に出した問い(今の問いのほか。区分と避けたいことの問いは戻らない)。"""
    qs = H.qmap(state, bank)
    for qid in reversed(state.asked):
        pq = qs.get(qid)
        if pq is not None and qid != state.current["qid"] and _slot_of(pq):
            return pq
    return None


def _go_back(state, bank, llm, q, p):
    """「前の質問に戻りたい」: 直前の問いを、直す問い(「そのまま」で戻れる)として出す(数えない。§4.6)。答えたら今の問いに戻る。"""
    pq = _previous(state, bank)
    if pq is None:
        H.ev(state, "go_back_none", qid=q["id"])
        return _aside(state, bank, llm, P.Plan(ack="short", next="keep", count="free"))
    slot = _slot_of(pq)
    state.current = H.ask_current(pq, "fix") | {"mode": "back", "slot": slot, "resume": q["id"]}
    H.ev(state, "go_back", qid=pq["id"], resume=q["id"])
    p.about = [f"{H.SLOTS[slot]['label']}の質問に戻ります"]
    return _say(state, bank, llm, counted=False, plan=p)


def _back(state, bank, llm, q, button, reading, text):
    """戻った問いへの答え: 形 D のまとめの「直す」と同じ決まりで入れ(high)、元の問いに戻る(数えない)。"""
    cur = state.current
    slot = cur["slot"]
    old = state.slots.get(slot)
    if button == H.FIX_KEEP:
        H.ev(state, "back_kept", slot=slot)
    else:
        _apply_fix(state, q, slot, button, reading, text)
    changed = state.slots.get(slot) != old
    _resume(state, bank, cur["resume"])
    p = P.Plan(ack="done", about=[f"{H.SLOTS[slot]['label']}を直しました"] if changed else [], next="keep", count="free")
    return _say(state, bank, llm, counted=False, plan=p)


def _resume(state, bank, qid):
    """戻る前の問いを出し直す。その問いの欄が、戻った間に埋まっていれば次の問いを選ぶ。"""
    oq = H.qmap(state, bank).get(qid)
    slots = [t for t in (oq or {}).get("targets", []) if t in H.ALL_SLOTS]
    if oq is not None and (not slots or any(H.is_open(state.slots.get(t)) for t in slots)):
        _present(state, oq)
    else:
        advance(state, bank)


def _more(state, q):
    """「他の候補」: 見せていない候補の次のページ(c1 の _more と同じ)。前置きは入り口の定義の文で、問いの短い名前から作る
    (モデルを呼ばない。数えない)。出し尽くしたら最初のページに戻す。戻った問いのときは「そのまま」も残す。"""
    cur = state.current
    i, cands = H.next_page(q, cur)
    if i is None:
        cur.update(page=len(q["pages"]), choices=H.page_choices(q, q["pages"][0], cur["mode"], more=False))
        lead = ENTRY.texts["more_done"]
    else:
        cur.update(page=i, choices=H.page_choices(q, cands, cur["mode"]))
        cur["shown"] = list(dict.fromkeys([*cur.get("shown", []), *H._shown_now(q, cur["choices"])]))
        lead = RP.more_lead(ENTRY.texts, q["topic"], q.get("kind"))
    if cur["mode"] == "back" and H.FIX_KEEP not in cur["choices"]:
        cur["choices"] = [*cur["choices"], H.FIX_KEEP]
    H.ev(state, "more", qid=q["id"], page=cur["page"], done=i is None)
    return H._out(state, [lead], cur["choices"], counted=False)


# ----------------------------- 言う -----------------------------
def _aside(state, bank, llm, plan=None):
    """答えでない発言: 今のものを出し直す(c1 の _aside と同じ数え方。計画の count が free なら数えない)。
    答えでないターンが続いたときと、疲れたと言ったときは、「中断」の注意を 1 回だけ添える(計画の pause_due)。"""
    p = plan or P.Plan(ack="light", next="keep", count="aside", offtrack=True)
    if p.count == "aside":
        state.aside_count += 1
    counted = p.count == "aside" and state.aside_count > H.MAX_ASIDES and state.turns < H.MAX_TURNS - H.REVIEW_RESERVE \
        and state.current["mode"] not in ("review", "result", "fix")
    if counted:
        H._count(state, "aside")
    if p.offtrack:
        state.offtrack += 1
    if p.hurry and H.HURRY not in state.flags:  # 急ぐ: この後は HARD のほかを聞かない(H.can_ask)
        H._flag(state, H.HURRY)
        H.ev(state, "hurry")
    if P.pause_due(p, state.offtrack, PAUSE_FLAG in state.flags):
        p.notes = [*p.notes, "pause"]
        H._flag(state, PAUSE_FLAG)
    if state.current["mode"] in ("review", "result"):
        return H._represent(state, bank, counted=counted, lead=H.ASIDE_FALLBACK)
    return _say(state, bank, llm, counted=counted, aside=True, plan=p)


def _known(state):
    return any(e.get("from") in ("saved", "resume") for e in state.slots.values())


def _say(state, bank, llm, counted=True, aside=False, plan=None, quiet=False, lead=None):
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
    p = plan or P.Plan(ack=None if cur["mode"] == "clarify" else "short")
    v, rp = view(state, bank, p, aside=aside, quiet=quiet, lead=lead)
    H.ev(state, "plan", **{k: x for k, x in p.to_dict().items() if k != "facts" and x})

    def _check(raw):
        parts, why = RP.validate(raw, rp, state.convo)
        return parts, ([why] if why else [])
    parts = None
    if llm is not None and RP.needs_model(rp):
        parts = call_llm(llm.generate, v, _check, _log(state), "generate")
    if parts is None:
        parts = RP.fallback(rp, state.convo, ENTRY.tone["acks"])
    if cur["mode"] != "clarify":
        cur["said"] = RP.ask_of(parts)  # 次に同じ問いを出し直すときは、この言い方を避ける
    state._ack = RP.ack_of(parts)
    return H._out(state, [RP.render(parts)], v["choices"], counted=counted) | {"parts": parts}


def _represent(state, bank, counted, lead=None):
    """今のものを、モデルを呼ばずに出し直す(c1 の _represent)。問いは層の組み立てで出す(前と違う言い方。c1 の聞き直しの文を付けない)。"""
    if (state.current or {}).get("mode") not in ASKING:
        return H._represent(state, bank, counted=counted, lead=lead)
    return _say(state, bank, None, counted=counted, quiet=True, lead=lead)


def phrasings(q, bank):
    """問いの言い方: 1 つ目は問いの文、続けて質問バンクの variants。区分ごとの文(text_by_segment)や、履歴書から入った仕事の言葉を
    引く文(text_known)に変わった問いは、その文だけ(variants は既定の文の言い換えなので)。"""
    n = q.get("card")
    raw = next((b for b in bank if b["id"] == _template(q)), q)
    which = "直近の" if n == 1 else "その前の"
    if q["text"] != raw["text"].replace("{which}", which):
        return [q["text"]]
    return [q["text"], *(x.replace("{which}", which) for x in raw.get("variants") or [])]


def view(state, bank, p, aside=False, quiet=False, lead=None):
    """言う段の入力(c1 の view に、計画とプロンプトの変数を足したもの)-> (view, 言う段の計画)。p は応じ方の計画(dip_mcp.convo.plan)。
    model_text は、言う段のモデルに本人の発言として渡す文、model_history は直前のやりとり(どちらも氏名と要配慮の部分を伏せたもの。
    ランタイムが、会話の記録の代わりに使う)。quiet はモデルを呼ばない出し直し(相づちも答えも付けず、lead を注意の場所に置く)。"""
    v = H.view(state, bank, aside=aside)
    cur = state.current
    q = H.qmap(state, bank).get(cur.get("qid"))
    mode = cur.get("mode")
    notes, lines = [], []
    for x in getattr(state, "_progress", None) or []:  # 飛ばした問いは注意、フェーズの区切りと職歴の「✓」は区切りの場所(§4.12)
        (lines if x in H.PHASE_LINES.values() or x in _group_lines() or x.startswith("✓") else notes).append(x)
    kind, answer = p.ack, None
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
    if p.answer:  # 逆質問(ほかの働きと一緒でも): 渡す事実だけで答える。事実が無ければ決まった文
        p.facts = F.pick(ENTRY.facts, p.answer, where=v["phase"], known=_known(state))
        answer = {"facts": p.facts, "fallback": ENTRY.texts["no_answer"]}
    notes += [ENTRY.texts[f"note_{n}"] for n in p.notes]
    v["choices"] = _offer(state, q, p, v["choices"], notes)
    reading, button = getattr(state, "_reading", None), getattr(state, "_button", None)
    hook = (reading or {}).get("hook") or (button if button and len(button) <= A.HOOK_MAX else None)
    hidden = guard.hidden(getattr(state, "_spans", []))
    if quiet:  # モデルを呼ばない出し直し(危ない言葉の後の「続ける」など): 前置きの文と問いだけ
        kind, answer = None, None
        notes = [lead] if lead else []
    rp = RP.new_plan(ask, kind, hook, answer, notes, lines, v["choices"], v["user_text"], hidden, p.about)
    convo = state.convo
    ack = ACK_JA[rp["ack"]] + (f"({'。'.join(p.about)})" if rp["ack"] == "done" and p.about else "")
    v |= {"text": ask["text"], "model_text": v["user_text"], "model_history": CS.messages(convo), "entry": ENTRY.title,
          "tone": ENTRY.tone["style"], "examples": " / ".join(ENTRY.tone.get("examples") or []) or "(なし)",
          "hook": rp["hook"] or "(なし)", "ack": ack, "ack_max": rp["ack_max"],
          "plan": json.dumps(p.for_say() | {"ack": rp["ack"]}, ensure_ascii=False),
          "answer": (f"答える。使ってよい事実(これだけ。無いことは「お答えできません」と言う): {rp['facts']}" if rp["answer"]
                     else f"書かない(システムが「{rp['fixed_answer']}」と答える)" if rp["fixed_answer"] else "しない"),
          "recent_acks": " / ".join(a for a in convo.get("acks", []) if a) or "(なし)",
          "last_reply": ((convo.get("replies") or [""])[-1] or "(なし)").replace("\n", " / "),
          "lines": " / ".join([*notes, *lines]) or "(なし)",
          "next_question": json.dumps({"id": ask["qid"], "mode": v["mode"], "text": ask["text"], "phrasings": ask["phrasings"]},
                                      ensure_ascii=False)}
    return v, rp


def _group_lines():
    return {g["line"] for g in ENTRY.groups if g.get("line")}
