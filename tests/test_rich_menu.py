"""LINE のリッチメニューを作って登録するスクリプト(scripts/line_rich_menu.py、task 220)。本物の LINE は呼ばない。

- ボタン: 音声相談、履歴書の読み込み、ゲーム、求人を探す。前の 3 つは LIFF URL(トークのボタンと同じ画面)を直に開く
- LIFF の設定が無いときは作らない(LINE を呼ばない)。音声相談は LiveKit の設定もあるときだけ
- 登録の順(確かめる → 作る → 画像を api-data に上げる → 既定にする)と、--replace が消すのは同じ名前のメニューだけ
"""

from __future__ import annotations

import io
import json

import httpx
import pytest

from line_runtime import line_channel
from scripts import line_rich_menu as rm

LIFF_ID = "2000000000-OfflineT"
CHANNEL = "2000000000"
TOKEN = "offline-test-channel-access-token"
LIVEKIT_ENV = {"LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k",
               "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret"}


class FakeRichMenuApi:
    """偽物の LINE(リッチメニューの API)。届いた呼び出しを (メソッド, ホスト, パス, 本文) で覚える。"""

    def __init__(self, existing: list[dict] | None = None, reject: str | None = None, default: str | None = None) -> None:
        self.calls: list[tuple[str, str, str, bytes]] = []
        self.existing = existing or []
        self.reject = reject  # このパスで終わる呼び出しを 400 で断る
        self.default = default  # 今の既定のメニュー(無ければ 404)

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.host, req.url.path, req.content))
        assert req.headers["authorization"] == f"Bearer {TOKEN}"
        if self.reject and req.url.path.endswith(self.reject):
            return httpx.Response(400, json={"message": "invalid"})
        if req.url.path == "/v2/bot/richmenu" and req.method == "POST":
            return httpx.Response(200, json={"richMenuId": "richmenu-new"})
        if req.url.path == "/v2/bot/richmenu/list":
            return httpx.Response(200, json={"richmenus": self.existing})
        if req.url.path == "/v2/bot/user/all/richmenu" and req.method == "GET":
            return httpx.Response(200, json={"richMenuId": self.default}) if self.default else httpx.Response(404, json={})
        return httpx.Response(200, json={})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def env(monkeypatch):
    for k in ("LINE_LIFF_ID", "LINE_LIFF_CHANNEL_ID", "LINE_LOGIN_CHANNEL_ID", line_channel.TOKEN_ENV,
              *line_channel.LIVEKIT_ENVS):
        monkeypatch.delenv(k, raising=False)

    def set_env(liff: bool = True, voice: bool = True, token: bool = True) -> None:
        if liff:
            monkeypatch.setenv("LINE_LIFF_ID", LIFF_ID)
            monkeypatch.setenv("LINE_LIFF_CHANNEL_ID", CHANNEL)
        if voice:
            for k, v in LIVEKIT_ENV.items():
                monkeypatch.setenv(k, v)
        if token:
            monkeypatch.setenv(line_channel.TOKEN_ENV, TOKEN)

    return set_env


def _font() -> str:
    try:
        return rm.find_font()
    except rm.RichMenuError:
        pytest.skip("日本語のフォントが無い環境")


def test_menu_has_the_four_entries_and_opens_the_same_liff_pages_as_the_talk_buttons(env):
    env()
    body = rm.menu_body(rm.items())
    assert body["size"] == {"width": 2500, "height": 843} and body["selected"] is True and body["name"] == rm.NAME
    actions = [a["action"] for a in body["areas"]]
    assert [a.get("label") for a in actions] == ["音声で相談", "履歴書を読み込む", "ゲームで書類を作る", "求人を探す"]
    # トークのボタン(line_channel.to_messages の LIFF_ACTIONS と、ゲームの liff_url_of)と同じ URL
    assert [a.get("uri") for a in actions[:3]] == [
        line_channel.liff_page_url(line_channel.LIFF_ACTIONS["voice_consult"][1]),
        line_channel.liff_page_url(line_channel.LIFF_ACTIONS["resume_upload"][1]),
        line_channel.liff_url_of(line_channel.ARCADE_PATH)]
    assert actions[1]["uri"] == f"https://liff.line.me/{LIFF_ID}/resume/upload"
    assert actions[3] == {"type": "message", "label": "求人を探す", "text": "求人を探す"}  # トークの選択肢と同じ発言
    xs = [(a["bounds"]["x"], a["bounds"]["width"]) for a in body["areas"]]
    assert xs == [(0, 625), (625, 625), (1250, 625), (1875, 625)]  # 横に 4 等分して、すき間も重なりも無い


def test_without_livekit_the_voice_button_is_left_out(env):
    """音声相談は LINE で使えない(LiveKit の設定が無い)ので置かない。残りの 3 つで幅を分ける。"""
    env(voice=False)
    body = rm.menu_body(rm.items())
    assert [a["action"]["label"] for a in body["areas"]] == ["履歴書を読み込む", "ゲームで書類を作る", "求人を探す"]
    assert sum(a["bounds"]["width"] for a in body["areas"]) == rm.WIDTH


