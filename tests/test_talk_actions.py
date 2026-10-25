"""履歴書と音声の導線を会話の中へ(task 91)の台本テスト。実 LLM なし、dip の MCP は本物、履歴書の読み取りはスタブ。

- AI は返信の actions で「履歴書を選ぶ」(resume_upload)「音声相談を始める」(voice_consult)を差し出せる。
  quick_replies(本人の発言として送る選択肢)とは別で、/ と /dev の両方の画面が同じ部品(talk-core.js)で描く。
- アップロードは本人の1ターン: 本人側に「履歴書(PDF)」の吹き出し(ファイル名は出さない)→ AI 側に読み取り結果の
  確認 → resume_upload のイベントで AI が次の1問を返す。定型文を本人の発言として送らない。
- アップロードも、続くターンも会話数を1つずつ使う(既存の quota の決まり)。

画面を実際に押すテスト(Playwright)は、playwright と Chromium が入っているときだけ動く。
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from langchain_core.outputs import ChatGeneration, ChatResult

from dip_mcp import logic
from evals.checks import turn_violations
from line_runtime.runtime import _tool_payload
from tests.test_handle import ScriptedModel, call, reply
from tests.test_resume_intake import StubClient
from tests.test_resume_upload import FILENAME, PII, clean_draft
from tests.test_talk_page import (  # noqa: F401  app はフィクスチャ
    WORKER,
    app,
    open_talk,
    stream,
)

PROPOSE = ["いくつか伺いながら一緒に整理します。", "これまでのお仕事を教えてください。職務経歴書や履歴書があれば、読み込むと早いです。"]
ACTIONS = ["resume_upload", "voice_consult"]
MENU = ["求人を探す", "まず相談する", "応募履歴を確認", "今はいい"]


class Advisor(ScriptedModel):
    """dip の Prompt どおりに動く偽のモデル(何度でも返す): 挨拶はメニュー、「まず相談する」で経験を聞きながら
    履歴書・音声を差し出し、アップロードのイベントでは要約を読み直して次の1問を返す。"""

    seen: list = []  # noqa: RUF012  モデルに渡った本人側の入力(ファイル名が混ざっていないかを見る)
    contexts: list = []  # noqa: RUF012  モデルに渡った会話(音声相談の結果が文脈に入っているかを見る)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.respond(messages))])

    def respond(self, messages):
        uid = re.search(r"LINE ユーザーID: ([^(\s]+)", messages[0].content).group(1)
        last = max(i for i, m in enumerate(messages) if m.type == "human")
        human, turn = messages[last].content, messages[last + 1:]
        self.seen.append(human)
        self.contexts.append(list(messages))
        if human.startswith("[イベント: 本人が") and "読み取れた項目がありませんでした" in human:
            return reply(["読み取れなかったので、これまでのお仕事を1つ教えてください。"], quick_replies=["飲食", "販売", "今はいい"])
        if human.startswith("[イベント: 本人が音声相談を終えた]"):
            raise AssertionError("音声相談の結果のターンでは LLM を呼ばない")
        if human.startswith("[イベント: 本人が"):
            if not any(m.type == "tool" for m in turn):
                return call("get_user_conversation_summary", {"line_user_id": uid}, "sum")
            summary = next(_tool_payload(m) for m in turn if m.type == "tool")
            org = summary["uploaded_resume"]["work_history"][0]["organization"]
            return reply([f"{org}での接客のご経験を活かせる仕事で探しますか?"], quick_replies=["はい", "ほかも見たい", "今はいい"])
        if human.startswith("[イベント:"):
            return reply(["こんにちは。お仕事探しをお手伝いします。何をしますか?"], quick_replies=MENU)
        if human == "まず相談する":
            return reply(PROPOSE, quick_replies=["文字で話す", "今はいい"], actions=ACTIONS)
        return reply(["承知しました。これまでのお仕事を教えてください。"], quick_replies=["飲食", "販売", "今はいい"])


@pytest.fixture
def advised(app, monkeypatch):  # noqa: F811
    model = Advisor()
    model.seen = []
    model.contexts = []
    app.set_model(model)
    stub = StubClient(clean_draft(), usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    monkeypatch.setattr(app, "resume_client", lambda: stub)
    app.model = model
    yield app
    del app.model


def _upload(client, user, name=FILENAME, thread=None):
    return client.post("/api/resume/upload", data={"user_id": user}, params={"thread": thread} if thread is not None else {},
                       files={"file": (name, b"%PDF-1.4 dummy", "application/octet-stream")})


# ----------------------------- AI が差し出す操作(返信の actions) -----------------------------


def test_ai_offers_resume_and_voice_as_actions_on_the_talk_and_dev_screens(advised):
    from fastapi.testclient import TestClient

    with TestClient(advised.app) as client:
        opened, greeting = open_talk(client)
        assert greeting["actions"] == [] and greeting["quick_replies"] == MENU
        r = stream(client, opened["user"], "message", greeting["thread"], "まず相談する")[-1]
        assert r["kind"] == "reply" and r["actions"] == ACTIONS
        assert r["quick_replies"] == ["文字で話す", "今はいい"]  # 操作は自由文の選択肢とは別に届く
        assert turn_violations(0, r) == []  # 評価の機械判定(1問・逃げ道・80字)も通る

        # /dev のシナリオでも同じ返信の形(画面は同じ talk-core.js で描く)
        user = client.post("/api/scenarios/C-consult-propose/start").json()["user"]
        stream(client, user, "visit", None)
        dev = stream(client, user, "message", None, "まず相談する")[-1]
        assert dev["actions"] == ACTIONS

        # 描き直しでも操作は残る(選べるのは最後の返信だけ、は画面の側)
        hist = client.get("/api/talk/history").json()
        assert hist["turns"][-1]["bot"]["actions"] == ACTIONS


def test_actions_without_a_text_choice_are_flagged_by_the_checks():
    r = {"bubbles": ["これまでのお仕事を教えてください。"], "quick_replies": [], "cards": [], "buttons": [], "tools": [],
         "actions": ACTIONS}
    assert any("文字の選択肢" in v for v in turn_violations(0, r))
    assert turn_violations(0, r | {"quick_replies": ["接客", "事務", "今はいい"]}) == []


def test_the_screens_turn_actions_into_file_picking_and_the_voice_entry():
    """共通部品が actions を「ファイルを選ぶ」「話して伝える」にし、押すとファイル選択・音声の入口に進む。
    定型文を本人の発言として送るボタン・技術的なラベルは無い。"""
    from line_runtime import config

    static = config.ROOT / "line_runtime" / "static"
    core = (static / "talk-core.js").read_text(encoding="utf-8")
    assert 'resume_upload: { label: "📄 ファイルを選ぶ", run: () => pickResume() }' in core
    assert 'voice_consult: { label: "🎤 話して伝える(音声・約5分)", run: () => openVoice() }' in core
    assert "fileInput.click()" in core and '"/api/voice/start"' in core
    assert "encodeURIComponent(user)" not in core  # ユーザーIDは URL に載せない(本文で送る)
    assert "履歴書・職務経歴書を送る" in core and "音声で相談する" in core and "約5分で" in core  # 「+」メニュー
    for page in ("talk.html", "dev.html", "talk-core.js"):
        text = (static / page).read_text(encoding="utf-8")
        assert "この内容で相談する" not in text and "履歴書をアップロードしました" not in text, page
        assert "[履歴書]" not in text and "読み取っています" not in text, page


# ----------------------------- アップロードは本人の1ターン -----------------------------


def test_upload_is_the_users_turn_and_the_ai_continues_with_the_next_question(advised, tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(advised.app) as client:
        opened, greeting = open_talk(client)
        uid, thread = opened["user"], greeting["thread"]
        used = client.app.state.quota.used(uid)

        up = _upload(client, uid)
        assert up.status_code == 200 and up.json()["file"] == "履歴書(PDF)"
        evs = stream(client, uid, "resume_upload", thread)
        r = evs[-1]
        assert r["kind"] == "reply" and r["bubbles"] == ["コンビニみほん駅前店での接客のご経験を活かせる仕事で探しますか?"]
        assert [t["name"] for t in r["tools"]] == ["get_user_conversation_summary"]
        assert client.app.state.quota.used(uid) == used + 2  # アップロード1 + 続くターン1

        # モデルには、ファイルの種類と、画面に出した確認だけが渡る(ファイル名・定型文は無い)
        event = advised.model.seen[-1]
        assert event.startswith("[イベント: 本人が履歴書(PDF)をアップロード]")
        assert json.dumps(up.json()["bubbles"], ensure_ascii=False) in event and "この確認は繰り返さず" in event

        # 描き直すと、本人側はファイルの吹き出し、AI 側は確認 → 次の1問
        turn = client.get("/api/talk/history").json()["turns"][-1]
        assert turn["user"] is None
        # 読み取り結果の画面の URL は、そのアップロードを指す(ユーザーIDは載せない)
        assert turn["upload"] == {"file": "履歴書(PDF)", "shown": up.json()["bubbles"], "view_url": "/liff/resume?rid=0"}
        assert up.json()["view_url"] == "/liff/resume?rid=0"
        assert turn["bot"]["bubbles"] == r["bubbles"]
        # ファイル名(氏名が入りがち)はトーク・履歴・dip の状態のどこにも出ない
        for text in (up.text, json.dumps(evs, ensure_ascii=False), json.dumps(turn, ensure_ascii=False),
                     client.post("/api/dip", json={"user_id": uid}).text, (tmp_path / "state.json").read_text(encoding="utf-8")):
            assert FILENAME not in text and "佐藤" not in text
            assert not any(p in text for p in PII)


def test_an_upload_turn_needs_an_uploaded_resume_and_spends_no_turn_otherwise(advised):
    from fastapi.testclient import TestClient

    with TestClient(advised.app) as client:
        opened, greeting = open_talk(client)
        used = client.app.state.quota.used(opened["user"])
        r = client.post("/api/event/stream", json={"user_id": opened["user"], "event": "resume_upload",
                                                    "thread": greeting["thread"]})
        assert r.status_code == 400
        assert client.app.state.quota.used(opened["user"]) == used


def test_an_unreadable_upload_still_continues_the_talk_with_one_question(advised, monkeypatch):
    """読めなかったファイル(empty)でも、本人の1ターンとして会話を続ける: AI が読み取れなかった前提で1問返し、
    選択肢が付く。再読み込みしても、ファイルの吹き出し → 案内 → 1問 のまま残る(task 92 の追記)。"""
    from fastapi.testclient import TestClient

    from resume_draft.schema import ResumeDraft
    from tests.test_resume_intake import StubClient

    monkeypatch.setattr(advised, "resume_client", lambda: StubClient(ResumeDraft(unconfirmed=["画像が不鮮明"])))
    with TestClient(advised.app) as client:
        opened, greeting = open_talk(client)
        uid, thread = opened["user"], greeting["thread"]
        used = client.app.state.quota.used(uid)
        up = _upload(client, uid, name="scan.jpg")
        assert up.status_code == 200 and up.json()["empty"] is True and up.json()["file"] == "履歴書(画像)"
        r = stream(client, uid, "resume_upload", thread)[-1]
        assert r["kind"] == "reply" and r["bubbles"] == ["読み取れなかったので、これまでのお仕事を1つ教えてください。"]
        assert r["quick_replies"] == ["飲食", "販売", "今はいい"]
        assert client.app.state.quota.used(uid) == used + 2  # 読めたときと同じ(アップロード1 + 続くターン1)
        event = advised.model.seen[-1]
        assert event.startswith("[イベント: 本人が履歴書(画像)をアップロード] 読み取れた項目がありませんでした")
        assert json.dumps(up.json()["bubbles"], ensure_ascii=False) in event
        turn = client.get("/api/talk/history").json()["turns"][-1]
        assert turn["upload"] == {"file": "履歴書(画像)", "shown": up.json()["bubbles"], "view_url": None, "empty": True}
        assert turn["bot"]["bubbles"] == r["bubbles"]
        assert logic.resume_upload(advised.state, uid) is None  # 読めなかったものは dip に保存しない
        # 「読み取れなかった」の事実は使い終わり: アップロードなしの resume_upload は、読み取り結果が無いので断る
        assert client.post("/api/event/stream", json={"user_id": uid, "event": "resume_upload",
                                                       "thread": thread}).status_code == 400


def test_upload_and_the_following_turn_both_count_against_the_daily_quota(advised, monkeypatch):
    """上限の最後の1回をアップロードが使ったら、続くターンは LLM を呼ばずに上限の案内だけになる。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "2")  # 挨拶1 + アップロード1
    with TestClient(advised.app) as client:
        opened, greeting = open_talk(client)
        uid = opened["user"]
        assert _upload(client, uid).status_code == 200
        seen = len(advised.model.seen)
        r = stream(client, uid, "resume_upload", greeting["thread"])[-1]
        assert r["limited"] is True and "上限" in r["bubbles"][0] and r["actions"] == []
        assert len(advised.model.seen) == seen  # LLM は呼ばれていない
        assert _upload(client, uid).status_code == 429  # アップロードも上限で止まる


