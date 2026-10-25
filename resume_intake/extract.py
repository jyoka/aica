"""履歴書ファイル(PDF / JPEG / PNG)→ 履歴書下書き(ResumeDraft)。

この部分はオフラインでテストする(実 API を呼ばない)。client を外から渡し、
テストでは responses.parse を持つスタブを渡す(旧リポジトリの voice-resume/drafter.py と同じ作り)。
トークからのアップロードはファイルをディスクに置かず、バイト列のまま extract_bytes で読む。
テキストのある PDF は、ローカルでテキストを取り出してテキストだけを送る(写真などの画像を OpenAI に送らない。
docs/design/privacy.md の 3.2 の案 B)。テキストの無い PDF と画像は、ファイルをそのまま送る。
"""

from __future__ import annotations

import asyncio
import base64
import io
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader, apply_configuration
from pypdf.generic import ContentStream, StreamObject

from resume_draft.pii import scrub_pii
from resume_draft.schema import ResumeDraft, envelope
from resume_intake import config

EXTRACT_SYSTEM = """あなたは履歴書・職務経歴書の読み取り係です。渡されたファイル(履歴書のPDFや写真)、
または PDF から取り出したテキストに書いてあることを、指定されたスキーマの JSON に書き写します。

守ること:
- 書いてあることだけを書き写す。推測・補完・要約での創作はしない。
- 読めない字・不鮮明な箇所・意味が確定できない箇所は、その項目を空(空文字・空配列)にし、
  unconfirmed に「何が読めなかったか」を日本語で1行ずつ入れる。
- ファイル全体が履歴書として読めない(白紙・別の書類・不鮮明すぎる)場合は、全項目を空にし、
  unconfirmed にその旨を1行で入れる(例: 「画像が不鮮明で読み取れなかった」)。
- 氏名・ふりがな・生年月日・年齢・性別・住所・電話番号・メールアドレスは、書いてあっても
  どの項目にも書かない(保存しない決まり)。
- 書いてある場所のとおりに項目へ分ける。欄をまたいで写し直さない
  (例: 自己PRの文を work_history の achievements や skills に入れ直さない)。
- job_summary / self_pr は「職務要約」「自己PR」などの欄が書いてある場合だけ、その欄の文を写す。
  欄が無ければ空文字にする(職歴などから要約を作らない)。
- skills は「特技」「スキル」などの欄に書いてある場合だけ入れる。職歴から推測して作らない。
- 学歴・職歴の年月は書いてあるまま period に写す(例: 2020年4月〜2023年3月)。文字・記号も書いてあるままにする。
- 「志望動機」「本人希望欄」は preferences(desired_work / conditions / notes)に振り分ける。
- 職歴以外の経験(部活・委員会・家事・育児・ボランティア・趣味など)は other_experience に入れる。"""

