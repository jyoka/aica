"""アルバイトの相談を 1 タップで始める入口と結果のカードのアイコン(設計メモ docs/design/baito-quick-start.md の §8)を作る。

docs/images/baito-quick-start/sprites.json の 16×16 の各マスの色を、64×64 の PNG(1 マス 4 px、背景は透明)にする。
拡大は補間しない(マスの境目をぼかさない)。LINE の Flex では拡大の補間を指定できないので、表示する大きさで作る。
作った PNG は line_runtime/static/baito/ に置き、サーバーの /static/baito/{名前}.png から出す。

    ./.venv/bin/python -m scripts.baito_icons [--out line_runtime/static/baito] [--sheet docs/images/baito-quick-start/icons.png]

--sheet は、5 枚を暗いカードの色と LINE のふつうの背景の上に並べた見本の PNG も作る(PR に貼る写真)。実 API は使わない。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image

from line_runtime import config

SPRITES = config.ROOT / "docs" / "images" / "baito-quick-start" / "sprites.json"
OUT = config.ROOT / "line_runtime" / "static" / "baito"
SCALE = 4  # 1 マスの px
# 見本の背景: 暗いカード(§8 の #1c1430)と、LINE のふつうのトークの背景(推測の近い色)
SHEET_BG = ("#1c1430", "#8cabd9")
SHEET_GAP = 16


def load(path: Path = SPRITES) -> dict[str, dict]:
    """sprites.json: {名前: {"w": 16, "h": 16, "px": [[x, y, "#rrggbb"], ...]}}。px に無いマスは透明。"""
    return json.loads(path.read_text(encoding="utf-8"))


def render(sprite: dict, scale: int = SCALE) -> Image.Image:
    """1 つの絵を、背景が透明な RGBA の画像にする。1 マスを scale × scale にし、補間しない(NEAREST)。"""
    img = Image.new("RGBA", (sprite["w"], sprite["h"]), (0, 0, 0, 0))
    for x, y, color in sprite["px"]:
        img.putpixel((x, y), (*bytes.fromhex(color.removeprefix("#")), 255))
    return img.resize((sprite["w"] * scale, sprite["h"] * scale), Image.NEAREST)


def build(sprites: Path = SPRITES, out: Path = OUT) -> list[Path]:
    """sprites.json の絵をすべて {out}/{名前}.png に書く。書いたファイルを返す。"""
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, sprite in load(sprites).items():
        path = out / f"{name}.png"
        render(sprite).save(path, optimize=True)
        paths.append(path)
    return paths


def sheet(sprites: Path = SPRITES) -> Image.Image:
    """5 枚を、背景の色ごとに 1 行ずつ並べた見本(64×64 のまま。拡大しない)。"""
    icons = [render(s) for s in load(sprites).values()]
    size = icons[0].width
    w = SHEET_GAP + len(icons) * (size + SHEET_GAP)
    img = Image.new("RGBA", (w, len(SHEET_BG) * (size + 2 * SHEET_GAP)))
    for row, bg in enumerate(SHEET_BG):
        top = row * (size + 2 * SHEET_GAP)
        img.paste(bg, (0, top, w, top + size + 2 * SHEET_GAP))
        for i, icon in enumerate(icons):
            img.alpha_composite(icon, (SHEET_GAP + i * (size + SHEET_GAP), top + SHEET_GAP))
    return img


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sprites", type=Path, default=SPRITES)
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--sheet", type=Path, help="5 枚を並べた見本の PNG の書き先")
    a = p.parse_args()
    for path in build(a.sprites, a.out):
        print(path)
    if a.sheet:
        sheet(a.sprites).convert("RGB").save(a.sheet, optimize=True)
        print(a.sheet)


if __name__ == "__main__":
    main()
