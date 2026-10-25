"""LINE でレジュメ・アーケードを使う(task 219)。実 LINE・実 LLM は呼ばない。

LINE の Messaging API と ID トークンの検証は偽物の LINE(httpx.MockTransport)、会話のモデルは台本の偽物、ゲームの AI は台本のスタブ
(tests/test_arcade_ai.StubLLM)。dip の MCP は本物(子プロセス。環境変数も同じものを読む)。
- LIFF の設定があれば、dip の要約は LINE のユーザーにもゲームの URL(arcade_url)を渡し、LINE のトークでは LIFF URL のボタンになる。
  無ければ今までどおり渡さない
- LIFF の中のゲームは、AI と保存を LIFF の本人(LINE のユーザー)で呼ぶ。回数の上限は Web と同じ。下書きはその本人の要約と
  プロフィール(「データを見る」)に出て、ほかの人の下書きにはならない
- 保存したら、今の会話に「下書きができました」を足し、その本人の LINE のトークに 1 回だけ push する
- 画面を押すテスト(Playwright。入っていなければ飛ばす): LIFF の中のゲームの部品(arcade-ai.js)が LIFF の API を呼び、ID トークンを送る
"""

from __future__ import annotations

import json
import re

import pytest
from langchain_core.outputs import ChatGeneration, ChatResult

from dip_mcp import arcade, logic, profile
from dip_mcp.data import State
from line_runtime import line_channel
from line_runtime.line_channel import line_user
from line_runtime.runtime import _tool_payload
from tests.test_arcade_ai import MD, StubLLM
from tests.test_handle import ScriptedModel, call, reply
from tests.test_liff_actions import (  # noqa: F401  browser と served と line_liff はフィクスチャ
    CHANNEL,
    ID_KEY,
    LIFF_ID,
    _liff_client,
    _liff_context,
    _no_liff,
    _pushes,
    browser,
    line_liff,
    served,
)
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

GAME = "the-case-of-you"
ASK = "職務経歴書を作りたい"
GUIDE = "遊びながら書類を作れます"


# ----------------------------- dip の要約のゲームの URL -----------------------------


def test_line_users_get_the_game_url_only_when_liff_is_configured(tmp_path, monkeypatch):
    """LIFF の設定(LIFF ID とチャネル ID)があるときだけ、LINE のユーザーの要約に arcade_url が載る。Web のユーザーは今までどおり。
    dip の LIFF の判定は、LINE の口(line_channel.liff_configured)と同じ答えになる。"""
    _no_liff(monkeypatch)
    st = State(tmp_path / "state.json")
    st.reset()
    line_uid, web_uid = line_user(RAW_USER, ID_KEY.encode()), "w-0123456789abcdef"
    assert "arcade_url" not in logic.conversation_summary(st, line_uid)
    assert logic.conversation_summary(st, web_uid)["arcade_url"] == "/liff/arcade"

    for env in ({"LINE_LIFF_ID": LIFF_ID}, {"LINE_LIFF_CHANNEL_ID": CHANNEL}, {"LINE_LIFF_ID": " ", "LINE_LIFF_CHANNEL_ID": CHANNEL},
                {"LINE_LIFF_ID": LIFF_ID, "LINE_LOGIN_CHANNEL_ID": CHANNEL}, {"LINE_LIFF_ID": LIFF_ID, "LINE_LIFF_CHANNEL_ID": CHANNEL}):
        with monkeypatch.context() as m:
            for k, v in env.items():
                m.setenv(k, v)
            assert arcade.liff_configured() == line_channel.liff_configured(), env

    monkeypatch.setenv("LINE_LIFF_ID", LIFF_ID)
    monkeypatch.setenv("LINE_LIFF_CHANNEL_ID", CHANNEL)
    assert logic.conversation_summary(st, line_uid)["arcade_url"] == "/liff/arcade"
    logic.set_search_track(st, line_uid, "正社員", "テスト")
    assert logic.conversation_summary(st, line_uid)["arcade_url"] == "/liff/arcade/the-case-of-you"
    logic.set_search_track(st, line_uid, "アルバイト・パート", "テスト")
    assert logic.conversation_summary(st, line_uid)["arcade_url"] == "/liff/arcade/harvest-of-work"
    assert logic.conversation_summary(st, web_uid)["arcade_url"] == "/liff/arcade"


