"""正社員のヒアリングの形 D(設計メモ docs/design/fulltime-hearing.md §5.2)の部品を、LINE の Flex と文の形にする。task 246。

何を付けるかは dip のハーネスの側(dip_mcp/career/ui.py)が決め、返信の ui に入れてくる。ここは数えずに描くだけ。
帯とプロフィールカードは、アルバイトの相談と同じ部品(line_runtime/progress_ui.py)を使う。返すものは progress_ui.build と
同じ形 {kind, flex, text}(line_channel.to_messages が Flex を 1 つ足し、Web のトーク talk-core.js は text を吹き出しに足す)。
Web のトークには押せる Flex が無いので、まとめの「直す」のボタンは quick_replies(選択肢に足すもの)にする。

部品(kind):
  start        「始める」の 1 枚。フェーズのアイコン(番号の丸)、かかる時間、「始める」「履歴書の写真から始める」「あとで」
  band、card   帯とプロフィールカード(progress_ui)
  career_card  職歴カード 1 枚。見出し、進み具合のバー、3 つの欄(空きは灰色の「まだ」)、3 つが埋まると「完成」のスタンプ
  review       まとめ。行ごとに「直す」(職歴カードの行は「仕事」「規模」「結果」)を押すと、その欄を直す文を送る
  complete     全部の完成のカルーセル。先頭は条件のプロフィールカード(と空欄の数)、続けて職歴カード

Flex の中の字(flex_lines)は、ハーネスが本人の推定時間に数えた字(dip_mcp/career/ui.py の read_chars)と同じになるようにする
(帯とプロフィールカードは見積もり)。評価の本人役は flex_text で読む(fulltime-hearing.md §6.1)。
得点、順位、ほめ言葉は出さない。
"""

from __future__ import annotations

from line_runtime import progress_ui as P
from line_runtime.line_channel import (
    LIFF_ACTIONS,
    liff_features,
    liff_page_url,
    liff_url_of,
    public_url,
)

STAMP_PATH = "/static/career/stamp.png"
STAMP_COLOR = "#ff5fa2"  # アーケードの --neon(スタンプのチェックと同じ色)
LINK = "#3d5a99"
ALT_MAX = 400
WEB_FIX_MAX = 11  # Web のトークで「直す」を足す数(まとめの選択肢 2 つと合わせて 13 個まで)


def build(ui: dict | None) -> dict | None:
    """返信に足すもの {kind, flex, text}。まとめは quick_replies(Web で選択肢に足すもの)も持つ。付けないなら None。"""
    if not ui:
        return None
    k = ui["kind"]
    if k == "band":
        return {"kind": k, "flex": P.band_flex(ui["progress"]), "text": P.band_text(ui["progress"])}
    if k == "card":
        return {"kind": k, "flex": P.card_flex(ui["progress"]), "text": P.card_text(ui["progress"])}
    if k == "start":
        return {"kind": k, "flex": start_flex(ui["start"]), "text": start_text(ui["start"])}
    if k == "career_card":
        c = ui["career_card"]
        return {"kind": k, "flex": _message(f"{c['title']} {c['count']}" + ("(完成)" if c["complete"] else ""),
                                            career_card_bubble(c)), "text": career_card_text(c)}
    if k == "review":
        r = ui["review"]
        fixes = [b["text"] for row in r["rows"] for b in row["fix"]]
        return {"kind": k, "flex": review_flex(r), "text": review_text(r), "quick_replies": fixes[:WEB_FIX_MAX]}
    if k == "complete":
        return {"kind": k, "flex": complete_flex(ui["progress"], ui["complete"]),
                "text": complete_text(ui["progress"], ui["complete"])}
    return None


# ----------------------------- 部品 -----------------------------


def _text(s: str, **kw) -> dict:
    return {"type": "text", "text": s, **kw}


def _message(alt: str, contents: dict) -> dict:
    return {"type": "flex", "altText": P._clip(alt, ALT_MAX), "contents": contents}


