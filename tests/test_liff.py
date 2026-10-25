"""LINE のアプリで開く LIFF 画面(/liff/line/…)の本人(task 203)。実 LINE は呼ばない: LINE ログインの ID トークンの検証は
偽物の LINE(httpx.MockTransport)が、本物と同じ形(フォームの id_token と client_id、断るときは 400 と error_description)で答える。

- 正しい ID トークンなら、トークと同じ LINE のユーザーID(line_channel.line_user)の本人の画面になる
- 検証が失敗・期限切れ・別のチャネルの aud なら本人にならず、Cookie の匿名ユーザーにも切り替えない
- LIFF の設定が無ければ /liff/line/… は 404 で、ほかは今までどおり
- 画面(LIFF SDK を読む殻)をブラウザで動かす(Playwright。LIFF SDK は偽物に差し替える)
- LIFF の中でマイクを試す画面(task 205): 許可・拒否・エラーを偽物の getUserMedia で動かし、ログには結果の 1 行だけが残る
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from dip_mcp.data import State
from line_runtime import line_channel
from line_runtime.line_channel import (
    IdKeyError,
    LineLogin,
    liff_session,
    liff_session_user,
    line_user,
    to_messages,
)

ID_KEY = "offline-test-aica-id-key-0123456789abcdef"
LIFF_ID = "2000000000-OfflineT"
CHANNEL = "2000000000"  # LIFF を置いた LINE ログインのチャネルの ID(ID トークンの aud)
OTHER_CHANNEL = "2999999999"
RAW_USER = "U" + "0123456789abcdef" * 2  # LINE の userId の形(U + 32 桁)
WEB_USER = "w-00000000000000aa"  # 同じブラウザの Cookie の匿名ユーザー(切り替わってはいけない相手)
LIFF_ENVS = (line_channel.LIFF_ID_ENV, line_channel.LOGIN_CHANNEL_ENV)  # 設定するもの(チャネル ID は前の名前でも読む)
LINE_ENVS = ("LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "AICA_PUBLIC_URL")


def _claims(sub: str = RAW_USER, aud: str = CHANNEL, exp_in: float = 600, iss: str = line_channel.ID_TOKEN_ISSUER) -> dict:
    now = int(time.time())
    return {"iss": iss, "sub": sub, "aud": aud, "exp": int(now + exp_in), "iat": now, "amr": ["linesso"]}


class FakeLineLogin:
    """偽物の LINE ログインの検証の API(POST /oauth2/v2.1/verify)。tokens: ID トークン → その中身(claims)。
    本物と同じく、知らないトークン・期限切れ・client_id と違う aud は 400 で断る。lie なら確かめずに中身を 200 で返す
    (LINE の答えを鵜呑みにしないことを確かめる)。down ならつながらない。"""

    def __init__(self) -> None:
        self.tokens: dict[str, dict] = {"good": _claims()}
        self.lie = self.down = False
        self.calls: list[dict] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down", request=request)
        assert request.method == "POST" and request.url.path == line_channel.VERIFY_PATH
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append(form)
        claims = self.tokens.get(form.get("id_token", ""))
        if self.lie and claims:
            return httpx.Response(200, json=claims)
        if claims is None:
            return httpx.Response(400, json={"error": "invalid_request", "error_description": "Invalid IdToken."})
        if claims["exp"] <= time.time():
            return httpx.Response(400, json={"error": "invalid_request", "error_description": "IdToken expired."})
        if claims["aud"] != form.get("client_id"):
            return httpx.Response(400, json={"error": "invalid_request", "error_description": "Invalid IdToken Audience."})
        return httpx.Response(200, json=claims)


@pytest.fixture
def liff(tmp_path, monkeypatch):
    """LIFF の設定がある Web サーバー(lifespan は回さない。LIFF 画面は会話を使わない)。(server, 偽物の LINE)"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")
    for k in (*LIFF_ENVS, line_channel.LIFF_CHANNEL_ENV, *LINE_ENVS):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv(line_channel.ID_KEY_ENV, ID_KEY)
    monkeypatch.setenv(line_channel.LIFF_ID_ENV, LIFF_ID)
    monkeypatch.setenv(line_channel.LOGIN_CHANNEL_ENV, CHANNEL)
    from line_runtime import config, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()
    fake = FakeLineLogin()
    monkeypatch.setattr(server, "line_login", lambda: LineLogin(CHANNEL, transport=fake.transport))
    return server, fake


