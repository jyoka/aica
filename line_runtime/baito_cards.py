"""アルバイト・パートの相談を 1 タップで始めるカード(設計メモ docs/design/baito-quick-start.md §4・§5・§8・§9。task 265)。

何を出すか(入口の絵 4 枚と下のボタン、合いそうな職種 3 つとタグ、ボタン)は dip のハーネスが決め、ターンの結果の card にする
(dip_mcp.baito.harness の _quick_entry・_quick_result)。ここはそれを LINE の Flex(flex)と、Flex を描かない Web のトークの
文の形(text)にするだけで、職種もタグも選ばない。

- 入口(kind=entry): 問いと一言、絵 4 枚(2 × 2)、下のボタン「どれもピンと来ない」。押すと、その文字を本人の発言として送る
- 職種の結果(kind=result): 絵と見出し(押した文字か「ちょっとだけ」)、職種 3 つとタグ、[近くで探す] [ほかのタイプも見る]
- 暗い色 1 つ(CARD_BG)、字はふつうのゴシック(フォントを指定しない)。絵は Render の static の 64×64 の PNG
  (line_runtime/static/baito/。scripts/baito_icons.py が作る)。LINE の画像は https の URL だけなので、公開 URL が https で
  なければ絵を出さない(文字のカードになる)
- 実機の LINE では見ていない(画面の写真は scripts/baito_quick_preview.py が Flex をまねた HTML を撮ったもの)
"""

from __future__ import annotations

ENTRY, RESULT = "entry", "result"
ICON_PATH = "/static/baito/{name}.png"
CARD_BG = "#1c1430"     # カードの暗い色(設計メモ §8)
TILE_BG = "#130d24"     # 絵のマス
LINE_COLOR = "#3a2f55"  # マスのふち、区切り、タグの地
TEXT = "#f4f0ff"
SUB = "#b9b0d0"
TAG_TEXT = "#e6dcff"
NEAR_COLOR = "#06c755"  # [近くで探す](求人のカードのボタンと同じ色)
ICON_PX = "64px"        # PNG の大きさのまま出す(Flex は拡大の補間を指定できない。§8)
ALT_MAX = 400           # LINE の altText の上限
LABEL_MAX = 20          # ボタンのラベル(message アクションの label)の上限
TEXT_MAX = 300          # message アクションの text の上限


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _say(label: str) -> dict:
    """押すと、その文字を本人の発言として送る。"""
    return {"type": "message", "label": _clip(label, LABEL_MAX), "text": _clip(label, TEXT_MAX)}


def icon_url(name: str, base: str) -> str | None:
    """絵の URL。公開 URL(base)が https でなければ None(LINE の画像は https だけ)。"""
    return base.rstrip("/") + ICON_PATH.format(name=name) if base.startswith("https://") else None


def _icon(name: str, base: str) -> list[dict]:
    url = icon_url(name, base)
    return [{"type": "image", "url": url, "size": ICON_PX, "aspectRatio": "1:1", "aspectMode": "fit"}] if url else []


def _text(s: str, **kw) -> dict:
    return {"type": "text", "text": s, **kw}


def _tile(pick: dict, base: str) -> dict:
    return {"type": "box", "layout": "vertical", "spacing": "sm", "flex": 1, "paddingAll": "10px", "cornerRadius": "12px",
            "backgroundColor": TILE_BG, "borderColor": LINE_COLOR, "borderWidth": "1px", "action": _say(pick["label"]),
            "contents": [*_icon(pick["icon"], base),
                         _text(pick["label"], size="sm", weight="bold", color=TEXT, align="center", wrap=True)]}


