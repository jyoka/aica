"""LINE 公式アカウントのリッチメニュー(トークの下に常に出しておけるメニュー)を作って登録する(task 220)。

ボタンは 音声相談、履歴書の読み込み、ゲーム(レジュメ・アーケード)、求人を探す の 4 つ。前の 3 つは LIFF URL
(line_channel.liff_page_url。トークのボタンと同じ画面)を直に開き、「求人を探す」はその文を本人の発言として送る(トークの選択肢と同じ)。
音声相談は LiveKit の設定もあるときだけ置く(line_channel.liff_features。使えないものは出さない)。LIFF の設定が無いときは作らない。
LINE に登録するのは人。設定(.env)は Web サーバーと同じものを読む。手順は README の「LINE のリッチメニュー」。

    ./.venv/bin/python -m scripts.line_rich_menu --dry-run      # 画像と JSON を data/richmenu/ に作るだけ(LINE を呼ばない)
    ./.venv/bin/python -m scripts.line_rich_menu                # 作って、画像を上げて、友だち全員の既定のメニューにする
    ./.venv/bin/python -m scripts.line_rich_menu --replace      # 前にこのスクリプトで作ったメニュー(名前が NAME)を消してから

呼ぶ LINE の API(Messaging API。チャネルアクセストークン LINE_CHANNEL_ACCESS_TOKEN):
- POST https://api.line.me/v2/bot/richmenu/validate(形を確かめる)→ POST /v2/bot/richmenu(作る。richMenuId が返る)
- POST https://api-data.line.me/v2/bot/richmenu/{richMenuId}/content(画像。PNG)
- POST https://api.line.me/v2/bot/user/all/richmenu/{richMenuId}(既定のリッチメニューにする)
- 既定にする前に GET /v2/bot/user/all/richmenu で今の既定を見て、別のメニューが既定なら、既定でなくなることを出す
- --replace: 新しいメニューを既定にした後で、GET /v2/bot/richmenu/list から名前が NAME の前のメニューを DELETE /v2/bot/richmenu/{richMenuId}
  (途中で失敗しても、前のメニューが残る。メニューが出ない間を作らない)
transport はテストで偽物の LINE に差し替える(テストは本物の LINE を呼ばない)。トークンはログにも画面にも出さない。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from line_runtime import config, line_channel

NAME = "aica-menu"  # --replace で消すのは、この名前のメニューだけ(ほかの運用のメニューは触らない)
CHAT_BAR_TEXT = "メニュー"  # トークの下のバーの文言(14 字まで)
WIDTH, HEIGHT = 2500, 843  # LINE のリッチメニューの画像の大きさの 1 つ(小さい方。トークが隠れにくい)
MAX_IMAGE_BYTES = 1024 * 1024  # 画像の上限(1MB)
DATA_API = "https://api-data.line.me"
JOBS_TEXT = "求人を探す"  # 押すと本人の発言としてトークに送る文(トークの選択肢と同じ)
OUT_DIR = config.ROOT / "data" / "richmenu"
# 日本語が出るフォント(--font が無ければ順に探す)。macOS のヒラギノ、Linux の Noto Sans CJK
FONT_CANDIDATES = (
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
)
GREEN, INK, SUB, LINE_GRAY, BG = "#06c755", "#222222", "#666666", "#dddddd", "#ffffff"


class RichMenuError(RuntimeError):
    """作れない(設定が無い)、LINE が断った。"""


@dataclass(frozen=True)
class Item:
    key: str  # voice_consult / resume_upload / arcade / jobs
    label: str  # 画像の大きな文言
    sub: str  # 画像の小さな文言
    action: dict  # LINE のアクション(uri か message)


def items() -> list[Item]:
    """メニューに置くボタン(左から)。LIFF の設定が無ければ RichMenuError(作らない)。
    音声相談は LiveKit の設定もあるときだけ(liff_features に voice_consult があるとき)。"""
    features = line_channel.liff_features()
    if not features:
        raise RichMenuError(f"LIFF の設定({line_channel.LIFF_ID_ENV} と {line_channel.LIFF_CHANNEL_ENV})が無いので、"
                            "リッチメニューは作りません。音声相談、履歴書、ゲームは LINE の画面(LIFF)で開くためです。")

    def uri(label: str, subpath: str) -> dict:
        return {"type": "uri", "label": label, "uri": line_channel.liff_page_url(subpath)}

    out = []
    if "voice_consult" in features:
        out.append(Item("voice_consult", "音声で相談", "話して伝える(約5分)",
                        uri("音声で相談", line_channel.LIFF_ACTIONS["voice_consult"][1])))
    out.append(Item("resume_upload", "履歴書を読み込む", "PDF・写真から",
                    uri("履歴書を読み込む", line_channel.LIFF_ACTIONS["resume_upload"][1])))
    if "arcade" in features:
        out.append(Item("arcade", "ゲームで書類を作る", "遊びながら書類づくり",
                        uri("ゲームで書類を作る", line_channel.ARCADE_PATH.removeprefix("/liff/"))))
    out.append(Item("jobs", JOBS_TEXT, "条件を聞いて探します", {"type": "message", "label": JOBS_TEXT, "text": JOBS_TEXT}))
    return out


def _bounds(n: int) -> list[dict]:
    """横に n 等分した領域(端数は最後の列に足す)。"""
    w = WIDTH // n
    return [{"x": i * w, "y": 0, "width": WIDTH - i * w if i == n - 1 else w, "height": HEIGHT} for i in range(n)]


def menu_body(menu: list[Item]) -> dict:
    """POST /v2/bot/richmenu の本文。selected=True で、トークを開いたときにメニューが開いている。"""
    return {"size": {"width": WIDTH, "height": HEIGHT}, "selected": True, "name": NAME, "chatBarText": CHAT_BAR_TEXT,
            "areas": [{"bounds": b, "action": it.action} for b, it in zip(_bounds(len(menu)), menu, strict=True)]}


# ----------------------------- 画像 -----------------------------


def find_font(path: str | None = None) -> str:
    if path:
        if not Path(path).is_file():
            raise RichMenuError(f"フォント {path} がありません")
        return path
    for p in FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    raise RichMenuError("日本語のフォントが見つかりません。--font でフォントのファイル(.ttf / .ttc / .otf)を渡してください")


def _icon(draw, key: str, cx: int, cy: int, r: int) -> None:
    """ボタンの絵(緑の丸に白の線画)。"""
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=GREEN)
    w = max(6, r // 9)
    if key == "voice_consult":  # マイク
        draw.rounded_rectangle((cx - r * 0.22, cy - r * 0.55, cx + r * 0.22, cy + r * 0.15), radius=r * 0.22, fill=BG)
        draw.arc((cx - r * 0.42, cy - r * 0.35, cx + r * 0.42, cy + r * 0.38), 0, 180, fill=BG, width=w)
        draw.line((cx, cy + r * 0.38, cx, cy + r * 0.6), fill=BG, width=w)
        draw.line((cx - r * 0.25, cy + r * 0.6, cx + r * 0.25, cy + r * 0.6), fill=BG, width=w)
    elif key == "resume_upload":  # 書類
        draw.rectangle((cx - r * 0.38, cy - r * 0.55, cx + r * 0.38, cy + r * 0.55), fill=BG)
        for k in range(4):
            y = cy - r * 0.3 + k * r * 0.22
            draw.line((cx - r * 0.22, y, cx + r * (0.22 if k < 3 else 0.05), y), fill=GREEN, width=w)
    elif key == "arcade":  # ゲームのコントローラー
        draw.rounded_rectangle((cx - r * 0.6, cy - r * 0.32, cx + r * 0.6, cy + r * 0.32), radius=r * 0.3, fill=BG)
        draw.line((cx - r * 0.42, cy, cx - r * 0.18, cy), fill=GREEN, width=w)
        draw.line((cx - r * 0.3, cy - r * 0.12, cx - r * 0.3, cy + r * 0.12), fill=GREEN, width=w)
        for dx, dy in ((0.22, -0.08), (0.38, 0.08)):
            s = r * 0.07
            draw.ellipse((cx + r * dx - s, cy + r * dy - s, cx + r * dx + s, cy + r * dy + s), fill=GREEN)
    else:  # 虫めがね
        draw.ellipse((cx - r * 0.42, cy - r * 0.42, cx + r * 0.18, cy + r * 0.18), outline=BG, width=w + 4)
        draw.line((cx + r * 0.12, cy + r * 0.12, cx + r * 0.45, cy + r * 0.45), fill=BG, width=w + 8)


def _fit(draw, text: str, font_path: str, size: int, max_width: int):
    from PIL import ImageFont

    while True:
        font = ImageFont.truetype(font_path, size)
        if draw.textlength(text, font=font) <= max_width or size <= 24:
            return font
        size -= 4


def render_image(menu: list[Item], font_path: str) -> bytes:
    """メニューの画像(PNG、WIDTH x HEIGHT)。列ごとに絵、大きな文言、小さな文言。1MB を超えたら RichMenuError。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    bounds, pad = _bounds(len(menu)), 40
    room = min(b["width"] for b in bounds) - pad * 2
    # 文言の大きさは、どの列も同じにする(いちばん長い文言が列に収まる大きさ)
    big = min((_fit(draw, it.label, font_path, 96, room) for it in menu), key=lambda f: f.size)
    small = min((_fit(draw, it.sub, font_path, 56, room) for it in menu), key=lambda f: f.size)
    for b, it in zip(bounds, menu, strict=True):
        cx = b["x"] + b["width"] // 2
        if b["x"]:
            draw.line((b["x"], 60, b["x"], HEIGHT - 60), fill=LINE_GRAY, width=4)
        _icon(draw, it.key, cx, 260, 150)
        draw.text((cx, 530), it.label, font=big, fill=INK, anchor="mm")
        draw.text((cx, 650), it.sub, font=small, fill=SUB, anchor="mm")
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=LINE_GRAY, width=4)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        raise RichMenuError(f"画像が {len(data)} バイトで、LINE の上限 {MAX_IMAGE_BYTES} バイトを超えます")
    return data