def _resumes(server) -> tuple[int, int]:
    """トークの LINE のユーザーと、同じブラウザの匿名ユーザーに、別々の履歴書の読み取り結果を置く。(LINE の rid, Web の rid)"""
    line = logic.save_resume_upload(server.state, line_user(RAW_USER), {"resume": {"skills": ["LINEの本人のスキル"]}})
    web = logic.save_resume_upload(server.state, WEB_USER, {"resume": {"skills": ["匿名ユーザーのスキル"]}})
    return line["id"], web["id"]


def _client(server) -> TestClient:
    """https のブラウザ(LIFF の Cookie は Secure)。同じブラウザで Web の匿名ユーザーの Cookie も持っている。"""
    client = TestClient(server.app, base_url="https://testserver")
    client.cookies.set(server.ANON_COOKIE, WEB_USER)
    return client


def _session(client, token: str, **kw) -> httpx.Response:
    return client.post("/api/liff/session", json={"id_token": token}, **kw)


# ----------------------------- 本人を決める -----------------------------


def test_a_verified_id_token_makes_the_liff_page_the_same_user_as_the_talk(liff):
    server, fake = liff
    rid, web_rid = _resumes(server)
    client = _client(server)

    # まだ確かめていない: LIFF SDK で ID トークンを取る殻だけ。匿名ユーザーの Cookie があっても中身は出さない
    shell = client.get(f"/liff/line/resume?rid={rid}")
    assert shell.status_code == 200
    assert LIFF_ID in shell.text and server.LIFF_SDK in shell.text
    assert "スキル" not in shell.text

    r = _session(client, "good")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert fake.calls == [{"id_token": "good", "client_id": CHANNEL}]
    assert RAW_USER not in r.text and RAW_USER not in r.headers.get("set-cookie", "")  # LINE の userId は返さない
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    assert "max-age" not in cookie.lower() and "expires" not in cookie.lower()  # ブラウザを閉じたら消える
    # Cookie の本人は、トーク(webhook。line_channel.line_user)と同じ鍵つきの ID
    assert liff_session_user(client.cookies[server.LIFF_COOKIE]) == line_user(RAW_USER)

    page = client.get(f"/liff/line/resume?rid={rid}")
    assert page.status_code == 200
    assert "LINEの本人のスキル" in page.text and "匿名ユーザーのスキル" not in page.text
    # Web のブラウザの画面(/liff/…)は今までどおり Cookie の匿名ユーザー
    assert "匿名ユーザーのスキル" in client.get(f"/liff/resume?rid={web_rid}").text


@pytest.mark.parametrize("token, claims", [
    ("forged", None),                                  # 署名が合わない(LINE が知らない)
    ("expired", _claims(exp_in=-60)),                  # 期限切れ
    ("other-channel", _claims(aud=OTHER_CHANNEL)),     # 別のチャネルの LIFF のトークン
])
def test_a_rejected_id_token_is_nobody_and_never_falls_back_to_the_web_user(liff, token, claims):
    server, fake = liff
    if claims:
        fake.tokens[token] = claims
    rid, web_rid = _resumes(server)
    client = _client(server)
    assert _session(client, "good").status_code == 200  # 前に別の本人で確かめていても

    r = _session(client, token)
    assert r.status_code == 401
    assert server.LIFF_COOKIE not in client.cookies  # 前の本人の Cookie も消す
    for path in (f"/liff/line/resume?rid={rid}", f"/liff/line/resume?rid={web_rid}"):
        page = client.get(path)
        assert page.status_code == 200 and "スキル" not in page.text  # 殻のまま。匿名ユーザーの画面にもならない


@pytest.mark.parametrize("claims", [
    _claims(aud=OTHER_CHANNEL), _claims(exp_in=-60), _claims(iss="https://evil.test"), _claims(sub="not-a-line-user"),
])
def test_the_liff_session_does_not_trust_line_s_answer_blindly(liff, claims):
    """LINE が 200 で答えても、発行元・aud・期限・sub の形が合わなければ本人にしない。"""
    server, fake = liff
    fake.tokens["odd"], fake.lie = claims, True
    client = _client(server)
    assert _session(client, "odd").status_code == 401
    assert server.LIFF_COOKIE not in client.cookies


