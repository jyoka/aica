"""会話数の上限とユーザー別費用集計(task 59)、IP ごと・全体の上限(task 112)、IPv6 の /64・全体の匿名IDの上限(task 113)。実 LLM なし(呼ばれた回数を数える偽のモデル)。"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from functools import partial

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

from dip_mcp.data import State
from line_runtime import ledger as ledger_mod
from line_runtime.quota import (
    DEFAULT_IP_IDS,
    DEFAULT_IP_LIMIT,
    DEFAULT_LIMIT,
    DEFAULT_TOTAL_IDS,
    DEFAULT_TOTAL_LIMIT,
    IP_LIMIT_MESSAGE,
    JST,
    LIMIT_MESSAGE,
    TOTAL_LIMIT_MESSAGE,
    DailyQuota,
    daily_limit,
    ip_ids_limit,
    ip_key,
    ip_limit,
    total_ids_limit,
    total_limit,
)
from tests.test_handle import reply


class CountingModel(BaseChatModel):
    """呼ばれるたびに数えて、決まった返信を返す。"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "counting"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=reply(["はい、どうぞ。"]))])


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


LIVEKIT = {"LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k",
           "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret"}
QUOTA_ENVS = ("AICA_DAILY_TURN_LIMIT_PER_IP", "AICA_DAILY_NEW_IDS_PER_IP", "AICA_DAILY_TURN_LIMIT_TOTAL",
              "AICA_DAILY_NEW_IDS_TOTAL", "AICA_TRUST_PROXY")


def _server(tmp_path, monkeypatch, limit: str | None, **env: str):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")
    for k in QUOTA_ENVS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if limit is None:
        monkeypatch.delenv("LINE_SIM_DAILY_TURN_LIMIT", raising=False)
    else:
        monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", limit)
    from line_runtime import config, server
    from line_runtime.runtime import LineRuntime

    model = CountingModel()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, in_memory=True))
    server.state.reset()
    return server, model


def _stream(client, user, event, text=None) -> list[dict]:
    r = client.post("/api/event/stream", json={"user_id": user, "event": event, "text": text})
    assert r.status_code == 200
    return [json.loads(x) for x in r.text.splitlines()]


def test_over_the_limit_gets_a_polite_notice_without_calling_the_llm_and_recovers_next_day(tmp_path, monkeypatch):
    server, model = _server(tmp_path, monkeypatch, "2")
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        clock = Clock(datetime(2026, 9, 27, 23, 50, tzinfo=JST))
        server.app.state.quota.clock = clock
        client.get("/dev")
        user = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert client.post("/api/event", json={"user_id": user, "event": "visit"}).json()["bubbles"] == ["はい、どうぞ。"]
        assert _stream(client, user, "message", "渋谷で")[-1]["bubbles"] == ["はい、どうぞ。"]
        assert model.calls == 2
        rows = len(server.app.state.rt.ledger.rows)

        # 3回目: 丁寧な案内が返り、LLM は呼ばれない(費用台帳も増えない)。どちらの API でも同じ
        r = client.post("/api/event", json={"user_id": user, "event": "message", "text": "もっと"}).json()
        assert r["bubbles"] == [LIMIT_MESSAGE] and r["limited"] and r["cards"] == []
        last = _stream(client, user, "message", "もっと")[-1]
        assert last["kind"] == "reply" and last["bubbles"] == [LIMIT_MESSAGE]
        # プッシュも送らない(dip に接触として記録させない)
        assert client.post("/api/event", json={"user_id": user, "event": "form_exit"}).json()["sent"] is False
        assert model.calls == 2 and len(server.app.state.rt.ledger.rows) == rows
        # 同じブラウザのシナリオユーザーを替えても、上限は人(匿名ID)単位で共有
        anon = client.get("/api/me").json()["user_id"]
        assert client.post("/api/event", json={"user_id": f"{anon}:sim-exit", "event": "message", "text": "hi"}).json()["limited"]
        assert client.post("/api/event", json={"user_id": f"{anon}:made-up", "event": "message", "text": "hi"}).status_code == 403
        # 別の人(別のブラウザ。ターンを進めるので lifespan と同じクライアントの Cookie を替える)は使える
        mine = dict(client.cookies)
        client.cookies.clear()
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        assert client.post("/api/event", json={"user_id": uid, "event": "message", "text": "hi"}).json()["bubbles"] == [
            "はい、どうぞ。"]
        assert model.calls == 3
        client.cookies.clear()
        client.cookies.update(mine)

        # 日付(日本時間)が変わると再び使える
        clock.now += timedelta(minutes=15)
        assert _stream(client, user, "message", "今日も")[-1]["bubbles"] == ["はい、どうぞ。"]
        assert model.calls == 4

    # 数は保存されるので、再起動しても当日の分は消えない
    q = DailyQuota(2, tmp_path / "quota.json", clock=clock)
    assert q.used(user) == 1