# ----------------------------- LINE のトークのボタン -----------------------------


class GameGuide(ScriptedModel):
    """dip の Prompt の「ゲームで書類を作る」どおりに動く偽のモデル(何度でも返す)。本人が書類を作りたいと言ったら要約を読み、
    arcade_url があればそのままボタンにする(無ければ出さない)。ほかの発言には 1 問だけ返す。"""

    systems: list = []  # noqa: RUF012  モデルに渡ったシステムプロンプト

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.respond(messages))])

    def respond(self, messages):
        self.systems.append(messages[0].content)
        uid = re.search(r"LINE ユーザーID: ([^(\s]+)", messages[0].content).group(1)
        last = max(i for i, m in enumerate(messages) if m.type == "human")
        human, turn = messages[last].content, messages[last + 1:]
        if human != ASK:
            return reply(["これまでのお仕事を教えてください。"])
        if not any(m.type == "tool" for m in turn):
            return call("get_user_conversation_summary", {"line_user_id": uid}, "sum")
        summary = next(_tool_payload(m) for m in turn if m.type == "tool")
        if "arcade_url" not in summary:
            return reply(["これまでのお仕事を1つ教えてください。一緒に書類にします。"])
        msg = reply([GUIDE])
        body = json.loads(msg.content) | {"buttons": [{"label": "ゲームで書類を作る", "url": summary["arcade_url"]}]}
        return msg.model_copy(update={"content": json.dumps(body, ensure_ascii=False)})


def _links(body: dict) -> list[dict]:
    return [b["action"] for m in body["messages"] if m["type"] == "flex" and m["contents"]["type"] == "bubble"
            for b in m["contents"]["body"]["contents"]]


@pytest.mark.parametrize("liff", [True, False])
def test_line_talk_shows_the_game_button_as_a_liff_url_when_asked(tmp_path, monkeypatch, liff):
    """LINE で「職務経歴書を作りたい」: LIFF の設定があれば、エージェントは要約の arcade_url をボタンにし、LINE には LIFF URL の
    ボタンが届く(正社員なら「あなたという事件」)。エージェントには LINE でゲームを使えると伝わる。
    LIFF の設定が無ければ、要約に URL が無く、ボタンは出ない(エージェントには LINE では開けないと伝わる)。"""
    _no_liff(monkeypatch)
    model = GameGuide()
    model.systems = []
    env = {"LINE_LIFF_ID": LIFF_ID, "LINE_LIFF_CHANNEL_ID": CHANNEL} if liff else {}
    server, fake = _server(tmp_path, monkeypatch, model, AICA_ID_KEY=ID_KEY, **env)
    uid = line_user(RAW_USER)
    logic.set_search_track(server.state, uid, "正社員", "テスト")
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text=ASK)]).status_code == 200

    ((path, body),) = fake.sent()
    assert path == REPLY
    texts = [m["text"] for m in body["messages"] if m["type"] == "text"]
    if liff:
        assert texts == [GUIDE]
        assert _links(body) == [{"type": "uri", "label": "ゲームで書類を作る", "uri": f"https://liff.line.me/{LIFF_ID}/arcade/{GAME}"}]
        assert "ゲームの AI と下書きの保存も" in model.systems[0]
    else:
        assert GUIDE not in texts and _links(body) == []
        assert "レジュメ・アーケード(/liff/arcade)は、LINE では開けない" in model.systems[0]


# ----------------------------- LIFF の中のゲームの AI と保存 -----------------------------


@pytest.fixture
def liff_game(line_liff, monkeypatch):  # noqa: F811
    """LINE と LIFF の設定がある Web サーバーと、ゲームの AI のスタブ。(server, 偽物の LINE の API, 台本の LLM)"""
    server, fake, _, _ = line_liff
    stub = StubLLM()
    monkeypatch.setattr(server, "arcade_client", lambda: stub)
    return server, fake, stub


