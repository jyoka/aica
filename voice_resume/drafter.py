"""文字起こし → 下書き(ResumeDraft)。書類の種類(履歴書 / 職務経歴書)で LLM への指示を変える。

この部分はオフラインでテストする(実 API を呼ばない)。client を外から渡し、
テストでは responses.parse を持つスタブを渡す。
"""

from __future__ import annotations

import re

from voice_resume import config
from voice_resume.prompts import drafter_system, drafter_user
from voice_resume.schema import DEFAULT_DOCUMENT_KIND, ResumeDraft, envelope

# 個人情報のセーフティネット。プロンプトで「書かない」を指示した上で、
# 出力に混ざっても機械的に落とす(電話番号・メール・郵便番号)。
# 電話番号を郵便番号より先に消す(逆だと 090-1234-5678 の前半が郵便番号として食われ、-5678 が残る)
_PII_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"(?<!\d)0\d{1,4}[-()\s]?\d{1,4}[-()\s]?\d{3,4}(?!\d)"),
    re.compile(r"〒?\s*(?<!\d)\d{3}-\d{4}(?!\d)"),
]


def transcript_to_text(transcript: list[dict]) -> str:
    """[{role, text}] を「本人:/アドバイザー:」のテキストに直す。"""
    names = {"user": "本人", "assistant": "アドバイザー"}
    lines = []
    for turn in transcript:
        text = str(turn.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"{names.get(turn.get('role'), turn.get('role', '?'))}: {text}")
    return "\n".join(lines)


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


async def build_draft(transcript: list[dict], client, *, model: str | None = None,
                      document_kind: str = DEFAULT_DOCUMENT_KIND) -> ResumeDraft:
    """文字起こしから ResumeDraft を作る。client は AsyncOpenAI(またはテスト用スタブ)。
    document_kind が職務経歴書なら、職務要約と職歴ごとの成果(本人が言った数字)を中心に書かせる。"""
    text = transcript_to_text(transcript)
    if not text:
        return ResumeDraft()
    resp = await client.responses.parse(
        model=model or config.DRAFT_MODEL,
        instructions=drafter_system(document_kind),
        input=[{"role": "user", "content": drafter_user(text, document_kind)}],
        text_format=ResumeDraft,
        max_output_tokens=4096,
    )
    draft = resp.output_parsed or ResumeDraft()
    return ResumeDraft.model_validate(scrub_pii(draft.model_dump()))


def finalize(draft: ResumeDraft, *, source: str = "voice-interview", document_kind: str = DEFAULT_DOCUMENT_KIND) -> dict:
    """保存用 JSON(envelope)にする。"""
    return envelope(draft, source=source, document_kind=document_kind)
