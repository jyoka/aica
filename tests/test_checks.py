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


FULLTIME = ("recommend_jobs", {"total": 4, "jobs": [
    {"job_id": "f0201", "title": "プロダクトマネージャー(BtoB SaaS)", "employment_type": "正社員",
     "pay": {"kind": "年収", "min": 6500000, "max": 9500000}, "pay_text": "年収650万〜950万円", "walk_min": 5},
    {"job_id": "f0104", "title": "賃貸仲介の営業(店舗でのご案内)", "employment_type": "正社員",
     "pay": {"kind": "月給", "min": 260000, "max": 350000}, "pay_text": "月給26万〜35万円", "walk_min": 4},
    {"job_id": "b0101", "title": "居酒屋ホールスタッフ", "employment_type": "アルバイト・パート",
     "pay": {"kind": "時給", "min": 1350, "max": None}, "pay_text": "時給1350円", "walk_min": 4}]})


def test_annual_and_monthly_pay_from_dip_are_not_violations():
    # dip の pay(円)を、万で書いても円で書いても根拠にする。幅の下限だけ・上限だけ・「〜」の片側に万が無い書き方も同じ
    turns = [turn(["年収650万〜950万円のPMの求人と、月給26万〜35万円の営業の求人があります。"], [FULLTIME]),
             turn(["PMの求人は年収650〜950万円、下限は6,500,000円です。月給は35万円までです。"]),
             turn(["居酒屋は時給1,350円です。"])]
    assert fact_violations(turns, ["正社員で探したい"], None) == []


def test_pay_the_person_said_is_not_a_violation():
    # 本人が言った希望年収(「600万以上」)を繰り返すのは作り話ではない
    turns = [turn(["年収600万円以上で探しますね。"], [SUMMARY])]
    assert fact_violations(turns, ["年収は600万以上がいい"], None) == []


def test_annual_pay_not_from_dip_is_still_a_violation():
    # 捏造の検出は今まで通り: dip が返していない年収・月給、幅の片側の書き換え、時給の作り話
    turns = [turn(["年収800万円も狙えます。"], [FULLTIME]),
             turn(["PMは年収650〜1000万円です。"]),
             turn(["月給40万円の求人もあります。", "時給1,600円です。"])]
    v = fact_violations(turns, ["正社員で探したい"], None)
    assert any("T1" in x and "800万円" in x for x in v)
    assert any("T2" in x and "1000万円" in x for x in v)
    assert any("T3" in x and "40万円" in x for x in v) and any("T3" in x and "1600円" in x for x in v)
    assert not any("650" in x or "950" in x for x in v)


def test_monthly_pay_with_a_decimal_is_checked():
    # 「月給27.5万円」のような小数の万も金額として照合する: dip の pay(275000 円)なら根拠あり、無ければ作り話
    decimal = ("recommend_jobs", {"jobs": [{"job_id": "f0105", "employment_type": "正社員",
                                            "pay": {"kind": "月給", "min": 275000, "max": 350000}}]})
    assert fact_violations([turn(["月給27.5万円からです。"], [decimal])], ["正社員で探したい"], None) == []
    v = fact_violations([turn(["月給27.5万円です。"], [FULLTIME])], ["正社員で探したい"], None)
    assert any("27.5万円" in x for x in v)


def test_decimal_man_is_not_read_as_its_last_digits():
    # 「1.5万」の「5万」を別の金額として拾わない(dip に5万が無くても、1.5万が根拠ありなら違反にしない)
    bonus = ("answer_job_question", {"job": {"pay": {"kind": "月給", "min": 260000, "max": 350000}},
                                     "answer": "資格手当は月1.5万円です。"})
    assert fact_violations([turn(["資格手当は月1.5万円です。"], [bonus])], ["手当は?"], None) == []
    v = fact_violations([turn(["資格手当は月1.5万円です。"], [FULLTIME])], ["手当は?"], None)
    assert any("1.5万円" in x for x in v) and not any("「5万円」" in x for x in v)


