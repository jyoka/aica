"""挨拶で 3 つの入口(音声相談・履歴書の読み込み・ゲーム)へ案内する(会話の改善 (2/3)、task 220)。実 LLM・実 LINE は呼ばない。

- 評価の判定(evals/checks.py の greeting_entry_violations): 初回訪問は窓口で使える入口を全部、再訪は summary に合わせて 1 つだけ
- dip の Prompt: 入口V と入口R が、挨拶の入口の決まりを指している
- LINE の友だち追加の挨拶: 入口は LIFF 画面へのボタンになり、使えないもの(LiveKit が無いときの音声)は出ない。文字の選択肢も残る
- 再訪(ゲームの下書きに空欄): 要約を読んでから、下書きの続きだけを勧める
- 挨拶の言い方と並び(会話の改善 C (6/10)、task 227。evals/checks.py の greeting_wording_violations): 連絡した言い方をしない、
  問いが最後、再訪は行動履歴を言い切らない・「今回も」は前に話した人だけ・勧めは問いと別の吹き出し・合計の字数
"""

from __future__ import annotations

import pytest

from dip_mcp import arcade
from dip_mcp.data import State
from evals.checks import (
    available_entries,
    conversation_checks,
    greeting_entry_violations,
    greeting_offers,
    greeting_wording_violations,
    line_greeting_violations,
    line_message_counts,
    turn_violations,
)
from evals.scenarios import load_scenarios, prepare
from line_runtime import line_channel
from tests.test_handle import reply, run
from tests.test_liff_actions import (
    CHANNEL,
    GREETING,
    ID_KEY,
    INTERNAL_KEY,
    LIFF_ENVS,
    LIFF_ID,
    LIVEKIT_ENV,
    _heading,
)
from tests.test_line_webhook import REPLY, TestClient, _event, _post, _server
from tests.test_revisit import ARCADE_BUTTON, VISIT_MENU, Greeter, greet

SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
VISIT = {"event": "visit", "expect_outcome": ["propose"]}
REVISIT = {"event": "revisit", "expect_outcome": ["propose"]}


def turn(actions=(), buttons=(), quick_replies=("求人を探す", "まず相談する", "今はいい")) -> dict:
    return {"bubbles": ["どれから始めますか?"], "quick_replies": list(quick_replies), "cards": [], "buttons": list(buttons),
            "actions": list(actions), "tools": []}


ALL = turn(["voice_consult", "resume_upload"], [ARCADE_BUTTON])


# ----------------------------- 評価の判定 -----------------------------


def test_visit_must_offer_every_entry_the_channel_can_use():
    assert greeting_offers(ALL) == ["voice_consult", "resume_upload", "arcade"]
    assert greeting_entry_violations(VISIT, [ALL]) == []
    v = greeting_entry_violations(VISIT, [turn(["resume_upload"])])
    assert v == ["T1: 初回訪問の挨拶に、使える入口が出ていない: ['voice_consult', 'arcade']"]
    # LINE で LiveKit が無い: 音声は使えないので、出さないのが正しく、出したら違反
    line = ("resume_upload", "arcade")
    assert available_entries("line", line) == line
    assert greeting_entry_violations(VISIT, [turn(["resume_upload"], [ARCADE_BUTTON])], "line", line) == []
    assert greeting_entry_violations(VISIT, [ALL], "line", line) == ["T1: 窓口で使えない入口を出した: ['voice_consult']"]
    # LIFF の設定が無い LINE: どれも出さない
    assert greeting_entry_violations(VISIT, [turn()], "line", ()) == []


def test_the_draft_page_button_is_not_the_game_entry():
    draft = {"label": "下書きを見る", "url": "/liff/arcade/draft?rid=0"}
    assert greeting_offers(turn(buttons=[draft])) == []


