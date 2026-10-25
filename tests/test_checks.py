"""事実性のチェック(evals/checks.py の fact_violations)。LLM は使わない。"""

from __future__ import annotations

import pytest

from evals.checks import (
    area_violations,
    asks_area,
    conversation_checks,
    experience_keyword_violations,
    fact_violations,
    preference_violations,
)


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


# ----------------------------- aica v2 の E: 自由記述の作り話の判定(D の再レビューの指摘) -----------------------------


def _saved(prefs):
    t = turn(["承知しました。"], [("save_user_preferences", {"saved": list(prefs)})])
    t["tools"][0]["args"] = {"preferences": prefs}
    return t


def test_one_unsaid_content_word_in_free_text_is_a_fabrication():
    # 多数決では通っていた: 発言に無い「人間関係」が1語だけ混ざる
    v = preference_violations([_saved({"concerns": "未経験で人間関係が不安"})], ["未経験でついていけるかが不安です"])
    assert v == ["T1: 本人が言っていない希望を保存した: concerns=未経験で人間関係が不安"]


def test_free_text_rewordings_are_not_fabrications():
    said = ["接客の経験を活かしたい", "未経験でもついていけるかが不安です", "人と話すのが好きです"]
    ok = {"purpose": "接客経験を活かしたい", "concerns": "未経験でついていけるか", "wants": "人と話すこと"}
    assert preference_violations([_saved(ok)], said) == []


def test_purpose_may_be_named_by_its_kind():
    # 目的は種類の名前でまとめてよい(「空いた時間にバイトを」→「副業」)。種類でない語は作り話のまま
    said = ["本業の後の空いた時間でバイトをしたい", "少し収入を増やしたい"]
    assert preference_violations([_saved({"purpose": "副業で収入を増やしたい"})], said) == []
    v = preference_violations([_saved({"purpose": "副業でキャリアを広げたい"})], said)
    assert v == ["T1: 本人が言っていない希望を保存した: purpose=副業でキャリアを広げたい"]


def test_values_without_content_words_are_checked_word_by_word():
    # 「仕事」「やりがいのある職場」は内容語が残らないので、今までは必ず通っていた
    assert preference_violations([_saved({"wants": "やりがいのある職場"})], ["とにかく稼ぎたい"])
    assert preference_violations([_saved({"wants": "仕事"})], ["とにかく稼ぎたい"])
    assert preference_violations([_saved({"wants": "やりがいのある職場"})], ["やりがいのある職場がいい"]) == []


def test_kanji_compound_that_is_not_split_in_what_was_said_is_still_a_fabrication():
    # 切り分けた語がどちらも発言に無ければ、作り話のまま
    assert preference_violations([_saved({"purpose": "接客経験を活かしたい"})], ["事務の仕事を活かしたい"])


# ----------------------------- aica v2 の E: 金額の照合(A のレビューの指摘) -----------------------------

MONTHLY = ("recommend_jobs", {"jobs": [{"job_id": "f0104", "employment_type": "正社員",
                                        "pay": {"kind": "月給", "min": 260000, "max": 350000}}]})


def test_monthly_to_annual_conversion_of_dip_pay_is_allowed():
    turns = [turn(["月給35万円なので、12か月で年収420万円ほどです。"], [MONTHLY])]
    assert fact_violations(turns, ["年収にするとどのくらい?"], None) == []


def test_annual_amount_that_is_not_a_conversion_is_still_a_violation():
    v = fact_violations([turn(["月給35万円なので、年収500万円ほどです。"], [MONTHLY])], ["年収は?"], None)
    assert any("500万円" in x for x in v)


def test_pay_is_matched_by_its_kind():
    # 年収800万の求人があっても「月給800万円」は作り話。年収の額を月給として言い換えない
    annual = ("recommend_jobs", {"jobs": [{"job_id": "f0201", "pay": {"kind": "年収", "min": 8000000, "max": None}}]})
    assert fact_violations([turn(["年収800万円の求人です。"], [annual])], ["正社員で"], None) == []
    v = fact_violations([turn(["月給800万円の求人です。"], [annual])], ["正社員で"], None)
    assert any("800万円" in x for x in v)
    v = fact_violations([turn(["年収26万円からです。"], [MONTHLY])], ["正社員で"], None)
    assert any("26万円" in x for x in v)


def test_general_knowledge_and_estimates_are_not_dip_claims():
    turns = [turn(["扶養内(年収103万円以下)で探しますね。"], [SUMMARY]),
             turn(["週3日・1日5時間なら月に約7万円です。"]),
             turn(["月1万5千円くらいのお小遣いになります。"])]
    assert fact_violations(turns, ["扶養内で働きたい"], None) == []


