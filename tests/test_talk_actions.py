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

from evals.checks import turn_violations
from line_runtime.runtime import _tool_payload
from tests.test_handle import ScriptedModel, call, reply
from tests.test_resume_intake import StubClient
from tests.test_resume_upload import FILENAME, PII, clean_draft
from tests.test_talk_page import (  # noqa: F401  app はフィクスチャ
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

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.respond(messages))])

    def respond(self, messages):
        uid = re.search(r"LINE ユーザーID: ([^(\s]+)", messages[0].content).group(1)
        last = max(i for i, m in enumerate(messages) if m.type == "human")
        human, turn = messages[last].content, messages[last + 1:]
        self.seen.append(human)
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
    app.set_model(model)
    stub = StubClient(clean_draft(), usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    monkeypatch.setattr(app, "resume_client", lambda: stub)
    app.model = model
    yield app
    del app.model


def _upload(client, user, name=FILENAME):
    return client.post(f"/api/resume/{user}/upload", files={"file": (name, b"%PDF-1.4 dummy", "application/octet-stream")})


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
        hist = client.get(f"/api/talk/history/{opened['user']}").json()
        assert hist["turns"][-1]["bot"]["actions"] == ACTIONS


def test_actions_without_a_text_choice_are_flagged_by_the_checks():
    r = {"bubbles": ["これまでのお仕事を教えてください。"], "quick_replies": [], "cards": [], "buttons": [], "tools": [],
         "actions": ACTIONS}
    assert any("文字で話す" in v for v in turn_violations(0, r))
    assert turn_violations(0, r | {"quick_replies": ["文字で話す", "今はいい"]}) == []


def test_the_screens_turn_actions_into_file_picking_and_the_voice_entry():
    """共通部品が actions を「ファイルを選ぶ」「話して伝える」にし、押すとファイル選択・音声の入口に進む。
    定型文を本人の発言として送るボタン・技術的なラベルは無い。"""
    from line_runtime import config

    static = config.ROOT / "line_runtime" / "static"
    core = (static / "talk-core.js").read_text(encoding="utf-8")
    assert 'resume_upload: { label: "📄 ファイルを選ぶ", run: () => pickResume() }' in core
    assert 'voice_consult: { label: "🎤 話して伝える(音声・約5分)", run: () => openVoice() }' in core
    assert "fileInput.click()" in core and '"/liff/voice?user="' in core
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
        turn = client.get(f"/api/talk/history/{uid}").json()["turns"][-1]
        assert turn["user"] is None
        assert turn["upload"] == {"file": "履歴書(PDF)", "shown": up.json()["bubbles"], "view_url": f"/liff/resume?user={uid}"}
        assert turn["bot"]["bubbles"] == r["bubbles"]
        # ファイル名(氏名が入りがち)はトーク・履歴・dip の状態のどこにも出ない
        for text in (up.text, json.dumps(evs, ensure_ascii=False), json.dumps(turn, ensure_ascii=False),
                     client.get(f"/api/dip/{uid}").text, (tmp_path / "state.json").read_text(encoding="utf-8")):
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


def _voice_opens(context) -> list[str]:
    """音声の入口(/liff/voice)が開かれた URL を集める(voice-resume は動かさない)。"""
    opened: list[str] = []

    def handle(route):
        opened.append(route.request.url)
        route.fulfill(status=200, content_type="text/html", body="<p>voice</p>")

    context.route(re.compile(r".*/liff/voice\?user=.*"), handle)
    return opened


def _talk_through_actions(page, context, voice: list[str]) -> None:
    """「まず相談する」→ AI が差し出す [ファイルを選ぶ] [話して伝える] を押して進む(/ と /dev で同じ)。"""
    page.locator("#log .chips button", has_text="まず相談する").click()
    pick = page.locator("#log .chips button.action", has_text="ファイルを選ぶ")
    talk = page.locator("#log .chips button.action", has_text="話して伝える(音声・約5分)")
    pick.wait_for()
    assert talk.count() == 1 and page.locator("#log .chips button", has_text="文字で話す").count() == 1

    notices = page.locator(".sys", has_text="話すと約5分で履歴書の下書きができ")
    before = notices.count()
    with context.expect_page() as popup:
        talk.click()
    popup.value.wait_for_load_state()
    assert len(voice) == 1 and notices.count() == before + 1

    with page.expect_file_chooser() as chooser:
        pick.click()
    chooser.value.set_files({"name": FILENAME, "mimeType": "application/pdf", "buffer": b"%PDF-1.4 dummy"})
    page.locator(".row.me .bubble.file", has_text="履歴書(PDF)").wait_for()
    page.get_by_text("コンビニみほん駅前店での接客のご経験を活かせる仕事で探しますか?").wait_for()
    body = page.locator("#log").inner_text()
    assert "履歴書を読み取りました" in body and "読み取り結果を見る" in body
    assert "この内容で相談する" not in body and "履歴書をアップロードしました" not in body
    assert FILENAME not in page.content() and "佐藤" not in page.content()


def test_talk_screen_actions_and_plus_menu_in_the_browser(served, browser):
    context = browser.new_context()
    voice = _voice_opens(context)
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
    with context.expect_page():
        page.locator("#menu-voice").click()
    assert len(voice) == 1
    voice.clear()

    _talk_through_actions(page, context, voice)

    # 再読み込みしても、ファイルの吹き出し → 確認 → 次の1問 のまま描き直す
    page.reload()
    page.get_by_text("コンビニみほん駅前店での接客のご経験を活かせる仕事で探しますか?").wait_for()
    assert page.locator(".row.me .bubble.file", has_text="履歴書(PDF)").count() == 1
    assert "履歴書を読み取りました" in page.locator("#log").inner_text()
    context.close()


def test_dev_screen_draws_the_same_actions_in_the_browser(served, browser):
    context = browser.new_context(viewport={"width": 1400, "height": 900})
    voice = _voice_opens(context)
    page = context.new_page()
    page.goto(served + "/dev")
    page.locator('#scenarios .act[data-id="C-consult-propose"]').click()
    page.locator("#log .chips button", has_text="まず相談する").wait_for()
    _talk_through_actions(page, context, voice)
    page.locator("#trace", has_text="ユーザーが履歴書をアップロード").wait_for()
    assert page.locator("#state", has_text="読み取り済み").count() == 1
    context.close()


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
        r = client.post(f"/api/resume/{uid}/upload", params={"thread": old_greeting["thread"]},
                        files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/octet-stream")})
        assert r.status_code == 409
        assert client.app.state.quota.used(uid) == used
        ok = client.post(f"/api/resume/{uid}/upload", params={"thread": new_greeting["thread"]},
                         files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/octet-stream")})
        assert ok.status_code == 200
