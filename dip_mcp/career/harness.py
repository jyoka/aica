"""正社員のチャットのヒアリングのハーネス。何を聞くか、いつフェーズを切り替えるか、いつ終えるかを決める。

設計は docs/design/fulltime-hearing.md の §3〜§5(決めたことは §8)、ADR-0010。形は、アルバイト・パートの v4.1
(dip_mcp/baito/harness.py、ADR-0009)と同じ 3 段の turn():
  Stage1 抽出: ボタン(出している選択肢そのもの)→ choice_effects をここで当てる(LLM を呼ばない)。
               文 → llm.extract(payload) → validate_extract()(失敗したら 1 回だけやり直す)
  Stage2 判断: 確信度と from はここが決める。マージ、時間の予算、次の質問とフェーズ
  Stage3 生成: llm.generate(view) → validate_reply()(1 回だけやり直し、だめなら質問の原文)。まとめと結果は LLM を呼ばない
正社員だけのものは、職歴カード(§4)と、本人の推定時間の予算(§3.3)。

- 本人の言葉のまま入れる: 今の質問への文の答えは、本人の文をそのまま text に入れる。ほかの欄は、抽出が抜き出した部分が
  発言の部分文字列のときだけ入れる(確信度は low。まとめで確かめる)。数は、発言に数字で書いてあるときだけ入れる
- 自由に打った文を正規表現で読まない: 文の中身は抽出(LLM)が読む。読めなかった答えは、ボタンで聞き直す(1 回だけ。数えない)。
  2 回目も読めなければ、本人の文を low のまま入れ、まとめで確かめる(聞き直さない)
- 「中断」(と形 D の「始める」の「あとで」): そこまでの状態(summary)を返して終える。保存は呼ぶ側(2b のタスク)
- 答えの候補(task 315、設計メモ §5.3): どの問いも候補をページで持ち、「他の候補」で次のページを出す(数えない)。問いのボタンは
  「スキップ」(この問いだけ)で、終える操作はボタンにしない。聞き返しには問いの説明(explain)と、それに合う候補を出す
- 形 B、C、D(§6.5): B は場面の二択と逆選択をせず、MID を直接聞き、進み具合と 1 行を見せない。C は全部。new_state(form=)、
  無ければ環境変数 CAREER_HARNESS_FORM、既定は C
- 形 D(task 246)は、C の問いに LINE の Flex を足した形。問いと予算は C と同じで、違うのは 3 つだけ: 最初に「始める」の 1 枚を
  出す(mode=start)、職歴カードの「✓職歴」の 1 行を吹き出しに入れない(職歴カードの Flex が見せる)、まとめの行は吹き出しでなく
  Flex に入れ、行の「直す」のボタン(FIX_TEXT の文)で直す欄を選べる(mode=fix)。何を Flex にするかと、その字を読む時間は
  dip_mcp/career/ui.py が受け持つ
"""

from __future__ import annotations

import copy
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from resume_draft.pii import personal_value
from resume_lines import blank_count, check_numbers

HERE = Path(__file__).resolve().parent
PROMPT_DIR = HERE.parent / "prompts" / "career"

VERSION = "c1"
HARD, CARD1, CARD2, MID, LOW = 5, 4, 3, 2, 1  # 出題の優先度(§3.3): HARD > 1 枚目の職歴カード > 2 枚目 > MID と LOW
ORDER = ["icebreak", "hard_filter", "career_cards", "scenario_game", "reverse_pick", "fill_gaps", "review", "result"]
PRE_REVIEW = ORDER[:6]
DIRECT = {"icebreak", "hard_filter", "career_cards", "fill_gaps"}  # 直接聞くフェーズ(ボタンも文も high)
MAX_TURNS = 16
REVIEW_RESERVE = 2
HARD_LAST_TURN = MAX_TURNS - 1  # HARD は、review のために残した 2 ターンのうち 1 ターンまで使ってよい(review は必ず 1 回出す)
MAX_REVIEW_ROUNDS = 2
CARD_COUNT = 2  # 直近の 2 件(§4)。3 件目は MAX_TURNS の中に入らない
MAX_CLARIFY = 2
# フェーズの上限。問いの数に聞き返し(MAX_CLARIFY)の分を足す(聞き返しで HARD が職歴カードの後ろに押し出されないように)
PHASE_CAP = {"icebreak": 2 + MAX_CLARIFY, "hard_filter": 5 + MAX_CLARIFY, "career_cards": 3 * CARD_COUNT + MAX_CLARIFY,
             "scenario_game": 2, "reverse_pick": 1, "fill_gaps": 3}
MAX_ASIDES = 3
LLM_RETRIES = 1
FORMS = ("B", "C", "D")
UI_FORMS = ("D",)  # LINE の Flex を付ける形(問いは C と同じ)
DEFAULT_FORM = "C"
FORM_ENV = "CAREER_HARNESS_FORM"

# 時間の予算(§3.3。数え方は §6.1 と同じ)
READ_CHARS_PER_MIN = 400
TYPE_CHARS_PER_MIN = 40
BUTTON_SECONDS = 2.0
TARGET_SECONDS = {True: 180.0, False: 300.0}  # 目標時間(履歴書あり / なし)
REVIEW_TIME_RESERVE = 90.0  # 質問を止める時刻 = 目標時間 - これ(review 2 回分の 70 秒とゆとり 20 秒)

# 1 ターンの字数の予算(§3.3、§5.2。字は空白を数えない)
BUBBLE_MAX = 80
LABELS_MAX = 40
QUICK_MAX = 13
LABEL_MAX = 20
CARD_MAX = 200  # まとめ 1 回(文とカードの中の字の合計)。読む 30 秒 + ボタン 2 秒で 35 秒以内(§3.3)
REACTION_MAX = 25  # 生成の受け止め(質問の文の外)の字数
PROGRESS_WORDS = 10  # 形 C の 1 行に出す本人の言葉の字数
KNOWN_ROLE_CHARS = 10  # 履歴書から入った職歴の問いの頭に付ける、仕事の言葉の字数(_expand)
KNOWN_ROLE_MIN = 4  # 数の手前で切った仕事の言葉がこれより短ければ、引用をやめる(_clip)
KNOWN_ROLE_FALLBACK = "そのお仕事"  # 引用をやめたときの決まった言い方(「{role}」の代わり)
SAVED_TEXT_MAX = 200  # 保存する 1 つの欄の字数の上限(dip_mcp.career.store.VALUE_MAX。超える値は保存しない)

# 「スキップ」はこの問いだけを飛ばす。ヒアリングを終える操作は問いのボタンに入れない(task 315。「パス」と「あとで」が並び、
# どちらが全体を終えるのかラベルから分からない、と利用者の指摘)。終えるときは「中断」と送る(END_WORDS)。
# 「あとで」のボタンは、形 D の「始める」の 1 枚にだけ残す(まだ問いに入っていないので、答えと紛れない)
PASS, DONT_KNOW, LATER, REVIEW_OK = "スキップ", "覚えていない", "あとで", "これでOK"
END_WORDS = ("中断", "中断する", "ヒアリングを中断", "あとで")  # 打った文がこれだけなら、そこまでを残して閉じる(LATER と同じ)
# task 315 より前に出したボタン(進めている相談の状態に残る)を、今の名前で受ける。名前を変えた候補も(受けないと、ボタンとして
# 受けたのに効き目が無く、答えが黙って消える)
LEGACY_BUTTONS = {"パス": PASS, "5人くらい": "チーム5人くらい", "10人くらい": "チーム10人くらい", "20人以上": "チーム20人以上",
                  "規模": "任される仕事の大きさ"}
MORE = "他の候補"  # 答えの候補の次のページを出すボタン(task 315)。数えない
# 「他の候補」の吹き出し(task 326)。押した文をそのまま返すと、何の候補か分からない(本番で「他の候補です。」と返して利用者の指摘)。
# 問いごとに手で書かず、質問バンクの短い名前(topic)から作る。数の問い(kind=number)の候補は区切り(「年収のほかの区切りです。」)
MORE_LEAD = "{topic}のほかの{noun}です。"
MORE_NOUN = {"number": "区切り"}
MORE_NOUN_DEFAULT = "候補"
MORE_DONE = "候補は以上です。最初の候補に戻しました。合わなければ、ひとことで送ってください。"
NOPREF = "こだわりなし"
SEGMENTS = ("same_path", "change", "return", "early")
SEGMENT_JA = {"same_path": "同じ仕事で上へ", "change": "仕事を変えたい", "return": "ブランクから戻る", "early": "経験が浅い",
              "unknown": "未回答"}
INTENTS = ("answer", "pass", "dont_know", "later", "clarify", "aside", "show_result", "more")
STATUSES = ("filled", "passed", "dont_know")

# 入口では残りの問いの数とかかる時間を出さない(task 288。「あと12問」で、せっかちな人はほぼ離れる、と利用者の指摘)。
# 何のためか(職務経歴書の材料)と、1 問ずつなことだけを言う
# やめ方(「中断」と送る)も入口で言う(task 315。問いのボタンに終える操作を入れないため)。形 D は「始める」の 1 枚の
# 一言(dip_mcp.career.ui.START_NOTE)が言うので、吹き出しは START_LEAD_UI
START_LEAD_UI = "職務経歴書の材料を、1問ずつ集めます。"
START_LEAD = START_LEAD_UI + "「中断」でいつでもやめられます。"
# フェーズが変わったときの 1 行(形 C と D。task 288)。残りの問いの数は出さず、次に何を聞くかだけを言う。
# 吹き出しの中では、前の答えへの受け止めの後、次の問いの前に置く(bubble。task 326)
PHASE_LINES = {"hard_filter": "次は希望の条件です", "career_cards": "次は職歴です", "scenario_game": "次は二択の質問です",
               "reverse_pick": "次は選ぶだけの質問です", "fill_gaps": "あと少しで完成です"}
