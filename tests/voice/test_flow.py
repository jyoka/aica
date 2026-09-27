"""会話の流れを台本(文字起こし)で確かめるオフラインテスト。実 API は呼ばない。"""

from __future__ import annotations

import json
from pathlib import Path

from voice_resume.flow import (
    count_experience_probes,
    find_leading_questions,
    stage_positions,
    verify_flow,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_script() -> list[dict]:
    return json.loads((FIXTURES / "script_transcript.json").read_text(encoding="utf-8"))


def load_bad_probing() -> list[dict]:
    """2026-09-26 の実会話の抜粋(経験の深掘り3回と誘導質問が出た部分)。"""
    return json.loads((FIXTURES / "bad_probing_transcript.json").read_text(encoding="utf-8"))


def test_script_covers_all_stages_in_order():
    """台本が 希望 → 経験 → できること → 資格 → 確認 → 求人の提案 の順で進む。"""
    ok, found = verify_flow(load_script())
    assert ok, f"流れが崩れている: {found}"
    assert found == ["wishes", "experience", "strengths", "certifications", "confirm", "recommend"]


def test_stage_positions_are_increasing():
    positions = stage_positions(load_script())
    values = [positions[s] for s in ("wishes", "experience", "strengths", "certifications", "confirm", "recommend")]
    assert values == sorted(values)


def test_out_of_order_transcript_fails():
    """確認より先に求人を提案してしまう台本は不合格になる。"""
    reordered = [
        {"role": "assistant", "text": "どんなお仕事を探していますか。"},
        {"role": "assistant", "text": "求人が3件見つかりました。"},
        {"role": "assistant", "text": "アルバイトの経験はありますか。部活でも大丈夫です。"},
        {"role": "assistant", "text": "接客は得意そうですね。合っていますか。"},
        {"role": "assistant", "text": "資格や免許はありますか。"},
        {"role": "assistant", "text": "一度まとめますね。違うところはありますか。"},
    ]
    ok, found = verify_flow(reordered)
    assert not ok
    assert "recommend" not in found


def test_missing_stage_fails():
    """資格を聞かない台本は不合格になる。"""
    script = [t for t in load_script() if "資格" not in t["text"]]
    ok, found = verify_flow(script)
    assert not ok
    assert "certifications" not in found


def test_bad_log_reproduces_over_probing_and_leading():
    """2026-09-26 の実会話では、経験の深掘りが3回(上限1回)、誘導する質問が1回出ていた。"""
    bad = load_bad_probing()
    assert count_experience_probes(bad) == 3
    leading = find_leading_questions(bad)
    assert len(leading) == 1
    assert "合いそうですか" in leading[0]


def test_good_script_keeps_probe_budget_and_no_leading():
    """良い台本は、経験の深掘りが1回以内で、誘導する質問(適性を聞く形)が無い。"""
    script = load_script()
    assert count_experience_probes(script) <= 1
    assert find_leading_questions(script) == []


def test_confirming_facts_is_not_probing():
    """事実の確認だけの聞き返し(「〜で合っていますか」)は深掘りに数えない。"""
    transcript = [
        {"role": "assistant", "text": "これまでにアルバイトや仕事の経験はありますか。"},
        {"role": "user", "text": "あ、ファミレス"},
        {"role": "assistant", "text": "ファミレスでどんなお仕事をされていましたか。"},
        {"role": "user", "text": "キッチンで調理をしていました"},
        {"role": "assistant", "text": "キッチンで調理をされていた、で合っていますか。"},
        {"role": "user", "text": "はい"},
        {"role": "assistant", "text": "持っている資格や免許はありますか。"},
    ]
    assert count_experience_probes(transcript) == 1
    assert find_leading_questions(transcript) == []
