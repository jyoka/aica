"""ヒアリングで、本人のプロフィールが育つのを見せる部品(設計メモ docs/design/hearing-progress-ui.md §3)。
アルバイトの相談と正社員のヒアリングで同じものを使う。相談ごとのこと(何を数えるか、項目の値の出し方)はここに書かない。

ハーネスがターンの結果に足した progress から、そのターンに付ける部品を 0〜1 つ選び(§3.4。choose)、LINE の Flex と、
Flex を使わないときの文(Web のトーク、Flex を切った形。§5)を作る(build)。ここでは数えない(数えるのはハーネス)。

progress の形(アルバイトは dip_mcp.baito.progress.update が作る):
  filled, total   埋まった数と全体の数
  added           このターンに埋まった項目 [{label, value, inferred}](表の上から)
  groups          プロフィールのグループ [{name, items: [{label, value, inferred, undecided}]}]。value が None なら
                  まだ答えていない項目(undecided なら、一度埋まって「未定」になった項目)。NG のように label の無い項目もある
  milestone       プロフィールカードを付けるターンか
  review          まとめ(review)のターンか。帯もカードも付けない
  complete, jobs  最後のターンか。おすすめの仕事 [{name, reason}]

1 ターンに付けるのは 0〜1 つ: 完成 > プロフィールカード > 帯(§3.4。正社員のまとめと職歴カードは、正社員の UI が足す)。
得点、順位、ほめ言葉は出さない。「6/10」は進み具合で、評価ではない。
"""

from __future__ import annotations

import os

# Flex を出すかの設定(1 つ)。flex = 帯・プロフィールカード・完成を Flex で出す(既定)、text = Flex を切って文で出す、
# no_band = 帯だけ切る(プロフィールカードと完成は Flex)。Flex を切った形・帯を切った形と比べるため(設計メモ §6)
MODE_ENV = "AICA_PROGRESS_UI"
MODES = ("flex", "text", "no_band")

BAND, CARD, COMPLETE = "band", "card", "complete"
# 帯の size とバーの色は、実機の LINE で見て利用者が決める(設計メモ §7。docs/images/progress-ui-*.png)
BAND_SIZE = "micro"
BAR_COLOR = "#06c755"
BAR_BACK = "#e3e3e3"
BAND_MAX = 24        # 帯の字(見出し・バー・数字を除く「＋ 表示名: 値」と「ほか N」)
BAND_ITEM_MAX = 20   # 「＋ 表示名: 値」(超えたら値を切って「…」)
CARD_MAX = 80        # プロフィールカードの Flex の中の字
COMPLETE_MAX = 160   # 完成の Flex の中の字(アルバイト。利用者の決定 2026-10-27)
JOB_MAX = 24         # 完成のおすすめの仕事 1 つ(職種名と理由)の字
ALT_MAX = 400        # LINE の altText の上限
HEAD = "あなたのプロフィール"
BAND_HEAD = "プロフィール"
JOBS_HEAD = "おすすめの仕事"
TODO = "まだ"
SEP = " / "
GRAY, DARK = "#888888", "#333333"


def mode() -> str:
    """今の設定(MODE_ENV)。無い・知らない値なら flex。"""
    v = os.environ.get(MODE_ENV, "").strip()
    return v if v in MODES else "flex"


def choose(progress: dict | None) -> str | None:
    """このターンに付ける部品(§3.4)。完成 > プロフィールカード > 帯。review のターンと、何も埋まらなかったターンは None。"""
    if not progress:
        return None
    if progress.get("complete"):
        return COMPLETE
    if progress.get("milestone"):
        return CARD
    if progress.get("review") or not progress.get("added"):
        return None
    return BAND


def build(progress: dict | None, ui_mode: str | None = None) -> dict | None:
    """このターンに返信へ足すもの {kind, flex(LINE の Flex のメッセージ。Flex を切った形では None), text(文の形)}。
    付けないターンは None。"""
    m = ui_mode or mode()
    kind = choose(progress)
    if kind is None or (kind == BAND and m == "no_band"):
        return None
    flex = {BAND: band_flex, CARD: card_flex, COMPLETE: complete_flex}[kind](progress) if m != "text" else None
    text = {BAND: band_text, CARD: card_text, COMPLETE: complete_text}[kind](progress)
    return {"kind": kind, "flex": flex, "text": text}


# ----------------------------- 文 -----------------------------


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(n - 1, 0)] + "…"


def _count(p: dict) -> str:
    return f"{p['filled']}/{p['total']}"


