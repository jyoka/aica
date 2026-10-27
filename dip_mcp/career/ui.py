"""正社員のヒアリングの形 D(LINE の Flex。設計メモ docs/design/fulltime-hearing.md §5.2、hearing-progress-ui.md)で、
そのターンに何を付けるか。task 246。

形はアルバイト・パートの dip_mcp/baito/progress.py と同じ: ハーネスの本体の turn() の前後の状態から作り、LINE 側
(line_runtime/career_ui.py)は数えずに Flex にするだけ。ターンをまたいで覚えること(mem)は、相談の状態の "ui" に置く。

正社員だけ、付けるもの(kind)もここが決める。ハーネスは本人の推定時間を予算に持ち(§3.3)、形 D では Flex の中の字も
読む時間に数えるため(update が h.seconds に足す)。1 ターンに付けるのは 0〜1 つで、hearing-progress-ui.md §3.4 の順:

  complete(全部の完成のカルーセル)> review(まとめ)> card(プロフィールカード)> career_card(職歴カード)> band(帯)

最初の返信には start(「始める」の 1 枚)を付ける(start_payload)。

- 数えるもの(§4): 全体は今の segment の条件の HARD と MID と、職歴カードの欄(3 つ × 2 枚)。埋まったのは、そのうち一度でも
  値が入ったもの(パス、「覚えていない」、「こだわりなし」も)。segment が変わったときだけ、新しい表で数え直す
- 出し始めは hard_filter の最初のターン(返事が hard_filter 以降の問いのターン)から。「始める」と icebreak の間は出さない
- card(プロフィールカード)は、hard_filter(か、それより前)から次に移った最初のターンと、career_cards から次に移った
  最初のターン(1 回の相談で 2 回まで)。review と result に直接移ったときは付けない(まとめが同じ役を持つ)
- career_card は、職歴カードの欄が埋まったターンだけ(LINE は送ったメッセージを書き換えられないので)
- band は、条件(HARD と MID)か職歴カードの欄が埋まったターン。帯を切る設定(BAND_ENV)なら付けない。review のターンには付けない

字数の決まり(§3.3): 職歴カードの Flex の中は 60 字まで、帯は 24 字まで、プロフィールカードは 80 字まで。
帯とプロフィールカードの字は line_runtime/progress_ui.py が切るので、読む字の数はここで見積もる(read_chars)。
"""

from __future__ import annotations

import os

from dip_mcp.career import harness as H

BAND_ENV = "CAREER_UI_BAND"  # off なら帯を付けない(5/5 の測定で、帯を切った形 D と比べるため)。既定は付ける
START, BAND, CARD, CAREER_CARD, REVIEW, COMPLETE = "start", "band", "card", "career_card", "review", "complete"
CAREER_CARD_MAX = 60  # 職歴カードの Flex の中の字(§3.3)
# 帯とプロフィールカードの字の上限(line_runtime/progress_ui.py の BAND_MAX と CARD_MAX。dip は line_runtime を import しないので写す。
# 同じ値なことは tests/test_career_ui.py が確かめる)
BAND_MAX, BAND_ITEM_MAX, PROFILE_MAX = 24, 20, 80
BAND_HEAD, PROFILE_HEAD, TODO = "プロフィール", "あなたのプロフィール", "まだ"
CARD_TITLES = {1: "職歴1(直近)", 2: "職歴2(その前)"}  # dip_mcp.career.store と同じ見出し
STAMP_TEXT = "完成"  # 3 つの欄が本人の言葉で埋まった職歴カードのスタンプ(絵の横の字)
REVIEW_HEAD = "まとめ"
FIX_LABEL = "直す"
START_TITLE = "職務経歴書を3分で"
START_PHASES = ("条件", "職歴", "まとめ")  # 「始める」の 1 枚のフェーズのアイコン(番号の丸)の下の名前
START_TIME = "目安 3〜5分"  # 目標時間(履歴書あり 180 秒、なし 300 秒。§3.3)
RESUME_START = "履歴書の写真から始める(いちばん早い)"  # LINE 側が LIFF の履歴書の読み込みの画面へのリンクにする
BLANKS_TEXT = "空欄{n}つ。あとから埋められます"
GROUPS = (("条件", H.HARD), ("働き方", H.MID))
CAREER_GROUP, NG_GROUP = "職歴", "NG"
AFTER_HF = ("career_cards", "scenario_game", "reverse_pick", "fill_gaps")
AFTER_CARDS = ("scenario_game", "reverse_pick", "fill_gaps")


def band_on() -> bool:
    return os.environ.get(BAND_ENV, "").strip().lower() not in ("off", "0", "false", "no")


