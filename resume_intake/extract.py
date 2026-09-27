"""履歴書ファイル(PDF / JPEG / PNG)→ 履歴書下書き(ResumeDraft)。

この部分はオフラインでテストする(実 API を呼ばない)。client を外から渡し、
テストでは responses.parse を持つスタブを渡す(旧リポジトリの voice-resume/drafter.py と同じ作り)。
トークからのアップロードはファイルをディスクに置かず、バイト列のまま extract_bytes で読む。
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path

from resume_intake import config
from resume_intake.schema import ResumeDraft, envelope

EXTRACT_SYSTEM = """あなたは履歴書・職務経歴書の読み取り係です。渡されたファイル(履歴書のPDFや写真)に
書いてあることを、指定されたスキーマの JSON に書き写します。

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

# 個人情報のセーフティネット。プロンプトで「書かない」を指示した上で、
# 出力に混ざっても機械的に落とす(電話番号・メール・郵便番号)。voice-resume/drafter.py が元。
# 電話番号を郵便番号より先に消す(逆だと 090-1234-5678 の前半が郵便番号として食われ、-5678 が残る)。
_PII_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"(?<!\d)0\d{1,4}[-()\s]?\d{1,4}[-()\s]?\d{3,4}(?!\d)"),
    re.compile(r"〒?\s*(?<!\d)\d{3}-\d{4}(?!\d)"),
]

_MEDIA_TYPES = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


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


def content_part(filename: str, data: bytes) -> dict:
    """ファイルの中身を Responses API の content part(input_file / input_image)にする。"""
    mt = media_type(filename)
    b64 = base64.b64encode(data).decode("ascii")
    if mt == "application/pdf":
        return {"type": "input_file", "filename": Path(filename).name, "file_data": f"data:{mt};base64,{b64}"}
    return {"type": "input_image", "image_url": f"data:{mt};base64,{b64}"}


def file_part(path: Path) -> dict:
    """入力ファイルを Responses API の content part(input_file / input_image)にする。"""
    media_type(path.name)  # 読む前に形式を確かめる
    return content_part(path.name, path.read_bytes())


def scrub_pii(value):
    """文字列・リスト・辞書の中の電話番号/メール/郵便番号を機械的に消す。"""
    if isinstance(value, str):
        for pat in _PII_PATTERNS:
            value = pat.sub("", value)
        return value.strip()
    if isinstance(value, list):
        cleaned = [scrub_pii(v) for v in value]
        return [v for v in cleaned if v != ""]
    if isinstance(value, dict):
        return {k: scrub_pii(v) for k, v in value.items()}
    return value


async def extract_draft(path: Path, client, *, model: str | None = None) -> tuple[ResumeDraft, Usage]:
    """履歴書ファイルから ResumeDraft を作る。client は AsyncOpenAI(またはテスト用スタブ)。"""
    return await _extract(file_part(path), client, model=model)


async def extract_bytes(filename: str, data: bytes, client, *, model: str | None = None) -> tuple[ResumeDraft, Usage]:
    """アップロードされたファイルの中身(バイト列)から ResumeDraft を作る。ファイルはディスクに置かない。"""
    return await _extract(content_part(filename, data), client, model=model)


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
    )
    draft = resp.output_parsed or ResumeDraft()
    draft = ResumeDraft.model_validate(scrub_pii(draft.model_dump()))
    usage = getattr(resp, "usage", None)
    return draft, Usage(
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


def finalize(draft: ResumeDraft) -> dict:
    """保存用 JSON(envelope)にする。source は upload 固定。"""
    return envelope(draft, source="upload")