def test_min_and_max_outside_pay_are_not_taken_as_pay():
    # min / max という名前の数字でも、pay の形(kind が 時給・月給・年収)でなければ給与の根拠にしない
    other = ("get_user_conversation_summary", {"range": {"min": 8000000, "max": 800}})
    v = fact_violations([turn(["年収800万円の求人があります。"], [other])], ["こんにちは"], None)
    assert any("800万円" in x for x in v)


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


def test_free_text_items_in_the_persons_own_words_are_not_violations():
    """相談で分かった本人のこと(自由記述)は、助詞や言い回しが違っても、内容語の大半が発言にあれば作り話ではない。"""
    said = ["人と話すのは好きです", "未経験でもついていけるかが不安です"]
    fine = [("concerns", "未経験でもついていけるか不安"), ("concerns", "未経験でついていけるか"),
            ("wants", "人と話すこと"), ("wants", "人と話す仕事"), ("strengths", "人と話すのが好き")]
    for k, v in fine:
        t = turn(["承知しました。"], [("save_user_preferences", {"saved": [k]})])
        t["tools"][0]["args"] = {"preferences": {k: v}}
        assert preference_violations([t], said) == [], (k, v)
    t = turn(["承知しました。"], [("save_user_preferences", {"saved": ["concerns"]})])
    t["tools"][0]["args"] = {"preferences": {"concerns": "残業が多いこと"}}  # 誰も言っていない
    assert preference_violations([t], said) == ["T1: 本人が言っていない希望を保存した: concerns=残業が多いこと"]


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
    for confirm in ("エリアは渋谷と原宿でよいですか?", "原宿も入れますね。エリアは渋谷・原宿の2つで探しますか?",
                    "エリアは渋谷と原宿の2つですね?", "エリアは渋谷と原宿で合っていますか?",
                    "原宿も追加しますね。エリアは渋谷・原宿でOKですか?", "エリアは渋谷と原宿でどうでしょう?",
                    "エリアは渋谷と原宿でどうですか?", "エリアは渋谷と原宿、どちらも含めますか?",
                    "エリアは渋谷と原宿で、何か他に条件はありますか?", "エリアはいつもの渋谷に原宿を足す形でよいですか?",
                    "エリアは渋谷と原宿の両方で、どうぞよろしいですか?"):
        turns[1]["bubbles"] = [confirm]
        assert not any("聞き直した" in v for v in conversation_checks(sc, turns, msgs)["violations"]), confirm
    for blank in ("承知しました。どのエリアで探しますか?", "渋谷以外だと、どのエリアがいいですか?",
                  "渋谷以外のどのエリアで探しますか?"):  # 値を示さない白紙の問いは聞き直し
        turns[1]["bubbles"] = [blank]
        assert any("聞き直した" in v and "area" in v for v in conversation_checks(sc, turns, msgs)["violations"]), blank
    for blank in ("夕方ですね。日中から変えて、どの時間帯にしますか?", "夕方ですね。日中以外なら、どの時間帯でよいですか?"):
        turns[1]["bubbles"] = [blank]
        v = conversation_checks(sc, turns, [None, "夕方がいい", "はい"])["violations"]
        assert any("time_band" in x for x in v), blank


