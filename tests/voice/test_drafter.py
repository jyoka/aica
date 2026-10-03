"""台本(文字起こし)→ 下書きの部分のオフラインテスト。実 API は呼ばない。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from voice_resume.drafter import build_draft, finalize, scrub_pii, transcript_to_text
from voice_resume.schema import PERSONAL_KEYS, Preferences, ResumeDraft, WorkExperience

TRANSCRIPT = [
    {"role": "assistant", "text": "こんにちは。これまでの経験を教えてください。"},
    {"role": "user", "text": "コンビニで1年半アルバイトをしていました。レジと品出しです。"},
    {"role": "assistant", "text": "工夫したことはありますか。"},
    {"role": "user", "text": "発注を任されて、廃棄を減らしました。"},
]


class _FakeResponses:
    def __init__(self, draft: ResumeDraft):
        self._draft = draft
        self.calls: list[dict] = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self._draft)


class _FakeClient:
    def __init__(self, draft: ResumeDraft):
        self.responses = _FakeResponses(draft)


def _sample_draft(**overrides) -> ResumeDraft:
    base = {
        "job_summary": "コンビニで1年半、レジ・品出し・発注を担当した。",
        "self_pr": "発注を任され、廃棄を減らす工夫をした。",
        "work_history": [
            WorkExperience(organization="コンビニ", employment_type="アルバイト",
                           duties=["レジ", "品出し"], achievements=["発注を任され廃棄を削減"])
        ],
        "skills": ["接客"],
        "preferences": Preferences(desired_work=["カフェ"], conditions=["平日夕方と土日"]),
        "unconfirmed": ["勤務先の正式名称"],
    }
    base.update(overrides)
    return ResumeDraft(**base)


def test_transcript_to_text_roles_and_skip_empty():
    text = transcript_to_text(TRANSCRIPT + [{"role": "user", "text": "  "}])
    assert "本人: コンビニで1年半アルバイトをしていました。レジと品出しです。" in text
    assert "アドバイザー: 工夫したことはありますか。" in text
    assert text.count("本人:") == 2


def test_build_draft_passes_facts_only_prompt():
    client = _FakeClient(_sample_draft())
    draft = asyncio.run(build_draft(TRANSCRIPT, client))
    assert draft.work_history[0].organization == "コンビニ"
    call = client.responses.calls[0]
    assert "盛らない・作らない" in call["instructions"]
    assert "性格・感情・思考傾向の推定は一切しない" in call["instructions"]
    assert "コンビニで1年半" in call["input"][0]["content"]
    assert call["text_format"] is ResumeDraft


def test_build_draft_empty_transcript_skips_api():
    client = _FakeClient(_sample_draft())
    draft = asyncio.run(build_draft([], client))
    assert draft == ResumeDraft()
    assert client.responses.calls == []


def test_build_draft_scrubs_pii_from_model_output():
    dirty = _sample_draft(
        self_pr="連絡先は 090-1234-5678 か taro@example.com です。接客が得意です。",
        skills=["接客", "0312345678"],
    )
    draft = asyncio.run(build_draft(TRANSCRIPT, _FakeClient(dirty)))
    assert "090" not in draft.self_pr
    assert "example.com" not in draft.self_pr
    assert "接客が得意です。" in draft.self_pr
    assert draft.skills == ["接客"]  # 電話番号だけの要素は消える


def test_scrub_pii_keeps_normal_text():
    assert scrub_pii("2018年4月〜2021年3月 時給1200円 平日9時から22時") == \
        "2018年4月〜2021年3月 時給1200円 平日9時から22時"
    assert scrub_pii("〒150-0001 に住んでいます") == "に住んでいます"


def test_finalize_envelope_has_blank_personal():
    env = finalize(_sample_draft())
    assert env["format_version"] == "1.1" and env["document_kind"] == "履歴書"  # 1.1 で書類の種類を足した
    assert env["source"] == "voice-interview"
    assert env["created_at"].endswith("+09:00")  # 作成日時は日本時間
    assert set(env["personal"]) == set(PERSONAL_KEYS)
    assert all(v == "" for v in env["personal"].values())
    assert env["resume"]["work_history"][0]["organization"] == "コンビニ"


def test_scrub_pii_removes_the_whole_phone_number():
    # 郵便番号のパターンが電話番号の前半(090-1234)を先に消し、-5678 が残っていた
    for text in ("電話は090-1234-5678です", "電話は03-1234-5678です", "連絡先 090 1234 5678 まで"):
        cleaned = scrub_pii(text)
        assert not any(ch.isdigit() for ch in cleaned), cleaned
