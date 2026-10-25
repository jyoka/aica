"""extract のオフラインテスト。実 API は呼ばない(client を固定応答のスタブに差し替える)。"""

from __future__ import annotations

import asyncio
import io
import json
import time
import tracemalloc
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import FreeText, Link, Rectangle
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)

from resume_draft.schema import PERSONAL_KEYS, ResumeDraft
from resume_intake import config, extract
from resume_intake.extract import (
    extract_bytes,
    extract_draft,
    file_part,
    finalize,
    pdf_text,
    scrub_pii,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLEAN = FIXTURES / "resume_clean_response.json"
# テキストの職歴(架空)。MIN_PAGE_CHARS より長い
WORK_TEXT = "Work history: Miho Systems helpdesk 2022-2025, Miho Electronics sales 2025-now"


class StubClient:
    """responses.parse だけ持つ AsyncOpenAI の代役。固定の ResumeDraft を返す。"""

    def __init__(self, draft: ResumeDraft, usage=None):
        async def parse(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(output_parsed=draft, usage=usage)

        self.calls: list[dict] = []
        self.responses = SimpleNamespace(parse=parse)


def fixture_draft() -> ResumeDraft:
    return ResumeDraft.model_validate(json.loads((CLEAN).read_text(encoding="utf-8")))


def make_pdf(text: str = WORK_TEXT, *, image: tuple[float, float] | None = None, image_as: str = "xobject",
             to_unicode: bytes | None = None, pages: int = 1, password: str | None = None) -> bytes:
    """PDF を pypdf だけで作る。image=(幅, 高さ) なら、左下から灰色の画像を描く(スキャンした紙や貼った画像の代わり)。
    image_as は画像の置き方: xobject(ページの画像)、inline(BI … EI)、form(Form XObject の中の画像)。
    to_unicode はフォントの ToUnicode の CMap(取り出した字を変える)。"""
    w = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    if to_unicode:
        cmap = DecodedStreamObject()
        cmap.set_data(to_unicode)
        font[NameObject("/ToUnicode")] = w._add_object(cmap)
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    ops = f"BT /F1 10 Tf 50 800 Td ({text}) Tj ET".encode()
    if image:
        img = DecodedStreamObject()
        img.set_data(b"\x80")
        img.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                    NameObject("/ColorSpace"): NameObject("/DeviceGray"), NameObject("/BitsPerComponent"): NumberObject(8)})
        draw = f"q {image[0]} 0 0 {image[1]} 0 0 cm ".encode()
        if image_as == "inline":
            draw += b"BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 EI Q "
        elif image_as == "form":
            form = DecodedStreamObject()
            form.set_data(b"/Im0 Do")
            form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                         NameObject("/BBox"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(1), NumberObject(1)]),
                         NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): DictionaryObject(
                             {NameObject("/Im0"): w._add_object(img)})})})
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Fm0"): w._add_object(form)})
            draw += b"/Fm0 Do Q "
        else:
            resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Im0"): w._add_object(img)})
            draw += b"/Im0 Do Q "
        ops = draw + ops
    for _ in range(pages):
        page = w.add_blank_page(595, 842)  # A4(pt)
        content = DecodedStreamObject()
        content.set_data(ops)
        page[NameObject("/Contents")] = w._add_object(content)
        page[NameObject("/Resources")] = resources
    if password:
        w.encrypt(password)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


# 0x20〜0x7E の字を、結合文字(U+0300〜)として取り出させる ToUnicode(ToUnicode の壊れたフォントの代わり)
COMBINING_CMAP = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /Bad def "
                  b"1 begincodespacerange <00> <FF> endcodespacerange 1 beginbfrange <20> <7E> <0300> endbfrange "
                  b"endcmap CMapName currentdict /CMap defineresource pop end end")


