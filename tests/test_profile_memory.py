"""保存項目の拡張と相談のまとめ(aica v2 の D。jyoka/tasks#61 を吸収)のテスト。実 API なし、dip の MCP は本物。

確かめること:
(1) 新しい項目(目的・強み・やりたいこと・避けたいこと・不安、正社員の条件)が保存され、要約に出る。
    個人情報らしい値は全項目で弾かれ、年収の桁表記は個人情報に数えない。
(2) 相談のまとめ(200字以内)が記録されて要約に出る。200字を超えると記録されない。
(3) チャットで保存した項目と相談のまとめが、音声アドバイザーの指示に入る(PR #10 と同じ作り: 本物の dip MCP を stdio で)。
(4) 音声で保存した項目が、次のチャットの要約に出る。
(5) 評価の「聞き直し」の判定が新しい項目に効き、確認・掘り下げの問いは違反にしない。
"""

from __future__ import annotations

import asyncio

import pytest

from dip_mcp import logic
from dip_mcp.data import PII_VALUE, State
from evals.checks import conversation_checks, reask_violations, turn_violations
from tests.test_handle import ScriptedModel, call, reply, run
from voice_resume.advisor import ResumeAdvisor
from voice_resume.agent import prepare_advisor
from voice_resume.dip import DipMCP

UID = "w-3333333333333333:sim-new"
NEW_ITEMS = {
    "purpose": "前職の経験を活かしたい",
    "strengths": "法人営業で5年、新規開拓が得意",
    "wants": "企画の仕事に挑戦したい",
    "avoids": "毎日の飛び込み営業は避けたい",
    "concerns": "未経験の職種で通用するか不安",
    "min_annual_salary": "年収500万円以上",
    "work_style": "リモート中心、残業少なめ",
    "start_timing": "3か月以内",
}
SUMMARY_TEXT = ("前職の経験(法人営業5年)を活かせる仕事を探している。企画に挑戦したいが、未経験で通用するか不安。"
                "次に確かめること: 正社員かアルバイトか、希望の勤務地")


def _state(tmp_path) -> State:
    return State(tmp_path / "state.json")


# ----------------------------- (1) 新しい項目の保存と、個人情報の防御 -----------------------------


def test_new_items_are_saved_and_show_up_in_the_summary(tmp_path):
    state = _state(tmp_path)
    r = logic.save_preferences(state, UID, NEW_ITEMS)
    assert set(r["saved"]) == set(NEW_ITEMS) and r["rejected"] == []
    summary = logic.conversation_summary(state, UID)
    assert {k: v["value"] for k, v in summary["stated_preferences"].items()} == NEW_ITEMS
    assert "consultation_summary" in summary["note"] and "purpose" in summary["note"]
    # 行動履歴のある人(dip 連携済み)の要約にも同じ形で出る
    logic.save_preferences(state, "sim-browsing", {"purpose": "生活費の足しに"})
    linked = logic.conversation_summary(state, "sim-browsing")
    assert linked["linked"] and linked["stated_preferences"]["purpose"]["value"] == "生活費の足しに"


@pytest.mark.parametrize("key", sorted(logic.PREFERENCE_KEYS))
@pytest.mark.parametrize("value", ["連絡は090-1234-5678まで", "09012345678", "taro@example.com に送って",
                                   "みほん市1-2-3", "みほん町3丁目", "5番地の近く", "みほん町3-5のあたり",
                                   "03-12345678", "090-1234-5678,よろしく"])
def test_personal_info_is_rejected_for_every_item(tmp_path, key, value):
    state = _state(tmp_path)
    r = logic.save_preferences(state, UID, {key: value})
    assert r["rejected"] == [key] and r["saved"] == [] and "個人情報" in r["note"]
    assert logic.stated_preferences(state.user(UID)) == {}


@pytest.mark.parametrize(("key", "value"), [
    ("min_annual_salary", "年収400-500万円"), ("start_timing", "3-6か月以内"), ("start_timing", "2026-09-30に入社したい"),
    ("concerns", "ブランクが1-2年あって不安"), ("time_band", "10-18時"), ("days_per_week", "週3-4日"),
    ("min_wage", "時給1000-1200円"), ("area", "みほん区の駅から1-2分"),
])
def test_ranges_and_dates_are_not_mistaken_for_an_address(tmp_path, key, value):
    """幅や日付の「数字-数字」は住所ではない(番地の「1-2-3」「みほん町3-5」だけを弾く)。"""
    state = _state(tmp_path)
    r = logic.save_preferences(state, UID, {key: value})
    assert r["saved"] == [key] and r["rejected"] == [], r


