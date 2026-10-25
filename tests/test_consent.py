"""本人の同意(task 214。docs/design/privacy.md の 3.6、line_runtime.consent)。実 LLM も実 LINE も呼ばない。

- Web は最初にトークを開いたとき、LINE は友だち追加と最初の発言の前に、明示のボタンで同意を取る。同意の前は LLM を呼ばず、状態に書かない
- 履歴書と音声相談は、使う直前にもう一度示して同意を取る
- 同意は版つきで記録し、規約かポリシーの版が上がったら取り直す

ここのテストは同意の判定を本物のまま使う(tests/conftest.py の consent の印)。
"""

from __future__ import annotations

import re
from functools import partial
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dip_mcp.data import State
from line_runtime import consent, legal, line_channel
from line_runtime.line_channel import line_user
from line_runtime.runtime import LineRuntime
from tests.test_handle import ScriptedModel
from tests.test_liff_actions import _liff_client, line_liff  # noqa: F401  line_liff はフィクスチャ
from tests.test_line_webhook import (
    RAW_OTHER,
    RAW_USER,
    REPLY,
    _event,
    _post,
    line_id_key,  # noqa: F401  LINE のユーザーIDを作る鍵(autouse)
)
from tests.test_line_webhook import _server as _line_server
from tests.test_resume_intake import StubClient
from tests.test_resume_upload import FILENAME, clean_draft
from tests.test_revisit import Greeter, greet
from tests.test_talk_actions import (  # noqa: F401  フィクスチャ
    LIVEKIT_ENV,
    _fake_livekit,
    advised,
    browser,
    livekit,
    served,
)
from tests.test_talk_page import app, stream  # noqa: F401  app はフィクスチャ

pytestmark = pytest.mark.consent


class NoModel(ScriptedModel):
    """呼ばれたら落ちる偽のモデル(同意の前は LLM を呼ばないことを確かめる)。"""

    def _generate(self, *a, **k):
        raise AssertionError("同意の前に LLM を呼んだ")


def _bump(monkeypatch, privacy: str = "1.2") -> None:
    """ポリシーの版を上げたことにする。"""
    monkeypatch.setattr(legal, "versions", lambda: {"terms": legal.TERMS_VERSION, "privacy": privacy})


# ----------------------------- 記録と版 -----------------------------


def test_record_keeps_the_version_and_a_new_version_asks_again(tmp_path, monkeypatch):
    state = State(tmp_path / "state.json")
    uid = "w-1234567890abcdef"
    assert consent.needed(state, uid, "terms") and uid not in state.user_ids()  # 同意の前は行も無い
    assert consent.tag() == "privacy=1.1;terms=1.0"
    assert consent.record(state, uid, "terms", "privacy=1.0;terms=1.0") is False  # 示していない版には同意できない
    assert uid not in state.user_ids()
    assert consent.record(state, uid, "terms", consent.tag()) is True
    rec = state.user(uid)["consents"]["terms"]
    assert rec["version"] == {"terms": "1.0", "privacy": "1.1"} and rec["at"].endswith("+00:00")
    assert not consent.needed(state, uid, "terms") and consent.needed(state, uid, "resume")
    # /dev のシナリオユーザーは、同じブラウザの人(匿名ID)の同意を見る
    assert not consent.needed(state, f"{uid}:sim-new", "terms")
    _bump(monkeypatch)
    assert consent.needed(state, uid, "terms")  # 版が上がったら取り直す
    assert consent.record(state, uid, "terms", "privacy=1.2;terms=1.0")
    assert state.user(uid)["consents"]["terms"]["version"]["privacy"] == "1.2"


def test_texts_follow_the_design_and_the_policy():
    web, line = consent.texts("web"), consent.texts("line")
    terms = "\n".join(web["terms"]["points"])
    for words in ("米国の Anthropic", "米国の OpenAI にも", "学習には使わない", "30 日", "180 日", "「すべて消す」", "18 歳未満", "自動で伏せます",
                  "プライバシーポリシーの「5」", "架空のデモ", "米国の Render", "州ごとの法律"):
        assert words in terms
    assert "「データを消す」" in "\n".join(line["terms"]["points"]) and "「すべて消す」" not in "\n".join(line["terms"]["points"])
    assert "隠すか消してから" in web["resume"]["strong"] and web["resume"]["button"] == "同意してファイルを選ぶ"
    assert all(w in "".join(web["voice"]["paragraphs"]) for w in ("LiveKit", "Deepgram", "Cartesia", "録音は保存しません"))
    assert [x["url"] for x in consent.links()] == ["/legal/terms", "/legal/privacy"]