def with_annotation(data: bytes, annotation) -> bytes:
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    w.add_annotation(page_number=0, annotation=annotation)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def with_form_field(data: bytes) -> bytes:
    """入力欄(Widget の注釈)を 1 つ足す。値はページの本文のテキストに出てこない。"""
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    widget = DictionaryObject({NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
                               NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): TextStringObject("career"),
                               NameObject("/V"): TextStringObject("Miho Systems 2022-2025"),
                               NameObject("/Rect"): ArrayObject([NumberObject(n) for n in (50, 600, 300, 620)])})
    w.pages[0][NameObject("/Annots")] = ArrayObject([w._add_object(widget)])
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _flate(data: bytes, extra: dict | None = None) -> StreamObject:
    """Flate で圧縮したストリーム(作り込んだ PDF の、小さなファイルで大きく展開される命令の代わり)。"""
    stream = StreamObject()
    stream._data = zlib.compress(data, 9)
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    stream.update(extra or {})
    return stream


def crafted_pdf(page_ops: bytes, *, forms: dict | None = None, self_calling: bool = False,
                cid_font: str | None = None, to_unicode: bool = False) -> bytes:
    """描画の命令と Form XObject を自由に組んだ 1 ページの PDF(命令とストリームは Flate で圧縮する)。
    forms は 名前 → (命令, 入れ子の forms)。self_calling なら、/Fm0 の /Resources に /Fm0 自身を入れる。
    cid_font は /F1 を Type0 のフォントにするときの /Encoding(例: /Identity-H)。to_unicode なら ToUnicode を付ける。"""
    w = PdfWriter()
    if cid_font:
        desc = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/CIDFontType2"),
            NameObject("/BaseFont"): NameObject("/MihoGothic"), NameObject("/DW"): NumberObject(1000),
            NameObject("/CIDSystemInfo"): DictionaryObject({NameObject("/Registry"): TextStringObject("Adobe"),
                                                            NameObject("/Ordering"): TextStringObject("Identity"),
                                                            NameObject("/Supplement"): NumberObject(0)})})
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type0"),
                                 NameObject("/BaseFont"): NameObject("/MihoGothic"),
                                 NameObject("/Encoding"): NameObject(cid_font),
                                 NameObject("/DescendantFonts"): ArrayObject([w._add_object(desc)])})
        if to_unicode:  # GID 0x0020〜0x007E を ASCII の同じ字に引く
            font[NameObject("/ToUnicode")] = w._add_object(_flate(
                b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /Miho def "
                b"1 begincodespacerange <0000> <FFFF> endcodespacerange 1 beginbfrange <0020> <007E> <0020> endbfrange "
                b"endcmap CMapName currentdict /CMap defineresource pop end end"))
    else:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
    fonts = DictionaryObject({NameObject("/F1"): w._add_object(font)})

    def add_forms(spec: dict) -> DictionaryObject:
        xobjects = DictionaryObject()
        for name, (ops, inner) in spec.items():
            resources = DictionaryObject({NameObject("/Font"): fonts})
            if inner:
                resources[NameObject("/XObject")] = add_forms(inner)
            form = _flate(ops, {NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                                NameObject("/BBox"): ArrayObject([NumberObject(n) for n in (0, 0, 595, 842)]),
                                NameObject("/Resources"): resources})
            ref = w._add_object(form)
            if self_calling and name == "/Fm0":
                resources[NameObject("/XObject")] = DictionaryObject({NameObject("/Fm0"): ref})
            xobjects[NameObject(name)] = ref
        return xobjects

    page = w.add_blank_page(595, 842)
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    if forms:
        page[NameObject("/Resources")][NameObject("/XObject")] = add_forms(forms)
    page[NameObject("/Contents")] = w._add_object(_flate(page_ops))
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


TEXT_OPS = f"BT /F1 10 Tf 50 800 Td ({WORK_TEXT}) Tj ET\n".encode()
LINE_OPS = b"0.5 w 50 790 m 545 790 l S"  # 罫線(テキストの無い描画)
# Identity-H で ToUnicode の無いフォントの、サブセットの小さなグリフの番号(ASCII の範囲の字として取り出される)
CID_OPS = ("BT /F1 10 Tf 50 800 Td <" + "".join(f"{g:04X}" for g in range(0x21, 0x7E)) + "> Tj ET").encode()


def _nested_forms(depth: int) -> dict:
    """/Fm が /Fm を呼ぶ入れ子を depth 段。いちばん内側に罫線を描く。"""
    spec = {"/Fm": (LINE_OPS, None)}
    for _ in range(depth - 1):
        spec = {"/Fm": (b"/Fm Do", spec)}
    return spec