def test_too_long_or_unknown_items_are_not_saved(tmp_path):
    state = _state(tmp_path)
    long = "あ" * (logic.PREFERENCE_VALUE_MAX + 1)
    r = logic.save_preferences(state, UID, {"wants": long, "hobby": "釣り", "purpose": "生活費の足しに"})
    assert r["saved"] == ["purpose"] and set(r["rejected"]) == {"wants", "hobby"}
    assert "100字以内" in r["note"] and "保存できるのは" in r["note"]
    assert logic.save_preferences(state, UID, {"wants": "あ" * logic.PREFERENCE_VALUE_MAX})["saved"] == ["wants"]


def test_annual_salary_digits_are_not_mistaken_for_a_phone_number(tmp_path):
    """C の PR レビューからの後続指摘: 「年収3000000円」を電話番号と誤認して、保存・根拠・評価で消さない。"""
    for text in ("年収3000000円", "年収3,000,000円以上", "年収5000000円くらいで転職したい", "時給1200円"):
        assert not PII_VALUE.search(text), text
    for text in ("090-1234-5678", "09012345678", "03-1234-5678", "0120-123-456", "taro@example.com",
                 "03-12345678", "06-12345678", "連絡は090-1234-5678,", "090-1234-5678,よろしく", "090-1234-5678, よろしく"):
        assert PII_VALUE.search(text), text  # 電話番号・メールは今までどおり捕まえる

    state = _state(tmp_path)
    assert logic.save_preferences(state, UID, {"min_annual_salary": "年収3000000円"})["saved"] == ["min_annual_salary"]
    r = logic.set_search_track(state, UID, "正社員", "年収3000000円以上で転職したい")
    assert r["recorded"]["evidence"] == "年収3000000円以上で転職したい"  # 根拠が消されない
    assert logic.set_search_track(state, UID, "アルバイト・パート", "連絡は09012345678")["recorded"]["evidence"] \
        == "連絡は[個人情報を削除]"

    # 評価も、年収の桁表記をツールの引数・本人の発言に書いたことを個人情報の違反にしない
    turn = {"bubbles": ["年収300万円以上ですね。"], "quick_replies": ["今はいい"], "cards": [], "buttons": [],
            "unknown_job_ids": [], "tools": [
                {"name": "save_user_preferences", "args": {"preferences": {"min_annual_salary": "年収3000000円"}}, "result": {}},
                {"name": "set_search_track", "args": {"track": "正社員", "evidence": "年収3000000円以上で転職したい"}, "result": {}}]}
    assert turn_violations(0, turn) == []
    turn["tools"].append({"name": "save_user_preferences", "args": {"preferences": {"area": "09012345678"}}, "result": {}})
    assert any("個人情報" in v for v in turn_violations(0, turn))
    v = conversation_checks({"expect_outcome": ["consult"], "pii_test": True}, [turn], ["年収3000000円以上で転職したい"])
    assert not any("入力画面" in x for x in v["violations"])


# ----------------------------- (2) 相談のまとめ -----------------------------


def test_consultation_summary_is_recorded_and_shows_up_in_the_summary(tmp_path):
    state = _state(tmp_path)
    assert logic.conversation_summary(state, UID)["consultation_summary"] is None
    r = logic.record_outcome(state, UID, "consult", "相談のみ", 0.8, SUMMARY_TEXT)
    assert r["recorded"]["summary"] == SUMMARY_TEXT
    # まとめの無い後の記録(提案だけなど)で、前のまとめは消えない
    logic.record_outcome(state, UID, "propose", "求人を見る", 0.8)
    cs = logic.conversation_summary(state, UID)["consultation_summary"]
    assert cs == {"text": SUMMARY_TEXT, "recorded": "9/23", "outcome": "consult"}
    # 新しいまとめがあれば、それが出る
    logic.record_outcome(state, UID, "consult", "相談のみ", 0.8, "勤務地は渋谷を希望。次に確かめること: 年収の下限")
    assert logic.conversation_summary(state, UID)["consultation_summary"]["text"].startswith("勤務地は渋谷")


