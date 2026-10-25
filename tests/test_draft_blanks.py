"""下書きの空欄の数字を聞いたら、下書きの画面へつなぐ(会話の改善 C (7/10)、task 228)。実 LLM・実 LINE は呼ばない。

aica は下書きに書き込まない(利用者の判断 D4)。数字を聞いて本人が答えたら「下書きを見る」を出し、本人が画面で数字を入れる。
- dip の要約: arcade_draft に下書きの画面の URL(view_url)が載る。Web と、LIFF の設定がある LINE だけ(ユーザーIDは URL に載せない)
- LINE のトーク: エージェントが view_url をボタンにすると、LIFF URL のボタンになる。LIFF の設定が無ければ URL もボタンも無い
- 評価の判定(evals/checks.py の draft_number_violations): 答えの後の「下書きを見る」、1 つの会話で 1 回だけ、履歴書の数字は確認形
- dip の Prompt: 上の決まりが書いてある
"""

from __future__ import annotations

import re

import pytest

from dip_mcp import arcade, logic
from dip_mcp.data import State
from evals.checks import conversation_checks, draft_number_violations
from evals.scenarios import (
    load_arcade_draft,
    load_scenarios,
    prepare,
    seed_arcade_draft,
)
from line_runtime.line_channel import line_user
from tests.test_arcade_liff import _links
from tests.test_handle import call, reply, run
from tests.test_liff_actions import CHANNEL, ID_KEY, LIFF_ID, _no_liff
from tests.test_line_webhook import RAW_USER, REPLY, TestClient, _event, _post, _server
from tests.test_playbook import PROMPT
from tests.test_revisit import Greeter, _this_turn, _tool_result, greet

SC = next(sc for sc in load_scenarios() if sc["id"] == "R-revisit-draft")
LIFF = {"LINE_LIFF_ID": LIFF_ID, "LINE_LIFF_CHANNEL_ID": CHANNEL}
CONFIRM = "履歴書には、決算の作業日数を2日短くしたとあります。下書きの空欄も2日で合っていますか?"
TO_SCREEN = "下書きの画面で、この数字を入れて仕上げられます。"
NEXT = "経費精算の見直しは、何件くらいでしたか?"


# ----------------------------- dip の要約の下書きの画面の URL -----------------------------


def test_the_summary_has_the_draft_screen_url_for_web_and_for_line_with_liff(tmp_path, monkeypatch):
    """下書きがある人の要約の arcade_draft に、下書きの画面の URL が載る。Web はいつも、LINE は LIFF の設定があるときだけ。
    URL にユーザーIDは載せない(画面は Cookie か LIFF の本人から決める)。下書きが無ければ arcade_draft は None のまま。"""
    _no_liff(monkeypatch)
    st = State(tmp_path / "state.json")
    st.reset()
    web_uid, line_uid = "w-0123456789abcdef", line_user(RAW_USER, ID_KEY.encode())
    assert logic.conversation_summary(st, web_uid)["arcade_draft"] is None
    for uid in (web_uid, line_uid):
        seed_arcade_draft(st, uid, load_arcade_draft("accounting_arcade"))

    web = logic.conversation_summary(st, web_uid)["arcade_draft"]
    assert web["view_url"] == "/liff/arcade/draft?rid=0" and web["blanks"] == 2
    assert web_uid not in web["view_url"]
    assert "view_url" not in logic.conversation_summary(st, line_uid)["arcade_draft"]  # LINE で開けない

    for k, v in LIFF.items():
        monkeypatch.setenv(k, v)
    line = logic.conversation_summary(st, line_uid)["arcade_draft"]
    assert line["view_url"] == "/liff/arcade/draft?rid=0" and line_uid not in line["view_url"]
    assert "arcade_draft の view_url は下書きの画面の URL" in logic.conversation_summary(st, line_uid)["note"]


def test_the_developer_screen_draft_url_keeps_only_the_scenario_user(tmp_path):
    """開発者画面(匿名ID:シナリオユーザー)では、匿名IDを除いたシナリオユーザーだけが URL に載る(ゲームの URL と同じ決まり)。"""
    st = State(tmp_path / "state.json")
    st.reset()
    uid = "anon0123:sim-new"
    seed_arcade_draft(st, uid, load_arcade_draft("accounting_arcade"))
    url = logic.conversation_summary(st, uid)["arcade_draft"]["view_url"]
    assert url == "/liff/arcade/draft?rid=0&scenario_user=sim-new" and "anon0123" not in url


