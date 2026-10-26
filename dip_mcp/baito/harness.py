"""バイト探しの相棒(PRD v3)のハーネス。何を聞くか・いつフェーズを切り替えるか・いつ終えるかを決める。LLM は使わない。

モデルは毎ターン決まった JSON(reply / choices / slot_updates / ng_updates / segment)を返し、ここがそれを条件表に
マージして次の質問を決める(PRD §8)。プロンプトは「どう言うか」だけを受け持つ(prompts/baito_main_ja.md)。

正は docs/prd/baito_harness/harness_v3.py(FIX1〜FIX8 と V3-a〜d)。そこから変えたところ:
- 状態を dip の JSON に保存できるようにした(State.to_dict / from_dict)
- PRD §12 の既知の課題のうち、1・3・4・5 を足した(§13 の P4)
  - 1: 本人が文で言った値を、バンクの列挙値に寄せる(normalize。元の文は raw_text に残す)
  - 3: モデルが ng_removals で返した NG を消す(未成年の「深夜シフト」は消さない)
  - 4: NG で選べない選択肢を隠す(visible_choices。未成年に「深夜もOK」を出さない)
  - 5: あいまいな答えで同じ質問を聞き直すときは retry を付ける(プロンプトが聞き方を変える)
- 未成年の深夜の時間帯は、モデルの返り値によらずコードで受け付けない(merge_updates の最後。PRD §10)
- §12 の 2(review を2回しても残る矛盾の扱い)はユーザーが決めていないので変えていない(最後の low の値が残る)
"""

from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import asdict, dataclass, field

from dip_mcp.baito.bank import load_bank

HARD, MID, LOW = 3, 2, 1
SLOT_PRIORITY = {
    "common": {"days_per_week": HARD, "time_slots": HARD, "commute": HARD,
               "hourly_wage_min": MID, "shift_length": MID, "customer_facing": MID,
               "physical": MID, "purpose": MID, "pace": LOW,
               "atmosphere": LOW, "environment_noise": LOW},
    "student": {"age_under_18": HARD, "school_permission": HARD,
                "exam_flex": MID, "long_vacation": LOW},
    "homemaker": {"available_hours": HARD, "sudden_leave": HARD,
                  "fuyou": MID, "remote": MID},
    "side_job": {"side_job_allowed": HARD, "weekday_or_weekend": HARD,
                 "energy_left": MID, "avoid_coworkers": LOW},
    # FIX7: segment が最後まで分からなくても、法令に関わる年齢は聞く
    "unknown": {"age_under_18": HARD},
}
SEGMENTS = ("student", "homemaker", "side_job", "unknown")
ORDER = ["icebreak", "hard_filter", "scenario_game", "reverse_pick", "fill_gaps", "review", "result"]  # V3: review
MAX_TURNS = 15
REVIEW_RESERVE = 2      # V3: review(1回目 + 任意の2回目)のために残すターン
MAX_REVIEW_ROUNDS = 2
PRE_REVIEW = ORDER[:ORDER.index("review")]
DIRECT = {"hard_filter", "fill_gaps"}          # 直接聞くフェーズ(答えは high)
SEGMENT_SCORE = 1.5                            # FIX1: q_money(2)より小さくし、楽しい質問を先に出す
TRUTHY = (True, "true", "True", "yes", "はい")  # FIX8
NOPREF = "こだわりなし"                          # スキップ・どっちでもいい(PRD §5.1 の 6)
LATE_NIGHT_NG = "深夜シフト"                     # 未成年に自動で足す NG(22:00〜5:00)
REVIEW_OK = "これでOK"
# §12-4: NG にあるとき隠す選択肢(NG の項目, スロット, 値)。未成年には「深夜もOK」を出さない
CHOICE_BLOCKERS = ((LATE_NIGHT_NG, "time_slots", "late_night_ok"),)


