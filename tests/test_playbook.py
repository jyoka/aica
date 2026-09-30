"""相談の playbook(aica v2 の F、ADR-0001)の台本テスト。実 LLM なし、dip の MCP は本物。

確かめること:
(1) 3つの playbook(正社員・アルバイト・パート・未定のチャット版)が dip にあり、get_counseling_playbook で
    LINE 側エージェントに配られる(区分に共通の決まりと、区分ごとの会話の決まりの値つき)
(2) 区分ごとの会話の決まり(吹き出しの字数・分岐の期限)が、playbook・Prompt・評価の形式判定で同じ値
(3) 台本の相談(経歴の厚い正社員志望): 目的を聞く / 掘り下げる / 選択肢が履歴書・発言から作られる /
    「分からない」で職種を並べない。同じ場面の悪い振る舞いは評価の機械判定が捕まえる
(4) 区分が変わったターンで playbook を読み直す(読み直さなければ評価の違反)
LLM が playbook どおりに深く聞けるかは、評価(実 API、LLM ジャッジの深さの項目)で測る。
"""

from __future__ import annotations

import re

import pytest

from dip_mcp import logic
from dip_mcp.data import ROOT, State
from evals import checks
from evals.checks import (
    PROFILE_FOLLOW_UP,
    PURPOSE_ASK,
    conversation_checks,
    dont_know_violations,
    turn_violations,
)
from evals.scenarios import load_scenarios, prepare
from tests.test_consult import (
    CLOSING,
    MENU,
    ConsultModel,
    calls,
    playbooks_read,
    returned_jobs,
    run,
    stated,
    switch_and_read,
    tools_of,
)
from tests.test_handle import call, reply

SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
UID = "sim-new"
BAITO, SEISHAIN, MITEI = "アルバイト・パート", "正社員", "未定"
PROMPT = (ROOT / "dip_mcp" / "prompts" / "baitoru_assistant.md").read_text(encoding="utf-8")


# ----------------------------- (1) 3つの playbook を dip のツールで配る -----------------------------


def test_three_chat_playbooks_are_served_by_the_dip_tool(tmp_path):
    model = ConsultModel(script=[
        calls(*[("get_counseling_playbook", {"track": t, "channel": "chat"}) for t in logic.TRACKS]),
        reply(["わかりました。"]),
    ])
    (r,) = run(model, [(UID, "message", "相談したい")], tmp_path)
    got = {t["args"]["track"]: t["result"] for t in r["tools"]}
    assert set(got) == set(logic.TRACKS) == {BAITO, SEISHAIN, MITEI}
    for track, res in got.items():
        assert res["track"] == track and res["channel"] == "chat"
        assert res["rules"] == logic.CONVERSATION_RULES[track]
        text = res["playbook"]
        assert text.startswith(f"# playbook: {track}(チャット)")
        assert "## 区分に共通の決まり(チャット)" in text  # 共通の決まりが区分ごとの playbook に付く
        assert "## 保存先" in text and "## 会話の決まり" in text
    # 中身は「明らかにすること」と「掘り下げ方」。次に何を聞くか(順番)は決めない
    for track in (SEISHAIN, BAITO):
        text = got[track]["playbook"]
        assert "## 明らかにすること" in text and "## 掘り下げ方" in text and "聞く順番ではない" in text
    fulltime, parttime = got[SEISHAIN]["playbook"], got[BAITO]["playbook"]
    for item in ("目的・転職の理由", "強み: 履歴書と会話から、本人の言葉で", "次にやりたいこと・避けたいこと",
                 "条件: 年収(希望の下限)・勤務地・働き方(リモート・フレックス・残業)・時期", "不安: ブランク・年齢・未経験の職種への挑戦"):
        assert item in fulltime, item
    for item in ("目的: 生活費・副業(本業と別の収入)・経験を積む・学業や家事との両立", "生活の制約: 働ける時間帯・曜日(週の日数)・エリア",
                 "興味・得意", "経験: これまでの仕事・アルバイト", "不安:"):
        assert item in parttime, item
    # 未定は、まず区分を1問で確かめる(C の合図の決まり)
    assert "## まず区分を確かめる" in got[MITEI]["playbook"]
    assert "正社員の仕事と、空いた時間のアルバイト、どちらを考えていますか?" in got[MITEI]["playbook"]
    # 区分に共通の決まり
    common = got[SEISHAIN]["playbook"]
    assert "目的とやりたいことは、それぞれ最低1回掘り下げる" in common
    assert "本人の履歴書(uploaded_resume)と本人の発言から作る" in common and "一般論の例" in common
    assert "「分からない」「まだ決めていない」「思いつかない」には、職種を並べない" in common
    # 正社員の条件は time_band を使わない(A のレビュー: 相談の time_band と正社員の節が食い違っていた)。経歴は experience に保存
    assert "時間帯(time_band)・週の日数(days_per_week)・時給の下限(min_wage)はアルバイト・パートの条件なので" in fulltime
    assert "経歴 experience(これまでの仕事の1問への答えも、ここに保存する" in fulltime
    assert "time_band" not in fulltime.split("## 保存先")[1].split("- 時間帯")[0]