# ----------------------------- 会話: 数字を答えたら「下書きを見る」 -----------------------------


def blank_filler(messages):
    """dip の Prompt の「ゲームで書類を作る」どおりに動く偽のモデル。「空欄を埋める」には履歴書の数字で確かめ、
    本人が答えたら要約の view_url を「下書きを見る」にする(view_url が無ければボタンを出さない)。"""
    human = next(m for m in reversed(messages) if m.type == "human").content
    if "空欄を埋める" not in human and "2日" not in human:
        return greet(messages)
    if not any(m.type == "tool" for m in _this_turn(messages)):
        uid = re.search(r"LINE ユーザーID: ([^(\s]+)", messages[0].content).group(1)
        return call("get_user_conversation_summary", {"line_user_id": uid}, "sum")
    draft = _tool_result(messages, "get_user_conversation_summary")["arcade_draft"]
    if "空欄を埋める" in human:
        return reply([CONFIRM], quick_replies=["2日", "別の数字を入れる", "今はいい"])
    url = draft.get("view_url")
    return reply(["「2日」ですね。" + (TO_SCREEN if url else ""), NEXT], quick_replies=["わからない", "今はいい"],
                 buttons=[{"label": "下書きを見る", "url": url}] if url else [])


def test_web_conversation_shows_the_draft_button_after_the_number_and_passes_the_check(tmp_path):
    """Web(評価と同じ道): 再訪 → 「空欄を埋める」→ 確認形の問い → 「はい、2日です」→ 「下書きを見る」。判定は違反なし。"""
    uid = SC["user"]
    prepare(State(tmp_path / "state.json"), SC)
    said = [None, "空欄を埋める", "はい、2日です"]
    turns = run(Greeter(script=[greet, greet] + [blank_filler] * 4),
                [(uid, "revisit", None), (uid, "message", said[1]), (uid, "message", said[2])], tmp_path)
    t1, t2, t3 = turns
    assert "空欄を埋める" in t1["quick_replies"] and t1["buttons"] == []  # 挨拶は下書きの続きを勧めるだけ(ボタンは付けない)
    assert t2["bubbles"] == [CONFIRM] and t2["buttons"] == []
    assert t3["buttons"] == [{"label": "下書きを見る", "url": "/liff/arcade/draft?rid=0"}]
    assert TO_SCREEN in t3["bubbles"][0]
    r = conversation_checks(SC, turns, said)
    assert draft_number_violations(SC, turns, said) == [] and r["draft_button_turns"] == [3]
    assert arcade.draft(State(tmp_path / "state.json"), uid, 0)["markdown"].count("← 数字を入れる") == 2  # aica は書き込まない


@pytest.mark.parametrize("liff", [True, False])
def test_line_talk_turns_the_draft_button_into_a_liff_url(tmp_path, monkeypatch, liff):
    """LINE: 本人が数字を答えると、LIFF の設定があれば「下書きを見る」が LIFF URL のボタンで届く。
    無ければ要約に URL が無く、ボタンも画面の案内も出ない。"""
    _no_liff(monkeypatch)
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[blank_filler] * 4), AICA_ID_KEY=ID_KEY,
                           **(LIFF if liff else {}))
    uid = line_user(RAW_USER)
    seed_arcade_draft(server.state, uid, load_arcade_draft("accounting_arcade"))
    with TestClient(server.app) as client:
        _post(client, [_event("message", text="空欄を埋める")])
        _post(client, [_event("message", text="はい、2日です", event_id="01EVENT0000000000000000002", token="reply-token-2")])

    first, second = [b for p, b in fake.sent() if p == REPLY]
    assert _links(first) == []
    texts = [m["text"] for m in second["messages"] if m["type"] == "text"]
    if liff:
        assert _links(second) == [{"type": "uri", "label": "下書きを見る", "uri": f"https://liff.line.me/{LIFF_ID}/arcade/draft?rid=0"}]
        assert any(TO_SCREEN in t for t in texts)
    else:
        assert _links(second) == [] and not any(TO_SCREEN in t for t in texts)


