"""バイト探しの相棒のハーネス(dip_mcp/baito)のテスト。実 LLM なし。

確かめること:
(1) 質問バンク(bank.json)とプロンプトが、PRD の資料(docs/prd/baito_harness)の原文のまま。プロンプトに質問を書いていない
(2) 8ペルソナの回帰(evals.baito_sim)が PRD §11.3 の受け入れチェックに通り、§4.1 のベースラインより悪くならない
(3) コンプライアンスをコードで強制する: 未成年 → NG「深夜シフト」、segment が分からなくても年齢を聞く、深夜の時間帯を残さない
(4) PRD §12 の 1・3・4・5 への対応と、2 を実装していないこと(v3 のまま)
(5) dip の状態に保存して進める相談(session): 選択肢・文の読み取り・やめる合図・結果の保存・プロンプトの変数
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from dip_mcp.baito import harness as H
from dip_mcp.baito import session
from dip_mcp.baito.bank import load_bank, validate
from dip_mcp.data import ROOT, State
from evals import baito_sim

PRD_DIR = ROOT / "docs" / "prd" / "baito_harness"
PROMPTS = ROOT / "dip_mcp" / "prompts"


def _prd_module(name: str):
    spec = importlib.util.spec_from_file_location(f"prd_{name}", PRD_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _out(*ups, ng=(), seg="unknown", removals=()):
    return {"slot_updates": [{"slot": s, "value": v, "confidence": c, "from": "t"} for s, v, c in ups],
            "ng_updates": list(ng), "segment": seg, "ng_removals": list(removals)}


# ----------------------------- (1) 原文のまま -----------------------------


def test_bank_json_is_the_prd_bank_verbatim():
    prd = _prd_module("bank").make_bank()
    ours = load_bank()
    assert len(ours) == len(prd) == 48
    for a, b in zip(ours, prd, strict=True):
        assert a == b  # id・phase・segments・targets・質問文・選択肢(順も)・choice_effects
    assert validate(ours) == []


def test_bank_counts_match_prd_7_2():
    counts = {}
    for q in load_bank():
        for seg in q["segments"]:
            counts[(q["phase"], seg)] = counts.get((q["phase"], seg), 0) + 1
    assert counts == {("icebreak", "all"): 2, ("hard_filter", "all"): 3, ("hard_filter", "student"): 2,
                      ("hard_filter", "homemaker"): 2, ("hard_filter", "side_job"): 2, ("scenario_game", "all"): 7,
                      ("scenario_game", "student"): 6, ("scenario_game", "homemaker"): 6, ("scenario_game", "side_job"): 6,
                      ("reverse_pick", "student"): 1, ("reverse_pick", "homemaker"): 1, ("reverse_pick", "side_job"): 1,
                      ("fill_gaps", "all"): 5, ("fill_gaps", "student"): 1, ("fill_gaps", "homemaker"): 2,
                      ("fill_gaps", "side_job"): 1}


def test_bank_validation_catches_broken_questions():
    q = {"id": "x", "phase": "scenario_game", "segments": ["all"], "targets": ["pace"], "text": "?",
         "choice_effects": {"a": {"pace": "calm"}}}
    assert any("選択肢が 1 個" in e for e in validate([q]))
    q2 = q | {"choice_effects": {"a": {"pace": "calm"}, "b": {"shift_length": "short"}}}
    assert any("targets" in e for e in validate([q2]))


def test_main_prompt_is_prd_9_1_and_review_prompt_is_verbatim():
    prd = (PRD_DIR / "PRD_baito_agent.md").read_text(encoding="utf-8")
    block = prd.split("### 9.1 メインのシステムプロンプト")[1].split("```text\n", 1)[1].split("\n```", 1)[0]
    assert (PROMPTS / "baito_main_ja.md").read_text(encoding="utf-8") == block + "\n"
    assert ((PROMPTS / "baito_review_ja.md").read_text(encoding="utf-8")
            == (PRD_DIR / "prompt_review_ja.txt").read_text(encoding="utf-8"))
    # 9.1 のルール 1〜9 と出力の JSON の契約
    for rule in ("1. 1ターンに質問は1つだけ。", "5. 「どっちでもない」と言われたら、軽く1回だけ聞く：「お、どのへんがちがう？一言で教えて！」",
                 "8. 面接っぽい言い方", "9. ユーザーがはっきり言っていないことを事実として記録しない。",
                 '{"reply": "...", "choices": ["..."],'):
        assert rule in block


def test_prompts_do_not_contain_the_question_bank():
    """質問はデータ(bank.json)にだけある。プロンプトのファイルに質問文と選択肢を書かない(P0-4)。"""
    text = "".join((PROMPTS / n).read_text(encoding="utf-8") for n in ("baito_main_ja.md", "baito_notes_ja.md", "baito_review_ja.md"))
    for q in load_bank():
        assert q["text"] not in text, q["id"]
        if q["id"] == "q_segment":
            continue  # 属性の名前(学生／家のこと中心／本業あり副業探し)は、§9.1 と §9.2 の原文が役割の説明に使う
        for c in q["choices"]:
            if len(c) >= 4:  # 「OK」「はい」のような短い語は、ふつうの文にも出る
                assert c not in text, (q["id"], c)


# ----------------------------- (2) 8ペルソナの回帰 -----------------------------


@pytest.fixture(scope="module")
def results():
    return [baito_sim.run(p) for p in baito_sim.PERSONAS]


def test_eight_personas_pass_the_prd_acceptance_checks(results):
    assert baito_sim.acceptance(results) == []
    for m in results:
        assert m["turns"] <= H.MAX_TURNS and m["reached"] and not m["loop"] and m["crash"] is None
        assert m["review_rounds"] >= 1 and m["empty"]["HARD"] == [] and m["low"]["HARD"] == []
        assert m["irrelevant"] == [] and m["dropped"] == 0 and m["wasted"] == []


def test_personas_match_the_v3_baseline(results):
    """§4.1 の表と同じ(良くなったところ以外)。ターン数、フェーズごとのターン数、review の回数、空の MID。"""
    for m in results:
        base = baito_sim.BASELINE[m["persona"]]
        assert m["turns"] == base["turns"], m["persona"]
        assert baito_sim.phases(m) == base["phases"], m["persona"]
        assert m["review_rounds"] == base["review_rounds"], m["persona"]
        assert m["empty"]["MID"] == base["empty_mid"], m["persona"]
    assert {m["persona"]: m["final_seg"] for m in results} == {
        "hs16": "student", "univ": "student", "homemaker": "homemaker", "office": "side_job", "nopref": "student",
        "unclear": "side_job", "late_seg": "homemaker", "no_seg": "student"}


def test_acceptance_catches_regressions(results):
    """チェック自体が退化を見つける(シナリオが v2a の2ターンに縮んだ、HARD が空、未成年に深夜 NG が無い)。"""
    bad = [dict(m) for m in results]
    bad[0] = bad[0] | {"per_phase": bad[0]["per_phase"] | {"scenario_game": 2}, "late_night_ng": False}
    bad[1] = bad[1] | {"empty": {"HARD": ["commute"], "MID": []}}
    found = baito_sim.acceptance(bad)
    assert any("hs16: シナリオのターンが減った" in v for v in found)
    assert any("深夜シフト" in v for v in found)
    assert any("univ: 空か low の HARD" in v for v in found)


def test_hs16_gets_the_late_night_ng_and_never_sees_the_late_night_choice(results):
    hs16 = next(m for m in results if m["persona"] == "hs16")
    assert hs16["late_night_ng"] and hs16["late_night_choice_shown"] == []


# ----------------------------- (3) コンプライアンス -----------------------------


def test_age_before_segment_is_buffered_and_adds_the_late_night_ng():
    """FIX3 + FIX8(PRD の unit_checks.py)。segment が決まる前の年齢も捨てない。文字列の "true" でも深夜 NG。"""
    s = H.State(segment="unknown")
    s.slots.clear()
    H.merge_updates(s, _out(("school_permission", "ok", "high")))  # unknown にはまだ無いスロット: 預ける
    assert s.pending and "school_permission" not in s.slots
    H.merge_updates(s, _out(seg="student"))
    assert s.slots["school_permission"]["value"] == "ok" and s.pending == []
    for v in ("true", "はい", "True", True):
        s = H.State(segment="student")
        H.merge_updates(s, _out(("age_under_18", v, "high")))
        assert s.ng == [H.LATE_NIGHT_NG], v


def test_unknown_segment_still_asks_the_age():
    """FIX7: segment が最後まで分からなくても hf_age を出す(no_seg)。"""
    no_seg = next(p for p in baito_sim.PERSONAS if p.name == "no_seg")
    log: list[str] = []
    baito_sim.run(no_seg, log=log)
    assert any("hf_age" in line and "[hard_filter]" in line for line in log)


def test_minor_cannot_keep_a_late_night_time_slot():
    """先に「深夜もOK」と答えてから18歳未満と分かっても、深夜の時間帯は残さず、深夜を隠して聞き直す。"""
    s = H.State(segment="student", phase="fill_gaps")
    H.merge_updates(s, _out(("days_per_week", "1-2", "high"), ("commute", "walk_bike", "high"),
                            ("school_permission", "ok", "high"), ("time_slots", "late_night_ok", "high")))
    H.merge_updates(s, _out(("age_under_18", True, "high")))
    assert "time_slots" not in s.slots and s.rejected[0]["value"] == "late_night_ok"
    assert H.LATE_NIGHT_NG in s.ng
    q = H.step(s, None, H.BANK)
    assert q["id"] == "hf_time" and "深夜もOK" not in q["choices"] and q["choices"] == ["朝", "昼", "夕方〜夜"]
    # 文で言われた深夜も受け付けない(モデルの返り値によらない)
    H.merge_updates(s, _out(("time_slots", "深夜でも大丈夫", "high")))
    assert "time_slots" not in s.slots


@pytest.mark.parametrize("said", ["22時以降もOK", "23時まで", "夜10時から", "22時半まで", "夜中の2時",
                                  # 文末の「ない」で寄せない(NEGATION)肯定の言い方も、深夜の語があれば受け付けない
                                  "深夜も問題ないです", "22時以降でも問題ないです", "夜中でもかまわない",
                                  "深夜は無理"])  # 否定でも捨てる(深夜を隠した選択肢で聞き直すだけ)
def test_minor_cannot_keep_a_late_night_time_said_as_a_clock_time(said):
    """深夜もOK の選択肢を隠すので、モデルは本人の文(時刻)をそのまま返しやすい。時刻で言っても深夜は受け付けない。"""
    s = H.State(segment="student", phase="fill_gaps")
    H.merge_updates(s, _out(("age_under_18", True, "high"), ("time_slots", said, "high")))
    assert "time_slots" not in s.slots and s.rejected[-1]["raw_text"] == said
    assert "深夜" not in session.result_preferences(s).get("time_band", "")
    assert not any("時間帯" in line and "未定" not in line for line in H.render_card_ja(H.build_card(s), final=True).split("\n"))


def test_minor_late_night_text_said_before_the_age_is_dropped():
    """年齢より先に文で届いた深夜(列挙値に寄らない)も、未成年と分かったら消し、矛盾の候補からも外す。"""
    s = H.State(segment="student")
    H.merge_updates(s, _out(("time_slots", "evening", "low")))
    H.merge_updates(s, _out(("time_slots", "夜中でもかまわない", "low")))
    assert s.conflicts == {"time_slots": ["evening", "夜中でもかまわない"]}
    H.merge_updates(s, _out(("age_under_18", True, "high")))
    assert s.conflicts == {} and "time_slots" not in s.slots and s.rejected[-1]["value"] == "夜中でもかまわない"


@pytest.mark.parametrize("said", ["22時まで", "17〜22時", "2時から5時まで"])
def test_minor_time_that_ends_by_22_is_kept(said):
    s = H.State(segment="student", phase="fill_gaps")
    H.merge_updates(s, _out(("age_under_18", True, "high"), ("time_slots", said, "high")))
    assert s.slots["time_slots"]["value"] == said and s.rejected == []


def test_minor_late_night_correction_in_review_keeps_the_answer_they_gave():
    """review で未成年が深夜に直そうとしても、前の答え(夕方〜夜)を消さずに残し、深夜は受け付けない。"""
    s = H.State(segment="student", phase="review", review_rounds=1)
    H.merge_updates(s, _out(("age_under_18", True, "high"), ("time_slots", "evening", "high")))
    H.apply_review(s, _out(("time_slots", "深夜もOK", "high")))
    assert s.slots["time_slots"]["value"] == "evening" and s.rejected[-1]["value"] == "late_night_ok"
    assert s.review_log[-1]["touched"] == [] and s.need_more_review is False


def test_minor_conflict_does_not_offer_late_night():
    """年齢より先に、食い違う low の時間帯(深夜もOK / 夕方〜夜)が届いても、カードの「どっち？」に深夜を出さない。"""
    s = H.State(segment="student")
    H.merge_updates(s, _out(("time_slots", "evening", "low")))
    H.merge_updates(s, _out(("time_slots", "late_night_ok", "low")))
    assert s.conflicts == {"time_slots": ["evening", "late_night_ok"]}
    H.merge_updates(s, _out(("age_under_18", True, "high")))
    assert s.conflicts == {} and "time_slots" not in s.slots
    assert not any("深夜" in line for line in H.render_card_ja(H.build_card(s)).split("\n") if "時間帯" in line)


def test_minor_late_night_ng_cannot_be_removed():
    s = H.State(segment="student")
    H.merge_updates(s, _out(("age_under_18", True, "high"), ng=["レジ"]))
    H.merge_updates(s, _out(removals=[H.LATE_NIGHT_NG, "レジ"]))
    assert s.ng == [H.LATE_NIGHT_NG]


# ----------------------------- (4) PRD §12 -----------------------------


@pytest.mark.parametrize(("slot", "said", "value"), [
    ("shift_length", "4時間くらい", "2-4h"),
    ("shift_length", "6時間以上がいい", "6h+"),
    ("time_slots", "平日の夜", "evening"),
    ("time_slots", "朝", "morning"),
    ("commute", "通勤は30分まで", "train_30"),
    ("commute", "家の近くがいい", "walk_bike"),
    ("hourly_wage_min", "1,200円以上はほしい", 1200),
    ("days_per_week", "週２日くらい", "1-2"),
    ("age_under_18", "16歳です", True),
    ("customer_facing", "接客は避けたい", "avoid"),   # カードのラベルそのもの
    ("fuyou", "はい、扶養内で", "within"),           # 選択肢の文そのもの
    ("days_per_week", "週3〜4日", "3-4"),
    ("age_under_18", "18歳未満です", True),
    ("age_under_18", "17才", True),
    ("age_under_18", "高校1年", True),
    ("age_under_18", "18歳以上", False),
    ("age_under_18", "高一です", True),
    ("age_under_18", "高校二年", True),
    ("age_under_18", "中3", True),
    ("age_under_18", "中一です", True),
    ("age_under_18", "未成年です", True),
    ("age_under_18", "高3で17歳", True),        # 学年の 3 ではなく 17歳 を読む
    ("age_under_18", "18歳です", False),        # 年齢だけの答えの 18 は成人
    ("age_under_18", "18", False),
])
def test_review_corrections_are_normalized_and_keep_the_raw_text(slot, said, value):
    """§12-1: 本人の文を列挙値に寄せ、元の文は raw_text に残す。"""
    s = H.State(segment="student" if slot != "fuyou" else "homemaker", phase="review", review_rounds=1)
    H.apply_review(s, _out((slot, said, "high")))
    u = s.slots[slot]
    assert u["value"] == value and type(u["value"]) is type(value)
    assert u["raw_text"] == said and u["normalized"] is True and u["from"] == "t"


@pytest.mark.parametrize(("slot", "said"), [
    ("time_slots", "深夜以外ならいつでも"),   # 否定・条件つきの言い方は、寄せると逆の値になる
    ("time_slots", "夜は無理"),
    ("time_slots", "夕方は厳しい、朝がいい"),
    ("shift_length", "6時間以上は無理"),
    ("commute", "近くじゃなくてもいい"),
    ("days_per_week", "週2〜3日"),            # 範囲が区切り(1-2 / 3-4)をまたぐ
    ("hourly_wage_min", "2026年までに"),       # 円も時給の語も無い数
    ("age_under_18", "高3"),                  # 18歳のこともある
    ("age_under_18", "高三です"),
    ("age_under_18", "20歳未満です"),          # 17歳のこともある
])
def test_risky_free_text_is_kept_as_said_instead_of_guessed(slot, said):
    assert H.normalize(slot, said) == (said, said)


@pytest.mark.parametrize("said", ["高3", "高三です", "20歳未満です", "未成年ではないです", "ひみつ",
                                  "未成年ではありません", "未成年じゃありません", "18歳未満ではありません",
                                  # 18 を含む17歳の言い方(「18歳」だけを読むと成人になり、深夜が通っていた)
                                  "まだ18歳になっていません", "まだ18歳になってないよ", "18歳になってません",
                                  "来月18歳になります", "今年18歳になる高3です", "もうすぐ18歳", "18歳の誕生日はまだです",
                                  "18歳になる年です", "高3で18歳"])
@pytest.mark.parametrize("segment", ["student", "unknown", "homemaker"])
def test_age_that_is_not_yes_or_no_is_not_kept(said, segment):
    """18歳未満かが はい / いいえ に寄らない答えは、スロットにも pending にも入れない(文のまま high で埋めると、
    未成年を見落としたまま hf_age を聞き直さない)。"""
    s = H.State(segment=segment)
    H.merge_updates(s, _out(("age_under_18", said, "high")))
    assert "age_under_18" not in s.slots and s.pending == [] and H.LATE_NIGHT_NG not in s.ng
    assert s.rejected[-1]["reason"] == "age_not_bool" and s.rejected[-1]["raw_text"] == said
    assert H.gap_weight(s, "age_under_18") == 1.0


@pytest.mark.parametrize("said", ["いいえ、未成年ではありません", "いいえ、18歳未満ではありません"])
def test_adult_who_denies_being_a_minor_keeps_late_night(said):
    """「未成年ではありません」の打ち消しを 18歳未満と読まない(成人に NG「深夜シフト」が付き、深夜の時間帯が消えていた)。"""
    assert H.normalize("age_under_18", said) == (False, said)
    s = H.State(segment="student")
    H.merge_updates(s, _out(("time_slots", "late_night_ok", "high")))
    H.merge_updates(s, _out(("age_under_18", said, "high")))
    assert s.slots["age_under_18"]["value"] is False and H.LATE_NIGHT_NG not in s.ng
    assert s.slots["time_slots"]["value"] == "late_night_ok" and s.rejected == []


@pytest.mark.parametrize("said", ["いいえ、ちがいます。高2です", "いいえ、違います、高2です", "いいえ、大学生ではありません。高校1年です",
                                  "いいえ、中学生ではありません。高校1年です", "いいえ、ちがうよ、中3", "違います、高2です",
                                  "いいえ、未成年ではありません。16歳です"])
def test_denying_something_else_while_saying_a_grade_is_a_minor(said):
    """打ち消しているのが未成年の言い方でなければ(カードの値・大学生)、文に残る学年や年齢で 18歳未満と読む。"""
    assert H.normalize("age_under_18", said) == (True, said)


@pytest.mark.parametrize("said", ["いいえ、中学生ではありません", "いいえ、16歳未満ではありません", "いいえ、ちがいます"])
def test_denial_that_does_not_say_adult_is_asked_again(said):
    """中学生でない・16歳未満でない・カードの値がちがう、だけでは成人と分からない(高校生・17歳のこともある)。"""
    assert H.normalize("age_under_18", said) == (said, said)


def test_minor_who_corrects_a_guessed_adult_in_review_gets_late_night_ng():
    """review のカードの「18歳未満：いいえ（推測）」「深夜もOK（推測）」を、本人が「いいえ、ちがいます。高2です」と直す。
    18歳未満と読み、NG「深夜シフト」を付け、推測の深夜は確定させずに消す(成人と読むと深夜もOK が確定していた)。"""
    s = H.State(segment="student", phase="review", review_rounds=1)
    H.merge_updates(s, _out(("age_under_18", False, "low"), ("time_slots", "late_night_ok", "low")))
    card = H.render_card_ja(H.build_card(s))
    assert "（推測）" in card and "深夜" in card
    H.apply_review(s, _out(("age_under_18", "いいえ、ちがいます。高2です", "high")))
    assert s.slots["age_under_18"]["value"] is True and s.slots["age_under_18"]["confidence"] == "high"
    assert H.LATE_NIGHT_NG in s.ng and "time_slots" not in s.slots
    assert s.rejected[-1]["reason"] == "under_18_late_night" and s.need_more_review is True


def test_age_given_as_a_number_is_read():
    assert H.normalize("age_under_18", 16) == (True, 16)
    assert H.normalize("age_under_18", 19) == (False, 19)
    s = H.State(segment="unknown")
    H.merge_updates(s, _out(("age_under_18", 16, "high")))
    assert s.ng == [H.LATE_NIGHT_NG]


def test_values_that_cannot_be_normalized_stay_as_said():
    s = H.State(segment="student")
    H.merge_updates(s, _out(("purpose", "ゲーム代", "high")))
    assert s.slots["purpose"] | {} == {"slot": "purpose", "value": "ゲーム代", "confidence": "high", "from": "t",
                                       "raw_text": "ゲーム代", "normalized": False}
    assert H.normalize("purpose", "hobby") == ("hobby", None)          # もう列挙値
    assert H.normalize("customer_facing", H.NOPREF) == (H.NOPREF, None)


def test_reverse_pick_choices_are_not_value_labels():
    """reverse_pick の選択肢はやりたくないもの。「引越しスタッフ」は physical=light のラベルにも逆引きにも使わない。"""
    assert H.value_ja("physical", "light") == "軽めの作業がいい"
    assert "引越しスタッフ" not in H.LABEL_TO_VALUE.get("physical", {})
    assert "コンビニ店員" not in H.LABEL_TO_VALUE.get("customer_facing", {})


def test_ng_can_be_removed():
    """§12-3: 本人が「やっぱり大丈夫」と取り消した NG は消える(ng_removals)。"""
    s = H.State(segment="homemaker")
    H.merge_updates(s, _out(ng=["夜の仕事", "レジ"]))
    H.merge_updates(s, _out(removals=["レジ", "無い NG"]))
    assert s.ng == ["夜の仕事"]


def test_choices_are_filtered_by_ng_only_when_needed():
    """§12-4: 成人には「深夜もOK」を出す。NG に「深夜シフト」があるときだけ隠す。"""
    s = H.State(segment="student", phase="hard_filter")
    q = next(q for q in H.BANK if q["id"] == "hf_time")
    assert H.visible_choices(s, q) == ["朝", "昼", "夕方〜夜", "深夜もOK"]
    s.ng.append(H.LATE_NIGHT_NG)
    assert H.visible_choices(s, q) == ["朝", "昼", "夕方〜夜"]


def test_unclear_reasks_are_marked_retry(results):
    """§12-5: あいまいな答えの後、fill_gaps で同じ直接質問を聞き直すときは retry を付ける(プロンプトが聞き方を変える)。"""
    unclear = next(m for m in results if m["persona"] == "unclear")
    assert unclear["retried"] == ["hf_time", "hf_weekday"]
    assert all(m["retried"] == [] for m in results if m["persona"] != "unclear")


def test_open_conflict_after_two_reviews_keeps_the_last_low_value():
    """§12-2 はユーザーが決めていないので実装しない: 「これでOK」が続くと result に進み、矛盾は残り、
    最後の low の値(avoid)が残る(PRD の unit_checks_v3.py と同じ経路・同じ結果)。"""
    s = H.State(segment="student", turns=12, phase="fill_gaps")
    for v in ("ok", "avoid"):
        H.merge_updates(s, _out(("customer_facing", v, "low")))
    q = H.step(s, None, H.BANK)
    assert s.phase == "fill_gaps"
    q = H.step(s, _out(), H.BANK)  # 13 ターン目: review の予約に入る
    assert q["id"] == "review_1" and [x for x in q["text"].split("\n") if x.startswith("・接客")] == ["・接客：どっち？ 接客OK / 接客は避けたい"]
    q = H.step(s, _out(), H.BANK)  # 「これでOK」(矛盾が残る): もう1回だけ
    assert q["id"] == "review_2" and s.need_more_review and s.conflicts == {"customer_facing": ["ok", "avoid"]}
    q = H.step(s, _out(), H.BANK)
    assert q is None and s.phase == "result" and s.turns == 15 and s.review_rounds == 2
    assert s.conflicts == {"customer_facing": ["ok", "avoid"]}
    assert s.slots["customer_facing"]["value"] == "avoid" and s.slots["customer_facing"]["confidence"] == "low"


# ----------------------------- (5) dip の状態に保存して進める -----------------------------

UID = "baito-user"


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state.json")


def test_session_starts_with_q_money_and_taps_follow_choice_effects(state):
    r = session.start(state, UID)
    assert r["started"] and r["question"] == {"id": "q_money", "text": "バイト代、何に使いたい？",
                                               "choices": ["推し活・趣味", "貯金・将来のため", "生活費・家計の足し", "旅行・遊び"]}
    assert session.start(state, UID)["started"] is False  # 進めている相談の続き
    r = session.turn(state, UID, "推し活・趣味")
    assert r["status"] == "ask" and r["question"]["id"] == "q_segment" and r["choices"] == ["学生", "家のこと中心", "本業あり副業探し"]
    h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
    assert h.slots["purpose"] == {"slot": "purpose", "value": "hobby", "confidence": "low", "from": "q_money"}  # icebreak は low
    r = session.turn(state, UID, "学生")
    assert r["phase"] == "hard_filter" and r["question"]["id"] == "hf_age"
    r = session.turn(state, UID, "はい")
    h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
    assert h.segment == "student" and h.slots["age_under_18"]["confidence"] == "high" and h.ng == [H.LATE_NIGHT_NG]


def test_free_text_asks_the_model_to_extract_then_merges_its_json(state):
    session.start(state, UID)
    session.turn(state, UID, "推し活・趣味")
    r = session.turn(state, UID, "高1です！")
    assert r == {"active": True, "status": "extract", "phase": "icebreak", "system": r["system"]}
    assert '"id": "q_segment"' in r["system"]  # 読み取りは、聞いた質問(q_segment)のプロンプトで
    extracted = {"reply": "高1なんだ！", "choices": [], "segment": "student", "ng_updates": [],
                 "slot_updates": [{"slot": "age_under_18", "value": True, "confidence": "high", "from": "q_segment"}]}
    r = session.turn(state, UID, "高1です！", extracted)
    assert r["status"] == "ask" and r["phase"] == "hard_filter" and r["question"]["id"] == "hf_school"
    h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
    assert h.segment == "student" and H.LATE_NIGHT_NG in h.ng


def test_exit_word_closes_the_session_and_keeps_only_answered_conditions(state):
    session.start(state, UID)
    for said in ("推し活・趣味", "本業あり副業探し", "OK", "土日"):
        assert session.turn(state, UID, said)["status"] == "ask"
    r = session.turn(state, UID, "求人を見せて")
    assert r["active"] is False and r["ended"] == "exit" and r["saved"] == ["time_band"]
    assert session.KEY not in state.user(UID)
    prefs = state.user(UID)["preferences"]
    assert prefs["time_band"]["value"] == "土日"
    assert "purpose" not in prefs  # 推し活・趣味は icebreak の推測(low)なので、途中でやめたときは保存しない
    assert session.turn(state, UID, "こんにちは") == {"active": False}


def test_track_undo_ends_the_consultation(state):
    """始めた後に区分がアルバイト・パートから外れたら(トークの表示のボタンで取り消した)、ハーネスを閉じてトークの AI に返す。"""
    from dip_mcp import logic

    logic.set_search_track(state, UID, "アルバイト・パート", "バイトを探したい")
    session.start(state, UID)
    assert session.turn(state, UID, "推し活・趣味")["status"] == "ask"
    logic.set_search_track(state, UID, "未定", "取り消し", undo=True)
    r = session.turn(state, UID, "学生")
    assert r["active"] is False and r["ended"] == "track" and session.KEY not in state.user(UID)


def test_start_hands_the_parttime_playbook_to_the_agent(state):
    """結果の後・途中でやめた後はトークの AI が続けるので、保存先と提案の決まりを始めるときに渡す。"""
    r = session.start(state, UID)
    assert "## 保存先" in r["playbook"] and "## 提案に進むとき" in r["playbook"] and r["rules"]["bubble_max"] == 80


def test_end_keeps_the_answered_conditions_and_avoids_are_added_not_replaced(state):
    logic_prefs = {"avoids": "接客は苦手"}
    from dip_mcp import logic

    logic.save_preferences(state, UID, logic_prefs)
    session.start(state, UID)
    for said in ("推し活・趣味", "本業あり副業探し", "OK", "土日"):
        session.turn(state, UID, said)
    h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
    h.ng.append("夜の飲食ホール")
    session._save(state, UID, h)
    assert session.end(state, UID) == {"ended": True, "saved": ["time_band", "avoids"]}
    prefs = {k: v["value"] for k, v in state.user(UID)["preferences"].items()}
    assert prefs == {"avoids": "接客は苦手、夜の飲食ホール", "time_band": "土日"}
    assert session.end(state, UID) == {"ended": False}


def test_choices_are_not_exit_words(state):
    session.start(state, UID)
    h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
    h.question = h.question | {"choices": ["今はいい", "あとで"], "choice_effects": {"今はいい": {}, "あとで": {}}}
    assert session._exits(h, "今はいい") is False
    assert session._exits(H.State(phase="review"), "これでOK、求人見せて") is False
    assert session._exits(H.State(phase="scenario_game"), "もういい") is True
    assert session._exits(H.State(phase="scenario_game"), "求人を探すのは初めて") is False
    assert session._exits(H.State(phase="scenario_game"), "求人を見せて") is True
    assert session._exits(H.State(phase="hard_filter"), "やっぱり正社員で探したい") is True


@pytest.mark.parametrize("said", ["正社員で働いてて、副業を探してます", "正社員じゃなくて、あくまでバイトで",
                                  "求人を見たいけど、先に条件を整理したい"])
def test_answers_that_mention_full_time_or_jobs_keep_the_consultation(state, said):
    """今の働き方(正社員で働いている)や後回しの頼みは、相談を抜ける合図ではない。モデルに読み取らせて続ける。"""
    session.start(state, UID)
    session.turn(state, UID, "貯金・将来のため")
    r = session.turn(state, UID, said)
    assert r["status"] == "extract" and session.active(state, UID)


def _run_session(state, free: dict[str, tuple[str, dict]], limit=baito_sim.STEP_LIMIT):
    """相談を最後まで進める。free の質問(id -> (本人の文, モデルの JSON))には、最初の1回だけ文で答える
    (読み取らせてから呼び直す)。ほかは最初の選択肢をタップし、review は「これでOK」。聞いた質問の (id, retry)。"""
    session.start(state, UID)
    asked = []
    for _ in range(limit):
        h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
        asked.append((h.question["id"], bool(h.question.get("retry"))))
        if h.phase == "review":
            r = session.turn(state, UID, "これでOK")
        elif h.question["id"] in free:
            said, extracted = free.pop(h.question["id"])
            assert session.turn(state, UID, said)["status"] == "extract"
            r = session.turn(state, UID, said, extracted)
        else:
            r = session.turn(state, UID, h.question["choices"][0])
        if r["status"] == "result":
            return r, asked
    raise AssertionError(asked)


def _said(slot, value, qid):
    return {"reply": "", "choices": [], "segment": "unknown", "ng_updates": [],
            "slot_updates": [{"slot": slot, "value": value, "confidence": "high", "from": qid}]}


def test_minor_who_says_late_night_is_fine_never_gets_it_saved(state):
    """E2E: 高校生(hf_age「はい」)が、深夜を隠した hf_time に「深夜も問題ないです」と文で答え、モデルが文のまま返す。
    深夜は受け付けず(時間帯は空のまま。この流れでは後のシナリオ「放課後すぐ」で埋まる)、結果のカードにも保存した希望にも出ない。"""
    r, _ = _run_session(state, {"hf_time": ("深夜も問題ないです", _said("time_slots", "深夜も問題ないです", "hf_time"))})
    assert "深夜も問題ないです" not in r["system"] and "深夜も問題ないです" not in r["fallback"]
    prefs = {k: v["value"] for k, v in state.user(UID)["preferences"].items()}
    assert prefs["time_band"] == "放課後すぐ" and H.LATE_NIGHT_NG in prefs["avoids"]


def test_age_answered_vaguely_is_asked_again(state):
    """E2E: hf_age に「高3です」と答え、モデルが文のまま返す。年齢は埋めずに hf_age を聞き直し、「はい」で深夜 NG が付く。"""
    _, asked = _run_session(state, {"hf_age": ("高3です", _said("age_under_18", "高3です", "hf_age"))})
    assert [a for a in asked if a[0] == "hf_age"] == [("hf_age", False), ("hf_age", True)]
    prefs = {k: v["value"] for k, v in state.user(UID)["preferences"].items()}
    assert H.LATE_NIGHT_NG in prefs["avoids"]


def test_minor_who_says_not_18_yet_is_asked_again_and_never_gets_late_night(state):
    """E2E: hf_age に「まだ18歳になっていません」と文で答え、モデルが文のまま返す。17歳なので成人と読まず、
    hf_age を聞き直す(「18歳」だけを読んで成人にすると、hf_time に「深夜もOK」が出て、結果に深夜が保存されていた)。"""
    said = "まだ18歳になっていません"
    _, asked = _run_session(state, {"hf_age": (said, _said("age_under_18", said, "hf_age"))})
    assert [a for a in asked if a[0] == "hf_age"] == [("hf_age", False), ("hf_age", True)]
    prefs = {k: v["value"] for k, v in state.user(UID)["preferences"].items()}
    assert H.LATE_NIGHT_NG in prefs["avoids"] and prefs.get("time_band") != "深夜もOK"


def test_session_runs_to_the_result_and_saves_the_card_as_preferences(state):
    """hs16 と同じ答えをタップで最後まで。結果で相談を閉じ、確かめた条件をプロフィールの項目に保存する。"""
    hs16 = next(p for p in baito_sim.PERSONAS if p.name == "hs16")
    session.start(state, UID)
    phases, systems = [], []
    age = {"reply": "", "choices": [], "segment": "student", "ng_updates": [],
           "slot_updates": [{"slot": "age_under_18", "value": True, "confidence": "high", "from": "q_segment"}]}
    for _ in range(baito_sim.STEP_LIMIT):
        h = H.State.from_dict(state.user(UID)[session.KEY]["harness"])
        if h.phase == "review":
            r = session.turn(state, UID, "これでOK")
        elif h.question["id"] == "q_segment":  # 文で「高1です！」: 読み取らせてから
            assert session.turn(state, UID, "高1です！")["status"] == "extract"
            r = session.turn(state, UID, "高1です！", age)
        else:
            said = hs16.choose(h.question)
            r = session.turn(state, UID, said if said in h.question["choices"] else h.question["choices"][0])
        phases.append(r["phase"])
        systems.append(r["system"])
        if r["status"] == "result":
            break
    assert r["status"] == "result" and r["choices"] == session.RESULT_CHOICES and session.KEY not in state.user(UID)
    assert "review" in phases and len(phases) <= H.MAX_TURNS
    assert '"result": true' in r["system"] and "【ゆずれない条件】" in r["system"]
    assert "推測のところ、ちがってたらまとめて教えてね！「どっち？」はどちらか選んでね。" not in r["system"].split("# ルール")[0]
    prefs = {k: v["value"] for k, v in state.user(UID)["preferences"].items()}
    assert prefs["days_per_week"] == "週1〜2日" and prefs["time_band"] == "夕方〜夜" and prefs["purpose"] == "推し活・趣味"
    assert H.LATE_NIGHT_NG in prefs["avoids"]
    assert set(r["saved"]) == set(prefs)
    review_systems = [s for s, p in zip(systems, phases, strict=True) if p == "review"]
    assert review_systems and all("【フェーズ: review（まとめカードの確認）】追加ルール" in s for s in review_systems)
    assert not any("【フェーズ: review" in s for s, p in zip(systems, phases, strict=True) if p != "review")


def test_system_prompt_fills_the_state_variables_once(state):
    session.start(state, UID)
    r = session.turn(state, UID, "推し活・趣味")
    s = r["system"]
    assert "- フェーズ: icebreak\n- ユーザーの属性: unknown\n" in s
    slots = json.loads(s.split("- これまでにわかった条件: ")[1].split("\n")[0])
    assert slots == {"slots": {"purpose": {"label": "バイトの目的", "value": "hobby", "value_ja": "推し活・趣味",
                                            "confidence": "low"}}, "ng": []}
    nq = json.loads(s.split("- 次に聞く質問（ハーネスが決定）: ")[1].split("\n")[0])
    assert nq["id"] == "q_segment" and nq["choices"] == ["学生", "家のこと中心", "本業あり副業探し"]
    assert "{{phase}}" not in s and "{{slots_json}}" not in s
    assert "1. 1ターンに質問は1つだけ。聞くのは {{next_question}} だけ。" in s  # ルールの中の語はそのまま
    assert "# ハーネスからの補足" in s


def test_result_preferences_use_labels_and_skip_unconfirmed_values():
    h = H.State(segment="side_job")
    H.merge_updates(h, _out(("hourly_wage_min", 1200, "high"), ("time_slots", "平日の夜", "high"),
                            ("weekday_or_weekend", "weekend", "high"), ("purpose", "earn", "low"), ng=["倉庫作業"]))
    assert session.result_preferences(h) == {"time_band": "夕方〜夜、土日", "min_wage": "1200", "avoids": "倉庫作業"}


@pytest.mark.parametrize(("text", "bad"), [
    ("扶養内で働きたいんだね！103万円を超えないようにしよう", True),
    ("130万の壁があるから気をつけてね", True),
    ("年収106万円までなら扶養のままだよ", True),
    ("扶養の範囲内で働きたい？", False),
    ("時給1,200円以上がいいんだね！扶養はわからない？", False),
    ("扶養のことは、家族か勤務先、専門の窓口に確かめてみてね", False),
    ("扶養内でOK！月5万円くらいを目安にした事務はどう？", False),   # 基準の額ではない
])
def test_replies_that_state_a_fuyou_amount_are_not_used(text, bad):
    """PRD §10: 扶養の金額の基準は示さない。モデルの返信に出たら、ランタイムは使わずに質問の原文を出す。"""
    assert (session.unsafe_reply(text) is not None) is bad