def band_line(p: dict) -> str:
    """帯の 1 行「＋ 表示名: 値」。2 つ以上埋まったら、いちばん上の 1 つに「ほか N」を足す(合わせて BAND_MAX 字まで)。"""
    first, rest = p["added"][0], len(p["added"]) - 1
    tail = f" ほか{rest}" if rest else ""
    head, guess = f"＋ {first['label']}: ", "?" if first.get("inferred") else ""
    room = min(BAND_ITEM_MAX, BAND_MAX - len(tail))
    if room - len(head) - len(guess) < 1:  # 表示名だけで埋まる(ふつうは無い)
        return _clip(head + first["value"] + guess, room) + tail
    return head + _clip(first["value"], room - len(head) - len(guess)) + guess + tail  # 値を切っても「?」は残す


def band_text(p: dict) -> str:
    """帯を文にしたもの。吹き出しの最後の 1 行(「（6/10 ＋働ける時間）」)。"""
    rest = len(p["added"]) - 1
    return f"（{_count(p)} ＋{p['added'][0]['label']}{f' ほか{rest}' if rest else ''}）"


def _value(it: dict) -> str | None:
    if it.get("value") is None:
        return f"{it['label']} 未定" if it.get("undecided") else None
    return it["value"] + ("?" if it.get("inferred") else "")


def profile_rows(p: dict, budget: int) -> list[tuple[str, str]]:
    """プロフィールの行 [(グループの名前, 値)] と、最後に「まだ」の行。名前と値の字を budget までにする:
    収まらなければ、まず「まだ」の項目を後ろから減らして「ほか N」にし、それでも超えるなら長い行の値を切って「…」。"""
    rows = [(g["name"], SEP.join(v for it in g["items"] if (v := _value(it)))) for g in p["groups"]]
    rows = [(n, t) for n, t in rows if t]
    todo = [it["label"] for g in p["groups"] for it in g["items"]
            if it.get("value") is None and not it.get("undecided") and it.get("label")]

    def todo_text(k: int) -> str:
        shown = " ".join("□ " + x for x in todo[:k])
        rest = len(todo) - k
        return shown + ((" " if shown else "") + f"ほか{rest}" if rest else "")

    def cost(rs: list, tt: str) -> int:
        return sum(len(n) + len(t) for n, t in rs) + (len(TODO) + len(tt) if tt else 0)

    def clip_rows(tt: str) -> None:  # いちばん長い行を、2 番目の長さまで切る(1 行だけを消さない)
        while rows and (over := cost(rows, tt) - budget) > 0:
            order = sorted(range(len(rows)), key=lambda j: -len(rows[j][1]))
            n, t = rows[order[0]]
            if len(t) <= 1:
                return
            second = len(rows[order[1]][1]) if len(order) > 1 else 0
            rows[order[0]] = (n, _clip(t, max(len(t) - over, second, 1) if len(t) > second else len(t) - 1))

    k = len(todo)
    while k > 1 and cost(rows, todo_text(k)) > budget:  # 「まだ」は 1 つは残す(「ほか N」だけにしない)
        k -= 1
    clip_rows(todo_text(k))
    if k and cost(rows, todo_text(k)) > budget:
        k = 0
        clip_rows(todo_text(k))
    tt = todo_text(k)
    return rows + ([(TODO, tt)] if tt else [])


def card_text(p: dict) -> str:
    """プロフィールカードを文にしたもの(短い文。吹き出しの後ろに足す)。"""
    rows = profile_rows(p, CARD_MAX - len(HEAD) - len(_count(p)))
    return "\n".join([f"【{HEAD} {_count(p)}】", *(f"{n}：{t}" for n, t in rows)])


def complete_text(p: dict) -> str:
    """完成を文にしたもの。おすすめの仕事は結果の吹き出しにあるので、プロフィールだけ。"""
    return card_text(p)


# ----------------------------- Flex -----------------------------


def _text(s: str, **kw) -> dict:
    return {"type": "text", "text": s, **kw}


def _bar(p: dict) -> dict:
    """進み具合のバー。色を付けた box と灰色の box の幅の比で描く(字は持たない)。"""
    pct = round(100 * p["filled"] / p["total"]) if p["total"] else 0
    fill = [{"type": "box", "layout": "vertical", "contents": [], "width": f"{pct}%", "height": "6px",
             "backgroundColor": BAR_COLOR, "cornerRadius": "3px"}] if pct else []
    return {"type": "box", "layout": "vertical", "contents": fill, "height": "6px", "backgroundColor": BAR_BACK,
            "cornerRadius": "3px", "flex": 1}


