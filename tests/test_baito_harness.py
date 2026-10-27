"""バイト探しの相棒のハーネス(PRD v4.1。dip_mcp/baito)。LLM は偽物(呼ばれた順に台本の JSON を返す)。

(1) データとプロンプト: 質問バンク・職種カタログ(JSON)とプロンプト 4 種が、PRD の資料(docs/prd/baito_harness)の原文のまま。
    プロンプトに質問とカタログを書いていない
(2) PRD の検査: 移植版で単体チェック 131 件と 20 ペルソナ(evals/baito_sim.py)。基準値の範囲内
(3) 未成年: どの区分でも、年齢が分からない・未成年なら NG「深夜シフト」が付き、深夜の選択肢を出さず、深夜を推薦しない。
    中学生以下は推薦に進まない(抽出が失敗しても。レビュー M-4)
(4) 移植で直したところの回帰: M-16(「推し活・趣味」)、年齢の「18歳未満」、M-5・M-6・M-3・N-17・B-2
(5) ユーザーの決定: ng_removals、§12-2(解けない矛盾は「未定」と候補)
(6) dip の相談(session.py): モデルの出力を受け取りながら turn() をやり直す流れ、機能フラグ、保存
"""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest

from dip_mcp import logic
from dip_mcp.baito import harness as H
from dip_mcp.baito import session
from dip_mcp.data import ROOT, State
from evals import baito_sim

REF = ROOT / "docs" / "prd" / "baito_harness"
SEGMENTS = ("student", "homemaker", "side_job", "other", "unknown")
QM = H.qmap(H.BANK)


def _ref(name: str):
    """参照資料の付録(bank_v4 は catalog_v4 を import するので、資料のフォルダを path に足して読む)。"""
    sys.path.insert(0, str(REF))
    try:
        spec = importlib.util.spec_from_file_location(f"ref_{name}", REF / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(REF))


class LLM:
    """extract は台本(dict / 文字列 / 例外)を順に返す。generate は質問・カードをそのまま返し、結果は候補の上から選ぶ。"""

    def __init__(self, extracts=(), generates=()):
        self.extracts, self.generates = list(extracts), list(generates)
        self.calls: list[str] = []

    @staticmethod
    def _give(x):
        if isinstance(x, Exception):
            raise x
        return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)

    def extract(self, payload):
        self.calls.append("extract")
        return self._give(self.extracts.pop(0)) if self.extracts else json.dumps({"slot_updates": []})

    def generate(self, v):
        self.calls.append(H.prompt_stage(v))
        if self.generates:
            return self._give(self.generates.pop(0))
        if v.get("result_json"):
            r = json.loads(v["result_json"])
            recs = [{"job_id": c["job_id"], "reason": "合いそう"} for c in r["candidates"][:r["need"]]]
            return json.dumps({"reply": "どうぞ", "recommendations": recs}, ensure_ascii=False)
        if v["mode"] == "review":
            return json.dumps({"reply": v["card_text"] + "\n\n" + H.CLOSING_JA}, ensure_ascii=False)
        return json.dumps({"reply": v["text"]}, ensure_ascii=False)


def _ups(*ups, **kw) -> dict:
    return {"slot_updates": [{"slot": s, "value": v, "evidence": e} for s, v, e in ups], **kw}


def fresh(segment: str = "unknown") -> H.State:
    st = H.new_state()
    H.start(st, H.BANK)
    if segment != "unknown":
        H.set_segment(st, segment, "choice")
    return st


def goto(st: H.State, qid: str, mode: str = "ask") -> None:
    q = QM[qid]
    st.phase = q["phase"]
    st.current = {"qid": qid, "mode": mode, "choices": H.visible_choices(q, st)}
    if qid not in st.asked:
        st.asked.append(qid)


def finish(st: H.State, llm=None, choose=None, limit: int = 40) -> dict:
    """最後まで進める(ボタンは choose(qid, choices) か先頭、review は「これでOK」)。最後の turn() の結果。"""
    llm = llm or LLM()
    r = None
    for _ in range(limit):
        if st.result:
            return r
        cur = st.current
        text = H.REVIEW_OK if cur["mode"] == "review" else (choose and choose(cur["qid"], cur["choices"])) or cur["choices"][0]
        r = H.turn(st, text, H.BANK, llm)
    raise AssertionError("結果に着かない")


# ----------------------------- (1) データとプロンプト -----------------------------


def test_bank_and_catalog_json_are_the_prd_appendices_verbatim():
    """質問バンク(付録A、49 問)と職種カタログ(付録B、24 件)は、データファイル(JSON)に原文のまま。"""
    cat = _ref("catalog_v4")
    assert H.BANK == _ref("bank_v4").BANK and len(H.BANK) == 49
    assert H.CATALOG == cat.CATALOG and len(H.CATALOG) == 24  # times は集合で読む
    assert all(isinstance(j["times"], set) for j in H.CATALOG)