START_GO = "始める"  # 形 D の「始める」の 1 枚のボタン(履歴書の写真から始めるボタンは、LINE 側が LIFF の画面へのリンクにする)
REVIEW_LEAD_UI = "まとめです。違うところを押すか、送ってください。"  # 形 D(行は Flex に入れる)
FIX_LEAD = "直します。"
FIX_KEEP = "そのまま"  # 直す問いのボタン。前の値を変えずにまとめに戻る(押し間違えた「直す」をやめる)
FIX_LIMIT_LEAD = "ここからは、違うところを文で送ってください。"
CARD_UI_LABELS = {"role": "仕事", "scale": "規模", "result": "結果"}  # 形 D のカードとボタンの短い名前
RETRY_TEXT = "読み取れなかったので、近いものを押すか、ひとことで送ってください。"
# 聞き返し(「規模って何?」)には、まず問いの意味を説明する(質問バンクの explain)。同じ問いを繰り返さない(task 315)。
# 同じ問いへの 2 回目は、別の候補を出してこの文。3 回目は、その問いを飛ばして次へ進む(CLARIFY_SKIP)
CLARIFY_AGAIN = "迷ったら、近いものを押すか、思いついた言葉をそのまま送ってください。"
CLARIFY_SKIP = "この質問は飛ばします。"
REVIEW_LEAD = "まとめです。違うところだけ送ってください。"
REVIEW_FOCUS_LEAD = "ここだけ教えてください。"
RESULT_TEXT = "おつかれさまでした。職務経歴書の材料をまとめました。"
LATER_TEXT = "ここまでの分を残しました。続きはいつでもどうぞ。"
CLOSED_TEXT = "このヒアリングは終わりました。"
ASIDE_FALLBACK = "あとでお答えします。"

PRAISE_RE = re.compile(r"すごい|凄い|素晴らし|すばらし|さすが|立派|優秀|見事|えらい|偉い|素敵|すてき|いいですね|頼もし|感心")
KANJI_NUM_RE = re.compile(r"[〇一二三四五六七八九十百千万億兆]+")
NUM_RUN_RE = re.compile(r"(?:\d+(?:[.,]\d+)*|[〇一二三四五六七八九十百千万億兆])+")  # 数の並び(3億5千万のような混ざった数も 1 つ)
QMARK_RE = re.compile(r"[?？]")


def S(label, kind, values=None, short=None):
    return {"label": label, "kind": kind, "values": values or {}, "short": short or label}


SLOTS = {
    "why_now": S("なぜ今", "text"),
    "area": S("勤務地", "text"),
    "min_annual_salary": S("年収の下限", "number", short="年収"),
    "start_timing": S("働き始める時期", "text", short="時期"),
    "raise_target": S("上げたいもの", "text", short="上げたい"),
    "change_reason": S("変えたい理由", "text"),
    "use_experience": S("活かしたい経験", "text"),
    "work_hours": S("働ける時間", "text"),
    "blank_period": S("ブランクの期間", "number", short="ブランク"),
    "keep_doing": S("続けたいこと", "text"),
    "stop_doing": S("やめたいこと", "text"),
    "work_style": S("働き方", "multi", {"remote": "リモート多め", "office": "出社中心", "overtime_ok": "残業もOK",
                                     "less_overtime": "残業少なめ"}),
    "direction": S("次にやりたいこと", "text", {"plan": "新しいことを企画する", "organize": "散らかった仕事を整える"},
                   short="次は"),
    "team_style": S("人と働く形", "text", {"team": "チームで分担", "solo": "一人で任される"}, short="働く形"),
}
CARD_FIELDS = ("role", "scale", "result")
CARD_LABELS = {"role": "何をしていたか", "scale": "どのくらい", "result": "どうなったか"}
for _n in range(1, CARD_COUNT + 1):
    for _f in CARD_FIELDS:
        SLOTS[f"career{_n}.{_f}"] = S(f"職歴{_n} {CARD_LABELS[_f]}", "number" if _f == "scale" else "text")
ALL_SLOTS = set(SLOTS)
COMMON_HARD = ("why_now", "area", "min_annual_salary", "start_timing")
SEGMENT_HARD = {"same_path": ("raise_target",), "change": ("change_reason", "use_experience"),
                "return": ("work_hours", "blank_period"), "early": ("keep_doing", "stop_doing"), "unknown": ()}
# 「パス」で「こだわりなし」になる条件の欄(§3.3)。ほかの欄のパスは空欄(もう聞かない)
CONDITION_SLOTS = {"area", "min_annual_salary", "start_timing", "raise_target", "work_hours", "work_style"}


def card_key(n, f):
    return f"career{n}.{f}"


def active_slots(state):
    """今の segment で使う欄と優先度。"""
    pri = {s: HARD for s in COMMON_HARD + SEGMENT_HARD[state.segment]}
    for n in range(1, CARD_COUNT + 1):
        for f in CARD_FIELDS:
            pri[card_key(n, f)] = CARD1 if n == 1 else CARD2
    pri.update(work_style=MID, direction=MID, team_style=LOW)
    return pri


def hard_slots(state):
    return [s for s, p in active_slots(state).items() if p == HARD]


def nfkc(s):
    return unicodedata.normalize("NFKC", str(s)).strip()


def chars(s):
    """字数(空白を数えない。§6.1 の本人役と同じ)。"""
    return len(re.sub(r"\s", "", str(s)))


