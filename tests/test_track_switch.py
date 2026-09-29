"""雇用区分の切り替えを見せて取り消せる(task 96)の台本テスト。実 LLM なし、dip の MCP は本物。

確かめること:
(1) 区分が切り替わったターンの返信にだけ、切り替えの表示(track_switch)が付く。切り替わらないターン・今と同じ区分で
    呼んだターン・根拠の無い呼び出しで記録されなかったターンには付かない
(2) 表示は dip の記録(set_search_track の recorded)から作り、返信文には依存しない(文で「切り替えた」と言っても、
    記録が無ければ出さない。文で何も言わなくても、記録があれば出す)
(3) 表示のボタン(/api/talk/{user}/track/undo)で元の区分に戻り、dip に取り消しとして記録される(根拠はボタンを
    押したこと)。次のターンの前に知らせが届き、AI は要約を読み直して、戻した区分で続ける(同じ切り替えを繰り返さない)
(4) 直前の切り替えでなければ(後に別の切り替え・取り消し済み・記録ごと消した)取り消さない。他人は取り消せない
(5) ブラウザ: ヘッダーに今の区分が出て、表示は AI の返信の前に出て、ボタンで戻る。再読み込みしても dip の記録どおり。
    プロフィールの「記録ごと消す」は取り消しと別のもの(確認つき)
(6) 追記(プロフィールのレビューから): 音声のシートを結果の前に閉じても、END_WAIT_MS を過ぎたら履歴の読み直しをやめる
"""

from __future__ import annotations

import re

import pytest
from langchain_core.outputs import ChatGeneration, ChatResult

from dip_mcp import logic, profile
from line_runtime.runtime import _tool_payload
from tests.test_handle import ScriptedModel, call, reply
from tests.test_talk_actions import (  # noqa: F401  フィクスチャ
    _free_port,
    browser,
    livekit,
)
from tests.test_talk_page import (  # noqa: F401  app はフィクスチャ
    app,
    open_talk,
    stream,
)
from tests.test_track import switch

NOTICE = "[お知らせ: 本人がプロフィール画面で"
WELCOME = "こんにちは。お仕事探しをお手伝いします。何をしますか?"
# 本人の発言 → 台本のモデルが呼ぶ set_search_track(区分, 根拠)。根拠が空なら dip が記録しない
SWITCHES = {"正社員で探したい": ("正社員", "正社員で探したい"), "転職を考えています": ("正社員", "転職を考えています"),
            "バイトを探しています": ("アルバイト・パート", "バイトを探しています"),
            "正社員でお願いします": ("正社員", "正社員でお願いします"), "根拠なしで切り替えて": ("正社員", ""),
            "正社員も考えたい": ("正社員", "正社員も考えたい")}
SILENT = "正社員も考えたい"  # 切り替えても返信では何も言わない(表示が返信文に依存しないことを見る)
CLAIM = "正社員って言ったことにして"  # 返信では「切り替えた」と言うが、ツールは呼ばない


class Switcher(ScriptedModel):
    """発言に応じて区分を切り替える偽のモデル(何度でも返す)。返信は、このターンで分かった今の区分で話す:
    set_search_track の結果 > 要約の search_track > プロフィールの知らせ。知らせが「読み直し」を求めたら要約を読む。"""

    contexts: list = []  # noqa: RUF012  モデルに渡った会話

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.contexts.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.respond(messages))])

    @staticmethod
    def respond(messages):
        uid = re.search(r"LINE ユーザーID: ([^(\s]+)", messages[0].content).group(1)
        last = max(i for i, m in enumerate(messages) if m.type == "human")
        human, turn = messages[last].content, messages[last + 1:]
        notice = messages[last - 1].content if messages[last - 1].type == "human" else ""
        tools = {m.name: _tool_payload(m) for m in turn if m.type == "tool"}
        if human.startswith("[イベント:"):
            return reply([WELCOME], quick_replies=["今はいい"])
        if human in SWITCHES and "set_search_track" not in tools:
            return switch(*SWITCHES[human], uid=uid)
        if notice.startswith(NOTICE) and "読み直し" in notice and "get_user_conversation_summary" not in tools:
            return call("get_user_conversation_summary", {"line_user_id": uid}, "sum")
        if human == CLAIM:
            return reply(["正社員のお仕事に切り替えました。"])
        if human == SILENT:
            return reply(["希望の働き方を教えてください。"])
        track = (tools.get("set_search_track") or {}).get("track") \
            or (tools.get("get_user_conversation_summary") or {}).get("search_track")
        return reply([f"{track}のお仕事で考えますね。" if track else "わかりました。"])


@pytest.fixture
def switched(app, monkeypatch):  # noqa: F811
    model = Switcher()
    model.contexts = []
    app.set_model(model)
    app.model = model
    yield app
    del app.model