def test_prompts_are_the_prd_prompt_files_verbatim():
    for name in ("prompt_main_v4_ja.txt", "prompt_extract_v4_ja.txt", "prompt_review_v4_ja.txt", "prompt_result_v4_ja.txt"):
        assert (H.PROMPT_DIR / name).read_text(encoding="utf-8") == (REF / name).read_text(encoding="utf-8"), name


def test_prompts_do_not_contain_the_question_bank_or_the_catalog():
    """質問と職種はデータにだけある。プロンプトは変数で受け取る(PRD §0-1)。"""
    text = "".join((H.PROMPT_DIR / f).read_text(encoding="utf-8") for fs in H.PROMPT_FILES.values() for f in fs)
    words = {q["text"] for q in H.BANK} | {c for q in H.BANK for c in q["choices"] if len(c) >= 4} | {j["name"] for j in H.CATALOG}
    assert [w for w in words if w in text] == []
    for stage in H.PROMPT_FILES:  # 変数はすべてハーネスが埋める(足りなければ KeyError)
        st = fresh("student")
        v = H.extract_payload(st, H.BANK, "週3") if stage == "extract" else H.view(st, H.BANK)
        if stage == "review":
            st.phase, st.current, st.last_card = "review", {"qid": "review", "mode": "review", "choices": []}, H.build_card(st)
            v = H.view(st, H.BANK)
        if stage == "result":
            v = v | {"mode": "result", "result_json": "{}"}
        assert "{{" not in H.render_prompt(stage, v)


# ----------------------------- (2) PRD の検査 -----------------------------


@pytest.fixture(scope="module")
def reference():
    return baito_sim.check()


def test_port_passes_the_prd_unit_checks_and_the_20_personas(reference):
    """BAITO_HARNESS=aica_port で unit_checks_v4.py が 131/131、simulate.py v4 の v4_agg が全部 0 で、§11-5 の範囲内。"""
    r, bad = reference
    assert r["unit_summary"] == "131/131 passed", r["unit_fails"]
    assert bad == []
    agg = r["results"]["v4_agg"]
    assert agg["max_turns"] <= 15 and agg["not_reached"] == []
    assert all(v == 0 for k, v in agg.items() if k not in ("max_turns", "not_reached"))


def test_port_matches_the_reference_baseline_persona_by_persona(reference):
    """ターン数・フェーズの配分・review の回数・結果の推薦は、参照実装の基準値(results_v4.json)と同じ。"""
    r, _ = reference
    keys = ("turns", "per_phase", "review_rounds", "recs", "blocked", "unresolved", "mid_empty_end", "derived_ng")
    port = {m["persona"]: {k: m[k] for k in keys} for m in r["results"]["v4"]}
    base = {m["persona"]: {k: m[k] for k in keys} for m in r["baseline"]["v4"]}
    assert port == base


def test_the_baseline_files_are_not_overwritten(reference):
    """移植版のシミュレーションは写しの中で回す(レビュー M-10)。基準値のファイルは変わらない。"""
    r, _ = reference
    base = (REF / "transcripts" / "v4_hs16.txt").read_text(encoding="utf-8")
    assert r["transcripts"]["v4_hs16.txt"] == base and r["baseline"] == json.loads((REF / "results_v4.json").read_text())
    # N-17 で結果の文の「未定」が 1 行減ったところだけが、基準の会話ログと違う
    diff = [x for x in r["transcripts"] if r["transcripts"][x] != (REF / "transcripts" / x).read_text(encoding="utf-8")]
    assert diff == ["v4_spam_high.txt"]


def test_acceptance_catches_regressions(reference):
    r, _ = reference
    worse = json.loads(json.dumps(r["results"]))
    worse["v4_agg"]["minor_violation"] = 1
    worse["v4"][0]["turns"] += 2
    assert baito_sim.acceptance(worse, r["baseline"]) == ["minor_violation=1", "hs16: turns=15(基準 13)"]


# ----------------------------- (3) 未成年と中学生 -----------------------------


@pytest.mark.parametrize("segment", SEGMENTS)
def test_unknown_age_is_a_minor_in_every_segment(segment):
    """年齢をまだ答えていなければ未成年として扱う: NG「深夜シフト」が付き、深夜の選択肢を出さない。"""
    st = fresh(segment)
    assert H.minor_mode(st) and "深夜シフト" in st.derived_ng
    assert "深夜もOK" not in H.visible_choices(QM["hf_time"], st)
    assert "age_band" in H.active_slots(st) and H.active_slots(st)["age_band"] == H.HARD