# ----------------------------- Web のトーク画面 -----------------------------


def test_web_talk_asks_before_the_greeting_and_calls_no_llm_until_agreed(app, monkeypatch):  # noqa: F811
    app.set_model(NoModel())
    with TestClient(app.app) as client:
        client.get("/")
        opened = client.post("/api/talk/open").json()
        uid = opened["user"]
        assert opened["event"] is None and opened["consent"]["needed"] == ["terms", "resume", "voice"]
        assert opened["consent"]["version"] == consent.tag()
        # 同意の前: 発言も挨拶も LLM に届かない(403)。状態にも書かない
        for event, text in (("visit", None), ("message", "渋谷で日中")):
            r = client.post("/api/event/stream", json={"user_id": uid, "event": event, "text": text, "thread": None})
            assert r.status_code == 403 and "同意" in r.json()["detail"]
        assert client.post("/api/event", json={"user_id": uid, "event": "message", "text": "こんにちは"}).status_code == 403
        assert uid not in app.state.user_ids()
        # 履歴書や音声の同意は、使い始める同意の後だけ
        assert client.post("/api/consent", json={"kind": "resume", "version": consent.tag()}).status_code == 403
        # 示していない版では記録しない
        r = client.post("/api/consent", json={"kind": "terms", "version": "privacy=0.9;terms=1.0"})
        assert r.status_code == 409 and r.json()["consent"]["version"] == consent.tag()
        assert uid not in app.state.user_ids()

        r = client.post("/api/consent", json={"kind": "terms", "version": consent.tag()})
        assert r.status_code == 200 and r.json()["needed"] == ["resume", "voice"]
        assert app.state.user(uid)["consents"]["terms"]["version"] == legal.versions()

        app.set_model(Greeter(script=[greet]))
    with TestClient(app.app, cookies=client.cookies) as client:  # 新しいモデルで開き直す
        opened = client.post("/api/talk/open").json()
        assert opened["event"] == "visit"
        evs = stream(client, uid, "visit", opened["thread"])
        assert evs[-1]["kind"] == "reply" and evs[-1]["bubbles"]

        # 版が上がったら、次に開いたときに取り直す(LLM は呼ばない)
        _bump(monkeypatch)
        opened = client.post("/api/talk/open").json()
        assert opened["event"] is None and "terms" in opened["consent"]["needed"]
        r = client.post("/api/event/stream", json={"user_id": uid, "event": "message", "text": "こんにちは", "thread": None})
        assert r.status_code == 403