# ----------------------------- LINE の API -----------------------------


class RichMenuApi:
    """リッチメニューの API。transport はテストで偽物の LINE に差し替える。"""

    def __init__(self, token: str, base_url: str = line_channel.API_BASE, data_url: str = DATA_API,
                 transport: httpx.BaseTransport | None = None) -> None:
        headers = {"Authorization": f"Bearer {token}"}
        self._api = httpx.Client(base_url=base_url, transport=transport, timeout=30.0, headers=headers)
        self._data = httpx.Client(base_url=data_url, transport=transport, timeout=60.0, headers=headers)

    def close(self) -> None:
        self._api.close()
        self._data.close()

    @staticmethod
    def _ok(r: httpx.Response, what: str) -> httpx.Response:
        if not r.is_success:
            raise RichMenuError(f"LINE が{what}を断りました({r.status_code}): {r.text[:300]}")
        return r

    def validate(self, body: dict) -> None:
        self._ok(self._api.post("/v2/bot/richmenu/validate", json=body), "リッチメニューの形")

    def create(self, body: dict) -> str:
        rid = self._ok(self._api.post("/v2/bot/richmenu", json=body), "リッチメニューの作成").json().get("richMenuId")
        if not isinstance(rid, str) or not rid:
            raise RichMenuError("LINE の答えに richMenuId がありません")
        return rid

    def upload(self, rid: str, png: bytes) -> None:
        self._ok(self._data.post(f"/v2/bot/richmenu/{quote(rid, safe='')}/content", content=png,
                                 headers={"Content-Type": "image/png"}), "画像のアップロード")

    def default_id(self) -> str | None:
        """今の既定のリッチメニューの ID(無ければ None。LINE は 404 を返す)。"""
        r = self._api.get("/v2/bot/user/all/richmenu")
        if r.status_code == 404:
            return None
        return self._ok(r, "既定のリッチメニューの取得").json().get("richMenuId")

    def set_default(self, rid: str) -> None:
        self._ok(self._api.post(f"/v2/bot/user/all/richmenu/{quote(rid, safe='')}"), "既定のリッチメニューの設定")

    def list(self) -> list[dict]:
        return self._ok(self._api.get("/v2/bot/richmenu/list"), "リッチメニューの一覧").json().get("richmenus") or []

    def delete(self, rid: str) -> None:
        self._ok(self._api.delete(f"/v2/bot/richmenu/{quote(rid, safe='')}"), "リッチメニューの削除")


