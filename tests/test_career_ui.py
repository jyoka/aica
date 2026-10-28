"""正社員のヒアリングの形 D(LINE の Flex。設計メモ docs/design/fulltime-hearing.md §5.2、hearing-progress-ui.md、task 246)。
実 LLM なし。

- dip の側(dip_mcp/career/ui.py): どのターンに何を付けるか(§3.4 の順、1 ターンに 0〜1 つ)、何を数えるか(§4)、読む時間
- ハーネス(dip_mcp/career/harness.py)の形 D: 「始める」の 1 枚、まとめの「直す」、形 C と D の切り替え
- LINE 側(line_runtime/career_ui.py): Flex の形と LINE の上限、字数、文の形(Web のトークと評価の本人役)
- LINE の Webhook を通した通し(偽の LINE、偽のモデル、本物の dip の MCP)と、Web のトーク(talk-core.js。Playwright があるときだけ)
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from dip_mcp.career import harness as H
from dip_mcp.career import session
from dip_mcp.career import ui as UI
from dip_mcp.data import State
from evals import career_sim
from evals.fulltime_hearing.personas import load_personas
from line_runtime import career_ui as C
from line_runtime import line_channel
from line_runtime import progress_ui as P
from line_runtime.line_channel import line_user, to_messages
from tests.test_career_chat import (
    TEXT,
    FakeCareerLLM,
    answer_for,
    current_qid,
    start_script,
)
from tests.test_consult import ConsultModel
from tests.test_line_data import _say
from tests.test_line_webhook import RAW_USER, _server, line_id_key  # noqa: F401  line_id_key はフィクスチャ

PERSONAS = load_personas()
TALK_CORE = Path(__file__).resolve().parents[1] / "line_runtime" / "static" / "talk-core.js"
KINDS = (UI.COMPLETE, UI.REVIEW, UI.CARD, UI.CAREER_CARD, UI.BAND)  # §3.4 の順


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in (H.FORM_ENV, UI.BAND_ENV, line_channel.LIFF_ID_ENV, line_channel.LIFF_CHANNEL_ENV,
              line_channel.LOGIN_CHANNEL_ENV, line_channel.PUBLIC_URL_ENV):
        monkeypatch.delenv(k, raising=False)


class LLM:
    """読み取りは、今の問いの欄に本人の文をそのまま入れる。言い方は問いの原文。"""

    def extract(self, payload):
        t = [x for x in payload["targets"] if x in H.ALL_SLOTS][:1]
        ups = [{"slot": t[0], "text": payload["user_text"], "number": None, "unit": None, "choice": None}] if t else []
        return json.dumps({"slot_updates": ups, "ng_updates": [], "segment": None, "intent": "answer", "review_ack": None},
                          ensure_ascii=False)

    def generate(self, view):
        return json.dumps({"reply": view["text"]}, ensure_ascii=False)


def step(h, mem, text):
    """session.turn と同じ順で、turn() の前後から ui を作る。"""
    prev = UI.before(h)
    r = H.turn(h, text, H.BANK, LLM())
    ui, mem = UI.update(h, mem, prev, r)
    return ui, mem, r


def answer(h, out):
    cur = h.current
    if cur["mode"] == "start":
        return H.START_GO
    return answer_for(cur["qid"], out["choices"])


def run(form="D", band=True):
    """形 D の相談を result まで: [(本人の発言, turn の結果, ui, このターンに新しく埋まった欄)]。"""
    h = H.new_state(form=form)
    mem = UI.fresh_mem(h, band)
    out = H.start(h, H.BANK)
    turns = [(None, out, UI.start_payload(h), set())]
    for _ in range(40):
        if out["done"]:
            return h, turns
        said = answer(h, out)
        before = set(h.slots)
        ui, mem, out = step(h, mem, said)
        turns.append((said, out, ui, set(h.slots) - before))
    raise AssertionError("result に着かない")


# ----------------------------- 形 C と D、帯の設定 -----------------------------


def test_form_setting_switches_between_c_and_d(monkeypatch):
    """CAREER_HARNESS_FORM=D なら Flex の部品(ui)が付き、C なら付かない。問いは同じ(D は C の問いを使う)。"""
    monkeypatch.setenv(H.FORM_ENV, "D")
    d = H.new_state()
    assert d.form == "D" and UI.is_ui(d)
    monkeypatch.setenv(H.FORM_ENV, "C")
    c = H.new_state()
    assert c.form == "C" and not UI.is_ui(c)
    assert [q["id"] for q in H.questions(d, H.BANK)] == [q["id"] for q in H.questions(c, H.BANK)]
    _, turns_c = run("C")
    assert all(ui is None for *_, ui, _ in turns_c)  # C は Flex を付けない(進み具合は吹き出しの 1 行)
    assert any("✓職歴" in out["reply"] for _, out, *_ in turns_c)
    _, turns_d = run("D")
    assert next(ui["kind"] for *_, ui, _ in turns_d if ui) == UI.START
    assert not any("✓職歴" in out["reply"] for _, out, *_ in turns_d)  # D は職歴カードの Flex が見せる


def test_session_reads_the_form_and_band_settings_when_it_starts(tmp_path, monkeypatch):
    """dip のツールの start で、形と帯の設定を相談に覚える(始めた相談は、設定を変えても最後まで同じ)。"""
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    monkeypatch.setenv(H.FORM_ENV, "D")
    monkeypatch.setenv(UI.BAND_ENV, "off")
    st = State(tmp_path / "state.json")
    r = session.start(st, "w-0000000000000d01")
    assert r["ui"]["kind"] == UI.START and r["question"]["choices"] == [H.START_GO, H.LATER]
    saved = st.user("w-0000000000000d01")[session.KEY]
    assert saved["harness"]["form"] == "D" and saved["ui"]["band"] is False
    monkeypatch.setenv(H.FORM_ENV, "C")
    monkeypatch.delenv(UI.BAND_ENV)
    r = session.start(st, "w-0000000000000d01")  # 進めている相談は続ける
    assert r["started"] is False and st.user("w-0000000000000d01")[session.KEY]["ui"]["band"] is False


def test_band_setting_turns_the_band_off_and_keeps_the_other_parts():
    _, on = run(band=True)
    _, off = run(band=False)
    kinds_on = [ui["kind"] for *_, ui, _ in on if ui]
    kinds_off = [ui["kind"] for *_, ui, _ in off if ui]
    assert UI.BAND in kinds_on and UI.BAND not in kinds_off
    for k in (UI.START, UI.CARD, UI.CAREER_CARD, UI.REVIEW, UI.COMPLETE):
        assert k in kinds_off
    assert UI.band_on() is True


def test_band_env_values(monkeypatch):
    for v, on in (("off", False), ("OFF", False), ("0", False), ("on", True), ("", True), ("x", True)):
        monkeypatch.setenv(UI.BAND_ENV, v)
        assert UI.band_on() is on, v


# ----------------------------- どのターンに何を付けるか(§3.4) -----------------------------


def test_at_most_one_flex_per_turn_in_the_design_order_and_no_band_on_review_turns():
    h, turns = run()
    kinds = []
    for said, out, ui, _ in turns:
        built = C.build(ui)
        msgs = to_messages({"bubbles": out["bubbles"], "quick_replies": out["choices"],
                            "progress": built} if built else {"bubbles": out["bubbles"], "quick_replies": out["choices"]},
                           base_url="", features=())
        assert len(msgs) == 1 + bool(built) and msgs[0]["type"] == "text"  # 吹き出し 1 つ + Flex 0〜1 つ
        if not ui:
            continue
        kinds.append(ui["kind"])
        if ui["kind"] == UI.START:
            continue
        p = ui.get("progress") or {}
        if out["phase"] == "review" and not out["done"]:
            assert ui["kind"] == UI.REVIEW  # まとめのターンに帯もプロフィールカードも付けない
    assert kinds[0] == UI.START and kinds[-1] == UI.COMPLETE and 1 <= kinds.count(UI.CARD) <= 2  # 1 回の相談で 2 回まで
    assert UI.BAND in kinds and UI.CAREER_CARD in kinds and UI.REVIEW in kinds
    assert h.result is not None
    assert p  # 完成は progress を持つ


def test_choose_follows_the_order_when_several_apply():
    """1 つのターンにいくつも当てはまるときは、上のもの(完成 > まとめ > プロフィールカード > 職歴カード > 帯)。"""
    h = H.new_state(form="D")
    mem = UI.fresh_mem(h)
    H.start(h, H.BANK)
    for said in (H.START_GO, "同じ仕事で上へ", TEXT["ib_why_now"], TEXT["hf_area"], "400万円台", "すぐ"):
        ui, mem, _ = step(h, mem, said)
    assert h.current["qid"] == "hf_raise"
    # hard_filter の最後の答え: 欄が埋まった(帯)と、career_cards に移った(プロフィールカード)が重なる → カード
    ui, mem, _ = step(h, mem, "年収")
    assert h.phase == "career_cards" and ui["kind"] == UI.CARD and ui["progress"]["added"]
    # 職歴カードの欄(職歴カード)と、条件の欄(帯)が同じターンに埋まる → 職歴カード
    before = UI.before(h)
    r = H.turn(h, "物流会社で現場リーダー、6年", H.BANK, LLM())
    h.slots["work_style"] = H.entry("filled", "リモート多め", ["remote"], confidence="low", frm="free_other:x")
    ui, mem = UI.update(h, mem, before, r)
    assert ui["kind"] == UI.CAREER_CARD and "work_style" in mem["seen"]  # 帯にも当たるが、職歴カードを付ける
    # まとめのターンは、欄が埋まっても(本人が文で直した)まとめだけ
    while h.current["mode"] != "review":
        ui, mem, _ = step(h, mem, answer(h, {"choices": h.current["choices"]}))
    assert ui["kind"] == UI.REVIEW


def test_career_card_only_on_turns_where_a_card_field_was_filled():
    for band in (True, False):
        _, turns = run(band=band)
        seen = 0
        for _, _, ui, new in turns:
            kind = ui and ui["kind"]
            card_now = {k for k in new if k.startswith("career")}
            if kind == UI.CAREER_CARD:
                seen += 1
                assert card_now and f"career{ui['career_card']['n']}.role" in {k.split(".")[0] + ".role" for k in card_now}
            elif kind not in (UI.COMPLETE, UI.REVIEW, UI.CARD):  # 上の部品に譲ったターンのほかは
                assert not card_now
        assert seen >= 3
    # 欄が埋まらなかったターン(問いを出しただけ、聞き直し)には付かない
    h = H.new_state(form="D")
    mem = UI.fresh_mem(h)
    H.start(h, H.BANK)
    for said in (H.START_GO, "同じ仕事で上へ", TEXT["ib_why_now"], TEXT["hf_area"], "400万円台", "すぐ", "年収"):
        ui, mem, _ = step(h, mem, said)
    assert h.current["qid"] == "card_role1"
    ui, mem, _ = step(h, mem, "物流会社で現場リーダー、6年")
    assert ui["kind"] == UI.CAREER_CARD and ui["career_card"]["status"] is None  # 1 つ目はバーだけ
    assert ui["career_card"]["rows"][1]["value"] is None and not ui["career_card"]["complete"]
    ui, mem, _ = step(h, mem, "チーム5人くらい")
    assert ui["kind"] == UI.CAREER_CARD and ui["career_card"]["status"] == "あと1つ"
    ui, mem, _ = step(h, mem, "誤出荷が半分になった")
    c = ui["career_card"]
    assert c["status"] is None and c["complete"]  # 3 つが本人の言葉で埋まった → 一言は無く、完成のスタンプ
    # 2 枚目の何をしていたかをスキップ: 空欄として埋まり(灰色の「スキップ」)、残りの 2 つは聞かないので career_cards が終わる。
    # 職歴カードより上のプロフィールカードを付ける(職歴の行は、何をしていたかの本人の言葉。数は出さない)
    ui, mem, _ = step(h, mem, H.PASS)
    assert h.phase != "career_cards" and ui["kind"] == UI.CARD
    assert {"label": "職歴カード", "value": "物流会社で現場リーダー、6年", "inferred": False, "undecided": False} in \
        ui["progress"]["groups"][1]["items"]
    assert ui["progress"]["growth"]["head"] == "職歴が入りました"
    c = UI.career_card(h, 2)
    assert c["rows"][0] == {"label": "仕事", "value": H.PASS, "muted": True} and not c["complete"]
    assert c["status"] is None and c["done"] == 1  # 2 枚目は何をしていたかだけ埋まった(パス)
    assert [UI.card_status(d, 3, False) for d in (1, 2, 3)] == [None, "あと1つ", "空欄あり"]
    assert UI.card_status(3, 3, True) is None  # 完成は「完成」のスタンプが見せる


def test_counts_follow_section_4_and_start_from_hard_filter():
    """全体は segment の条件の HARD と MID + 職歴カードの欄 6 つ。「始める」と icebreak の間は出さない。"""
    h = H.new_state(form="D")
    mem = UI.fresh_mem(h)
    H.start(h, H.BANK)
    ui, mem, _ = step(h, mem, H.START_GO)
    assert ui is None and h.current["qid"] == "q_segment"
    ui, mem, _ = step(h, mem, "同じ仕事で上へ")
    assert ui is None and h.phase == "icebreak"
    ui, mem, _ = step(h, mem, H.PASS)  # なぜ今のパス: 空欄として埋まる(数える)
    assert h.phase == "hard_filter" and ui["kind"] == UI.BAND
    p = ui["progress"]
    assert (p["filled"], p["total"]) == (1, 5 + 2 + 6)  # same_path: HARD 5、MID 2、職歴カード 6
    assert p["added"] == [{"label": "なぜ今", "value": "なぜ今 スキップ", "inferred": False}]
    assert [g["name"] for g in p["groups"]] == ["条件", "職歴", "働き方", "NG"]
    # 帯は数字を出さない: 最初に埋まったターンは芽が出る。パスは札の後ろに値だけ
    assert ui["band"] == {"icon": "🌱", "chip": "＋なぜ今", "value": "スキップ", "more": None, "up": "プロフィールの芽が出ました",
                              "filled": 1, "total": 13}


def test_review_has_fix_buttons_and_fixing_a_field_goes_back_to_the_review():
    """まとめの行の「直す」を押すと、その欄の問いを出し(数えない)、答えると直した値(high)でまとめに戻る。"""
    h = H.new_state(form="D")
    mem = UI.fresh_mem(h)
    out = H.start(h, H.BANK)
    while h.current["mode"] != "review":
        ui, mem, out = step(h, mem, answer(h, out))
    assert ui["kind"] == UI.REVIEW and out["bubbles"] == [H.REVIEW_LEAD_UI]  # 行は吹き出しでなく Flex
    rows = ui["review"]["rows"]
    area = next(r for r in rows if r["text"].startswith("勤務地"))
    assert area["fix"] == [{"label": "直す", "text": "勤務地を直す"}]
    card = next(r for r in rows if r["text"].startswith("職歴1"))
    assert [b["label"] for b in card["fix"]] == ["仕事", "規模", "結果"]
    turns = h.turns
    ui, mem, out = step(h, mem, "勤務地を直す")
    assert h.current["mode"] == "fix" and h.current["qid"] == "hf_area" and h.turns == turns and ui is None
    assert out["bubbles"][0].startswith(H.FIX_LEAD)
    ui, mem, out = step(h, mem, "京都市内")
    assert h.slots["area"]["text"] == "京都市内" and h.slots["area"]["confidence"] == "high"
    assert h.slots["area"]["from"] == "fix:hf_area" and h.turns == turns + 1
    assert h.current["mode"] == "review" and ui["kind"] == UI.REVIEW
    assert any(r["text"] == "勤務地: 京都市内" for r in ui["review"]["rows"])
    ui, mem, out = step(h, mem, "職歴1の規模を直す")
    assert h.current["qid"] == "card_scale1"
    ui, mem, out = step(h, mem, "チーム20人以上")
    assert h.slots["career1.scale"]["text"] == "チーム20人以上" and h.slots["career1.scale"]["range"]
    while not out["done"]:
        ui, mem, out = step(h, mem, H.REVIEW_OK)
    assert ui["kind"] == UI.COMPLETE and h.turns <= H.MAX_TURNS


def _to_review():
    h = H.new_state(form="D")
    mem = UI.fresh_mem(h)
    out = H.start(h, H.BANK)
    while h.current["mode"] != "review":
        _ui, mem, out = step(h, mem, answer(h, out))
    return h, mem


def test_pressing_another_fix_button_while_fixing_switches_the_fix_and_keeps_the_values():
    """直している間に、まだ画面にあるまとめの別の「直す」を押した: ボタンの文を値にせず、その欄を直す問いに替える。"""
    h, mem = _to_review()
    before = dict(h.slots["career1.result"])
    step(h, mem, "職歴1の結果を直す")
    ui, mem, _out = step(h, mem, "職歴1の仕事を直す")
    assert {k: h.current[k] for k in ("qid", "mode", "slot", "choices")} == {
        "qid": "card_role1", "mode": "fix", "slot": "career1.role", "choices": H.fix_choices(H.qmap(h, H.BANK)["card_role1"])}
    assert h.slots["career1.result"] == before and ui is None
    step(h, mem, "物流会社で倉庫のリーダー、6年")
    assert h.slots["career1.role"]["text"] == "物流会社で倉庫のリーダー、6年" and h.current["mode"] == "review"


def test_a_fix_button_left_on_an_earlier_review_still_opens_the_fix():
    """前のまとめの Flex に残ったボタン(今のまとめに無い行)でも、今の segment の欄なら直す問いを出す(まとめを終えない)。"""
    h, mem = _to_review()
    h.last_card = dict(h.last_card, slots=[["area"]], lines=["勤務地: 大阪市内"])  # 今のまとめは勤務地の行だけ
    _ui, mem, out = step(h, mem, "職歴1の規模を直す")
    assert h.current["mode"] == "fix" and h.current["slot"] == "career1.scale" and not out["done"]
    assert h.review_rounds == 0


def test_fixes_always_leave_a_turn_to_confirm_the_review():
    """直した答えは review のターンに数える。最後の 1 ターンは「これでOK」に残し、まとめを確かめずに終えない。"""
    h, mem = _to_review()
    for n in range(20):
        ui, mem, out = step(h, mem, "勤務地を直す")
        if h.current["mode"] == "review":  # もう直せない: まとめを出し直す(文で直すのは受ける)
            assert out["bubbles"][0].startswith(H.FIX_LIMIT_LEAD) and ui["kind"] == UI.REVIEW and not out["done"]
            break
        ui, mem, out = step(h, mem, f"京都市内{n}")
        assert h.current["mode"] == "review" and not out["done"]
    else:
        raise AssertionError("直す回数が止まらない")
    assert h.turns == H.MAX_TURNS - 1 and sum(h.phase_turns.values()) == h.turns
    ui, mem, out = step(h, mem, H.REVIEW_OK)
    assert out["done"] and h.review_rounds == 1 and h.turns == H.MAX_TURNS and ui["kind"] == UI.COMPLETE


def test_passing_a_fix_on_a_guessed_slot_clears_the_guess():
    """場面の二択でしか聞かない欄(人と働く形)を直すときのパスは、推し量った前の値を残さない(「これでOK」で確かめたことにしない)。"""
    h, mem = _to_review()
    h.slots["team_style"] = H.entry("filled", "チームで分担", "team", confidence="low", frm="choice:sc_big_task")
    step(h, mem, "人と働く形を直す")
    assert h.current["qid"] == "sc_big_task"
    step(h, mem, H.PASS)
    assert h.slots["team_style"]["status"] == "passed" and h.slots["team_style"]["value"] is None
    step(h, mem, "勤務地を直す")
    step(h, mem, H.PASS)
    assert h.slots["area"]["status"] == "passed" and h.slots["area"]["value"] == H.NOPREF  # 条件のパスは「こだわりなし」


@pytest.mark.parametrize("slot,fix", [("area", "勤務地を直す"), ("career1.result", "職歴1の結果を直す")])
def test_keep_on_a_fix_goes_back_to_the_review_without_touching_the_value(slot, fix):
    """押し間違えた「直す」をやめる: 「そのまま」は本人の打った値(high)を変えずにまとめに戻る(パスで消さない)。"""
    h, mem = _to_review()
    h.slots[slot] = H.entry("filled", "本人が打った値", frm="text:q", confidence="high")
    before = dict(h.slots[slot])
    _ui, mem, out = step(h, mem, fix)
    assert h.current["mode"] == "fix" and H.FIX_KEEP in out["choices"]
    assert out["choices"][-1] == H.FIX_KEEP and H.LATER not in out["choices"] and len(out["choices"]) <= H.QUICK_MAX
    turns = h.turns
    ui, mem, out = step(h, mem, H.FIX_KEEP)
    assert h.slots[slot] == before
    assert h.current["mode"] == "review" and ui["kind"] == UI.REVIEW and not out["done"]
    assert h.turns == turns + 1 and h.review_rounds == 0 and sum(h.phase_turns.values()) == h.turns
    while not out["done"]:
        ui, mem, out = step(h, mem, H.REVIEW_OK)
    assert h.slots[slot]["text"] == "本人が打った値" and h.slots[slot]["confidence"] == "high"


def test_an_aside_while_fixing_is_not_counted():
    h, mem = _to_review()
    h.aside_count = H.MAX_ASIDES
    step(h, mem, "勤務地を直す")
    turns = h.turns
    r = H.turn(h, "ところで、求人はいつ見られますか", H.BANK, Aside())
    assert h.turns == turns and h.current["mode"] == "fix" and not r["done"]


class Aside(LLM):
    def extract(self, payload):
        return json.dumps({"slot_updates": [], "ng_updates": [], "segment": None, "intent": "aside", "review_ack": None})


def test_saved_values_are_not_shown_as_just_filled_on_the_band():
    """前の相談で保存した欄(と履歴書の欄)は、始めたときから埋まっていたものとして数える(帯の「いま埋まった」に出さない)。"""
    h = H.new_state(form="D")
    H.apply_saved(h, "same_path", {"why_now": {"status": "filled", "text": "評価が合わない"},
                                   "area": {"status": "filled", "text": "大阪市内"}})
    mem = UI.fresh_mem(h)
    assert set(mem["seen"]) == {"why_now", "area"}
    H.start(h, H.BANK)
    ui, mem, _ = step(h, mem, H.START_GO)
    assert h.phase == "hard_filter" and ui is None  # 何も新しく埋まっていない
    ui, mem, _ = step(h, mem, "400万円台")
    assert [a["label"] for a in ui["progress"]["added"]] == ["年収の下限"] and ui["progress"]["filled"] == 3


def test_fix_buttons_are_only_read_in_form_d_on_the_review():
    h = H.new_state(form="C")
    H.start(h, H.BANK)
    assert H.fix_targets(h) == {}
    h = H.new_state(form="D")
    H.start(h, H.BANK)
    assert H.fix_targets(h) == {}  # まとめの前は「直す」の文を答えとして読む


def test_start_card_and_typing_an_answer_instead_of_pressing_start():
    """「始める」の 1 枚: 押せば最初の問い(数えない)、ボタンを押さずに打った文は最初の問いへの答えとして読む。"""
    h = H.new_state(form="D")
    out = H.start(h, H.BANK)
    assert out["bubbles"] == [H.START_LEAD_UI] and out["choices"] == [H.START_GO, H.LATER]
    assert "中断" in UI.START_NOTE and H.PASS in UI.START_NOTE  # やめ方は 1 枚の一言が言う(吹き出しと二重にしない)
    s = UI.start_payload(h)["start"]
    assert s["title"] == "まずは1問から" and s["buttons"] == [H.START_GO, UI.RESUME_START, H.LATER]
    assert [x["icon"] for x in s["steps"]] == ["🌱", "🌿", "🌸"] and s["note"] == UI.START_NOTE
    assert all(len(b) <= 20 for b in s["buttons"])
    out = H.turn(h, H.START_GO, H.BANK, LLM())
    assert h.current["qid"] == "q_segment" and h.turns == 0 and out["choices"][:2] == ["同じ仕事で上へ", "仕事を変えたい"]
    h = H.new_state(form="D")
    H.start(h, H.BANK)
    H.turn(h, "同じ仕事で上へ", H.BANK, LLM())  # ボタンの文を打った: q_segment の答えとして読む
    assert h.segment == "same_path" and h.current["qid"] == "ib_why_now" and h.turns == 1
    h = H.new_state(form="D")
    H.start(h, H.BANK)
    out = H.turn(h, H.LATER, H.BANK, LLM())
    assert out["done"] and h.closed == "later"


# ----------------------------- 字数と LINE の上限 -----------------------------


@pytest.mark.parametrize("persona", PERSONAS, ids=[p["id"] for p in PERSONAS])
def test_every_flex_fits_line_limits_and_the_design_lengths(persona, monkeypatch):
    """クイックリプライ 13 個・ラベル 20 字、1 回の返信 5 メッセージ、Flex のボタンのラベル 20 字、altText 400 字。
    職歴カードの中は 60 字、帯 24 字、プロフィールカード 80 字(§3.3)。ハーネスが読む時間に数えた字は、Flex の中の字と同じ
    (帯とプロフィールカードは見積もりなので、それより少なくはない)。"""
    monkeypatch.setenv(line_channel.PUBLIC_URL_ENV, "https://aica.example.test")
    monkeypatch.setenv(line_channel.LIFF_ID_ENV, "1234567890-abcdefgh")
    monkeypatch.setenv(line_channel.LIFF_CHANNEL_ENV, "1234567890")
    r = career_sim.run_one(persona, "D")
    m = career_sim.metrics(r, persona)
    assert career_sim.failures(m) == [] and m["completed"]
    h = H.new_state(form="D", prefill=career_sim.prefill(persona))
    mem = UI.fresh_mem(h)
    actor, llm = career_sim.MockActor(persona), career_sim.OracleLLM()
    out = H.start(h, H.BANK)
    ui = UI.start_payload(h)
    while True:
        built = C.build(ui)
        msgs = to_messages({"bubbles": out["bubbles"], "quick_replies": out["choices"]}
                           | ({"progress": built} if built else {}), base_url="", features=())
        assert len(msgs) <= line_channel.MAX_MESSAGES and len(msgs) == 1 + bool(built)
        items = msgs[-1].get("quickReply", {}).get("items", [])
        assert len(items) <= 13 and all(len(i["action"]["label"]) <= 20 for i in items)
        if built:
            flex = built["flex"]
            assert flex["type"] == "flex" and 0 < len(flex["altText"]) <= 400
            assert all(len(a["label"]) <= 20 for a in _actions(flex))
            n = sum(H.chars(t) for t in C.flex_lines(flex))
            if ui["kind"] in (UI.BAND, UI.CARD, UI.COMPLETE):
                assert UI.read_chars(ui) >= n - 6, (ui["kind"], UI.read_chars(ui), n)
            else:
                assert UI.read_chars(ui) == n, (ui["kind"], UI.read_chars(ui), n)
            if ui["kind"] == UI.CAREER_CARD:
                assert n <= UI.CAREER_CARD_MAX
            if ui["kind"] == UI.BAND:
                b = ui["band"]
                assert len(b["chip"] + b["value"] + (b["more"] or "")) <= UI.BAND_MAX
                assert UI.read_chars(ui) == n
            if ui["kind"] == UI.CARD:
                assert P.flex_chars(flex) <= P.CARD_MAX
            json.dumps(flex, ensure_ascii=False)
        if out["done"]:
            break
        said, _, llm.meaning = actor.answer(h, H.BANK, out, False)
        ui, mem, out = step_with(h, mem, said, llm)


def step_with(h, mem, said, llm):
    prev = UI.before(h)
    r = H.turn(h, said, H.BANK, llm)
    ui, mem = UI.update(h, mem, prev, r)
    return ui, mem, r


def _actions(node):
    if isinstance(node, dict):
        own = [node["action"]] if isinstance(node.get("action"), dict) else []
        return own + [a for k, v in node.items() if k != "action" for a in _actions(v)]
    if isinstance(node, list):
        return [a for v in node for a in _actions(v)]
    return []


def test_long_values_are_clipped_to_60_chars_in_the_career_card():
    h = H.new_state(form="D")
    long = "とても長い会社の名前の物流センターで現場のリーダーを六年ほど担当していました"
    h.slots["career1.role"] = H.entry("filled", long)
    h.slots["career1.scale"] = H.entry("filled", "120社の取引先と、毎月のべ三千件ほどの出荷", number=120, unit="社")
    h.slots["career1.result"] = H.entry("filled", "誤出荷が半分になり、表彰されて、手順書が全拠点で使われるようになった")
    c = UI.career_card(h, 1)
    built = C.build({"kind": UI.CAREER_CARD, "career_card": c})
    n = sum(H.chars(t) for t in C.flex_lines(built["flex"]))
    assert n <= UI.CAREER_CARD_MAX and c["complete"] and all(r["value"].endswith("…") for r in c["rows"])
    assert UI.read_chars({"kind": UI.CAREER_CARD, "career_card": c}) == n


def test_shared_limits_match_progress_ui():
    """dip は line_runtime を import しないので、帯とプロフィールカードの上限と字を写して持つ。同じ値か。"""
    assert (UI.BAND_MAX, UI.BAND_ITEM_MAX, UI.PROFILE_MAX) == (P.BAND_MAX, P.BAND_ITEM_MAX, P.CARD_MAX)
    assert (UI.BAND_HEAD, UI.PROFILE_HEAD, UI.TODO) == (P.BAND_HEAD, P.HEAD, P.TODO)
    assert (UI.NEXT_LABEL, UI.DONE_MARK) == (C.NEXT_LABEL, C.DONE_MARK)


def test_band_clips_the_value_and_keeps_the_guess_mark():
    """帯の札と値と「ほかN」は 24 字まで。値を切って「…」、推し量った値の「?」は残す。"""
    p = {"filled": 3, "total": 13, "added": [{"label": "次にやりたいこと", "value": "現場の改善をもっと進めたい",
                                            "inferred": True}] * 3, "growth": UI.growth(0, False, False)}
    b = UI.band(p)
    assert b["chip"] == "＋次にやりたいこと" and b["more"] == "ほか2" and b["value"].endswith("…?")
    assert len(b["chip"] + b["value"]) <= UI.BAND_ITEM_MAX and len(b["chip"] + b["value"] + b["more"]) <= UI.BAND_MAX
    flex = C.build({"kind": UI.BAND, "progress": p, "band": b})["flex"]
    assert sum(H.chars(t) for t in C.flex_lines(flex)) == UI.read_chars({"kind": UI.BAND, "band": b})


def test_growth_follows_the_phases_from_sprout_to_bloom():
    """育ち方(task 288): 聞き終えたフェーズで 🌱(条件)→ 🌿(職歴)→ 🌷、相談を終えたら 🌸(「始める」の 1 枚と同じ絵)。
    帯の一言(up)は相談で最初の帯だけ。プロフィールカードの見出しは、聞き終えたフェーズの一言を先に。"""
    assert UI.growth(0, True, False) == {"icon": "🌱", "word": "芽が出ました", "up": UI.SPROUT, "head": "芽が出ました"}
    assert UI.growth(0, False, False)["up"] is None
    g = UI.growth(1, False, False, "hf")
    assert (g["icon"], g["head"]) == ("🌿", "条件が入りました")
    assert UI.growth(2, False, False, "cards")["head"] == "職歴が入りました"
    assert UI.growth(2, False, False)["icon"] == "🌷" and UI.growth(5, False, False)["icon"] == "🌷"
    assert UI.growth(2, False, True)["icon"] == "🌸"
    _, turns = run()
    icons = [(ui["kind"], (ui.get("progress") or {}).get("growth", {}).get("icon")) for *_, ui, _ in turns
             if ui and ui["kind"] in (UI.BAND, UI.CARD, UI.COMPLETE)]
    assert icons[0] == (UI.BAND, "🌱") and icons[-1] == (UI.COMPLETE, "🌸")
    cards = [i for k, i in icons if k == UI.CARD]
    assert cards[0] == "🌿" and set(cards) <= {"🌿", "🌷"}  # 条件を聞き終えた(と、職歴を聞き終えた)
    ups = [ui["band"]["up"] for *_, ui, _ in turns if ui and ui["kind"] == UI.BAND]
    assert ups[0] == UI.SPROUT and not any(ups[1:])


def test_sprout_shows_on_the_first_band_even_with_saved_values():
    """履歴書や前の相談の値が先に入っていても、相談で最初に出す帯には「プロフィールの芽が出ました」。"""
    h = H.new_state(form="D")
    H.apply_saved(h, "same_path", {"why_now": {"status": "filled", "text": "評価が合わない"}})
    mem = UI.fresh_mem(h)
    H.start(h, H.BANK)
    step(h, mem, H.START_GO)
    ui, mem, _ = step(h, mem, "大阪市内")
    assert ui["kind"] == UI.BAND and ui["band"]["up"] == UI.SPROUT and mem["sprouted"]
    ui, mem, _ = step(h, mem, "400万円台")
    assert ui["kind"] == UI.BAND and ui["band"]["up"] is None


def test_passing_both_jobs_does_not_say_the_career_was_added():
    """職歴カードの欄をどれもスキップしたら、プロフィールの職歴の行は「スキップ」、見出しは「職歴が入りました」にしない。"""
    h = H.new_state(form="D")
    mem = UI.fresh_mem(h)
    H.start(h, H.BANK)
    for said in (H.START_GO, "同じ仕事で上へ", TEXT["ib_why_now"], TEXT["hf_area"], "400万円台", "すぐ", "年収"):
        ui, mem, _ = step(h, mem, said)
    while h.phase == "career_cards":
        ui, mem, _ = step(h, mem, H.PASS)
    assert ui["kind"] == UI.CARD
    p = ui["progress"]
    assert p["groups"][1]["items"][0]["value"] == H.PASS
    assert p["growth"]["head"] != UI.MILESTONE_WORDS["cards"] and p["growth"]["icon"] == "🌷"
    assert "職歴：スキップ" in C.build(ui)["text"]


def test_complete_carousel_never_says_one_more_left():
    """相談を終えた完成のカルーセルでは、もう聞かないので「あと1つ」を出さない(完成でなければ「空欄あり」)。"""
    assert UI.card_status(2, 3, False, final=True) == "空欄あり" and UI.card_status(1, 3, False, final=True) == "空欄あり"
    assert UI.card_status(3, 3, True, final=True) is None
    h = H.new_state(form="D")
    h.slots["career1.role"] = H.entry("filled", "物流会社で現場リーダー、6年")
    h.slots["career1.scale"] = H.entry("filled", "5人くらい", number=5, unit="人")
    assert UI.career_card(h, 1)["status"] == "あと1つ" and UI.career_card(h, 1, final=True)["status"] == "空欄あり"


def test_no_question_counts_or_bare_fractions_are_shown():
    """入口で残りの問いの数を出さない。帯、プロフィールカード、職歴カード、完成に「n/13」「n/3」の数字を出さない(task 288)。"""
    import re

    for form in ("C", "D"):
        _, turns = run(form)
        for said, out, ui, _ in turns:
            assert not re.search(r"あと\d+問", out["reply"]), out["reply"]
            b = C.build(ui)
            if b is None:
                continue
            shown = C.flex_text(b["flex"]) + "\n" + b["text"] + "\n" + b["flex"]["altText"]
            assert not re.search(r"\d+/\d+", shown), shown
    _, turns = run("D")
    replies = [out["reply"] for _, out, *_ in turns]
    assert replies[0] == H.START_LEAD_UI and any(H.PHASE_LINES["hard_filter"] in r for r in replies)


def test_no_score_rank_or_praise_in_the_parts():
    _, turns = run()
    shown = "\n".join(C.flex_text(b["flex"]) + "\n" + b["text"] + "\n" + b["flex"]["altText"]
                      for *_, ui, _ in turns if (b := C.build(ui)))
    for word in ("点", "位", "すごい", "えらい", "完璧", "ランク"):
        assert word not in shown


# ----------------------------- Flex の形 -----------------------------


def test_stamp_image_only_with_an_https_public_url(monkeypatch):
    c = {"n": 1, "title": "職歴1(直近)", "status": None, "done": 3, "of": 3, "complete": True,
         "rows": [{"label": x, "value": "a", "muted": False} for x in ("仕事", "規模", "結果")]}
    images = lambda b: [n for n in _nodes(b) if n.get("type") == "image"]
    assert images(C.career_card_bubble(c)) == [] and "完成" in C.flex_lines(C.career_card_bubble(c))
    monkeypatch.setenv(line_channel.PUBLIC_URL_ENV, "https://aica.example.test/")
    assert images(C.career_card_bubble(c))[0]["url"] == "https://aica.example.test/static/career/stamp.png"
    assert images(C.career_card_bubble(dict(c, complete=False))) == []


def test_start_card_links_the_resume_upload_only_when_liff_is_configured(monkeypatch):
    s = UI.start_payload(_d_start())["start"]
    labels = [b["action"]["label"] for b in C.start_flex(s)["contents"]["footer"]["contents"]]
    assert labels == [H.START_GO, H.LATER]
    monkeypatch.setenv(line_channel.LIFF_ID_ENV, "1234567890-abcdefgh")
    monkeypatch.setenv(line_channel.LIFF_CHANNEL_ENV, "1234567890")
    btns = C.start_flex(s)["contents"]["footer"]["contents"]
    assert [b["action"]["label"] for b in btns] == [H.START_GO, UI.RESUME_START, H.LATER]
    assert btns[1]["action"] == {"type": "uri", "label": UI.RESUME_START,
                                 "uri": "https://liff.line.me/1234567890-abcdefgh/resume/upload"}
    assert btns[0]["action"] == {"type": "message", "label": H.START_GO, "text": H.START_GO}


def test_complete_is_a_carousel_led_by_the_profile_card_with_the_blank_count():
    h, turns = run()
    ui = turns[-1][2]
    assert ui["kind"] == UI.COMPLETE
    flex = C.build(ui)["flex"]["contents"]
    assert flex["type"] == "carousel" and len(flex["contents"]) == 1 + len(ui["complete"]["cards"])
    first = C.flex_lines(flex["contents"][0])
    assert first[:3] == ["🌸", P.HEAD, UI.BLOOM[1]]
    blanks = h.result["blanks"]  # 結果の文の「空欄はNつ」と同じ数(職歴カードの下書きの空欄)
    assert ui["complete"]["blanks"] == blanks and (f"空欄は{blanks}つ" in turns[-1][1]["reply"]) is bool(blanks)
    assert (f"空欄{blanks}つ。あとから埋められます" in first) is bool(blanks)
    assert ui["complete"]["draft_url"] is None  # 下書きの画面の URL は session.turn が下書きを作った後に入れる
    assert C.flex_lines(flex["contents"][1])[0] == "職歴1(直近)"


def test_flex_text_is_what_the_simulated_person_reads():
    """評価の本人役は、Flex の中の字(ボタンのラベルを含む)を文で読む(fulltime-hearing.md §6.1)。"""
    s = UI.start_payload(_d_start())["start"]
    text = C.flex_text(C.start_flex(s))
    assert text.splitlines()[0] == "まずは1問から" and "始める" in text and UI.START_NOTE in text
    assert not any(ch.isdigit() for ch in text.replace("1問", ""))  # 問いの数も時間も出さない
    r = career_sim.run_one(PERSONAS[0], "D")
    assert any("[career_card] 職歴1(直近)" in line for line in r["transcript"])


def _d_start():
    h = H.new_state(form="D")
    H.start(h, H.BANK)
    return h


def _nodes(node):
    if isinstance(node, dict):
        return [node] + [n for v in node.values() for n in _nodes(v)]
    if isinstance(node, list):
        return [n for v in node for n in _nodes(v)]
    return []


# ----------------------------- LINE の Webhook を通して -----------------------------


def test_line_webhook_form_d_sends_the_start_card_career_cards_review_and_complete(tmp_path, monkeypatch):
    """CAREER_HARNESS_FORM=D: 最初の返信に「始める」の 1 枚、職歴カードの欄が埋まると職歴カード、まとめ、最後に完成の
    カルーセル。どの返信も吹き出し 1 つと Flex 0〜1 つ。まとめの「直す」で直した値が保存される。"""
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c1")
    monkeypatch.setenv(H.FORM_ENV, "D")
    from line_runtime.runtime import LineRuntime

    uid = line_user(RAW_USER)
    llm = FakeCareerLLM()
    model = ConsultModel(script=start_script(uid))
    server, fake = _server(tmp_path, monkeypatch, model)
    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=llm))
    st = server.state
    kinds, fixed = [], False
    with TestClient(server.app) as client:
        assert _say(client, "正社員で転職したいので相談したい", 1).status_code == 200
        for n in range(2, 45):
            msgs = fake.sent()[-1][1]["messages"]
            assert msgs[0]["type"] == "text" and len(msgs) <= 2
            flex = [m for m in msgs if m["type"] == "flex"]
            kinds.append(C.flex_lines(flex[0]["contents"])[0] if flex else None)
            qid = current_qid(st, uid)
            if qid is None:
                break
            quick = [i["action"]["text"] for i in msgs[-1].get("quickReply", {}).get("items", [])]
            if qid == "start":
                said = H.START_GO
            elif qid == "review" and not fixed:
                said, fixed = "勤務地を直す", True
            elif st.user(uid)[session.KEY]["harness"]["current"]["mode"] == "fix":
                said = "京都市内"
            else:
                said = answer_for(qid, quick)
            assert _say(client, said, n).status_code == 200
        else:
            raise AssertionError("result に着かない")
    assert kinds[0] == "まずは1問から"  # 「始める」の 1 枚
    assert "職歴1(直近)" in kinds and "まとめ" in kinds and kinds[-1] == "🌸"  # 完成の先頭はプロフィールカード(花の絵から)
    last = fake.sent()[-1][1]["messages"]
    assert last[-1]["contents"]["type"] == "carousel"
    stamp = [n for n in _nodes(last[-1]) if n.get("type") == "image"]
    assert stamp and stamp[0]["url"] == "https://aica.example.test/static/career/stamp.png"
    assert logic.stated_preferences(st.user(uid))["area"]["value"] == "京都市内"
    with TestClient(server.app) as client:
        r = client.get("/static/career/stamp.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert client.get("/static/career/other.png").status_code == 404


# ----------------------------- Web のトーク(talk-core.js) -----------------------------


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:  # noqa: BLE001  Chromium が入っていない環境
            pytest.skip(f"Chromium を起動できない: {e}")
        pg = b.new_page()
        pg.set_content('<div id="log"></div><form id="f"><input></form>')
        pg.add_script_tag(path=str(TALK_CORE))
        pg.evaluate("window.talk = createTalk({log: document.getElementById('log'), form: document.getElementById('f'),"
                    " getUser: () => 'u', isBusy: () => false, setBusy() {}, send(t) { window.sent = t; }, fire() {}})")
        yield pg
        b.close()


def test_web_talk_draws_the_career_card_as_text_and_the_fix_buttons_as_choices(page):
    h = H.new_state(form="D")
    mem = UI.fresh_mem(h)
    out = H.start(h, H.BANK)
    while h.current["mode"] != "review":
        ui, mem, out = step(h, mem, answer(h, out))
    built = C.build(ui)
    reply = {"bubbles": out["bubbles"], "quick_replies": out["choices"], "cards": [], "buttons": [], "actions": [],
             "progress": built}
    got = page.evaluate("(r) => { window.talk.renderReply(r, window.talk.botCol());"
                        " return {b: [...document.querySelectorAll('.bubble')].map(b => b.textContent),"
                        " q: [...document.querySelectorAll('.chips button')].map(b => b.textContent)}; }", reply)
    assert got["b"] == [f"{H.REVIEW_LEAD_UI}\n{built['text']}"] and "勤務地: " in got["b"][0]
    assert got["q"][:2] == [H.REVIEW_OK, H.LATER] and "勤務地を直す" in got["q"] and len(got["q"]) <= 13
    page.evaluate("() => [...document.querySelectorAll('.chips button')].find(b => b.textContent === '勤務地を直す').click()")
    assert page.evaluate("() => window.sent") == "勤務地を直す"
    card = C.build({"kind": UI.CAREER_CARD, "career_card": UI.career_card(h, 1)})
    reply = dict(reply, bubbles=["それで、何が変わりましたか?"], quick_replies=["パス"], progress=card)
    got = page.evaluate("(r) => { window.talk.renderReply(r, window.talk.botCol());"
                        " return [...document.querySelectorAll('.bubble')].map(b => b.textContent); }", reply)
    assert got[-1].splitlines()[1].startswith("【職歴1(直近)")  # 職歴カードは文にして最後の吹き出しに


def test_more_and_clarify_turns_add_no_flex():
    """形 D で「他の候補」と聞き返しのターンは、欄が埋まらないので Flex を付けない(数えない)。"""
    h = H.new_state(form="D")
    mem = UI.fresh_mem(h)
    H.start(h, H.BANK)
    for said in (H.START_GO, "同じ仕事で上へ", TEXT["ib_why_now"]):
        ui, mem, _ = step(h, mem, said)
    assert h.current["qid"] == "hf_area"
    turns = h.turns
    ui, mem, out = step(h, mem, H.MORE)
    assert ui is None and out["bubbles"] == [H.MORE_LEAD] and h.turns == turns
