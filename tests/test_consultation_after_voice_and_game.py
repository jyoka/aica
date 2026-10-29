"""ハーネスの相談の途中で、音声相談やゲームの結果がトークに入った後の、本人の次の発言の行き先(task 364。設計メモ
docs/design/conversation-layer.md の §2.4)。実 LINE・実 LiveKit・実 LLM は呼ばない。dip の MCP は本物。

今の本番の組み合わせ(BAITO_HARNESS_VERSION=v4、CAREER_HARNESS_VERSION=c1、CAREER_HARNESS_FORM=D)で、本人から見た流れを通す:
LINE のトークでアルバイト・パート(または正社員)の相談を始め、途中まで答える → LIFF の画面で音声相談をする(音声ワーカーが
結果を送る)か、ゲームで下書きを保存する → トークに決まった返信が入る → 本人がその返信の選択肢を押す。

音声相談とゲームは、会話の外のイベント(履歴書のアップロードと同じ)なので、途中の相談を閉じる(そこまでの確かめた答えは
保存する)。音声は本人が部屋に入ったとき(ワーカーが面談の中で希望を保存する前)、ゲームは下書きを保存したとき。
次の発言はトークの AI が受け、音声やゲームの結果を文脈に読んで続ける。相談に戻すと、本人が押した結果の選択肢
(「条件を変えて探す」「求人を探す」)を前の問いの答えとして読んでしまう。
"""

from __future__ import annotations

from functools import partial

import pytest

from dip_mcp import logic
from dip_mcp.baito import harness as BH
from dip_mcp.baito import session as baito_session
from dip_mcp.career import harness as CH
from dip_mcp.career import session as career_session
from dip_mcp.career import store as career_store
from line_runtime.arcade_reply import QUICK_REPLIES as ARCADE_QUICK
from line_runtime.line_channel import LineLogin, line_user
from tests.test_arcade_ai import MD
from tests.test_baito_chat import FakeBaitoLLM
from tests.test_career_chat import FakeCareerLLM
from tests.test_consult import ConsultModel
from tests.test_handle import call, reply
from tests.test_liff import FakeLineLogin
from tests.test_liff_actions import (
    CHANNEL,
    ID_KEY,
    INTERNAL_KEY,
    LIFF_ID,
    LIVEKIT_ENV,
    WORKER,
    _liff_client,
    _no_liff,
)
from tests.test_line_data import _say
from tests.test_line_webhook import RAW_USER, _server

GAME = "the-case-of-you"
AFTER = "結果を見ました。続きはトークで相談します。"  # トークの AI の返事(台本)


@pytest.fixture(autouse=True)
def production_flags(monkeypatch):
    """今の本番の組み合わせ。"""
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    monkeypatch.setenv(CH.FORM_ENV, "D")


# 相談の始め方(トークの AI の台本)と、途中まで進める本人のタップ。アルバイト・パートは入口のカードの次、正社員は「なぜ今」で止める
BAITO = {
    "start": lambda uid: [
        call("get_counseling_playbook", {"track": "アルバイト・パート", "channel": "chat"}, "pb"),
        call("start_baito_consultation", {"line_user_id": uid}, "st"),
        reply(["合いそうなバイトを、一緒に探しましょう。"], quick_replies=["(ハーネスの選択肢に置き換わる)"])],
    "first": "まず相談する",
    "taps": [BH.QUICK_NONE],
    "session": baito_session,
    "document_kind": "履歴書",
}
CAREER = {
    "start": lambda uid: [
        call("set_search_track", {"line_user_id": uid, "track": "正社員", "evidence": "正社員で転職したい"}, "t0"),
        call("get_counseling_playbook", {"track": "正社員", "channel": "chat"}, "pb"),
        call("start_career_consultation", {"line_user_id": uid}, "st"),
        reply(["(ハーネスの吹き出しに置き換わる)"], quick_replies=["(ハーネスの選択肢に置き換わる)"])],
    "first": "正社員で転職したいので相談したい",
    "taps": [CH.START_GO, "同じ仕事で上へ"],  # 形 D の「始める」→ segment。次は「なぜ今」(文で答える問い)
    "session": career_session,
    "document_kind": None,  # 正社員の区分から職務経歴書に決まる
}
CONSULTATIONS = {"baito": BAITO, "career": CAREER}


