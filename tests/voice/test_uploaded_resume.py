"""トークでアップロードした履歴書を、音声相談が踏まえて始めることのテスト。実 API なし。

トークで履歴書をアップロード → dip の状態に resume_upload が保存される(task 63)→ 同じユーザーが
「音声で相談」に入る → 音声ワーカーは面談の前に dip の要約(本物の MCP サーバー、stdio)を読み、
アドバイザーの指示と最初の挨拶に履歴書の要点が入る。別のユーザーや要約が読めないときは従来どおり始める。
"""

from __future__ import annotations

import asyncio

from dip_mcp import logic
from dip_mcp.data import State
from tests.test_resume_upload import PII, clean_draft
from voice_resume.advisor import ResumeAdvisor
from voice_resume.agent import prepare_advisor
from voice_resume.dip import DipMCP
from voice_resume.prompts import (
    RESUME_NOTE_MAX_CHARS,
    greeting_instructions,
    uploaded_resume_note,
)

U1 = "w-1111111111111111:sim-new"  # 履歴書をアップロードしてから音声で相談するブラウザ
U2 = "w-2222222222222222:sim-new"  # 履歴書を出していない別のブラウザ


def _start_interviews(tmp_path, monkeypatch, *users):
    """トークで U1 が履歴書をアップロードした状態で、音声ワーカーの面談開始処理を各ユーザーで走らせる。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))  # dip の子プロセスも同じ状態を使う
    state = State(tmp_path / "state.json")
    logic.save_resume_upload(state, U1, {"format_version": "1.0", "resume": clean_draft().model_dump()})

    async def run():
        dip = DipMCP()
        await dip.start()
        try:
            return [await prepare_advisor(dip, u) for u in users]
        finally:
            await dip.aclose()

    return asyncio.run(run())


def _saved_resume(tmp_path) -> dict:
    """トークのアップロードと同じ保存(dip_mcp.logic.save_resume_upload)を通した、要約の uploaded_resume。"""
    state = State(tmp_path / "resume-state.json")
    logic.save_resume_upload(state, U1, {"format_version": "1.0", "resume": clean_draft().model_dump()})
    return logic.uploaded_resume(state.user(U1))


def test_voice_interview_starts_from_the_uploaded_resume(tmp_path, monkeypatch):
    (advisor, greeting), (other, other_greeting) = _start_interviews(tmp_path, monkeypatch, U1, U2)

    # 履歴書の要点(勤め先・期間・やっていたこと・スキル)がアドバイザーの指示に入る
    for fact in ("コンビニみほん駅前店", "2022年5月〜2024年12月", "レジ", "品出し", "接客"):
        assert fact in advisor.instructions, fact
    # チャット側 Prompt と同じ決まり: 聞き直さず確認形で使う・読み取れなかった点は1問で確かめる
    assert "聞き直さない" in advisor.instructions and "確認形" in advisor.instructions
    # 最初の挨拶で、履歴書を見たことに確認形で1回触れる
    assert "履歴書" in greeting and "確認形で1回" in greeting and "コンビニみほん駅前店" in greeting
    # 個人情報(読み取り前の連絡先など)は指示にも挨拶にも入らない
    assert not any(p in advisor.instructions + greeting for p in PII)

    # 履歴書を出していない別ユーザーは従来どおり(希望も履歴書も無い指示・挨拶)
    assert other.instructions == ResumeAdvisor().instructions
    assert other_greeting == greeting_instructions()


def test_summary_failure_or_no_user_starts_as_before():
    class BrokenDip:
        async def get_user_conversation_summary(self, user_id):
            raise RuntimeError("dip down")

    for dip, user in ((BrokenDip(), U1), (None, U1), (BrokenDip(), None)):
        advisor, greeting = asyncio.run(prepare_advisor(dip, user))
        assert advisor.instructions == ResumeAdvisor().instructions
        assert greeting == greeting_instructions()


def test_resume_note_is_bounded_and_never_carries_personal_info(tmp_path):
    resume = _saved_resume(tmp_path)
    # 連絡先は dip に保存する時点で消えている。要約に personal などが紛れ込んでも(本来は無い)、
    # 決まった項目だけを拾うので指示に入らない
    leaked = {**resume, "personal": {"name": "佐藤 美咲", "phone": "090-0000-1111"}, "name": "佐藤 美咲"}
    assert not any(p in uploaded_resume_note(leaked) for p in (*PII, "佐藤"))

    # 履歴書が無ければ何も足さない
    assert uploaded_resume_note(None) == "" and uploaded_resume_note({"uploaded": "9/23"}) == ""


def test_resume_and_stated_preferences_together(tmp_path):
    """希望と履歴書が両方あるとき: 挨拶は履歴書に確認形で触れ、最初の1問は前に話した希望の確認にする。
    希望が食い違えば、本人が会話で話した希望を優先する。"""
    stated = {"job_type": {"value": "カフェ", "saved": "9/23"}}
    resume = _saved_resume(tmp_path)
    advisor = ResumeAdvisor(stated_preferences=stated, uploaded_resume=resume)
    assert "カフェ" in advisor.instructions and "コンビニみほん駅前店" in advisor.instructions
    assert "会話で話した希望を優先" in advisor.instructions
    greeting = greeting_instructions(stated, resume)
    assert "職種はカフェ" in greeting and "コンビニみほん駅前店" in greeting
    assert "近い仕事で探すか" not in greeting  # 最初の1問は希望の確認だけ(2問にしない)


def _facts(note: str) -> list[str]:
    """指示の追記から、履歴書の要点の行だけを取り出す。"""
    body = note.split("読み取り結果の要点)\n", 1)[1].split("\n- これは本人が", 1)[0]
    return body.split("\n")


def test_long_resume_keeps_whole_lines_and_the_important_facts(monkeypatch):
    """職歴7件(1件あたり duties 5つ)の長い履歴書でも、行の途中で切れず、資格・読み取れなかった点・最新の職歴が残る。"""
    from voice_resume import prompts

    resume = {
        "uploaded": "9/23",
        "job_summary": "飲食店と小売店で合わせて8年、接客と調理補助、売り場づくりを担当してきました。" * 6,
        "work_history": [  # 履歴書の書き方どおり古い順
            {"organization": f"店舗{i}", "employment_type": "アルバイト", "period": f"{2010 + 2 * i}年4月〜{2011 + 2 * i}年3月",
             "duties": [f"業務{i}-{d}" for d in range(1, 6)], "achievements": [f"成果{i}-{d}" for d in range(1, 5)]}
            for i in range(1, 8)
        ],
        "skills": ["接客", "調理補助", "レジ"],
        "certifications": [{"name": "調理師", "acquired": "2019年3月"}, {"name": "食品衛生責任者"}],
        "preferences": {"desired_work": ["飲食店のホール", "キッチン"], "conditions": ["平日の日中", "週3日"],
                        "notes": ["家から自転車で通える範囲"]},
        "unconfirmed": ["店舗3の退職時期"],
    }
    note = uploaded_resume_note(resume)
    facts = _facts(note)
    assert sum(len(f) + 1 for f in facts) <= RESUME_NOTE_MAX_CHARS + 20  # 上限(+ 省略の1行)に収まる
    assert facts[-1].startswith("- ほか ") and facts[-1].endswith("件は省略")

    # 行の途中で切れていない: 残った行はどれも、上限なしで作った行のどれかとそのまま一致する
    monkeypatch.setattr(prompts, "RESUME_NOTE_MAX_CHARS", 100_000)
    whole = set(_facts(uploaded_resume_note(resume)))
    assert all(f in whole for f in facts[:-1])

    text = "\n".join(facts)
    assert "資格・免許: 調理師(2019年3月)、食品衛生責任者" in text
    assert "読み取れなかった点: 店舗3の退職時期" in text
    assert "店舗7" in text and "店舗1(" not in text  # 最新の職歴が残り、いちばん古い職歴は入らない
    assert "業務7-3" in text and "業務7-4" not in text  # 1件あたり先頭3つまで


def test_greeting_touches_the_most_recent_job():
    """挨拶で触れるのは直近の職歴。期間で新しい順が分からなければ、末尾(履歴書の書き方で最新)を使う。"""
    old = {"organization": "古い店", "period": "2018年4月〜2019年3月", "duties": ["品出し"]}
    new = {"organization": "新しい店", "period": "2023年5月〜現在", "duties": ["レジ"]}
    for works in ([old, new], [new, old]):
        greeting = greeting_instructions(None, {"uploaded": "9/23", "work_history": works})
        assert "新しい店でレジ" in greeting and "古い店" not in greeting
    no_period = [{"organization": "一件目"}, {"organization": "二件目"}]
    assert "二件目" in greeting_instructions(None, {"uploaded": "9/23", "work_history": no_period})


def test_education_only_resume_is_still_used():
    """学歴しかない履歴書(学生・バイト経験なし)でも、在学中の学歴が指示に入り、挨拶で履歴書に触れる。"""
    summary = {"stated_preferences": {}, "uploaded_resume": {"uploaded": "9/23", "education": [
        {"school": "みほん中学校", "status": "卒業", "period": "2021年3月"},
        {"school": "みほん高等学校", "status": "在学中", "period": "2021年4月〜"},
    ]}}

    class Dip:
        async def get_user_conversation_summary(self, user_id):
            return summary

    advisor, greeting = asyncio.run(prepare_advisor(Dip(), U1))
    assert "学歴: みほん高等学校(在学中" in advisor.instructions
    assert "みほん中学校" not in advisor.instructions  # 1行だけ(在学中か最終学歴)
    assert "履歴書" in greeting and "確認形で1回" in greeting and "みほん高等学校に在学中" in greeting
    assert "職種" in greeting and "近い仕事で探すか" not in greeting  # 職歴が無いので職種から聞く