# ----------------------------- 画面を実際に押す(Playwright) -----------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(advised):
    """本物の HTTP サーバー(uvicorn)で画面を出す。"""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(advised.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
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


# LiveKit のブラウザ用ライブラリ(CDN)の代役。部屋への接続・マイク・データ送信をまねるだけで、音声は流さない
FAKE_LIVEKIT = """
window.__lk = { rooms: [], sent: [], connected: null, mic: false, disconnected: 0 };
window.LivekitClient = { Room: class {
  constructor() {
    this.h = {};
    this.localParticipant = { identity: "me",
      setMicrophoneEnabled: async (on, opts) => { window.__lk.mic = on && opts.echoCancellation === true; },
      publishData: async (data, opts) => { window.__lk.sent.push([JSON.parse(new TextDecoder().decode(data)), opts.topic]); } };
    window.__lk.rooms.push(this);
  }
  on(ev, fn) { this.h[ev] = fn; return this; }
  registerTextStreamHandler() {}
  async connect(url, token) { window.__lk.connected = { url, token }; }
  disconnect() { window.__lk.disconnected += 1; }
} };
"""
LIVEKIT_ENV = {"LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k",
               "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret"}


@pytest.fixture
def livekit(monkeypatch):
    for k, v in LIVEKIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AICA_INTERNAL_KEY", "offline-test-internal-key-offline-test")  # 無いと起動で止まる


def _fake_livekit(context) -> list[str]:
    """LiveKit のライブラリを代役に差し替え、画面が出したリクエストの URL と本文(「URL 本文」の1行)を集める
    (別タブが開かないことも見る)。"""
    requests: list[str] = []
    context.on("request", lambda req: requests.append(f"{req.url} {req.post_data or ''}"))
    context.route(re.compile(r"https://cdn\.jsdelivr\.net/npm/livekit-client.*"),
                  lambda route: route.fulfill(status=200, content_type="application/javascript", body=FAKE_LIVEKIT))
    return requests


def _open_and_close_the_voice_sheet(page, context, open_it) -> None:
    """音声相談を開くと同じ画面の中のシートが開き(別タブは開かない)、閉じるとトークに戻る。"""
    sheet = page.locator("#voice-sheet")
    assert sheet.is_hidden()
    open_it()
    sheet.wait_for(state="visible")
    page.wait_for_function("window.__lk && window.__lk.mic === true && !document.getElementById('voice-end').disabled")
    assert len(context.pages) == 1  # 別タブは開かない
    page.locator("#voice-close").click()
    # 通話中は確認を出す(誤って押して面談が切れないように)。「面談を続ける」なら閉じない
    page.locator("#voice-confirm").wait_for(state="visible")
    page.locator("#voice-keep").click()
    assert sheet.is_visible() and page.locator("#voice-confirm").is_hidden()
    page.locator("#voice-close").click()
    page.locator("#voice-quit").click()
    sheet.wait_for(state="hidden")
    assert page.evaluate("window.__lk.disconnected") >= 1


def _talk_through_actions(page, context) -> None:
    """「まず相談する」→ AI が差し出す [ファイルを選ぶ] [話して伝える] を押して進む(/ と /dev で同じ)。"""
    page.locator("#log .chips button", has_text="まず相談する").click()
    pick = page.locator("#log .chips button.action", has_text="ファイルを選ぶ")
    talk = page.locator("#log .chips button.action", has_text="話して伝える(音声・約5分)")
    pick.wait_for()
    assert talk.count() == 1 and page.locator("#log .chips button", has_text="文字で話す").count() == 1

    before = page.locator("#log .sys").count()
    _open_and_close_the_voice_sheet(page, context, talk.click)
    assert page.locator("#log .sys").count() == before  # 通知の吹き出しは出さない

    with page.expect_file_chooser() as chooser:
        pick.click()
    chooser.value.set_files({"name": FILENAME, "mimeType": "application/pdf", "buffer": b"%PDF-1.4 dummy"})
    page.locator(".row.me .bubble.file", has_text="履歴書(PDF)").wait_for()
    page.get_by_text("コンビニみほん駅前店での接客のご経験を活かせる仕事で探しますか?").wait_for()
    body = page.locator("#log").inner_text()
    assert "履歴書を読み取りました" in body and "読み取り結果を見る" in body
    assert "この内容で相談する" not in body and "履歴書をアップロードしました" not in body
    assert FILENAME not in page.content() and "佐藤" not in page.content()


def test_talk_screen_actions_and_plus_menu_in_the_browser(served, browser, livekit):
    context = browser.new_context()
    _fake_livekit(context)
    page = context.new_page()
    page.goto(served + "/")
    page.locator("#log .chips button", has_text="まず相談する").wait_for()
    assert page.locator("header button").count() == 0  # ヘッダーに常設のボタンは無い

    # 入力欄の左の「+」から、自分で履歴書・音声に入れる
    page.locator("#plus").click()
    assert page.locator("#plus-menu").is_visible()
    assert page.locator("#menu-resume").inner_text().startswith("📄")
    with page.expect_file_chooser():
        page.locator("#menu-resume").click()
    assert page.locator("#plus-menu").is_hidden()
    page.locator("#plus").click()
    _open_and_close_the_voice_sheet(page, context, page.locator("#menu-voice").click)

    _talk_through_actions(page, context)

    # 再読み込みしても、ファイルの吹き出し → 確認 → 次の1問 のまま描き直す
    page.reload()
    page.get_by_text("コンビニみほん駅前店での接客のご経験を活かせる仕事で探しますか?").wait_for()
    assert page.locator(".row.me .bubble.file", has_text="履歴書(PDF)").count() == 1
    assert "履歴書を読み取りました" in page.locator("#log").inner_text()
    context.close()


def test_dev_screen_draws_the_same_actions_in_the_browser(served, browser, livekit):
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    _fake_livekit(context)
    page = context.new_page()
    page.goto(served + "/dev")
    page.locator('#scenarios .act[data-id="C-consult-propose"]').click()
    page.locator("#log .chips button", has_text="まず相談する").wait_for()
    _talk_through_actions(page, context)
    page.locator("#trace", has_text="ユーザーが履歴書をアップロード").wait_for()
    assert page.locator("#state", has_text="読み取り済み").count() == 1
    context.close()


def _post_json(url: str, payload: dict) -> dict:
    """音声ワーカーの代わりに結果を送る(Cookie なし・共有鍵あり。ワーカーと同じ呼び方)。"""
    import urllib.request

    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"} | WORKER)
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read())