def _texts(fake) -> list[str]:
    """偽物の LINE に最後に送った返信の、文の吹き出し。"""
    return [m["text"] for m in fake.sent()[-1][1]["messages"] if m["type"] == "text"]


def _buttons(fake) -> list[str]:
    """偽物の LINE に最後に送った返信の、押せる文(クイックリプライと、Flex のカードのボタン)。"""
    found: list[str] = []

    def walk(v):
        if isinstance(v, dict):
            if (v.get("action") or {}).get("type") == "message":
                found.append(v["action"]["text"])
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(fake.sent()[-1][1]["messages"])
    return found


def _voice(client, server, uid: str, preferences: dict) -> None:
    """LIFF の画面で音声相談をする。音声ワーカーは、本人が部屋に入ったことを知らせ、面談の中で話した希望をその場で dip に
    保存し(save_user_preferences)、終わりに結果を送る(求人なし)。ワーカーの経路は共有鍵で守る。
    どれもトークのランタイムと同じイベントループで呼ぶ(相談を閉じるときに dip の MCP を呼ぶ。本番の Web プロセスも 1 つのループ)。"""
    assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
    r = client.post("/api/liff/voice/start", json={"user_id": uid, "id_token": "good"})
    assert r.status_code == 200, r.text
    room = r.json()["room"]
    assert client.post("/api/voice/session", headers=WORKER,
                       json={"user_id": uid, "room": room, "state": "joined"}).json() == {"allowed": True}
    assert logic.save_preferences(server.state, uid, preferences, room=room)["saved"]
    out = client.post("/api/voice/result", headers=WORKER,
                      json={"user_id": uid, "room": room, "preferences": preferences}).json()
    assert out["talk"] is True


def _game(client, kind: str | None) -> None:
    """LIFF の画面でゲームを遊び、下書きを保存する。"""
    assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
    play = client.post("/api/liff/arcade/start", json={"game": GAME}).json()["play"]
    body = {"game": GAME, "play": play, "markdown": MD, "id_token": "good"} | ({"document_kind": kind} if kind else {})
    r = client.post("/api/liff/arcade/save", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["talk"] is True


def _setup(tmp_path, monkeypatch, script):
    """LINE と LIFF の設定がある Web サーバー。トークの AI は台本(script(uid))、ハーネスのモデルは偽物。
    (server, 偽物の LINE の API, 台本のモデル, バイトのモデル, 正社員のモデル, 本人の aica のユーザーID)"""
    from line_runtime.runtime import LineRuntime

    _no_liff(monkeypatch)
    model = ConsultModel(script=[])
    server, fake = _server(tmp_path, monkeypatch, model, AICA_ID_KEY=ID_KEY, LINE_LIFF_ID=LIFF_ID,
                           LINE_LIFF_CHANNEL_ID=CHANNEL, AICA_INTERNAL_KEY=INTERNAL_KEY, **LIVEKIT_ENV)
    uid = line_user(RAW_USER)
    model.script = script(uid)
    baito_llm, career_llm = FakeBaitoLLM(), FakeCareerLLM()
    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, baito_llm=baito_llm,
                                                       career_llm=career_llm))
    monkeypatch.setattr(server, "line_login", lambda: LineLogin(CHANNEL, transport=FakeLineLogin().transport))
    return server, fake, model, baito_llm, career_llm, uid


def _progress(client, fake, c: dict, steps: list[str]) -> None:
    """LINE のトークで相談を始めて、押せるボタンか文で途中まで答える(ハーネスが受け持つ)。"""
    assert _say(client, c["first"], 1).status_code == 200
    for n, said in enumerate(steps, 2):
        assert said in _buttons(fake) or said not in c["taps"], (said, _buttons(fake))
        assert _say(client, said, n).status_code == 200


def _last_bot(client, server, uid: str) -> dict:
    """今の会話の最後の返信(画面に描き直す形)。音声とゲームの結果のターンも入る。"""
    return client.portal.call(server.app.state.rt.conversation, uid)["turns"][-1]["bot"]