@dataclass
class State:
    phase: str = "icebreak"
    segment: str = "unknown"
    slots: dict = field(default_factory=dict)
    ng: list = field(default_factory=list)
    asked: set = field(default_factory=set)
    attempts: dict = field(default_factory=dict)
    turns: int = 0
    phase_turns: int = 0
    pending: list = field(default_factory=list)  # FIX3: segment が決まる前に届いた、まだ無いスロットの更新
    conflicts: dict = field(default_factory=dict)  # V3: スロット -> 食い違う low の値
    review_rounds: int = 0                         # V3
    need_more_review: bool = False                 # V3
    review_log: list = field(default_factory=list) # V3
    question: dict | None = None                   # 最後に聞いた質問(step が返したもの。答えの読み取りに使う)
    rejected: list = field(default_factory=list)   # コードが受け付けなかった更新(未成年の深夜など)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["asked"] = sorted(self.asked)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> State:
        return cls(**(copy.deepcopy(d) | {"asked": set(d.get("asked", []))}))


def slots_for(state):
    s = dict(SLOT_PRIORITY["common"])
    s.update(SLOT_PRIORITY.get(state.segment, {}))
    return s


def gap_weight(state, slot):
    v = state.slots.get(slot)
    if v is None:
        return 1.0
    if v["confidence"] == "low":
        return 0.5
    return 0.0


def is_minor(state) -> bool:
    a = state.slots.get("age_under_18")
    return bool(a) and a["value"] in TRUTHY


def merge_updates(state, out):
    if out.get("segment") in SEGMENTS[:-1]:
        state.segment = out["segment"]
    known = slots_for(state)
    ups = state.pending + [u for u in map(_clean_update, out.get("slot_updates") or []) if u]  # FIX3: 預けた更新から
    state.pending = []
    minor = is_minor(state) or any(u["slot"] == "age_under_18" and u["value"] in TRUTHY for u in ups)
    for u in ups:
        if u["slot"] not in known:
            state.pending.append(u)                         # FIX3: 捨てずに預ける
            continue
        if minor and _late_night(u):                        # 未成年の深夜は受け付けない(今の値も上書きしない)
            state.rejected.append(u | {"reason": "under_18_late_night"})
            continue
        old = state.slots.get(u["slot"])
        if old and old["confidence"] == "high" and u["confidence"] == "low":
            continue
        _track_conflict(state, old, u)                      # V3
        state.slots[u["slot"]] = u
    for x in out.get("ng_updates") or []:
        if isinstance(x, str) and x.strip() and x.strip() not in state.ng:
            state.ng.append(x.strip())
    for x in out.get("ng_removals") or []:                  # §12-3: 本人が「やっぱり大丈夫」と取り消した NG
        if x in state.ng:
            state.ng.remove(x)
    _enforce_minor(state)


def _enforce_minor(state):
    """18歳未満なら NG に「深夜シフト」を足し(FIX8: TRUTHY で寛容に読む)、深夜の時間帯は残さない。モデルには頼らない。
    年齢より先に届いた深夜の時間帯は消し(スロットは空に戻り、fill_gaps で深夜を隠した選択肢で聞き直す)、
    矛盾の候補からも外す(カードの「どっち？」に深夜を出さない)。"""
    if not is_minor(state):
        return
    if LATE_NIGHT_NG not in state.ng:
        state.ng.append(LATE_NIGHT_NG)
    vals = state.conflicts.get("time_slots")
    if vals and "late_night_ok" in vals:
        vals.remove("late_night_ok")
        if len(vals) < 2:
            state.conflicts.pop("time_slots")
    t = state.slots.get("time_slots")
    if t and _late_night(t):
        state.rejected.append(state.slots.pop("time_slots") | {"reason": "under_18_late_night"})
        state.conflicts.pop("time_slots", None)


def _late_night(u) -> bool:
    return u["slot"] == "time_slots" and u["value"] == "late_night_ok"


def _clean_update(u) -> dict | None:
    """モデルが返した1件の更新をそろえる: slot が無いものは捨て、confidence は high / low に、値は列挙値に寄せる(§12-1)。"""
    if not isinstance(u, dict) or not isinstance(u.get("slot"), str) or "value" not in u:
        return None
    value, raw = normalize(u["slot"], u["value"])
    out = {"slot": u["slot"], "value": value, "confidence": "high" if u.get("confidence") == "high" else "low",
           "from": str(u.get("from") or "")}
    if raw is not None:
        out |= {"raw_text": raw, "normalized": value != raw}
    return out