# ----------------------------- 評価の判定 -----------------------------


def turn(bubbles, buttons=(), quick_replies=("今はいい",)) -> dict:
    return {"bubbles": list(bubbles), "quick_replies": list(quick_replies), "cards": [], "buttons": list(buttons),
            "actions": [], "tools": []}


DRAFT = {"label": "下書きを見る", "url": "/liff/arcade/draft?rid=0"}
GREETING = turn(["ゲームで作った下書きに、数字の空欄が残っています。", "今日は何をしますか?"], quick_replies=["空欄を埋める", "今はいい"])
BLANK_Q = turn(["決算の作業日数は、何日短くしましたか?"], quick_replies=["2日", "今はいい"])


def test_the_check_wants_the_draft_button_right_after_the_answer():
    said = [None, "空欄を埋める", "2日です", "わからない"]
    good = [GREETING, BLANK_Q, turn(["「2日」ですね。" + TO_SCREEN, NEXT], [DRAFT]), turn(["分かりました。"])]
    assert draft_number_violations({}, good, said) == []
    missing = [GREETING, BLANK_Q, turn(["「2日」ですね。", NEXT]), turn(["分かりました。"])]
    assert draft_number_violations({}, missing, said) == [
        "T3: 空欄の数字を聞いて本人が答えた後に「下書きを見る」が無い(本人が下書きの画面で入れられない)"]
    twice = [GREETING, BLANK_Q, turn(["「2日」ですね。", NEXT], [DRAFT]), turn(["分かりました。"], [DRAFT])]
    assert draft_number_violations({}, twice, said) == ["T4: 「下書きを見る」を1つの会話で2回出した(1回だけ)"]


def test_the_check_skips_answers_without_a_number_and_questions_off_the_blanks():
    dont_know = [None, "空欄を埋める", "わからない", "2日です"]
    next_blank = turn(["次の空欄です。決算の作業日数は、何日短くしましたか?"])
    turns = [GREETING, turn(["経費精算の空欄は、何件くらいでしたか?"]), next_blank, turn(["「2日」ですね。"], [DRAFT])]
    assert draft_number_violations({}, turns, dont_know) == []  # 「わからない」の後は要らず、次の数字の答えの後に出す
    assert draft_number_violations({}, [*turns[:3], turn(["「2日」ですね。"])], dont_know) == [
        "T4: 空欄の数字を聞いて本人が答えた後に「下書きを見る」が無い(本人が下書きの画面で入れられない)"]
    # 空欄の話の後の、ほかの数の問い(勤務の日数)は空欄の問いではない
    work_days = [GREETING, turn(["経費精算の空欄は、何件でしたか?"]), turn(["分かりました。週に何日働きたいですか?"]),
                 turn(["週3日ですね。"])]
    assert draft_number_violations({}, work_days, [None, "空欄を埋める", "わからない", "3日"]) == []
    # 数字を示さない問いへの「はい」と、「もう十分です」は数字の答えではない
    recall = [GREETING, turn(["空欄の数字を、一緒に思い出しましょうか?"]), next_blank, turn(["分かりました。"])]
    assert draft_number_violations({}, recall, [None, "空欄を埋める", "はい", "もう十分です"]) == []
    assert draft_number_violations({}, [turn(["渋谷で何件か探しますか?"]), turn(["3件です。"])], [None, "2件で"]) == []


def test_the_check_reads_the_whole_reply_and_the_agents_own_blank_question():
    """数字が問いの前の文にある確認形も、エージェントが自分から空欄を聞いた場面(playbook)も同じに見る。"""
    split_confirm = turn(["履歴書には2日短くしたとあります。", "下書きの空欄もこの日数で合っていますか?"])
    assert draft_number_violations({}, [GREETING, split_confirm, turn(["分かりました。"])], [None, "空欄を埋める", "はい"]) == [
        "T3: 空欄の数字を聞いて本人が答えた後に「下書きを見る」が無い(本人が下書きの画面で入れられない)"]
    own = [turn(["お仕事の相談ですね。"]), turn(["下書きの空欄を1つ伺います。決算の作業日数は何日短くしましたか?"]),
           turn(["「2日」ですね。"])]
    assert draft_number_violations({}, own, [None, "職務経歴書を仕上げたい", "2日です"]) == [
        "T3: 空欄の数字を聞いて本人が答えた後に「下書きを見る」が無い(本人が下書きの画面で入れられない)"]


