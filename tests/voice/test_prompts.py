"""アドバイザー指示(ヒアリングの方針)と下書きプロンプトの決まりごとのテスト。

指示は、区分によらない音声の決まり(prompts.advisor_instructions)に、dip が配る今の雇用区分の音声版の playbook を
足したもの。アルバイト・パートの面談の決まり(タスク 31〜38)は parttime_voice.md にあるので、ここではアルバイト・パートの
面談の指示(PARTTIME)で確かめる。
"""

from __future__ import annotations

from dip_mcp import logic
from voice_resume.prompts import (
    CAREER_DRAFTER_SYSTEM,
    DRAFTER_SYSTEM,
    STAGES,
    advisor_instructions,
    drafter_system,
    drafter_user,
    greeting_instructions,
    stated_preferences_note,
)

BAITO, SEISHAIN, MITEI = "アルバイト・パート", "正社員", "未定"


def _instructions(track: str) -> str:
    """dip が配る音声版の playbook を入れた、その区分のアドバイザーの指示。"""
    return advisor_instructions(track, logic.counseling_playbook(track, "voice")["playbook"])


ADVISOR_INSTRUCTIONS = PARTTIME = _instructions(BAITO)


def test_advisor_honesty_and_pii_rules():
    assert "性格・感情を推測しない" in ADVISOR_INSTRUCTIONS
    assert "氏名・住所・電話番号・メールアドレス・生年月日は聞かない" in ADVISOR_INSTRUCTIONS
    assert "盛ったりしない" in ADVISOR_INSTRUCTIONS
    # 職歴が少ない人向けの聞き方(学校・家事・ボランティアも経験として拾う)
    for kw in ("部活", "家事", "ボランティア"):
        assert kw in ADVISOR_INSTRUCTIONS


def test_advisor_stages_in_order():
    """アルバイト・パートの進め方の目安が 希望条件 → 経験 → できること → 資格 → 確認(要約)→ 求人の提案 の順に書かれている。"""
    labels = [label for _, label in STAGES]
    assert labels == ["希望条件", "経験", "できること", "資格・免許", "確認", "求人の提案"]
    marks = ["2. 希望条件", "3. 経験", "4. できること", "5. 資格・免許", "短く要約して確かめ", "propose_jobs で求人を出す"]
    positions = [ADVISOR_INSTRUCTIONS.index(m) for m in marks]
    assert positions == sorted(positions)


def test_advisor_wishes_cover_search_conditions():
    """希望条件で聞く項目が求人の検索にそのまま使える(職種・エリア・曜日/時間帯・日数・時給)。"""
    for kw in ("職種", "エリアか最寄り駅", "曜日と時間帯", "週の日数", "時給"):
        assert kw in ADVISOR_INSTRUCTIONS


def test_advisor_keeps_it_short_and_focused():
    # 会話の長さと深掘りの上限
    assert "3〜5分" in ADVISOR_INSTRUCTIONS
    assert "6〜8問" in ADVISOR_INSTRUCTIONS
    assert "掘り下げは1つの項目につき1回まで" in ADVISOR_INSTRUCTIONS
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
    note = stated_preferences_note(STATED, BAITO)
    assert "職種: カフェ" in note and "時給の下限(円): 1200" in note
    assert "確認形で1回だけ" in note
    assert "聞き直さない" in note
    # 希望が無ければ何も足さない
    assert stated_preferences_note(None) == "" and stated_preferences_note({}) == ""


def test_greeting_asks_the_first_question_or_confirms_previous_wishes():
    """アルバイト・パートの初回は職種を聞き、前に話した希望があるユーザーには確認形で始めて聞き直さない。"""
    first = greeting_instructions(None, track=BAITO)
    assert "どんなバイトを探しているか" in first
    revisit = greeting_instructions(STATED, track=BAITO)
    assert "カフェ" in revisit and "確認形で1回だけ" in revisit and "聞き直さない" in revisit
    assert "どんなバイトを探しているか" not in revisit


def test_greeting_while_undecided_asks_the_track_first():
    """区分が未定なら、最初の1問で区分を確かめる(前に話した希望の確認は区分が決まってから)。書類は「履歴書など」。"""
    g = greeting_instructions(STATED)
    assert "正社員のお仕事と、空いた時間のアルバイトのどちらを考えているか" in g
    assert "履歴書などの下書き" in g and "確認形で1回だけ" not in g
    fulltime = greeting_instructions(None, track=SEISHAIN)
    assert "職務経歴書の下書き" in fulltime and "探している理由(目的)" in fulltime