def _session(client, token: str = "good") -> None:
    assert client.post("/api/liff/session", json={"id_token": token}).status_code == 200


def _start(client) -> dict:
    r = client.post("/api/liff/arcade/start", json={"game": GAME})
    assert r.status_code == 200, r.text
    return r.json()


def _reaction(client, play: str) -> dict:
    data = {"job": {"id": "j1", "company": "ペーパームーン", "title": "エンジニア", "dates": "2022年 - 現在"},
            "role": "boss", "witness": "ルー", "question": "数字は？", "answer": "決済完了率を61%から68%に上げました。"}
    r = client.post("/api/liff/arcade/ai", json={"game": GAME, "play": play, "stage": "reaction", "data": data})
    assert r.status_code == 200, r.text
    return r.json()


def _save(client, play: str, token: str = "good"):
    return client.post("/api/liff/arcade/save", json={"game": GAME, "play": play, "markdown": MD, "id_token": token})


def test_a_game_played_in_liff_is_saved_for_the_line_user_and_the_talk_gets_one_push(liff_game, tmp_path, monkeypatch):
    """LINE のトーク → LIFF のゲーム: AI はその LINE の本人として動き(ターンと 1 日の回数、費用台帳、反応の回数の上限は Web と同じ)、
    保存した下書きはその本人の要約と「データを見る」に出る。LINE のトークには「下書きができました」と LIFF URL の「下書きを見る」が
    1 回の push で届く。LINE の userId はどこにも保存しない。"""
    server, fake, stub = liff_game
    monkeypatch.setenv(arcade.REACTIONS_ENV, "1")
    uid = line_user(RAW_USER)
    with _liff_client(server) as client:
        quota = server.app.state.quota  # 起動のときに作る
        _post(client, [_event("message", text="まず相談する")])  # LINE のトークの会話がある
        logic.set_search_track(server.state, uid, "正社員", "テスト")
        assert client.post("/api/liff/arcade/start", json={"game": GAME}).status_code == 401  # まだ本人でない
        _session(client)

        page = client.get(f"/liff/line/arcade/{GAME}")
        assert page.status_code == 200 and f'window.AICA_LIFF = {{"liffId": "{LIFF_ID}"}}' in page.text
        assert server.LIFF_SDK in page.text and "/static/arcade/arcade-ai.js" in page.text
        assert "AICA_LIFF" not in client.get(f"/liff/arcade/{GAME}").text  # Web のゲームは今までどおり

        used = quota.used(uid)
        p = _start(client)
        assert p["ai"] is True and p["reason"] is None and p["document_kind"] == "職務経歴書" and p["track"] == "正社員"
        assert (quota.used(uid), quota.daily_used(server.ARCADE_DAILY_KIND, uid)) == (used + 1, 1)
        assert _reaction(client, p["play"])["ok"] is True
        assert _reaction(client, p["play"]) == {"ok": False, "offline": True, "reason": "reaction_limit"}  # Web と同じ上限
        assert len(stub.calls) == 1
        assert [(r["user_id"], r["event"]) for r in server.app.state.rt.ledger.rows][-1] == (uid, f"arcade:{GAME}:reaction")

        r = _save(client, p["play"])
        assert r.status_code == 200, r.text
        out = r.json()
        assert out == {"draft": 0, "view_url": "/liff/line/arcade/draft?rid=0", "document_kind": "職務経歴書", "blanks": 1,
                       "talk": True, "line": True}

        draft = client.get(out["view_url"])  # LIFF の本人のまま開け、直した書類も LIFF の本人の画面へ送る
        assert draft.status_code == 200 and "共通コンポーネントのライブラリを整備" in draft.text
        assert "action='/liff/line/arcade/draft'" in draft.text
        edited = client.post("/liff/line/arcade/draft", data={"rid": 0, "markdown": MD.replace("[+X% ← 数字を入れる]", "+20%")})
        assert edited.status_code == 200 and "保存しました" in edited.text
        assert arcade.draft(server.state, uid, 0)["markdown"].count("+20%") == 1

        _post(client, [_event("message", text=line_channel.VIEW_DATA_TEXT, event_id="01EVENT0000000000000000009",
                              token="reply-token-9")])

    (push,) = _pushes(fake)  # 保存 1 回につき 1 通
    assert push["to"] == RAW_USER
    texts = [m["text"] for m in push["messages"] if m["type"] == "text"]
    assert texts[0] == "ゲームで職務経歴書の下書きができました。内容はご自身で確認・修正してください。"
    assert _links(push) == [{"type": "uri", "label": "下書きを見る", "uri": f"https://liff.line.me/{LIFF_ID}/arcade/draft?rid=0"}]
    assert [i["action"]["text"] for i in push["messages"][-1]["quickReply"]["items"]] == ["求人を探す", "空欄を埋める"]

    summary = logic.conversation_summary(server.state, uid)
    assert summary["arcade_draft"]["document_kind"] == "職務経歴書" and summary["arcade_url"] == f"/liff/arcade/{GAME}"
    assert [d["document_kind"] for d in profile.view(server.state, uid)["arcade_drafts"]] == ["職務経歴書"]
    view = [b for p_, b in fake.sent() if p_ == REPLY][-1]
    assert "■ ゲームで作った下書き" in "\n".join(m["text"] for m in view["messages"] if m["type"] == "text")
    assert logic.search_track(server.state.user(uid)) == "正社員"
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert RAW_USER.encode() not in f.read_bytes(), f  # LINE の userId は保存しない