def test_revisit_offers_at_most_one_entry_and_the_one_the_summary_calls_for():
    sc = REVISIT | {"expect_greeting_offer": "resume_upload"}
    assert greeting_entry_violations(sc, [turn(["resume_upload"])]) == []
    assert greeting_entry_violations(sc, [ALL])[0].startswith("T1: 再訪の挨拶で入口を3つ勧めた(1つだけ)")
    assert greeting_entry_violations(sc, [turn(["voice_consult"])]) == [
        "T1: 再訪の挨拶で勧めた入口が ['voice_consult'](summary からは resume_upload)"]
    assert greeting_entry_violations(sc, [turn()]) == ["T1: 再訪の挨拶で勧めた入口が なし(summary からは resume_upload)"]
    # 期待の入口が LINE で使えない(LIFF が無い)なら、何を勧めたかは問わない(使えないものを出したら違反)
    assert greeting_entry_violations(sc, [turn()], "line", ()) == []
    assert greeting_entry_violations(sc, [turn(["resume_upload"])], "line", ()) == [
        "T1: 窓口で使えない入口を出した: ['resume_upload']"]
    blanks = REVISIT | {"expect_greeting_offer": "blanks"}
    assert greeting_entry_violations(blanks, [turn(quick_replies=["空欄を埋める", "求人を探す", "今はいい"])]) == []


def test_conversation_checks_report_the_greeting_offers_and_ask_for_the_new_menu():
    sc = SCENARIOS["F-follow-new"]
    r = conversation_checks(sc, [ALL], [None])
    assert r["violations"] == [] and r["greeting_offers"] == ["voice_consult", "resume_upload", "arcade"]
    old_menu = turn(["voice_consult", "resume_upload"], [ARCADE_BUTTON], ["求人を探す", "応募履歴を確認", "使い方", "今はいい"])
    assert any("「求人を探す」「まず相談する」が無い" in v for v in conversation_checks(sc, [old_menu], [None])["violations"])


# ----------------------------- 挨拶の言い方と並び -----------------------------

BROWSING = SCENARIOS["R-revisit-browsing"]  # 希望も区分も無く、行動履歴だけ
TRACKED = SCENARIOS["R-revisit-track"]  # 記録された区分(アルバイト・パート)と行動履歴
RESUME = ["resume_upload"]
QUICK = ["カフェで探す", "別の仕事で探す", "まず相談する", "今はいい"]


def said(bubbles, actions=()) -> dict:
    return turn(actions, quick_replies=QUICK) | {"bubbles": list(bubbles)}


def test_revisit_greeting_in_the_new_order_passes():
    good = said(["おかえりなさい。履歴書を読み込むと、経歴を一から書かずに探せます。",
                 "前回はアルバイトで探していましたが、最近見ていたカフェのお仕事で探しますか?"], RESUME)
    assert greeting_wording_violations(TRACKED, [good], "アルバイト・パート") == []
    assert greeting_wording_violations(BROWSING, [said(["おかえりなさい。", "最近はカフェのお仕事を見ていましたか?"])]) == []
    # 前に希望を話した人には「今回も」と言ってよい。勧めの無い挨拶は、問いと同じ吹き出しでよい
    prefs = SCENARIOS["R-revisit"]
    assert greeting_wording_violations(prefs, [said(["おかえりなさい。前回は渋谷・日中のカフェでしたが、今回も同じですか?"])]) == []
    # 締めくくりの「いつでもお声がけください」は、こちらから連絡した言い方ではない
    assert greeting_wording_violations(prefs, [said(["おかえりなさい。いつでもお声がけください。", "今回も同じですか?"])]) == []