def test_greeting_and_note_skip_conditions_of_the_other_track():
    """D のレビューの後続指摘: チャットで正社員の条件(希望年収など)を保存した人がアルバイトの音声面談に入っても、
    それを「前回の希望」として確かめない(指示の「前に本人が話した希望」にも入れない)。逆も同じ。
    負例: 区分に合う条件は、これまでどおり確かめる。"""
    stated = {"min_annual_salary": {"value": "年収500万円", "saved": "9/28"},
              "work_style": {"value": "リモート中心", "saved": "9/28"},
              "time_band": {"value": "夕方", "saved": "9/28"}, "min_wage": {"value": "1200", "saved": "9/28"},
              "area": {"value": "新宿", "saved": "9/28"}, "job_type": {"value": "カフェ", "saved": "9/28"}}
    baito = greeting_instructions(stated, track=BAITO) + stated_preferences_note(stated, BAITO)
    assert "年収500万円" not in baito and "リモート中心" not in baito
    assert "夕方" in baito and "1200" in baito and "新宿" in baito and "カフェ" in baito  # 負例: アルバイトの条件は確かめる
    seishain = greeting_instructions(stated, track=SEISHAIN) + stated_preferences_note(stated, SEISHAIN)
    assert "夕方" not in seishain and "1200" not in seishain
    assert "カフェ" not in seishain  # アルバイト・パートにしか無い職種は、正社員の検索に使えない
    assert "年収500万円" in seishain and "リモート中心" in seishain and "新宿" in seishain


def test_each_track_gets_its_own_voice_playbook_and_document_name():
    """指示に、区分に合った音声版の playbook が入り、書類の呼び方が区分に合う(アルバイト = 履歴書、正社員 = 職務経歴書、
    未定 = 履歴書など)。"""
    for track, doc in ((BAITO, "履歴書"), (SEISHAIN, "職務経歴書"), (MITEI, "履歴書など")):
        text = _instructions(track)
        assert f"# playbook: {track}(音声)" in text and "## 区分に共通の決まり(音声)" in text
        assert f"今の雇用区分: {track}" in text
        assert f"トークに{doc}の下書きと求人が出ます" in text
        others = [t for t in (BAITO, SEISHAIN, MITEI) if t != track]
        assert not any(f"# playbook: {t}(" in text for t in others)
    assert "職務経歴書" not in PARTTIME.replace("履歴書・職務経歴書", "")  # アルバイトの面談では職務経歴書と言わない
    seishain = _instructions(SEISHAIN)
    assert "職歴ごとの成果と数字" in seishain and "規模" in seishain
    assert "時給の下限(min_wage)はアルバイト・パートの条件なので、聞かない" in seishain
    assert "まず区分を確かめる" in _instructions(MITEI)
    # playbook が読めないときも、最小の進め方で会話はできる
    assert "最小の進め方" in advisor_instructions(BAITO, None)


def test_advisor_switches_track_only_on_the_persons_words():
    """区分の切り替えはチャットと同じ合図の決まり。履歴書だけでは切り替えない。"""
    for text in ("set_search_track", "evidence には根拠になった本人の発言をそのまま", "1問で確かめ", "履歴書の内容・前に話した希望だけでは切り替えない"):
        assert text in PARTTIME, text


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


def test_career_sheet_drafter_writes_summary_and_numbers_only_from_the_person():
    """正社員の面談の下書き(職務経歴書)は、職務要約と職歴ごとの成果(本人が言った数字)を中心に書き、数字を作らない。"""
    assert drafter_system("職務経歴書") == CAREER_DRAFTER_SYSTEM and drafter_system("履歴書") == DRAFTER_SYSTEM
    assert "職務経歴書の下書きを作る" in CAREER_DRAFTER_SYSTEM
    assert "職務要約(job_summary)" in CAREER_DRAFTER_SYSTEM and "achievements に成果" in CAREER_DRAFTER_SYSTEM
    assert "本人が数字を言っていなければ数字を作らず" in CAREER_DRAFTER_SYSTEM
    assert "盛らない・作らない" in CAREER_DRAFTER_SYSTEM and "本人の希望ではない" in CAREER_DRAFTER_SYSTEM
    assert "職務経歴書の下書きを作ってください" in drafter_user("本人: はい", "職務経歴書")
    assert "履歴書の下書きを作ってください" in drafter_user("本人: はい")