_MEDIA_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
# OpenAI に渡す PDF のファイル名。本人のファイル名(氏名が入りがち)は送らず、形式だけの固定の名前にする
SENT_PDF_NAME = "resume.pdf"
# テキストだけを送るのは、どのページにもこの字数(空白を除く)以上のテキストがある PDF だけ。
# スキャンした PDF のページ番号や、スキャンアプリの透かしのような短い文字だけのページは、テキストが無いとみなす
MIN_PAGE_CHARS = 50
# ページの面積のこの割合以上を画像が覆う PDF は、テキストがあってもファイルをそのまま送る。
# OCR のテキストを重ねたスキャン(手書きを OCR の文字に置き換えない)と、職歴の表などを画像で貼った PDF のため。
# 顔写真の欄(30×40mm で A4 の 2% ほど)は、この割合より小さい
MAX_PAGE_IMAGE_SHARE = 0.1
# 読めない字(結合文字、私用領域、制御文字、置き換え文字)がこの割合以上なら、テキストとして使えないとみなす。
# ToUnicode の無いフォントの PDF は、字数はあっても意味の無い字が出る
MAX_BAD_CHAR_SHARE = 0.1
# 手元で解析する PDF の大きさの上限。超えたら解析せず、ファイルをそのまま送る(作り込んだ PDF で CPU とメモリを使わせない)。
# 履歴書と職務経歴書は数ページで、1 ページの描画の命令は数十 KB ほど
MAX_PDF_PAGES = 10
# 全ページの描画の命令と Form XObject の、展開した大きさの合計。Form は呼んだ回数だけ数えた合計も、この上限で抑える。
# pypdf が 1 本のストリームを展開する大きさも、この上限で止める(展開しきってから測ると、圧縮した巨大な命令でメモリを使う)
MAX_PDF_CONTENT_BYTES = 1_000_000
MAX_PDF_TEXT_CHARS = 40_000  # 取り出したテキストの合計(行末の空白を詰めた後)
MAX_FORM_DEPTH = 8  # Form XObject の入れ子の深さ
# 取り出したテキストの前に付ける見出し。モデルにファイルではなくテキストだと伝える。
# JIS の様式のような表は、見出しの「年」「月」が列の上にだけあり、行には数字しか残らない(サンプルの比べで
# period が「2017  4」になった)。年と月を付けて書くよう、テキストのときだけ添える
PDF_TEXT_HEADER = (
    "以下は履歴書の PDF から取り出したテキストです(画像は含みません)。表の列は空白で位置をそろえてあります。\n"
    "見出しが「年」「月」の列にある数字は、period と acquired に「2020年4月」のように年と月を付けて書いてください。"
)


