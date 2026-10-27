"""正社員のヒアリングで取れたものの保存先(task 255。設計メモ docs/design/fulltime-hearing.md の §3 の「終わり」と「あとで」)。

- 条件(なぜ今、勤務地、年収の下限、時期、働き方、次にやりたいこと、人と働く形)は、本人の希望の stated_preferences
  (dip_mcp.logic.PREFERENCE_KEYS)に入れる。呼ぶ側(dip_mcp.career.session)が logic.save_preferences で保存する
- 職歴カード(直近の 2 件 × 何をしていたか / どのくらい / どうなったか)、segment(今のあなたに近いの)、segment だけの欄
  (上げたいもの、変えたい理由など)は、本人のプロフィールの career_profile に入れる(ここ)。
  形は {"segment": {"value", "at"}, "items": {ハーネスの欄の key: {"status", "text", "value", "number", "range", "unit", "at"}}}
- 保存するのは確かめた値(high)だけ。パスと「覚えていない」の職歴カードの欄も、空欄として残す(下書きの空欄の仕組みで
  後から埋めるので、次の相談では聞かない。§4)。条件のパスは「こだわりなし」のときだけ希望に入れる
- 次の相談を始めるときは、保存した segment と欄を high としてハーネスに入れ、聞かない(prefill。from は saved)。
  閉じるときに from=saved の欄は書き直さない(相談の途中にプロフィール画面で直した・消した値を、前の値に戻さないため)
- 逆選択などの NG は、避けたいこと(avoids)に入れる(session が前の値に足す)。避けたいことがあれば、次の相談で逆選択を聞かない

プライバシーポリシー 1.4 の扱い(task 255 で確かめた): 「相談で話した希望や、ご自身のこと(経験など)」(§2)として、
dip の状態の 1 人の行に入る。期間はその行と同じ最後の利用から 180 日(§6。dip_mcp.retention が行ごと外す)。
見る・直す・消すはプロフィール画面(dip_mcp.profile)、LINE は「データを見る」、書き出すは「データを書き出す」の行の写し、
「すべて消す」は行ごと消すので、これも消える(§7)。
"""

from __future__ import annotations

from dip_mcp.career import harness as H
from dip_mcp.data import State, now
from resume_draft.pii import personal_value

KEY = "career_profile"
# 条件の欄 → 希望の項目(dip_mcp.logic.PREFERENCE_KEYS)
PREFERENCE_SLOTS = {"why_now": "why_now", "area": "area", "min_annual_salary": "min_annual_salary",
                    "start_timing": "start_timing", "work_style": "work_style", "direction": "direction",
                    "team_style": "team_style"}
# career_profile に入れる欄: segment だけの欄と、職歴カードの欄(ハーネスの並び)
SEGMENT_SLOTS = tuple(s for seg in H.SEGMENTS for s in H.SEGMENT_HARD[seg])
CARD_SLOTS = tuple(H.card_key(n, f) for n in range(1, H.CARD_COUNT + 1) for f in H.CARD_FIELDS)
PROFILE_SLOTS = SEGMENT_SLOTS + CARD_SLOTS
VALUE_MAX = 200  # 1 つの欄の字数の上限(本人の言葉のまま。相談のまとめと同じ長さ)
SEGMENT_LABEL = "今のあなたに近いの"
CARD_TITLES = {1: "職歴1(直近)", 2: "職歴2(その前)"}
SHOWN = {"passed": H.PASS, "dont_know": H.DONT_KNOW}  # 空欄として残した欄の見せ方


def _error(text: str) -> str | None:
    """保存しない理由(保存してよければ None)。希望の項目と同じく、個人情報らしい値と長すぎる値は入れない。"""
    if personal_value(text):
        return "personal_info"
    if len(text.strip()) > VALUE_MAX:
        return "too_long"
    return None


def _new(e: dict | None) -> bool:
    """この相談で取れた、確かめた値か(前の相談で保存して入れた値は除く)。"""
    return bool(e) and e.get("confidence") == "high" and e.get("from") != "saved"


def preferences_of(slots: dict) -> dict[str, str]:
    """ハーネスの終わりの状態の欄(harness.State.slots)のうち、希望の項目に入れるもの。high の値だけ。
    パスは「こだわりなし」のときだけ(なぜ今、次にやりたいことのパスは入れない)。値は本人の言葉(ボタンならラベル)。"""
    out = {}
    for slot, key in PREFERENCE_SLOTS.items():
        e = slots.get(slot)
        if not _new(e):
            continue
        if e["status"] == "filled" and e.get("text"):
            out[key] = e["text"]
        elif e["status"] == "passed" and e.get("value") == H.NOPREF:
            out[key] = H.NOPREF
    return out


def save(state: State, line_user_id: str, summary: dict) -> list[str]:
    """segment と、職歴カード・segment だけの欄のうち high のものを career_profile に入れる(欄ごとに上書き)。
    summary は {"segment", "slots", "pending", "saved_segment"}(ハーネスの状態の欄と、前の相談で保存して入れた segment)。
    segment が決まらないまま来た segment だけの欄(pending)も、high なら入れる。前の相談の値は書き直さない。保存した key。"""
    at = now(line_user_id).isoformat()
    slots = {**{s: e for s, e in (summary.get("pending") or {}).items()}, **(summary.get("slots") or {})}
    items = {}
    for s in PROFILE_SLOTS:
        e = slots.get(s)
        if not _new(e) or e.get("status") not in H.STATUSES:
            continue
        text = str(e.get("text") or "")
        if e["status"] == "filled" and (not text.strip() or _error(text)):
            continue
        items[s] = {"status": e["status"], "text": text.strip(), "value": e.get("value"), "number": e.get("number"),
                    "range": e.get("range"), "unit": e.get("unit"), "at": at}
    seg = summary.get("segment")
    seg = seg if seg in H.SEGMENTS and seg != summary.get("saved_segment") else None
    if not items and seg is None:
        return []

    def apply(u):
        rec = u.setdefault(KEY, {})
        if seg is not None:
            rec["segment"] = {"value": seg, "at": at}
        rec.setdefault("items", {}).update(items)

    state.update(line_user_id, apply)
    return (["segment"] if seg else []) + list(items)