def test_voice_start_counts_against_the_daily_quota(tmp_path, monkeypatch):
    """音声相談を始める(接続トークンの発行)も実 API を使うので1ターン扱い: 上限に達したら発行せずに案内だけ返す。"""
    server, model = _server(tmp_path, monkeypatch, "1")
    for k, v in LIVEKIT.items():
        monkeypatch.setenv(k, v)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        client.get("/dev")
        user = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        r = client.post("/api/voice/start", json={"user_id": user})
        assert r.status_code == 200 and r.json()["token"]  # 1回目は発行(1ターン消費)
        assert server.app.state.quota.used(user) == 1
        server.app.state.voice_rooms.release(user, r.json()["room"])  # 面談が終わった(同時に開けるのは 1 部屋まで)
        r = client.post("/api/voice/start", json={"user_id": user})
        assert r.status_code == 429 and r.json()["detail"] == LIMIT_MESSAGE
        # テキストの会話も同じ枠なので、もう使えない
        assert client.post("/api/event", json={"user_id": user, "event": "message", "text": "hi"}).json()["limited"]
        assert model.calls == 0


def test_limit_comes_from_env_with_a_default(monkeypatch):
    monkeypatch.delenv("LINE_SIM_DAILY_TURN_LIMIT", raising=False)
    assert daily_limit() == DEFAULT_LIMIT > 0
    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "7")
    assert daily_limit() == 7


def test_zero_limit_means_unlimited_and_the_count_is_per_person():
    q = DailyQuota(0)
    assert all(q.take("u") for _ in range(100))
    q = DailyQuota(1)
    assert q.take("w-1:a") and not q.take("w-1:b") and q.take("w-2")


def test_empty_message_is_rejected_before_counting(tmp_path, monkeypatch):
    server, model = _server(tmp_path, monkeypatch, "1")
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        client.get("/")
        u = client.get("/api/me").json()["user_id"]
        assert client.post("/api/event", json={"user_id": u, "event": "message", "text": " "}).status_code == 400
        assert client.post("/api/event", json={"user_id": u, "event": "message", "text": "hi"}).json()["bubbles"] == [
            "はい、どうぞ。"]
        assert model.calls == 1


def _new_browser(client) -> str:
    """Cookie を作り直したブラウザ(GET / を開かず、正しい形式の匿名IDを自分で作る。スクリプトで回すのと同じ)。"""
    uid = f"w-{secrets.token_hex(8)}"
    client.cookies.clear()
    client.cookies.set("aica_uid", uid)
    return uid


def _say(client, uid, headers=None) -> dict:
    r = client.post("/api/event", json={"user_id": uid, "event": "message", "text": "hi"}, headers=headers)
    assert r.status_code == 200
    return r.json()


def test_rotating_cookies_from_one_ip_stops_at_the_ip_limit(tmp_path, monkeypatch):
    """Cookie を毎回作り直しても、同じ IP からのターンの合計で止まり、それ以上 LLM を呼ばない。"""
    server, model = _server(tmp_path, monkeypatch, "2", AICA_DAILY_TURN_LIMIT_PER_IP="5", **LIVEKIT)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        for _ in range(5):
            assert _say(client, _new_browser(client))["bubbles"] == ["はい、どうぞ。"]
        assert model.calls == 5
        # 6人目(新しい Cookie)からは、どの経路でも LLM を呼ばずに IP の上限の案内
        uid = _new_browser(client)
        r = _say(client, uid)
        assert r["limited"] and r["bubbles"] == [IP_LIMIT_MESSAGE]
        assert _stream(client, uid, "message", "hi")[-1]["bubbles"] == [IP_LIMIT_MESSAGE]
        assert client.post("/api/event", json={"user_id": uid, "event": "form_exit"}).json()["sent"] is False
        r = client.post("/api/voice/start", json={"user_id": uid})
        assert r.status_code == 429 and r.json()["detail"] == IP_LIMIT_MESSAGE
        assert model.calls == 5
        # 止めたターンは、人・IP・全体のどれにも数えない
        quota = server.app.state.quota
        assert quota.used(uid) == 0 and quota.ip_used("testclient") == 5 and quota.total_used() == 5


