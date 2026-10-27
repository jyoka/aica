"""アルバイトの相談のアイコン(docs/design/baito-quick-start.md の §8。scripts/baito_icons.py)のテスト。実 API なし。

- スクリプトで 5 枚(talk、move、solo、sit、light)が作れ、どれも 64×64、背景が透明で、各マスの 4×4 px が sprites.json の色と
  一致する(補間でぼけていない)
- line_runtime/static/baito/ に置いた 5 枚が、スクリプトで作り直したものと同じ絵(sprites.json を直したら作り直す)
- 5 枚がサーバーの /static/baito/{名前}.png から出る。ほかの名前は 404
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from scripts import baito_icons
from tests.test_talk_page import app  # noqa: F401  app はフィクスチャ

NAMES = ["talk", "move", "solo", "sit", "light"]
SPRITES = json.loads(baito_icons.SPRITES.read_text(encoding="utf-8"))


def _cells(img: Image.Image) -> dict[tuple[int, int], set[tuple[int, int, int, int]]]:
    """16×16 の各マスの中の 4×4 px の色(RGBA)の集まり。"""
    px = img.load()
    return {(x, y): {px[x * 4 + i, y * 4 + j] for i in range(4) for j in range(4)} for x in range(16) for y in range(16)}


def _check(img: Image.Image, name: str) -> None:
    assert img.size == (64, 64) and img.mode == "RGBA", name
    want = {(x, y): (*bytes.fromhex(c[1:]), 255) for x, y, c in SPRITES[name]["px"]}
    for cell, colors in _cells(img).items():
        if cell in want:
            assert colors == {want[cell]}, (name, cell, colors)
        else:
            assert {a for *_, a in colors} == {0}, (name, cell, colors)  # 絵の無いマスは透明


def test_the_script_makes_five_64px_transparent_icons_matching_the_sprites(tmp_path):
    paths = baito_icons.build(out=tmp_path)
    assert [p.name for p in paths] == [f"{n}.png" for n in NAMES]
    for name in NAMES:
        with Image.open(tmp_path / f"{name}.png") as img:
            assert img.format == "PNG"
            _check(img, name)
            assert img.getpixel((0, 0))[3] == 0  # 角は透明


def test_the_shipped_icons_are_the_ones_the_script_makes():
    for name in NAMES:
        with Image.open(baito_icons.OUT / f"{name}.png") as img:
            _check(img, name)
            assert img.tobytes() == baito_icons.render(SPRITES[name]).tobytes(), name


def test_the_sheet_shows_all_five_icons_at_their_own_size():
    img = baito_icons.sheet()
    gap = baito_icons.SHEET_GAP
    assert img.size == (gap + 5 * (64 + gap), 2 * (64 + 2 * gap))


@pytest.fixture
def client(app):  # noqa: F811
    from fastapi.testclient import TestClient

    return TestClient(app.app)  # lifespan を回さない(アイコンは会話を使わない)


def test_the_icons_are_served_from_static(client, app):  # noqa: F811
    assert app.BAITO_ICON_STATIC == {f"{n}.png" for n in SPRITES}
    for name in NAMES:
        r = client.get(f"/static/baito/{name}.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", name
        assert r.content == (baito_icons.OUT / f"{name}.png").read_bytes()
        with Image.open(io.BytesIO(r.content)) as img:
            _check(img, name)


def test_other_names_are_404(client):
    for path in ["/static/baito/nope.png", "/static/baito/talk", "/static/baito/..%2Fserver.py",
                 "/static/baito/sprites.json", "/static/talk.png"]:
        assert client.get(path).status_code == 404, path
