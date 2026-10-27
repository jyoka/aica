"""アルバイト・パートの相談で、プロフィールが育つのを見せる UI(設計メモ docs/design/hearing-progress-ui.md、task 259)。実 LLM なし。

- ハーネスの側(dip_mcp/baito/progress.py): 何を数えるか、どのターンにプロフィールカードを付けるか(§4・§5)
- 共通の部品(line_runtime/progress_ui.py): どれを付けるか(§3.4)、Flex と文の形、字数と LINE の上限
- LINE への返信(line_channel.to_messages)と、Web のトーク(talk-core.js。ブラウザは Playwright があるときだけ)
トークのターンとして通しで動かすテストは tests/test_baito_chat.py にある。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dip_mcp.baito import harness as H
from dip_mcp.baito import progress as P
from line_runtime import progress_ui as U
from line_runtime.line_channel import to_messages
from tests.test_baito_harness import LLM, fresh

QM = H.qmap(H.BANK)
SEGMENT_CHOICE = {"student": "学生", "homemaker": "家のこと中心（主婦・主夫など）", "side_job": "本業あり副業探し",
                  "other": "フリーター・求職中・その他"}
TALK_CORE = Path(__file__).resolve().parents[1] / "line_runtime" / "static" / "talk-core.js"


def step(h, mem, text, llm=None):
    """session.turn と同じ順で、turn() の前後から progress を作る。"""
    prev = P.before(h)
    r = H.turn(h, text, H.BANK, llm or LLM())
    p, mem = P.update(h, mem, prev, r)
    return p, mem, r


def run(segment="student", age="18歳以上", limit=40):
    """ボタンだけで結果まで進める。1 ターンずつ (押した質問, 押した文, 後のフェーズ, progress, turn の結果)。"""
    h, mem, out = fresh(), None, []
    picks = {"q_segment": SEGMENT_CHOICE[segment], "hf_age": age}
    for _ in range(limit):
        if h.result:
            return h, out
        cur = h.current
        text = H.REVIEW_OK if cur["mode"] == "review" else picks.get(cur["qid"]) or cur["choices"][0]
        p, mem, r = step(h, mem, text)
        out.append((cur["qid"], text, h.phase, p, r))
    raise AssertionError("結果に着かない")


def counted_table(segment):
    return [s for s, p in H.SLOT_PRIORITY[segment].items() if p >= H.MID]


# ----------------------------- ハーネス: 何を数えるか(§4・§5) -----------------------------


def test_band_only_on_turns_where_something_was_filled():
    h, mem = fresh(), None
    p, mem, _ = step(h, mem, "推し活・趣味")                       # icebreak の間は出さない
    assert p is None and h.phase == "icebreak"
    p, mem, _ = step(h, mem, "学生")                                # hard_filter の最初のターン: 区分だけで、項目は埋まらない
    assert h.phase == "hard_filter" and p["filled"] == 1 and p["added"] == [] and U.choose(p) is None
    p, mem, _ = step(h, mem, "18歳以上")                            # 年齢が埋まった
    assert p["added"] == [{"label": "年齢", "value": "18歳以上", "inferred": False}]
    assert U.choose(p) == U.BAND and U.band_line(p) == "＋ 年齢: 18歳以上" and (p["filled"], p["total"]) == (2, 11)
    p, mem, r = step(h, mem, "うーん", LLM(extracts=["x", "x"]))    # 読めない答え: 聞き直す(何も埋まらない)
    assert r["counted"] is False and p["added"] == [] and U.choose(p) is None and U.build(p) is None


def test_profile_card_on_the_first_turn_after_hard_filter_and_nothing_on_review_turns():
    _, turns = run("student")
    cards = [(q, phase, p) for q, _, phase, p, _ in turns if U.choose(p) == U.CARD]
    assert len(cards) == 1  # 1 回の相談で 1 回
    qid, phase, p = cards[0]
    assert QM[qid]["phase"] == "hard_filter" and phase == "scenario_game" and p["milestone"]
    assert p["added"]  # 埋まった項目があっても、帯の代わりにカード
    built = U.build(p)
    assert built["kind"] == U.CARD and U.flex_text(built["flex"]).startswith("あなたのプロフィール")
    reviews = [p for _, _, phase, p, _ in turns if phase == "review"]
    assert reviews and all(p["review"] and U.choose(p) is None and U.build(p) is None for p in reviews)
    assert any(p["added"] for p in reviews)  # review で埋まった項目があっても、帯を付けない


def test_show_result_from_hard_filter_goes_to_review_without_the_profile_card():
    h, mem = fresh(), None
    for text in ("推し活・趣味", "学生", "18歳以上"):
        p, mem, _ = step(h, mem, text)
    assert h.phase == "hard_filter"
    p, mem, _ = step(h, mem, "もう結果を見せて", LLM(extracts=[{"slot_updates": [], "intent": "show_result"}]))
    assert h.phase == "review" and p["milestone"] is False and U.build(p) is None
    while not h.result:
        p, mem, _ = step(h, mem, H.REVIEW_OK)
    assert U.choose(p) == U.COMPLETE and not mem["card"]


def test_unknown_segment_is_counted_with_the_common_table_from_the_first_hard_filter_turn():
    h, mem = fresh(), None
    p, mem, _ = step(h, mem, "推し活・趣味")
    assert p is None
    p, mem, _ = step(h, mem, "ひみつ", LLM(extracts=[{"slot_updates": []}]))  # 区分を言わない: unknown のまま hard_filter へ
    assert h.segment == "unknown" and h.phase == "hard_filter"
    assert p["total"] == len(counted_table("unknown")) == 9 and p["filled"] == 1
    assert [g["name"] for g in p["groups"]] == ["条件", "働き方", "NG"]
    assert [i["label"] for i in p["groups"][0]["items"]] == ["年齢", "週の日数", "時間帯", "通勤"]


def test_segment_change_recounts_with_the_new_table():
    h, mem = fresh(), None
    for text in ("推し活・趣味", "学生", "18歳以上", "OK"):  # 学校の許可(学生の表だけの HARD)まで埋める
        p, mem, _ = step(h, mem, text)
    assert (p["filled"], p["total"]) == (3, 11)
    p, mem, _ = step(h, mem, "実は会社員で副業を探してる", LLM(extracts=[{"slot_updates": [], "segment": "side_job"}]))
    assert h.current["mode"] == "confirm" and (p["filled"], p["total"]) == (3, 11)  # 確かめるまでは今の表
    p, mem, _ = step(h, mem, "本業あり副業探し")
    assert h.segment == "side_job" and "school_permission" in h.archived["student"]
    # 新しい表で数え直す: 学校の許可は新しい表に無いので数えない(このときだけ分子が減る)。分母は副業の表
    assert (p["filled"], p["total"]) == (2, len(counted_table("side_job"))) == (2, 12)
    assert "school_permission" not in mem["seen"]


def test_filled_never_goes_down_and_review_undecided_items_stay_counted():
    """区分を変えないかぎり分子は減らない。review の後に「未定」になった項目(矛盾を解けなかった接客)も埋まったまま数える。"""
    h, turns = run("student")
    counts = [p["filled"] for *_, p, _ in turns if p]
    assert counts == sorted(counts) and counts[-1] == turns[-1][3]["total"] == 11
    assert "customer_facing" in h.unresolved and "customer_facing" not in h.slots  # 結果の前に「未定」にした
    item = next(i for g in turns[-1][3]["groups"] for i in g["items"] if i.get("label") == "接客")
    assert item == {"label": "接客", "value": None, "inferred": False, "undecided": True}
    assert "接客 未定" in U.flex_text(U.build(turns[-1][3])["flex"])


def test_guess_from_a_scenario_choice_gets_a_question_mark_and_a_direct_answer_does_not():
    _, turns = run("student")
    sc = next(p for q, *_, p, _ in turns if q == "sc_line")
    assert sc["added"][0] == {"label": "接客", "value": "ふつう（できる）", "inferred": True}
    assert U.band_line(sc) == "＋ 接客: ふつう（できる）?"
    card = next(p for *_, p, _ in turns if p and p["milestone"])
    purpose = next(i for i in card["groups"][1]["items"] if i["label"] == "目的")
    assert purpose["value"] == "推し活・趣味" and purpose["inferred"] is False  # 最初の質問のボタン(本人が選んだ)は「?」なし
    assert "推し活・趣味?" not in U.card_text(card)


def test_profile_card_shows_user_ng_and_rule_ng_with_the_rule_tail():
    h = fresh()
    h.user_ng.append("立ち仕事")
    ng = P.groups(h, set())[-1]
    assert ng == {"name": "NG", "items": [{"value": "立ち仕事"}, {"value": "深夜シフト（ルール）"},
                                          {"value": "重量物・危険な作業（ルール）"}]}
    p = {"filled": 0, "total": 9, "groups": P.groups(h, set())}
    assert "立ち仕事 / 深夜シフト（ルール）" in U.flex_text(U.card_flex(p))


def test_values_are_the_harness_buckets_and_labels():
    """見本の「時給1100円」「体を使う」ではなく、実際の区切り(「1,000円台でOK」)と表示名(「体力面」)で出す。"""
    _, turns = run("other")
    card = next(p for *_, p, _ in turns if p and p["milestone"])
    todo = [i["label"] for g in card["groups"] for i in g["items"] if i.get("value") is None]
    assert "体力面" in todo and "時給の下限" in todo
    final = turns[-1][3]
    assert "1,000円台でOK" in [i["value"] for i in final["groups"][1]["items"]]


def test_complete_turn_has_the_profile_and_the_recommended_job_types_and_keeps_the_result_bubble():
    h, turns = run("student")
    _, _, phase, p, r = turns[-1]
    assert r["done"] and phase == "result" and U.choose(p) == U.COMPLETE
    recs = h.result["recommendations"]
    assert p["jobs"] == [{"name": H.CATALOG_BY_ID[x["job_id"]]["name"], "reason": x["reason"]} for x in recs]
    assert len(p["jobs"]) == 3
    built = U.build(p)
    text = U.flex_text(built["flex"])
    assert text.index("あなたのプロフィール") < text.index("おすすめの仕事")
    for j in p["jobs"]:
        assert j["name"] in text
    assert built["flex"]["contents"]["type"] == "bubble"  # 1 つの bubble
    assert r["reply"] == h.result["text"] and "1. " + p["jobs"][0]["name"] in r["reply"]  # 結果の吹き出しは今のまま


def test_restart_and_a_consultation_started_before_this_ui():
    h, mem = fresh(), None
    for text in ("推し活・趣味", "学生", "18歳以上", "OK"):
        p, mem, _ = step(h, mem, text)
    assert p["filled"] == 3
    # この UI の前に始めた相談(覚えが無い): 前から埋まっていた項目は、このターンに埋まったことにしない
    p, mem2, _ = step(h, None, QM["hf_days"]["choices"][0])
    assert [a["label"] for a in p["added"]] == ["週の日数"] and p["filled"] == 4
    # 「最初から」: 数え直す(icebreak に戻るので出さない)
    p, mem2, _ = step(h, mem2, "最初からやり直したい", LLM(extracts=[{"slot_updates": [], "intent": "restart"}]))
    assert h.restarts == 1 and p is None and mem2["seen"] == [] and mem2["card"] is False


def test_blocked_junior_high_and_crisis_turns_have_no_progress():
    h, mem = fresh(), None
    for text in ("推し活・趣味", "学生"):
        p, mem, _ = step(h, mem, text)
    p, mem, r = step(h, mem, "中学生以下")
    assert h.blocked == "under_15" and r["done"] and p is None
    h, mem = fresh(), None
    for text in ("推し活・趣味", "学生"):
        p, mem, _ = step(h, mem, text)
    p, mem, r = step(h, mem, "もう消えたい")
    assert r["view"].get("crisis") and p is None


# ----------------------------- 部品: 字数と LINE の上限 -----------------------------


@pytest.mark.parametrize("segment", sorted(SEGMENT_CHOICE))
@pytest.mark.parametrize("age", ["18歳以上", "15〜17歳"])
def test_every_flex_fits_line_limits_and_the_design_lengths(segment, age):
    _, turns = run(segment, age)
    kinds = []
    for _, _, _, p, r in turns:
        built = U.build(p)
        if not built:
            continue
        kinds.append(built["kind"])
        flex = built["flex"]
        assert flex["type"] == "flex" and 0 < len(flex["altText"]) <= 400
        if built["kind"] == U.BAND:
            assert len(U.band_line(p)) <= U.BAND_MAX and flex["contents"]["size"] == U.BAND_SIZE
            item = U.band_line(p).split(" ほか")[0]
            assert len(item) <= U.BAND_ITEM_MAX
        elif built["kind"] == U.CARD:
            assert U.flex_chars(flex) <= U.CARD_MAX
        else:
            assert U.flex_chars(flex) <= U.COMPLETE_MAX
        msgs = to_messages({"bubbles": [r["reply"]], "quick_replies": r["view"].get("choices", []), "progress": built},
                           base_url="", features=())
        assert len(msgs) == 2 and msgs[0]["type"] == "text"  # 吹き出し 1 つと Flex 1 つ
        assert {k: v for k, v in msgs[1].items() if k != "quickReply"} == flex and "quickReply" not in flex
        items = msgs[-1].get("quickReply", {}).get("items", [])
        assert len(items) <= 13 and all(len(i["action"]["label"]) <= 20 for i in items)
    assert kinds.count(U.CARD) == 1 and kinds[-1] == U.COMPLETE and U.BAND in kinds


def _progress(**kw):
    long = "とても長い値がここに入ってくるときの表示"
    groups = [{"name": "条件", "items": [{"label": f"項目{i}", "value": long, "inferred": i % 2 == 0, "undecided": False}
                                       for i in range(6)] + [{"label": "未回答", "value": None, "inferred": False,
                                                             "undecided": False}]},
              {"name": "働き方", "items": [{"label": f"好み{i}", "value": None, "inferred": False, "undecided": i == 0}
                                         for i in range(7)]},
              {"name": "NG", "items": [{"value": "深夜シフト（ルール）"}, {"value": "重量物・危険な作業（ルール）"}]}]
    return {"filled": 7, "total": 14, "added": [{"label": "時給と働きやすさ", "value": long, "inferred": True}] * 12,
            "groups": groups, "milestone": False, "review": False, "complete": False, "jobs": []} | kw


def test_long_values_are_clipped_to_the_design_lengths():
    p = _progress()
    assert U.band_line(p) == "＋ 時給と働きやすさ: とても長い…? ほか11" and len(U.band_line(p)) == U.BAND_MAX  # 「?」は残す
    card = U.card_flex(p)
    assert U.flex_chars(card) <= U.CARD_MAX
    todo = U.flex_texts(card)[-1]
    assert todo.startswith("□ 未回答") and "ほか" in todo  # 「まだ」を減らして「ほか N」
    jobs = [{"name": "コンビニスタッフ", "reason": "時間帯と接客の条件に合いそうで、近所で探しやすいから"}] * 3
    done = U.complete_flex(_progress(complete=True, jobs=jobs))
    assert U.flex_chars(done) <= U.COMPLETE_MAX and "コンビニスタッフ" in U.flex_text(done)
    for flex in (U.band_flex(p), card, done):
        assert len(flex["altText"]) <= 400
        json.dumps(flex, ensure_ascii=False)


def test_no_score_rank_or_praise_in_the_parts():
    _, turns = run("student")
    shown = "\n".join(U.flex_text(b["flex"]) + "\n" + b["text"] + "\n" + b["flex"]["altText"]
                      for *_, p, _ in turns if (b := U.build(p)))
    for word in ("点", "位", "すごい", "えらい", "完璧", "ランク"):
        assert word not in shown


# ----------------------------- Flex を切った形と、帯を切った形 -----------------------------


def test_text_mode_folds_the_band_into_the_last_line_of_the_bubble_and_the_card_into_short_text(monkeypatch):
    _, turns = run("student")
    band = next(p for *_, p, _ in turns if U.choose(p) == U.BAND)
    card = next(p for *_, p, _ in turns if U.choose(p) == U.CARD)
    monkeypatch.setenv(U.MODE_ENV, "text")
    built = U.build(band)
    assert built["flex"] is None and built["text"] == f"（{band['filled']}/{band['total']} ＋{band['added'][0]['label']}）"
    msgs = to_messages({"bubbles": ["次の質問だよ"], "quick_replies": ["はい"], "progress": built}, base_url="", features=())
    assert [m["type"] for m in msgs] == ["text"] and msgs[0]["text"].splitlines() == ["次の質問だよ", built["text"]]
    built = U.build(card)
    assert built["flex"] is None and built["text"].startswith("【あなたのプロフィール ")
    assert "条件：" in built["text"] and "まだ：□ " in built["text"]
    msgs = to_messages({"bubbles": ["次の質問だよ"], "progress": built}, base_url="", features=())
    assert len(msgs) == 1 and msgs[0]["text"].endswith(built["text"])


def test_no_band_mode_drops_only_the_band(monkeypatch):
    _, turns = run("student")
    band = next(p for *_, p, _ in turns if U.choose(p) == U.BAND)
    card = next(p for *_, p, _ in turns if U.choose(p) == U.CARD)
    monkeypatch.setenv(U.MODE_ENV, "no_band")
    assert U.build(band) is None and U.build(card)["flex"]["type"] == "flex"
    monkeypatch.setenv(U.MODE_ENV, "unknown-value")  # 知らない値なら既定(Flex あり)
    assert U.build(band)["flex"]["contents"]["size"] == U.BAND_SIZE


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
                    " getUser: () => 'u', isBusy: () => false, setBusy() {}, send() {}, fire() {}})")
        yield pg
        b.close()


def test_web_talk_draws_the_text_form_as_the_last_line_of_the_bubble(page):
    reply = {"bubbles": ["週に何日くらい入りたい？"], "quick_replies": ["週1〜2日"], "cards": [], "buttons": [], "actions": [],
             "progress": {"kind": "band", "flex": {"type": "flex"}, "text": "（2/11 ＋年齢）"}}
    texts = page.evaluate("(r) => { window.talk.renderReply(r, window.talk.botCol());"
                          " return [...document.querySelectorAll('.bubble')].map(b => b.textContent); }", reply)
    assert texts == ["週に何日くらい入りたい？\n（2/11 ＋年齢）"]  # Flex は描かない
    no_progress = dict(reply, progress=None, bubbles=["ふつうの返信"])
    texts = page.evaluate("(r) => { window.talk.renderReply(r, window.talk.botCol());"
                          " return [...document.querySelectorAll('.bubble')].map(b => b.textContent); }", no_progress)
    assert texts[-1] == "ふつうの返信"