def test_new_ids_from_one_ip_are_capped_and_do_not_grow_the_state_file(tmp_path, monkeypatch):
    """1つの IP から1日に使える匿名IDの数を超えると、挨拶も返さず、状態ファイルにユーザーが増えない。"""
    server, model = _server(tmp_path, monkeypatch, None, AICA_DAILY_NEW_IDS_PER_IP="2")
    from fastapi.testclient import TestClient

    def users() -> set[str]:
        return set(json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["users"])

    def open_and_greet(client) -> list[dict]:
        opened = client.post("/api/talk/open").json()
        r = client.post("/api/event/stream", json={"user_id": opened["user"], "event": opened["event"],
                                                   "thread": opened["thread"]})
        return [json.loads(x) for x in r.text.splitlines()]

    with TestClient(server.app) as client:
        first, second = _new_browser(client), None
        assert open_and_greet(client)[-1]["bubbles"] == ["はい、どうぞ。"]
        second = _new_browser(client)
        assert open_and_greet(client)[-1]["bubbles"] == ["はい、どうぞ。"]
        before = users()
        assert {first, second} <= before
        third = _new_browser(client)
        last = open_and_greet(client)[-1]
        assert last["limited"] and last["bubbles"] == [IP_LIMIT_MESSAGE]
        assert users() == before and third not in users() and model.calls == 2
        assert third not in json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))["counts"]
        # ターンを使わずに状態へ書く API(シナリオの開始・プロフィールの書き換え)も、新しい匿名IDでは書かない
        for _ in range(3):
            _new_browser(client)
            for r in (client.post("/api/scenarios/F-follow-new/start"),
                      client.put("/api/profile/track", json={"track": "正社員"}),
                      client.put("/api/profile/items/consultation_summary", json={"value": "飲食で探している"}),
                      client.delete("/api/profile/saved/J-001")):
                assert r.status_code == 429 and r.json()["detail"] == IP_LIMIT_MESSAGE
        assert users() == before and model.calls == 2
        # すでに使った匿名IDは、同じ IP から続けて使える(ターンも、シナリオの開始・プロフィールの書き換えも)
        client.cookies.clear()
        client.cookies.set("aica_uid", first)
        assert _say(client, first)["bubbles"] == ["はい、どうぞ。"]
        assert client.post("/api/scenarios/F-follow-new/start").status_code == 200
        assert client.put("/api/profile/track", json={"track": "正社員"}).status_code == 200


def test_admit_registers_a_new_id_without_counting_a_turn():
    """admit は IP の匿名IDの数だけを確かめ、空きがあれば0ターンで加える(ターンの上限は見ない・数えない)。"""
    q = DailyQuota(1, ip_ids=2, ip_limit=1, total_limit=1)
    assert q.take("w-a", "1.1.1.1")  # 人・IP・全体のターンの上限はすべて使い切った
    assert q.admit("w-a", "1.1.1.1") is None and q.admit("w-b:sim", "1.1.1.1") is None
    assert q.admit("w-b", "1.1.1.1") is None  # 同じ人(シナリオユーザーは匿名IDにまとめる)
    assert q.admit("w-c", "1.1.1.1") == IP_LIMIT_MESSAGE
    assert q.admit("w-c", "2.2.2.2") is None  # 別の IP には空きがある
    assert q.used("w-b") == 0 and q.ip_used("1.1.1.1") == 1 and q.total_used() == 1
    assert DailyQuota(1).admit("w-z", "1.1.1.1") is None  # 匿名IDの数の上限なし(0)


