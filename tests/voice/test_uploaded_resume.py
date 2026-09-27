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

    # 長い履歴書でも、指示に入る要点は上限の文字数で切る
    long = {**resume, "work_history": [{"organization": f"店舗{i}", "duties": ["レジ" * 20]} for i in range(50)]}
    note = uploaded_resume_note(long)
    facts = note.split("## アップロードした履歴書", 1)[1].split("\n- これは", 1)[0]
    assert len(facts) <= RESUME_NOTE_MAX_CHARS + 50 and "省略" in facts
    assert "店舗0" in note and "店舗49" not in note

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
