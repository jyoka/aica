"""本番のトーク画面(/)と開発者画面(/dev)の台本テスト(task 90。実 LLM なし、dip の MCP は本物)。

本番の / はシナリオを選ばせない: 開いたブラウザの匿名ID そのものをユーザーにして、dip がそのユーザーの状態から
初回訪問か再訪かを決め(/api/talk/open)、画面はそのイベントで /api/event/stream を呼んで AI 側の1通目を受け取る。
ここでは画面と同じ呼び方で、挨拶・ユーザーの分離・会話数の上限・保存した求人を確かめる。

再読み込み(task 98): 最後のやりとりから30分以内に開き直したら今の会話を続け、/api/talk/history で履歴を
描き直す(LLM を呼ばない・会話数を使わない)。時刻は server.clock を差し替えて進める(app.later)。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from dip_mcp import behavior, logic
from dip_mcp.data import State
from tests.test_handle import call, reply
from tests.test_revisit import OFFER_TEXT, Greeter, greet

# 台本の再訪の挨拶(新しい匿名ユーザー: 経歴が分からないので、履歴書の読み込みを勧めの1文で勧める)
REVISIT_GREETING = f"おかえりなさい。{OFFER_TEXT['resume_upload']}"

INTERNAL_KEY = "offline-test-internal-key-offline-test"
WORKER = {"Authorization": f"Bearer {INTERNAL_KEY}"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    """server を一時ディレクトリの状態で動かす。set_model で偽のモデルを差し込む。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # モデルは作るが呼ばない(偽のモデルを差し込まないとき)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    from line_runtime import config, runtime, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)  # 会話数の数(quota.json)・会話を本物の data/ に書かない
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    monkeypatch.setenv(server.INTERNAL_KEY_ENV, INTERNAL_KEY)  # 音声ワーカー(サーバー間の経路)の共有鍵
    server.state.reset()

    def set_model(model):
        monkeypatch.setattr(server, "LineRuntime", lambda: runtime.LineRuntime(model=model, in_memory=True))

    def later(minutes: int):
        """時刻を進める(最後のやりとり = チェックポイントの保存時刻 は実時刻なので、今を先へずらす)。"""
        offset = timedelta(minutes=minutes)
        monkeypatch.setattr(server, "clock", lambda: datetime.now(UTC) + offset)

    server.set_model = set_model
    server.later = later
    yield server
    del server.set_model, server.later


def open_talk(client) -> tuple[dict, dict]:
    """画面を開いたときと同じ: GET /(初めてならここで匿名IDの Cookie が発行される)→ /api/talk/open →
    続きなら /api/talk/history、新しい会話ならそのイベントで /api/event/stream(画面が表示する会話の番号 thread つき)。
    (open の結果, 履歴 または 最後の返信)"""
    client.get("/")
    opened = client.post("/api/talk/open").json()
    if opened["event"] is None:
        r = client.get("/api/talk/history")
        assert r.status_code == 200
        return opened, r.json()
    evs = stream(client, opened["user"], opened["event"], opened["thread"])
    assert evs[0]["kind"] == "start" and evs[0]["event"] == opened["event"]
    return opened, evs[-1] | {"thread": thread_of(evs)}


def without_consent(opened: dict) -> dict:
    """/api/talk/open の結果から、同意の状態と文言(consent。tests/test_consent.py で確かめる)を除いたもの。"""
    return {k: v for k, v in opened.items() if k != "consent"}


def thread_of(evs: list[dict]) -> int | None:
    """ストリームが LLM を呼ぶ前に出す、このターンの会話番号(本番のトーク画面が thread を送ったときだけ出る。
    会話数の上限で断ったときは会話に入らないので出ない)。"""
    return next((e["thread"] for e in evs if e["kind"] == "thread"), None)


def stream(client, uid: str, event: str, thread: int | None, text: str | None = None) -> list[dict]:
    r = client.post("/api/event/stream", json={"user_id": uid, "event": event, "text": text, "thread": thread})
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines()]


