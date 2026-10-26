"""LINE のトークから LIFF 画面で、履歴書・音声相談・レジュメ・アーケードを使う(task 204)。

実 LINE・実 LiveKit・実 LLM は呼ばない: LINE の Messaging API と ID トークンの検証は偽物の LINE(httpx.MockTransport)、
LLM は台本の偽のモデル、履歴書の読み取りはスタブ、LiveKit は接続トークンを作るだけ(画面のテストでは CDN のライブラリを代役に差し替える)。

- エージェントは今の窓口(Web / LINE)を知る。LINE では、LIFF で開けるものだけを勧める
- 返信の actions(resume_upload / voice_consult)は、LIFF の設定があれば LIFF 画面へのボタン。無ければ今の断りの文
- LIFF 画面から始めた音声相談と履歴書の結果は、その本人の LINE のトークに push で届く
- レジュメ・アーケードのボタンは、LIFF の設定があるときだけ LINE に出る
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from dip_mcp import logic
from dip_mcp.arcade import GAMES as ARCADE_GAMES
from dip_mcp.data import load_jobs
from line_runtime import legal, line_channel
from line_runtime.line_channel import LineLogin, line_user, to_messages
from tests.test_handle import reply
from tests.test_liff import FakeLineLogin
from tests.test_line_webhook import (
    PUSH,
    RAW_OTHER,
    RAW_USER,
    REPLY,
    TestClient,
    _event,
    _post,
    _server,
)
from tests.test_resume_intake import StubClient
from tests.test_resume_upload import FILENAME, clean_draft
from tests.test_revisit import Greeter
from tests.test_talk_actions import FAKE_LIVEKIT, Advisor

LIFF_ID = "2000000000-OfflineT"
CHANNEL = "2000000000"
ID_KEY = "offline-test-aica-id-key-0123456789abcdef"
INTERNAL_KEY = "offline-test-internal-key-offline-test"
WORKER = {"Authorization": f"Bearer {INTERNAL_KEY}"}  # 音声ワーカー(サーバー間の経路)の共有鍵
LIFF_ENVS = ("LINE_LIFF_ID", "LINE_LIFF_CHANNEL_ID", "LINE_LOGIN_CHANNEL_ID")
LIVEKIT_ENV = {"LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k",
               "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret"}
# 実機で 2 回再現した返事(エージェントがファイルを勧め、actions を付ける)
PROPOSE = ["いくつか伺いながら一緒に整理します。", "これまでのお仕事を教えてください。職務経歴書や履歴書があれば、読み込むと早いです。"]
ACTIONS = ["resume_upload", "voice_consult"]


def test_line_offers_liff_buttons_instead_of_refusing_right_after_recommending(tmp_path, monkeypatch):
    """実機の再現: LINE で話すと、エージェントが履歴書と音声を勧めた直後に「LINE ではまだ…」の断りが続いていた。
    LIFF の設定があれば、勧めた操作は LIFF 画面へのボタンになり、断りの文は出ない。エージェントは LINE の窓口だと知っている。"""
    systems: list[str] = []

    def propose(messages):
        systems.append(messages[0].content)
        return reply(PROPOSE, quick_replies=["接客", "事務", "今はいい"], actions=ACTIONS)

    _no_liff(monkeypatch)  # 前の名前(LINE_LOGIN_CHANNEL_ID)のチャネル ID でも LIFF を使う
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[propose]),
                           LINE_LIFF_ID=LIFF_ID, LINE_LOGIN_CHANNEL_ID=CHANNEL, AICA_ID_KEY=ID_KEY,
                           AICA_INTERNAL_KEY=INTERNAL_KEY, **LIVEKIT_ENV)
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text="まず相談する")]).status_code == 200

    ((path, body),) = fake.sent()
    assert path == REPLY
    texts = [m["text"] for m in body["messages"] if m["type"] == "text"]
    assert texts == PROPOSE  # 勧めた本文の後に、断りの文が続かない
    assert not any("LINE ではまだ" in t or "使えません" in t or "受け取れません" in t for t in texts)
    (flex,) = [m for m in body["messages"] if m["type"] == "flex"]
    assert _heading(flex) is None  # 経験の問いの答えは文字の選択肢なので、ボタンの見出しにしない(見出しは初回訪問の挨拶だけ。task 231)
    uris = [b["action"]["uri"] for b in flex["contents"]["body"]["contents"]]
    assert uris == [f"https://liff.line.me/{LIFF_ID}/resume/upload", f"https://liff.line.me/{LIFF_ID}/voice"]
    (system,) = systems
    assert "# 窓口" in system and "LINE のトーク" in system  # エージェントに今の窓口が伝わっている



def _heading(flex: dict) -> str | None:
    """ボタンの箱の見出し(header の文)。無ければ None。"""
    header = flex["contents"].get("header")
    return header["contents"][0]["text"] if header else None


def _no_liff(monkeypatch) -> None:
    for k in LIFF_ENVS + line_channel.LIVEKIT_ENVS:
        monkeypatch.delenv(k, raising=False)


def test_without_liff_the_agent_is_told_line_cannot_take_files_or_voice_and_the_refusal_stays_as_a_guard(tmp_path, monkeypatch):
    """LIFF の設定が無ければ、エージェントには「LINE では履歴書・音声・ゲームは使えない」と伝える。それでも actions を付けたら、
    今までどおりの断りの文(守り)。Web のトーク画面のターンには Web の窓口を伝える。"""
    systems: list[str] = []

    def answer(messages):
        systems.append(messages[0].content)
        return reply(["これまでのお仕事を教えてください。"], quick_replies=["接客", "事務", "今はいい"], actions=ACTIONS)

    _no_liff(monkeypatch)
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[answer, answer]), AICA_ID_KEY=ID_KEY)
    with TestClient(server.app) as client:
        _post(client, [_event("message", text="まず相談する")])
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        web = client.post("/api/event", json={"user_id": uid, "event": "message", "text": "まず相談する"}).json()
    line_system, web_system = systems
    for word in ("LINE のトーク", "履歴書・職務経歴書のファイルの読み込みは、LINE では使えない", "音声相談は、LINE では使えない",
                 "レジュメ・アーケード(/liff/arcade)は、LINE では開けない"):
        assert word in line_system, word
    assert "Web のトーク画面" in web_system and "LINE のトーク。" not in web_system
    assert web["actions"] == ACTIONS  # Web は今までどおり、トークの中のボタン
    ((_, body),) = fake.sent()
    texts = [m["text"] for m in body["messages"] if m["type"] == "text"]
    assert texts == ["これまでのお仕事を教えてください。", *line_channel.ACTION_NOTES.values()]


def test_line_buttons_follow_what_liff_can_open(monkeypatch):
    """actions と、レジュメ・アーケードのボタン: LIFF の設定があれば LIFF URL。音声は LiveKit の設定も要る。
    LIFF の設定が無ければ、actions は断りの文に、ゲームのボタンは(公開 URL があっても)出さない。"""
    _no_liff(monkeypatch)
    r = {"bubbles": ["どうぞ"], "actions": ACTIONS, "quick_replies": ["今はいい"],
         "buttons": [{"label": "ゲームで書類を作る", "url": "/liff/arcade"}, {"label": "応募の続き", "url": "/liff/apply?job=a001"}]}
    msgs = to_messages(r, "https://aica.example.test")
    assert [m.get("text") for m in msgs if m["type"] == "text"] == ["どうぞ", *line_channel.ACTION_NOTES.values()]
    (flex,) = [m for m in msgs if m["type"] == "flex"]
    assert [b["action"]["uri"] for b in flex["contents"]["body"]["contents"]] == ["https://aica.example.test/liff/apply?job=a001"]
    assert line_channel.liff_features() == ()

    monkeypatch.setenv("LINE_LIFF_ID", LIFF_ID)
    monkeypatch.setenv("LINE_LIFF_CHANNEL_ID", CHANNEL)
    assert line_channel.liff_features() == ("resume_upload", "arcade")  # LiveKit の設定が無い: 音声は使えない
    msgs = to_messages(r, "")
    assert [m.get("text") for m in msgs if m["type"] == "text"] == ["どうぞ", line_channel.ACTION_NOTES["voice_consult"]]
    (flex,) = [m for m in msgs if m["type"] == "flex"]
    links = [(b["action"]["label"], b["action"]["uri"]) for b in flex["contents"]["body"]["contents"]]
    assert links == [("履歴書を読み込む", f"https://liff.line.me/{LIFF_ID}/resume/upload"),  # 操作のボタンが先(task 237)
                     ("ゲームで書類を作る", f"https://liff.line.me/{LIFF_ID}/arcade"),
                     ("応募の続き", f"https://liff.line.me/{LIFF_ID}/apply?job=a001")]
    assert "quickReply" in msgs[-1]
    for k, v in LIVEKIT_ENV.items():
        monkeypatch.setenv(k, v)
    assert line_channel.liff_features() == ("resume_upload", "voice_consult", "arcade")
    assert [m["type"] for m in to_messages(r, "")] == ["text", "flex"]  # 断りの文は無い


# 初回訪問の挨拶(task 231)。本文は dip の Prompt(entries.json の visit)の例と同じ言い方(task 237 から本文で 3 つを並べない)
GREETING = ["お仕事探しの相談と、求人のご提案をします。",
            "ゲームは、遊びながら履歴書や職務経歴書の下書きを作れます。どれから始めますか?"]


def _liff_on(monkeypatch, voice: bool = True) -> None:
    _no_liff(monkeypatch)
    monkeypatch.setenv("LINE_LIFF_ID", LIFF_ID)
    monkeypatch.setenv("LINE_LIFF_CHANNEL_ID", CHANNEL)
    for k, v in (LIVEKIT_ENV if voice else {}).items():
        monkeypatch.setenv(k, v)


def test_the_first_visit_greeting_is_two_line_messages_with_the_question_over_the_buttons(monkeypatch):
    """初回の挨拶は LINE で 2 通: 本文と、問いを見出しにした入口のボタン(LINE のボタンのテンプレートと同じ形)。
    問いは通知とトークの一覧にも出る(altText)。文字の選択肢は、ボタンのメッセージのクイックリプライ。"""
    _liff_on(monkeypatch)
    r = {"entry": "visit", "bubbles": GREETING, "actions": ["voice_consult", "resume_upload"], "quick_replies": ["求人を探す", "まず相談する", "今はいい"],
         "buttons": [{"label": line_channel.ARCADE_LABEL, "url": "/liff/arcade"}], "cards": []}
    msgs = to_messages(r, "", greeting="visit")
    assert [m["type"] for m in msgs] == ["text", "flex"]
    assert msgs[0]["text"] == GREETING[0] and "quickReply" not in msgs[0]
    flex = msgs[1]
    assert _heading(flex) == GREETING[1] and flex["altText"] == GREETING[1]
    assert [b["action"]["label"] for b in flex["contents"]["body"]["contents"]] == [
        "音声で相談する", "履歴書を読み込む", "ゲームで書類を作る"]  # リッチメニューと同じ並び(task 237)
    assert [i["action"]["label"] for i in flex["quickReply"]["items"]] == ["求人を探す", "まず相談する", "今はいい"]
    # 前の挨拶(本文、問い、見出しの無いボタンの箱の 3 通)と同じ返信も、いまは 2 通になる
    old = r | {"bubbles": ["お仕事探しの相談や求人のご提案ができます。音声相談、履歴書の読み込み、ゲームでの書類作りもできます。",
                           "どれから始めますか？"]}
    assert len(to_messages(old, "", greeting="visit")) == 2
    # 挨拶のターンかどうかは呼び出し元が決める。会話の入口(entry)が visit でも、挨拶のターンでなければ見出しにしない
    assert [m["type"] for m in to_messages(r, "")] == ["text", "text", "flex"]


def test_the_heading_is_only_used_when_it_keeps_the_order_and_a_plain_bubble(monkeypatch):
    """見出しにするのは、初回訪問の挨拶のターンで、ボタンがあり、求人が無く、吹き出しが 2 つ以上のときの最後の吹き出しだけ。
    吹き出しが 1 つなら、本文の吹き出しをそのまま残す(ボタンだけのメッセージにしない)。求人があれば、問いを求人の後ろへ動かさない。"""
    _liff_on(monkeypatch)
    one = {"entry": "visit", "bubbles": ["遊びながら書類を作れます。"], "buttons": [{"label": line_channel.ARCADE_LABEL, "url": "/liff/arcade"}]}
    msgs = to_messages(one, "", greeting="visit")
    assert [m["type"] for m in msgs] == ["text", "flex"] and _heading(msgs[1]) is None
    assert msgs[1]["altText"] == "リンク: ゲームで書類を作る"
    card = {"job_id": "a001", "title": "カフェ", "employer": "店", "reasons": ["近い"]}
    with_cards = {"entry": "visit", "bubbles": ["3件あります。", "気になるものはありますか?"], "cards": [card],
                  "buttons": [{"label": "応募の続き", "url": "/liff/apply?job=a001"}]}
    assert [m.get("text") for m in to_messages(with_cards, "", greeting="visit")][:2] == ["3件あります。", "気になるものはありますか?"]
    joined = {"bubbles": ["読み取りました。", "希望の勤務地はどこですか?"],
              "buttons": [{"label": "読み取り結果を見る", "url": "/liff/resume?rid=0"}]}
    msgs = to_messages(joined, "")  # 2 つの返信をつないだもの(server._joined。履歴書の続きのターン)は見出しにしない
    assert [m["type"] for m in msgs] == ["text", "text", "flex"] and _heading(msgs[-1]) is None
    no_links = {"bubbles": ["一つ目。", "二つ目?"]}
    assert [m["text"] for m in to_messages(no_links, "")] == ["一つ目。", "二つ目?"]


def test_the_revisit_greeting_puts_the_offer_over_the_buttons_and_the_question_last(monkeypatch):
    """再訪の挨拶(task 234。Prompt の「再訪の挨拶の並び」の例)は LINE で 2 通: 挨拶と勧めの1文を見出しにした入口のボタンの箱と、
    確認の1問。確認の1問は「はい」で答える問いなので、答えではないボタンの見出しにせず、文字の選択肢と一緒に最後に残す。
    勧めの文はボタンのすぐ上に載る。ボタンの文言はリッチメニューと同じ(LIFF_ACTIONS と ARCADE_LABEL)。"""
    _liff_on(monkeypatch)
    welcome = "おかえりなさい。履歴書を読み込むと、経歴を一から書かずに探せます。"
    question = "前回はアルバイトで探していましたが、最近見ていたカフェのお仕事で探しますか?"
    r = {"entry": "revisit", "bubbles": [welcome, question], "actions": ["voice_consult", "resume_upload"],
         "quick_replies": ["はい", "ほかの条件で探す", "今はいい"],
         "buttons": [{"label": line_channel.ARCADE_LABEL, "url": "/liff/arcade"}], "cards": []}
    msgs = to_messages(r, "", greeting="revisit")
    assert [m["type"] for m in msgs] == ["flex", "text"]
    assert _heading(msgs[0]) == welcome and msgs[0]["altText"] == welcome and "quickReply" not in msgs[0]
    assert [b["action"]["label"] for b in msgs[0]["contents"]["body"]["contents"]] == [
        "音声で相談する", "履歴書を読み込む", "ゲームで書類を作る"]
    assert msgs[1]["text"] == question
    assert [i["action"]["label"] for i in msgs[1]["quickReply"]["items"]] == ["はい", "ほかの条件で探す", "今はいい"]
    # 挨拶のターンでなければ、前の組み立て(吹き出し 2 つとボタンの箱の 3 通)
    assert [m["type"] for m in to_messages(r, "")] == ["text", "text", "flex"]
    # 勧めの無い 1 つの吹き出しは、見出しにせず残す。ボタンの箱を先に、問いを最後に(選択肢は問いに付く)
    one = r | {"bubbles": [f"おかえりなさい。{question}"]}
    msgs = to_messages(one, "", greeting="revisit")
    assert [m["type"] for m in msgs] == ["flex", "text"] and _heading(msgs[0]) is None and "quickReply" in msgs[1]
    # 使えない操作を付けてしまったときの断りの文は、問いの前(問いが最後のまま)
    _liff_on(monkeypatch, voice=False)
    msgs = to_messages(r, "", greeting="revisit")
    assert [m["type"] for m in msgs] == ["flex", "text", "text"]
    assert msgs[1]["text"] == line_channel.ACTION_NOTES["voice_consult"] and msgs[2]["text"] == question
    assert "quickReply" in msgs[2]
    # ボタンの使えない LINE(LIFF が無い)では、吹き出しがそのまま
    _no_liff(monkeypatch)
    plain = r | {"actions": [], "buttons": []}
    assert [m["text"] for m in to_messages(plain, "", greeting="revisit")] == [welcome, question]


def test_line_buttons_have_no_emoji_and_use_the_greetings_words():
    """D3: LINE のボタンに絵文字を付けない。ボタンの文言は、挨拶の本文(dip の Prompt の例)と同じ言い方。
    評価の模擬ユーザーにも、LINE ではその文言で見せる(evals.run の ACTION_LABEL)。「文字で話す」の選択肢は Prompt から無くした。"""
    from dip_mcp.server import baitoru_assistant
    from evals.checks import EMOJI
    from evals.run import ACTION_LABEL, _format_agent

    labels = [line_channel.LIFF_ACTIONS["voice_consult"][0], line_channel.LIFF_ACTIONS["resume_upload"][0],
              line_channel.ARCADE_LABEL]
    assert labels == ["音声で相談する", "履歴書を読み込む", "ゲームで書類を作る"]
    assert not any(EMOJI.search(x) for x in labels)
    visit = baitoru_assistant("visit")
    assert "「音声で相談する」「履歴書を読み込む」「ゲームで書類を作る」" in visit  # ボタンの名前の決まり(本文で並べない例にも出る)
    assert "Web のトーク画面は「話して伝える」「ファイルを選ぶ」「ゲームで書類を作る」" in visit  # Web は画面のボタンの名前
    assert f'{{"label": "{line_channel.ARCADE_LABEL}", "url": arcade_url}}' in visit  # ゲームのボタンの文言
    assert "遊びながら履歴書や職務経歴書の下書きを作れます" in visit  # ゲームが何をするものか
    assert "「文字で話す」と「今はいい」" not in visit and "「文字で話す」のような選択肢は付けない" in visit
    assert ACTION_LABEL["line"] == {"resume_upload": "履歴書を読み込む", "voice_consult": "音声で相談する"}
    r = {"bubbles": ["どうぞ"], "cards": [], "buttons": [], "actions": ["voice_consult"], "quick_replies": []}
    assert _format_agent(r, "line") == "どうぞ\n[操作] 音声で相談する"
    assert _format_agent(r) == "どうぞ\n[操作] 話して伝える(音声・約5分)"  # Web はトーク画面の文言のまま
    assert EMOJI.search("🎤 音声で相談する(約5分)") and EMOJI.search("📄 履歴書") and not EMOJI.search("今はいい")


def test_the_new_channel_id_name_wins_and_the_old_one_still_works(monkeypatch):
    _no_liff(monkeypatch)
    monkeypatch.setenv("LINE_LIFF_ID", LIFF_ID)
    monkeypatch.setenv("LINE_LOGIN_CHANNEL_ID", "1111")
    assert line_channel.login_channel_id() == "1111" and line_channel.liff_configured()
    monkeypatch.setenv("LINE_LIFF_CHANNEL_ID", CHANNEL)
    assert line_channel.login_channel_id() == CHANNEL


# ----------------------------- LIFF 画面から始めた結果が LINE のトークに届く -----------------------------


@pytest.fixture
def line_liff(tmp_path, monkeypatch):
    """LINE と LIFF と LiveKit の設定がある Web サーバー。LINE の API と ID トークンの検証は偽物、LLM は台本、履歴書の読み取りはスタブ。
    (server, 偽物の LINE の API, 偽物の LINE ログイン, 台本のモデル)"""
    _no_liff(monkeypatch)
    model = Advisor()
    model.seen, model.contexts = [], []
    server, fake = _server(tmp_path, monkeypatch, model, AICA_ID_KEY=ID_KEY, LINE_LIFF_ID=LIFF_ID,
                           LINE_LIFF_CHANNEL_ID=CHANNEL, AICA_INTERNAL_KEY=INTERNAL_KEY, **LIVEKIT_ENV)
    login = FakeLineLogin()
    login.tokens["other"] = login.tokens["good"] | {"sub": RAW_OTHER}
    monkeypatch.setattr(server, "line_login", lambda: LineLogin(CHANNEL, transport=login.transport))
    stub = StubClient(clean_draft(), usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    monkeypatch.setattr(server, "resume_client", lambda: stub)
    return server, fake, login, model


def _pushes(fake) -> list[dict]:
    return [b for p, b in fake.sent() if p == PUSH]


def _liff_client(server) -> TestClient:
    """LINE のアプリの中のブラウザ(https。LIFF の Cookie は Secure)。"""
    return TestClient(server.app, base_url="https://testserver")


def test_a_voice_consultation_started_in_liff_reaches_the_users_line_talk(line_liff, tmp_path):
    """LINE のトーク → LIFF 画面で音声相談を始める → 音声ワーカーが結果を送る → 同じ本人の LINE のトークに、まとめ・求人・
    下書きのボタン(LIFF URL)が push で届く。LINE の userId はどこにも保存しない。"""
    import jwt

    server, fake, _, _ = line_liff
    uid = line_user(RAW_USER)
    with _liff_client(server) as client:
        _post(client, [_event("message", text="まず相談する")])  # トークで勧められた
        (talk,) = fake.sent()
        uris = [b["action"]["uri"] for m in talk[1]["messages"] if m["type"] == "flex" for b in m["contents"]["body"]["contents"]]
        assert f"https://liff.line.me/{LIFF_ID}/voice" in uris

        assert client.get("/liff/line/voice").text.count("LINE で本人を確かめています") == 1  # まだ本人でない: 殻だけ
        assert client.post("/api/liff/voice/start", json={"user_id": uid, "id_token": "good"}).status_code == 401
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
        page = client.get("/liff/line/voice")
        assert "/api/liff/voice/start" in page.text and "/static/talk-core.js" in page.text and RAW_USER not in page.text

        # 別の人の ID トークン・別のユーザーの本文では始めない(会話数も使わない)
        used = server.app.state.quota.used(uid)
        assert client.post("/api/liff/voice/start", json={"user_id": uid, "id_token": "other"}).status_code == 401
        assert client.post("/api/liff/voice/start", json={"user_id": line_user(RAW_OTHER), "id_token": "other"}).status_code == 403
        assert server.app.state.quota.used(uid) == used

        r = client.post("/api/liff/voice/start", json={"user_id": uid, "id_token": "good"})
        assert r.status_code == 200, r.text
        token = r.json()
        claims = jwt.decode(token["token"], options={"verify_signature": False})
        assert claims["sub"] == uid and RAW_USER not in token["token"]  # 音声ワーカーには aica のユーザーIDだけが渡る
        assert server.app.state.quota.used(uid) == used + 1

        ids = sorted(load_jobs())[:2]
        out = TestClient(server.app).post("/api/voice/result", headers=WORKER, json={
            "user_id": uid, "room": token["room"], "jobs": [{"job_id": i, "reasons": ["夕方の勤務"]} for i in ids],
            "draft_markdown": "# 職務経歴書の下書き\n\n- カフェで1年勤務\n", "document_kind": "職務経歴書",
            "preferences": {"job_type": "カフェ", "time_band": "夕方"}}).json()
        assert out["talk"] is True and out["line"] is True

        (push,) = _pushes(fake)
        assert push["to"] == RAW_USER
        texts = [m["text"] for m in push["messages"] if m["type"] == "text"]
        assert texts[0] == "音声でお話しした内容をまとめました。\n職種: カフェ\n時間帯: 夕方"
        flex = [m for m in push["messages"] if m["type"] == "flex"]
        assert flex[0]["contents"]["type"] == "carousel" and len(flex[0]["contents"]["contents"]) == 2
        (draft,) = flex[1]["contents"]["body"]["contents"]
        assert draft["action"] == {"type": "uri", "label": "職務経歴書の下書きを見る",
                                   "uri": f"https://liff.line.me/{LIFF_ID}/voice/draft?rid=0"}
        assert "下書き" in client.get("/liff/line/voice/draft", params={"rid": 0}).text  # LIFF の本人で開ける

        # 画面のシートも同じ結果を受け取る(ボタンは LIFF の本人のまま開く /liff/line/…)
        got = client.post("/api/liff/voice/wait", json={"user_id": uid, "since": 0, "room": token["room"], "timeout": 0}).json()
        assert got["results"][0]["buttons"] == [{"label": "職務経歴書の下書きを見る", "url": "/liff/line/voice/draft?rid=0"}]

        # 同じ部屋の結果がもう一度来ても、2 回は push しない
        TestClient(server.app).post("/api/voice/result", headers=WORKER, json={"user_id": uid, "room": token["room"]})
        assert len(_pushes(fake)) == 1

    assert server.app.state.line_voice_rooms == {}  # 届けたら宛先を忘れる
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert RAW_USER.encode() not in f.read_bytes(), f  # LINE の userId は保存しない


def test_a_resume_read_in_liff_continues_the_users_line_talk(line_liff, tmp_path):
    """LIFF 画面で履歴書を読み込む → 画面に確認 → 同じ本人の LINE のトークに、確認と読み取り結果のボタンとエージェントの
    次の 1 問が 1 回の push で届く。会話数は Web と同じく 2 つ(読み取りと続きのターン)。"""
    server, fake, _, model = line_liff
    uid = line_user(RAW_USER)
    with _liff_client(server) as client:
        _post(client, [_event("message", text="まず相談する")])
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
        page = client.get("/liff/line/resume/upload")
        assert "/api/liff/resume/upload" in page.text and 'type="file"' in page.text
        used = server.app.state.quota.used(uid)

        def upload(token: str = "good", user: str = uid):
            return client.post("/api/liff/resume/upload", data={"user_id": user, "id_token": token},
                               files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/octet-stream")})

        assert upload("other").status_code == 401 and upload(user=line_user(RAW_OTHER)).status_code == 403
        assert logic.resume_upload(server.state, uid) is None and server.app.state.quota.used(uid) == used

        r = upload()
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["talk"] is True and d["empty"] is False and d["bubbles"][0].startswith("履歴書を読み取りました")
        assert d["view_url"].startswith(f"https://liff.line.me/{LIFF_ID}/resume?rid=")
        assert logic.resume_upload(server.state, uid) is not None
        assert server.app.state.quota.used(uid) == used + 2

    (push,) = _pushes(fake)
    assert push["to"] == RAW_USER
    texts = "\n".join(m["text"] for m in push["messages"] if m["type"] == "text")
    assert "履歴書を読み取りました" in texts
    assert "コンビニみほん駅前店での接客のご経験を活かせる仕事で探しますか?" in texts  # エージェントの次の 1 問
    # 次の 1 問は、確認の「読み取り結果を見る」の見出しにしない(2 つの返信をつないだ push。task 231)
    assert _heading(push["messages"][-1]) is None
    links = [b["action"] for m in push["messages"] if m["type"] == "flex" for b in m["contents"]["body"]["contents"]]
    assert links[0]["label"] == "読み取り結果を見る" and links[0]["uri"] == d["view_url"]
    assert [i["action"]["text"] for i in push["messages"][-1]["quickReply"]["items"]] == ["はい", "ほかも見たい", "今はいい"]
    assert FILENAME not in json.dumps(push, ensure_ascii=False)
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert RAW_USER.encode() not in f.read_bytes(), f  # LINE の userId は保存しない
    upload_turn = next(c for c in model.contexts if c[-1].type == "human" and c[-1].content.startswith("[イベント: 本人が"))
    assert "LINE のトーク" in upload_turn[0].content  # 続きのターンも LINE の窓口として話す


def test_voice_results_are_not_pushed_without_messaging_api_or_after_the_room_expired(line_liff, monkeypatch):
    """LIFF はあるが Messaging API の設定が無い: 結果は会話に入るが push しない。始めてから 1 時間を過ぎた部屋の結果も push しない
    (宛先はもう忘れている)。前の部屋が終わるまでは、次の部屋を開けない(409)。"""
    server, fake, _, _ = line_liff
    uid = line_user(RAW_USER)
    with _liff_client(server) as client:
        _post(client, [_event("message", text="まず相談する")])
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200

        def start() -> str:
            r = client.post("/api/liff/voice/start", json={"user_id": uid, "id_token": "good"})
            assert r.status_code == 200, r.text
            return r.json()["room"]

        def result(room: str) -> dict:
            return TestClient(server.app).post("/api/voice/result", headers=WORKER,
                                               json={"user_id": uid, "room": room, "draft_markdown": ""}).json()

        room = start()
        with monkeypatch.context() as m:
            m.delenv("LINE_CHANNEL_ACCESS_TOKEN")
            out = result(room)
        assert out["line"] is False and out["talk"] is True and _pushes(fake) == []
        # 前の部屋が終わるまでは、LIFF からも次の部屋を開けない(Web の /api/voice/start と同じ。task 202)
        r = client.post("/api/liff/voice/start", json={"user_id": uid, "id_token": "good"})
        assert r.status_code == 409 and r.json()["detail"] == server.VOICE_BUSY_MESSAGE
        assert TestClient(server.app).post("/api/voice/session", headers=WORKER,
                                           json={"user_id": uid, "room": room, "state": "ended"}).json() == {"allowed": True}

        room = start()
        later = time.monotonic() + server.LINE_VOICE_ROOM_SECONDS + 1
        monkeypatch.setattr(server.time, "monotonic", lambda: later)
        assert result(room)["line"] is False and _pushes(fake) == []
        assert server.app.state.line_voice_rooms == {}


def test_the_arcade_opens_in_liff_only_as_the_verified_line_user(line_liff, monkeypatch):
    server, _, _, _ = line_liff
    with _liff_client(server) as client:
        assert "LINE で本人を確かめています" in client.get("/liff/line/arcade").text
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
        hub = client.get("/liff/line/arcade")
        assert hub.status_code == 200
        assert [g for g in ARCADE_GAMES if f'href="/liff/line/arcade/{g}"' in hub.text] == list(ARCADE_GAMES)
        assert client.get("/liff/line/arcade/harvest-of-work").status_code == 200
        assert client.get("/liff/line/arcade/nope").status_code == 404
        monkeypatch.delenv("LINE_LIFF_ID")
        assert client.get("/liff/line/arcade").status_code == 404
        assert client.get("/liff/arcade").status_code == 200  # Web のハブは今までどおり


def test_the_three_liff_first_screens_share_one_frame_without_developer_words(line_liff):
    """リッチメニューから開く 3 つの最初の画面(音声相談、履歴書の読み込み、ゲームの一覧)と、その前の本人を確かめる殻は、
    同じ枠(aica の帯、規約とポリシーへのリンク)で出し、開発用の言葉(「LIFF・dip の画面」)を出さない。
    上から、何ができるか(1 行)、箇条書き、押すボタン 1 つの順。今までの説明文の事実は落とさない(task 241)。"""
    server, _, _, _ = line_liff
    facts = {
        "/liff/line/voice": ["5分ほど", "話した内容のまとめと求人が LINE のトークに届きます", "氏名・住所・電話番号は聞きません",
                             "イヤホンの利用がおすすめです"],
        "/liff/line/resume/upload": ["PDF / JPEG / PNG、10MB まで", "読み取った内容で、LINE のトークの相談を続けます",
                                     "氏名・住所・電話番号・メールアドレスは保存しません"],
        "/liff/line/arcade": ["遊べるのは2本", "サンプル", "名前や連絡先は聞きません"],
    }
    with _liff_client(server) as client:
        for path in facts:
            shell = client.get(path).text
            assert "LINE で本人を確かめています" in shell and 'class="lf-head"' in shell and "LIFF・dip" not in shell
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
        for path, words in facts.items():
            page = client.get(path).text
            assert "LIFF・dip" not in page and "(LINE)" not in page, path
            assert 'class="lf-head"' in page and legal.DEMO_NOTE in page and legal.links_html() in page, path
            intro = re.search(r'<section class="lf-card lf-intro">.*?</section>', page, re.DOTALL).group(0)
            assert len(re.findall(r'<p class="lf-lead">', intro)) == 1 and 2 <= intro.count("<li>") <= 4, path
            assert all(w in intro for w in words), path
            after = page[page.index(intro) + len(intro):]
            shown = re.findall(r'<(?:button|a) class="lf-(?:btn|game)"[^>]*>', after)
            visible = [b for b in shown if " hidden" not in b]
            assert visible and all('id="liff-back"' not in b for b in visible), path
            if path != "/liff/line/arcade":  # ゲームの一覧は、ボタンの代わりに遊ぶゲームを選ぶ
                assert len(visible) == 1, (path, visible)
    assert set(server.ARCADE_LIFF_CARDS) == set(ARCADE_GAMES)  # ハブに出すゲームを足したら、一覧の 1 行も足す


# ----------------------------- 画面をブラウザで動かす(Playwright。LIFF SDK と LiveKit は偽物) -----------------------------


def _fake_sdk(token: str) -> str:
    """LIFF SDK の偽物。init は LINE と同じく、URL に liff.state があればエンドポイント URL(/liff/line)の後ろに付けた画面へ送る。"""
    return f"""window.__liffClosed = false;