def is_ui(h: H.State) -> bool:
    return h.form in H.UI_FORMS


def fresh_mem(h: H.State, band: bool | None = None) -> dict:
    """相談を始めるときの覚え。band は帯を付けるか(相談の間は変えない。無ければ BAND_ENV)。前の相談で保存した欄と履歴書の欄は、
    始めたときから埋まっていたものとして数える(帯の「いま埋まった」に出さない)。"""
    return {"seen": [s for s in table(h) if h.slots.get(s) is not None], "cards": [],
            "band": band_on() if band is None else band}


# ----------------------------- 数える -----------------------------


def table(h: H.State) -> list[str]:
    """数える項目(分母)。優先度の高い順: HARD、1 枚目の職歴カード、2 枚目、MID(LOW は入れない。同じ優先度は表の順)。"""
    pri = H.active_slots(h)
    return sorted((s for s, p in pri.items() if p >= H.MID), key=lambda s: -pri[s])


def label(slot: str) -> str:
    if slot.startswith("career"):
        n, f = slot[len("career"):].split(".")
        return f"職歴{n} {H.CARD_UI_LABELS[f]}"
    return H.SLOTS[slot]["label"]


def value(slot: str, e: dict) -> str:
    """プロフィールに出す値。本人の言葉(ボタンならラベル)。パス、「覚えていない」、「こだわりなし」は項目名を前に付ける。"""
    if e["status"] == "dont_know":
        return f"{label(slot)} {H.DONT_KNOW}"
    if e["status"] == "passed":
        return f"{label(slot)} {H.NOPREF if e.get('value') == H.NOPREF else H.PASS}"
    return e.get("text") or ""


def _inferred(e: dict) -> bool:
    """推し量った値(場面の二択、ほかの問いへの文の答え、読めなかった答え)。まとめで確かめるので「?」を付ける。"""
    return e.get("confidence") == "low"


def _cards_line(h: H.State) -> dict:
    """プロフィールカードの職歴の 1 行(「2枚、欄4/6」)。中身は職歴カードの Flex が見せる(hearing-progress-ui.md §3.2)。"""
    keys = [H.card_key(n, f) for n in range(1, H.CARD_COUNT + 1) for f in H.CARD_FIELDS]
    done = sum(1 for k in keys if h.slots.get(k) is not None)
    cards = sum(1 for n in range(1, H.CARD_COUNT + 1)
                if (h.slots.get(H.card_key(n, "role")) or {}).get("status") == "filled")  # 何をしていたかが埋まった枚数
    return {"label": "職歴カード", "value": f"{cards}枚、欄{done}/{len(keys)}" if done else None, "inferred": False,
            "undecided": False}


def groups(h: H.State, seen: set) -> list[dict]:
    """プロフィールのグループ(line_runtime/progress_ui.py の形): 条件(HARD)、職歴(1 行)、働き方(MID)、NG。LOW は出さない。"""
    pri = H.active_slots(h)
    tbl = [s for s in table(h) if not s.startswith("career")]

    def item(s):
        e = h.slots.get(s)
        return {"label": label(s), "value": value(s, e) if e else None, "inferred": bool(e) and _inferred(e),
                "undecided": e is None and s in seen}
    out = [{"name": name, "items": [item(s) for s in tbl if pri[s] == p]} for name, p in GROUPS]
    out.insert(1, {"name": CAREER_GROUP, "items": [_cards_line(h)]})
    out.append({"name": NG_GROUP, "items": [{"value": n} for n in h.user_ng]})
    return out


def career_card(h: H.State, n: int) -> dict:
    """職歴カード 1 枚: 見出し、3 つの欄(空きは None。LINE 側が灰色の「まだ」にする)、埋まった欄の数、完成(3 つとも本人の言葉)。
    中の字(見出し、「n/3」、欄の名前と値、完成の字)を CAREER_CARD_MAX までにする(長い値から切る)。"""
    es = [h.slots.get(H.card_key(n, f)) for f in H.CARD_FIELDS]
    rows = []
    for f, e in zip(H.CARD_FIELDS, es, strict=True):
        if e is None:
            rows.append({"label": H.CARD_UI_LABELS[f], "value": None, "muted": False})
        elif e["status"] != "filled":
            rows.append({"label": H.CARD_UI_LABELS[f], "value": H.DONT_KNOW if e["status"] == "dont_know" else H.PASS,
                         "muted": True})
        else:
            rows.append({"label": H.CARD_UI_LABELS[f], "value": (e.get("text") or "") + ("?" if _inferred(e) else ""),
                         "muted": False})
    done = sum(1 for e in es if e is not None)
    complete = all(e is not None and e["status"] == "filled" for e in es)
    card = {"n": n, "title": CARD_TITLES[n], "count": f"{done}/{len(H.CARD_FIELDS)}", "done": done,
            "of": len(H.CARD_FIELDS), "rows": rows, "complete": complete}
    _fit(card)
    return card


