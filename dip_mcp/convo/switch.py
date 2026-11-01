"""入り口を移す、値を引き継ぐ(設計メモ docs/design/conversation-layer.md §4.5、§4.15)。どの入り口でも同じ決まり。

- 読んだこと → 振る舞い: switch の want(「やっぱり正社員で」「とりあえずバイトでいい」)は、今の相談を閉じて移る(wanted)。
  switch の consider(「正社員も考えてる」)は移らずに、移るボタンを出す(offered。押せば want と同じ)。打ち消し
  (「正社員じゃなくて」)は移らない(読む段が switch にしない。正規表現の頼みは hold で外す)
- 移る強い頼み(今の EXIT_REQUEST。入り口が持つ正規表現)に当たり、打ち消しや後回し(EXIT_HOLD)に当たらなければ、
  読む段を呼ばずに want にする(requested、want_reading)。自由に打った文を正規表現で読まない、の例外(§4.3)
- 引き継ぎの記録(handoff): 1 人の dict の KEY に 1 つだけ置き、移った先の相談が始めるときに読んで消す(take)。
  中身は、元の入り口(from)、移る先(to)、閉じたときの全部の値(slots。low も、出どころつき)、移る発言の中の値の act(acts)、
  移る発言(said。氏名と要配慮の部分を伏せたもの。移った先の最初の返事の言う段に渡す)、前に引き継いだ記録(prior。
  移った先から元の入り口へすぐ戻ったとき、元の値を戻すため。1 段だけ)。書いた時刻(at)から TTL を過ぎた記録は読まない。
  新しい会話でも消す(入り口の end。dip_mcp.career.session.end)。「すべて消す」は 1 人の dict ごと消すので、これも消える
- 値の対応(carry): 移った先の入り口の定義の carry({元の入り口: {元の欄: {"slot": 先の欄, "how": same / near}}})で写す。
  同じ意味(same)は元の確かさのまま、近い意味(near)は推測(low)で入れる。対応の無い欄は写さない(年収と時給は換算しない)。
  移る発言の中の値の act は、本人がそのターンに言ったまま(直接聞いていない)なので low。前に引き継いだ記録(prior)が先の入り口の
  ものなら、先の欄の名前のまま元の確かさで戻す。写した値をどの欄に入れるか(空の欄だけか)は入り口が決める
- 移った先の最初の返事(mentions。task 379): 相づちに、引き継いだ値の言葉を MENTION_MAX まで入れる(「名古屋、年収500万以上で、
  正社員で探します。」)。言葉は記録と希望の項目の文のまま。前の会話で分かっていることを、聞き直さずに踏まえていると見せるため

- トークの AI から始める(t1。§4.5、§4.13): トークの AI が start_* に本人の発言を渡し、移る先が会話の層の版なら、dip は始めずに
  from が talk の記録(talk_record)を書く。値はまだ読んでいない(read)。ランタイムが同じターンで移る先の switch_in を呼び、移る先は
  その発言を自分の読む段で読み(読んだ値の act は移る先の欄の名前)、記録の acts に入れてから当てる。値の対応は無く、欄の名前のまま推測(low)

引き継ぎの記録を書くのも読むのも、会話の層の版だけ(§4.15)。今の版(c1、v4)は書かず、読まない。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from dip_mcp.convo import acts as A
from dip_mcp.convo.text import nfkc
from dip_mcp.convo.values import merge

KEY = "handoff"
TTL = timedelta(minutes=30)  # 記録を読む期限。ランタイムが新しい会話を始める間(line_runtime.server.TALK_RESUME_WINDOW)と同じ
HOWS = ("same", "near")
ENTRY_KEYS = ("text", "value", "number", "range", "unit", "status", "confidence", "from")  # 記録に残す値の中身
TALK = "talk"  # トークの AI(相談の外)。トークの AI が始めた相談の記録の from


def wanted(reading: dict | None, entries) -> str | None:
    """移りたい(want)入り口。entries は今の入り口から移れる入り口。無ければ None。"""
    return next((a["to"] for a in (reading or {}).get("acts", []) if a["act"] == "switch" and a["strength"] == "want"
                 and a["to"] in entries), None)


def offered(reading: dict | None, entries) -> list[str]:
    """考えている(consider)入り口。移るボタンを出す先。"""
    out = [a["to"] for a in (reading or {}).get("acts", []) if a["act"] == "switch" and a["strength"] == "consider"
           and a["to"] in entries]
    return list(dict.fromkeys(out))


def requested(text: str, request_re, hold_re) -> bool:
    """移る強い頼み(入り口の EXIT_REQUEST)に当たり、打ち消しや後回し(EXIT_HOLD)に当たらないか。"""
    return bool(request_re.search(text)) and not hold_re.search(text)


def want_reading(to: str) -> dict:
    """移る強い頼みの読み(読む段を呼ばない)。"""
    return {"acts": [{"act": "switch", "to": to, "strength": "want"}], "hook": None, "mentions": [], "extra": {}}


def value_acts(reading: dict | None, scrub) -> list[dict]:
    """移る発言の中の値の act(記録に残す形)。scrub は値から氏名と要配慮の部分を除く関数。除いて何も残らなければ残さない。"""
    out = []
    for a in (reading or {}).get("acts", []):
        if a["act"] not in A.VALUE_ACTS:
            continue
        text = scrub(a["text"])
        if text:
            out.append({k: v for k, v in a.items() if k in ("act", "slot", "number", "unit", "value")} | {"text": text})
    return out


def record(source: str, to: str, slots: dict, acts: list[dict], said: str, at: datetime, prior: dict | None = None) -> dict:
    """引き継ぎの記録。slots は元の入り口の欄の値({欄: 値の dict})。prior は前に引き継いだ記録(移る先のものだけを 1 段)。"""
    kept = {s: {k: e.get(k) for k in ENTRY_KEYS} for s, e in slots.items() if e}
    rec = {"from": source, "to": to, "at": at.isoformat(), "slots": kept, "acts": list(acts), "said": said}
    if prior and prior.get("from") == to:
        rec["prior"] = {k: v for k, v in prior.items() if k != "prior"}
    return rec


def talk_record(to: str, said: str, at: datetime) -> dict:
    """トークの AI が始める相談の記録(t1)。始めた発言(said)は、移る先が自分の読む段で読む(unread)。"""
    return record(TALK, to, {}, [], said, at) | {"read": True}


def unread(rec: dict | None) -> bool:
    """まだ読んでいない、トークの AI からの記録か(移る先が said を読む段で読み、acts に入れてから当てる)。"""
    return bool(rec) and rec.get("from") == TALK and bool(rec.get("read"))


def write(state, line_user_id: str, rec: dict) -> None:
    state.update(line_user_id, lambda u: u.__setitem__(KEY, rec))


def clear(state, line_user_id: str) -> None:
    if KEY in state.user(line_user_id):  # 無ければ書かない(新しい会話のたびに、どの入り口の end からも呼ばれる)
        state.update(line_user_id, lambda u: u.pop(KEY, None))


def peek(state, line_user_id: str, entry: str, now: datetime) -> dict | None:
    """その入り口に宛てた、期限内の記録(消さない。呼び直しの間に何度読んでも同じ)。"""
    rec = state.user(line_user_id).get(KEY)
    if not rec or rec.get("to") != entry:
        return None
    try:
        at = datetime.fromisoformat(rec["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return rec if timedelta(0) <= now - at <= TTL else None


def carry(rec: dict | None, entry: str, mapping: dict) -> dict:
    """記録を、先の入り口(entry)の欄の値に写す -> {先の欄: {"text", "number", "unit", "confidence", "from", ...}}。
    mapping は先の入り口の定義の carry。前に引き継いだ記録(prior)が先の入り口のものなら、その値を先に戻し、元の入り口の値と
    移る発言の値で上書きする(新しいものを後に)。同じ先の欄に元の欄がいくつか写るときは、言葉をつなぐ。"""
    if not rec:
        return {}
    out: dict = {}
    prior = rec.get("prior") or {}
    if prior.get("from") == entry:
        for s, e in (prior.get("slots") or {}).items():
            out[s] = dict(e, **{"from": entry})
    table = mapping.get(rec.get("from")) or {}
    if rec.get("from") == TALK:  # 移る先の読む段で読んだ値: 欄の名前のまま(区切りの欄は value も)、推測で
        table = {a.get("slot"): {"slot": a.get("slot"), "how": "talk"} for a in rec.get("acts") or [] if a.get("slot")}
    fresh: dict = {}

    def put(src_slot, e, conf):
        m = table.get(src_slot)
        text = nfkc((e or {}).get("text") or "")
        if not m or not text:
            return
        same = m["how"] == "same"
        conf = conf if same else "low"
        if m["how"] == "talk":
            fresh[m["slot"]] = {"status": "filled", "text": text, "value": e.get("value"), "number": e.get("number"),
                                "range": None, "unit": e.get("unit"), "confidence": "low", "from": TALK}
            return
        old = fresh.get(m["slot"])
        if old is not None:
            fresh[m["slot"]] = dict(old, text=merge(old["text"], text), number=None, unit=None,
                                    confidence="low" if "low" in (old["confidence"], conf) else conf)
            return
        fresh[m["slot"]] = {"status": "filled", "text": text, "value": None,
                            "number": e.get("number") if same else None, "range": e.get("range") if same else None,
                            "unit": e.get("unit") if same else None, "confidence": conf, "from": rec["from"]}
    for s, e in (rec.get("slots") or {}).items():
        if (e or {}).get("status", "filled") == "filled":
            put(s, e, e.get("confidence") or "low")
    for a in rec.get("acts") or []:
        put(a.get("slot"), a, "low")
    return out | fresh


MENTION_MAX = 2  # 移った先の最初の返事で触れる値の数
MENTION_CHARS = 12  # 触れる値の言葉の字数の上限(長い文の値は触れない)


def mentions(rec: dict | None, entry: str, mapping: dict, applied, only, area: str = "", area_slot: str = "area") -> list[str]:
    """移った先の最初の返事の相づちで触れる、引き継いだ値の言葉(§4.5、task 379)-> 言葉の並び(勤務地を先に。MENTION_MAX まで)。
    値は、記録のうち先の入り口が入れた欄(applied。入り口の take_handoff が返す欄)で、条件の欄(only)のものと、勤務地 area だけ。
    area は、先の入り口に今ある勤務地(記録から入れた値か、前の相談で保存した値)か、無ければ希望の項目の勤務地(呼ぶ側が選ぶ)。
    入れなかった値(空でない欄、区切りに読めない文)と、条件でない欄(なぜ今、扶養、時給など。要配慮の言葉や金額を含みうる)は触れない。
    言葉は記録と希望の項目の文のまま(数を作らない、換算しない。§6 の 8)。選ぶ順は、移る発言で言った値、勤務地、ほかの入れた値。"""
    carried = carry(rec, entry, mapping)
    ok = set(applied or ()) & set(only)
    table = (mapping.get((rec or {}).get("from")) or {}) if rec else {}
    said: list[tuple[str, str]] = []
    for a in (rec or {}).get("acts") or []:
        slot = a.get("slot") if rec.get("from") == TALK else (table.get(a.get("slot")) or {}).get("slot")
        if slot in ok and slot != area_slot:
            said.append((slot, a.get("text") or ""))
    others = [(s, e.get("text") or "") for s, e in carried.items() if s in ok and s != area_slot]
    picked: list[tuple[str, str]] = []
    for slot, text in [*said, (area_slot, area or ""), *others]:
        w = nfkc(text).strip()
        if not w or len(w) > MENTION_CHARS or any(w in x or x in w for _, x in picked):
            continue
        picked.append((slot, w))
        if len(picked) == MENTION_MAX:
            break
    return [w for s, w in picked if s == area_slot] + [w for s, w in picked if s != area_slot]


def check_carry(name: str, carry_map, slots, entries) -> None:
    """入り口の定義の carry の形: 元の入り口は移れる入り口(自分でない)、先の欄は自分の欄、how は same か near。"""
    for source, table in carry_map.items() if isinstance(carry_map, dict) else [("(形が dict でない)", None)]:
        if source not in entries or source == name or not isinstance(table, dict) or not table:
            raise ValueError(f"{name}: carry の元の入り口 {source}")
        for src_slot, m in table.items():
            if not isinstance(m, dict) or m.get("slot") not in slots or m.get("how") not in HOWS:
                raise ValueError(f"{name}: carry の {source}.{src_slot} {m}")