def test_playbook_tool_rejects_unknown_tracks_and_channels_and_voice_is_not_there_yet():
    assert "error" in logic.counseling_playbook("契約社員")
    assert "error" in logic.counseling_playbook(SEISHAIN, "phone")
    # 音声の playbook は G で足す。それまでは無いことをはっきり返す(チャット版で代用しない)
    assert logic.counseling_playbook(SEISHAIN, "voice") == {"error": "voice の playbook はまだありません"}


# ----------------------------- (2) 区分ごとの会話の決まり = 評価の形式判定 -----------------------------


def test_conversation_rules_match_between_playbooks_prompt_and_eval_format_checks():
    rules = logic.CONVERSATION_RULES
    assert checks.FORMAT_RULES == rules  # 評価の形式判定は dip の値そのもの
    assert rules[BAITO] == rules[MITEI] == {"questions_max": 1, "bubble_max": 80, "branch_by": 3}  # v1 のまま
    assert rules[SEISHAIN] == {"questions_max": 1, "bubble_max": 120, "branch_by": 6}  # 正社員だけ緩める
    for track, r in rules.items():
        text = logic.counseling_playbook(track)["playbook"]
        section = text.split("## 会話の決まり")[1].split("\n## ")[0]
        assert f"1つの吹き出しは{r['bubble_max']}字以内" in section, track
        assert f"ユーザーの{r['branch_by']}回目の返信までに入る" in section, track
        assert f"1回の返信で質問は{r['questions_max']}つ" in section, track
    # Prompt(相談に入る前から効く決まり)にも同じ値
    assert (f"アルバイト・パートと未定は{rules[BAITO]['bubble_max']}字以内、"
            f"正社員は{rules[SEISHAIN]['bubble_max']}字以内") in PROMPT
    assert f"ユーザーの{rules[BAITO]['branch_by']}回目の返信までに「分岐」" in PROMPT
    assert f"区分が正社員なら{rules[SEISHAIN]['branch_by']}回目の返信まで" in PROMPT


def t(bubbles, tools=(), quick_replies=("今はいい",), cards=()):
    return {"bubbles": list(bubbles), "quick_replies": list(quick_replies), "cards": list(cards), "buttons": [],
            "tools": [{"name": n, "args": a, "result": res} for n, a, res in tools], "unknown_job_ids": []}


def _switch(track):
    return ("set_search_track", {"track": track}, {"track": track, "changed": True})


def _read(track):
    return ("get_counseling_playbook", {"track": track, "channel": "chat"}, {"track": track})


def test_format_checks_follow_the_track_rules():
    long = "履歴書では法人営業を4年、プロダクトマネージャーを4年されていたんですね。" * 2 + "なぜ今、転職を考えているのですか?"
    assert 80 < len(long) <= 120
    assert turn_violations(0, t([long]), SEISHAIN) == []
    assert turn_violations(0, t([long]), BAITO) == [f"T1: 吹き出しが{len(long)}字(80字以内)"]
    assert turn_violations(0, t([long + "あ" * 30]), SEISHAIN) == [f"T1: 吹き出しが{len(long) + 30}字(120字以内)"]
    # 分岐の期限: 3ターン目の区分が正社員なら6回目の返信まで、アルバイト・パートなら3回目まで
    plain = [t(["どうしますか?"])] * 5 + [t(["求人です。"], [("recommend_jobs", {}, {})], quick_replies=CLOSING)]
    fulltime = [plain[0], t(["正社員で考えますね。経歴を教えてください?"], [_switch(SEISHAIN), _read(SEISHAIN)]), *plain[2:]]
    sc = {"expect_outcome": ["propose"]}
    assert conversation_checks(sc, fulltime, [None] + ["正社員で"] * 5)["branch_within_3"] is True
    parttime = [plain[0], t(["バイトで考えますね。どこで働きたいですか?"], [_switch(BAITO), _read(BAITO)]), *plain[2:]]
    assert conversation_checks(sc, parttime, [None] + ["バイトで"] * 5)["branch_within_3"] is False