def test_fabricated_pay_facts_near_those_phrases_are_still_violations():
    turns = [turn(["交通費は月1万5千円まで支給です。"], [SUMMARY]),
             turn(["年収約800万円の求人があります。"]),
             turn(["扶養内で、年収200万円の求人です。"]),
             turn(["月28万円の求人があります。", "月に約30万円の求人です。", "月20万円です。"])]
    v = fact_violations(turns, ["扶養内で働きたい"], None)
    assert any("T4" in x and "「28万円」" in x for x in v) and any("T4" in x and "「30万円」" in x for x in v)
    assert any("T4" in x and "「20万円」" in x for x in v)
    assert any("T1" in x and "1.5万円" in x for x in v)
    assert any("T2" in x and "800万円" in x for x in v)
    assert any("T3" in x and "200万円" in x for x in v)


def test_amount_said_in_kanji_numerals_can_be_repeated():
    assert fact_violations([turn(["年収600万円以上で探しますね。"], [SUMMARY])], ["年収は六百万以上がいい"], None) == []
    assert fact_violations([turn(["月給35万円以上ですね。"], [SUMMARY])], ["月給は三十五万くらい"], None) == []
    v = fact_violations([turn(["年収700万円以上で探しますね。"], [SUMMARY])], ["年収は六百万以上がいい"], None)
    assert any("700万円" in x for x in v)
    # 漢数字を含む普通の語(一緒・一つ)は数として読まない
    v = fact_violations([turn(["1件あります。"], [SUMMARY])], ["一緒に一つ探して"], None)
    assert any("1件" in x for x in v)


def test_amount_said_with_arabic_digits_and_kanji_units_is_one_number():
    """「年収1千万」を「11000万」と読まない(E の PR レビューの指摘)。正しく繰り返した「1000万円」は違反にしない。"""
    for said in ("年収は1千万以上がいい", "年収は一千万以上がいい", "年収は1,000万以上がいい"):
        assert fact_violations([turn(["年収1000万円以上で探しますね。"], [SUMMARY])], [said], None) == [], said
    assert fact_violations([turn(["3000円の差です。"], [SUMMARY])], ["時給で3千円違う"], None) == []
    v = fact_violations([turn(["年収1100万円以上で探しますね。"], [SUMMARY])], ["年収は1千万以上がいい"], None)
    assert any("1100万円" in x for x in v)
    # 算用数字のあとの普通の語(「2人一緒に」)は数として読まない
    v = fact_violations([turn(["21件あります。"], [SUMMARY])], ["2人一緒に探して"], None)
    assert any("21件" in x for x in v)


def test_estimate_wording_about_a_job_is_still_a_pay_claim():
    """「こちらは月に約30万円です」は求人の給与の主張(目安の言い方でも照合する)。E の PR レビューの指摘。"""
    for line in ("こちらは月に約30万円です。", "このお仕事は月に約30万円です。", "1件目は月に約30万円です。"):
        v = fact_violations([turn([line], [SUMMARY])], ["正社員で"], None)
        assert any("30万円" in x for x in v), line
    # 本人の働き方から計算した目安は、今までどおり主張に数えない(負例)
    ok = [turn(["週3日・1日5時間なら月に約7万円です。"], [SUMMARY]), turn(["月1万5千円くらいのお小遣いになります。"])]
    assert fact_violations(ok, ["週3日で働きたい"], None) == []


def test_short_fulltime_job_type_names_inside_other_words_are_not_a_list():
    # 正社員の職種名(営業・教育・物流)は短く、「営業時間」「研修・教育制度」の中にも出る。それは職種の羅列ではない
    fine = turn(["おかえりなさい。最近は営業時間が短めのカフェのお仕事を見ていましたか?"])
    fine["quick_replies"] = ["今はいい"]
    assert not any("羅列" in v for v in _checks("R-revisit-browsing", [fine]))
    listed = turn(["おかえりなさい。最近はカフェや物流のお仕事を見ていましたか?"])
    listed["quick_replies"] = ["今はいい"]
    assert any("羅列" in v for v in _checks("R-revisit-browsing", [listed]))


# ----------------------------- aica v2 の F: E のレビューからの後続指摘(評価器の偽陽性・見逃し) -----------------------------


def test_stated_monthly_pay_converted_to_annual_is_not_a_fabrication():
    # 本人が言った月給を年収に換算するのは計算(「月給30万以上」→「年収だと360万円」)
    turns = [turn(["月給30万円以上ですね。年収だと360万円以上で探します。"])]
    assert fact_violations(turns, ["月給30万以上がいい"], None) == []
    # 換算でない年収は作り話のまま。本人が年収で言った額を12倍しても根拠にならない
    assert any("400万円" in v for v in fact_violations([turn(["年収だと400万円以上ですね。"])], ["月給30万以上"], None))
    assert any("360万円" in v for v in fact_violations([turn(["年収360万円ですね。"])], ["年収30万以上"], None))