def prefill(user: dict, preferences: dict) -> tuple[str | None, dict]:
    """次の相談でハーネスに入れる (segment, 欄)。preferences は stated_preferences(dip_mcp.logic)。
    希望の項目に入っている条件(相談の外で話したものも)と、career_profile の欄を入れる。避けたいことは ng_known で見る。"""
    rec = user.get(KEY) or {}
    items = {s: {k: v for k, v in e.items() if k != "at"} for s, e in (rec.get("items") or {}).items() if s in PROFILE_SLOTS}
    for slot, key in PREFERENCE_SLOTS.items():
        value = (preferences.get(key) or {}).get("value")
        if value:
            items[slot] = ({"status": "passed", "text": H.PASS, "value": H.NOPREF} if value == H.NOPREF
                           else {"status": "filled", "text": str(value)})
    return (rec.get("segment") or {}).get("value"), items


def ng_known(preferences: dict) -> bool:
    """避けたいこと(NG)をもう保存しているか(次の相談で逆選択を聞かない)。"""
    return bool((preferences.get("avoids") or {}).get("value"))


def _shown(e: dict) -> str:
    if e["status"] == "passed" and e.get("value") == H.NOPREF:
        return H.NOPREF
    return SHOWN.get(e["status"]) or e.get("text") or ""


def view(user: dict, md) -> dict | None:
    """プロフィール画面に出す形(md は保存日の書き方。dip_mcp.logic._md)。何も保存していなければ None。
    {"segment": 行 か None, "answers": [行…], "cards": [{"n", "title", "items": [行…(3 つ。無い欄は value=None)]}]}。
    行は {"key", "label", "value", "saved"}。"""
    rec = user.get(KEY) or {}
    items = rec.get("items") or {}
    seg = rec.get("segment")
    if not items and not seg:
        return None

    def row(key, label, e):
        return {"key": key, "label": label, "value": _shown(e) if e else None, "saved": md(e["at"]) if e else None}

    cards = []
    for n in range(1, H.CARD_COUNT + 1):
        keys = [H.card_key(n, f) for f in H.CARD_FIELDS]
        if any(k in items for k in keys):
            cards.append({"n": n, "title": CARD_TITLES.get(n, f"職歴{n}"), "items": [
                row(k, H.CARD_LABELS[f], items.get(k)) for k, f in zip(keys, H.CARD_FIELDS, strict=True)]})
    return {"segment": {"key": "segment", "label": SEGMENT_LABEL, "value": H.SEGMENT_JA[seg["value"]],
                        "saved": md(seg["at"])} if seg else None,
            "answers": [row(s, H.SLOTS[s]["label"], items[s]) for s in SEGMENT_SLOTS if s in items],
            "cards": cards}


def summary(user: dict) -> dict | None:
    """トークの AI が読む形(要約の career_profile)。本人の言葉のまま。空欄は「パス」「覚えていない」。"""
    rec = user.get(KEY) or {}
    items = rec.get("items") or {}
    seg = (rec.get("segment") or {}).get("value")
    if not items and not seg:
        return None
    cards = []
    for n in range(1, H.CARD_COUNT + 1):
        card = {f: _shown(items[k]) for f in H.CARD_FIELDS if (k := H.card_key(n, f)) in items}
        if card:
            cards.append(card)
    return {"segment": H.SEGMENT_JA.get(seg) if seg else None,
            "answers": {H.SLOTS[s]["label"]: _shown(items[s]) for s in SEGMENT_SLOTS if s in items},
            "career_cards": cards}


def label(key: str) -> str | None:
    """欄の呼び名(プロフィールの知らせに使う)。知らない key なら None。"""
    if key == "segment":
        return SEGMENT_LABEL
    if key in SEGMENT_SLOTS:
        return H.SLOTS[key]["label"]
    if key in CARD_SLOTS:
        n, f = key.removeprefix("career").split(".")
        return f"{CARD_TITLES.get(int(n), '職歴' + n)}の{H.CARD_LABELS[f]}"
    return None


def edit(u: dict, key: str, value: str, at: str) -> str | None:
    """本人がプロフィール画面で欄を本人の言葉で直す(u は 1 人の dict。上書き)。直せないなら理由。segment は直さない
    (選び直しは次の相談で。消すことはできる)。数と区切りは、本人が直した文からは作らない(文だけを持つ)。"""
    if key not in PROFILE_SLOTS:
        return "unknown_key"
    if not value:
        return "empty"
    if err := _error(value):
        return err
    u.setdefault(KEY, {}).setdefault("items", {})[key] = {"status": "filled", "text": value, "value": None, "number": None,
                                                          "range": None, "unit": None, "at": at}
    return None


def delete(u: dict, key: str) -> bool | None:
    """欄(か segment)を 1 つ消す(u は 1 人の dict)。消したか。知らない key なら None。何も残らなければ career_profile ごと外す。"""
    if key != "segment" and key not in PROFILE_SLOTS:
        return None
    rec = u.get(KEY) or {}
    found = (rec.pop("segment", None) if key == "segment" else (rec.get("items") or {}).pop(key, None)) is not None
    if KEY in u and not rec.get("segment") and not rec.get("items"):
        u.pop(KEY)
    return found