def test_turns_from_many_ips_stop_at_the_total_limit(tmp_path, monkeypatch):
    """別々の IP(プロキシの後ろ)からの合計が、全体の上限で止まる。案内は全体の上限と分かる文。"""
    server, model = _server(tmp_path, monkeypatch, None, AICA_DAILY_TURN_LIMIT_TOTAL="3", AICA_TRUST_PROXY="1")
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        for i in range(3):
            assert _say(client, _new_browser(client), {"X-Forwarded-For": f"203.0.113.{i}"})["bubbles"] == [
                "はい、どうぞ。"]
        r = _say(client, _new_browser(client), {"X-Forwarded-For": "203.0.113.99"})
        assert r["limited"] and r["bubbles"] == [TOTAL_LIMIT_MESSAGE] and "本日はたくさんのご利用があり" in r["bubbles"][0]
        assert model.calls == 3 and server.app.state.quota.ip_used("203.0.113.99") == 0


def test_spoofed_forwarded_for_is_ignored_unless_the_proxy_is_trusted(tmp_path, monkeypatch):
    """AICA_TRUST_PROXY が無ければ X-Forwarded-For を見ない(書き換えても IP の上限を迂回できない)。
    あれば最後の値(プロキシが付けた値)で数え、本人が先頭に書いた値は使わない。"""
    server, model = _server(tmp_path, monkeypatch, None, AICA_DAILY_TURN_LIMIT_PER_IP="2")
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        for i in range(2):
            _say(client, _new_browser(client), {"X-Forwarded-For": f"198.51.100.{i}"})
        r = _say(client, _new_browser(client), {"X-Forwarded-For": "198.51.100.7"})
        assert r["bubbles"] == [IP_LIMIT_MESSAGE] and model.calls == 2
        assert server.app.state.quota.ip_used("testclient") == 2

    server, model = _server(tmp_path / "proxied", monkeypatch, None, AICA_DAILY_TURN_LIMIT_PER_IP="2",
                            AICA_TRUST_PROXY="1")
    with TestClient(server.app) as client:
        quota = server.app.state.quota
        for i in range(2):  # 先頭を毎回書き換えても、プロキシが付けた最後の値で数える
            _say(client, _new_browser(client), {"X-Forwarded-For": f"10.0.0.{i}, 192.0.2.1"})
        assert quota.ip_used("192.0.2.1") == 2 and quota.ip_used("10.0.0.0") == 0
        assert _say(client, _new_browser(client), {"X-Forwarded-For": "10.0.0.9, 192.0.2.1"})["limited"]
        # 別の IP(プロキシが付けた値が違う)は使える
        assert _say(client, _new_browser(client), {"X-Forwarded-For": "192.0.2.2"})["bubbles"] == ["はい、どうぞ。"]
        assert model.calls == 3 and quota.ip_used("testclient") == 0


def test_ip_and_total_limits_come_from_env_and_zero_means_unlimited(monkeypatch):
    for k in QUOTA_ENVS:
        monkeypatch.delenv(k, raising=False)
    assert (ip_limit(), ip_ids_limit(), total_limit()) == (DEFAULT_IP_LIMIT, DEFAULT_IP_IDS, DEFAULT_TOTAL_LIMIT)
    assert (DEFAULT_IP_LIMIT, DEFAULT_IP_IDS, DEFAULT_TOTAL_LIMIT) == (150, 20, 1000)
    monkeypatch.setenv("AICA_DAILY_TURN_LIMIT_PER_IP", "7")
    monkeypatch.setenv("AICA_DAILY_NEW_IDS_PER_IP", "3")
    monkeypatch.setenv("AICA_DAILY_TURN_LIMIT_TOTAL", "0")
    assert (ip_limit(), ip_ids_limit(), total_limit()) == (7, 3, 0)
    assert total_ids_limit() == DEFAULT_TOTAL_IDS == 300
    monkeypatch.setenv("AICA_DAILY_NEW_IDS_TOTAL", "-1")
    assert total_ids_limit() == -1

    q = DailyQuota(0, ip_limit=0, ip_ids=0, total_limit=-1)
    assert all(q.take(f"w-{i}", "1.1.1.1") for i in range(200))
    q = DailyQuota(0, ip_limit=2, ip_ids=0, total_limit=0)
    assert q.take("w-1", "a") and q.take("w-2", "a") and not q.take("w-3", "a") and q.take("w-3", "b")
    assert q.take("w-4")  # IP を渡さない呼び出し(評価など)は IP の上限を見ない


