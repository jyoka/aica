"""履歴書下書きの JSON スキーマ(docs/design/resume-draft-schema.md と同じ形)。

別タスク「履歴書のアップロードと読み取り」とスキーマを共有する前提。
personal(氏名など)はモデルには書かせず、常に空欄で envelope に足す(本人が記入する)。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

FORMAT_VERSION = "1.0"

# 出力の日時はユーザーのいる日本時間で書く(created_at・ファイル名とも)
JST = ZoneInfo("Asia/Tokyo")

# 本人記入欄。会話では聞かない・保存しない(コミット 507511f の方針と同じ)。
PERSONAL_KEYS = ("name", "name_kana", "birth_date", "address", "phone", "email")


class Education(BaseModel):
    """学歴(会話に出た範囲だけ。出なければ空)。"""

    period: str = Field(default="", description="例: 2018年4月〜2021年3月。不明なら空文字")
    school: str = Field(description="学校名(本人が言った呼び方のまま)")
    status: str = Field(default="", description="卒業 / 在学中 / 中退 など。不明なら空文字")


class WorkExperience(BaseModel):
    """職歴(アルバイト・パートも1件として扱う)。"""

    period: str = Field(default="", description="例: 2022年6月〜2024年3月。不明なら空文字")
    organization: str = Field(description="勤務先(本人が言った呼び方のまま。例: コンビニ)")
    employment_type: str = Field(default="", description="アルバイト / パート / 正社員 など")
    role: str = Field(default="", description="担当・役割")
    duties: list[str] = Field(default_factory=list, description="やっていたこと")
    achievements: list[str] = Field(default_factory=list, description="工夫・任されたこと・成果(本人談のみ)")


class OtherExperience(BaseModel):
    """職歴以外の経験(部活・委員会・家事・育児・ボランティア・趣味など)。"""

    kind: str = Field(description="部活動 / 委員会 / 家事・育児 / ボランティア / 趣味 など")
    title: str = Field(description="何をしていたか(例: バスケットボール部)")
    description: str = Field(default="", description="内容・続けた期間・役割など(本人談のみ)")


class Certification(BaseModel):
    """資格・免許。"""

    name: str = Field(description="資格・免許の名前")
    acquired: str = Field(default="", description="取得時期。不明なら空文字")


class Preferences(BaseModel):
    """本人の希望。"""

    desired_work: list[str] = Field(default_factory=list, description="やってみたい仕事・職種")
    conditions: list[str] = Field(default_factory=list, description="働ける時間帯・曜日・場所などの条件")
    notes: list[str] = Field(default_factory=list, description="その他、本人が大事にしたいと言ったこと")


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
    unconfirmed: list[str] = Field(default_factory=list, description="会話で確認しきれず、本人に確認したい点")


def envelope(draft: ResumeDraft, *, source: str = "voice-interview") -> dict:
    """保存・受け渡し用の形にくるむ。personal はコードで常に空欄を足す(モデルには書かせない)。"""
    return {
        "format_version": FORMAT_VERSION,
        "source": source,
        "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        "personal": {k: "" for k in PERSONAL_KEYS},
        "resume": draft.model_dump(),
    }