def _load_json(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def _with_choices(q):
    """答えの候補のページ(pages)から、候補の全部(choices。ページの順)を作る。"""
    return dict(q, choices=[c for page in q["pages"] for c in page])


def load_bank():
    """質問バンク(データ)。プロンプトには書かない。職歴カードの問いは card_role などのひな形で、questions() が 1 枚ごとに広げる。
    どの問いも答えの候補を pages(1 ページ = 1 回に見せるボタン)で持つ(task 315)。最初のページを出し、「ほかの候補」で次の
    ページを出す。explain は聞き返されたときの説明、explain_choices はその説明に合わせて出す候補(無ければ今のページ)。
    topic は問いの短い名前で、「他の候補」の吹き出し(more_lead)に使う。"""
    return [_with_choices(q) for q in _load_json("bank.json")]


BANK = load_bank()


def bank_form(state):
    """質問バンクの forms で見る形(D は C と同じ問い)。"""
    return "C" if state.form in UI_FORMS else state.form


def fix_text(slot):
    """形 D のまとめの「直す」のボタンが送る文(本人の発言として出る)。"""
    if slot.startswith("career"):
        n, f = slot[len("career"):].split(".")
        return f"職歴{n}の{CARD_UI_LABELS[f]}を直す"
    return f"{SLOTS[slot]['label']}を直す"


def default_form():
    f = os.environ.get(FORM_ENV, DEFAULT_FORM).strip().upper()
    return f if f in FORMS else DEFAULT_FORM


# ----------------------------- 状態 -----------------------------
def entry(status, text="", value=None, number=None, rng=None, unit=None, confidence="high", frm=""):
    """欄の値。status は filled / passed / dont_know。text は本人の言葉(ボタンならラベル)。"""
    return {"status": status, "text": text, "value": value, "number": number, "range": rng, "unit": unit,
            "confidence": confidence, "from": frm}


@dataclass
class State:
    version: str = VERSION
    form: str = DEFAULT_FORM
    phase: str = "icebreak"
    segment: str = "unknown"
    has_resume: bool = False
    slots: dict = field(default_factory=dict)      # slot -> entry
    pending: dict = field(default_factory=dict)    # 今の segment で使わない欄に来た値(segment が決まったら当てる)
    user_ng: list = field(default_factory=list)
    said: list = field(default_factory=list)       # 本人の発言(押したボタンのラベルも)。下書きの数字の確かめに使う(draft.py)
    asked: list = field(default_factory=list)
    turns: int = 0
    phase_turns: dict = field(default_factory=dict)
    seconds: float = 0.0                           # 本人の推定時間(§3.3)
    clarify_used: int = 0
    clarified: list = field(default_factory=list)
    retried: list = field(default_factory=list)
    aside_count: int = 0
    current: dict = None
    review_rounds: int = 0
    last_card: dict = None
    review_log: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    stop_reason: str = None                        # review に進んだ理由: done / time / turns / show_result
    closed: str = None                             # later(「あとで」で終えた)
    result: dict = None
    log: list = field(default_factory=list)

    def to_dict(self):
        return copy.deepcopy({k: v for k, v in self.__dict__.items() if not k.startswith("_")})

    @classmethod
    def from_dict(cls, d):
        st = cls()
        for k, v in d.items():
            if hasattr(st, k):
                setattr(st, k, copy.deepcopy(v))
        return st


def new_state(form=None, prefill=None):
    """空の状態。prefill は履歴書の読み取り結果で埋まる欄({slot: {"text", "number", "range", "unit"}})。high として入れ、聞かない。"""
    st = State(form=form if form in FORMS else default_form())
    for s, v in (prefill or {}).items():
        if s not in ALL_SLOTS:
            continue
        st.slots[s] = entry("filled", str(v.get("text", "")), v.get("value"), v.get("number"), v.get("range"), v.get("unit"),
                            "high", "resume")
        st.has_resume = True
    return st


def apply_saved(state, segment=None, items=None, ng_known=False, bank=None):
    """前の相談で保存した segment と欄を入れる(task 255。§3 の「あとで」: 次の相談で聞かない)。欄は high で、from は saved。
    items は {slot: {"status", "text", "value", "number", "range", "unit"}}。履歴書ではないので、目標時間は変えない(has_resume)。
    segment だけの欄は、segment が決まっていなければ pending に入る(set_slot)。ng_known(避けたいことを保存している)なら、
    NG をねらう問い(逆選択)を出したことにして聞かない。保存のときに数字を確かめた欄は checked を持ち越す(下書きが本人の
    言葉として扱う。dip_mcp.career.draft.trusted)。checked の無い職歴カードの欄(印より前の版で保存した値と、トークの AI が
    直した値)は、入れる前に数字を、履歴書の欄と checked の欄の言葉と比べ、無い数字を数字の空欄にする(数と区切りも持たない)。
    まとめのカードと下書きに、確かめていない数字を出さないため。"""
    if segment in SEGMENTS:
        state.segment = segment
    if ng_known:
        state.asked += [q["id"] for q in (bank or BANK) if q["targets"] == ["__ng__"] and q["id"] not in state.asked]
    items = {s: v for s, v in (items or {}).items() if s in ALL_SLOTS}
    src = [*(e["text"] for e in state.slots.values() if e.get("from") == "resume" and e.get("text")),
           *(str(v.get("text") or "") for v in items.values() if v.get("checked"))]
    for s, v in items.items():
        status = v.get("status") if v.get("status") in STATUSES else "filled"
        text, number, value, rng = str(v.get("text") or ""), v.get("number"), v.get("value"), v.get("range")
        if s.startswith("career") and not v.get("checked"):
            text, replaced = check_numbers(text, src)
            if replaced:
                number = value = rng = None
        e = entry(status, text, value, number, rng, v.get("unit"), "high", "saved")
        if v.get("checked"):
            e["checked"] = True
        set_slot(state, s, e)


def ev(state, kind, **kw):
    state.log.append({"t": state.turns, "kind": kind, **kw})


def stop_at(state):
    """質問を止める時刻(§3.3)。履歴書あり 90 秒、なし 210 秒。"""
    return TARGET_SECONDS[state.has_resume] - REVIEW_TIME_RESERVE


def is_open(e):
    """まだ聞いていない(空の)欄。low の値は聞き直さず、まとめで確かめる。"""
    return e is None


def is_set(e):
    return e is not None and e.get("status") in STATUSES


def read_seconds(texts):
    return sum(chars(t) for t in texts) * 60 / READ_CHARS_PER_MIN


def type_seconds(text):
    return chars(text) * 60 / TYPE_CHARS_PER_MIN


# ----------------------------- 質問 -----------------------------
def _clip(text, n):
    """text が n 字を超えるなら、n 字か最初の数の手前の短い方で切って「…」を付ける。切った引用には数を残さない(task 248。
    「12…」「3億5…」「1兆…」のように、本人が言った数と違う数を見せない。字の集まりを足していく直し方をやめ、数を見せない側に倒した)。
    数の始まりは NUM_RUN_RE か KANJI_NUM_RE の始まり(「一般職」の「一」も数として扱う)。切った引用が KNOWN_ROLE_MIN 字より
    短ければ None を返し、呼ぶ側は決まった言い方(KNOWN_ROLE_FALLBACK)にする。n 字以内なら、数があっても本人の言葉のまま返す。"""
    if len(text) <= n:
        return text
    starts = [m.start() for m in (NUM_RUN_RE.search(text), KANJI_NUM_RE.search(text)) if m]
    quote = text[:min([n, *starts])].rstrip()
    return quote + "…" if len(quote) >= KNOWN_ROLE_MIN else None


def _expand(q, n, segment, state=None):
    """職歴カードのひな形を n 枚目の問いにする。何をしていたかが履歴書か前の相談から入っている(本人がこの会話で言っていない)
    カードの残りの問いは、どの職歴の話か分かるように、仕事の言葉を頭に付ける(text_known。「それで」だけでは何の話か分からないため)。"""
    def sub(s):
        return s.replace("{n}", str(n))
    which = "直近の" if n == 1 else "その前の"
    text = ((q.get("text_by_segment") or {}).get(segment) or {}).get(str(n)) or q["text"].replace("{which}", which)
    pages = ((q.get("pages_by_segment") or {}).get(segment) or {}).get(str(n)) or q["pages"]
    role = (state.slots.get(card_key(n, "role")) if state else None) or {}
    if q.get("text_known") and role.get("from") in ("resume", "saved") and role.get("status") == "filled" and role.get("text"):
        r = " ".join(nfkc(role["text"]).split())  # 改行や続く空白を 1 つの空白に(問いの文は 1 行。validate_reply が文のまま探す)
        quote = _clip(r, KNOWN_ROLE_CHARS)
        text = q["text_known"].replace("「{role}」", KNOWN_ROLE_FALLBACK) if quote is None else q["text_known"].replace("{role}", quote)
    return _with_choices(dict(q, id=f"{q['id']}{n}", card=n, text=text, targets=[sub(t) for t in q["targets"]], pages=pages,
                              explain=q.get("explain", "").replace("{which}", which),
                              choice_effects={c: {sub(k): v for k, v in eff.items()} for c, eff in q["choice_effects"].items()}))


def questions(state, bank):
    """今の形で出せる問い(職歴カードは 1 枚ずつ広げる。1 枚目の 3 問、2 枚目の 3 問の順)。"""
    out, cards = [], []
    for q in bank:
        if bank_form(state) not in q["forms"]:
            continue
        if q["phase"] == "career_cards":
            cards.append(q)
            continue
        out.append(q)
    for n in range(1, CARD_COUNT + 1):
        out += [_expand(q, n, state.segment, state) for q in cards]
    return out


def qmap(state, bank):
    return {q["id"]: q for q in questions(state, bank)}


def _segment_ok(q, state):
    return "all" in q["segments"] or state.segment in q["segments"]


def can_ask(state, slot):
    """時間とターンの予算(§3.3)。HARD は時刻を過ぎても聞き、review のための 2 ターンのうち 1 ターンまで使う。
    ほかは、質問を止める時刻の前で、review のための 2 ターンに入る前だけ。"""
    if slot == "segment" or active_slots(state).get(slot) == HARD:
        return state.turns < HARD_LAST_TURN
    return state.turns < MAX_TURNS - REVIEW_RESERVE and state.seconds <= stop_at(state)


def _card_skipped(state, slot):
    """何をしていたかをパスした(覚えていない)カードの、残りの欄は聞かない。"""
    if not slot.startswith("career") or slot.endswith(".role"):
        return False
    role = state.slots.get(slot.split(".")[0] + ".role")
    return role is not None and role["status"] in ("passed", "dont_know")


def score(state, q, budget=True):
    """出題のスコア。0 なら出さない。budget=False は予算を見ない(review に進んだ理由に使う)。"""
    pri = active_slots(state)
    if not _segment_ok(q, state):
        return 0.0
    if q["id"] == "q_segment":
        ok = state.segment == "unknown" and (not budget or can_ask(state, "segment"))
        return HARD + 0.5 if ok else 0.0
    if q["targets"] == ["__ng__"]:
        if state.user_ng or (state.slots.get("stop_doing") or {}).get("status") == "filled":
            return 0.0  # もう避けたいこと(NG、やめたいこと)を言った人には、逆選択を聞かない(聞き直しに見えるため)
        return float(LOW) if not budget or (state.turns < MAX_TURNS - REVIEW_RESERVE and state.seconds <= stop_at(state)) else 0.0
    total = 0.0
    for t in q["targets"]:
        if t in pri and is_open(state.slots.get(t)) and not _card_skipped(state, t) and (not budget or can_ask(state, t)):
            total += pri[t]
    return total


def pick_question(state, bank, phase, budget=True):
    """今のフェーズの問いから、スコアがいちばん高いものを選ぶ。hard_filter より後のフェーズでは hard_filter の問いも比べる:
    後から segment が決まったときの HARD を、職歴カードの続き・2 枚目・MID・LOW より先に聞く(§3.3 の優先度)。"""
    later = phase in ORDER and ORDER.index(phase) > ORDER.index("hard_filter")
    phases = {phase, "hard_filter"} if later else {phase}
    best, best_score = None, 0.0
    for q in questions(state, bank):  # 同点はバンクの並び順
        if q["phase"] not in phases or q["id"] in state.asked:
            continue
        s = score(state, q, budget)
        if s > best_score:
            best, best_score = q, s
    return best


def remaining(state, bank, budget=True):
    """まだ出していない、出せる問いの id(フェーズの順)。"""
    out = []
    for q in questions(state, bank):
        if q["phase"] in PRE_REVIEW and q["id"] not in state.asked and score(state, q, budget) > 0:
            out.append(q["id"])
    return out


def next_phase(state):
    p = state.phase
    if p == "result" or state.turns >= MAX_TURNS:
        return "result"
    if p == "review":
        more = getattr(state, "_need_more_review", False) and state.review_rounds < MAX_REVIEW_ROUNDS \
            and "show_result" not in state.flags
        return "review" if more else "result"
    if "show_result" in state.flags:
        return "review"
    if state.phase_turns.get(p, 0) >= PHASE_CAP.get(p, 99):
        return ORDER[ORDER.index(p) + 1]
    return p


def advance(state, bank):
    """次に出すものを決めて state.current に入れる。ORDER の長さで打ち切るので繰り返さない。"""
    for _ in range(len(ORDER) + 2):
        np = next_phase(state)
        state.phase = np
        if np in ("review", "result"):
            break
        q = pick_question(state, bank, np)
        if q:
            _present(state, q)
            return
        state.phase = ORDER[ORDER.index(np) + 1]
        if state.phase in ("review", "result"):
            break
    else:
        state.phase = "review"
    if state.phase == "review":
        if state.stop_reason is None:
            state.stop_reason = _why_stop(state, bank)
            ev(state, "stop", reason=state.stop_reason, seconds=round(state.seconds, 1))
        state.last_card = build_card(state)
        state.current = {"qid": "review", "mode": "review", "choices": [REVIEW_OK]}
    else:
        state.current = {"qid": "result", "mode": "result", "choices": []}


def _why_stop(state, bank):
    if "show_result" in state.flags:
        return "show_result"
    left = remaining(state, bank, budget=False)
    if not left:
        return "done"
    if state.turns >= MAX_TURNS - REVIEW_RESERVE:
        return "turns"
    if state.seconds > stop_at(state):
        return "time"
    return "done"  # フェーズの上限で出せなかった


def page_choices(q, cands, mode="ask", more=True):
    """ボタン: 答えの候補 → 「他の候補」→ スキップ(と「覚えていない」)。直す問い(mode=fix)は最後に「そのまま」。
    ヒアリングを終えるボタンは入れない(END_WORDS)。質問バンクのページは、ラベルの合計が LABELS_MAX に入る
    (tests/test_career_harness.py)。直す問い(形 D のまとめの「直す」だけ)は「そのまま」の 4 字だけ超えてよい。
    候補を削ると、押したいラベルが消えるため。"""
    tail = ([MORE] if more else []) + list(q.get("escape", [])) + ([FIX_KEEP] if mode == "fix" else [])
    return list(cands) + tail


def choices_of(q, mode="ask"):
    """問いを最初に出すときのボタン(候補の最初のページ)。"""
    return page_choices(q, q["pages"][0] if q.get("pages") else q["choices"], mode)


def fix_choices(q):
    """直す問いのボタン。「そのまま」で、前の値を変えずにまとめに戻れる。"""
    return choices_of(q, "fix")


def _shown_now(q, choices):
    return [c for c in choices if c in q["choices"]]


def ask_current(q, mode="ask", **kw):
    """問いを出すときの state.current。page は今のページ、shown はこの問いで見せた候補(「ほかの候補」で重ねないため)。"""
    ch = choices_of(q, mode)
    return {"qid": q["id"], "mode": mode, "choices": ch, "page": 0, "shown": _shown_now(q, ch), **kw}


def next_page(q, cur):
    """「ほかの候補」で出す (ページの番号, 候補)。見せた候補を除き、残りが無ければ (None, [])。"""
    shown = set(cur.get("shown") or [])
    pages = q.get("pages") or []
    for i in range(cur.get("page", 0) + 1, len(pages)):
        cands = [c for c in pages[i] if c not in shown]
        if cands:
            return i, cands
    return None, []


def _present(state, q, mode="ask"):
    pri = active_slots(state)
    if q["id"] not in state.asked:
        state.asked.append(q["id"])
    ev(state, "ask", qid=q["id"], phase=q["phase"], mode=mode, targets=list(q["targets"]),
       open=[t for t in q["targets"] if t in pri and is_open(state.slots.get(t))],
       hard=[t for t in q["targets"] if pri.get(t) == HARD or t == "segment"],
       segments=list(q["segments"]), segment=state.segment, turns=state.turns, seconds=round(state.seconds, 1))
    state.current = ask_current(q, mode)


# ----------------------------- マージ -----------------------------
def set_slot(state, slot, e):
    """戻り値は set / pending / kept / discarded。high の値は low で上書きしない。"""
    if slot not in ALL_SLOTS:
        ev(state, "discarded_unknown_slot", slot=slot)
        return "discarded"
    if slot not in active_slots(state):
        state.pending[slot] = e
        ev(state, "pending", slot=slot)
        return "pending"
    old = state.slots.get(slot)
    if old is not None and old["confidence"] == "high" and e["confidence"] == "low":
        ev(state, "kept_high", slot=slot)
        return "kept"
    if SLOTS[slot]["kind"] == "multi" and old is not None and isinstance(old.get("value"), list) \
            and isinstance(e.get("value"), list) and old["confidence"] == e["confidence"]:
        merged = old["value"] + [v for v in e["value"] if v not in old["value"]]
        e = dict(e, value=merged, text="・".join(SLOTS[slot]["values"].get(v, v) for v in merged))
    state.slots[slot] = e
    ev(state, "set", slot=slot, status=e["status"], confidence=e["confidence"], frm=e["from"])
    return "set"


def set_segment(state, seg, source):
    """segment は unknown のときだけ、本人の答えから決める。review でははっきり直したときに変える。"""
    if seg not in SEGMENTS or seg == state.segment:
        return "noop"
    if state.segment != "unknown" and source != "review":
        ev(state, "segment_change_ignored", old=state.segment, new=seg)
        return "ignored"
    old = state.segment
    state.segment = seg
    ev(state, "segment_set", old=old, new=seg, source=source)
    for s in list(state.pending):
        if s in active_slots(state):
            set_slot(state, s, state.pending.pop(s))
    return "set"


def confidence_for(phase, src):
    """確信度はハーネスが決める。直接聞いた問いへの答え・review・履歴書は high、ほかの問いへの文と場面の二択は low。"""
    if src in ("review", "resume"):
        return "high"
    if src == "current" and phase in DIRECT:
        return "high"
    return "low"


def _apply_choice(state, q, label, text=None, src="button"):
    """選択肢の効き目を当てる。text は本人の言葉(打った文が選択肢と同じ意味だったとき)。"""
    conf = confidence_for(q["phase"], "current")
    effects = q["choice_effects"].get(label)
    if effects is None:  # 効き目を書いていない候補は、ラベルをそのまま問いの欄の値にする(本人が選んだ言葉)
        effects = {t: {} for t in q["targets"] if t in ALL_SLOTS} if label in q["choices"] else {}
    for k, eff in effects.items():
        if k == "__segment__":
            set_segment(state, eff, "choice")
        elif k == "__ng__":
            for n in eff:
                if n not in state.user_ng:
                    state.user_ng.append(n)
        elif "value" in eff:  # 場面の二択・MID の直接の問い: 推し量った値(正規値)
            v = eff["value"]
            ja = "・".join(SLOTS[k]["values"].get(x, x) for x in v) if isinstance(v, list) else SLOTS[k]["values"].get(v, v)
            set_slot(state, k, entry("filled", text or ja, v, confidence=conf, frm=f"choice:{q['id']}"))
        elif eff.get("nopref"):
            set_slot(state, k, entry("filled", text or label, NOPREF, confidence=conf, frm=f"choice:{q['id']}"))
        else:
            set_slot(state, k, entry("filled", text or label, label, rng=eff.get("range"), unit=eff.get("unit"),
                                     confidence=conf, frm=f"{src}:{q['id']}"))


def _apply_escape(state, q, label):
    """パス・覚えていない。もう聞かない(条件の欄のパスは「こだわりなし」)。"""
    if q["id"] == "q_segment":
        _flag(state, "segment_passed")
        return
    for t in q["targets"]:
        if t not in ALL_SLOTS:
            continue
        if label == DONT_KNOW:
            set_slot(state, t, entry("dont_know", DONT_KNOW, frm=f"escape:{q['id']}"))
        elif q["phase"] in DIRECT:
            set_slot(state, t, entry("passed", PASS, NOPREF if t in CONDITION_SLOTS else None, frm=f"escape:{q['id']}"))
    if q["phase"] not in DIRECT:
        ev(state, "scenario_passed", qid=q["id"])


# ----------------------------- LLM の出力の検証 -----------------------------
def parse_json(raw):
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


def _empty_extract():
    return {"slot_updates": [], "ng_updates": [], "segment": None, "intent": "answer", "review_ack": None}


def validate_extract(raw):
    """-> (clean, problems)。使えなければ (None, problems)。中身(発言の部分文字列か、数が発言にあるか)は当てるときに見る。"""
    d = parse_json(raw)
    if d is None:
        return None, ["not_json"]
    probs, out = [], _empty_extract()
    su = d.get("slot_updates")
    if isinstance(su, dict):
        su = [{"slot": k, **(v if isinstance(v, dict) else {"text": v})} for k, v in su.items()]
    if not isinstance(su, list):
        probs.append("slot_updates_bad_type" if su is not None else "slot_updates_missing")
        su = []
    for u in su[:10]:
        if not isinstance(u, dict) or not isinstance(u.get("slot"), str):
            probs.append("update_bad_item")
            continue
        slot = u["slot"].strip()
        if slot == "segment":
            d.setdefault("segment", u.get("text") or u.get("value"))
            continue
        if slot not in ALL_SLOTS:
            probs.append("unknown_slot:" + slot)
            continue
        num = u.get("number")
        if isinstance(num, bool) or not isinstance(num, (int, float)):
            num = None
        text = u.get("text") if isinstance(u.get("text"), str) else (u.get("value") if isinstance(u.get("value"), str) else "")
        out["slot_updates"].append({"slot": slot, "text": nfkc(text), "number": num,
                                    "unit": u["unit"] if isinstance(u.get("unit"), str) and u["unit"].strip() else None,
                                    "choice": u["choice"] if isinstance(u.get("choice"), str) else None})
    ng = d.get("ng_updates") or []
    ng = [ng] if isinstance(ng, str) else ng
    out["ng_updates"] = [nfkc(x) for x in ng if isinstance(x, str) and nfkc(x)][:5] if isinstance(ng, list) else []
    seg = d.get("segment")
    out["segment"] = seg if seg in SEGMENTS else None
    if isinstance(seg, str) and seg not in SEGMENTS and seg not in ("null", "unknown", ""):
        probs.append("invalid_segment:" + seg)
    it = d.get("intent", "answer")
    out["intent"] = it if it in INTENTS else "answer"
    ra = d.get("review_ack")
    out["review_ack"] = ra if ra in ("all", "partial", "none") else None
    return out, probs


def digits_in(text):
    t = nfkc(text).replace(",", "")
    return set(re.findall(r"\d+(?:\.\d+)?", t))


def number_in_text(number, text):
    """数が発言に数字で書いてあるか(数を作らない)。"""
    if number is None:
        return False
    s = str(int(number)) if float(number).is_integer() else str(number)
    return s in digits_in(text)


def validate_reply(raw, v):
    d = parse_json(raw)
    if d:
        reply = d.get("reply")
    else:
        reply = raw if isinstance(raw, str) and raw.strip() and not raw.strip().startswith("{") else None
    if not isinstance(reply, str) or not reply.strip():
        return None, "no_reply"
    reply = reply.strip()
    q = v.get("text", "")
    if nfkc(q) not in nfkc(reply):
        return None, "question_missing"
    if chars(reply) - chars(q) > REACTION_MAX:
        return None, "too_much_extra"
    if chars(bubble(v, reply)) > BUBBLE_MAX:
        return None, "too_long"
    if PRAISE_RE.search(reply):
        return None, "praise"
    said = q + v.get("user_text", "")
    if digits_in(reply) - digits_in(said) or set(KANJI_NUM_RE.findall(reply)) - set(KANJI_NUM_RE.findall(said)):
        return None, "made_up_number"
    if len(QMARK_RE.findall(reply)) > max(1, len(QMARK_RE.findall(q))):
        return None, "two_questions"
    return reply, None


def bubble(v, reply):
    """1 つの吹き出し: 前の答えの 1 行(職歴カードの「✓職歴」、飛ばした問い)→ 受け止め → フェーズの区切り(PHASE_LINES)→ 問い。
    区切りを受け止めより先に出すと、受け止めが次の問いの前置きに読める(task 326)。受け止めは、reply の中の問いの文より前の部分。
    問いの文が見つからなければ(検証の前の reply)、区切りも reply の前に置く。"""
    progress = list(v.get("progress", []))
    phase = [x for x in progress if x in PHASE_LINES.values()]
    past = [x for x in progress if x not in phase]
    q = v.get("text", "")
    i = reply.find(q) if phase and q else -1
    if i < 0 and phase and q and nfkc(q) in nfkc(reply):  # 全角と半角の違い(validate_reply は NFKC で問いの文を探す)
        reply, i = nfkc(reply), nfkc(reply).find(nfkc(q))
    if i < 0:
        return "\n".join([*past, *phase, reply])
    reaction = reply[:i].strip()
    return "\n".join([*past, *([reaction] if reaction else []), *phase, reply[i:]])


# ----------------------------- まとめ(review) -----------------------------
def _disp(slot, e, width):
    if e is None:
        return "未定"
    if e["status"] == "passed":
        return NOPREF if e.get("value") == NOPREF else PASS
    if e["status"] == "dont_know":
        return DONT_KNOW
    t = e.get("text") or ""
    t = t if chars(t) <= width else t[:width] + "…"
    return t + ("(推測)" if e["confidence"] == "low" else "")


def card_lines(state, width, keep_mid=True, keep_low=True, focus=False):
    return [line for _, line in card_rows(state, width, keep_mid, keep_low, focus)]


def card_rows(state, width, keep_mid=True, keep_low=True, focus=False):
    """まとめのカードの行 [(行の欄, 行)]。欄は形 D の「直す」のボタンに使う(NG の行は欄を持たない)。"""
    pri = active_slots(state)
    lines = []
    for s, p in pri.items():
        if s.startswith("career"):
            continue
        e = state.slots.get(s)
        if focus and not (p == HARD and (e is None or e["confidence"] == "low")):
            continue
        if p == MID and (not keep_mid or e is None) or p == LOW and (not keep_low or e is None):
            continue  # 聞かなかった MID と LOW は出さない(「未定」の行が、急いでいる本人に長い文を打たせるため。task 248)
        lines.append(([s], f"{SLOTS[s]['short']}: {_disp(s, e, width)}"))
    if not focus:
        for n in range(1, CARD_COUNT + 1):
            es = [state.slots.get(card_key(n, f)) for f in CARD_FIELDS]
            if all(e is None for e in es) or (es[0] is not None and es[0]["status"] != "filled" and all(e is None for e in es[1:])):
                continue
            keys = [card_key(n, f) for f in CARD_FIELDS if not _card_skipped(state, card_key(n, f))]
            # 職歴カードの聞かなかった欄も出さない(空欄は下書きの画面で埋める。§4)
            lines.append((keys, f"職歴{n}: " + " / ".join(_disp(card_key(n, f), e, width)
                                                          for f, e in zip(CARD_FIELDS, es, strict=True) if e is not None)))
        if state.user_ng:
            lines.append(([], "NG: " + "、".join(n if chars(n) <= width else n[:width] + "…" for n in state.user_ng)))
    return [(k, x if chars(x) <= BUBBLE_MAX else x[:BUBBLE_MAX - 1] + "…") for k, x in lines]


def build_card(state):
    """まとめのカード。文(REVIEW_LEAD)とカードの中の字を合わせて CARD_MAX まで。長ければ値を短くし、LOW と MID の行を落とす。"""
    focus = state.review_rounds >= 1
    lead = REVIEW_FOCUS_LEAD if focus else REVIEW_LEAD_UI if state.form in UI_FORMS else REVIEW_LEAD
    for width, mid, low in ((16, True, True), (12, True, True), (12, True, False), (8, True, False), (8, False, False),
                            (5, False, False)):
        rows = card_rows(state, width, mid, low, focus)
        lines = [x for _, x in rows]
        if chars(lead) + sum(chars(x) for x in lines) <= CARD_MAX:
            break
    return {"round": state.review_rounds + 1, "lead": lead, "lines": lines, "focus": focus, "shown": list(active_slots(state)),
            "slots": [k for k, _ in rows]}


def review_bubbles(state, card):
    """まとめの吹き出し。形 B と C は行を 80 字までの吹き出しに分けて送り、D は文だけ(行は Flex。dip_mcp/career/ui.py)。"""
    return [card["lead"]] if state.form in UI_FORMS else [card["lead"], *pack_bubbles(card["lines"])]


def fix_targets(state):
    """形 D のまとめと直している間に、「直す」のボタンが送る文 → 欄。今の segment で使う欄なら、前のまとめの Flex に残った
    ボタンも受ける(直している間に別の「直す」を押した、前の回のまとめのボタンを押した)。何をしていたかをパスした職歴カードの
    残りの欄は受けない。"""
    if state.form not in UI_FORMS or (state.current or {}).get("mode") not in ("review", "fix"):
        return {}
    return {nfkc(fix_text(s)): s for s in active_slots(state) if not _card_skipped(state, s)}


def can_fix(state):
    """まだ直せるか。直した答えは review のターンに数えるので、まとめの「これでOK」の 1 ターンを残す(まとめを必ず確かめる)。"""
    return state.turns < MAX_TURNS - 1


def fix_question(state, bank, slot):
    """欄を直すときに出す問い。直接聞くフェーズの問いを先に(場面の二択しか無い欄は、その問い)。"""
    qs = [q for q in questions(state, bank) if slot in q["targets"]]
    qs.sort(key=lambda q: q["phase"] not in DIRECT)
    return qs[0] if qs else None


def pack_bubbles(lines, limit=BUBBLE_MAX):
    """カードの行を、吹き出し 1 つが limit 字までになるように束ねる(形 B と C。D は Flex にする)。"""
    out, cur = [], []
    for line in lines:
        if cur and chars("".join(cur + [line])) > limit:
            out.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


def apply_review(state, ext, button_ok, user_text, promote=True):
    state.review_rounds += 1
    ack = "all" if button_ok else (ext or {}).get("review_ack")
    touched = set()
    if ext and not button_ok:
        if ext.get("segment"):
            set_segment(state, ext["segment"], "review")
        for u in ext["slot_updates"]:
            e = _free_entry(state, u, user_text, "review")
            if e is not None:
                set_slot(state, u["slot"], e)
                touched.add(u["slot"])
        _apply_ng(state, ext, user_text)
    promoted = []
    pri = active_slots(state)
    for s, e in list(state.slots.items()) if promote else ():
        if s in touched or e["confidence"] != "low" or s not in pri:
            continue
        if pri[s] == HARD and ack != "all":
            continue  # HARD の推測は「これでOK」(all)のときだけ確かめたことにする
        if ack in ("all", "partial"):
            e["confidence"] = "high"
            e["from"] = "review_accepted"
            promoted.append(s)
    state.review_log.append({"round": state.review_rounds, "ack": ack, "touched": sorted(touched), "promoted": promoted})
    open_hard = [s for s in hard_slots(state) if (state.slots.get(s) or {}).get("confidence", "low") == "low"]
    state._need_more_review = state.review_rounds == 1 and bool(open_hard)


# ----------------------------- 文の答え -----------------------------
def _free_entry(state, u, user_text, src, whole=False, phase=None):
    """抽出の 1 件から欄の値を作る。whole=True(今の質問への答え)は本人の文をそのまま入れる。ほかは抜き出した部分が発言に
    あるときだけ。数は発言に数字で書いてあるときだけ。"""
    text = nfkc(user_text) if whole else u["text"]
    if not text or (not whole and text not in nfkc(user_text)):
        ev(state, "evidence_mismatch", slot=u["slot"])
        return None
    num = u["number"] if number_in_text(u["number"], user_text) else None
    if u["number"] is not None and num is None:
        ev(state, "number_not_in_text", slot=u["slot"])
    unit = u["unit"] if num is not None and u["unit"] and nfkc(u["unit"]) in nfkc(user_text) else None
    conf = confidence_for(phase or state.phase, src)
    if whole and SLOTS[u["slot"]]["kind"] == "number" and num is None:
        conf = "low"  # 数の問いで、聞き直しても数が読めなかった: 本人の文を low で入れ、まとめで確かめる
    return entry("filled", text, number=num, unit=unit, confidence=conf,
                 frm=f"free_{src}:{(state.current or {}).get('qid')}")


def _apply_ng(state, ext, user_text):
    for n in ext["ng_updates"]:
        if n in nfkc(user_text) and n not in state.user_ng:
            state.user_ng.append(n)


def _apply_text(state, q, ext, user_text, retry=True):
    """今の質問への文の答えを当てる。戻り値は、今の質問の欄が埋まったか。retry=False は聞き直さないとき(「あとで」)。"""
    if ext.get("segment"):
        set_segment(state, ext["segment"], "choice" if q["id"] == "q_segment" else "free")
    answered = q["id"] == "q_segment" and state.segment != "unknown"
    if q["targets"] == ["__ng__"]:
        n = len(state.user_ng)
        _apply_ng(state, ext, user_text)
        return len(state.user_ng) > n
    for u in ext["slot_updates"]:
        current = u["slot"] in q["targets"]
        number_kind = SLOTS[u["slot"]]["kind"] == "number"
        if current and u.get("choice") in q["choices"] and not (number_kind and number_in_text(u["number"], user_text)) \
                and (not number_kind or digits_in(u["choice"]) <= digits_in(user_text)):
            # 打った文がボタンと同じ意味。数の区切りは、ラベルの数が発言に数字で書いてあるときだけ(「四百万」は区切りにしない)
            _apply_choice(state, q, u["choice"], text=nfkc(user_text), src="free")
            answered = True
            continue
        if current and number_kind and not number_in_text(u["number"], user_text) \
                and retry and f"num:{q['id']}" not in state.retried:
            continue  # 数の問いで数が読めない: ボタンで聞き直す(1 回だけ)
        e = _free_entry(state, u, user_text, "current" if current else "other", whole=current, phase=q["phase"])
        if e is not None:
            if not current and not is_open(state.slots.get(u["slot"])):
                ev(state, "kept_existing", slot=u["slot"])
                continue
            set_slot(state, u["slot"], e)
            answered = answered or current
    _apply_ng(state, ext, user_text)
    return answered


def _give_up(state, q, user_text):
    """2 回とも読めなかった答え: 本人の文を low で入れ、まとめで確かめる(聞き直さない)。"""
    ev(state, "extract_gave_up", qid=q["id"])
    for t in q["targets"]:
        if t in ALL_SLOTS and is_open(state.slots.get(t)):
            set_slot(state, t, entry("filled", nfkc(user_text), confidence="low", frm=f"free_unparsed:{q['id']}"))


# ----------------------------- ターン -----------------------------
def _init_volatile(state, reset=False):
    """そのターン限りの値(先頭が _。保存しない)。"""
    for k, v in (("_need_more_review", False), ("_user_text", ""), ("_progress", None)):
        if reset or not hasattr(state, k):
            setattr(state, k, [] if v is None else v)


def start(state, bank=BANK):
    """最初の質問(ターンに数えない)。何回呼んでも同じものを返す。読む時間は 1 回目だけ数える。"""
    _init_volatile(state)
    first = state.current is None
    if first and state.form in UI_FORMS:  # 形 D: 先に「始める」の 1 枚(最初の問いは「始める」の後)
        state.current = {"qid": "start", "mode": "start", "choices": [START_GO, LATER]}
    elif first:
        advance(state, bank)
    lead = (START_LEAD_UI if state.form in UI_FORMS else START_LEAD) if state.turns == 0 else None
    out = _represent(state, bank, counted=False, lead=lead, read=False)
    if not first:
        return out
    state.seconds += read_seconds(out["bubbles"] + out["choices"])
    return out


def match_button(state, text):
    """今のボタンか、この問いで前に見せた候補(「他の候補」で隠れた前のページ。LINE は前のボタンを消すので、打ち直した)と同じ文。"""
    t = nfkc(text)
    cur = state.current or {}
    for c in [*cur.get("choices", []), *cur.get("shown", [])]:
        if nfkc(c) == t:
            return c
    return None


def _call_llm(fn, payload, validator, state, stage):
    probs_all = []
    for attempt in range(1 + LLM_RETRIES):
        try:
            raw = fn(payload)
        except Exception as e:  # noqa: BLE001  モデル・通信の失敗で落とさない(失敗 1 回として数える)
            raw = None
            probs_all.append(f"exception:{type(e).__name__}")
        res, probs = validator(raw)
        if res is not None:
            if probs:
                ev(state, f"{stage}_repaired", problems=probs)
            if attempt:
                ev(state, f"{stage}_retry_ok")
            return res
        probs_all += probs if isinstance(probs, list) else [probs]
    ev(state, f"{stage}_fallback", problems=probs_all)
    return None


def _count(state, bucket=None):
    """数えるターン: turns と phase_turns[計上先] を 1 つ足す(sum(phase_turns) == turns)。"""
    b = bucket or state.phase
    state._counted_phase = b
    state.turns += 1
    state.phase_turns[b] = state.phase_turns.get(b, 0) + 1


def _uncount(state):
    p = getattr(state, "_counted_phase", state.phase)
    state.turns -= 1
    state.phase_turns[p] -= 1


def _flag(state, key):
    if key not in state.flags:
        state.flags.append(key)


def turn(state, user_text, bank=BANK, llm=None):
    """本人の発言 1 通を処理して {reply, bubbles, choices, card, done, counted, closed, summary} を返す。例外は出さない:
    ハーネスの中の例外は、ターンの始めの状態に戻して、今出しているものを出し直す。"""
    _init_volatile(state)
    snap = state.to_dict()
    try:
        return _turn(state, user_text, bank, llm)
    except Exception as e:  # noqa: BLE001
        restored = State.from_dict(snap)
        state.__dict__.clear()
        state.__dict__.update(restored.__dict__)
        _init_volatile(state, reset=True)
        ev(state, "internal_error", error=type(e).__name__)
        return _represent(state, bank, counted=False)


def extract_payload(state, bank, user_text):
    cur = state.current or {}
    pri = active_slots(state)
    phase = state.phase
    if cur.get("mode") == "review":
        card = state.last_card or build_card(state)
        question = "\n".join([card["lead"], *card["lines"]])
        targets, kind = list(card["shown"]), "review"
    else:
        q = qmap(state, bank).get(cur.get("qid")) or {}
        question, targets, kind = q.get("text", ""), list(q.get("targets", [])), q.get("kind")
        phase = q.get("phase", phase)  # 後のフェーズで聞く hard_filter の問いは hard_filter として
    return {"phase": phase, "segment": state.segment, "mode": cur.get("mode"), "kind": kind, "question": question,
            "choices": list(dict.fromkeys([*cur.get("choices", []), *cur.get("shown", [])])), "targets": targets,
            "user_text": user_text,
            "allowed_slots": {s: SLOTS[s]["label"] for s in pri}}


def _turn(state, user_text, bank, llm):
    text = user_text if isinstance(user_text, str) else ""
    state._user_text = text
    state._progress = []
    if state.current is None:
        advance(state, bank)
    if state.closed or state.result:
        return _stored(state)
    if not nfkc(text):
        ev(state, "empty_input")
        return _represent(state, bank, counted=False)
    state.said.append(text)
    cur = state.current
    q = qmap(state, bank).get(cur["qid"])
    button = match_button(state, text)
    button = LEGACY_BUTTONS.get(button, button)
    fix = fix_targets(state).get(nfkc(text))
    state.seconds += BUTTON_SECONDS if button or fix else type_seconds(text)
    if button == LATER or (button is None and fix is None and nfkc(text) in END_WORDS):
        return _close(state, "later")
    if cur["mode"] == "start":  # 形 D の「始める」: 最初の問いを出す。打った文は、最初の問いへの答えとして読む
        advance(state, bank)
        if button is not None:
            ev(state, "started")
            return _represent(state, bank, counted=False)
        cur = state.current
        q = qmap(state, bank).get(cur["qid"])
        button = match_button(state, text)
    if button == MORE and q is not None:
        return _more(state, q)
    if fix is not None:  # 形 D のまとめの「直す」: その欄の問いを出す(数えない)
        fq = fix_question(state, bank, fix)
        if fq is not None and can_fix(state):
            state.current = ask_current(fq, "fix", slot=fix)
            ev(state, "fix", slot=fix, qid=fq["id"])
            return _represent(state, bank, counted=False, lead=FIX_LEAD)
        ev(state, "fix_refused", slot=fix)  # ターンの予算が尽きた: まとめを出し直す(文で直すのは受ける)
        _back_to_review(state)
        return _represent(state, bank, counted=False, lead=FIX_LIMIT_LEAD)
    ext = None
    if button is None:
        ext = _call_llm(llm.extract, extract_payload(state, bank, text), validate_extract, state, "extract")
    intent = (ext or {}).get("intent", "answer")
    if intent == "later":
        if ext is not None and q is not None and cur["mode"] != "review":
            _apply_text(state, q, ext, text, retry=False)  # 「あとで」と一緒に打った答えも残す(次は聞かない)
        return _close(state, "later")
    if intent == "aside":
        return _aside(state, bank, llm)
    if intent == "more" and q is not None and cur["mode"] not in ("review", "start"):  # 「ほかには?」と打った
        return _more(state, q)
    if cur["mode"] == "fix":
        _count(state, "review")
        if button == FIX_KEEP:  # 直すのをやめる: 値も確かさも変えずにまとめに戻る
            ev(state, "fix_kept", slot=cur["slot"], by="keep")
            _back_to_review(state)
            return _say(state, bank, llm)
        _apply_fix(state, q, cur["slot"], button, ext, intent, text)
        _back_to_review(state)  # 直した値でまとめを作り直す(まとめの回数は増やさない。can_fix で 1 ターンを残してある)
        return _say(state, bank, llm)
    _count(state, q["phase"] if q is not None and cur["mode"] != "review" else None)  # 問いのフェーズに数える
    if cur["mode"] == "review":
        if button is None and ext is None:
            key = f"review{state.review_rounds + 1}"
            if key not in state.retried:  # 1 回目: 数えずにまとめを出し直す
                state.retried.append(key)
                _uncount(state)
                return _represent(state, bank, counted=False, lead=RETRY_TEXT)
            apply_review(state, None, button_ok=False, user_text=text, promote=False)
        else:
            apply_review(state, ext, button_ok=button == REVIEW_OK, user_text=text)
        if intent == "show_result":
            state._need_more_review = False
        advance(state, bank)
        return _say(state, bank, llm)
    before = {k: copy.deepcopy(v) for k, v in state.slots.items()}
    skipped = None
    if button is not None:
        if button in q.get("escape", []):
            _apply_escape(state, q, button)
        else:
            _apply_choice(state, q, button)
    elif intent in ("pass", "dont_know"):
        _apply_escape(state, q, DONT_KNOW if intent == "dont_know" and DONT_KNOW in q.get("escape", []) else PASS)
    elif intent == "clarify" and state.clarified.count(cur["qid"]) < MAX_CLARIFY:
        _uncount(state)  # 説明は数えない(答えていないので。聞き返しで HARD が職歴カードの後ろに押し出されない)
        _clarify(state, q)
        return _say(state, bank, llm, counted=False)
    elif intent == "clarify":  # 同じ問いの 3 回目の聞き返し: 空のまま次へ(HARD ならまとめの「ここだけ」で聞く)
        ev(state, "clarify_unanswered", qid=q["id"])
        skipped = CLARIFY_SKIP
    else:
        answered = ext is not None and _apply_text(state, q, ext, text)
        if not answered and intent == "answer":
            if q["id"] not in state.retried:  # 1 回目: 数えずに、ボタンで聞き直す
                state.retried += [q["id"], f"num:{q['id']}"]
                _uncount(state)
                state.current = dict(cur, mode="retry")
                return _say(state, bank, llm, counted=False)
            _give_up(state, q, text)
    if intent == "show_result":
        _flag(state, "show_result")
    phase_before = state.phase
    advance(state, bank)
    if state.form != "B":
        _progress(state, before, phase_before)
    if skipped:
        state._progress = [skipped, *state._progress][-2:]
    return _say(state, bank, llm)


def more_lead(q):
    """「他の候補」の吹き出し(MORE_LEAD)。問いの文は繰り返さない(上の吹き出しにある)。"""
    return MORE_LEAD.format(topic=q["topic"], noun=MORE_NOUN.get(q.get("kind"), MORE_NOUN_DEFAULT))


def _more(state, q):
    """「ほかの候補」: 見せていない候補の次のページを出す(数えない。問いの文は上の吹き出しにあるので、何の候補かの短い前置きだけ)。
    出し尽くしたら、最初のページに戻し、文で送るよう案内する(「ほかの候補」はもう出さない)。"""
    cur = state.current
    i, cands = next_page(q, cur)
    if i is None:
        cur.update(page=len(q["pages"]), choices=page_choices(q, q["pages"][0], cur["mode"], more=False))
        lead = MORE_DONE
    else:
        cur.update(page=i, choices=page_choices(q, cands, cur["mode"]))
        cur["shown"] = list(dict.fromkeys([*cur.get("shown", []), *_shown_now(q, cur["choices"])]))
        lead = more_lead(q)
    ev(state, "more", qid=q["id"], page=cur["page"], done=i is None)
    return _out(state, [lead], cur["choices"], counted=False)


def _clarify(state, q):
    """聞き返し: 1 回目は問いの意味の説明(explain)と、その説明に合う候補(explain_choices、無ければ今の候補)。
    2 回目は、見せていない候補(無ければ今の候補)と CLARIFY_AGAIN。どちらも問いの文を繰り返さない。"""
    cur = state.current
    n = state.clarified.count(q["id"])
    state.clarified.append(q["id"])
    state.clarify_used += 1
    new = dict(cur, mode="clarify")
    if n == 0:
        cands = q.get("explain_choices") or _shown_now(q, cur["choices"])
        new["text"] = q.get("explain") or q["text"]
    else:
        i, cands = next_page(q, cur)
        if i is not None:
            new["page"] = i
        cands = cands or _shown_now(q, cur["choices"])
        new["text"] = CLARIFY_AGAIN
    new["choices"] = page_choices(q, cands, more=new.get("page", 0) < len(q["pages"]))
    new["shown"] = list(dict.fromkeys([*cur.get("shown", []), *_shown_now(q, new["choices"])]))
    state.current = new
    ev(state, "clarify", qid=q["id"], n=n + 1)


def _back_to_review(state):
    state.phase = "review"
    state.last_card = build_card(state)
    state.current = {"qid": "review", "mode": "review", "choices": [REVIEW_OK]}


def _apply_fix(state, q, slot, button, ext, intent, text):
    """形 D のまとめで選んだ欄への答え。前の値を外してから当て、何も入らなければ前の値に戻す。入った値は、本人が直接直したので
    high(場面の二択しか無い欄も)。パスと「覚えていない」は、前の値が本人の打った値(high)でも、問いのフェーズによらず空欄
    (条件の欄は「こだわりなし」)にする。本人が「直す」を選んでから空にしたので(推し量った前の値を残すと、「これでOK」で
    確かめたことになる)。値を残して戻りたいときは「そのまま」(FIX_KEEP)を押す。_turn がここを呼ばずにまとめに戻す。"""
    old = state.slots.pop(slot, None)
    escape = button if button is not None and button in q.get("escape", []) else \
        (DONT_KNOW if intent == "dont_know" and DONT_KNOW in q.get("escape", []) else PASS) \
        if button is None and intent in ("pass", "dont_know") else None
    if escape == DONT_KNOW:
        state.slots[slot] = entry("dont_know", DONT_KNOW, frm=f"fix:{q['id']}")
    elif escape is not None:
        state.slots[slot] = entry("passed", PASS, NOPREF if slot in CONDITION_SLOTS else None, frm=f"fix:{q['id']}")
    elif button is not None:
        _apply_choice(state, q, button)
    elif ext is not None:
        _apply_text(state, q, ext, text, retry=False)
        if state.slots.get(slot) is None and intent == "answer" and nfkc(text):
            state.slots[slot] = entry("filled", nfkc(text), frm=f"fix:{q['id']}")
    e = state.slots.get(slot)
    if e is None:
        if old is not None:
            state.slots[slot] = old
        ev(state, "fix_kept", slot=slot)
        return
    e["confidence"], e["from"] = "high", f"fix:{q['id']}"
    ev(state, "fixed", slot=slot, status=e["status"])


def _progress(state, before, phase_before):
    """形 C: 職歴カードの欄が埋まったら、本人の言葉の 1 行と「n/3」。フェーズが変わったら、次に何を聞くかの 1 行(PHASE_LINES)。
    残りの問いの数は出さない(task 288)。形 D はフェーズの 1 行だけ(職歴カードは Flex が見せる)。"""
    out = []
    for n in range(1, CARD_COUNT + 1) if state.form not in UI_FORMS else ():
        for f in CARD_FIELDS:
            k = card_key(n, f)
            e = state.slots.get(k)
            if e is not None and before.get(k) is None and e["status"] == "filled":
                words = e["text"] if chars(e["text"]) <= PROGRESS_WORDS else e["text"][:PROGRESS_WORDS] + "…"
                done = sum(1 for x in CARD_FIELDS if state.slots.get(card_key(n, x)) is not None)
                out.append(f"✓職歴{n}「{words}」{done}/3")
    if state.phase != phase_before and state.phase in PHASE_LINES:
        out.append(PHASE_LINES[state.phase])
    state._progress = out[-2:]


def _close(state, why):
    state.closed = why
    state.phase = "result"
    state.current = {"qid": "closed", "mode": "closed", "choices": []}
    ev(state, "closed", why=why, turns=state.turns, seconds=round(state.seconds, 1))
    return _out(state, [LATER_TEXT], [], counted=False, done=True)


def _stored(state):
    text = state.result["text"] if state.result else (LATER_TEXT if state.closed == "later" else CLOSED_TEXT)
    return _out(state, [text], [], counted=False, done=True, read=False)


def _aside(state, bank, llm):
    """雑談・逆質問: 状態を変えずに、今のものを出し直す。"""
    state.aside_count += 1
    # MAX_ASIDES を超えた分は数える。ただし review のための 2 ターンと review の中では数えない(review を必ず 1 回出すため)
    counted = state.aside_count > MAX_ASIDES and state.turns < MAX_TURNS - REVIEW_RESERVE \
        and state.current["mode"] not in ("review", "result", "fix")
    if counted:
        _count(state, "aside")
    if state.current["mode"] in ("review", "result"):
        return _represent(state, bank, counted=counted, lead=ASIDE_FALLBACK)
    return _say(state, bank, llm, counted=counted, aside=True)


def view(state, bank, aside=False):
    """生成段の入力。"""
    cur = state.current or {}
    mode = cur.get("mode")
    q = qmap(state, bank).get(cur.get("qid"))
    text = q["text"] if q else ""
    if mode == "retry":
        text = RETRY_TEXT + text
    elif mode == "clarify":
        text = cur.get("text") or text
    v = {"phase": q["phase"] if q else state.phase, "segment": state.segment, "mode": "aside" if aside else mode, "qid": cur.get("qid"),
         "text": text, "choices": list(cur.get("choices", [])), "user_text": getattr(state, "_user_text", ""),
         "progress": list(getattr(state, "_progress", None) or [])}
    v["next_question"] = json.dumps({"id": v["qid"], "mode": v["mode"], "text": text}, ensure_ascii=False)
    return v


def _represent(state, bank, counted, lead=None, read=True):
    """今のものを LLM を呼ばずに出し直す。読む時間も数える(read=False は start が自分で数えるとき)。"""
    cur = state.current or {}
    if cur.get("mode") == "review":
        card = state.last_card or build_card(state)
        bubbles = review_bubbles(state, card)
        bubbles[0] = (lead + "\n" if lead else "") + bubbles[0]
        return _out(state, bubbles, cur["choices"], counted=counted, card=card["lines"], read=read)
    if cur.get("mode") == "start":
        return _out(state, [lead or START_LEAD_UI], cur["choices"], counted=counted, read=read)
    if cur.get("mode") in ("result", "closed"):
        return _stored(state)
    v = view(state, bank)
    return _out(state, [((lead or "") + bubble(v, v["text"]))], v["choices"], counted=counted, read=read)


def _say(state, bank, llm, counted=True, aside=False):
    """Stage 3。質問は LLM が言い方を付け、まとめと結果はハーネスの決まった文。"""
    cur = state.current
    if cur["mode"] == "review":
        card = state.last_card
        return _out(state, review_bubbles(state, card), cur["choices"], counted=counted, card=card["lines"])
    if cur["mode"] == "result":
        return _finish(state, counted)
    v = view(state, bank, aside=aside)

    def _vr(raw):
        r, err = validate_reply(raw, v)
        return r, ([err] if err else [])
    reply = _call_llm(llm.generate, v, _vr, state, "generate") if llm is not None else None
    if reply is None:
        reply = v["text"]
    return _out(state, [bubble(v, reply)], v["choices"], counted=counted)


def _finish(state, counted):
    """結果の文。「空欄はNつ」は、職歴カードから作る下書きの空欄の数(blank_cards の欄と、本人の言葉に無かったので
    空欄に戻した数字。下書きの画面の数と同じ)。"""
    from dip_mcp.career import draft  # draft がこのモジュールを使うので、循環を避ける

    blanks = blank_count(draft.build(state)[0])
    text = RESULT_TEXT + (f"空欄は{blanks}つ。あとから埋められます。" if blanks else "")
    state.result = {"text": text, "summary": summary(state), "blanks": blanks}
    ev(state, "result", turns=state.turns, seconds=round(state.seconds, 1))
    return _out(state, [text], [], counted=counted, done=True)


def _out(state, bubbles, choices, counted, card=None, done=False, read=True):
    if read:
        state.seconds += read_seconds(list(bubbles) + list(choices))
    out = {"reply": "\n".join(bubbles), "bubbles": list(bubbles), "choices": list(choices), "card": card, "done": done,
           "counted": counted, "closed": state.closed, "phase": state.phase, "qid": (state.current or {}).get("qid")}
    if done:
        out["summary"] = summary(state)
    return out


# ----------------------------- 終わりの状態 -----------------------------
def card_filled(e):
    """職歴カードの欄が、確かめた本人の言葉で埋まっているか。保存しない値(推測のまま(low)、個人情報らしい値、長すぎる値。
    dip_mcp.career.store.save と同じ決まり)は、埋まっていない。"""
    text = str((e or {}).get("text") or "").strip()
    return (bool(e) and e["status"] == "filled" and e.get("confidence", "high") == "high" and bool(text)
            and len(text) <= SAVED_TEXT_MAX and not personal_value(text))


def blank_cards(state):
    """職歴カードの空欄(パス、覚えていない、聞かなかった、推測のまま確かめていない、保存しない値)。下書きの空欄の仕組みで後から埋める(§4)。
    下書き(dip_mcp/career/draft.py)は、ここに出る欄を空欄にする。結果の「空欄はNつ」は、これに下書きで空欄に戻した数字を足した数。"""
    out = []
    for n in range(1, CARD_COUNT + 1):
        if n > 1 and not card_filled(state.slots.get(card_key(n, "role"))):
            continue  # 2 枚目が無い人(パスした、聞けなかった)は空欄に数えない
        out += [card_key(n, f) for f in CARD_FIELDS if not card_filled(state.slots.get(card_key(n, f)))]
    return out


def captured(state):
    """取れた値を、評価の事実表と同じ key と形で(evals/fulltime_hearing/judge.py の docstring)。"""
    out = {}
    for s in sorted(ALL_SLOTS):
        e = state.slots.get(s)
        if e is None:
            out[s] = {"status": "missing", "text": "", "number": None, "range": None, "unit": None}
        else:
            out[s] = {"status": e["status"], "text": e["text"], "number": e.get("number"), "range": e.get("range"),
                      "unit": e.get("unit"), "confidence": e["confidence"], "from": e["from"]}
    return out


def summary(state):
    """終わり(result か「あとで」)に返す状態。保存は呼ぶ側(2b のタスク)。"""
    return {"version": VERSION, "form": state.form, "segment": state.segment, "closed": state.closed,
            "slots": {s: v for s, v in captured(state).items() if v["status"] != "missing"},
            "ng": list(state.user_ng), "blank_cards": blank_cards(state), "turns": state.turns,
            # segment が決まらなかったので使わなかった、segment だけの欄の答え(本人が言ったので捨てない)
            "pending": {s: {k: e.get(k) for k in ("status", "text", "number", "range", "unit", "confidence", "from")}
                        for s, e in state.pending.items()},
            "seconds": round(state.seconds, 1), "stop_reason": state.stop_reason}


# ----------------------------- プロンプトの組み立て -----------------------------
PROMPT_FILES = {"extract": ["prompt_extract_ja.txt"], "say": ["prompt_say_ja.txt"]}


def render_prompt(stage, variables):
    """{{変数}} を埋める。足りない変数があれば KeyError。文字列でない値は JSON にする。"""
    text = "\n\n".join((PROMPT_DIR / f).read_text(encoding="utf-8") for f in PROMPT_FILES[stage])
    names = sorted(set(re.findall(r"\{\{(\w+)\}\}", text)))
    missing = [n for n in names if n not in variables]
    if missing:
        raise KeyError(f"prompt variables missing for {stage}: {missing}")
    for n in names:
        val = variables[n]
        if not isinstance(val, str):
            val = json.dumps(val, ensure_ascii=False, default=str)
        text = text.replace("{{" + n + "}}", val)
    return text