def test_a_blocked_turn_counts_nowhere_and_refund_returns_all_three():
    q = DailyQuota(1, ip_limit=3, ip_ids=2, total_limit=4)
    assert q.try_take("w-1:sim-new", "a") is None
    assert q.try_take("w-1:sim-exit", "a") == LIMIT_MESSAGE  # 匿名IDの上限: IP にも全体にも数えない
    assert (q.used("w-1"), q.ip_used("a"), q.total_used()) == (1, 1, 1)
    assert q.try_take("w-2", "a") is None
    assert q.try_take("w-3", "a") == IP_LIMIT_MESSAGE  # この IP から3つ目の匿名ID
    assert (q.used("w-3"), q.ip_used("a"), q.total_used()) == (0, 2, 2)
    q.refund("w-2", "a")  # 返すと、人・IP・全体のすべてが戻る
    assert (q.used("w-2"), q.ip_used("a"), q.total_used()) == (0, 1, 1)
    assert q.try_take("w-3", "a") == IP_LIMIT_MESSAGE  # 使われた匿名IDとしては残る(状態に書かれたかもしれない)
    assert q.try_take("w-2", "a") is None and q.try_take("w-4", "b") is None and q.try_take("w-5", "c") is None
    assert q.try_take("w-6", "d") == TOTAL_LIMIT_MESSAGE and q.total_used() == 4


def test_ip_counts_are_saved_and_reset_on_the_next_day(tmp_path):
    clock = Clock(datetime(2026, 9, 27, 23, 50, tzinfo=JST))
    path = tmp_path / "quota.json"
    q = DailyQuota(0, path, clock=clock, ip_limit=1)
    assert q.take("w-1", "a") and not q.take("w-2", "a")
    assert not DailyQuota(0, path, clock=clock, ip_limit=1).take("w-3", "a")  # 再起動しても当日の分は残る
    clock.now += timedelta(minutes=15)  # 日本時間の日付が変わると数え直す(IP も消える)
    assert q.take("w-2", "a") and "w-1" not in json.loads(path.read_text(encoding="utf-8"))["ips"]["a"]


def test_ipv6_is_counted_per_64_and_ipv4_per_address():
    """IPv6 は /64 にまとめる。IPv4 と IPv4 射影アドレスは1アドレス(射影アドレスは元の IPv4 と同じ)。"""
    assert ip_key("2001:db8:1:2::1") == ip_key("2001:db8:1:2:ffff:ffff:ffff:ffff") == "2001:db8:1:2::/64"
    assert ip_key("2001:DB8:1:2:0:0:0:abcd") == "2001:db8:1:2::/64"  # 書き方が違っても同じ鍵
    assert ip_key("2001:db8:1:3::1") == "2001:db8:1:3::/64"
    assert ip_key("203.0.113.5") == "203.0.113.5" and ip_key("::ffff:203.0.113.5") == "203.0.113.5"
    assert ip_key("::ffff:203.0.113.6") == "203.0.113.6"  # 射影アドレスを /64 にまとめない(IPv4 全体が1つにならない)
    assert ip_key("testclient") == "testclient" and ip_key("unknown") == "unknown"  # IP でない値はそのまま

    q = DailyQuota(0, ip_limit=3, ip_ids=4)
    for i in range(3):  # 同じ /64 の中で送信元を替えても、ターンは1つの IP として数える
        assert q.try_take(f"w-{i}", f"2001:db8:1:2::{i + 1:x}") is None
    assert q.try_take("w-0", "2001:db8:1:2:aaaa::1") == IP_LIMIT_MESSAGE
    assert q.ip_used("2001:db8:1:2::99") == 3 and q.available("w-9", "2001:db8:1:3::1")  # 別の /64 は別に数える
    assert q.admit("w-3", "2001:db8:1:2::50") is None  # 匿名IDの数も /64 で共有: 4つ目までは入る
    assert q.admit("w-4", "2001:db8:1:2:1::1") == IP_LIMIT_MESSAGE
    assert q.admit("w-4", "2001:db8:1:3::1") is None
    q.refund("w-0", "2001:db8:1:2::ffff")  # 返すのも /64 で
    assert q.ip_used("2001:db8:1:2::1") == 2

    q = DailyQuota(0, ip_limit=2)
    assert q.take("w-1", "203.0.113.5") and q.take("w-2", "::ffff:203.0.113.5")
    assert not q.take("w-3", "203.0.113.5") and q.take("w-3", "203.0.113.6") and q.take("w-4", "::ffff:203.0.113.7")


