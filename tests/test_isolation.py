"""公開前の守り(task 99): ユーザーの分離。

- ブラウザから呼ぶ経路は、正しい形式の Cookie が必須(無い・不正なら 401)。他人の匿名IDを本文や URL に入れても
  読めない・話せない(403 か、Cookie の本人の分だけ)。
- Cookie 無しの /api/talk/open は ID を発行せず、dip の状態ファイルにユーザーを増やさない。
- サーバー間の経路(音声ワーカー → /api/voice/result)は共有鍵が無いと 401。
- 会話数の上限は、Cookie 無しや ID の詐称で迂回できない。
- 本人のデータを指す URL(音声の入口・下書き・履歴書の読み取り結果・応募フォーム)にユーザーIDが出ない。
- dip のツールの line_user_id は会話のユーザーに固定する(LLM が別の ID を渡しても他人のデータに触れない)。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from dip_mcp import logic
from dip_mcp.data import State
from resume_draft.pii import has_pii, redact_pii
from tests.test_handle import ScriptedModel, call, reply
from tests.test_revisit import Greeter, greet
from tests.test_talk_page import (  # noqa: F401  app はフィクスチャ
    INTERNAL_KEY,
    WORKER,
    app,
    open_talk,
)

OTHER = "w-0123456789abcdef"  # 知られてしまった他人の匿名ID


def _browser(app):  # noqa: F811
    from fastapi.testclient import TestClient

    c = TestClient(app.app)
    c.get("/")
    return c


# ブラウザから呼ぶ、本人のデータを読む・話す・音声結果を取る経路(Cookie の本人しか扱えない)
def _routes(uid: str) -> list[tuple[str, str, dict]]:
    upload = {"data": {"user_id": uid}, "files": {"file": ("r.pdf", b"%PDF-1.4 dummy", "application/pdf")}}
    return [
        ("GET", "/api/me", {}),
        ("POST", "/api/talk/open", {}),
        ("GET", "/api/talk/history", {}),
        ("GET", "/api/saved", {}),
        ("POST", "/api/dip", {"json": {"user_id": uid}}),
        ("POST", "/api/scenarios/F-follow-new/start", {}),
        ("POST", "/api/event", {"json": {"user_id": uid, "event": "message", "text": "hi"}}),
        ("POST", "/api/event/stream", {"json": {"user_id": uid, "event": "message", "text": "hi"}}),
        ("POST", "/api/talk/track/undo", {"json": {"user_id": uid, "id": "x"}}),
        ("POST", "/api/voice/start", {"json": {"user_id": uid}}),
        ("POST", "/api/voice/wait", {"json": {"user_id": uid, "since": 0, "timeout": 0}}),
        ("POST", "/api/resume/upload", upload),
        ("GET", "/liff/voice/draft?rid=0", {}),
        ("GET", "/liff/resume?rid=0", {}),
        ("GET", "/liff/apply?job=a002", {}),
        ("POST", "/liff/apply", {"data": {"job": "a002"}}),
        # わたしのプロフィール(パスにユーザーIDは無い。Cookie の本人の分だけ)
        ("GET", "/api/profile", {}),
        ("PUT", "/api/profile/items/area", {"json": {"value": "新宿"}}),
        ("DELETE", "/api/profile/items/area", {}),
        ("PUT", "/api/profile/track", {"json": {"track": "正社員"}}),
        ("DELETE", "/api/profile/track", {}),
        ("DELETE", "/api/profile/resume", {}),
        ("DELETE", "/api/profile/voice/0", {}),
        ("DELETE", "/api/profile/saved/a003", {}),
        ("POST", "/api/profile/forget", {}),
    ]


@pytest.mark.parametrize("cookies", [{}, {"aica_uid": "x"}, {"aica_uid": "w-XYZ"}, {"aica_uid": f"{OTHER}:sim-new"}],
                         ids=["no-cookie", "malformed", "not-hex", "scenario-user-as-cookie"])
def test_every_browser_route_needs_a_well_formed_cookie(app, cookies):  # noqa: F811
    """Cookie が無い・形式が不正なら、どの経路も 401。LLM も呼ばず、dip の状態ファイルにも書かない。"""
    from fastapi.testclient import TestClient

    model = Greeter(script=[])  # 呼ばれたら台本が尽きて落ちる
    app.set_model(model)
    with TestClient(app.app):
        before = app.state.path.read_bytes()
        nc = TestClient(app.app, cookies=cookies)
        for method, path, kw in _routes(OTHER):
            r = nc.request(method, path, **kw)
            assert r.status_code == 401, (path, r.status_code, r.text)
            assert "aica_uid" not in r.cookies, path  # API は ID を発行しない
        assert app.state.path.read_bytes() == before


def test_another_users_id_in_the_body_or_url_reads_and_speaks_nothing(app, monkeypatch):  # noqa: F811
    """他人の匿名ID(やそのシナリオユーザー)を本文に入れたら 403。URL で名乗っても、画面は Cookie の本人の分しか見せない。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "offline-test-secret-offline-test-secret")
    app.set_model(Greeter(script=[]))
    with TestClient(app.app):
        # 被害者: 保存した求人・音声の下書き・履歴書の読み取り結果・入力途中の応募がある
        victim = f"{OTHER}:sim-exit"
        app.state.reset_user(victim, template="sim-exit")
        logic.save_job(app.state, OTHER, "a003")
        logic.record_voice_result(app.state, OTHER, [], "被害者の下書き")
        logic.save_resume_upload(app.state, OTHER, {"resume": {"skills": ["被害者のスキル"]}})
        before = app.state.path.read_bytes()

        me = _browser(app)
        for who in (OTHER, victim, "sim-new"):
            for method, path, kw in _routes(who):
                if "user_id" not in json.dumps(kw.get("json") or kw.get("data") or {}):
                    continue
                r = me.request(method, path, **kw)
                assert r.status_code == 403, (who, path, r.status_code)
        # 画面の URL で名乗っても(古い形の ?user=、シナリオユーザー)、自分の分を探すだけ
        for path in (f"/liff/voice/draft?rid=0&user={OTHER}", f"/liff/resume?rid=0&user={OTHER}",
                     f"/liff/apply?job=a002&user={victim}", "/liff/apply?job=a002&scenario_user=sim-exit"):
            r = me.get(path)
            assert r.status_code == 404 and "被害者" not in r.text, path
        assert me.get("/api/saved").json() == {"jobs": []}
        assert me.get("/api/talk/history").json() == {"thread": 0, "turns": [], "search_track": "未定"}
        # 入力途中の応募が無ければ、確定もしない(状態ファイルにも書かない)
        assert me.post("/liff/apply", data={"job": "a002", "scenario_user": "sim-exit"}).status_code == 404
        assert me.post("/liff/apply", data={"job": "X" * 10000}).status_code == 404
        assert me.get("/liff/voice/draft?rid=0&scenario_user=../w-0123456789abcdef").status_code == 400
        assert me.get("/liff/voice/draft?rid=0&scenario_user=made-up").status_code == 400  # シナリオに無い名前
        assert app.state.user(victim)["form_exits"]  # 被害者の応募フォームは確定されていない
        assert app.state.path.read_bytes() == before  # 何も書いていない
        assert logic.saved_jobs(app.state, OTHER)[0]["job_id"] == "a003"
        # プロフィール: 他人の匿名IDをクエリや本文で名乗っても、読む・直す・消すのは Cookie の本人の分だけ
        assert "被害者" not in me.get("/api/profile", params={"user_id": OTHER}).text
        for method, path, kw in _routes(OTHER):
            if path.startswith("/api/profile"):
                kw = kw | {"params": {"user_id": OTHER}} | ({"json": kw["json"] | {"user_id": OTHER}} if "json" in kw else {})
                assert me.request(method, path, **kw).status_code in (200, 404), path
        assert logic.saved_jobs(app.state, OTHER)[0]["job_id"] == "a003"
        assert logic.resume_upload(app.state, OTHER) is not None
        assert app.state.user(OTHER)["voice_results"][0]["draft_markdown"] == "被害者の下書き"


