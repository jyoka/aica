"""履歴書下書きの Markdown 描画のテスト。"""

from __future__ import annotations

from voice_resume.drafter import finalize
from voice_resume.render import to_markdown
from voice_resume.schema import (
    Certification,
    OtherExperience,
    Preferences,
    ResumeDraft,
    WorkExperience,
)


def _env() -> dict:
    return finalize(
        ResumeDraft(
            job_summary="コンビニで1年半、レジ・品出し・発注を担当した。",
            self_pr="発注を任され、廃棄を減らす工夫をした。",
            work_history=[
                WorkExperience(period="2024年〜2025年", organization="コンビニ",
                               employment_type="アルバイト", duties=["レジ"], achievements=["廃棄削減"])
            ],
            other_experience=[OtherExperience(kind="部活動", title="吹奏楽部", description="3年間継続")],
            skills=["接客"],
            certifications=[Certification(name="普通自動車運転免許")],
            preferences=Preferences(desired_work=["カフェ"], conditions=["土日"]),
            unconfirmed=["勤務先の正式名称"],
        )
    )


def test_markdown_sections_and_blank_personal():
    md = to_markdown(_env())
    assert md.startswith("# 履歴書(下書き)")
    assert "- 氏名: ____________" in md  # 個人情報は常に本人記入
    assert "## 職務要約" in md
    assert "- 2024年〜2025年 コンビニ アルバイト" in md
    assert "  - 工夫・成果: 廃棄削減" in md
    assert "部活動: 吹奏楽部(3年間継続)" in md
    assert "- 普通自動車運転免許" in md
    assert "- やってみたい仕事: カフェ" in md
    assert "## 確認したい点(下書きに入れていません)" in md
    assert "必ずご自身で確認・修正" in md


def test_markdown_empty_draft_renders_placeholders():
    md = to_markdown(finalize(ResumeDraft()))
    assert "ご自身で記入してください" in md
    assert "(会話に出なかったため空欄)" in md
    assert "None" not in md
