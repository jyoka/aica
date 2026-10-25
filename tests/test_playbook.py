"""相談の playbook(aica v2 の F、ADR-0001)の台本テスト。実 LLM なし、dip の MCP は本物。

確かめること:
(1) 3つの playbook(正社員・アルバイト・パート・未定のチャット版)が dip にあり、get_counseling_playbook で
    LINE 側エージェントに配られる(区分に共通の決まりと、区分ごとの会話の決まりの値つき)
(2) 区分ごとの会話の決まり(吹き出しの字数・分岐の期限)が、playbook・Prompt・評価の形式判定で同じ値
(3) 台本の相談(経歴の厚い正社員志望): 目的を聞く / 掘り下げる / 選択肢が履歴書・発言から作られる /
    「分からない」で職種を並べない。同じ場面の悪い振る舞いは評価の機械判定が捕まえる
(4) 区分が変わったターンで playbook を読み直す(読み直さなければ評価の違反)
(5) 相談の長さ: 相談の問いは4問まで(4問目までに求人を見るかを聞く)。掘り下げは1つの項目に1回まで、「分からない」
    「特にない」が2回続いたら条件の1問へ、条件は1問に1つで選択肢を付ける(task 233。task 225 ではまとめて1問だった。前は「目安は5、6問」で、jyoka/tasks#221 の
    C-consult-propose は8ターンで7問聞いて求人に届かなかった)。評価の判定 evals.checks.consult_questions の正例・負例も見る
    事情を話して探し始めた人(T-side-job、T-blank-parent)は相談に入れず、足りない条件だけを聞いて(2問まで)探す
    (evals.checks.situation_start_violations)
(6) playbook と Prompt の例文が、評価のペルソナの発言・経歴と重ならない(例文をなぞるだけで評価が良くならないように)
LLM が playbook どおりに深く聞けるかは、評価(実 API、LLM ジャッジの深さの項目)で測る。
"""

from __future__ import annotations

import json
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
    ENTRIES,
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
    assert "相談の問いは4問まで" in common and "掘り下げは1つの項目につき1回まで" in common
    assert "「分からない」「特にない」「思いつかない」が2回続いたら、掘り下げをやめて、探す条件の1問へ進む" in common
    assert "最低1回掘り下げる" not in common and "目安は5、6問" not in common  # 前の決まり(相談が長すぎた)
    for track in (SEISHAIN, BAITO, MITEI):  # 区分ごとの会話の決まりにも、相談の問いの上限
        section = got[track]["playbook"].split("## 会話の決まり")[1].split("\n## ")[0]
        assert "相談の問いは4問まで(求人を見るかの問い" in section, track
    assert "本人の履歴書(uploaded_resume)と本人の発言から作る" in common and "一般論の例" in common
    assert "「分からない」「まだ決めていない」「思いつかない」には、職種を並べない" in common
    # 正社員の条件は time_band を使わない(A のレビュー: 相談の time_band と正社員の節が食い違っていた)。経歴は experience に保存
    assert "時間帯(time_band)・週の日数(days_per_week)・時給の下限(min_wage)はアルバイト・パートの条件なので" in fulltime
    assert "経歴 experience(これまでの仕事の1問への答えも、ここに保存する" in fulltime
    assert "time_band" not in fulltime.split("## 保存先")[1].split("- 時間帯")[0]


def test_playbook_tool_rejects_unknown_tracks_and_channels():
    assert "error" in logic.counseling_playbook("契約社員")
    assert "error" in logic.counseling_playbook(SEISHAIN, "phone")


def test_voice_playbooks_share_the_chat_skeleton_but_speak_shorter():
    """音声版の playbook(aica v2 の G)はチャットと同じ骨格(見出し)で、言い回しだけ短く・1問ずつ。
    会話の決まりの値は音声のもの(1回の発話は2文まで・1問ずつ)。書類の呼び方は区分に合う。"""
    for track in (BAITO, SEISHAIN, MITEI):
        chat, voice = logic.counseling_playbook(track, "chat"), logic.counseling_playbook(track, "voice")
        assert voice["track"] == track and voice["channel"] == "voice"
        assert voice["rules"] == logic.VOICE_CONVERSATION_RULES[track] == {"questions_max": 1, "sentences_max": 2}
        text = voice["playbook"]
        assert text.startswith(f"# playbook: {track}(音声)") and "## 区分に共通の決まり(音声)" in text
        headings = [h for h in chat["playbook"].split("\n") if h.startswith("## ") and "共通の決まり" not in h]
        for h in headings:  # チャット版の見出しはすべて音声版にもある
            assert h.split("(")[0] in text, (track, h)
        assert "## 下書き" in text and "1回の発話は2文まで" in text
    assert "職務経歴書の下書きができる" in logic.counseling_playbook(SEISHAIN, "voice")["playbook"]
    assert "履歴書の下書きができる" in logic.counseling_playbook(BAITO, "voice")["playbook"]
    assert "「履歴書など」と呼ぶ" in logic.counseling_playbook(MITEI, "voice")["playbook"]
    # チャットの会話の決まりは変わらない(評価の形式判定がそのまま使う)
    assert logic.counseling_playbook(SEISHAIN)["rules"] == logic.CONVERSATION_RULES[SEISHAIN]


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


def t(bubbles, tools=(), quick_replies=("答えの候補", "今はいい"), cards=()):
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
        "年収は600万以上で、勤務地は新宿がいいです",
        "求人を見る"]
# 掘り下げに「まだ分からない」と答えた場合(3問目の返信までを見る)
DONT_KNOW_SAID = [*SAID[:3], "まだ分からないです"]
DIG_BACK = reply(["まだはっきりしないのですね。", "これまでの仕事で、もう続けたくないと感じた場面はありましたか?"],
                 quick_replies=["法人営業の新規開拓", "今はいい"])