def _track_conflict(state, old, u):
    # V3: 食い違う2つの low の更新は、どちらの値も残す。high の更新が来たら解消する
    slot = u["slot"]
    if u["confidence"] == "high":
        state.conflicts.pop(slot, None)
        return
    if old and old["confidence"] == "low" and value_ja(slot, old["value"]) != value_ja(slot, u["value"]):
        # 日本語のラベルで比べ、同じ意味の値(living_cost / household)を矛盾にしない
        vals = state.conflicts.setdefault(slot, [old["value"]])
        if u["value"] not in vals:
            vals.append(u["value"])


def open_gaps(state, min_pri, include_low):
    out = []
    for slot, pri in slots_for(state).items():
        if pri < min_pri or state.attempts.get(slot, 0) >= 2:
            continue
        w = gap_weight(state, slot)
        if w == 1.0 or (include_low and w == 0.5):
            out.append(slot)
    return out


def _phase_ok(state, q):
    if q["phase"] == state.phase:
        return True
    # FIX5: fill_gaps は hard_filter の直接質問も(また)使える(segment が遅く分かった / HARD の答えがあいまい)
    return state.phase == "fill_gaps" and q["phase"] == "hard_filter"


def _segment_ok(state, q, pri):
    if "all" in q["segments"] or state.segment in q["segments"]:
        return True
    # FIX7: segment が分からないときは、targets がどれも今の条件表にある直接質問を許す
    return (state.segment == "unknown" and q["phase"] in DIRECT
            and all(t in pri for t in q["targets"]))


def pick_question(state, bank):
    pri = slots_for(state)
    best, best_score = None, 0
    for q in bank:
        if not _phase_ok(state, q) or not _segment_ok(state, q, pri):
            continue
        if q["id"] in state.asked and not (state.phase == "fill_gaps" and q["phase"] in DIRECT):
            continue                                        # FIX5: fill_gaps の直接質問は聞き直せる(attempts まで)
        if "segment" in q["targets"]:                        # FIX1: segment は疑似ターゲット
            score = SEGMENT_SCORE if state.segment == "unknown" and q["id"] not in state.asked else 0
        else:
            score = sum(pri[t] * gap_weight(state, t)
                        for t in q["targets"]
                        if t in pri and state.attempts.get(t, 0) < 2)
        if score > 0 and state.segment in q["segments"]:
            score += 0.5
        if score > best_score:
            best, best_score = q, score
    return best


def remaining_gaps(state):
    # FIX6b: fill_gaps で埋めるもの = 空か low の HARD + 空の MID。
    # シナリオで推測した(low の)MID はそのまま受け取る(シナリオゲームの狙い。review のカードでまとめて確かめる)
    return sorted(set(open_gaps(state, HARD, True)) | set(open_gaps(state, MID, False)))


def next_phase(state):
    p, n = state.phase, state.phase_turns
    if p == "review":                                   # V3: もう1回までで、あとは result
        more = state.need_more_review and state.review_rounds < MAX_REVIEW_ROUNDS and state.turns < MAX_TURNS
        return "review" if more else "result"
    if state.turns >= MAX_TURNS:
        return "result"
    if p in PRE_REVIEW and state.turns >= MAX_TURNS - REVIEW_RESERVE:
        return "review"                                 # V3: 予約したターンで review は必ず行う
    if p == "icebreak":
        return "hard_filter" if state.segment != "unknown" or n >= 3 else p   # FIX2
    if p == "hard_filter":
        return "scenario_game" if not open_gaps(state, HARD, False) or n >= 5 else p
    if p == "scenario_game":
        # FIX6: reverse_pick(1)と、空の HARD を埋めるターンを残す。
        # V3: シナリオが会話の中心なので、短くするのは HARD の不足(+ reverse_pick + review の予約)だけ。
        # 空の MID は fill_gaps のターンで聞くか、review のカードに「未定」で出す
        budget_tight = state.turns + 1 + len(open_gaps(state, HARD, True)) + REVIEW_RESERVE >= MAX_TURNS
        done = n >= 6 or (n >= 4 and not open_gaps(state, MID, False)) or (n >= 2 and budget_tight)
        return "reverse_pick" if done else p
    if p == "reverse_pick":
        return "fill_gaps"
    if p == "fill_gaps":
        return "review" if not remaining_gaps(state) else p  # V3
    return p