def test_purpose_kind_needs_a_cue_in_what_was_said():
    # 「とにかく稼ぎたい」からは「経験を活かしたい」「スキル活用」を作れない(種類の名前にも本人の手がかりが要る)
    for purpose in ("経験を活かしたい", "スキル活用"):
        v = preference_violations([_saved({"purpose": purpose})], ["とにかく稼ぎたい"])
        assert v == [f"T1: 本人が言っていない希望を保存した: purpose={purpose}"], purpose
    # 手がかりがあれば、種類の名前でまとめてよい
    assert preference_violations([_saved({"purpose": "生活費"})], ["とにかく稼ぎたい"]) == []
    assert preference_violations([_saved({"purpose": "スキル活用"})], ["PMの経験を活かしたい"]) == []
    assert preference_violations([_saved({"purpose": "学業と両立"})], ["授業の後に働きたい"]) == []


def test_ordinary_summaries_in_free_text_are_not_fabrications():
    # 接尾語(業・職・勤務)と、よくある言い換え(ぶり → ブランク、子ども → 子育て、やってきた → 経験)
    cases = [("wants", "接客業", "接客の仕事がしたい"),
             ("wants", "企画職", "企画をやりたい"),
             ("avoids", "深夜勤務", "深夜は避けたい"),
             ("concerns", "ブランクがあること", "10年ぶりに働くので不安"),
             ("concerns", "子育てとの両立", "子どもが小学校に上がったので、その間に働きたい"),
             ("strengths", "法人営業の経験", "法人営業を4年やってきました")]
    for key, value, said in cases:
        assert preference_violations([_saved({key: value})], [said]) == [], value


def test_suffixes_and_paraphrases_do_not_let_unsaid_words_through():
    # 語幹が発言に無ければ、接尾語を外しても作り話(「人間関係」は「関係」を外しても「人間」が無い)
    assert preference_violations([_saved({"concerns": "人間関係"})], ["未経験でついていけるか不安"])
    assert preference_violations([_saved({"wants": "事務職"})], ["接客がしたい"])
    assert preference_violations([_saved({"concerns": "ブランクがあること"})], ["未経験で不安"])
    assert preference_violations([_saved({"strengths": "法人営業の経験"})], ["法人営業に興味があります"])


def _reask(bubble, saved, said):
    from evals.checks import reask_violations

    turns = [{"bubbles": ["はい"], "cards": [], "quick_replies": [],
              "tools": [{"name": "save_user_preferences", "args": {"preferences": saved}}]},
             {"bubbles": [bubble], "cards": [], "quick_replies": [], "tools": []}]
    return reask_violations(turns, [None, said])


def test_narrowing_after_a_rejected_job_is_not_a_reask():
    # 本人が見送った理由を受けて絞り込む問いは、時間帯の聞き直しではない(T-side-job#1 の T6)
    said = "本業に響く深夜は避けたいので、この求人は見送ります。"
    assert _reask("この求人は見送るのですね。深夜を避けるなら、何時ごろまでの勤務がよいですか？",
                  {"time_band": "平日の夜か土日"}, said) == []
    # 本人がそのターンに言っていない語での「なら」や、避ける語の無い「なら」は、白紙の聞き直しのまま
    assert _reask("深夜を避けるなら、何時ごろまでの勤務がよいですか?", {"time_band": "夜"}, "この求人は見送ります")
    assert _reask("それなら、どの時間帯がよいですか?", {"time_band": "夜"}, "深夜はちょっと")


def test_where_are_you_stuck_is_not_an_area_reask():
    # 「どのあたり」は場所の文脈のときだけ場所を聞く問い
    for q in ("どのあたりで悩んでいますか?", "どのあたりがネックですか?", "どのあたりに興味がありますか?",
              "どのあたりが良いと思いましたか?", "どのあたりで迷っていますか?", "どのあたりが気になっていますか?"):
        assert _reask(q, {"area": "渋谷"}, "考えます") == [], q
    for q in ("どのあたりで働きたいですか?", "どのあたりで探しますか?", "どのあたりをご希望ですか?",
              "どの辺りが通いやすいですか?", "どのエリアで探しますか?"):
        assert _reask(q, {"area": "渋谷"}, "考えます"), q


def test_job_type_saved_as_the_list_name_of_what_was_said_is_not_a_fabrication():
    # 「販売の仕事で店長を目指したい」→ job_type は正社員の一覧の名前「販売・サービス」(T-switch-to-fulltime の調整1回目)
    assert preference_violations([_saved({"job_type": "販売・サービス"})], ["販売の仕事で店長を目指したい"]) == []
    # 一覧のどの部分も言っていなければ作り話のまま。一覧に無い値は語ごとに照合する
    assert preference_violations([_saved({"job_type": "営業"})], ["販売の仕事で店長を目指したい"])
    assert preference_violations([_saved({"job_type": "販売と事務"})], ["販売の仕事"])