@dataclass
class Usage:
    """1回の読み取りのトークン数と費用目安(USD)。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.cost_usd = (
            self.input_tokens * config.PRICE_INPUT_PER_MTOK + self.output_tokens * config.PRICE_OUTPUT_PER_MTOK
        ) / 1_000_000


def media_type(filename: str) -> str:
    """拡張子から MIME タイプ。PDF / JPEG / PNG 以外は ValueError。"""
    suffix = Path(filename).suffix.lower()
    mt = _MEDIA_TYPES.get(suffix)
    if mt is None:
        raise ValueError(f"対応していないファイル形式です: {suffix or '(拡張子なし)'}(PDF / JPEG / PNG のみ)")
    return mt


def pdf_text(data: bytes) -> str | None:
    """PDF のテキストを取り出す。テキストだけを送ってよい PDF でなければ None(ファイルをそのまま送る)。

    None にするのは次のどれかがあるとき。テキストだけでは書いた中身が欠けるか、解析に手間がかかりすぎるため。
    迷うときはファイルをそのまま送る側に倒す(読み取りの質を落とさない)。
    - 読めない(壊れている、開くのにパスワードが要る)
    - MAX_PDF_PAGES より多いページ、MAX_PDF_CONTENT_BYTES より大きい描画の命令、MAX_PDF_TEXT_CHARS より長いテキスト、
      MAX_FORM_DEPTH より深い Form XObject の入れ子、自分を呼ぶ Form XObject
    - ToUnicode の無い Type0 のフォント(Identity-H / Identity-V か埋め込みの CMap)。グリフの番号が意味の無い字になる
    - リンク以外の注釈。閲覧アプリで書き込んだ文字(FreeText)、フォームの入力欄(Widget)、貼り付けた画像(Stamp)、
      手書き(Ink)などで、本文のテキストには出てこない
    - 画像がページの MAX_PAGE_IMAGE_SHARE 以上を覆うページ。Form XObject の中に画像があるページ(面積を数えない)
    - テキストが MIN_PAGE_CHARS に満たないページ、読めない字が MAX_BAD_CHAR_SHARE 以上のページ
    - layout で取り出せない字があるページ。pypdf の layout は Form XObject の中のテキストを取り出さない"""
    cap = MAX_PDF_CONTENT_BYTES
    limits = {key: cap for key in ("zlib_maximum_output_length", "lzw_maximum_output_length",
                                   "run_length_maximum_output_length", "brotli_maximum_output_length",
                                   "jbig2_maximum_output_length", "array_based_stream_maximum_output_length")}
    try:
        with apply_configuration(**limits):  # このスレッドの、この呼び出しの間だけ pypdf の上限を下げる
            reader = PdfReader(io.BytesIO(data))
            if len(reader.pages) > MAX_PDF_PAGES:
                return None
            budget = _Budget(cap)
            form_images = [_scan(page, budget) for page in reader.pages]
            texts = []
            for page, form_image in zip(reader.pages, form_images):
                if any(a.get_object().get("/Subtype") != "/Link" for a in page.get("/Annots") or []):
                    return None
                if form_image:
                    return None
                share, plain = _image_share(page)
                if share >= MAX_PAGE_IMAGE_SHARE:
                    return None
                text = page.extract_text(extraction_mode="layout")
                chars = "".join(text.split())
                if len(chars) < MIN_PAGE_CHARS or _bad_char_share(chars) >= MAX_BAD_CHAR_SHARE:
                    return None
                # layout では取り出せない字(Form XObject の中のテキストなど)があれば、テキストだけでは中身が欠ける
                if Counter("".join(plain.split())) - Counter(chars):
                    return None
                texts.append(_tidy(text))
    except Exception:  # noqa: BLE001  壊れた PDF、パスワード付き、上限を超えたなど。pypdf はいろいろな型の例外を出す
        return None
    joined = "\n\n".join(texts)
    return joined if texts and len(joined) <= MAX_PDF_TEXT_CHARS else None


class _Unfit(Exception):
    """テキストだけを送らない PDF だと、解析の途中で分かった(上限を超えた、読めないフォントがある)。"""


class _Budget:
    """1 つの PDF で展開してよい大きさ(decoded)と、Form を呼んだ回数だけ数えた解析の大きさ(expanded)の残り。"""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.decoded = cap
        self.expanded = cap
        self.seen: set[int] = set()  # 調べ終わった Form XObject(ページをまたいで同じものを数え直さない)
        self.calls: dict[int, tuple[int, Counter]] = {}  # Form XObject → (中身の大きさ, 呼ぶ名前ごとの回数)

    def take(self, kind: str, n: int) -> None:
        left = getattr(self, kind) - n
        if left < 0:
            raise _Unfit
        setattr(self, kind, left)


def _scan(page, budget: _Budget) -> bool:
    """ページの描画の命令と、ページから届く Form XObject(入れ子も、呼ばれていないものも)を、テキストを取り出す前に調べる。
    上限を超えたら、読めないフォントがあれば、Form が自分を呼べば _Unfit。Form の中に画像があれば True。
    展開は pypdf の上限(pdf_text の apply_configuration)で 1 本ずつ止まるので、ここで測る前にメモリを使いきらない。"""
    contents = page.get_contents()
    data = contents.get_data() if contents is not None else b""
    budget.take("decoded", len(data))
    _check_fonts(page)
    has_image = False
    stack = [page]
    while stack:
        for obj in _xobjects(stack.pop()).values():
            if obj.get("/Subtype") != "/Form" or id(obj) in budget.seen:
                continue
            budget.seen.add(id(obj))
            form_data = obj.get_data()
            budget.take("decoded", len(form_data))
            _check_fonts(obj)
            # BI … EI の画像(誤って当たっても、ファイルをそのまま送るだけ)
            has_image |= b"BI" in form_data or any(x.get("/Subtype") == "/Image" for x in _xobjects(obj).values())
            stack.append(obj)
    calls = _do_calls(contents) if contents is not None else Counter()
    budget.take("expanded", _expanded(len(data), calls, _xobjects(page), page.pdf, budget))
    return has_image


def _expanded(size: int, calls: Counter, xobjects: dict, reader, budget: _Budget, depth: int = 0,
              calling: frozenset = frozenset()) -> int:
    """描画の命令を解析する大きさ。命令そのものの大きさ size に、呼ぶ Form XObject の中身を呼んだ回数だけ足す
    (pypdf は呼ぶたびに解析し直す)。上限を超えたら、入れ子が深すぎたら、Form が自分を呼べば _Unfit。"""
    if depth > MAX_FORM_DEPTH:
        raise _Unfit
    for name, n in calls.items():
        form = xobjects.get(name)
        if form is None or form.get("/Subtype") != "/Form":
            continue
        if id(form) in calling:
            raise _Unfit
        if id(form) not in budget.calls:  # 同じ Form は 1 回だけ解析する(展開した大きさは _scan が decoded で数えた)
            stream = ContentStream(form, reader)
            budget.calls[id(form)] = (len(stream.get_data()), _do_calls(stream))
        form_size, form_calls = budget.calls[id(form)]
        inner = _xobjects(form) if "/Resources" in form else xobjects  # /Resources の無い Form は呼んだ側のものを使う
        size += n * _expanded(form_size, form_calls, inner, reader, budget, depth + 1, calling | {id(form)})
        if size > budget.cap:
            raise _Unfit
    return size


def _do_calls(stream: ContentStream) -> Counter:
    """描画の命令が Do(画像か Form XObject を描く)で呼ぶ名前ごとの回数。テキストを取り出すときと同じ pypdf の字句の解析で
    数えるので、名前と Do の間のコメントや空白の字、#xx で書いた名前も、pypdf が呼ぶとおりに数える。"""
    return Counter(args[0] for args, op in stream.operations if op == b"Do" and args)