def step(state, llm_out, bank):
    """1ターン進める: マージ → turns+1 → next_phase → 次の質問 / review のカード / None(result)。
    llm_out が None なら、何も受け取らずに最初の質問を決める。"""
    if llm_out is not None:
        if state.phase == "review":
            apply_review(state, llm_out)                # V3
        else:
            merge_updates(state, llm_out)
        state.turns += 1
        state.phase_turns += 1
    new = next_phase(state)
    while True:
        if new != state.phase:
            state.phase, state.phase_turns = new, 0
        if new == "result":
            state.question = None
            return None
        if new == "review":                             # V3: カードを「質問」にする
            state.review_rounds += 1
            card = build_card(state)
            state.question = {"id": f"review_{state.review_rounds}", "phase": "review", "segments": ["all"],
                              "targets": [], "text": render_card_ja(card), "choices": [REVIEW_OK],
                              "choice_effects": {REVIEW_OK: {}}, "card": card}
            return state.question
        q = pick_question(state, bank)
        if q:
            break
        new = ORDER[ORDER.index(new) + 1]
    retry = q["id"] in state.asked                      # §12-5: あいまいな答えの後の聞き直し
    state.asked.add(q["id"])
    if q["phase"] in DIRECT:                                 # FIX4: 聞き直しの枠を使うのは直接質問だけで、
        for t in q["targets"]:                              #       まだ空か low のスロットだけ
            if gap_weight(state, t) > 0:
                state.attempts[t] = state.attempts.get(t, 0) + 1
    state.question = _shown(state, q, retry)
    return state.question


def visible_choices(state, q) -> list[str]:
    """§12-4: NG で選べない選択肢を除いた選択肢(順はバンクのまま)。除くと1つも残らない質問は、そのまま出す。"""
    blocked = {(slot, value) for ng, slot, value in CHOICE_BLOCKERS if ng in state.ng}
    keep = [c for c in q["choices"]
            if not any((s, v) in blocked for s, v in q["choice_effects"][c].items() if not s.startswith("__"))]
    return keep or list(q["choices"])


def _shown(state, q, retry: bool) -> dict:
    """本人に出す形の質問(バンクの dict の写し。選択肢は NG で絞り、聞き直しなら retry を付ける)。"""
    choices = visible_choices(state, q)
    return copy.deepcopy(q) | {"choices": choices, "choice_effects": {c: q["choice_effects"][c] for c in choices}} | (
        {"retry": True} if retry else {})


def answer_from_choice(state, text: str) -> dict | None:
    """本人の発言が、最後に聞いた質問の選択肢そのもの(クイックリプライを押した)なら、choice_effects どおりの更新
    (モデルの JSON と同じ形)。確信度は PRD §6: 直接質問は high、それ以外(icebreak・シナリオ・reverse_pick)は low。
    選択肢でなければ None(モデルが文から読み取る)。"""
    q = state.question
    if not q or (text or "").strip() not in q["choices"]:
        return None
    eff = q["choice_effects"][text.strip()]
    conf = "high" if q["phase"] in DIRECT else "low"
    return {"slot_updates": [{"slot": k, "value": v, "confidence": conf, "from": q["id"]}
                             for k, v in eff.items() if not k.startswith("__")],
            "ng_updates": list(eff.get("__ng__", [])), "segment": eff.get("__segment__", "unknown")}


