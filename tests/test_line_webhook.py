"""LINE 公式アカウントの webhook(task 177)。実 LINE も実 LLM も呼ばない: LINE の API は偽物(httpx.MockTransport)、
LLM は決められた順に返す偽のモデル、dip の MCP は本物。LINE のサーバーと同じく、署名を付けた webhook を POST する。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from dip_mcp import data
from dip_mcp.data import State, load_jobs
from line_runtime import line_channel
from line_runtime.fake_line import FakeLinePlatform
from line_runtime.line_channel import LINE_ID, LineApi, line_user, to_messages
from line_runtime.quota import JST
from tests.test_handle import call, reply
from tests.test_revisit import Greeter, _tool_result, greet, propose

SECRET = "offline-test-channel-secret"
ID_KEY = "offline-test-aica-id-key-0123456789abcdef"  # LINE のユーザーIDを作る鍵(32 バイト以上)
RAW_USER = "U" + "0123456789abcdef" * 2  # LINE の userId の形(U + 32 桁)。どこにも保存されてはいけない
RAW_OTHER = "U" + "fedcba9876543210" * 2
REPLY, PUSH, LOADING = "/v2/bot/message/reply", "/v2/bot/message/push", "/v2/bot/chat/loading/start"
LINE_ENVS = ("LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "AICA_PUBLIC_URL")
QUOTA_ENVS = ("LINE_SIM_DAILY_TURN_LIMIT", "AICA_DAILY_TURN_LIMIT_PER_IP", "AICA_DAILY_NEW_IDS_PER_IP",
              "AICA_DAILY_TURN_LIMIT_TOTAL", "AICA_DAILY_NEW_IDS_TOTAL", "AICA_TRUST_PROXY")


@pytest.fixture(autouse=True)
def line_id_key(monkeypatch):
    """LINE のユーザーIDを作る鍵。LINE の設定があるときは起動に要る(test_account_link も import して使う)。"""
    monkeypatch.setenv(line_channel.ID_KEY_ENV, ID_KEY)


class FakeLine:
    """偽物の LINE の API。受けた呼び出しを順に記録する。fail のパスは 400 を返す(期限切れの返信トークなど)。
    reject(パス, 本文)が真なら 400(メッセージの形を断る)。timeout のパスは応答を返さない(ReadTimeout)。
    slow_loading: 最初の「入力中」の表示だけ、この秒数だけ待ってから返す。
    linkToken の発行は偽物の LINE プラットフォーム(platform。line_runtime.fake_line)が答える。"""

    def __init__(self, fail: tuple[str, ...] = (), reject=None, timeout: tuple[str, ...] = (),
                 slow_loading: float = 0.0) -> None:
        self.calls: list[tuple[str, dict, str | None]] = []
        self.fail, self.reject, self.timeout, self.slow_loading = set(fail), reject, set(timeout), slow_loading
        self.platform = FakeLinePlatform()
        self.transport = httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path, body = request.url.path, json.loads(request.content or b"{}")
        self.calls.append((path, body, request.headers.get("authorization")))
        if path not in self.fail and (issued := self.platform.api_response(request)) is not None:
            return issued
        if path == LOADING and self.slow_loading:
            delay, self.slow_loading = self.slow_loading, 0.0
            await asyncio.sleep(delay)
        if path in self.timeout:
            raise httpx.ReadTimeout("no response", request=request)
        if path in self.fail or (self.reject and self.reject(path, body)):
            return httpx.Response(400, json={"message": "Invalid reply token"})
        return httpx.Response(200, json={})

    def sent(self) -> list[tuple[str, dict]]:
        """届けたメッセージ(返信と push)。"""
        return [(p, b) for p, b, _ in self.calls if p in (REPLY, PUSH)]


def _server(tmp_path, monkeypatch, model, fake: FakeLine | None = None, configured: bool = True, **env: str):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")
    for k in LINE_ENVS + QUOTA_ENVS:
        monkeypatch.delenv(k, raising=False)
    if configured:
        monkeypatch.setenv("LINE_CHANNEL_SECRET", SECRET)
        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "offline-test-token")
        monkeypatch.setenv("AICA_PUBLIC_URL", "https://aica.example.test/")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from line_runtime import config, server
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    # 会話も費用台帳もファイルに書く(userId が保存されていないことを、書かれたファイルで確かめる)
    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model))
    fake = fake or FakeLine()
    monkeypatch.setattr(server, "line_api", lambda: LineApi("offline-test-token", transport=fake.transport))
    return server, fake


def _event(kind: str, user: str = RAW_USER, text: str | None = None, event_id: str = "01EVENT0000000000000000001",
           redelivery: bool = False, token: str = "reply-token-1", source: str = "user") -> dict:
    e = {"type": kind, "mode": "active", "timestamp": 1792540800000, "webhookEventId": event_id,
         "deliveryContext": {"isRedelivery": redelivery}, "replyToken": token,
         "source": {"type": source, "userId": user} | ({"groupId": "Cgroup"} if source == "group" else {})}
    if kind == "message":
        e["message"] = {"type": "text", "id": "468789577898262530", "quoteToken": "q", "text": text}
    return e


def _post(client, events: list[dict], signature: str | None = "sign") -> httpx.Response:
    body = json.dumps({"destination": "Ubot", "events": events}, ensure_ascii=False).encode()
    headers = {"content-type": "application/json"}
    if signature == "sign":
        signature = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    if signature is not None:
        headers["x-line-signature"] = signature
    return client.post("/line/webhook", content=body, headers=headers)


def _search_and_propose(uid: str) -> Greeter:
    """「渋谷で日中」に、dip の recommend_jobs で探して、返った求人をカードにして返す偽のモデル。"""
    return Greeter(script=[call("recommend_jobs", {"line_user_id": uid, "area": "渋谷", "time_band": "日中"}, "rec"),
                           propose])


# ----------------------------- 返信が 1 回だけ届く -----------------------------


def test_signed_text_message_is_answered_once_with_text_quick_replies_and_job_flex(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    model = _search_and_propose(uid)
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        r = _post(client, [_event("message", text="渋谷で日中")])
        assert r.status_code == 200

    assert model.script == []  # ツールを呼ぶ 1 回 + 返信 1 回
    assert fake.calls[0][:2] == (LOADING, {"chatId": RAW_USER, "loadingSeconds": 60})  # 受けてすぐ「入力中」
    assert all(auth == "Bearer offline-test-token" for _, _, auth in fake.calls)
    ((path, body),) = fake.sent()  # 返信トークで 1 回だけ届く(push はしない)
    assert path == REPLY and body["replyToken"] == "reply-token-1"
    texts = [m for m in body["messages"] if m["type"] == "text"]
    (flex,) = [m for m in body["messages"] if m["type"] == "flex"]
    assert [t["text"] for t in texts] == ["条件に合う求人です。気になるものはありますか?"]
    # 求人は、このターンに dip の recommend_jobs が返したもの(偽のモデルはツールの結果の ID をそのまま返す)
    bubbles = flex["contents"]["contents"]
    assert flex["contents"]["type"] == "carousel" and 1 <= len(bubbles) <= 3
    saves = [re.fullmatch(r"「(.+)」\((\w+)\)を保存して", b["footer"]["contents"][1]["action"]["text"]) for b in bubbles]
    jobs = load_jobs()
    assert all(m and jobs[m[2]].title == m[1] for m in saves)
    last = body["messages"][-1]
    assert [i["action"]["text"] for i in last["quickReply"]["items"]] == ["詳しく見る", "ほかの求人", "今はいい"]
    assert len(body["messages"]) <= 5


def test_cards_are_only_jobs_dip_returned(tmp_path, monkeypatch):
    """モデルが作った求人 ID はカードにしない(render の決まりのまま)。"""
    model = Greeter(script=[reply(["こちらはいかがですか?"], job_ids=["zz999"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _post(client, [_event("message", text="何かある?")])
    ((_, body),) = fake.sent()
    assert [m["type"] for m in body["messages"]] == ["text"]


def test_expired_reply_token_falls_back_to_push(tmp_path, monkeypatch):
    model = Greeter(script=[reply(["はい、どうぞ。"])])
    server, fake = _server(tmp_path, monkeypatch, model, FakeLine(fail=(REPLY,)))
    with TestClient(server.app) as client:
        _post(client, [_event("message", text="こんにちは")])
    paths = [p for p, _ in fake.sent()]
    assert paths == [REPLY, PUSH]  # 返信トークで断られた → push で 1 回だけ届く
    push = fake.sent()[1][1]
    assert push["to"] == RAW_USER and push["messages"][0]["text"] == "はい、どうぞ。"


def test_reply_without_response_is_not_pushed_again(tmp_path, monkeypatch):
    """返信の応答が来なかった(届いたかもしれない)ときは push しない(2 回届かないように)。"""
    model = Greeter(script=[reply(["はい、どうぞ。"])])
    server, fake = _server(tmp_path, monkeypatch, model, FakeLine(timeout=(REPLY,)))
    with TestClient(server.app) as client:
        _post(client, [_event("message", text="こんにちは")])
    assert [p for p, _ in fake.sent()] == [REPLY]


def test_a_reply_line_rejects_still_tells_the_user_something(tmp_path, monkeypatch):
    """LINE がメッセージの形を断ったら(返信も push も)、謝りの文だけを push する(入力中の表示のまま黙らない)。"""
    model = Greeter(script=[reply(["はい、どうぞ。"], quick_replies=["今はいい"])])
    def rejects_quick_replies(_path, body):
        return any("quickReply" in m for m in body.get("messages", []))

    server, fake = _server(tmp_path, monkeypatch, model, FakeLine(reject=rejects_quick_replies))
    with TestClient(server.app) as client:
        _post(client, [_event("message", text="こんにちは")])
    assert [p for p, _ in fake.sent()] == [REPLY, PUSH, PUSH]
    assert fake.sent()[-1][1]["messages"] == [{"type": "text", "text": line_channel.FAILED_TEXT}]


def test_one_users_messages_are_answered_in_the_order_they_arrived(tmp_path, monkeypatch):
    """1 通ずつ別の webhook で届いても、同じ人の発言は届いた順にターンへ入る(先の「入力中」の表示が遅くても)。
    TestClient は応答の後の処理を 1 つずつ進めるので、実際の uvicorn と同じく 2 つを並べて動かす。"""
    seen = []

    def echo(messages):
        seen.append(messages[-1].content)
        return reply([f"「{messages[-1].content}」ですね"])

    model = Greeter(script=[echo, echo])
    server, fake = _server(tmp_path, monkeypatch, model, FakeLine(slow_loading=0.5))
    first = [_event("message", text="FIRST", event_id="01E1", token="reply-token-1")]
    second = [_event("message", text="SECOND", event_id="01E2", token="reply-token-2")]
    with TestClient(server.app) as client:
        async def both():
            await asyncio.gather(server._line_handle(first), server._line_handle(second))

        client.portal.call(both)
    assert seen == ["FIRST", "SECOND"]
    assert [b["replyToken"] for _, b in fake.sent()] == ["reply-token-1", "reply-token-2"]


def test_failed_turn_tells_the_user_instead_of_leaving_them_waiting(tmp_path, monkeypatch):
    def broken(_messages):
        raise RuntimeError("API が落ちた")

    model = Greeter(script=[broken])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text="こんにちは")]).status_code == 200
    ((_, body),) = fake.sent()
    assert body["messages"] == [{"type": "text", "text": line_channel.FAILED_TEXT}]


# ----------------------------- 署名 -----------------------------


def test_missing_or_wrong_signature_is_400_and_the_agent_does_not_run(tmp_path, monkeypatch):
    model = Greeter(script=[reply(["呼ばれてはいけない"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text="こんにちは")], signature=None).status_code == 400
        assert _post(client, [_event("message", text="こんにちは")], signature="AAAA").status_code == 400
        wrong = base64.b64encode(hmac.new(b"other-secret", b"{}", hashlib.sha256).digest()).decode()
        assert _post(client, [_event("message", text="こんにちは")], signature=wrong).status_code == 400
    assert len(model.script) == 1 and fake.calls == []
    assert line_user(RAW_USER) not in server.state.user_ids()


# ----------------------------- 再送 -----------------------------


def test_redelivered_event_is_not_answered_twice(tmp_path, monkeypatch):
    model = Greeter(script=[reply(["はい、どうぞ。"]), reply(["2 回目は呼ばれてはいけない"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text="こんにちは")]).status_code == 200
        again = _event("message", text="こんにちは", redelivery=True, token="reply-token-2")
        assert _post(client, [again]).status_code == 200
    assert len(model.script) == 1 and len(fake.sent()) == 1

    # 再起動した後に届いた再送にも答えない(受けた ID は data/ に残る)
    with TestClient(server.app) as client:
        assert _post(client, [again]).status_code == 200
    assert len(model.script) == 1 and len(fake.sent()) == 1


# ----------------------------- ユーザー -----------------------------


def test_line_users_are_production_users_with_their_own_ids_and_quota(tmp_path, monkeypatch):
    """LINE のユーザーは l-<ハッシュ>: 匿名ユーザーID・シナリオのユーザーと重ならず、実際の時刻を使い、
    会話数は LINE のユーザーごとに数える(どの人も LINE のサーバーから届くので、IP ごとには数えない)。
    userId そのものは、書かれたどのファイルにも残らない。"""
    from line_runtime.server import ANON_ID

    uid, other = line_user(RAW_USER), line_user(RAW_OTHER)
    model = Greeter(script=[greet, greet, reply(["はい、どうぞ。"])])
    # 1 つの IP から使える匿名IDを 1 つにしても、LINE の 2 人はどちらも話せる
    server, fake = _server(tmp_path, monkeypatch, model, AICA_DAILY_NEW_IDS_PER_IP="1")
    with TestClient(server.app) as client:
        before = datetime.now(JST)
        _post(client, [_event("follow", event_id="01FOLLOW1"), _event("follow", user=RAW_OTHER, event_id="01FOLLOW2",
                                                                    token="reply-token-2")])
        _post(client, [_event("message", text="こんにちは", event_id="01MSG1", token="reply-token-3")])
        quota = server.app.state.quota
        assert quota.used(uid) == 2 and quota.used(other) == 1 and quota.total_used() == 3

    assert uid != other and LINE_ID.fullmatch(uid) and LINE_ID.fullmatch(other)
    assert uid == line_user(RAW_USER)  # 同じ人はいつも同じユーザー
    assert not ANON_ID.fullmatch(uid) and not data.uses_sim_clock(uid)
    assert uid not in data.load_users() and ":" not in uid
    # 友だち追加は初回訪問の挨拶。挨拶が返ったことは実際の時刻で記録する(SIM_NOW ではない)
    greeting = next(b for _, b in fake.sent() if b["replyToken"] == "reply-token-1")
    assert greeting["messages"][0]["text"] == "こんにちは。バイト探しをお手伝いします。何をしますか?"
    (opened,) = server.state.user(uid)["talk_opens"]
    assert abs(datetime.fromisoformat(opened) - before) < timedelta(minutes=1)
    assert len(fake.sent()) == 3 and model.script == []

    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert any(p.name == "line_checkpoints.db" for p in files) and any(p.name == "costs.jsonl" for p in files)
    for p in files:
        raw = p.read_bytes()
        assert RAW_USER.encode() not in raw and RAW_OTHER.encode() not in raw, p.name
    assert uid.encode() in (tmp_path / "costs.jsonl").read_bytes()


def test_follow_again_after_block_is_a_revisit(tmp_path, monkeypatch):
    model = Greeter(script=[greet, greet, greet])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _post(client, [_event("follow", event_id="01F1")])
        _post(client, [_event("unfollow", event_id="01U1")])  # ブロック: 無視する
        _post(client, [_event("follow", event_id="01F2", token="reply-token-2")])
    first, again = (b["messages"][0]["text"] for _, b in fake.sent())
    assert first.startswith("こんにちは") and again.startswith("おかえりなさい")


def test_other_events_are_ignored_with_200(tmp_path, monkeypatch):
    model = Greeter(script=[reply(["呼ばれてはいけない"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    image = _event("message", event_id="01IMG") | {"message": {"type": "image", "id": "1"}}
    standby = _event("message", text="ほかのチャネルが受け持つ間", event_id="01S") | {"mode": "standby"}
    with TestClient(server.app) as client:
        for events in ([], [_event("unfollow", event_id="01U")], [image], [standby],
                       [_event("message", text="  \n ", event_id="01BLANK")],
                       [_event("message", text="グループの発言", event_id="01G", source="group")],
                       [_event("postback", event_id="01P")]):
            assert _post(client, events).status_code == 200
    assert len(model.script) == 1 and fake.calls == []


# ----------------------------- 会話の区切り(Web と同じ 30 分) -----------------------------


def _later(monkeypatch, server, minutes: int) -> None:
    """時刻を進める(最後のやりとり = チェックポイントの保存時刻 は実時刻なので、今を先へずらす。test_talk_page と同じ)。"""
    monkeypatch.setattr(server, "clock", lambda: datetime.now(UTC) + timedelta(minutes=minutes))


def _answer(seen: list[dict]):
    """そのターンにモデルが受け取った会話(本人側の入力と入口)を覚えて、決まった返事をする。"""
    def answer(messages) -> AIMessage:
        seen.append({"said": [m.content for m in messages if m.type == "human"],
                     "entry_a": "入口A(トーク)" in messages[0].content})
        return reply(["承知しました。"], quick_replies=["求人を探す"])
    return answer


def test_text_continues_within_30_minutes_and_starts_a_new_conversation_after(tmp_path, monkeypatch):
    """Web のトーク画面を開いたときと同じ決まり(server.talk_continues): 最後のやりとりから 29 分なら同じ会話、
    31 分なら新しい会話。新しい会話は本人の発言から始まり(入口A)、前の会話はモデルに渡らない。"""
    uid = line_user(RAW_USER)
    seen: list[dict] = []
    model = Greeter(script=[greet, _answer(seen), _answer(seen)])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        rt = server.app.state.rt
        _post(client, [_event("follow", event_id="01F1")])
        assert client.portal.call(rt.current_thread, uid) == 1
        _later(monkeypatch, server, 29)
        _post(client, [_event("message", text="29分後", event_id="01M1", token="reply-token-2")])
        assert client.portal.call(rt.current_thread, uid) == 1
        _later(monkeypatch, server, 31)
        _post(client, [_event("message", text="31分後", event_id="01M2", token="reply-token-3")])
        assert client.portal.call(rt.current_thread, uid) == 2

    assert model.script == []
    assert seen[0]["said"][-1] == "29分後" and len(seen[0]["said"]) == 2  # 友だち追加の挨拶の会話の続き
    assert seen[1] == {"said": ["31分後"], "entry_a": True}  # 新しい会話: 前の会話は渡らない
    assert [b["messages"][0]["text"] for p, b in fake.sent()][1:] == ["承知しました。"] * 2  # 挨拶は挟まない


def test_first_text_without_follow_starts_a_conversation_and_the_next_one_continues_it(tmp_path, monkeypatch):
    """返信のある会話がまだ無ければ新しい会話を始め、すぐ後の発言はその会話に続く。"""
    uid = line_user(RAW_USER)
    seen: list[dict] = []
    model = Greeter(script=[_answer(seen), _answer(seen)])
    server, _ = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _post(client, [_event("message", text="はじめまして", event_id="01M1")])
        _post(client, [_event("message", text="続き", event_id="01M2", token="reply-token-2")])
        assert client.portal.call(server.app.state.rt.current_thread, uid) == 1
    assert [s["said"] for s in seen] == [["はじめまして"], ["はじめまして", "続き"]]


def test_new_conversation_after_30_minutes_still_gets_saved_preferences_and_the_consultation_summary(
        tmp_path, monkeypatch):
    """前の会話で保存した希望と相談のまとめは、31 分後に始まった新しい会話でも dip の要約からエージェントに渡る。"""
    uid = line_user(RAW_USER)
    note = "渋谷で日中のカフェを探している。次は週の日数を確かめる。"
    got: dict = {}

    def read_summary(messages) -> AIMessage:
        got["said"] = [m.content for m in messages if m.type == "human"]
        got["summary"] = _tool_result(messages, "get_user_conversation_summary")
        return reply(["前回は渋谷・日中のカフェでしたね。週に何日働けますか?"], quick_replies=["週2日", "週3日以上"])

    first_turn = AIMessage(content="", tool_calls=[
        {"name": "save_user_preferences", "id": "save", "type": "tool_call",
         "args": {"line_user_id": uid, "preferences": {"area": "渋谷", "time_band": "日中", "job_type": "カフェ"}}},
        {"name": "record_conversation_outcome", "id": "out", "type": "tool_call",
         "args": {"line_user_id": uid, "outcome": "consult", "reason": "希望を聞いた", "confidence": 0.7, "summary": note}}])
    model = Greeter(script=[first_turn, reply(["承知しました。"]),
                            call("get_user_conversation_summary", {"line_user_id": uid}, "sum"), read_summary])
    server, _ = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _post(client, [_event("message", text="渋谷で日中のカフェ", event_id="01M1")])
        _later(monkeypatch, server, 31)
        _post(client, [_event("message", text="また探したい", event_id="01M2", token="reply-token-2")])
        assert client.portal.call(server.app.state.rt.current_thread, uid) == 2

    assert model.script == []
    assert got["said"] == ["また探したい"]  # 新しい会話(前の会話の発言は渡らない)
    prefs = {k: v["value"] for k, v in got["summary"]["stated_preferences"].items()}
    assert prefs == {"area": "渋谷", "time_band": "日中", "job_type": "カフェ"}
    assert got["summary"]["consultation_summary"]["text"] == note


# ----------------------------- ユーザーIDの鍵 -----------------------------


def test_line_user_id_is_a_keyed_hash_that_cannot_be_made_from_the_user_id_alone(monkeypatch):
    """同じ鍵と userId なら同じ l-…、鍵が違えば違う l-…。鍵なしの sha256(前の作り方)とは一致しないので、
    userId を知っていても鍵を知らなければ l-… を作れない。鍵が無い・短いときは作らない。"""
    other_key = "another-offline-aica-id-key-fedcba9876543210"
    uid = line_user(RAW_USER)
    assert LINE_ID.fullmatch(uid) and uid == line_user(RAW_USER) == line_user(RAW_USER, ID_KEY.encode())
    assert uid != line_user(RAW_OTHER)
    assert uid == "l-" + hmac.new(ID_KEY.encode(), RAW_USER.encode(), hashlib.sha256).hexdigest()[:16]
    assert line_user(RAW_USER, other_key.encode()) != uid
    monkeypatch.setenv(line_channel.ID_KEY_ENV, other_key)
    assert line_user(RAW_USER) == line_user(RAW_USER, other_key.encode()) != uid
    unkeyed = {hashlib.sha256(f"aica-line:{RAW_USER}".encode()).hexdigest()[:16],
               hashlib.sha256(RAW_USER.encode()).hexdigest()[:16]}
    assert {uid[2:], line_user(RAW_USER)[2:]}.isdisjoint(unkeyed)

    for key in ("", "too-short-key"):
        monkeypatch.setenv(line_channel.ID_KEY_ENV, key)
        with pytest.raises(line_channel.IdKeyError, match="AICA_ID_KEY"):
            line_user(RAW_USER)
    monkeypatch.delenv(line_channel.ID_KEY_ENV)
    with pytest.raises(line_channel.IdKeyError, match="AICA_ID_KEY"):
        line_user(RAW_USER)


def test_line_settings_without_a_long_enough_id_key_stop_the_server_at_startup(tmp_path, monkeypatch):
    model = Greeter(script=[reply(["呼ばれてはいけない"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    for key, why in ((None, "がありません"), ("x" * 31, "が短すぎます")):
        if key is None:
            monkeypatch.delenv(line_channel.ID_KEY_ENV)
        else:
            monkeypatch.setenv(line_channel.ID_KEY_ENV, key)
        with pytest.raises(line_channel.IdKeyError, match=f"AICA_ID_KEY {why}") as err, TestClient(server.app):
            pass
        assert "secrets.token_hex(32)" in str(err.value)  # 鍵の作り方も出す
    monkeypatch.setenv(line_channel.ID_KEY_ENV, "x" * 32)  # 32 バイトあれば起動する
    with TestClient(server.app) as client:
        assert client.get("/").status_code == 200
    assert len(model.script) == 1 and fake.calls == []


def test_without_line_settings_the_server_starts_without_an_id_key(tmp_path, monkeypatch):
    model = Greeter(script=[reply(["呼ばれてはいけない"])])
    server, fake = _server(tmp_path, monkeypatch, model, configured=False)
    monkeypatch.delenv(line_channel.ID_KEY_ENV)
    with TestClient(server.app) as client:
        assert client.get("/").status_code == 200
        assert _post(client, [_event("message", text="こんにちは")]).status_code == 404
    assert len(model.script) == 1 and fake.calls == []


def test_line_user_ids_are_made_only_by_line_user():
    """LINE のユーザーID(l-…)を作るのは line_channel.line_user だけ(ほかの所で鍵なしの ID を作らない)。"""
    root = Path(__file__).resolve().parents[1]
    makers = [p.relative_to(root).as_posix() for d in ("dip_mcp", "line_runtime", "evals", "voice_resume", "scripts")
              for p in (root / d).rglob("*.py") if re.search(r"[\"']l-[\"']", p.read_text(encoding="utf-8"))]
    assert makers == ["line_runtime/line_channel.py"]


# ----------------------------- 設定が無いとき -----------------------------


def test_without_settings_the_webhook_is_404_and_nothing_changes(tmp_path, monkeypatch):
    model = Greeter(script=[reply(["呼ばれてはいけない"])])
    server, fake = _server(tmp_path, monkeypatch, model, configured=False)
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text="こんにちは")]).status_code == 404
        monkeypatch.setenv("LINE_CHANNEL_SECRET", SECRET)  # シークレットだけでも、トークンだけでも開かない
        assert _post(client, [_event("message", text="こんにちは")]).status_code == 404
        monkeypatch.delenv("LINE_CHANNEL_SECRET")
        monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "offline-test-token")
        assert _post(client, [_event("message", text="こんにちは")]).status_code == 404
        assert client.get("/").status_code == 200  # Web のトーク画面はそのまま
    assert len(model.script) == 1 and fake.calls == []


# ----------------------------- 返信の形 -----------------------------


def test_reply_becomes_at_most_five_line_messages():
    card = {"job_id": "a001", "title": "カフェのホール", "employer": "架空カフェ 渋谷店", "employment_type": "アルバイト・パート",
            "job_type": "飲食", "area": "渋谷", "station": "渋谷駅", "walk_min": 3, "pay_text": "時給1300円",
            "shift": "日中", "days_min": 2, "reasons": ["渋谷で日中"]}
    r = {"bubbles": ["1つ目", "2つ目", "3つ目"], "quick_replies": ["はい", "今はいい"], "cards": [card],
         "buttons": [{"label": "応募の続き", "url": "/liff/apply?job=a001"}, {"label": "外", "url": "https://evil.test"}],
         "actions": ["resume_upload", "voice_consult"]}
    msgs = to_messages(r, "https://aica.example.test/")
    assert len(msgs) == 5
    assert [m["type"] for m in msgs] == ["text", "text", "text", "flex", "flex"]
    # 5 つに収めるため、3 つ目の吹き出しと操作の案内 2 つは 1 つのテキストにまとめる
    assert msgs[2]["text"] == "\n\n".join(["3つ目", *line_channel.ACTION_NOTES.values()])
    (link,) = msgs[4]["contents"]["body"]["contents"]  # /liff/ のボタンだけ、公開 URL へのリンクにする
    assert link["action"] == {"type": "uri", "label": "応募の続き", "uri": "https://aica.example.test/liff/apply?job=a001"}
    assert "quickReply" in msgs[-1] and all("quickReply" not in m for m in msgs[:-1])
    bubble = msgs[3]["contents"]["contents"][0]
    assert bubble["footer"]["contents"][0]["action"]["text"] == "「カフェのホール」について詳しく教えて"

    # 公開 URL が無ければ、リンクは出さない
    assert [m["type"] for m in to_messages(r, "")] == ["text", "text", "text", "text", "flex"]
    # 上限の案内(limit_reply)も同じ形で送れる
    from line_runtime.quota import LIMIT_MESSAGE, limit_reply
    assert to_messages(limit_reply("A", LIMIT_MESSAGE), "") == [{"type": "text", "text": LIMIT_MESSAGE}]


def test_blank_labels_and_bad_public_urls_never_reach_line():
    """LINE が断る形(空のラベル、空の本文、http(s) でない URI)は送らない。送るものが無ければ謝りの文にする。"""
    r = {"bubbles": ["  ", "はい"], "quick_replies": ["", "  ", "今はいい"], "cards": [],
         "buttons": [{"label": " ", "url": "/liff/apply?job=a001"}, {"label": "応募の続き", "url": "/liff/apply?job=a001"}],
         "actions": []}
    msgs = to_messages(r, "https://aica.example.test")
    assert msgs[0] == {"type": "text", "text": "はい"}
    (link,) = msgs[1]["contents"]["body"]["contents"]
    assert link["action"]["label"] == "応募の続き" and msgs[1]["altText"]
    assert [i["action"]["label"] for i in msgs[-1]["quickReply"]["items"]] == ["今はいい"]
    assert [m["type"] for m in to_messages(r, "aica.example.test")] == ["text"]  # https:// の無い公開 URL ではリンクを出さない
    assert to_messages({"bubbles": [" "], "quick_replies": []}, "") == [{"type": "text", "text": line_channel.FAILED_TEXT}]


def test_seen_events_survive_a_broken_file(tmp_path):
    """受けたイベントの ID のファイルが壊れていても Web サーバーは起動する(覚えていた ID だけを捨てる)。"""
    path = tmp_path / "line_events.json"
    path.write_text("[\"01A\", ", encoding="utf-8")
    seen = line_channel.SeenEvents(path)
    assert seen.first_time("01A") and not seen.first_time("01A")
    assert json.loads(path.read_text(encoding="utf-8")) == ["01A"]
    assert not line_channel.SeenEvents(path).first_time("01A")