@pytest.mark.parametrize("segment", SEGMENTS)
@pytest.mark.parametrize("age", ["15〜17歳", "ひみつ"])
def test_minor_or_declined_age_by_button_keeps_late_night_out(segment, age):
    """年齢のボタンが 15〜17歳・ひみつなら、深夜を文で言っても入らず、結果にも深夜専用・18歳以上の仕事が出ない。"""
    st = fresh(segment)
    goto(st, "hf_age")
    H.turn(st, age, H.BANK, LLM())
    assert "深夜シフト" in st.derived_ng and "深夜もOK" not in H.visible_choices(QM["hf_time"], st)
    goto(st, "hf_time")
    H.turn(st, "深夜もいけるよ", H.BANK, LLM([_ups(("time_slots", "深夜", "深夜"))]))
    assert "late_night" not in ((st.slots.get("time_slots") or {}).get("value") or [])
    r = finish(st)
    assert r["done"] and "深夜シフト" in st.derived_ng
    for rec in st.result["recommendations"]:
        job = H.CATALOG_BY_ID[rec["job_id"]]
        assert job["min_age"] < 18 and not job["night_only"] and job["phys"] != "heavy"


@pytest.mark.parametrize("segment", ("homemaker", "side_job", "other"))
def test_age_said_in_free_text_on_another_question_applies_in_every_segment(segment):
    """区分が学生でなくても、本人が文で言った年齢(根拠つき)は high で入り、深夜 NG が付く(v3 で漏れた区分)。"""
    st = fresh(segment)
    st.slots["age_band"] = {"value": "18_plus", "confidence": "low", "from": "x"}
    H.recompute_derived(st)
    goto(st, "sc_line")
    H.turn(st, "17歳だから夜はちょっと", H.BANK, LLM([_ups(("age_band", "17歳", "17歳"))]))
    assert st.slots["age_band"] == {"value": "15_17", "confidence": "high", "from": "free_other:sc_line"}
    assert "深夜シフト" in st.derived_ng and "深夜もOK" not in H.visible_choices(QM["hf_time"], st)


@pytest.mark.parametrize("segment", SEGMENTS)
def test_junior_high_by_button_is_stopped_before_any_recommendation(segment):
    st = fresh(segment)
    goto(st, "hf_age")
    r = H.turn(st, "中学生以下", H.BANK, LLM())
    assert r["done"] and st.blocked == "under_15" and st.result["recommendations"] == []
    assert r["reply"].startswith(H.NOTICE["under_15"])


@pytest.mark.parametrize("segment", SEGMENTS)
def test_junior_high_free_text_is_stopped_even_when_extraction_fails(segment):
    """レビュー M-4: 年齢の質問への「中2です」で抽出が 2 回とも失敗しても、中学生として止める(推薦に進まない)。"""
    st = fresh(segment)
    goto(st, "hf_age")
    llm = LLM([RuntimeError("timeout"), "壊れた出力"])
    r = H.turn(st, "中2です", H.BANK, llm)
    assert llm.calls[:2] == ["extract", "extract"]
    assert st.blocked == "under_15" and r["done"] and st.result["recommendations"] == []
    assert st.slots["age_band"] == {"value": "under_15", "confidence": "high", "from": "free:hf_age"}
    assert any(e["kind"] == "jhs_fallback" and e.get("extract_failed") for e in st.log)


def test_junior_high_free_text_on_the_reask_is_also_stopped():
    """fill_gaps の言い換え(reask)で聞いた年齢も同じ。"""
    st = fresh("student")
    goto(st, "hf_age", mode="reask")
    st.phase = "fill_gaps"
    H.turn(st, "中3だよ", H.BANK, LLM(["x", "y"]))
    assert st.blocked == "under_15"


def test_family_member_in_junior_high_is_not_the_users_age():
    """年齢を聞いていない質問の「子どもが中学生」は年齢にしない(抽出が失敗しても)。"""
    st = fresh("homemaker")
    goto(st, "sc_line")
    H.turn(st, "子どもが中学生で", H.BANK, LLM(["x", "y"]))
    assert st.blocked is None and "age_band" not in st.slots


def test_guessed_junior_high_is_stopped_at_the_result():
    st = fresh("student")
    H.apply_update(st, "age_band", "under_15", "low", "free_other:sc_line")
    assert st.blocked is None and H.minor_mode(st)
    H.finalize(st)
    assert st.blocked == "under_15"


@pytest.mark.parametrize(("said", "extracted", "band"), [
    ("中3で15歳です", "15_17", "under_15"),       # 本人の学年が中学生なら、抽出の年齢より優先する
    ("中2", "わからない", "under_15"),             # 抽出の値が読めない
    ("中学の先生をしてる35歳", "35歳", "18_plus"),  # 本人の学年ではない
    ("中学卒業して16歳", "16歳", "15_17"),
])
def test_junior_high_on_the_age_question_overrides_a_wrong_extraction(said, extracted, band):
    st = fresh("student")
    goto(st, "hf_age")
    H.turn(st, said, H.BANK, LLM([_ups(("age_band", extracted, extracted))]))
    assert st.slots["age_band"]["value"] == band and (st.blocked == "under_15") == (band == "under_15")