# ======================= V3: review フェーズ / 結果のカード =======================
SLOT_LABEL_JA = {
    "days_per_week": "週の日数", "time_slots": "働く時間帯", "commute": "通勤", "hourly_wage_min": "時給",
    "shift_length": "1回のシフトの長さ", "customer_facing": "接客", "physical": "体力・立ち仕事",
    "purpose": "バイトの目的", "pace": "忙しさ", "atmosphere": "職場の雰囲気", "environment_noise": "職場のにぎやかさ",
    "age_under_18": "18歳未満", "school_permission": "学校のバイト許可", "exam_flex": "テスト期間",
    "long_vacation": "長期休み", "available_hours": "働ける時間", "sudden_leave": "急なお休み",
    "fuyou": "扶養", "remote": "在宅/外", "side_job_allowed": "会社の副業OK", "weekday_or_weekend": "平日/週末",
    "energy_left": "本業後の体力", "avoid_coworkers": "会社の人に会うのは",
}
SEGMENT_JA = {"student": "学生", "homemaker": "家のこと中心", "side_job": "本業あり・副業", "unknown": "（未回答）"}
VALUE_JA = {True: "はい", False: "いいえ", NOPREF: NOPREF}
# 推測した値(シナリオ・reverse_pick・icebreak)の読みやすいラベル。選択肢の文は補いにだけ使う:
# 例えば reverse_pick の「引越しスタッフ」は、本人がやりたくないもの
_E = {
 "customer_facing": {"ok": "接客OK", "avoid": "接客は避けたい", "backoffice_ok": "裏方もOK（店長にほめられたい）", "like": "接客が好き"},
 "pace": {"busy_ok": "忙しくてもOK", "calm": "落ち着いたペースがいい"},
 "shift_length": {"short": "短め（3時間くらい）", "long": "長め（6時間くらい）"},
 "atmosphere": {"structured": "マニュアルがしっかり", "autonomous": "ざっくり・自分で考える", "friendly": "仲良くわいわい",
                "dry": "さっぱり各自解散", "same_age": "同年代が多い", "mixed_age": "大人が多くてもOK",
                "parents": "子育て中の人が多い", "any": "こだわらない"},
 "environment_noise": {"lively": "にぎやか", "quiet": "静か", "lively_ok": "にぎやかでもOK"},
 "hourly_wage_min": {"wage_less_important": "時給より近さ優先", "wage_priority": "時給優先（多少遠くても）",
                     "high": "高め希望（立ち仕事でも）", "normal": "ふつうでOK（座れるなら）"},
 "physical": {"light": "軽めの作業がいい", "seated": "座り仕事がいい", "standing_ok": "立ち仕事OK", "heavy_ok": "体力仕事どんとこい"},
 "purpose": {"hobby": "推し活・趣味", "saving": "貯金・将来のため", "living_cost": "家計の足し・生活費",
             "household": "家計の足し・生活費", "fun": "旅行・遊び", "career": "履歴書に書ける経験", "easygoing": "ゆるく楽しく",
             "perk_meal": "まかない", "perk_discount": "社割", "pocket_money": "自分のおこづかい",
             "use_experience": "経験を活かしたい", "try_new": "新しいことをしたい", "no_brain": "頭を使わない作業",
             "skill_up": "本業にプラスになる仕事", "earn": "しっかり稼ぐ", "refresh": "気分転換", "money": "お金",
             "skill": "経験・スキル", "people": "人とのつながり"},
 "exam_flex": {"full_off": "テスト前は完全に休みたい", "reduced": "テスト前は少なめならOK", "no_change": "気にしない"},
 "long_vacation": {"heavy": "長期休みはがっつり", "normal": "長期休みもいつも通り"},
 "available_hours": {"fixed_morning": "週2・午前で固定", "flexible": "毎週自分で選びたい"},
 "sudden_leave": {"need_instant": "急に休める職場がいい", "cover_ok": "代わりの人がいれば安心"},
 "remote": {"yes": "在宅がいい", "no": "外で働きたい", "either": "どちらでも"},
 "fuyou": {"check_needed": "要確認（家計の足しにしたい）", "within": "扶養内", "beyond_ok": "扶養は気にしない",
           "unknown_needs_help": "わからない（相談したい）"},
 "weekday_or_weekend": {"weekend_full_day": "週末に1日しっかり", "weekend_half_day": "週末に半日"},
 "avoid_coworkers": {"yes": "会社の人には会いたくない", "no": "気にしない"},
 "energy_left": {"steady_ok": "毎週安定して働ける", "limited": "空いた日だけ（スポット）"},
 "time_slots": {"after_school": "放課後すぐ", "after_club": "部活のあと"},
}
for _slot, _m in _E.items():
    for _v, _ja in _m.items():
        VALUE_JA[(_slot, _v)] = _ja
