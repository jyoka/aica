"""LIFF の 3 つの画面(音声相談、履歴書の読み込み、ゲーム)で、使い始める同意をその場で取る(task 239)。

リッチメニューから LIFF の画面を直接開いた、まだ同意していない LINE の本人は、トークに戻らずにその画面で同意して、
そのままサービスを使える。音声相談と履歴書は、使い始める同意とそれぞれの同意を 1 つの画面で示し、1 回押せば両方を記録する。
画面で同意した人には、トークで同意を求め直さず、次に話しかけたときに挨拶から始める(同意の直後に挨拶を push はしない)。
同意しなかった(閉じた)人には使わせず、何も記録しない。実 LLM も実 LINE も呼ばない。
"""

from __future__ import annotations

import time
from urllib.parse import urlencode

import pytest

from dip_mcp import logic
from line_runtime import consent, legal
from line_runtime.line_channel import line_user
from tests.test_liff_actions import (  # noqa: F401  browser と served と line_liff はフィクスチャ
    _liff_client,
    _liff_context,
    _pushes,
    browser,
    line_liff,
    served,
)
from tests.test_line_webhook import (
    RAW_OTHER,
    RAW_USER,
    REPLY,
    _event,
    _post,
    line_id_key,  # noqa: F401  LINE のユーザーIDを作る鍵(autouse)
)
from tests.test_resume_upload import FILENAME

pytestmark = pytest.mark.consent

GAME = "the-case-of-you"


def _uid() -> str:
    return line_user(RAW_USER)  # LINE のユーザーIDは鍵(line_id_key)ができてから作る


def _agreed(server, *kinds: str) -> bool:
    return all(not consent.needed(server.state, _uid(), k) for k in kinds)


def _open(page, base: str, path: str) -> None:
    """LINE のリッチメニューのボタン(https://liff.line.me/<LIFF ID><path>)で開いたのと同じ流れ。"""
    page.goto(f"{base}/liff/line?" + urlencode({"liff.state": path}))
    page.wait_for_url(f"{base}/liff/line{path}")


@pytest.fixture
def page_of(served, browser):  # noqa: F811
    """LIFF の偽の SDK を入れたブラウザの画面を作る。画面のエラーを集め、最後に閉じる。"""
    server = served[0]
    contexts, errors = [], []

    def make():
        context = _liff_context(browser, server)
        contexts.append(context)
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.set_default_timeout(10_000)
        return page

    yield make, errors
    for c in contexts:
        c.close()


# ----------------------------- 画面(Playwright) -----------------------------


def test_voice_page_asks_terms_and_voice_once_and_opens_the_sheet(served, page_of):  # noqa: F811
    """terms も voice もまだの本人が音声相談の画面を開くと、同意の画面が 1 回だけ出て、両方の文を示す。
    「同意して始める」を 1 回押すと両方を記録し、そのまま音声相談のシートに進む。挨拶の push はしない。"""
    server, fake, _, _, base = served
    make, errors = page_of
    page = make()
    _open(page, base, "/voice")
    sheet = page.locator("#consent-sheet")
    sheet.wait_for(state="visible")
    (box,) = sheet.locator(".consent-box").all()
    assert box.get_attribute("data-kind") == "terms voice"
    text = box.inner_text()
    assert consent.TERMS_INTRO in text and "「データを消す」" in text  # 使い始める同意(LINE の文言)
    assert "米国の LiveKit" in text  # 音声の同意
    assert box.locator("a[href='/legal/terms']").count() == 1 and box.locator("a[href='/legal/privacy']").count() == 1
    assert sheet.locator("#consent-agree").count() == 1
    assert sheet.locator("#consent-agree").inner_text() == consent.texts("line")["terms"]["button"]
    assert not _agreed(server, "terms") and _uid() not in server.state.user_ids()  # 押すまで何も書かない

    page.locator("#consent-agree").click()
    page.locator("#voice-sheet").wait_for(state="visible")
    page.wait_for_function("window.__lk && window.__lk.mic === true")
    assert sheet.is_hidden()
    assert _agreed(server, "terms", "voice")
    assert server.state.user(_uid())["consents"]["terms"]["version"] == legal.versions()
    assert logic.greeting_due(server.state, _uid())  # 次にトークで話しかけたら挨拶から
    assert server.app.state.line_voice_rooms  # 音声相談が始まった
    time.sleep(0.3)
    assert _pushes(fake) == []  # 同意の直後に挨拶を push しない
    assert errors == []


def test_closing_the_voice_consent_records_nothing_and_asks_again(served, page_of):  # noqa: F811
    """同意の画面を「やめる」で閉じたら、音声相談を始めず、何も記録しない。もう一度押す、開き直すと、また同意の画面が出る。"""
    server, _, _, _, base = served
    make, errors = page_of
    page = make()
    _open(page, base, "/voice")
    sheet = page.locator("#consent-sheet")
    sheet.wait_for(state="visible")
    page.locator("#consent-cancel").click()
    sheet.wait_for(state="hidden")
    assert page.locator("#voice-sheet").is_hidden()
    assert _uid() not in server.state.user_ids() and server.app.state.line_voice_rooms == {}

    page.locator("#voice-open").click()  # もう一度押すと、また同意の画面
    sheet.locator(".consent-box[data-kind='terms voice']").wait_for()
    page.locator("#consent-cancel").click()
    page.reload()  # 開き直しても、また同意の画面
    sheet.locator(".consent-box[data-kind='terms voice']").wait_for()
    assert _uid() not in server.state.user_ids()
    assert errors == []


