"""事実性のチェック(evals/checks.py の fact_violations)。LLM は使わない。"""

from __future__ import annotations

from evals.checks import fact_violations, preference_violations


def turn(bubbles, tools=()):
    return {"bubbles": bubbles, "quick_replies": [], "cards": [], "buttons": [],
            "tools": [{"name": n, "args": {}, "result": r} for n, r in tools]}


SUMMARY = ("get_user_conversation_summary", {"last_contact": {"when": "9/23"}})


def test_facts_from_tools_push_and_user_are_not_violations():
    push = {"job_id": "a002", "title": "コンビニスタッフ(夜勤)", "remaining_fields": ["電話番号", "希望開始日"]}
    job = ("answer_job_question", {"job": {"title": "カフェホール 渋谷道玄坂", "employer": "コーヒースタンド ドウゲン",
                                           "hourly_wage": 1250, "walk_min": 6}})
    turns = [
        turn(["「コンビニスタッフ(夜勤)」の応募フォームが途中のようです。"], [SUMMARY]),
        turn(["コーヒースタンド ドウゲンです。渋谷駅徒歩6分、時給1,250円。"], [job]),
    ]
    assert fact_violations(turns, ["詳しく聞きたい", "時給は1200円以上がいい"], push) == []


def test_wage_date_walk_and_count_not_in_any_source_are_violations():
    turns = [turn(["新着が5件入りました。時給1,500円、駅から徒歩2分、掲載は9/30までです。"], [SUMMARY])]
    v = fact_violations(turns, ["こんにちは"], None)
    assert any("1500円" in x for x in v) and any("9/30" in x for x in v)
    assert any("徒歩2分" in x for x in v) and any("5件" in x for x in v)


def test_real_job_not_returned_in_this_conversation_is_a_violation():
    # 求人一覧に実在する求人名でも、この会話でツールが返していなければ作り話と同じ扱い
    turns = [turn(["「居酒屋ホール 新宿西口」はいかがですか?"], [SUMMARY])]
    v = fact_violations(turns, ["夜のバイト"], None)
    assert any("居酒屋ホール 新宿西口" in x for x in v)


def test_saved_preferences_must_come_from_what_the_user_said():
    save = ("save_user_preferences", {"saved": ["time_band", "area", "min_wage"]})
    t = turn(["承知しました。"], [save])
    t["tools"][0]["args"] = {"preferences": {"time_band": "夜", "area": "新宿", "min_wage": "1500"}}
    v = preference_violations([t], ["夜だけ、新宿あたりで働きたい", "時給は1200円以上"])
    assert v == ["T1: 本人が言っていない希望を保存した: min_wage=1500"]


def test_saved_skills_and_certifications_must_come_from_what_the_user_said():
    """「できること」「資格」でも、本人の発言に無い値の保存は違反になる(task 41)。"""
    save = ("save_user_preferences", {"saved": ["skills", "certifications"]})
    t = turn(["承知しました。"], [save])
    t["tools"][0]["args"] = {"preferences": {"skills": "レジ打ち", "certifications": "普通自動車免許"}}
    said = ["コンビニでレジ打ちをやっていました", "免許は持っていないです"]
    v = preference_violations([t], said)
    assert v == ["T1: 本人が言っていない希望を保存した: certifications=普通自動車免許"]


def test_preferences_confirmed_from_last_time_or_combined_are_not_violations():
    # 再訪: 前回の希望(要約の stated_preferences)を本人が「だいたい同じ」と認め、エリアだけ広げた
    summary = ("get_user_conversation_summary", {"stated_preferences": {
        "job_type": {"value": "カフェ", "saved": "9/20"}, "area": {"value": "渋谷", "saved": "9/20"},
        "time_band": {"value": "日中", "saved": "9/20"}}})
    t1 = turn(["前回は渋谷・日中・カフェでしたが、今回も同じですか?"], [summary])
    t2 = turn(["承知しました。"], [("save_user_preferences", {"saved": ["area", "time_band", "job_type"]})])
    t2["tools"][0]["args"] = {"preferences": {"area": "渋谷と原宿", "time_band": "日中", "job_type": "カフェ"}}
    said = ["またバイト探したいです", "だいたい前回と同じで、エリアは渋谷だけじゃなくて原宿も入れてほしい"]
    assert preference_violations([t1, t2], said) == []
    t2["tools"][0]["args"] = {"preferences": {"area": "渋谷と新宿"}}  # 新宿は誰も言っていない
    assert preference_violations([t1, t2], said) == ["T2: 本人が言っていない希望を保存した: area=渋谷と新宿"]