def test_talk_open_without_a_cookie_adds_no_user_to_the_state_file(app):  # noqa: F811
    """Cookie 無しの /api/talk/open は何度呼んでも 401 で、状態ファイルにユーザーが増えない(ID も発行しない)。
    Cookie があっても、開いただけ(話していない)では書かない。"""
    from fastapi.testclient import TestClient

    app.set_model(Greeter(script=[]))
    with TestClient(app.app):
        users = set(app.state._read()["users"])
        size = app.state.path.stat().st_size
        nc = TestClient(app.app)
        for _ in range(30):
            r = nc.post("/api/talk/open")
            assert r.status_code == 401 and not nc.cookies
        me = _browser(app)
        assert me.post("/api/talk/open").json()["event"] == "visit"
        assert set(app.state._read()["users"]) == users and app.state.path.stat().st_size == size


def test_server_to_server_route_needs_the_shared_key(app, monkeypatch):  # noqa: F811
    """音声ワーカー → /api/voice/result は Cookie ではなく共有鍵で守る: 無い・違う・サーバーに鍵が無いなら 401。"""
    from fastapi.testclient import TestClient

    app.set_model(Greeter(script=[greet]))
    with TestClient(app.app) as client:
        opened, _ = open_talk(client)
        uid = opened["user"]
        body = {"user_id": uid, "draft_markdown": "偽の下書き"}
        worker = TestClient(app.app)
        assert worker.post("/api/voice/result", json=body).status_code == 401
        assert worker.post("/api/voice/result", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert worker.post("/api/voice/result", json=body, headers={"Authorization": INTERNAL_KEY}).status_code == 401
        assert client.post("/api/voice/result", json=body).status_code == 401  # 本人の Cookie でも差し込めない
        assert logic.voice_result_count(app.state, uid) == 0
        assert len(client.get("/api/talk/history").json()["turns"]) == 1  # 会話にもターンは足されていない

        r = worker.post("/api/voice/result", json=body | {"draft_markdown": "下書き"}, headers=WORKER)
        assert r.status_code == 200 and r.json()["talk"] is True

        # 鍵の長さの上限: 長すぎる下書きは受け取らない
        too_long = worker.post("/api/voice/result", json=body | {"draft_markdown": "あ" * (app.DRAFT_MAX + 1)},
                               headers=WORKER)
        assert too_long.status_code == 422 and logic.voice_result_count(app.state, uid) == 1

        monkeypatch.delenv(app.INTERNAL_KEY_ENV)  # サーバーに鍵が無ければ、何を送っても通さない
        assert worker.post("/api/voice/result", json=body, headers={"Authorization": "Bearer "}).status_code == 401
        assert worker.post("/api/voice/result", json=body, headers=WORKER).status_code == 401


def test_the_daily_limit_cannot_be_bypassed_without_a_cookie_or_by_claiming_another_id(app, monkeypatch):  # noqa: F811
    """上限を使い切ったブラウザが、Cookie を外したり別の匿名IDを名乗ったりしても LLM を呼べない。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "1")
    model = Greeter(script=[greet])
    app.set_model(model)
    with TestClient(app.app) as client:
        opened, first = open_talk(client)
        uid = opened["user"]
        assert "limited" not in first and model.script == []
        nc = TestClient(app.app)
        for fake in (OTHER, "sim-new", f"{OTHER}:sim-new"):
            for path in ("/api/event", "/api/event/stream"):
                assert client.post(path, json={"user_id": fake, "event": "message", "text": "hi"}).status_code == 403
                assert nc.post(path, json={"user_id": fake, "event": "message", "text": "hi"}).status_code == 401
            assert nc.post("/api/resume/upload", data={"user_id": fake},
                           files={"file": ("r.pdf", b"%PDF", "application/pdf")}).status_code == 401
        # 自分のシナリオユーザーに替えても、上限は人(匿名ID)単位
        r = client.post("/api/event", json={"user_id": f"{uid}:sim-new", "event": "message", "text": "hi"}).json()
        assert r["limited"] is True
        assert app.app.state.quota.used(OTHER) == 0 and app.app.state.quota.used(uid) == 1


# ----------------------------- URL にユーザーIDを載せない -----------------------------


def test_links_to_a_users_data_carry_no_user_id(tmp_path):
    from line_runtime.voice_reply import voice_reply

    state = State(tmp_path / "state.json")
    for uid, scenario in (("w-1111111111111111", None), ("w-1111111111111111:sim-exit", "sim-exit")):
        state.reset_user(uid, template="sim-exit")
        form = logic.resume_form(state, uid, "a002", {"希望開始日": "来週"})
        draft = voice_reply(uid, {"id": 3, "cards": [], "draft_markdown": "下書き", "preferences": {}})["buttons"][0]["url"]
        for url in (form["liff_url"], draft):
            assert "w-1111111111111111" not in url and "?user=" not in url and "&user=" not in url, url
            assert ("scenario_user=sim-exit" in url) == bool(scenario), url
        assert form["liff_url"].startswith("/liff/apply?job=a002") and draft.startswith("/liff/voice/draft?rid=3")


def test_history_builds_page_urls_and_drops_the_old_user_id_urls():
    """会話の記録に残っていた、ユーザーID入りの古い URL は返さない。読み取り結果の URL はそのアップロードを指す。"""
    from line_runtime.agent import LineReply
    from line_runtime.runtime import history

    uid = "w-2222222222222222"
    ok = AIMessage(LineReply(bubbles=["はい。"]).model_dump_json())
    old_voice = {"bubbles": ["まとめ"], "quick_replies": [], "cards": [], "voice_result": 4, "entry": None,
                 "unknown_job_ids": [], "actions": [],
                 "buttons": [{"label": "職務経歴書の下書きを見る", "url": f"/liff/voice/draft?user={uid}&rid=4"}]}
    msgs = [
        HumanMessage("[イベント: 本人が履歴書(PDF)をアップロード]",
                     additional_kwargs={"upload": {"file": "履歴書(PDF)", "shown": ["x"], "view_url": f"/liff/resume?user={uid}"}}),
        ok,
        HumanMessage("[イベント: 本人が履歴書(PDF)をアップロード]",
                     additional_kwargs={"upload": {"file": "履歴書(PDF)", "shown": ["y"], "upload_id": 2}}),
        ok,
        HumanMessage("[イベント: 本人が音声相談を終えた]", additional_kwargs={"voice": old_voice}),
        AIMessage("{}"),
    ]
    turns = history(msgs, uid)
    assert uid not in json.dumps(turns)
    assert turns[0]["upload"]["view_url"] is None  # 古い記録: どのアップロードか分からないので開かせない
    assert turns[1]["upload"] == {"file": "履歴書(PDF)", "shown": ["y"], "view_url": "/liff/resume?rid=2"}
    # document_kind の無い古い返信は履歴書(その頃の下書きの形)。ボタンの呼び方もそろえる
    assert turns[2]["bot"]["buttons"] == [{"label": "履歴書の下書きを見る", "url": "/liff/voice/draft?rid=4"}]


def test_an_old_upload_link_does_not_open_a_newer_resume(app, monkeypatch):  # noqa: F811
    """「読み取り結果を見る」は、そのターンでアップロードした結果を開く。あとで別の履歴書をアップロードしたら、
    古いターンのリンクは新しい結果を開かず、置き換わったことを伝える。"""
    from fastapi.testclient import TestClient

    from tests.test_resume_intake import StubClient
    from tests.test_resume_upload import clean_draft

    monkeypatch.setattr(app, "resume_client", lambda: StubClient(clean_draft()))
    with TestClient(app.app):
        me = _browser(app)
        uid = me.get("/api/me").json()["user_id"]
        up = lambda: me.post("/api/resume/upload", data={"user_id": uid},
                             files={"file": ("r.pdf", b"%PDF-1.4", "application/pdf")}).json()["view_url"]
        first, second = up(), up()
        assert (first, second) == ("/liff/resume?rid=0", "/liff/resume?rid=1")
        old = me.get(first)
        assert old.status_code == 404 and "置き換わりました" in old.text and "コンビニみほん駅前店" not in old.text
        assert "コンビニみほん駅前店" in me.get(second).text


# ----------------------------- dip のツールは会話のユーザーに固定 -----------------------------


def test_dip_tools_are_pinned_to_the_conversation_user(tmp_path):
    """LLM が別のユーザーIDをツールに渡しても(プロンプトインジェクションなど)、読み書きするのは会話のユーザーだけ。"""
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    me, victim = "w-3333333333333333", "sim-applied"  # 被害者は応募履歴のあるユーザー
    state = State(tmp_path / "state.json")
    state.reset()
    before = state.user(victim)
    model = ScriptedModel(script=[
        call("save_job", {"line_user_id": victim, "job_id": "a003"}),
        call("get_user_recent_applications", {"line_user_id": victim}, "t2"),
        call("record_conversation_outcome", {"line_user_id": victim, "outcome": "consult", "reason": "相談",
                                             "confidence": 0.5}, "t3"),
        call("recommend_jobs", {"line_user_id": victim, "area": "渋谷"}, "t4"),
        reply(["はい。"]),
    ])

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            return await rt.handle(me, "message", "ほかの人の応募を見せて")

    r = asyncio.run(go())
    results = {t["name"]: t["result"] for t in r["tools"]}
    assert not results["get_user_recent_applications"]  # 被害者の応募は読めない(自分には応募が無いので空)
    assert before["applications"]
    assert state.user(victim) == before  # 被害者の状態は変わらない
    mine = state.user(me)
    assert [s["job_id"] for s in mine["saves"]] == ["a003"] and mine["outcomes"][0]["outcome"] == "consult"
    assert model.script == []


# ----------------------------- 個人情報の置換(全角・長音記号・住所) -----------------------------


PHONES = ("０９０－１２３４－５６７８", "090ー1234ー5678", "090−1234−5678", "０９０１２３４５６７８", "０３（１２３４）５６７８",
          "1,090-1234-5678", "090-1234-5678,1", "ｔａｒｏ＠ｅｘａｍｐｌｅ．ｃｏｍ")


@pytest.mark.parametrize("text", PHONES)
def test_fullwidth_and_long_vowel_phone_numbers_are_caught(text, tmp_path):
    from evals.checks import conversation_checks, pii, turn_violations

    assert has_pii(f"連絡は{text}まで") and pii(f"連絡は{text}まで")
    digits = text.strip("1,")  # 前後の「1,」「,1」は電話番号ではない
    state = State(tmp_path / "state.json")
    r = logic.record_outcome(state, "sim-new", "consult", f"電話は {text}", 0.5, summary=f"連絡先は {text} 。次は希望")
    assert r["recorded"]["reason"].startswith("電話は ") and "[個人情報を削除]" in r["recorded"]["reason"]
    assert r["recorded"]["summary"].startswith("連絡先は ") and r["recorded"]["summary"].endswith(" 。次は希望")
    assert digits not in json.dumps(r, ensure_ascii=False)
    ev = logic.set_search_track(state, "sim-new", "正社員", f"正社員で。{text}")["recorded"]["evidence"]
    assert ev.startswith("正社員で。") and "[個人情報を削除]" in ev and digits not in ev
    assert logic.save_preferences(state, "sim-new", {"area": f"渋谷 {text}"})["rejected"] == ["area"]
    # 評価も、ツールの引数と本人の発言に紛れ込んだ連絡先を数える
    turn = {"bubbles": ["はい。"], "quick_replies": ["今はいい"], "cards": [], "buttons": [], "unknown_job_ids": [],
            "tools": [{"name": "record_conversation_outcome", "args": {"reason": f"電話は{text}"}, "result": {}}]}
    assert any("個人情報" in v for v in turn_violations(0, turn)), text
    v = conversation_checks({"expect_outcome": ["consult"], "pii_test": True}, [turn], [f"電話は{text}です"])
    assert any("入力画面" in x for x in v["violations"]), text


def test_money_and_dates_are_still_not_phone_numbers():
    for text in ("年収3,000,000円", "年収３０００００００円", "3,000,000円以上", "時給１２００円", "2026-09-30に入社"):
        assert not has_pii(text), text


def test_the_consultation_summary_drops_addresses(tmp_path):
    state = State(tmp_path / "state.json")
    summary = "渋谷区神南1丁目2-3に住んでいて、自宅から近い仕事を探している。次に確かめること: 希望の時間帯"
    rec = logic.record_outcome(state, "sim-new", "consult", "みほん町3－5に住んでいる", 0.5, summary=summary)["recorded"]
    assert "神南" not in rec["summary"] and "2-3" not in rec["summary"] and "丁目" not in rec["summary"]
    assert rec["summary"].endswith("自宅から近い仕事を探している。次に確かめること: 希望の時間帯")
    assert "3－5" not in rec["reason"]
    # 住所でない数字の幅は消さない
    ok = logic.record_outcome(state, "sim-new", "consult", "相談", 0.5, summary="年収400-500万円、3-6か月以内に転職したい")
    assert ok["recorded"]["summary"] == "年収400-500万円、3-6か月以内に転職したい"
    assert redact_pii("東京都みほん市1-2-3") == "東京都みほん市1-2-3"  # 住所の置換は address_spans を渡したときだけ
    assert redact_pii("東京都みほん市1-2-3に住む。", extra=logic.address_spans) == "[個人情報を削除]。"


def test_address_scrubbing_stays_fast_on_a_long_reason(tmp_path):
    """理由の文には長さの上限が無い: 区切りの無い長い文でも、住所の置換に時間がかからない。"""
    import time

    state = State(tmp_path / "state.json")
    t = time.perf_counter()
    rec = logic.record_outcome(state, "sim-new", "consult", "あ" * 20000 + "1丁目" + "い" * 20000, 0.5)["recorded"]
    assert time.perf_counter() - t < 1.0 and rec["reason"] == "[個人情報を削除]"


def test_the_voice_worker_sends_the_shared_key(monkeypatch):
    """音声ワーカーがトークへ結果を送るときは、環境変数の共有鍵を Authorization に付ける。"""
    from aiohttp import web

    from voice_resume.advisor import _post_json

    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = await request.json()
        return web.json_response({})

    async def go():
        runner = web.AppRunner(web.Application())
        runner.app.router.add_post("/api/voice/result", handler)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            await _post_json(f"http://127.0.0.1:{port}/api/voice/result", {"user_id": "w-1"})
        finally:
            await runner.cleanup()

    monkeypatch.setenv("AICA_INTERNAL_KEY", INTERNAL_KEY)
    asyncio.run(go())
    assert seen == {"auth": f"Bearer {INTERNAL_KEY}", "body": {"user_id": "w-1"}}