def entry_flex(card: dict, base: str) -> dict:
    picks = card["picks"]
    rows = [{"type": "box", "layout": "horizontal", "spacing": "md", "contents": [_tile(p, base) for p in picks[i:i + 2]]}
            for i in range(0, len(picks), 2)]
    other = {"type": "box", "layout": "vertical", "paddingAll": "10px", "cornerRadius": "12px", "borderColor": LINE_COLOR,
             "borderWidth": "1px", "action": _say(card["other"]),
             "contents": [_text(card["other"], size="sm", color=SUB, align="center")]}
    body = {"type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px", "contents": [
        _text(card["title"], size="lg", weight="bold", color=TEXT, align="center", wrap=True),
        _text(card["lead"], size="xs", color=SUB, align="center", wrap=True), *rows, other]}
    alt = f"{card['title']} " + " / ".join([*(p["label"] for p in picks), card["other"]])
    return _message(alt, {"type": "bubble", "size": "mega", "body": body, "styles": {"body": {"backgroundColor": CARD_BG}}})


def _job_row(job: dict) -> dict:
    tag = [{"type": "box", "layout": "vertical", "flex": 0, "paddingStart": "8px", "paddingEnd": "8px", "paddingTop": "2px",
            "paddingBottom": "2px", "cornerRadius": "10px", "backgroundColor": LINE_COLOR,
            "contents": [_text(job["tag"], size="xxs", color=TAG_TEXT)]}] if job.get("tag") else []
    return {"type": "box", "layout": "horizontal", "spacing": "md", "alignItems": "center", "contents": [
        _text(job["name"], size="sm", weight="bold", color=TEXT, wrap=True, flex=1), *tag]}


def result_flex(card: dict, base: str) -> dict:
    head = {"type": "box", "layout": "horizontal", "spacing": "md", "alignItems": "center", "contents": [
        *(dict(i, flex=0) for i in _icon(card["icon"], base)),
        {"type": "box", "layout": "vertical", "flex": 1, "contents": [
            _text(card["lead"], size="xxs", color=SUB, wrap=True),
            _text(card["title"], size="lg", weight="bold", color=TEXT, wrap=True)]}]}
    jobs = [_job_row(j) for j in card["jobs"]]
    body = {"type": "box", "layout": "vertical", "spacing": "md", "paddingAll": "16px",
            "contents": [head, {"type": "separator", "color": LINE_COLOR}, *jobs]}
    near, *rest = card["buttons"]
    footer = {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px", "contents": [
        {"type": "button", "style": "primary", "height": "sm", "color": NEAR_COLOR, "action": _say(near)},
        *({"type": "button", "style": "link", "height": "sm", "color": SUB, "action": _say(b)} for b in rest)]}
    alt = f"{card['title']}: " + "、".join(j["name"] for j in card["jobs"])
    return _message(alt, {"type": "bubble", "size": "mega", "body": body, "footer": footer,
                          "styles": {"body": {"backgroundColor": CARD_BG}, "footer": {"backgroundColor": CARD_BG}}})


def _message(alt: str, bubble: dict) -> dict:
    return {"type": "flex", "altText": _clip(alt, ALT_MAX), "contents": bubble}


def flex(card: dict, base: str) -> dict:
    """カードを LINE の Flex のメッセージにする。base は公開 URL(絵の URL に使う。https でなければ絵を出さない)。"""
    return (entry_flex if card["kind"] == ENTRY else result_flex)(card, base)


def text(card: dict) -> str:
    """Flex を描かない Web のトークの文の形(吹き出し 1 つ)。ボタンはクイックリプライ(返信の quick_replies)が出す。"""
    if card["kind"] == ENTRY:
        return f"{card['title']}\n{card['lead']}"
    lines = [f"【{card['title']}】{card['lead']}"]
    lines += [f"・{j['name']}" + (f"：{j['tag']}" if j.get("tag") else "") for j in card["jobs"]]
    return "\n".join(lines)


def view(card: dict | None) -> dict | None:
    """返信(runtime.render)に載せる形: ハーネスのカードに、文の形(text)を足したもの。"""
    return None if not card else {**card, "text": text(card)}
