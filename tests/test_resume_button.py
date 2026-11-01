"""時間が空いた後のボタンの答え(設計メモ docs/design/conversation-layer.md §4.16。task 415)。

実 LLM なし。偽物の LINE に署名つきの webhook を送り、本物のランタイムと dip の MCP を、偽のトークの AI と偽のハーネスのモデルで動かす。
- 本番のトーク(2026-10-30): [近くで探す] の年齢の問いの [18歳以上] を 6 時間後に押すと、前の会話を続けてエリアを聞く。
  答えた入口のカードを出し直さない。年齢を聞き直さない
- 正社員(c2)、トークの AI(t1)、今の版(v4、c1)でも同じ決まり(版を見ないランタイムの決まり)
- 変えないこと: 30 分を過ぎた自由な発言は新しい会話。同意の後の挨拶(logic.greeting_due)が先。スタンプは答えない。求人のカードのボタンは今までどおり
- 上限: 会話を消すまで(purge.CONVERSATION_TTL)
- 新しい会話に引き継ぐのは、前の相談で保存した希望の項目だけ(入口の答えと年齢は引き継がない)
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from dip_mcp.baito import harness as BH
from dip_mcp.baito import session as baito_session
from dip_mcp.career import harness as CH
from dip_mcp.career import session as career_session
from line_runtime import line_channel, purge, turn
from line_runtime.line_channel import line_user
from tests.test_career_c2 import FakeReadLLM, act
from tests.test_consult import ConsultModel
from tests.test_handle import call, reply
from tests.test_line_data import _say
from tests.test_line_webhook import (  # noqa: F401  line_id_key はフィクスチャ
    ID_KEY,
    RAW_USER,
    _event,
    _later,
    _post,
    _server,
    line_id_key,
)

UID = line_user(RAW_USER, ID_KEY.encode())
HOURS_6 = 6 * 60
ENTRY_CARD = BH.QUICK_TEXT
AGE_QUESTION = BH.NEAR_TEXT[BH.NEAR_AGE]
AREA_QUESTION = BH.NEAR_TEXT[BH.NEAR_AREA]
TIME_QUESTION = BH.NEAR_TEXT[BH.NEAR_TIME]
LATE_REPLY = "18歳以上とのこと、確認できました。アルバイトで、一緒に合いそうな仕事を探しましょう。"
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read": 0, "cache_write": 0}


@pytest.fixture(autouse=True)
def versions(monkeypatch):
    """本番の組み合わせ(v5、c2、t1)。形は C(既定)。"""
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v5")
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c2")
    monkeypatch.setenv(logic.TALK_LAYER_FLAG, "t1")
    monkeypatch.delenv(CH.FORM_ENV, raising=False)


class Tappy:
    """アルバイト・パートのハーネスのモデル: 言う段は「いいね!」と問いの原文。ボタンのターンは読む段を呼ばない(呼ばれたら落とす)。"""

    def __init__(self):
        self.stages: list[str] = []

    async def __call__(self, stage, system, messages):
        self.stages.append(stage)
        assert stage == "say", stage
        q = json.loads(re.search(r"次に聞くこと[（(]JSON[）)]: (\{.*\})", system).group(1))
        return {"reply": "いいね!" + q["text"]}, USAGE


def _start_baito(text: str | None = None, say: str = "一緒に探そう。") -> list:
    """トークの AI: アルバイト・パートに切り替え、playbook を読み、相談を始める(本番の t1 と同じ並び)。"""
    args = {"line_user_id": UID} | ({"text": text} if text is not None else {})
    msg = call("set_search_track", {"line_user_id": UID, "track": "アルバイト・パート", "evidence": "バイトで探す"}, "t0")
    msg.tool_calls = [*msg.tool_calls,
                      {"name": "get_counseling_playbook", "args": {"track": "アルバイト・パート", "channel": "chat"}, "id": "pb",
                       "type": "tool_call"},
                      {"name": "start_baito_consultation", "args": args, "id": "st", "type": "tool_call"}]
    return [msg, reply([say], quick_replies=[])]


def _start_career() -> list:
    msg = call("set_search_track", {"line_user_id": UID, "track": "正社員", "evidence": "正社員で探す"}, "t0")
    msg.tool_calls = [*msg.tool_calls,
                      {"name": "get_counseling_playbook", "args": {"track": "正社員", "channel": "chat"}, "id": "pb", "type": "tool_call"},
                      {"name": "start_career_consultation", "args": {"line_user_id": UID}, "id": "st", "type": "tool_call"}]
    return [msg, reply(["正社員で探しますね。"], quick_replies=[])]


def _line(tmp_path, monkeypatch, model, baito=None, career=None):
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, baito_llm=baito, career_llm=career))
    return server, fake


def _shown(fake) -> str:
    """最後に LINE へ送ったものの全部の文(吹き出し、Flex の文、ボタン)。"""
    return json.dumps(fake.sent()[-1][1], ensure_ascii=False)


def _thread(client, server) -> int:
    return client.portal.call(server.app.state.rt.current_thread, UID)


def _quick_labels(fake) -> list[str]:
    last = fake.sent()[-1][1]["messages"][-1]
    return [i["action"]["label"] for i in (last.get("quickReply") or {}).get("items", [])]


def _to_near_age(client, server, fake) -> None:
    """本番のトークの 10:50 の 3 つ: [アルバイト・パートで探す] → カード、[しゃべるの好き] → 結果、[近くで探す] → 年齢の問い。"""
    _say(client, "アルバイト・パートで探す", 1)
    assert ENTRY_CARD in _shown(fake)
    _say(client, "しゃべるの好き", 2)
    assert "近くで探す" in _shown(fake)
    _say(client, BH.QUICK_NEAR, 3)
    assert AGE_QUESTION in _shown(fake) and _thread(client, server) == 1


# ----------------------------- 本番のトーク -----------------------------
def test_age_button_six_hours_later_goes_on_to_the_area_question(tmp_path, monkeypatch):
    """運営者のトーク(2026-10-30): 6 時間後の [18歳以上] は、前の会話の年齢の答え。答えた入口のカードを出し直さず、
    年齢を聞き直さず、近くで探すの 2 問目(エリア)へ。トークの AI は呼ばない。続きの答えも同じ相談が受ける。"""
    # 直す前の本番の動き: 新しい会話の最初のひとことになり、トークの AI が相談を始め直す(台本の後ろの 2 つ)
    model = ConsultModel(script=[*_start_baito(), *_start_baito(say=LATE_REPLY)])
    server, fake = _line(tmp_path, monkeypatch, model, baito=Tappy())
    with TestClient(server.app) as client:
        _to_near_age(client, server, fake)
        calls_before = len(model.script)
        _later(monkeypatch, server, HOURS_6)
        _say(client, "18歳以上", 4)
        shown = _shown(fake)
        assert ENTRY_CARD not in shown and LATE_REPLY not in shown  # もう答えた問いを出さない
        assert AGE_QUESTION not in shown  # 年齢を聞き直さない
        assert AREA_QUESTION in shown and "東京" in _quick_labels(fake)  # エリアの問いとボタン
        assert _thread(client, server) == 1 and len(model.script) == calls_before  # 同じ会話、トークの AI を呼ばない
        h = baito_session._load(server.state, UID)
        assert h.phase == BH.NEAR_AREA and h.near["age"] == "18_plus" and h.slots["age_band"]["value"] == "18_plus"
        assert h.near["pick"] == "しゃべるの好き"  # 入口の答えも残っている
        _later(monkeypatch, server, 0)
        _say(client, "東京", 5)  # 続きは 30 分以内の答え
        assert TIME_QUESTION in _shown(fake) and _thread(client, server) == 1


@pytest.mark.parametrize("baito,talk", [("v4", None), ("v4", "t1"), ("v5", None)])
def test_the_rule_does_not_look_at_the_versions(tmp_path, monkeypatch, baito, talk):
    """入り口ごとの環境変数の組み合わせによらず、同じ決まり(版を見ないランタイムの決まり)。"""
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, baito)
    if talk is None:
        monkeypatch.delenv(logic.TALK_LAYER_FLAG)
    model = ConsultModel(script=_start_baito())
    server, fake = _line(tmp_path, monkeypatch, model, baito=Tappy())
    with TestClient(server.app) as client:
        _to_near_age(client, server, fake)
        _later(monkeypatch, server, HOURS_6)
        _say(client, "18歳以上", 4)
        assert AREA_QUESTION in _shown(fake) and ENTRY_CARD not in _shown(fake) and _thread(client, server) == 1


def test_entry_card_button_itself_is_an_answer_after_a_gap(tmp_path, monkeypatch):
    """カードの中のボタン(クイックリプライでなく Flex)も、最後に出した問いのボタン。"""
    model = ConsultModel(script=_start_baito())
    server, fake = _line(tmp_path, monkeypatch, model, baito=Tappy())
    with TestClient(server.app) as client:
        _say(client, "アルバイト・パートで探す", 1)
        assert ENTRY_CARD in _shown(fake) and not _quick_labels(fake)  # カードにはクイックリプライが付かない
        _later(monkeypatch, server, HOURS_6)
        _say(client, "しゃべるの好き", 2)
        assert ENTRY_CARD not in _shown(fake) and "近くで探す" in _shown(fake) and _thread(client, server) == 1


# ----------------------------- 変えないこと -----------------------------
@pytest.mark.parametrize("said", ["おはよう", "18歳", "18歳以上です"])
def test_free_text_after_30_minutes_still_starts_a_new_conversation(tmp_path, monkeypatch, said):
    """ボタンと一致しない発言は、今までどおり新しい会話(30 分の決まりは変えない)。途中の相談は閉じる。"""
    model = ConsultModel(script=[*_start_baito(), reply(["こんにちは。"], quick_replies=[])])
    server, fake = _line(tmp_path, monkeypatch, model, baito=Tappy())
    with TestClient(server.app) as client:
        _to_near_age(client, server, fake)
        _later(monkeypatch, server, 31)
        _say(client, said, 4)
        assert "こんにちは。" in _shown(fake) and _thread(client, server) == 2
        assert not baito_session.active(server.state, UID)


def test_within_30_minutes_nothing_changes(tmp_path, monkeypatch):
    model = ConsultModel(script=[*_start_baito()])
    server, fake = _line(tmp_path, monkeypatch, model, baito=Tappy())
    with TestClient(server.app) as client:
        _to_near_age(client, server, fake)
        _later(monkeypatch, server, 29)
        _say(client, "18歳以上", 4)
        assert AREA_QUESTION in _shown(fake) and _thread(client, server) == 1


def test_greeting_after_consent_comes_before_the_button_rule(tmp_path, monkeypatch):
    """同意の後の挨拶がまだ返っていなければ(logic.greeting_due)、30 分を過ぎたボタンの答えも、今までどおり挨拶から始める。"""
    model = ConsultModel(script=[*_start_baito(), reply(["こんにちは、aica です。"], quick_replies=[])])
    server, fake = _line(tmp_path, monkeypatch, model, baito=Tappy())
    with TestClient(server.app) as client:
        _to_near_age(client, server, fake)
        logic.await_greeting(server.state, UID)
        _later(monkeypatch, server, HOURS_6)
        _say(client, "18歳以上", 4)
        assert "こんにちは、aica です。" in _shown(fake) and _thread(client, server) == 2
        assert not logic.greeting_due(server.state, UID)


def test_sticker_after_30_minutes_is_still_not_answered(tmp_path, monkeypatch):
    model = ConsultModel(script=_start_baito())
    server, fake = _line(tmp_path, monkeypatch, model, baito=Tappy())
    with TestClient(server.app) as client:
        _to_near_age(client, server, fake)
        sent = len(fake.sent())
        _later(monkeypatch, server, 31)
        e = _event("message", event_id="01S1", token="reply-token-s") | {"message": {"type": "sticker", "id": "1"}}
        _post(client, [e])
        assert len(fake.sent()) == sent and _thread(client, server) == 1


def test_job_card_buttons_are_not_answers():
    """求人のカードの「詳しく見る」「保存」は問いの答えではないので、ボタンの答えに入れない(今までどおり 30 分で新しい会話)。"""
    card = {"job_id": "J1", "title": "みほんカフェ", "employer": "みほん", "shift": "昼", "station": "新宿", "walk_min": 3, "reasons": []}
    bot = {"bubbles": ["見つけたよ。"], "quick_replies": ["もっと絞る", "ちがう"], "cards": [card], "buttons": [], "actions": []}
    assert line_channel.answer_buttons(bot) == {"もっと絞る", "ちがう"}
    assert line_channel.answer_buttons(None) == set()


# ----------------------------- 上限 -----------------------------
@pytest.mark.parametrize("gap,same", [(timedelta(days=30) - timedelta(minutes=1), True),
                                      (timedelta(days=30) + timedelta(minutes=1), False)])
def test_button_answer_keeps_the_conversation_until_it_would_be_erased(tmp_path, monkeypatch, gap, same):
    """続けられる上限は、会話を消すまで(purge.CONVERSATION_TTL)。掃除がいつ走ったかによらない。"""
    assert purge.CONVERSATION_TTL == timedelta(days=30) and turn.BUTTON_RESUME_WINDOW == purge.CONVERSATION_TTL
    model = ConsultModel(script=[*_start_baito(), reply(["こんにちは。"], quick_replies=[])])
    server, fake = _line(tmp_path, monkeypatch, model, baito=Tappy())
    with TestClient(server.app) as client:
        _to_near_age(client, server, fake)
        _later(monkeypatch, server, int(gap.total_seconds() // 60))
        _say(client, "18歳以上", 4)
        assert _thread(client, server) == (1 if same else 2)
        assert (AREA_QUESTION in _shown(fake)) is same


def test_answers_last_question_reads_only_the_last_reply():
    now = datetime(2026, 10, 30, tzinfo=UTC)
    old = {"bot": {"bubbles": ["年齢は?"], "quick_replies": ["18歳以上"]}}
    new = {"bot": {"bubbles": ["エリアは?"], "quick_replies": ["東京"]}}
    failed = {"bot": None}  # 返信が保存されなかったターン: その前の返信のボタンを見る
    conv = {"last_at": now - timedelta(hours=6), "turns": [old, new]}
    assert turn.answers_last_question(conv, "東京", now) and turn.answers_last_question(conv, " 東京 ", now)
    assert not turn.answers_last_question(conv, "18歳以上", now)  # 前の前の問いのボタン
    assert not turn.answers_last_question(conv, None, now)
    assert turn.answers_last_question(conv | {"turns": [new, failed]}, "東京", now)
    assert not turn.answers_last_question({"last_at": None, "turns": []}, "東京", now)  # 返信の無い会話


# ----------------------------- 新しい会話に引き継ぐもの -----------------------------
def test_new_conversation_carries_only_saved_preferences_not_the_entry_answer_or_age(tmp_path, monkeypatch):
    """自由な発言で新しい会話になると、途中の相談は閉じて、希望の項目に入るもの(エリア)だけが残る。入口の答え(推測)と年齢は
    希望の項目が無いので残らない。次の相談は入口のカードから始まり、[近くで探す] で年齢を聞き直し、エリアは聞かない。
    本番で返事に出た「18歳以上」は引き継ぎではなく、その発言をトークの AI が読んだもの(§4.16)。"""
    model = ConsultModel(script=[*_start_baito(), *_start_baito(say="また探そう。")])
    server, fake = _line(tmp_path, monkeypatch, model, baito=Tappy())
    with TestClient(server.app) as client:
        _to_near_age(client, server, fake)
        _say(client, "18歳以上", 4)
        _say(client, "東京", 5)
        _later(monkeypatch, server, HOURS_6)
        _say(client, "またバイト探したい", 6)
        assert _thread(client, server) == 2 and ENTRY_CARD in _shown(fake)
        prefs = logic.stated_preferences(server.state.user(UID))
        assert prefs["area"]["value"] == "東京" and not set(prefs) & {"age", "age_band", "purpose"}
        h = baito_session._load(server.state, UID)
        assert "age" not in h.near and not h.slots.get("age_band") and not h.near.get("pick") and h.near["area"] == "東京"
        _later(monkeypatch, server, 0)
        _say(client, "しゃべるの好き", 7)
        _say(client, BH.QUICK_NEAR, 8)
        assert AGE_QUESTION in _shown(fake)  # 年齢は聞き直す


# ----------------------------- 正社員(c2) -----------------------------
def test_career_button_after_six_hours_goes_on_to_the_next_question(tmp_path, monkeypatch):
    """c2 の問いのボタンも同じ: 6 時間後に押すと前の相談の答え。同じ問いを出し直さず、次の問いへ。トークの AI は呼ばない。"""
    model = ConsultModel(script=[*_start_career(), reply(["(新しい会話)"], quick_replies=[])])
    llm = FakeReadLLM()
    server, fake = _line(tmp_path, monkeypatch, model, career=llm)
    with TestClient(server.app) as client:
        _say(client, "正社員で探す", 1)
        first = _shown(fake)
        qid = career_session._load(server.state, UID).current["qid"]
        labels = _quick_labels(fake)
        assert labels, first
        left = len(model.script)
        _later(monkeypatch, server, HOURS_6)
        _say(client, labels[0], 2)
        assert len(model.script) == left and _thread(client, server) == 1
        h = career_session._load(server.state, UID)
        assert h.current["qid"] != qid and "(新しい会話)" not in _shown(fake)


def test_career_form_d_flex_button_after_six_hours_continues(tmp_path, monkeypatch):
    """形 D: ボタンが Flex の中にあるものも、最後に出したボタン(クイックリプライに無いものも読む)。"""
    monkeypatch.setenv(CH.FORM_ENV, "D")
    model = ConsultModel(script=[*_start_career(), reply(["(新しい会話)"], quick_replies=[])])
    server, fake = _line(tmp_path, monkeypatch, model, career=FakeReadLLM())
    with TestClient(server.app) as client:
        _say(client, "正社員で探す", 1)
        sent = fake.sent()[-1][1]["messages"]
        said = [t for m in sent if m["type"] == "flex" for t in line_channel.message_actions(m)]
        said += _quick_labels(fake)
        assert said, sent
        left = len(model.script)
        _later(monkeypatch, server, HOURS_6)
        _say(client, said[0], 2)
        assert len(model.script) == left and _thread(client, server) == 1


def test_career_free_text_after_six_hours_starts_a_new_conversation(tmp_path, monkeypatch):
    model = ConsultModel(script=[*_start_career(), reply(["こんにちは。"], quick_replies=[])])
    server, fake = _line(tmp_path, monkeypatch, model, career=FakeReadLLM())
    with TestClient(server.app) as client:
        _say(client, "正社員で探す", 1)
        _later(monkeypatch, server, HOURS_6)
        _say(client, "おはよう", 2)
        assert "こんにちは。" in _shown(fake) and _thread(client, server) == 2
        assert not career_session.active(server.state, UID)


# ----------------------------- トークの AI(t1) -----------------------------
WEEKEND = "土日だけ駅近で働きたい"
TAP = "アルバイト・パートで探す"


class WeekendBaito(Tappy):
    """読む段: 始めた発言の「土日だけ」を読む(ボタンの文には値が無い)。"""

    def __init__(self):
        super().__init__()
        self.read: list[str] = []

    async def __call__(self, stage, system, messages):
        if stage != "read":
            return await super().__call__(stage, system, messages)
        said = re.search(r"本人の今の発言: (.*)", system).group(1).strip()
        self.read.append(said)
        acts = [act("add", slot="weekday_or_weekend", text="土日だけ", value="weekend")] if "土日" in said else []
        from line_runtime.baito_chat import SCHEMAS

        return SCHEMAS["read"](acts=acts, hook=None, mentions=[], ng=[]).model_dump(), USAGE


def test_entry_button_of_the_talk_ai_six_hours_later_keeps_what_was_said_before(tmp_path, monkeypatch):
    """t1 の区分を聞くボタン: 6 時間後に押しても前の会話の続き。トークの AI は前の発言を読め、その発言を相談に渡せる(§4.13)。
    新しい会話にすると、前の発言は会話に無いので渡せず(said_of)、条件を聞き直すことになる。"""
    seen: list[list[str]] = []

    def ask(messages):
        seen.append([m.content for m in messages if m.type == "human"])
        return reply(["どちらで探しますか?"], quick_replies=[TAP, "正社員で探す"])

    def start_with_earlier_words(messages):
        seen.append([m.content for m in messages if m.type == "human"])
        return _start_baito(WEEKEND)[0]

    model = ConsultModel(script=[ask, start_with_earlier_words, reply(["探しますね。"], quick_replies=[])])
    llm = WeekendBaito()
    server, fake = _line(tmp_path, monkeypatch, model, baito=llm)
    with TestClient(server.app) as client:
        _say(client, WEEKEND, 1)
        assert TAP in _quick_labels(fake)
        _later(monkeypatch, server, HOURS_6)
        _say(client, TAP, 2)
        assert _thread(client, server) == 1 and seen[1] == [WEEKEND, TAP]
        assert llm.read == [WEEKEND]  # 前の発言を読む段が読んだ(ボタンの文ではなく)
        days = baito_session._load(server.state, UID).slots["days_per_week"]
        assert (days["value"], days["confidence"]) == ("1-2", "low")  # 前の発言の値を推測で入れた


def test_talk_ai_free_text_after_six_hours_starts_a_new_conversation(tmp_path, monkeypatch):
    model = ConsultModel(script=[reply(["どちらで探しますか?"], quick_replies=[TAP, "正社員で探す"]),
                                 reply(["こんにちは。"], quick_replies=[])])
    server, _ = _line(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _say(client, "こんにちは", 1)
        _later(monkeypatch, server, HOURS_6)
        _say(client, "やっぱり明日にする", 2)
        assert _thread(client, server) == 2
