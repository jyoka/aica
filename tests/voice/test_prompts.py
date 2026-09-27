"""アドバイザー指示(ヒアリングの方針)と下書きプロンプトの決まりごとのテスト。"""

from __future__ import annotations

from voice_resume.prompts import (
    ADVISOR_INSTRUCTIONS,
    DRAFTER_SYSTEM,
    STAGES,
    greeting_instructions,
    stated_preferences_note,
)


def test_advisor_honesty_and_pii_rules():
    assert "性格・感情を推測しない" in ADVISOR_INSTRUCTIONS
    assert "氏名・住所・電話番号・メールアドレス・生年月日は聞かない" in ADVISOR_INSTRUCTIONS
    assert "盛ったりしない" in ADVISOR_INSTRUCTIONS
    # 職歴が少ない人向けの聞き方(学校・家事・ボランティアも経験として拾う)
    for kw in ("部活", "家事", "ボランティア"):
        assert kw in ADVISOR_INSTRUCTIONS


def test_advisor_stages_in_order():
    """進め方が 希望条件 → 経験 → できること → 資格 → 確認 → 求人の提案 の順に書かれている。"""
    labels = [label for _, label in STAGES]
    assert labels == ["希望条件", "経験", "できること", "資格・免許", "確認", "求人の提案"]
    positions = [ADVISOR_INSTRUCTIONS.index(label) for label in labels]
    assert positions == sorted(positions)


def test_advisor_wishes_cover_search_conditions():
    """希望条件で聞く項目が求人の検索にそのまま使える(職種・エリア・曜日/時間帯・日数・時給)。"""
    for kw in ("職種", "エリアか最寄り駅", "曜日と時間帯", "週の日数", "時給"):
        assert kw in ADVISOR_INSTRUCTIONS


def test_advisor_keeps_it_short_and_focused():
    # 会話の長さと深掘りの上限
    assert "3〜5分" in ADVISOR_INSTRUCTIONS
    assert "6〜8問" in ADVISOR_INSTRUCTIONS
    assert "深掘りするのは最大1回" in ADVISOR_INSTRUCTIONS
    # 話し方: 短く、選択肢を読み上げすぎない、考え中は待つ
    assert "2文まで" in ADVISOR_INSTRUCTIONS
    assert "2〜3個" in ADVISOR_INSTRUCTIONS
    assert "考えている間は待つ" in ADVISOR_INSTRUCTIONS
    assert "続きをどうぞ" in ADVISOR_INSTRUCTIONS


def test_advisor_limits_experience_probing():
    """経験の深掘りは1回だけで、答えが短くても2問目を続けない(2026-09-26 の会話で3回続いた対策)。"""
    assert "追加の質問は1回だけ" in ADVISOR_INSTRUCTIONS
    assert "2問目は聞かずに次の段階へ進む" in ADVISOR_INSTRUCTIONS
    assert "答えが短くても重ねて聞かない" in ADVISOR_INSTRUCTIONS


def test_advisor_strengths_stage_is_not_leading():
    """「できること」は本人が言った事実の確認だけ。適性を聞く質問は誘導になるので禁止と明記。"""
    assert "合いそうですか" in ADVISOR_INSTRUCTIONS  # 禁止する言い方の例として書かれている
    assert "答えを誘導する" in ADVISOR_INSTRUCTIONS
    assert "本人が言っていない作業や強みを" in ADVISOR_INSTRUCTIONS
    # 旧の例文(「接客は得意そうですね。合っていますか?」)自体が誘導だったので消えている
    assert "得意そうですね" not in ADVISOR_INSTRUCTIONS
    assert "候補を2〜3個挙げて" not in ADVISOR_INSTRUCTIONS


def test_advisor_bans_off_topic_probing():
    # STAR のような型・バイト探しに直結しない質問・解釈や評価・自己PRの聞き出しはしない
    assert "STAR" in ADVISOR_INSTRUCTIONS
    assert "大切にしたいこと" in ADVISOR_INSTRUCTIONS  # やらないことの例として明記
    assert "解釈や評価を足さない" in ADVISOR_INSTRUCTIONS
    assert "自己PRは聞き出さない" in ADVISOR_INSTRUCTIONS


def test_advisor_zero_hit_policy():
    """0件のときは relax の選択肢を数字つきで伝え、一度で選んでもらう(タスク34)。"""
    assert "求人が0件だったとき" in ADVISOR_INSTRUCTIONS
    assert "エリアを外すと5件" in ADVISOR_INSTRUCTIONS  # 具体的に伝える例
    assert "探してもよいですか" in ADVISOR_INSTRUCTIONS  # 許可の確認を繰り返さない、の文脈で
    assert "当てずっぽう" in ADVISOR_INSTRUCTIONS
    assert "relax" in ADVISOR_INSTRUCTIONS
    assert "書き換えない" in ADVISOR_INSTRUCTIONS  # 本人の希望はそのまま