def _pdf_text_cost(data: bytes) -> tuple[str | None, float, int]:
    """pdf_text の結果、かかった秒数、Python が確保したメモリのピーク(バイト)。"""
    tracemalloc.start()
    start = time.perf_counter()
    try:
        result = pdf_text(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, time.perf_counter() - start, peak


def test_file_part_pdf(tmp_path):
    f = tmp_path / "みほん花子_履歴書.pdf"  # 架空の氏名。ファイル名には氏名が入りがち
    f.write_bytes(b"%PDF-1.4 dummy")
    part = file_part(f)
    assert part["type"] == "input_file"
    assert part["filename"] == "resume.pdf"  # 本人のファイル名は送らず、形式だけの固定の名前にする
    assert "みほん花子" not in json.dumps(part, ensure_ascii=False)
    assert part["file_data"].startswith("data:application/pdf;base64,")


def test_text_pdf_sends_only_text():
    """テキストのある PDF(JIS 風の表と顔写真の架空のサンプル)は、テキストだけを送り、画像もファイルも送らない。"""
    part = file_part(config.SAMPLES / "05_table_photo.pdf")
    assert part["type"] == "input_text"
    assert set(part) == {"type", "text"}  # file_data / image_url / filename は無い
    text = part["text"]
    assert text.startswith("以下は履歴書の PDF から取り出したテキストです")
    for s in ("みほん情報専門学校 情報処理科 卒業", "株式会社みほんシステム 入社(正社員) ヘルプデスク", "基本情報技術者試験 合格",
              "IT のサポートの仕事を希望します"):
        assert s in text
    # 表の行(年 / 月 / 中身)は 1 行にそろったまま。行末の空白と続く空行は詰める
    assert any(line.split() == ["2022", "4", "株式会社みほんシステム", "入社(正社員)", "ヘルプデスク"] for line in text.splitlines())
    assert "\n\n\n" not in text and not any(line != line.rstrip() for line in text.splitlines())


def test_text_pdf_without_images_sends_only_text():
    assert file_part(config.SAMPLES / "01_clean.pdf")["type"] == "input_text"


@pytest.mark.parametrize(
    ("label", "data"),
    [
        ("壊れた PDF", b"%PDF-1.4 dummy"),
        ("テキストの無いページ(スキャン)", make_pdf("", image=(595, 842))),
        ("短い文字だけのページ(スキャンアプリの透かし)", make_pdf("Scanned with MihoScan")),
        ("OCR のテキストを重ねたスキャン", make_pdf(image=(595, 842))),
        ("OCR のテキストを重ねたスキャン(BI … EI の画像)", make_pdf(image=(595, 842), image_as="inline")),
        ("OCR のテキストを重ねたスキャン(Form XObject の中の画像)", make_pdf(image=(595, 842), image_as="form")),
        ("職歴の表を画像で貼ったページ(A4 の 15%)", make_pdf(image=(595, 126))),
        ("読めない字ばかりのテキスト(ToUnicode の壊れたフォント)", make_pdf(to_unicode=COMBINING_CMAP)),
        ("開くのにパスワードが要る", make_pdf(password="pw")),
        ("閲覧アプリで書き込んだ文字", with_annotation(make_pdf(), FreeText(text="2026年 みほん商店", rect=(50, 700, 300, 720)))),
        ("図形を描いた注釈", with_annotation(make_pdf(), Rectangle(rect=(400, 600, 500, 720)))),
        ("フォームの入力欄", with_form_field(make_pdf())),
        ("ページが多すぎる", make_pdf(pages=11)),
        ("ToUnicode の無い Identity-H のフォント", crafted_pdf(CID_OPS, cid_font="/Identity-H")),
        ("ToUnicode の無い Identity-V のフォント(Form XObject の中)",
         crafted_pdf(TEXT_OPS + b"/Fm0 Do", forms={"/Fm0": (CID_OPS, None)}, cid_font="/Identity-V")),
        ("自分を呼ぶ Form XObject", crafted_pdf(TEXT_OPS + b"/Fm0 Do", forms={"/Fm0": (b"/Fm0 Do", None)},
                                              self_calling=True)),
        ("Form XObject の中のテキスト(pypdf の layout は取り出さない)",
         crafted_pdf(TEXT_OPS + b"/Fm0 Do", forms={"/Fm0": (b"BT /F1 10 Tf 50 700 Td (Self PR: Miho club) Tj ET", None)})),
        ("Form XObject の入れ子が深すぎる", crafted_pdf(TEXT_OPS + b"/Fm Do", forms=_nested_forms(10))),
    ],
)
def test_pdf_without_usable_text_is_sent_as_file(label, data):
    """テキストだけでは中身が欠ける PDF は、今までどおりファイルをそのまま送る。"""
    assert pdf_text(data) is None, label
    part = asyncio.run(_sent_part("r.pdf", data))
    assert part["type"] == "input_file" and part["filename"] == "resume.pdf", label
    assert part["file_data"].startswith("data:application/pdf;base64,")


@pytest.mark.parametrize("cap", ["MAX_PDF_CONTENT_BYTES", "MAX_PDF_TEXT_CHARS"])
def test_pdf_too_large_to_parse_is_sent_as_file(monkeypatch, cap):
    """描画の命令やテキストが大きすぎる PDF は、手元で解析しきらず、ファイルをそのまま送る(作り込んだ PDF の対策)。"""
    assert pdf_text(make_pdf()) == WORK_TEXT
    monkeypatch.setattr(extract, cap, 60)
    assert pdf_text(make_pdf()) is None


@pytest.mark.parametrize(
    ("label", "data"),
    [
        # 展開すると 50MB の空白(ファイルは 50KB ほど)。展開しきってから測ると、50MB を超えるメモリを使う
        ("圧縮した巨大な描画の命令", crafted_pdf(TEXT_OPS + b" " * 50_000_000)),
        ("圧縮した巨大な Form XObject", crafted_pdf(TEXT_OPS + b"/Fm0 Do", forms={"/Fm0": (TEXT_OPS * 120_000, None)})),
        # 1 回の解析は 10KB ほどでも、呼ぶたびに解析し直すので、合計で 4MB を解析することになる
        ("小さな Form XObject を何度も呼ぶ", crafted_pdf(TEXT_OPS + b"/Fm0 Do " * 400,
                                                    forms={"/Fm0": (TEXT_OPS * 120, None)})),
    ],
)
def test_crafted_pdf_is_rejected_cheaply(label, data):
    """作り込んだ PDF は、展開や解析をしきる前に上限で止め、短い時間と少ないメモリで None を返す。"""
    result, seconds, peak = _pdf_text_cost(data)
    assert result is None, label
    assert seconds < 1.0, (label, seconds)
    assert peak < 20_000_000, (label, peak)  # 上限(1MB)の数倍まで


def test_forms_without_text_and_cid_font_with_to_unicode_keep_text_only():
    """テキストの無い Form XObject(罫線など。入れ子 8 段まで)と、ToUnicode のある Identity-H のフォントは、
    テキストだけを送る。"""
    assert pdf_text(crafted_pdf(TEXT_OPS + b"/Fm0 Do", forms={"/Fm0": (LINE_OPS, None)})) == WORK_TEXT
    assert pdf_text(crafted_pdf(TEXT_OPS + b"/Fm Do", forms=_nested_forms(8))) == WORK_TEXT
    text = "".join(chr(g) for g in range(0x21, 0x7E))
    assert pdf_text(crafted_pdf(CID_OPS, cid_font="/Identity-H", to_unicode=True)) == text


def test_owner_password_only_pdf_keeps_text_only():
    """開くのにパスワードが要らない PDF(編集の制限だけ)は、テキストを取り出せるのでテキストだけを送る。"""
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf())))
    w.encrypt(user_password="", owner_password="owner")
    out = io.BytesIO()
    w.write(out)
    assert pdf_text(out.getvalue()) == WORK_TEXT


