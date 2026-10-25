"""送る前に伏せる(task 211、docs/design/privacy.md の 3.2)。実 LLM も実 LINE も OpenAI も呼ばない。

- トークの発言の電話番号・メールアドレス・住所らしい所は、LLM に送る前、会話を保存する前に [個人情報を削除] に置き換わる
  (Web と LINE の入口の turn.accept。handle を直に呼ぶ評価も runtime.event_input で伏せる)。画面には伏せたことを知らせる
- system prompt に LINE のユーザーIDを載せない。ツールの line_user_id は PinUserMiddleware が会話のユーザーに固定する
- dip のツールの結果にもユーザーIDは入らない(LLM に届かない)
- 音声相談の調べものの問いに個人情報らしい値があれば、web で調べずに断る
- 住所らしい所は、同じ文に住んでいる所を言う言葉があるときだけ伏せる。電話番号とメールアドレスはいつも伏せる
- 伏せても、求人の検索と会話の流れは変わらない(ふつうの条件の言い方、駅の名前、丁目は伏せない)

    ./.venv/bin/python -m pytest -q tests/test_redact_before_send.py

値はすべて架空(電話番号・メールアドレス・住所は例示用の形)。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from dip_mcp import logic
from dip_mcp import server as dip_server
from dip_mcp.data import State, load_jobs
from line_runtime import turn
from line_runtime.agent import PINNED_USER
from resume_draft.pii import REDACTED, personal_value, redact_personal
from tests.test_arcade import _free_port, browser  # noqa: F401  browser はフィクスチャ
from tests.test_dip_summary_read import TOOL_ARGS
from tests.test_handle import ScriptedModel, call, reply, run
from tests.test_revisit import Greeter, greet, propose
from tests.test_talk_page import app, open_talk, stream  # noqa: F401  app はフィクスチャ

PHONE, MAIL, ADDRESS = "090-1234-5678", "sample.user@example.com", "みほん市みほん町1丁目2-3"
SAID = f"渋谷で日中に働きたいです。連絡は {PHONE} か {MAIL} に。家は {ADDRESS} です"
SENT = f"渋谷で日中に働きたいです。連絡は {REDACTED} か {REDACTED} に。家は {REDACTED} です"


def _no_personal(text: str) -> bool:
    return all(v not in text for v in (PHONE, MAIL, "1丁目", "2-3"))


# ----------------------------- 受け付けで伏せる -----------------------------


def test_accept_redacts_phone_mail_and_address_and_marks_the_turn():
    t = turn.accept(None, "w-0123456789abcdef", "message", SAID, None)
    assert t.text == SENT and t.redacted is True
    # 全角で書いても伏せる(照合の前に半角にそろえる決まりは resume_draft.pii と同じ)。電話番号は住む言葉が無くても伏せる。
    # 電話番号の形は住所の形(1-2-3)にも当たるが、住む言葉の無い文では住所とみなさないので、電話番号の所だけを伏せる
    t = turn.accept(None, "w-0123456789abcdef", "message", "渋谷で働きたい。電話は０９０－１２３４－５６７８です", None)
    assert t.text == f"渋谷で働きたい。電話は{REDACTED}です" and t.redacted
    t = turn.accept(None, "w-0123456789abcdef", "message", f"{PHONE}に連絡して渋谷で日中がいい", None)
    assert t.text == f"{REDACTED}に連絡して渋谷で日中がいい"
    t = turn.accept(None, "w-0123456789abcdef", "message", f"{MAIL} に送って", None)
    assert t.text == f"{REDACTED} に送って"


# 求人を探すときのふつうの言い方。どれも伏せない(伏せると検索の条件が消える)
ORDINARY = [
    "渋谷で日中", "時給1200円以上で週3-4日", "10時-18時で働きたい", "2026-11-01から働けます", "年収400-500万円",
    "新宿区で3-6か月", "1日8時間まで", "土日祝休みで残業は月10時間まで", "正社員で年収600万以上", "中野駅の近く",
    "まず相談する", "今はいい", "東京都内のコンビニ", "新宿三丁目駅の近くで働きたい", "渋谷駅の近くで",
    # AI は求人の最寄り駅(「新宿三丁目」)をそのまま言う。住む言葉の無い地名、駅、丁目、数の幅は伏せない
    "新宿三丁目の飲食ホールが気になります", "新宿三丁目で働きたい", "新宿三丁目5分以内で探して", "新宿三丁目の5-10人の職場",
    "新宿三丁目18時から働ける", "新宿三丁目1200円以上", "新宿三丁目 18-22", "大井町3-4人の職場", "西新宿2-8-1の近くの店",
    "番地は言いたくないです", "住み込みで新宿三丁目の求人", "住宅手当がある新宿三丁目の求人",
]


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_search_words_are_not_redacted(text):
    t = turn.accept(None, "w-0123456789abcdef", "message", text, None)
    assert t.text == text and t.redacted is False


def test_job_card_buttons_send_the_title_unchanged():
    """求人カードのボタンが送る文(talk-core.js、LINE の Flex)は、どの求人でも伏せない(求人の話が続けられる)。"""
    for job_id, job in load_jobs().items():
        for text in (f"「{job.title}」について詳しく教えて", f"「{job.title}」({job_id})を保存して"):
            t = turn.accept(None, "w-0123456789abcdef", "message", text, None)
            assert t.text == text, text


def test_too_long_is_checked_on_what_the_user_wrote_and_on_what_is_sent():
    with pytest.raises(turn.BadTurn):
        turn.accept(None, "w-0123456789abcdef", "message", PHONE * 500, None)
    # 伏せると長くなる(「番地」の 2 字が [個人情報を削除] の 9 字に)。送る文も上限を超えない
    text = "家は" + " 番地" * 1666
    assert len(text) <= turn.MAX_TEXT_CHARS
    with pytest.raises(turn.BadTurn):
        turn.accept(None, "w-0123456789abcdef", "message", text, None)


def test_redaction_repeats_until_nothing_is_left():
    """電話番号のすぐ後ろにメールアドレスが続いても、どちらも伏せる(1 回では、メールが前の文字に続いて見えて当たらない)。
    伏せた文をもう一度伏せても変わらない(runtime.event_input でもう一度伏せる)。"""
    t = turn.accept(None, "w-0123456789abcdef", "message", f"{PHONE}.{MAIL}", None)
    assert t.text == REDACTED * 2 and redact_personal(t.text) == t.text
    assert redact_personal(SENT) == SENT


def test_addresses_are_redacted_only_with_words_about_where_the_user_lives():
    """住所らしい所は、同じ文に住んでいる所を言う言葉(「住んでいる」「住所」「在住」「自宅」「家は」「引っ越し」など)が
    あるときだけ伏せる。駅の名前や数の幅は住所と同じ形になることがあり、形だけでは見分けられないため。"""
    for text in ("新宿三丁目に住んでいます", "住所は新宿区西新宿2-8-1です", "西新宿三丁目に住んでいます",
                 "新宿三丁目5-1に住んでいます", "渋谷区神南1丁目2-3在住", "自宅は渋谷区神南1-2-3",
                 "実家はみほん市みほん町3丁目", "みほん町３－５に引っ越しました", "１－２－３に住む"):
        t = turn.accept(None, "w-0123456789abcdef", "message", text, None)
        assert t.text == REDACTED and t.redacted, text
        assert personal_value(text), text
    # 伏せるのは住む言葉と同じ文の住所らしい所だけ。ほかの文の条件は残る
    t = turn.accept(None, "w-0123456789abcdef", "message", "新宿三丁目5分以内で探して。家は 渋谷区神南1丁目2-3 です", None)
    assert t.text == f"新宿三丁目5分以内で探して。家は {REDACTED} です"
    t = turn.accept(None, "w-0123456789abcdef", "message", "住所は、みほん市みほん町1丁目。渋谷駅の近くで働きたい", None)
    assert t.text == f"住所は、{REDACTED}。渋谷駅の近くで働きたい"
    # 見出しだけの行(「住所:」「【住所】」「自宅」)の後ろの行の住所も伏せる。連絡先を貼るときによくある書き方
    for text, said in (("住所:\n東京都みほん区みほん町1-2-3", f"住所:\n{REDACTED}"),
                       ("【住所】\nみほん市みほん町1-2-3", f"【住所】\n{REDACTED}"),
                       ("自宅\nみほん市みほん町3丁目", f"自宅\n{REDACTED}"),
                       ("現住所：\n\nみほん市みほん町1-2-3", f"現住所：\n\n{REDACTED}"),
                       ("住所\n〒100-0001\nみほん市みほん町3丁目", f"住所\n〒100-0001\n{REDACTED}"),
                       ("住所:\nみほん市みほん町1-2-3\n新宿三丁目5分以内で探して",
                        f"住所:\n{REDACTED}\n新宿三丁目5分以内で探して")):
        t = turn.accept(None, "w-0123456789abcdef", "message", text, None)
        assert t.text == said and t.redacted, text
        assert personal_value(text), text
    # ふつうの文の改行は、次の行に効かせない
    text = "住所は言いたくないです\n新宿三丁目5分以内で探して"
    assert turn.accept(None, "w-0123456789abcdef", "message", text, None).text == text
    assert not personal_value(text)
    # 住む言葉の無い文は、地名、駅、丁目、番地の形でも伏せない(personal_value も同じ)
    for text in ORDINARY:
        assert not personal_value(text), text


def test_every_job_station_is_kept_with_or_without_eki():
    """求人データのどの最寄り駅も、「駅」を付けても外しても伏せない(AI が言う求人の最寄り駅を本人が返せる)。"""
    for station in {j.station for j in load_jobs().values()}:
        for name in (station, station.removesuffix("駅")):
            text = f"{name}の求人が気になります"
            assert turn.accept(None, "w-0123456789abcdef", "message", text, None).text == text, text


# ----------------------------- Web のトーク: LLM と会話の保存に届かない -----------------------------


def test_web_talk_redacts_before_the_llm_and_the_saved_conversation_and_search_still_works(app):  # noqa: F811
    from fastapi.testclient import TestClient

    seen: list = []

    def search(messages):
        seen.append(list(messages))
        return call("recommend_jobs", {"line_user_id": PINNED_USER, "area": "渋谷", "time_band": "日中"}, "rec")

    def remembers(messages):  # 次のターンでも、前の発言は伏せた文のまま
        seen.append(list(messages))
        return reply(["1件目は駅から近いです。"])

    model = Greeter(script=[greet, search, propose, remembers])
    app.set_model(model)
    with TestClient(app.app) as client:
        opened, greeting = open_talk(client)
        uid = opened["user"]
        evs = stream(client, uid, "message", greeting["thread"], SAID)
        r = evs[-1]
        assert r["kind"] == "reply" and r["redacted"] is True
        assert len(r["cards"]) == 3  # 伏せても求人の検索はそのまま
        nxt = stream(client, uid, "message", greeting["thread"], "1件目は?")[-1]
        assert nxt["bubbles"] == ["1件目は駅から近いです。"] and "redacted" not in nxt  # 伏せなかったターンには付けない
        hist = client.get("/api/talk/history").json()
    assert model.script == []

    first, second = seen
    humans = [m.content for m in first if m.type == "human"]
    assert humans[-1] == SENT  # LLM に送ったのは伏せた文
    assert all(_no_personal(m.content) for m in second if m.type == "human")
    assert uid not in first[0].content  # system prompt にユーザーIDは無い
    # 保存した会話(再読み込みで描き直す履歴)も伏せた文。画面が知らせを出せるよう redacted が付く
    said = next(t for t in hist["turns"] if t["user"] == SENT)
    assert said["redacted"] is True and len(said["bot"]["cards"]) == 3
    assert all("redacted" not in t for t in hist["turns"] if t is not said)


def test_handle_called_directly_also_redacts(tmp_path):
    """評価などが LineRuntime.handle を直に呼んでも(turn.accept を通らない)、伏せた文だけが LLM と会話に入る。"""
    seen: list = []

    class Seeing(ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(list(messages))
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = Seeing(script=[reply(["承知しました。"])])
    (r,) = run(model, [("sim-new", "message", SAID)], tmp_path)
    assert r["bubbles"] == ["承知しました。"]
    assert [m.content for m in seen[0] if m.type == "human"] == [SENT]


# ----------------------------- system prompt にユーザーIDを載せない -----------------------------


def test_system_prompt_has_no_user_id_and_tools_still_reach_this_user(tmp_path):
    """LLM は line_user_id に PINNED_USER を渡すだけ。保存も検索も、この会話のユーザーの分に入る。"""
    uid = "w-0123456789abcdef"
    seen: list = []

    class Seeing(ScriptedModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(messages[0].content)
            return super()._generate(messages, stop, run_manager, **kwargs)

    model = Seeing(script=[
        call("save_user_preferences", {"line_user_id": PINNED_USER, "preferences": {"area": "渋谷"}}),
        call("recommend_jobs", {"line_user_id": PINNED_USER, "area": "渋谷", "time_band": "日中"}, "t2"),
        reply(["渋谷・日中で探しました。"]),
    ])
    (r,) = run(model, [(uid, "message", "渋谷で日中")], tmp_path)
    assert all(uid not in s and f"「{PINNED_USER}」" in s for s in seen)
    assert len(set(seen)) == 1  # 毎回同じ文(プロンプトキャッシュを壊さない)
    assert [t["name"] for t in r["tools"]] == ["save_user_preferences", "recommend_jobs"]
    state = State(tmp_path / "state.json")
    assert logic.conversation_summary(state, uid)["stated_preferences"]["area"]["value"] == "渋谷"
    users = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["users"]
    assert PINNED_USER not in users  # 「me」というユーザーは作られない


@pytest.mark.parametrize("uid", ["w-0123456789abcdef", "w-0123456789abcdef:sim-applied"])
def test_dip_tools_do_not_return_the_user_id(tmp_path, monkeypatch, uid):
    """どのツールの結果にも、呼んだユーザーのIDが入らない(結果は LLM に届くので)。
    開発者画面のシナリオユーザー(匿名ID:シナリオ)は、画面の URL にシナリオの名前だけが載り、匿名IDは載らない。"""
    state = State(tmp_path / "state.json")
    state.reset()
    if ":" in uid:
        state.reset_user(uid, template=uid.partition(":")[2])
    monkeypatch.setattr(dip_server, "state", state)
    anon = uid.partition(":")[0]
    tools = {t.name for t in asyncio.run(dip_server.mcp.list_tools())}
    assert tools == set(TOOL_ARGS)  # ツールを足したら、ここでも確かめる
    for name, args in TOOL_ARGS.items():
        if name not in ("answer_job_question", "get_counseling_playbook"):
            args = {"line_user_id": uid, **args}
        out = asyncio.run(dip_server.mcp.call_tool(name, args))
        assert anon not in json.dumps(out, ensure_ascii=False, default=str), name


# ----------------------------- LINE のトーク -----------------------------


def test_line_talk_redacts_before_the_llm_and_nothing_personal_is_written(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from line_runtime import line_channel
    from line_runtime.line_channel import line_user
    from tests.test_line_webhook import ID_KEY, RAW_USER, _event, _post, _server

    monkeypatch.setenv(line_channel.ID_KEY_ENV, ID_KEY)  # test_line_webhook の line_id_key と同じ
    monkeypatch.delenv(line_channel.LIFF_ID_ENV, raising=False)
    monkeypatch.delenv(line_channel.LOGIN_CHANNEL_ENV, raising=False)

    uid = line_user(RAW_USER)
    seen: list = []

    def search(messages):
        seen.append(list(messages))
        return call("recommend_jobs", {"line_user_id": PINNED_USER, "area": "渋谷", "time_band": "日中"}, "rec")

    model = Greeter(script=[search, propose])
    server, fake = _server(tmp_path, monkeypatch, model)
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text=SAID)]).status_code == 200
    assert model.script == []
    (msgs,) = seen
    assert [m.content for m in msgs if m.type == "human"] == [SENT]
    assert uid not in msgs[0].content
    ((_, body),) = fake.sent()
    assert any(m["type"] == "flex" for m in body["messages"])  # 求人のカードは届く
    for p in (p for p in tmp_path.rglob("*") if p.is_file()):
        raw = p.read_bytes()
        assert PHONE.encode() not in raw and MAIL.encode() not in raw and "1丁目".encode() not in raw, p.name


# ----------------------------- 画面: 伏せたことを本人に知らせる -----------------------------


@pytest.fixture
def served(app):  # noqa: F811
    """本物の HTTP サーバー(uvicorn)で画面を出す。モデルは起動の前に差し込む(ランタイムは起動のときに作る)。"""
    import threading
    import time

    import uvicorn

    app.set_model(Greeter(script=[greet, reply(["承知しました。"]), reply(["はい。"])]))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port, log_level="warning"))
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


def test_talk_screen_tells_the_user_under_their_bubble_and_after_reload(served, browser):  # noqa: F811
    """本番のトーク画面で個人情報らしい所を含めて送ると、本人の吹き出しのすぐ下に知らせが出る。再読み込みで描き直した履歴は、
    伏せた後の文と同じ知らせ。伏せない発言には出さない。"""
    page = browser.new_context().new_page()
    page.set_default_timeout(10_000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(served + "/")
    page.locator("#log .row.bot .bubble", has_text="バイト探しをお手伝いします").wait_for()
    page.locator("#t").fill(SAID)
    page.locator("#send").click()
    note = page.locator("#log .redacted")
    note.wait_for()
    assert note.inner_text() == "電話番号などは送らないようにしました"
    # 本人の吹き出し(送った直後は本人が書いたまま。このブラウザの中だけ)のすぐ下、返信より前
    assert note.evaluate("n => n.previousElementSibling.matches('.row.me') && n.nextElementSibling.matches('.row.bot')")
    assert page.locator("#log .row.me .bubble").last.inner_text() == SAID
    page.locator("#t").fill("渋谷で日中")
    page.locator("#send").click()
    page.locator("#log .row.bot .bubble", has_text="はい。").wait_for()
    assert note.count() == 1  # 伏せない発言には出さない

    page.reload()
    page.locator("#log .row.bot .bubble", has_text="はい。").wait_for()
    assert note.count() == 1
    assert page.locator("#log .row.me .bubble").nth(0).inner_text() == SENT
    assert note.evaluate("n => n.previousElementSibling.matches('.row.me')")
    assert errors == []