ACK = ("新規事業の企画を2回提案しても通らなかったことが、転職を考えたきっかけなのですね。今の会社での既存機能の改善の"
       "仕事だけでなく、新しいプロダクトをゼロから企画できる環境を望まれていると受け止めました。")
OFFER = ["求人を見る", "もう少し相談したい", "今はいい"]


def career_script(third: dict | None = None) -> list:
    """目的を聞く(履歴書は確認形)→ 掘り下げ → 条件の1問 → 4問目で求人を見るか → 提案(相談の問いは4問まで)。
    third を渡すと、掘り下げへの答え(「まだ分からない」)に返す3問目の返信で台本を終える。"""
    head = [
        call("get_user_conversation_summary", {"line_user_id": UID}),
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU, **ENTRIES),
        switch_and_read(UID, SEISHAIN, SAID[1]),
        reply(["正社員の転職について、一緒に整理しましょう。",
               "履歴書では法人営業を4年、プロダクトマネージャーを4年されていたんですね。そのうえで、なぜ今、転職を考えているのですか?"],
              quick_replies=["企画の幅を広げたい", "営業の経験を活かしたい", "今はいい"]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {
            "purpose": "新しいプロダクトを最初から企画する経験を積みたい"}}),
        reply(["新しいプロダクトを最初から企画したいと感じたのは、どんな場面でしたか?"],
              quick_replies=[]),
    ]
    if third is not None:
        return [*head, third]
    return [
        *head,
        # 求人を探す前にエリアが要る(task 224)。条件は1問に1つで、選択肢を付ける(task 233)。本人が年収も一緒に答えたので、
        # 年収は聞き足さない
        reply([ACK, "勤務地はどのあたりがよいですか?"], quick_replies=["新宿", "渋谷", "今はいい"]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {"min_annual_salary": "年収600万以上",
                                                                          "area": "新宿"}}),
        reply(["新しいプロダクトを最初から企画したい、年収600万以上、新宿で整理しました。", "この内容で求人を見ますか?"],
              quick_replies=OFFER),
        calls(("recommend_jobs", {"line_user_id": UID, "employment_type": SEISHAIN, "area": "新宿",
                                  "min_annual_salary": 600}),
              ("record_conversation_outcome", {"line_user_id": UID, "outcome": "propose",
                                               "reason": "相談のあと求人を見る", "confidence": 0.8})),
        lambda msgs: reply(["新宿の求人です。新しいプロダクトを企画したいというお話に合わせました。"],
                           job_ids=returned_jobs(msgs), quick_replies=CLOSING),
    ]


CAREER_SC = {"expect_outcome": ["propose"], "consult_mode": True, "expect_consult_menu": True,
             "expect_tracks": [{"track": SEISHAIN, "on_said": "正社員|転職"}],
             "expect_saved_preferences": ["purpose", "min_annual_salary", "area"]}


def run_career(tmp_path, script, said=SAID):
    prepare(State(tmp_path / "state.json"), RESUME)  # 前にアップロード済みの履歴書(要約の uploaded_resume)
    model = ConsultModel(script=script)
    steps = [(UID, "visit", None)] + [(UID, "message", m) for m in said[1:]]
    return run(model, steps, tmp_path)


def _grounded(option: str, sources: str) -> bool:
    """選択肢の内容語(漢字・カタカナ2文字以上、英字)のどれかが、履歴書か本人の発言にある。"""
    words = re.findall(r"[一-龥々]{2,}|[ァ-ヴー]{2,}|[A-Za-z]{2,}", option)
    return any(w in sources or w[:2] in sources for w in words)


def _resume(turns) -> str:
    summary = next(x["result"] for x in turns[0]["tools"] if x["name"] == "get_user_conversation_summary")
    return str(summary["uploaded_resume"])


def test_career_consult_asks_the_purpose_digs_and_builds_options_from_the_resume(tmp_path):
    turns = run_career(tmp_path, career_script())
    r = conversation_checks(CAREER_SC, turns, SAID)
    assert r["violations"] == [], r["violations"]
    assert r["track_ok"] is True and r["tracks"][1] == SEISHAIN
    # 切り替えたターンで正社員の playbook を読み、その会話の決まり(120字)で返信した
    assert playbooks_read(turns[1]) == [SEISHAIN]
    assert any(len(b) > 80 for b in turns[3]["bubbles"])  # 正社員の吹き出しは 80 字を超えてよい
    # 目的を聞く(白紙の1問)→ 答えを掘り下げる(具体的な場面)→ 条件の1問 → 4問目で求人を見るか
    assert PURPOSE_ASK.search(turns[1]["bubbles"][-1])
    assert PROFILE_FOLLOW_UP.search(turns[2]["bubbles"][-1])
    assert checks.asks_conditions(turns[3]) and r["consult_questions"] == {"start": 2, "asked": [2, 3, 4], "offer": 5,
                                                                          "offer_at": 4}
    # 選択肢は履歴書と本人の発言から作る(逃げ道の「今はいい」を除いて、どれも手がかりがある)
    options = [q for q in turns[1]["quick_replies"] if not checks.ESCAPE.search(q)]
    assert options and all(_grounded(q, _resume(turns) + SAID[1]) for q in options), options
    # 相談で分かったことは本人の言葉で保存された。正社員の条件に time_band は入らない
    prefs = stated(tmp_path, UID)
    assert prefs["purpose"] == "新しいプロダクトを最初から企画する経験を積みたい"
    assert prefs["area"] == "新宿" and "time_band" not in prefs
    rec = next(x for x in turns[5]["tools"] if x["name"] == "recommend_jobs")
    assert rec["args"]["employment_type"] == SEISHAIN and "time_band" not in rec["args"] and turns[5]["cards"]


