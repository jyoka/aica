"""わたしのプロフィール(task 95)の台本テスト。実 LLM なし、dip の MCP は本物、履歴書の読み取りはスタブ。

aica が本人について覚えていること(雇用区分・目的・希望・経歴・相談のまとめ・履歴書・音声の下書き・保存・応募)を、
本人が見て、直して、消せる。確かめること:
(1) 画面の API に全項目が出る(空の項目も)。画面では空の項目が「まだありません」になる(ブラウザ)
(2) 直す・消すと、次の返信の前に AI へ伝わる(会話の途中なら知らせ、要約もすぐ変わる)。AI は直した値で話し、
    消した項目を前提にしない
(3) 履歴書の読み取り結果と音声の下書きを丸ごと消せる。消した後はチャットの要約にも音声の指示にも出ない。
    履歴書を消すと今の会話は閉じ、次は新しい会話から始まる(task 115。古いタブは stale、会話数は余計に使わない)
(4) 「すべて消す」でプロフィール(保存・応募を含む)と会話が消え、次に開くと初回訪問として始まる
    (会話数の上限の数は消さない)。/dev のシナリオ利用者の記録も消える。画面の説明と確認に、消えるものと消えないものが出る
(5) パスにユーザーIDは無く、Cookie の本人の分だけ。他人の匿名IDを名乗っても届かず、Cookie なし・不正は 401
(6) 手で直すときも、電話番号・メール・住所らしい値は弾く
追記(UX 刷新 3/3 のレビューから): 通話中の × は確認を出す / 閉じた後に届いた結果は次のターンの前に描く(ブラウザ)
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from langchain_core.outputs import ChatGeneration, ChatResult

from dip_mcp import behavior, logic, profile
from dip_mcp.data import JST
from line_runtime.agent import PINNED_USER
from line_runtime.runtime import _tool_payload
from tests.test_handle import ScriptedModel, call, reply
from tests.test_resume_intake import StubClient
from tests.test_resume_upload import FILENAME, clean_draft
from tests.test_talk_actions import browser, livekit  # noqa: F401  フィクスチャ
from tests.test_talk_page import (  # noqa: F401  app はフィクスチャ
    WORKER,
    app,
    open_talk,
    stream,
)

MENU = ["求人を探す", "まず相談する", "今はいい"]
NOTICE = "[お知らせ: 本人が画面で"


class Rememberer(ScriptedModel):
    """覚えていることを前提に話す偽のモデル(何度でも返す)。会話の中でいちばん新しい情報(要約のツール結果か、
    プロフィールを変えた知らせ)の希望で話す。「要約を読んで」と言われたら要約を読み直す。"""

    contexts: list = []  # noqa: RUF012  モデルに渡った会話

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.respond(messages))])

    @staticmethod
    def latest(messages) -> dict:
        prefs: dict = {}
        for m in messages:
            if m.type == "tool" and m.name == "get_user_conversation_summary":
                s = _tool_payload(m)
                prefs = {k: v["value"] for k, v in s["stated_preferences"].items()}
                prefs["uploaded_resume"] = s["uploaded_resume"]
            elif m.type == "human" and m.content.startswith(NOTICE):
                now = json.loads(re.search(r"今の内容: (\{.*\}) これを前提", m.content).group(1))
                prefs = dict(now["stated_preferences"]) | {"uploaded_resume": now["uploaded_resume"] == "あり"}
        return prefs

    def respond(self, messages):
        self.contexts.append(list(messages))
        uid = PINNED_USER  # system prompt にユーザーIDは無い。LINE 側(PinUserMiddleware)が置き換える
        last = max(i for i, m in enumerate(messages) if m.type == "human")
        human, turn = messages[last].content, messages[last + 1:]
        if human.startswith("[イベント:") and "アップロード" not in human or human == "要約を読んで":
            if not any(m.type == "tool" for m in turn):
                return call("get_user_conversation_summary", {"line_user_id": uid}, "sum")
            if human.startswith("[イベント:"):
                return reply(["こんにちは。お仕事探しをお手伝いします。何をしますか?"], quick_replies=MENU)
        if "アップロード" in human:
            if not any(m.type == "tool" for m in turn):
                return call("get_user_conversation_summary", {"line_user_id": uid}, "sum")
            return reply(["履歴書を拝見しました。近い仕事で探しますか?"], quick_replies=["はい", "今はいい"])
        p = self.latest(messages)
        said = [f"エリアは{p['area']}で探しますね。" if p.get("area") else "エリアはどこにしますか?"]
        if p.get("purpose"):
            said.append(f"目的は「{p['purpose']}」ですね。")
        if p.get("uploaded_resume"):
            said.append("履歴書の経歴も踏まえます。")
        return reply(said, quick_replies=["はい", "今はいい"])


@pytest.fixture
def remembered(app, monkeypatch, tmp_path):  # noqa: F811
    model = Rememberer()
    model.contexts = []
    app.set_model(model)
    stub = StubClient(clean_draft(), usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    monkeypatch.setattr(app, "resume_client", lambda: stub)
    from voice_resume import config as voice_config

    monkeypatch.setattr(voice_config, "OUT_DIR", tmp_path / "voice-out")
    app.model = model
    yield app
    del app.model


def _client(app):  # noqa: F811
    from fastapi.testclient import TestClient

    return TestClient(app.app)


def _items(profile: dict) -> dict:
    return {it["key"]: it["value"] for s in profile["sections"] for it in s["items"]}


# ----------------------------- (1) 画面に出る項目 -----------------------------


def test_profile_shows_every_item_and_empty_ones_as_none(remembered):
    with _client(remembered) as client:
        opened, greeting = open_talk(client)
        uid = opened["user"]
        p = client.get("/api/profile").json()
        assert [s["title"] for s in p["sections"]] == ["目的", "希望", "経歴"]
        items = _items(p)
        # 区分が未定なら正社員の条件(年収・働き方・時期)は出さない。ほかは空でも出す(画面が「まだありません」)
        assert set(items) == set(logic.PREFERENCE_KEYS) - set(logic.FULLTIME_PREFERENCE_KEYS)
        assert all(v is None for v in items.values())
        assert p["track"]["current"] == "未定" and p["track"]["history"] == []
        assert p["consultation_summary"] is None and p["resume"] is None and p["voice_drafts"] == []
        assert p["saved_jobs"] == [] and p["applications"] == []

        # 覚えたことが出る: 区分(いつ・どの発言で)・目的・希望・経歴・相談のまとめ・履歴書・下書き・保存・応募
        before = datetime.now(JST)  # 本番のユーザーは実際の時刻で記録する(ADR-0004)
        logic.set_search_track(remembered.state, uid, "正社員", "転職を考えています")
        logic.save_preferences(remembered.state, uid, {"purpose": "転職", "area": "渋谷", "skills": "Excel",
                                                       "min_annual_salary": "500万円以上"})
        logic.record_outcome(remembered.state, uid, "consult", "相談のみ", 0.8, "転職で渋谷を希望。次に確かめること: 職種")
        assert client.post("/api/resume/upload", params={"thread": greeting["thread"]}, data={"user_id": uid},
                           files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/pdf")}).status_code == 200
        assert client.post("/api/voice/result", json={"user_id": uid, "draft_markdown": "# 職務経歴書\n- 営業5年"},
                           headers=WORKER).status_code == 200
        job = min(logic.load_jobs())
        logic.save_job(remembered.state, uid, job)
        logic.submit_application(remembered.state, uid, sorted(logic.load_jobs())[1])
        p = client.get("/api/profile").json()
        items = _items(p)
        assert items["purpose"] == "転職" and items["area"] == "渋谷" and items["skills"] == "Excel"
        assert items["min_annual_salary"] == "500万円以上" and "work_style" in items  # 正社員なら条件も出す
        assert p["track"]["current"] == "正社員"
        after = datetime.now(JST)  # 日本時間の 0 時をまたいでも、記録した日は before か after の日になる
        changed = p["track"]["history"][0]
        assert changed.pop("when") in {f"{before.month}/{before.day}", f"{after.month}/{after.day}"}
        assert changed == {"from": "未定", "track": "正社員", "evidence": "転職を考えています", "undo": False}
        assert p["consultation_summary"]["text"].startswith("転職で渋谷")
        assert p["resume"]["view_url"] == "/liff/resume?rid=0" and p["resume"]["highlights"]
        assert FILENAME not in json.dumps(p, ensure_ascii=False)  # ファイル名は出さない(保存もしていない)
        assert [d["id"] for d in p["voice_drafts"]] == [0]
        assert p["voice_drafts"][0]["view_url"] == "/liff/voice/draft?rid=0"
        assert [j["job_id"] for j in p["saved_jobs"]] == [job]
        assert len(p["applications"]) == 1 and p["applications"][0]["title"]


# ----------------------------- (2) 直す・消すと、次の返信の前に AI へ伝わる -----------------------------


def test_edits_reach_the_ai_before_the_next_reply(remembered):
    """会話の途中で直す・消すと、次の本人の発言の直前に知らせが入り、AI は直した値で話し、消した項目を前提にしない。
    要約もすぐ変わる(次に読み直しても、直した値だけが出る)。"""
    with _client(remembered) as client:
        client.get("/")
        uid = client.cookies[remembered.ANON_COOKIE]
        logic.save_preferences(remembered.state, uid, {"area": "新宿", "purpose": "生活費の足しに"})
        _, greeting = open_talk(client)
        th = greeting["thread"]
        before = stream(client, uid, "message", th, "探して")[-1]
        assert before["bubbles"] == ["エリアは新宿で探しますね。", "目的は「生活費の足しに」ですね。"]

        assert client.put("/api/profile/items/area", json={"value": "渋谷"}).status_code == 200
        p = client.delete("/api/profile/items/purpose").json()
        assert _items(p)["area"] == "渋谷" and _items(p)["purpose"] is None
        # 要約はすぐ変わる(次の会話・音声が読む)
        stated = logic.conversation_summary(remembered.state, uid)["stated_preferences"]
        assert {k: v["value"] for k, v in stated.items()} == {"area": "渋谷"}

        after = stream(client, uid, "message", th, "探して")[-1]
        assert after["bubbles"] == ["エリアは渋谷で探しますね。"]
        assert not any("生活費" in b or "新宿" in b for b in after["bubbles"])
        # 知らせは本人の発言の直前に1回だけ入り、消した項目を名前で伝える
        ctx = remembered.model.contexts[-1]
        assert ctx[-1].content == "探して" and ctx[-2].content.startswith(NOTICE)
        assert "エリア・駅を「渋谷」に直した" in ctx[-2].content and "目的を消した" in ctx[-2].content
        assert "生活費" not in ctx[-2].content
        # 渡し終えた知らせは次のターンに持ち越さない。画面の履歴にも出さない
        stream(client, uid, "message", th, "ありがとう")
        assert sum(m.content.startswith(NOTICE) for m in remembered.model.contexts[-1] if m.type == "human") == 1
        assert remembered.state.user(uid).get("profile_changes") == []
        turns = client.get("/api/talk/history").json()["turns"]
        assert [t["user"] for t in turns] == [None, "探して", "探して", "ありがとう"]
        assert turns[1]["bot"]["bubbles"] == before["bubbles"]  # 知らせの前のターンの返信も欠けない
        assert all(NOTICE not in json.dumps(t, ensure_ascii=False) for t in turns)

        # AI が要約を読み直しても、直した値だけ
        stream(client, uid, "message", th, "要約を読んで")
        summary = next(_tool_payload(m) for m in reversed(remembered.model.contexts[-1]) if m.type == "tool")
        assert {k: v["value"] for k, v in summary["stated_preferences"].items()} == {"area": "渋谷"}


def test_a_new_conversation_reads_the_edited_profile_without_a_notice(remembered):
    """新しい会話(30分後の再訪)は最初に要約を読むので、知らせは挟まず、要約に直した値が出る。"""
    with _client(remembered) as client:
        opened, first = open_talk(client)
        uid = opened["user"]
        client.put("/api/profile/items/area", json={"value": "池袋"})
        client.put("/api/profile/track", json={"track": "アルバイト・パート"})
        remembered.later(31)
        _, greeting = open_talk(client)
        assert greeting["thread"] == first["thread"] + 1
        assert remembered.state.user(uid).get("profile_changes") == []  # 挨拶が返ったら渡し終えた扱い
        r = stream(client, uid, "message", greeting["thread"], "探して")[-1]
        assert r["bubbles"] == ["エリアは池袋で探しますね。"]
        assert not any(m.type == "human" and m.content.startswith(NOTICE) for m in remembered.model.contexts[-1])
        summary = logic.conversation_summary(remembered.state, uid)
        assert summary["search_track"] == "アルバイト・パート"
        assert summary["track_history"][-1]["evidence"] == "(プロフィール画面で本人が選んだ)"


def test_track_and_consultation_summary_can_be_edited_and_deleted(remembered):
    with _client(remembered) as client:
        opened, _ = open_talk(client)
        uid = opened["user"]
        logic.set_search_track(remembered.state, uid, "正社員", "転職したい")
        logic.record_outcome(remembered.state, uid, "consult", "相談のみ", 0.8, "転職を考えている")
        logic.record_outcome(remembered.state, uid, "consult", "相談のみ", 0.8, "前のまとめ: 渋谷で転職")

        p = client.put("/api/profile/items/consultation_summary", json={"value": "副業を探している"}).json()
        assert p["consultation_summary"]["text"] == "副業を探している"
        assert client.put("/api/profile/items/consultation_summary",
                          json={"value": "あ" * 201}).status_code == 400
        # 消すと、前のまとめが繰り上がって出ることもない
        p = client.delete("/api/profile/items/consultation_summary").json()
        assert p["consultation_summary"] is None
        assert logic.conversation_summary(remembered.state, uid)["consultation_summary"] is None
        assert len(remembered.state.user(uid)["outcomes"]) == 2  # 会話の結果の記録は残す

        assert client.put("/api/profile/track", json={"track": "社長"}).status_code == 400
        p = client.delete("/api/profile/track").json()
        assert p["track"] == {"current": "未定", "choices": ["アルバイト・パート", "正社員", "未定"], "history": []}
        assert logic.conversation_summary(remembered.state, uid)["search_track"] == "未定"
        assert client.put("/api/profile/items/nickname", json={"value": "x"}).status_code == 400
        assert client.put("/api/profile/items/area", json={"value": "  "}).status_code == 400


# ----------------------------- (3) 履歴書と音声の下書きを丸ごと消す -----------------------------


def test_deleted_resume_is_used_neither_in_the_chat_nor_in_the_voice(remembered, tmp_path):
    from voice_resume.agent import prepare_advisor
    from voice_resume.dip import DipMCP

    with _client(remembered) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        assert client.post("/api/resume/upload", params={"thread": th}, data={"user_id": uid},
                           files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/pdf")}).status_code == 200
        stream(client, uid, "resume_upload", th)
        assert "履歴書の経歴も踏まえます。" in stream(client, uid, "message", th, "探して")[-1]["bubbles"]

        p = client.delete("/api/profile/resume").json()
        assert p["resume"] is None
        assert logic.conversation_summary(remembered.state, uid)["uploaded_resume"] is None
        assert client.get("/liff/resume?rid=0").status_code == 404
        # チャット: 今の会話は閉じ、新しい会話は消した後の要約を読むので、履歴書を前提にしない
        _, again = open_talk(client)
        r = stream(client, uid, "message", again["thread"], "探して")[-1]
        assert "履歴書の経歴も踏まえます。" not in r["bubbles"]
        # アップロードのターンも、消した後は読み取り結果が無いので始められない
        assert client.post("/api/event/stream", json={"user_id": uid, "event": "resume_upload",
                                                      "thread": again["thread"]}).status_code == 400

    # 音声: 同じ要約を読むので、面談の指示にも入らない(本物の dip MCP)
    async def start():
        dip = DipMCP()
        await dip.start()
        try:
            return await prepare_advisor(dip, uid)
        finally:
            await dip.aclose()

    advisor, _ = asyncio.run(start())
    assert "コンビニみほん駅前店" not in advisor.instructions


def test_a_link_from_before_the_resume_was_deleted_does_not_open_the_next_upload(remembered):
    """プロフィール画面で読み取り結果を消してから別の履歴書をアップロードしても、消す前のターンの
    「読み取り結果を見る」は新しい結果を開かず、置き換わったことを伝える(番号は消しても戻らない)。"""
    with _client(remembered) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        up = lambda th: client.post("/api/resume/upload", params={"thread": th}, data={"user_id": uid},
                                    files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/pdf")}).json()["view_url"]
        first = up(th)
        assert client.delete("/api/profile/resume").json()["resume"] is None
        second = up(open_talk(client)[1]["thread"])  # 消すと今の会話は閉じるので、新しい会話でアップロードする
        assert first != second
        old = client.get(first)
        assert old.status_code == 404 and "置き換わりました" in old.text and "コンビニみほん駅前店" not in old.text
        assert "コンビニみほん駅前店" in client.get(second).text


RESUME_TEXT = "コンビニみほん駅前店"  # 読み取り結果の要点(アップロードの確認の吹き出し・要約のツール結果に出る)


def _talk_with_resume(client) -> tuple[str, int]:
    """トークを開き、履歴書をアップロードして、その要点で1往復話した会話: (匿名ID, 会話の番号)。"""
    opened, greeting = open_talk(client)
    uid, th = opened["user"], greeting["thread"]
    assert client.post("/api/resume/upload", params={"thread": th}, data={"user_id": uid},
                       files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/pdf")}).status_code == 200
    stream(client, uid, "resume_upload", th)
    assert "履歴書の経歴も踏まえます。" in stream(client, uid, "message", th, "探して")[-1]["bubbles"]
    return uid, th


def test_deleting_the_resume_closes_the_conversation_and_the_next_one_starts_new(remembered):
    """履歴書を消すと今の会話は閉じる。30分以内に開き直しても前の会話を描き直さず、新しい会話(再訪の挨拶)から始まる。
    前の会話に残っていた要点(アップロードの確認・要約のツール結果)は、画面にも新しい会話の LLM への入力にも出ない。"""
    with _client(remembered) as client:
        uid, th = _talk_with_resume(client)
        rt = client.app.state.rt
        assert RESUME_TEXT in json.dumps(client.get("/api/talk/history").json(), ensure_ascii=False)

        client.delete("/api/profile/resume")
        # 閉じた会話は描き直せない(履歴も、チェックポイントの中身も無い)
        h = client.get("/api/talk/history").json()
        assert h["thread"] == th + 1 and h["turns"] == []
        closed = asyncio.run(rt.agent.aget_state(rt._cfg(uid, th))).values
        assert not closed.get("messages")

        remembered.later(0)  # 30分以内の再読み込み
        opened, greeting = open_talk(client)
        assert opened["event"] == "revisit" and greeting["entry"] == "revisit"
        assert greeting["thread"] > th
        assert greeting["bubbles"] == ["こんにちは。お仕事探しをお手伝いします。何をしますか?"]
        h = client.get("/api/talk/history").json()
        assert [t["user"] for t in h["turns"]] == [None] and "upload" not in h["turns"][0]
        assert RESUME_TEXT not in json.dumps(h, ensure_ascii=False)
        # 新しい会話の LLM への入力に、消した履歴書の要点が入らない
        r = stream(client, uid, "message", greeting["thread"], "探して")[-1]
        assert "履歴書の経歴も踏まえます。" not in r["bubbles"]
        ctx = remembered.model.contexts[-1]
        assert ctx[1].content.startswith("[イベント: 再訪")  # 新しい会話の最初から(前の会話を引き継がない)
        assert not any(RESUME_TEXT in json.dumps(m.content, ensure_ascii=False) for m in ctx)
        assert not any(m.type == "human" and m.content.startswith(NOTICE) for m in ctx)

        # 再起動しても(会話の番号をチェックポイントから数え直しても)、閉じた会話には戻らない
        rt._threads.pop(uid)
        assert asyncio.run(rt.current_thread(uid)) == greeting["thread"]


def _checkpoint_rows(rt, uid: str) -> dict[str, list[bytes]]:
    """サーバーの DB(チェックポイントの SQLite)を直接読む: そのユーザーの会話ごとの、保存された中身(チェックポイントと書き込み)。"""
    async def read():
        rows: dict[str, list[bytes]] = {}
        for sql in ("SELECT thread_id, checkpoint FROM checkpoints", "SELECT thread_id, value FROM writes"):
            async with rt._saver.conn.execute(sql) as cur:
                for thread_id, blob in await cur.fetchall():
                    if thread_id.startswith(f"{uid}:"):
                        rows.setdefault(thread_id, []).append(bytes(blob or b""))
        return rows

    return asyncio.run(read())


def _mentions_resume(blobs: list[bytes]) -> bool:
    needles = (RESUME_TEXT.encode(), json.dumps(RESUME_TEXT).strip('"').encode())  # そのまま / JSON の \u エスケープ
    return any(n in b for b in blobs for n in needles)


def test_deleting_the_resume_erases_the_upload_from_every_past_conversation_in_the_db(remembered):
    """履歴書を消すと、今の会話だけでなく、その人のそれまでのすべての会話のチェックポイントから、アップロードの要点
    (確認の吹き出し・要約のツール結果)が消える(DB を直接見る)。残るのは閉じた後の空の会話だけで、番号はそこから
    続き、再起動後も(最大の番号から)数え直せる。会話数は余計に使わない。"""
    with _client(remembered) as client:
        uid, first = _talk_with_resume(client)
        rt, quota = client.app.state.rt, client.app.state.quota
        remembered.later(31)  # 30分を過ぎて開き直す: 新しい会話になり、そこでもまた履歴書の要点が出る
        _, greeting = open_talk(client)
        second = greeting["thread"]
        assert second == first + 1
        stream(client, uid, "message", second, "要約を読んで")
        rows = _checkpoint_rows(rt, uid)
        assert _mentions_resume(rows[f"{uid}:{first}"]) and _mentions_resume(rows[f"{uid}:{second}"])
        used = quota.used(uid)

        client.delete("/api/profile/resume")
        rows = _checkpoint_rows(rt, uid)
        assert sorted(rows) == [f"{uid}:{second + 1}"]  # 前の会話は消え、閉じた後の空の会話だけ
        assert not any(_mentions_resume(b) for b in rows.values())
        for k in range(1, second + 2):
            assert not asyncio.run(rt.agent.aget_state(rt._cfg(uid, k))).values.get("messages")
        assert quota.used(uid) == used

        h = client.get("/api/talk/history").json()
        assert h["thread"] == second + 1 and h["turns"] == []
        rt._threads.pop(uid)  # 再起動しても、閉じた後の会話から数え直す
        assert asyncio.run(rt.current_thread(uid)) == second + 1


def test_an_old_tab_is_told_stale_after_the_resume_is_deleted_without_using_the_quota(remembered):
    """別のタブで古い会話を開いたまま: 履歴書を消した後の発言・アップロードは、既存の古いタブの案内(stale)になる。
    消すことも stale も会話数を使わない。新しい会話は挨拶の1回分だけ(迂回もできない: 数は消さない)。"""
    with _client(remembered) as client:
        uid, th = _talk_with_resume(client)
        quota = client.app.state.quota
        used = quota.used(uid)

        client.delete("/api/profile/resume")
        assert quota.used(uid) == used
        evs = stream(client, uid, "message", th, "まだ続けたい")
        assert evs[-1] == {"kind": "stale", "thread": th + 1}
        r = client.post("/api/event", json={"user_id": uid, "event": "message", "text": "まだ", "thread": th}).json()
        assert r == {"stale": True, "thread": th + 1}
        assert client.post("/api/resume/upload", params={"thread": th}, data={"user_id": uid},
                           files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/pdf")}).status_code == 409
        assert quota.used(uid) == used
        assert client.get("/api/talk/history").json()["turns"] == []  # 古いタブの発言は会話に入らない

        _, greeting = open_talk(client)
        assert quota.used(uid) == used + 1 and greeting["thread"] == th + 2
        # 消すものが無ければ、会話は閉じない(開き直しは前の会話を続ける)
        client.delete("/api/profile/resume")
        opened, h = open_talk(client)
        assert opened["event"] is None and h["thread"] == greeting["thread"] and len(h["turns"]) == 1
        assert quota.used(uid) == used + 1


def test_a_voice_result_after_the_resume_is_deleted_starts_the_next_conversation(remembered):
    """閉じた後に音声相談の結果が届いたら、空の新しい会話の最初のターンになり、続けて話せる(ツールに渡す会話のユーザーが分かる)。"""
    with _client(remembered) as client:
        uid, th = _talk_with_resume(client)
        client.delete("/api/profile/resume")
        out = client.post("/api/voice/result", json={"user_id": uid, "draft_markdown": "# 下書き",
                                                     "preferences": {"area": "中野"}}, headers=WORKER).json()
        assert out["talk"] is True
        opened, h = open_talk(client)
        assert opened["event"] is None and h["thread"] == th + 1 and len(h["turns"]) == 1
        assert RESUME_TEXT not in json.dumps(h, ensure_ascii=False)
        stream(client, uid, "message", th + 1, "要約を読んで")
        summary = next(_tool_payload(m) for m in reversed(remembered.model.contexts[-1]) if m.type == "tool")
        assert "error" not in summary and summary["uploaded_resume"] is None  # 会話のユーザーで要約を読めた


OLD_WORDS = "前の会話の合言葉はみかん"  # 閉じる前の会話にだけ出る本人の発言


def _fail_midway(monkeypatch) -> None:
    """会話の記録を消すトランザクションの途中で失敗させる: checkpoints の DELETE は通り、次の表で例外になる。"""
    from line_runtime import runtime

    monkeypatch.setattr(runtime, "CONVERSATION_TABLES", ("checkpoints", "no_such_table"))


def _heal(monkeypatch) -> None:
    from line_runtime import runtime

    monkeypatch.setattr(runtime, "CONVERSATION_TABLES", ("checkpoints", "writes"))


def test_a_close_that_fails_midway_deletes_nothing_and_the_old_conversations_do_not_come_back(remembered, monkeypatch):
    """履歴書を消して会話を閉じる途中(前の会話の記録を消すトランザクションの途中)で失敗した: 前の会話は1つも消えず
    (半分だけ消えて番号が戻ることはない)、履歴書も残る。再起動しても今の会話は閉じた後の空の会話で、その後に何度
    新しい会話を始めても、前の会話の中身は文脈に戻らない。もう一度押せば閉じて、前の会話も履歴書も消える。"""
    import sqlite3

    with _client(remembered) as client:
        uid, first = _talk_with_resume(client)
        rt = client.app.state.rt
        stream(client, uid, "message", first, OLD_WORDS)
        for _ in range(2):  # 30分を過ぎて開き直す: 前の会話が3つ
            remembered.later(31)
            _, greeting = open_talk(client)
            stream(client, uid, "message", greeting["thread"], OLD_WORDS)
        last = greeting["thread"]
        assert last == first + 2
        before = _checkpoint_rows(rt, uid)

        _fail_midway(monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            client.delete("/api/profile/resume")
        _heal(monkeypatch)
        rows = _checkpoint_rows(rt, uid)
        assert {k: rows[k] for k in before} == before  # 何も消えていない(途中まで消えた会話は無い)
        assert sorted(rows) == sorted([*before, f"{uid}:{last + 1}"])  # 閉じた後の空の会話は書けている
        assert client.get("/api/profile").json()["resume"] is not None  # 履歴書も残る(もう一度押せる)

        rt._threads.clear()  # 再起動: 会話の番号を DB から数え直す
        assert asyncio.run(rt.current_thread(uid)) == last + 1
        assert client.get("/api/talk/history").json()["turns"] == []
        seen = []
        for _ in range(last + 1):  # 前の会話の数より多く新しい会話を始めても、前の会話に追記しない
            remembered.later(31)
            _, greeting = open_talk(client)
            stream(client, uid, "message", greeting["thread"], "探して")
            seen.append(greeting["thread"])
            ctx = remembered.model.contexts[-1]
            assert ctx[1].content.startswith("[イベント: 再訪")
            assert not any(OLD_WORDS in json.dumps(m.content, ensure_ascii=False) for m in ctx)
        assert seen == list(range(last + 2, 2 * last + 3))

        p = client.delete("/api/profile/resume").json()  # もう一度押す: 閉じて、前の会話も履歴書も消える
        assert p["resume"] is None
        rows = _checkpoint_rows(rt, uid)
        assert sorted(rows) == [f"{uid}:{seen[-1] + 1}"]
        assert not any(_mentions_resume(b) or OLD_WORDS.encode() in b for b in rows.values())
        assert client.get("/api/talk/history").json() == {"thread": seen[-1] + 1, "turns": [], "search_track": "未定"}


def test_a_resume_delete_that_fails_after_closing_closes_again_when_pressed_again(remembered, monkeypatch):
    """会話を閉じた後、読み取り結果を消すところで失敗した: 履歴書は残っているので、もう一度押せば会話を閉じ直して
    消せる(先に読み取り結果を消して、閉じる前に失敗すると、もう一度押しても消すものが無く閉じられなかった)。"""
    from dip_mcp import profile

    with _client(remembered) as client:
        uid, th = _talk_with_resume(client)
        rt = client.app.state.rt
        delete_resume = profile.delete_resume

        def broken(*a, **kw):
            raise RuntimeError("dip の状態を書けない")

        monkeypatch.setattr(profile, "delete_resume", broken)
        with pytest.raises(RuntimeError):
            client.delete("/api/profile/resume")
        monkeypatch.setattr(profile, "delete_resume", delete_resume)
        assert client.get("/api/profile").json()["resume"] is not None
        assert client.get("/api/talk/history").json()["thread"] == th + 1  # 会話は閉じた

        assert client.delete("/api/profile/resume").json()["resume"] is None
        assert client.get("/api/talk/history").json() == {"thread": th + 2, "turns": [], "search_track": "未定"}
        rows = _checkpoint_rows(rt, uid)
        assert sorted(rows) == [f"{uid}:{th + 2}"] and not any(_mentions_resume(b) for b in rows.values())


def test_deleting_the_resume_in_the_browser_starts_a_new_conversation(remembered, browser, served_profile):  # noqa: F811
    """プロフィール画面で履歴書を消す確認に「今の会話は終わり」と、会話で保存した内容は残り各項目から消せることが出る。消してトークに戻ると、前の会話の吹き出しは
    描かず、新しい会話(再訪の挨拶)から始まる。再読み込みしても前の会話には戻らない。"""
    from resume_intake.extract import finalize

    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(served_profile + "/")
    page.get_by_text("こんにちは。お仕事探しをお手伝いします。何をしますか?").wait_for()
    uid = next(c["value"] for c in context.cookies() if c["name"] == "aica_uid")
    page.locator("#t").fill("探して")
    page.locator("#send").click()
    page.get_by_text("エリアはどこにしますか?").wait_for()
    logic.save_resume_upload(remembered.state, uid, finalize(clean_draft()), file="履歴書(PDF)")

    page.locator("#nav button[data-view=profile]").click()
    row = page.locator('#profile .prow[data-key="resume"]')
    row.locator("button.del").click()
    row.locator(".pconfirm", has_text="今の会話は終わり、次は新しい会話から始まります").wait_for()
    row.locator(".pconfirm", has_text="これまでの会話の記録も消えます。").wait_for()
    row.locator(".pconfirm", has_text="経験など、会話で保存した内容は残ります。プロフィールの各項目から消せます。").wait_for()
    row.locator(".pconfirm button.danger").click()
    row.locator(".pv.empty").wait_for()
    assert logic.resume_upload(remembered.state, uid) is None

    page.locator("#nav button[data-view=talk]").click()
    page.locator("#log .row.bot", has_text="こんにちは。").wait_for()
    assert page.locator("#log .row.me").count() == 0  # 前の会話は描かない
    assert page.get_by_text("エリアはどこにしますか?").count() == 0
    page.reload()
    page.locator("#log .row.bot", has_text="こんにちは。").wait_for()
    assert page.locator("#log .row.me").count() == 0
    # 新しい会話で話せる(stale にならない)
    page.locator("#t").fill("探して")
    page.locator("#send").click()
    page.get_by_text("エリアはどこにしますか?").wait_for()
    context.close()


def test_deleted_voice_draft_is_gone_with_its_files(remembered, tmp_path):
    from voice_resume import config as voice_config

    out = voice_config.OUT_DIR
    out.mkdir(parents=True)
    files = [out / "resume-draft-1.md", out / "transcript-1.json"]
    for f in files:
        f.write_text("下書き", encoding="utf-8")
    outside = tmp_path / "not-voice.txt"
    outside.write_text("消さない", encoding="utf-8")
    with _client(remembered) as client:
        opened, greeting = open_talk(client)
        uid = opened["user"]
        client.post("/api/voice/result", json={"user_id": uid, "draft_markdown": "# 下書き\n- 営業5年",
                                                "files": [str(f) for f in files] + [str(outside)]}, headers=WORKER)
        assert client.get("/liff/voice/draft?rid=0").status_code == 200

        p = client.delete("/api/profile/voice/0").json()
        assert p["voice_drafts"] == []
        assert client.get("/liff/voice/draft?rid=0").status_code == 404
        assert not any(f.exists() for f in files) and outside.exists()  # 音声の出力先の外は消さない
        assert client.delete("/api/profile/voice/0").status_code == 404
        r = stream(client, uid, "message", greeting["thread"], "探して")[-1]
        assert r["kind"] == "reply"
        assert "音声相談の下書きを消した" in remembered.model.contexts[-1][-2].content


# ----------------------------- (4) すべて消す -----------------------------


def test_forget_all_starts_over_as_a_first_visit(remembered):
    with _client(remembered) as client:
        client.get("/")
        uid = client.cookies[remembered.ANON_COOKIE]
        _, greeting = open_talk(client)
        stream(client, uid, "message", greeting["thread"], "探して")
        logic.save_preferences(remembered.state, uid, {"area": "新宿", "purpose": "生活費"})
        logic.save_job(remembered.state, uid, min(logic.load_jobs()))
        remembered.later(31)
        _, again = open_talk(client)
        assert again["entry"] == "revisit"  # 覚えているので再訪
        used = client.app.state.quota.used(uid)

        assert client.post("/api/profile/forget").json() == {"forgotten": True}
        p = client.get("/api/profile").json()
        assert all(v is None for v in _items(p).values()) and p["saved_jobs"] == []
        assert client.get("/api/talk/history").json() == {"thread": 0, "turns": [], "search_track": "未定"}  # 会話も消える
        # 30分以内に開き直しても、前の会話を続けず初回訪問として始まる
        remembered.later(0)
        opened, first = open_talk(client)
        assert opened["event"] == "visit" and first["entry"] == "visit" and first["thread"] == 1
        assert client.app.state.quota.used(uid) == used + 1  # 会話数の上限の数は消さない(挨拶の1回分だけ増える)
        assert logic.conversation_summary(remembered.state, uid)["stated_preferences"] == {}


def test_forget_all_that_fails_midway_deletes_no_conversation_and_can_be_pressed_again(remembered, monkeypatch):
    """「すべて消す」が会話の記録を消すトランザクションの途中で失敗した: 会話は1つも消えず(番号も戻らない)、
    dip の状態(希望・保存)も消えていない(消えたものが無いので、30分以内に開き直して前の会話が続いても、消したはずの
    ものが戻ることはない)。もう一度押せば全部消えて1番から始まる。"""
    import sqlite3

    with _client(remembered) as client:
        client.get("/")
        uid = client.cookies[remembered.ANON_COOKIE]
        for _ in range(2):
            remembered.later(31)
            _, greeting = open_talk(client)
            stream(client, uid, "message", greeting["thread"], OLD_WORDS)
        logic.save_preferences(remembered.state, uid, {"area": "新宿", "purpose": "生活費"})
        logic.save_job(remembered.state, uid, min(logic.load_jobs()))
        rt = client.app.state.rt
        before = _checkpoint_rows(rt, uid)
        assert sorted(before) == [f"{uid}:1", f"{uid}:2"]
        profile_before = client.get("/api/profile").json()

        _fail_midway(monkeypatch)
        with pytest.raises(sqlite3.OperationalError):
            client.post("/api/profile/forget")
        _heal(monkeypatch)
        assert _checkpoint_rows(rt, uid) == before
        assert client.get("/api/profile").json() == profile_before  # dip の状態も消えていない
        rt._threads.clear()  # 再起動
        assert asyncio.run(rt.current_thread(uid)) == 2

        assert client.post("/api/profile/forget").json() == {"forgotten": True}
        assert _checkpoint_rows(rt, uid) == {}
        p = client.get("/api/profile").json()
        assert all(v is None for v in _items(p).values()) and p["saved_jobs"] == []
        assert client.get("/api/talk/history").json() == {"thread": 0, "turns": [], "search_track": "未定"}
        rt._threads.clear()
        opened, first = open_talk(client)
        assert opened["event"] == "visit" and first["thread"] == 1


def test_forget_all_that_fails_after_erasing_the_conversations_does_not_bring_them_back(remembered, monkeypatch):
    """「すべて消す」が会話を消した後、dip の状態を消すところで失敗した: 30分以内に開き直しても前の会話は続かず
    (描かず、文脈にも入らない)、新しい会話が1番から始まる。もう一度押せば dip の状態も消える。"""
    from dip_mcp import profile

    with _client(remembered) as client:
        client.get("/")
        uid = client.cookies[remembered.ANON_COOKIE]
        _, greeting = open_talk(client)
        stream(client, uid, "message", greeting["thread"], OLD_WORDS)
        logic.save_preferences(remembered.state, uid, {"area": "新宿", "purpose": "生活費"})
        rt = client.app.state.rt
        forget_all = profile.forget_all

        def broken(*a, **kw):
            raise RuntimeError("dip の状態を書けない")

        monkeypatch.setattr(profile, "forget_all", broken)
        with pytest.raises(RuntimeError):
            client.post("/api/profile/forget")
        monkeypatch.setattr(profile, "forget_all", forget_all)
        assert _checkpoint_rows(rt, uid) == {}

        remembered.later(0)  # 30分以内に開き直す
        opened, first = open_talk(client)
        assert opened["event"] is not None and first["thread"] == 1  # 前の会話を続けず、新しい会話の挨拶から
        stream(client, uid, "message", 1, "探して")
        assert not any(OLD_WORDS in json.dumps(m.content, ensure_ascii=False) for m in remembered.model.contexts[-1])
        assert OLD_WORDS not in json.dumps(client.get("/api/talk/history").json(), ensure_ascii=False)

        assert client.post("/api/profile/forget").json() == {"forgotten": True}  # もう一度押す
        assert logic.conversation_summary(remembered.state, uid)["stated_preferences"] == {}
        assert _checkpoint_rows(rt, uid) == {}


def test_forget_all_also_clears_the_dev_scenario_users_of_this_browser(remembered):
    """/dev のシナリオ利用者(「匿名ID:シナリオユーザー」)も同じ Cookie の本人の記録なので、「すべて消す」で /dev に
    見えていた記録(dip の状態・会話)も消える。別のブラウザのシナリオ利用者の記録は、名乗っても消せない。"""
    with _client(remembered) as a:
        b = _client(remembered)
        a.get("/dev"), b.get("/dev")
        rt = a.app.state.rt
        mine = a.post("/api/scenarios/R-revisit-browsing/start").json()
        theirs = b.post("/api/scenarios/R-revisit-browsing/start").json()
        stream(a, mine["user"], mine["event"], None)
        stream(a, mine["user"], "message", None, "探して")
        # 別のブラウザの会話(b はこのアプリの lifespan の外なので、同じランタイムで直接進める)
        a.portal.call(rt.handle, theirs["user"], theirs["event"])
        a.portal.call(rt.handle, theirs["user"], "message", "探して")
        for r in (mine, theirs):
            logic.save_preferences(remembered.state, r["user"], {"area": "新宿"})
            logic.save_job(remembered.state, r["user"], "a003")
            logic.submit_application(remembered.state, r["user"], "a003")
        seen = a.post("/api/dip", json={"user_id": mine["user"]}).json()
        assert seen["summary"]["stated_preferences"] and seen["saves"] and seen["applications"]
        assert behavior.view(remembered.state, mine["user"])["recent_views"] and a.portal.call(rt.current_thread, mine["user"]) == 1

        assert a.post("/api/profile/forget", params={"user_id": theirs["user"]},
                      json={"user_id": theirs["user"]}).json() == {"forgotten": True}
        seen = a.post("/api/dip", json={"user_id": mine["user"]}).json()
        assert seen["summary"]["stated_preferences"] == {} and seen["summary"]["linked"] is False
        assert seen["saves"] == seen["applications"] == seen["outcomes"] == [] and seen["last_contact"] is None
        assert not behavior.view(remembered.state, mine["user"])["recent_views"]
        assert a.portal.call(rt.current_thread, mine["user"]) == 0  # 会話も消える
        # 別のブラウザのシナリオ利用者はそのまま
        kept = b.post("/api/dip", json={"user_id": theirs["user"]}).json()
        assert kept["summary"]["stated_preferences"] and kept["saves"] and kept["applications"]
        assert a.portal.call(rt.current_thread, theirs["user"]) == 1
        assert a.post("/api/dip", json={"user_id": theirs["user"]}).status_code == 403


# ----------------------------- (5) 本人の Cookie のユーザーだけ -----------------------------


def _cookie_user(client) -> str:
    """画面を開いたときと同じく GET / で匿名IDの Cookie を受け取る(API は発行しない)。"""
    client.get("/")
    return client.cookies["aica_uid"]


PROFILE_CALLS = [("get", ""), ("put", "/items/area"), ("delete", "/items/area"), ("put", "/track"),
                 ("delete", "/track"), ("delete", "/resume"), ("delete", "/voice/0"), ("delete", "/saved/x"),
                 ("post", "/forget")]


def test_another_users_profile_can_be_neither_read_nor_changed(remembered):
    """パスにユーザーIDは無く、Cookie の本人の分だけを読む・変える。他人の匿名IDを本文やクエリで名乗っても届かない。"""
    with _client(remembered) as a:
        b = _client(remembered)
        ua, ub = _cookie_user(a), _cookie_user(b)
        assert ua != ub
        logic.save_preferences(remembered.state, ub, {"area": "渋谷"})
        logic.save_job(remembered.state, ub, min(logic.load_jobs()))
        assert _items(a.get("/api/profile", params={"user_id": ub}).json())["area"] is None  # 他人の分は見えない
        for method, path in PROFILE_CALLS:
            body = {"json": {"value": "新宿", "track": "正社員", "user_id": ub}} if method == "put" else {}
            assert getattr(a, method)(f"/api/profile{path}", params={"user_id": ub}, **body).status_code in (200, 404), \
                (method, path)
        after = remembered.state.user(ub)
        assert logic.stated_preferences(after)["area"]["value"] == "渋谷" and logic.saved_jobs(remembered.state, ub)  # 変わっていない
        # 旧来の「パスにユーザーID」の形は無い
        assert a.get(f"/api/profile/{ub}").status_code == 404


@pytest.mark.parametrize("cookies", [{}, {"aica_uid": "x"}, {"aica_uid": "w-XYZ"}], ids=["no-cookie", "malformed", "not-hex"])
def test_profile_routes_need_a_well_formed_cookie(remembered, cookies):
    from fastapi.testclient import TestClient

    with _client(remembered):
        nc = TestClient(remembered.app, cookies=cookies)
        for method, path in PROFILE_CALLS:
            body = {"json": {"value": "新宿", "track": "正社員"}} if method == "put" else {}
            assert getattr(nc, method)(f"/api/profile{path}", **body).status_code == 401, (method, path)


# ----------------------------- (6) 個人情報らしい値は、手で直すときも弾く -----------------------------


@pytest.mark.parametrize("value", ["090-1234-5678", "連絡は09012345678", "misaki@example.com",
                                   "住所は新宿区西新宿2-8-1", "西新宿三丁目に住んでいます", "1-2-3 に住んでいます",
                                   "０９０－１２３４－５６７８", "090ー1234ー5678", "自宅は西新宿２－８－１"])
def test_personal_info_is_rejected_when_editing_by_hand(remembered, value):
    with _client(remembered) as client:
        uid = _cookie_user(client)
        for key in ("area", "experience", "consultation_summary"):
            r = client.put(f"/api/profile/items/{key}", json={"value": value})
            assert r.status_code == 400 and "個人情報" in r.json()["detail"], (key, value)
        p = client.get("/api/profile").json()
        assert all(v is None for v in _items(p).values()) and p["consultation_summary"] is None
        assert remembered.state.user(uid).get("profile_changes") in (None, [])


def test_values_that_only_look_like_numbers_are_kept_when_editing_by_hand(remembered):
    with _client(remembered) as client:
        _cookie_user(client)
        for key, value in (("min_annual_salary", "年収400-500万円"), ("start_timing", "3-6か月以内"),
                           ("area", "新宿区"), ("area", "新宿三丁目")):  # 住む言葉の無い地名は住所とみなさない
            assert client.put(f"/api/profile/items/{key}", json={"value": value}).status_code == 200, value
        assert client.put("/api/profile/items/wants", json={"value": "あ" * 101}).status_code == 400


# ----------------------------- ブラウザ(Playwright と Chromium があるときだけ) -----------------------------


def test_profile_screen_in_the_browser(remembered, browser, served_profile):  # noqa: F811
    """ナビの「プロフィール」で覚えていることが出る(空の項目は「まだありません」)。直す・個人情報を弾く・
    「すべて消す」(確認の1段つき)で、トークが初回訪問からやり直しになる。"""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    page.goto(served_profile + "/")
    page.get_by_text("こんにちは。お仕事探しをお手伝いします。何をしますか?").wait_for()
    uid = next(c["value"] for c in context.cookies() if c["name"] == "aica_uid")
    logic.save_preferences(remembered.state, uid, {"area": "新宿", "purpose": "生活費の足しに"})
    logic.set_search_track(remembered.state, uid, "アルバイト・パート", "空いた時間にバイトを")

    page.locator("#nav button[data-view=profile]").click()
    prof = page.locator("#profile")
    prof.locator(".pcard").first.wait_for()
    text = prof.inner_text()
    for title in ("雇用区分", "目的", "希望", "経歴", "相談のまとめ", "履歴書の読み取り結果", "音声相談の下書き",
                  "保存した求人", "応募した求人", "すべて消す"):
        assert title in text, title
    assert "生活費の足しに" in text and "「空いた時間にバイトを」で 未定 → アルバイト・パート" in text
    assert prof.locator(".pv.empty", has_text="まだありません").count() >= 8

    # 直す: 本人の言葉で上書き。電話番号は弾いて理由を出す
    row = prof.locator('.prow[data-key="area"]')
    row.locator("button.edit").click()
    row.locator("textarea").fill("090-1234-5678")
    row.locator("button.ok").click()
    row.locator(".perr", has_text="個人情報").wait_for()
    row.locator("textarea").fill("渋谷")
    row.locator("button.ok").click()
    prof.locator('.prow[data-key="area"] .pv', has_text="渋谷").wait_for()
    prof.locator('.prow[data-key="purpose"] button.del').click()
    prof.locator('.prow[data-key="purpose"] .pv.empty').wait_for()
    assert logic.stated_preferences(remembered.state.user(uid)).keys() == {"area"}

    # トークに戻って話すと、直した値で返事が来る
    page.locator("#nav button[data-view=talk]").click()
    page.locator("#t").fill("探して")
    page.locator("#send").click()
    page.get_by_text("エリアは渋谷で探しますね。").wait_for()
    assert "生活費" not in page.locator("#log .row.bot").last.inner_text()

    # すべて消す: 説明と確認に、応募を含めて消えるものと、消えないもの(その日の利用回数)が出る。
    # 確認の1段を挟み、やめれば何も消えない
    page.locator("#nav button[data-view=profile]").click()
    forget = prof.locator('.prow[data-key="forget"]')
    forget.wait_for()
    what = "aica が覚えていること・保存した求人・応募・これまでの会話"
    assert prof.locator(".pcard.danger .pnote").inner_text() == \
        f"{what}をすべて消します。その日の利用回数は消えません。次に開くと、はじめての訪問として始まります。"
    forget.locator("button.del").click()
    assert forget.locator(".pconfirm span").inner_text() == \
        f"本当にすべて消しますか?{what}が消え、元に戻せません。その日の利用回数は消えません。"
    forget.locator(".pconfirm button", has_text="やめる").click()
    assert logic.stated_preferences(remembered.state.user(uid))
    forget.locator("button.del").click()
    forget.locator(".pconfirm button.danger").click()
    page.locator("#v-talk").wait_for(state="visible")
    page.get_by_text("こんにちは。お仕事探しをお手伝いします。何をしますか?").wait_for()
    assert page.locator("#log .row.me").count() == 0  # 前の会話は描かない
    assert logic.stated_preferences(remembered.state.user(uid)) == {}
    assert remembered.state.user(uid).get("talk_opens")  # 初回訪問の挨拶が返った
    page.reload()
    page.get_by_text("こんにちは。お仕事探しをお手伝いします。何をしますか?").wait_for()
    context.close()


def test_voice_sheet_asks_before_closing_mid_call_and_a_late_result_is_drawn_before_the_next_turn(
        remembered, browser, served_profile, livekit):  # noqa: F811
    """通話中に × を押すと確認が出る。結果が届く前に閉じても、結果は会話に入るので、次の発言の前に履歴を読み直して描く。"""
    from tests.test_talk_actions import _fake_livekit, _post_json

    context = browser.new_context()
    requests = _fake_livekit(context)
    page = context.new_page()
    page.goto(served_profile + "/")
    page.get_by_text("こんにちは。お仕事探しをお手伝いします。何をしますか?").wait_for()
    uid = next(c["value"] for c in context.cookies() if c["name"] == "aica_uid")
    page.locator("#plus").click()
    page.locator("#menu-voice").click()
    page.wait_for_function("window.__lk && window.__lk.mic === true && !document.getElementById('voice-end').disabled")
    page.locator("#voice-close").click()
    page.locator("#voice-confirm", has_text="通話中です").wait_for()
    assert page.evaluate("window.__lk.disconnected") == 0  # まだ切らない
    page.locator("#voice-quit").click()
    page.locator("#voice-sheet").wait_for(state="hidden")

    # 閉じた後に結果が届く(会話には入るが、この画面はもう待っていない)
    room = json.loads(next(u for u in requests if "/api/voice/wait" in u).split(" ", 1)[1])["room"]
    _post_json(served_profile + "/api/voice/result", {"user_id": uid, "room": room, "draft_markdown": "# 下書き",
                                                      "preferences": {"area": "中野"}})
    page.wait_for_timeout(500)
    assert page.get_by_text("音声でお話しした内容をまとめました。").count() == 0
    page.locator("#t").fill("探して")
    page.locator("#send").click()
    summary = page.locator("#log .row.bot", has_text="音声でお話しした内容をまとめました。")
    summary.wait_for()
    assert "エリア: 中野" in summary.inner_text()
    # 結果 → 本人の発言 → AI の返信 の順に並ぶ
    rows = page.locator("#log > .row").all_inner_texts()
    i = next(k for k, t in enumerate(rows) if "音声でお話しした内容をまとめました。" in t)
    assert "探して" in rows[i + 1]
    page.locator("#log .row.bot").nth(-1).get_by_text("エリアは").wait_for()
    context.close()


@pytest.fixture
def served_profile(remembered):
    """本物の HTTP サーバー(uvicorn)で画面を出す(tests/test_talk_actions.py の served と同じ)。"""
    import threading
    import time

    import uvicorn

    from tests.test_talk_actions import _free_port

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(remembered.app, host="127.0.0.1", port=port, log_level="warning"))
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


# ----------------------------- 追記(aica v2 の G): プロフィール画面・公開前の守りの PR レビューからの後続指摘 -----------------------------


def _upload(client, uid, th):
    r = client.post("/api/resume/upload", params={"thread": th}, data={"user_id": uid},
                    files={"file": (FILENAME, b"%PDF-1.4 dummy", "application/pdf")})
    assert r.status_code == 200
    return r.json()["view_url"]


def test_uploading_again_after_deleting_the_resume_does_not_tell_the_ai_to_avoid_it(remembered):
    """履歴書を消した直後に別の履歴書を上げたら、アップロードのターンに「消した履歴書は使わず触れない」の知らせを渡さない
    (新しい履歴書まで避けうる)。負例: 同じ間に直したほかの項目の知らせは、これまでどおり渡す。"""
    with _client(remembered) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        _upload(client, uid, th)
        stream(client, uid, "resume_upload", th)
        assert client.delete("/api/profile/resume").json()["resume"] is None
        assert profile.pending_changes(remembered.state, uid)  # 「消した」の知らせはまだ渡していない
        # 履歴書を消すと今の会話は閉じる(task 115): 画面は新しい会話を開き直してから上げ直す
        _, greeting = open_talk(client)
        th = greeting["thread"]
        assert client.put("/api/profile/items/area", json={"value": "渋谷"}).status_code == 200
        _upload(client, uid, th)
        stream(client, uid, "resume_upload", th)
        notices = [m.content for m in remembered.model.contexts[-1] if m.type == "human" and m.content.startswith(NOTICE)]
        assert notices and "エリア・駅を「渋谷」に直した" in notices[-1]
        assert "履歴書の読み取り結果を消した" not in notices[-1] and '"uploaded_resume": "あり"' in notices[-1]
        assert "履歴書の経歴も踏まえます。" in stream(client, uid, "message", th, "探して")[-1]["bubbles"]


def test_unsaving_a_job_reaches_the_ai(remembered):
    """「保存をやめる」も、次の本人のターンの前に AI へ伝わる。負例: 保存していない求人をやめても知らせは出さない。"""
    with _client(remembered) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        job = min(logic.load_jobs())
        logic.save_job(remembered.state, uid, job)
        assert client.delete(f"/api/profile/saved/{job}").json()["saved_jobs"] == []
        stream(client, uid, "message", th, "探して")
        notice = remembered.model.contexts[-1][-2].content
        assert notice.startswith(NOTICE) and f"求人「{logic.load_jobs()[job].title}」の保存を消した" in notice
        client.delete(f"/api/profile/saved/{job}")  # もう保存していない
        stream(client, uid, "message", th, "探して")
        assert not remembered.model.contexts[-1][-2].content.startswith(NOTICE)


def test_forget_all_keeps_the_link_numbers_so_an_old_link_opens_nothing(remembered):
    """公開前の守りの PR レビューからの後続指摘(決定: 連番を持ち越す): 「すべて消す」の後も、履歴書の読み取り結果と
    音声の下書きの番号は 0 に戻らない。同じブラウザに残った古い rid=0 のリンクは、消した後の新しい結果を開かない。"""
    with _client(remembered) as client:
        opened, greeting = open_talk(client)
        uid, th = opened["user"], greeting["thread"]
        old_resume = _upload(client, uid, th)
        client.post("/api/voice/result", json={"user_id": uid, "draft_markdown": "# 履歴書(下書き)\n- 消す前"},
                    headers=WORKER)
        assert old_resume.endswith("rid=0")
        assert client.post("/api/profile/forget").json() == {"forgotten": True}

        _, greeting = open_talk(client)
        new_resume = _upload(client, uid, greeting["thread"])
        client.post("/api/voice/result", json={"user_id": uid, "draft_markdown": "# 履歴書(下書き)\n- 消した後"},
                    headers=WORKER)
        assert new_resume.endswith("rid=1") and client.get(old_resume).status_code == 404
        assert client.get(new_resume).status_code == 200
        assert client.get("/liff/voice/draft?rid=0").status_code == 404
        assert "消した後" in client.get("/liff/voice/draft?rid=1").text
        assert [d["id"] for d in client.get("/api/profile").json()["voice_drafts"]] == [1]
        # 持ち越すのは番号だけ: 覚えていることは何も残らない
        assert logic.conversation_summary(remembered.state, uid)["stated_preferences"] == {}


def test_forget_all_clears_scenario_users_found_only_in_dip(remembered):
    """「すべて消す」で、同じブラウザの /dev のシナリオ用ユーザー(「匿名ID:シナリオユーザー」)の状態と会話も消える。
    負例: 別のブラウザのシナリオ用ユーザーは消えない。"""
    with _client(remembered) as client:
        client.get("/")
        uid = client.cookies[remembered.ANON_COOKIE]
        scoped = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert scoped.startswith(f"{uid}:")
        stream(client, scoped, "visit", None)
        logic.save_preferences(remembered.state, scoped, {"area": "新宿"})
        other = "w-9999999999999999:sim-new"
        logic.save_preferences(remembered.state, other, {"area": "渋谷"})
        assert client.portal.call(client.app.state.rt.current_thread, scoped) == 1

        assert client.post("/api/profile/forget").json() == {"forgotten": True}
        assert logic.conversation_summary(remembered.state, scoped)["stated_preferences"] == {}
        assert client.portal.call(client.app.state.rt.current_thread, scoped) == 0
        assert logic.conversation_summary(remembered.state, other)["stated_preferences"]["area"]["value"] == "渋谷"