# スロットごとの、バンクと _E に出てくる値(§12-1 の正規化で、列挙値かを見る)と、ラベル → 値
SLOT_VALUES: dict[str, list] = {}
LABEL_TO_VALUE: dict[str, dict[str, object]] = {}


def set_value_labels(bank):
    """保存した値の読みやすい日本語: その値を作った選択肢の文を使う(_E が先)。§12-1 の逆引きの表も作る。
    逆引きには reverse_pick の選択肢の文を使わない(本人がやりたくないものの名前なので、値のラベルではない)。"""
    for slot, m in _E.items():
        for v, ja in m.items():
            _add_value(slot, v, ja)
    for q in bank:
        for choice, eff in q.get("choice_effects", {}).items():
            for slot, v in eff.items():
                if not slot.startswith("__"):
                    VALUE_JA.setdefault((slot, v), choice)
                    _add_value(slot, v, None if q["phase"] == "reverse_pick" else choice)


def _add_value(slot, v, label):
    vals = SLOT_VALUES.setdefault(slot, [])
    if not any(v == x and type(v) is type(x) for x in vals):
        vals.append(v)
    if label:
        LABEL_TO_VALUE.setdefault(slot, {}).setdefault(label, v)


def value_ja(slot, v):
    if isinstance(v, bool):
        return VALUE_JA[v]
    try:
        return VALUE_JA.get((slot, v), VALUE_JA.get(v, str(v)))
    except TypeError:                                   # list など、表のキーにできない値
        return str(v)


# ----------------------------- §12-1: 本人が文で言った値を列挙値に寄せる -----------------------------


def _hours(s):
    m = re.search(r"(\d+(?:\.\d+)?)\s*時間", s)
    return float(m.group(1)) if m else None


def _bucket(n, steps):
    """n が入る区切りの値(steps は (上限, 値) の昇順。最後の上限は None)。"""
    return next(v for top, v in steps if top is None or n <= top)


# 否定や条件つきの言い方(「夜は無理」「深夜以外」「近くじゃなくてもいい」)は、読み方の規則で寄せると逆の値になるので寄せない
NEGATION = re.compile(r"無理|以外|ダメ|だめ|NG|厳し|きつ|できな|でき(ません|ない)|じゃな|ではな|避け|嫌|いや|ない(です)?$|なく")


def _days(s):
    """「週2日」→ 1-2。範囲(「週2〜3日」)が区切りをまたぐときは寄せない。"""
    steps = ((2, "1-2"), (4, "3-4"), (None, "5+"))
    m = re.search(r"週\s*(\d)(?:\s*[〜~\-ー]\s*(\d))?", s) or re.search(r"(\d)(?:\s*[〜~\-ー]\s*(\d))?\s*日", s)
    if not m:
        return None
    lo, hi = _bucket(int(m.group(1)), steps), _bucket(int(m.group(2) or m.group(1)), steps)
    return lo if lo == hi else None


# 文の時刻(「22時」「夜10時」「23:30」)。前の語で午前・午後を見分け、後の「まで」・前の「〜」で終わりの時刻と読む
CLOCK = re.compile(r"([〜~\-ー]\s*)?(深夜|夜中の?|夜の?|晩の?|午前|午後|朝)?\s*(\d{1,2})\s*(?:時(?!間)\s*(半)?|:(\d{2}))(\s*(?:まで|迄))?")


def _late_clock(s) -> bool:
    """文の時刻が深夜(22:00〜5:00)にかかるか。「22時以降」「23時まで」「夜10時から」「24時」「夜中の2時」→ True。
    「22時まで」「17〜22時」(22時ちょうどに終わる)と、前の語の無い「2時から」(午後と読む)は当たらない。"""
    for m in CLOCK.finditer(s):
        pre, n = m.group(2) or "", int(m.group(3))
        if pre.startswith(("夜", "晩", "午後")) and 5 <= n < 12:
            n += 12
        if n < 5 and not (n == 0 or pre.startswith(("深夜", "夜", "晩", "午前", "朝"))):
            continue
        ends_on_the_hour = (m.group(1) or m.group(6)) and not (m.group(4) or int(m.group(5) or 0))
        if n >= 23 or n < 5 or (n == 22 and not ends_on_the_hour):
            return True
    return False