def test_revisit_greeting_does_not_say_aica_reached_out():
    """#221 の R-revisit-browsing T1 と R-revisit-track T1。トークを開いたのは本人で、aica は連絡していない。"""
    browsing = said(["最近カフェのお仕事をご覧になっていたので、ご連絡しました。履歴書があれば、読み込むとお仕事探しが進めやすくなります。",
                     "今回もカフェのお仕事を探していますか？"], RESUME)
    assert greeting_wording_violations(BROWSING, [browsing]) == [
        "T1: 挨拶でこちらから連絡した言い方をした(開いたのは本人): 最近カフェのお仕事をご覧になっていたので、ご連絡しました。",
        "T1: 行動履歴に言い切りで触れた(問いの中で確認形に): 最近カフェのお仕事をご覧になっていたので、ご連絡しました。",
        "T1: 前に話したことが無い人に「今回も」と言った"]
    track = said(["お仕事を見ていただいていたので、お声がけしました。", "前回はアルバイトで探していましたが、カフェで探しますか?"])
    assert greeting_wording_violations(TRACKED, [track], "アルバイト・パート") == [
        "T1: 挨拶でこちらから連絡した言い方をした(開いたのは本人): お仕事を見ていただいていたので、お声がけしました。",
        "T1: 行動履歴に言い切りで触れた(問いの中で確認形に): お仕事を見ていただいていたので、お声がけしました。"]
    for phrase in ("ご連絡いたしました。", "お声掛けしました。", "声をおかけしました。", "ご連絡を差し上げました。",
                   "ご連絡をしました。", "お知らせしました。", "ご連絡しております。", "お声がけしています。",
                   "ご連絡させてもらいました。", "お声がけさせてもらいました。", "ご連絡させていただいております。"):
        assert greeting_wording_violations(TRACKED, [said([phrase, "カフェで探しますか?"])], "アルバイト・パート"), phrase
    # 求人が出たときの約束と、本人への問いと、仕組みの説明は、こちらから連絡した言い方ではない
    for fine in (["新しい求人が出たらお知らせします。", "カフェで探しますか?"],
                 ["新しい求人が出たらご連絡します。", "カフェで探しますか?"],
                 ["おかえりなさい。", "応募先に連絡しましたか?"],
                 ["おかえりなさい。", "応募先に連絡していますか?"],
                 ["新着をお知らせしています。", "カフェで探しますか?"]):
        assert greeting_wording_violations(TRACKED, [said(fine)], "アルバイト・パート") == [], fine
    # 初回訪問の挨拶も同じ(連絡の理由はプッシュだけ)
    assert greeting_wording_violations(VISIT, [said(["はじめまして。ご連絡しました。", "どれから始めますか?"])])


def test_revisit_greeting_mentions_activity_only_inside_the_question():
    """#221 の Q-job-question T1「最近、お仕事を見ていましたね。」は、見ていたことを言い切っている。"""
    told = said(["最近、お仕事を見ていましたね。カフェのお仕事を探していますか？"])
    assert greeting_wording_violations(BROWSING, [told]) == [
        "T1: 行動履歴に言い切りで触れた(問いの中で確認形に): 最近、お仕事を見ていましたね。"]
    # 敬語の言い切り(#221 の R-revisit-track T1 の形)も同じ
    for told in ("おかえりなさい。最近お仕事を見ていただいていましたね。", "お仕事をご覧いただいていたので。"):
        assert greeting_wording_violations(BROWSING, [said([told, "カフェで探しますか?"])]) == [
            f"T1: 行動履歴に言い切りで触れた(問いの中で確認形に): {told.removeprefix('おかえりなさい。')}"], told
    # 問いと同じ文に読点でつないでも、理由や言い切りの形なら言い切り(#221 の R-revisit-browsing T1 から連絡の言い方を消した形)
    for told in ("最近カフェのお仕事をご覧になっていたので、カフェで探しますか？",
                 "最近カフェのお仕事を見ていましたね、カフェで探しますか？",
                 "お仕事を見ていただいていたので、カフェで探しますか?",
                 "カフェのお仕事を見ていましたが、カフェで探しますか?",
                 # 敬語、「んです」「ようなので」「ため」の形も同じ
                 "最近カフェのお仕事を見ていらっしゃいましたね、カフェで探しますか？",
                 "最近カフェのお仕事をご覧になっていたんですね、カフェで探しますか？",
                 "最近カフェのお仕事を見ていたようなので、カフェで探しますか？",
                 "最近カフェのお仕事をご覧になっていたため、カフェで探しますか？",
                 "最近カフェのお仕事を見ていたことから、カフェで探しますか？"):
        assert greeting_wording_violations(BROWSING, [said(["おかえりなさい。", told])]) == [
            f"T1: 行動履歴に言い切りで触れた(問いの中で確認形に): {told}"], told
    assert greeting_wording_violations(BROWSING, [said(["おかえりなさい。", "最近カフェのお仕事を見ていましたね、今回もカフェで探しますか？"])]) == [
        "T1: 行動履歴に言い切りで触れた(問いの中で確認形に): 最近カフェのお仕事を見ていましたね、今回もカフェで探しますか？",
        "T1: 前に話したことが無い人に「今回も」と言った"]
    # 連体形で問いの中に入れるのと、見ていたこと自体を聞くのは確認の問い
    for fine in ("前回はアルバイトで探していましたが、最近見ていたカフェのお仕事で探しますか?",
                 "最近はカフェのお仕事を見ていましたか?",
                 "最近カフェのお仕事を見ていましたか、それとも別のお仕事ですか？",
                 "最近カフェのお仕事を見ていたんですか？",
                 "最近カフェのお仕事を見ていたのでしょうか？"):
        assert greeting_wording_violations(TRACKED, [said(["おかえりなさい。", fine])], "アルバイト・パート") == [], fine


