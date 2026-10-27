"""正社員のヒアリングの職歴カードに付ける「完成」のスタンプ(16×16 のドット絵を 4 倍にした 64×64 の PNG)を作る(task 246)。

色はレジュメ・アーケード(line_runtime/static/arcade/resume-arcade.html の :root)と同じ: 縁は --floor、面は --coin、
チェックは --neon、光は --neon2。LINE の Flex は AICA_PUBLIC_URL を前に付けて /static/career/stamp.png を読む。

    ./.venv/bin/python -m scripts.career_stamp [--out line_runtime/static/career/stamp.png]

実 API は使わない。同じ入力からは同じ PNG ができる。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

FLOOR, COIN, COIN_DARK, NEON, NEON2 = "#120d1f", "#ffd25f", "#c9a14a", "#ff5fa2", "#5ff2ff"
SIZE, SCALE = 16, 4
CHECK = [(4, 8), (5, 9), (6, 10), (7, 9), (8, 8), (9, 7), (10, 6), (11, 5)]  # チェックの線(左下 → 右上)
SHINE = [(4, 3), (5, 3), (3, 4), (3, 5)]


def _rgba(hex_color: str) -> tuple[int, int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255


def pixels() -> dict[tuple[int, int], str]:
    """(x, y) → 色。丸い印(縁、面、内側の輪)に、チェックと光を重ねる。"""
    out = {}
    c = (SIZE - 1) / 2
    for y in range(SIZE):
        for x in range(SIZE):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d <= 6.6:
                out[(x, y)] = COIN_DARK if 5.0 <= d <= 5.7 else COIN
            elif d <= 7.6:
                out[(x, y)] = FLOOR
    for x, y in CHECK:  # 2 ドットの太さ
        out[(x, y)] = out[(x, y + 1)] = NEON
    for p in SHINE:
        out[p] = NEON2
    return out


def stamp() -> Image.Image:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    for (x, y), color in pixels().items():
        img.putpixel((x, y), _rgba(color))
    return img.resize((SIZE * SCALE, SIZE * SCALE), Image.NEAREST)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="line_runtime/static/career/stamp.png")
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    stamp().save(out, optimize=True)
    print(f"書きました: {out}")


if __name__ == "__main__":
    main()