def test_voice_sheet_closes_and_the_result_continues_the_same_talk(served, browser, livekit, advised):
    """音声相談はトークの上のシートで開き、面談が終わるとシートが閉じて、同じトークに AI の返信として
    「話した内容のまとめ」「求人カード」「職務経歴書の下書きを見る」が続く(実際の音声は使わない。
    音声ワーカーの代わりに /api/voice/result へ結果を送る)。常時の問い合わせはせず、待つのはシートを開いている間だけ。"""
    from dip_mcp.data import load_jobs

    context = browser.new_context()
    requests = _fake_livekit(context)
    page = context.new_page()
    page.goto(served + "/")
    page.locator("#log .chips button", has_text="まず相談する").wait_for()
    page.locator("#log .chips button", has_text="まず相談する").click()
    talk = page.locator("#log .chips button.action", has_text="話して伝える(音声・約5分)")
    talk.wait_for()
    uid = next(c["value"] for c in context.cookies() if c["name"] == "aica_uid")
    used = advised.app.state.quota.used(uid)
    page.wait_for_timeout(1500)
    assert not any("/api/voice/" in u for u in requests)  # シートを開くまでは何も問い合わせない

    talk.click()
    page.locator("#voice-sheet").wait_for(state="visible")
    # マイクはエコーキャンセルつき。つながったら「面談を終える」が押せる
    page.wait_for_function("window.__lk && window.__lk.mic === true && !document.getElementById('voice-end').disabled")
    assert advised.app.state.quota.used(uid) == used + 1  # 音声相談は会話数を1つ使う(今と同じ)
    connected = page.evaluate("window.__lk.connected")
    assert connected["url"] == LIVEKIT_ENV["LIVEKIT_URL"] and connected["token"]
    waits = [u for u in requests if "/api/voice/wait " in u]
    assert waits and json.loads(waits[0].split(" ", 1)[1])["user_id"] == uid  # ユーザーIDは本文で送る
    assert not any(uid in u.split(" ", 1)[0] for u in requests)  # どの URL にもユーザーIDは無い
    status = page.locator("#voice-status")
    assert "アドバイザー" in status.inner_text()

    # 「面談を終える」→ ワーカーへ end を送り、まとめを待つ
    page.locator("#voice-end").click()
    page.wait_for_function("window.__lk.sent.length === 1")
    assert page.evaluate("window.__lk.sent") == [[{"type": "end"}, "voice-resume"]]
    assert "まとめています" in status.inner_text()

    # 別の面談(前に閉じたシートの部屋)の結果では、このシートは閉じない
    _post_json(served + "/api/voice/result", {"user_id": uid, "draft_markdown": "", "room": "voice-resume-other"})
    page.wait_for_timeout(1000)
    assert page.locator("#voice-sheet").is_visible()
    assert page.get_by_text("音声でお話しした内容をまとめました。").count() == 0

    # 音声ワーカーの代わりに結果を送る → シートが閉じ、同じトークに AI の返信が続く
    ids = sorted(load_jobs())[:2]
    room = json.loads(waits[0].split(" ", 1)[1])["room"]  # /start が発行した部屋(ワーカーはこの名前で送る)
    out = _post_json(served + "/api/voice/result", {
        "user_id": uid, "room": room, "jobs": [{"job_id": i, "reasons": ["夕方の勤務"]} for i in ids],
        "draft_markdown": "# 職務経歴書の下書き\n\n- カフェで1年勤務\n", "document_kind": "職務経歴書",
        "preferences": {"job_type": "カフェ", "time_band": "夕方", "min_wage": 1200}})
    assert out["talk"] is True
    page.locator("#voice-sheet").wait_for(state="hidden")
    last = page.locator("#log .row.bot").last
    last.get_by_text("音声でお話しした内容をまとめました。").wait_for()
    assert "職種: カフェ" in last.inner_text() and "時給: 1,200円以上" in last.inner_text()
    assert page.locator("#log .cards").last.locator(".jc").count() == 2
    draft = page.locator("#log .liff a", has_text="職務経歴書の下書きを見る")
    assert draft.count() == 1 and draft.get_attribute("href").endswith("/liff/voice/draft?rid=1")  # ユーザーIDは無い
    log = page.locator("#log").inner_text()
    assert "[音声相談]" not in log and "音声相談の結果が届きました" not in log
    assert page.locator("#log .chips button", has_text="ほかの求人も見たい").is_enabled()
    assert len(context.pages) == 1  # 別タブは開いていない
    assert advised.app.state.quota.used(uid) == used + 1  # 結果をトークに続けるのに会話数は使わない

    # シートを閉じたら問い合わせは止まる。常時の pull は無い
    waits = sum("/wait" in u for u in requests)
    page.wait_for_timeout(1500)
    assert sum("/wait" in u for u in requests) == waits
    assert not any("/pull" in u for u in requests)

    # 返信は会話に入っているので、再読み込みしても残り、次のテキストの会話もその続きになる
    page.reload()
    page.get_by_text("音声でお話しした内容をまとめました。").wait_for()
    assert page.locator("#log .liff a", has_text="職務経歴書の下書きを見る").count() == 1
    page.locator("#log .chips button", has_text="ほかの求人も見たい").click()
    page.get_by_text("承知しました。これまでのお仕事を教えてください。").wait_for()
    context.close()