def test_unreachable_line_or_a_non_json_body_is_nobody(liff):
    server, fake = liff
    client = _client(server)
    # ほかのサイトのフォーム(事前の確認が要らない送り方)からは、本人を入れ替えさせない
    for kw in ({"content": json.dumps({"id_token": "good"}), "headers": {"content-type": "text/plain"}},
               {"content": json.dumps({"id_token": "good"})}):
        assert client.post("/api/liff/session", **kw).status_code in (415, 422)
    assert fake.calls == []
    fake.down = True
    assert _session(client, "good").status_code == 401
    assert server.LIFF_COOKIE not in client.cookies


def test_a_forged_or_expired_liff_cookie_is_nobody(liff, monkeypatch):
    server, _ = liff
    rid, _ = _resumes(server)
    client = _client(server)
    value = liff_session(line_user(RAW_USER))
    forged = value.replace(line_user(RAW_USER), line_user("U" + "f" * 32))
    for v in (forged, f"{WEB_USER}.9999999999.00", "garbage"):
        client.cookies.set(server.LIFF_COOKIE, v)
        assert "スキル" not in client.get(f"/liff/line/resume?rid={rid}").text
    client.cookies.set(server.LIFF_COOKIE, value)
    assert "LINEの本人のスキル" in client.get(f"/liff/line/resume?rid={rid}").text
    later = time.time() + line_channel.LIFF_SESSION_SECONDS + 1
    assert liff_session_user(value) == line_user(RAW_USER) and liff_session_user(value, now=later) is None


def test_the_liff_apply_form_needs_the_verified_user(liff):
    server, _ = liff
    client = _client(server)
    r = client.post("/liff/line/apply", data={"job": "a002"})
    assert r.status_code == 401 and server.APPLY_DONE_TEXT not in r.text
    assert _session(client, "good").status_code == 200
    r = client.post("/liff/line/apply", data={"job": "a002"})  # 確かめた本人に、入力途中の応募が無い
    assert r.status_code == 404 and "入力途中の応募はありません" in r.text


def test_the_endpoint_url_lets_liff_init_redirect_first(liff):
    """LINE はまずエンドポイント URL(/liff/line)を liff.state つきで開く。そのときは本人がいても殻(liff.init が送る)。"""
    server, _ = liff
    client = _client(server)
    assert server.LIFF_SDK in client.get("/liff/line", params={"liff.state": "/resume?rid=1"}).text
    assert _session(client, "good").status_code == 200
    assert server.LIFF_SDK in client.get("/liff/line", params={"liff.state": "/resume?rid=1"}).text
    assert server.LIFF_SDK not in client.get("/liff/line").text


# ----------------------------- LINE のボタンと設定 -----------------------------


def test_line_buttons_open_the_liff_url_that_lands_on_the_liff_pages(liff):
    """LIFF の設定があれば、LINE のボタンは LIFF URL。LINE はエンドポイント URL(/liff/line)の後ろにその先を付けて開く。"""
    server, _ = liff
    reply = {"bubbles": ["はい"], "buttons": [{"label": "応募の続き", "url": "/liff/apply?job=a001"}]}
    (_, flex) = to_messages(reply, "")  # 公開 URL が無くても出す
    (link,) = flex["contents"]["body"]["contents"]
    uri = link["action"]["uri"]
    assert uri == f"https://liff.line.me/{LIFF_ID}/apply?job=a001"
    opened = line_channel.LIFF_PREFIX + "/" + uri.removeprefix(f"https://liff.line.me/{LIFF_ID}/")
    assert _client(server).get(opened).status_code == 200
    for page in line_channel.LIFF_PAGES:  # LIFF URL にする画面は、どれも /liff/line の下にある
        assert _client(server).get(line_channel.LIFF_PREFIX + page.removeprefix("/liff"),
                                   params={"rid": 1, "job": "a001"}).status_code == 200

    # /liff/line の下に無い画面は、今までどおり公開 URL。公開 URL も無ければ出さない
    other = {"bubbles": ["はい"], "buttons": [{"label": "ほかの画面", "url": "/liff/other"}]}
    (_, flex) = to_messages(other, "https://aica.example.test")
    assert flex["contents"]["body"]["contents"][0]["action"]["uri"] == "https://aica.example.test/liff/other"
    assert [m["type"] for m in to_messages(other, "")] == ["text"]
    # レジュメ・アーケードも /liff/line の下にある(task 204)。公開 URL があっても LIFF URL で開く
    arcade = {"bubbles": ["はい"], "buttons": [{"label": "ゲーム", "url": "/liff/arcade/harvest-of-work"}]}
    for base in ("https://aica.example.test", ""):
        (_, flex) = to_messages(arcade, base)
        assert flex["contents"]["body"]["contents"][0]["action"]["uri"] == f"https://liff.line.me/{LIFF_ID}/arcade/harvest-of-work"