def test_consultation_summary_over_200_chars_is_not_recorded(tmp_path):
    state = _state(tmp_path)
    r = logic.record_outcome(state, UID, "consult", "相談のみ", 0.8, "あ" * 201)
    assert "200字以内" in r["error"] and "recorded" not in r
    assert state.user(UID)["outcomes"] == []  # 結果ごと記録しない(書き直して呼び直す)
    assert logic.record_outcome(state, UID, "consult", "相談のみ", 0.8, "あ" * 200)["recorded"]["summary"] == "あ" * 200


def test_consultation_summary_drops_contact_info(tmp_path):
    state = _state(tmp_path)
    r = logic.record_outcome(state, UID, "consult", "相談のみ", 0.8, "連絡先は09012345678と話した。年収3000000円希望")
    assert r["recorded"]["summary"] == "連絡先は[個人情報を削除]と話した。年収3000000円希望"


def test_the_rule_for_the_consultation_summary_is_in_the_prompt_and_the_tool():
    from dip_mcp import server

    prompt = server.baitoru_assistant()
    assert "相談のまとめ(200字以内)" in prompt and "本人の発言に基づく事実と、次に確かめることだけ" in prompt
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    desc = str(tools["record_conversation_outcome"].inputSchema)
    assert "200字以内" in desc and "本人の発言に基づく事実" in desc and "次に確かめること" in desc
    prefs = str(tools["save_user_preferences"].inputSchema)
    for key in logic.PREFERENCE_KEYS:
        assert key in prefs, key


# ----------------------------- (3) チャット → 音声 -----------------------------


def test_chat_items_and_consultation_summary_reach_the_voice_advisor(tmp_path, monkeypatch):
    """チャットの相談で保存した項目と相談のまとめが、同じユーザーの音声相談の指示に入る(本物の dip MCP)。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    model = ScriptedModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=["まず相談する", "今はいい"]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {
            "purpose": "前職の経験を活かしたい", "concerns": "未経験の職種で通用するか不安"}}),
        reply(["経験を活かしたいのですね。", "これまでどんなお仕事をされてきましたか?"], quick_replies=["今はいい"]),
        call("record_conversation_outcome", {"line_user_id": UID, "outcome": "consult", "reason": "相談のみ",
                                             "confidence": 0.8, "summary": SUMMARY_TEXT}),
        reply(["またいつでもご相談ください。"], quick_replies=["求人を探す", "使い方"]),
    ])
    said = ["経験を活かしたい。でも未経験の職種で通用するか不安", "今日はこれで"]
    turns = run(model, [(UID, "visit", None)] + [(UID, "message", m) for m in said], tmp_path)
    assert turns[2]["tools"][0]["result"]["recorded"]["summary"] == SUMMARY_TEXT

    async def start():
        dip = DipMCP()
        await dip.start()
        try:
            return await prepare_advisor(dip, UID)
        finally:
            await dip.aclose()

    advisor, _greeting = asyncio.run(start())
    for fact in ("前職の経験を活かしたい", "未経験の職種で通用するか不安", SUMMARY_TEXT, "## 前の会話の相談のまとめ"):
        assert fact in advisor.instructions, fact
    assert "聞き直さず" in advisor.instructions
    # 基本の指示(区分が未定の音声版の playbook つき)に追記する形
    base = ResumeAdvisor(playbook=logic.counseling_playbook("未定", "voice")["playbook"]).instructions
    assert advisor.instructions.startswith(base)
    assert "## 前の会話の相談のまとめ" not in ResumeAdvisor().instructions  # まとめが無ければ足さない


# ----------------------------- (4) 音声 → チャット -----------------------------


def test_voice_items_show_up_in_the_next_chat_summary(tmp_path, monkeypatch):
    """音声相談で本人が話した目的・不安が dip に保存され、次のチャットの要約(stated_preferences)に出る。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))

    async def interview():
        dip = DipMCP()
        await dip.start()
        try:
            advisor = ResumeAdvisor(dip=dip, user_id=UID)
            await advisor.propose_jobs(job_type="カフェ", purpose="生活費の足しに", concerns="立ち仕事が続くか不安",
                                       wants="人と話す仕事")
        finally:
            await dip.aclose()

    asyncio.run(interview())
    model = ScriptedModel(script=[
        call("get_user_conversation_summary", {"line_user_id": UID}),
        reply(["前回は生活費の足しにカフェで、とのことでしたが、今回も同じですか?"], quick_replies=["同じ", "今はいい"]),
    ])
    (again,) = run(model, [(UID, "revisit", None)], tmp_path)
    stated = {k: v["value"] for k, v in again["tools"][0]["result"]["stated_preferences"].items()}
    assert stated == {"job_type": "カフェ", "purpose": "生活費の足しに", "concerns": "立ち仕事が続くか不安",
                      "wants": "人と話す仕事"}