def test_other_people_cannot_use_or_see_a_line_users_game(liff_game):
    """ほかの LINE の本人、Web のブラウザ、本人でないブラウザは、その LINE の本人の遊びで AI も保存もできず、下書きも開けない。
    下書きはほかの人のものにならない。"""
    server, _, stub = liff_game
    uid, other_uid = line_user(RAW_USER), line_user(RAW_OTHER)
    logic.set_search_track(server.state, uid, "正社員", "テスト")
    with _liff_client(server) as me:  # ほかのクライアントは同じサーバーに後から入る(起動し直さない)
        other, web, nobody = _liff_client(server), TestClient(server.app), _liff_client(server)
        _session(me)
        p = _start(me)
        assert _save(me, p["play"]).status_code == 200

        _session(other, "other")
        assert _reaction(other, p["play"])["reason"] == "no_play"
        assert _save(other, p["play"]).status_code == 403
        assert other.get("/liff/line/arcade/draft", params={"rid": 0}).status_code == 404

        web.get("/")  # Web の匿名ユーザーは、Web の API でも LINE の本人の遊びを使えない
        assert web.post("/api/arcade/save", json={"game": GAME, "play": p["play"], "markdown": MD}).status_code == 403
        assert web.post("/api/liff/arcade/save", json={"game": GAME, "play": p["play"], "markdown": MD}).status_code == 401

        assert nobody.post("/api/liff/arcade/ai", json={"game": GAME, "play": p["play"], "stage": "reaction"}).status_code == 401
        assert _save(nobody, p["play"]).status_code == 401
        assert "LINE で本人を確かめています" in nobody.get("/liff/line/arcade/draft", params={"rid": 0}).text
        assert nobody.post("/liff/line/arcade/draft", data={"rid": 0, "markdown": "x"}).status_code == 401

    assert arcade.drafts(server.state, other_uid) == [] and len(arcade.drafts(server.state, uid)) == 1
    assert stub.calls == []


def test_the_save_still_works_without_a_push_when_the_line_target_is_unknown(liff_game, monkeypatch):
    """ID トークンが無い・確かめられない・別の人のものなら、保存はして push しない(本人の書類を失わない。別の人には送らない)。
    Messaging API の設定が無いときも push しない。"""
    server, fake, _ = liff_game
    with _liff_client(server) as client:
        _session(client)
        logic.set_search_track(server.state, line_user(RAW_USER), "正社員", "テスト")
        p = _start(client)
        for token in ("", "bad", "other"):
            r = _save(client, p["play"], token)
            assert r.status_code == 200 and r.json()["line"] is False, token
        monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN")
        assert _save(client, p["play"]).json()["line"] is False
    assert _pushes(fake) == []