def test_junior_high_said_in_review_is_stopped_when_extraction_fails():
    st = fresh("student")
    H.apply_update(st, "age_band", "18_plus", "high", "choice:hf_age")
    st.phase, st.current, st.last_card = "review", {"qid": "review", "mode": "review", "choices": [H.REVIEW_OK]}, H.build_card(st)
    st.turns = 13
    r = H.turn(st, "ごめん本当は中2です", H.BANK, LLM(["x", "y", "x", "y"]))
    assert r["counted"] and r["done"]  # 読めない返事でもカードを出し直さず(§5-5 の前に)、中学生として止める
    assert st.blocked == "under_15" and st.result and st.result["recommendations"] == []


def test_review_mention_of_a_child_in_junior_high_is_not_the_users_age():
    st = fresh("homemaker")
    H.apply_update(st, "age_band", "18_plus", "high", "choice:hf_age")
    st.phase, st.current, st.last_card = "review", {"qid": "review", "mode": "review", "choices": [H.REVIEW_OK]}, H.build_card(st)
    H.turn(st, "子どもが中2なので夕方まで", H.BANK, LLM(["x", "y"]))
    assert st.blocked is None and st.slots["age_band"]["value"] == "18_plus"


def test_junior_high_block_is_not_lifted_by_a_grade_said_with_an_age():
    st = fresh("student")
    goto(st, "hf_age")
    H.turn(st, "中学生以下", H.BANK, LLM())
    H.turn(st, "中3だけど16になった", H.BANK, LLM([_ups(("age_band", "16", "16"))]))
    assert st.blocked == "under_15" and st.result["recommendations"] == []


def test_conflicting_guesses_of_the_age_keep_minor_mode():
    """推測どうしで年齢が食い違ったら(どっち？)、最後の推測が 18歳以上でも未成年として扱い、中学生以下の候補があれば止める。"""
    st = fresh("side_job")
    H.apply_update(st, "age_band", "15_17", "low", "free_other:a")
    H.apply_update(st, "age_band", "18_plus", "low", "free_other:b")
    assert st.slots["age_band"]["value"] == "18_plus" and H.minor_mode(st) and "深夜シフト" in st.derived_ng
    assert "深夜もOK" not in H.visible_choices(QM["hf_time"], st)
    st2 = fresh("other")
    H.apply_update(st2, "age_band", "under_15", "low", "free_other:a")
    H.apply_update(st2, "age_band", "18_plus", "low", "free_other:b")
    H.finalize(st2)
    assert st2.blocked == "under_15"


@pytest.mark.parametrize(("said", "band"), [
    ("18歳未満です", "under_18"), ("まだ18歳になっていません", "under_18"), ("来月18歳になります", "under_18"),
    ("20歳未満", "under_18"), ("未成年です", "under_18"), ("17歳だけど来月18歳", "15_17"), ("16", "15_17"),
    ("高2", "15_17"), ("中1", "under_15"), ("18歳です", "18_plus"), ("19歳になったばかり", "18_plus"),
    ("18歳以上", "18_plus"), ("大学2年", "18_plus"), ("ひみつ", "declined"),
    # 学校の語より「〜歳」を先に読む。進学の予定・生年・学年だけは決めない(未成年のまま年齢を聞く)
    ("17歳、大学志望", "15_17"), ("会社員だけど17歳", "15_17"), ("社会人1年目、16歳", "15_17"), ("大学受験生です", None),
    ("4月から大学生", None), ("2009年生まれ", None), ("H21生まれ", None), ("1年生です", None), ("高専1年", "15_17"),
    ("成人してない", "under_18"), ("まだ大人じゃない", "under_18"), ("18歳まであと半年", "under_18"),
    ("あと3か月で18", "under_18"), ("今年で18", "under_18"), ("18歳以下", "under_18"), ("社会人です", "18_plus"),
])
def test_age_said_in_words_is_read_on_the_safe_side(said, band):
    """PRD §6-3 の表のままだと「18歳未満です」「17歳、大学志望」が 18_plus になる。未成年を成人と読まない。"""
    assert H.normalize_value("age_band", said) == band