def _say(label: str, text: str) -> dict:
    return {"type": "message", "label": label[:20], "text": text}


def _link(label: str, text: str) -> dict:
    """押すと text を本人の発言として送る、小さな字のボタン。"""
    return _text(label, size="xs", color=LINK, weight="bold", flex=0, action=_say(label, text))


def _bar(done: int, of: int) -> dict:
    return P._bar({"filled": done, "total": of})


def _stamp_url() -> str | None:
    """スタンプの絵の URL。LINE は https の画像しか読まないので、公開 URL が https のときだけ。"""
    base = public_url()
    return base + STAMP_PATH if base.startswith("https://") else None


def start_flex(s: dict) -> dict:
    """「始める」の 1 枚: 見出し、フェーズの番号の丸と名前、かかる時間。ボタンは「始める」(発言)、「履歴書の写真から始める」
    (LIFF の履歴書の読み込み。LIFF で開けないときは出さない)、「あとで」(発言)。"""
    go, resume, later = s["buttons"]
    steps = []
    for i, name in enumerate(s["phases"]):
        dot = {"type": "box", "layout": "vertical", "width": "26px", "height": "26px", "cornerRadius": "13px",
               "backgroundColor": P.BAR_COLOR, "justifyContent": "center",
               "contents": [_text(str(i + 1), size="xs", color="#ffffff", weight="bold", align="center")]}
        steps.append({"type": "box", "layout": "vertical", "alignItems": "center", "spacing": "xs",
                      "contents": [dot, _text(name, size="xxs", color=P.DARK, align="center")]})
    body = {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
        _text(s["title"], size="lg", weight="bold", color=P.DARK),
        {"type": "box", "layout": "horizontal", "contents": steps},
        _text(s["time"], size="xs", color=P.GRAY, align="center")]}
    buttons = [{"type": "button", "style": "primary", "height": "sm", "color": P.BAR_COLOR, "action": _say(go, go)}]
    if "resume_upload" in liff_features():
        buttons.append({"type": "button", "style": "secondary", "height": "sm",
                        "action": {"type": "uri", "label": resume[:20], "uri": liff_page_url(LIFF_ACTIONS["resume_upload"][1])}})
    buttons.append({"type": "button", "style": "link", "height": "sm", "action": _say(later, later)})
    footer = {"type": "box", "layout": "vertical", "spacing": "sm", "contents": buttons}
    return _message(f"{s['title']}({s['time']})", {"type": "bubble", "size": "mega", "body": body, "footer": footer})


def start_text(s: dict) -> str:
    return f"【{s['title']}】" + " → ".join(f"{i + 1} {n}" for i, n in enumerate(s["phases"])) + f"({s['time']})"