def _check_fonts(owner) -> None:
    """ページか Form XObject のフォントに、ToUnicode の無い Type0 のフォントがあれば _Unfit。
    Identity-H / Identity-V や埋め込みの CMap では、pypdf はグリフの番号をそのまま字にする(ASCII や別の漢字になり、
    読めない字の割合では見分けられない)。UniJIS-UCS2-H のような Unicode に引ける CMap は、そのまま使う。"""
    resources = owner.get("/Resources")
    fonts = resources.get_object().get("/Font") if resources is not None else None
    for font in (fonts.get_object().values() if fonts is not None else ()):
        font = font.get_object()
        if font.get("/Subtype") != "/Type0" or "/ToUnicode" in font:
            continue
        encoding = font.get("/Encoding")
        encoding = encoding.get_object() if encoding is not None else None
        if encoding is None or encoding in ("/Identity-H", "/Identity-V") or isinstance(encoding, StreamObject):
            raise _Unfit


def _image_share(page) -> tuple[float, str]:
    """ページに描いた画像が覆う面積の、ページ(CropBox)の面積に対する割合(重なりは数えたまま)と、
    そのとき取り出したテキスト(layout ではない取り出し方。Form XObject の中のテキストも入る)。
    Form XObject の中の画像は _scan が見る。"""
    images = {name for name, obj in _xobjects(page).items() if obj.get("/Subtype") == "/Image"}
    area = 0.0

    def visit(op, args, cm, tm):  # 画像は 1×1 の正方形を cm で広げて描かれる。BI … EI の画像も同じ
        nonlocal area
        if (op == b"Do" and args and args[0] in images) or op == b"INLINE IMAGE":
            area += abs(cm[0] * cm[3] - cm[1] * cm[2])

    plain = page.extract_text(visitor_operand_before=visit)
    page_area = float(page.cropbox.width) * float(page.cropbox.height)
    return (area / page_area if page_area else 1.0), plain


def _xobjects(owner) -> dict:
    """ページか Form XObject の /Resources の /XObject を、名前 → 中身の dict にする。"""
    resources = owner.get("/Resources")
    xobjects = resources.get_object().get("/XObject") if resources is not None else None
    if xobjects is None:
        return {}
    return {name: obj.get_object() for name, obj in xobjects.get_object().items()}