def test_digging_back_once_after_i_dont_know(tmp_path):
    """掘り下げに「まだ分からない」(1回目)には、職種を並べず、問いを小さくして掘り下げに戻ってよい。選択肢は履歴書から。"""
    turns = run_career(tmp_path, career_script(third=DIG_BACK), DONT_KNOW_SAID)
    r = conversation_checks(CAREER_SC | {"expect_saved_preferences": ["purpose"]}, turns, DONT_KNOW_SAID)
    assert r["violations"] == [], r["violations"]
    assert dont_know_violations(turns, DONT_KNOW_SAID) == [] and not tools_of(turns[3])
    options = [q for q in turns[3]["quick_replies"] if not checks.ESCAPE.search(q)]
    assert options and all(_grounded(q, _resume(turns) + "".join(DONT_KNOW_SAID[1:])) for q in options), options


def test_listing_job_types_after_i_dont_know_is_caught(tmp_path):
    """v1 のように「まだ分からない」の後に職種の一覧を並べると、評価の機械判定が捕まえる(台本の悪い例)。"""
    listing = reply(["では、正社員の職種から考えてみましょう。", "営業・企画・事務のどれが気になりますか?"],
                    quick_replies=["営業", "企画・PM", "事務・管理", "今はいい"])
    turns = run_career(tmp_path, career_script(third=listing), DONT_KNOW_SAID)
    v = conversation_checks(CAREER_SC, turns, DONT_KNOW_SAID)["violations"]
    assert any(x.startswith("T4: 「分からない」の後に職種を並べた") for x in v), v


@pytest.mark.parametrize("bubbles, quick", [
    # 経歴を受け止める平叙文(候補ではない)と、職種の無い掘り下げの問い
    (["カフェと事務のご経験があるんですね。", "どんな場面で手応えを感じましたか?"], []),
    # 本人の経験から1つだけ確かめる問い
    (["コンビニのご経験で、楽しかったのはどんな場面でしたか?"], ["レジでの会話", "今はいい"]),
    # 職種の名前が別の語の一部(「法人営業」「営業時間」)
    (["法人営業のどんなところが合わないと感じましたか?"], ["営業時間が長い", "今はいい"]),
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
    """アルバイトで探していた人が「やっぱり正社員で」。切り替えたターンで正社員の playbook を読み、
    その会話の決まり(吹き出しの字数)と保存先で続ける。条件だけ(相談に入っていない)のアルバイト・パートへの
    切り替えでは playbook を読まない(task 132: 呼び出しを重ねない)。"""
    said = [None, "渋谷あたりでカフェのバイトを探しています", "やっぱり正社員で探したいです。販売の仕事で店長を目指したい"]
    long = ("正社員で、合うお仕事を考えますね。カフェのホールでの経験を、販売の店長を目指すお仕事につなげられるように、"
            "正社員の求人を探す前に、目的や強み、条件を何から始めるか、一緒に整理しましょう。")
    assert 80 < len(long) <= 120
    model = ConsultModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU, **ENTRIES),
        call("set_search_track", {"line_user_id": UID, "track": BAITO, "evidence": said[1]}),
        reply(["アルバイト・パートで、合うお仕事を考えますね。", "カフェでのお仕事の経験はありますか?"], quick_replies=[]),
        switch_and_read(UID, SEISHAIN, said[2]),
        reply([long, "店長を目指したいと思ったのは、なぜですか?"], quick_replies=[]),
    ])
    turns = run(model, [(UID, "visit", None)] + [(UID, "message", m) for m in said[1:]], tmp_path)
    sc = SCENARIOS["T-switch-to-fulltime"]
    r = conversation_checks(sc, turns, said)
    assert r["violations"] == [] and r["track_ok"] is True and r["call_overlaps"] == []
    assert r["tracks"] == [MITEI, BAITO, SEISHAIN]
    assert playbooks_read(turns[1]) == []
    second = next(x["result"] for x in turns[2]["tools"] if x["name"] == "get_counseling_playbook")
    assert second["track"] == SEISHAIN
    assert "min_annual_salary" in second["playbook"] and second["rules"]["bubble_max"] == 120

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


# ----------------------------- (5) 相談の長さ: 4問目までに求人を見るかを聞く -----------------------------

LENGTH_SAID = [None,
               "資格の勉強と並行して、パートを探したいです",
               "介護の資格の学費を、自分でまかないたいからです",
               "勉強している介護の現場を、少しでも見ておきたいです",
               "平日の日中、週2日くらい、大宮あたりです",
               "求人を見る",
               "ありがとう、考えます"]
LENGTH_SC = {"expect_outcome": ["propose"], "consult_mode": True, "max_turns": 12,
             "expect_tracks": [{"track": BAITO, "on_said": "パート"}],
             "expect_saved_preferences": ["purpose", "wants", "time_band", "area"]}


def length_script(after_dig: list) -> list:
    """目的を聞く → 目的を1回掘り下げる(やりたいことの答え)→ その後は after_dig。"""
    return [
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU, **ENTRIES),
        switch_and_read(UID, BAITO, LENGTH_SAID[1]),
        reply(["アルバイトで何から始めるか、一緒に整理しましょう。",
               "勉強と並行して働きたいと思ったのは、どんな理由からですか?"], quick_replies=[]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {"purpose": "介護の資格の学費を自分でまかないたい"}}),
        reply(["学費を自分でまかないたいのですね。", "学費のほかに、このパートで得たいことはありますか?"],
              quick_replies=[]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {"wants": "介護の現場を見ておきたい"}}),
        *after_dig,
    ]


# 条件は1問に1つで、選択肢を付ける(task 233)。本人が日数とエリアも一緒に答えたので、聞き足さない
CONDITIONS = reply(["介護の現場を見ておくために、勉強の時間を除くと、働けるのはいつごろですか?"],
                   quick_replies=["平日の日中", "夕方から", "土日", "今はいい"])
SAVE_CONDITIONS = call("save_user_preferences", {"line_user_id": UID, "preferences": {
    "time_band": "日中", "days_per_week": "週2日", "area": "大宮"}})