def test_minor_who_says_not_18_yet_in_review_gets_the_late_night_ng():
    st = fresh("side_job")
    H.apply_update(st, "age_band", "18_plus", "low", "free_other:x")
    H.apply_update(st, "time_slots", ["late_night"], "low", "free_other:x")
    st.phase, st.current, st.last_card = "review", {"qid": "review", "mode": "review", "choices": [H.REVIEW_OK]}, H.build_card(st)
    st.turns = 13
    H.turn(st, "まだ18歳になってないよ", H.BANK, LLM([_ups(("age_band", "まだ18歳になってない", "まだ18歳になってない"),
                                                      review_ack="partial")]))
    assert st.slots["age_band"]["value"] == "under_18" and "深夜シフト" in st.derived_ng
    assert "late_night" not in ((st.slots.get("time_slots") or {}).get("value") or [])


# ----------------------------- (4) 移植で直したところ -----------------------------


@pytest.mark.parametrize(("value", "want"), [
    ("推し活・趣味", ["hobby"]), ("生活費・家計の足し", ["living_cost"]), ("経験・スキル", ["skill"]),
    ("人とのつながり", ["people"]), ("hobby", ["hobby"]), ("貯金、旅行・遊び", ["saving", "fun"]),
    (["推し活・趣味", "貯金"], ["hobby", "saving"]), ("推し活・趣味と貯金", ["hobby", "saving"]),
    ("お金と人とのつながり", ["money", "people"]), ("旅行・遊び&まかない", ["fun", "perk_meal"]),
])
def test_multi_value_display_names_with_separators_are_not_split(value, want):
    """レビュー M-16: 区切り文字(・と)を含む表示名を、文のまま来ても壊さない。"""
    assert H.normalize_value("purpose", value) == want


def test_purpose_display_name_typed_as_free_text_is_stored_not_dropped():
    st = fresh()
    goto(st, "q_money")
    H.turn(st, "推し活・趣味かな", H.BANK, LLM([_ups(("purpose", "推し活・趣味", "推し活・趣味"))]))
    assert st.slots["purpose"]["value"] == ["hobby"] and "purpose" not in st.notes


def test_age_is_applied_before_other_slots_in_one_extraction():
    """レビュー M-5: 「20歳で深夜もOK」で深夜が年齢より先に来ても、未成年として深夜を捨てない。"""
    st = fresh("other")
    goto(st, "hf_time")
    H.turn(st, "20歳で深夜もOK", H.BANK, LLM([_ups(("time_slots", "深夜", "深夜"), ("age_band", "20歳", "20歳"))]))
    assert st.slots["time_slots"]["value"] == ["late_night"] and st.derived_ng == []


def test_review_free_text_with_two_values_keeps_the_last_one_high_and_logs_it():
    """レビュー B-2: review の自由入力は最後の値を high で入れ(参照実装どおり)、曖昧だったことをログに残す。"""
    st = fresh("student")
    H.apply_update(st, "age_band", "18_plus", "high", "t")
    st.phase, st.current, st.last_card = "review", {"qid": "review", "mode": "review", "choices": [H.REVIEW_OK]}, H.build_card(st)
    H.turn(st, "通勤は徒歩、いや電車30分", H.BANK, LLM([_ups(("commute", "徒歩", "徒歩"), ("commute", "電車30分", "電車30分"))]))
    assert st.slots["commute"] == {"value": "train_30", "confidence": "high", "from": "review"}
    assert any(e["kind"] == "ambiguous_extract" and e["slot"] == "commute" for e in st.log)


def test_unblocking_at_the_turn_cap_does_not_count_past_15():
    """レビュー M-6: 15 ターンで中学生として止めた後の年齢の訂正は数えず、そのまま結果を作り直す。"""
    st = fresh("student")
    st.turns, st.phase_turns = 14, {"hard_filter": 14}
    goto(st, "hf_age")
    H.turn(st, "中学生以下", H.BANK, LLM())
    assert st.blocked and st.turns == 15
    r = H.turn(st, "ごめん、本当は16歳", H.BANK, LLM([_ups(("age_band", "16歳", "16歳"))]))
    assert st.turns == 15 and st.blocked is None and r["done"] and st.result["recommendations"]
    assert "深夜シフト" in st.derived_ng


def test_round_two_promotes_only_the_items_shown_on_the_card():
    """レビュー M-3: 2 巡目のカードは focus の項目だけを出すので、上げる(確定する)のもその項目だけ。"""
    st = fresh("student")
    H.apply_update(st, "age_band", "18_plus", "high", "t")
    st.review_rounds = 1
    H.apply_update(st, "commute", "walk_bike", "low", "free_other:x")
    H.apply_update(st, "pace", "calm", "low", "choice:sc_line")
    st.last_card = H.build_card(st)
    assert "commute" in st.last_card["focus"] and "pace" not in st.last_card["focus"]
    H.apply_review(st, None, button_ok=True)
    assert st.slots["commute"]["confidence"] == "high" and st.slots["pace"]["confidence"] == "low"