def _fit(card: dict) -> None:
    """職歴カードの中の字を CAREER_CARD_MAX までにする: いちばん長い値を 1 字ずつ切って「…」を付ける。"""
    while _chars(career_card_texts(card)) > CAREER_CARD_MAX:
        r = max((r for r in card["rows"] if r["value"]), key=lambda r: H.chars(r["value"]), default=None)
        if r is None or len(r["value"]) <= 2:
            return
        body = r["value"].removesuffix("…")
        r["value"] = body[:-1] + "…"


def progress(h: H.State, seen: set, added: list[dict], milestone: bool, complete: bool) -> dict:
    """line_runtime/progress_ui.py の progress の形。"""
    return {"filled": len(seen), "total": len(table(h)), "added": added, "groups": groups(h, seen),
            "milestone": milestone, "review": h.phase == "review" and not complete, "complete": complete, "jobs": []}


# ----------------------------- 読む字(推定時間に数える) -----------------------------


def career_card_texts(card: dict) -> list[str]:
    """職歴カードの Flex の中の字(line_runtime/career_ui.py の career_card_bubble と同じもの)。"""
    out = [card["title"], card["count"]]
    for r in card["rows"]:
        out += [r["label"], r["value"] or TODO]
    return out + ([STAMP_TEXT] if card["complete"] else [])


def band_line(p: dict) -> str:
    """帯の 1 行の見積もり(line_runtime/progress_ui.band_line と同じ長さ。切り方は違ってよい)。"""
    first, rest = p["added"][0], len(p["added"]) - 1
    tail = f" ほか{rest}" if rest else ""
    head, guess = f"＋ {first['label']}: ", "?" if first.get("inferred") else ""
    room = min(BAND_ITEM_MAX, BAND_MAX - len(tail))
    whole = head + first["value"] + guess
    return (whole if len(whole) <= room else whole[:room]) + tail


def profile_chars(p: dict) -> int:
    """プロフィールカードの字の数の見積もり。切らないときの字で、PROFILE_MAX を上限にする(切るのは line_runtime/progress_ui.py)。"""
    rows = []
    for g in p["groups"]:
        vals = [it["value"] + ("?" if it.get("inferred") else "") for it in g["items"] if it.get("value")]
        if vals:
            rows += [g["name"], " / ".join(vals)]
    todo = [it["label"] for g in p["groups"] for it in g["items"] if it.get("value") is None and it.get("label")]
    texts = [PROFILE_HEAD, f"{p['filled']}/{p['total']}", *rows] + ([TODO, " ".join("□ " + x for x in todo)] if todo else [])
    return min(_chars(texts), PROFILE_MAX)


def _chars(texts) -> int:
    return sum(H.chars(t) for t in texts)


def review_texts(r: dict) -> list[str]:
    """まとめの Flex の中の字(見出し、行、「直す」のボタンのラベル)。"""
    out = [REVIEW_HEAD]
    for row in r["rows"]:
        out += [row["text"], *(b["label"] for b in row["fix"])]
    return out


def start_texts(s: dict) -> list[str]:
    """「始める」の 1 枚の Flex の中の字(見出し、フェーズの番号と名前、かかる時間、ボタン)。"""
    return [s["title"], *(str(i + 1) for i in range(len(s["phases"]))), *s["phases"], s["time"], *s["buttons"]]


def read_chars(ui: dict | None) -> int:
    """この部品の Flex の中の字の数(本人の推定時間に数える。§3.3)。帯とプロフィールカードは見積もり。"""
    if not ui:
        return 0
    k = ui["kind"]
    if k == START:
        return _chars(start_texts(ui["start"]))
    if k == BAND:
        p = ui["progress"]
        return _chars([BAND_HEAD, f"{p['filled']}/{p['total']}", band_line(p)])
    if k == CARD:
        return profile_chars(ui["progress"])
    if k == CAREER_CARD:
        return _chars(career_card_texts(ui["career_card"]))
    if k == REVIEW:
        return _chars(review_texts(ui["review"]))
    c = ui["complete"]
    return profile_chars(ui["progress"]) + _chars([BLANKS_TEXT.format(n=c["blanks"])] if c["blanks"] else []) \
        + _chars(t for card in c["cards"] for t in career_card_texts(card))