def test_consult_asks_to_see_jobs_by_the_fourth_question(tmp_path):
    """目的 → 掘り下げ1回 → 条件の1問 → 4問目で求人を見るか。やりたいことの掘り下げや不安・強みが残っていても
    聞き続けない。ユーザーの4回目の返信に求人を見るかを聞き、5回目の返信で提案する(jyoka/tasks#221 の C-consult-propose は 8 ターンで 7 問だった)。"""
    model = ConsultModel(script=length_script([
        CONDITIONS,
        SAVE_CONDITIONS,
        reply(["学費をまかないつつ介護の現場を近くで見たい、大宮の平日日中・週2日で整理しました。", "この内容で求人を見ますか?"],
              quick_replies=["求人を見る", "もう少し相談したい", "今はいい"]),
        calls(("recommend_jobs", {"line_user_id": UID, "employment_type": BAITO, "area": "大宮", "time_band": "日中"}),
              ("record_conversation_outcome", {"line_user_id": UID, "outcome": "propose",
                                               "reason": "相談のあと求人を見る", "confidence": 0.8})),
        lambda msgs: reply(["介護の現場を見ておきたいというお話から、大宮の平日日中の求人です。"],
                           job_ids=returned_jobs(msgs), quick_replies=CLOSING),
        reply(["ありがとうございます。気になる求人は「保存」を押しておくと、次に開いたときに見返せます。"], quick_replies=CLOSING),
    ]))
    turns = run(model, [(UID, "visit", None)] + [(UID, "message", m) for m in LENGTH_SAID[1:]], tmp_path)
    # 配られた playbook に、相談の長さの決まりがある
    text = next(x["result"]["playbook"] for x in turns[1]["tools"] if x["name"] == "get_counseling_playbook")
    assert "相談の問いは4問まで。相談に入ってから4問目までに" in text and "この内容で求人を見るかを1問で聞く" in text
    assert "1問に1つだけ聞き、必ず選択肢を付ける" in text and "まとめて1問で聞いてよい" not in text
    assert "掘り下げは1つの項目につき1回まで" in text
    r = conversation_checks(LENGTH_SC, turns, LENGTH_SAID)
    assert r["violations"] == [], r["violations"]
    assert r["outcome_as_expected"] is True and r["outcomes"] == ["propose"]
    # 目的を聞く → 掘り下げ → 条件 → 求人を見るか、の4問(T1 は挨拶のメニュー)
    questions = [i for i, x in enumerate(turns) if any("?" in b for b in x["bubbles"]) and not x["cards"]]
    assert questions == [0, 1, 2, 3, 4]
    assert PURPOSE_ASK.search(turns[1]["bubbles"][-1]) and checks.asks_conditions(turns[3])
    assert r["consult_questions"] == {"start": 2, "asked": [2, 3, 4], "offer": 5, "offer_at": 4}
    # 条件を保存したターンの返信で、求人を見るかを聞いた。提案はユーザーの5回目の返信
    saved = next(i for i, x in enumerate(turns) for t in x["tools"]
                 if t["name"] == "save_user_preferences" and "area" in t["args"]["preferences"])
    assert saved == 4 and "求人を見ますか" in turns[saved]["bubbles"][-1]
    assert turns[saved + 1]["cards"] and r["branch_turn"] == saved + 2  # branch_turn は挨拶を T1 と数える


def test_digging_twice_and_asking_conditions_one_by_one_is_caught(tmp_path):
    """前の決まり(目的とやりたいことを最低1回ずつ掘り下げ、条件を1つずつ聞く)のままの5問目の提案は、評価の判定が捕まえる。"""
    said = [*LENGTH_SAID[:4], "利用者さんとの関わり方を、近くで見てみたいです", "平日の日中、週2日くらいです", "大宮あたりです"]
    model = ConsultModel(script=length_script([
        reply(["『現場を見ておきたい』というのは、例えばどんなことですか?"],
              quick_replies=[]),
        reply(["利用者さんとの関わり方を近くで見たいのですね。", "勉強の時間を除くと、働けるのはいつですか?"],
              quick_replies=["平日の日中", "今はいい"]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {"time_band": "日中", "days_per_week": "週2日"}}),
        reply(["通いやすいエリアはどのあたりですか?"], quick_replies=["大宮", "浦和", "今はいい"]),
        call("save_user_preferences", {"line_user_id": UID, "preferences": {"area": "大宮"}}),
        reply(["大宮の平日日中・週2日で整理しました。", "この内容で求人を見ますか?"],
              quick_replies=["求人を見る", "もう少し相談したい", "今はいい"]),
    ]))
    turns = run(model, [(UID, "visit", None)] + [(UID, "message", m) for m in said[1:]], tmp_path)
    r = conversation_checks(LENGTH_SC, turns, said)
    assert r["consult_questions"] == {"start": 2, "asked": [2, 3, 4, 5, 6], "offer": 7, "offer_at": 6}
    assert r["violations"] == ["T5: 相談で求人を見るかを聞かずに4問を聞いた(4問目までに聞く)"], r["violations"]


def _q(text: str, **kw) -> dict:
    return t(["わかりました。", text], **kw)


GREETING = t(["はじめまして。どうしますか?"], quick_replies=MENU)
READ = t(["何から始めるか、一緒に整理しましょう。", "今回バイトを探そうと思ったきっかけは何ですか?"], [_read(BAITO)])
ASK_JOBS = _q("この内容で求人を見ますか?", quick_replies=("求人を見る", "もう少し相談したい", "今はいい"))
DIG = [_q("どんな場面でそう感じましたか?"), _q("ほかに、やってみたいことはありますか?"),
       _q("避けたいことはありますか?"), _q("今の生活で変えたいことはありますか?")]