def test_small_photo_and_links_keep_text_only():
    """顔写真くらいの画像(A4 の 2%)とリンクの注釈は、テキストだけを送るのを止めない。"""
    data = with_annotation(make_pdf(image=(90, 120)), Link(rect=(50, 790, 300, 810), url="https://example.com"))
    assert pdf_text(data) == WORK_TEXT


def test_text_pdf_from_upload_bytes_sends_no_file():
    """アップロード(バイト列)の経路でも、送る内容にファイル(base64)が入らない。"""
    data = (config.SAMPLES / "05_table_photo.pdf").read_bytes()
    client = StubClient(fixture_draft())
    asyncio.run(extract_bytes("鈴木陽翔_履歴書.pdf", data, client))
    sent = json.dumps(client.calls[0]["input"], ensure_ascii=False)
    assert "base64" not in sent and "input_file" not in sent and "鈴木陽翔_履歴書" not in sent
    assert client.calls[0]["store"] is False


async def _sent_part(filename: str, data: bytes) -> dict:
    client = StubClient(fixture_draft())
    await extract_bytes(filename, data, client)
    return client.calls[0]["input"][0]["content"][0]


@pytest.mark.parametrize(("name", "media"), [("a.jpg", "image/jpeg"), ("a.jpeg", "image/jpeg"), ("a.png", "image/png")])
def test_file_part_image(tmp_path, name, media):
    f = tmp_path / name
    f.write_bytes(b"dummy")
    part = file_part(f)
    assert part["type"] == "input_image"
    assert part["image_url"].startswith(f"data:{media};base64,")