def test_without_liff_settings_nothing_changes(liff, monkeypatch):
    server, fake = liff
    rid, web_rid = _resumes(server)
    for missing in LIFF_ENVS:  # 片方だけでも使わない
        with monkeypatch.context() as m:
            m.delenv(missing)
            assert not line_channel.liff_configured()
            client = _client(server)
            assert client.get(f"/liff/line/resume?rid={rid}").status_code == 404
            assert client.get("/liff/line").status_code == 404
            assert _session(client, "good").status_code == 404
            assert "匿名ユーザーのスキル" in client.get(f"/liff/resume?rid={web_rid}").text  # Web は今までどおり
            reply = {"bubbles": ["はい"], "buttons": [{"label": "応募の続き", "url": "/liff/apply?job=a001"}]}
            (_, flex) = to_messages(reply, "https://aica.example.test")
            assert flex["contents"]["body"]["contents"][0]["action"]["uri"] == "https://aica.example.test/liff/apply?job=a001"
            line_channel.check_id_key()  # 起動も止めない
    assert fake.calls == []


def test_liff_settings_need_the_id_key_at_startup(liff, monkeypatch):
    monkeypatch.delenv(line_channel.ID_KEY_ENV)
    with pytest.raises(IdKeyError):
        line_channel.check_id_key()
    monkeypatch.delenv(line_channel.LIFF_ID_ENV)
    line_channel.check_id_key()  # LIFF も LINE も設定が無ければ、鍵は要らない


# ----------------------------- 画面をブラウザで動かす(Playwright。LIFF SDK は偽物) -----------------------------


def _fake_sdk(token: str, logged_in: bool) -> str:
    """LIFF SDK の偽物。init は LINE と同じく、URL に liff.state があればエンドポイント URL(/liff/line)の後ろに付けた画面へ送る。
    決まった ID トークンを返す。ログインしていなければ、login は同じ画面を開き直すだけ(ログインできない人)。"""
    return f"""window.liff = {{
  init: async () => {{
    const st = new URLSearchParams(location.search).get("liff.state");
    if (st) {{ location.replace("{line_channel.LIFF_PREFIX}" + st); await new Promise(() => {{}}); }}
  }},
  isLoggedIn: () => {json.dumps(logged_in)},
  login: ({{ redirectUri }}) => location.replace(redirectUri),
  getIDToken: () => {json.dumps(token)},
}};"""


@pytest.fixture
def served(liff):
    import uvicorn

    server, fake = liff
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(300):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    yield server, fake, f"http://localhost:{port}"
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


@pytest.mark.parametrize("token, logged_in, shown", [
    ("good", True, "LINEの本人のスキル"),
    ("forged", True, None),   # LINE が断るトークン
    ("good", False, None),    # ログインできない(ログインの画面と行き来し続けない)
])
def test_the_liff_page_in_a_browser(served, browser, token, logged_in, shown):
    """LINE のアプリで開いたのと同じ流れ: LINE はまずエンドポイント URL を liff.state つきで開き、liff.init が本来の画面へ送る。
    殻が ID トークンを送り、読み直すと本人の画面。確かめられなければ、そう出して止まる。
    同じブラウザに匿名ユーザーの Cookie があっても、その画面にはならない。"""
    server, _, base = served
    rid, _ = _resumes(server)
    context = browser.new_context()
    context.add_cookies([{"name": server.ANON_COOKIE, "value": WEB_USER, "url": base}])
    context.route(server.LIFF_SDK, lambda route: route.fulfill(status=200, content_type="text/javascript",
                                                               body=_fake_sdk(token, logged_in)))
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(10_000)
    try:
        page.goto(f"{base}/liff/line?" + urlencode({"liff.state": f"/resume?rid={rid}"}))
        if shown:
            page.get_by_text(shown).wait_for()
        else:
            page.get_by_text(server.LIFF_FAILED_TEXT).wait_for()
        assert "匿名ユーザーのスキル" not in page.content()
        assert re.search(r"/liff/line/resume\?rid=\d+$", page.url)
        assert errors == []
    finally:
        context.close()


