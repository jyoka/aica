"""Resume Draft の形(docs/architecture/lld.md の 5.5)。読み取り、下書き、dip の保存が同じものを使う。

旧リポジトリ jyoka/aica_ra_a2a_poc の task/29(PR #26)voice-resume/voice_resume/schema.py が元(format_version 1.0)。
format_version 1.1(aica v2 の G)は envelope に任意項目 document_kind(履歴書 / 職務経歴書)を足しただけで、resume の形は同じ。
アップロードしたファイルがどちらの書類かは決めないので、読み取り結果は document_kind を書かない 1.0 のまま出す。
personal(氏名など)はモデルには書かせず、常に空欄で envelope に足す(本人が記入する)。

説明の文(LLM に送るスキーマの一部)は読み取りと下書きで共通。「書いてあるまま」と「本人が言ったまま」の違いは、
それぞれの指示(resume_intake/extract.py の EXTRACT_SYSTEM、voice_resume/prompts.py)に書く。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

# document_kind を書かない形(読み取り結果)と、書く形(下書き)
FORMAT_VERSION = "1.0"
FORMAT_VERSION_WITH_KIND = "1.1"
# created_at・ファイル名の日時はユーザーのいる日本時間で書く
JST = ZoneInfo("Asia/Tokyo")

# 本人記入欄。読み取っても、会話に出ても保存しない(本人が記入する)。
PERSONAL_KEYS = ("name", "name_kana", "birth_date", "address", "phone", "email")

# 書類の種類(envelope の document_kind)。種類の無いもの(format_version 1.0)は履歴書として読む
DOCUMENT_KINDS = ("履歴書", "職務経歴書")
DEFAULT_DOCUMENT_KIND = "履歴書"


class Education(BaseModel):
    """学歴(分かる範囲だけ。無ければ空)。"""

    period: str = Field(default="", description="例: 2018年4月〜2021年3月。不明なら空文字")
    school: str = Field(description="学校名(元の呼び方のまま)")
    status: str = Field(default="", description="卒業 / 在学中 / 中退 など。不明なら空文字")


class WorkExperience(BaseModel):
    """職歴(アルバイト・パートも1件として扱う)。"""

    period: str = Field(default="", description="例: 2022年6月〜2024年3月。不明なら空文字")
    organization: str = Field(description="勤務先(元の呼び方のまま。例: コンビニ)")
    employment_type: str = Field(default="", description="アルバイト / パート / 正社員 など")
    role: str = Field(default="", description="担当・役割")
    duties: list[str] = Field(default_factory=list, description="やっていたこと")
    achievements: list[str] = Field(default_factory=list, description="工夫・任されたこと・成果(元にある事実のみ)")


class OtherExperience(BaseModel):
    """職歴以外の経験(部活・委員会・家事・育児・ボランティア・趣味など)。"""

    kind: str = Field(description="部活動 / 委員会 / 家事・育児 / ボランティア / 趣味 など")
    title: str = Field(description="何をしていたか(例: バスケットボール部)")
    description: str = Field(default="", description="内容・続けた期間・役割など(元にある事実のみ)")


class Certification(BaseModel):
    """資格・免許。"""

    name: str = Field(description="資格・免許の名前")
    acquired: str = Field(default="", description="取得時期。不明なら空文字")


class Preferences(BaseModel):
    """本人の希望。"""

    desired_work: list[str] = Field(default_factory=list, description="やってみたい仕事・職種")
    conditions: list[str] = Field(default_factory=list, description="働ける時間帯・曜日・場所などの条件")
    notes: list[str] = Field(default_factory=list, description="その他、本人が大事にしたいこと")


class ResumeDraft(BaseModel):
    """履歴書下書きの本体(モデルが生成する部分)。"""

    job_summary: str = Field(default="", description="職務要約(2〜3文・事実のみ)")
    self_pr: str = Field(default="", description="自己PR(3〜4文・事実のみ)")
    education: list[Education] = Field(default_factory=list)
    work_history: list[WorkExperience] = Field(default_factory=list)
    other_experience: list[OtherExperience] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list, description="できること・得意なこと")
    certifications: list[Certification] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    unconfirmed: list[str] = Field(default_factory=list, description="確かめきれず、本人に確認したい点")


def envelope(draft: ResumeDraft, *, source: str, document_kind: str | None = None) -> dict:
    """保存・受け渡し用の形にくるむ。personal はコードで常に空欄を足す(モデルには書かせない)。
    document_kind が None なら format_version 1.0 で document_kind を書かない(読み取り結果)。
    書類の種類を渡したら 1.1 で document_kind を書く(下書き)。知らない種類は ValueError。"""
    if document_kind is not None and document_kind not in DOCUMENT_KINDS:
        raise ValueError(f"document_kind は {' / '.join(DOCUMENT_KINDS)} のどれか")
    head = ({"format_version": FORMAT_VERSION} if document_kind is None
            else {"format_version": FORMAT_VERSION_WITH_KIND, "document_kind": document_kind})
    return head | {
        "source": source,
        "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        "personal": {k: "" for k in PERSONAL_KEYS},
        "resume": draft.model_dump(),
    }


def document_kind_of(env: dict | None) -> str:
    """下書きの JSON(または音声相談の結果)の書類の種類。document_kind が無い、知らないものは履歴書。"""
    kind = (env or {}).get("document_kind")
    return kind if kind in DOCUMENT_KINDS else DEFAULT_DOCUMENT_KIND