def test_the_question_comes_last_and_the_offer_comes_first():
    # #221 の Q-job-question T1: 問いの後ろに勧めがある
    after = said(["最近はカフェのお仕事を見ていましたか?", "履歴書や職務経歴書があれば、読み込むと早いです。"], RESUME)
    assert greeting_wording_violations(BROWSING, [after]) == [
        "T1: 挨拶の問いが最後に無い: 履歴書や職務経歴書があれば、読み込むと早いです。"]
    # #221 の R-revisit T1: 勧めが問いと同じ吹き出しにある
    packed = said(["前回は渋谷・日中のカフェを希望されていました。", "履歴書があれば、読み込むと早いです。今回も同じ条件で探しますか？"], RESUME)
    assert greeting_wording_violations(SCENARIOS["R-revisit"], [packed]) == [
        "T1: 入口の勧めを問いと同じ吹き出しに詰めた(勧めを先の吹き出しに): 履歴書があれば、読み込むと早いです。今回も同じ条件で探しますか？"]
    # 下書きの続き(文字の選択肢)も入口の勧め
    draft = turn(quick_replies=["空欄を埋める", "求人を探す", "今はいい"]) | {
        "bubbles": ["おかえりなさい。ゲームで作った下書きに、数字の空欄が残っています。今日は何から進めますか?"]}
    assert greeting_wording_violations(SCENARIOS["R-revisit-draft"], [draft])[0].startswith("T1: 入口の勧めを問いと同じ吹き出しに詰めた")
    # 初回訪問も問いが最後
    assert greeting_wording_violations(VISIT, [said(["どれから始めますか?", "音声での相談もできます。"])]) == [
        "T1: 挨拶の問いが最後に無い: 音声での相談もできます。"]


def test_revisit_greeting_total_length_depends_on_the_track():
    """#221 の R-revisit-track T1 は、勧めと問いを 66 字の吹き出しに詰め、計 91 字だった。"""
    packed = "履歴書や職務経歴書があれば、読み込むと早いです。前回はアルバイト・パートで探していましたが、最近見ていたカフェのお仕事で探しますか？"
    long = said(["おかえりなさい。今日もお仕事探しをお手伝いします。", packed])
    total = sum(map(len, long["bubbles"]))
    assert total == 91
    assert f"T1: 再訪の挨拶が計{total}字(90字まで)" in greeting_wording_violations(TRACKED, [long], "アルバイト・パート")
    assert not any("字(" in v for v in greeting_wording_violations(TRACKED, [long], "正社員"))  # 正社員は 130 字まで
    # 初回訪問は数えない(3 つの入口を文で伝えるため)
    assert greeting_wording_violations(VISIT, [long]) == []


def test_conversation_checks_report_the_greeting_wording():
    bad = said(["お仕事を見ていただいていたので、お声がけしました。", "カフェで探しますか?"])
    r = conversation_checks(TRACKED, [bad], [None])
    assert "T1: 挨拶でこちらから連絡した言い方をした(開いたのは本人): お仕事を見ていただいていたので、お声がけしました。" in r["violations"]
    # 挨拶でないシナリオ(プッシュ)は見ない: 連絡の理由を伝えるのはプッシュの決まり
    push = SCENARIOS["B-exit"]
    assert greeting_wording_violations(push, [said(["応募フォームの途中だったので、ご連絡しました。", "どこで止まりましたか?"])]) == []