def test_resume_page_asks_terms_and_resume_once_and_shows_the_file_picker(served, page_of):  # noqa: F811
    """terms も resume もまだの本人が履歴書の画面を開くと、同意の画面が 1 回だけ出て、両方の文を示す。
    1 回押すと両方を記録し、ファイルを選ぶ欄が出て、読み込める。"""
    server, fake, _, _, base = served
    make, errors = page_of
    page = make()
    _open(page, base, "/resume/upload")
    page.locator(".consent").first.wait_for()
    (box,) = page.locator(".consent").all()
    assert box.get_attribute("data-kind") == "terms resume"
    text = box.inner_text()
    assert consent.TERMS_INTRO in text and "隠すか消してから" in text
    assert page.locator("#consent-agree").count() == 1
    assert page.locator("#resume-file").is_hidden() and page.locator("#resume-send").is_hidden()
    assert _uid() not in server.state.user_ids()

    page.locator("#consent-agree:not([disabled])").click()
    page.locator("#resume-file").wait_for(state="visible")
    assert page.locator(".consent").count() == 0
    assert _agreed(server, "terms", "resume") and logic.greeting_due(server.state, _uid())
    assert _pushes(fake) == []  # 同意の直後に挨拶を push しない

    page.locator("#resume-file").set_input_files({"name": FILENAME, "mimeType": "application/pdf", "buffer": b"%PDF-1.4 dummy"})
    page.locator("#resume-send").click()
    page.get_by_text("続きを LINE のトークに送ります").wait_for()
    assert "履歴書を読み取りました" in page.locator("#resume-result").inner_text()
    assert errors == []


def test_resume_page_without_agreeing_does_not_read_or_record(served, page_of):  # noqa: F811
    """同意の画面で押さずに閉じた(開き直した)人には、ファイルを選ばせず、何も記録しない。次に開くとまた同意の画面が出る。"""
    server, _, _, _, base = served
    make, _ = page_of
    page = make()
    _open(page, base, "/resume/upload")
    page.locator("#consent-agree:not([disabled])").wait_for()
    page.reload()
    page.locator(".consent[data-kind='terms resume']").wait_for()
    assert page.locator("#resume-file").is_hidden()
    assert _uid() not in server.state.user_ids()


@pytest.mark.parametrize("path", ["/arcade", f"/arcade/{GAME}"])
def test_arcade_asks_terms_then_the_games_ai_and_save_work(served, page_of, monkeypatch, path):  # noqa: F811
    """ゲームの一覧かゲームを開くと、terms がまだなら同意の画面が出る。同意したらその画面に進み、AI と保存が使える。"""
    from tests.test_arcade_ai import MD, StubLLM

    server, _, _, _, base = served
    monkeypatch.setattr(server, "arcade_client", lambda: StubLLM())
    logic.set_search_track(server.state, _uid(), "正社員", "テスト")  # 状態は同意の判定に関わらない(雇用区分を決めておくだけ)
    make, errors = page_of
    page = make()
    _open(page, base, path)
    box = page.locator(".consent[data-kind=terms]")
    box.wait_for()
    assert consent.TERMS_INTRO in box.inner_text()
    assert not _agreed(server, "terms")
    page.locator("#consent-agree:not([disabled])").click()
    if path == "/arcade":
        page.locator(f"a[href='/liff/line/arcade/{GAME}']").click()
    page.wait_for_url(f"{base}/liff/line/arcade/{GAME}")
    page.wait_for_function("() => !!window.ArcadeAI && !!window.AICA_LIFF")
    assert _agreed(server, "terms") and logic.greeting_due(server.state, _uid())
    play = page.evaluate(f"ArcadeAI('{GAME}').start()")
    assert play["ai"] is True and play["id"]
    out = page.evaluate(f"([p, md]) => ArcadeAI('{GAME}').save(p, md, null)", [play, MD])
    assert out["view_url"] == "/liff/line/arcade/draft?rid=0"
    assert errors == []


def test_the_three_first_screens_fit_an_iphone_width(served, page_of):  # noqa: F811
    """iPhone の幅(390px)で、3 つの最初の画面が、同意の前も後も横にはみ出さない(task 241)。"""
    server, _, _, _, base = served
    make, errors = page_of
    ready = {"/voice": "#voice-open:not([disabled])", "/resume/upload": "#resume-send:not([disabled])", "/arcade": ".lf-game"}
    asked = {"/voice": "#consent-sheet #consent-agree", "/resume/upload": "#consent-agree:not([disabled])",
             "/arcade": "#consent-agree:not([disabled])"}
    page = make()
    page.set_viewport_size({"width": 390, "height": 844})
    for i, waits in enumerate((asked, ready)):
        if i:  # 同意した後の画面
            for k in ("terms", "resume", "voice"):
                consent.record(server.state, _uid(), k, consent.tag())
        for path, selector in waits.items():
            _open(page, base, path)
            page.locator(selector).first.wait_for()
            assert page.evaluate("document.documentElement.scrollWidth") <= 390, path
    assert errors == []