def add_read(h: H.State, ui: dict | None) -> None:
    """形 D の Flex の字を、本人の推定時間に足す(吹き出しとボタンはハーネスが数えている)。"""
    h.seconds += read_chars(ui) * 60 / H.READ_CHARS_PER_MIN


# ----------------------------- そのターンに付けるもの -----------------------------


def start_payload(h: H.State) -> dict | None:
    """最初の返信(「始める」の 1 枚)。形 D で、今が「始める」のときだけ。"""
    if not is_ui(h) or (h.current or {}).get("mode") != "start":
        return None
    return {"kind": START, "start": {"title": START_TITLE, "phases": list(START_PHASES), "time": START_TIME,
                                     "buttons": [H.START_GO, RESUME_START, H.LATER]}}


def before(h: H.State) -> dict:
    """turn() の前に覚えておくこと。"""
    return {"phase": h.phase, "mode": (h.current or {}).get("mode"), "result": bool(h.result),
            "slots": {k: (e or {}).get("status") for k, e in h.slots.items()},
            "filled": [s for s in table(h) if h.slots.get(s) is not None]}


def _review(h: H.State) -> dict:
    card = h.last_card or H.build_card(h)
    rows = []
    for keys, text in zip(card.get("slots") or [[] for _ in card["lines"]], card["lines"], strict=True):
        if any(k.startswith("career") for k in keys):
            fix = [{"label": H.CARD_UI_LABELS[k.split(".")[1]], "text": H.fix_text(k)} for k in keys]
        else:
            fix = [{"label": FIX_LABEL, "text": H.fix_text(k)} for k in keys]
        rows.append({"text": text, "fix": fix})
    return {"rows": rows, "focus": card["focus"]}


def _complete(h: H.State) -> dict:
    cards = [career_card(h, n) for n in range(1, H.CARD_COUNT + 1)
             if (h.slots.get(H.card_key(n, "role")) or {}).get("status") == "filled"]
    # 下書きの画面は 4/5 のタスク(職歴カードから職務経歴書の下書きを作る)が足す。それまでは開くボタンを出さない
    return {"cards": cards, "blanks": len(H.blank_cards(h)), "draft_url": None}


def update(h: H.State, mem: dict | None, prev: dict, reply: dict) -> tuple[dict | None, dict]:
    """turn() の後の状態から、このターンに付けるもの(ui。付けないなら None)と、次のターンに持ち越す mem を返す。
    ui の字を読む時間を h.seconds に足す。形 D でなければ何も付けない。reply は turn() の戻り値。"""
    mem = dict(mem) if mem else dict(fresh_mem(h), seen=list(prev.get("filled", [])))
    tbl = table(h)
    old = set(mem["seen"]) & set(tbl)  # segment が変わったら、新しい表に無い項目は数えない
    seen = old | {s for s in tbl if h.slots.get(s) is not None}
    mem["seen"] = [s for s in tbl if s in seen]
    if not is_ui(h) or h.closed:
        return None, mem
    complete = bool(reply.get("done") and h.result and not prev["result"])
    mode = (h.current or {}).get("mode")
    if not complete and (h.phase in ("icebreak", "result") or mode in ("start", "fix", None)):
        return None, mem
    milestone = False
    if prev["phase"] in ("icebreak", "hard_filter") and h.phase in AFTER_HF and "hf" not in mem["cards"]:
        milestone = True
        mem["cards"] = mem["cards"] + ["hf"] + (["cards"] if h.phase in AFTER_CARDS else [])
    elif prev["phase"] == "career_cards" and h.phase in AFTER_CARDS and "cards" not in mem["cards"]:
        milestone = True
        mem["cards"] = mem["cards"] + ["cards"]
    added = [{"label": label(s), "value": value(s, h.slots[s]), "inferred": _inferred(h.slots[s])}
             for s in tbl if s in seen and s not in old]
    p = progress(h, seen, added, milestone, complete)
    changed = [k for k in (H.card_key(n, f) for n in range(1, H.CARD_COUNT + 1) for f in H.CARD_FIELDS)
               if h.slots.get(k) is not None and prev["slots"].get(k) is None]
    ui = None
    if complete:
        ui = {"kind": COMPLETE, "progress": p, "complete": _complete(h)}
    elif mode == "review":
        ui = {"kind": REVIEW, "review": _review(h)}
    elif milestone:
        ui = {"kind": CARD, "progress": p}
    elif changed:
        ui = {"kind": CAREER_CARD, "career_card": career_card(h, int(changed[0][len("career"):].split(".")[0]))}
    elif added and mem.get("band", True) and mode != "fix":
        ui = {"kind": BAND, "progress": p}
    add_read(h, ui)
    return ui, mem