def test_resume_and_voice_ask_again_right_before_use(app, monkeypatch):  # noqa: F811
    for k, v in LIVEKIT_ENV.items():
        monkeypatch.setenv(k, v)
    stub = StubClient(clean_draft(), usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    monkeypatch.setattr(app, "resume_client", lambda: stub)
    with TestClient(app.app) as client:
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        assert client.post("/api/consent", json={"kind": "terms", "version": consent.tag()}).status_code == 200

        def upload():
            return client.post("/api/resume/upload", data={"user_id": uid},
                               files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/octet-stream")})

        r = upload()
        assert r.status_code == 403 and "同意してファイルを選ぶ" in r.json()["detail"]
        assert stub.calls == []  # 読み取りに送らない
        r = client.post("/api/voice/start", json={"user_id": uid})
        assert r.status_code == 403 and "音声相談の前の同意" in r.json()["detail"]
        assert app.app.state.voice_rooms.open_room(uid) is None

        assert client.post("/api/consent", json={"kind": "resume", "version": consent.tag()}).status_code == 200
        assert upload().status_code == 200 and len(stub.calls) == 1
        assert client.post("/api/consent", json={"kind": "voice", "version": consent.tag()}).json()["needed"] == []
        assert client.post("/api/voice/start", json={"user_id": uid}).status_code == 200
        assert set(app.state.user(uid)["consents"]) == {"terms", "resume", "voice"}


def test_refusals_before_consent_spend_no_turn(app, monkeypatch):  # noqa: F811
    for k, v in LIVEKIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "1")
    with TestClient(app.app) as client:
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        client.post("/api/consent", json={"kind": "terms", "version": consent.tag()})
        for _ in range(3):  # 何度断られても、1 日 1 回の枠は残る
            assert client.post("/api/voice/start", json={"user_id": uid}).status_code == 403
        client.post("/api/consent", json={"kind": "voice", "version": consent.tag()})
        assert client.post("/api/voice/start", json={"user_id": uid}).status_code == 200


def test_arcade_ai_waits_for_consent(app, monkeypatch):  # noqa: F811
    with TestClient(app.app) as client:
        client.get("/")
        r = client.post("/api/arcade/start", json={"game": "harvest-of-work"})
        assert r.status_code == 200 and r.json()["play"] is None and r.json()["ai"] is False
        uid = client.get("/api/me").json()["user_id"]
        assert uid not in app.state.user_ids()


def test_writes_wait_for_consent_but_deleting_does_not(app):  # noqa: F811
    """同意の前は、プロフィールに足す・直す口、シナリオと連携の開始を断る(行を作らない)。消す口は同意の前もできる。"""
    with TestClient(app.app) as client:
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        assert client.put("/api/profile/items/area", json={"value": "渋谷"}).status_code == 403
        assert client.put("/api/profile/track", json={"track": "正社員"}).status_code == 403
        assert client.post("/api/scenarios/F-follow-new/start").status_code == 403
        assert client.post("/api/dev/link/start").status_code == 403
        assert client.post("/api/talk/track/undo", json={"user_id": uid, "id": "x"}).status_code == 403
        assert client.delete("/api/profile/items/area").status_code == 200
        assert client.delete("/api/profile/track").status_code == 200
        assert not [u for u in app.state.user_ids() if u.startswith(f"{uid}:")]  # シナリオユーザーも作らない
        assert client.delete("/api/profile/resume").status_code == 200
        assert client.delete("/api/profile/saved/J-0001").status_code == 200
        assert uid not in app.state.user_ids()  # 消すものが無いので行も作らない
        client.post("/api/consent", json={"kind": "terms", "version": consent.tag()})
        assert client.put("/api/profile/items/area", json={"value": "渋谷"}).status_code == 200
        assert client.post("/api/scenarios/F-follow-new/start").status_code == 200


def test_deleting_before_consent_cannot_add_rows_past_the_ip_limit(app, monkeypatch):  # noqa: F811
    """同意の前の消す口でも、Cookie を作り直して状態ファイルに行を増やし続けられない(IP ごとの新しい匿名IDの上限)。
    版が上がって同意を取り直す人(行がある)は、同意の前も消せる。"""
    monkeypatch.setenv("AICA_DAILY_NEW_IDS_PER_IP", "2")
    with TestClient(app.app) as client:
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        client.post("/api/consent", json={"kind": "terms", "version": consent.tag()})
        assert client.put("/api/profile/items/area", json={"value": "渋谷"}).status_code == 200
        before, own = set(app.state.user_ids()), dict(client.cookies)
        for _ in range(6):
            client.cookies.clear()
            client.get("/")
            assert client.delete("/api/profile/track").status_code == 200
            assert client.delete("/api/profile/items/area").json()["sections"]  # 空のプロフィールを返す
        assert set(app.state.user_ids()) == before
        # 版が上がった後、同意の前でも本人の行は消せる
        _bump(monkeypatch)
        client.cookies.clear()
        client.cookies.update(own)
        assert client.post("/api/talk/open").json()["consent"]["needed"][0] == "terms"
        assert client.put("/api/profile/items/area", json={"value": "新宿"}).status_code == 403
        assert client.delete("/api/profile/items/area").status_code == 200
        assert not app.state.user(uid).get("preferences", {}).get("area")


def test_forget_all_removes_the_consent_too(app):  # noqa: F811
    """「すべて消す」の後に残るもの(ポリシーの 7)に同意は無い: 次に使うときに取り直す。"""
    with TestClient(app.app) as client:
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        client.post("/api/consent", json={"kind": "terms", "version": consent.tag()})
        assert client.post("/api/profile/forget").status_code == 200
        assert client.post("/api/talk/open").json()["consent"]["needed"][0] == "terms"
        assert "consents" not in app.state.user(uid)


# ----------------------------- LINE -----------------------------


def _messages(fake) -> list[list[dict]]:
    return [b["messages"] for p, b in fake.sent() if p == REPLY]


def test_line_asks_on_follow_and_before_the_first_message_then_greets_after_the_button(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    server, fake = _line_server(tmp_path, monkeypatch, NoModel())
    with TestClient(server.app) as client:
        _post(client, [_event("follow")])
        _post(client, [_event("message", text="渋谷で日中", event_id="01EVENT0000000000000000002")])
        _post(client, [_event("message", text=line_channel.LINK_TEXT, event_id="01EVENT0000000000000000003")])
    asked = _messages(fake)
    assert len(asked) == 3
    for (m,) in asked:  # どれも同意の要点と、規約とポリシーの URL と、「同意して始める」(postback)
        assert "米国の Anthropic" in m["text"] and "「データを消す」" in m["text"]
        assert "https://aica.example.test/legal/terms" in m["text"] and "https://aica.example.test/legal/privacy" in m["text"]
        (item,) = m["quickReply"]["items"]
        assert item["action"] == {"type": "postback", "label": "同意して始める",
                                  "data": "aica-consent=" + consent.tag(), "displayText": "同意して始める"}
    assert uid not in server.state.user_ids()  # 発言も残さない
    assert "渋谷で日中" not in "".join(p.read_text(encoding="utf-8", errors="ignore") for p in tmp_path.rglob("*") if p.is_file())

    # 「同意して始める」を押すと記録して、友だち追加と同じ挨拶をする
    model = Greeter(script=[greet])
    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model))
    fake.calls.clear()
    with TestClient(server.app) as client:
        pb = _event("postback", event_id="01EVENT0000000000000000004") | {"postback": {"data": "aica-consent=" + consent.tag()}}
        _post(client, [pb])
        # 二度押しは何もしない
        _post(client, [pb | {"webhookEventId": "01EVENT0000000000000000005"}])
    assert model.script == []
    (greeting,) = _messages(fake)
    assert "quickReply" in greeting[-1] and all("同意して始める" not in m.get("text", "") for m in greeting)
    assert server.state.user(uid)["consents"]["terms"]["version"] == legal.versions()