# ----------------------------- 開くと、初回訪問 / 再訪の挨拶から始まる -----------------------------


def test_first_open_greets_as_a_first_visit_and_reopening_greets_as_a_revisit(app):
    from fastapi.testclient import TestClient

    model = Greeter(script=[greet] * 4)  # 初回訪問 1回 + 再訪 2回(要約を読む → 挨拶)+ 別ブラウザの初回訪問 1回
    app.set_model(model)
    with TestClient(app.app) as client:
        # Cookie なし: 画面を開いた最初の呼び出しで匿名IDが発行され、初回訪問の挨拶から始まる
        assert app.ANON_COOKIE not in client.cookies
        opened, first = open_talk(client)
        uid = client.cookies[app.ANON_COOKIE]
        assert without_consent(opened) == {"user": uid, "event": "visit", "thread": 0, "search_track": "未定"}
        assert first["entry"] == "visit" and first["tools"] == [] and first["thread"] == 1
        assert first["bubbles"] == ["こんにちは。バイト探しをお手伝いします。何をしますか?"]

        # Cookie あり(同じブラウザで30分を過ぎて開き直す): 同じユーザーの再訪。要約を読んでから挨拶する
        app.later(31)
        opened, again = open_talk(client)
        assert without_consent(opened) == {"user": uid, "event": "revisit", "thread": 1, "search_track": "未定"}
        assert again["entry"] == "revisit" and [t["name"] for t in again["tools"]] == ["get_user_conversation_summary"]
        assert again["bubbles"] == [REVISIT_GREETING, "何をしますか?"] and again["thread"] == 2

        # 別のブラウザ(Cookie なし)は別の匿名ユーザーの初回訪問
        client.cookies.clear()  # 別ブラウザ相当(ターンを進めるので lifespan と同じクライアントを使う)
        opened, first_b = open_talk(client)
        assert opened["event"] == "visit" and opened["user"] != uid
        assert first_b["entry"] == "visit"
    assert model.script == []


def test_production_user_is_the_anonymous_id_itself_and_starts_empty(app):
    """本番のユーザーIDは匿名ID そのもの(fixtures 由来のシナリオ用ユーザーではない)。状態は空から始まる。"""
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        client.get("/")
        uid = client.cookies[app.ANON_COOKIE]
        opened = client.post("/api/talk/open").json()
    assert opened["user"] == uid and re.fullmatch(r"w-[0-9a-f]{16}", uid)  # 「匿名ID:sim-…」ではない
    u = app.state.user(uid)
    assert behavior.dip_id(app.state, uid) is None and u["outcomes"] == [] and not u.get(behavior.WRITES)
    assert logic.saved_jobs(app.state, uid) == [] and behavior.view(app.state, uid)["applications"] == []
    assert not u.get("talk_opens")  # 開いた記録は挨拶が返ったときに残す(ここでは挨拶を呼んでいない)


def test_production_user_cannot_read_or_talk_as_another_user(app):
    """他人のユーザーIDの状態は読めない・その人として話せない(既存の 403 の決まり)。"""
    from fastapi.testclient import TestClient

    with TestClient(app.app) as a:
        b = TestClient(app.app)
        a.get("/"), b.get("/")
        ua = a.post("/api/talk/open").json()["user"]
        ub = b.post("/api/talk/open").json()["user"]
        assert ua != ub
        assert a.get("/api/saved").status_code == 200  # 保存した求人は Cookie の本人の分だけ(ユーザーIDを受け取らない)
        assert a.post("/api/dip", json={"user_id": ub}).status_code == 403
        assert a.post("/api/event/stream", json={"user_id": ub, "event": "revisit"}).status_code == 403
        assert a.post("/api/event", json={"user_id": ub, "event": "message", "text": "hi"}).status_code == 403
        assert a.post("/api/voice/start", json={"user_id": ub}).status_code == 403
        assert a.post("/api/voice/wait", json={"user_id": ub, "since": 0, "timeout": 0}).status_code == 403
        # open は自分の Cookie のユーザーしか開けない(ユーザーIDを受け取らない)
        assert a.post("/api/talk/open", json={"user": ub}).json()["user"] == ua


