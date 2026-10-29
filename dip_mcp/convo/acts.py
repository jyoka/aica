"""発言の働き(act)の型と、読む段の出力の検証(設計メモ docs/design/conversation-layer.md §4.3)。

読む段は、1 つの発言から act の並びを返す。1 つの発言に act は何個あってもよく、1 つも無い発言もある(「はい」「うん」だけ)。
出力の形は {"acts": [{"act", ...引数}], "hook": 相づちに使う本人の言葉, "mentions": [候補に使える本人の言葉]}。
入り口の定義が足す欄(正社員の ng など)は、入り口が読む。

検証(validate_reading)は、中身が発言から取ったものかを確かめる。値の text と、氏名・要配慮の span は今の発言の部分文字列、
number は発言に数字で書いてある数、slot は入り口の欄、区切りの欄(アルバイト・パート)の value は区切りのキー。
合わない act は捨てて、問題の名前を残す(出力の全体は捨てない)。形が壊れていて使えないときだけ None を返す(呼ぶ側が 1 回やり直す)。

場面の一覧(evals/scenes/)の読む段の正解(reading)も、同じ型で書く。
"""

from __future__ import annotations

from dip_mcp.convo.text import nfkc, number_in_text
from dip_mcp.convo.turn import parse_json

# 値の act: 今の問いへの答え、ほかの欄の値(まだ聞いていない欄も、もう答えた欄への言い足しも)、直す、迷う
VALUE_ACTS = ("answer", "add", "correct", "unsure")
VALUE_ARGS = {"slot": "str", "text": "str", "number": "num?", "unit": "str?", "choice": "str?", "value": "any?"}
OPERATIONS = ("more", "clarify", "go_back", "show_result", "restart", "later", "stop")  # 操作
TALK = ("chitchat", "ask_service", "ask_advice", "story", "complaint", "falsify")  # 話(値ではない)
GUARD = ("crisis", "danger")  # 守り(読む段の後ろの網。危ない言葉の表と闇バイトの言葉に当たらない言い方)
ACTS = {
    **dict.fromkeys(VALUE_ACTS, VALUE_ARGS),
    "retract": {"slot": "str"},                          # もう答えた欄を取り消す
    "dont_know": {"slot": "str?"}, "pass": {"slot": "str?"},  # 覚えていない、言いたくない・特にない(既定は今の問いの欄)
    **{a: {} for a in OPERATIONS},
    "confirm": {},                                       # まとめに「合ってる」と答える
    "switch": {"to": "str", "strength": "str"},          # 別の入り口の話。strength は want(移りたい)か consider(考えている)
    **{a: {} for a in (*TALK, *GUARD)},
    "emotion": {"feeling": "str"},                       # 感情(FEELINGS)
    "sensitive": {"span": "str"}, "name": {"span": "str"},  # 要配慮の部分、氏名の部分(発言の部分文字列)
}
FEELINGS = ("anxious", "angry", "tired", "sad")
STRENGTHS = ("want", "consider")
SPAN_ACTS = ("sensitive", "name")
HOOK_MAX = 15  # 相づちに使う本人の言葉の字数(§4.3)
MENTION_MAX = 10  # 候補に使う本人の言葉の字数(§4.7)
MAX_ACTS = 12  # 1 つの発言から受け取る act の数の上限
TYPES = {"str": str, "num": (int, float), "any": (str, int, float, list)}


def _typed(v, t: str) -> bool:
    return not isinstance(v, bool) and isinstance(v, TYPES[t.rstrip("?")])


def _in(part, said: str) -> bool:
    return isinstance(part, str) and bool(nfkc(part)) and nfkc(part) in said


