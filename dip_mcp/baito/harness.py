"""バイト探しの相棒(PRD v4.1)のハーネス。何を聞くか・いつフェーズを切り替えるか・いつ終えるかを決める。

1 ターン = 本人の発言 1 通を turn() で処理すること。turn() は 3 段でできている(PRD §5-4):
  Stage1 抽出: 選択肢そのもの → choice_effects をここで当てる(LLM を呼ばない)。
               文 → llm.extract(payload) → validate_extract()(失敗したら 1 回だけやり直し、だめなら代わりの処理)
  Stage2 判断: 確信度と from はここが決める(モデルの値は読まない)。未成年・中学生・闇バイトの決まり、次の質問とフェーズ
  Stage3 生成: llm.generate(view) → validate_reply()(1 回だけやり直し、だめなら質問の原文)
モデル(LLM)は抽出の JSON と言い方({"reply"})だけを返す。llm は aica 側が渡す(session.py、line_runtime/baito_chat.py)。

正は docs/prd/baito_harness/PRD_baito_agent_v4.md(本書)と付録A・B のデータ。このファイルは参照実装 harness_v4.py を
移したもの(PRD §13-1。付録C の名前をすべて持つ)。質問バンクと職種カタログは JSON のデータ(bank_v4.json・catalog_v4.json)
から読み、プロンプトは dip_mcp/prompts/baito/ の 4 ファイル(原文のまま)を使う。
参照実装から変えたところ(レビュー docs/prd/baito_harness/review/06_readiness_v41.md の ID):
- M-4: 年齢を聞いている質問で抽出が 2 回とも失敗しても、発言に JHS_RE が当たれば age_band=under_15 を入れる(_force_jhs)。
  本人の学年として「中2」などと言ったのに抽出が別の年齢を返したときも under_15。review(年齢の行があり、抽出が失敗)と
  結果の後の年齢の訂正(ブロックを外さない)でも同じ
- 年齢の矛盾(どっち？)の候補に未成年があれば未成年として扱い、中学生以下があれば結果の前に止める(minor_mode・update_block)
- M-16: 複数のスロットは、区切る前に文全体を表示名・正規値と照らす(「推し活・趣味」を壊さない。normalize_value)
- B-2: review の自由入力は参照実装どおり最後の値を high で入れ、同じスロットに複数の値があればログ ambiguous_extract を残す
- M-5: 1 回の抽出では age_band を先に当てる(「20歳で深夜もOK」の深夜を、年齢が空のうちに捨てない)
- M-6: 結果の後の年齢の訂正で中学生のブロックを外すとき、review の巡回と「早く結果」を戻し、15 ターンに達していれば数えない
- M-3: 2 巡目の review の昇格は、カードに出した focus の項目だけ
- M-7: 生成の view に本人の直近の発言(user_text)を入れる
- N-3・N-4: review の返事は、カードの長さに合わせて上限を広げ、カードの中のユーザー由来の文は金額の検査から外す
- N-17: 結果の文で、矛盾のまま未定にした項目を「未定：…」に二重に並べない(unknown_hard_slots には残す)
- 年齢の正規化(§6-3): 未成年を成人と読まないよう読み方を変えた(_norm_age。PRD の表の順では「18歳未満です」
  「17歳、大学志望」が 18_plus になる。レビューに無い、PRD の表の誤り)
- 区分の表に無い項目の答え(task 257。P3 で区分を言わない主婦の「扶養内」を捨てた): 区分が未判明の間は抽出に全部の項目を
  渡す。表に無い答えは pending に保留し、次のターンで区分を確かめる質問(バンクのデータ q_held_segment)を 1 回だけ出す
  (_ask_held。review の 2 ターンと reverse_pick の 1 ターンは使わない)。区分が決まって表にあれば入れ、無ければ捨ててログ
  held_discarded を残す(_settle_held)。保留中と、捨てて今も空の項目を、以後のどの返事が口にしても検証で落とす(view の unkept。
  捨てたときは決まり文句 DISCARD_TEXT を返事の前に付ける)
- 希死念慮・自傷の言葉(task 256。PRD に無い): 抽出の前に本人の自由入力を crisis_v4.json の言葉と照らし、当たれば
  質問を進めずに決まった文(相談窓口の案内)を返す(_crisis)。本人の文は NFKC とカタカナからひらがなへのそろえ
  (kana_fold)をしてから照らすので、一覧はひらがなと漢字の形で持つ。選択肢の文字そのものには当てない。次のターンで「続ける」と
  言えば今の質問を出し直す。どのフェーズでも、結果の後でも同じ
- sc_line(お店の行列)の 2 つ目の選択肢を、希死念慮と読まれない「今すぐ抜けたい」にした(利用者の決定 2026-10-27。
  choice_effects は同じ。PRD の「選択肢はバンクの原文のまま」から外れる)
B-1(DIRECT は hard_filter・fill_gaps)は参照実装のまま(review の修正案)。
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPT_DIR = HERE.parent / "prompts" / "baito"

VERSION = "v4"
HARD, MID, LOW = 3, 2, 1
ORDER = ["icebreak", "hard_filter", "scenario_game", "reverse_pick", "fill_gaps", "review", "result"]
PRE_REVIEW = set(ORDER[:5])
DIRECT = {"hard_filter", "fill_gaps"}  # B-1: 直接質問のフェーズ(PRD §5-1・§7-2。§8-3 の「icebreak・hard_filter」は誤り)
MAX_TURNS = 15
REVIEW_RESERVE = 2
MAX_REVIEW_ROUNDS = 2
HF_MAX, SC_MAX, SC_MIN = 6, 6, 3
MAX_ATTEMPTS = 2
TOPIC_CAP = 2
MAX_CLARIFY = 2
MAX_ASIDES = 3
LLM_RETRIES = 1
SEGMENT_SCORE = 1.5
PHASE_CAP = {"icebreak": 3, "hard_filter": HF_MAX, "scenario_game": SC_MAX, "reverse_pick": 1}
TIER = {HARD: "hard", MID: "mid", LOW: "low"}
SEGMENTS = ("student", "homemaker", "side_job", "other")
NOPREF = "こだわりなし"
SEGMENT_JA = {"student": "学生", "homemaker": "家のこと中心（主婦・主夫など）", "side_job": "本業あり副業探し",
              "other": "フリーター・求職中・その他", "unknown": "未判明"}
INTENTS = ("answer", "clarify", "show_result", "restart", "aside", "danger")
REPLY_MAX = 600


# ----------------------------- スロット(PRD §6) -----------------------------
def S(label, kind, values, multi=False):
    return {"label": label, "kind": kind, "values": values, "multi": multi}


SLOTS = {
    # 共通
    "age_band": S("年齢", "fact", {"under_15": "中学生以下", "15_17": "15〜17歳", "under_18": "18歳未満",
                                  "18_plus": "18歳以上", "declined": "ひみつ（未回答）"}),
    "days_per_week": S("週の日数", "pref", {"1-2": "週1〜2日", "3-4": "週3〜4日", "5+": "週5日以上"}),
    "time_slots": S("時間帯", "pref", {"morning": "朝", "daytime": "昼", "evening": "夕方〜夜", "late_night": "深夜"},
                    multi=True),
    "commute": S("通勤", "pref", {"walk_bike": "徒歩・自転車", "train_30": "電車30分まで", "train_60": "1時間くらいまで"}),
    "hourly_wage_min": S("時給の下限", "pref", {1000: "1,000円台でOK", 1200: "1,200円以上", 1500: "1,500円以上"}),
    "shift_length": S("1回の勤務時間", "pref", {"short": "4時間未満", "mid": "4〜6時間", "long": "6時間以上"}),
    "customer_facing": S("接客", "pref", {"like": "好き", "ok": "ふつう（できる）", "avoid": "できれば避けたい"}),
    "physical": S("体力面", "pref", {"heavy_ok": "体力仕事もOK", "standing_ok": "立ち仕事OK", "light": "軽めがいい",
                                    "seated": "座り仕事がいい"}),
    "purpose": S("目的", "pref", {"hobby": "推し活・趣味", "saving": "貯金", "living_cost": "生活費・家計の足し",
                                 "fun": "旅行・遊び", "career": "履歴書に書ける経験", "easygoing": "ゆるく",
                                 "perk_meal": "まかない", "perk_discount": "社割", "pocket_money": "おこづかい",
                                 "use_experience": "経験を活かす", "try_new": "違うことをしたい",
                                 "no_brain": "頭を使わない", "skill_up": "本業にプラス", "earn": "稼ぐ",
                                 "refresh": "気分転換", "money": "お金", "skill": "経験・スキル",
                                 "people": "人とのつながり"}, multi=True),
    "pace": S("忙しさ", "pref", {"busy_ok": "忙しくてもOK", "calm": "落ち着いた職場"}),
    "teaching_style": S("教わり方", "pref", {"structured": "マニュアルできっちり", "autonomous": "自分で考えたい"}),
    "social": S("職場の付き合い", "pref", {"friendly": "仲良くしたい", "dry": "あっさりがいい"}),
    "environment_noise": S("にぎやかさ", "pref", {"lively": "にぎやか", "quiet": "静か"}),
    "wage_priority": S("時給と働きやすさ", "pref", {"near_first": "近さ優先", "wage_first": "時給優先",
                                                  "comfort_first": "ラクさ優先"}),
    "recognition": S("うれしい評価", "pref", {"customers": "お客さんから", "manager": "店長から"}),
    "coworker_age": S("一緒に働く人", "pref", {"same_age": "同年代", "mixed_age": "大人も", "similar_life": "同じような立場",
                                             "any": "気にしない"}),
    # 学生
    "school_permission": S("学校の許可", "fact", {"ok": "OK", "needs_notice": "届け出すればOK", "banned": "禁止",
                                                "unknown": "わからない"}),
    "exam_flex": S("テスト期間", "pref", {"full_off": "休みたい", "reduced": "減らせればOK", "no_change": "気にしない"}),
    "long_vacation": S("長期休み", "pref", {"heavy": "がっつり", "normal": "いつも通り"}),
    "start_time_pref": S("学校のあと", "pref", {"right_after_school": "放課後すぐ", "after_club": "部活のあと"}),
    # 家のこと中心
    "available_hours": S("働ける時間", "fact", {"9-14": "9〜14時くらい", "10-16": "10〜16時くらい", "evening": "夕方以降",
                                              "varies": "日によってバラバラ"}),
    "sudden_leave": S("急なお休み", "fact", {"frequent": "けっこうある", "sometimes": "たまにある", "rare": "ほぼない"}),
    "fuyou": S("扶養", "fact", {"within": "扶養内で", "beyond_ok": "気にしない", "unknown_needs_help": "わからない"}),
    "remote": S("在宅", "pref", {"yes": "在宅がいい", "no": "外がいい", "either": "どちらでも"}),
    "shift_flex": S("シフトの決め方", "pref", {"fixed": "固定がいい", "flexible": "自分で選びたい"}),
    "leave_support": S("休むときの安心", "pref", {"instant": "すぐ休める", "cover": "代わりがいる"}),
    # 本業あり副業探し
    "side_job_allowed": S("副業の可否", "fact", {"ok": "OK", "needs_notice": "届け出が必要", "banned": "禁止",
                                               "unknown": "わからない・グレー", "civil_servant": "公務員"}),
    "weekday_or_weekend": S("平日・週末", "pref", {"weekday_evening": "平日の夜", "weekend": "土日", "either": "どちらでも"}),
    "energy_left": S("本業後の体力", "pref", {"high": "けっこう残ってる", "some": "少しだけ", "none": "ほぼゼロ"}),
    "avoid_coworkers": S("会社の人に会う", "pref", {"no": "気にしない", "yes": "絶対イヤ"}),
    "work_style": S("働き方", "pref", {"steady": "安定（毎週）", "spot": "スポット"}),
    "weekend_length": S("週末の長さ", "pref", {"full_day": "1日", "half_day": "半日"}),
}
ALL_SLOTS = set(SLOTS)
FACT_SLOTS = {s for s, v in SLOTS.items() if v["kind"] == "fact"}
COMMON = {"age_band": HARD, "days_per_week": HARD, "time_slots": HARD, "commute": HARD,
          "hourly_wage_min": MID, "shift_length": MID, "customer_facing": MID, "physical": MID, "purpose": MID,
          "pace": LOW, "teaching_style": LOW, "social": LOW, "environment_noise": LOW, "wage_priority": LOW,
          "recognition": LOW}
SLOT_PRIORITY = {
    "student": {**COMMON, "school_permission": HARD, "exam_flex": MID, "long_vacation": LOW, "coworker_age": LOW,
                "start_time_pref": LOW},
    "homemaker": {**COMMON, "available_hours": HARD, "sudden_leave": HARD, "fuyou": MID, "remote": MID,
                  "shift_flex": LOW, "leave_support": LOW, "coworker_age": LOW},
    "side_job": {**COMMON, "side_job_allowed": HARD, "weekday_or_weekend": HARD, "energy_left": MID,
                 "avoid_coworkers": LOW, "work_style": LOW, "weekend_length": LOW},
    "other": {**COMMON, "coworker_age": LOW},
    "unknown": dict(COMMON),
}
MINOR_BANDS = {"under_15", "15_17", "under_18"}
DERIVED_NG_MINOR = ["深夜シフト", "重量物・危険な作業"]

# ----------------------------- 決まり文句と注意文(PRD §10-5。法務の確認待ち。文面を変えない) -----------------------------
NOTICE = {
    "under_15": "中学生のうちは、法律で原則アルバイトができないんだ（子役など特別な許可がある場合をのぞく）。高校生になったらまた一緒に探そう！それまでは、おうちの人や学校の先生に相談してみてね。",
    "minor_rules": "18歳未満は、22時〜翌5時の深夜と、重い物を運ぶような危険な作業はNGなんだ。1日8時間・週40時間までがルールだよ。その条件で探すね。",
    "age_declined": "年齢を教えてもらえなかったから、安全のため18歳未満と同じ条件（深夜・重い作業なし）で探すね。あとで変えられるよ。",
    "late_night_removed": "18歳未満（または年齢未回答）だと深夜（22時〜翌5時）は働けないから、深夜はのぞいておくね。",
    "school_banned": "学校のルールで禁止なら、まずは先生やおうちの人に相談してみてね。ここでは「もしOKになったら」の参考として続けるね。",
    "school_unknown": "学校のバイトのルールは、生徒手帳や先生に一度確認しておくと安心だよ。",
    "side_banned": "会社で副業が禁止なら、無理に始めずに就業規則を確認してね。ここでは「もしOKになったら」の参考として続けるね。",
    "side_unknown": "副業OKかわからないときは、就業規則や人事に一度確認してね。",
    "civil_servant": "公務員は副業に法律上の制限があるよ。始める前に職場のルールを必ず確認してね。ここでは参考として続けるね。",
    "fuyou": "扶養のラインは家族の勤め先や制度で変わるから、具体的な金額は家族・勤務先・税務署などで確認してね。",
    "danger": "⚠️「SNSで高額」「受け取るだけ」「すぐ稼げる」みたいな話は、闇バイト（犯罪）の可能性があるよ。絶対に応募しないでね。もう連絡しちゃった・脅されてるときは、警察相談専用電話 #9110 に相談してね。",
    "minor_hours": "18歳未満は1日8時間・週40時間までだから、日数か時間を少し調整するかも。",
    "student_fuyou": "親の扶養に入っている学生は、稼ぎすぎると親の税金や保険に影響することがあるよ。くわしくはおうちの人に確認してね。",
    "side_tax": "副業の収入は、確定申告が必要になることや、本業と労働時間が合算されることがあるよ。くわしくは勤務先や税務署で確認してね。",
    "rules_only": "※ここで伝えているのは一般的なルールの目安で、法律相談ではないよ。",
}
CLOSING_JA = "推測のところ、ちがってたらまとめて教えてね！「どっち？」はどっちか選んでね。「未定」はわかる範囲で教えてね。ぜんぶ合ってたら「これでOK」だけで大丈夫🙆"
REVIEW_OK = "これでOK"
CLARIFY_TEXT = "お、どのへんがちがう？ひとことで教えて！"
RETRY_TEXT = "ごめん、うまく受け取れなかった！もう一回だけ教えて。"
CONFIRM_TEXT = "確認させてね！今のあなたに近いのはどれ？"
DISCARD_TEXT = "{labels}の話は、今回の条件には入れないでおくね。"  # 保留した答えを捨てたとき(「覚えた」と言わない)
POST_RESULT_TEXT = "おすすめはここまでだよ。条件を変えてもう一回探すなら「最初から」と送ってね。"
GUESS_TAIL = "（確認できていない推測の条件だよ。ちがっていたら「最初から」でやり直せるよ）"

# ----------------------------- 正規表現(PRD §5-8。規範) -----------------------------
DANGER_RE = re.compile(r"闇バイト|ホワイト案件|受け子|出し子|運び屋|叩き|荷物を?受け取るだけ|受け取るだけ|口座.*(売|貸|渡)|名義.*(貸|売)|"
                       r"即日.*高(収入|額)|高(収入|額).*即日|1日で?\s*\d+\s*万|日給\s*\d+\s*万|SNS.*(高額|高収入)|簡単.*高収入")
MONEY_RE = re.compile(r"\d+(\.\d+)?\s*万|１０３万|103万|106万|130万|150万|160万")
JHS_RE = re.compile(r"中学|中[1-3１-３](?!\d)")
NOPREF_RE = re.compile(r"こだわりなし|どっちでもいい|どちらでもいい|なんでもいい|何でもいい|気にしない")
DECLINE_RE = re.compile(r"ひみつ|秘密|答えたくない|言いたくない|内緒|スキップ")
PII_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|0\d{1,4}-?\d{1,4}-?\d{3,4}|\d{3}-?\d{4}|〒")
MULTI_SEP_RE = re.compile(r"[、,，/／]\s*")
STRONG_SPLIT_RE = re.compile(r"[、,，/／\s]+")  # 複数スロットの文の区切り(PRD §6-3 の 1)。M-16: 先にこれで区切り、表示名と一致しない部分だけを「・と&」でも区切る
WEAK_SPLIT_RE = re.compile(r"[・と&]+")
# 18 以上の数があっても、まだその年齢になっていない言い方(「18歳未満」「まだ18歳になっていません」「来月18歳」)。
# PRD §6-3 の表は数の行(8)が「18歳未満」の行(9)より先なので、そのままでは 18_plus と読む。安全側に under_18 にする
NOT_YET_RE = re.compile(r"未満|未成年|以下|なって(い)?な|なって(い)?ませ|まだ|もうすぐ|あと|来月|来年|今年|になる|になります")


def scrub_note(raw):
    """notes には、伏せ字にした 30 字までの写しだけを残す(PRD §10-6)。"""
    return PII_RE.sub("＊", str(raw))[:30]


def nfkc(s):
    return unicodedata.normalize("NFKC", str(s)).strip()


KATA_TO_HIRA = {c: c - 0x60 for c in range(0x30A1, 0x30F7)}  # ァ..ヶ -> ぁ..ゖ(長音の「ー」はそのまま)


def kana_fold(s):
    """NFKC にそろえ、カタカナをひらがなにする(希死念慮の言葉と「続ける」を読む前)。
    「シニタイ」「ｼﾆﾀｲ」「しにたい」が同じ形になるので、言葉の一覧はひらがなと漢字の形だけで持てる。"""
    return nfkc(s).translate(KATA_TO_HIRA)


def _words_re(words):
    return re.compile("|".join(map(re.escape, map(kana_fold, words))))


# ----------------------------- データ(付録A・付録B) -----------------------------
def _load_json(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def load_bank():
    """質問バンク(付録A、49 問)と、ハーネスが足した区分の確認の質問(q_held_segment、task 257)。プロンプトには書かない。"""
    return _load_json("bank_v4.json")


def load_catalog():
    """職種カタログ(付録B、24 件)。推薦の許可リスト。times は集合にする。"""
    return [dict(j, times=set(j["times"])) for j in _load_json("catalog_v4.json")]


def load_crisis():
    """希死念慮・自傷の言葉と、そのときに返す決まった文(crisis_v4.json。PRD に無い aica の決まり。task 256)。
    窓口は厚生労働省「まもろうよ こころ」(source)で checked の日に確かめたもの。プロンプトには書かない。"""
    return _load_json("crisis_v4.json")


BANK = load_bank()
HELD_QID = "q_held_segment"  # 区分の表に無い答えを保留したときの、区分の確認の質問(phase "confirm"。選び方では拾わない)
CATALOG = load_catalog()
CATALOG_BY_ID = {j["id"]: j for j in CATALOG}
CRISIS = load_crisis()
CRISIS_TEXT = "\n".join(CRISIS["reply"])
CRISIS_RE = _words_re(CRISIS["words"])
RESUME_RE = _words_re(CRISIS["continue_words"])
RESUME_NOT_RE = _words_re(CRISIS["continue_not"])


# ----------------------------- 正規化(PRD §6-3) -----------------------------
def _hours(s):
    m = re.search(r"(\d+(?:\.\d+)?)\s*時間", s)
    return float(m.group(1)) if m else None


def _exact(slot, value):
    """値が正規値か表示名とそのまま一致すれば正規値(区切りを含む表示名「推し活・趣味」もここで決まる)。"""
    vals = SLOTS[slot]["values"]
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if value in vals:
        return value
    s = nfkc(value)
    if s in vals:
        return s
    for k, ja in vals.items():
        if s == nfkc(ja):
            return k
    return None


def _weak_split(slot, part):
    """「・と&」で区切る。ただし区切り文字を含む表示名(「推し活・趣味」「人とのつながり」)は、文の中にあればそのまま残す。"""
    if _exact(slot, part) is not None:
        return [part]
    names = sorted({nfkc(ja) for ja in SLOTS[slot]["values"].values() if WEAK_SPLIT_RE.search(nfkc(ja))}, key=len, reverse=True)
    if not names:
        return WEAK_SPLIT_RE.split(part)
    out = []
    for i, piece in enumerate(re.split("(" + "|".join(map(re.escape, names)) + ")", part)):
        out += [piece] if i % 2 else WEAK_SPLIT_RE.split(piece)
    return out


def normalize_value(slot, value):
    """正規値(複数スロットはリスト)、NOPREF、正規化できなければ None。"""
    spec = SLOTS[slot]
    if value is None:
        return None
    if spec["multi"]:
        whole = _exact(slot, value) if isinstance(value, str) else None
        if whole is not None:  # M-16: 区切る前に文全体を照らす
            return [whole]
        if isinstance(value, list):
            items = value
        elif isinstance(value, str):
            items = []
            for part in STRONG_SPLIT_RE.split(nfkc(value)):
                items += _weak_split(slot, part)
        else:
            items = [value]
        out = []
        for it in items:
            if it in (None, ""):
                continue
            if isinstance(it, str) and NOPREF_RE.search(nfkc(it)):
                return NOPREF if spec["kind"] == "pref" else None
            n = _norm_single(slot, it)
            if n is not None and n != NOPREF and n not in out:
                out.append(n)
        return out or None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    return _norm_single(slot, value)


def _age_number(s):
    """年齢らしい数。「〜歳・才」の付いた数を先に、無ければ単位(か月・年・時間など)の付かない数。生年(「2009年生まれ」)は読まない。"""
    m = re.search(r"(\d{1,3})\s*(歳|才)", s)
    if m:
        return int(m.group(1))
    for m in re.finditer(r"\d{1,4}", s):
        if not re.match(r"\s*(か月|ヶ月|ヵ月|カ月|ヶ|年|月|日|時|分|万|円|週|回|人|学期)", s[m.end():]):
            return int(m.group(0)) if len(m.group(0)) <= 3 else None
    return None


def _norm_age(s):
    """PRD §6-3 の年齢の行。未成年を成人と読まないよう、参照実装から次を変えた(安全側):
    「〜歳」の数を学校の語より先に読む(「17歳、大学志望」)、まだその年齢になっていない言い方は under_18
    (「18歳未満」「まだ18歳になっていない」「あと半年で18」)、進学の予定(「大学受験生」「4月から大学生」)と
    生年(「2009年生まれ」)と学年だけ(「1年生」)は決めない(None。未成年として扱い、年齢を聞く)、
    成人・大人の打ち消し(「成人してない」)は under_18。"""
    if JHS_RE.search(s):
        return "under_15"
    if DECLINE_RE.search(s) or NOPREF_RE.search(s):
        return "declined"
    a = _age_number(s)
    if a is not None and re.search(r"(\d{1,3})\s*(歳|才)", s):
        return _band(a, s)
    if re.search(r"高[1-3]|高校|高専", s):
        return "15_17"
    if re.search(r"未成年|未満|以下", s) or re.search(r"(成人|大人).*(ない|ません|まだ|前)|(まだ|前).*(成人|大人)", s):
        return "under_18"
    if re.search(r"生まれ|年生", s) or re.search(r"志望|目指|受験|予定|から|なりたい|行きたい|入りたい", s):
        return None
    if re.search(r"大学|大[1-4]|専門|短大|社会人|会社員", s):
        return "18_plus"
    if a is not None:
        return _band(a, s)
    if "成人" in s or "大人" in s or "18歳以上" in s:
        return "18_plus"
    return None


def _band(a, s):
    if a >= 18 and NOT_YET_RE.search(s):
        return "under_18"
    return "under_15" if a < 15 else "15_17" if a < 18 else "18_plus"


def _norm_single(slot, value):
    spec = SLOTS[slot]
    vals = spec["values"]
    if isinstance(value, bool):
        if slot == "age_band":
            return "under_18" if value else "18_plus"
        return None
    exact = _exact(slot, value)
    if exact is not None:
        return exact
    s = nfkc(value)
    if slot == "age_band":
        return _norm_age(s)
    if spec["kind"] == "pref" and NOPREF_RE.search(s):
        return NOPREF
    if spec["kind"] == "fact" and DECLINE_RE.search(s) and "unknown" in vals:
        return "unknown"
    if slot == "shift_length":
        h = _hours(s)
        if h is not None:
            return "short" if h < 4 else "mid" if h < 6 else "long"
        if "短" in s or "サクッと" in s:
            return "short"
        if "長" in s or "ガッツリ" in s:
            return "long"
    if slot == "hourly_wage_min":
        m = re.search(r"(\d[\d,]*)", s)
        if m:
            w = int(m.group(1).replace(",", ""))
            if w < 20:
                w *= 100
            return 1000 if w < 1200 else 1200 if w < 1500 else 1500
    if slot == "days_per_week":
        m = re.search(r"(\d)", s)
        if m:
            d = int(m.group(1))
            return "1-2" if d <= 2 else "3-4" if d <= 4 else "5+"
    if slot == "time_slots":
        if "深夜" in s:
            return "late_night"
        if "朝" in s or "午前" in s:
            return "morning"
        if "昼" in s or "日中" in s:
            return "daytime"
        if "夕" in s or "夜" in s or "放課後" in s:
            return "evening"
    if slot == "commute":
        if "徒歩" in s or "自転車" in s or "近所" in s:
            return "walk_bike"
        if "1時間" in s:
            return "train_60"
        m = re.search(r"(\d+)\s*分", s)
        if m:
            n = int(m.group(1))
            return "walk_bike" if n <= 15 and "電車" not in s else "train_30" if n <= 30 else "train_60"
    if slot == "physical":
        if "座" in s:
            return "seated"
        if "軽" in s:
            return "light"
    if slot == "customer_facing":
        if "苦手" in s or "避け" in s or "イヤ" in s or "嫌" in s:
            return "avoid"
        if "好き" in s:
            return "like"
    return None


def value_ja(slot, v):
    if v is None:
        return "未定"
    if v == NOPREF:
        return NOPREF
    vals = SLOTS[slot]["values"]
    if isinstance(v, list):
        return "・".join(vals.get(x, str(x)) for x in v)
    return vals.get(v, str(v))


def is_filled(slot_entry):
    return slot_entry is not None and slot_entry.get("value") not in (None, [], "")


# ----------------------------- 状態(付録D) -----------------------------
@dataclass
class State:
    version: str = VERSION
    phase: str = "icebreak"
    segment: str = "unknown"
    slots: dict = field(default_factory=dict)          # slot -> {"value","confidence","from"}
    user_ng: list = field(default_factory=list)
    derived_ng: list = field(default_factory=list)
    pending: dict = field(default_factory=dict)        # まだ有効でないスロットの値(区分の表に無い答えの保留)
    archived: dict = field(default_factory=dict)       # セグメント -> {slot: entry}
    conflicts: dict = field(default_factory=dict)      # slot -> [values]
    unresolved: dict = field(default_factory=dict)     # 結果の時点で解けなかった矛盾(§12-2)
    notes: dict = field(default_factory=dict)          # slot -> 正規化できなかった原文(伏せ字・30 字)
    asked: list = field(default_factory=list)
    attempts: dict = field(default_factory=dict)
    topic_count: dict = field(default_factory=dict)
    turns: int = 0
    phase_turns: dict = field(default_factory=dict)
    aside_count: int = 0
    clarify_used: int = 0
    clarified: list = field(default_factory=list)
    retried: list = field(default_factory=list)
    current: dict = None
    pending_segment: str = None
    review_rounds: int = 0
    last_card: dict = None
    review_log: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    notices_sent: list = field(default_factory=list)
    blocked: str = None
    restarts: int = 0
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
        for s, e in list(st.slots.items()):  # JSON で文字列になった時給を整数に戻す
            if s == "hourly_wage_min" and isinstance(e.get("value"), str) and e["value"].isdigit():
                e["value"] = int(e["value"])
        return st


def new_state():
    """空の状態。年齢がまだ無いので、derived_ng は未成年のルールの NG で始まる(05-N4)。"""
    st = State()
    recompute_derived(st)
    if hasattr(st, "_new_notices"):
        del st._new_notices
    return st


def ev(state, kind, **kw):
    state.log.append({"t": state.turns, "kind": kind, **kw})


def active_slots(state):
    return SLOT_PRIORITY.get(state.segment, SLOT_PRIORITY["unknown"])


def hard_slots(state):
    return [s for s, p in active_slots(state).items() if p == HARD]


def _age_values(state):
    """今の年齢の値と、矛盾として残っている年齢の候補。"""
    e = state.slots.get("age_band")
    return [e.get("value") if e else None] + state.conflicts.get("age_band", []) + state.unresolved.get("age_band", [])


def minor_mode(state):
    """年齢が空・未成年・未回答なら未成年として扱う(§10-1)。どの区分でも同じ。年齢の候補が矛盾していて、
    どれかが未成年なら未成年(最後の推測が 18歳以上でも、深夜を開けない)。"""
    return any(v is None or v in MINOR_BANDS or v == "declined" for v in _age_values(state))


def recompute_derived(state):
    state.derived_ng = list(DERIVED_NG_MINOR) if minor_mode(state) else []
    if minor_mode(state):
        e = state.slots.get("time_slots")
        if e and isinstance(e.get("value"), list) and "late_night" in e["value"]:
            e["value"] = [x for x in e["value"] if x != "late_night"]
            ev(state, "rejected_by_rule", slot="time_slots", value="late_night", rule="minor")
            _notice(state, "late_night_removed")
            if not e["value"]:
                del state.slots["time_slots"]
        e = state.slots.get("physical")
        if e and e.get("value") == "heavy_ok":
            e["value"] = "standing_ok"
            ev(state, "rejected_by_rule", slot="physical", value="heavy_ok", rule="minor")


def _notice(state, key):
    _init_volatile(state)
    if key not in state.notices_sent:
        state.notices_sent.append(key)
        state._new_notices.append(key)


# ----------------------------- 統合(§7-2・§7-5) -----------------------------
def confidence_for(src_kind, phase, slot, evidence_ok):
    """確信度はハーネスが決める(モデルの confidence は読まない)。"""
    if src_kind in ("review", "choice_direct", "confirm"):
        return "high"
    if src_kind == "free_current" and phase in DIRECT:
        return "high"
    if slot in FACT_SLOTS and evidence_ok:
        return "high"
    return "low"


def apply_update(state, slot, value, conf, frm, review=False):
    """戻り値は discarded / pending / rejected / noop / set / conflict。例外は出さない。"""
    if slot not in ALL_SLOTS:
        ev(state, "discarded_unknown_slot", slot=slot)
        return "discarded"
    if slot not in active_slots(state):
        state.pending[slot] = {"value": value, "confidence": conf, "from": frm}
        ev(state, "held", slot=slot, segment=state.segment)
        return "pending"
    spec = SLOTS[slot]
    if value == NOPREF and spec["kind"] == "fact":
        if slot == "age_band":
            value = "declined"
        else:
            ev(state, "rejected_by_rule", slot=slot, value=value, rule="nopref_on_fact")
            return "rejected"
    if minor_mode(state):  # 未成年の深夜・重労働はここで受け付けない(§10-1)
        if slot == "time_slots" and isinstance(value, list) and "late_night" in value:
            value = [v for v in value if v != "late_night"]
            ev(state, "rejected_by_rule", slot=slot, value="late_night", rule="minor")
            _notice(state, "late_night_removed")
            if not value:
                return "rejected"
        if slot == "physical" and value == "heavy_ok":
            ev(state, "rejected_by_rule", slot=slot, value=value, rule="minor")
            value = "standing_ok"
    old = state.slots.get(slot)
    new = {"value": value, "confidence": conf, "from": frm}
    if not is_filled(old):
        state.slots[slot] = new
        status = "set"
    elif spec["multi"] and slot == "purpose":
        merged = list(old["value"]) if isinstance(old["value"], list) else []
        for v in (value if isinstance(value, list) else [value]):
            if v not in merged and v != NOPREF:
                merged.append(v)
        conf2 = "high" if "high" in (old["confidence"], conf) else "low"
        state.slots[slot] = {"value": merged or value, "confidence": conf2, "from": frm if conf == "high" else old["from"]}
        status = "set"
    elif old["confidence"] == "high" and conf == "low":
        if old["value"] != value:
            ev(state, "rejected_by_rule", slot=slot, value=value, rule="low_over_high")
            return "rejected"
        return "noop"
    elif old["confidence"] == "low" and conf == "low" and old["value"] != value and not spec["multi"]:
        c = state.conflicts.setdefault(slot, [old["value"]])
        if value not in c:
            c.append(value)
        state.slots[slot] = new
        status = "conflict"
    elif spec["multi"] and conf == "low" and old["confidence"] == "low":
        if isinstance(value, list) and isinstance(old["value"], list):
            merged = list(old["value"]) + [v for v in value if v not in old["value"]]
        else:
            merged = value
        state.slots[slot] = {"value": merged, "confidence": "low", "from": frm}
        status = "set"
    else:
        state.slots[slot] = new
        status = "set"
        if conf == "high":
            state.conflicts.pop(slot, None)
    if slot == "age_band":
        recompute_derived(state)
        v = state.slots[slot]["value"]
        update_block(state)
        if v == "declined":
            _notice(state, "age_declined")
        elif v in MINOR_BANDS:
            _notice(state, "minor_rules")
    if slot == "school_permission":
        _flag_notice(state, {"banned": "school_banned", "unknown": "school_unknown"}.get(state.slots[slot]["value"]))
    if slot == "side_job_allowed":
        _flag_notice(state, {"banned": "side_banned", "unknown": "side_unknown",
                             "civil_servant": "civil_servant"}.get(state.slots[slot]["value"]))
    if slot == "fuyou":
        _flag_notice(state, "fuyou")
    hours = state.slots[slot]["value"] if slot == "available_hours" else None
    if hours in ("9-14", "10-16", "evening") and not is_filled(state.slots.get("time_slots")):
        derived = {"9-14": ["morning", "daytime"], "10-16": ["daytime"], "evening": ["evening"]}[hours]
        state.slots["time_slots"] = {"value": derived, "confidence": conf, "from": "derived"}
    return status


def update_block(state, final=False):
    """under_15 は確信度 high のときだけ止める。low は会話を続けて hf_age で確かめ、finalize(final=True)で止める(05-B10)。"""
    e = state.slots.get("age_band") or {}
    sure = e.get("value") == "under_15" and e.get("confidence") == "high"
    # 結果の前(final)は、推測の under_15 も、矛盾の候補にある under_15 も止める
    state.blocked = "under_15" if sure or (final and "under_15" in _age_values(state)) else None


def unkept_slots(state):
    """受け取ったが入れていない答えの項目。返事が「覚えた」と言わないよう、生成の検証に渡す。保留中のものと、
    会話の中で捨てて今も空のもの(捨てたターンだけでなく、その後のどのターンの返事も、結果の返事も)。"""
    _init_volatile(state)
    out = list(state.pending) + [s for s in state._discarded if s not in state.pending]
    for e in state.log:
        if e["kind"] == "held_discarded" and e["slot"] not in out and not is_filled(state.slots.get(e["slot"])):
            out.append(e["slot"])
    return out


def _settle_held(state, force=False):
    """区分が決まったら、保留した答えのうち今の区分の表に無いものを捨てる(表にあるものは _switch_segment が入れた)。
    force=True は、区分を確かめても決まらなかったとき・結果の前(未判明のままでも捨てる)。"""
    if state.segment == "unknown" and not force:
        return
    for s in list(state.pending):
        e = state.pending.pop(s)
        if s in active_slots(state):  # 念のため(ふつうは _switch_segment で入っている)
            apply_update(state, s, e["value"], e["confidence"], e["from"])
            continue
        ev(state, "held_discarded", slot=s, segment=state.segment)
        _init_volatile(state)
        if s not in state._discarded:
            state._discarded.append(s)


def _flag_notice(state, key):
    if key and key not in state.flags:
        state.flags.append(key)
    if key:
        _notice(state, key)


def set_segment(state, seg, source):
    """source: choice|confirm|review_explicit|free。一度分かったセグメントは unknown に戻さない。"""
    if seg not in SEGMENTS:
        ev(state, "invalid_segment", value=seg)
        return "ignored"
    if seg == state.segment:
        state.pending_segment = None
        return "noop"
    if state.segment == "unknown" or source in ("choice", "confirm"):
        _switch_segment(state, seg)
        return "set"
    state.pending_segment = seg
    ev(state, "segment_change_pending", old=state.segment, new=seg)
    return "pending_confirm"


def _switch_segment(state, seg):
    old = state.segment
    if old != "unknown":
        keep = set(SLOT_PRIORITY[seg])
        arch = {s: e for s, e in state.slots.items() if s not in keep}
        if arch:
            state.archived[old] = arch
            for s in arch:
                state.slots.pop(s)
                state.conflicts.pop(s, None)
        ev(state, "segment_switched", old=old, new=seg, archived=sorted(arch))
    state.segment = seg
    state.pending_segment = None
    for s, e in state.archived.pop(seg, {}).items():  # 05-M7: 戻すときも統合の決まりを通す
        if not is_filled(state.slots.get(s)):
            apply_update(state, s, e["value"], e["confidence"], e["from"])
    for s in list(state.pending):
        if s in active_slots(state):
            e = state.pending.pop(s)
            apply_update(state, s, e["value"], e["confidence"], e["from"])
    _settle_held(state)  # 区分が決まった: 表に無い保留は捨てる


# ----------------------------- LLM の出力の検証(§8-4) -----------------------------
def parse_json(raw):
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    s = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except ValueError:
        m = re.search(r"\{.*\}", s, flags=re.DOTALL)
        if m:
            try:
                d = json.loads(m.group(0))
                return d if isinstance(d, dict) else None
            except ValueError:
                return None
    return None


SEG_SYNONYM = {"学生": "student", "大学生": "student", "高校生": "student", "専門学生": "student", "主婦": "homemaker",
               "主夫": "homemaker", "会社員": "side_job", "社会人": "side_job", "副業": "side_job",
               "フリーター": "other", "求職中": "other", "その他": "other"}


def _empty_extract():
    return {"slot_updates": [], "ng_updates": [], "ng_removals": [], "segment": None, "intent": "answer",
            "review_ack": None}


def validate_extract(raw):
    """-> (clean, problems)。使えなければ (None, problems)。"""
    probs = []
    d = parse_json(raw)
    if d is None:
        return None, ["not_json"]
    out = _empty_extract()
    su = d.get("slot_updates")
    if su is None:
        su = []
        probs.append("slot_updates_null" if "slot_updates" in d else "slot_updates_missing")
    if isinstance(su, dict):
        su = [{"slot": k, "value": v} for k, v in su.items()]
    if not isinstance(su, list):
        su = []
        probs.append("slot_updates_bad_type")
    if len(su) > 10:
        probs.append("slot_updates_truncated")
    for u in su[:10]:
        if not isinstance(u, dict) or not isinstance(u.get("slot"), str):
            probs.append("update_bad_item")
            continue
        slot = u["slot"].strip()
        if slot == "age_under_18":  # v3 の名前
            slot = "age_band"
        if slot == "segment":
            d.setdefault("segment", u.get("value"))
            continue
        if slot not in ALL_SLOTS:
            probs.append("unknown_slot:" + slot)
            continue
        if "confidence" not in u:
            probs.append("confidence_missing")
        val = u.get("value")
        if isinstance(val, list) and not SLOTS[slot]["multi"]:
            probs.append("list_for_single:" + slot)
        nv = normalize_value(slot, val)
        if nv is None:
            probs.append("unnormalizable:" + slot)
            out["slot_updates"].append({"slot": slot, "value": None, "raw": val, "evidence": u.get("evidence")})
            continue
        out["slot_updates"].append({"slot": slot, "value": nv, "evidence": u.get("evidence")})
    for key in ("ng_updates", "ng_removals"):
        v = d.get(key) or []
        if isinstance(v, str):
            v = [v]
        lst = [nfkc(x) for x in v if isinstance(x, str) and nfkc(x)] if isinstance(v, list) else []
        if len(lst) > 5:
            probs.append(key + "_truncated")
        out[key] = lst[:5]
    seg = d.get("segment")
    if isinstance(seg, str) and seg:
        s2 = seg if seg in SEGMENTS else SEG_SYNONYM.get(nfkc(seg))
        if s2 is None or s2 == "unknown":
            probs.append("invalid_segment:" + seg)
        out["segment"] = s2 if s2 in SEGMENTS else None
    it = d.get("intent", "answer")
    if d.get("clarify") is True:
        it = "clarify"
    out["intent"] = it if it in INTENTS else "answer"
    ra = d.get("review_ack")
    out["review_ack"] = ra if ra in ("all", "partial", "none") else None
    return out, probs


def validate_reply(raw, view):
    d = parse_json(raw)
    if d:
        reply = d.get("reply")
    else:
        reply = raw if isinstance(raw, str) and raw.strip() and not raw.strip().startswith("{") else None
    if not isinstance(reply, str) or not reply.strip():
        return None, "no_reply"
    review = view.get("mode") == "review"
    limit = REPLY_MAX
    if review:  # N-3: カードと締めの一文がそのまま入る長さは許す
        limit = max(REPLY_MAX, len(view["card_text"]) + len(CLOSING_JA) + 120)
    if len(reply) > limit:
        return None, "too_long"
    # N-4: カードの中のユーザー由来の文(NG・メモ)は、金額の検査から外す
    if MONEY_RE.search(reply.replace(view["card_text"], "") if review else reply):
        return None, "money_amount"
    if review:
        if view["card_text"] not in reply or reply.count(CLOSING_JA) != 1:
            return None, "card_altered"
    elif view.get("text") and view.get("mode") in ("ask", "reask", "clarify", "retry", "confirm", "aside"):
        if nfkc(view["text"]) not in nfkc(reply):
            return None, "question_missing"
        if len(reply) - len(view["text"]) > 120:
            return None, "too_much_extra"
    if _mentions_unkept(reply, view.get("unkept"), view.get("card_text") if review else view.get("text")):
        return None, "unkept_mentioned"
    return reply.strip(), None


def _mentions_unkept(text, labels, fixed=None):
    """保留・捨てた項目(labels)を、決まった文(質問・カード)の外で口にしているか。入れていない答えに「覚えた」と言わせない。"""
    if not labels or not isinstance(text, str):
        return False
    t = nfkc(text)
    if fixed:
        t = t.replace(nfkc(fixed), "")
    return any(nfkc(x) in t for x in labels)


# ----------------------------- 質問の選び方(§8-3) -----------------------------
def gap_weight(entry):
    if not is_filled(entry):
        return 1.0
    return 0.5 if entry.get("confidence") == "low" else 0.0


def _segment_ok(q, state):
    return "all" in q["segments"] or state.segment in q["segments"]


def visible_choices(q, state):
    """未成年(年齢が空・未回答を含む)には、深夜・重労働の選択肢を出さない。"""
    ch = []
    for c in q["choices"]:
        fl = q["choice_flags"].get(c, [])
        if minor_mode(state) and ("late_night" in fl or "heavy" in fl):
            continue
        ch.append(c)
    return ch


def pick_question(state, bank, phase):
    pri = active_slots(state)
    if phase == "hard_filter" and "hf_age" not in state.asked and gap_weight(state.slots.get("age_band")) > 0:
        return qmap(bank)["hf_age"], "ask"  # 年齢を必ず最初に聞く
    phases = {phase, "hard_filter"} if phase == "fill_gaps" else {phase}
    best, best_score, best_mode = None, 0.0, "ask"
    for q in bank:  # 同点はバンクの並び順(「より大きい」だけで比べる)
        if q["phase"] not in phases or not _segment_ok(q, state):
            continue
        if q["id"] == "q_segment":
            if state.segment == "unknown" and q["id"] not in state.asked and SEGMENT_SCORE > best_score:
                best, best_score, best_mode = q, SEGMENT_SCORE, "ask"
            continue
        mode = "ask"
        if q["id"] in state.asked:
            if phase == "fill_gaps" and q["phase"] in DIRECT and all(
                    state.attempts.get(t, 0) < MAX_ATTEMPTS for t in q["targets"] if t in pri):
                mode = "reask"
            else:
                continue
        targets = [t for t in q["targets"] if t in pri]
        if phase in DIRECT:
            targets = [t for t in targets if state.attempts.get(t, 0) < MAX_ATTEMPTS]
        elif phase == "scenario_game":
            targets = [t for t in targets if state.topic_count.get(t, 0) < TOPIC_CAP]
        if phase == "reverse_pick":
            score = 1.0
        else:
            score = sum(pri[t] * gap_weight(state.slots.get(t)) for t in targets)
            if score > 0 and "all" not in q["segments"]:
                score += 0.5
        if len(visible_choices(q, state)) < 2:
            continue
        if score > best_score:
            best, best_score, best_mode = q, score, mode
    return (best, best_mode) if best else (None, None)


def open_hard(state):
    return [s for s in hard_slots(state) if gap_weight(state.slots.get(s)) > 0]


def remaining_gaps(state):
    pri = active_slots(state)
    return [s for s, p in pri.items() if p >= MID and (p == HARD and gap_weight(state.slots.get(s)) > 0 or
            p == MID and not is_filled(state.slots.get(s))) and state.attempts.get(s, 0) < MAX_ATTEMPTS]


def next_phase(state, bank):
    p = state.phase
    if state.blocked:
        return "result"
    if p == "result":
        return "result"
    if state.turns >= MAX_TURNS:  # 15 ターンの上限は何より先(05-B6)
        return "result"
    if p == "review":
        more = state._need_more_review and state.review_rounds < MAX_REVIEW_ROUNDS and "show_result" not in state.flags
        return "review" if more else "result"
    if state.turns >= MAX_TURNS - REVIEW_RESERVE or "show_result" in state.flags:
        return "review"
    n = state.phase_turns.get(p, 0)
    if p == "icebreak":
        return "hard_filter" if state.segment != "unknown" or n >= 3 else "icebreak"
    if p == "hard_filter":
        if n >= HF_MAX or not [s for s in open_hard(state) if state.attempts.get(s, 0) < 1]:
            return "scenario_game"
        return "hard_filter"
    if p == "scenario_game":
        budget_tight = state.turns >= MAX_TURNS - REVIEW_RESERVE - 3
        return "reverse_pick" if n >= SC_MAX or (n >= SC_MIN and budget_tight) else "scenario_game"
    if p == "reverse_pick":
        return "fill_gaps"
    if p == "fill_gaps":
        return "fill_gaps" if remaining_gaps(state) else "review"
    return "review"


def advance(state, bank):
    """次に出すものを決めて state.current に入れる。ORDER の長さで打ち切るので繰り返さない。"""
    if state.pending_segment and state.phase != "result":
        np = next_phase(state, bank)
        if np in ("review", "result"):
            ev(state, "confirm_skipped", pending=state.pending_segment)
            state.pending_segment = None
        else:  # 05-B4: 確認質問は次のフェーズに数える(フェーズの上限を超えない)
            state.phase = np
            state.phase_turns.setdefault(np, 0)
            state.current = {"qid": "confirm_segment", "mode": "confirm", "choices": list(qmap(bank)["q_segment"]["choices"])}
            if state.pending:  # 保留した答えも、この確認で決まる
                _flag(state, "held_asked")
                state.current.update(choices=list(_held_q(bank)["choices"]), held=sorted(state.pending))
            return
    if _ask_held(state, bank):
        return
    for _ in range(len(ORDER) + 2):
        np = next_phase(state, bank)
        state.phase = np
        if np in ("review", "result"):
            break
        q, mode = pick_question(state, bank, np)
        if q:
            _present(state, q, mode)
            return
        state.phase = ORDER[ORDER.index(np) + 1]
        if state.phase == "review":
            break
        state.phase_turns.setdefault(state.phase, 0)
    else:
        state.phase = "review"
    if state.phase == "review":
        state.current = {"qid": "review", "mode": "review", "choices": [REVIEW_OK]}
        state.last_card = build_card(state)
    else:
        finalize(state)
        state.current = {"qid": "result", "mode": "result", "choices": []}


def _ask_held(state, bank):
    """区分の表に無い答えを保留したら、次のターンで区分を確かめる(1 回だけ)。区分の質問(q_segment)をまだ出していなければ、
    そちらで決まるので出さない。review の前は review の 2 ターンを使わない(review に入るなら、review の返事の後に聞く)。
    review の中では review のターンに数え、15 ターンに達していれば聞かない(結果の前に捨てる)。"""
    if not state.pending or "held_asked" in state.flags or state.blocked or state.phase == "result":
        return False
    if state.segment == "unknown" and "q_segment" not in state.asked:
        return False
    if state.turns >= MAX_TURNS or "show_result" in state.flags:
        return False
    need = False
    if state.phase == "review":
        need = getattr(state, "_need_more_review", False) and state.review_rounds < MAX_REVIEW_ROUNDS
        if need and state.turns + 2 > MAX_TURNS:
            return False  # 2 巡目の review を先にする(その後にターンが残れば聞く)
    else:
        np = next_phase(state, bank)
        if np in ("review", "result", "reverse_pick"):
            return False  # reverse_pick(1 ターン)は使わない。次のフェーズで聞く
        state.phase = np  # 05-B4 と同じく、確認質問は次のフェーズに数える
        state.phase_turns.setdefault(np, 0)
    _flag(state, "held_asked")  # 1 回だけ(状態の欄は増やさない)
    ev(state, "held_confirm", slots=sorted(state.pending))
    state.current = {"qid": "confirm_segment", "mode": "confirm", "choices": list(_held_q(bank)["choices"]),
                     "held": sorted(state.pending)}
    if need:  # review の続きが要るかを、確認の答えのターンまで持ち越す(_need_more_review は保存しない)
        state.current["need_review"] = True
    return True


def _held_q(bank):
    """区分の確認の質問(バンクのデータ)。参照資料の写しのバンク(付録A の 49 問だけ。シミュレーター・単体チェック)を
    渡されたときは、このモジュールのバンクから読む。"""
    return qmap(bank).get(HELD_QID) or qmap(BANK)[HELD_QID]


def _held_text(cur, bank):
    if not cur.get("held"):
        return CONFIRM_TEXT
    return _held_q(bank)["text"].format(labels="・".join(SLOTS[s]["label"] for s in cur["held"]))


def _present(state, q, mode):
    pri = active_slots(state)
    if q["id"] not in state.asked:
        state.asked.append(q["id"])
    for t in q["targets"]:
        if t in pri:
            state.topic_count[t] = state.topic_count.get(t, 0) + 1
            if q["phase"] in DIRECT and gap_weight(state.slots.get(t)) > 0:  # B-1: DIRECT は hard_filter・fill_gaps
                state.attempts[t] = state.attempts.get(t, 0) + 1
    state.current = {"qid": q["id"], "mode": mode, "choices": visible_choices(q, state)}


def qmap(bank):
    return {q["id"]: q for q in bank}


# ----------------------------- review(§8-6) -----------------------------
def cross_warnings(state):
    def v(s):
        return (state.slots.get(s) or {}).get("value")
    w = []
    if v("energy_left") == "none" and v("days_per_week") == "5+":
        w.append("本業後の体力は「ほぼゼロ」なのに週5日以上になってるよ。どっちを優先する？")
    ts = v("time_slots")
    if state.segment == "side_job" and v("weekday_or_weekend") == "weekday_evening" and isinstance(ts, list) \
            and "evening" not in ts:
        w.append("「平日の夜」希望だけど、時間帯に夜が入ってないよ。")
    if v("available_hours") in ("9-14", "10-16") and isinstance(ts, list) and "evening" in ts:
        w.append("働ける時間は日中なのに、時間帯に夕方〜夜が入ってるよ。")
    if any("立ち仕事" in n for n in state.user_ng) and v("physical") in ("standing_ok", "heavy_ok"):
        w.append("NGに「立ち仕事」があるけど、体力面は「立ち仕事OK」になってるよ。")
    if minor_mode(state) and v("days_per_week") == "5+" and v("shift_length") == "long":
        w.append(NOTICE["minor_hours"])
    if v("fuyou") == "within" and v("days_per_week") == "5+" and v("shift_length") == "long":
        w.append("扶養内希望なら、週5日×長時間は多めかも（金額のラインは家族・勤務先で確認してね）。")
    return w


def build_card(state):
    pri = active_slots(state)

    def item(s):
        e = state.slots.get(s)
        filled = is_filled(e)
        it = {"slot": s, "label_ja": SLOTS[s]["label"], "tier": TIER[pri[s]],
              "priority": pri[s], "value": e["value"] if filled else None,
              "value_ja": value_ja(s, e["value"]) if filled else "未定",
              "inferred": bool(filled and e["confidence"] == "low"), "missing": not filled,
              "conflict_values": None, "conflict": None, "note": state.notes.get(s)}
        if s in state.conflicts:
            it["conflict_values"] = list(state.conflicts[s])
            it["conflict"] = [value_ja(s, x) for x in state.conflicts[s]]
        if s == "age_band" and (not filled or e["value"] == "declined"):
            it["safety_note"] = "安全のため18歳未満と同じ条件で探すよ"
        return it
    hard = [item(s) for s, p in pri.items() if p == HARD]
    prefs = [item(s) for s, p in pri.items() if p == MID] + \
        [item(s) for s, p in pri.items() if p == LOW and is_filled(state.slots.get(s))]
    focus = []
    if state.review_rounds >= 1:
        focus = [i["slot"] for i in hard + prefs if i["conflict"] or (i["tier"] == "hard" and (i["missing"] or i["inferred"]))]
    return {"round": state.review_rounds + 1, "segment": state.segment, "segment_ja": SEGMENT_JA[state.segment],
            "hard": hard, "prefs": prefs, "ng_user": list(state.user_ng), "ng_rule": list(state.derived_ng),
            "warnings": cross_warnings(state), "focus": focus}


def render_card(card):
    def line(i):
        if i["conflict"]:
            return f"・{i['label_ja']}：{' / '.join(i['conflict'])}（どっち？）"
        extra = f"※{i['safety_note']}" if i.get("safety_note") else ""
        if i["missing"]:
            tail = "（ここだけ教えて！）" if i["tier"] == "hard" else "（よければ教えてね）"
            memo = f"（メモ：{i['note']}）" if i.get("note") else ""
            return f"・{i['label_ja']}：未定{memo}{tail}{extra}"
        return f"・{i['label_ja']}：{i['value_ja']}{'（推測）' if i['inferred'] else ''}{extra}"
    lines = []
    if card["focus"]:
        lines.append("ここだけもう一回！")
        lines += [line(i) for i in card["hard"] + card["prefs"] if i["slot"] in card["focus"]]
    else:
        lines.append(f"【あなた】{card['segment_ja']}")
        lines.append("【ゆずれない条件】")
        lines += [line(i) for i in card["hard"]]
        lines.append("【好み】")
        lines += [line(i) for i in card["prefs"]]
    ng = card["ng_user"] + [f"{n}（ルール）" for n in card["ng_rule"]]
    lines.append("【NG】" + ("、".join(ng) if ng else "なし"))
    lines += ["⚠️ " + w for w in card["warnings"]]
    return "\n".join(lines)


def _apply_ng(state, ext):
    """ng_updates を足し、ng_removals を本人の NG から外す(ルールの NG は外さない。ユーザーの決定で ng_removals を採用)。"""
    for n in ext["ng_updates"]:
        if n not in state.user_ng:
            state.user_ng.append(n)
    for n in ext["ng_removals"]:
        if n in state.user_ng:
            state.user_ng.remove(n)


def _ambiguous(state, ext):
    """1 回の抽出で、同じ単一スロットに違う値が複数あるもの(ログ ambiguous_extract)。"""
    seen = {}
    for u in ext["slot_updates"]:
        if u["value"] is not None:
            seen.setdefault(u["slot"], [])
            if u["value"] not in seen[u["slot"]]:
                seen[u["slot"]].append(u["value"])
    out = {s for s, vals in seen.items() if len(vals) > 1 and not SLOTS.get(s, {}).get("multi")}
    for s in sorted(out):
        ev(state, "ambiguous_extract", slot=s, values=seen[s])
    return out


def _age_first(updates):
    """M-5: 1 回の抽出では age_band を先に当てる(残りはモデルが返した順)。"""
    return [u for u in updates if u["slot"] == "age_band"] + [u for u in updates if u["slot"] != "age_band"]


def apply_review(state, ext, button_ok, promote=True):
    """review の返事を反映する。promote=False(返事が 2 回読めなかった)なら何も上げず、巡回だけ数える(05-B8)。"""
    card = state.last_card or build_card(state)
    items = card["hard"] + card["prefs"]
    shown = {i["slot"]: i for i in items if not card["focus"] or i["slot"] in card["focus"]}  # M-3: 出した項目だけ
    state.review_rounds += 1
    ack = "all" if button_ok else (ext or {}).get("review_ack")
    touched, hard_changed = set(), False
    if ext and not button_ok:
        _ambiguous(state, ext)  # B-2: review は最後の値を high で入れ、ログだけ残す
        for u in _age_first(ext["slot_updates"]):
            s = u["slot"]
            if u["value"] is None:
                state.notes[s] = scrub_note(u.get("raw"))
                ev(state, "note_unnormalizable", slot=s)
                continue
            old = state.slots.get(s)
            st = apply_update(state, s, u["value"], "high", "review", review=True)
            touched.add(s)
            if st == "set" and is_filled(old) and old["value"] != state.slots.get(s, {}).get("value") and \
                    active_slots(state).get(s) == HARD and old.get("confidence") == "high":
                hard_changed = True
        _apply_ng(state, ext)
        if ext.get("segment"):
            set_segment(state, ext["segment"], "review_explicit")
    promoted = []
    for s, i in (shown.items() if promote else ()):
        e = state.slots.get(s)
        if s in touched or not is_filled(e) or e["confidence"] != "low" or s in state.conflicts:
            continue
        if i["tier"] == "hard" and ack != "all":
            continue  # HARD の推測は「これでOK」(all)のときだけ確定する
        e["confidence"] = "high"
        e["from"] = "review_accepted"
        promoted.append(s)
    update_block(state)
    state.review_log.append({"round": state.review_rounds, "ack": ack, "touched": sorted(touched),
                             "promoted": promoted, "hard_changed": hard_changed})
    need = hard_changed or bool(state.conflicts) or bool(state.pending_segment)
    if state.review_rounds == 1 and any(gap_weight(state.slots.get(s)) > 0 for s in hard_slots(state)):
        need = True
    state._need_more_review = need


def finalize(state):
    """結果の前: 解けない矛盾は値を消して「未定」にし、候補を unresolved に移す(§12-2。ユーザーの決定)。"""
    for s, vals in list(state.conflicts.items()):
        state.unresolved[s] = list(vals)
        state.slots.pop(s, None)
        ev(state, "conflict_unresolved", slot=s)
    state.conflicts = {}
    _settle_held(state, force=True)  # 確かめられなかった保留は捨てる(ログ held_discarded)
    recompute_derived(state)
    update_block(state, final=True)


# ----------------------------- 結果(§8-7) -----------------------------
def ng_hits(job, ngs):
    for n in ngs:
        n2 = nfkc(n)
        if n2 == "深夜シフト" and job["night_only"]:
            return True
        if "重量物" in n2 and job["phys"] == "heavy":
            return True
        if any(n2 in k or k in n2 for k in job["keys"]) or n2 in job["name"]:
            return True
    return False


def job_safe(state, j):
    """事後チェック: カタログにあり、未成年に出さない職種でなく、NG に当たらない。"""
    if not j:
        return False
    if minor_mode(state) and (j["min_age"] >= 18 or j["phys"] == "heavy" or j["night_only"]):
        return False
    return not ng_hits(j, state.user_ng + state.derived_ng)


def eligible_jobs(state, catalog):
    def v(s):
        return (state.slots.get(s) or {}).get("value")
    minor = minor_mode(state)
    out = []
    for j in catalog:
        if minor and (j["min_age"] >= 18 or j["phys"] == "heavy" or j["night_only"]):
            continue
        if ng_hits(j, state.user_ng + state.derived_ng):
            continue
        ts = v("time_slots")
        times = set(j["times"])
        if isinstance(ts, list) and ts:
            usable = set(ts) - ({"late_night"} if minor else set())
            if usable and not (times & usable):
                continue
        if v("customer_facing") == "avoid" and j["cf"] == "high":
            continue
        if v("physical") == "seated" and j["phys"] in ("standing", "heavy"):
            continue
        if v("physical") == "light" and j["phys"] == "heavy":
            continue
        score, why = 0.0, []
        if v("customer_facing") in ("like", "ok") and j["cf"] in ("high", "mid"):
            score += 1
            why.append("customer_facing")
        if v("customer_facing") == "avoid" and j["cf"] in ("none", "low"):
            score += 1
            why.append("customer_facing")
        if v("physical") == "seated" and j["phys"] == "seated":
            score += 1
            why.append("physical")
        if v("environment_noise") and v("environment_noise") == j["noise"]:
            score += 0.5
            why.append("environment_noise")
        if v("remote") == "yes" and j["remote"]:
            score += 1.5
            why.append("remote")
        if v("work_style") == "spot" and j["spot"]:
            score += 0.5
            why.append("work_style")
        if v("avoid_coworkers") == "yes" and j["remote"]:
            score += 0.5
            why.append("avoid_coworkers")
        if isinstance(ts, list) and ts and times & set(ts):
            score += 0.5
            why.append("time_slots")
        out.append({"job_id": j["id"], "name": j["name"], "score": score, "why_slots": why})
    out.sort(key=lambda x: -x["score"])  # 同点はカタログの並び順(安定ソート)
    return out


def build_result_payload(state, catalog):
    card = build_card(state)
    card["focus"] = []
    unknown_hard = [s for s in hard_slots(state) if not is_filled(state.slots.get(s))]
    guessed = [s for s in hard_slots(state) if is_filled(state.slots.get(s)) and state.slots[s]["confidence"] == "low"]
    blocked = state.blocked
    cands = [] if blocked else eligible_jobs(state, catalog)[:6]
    notices = [NOTICE["under_15"]] if blocked else []
    for k in ("school_banned", "side_banned", "civil_servant", "school_unknown", "side_unknown", "fuyou", "danger"):
        if k in state.flags or (k == "danger" and "danger_warned" in state.flags):
            notices.append(NOTICE[k])
    if not blocked and state.segment == "student":
        notices.append(NOTICE["student_fuyou"])
    if not blocked and state.segment == "side_job":
        notices.append(NOTICE["side_tax"])
    if minor_mode(state) and not blocked:
        declined = (state.slots.get("age_band") or {}).get("value") == "declined"
        notices.append(NOTICE["age_declined"] if declined else NOTICE["minor_rules"])
    if notices:
        notices.append(NOTICE["rules_only"])
    conditional = any(f in state.flags for f in ("school_banned", "side_banned", "civil_servant"))
    return {"card": card, "card_text": render_card(card), "candidates": cands, "need": min(3, len(cands)),
            "unknown_hard": [SLOTS[s]["label"] for s in unknown_hard], "unknown_hard_slots": unknown_hard,
            "guessed_hard": [f"{SLOTS[s]['label']}＝{value_ja(s, state.slots[s]['value'])}" for s in guessed],
            "guessed_hard_slots": guessed,
            "unresolved": {SLOTS[s]["label"]: [value_ja(s, x) for x in v] for s, v in state.unresolved.items()},
            "notices": notices, "blocked": blocked, "conditional": conditional}


def validate_result(raw, payload):
    d = parse_json(raw)
    if not d:
        return None, "not_json"
    recs = d.get("recommendations")
    need = payload["need"]
    if not isinstance(recs, list) or len(recs) != need:
        return None, "wrong_count"
    ids = {c["job_id"] for c in payload["candidates"]}
    seen = set()
    for r in recs:
        if not isinstance(r, dict) or r.get("job_id") not in ids or r["job_id"] in seen:
            return None, "not_in_allowlist"
        if not isinstance(r.get("reason"), str) or not r["reason"].strip() or MONEY_RE.search(r["reason"]):
            return None, "bad_reason"
        if _mentions_unkept(r["reason"], payload.get("unkept")):
            return None, "unkept_mentioned"
        seen.add(r["job_id"])
    reply = d.get("reply", "")
    if not isinstance(reply, str) or MONEY_RE.search(reply):
        return None, "bad_reply"
    if _mentions_unkept(reply, payload.get("unkept")):
        return None, "unkept_mentioned"
    return {"reply": reply, "recommendations": [{"job_id": r["job_id"], "reason": r["reason"].strip()} for r in recs]}, None


def fallback_reason(c):
    why = "・".join(SLOTS[s]["label"] for s in c["why_slots"][:2]) or "条件"
    return f"{why}の条件に合いそうだから"


def fallback_result(payload):
    recs = [{"job_id": c["job_id"], "reason": fallback_reason(c)} for c in payload["candidates"][:payload["need"]]]
    return {"reply": "条件から、合いそうなバイトを選んだよ！", "recommendations": recs}


def compose_result_text(payload, out, catalog_by_id):
    lines = list(payload["notices"])
    if payload["blocked"]:
        return "\n".join(lines)
    lines.append(out["reply"])
    if payload["conditional"]:
        lines.append("（ルールの確認がとれたら、の参考だよ）")
    for i, r in enumerate(out["recommendations"], 1):
        lines.append(f"{i}. {catalog_by_id[r['job_id']]['name']}：{r['reason']}")
    if not out["recommendations"]:
        lines.append("今の条件だと合うものが見つからなかった…条件を少しゆるめてもう一回探してみよう。")
    elif len(out["recommendations"]) < 3:
        lines.append("今の条件だと合うのはここまで。時間帯や通勤の条件を少し広げると、候補が増えるよ。")
    if payload.get("guessed_hard"):
        lines.append("推測：" + "、".join(payload["guessed_hard"]) + GUESS_TAIL)
    unknown = [x for x in payload["unknown_hard"] if x not in payload["unresolved"]]  # N-17: 候補つきの行だけにする
    if unknown:
        lines.append("未定：" + "、".join(unknown) + "（決まったらもっと絞り込めるよ）")
    for k, v in payload["unresolved"].items():
        lines.append(f"未定：{k}（{' / '.join(v)} のどちらか。決まったら教えてね）")
    return "\n".join(lines)


def post_check_result(state, payload, out, cby):
    """推薦の事後チェック(重複・カタログ外・未成年に出さない職種・NG)と、落とした分の補充(05-M13)。"""
    ok, used = [], set()
    for r in out["recommendations"]:
        j = cby.get(r["job_id"])
        if r["job_id"] in used or not job_safe(state, j):
            ev(state, "result_postcheck_drop", job=r["job_id"])
            continue
        ok.append(r)
        used.add(r["job_id"])
    need = payload.get("need", len(ok)) if isinstance(payload, dict) else len(ok)
    for c in (payload.get("candidates", []) if isinstance(payload, dict) else []):
        if len(ok) >= need:
            break
        if c["job_id"] in used or not job_safe(state, cby.get(c["job_id"])):
            continue
        ok.append({"job_id": c["job_id"], "reason": fallback_reason(c)})
        used.add(c["job_id"])
        ev(state, "result_refill", job=c["job_id"])
    out["recommendations"] = ok
    return out


# ----------------------------- プロンプトの変数(§8-5) -----------------------------
def slots_json(state):
    pri = active_slots(state)
    filled = [{"slot": s, "label_ja": SLOTS[s]["label"], "tier": TIER[pri[s]],
               "value_ja": value_ja(s, e["value"]), "confidence": e["confidence"], "from": e["from"]}
              for s, e in state.slots.items() if is_filled(e) and s in pri]
    missing = [SLOTS[s]["label"] for s in hard_slots(state) if not is_filled(state.slots.get(s))]
    return json.dumps({"slots": filled, "missing_hard": missing, "ng": state.user_ng + state.derived_ng,
                       "minor_mode": minor_mode(state), "turn": state.turns, "max_turns": MAX_TURNS},
                      ensure_ascii=False, separators=(",", ":"))


def view(state, bank, aside=False):
    """生成段の入力。aside=True は脱線の後に同じものを出し直すとき(05-B7)。"""
    _init_volatile(state)
    cur = state.current or {}
    mode = cur.get("mode")
    v = {"phase": state.phase, "segment": f"{state.segment}（{SEGMENT_JA[state.segment]}）", "mode": mode,
         "qid": cur.get("qid"), "choices": list(cur.get("choices", [])), "notices": [NOTICE[k] for k in state._new_notices],
         "slots_json": slots_json(state), "user_text": state._user_text}
    unkept = unkept_slots(state)
    if unkept:  # 生成の検証だけが読む(プロンプトの変数ではない)
        v["unkept"] = [SLOTS[s]["label"] for s in unkept]
    q = qmap(bank).get(cur.get("qid"))
    if mode == "confirm":
        v["text"] = _held_text(cur, bank)
    elif mode == "review":
        v["card_json"] = state.last_card
        v["card_text"] = render_card(state.last_card)
        v["text"] = v["card_text"]
        v["closing"] = CLOSING_JA
    elif q:
        v["text"] = q["alt_text"] if mode == "reask" else q["text"]
        if mode == "clarify" and not aside:
            v["text"] = CLARIFY_TEXT + "（" + q["text"] + "）"
        if mode == "retry" and not aside:
            v["text"] = RETRY_TEXT + q["text"]
    else:
        v["text"] = ""
    if aside and mode not in ("review", "result"):
        v["base_mode"] = mode
        v["mode"] = "aside"
    v["next_question"] = json.dumps({"id": v["qid"], "mode": v["mode"], "text": v.get("text", ""), "choices": v["choices"]},
                                    ensure_ascii=False)
    return v


def extract_payload(state, bank, user_text):
    """抽出段の入力(05-B8)。question / choices / targets は出しているものによる。"""
    cur = state.current or {}
    mode = cur.get("mode")
    # 区分が未判明の間は全部の項目を渡す(区分の表に無い答えは保留する。task 257)
    allowed = sorted(ALL_SLOTS) if state.segment == "unknown" else sorted(active_slots(state))
    if mode == "review":
        card = state.last_card or build_card(state)
        question, choices = render_card(card), [REVIEW_OK]
        targets = list(card["focus"]) or [i["slot"] for i in card["hard"] + card["prefs"]]
    elif mode == "confirm":
        question, choices, targets = _held_text(cur, bank), list(cur.get("choices", [])), ["segment"]
    elif mode == "result":
        question, choices, targets = "（おすすめを表示したあと）", [], (["age_band"] if state.blocked else [])
    else:
        q = qmap(bank).get(cur.get("qid"))
        question = (q["alt_text"] if mode == "reask" else q["text"]) if q else (cur.get("qid") or "")
        choices, targets = list(cur.get("choices", [])), (list(q["targets"]) if q else [])
    return {"phase": state.phase, "segment": state.segment, "user_text": user_text, "question": question,
            "choices": choices, "targets": targets,
            "allowed_slots": {s: list(map(str, SLOTS[s]["values"])) for s in allowed}}


# ----------------------------- ターン(§5) -----------------------------
def _init_volatile(state, reset=False):
    """そのターン限りの値(先頭が _。保存しない)。"""
    for k, v in (("_new_notices", None), ("_need_more_review", False), ("_user_text", ""), ("_discarded", None)):
        if reset or not hasattr(state, k):
            setattr(state, k, [] if v is None else v)


def start(state, bank):
    """最初の質問(ターンに数えない)。何回呼んでも同じ質問を返す。"""
    _init_volatile(state)
    state._new_notices = []
    if state.current is None:
        advance(state, bank)
    return view(state, bank)


def resume(state, bank):
    v = start(state, bank)
    v["resumed"] = True
    return v


def match_button(state, bank, text):
    """出している選択肢と(NFKC で)そのまま一致すればボタン。multi の質問は区切った各部分がすべて選択肢なら複数ボタン。"""
    cur = state.current or {}
    t = nfkc(text)
    if not t:
        return None
    nch = {nfkc(c): c for c in cur.get("choices", [])}
    if t in nch:
        return [nch[t]]
    q = qmap(bank).get(cur.get("qid"))
    if q and q.get("multi"):
        parts = [p for p in MULTI_SEP_RE.split(t) if p]
        if parts and all(p in nch for p in parts):
            out = []
            for p in parts:
                if nch[p] not in out:
                    out.append(nch[p])
            return out
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


def _apply_effects(state, bank, q, choices, src_kind):
    direct = q["phase"] in DIRECT or src_kind == "confirm"
    if len(choices) > 1:  # 複数のボタン: 複数スロットは和集合にしてから 1 回で当てる
        merged = {}
        for c in choices:
            for k, val in q["choice_effects"].get(c, {}).items():
                if k in SLOTS and SLOTS[k]["multi"]:
                    merged.setdefault(k, [])
                    merged[k] += [x for x in (val if isinstance(val, list) else [val]) if x not in merged[k]]
        if merged:
            conf = confidence_for("choice_direct" if q["phase"] in DIRECT else "choice", q["phase"], None, False)
            for k, val in merged.items():
                apply_update(state, k, val, conf, f"choice:{q['id']}")
            effects = {c: {k: v for k, v in q["choice_effects"].get(c, {}).items() if k not in merged} for c in choices}
            q = dict(q, choice_effects=effects)
    for c in choices:
        for k, val in q["choice_effects"].get(c, {}).items():
            if k == "__ng__":
                for n in val:
                    if n not in state.user_ng:
                        state.user_ng.append(n)
            elif k == "__segment__":
                set_segment(state, val, "confirm" if src_kind == "confirm" else "choice")
            else:
                conf = confidence_for("choice_direct" if direct else "choice", q["phase"], k, False)
                apply_update(state, k, val, conf, f"choice:{q['id']}")


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


def clarify_room(state):
    """聞き返しは、フェーズの上限と review の予備ターンを侵さないときだけ。"""
    cap = PHASE_CAP.get(state.phase)
    if cap is not None and state.phase_turns.get(state.phase, 0) >= cap:
        return False
    return state.turns < MAX_TURNS - REVIEW_RESERVE


def turn(state, user_text, bank, llm, catalog=None):
    """本人の発言 1 通を処理して {reply, view, done, counted} を返す。例外は出さない: ハーネスの中の例外は、
    ターンの始めの状態に戻して、今出しているものを出し直す(05-M15)。"""
    _init_volatile(state)
    snap = state.to_dict()
    try:
        return _turn(state, user_text, bank, llm, catalog)
    except Exception as e:  # noqa: BLE001
        restored = State.from_dict(snap)
        state.__dict__.clear()
        state.__dict__.update(restored.__dict__)
        _init_volatile(state, reset=True)
        ev(state, "internal_error", error=type(e).__name__)
        if state.result:
            return {"reply": state.result["text"], "view": view(state, bank), "done": True, "counted": False}
        return _represent(state, bank, counted=False)


# 本人の学年としての「中学生」(「中2」「中学3年」「中学生です」)。卒業・昔の話と、家族・先生の話は除く(_jhs_self)
JHS_SELF_RE = re.compile(r"中[1-3１-３](?!\d)|中学[1-3１-３一二三]年|中学生")
NOT_SELF_RE = re.compile(r"卒|の(時|とき|頃|ころ)|だった|子ども|子供|娘|息子|弟|妹|兄|姉|生徒|先生|親")


def _jhs_applies(cur, q, text):
    """中学生以下の文字列で年齢を補う場面: 年齢を聞いている質問(確認質問・review でない)で、発言が JHS_RE に当たる。"""
    return q is not None and cur["mode"] != "confirm" and "age_band" in q["targets"] and bool(JHS_RE.search(nfkc(text)))


def _jhs_self(text):
    t = nfkc(text)
    return bool(JHS_SELF_RE.search(t)) and not NOT_SELF_RE.search(t)


def _force_jhs(state, ext, text):
    """年齢を聞いている質問への中学生の発言(§5-4 の 5)。抽出が失敗しても(M-4)、抽出の年齢が無い・読めないときも、
    本人の学年として「中2」などを言ったのに抽出が別の年齢を返したときも、age_band=under_15 にする(安全側)。"""
    if ext is None:
        ext = _empty_extract()
        ev(state, "jhs_fallback", extract_failed=True)
    ages = [u for u in ext["slot_updates"] if u["slot"] == "age_band"]
    if ages and all(u["value"] == "under_15" for u in ages):
        return ext
    if ages and all(u["value"] is not None for u in ages) and not _jhs_self(text):
        return ext  # 「中学の先生」のような語だけで、抽出がはっきりした年齢を返したときは抽出のまま
    if ages:
        ev(state, "jhs_override", values=[u["value"] for u in ages])
    elif not any(e["kind"] == "jhs_fallback" for e in state.log[-1:]):
        ev(state, "jhs_fallback")
    ext["slot_updates"] = [u for u in ext["slot_updates"] if u["slot"] != "age_band"] + \
        [{"slot": "age_band", "value": "under_15", "evidence": text}]
    return ext


def _turn(state, user_text, bank, llm, catalog):
    state._new_notices, state._discarded = [], []
    text = user_text if isinstance(user_text, str) else ""
    state._user_text = text
    if state.current is None:
        advance(state, bank)
    if state.phase == "result" and state.result:
        return _post_result(state, text, bank, llm, catalog)
    cur = state.current
    qm = qmap(bank)
    if cur["qid"] == "confirm_segment":
        q = _held_q(bank) if cur.get("held") else qm.get("q_segment")
    else:
        q = qm.get(cur["qid"])
    if not nfkc(text):
        ev(state, "empty_input")
        return _represent(state, bank, counted=False)
    crisis_open = _take_crisis(state)
    if match_button(state, bank, text) is None:  # 選択肢の文字そのものは答えとして読む
        if crisis_hit(text):
            return _crisis(state, bank)
        if crisis_open and wants_resume(text):
            ev(state, "crisis_resume")
            return _represent(state, bank, counted=False, lead=[CRISIS["resume_lead"]])
    if DANGER_RE.search(nfkc(text)):
        return _aside(state, bank, llm, catalog, kind="danger")
    # ---- Stage 1
    buttons = match_button(state, bank, text)
    ext = None
    if buttons is None:
        ext = _call_llm(llm.extract, extract_payload(state, bank, text), validate_extract, state, "extract")
        if _jhs_applies(cur, q, text):
            ext = _force_jhs(state, ext, text)
        elif cur["mode"] == "review" and ext is None and _jhs_self(text) and \
                any(i["slot"] == "age_band" for i in (state.last_card or {}).get("hard", [])):
            ext = _force_jhs(state, None, text)  # review: 年齢の行があり、抽出が 2 回とも失敗したときだけ
    intent = (ext or {}).get("intent", "answer")
    if intent == "restart":
        return _restart(state, bank, llm, catalog)
    if intent == "danger":
        return _aside(state, bank, llm, catalog, kind="danger")
    if intent == "aside":
        return _aside(state, bank, llm, catalog, kind="aside")
    # ---- 数えるターン
    _count(state)
    mode = cur["mode"]
    if mode == "review":
        if buttons is None and ext is None:
            key = f"review{state.review_rounds + 1}"
            if key not in state.retried:  # 1 回目: 数えずにカードを出し直す
                state.retried.append(key)
                _uncount(state)
                ev(state, "review_reply_unparsed", retry=True)
                return _represent(state, bank, counted=False, prefix=RETRY_TEXT)
            ev(state, "review_reply_unparsed", retry=False)
            apply_review(state, None, button_ok=False, promote=False)  # 2 回目: 巡回は数え、何も上げない
        else:
            _apply_ext(state, bank, ext, q=None, text=text, review=True)
            apply_review(state, ext, button_ok=bool(buttons))
        if intent == "show_result":
            state._need_more_review = False
            _flag(state, "show_result")
        advance(state, bank)
        return _say(state, bank, llm, catalog)
    held_confirm = mode == "confirm" and bool(cur.get("held"))
    if buttons is not None:
        _apply_effects(state, bank, q, buttons, "confirm" if mode == "confirm" else "choice")
        if mode == "confirm":
            state.pending_segment = None
    elif ext is None:
        if mode == "confirm":  # 05-B8: 読めなければ今のセグメントのまま
            ev(state, "confirm_unparsed", kept=state.segment)
            state.pending_segment = None
        elif cur["qid"] not in state.retried:  # 1 回目: 数えずに 1 回だけ聞き直す
            state.retried.append(cur["qid"])
            _uncount(state)
            state.current = dict(cur, mode="retry")
            return _say(state, bank, llm, catalog, counted=False)
        else:
            ev(state, "extract_gave_up", qid=cur["qid"])
    elif intent == "clarify" and mode != "confirm" and cur["qid"] not in state.clarified \
            and state.clarify_used < MAX_CLARIFY and clarify_room(state):
        state.clarified.append(cur["qid"])
        state.clarify_used += 1
        state.current = dict(cur, mode="clarify")
        return _say(state, bank, llm, catalog)
    else:
        if intent == "clarify":
            ev(state, "clarify_suppressed", qid=cur["qid"])
        _apply_ext(state, bank, ext, q=q, text=text, review=False, confirm=(mode == "confirm"))
        if mode == "confirm":
            state.pending_segment = None
    if held_confirm:  # 区分を確かめた: 表にあれば入った。無い・決まらなければ捨てる
        _settle_held(state, force=True)
        if state.phase == "review":  # review の中で聞いたときは、区分が変わって空いた条件をもう 1 巡で確かめる
            state._need_more_review = bool(cur.get("need_review") or state.conflicts or state.pending_segment) or \
                any(gap_weight(state.slots.get(s)) > 0 for s in hard_slots(state))
    if intent == "show_result":
        _flag(state, "show_result")
    advance(state, bank)
    return _say(state, bank, llm, catalog)


def _flag(state, key):
    if key not in state.flags:
        state.flags.append(key)


def crisis_hit(text):
    """本人の自由入力に、希死念慮・自傷の言葉(CRISIS["words"])があるか。文は kana_fold にそろえてから照らす。"""
    return bool(CRISIS_RE.search(kana_fold(text)))


def wants_resume(text):
    """窓口を案内した次の発言が「続ける」か(「続けたくない」「大丈夫じゃない」は当たらない)。"""
    t = kana_fold(text)
    return bool(RESUME_RE.search(t)) and not RESUME_NOT_RE.search(t)


def _take_crisis(state):
    """前のターンで窓口を案内したか。印は 1 ターンだけ残す(読んだら消す)。"""
    shown = "crisis_shown" in state.flags
    if shown:
        state.flags.remove("crisis_shown")
    return shown


def _crisis(state, bank, done=False):
    """希死念慮・自傷の言葉: 質問を進めず(数えず、抽出も生成もしない)、決まった文だけを返す。選択肢は出さない。
    今の質問はそのまま残すので、次のターンで「続ける」と言えば、そこから相談を続ける。"""
    _flag(state, "crisis_shown")
    ev(state, "crisis_guidance")
    v = dict(view(state, bank), choices=[], crisis=True)
    return {"reply": CRISIS_TEXT, "view": v, "done": done, "counted": False}


def _restart(state, bank, llm, catalog):
    restarts = state.restarts + 1
    user_text = state._user_text
    fresh = new_state()
    fresh.restarts = restarts
    state.__dict__.clear()
    state.__dict__.update(fresh.__dict__)
    _init_volatile(state, reset=True)
    state._user_text = user_text
    ev(state, "restart", n=restarts)
    advance(state, bank)
    return _say(state, bank, llm, catalog, counted=False, extra={"restarted": True})


def _post_result(state, text, bank, llm, catalog):
    """結果を出した後の発言(05-B9)。やり直しと、中学生のブロックを外す年齢の訂正のほかは面談に戻らない。"""
    v = view(state, bank)
    stored = {"reply": POST_RESULT_TEXT + "\n" + state.result["text"], "view": v, "done": True, "counted": False}
    if not nfkc(text):
        return stored
    crisis_open = _take_crisis(state)
    if crisis_hit(text):
        return _crisis(state, bank, done=True)
    if crisis_open and wants_resume(text):
        ev(state, "crisis_resume")
        return stored
    if DANGER_RE.search(nfkc(text)):
        _flag(state, "danger_warned")
        ev(state, "danger_skip", where="post_result")
        return {"reply": NOTICE["danger"], "view": v, "done": True, "counted": False}
    ext = _call_llm(llm.extract, extract_payload(state, bank, text), validate_extract, state, "extract")
    intent = (ext or {}).get("intent", "answer")
    if intent == "restart":
        return _restart(state, bank, llm, catalog)
    if intent == "danger":
        _flag(state, "danger_warned")
        ev(state, "danger_skip", where="post_result")
        return {"reply": NOTICE["danger"], "view": v, "done": True, "counted": False}
    if state.blocked and ext:
        ages = [u["value"] for u in ext["slot_updates"]
                if u["slot"] == "age_band" and u["value"] in ("15_17", "under_18", "18_plus")]
        if ages and not _jhs_self(text):  # 「中3だけど16になった」は中学生のまま(ブロックを外さない)  # 年齢の訂正でブロックを外す
            if state.turns < MAX_TURNS:  # M-6: 15 ターンに達していれば数えず、そのまま結果を作り直す
                _count(state)
            apply_update(state, "age_band", ages[-1], "high", "free:post_result")
            state.result = None
            state.unresolved = {}
            state.review_rounds = 0  # M-6: review をはじめからやり直せるようにする
            state.last_card = None
            state.flags = [f for f in state.flags if f != "show_result"]
            state.phase = "hard_filter"
            ev(state, "unblocked", value=ages[-1])
            advance(state, bank)
            return _say(state, bank, llm, catalog)
    ev(state, "post_result_message", intent=intent)
    return stored


def _apply_ext(state, bank, ext, q, text, review, confirm=False):
    if not ext:
        return
    if ext.get("segment"):
        set_segment(state, ext["segment"], "confirm" if confirm else ("review_explicit" if review else "free"))
    if review:
        return  # review のスロットは apply_review が当てる
    ambiguous = _ambiguous(state, ext)
    targets = [] if (q is None or confirm) else q["targets"]
    qid = q["id"] if q is not None and not confirm else ("confirm_segment" if confirm else None)
    for u in _age_first(ext["slot_updates"]):
        s = u["slot"]
        if u["value"] is None:
            state.notes[s] = scrub_note(u.get("raw"))
            ev(state, "note_unnormalizable", slot=s)
            continue
        current = s in targets
        evidence = u.get("evidence")  # 05-M12: NFKC 後の発言の部分文字列のときだけ根拠として数える
        ev_ok = isinstance(evidence, str) and bool(nfkc(evidence)) and nfkc(evidence) in nfkc(text)
        kind = "free_current" if current else "free_other"
        conf = "low" if s in ambiguous else confidence_for(kind, state.phase, s, ev_ok)
        frm = f"free:{qid}" if current else (f"free_other:{qid}" if qid else "free_other")
        apply_update(state, s, u["value"], conf, frm)
    _apply_ng(state, ext)


def _count_aside(state):
    """脱線と危険話題は MAX_ASIDES を分け合う(05-M5)。超えた分は数える。"""
    state.aside_count += 1
    if state.aside_count > MAX_ASIDES:
        _count(state, "aside")
        return True
    return False


def _aside(state, bank, llm, catalog, kind):
    """脱線・危険話題: 状態を変えずに、今のものを出し直す。危険話題は抽出も生成もしない。"""
    if kind == "danger":
        _flag(state, "danger_warned")
        ev(state, "danger_skip")
    counted = _count_aside(state)
    if counted and state.turns >= MAX_TURNS:
        advance(state, bank)
        return _say(state, bank, llm, catalog, counted=True, extra={"aside": kind})
    if kind == "danger":
        r = _represent(state, bank, counted=counted, aside=True, lead=[NOTICE["danger"]])
        r["view"]["aside"] = "danger"
        return r
    return _say(state, bank, llm, catalog, counted=counted, extra={"aside": kind}, aside=True)


def _represent(state, bank, counted, prefix=None, aside=False, lead=None):
    """今のものを LLM を呼ばずに出し直す。"""
    v = view(state, bank, aside=aside)
    body = v["card_text"] + "\n\n" + CLOSING_JA if v["mode"] == "review" else v.get("text", "")
    lead = list(lead or [])
    parts = lead + [n for n in v["notices"] if n not in lead] + _discard_lead(state) + ([prefix] if prefix else []) + [body]
    return {"reply": "\n".join(parts), "view": v, "done": False, "counted": counted}


def _discard_lead(state):
    """このターンに捨てた保留の答えを、決まり文句で伝える(返事が「覚えた」と言わないように)。"""
    _init_volatile(state)
    if not state._discarded:
        return []
    return [DISCARD_TEXT.format(labels="・".join(SLOTS[s]["label"] for s in state._discarded))]


def _say(state, bank, llm, catalog, counted=True, extra=None, aside=False):
    """Stage 3。注意文は生成した返事の前にここで付ける。"""
    v = view(state, bank, aside=aside)
    if extra:
        v.update(extra)
    notices = list(v["notices"]) + _discard_lead(state)
    if state.current["mode"] == "result":
        cat = catalog or CATALOG
        cby = {j["id"]: j for j in cat}
        payload = build_result_payload(state, cat)
        payload["unkept"] = v.get("unkept")  # 生成の検証だけが読む(result_json には入れない)
        keys = ("card", "candidates", "need", "unknown_hard", "unresolved", "notices", "blocked", "conditional")
        v["result_json"] = json.dumps({k: payload[k] for k in keys}, ensure_ascii=False, default=str)
        out = None
        if payload["need"] > 0:  # need=0(ブロック・候補なし)では LLM を呼ばない
            def _vres(raw):
                r, err = validate_result(raw, payload)
                return r, ([err] if err else [])
            out = _call_llm(llm.generate, v, _vres, state, "result")
        if out is None:
            out = fallback_result(payload)
        out = post_check_result(state, payload, out, cby)
        txt = compose_result_text(payload, out, cby)
        lead = [NOTICE[k] for k in state._new_notices
                if NOTICE[k] not in payload["notices"] and not (payload["blocked"] and k == "minor_rules")] + _discard_lead(state)
        txt = "\n".join(lead + [txt])
        state.result = {"text": txt, "recommendations": out["recommendations"], "payload": payload}
        return {"reply": txt, "view": v, "done": True, "counted": counted}

    def _vr(raw):
        r, err = validate_reply(raw, v)
        return r, ([err] if err else [])
    reply = _call_llm(llm.generate, v, _vr, state, "generate")
    if reply is None:
        reply = v["text"] if v["mode"] != "review" else v["card_text"] + "\n\n" + CLOSING_JA
    return {"reply": "\n".join(notices + [reply]), "view": v, "done": False, "counted": counted}


# ----------------------------- プロンプトの組み立て(§9-1) -----------------------------
PROMPT_FILES = {  # 常時オンの main と、フェーズ別の追加
    "extract": ["prompt_extract_v4_ja.txt"],
    "say": ["prompt_main_v4_ja.txt"],
    "review": ["prompt_main_v4_ja.txt", "prompt_review_v4_ja.txt"],
    "result": ["prompt_main_v4_ja.txt", "prompt_result_v4_ja.txt"],
}


def prompt_stage(v):
    return {"review": "review", "result": "result"}.get(v.get("mode"), "say")


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
