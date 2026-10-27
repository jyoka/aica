"""正社員のヒアリング(dip_mcp/career、task 255)を、トークのターンとして通しで動かす。実 LLM なし、dip の MCP は本物。

機能フラグ CAREER_HARNESS_VERSION=c1 のときの、本人から見た流れ: 正社員の相談に入る → トークの AI が playbook を読んで
相談を始め、最初の吹き出しと選択肢はハーネスのもの → その後の発言はランタイムが career_turn に渡す(トークの AI は呼ばない)
→ result に着くと、条件は stated_preferences に、職歴カードは career_profile に保存されて相談が閉じる。
「あとで」はそこまでを保存して閉じ、次の相談では保存した項目を聞かない。「すべて消す」で職歴カードも消える。
フラグが無ければ、今までの正社員のヒアリング(playbook の fulltime_chat.md)のまま。
モデル(ハーネスの読み取りと言い方)は偽物: 読み取りは今の質問の欄に本人の発言をそのまま入れ、言い方は質問の原文を返す。
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic, profile
from dip_mcp.career import harness as H
from dip_mcp.career import session, store
from dip_mcp.data import State
from evals.checks import conversation_checks
from line_runtime.career_chat import SCHEMAS, ExtractOut
from line_runtime.line_channel import line_user
from tests.test_consult import ConsultModel
from tests.test_handle import call, reply
from tests.test_line_data import _ask, _say, _tap
from tests.test_line_webhook import RAW_USER, _server, line_id_key  # noqa: F401  line_id_key はフィクスチャ
from tests.test_talk_page import app  # noqa: F401  フィクスチャ(Web のサーバー)

UID = "w-00000000000000c1"  # 架空の匿名ユーザーID(fixtures のユーザーでない)
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read": 0, "cache_write": 0}
# 文で答える問いの、本人の答え(架空)。ほかの問いはボタンの先頭を押す
TEXT = {"ib_why_now": "評価の仕組みが合わないから", "hf_area": "大阪市内", "card_role1": "物流会社で現場リーダー、6年",
        "card_result1": "誤出荷が半分になった", "card_role2": "倉庫の作業スタッフ", "card_result2": "手順書を作った",
        "fg_direction": "現場の改善"}
ESCAPES = {H.PASS, H.DONT_KNOW, H.LATER}


@pytest.fixture(autouse=True)
def harness_on(monkeypatch):
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    monkeypatch.delenv(H.FORM_ENV, raising=False)  # 形 C(既定)


class FakeCareerLLM:
    """読み取りは、今の質問の欄(システムプロンプトの「この質問が埋めたい欄」)に本人の発言をそのまま入れる。
    「また今度」「あとで」なら intent=later(前に答えがあれば、それも今の質問の答えにする)。言い方は質問の原文。"""

    def __init__(self):
        self.calls: list[tuple[str, str, list[dict]]] = []

    async def __call__(self, stage, system, messages):
        self.calls.append((stage, system, [dict(m) for m in messages]))
        if stage == "say":
            q = json.loads(re.search(r"次に聞くこと\(JSON\): (\{.*\})", system).group(1))
            return {"reply": q["text"]}, USAGE
        targets = json.loads(re.search(r"この質問が埋めたい欄: (\[.*\])", system).group(1))
        said = re.search(r"本人の発言: (.*)", system).group(1).strip()
        answer, later = said, False
        if "また今度" in said:
            answer, later = said.split("。")[0], True
        updates = [{"slot": targets[0], "text": answer, "number": None, "unit": None, "choice": None}] if answer else []
        return {"slot_updates": updates, "ng_updates": [], "segment": None, "intent": "later" if later else "answer",
                "review_ack": None}, USAGE


def start_script(uid: str) -> list:
    """トークの AI: 正社員に切り替え、playbook(ハーネス版)を読み、相談を始める。返信の文はハーネスの吹き出しに置き換わる。"""
    return [call("set_search_track", {"line_user_id": uid, "track": "正社員", "evidence": "正社員で転職したい"}, "t0"),
            call("get_counseling_playbook", {"track": "正社員", "channel": "chat"}, "pb"),
            call("start_career_consultation", {"line_user_id": uid}, "st"),
            reply(["(この文はハーネスの吹き出しに置き換わる)"], quick_replies=["モデルの選択肢"])]


def current_qid(st: State, uid: str) -> str | None:
    d = st.user(uid).get(session.KEY)
    return d["harness"]["current"]["qid"] if d else None


def answer_for(qid: str, quick: list[str]) -> str:
    """本人役: まとめは「これでOK」、文の問いは TEXT、ほかはボタンの先頭(パスなどのほかに無ければパス)。"""
    if qid == "review":
        return H.REVIEW_OK
    if qid in TEXT:
        return TEXT[qid]
    return next((c for c in quick if c not in ESCAPES), H.PASS)


# ----------------------------- LINE の Webhook を通して result まで -----------------------------


def test_line_webhook_reaches_result_and_saves_conditions_and_career_cards_then_forget_erases_them(tmp_path, monkeypatch):
    """偽の Webhook で正社員の相談を最後まで進めると、result に着き、条件は stated_preferences に、職歴カードと segment は
    career_profile に保存される。プロフィールと「データを見る」に職歴カードが出て、「データを消す」→「すべて消す」で消える。"""
    from functools import partial

    from line_runtime.runtime import LineRuntime

    uid = line_user(RAW_USER)
    llm = FakeCareerLLM()
    model = ConsultModel(script=start_script(uid))
    server, fake = _server(tmp_path, monkeypatch, model)
    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=llm))
    st = server.state
    sent = []
    with TestClient(server.app) as client:
        assert _say(client, "正社員で転職したいので相談したい", 1).status_code == 200
        (first,) = [m for m in fake.sent()[-1][1]["messages"] if m["type"] == "text"]
        # 最初の吹き出しと選択肢はハーネスのもの(モデルの文と選択肢は使わない)
        q_segment = H.qmap(H.new_state(), H.BANK)["q_segment"]
        assert first["text"] == H.START_LEAD + q_segment["text"]
        assert [i["action"]["text"] for i in first["quickReply"]["items"]] == H.choices_of(q_segment)
        assert current_qid(st, uid) == "q_segment"
        for n in range(2, 40):
            qid = current_qid(st, uid)
            if qid is None:
                break
            last = fake.sent()[-1][1]["messages"][-1]
            quick = [i["action"]["text"] for i in last.get("quickReply", {}).get("items", [])]
            said = answer_for(qid, quick)
            sent.append((qid, said))
            assert _say(client, said, n).status_code == 200
        else:
            raise AssertionError("result に着かない")
        texts = [m["text"] for m in fake.sent()[-1][1]["messages"] if m["type"] == "text"]
        assert texts[-1].startswith(H.RESULT_TEXT)
        assert [i["action"]["text"] for i in fake.sent()[-1][1]["messages"][-1]["quickReply"]["items"]] == session.RESULT_CHOICES
        assert model.script == []  # 相談の間、トークの AI は呼ばない

        # 条件は stated_preferences に(本人の言葉・ボタンのラベルのまま)
        prefs = logic.stated_preferences(st.user(uid))
        picks = dict(sent)
        assert prefs["why_now"]["value"] == TEXT["ib_why_now"] and prefs["area"]["value"] == TEXT["hf_area"]
        assert prefs["min_annual_salary"]["value"] == picks["hf_salary"]
        assert prefs["start_timing"]["value"] == picks["hf_timing"]
        # 職歴カードと segment は career_profile に
        rec = st.user(uid)[store.KEY]
        assert rec["segment"]["value"] == "same_path" and rec["items"]["raise_target"]["text"] == picks["hf_raise"]
        assert rec["items"]["career1.role"]["text"] == TEXT["card_role1"]
        # 区切りのボタンは、選んだラベル(「くらい」つき)と区切り・単位を持つ(数は作らない。設計メモ §4)
        scale = rec["items"]["career1.scale"]
        assert (scale["text"], scale["range"], scale["unit"], scale["number"]) == (picks["card_scale1"], [1, 7], "人", None)
        assert rec["items"]["career1.result"]["text"] == TEXT["card_result1"]
        assert session.KEY not in st.user(uid)  # 相談は閉じた

        # 見る: プロフィール画面と LINE の「データを見る」に職歴カードが出る
        view = profile.view(st, uid)["career_profile"]
        assert view["segment"]["value"] == "同じ仕事で上へ"
        assert view["cards"][0]["items"][0]["value"] == TEXT["card_role1"]
        _say(client, "データを見る", 50)
        shown = "\n".join(m["text"] for m in fake.sent()[-1][1]["messages"])
        assert "■ 職歴カード" in shown and TEXT["card_role1"] in shown and "なぜ今: " + TEXT["ib_why_now"] in shown

        # 全部消す: 職歴カードも、条件も消える
        data = _ask(client, fake, 51)
        _tap(client, data, 52)
    user = State(tmp_path / "state.json").user(uid)
    assert store.KEY not in user and not user.get("preferences") and session.KEY not in user


# ----------------------------- LINE のトークで言い直す(ポリシー 1.4 §7 の LINE の「直す」) -----------------------------


def test_line_talk_correction_fixes_career_conditions_and_card_fields(tmp_path, monkeypatch):
    """「データを見る」の後に本人がトークで言い直すと、トークの AI(偽のモデル)が save_user_preferences で why_now・direction・
    team_style を、update_career_card で職歴カードの欄を、本人の言葉のまま直す。もう一度「データを見る」と、直した値が出る。
    区切りのボタンで取れていた規模を言い直すと、文だけを持ち、区切りと単位は残さない(数を作らない)。"""
    from tests.test_consult import calls

    uid = line_user(RAW_USER)
    fixed = {"why_now": "年収を上げたいから", "direction": "物流の業務改善", "team_style": "一人で任される"}
    model = ConsultModel(script=[
        calls(("save_user_preferences", {"line_user_id": uid, "preferences": fixed}),
              ("update_career_card", {"line_user_id": uid, "item": "career1.result", "text": "誤出荷がゼロになった"}),
              ("update_career_card", {"line_user_id": uid, "item": "career1.scale", "text": "30人くらい"}),
              ("update_career_card", {"line_user_id": uid, "item": "career1.place", "text": "大阪"})),
        reply(["直しました。なぜ今は「年収を上げたいから」です。"], quick_replies=["求人を見る", "今はいい"])])
    server, fake = _server(tmp_path, monkeypatch, model)
    st = server.state
    with TestClient(server.app) as client:
        logic.set_search_track(st, uid, "正社員", "正社員で転職したい")
        logic.save_preferences(st, uid, {"why_now": "評価の仕組みが合わないから", "direction": "現場の改善",
                                         "team_style": "チームで分担"})
        store.save(st, uid, {"segment": "same_path", "slots": {
            "career1.role": {"status": "filled", "text": "物流会社で現場リーダー、6年", "confidence": "high"},
            "career1.scale": {"status": "filled", "text": "5人くらい", "range": [1, 7], "unit": "人", "confidence": "high"},
            "career1.result": {"status": "filled", "text": "誤出荷が半分になった", "confidence": "high"}},
            "sources": ["物流会社で現場リーダー、6年", "5人くらい"]})  # 職歴カードの数字は本人の発言と比べて保存する
        _say(client, "データを見る", 2)
        assert len(model.script) == 2  # 見るのはモデルを呼ばない
        _say(client, "なぜ今は年収を上げたいから、次は物流の業務改善、一人で任されたい。職歴1は30人くらいで、誤出荷がゼロになった", 3)
        assert model.script == []
        assert "update_career_card" in model.systems[-1]  # 直し方は Prompt にある
        user = st.user(uid)
        assert {k: user["preferences"][k]["value"] for k in fixed} == fixed
        items = user[store.KEY]["items"]
        assert items["career1.result"]["text"] == "誤出荷がゼロになった"
        scale = items["career1.scale"]
        assert (scale["text"], scale["range"], scale["unit"], scale["number"]) == ("30人くらい", None, None, None)
        assert items["career1.role"]["text"] == "物流会社で現場リーダー、6年"  # 言い直していない欄はそのまま
        assert "career1.place" not in items  # 知らない欄は直さない
        assert not user.get("profile_changes")  # トークの AI が直したので、画面で直したときの知らせは残さない
        _say(client, "データを見る", 4)
        shown = "\n".join(m["text"] for m in fake.sent()[-1][1]["messages"])
        for v in [*fixed.values(), "誤出荷がゼロになった", "30人くらい"]:
            assert v in shown, v
        assert "評価の仕組みが合わないから" not in shown and "誤出荷が半分になった" not in shown


def test_update_career_card_rejects_unknown_personal_long_and_empty_values(tmp_path):
    st = State(tmp_path / "state.json")
    _saved_profile(st)
    assert store.update(st, UID, "career1.role", " 倉庫の作業スタッフ ") == {"saved": "career1.role", "value": "倉庫の作業スタッフ"}
    assert store.update(st, UID, "career1.scale", "30人くらい") == {"saved": "career1.scale", "value": "30人くらい"}  # 空欄も直せる
    assert store.update(st, UID, "raise_target", "役職") == {"saved": "raise_target", "value": "役職"}
    for key, text, err in [("segment", "仕事を変えたい", "unknown_key"), ("area", "大阪", "unknown_key"),
                           ("career2.role", "倉庫の作業スタッフ", "not_saved"), ("career1.result", "売上が倍", "not_saved"),
                           ("career1.role", "090-1234-5678", "personal_info"),
                           ("career1.role", "あ" * (store.VALUE_MAX + 1), "too_long"), ("career1.role", " ", "empty")]:
        r = store.update(st, UID, key, text)
        assert r["error"] == err and r["note"], (key, err)
    items = st.user(UID)[store.KEY]["items"]
    assert items["career1.role"]["text"] == "倉庫の作業スタッフ" and items["raise_target"]["text"] == "役職"
    assert "career2.role" not in items and "career1.result" not in items  # 保存していない欄は作らない


def test_update_career_card_does_not_create_a_profile_for_someone_with_nothing_saved(tmp_path):
    """フラグが無い本番の既定でも update_career_card はトークの AI に見える。何も保存していない人に呼んでも、
    career_profile を作らない(ヒアリングを通らずに職歴を足す道にしない)。要約にも職歴カードの説明が足されない。"""
    st = State(tmp_path / "state.json")
    logic.save_preferences(st, UID, {"area": "大阪"})
    r = store.update(st, UID, "career2.result", "売上が倍")
    assert r["error"] == "not_saved" and r["note"]
    assert store.KEY not in st.user(UID)
    assert "career_profile" not in logic.conversation_summary(st, UID)


def test_career_cards_in_the_summary_carry_their_number_after_card_1_is_deleted(tmp_path):
    """職歴1の欄をプロフィール画面で全部消して職歴2だけが残っても、要約のカードは n=2 を持つ。トークの AI が
    その n で update_career_card を呼ぶと、職歴2の欄が直り、職歴1の欄は作られない(職歴が 2 つに割れない)。"""
    st = State(tmp_path / "state.json")
    store.save(st, UID, {"segment": "same_path", "slots": {
        "career1.role": {"status": "filled", "text": "物流会社で現場リーダー、6年", "confidence": "high"},
        "career2.role": {"status": "filled", "text": "倉庫スタッフ", "confidence": "high"},
        "career2.result": {"status": "filled", "text": "手順書を作った", "confidence": "high"}},
        "sources": ["物流会社で現場リーダー、6年"]})
    assert profile.delete_career_item(st, UID, "career1.role") == {"deleted": "career1.role"}
    cards = logic.conversation_summary(st, UID)["career_profile"]["career_cards"]
    assert cards == [{"n": 2, "role": "倉庫スタッフ", "result": "手順書を作った"}]
    assert store.update(st, UID, f"career{cards[0]['n']}.result", "手順書を全拠点に広げた")["saved"] == "career2.result"
    assert store.update(st, UID, "career1.result", "手順書を全拠点に広げた")["error"] == "not_saved"
    items = st.user(UID)[store.KEY]["items"]
    assert set(items) == {"career2.role", "career2.result"}
    assert items["career2.result"]["text"] == "手順書を全拠点に広げた"


# ----------------------------- 機能フラグ -----------------------------


def _rt_run(agent_script, llm, steps, tmp_path):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=ConsultModel(script=agent_script), in_memory=True, career_llm=llm) as rt:
            out = [await rt.handle(*s) for s in steps]
            return out, {t.name for t in rt.tools}

    return asyncio.run(go())


def test_without_the_flag_the_fulltime_hearing_is_the_playbook_as_before(tmp_path, monkeypatch):
    """フラグが無いとき(本番の既定): トークの AI に start_career_consultation を見せず、正社員の playbook は今までの
    fulltime_chat.md(4問まで)。dip のツールを直に呼んでも始まらない。相談はトークの AI が受け持つ(career の印が付かない)。"""
    monkeypatch.delenv(logic.CAREER_HARNESS_FLAG)
    agent = [call("set_search_track", {"line_user_id": UID, "track": "正社員", "evidence": "正社員で転職したい"}, "t0"),
             call("get_counseling_playbook", {"track": "正社員", "channel": "chat"}, "pb"),
             reply(["転職の相談ですね。", "今回、転職を考え始めたきっかけは何ですか?"], quick_replies=["今はいい"]),
             reply(["そうだったんですね。", "希望の勤務地はどのあたりですか?"], quick_replies=["今はいい"])]
    (r1, r2), tools = _rt_run(agent, FakeCareerLLM(), [(UID, "message", "正社員で転職したい"),
                                                       (UID, "message", "評価の仕組みが合わないから")], tmp_path)
    assert "start_career_consultation" not in tools and not {"career_turn", "end_career_consultation"} & tools
    playbook = r1["tools"][1]["result"]["playbook"]
    assert "相談の問いは4問まで" in playbook and "start_career_consultation" not in playbook
    assert r1["bubbles"] == ["転職の相談ですね。", "今回、転職を考え始めたきっかけは何ですか?"] and "career" not in r1
    assert r2["bubbles"][1] == "希望の勤務地はどのあたりですか?" and "career" not in r2
    st = State(tmp_path / "state.json")
    assert session.start(st, UID)["started"] is False and not session.active(st, UID)
    # フラグがあれば、同じ区分の playbook はハーネス版になり、ツールも見える
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    assert "start_career_consultation" in logic.counseling_playbook("正社員")["playbook"]
    assert "start_career_consultation" not in logic.counseling_playbook("正社員", "voice")["playbook"]
    assert "start_career_consultation" not in logic.counseling_playbook("アルバイト・パート")["playbook"]


def test_agent_sees_the_start_tool_only_with_the_flag(tmp_path):
    _, tools = _rt_run([], None, [], tmp_path)
    assert "start_career_consultation" in tools and not {"career_turn", "end_career_consultation"} & tools


# ----------------------------- 「あとで」 -----------------------------


def test_later_saves_what_was_answered_closes_and_the_next_consultation_skips_it(tmp_path):
    """「あとで」を押すと、そこまでの分(segment、なぜ今、勤務地)を保存して相談を閉じる。次の発言はトークの AI が受ける。
    次に相談を始めると、保存した項目は聞かず、残りの最初の問い(年収)から始める(前の相談の状態は持ち越さない)。"""
    llm = FakeCareerLLM()
    agent = [*start_script(UID), reply(["わかりました。"], quick_replies=["今はいい"]), *start_script(UID)[2:]]
    steps = [(UID, "message", "正社員で転職したい"), (UID, "message", "同じ仕事で上へ"),
             (UID, "message", TEXT["ib_why_now"]), (UID, "message", TEXT["hf_area"]),
             (UID, "message", H.LATER), (UID, "message", "ありがとう"), (UID, "message", "また相談したい")]
    rs, _ = _rt_run(agent, llm, steps, tmp_path)
    st = State(tmp_path / "state.json")
    assert [r.get("career", {}).get("question") for r in rs[1:4]] == ["ib_why_now", "hf_area", "hf_salary"]
    assert rs[4]["bubbles"] == [H.LATER_TEXT] and rs[4]["career"]["question"] == "closed" and rs[4]["quick_replies"] == session.RESULT_CHOICES
    assert "career" not in rs[5] and rs[5]["bubbles"] == ["わかりました。"]  # 閉じた後はトークの AI
    # 「あとで」で閉じた返信も選択肢で終える(会話の判定の「最後の返信に選択肢がない」に当たらない)
    said = [m for _, _, m in steps[:5]]
    assert not any("選択肢がない" in v for v in conversation_checks({"expect_outcome": ["consult"]}, rs[:5], said)["violations"])
    user = st.user(UID)
    assert user["preferences"]["why_now"]["value"] == TEXT["ib_why_now"]
    assert user["preferences"]["area"]["value"] == TEXT["hf_area"]
    assert user[store.KEY]["segment"]["value"] == "same_path"
    # 次の相談: 保存した segment・なぜ今・勤務地を聞かず、年収から
    started = rs[6]["tools"][-1]["result"]
    assert started["started"] is True and started["question"]["id"] == "hf_salary"
    assert {"segment", "why_now", "area"} <= set(started["prefilled"])
    assert rs[6]["bubbles"] == started["question"]["bubbles"] and rs[6]["quick_replies"] == started["question"]["choices"]
    h = H.State.from_dict(st.user(UID)[session.KEY]["harness"])
    assert h.turns == 0 and {"q_segment", "ib_why_now", "hf_area"}.isdisjoint(h.asked)
    assert h.slots["why_now"]["from"] == "saved" and h.slots["why_now"]["confidence"] == "high" and not h.has_resume


def test_later_typed_with_an_answer_keeps_the_answer(tmp_path):
    """「大阪市内です。続きはまた今度」のように答えと一緒に打っても、答えを残してから閉じる(聞き直さない)。
    今の問いへの文の答えは、本人の文をそのまま入れる(ハーネスの決まり。設計メモ §3.2)。"""
    llm = FakeCareerLLM()
    st = State(tmp_path / "state.json")
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    logic.set_search_track(st, UID, "正社員", "正社員で転職したい")
    session.start(st, UID)
    for said in ("同じ仕事で上へ", TEXT["ib_why_now"]):
        _drive(st, UID, said, llm)
    assert current_qid(st, UID) == "hf_area"
    r = _drive(st, UID, "大阪市内です。続きはまた今度", llm)
    assert r["status"] == "result" and r["closed"] == "later" and "area" in r["saved"]
    assert r["choices"] == session.RESULT_CHOICES  # 閉じた返信も選択肢で終える
    assert st.user(UID)["preferences"]["area"]["value"] == "大阪市内です。続きはまた今度"
    assert not session.active(st, UID)


def _drive(st: State, uid: str, said: str, llm: FakeCareerLLM) -> dict:
    """dip の career_turn を、ランタイムと同じく status=llm のあいだ呼び直して 1 ターン進める。"""
    outputs: list[dict] = []
    r = session.turn(st, uid, said)
    while r.get("status") == "llm":
        out, _ = asyncio.run(llm(r["call"]["stage"], r["call"]["system"], []))
        outputs.append({"stage": r["call"]["stage"], "output": out})
        r = session.turn(st, uid, said, outputs)
    return r


# ----------------------------- 保存の決まり -----------------------------


def test_only_confirmed_values_are_saved_and_blank_cards_stay_blank(tmp_path):
    """保存するのは確かめた値(high)だけ。推測のまま(low)の値は保存しない。職歴カードのパスと「覚えていない」は
    空欄として残し、次の相談では聞かない(下書きの空欄で後から埋める)。条件のパスは「こだわりなし」。"""
    st = State(tmp_path / "state.json")
    h = H.new_state()
    h.segment = "change"
    h.slots = {
        "why_now": H.entry("passed", H.PASS),
        "area": H.entry("passed", H.PASS, H.NOPREF),
        "start_timing": H.entry("filled", "すぐ", "すぐ"),
        "direction": H.entry("filled", "新しいことを企画する", "plan", confidence="low"),
        "change_reason": H.entry("filled", "裁量がほしい"),
        "career1.role": H.entry("filled", "経理を5年"),
        "career1.scale": H.entry("dont_know", H.DONT_KNOW),
        "career1.result": H.entry("passed", H.PASS),
        "career2.role": H.entry("filled", "販売", confidence="low"),
    }
    saved = session._finish(st, UID, h)
    user = st.user(UID)
    assert {k: v["value"] for k, v in user["preferences"].items()} == {"area": H.NOPREF, "start_timing": "すぐ"}
    items = user[store.KEY]["items"]
    assert set(items) == {"change_reason", "career1.role", "career1.scale", "career1.result"}
    assert items["career1.scale"]["status"] == "dont_know" and items["career1.result"]["status"] == "passed"
    assert set(saved) == {"area", "start_timing", "segment", *items}
    # 次の相談: 保存した欄は high で入り、聞かない。空欄も空欄のまま(blank_cards に残る)
    segment, prefill = store.prefill(user, logic.stated_preferences(user))
    nh = H.new_state()
    H.apply_saved(nh, segment, prefill)
    assert nh.segment == "change" and nh.slots["area"]["status"] == "passed" and nh.slots["area"]["value"] == H.NOPREF
    assert "career1.scale" in H.blank_cards(nh) and "career1.result" in H.blank_cards(nh)
    H.start(nh)
    assert nh.current["qid"] == "ib_why_now"  # why_now のパスは保存しない(次は聞く)。segment は聞かない


def test_personal_or_too_long_card_values_are_not_saved(tmp_path):
    st = State(tmp_path / "state.json")
    s = {"segment": "early", "slots": {
        "career1.role": {"status": "filled", "text": "連絡は090-1234-5678まで", "confidence": "high"},
        "career1.result": {"status": "filled", "text": "あ" * (store.VALUE_MAX + 1), "confidence": "high"},
        "keep_doing": {"status": "filled", "text": "人と話すこと", "confidence": "high"}}}
    assert store.save(st, UID, s) == ["segment", "keep_doing"]


def test_a_new_conversation_closes_the_consultation_and_keeps_what_was_answered(tmp_path):
    """新しい会話(挨拶)で途中の相談は閉じ、そこまでの分を保存する。"""
    agent = [*start_script(UID), reply(["こんにちは。"], quick_replies=["今はいい"])]
    rs, _ = _rt_run(agent, FakeCareerLLM(), [(UID, "message", "正社員で転職したい"), (UID, "message", "同じ仕事で上へ"),
                                            (UID, "visit", None)], tmp_path)
    st = State(tmp_path / "state.json")
    assert rs[2]["bubbles"] == ["こんにちは。"] and not session.active(st, UID)
    assert st.user(UID)[store.KEY]["segment"]["value"] == "same_path"


def test_switching_away_from_fulltime_closes_the_consultation(tmp_path):
    st = State(tmp_path / "state.json")
    logic.set_search_track(st, UID, "正社員", "正社員で転職したい")
    session.start(st, UID)
    _drive(st, UID, "同じ仕事で上へ", FakeCareerLLM())
    logic.set_search_track(st, UID, "アルバイト・パート", "やっぱりバイトで")
    r = session.turn(st, UID, "評価の仕組みが合わないから")
    assert r == {"active": False, "ended": "track", "saved": ["segment"]} and not session.active(st, UID)


def test_profile_changes_during_a_consultation_are_not_undone_when_it_closes(tmp_path):
    """前の相談で保存した値は、次の相談に high で入るが、閉じるときに書き直さない。相談の途中にプロフィール画面で
    消した・直した値(勤務地、なぜ今、segment)は、閉じた後もそのまま(前の値に戻らない)。この相談で答えた値は保存する。"""
    llm = FakeCareerLLM()
    st = State(tmp_path / "state.json")
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    logic.set_search_track(st, UID, "正社員", "正社員で転職したい")
    session.start(st, UID)
    for said in ("同じ仕事で上へ", TEXT["ib_why_now"], TEXT["hf_area"], H.LATER):
        _drive(st, UID, said, llm)
    session.start(st, UID)  # 2 回目: 保存した 3 つは聞かず、年収から
    assert current_qid(st, UID) == "hf_salary"
    profile.delete_item(st, UID, "area")
    profile.edit_item(st, UID, "why_now", "給与を上げたい")
    profile.delete_career_item(st, UID, "segment")
    _drive(st, UID, "400万円台", llm)
    r = session.turn(st, UID, "今はいい")
    assert r["ended"] == "exit" and set(r["saved"]) == {"min_annual_salary"}
    user = st.user(UID)
    prefs = {k: v["value"] for k, v in user["preferences"].items()}
    assert prefs == {"why_now": "給与を上げたい", "min_annual_salary": "400万円台"}
    assert "segment" not in user.get(store.KEY, {})


def test_reverse_pick_ng_is_saved_to_avoids_and_not_asked_again(tmp_path):
    """逆選択で選んだ NG は、避けたいこと(avoids)に、前の値に足して保存する。次の相談では逆選択を聞かない。"""
    st = State(tmp_path / "state.json")
    logic.save_preferences(st, UID, {"avoids": "接客"})
    h = H.new_state()
    h.user_ng = ["電話をかけ続ける仕事"]
    assert "avoids" in session._finish(st, UID, h)
    assert st.user(UID)["preferences"]["avoids"]["value"] == "電話をかけ続ける仕事、接客"
    nh = H.new_state()
    prefs = logic.stated_preferences(st.user(UID))
    H.apply_saved(nh, *store.prefill(st.user(UID), prefs), ng_known=store.ng_known(prefs))
    assert "rp_general" in nh.asked and "rp_general" not in H.remaining(nh, H.BANK, budget=False)


@pytest.mark.parametrize("said, exits", [
    ("やっぱりバイトで探したい", True), ("アルバイトの求人を見たい", True), ("パートがいいです", True),
    ("バイトで探すのは、あとで", False), ("バイトの経験はあります", False), ("物流会社で現場リーダー、6年", False)])
def test_asking_for_part_time_leaves_the_consultation_to_the_agent(tmp_path, said, exits):
    """review の前に「バイトで探したい」と言ったら、そこまでを保存して閉じ、トークの AI に渡す(区分の切り替えは AI がする)。
    アルバイト・パートの相談の「正社員で探したい」と同じ形。後回し・打ち消しと、経歴の話は相談を続ける。"""
    st = State(tmp_path / "state.json")
    logic.set_search_track(st, UID, "正社員", "正社員で転職したい")
    session.start(st, UID)
    _drive(st, UID, "同じ仕事で上へ", FakeCareerLLM())
    r = session.turn(st, UID, said)
    assert (r.get("ended") == "exit") is exits and session.active(st, UID) is not exits


# ----------------------------- プロフィール(見る・直す・消す・書き出す) -----------------------------


def _saved_profile(st: State) -> None:
    store.save(st, UID, {"segment": "same_path", "slots": {
        "raise_target": {"status": "filled", "text": "年収", "confidence": "high"},
        "career1.role": {"status": "filled", "text": "物流会社で現場リーダー、6年", "confidence": "high"},
        "career1.scale": {"status": "dont_know", "text": H.DONT_KNOW, "confidence": "high"}},
        "sources": ["物流会社で現場リーダー、6年"]})


def test_profile_shows_edits_and_deletes_career_cards(tmp_path):
    st = State(tmp_path / "state.json")
    _saved_profile(st)
    v = profile.view(st, UID)["career_profile"]
    assert v["segment"]["value"] == "同じ仕事で上へ" and [a["value"] for a in v["answers"]] == ["年収"]
    (card,) = v["cards"]
    assert card["title"] == "職歴1(直近)"
    assert [(i["label"], i["value"]) for i in card["items"]] == [
        ("何をしていたか", "物流会社で現場リーダー、6年"), ("どのくらい", H.DONT_KNOW), ("どうなったか", None)]
    assert profile.edit_career_item(st, UID, "career1.scale", "30人くらい") == {"saved": "career1.scale", "value": "30人くらい"}
    assert profile.edit_career_item(st, UID, "career1.scale", "090-1234-5678")["error"] == "personal_info"
    assert profile.edit_career_item(st, UID, "area", "大阪")["error"] == "unknown_key"
    assert st.user(UID)[store.KEY]["items"]["career1.scale"]["text"] == "30人くらい"
    # 書き出す: 保存しているそのままの形に入る
    assert profile.stored(st, UID)["users"][UID][store.KEY]["items"]["career1.role"]["text"] == "物流会社で現場リーダー、6年"
    # トークの AI への知らせと要約にも出る
    assert [c["what"] for c in profile.pending_changes(st, UID)] == ["職歴1(直近)のどのくらい"]
    assert logic.conversation_summary(st, UID)["career_profile"]["career_cards"] == [
        {"n": 1, "role": "物流会社で現場リーダー、6年", "scale": "30人くらい"}]
    for key in ("career1.role", "career1.scale", "raise_target", "segment"):
        assert profile.delete_career_item(st, UID, key) == {"deleted": key}
    assert store.KEY not in st.user(UID) and profile.view(st, UID)["career_profile"] is None
    assert profile.delete_career_item(st, UID, "nope")["error"] == "unknown_key"


def test_forget_all_erases_career_cards(tmp_path):
    st = State(tmp_path / "state.json")
    _saved_profile(st)
    logic.save_preferences(st, UID, {"why_now": "評価の仕組みが合わないから"})
    profile.forget_all(st, UID)
    user = st.user(UID)
    assert store.KEY not in user and not user.get("preferences")


def test_web_profile_api_edits_and_deletes_career_cards(app):  # noqa: F811
    """Web のプロフィール画面の口(PUT・DELETE /api/profile/career/{key})。本人は Cookie で決まる。"""
    with TestClient(app.app) as client:
        client.get("/")
        uid = client.cookies["aica_uid"]
        st = app.state
        store.save(st, uid, {"segment": "same_path", "slots": {
            "career1.role": {"status": "filled", "text": "物流会社で現場リーダー", "confidence": "high"}}})
        got = client.get("/api/profile").json()["career_profile"]
        assert got["cards"][0]["items"][0]["value"] == "物流会社で現場リーダー"
        r = client.put("/api/profile/career/career1.role", json={"value": "物流会社で現場リーダー、6年"})
        assert r.status_code == 200 and r.json()["career_profile"]["cards"][0]["items"][0]["value"].endswith("6年")
        assert client.put("/api/profile/career/career1.role", json={"value": "a" * 300}).status_code == 400
        r = client.delete("/api/profile/career/career1.role")
        assert r.status_code == 200 and r.json()["career_profile"]["cards"] == []
        assert client.delete("/api/profile/career/nope").status_code == 400
        assert client.get("/api/profile/export").json()["stored"]["users"][uid][store.KEY]["segment"]["value"] == "same_path"


# ----------------------------- モデルの出力の形 -----------------------------


def test_model_output_schemas_match_the_career_prompts():
    """読み取りは prompt_extract_ja.txt の 5 つのキー(確信度と from は出させない)、言い方は {reply}。"""
    schema = ExtractOut.model_json_schema()
    assert set(schema["properties"]) == {"slot_updates", "ng_updates", "segment", "intent", "review_ack"}
    assert set(schema["$defs"]["SlotUpdate"]["properties"]) == {"slot", "text", "number", "unit", "choice"}
    assert set(SCHEMAS["say"].model_json_schema()["properties"]) == {"reply"}
    assert set(schema["properties"]["intent"]["enum"]) == set(H.INTENTS)