def _greet(client, headers) -> dict:
    """本番のトーク画面を開いて挨拶まで(状態ファイルにユーザーを書くのは挨拶が返ったとき)。最後の行を返す。"""
    opened = client.post("/api/talk/open", headers=headers).json()
    r = client.post("/api/event/stream", json={"user_id": opened["user"], "event": opened["event"],
                                               "thread": opened["thread"]}, headers=headers)
    return [json.loads(x) for x in r.text.splitlines()][-1]


def test_rotating_ipv6_addresses_in_one_64_share_the_ip_limits(tmp_path, monkeypatch):
    """同じ /64 の中で IPv6 アドレスと Cookie を替えても、IP のターンと匿名IDの上限は共有される。別の /64 は別。"""
    server, model = _server(tmp_path, monkeypatch, None, AICA_DAILY_TURN_LIMIT_PER_IP="2",
                            AICA_DAILY_NEW_IDS_PER_IP="3", AICA_TRUST_PROXY="1")
    from fastapi.testclient import TestClient

    def via(ip: str) -> dict:
        return {"X-Forwarded-For": ip}

    with TestClient(server.app) as client:
        for i in range(2):
            assert _say(client, _new_browser(client), via(f"2001:db8:1:2::{i + 1}"))["bubbles"] == ["はい、どうぞ。"]
        r = _say(client, _new_browser(client), via("2001:db8:1:2:dead:beef::7"))
        assert r["limited"] and r["bubbles"] == [IP_LIMIT_MESSAGE] and model.calls == 2
        assert _say(client, _new_browser(client), via("2001:db8:1:3::1"))["bubbles"] == ["はい、どうぞ。"]
        quota = server.app.state.quota
        assert quota.ip_used("2001:db8:1:2::ffff") == 2 and quota.ip_used("2001:db8:1:3::") == 1
        # 匿名IDの数も /64 で共有(止めたターンの匿名IDは数えないので、使われたのは2つ): 3つ目まではシナリオを始められる
        _new_browser(client)
        assert client.post("/api/scenarios/F-follow-new/start", headers=via("2001:db8:1:2::abcd")).status_code == 200
        _new_browser(client)
        r = client.post("/api/scenarios/F-follow-new/start", headers=via("2001:db8:1:2:1:2:3:4"))
        assert r.status_code == 429 and r.json()["detail"] == IP_LIMIT_MESSAGE
        assert client.post("/api/scenarios/F-follow-new/start", headers=via("2001:db8:1:4::1")).status_code == 200
        assert model.calls == 3