@pytest.mark.parametrize("said, turns, want", [
    # 4問目で求人を見るかを聞いた
    ([None, "相談したい", "a", "b", "c"], [GREETING, READ, *DIG[:2], ASK_JOBS], {"asked": [2, 3, 4], "offer": 5}),
    # 求人を見るかを聞かずに4問 → 4問目のターンで違反
    ([None, "相談したい", "a", "b", "c", "d"], [GREETING, READ, *DIG], {"asked": [2, 3, 4, 5, 6], "offer": None}),
    # 本人が「今はいい」で切り上げたら、そこで数えるのをやめる
    ([None, "相談したい", "a", "b", "今はいい"], [GREETING, READ, *DIG[:2], t(["わかりました。"])],
     {"asked": [2, 3, 4], "offer": None}),
])
def test_consult_questions_count_until_asking_to_see_jobs(said, turns, want):
    q = checks.consult_questions(turns, said)
    assert {k: q[k] for k in want} == want and q["start"] == 2
    v = checks.consult_length_violations(turns, said)
    assert v == (["T5: 相談で求人を見るかを聞かずに4問を聞いた(4問目までに聞く)"] if len(want["asked"]) >= 4 else [])


def test_continuing_after_more_consulting_is_not_counted_again():
    """4問目で求人を見るかを聞き、本人が「もう少し相談したい」と言ったら、4問を過ぎても続けてよい(入口に数え直さない)。"""
    said = [None, "相談したい", "a", "b", "c", "もう少し相談したい", "d", "e"]
    turns = [GREETING, READ, *DIG[:2], ASK_JOBS, *DIG[2:], ASK_JOBS]
    assert checks.consult_questions(turns, said)["offer_at"] == 4
    assert checks.consult_length_violations(turns, said) == []


def test_no_consult_no_count():
    """相談に入らず条件から探した会話(「求人を探す」・条件だけの発言)は数えない。"""
    said = [None, "求人を探す", "渋谷でカフェ"]
    turns = [GREETING, _q("どのエリアで探しますか?"), t(["渋谷のカフェです。"], [("recommend_jobs", {}, {})])]
    assert checks.consult_questions(turns, said) is None and checks.consult_length_violations(turns, said) == []


@pytest.mark.parametrize("reply_turn, bad", [
    (DIG[3], True),  # #221 の C-consult-propose T4: 2回続けて詰まったのに、別の項目の抽象的な問いを続けた
    (_q("働けるのは、いつごろですか?"), False),  # 条件の1問へ進んだ
    (_q("希望の年収はどのくらいですか?"), False),
    (ASK_JOBS, False),  # 条件が分かっていて、求人を見るかを聞いた
])
def test_two_stuck_answers_in_a_row_move_on_to_conditions(reply_turn, bad):
    said = [None, "バイトを探したいけど、何から始めればいいか分からない", "特にないです", "それ以外かな。"]
    turns = [GREETING, READ, DIG[0], reply_turn]
    want = ["T4: 「分からない」「特にない」が2回続いたのに、掘り下げを続けた(条件の1問か、求人を見るかの問いへ進む)"]
    assert checks.consult_length_violations(turns, said) == (want if bad else [])


@pytest.mark.parametrize("first, second", [
    ("分かりません", "わかりません"),  # 丁寧形(模擬ユーザーは丁寧形で答えることが多い)
    ("思いつきません", "特に思いつきません"),
    ("まだ分かりません。", "うーん、わかんない"),
    ("特にないです。", "浮かびません"),
])
def test_stuck_answers_in_polite_and_casual_forms(first, second):
    """詰まった答えは「分からない」「特にない」の言い方によらず数える(丁寧形・くだけた形も)。"""
    said = [None, "まず相談する", first, second]
    turns = [GREETING, READ, DIG[0], DIG[3]]
    assert checks.consult_length_violations(turns, said) == [
        "T4: 「分からない」「特にない」が2回続いたのに、掘り下げを続けた(条件の1問か、求人を見るかの問いへ進む)"]


@pytest.mark.parametrize("said", ["分かりました", "わかりました、カフェです", "決まっています"])
def test_answers_that_are_not_stuck(said):
    assert not checks.STUCK.search(said) and not checks.DONT_KNOW.search(said)


def test_one_stuck_answer_may_dig_back_once():
    said = [None, "相談したい", "思いつかない", "接客のバイトが楽しかったです"]
    turns = [GREETING, READ, _q("これまでで、手応えがあった場面はありますか?"), DIG[0]]
    assert checks.consult_length_violations(turns, said) == []


def test_saved_conversations_before_the_change_ask_too_many_questions():
    """直す前の保存済みの会話(#221 の回、上限 8)を今の判定で見直す(費用なし)。相談の5件は、どれも4問目までに
    求人を見るかを聞いていない。C-consult-propose は「特にない」が2回続いた T4 でも掘り下げを続けた。"""
    from evals.run import recheck
    runs = {r["case"]: r for r in json.loads((EVALS / "baseline" / "sim-20261025-230120.json").read_text(encoding="utf-8"))["runs"]}
    got = {case: recheck(runs[case]["detail"]) for case in
           ("C-consult-propose", "T-career-fulltime", "T-student-first", "T-blank-parent", "T-switch-to-fulltime")}
    assert {case: (r["consult_questions"]["offer"], r["consult_questions"]["offer_at"]) for case, r in got.items()} == {
        "C-consult-propose": (None, None), "T-career-fulltime": (8, 7), "T-student-first": (7, 6),
        "T-blank-parent": (7, 6), "T-switch-to-fulltime": (None, None)}
    assert all(any("相談で求人を見るかを聞かずに4問を聞いた" in v for v in r["violations"]) for r in got.values())
    assert any(v.startswith("T4: 「分からない」「特にない」が2回続いた") for v in got["C-consult-propose"]["violations"])