def test_numbers_are_checked_against_the_same_unit_only():
    # ID や日付の数字(J0020 の 20、total の 12)は、徒歩や件数の根拠にならない
    rec = ("recommend_jobs", {"total": 12, "jobs": [{"job_id": "J0020", "walk_min": 4, "hourly_wage": 1200,
                                                   "posted": "2026-09-20"}]})
    v = fact_violations([turn(["徒歩20分、全部で7件、時給1,200〜1,500円です。"], [rec])], ["探して"], None)
    assert any("徒歩20分" in x for x in v) and any("7件" in x for x in v) and any("1500円" in x for x in v)
    assert not any("1200円" in x for x in v)
    ok = fact_violations([turn(["徒歩4分、全部で12件、時給1,200円です。"], [rec])], ["探して"], None)
    assert ok == []


def test_dates_match_full_dates_but_unreturned_dates_are_violations():
    listing = ("answer_job_question", {"hits": [{"a": "掲載は2026/09/30までです。"}]})
    assert fact_violations([turn(["掲載は9月30日までです。"], [listing])], ["いつまで?"], None) == []
    # ツールが返していない日付は事実の根拠にならない
    v = fact_violations([turn(["掲載は10月23日までです。"], [SUMMARY])], ["いつまで?"], None)
    assert any("10/23" in x for x in v)


def test_ending_without_choices_is_allowed_only_right_after_a_stop_request():
    from evals.checks import conversation_checks

    sc = {"expect_outcome": ["wait"]}
    ask = {**turn(["探しているお仕事はありますか?"]), "quick_replies": ["求人を探す", "今はいい"]}
    done = turn(["承知しました。またいつでもお声がけください。"])  # 選択肢なし
    v = conversation_checks(sc, [ask, done], [None, "もう送らないで"])["violations"]
    assert not any("選択肢がない" in x for x in v)  # 「もう送らないで」の後は「承知しました」で終えてよい
    v = conversation_checks(sc, [ask, done], [None, "ありがとう"])["violations"]
    assert any("選択肢がない" in x for x in v)


def test_questions_or_jobs_right_after_a_refusal_are_violations():
    from evals.checks import conversation_checks

    sc = {"expect_outcome": ["wait"]}
    ask = {**turn(["探しているお仕事はありますか?"]), "quick_replies": ["求人を探す", "今はいい"]}
    push_more = {**turn(["条件を変えて探しますか?"]), "quick_replies": ["条件を変える", "今はいい"]}
    v = conversation_checks(sc, [ask, push_more], [None, "今はいい"])["violations"]
    assert any("質問や求人を続けた" in x for x in v)
    done = {**turn(["承知しました。またいつでもお声がけください。"]), "quick_replies": ["求人を探す", "使い方"]}
    assert conversation_checks(sc, [ask, done], [None, "今はいい"])["violations"] == []


def test_phrase_inside_a_longer_message_is_not_a_refusal():
    """「今はいい感じの…を探したい」のような部分一致は断りではなく、続けて質問してよい。"""
    from evals.checks import conversation_checks

    sc = {"expect_outcome": ["propose"]}
    ask = {**turn(["渋谷ですね。時間帯の希望はありますか?"]), "quick_replies": ["日中", "夜", "今はいい"]}
    v = conversation_checks(sc, [ask], ["今はいい感じのカフェバイトを渋谷で探したい"])["violations"]
    assert not any("質問や求人を続けた" in x for x in v)
    # 語尾ゆれ(「今はいいです。」)は発言そのものなら断りとして扱う
    push_more = {**turn(["条件を変えて探しますか?"]), "quick_replies": ["条件を変える", "今はいい"]}
    v = conversation_checks(sc, [ask, push_more], [None, "今はいいです。"])["violations"]
    assert any("質問や求人を続けた" in x for x in v)


# ----------------------------- 再訪の挨拶(evals/checks.py の conversation_checks) -----------------------------


def _checks(sc_id: str, turns: list[dict]) -> list[str]:
    from evals.checks import conversation_checks
    from evals.scenarios import load_scenarios

    sc = next(s for s in load_scenarios() if s["id"] == sc_id)
    return conversation_checks(sc, turns, [None] + ["はい"] * (len(turns) - 1))["violations"]