def test_unresolved_hard_is_listed_once_with_its_candidates():
    """§12-2(ユーザーの決定)と N-17: 解けない矛盾は値を消して「未定」にし、候補を並べる。同じ項目を 2 行にしない。
    推薦の絞り込みでは空として扱う。"""
    st = fresh("student")
    H.apply_update(st, "age_band", "18_plus", "high", "t")
    H.apply_update(st, "commute", "walk_bike", "low", "a")
    H.apply_update(st, "commute", "train_60", "low", "b")
    H.apply_update(st, "customer_facing", "like", "low", "a")
    H.apply_update(st, "customer_facing", "avoid", "low", "b")
    H.finalize(st)
    assert "commute" not in st.slots and st.unresolved["commute"] == ["walk_bike", "train_60"]
    p = H.build_result_payload(st, H.CATALOG)
    assert "commute" in p["unknown_hard_slots"]  # M-silent の検査にはそのまま残す
    txt = H.compose_result_text(p, H.fallback_result(p), H.CATALOG_BY_ID)
    assert "未定：通勤（徒歩・自転車 / 1時間くらいまで のどちらか。決まったら教えてね）" in txt
    assert "未定：接客（好き / できれば避けたい のどちらか。決まったら教えてね）" in txt
    assert txt.count("通勤") == 1
    # 接客は空として扱う(「避けたい」で接客の多い仕事を外さない)
    assert any(H.CATALOG_BY_ID[c["job_id"]]["cf"] == "high" for c in p["candidates"])


# ----------------------------- (5) ng_removals -----------------------------


def test_ng_removals_remove_user_ng_but_never_the_minor_rule_ng():
    st = fresh("student")
    goto(st, "rp_student")
    H.turn(st, "居酒屋はイヤ", H.BANK, LLM([{"slot_updates": [], "ng_updates": ["居酒屋"]}]))
    assert st.user_ng == ["居酒屋"]
    goto(st, "hf_days")
    H.turn(st, "やっぱ居酒屋も深夜もOK", H.BANK, LLM([{"slot_updates": [], "ng_removals": ["居酒屋", "深夜シフト"]}]))
    assert st.user_ng == [] and "深夜シフト" in st.derived_ng


def test_rule_ng_goes_away_when_the_age_is_corrected_to_adult():
    """年齢を 18 歳以上に直すと、自動で付いた NG(深夜シフト・重量物)は外れる。"""
    st = fresh("student")
    H.apply_update(st, "age_band", "15_17", "high", "choice:hf_age")
    st.phase, st.current, st.last_card = "review", {"qid": "review", "mode": "review", "choices": [H.REVIEW_OK]}, H.build_card(st)
    H.turn(st, "19歳です", H.BANK, LLM([_ups(("age_band", "19歳", "19歳"), review_ack="partial")]))
    assert st.slots["age_band"]["value"] == "18_plus" and st.derived_ng == []


# ----------------------------- (6) dip の相談(session.py) -----------------------------


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")
    s = State(tmp_path / "state.json")
    s.reset()
    return s


UID = "sim-new"


def _drive(state, text, outputs):
    """runtime と同じ: status=llm の間、台本の出力を 1 つずつ足して呼び直す。返すのは (最後の結果, 求められた段の列)。"""
    got, stages = [], []
    r = session.turn(state, UID, text)
    while r.get("status") == "llm":
        stages.append(r["call"]["stage"])
        got.append(outputs.pop(0))
        r = session.turn(state, UID, text, got)
    return r, stages


def _say(text):
    return {"output": {"reply": text}}


def test_session_start_needs_the_flag(tmp_path, monkeypatch):
    monkeypatch.delenv(logic.BAITO_HARNESS_FLAG, raising=False)
    s = State(tmp_path / "state.json")
    s.reset()
    r = session.start(s, UID)
    assert r["active"] is False and "error" in r and session.KEY not in s.user(UID)
    assert "相談の進め方(ハーネス)" not in logic.counseling_playbook("アルバイト・パート")["playbook"]
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")
    assert "start_baito_consultation" in logic.counseling_playbook("アルバイト・パート")["playbook"]
    assert "start_baito_consultation" not in logic.counseling_playbook("アルバイト・パート", "voice")["playbook"]
    assert "start_baito_consultation" not in logic.counseling_playbook("正社員")["playbook"]