def _bad_char_share(chars: str) -> float:
    """読めない字(結合文字、私用領域、制御文字、未割り当て、置き換え文字)の割合。"""
    bad = sum(1 for ch in chars if ch == "\ufffd" or unicodedata.category(ch)[0] in "MC")
    return bad / len(chars) if chars else 1.0


def _tidy(text: str) -> str:
    """layout で取り出したテキストの、行末の空白と続く空行を詰める(表の列の位置は行頭の空白で残す)。"""
    lines = [line.rstrip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip("\n")


def content_part(filename: str, data: bytes, *, pdf_text_only: bool = True) -> dict:
    """ファイルの中身を Responses API の content part(input_text / input_file / input_image)にする。
    テキストのある PDF はテキストだけ(input_text)にし、画像を送らない。pdf_text_only=False は、
    前の送り方(PDF をそのまま送る)と読み取りの質を比べるときだけに使う(run_samples の --compare)。
    filename は形式を決めるのにだけ使い、送らない(PDF の filename は SENT_PDF_NAME)。"""
    mt = media_type(filename)
    if mt == "application/pdf" and pdf_text_only:
        text = pdf_text(data)
        if text is not None:
            return {"type": "input_text", "text": f"{PDF_TEXT_HEADER}\n\n{text}"}
    b64 = base64.b64encode(data).decode("ascii")
    if mt == "application/pdf":
        return {"type": "input_file", "filename": SENT_PDF_NAME, "file_data": f"data:{mt};base64,{b64}"}
    return {"type": "input_image", "image_url": f"data:{mt};base64,{b64}"}


def file_part(path: Path, *, pdf_text_only: bool = True) -> dict:
    """入力ファイルを Responses API の content part(input_text / input_file / input_image)にする。"""
    media_type(path.name)  # 読む前に形式を確かめる
    return content_part(path.name, path.read_bytes(), pdf_text_only=pdf_text_only)


async def extract_draft(path: Path, client, *, model: str | None = None,
                        pdf_text_only: bool = True) -> tuple[ResumeDraft, Usage]:
    """履歴書ファイルから ResumeDraft を作る。client は AsyncOpenAI(またはテスト用スタブ)。"""
    return await _extract(file_part(path, pdf_text_only=pdf_text_only), client, model=model)


async def extract_bytes(filename: str, data: bytes, client, *, model: str | None = None) -> tuple[ResumeDraft, Usage]:
    """アップロードされたファイルの中身(バイト列)から ResumeDraft を作る。ファイルはディスクに置かない。
    PDF の解析は CPU を使うので、イベントループを止めないよう別のスレッドで行う。"""
    part = await asyncio.to_thread(content_part, filename, data)
    return await _extract(part, client, model=model)


async def _extract(part: dict, client, *, model: str | None) -> tuple[ResumeDraft, Usage]:
    content = [
        part,
        {"type": "input_text", "text": "この履歴書に書いてあることをスキーマどおりの JSON に書き写してください。"},
    ]
    resp = await client.responses.parse(
        model=model or config.MODEL,
        instructions=EXTRACT_SYSTEM,
        input=[{"role": "user", "content": content}],
        text_format=ResumeDraft,
        max_output_tokens=4096,
        store=False,  # OpenAI に応答を残さない(docs/design/privacy.md の 3.2)
    )
    draft = resp.output_parsed or ResumeDraft()
    draft = ResumeDraft.model_validate(scrub_pii(draft.model_dump()))
    usage = getattr(resp, "usage", None)
    return draft, Usage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


def finalize(draft: ResumeDraft) -> dict:
    """保存用 JSON(envelope)にする。source は upload 固定。書類の種類は決めないので format_version 1.0。"""
    return envelope(draft, source="upload")