def test_confirming_a_saved_value_by_its_word_and_digging_into_wants_are_not_reasks():
    # 調整2回目の T-switch-to-fulltime で出た偽陽性
    assert _reask("お店の運営に興味があり、年収350万円以上をご希望ですね。勤務地は、先ほどの渋谷周辺で探しますか？",
                  {"area": "渋谷あたり"}, "お店の運営に興味があります") == []
    assert _reask("店長を目指したいとのことですが、店長としてどんな仕事をしてみたいですか？",
                  {"job_type": "販売・サービス"}, "月ごとに収入が変わるときです") == []
    # 白紙の問い・別の値は聞き直しのまま
    assert _reask("勤務地はどこがいいですか?", {"area": "渋谷あたり"}, "はい")
    assert _reask("勤務地は新宿で探しますか?", {"area": "渋谷あたり"}, "はい")
    assert _reask("どんな仕事を探していますか?", {"job_type": "販売・サービス"}, "はい")


# ----------------------------- task 130: H のレビューからの評価器の誤判定 -----------------------------


def test_estimates_with_a_job_pointer_or_a_numbered_goal_are_not_pay_claims():
    """本人の働き方(週の日数・時間)で計算した文の目安と、「1つ目の目標」は求人の給与の主張ではない(H のレビュー)。"""
    said = ["週3日、1日5時間くらいで働きたい。週2日・4時間でもいいです"]
    for line in ("こちらは週3日・1日5時間なら月に約7万円です。", "1つ目の目標は月に約7万円ですね。",
                 "このお仕事だと、週に2日・4時間で月に約3万円くらいです。"):
        assert fact_violations([turn([line], [SUMMARY])], said, None) == [], line
    # 求人の事実を作った文は、今までどおり作り話(正例)。本人が言っていない勤務条件を添えても、目安にはならない
    for line in ("こちらは月に約30万円です。", "1つ目のお仕事は月に約30万円です。", "2件目は月に約30万円です。",
                 "週3日の求人で、月給約30万円です。", "こちらは週5日・1日8時間勤務で月に約30万円です。",
                 "この仕事は1日8時間で月に約30万円です。", "こちらは週3日・1日8時間で月に約30万円です。",
                 "2つ目は月に約30万円です。"):
        v = fact_violations([turn([line], [SUMMARY])], ["週3日で働きたい"], None)
        assert any("30万円" in x for x in v), line


def test_saved_preference_whose_meaning_is_reversed_is_a_fabrication():
    """意味が逆になる保存は作り話(task 130 のレビュー)。言い換えを見分ける照合は入れないので、main と同じく作り話に数える。"""
    for said, prefs in (("土日は働けません。平日なら働けます。", {"days_per_week": "土日なら働けます"}),
                        ("土日は働けませんが、平日なら働けます。", {"days_per_week": "土日なら働けます"}),
                        ("渋谷は遠いので避けたいです。新宿がいいです", {"area": "渋谷がいい"}),
                        ("接客は苦手です。事務がいいです", {"job_type": "接客がいい"}),
                        ("夜は寝たいので、勤務は昼がいいです", {"time_band": "夜勤"}),
                        ("土曜日は家族と過ごすので、日曜なら働けます", {"days_per_week": "土日"}),
                        ("渋谷ではなく新宿がいいです", {"area": "渋谷がいい"}),
                        ("接客じゃなくて事務がいいです", {"job_type": "接客がいい"}),
                        ("渋谷より新宿がいいです", {"area": "渋谷がいい"}),
                        ("土日はちょっと、平日がいいです", {"days_per_week": "土日がいいです"}),
                        ("渋谷はもういいです、新宿で働きたい", {"area": "渋谷で働きたい"}),
                        ("接客ではなく事務の仕事がいい", {"job_type": "接客の仕事がいい"}),
                        ("接客より事務の仕事がいい", {"job_type": "接客の仕事がいい"}),
                        # 「X の Y」の X を差し替えた保存(task 130 の 4 回目のレビュー)
                        ("夜は寝たいです、昼の勤務がいいです", {"time_band": "夜の勤務がいい"}),
                        ("渋谷はもういい、新宿の仕事がいい", {"area": "渋谷の仕事がいい"}),
                        ("渋谷はやめて新宿の仕事がいい", {"area": "渋谷の仕事がいい"}),
                        ("接客は卒業して事務の仕事がいい", {"job_type": "接客の仕事がいい"}),
                        ("接客はもう十分、事務の仕事がいい", {"job_type": "接客の仕事がいい"}),
                        ("接客は疲れた、事務の仕事がいい", {"job_type": "接客の仕事がいい"}),
                        ("土日は休みたい、平日の仕事がいい", {"days_per_week": "土日の仕事がいい"})):
        k, v = next(iter(prefs.items()))
        assert preference_violations([_saved(prefs)], [said]) == [f"T1: 本人が言っていない希望を保存した: {k}={v}"], said