def test_session_replays_turn_with_the_model_outputs_and_saves_only_at_the_end(state):
    r = session.start(state, UID)
    assert r["question"] == {"id": "q_money", "text": "バイト代、何に使いたい？", "choices": QM["q_money"]["choices"]}
    saved = json.dumps(state.user(UID)[session.KEY], ensure_ascii=False)
    # ボタン: 抽出は呼ばず、言い方を 1 回だけ求める
    first = session.turn(state, UID, "推し活・趣味")
    assert first["status"] == "llm" and first["call"]["stage"] == "say"
    assert "今のあなたに近いのは？" in first["call"]["system"] and first["call"]["user_text"] == "推し活・趣味"
    assert json.dumps(state.user(UID)[session.KEY], ensure_ascii=False) == saved  # 途中では保存しない
    r = session.turn(state, UID, "推し活・趣味", [_say("推し活いいね！今のあなたに近いのは？")])
    assert r["status"] == "reply" and r["reply"] == "推し活いいね！今のあなたに近いのは？"
    assert r["choices"] == QM["q_segment"]["choices"] and r["question"] == "q_segment" and r["llm_calls"] == 1
    # 文: 抽出 → 言い方。抽出のプロンプトに本人の発言が入る。年齢は根拠つきで high、未成年の注意文はハーネスが先に付ける
    ext = {"output": {"slot_updates": [{"slot": "age_band", "value": "高1", "evidence": "高1"}], "ng_updates": [],
                      "ng_removals": [], "segment": "student", "intent": "answer", "review_ack": None}}
    r1 = session.turn(state, UID, "高1です！")
    assert r1["call"]["stage"] == "extract" and "ユーザーの発言: 高1です！" in r1["call"]["system"]
    r, stages = _drive(state, "高1です！", [ext, _say("高1なんだ！学校のバイトのルールは？")])
    assert stages == ["extract", "say"]
    assert r["reply"] == H.NOTICE["minor_rules"] + "\n高1なんだ！学校のバイトのルールは？" and r["question"] == "hf_school"
    h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
    assert h.slots["age_band"]["value"] == "15_17" and h.segment == "student" and "深夜シフト" in h.derived_ng


def test_session_model_errors_fall_back_to_the_question_text(state):
    session.start(state, UID)
    r, stages = _drive(state, "旅行・遊び", [{"error": "APIError"}, _say("扶養は103万円までだよ")])
    assert stages == ["say", "say"] and r["reply"] == "今のあなたに近いのは？"


def test_session_result_saves_the_conditions_with_the_rule_ng_and_closes(state):
    session.start(state, UID)
    picks = {"q_segment": "学生", "hf_age": "15〜17歳"}
    r = {"status": "reply", "choices": QM["q_money"]["choices"], "question": "q_money"}
    for _ in range(30):
        if r["status"] == "result":
            break
        text = picks.get(r["question"]) or r["choices"][0]
        r = session.turn(state, UID, text)
        while r.get("status") == "llm":  # 偽のモデル: 求められたものを、ハーネスの代わりの文で返す(検証で落ちれば原文)
            r = session.turn(state, UID, text, [{"error": "x"}] * (r["call"]["n"] + 1))
    assert r["status"] == "result" and r["choices"] == session.RESULT_CHOICES and session.KEY not in state.user(UID)
    prefs = state.user(UID)["preferences"]
    assert "深夜シフト" in prefs["avoids"]["value"] and "重量物・危険な作業" in prefs["avoids"]["value"]
    assert "1. " in r["reply"] and H.NOTICE["minor_rules"] in r["reply"]


def test_session_keeps_a_junior_high_block_open_and_lifts_it_on_an_age_fix(state):
    session.start(state, UID)
    h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
    goto(h, "hf_age")
    session._save(state, UID, h)
    r = session.turn(state, UID, "中学生以下")
    assert r["status"] == "result" and r["blocked"] == "under_15" and r["choices"] == []
    assert r["reply"].startswith(H.NOTICE["under_15"]) and session.active(state, UID)
    fix = {"output": {"slot_updates": [{"slot": "age_band", "value": "16歳", "evidence": "16歳"}], "ng_updates": [],
                      "ng_removals": [], "segment": None, "intent": "answer", "review_ack": None}}
    r, stages = _drive(state, "ごめん本当は16歳", [fix, {"error": "x"}, {"error": "x"}])
    assert stages == ["extract", "say", "say"] and r["status"] == "reply" and session.active(state, UID)
    assert r["reply"].endswith(QM[r["question"]]["text"])
    assert H.State.from_dict(state.user(UID)[session.KEY]["harness"]).blocked is None


def test_session_exit_word_saves_answered_conditions_and_hands_back(state):
    session.start(state, UID)
    session.turn(state, UID, "貯金・将来のため", [_say("いいね")])
    r = session.turn(state, UID, "今はいい")
    assert r == {"active": False, "ended": "exit", "saved": ["avoids"]} and not session.active(state, UID)
    assert "深夜シフト" in state.user(UID)["preferences"]["avoids"]["value"]


def test_session_drops_a_saved_state_from_another_version(state):
    state.update(UID, lambda u: u.__setitem__(session.KEY, {"harness": {"phase": "hard_filter"}, "track": None}))
    assert session.turn(state, UID, "はい") == {"active": False} and session.KEY not in state.user(UID)


