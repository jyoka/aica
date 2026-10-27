"""正社員のヒアリングのハーネス(dip_mcp/career、設計メモ docs/design/fulltime-hearing.md、ADR-0010)。LLM は偽物。

(1) 質問バンク: role と result の問いに答えの候補のボタンが無い。ボタンと吹き出しの字数の予算。HARD の欄が評価の事実表と同じ
(2) モックの本人役 6 人 × 形 B と C(evals/career_sim.py): 全員 result に着き、合否の指標がすべて 0
(3) 時間と MAX_TURNS の予算で止まる。HARD は時刻を過ぎても聞き、review は必ず 1 回出す
(4) 履歴書の読み取り結果で埋まった欄を聞かない
(5) 形 B と C の切り替え。B では場面の二択と逆選択、進み具合を出さない
(6) 「あとで」で、そこまでの状態を返して終える
(7) 文の答え: 本人の言葉のまま入れる、数を作らない、読めなければボタンで聞き直す(1 回だけ)、生成の検証
"""

from __future__ import annotations

import json

import pytest

from dip_mcp.career import harness as H
from evals import career_sim
from evals.fulltime_hearing import personas as P

PERSONAS = {p["id"]: p for p in P.load_personas()}


class LLM:
    """extract は台本(dict / 文字列 / 例外)を順に返す(無くなれば空の答え)。generate は台本か、質問をそのまま返す。"""

    def __init__(self, extracts=(), generates=()):
        self.extracts, self.generates = list(extracts), list(generates)
        self.calls: list[str] = []
        self.payloads: list[dict] = []

    @staticmethod
    def _give(x):
        if isinstance(x, Exception):
            raise x
        return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)

    def extract(self, payload):
        self.calls.append("extract")
        self.payloads.append(payload)
        if self.extracts:
            return self._give(self.extracts.pop(0))
        # 台本が尽きたら、今の問いの欄に本人の文をそのまま入れる(文の問いを進めるため)
        t = [x for x in payload["targets"] if x in H.ALL_SLOTS][:1] if payload["mode"] != "review" else []
        return json.dumps({"slot_updates": [{"slot": x, "text": payload["user_text"]} for x in t]}, ensure_ascii=False)

    def generate(self, view):
        self.calls.append("generate")
        self.payloads.append(view)
        return self._give(self.generates.pop(0)) if self.generates else json.dumps({"reply": view["text"]}, ensure_ascii=False)


def _ext(slot=None, text="", number=None, unit=None, choice=None, **kw):
    ups = [{"slot": slot, "text": text, "number": number, "unit": unit, "choice": choice}] if slot else []
    return {"slot_updates": ups, "ng_updates": [], "segment": None, "intent": "answer", "review_ack": None, **kw}


def _to(state, qid, llm=None, limit=30):
    """qid が出るまで、ボタンの 1 つ目で進める。ボタンの無い文の問いには「答え」と打つ。"""
    llm = llm or LLM()
    out = H.start(state)
    for _ in range(limit):
        if state.current["qid"] == qid:
            return out
        q = H.qmap(state, H.BANK).get(state.current["qid"])
        out = H.turn(state, (q["choices"] or ["答え"])[0] if q else H.REVIEW_OK, H.BANK, llm)
    raise AssertionError(f"{qid} が出なかった: {state.asked}")


# ----------------------------- (1) 質問バンク -----------------------------
def test_role_and_result_have_no_answer_candidates():
    """role と result の問いは、答えの候補を出さず「覚えていない」「パス」(と「あとで」)だけ(§5.1。誘導しない)。"""
    st = H.new_state(form="C")
    qs = [q for q in H.questions(st, H.BANK) if q["id"].startswith(("card_role", "card_result"))]
    assert len(qs) == 2 * H.CARD_COUNT
    for q in qs:
        assert q["choices"] == [] and q["choice_effects"] == {}
        assert H.choices_of(q) == [H.DONT_KNOW, H.PASS, H.LATER]


def test_role_and_result_buttons_in_a_conversation():
    """会話の中で role と result を聞いたターンのボタンも、その 3 つだけ。"""
    rows = career_sim.run_all([PERSONAS["p1-sales"]], forms=("B", "C"))
    for r in rows:
        lines = [x for x in r["transcript"] if "/card_role" in x or "/card_result" in x]
        assert lines
        for x in lines:
            assert x.endswith("[ボタン] 覚えていない / パス / あとで"), x


