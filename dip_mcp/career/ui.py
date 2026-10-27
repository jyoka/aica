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
プロフィールカードの字は line_runtime/career_ui.py が(progress_ui.profile_rows で)切るので、読む字の数はここで見積もる(read_chars)。

見せ方(task 288): 「n/13」の数字や残りの問いの数は出さない。プロフィールを育つ植物にたとえ(GROWTH)、聞き終えたフェーズで
芽(条件)→ 若葉(職歴)→ つぼみ(その後)→ 花(相談を終えたとき)と育つ(「始める」の 1 枚の 🌱 条件、🌿 職歴、🌸 完成と同じ絵)。
帯は、いま足した項目と、相談で最初に出す帯だけ「プロフィールの芽が出ました」を出す(「プロフィール」の見出しも、その一言の
中だけ。帯の字を前より減らし、聞ける問いの数を減らさないため)。段が上がるのはフェーズを聞き終えたターンで、そのターンは
プロフィールカードが、聞き終えたフェーズの一言(「条件が入りました」)を新しい絵と一緒に見出しに出す。
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
NEXT_LABEL = "これから"  # プロフィールカードで、まだの項目の行の名前(TODO の代わり。相談を終えた完成のカードは TODO のまま)
DONE_MARK = "✓"  # プロフィールカードで、項目がそろったグループの名前の横
# 育ち方(task 288): 聞き終えたフェーズの数(0: 条件の途中、1: 条件を聞き終えた、2: 職歴も)で 3 段。相談を終えたら花(BLOOM)。
# (絵、プロフィールカードの見出しの一言)
GROWTH = (("🌱", "芽が出ました"), ("🌿", "育っています"), ("🌷", "もうすぐ完成です"))
BLOOM = ("🌸", "できあがりました")
SPROUT = "プロフィールの芽が出ました"  # 帯: 相談で最初に出す帯だけ(履歴書や前の相談の値が先に入っていても)
# プロフィールカードの見出しの一言。職歴は、本人の言葉で入った欄があるときだけ(どれもパスなら GROWTH の一言)
MILESTONE_WORDS = {"hf": "条件が入りました", "cards": "職歴が入りました"}
CARD_WRITING = "書きはじめ"  # プロフィールカードの職歴の行: 本人の言葉で入った欄はあるが、何をしていたかがまだのとき
FILLED_MARK = "✓"  # 帯: 値の字が無いときの代わり(Flex の text は空にできない)
CARD_TITLES = {1: "職歴1(直近)", 2: "職歴2(その前)"}  # dip_mcp.career.store と同じ見出し
STAMP_TEXT = "完成"  # 3 つの欄が本人の言葉で埋まった職歴カードのスタンプ(絵の横の字)
REVIEW_HEAD = "まとめ"
FIX_LABEL = "直す"
# 「始める」の 1 枚(task 288): 問いの数は出さず、まず 1 問から。フェーズは育ち方の絵と名前で見せる
START_TITLE = "まずは1問から"
START_STEPS = (("🌱", "条件"), ("🌿", "職歴"), ("🌸", "完成"))
START_NOTE = "パスしてもOK。途中でやめても残ります"  # 「あとで」で閉じても、そこまでの分を保存する(§3)
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
    """プロフィールカードの職歴の 1 行: 職歴カードの何をしていたか(本人の言葉。2 枚なら「 / 」でつなぐ)。何をしていたかが
    まだで、ほかの欄が本人の言葉で入ったら「書きはじめ」、入った欄がどれもパスか「覚えていない」なら「パス」。数(「欄4/6」)は
    出さない(task 288)。中身は職歴カードの Flex が見せる。"""
    roles = [e for n in range(1, H.CARD_COUNT + 1) if (e := h.slots.get(H.card_key(n, "role")) or {}).get("status") == "filled"]
    text = " / ".join((e.get("text") or "") + ("?" if _inferred(e) else "") for e in roles)
    if not text and _cards_filled(h):
        text = CARD_WRITING
    elif not text and any(h.slots.get(k) is not None for k in _card_keys()):
        text = H.PASS
    return {"label": "職歴カード", "value": text or None, "inferred": False, "undecided": False}


def _card_keys() -> list[str]:
    return [H.card_key(n, f) for n in range(1, H.CARD_COUNT + 1) for f in H.CARD_FIELDS]


def _cards_filled(h: H.State) -> bool:
    """職歴カードに、本人の言葉で入った欄があるか(パスと「覚えていない」は数えない)。"""
    return any((h.slots.get(k) or {}).get("status") == "filled" for k in _card_keys())


