"""アルバイトの相談の「近くで探す」と本物の求人(設計メモ docs/design/baito-quick-start.md §6・§7、task 266)。実 API なし。

- ランタイムを通して(偽のモデル、dip の MCP は本物): 入口から絵 → [近くで探す] → 年齢 → エリア → 時間帯(いくつでも押して
  [これで探す]。task 287)で、dip の recommend が返した本物の求人(選んだ時間帯のどれかに合う求人)が、今の求人のカルーセル
  (line_channel._cards_message)と件数の吹き出しで出る
- 時間帯は押すたびに足し、[これで探す] で決める。[どれでもOK] は出しているものを全部。打った「昼 夕方 夜全部OK」も読む。
  前の相談(near の time が 1 つの文字)も読める
- 未成年(15〜17 歳)と年齢不明(ひみつ)には「夜」「深夜」のボタンが出ず、求人の検索は必ず未成年として呼ぶ(未成年の決まりに
  当たる求人が出ない)。中学生以下には求人を出さない(そこで止める)
- 0 件のときは relax の 1 つ(いちばん件数の増える条件)と、それを外して探すボタンが出る
- [もっと絞る] の後は今の流れ(区分、残りの HARD、MID)に入り、入った値(年齢・時間帯)は聞き直さない。[ちがう] は入口のカード
- 求人の後のボタンでない発言(カルーセルの「詳しく見る」)は、相談を閉じてトークの AI に渡す。年齢の訂正、闇バイトの言葉、
  窓口の案内の後の「続ける」はハーネスが受ける。検索が失敗しても、時間帯の問いに戻し続けない
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from dip_mcp import logic
from dip_mcp.baito import harness as H
from dip_mcp.baito import quick_start as Q
from dip_mcp.baito import session
from dip_mcp.data import State
from line_runtime.line_channel import _cards_message, to_messages
from tests.test_baito_chat import GREET, START, UID, FakeBaitoLLM, _harness
from tests.test_consult import ConsultModel

QM = H.qmap(H.BANK)
AGES = QM["hf_age"]["choices"]
ALL_TIMES = list(Q.TIME_BUTTONS)
MINOR_TIMES = ["朝", "昼", "夕方"]
DONE = Q.DONE_BUTTON
TRACK = "アルバイト・パート"


@pytest.fixture(autouse=True)
def harness_on(monkeypatch):
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")


@pytest.fixture
def state(tmp_path):
    s = State(tmp_path / "state.json")
    s.reset()
    return s


class Quiet:
    """ハーネスの llm(同期)。言い方は「いいね！」と問いの原文。抽出は呼ばれたら落とす(3 問と求人ではモデルに読ませない)。"""

    def extract(self, payload):
        raise AssertionError("「近くで探す」では、抽出のモデルを呼ばない")

    def generate(self, v):
        return json.dumps({"reply": "いいね！" + v["text"]}, ensure_ascii=False)


def tap(state: State, said: str) -> dict:
    """session.turn を、言い方のモデルの出力(「いいね！」+ 問いの原文)を渡しながら最後まで進める。"""
    r, outs = session.turn(state, UID, said), []
    while r.get("status") == "llm":
        assert r["call"]["stage"] == "say", r["call"]["stage"]  # 抽出は呼ばない
        text = json.loads(r["call"]["system"].split("次に聞くこと（JSON）: ", 1)[1].split("\n", 1)[0])["text"]
        outs.append({"stage": "say", "output": {"reply": "いいね！" + text}})
        r = session.turn(state, UID, said, outs)
    return r


def walk(state: State, *said: str) -> list[dict]:
    session.start(state, UID)
    return [tap(state, s) for s in said]


def jobs_of(state: State, **cond) -> dict:
    return logic.recommend(state, UID, employment_type=TRACK, **cond)


# ----------------------------- ランタイムを通して(偽のモデル) -----------------------------


def test_taps_from_the_entry_card_show_real_jobs_for_every_picked_time(tmp_path):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    baito = FakeBaitoLLM()
    taps = ["座ってじっくり", H.QUICK_NEAR, "18歳以上", "東京", "昼", "夕方", DONE]

    async def go():
        async with LineRuntime(model=ConsultModel(script=[GREET, *START]), in_memory=True, baito_llm=baito) as rt:
            out = [await rt.handle(UID, "visit", None), await rt.handle(UID, "message", "まず相談する")]
            out += [await rt.handle(UID, "message", t) for t in taps]
            return out, await rt.conversation(UID)

    (_, entry, r1, r2, r3, r4, p1, p2, r5), conv = asyncio.run(go())
    assert entry["baito_card"]["kind"] == "entry"
    # タップ 2〜4: 年齢 → エリア → 時間帯を、ボタン(クイックリプライ)で聞く。モデルはリアクションの前置きだけ
    assert r2["baito"] == {"phase": H.NEAR_AGE, "question": H.NEAR_AGE} and r2["quick_replies"] == AGES
    assert r2["bubbles"] == ["いいね！" + H.NEAR_TEXT[H.NEAR_AGE]]
    assert r3["baito"]["phase"] == H.NEAR_AREA and r3["quick_replies"] == ["東京", "神奈川", "埼玉", "どこでも"]
    assert r4["baito"]["phase"] == H.NEAR_TIME and r4["quick_replies"] == [*ALL_TIMES, Q.ANY_BUTTON]  # 18歳以上には夜・深夜も
    assert r4["bubbles"] == ["いいね！いつ働けそう？（いくつでも選べるよ）"]
    # 時間帯は押すたびに足す(モデルは呼ばない)。選んだものはボタンから消え、[これで探す] が先頭に出る
    assert p1["baito"]["phase"] == H.NEAR_TIME and p1["bubbles"] == ["昼ね！ほかにもあればどうぞ。決まったら「これで探す」を押してね"]
    assert p1["quick_replies"] == [DONE, "朝", "夕方", "夜", "深夜", Q.ANY_BUTTON, Q.RESET_BUTTON]
    assert p2["bubbles"][0].startswith("昼・夕方ね！") and p2["quick_replies"] == [DONE, "朝", "夜", "深夜", Q.ANY_BUTTON,
                                                                              Q.RESET_BUTTON]
    assert all("progress" not in r and r["cards"] == [] for r in (r1, r2, r3, r4, p1, p2))
    # [これで探す]: dip の recommend が返した本物の求人(エリア、§6.2 で直した時間帯のどれか、絵の値、未成年でない)
    want = jobs_of(State(), area="東京", time_band=["日中", "夕方"], max_physical_level=1, minor=False)
    day = jobs_of(State(), area="東京", time_band="日中", max_physical_level=1, minor=False)
    evening = jobs_of(State(), area="東京", time_band="夕方", max_physical_level=1, minor=False)
    assert want["total"] > max(day["total"], evening["total"]) and r5["cards"] == want["jobs"]
    assert r5["bubbles"] == [f"昼・夕方・東京で、座ってできる仕事は {want['total']} 件！"]
    assert r5["baito"] == {"phase": H.NEAR_JOBS, "question": H.NEAR_JOBS}
    assert r5["quick_replies"] == [H.NEAR_MORE, H.NEAR_WRONG] and "progress" not in r5 and "baito_card" not in r5
    # モデルは言い方だけ(職種の結果と 3 問の 4 回)。抽出は呼ばず、求人のターンも呼ばない
    assert [c[0] for c in baito.calls] == ["say"] * 4
    # LINE: 吹き出しと、今の求人のカルーセル(ボタンはクイックリプライ)。Web のトークは同じ cards を求人カードで出す
    msgs = to_messages(r5, base_url="https://aica.example", features=())
    assert [m["type"] for m in msgs] == ["text", "flex"] and msgs[1]["contents"]["type"] == "carousel"
    assert {k: v for k, v in msgs[1].items() if k != "quickReply"} == _cards_message(want["jobs"])
    assert [i["action"]["text"] for i in msgs[-1]["quickReply"]["items"]] == [H.NEAR_MORE, H.NEAR_WRONG]
    # 入口のタップは 15 ターンに数えない。答えは確信度 high。時間帯は選んだものすべて
    h = _harness()
    assert h.turns == 0 and h.near == {"pick": "座ってじっくり", "age": "18_plus", "area": "東京", "time": ["昼", "夕方"]}
    assert h.slots["age_band"] == {"value": "18_plus", "confidence": "high", "from": "choice:near_age"}
    assert h.slots["time_slots"] == {"value": ["daytime", "evening"], "confidence": "high", "from": "choice:near_time"}
    # 画面の描き直しにも、同じ求人が残る
    assert conv["turns"][-1]["bot"]["cards"] == want["jobs"]


def test_more_then_continues_the_harness_flow_without_asking_entered_values_again(tmp_path):
    """[もっと絞る] で今の流れに入り、帯が出始める。年齢と時間帯は聞かない(区分、残りの HARD、MID を聞く)。"""
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    picks = {H.QUICK_PICK: "動いてたい", H.NEAR_AGE: "15〜17歳", H.NEAR_AREA: "東京", H.NEAR_TIME: "夕方だけ",
             H.NEAR_JOBS: H.NEAR_MORE, "q_segment": "学生"}

    async def go():
        async with LineRuntime(model=ConsultModel(script=[GREET, *START]), in_memory=True, baito_llm=FakeBaitoLLM()) as rt:
            await rt.handle(UID, "visit", None)
            r = await rt.handle(UID, "message", "まず相談する")
            out = []
            for _ in range(30):
                q = (r.get("baito") or {}).get("question") or H.QUICK_PICK
                if q == "result":
                    return out
                r = await rt.handle(UID, "message", H.REVIEW_OK if q == "review" else picks.get(q) or r["quick_replies"][0])
                out.append(r)
            raise AssertionError("結果に着かない")

    out = asyncio.run(go())
    phases = [r["baito"]["question"] for r in out[:-1]]
    more = phases.index(H.NEAR_JOBS) + 1
    assert out[more]["baito"] == {"phase": "icebreak", "question": "q_money"} and "progress" in out[more]
    asked = phases[more:]
    assert "hf_age" not in asked and "hf_time" not in asked and "q_segment" in asked
    assert {"hf_days", "hf_commute", "hf_school"} <= set(asked)  # 残りの HARD(週の日数、通勤、学生の HARD)は聞く
    assert out[-1]["baito"]["phase"] == "result" and "1. " in out[-1]["bubbles"][0]


# ----------------------------- 時間帯の複数選択(task 287) -----------------------------


def _to_time(age="18歳以上", search=None):
    st, search = H.new_state(quick=True), search or _Found()
    for said in ("座ってじっくり", H.QUICK_NEAR, age, "東京"):
        H.turn(st, said, H.BANK, Quiet(), search=search)
    assert st.phase == H.NEAR_TIME
    return st, search


def test_time_buttons_add_up_until_the_done_button_and_reset_clears_them():
    st, search = _to_time()
    r = H.turn(st, "夜", H.BANK, Quiet(), search=search)
    r = H.turn(st, "朝", H.BANK, Quiet(), search=search)
    # 選んだものはボタンの並びで言い、ボタンから消す。[これで探す] が先頭、[選び直す] が最後。モデルは呼ばない
    assert r["reply"] == "朝・夜ね！ほかにもあればどうぞ。決まったら「これで探す」を押してね" and search.calls == []
    assert r["view"]["choices"] == [DONE, "昼", "夕方", "深夜", Q.ANY_BUTTON, Q.RESET_BUTTON]
    assert st.near["time_pick"] == ["朝", "夜"] and "time_slots" not in st.slots and st.turns == 0
    r = H.turn(st, "夜", H.BANK, Quiet(), search=search)  # もう選んだものを打っても、そのまま(重ねない)
    assert st.near["time_pick"] == ["朝", "夜"] and st.phase == H.NEAR_TIME
    r = H.turn(st, "ランチの時間", H.BANK, Quiet(), search=search)  # 読めない答えは聞き直す(選んだものは残す)
    assert r["reply"] == H.QUICK_TAP_TEXT and r["view"]["choices"][0] == DONE and st.near["time_pick"] == ["朝", "夜"]
    r = H.turn(st, Q.RESET_BUTTON, H.BANK, Quiet(), search=search)
    assert r["reply"] == H.NEAR_TIME_RESET_TEXT and r["view"]["choices"] == [*ALL_TIMES, Q.ANY_BUTTON]
    assert "time_pick" not in st.near
    r = H.turn(st, DONE, H.BANK, Quiet(), search=search)  # 何も選んでいないときの「これで探す」は読まない
    assert r["reply"] == H.QUICK_TAP_TEXT and search.calls == []
    for said in ("昼", "夕方", DONE):
        r = H.turn(st, said, H.BANK, Quiet(), search=search)
    assert st.phase == H.NEAR_JOBS and st.near["time"] == ["昼", "夕方"] and "time_pick" not in st.near
    assert st.slots["time_slots"] == {"value": ["daytime", "evening"], "confidence": "high", "from": "choice:near_time"}
    assert search.calls == [{"area": "東京", "time_band": ["日中", "夕方"], "max_physical_level": 1, logic.MINOR_ARG: False}]
    assert r["reply"] == "昼・夕方・東京で、座ってできる仕事は 1 件！"


@pytest.mark.parametrize("age, said, times", [
    ("18歳以上", Q.ANY_BUTTON, ALL_TIMES),
    ("18歳以上", "全部OK", ALL_TIMES),
    ("15〜17歳", Q.ANY_BUTTON, MINOR_TIMES),  # 未成年の「どれでもOK」は出しているボタン(朝・昼・夕方)だけ
    ("18歳以上", "昼 夕方 夜全部OK", ["昼", "夕方", "夜"]),  # 利用者の言い方(2026-10-27)
    ("18歳以上", "昼と夕方", ["昼", "夕方"]),
    ("18歳以上", "昼、夕方、深夜", ["昼", "夕方", "深夜"]),
    ("15〜17歳", "夕方だけ", ["夕方"]),
])
def test_typed_or_any_answers_decide_the_times_at_once(age, said, times):
    st, search = _to_time(age)
    r = H.turn(st, said, H.BANK, Quiet(), search=search)
    assert st.phase == H.NEAR_JOBS and st.near["time"] == times
    assert search.calls[-1]["time_band"] == Q.search_time_bands(times)
    if times in (ALL_TIMES, MINOR_TIMES):  # 全部なら、吹き出しで時間帯を言わない
        assert r["reply"] == "東京で、座ってできる仕事は 1 件！"


def test_picking_every_shown_time_decides_without_the_done_button():
    st, search = _to_time("15〜17歳")
    for said in MINOR_TIMES:
        H.turn(st, said, H.BANK, Quiet(), search=search)
    assert st.phase == H.NEAR_JOBS and st.near["time"] == MINOR_TIMES and len(search.calls) == 1


def test_a_picked_time_is_added_to_a_later_typed_answer_unless_it_says_only():
    st, search = _to_time()
    H.turn(st, "朝", H.BANK, Quiet(), search=search)
    H.turn(st, "夕方と夜", H.BANK, Quiet(), search=search)
    assert st.near["time"] == ["朝", "夕方", "夜"] and search.calls[-1]["time_band"] == ["早朝", "夕方", "夜"]
    st, search = _to_time()
    H.turn(st, "朝", H.BANK, Quiet(), search=search)
    H.turn(st, "夕方だけ", H.BANK, Quiet(), search=search)  # 「だけ」は前に押したものと入れ替える
    assert st.near["time"] == ["夕方"] and search.calls[-1]["time_band"] == ["夕方"]


@pytest.mark.parametrize("said", ["ランチの時間", "夕方以降", "朝から夕方まで", "朝〜夕方", "朝~夜", "朝～夕方", "夕方〜夜", "全部ダメ", "夜は無理", "夜以外", "土日の夜", ""])
def test_read_times_does_not_guess(said):
    assert H.read_times(said, ALL_TIMES) is None


def test_a_typed_range_of_times_is_asked_again_not_read_as_its_two_ends():
    """「朝〜夕方」を「朝」と「夕方」だけと読むと、間の「昼」の求人が出ず、プロフィールにも入らない。読まずに聞き直す。"""
    st, search = _to_time()
    r = H.turn(st, "朝〜夕方", H.BANK, Quiet(), search=search)
    assert st.phase == H.NEAR_TIME and r["reply"] == H.QUICK_TAP_TEXT and search.calls == []
    assert "time" not in st.near and "time_slots" not in st.slots and r["view"]["choices"] == [*ALL_TIMES, Q.ANY_BUTTON]


def test_a_conversation_saved_with_one_time_still_works():
    """task 287 より前の相談(near の time が 1 つの文字)も、検索・吹き出し・未成年の聞き直しで読める。"""
    st = H.State.from_dict({**H.new_state(quick=True).to_dict(), "phase": H.NEAR_JOBS,
                            "near": {"pick": "しゃべるの好き", "age": "18_plus", "area": "神奈川", "time": "夜"}})
    st.slots["age_band"] = {"value": "18_plus", "confidence": "high", "from": "choice:near_age"}
    st.current = {"qid": H.NEAR_JOBS, "mode": H.NEAR_JOBS, "choices": [H.NEAR_MORE, H.NEAR_WRONG], "text": ""}
    assert H.near_times(st) == ["夜"] and H.near_search(st)["time_band"] == ["夜"]
    search = _Found(total=2)
    H.turn(st, H.NEAR_WRONG, H.BANK, Quiet(), search=search)
    H.turn(st, "ひとりで黙々", H.BANK, Quiet(), search=search)
    r = H.turn(st, H.QUICK_NEAR, H.BANK, Quiet(), search=search)  # 聞き直さずに求人
    assert r["reply"] == "夜・神奈川で、黙々とできる仕事は 2 件！" and search.calls[-1]["time_band"] == ["夜"]
    r = H.turn(st, "16歳", H.BANK, Quiet(), search=search)  # 未成年と分かったら、夜の答えは使わず聞き直す
    assert st.phase == H.NEAR_TIME and "time" not in st.near and r["view"]["choices"] == [*MINOR_TIMES, Q.ANY_BUTTON]


def test_the_time_question_fits_the_line_quick_reply_limits(state):
    *_, r = walk(state, "動いてたい", H.QUICK_NEAR, "18歳以上", "どこでも", "昼")
    msgs = to_messages({"bubbles": [r["reply"]], "quick_replies": r["choices"]}, base_url="https://aica.example", features=())
    items = msgs[-1]["quickReply"]["items"]
    assert [i["action"]["text"] for i in items] == r["choices"] and len(items) <= 13
    assert all(0 < len(i["action"]["label"]) <= 20 for i in items)


# ----------------------------- 未成年と中学生 -----------------------------


@pytest.mark.parametrize("age", ["15〜17歳", "ひみつ"])
def test_minors_and_unknown_age_get_no_night_buttons_and_only_jobs_minors_may_take(state, age):
    for pick in [*H.QUICK_PICKS, H.QUICK_NONE]:
        for area in H.NEAR_AREAS:
            state.reset()
            *_, ask_time = walk(state, pick, H.QUICK_NEAR, age, area)
            assert ask_time["choices"] == [*MINOR_TIMES, Q.ANY_BUTTON], (pick, area)
            r = tap(state, Q.ANY_BUTTON)  # 未成年の「どれでもOK」は朝・昼・夕方
            assert r["phase"] == H.NEAR_JOBS
            for c in r["jobs"]:
                assert logic.minor_ok(logic.load_jobs()[c["job_id"]]), (pick, area, c["job_id"])
    # 打っても夜・深夜は読まない(ボタンに無い)
    state.reset()
    walk(state, "ひとりで黙々", H.QUICK_NEAR, age, "どこでも")
    for night in ("夜", "深夜", "夕方と夜", "昼 深夜"):
        r = tap(state, night)
        assert r["phase"] == H.NEAR_TIME and r["reply"] == H.QUICK_TAP_TEXT and "jobs" not in r


def test_the_search_is_always_called_as_minor_unless_the_age_is_18_or_more():
    for age, minor in (("15〜17歳", True), ("ひみつ", True), ("18歳以上", False)):
        st, search = H.new_state(quick=True), _Found()
        for said in ("しゃべるの好き", H.QUICK_NEAR, age, "埼玉", "夕方", DONE):
            H.turn(st, said, H.BANK, Quiet(), search=search)
        assert search.calls == [{"area": "埼玉", "time_band": ["夕方"], "min_talk_level": 3, logic.MINOR_ARG: minor}], age
    st = H.new_state(quick=True)  # 年齢が分からない状態の条件(念のため。3 問を通らずに条件を作っても未成年)
    st.near = {"pick": H.QUICK_NONE, "area": "どこでも", "time": ["朝"]}
    assert H.near_search(st) == {"time_band": ["早朝"], logic.MINOR_ARG: True}


def test_minor_jobs_from_the_real_search_never_break_the_minor_rules(state):
    """未成年の決まり(深夜の帯、22 時より後・5 時より前、時刻の読めない shift、重い作業)に当たる求人が、3 問の答えの
    どの組でも出ない。未成年でない人なら出る求人があることも見る(決まりが効いているから外れている)。"""
    bad = {k for k, j in logic.load_jobs().items() if j.employment_type == TRACK and not logic.minor_ok(j)}
    shown_minor, shown_adult = set(), set()
    for t in MINOR_TIMES:
        for area in H.NEAR_AREAS:
            for pick in [*H.QUICK_PICKS, H.QUICK_NONE]:
                st = H.new_state(quick=True)
                st.near = {"pick": pick, "area": area, "time": [t]}
                for minor, shown in ((True, shown_minor), (False, shown_adult)):
                    cond = H.near_search(st) | {logic.MINOR_ARG: minor}
                    found = session.near_search(state, UID, cond)
                    shown |= {c["job_id"] for c in found["jobs"]}
    assert shown_minor and not shown_minor & bad
    assert shown_adult & bad  # 同じ条件で成人には出る求人を、未成年には出していない


@pytest.mark.parametrize("said", ["中学生以下", "14歳"])
def test_junior_high_students_stop_at_the_age_and_get_no_jobs(state, said):
    *_, r = walk(state, "しゃべるの好き", H.QUICK_NEAR, said)
    assert r["status"] == "result" and r["blocked"] == "under_15" and r["choices"] == [] and "jobs" not in r
    assert r["reply"] == H.NOTICE["under_15"] + "\n" + H.NOTICE["rules_only"]
    # 止めた後もハーネスが受ける(今の流れの結果の後と同じ。求人に進まない)
    empty = {"slot_updates": [], "ng_updates": [], "ng_removals": [], "segment": None, "intent": "answer", "review_ack": None}
    r = session.turn(state, UID, "東京", [{"stage": "extract", "output": empty}])
    assert r["reply"].startswith(H.POST_RESULT_TEXT) and "jobs" not in r


def test_a_typed_junior_high_answer_is_asked_again_and_never_reaches_jobs():
    st, search = H.new_state(quick=True), _Found()
    for said in ("座ってじっくり", H.QUICK_NEAR, "中2です"):
        r = H.turn(st, said, H.BANK, Quiet(), search=search)
    assert st.phase == H.NEAR_AGE and r["reply"] == H.QUICK_TAP_TEXT and r["view"]["choices"] == AGES
    assert "age_jhs" in st.flags and H.jhs_unresolved(st) and search.calls == []
    H.turn(st, "16歳", H.BANK, Quiet(), search=search)  # はっきりした年齢(task 258 の typed_age)は読む
    assert st.slots["age_band"]["value"] == "15_17" and st.phase == H.NEAR_AREA


def test_a_declined_age_after_a_junior_high_word_stops_as_junior_high():
    """「中2です」と打った後の「ひみつ」は、2 回目も読めない答えと同じく中学生以下として止める(task 258 の決まり)。"""
    st, search = H.new_state(quick=True), _Found()
    for said in ("座ってじっくり", H.QUICK_NEAR, "中2です", "ひみつ"):
        r = H.turn(st, said, H.BANK, Quiet(), search=search)
    assert st.blocked == "under_15" and r["done"] is True and search.calls == []


@pytest.mark.parametrize("said", ["14歳です", "中2です"])
def test_a_junior_high_age_typed_on_the_area_question_is_never_shown_jobs(said):
    """エリア・時間帯の問いで打った年齢も、年齢の答えとして読み直す: 「ひみつ」の後に「14歳です」なら止める。「中2です」は
    年齢をボタンで聞き直し、その後の「ひみつ」は中学生以下として止める。どちらも求人を探さない。"""
    st, search = H.new_state(quick=True), _Found()
    for s in ("座ってじっくり", H.QUICK_NEAR, "ひみつ", said):
        r = H.turn(st, s, H.BANK, Quiet(), search=search)
    for s in ("東京", "ひみつ", "東京", "昼", DONE):
        if st.blocked:
            break
        r = H.turn(st, s, H.BANK, Quiet(), search=search)
    assert st.blocked == "under_15" and r["done"] is True and search.calls == []


def test_a_minor_age_typed_on_the_time_question_hides_the_night_buttons():
    """「18歳以上」の後、時間帯の問いで「実は16歳です」と打つと、前の答えを外して年齢を聞き直し、「16歳」の後は未成年の
    ボタン(朝・昼・夕方)で時間帯を聞き、未成年として探す。エリアの問いで打った「16歳」も同じ(エリアの問いに戻る)。"""
    st, search = H.new_state(quick=True), _Found()
    for s in ("座ってじっくり", H.QUICK_NEAR, "18歳以上", "東京"):
        r = H.turn(st, s, H.BANK, Quiet(), search=search)
    assert r["view"]["choices"] == [*ALL_TIMES, Q.ANY_BUTTON]
    r = H.turn(st, "夜", H.BANK, Quiet(), search=search)  # 決める前に年齢を言い直す(選んだ夜は使わない)
    r = H.turn(st, "実は16歳です", H.BANK, Quiet(), search=search)  # typed_age で読めない言い方: 年齢を聞き直す
    assert st.phase == H.NEAR_AGE and r["view"]["choices"] == AGES and "age_band" not in st.slots and H.minor_mode(st)
    r = H.turn(st, "16歳", H.BANK, Quiet(), search=search)
    assert st.phase == H.NEAR_TIME and r["view"]["choices"] == [*MINOR_TIMES, Q.ANY_BUTTON]
    assert st.slots["age_band"]["value"] == "15_17" and "time_pick" not in st.near
    H.turn(st, "深夜", H.BANK, Quiet(), search=search)  # 選択肢に無いので読まない
    assert st.phase == H.NEAR_TIME and search.calls == []
    H.turn(st, "夕方", H.BANK, Quiet(), search=search)
    H.turn(st, DONE, H.BANK, Quiet(), search=search)
    assert search.calls[-1][logic.MINOR_ARG] is True and search.calls[-1]["time_band"] == ["夕方"]
    st, search = H.new_state(quick=True), _Found()
    for s in ("座ってじっくり", H.QUICK_NEAR, "18歳以上", "16歳"):
        r = H.turn(st, s, H.BANK, Quiet(), search=search)
    assert st.phase == H.NEAR_AREA and st.slots["age_band"]["value"] == "15_17"
    for s in ("東京", "夜", "昼", DONE):
        r = H.turn(st, s, H.BANK, Quiet(), search=search)
    assert search.calls == [search.calls[-1]] and search.calls[-1][logic.MINOR_ARG] is True


def test_an_age_correction_after_the_jobs_searches_again_as_a_minor_or_stops(state):
    """求人の後の年齢の訂正は、相談を閉じずにハーネスが読み直す: 16歳なら未成年として(夜・深夜の答えは未成年のボタンで
    聞き直して)探し直し、14歳なら止める。「中2」は年齢をボタンで聞き直す。"""
    *_, r = walk(state, "ひとりで黙々", H.QUICK_NEAR, "18歳以上", "東京", "昼", "深夜", DONE)
    assert r["phase"] == H.NEAR_JOBS and r["jobs"]
    r = tap(state, "16歳")
    assert r["active"] is True and r["phase"] == H.NEAR_TIME and r["choices"] == [*MINOR_TIMES, Q.ANY_BUTTON]
    r = tap(state, "夕方だけ")
    assert r["phase"] == H.NEAR_JOBS and all(logic.minor_ok(logic.load_jobs()[c["job_id"]]) for c in r["jobs"])
    r = tap(state, "14歳")
    assert r["status"] == "result" and r["blocked"] == "under_15" and "jobs" not in r
    state.reset()
    *_, r = walk(state, "ひとりで黙々", H.QUICK_NEAR, "18歳以上", "東京", "昼", DONE)
    r = tap(state, "実は中2です")
    assert r["phase"] == H.NEAR_AGE and r["reply"] == H.QUICK_TAP_TEXT and r["choices"] == AGES


def test_danger_words_and_resuming_after_the_crisis_guidance_stay_in_the_harness(state):
    *_, r = walk(state, "ひとりで黙々", H.QUICK_NEAR, "18歳以上", "東京", "昼", DONE)
    r = tap(state, "SNSで受け取るだけのバイトある？")
    assert r["active"] is True and r["reply"] == H.NOTICE["danger"] and r["phase"] == H.NEAR_JOBS
    r = tap(state, "もう消えたい")
    assert r["active"] is True and r["reply"] == H.CRISIS_TEXT
    r = tap(state, "続ける")
    assert r["active"] is True and r["phase"] == H.NEAR_JOBS and r["choices"] == [H.NEAR_MORE, H.NEAR_WRONG]


def test_a_failed_search_does_not_trap_the_person_on_the_time_question():
    def broken(cond):
        raise OSError("求人のデータが読めない")

    st = H.new_state(quick=True)
    for said in ("座ってじっくり", H.QUICK_NEAR, "18歳以上", "東京", "昼", DONE):
        r = H.turn(st, said, H.BANK, Quiet(), search=broken)
    assert st.phase == H.NEAR_JOBS and r["reply"] == H.NEAR_FAILED_TEXT and r["view"]["jobs"] == []
    assert r["view"]["choices"] == [H.NEAR_MORE, H.NEAR_WRONG]
    assert [e for e in st.log if e["kind"] == "near_search_failed"] == [{"t": 0, "kind": "near_search_failed",
                                                                         "error": "OSError"}]
    H.turn(st, H.NEAR_MORE, H.BANK, Quiet(), search=broken)
    assert st.phase == "icebreak"


def test_two_relax_steps_keep_both_conditions_off():
    st, search = H.new_state(quick=True), _Found(total=0, relax={"remove": "area", "current": "埼玉", "total_after": 2})
    for said in ("しゃべるの好き", H.QUICK_NEAR, "15〜17歳", "埼玉", "朝", DONE, "エリアを外して探す"):
        H.turn(st, said, H.BANK, Quiet(), search=search)
    search.relax = {"remove": "time_band", "current": ["早朝"], "total_after": 5}
    H.turn(st, "エリアを外して探す", H.BANK, Quiet(), search=search)  # 同じボタン(もう一度 0 件)
    H.turn(st, "時間帯を外して探す", H.BANK, Quiet(), search=search)
    assert search.calls[-1] == {"min_talk_level": 3, logic.MINOR_ARG: True}


# ----------------------------- 0 件と relax -----------------------------


def test_zero_jobs_show_the_one_relax_option_that_adds_the_most(state):
    *_, r = walk(state, "しゃべるの好き", H.QUICK_NEAR, "ひみつ", "埼玉", "朝", DONE)
    cond = {"area": "埼玉", "time_band": ["早朝"], "min_talk_level": 3, "minor": True}
    assert jobs_of(state, **cond)["total"] == 0
    best = logic.relax(state, UID, employment_type=TRACK, **cond)["options"][0]
    assert best["remove"] == "time_band" and best["total_after"] > 0
    assert r["jobs"] == [] and r["reply"] == (f"朝・埼玉で、人と話す仕事は見つからなかった…\n"
                                              f"時間帯を外すと {best['total_after']} 件あるよ！")
    assert r["choices"] == ["時間帯を外して探す", H.NEAR_MORE, H.NEAR_WRONG]
    # 押すと、その条件を外して探し直す(未成年の決まりは外さない)
    r = tap(state, "時間帯を外して探す")
    again = jobs_of(state, area="埼玉", min_talk_level=3, minor=True)
    assert r["jobs"] == again["jobs"] and again["total"] == best["total_after"]
    assert r["reply"] == f"埼玉で、人と話す仕事は {again['total']} 件！" and r["choices"] == [H.NEAR_MORE, H.NEAR_WRONG]


def test_a_time_asked_again_after_dropping_it_narrows_the_search_again(state):
    """0 件で時間帯を外した後に年齢を言い直すと、時間帯を未成年のボタンで聞き直す。選び直した時間帯で探す(外したままにしない)。"""
    *_, r = walk(state, "座ってじっくり", H.QUICK_NEAR, "18歳以上", "埼玉", "深夜", DONE)
    assert r["jobs"] == [] and r["choices"][0] == "時間帯を外して探す"
    r = tap(state, "時間帯を外して探す")
    assert r["reply"].startswith("埼玉で、")
    r = tap(state, "16歳です")
    assert r["phase"] == H.NEAR_TIME and r["choices"] == [*MINOR_TIMES, Q.ANY_BUTTON]
    tap(state, "朝")
    r = tap(state, DONE)
    found = jobs_of(state, area="埼玉", time_band="早朝", max_physical_level=1, minor=True)
    assert r["phase"] == H.NEAR_JOBS and r["reply"].startswith("朝・埼玉で、座ってできる仕事は")
    assert r["jobs"] == found["jobs"]
    # ハーネスだけで: 検索の条件に、選び直した時間帯が入る
    st, search = H.new_state(quick=True), _Found(total=0, relax={"remove": "time_band", "current": ["深夜"], "total_after": 4})
    for said in ("座ってじっくり", H.QUICK_NEAR, "18歳以上", "埼玉", "深夜", DONE, "時間帯を外して探す", "16歳です", "朝", DONE):
        H.turn(st, said, H.BANK, Quiet(), search=search)
    assert search.calls[-2] == {"area": "埼玉", "max_physical_level": 1, logic.MINOR_ARG: False}
    assert search.calls[-1] == {"area": "埼玉", "time_band": ["早朝"], "max_physical_level": 1, logic.MINOR_ARG: True}


def test_relax_options_name_the_pick_and_the_area_and_never_the_minor_rule():
    for remove, what in (("min_talk_level", "「しゃべるの好き」"), ("area", "エリア")):
        st, search = H.new_state(quick=True), _Found(total=0, relax={"remove": remove, "current": 1, "total_after": 7})
        for said in ("しゃべるの好き", H.QUICK_NEAR, "15〜17歳", "埼玉", "朝", DONE):
            r = H.turn(st, said, H.BANK, Quiet(), search=search)
        assert r["reply"].endswith(f"{what}を外すと 7 件あるよ！") and r["view"]["choices"][0] == f"{what}を外して探す"
        H.turn(st, f"{what}を外して探す", H.BANK, Quiet(), search=search)
        assert remove not in search.calls[-1] and search.calls[-1][logic.MINOR_ARG] is True
    st, search = H.new_state(quick=True), _Found(total=0)
    for said in (H.QUICK_NONE, H.QUICK_NEAR, "ひみつ", "どこでも", "朝", DONE):
        r = H.turn(st, said, H.BANK, Quiet(), search=search)
    assert r["reply"] == "朝で、仕事は見つからなかった…\n" + H.NEAR_STUCK_TEXT
    assert r["view"]["choices"] == [H.NEAR_MORE, H.NEAR_WRONG]


# ----------------------------- [ちがう] と、求人の後の発言 -----------------------------


def test_wrong_shows_the_entry_card_again_and_a_new_pick_goes_straight_to_jobs():
    st, search = H.new_state(quick=True), _Found(total=3)
    for said in ("しゃべるの好き", H.QUICK_NEAR, "18歳以上", "神奈川", "夜", DONE):
        H.turn(st, said, H.BANK, Quiet(), search=search)
    r = H.turn(st, H.NEAR_WRONG, H.BANK, Quiet(), search=search)
    assert st.phase == H.QUICK_PICK and r["view"]["quick"]["kind"] == "entry" and r["reply"] == H.QUICK_AGAIN_TEXT
    assert "customer_facing" not in st.slots and st.near == {"age": "18_plus", "area": "神奈川", "time": ["夜"]}
    H.turn(st, "ひとりで黙々", H.BANK, Quiet(), search=search)
    r = H.turn(st, H.QUICK_NEAR, H.BANK, Quiet(), search=search)  # 3 問は聞き直さない
    assert st.phase == H.NEAR_JOBS and r["reply"] == "夜・神奈川で、黙々とできる仕事は 3 件！"
    assert search.calls[-1] == {"area": "神奈川", "time_band": ["夜"], "max_talk_level": 2, logic.MINOR_ARG: False}
    assert st.turns == 0


def test_other_words_after_the_jobs_go_to_the_talk_agent_with_the_answers_saved(state):
    *_, r = walk(state, "座ってじっくり", H.QUICK_NEAR, "15〜17歳", "東京", "昼", "夕方", DONE)
    title = r["jobs"][0]["title"]
    assert "中学" in title  # 求人名の「小中学生」は、年齢の訂正(「中学」の語)と読まない
    r = session.turn(state, UID, f"「{title}」について詳しく教えて")
    assert r["active"] is False and r["ended"] == "exit"
    prefs = state.user(UID)["preferences"]
    assert prefs["area"]["value"] == "東京" and prefs["time_band"]["value"] == "昼・夕方〜夜"
    assert "深夜シフト" in prefs["avoids"]["value"]


def test_the_reaction_before_a_question_may_not_name_a_job():
    class Says(Quiet):
        def generate(self, v):
            return json.dumps({"reply": "居酒屋もいいかも！" + v["text"]}, ensure_ascii=False)

    st = H.new_state(quick=True)
    H.turn(st, H.QUICK_NONE, H.BANK, Quiet())
    r = H.turn(st, H.QUICK_NEAR, H.BANK, Says())
    assert r["reply"] == H.NEAR_TEXT[H.NEAR_AGE]


def test_the_jobs_flex_fits_the_line_limits(state):
    *_, r = walk(state, "動いてたい", H.QUICK_NEAR, "18歳以上", "どこでも", "夕方", DONE)
    reply = {"bubbles": [r["reply"]], "cards": r["jobs"], "quick_replies": r["choices"]}
    msgs = to_messages(reply, base_url="https://aica.example", features=())
    assert len(msgs) <= 5 and 0 < len(msgs[1]["altText"]) <= 400
    assert len(json.dumps(msgs[1]["contents"], ensure_ascii=False).encode()) < 50_000  # carousel の上限
    assert all(0 < len(i["action"]["label"]) <= 20 for i in msgs[-1]["quickReply"]["items"])
    assert all(len(H.NEAR_RELAX_BUTTON.format(what=w)) <= 20
               for w in [*H.RELAX_WHAT.values(), *(f"「{p}」" for p in H.QUICK_PICKS)])


class _Found:
    """「近くで探す」の求人の検索の代わり(ハーネスだけを見るテスト)。受け取った条件を calls に残す。"""

    def __init__(self, total=1, relax=None):
        self.total, self.relax, self.calls = total, relax, []

    def __call__(self, cond):
        self.calls.append(dict(cond))
        return {"total": self.total, "jobs": [], "relax": self.relax}
