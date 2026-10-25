"""LINE で見る、消す(task 215、docs/design/privacy.md の 3.7)。実 LINE も実 LLM も呼ばない: LINE の API は偽物
(tests.test_line_webhook.FakeLine)、LLM は決められた順に返す偽のモデル。値はすべて架空。

- 「データを見る」: プロフィール画面と同じ中身をテキストで返す。LLM を呼ばず、会話数も数えない
- 「データを消す」: 確かめのクイックリプライ(postback)の「すべて消す」を押したときだけ、Web の「すべて消す」と同じ範囲を消す
- 友だち解除(unfollow): 消さない
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from dip_mcp import accounts, arcade, behavior, logic
from line_runtime import line_channel
from line_runtime.line_channel import line_user
from resume_arcade.draft import to_envelope
from tests.test_handle import reply
from tests.test_line_webhook import (  # noqa: F401  line_id_key はフィクスチャ(LINE のユーザーIDの鍵)
    RAW_OTHER,
    RAW_USER,
    REPLY,
    _event,
    _post,
    _server,
    line_id_key,
)
from tests.test_resume_upload import clean_draft
from tests.test_revisit import Greeter, greet

WEB_USER = "w-00000000000000aa"  # 架空の匿名ユーザーID
NOTE = "みほん駅の近くで日中のカフェを探している。次は週の日数を確かめる。"


def _fill(server, uid: str, files: list[str] | None = None) -> None:
    """プロフィール画面に出るものを一通り入れる(雇用区分、希望、まとめ、履歴書、音声の下書き、ゲームの下書き、保存、応募、連携)。
    files は音声相談で書き出した下書きのファイル。"""
    st = server.state
    logic.set_search_track(st, uid, "アルバイト・パート", "バイトを探しています")
    logic.save_preferences(st, uid, {"area": "みほん駅", "time_band": "日中", "job_type": "カフェ"})
    logic.record_outcome(st, uid, "consult", "希望を聞いた", 0.7, summary=NOTE)
    logic.save_resume_upload(st, uid, {"resume": clean_draft().model_dump()}, file="履歴書(PDF)")
    logic.record_voice_result(st, uid, [], "# 履歴書の下書き", files=files, room="room-fake-1")
    play = arcade.start_play(st, uid, "harvest-of-work", ai=False)
    arcade.save_draft(st, uid, "harvest-of-work", play["id"], to_envelope("# [氏名]\n\n## 職務経歴\n", "職務経歴書"))
    job_id = next(iter(logic.load_jobs()))
    behavior.record(st, uid, "save", job_id)
    behavior.record(st, uid, "apply", job_id, status="応募済み")
    accounts.of(st).set_link(uid, "dip-fake-0001")


def _texts(fake) -> list[list[str]]:
    """届けたメッセージの本文(1 回の返信ごと)。"""
    return [[m.get("text", "") for m in body["messages"]] for _, body in fake.sent()]


def _say(client, text: str, n: int, user: str = RAW_USER):
    return _post(client, [_event("message", user=user, text=text, event_id=f"01D{n}", token=f"reply-token-{n}")])


def _tap(client, data: str, n: int, user: str = RAW_USER):
    e = _event("postback", user=user, event_id=f"01P{n}", token=f"reply-token-p{n}") | {"postback": {"data": data}}
    return _post(client, [e])


# ----------------------------- データを見る -----------------------------


def test_view_data_sends_a_summary_of_what_is_saved_without_the_llm(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    model = Greeter(script=[reply(["承知しました。"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _say(client, "こんにちは", 1)  # 会話を 1 つ保存する
        _fill(server, uid)
        used = server.app.state.quota.used(uid)
        assert _say(client, " データを見る ", 2).status_code == 200
        assert server.app.state.quota.used(uid) == used  # 会話数を使わない

    assert model.script == []  # 「データを見る」で LLM を呼ばない
    (path, body) = fake.sent()[-1]
    assert path == REPLY and body["replyToken"] == "reply-token-2"
    assert 1 <= len(body["messages"]) <= line_channel.MAX_MESSAGES
    assert all(m["type"] == "text" and len(m["text"]) <= 5000 for m in body["messages"])
    text = "\n".join(m["text"] for m in body["messages"])
    job = next(iter(logic.load_jobs().values()))
    for want in (line_channel.VIEW_INTRO, "■ 雇用区分\nアルバイト・パート", "バイトを探しています", "エリア・駅: みほん駅",
                 NOTE, "■ 履歴書の読み取り結果", "に読み取り", "■ 音声相談の下書き\n・", "■ ゲームで作った下書き\n・", "「仕事の収穫」の職務経歴書", f"■ 保存した求人\n・{job.title}",
                 f"■ 応募した求人\n・{job.title}", "■ dip の会員との連携\nあり", "このトークでの、あなたの発言と aica の返信",
                 line_channel.FORGET_DATA_TEXT):
        assert want in text, want
    assert RAW_USER not in text and uid not in text  # LINE の userId も aica のユーザーIDも出さない


def test_view_data_for_a_new_user_says_none_and_writes_nothing(tmp_path, monkeypatch):
    uid = line_user(RAW_USER)
    model = Greeter(script=[reply(["呼ばれてはいけない"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _say(client, "データを見る", 1)
    assert len(model.script) == 1
    ((_, body),) = fake.sent()
    text = "\n".join(m["text"] for m in body["messages"])
    assert "■ 雇用区分\n未定" in text and "■ 会話\nなし" in text and "■ 保存した求人\nなし" in text
    assert uid not in server.state.user_ids()  # 見るだけでは dip の状態にユーザーを作らない


def test_view_data_fits_in_five_line_messages_even_when_long_and_keeps_every_section():
    """どれだけ長くても 5 つまで、1 つ 5000 字まで(LINE の上限。絵文字は 2 字に数える)。どの区分けも落とさず、
    減らした行は「ほか N 件」と書く。"""
    long = "🙂" * 150 + "架空の"
    history = [{"when": "10/1", "from": "未定", "track": "正社員", "undo": False, "evidence": long} for _ in range(80)]
    sections = [{"title": t, "items": [{"label": f"項目{i}", "value": long, "saved": "10/1"} for i in range(16)]}
                for t in ("目的", "希望", "経歴")]
    view = {"track": {"current": "正社員", "history": history}, "sections": sections,
            "consultation_summary": {"text": long, "recorded": "10/1"},
            "resume": {"uploaded": "10/1", "highlights": [long] * 30},
            "voice_drafts": [{"at": "10/1", "document_kind": "履歴書"}] * 300,
            "arcade_drafts": [{"at": "10/1", "game": long, "document_kind": "職務経歴書"}] * 100,
            "saved_jobs": [{"title": long, "saved_at": "10/1"}] * 200,
            "applications": [{"title": long, "job_id": "x", "applied": "10/1", "status": "応募済み"}] * 200}
    msgs = line_channel.data_view_messages(view, talked=True, linked=True)
    assert len(msgs) <= line_channel.MAX_MESSAGES
    assert all(len(m["text"].encode("utf-16-le")) // 2 <= 5000 for m in msgs)
    text = "\n".join(m["text"] for m in msgs)
    assert msgs[0]["text"].startswith(line_channel.VIEW_INTRO) and text.endswith(line_channel.VIEW_OUTRO)
    for title in ("雇用区分", "目的", "希望", "経歴", "相談のまとめ", "履歴書の読み取り結果", "音声相談の下書き",
                  "ゲームで作った下書き", "保存した求人", "応募した求人", "dip の会員との連携\nあり", "会話\nこのトーク"):
        assert f"■ {title}" in text, title
    assert any(f"ほか {200 - cap} 件" in text for cap in line_channel.VIEW_CAPS)  # 保存した求人 200 件のうち、出せなかった分



def test_view_data_splits_no_message_past_5000_units_when_one_section_is_full_of_emoji():
    """1 つの区分けだけが長く、絵文字が多くても(全体は 5 つに収まる)、1 つ 5000 単位を超えない。行は UTF-16 の単位で
    VIEW_ITEM_CHARS までに切り、絵文字を途中で割らない。"""
    emoji = "🙂" * 200  # 根拠の発言の上限(200 字)の内
    history = [{"when": "10/1", "from": "未定", "track": "正社員", "undo": False, "evidence": emoji} for _ in range(20)]
    view = {"track": {"current": "正社員", "history": history}, "sections": [], "consultation_summary": None,
            "resume": None, "voice_drafts": [], "saved_jobs": [], "applications": []}
    msgs = line_channel.data_view_messages(view, talked=False, linked=False)
    assert len(msgs) <= line_channel.MAX_MESSAGES
    assert all(len(m["text"].encode("utf-16-le")) // 2 <= line_channel.MAX_TEXT_CHARS for m in msgs)
    text = "\n".join(m["text"] for m in msgs)
    assert text.count("・10/1 未定→正社員") == 20  # 行は減らさずに収まる
    lines = [x for x in text.split("\n") if x.startswith("・")]
    assert all(len(x.encode("utf-16-le")) // 2 <= line_channel.VIEW_ITEM_CHARS and x.endswith("🙂…") for x in lines)


# ----------------------------- データを消す -----------------------------


def _ask(client, fake, n: int, user: str = RAW_USER) -> str:
    """「データを消す」と送り、確かめのクイックリプライの「すべて消す」の postback のデータを返す。"""
    _say(client, "データを消す", n, user)
    (msg,) = fake.sent()[-1][1]["messages"]
    assert msg["text"] == line_channel.FORGET_ASK_TEXT
    items = [i["action"] for i in msg["quickReply"]["items"]]
    assert [(a["type"], a["label"], a["displayText"]) for a in items] == [
        ("postback", line_channel.FORGET_LABEL, line_channel.FORGET_LABEL),
        ("postback", line_channel.FORGET_CANCEL_LABEL, line_channel.FORGET_CANCEL_LABEL)]
    assert items[1]["data"] == line_channel.FORGET_CANCEL_POSTBACK
    return items[0]["data"]


def test_forget_data_asks_first_and_then_erases_the_same_as_the_web_forget_all(tmp_path, monkeypatch):
    """LINE の「データを消す」→「すべて消す」の後の dip の状態と連携と会話は、Web のプロフィール画面の「すべて消す」の後と同じ。
    次の友だち追加は初回訪問の挨拶になる。"""
    from voice_resume import config as voice_config

    uid = line_user(RAW_USER)
    model = Greeter(script=[reply(["承知しました。"]), greet])
    server, fake = _server(tmp_path, monkeypatch, model)
    monkeypatch.setattr(voice_config, "OUT_DIR", tmp_path / "voice-resume")
    draft = tmp_path / "voice-resume" / "draft-fake.md"
    draft.parent.mkdir()
    draft.write_text("# 架空の下書き", encoding="utf-8")
    with TestClient(server.app) as client:
        rt = server.app.state.rt
        _say(client, "こんにちは", 1)
        _fill(server, uid, files=[str(draft)])
        _fill(server, WEB_USER)
        server.app.state.unread_uploads[uid] = {"fake": True}  # 読み取れなかった直前のアップロード
        used = server.app.state.quota.used(uid)

        data = _ask(client, fake, 2)
        assert server.state.user(uid).get("preferences")  # 確かめるまでは消さない
        assert client.portal.call(rt.current_thread, uid) == 1
        assert any(r["user_id"] == uid for r in rt.ledger.rows)

        assert _tap(client, data, 3).status_code == 200
        assert _texts(fake)[-1] == [line_channel.FORGOT_TEXT]
        assert client.portal.call(rt.current_thread, uid) == 0  # 会話も消えた
        assert not draft.exists() and uid not in server.app.state.unread_uploads
        assert server.app.state.quota.used(uid) == used  # 会話数は使わず、消しもしない
        assert not any(r["user_id"] == uid for r in rt.ledger.rows)  # 費用台帳のユーザーIDも外れる(金額は残る)

        client.cookies.set(server.ANON_COOKIE, WEB_USER)
        assert client.post("/api/profile/forget").status_code == 200

        def after(u: str) -> dict:
            return {k: v for k, v in server.state.user(u).items() if k != "line_user_id"}

        assert after(uid) == after(WEB_USER)  # 残るのは、どちらも同じ(初期の値と、持ち越す連番)
        gone = ("track_history", "preferences", "resume_upload", "voice_results", "arcade_drafts", "arcade_plays",
                "dip_writes", "talk_opens")
        assert not any(after(uid).get(k) for k in gone) and "resume_upload_seq" in after(uid)
        assert not any(o.get("summary") for o in after(uid).get("outcomes", []))
        assert behavior.dip_id(server.state, uid) is None is behavior.dip_id(server.state, WEB_USER)

        _post(client, [_event("follow", event_id="01F9", token="reply-token-f")])
    assert model.script == []
    assert _texts(fake)[-1][0].startswith("こんにちは")  # はじめての方として始まる(「おかえりなさい」ではない)


def test_writing_the_button_text_or_an_old_or_cancelled_confirmation_erases_nothing(tmp_path, monkeypatch):
    """「すべて消す」と文で書いてもふつうの発言(LLM に流れる)。やめた確かめ、期限の切れた確かめ、ほかの人の確かめ、
    2 回目の押し直しでは消さない。"""
    uid = line_user(RAW_USER)
    model = Greeter(script=[reply(["どの情報を消したいですか?"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _fill(server, uid)
        _say(client, line_channel.FORGET_LABEL, 1)  # 文で書いた「すべて消す」
        assert model.script == []

        data = _ask(client, fake, 2)
        _tap(client, line_channel.FORGET_CANCEL_POSTBACK, 3)
        assert _texts(fake)[-1] == [line_channel.FORGET_CANCELLED_TEXT]
        _tap(client, data, 4)  # やめた後に、前の「すべて消す」を押す
        assert _texts(fake)[-1] == [line_channel.FORGET_EXPIRED_TEXT]

        data = _ask(client, fake, 5)
        nonce, _ = server.app.state.line_forget_asks[uid]
        server.app.state.line_forget_asks[uid] = (nonce, 0.0)  # 期限を過ぎた
        _say(client, "データを見る", 61)  # 見るの決まった文でも、期限の切れた確かめを捨てる
        assert uid not in server.app.state.line_forget_asks
        _tap(client, data, 6)
        assert _texts(fake)[-1] == [line_channel.FORGET_EXPIRED_TEXT]

        old = _ask(client, fake, 7)
        data = _ask(client, fake, 8)
        _tap(client, old, 9)  # 前の確かめは使えない(今の確かめは残る)
        assert _texts(fake)[-1] == [line_channel.FORGET_EXPIRED_TEXT]
        _tap(client, data, 10, user=RAW_OTHER)  # ほかの人が同じデータを送っても、その人の確かめではない
        assert _texts(fake)[-1] == [line_channel.FORGET_EXPIRED_TEXT]
        sent = len(fake.sent())
        _tap(client, line_channel.FORGET_POSTBACK, 11)  # 番号の無いデータは答えない(無視)
        assert len(fake.sent()) == sent and server.state.user(uid).get("preferences")

        _tap(client, data, 12)  # 本人の今の確かめなら消える
        assert _texts(fake)[-1] == [line_channel.FORGOT_TEXT]
        assert not server.state.user(uid).get("preferences")
        _tap(client, data, 13)  # 同じ確かめは 1 回だけ
        assert _texts(fake)[-1] == [line_channel.FORGET_EXPIRED_TEXT]


# ----------------------------- 友だち解除(ブロック) -----------------------------


def test_unfollow_keeps_the_data_and_following_again_continues(tmp_path, monkeypatch):
    """unfollow を受けても消さない(間違えて押すこともあるため。docs/design/privacy.md の 3.7)。解除すれば前の続きから。"""
    uid = line_user(RAW_USER)
    model = Greeter(script=[greet, greet, greet])  # 再訪の挨拶は、要約を読んでから返す
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        _post(client, [_event("follow", event_id="01F1")])
        _fill(server, uid)
        before = server.state.user(uid)
        _post(client, [_event("unfollow", event_id="01U1")])
        assert server.state.user(uid) == before
        assert behavior.dip_id(server.state, uid) == "dip-fake-0001"
        assert client.portal.call(server.app.state.rt.current_thread, uid) == 1
        _post(client, [_event("follow", event_id="01F2", token="reply-token-2")])
    assert len(fake.sent()) == 2  # unfollow には何も送らない(送れない)
    assert model.script == []
    assert _texts(fake)[-1][0].startswith("おかえりなさい。前回はみほん駅・日中・カフェ")  # 消していないので、前の希望から
