"""LINE のアカウント連携(task 182)。実 LINE も実 LLM も呼ばない: linkToken の発行と accountLink の画面と webhook は偽物の LINE
(line_runtime.fake_line)、LLM は決められた順に返す偽のモデル、dip の MCP は本物。dip のログインの画面は本物の HTTP の口を通す。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from dip_mcp import accounts, behavior, logic
from dip_mcp.data import State
from evals.scenarios import load_scenarios, prepare
from line_runtime import line_channel
from line_runtime.line_channel import ACCOUNT_LINK_URL, line_user
from tests.test_handle import call, reply
from tests.test_line_webhook import (
    RAW_OTHER,
    RAW_USER,
    FakeLine,
    _event,
    _post,
    _server,
    line_id_key,  # noqa: F401  LINE のユーザーIDを作る鍵(autouse の fixture)
)
from tests.test_revisit import Greeter, _tool_result

EXIT = ("exit@dip.test", "demo-pass-1002")  # fixtures の会員 D1002(シナリオ B: 応募フォームの途中で離脱)
_ids = iter(range(10_000))


def _say(client, text: str, user: str = RAW_USER):
    n = next(_ids)
    return _post(client, [_event("message", user=user, text=text, event_id=f"01LINK{n}", token=f"reply-token-{n}")])


def _last_messages(fake: FakeLine) -> list[dict]:
    return fake.sent()[-1][1]["messages"]


def _start(client, fake: FakeLine, user: str = RAW_USER) -> str:
    """本人が「アカウント連携」と送る: bot が linkToken をもらい、dip のログインの画面へのボタンを返す。その linkToken。"""
    _say(client, line_channel.LINK_TEXT, user)
    text, flex = _last_messages(fake)
    assert text["text"] == line_channel.LINK_INTRO
    uri = flex["contents"]["body"]["contents"][0]["action"]["uri"]
    assert uri.startswith("https://aica.example.test/dip/login?linkToken=")
    return parse_qs(urlparse(uri).query)["linkToken"][0]


def _login(client, link_token: str, login_id: str = EXIT[0], password: str = EXIT[1]):
    return client.post("/dip/login", data={"linkToken": link_token, "login_id": login_id, "password": password},
                       follow_redirects=False)


def _nonce(resp) -> str:
    """dip がログインの後に本人を送る、LINE の accountLink の画面の URL の nonce。"""
    assert resp.status_code == 303
    url = urlparse(resp.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == ACCOUNT_LINK_URL
    return parse_qs(url.query)["nonce"][0]


def _webhook(client, event: dict):
    """LINE のサーバーとして、署名を付けて accountLink の webhook を送る。"""
    return _post(client, [event])


def _link(client, fake: FakeLine, user: str = RAW_USER) -> dict:
    """最初から最後まで: linkToken → dip のログイン → nonce → 本人が LINE の画面を開く → accountLink の webhook。"""
    token = _start(client, fake, user)
    nonce = _nonce(_login(client, token))
    event = fake.platform.open_account_link(token, nonce, opener=user)
    assert _webhook(client, event).status_code == 200
    return event


def _summary_reader(uid: str, heard: list | None = None) -> list:
    """dip の get_user_conversation_summary を呼び、返った状況(連携しているか、要約の時点、行動の傾向のエリア)をそのまま
    答える偽のモデル。LINE のユーザーは実際の時計なので、日数で決まる segment ではなく、要約の中身そのものを見る。
    heard を渡すと、そのターンまでにエージェントが受けた本人側の文(知らせを含む)を足す。"""
    def answer(messages):
        if heard is not None:
            heard.extend(m.content for m in messages if m.type == "human")
        s = _tool_result(messages, "get_user_conversation_summary")
        area = ",".join((s.get("tendency") or {}).get("area") or [])
        return reply([f"{'unlinked' if s['segment'] == 'unlinked' else 'linked'} {s.get('as_of')} {area}".strip()])

    return [call("get_user_conversation_summary", {"line_user_id": uid}, "sum"), answer]


# ----------------------------- 最初から最後まで -----------------------------


def test_line_user_links_end_to_end_and_tools_then_read_the_members_summary(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    heard = []
    model = Greeter(script=[*_summary_reader(uid), *_summary_reader(uid, heard)])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _say(client, "いまの状況は?")  # 連携の前: 未連携の新規
        assert _last_messages(fake)[0]["text"] == "unlinked None"

        token = _start(client, fake)
        assert any(p == f"/v2/bot/user/{RAW_USER}/linkToken" for p, _, _ in fake.calls)  # bot が LINE に linkToken を頼んだ
        page = client.get("/dip/login", params={"linkToken": token})
        assert page.status_code == 200 and "パスワード" in page.text
        nonce = _nonce(_login(client, token))
        assert len(nonce) >= 43 and nonce != _nonce(_login(client, token))  # 256 ビットの乱数。毎回ちがう
        event = fake.platform.open_account_link(token, nonce, opener=RAW_USER)
        assert event["link"] == {"result": "ok", "nonce": nonce} and event["source"]["userId"] == RAW_USER
        assert _webhook(client, event).status_code == 200
        assert _last_messages(fake)[0]["text"] == line_channel.LINKED_TEXT
        assert behavior.dip_id(server.state, uid) == "D1002"

        _say(client, "いまの状況は?")  # 連携の後: dip のツールがその会員(D1002)の要約(中野の深夜のコンビニを閲覧)を読む
        assert _last_messages(fake)[0]["text"] == "linked 2026-09-23T12:00:00+09:00 中野"
    assert model.script == []
    # 会話の途中で連携が変わったことは、次のターンの前にエージェントへ知らせる(前に読んだ「未連携」を使わないように)
    assert any("dip の会員との連携をつないだ" in h and "get_user_conversation_summary を読み直す" in h for h in heard)

    # LINE の userId、nonce、linkToken、パスワードは、どのファイルにもそのままでは残らない
    for p in (p for p in tmp_path.rglob("*") if p.is_file()):
        raw = p.read_bytes()
        for secret in (RAW_USER, nonce, token, EXIT[1]):
            assert secret.encode() not in raw, (p.name, secret)


def test_asking_to_link_when_already_linked_or_without_a_public_url_does_not_issue_a_link_token(tmp_path, monkeypatch):
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))
    with TestClient(server.app) as client:
        _link(client, fake)
        before = len(fake.calls)
        _say(client, line_channel.LINK_TEXT)
        assert _last_messages(fake)[0]["text"] == line_channel.ALREADY_LINKED_TEXT
        _say(client, line_channel.UNLINK_TEXT)
        monkeypatch.delenv("AICA_PUBLIC_URL")
        _say(client, " アカウント連携 ")
        assert _last_messages(fake)[0]["text"] == line_channel.LINK_UNAVAILABLE_TEXT
        assert not any(p.endswith("/linkToken") for p, _, _ in fake.calls[before:])


# ----------------------------- 連携しないもの -----------------------------


def test_a_used_nonce_does_not_link_again(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))
    with TestClient(server.app) as client:
        event = _link(client, fake)
        assert behavior.dip_id(server.state, uid) == "D1002"
        _say(client, line_channel.UNLINK_TEXT)
        replay = event | {"webhookEventId": "01REPLAY", "replyToken": "reply-token-replay"}
        _webhook(client, replay)
        assert _last_messages(fake)[0]["text"] == line_channel.LINK_FAILED_TEXT
    assert behavior.dip_id(server.state, uid) is None


def test_an_expired_nonce_does_not_link(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))
    with TestClient(server.app) as client:
        token = _start(client, fake)
        nonce = _nonce(_login(client, token))
        event = fake.platform.open_account_link(token, nonce, opener=RAW_USER)
        later = datetime.now(UTC) + accounts.NONCE_TTL + timedelta(seconds=1)  # webhook が nonce の期限の後に届いた
        monkeypatch.setattr(accounts, "clock", lambda: later)
        _webhook(client, event)
        assert _last_messages(fake)[0]["text"] == line_channel.LINK_FAILED_TEXT
    assert behavior.dip_id(server.state, uid) is None


def test_a_failed_link_result_does_not_link_and_burns_the_nonce(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))
    with TestClient(server.app) as client:
        token = _start(client, fake)
        nonce = _nonce(_login(client, token))
        failed = fake.platform.open_account_link(token, nonce, opener=RAW_USER) | {"link": {"result": "failed",
                                                                                            "nonce": nonce}}
        _webhook(client, failed)
        assert _last_messages(fake)[0]["text"] == line_channel.LINK_FAILED_TEXT
        _webhook(client, failed | {"webhookEventId": "01OK", "link": {"result": "ok", "nonce": nonce}})
    assert behavior.dip_id(server.state, uid) is None


def test_unsigned_or_wrongly_signed_account_link_webhooks_do_not_link(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))
    with TestClient(server.app) as client:
        token = _start(client, fake)
        event = fake.platform.open_account_link(token, _nonce(_login(client, token)), opener=RAW_USER)
        assert _post(client, [event], signature=None).status_code == 400
        assert _post(client, [event], signature="bm90LXRoZS1zaWduYXR1cmU=").status_code == 400
    assert behavior.dip_id(server.state, uid) is None
    assert not [b for _, b in fake.sent() if b["messages"][0]["text"] == line_channel.LINKED_TEXT]


def test_a_nonce_made_for_another_users_link_token_does_not_link(tmp_path, monkeypatch):
    """A の linkToken でログインして作った nonce を、B の linkToken の accountLink の画面に付け替えても、B は連携されない
    (nonce は作ったときの linkToken に結んである。その linkToken を bot が出したのは A)。"""
    a, b = line_user(RAW_USER), line_user(RAW_OTHER)
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))
    with TestClient(server.app) as client:
        token_a, token_b = _start(client, fake, RAW_USER), _start(client, fake, RAW_OTHER)
        nonce_a = _nonce(_login(client, token_a))
        spliced = fake.platform.open_account_link(token_b, nonce_a, opener=RAW_OTHER)
        assert spliced["link"]["result"] == "ok"  # LINE から見れば B の正しい linkToken
        _webhook(client, spliced)
        assert _last_messages(fake)[0]["text"] == line_channel.LINK_FAILED_TEXT
    assert behavior.dip_id(server.state, b) is None and behavior.dip_id(server.state, a) is None


def test_the_fake_line_fails_a_link_opened_by_someone_else_or_after_the_link_token_expired(tmp_path, monkeypatch):
    """偽物の LINE も本物と同じく、linkToken のユーザーでない人が開いた・期限切れ・使い終わった linkToken は failed にする。
    知らない linkToken には webhook を送らない。"""
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))
    with TestClient(server.app) as client:
        token = _start(client, fake)
        nonce = _nonce(_login(client, token))
        assert fake.platform.open_account_link(token, nonce, opener=RAW_OTHER)["link"]["result"] == "failed"
        assert fake.platform.open_account_link(token, nonce, opener=RAW_USER)["link"]["result"] == "failed"  # 使い終わり
        token2 = _start(client, fake)
        fake.platform.clock = lambda: datetime.now(UTC) + timedelta(minutes=11)
        assert fake.platform.open_account_link(token2, nonce, opener=RAW_USER)["link"]["result"] == "failed"
        assert fake.platform.open_account_link("unknown-token", nonce, opener=RAW_USER) is None


# ----------------------------- dip のログイン -----------------------------


def test_login_failures_are_limited_and_do_not_tell_which_part_was_wrong(tmp_path, monkeypatch):
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))
    with TestClient(server.app) as client:
        token = _start(client, fake)
        assert _login(client, token, "nobody@dip.test", "x").status_code == 401
        for _ in range(accounts.MAX_FAILURES - 1):
            r = _login(client, token, password="wrong")
            assert r.status_code == 401 and "ログインID かパスワードがちがいます" in r.text
        assert _login(client, token, password="wrong").status_code == 429  # 上限: ロック
        assert _login(client, token).status_code == 429  # 正しいパスワードでも、ロックの間は入れない
        later = datetime.now(UTC) + accounts.LOCK + timedelta(seconds=1)
        monkeypatch.setattr(accounts, "clock", lambda: later)
        assert _login(client, token).status_code == 303
        assert client.get("/dip/login", params={"linkToken": "<script>"}).status_code == 400
        assert client.post("/dip/login", data={"linkToken": "", "login_id": EXIT[0], "password": EXIT[1]},
                           follow_redirects=False).status_code == 400


def test_a_successful_login_resets_the_failure_count(tmp_path):
    acc = accounts.of(State(tmp_path / "state.json"))
    for _ in range(accounts.MAX_FAILURES - 1):
        assert acc.login(EXIT[0], "wrong") == {"error": "bad"}
    assert acc.login(*EXIT) == {"dip_id": "D1002"}
    assert acc.login(EXIT[0], "wrong") == {"error": "bad"}  # 数え直したので、まだロックしない


def _slow_check(monkeypatch, slow) -> tuple[threading.Event, threading.Event, list[str]]:
    """slow(password) が真のパスワードは、release まで check_password を止める。entered は止まったら立つ。"""
    entered, release, calls = threading.Event(), threading.Event(), []
    real = accounts.check_password

    def check(password, stored):
        calls.append(password)
        if slow(password):
            entered.set()
            assert release.wait(10)
        return real(password, stored)

    monkeypatch.setattr(accounts, "check_password", check)
    return entered, release, calls


def test_a_right_password_checked_while_other_failures_lock_does_not_get_in_or_clear_the_lock(tmp_path, monkeypatch):
    """ハッシュの計算の間に、ほかの失敗でロックがかかったら、正しいパスワードも通さず、ロックも残す(同時のログイン)。"""
    acc = accounts.of(State(tmp_path / "state.json"))
    entered, release, _ = _slow_check(monkeypatch, lambda p: p == EXIT[1])
    with ThreadPoolExecutor(1) as pool:
        right = pool.submit(acc.login, *EXIT)
        assert entered.wait(10)  # 正しいパスワードは計算中(1 回分を数えてある)
        got = [acc.login(EXIT[0], "wrong")["error"] for _ in range(accounts.MAX_FAILURES - 1)]
        assert got == ["bad"] * (accounts.MAX_FAILURES - 2) + ["locked"]
        release.set()
        assert right.result(10) == {"error": "locked"}
    monkeypatch.setattr(accounts, "check_password", lambda *a: pytest.fail("ロックの間は計算しない"))
    assert acc.login(*EXIT) == {"error": "locked"}  # ロックは消えていない


def test_logins_sent_at_once_check_no_more_passwords_than_the_limit(tmp_path, monkeypatch):
    acc = accounts.of(State(tmp_path / "state.json"))
    _entered, release, calls = _slow_check(monkeypatch, lambda p: True)
    n = accounts.MAX_FAILURES * 2
    with ThreadPoolExecutor(n) as pool:
        futures = [pool.submit(acc.login, EXIT[0], f"wrong-{i}") for i in range(n)]
        deadline = time.monotonic() + 10
        while sum(f.done() for f in futures) < n - accounts.MAX_FAILURES and time.monotonic() < deadline:
            time.sleep(0.01)
        early = [f.result()["error"] for f in futures if f.done()]  # 計算中のものが上限に達した後の要求は、計算せずに locked
        assert len(calls) == accounts.MAX_FAILURES and early == ["locked"] * (n - accounts.MAX_FAILURES)
        release.set()
        results = [f.result(10)["error"] for f in futures]
    assert len(calls) == accounts.MAX_FAILURES  # 上限より多くは確かめない
    assert results == ["locked"] * n  # 計算中の分も数えてあるので、最初に終わった失敗でロックがかかる


def test_an_unknown_login_id_is_locked_the_same_way(tmp_path):
    """会員にいないログインID も同じ回数でロックする(ロックされるかどうかで、ID があるかを知られない)。"""
    acc = accounts.of(State(tmp_path / "state.json"))
    got = [acc.login("nobody@dip.test", "x")["error"] for _ in range(accounts.MAX_FAILURES + 1)]
    assert got == ["bad"] * (accounts.MAX_FAILURES - 1) + ["locked", "locked"]
    assert acc.login(EXIT[0], "wrong") == {"error": "bad"}  # ほかの ID は数えない


def test_a_link_token_the_bot_issued_too_long_ago_does_not_link(tmp_path, monkeypatch):
    """bot が覚えている linkToken にも期限がある(LINE の linkToken と同じ 10 分)。"""
    acc = accounts.of(State(tmp_path / "state.json"))
    acc.record_link_token("l-0123456789abcdef", "token-from-line")
    later = datetime.now(UTC) + accounts.LINK_TOKEN_TTL + timedelta(seconds=1)
    monkeypatch.setattr(accounts, "clock", lambda: later)
    nonce = acc.issue_nonce("D1002", "token-from-line")
    assert acc.complete_link("l-0123456789abcdef", nonce) == {"error": accounts.OTHER_TOKEN}
    assert acc.dip_id("l-0123456789abcdef") is None


# ----------------------------- 解除 -----------------------------


def test_unlinking_goes_back_to_the_unlinked_handling(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    model = Greeter(script=_summary_reader(uid))
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _link(client, fake)
        logic.save_job(server.state, uid, "b0101")  # 連携の間に保存した求人(会員の元データに書く)
        assert "b0101" in [j["job_id"] for j in logic.saved_jobs(server.state, uid)]

        _say(client, line_channel.UNLINK_TEXT)
        assert _last_messages(fake)[0]["text"] == line_channel.UNLINKED_TEXT
        assert behavior.dip_id(server.state, uid) is None
        _say(client, "いまの状況は?")
        assert _last_messages(fake)[0]["text"] == "unlinked None"
        assert logic.saved_jobs(server.state, uid) == []  # 会員の行動は重ねない
        assert logic.push_trigger(server.state, uid, "form_exit")["reason"] == "dip ID と未連携なので行動イベントがない"
        _say(client, line_channel.UNLINK_TEXT)
        assert _last_messages(fake)[0]["text"] == line_channel.NOT_LINKED_TEXT

        _link(client, fake)  # 同じ会員につなぎ直すと、連携の間に書いた分がまた重なる(元データにはあり、要約は次のバッチ)
        assert "b0101" in [j["job_id"] for j in logic.saved_jobs(server.state, uid)]
    assert model.script == []


def test_a_write_racing_an_unlink_is_not_overlaid_on_the_unlinked_user(tmp_path):
    """ツールが会員の元データに書いている間に解除されても、その 1 件は書いたときの会員のものなので、未連携の人には重ならない。"""
    state = State(tmp_path / "state.json")
    state.reset_user("w-0123456789abcdef:sim-exit", template="sim-exit")
    uid = "w-0123456789abcdef:sim-exit"
    unlink_midway = state.update

    def update(line_user_id, fn):  # record が会員を引いて元データに書いた後、dip_writes に足す前に解除される
        accounts.of(state).unlink(uid)
        return unlink_midway(line_user_id, fn)

    state.update = update
    logic.save_job(state, uid, "b0101")
    state.update = unlink_midway
    assert state.user(uid)[behavior.WRITES][-1]["dip_id"] == "D1002"
    assert logic.saved_jobs(state, uid) == []
    accounts.of(state).set_link(uid, "D1005")  # 別の会員につないでも重ならない
    assert "b0101" not in [j["job_id"] for j in logic.saved_jobs(state, uid)]


# ----------------------------- Web の匿名ユーザー(開発者画面から) -----------------------------


def test_web_anonymous_user_links_from_the_dev_page_through_the_fake_line(tmp_path, monkeypatch):
    server, _ = _server(tmp_path, monkeypatch, Greeter(script=[]), configured=False)
    with TestClient(server.app) as client:
        client.get("/")
        uid = client.cookies[server.ANON_COOKIE]
        assert client.get("/api/dev/link").json() == {"user_id": uid, "dip_id": None}
        url = client.post("/api/dev/link/start").json()["url"]
        assert client.get(url).status_code == 200
        token = parse_qs(urlparse(url).query)["linkToken"][0]
        r = _login(client, token)
        assert r.status_code == 303 and r.headers["location"].startswith(server.FAKE_ACCOUNT_LINK_PATH + "?")

        other = TestClient(server.app)  # 別のブラウザが accountLink の画面を開いても連携されない(linkToken は使い終わる)
        other.get("/")
        page = other.get(r.headers["location"])
        assert line_channel.LINK_FAILED_TEXT in page.text
        assert client.get(r.headers["location"]).status_code == 200
        assert behavior.dip_id(server.state, uid) is None

        url = client.post("/api/dev/link/start").json()["url"]  # 本人がやり直す
        token = parse_qs(urlparse(url).query)["linkToken"][0]
        page = client.get(_login(client, token).headers["location"])
        assert line_channel.LINKED_TEXT in page.text
        assert client.get("/api/dev/link").json()["dip_id"] == "D1002"
        assert client.post("/api/dip", json={"user_id": uid}).json()["summary"]["tendency"]["area"] == ["中野"]
        assert client.post("/api/dev/link/start").status_code == 409

        assert client.post("/api/dev/link/unlink").json() == {"unlinked": "D1002"}
        assert client.post("/api/dip", json={"user_id": uid}).json()["summary"]["segment"] == "unlinked"
        assert TestClient(server.app).post("/api/dev/link/start").status_code == 401  # Cookie の無いブラウザ


def test_forgetting_everything_also_unlinks(tmp_path, monkeypatch):
    server, _ = _server(tmp_path, monkeypatch, Greeter(script=[]), configured=False)
    with TestClient(server.app) as client:
        client.get("/")
        uid = client.cookies[server.ANON_COOKIE]
        accounts.of(server.state).set_link(uid, "D1002")
        assert client.post("/api/profile/forget").status_code == 200
        assert behavior.dip_id(server.state, uid) is None


# ----------------------------- シナリオのユーザーは最初から連携済み -----------------------------


def test_scenario_users_start_linked_and_reset_brings_the_links_back(tmp_path):
    state = State(tmp_path / "state.json")
    state.reset()
    want = {"sim-yoru": "D1001", "sim-exit": "D1002", "sim-applied": "D1004", "sim-browsing": "D1005", "sim-new": None}
    assert {u: behavior.dip_id(state, u) for u in want} == want
    assert all("dip_id" not in state.user(u) for u in want)  # 引くのは連携の表だけ
    for sc in load_scenarios():  # 開発者画面のシナリオ(ブラウザごとの写し)も、元のユーザーと同じ会員
        prepare(state, sc, f"w-0123456789abcdef:{sc['user']}")
        assert behavior.dip_id(state, f"w-0123456789abcdef:{sc['user']}") == want[sc["user"]]

    accounts.of(state).unlink("sim-exit")
    state.reset_user("sim-exit")
    assert behavior.dip_id(state, "sim-exit") == "D1002"
    accounts.of(state).unlink("sim-yoru")
    state.reset()
    assert behavior.dip_id(state, "sim-yoru") == "D1001"


def test_passwords_are_stored_only_as_salted_hashes():
    import json

    rows = json.loads(accounts.LOGINS.read_text(encoding="utf-8"))["logins"]
    hashes = [r["password_hash"] for r in rows]
    assert all(h.startswith("pbkdf2_sha256$") for h in hashes) and len({h.split("$")[2] for h in hashes}) == len(hashes)
    assert accounts.check_password(EXIT[1], next(r["password_hash"] for r in rows if r["login_id"] == EXIT[0]))
    assert not accounts.check_password("demo-pass-1001", next(r["password_hash"] for r in rows if r["login_id"] == EXIT[0]))
