"""正社員のヒアリングの形 D(line_runtime/career_ui.py)の画面の写真を作る(task 246)。

正社員のハーネス(dip_mcp/career)を形 D にして、偽のモデルとボタン(文の問いは架空の答え)で最後まで進め、部品ごとに 1 ターンの
返信(吹き出し 1 つと Flex 1 つ、クイックリプライ)を、LINE のトークに近い見た目の HTML に描いて PNG にする。
Flex は LINE の Flex の決まりをまねて描くだけで(scripts/progress_ui_preview.py と同じ大きさの表)、LINE の実機の描き方ではない
(実機では確かめていない)。スタンプの絵は line_runtime/static/career/stamp.png をそのまま使う。

    ./.venv/bin/python -m scripts.career_ui_preview [--out docs/images/career-ui]

実 API は使わない。PNG は Playwright(と Chromium)があるときだけ作る(無ければ HTML だけ)。
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path

from dip_mcp.career import harness as H
from dip_mcp.career import ui as UI
from line_runtime import career_ui as C
from line_runtime import line_channel
from scripts.progress_ui_preview import BUBBLE_W, SPACE_PX, TEXT_PX, page

STAMP = Path(__file__).resolve().parents[1] / "line_runtime" / "static" / "career" / "stamp.png"
PREVIEW_URL = "https://preview.invalid"
# 文で答える問いの架空の答え。ほかの問いはボタンの先頭を押す
TEXT = {"ib_why_now": "評価の仕組みが合わないから", "hf_area": "大阪市内", "card_role1": "物流会社で現場リーダー、6年",
        "card_result1": "誤出荷が半分になった", "card_role2": "倉庫の作業スタッフ、2年", "card_result2": "手順書を作った",
        "fg_direction": "現場の改善"}
TITLES = {UI.START: "始める: 正社員の相談に入った最初の返信", UI.BAND: "帯: 条件の欄が埋まったターン",
          UI.CARD: "プロフィールカード: hard_filter が終わったターン", UI.CAREER_CARD: "職歴カード: 3 つの欄が埋まったターン(完成)",
          UI.REVIEW: "まとめ: 直すところを押せる", UI.COMPLETE: "全部の完成: 先頭は条件のプロフィールカード"}
EXTRA_CSS = """
.btn { border-radius: 8px; text-align: center; font-size: 14px; padding: 7px 8px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.col .bubble { max-width: 290px; box-sizing: border-box; width: fit-content; }
.btn.primary { color: #fff; } .btn.secondary { background: #f0f0f0; color: #111; } .btn.link { color: #3d5a99; }
.carousel { display: flex; gap: 8px; align-items: flex-start; }
"""


class MockLLM:
    """読み取りは、今の問いの欄に本人の文をそのまま入れる。言い方は問いの原文。"""

    def extract(self, payload):
        t = [x for x in payload["targets"] if x in H.ALL_SLOTS][:1]
        ups = [{"slot": t[0], "text": payload["user_text"], "number": None, "unit": None, "choice": None}] if t else []
        return json.dumps({"slot_updates": ups, "ng_updates": [], "segment": None, "intent": "answer", "review_ack": None},
                          ensure_ascii=False)

    def generate(self, v):
        return json.dumps({"reply": v["text"]}, ensure_ascii=False)


def scenes() -> dict[str, dict]:
    """形 D の相談を最後まで進め、部品ごとに 1 ターンの {said, bubbles, choices, built}。職歴カードは完成したもの。"""
    h, out = H.new_state(form="D"), {}
    mem = UI.fresh_mem(h, True)
    first = H.start(h, H.BANK)
    found = {UI.START: {"said": None, "bubbles": first["bubbles"], "choices": first["choices"],
                        "built": C.build(UI.start_payload(h))}}
    r = first
    for _ in range(40):
        if r["done"]:
            break
        cur = h.current
        said = (H.START_GO if cur["mode"] == "start" else H.REVIEW_OK if cur["mode"] == "review"
                else TEXT.get(cur["qid"]) or cur["choices"][0])
        prev = UI.before(h)
        r = H.turn(h, said, H.BANK, MockLLM())
        ui, mem = UI.update(h, mem, prev, r)
        if not ui:
            continue
        k = ui["kind"]
        if k in found and not (k == UI.CAREER_CARD and ui["career_card"]["complete"] and not out.get("done_card")):
            continue
        out["done_card"] = out.get("done_card") or (k == UI.CAREER_CARD and ui["career_card"]["complete"])
        found[k] = {"said": said, "bubbles": r["bubbles"], "choices": r["choices"], "built": C.build(ui)}
    return found


# ----------------------------- Flex を HTML に -----------------------------


def _px(v, table=SPACE_PX) -> str:
    return f"{table[v]}px" if v in table else str(v)


def _node(n: dict, parent: str) -> str:
    style = []
    if n.get("margin"):
        style.append(f"margin-{'left' if parent == 'horizontal' else 'top'}:{_px(n['margin'])}")
    if parent == "horizontal":
        style.append("flex:0 0 auto" if n.get("flex") == 0 else f"flex:{n.get('flex', 1)} 1 0;min-width:0")
    t = n["type"]
    if t == "text":
        style += [f"font-size:{TEXT_PX.get(n.get('size', 'md'), 16)}px", f"color:{n.get('color', '#111')}",
                  f"font-weight:{700 if n.get('weight') == 'bold' else 400}", f"text-align:{n.get('align', 'start')}"]
        style.append("white-space:pre-wrap;word-break:break-word" if n.get("wrap") else
                     "white-space:nowrap;overflow:hidden;text-overflow:ellipsis")
        if n.get("gravity") == "center":
            style.append("align-self:center")
        return f'<div style="{";".join(style)}">{html.escape(n["text"])}</div>'
    if t == "separator":
        return f'<div style="{";".join(style)};border-top:1px solid #e5e5e5"></div>'
    if t == "image":
        src = STAMP.resolve().as_uri() if n["url"].endswith("/static/career/stamp.png") else n["url"]
        size = n.get("size", "md")
        return f'<img src="{src}" style="{";".join(style)};width:{size};height:{size};image-rendering:pixelated">'
    if t == "button":
        kind = n.get("style", "link")
        bg = f"background:{n.get('color', '#06c755')}" if kind == "primary" else ""
        return f'<div class="btn {kind}" style="{";".join(style)};{bg}">{html.escape(n["action"]["label"])}</div>'
    layout = n.get("layout", "vertical")
    gap = SPACE_PX.get(n.get("spacing", "none"), 0)
    style += [f"display:flex;flex-direction:{'row' if layout == 'horizontal' else 'column'};gap:{gap}px"]
    for k, css in (("width", "width"), ("height", "height"), ("backgroundColor", "background"),
                   ("cornerRadius", "border-radius"), ("paddingAll", "padding")):
        if n.get(k):
            style.append(f"{css}:{_px(n[k])}")
    if n.get("alignItems"):
        style.append(f"align-items:{n['alignItems']}")
    if n.get("justifyContent"):
        style.append(f"justify-content:{n['justifyContent']}")
    return f'<div style="{";".join(style)}">{"".join(_node(c, layout) for c in n.get("contents", []))}</div>'


def bubble_html(b: dict) -> str:
    parts = []
    for key in ("body", "footer"):
        if b.get(key):
            box = dict(b[key])
            box.setdefault("paddingAll", "20px" if key == "body" else "10px")
            parts.append(_node(box, "vertical"))
    return f'<div class="flex" style="width:{BUBBLE_W[b.get("size", "mega")]}px">{"".join(parts)}</div>'


def flex_html(message: dict) -> str:
    c = message["contents"]
    if c["type"] == "carousel":
        return f'<div class="carousel">{"".join(bubble_html(b) for b in c["contents"])}</div>'
    return bubble_html(c)


def scene_html(title: str, s: dict) -> str:
    qr = "".join(f"<span>{html.escape(c)}</span>" for c in s["choices"])
    carousel = s["built"]["flex"]["contents"]["type"] == "carousel"
    width = sum(BUBBLE_W[b.get("size", "mega")] + 8 for b in s["built"]["flex"]["contents"]["contents"]) + 70 \
        if carousel else 390
    me = f'<div class="row me"><div class="bubble">{html.escape(s["said"])}</div></div>' if s["said"] else ""
    bubbles = "".join(f'<div class="bubble">{html.escape(b)}</div>' for b in s["bubbles"])
    col = "max-width:none" if carousel else ""
    return (f'<div class="phone" id="shot" style="width:{width}px"><div class="cap">{html.escape(title)}</div>{me}'
            f'<div class="row"><div class="avatar">B</div><div class="col" style="{col}">{bubbles}'
            f'{flex_html(s["built"]["flex"])}</div></div>' + (f'<div class="qr">{qr}</div>' if qr else "") + "</div>")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="docs/images/career-ui", help="HTML と PNG を書くディレクトリ")
    a = ap.parse_args()
    # スタンプの絵と、履歴書の写真から始めるボタン(LIFF)が出る設定で描く(どれも架空の値。通信はしない)
    os.environ[line_channel.PUBLIC_URL_ENV] = PREVIEW_URL
    os.environ[line_channel.LIFF_ID_ENV] = "0000000000-preview"
    os.environ[line_channel.LIFF_CHANNEL_ENV] = "0000000000"
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sc = scenes()
    names = {UI.START: "start", UI.BAND: "band", UI.CARD: "profile-card", UI.CAREER_CARD: "career-card",
             UI.REVIEW: "review", UI.COMPLETE: "complete"}
    pages = {names[k]: page(scene_html(TITLES[k], sc[k])).replace("</style>", EXTRA_CSS + "</style>") for k in names}
    for name, text in pages.items():
        (out / f"{name}.html").write_text(text, encoding="utf-8")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"playwright が無いので HTML だけ書きました: {out}")
        return
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(device_scale_factor=2, viewport={"width": 1400, "height": 900})
        for name in pages:
            pg.goto((out / f"{name}.html").resolve().as_uri())
            pg.locator("#shot").screenshot(path=str(out / f"{name}.png"))
            (out / f"{name}.html").unlink()  # PNG にできたら HTML は残さない
        b.close()
    print(f"書きました: {', '.join(str(out / f'{n}.png') for n in pages)}")


if __name__ == "__main__":
    main()