window.liff = {{
  init: async () => {{
    const st = new URLSearchParams(location.search).get("liff.state");
    if (st) {{ location.replace("{line_channel.LIFF_PREFIX}" + st); await new Promise(() => {{}}); }}
  }},
  isLoggedIn: () => true,
  login: () => {{}},
  getIDToken: () => {json.dumps(token)},
  closeWindow: () => {{ window.__liffClosed = true; }},
}};"""


@pytest.fixture
def served(line_liff):
    import uvicorn

    server = line_liff[0]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(300):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    yield (*line_liff, f"http://localhost:{port}")
    srv.should_exit = True
    th.join(10)


@pytest.fixture
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001  Chromium が入っていない環境
            pytest.skip(f"Chromium を起動できない: {e}")
        yield b
        b.close()


def _liff_context(browser, server):
    context = browser.new_context()
    context.route(server.LIFF_SDK, lambda route: route.fulfill(status=200, content_type="text/javascript", body=_fake_sdk("good")))
    context.route(re.compile(r"https://cdn\.jsdelivr\.net/npm/livekit-client.*"),
                  lambda route: route.fulfill(status=200, content_type="application/javascript", body=FAKE_LIVEKIT))
    return context


def test_the_liff_voice_page_in_a_browser(served, browser):
    """LINE のボタン(https://liff.line.me/<LIFF ID>/voice)で開いたのと同じ流れ: LINE はまずエンドポイント URL を
    liff.state=/voice つきで開き、liff.init が /liff/line/voice へ送る。殻が本人を確かめ、音声相談のシートで話し、
    まとめが画面と LINE のトークに届く。"""
    import urllib.request

    server, fake, _, _, base = served
    uid = line_user(RAW_USER)
    context = _liff_context(browser, server)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(10_000)
    try:
        page.goto(f"{base}/liff/line?" + urlencode({"liff.state": "/voice"}))
        page.locator("#voice-open:not([disabled])").wait_for()
        assert page.url == f"{base}/liff/line/voice"
        page.locator("#voice-open").click()
        page.locator("#voice-sheet").wait_for(state="visible")
        page.wait_for_function("window.__lk && window.__lk.mic === true && !document.getElementById('voice-end').disabled")
        room = next(iter(server.app.state.line_voice_rooms))

        req = urllib.request.Request(base + "/api/voice/result", json.dumps({
            "user_id": uid, "room": room, "draft_markdown": "# 履歴書の下書き\n\n- カフェで1年勤務\n",
            "preferences": {"job_type": "カフェ"}}).encode(), {"Content-Type": "application/json"} | WORKER)
        with urllib.request.urlopen(req, timeout=10) as res:
            assert json.loads(res.read())["line"] is True

        page.locator("#voice-sheet").wait_for(state="hidden")
        page.get_by_text("音声でお話しした内容をまとめました。").wait_for()
        page.get_by_text(server.LIFF_SENT_TEXT).wait_for()
        assert page.locator("#log .liff a").get_attribute("href") == "/liff/line/voice/draft?rid=0"
        page.locator("#liff-back").click()
        assert page.evaluate("window.__liffClosed") is True
        assert errors == []
    finally:
        context.close()
    for _ in range(100):  # push は応答の後に送る
        if _pushes(fake):
            break
        time.sleep(0.05)
    (push,) = _pushes(fake)
    assert push["to"] == RAW_USER and push["messages"][0]["text"].startswith("音声でお話しした内容をまとめました。")


def test_a_voice_start_that_fails_does_not_promise_a_result(served, browser):
    """ID トークンを LINE が断る(期限切れなど)と、音声相談は始まらない。シートを閉じても「まとめが届きます」とは言わず、もう一度押せる。"""
    server, fake, login, _, base = served
    context = _liff_context(browser, server)
    page = context.new_page()
    page.set_default_timeout(10_000)
    try:
        page.goto(f"{base}/liff/line?" + urlencode({"liff.state": "/voice"}))
        page.locator("#voice-open:not([disabled])").wait_for()
        login.tokens.pop("good")  # 画面を開いた後で、ID トークンが通らなくなった
        page.locator("#voice-open").click()
        page.locator("#voice-sheet[data-state='error']").wait_for()
        page.locator("#voice-close").click()
        page.get_by_text(server.LIFF_NOT_STARTED_TEXT).wait_for()
        assert page.locator("#voice-open").is_visible() and page.locator("#liff-back").is_hidden()
        assert page.locator("#liff-status").inner_text() == server.LIFF_NOT_STARTED_TEXT
    finally:
        context.close()
    assert server.app.state.line_voice_rooms == {} and _pushes(fake) == []


def test_the_liff_resume_page_in_a_browser(served, browser):
    """https://liff.line.me/<LIFF ID>/resume/upload で開いたのと同じ流れ(liff.state=/resume/upload)。ファイルを選んで読み込むと、
    画面に確認が出て、続きは LINE のトークに届く。"""
    server, fake, _, _, base = served
    context = _liff_context(browser, server)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(10_000)
    try:
        page.goto(f"{base}/liff/line?" + urlencode({"liff.state": "/resume/upload"}))
        page.locator("#resume-send:not([disabled])").wait_for()
        assert page.url == f"{base}/liff/line/resume/upload"
        page.locator("#resume-file").set_input_files({"name": FILENAME, "mimeType": "application/pdf", "buffer": b"%PDF-1.4 dummy"})
        assert page.locator("#resume-name").inner_text() == FILENAME  # 選んだファイルの名前を、大きな枠に出す
        assert "chosen" in page.locator("#resume-pick").get_attribute("class")
        page.locator("#resume-send").click()
        page.get_by_text("続きを LINE のトークに送ります").wait_for()
        assert "履歴書を読み取りました" in page.locator("#resume-result").inner_text()
        assert FILENAME not in page.content()  # 読み込んだら、ファイルの名前を画面に残さない(サーバーも返さない)
        assert errors == []
    finally:
        context.close()
    for _ in range(100):
        if _pushes(fake):
            break
        time.sleep(0.05)
    (push,) = _pushes(fake)
    assert "コンビニみほん駅前店" in "\n".join(m.get("text", "") for m in push["messages"])