# ----------------------------- (5) 聞き直しの判定 -----------------------------


def _t(bubbles, saves=None):
    return {"bubbles": bubbles, "cards": [], "quick_replies": ["今はいい"],
            "tools": [{"name": "save_user_preferences", "args": {"preferences": saves}}] if saves else []}


REASKS = [  # (保存した項目, 保存した値, 後のターンの白紙の問い)
    ("purpose", "経験を活かしたい", "ところで、なぜ今お仕事を探しているのですか?"),
    ("purpose", "経験を活かしたい", "転職の目的は何ですか?"),
    ("purpose", "経験を活かしたい", "どうして働きたいと思ったのですか?"),
    ("purpose", "経験を活かしたい", "なぜ今、転職を考えているのですか?"),
    ("strengths", "新規開拓", "あなたの強みは何ですか?"),
    ("strengths", "新規開拓", "ご自身の強みを教えていただけますか?"),
    ("strengths", "新規開拓", "ご自身の強みはどこだと思いますか?"),
    ("area", "渋谷", "どのあたりで働きたいですか?"),
    ("area", "渋谷", "どのあたりで探しますか?"),
    ("area", "渋谷", "どの辺りで働きたいですか?"),
    ("area", "渋谷", "どの辺で働きたいですか?"),
    ("area", "渋谷", "どのエリアがよいですか?"),
    ("wants", "企画に挑戦したい", "やってみたいことはありますか?"),
    ("avoids", "飛び込み営業", "避けたいことはありますか?"),
    ("concerns", "未経験で通用するか", "働くうえで不安なことはありますか?"),
    ("concerns", "未経験で通用するか", "働くうえで気になることはありますか?"),
    ("min_annual_salary", "年収500万円以上", "希望の年収はどのくらいですか?"),
    ("work_style", "リモート中心", "どんな働き方を希望していますか?"),
    ("start_timing", "3か月以内", "いつから働けそうですか?"),
    ("start_timing", "3か月以内", "転職の時期はいつ頃を考えていますか?"),
]


@pytest.mark.parametrize(("key", "value", "ask"), REASKS)
def test_reasking_a_saved_new_item_is_a_violation(key, value, ask):
    turns = [_t(["どうしますか?"]), _t(["承知しました。"], {key: value}), _t([ask])]
    v = reask_violations(turns, [None, "はい", "はい"])
    assert any(f"「{key}」を聞き直した" in x for x in v), (ask, v)