# ----------------------------- 最初の求人はエリアが分かってから(task 224) -----------------------------

AREA_UNKNOWN = "エリアが分からないまま recommend_jobs で探した(先にエリアを1問で聞く)"


def t(bubbles, tools=(), quick_replies=("今はいい",), cards=()):
    """(ツール名, args) か (ツール名, args, result) の呼び出しを持つ1ターン。"""
    return {"bubbles": list(bubbles), "quick_replies": list(quick_replies), "cards": list(cards), "buttons": [],
            "tools": [{"name": x[0], "args": x[1], "result": x[2] if len(x) > 2 else {}} for x in tools],
            "unknown_job_ids": []}


def rec(**args):
    return ("recommend_jobs", args)


def summary(**prefs):
    return ("get_user_conversation_summary", {}, {"stated_preferences": {
        k: {"value": v, "saved": "10/20"} for k, v in prefs.items()}})


GREETING = t(["はじめまして。どうしますか?"], quick_replies=["求人を探す", "まず相談する", "今はいい"])
CLOSE = ["求人を探す", "応募履歴を確認", "使い方"]


def follow_new(last_tools, said3="カフェで働きたいです"):
    """#221 の F-follow-new の形: 区分 → 仕事 → 4 ターン目に探す。"""
    turns = [GREETING, t(["どちらで探しますか?"], quick_replies=["アルバイト・パート", "今はいい"]),
             t(["どんなお仕事で探しますか?"], quick_replies=["カフェ", "それ以外", "今はいい"]),
             t(["3件あります。"], last_tools, quick_replies=CLOSE, cards=[{"job_id": "b0101", "reasons": ["カフェ"]}])]
    return turns, [None, "求人を探す", "アルバイト・パート", said3]


def test_search_before_the_area_is_known_is_a_violation():
    # #221 の F-follow-new T4: 「カフェ」だけで探し、大宮・中野・吉祥寺の求人を出した
    turns, said = follow_new([rec(job_type="カフェ")])
    assert area_violations(turns, said) == [f"T4: {AREA_UNKNOWN}"]
    assert f"T4: {AREA_UNKNOWN}" in conversation_checks({"expect_outcome": ["propose"]}, turns, said)["violations"]
    # #221 の C-consult-switch T4: 「求人を見せて」で、引数がすべて null
    turns, said = follow_new([rec(area=None, job_type=None, keywords=None)], "求人を見せて")
    assert area_violations(turns, said) == [f"T4: {AREA_UNKNOWN}"]


def test_known_area_left_out_of_the_search_is_a_violation():
    turns, said = follow_new([rec(job_type="カフェ")], "渋谷か池袋のカフェで")
    assert area_violations(turns, said) == ["T4: 分かっているエリア(渋谷)を recommend_jobs の area に入れずに探した"]
    # 要約の stated_preferences にエリアがあるとき(再訪)
    turns, said = follow_new([rec(job_type="カフェ")])
    turns[0] = t(["前回は新宿でしたが、今回も同じですか?"], [summary(area="新宿")])
    assert area_violations(turns, said) == ["T4: 分かっているエリア(新宿)を recommend_jobs の area に入れずに探した"]


def test_search_with_the_area_is_fine():
    turns, said = follow_new([rec(job_type="カフェ", area="渋谷")], "渋谷のカフェで")
    assert area_violations(turns, said) == []
    # 本人が挙げた2つのエリアを並べて探すのも同じ
    turns, said = follow_new([rec(job_type="カフェ", area="渋谷"), rec(job_type="カフェ", area="池袋")], "渋谷か池袋で")
    assert area_violations(turns, said) == []


@pytest.mark.parametrize("said3", ["カフェで、場所はどこでもいいです", "カフェで。エリアはこだわりません",
                                   "勤務地は特にないです", "在宅でできる仕事がいいです"])
def test_area_left_open_by_the_person_is_not_a_violation(said3):
    turns, said = follow_new([rec(job_type="カフェ")], said3)
    assert area_violations(turns, said) == []


def test_no_preference_answer_to_the_area_question_is_not_a_violation():
    turns, said = follow_new([rec(job_type="カフェ")], "特にないです")
    turns[2] = t(["どのエリアで探しますか?"], quick_replies=["渋谷", "池袋", "今はいい"])
    assert area_violations(turns, said) == []
    # 同じ「特にないです」でも、エリアを聞いていない問い(やりたいこと)への答えなら、エリアは分かっていない
    turns[2] = t(["やってみたいことはありますか?"])
    assert area_violations(turns, said) == [f"T4: {AREA_UNKNOWN}"]