@pytest.mark.parametrize("event", ["voice", "game"])
@pytest.mark.parametrize("kind", ["baito", "career"])
def test_after_voice_or_game_the_next_message_goes_to_the_talk_ai_not_back_into_the_consultation(
        kind, event, tmp_path, monkeypatch):
    """相談の途中で音声相談かゲームの結果がトークに入ると、途中の相談は閉じる。本人が結果の返信の選択肢を押すと、
    トークの AI が受ける(相談の前の問いの答えとして読まない。ハーネスのモデルも呼ばない)。
    直す前は、どの組み合わせでもハーネスが受けていた: アルバイト・パートは「下のボタンから選んでね！」(選択肢なし)、
    正社員は選択肢を「なぜ今」の答えにして「働きたい場所」へ進んだ。"""
    c = CONSULTATIONS[kind]
    seen: list = []

    def after(messages):  # 結果の後の本人の発言を受けたトークの AI。読んだ文脈を残す
        seen.extend(messages)
        return reply([AFTER], quick_replies=["今はいい"])

    server, fake, model, baito_llm, career_llm, uid = _setup(tmp_path, monkeypatch, lambda u: [*c["start"](u), after])
    st = server.state
    with _liff_client(server) as client:  # LINE のアプリ(トークと LIFF の画面)
        _progress(client, fake, c, c["taps"])
        assert c["session"].active(st, uid)
        llm_calls = len(baito_llm.calls) + len(career_llm.calls)

        # LIFF の画面で音声相談をする / ゲームで下書きを保存する。トークに決まった返信が入る(LLM は呼ばない)
        if event == "voice":
            _voice(client, server, uid, {"job_type": "カフェ", "time_band": "夕方"})
        else:
            _game(client, c["document_kind"])
        shown = _last_bot(client, server, uid)
        assert shown["quick_replies"] == (["条件を変えて探す", "今はいい"] if event == "voice" else list(ARCADE_QUICK))
        assert not c["session"].active(st, uid)  # 結果を受けたときに、途中の相談を閉じる

        # 本人が、結果の返信の選択肢を押す
        said = shown["quick_replies"][0]
        assert _say(client, said, 9).status_code == 200

    assert model.script == []  # トークの AI が受けた
    assert _texts(fake) == [AFTER]
    assert len(baito_llm.calls) + len(career_llm.calls) == llm_calls
    assert not c["session"].active(st, uid)
    # トークの AI は、相談のやりとりと、音声やゲームの結果のターンを文脈として読む
    humans = [m.content for m in seen if m.type == "human"]
    assert humans[-1] == said
    mark = "本人が音声相談を終えた" if event == "voice" else "本人がゲーム(レジュメ・アーケード)で書類の下書きを保存した"
    assert mark in humans[-2]
    assert c["taps"][-1] in humans


def test_answers_are_saved_when_voice_closes_the_consultation_and_the_voice_preferences_win(tmp_path, monkeypatch):
    """正社員の相談で答えた勤務地となぜ今は、音声の結果で閉じるときに保存する。その後の音声で別の勤務地を話したら、
    音声の値が残る(閉じるときの保存が、音声で話した新しい希望を上書きしない)。"""
    c = CAREER
    server, fake, _, _, _, uid = _setup(tmp_path, monkeypatch, c["start"])
    st = server.state
    with _liff_client(server) as client:  # LINE のアプリ(トークと LIFF の画面)
        _progress(client, fake, c, [*c["taps"], "評価の仕組みが合わないから", "大阪市内"])
        assert career_session.active(st, uid)
        _voice(client, server, uid, {"area": "名古屋駅の近く", "job_type": "物流の管理"})
    assert not career_session.active(st, uid)
    prefs = {k: v["value"] for k, v in logic.stated_preferences(st.user(uid)).items()}
    assert prefs["area"] == "名古屋駅の近く" and prefs["job_type"] == "物流の管理"
    assert prefs["why_now"] == "評価の仕組みが合わないから"  # 相談で答えた分は残る
    assert st.user(uid)[career_store.KEY]["segment"]["value"] == "same_path"