def _message(alt: str, bubble: dict) -> dict:
    return {"type": "flex", "altText": _clip(alt, ALT_MAX), "contents": bubble}


def band_flex(p: dict) -> dict:
    """帯(§3.1): 見出し、バー、「埋まった数/全体」、いま埋まった 1 行。"""
    rest = len(p["added"]) - 1
    alt = f"{BAND_HEAD} {_count(p)}: {p['added'][0]['label']}{f'ほか{rest}項目' if rest else ''}を足しました"
    head = {"type": "box", "layout": "horizontal", "spacing": "sm", "alignItems": "center", "contents": [
        _text(BAND_HEAD, size="xxs", color=GRAY, flex=0), _bar(p), _text(_count(p), size="xxs", color=GRAY, flex=0)]}
    body = {"type": "box", "layout": "vertical", "spacing": "xs", "paddingAll": "10px",
            "contents": [head, _text(band_line(p), size="xs", color=DARK, wrap=True)]}
    return _message(alt, {"type": "bubble", "size": BAND_SIZE, "body": body})


def _row(name: str, value: str) -> dict:
    return {"type": "box", "layout": "horizontal", "spacing": "md", "contents": [
        _text(name, size="xs", color=GRAY, flex=2), _text(value, size="xs", color=GRAY if name == TODO else DARK, wrap=True, flex=7)]}


def _profile_box(p: dict, budget: int) -> list[dict]:
    head = {"type": "box", "layout": "horizontal", "contents": [
        _text(HEAD, size="sm", weight="bold", color=DARK, flex=0), _text(_count(p), size="sm", color=GRAY, align="end")]}
    return [head, {"type": "separator", "margin": "sm"},
            *(_row(n, t) for n, t in profile_rows(p, budget - len(HEAD) - len(_count(p))))]


def card_flex(p: dict) -> dict:
    """プロフィールカード(§3.2): 埋まった項目をグループごとに、まだの項目を灰色の「□ 表示名」で。中の字は CARD_MAX まで。"""
    rows = profile_rows(p, CARD_MAX - len(HEAD) - len(_count(p)))
    alt = f"{HEAD} {_count(p)}: " + "、".join(f"{n} {t}" for n, t in rows)
    body = {"type": "box", "layout": "vertical", "spacing": "sm", "contents": _profile_box(p, CARD_MAX)}
    return _message(alt, {"type": "bubble", "size": "kilo", "body": body})


def job_lines(p: dict) -> list[tuple[str, str]]:
    """おすすめの仕事 [(職種名, 理由)]。1 つ JOB_MAX 字まで(理由を切る)。"""
    return [(j["name"], _clip(j["reason"], max(JOB_MAX - len(j["name"]), 1))) for j in p.get("jobs") or []]


def complete_flex(p: dict, budget: int = COMPLETE_MAX) -> dict:
    """完成(§3.3): 1 つの bubble に、プロフィール(§3.2 と同じグループ)とおすすめの仕事を並べる。中の字は budget まで。"""
    jobs = job_lines(p)
    used = (len(JOBS_HEAD) + sum(len(n) + len(r) for n, r in jobs)) if jobs else 0
    contents = _profile_box(p, budget - used)
    if jobs:
        contents += [{"type": "separator", "margin": "md"}, _text(JOBS_HEAD, size="sm", weight="bold", color=DARK, margin="md")]
        contents += [{"type": "box", "layout": "vertical", "contents": [
            _text(n, size="xs", weight="bold", color=DARK, wrap=True), _text(r, size="xxs", color=GRAY, wrap=True)]}
            for n, r in jobs]
    alt = f"{HEAD}({_count(p)})" + (f"と{JOBS_HEAD}: " + "、".join(n for n, _ in jobs) if jobs else "")
    return _message(alt, {"type": "bubble", "size": "mega",
                          "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": contents}})


def flex_texts(node) -> list[str]:
    """Flex の中の字(text の部品)を順に。altText は入れない。"""
    if isinstance(node, dict):
        own = [node["text"]] if node.get("type") == "text" and isinstance(node.get("text"), str) else []
        return own + [t for k, v in node.items() if k != "altText" for t in flex_texts(v)]
    if isinstance(node, list):
        return [t for v in node for t in flex_texts(v)]
    return []


def flex_text(message: dict) -> str:
    """Flex の中の字を文にする(評価の本人役が読むため。設計メモ §5)。"""
    return "\n".join(flex_texts(message))


def flex_chars(message: dict) -> int:
    """Flex の中の字の数(設計メモの字数の上限に照らす)。"""
    return sum(len(t) for t in flex_texts(message))