def test_area_named_in_the_answer_is_not_left_open():
    # 「どのエリアで、どんなお仕事を」への答えの「特にこだわりません」は仕事のこと。地名があればエリアは分かっている
    turns, said = follow_new([rec(area=None)], "渋谷で。仕事内容は特にこだわりません")
    turns[2] = t(["どのエリアで、どんなお仕事を探しますか？"])
    assert area_violations(turns, said) == ["T4: 分かっているエリア(渋谷)を recommend_jobs の area に入れずに探した"]
    # 問わないと言った後に地名を言い直したら、エリアは決まっている
    turns, said = follow_new([rec(job_type="カフェ")], "やっぱり池袋で")
    said[2] = "場所はどこでもいいです"
    assert area_violations(turns, said) == ["T4: 分かっているエリア(池袋)を recommend_jobs の area に入れずに探した"]


@pytest.mark.parametrize("said3", ["在宅じゃなくて、カフェで働きたい", "リモートでなくていいです",
                                   "リモートは無理なので、カフェで働きたい", "在宅ワークはしたくないです。カフェで",
                                   "リモートの仕事は興味ないです、カフェがいい"])
def test_ruling_out_remote_does_not_leave_the_area_open(said3):
    turns, said = follow_new([rec(job_type="カフェ")], said3)
    assert area_violations(turns, said) == [f"T4: {AREA_UNKNOWN}"]


@pytest.mark.parametrize(("said3", "area"), [("リモートは無理なので、渋谷のカフェで働きたい", "渋谷"),
                                             ("在宅は苦手です。池袋のカフェで", "池袋"),
                                             ("リモートが週2日あるとよいです。場所は渋谷で", "渋谷")])
def test_remote_words_with_an_area_do_not_leave_the_area_open(said3, area):
    # 地名のある発言は、在宅・リモートの言い方があっても、エリアが分かっている
    turns, said = follow_new([rec(job_type="カフェ")], said3)
    assert area_violations(turns, said) == [f"T4: 分かっているエリア({area})を recommend_jobs の area に入れずに探した"]


def test_dropping_the_area_without_widening_after_an_area_search_is_a_violation():
    # エリアつきで探した後、本人が別の地名を言ったのに、area を外して探した
    turns, said = follow_new([rec(job_type="カフェ", area="渋谷")], "渋谷のカフェで")
    turns.append(t(["池袋のカフェです。"], [rec(job_type="カフェ")], quick_replies=CLOSE))
    assert area_violations(turns, said + ["やっぱり池袋で探したい"]) == [
        "T5: 分かっているエリア(池袋)を recommend_jobs の area に入れずに探した"]
    # relax_conditions の案内の後でも、本人が新しい地名を言ったら、広げたのではない
    turns.insert(4, t(["エリアを外すと増えます。外しますか?"], [("relax_conditions", {"area": "渋谷"})]))
    assert area_violations(turns, said + ["もっと見たい", "それより池袋で"]) == [
        "T6: 分かっているエリア(池袋)を recommend_jobs の area に入れずに探した"]
    # relax_conditions も「広げて」も無いまま、エリアを外した
    turns, said = follow_new([rec(job_type="カフェ", area="渋谷")], "渋谷のカフェで")
    turns.append(t(["ほかの求人です。"], [rec(job_type="カフェ")], quick_replies=CLOSE))
    assert area_violations(turns, said + ["ほかのも見たい"]) == [
        "T5: 分かっているエリア(渋谷)を recommend_jobs の area に入れずに探した"]
    # 本人が「広げて」と言ったなら、エリアを外してよい
    assert area_violations(turns, said + ["もう少し広げて見たい"]) == []


def test_remote_search_and_widening_after_an_area_search_are_not_violations():
    turns, said = follow_new([rec(employment_type="正社員", remote="可")], "フルで働ける正社員の仕事")
    assert area_violations(turns, said) == []
    # 前のターンでエリアつきで探した後、本人の了承でエリアを外して広げる(relax_conditions の案内の後)
    turns, said = follow_new([rec(job_type="カフェ", area="渋谷")], "渋谷のカフェで")
    turns += [t(["エリアを外すと12件に増えます。外して探しますか?"], [("relax_conditions", {"area": "渋谷"})]),
              t(["広げて探しました。"], [rec(job_type="カフェ")], quick_replies=CLOSE)]
    assert area_violations(turns, said + ["0件でした", "はい、外してください"]) == []