# 事情を話して探し始めた人は相談の掘り下げに入れず、足りない条件だけを聞いて早く探す。条件は1問に1つで選択肢を付け(task 233)、
# 条件の問いは2問まで(利用者の決定 2026-10-25、その読み方 2026-10-26。jyoka/tasks#221 で、相談に入らずに時間帯 → エリアを
# 聞いて T4 で提案した T-side-job の会話が、16 件の中でいちばん滑らかだった。この会話は合格にする)
SIDE_SAID = [None, "本業の後の空いた時間でバイトを探したい", "平日の19時以降か土日に、新宿あたりで働きたいです。",
             "ありがとう、考えます"]
SIDE_ASK = "働けるのは、何曜日の何時ごろですか?"  # 条件は1問に1つ(task 233)。本人がエリアも一緒に答える


def test_prompt_does_not_enter_a_consult_for_someone_who_explains_their_situation():
    entry = _between(PROMPT, "## 相談(playbook に沿って進める)", "- 入ったら、")
    assert "相談に入るのは、この2つのときだけ" in entry
    assert "迷いや相談したいことを言っていなければ相談に入らない" in entry
    assert "目的ややりたいことの掘り下げは聞かず" in entry
    assert "足りないもの(エリアと、アルバイト・パートなら働ける時間帯・曜日)だけを聞いて、早く探す" in entry
    assert "条件は1問に1つ、選択肢を付けて聞き" in entry and "条件の問いは2問まで" in entry
    assert "条件はまとめて1問で聞いてよく" not in entry  # task 225 の決まりは task 233 で直した
    assert checks.SITUATION_QUESTIONS_MAX == 2
    # 前の読み方(条件が揃っていなければ相談に入る)は残っていない
    assert "揃っていれば、相談に入らず" not in entry and "または、本人が自分の状況や事情を話して探し始めたとき" not in entry


def test_situation_start_asks_the_missing_conditions_once_and_searches(tmp_path):
    """T-side-job の流れ: 区分を切り替え、時間帯・曜日を選択肢つきの1問で聞き、本人がエリアも一緒に答えたターンで探す(T3)。
    playbook は読まず、相談の1文(「一緒に整理」)も出さない。シナリオの期待(相談に入れない、T4 までに探す)を満たす。"""
    sc = SCENARIOS["T-side-job"]
    model = ConsultModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU, **ENTRIES),
        call("set_search_track", {"line_user_id": UID, "track": BAITO, "evidence": SIDE_SAID[1]}),
        reply(["アルバイト・パートで、合うお仕事を考えますね。", SIDE_ASK], quick_replies=["平日の夜", "夕方から", "土日", "今はいい"]),
        calls(("save_user_preferences", {"line_user_id": UID, "preferences": {"time_band": "平日の19時以降か土日",
                                                                                     "area": "新宿あたり"}}),
              ("recommend_jobs", {"line_user_id": UID, "employment_type": BAITO, "area": "新宿", "time_band": "夜"}),
              ("record_conversation_outcome", {"line_user_id": UID, "outcome": "propose",
                                               "reason": "本業の後の夜に新宿で探す", "confidence": 0.8})),
        lambda msgs: reply(["本業の後に働ける、新宿の夜の求人です。"], job_ids=returned_jobs(msgs), quick_replies=CLOSING),
        reply(["ありがとうございます。気になる求人は「保存」を押しておくと、次に開いたときに見返せます。"], quick_replies=CLOSING),
    ])
    turns = run(model, [(UID, "visit", None)] + [(UID, "message", m) for m in SIDE_SAID[1:]], tmp_path)
    assert "迷いや相談したいことを言っていなければ相談に入らない" in model.systems[1]
    assert not any(playbooks_read(x) for x in turns) and turns[2]["cards"]
    r = conversation_checks(sc, turns, SIDE_SAID)
    assert r["violations"] == [], r["violations"]
    assert r["consult_questions"] is None and r["offer_turn"] == 3 <= sc["expect_offer_by"] == 4
    assert r["outcome_as_expected"] and r["outcomes"] == ["propose"]


SEARCHED = t(["新宿の夜の求人です。"], [("recommend_jobs", {}, {})], cards=[{"job_id": "j1"}])


@pytest.mark.parametrize("turns, want", [
    # 条件の1問(本人がエリアも一緒に答えた)で、T3 で探した
    ([GREETING, _q(SIDE_ASK), SEARCHED], []),
    # 時間帯とエリアを別々に聞いて T4 で提案した(#221 の T-side-job)。条件の問いは2問までなので合格
    ([GREETING, _q("本業の後なら、何時ごろから働けますか?"), _q("どのエリアで探しますか?"), SEARCHED], []),
    # 区分の確かめと条件の1問(どちらも探す条件の問い)
    ([GREETING, _q("アルバイトで探しますか、正社員ですか?"), _q(SIDE_ASK), SEARCHED], []),
    # 条件を1つずつ3問聞いた → 3問目のターンで違反
    ([GREETING, _q("何時ごろから働けますか?"), _q("どのエリアで探しますか?"), _q("週に何日くらい働けますか?"), SEARCHED],
     ["T4: 事情を話して探し始めた人に、探す前に3問目を聞いた(条件の問いは2問まで)"]),
    # 相談の合図なしに、目的の掘り下げを1問挟んだ → そのターンで違反(2問以内でも)
    ([GREETING, _q("本業の後にバイトをしたいと思ったのは、どんなきっかけからですか?"), _q(SIDE_ASK), SEARCHED],
     ["T2: 事情を話して探し始めた人に、探す前に条件でないことを聞いた(掘り下げずに、足りない条件だけを聞く)"]),
    # 相談に入った(playbook を読み、目的から聞いた)→ 入口のターンと、条件でない問いのターンで違反
    ([GREETING, READ, _q(SIDE_ASK)],
     ["T2: 事情を話して探し始めた人を相談に入れた(掘り下げずに、足りない条件だけを聞いて探す)",
      "T2: 事情を話して探し始めた人に、探す前に条件でないことを聞いた(掘り下げずに、足りない条件だけを聞く)"]),
])
def test_situation_start_violations(turns, want):
    sc = {"expect_no_consult": True}
    said = SIDE_SAID[:2] + ["夜から", "新宿あたりがいいです。", "週2日くらいです。"]
    assert checks.situation_start_violations(sc, turns, said[:len(turns)]) == want
    # 期待の無いシナリオ(相談のシナリオなど)では数えない
    assert checks.situation_start_violations({}, turns, said[:len(turns)]) == []