def test_revisit_reasking_a_saved_preference_is_a_violation():
    turns = [turn(["前回は渋谷・日中・カフェでしたが、今回も同じですか?"]),
             turn(["承知しました。どのエリアで探しますか?"]), turn(["探します。"])]
    for t in turns:
        t["quick_replies"] = ["今はいい"]
    assert any("聞き直した" in v and "area" in v for v in _checks("R-revisit", turns))
    for ask, key in (("勤務地はどこがいいですか?", "area"), ("どの辺で働きたいですか?", "area"),
                     ("何時まで働けますか?", "time_band")):
        turns[1]["bubbles"] = [ask]
        assert any("聞き直した" in v and key in v for v in _checks("R-revisit", turns)), ask
    turns[1]["bubbles"] = ["承知しました。渋谷・日中のカフェで探しますか?"]  # 確認して進むのは聞き直しではない
    assert not any("聞き直した" in v for v in _checks("R-revisit", turns))

    # 本人が変えた項目を、今回の値で確かめるのは聞き直しではない(R-revisit のペルソナは「エリアは原宿も入れてほしい」と答える)
    from evals.checks import conversation_checks
    from evals.scenarios import load_scenarios

    sc = next(s for s in load_scenarios() if s["id"] == "R-revisit")
    msgs = [None, "そのとおりで、エリアは原宿も入れてほしい", "はい"]
    for confirm in ("エリアは渋谷と原宿でよいですか?", "原宿も入れますね。エリアは渋谷・原宿の2つで探しますか?"):
        turns[1]["bubbles"] = [confirm]
        assert not any("聞き直した" in v for v in conversation_checks(sc, turns, msgs)["violations"]), confirm
    turns[1]["bubbles"] = ["承知しました。どのエリアで探しますか?"]  # 答えを受け取った後に白紙で聞くのは聞き直し
    assert any("聞き直した" in v and "area" in v for v in conversation_checks(sc, turns, msgs)["violations"])


def test_revisit_browsing_must_mention_once_and_not_list_jobs():
    ok = [turn(["最近はカフェのお仕事を見ていましたか?"]), turn(["渋谷・日中のカフェです。"])]
    for t in ok:
        t["quick_replies"] = ["今はいい"]
    assert not any("行動" in v or "羅列" in v for v in _checks("R-revisit-browsing", ok))

    silent = [dict(ok[0], bubbles=["こんにちは。何をしますか?"]), ok[1]]
    assert any("触れていない" in v for v in _checks("R-revisit-browsing", silent))
    twice = [ok[0], dict(ok[1], bubbles=["閲覧していたカフェに近い求人です。"])]
    assert any("2回触れた" in v for v in _checks("R-revisit-browsing", twice))
    # 丁寧な依頼・案内や、希望を保存したという報告は行動履歴への言及に数えない。「ご覧になっていましたか」は言及に数える
    polite = [ok[0], dict(ok[1], bubbles=["気になる求人があれば詳細を見ていただけますか?", "ぜひ見ていってください。ご覧ください。",
                                          "条件に合う求人を一緒に見ていきましょう。", "詳細はこちらからご覧になれます。",
                                          "詳しくはご覧になってください。", "渋谷・日中のカフェで希望を保存しています。",
                                          "前回の希望を保存していましたので、その条件で探します。"])]
    assert not any("行動" in v for v in _checks("R-revisit-browsing", polite))
    # 2通目以降の、画面の求人を指す現在形は行動履歴への言及に数えない
    now = [ok[0], dict(ok[1], bubbles=["今ご覧になっている求人の詳細を見ますか?",
                                       "いま見ている求人について聞きたいことはありますか?"])]
    assert not any("行動" in v for v in _checks("R-revisit-browsing", now))
    for mention in ("最近はカフェのお仕事をご覧になっていましたか?", "最近カフェのお仕事をご覧になりましたか?",
                    "最近はカフェのお仕事を見てましたか?", "最近カフェの求人をチェックされていましたか?",
                    "最近カフェのお仕事を探していましたか?"):
        once = [dict(ok[0], bubbles=[mention]), ok[1]]
        assert not any("行動" in v or "羅列" in v for v in _checks("R-revisit-browsing", once)), mention
    for listing in ("最近はカフェホール 渋谷道玄坂などを見ていましたか?", "最近3件の求人を見ていましたか?",
                    "最近はカフェやアパレル販売を見ていましたか?"):
        listed = [dict(ok[0], bubbles=[listing]), ok[1]]
        assert any("羅列" in v for v in _checks("R-revisit-browsing", listed)), listing