def test_advisor_uses_tools():
    assert "propose_jobs" in ADVISOR_INSTRUCTIONS
    assert "finalize_interview" in ADVISOR_INSTRUCTIONS


def test_advisor_calls_tool_in_same_turn():
    """「探します」と言ったのにツールを呼ばず、本人が促すまで検索されない事故(タスク 36)を防ぐ。"""
    # 「探します」「調べます」と言うなら同じ応答でツールを呼ぶ。呼ばないなら言わない
    assert "その発話と同じ応答の中で" in ADVISOR_INSTRUCTIONS
    assert "ツールを呼ばない応答では「探します」「調べます」と言わない" in ADVISOR_INSTRUCTIONS
    # 条件を変えて探し直すときは、本人の了承を得て、了承と同じ応答で propose_jobs を呼び直す
    assert "propose_jobs を呼び直す" in ADVISOR_INSTRUCTIONS
    assert "発話と同じ応答で propose_jobs を呼び直す" in ADVISOR_INSTRUCTIONS
    # 省いてよいのは再度の要約確認であって、本人の了承ではない
    assert "本人の了承ではない" in ADVISOR_INSTRUCTIONS
    # 聞き取り途中の受け止めで「〜で探しますね」と言わない
    assert "「〜ですね」と言う" in ADVISOR_INSTRUCTIONS


def test_advisor_does_not_guess_job_type():
    """職種を決めていない人には、推測した職種で検索しない(職種なしで検索するか本人に聞く)。"""
    assert "職種なしで検索" in ADVISOR_INSTRUCTIONS
    assert "経験からの推測で" in ADVISOR_INSTRUCTIONS


def test_advisor_separates_relaxed_search_from_wishes():
    """条件をゆるめた再検索は search_only=True で、本人の希望の記録を変えない。"""
    assert "search_only=True" in ADVISOR_INSTRUCTIONS
    assert "本人の希望が変わったわけではない" in ADVISOR_INSTRUCTIONS


STATED = {"job_type": {"value": "カフェ", "saved": "9/23"}, "min_wage": {"value": "1200", "saved": "9/23"}}


def test_stated_preferences_note_follows_the_line_rule():
    """前に話した希望は、LINE 側と同じ決まり(確認形で1回だけ・聞き直さない)で指示に入る。"""
    note = stated_preferences_note(STATED)
    assert "職種: カフェ" in note and "時給の下限(円): 1200" in note
    assert "確認形で1回だけ" in note
    assert "聞き直さない" in note
    # 希望が無ければ何も足さない
    assert stated_preferences_note(None) == "" and stated_preferences_note({}) == ""


def test_greeting_asks_the_first_question_or_confirms_previous_wishes():
    """初回は職種を聞き、前に話した希望があるユーザーには確認形で始めて聞き直さない。"""
    first = greeting_instructions(None)
    assert "どんなバイトを探しているか" in first
    revisit = greeting_instructions(STATED)
    assert "カフェ" in revisit and "確認形で1回だけ" in revisit and "聞き直さない" in revisit
    assert "どんなバイトを探しているか" not in revisit


def test_drafter_system_facts_only():
    assert "盛らない・作らない" in DRAFTER_SYSTEM
    assert "個人情報" in DRAFTER_SYSTEM
    assert "unconfirmed" in DRAFTER_SYSTEM


def test_drafter_keeps_relaxed_search_conditions_out_of_wishes():
    """検索のために外した・ゆるめた条件を本人の希望として書かない(希望は言ったまま)。"""
    assert "本人が希望として言ったことだけを書く" in DRAFTER_SYSTEM
    assert "一旦、時給の下限を外して" in DRAFTER_SYSTEM  # 実会話で起きた例をそのまま挙げる
    assert "時給1500円くらい" in DRAFTER_SYSTEM
    assert "検索の経緯" in DRAFTER_SYSTEM


def test_drafter_self_pr_is_not_a_repeat_of_job_summary():
    """材料が無いとき、自己PRを職務要約の繰り返しにせず、空にして unconfirmed に回す。"""
    assert "職務要約の繰り返し・言い換えにはしない" in DRAFTER_SYSTEM
    assert "空文字" in DRAFTER_SYSTEM
    assert "自己PRの材料(工夫したこと・任されたことなど)を本人に確認したい" in DRAFTER_SYSTEM