# ----------------------------- API とトーク -----------------------------


def test_liff_consent_records_terms_for_the_liff_user_only(line_liff):  # noqa: F811
    """/api/liff/consent は terms も受ける(本人は LIFF の Cookie)。示した版が今と違えば 409 で記録しない。
    terms と一緒に履歴書か音声の同意も 1 回で記録できる。LIFF の本人でなければ今と同じく断る。"""
    server, fake, _, _ = line_liff
    body = {"user_id": _uid(), "kind": "terms", "version": consent.tag()}
    with _liff_client(server) as client:
        assert client.post("/api/liff/consent", json=body).status_code == 401  # LIFF の Cookie が無い
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
        assert client.post("/api/liff/consent", json=body | {"user_id": line_user(RAW_OTHER)}).status_code == 403
        r = client.post("/api/liff/consent", json=body | {"version": "terms=0.9;privacy=0.9"})
        assert r.status_code == 409 and r.json()["consent"]["version"] == consent.tag()
        assert r.json()["consent"]["needed"] == ["terms", "resume", "voice"]
        assert client.post("/api/liff/consent", json=body | {"kind": "voice"}).status_code == 403  # terms の前
        assert client.post("/api/liff/consent", json=body | {"kind": "voice", "also": "resume"}).status_code == 422
        assert _uid() not in server.state.user_ids()

        r = client.post("/api/liff/consent", json=body | {"also": "voice"})
        assert r.status_code == 200 and r.json()["needed"] == ["resume"]
        assert logic.greeting_due(server.state, _uid())
        assert client.post("/api/liff/consent", json=body | {"kind": "resume"}).json()["needed"] == []
    assert _pushes(fake) == [] and [p for p, _ in fake.sent() if p == REPLY] == []  # 同意の直後に何も送らない


def test_after_agreeing_on_a_liff_page_the_talk_starts_with_the_greeting(line_liff):  # noqa: F811
    """画面で terms に同意した人には、トークで同意を求め直さない。次に話しかけたときは挨拶(3 つの入口の案内)から始め、
    書いた文から会話を始めない(入口 A にしない)。"""
    server, fake, _, model = line_liff
    with _liff_client(server) as client:
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
        r = client.post("/api/liff/consent", json={"user_id": _uid(), "kind": "terms", "version": consent.tag()})
        assert r.status_code == 200
        _post(client, [_event("message", text="渋谷で日中")])
    (sent,) = [b["messages"] for p, b in fake.sent() if p == REPLY]
    assert all("同意して始める" not in m.get("text", "") for m in sent)  # 同意を求め直さない
    assert sent[0]["text"].startswith("こんにちは")
    flex = [m for m in sent if m["type"] == "flex"]
    assert flex and [b["action"]["label"] for b in flex[0]["contents"]["body"]["contents"]] == [
        "音声で相談する", "履歴書を読み込む", "ゲームで書類を作る"]
    assert model.seen[0].startswith("[イベント: 初回訪問") and "渋谷で日中" not in model.seen
    assert not logic.greeting_due(server.state, _uid())  # 挨拶が返った


def test_answering_the_resume_follow_up_in_the_talk_ends_the_wait_for_the_greeting(line_liff):  # noqa: F811
    """履歴書の画面で同意して読み込むと、続きの返事がトークに push され、会話が始まる。その続きに答えたら、今の会話に続け
    (挨拶で切らない)、挨拶を待つ印も外す(後の日の新しい会話で、本人の発言を初回訪問の挨拶に替えない)。"""
    server, fake, _, model = line_liff
    files = {"file": (FILENAME, b"%PDF-1.4 dummy", "application/octet-stream")}
    with _liff_client(server) as client:
        assert client.post("/api/liff/session", json={"id_token": "good"}).status_code == 200
        body = {"user_id": _uid(), "kind": "terms", "also": "resume", "version": consent.tag()}
        assert client.post("/api/liff/consent", json=body).json()["needed"] == ["voice"]
        r = client.post("/api/liff/resume/upload", data={"user_id": _uid(), "id_token": "good"}, files=files)
        assert r.status_code == 200, r.text
        assert len(_pushes(fake)) == 1  # 確認と続きの返事(挨拶ではない)
        assert logic.greeting_due(server.state, _uid())  # 本人はまだトークで話していない
        _post(client, [_event("message", text="はい")])
    (sent,) = [b["messages"] for p, b in fake.sent() if p == REPLY]
    assert not sent[0]["text"].startswith("こんにちは")  # 今の会話に続けた
    assert model.seen[-1] == "はい"
    assert not logic.greeting_due(server.state, _uid())
    assert not server.state.user(_uid()).get("talk_opens")  # 挨拶は返っていないので、再訪にはしない