# ----------------------------- (3) 深い相談の台本(経歴の厚い正社員志望) -----------------------------

RESUME = {"user": UID, "seed_resume": "pm_sales"}  # 架空の職務経歴書(法人営業4年・PM4年)
SAID = [None,
        "転職を考えていて、正社員の仕事を相談したい",
        "今の会社では決まった機能の改善ばかりで、新しいプロダクトを最初から企画する経験を積みたい",
        "提案した新規事業の企画が2回通らなかったことです",
        "まだ分からないです",
        "飛び込みの新規開拓営業には戻りたくないです",
        "企画・PMで新規事業",
        "年収は600万以上です",
        "求人を見る"]
DIG_BACK = reply(["まだはっきりしないのですね。", "これまでの仕事で、もう続けたくないと感じた場面はありましたか?"],
                 quick_replies=["法人営業の新規開拓", "それ以外", "今はいい"])
ACK = ("新規事業の企画を2回提案しても通らなかったことが、転職を考えたきっかけなのですね。今の会社での既存機能の改善の"
       "仕事だけでなく、新しいプロダクトをゼロから企画できる環境を望まれていると受け止めました。")


def career_script(dig_back=DIG_BACK) -> list:
    return [
        call("get_user_conversation_summary", {"line_user_id": UID}),
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU),
        switch_and_read(UID, SEISHAIN, SAID[1]),
        reply(["正社員のお仕事で考えますね。いくつか伺いながら一緒に整理します。",
               "履歴書では法人営業を4年、プロダクトマネージャーを4年されていたんですね。そのうえで、なぜ今、転職を考えているのですか?"],
              quick_replies=["企画の幅を広げたい", "営業の経験を活かしたい", "それ以外", "今はいい"]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {
            "purpose": "新しいプロダクトを最初から企画する経験を積みたい"}}),
        reply(["新しいプロダクトを最初から企画する経験を積みたいのですね。", "そう感じたのは、どんな場面でしたか?"],
              quick_replies=["それ以外", "今はいい"]),
        reply([ACK, "逆に、次の仕事で避けたいことはありますか?"], quick_replies=["それ以外", "今はいい"]),
        dig_back,
        call("save_user_preferences", {"line_user_id": UID, "preferences": {"avoids": "飛び込みの新規開拓営業"}}),
        reply(["法人営業とプロダクトマネージャーのご経験と、新しいプロダクトを最初から企画したいというお話から、2つの方向が考えられます。",
               "どちらが近いですか?"], quick_replies=["企画・PMで新規事業", "営業経験を活かす事業開発", "それ以外", "今はいい"]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {"job_type": "企画・PM", "wants": "新規事業"}}),
        reply(["企画・PMで新規事業ですね。", "希望の年収はありますか?"], quick_replies=["それ以外", "今はいい"]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {"min_annual_salary": "年収600万以上"}}),
        reply(["新しいプロダクトを最初から企画したい、企画・PM、年収600万以上で整理しました。", "この内容で求人を見ますか?"],
              quick_replies=["求人を見る", "もう少し相談したい", "今はいい"]),
        calls(("recommend_jobs", {"line_user_id": UID, "employment_type": SEISHAIN, "job_type": "企画・PM",
                                  "min_annual_salary": 600}),
              ("record_conversation_outcome", {"line_user_id": UID, "outcome": "propose",
                                               "reason": "相談のあと求人を見る", "confidence": 0.8})),
        lambda msgs: reply(["企画・PMの求人です。新しいプロダクトを企画したいというお話に合わせました。"],
                           job_ids=returned_jobs(msgs), quick_replies=CLOSING),
    ]