def test_without_liff_nothing_is_made_and_line_is_not_called(env, tmp_path, capsys):
    env(liff=False)
    fake = FakeRichMenuApi()
    assert rm.main(["--out", str(tmp_path)], transport=fake.transport) == 1
    assert fake.calls == [] and list(tmp_path.iterdir()) == []
    assert "リッチメニューは作りません" in capsys.readouterr().err


def test_image_is_a_png_of_the_menu_size_under_the_limit(env):
    env()
    from PIL import Image

    png = rm.render_image(rm.items(), _font())
    img = Image.open(io.BytesIO(png))
    assert img.format == "PNG" and img.size == (rm.WIDTH, rm.HEIGHT)
    assert len(png) <= rm.MAX_IMAGE_BYTES


def test_dry_run_writes_the_image_and_body_without_calling_line(env, tmp_path):
    env()
    fake = FakeRichMenuApi()
    assert rm.main(["--dry-run", "--out", str(tmp_path), "--font", _font()], transport=fake.transport) == 0
    assert fake.calls == []
    assert json.loads((tmp_path / "richmenu.json").read_text(encoding="utf-8"))["name"] == rm.NAME
    assert (tmp_path / "richmenu.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_register_validates_creates_uploads_and_sets_default_in_order(env, tmp_path):
    env()
    fake = FakeRichMenuApi()
    assert rm.main(["--out", str(tmp_path), "--font", _font()], transport=fake.transport) == 0
    assert [(m, h, p) for m, h, p, _ in fake.calls] == [
        ("POST", "api.line.me", "/v2/bot/richmenu/validate"),
        ("GET", "api.line.me", "/v2/bot/user/all/richmenu"),  # 今の既定(無い)
        ("POST", "api.line.me", "/v2/bot/richmenu"),
        ("POST", "api-data.line.me", "/v2/bot/richmenu/richmenu-new/content"),  # 画像は api-data のほう
        ("POST", "api.line.me", "/v2/bot/user/all/richmenu/richmenu-new"),
    ]
    assert json.loads(fake.calls[2][3]) == json.loads((tmp_path / "richmenu.json").read_text(encoding="utf-8"))
    assert fake.calls[3][3] == (tmp_path / "richmenu.png").read_bytes()


def test_replace_deletes_only_the_menus_this_script_made_after_the_new_one_is_default(env, tmp_path, capsys):
    env()
    old = {"richMenuId": "richmenu-old", "name": rm.NAME}
    fake = FakeRichMenuApi(existing=[old, {"richMenuId": "richmenu-campaign", "name": "キャンペーン"},
                                     {"richMenuId": "richmenu-new", "name": rm.NAME}], default="richmenu-old")
    assert rm.main(["--replace", "--out", str(tmp_path), "--font", _font()], transport=fake.transport) == 0
    paths = [(m, p) for m, _, p, _ in fake.calls]
    assert [p for m, p in paths if m == "DELETE"] == ["/v2/bot/richmenu/richmenu-old"]  # 作ったばかりのものと、ほかのツールのものは消さない
    # 新しいメニューを既定にしてから消す(途中で失敗しても、メニューが出ない間を作らない)
    assert paths.index(("POST", "/v2/bot/user/all/richmenu/richmenu-new")) < paths.index(("DELETE", "/v2/bot/richmenu/richmenu-old"))
    assert "既定でなくなりました" not in capsys.readouterr().out  # 前の既定は自分のもので、消した


def test_making_it_default_says_another_default_menu_stops_showing(env, tmp_path, capsys):
    """既定は友だち全員に 1 つ。ほかのツールで既定にしていたメニューは既定でなくなる(消さない)ことを出す。"""
    env()
    fake = FakeRichMenuApi(existing=[{"richMenuId": "richmenu-campaign", "name": "キャンペーン"}], default="richmenu-campaign")
    assert rm.main(["--replace", "--out", str(tmp_path), "--font", _font()], transport=fake.transport) == 0
    assert not any(m == "DELETE" for m, _, _, _ in fake.calls)
    assert "richmenu-campaign は、既定でなくなりました(消していません)" in capsys.readouterr().out


def test_line_refusal_stops_before_making_it_default_and_keeps_the_old_menu(env, tmp_path, capsys):
    env()
    fake = FakeRichMenuApi(reject="/content", existing=[{"richMenuId": "richmenu-old", "name": rm.NAME}])
    assert rm.main(["--replace", "--out", str(tmp_path), "--font", _font()], transport=fake.transport) == 1
    assert not any(m == "POST" and p.startswith("/v2/bot/user/all/richmenu") for m, _, p, _ in fake.calls)
    assert not any(m == "DELETE" for m, _, _, _ in fake.calls)
    err = capsys.readouterr().err
    assert "画像のアップロード" in err and TOKEN not in err


def test_without_token_it_stops_after_making_the_files(env, tmp_path, capsys):
    env(token=False)
    fake = FakeRichMenuApi()
    assert rm.main(["--out", str(tmp_path), "--font", _font()], transport=fake.transport) == 1
    assert fake.calls == [] and (tmp_path / "richmenu.png").is_file()
    assert line_channel.TOKEN_ENV in capsys.readouterr().err