def register(api: RichMenuApi, body: dict, png: bytes, replace: bool = False, log=print) -> str:
    """確かめて、作って、画像を上げて、既定にする。replace なら、その後で同じ名前(NAME)の前のメニューを消す
    (新しいメニューが既定になってから消すので、途中で失敗しても前のメニューが出たまま)。作ったメニューの ID を返す。
    既定は友だち全員に 1 つなので、ほかのツールで既定にしていたメニューは既定でなくなる(消しはしない)。"""
    api.validate(body)
    before = api.default_id()
    rid = api.create(body)
    api.upload(rid, png)
    api.set_default(rid)
    log(f"リッチメニュー {rid} を作り、友だち全員の既定にしました")
    olds = [m["richMenuId"] for m in api.list() if m.get("name") == NAME and m.get("richMenuId") != rid] if replace else []
    if before and before not in olds:
        log(f"前に既定だったメニュー {before} は、既定でなくなりました(消していません)")
    for old in olds:
        api.delete(old)
        log(f"前のメニュー {old} を消しました")
    return rid


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    ap = argparse.ArgumentParser(description="LINE のリッチメニューを作って登録する")
    ap.add_argument("--dry-run", action="store_true", help="画像と JSON を作るだけ(LINE を呼ばない)")
    ap.add_argument("--replace", action="store_true", help=f"前にこのスクリプトで作ったメニュー(名前 {NAME})を消してから登録する")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="画像(richmenu.png)と本文(richmenu.json)を書くところ")
    ap.add_argument("--font", help="日本語のフォントのファイル(無ければ OS のものを探す)")
    args = ap.parse_args(argv)
    try:
        menu = items()
        body = menu_body(menu)
        png = render_image(menu, find_font(args.font))
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "richmenu.png").write_bytes(png)
        (args.out / "richmenu.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{args.out}/richmenu.png と richmenu.json を作りました(ボタン: {'、'.join(it.label for it in menu)})")
        if args.dry_run:
            return 0
        token = line_channel.access_token()
        if not token:
            raise RichMenuError(f"{line_channel.TOKEN_ENV} が無いので、LINE に登録できません")
        api = RichMenuApi(token, transport=transport)
        try:
            register(api, body, png, replace=args.replace)
        finally:
            api.close()
    except RichMenuError as e:
        print(f"!! {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    config.load_env()
    sys.exit(main())
