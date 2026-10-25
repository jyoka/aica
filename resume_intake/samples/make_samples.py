"""架空のサンプル履歴書を5種類作る(本物の履歴書はリポジトリに入れない)。

  01_clean.pdf      きれいな PDF(テキスト埋め込み・項目多め)
  02_photo.jpg      スマホで撮った写真風(傾き・ノイズ・影・低めの JPEG 品質)
  03_sparse.png     項目が少ない(学歴と本人希望だけ)
  04_unreadable.jpg ほぼ読めない(強いぼかし。読めないときの挙動の確認用)
  05_table_photo.pdf JIS 風の表の PDF(テキスト埋め込み・顔写真の欄に架空の画像。テキストだけを送るときの比べ用)

人物・住所・電話・メールはすべて架空。生成にだけ reportlab / pillow を使う(requirements.txt には入れていない)。実行:
    ./.venv/bin/pip install reportlab pillow
    ./.venv/bin/python resume_intake/samples/make_samples.py
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

HERE = Path(__file__).resolve().parent
FONT_TTC = "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"

CLEAN_LINES = [
    ("h", "履 歴 書"),
    ("p", "2026年9月20日現在"),
    ("s", "基本情報"),
    ("p", "氏名: 佐藤 美咲(さとう みさき)"),
    ("p", "生年月日: 1998年4月12日(28歳)"),
    ("p", "住所: 〒100-0000 東京都架空区みほん町1丁目2番3号"),
    ("p", "電話: 090-0000-1111  メール: misaki.sato@example.com"),
    ("s", "学歴"),
    ("p", "2014年4月〜2017年3月  私立みほん高等学校 普通科  卒業"),
    ("p", "2017年4月〜2021年3月  みほん大学 経済学部  卒業"),
    ("s", "職歴"),
    ("p", "2021年4月〜2024年9月  株式会社サンプル商事(正社員)営業事務"),
    ("p", "  ・受発注データの入力・管理、電話とメールでの顧客対応"),
    ("p", "  ・受注処理の手順書を作成し、新人教育を担当した"),
    ("p", "2024年10月〜2026年8月  カフェみほん堂(アルバイト)ホールスタッフ"),
    ("p", "  ・接客、レジ、ドリンク提供"),
    ("s", "職務要約"),
    ("p", "大学卒業後、株式会社サンプル商事で約3年半、営業事務として受発注管理と顧客対応を担当。"),
    ("p", "退職後はカフェみほん堂でホールスタッフとして接客に従事している。"),
    ("s", "免許・資格"),
    ("p", "2016年10月  実用英語技能検定2級 取得"),
    ("p", "2017年8月   普通自動車第一種運転免許 取得"),
    ("p", "2022年6月   日商簿記検定3級 取得"),
    ("s", "自己PR"),
    ("p", "受発注データの入力では、確認リストを自分で作りミスを減らしました。"),
    ("p", "手順書づくりと新人教育を任された経験から、人に教えることが得意です。"),
    ("p", "カフェでは常連のお客様の好みを覚え、席の案内や提案に活かしています。"),
    ("s", "本人希望欄"),
    ("p", "事務の仕事を希望します。平日の勤務を希望します。"),
]

PHOTO_LINES = [
    ("h", "履歴書"),
    ("p", "氏名: 田中 大輝(たなか だいき)"),
    ("p", "生年月日: 2003年11月2日  電話: 080-1111-2222"),
    ("p", "住所: みほん県みほん市中央4-5-6"),
    ("s", "学歴"),
    ("p", "2019年4月〜2022年3月  みほん県立第一高等学校  卒業"),
    ("s", "職歴"),
    ("p", "2022年5月〜2024年12月  コンビニみほん駅前店(アルバイト)"),
    ("p", "  レジ、品出し。飲料の発注を任された。"),
    ("s", "部活動など"),
    ("p", "高校ではサッカー部に3年間所属(ディフェンダー)。"),
    ("s", "免許・資格"),
    ("p", "2023年3月  普通自動車第一種運転免許 取得"),
    ("s", "本人希望欄"),
    ("p", "体を動かす仕事がしたいです。週4日以上働けます。"),
]

SPARSE_LINES = [
    ("h", "履歴書"),
    ("p", "氏名: 高橋 さくら"),
    ("p", "生年月日: 2006年2月14日"),
    ("s", "学歴"),
    ("p", "2021年4月〜2024年3月  みほん学園高等学校  卒業"),
    ("s", "職歴"),
    ("p", "なし"),
    ("s", "免許・資格"),
    ("p", "なし"),
    ("s", "本人希望欄"),
    ("p", "土日に働けるお店を探しています。"),
]

# JIS 風の表の履歴書(05_table_photo.pdf)。年 / 月 / 学歴・職歴 の 3 列
TABLE_PERSONAL = [
    ("ふりがな", "すずき はると"),
    ("氏名", "鈴木 陽翔"),
    ("生年月日", "2001年7月3日生(満25歳)"),
    ("現住所", "〒000-0000 みほん県みほん市東町7-8-9"),
    ("電話", "070-2222-3333"),
]
TABLE_HISTORY = [
    ("", "", "学歴"),
    ("2017", "4", "みほん県立南高等学校 入学"),
    ("2020", "3", "みほん県立南高等学校 卒業"),
    ("2020", "4", "みほん情報専門学校 情報処理科 入学"),
    ("2022", "3", "みほん情報専門学校 情報処理科 卒業"),
    ("", "", "職歴"),
    ("2022", "4", "株式会社みほんシステム 入社(正社員) ヘルプデスク"),
    ("", "", "  社内からの問い合わせ対応、パソコンの設定"),
    ("2025", "6", "株式会社みほんシステム 退社"),
    ("2025", "8", "みほん家電 東町店 入社(契約社員) 販売スタッフ"),
    ("", "", "  パソコン売り場での接客と説明"),
    ("", "", "現在に至る"),
    ("", "", "以上"),
]
TABLE_LICENSES = [
    ("2021", "11", "基本情報技術者試験 合格"),
    ("2023", "2", "普通自動車第一種運転免許 取得"),
]
TABLE_WISH = "IT のサポートの仕事を希望します。土日も勤務できます。"


def make_table_pdf(out: Path) -> None:
    """JIS 風の表の履歴書。顔写真の欄に架空の画像(人の形の影)を置く。テキストは PDF に埋め込む。"""
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    c = canvas.Canvas(str(out), pagesize=A4)
    _, height = A4
    font = "HeiseiKakuGo-W5"
    left, right = 40, 555
    c.setFont(font, 20)
    c.drawString(left, height - 60, "履 歴 書")
    c.setFont(font, 9)
    c.drawString(left + 120, height - 58, "2026年10月1日現在")
    # 顔写真(架空。灰色の背景に人の形の影)
    photo = Image.new("RGB", (300, 400), (200, 205, 210))
    d = ImageDraw.Draw(photo)
    d.ellipse((95, 70, 205, 190), fill=(90, 90, 100))
    d.ellipse((40, 200, 260, 460), fill=(90, 90, 100))
    c.drawImage(ImageReader(photo), right - 90, height - 190, width=90, height=120)
    # 個人欄
    y = height - 80
    c.setFont(font, 10)
    for label, value in TABLE_PERSONAL:
        c.rect(left, y - 22, right - left - 100, 22)
        c.drawString(left + 6, y - 15, label)
        c.drawString(left + 80, y - 15, value)
        y -= 22
    # 学歴・職歴の表

    def table(y: float, title: str, rows: list[tuple[str, str, str]]) -> float:
        c.rect(left, y - 20, right - left, 20)
        c.drawString(left + 10, y - 14, "年")
        c.drawString(left + 60, y - 14, "月")
        c.drawString(left + 200, y - 14, title)
        y -= 20
        for year, month, text in rows:
            c.rect(left, y - 20, 45, 20)
            c.rect(left + 45, y - 20, 35, 20)
            c.rect(left + 80, y - 20, right - left - 80, 20)
            c.drawString(left + 8, y - 14, year)
            c.drawString(left + 55, y - 14, month)
            x = left + 250 if text in ("学歴", "職歴") else left + 90
            c.drawString(x, y - 14, text)
            y -= 20
        return y

    y = table(y - 30, "学歴・職歴", TABLE_HISTORY)
    y = table(y - 20, "免許・資格", TABLE_LICENSES)
    c.rect(left, y - 80, right - left, 70)
    c.drawString(left + 6, y - 24, "本人希望記入欄")
    c.drawString(left + 6, y - 44, TABLE_WISH)
    c.save()


def make_pdf(lines: list[tuple[str, str]], out: Path) -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    c = canvas.Canvas(str(out), pagesize=A4)
    _, height = A4
    y = height - 60
    for kind, text in lines:
        size = {"h": 20, "s": 13, "p": 10.5}[kind]
        y -= {"h": 30, "s": 26, "p": 16}[kind]
        c.setFont("HeiseiKakuGo-W5", size)
        c.drawString(50, y, text)
        if kind == "s":
            c.line(50, y - 3, 545, y - 3)
    c.save()


def render_image(lines: list[tuple[str, str]], width: int = 1000) -> Image.Image:
    fonts = {k: ImageFont.truetype(FONT_TTC, s) for k, s in (("h", 40), ("s", 27), ("p", 22))}
    img = Image.new("RGB", (width, 1600), "white")
    draw = ImageDraw.Draw(img)
    y = 60
    for kind, text in lines:
        if kind == "s":
            y += 22
        draw.text((60, y), text, font=fonts[kind], fill=(25, 25, 25))
        y += {"h": 62, "s": 36, "p": 32}[kind]
        if kind == "s":
            draw.line((60, y, width - 60, y), fill=(120, 120, 120), width=2)
            y += 12
    return img.crop((0, 0, width, y + 60))


def make_photo(lines: list[tuple[str, str]], out: Path) -> None:
    """写真風: 少し回転させ、明るさのムラとノイズを乗せ、低めの品質で JPEG 保存する。"""
    rng = random.Random(30)
    img = render_image(lines)
    img = img.rotate(2.4, expand=True, fillcolor=(210, 205, 198), resample=Image.BICUBIC)
    shade = Image.new("L", img.size, 0)
    sd = ImageDraw.Draw(shade)
    for i in range(img.height):
        sd.line((0, i, img.width, i), fill=int(40 * i / img.height))
    img = Image.composite(Image.new("RGB", img.size, (150, 145, 140)), img, shade.filter(ImageFilter.GaussianBlur(60)))
    noise = Image.effect_noise(img.size, 14).convert("L")
    img = Image.blend(img, Image.merge("RGB", (noise, noise, noise)), 0.06)
    img = img.filter(ImageFilter.GaussianBlur(0.6)).resize((img.width * 3 // 4, img.height * 3 // 4))
    img.save(out, quality=68 + rng.randint(0, 1))


def make_png(lines: list[tuple[str, str]], out: Path) -> None:
    render_image(lines).save(out)


def make_unreadable(lines: list[tuple[str, str]], out: Path) -> None:
    """読めない写真: 強いぼかしと縮小で文字を潰す(「読めない場合の返し方」の確認用)。"""
    img = render_image(lines)
    img = img.resize((img.width // 5, img.height // 5)).filter(ImageFilter.GaussianBlur(3))
    img = img.rotate(3.0, expand=True, fillcolor=(190, 186, 180))
    img.save(out, quality=40)


if __name__ == "__main__":
    make_pdf(CLEAN_LINES, HERE / "01_clean.pdf")
    make_photo(PHOTO_LINES, HERE / "02_photo.jpg")
    make_png(SPARSE_LINES, HERE / "03_sparse.png")
    make_unreadable(PHOTO_LINES, HERE / "04_unreadable.jpg")
    make_table_pdf(HERE / "05_table_photo.pdf")
    print("wrote:", *[p.name for p in sorted(HERE.glob("0*"))])