def test_pushes_for_saves_stop_at_the_daily_limit(line_liff, monkeypatch):  # noqa: F811
    """保存は何度でもできるが、LINE のトークへの知らせは 1 人 1 日 ARCADE_DAILY_PUSHES 通まで(月の枠を守る)。
    超えた保存も下書きにはなる。Messaging API の設定が無ければ、ID トークンを LINE に確かめに行かない。"""
    server, fake, login, _ = line_liff
    uid = line_user(RAW_USER)
    with _liff_client(server) as client:
        _session(client)
        logic.set_search_track(server.state, uid, "正社員", "テスト")
        lines = [_save(client, _start(client)["play"]).json()["line"] for _ in range(server.ARCADE_DAILY_PUSHES + 1)]
        assert lines == [True] * server.ARCADE_DAILY_PUSHES + [False]
        assert len(arcade.drafts(server.state, uid)) == 1 and arcade.draft(server.state, uid, server.ARCADE_DAILY_PUSHES)
        checked = len(login.calls)
        monkeypatch.delenv("LINE_CHANNEL_ACCESS_TOKEN")
        assert _save(client, _start(client)["play"]).json()["line"] is False
        assert len(login.calls) == checked
    assert len(_pushes(fake)) == server.ARCADE_DAILY_PUSHES


def test_without_liff_the_line_game_api_is_closed(liff_game, monkeypatch):
    server, _, _ = liff_game
    with _liff_client(server) as client:
        _session(client)
        monkeypatch.delenv("LINE_LIFF_ID")
        assert client.post("/api/liff/arcade/start", json={"game": GAME}).status_code == 404
        assert client.get("/liff/line/arcade/draft", params={"rid": 0}).status_code == 404


# ----------------------------- 画面をブラウザで動かす(Playwright。LIFF SDK は偽物) -----------------------------


def test_the_game_parts_in_liff_call_the_line_users_api_and_renew_the_session(served, browser, monkeypatch):  # noqa: F811
    """LINE のボタン(…/arcade/the-case-of-you)で開いたのと同じ流れ: 殻が本人を確かめ、ゲームの部品(arcade-ai.js)は
    /api/liff/arcade/… を呼ぶ。保存では ID トークンを送り、LINE のトークに知らせが届く。本人の Cookie の期限が切れていても、
    ID トークンで確かめ直して続ける。"""
    from urllib.parse import urlencode

    server, fake, _, _, base = served
    stub = StubLLM()
    monkeypatch.setattr(server, "arcade_client", lambda: stub)
    uid = line_user(RAW_USER)
    logic.set_search_track(server.state, uid, "正社員", "テスト")
    context = _liff_context(browser, server)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    calls: list[str] = []
    page.on("request", lambda r: calls.append(r.url.removeprefix(base)) if "/api/" in r.url else None)
    page.set_default_timeout(10_000)
    try:
        page.goto(f"{base}/liff/line?" + urlencode({"liff.state": f"/arcade/{GAME}"}))
        page.wait_for_url(f"{base}/liff/line/arcade/{GAME}")
        page.wait_for_function("() => !!window.ArcadeAI && !!window.AICA_LIFF")
        play = page.evaluate("ArcadeAI('the-case-of-you').start()")
        assert play["ai"] is True and play["id"] and play["documentKind"] == "職務経歴書"

        context.clear_cookies()  # 本人の Cookie の期限が切れた
        out = page.evaluate("([p, md]) => ArcadeAI('the-case-of-you').save(p, md, null)", [play, MD])
        assert out["line"] is True and out["view_url"] == "/liff/line/arcade/draft?rid=0"
    finally:
        context.close()
    assert errors == []
    assert calls == ["/api/liff/session", "/api/liff/arcade/start", "/api/liff/arcade/save", "/api/liff/session",
                     "/api/liff/arcade/save"]
    assert [d["id"] for d in arcade.drafts(server.state, uid)] == [0]
    assert [b["to"] for p_, b in fake.sent() if p_ == PUSH] == [RAW_USER]