def test_daily_limit_applies_to_the_production_talk(app, monkeypatch):
    """会話数の上限は本番の / にも効く: 上限を超えた挨拶は LLM を呼ばずに案内だけ返す。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "1")
    model = Greeter(script=[greet])  # 1回目の挨拶だけ
    app.set_model(model)
    with TestClient(app.app) as client:
        _, first = open_talk(client)
        app.later(31)  # 30分以内なら今の会話を続けるだけ(挨拶しない)なので、過ぎてから開き直す
        _, limited = open_talk(client)
        uid = client.cookies[app.ANON_COOKIE]
        assert client.app.state.quota.used(uid) == 1
    assert first["entry"] == "visit" and "limited" not in first
    assert limited["limited"] is True and limited["entry"] == "revisit"
    assert model.script == []


# ----------------------------- 再読み込み: 30分以内は今の会話を続ける(task 98) -----------------------------


def search_jobs(holder: dict):
    def step(messages):
        return call("recommend_jobs", {"line_user_id": holder["uid"], "area": "渋谷", "time_band": "日中"}, "rec")
    return step


def test_reloading_mid_conversation_redraws_the_same_conversation_without_the_llm(app):
    """会話の途中で再読み込みすると、同じ会話の履歴(吹き出し・求人カード・選択肢)が描き直される。
    LLM を呼ばず(台本が尽きていても通る)、会話数も使わない。続けて話すと同じ会話に入る。"""
    from fastapi.testclient import TestClient

    from tests.test_revisit import propose

    holder: dict = {}

    def remembers(messages):  # 続けた発言は同じ会話に入る: 前のターンの発言がモデルに見えている
        assert any(m.type == "human" and m.content == "渋谷で日中" for m in messages)
        return reply(["1件目は駅から近いです。"])

    model = Greeter(script=[greet, search_jobs(holder), propose, remembers])
    app.set_model(model)
    with TestClient(app.app) as client:
        opened, _ = open_talk(client)
        holder["uid"] = uid = opened["user"]
        live = stream(client, uid, "message", 1, "渋谷で日中")[-1]
        used = client.app.state.quota.used(uid)
        assert model.script == [remembers]

        app.later(29)  # 最後のやりとりから30分以内に開き直す(再読み込み)
        reopened, hist = open_talk(client)
        assert without_consent(reopened) == {"user": uid, "event": None, "thread": 1, "search_track": "未定"}
        assert client.app.state.quota.used(uid) == used and model.script == [remembers]  # LLM も会話数も使わない
        assert hist["thread"] == 1
        greeting, search = hist["turns"]
        assert greeting["user"] is None  # 挨拶のイベント(こちらから話しかけた)はユーザーの吹き出しにしない
        assert greeting["bot"]["bubbles"] == ["こんにちは。バイト探しをお手伝いします。何をしますか?"]
        assert greeting["bot"]["cards"] == []
        assert search["user"] == "渋谷で日中"
        for k in ("bubbles", "quick_replies", "cards", "buttons"):
            assert search["bot"][k] == live[k], k
        assert len(search["bot"]["cards"]) == 3 and search["bot"]["quick_replies"]

        # 続けて話すと同じ会話(再訪の挨拶で新しい会話を始めていない)
        evs = stream(client, uid, "message", reopened["thread"], "1件目は?")
        assert evs[-1]["bubbles"] == ["1件目は駅から近いです。"] and thread_of(evs) == 1
    assert model.script == []


def test_opening_after_30_minutes_starts_a_new_conversation_with_the_revisit_greeting(app):
    from fastapi.testclient import TestClient

    model = Greeter(script=[greet, reply(["はい。"]), greet, greet])
    app.set_model(model)
    with TestClient(app.app) as client:
        opened, _ = open_talk(client)
        uid = opened["user"]
        stream(client, uid, "message", 1, "こんにちは")
        app.later(31)
        reopened, again = open_talk(client)
        assert without_consent(reopened) == {"user": uid, "event": "revisit", "thread": 1, "search_track": "未定"}
        assert again["bubbles"] == [REVISIT_GREETING, "何をしますか?"] and again["thread"] == 2
        hist = client.get("/api/talk/history").json()  # 描き直すのは新しい会話だけ
        assert hist["thread"] == 2 and len(hist["turns"]) == 1
    assert model.script == []


def test_a_message_from_an_old_tab_is_not_mixed_into_the_new_conversation(app):
    """同じブラウザの2つのタブ: 古いタブ(会話1)を開いたまま、30分後に別のタブで新しい会話(会話2)が始まった。
    古いタブから送った発言は会話2に入らず(LLM も会話数も使わない)、stale と今の会話番号が返る。
    画面はそれで「別の画面で新しい会話が始まりました」と出し、最新の会話を描き直す。"""
    from fastapi.testclient import TestClient

    model = Greeter(script=[greet, greet, greet])  # 古いタブの初回訪問 + 新しいタブの再訪(要約 → 挨拶)
    app.set_model(model)
    with TestClient(app.app) as client:
        old_tab, _ = open_talk(client)
        uid = old_tab["user"]
        app.later(31)
        _, greeted = open_talk(client)  # 別のタブ(同じ Cookie)で開いた
        assert greeted["thread"] == 2
        used = client.app.state.quota.used(uid)

        evs = stream(client, uid, "message", 1, "古いタブからの発言")  # 古いタブは会話1を表示している
        assert evs == [{"kind": "start", "event": "message", "entry": "A", "event_kind": "user",
                        "model": evs[0]["model"]}, {"kind": "stale", "thread": 2}]
        r = client.post("/api/event", json={"user_id": uid, "event": "message", "text": "古いタブから", "thread": 1})
        assert r.json() == {"stale": True, "thread": 2}
        assert client.app.state.quota.used(uid) == used and model.script == []

        hist = client.get("/api/talk/history").json()
        assert hist["thread"] == 2
        assert [t["user"] for t in hist["turns"]] == [None]  # 会話2は挨拶だけ。古いタブの発言は入っていない
        assert all("古いタブ" not in str(t) for t in hist["turns"])
    page = (app.STATIC / "talk.html").read_text(encoding="utf-8")
    assert '"stale"' in page and "別の画面で新しい会話が始まりました" in page


def test_stale_check_also_holds_inside_the_turn_lock(tmp_path):
    """server の事前の照合をすり抜けても(照合とターンの間に別のタブが会話を始めた)、runtime がターンの排他の中で
    同じ照合をして、発言を新しい会話に入れない。"""
    import asyncio
    import os

    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    model = Greeter(script=[greet, greet])

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            await rt.handle("w-0000000000000001", "visit", talk_thread=0)
            await rt.handle("w-0000000000000001", "visit", talk_thread=1)
            stale = await rt.handle("w-0000000000000001", "message", "古いタブ", talk_thread=1)
            conv = await rt.conversation("w-0000000000000001")
            return stale, conv

    stale, conv = asyncio.run(go())
    assert stale == {"stale": True, "thread": 2}
    assert conv["thread"] == 2 and [t["user"] for t in conv["turns"]] == [None]
    assert model.script == []


def test_stale_found_inside_the_turn_lock_gives_the_turn_back(app, monkeypatch):
    """事前の照合(ロックの外)をすり抜けて会話数を使った後に、runtime が stale と判定したら会話数を返す。"""
    from fastapi.testclient import TestClient

    from line_runtime import turn

    model = Greeter(script=[greet, greet, greet])
    app.set_model(model)
    with TestClient(app.app) as client:
        old_tab, _ = open_talk(client)
        uid = old_tab["user"]
        app.later(31)
        open_talk(client)
        used = client.app.state.quota.used(uid)

        async def passes(deps, t):  # 照合とターンの間に別のタブが会話を始めた、の再現
            return None

        monkeypatch.setattr(turn, "stale_thread", passes)
        assert stream(client, uid, "message", 1, "古いタブから")[-1] == {"kind": "stale", "thread": 2}
        r = client.post("/api/event", json={"user_id": uid, "event": "message", "text": "古いタブから", "thread": 1})
        assert r.json() == {"stale": True, "thread": 2}
        assert client.app.state.quota.used(uid) == used and model.script == []


def test_a_failed_first_greeting_leaves_the_next_open_as_a_first_visit(app, monkeypatch):
    """初回の挨拶が失敗・上限で返らなかったら、開いた記録を残さない: 次に開いても初回訪問(「おかえりなさい」と言わない)。"""
    from fastapi.testclient import TestClient

    def broken(messages):
        raise RuntimeError("API が落ちた")

    model = Greeter(script=[broken, greet])
    app.set_model(model)
    with TestClient(app.app) as client:
        client.get("/")
        opened = client.post("/api/talk/open").json()
        uid = opened["user"]
        assert opened["event"] == "visit"
        evs = stream(client, uid, "visit", opened["thread"])
        assert evs[-1]["kind"] == "error" and thread_of(evs) == 1  # 画面は失敗しても新しい会話の番号を知る
        assert not app.state.user(uid).get("talk_opens")

        # 30分以内でも、返信の無い会話は続けない(続ける相手がいない): 初回訪問から始め直す
        opened, first = open_talk(client)
        assert opened["event"] == "visit"
        assert first["bubbles"] == ["こんにちは。バイト探しをお手伝いします。何をしますか?"]
        assert len(app.state.user(uid)["talk_opens"]) == 1  # 挨拶が返ったので、ここで記録する

    # 上限で挨拶が返らなかったときも記録しない
    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "1")
    app.set_model(Greeter(script=[]))
    with TestClient(app.app) as client:
        client.get("/")
        opened = client.post("/api/talk/open").json()
        uid = opened["user"]
        assert client.app.state.quota.take(uid)  # 今日の枠を使い切っている
        assert stream(client, uid, "visit", opened["thread"])[-1]["limited"] is True
        assert not app.state.user(uid).get("talk_opens")
        assert client.post("/api/talk/open").json()["event"] == "visit"


def test_a_first_greeting_cut_off_by_a_disconnect_still_counts_as_opened(app):
    """初回の挨拶の途中で接続が切れた: 返信は会話に保存されたが、画面に届かず開いた記録も残らなかった。
    30分後に開いたら、会った相手なので再訪の挨拶で始める(初回訪問の挨拶を繰り返さない)。"""
    from fastapi.testclient import TestClient

    model = Greeter(script=[greet] * 3)  # 初回訪問 + 再訪(要約を読む → 挨拶)
    app.set_model(model)
    with TestClient(app.app) as client:
        client.get("/")
        opened = client.post("/api/talk/open").json()
        uid = opened["user"]
        assert opened["event"] == "visit"
        # 接続が切れたときの server と同じ: runtime はターンを保存し終えたが、reply を受け取って記録する前に止まった
        rt = client.app.state.rt
        client.portal.call(rt.handle, uid, "visit", None, None, opened["thread"])
        assert not app.state.user(uid).get("talk_opens")

        app.later(31)
        reopened, again = open_talk(client)
        assert without_consent(reopened) == {"user": uid, "event": "revisit", "thread": 1, "search_track": "未定"}
        assert again["bubbles"] == [REVISIT_GREETING, "何をしますか?"] and again["thread"] == 2
    assert model.script == []


def stream_cut_off(client, uid: str, event: str, thread: int | None) -> list[dict]:
    """本物の切断: ASGI アプリを直接呼び、/api/event/stream が2行目を送る時点で send が OSError を出す
    (画面を閉じた・回線が切れた)。届いた行(1行目だけ)を返す。切断は send の例外か receive の
    http.disconnect で流れてくる。そのターンの処理(裏で進む)が終わるまで待ってから返す。"""
    import asyncio

    from line_runtime.server import ANON_COOKIE

    body = json.dumps({"user_id": uid, "event": event, "thread": thread}).encode()
    got: list[dict] = []
    cut = False

    async def call() -> None:
        nonlocal cut
        disconnected = asyncio.Event()
        requested = False

        async def receive() -> dict:
            nonlocal requested
            if not requested:
                requested = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            nonlocal cut
            if message["type"] != "http.response.body" or not message.get("body"):
                return
            if got:
                cut = True
                disconnected.set()
                raise OSError("Connection reset by peer")
            got.extend(json.loads(line) for line in message["body"].decode().splitlines())

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": "POST", "scheme": "http", "path": "/api/event/stream", "raw_path": b"/api/event/stream",
                 "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
                 "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                             (b"content-length", str(len(body)).encode()),
                             (b"cookie", f"{ANON_COOKIE}={uid}".encode())]}
        with pytest.raises(OSError):
            await client.app(scope, receive, send)
        async with client.app.state.rt.user_lock(uid):  # 画面が去っても、ターンは裏で最後まで進んで保存される
            pass

    client.portal.call(call)
    assert cut
    return got


def test_a_first_greeting_cut_off_by_a_real_disconnect_still_counts_as_opened(app):
    """本物の切断(ASGI の send が OSError)で初回の挨拶が画面に届かなかった: 開いた記録は残らないが、挨拶は会話に
    保存される。30分後に開いたら、会った相手なので再訪の挨拶で始める(talk_open が記録を補う)。"""
    from fastapi.testclient import TestClient

    model = Greeter(script=[greet] * 3)  # 初回訪問 + 再訪(要約を読む → 挨拶)
    app.set_model(model)
    with TestClient(app.app) as client:
        client.get("/")
        opened = client.post("/api/talk/open").json()
        uid = opened["user"]
        assert opened["event"] == "visit"
        got = stream_cut_off(client, uid, "visit", opened["thread"])
        assert [e["kind"] for e in got] == ["start"]  # 2行目(thread)からは届いていない
        assert not app.state.user(uid).get("talk_opens")
        assert client.get("/api/talk/history").json()["turns"][0]["bot"] is not None  # 挨拶は保存された

        app.later(31)
        reopened, again = open_talk(client)
        assert without_consent(reopened) == {"user": uid, "event": "revisit", "thread": 1, "search_track": "未定"}
        assert again["bubbles"] == [REVISIT_GREETING, "何をしますか?"] and again["thread"] == 2
    assert model.script == []


def test_a_greeting_cut_off_before_its_wording_changed_still_counts_as_opened(app, monkeypatch):
    """挨拶の文言を変えてデプロイした: 変える前の文言で切れた初回の挨拶も、会話を始めたときの入口(entry)で
    挨拶と分かるので、30分後に開いたら再訪の挨拶で始める(初回訪問の挨拶を繰り返さない)。"""
    from fastapi.testclient import TestClient

    from line_runtime import runtime

    model = Greeter(script=[greet] * 3)
    app.set_model(model)
    with TestClient(app.app) as client:
        client.get("/")
        opened = client.post("/api/talk/open").json()
        uid = opened["user"]
        stream_cut_off(client, uid, "visit", opened["thread"])
        assert not app.state.user(uid).get("talk_opens")

        monkeypatch.setattr(runtime, "FIRST_MESSAGE", "こちらから最初のひとことを送ってください。")
        monkeypatch.setattr(runtime, "GREETINGS", {"visit": "はじめての訪問", "revisit": "ふたたびの訪問"})
        app.later(31)
        reopened, again = open_talk(client)
        assert without_consent(reopened) == {"user": uid, "event": "revisit", "thread": 1, "search_track": "未定"}
        assert again["bubbles"] == [REVISIT_GREETING, "何をしますか?"] and again["thread"] == 2
    assert model.script == []


def test_greeted_reads_the_entry_and_falls_back_to_the_wording_only_for_old_records(app, monkeypatch):
    """挨拶済みかは会話の入口(entry)で決める。entry の無い古い記録だけ、最初の入力を今の挨拶文と比べる。
    閉じた後の空の会話・音声の結果(entry A)で始まった会話は挨拶済みではない。"""
    from fastapi.testclient import TestClient
    from langchain_core.messages import HumanMessage

    from line_runtime import runtime

    app.set_model(Greeter(script=[]))
    with TestClient(app.app) as client:
        client.get("/")
        uid = client.post("/api/talk/open").json()["user"]
        rt = client.app.state.rt

        async def old_record(first: str) -> dict:
            """entry を残す前の形の会話(挨拶の入力と返信だけ)を新しい会話として書き、conversation を返す。"""
            await rt.new_conversation(uid)
            cfg = rt._cfg(uid, await rt.current_thread(uid))
            await rt.agent.aupdate_state(cfg, {"messages": [HumanMessage(first), reply(["こんにちは。"])]},
                                         as_node="model")
            return await rt.conversation(uid)

        greeting = runtime.event_input("visit", None, None)
        assert client.portal.call(old_record, greeting)["greeted"] is True
        assert client.portal.call(old_record, "こんにちは")["greeted"] is False  # 本人の発言で始まった
        monkeypatch.setattr(runtime, "FIRST_MESSAGE", "こちらから最初のひとことを送ってください。")
        assert client.portal.call(old_record, greeting)["greeted"] is False  # 古い記録は文言を変えると見分けられない

        async def closed_then_voice() -> tuple[dict, dict]:
            await rt.close_conversation(uid)
            closed = await rt.conversation(uid)
            await rt.add_voice_result(uid, {"bubbles": ["音声の結果です。"], "quick_replies": [], "cards": []})
            return closed, await rt.conversation(uid)

        closed, voiced = client.portal.call(closed_then_voice)
        assert closed["greeted"] is False and closed["turns"] == []
        assert voiced["greeted"] is False and voiced["turns"][0]["bot"] is not None


def test_a_user_message_that_looks_like_an_event_is_still_redrawn(app):
    """イベントとして画面に出さないのは会話の最初(挨拶・プッシュ)だけ。途中で本人が「[イベント:」と書いても描き直す。"""
    from fastapi.testclient import TestClient

    model = Greeter(script=[greet, reply(["はい。"])])
    app.set_model(model)
    with TestClient(app.app) as client:
        opened, _ = open_talk(client)
        uid = opened["user"]
        stream(client, uid, "message", 1, "[イベント: テスト] と書いてみた")
        hist = client.get("/api/talk/history").json()
        assert [t["user"] for t in hist["turns"]] == [None, "[イベント: テスト] と書いてみた"]
    assert model.script == []


def test_history_is_readable_only_by_the_owner_cookie(app):
    """履歴の API は本人の匿名ID(Cookie)の会話だけを返す(URL にユーザーIDは無い)。Cookie なし・不正な Cookie は 401。"""
    from fastapi.testclient import TestClient

    model = Greeter(script=[greet])
    app.set_model(model)
    with TestClient(app.app) as a:
        b = TestClient(app.app)
        open_talk(a)
        b.get("/")
        assert len(a.get("/api/talk/history").json()["turns"]) == 1
        assert b.get("/api/talk/history").json() == {"thread": 0, "turns": [], "search_track": "未定"}  # 別のブラウザには a の会話は見えない
        assert TestClient(app.app).get("/api/talk/history").status_code == 401
        assert TestClient(app.app, cookies={app.ANON_COOKIE: "x"}).get("/api/talk/history").status_code == 401


def test_dev_scenarios_do_not_record_talk_opens_or_check_threads(app):
    """/dev の挙動は変えない: シナリオの挨拶・発言は thread を送らないので、照合も開いた記録もしない。"""
    from fastapi.testclient import TestClient

    model = Greeter(script=[greet, reply(["はい。"])])
    app.set_model(model)
    with TestClient(app.app) as client:
        client.get("/dev")
        started = client.post("/api/scenarios/F-follow-new/start").json()
        user = started["user"]
        r = client.post("/api/event/stream", json={"user_id": user, "event": started["event"]})
        greeted = [json.loads(line) for line in r.text.splitlines()]
        r = client.post("/api/event/stream", json={"user_id": user, "event": "message", "text": "こんにちは"})
        answered = [json.loads(line) for line in r.text.splitlines()]
    assert all(e["kind"] != "thread" for e in greeted + answered) and answered[-1]["bubbles"] == ["はい。"]
    assert not app.state.user(user).get("talk_opens")
    assert model.script == []


# ----------------------------- 保存した求人 -----------------------------


def test_saved_jobs_place_shows_what_this_user_saved_in_the_talk(app):
    """トークで保存した求人が「保存した求人」(/api/saved。Cookie の本人)に出る。他のユーザーの一覧には出ない。"""
    from fastapi.testclient import TestClient

    holder: dict = {}

    def save(messages):
        return call("save_job", {"line_user_id": holder["uid"], "job_id": "a003"}, "save")

    model = Greeter(script=[greet, save, reply(["保存しました。"])])
    app.set_model(model)
    with TestClient(app.app) as client:
        opened, _ = open_talk(client)
        holder["uid"] = uid = opened["user"]
        assert client.get("/api/saved").json() == {"jobs": []}
        r = client.post("/api/event/stream", json={"user_id": uid, "event": "message", "text": "a003 を保存して"})
        assert json.loads(r.text.splitlines()[-1])["bubbles"] == ["保存しました。"]
        jobs = client.get("/api/saved").json()["jobs"]
        other = TestClient(app.app)
        other.get("/")
        assert other.get("/api/saved").json() == {"jobs": []}
    assert [j["job_id"] for j in jobs] == ["a003"]
    assert {"title", "employer", "pay", "pay_text", "shift", "station", "walk_min", "saved_at"} <= jobs[0].keys()
    assert model.script == []


# ----------------------------- 画面: / はトークだけ、/dev は今までの3列 -----------------------------


def test_root_is_the_talk_only_screen_and_dev_keeps_the_simulator(app):
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        root = client.get("/")
        dev = client.get("/dev")
    assert root.status_code == dev.status_code == 200
    page = root.text
    # dip の状態・構成図・トレース・シナリオ一覧は出さない(呼びもしない)
    for dev_only in ('id="scenarios"', 'id="state"', 'id="trace"', "<svg", "/api/scenarios", "/api/dip", "restart"):
        assert dev_only not in page, dev_only
    # 開いたら dip に初回訪問か再訪かを聞いて始める(30分以内なら履歴を描き直す)。ナビは3つの置き場(プロフィールは task 95)
    assert "/api/talk/open" in page and "/api/talk/history" in page
    for place in ("トーク", "保存した求人", "プロフィール", "/api/saved", "/api/profile"):
        assert place in page, place
    # ヘッダーに履歴書・音声のボタンは無い(入力欄の「+」と、AI が差し出す操作から入る。task 91)
    header = re.search(r"<header>.*?</header>", page, re.DOTALL).group(0)
    assert "履歴書" not in header and "音声" not in header and 'id="resume"' not in page and 'id="voice"' not in page
    for p in (page, dev.text):  # どちらの画面もトークの部品は共通のファイルから
        assert '<script src="/static/talk-core.js">' in p and "createTalk(" in p
        assert "function renderReply" not in p and "function jobCard" not in p
    with TestClient(app.app) as client:
        core = client.get("/static/talk-core.js")
        assert core.status_code == 200 and "function renderReply" in core.text
        assert client.get("/static/talk-core.css").status_code == 200
        assert client.get("/static/talk.html").status_code == 404  # 共通部品以外は出さない
    # /dev は今までの3列(シナリオの起動・状態・構成図と処理の流れ)
    for part in ('id="scenarios"', 'id="state"', 'id="trace"', "/api/scenarios/", "/api/dip"):
        assert part in dev.text, part