# ----------------------------- LIFF の中でマイクを試す画面(task 205) -----------------------------

MIC_LOGGER = "line_runtime.mic_check"
MIC_RESULT = {"mic": "allowed", "level": 0.42, "webrtc": "ok", "candidates": 2, "livekit": "supported", "os": "ios",
              "in_client": True, "line_version": "14.19.0", "liff_version": "2.27.0", "view_type": "full",
              "secure_context": True, "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Line/14.19.0"}


def _mic_lines(caplog) -> list[dict]:
    prefix = "LIFF のマイクの確認: "
    return [json.loads(r.getMessage().removeprefix(prefix)) for r in caplog.records if r.name == MIC_LOGGER]


def test_the_mic_check_log_keeps_one_result_line_and_nothing_else(liff, caplog):
    server, _ = liff
    caplog.set_level("INFO", logger=MIC_LOGGER)
    client = _client(server)
    page = client.get("/liff/line/mic-check")
    assert page.status_code == 200 and server.LIFF_SDK in page.text and "/static/mic-check.js" in page.text
    assert LIFF_ID in page.text and server.ANON_COOKIE not in page.headers.get("set-cookie", "")
    assert client.get("/static/mic-check.js").status_code == 200

    assert client.post("/api/liff/mic-check", json=MIC_RESULT).json() == {"ok": True}
    (line,) = _mic_lines(caplog)
    assert line["mic"] == "allowed" and line["level"] == 0.42 and line["os"] == "ios" and line["line_version"] == "14.19.0"
    assert WEB_USER not in json.dumps(line) and "testclient" not in json.dumps(line)  # 本人も接続元も書かない

    # 声やその文字起こしを載せる項目は無い(決まった項目の外は 422 で、ログにも書かない)
    for extra in ({"audio": "UklGRiQAAABXQVZF"}, {"transcript": "こんにちは、田中です"}, {"user_id": WEB_USER}):
        assert client.post("/api/liff/mic-check", json=MIC_RESULT | extra).status_code == 422
    for bad in ({"mic": "maybe"}, {"mic_error": "<script>"}, {"level": 2}, {"line_version": "1 2"},
                {"user_agent": "x" * 301}):
        assert client.post("/api/liff/mic-check", json=MIC_RESULT | bad).status_code == 422
    # ほかのサイトのフォーム(事前の確認が要らない送り方)からは書かせない
    r = client.post("/api/liff/mic-check", content=json.dumps(MIC_RESULT), headers={"content-type": "text/plain"})
    assert r.status_code in (415, 422)
    assert len(_mic_lines(caplog)) == 1


def test_the_mic_check_log_has_a_daily_limit(liff, monkeypatch, caplog):
    server, _ = liff
    caplog.set_level("INFO", logger=MIC_LOGGER)
    monkeypatch.setattr(server, "MIC_CHECK_DAILY_LIMIT", 2)
    monkeypatch.setattr(server, "_mic_checks", {})
    client = _client(server)
    assert [client.post("/api/liff/mic-check", json=MIC_RESULT).status_code for _ in range(3)] == [200, 200, 429]
    assert len(_mic_lines(caplog)) == 2


def test_without_liff_settings_there_is_no_mic_check(liff, monkeypatch):
    server, _ = liff
    monkeypatch.delenv(line_channel.LIFF_ID_ENV)
    client = _client(server)
    assert client.get("/liff/line/mic-check").status_code == 404
    assert client.post("/api/liff/mic-check", json=MIC_RESULT).status_code == 404


FAKE_LIVEKIT = "window.LivekitClient = { isBrowserSupported: () => true };"