def test_experience_goes_into_the_keywords():
    exp = ("save_user_preferences", {"preferences": {"experience": "居酒屋のホールを1年"}})
    turns, said = follow_new([rec(area="渋谷")], "求人を見せて")
    turns[2]["tools"] = [{"name": exp[0], "args": exp[1], "result": {}}]
    assert experience_keyword_violations(turns) == [
        "T4: 分かっている経験「居酒屋のホールを1年」を recommend_jobs の keywords に入れていない(keywords=None)"]
    # 指標は検索の引数の違反(search_arg_violations)として出る。会話の違反(violations)には入れない
    r = conversation_checks({"expect_outcome": ["propose"]}, turns, said)
    assert r["search_arg_violations"] == experience_keyword_violations(turns)
    assert not any("経験" in v for v in r["violations"])
    for kw in ("居酒屋 ホール", "ホール経験", "居酒屋のホールを1年"):
        turns[3]["tools"][0]["args"]["keywords"] = kw
        assert experience_keyword_violations(turns) == [], kw
    # 経験の語の無い keywords は、入れていないのと同じ
    turns[3]["tools"][0]["args"]["keywords"] = "日払い"
    assert len(experience_keyword_violations(turns)) == 1


def test_experience_from_the_summary_and_only_the_first_search_is_judged():
    turns, _ = follow_new([rec(area="渋谷", keywords="カフェ")])
    turns[0] = t(["前回のお話の続きから伺います。どうしますか?"], [summary(experience="未経験")])
    assert experience_keyword_violations(turns) == [
        "T4: 分かっている経験「未経験」を recommend_jobs の keywords に入れていない(keywords='カフェ')"]
    turns[3]["tools"][0]["args"]["keywords"] = "カフェ 未経験"
    # 最初の検索で入れていれば、後で本人が別の方向を言って探し直したときは問わない
    turns.append(t(["事務の求人です。"], [rec(area="渋谷", job_type="オフィス事務")], quick_replies=CLOSE))
    assert experience_keyword_violations(turns) == []
    # 経験が分からなければ、何も見ない
    turns, _ = follow_new([rec(area="渋谷")])
    assert experience_keyword_violations(turns) == []


def test_the_area_question_is_not_counted_for_branch_within_3():
    sc = {"expect_outcome": ["propose"]}
    # 仕事 → エリア → 4 ターン目に探す: エリアの1問は数えないので、3回目の返信までに分岐したことになる
    turns, said = follow_new([rec(job_type="カフェ", area="渋谷")], "渋谷です")
    turns[2] = t(["どのエリアで探しますか?"], quick_replies=["それ以外", "今はいい"])
    assert conversation_checks(sc, turns, said)["branch_within_3"] is True
    # エリア以外の条件(時間帯)を聞いて遅れたのは、これまでどおり期限に数える
    turns[2] = t(["働きたい時間帯はありますか?"], quick_replies=["日中", "それ以外", "今はいい"])
    assert conversation_checks(sc, turns, said[:3] + ["日中です"])["branch_within_3"] is False
    # 数えないのは1問だけ(エリアを聞いた後、さらに時間帯を聞いて 5 ターン目に探すのは遅い)
    turns, said = follow_new([rec(job_type="カフェ", area="渋谷", time_band="日中")], "日中です")
    turns[2] = t(["どのエリアで探しますか?"], quick_replies=["それ以外", "今はいい"])
    turns.insert(3, t(["働きたい時間帯はありますか?"]))
    assert conversation_checks(sc, turns, said[:3] + ["渋谷です", "日中です"])["branch_within_3"] is False


CONSULT_SC = {"expect_outcome": ["propose"], "consult_mode": True}
READ_PLAYBOOK = ("get_counseling_playbook", {"track": "未定", "channel": "chat"})


def consult_switch(ask: str, last_tools):
    """C-consult-switch の形: 相談 → 経験 → 「求人を見せて」→ ask の1問 → 次のターンで探す。"""
    turns = [GREETING, t(["いくつか伺いながら一緒に整理します。経験はありますか?"], [READ_PLAYBOOK]),
             t(["きっかけは何ですか?"], [("save_user_preferences", {"preferences": {"experience": "居酒屋のホール"}})]),
             t([ask], quick_replies=["それ以外", "今はいい"]),
             t(["渋谷の3件です。"], last_tools, quick_replies=CLOSE, cards=[{"job_id": "b0101", "reasons": ["渋谷"]}])]
    said = [None, "まず相談する", "居酒屋のホールをしていました", "求人を見せて", "渋谷でお願いします"]
    return turns, said


