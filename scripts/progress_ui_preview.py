"""プロフィールが育つ UI(帯、プロフィールカード、完成。line_runtime/progress_ui.py)の画面の写真を作る(task 259)。

アルバイト・パートの相談(dip_mcp/baito)を、偽のモデルとボタンだけで最後まで進め、帯・プロフィールカード・完成が付く
ターンの返信(吹き出し 1 つと Flex 1 つ、クイックリプライ)を、LINE のトークに近い見た目の HTML に描いて PNG にする。
Flex は LINE の Flex の決まり(bubble の size、box の layout・width・backgroundColor、text の size など)をまねて描くだけで、
LINE の実機の描き方ではない(実機では確かめていない)。帯の size(micro / kilo)とバーの色の見比べも 1 枚作る。

    ./.venv/bin/python -m scripts.progress_ui_preview [--out docs/images/progress-ui] [--segment homemaker]

実 API は使わない。PNG は Playwright(と Chromium)があるときだけ作る(無ければ HTML だけ)。
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from dip_mcp.baito import harness as H
from dip_mcp.baito import progress as P
from line_runtime import progress_ui as U

SEGMENTS = {"student": "学生", "homemaker": "家のこと中心（主婦・主夫など）", "side_job": "本業あり副業探し",
            "other": "フリーター・求職中・その他"}
# LINE の Flex の大きさ(px)。bubble の幅と、text・spacing の size
BUBBLE_W = {"nano": 120, "micro": 160, "deca": 220, "hecto": 241, "kilo": 260, "mega": 300, "giga": 500}
TEXT_PX = {"xxs": 11, "xs": 13, "sm": 14, "md": 16, "lg": 19, "xl": 22}
SPACE_PX = {"none": 0, "xs": 2, "sm": 4, "md": 8, "lg": 12, "xl": 16, "xxl": 20}


class MockLLM:
    """言い方は質問・カードの原文、結果は候補の上から(tests/test_baito_harness.LLM と同じ)。"""

    def extract(self, payload):
        return json.dumps({"slot_updates": []})

    def generate(self, v):
        if v.get("result_json"):
            r = json.loads(v["result_json"])
            recs = [{"job_id": c["job_id"], "reason": H.fallback_reason(c)} for c in r["candidates"][:r["need"]]]
            return json.dumps({"reply": "条件から、合いそうなバイトを選んだよ！", "recommendations": recs}, ensure_ascii=False)
        if v["mode"] == "review":
            return json.dumps({"reply": v["card_text"] + "\n\n" + H.CLOSING_JA}, ensure_ascii=False)
        return json.dumps({"reply": v["text"]}, ensure_ascii=False)


def scenes(segment: str) -> dict[str, dict]:
    """相談を最後まで進め、部品ごとに 1 ターンの {said, reply, choices, ui}。帯は、いちばん多く埋まったターン(「ほか N」が出る)。"""
    h, mem, out = H.new_state(), None, {}
    H.start(h, H.BANK)
    picks = {"q_segment": SEGMENTS[segment], "hf_age": "18歳以上"}
    for _ in range(40):
        if h.result:
            break
        cur = h.current
        said = H.REVIEW_OK if cur["mode"] == "review" else picks.get(cur["qid"]) or cur["choices"][0]
        prev = P.before(h)
        r = H.turn(h, said, H.BANK, MockLLM())
        p, mem = P.update(h, mem, prev, r)
        ui = U.build(p, "flex")
        if ui and (ui["kind"] not in out or ui["kind"] == U.BAND and len(p["added"]) > out[U.BAND]["added"]):
            out[ui["kind"]] = {"said": said, "reply": r["reply"], "choices": list(r["view"].get("choices", [])), "ui": ui,
                               "added": len(p["added"])}
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
        style.append("white-space:pre-wrap;word-break:break-word" if n.get("wrap") else
                     "white-space:nowrap;overflow:hidden;text-overflow:ellipsis")
        return f'<div style="{";".join(style)}">{html.escape(n["text"])}</div>'
    if n["type"] == "separator":
        return f'<div style="{";".join(style)};border-top:1px solid #e5e5e5"></div>'
    layout = n.get("layout", "vertical")
    gap = SPACE_PX.get(n.get("spacing", "none"), 0)
    style += [f"display:flex;flex-direction:{'row' if layout == 'horizontal' else 'column'};gap:{gap}px"]
    for k, css in (("width", "width"), ("height", "height"), ("backgroundColor", "background"),
                   ("cornerRadius", "border-radius"), ("paddingAll", "padding")):
        if n.get(k):
            style.append(f"{css}:{_px(n[k])}")
    if n.get("alignItems"):
        style.append(f"align-items:{n['alignItems']}")
    return f'<div style="{";".join(style)}">{"".join(_node(c, layout) for c in n.get("contents", []))}</div>'


def flex_html(message: dict) -> str:
    b = message["contents"]
    body = dict(b["body"])
    body.setdefault("paddingAll", "20px")
    w = BUBBLE_W[b.get("size", "mega")]
    return (f'<div class="flex" style="width:{w}px">{_node(body, "vertical")}</div>')


CSS = """
body { margin: 0; background: #e9ecf2; font-family: -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif; }
.phone { width: 390px; background: #8aa6cf; padding: 14px 10px 12px; box-sizing: border-box; }
.cap { color: #fff; font-size: 12px; text-align: center; margin-bottom: 10px; opacity: .9; }
.row { display: flex; gap: 8px; align-items: flex-start; margin: 8px 0; } .row.me { justify-content: flex-end; }
.avatar { width: 34px; height: 34px; border-radius: 50%; background: #fff; color: #d0021b; font-weight: 800; font-size: 13px;
  display: flex; align-items: center; justify-content: center; flex: none; }
.col { display: flex; flex-direction: column; gap: 6px; max-width: 290px; }
.bubble { background: #fff; padding: 8px 12px; border-radius: 16px; font-size: 14.5px; line-height: 1.55; white-space: pre-wrap; }
.me .bubble { background: #85e249; }
.flex { background: #fff; border-radius: 16px; overflow: hidden; box-sizing: border-box; }
.qr { display: flex; gap: 6px; overflow: hidden; margin-top: 12px; padding: 8px 4px; background: #ffffffcc; border-radius: 10px; }
.qr span { flex: none; border: 1px solid #c8d3e6; color: #3d5a99; background: #fff; border-radius: 16px; padding: 5px 11px; font-size: 13px; }
.grid { display: grid; grid-template-columns: repeat(2, auto); gap: 14px; padding: 14px; background: #8aa6cf; width: max-content; }
.grid .cap { margin: 0 0 6px; text-align: left; }
"""


def scene_html(title: str, s: dict) -> str:
    qr = "".join(f"<span>{html.escape(c)}</span>" for c in s["choices"])
    return (f'<div class="phone" id="shot"><div class="cap">{html.escape(title)}</div>'
            f'<div class="row me"><div class="bubble">{html.escape(s["said"])}</div></div>'
            f'<div class="row"><div class="avatar">B</div><div class="col"><div class="bubble">{html.escape(s["reply"])}</div>'
            f'{flex_html(s["ui"]["flex"])}</div></div>' + (f'<div class="qr">{qr}</div>' if qr else "") + "</div>")


def sizes_html(band: dict) -> str:
    cells = []
    for size in ("micro", "kilo"):
        for color in (U.BAR_COLOR, "#3d7bfd"):
            msg = json.loads(json.dumps(band["ui"]["flex"]).replace(U.BAR_COLOR, color))
            msg["contents"]["size"] = size
            cells.append(f'<div><div class="cap">size={size} / バー {color}</div>{flex_html(msg)}</div>')
    return f'<div class="grid" id="shot">{"".join(cells)}</div>'


def page(body: str) -> str:
    return f'<!doctype html><html lang="ja"><meta charset="utf-8"><style>{CSS}</style><body>{body}</body></html>'


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="docs/images/progress-ui", help="HTML と PNG を書くディレクトリ")
    ap.add_argument("--segment", default="homemaker", choices=sorted(SEGMENTS))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    sc = scenes(a.segment)
    pages = {
        "band": page(scene_html("帯: 何かが埋まったターン", sc[U.BAND])),
        "card": page(scene_html("プロフィールカード: hard_filter が終わったターン", sc[U.CARD])),
        "complete": page(scene_html("完成: 最後のターン(結果の吹き出しは今のまま)", sc[U.COMPLETE])),
        "band-sizes": page(sizes_html(sc[U.BAND])),
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