def career_card_bubble(c: dict) -> dict:
    """職歴カード 1 枚の bubble: 見出しと「n/3」、バー、3 つの欄。空きは灰色の「まだ」、パスと「覚えていない」も灰色。
    3 つが本人の言葉で埋まったら、下に「完成」のスタンプ(絵は公開 URL が https のときだけ。字はいつも)。"""
    head = {"type": "box", "layout": "horizontal", "contents": [
        _text(c["title"], size="sm", weight="bold", color=P.DARK, flex=0),
        _text(c["count"], size="sm", color=P.GRAY, align="end")]}
    rows = [{"type": "box", "layout": "horizontal", "spacing": "md", "contents": [
        _text(r["label"], size="xs", color=P.GRAY, flex=2),
        _text(r["value"] or P.TODO, size="xs", color=P.DARK if r["value"] and not r["muted"] else P.GRAY, wrap=True, flex=7)]}
        for r in c["rows"]]
    contents = [head, _bar(c["done"], c["of"]), *rows]
    if c["complete"]:
        mark = [_text("完成", size="md", weight="bold", color=STAMP_COLOR, flex=0, gravity="center")]
        if url := _stamp_url():
            mark.insert(0, {"type": "image", "url": url, "size": "40px", "aspectMode": "fit", "flex": 0})
        contents.append({"type": "box", "layout": "horizontal", "spacing": "sm", "justifyContent": "flex-end",
                         "alignItems": "center", "contents": mark})
    return {"type": "bubble", "size": "kilo",
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": contents}}


def career_card_text(c: dict) -> str:
    lines = [f"【{c['title']} {c['count']}】", *(f"{r['label']}：{r['value'] or P.TODO}" for r in c["rows"])]
    return "\n".join(lines) + ("\n（完成）" if c["complete"] else "")


def review_flex(r: dict) -> dict:
    """まとめ: 行ごとに、押すとその欄を直す文を送る小さなボタン。職歴カードの行は欄ごと(仕事、規模、結果)。"""
    rows = []
    for row in r["rows"]:
        line = _text(row["text"], size="xs", color=P.DARK, wrap=True)
        links = [_link(b["label"], b["text"]) for b in row["fix"]]
        if len(links) == 1:
            rows.append({"type": "box", "layout": "horizontal", "spacing": "md", "alignItems": "center",
                         "contents": [line | {"flex": 1}, *links]})
        else:
            rows.append({"type": "box", "layout": "vertical", "spacing": "xs", "contents": [line] + ([
                {"type": "box", "layout": "horizontal", "spacing": "lg", "justifyContent": "flex-end", "contents": links}]
                if links else [])})
    body = {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
        _text("まとめ", size="sm", weight="bold", color=P.DARK), {"type": "separator", "margin": "sm"}, *rows]}
    return _message("まとめ: " + "、".join(row["text"] for row in r["rows"]),
                    {"type": "bubble", "size": "mega", "body": body})


def review_text(r: dict) -> str:
    return "\n".join(["【まとめ】", *(row["text"] for row in r["rows"])])


def complete_flex(p: dict, c: dict) -> dict:
    """全部の完成: カルーセルの先頭は条件のプロフィールカード(下に空欄の数と、あれば下書きを開くボタン)、続けて職歴カード。"""
    first = P.card_flex(p)["contents"]
    footer = [_text(f"空欄{c['blanks']}つ。あとから埋められます", size="xs", color=P.GRAY, wrap=True)] if c["blanks"] else []
    draft = liff_url_of(c["draft_url"]) if c.get("draft_url") else None
    if draft:
        footer.append({"type": "button", "style": "primary", "height": "sm", "color": P.BAR_COLOR,
                       "action": {"type": "uri", "label": "下書きを開く", "uri": draft}})
    if footer:
        first = first | {"footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer}}
    bubbles = [first, *(career_card_bubble(x) for x in c["cards"])]
    alt = f"職務経歴書の材料: {P.HEAD} {p['filled']}/{p['total']}、職歴カード {len(c['cards'])} 枚"
    return _message(alt, {"type": "carousel", "contents": bubbles[:12]})


def complete_text(p: dict, c: dict) -> str:
    parts = [P.card_text(p), *(career_card_text(x) for x in c["cards"])]
    return "\n".join(parts)


# ----------------------------- 字(評価の本人役が読む) -----------------------------


def flex_lines(node) -> list[str]:
    """Flex の中の字を順に: text の部品と、ボタンのラベル(altText は入れない)。"""
    if isinstance(node, dict):
        own = []
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            own = [node["text"]]
        elif node.get("type") == "button":
            own = [node["action"]["label"]]
        return own + [t for k, v in node.items() if k not in ("altText", "action") for t in flex_lines(v)]
    if isinstance(node, list):
        return [t for v in node for t in flex_lines(v)]
    return []


def flex_text(message: dict) -> str:
    """Flex の中の字を文にする(本人役が「吹き出しの文 + ボタンのラベル + Flex の中の字」として読む。fulltime-hearing.md §6.1)。"""
    return "\n".join(flex_lines(message))