def test_show_me_jobs_may_wait_for_the_one_area_question():
    search = [rec(area="渋谷", keywords="居酒屋 ホール"), ("record_conversation_outcome", {"outcome": "propose"})]
    turns, said = consult_switch("どのエリアで探しますか?", search)
    r = conversation_checks(CONSULT_SC, turns, said)
    assert r["violations"] == [] and r["search_arg_violations"] == []
    # エリアの1問でも、答えを受けたターンで探さなければ違反のまま
    turns[4] = t(["時間帯はどうしますか?"])
    assert "T4: 「求人を見せて」の後に求人を探さなかった" in conversation_checks(CONSULT_SC, turns, said)["violations"]
    # エリアの1問の後に「今はいい」なら、探さずに終えてよい
    turns[4] = t(["承知しました。"], [("record_conversation_outcome", {"outcome": "consult"})], quick_replies=CLOSE)
    v = conversation_checks(CONSULT_SC, turns, said[:4] + ["今はいい"])["violations"]
    assert not any("求人を見せて" in x for x in v), v


def test_show_me_jobs_followed_by_other_conditions_is_still_a_violation():
    # ほかの条件(時間帯)は聞き足さない: エリア以外の問いで探すのを遅らせたら違反
    search = [rec(area="渋谷", keywords="居酒屋 ホール"), ("record_conversation_outcome", {"outcome": "propose"})]
    turns, said = consult_switch("働きたい時間帯はありますか?", search)
    assert "T4: 「求人を見せて」の後に求人を探さなかった" in conversation_checks(CONSULT_SC, turns, said)["violations"]


def test_area_wording_that_keeps_a_preference_is_not_left_open():
    # 「こだわりはあります」はエリアを問わない言い方ではない
    turns, said = follow_new([rec(job_type="カフェ")], "場所のこだわりはあります")
    assert area_violations(turns, said) == [f"T4: {AREA_UNKNOWN}"]


def test_prompt_and_playbook_state_the_area_rule():
    from dip_mcp.data import ROOT

    prompt = (ROOT / "dip_mcp" / "prompts" / "baitoru_assistant.md").read_text(encoding="utf-8")
    common = (ROOT / "dip_mcp" / "playbooks" / "common_chat.md").read_text(encoding="utf-8")
    assert "最低限の条件はエリア" in prompt and "エリアが無ければエリアだけを1問で聞いてから探す" in prompt
    assert "エリアと仕事の希望をまとめて1問で聞く" in prompt
    assert "探す前にエリアを聞く1問(上の「提案」)は、この回数に数えない" in prompt
    assert "keywords に本人の言葉で入れる" in prompt
    assert "エリアの1問だけを聞いてから探す" in common and "経験とやりたいことは keywords" in common


@pytest.mark.parametrize(("ask", "is_area"), [
    ("どのエリアで探しますか?", True), ("どのエリアで、どんなお仕事を探しますか?", True),
    ("どのあたりで、どんなお仕事を探しますか?", True), ("最寄りの駅はどこですか?", True),
    ("どこで働きたいですか?", True), ("勤務地の希望はありますか?", True),
    # 区分の問い・迷いの問い・ほかの条件の問いは、エリアの問いではない
    ("正社員とアルバイト・パート、どちらで探しますか?", False), ("どのあたりで迷っていますか?", False),
    ("どこが気になりますか?", False), ("働きたい時間帯はありますか?", False),
    ("エリアが渋谷の求人です。", False),  # 疑問符の無い文は問いではない
])
def test_area_question_detection(ask, is_area):
    assert asks_area(t([ask])) is is_area


def test_show_me_jobs_area_question_must_not_add_other_conditions():
    # 実 API の 2 回目(task 224)の C-consult-switch T4: 「求人を見せて」にエリアと仕事をまとめて聞いた
    search = [rec(area="渋谷", keywords="居酒屋 ホール"), ("record_conversation_outcome", {"outcome": "propose"})]
    turns, said = consult_switch("求人を探します。どのエリアで、どんなお仕事を探しますか?", search)
    v = conversation_checks(CONSULT_SC, turns, said)["violations"]
    assert v == ["T4: 「求人を見せて」の後に、エリアのほかの条件(job_type)も聞いた"]
    turns[3] = t(["どのエリアで、何時ごろ働きたいですか?"])
    v = conversation_checks(CONSULT_SC, turns, said)["violations"]
    assert v == ["T4: 「求人を見せて」の後に、エリアのほかの条件(time_band)も聞いた"]
    # 「求人を探す」を選んだだけ(「求人を見せて」ではない)なら、まとめて聞いてよい
    turns, said = follow_new([rec(area="渋谷", job_type="カフェ")], "渋谷のカフェで")
    turns[1] = t(["どのエリアで、どんなお仕事を探しますか?"])
    sc = {"expect_outcome": ["propose"]}
    assert conversation_checks(sc, turns[:2] + turns[3:], said[:2] + said[3:])["violations"] == []
