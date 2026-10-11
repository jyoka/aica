"""履歴書下書きの Markdown 描画のテスト。"""

from __future__ import annotations

from resume_draft.schema import (
    Certification,
    Education,
    OtherExperience,
    Preferences,
    ResumeDraft,
    WorkExperience,
)
from voice_resume.drafter import finalize
from voice_resume.render import to_markdown


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


def test_a_1_0_draft_without_document_kind_still_renders_as_a_resume():
    """format_version 1.0 の下書き(document_kind なし。履歴書のアップロードの読み取り結果も)は、これまでどおり履歴書。"""
    env = _env()
    old = {k: v for k, v in env.items() if k != "document_kind"} | {"format_version": "1.0"}
    assert to_markdown(old) == to_markdown(env)
    assert to_markdown(old).startswith("# 履歴書(下書き)")


def test_career_sheet_puts_summary_and_achievements_first_and_leaves_out_education():
    """職務経歴書は 職務要約 → 職務経歴(業務内容・成果)→ 活かせる経験・知識・技術 → 資格 → 自己PR の並び。
    学歴と、氏名以外の個人情報の欄は出さない。"""
    env = finalize(ResumeDraft(
        job_summary="法人営業を6年経験した。",
        self_pr="担当顧客の課題を聞き取り、提案につなげてきた。",
        education=[Education(school="みほん大学", status="卒業")],
        work_history=[WorkExperience(period="2020年4月〜現在", organization="みほん商事", employment_type="正社員",
                                     role="法人営業", duties=["提案営業"], achievements=["担当売上を2年で1.3倍にした"])],
        skills=["提案営業"],
        certifications=[Certification(name="普通自動車運転免許")],
        unconfirmed=["チームの人数"],
    ), document_kind="職務経歴書")
    md = to_markdown(env)
    assert md.startswith("# 職務経歴書(下書き)") and "- 氏名: ____________" in md
    heads = ["## 職務要約", "## 職務経歴", "### 2020年4月〜現在 みほん商事 正社員 法人営業", "- 業務内容:", "- 成果:",
             "## 活かせる経験・知識・技術", "## 資格・免許", "## 自己PR", "## 確認したい点"]
    assert [md.index(h) for h in heads] == sorted(md.index(h) for h in heads)
    assert "  - 担当売上を2年で1.3倍にした" in md
    assert "学歴" not in md and "みほん大学" not in md and "生年月日" not in md and "電話番号" not in md
    # 職務要約が無くても見出しは出し、空欄と分かるようにする
    assert "会話に出なかったため空欄" in to_markdown(finalize(ResumeDraft(), document_kind="職務経歴書"))