FINE = [  # (保存した項目, 保存した値, 後のターンの問い): 確認・掘り下げ・追加の問いは聞き直しではない
    ("purpose", "経験を活かしたい", "目的は経験を活かしたい、でよいですか?"),
    ("purpose", "経験を活かしたい", "それはなぜですか?"),
    ("purpose", "経験を活かしたい", "経験を活かしたいと思ったのは、どんな場面からですか?"),
    ("wants", "企画に挑戦したい", "企画に挑戦したいとのことですが、具体的にはどんな仕事をしてみたいですか?"),
    ("wants", "企画に挑戦したい", "やってみたいことについて、もう少し詳しく教えていただけますか?"),
    ("wants", "企画に挑戦したい", "やりたいことは企画に挑戦したい、で合っていますか?"),
    ("concerns", "未経験で通用するか", "他に不安なことはありますか?"),
    ("concerns", "未経験で通用するか", "不安なことは未経験で通用するか、でよいですか?"),
    ("avoids", "飛び込み営業", "避けたいことは飛び込み営業、で合っていますか?"),
    ("strengths", "新規開拓", "強みは新規開拓、でよいですか?"),
    ("min_annual_salary", "年収500万円以上", "希望年収は500万円以上のままでよいですか?"),
    ("min_annual_salary", "年収500万円以上", "年収の下限は500万円のままでよいですか?"),
    # 保存した強みを使う問い・出した求人の中から選んでもらう問いは、聞き直しではない
    ("strengths", "新規開拓", "強みを活かせる求人を探しますか?"),
    ("strengths", "新規開拓", "ご自身の強みを活かせるお仕事を探しますか?"),
    ("strengths", "新規開拓", "あなたの強みを活かせる求人を見てみますか?"),
    ("strengths", "新規開拓", "あなたの強みを生かせる職場を探してみますか?"),
    # 迷い・不安の中身を聞く「どのあたり」は、場所の聞き直しではない(2026-09-29 の基準で違反になった2件)
    ("area", "渋谷", "どのあたりで迷っていますか?"),
    ("area", "渋谷", "どのあたりが気になっていますか?"),
    ("area", "渋谷", "どのあたりが不安ですか?"),
    ("area", "渋谷", "どのあたりが心配ですか?"),
    # 漢字の「辺り」でも同じ(「辺り?」が「り」を手放して先読みをすり抜けないこと)
    ("area", "渋谷", "どの辺りで迷っていますか?"),
    ("area", "渋谷", "どの辺りが気になりますか?"),
    ("area", "渋谷", "どこの辺りが不安ですか?"),
    ("wants", "企画に挑戦したい", "この中にやってみたいお仕事はありますか?"),
    ("avoids", "飛び込み営業", "この中で苦手な作業はありますか?"),
    ("avoids", "飛び込み営業", "ご紹介した求人に、やりたくない仕事は入っていますか?"),
    ("work_style", "リモート中心", "働き方はリモート中心でよいですか?"),
    ("start_timing", "3か月以内", "転職の時期は3か月以内で合っていますか?"),
    # 特定の求人・面接についての問いと、白紙でない「なぜ」は聞き直しではない
    ("concerns", "未経験で通用するか不安", "この求人について不安な点はありますか?"),
    ("concerns", "未経験で通用するか不安", "こちらのカフェの求人で、気になる点はありますか?"),
    ("concerns", "未経験で通用するか不安", "面接で気になることはありますか?"),
    ("concerns", "未経験で通用するか不安", "応募する前に心配なことはありますか?"),
    ("concerns", "未経験で通用するか不安", "こちらの3件で気になる点はありますか?"),
    ("concerns", "未経験で通用するか不安", "この3件の中で気になる点はありますか?"),
    ("concerns", "未経験で通用するか不安", "ご紹介したお店で気になることはありますか?"),
    ("concerns", "未経験で通用するか不安", "シフトについて気になることはありますか?"),
    ("purpose", "生活費の足しに", "どうして夜に働きたいのですか?"),
    ("purpose", "生活費の足しに", "なぜ接客のお仕事を探しているのですか?"),
]


@pytest.mark.parametrize(("key", "value", "ask"), FINE)
def test_confirming_or_deepening_a_saved_new_item_is_not_a_reask(key, value, ask):
    turns = [_t(["どうしますか?"]), _t(["承知しました。"], {key: value}), _t([ask])]
    assert reask_violations(turns, [None, "はい", "はい"]) == [], ask


# 求人を提案した(recommend_jobs を呼び、カードを出した)ターン以降に、迷っている点を聞く問い。提案への問いで、聞き直しではない
FINE_AFTER_PROPOSAL = [
    "時給・シフト・場所のうち、気になる点はどれですか?",
    "ありがとうございます。気になる点はありますか?",
    "3件ご紹介しました。気になる点はありますか?",
    "研修がある求人を選びました。気になる点はありますか?",
]


def _proposal(bubbles):
    t = _t(bubbles)
    t["tools"] = [{"name": "recommend_jobs", "args": {}}]
    t["cards"] = [{"job_id": "a001", "reasons": ["研修あり"]}]
    return t


@pytest.mark.parametrize("ask", FINE_AFTER_PROPOSAL)
def test_asking_what_bothers_them_after_proposing_jobs_is_not_a_reask(ask):
    saved = [_t(["どうしますか?"]), _t(["承知しました。"], {"concerns": "未経験で通用するか不安"})]
    assert reask_violations([*saved, _proposal([ask])], [None, "はい", "はい"]) == [], ask
    assert reask_violations([*saved, _proposal(["ご紹介します。"]), _t([ask])], [None, "はい", "はい", "はい"]) == [], ask
    # 提案の前なら、同じ問いは保存した不安の聞き直し
    assert any("「concerns」を聞き直した" in x for x in reask_violations([*saved, _t([ask])], [None, "はい", "はい"])), ask