@pytest.mark.parametrize("form", H.FORMS)
def test_bank_fits_the_budgets(form):
    """ボタンは 13 個まで、ラベルは 20 字まで、合わせて 40 字まで。問いの文は、前置き(最初、聞き直し)を付けても 80 字まで。"""
    for seg in (*H.SEGMENTS, "unknown"):
        st = H.new_state(form=form)
        st.segment = seg
        for q in H.questions(st, H.BANK):
            ch = H.choices_of(q)
            assert len(ch) <= H.QUICK_MAX and all(len(c) <= H.LABEL_MAX for c in ch), q["id"]
            assert sum(H.chars(c) for c in ch) <= H.LABELS_MAX, (q["id"], sum(H.chars(c) for c in ch))
            for lead in ("", H.START_LEAD, H.RETRY_TEXT, H.CLARIFY_TEXT):
                assert H.chars(lead + q["text"]) <= H.BUBBLE_MAX, (q["id"], lead)
            assert set(q["choice_effects"]) <= set(q["choices"]), q["id"]
            for eff in q["choice_effects"].values():
                assert set(eff) <= H.ALL_SLOTS | {"__segment__", "__ng__"}, q["id"]
            assert all(t in H.ALL_SLOTS | {"segment", "__ng__"} for t in q["targets"]), q["id"]


def test_hard_slots_match_the_fact_tables():
    """HARD の欄と職歴カードの欄の名前が、1/5 の評価の事実表(evals/fulltime_hearing/personas.py)と同じ。"""
    assert H.COMMON_HARD == P.COMMON_HARD
    assert H.SEGMENT_HARD == P.SEGMENT_HARD
    assert H.CARD_FIELDS == P.CARD_FIELDS
    for p in PERSONAS.values():
        assert {f["key"] for f in p["facts"]} <= H.ALL_SLOTS, p["id"]


def test_scenarios_and_reverse_pick_never_write_hard():
    """場面の二択と逆選択は HARD を書かない(推し量るのは MID と LOW、NG だけ)。"""
    hard = set(H.COMMON_HARD).union(*H.SEGMENT_HARD.values())
    for q in H.BANK:
        if q["phase"] in ("scenario_game", "reverse_pick"):
            assert not hard & set(q["targets"]), q["id"]
            for eff in q["choice_effects"].values():
                assert not hard & set(eff), q["id"]


# ----------------------------- (2) モックの本人役での回帰 -----------------------------
def test_mock_regression_all_pass():
    """6 人 × 形 B、C、D で、result に着き、離脱せず、HARD が空 0、wasted 0、予算で止まる(§6.2 の「モック ○」の行)。"""
    rows = career_sim.run_all()
    assert len(rows) == 18
    for r in rows:
        m = r["metrics"]
        assert career_sim.failures(m) == [], (r["form"], r["persona"], career_sim.failures(m))
        assert m["completed"] and m["turns"] <= H.MAX_TURNS
        assert m["stop_reason"] in ("time", "turns", "done")
        limit = 180 if PERSONAS[r["persona"]].get("resume") else 300
        assert m["actor_seconds"] <= limit, (r["persona"], m["actor_seconds"])
    assert career_sim.main([]) == 0