CAREER_SC = {"expect_outcome": ["propose"], "consult_mode": True, "expect_consult_menu": True,
             "expect_tracks": [{"track": SEISHAIN, "on_said": "正社員|転職"}],
             "expect_saved_preferences": ["purpose", "avoids", "min_annual_salary"]}


def run_career(tmp_path, script):
    prepare(State(tmp_path / "state.json"), RESUME)  # 前にアップロード済みの履歴書(要約の uploaded_resume)
    model = ConsultModel(script=script)
    steps = [(UID, "visit", None)] + [(UID, "message", m) for m in SAID[1:]]
    return run(model, steps, tmp_path)


def _grounded(option: str, sources: str) -> bool:
    """選択肢の内容語(漢字・カタカナ2文字以上、英字)のどれかが、履歴書か本人の発言にある。"""
    words = re.findall(r"[一-龥々]{2,}|[ァ-ヴー]{2,}|[A-Za-z]{2,}", option)
    return any(w in sources or w[:2] in sources for w in words)


def test_career_consult_asks_the_purpose_digs_and_builds_options_from_the_resume(tmp_path):
    turns = run_career(tmp_path, career_script())
    r = conversation_checks(CAREER_SC, turns, SAID)
    assert r["violations"] == [], r["violations"]
    assert r["track_ok"] is True and r["tracks"][1] == SEISHAIN
    # 切り替えたターンで正社員の playbook を読み、その会話の決まり(120字)で返信した
    assert playbooks_read(turns[1]) == [SEISHAIN]
    assert any(len(b) > 80 for b in turns[3]["bubbles"])  # 正社員の吹き出しは 80 字を超えてよい
    # 目的を聞く(白紙の1問)→ 答えを掘り下げる(具体的な場面)
    assert PURPOSE_ASK.search(turns[1]["bubbles"][-1])
    assert PROFILE_FOLLOW_UP.search(turns[2]["bubbles"][-1])
    # 選択肢は履歴書と本人の発言から作る(「文字で話す」「今はいい」などの逃げ道を除いて、どれも手がかりがある)
    summary = next(x["result"] for x in turns[0]["tools"] if x["name"] == "get_user_conversation_summary")
    resume = str(summary["uploaded_resume"])
    for i in (1, 4, 5):
        options = [q for q in turns[i]["quick_replies"] if not checks.ESCAPE.search(q)]
        sources = resume + "".join(m for m in SAID[1:i + 1])
        assert options and all(_grounded(q, sources) for q in options), (i, options)
    # 「まだ分からない」には職種を並べず、問いを小さくして掘り下げに戻った
    assert dont_know_violations(turns, SAID) == [] and not tools_of(turns[4])
    # 相談で分かったことは本人の言葉で保存された。正社員の条件に time_band は入らない
    prefs = stated(tmp_path, UID)
    assert prefs["purpose"] == "新しいプロダクトを最初から企画する経験を積みたい"
    assert prefs["avoids"] == "飛び込みの新規開拓営業" and "time_band" not in prefs
    rec = next(x for x in turns[8]["tools"] if x["name"] == "recommend_jobs")
    assert rec["args"]["employment_type"] == SEISHAIN and "time_band" not in rec["args"] and turns[8]["cards"]


def test_listing_job_types_after_i_dont_know_is_caught(tmp_path):
    """v1 のように「まだ分からない」の後に職種の一覧を並べると、評価の機械判定が捕まえる(台本の悪い例)。"""
    listing = reply(["では、正社員の職種から考えてみましょう。", "営業・企画・事務のどれが気になりますか?"],
                    quick_replies=["営業", "企画・PM", "事務・管理", "今はいい"])
    turns = run_career(tmp_path, career_script(dig_back=listing))
    v = conversation_checks(CAREER_SC, turns, SAID)["violations"]
    assert any(x.startswith("T5: 「分からない」の後に職種を並べた") for x in v), v