def test_new_ids_are_capped_in_total_even_from_many_ips(tmp_path, monkeypatch):
    """全体の匿名IDの数を超えると、別々の IP(別々の /64)からでも状態ファイルにユーザーが増えない。
    その日にすでに使った匿名IDは続けて使える。"""
    server, model = _server(tmp_path, monkeypatch, None, AICA_DAILY_NEW_IDS_TOTAL="2", AICA_TRUST_PROXY="1")
    from fastapi.testclient import TestClient

    def users() -> set[str]:
        return set(json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["users"])

    with TestClient(server.app) as client:
        first = _new_browser(client)
        assert _greet(client, {"X-Forwarded-For": "203.0.113.1"})["bubbles"] == ["はい、どうぞ。"]
        _new_browser(client)
        assert _greet(client, {"X-Forwarded-For": "2001:db8:1::1"})["bubbles"] == ["はい、どうぞ。"]
        before = users()
        for i in range(3):  # 3つ目からは、IP をいくつ替えても挨拶も返さず、状態にも書かない
            uid = _new_browser(client)
            headers = {"X-Forwarded-For": f"2001:db8:{i + 2}::1"}
            last = _greet(client, headers)
            assert last["limited"] and last["bubbles"] == [TOTAL_LIMIT_MESSAGE]
            for r in (client.post("/api/scenarios/F-follow-new/start", headers=headers),
                      client.put("/api/profile/track", json={"track": "正社員"}, headers=headers),
                      client.delete("/api/profile/saved/J-001", headers=headers)):
                assert r.status_code == 429 and r.json()["detail"] == TOTAL_LIMIT_MESSAGE
            assert uid not in users()
        assert users() == before and model.calls == 2
        quota = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
        assert len(set(quota["counts"]).union(*quota["ips"].values())) == 2
        # すでに使った匿名IDは、別の IP からでも続けて使える
        client.cookies.clear()
        client.cookies.set("aica_uid", first)
        assert _say(client, first, {"X-Forwarded-For": "198.51.100.1"})["bubbles"] == ["はい、どうぞ。"]
        assert client.post("/api/scenarios/F-follow-new/start", headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 200
        assert model.calls == 3


def test_total_new_ids_limit_on_the_quota():
    q = DailyQuota(0, total_ids=2)
    assert q.admit("w-1:sim", "1.1.1.1") is None and q.try_take("w-2", "2.2.2.2") is None
    assert q.admit("w-3", "3.3.3.3") == TOTAL_LIMIT_MESSAGE and q.try_take("w-3", "4.4.4.4") == TOTAL_LIMIT_MESSAGE
    assert q.try_take("w-3") == TOTAL_LIMIT_MESSAGE and not q.available("w-3", "5.5.5.5")  # IP を渡さなくても
    assert q.try_take("w-1", "9.9.9.9") is None and q.admit("w-2", "8.8.8.8") is None  # 使った匿名IDは別の IP からも
    q = DailyQuota(0, ip_ids=1, total_ids=1)
    assert q.admit("w-1", "a") is None and q.admit("w-2", "a") == IP_LIMIT_MESSAGE  # IP の上限を先に確かめる
    assert q.admit("w-2", "b") == TOTAL_LIMIT_MESSAGE
    assert all(DailyQuota(0, total_ids=0).take(f"w-{i}", f"10.0.0.{i}") for i in range(50))  # 0 は上限なし


def test_costs_summarized_per_user_per_day_in_one_command(tmp_path, capsys):
    rows = [
        # 日本時間の日付で区切る(UTC 15:30 は翌日 00:30)
        {"at": "2026-09-27T01:00:00+00:00", "user_id": "w-aaaaaaaaaaaaaaaa:sim-new", "input_tokens": 100,
         "output_tokens": 10, "cost_usd": 0.001},
        {"at": "2026-09-27T02:00:00+00:00", "user_id": "w-aaaaaaaaaaaaaaaa:sim-exit", "input_tokens": 200,
         "output_tokens": 20, "cost_usd": 0.002},
        {"at": "2026-09-27T15:30:00+00:00", "user_id": "w-aaaaaaaaaaaaaaaa:sim-new", "input_tokens": 50,
         "output_tokens": 5, "cost_usd": 0.0005},
        {"at": "2026-09-27T03:00:00+00:00", "user_id": "sim-new", "input_tokens": 1, "output_tokens": 1,
         "cost_usd": 0.0001},
    ]
    path = tmp_path / "costs.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    ledger_mod.main(["--path", str(path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert [(s["date"], s["user_id"], s["calls"], s["cost_usd"]) for s in out] == [
        ("2026-09-27", "sim-new", 1, 0.0001),
        ("2026-09-27", "w-aaaaaaaaaaaaaaaa", 2, 0.003),
        ("2026-09-28", "w-aaaaaaaaaaaaaaaa", 1, 0.0005),
    ]
    assert out[1]["input_tokens"] == 300 and out[1]["output_tokens"] == 30

    ledger_mod.main(["--path", str(path), "--by-user-id", "--date", "2026-09-27", "--json"])
    assert [s["user_id"] for s in json.loads(capsys.readouterr().out)] == [
        "sim-new", "w-aaaaaaaaaaaaaaaa:sim-exit", "w-aaaaaaaaaaaaaaaa:sim-new"]

    ledger_mod.main(["--path", str(path)])  # 表でも出る(合計行つき)
    table = capsys.readouterr().out.splitlines()
    assert table[0].split()[:2] == ["date", "user_id"] and table[-1].startswith("total") and "0.0036" in table[-1]