def test_mock_regression_writes_evidence(tmp_path, capsys):
    assert career_sim.main(["--log", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip().endswith("すべて合格")
    assert (tmp_path / "results.json").exists() and (tmp_path / "C_p1-sales.txt").exists()


# ----------------------------- (3) 時間と MAX_TURNS の予算 -----------------------------
def test_stops_at_the_time_budget(monkeypatch):
    """長く打つ人: 質問を止める時刻を過ぎたら、HARD のほかは聞かずに review。止める時刻の後に HARD でない問いを出さない。"""
    slow = json.loads(json.dumps(PERSONAS["p1-sales"]))
    for f in slow["facts"]:
        if f["kind"] == "text" and f["key"] not in ("start_timing", "raise_target"):
            f["value"] = f["value"] * 3
    r = career_sim.run_one(slow, "C")
    m = career_sim.metrics(r, slow)
    assert m["stop_reason"] == "time" and m["budget_violation"] == [] and m["hard_empty"] == []
    st = r["state"]
    late = [e for e in st.log if e["kind"] == "ask" and e["seconds"] > H.stop_at(st)]
    assert all(set(e["targets"]) <= set(H.hard_slots(st)) for e in late)
    assert "card_role2" not in st.asked  # 1 枚目より優先度の低い 2 枚目は聞かない


def test_open_hard_is_asked_after_the_time_budget():
    """時刻を過ぎても、空いている HARD は聞く(時刻の前に聞く順にしてあるが、遅い人でも HARD は埋める)。"""
    st = H.new_state(form="C")
    _to(st, "hf_area")
    st.seconds = H.stop_at(st) + 50
    out = H.turn(st, "大阪市内", H.BANK, LLM([_ext("area", "大阪市内")]))
    assert out["qid"] == "hf_salary"
    for label in ("400万円台", "半年以内", "役職"):  # segment は「同じ仕事で上へ」なので、上げたいものも HARD
        out = H.turn(st, label, H.BANK, LLM())
    assert out["qid"] == "review" and st.stop_reason == "time"


def test_turn_budget_reserves_review():
    """HARD でない問いは 14 ターン目まで。HARD は 15 ターン目まで使ってよい。review は必ず 1 回出し、16 ターンを超えない。"""
    st = H.new_state(form="C")
    _to(st, "card_role1")
    st.turns = H.MAX_TURNS - H.REVIEW_RESERVE
    assert not H.can_ask(st, "career1.role") and H.can_ask(st, "why_now")
    st.turns = H.HARD_LAST_TURN
    assert not H.can_ask(st, "why_now")


def test_stops_at_max_turns(monkeypatch):
    """時間が十分にあっても、MAX_TURNS の予算で止まる。review は 1 回出て、16 ターンを超えない。"""
    monkeypatch.setattr(H, "TARGET_SECONDS", {True: 9999.0, False: 9999.0})
    r = career_sim.run_one(PERSONAS["p1-sales"], "C")
    m = career_sim.metrics(r, PERSONAS["p1-sales"])
    st = r["state"]
    assert st.result is not None and st.stop_reason == "turns"
    assert st.turns <= H.MAX_TURNS and st.review_rounds >= 1
    assert m["budget_violation"] == [] and m["over_max_turns"] == 0
    assert max(e["turns"] for e in st.log if e["kind"] == "ask") < H.MAX_TURNS - H.REVIEW_RESERVE


def test_seconds_follow_the_reading_and_typing_formula():
    """推定時間は §6.1 と同じ式(読む 400 字/分、打つ 40 字/分、ボタン 2 秒)。"""
    st = H.new_state()
    out = H.start(st)
    read0 = sum(H.chars(x) for x in out["bubbles"] + out["choices"]) * 60 / 400
    assert st.seconds == pytest.approx(read0)
    H.start(st)  # 何回呼んでも同じ。時間は 1 回目だけ
    assert st.seconds == pytest.approx(read0)
    out = H.turn(st, "同じ仕事で上へ", H.BANK, LLM())
    read1 = sum(H.chars(x) for x in out["bubbles"] + out["choices"]) * 60 / 400
    assert st.seconds == pytest.approx(read0 + 2 + read1)


# ----------------------------- (4) 履歴書 -----------------------------
def test_resume_filled_slots_are_not_asked():
    """履歴書の読み取り結果で埋まった欄(p5: 勤務地、職歴1 の何をしていたかとどのくらい、職歴2 の何をしていたか)は聞かない。"""
    p5 = PERSONAS["p5-resume"]
    for form in H.FORMS:
        r = career_sim.run_one(p5, form)
        st = r["state"]
        asked_targets = {t for e in st.log if e["kind"] == "ask" for t in e["targets"]}
        assert not asked_targets & set(p5["resume"]["filled"]), asked_targets
        assert {"hf_area", "card_role1", "card_scale1", "card_role2"}.isdisjoint(st.asked)
        for k in p5["resume"]["filled"]:
            assert st.slots[k]["from"] == "resume" and st.slots[k]["confidence"] == "high"
        assert st.has_resume and H.stop_at(st) == 90


def test_questions_on_a_card_known_from_the_resume_name_the_job():
    """何をしていたかが履歴書から入った職歴の、残りの問いには仕事の言葉を付ける(task 248。実 API の本人役が
    「それで、何が変わりましたか?」だけでは何の話か分からず、聞き返した)。この会話で本人が言った職歴には付けない。"""
    p5 = PERSONAS["p5-resume"]
    r = career_sim.run_one(p5, "C")
    asked = {e["qid"]: e for e in r["state"].log if e["kind"] == "ask"}
    q = H.qmap(r["state"], H.BANK)["card_result1"]
    assert "card_result1" in asked and q["text"] == "「みほんホテルでフロン…」で、何が変わりましたか?"
    assert H.chars(q["text"]) <= H.BUBBLE_MAX - H.REACTION_MAX
    st = H.new_state(form="C")
    H.start(st, H.BANK)
    st.slots["career1.role"] = H.entry("filled", "物流会社で現場リーダー", frm="free_current:card_role1")
    assert H.qmap(st, H.BANK)["card_result1"]["text"] == "それで、何が変わりましたか?"
    st.slots["career1.role"]["from"] = "saved"  # 前の相談で保存した職歴も、この会話では言っていない
    assert H.qmap(st, H.BANK)["card_scale1"]["text"].startswith("「物流会社で現場リーダ…」の規模は?")  # 10 字で切る
    st.slots["career1.role"]["text"] = "営業\n係長"  # 履歴書の改行は空白に(問いの文は 1 行)
    assert H.qmap(st, H.BANK)["card_result1"]["text"] == "「営業 係長」で、何が変わりましたか?"
    st.slots["career1.role"]["text"] = "物流センター長で1200人を統括"  # 数の途中で切らない(「12…」は本人が言っていない数)
    q = H.qmap(st, H.BANK)["card_result1"]
    assert q["text"] == "「物流センター長で…」で、何が変わりましたか?" and not H.digits_in(q["text"])
    st.slots["career1.role"]["text"] = "物流センター長で千二百人を統括"  # 漢数字も
    assert H.qmap(st, H.BANK)["card_result1"]["text"] == "「物流センター長で…」で、何が変わりましたか?"
    st.slots["career1.role"]["text"] = "物流センター長で1,200人"  # 区切りの「,」も並びの中
    assert H.qmap(st, H.BANK)["card_result1"]["text"] == "「物流センター長で…」で、何が変わりましたか?"
    st.slots["career1.role"]["text"] = "営業部長で年商3億5千万円"  # 算用数字と漢数字が混ざった数も 1 つの並び(「3億5…」にしない)
    assert H.qmap(st, H.BANK)["card_scale1"]["text"].startswith("「営業部長で年商…」の規模は?")
    st.slots["career1.role"]["text"] = "物流センター長で1万2000人"  # 「1万…」は 1万2000 と違う数
    assert H.qmap(st, H.BANK)["card_result1"]["text"] == "「物流センター長で…」で、何が変わりましたか?"
    st.slots["career1.role"]["text"] = "物流センター長で千2百人"
    assert H.qmap(st, H.BANK)["card_result1"]["text"] == "「物流センター長で…」で、何が変わりましたか?"
    assert H._clip("一般職の事務と経理の担当", 10) == "一般職の事務と経理の…"  # 漢数字 1 字の語は、並びの途中で切れないので変わらない
    assert H._clip("1200人の物流", 3) == "1200…" and H._clip("1200", 3) == "1200"  # 頭が数なら、数の後ろで切る


def test_no_reverse_pick_after_the_person_named_what_to_stop():
    """やめたいこと(early)か NG をもう言った人には、逆選択を聞かない(task 248。実 API で聞き直しに見えた)。"""
    p4 = PERSONAS["p4-short"]
    r = career_sim.run_one(p4, "C")
    st = r["state"]
    assert st.slots["stop_doing"]["status"] == "filled" and "rp_general" not in st.asked
    st2 = H.new_state(form="C")
    st2.user_ng.append("夜勤")
    rp = next(q for q in H.BANK if q["id"] == "rp_general")
    assert H.score(st2, rp) == 0.0 and H.score(H.new_state(form="C"), rp) > 0


def test_prefill_skips_why_now():
    st = H.new_state(prefill={"why_now": {"text": "役職が上がらない"}})
    _to(st, "hf_area")
    assert "ib_why_now" not in st.asked


# ----------------------------- (5) 形 B と C -----------------------------
def test_form_b_has_no_scenarios_reverse_pick_or_progress():
    """B は場面の二択と逆選択を出さず、MID を直接聞き、進み具合と 1 行を見せない。C は出す。"""
    b = career_sim.run_all([PERSONAS["p4-short"]], forms=("B",))[0]
    c = career_sim.run_all([PERSONAS["p4-short"]], forms=("C",))[0]
    b_text, c_text = "\n".join(b["transcript"]), "\n".join(c["transcript"])
    assert "/sc_" not in b_text and "/rp_" not in b_text and "/fg_work_style" in b_text
    assert "✓職歴" not in b_text and "あと" not in b_text.replace("あとで", "")
    # p4 はやめたいことを言うので、C でも逆選択は出ない(task 248)。逆選択が C の問いにあることは、状態で確かめる
    assert "/sc_" in c_text and "/rp_" not in c_text and "✓職歴1" in c_text and "問です" in c_text
    assert [q["id"] for q in H.questions(H.new_state(form="C"), H.BANK) if q["phase"] == "reverse_pick"] == ["rp_general"]
    assert H.new_state(form="B").form == "B"
    assert not [q for q in H.questions(H.new_state(form="B"), H.BANK) if q["phase"] in ("scenario_game", "reverse_pick")]


def test_form_setting_from_env(monkeypatch):
    monkeypatch.setenv(H.FORM_ENV, "B")
    assert H.new_state().form == "B"
    monkeypatch.setenv(H.FORM_ENV, "x")
    assert H.new_state().form == H.DEFAULT_FORM == "C"
    monkeypatch.delenv(H.FORM_ENV)
    assert H.new_state().form == "C" and H.new_state(form="B").form == "B"


# ----------------------------- (6) あとで -----------------------------
def test_later_button_returns_state_so_far():
    """「あとで」を押すと、そこまでの状態(summary)を返して終える。その後の発言では何も聞かない。"""
    st = H.new_state(form="C")
    _to(st, "ib_why_now")
    H.turn(st, "年収が上がらない", H.BANK, LLM())
    H.turn(st, "新宿", H.BANK, LLM())
    assert st.current["qid"] == "hf_salary"
    out = H.turn(st, H.LATER, H.BANK, LLM())
    assert out["done"] and out["closed"] == "later" and out["reply"] == H.LATER_TEXT
    s = out["summary"]
    assert s["closed"] == "later" and set(s["slots"]) == {"why_now", "area"}
    assert s["slots"]["why_now"]["text"] == "年収が上がらない"
    llm = LLM()
    again = H.turn(st, "やっぱり続ける", H.BANK, llm)
    assert again["done"] and llm.calls == [] and st.turns == 3


@pytest.mark.parametrize("where", ["review", "card_scale1"])
def test_later_typed_or_in_review(where):
    """「あとで」は、どの問いにもボタンがあり、文で言っても(抽出の intent=later)終える。"""
    st = H.new_state(form="B")
    out = _to(st, where)
    assert H.LATER in out["choices"]
    out = H.turn(st, "また今度にします", H.BANK, LLM([{"slot_updates": [], "intent": "later"}]))
    assert out["done"] and st.closed == "later" and st.result is None


def test_answer_typed_with_later_is_kept():
    """「あとで」と一緒に打った答えも、終える前に今の問いの答えとして残す(次は聞かない)。"""
    st = H.new_state(form="C")
    _to(st, "hf_area")
    out = H.turn(st, "大阪市内です。続きはまた今度", H.BANK, LLM([_ext("area", "大阪市内", intent="later")]))
    assert out["done"] and st.closed == "later"
    assert out["summary"]["slots"]["area"]["text"] == "大阪市内です。続きはまた今度"
    assert out["summary"]["slots"]["area"]["confidence"] == "high"


# ----------------------------- (7) 文の答え、生成 -----------------------------
def test_free_text_is_kept_verbatim_and_numbers_are_not_made_up():
    """今の問いへの文は本人の言葉のまま。数は発言に数字で書いてあるときだけ入れる。ほかの欄は抜き出しが発言にあるときだけ(low)。"""
    st = H.new_state(form="B")
    _to(st, "ib_why_now")
    H.turn(st, "年収が上がらないので。勤務地は大阪市内がいい", H.BANK,
           LLM([{"slot_updates": [{"slot": "why_now", "text": "年収アップ"}, {"slot": "area", "text": "大阪市内"},
                                  {"slot": "start_timing", "text": "来月"}]}]))
    assert st.slots["why_now"]["text"] == "年収が上がらないので。勤務地は大阪市内がいい"
    assert st.slots["area"] == {**st.slots["area"], "text": "大阪市内", "confidence": "low"}
    assert "start_timing" not in st.slots  # 発言に無い抜き出し
    assert "hf_area" not in st.asked or st.current["qid"] != "hf_area"  # low の欄は聞き直さず、まとめで確かめる
    _to(st, "card_scale1")
    out = H.turn(st, "百人くらい", H.BANK, LLM([_ext("career1.scale", "百人くらい", number=100, unit="人")]))
    assert out["qid"] == "card_scale1" and "career1.scale" not in st.slots  # 数が読めない: ボタンで聞き直す
    assert H.RETRY_TEXT in out["reply"] and out["counted"] is False
    H.turn(st, "120社", H.BANK, LLM([_ext("career1.scale", "120社", number=120, unit="社")]))
    assert st.slots["career1.scale"] | {} == {**st.slots["career1.scale"], "text": "120社", "number": 120, "unit": "社"}


def test_unreadable_answer_is_retried_once_with_buttons():
    """読めない答え(抽出が 2 回とも失敗): 1 回だけ数えずにボタンで聞き直す。2 回目は本人の文を low で入れ、もう聞かない。"""
    st = H.new_state(form="B")
    _to(st, "hf_timing")
    turns = st.turns
    out = H.turn(st, "うーん", H.BANK, LLM(["壊れた", "壊れた"]))
    assert out["qid"] == "hf_timing" and st.turns == turns and out["choices"][0] == "すぐ"
    out = H.turn(st, "そのうち", H.BANK, LLM(["壊れた", "壊れた"]))
    assert st.slots["start_timing"]["text"] == "そのうち" and st.slots["start_timing"]["confidence"] == "low"
    assert out["qid"] != "hf_timing" and st.turns == turns + 1


def test_typed_choice_keeps_the_users_words():
    st = H.new_state(form="B")
    _to(st, "hf_raise", LLM([{"slot_updates": [], "segment": "same_path"}]))
    H.turn(st, "やっぱり給料かな", H.BANK, LLM([_ext("raise_target", "給料", choice="年収")]))
    assert st.slots["raise_target"]["text"] == "やっぱり給料かな" and st.slots["raise_target"]["value"] == "年収"


@pytest.mark.parametrize("bad,why", [
    ({"reply": "すごいですね!働きたい場所はどこですか?(駅や市の名前で)"}, "praise"),
    ({"reply": "30社もですね。働きたい場所はどこですか?(駅や市の名前で)"}, "made_up_number"),
    ({"reply": "年収も聞いていいですか?働きたい場所はどこですか?(駅や市の名前で)"}, "two_questions"),
    ({"reply": "ありがとうございます。"}, "question_missing"),
    ({"reply": "ありがとうございます、とても参考になりましたし次に進みます。働きたい場所はどこですか?(駅や市の名前で)"},
     "too_much_extra"),
])
def test_generation_is_checked_and_falls_back_to_the_question(bad, why):
    v = {"text": "働きたい場所はどこですか?(駅や市の名前で)", "user_text": "成果が出ても上がらない", "progress": []}
    assert H.validate_reply(json.dumps(bad, ensure_ascii=False), v) == (None, why)
    ok = "ありがとうございます。" + v["text"]
    assert H.validate_reply(json.dumps({"reply": ok}, ensure_ascii=False), v) == (ok, None)
    st = H.new_state(form="B")
    _to(st, "ib_why_now")
    out = H.turn(st, "成果が出ても上がらない", H.BANK, LLM([_ext("why_now", "成果が出ても上がらない")], [bad, bad]))
    assert out["reply"] == v["text"]


def test_prompts_get_all_variables():
    st = H.new_state(form="C")
    _to(st, "ib_why_now")
    H.render_prompt("extract", H.extract_payload(st, H.BANK, "テスト"))
    H.render_prompt("say", H.view(st, H.BANK))
    _to(st, "review")
    H.render_prompt("extract", H.extract_payload(st, H.BANK, "これでOK"))


def test_review_card_budget_and_corrections():
    """まとめは文とカードで 200 字まで。文で直した欄は high。HARD が空なら 2 回目は「ここだけ」。"""
    st = H.new_state(form="C", prefill={"why_now": {"text": "あ" * 60}, "career1.role": {"text": "い" * 60},
                                         "career1.result": {"text": "う" * 60}, "career2.role": {"text": "え" * 60}})
    out = _to(st, "review", LLM([_ext("area", "大阪市内")]))
    assert H.chars(st.last_card["lead"]) + sum(H.chars(x) for x in out["card"]) <= H.CARD_MAX
    assert all(H.chars(b) <= H.BUBBLE_MAX for b in out["bubbles"])
    st.slots.pop("start_timing")  # HARD を空にしておく
    out = H.turn(st, "勤務地は梅田にして", H.BANK, LLM([_ext("area", "梅田", review_ack="partial")]))
    assert st.slots["area"]["text"] == "梅田" and st.slots["area"]["from"].startswith("free_review")
    assert out["qid"] == "review" and st.last_card["focus"] and out["bubbles"][0] == H.REVIEW_FOCUS_LEAD
    out = H.turn(st, H.REVIEW_OK, H.BANK, LLM())
    assert out["done"] and st.result and st.review_rounds == 2


def test_review_card_shows_no_unasked_mid_or_card_fields():
    """まとめのカードに、聞かなかった MID と職歴カードの欄(「未定」)を出さない(task 248。実 API の本人役が「未定」を埋めようと
    長い文を打ち、履歴書ありの人が目標時間を超えた)。空いている HARD は「未定」のまま出す(まとめの 2 回目で聞く)。"""
    st = H.new_state(form="C", prefill={"why_now": {"text": "役職が上がらない"}, "career1.role": {"text": "ホテルのフロント"},
                                         "career2.role": {"text": "旅行の営業"}})
    st.segment = "same_path"
    lines = H.card_lines(st, 16)
    assert "勤務地: 未定" in lines  # HARD
    assert not [x for x in lines if x.startswith(("働き方", "次は"))]  # MID
    assert "職歴1: ホテルのフロント" in lines and "職歴2: 旅行の営業" in lines
    assert not [x for x in lines if "未定" in x and not x.startswith(tuple(H.SLOTS[s]["short"] for s in H.hard_slots(st)))]


def test_review_ok_promotes_inferred_values():
    st = H.new_state(form="C")
    _to(st, "review")
    lows = [s for s, e in st.slots.items() if e["confidence"] == "low"]
    H.turn(st, H.REVIEW_OK, H.BANK, LLM())
    assert all(st.slots[s]["confidence"] == "high" for s in lows) and st.result


def test_internal_error_rolls_back(monkeypatch):
    st = H.new_state()
    H.start(st)
    before = st.to_dict()
    monkeypatch.setattr(H, "advance", lambda *a: 1 / 0)
    out = H.turn(st, "同じ仕事で上へ", H.BANK, LLM())
    assert out["qid"] == "q_segment" and st.turns == before["turns"] and st.log[-1]["kind"] == "internal_error"


@pytest.mark.parametrize("form", H.FORMS)
def test_state_round_trip_and_determinism(form):
    """毎ターンの前に状態を JSON にして戻しても、同じ出力で同じ道をたどる(2b の dip のツールが、保存した状態から
    出力を受け取りながらやり直すため)。"""
    for p in PERSONAS.values():
        a = career_sim.run_one(p, form)["state"]
        b = career_sim.run_one(p, form, roundtrip=True)["state"]
        assert a.to_dict() == b.to_dict(), p["id"]


@pytest.mark.parametrize("form", H.FORMS)
def test_mock_regression_with_broken_extraction(form):
    """読み取りが文の答えごとに 1 回壊れても(1 回やり直して読める)、6 人とも合否の指標がすべて 0。"""
    for p in PERSONAS.values():
        r = career_sim.run_one(p, form, llm=career_sim.FlakyOracleLLM())
        m = career_sim.metrics(r, p)
        assert career_sim.failures(m) == [] and m["completed"], (p["id"], career_sim.failures(m))
        assert r["llm"].calls["broken"] >= 1 and any(e["kind"] == "extract_retry_ok" for e in r["state"].log)


def test_counted_asides_never_skip_review():
    """4 回目からの雑談は数えるが、review のための 2 ターンと review の中では数えない。review は必ず 1 回出る。"""
    st = H.new_state(form="C")
    _to(st, "card_role1")
    st.turns, st.aside_count = 13, H.MAX_ASIDES
    aside = {"slot_updates": [], "intent": "aside"}
    for _ in range(3):
        H.turn(st, "雑談", H.BANK, LLM([aside]))
    assert st.turns == H.MAX_TURNS - H.REVIEW_RESERVE and st.current["qid"] == "card_role1"
    out = H.turn(st, "物流会社で事務", H.BANK, LLM())
    assert out["qid"] == "review" and st.stop_reason == "turns"
    for _ in range(3):
        out = H.turn(st, "雑談", H.BANK, LLM([aside]))
        assert out["qid"] == "review" and not out["counted"]
    out = H.turn(st, H.REVIEW_OK, H.BANK, LLM())
    assert out["done"] and st.review_rounds == 1 and st.turns <= H.MAX_TURNS


def test_clarify_does_not_push_hard_behind_cards():
    """聞き返しを 2 回使っても、segment だけの HARD は職歴カードより先に聞く。"""
    st = H.new_state(form="B")
    H.start(st)
    H.turn(st, "仕事を変えたい", H.BANK, LLM())
    H.turn(st, "仕組みを作りたい", H.BANK, LLM())
    for qid in ("hf_area", "hf_salary"):
        assert st.current["qid"] == qid
        H.turn(st, "どういうこと?", H.BANK, LLM([{"slot_updates": [], "intent": "clarify"}]))
        H.turn(st, "大阪市内" if qid == "hf_area" else "400万円台", H.BANK, LLM())
    order = [e["qid"] for e in st.log if e["kind"] == "ask" and e["mode"] == "ask"]
    _to(st, "card_role1")
    order = [e["qid"] for e in st.log if e["kind"] == "ask" and e["mode"] == "ask"]
    assert order.index("hf_use_experience") < order.index("card_role1")


@pytest.mark.parametrize("form", ["B", "C"])
def test_late_segment_hard_comes_before_card2_mid_and_low(form):
    """segment をパスし、1 枚目の職歴カードの答えで change に決まったとき、change の HARD を 2 枚目のカード・場面の二択・
    逆選択より先に聞き、2 つとも埋める(§3.3 の優先度)。"""
    st = H.new_state(form=form)
    H.start(st)
    H.turn(st, H.PASS, H.BANK, LLM())
    assert st.segment == "unknown"
    _to(st, "card_role1")
    H.turn(st, "営業から企画に変えたい", H.BANK,
           LLM([_ext("career1.role", "営業から企画に変えたい", segment="change")]))
    assert st.segment == "change"
    out = _to(st, "review")
    order = [e["qid"] for e in st.log if e["kind"] == "ask" and e["mode"] == "ask"]
    for hard in ("hf_change_reason", "hf_use_experience"):
        assert st.slots[hard.removeprefix("hf_")]["status"] == "filled"
        later = [q for q in order if q.startswith(("sc_", "rp_")) or (q.startswith("card_") and q.endswith("2"))]
        assert all(order.index(hard) < order.index(q) for q in later), order
    assert out["choices"][0] == H.REVIEW_OK


def test_typed_numbers_must_be_written_in_digits():
    """「四百万くらい」は、抽出が区切り(400万円台)を返しても区切りにしない(数を作らない)。ボタンで聞き直し、それでも読めなければ
    本人の文を low で入れる。単位は発言に書いてあるときだけ。"""
    st = H.new_state(form="B")
    _to(st, "hf_salary")
    out = H.turn(st, "四百万くらい", H.BANK, LLM([_ext("min_annual_salary", "四百万", number=400, unit="万円", choice="400万円台")]))
    assert out["qid"] == "hf_salary" and H.RETRY_TEXT in out["reply"] and "min_annual_salary" not in st.slots
    H.turn(st, "四百万くらい", H.BANK, LLM([_ext("min_annual_salary", "四百万", number=400, unit="万円", choice="400万円台")]))
    e = st.slots["min_annual_salary"]
    assert (e["text"], e["number"], e["range"], e["confidence"]) == ("四百万くらい", None, None, "low")
    st = H.new_state(form="B")
    _to(st, "hf_salary")
    H.turn(st, "400万円台かな", H.BANK, LLM([_ext("min_annual_salary", "400万円台", choice="400万円台")]))
    assert st.slots["min_annual_salary"]["range"] == [400, 499] and st.slots["min_annual_salary"]["confidence"] == "high"
    _to(st, "card_scale1")
    H.turn(st, "120", H.BANK, LLM([_ext("career1.scale", "120", number=120, unit="社")]))
    assert st.slots["career1.scale"]["number"] == 120 and st.slots["career1.scale"]["unit"] is None


@pytest.mark.parametrize("reply", ["百社も担当されたんですね。それで、何が変わりましたか?",
                                   "素敵ですね。それで、何が変わりましたか?"])
def test_generation_rejects_kanji_numbers_and_praise(reply):
    v = {"text": "それで、何が変わりましたか?", "user_text": "120社", "progress": []}
    assert H.validate_reply(json.dumps({"reply": reply}, ensure_ascii=False), v)[0] is None


def test_long_ng_lines_fit_the_card():
    st = H.new_state(form="C")
    _to(st, "review")
    st.user_ng = ["あ" * 45, "い" * 45, "う" * 45]
    card = H.build_card(st)
    assert H.chars(card["lead"]) + sum(H.chars(x) for x in card["lines"]) <= H.CARD_MAX
    assert all(H.chars(b) <= H.BUBBLE_MAX for b in H.pack_bubbles(card["lines"]))


def test_pending_answers_are_kept_in_the_summary():
    """segment が決まらないまま、segment だけの欄の答えが来たら、捨てずに summary の pending に残す。"""
    st = H.new_state(form="B")
    H.start(st)
    H.turn(st, H.PASS, H.BANK, LLM())
    H.turn(st, "企画職に変えたいから", H.BANK, LLM([{"slot_updates": [
        {"slot": "why_now", "text": "企画職に変えたいから"}, {"slot": "change_reason", "text": "企画職に変えたい"}]}]))
    out = H.turn(st, H.LATER, H.BANK, LLM())
    assert out["summary"]["pending"]["change_reason"]["text"] == "企画職に変えたい"


def test_review_correction_and_second_round_in_a_conversation():
    """まとめで本人が文で直すと high で入り、空の HARD があれば 2 回目のまとめは「ここだけ」。"""
    st = H.new_state(form="C")
    _to(st, "hf_timing")
    H.turn(st, "そのうち", H.BANK, LLM(["x", "x"]))  # 読めない → ボタンで聞き直し(数えない)
    for _ in range(2):  # 1 回目の聞き返しは受ける。2 回目は答えにせず、空のまま先へ
        H.turn(st, "どういうこと?", H.BANK, LLM([{"slot_updates": [], "intent": "clarify"}]))
    assert "start_timing" not in st.slots and st.current["qid"] != "hf_timing"
    _to(st, "review")
    assert any(x.startswith("時期: 未定") for x in st.last_card["lines"])
    H.turn(st, "時期は来月から", H.BANK, LLM([_ext("start_timing", "来月から", review_ack="partial")]))
    assert st.slots["start_timing"]["text"] == "来月から" and st.slots["start_timing"]["confidence"] == "high"
    assert st.result is not None and st.review_rounds == 1  # 空の HARD が埋まったので 2 回目は無い


def test_aside_and_clarify_turns():
    """雑談は 3 回まで数えずに同じ問いへ戻る。聞き返し(clarify)は 1 問に 1 回、数えて同じ問いを出し直す。"""
    st = H.new_state(form="C")
    _to(st, "hf_area")
    turns = st.turns
    for _ in range(H.MAX_ASIDES):
        out = H.turn(st, "ところで天気は?", H.BANK, LLM([{"slot_updates": [], "intent": "aside"}],
                                                        [{"reply": "晴れです。" + "働きたい場所はどこですか?(駅や市の名前で)"}]))
        assert out["qid"] == "hf_area" and not out["counted"]
    assert st.turns == turns
    out = H.turn(st, "どういうこと?", H.BANK, LLM([{"slot_updates": [], "intent": "clarify"}]))
    assert out["qid"] == "hf_area" and H.CLARIFY_TEXT in out["reply"] and st.turns == turns + 1
    out = H.turn(st, "どういうこと?", H.BANK, LLM([{"slot_updates": [], "intent": "clarify"}]))
    assert out["qid"] == "hf_area" and H.RETRY_TEXT in out["reply"] and st.turns == turns + 1  # 2 回目: ボタンで聞き直す
    out = H.turn(st, "どういうこと?", H.BANK, LLM([{"slot_updates": [], "intent": "clarify"}]))
    assert out["qid"] != "hf_area" and "area" not in st.slots  # 3 回目: 先へ。空の HARD はまとめで「ここだけ」
    _to(st, "review")
    H.turn(st, H.REVIEW_OK, H.BANK, LLM())
    assert st.current["qid"] == "review" and st.last_card["focus"] and any(x.startswith("勤務地: 未定") for x in st.last_card["lines"])