def groups(h: H.State, seen: set) -> list[dict]:
    """プロフィールのグループ(line_runtime/progress_ui.py の形): 条件(HARD)、職歴(1 行)、働き方(MID)、NG。LOW は出さない。
    done は、そのグループの項目がどれも一度は埋まったか(職歴は 2 枚の欄の全部。NG は無し)。プロフィールカードが ✓ を付ける。"""
    pri = H.active_slots(h)
    tbl = [s for s in table(h) if not s.startswith("career")]

    def item(s):
        e = h.slots.get(s)
        return {"label": label(s), "value": value(s, e) if e else None, "inferred": bool(e) and _inferred(e),
                "undecided": e is None and s in seen}
    out = [{"name": name, "items": (its := [item(s) for s in tbl if pri[s] == p]),
            "done": bool(its) and all(it["value"] is not None for it in its)} for name, p in GROUPS]
    out.insert(1, {"name": CAREER_GROUP, "items": [_cards_line(h)],
                   "done": all(h.slots.get(k) is not None for k in _card_keys())})
    out.append({"name": NG_GROUP, "items": [{"value": n} for n in h.user_ng], "done": False})
    return out


def card_status(done: int, of: int, complete: bool, final: bool = False) -> str | None:
    """職歴カードの見出しの横の一言(task 288。「2/3」の数字だけにしない): 残りが 1 つなら「あと1つ」、全部埋まって空欄が
    あれば「空欄あり」。ほかは None(進み具合はバーが見せる。完成したカードは「完成」のスタンプが見せる)。final は相談を終えた
    完成のカルーセル: もう聞かないので、完成でなければ「空欄あり」(下書きで、あとから埋める)。"""
    if final or done == of:
        return None if complete else "空欄あり"
    return "あと1つ" if done == of - 1 else None