def test_line_button_from_an_old_version_asks_again(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    server, fake = _line_server(tmp_path, monkeypatch, NoModel())
    with TestClient(server.app) as client:
        _post(client, [_event("postback") | {"postback": {"data": "aica-consent=privacy=0.9;terms=1.0"}}])
        _post(client, [_event("message", text="同意して始める", event_id="01EVENT0000000000000000002")])  # 文では同意にならない
    old, typed = _messages(fake)
    assert old[0]["text"].startswith(consent.STALE) and old[0]["quickReply"]["items"][0]["action"]["data"].endswith(consent.tag())
    assert "米国の OpenAI" in typed[0]["text"]
    assert uid not in server.state.user_ids()


def test_line_data_commands_work_before_consent(tmp_path, monkeypatch):
    """データを見る、消すは同意の前もできる(本人の権利。LLM は使わない)。"""
    server, fake = _line_server(tmp_path, monkeypatch, NoModel())
    with TestClient(server.app) as client:
        _post(client, [_event("message", text=line_channel.VIEW_DATA_TEXT)])
    ((m, *_),) = _messages(fake)
    assert m["text"].startswith(line_channel.VIEW_INTRO)


# ----------------------------- 画面(Playwright) -----------------------------


def test_talk_screen_consent_flow_in_the_browser(advised, served, browser, livekit):  # noqa: F811
    """初めて開くと挨拶の前に同意の画面。押すまで LLM を呼ばない。履歴書と音声は、使う直前にもう一度示す。"""
    context = browser.new_context()
    _fake_livekit(context)
    page = context.new_page()
    page.goto(served + "/")
    sheet = page.locator("#consent-sheet")
    sheet.wait_for(state="visible")
    box = sheet.locator(".consent-box[data-kind=terms]")
    assert "米国の OpenAI" in box.inner_text() and "「すべて消す」" in box.inner_text()
    assert box.locator("a[href='/legal/terms']").count() == 1 and box.locator("a[href='/legal/privacy']").count() == 1
    assert sheet.locator("#consent-cancel").count() == 0  # 使い始める同意は「やめる」で閉じない
    page.wait_for_timeout(300)
    assert page.locator("#log .bubble").count() == 0
    assert advised.model.seen == []  # 押すまで LLM を呼ばない

    page.locator("#consent-agree").click()
    sheet.wait_for(state="hidden")
    page.locator("#log .chips button", has_text="まず相談する").click()
    pick = page.locator("#log .chips button.action", has_text="ファイルを選ぶ")
    pick.wait_for()

    # 履歴書: ファイルを選ぶ直前に同意の画面。「同意してファイルを選ぶ」で、ファイルを選ぶ画面が開く
    pick.click()
    resume = sheet.locator(".consent-box[data-kind=resume]")
    resume.wait_for()
    assert "隠すか消してから" in resume.inner_text()
    with page.expect_file_chooser() as chooser:
        page.locator("#consent-agree").click()
    chooser.value.set_files({"name": FILENAME, "mimeType": "application/pdf", "buffer": b"%PDF-1.4 dummy"})
    page.get_by_text("コンビニみほん駅前店での接客のご経験を活かせる仕事で探しますか?").wait_for()

    # 音声: シートを開く直前に同意の画面。「やめる」なら開かない、「同意して始める」で開く
    page.locator("#plus").click()
    page.locator("#menu-voice").click()
    sheet.locator(".consent-box[data-kind=voice]").wait_for()
    page.locator("#consent-cancel").click()
    sheet.wait_for(state="hidden")
    assert page.locator("#voice-sheet").is_hidden()
    page.locator("#plus").click()
    page.locator("#menu-voice").click()
    page.locator("#consent-agree").click()
    page.locator("#voice-sheet").wait_for(state="visible")
    page.wait_for_function("window.__lk && window.__lk.mic === true")

    # 開き直しても、もう聞かない
    page.locator("#voice-close").click()
    page.locator("#voice-quit").click()
    page.reload()
    page.locator("#log .bubble").first.wait_for()
    assert sheet.is_hidden()

    # 開いている間に同意の記録が消えた(別のタブで「すべて消す」、版が上がったなど): 次の発言で断られ、同意の画面を出し直す
    uid = page.evaluate("fetch('/api/me').then(r => r.json())")["user_id"]
    advised.state.update(uid, lambda u: u.pop("consents"))
    page.locator("#t").fill("渋谷で日中")
    page.locator("#send").click()
    sheet.locator(".consent-box[data-kind=terms]").wait_for()
    assert page.locator("#t").input_value() == "渋谷で日中"  # 送られなかった発言は入力欄に戻す
    assert "渋谷で日中" not in advised.model.seen
    page.locator("#consent-agree").click()
    sheet.wait_for(state="hidden")
    page.locator("#log .bubble").first.wait_for()
    context.close()


# ----------------------------- LINE のアプリで開く LIFF 画面 -----------------------------


def test_liff_resume_and_voice_ask_for_their_own_consent(line_liff):  # noqa: F811
    """LIFF 画面の履歴書と音声も、使う直前に同意を取る。使い始める同意(LINE のトーク)の前は、トークへ案内するだけ。"""
    server, _, _, _ = line_liff
    uid = line_user(RAW_USER)
    with _liff_client(server) as client:
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
        page = client.get("/liff/line/resume/upload").text
        assert consent.MISSING_LINE_TERMS in page and 'type="file"' not in page
        r = client.post("/api/liff/voice/start", json={"user_id": uid, "id_token": "good"})
        assert r.status_code == 403 and r.json()["detail"] == consent.MISSING_LINE_TERMS
        body = {"user_id": uid, "kind": "resume", "version": consent.tag()}
        assert client.post("/api/liff/consent", json=body).status_code == 403
        assert client.post("/api/liff/consent", json=body | {"kind": "terms"}).status_code == 400  # トークで取る

        consent.record(server.state, uid, "terms", consent.tag())  # LINE のトークで「同意して始める」を押した
        page = client.get("/liff/line/resume/upload").text
        assert 'id="consent-agree"' in page and "同意してファイルを選ぶ" in page and "隠すか消してから" in page
        assert re.search(r'<input type="file" id="resume-file"[^>]* hidden>', page)  # 同意するまで選ばせない
        files = {"file": (FILENAME, b"%PDF-1.4 dummy", "application/octet-stream")}
        r = client.post("/api/liff/resume/upload", data={"user_id": uid, "id_token": "good"}, files=files)
        assert r.status_code == 403
        assert client.post("/api/liff/consent", json=body | {"user_id": line_user(RAW_OTHER)}).status_code == 403
        assert client.post("/api/liff/consent", json=body).json()["needed"] == ["voice"]
        page = client.get("/liff/line/resume/upload").text
        assert 'id="consent-agree"' not in page and re.search(r'<input type="file" id="resume-file"[^>]*>', page)
        r = client.post("/api/liff/resume/upload", data={"user_id": uid, "id_token": "good"}, files=files)
        assert r.status_code == 200, r.text

        page = client.get("/liff/line/voice").text
        assert "/api/liff/consent" in page and '"needed": ["voice"]' in page
        assert client.post("/api/liff/voice/start", json={"user_id": uid, "id_token": "good"}).status_code == 403
        assert client.post("/api/liff/consent", json=body | {"kind": "voice"}).status_code == 200
        assert client.post("/api/liff/voice/start", json={"user_id": uid, "id_token": "good"}).status_code == 200


def test_liff_arcade_waits_for_the_talk_consent(line_liff, monkeypatch):  # noqa: F811
    """LIFF の中のゲームも、使い始める同意(LINE のトーク)の前は番号なしで、AI を呼ばず、会話数も使わない(Web と同じ)。"""
    server, _, _, _ = line_liff
    monkeypatch.setattr(server, "arcade_client", lambda: (_ for _ in ()).throw(AssertionError("同意の前に AI を呼んだ")))
    uid = line_user(RAW_USER)
    with _liff_client(server) as client:
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
        quota = server.app.state.quota
        used = quota.used(uid)
        r = client.post("/api/liff/arcade/start", json={"game": "the-case-of-you"})
        assert r.status_code == 200 and r.json()["play"] is None and r.json()["ai"] is False
        r = client.post("/api/liff/arcade/ai", json={"game": "the-case-of-you", "play": "p-1", "stage": "reaction"})
        assert r.status_code == 200 and r.json() == {"ok": False, "offline": True, "reason": "no_play"}
        assert quota.used(uid) == used and uid not in server.state.user_ids()

        consent.record(server.state, uid, "terms", consent.tag())  # LINE のトークで「同意して始める」を押した
        assert client.post("/api/liff/arcade/start", json={"game": "the-case-of-you"}).json()["play"] is not None


def test_dev_screen_asks_before_the_first_scenario(advised, served, browser):  # noqa: F811
    """開発者画面も、シナリオの前に同じ同意の画面を出す(同意は人ごと。シナリオユーザーは匿名IDの同意を見る)。"""
    context = browser.new_context()
    page = context.new_page()
    page.goto(served + "/dev")
    page.locator('#scenarios .act[data-id="C-consult-propose"]').click()
    sheet = page.locator("#consent-sheet")
    sheet.locator(".consent-box[data-kind=terms]").wait_for()
    page.locator("#consent-cancel").click()  # やめたらシナリオを始めない
    sheet.wait_for(state="hidden")
    page.wait_for_timeout(300)
    assert advised.model.seen == [] and page.locator("#log .bubble").count() == 0
    page.locator('#scenarios .act[data-id="C-consult-propose"]').click()
    page.locator("#consent-agree").click()
    page.locator("#log .chips button", has_text="まず相談する").wait_for()
    page.locator('#scenarios .act[data-id="C-consult-propose"]').click()  # 2 回目からは聞かない
    page.wait_for_timeout(300)
    assert sheet.is_hidden()
    context.close()