def test_voice_result_is_saved_in_the_conversation_without_calling_the_llm(advised):
    """結果は今の会話に AI の返信として入る(LLM は呼ばない)。履歴では本人側の発言は無く、返信だけ。"""
    from fastapi.testclient import TestClient

    from line_runtime.runtime import VOICE_EVENT

    with TestClient(advised.app) as client:
        opened, greeting = open_talk(client)
        uid = opened["user"]
        stream(client, uid, "message", greeting["thread"], "まず相談する")
        seen = len(advised.model.seen)
        out = client.post("/api/voice/result", headers=WORKER, json={"user_id": uid, "jobs": [], "draft_markdown": "",
                                                                      "preferences": {"area": "渋谷"}}).json()
        assert out["talk"] is True and len(advised.model.seen) == seen
        turns = client.get("/api/talk/history").json()["turns"]
        assert turns[-1]["user"] is None and turns[-1].get("upload") is None
        assert turns[-1]["bot"]["bubbles"][0] == "音声でお話しした内容をまとめました。\nエリア: 渋谷"
        assert turns[-1]["bot"]["buttons"] == [] and turns[-1]["bot"]["cards"] == []
        # 次のターンは同じ会話の続き(LLM には音声相談を終えたことが伝わる)
        r = stream(client, uid, "message", greeting["thread"], "条件を変えて探す")[-1]
        assert r["kind"] == "reply"
        msgs = advised.model.contexts[-1]
        assert any(m.type == "human" and m.content.startswith(VOICE_EVENT) for m in msgs)


def test_an_upload_from_an_old_tab_is_not_read_and_spends_no_turn(advised):
    """別のタブで新しい会話が始まった後に古いタブからアップロードしても、読み取らず会話数も使わない(409)。
    画面は最新の会話を描き直す(発言のときの stale と同じ)。"""
    from fastapi.testclient import TestClient

    with TestClient(advised.app) as client:
        old, old_greeting = open_talk(client)
        advised.later(31)
        _, new_greeting = open_talk(client)
        uid = old["user"]
        assert new_greeting["thread"] == old_greeting["thread"] + 1
        used = client.app.state.quota.used(uid)
        r = _upload(client, uid, thread=old_greeting["thread"])
        assert r.status_code == 409
        assert client.app.state.quota.used(uid) == used
        ok = _upload(client, uid, thread=new_greeting["thread"])
        assert ok.status_code == 200
