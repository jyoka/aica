"""正社員のヒアリングで取れたものの保存先(task 255。設計メモ docs/design/fulltime-hearing.md の §3 の「終わり」と「あとで」)。

- 条件(なぜ今、勤務地、年収の下限、時期、働き方、次にやりたいこと、人と働く形)は、本人の希望の stated_preferences
  (dip_mcp.logic.PREFERENCE_KEYS)に入れる。呼ぶ側(dip_mcp.career.session)が logic.save_preferences で保存する
- 職歴カード(直近の 2 件 × 何をしていたか / どのくらい / どうなったか)、segment(今のあなたに近いの)、segment だけの欄
  (上げたいもの、変えたい理由など)は、本人のプロフィールの career_profile に入れる(ここ)。
  形は {"segment": {"value", "at"}, "items": {ハーネスの欄の key: {"status", "text", "value", "number", "range", "unit", "at"}}}
- 保存するのは確かめた値(high)だけ。パスと「覚えていない」の職歴カードの欄も、空欄として残す(下書きの空欄の仕組みで
  後から埋めるので、次の相談では聞かない。§4)。条件のパスは「こだわりなし」のときだけ希望に入れる
- 職歴カードの欄の数字は、保存の前に本人の発言(summary の sources。dip_mcp.career.draft.sources)と比べる。無い数字
  (「1200社」から抜いた「200社」など)は保存せず、数字の空欄にして保存する(resume_lines.check_numbers。下書きと同じ空欄。
  次の相談では聞かず、下書きの画面で埋める)。空欄に置き換えて VALUE_MAX を超えた欄は、長すぎる値と同じく入れない
  (次の相談でまた聞く)。確かめた欄には checked を付ける(本人がプロフィール画面で直した欄も。次の相談の下書きで、
  確かめた本人の言葉として扱う。dip_mcp.career.draft.trusted)。トークの AI が直した欄(update)には付けない。
  AI が渡す文が本人の言葉そのものかを、コードで確かめられないため。次の相談で、履歴書と checked の欄の言葉と比べ、
  無い数字は数字の空欄にしてから出す(harness.apply_saved)
- 次の相談を始めるときは、保存した segment と欄を high としてハーネスに入れ、聞かない(prefill。from は saved)。
  閉じるときに from=saved の欄は書き直さない(相談の途中にプロフィール画面で直した・消した値を、前の値に戻さないため)
- 逆選択などの NG は、避けたいこと(avoids)に入れる(session が前の値に足す)。避けたいことがあれば、次の相談で逆選択を聞かない

プライバシーポリシー 1.4 の扱い(task 255 で確かめた): 「相談で話した希望や、ご自身のこと(経験など)」(§2)として、
dip の状態の 1 人の行に入る。期間はその行と同じ最後の利用から 180 日(§6。dip_mcp.retention が行ごと外す)。
見る・直す・消すはプロフィール画面(dip_mcp.profile)。LINE は「データを見る」で見て、直すときはトークで言い直す
(トークの AI が、条件は save_user_preferences、職歴カードと segment だけの欄は update_career_card で直す。update)。
書き出すは「データを書き出す」の行の写し、「すべて消す」(LINE は「データを消す」)は行ごと消すので、これも消える(§7)。
"""

from __future__ import annotations

from dip_mcp.career import harness as H
from dip_mcp.data import State, now
from resume_draft.pii import personal_value
from resume_lines import check_numbers

KEY = "career_profile"
# 条件の欄 → 希望の項目(dip_mcp.logic.PREFERENCE_KEYS)
PREFERENCE_SLOTS = {"why_now": "why_now", "area": "area", "min_annual_salary": "min_annual_salary",
                    "start_timing": "start_timing", "work_style": "work_style", "direction": "direction",
                    "team_style": "team_style"}