def _time_slots(s):
    if _late_clock(s):                                  # 時刻の言い方を先に見る(「夜10時から」は夜ではなく深夜)
        return "late_night_ok"
    for pat, v in ((r"深夜|夜勤|夜中", "late_night_ok"), (r"夕方|夜", "evening"), (r"昼|日中", "daytime"), (r"朝|午前", "morning")):
        if re.search(pat, s):
            return v
    return None


def _commute(s):
    if re.search(r"徒歩|自転車|近所|近く", s):
        return "walk_bike"
    if re.search(r"1\s*時間|60\s*分", s):
        return "train_60"
    m = re.search(r"(\d+)\s*分", s)
    return _bucket(int(m.group(1)), ((30, "train_30"), (None, "train_60"))) if m else None


def _shift(s):
    """「4時間くらい」→ 2-4h。区切りの時間ちょうどは、「以上」「から」なら上の区切り(「6時間以上」→ 6h+)。"""
    h = _hours(s)
    if h is None:
        return None
    if re.search(r"以上|から|超", s):
        return "6h+" if h >= 6 else "4-6h" if h >= 4 else "2-4h"
    return _bucket(h, ((4, "2-4h"), (6, "4-6h"), (None, "6h+")))


def _wage(s):
    """「1,200円以上」→ 1200。円か時給の語が無い数(「2026年」)は寄せない。"""
    m = re.search(r"(\d{3,5})\s*円", s.replace(",", "")) or (
        re.search(r"(\d{3,5})", s.replace(",", "")) if "時給" in s else None)
    return int(m.group(1)) if m and int(m.group(1)) >= 500 else None


def _age(s):
    """18歳未満か。「はい」「16歳」「17才」「16」「18歳未満です」「高1」→ True、「いいえ」「19歳」「18歳以上」→ False。
    高3・高校生・大学生のように18歳のこともある言い方は寄せない。"""
    if re.fullmatch(r"(はい|true|yes)", s, re.IGNORECASE):
        return True
    if re.fullmatch(r"(いいえ|false|no)", s, re.IGNORECASE):
        return False
    if re.search(r"中学|高[12１２]|高校[12１２]年", s):
        return True
    m = re.search(r"(\d+)\s*(歳|才)?\s*(未満|以上|より下|より上)?", s)
    if not m or (m.group(2) is None and not re.fullmatch(r"\d+", s)):
        return None
    n, rel = int(m.group(1)), m.group(3)
    if rel in ("未満", "より下"):
        return True if n <= 18 else None            # 「20歳未満」は17歳のこともある
    if rel in ("以上", "より上"):
        return False if n >= 18 else None
    return n < 18


NORMALIZERS = {"days_per_week": _days, "time_slots": _time_slots, "commute": _commute, "shift_length": _shift,
               "hourly_wage_min": _wage, "age_under_18": _age}


def normalize(slot, value):
    """(値, 元の文)。値がもう列挙値(バンクか _E にある値・こだわりなし)なら (値, None)。文なら、そのスロットの
    選択肢の文かラベルに一致するもの、次にスロットごとの読み方(「4時間くらい」→ 2-4h、「平日の夜」→ evening、
    「1,200円」→ 1200)で列挙値に寄せる。寄せられなければ文のまま(どちらも元の文を返し、raw_text に残す)。"""
    if value == NOPREF or any(value == x and type(value) is type(x) for x in SLOT_VALUES.get(slot, [])):
        return value, None
    if slot == "age_under_18" and type(value) is int:   # 年齢そのもの(16)
        return value < 18, value
    if not isinstance(value, str):
        return value, None
    s = unicodedata.normalize("NFKC", value).strip()
    for label, v in LABEL_TO_VALUE.get(slot, {}).items():
        if unicodedata.normalize("NFKC", label) == s:
            return v, value
    v = NORMALIZERS[slot](s) if slot in NORMALIZERS and not NEGATION.search(s) else None
    return (v, value) if v is not None else (value, value)


# ----------------------------- カード -----------------------------