def _client(app):  # noqa: F811
    from fastapi.testclient import TestClient

    return TestClient(app.app)


def _last_record(app, uid: str) -> dict:  # noqa: F811
    return app.state.user(uid)["track_history"][-1]


# ----------------------------- (1)(2) 切り替わったターンにだけ、dip の記録から出す -----------------------------


def test_the_switch_shows_only_on_the_turn_that_switched(switched):
    with _client(switched) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        assert opened["search_track"] == "未定" and greeting["track_switch"] is None

        r = stream(client, uid, "message", th, "正社員で探したい")[-1]
        rec = _last_record(switched, uid)
        assert r["track_switch"] == {"id": rec["id"], "from": "未定", "track": "正社員", "undo": False, "status": "undoable"}
        assert r["search_track"] == "正社員"

        # 切り替わらないターン・今と同じ区分で呼んだターン(dip は記録しない)には出ない
        for said in ("ありがとう", "正社員でお願いします"):
            r = stream(client, uid, "message", th, said)[-1]
            assert r["track_switch"] is None and r["search_track"] == "正社員", said
        assert len(switched.state.user(uid)["track_history"]) == 1

        # 次の切り替えが出ると、前の表示はもう取り消せない(再読み込みの履歴も dip の記録どおり)
        r = stream(client, uid, "message", th, "バイトを探しています")[-1]
        assert r["track_switch"]["from"] == "正社員" and r["track_switch"]["status"] == "undoable"
        turns = client.get(f"/api/talk/history/{uid}").json()
        assert turns["search_track"] == "アルバイト・パート"
        shown = [t["bot"]["track_switch"] for t in turns["turns"]]
        assert [s and (s["track"], s["status"]) for s in shown] == [
            None, ("正社員", "past"), None, None, ("アルバイト・パート", "undoable")]

        # /api/event(ストリームでない呼び方)も同じ形
        r = client.post("/api/event", json={"user_id": uid, "event": "message", "text": "転職を考えています",
                                            "thread": th}).json()
        assert r["track_switch"]["track"] == "正社員" and r["track_switch"]["status"] == "undoable"


def test_the_switch_comes_from_the_dip_record_not_from_the_reply_text(switched):
    with _client(switched) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        # 返信で「切り替えました」と言っても、dip に記録が無ければ出さない
        r = stream(client, uid, "message", th, CLAIM)[-1]
        assert "切り替えました" in r["bubbles"][0] and r["track_switch"] is None and r["search_track"] == "未定"
        # 根拠の無い呼び出しは dip が断る(記録なし)ので出さない
        r = stream(client, uid, "message", th, "根拠なしで切り替えて")[-1]
        assert "error" in r["tools"][0]["result"] and r["track_switch"] is None

        # 返信では切り替えの話をしなくても、記録があれば出す
        r = stream(client, uid, "message", th, SILENT)[-1]
        assert r["bubbles"] == ["希望の働き方を教えてください。"]
        assert r["track_switch"]["track"] == "正社員" and r["track_switch"]["id"] == _last_record(switched, uid)["id"]


# ----------------------------- (3) ボタンで取り消す -----------------------------


def test_the_button_undoes_the_switch_and_the_next_reply_continues_in_the_restored_track(switched):
    with _client(switched) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        logic.set_search_track(switched.state, uid, "アルバイト・パート", "空いた時間にバイトを")
        shown = stream(client, uid, "message", th, "正社員で探したい")[-1]["track_switch"]
        assert shown["from"] == "アルバイト・パート" and shown["status"] == "undoable"
        used = client.app.state.quota.used(uid)
        calls = len(switched.model.contexts)

        r = client.post(f"/api/talk/{uid}/track/undo", json={"id": shown["id"]})
        assert r.status_code == 200
        assert r.json() == {"search_track": "アルバイト・パート", "undone": "正社員", "status": "undone"}
        # dip に取り消しとして記録(根拠はボタンを押したこと)。LLM は呼ばず、会話数も使わない
        s = logic.conversation_summary(switched.state, uid)
        assert s["search_track"] == "アルバイト・パート"
        assert s["track_history"][-1] | {"when": None} == {"when": None, "from": "正社員", "track": "アルバイト・パート",
                                                           "evidence": profile.UNDO_BY_BUTTON, "undo": True}
        assert len(switched.model.contexts) == calls and client.app.state.quota.used(uid) == used
        # もう一度押しても取り消さない。履歴の表示も「取り消した」になる
        again = client.post(f"/api/talk/{uid}/track/undo", json={"id": shown["id"]})
        assert again.status_code == 409 and "取り消して" in again.json()["detail"]
        h = client.get(f"/api/talk/history/{uid}").json()
        assert h["turns"][-1]["bot"]["track_switch"]["status"] == "undone" and h["search_track"] == "アルバイト・パート"

        # 次のターン: 発言の直前に知らせが入り、AI は要約を読み直して、戻した区分で返す(切り替え直さない)
        r = stream(client, uid, "message", th, "よろしく")[-1]
        assert [t["name"] for t in r["tools"]] == ["get_user_conversation_summary"]
        assert r["tools"][0]["result"]["search_track"] == "アルバイト・パート"
        assert r["bubbles"] == ["アルバイト・パートのお仕事で考えますね。"]
        assert r["track_switch"] is None and r["search_track"] == "アルバイト・パート"
        ctx = switched.model.contexts[-1]
        notice = next(m.content for m in ctx if m.type == "human" and m.content.startswith(NOTICE))
        assert "雇用区分の「正社員」への切り替えを取り消して「アルバイト・パート」に戻した" in notice
        assert "get_user_conversation_summary を読み直し" in notice and "「正社員」へすぐに切り替え直さない" in notice
        assert switched.state.user(uid).get("profile_changes") == []  # 渡し終えた
        # その次のターンには知らせを持ち越さない
        stream(client, uid, "message", th, "ありがとう")
        assert not any(m.type == "human" and m.content.startswith(NOTICE)
                       for m in switched.model.contexts[-1][-3:])