@pytest.mark.parametrize("bubbles, quick", [
    # 経歴を受け止める平叙文(候補ではない)と、職種の無い掘り下げの問い
    (["カフェと事務のご経験があるんですね。", "どんな場面で手応えを感じましたか?"], ["それ以外", "今はいい"]),
    # 本人の経験から1つだけ確かめる問い
    (["コンビニのご経験で、楽しかったのはどんな場面でしたか?"], ["レジでの会話", "それ以外", "今はいい"]),
    # 職種の名前が別の語の一部(「法人営業」「営業時間」)
    (["法人営業のどんなところが合わないと感じましたか?"], ["営業時間が長い", "それ以外", "今はいい"]),
])
def test_digging_back_after_i_dont_know_is_not_a_violation(bubbles, quick):
    turns = [t(["どうしますか?"]), t(bubbles, quick_replies=quick)]
    assert dont_know_violations(turns, [None, "自分に何が合うのか、まだ分からない"]) == []


def test_i_dont_know_with_show_me_jobs_may_get_jobs():
    turns = [t(["どうしますか?"]), t(["カフェやスーパーの求人です。どれが気になりますか?"],
                                    quick_replies=["カフェ", "スーパー・販売", "今はいい"])]
    assert dont_know_violations(turns, [None, "よく分からないので求人を見せて"]) == []
    assert dont_know_violations(turns, [None, "よく分からない"])  # 頼まれていなければ違反


# ----------------------------- (4) 区分が変わったら playbook を読み直す -----------------------------


def test_switching_track_mid_conversation_rereads_the_playbook(tmp_path):
    """アルバイトで探していた人が「やっぱり正社員で」。切り替えたターンで正社員の playbook を読み直し、
    その会話の決まり(吹き出しの字数)と保存先で続ける。"""
    said = [None, "渋谷あたりでカフェのバイトを探しています", "やっぱり正社員で探したいです。販売の仕事で店長を目指したい"]
    long = ("正社員のお仕事で考えますね。カフェのホールでの経験を、販売の店長を目指すお仕事につなげられるように、"
            "正社員の求人を探す前に、目的や強み、条件をいくつか伺いながら一緒に整理します。")
    assert 80 < len(long) <= 120
    model = ConsultModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU),
        switch_and_read(UID, BAITO, said[1]),
        reply(["アルバイト・パートのお仕事で考えますね。", "カフェでのお仕事の経験はありますか?"], quick_replies=["それ以外", "今はいい"]),
        switch_and_read(UID, SEISHAIN, said[2]),
        reply([long, "店長を目指したいと思ったのは、なぜですか?"], quick_replies=["それ以外", "今はいい"]),
    ])
    turns = run(model, [(UID, "visit", None)] + [(UID, "message", m) for m in said[1:]], tmp_path)
    sc = SCENARIOS["T-switch-to-fulltime"]
    r = conversation_checks(sc, turns, said)
    assert r["violations"] == [] and r["track_ok"] is True
    assert r["tracks"] == [MITEI, BAITO, SEISHAIN]
    first, second = (next(x["result"] for x in turns[i]["tools"] if x["name"] == "get_counseling_playbook")
                     for i in (1, 2))
    assert (first["track"], second["track"]) == (BAITO, SEISHAIN)
    assert "min_annual_salary" in second["playbook"] and "min_annual_salary" not in first["playbook"]
    assert second["rules"]["bubble_max"] == 120 and first["rules"]["bubble_max"] == 80

    # 読み直さない・古い区分を読み直すのは違反(負例: 読み直していれば違反にならないことは上で見た)
    for tools in ([x for x in turns[2]["tools"] if x["name"] != "get_counseling_playbook"],
                  [x | {"args": {"track": BAITO}} if x["name"] == "get_counseling_playbook" else x
                   for x in turns[2]["tools"]]):
        bad = [*turns[:2], turns[2] | {"tools": tools}]
        v = conversation_checks(sc, bad, said)["violations"]
        assert "T3: 区分を「正社員」に切り替えたのに、同じターンで playbook を読み直していない" in v, v


def test_switch_that_did_not_change_the_track_needs_no_reread():
    # 今と同じ区分で呼んだ(changed が false)・エラーになった切り替えは、区分が変わっていないので読み直さなくてよい
    same = ("set_search_track", {"track": BAITO}, {"track": BAITO, "changed": False})
    failed = ("set_search_track", {"track": SEISHAIN}, {"error": "evidence に…"})
    turns = [t(["どうしますか?"]), t(["バイトですね。どこで働きたいですか?"], [same]),
             t(["わかりました。どこで働きたいですか?"], [failed])]
    assert checks.playbook_violations(turns) == []