# ----------------------------- LINE の初回の挨拶の通数と言い方(task 231) -----------------------------

FEATURES = ("resume_upload", "voice_consult", "arcade")
BODY = "お仕事探しの相談と、求人のご提案をします。下の「音声で相談する」「履歴書を読み込む」「ゲームで書類を作る」も使えます。"
QUESTION = "ゲームは、遊びながら履歴書や職務経歴書の下書きを作れます。どれから始めますか?"
# jyoka/tasks#221 の見直し(2026-10-25)で、初回訪問 9 件すべてに出た形: 言い方がずれた本文と問い
# 初回訪問の挨拶の返信(render の結果の entry が visit)。to_messages が問いを入口のボタンの見出しにするのは、挨拶のターン
# (評価ではシナリオの event が visit の 1 ターン目)だけ
FIRST = ALL | {"entry": "visit"}
OLD_BODY = "お仕事探しの相談や求人のご提案ができます。音声相談、履歴書の読み込み、ゲームでの書類作りもできます。"


def test_line_greeting_is_counted_through_to_messages_and_must_be_two_messages():
    """LINE の通数は line_channel.to_messages をオフラインで通して数える(LIFF の設定の代わりに使えたものを渡す)。"""
    good = FIRST | {"bubbles": [BODY, QUESTION]}
    assert line_message_counts([good], FEATURES, visit=True) == [2]
    assert line_greeting_violations(VISIT, [good], "line", FEATURES) == []
    three = FIRST | {"bubbles": [BODY, "ゲームは、遊びながら下書きを作れます。", "どれから始めますか?"]}
    assert line_message_counts([three], FEATURES, visit=True) == [3]
    assert line_greeting_violations(VISIT, [three], "line", FEATURES) == ["T1: 初回訪問の挨拶が LINE で 3 通(2 通まで)"]
    # 入口のボタンが無い LINE(LIFF の設定が無い)では、吹き出しがそのまま数になる。使えない操作を付けたら断りの文も数える
    assert line_message_counts([turn() | {"bubbles": three["bubbles"]}], (), visit=True) == [3]
    assert line_message_counts([three], (), visit=True) == [5]
    # Web の窓口と再訪は数えない(再訪の挨拶は別の決まり)
    assert line_greeting_violations(VISIT, [three], "web", FEATURES) == []
    assert line_greeting_violations(REVISIT, [three], "line", FEATURES) == []
    # 挨拶のターンでない返信は、同じ吹き出しとボタンでも見出しにしない(吹き出しの数 + ボタンの 1 通)。
    # 初回訪問で始まった会話の 2 ターン目(entry は visit のまま)も、再訪で始まった会話の 1 ターン目も
    assert line_message_counts([good, good], FEATURES, visit=True) == [2, 3]
    assert line_message_counts([good | {"entry": "revisit"}], FEATURES) == [3]


def test_line_greeting_must_name_the_entries_with_the_button_words():
    """本文が入口をボタンと違う言い方で呼ぶと違反(#221 の初回訪問の言い方)。使えない入口の名前は求めない。"""
    old = FIRST | {"bubbles": [OLD_BODY, "どれから始めますか?"]}
    assert line_greeting_violations(VISIT, [old], "line", FEATURES) == [
        "T1: 入口のボタンの名前が本文に無い(言い方がずれている): ['音声で相談する', '履歴書を読み込む', 'ゲームで書類を作る']"]
    no_voice = turn(["resume_upload"], [ARCADE_BUTTON]) | {"entry": "visit", "bubbles": ["「履歴書を読み込む」「ゲームで書類を作る」も使えます。", "どれから始めますか?"]}
    assert line_greeting_violations(VISIT, [no_voice], "line", ("resume_upload", "arcade")) == []
    renamed = FIRST | {"bubbles": [BODY, QUESTION], "buttons": [{"label": "書類ゲーム", "url": "/liff/arcade"}]}
    assert "T1: ゲームのボタンの文言が「書類ゲーム」(「ゲームで書類を作る」)" in line_greeting_violations(VISIT, [renamed], "line", FEATURES)