def test_file_part_unsupported(tmp_path):
    f = tmp_path / "resume.docx"
    f.write_bytes(b"dummy")
    with pytest.raises(ValueError, match="対応していない"):
        file_part(f)


def test_extract_sends_file_and_returns_draft(tmp_path):
    f = tmp_path / "resume.png"
    f.write_bytes(b"dummy")
    client = StubClient(fixture_draft(), usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    draft, usage = asyncio.run(extract_draft(f, client))
    call = client.calls[0]
    parts = call["input"][0]["content"]
    assert parts[0]["type"] == "input_image"
    assert call["text_format"] is ResumeDraft
    assert call["store"] is False  # OpenAI に応答を残さない
    assert draft.education[0].school == "みほん県立第一高等学校"
    assert usage.input_tokens == 1000
    assert usage.cost_usd == pytest.approx((1000 * 2.0 + 500 * 10.0) / 1_000_000)


def test_extract_scrubs_pii_from_model_output(tmp_path):
    """モデル出力に電話・メール・郵便番号が混ざっても、保存する draft からは消える。"""
    f = tmp_path / "resume.pdf"
    f.write_bytes(b"%PDF dummy")
    draft, _ = asyncio.run(extract_draft(f, StubClient(fixture_draft())))
    dumped = json.dumps(draft.model_dump(), ensure_ascii=False)
    for pii in ("090-0000-1111", "misaki.sato@example.com", "100-0000", "03-1234-5678"):
        assert pii not in dumped
    # PII 以外は残る
    assert "レジ" in dumped
    assert draft.skills == ["接客"]


def test_finalize_personal_always_empty():
    """個人情報の欄(氏名・住所・電話など)は、何を読み取っても常に空で保存される。"""
    result = finalize(fixture_draft())
    assert result["format_version"] == "1.0"
    assert result["source"] == "upload"
    assert set(result["personal"]) == set(PERSONAL_KEYS)
    assert all(v == "" for v in result["personal"].values())
    # created_at は日本時間(+09:00)で書く(docs/architecture/lld.md の 5.5、下書きと同じ)
    assert result["created_at"].endswith("+09:00")


def test_unreadable_returns_empty_draft_with_note():
    """読めないファイルは全項目が空 + unconfirmed に理由、という返し方(プロンプトで指示)。"""
    draft = ResumeDraft(unconfirmed=["画像が不鮮明で読み取れなかった"])
    result = finalize(draft)
    resume = result["resume"]
    assert resume["education"] == [] and resume["work_history"] == []
    assert resume["job_summary"] == "" and resume["self_pr"] == ""
    assert resume["unconfirmed"] == ["画像が不鮮明で読み取れなかった"]


def test_scrub_pii_keeps_structure():
    value = {"a": "電話 090-1234-5678", "b": ["x@example.com", "残る"], "c": 3}
    cleaned = scrub_pii(value)
    assert cleaned == {"a": "電話", "b": ["残る"], "c": 3}