@pytest.mark.parametrize("question", ["アルバイトで探しますか、正社員ですか?", "アルバイト・パートか、正社員か、どちらで探しますか?",
                                      "扶養の範囲で働きたいですか?",
                                      # Prompt と undecided_chat.md が指定する区分の確かめの文
                                      "正社員の仕事と、空いた時間のアルバイト、どちらを考えていますか？",
                                      # 時間帯・曜日・日数のよくある聞き方
                                      "平日の夜と土日、どちらが働きやすいですか?", "週何回くらい入れそうですか?",
                                      "勤務時間のご希望はありますか?", "1日何時間くらい働けますか?"])
def test_situation_start_counts_track_and_dependent_questions_as_conditions(question):
    assert checks.asks_situation_conditions(_q(question))
    # 区分の確かめ(または条件の1問)の後にエリアを聞いて T4 で探す会話は、決まりどおり(違反なし)
    turns = [GREETING, _q(question), _q("どのエリアで探しますか?"), SEARCHED]
    said = SIDE_SAID[:2] + ["アルバイトです。平日の夜か土日です。", "新宿あたりがいいです。"]
    assert checks.situation_start_violations({"expect_no_consult": True}, turns, said) == []


def test_situation_start_does_not_count_a_purpose_question_that_names_the_track():
    # #221 の T-blank-parent の T2(相談に入った回の目的の問い)。区分の語を含むが、区分の確かめではない
    assert not checks.asks_situation_conditions(_q("お子さんが小学校に上がったのを機に、パートでどんなことを実現したいですか？"))
    # 「どちら」があっても、区分の語が片方だけなら区分の確かめではない
    assert not checks.asks_situation_conditions(_q("パートで大事にしたいのは、収入とやりがいのどちらですか?"))


def test_situation_start_stops_counting_when_the_person_opts_out():
    said = [None, SIDE_SAID[1], "今はいい"]
    turns = [GREETING, _q(SIDE_ASK), _q("また気が向いたら声をかけてくださいね。ほかに気になることはありますか?")]
    assert checks.situation_start_violations({"expect_no_consult": True}, turns, said) == []


def _saved(name: str, case: str) -> dict:
    from evals.run import recheck
    doc = json.loads((EVALS / "baseline" / f"{name}.json").read_text(encoding="utf-8"))
    return recheck(next(r for r in doc["runs"] if r["case"] == case)["detail"])


def test_saved_side_job_conversations_are_rechecked_with_the_situation_rule():
    """保存済みの T-side-job の会話(費用なし)を今のシナリオの期待で見直す。#221 の回(相談に入らず、時間帯とエリアを
    別々に聞いて T4 で提案)と、お礼の締めを直した回(同じ流れ)は合格。相談に入った回は、入口・目的の問い・3問目で違反になる。"""
    got = {name: _saved(name, "T-side-job") for name in ("sim-20261025-230120", "sim-20261025-234745", "sim-20261025-234138")}
    assert {name: (r["offer_turn"], r["outcome_as_expected"]) for name, r in got.items()} == {
        "sim-20261025-230120": (4, True), "sim-20261025-234745": (4, True), "sim-20261025-234138": (5, False)}
    situation = {name: [v for v in r["violations"] if "事情を話して" in v] for name, r in got.items()}
    assert situation == {
        "sim-20261025-230120": [], "sim-20261025-234745": [],
        "sim-20261025-234138": ["T2: 事情を話して探し始めた人を相談に入れた(掘り下げずに、足りない条件だけを聞いて探す)",
                                "T2: 事情を話して探し始めた人に、探す前に条件でないことを聞いた(掘り下げずに、足りない条件だけを聞く)",
                                "T4: 事情を話して探し始めた人に、探す前に3問目を聞いた(条件の問いは2問まで)"]}


# T-blank-parent の本人の発言(「子どもが小学校に上がったので」は、迷いや相談したいことを含まない事情の発言)
BLANK_SAID = [None, "子どもが小学校に上がったので、久しぶりにパートを探したい",
              "平日の9時から14時まで、週3〜4日くらいで、吉祥寺あたりを希望しています。", "ありがとう、考えます"]


def test_blank_parent_scenario_follows_the_situation_entry_rule():
    """T-blank-parent の本人の最初の発言は、事情を話して探し始めた形(Prompt の例「この春に引っ越してきたので」と同じ)。
    シナリオは Prompt の入口の決まりに合わせ、相談に入れない期待にした(前は consult_mode・measure_depth で、
    決まりどおりに動くと「目的を確かめる前に求人を出した」の違反になり、相談に入るほうが評価で通りやすかった)。"""
    sc = SCENARIOS["T-blank-parent"]
    assert sc["expect_no_consult"] and not sc.get("consult_mode") and not sc.get("measure_depth")
    assert not checks.CONSULT_SIGN.search(BLANK_SAID[1])
    # 決まりどおりの会話: 足りない条件を1問で聞き、T3 で探す → 違反なし、期限(T4)までに探した
    ask = _q(SIDE_ASK, quick_replies=["平日の日中", "夕方から", "土日", "今はいい"])
    found = t(["吉祥寺で、平日の昼間に働ける事務のパートです。"], [("recommend_jobs", {}, {})], cards=[{"job_id": "j1"}])
    closing = t(["ありがとうございます。気になる求人は「保存」を押しておくと、次に開いたときに見返せます。"])
    r = conversation_checks(sc, [GREETING, ask, found, closing], BLANK_SAID)
    assert [v for v in r["violations"] if "事情を話して" in v or "目的" in v] == []
    assert r["consult_questions"] is None and r["offer_turn"] == 3 and r["outcome_as_expected"]
    # 相談に入った会話は違反になる
    r = conversation_checks(sc, [GREETING, READ, ask, found], BLANK_SAID)
    assert "T2: 事情を話して探し始めた人を相談に入れた(掘り下げずに、足りない条件だけを聞いて探す)" in r["violations"]