def _fake_mic_sdk(in_client: bool, init_error: str = "", init_waits: bool = False) -> str:
    """LIFF SDK の偽物(マイクを試す画面が使う分)。openWindow で開いた先を window.__opened に残す。
    init_error なら、init は本物の LiffError と同じく code つきのエラーで失敗する。
    init_waits なら、init はテストが window.__finishInit() を呼ぶまで終わらない(通信が遅いとき)。"""
    if init_error:
        init = f"async () => {{ const e = new Error('init'); e.code = {json.dumps(init_error)}; throw e; }}"
    elif init_waits:
        init = "() => new Promise((resolve) => { window.__finishInit = resolve; })"
    else:
        init = "async () => {}"
    return f"""window.liff = {{
  init: {init},
  getOS: () => "ios", isInClient: () => {json.dumps(in_client)}, getLineVersion: () => {json.dumps("14.19.0" if in_client else None)},
  getVersion: () => "2.27.0", getContext: () => ({{ viewType: "full" }}),
  openWindow: (p) => {{ window.__opened = p; }},
}};"""


# 偽物の getUserMedia。allowed は発振器の音(本物のマイクは使わない)、ほかは本物と同じ名前の DOMException で断る
FAKE_MEDIA = {
    "allowed": """navigator.mediaDevices.getUserMedia = async () => {
  const ctx = new AudioContext(); const osc = ctx.createOscillator(); const dst = ctx.createMediaStreamDestination();
  osc.connect(dst); osc.start(); return dst.stream;
};""",
    "denied": 'navigator.mediaDevices.getUserMedia = async () => { throw new DOMException("denied", "NotAllowedError"); };',
    "error": 'navigator.mediaDevices.getUserMedia = async () => { throw new DOMException("no mic", "NotFoundError"); };',
    "unsupported": 'Object.defineProperty(navigator, "mediaDevices", { value: undefined });',
}


@pytest.mark.parametrize("case, mic_error, shown", [
    ("allowed", "", "マイク: 使えた"),
    ("denied", "NotAllowedError", "マイク: 断られた(NotAllowedError)"),
    ("error", "NotFoundError", "マイク: エラー(NotFoundError)"),
    ("unsupported", "", "マイク: この画面には無い"),
])
def test_the_mic_check_page_in_a_browser(served, browser, caplog, case, mic_error, shown):
    """LIFF の中でマイクを試す画面を、偽物の LIFF SDK と偽物の getUserMedia で動かす。許可・拒否・エラーとエラーの名前、
    音の大きさ、LIFF の環境が画面に出て、サーバーには結果の 1 回だけを送る(ほかに何も送らない)。"""
    server, _, base = served
    caplog.set_level("INFO", logger=MIC_LOGGER)
    context = browser.new_context()
    context.add_init_script(FAKE_MEDIA[case])
    context.route(server.LIFF_SDK, lambda route: route.fulfill(status=200, content_type="text/javascript",
                                                               body=_fake_mic_sdk(in_client=True)))
    context.route(server.LIVEKIT_CLIENT, lambda route: route.fulfill(status=200, content_type="text/javascript",
                                                                     body=FAKE_LIVEKIT))
    page = context.new_page()
    errors: list[str] = []
    posts: list[tuple[str, str]] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("request", lambda r: posts.append((r.url, r.post_data or "")) if r.method != "GET" else None)
    page.set_default_timeout(20_000)
    try:
        page.goto(f"{base}/liff/line/mic-check")
        page.get_by_text("LINE の版: 14.19.0").wait_for()
        page.click("#mic-start")
        page.wait_for_selector(f"#mic-status[data-done='{case}']")
        result = page.inner_text("#mic-result")
        assert shown in result and "OS: ios" in result and "通話の部品(WebRTC): 使える" in result
        assert "LiveKit: 対応している" in result and "記録しました" in page.inner_text("#mic-status")

        (url, data), = posts  # サーバーに送るのは結果の 1 回だけ
        assert url == f"{base}/api/liff/mic-check"
        sent = json.loads(data)
        assert set(sent) <= set(server.MicCheckIn.model_fields)
        (line,) = _mic_lines(caplog)
        assert line == server.MicCheckIn(**sent).model_dump()
        assert line["mic"] == case and line["mic_error"] == mic_error and line["webrtc"] == "ok"
        assert line["in_client"] is True and line["os"] == "ios" and line["view_type"] == "full"
        if case == "allowed":
            assert line["level"] > 0.01 and line["audio_state"] == "running"  # 発振器の音が届いた

        # LINE のアプリの中なら、外のブラウザで同じ確認を開ける
        page.click("#mic-external")
        assert page.evaluate("window.__opened") == {"url": f"{base}/liff/line/mic-check", "external": True}
        assert errors == []
    finally:
        context.close()