def test_the_prompt_tells_the_ai_not_to_repeat_an_undone_switch(switched):
    with _client(switched) as client:
        open_talk(client)
    system = switched.model.contexts[-1][0].content
    assert "元に戻すボタン" in system and "同じ切り替えを繰り返さない" in system


# ----------------------------- (4) 取り消せないとき -----------------------------


def test_only_the_latest_switch_can_be_undone_and_only_by_its_owner(switched):
    with _client(switched) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        first = stream(client, uid, "message", th, "正社員で探したい")[-1]["track_switch"]
        second = stream(client, uid, "message", th, "バイトを探しています")[-1]["track_switch"]
        r = client.post(f"/api/talk/{uid}/track/undo", json={"id": first["id"]})
        assert r.status_code == 409 and "このあとに区分が切り替わっている" in r.json()["detail"]

        # 他人のブラウザ・Cookie の無い呼び出しは取り消せない
        other = _client(switched)
        other.get("/api/me")
        assert other.post(f"/api/talk/{uid}/track/undo", json={"id": second["id"]}).status_code == 403
        assert _client(switched).post(f"/api/talk/{uid}/track/undo", json={"id": second["id"]}).status_code == 403
        assert logic.search_track(switched.state.user(uid)) == "アルバイト・パート"

        # プロフィールの「記録ごと消す」は取り消しとは別: 記録が無くなり、表示のボタンからは戻せない
        client.delete(f"/api/profile/{uid}/track")
        r = client.post(f"/api/talk/{uid}/track/undo", json={"id": second["id"]})
        assert r.status_code == 409 and "記録が消されています" in r.json()["detail"]
        h = client.get(f"/api/talk/history/{uid}").json()
        assert h["turns"][-1]["bot"]["track_switch"]["status"] == "gone" and h["search_track"] == "未定"

        # AI が会話で取り消した切り替え(undo の記録)は「戻しました」と出し、ボタンでは取り消せない
        stream(client, uid, "message", th, "正社員で探したい")
        logic.set_search_track(switched.state, uid, "未定", "やっぱり違う", undo=True)
        undo_id = _last_record(switched, uid)["id"]
        assert profile.switch_status(switched.state, uid, undo_id) == "past"
        assert client.post(f"/api/talk/{uid}/track/undo", json={"id": undo_id}).status_code == 409


# ----------------------------- (5) ブラウザ(Playwright と Chromium があるときだけ) -----------------------------


@pytest.fixture
def served_switch(switched):
    import threading
    import time

    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(switched.app, host="127.0.0.1", port=port, log_level="warning"))
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


def _say(page, text: str) -> None:
    page.locator("#t").fill(text)
    page.locator("#send").click()