def test_session_free_text_with_every_model_call_failing_ends_in_four_calls(state):
    """文の答えでモデルが毎回失敗しても、1 ターンの呼び出しは抽出 2 回 + 生成 2 回まで(PRD §5-4)。質問の原文で聞き直す。"""
    session.start(state, UID)
    r, stages = _drive(state, "なんだろう", [{"error": "x"}] * session.MAX_LLM_CALLS)
    assert stages == ["extract", "extract", "say", "say"] and r["status"] == "reply"
    assert r["reply"] == H.RETRY_TEXT + QM["q_money"]["text"] and r["counted"] is False


def test_junior_high_block_outlives_the_consultation_and_stops_job_search(state, monkeypatch):
    """中学生以下で止めた後に「やめる」・新しい会話で相談を閉じても、トークの AI が探す求人は出ない(印が残る)。
    後の相談で 15 歳以上と確かめたら出す。"""
    from dip_mcp import server

    monkeypatch.setattr(server, "state", state)
    session.start(state, UID)
    h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
    goto(h, "hf_age")
    session._save(state, UID, h)
    session.turn(state, UID, "中学生以下")
    assert session.turn(state, UID, "やめる")["ended"] == "exit" and not session.active(state, UID)
    for got in (server.recommend_jobs(line_user_id=UID, area="新宿"), server.relax_conditions(line_user_id=UID)):
        assert got["jobs"] == [] and got["blocked"] == "under_15" and got["notice"] == H.NOTICE["under_15"]
    session.start(state, UID)
    h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
    goto(h, "hf_age")
    session._save(state, UID, h)
    assert session.turn(state, UID, "15〜17歳", [{"error": "x"}] * 2)["status"] == "reply"
    session.end(state, UID)
    assert session.job_block(state, UID) is None and server.recommend_jobs(line_user_id=UID, area="新宿")["jobs"]


def test_rule_ng_is_saved_even_when_the_previous_avoids_are_long(state):
    long = "、".join(f"苦手なこと{i}番目のもの" for i in range(8))
    logic.save_preferences(state, UID, {"avoids": long})
    session.start(state, UID)
    session.turn(state, UID, "今はいい")
    avoids = state.user(UID)["preferences"]["avoids"]["value"]
    assert avoids.startswith("深夜シフト、重量物・危険な作業、苦手なこと0番目") and len(avoids) <= logic.PREFERENCE_VALUE_MAX


def test_replayed_outputs_must_match_what_the_harness_asks_for(state):
    session.start(state, UID)
    with pytest.raises(RuntimeError, match="合いません"):  # 言い方を求めているのに、抽出の出力が来た
        session.turn(state, UID, "推し活・趣味", [{"stage": "extract", "output": {}}])
    with pytest.raises(RuntimeError, match="多く"):  # 求めより多い(呼び直しの間に状態が変わった)
        session.turn(state, UID, "推し活・趣味", [{"stage": "say", "output": {"reply": "x"}}] * 3)
    assert H.State.from_dict(state.user(UID)[session.KEY]["harness"]).turns == 0


# ----------------------------- P3 の入口(レビュー B-3) -----------------------------


def test_real_llm_runner_counts_first_calls_and_records_the_cost(tmp_path):
    """--real の道具立てを、偽のモデルで通す(API は呼ばない): 18 人から選んだペルソナが結果に着き、段ごとの 1 回目の
    呼び出しと合否を数え、台帳に書く。偽のモデルは抽出を空で返し、言い方は検証に落ちる(→ 代替)。"""
    async def fake(stage, system, messages):
        out = {"extract": {"slot_updates": [], "ng_updates": [], "ng_removals": [], "segment": None, "intent": "answer",
                           "review_ack": None}, "result": {"reply": "x", "recommendations": []}}.get(stage, {"reply": ""})
        return out, {"input_tokens": 0, "output_tokens": 0}

    ledger = tmp_path / "budget.jsonl"
    out = baito_sim.run_real(1, ledger, 1.0, call=fake, only=["nopref", "malformed"])
    assert out["conversations"] == 1 and out["results"][0]["persona"] == "nopref" and out["results"][0]["reached"]
    st = out["stats"]
    assert st["generate"]["first"] == st["generate"]["fallback"] and st["generate"]["calls"] == 2 * st["generate"]["first"]
    assert out["rates"]["extract_pass"] == 1.0 and out["rates"]["generate_pass"] == 0.0
    assert "generate_pass=0.0(基準 ≧ 0.95)" in out["problems"] and ledger.read_text().count("baito-p3") == 1
    assert "aica_port" not in sys.modules or sys.modules["aica_port"] is H