def test_the_mic_check_page_outside_line_has_no_external_button(served, browser):
    server, _, base = served
    context = browser.new_context()
    context.route(server.LIFF_SDK, lambda route: route.fulfill(status=200, content_type="text/javascript",
                                                               body=_fake_mic_sdk(in_client=False)))
    page = context.new_page()
    try:
        page.goto(f"{base}/liff/line/mic-check")
        page.get_by_text("LINE のアプリの中: いいえ").wait_for()
        assert "LINE の版: 不明" in page.inner_text("#mic-result")
        assert page.is_hidden("#mic-external")
    finally:
        context.close()


def test_the_mic_check_still_runs_when_liff_init_fails(served, browser, caplog):
    """liff.init が失敗しても(LIFF の設定の誤りなど)、マイクは試せて、失敗のコードを記録する。openWindow は使えないので
    外のブラウザのボタンは出さない。LiveKit の部品を読み込めなければ、そう記録する。"""
    server, _, base = served
    caplog.set_level("INFO", logger=MIC_LOGGER)
    context = browser.new_context()
    context.add_init_script(FAKE_MEDIA["denied"])
    context.route(server.LIFF_SDK, lambda route: route.fulfill(status=200, content_type="text/javascript",
                                                               body=_fake_mic_sdk(in_client=True, init_error="INIT_FAILED")))
    context.route(server.LIVEKIT_CLIENT, lambda route: route.abort())
    page = context.new_page()
    page.set_default_timeout(20_000)
    try:
        page.goto(f"{base}/liff/line/mic-check")
        page.get_by_text("LIFF の初期化のエラー: INIT_FAILED").wait_for()
        assert page.is_hidden("#mic-external")
        page.click("#mic-start")
        page.wait_for_selector("#mic-status[data-done='denied']")
        assert "LiveKit: 読み込めなかった" in page.inner_text("#mic-result")
        (line,) = _mic_lines(caplog)
        assert line["liff_error"] == "INIT_FAILED" and line["mic"] == "denied" and line["livekit"] == "not_loaded"
    finally:
        context.close()


def test_the_mic_check_result_stays_when_liff_init_finishes_late(served, browser, caplog):
    """liff.init が待つ上限(WAIT_MS)より遅れて終わっても、出ていたマイクの結果を画面から消さない。押したときの Timeout は
    消して遅れたと出し、LIFF の環境と外のブラウザのボタンを後から正しくする。サーバーには押したときの結果の 1 回だけを送る。"""
    server, _, base = served
    caplog.set_level("INFO", logger=MIC_LOGGER)
    context = browser.new_context()
    context.add_init_script(FAKE_MEDIA["denied"])
    context.route(server.LIFF_SDK, lambda route: route.fulfill(status=200, content_type="text/javascript",
                                                               body=_fake_mic_sdk(in_client=True, init_waits=True)))
    context.route(server.LIVEKIT_CLIENT, lambda route: route.fulfill(status=200, content_type="text/javascript",
                                                                     body=FAKE_LIVEKIT))
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_default_timeout(20_000)
    try:
        page.goto(f"{base}/liff/line/mic-check")
        page.wait_for_function("typeof window.__finishInit === 'function'")
        page.click("#mic-start")
        page.wait_for_selector("#mic-status[data-done='denied']")
        result = page.inner_text("#mic-result")
        assert "マイク: 断られた(NotAllowedError)" in result and "LIFF の初期化のエラー: Timeout" in result
        assert page.is_hidden("#mic-external")

        page.evaluate("window.__finishInit()")
        page.get_by_text("LINE の版: 14.19.0").wait_for()
        result = page.inner_text("#mic-result")
        assert "マイク: 断られた(NotAllowedError)" in result and "通話の部品(WebRTC): 使える" in result
        assert "LiveKit: 対応している" in result and "LINE のアプリの中: はい" in result
        assert "Timeout" not in result and "遅れて終わった(サーバーの記録は、終わる前の値)" in result
        assert page.is_visible("#mic-external")

        (line,) = _mic_lines(caplog)
        assert line["liff_error"] == "Timeout" and line["mic"] == "denied" and line["os"] == ""
        assert errors == []
    finally:
        context.close()