def test_emoji_in_buttons_and_choices_is_flagged_and_the_report_has_line_counts():
    emoji = FIRST | {"bubbles": [BODY, QUESTION], "buttons": [{"label": "🎮 ゲームで書類を作る", "url": "/liff/arcade"}],
                   "quick_replies": ["🔍 求人を探す", "まず相談する", "今はいい"]}
    assert "T1: ボタンや選択肢に絵文字がある(トークの決まりは絵文字なし): ['🎮 ゲームで書類を作る', '🔍 求人を探す']" in turn_violations(0, emoji)
    good = FIRST | {"bubbles": [BODY, QUESTION]}
    assert turn_violations(0, good) == []
    r = conversation_checks(SCENARIOS["F-follow-new"], [good], [None], channel="line", features=FEATURES)
    assert r["violations"] == [] and r["line_messages"] == [2]
    assert conversation_checks(SCENARIOS["F-follow-new"], [good], [None])["line_messages"] is None


# ----------------------------- dip の Prompt -----------------------------


def test_entries_point_to_the_greeting_rules():
    from dip_mcp.server import baitoru_assistant

    visit, revisit = baitoru_assistant("visit"), baitoru_assistant("revisit")
    for p in (visit, revisit):
        assert "## 挨拶で3つの入口を案内する(初回訪問・再訪)" in p
    assert "「求人を探す / まず相談する / 今はいい」の3つ" in visit and "窓口で使えるものを全部" in visit
    assert "入口を1つだけ勧める" in revisit and "3つ全部は並べない" in revisit
    # 「なぜ今連絡したか」はプッシュだけ。挨拶は「おかえりなさい」の側で、勧めを先、問いを最後
    assert "「おかえりなさい。」で始め" in revisit and "勧めを先、確認の1問を最後に置く" in revisit
    assert "- プッシュ(入口D)は、こちらから連絡する。最初の吹き出しで「なぜ今連絡したか」" in revisit
    assert "「なぜ今連絡したか」は書かず、「ご連絡しました」「お声がけしました」" in revisit
    assert "「今回も」は、前に本人が話したこと(stated_preferences の希望か、記録された区分)があるときだけ" in revisit
    # D2: 条件がそろっている人には履歴書を勧めない。勧めるときは何が早くなるかを言う
    assert "探す条件がそろっている人(区分が正社員でなく、stated_preferences に探す条件がある)には勧めない" in revisit
    assert "履歴書を読み込むと、経歴を一から書かずに探せます。" in revisit
    assert "読み込むと早いです。」のように1文添え" not in revisit
    assert "アルバイト・パートと未定は90字まで、正社員は130字まで" in revisit


# ----------------------------- LINE の友だち追加の挨拶 -----------------------------