@pytest.mark.parametrize(("key", "value", "ask"), REASKS)
def test_asking_an_item_that_is_not_saved_yet_is_not_a_reask(key, value, ask):
    """まだ保存していない項目を聞くのは、ヒアリングそのもの(聞き直しではない)。"""
    other = "area" if key != "area" else "time_band"
    turns = [_t(["どうしますか?"]), _t(["承知しました。"], {other: "渋谷"}), _t([ask])]
    assert reask_violations(turns, [None, "はい", "はい"]) == [], ask


def test_asking_in_the_same_turn_as_saving_is_not_a_reask():
    """保存したターンの問い(答えを受けて次の項目へ)は数えない。前のターンに保存した項目だけを見る。"""
    turns = [_t(["どうしますか?"]), _t(["なぜ今お仕事を探しているのですか?"], {"purpose": "経験を活かしたい"})]
    assert reask_violations(turns, [None, "経験を活かしたい"]) == []


@pytest.mark.parametrize(("key", "value", "ask"), [
    ("strengths", "新規開拓", "ご自身の強みを教えていただけますか?"),
    ("strengths", "新規開拓", "強みはどんなところですか?"),
    ("min_annual_salary", "年収500万円以上", "年収の下限はいくらですか?"),
    ("min_annual_salary", "年収500万円以上", "年収の下限は600万円でよいですか?"),  # 保存した値と違う額を示すのは確認ではない
    ("wants", "企画に挑戦したい", "やってみたいお仕事はありますか?"),  # 出した求人を指さない問いは聞き直しのまま
    ("avoids", "飛び込み営業", "苦手な作業はありますか?"),
])
def test_narrowed_reask_patterns_still_catch_blank_questions(key, value, ask):
    """過検知を直した後も、白紙で聞き直す問いは違反のまま(負例)。"""
    turns = [_t(["どうしますか?"]), _t(["承知しました。"], {key: value}), _t([ask])]
    v = reask_violations(turns, [None, "はい", "はい"])
    assert any(f"「{key}」を聞き直した" in x for x in v), (ask, v)


def test_concerns_are_exempt_only_right_after_a_proposal():
    """提案したターンとその後2ターンだけ、不安の問いを提案への問いとみなす。1通目で求人を出す再訪でも、
    会話の後半で保存済みの不安を白紙で聞き直せば違反。"""
    ask = "働くうえで不安なことはありますか?"
    seeded = {"concerns": "未経験で通用するか不安"}
    near = [_proposal(["おかえりなさい。3件あります。"]), _t(["はい。"]), _t([ask])]
    assert reask_violations(near, [None, "はい", "はい"], seeded) == []
    far = [_proposal(["おかえりなさい。3件あります。"]), _t(["はい。"]), _t(["はい。"]), _t(["はい。"]), _t([ask])]
    v = reask_violations(far, [None, "はい", "はい", "はい", "はい"], seeded)
    assert any("T5" in x and "「concerns」を聞き直した" in x for x in v), v


@pytest.mark.parametrize("ask", ["勤務地を広げて探しますか?", "エリアを広げてみますか?", "時間帯を変えて探してみますか?"])
def test_offering_to_widen_a_saved_condition_is_not_a_reask(ask):
    """0件のときに条件を広げる案内は、保存した条件を前提にした次の問い(実 API の基準の計測で見つかった過検知)。"""
    turns = [_t(["どうしますか?"]), _t(["承知しました。"], {"area": "渋谷・品川", "time_band": "日中"}), _t([ask])]
    assert reask_violations(turns, [None, "渋谷か品川で日中", "求人を見る"]) == [], ask


@pytest.mark.parametrize("ask", ["勤務地はどこですか?", "どのエリアで働きたいですか?"])
def test_blank_area_questions_are_still_reasks(ask):
    turns = [_t(["どうしますか?"]), _t(["承知しました。"], {"area": "渋谷・品川"}), _t([ask])]
    assert any("「area」を聞き直した" in x for x in reask_violations(turns, [None, "渋谷か品川", "はい"])), ask