def test_saved_blank_parent_conversations_entered_a_consult():
    """保存済みの T-blank-parent の会話(費用なし)は、どれも T2 で相談に入って目的を聞き、期限(T4)までに探していない。"""
    got = {name: _saved(name, "T-blank-parent") for name in ("sim-20261025-230120", "sim-20261025-234147")}
    assert {name: (r["offer_turn"], r["outcome_as_expected"]) for name, r in got.items()} == {
        "sim-20261025-230120": (7, False), "sim-20261025-234147": (6, False)}
    for r in got.values():
        assert [v for v in r["violations"] if "事情を話して" in v] == [
            "T2: 事情を話して探し始めた人を相談に入れた(掘り下げずに、足りない条件だけを聞いて探す)",
            "T2: 事情を話して探し始めた人に、探す前に条件でないことを聞いた(掘り下げずに、足りない条件だけを聞く)",
            "T4: 事情を話して探し始めた人に、探す前に3問目を聞いた(条件の問いは2問まで)"]

# ----------------------------- (6) 例文を評価のペルソナから離す -----------------------------

EVALS = ROOT / "evals"
PLAYBOOKS = ROOT / "dip_mcp" / "playbooks"


def _persona_text() -> str:
    """評価のペルソナの発言と経歴(scenarios.json の persona・script と、evals/personas の履歴書)。"""
    parts = [x for sc in load_scenarios() for x in (sc.get("persona", ""), *(sc.get("script") or []))]
    parts += [json.dumps(json.loads(f.read_text(encoding="utf-8")), ensure_ascii=False)
              for f in sorted((EVALS / "personas").glob("*.json"))]
    return "\n".join(parts)


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start) + len(start)
    return text[i:text.index(end, i)]


def _quotes(text: str) -> str:
    """指示の文に混ざった例(「」の中)だけ。"""
    return "".join(re.findall(r"「[^」]*」", text))


def _examples() -> dict[str, str]:
    """ペルソナから離す例文(場所 -> 例の文)。"""
    common = (PLAYBOOKS / "common_chat.md").read_text(encoding="utf-8")
    fulltime = (PLAYBOOKS / "fulltime_chat.md").read_text(encoding="utf-8")
    parttime = (PLAYBOOKS / "parttime_chat.md").read_text(encoding="utf-8")
    return {
        "Prompt: 正社員の経歴の確認形": _between(PROMPT, "確認形で使う(例:", ")。"),
        "Prompt: 相談に入らない事情の発言": _between(PROMPT, "話して探し始めたとき(", ")は、"),
        "Prompt: 事情を話した人に足りない条件を聞く1問": _between(PROMPT, "条件は1問に1つ、選択肢を付けて聞き(例:", "。下の対話ルール"),
        "Prompt: 提案の一言": _between(PROMPT, "その一言を本人の話につなげる(例:", ")。"),
        "common: 提案の一言": _between(common, "経験とのつながりを伝える(例:", ")。"),
        "common: 本人が最初に話したこと": _between(common, "本人が最初に話したこと(", ")の言葉"),
        "fulltime: 強みの確認形": _between(fulltime, "本人がどう捉えているかを聞く(例:", ")。"),
        "fulltime: 方向の候補": _between(fulltime, "組み合わせて作る(例:", ")。"),
        "parttime: 方向の候補": _between(parttime, "制約から作る(例:", ")。"),
        "Prompt: 相談に入らない条件だけの発言": _between(PROMPT, "条件だけ(", ")を言ったとき"),
        "common: 一般論の選択肢と答えの種類の一覧(悪い例)": _quotes(_between(common, "誰にでも出す一般論の例(", "を選択肢にしない")),
        "fulltime: やりたいことの掘り下げ": _quotes(_between(fulltime, "- やりたいこと: ", "\n")),
        "parttime: 興味の掘り下げ": _quotes(_between(parttime, "- やりたいこと・興味: ", "\n")),
    }


def _shared(example: str, persona: str, n: int = 4) -> set[str]:
    """例文とペルソナが共有する n 文字(漢字かカタカナを含むもの。「ますか?」のような言い回しは数えない)。"""
    return {example[i:i + n] for i in range(len(example) - n + 1)
            if re.search(r"[一-龥々ァ-ヴ]", example[i:i + n]) and example[i:i + n] in persona}


def test_examples_in_playbooks_and_prompt_do_not_echo_the_eval_personas():
    persona = _persona_text()
    examples = _examples()
    assert len(examples) == 13 and all(examples.values())
    for where, example in examples.items():
        assert _shared(example, persona) == set(), (where, example, _shared(example, persona))
    # 負例: 差し替える前の例文(ペルソナとほぼ同じ)は、この判定で重なりとして見つかる
    for old in ("本業の後の空いた時間で", "子どもが小学校に上がったので", "転職を考えていて", "法人営業を4年されていたんですね",
                "法人営業とPMの経歴で「新しいプロダクトを企画したい」", "「本や音楽が好き」「人見知り」",
                "「渋谷でカフェのバイト」", "「人と話す仕事 / 事務や運営を支える仕事」", "「収入を増やしたい / 経験を積みたい」",
                "「最初から企画する、というのは、どんな規模や役割のイメージですか?」", "「人と話すのが好き」",
                "「接客をしてみたいというお話から、夕方に働ける3件です。」"):
        assert _shared(old, persona), old