def _no_liff(monkeypatch) -> None:
    for k in LIFF_ENVS + line_channel.LIVEKIT_ENVS:
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("voice", [True, False], ids=["with-livekit", "without-livekit"])
def test_line_follow_greeting_turns_the_entries_into_liff_buttons(tmp_path, monkeypatch, voice):
    """友だち追加の挨拶(初回訪問): エージェントは窓口で使える入口を知り、LINE ではそれが LIFF 画面へのボタンになる。
    LiveKit が無ければ、音声相談はエージェントにも使えないと伝わり、ボタンも出ない。文字の選択肢はクイックリプライに残る。"""
    _no_liff(monkeypatch)
    systems: list[str] = []

    def visit(messages):
        systems.append(messages[0].content)
        return greet(messages)

    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[visit]), AICA_ID_KEY=ID_KEY, LINE_LIFF_ID=LIFF_ID,
                           LINE_LIFF_CHANNEL_ID=CHANNEL, AICA_INTERNAL_KEY=INTERNAL_KEY, **(LIVEKIT_ENV if voice else {}))
    with TestClient(server.app) as client:
        assert _post(client, [_event("follow")]).status_code == 200

    ((path, body),) = fake.sent()
    assert path == REPLY  # 友だち追加の挨拶は返信トークで返す(push の通数を使わない)
    assert len(body["messages"]) <= line_channel.MAX_MESSAGES
    (flex,) = [m for m in body["messages"] if m["type"] == "flex"]
    links = [(b["action"]["label"], b["action"]["uri"]) for b in flex["contents"]["body"]["contents"]]
    expected = [("ゲームで書類を作る", f"https://liff.line.me/{LIFF_ID}/arcade")]
    if voice:
        expected.append(("音声で相談する", f"https://liff.line.me/{LIFF_ID}/voice"))
    expected.append(("履歴書を読み込む", f"https://liff.line.me/{LIFF_ID}/resume/upload"))
    assert links == expected
    texts = [m["text"] for m in body["messages"] if m["type"] == "text"]
    assert not any(t in line_channel.ACTION_NOTES.values() for t in texts)  # 使えない入口の断りの文は出ない
    labels = [i["action"]["label"] for i in body["messages"][-1]["quickReply"]["items"]]
    assert labels == VISIT_MENU  # 文字で答えたい人の選択肢(今はいい も)
    (system,) = systems
    assert "入口V(初回訪問)" in system and "## 挨拶で3つの入口を案内する" in system
    assert ("音声相談は、LINE では使えない" in system) is (not voice)


def test_line_follow_greeting_arrives_as_two_messages_through_the_webhook(tmp_path, monkeypatch):
    """友だち追加から LINE に届くまで(webhook → ターン → to_messages)で、初回訪問の挨拶は 2 通: 本文と、問いを見出しにした
    入口のボタン。同じ会話の次のターン(「まず相談する」の後の経験の1問。会話の入口は visit のまま)は挨拶ではないので、
    同じ形(吹き出し 2 つと履歴書・音声のボタン)でも見出しにせず、問いは吹き出しのまま残る(答えは文字の選択肢)。"""
    _no_liff(monkeypatch)
    entries = ["voice_consult", "resume_upload"]
    ask = ["これまでにどんなお仕事をしましたか?", "職務経歴書や履歴書があれば、読み込むと早いです。"]
    histories: list[list[str]] = []

    def experience(messages):
        histories.append([str(m.content) for m in messages])
        return reply(ask, quick_replies=["接客", "事務", "今はいい"], actions=entries)

    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[
                               lambda m: reply(GREETING, quick_replies=VISIT_MENU, actions=entries, buttons=[ARCADE_BUTTON]),
                               experience]),
                           AICA_ID_KEY=ID_KEY, LINE_LIFF_ID=LIFF_ID, LINE_LIFF_CHANNEL_ID=CHANNEL,
                           AICA_INTERNAL_KEY=INTERNAL_KEY, **LIVEKIT_ENV)
    with TestClient(server.app) as client:
        _post(client, [_event("follow")])
        _post(client, [_event("message", text="まず相談する", event_id="01EVENT0000000000000000002", token="reply-token-2")])
    ((_, first), (_, second)) = fake.sent()
    assert [m["type"] for m in first["messages"]] == ["text", "flex"]
    assert first["messages"][0]["text"] == GREETING[0] and _heading(first["messages"][1]) == GREETING[1]
    assert [i["action"]["label"] for i in first["messages"][1]["quickReply"]["items"]] == VISIT_MENU
    ((*history, said),) = histories  # 2 ターン目は、初回訪問の挨拶で始まった同じ会話の続き(挨拶の後に本人の発言)
    assert said == "まず相談する" and any("[イベント: 初回訪問" in c for c in history)
    assert [m["type"] for m in second["messages"]] == ["text", "text", "flex"]
    assert [m["text"] for m in second["messages"][:2]] == ask
    assert _heading(second["messages"][2]) is None and second["messages"][2]["altText"] == "リンク: 音声で相談する、履歴書を読み込む"