def clean_act(a, said: str, slots, entries=(), values=None, segments=()) -> tuple[dict | None, str | None]:
    """1 つの act を確かめる。-> (確かめた act, 問題)。捨てるなら (None, 問題)。数が発言に無ければ数だけ外す。
    slots は入り口の欄、entries は移れる入り口、values は区切りの欄の {slot: 区切りのキーの集まり}(無ければ文の欄)、
    segments は区分(値の act の slot "segment" の value)。"""
    name = a.get("act") if isinstance(a, dict) else None
    if name not in ACTS:
        return None, f"unknown_act:{name}"
    spec = ACTS[name]
    out = {"act": name}
    bad_args = []
    for k, t in spec.items():
        v = a.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            if not t.endswith("?"):
                return None, f"{name}_missing:{k}"
            continue
        if not _typed(v, t):
            if t.endswith("?"):  # 任意の引数の型だけが違う(数を文で返したなど): その引数だけ外す
                bad_args.append(k)
                continue
            return None, f"{name}_bad_type:{k}"
        out[k] = nfkc(v) if isinstance(v, str) and k != "value" else v
    probs = f"{name}_bad_type:{','.join(bad_args)}" if bad_args else None
    if "slot" in out and out["slot"] not in set(slots) | ({"segment"} if segments else set()):
        return None, f"unknown_slot:{out['slot']}"
    if name in VALUE_ACTS:
        if not _in(out["text"], said):
            return None, f"text_not_said:{out['slot']}"
        if "number" in out and not number_in_text(out["number"], said):
            out.pop("number")
            out.pop("unit", None)
            probs = f"number_not_said:{out['slot']}"
        elif "unit" in out and ("number" not in out or nfkc(out["unit"]) not in said):
            out.pop("unit")
        if out["slot"] == "segment":
            if out.get("value") not in segments:
                return None, "invalid_segment"
        elif values is not None and out["slot"] in values:
            got = out.get("value")
            got = got if isinstance(got, list) else [got]
            if not got or any(v not in values[out["slot"]] for v in got):
                return None, f"value_not_in_buckets:{out['slot']}"
        else:
            out.pop("value", None)  # 文の欄の値は text と number で持つ
    if name in SPAN_ACTS and not _in(out["span"], said):
        return None, f"{name}_not_said"
    if name == "switch" and (out["to"] not in entries or out["strength"] not in STRENGTHS):
        return None, "switch_bad"
    if name == "emotion" and out["feeling"] not in FEELINGS:
        return None, "emotion_bad"
    return out, probs


def validate_reading(raw, user_text, slots, entries=(), values=None, segments=()) -> tuple[dict | None, list[str]]:
    """読む段の出力 -> (確かめた出力, 問題)。形が使えなければ (None, 問題)。確かめた出力は
    {"acts": [...], "hook": str | None, "mentions": [...], "extra": 入り口が読む欄(acts、hook、mentions のほか)}。"""
    d = parse_json(raw)
    if d is None:
        return None, ["not_json"]
    acts = d.get("acts")
    if not isinstance(acts, list):
        return None, ["acts_missing" if acts is None else "acts_bad_type"]
    said = nfkc(user_text)
    probs, out = [], []
    if len(acts) > MAX_ACTS:
        probs.append("too_many_acts")
    for a in acts[:MAX_ACTS]:
        act, p = clean_act(a, said, slots, entries, values, segments)
        if p:
            probs.append(p)
        if act is not None:
            out.append(act)
    hook = d.get("hook")
    if hook is not None and (not _in(hook, said) or len(nfkc(hook)) > HOOK_MAX):
        probs.append("hook_bad")
        hook = None
    mentions = d.get("mentions") or []
    mentions = mentions if isinstance(mentions, list) else []
    kept = [nfkc(m) for m in mentions if _in(m, said) and len(nfkc(m)) <= MENTION_MAX]
    if len(kept) != len(mentions):
        probs.append("mentions_bad")
    extra = {k: v for k, v in d.items() if k not in ("acts", "hook", "mentions")}
    return {"acts": out, "hook": nfkc(hook) if hook else None, "mentions": list(dict.fromkeys(kept)), "extra": extra}, probs


def has(reading: dict | None, *names) -> bool:
    return any(a["act"] in names for a in (reading or {}).get("acts", []))


def spans(reading: dict | None) -> list[str]:
    """氏名と要配慮の部分(値にも返事にも出さない)。"""
    return [a["span"] for a in (reading or {}).get("acts", []) if a["act"] in SPAN_ACTS]
