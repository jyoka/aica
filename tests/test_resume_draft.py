"""Resume Draft の契約(形、envelope、電話・メール・郵便番号を消す決まり)のテスト。実 API は呼ばない。

アップロードの読み取り(resume_intake)と音声の下書き(voice_resume)の 2 つの経路に、LLM の偽物から同じ下書きを返させ、
同じ形で出て、個人情報が消えることを確かめる。dip に保存するとき(save_resume_upload)も同じ決まりで消す。
docs/design/consolidate-resume-draft-and-event-turn.md の「振る舞いを変えないことの確かめ方」。
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai.lib._pydantic import to_strict_json_schema

from dip_mcp import logic
from dip_mcp.data import State
from resume_draft.pii import PII_VALUE, SCRUB_PATTERNS, fold_pii
from resume_draft.schema import document_kind_of
from resume_intake import extract
from voice_resume import drafter

SCHEMA = Path(__file__).resolve().parent / "fixtures" / "resume_draft_schema.json"
PERSONAL_KEYS = ["name", "name_kana", "birth_date", "address", "phone", "email"]
TRANSCRIPT = [{"role": "user", "text": "コンビニで1年半アルバイトをしていました。"}]

# 今の決まりで消える(または残る)文字列と、scrub_pii の結果
KEPT = [
    ("電話 090-1234-5678", "電話"),
    ("x@example.com", ""),
    ("残る", "残る"),
    ("2018年4月〜2021年3月 時給1200円 平日9時から22時", "2018年4月〜2021年3月 時給1200円 平日9時から22時"),
    ("〒150-0001 に住んでいます", "に住んでいます"),
    ("電話は090-1234-5678です", "電話はです"),
    ("電話は03-1234-5678です", "電話はです"),
    ("連絡先 090 1234 5678 まで", "連絡先  まで"),
    ("連絡先 090-1234-5678", "連絡先"),
    ("090 12345678", ""),
    ("連絡は(03)1234-5678に", "連絡はに"),
    ("+81 90-1234-5678", ""),
    ("090.1234.5678 まで", "まで"),
    ("x@example", ""),
    ("連絡はsample@example.comで新宿のカフェ", "連絡はで新宿のカフェ"),
    ("コーヒーとスーパーのレジ　週3日", "コーヒーとスーパーのレジ　週3日"),
]
# 全角の数字、長音記号のハイフン、全角の ＠ と ． で書いたもの。fold_pii でそろえてから消す
FULLWIDTH = [
    ("連絡先 ０９０－１２３４－５６７８", "連絡先"),
    ("連絡先 090ー1234ー5678", "連絡先"),
    ("ｔａｒｏ＠example．com", ""),
    ("〒１５０－０００１", ""),
]
# 全角の数字(0 で始まる)のすぐ後ろに半角の電話番号があるもの。半角の電話番号は丸ごと消える
MIXED = [
    ("内線 ０１２３ 090-1234-5678", "内線"),
    ("No.０１ 090-1234-5678", "No."),
    ("〒１５０－０００１ 03-1234-5678", "〒１５０－"),
]


class StubClient:
    """responses.parse だけ持つ AsyncOpenAI の代役。固定の下書きを返し、渡された text_format を覚える。"""

    def __init__(self, draft):
        async def parse(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(output_parsed=draft, usage=None)

        self.calls: list[dict] = []
        self.responses = SimpleNamespace(parse=parse)


def _models() -> tuple[type, type]:
    """2 つの経路が LLM に渡す下書きの型(読み取り、下書き)。"""
    upload, voice = StubClient(None), StubClient(None)
    asyncio.run(extract.extract_bytes("r.pdf", b"%PDF-1.4 dummy", upload))
    asyncio.run(drafter.build_draft(TRANSCRIPT, voice))
    return upload.calls[0]["text_format"], voice.calls[0]["text_format"]


def _raw(text: str) -> dict:
    """どの文字列の項目にも text が入った、LLM の出力(下書きの dict)。"""
    return {
        "job_summary": f"コンビニで1年半働いた。{text}",
        "self_pr": text,
        "education": [{"period": "2018年4月〜2021年3月", "school": f"みほん高校 {text}", "status": "卒業"}],
        "work_history": [{"period": "2024年6月〜2026年1月", "organization": "コンビニ", "employment_type": "アルバイト",
                          "role": "レジ", "duties": ["レジ", text], "achievements": [f"発注を任された {text}"]}],
        "other_experience": [{"kind": "部活動", "title": "吹奏楽部", "description": text}],
        "skills": ["接客", text],
        "certifications": [{"name": "普通自動車運転免許", "acquired": text}],
        "preferences": {"desired_work": ["カフェ"], "conditions": [text], "notes": [text]},
        "unconfirmed": [text],
    }


def _both(raw: dict, document_kind: str = "履歴書") -> tuple[dict, dict]:
    """同じ LLM の出力を、読み取りと下書きの経路に通した envelope。"""
    upload_model, voice_model = _models()
    upload, _ = asyncio.run(extract.extract_bytes("r.pdf", b"%PDF-1.4 dummy",
                                                  StubClient(upload_model.model_validate(raw))))
    voice = asyncio.run(drafter.build_draft(TRANSCRIPT, StubClient(voice_model.model_validate(raw)),
                                            document_kind=document_kind))
    return extract.finalize(upload), drafter.finalize(voice, document_kind=document_kind)


def _strip_descriptions(schema):
    if isinstance(schema, dict):
        return {k: _strip_descriptions(v) for k, v in schema.items() if not (k == "description" and isinstance(v, str))}
    if isinstance(schema, list):
        return [_strip_descriptions(v) for v in schema]
    return schema


# ----------------------------- 形 -----------------------------


def test_both_paths_send_the_same_schema_to_the_llm():
    """2 つの経路が LLM に送るスキーマは、説明の文を除くと同じで、固定値と同じ(項目、型、既定値)。"""
    expected = json.loads(SCHEMA.read_text(encoding="utf-8"))
    for model in _models():
        assert _strip_descriptions(to_strict_json_schema(model)) == expected


def test_empty_draft_is_the_same_on_both_paths():
    upload, voice = _both({})
    assert upload["resume"] == voice["resume"] == {
        "job_summary": "", "self_pr": "", "education": [], "work_history": [], "other_experience": [], "skills": [],
        "certifications": [], "preferences": {"desired_work": [], "conditions": [], "notes": []}, "unconfirmed": []}


def test_envelope_shape():
    """アップロードは 1.0 で document_kind なし、下書きは 1.1 で document_kind あり。personal は常に空。"""
    upload, voice = _both(_raw("連絡先 090-1234-5678"), document_kind="職務経歴書")
    assert list(upload) == ["format_version", "source", "created_at", "personal", "resume"]
    assert list(voice) == ["format_version", "document_kind", "source", "created_at", "personal", "resume"]
    assert (upload["format_version"], upload["source"]) == ("1.0", "upload")
    assert (voice["format_version"], voice["document_kind"], voice["source"]) == ("1.1", "職務経歴書", "voice-interview")
    for env in (upload, voice):
        assert env["created_at"].endswith("+09:00")
        assert env["personal"] == dict.fromkeys(PERSONAL_KEYS, "")
    assert drafter.finalize(drafter.ResumeDraft())["document_kind"] == "履歴書"


def test_unknown_document_kind_is_rejected():
    with pytest.raises(ValueError, match="document_kind"):
        drafter.finalize(drafter.ResumeDraft(), document_kind="自己紹介書")


@pytest.mark.parametrize(("env", "kind"), [
    (None, "履歴書"), ({}, "履歴書"), ({"document_kind": "自己紹介書"}, "履歴書"),
    ({"document_kind": "履歴書"}, "履歴書"), ({"document_kind": "職務経歴書"}, "職務経歴書")])
def test_document_kind_of(env, kind):
    """無い、知らない書類の種類は履歴書として読む(format_version 1.0 の下書き)。"""
    assert document_kind_of(env) == kind


# ----------------------------- 電話、メール、郵便番号を消す -----------------------------


@pytest.mark.parametrize(("text", "cleaned"), KEPT)
def test_scrub_pii_today(text, cleaned):
    assert extract.scrub_pii(text) == drafter.scrub_pii(text) == cleaned


@pytest.mark.parametrize(("text", "cleaned"), FULLWIDTH)
def test_scrub_pii_fullwidth(text, cleaned):
    assert extract.scrub_pii(text) == drafter.scrub_pii(text) == cleaned


@pytest.mark.parametrize(("text", "cleaned"), MIXED)
def test_scrub_pii_mixed_width(text, cleaned):
    assert extract.scrub_pii(text) == drafter.scrub_pii(text) == cleaned


def _scrub_without_fold(text: str) -> str:
    """fold_pii を使わない、元の文字列だけで照合する消し方(統合前の 2 つの経路の決まり)。"""
    for pat in SCRUB_PATTERNS:
        text = pat.sub("", text)
    return text.strip()


def test_scrub_pii_never_keeps_more_than_without_fold():
    """全角をそろえて照合するようにしても、元の文字列だけで照合したときより残るものは増えない。
    fold_pii で変わらない文字列では、結果は前と同じ。"""
    rng = random.Random(0)
    chars = "0123456789-() @.a〒ー０－"
    for _ in range(20000):
        text = "".join(rng.choice(chars) for _ in range(rng.randint(1, 25)))
        before, after = _scrub_without_fold(text), extract.scrub_pii(text)
        assert len(after) <= len(before), text
        if fold_pii(text) == text:
            assert after == before, text


def test_scrub_pii_keeps_structure():
    value = {"a": "電話 090-1234-5678", "b": ["x@example.com", "残る"], "c": 3}
    assert extract.scrub_pii(value) == drafter.scrub_pii(value) == {"a": "電話", "b": ["残る"], "c": 3}


@pytest.mark.parametrize("text", [t for t, _ in KEPT + FULLWIDTH if not t.startswith("〒１")])
def test_scrub_pii_removes_what_has_pii_finds(text):
    """fold_pii した文字の中で PII_VALUE に当たる範囲は、scrub_pii で消える(dip の決まりより弱くない)。
    郵便番号は PII_VALUE の対象外なので、全角の郵便番号は test_scrub_pii_fullwidth だけで見る。"""
    spans = [m.span() for m in PII_VALUE.finditer(fold_pii(text))]
    for scrub in (extract.scrub_pii, drafter.scrub_pii):
        cleaned = scrub(text)
        assert not any(text[a:b] in cleaned for a, b in spans)
        assert PII_VALUE.search(fold_pii(cleaned)) is None  # 一部だけ残って、まだ電話番号やメールに見えることもない


@pytest.mark.parametrize("text", [t for t, _ in KEPT + FULLWIDTH if t != "残る"])
def test_both_paths_scrub_the_same_way(text):
    """同じ LLM の出力から、2 つの経路は同じ下書きを出し、個人情報はどの項目にも残らない。"""
    upload, voice = _both(_raw(text))
    assert upload["resume"] == voice["resume"]
    resume = upload["resume"]
    cleaned = dict(KEPT + FULLWIDTH)[text]
    assert resume["self_pr"] == cleaned
    assert resume["skills"] == ["接客", *([cleaned] if cleaned else [])]  # 空になった要素はリストから落とす
    assert resume["work_history"][0]["organization"] == "コンビニ"


def test_save_resume_upload_scrubs_again(tmp_path):
    """読み取りをすり抜けた値が来ても、dip に保存するときにもう一度消す。"""
    state = State(tmp_path / "state.json")
    leaked = {"format_version": "1.0", "source": "upload", "created_at": "2026-09-23T12:00:00+09:00",
              "personal": {"phone": "090-1234-5678"}, "resume": _raw("連絡先 090-1234-5678")}
    rec = logic.save_resume_upload(state, "w-1111111111111111:sim-new", leaked)
    assert rec["personal"] == dict.fromkeys(PERSONAL_KEYS, "")
    assert "090" not in json.dumps(state.user("w-1111111111111111:sim-new"), ensure_ascii=False)
    assert rec["resume"]["self_pr"] == "連絡先"


def test_save_resume_upload_scrubs_fullwidth_phone(tmp_path):
    state = State(tmp_path / "state.json")
    leaked = {"format_version": "1.0", "resume": _raw("連絡先 ０９０－１２３４－５６７８")}
    logic.save_resume_upload(state, "w-1111111111111111:sim-new", leaked)
    assert "１２３４" not in json.dumps(state.user("w-1111111111111111:sim-new"), ensure_ascii=False)