# career_profile に入れる欄: segment だけの欄と、職歴カードの欄(ハーネスの並び)
SEGMENT_SLOTS = tuple(s for seg in H.SEGMENTS for s in H.SEGMENT_HARD[seg])
CARD_SLOTS = tuple(H.card_key(n, f) for n in range(1, H.CARD_COUNT + 1) for f in H.CARD_FIELDS)
PROFILE_SLOTS = SEGMENT_SLOTS + CARD_SLOTS
VALUE_MAX = H.SAVED_TEXT_MAX  # 1 つの欄の字数の上限(本人の言葉のまま。相談のまとめと同じ長さ)
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
    summary は {"segment", "slots", "pending", "saved_segment", "sources"}(ハーネスの状態の欄、前の相談で保存して入れた
    segment、数字を確かめる元)。segment が決まらないまま来た segment だけの欄(pending)も、high なら入れる。
    職歴カードの欄の数字のうち sources に無いものは、数字の空欄にして入れる(数と区切りも持たない)。前の相談の値は
    書き直さない。保存した key。"""
    at = now(line_user_id).isoformat()
    src = list(summary.get("sources") or [])
    slots = {**{s: e for s, e in (summary.get("pending") or {}).items()}, **(summary.get("slots") or {})}
    items = {}
    for s in PROFILE_SLOTS:
        e = slots.get(s)
        if not _new(e) or e.get("status") not in H.STATUSES:
            continue
        text = str(e.get("text") or "")
        if e["status"] == "filled" and (not text.strip() or _error(text)):
            continue
        item = {"status": e["status"], "text": text.strip(), "value": e.get("value"), "number": e.get("number"),
                "range": e.get("range"), "unit": e.get("unit"), "at": at}
        if s in CARD_SLOTS:
            item["text"], replaced = check_numbers(item["text"], src)
            if replaced:  # 本人の言葉に無い数字: 数と区切りも持たない(下書きの画面で埋める)
                item |= {"value": None, "number": None, "range": None}
                if _error(item["text"]):  # 空欄に置き換えて長くなった(VALUE_MAX を超えた)。長すぎる値と同じく入れない
                    continue
            item["checked"] = True
        items[s] = item
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
    """トークの AI が読む形(要約の career_profile)。本人の言葉のまま。空欄は「パス」「覚えていない」。
    職歴カードは欄のあるものだけを並べ、各カードに番号 n(career<n> の n)を持たせる(職歴1を消して職歴2だけが残っても、
    update_career_card が並びの順ではなく n で直す欄を決めるため)。"""
    rec = user.get(KEY) or {}
    items = rec.get("items") or {}
    seg = (rec.get("segment") or {}).get("value")
    if not items and not seg:
        return None
    cards = []
    for n in range(1, H.CARD_COUNT + 1):
        card = {f: _shown(items[k]) for f in H.CARD_FIELDS if (k := H.card_key(n, f)) in items}
        if card:
            cards.append({"n": n, **card})
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


def edit(u: dict, key: str, value: str, at: str, checked: bool = False) -> str | None:
    """欄を本人の言葉で直す(u は 1 人の dict。上書き)。直せないなら理由。segment は直さない
    (選び直しは次の相談で。消すことはできる)。数と区切りは、本人が直した文からは作らない(文だけを持つ)。
    checked は、本人がプロフィール画面で打った文のとき(dip_mcp.profile.edit_career_item)だけ True にする。
    トークの AI が直すとき(update)は付けない(次の相談で数字を確かめてから出す。harness.apply_saved)。"""
    if key not in PROFILE_SLOTS:
        return "unknown_key"
    if not value:
        return "empty"
    if err := _error(value):
        return err
    item = {"status": "filled", "text": value, "value": None, "number": None, "range": None, "unit": None, "at": at}
    if checked:  # 本人が打った文そのものなので、数字は確かめたことにする
        item["checked"] = True
    u.setdefault(KEY, {}).setdefault("items", {})[key] = item
    return None


TALK_ERRORS = {"unknown_key": "直せるのは item の説明にある欄だけ。",
               "not_saved": "直せるのは、要約の career_profile にもう出ている欄だけ。保存していない欄はトークで作らない"
                            "(職歴を足すのは正社員の相談で聞く)。",
               "empty": "本人が言い直した言葉を入れる。消すときは「データを消す」(すべて消す)を案内する。",
               "personal_info": "電話番号・メールアドレス・住所など個人情報を含む値は保存しない。",
               "too_long": f"値は{VALUE_MAX}字以内。本人の言葉のまま短くして呼び直す。"}


def update(state: State, line_user_id: str, key: str, text: str) -> dict:
    """トークで本人が言い直した欄を直す(dip のツール update_career_card。プライバシーポリシー 1.4 §7 の LINE の「直す」)。
    中身はプロフィール画面で直すのと同じ(edit。本人の言葉のまま上書きし、数と区切りは作らない)。ただし確かめた印
    (checked)は付けない(AI が渡す文の数字が本人の発言にあるかを、コードで確かめられないため)。トークの AI 自身が
    直すので、画面で直したときの知らせ(profile_changes)は残さない。直すのは、もう保存している欄(パスと「覚えていない」の
    空欄も含む)だけ。保存していない欄は作らない(ヒアリングを通らずに職歴を足す道にしないため。not_saved)。"""
    text = str(text or "").strip()
    err = None

    def apply(u):
        nonlocal err
        if key in PROFILE_SLOTS and key not in ((u.get(KEY) or {}).get("items") or {}):
            err = "not_saved"
            return
        err = edit(u, key, text, now(line_user_id).isoformat())

    state.update(line_user_id, apply)
    if err:
        return {"error": err, "note": TALK_ERRORS[err]}
    return {"saved": key, "value": text}


def delete(u: dict, key: str) -> bool | None:
    """欄(か segment)を 1 つ消す(u は 1 人の dict)。消したか。知らない key なら None。何も残らなければ career_profile ごと外す。"""
    if key != "segment" and key not in PROFILE_SLOTS:
        return None
    rec = u.get(KEY) or {}
    found = (rec.pop("segment", None) if key == "segment" else (rec.get("items") or {}).pop(key, None)) is not None
    if KEY in u and not rec.get("segment") and not rec.get("items"):
        u.pop(KEY)
    return found