def career_card(h: H.State, n: int, final: bool = False) -> dict:
    """職歴カード 1 枚: 見出し、3 つの欄(空きは None。LINE 側が灰色の「まだ」にする)、埋まった欄の数(バー)と見出しの横の一言
    (card_status)、完成(3 つとも本人の言葉)。中の字(見出し、一言、欄の名前と値、完成の字)を CAREER_CARD_MAX までにする
    (長い値から切る)。"""
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
    card = {"n": n, "title": CARD_TITLES[n], "status": card_status(done, len(H.CARD_FIELDS), complete, final), "done": done,
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


def growth(stage: int, first: bool, complete: bool, reached: str | None = None) -> dict:
    """育ち方の見せ方: 絵(icon)、今の段の一言(word)、相談で最初の帯なら帯の一言(up。first)、プロフィールカードの見出しの
    一言(head。聞き終えたフェーズの一言か、今の段の一言)。stage は聞き終えたフェーズの数(GROWTH の添字)。相談を終えたら花。"""
    if complete:
        icon, word = BLOOM
        return {"icon": icon, "word": word, "up": None, "head": word}
    icon, word = GROWTH[min(stage, len(GROWTH) - 1)]
    return {"icon": icon, "word": word, "up": SPROUT if first else None,
            "head": MILESTONE_WORDS.get(reached) or word}


def progress(h: H.State, seen: set, added: list[dict], milestone: bool, complete: bool, first: bool = False,
             stage: int = 0, reached: str | None = None) -> dict:
    """line_runtime/progress_ui.py の progress の形に、育ち方(growth)を足したもの。first はまだ帯を出していないか、
    stage は聞き終えたフェーズの数、reached はこのターンに聞き終えたフェーズ(MILESTONE_WORDS の鍵)。"""
    return {"filled": len(seen), "total": len(table(h)), "added": added, "groups": groups(h, seen),
            "milestone": milestone, "review": h.phase == "review" and not complete, "complete": complete, "jobs": [],
            "growth": growth(stage, first, complete, reached)}


def band(p: dict) -> dict:
    """帯の中身(task 288): 育ち方の絵、いま足した項目の札(「＋なぜ今」)と値、2 つ以上なら「ほかN」、段が上がったらその一言。
    札と値と「ほかN」は BAND_MAX 字まで(値を切って「…」。推し量った値の「?」は残す)。パスなどは値だけ(「パス」)。"""
    first, rest = p["added"][0], len(p["added"]) - 1
    more = f"ほか{rest}" if rest else None
    chip, guess = f"＋{first['label']}", "?" if first.get("inferred") else ""
    v = first["value"].removeprefix(first["label"] + " ") or FILLED_MARK
    room = min(BAND_ITEM_MAX, BAND_MAX - (len(more) if more else 0)) - len(chip) - len(guess)
    if len(v) > max(room, 1):
        v = v[:max(room - 1, 1)] + "…"
    g = p["growth"]
    return {"icon": g["icon"], "chip": chip, "value": v + guess, "more": more, "up": g["up"],
            "filled": p["filled"], "total": p["total"]}


# ----------------------------- 読む字(推定時間に数える) -----------------------------


def career_card_texts(card: dict) -> list[str]:
    """職歴カードの Flex の中の字(line_runtime/career_ui.py の career_card_bubble と同じもの)。"""
    out = [card["title"], *([card["status"]] if card["status"] else [])]
    for r in card["rows"]:
        out += [r["label"], r["value"] or TODO]
    return out + ([STAMP_TEXT] if card["complete"] else [])


def band_texts(b: dict) -> list[str]:
    """帯の Flex の中の字(line_runtime/career_ui.py の band_flex と同じもの)。"""
    return [b["icon"], b["chip"], b["value"], *([b["more"]] if b["more"] else []), *([b["up"]] if b["up"] else [])]


def done_groups(p: dict) -> list[str]:
    """項目がそろったグループの名前(プロフィールカードで、行があれば名前の横に ✓ を付ける)。"""
    return [g["name"] for g in p["groups"] if g.get("done") and any(it.get("value") for it in g["items"])]


def profile_head(p: dict) -> list[str]:
    """プロフィールカードの頭の字: 絵、見出し、一言、そろったグループの ✓。"""
    g = p["growth"]
    return [g["icon"], PROFILE_HEAD, g["head"], *(DONE_MARK for _ in done_groups(p))]


def profile_chars(p: dict) -> int:
    """プロフィールカードの字の数の見積もり。切らないときの字で、PROFILE_MAX を上限にする(切るのは line_runtime/career_ui.py)。"""
    rows = []
    for g in p["groups"]:
        vals = [it["value"] + ("?" if it.get("inferred") else "") for it in g["items"] if it.get("value")]
        if vals:
            rows += [g["name"], " / ".join(vals)]
    todo = [it["label"] for g in p["groups"] for it in g["items"] if it.get("value") is None and it.get("label")]
    todo_label = TODO if p.get("complete") else NEXT_LABEL
    texts = [*profile_head(p), *rows] + ([todo_label, " ".join("□ " + x for x in todo)] if todo else [])
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
    """「始める」の 1 枚の Flex の中の字(見出し、フェーズの絵と名前、ひとこと、ボタン)。"""
    return [s["title"], *(x for st in s["steps"] for x in (st["icon"], st["name"])), s["note"], *s["buttons"]]


def read_chars(ui: dict | None) -> int:
    """この部品の Flex の中の字の数(本人の推定時間に数える。§3.3)。帯とプロフィールカードは見積もり。"""
    if not ui:
        return 0
    k = ui["kind"]
    if k == START:
        return _chars(start_texts(ui["start"]))
    if k == BAND:
        return _chars(band_texts(ui["band"]))
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
    return {"kind": START, "start": {"title": START_TITLE, "steps": [{"icon": i, "name": n} for i, n in START_STEPS],
                                     "note": START_NOTE,
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
    cards = [career_card(h, n, final=True) for n in range(1, H.CARD_COUNT + 1)
             if (h.slots.get(H.card_key(n, "role")) or {}).get("status") == "filled"]
    # 空欄の数は結果の「空欄はNつ」と同じ(harness._finish が職歴カードの下書きの空欄を数えた数。空欄に戻した数字も入る)。
    # draft_url は、session.turn が下書きを作った後に入れる(下書きの画面の URL。開けない窓口では None のままで、ボタンを出さない)
    return {"cards": cards, "blanks": h.result["blanks"], "draft_url": None}


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
    milestone, reached = False, None
    if prev["phase"] in ("icebreak", "hard_filter") and h.phase in AFTER_HF and "hf" not in mem["cards"]:
        milestone, reached = True, "hf"
        mem["cards"] = mem["cards"] + ["hf"] + (["cards"] if h.phase in AFTER_CARDS else [])
    elif prev["phase"] == "career_cards" and h.phase in AFTER_CARDS and "cards" not in mem["cards"]:
        milestone, reached = True, "cards" if _cards_filled(h) else None
        mem["cards"] = mem["cards"] + ["cards"]
    added = [{"label": label(s), "value": value(s, h.slots[s]), "inferred": _inferred(h.slots[s])}
             for s in tbl if s in seen and s not in old]
    p = progress(h, seen, added, milestone, complete, first=not mem.get("sprouted"), stage=len(set(mem["cards"])),
                 reached=reached)
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
        ui = {"kind": BAND, "progress": p, "band": band(p)}
        mem["sprouted"] = True
    add_read(h, ui)
    return ui, mem