def build_card(state):
    pri = slots_for(state)
    card = {"segment": state.segment, "segment_ja": SEGMENT_JA.get(state.segment, state.segment),
            "hard": [], "prefs": [], "ng": list(state.ng), "conflicts": sorted(state.conflicts),
            "missing": [], "round": state.review_rounds}
    for slot, p in sorted(pri.items(), key=lambda kv: -kv[1]):
        u = state.slots.get(slot)
        item = {"slot": slot, "label": SLOT_LABEL_JA.get(slot, slot), "priority": p,
                "value": None if u is None else u["value"],
                "value_ja": "未定" if u is None else value_ja(slot, u["value"]),
                "inferred": bool(u and u["confidence"] == "low"),
                "missing": u is None,
                "conflict": [value_ja(slot, v) for v in state.conflicts[slot]] if slot in state.conflicts else None,
                "conflict_values": list(state.conflicts.get(slot, []))}
        if u is None:
            if p < MID:
                continue                     # 空の LOW はカードに出さない
            card["missing"].append(slot)
        (card["hard"] if p == HARD else card["prefs"]).append(item)
    return card


def render_card_ja(card, final: bool = False):
    """カードの日本語(PRD §8.6 の原文)。final は結果のカード(確認の呼びかけを付けない)。"""
    def line(it):
        if it["conflict"]:
            return f"・{it['label']}：どっち？ " + " / ".join(it["conflict"])
        mark = "（推測）" if it["inferred"] else ""
        if it["missing"]:
            return f"・{it['label']}：未定（よければ教えてね）"
        return f"・{it['label']}：{it['value_ja']}{mark}"
    out = ["📋 あなたのバイト条件まとめ" + ("（もう一度だけ確認！）" if card["round"] > 1 and not final else ""),
           f"（あなた：{card['segment_ja']}）", "", "【ゆずれない条件】"]
    out += [line(i) for i in card["hard"]]
    out += ["", "【こだわり・好み】"] + [line(i) for i in card["prefs"]]
    out += ["", "【NG】" + ("、".join(card["ng"]) if card["ng"] else "とくになし")]
    if not final:
        out += ["", ("（推測）は会話からの推測です。推測のところ、ちがってたらまとめて教えてね！"
                    "「どっち？」はどちらか選んでね。ぜんぶ合ってたら「これでOK」で！")]
    return "\n".join(out)


def apply_review(state, out):
    """review の返信: 直した項目は high、触れなかった low の項目は確定(review_accepted)にする。
    HARD の値が変わった、矛盾が残っている、segment が変わって HARD に空きができた、のどれかなら、もう1回確かめる。"""
    before = {k: v["value"] for k, v in state.slots.items()}
    seg_before = state.segment
    out = copy.deepcopy(out)
    for u in out.get("slot_updates") or []:
        if isinstance(u, dict):
            u["confidence"] = "high"
    n_rejected = len(state.rejected)
    merge_updates(state, out)
    pri = slots_for(state)
    rejected = {u["slot"] for u in state.rejected[n_rejected:]}  # 受け付けなかった直し(未成年の深夜)は、直したに数えない
    touched = {u["slot"] for u in out.get("slot_updates") or []
               if isinstance(u, dict) and u.get("slot") in state.slots and u.get("slot") not in rejected}
    promoted = []
    for slot, u in list(state.slots.items()):
        if slot in touched or slot in state.conflicts or u["confidence"] != "low":
            continue
        state.slots[slot] = dict(u, confidence="high", **{"from": "review_accepted"})
        promoted.append(slot)
    hard_changed = [s for s in touched if pri.get(s) == HARD and before.get(s) != state.slots[s]["value"]]
    new_hard_gaps = [s for s, p in pri.items() if p == HARD and s not in state.slots] if state.segment != seg_before else []
    state.need_more_review = bool(hard_changed or state.conflicts or new_hard_gaps)
    state.review_log.append({"round": state.review_rounds, "touched": sorted(touched), "promoted": promoted,
                             "hard_changed": hard_changed, "open_conflicts": dict(state.conflicts),
                             "new_hard_gaps": new_hard_gaps, "segment_change": (seg_before, state.segment)})


BANK = load_bank()
set_value_labels(BANK)
