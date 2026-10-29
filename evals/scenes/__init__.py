"""会話の層の場面の一覧(設計メモ docs/design/conversation-layer.md §3)。入り口をまたいで同じ形の、設計の物差し。

1 場面 = 入り口 + 架空の前の会話 + 本人の発言 + 期待する振る舞い(言葉でなく、確かめられる条件)。どれも架空。
入り口(entry)ごとに 1 フォルダ、種類(category)ごとに 1 ファイル(evals/scenes/<entry>/<category>.json。中身は場面のリスト)。
ここは読み込みと形の確かめ。場面を回して条件を判定する仕組みは、下の docstring の終わりにあるモジュール。

場面の形:
- id: 一覧の中で重ならない名前。title: 何の場面か。source: 出どころ(本番のトークなど。任意)。note: 条件の補い(任意)
- entry: どの入り口の途中か(ENTRIES)。フォルダの名前と同じ。talk は相談の外(トークの AI。最初のひとことなど)
- form: 形(B / C / D)。career だけ。無ければ C
- segment: 始めの区分。career は same_path / change / return / early / unknown、baito は student / homemaker / side_job /
  other / unknown。talk には置かない
- known: 始めに分かっている欄({slot: {...}})。slot はその入り口の欄の名前(career は dip_mcp.career.harness.SLOTS、
  baito は dip_mcp.baito.harness.SLOTS と近くで探すのエリア near.area)。career は {"text", "status", "confidence",
  "number", "range", "unit", "from"}、baito は {"value", "confidence", "from"}(value は SLOTS の区切りのキー)。
  status の既定は filled、confidence の既定は high。from は出どころ(入り口をまたいで引き継いだ値なら、元の入り口の名前)
- profile: 共通の値(入り口をまたいで dip に残っている値。{key: text}。key は common_keys():
  区分 track と、dip の希望の項目 stated_preferences の名前 dip_mcp.logic.PREFERENCE_KEYS)。任意
- history: 架空の前の会話([{"who": "agent" | "user", "text"}]。judge._log と同じ形)
- asking: 今出している問いの id(その入り口の質問バンクの id。career の職歴カードは card_role1 のように広げた名前)。
  まとめは review。talk には置かない
- says: 本人の発言([{"text", "button", "reading"?}])。button は押したボタンか(打った文なら false)。2 つ以上なら続けて送る。
  reading は読む段の正解(設計メモ §4.3 の act の形。下の ACTS)。career と baito の打った文には必ず置き、ボタンには置かない
  (ボタンは読む段を呼ばない)。talk は任意(トークの AI は読む段を持たない)。形は
  {"acts": [{"act", ...}], "hook"?: 相づちに使う本人の言葉, "mentions"?: [候補に使える本人の言葉]}。
  acts が空なら、働きを読めない発言(「はい」「うん」だけ)。値の act の text は発言の部分文字列、number は発言に数字で
  書いてある数、baito の value はその欄の区切りのキー(複数の値の欄は list)。slot "segment" の value は区分
- expect: 期待する振る舞い([{"check", "at"?, ...引数}])。check は下の CODE_CHECKS か JUDGE_CHECKS の名前。
  at は何番目の返信を見るか(0 から。既定は最後。"all" はすべて)。状態を見る check(slot など)も、その返信の後の状態を見る。
  状態を見る check の entry は、どの入り口の状態を見るか(既定は場面の entry。入り口を移した後の状態を見るときに書く)

場面を回して条件を判定するのは evals/scenes/runner.py(python -m evals.scenes)、条件の判定は checks.py、
採点の条件のジャッジは judge.py。
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from dip_mcp.convo import acts as convo_acts

DIR = Path(__file__).resolve().parent

# 入り口(設計メモ §2.1)。場面のフォルダの名前
ENTRIES = {
    "career": "正社員ヒアリング(dip_mcp/career/)",
    "baito": "アルバイト・パートの相談(dip_mcp/baito/、v4 と v5)",
    "talk": "トークの AI(相談の外。最初のひとこと、相談の前後)",
}
# 場面の種類
CATEGORIES = {
    "chat": "雑談、関係ない話(天気、食べ物、愚痴、冗談、絵文字やスタンプだけ)",
    "ask": "逆に質問される",
    "multi": "1 回に複数の答え、まだ聞いていない問いへの答え",
    "correct": "前の答えを直す、取り消す、迷う、「わからない」「特にない」",
    "emotion": "長い身の上話、感情(不安、怒り、疲れ)、ネガティブな転職理由",
    "safety": "答えたくない、個人情報を書いてしまう、危ない話",
    "short": "短すぎる答え、ボタンと違う言い方、打ち間違い、話し言葉",
    "flow": "「他の候補」「スキップ」「やめる」「あとで」、途中から再開",
    "switch": "入り口をまたぐ(別の入り口の話、最初のひとこと、値の引き継ぎ)",
    "t326": "jyoka/tasks#326 のトーク 1〜7",
}
MIN_SCENES = 60  # 全部の入り口を合わせた最低の数(task 327 の Goal)
MIN_PER_CATEGORY = 3  # 入り口を合わせた、1 種類あたりの最低の数
# 入り口ごとに置く種類(設計メモ §3.2)。switch はどの入り口にも置く
ENTRY_CATEGORIES = {
    "career": tuple(CATEGORIES),
    "baito": ("chat", "ask", "multi", "correct", "emotion", "safety", "short", "flow", "switch"),
    "talk": ("switch",),
}
FORMS = ("B", "C", "D")
SEGMENTS = {
    "career": ("same_path", "change", "return", "early", "unknown"),
    "baito": ("student", "homemaker", "side_job", "other", "unknown"),
}
STATUSES = ("filled", "passed", "dont_know")
CONFIDENCES = ("high", "low")
# 共通の値(設計メモ §4.5)は、区分(search_track)と dip の希望の項目(stated_preferences)。名前は common_keys()
BAITO_EXTRA_SLOTS = {"near.area"}  # 近くで探すのエリア(SLOTS には無く、state.near に入る)

# コードで判定する条件。値は {引数: 型}。型の後ろの ? は任意
CODE_CHECKS = {
    "asks": {"qid": "str?", "slot": "str?", "entry": "str?"},  # 次の問いがこの id か、この欄を聞く(どちらか 1 つ)
    "not_asks": {"slots": "list", "entry": "str?"},         # 次の問いがこの欄を聞かない(聞き直さない)
    "keeps_question": {},                                   # 今の問い(asking)を出し直す。候補のボタンも出す
    "slot": {"slot": "str", "contains": "str?", "not_contains": "list?", "value": "any?", "status": "str?",
             "confidence": "str?", "number": "num?", "entry": "str?"},  # 欄に値があり、引数のとおり(contains は値の字に含む。
                                                            # not_contains は値の字にどれも無い(ほかの欄の言葉が混じらない)。
                                                            # value は baito の区切りのキー。複数の値の欄なら含む)
    "slot_not_high": {"slot": "str", "entry": "str?"},      # 欄が空か推測(low)。確かめた値(high)にしない
    "slot_open": {"slot": "str", "entry": "str?"},          # 欄が空(まだ聞いていない扱い)
    "slot_unchanged": {"slot": "str"},                      # 欄が known のまま
    "no_slot_contains": {"texts": "list"},                  # どの欄の値にもこの字が無い(保存しない)
    "profile": {"key": "str", "contains": "str?"},          # 共通の値に残っている(入り口を移しても失わない)
    "reply_contains": {"any": "list"},                      # 返信がどれか 1 つを含む
    "reply_not_contains": {"texts": "list"},                # 返信がどれも含まない
    "choices_include": {"labels": "list"},                  # ボタンにどれも含む
    "choices_exclude": {"labels": "list"},                  # ボタンにどれも含まない
    "choices_ascending": {},                                # 金額のボタンが小さい順(「こだわらない」などは数えない)
    "order": {"parts": "list"},                             # 吹き出しの中の部品の順(設計メモ §4.6。ORDER_PARTS)
    "ack_varies": {"turns": "int"},                         # 相づちの文の終わり 4 字が、history も入れて turns 回続けて同じにならない
    "no_repeat": {"turns": "int"},                          # 問いの文のほかの同じ文が、turns 回続けて出ない
    "no_made_up_number": {},                                # 返信の数が、本人の発言(says、history)、known、ボタンのラベル、
                                                            # 場面の answers_from_facts の facts のどれかにある
    "bubble_max": {"chars": "int"},                         # 吹き出し 1 つの字数(空白を数えない)
    "one_question": {},                                     # 1 回の返信で問いは 1 つ
    "counted": {},                                          # ターンに数える
    "not_counted": {},                                      # ターンに数えない
    "closed": {"why": "str?"},                              # 相談を閉じた
    "not_closed": {},                                       # 相談を閉じない
    "phase": {"phase": "str"},                              # 最後の発言の後のフェーズ
    "crisis_reply": {},                                     # 危ない言葉への決まった文(相談窓口)。モデルを呼ばない
    "llm_calls_max": {"n": "int"},                          # その発言でのモデルの呼び出しの数の上限
    "switches_to": {"entry": "str"},                        # このターンで、その入り口に移る(今の相談を閉じ、値を引き継ぐ)
    "stays": {},                                            # 入り口を移さない(相談の外なら、相談を始めない)
    "offers_entries": {"entries": "list"},                  # 移らずに、その入り口を始めるボタンを出す
}
# LLM の採点で判定する条件(設計メモ §4.10)。facts は採点に渡す事実(この範囲の外を言ったら不合格)
JUDGE_CHECKS = {
    "responds_first": {},             # 問いに戻る前に、本人の発言に 1 文で応じた
    "answers_from_facts": {"facts": "list"},  # 逆質問に、渡した事実の範囲で答えた
    "no_invented_facts": {},          # 求人、会社、相場、制度などの事実を作っていない
    "empathy": {},                    # 気持ちを受け止めた。評価、説教、根拠のない励ましや約束をしていない
    "no_advice_beyond": {},           # 医療、法律、労務の判断をしていない
    "personal_touch": {},             # 本人の言葉に合わせた一言がある(決まり文句でない)
    "no_pressure": {},                # 答えを迫らない
    "no_falsify": {},                 # 事実と違う内容を書く頼みに応じない
    "light_tone": {},                 # 冗談に軽く応じ、ばかにしない
    "acknowledges_prior": {},         # 前の相談やほかの入り口で分かっていることを踏まえている
    "natural": {},                    # 決まった文の貼り合わせに見えない
    "tone_matches": {},               # 入り口の言い回しの調子(baito はくだけた話し言葉、career は丁寧語)に合っている
}
CHECK_NAMES = {**CODE_CHECKS, **JUDGE_CHECKS}
ORDER_PARTS = ("ack", "answer", "note", "phase_line", "ask")  # 相づち、逆質問の答え、注意、区切り、問い
TYPES = {"str": str, "list": list, "int": int, "num": (int, float), "any": (str, int, float)}
STATE_CHECKS = ("asks", "not_asks", "slot", "slot_not_high", "slot_open")  # entry を書ける(入り口を移した後を見る)

# 読む段の正解の act(設計メモ §4.3)。会話の層の読む段と同じ型(dip_mcp/convo/acts.py)。値は {引数: 型}
ACTS, VALUE_ACTS, VALUE_ARGS = convo_acts.ACTS, convo_acts.VALUE_ACTS, convo_acts.VALUE_ARGS
FEELINGS, STRENGTHS = convo_acts.FEELINGS, convo_acts.STRENGTHS
HOOK_MAX, MENTION_MAX = convo_acts.HOOK_MAX, convo_acts.MENTION_MAX  # 相づちに使う本人の言葉と、候補に使う本人の言葉の字数


def is_code(check: dict) -> bool:
    return check["check"] in CODE_CHECKS


# ---- 入り口ごとの欄と問い。harness を読むのはここだけ(読み込むだけなら dip_mcp を要らないように) ----

def _slots(entry: str) -> set[str]:
    if entry == "career":
        from dip_mcp.career import harness as H

        return set(H.ALL_SLOTS)
    if entry == "baito":
        from dip_mcp.baito import harness as B

        return set(B.ALL_SLOTS) | BAITO_EXTRA_SLOTS
    return set()


def _question_ids(entry: str, form: str | None = None, segment: str | None = None) -> set[str]:
    """その入り口で出せる問いの id。segment が None なら、区分によらずバンクの全部。"""
    if entry == "career":
        from dip_mcp.career import harness as H

        st = H.new_state(form or "C")
        st.segment = segment or "unknown"
        qs = H.questions(st, H.BANK)
        ok = qs if segment is None else [q for q in qs if H._segment_ok(q, st)]
        return {q["id"] for q in ok} | {"review", "start"}
    if entry == "baito":
        from dip_mcp.baito import harness as B

        st = B.new_state()
        st.segment = segment or "unknown"
        ok = B.BANK if segment is None else [q for q in B.BANK if B._segment_ok(q, st)]
        return {q["id"] for q in ok} | {"review"}
    return set()


def _choices(entry: str, qid: str, form: str | None, segment: str) -> set[str]:
    if entry == "career":
        from dip_mcp.career import harness as H

        if qid == "review":
            return {H.REVIEW_OK}
        if qid == "start":
            return {H.START_GO, H.LATER}
        st = H.new_state(form or "C")
        st.segment = segment
        q = H.qmap(st, H.BANK)[qid]
        return set(q["choices"]) | set(q.get("escape", [])) | {H.MORE}
    from dip_mcp.baito import harness as B

    if qid == "review":
        return {B.REVIEW_OK}
    return set(B.qmap(B.BANK)[qid]["choices"])


def common_keys() -> set[str]:
    """共通の値の名前: 区分(track)と、dip の希望の項目(dip_mcp.logic.PREFERENCE_KEYS)。"""
    from dip_mcp import logic

    return {"track"} | set(logic.PREFERENCE_KEYS)


def _baito_values(slot: str) -> set | None:
    """baito の欄の区切りのキー(near.area はエリアのボタン)。"""
    from dip_mcp.baito import harness as B

    if slot == "near.area":
        return set(B.NEAR_AREAS)
    return set(B.SLOTS[slot]["values"]) or None


def _typed(v, t) -> bool:
    return not isinstance(v, bool) and isinstance(v, t)


def _check_args(sid: str, c: dict) -> None:
    name = c.get("check")
    if name not in CHECK_NAMES:
        raise ValueError(f"{sid}: 知らない check {name}")
    spec = CHECK_NAMES[name]
    for k, v in c.items():
        if k in ("check", "at"):
            continue
        if k not in spec:
            raise ValueError(f"{sid}: {name} に知らない引数 {k}")
        if not _typed(v, TYPES[spec[k].rstrip("?")]):
            raise ValueError(f"{sid}: {name} の {k} の型")  # データの誤りは、ほかの確かめと同じく ValueError
    for k, t in spec.items():
        if not t.endswith("?") and k not in c:
            raise ValueError(f"{sid}: {name} に {k} が無い")
    if name == "asks" and ("qid" in c) == ("slot" in c):
        raise ValueError(f"{sid}: asks は qid か slot のどちらか 1 つ")
    if name == "order" and (not c["parts"] or any(p not in ORDER_PARTS for p in c["parts"])):
        raise ValueError(f"{sid}: order の parts は {ORDER_PARTS} から")
    if name == "answers_from_facts" and not c["facts"]:
        raise ValueError(f"{sid}: answers_from_facts に facts が無い")
    if name == "profile" and c["key"] not in common_keys():
        raise ValueError(f"{sid}: profile の知らない共通の値 {c['key']}")
    if name == "offers_entries" and (not c["entries"] or any(e not in ENTRIES for e in c["entries"])):
        raise ValueError(f"{sid}: offers_entries の entries は {tuple(ENTRIES)} から")
    at = c.get("at")
    if at is not None and at != "all" and (isinstance(at, bool) or not isinstance(at, int) or at < 0):
        raise ValueError(f"{sid}: at は 0 以上の数か all")


def _check_known(sid: str, entry: str, slots: set[str], known: dict) -> None:
    for k, e in known.items():
        if k not in slots:
            raise ValueError(f"{sid}: known の知らない欄 {k}")
        if e.get("status", "filled") not in STATUSES or e.get("confidence", "high") not in CONFIDENCES:
            raise ValueError(f"{sid}: known の {k} の status か confidence")
        if entry == "baito":
            values = _baito_values(k)
            got = e.get("value")
            got = got if isinstance(got, list) else [got]
            if values and any(v not in values for v in got):
                raise ValueError(f"{sid}: known の {k} の value {e.get('value')} が区切りに無い")


def nfkc(s) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip()


def _digits(text: str) -> set[str]:
    """発言に数字で書いてある数(全角は半角に、桁の区切りの , は外す)。"""
    return set(re.findall(r"\d+(?:\.\d+)?", nfkc(text).replace(",", "")))


def _check_reading(sid: str, entry: str, slots: set[str], say: dict) -> None:
    """読む段の正解(say の reading)の形。値の act は、発言の部分文字列と、その入り口の欄と区切りか。"""
    r = say.get("reading")
    if r is None:
        if not say["button"] and entry != "talk":
            raise ValueError(f"{sid}: 打った文「{say['text']}」に reading が無い")
        return
    if say["button"]:
        raise ValueError(f"{sid}: ボタン「{say['text']}」に reading は置かない(ボタンは読む段を呼ばない)")
    if not isinstance(r, dict) or not isinstance(r.get("acts"), list) or set(r) - {"acts", "hook", "mentions"}:
        raise ValueError(f"{sid}: reading の形")
    said = nfkc(say["text"])
    words = ([r["hook"]] if "hook" in r else []) + list(r.get("mentions") or [])
    if any(not isinstance(w, str) or not w or nfkc(w) not in said for w in words):
        raise ValueError(f"{sid}: reading の hook と mentions は発言の部分文字列")
    if len(r.get("hook", "")) > HOOK_MAX or any(len(m) > MENTION_MAX for m in r.get("mentions") or []):
        raise ValueError(f"{sid}: reading の hook({HOOK_MAX} 字まで)か mentions({MENTION_MAX} 字まで)が長い")
    for a in r["acts"]:
        name = a.get("act") if isinstance(a, dict) else None
        if name not in ACTS:
            raise ValueError(f"{sid}: 知らない act {name}")
        spec = ACTS[name]
        for k, v in a.items():
            if k == "act":
                continue
            if k not in spec:
                raise ValueError(f"{sid}: act {name} に知らない引数 {k}")
            ok = isinstance(v, (str, int, list)) if spec[k] == "any?" else _typed(v, TYPES[spec[k].rstrip("?")])
            if not ok:
                raise ValueError(f"{sid}: act {name} の {k} の型")
        for k, t in spec.items():
            if not t.endswith("?") and k not in a:
                raise ValueError(f"{sid}: act {name} に {k} が無い")
        if "slot" in a and a["slot"] not in slots | {"segment"}:
            raise ValueError(f"{sid}: act {name} の知らない欄 {a['slot']}")
        for k in ("text", "span"):
            if k in a and (not a[k] or nfkc(a[k]) not in said):
                raise ValueError(f"{sid}: act {name} の {k}「{a[k]}」が発言の部分文字列でない")
        if "number" in a and nfkc(str(a["number"]).removesuffix(".0")) not in _digits(say["text"]):
            raise ValueError(f"{sid}: act {name} の number {a['number']} が発言に数字で無い(数を作らない)")
        if name in VALUE_ACTS:
            _check_value(sid, entry, a)
        if name == "switch" and (a["to"] not in ENTRIES or a["to"] in (entry, "talk") or a["strength"] not in STRENGTHS):
            raise ValueError(f"{sid}: switch の to か strength")
        if name == "emotion" and a["feeling"] not in FEELINGS:
            raise ValueError(f"{sid}: emotion の feeling は {FEELINGS} から")


def _check_value(sid: str, entry: str, a: dict) -> None:
    """値の act の value: segment は区分、baito はその欄の区切りのキー(career の value は使わない)。"""
    if a["slot"] == "segment":
        if a.get("value") not in SEGMENTS.get(entry, ()) or a.get("value") == "unknown":
            raise ValueError(f"{sid}: segment の value {a.get('value')}")
        return
    if entry == "baito":
        values = _baito_values(a["slot"])
        got = a.get("value")
        if got is None or (values and any(v not in values for v in (got if isinstance(got, list) else [got]))):
            raise ValueError(f"{sid}: act {a['act']} の value {got} が {a['slot']} の区切りに無い")
    elif "value" in a:
        raise ValueError(f"{sid}: career の値の act に value は置かない(text と number で書く)")


def _check_state(sid: str, s: dict, c: dict, switched: set[str]) -> None:
    """状態を見る check の欄と問いが、見る入り口にあるか。"""
    entry = c.get("entry", s["entry"])
    if entry not in ENTRIES or entry == "talk":
        raise ValueError(f"{sid}: {c['check']} の entry {entry} には状態が無い")
    if entry != s["entry"] and entry not in switched:
        raise ValueError(f"{sid}: {c['check']} の entry {entry} に移る switches_to が無い")
    slots = _slots(entry)
    same = entry == s["entry"]
    if c["check"] == "asks" and "qid" in c:
        ids = _question_ids(entry, s.get("form"), s.get("segment")) if same else _question_ids(entry)
        if c["qid"] not in ids:
            raise ValueError(f"{sid}: asks の問い {c['qid']} が無い")
    if c["check"] in ("slot", "slot_open", "slot_unchanged", "slot_not_high") and c["slot"] not in slots:
        raise ValueError(f"{sid}: {c['check']} の知らない欄 {c['slot']}")
    if c["check"] == "not_asks" and any(x not in slots for x in c["slots"]):
        raise ValueError(f"{sid}: not_asks の知らない欄")
    if c["check"] == "slot":
        if c.get("status", "filled") not in STATUSES or c.get("confidence", "high") not in CONFIDENCES:
            raise ValueError(f"{sid}: slot の status か confidence")
        if "value" in c:
            values = _baito_values(c["slot"]) if entry == "baito" else None
            if not values or c["value"] not in values:
                raise ValueError(f"{sid}: slot の value {c['value']} が {c['slot']} の区切りに無い")


def validate(s: dict) -> None:
    """1 場面の形を確かめる。"""
    sid = s.get("id") or "?"
    for k in ("id", "entry", "category", "title", "says", "expect"):
        if k not in s:
            raise ValueError(f"{sid}: {k} が無い")
    entry = s["entry"]
    if entry not in ENTRIES:
        raise ValueError(f"{sid}: 知らない entry {entry}")
    if s["category"] not in CATEGORIES:
        raise ValueError(f"{sid}: 知らない category {s['category']}")
    if s["category"] not in ENTRY_CATEGORIES[entry]:
        raise ValueError(f"{sid}: {entry} に置かない category {s['category']}")
    form = s.get("form")
    if entry == "career":
        form = form or "C"
        if form not in FORMS:
            raise ValueError(f"{sid}: form {form}")
    elif form is not None:
        raise ValueError(f"{sid}: form は career だけ")
    if entry == "talk":
        for k in ("segment", "asking", "known"):
            if k in s:
                raise ValueError(f"{sid}: talk(相談の外)に {k} は置かない")
    else:
        for k in ("segment", "asking"):
            if k not in s:
                raise ValueError(f"{sid}: {k} が無い")
        if s["segment"] not in SEGMENTS[entry]:
            raise ValueError(f"{sid}: segment {s['segment']}")
        _check_known(sid, entry, _slots(entry), s.get("known") or {})
        if s["asking"] not in _question_ids(entry, form, s["segment"]):
            raise ValueError(f"{sid}: {entry} の区分 {s['segment']}(形 {form})で出せない問い {s['asking']}")
    for k in s.get("profile") or {}:
        if k not in common_keys():
            raise ValueError(f"{sid}: profile の知らない共通の値 {k}")
    for t in s.get("history") or []:
        if t.get("who") not in ("agent", "user") or not str(t.get("text") or "").strip():
            raise ValueError(f"{sid}: history の形")
    if not s["says"]:
        raise ValueError(f"{sid}: says が空")
    for x in s["says"]:
        if not str(x.get("text") or "").strip() or not isinstance(x.get("button"), bool) or set(x) - {"text", "button", "reading"}:
            raise ValueError(f"{sid}: says の形")
        _check_reading(sid, entry, _slots(entry) if entry != "talk" else set().union(*map(_slots, ("career", "baito"))), x)
    first = s["says"][0]
    if first["button"] and (entry == "talk" or first["text"] not in _choices(entry, s["asking"], form, s["segment"])):
        raise ValueError(f"{sid}: 押したボタン {first['text']} が {s.get('asking')} のボタンに無い")
    if not s["expect"]:
        raise ValueError(f"{sid}: expect が空")
    switched = set()
    for c in s["expect"]:
        _check_args(sid, c)
        if c["check"] == "switches_to":
            if c["entry"] not in ENTRIES or c["entry"] == entry:
                raise ValueError(f"{sid}: switches_to の entry {c['entry']}")
            switched.add(c["entry"])
    for c in s["expect"]:
        if c["check"] in STATE_CHECKS or c["check"] == "slot_unchanged":
            _check_state(sid, s, c, switched)
        if c["check"] == "slot_unchanged" and c["slot"] not in (s.get("known") or {}):
            raise ValueError(f"{sid}: slot_unchanged の {c['slot']} が known に無い")
        if c["check"] == "keeps_question" and entry == "talk":
            raise ValueError(f"{sid}: talk には今の問いが無い(keeps_question は置けない)")
        at = c.get("at")
        if isinstance(at, int) and at >= len(s["says"]):
            raise ValueError(f"{sid}: at {at} は返信の数を超える")
    if not any(is_code(c) for c in s["expect"]):
        raise ValueError(f"{sid}: コードで判定する条件が 1 つも無い(費用 0 で回せる条件を 1 つは置く)")


def files() -> list[Path]:
    """場面のファイル(入り口のフォルダの順、ファイル名の順)。"""
    return [f for e in ENTRIES for f in sorted((DIR / e).glob("*.json"))]


def load_scenes() -> list[dict]:
    """全部の場面を、入り口の順、ファイル名の順、ファイルの中の順に。"""
    out = []
    for f in files():
        for s in json.loads(f.read_text(encoding="utf-8")):
            validate(s)
            out.append(s)
    ids = [s["id"] for s in out]
    if len(ids) != len(set(ids)):
        raise ValueError("場面の id が重なっている")
    return out


def counts(scenes: list[dict], entry: str | None = None) -> dict[str, int]:
    """種類ごとの場面の数(entry を渡せば、その入り口だけ)。"""
    out = dict.fromkeys(CATEGORIES if entry is None else ENTRY_CATEGORIES[entry], 0)
    for s in scenes:
        if entry is None or s["entry"] == entry:
            out[s["category"]] += 1
    return out
