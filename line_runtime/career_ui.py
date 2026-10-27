"""正社員のヒアリングの形 D(設計メモ docs/design/fulltime-hearing.md §5.2)の部品を、LINE の Flex と文の形にする。task 246。

何を付けるかは dip のハーネスの側(dip_mcp/career/ui.py)が決め、返信の ui に入れてくる。ここは数えずに描くだけ。
返すものは progress_ui.build と同じ形 {kind, flex, text}(line_channel.to_messages が Flex を 1 つ足し、Web のトーク
talk-core.js は text を吹き出しに足す)。Web のトークには押せる Flex が無いので、まとめの「直す」のボタンは quick_replies
(選択肢に足すもの)にする。

帯とプロフィールカードは、正社員だけの形(task 288)。「n/13」の数字は出さず、プロフィールを育つ植物の絵(🌱 → 🌿 → 🌷 → 🌸)と
一言で見せる。プロフィールの行の切り方は、アルバイトの相談と同じ progress_ui.profile_rows を使う。

部品(kind):
  start        「始める」の 1 枚。「まずは1問から」、フェーズの絵と名前、ひとこと、「始める」「履歴書の写真から始める」「あとで」
  band         帯。育ち方の絵、いま足した項目の札(「＋なぜ今」)と値、バー、相談で最初の帯だけ「プロフィールの芽が出ました」
  card         プロフィールカード。絵と見出しと一言(「条件が入りました」)、バー、グループの行(そろったら ✓)、「これから」
  career_card  職歴カード 1 枚。見出しと一言(「あと1つ」)、進み具合のバー、3 つの欄(空きは灰色の「まだ」)、3 つが埋まると「完成」のスタンプ
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
CHIP_BACK = "#e6f9ee"  # 帯の「＋なぜ今」の札の地(P.BAR_COLOR の薄い色)
NEXT_LABEL, DONE_MARK = "これから", "✓"  # dip_mcp/career/ui.py の NEXT_LABEL、DONE_MARK と同じ
ALT_MAX = 400
WEB_FIX_MAX = 11  # Web のトークで「直す」を足す数(まとめの選択肢 2 つと合わせて 13 個まで)


def build(ui: dict | None) -> dict | None:
    """返信に足すもの {kind, flex, text}。まとめは quick_replies(Web で選択肢に足すもの)も持つ。付けないなら None。"""
    if not ui:
        return None
    k = ui["kind"]
    if k == "band":
        return {"kind": k, "flex": band_flex(ui["band"]), "text": band_text(ui["band"])}
    if k == "card":
        p = ui["progress"]
        return {"kind": k, "flex": _message(_profile_alt(p), profile_bubble(p)), "text": profile_text(p)}
    if k == "start":
        return {"kind": k, "flex": start_flex(ui["start"]), "text": start_text(ui["start"])}
    if k == "career_card":
        c = ui["career_card"]
        return {"kind": k, "flex": _message(_card_head(c) + ("(完成)" if c["complete"] else ""),
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
    """「始める」の 1 枚: 見出し(「まずは1問から」)、フェーズの絵の丸と名前(線でつなぐ)、ひとこと。ボタンは
    「始める」(発言)、「履歴書の写真から始める」(LIFF の履歴書の読み込み。LIFF で開けないときは出さない)、「あとで」(発言)。
    問いの数とかかる時間は出さない(task 288)。"""
    go, resume, later = s["buttons"]
    steps = []
    for i, st in enumerate(s["steps"]):
        if i:
            steps.append({"type": "box", "layout": "vertical", "width": "14px", "height": "2px", "backgroundColor": P.BAR_BACK,
                          "contents": [], "flex": 0, "margin": "md"})
        dot = {"type": "box", "layout": "vertical", "width": "40px", "height": "40px", "cornerRadius": "20px",
               "backgroundColor": CHIP_BACK, "justifyContent": "center",
               "contents": [_text(st["icon"], size="lg", align="center")]}
        steps.append({"type": "box", "layout": "vertical", "alignItems": "center", "spacing": "xs",
                      "contents": [dot, _text(st["name"], size="xxs", color=P.DARK, align="center")]})
    body = {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
        _text(s["title"], size="lg", weight="bold", color=P.DARK),
        {"type": "box", "layout": "horizontal", "alignItems": "center", "contents": steps},
        _text(s["note"], size="xs", color=P.GRAY, align="center", wrap=True)]}
    buttons = [{"type": "button", "style": "primary", "height": "sm", "color": P.BAR_COLOR, "action": _say(go, go)}]
    if "resume_upload" in liff_features():
        buttons.append({"type": "button", "style": "secondary", "height": "sm",
                        "action": {"type": "uri", "label": resume[:20], "uri": liff_page_url(LIFF_ACTIONS["resume_upload"][1])}})
    buttons.append({"type": "button", "style": "link", "height": "sm", "action": _say(later, later)})
    footer = {"type": "box", "layout": "vertical", "spacing": "sm", "contents": buttons}
    return _message(f"{s['title']}({s['note']})", {"type": "bubble", "size": "mega", "body": body, "footer": footer})


def start_text(s: dict) -> str:
    return f"【{s['title']}】" + " → ".join(f"{st['icon']}{st['name']}" for st in s["steps"]) + f"\n{s['note']}"


def band_flex(b: dict) -> dict:
    """帯: 左に育ち方の絵、右にいま足した項目の札(「＋なぜ今」)と値(「ほかN」)、下にバー(数字は出さない)。
    相談で最初の帯だけ、一番下に「プロフィールの芽が出ました」を緑で。"""
    chip = {"type": "box", "layout": "horizontal", "flex": 0, "backgroundColor": CHIP_BACK, "cornerRadius": "4px",
            "paddingAll": "2px", "contents": [_text(b["chip"], size="xxs", color=P.BAR_COLOR, weight="bold", flex=0)]}
    value = [_text(b["value"], size="xs", color=P.DARK, weight="bold", wrap=True)]
    if b["more"]:
        value.append(_text(b["more"], size="xxs", color=P.GRAY, flex=0))
    top = {"type": "box", "layout": "horizontal", "spacing": "md", "alignItems": "center", "contents": [
        _text(b["icon"], size="xxl", flex=0),
        {"type": "box", "layout": "vertical", "spacing": "xs", "contents": [
            {"type": "box", "layout": "horizontal", "contents": [chip]},
            {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": value}]}]}
    contents = [top, _bar(b["filled"], b["total"])] + ([_text(b["up"], size="xs", color=P.BAR_COLOR, weight="bold")] if b["up"] else [])
    alt = f"{P.BAND_HEAD}{b['icon']}: {b['chip']} {b['value']}" + (f" {b['more']}" if b["more"] else "") \
        + (f"。{b['up']}" if b["up"] else "")
    return _message(alt, {"type": "bubble", "size": "kilo",
                          "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "12px",
                                   "contents": contents}})


def band_text(b: dict) -> str:
    """帯を文にしたもの(吹き出しの最後の 1 行。「（🌱 ＋なぜ今: 評価が合わない／プロフィールの芽が出ました）」)。"""
    more = f" {b['more']}" if b["more"] else ""
    return f"（{b['icon']} {b['chip']}: {b['value']}{more}" + (f"／{b['up']}" if b["up"] else "") + "）"


def _profile_rows(p: dict) -> list[tuple[str, str]]:
    """プロフィールカードの行 [(グループの名前, 値)]。最後の「まだ」の行は、相談の途中なら「これから」にする(完成のカードは
    「まだ」のまま)。頭の字(dip の profile_head)と合わせて P.CARD_MAX 字までに切る(切り方は progress_ui.profile_rows)。"""
    g = p["growth"]
    todo = P.TODO if p.get("complete") else NEXT_LABEL
    head = len(g["icon"]) + len(P.HEAD) + len(g["head"]) + len(_done(p)) * len(DONE_MARK)
    rows = P.profile_rows(p, P.CARD_MAX - head - (len(todo) - len(P.TODO)))
    return [(todo if n == P.TODO else n, t) for n, t in rows]


def _done(p: dict) -> set[str]:
    return {g["name"] for g in p["groups"] if g.get("done") and any(it.get("value") for it in g["items"])}


def _profile_alt(p: dict) -> str:
    g = p["growth"]
    return f"{P.HEAD}{g['icon']} {g['head']}: " + "、".join(f"{n} {t}" for n, t in _profile_rows(p))


def profile_bubble(p: dict) -> dict:
    """プロフィールカード: 左に育ち方の絵、右に「あなたのプロフィール」と一言(「条件が入りました」)、バー(数字は出さない)、
    グループの行(項目がそろったグループは名前の横に緑の ✓)、まだの項目は灰色の「これから」の行。中の字は P.CARD_MAX まで。"""
    g, done = p["growth"], _done(p)
    head = {"type": "box", "layout": "horizontal", "spacing": "md", "alignItems": "center", "contents": [
        _text(g["icon"], size="3xl", flex=0),
        {"type": "box", "layout": "vertical", "contents": [
            _text(P.HEAD, size="xxs", color=P.GRAY),
            _text(g["head"], size="md", weight="bold", color=P.BAR_COLOR if p.get("complete") or p.get("milestone") else P.DARK,
                  wrap=True)]}]}
    rows = []
    for n, t in _profile_rows(p):
        name = [_text(n, size="xs", color=P.GRAY, flex=0)]
        if n in done:
            name.append(_text(DONE_MARK, size="xs", color=P.BAR_COLOR, weight="bold", flex=0))
        rows.append({"type": "box", "layout": "horizontal", "spacing": "md", "contents": [
            {"type": "box", "layout": "horizontal", "spacing": "xs", "flex": 2, "contents": name},
            _text(t, size="xs", color=P.GRAY if n in (NEXT_LABEL, P.TODO) else P.DARK, wrap=True, flex=7)]})
    return {"type": "bubble", "size": "kilo", "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
        head, _bar(p["filled"], p["total"]) if not p.get("complete") else _bar(1, 1), {"type": "separator", "margin": "sm"},
        *rows]}}


def profile_text(p: dict) -> str:
    """プロフィールカードを文にしたもの(吹き出しの後ろに足す)。"""
    g, done = p["growth"], _done(p)
    rows = [f"{n}{DONE_MARK if n in done else ''}：{t}" for n, t in _profile_rows(p)]
    return "\n".join([f"【{g['icon']} {P.HEAD}: {g['head']}】", *rows])


def _card_head(c: dict) -> str:
    return c["title"] + (f" {c['status']}" if c["status"] else "")


def career_card_bubble(c: dict) -> dict:
    """職歴カード 1 枚の bubble: 見出しと一言(「あと1つ」「空欄あり」)、バー、3 つの欄。空きは灰色の「まだ」、パスと「覚えていない」も灰色。
    3 つが本人の言葉で埋まったら、下に「完成」のスタンプ(絵は公開 URL が https のときだけ。字はいつも)。"""
    head = {"type": "box", "layout": "horizontal", "contents": [
        _text(c["title"], size="sm", weight="bold", color=P.DARK, flex=0)]
        + ([_text(c["status"], size="xs", color=P.BAR_COLOR, weight="bold", align="end")] if c["status"] else [])}
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
    lines = [f"【{_card_head(c)}】", *(f"{r['label']}：{r['value'] or P.TODO}" for r in c["rows"])]
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
    first = profile_bubble(p)
    footer = [_text(f"空欄{c['blanks']}つ。あとから埋められます", size="xs", color=P.GRAY, wrap=True)] if c["blanks"] else []
    draft = liff_url_of(c["draft_url"]) if c.get("draft_url") else None
    if draft:
        footer.append({"type": "button", "style": "primary", "height": "sm", "color": P.BAR_COLOR,
                       "action": {"type": "uri", "label": "下書きを開く", "uri": draft}})
    if footer:
        first = first | {"footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": footer}}
    bubbles = [first, *(career_card_bubble(x) for x in c["cards"])]
    alt = f"職務経歴書の材料: {P.HEAD}{p['growth']['icon']} {p['growth']['head']}、職歴カード {len(c['cards'])} 枚"
    return _message(alt, {"type": "carousel", "contents": bubbles[:12]})


def complete_text(p: dict, c: dict) -> str:
    parts = [profile_text(p), *(career_card_text(x) for x in c["cards"])]
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