def test_the_switch_and_its_undo_in_the_browser(switched, browser, served_switch):  # noqa: F811
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(served_switch + "/")
    page.get_by_text(WELCOME).wait_for()
    uid = next(c["value"] for c in context.cookies() if c["name"] == "aica_uid")
    header = page.locator("header #track")
    assert header.inner_text() == "区分は未定"

    # 切り替わらないターンには出ない
    _say(page, "ありがとう")
    page.get_by_text("わかりました。").wait_for()
    assert page.locator(".track-switch").count() == 0

    logic.set_search_track(switched.state, uid, "アルバイト・パート", "空いた時間にバイトを")
    _say(page, "正社員で探したい")
    page.get_by_text("正社員のお仕事で考えますね。").wait_for()
    sw = page.locator(".track-switch")
    assert sw.count() == 1 and sw.inner_text().startswith("正社員の相談に切り替えました")
    assert header.inner_text() == "正社員で相談中"
    # 表示は AI の返信の前(本人の発言 → 表示 → 返信)
    order = page.evaluate("""() => [...document.querySelectorAll('#log > .row, #log > .track-switch')].slice(-3).map(e =>
        e.classList.contains('track-switch') ? 'switch' : e.classList.contains('me') ? 'me' : 'bot')""")
    assert order == ["me", "switch", "bot"]
    assert "set_search_track" not in page.locator("#log").inner_text()  # 技術的なラベルは出さない

    sw.locator("button", has_text="アルバイトで探す").click()
    sw.get_by_text("取り消して、アルバイト・パートに戻しました").wait_for()
    assert sw.locator("button").count() == 0
    assert header.inner_text() == "アルバイト・パートで相談中"
    assert logic.search_track(switched.state.user(uid)) == "アルバイト・パート"

    # 次の返信は戻した区分で続く
    _say(page, "よろしく")
    page.get_by_text("アルバイト・パートのお仕事で考えますね。").wait_for()
    assert page.locator(".track-switch").count() == 1  # この返信では切り替わっていない

    # 再読み込みしても、dip の記録どおり(取り消し済みでボタンは出ない)
    page.reload()
    page.locator(".track-switch", has_text="取り消して、アルバイト・パートに戻しました").wait_for()
    assert page.locator(".track-switch button").count() == 0
    assert header.inner_text() == "アルバイト・パートで相談中"

    # プロフィール: 取り消しは記録に残り、「記録ごと消す」は確認つきの別の操作
    page.locator("#nav button[data-view=profile]").click()
    row = page.locator('#profile .prow[data-key="track"]')
    row.wait_for()
    assert "取り消し: 正社員 → アルバイト・パート" in row.inner_text()
    row.locator("button.del", has_text="記録ごと消す").click()
    row.locator(".pconfirm", has_text="直前の切り替えだけを元に戻すときは").wait_for()
    assert logic.search_track(switched.state.user(uid)) == "アルバイト・パート"  # まだ消さない
    row.locator(".pconfirm button.danger").click()
    page.locator('#profile .prow[data-key="track"] .pv', has_text="未定").wait_for()
    assert header.inner_text() == "区分は未定"
    context.close()


# ----------------------------- (6) 閉じた音声シートの待ちは END_WAIT_MS で終わる -----------------------------


def test_a_closed_voice_sheet_stops_redrawing_the_history_after_the_end_wait(
        switched, browser, served_switch, livekit):  # noqa: F811
    """通話中に「終了して閉じる」でワーカーが結果を送らなかったとき、閉じてから END_WAIT_MS(2分)を過ぎたら、
    発言のたびに履歴を読み直して描き直すのをやめる(描き直すと、上限の案内や失敗の吹き出しが消えてしまう)。"""
    from tests.test_talk_actions import _fake_livekit

    context = browser.new_context()
    requests = _fake_livekit(context)
    page = context.new_page()
    page.clock.install()
    page.goto(served_switch + "/")
    page.get_by_text(WELCOME).wait_for()
    page.locator("#plus").click()
    page.locator("#menu-voice").click()
    page.wait_for_function("window.__lk && window.__lk.mic === true && !document.getElementById('voice-end').disabled")
    page.locator("#voice-close").click()
    page.locator("#voice-quit").click()
    page.locator("#voice-sheet").wait_for(state="hidden")
    history = lambda: sum("/api/talk/history/" in u for u in requests)

    before = history()
    _say(page, "ありがとう")  # 閉じた直後は、結果が届いているかもしれないので読み直す
    page.get_by_text("わかりました。").last.wait_for()
    assert history() == before + 1
    page.evaluate("document.getElementById('log').appendChild(Object.assign(document.createElement('div'), "
                  "{id: 'kept', textContent: '描き直しで消えないもの'}))")

    page.clock.fast_forward(121_000)
    before = history()
    _say(page, "ありがとう")
    page.wait_for_function("document.querySelectorAll('#log .row.bot').length >= 3")
    page.locator("#log .row.bot").last.get_by_text("わかりました。").wait_for()
    assert history() == before  # もう読み直さない
    assert page.locator("#kept").count() == 1
    context.close()


def test_history_and_open_carry_the_current_track_for_the_header(switched):
    with _client(switched) as client:
        opened, greeting = open_talk(client)
        uid = opened["user"]
        stream(client, uid, "message", greeting["thread"], "転職を考えています")
        reopened = client.post("/api/talk/open").json()
        assert reopened["event"] is None and reopened["search_track"] == "正社員"
        h = client.get(f"/api/talk/history/{uid}").json()
        assert [t["bot"]["track_switch"] and t["bot"]["track_switch"]["track"] for t in h["turns"]] == [None, "正社員"]
