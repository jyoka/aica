"""extract のオフラインテスト。実 API は呼ばない(client を固定応答のスタブに差し替える)。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from resume_draft.schema import PERSONAL_KEYS, ResumeDraft
from resume_intake.extract import extract_draft, file_part, finalize, scrub_pii

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CLEAN = FIXTURES / "resume_clean_response.json"


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


def test_file_part_pdf(tmp_path):
    f = tmp_path / "resume.pdf"
    f.write_bytes(b"%PDF-1.4 dummy")
    part = file_part(f)
    assert part["type"] == "input_file"
    assert part["filename"] == "resume.pdf"
    assert part["file_data"].startswith("data:application/pdf;base64,")


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
