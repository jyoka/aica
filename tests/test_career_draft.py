"""正社員のヒアリングの職歴カードから作る、職務経歴書の下書き(dip_mcp/career/draft.py、task 247)。実 LLM・実 LINE は呼ばない。

設計メモ docs/design/fulltime-hearing.md の §4(職歴カード)と §5.2(全部の完成の「下書きを開く」)、§6.4 の関門「数字を作った: 0 件」。
- result に着くと、職歴カードから下書きを作り、ゲームの下書きと同じ場所に保存する。結果の返信に「下書きを開く」のボタン
- 書くのは本人の言葉だけ。区切りのボタンは「くらい」をつけたまま。パスと「覚えていない」の欄は空欄
- 下書きの数字は、本人の発言か選んだ区切りにある数字だけ(無い数字は数字の空欄に戻す)
- 空欄は下書きの空欄として数え、下書きの画面(/liff/arcade/draft)で本人が埋める
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from dip_mcp import arcade, logic, profile
from dip_mcp.career import draft, session
from dip_mcp.career import harness as H
from dip_mcp.data import State
from evals.checks import draft_number_violations
from line_runtime.line_channel import line_user
from resume_lines import BLANK_RE, check_numbers, number_blank, words_blank
from tests.test_arcade_liff import _links
from tests.test_career_chat import (  # noqa: F401  harness_on はフィクスチャ(フラグ c1)
    TEXT,
    UID,
    USAGE,
    FakeCareerLLM,
    _drive,
    answer_for,
    current_qid,
    harness_on,
    start_script,
)
from tests.test_consult import ConsultModel
from tests.test_liff_actions import CHANNEL, LIFF_ID
from tests.test_line_data import _say
from tests.test_line_webhook import RAW_USER, _server, line_id_key  # noqa: F401  line_id_key はフィクスチャ
from tests.test_talk_page import app  # noqa: F401  フィクスチャ(Web のサーバー)

LIFF = {"LINE_LIFF_ID": LIFF_ID, "LINE_LIFF_CHANNEL_ID": CHANNEL}
RESULT_BLANK = words_blank("どうなったか")
SCALE_BLANK = number_blank("規模")
ROLE_BLANK = words_blank("何をしていたか")
NUMBER = re.compile(r"\d+")


def _numbers(md: str) -> list[str]:
    """下書きの中の数字(空欄の中は除く)。"""
    return NUMBER.findall(BLANK_RE.sub("", md))


# ----------------------------- LINE のトークで result まで(台本の本人役) -----------------------------


def test_line_result_makes_a_draft_from_the_career_cards_without_made_up_numbers_and_opens_it(tmp_path, monkeypatch):
    """台本の本人役が LINE で正社員の相談を最後まで進める(1 枚目のどうなったかはパス)。result に着くと:
    - 職歴カードから職務経歴書の下書きができ、ゲームの下書きと同じ場所(arcade_drafts)に入る
    - 区切りのボタンは「くらい」つきのまま、何をしていたかは本人の言葉のまま。パスした欄は言葉の空欄
    - 下書きの数字は、どれも本人が送った文(押したボタンのラベルも)にある
    - 結果の返信に「下書きを開く」の LIFF URL のボタンが付き、空欄の数は下書きの空欄の数とそろう"""
    from functools import partial

    from line_runtime.runtime import LineRuntime

    uid = line_user(RAW_USER)
    llm = FakeCareerLLM()
    model = ConsultModel(script=start_script(uid))
    server, fake = _server(tmp_path, monkeypatch, model, **LIFF)
    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=llm))
    st = server.state
    sent: list[str] = []
    with TestClient(server.app) as client:
        assert _say(client, "正社員で転職したいので相談したい", 1).status_code == 200
        for n in range(2, 40):
            qid = current_qid(st, uid)
            if qid is None:
                break
            last = fake.sent()[-1][1]["messages"][-1]
            quick = [i["action"]["text"] for i in last.get("quickReply", {}).get("items", [])]
            said = H.PASS if qid == "card_result1" else answer_for(qid, quick)
            sent.append(said)
            assert _say(client, said, n).status_code == 200
        else:
            raise AssertionError("result に着かない")
        body = fake.sent()[-1][1]
    texts = [m["text"] for m in body["messages"] if m["type"] == "text"]
    assert texts[-1].startswith(H.RESULT_TEXT)

    (rec,) = arcade.drafts(st, uid)
    assert rec["game"] == arcade.CAREER_DRAFT and rec["document_kind"] == "職務経歴書"
    md = rec["markdown"]
    assert f"### {TEXT['card_role1']}" in md  # 本人の言葉のまま(見出し)
    scale = next(s for s in sent if s.endswith(("くらい", "以上")))
    assert f"- 規模: {scale}" in md  # 区切りのボタンは「くらい」つきのまま
    assert f"- 結果: {RESULT_BLANK}" in md  # パスした欄は空欄
    assert H.PASS not in md and H.DONT_KNOW not in md
    # 数字を作っていない: 下書きの数字は、どれも本人が送った文にある(関門「数字を作った: 0 件」)
    assert _numbers(md) and check_numbers(md, sent) == (md, 0)
    for x in _numbers(md):
        assert any(x in s for s in sent), x
    # 条件は書類に書かない(勤務地・年収の下限)
    assert TEXT["hf_area"] not in md and "万円" not in md

    # 空欄の数: 結果の文の「空欄はNつ」と、下書きの空欄の数がそろう
    n = arcade.blanks(rec)
    assert n >= 1 and f"空欄は{n}つ" in texts[-1]
    # 「下書きを開く」は LINE では LIFF URL のボタン(ユーザーIDは載せない)
    assert {"type": "uri", "label": session.DRAFT_LABEL,
            "uri": f"https://liff.line.me/{LIFF_ID}/arcade/draft?rid={rec['id']}"} in _links(body)
    assert uid not in str(_links(body))
    # トークの AI の要約: 下書きがあり、空欄の数を読める(下書きの空欄の仕組みで後から聞ける)
    ad = logic.conversation_summary(st, uid)["arcade_draft"]
    assert ad["game"] == "正社員の相談" and ad["blanks"] == n and ad["view_url"] == f"/liff/arcade/draft?rid={rec['id']}"


# ----------------------------- 数字を作らない(コードの確かめ) -----------------------------


def _state(**slots) -> H.State:
    h = H.new_state()
    h.slots = {k: v for k, v in slots.items()}
    return h


def test_numbers_not_in_what_the_person_said_go_back_to_blanks():
    """欄の数字が本人の発言にも選んだ区切りにもなければ、数字の空欄に戻す。部分だけ抜き出した数(1200社の 200社)も同じ。
    発言にある数字、区切りのラベル(「くらい」つき)、漢数字(半分)はそのまま。"""
    h = _state(**{"career1.role": H.entry("filled", "物流会社で現場リーダー、6年"),
                  "career1.scale": H.entry("filled", "200社", number=200, unit="社"),
                  "career1.result": H.entry("filled", "売上を30%伸ばした"),
                  "career2.role": H.entry("filled", "倉庫の作業スタッフ"),
                  "career2.scale": H.entry("filled", "10人くらい", "10人くらい", rng=[8, 14], unit="人"),
                  "career2.result": H.entry("filled", "誤出荷が半分になった")})
    h.said = ["物流会社で現場リーダー、6年", "取引先は1200社", "売上を伸ばした", "倉庫の作業スタッフ", "10人くらい",
              "誤出荷が半分になった"]
    md, replaced = draft.build(h)
    assert replaced == 2
    assert "- 規模: [N件 ← 数字を入れる]" in md and "200" not in md.replace("1200", "")
    assert "- 結果: 売上を[+X% ← 数字を入れる]伸ばした" in md and "30" not in md
    assert "### 物流会社で現場リーダー、6年" in md and "- 規模: 10人くらい" in md and "- 結果: 誤出荷が半分になった" in md
    assert check_numbers(md, h.said)[1] == 0  # 残った数字は、どれも本人の発言にある
    # 直近が先(1 枚目の後に 2 枚目)
    assert md.index("物流会社") < md.index("倉庫の作業スタッフ")


def test_values_from_the_resume_or_an_earlier_consultation_count_as_the_persons_words():
    """履歴書の読み取り結果と前の相談で保存した欄は、この相談の発言に無くても本人の言葉として数字を確かめる。"""
    h = H.new_state(prefill={"career1.role": {"text": "物流会社で現場リーダー、2018年から6年"}})
    H.apply_saved(h, None, {"career1.scale": {"status": "filled", "text": "120社", "number": 120, "unit": "社"}})
    md, replaced = draft.build(h)
    assert replaced == 0 and "### 物流会社で現場リーダー、2018年から6年" in md and "- 規模: 120社" in md


def test_passed_dont_know_unasked_and_unconfirmed_fields_are_blanks():
    """パス、「覚えていない」、聞かなかった、推測のまま(low)の欄は空欄。2 枚目は何をしていたかが埋まったときだけ書く。
    空欄の数は harness.blank_cards とそろう。個人情報らしい値も書かない(空欄)。"""
    h = _state(**{"career1.role": H.entry("filled", "経理を5年"),
                  "career1.scale": H.entry("dont_know", H.DONT_KNOW),
                  "career1.result": H.entry("passed", H.PASS),
                  "career2.role": H.entry("filled", "販売", confidence="low")})
    h.said = ["経理を5年"]
    md, _ = draft.build(h)
    assert md.count("### ") == 1 and "販売" not in md
    assert f"- 規模: {SCALE_BLANK}" in md and f"- 結果: {RESULT_BLANK}" in md
    assert len(BLANK_RE.findall(md)) == len(H.blank_cards(h)) == 2

    h = _state(**{"career1.role": H.entry("filled", "連絡は090-1234-5678まで"),
                  "career1.result": H.entry("filled", "結構増えた", confidence="low")})
    h.said = ["連絡は090-1234-5678まで", "結構増えた"]
    md, _ = draft.build(h)
    assert "090" not in md and f"### {ROLE_BLANK}" in md and "結構増えた" not in md
    assert "career1.result" in H.blank_cards(h) and len(BLANK_RE.findall(md)) == len(H.blank_cards(h)) == 3

    h = _state(**{"career1.role": H.entry("filled", "経理" * (H.SAVED_TEXT_MAX // 2 + 1))})  # 長すぎる値も保存しない
    h.said = [h.slots["career1.role"]["text"]]
    md, _ = draft.build(h)
    assert f"### {ROLE_BLANK}" in md and len(BLANK_RE.findall(md)) == len(H.blank_cards(h)) == 3


class PartNumberLLM(FakeCareerLLM):
    """まとめで本人が「取引先は1200社」と直すと、読み取りが一部の「200社」を規模に入れる(抜き出した文は発言にあるので通る)。"""

    async def __call__(self, stage, system, messages):
        said = re.search(r"本人の発言: (.*)", system)
        if stage != "say" and said and "1200社" in said.group(1):
            self.calls.append((stage, system, [dict(m) for m in messages]))
            return {"slot_updates": [{"slot": "career1.scale", "text": "200社", "number": 200, "unit": "社", "choice": None}],
                    "ng_updates": [], "segment": None, "intent": "answer", "review_ack": "partial"}, USAGE
        return await super().__call__(stage, system, messages)


def test_result_counts_the_number_blanks_the_draft_put_back(tmp_path):
    """下書きで数字を空欄に戻したとき(replaced が 1 以上)も、結果の「空欄はNつ」と下書きの空欄の数(arcade.blanks)がそろう。"""
    st = State(tmp_path / "state.json")
    llm = PartNumberLLM()
    session.start(st, UID)
    fixed = False
    for _ in range(40):
        qid = current_qid(st, UID)
        if qid is None:
            break
        said = H.PASS if qid == "card_result1" else answer_for(qid, [])
        if qid == "review" and not fixed:
            said, fixed = "規模は取引先1200社", True
        r = _drive(st, UID, said, llm)
    else:
        raise AssertionError("result に着かない")
    assert fixed and r["status"] == "result" and not r["closed"]
    (rec,) = arcade.drafts(st, UID)
    md, n = rec["markdown"], arcade.blanks(rec)
    assert r["draft"]["replaced"] >= 1 and "200" not in md  # 作った数字は空欄に戻した
    assert n == r["draft"]["blanks"] == len(r["blank_cards"]) + r["draft"]["replaced"]
    assert f"空欄は{n}つ" in r["reply"]


def test_later_closes_without_a_draft(tmp_path):
    """「あとで」で閉じたときは、下書きを作らない(続きは次の相談で聞く)。"""
    st = State(tmp_path / "state.json")
    llm = FakeCareerLLM()
    session.start(st, UID)
    r = _drive(st, UID, H.LATER, llm)
    assert r["status"] == "result" and r["closed"] == "later"
    assert "draft" not in r and "buttons" not in r and arcade.drafts(st, UID) == []


def test_a_new_consultation_replaces_the_previous_career_draft(tmp_path):
    """下書きは 1 人に 1 つ(前の相談の下書きを置き換える)。番号は新しくなり、前のリンクは開かない。ゲームの下書きは残す。"""
    st = State(tmp_path / "state.json")
    h = _state(**{"career1.role": H.entry("filled", "経理")})
    h.said = ["経理"]
    first = draft.create(st, UID, h)
    h.slots["career1.role"] = H.entry("filled", "経理を5年")
    h.said = ["経理を5年"]
    second = draft.create(st, UID, h)
    assert second["id"] == first["id"] + 1
    assert arcade.draft(st, UID, first["id"]) is None and "経理を5年" in arcade.draft(st, UID, second["id"])["markdown"]
    assert first["view_url"] == f"/liff/arcade/draft?rid={first['id']}"
    assert st.user(UID).get("arcade_metrics") is None  # ゲームの指標には数えない


# ----------------------------- 下書きの画面で空欄を埋める -----------------------------


def test_blank_fields_are_counted_and_filled_on_the_draft_page(app):  # noqa: F811
    """パスした欄は空欄として数えられ(プロフィールと要約の blanks、確かめたい点)、下書きの画面で本人が埋められる。
    埋めると空欄の数が減る。直すときの決まり(電話番号は保存しない)はゲームの下書きと同じ。"""
    with TestClient(app.app) as client:
        client.get("/")
        uid = client.cookies["aica_uid"]
        st = app.state
        h = _state(**{"career1.role": H.entry("filled", "物流会社で現場リーダー、6年"),
                      "career1.scale": H.entry("dont_know", H.DONT_KNOW),
                      "career1.result": H.entry("passed", H.PASS)})
        h.said = ["物流会社で現場リーダー、6年"]
        d = draft.create(st, uid, h)
        assert d["blanks"] == 2 and d["replaced"] == 0
        (shown,) = client.get("/api/profile").json()["arcade_drafts"]
        assert shown["game"] == "正社員の相談" and shown["blanks"] == 2

        page = client.get("/liff/arcade/draft", params={"rid": d["id"]})
        assert page.status_code == 200 and "正社員の相談で、あなたの答えから作った職務経歴書の下書き" in page.text
        assert "ゲーム(" not in page.text and "数字や言葉" in page.text
        assert "物流会社で現場リーダー、6年の「規模」の行の数字" in page.text  # 確かめたい点
        assert "物流会社で現場リーダー、6年の「結果」の行の言葉" in page.text

        md = arcade.draft(st, uid, d["id"])["markdown"]
        edited = md.replace(RESULT_BLANK, "誤出荷がゼロになった") + "\n連絡先 090-1234-5678\n"
        r = client.post("/liff/arcade/draft", data={"rid": d["id"], "markdown": edited, "scenario_user": ""})
        assert r.status_code == 200 and "保存しました" in r.text
        rec = arcade.draft(st, uid, d["id"])
        assert arcade.blanks(rec) == 1 and "誤出荷がゼロになった" in rec["markdown"] and "090-1234-5678" not in rec["markdown"]
        assert rec["resume"]["work_history"][0]["organization"] == "物流会社で現場リーダー、6年"
        assert rec["resume"]["unconfirmed"] == ["物流会社で現場リーダー、6年の「規模」の行の数字"]
        assert logic.conversation_summary(st, uid)["arcade_draft"]["blanks"] == 1
        # プロフィールの「消す」で消える
        assert profile.delete_arcade_draft(st, uid, d["id"]) == {"deleted": "arcade_draft", "id": d["id"]}
        assert client.get("/liff/arcade/draft", params={"rid": d["id"]}).status_code == 404


# ----------------------------- 評価の判定 -----------------------------


def test_the_result_draft_button_is_not_the_one_draft_button_of_the_conversation():
    """結果の「下書きを開く」(ハーネスの返信。career の印)は、§14 の「下書きを見る」の 1 回に数えない。"""
    url = "/liff/arcade/draft?rid=0"
    result = {"bubbles": [H.RESULT_TEXT], "buttons": [{"label": session.DRAFT_LABEL, "url": url}],
              "career": {"phase": "result", "question": None}}
    agent = {"bubbles": ["下書きの画面で、この数字を入れて仕上げられます。"], "buttons": [{"label": "下書きを見る", "url": url}]}
    assert draft_number_violations({}, [result, agent], ["これでOK", "10人です"]) == []
    assert draft_number_violations({}, [agent, agent], ["", ""])  # トークの AI が 2 回出したら違反のまま


def test_the_mock_regression_counts_a_made_up_number_in_the_draft():
    """モックの回帰(evals/career_sim.py)は、下書きに書く欄の数字が本人役の発言に無ければ made_up_number に数える(関門)。"""
    from evals import career_sim
    from evals.fulltime_hearing.personas import load_personas

    persona = next(p for p in load_personas() if p["id"] == "p1-sales")
    r = career_sim.run_one(persona, "C")
    m = career_sim.metrics(r, persona)
    assert m["made_up_number"] == 0 and m["draft_blanks"] == 0 and career_sim.failures(m) == []
    r["state"].slots["career1.result"]["text"] = "売上を37%伸ばした"  # 本人役は 37% と言っていない
    m = career_sim.metrics(r, persona)
    assert m["made_up_number"] == 1 and "made_up_number=1" in career_sim.failures(m)
