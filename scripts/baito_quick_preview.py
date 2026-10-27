"""アルバイトの相談を 1 タップで始めるカード(line_runtime/baito_cards.py)の画面の写真を作る(task 265)。

入口のカードから偽のモデルとボタンで進め、入口・職種の結果(ひとりで黙々)・「ちょっとだけ」(どれもピンと来ない)の 3 つの
ターンの返信(吹き出しと Flex)を、LINE のトークに近い見た目の HTML に描いて PNG にする。Flex は LINE の Flex の決まり
(bubble の size、box の layout・padding・border、image の size など)をまねて描くだけで、LINE の実機の描き方ではない
(実機では確かめていない)。絵は line_runtime/static/baito/ の PNG を、そのままの大きさで出す。

    ./.venv/bin/python -m scripts.baito_quick_preview [--out docs/images/baito-quick-start]

実 API は使わない。PNG は Playwright(と Chromium)があるときだけ作る(無ければ HTML だけ)。
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from dip_mcp.baito import harness as H
from line_runtime import baito_cards as C
from line_runtime import config
from scripts.progress_ui_preview import BUBBLE_W, SPACE_PX, TEXT_PX

ICONS = config.ROOT / "line_runtime" / "static" / "baito"
BASE = "https://aica.example"  # 絵の URL を作るための仮の公開 URL(HTML では手元の PNG に置き換える)
RADIUS = {"none": "0", "xs": "2px", "sm": "4px", "md": "8px", "lg": "12px", "xl": "16px", "xxl": "20px"}


class MockLLM:
    """偽のモデル。リアクション(実際はモデルが作る)を質問の原文の前に付ける。"""

    def __init__(self, reaction: str = ""):
        self.reaction = reaction

    def extract(self, payload):
        raise AssertionError("入口ではモデルに読ませない")

    def generate(self, v):
        return json.dumps({"reply": self.reaction + v["text"]}, ensure_ascii=False)


def scenes() -> dict[str, dict]:
    """入口・職種の結果・「ちょっとだけ」の {said, reply, card}。"""
    st = H.new_state(quick=True)
    v = H.start(st, H.BANK)
    out = {"entry": {"said": "バイト探したい", "reply": "合いそうなバイトを、一緒に探そう！", "card": v["quick"]}}
    for name, pick, reaction in (("result", "ひとりで黙々", "黙々派ね。"), ("light", H.QUICK_NONE, "")):
        st = H.new_state(quick=True)
        r = H.turn(st, pick, H.BANK, MockLLM(reaction))
        out[name] = {"said": pick, "reply": r["reply"], "card": r["view"]["quick"]}
    return out


# ----------------------------- Flex を HTML に -----------------------------


def _px(v, table=SPACE_PX) -> str:
    return f"{table[v]}px" if v in table else str(v)


def _node(n: dict, parent: str) -> str:
    style = []
    if n.get("margin"):
        style.append(f"margin-{'left' if parent == 'horizontal' else 'top'}:{_px(n['margin'])}")
    if parent == "horizontal":
        style.append("flex:0 0 auto" if n.get("flex") == 0 else f"flex:{n.get('flex', 1)} 1 0;min-width:0")
    if n["type"] == "text":
        style += [f"font-size:{TEXT_PX.get(n.get('size', 'md'), 16)}px", f"color:{n.get('color', '#111')}",
                  f"font-weight:{700 if n.get('weight') == 'bold' else 400}", f"text-align:{n.get('align', 'start')}"]
        style.append("white-space:pre-wrap;word-break:break-word" if n.get("wrap") else "white-space:nowrap")
        return f'<div style="{";".join(style)}">{html.escape(n["text"])}</div>'
    if n["type"] == "separator":
        return f'<div style="{";".join(style)};border-top:1px solid {n.get("color", "#e5e5e5")}"></div>'
    if n["type"] == "image":
        src = (ICONS / n["url"].rsplit("/", 1)[1]).resolve().as_uri()
        return (f'<div style="{";".join(style)};display:flex;justify-content:center"><img src="{src}" '
                f'style="width:{n["size"]};height:{n["size"]};image-rendering:pixelated"></div>')
    if n["type"] == "button":
        primary = n.get("style") == "primary"
        style += ["border-radius:8px;padding:8px;text-align:center;font-size:14px",
                  f"background:{n.get('color')};color:#fff" if primary else f"color:{n.get('color', '#42659a')}"]
        return f'<div style="{";".join(style)}">{html.escape(n["action"]["label"])}</div>'
    layout = n.get("layout", "vertical")
    gap = SPACE_PX.get(n.get("spacing", "none"), 0)
    style += [f"display:flex;flex-direction:{'row' if layout == 'horizontal' else 'column'};gap:{gap}px"]
    for k, css in (("width", "width"), ("height", "height"), ("backgroundColor", "background"),
                   ("paddingAll", "padding"), ("paddingTop", "padding-top"), ("paddingBottom", "padding-bottom"),
                   ("paddingStart", "padding-left"), ("paddingEnd", "padding-right")):
        if n.get(k):
            style.append(f"{css}:{_px(n[k])}")
    if n.get("cornerRadius"):
        style.append(f"border-radius:{RADIUS.get(n['cornerRadius'], n['cornerRadius'])}")
    if n.get("borderWidth"):
        style.append(f"border:{n['borderWidth']} solid {n.get('borderColor', '#000')}")
    if n.get("alignItems"):
        style.append(f"align-items:{n['alignItems']}")
    return f'<div style="{";".join(style)}">{"".join(_node(c, layout) for c in n.get("contents", []))}</div>'


def flex_html(message: dict) -> str:
    b = message["contents"]
    styles = b.get("styles", {})
    parts = []
    for part in ("body", "footer"):
        if part in b:
            bg = styles.get(part, {}).get("backgroundColor", "#fff")
            parts.append(f'<div style="background:{bg}">{_node(b[part], "vertical")}</div>')
    return f'<div class="flex" style="width:{BUBBLE_W[b.get("size", "mega")]}px">{"".join(parts)}</div>'


CSS = """
body { margin: 0; background: #e9ecf2; font-family: -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; }
.phone { width: 390px; background: #8aa6cf; padding: 14px 10px 16px; box-sizing: border-box; }
.cap { color: #fff; font-size: 12px; text-align: center; margin-bottom: 10px; opacity: .9; }
.row { display: flex; gap: 8px; align-items: flex-start; margin: 8px 0; } .row.me { justify-content: flex-end; }
.avatar { width: 34px; height: 34px; border-radius: 50%; background: #fff; color: #d0021b; font-weight: 800; font-size: 13px;
  display: flex; align-items: center; justify-content: center; flex: none; }
.col { display: flex; flex-direction: column; gap: 6px; max-width: 320px; }
.bubble { background: #fff; padding: 8px 12px; border-radius: 16px; font-size: 14.5px; line-height: 1.55; white-space: pre-wrap;
  align-self: flex-start; }
.me .bubble { background: #85e249; }
.flex { border-radius: 16px; overflow: hidden; box-sizing: border-box; }
"""


def scene_html(title: str, s: dict) -> str:
    return (f'<div class="phone" id="shot"><div class="cap">{html.escape(title)}</div>'
            f'<div class="row me"><div class="bubble">{html.escape(s["said"])}</div></div>'
            f'<div class="row"><div class="avatar">B</div><div class="col"><div class="bubble">{html.escape(s["reply"])}</div>'
            f'{flex_html(C.flex(s["card"], BASE))}</div></div></div>')


def page(body: str) -> str:
    return f'<!doctype html><html lang="ja"><meta charset="utf-8"><style>{CSS}</style><body>{body}</body></html>'


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="docs/images/baito-quick-start", help="HTML と PNG を書くディレクトリ")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sc = scenes()
    pages = {
        "card-entry": page(scene_html("入口のカード(新しい相談の最初)", sc["entry"])),
        "card-result": page(scene_html("職種の結果(ひとりで黙々)", sc["result"])),
        "card-light": page(scene_html("ちょっとだけ(どれもピンと来ない)", sc["light"])),
    }
    for name, text in pages.items():
        (out / f"{name}.html").write_text(text, encoding="utf-8")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"playwright が無いので HTML だけ書きました: {out}")
        return
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(device_scale_factor=2)
        for name in pages:
            pg.goto((out / f"{name}.html").resolve().as_uri())
            pg.locator("#shot").screenshot(path=str(out / f"{name}.png"))
            (out / f"{name}.html").unlink()  # PNG にできたら HTML は残さない
        b.close()
    print(f"書きました: {', '.join(str(out / f'{n}.png') for n in pages)}")


if __name__ == "__main__":
    main()