def test_the_check_skips_the_button_where_the_draft_screen_cannot_open():
    """LIFF の無い LINE(features に arcade が無い)では、ボタンが LINE に届かないので問わない。"""
    turns = [GREETING, BLANK_Q, turn(["「2日」ですね。"])]
    said = [None, "空欄を埋める", "2日です"]
    assert draft_number_violations({}, turns, said, "line", ("resume_upload",)) == []
    assert draft_number_violations({}, turns, said, "line", ("resume_upload", "arcade")) != []


# jyoka/tasks#221 の実 API の回(2026-10-25、LINE)の R-revisit-draft。直す前の会話(選択肢の先頭は「2日」でも、問いは白紙)
BEFORE = [
    turn(["ゲームで作った職務経歴書の下書きに、数字の空欄が残っています。", "今日は何から進めますか？"],
         quick_replies=["空欄を埋める", "求人を探す", "まず相談する", "今はいい"]),
    turn(["下書きの「決算の作業日数を短くした」の空欄から確認します。何日短くしましたか？"],
         quick_replies=["2日", "別の数字を入力する", "今はいい"]),
    turn(["決算の作業日数は「2日」ですね。次の空欄は経費精算の確認に関する件数です。何件でしたか？"],
         quick_replies=["数字を入力する", "分からない", "今はいい"]),
    turn(["件数が分からなければ、数字は入れずにおきましょう。空欄の確認はここで終えて、次は何をしますか？"],
         quick_replies=["求人を探す", "まず相談する", "応募履歴を確認", "今はいい"]),
    turn(["こちらこそ、ありがとうございました。また必要なときにお声がけください。"], quick_replies=["求人を探す", "応募履歴を確認", "使い方"]),
]
BEFORE_SAID = [None, "空欄を埋める", "2日です", "分からない", "ありがとう、今日はこれで。"]


def test_the_check_fails_the_conversation_before_the_fix():
    assert draft_number_violations(SC, BEFORE, BEFORE_SAID, "line", ("resume_upload", "voice_consult", "arcade")) == [
        "T3: 空欄の数字を聞いて本人が答えた後に「下書きを見る」が無い(本人が下書きの画面で入れられない)",
        "T2: 下書きの空欄を白紙で聞いた(summary にある「2日」を示して確かめていない)"]


def test_the_check_wants_the_resume_number_as_a_confirmation():
    sc = {"expect_confirm_number": "2日"}
    said = [None, "空欄を埋める", "はい、2日です"]
    confirmed = [GREETING, turn([CONFIRM]), turn(["「2日」ですね。" + TO_SCREEN, NEXT], [DRAFT])]
    assert draft_number_violations(sc, confirmed, said) == []
    blank = [GREETING, BLANK_Q, turn(["「2日」ですね。" + TO_SCREEN, NEXT], [DRAFT])]
    assert draft_number_violations(sc, blank, [None, "空欄を埋める", "2日です"]) == [
        "T2: 下書きの空欄を白紙で聞いた(summary にある「2日」を示して確かめていない)"]
    in_options_only = [GREETING, turn(["決算の作業日数は、何日短くしましたか?"], quick_replies=["2日", "今はいい"]),
                       turn(["分かりました。"])]
    assert draft_number_violations(sc, in_options_only, [None, "空欄を埋める", "わからない"]) == [
        "T2: 下書きの空欄を白紙で聞いた(summary にある「2日」を示して確かめていない)"]
    assert draft_number_violations(sc, [GREETING], [None]) == []  # 空欄を埋めに入らなければ問わない


def test_the_scenario_and_the_prompt_carry_the_rules():
    assert SC["expect_confirm_number"] == "2日" and "はい、2日です" in SC["persona"]
    assert '{"label": "下書きを見る", "url": arcade_draft の view_url}' in PROMPT
    assert "白紙で聞かず確認形で聞く" in PROMPT and "1つの会話で出すのは1回だけ(2つ目からの数字の後には出さない。" in PROMPT
    assert "こちらは書き込まない" in PROMPT