def test_line_follow_without_liff_greets_with_text_choices_only(tmp_path, monkeypatch):
    _no_liff(monkeypatch)
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[greet]), AICA_ID_KEY=ID_KEY)
    with TestClient(server.app) as client:
        _post(client, [_event("follow")])
    ((_, body),) = fake.sent()
    assert [m["type"] for m in body["messages"]] == ["text"]  # ボタンも断りの文も無い
    assert [i["action"]["label"] for i in body["messages"][-1]["quickReply"]["items"]] == VISIT_MENU


# ----------------------------- 再訪: 下書きの続きだけを勧める -----------------------------


def test_revisit_with_a_draft_offers_only_to_fill_the_blanks(tmp_path):
    sc = SCENARIOS["R-revisit-draft"]
    uid = sc["user"]
    prepare(State(tmp_path / "state.json"), sc)
    (t1,) = run(Greeter(script=[greet, greet]), [(uid, "revisit", None)], tmp_path)
    summary = t1["tools"][0]["result"]
    assert summary["arcade_draft"]["blank_lines"] and summary["uploaded_resume"]  # 経歴は分かり、下書きに空欄がある
    assert t1["actions"] == [] and t1["buttons"] == [] and "空欄を埋める" in t1["quick_replies"]
    assert "今はいい" in t1["quick_replies"] and len(t1["quick_replies"]) <= 4
    assert greeting_entry_violations(sc, [t1]) == []
    assert arcade.drafts(State(tmp_path / "state.json"), uid)  # 種の下書きは dip に保存された


# ----------------------------- Web: 音声相談の設定が無ければ勧めない -----------------------------


def test_web_note_leaves_out_voice_without_livekit(monkeypatch):
    from line_runtime.agent import WEB_NO_VOICE_NOTE, WEB_NOTE, channel_note

    assert channel_note("web") == WEB_NOTE  # 評価と開発者画面の既定(features が空)は 3 つとも使える
    assert channel_note("web", ("resume_upload", "voice_consult", "arcade")) == WEB_NOTE
    assert channel_note("web", ("resume_upload", "arcade")) == WEB_NO_VOICE_NOTE
    assert "voice_consult も付けない" in WEB_NO_VOICE_NOTE
    for k in line_channel.LIVEKIT_ENVS:
        monkeypatch.delenv(k, raising=False)
    assert line_channel.web_features() == ("resume_upload", "arcade")
    for k, v in LIVEKIT_ENV.items():
        monkeypatch.setenv(k, v)
    assert line_channel.web_features() == ("resume_upload", "voice_consult", "arcade")


@pytest.mark.parametrize("voice", [True, False], ids=["with-livekit", "without-livekit"])
def test_web_talk_turn_tells_the_agent_whether_voice_is_ready(tmp_path, monkeypatch, voice):
    """本番のトーク画面(/api/event)の挨拶: LiveKit の設定が無ければ、エージェントに音声相談を勧めないよう伝える(押すと 503 のため)。"""
    _no_liff(monkeypatch)
    systems: list[str] = []

    def visit(messages):
        systems.append(messages[0].content)
        return greet(messages)

    server, _ = _server(tmp_path, monkeypatch, Greeter(script=[visit]), AICA_ID_KEY=ID_KEY, AICA_INTERNAL_KEY=INTERNAL_KEY,
                        **(LIVEKIT_ENV if voice else {}))
    with TestClient(server.app) as client:
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        r = client.post("/api/event", json={"user_id": uid, "event": "visit"}).json()
    (system,) = systems
    assert ("音声相談は、この画面ではまだ使えない" in system) is (not voice)
    assert ("voice_consult" in r["actions"]) is voice and "resume_upload" in r["actions"]  # 台本の挨拶は窓口の案内に従う


def test_a_card_ledger_needs_its_own_limit(tmp_path):
    """--ledger を変えたら --limit-usd も要る(LLM を呼ぶ前に止まる)。"""
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    p = subprocess.run([sys.executable, "-m", "evals.run", "--only", "F-follow-new", "--ledger", str(tmp_path / "card.jsonl")],
                       check=False, capture_output=True, text=True, timeout=60, env=env)
    assert p.returncode == 2 and "--limit-usd" in p.stderr
    assert not (tmp_path / "card.jsonl").exists()