def test_reask_exemption_does_not_leak_across_keys_or_snapshot_saves():
    """確認扱いの免除は、確認の形の問いだけ。別の項目の語や、同じ値の保存し直しでは聞き直しを見逃さない。"""
    from evals.checks import reask_violations

    def t(bubbles, saves=None):
        return {"bubbles": bubbles, "cards": [], "quick_replies": [],
                "tools": [{"name": "save_user_preferences", "args": {"preferences": saves}}] if saves else []}

    cases = [  # (ターン, 本人の発言)
        ([t(["どこで働きたいですか?"]), t(["渋谷ですね。"], {"area": "渋谷"}),
          t(["接客のお仕事ですね、どのエリアで働きたいですか?"], {"job_type": "接客"})], [None, "渋谷", "接客の仕事がいい"]),
        ([t(["どこで働きたいですか?"]), t(["渋谷ですね。"], {"area": "渋谷"}),
          t(["接客のお仕事ですね、どのエリアで探しますか?"], {"job_type": "接客"})], [None, "渋谷", "接客の仕事がいい"]),
        ([t(["どこで働きたいですか?"]), t(["渋谷ですね。"], {"area": "渋谷"}),
          t(["接客ですね。接客でどのエリアで探しますか?"], {"job_type": "接客"})], [None, "渋谷", "接客の仕事がいい"]),
        # 疑問詞の無い白紙の問いでも、前の答えを添えただけなら聞き直し
        ([t(["どこで働きたいですか?"]), t(["渋谷ですね。"], {"area": "渋谷"}),
          t(["接客のお仕事で、勤務地の希望はありますか?"], {"job_type": "接客"})], [None, "渋谷", "接客の仕事がいい"]),
        ([t(["どこで働きたいですか?"]), t(["渋谷ですね。"], {"area": "渋谷"}),
          t(["接客のお仕事、エリアはご希望ありますか?"], {"job_type": "接客"})], [None, "渋谷", "接客の仕事がいい"]),
        ([t(["いつ働けますか?"]), t(["夕方ですね。"], {"time_band": "夕方"}),
          t(["カフェで、時間帯は希望がありますか?"], {"job_type": "カフェ"})], [None, "夕方", "カフェがいい"]),
        ([t(["経験はありますか?"]), t(["未経験ですね。"], {"experience": "未経験"}),
          t(["カフェのお仕事の経験はありますか?"])], [None, "未経験です", "カフェが気になる"]),
        ([t(["いつ働けますか?"]), t(["夕方ですね。"], {"time_band": "夕方"}), t(["承知しました。どの時間帯がいいですか?"])],
         [None, "夕方", "時間はあまり取れないかも"]),
        ([t(["どこで働きたいですか?"]), t(["渋谷ですね。"], {"area": "渋谷"}),
          t(["どのエリアで探しますか?"], {"area": "渋谷", "job_type": "カフェ"})], [None, "渋谷", "カフェ"]),
    ]
    for turns, msgs in cases:
        assert any("聞き直した" in v for v in reask_violations(turns, msgs)), turns[-1]["bubbles"]


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
    # 2通目以降の、これからの行動を促す言い方も数えない
    later = [ok[0], dict(ok[1], bubbles=["詳細をご覧になりましたら、気になる点を教えてください。",
                                         "求人をご覧になった上で、応募するか決めてください。",
                                         "求人を見てたら気になるのはありましたか?"])]
    assert not any("行動" in v for v in _checks("R-revisit-browsing", later))
    for mention in ("最近はカフェのお仕事をご覧になっていましたか?", "最近カフェのお仕事をご覧になりましたか?",
                    "最近はカフェのお仕事を見てましたか?", "最近カフェの求人をチェックされていましたか?",
                    "最近カフェのお仕事を探していましたか?", "最近カフェのお仕事を探されていましたか?",
                    "最近はカフェのお仕事をお探しでしたか?", "最近カフェのお仕事が気になっていましたか?",
                    "最近カフェの求人をよく見てくださっていましたね。今日も探しますか?",
                    "最近カフェの求人を見ていましたね。どれか気になっていましたか?",  # 1通目の言及と追いかけの問いで1回
                    "最近カフェの求人を見ていましたね。今も探していらっしゃいますか?"):
        once = [dict(ok[0], bubbles=[mention]), ok[1]]
        assert not any("行動" in v or "羅列" in v for v in _checks("R-revisit-browsing", once)), mention
    for listing in ("最近はカフェホール 渋谷道玄坂などを見ていましたか?", "最近3件の求人を見ていましたか?",
                    "最近はカフェやアパレル販売を見ていましたか?"):
        listed = [dict(ok[0], bubbles=[listing]), ok[1]]
        assert any("羅列" in v for v in _checks("R-revisit-browsing", listed)), listing
