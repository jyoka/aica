"""会話の層の共通の部品(dip_mcp/convo/。設計メモ docs/design/conversation-layer.md §4.1、§4.3、§4.4、§4.8、§4.11)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dip_mcp.baito import harness as B
from dip_mcp.career import harness as H
from dip_mcp.convo import acts as A
from dip_mcp.convo import entry as E
from dip_mcp.convo import facts as F
from dip_mcp.convo import guard
from dip_mcp.convo import plan as P
from dip_mcp.convo import reply as R
from dip_mcp.convo import state as CS
from dip_mcp.convo import turn as T
from dip_mcp.convo import values as V
from evals import scenes as S

ROOT = Path(__file__).resolve().parent.parent
CAREER_ENTRY = ROOT / "dip_mcp" / "career" / "entry.json"


# ----------------------------- 守りの前処理 -----------------------------
def test_crisis_table_is_the_v4_table_and_v4_reads_the_same_function():
    """言葉と窓口は v4 の表(crisis_v4.json)と同じ。v4 はこの関数を読み込む(v4 の動きは変えない)。"""
    v4 = B.CRISIS
    for k in ("source", "checked", "words", "continue_words", "continue_not"):
        assert guard.CRISIS[k] == v4[k], k
    assert guard.CRISIS["contacts"] == [x for x in v4["reply"] if x.startswith("・")]
    assert B.crisis_hit is guard.crisis_hit and B.wants_resume is guard.wants_resume


def test_crisis_hit_folds_kana_and_resume_reads_negation():
    assert guard.crisis_hit("仕事つらすぎてもう死にたい") and guard.crisis_hit("ｼﾆﾀｲ") and guard.crisis_hit("キエタイ")
    assert not guard.crisis_hit("年収を上げたい")
    assert guard.wants_resume("続けます") and not guard.wants_resume("続けたくない")


def test_crisis_text_puts_the_shared_contacts_between_the_entry_lines():
    text = guard.crisis_text(["受け止め"], ["窓口 {source}", "続けるときは「続ける」"])
    lines = text.split("\n")
    assert lines[0] == "受け止め" and lines[1:4] == guard.CRISIS["contacts"]
    assert lines[4] == "窓口 " + guard.CRISIS["source"]


def test_personal_parts_are_scrubbed_masked_and_found():
    assert guard.scrub("[個人情報を削除]の近くがいい") == "の近くがいい"
    assert guard.scrub("みほん太郎、販売", ["みほん太郎"]) == "販売"
    assert guard.scrub("[個人情報を削除]") == ""
    assert guard.mask("みほん太郎です", ["みほん太郎"]) == "…です"
    assert guard.leaks("みほん太郎さん、[個人情報を削除]ですね", ["みほん太郎"]) == ["[個人情報を削除]", "みほん太郎"]
    assert guard.leaks("販売ですね", ["みほん太郎"]) == []


# ----------------------------- 読む段の出力 -----------------------------
SLOTS = {"area", "min_annual_salary", "why_now"}


def read(acts, said, **kw):
    return A.validate_reading({"acts": acts, **kw}, said, SLOTS, entries=("baito",), segments=H.SEGMENTS)


def test_reading_keeps_only_what_was_said():
    out, probs = read([{"act": "answer", "slot": "area", "text": "東京"},
                       {"act": "add", "slot": "min_annual_salary", "text": "500万以上", "number": 500, "unit": "万"},
                       {"act": "add", "slot": "area", "text": "横浜"},           # 発言に無い
                       {"act": "add", "slot": "team_style", "text": "東京"},     # 知らない欄
                       {"act": "add", "slot": "why_now", "text": "東京", "number": 800},  # 数が発言に無い
                       {"act": "dance"}],
                      "東京で500万以上", hook="東京で500万以上の仕事を探しているところ", mentions=["東京", "大阪"])
    assert out["acts"] == [{"act": "answer", "slot": "area", "text": "東京"},
                           {"act": "add", "slot": "min_annual_salary", "text": "500万以上", "number": 500, "unit": "万"},
                           {"act": "add", "slot": "why_now", "text": "東京"}]
    assert out["hook"] is None and out["mentions"] == ["東京"]
    assert {"text_not_said:area", "unknown_slot:team_style", "number_not_said:why_now", "unknown_act:dance", "hook_bad",
            "mentions_bad"} <= set(probs)


def test_reading_checks_switch_emotion_spans_and_segment():
    out, probs = read([{"act": "switch", "to": "baito", "strength": "want"}, {"act": "switch", "to": "talk", "strength": "want"},
                       {"act": "emotion", "feeling": "angry"}, {"act": "emotion", "feeling": "bored"},
                       {"act": "name", "span": "みほん"}, {"act": "sensitive", "span": "うつ"},
                       {"act": "answer", "slot": "segment", "text": "転職", "value": "change"},
                       {"act": "answer", "slot": "segment", "text": "転職", "value": "nope"}],
                      "みほんです。転職したい。")
    assert [a["act"] for a in out["acts"]] == ["switch", "emotion", "name", "answer"]
    assert out["acts"][-1]["value"] == "change"
    assert {"switch_bad", "emotion_bad", "sensitive_not_said", "invalid_segment"} <= set(probs)


def test_reading_that_is_not_usable_is_retried():
    assert A.validate_reading("ではなく", "x", SLOTS) == (None, ["not_json"])
    assert A.validate_reading({"acts": "x"}, "x", SLOTS) == (None, ["acts_bad_type"])
    out, probs = A.validate_reading('```json\n{"acts": [], "ng": ["ノルマ"]}\n```', "はい", SLOTS)
    assert out == {"acts": [], "hook": None, "mentions": [], "extra": {"ng": ["ノルマ"]}} and probs == []


def test_baito_values_must_be_bucket_keys():
    out, probs = A.validate_reading({"acts": [{"act": "answer", "slot": "days_per_week", "text": "週3", "value": "3-4"},
                                              {"act": "add", "slot": "days_per_week", "text": "週3", "value": "9"}]},
                                    "週3くらい", set(B.SLOTS), values={"days_per_week": set(B.SLOTS["days_per_week"]["values"])})
    assert [a["value"] for a in out["acts"]] == ["3-4"] and probs == ["value_not_in_buckets:days_per_week"]


def test_act_vocabulary_is_the_one_the_scenes_use():
    """場面の一覧の読む段の正解(reading)は、層の読む段と同じ型で書く。"""
    assert S.ACTS is A.ACTS and S.VALUE_ACTS is A.VALUE_ACTS and S.FEELINGS is A.FEELINGS


# ----------------------------- 値の当て方 -----------------------------
def test_plan_orders_retract_first_and_scrubs_names():
    reading = {"acts": [{"act": "name", "span": "みほん太郎"},
                        {"act": "correct", "slot": "area", "text": "横浜"},
                        {"act": "answer", "slot": "career1.role", "text": "みほん太郎、販売"},
                        {"act": "add", "slot": "why_now", "text": "みほん太郎"},  # 氏名だけ: 入れない
                        {"act": "retract", "slot": "start_timing"},
                        {"act": "dont_know", "slot": "start_timing"},          # ほかの欄の「わからない」は空のまま
                        {"act": "pass"}]}
    ops = V.plan(reading, ["career1.role"])
    assert [(o["op"], o["slot"], o["text"]) for o in ops] == [
        ("retract", "start_timing", ""), ("correct", "area", "横浜"), ("set", "career1.role", "販売"), ("escape", None, "")]


def test_confidence_and_merge():
    assert [V.confidence(op, True) for op in ("set", "add", "correct", "unsure")] == ["high", "low", "high", "low"]
    assert V.confidence("set", False) == "low"  # 場面の二択など、直接聞かない問い
    assert V.merge("家から30分以内", "駅から歩けるところ") == "家から30分以内、駅から歩けるところ"
    assert V.merge("家から30分以内", "30分以内") == "家から30分以内" and V.merge("", "東京") == "東京"


# ----------------------------- 呼び直しと状態の器 -----------------------------
def test_call_llm_retries_once_then_gives_up():
    log, outs = [], iter(["x", '{"ok": 1}'])
    assert T.call_llm(lambda p: next(outs), {}, lambda raw: (T.parse_json(raw), ["bad"] if raw == "x" else []),
                      lambda kind, **kw: log.append(kind), "read") == {"ok": 1}
    assert log == ["read_retry_ok"]
    log.clear()

    def boom(p):
        raise TimeoutError
    assert T.call_llm(boom, {}, lambda raw: (None, ["none"]), lambda kind, **kw: log.append((kind, kw)), "read") is None
    assert log == [("read_fallback", {"problems": ["exception:TimeoutError", "none", "exception:TimeoutError", "none"]})]


def test_match_button_reads_shown_choices():
    cur = {"choices": ["東京23区", "他の候補"], "shown": ["大阪市内"]}
    assert T.match_button(cur, "東京２３区") == "東京23区" and T.match_button(cur, "大阪市内") == "大阪市内"
    assert T.match_button(cur, "横浜") is None and T.match_button(None, "x") is None


def test_recent_keeps_the_last_four_and_clips_long_text():
    c = CS.new()
    for i in range(3):
        CS.heard(c, f"発言{i}")
        CS.replied(c, f"返信{i}")
    CS.replied(c, "長" * 300)
    assert [m["text"] for m in c["recent"]][:3] == ["返信1", "発言2", "返信2"]
    assert len(c["recent"]) == CS.RECENT_MAX and c["recent"][-1]["text"].endswith("…")
    assert c["replies"] == ["返信2", c["recent"][-1]["text"]]
    assert CS.recent_lines(CS.new()) == "(なし)" and CS.recent_lines(c).startswith("aica: 返信1\n本人: 発言2")


# ----------------------------- 入り口の定義 -----------------------------
def test_career_entry_describes_every_slot_and_its_bank():
    e = E.load(CAREER_ENTRY, H.ALL_SLOTS, ("career", "baito"), ("crisis_lead", "crisis_tail", "crisis_resume"))
    assert set(e.slots) == H.ALL_SLOTS and [q["id"] for q in e.bank] == [q["id"] for q in H.BANK]
    assert [(s["to"], s["label"], s["track"]) for s in e.switch] == [("baito", "アルバイト・パートで探す", "アルバイト・パート")]
    assert set(e.carry) == {"baito"} and e.carry["baito"]["near.area"] == {"slot": "area", "how": "same"}


@pytest.mark.parametrize("change, match", [
    (lambda d: d["slots"].pop("area"), "説明の無い欄"),
    (lambda d: d["slots"].update(nope="x"), "知らない欄"),
    (lambda d: d["switch"].append({"to": "dance", "label": "x"}), "移る先"),
    (lambda d: d["infer"]["ib_why_now"].update({"ない": {"raise_target": "年収"}}), "ボタン"),
    (lambda d: d["infer"].update({"q_nope": {}}), "知らない問い"),
    (lambda d: d["texts"].pop("crisis_resume"), "決まった文"),
])
def test_entry_shape_errors_stop_loading(tmp_path, change, match):
    d = json.loads(CAREER_ENTRY.read_text(encoding="utf-8"))
    change(d)
    for name in ("bank.json", "facts.json"):
        (tmp_path / name).write_text((CAREER_ENTRY.parent / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "entry.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        E.load(tmp_path / "entry.json", H.ALL_SLOTS, ("career", "baito"), ("crisis_lead", "crisis_tail", "crisis_resume"))


def test_bank_shape_errors_stop_loading():
    q = {"id": "a", "text": "{which}問い", "variants": ["{which}言い方"], "targets": ["area"], "pages": [["東京"]],
         "choice_effects": {}}
    E.check_bank("x", [q], {"area"})
    for bad, match in ((dict(q, text=""), "文が無い"), (dict(q, targets=["nope"]), "知らない的"),
                       (dict(q, pages=[[]]), "候補が無い"), (dict(q, choice_effects={"大阪": {}}), "効き目"),
                       (dict(q, variants=[]), "variants"), (dict(q, variants=["a", "b", "c"]), "variants"),
                       (dict(q, variants=["{which}問い"]), "重なる"), (dict(q, variants=["どこ?いつ?"]), "問いは 1 つ"),
                       (dict(q, variants=["{role}の言い方"]), "差し込み")):
        with pytest.raises(ValueError, match=match):
            E.check_bank("x", [bad], {"area"})


def test_a_number_of_the_wrong_type_drops_only_the_number():
    out, probs = read([{"act": "add", "slot": "min_annual_salary", "text": "500万", "number": "500"}], "500万")
    assert out["acts"] == [{"act": "add", "slot": "min_annual_salary", "text": "500万"}] and probs == ["add_bad_type:number"]


# ----------------------------- 応じ方の計画(§4.6) -----------------------------
def rd(*acts):
    return {"acts": list(acts), "hook": None, "mentions": [], "extra": {}}


@pytest.mark.parametrize("reading, kw, want", [
    (rd({"act": "answer", "slot": "area", "text": "東京"}), {"answered": True}, ("short", "pick", "count", [])),
    (rd({"act": "answer", "slot": "raise_target", "text": "年収"}, {"act": "complaint"}, {"act": "emotion", "feeling": "angry"}),
     {"answered": True}, ("apology", "pick", "count", [])),
    (rd({"act": "answer", "slot": "why_now", "text": "パワハラ"}, {"act": "emotion", "feeling": "tired"}), {"answered": True},
     ("empathy", "pick", "count", [])),
    (rd({"act": "complaint"}), {}, ("apology", "skip", "count", [])),
    (rd({"act": "go_back"}), {}, ("done", "back", "free", [])),
    (rd({"act": "correct", "slot": "area", "text": "横浜"}), {"fixed": True}, ("done", "keep", "free", [])),
    (rd({"act": "falsify"}), {}, (None, "keep", "free", [])),
    (rd({"act": "chitchat"}), {}, ("light", "keep", "aside", [])),
    (rd({"act": "ask_service"}), {}, (None, "keep", "aside", ["ask_service"])),
    (rd({"act": "ask_advice"}, {"act": "chitchat"}), {}, ("light", "keep", "aside", ["ask_advice"])),
    (rd({"act": "emotion", "feeling": "anxious"}), {}, ("empathy", "keep", "aside", [])),
])
def test_plan_follows_the_table(reading, kw, want):
    p = P.respond(reading, **kw)
    assert (p.ack, p.next, p.count, p.answer) == want
    assert p.notes == (["falsify"] if "falsify" in [a["act"] for a in reading["acts"]] else [])


def test_only_tiredness_alone_makes_the_plan_hurry():
    assert P.respond(rd({"act": "emotion", "feeling": "tired"})).hurry
    assert not P.respond(rd({"act": "answer", "slot": "why_now", "text": "限界"}, {"act": "emotion", "feeling": "tired"}),
                         answered=True).hurry
    assert not P.respond(rd({"act": "emotion", "feeling": "sad"})).hurry


def test_pause_note_comes_after_two_offtrack_turns_or_when_tired_and_only_once():
    chat, tired = P.respond(rd({"act": "chitchat"})), P.respond(rd({"act": "emotion", "feeling": "tired"}))
    assert [P.pause_due(chat, n, False) for n in (1, 2, 3, 4)] == [False, False, True, True]
    assert P.pause_due(tired, 1, False) and not P.pause_due(tired, 1, True)
    assert not P.pause_due(P.respond(rd({"act": "go_back"})), 5, False)  # 前の問いに戻るのは脱線ではない
    assert P.PARTS == ("ack", "answer", "note", "phase_line", "ask")


# ----------------------------- 事実のデータ -----------------------------
def test_entry_facts_add_to_the_common_facts_and_pick_by_kind():
    e = E.load(CAREER_ENTRY, H.ALL_SLOTS, ("career", "baito"))
    common = F.COMMON
    assert e.facts["ask_service"][:len(common["ask_service"])] == common["ask_service"]
    assert "求人はヒアリングの後に出せます" in e.facts["ask_service"]
    assert e.facts["checked"] == {"common": "2026-10-29", "entry": "2026-10-29"}  # 運営者の代わりにポリシーと突き合わせた(jyoka/tasks#356)
    got = F.pick(e.facts, ["ask_service"], where="career_cards", known=True)
    assert got[:2] == ["いまは職歴を聞いています", e.facts["known"]] and not set(got) & set(e.facts["ask_advice"]) - set(
        e.facts["ask_service"])
    advice = F.pick(e.facts, ["ask_advice"], where="career_cards", known=True)
    assert advice == e.facts["ask_advice"]  # 今どこかと、分かっている値の文は、aica とこの相談を聞かれたときだけ
    assert not any(c.isdigit() for f in advice for c in f)  # 相場の数を持たない


def test_facts_shape_errors_stop_loading(tmp_path):
    for bad, match in (({"ask_service": "一つの文"}, "文のリスト"), ({"ask_advice": ["x" * 81]}, "80 字"),
                       ({"where": {"a": ""}}, "where"), ({"known": 3}, "known")):
        (tmp_path / "facts.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            F.read(tmp_path / "facts.json")


# ----------------------------- 言う段(reply) -----------------------------
ASK = {"qid": "hf_area", "mode": "ask", "text": "働きたい場所は?", "phrasings": ["働きたい場所は?", "どこで働きたいですか?"]}


def plan(**kw):
    return R.new_plan(**({"ask": ASK, "ack": "short", "said": "東京23区", "choices": ["400万円台"]} | kw))


def test_pick_rotates_phrasings_and_avoids_the_last_one():
    ps = ["a", "b", "c"]
    assert R.pick(ps, None) == ("a", ps)
    assert R.pick(ps, "a") == ("b", ["b", "c"]) and R.pick(ps, "c") == ("a", ["a", "b"])
    assert R.pick(["a"], "a") == ("a", ["a"])


def test_validate_reads_the_parts_and_puts_the_fixed_lines_between_ack_and_question():
    p = plan(note=["近いものを押すだけでも大丈夫です。"], phase_line=["次は希望の条件です"])
    parts, why = R.validate({"reply": "東京23区ですね。どこで働きたいですか?"}, p, CS.new())
    assert why is None and [k for k, _ in parts] == ["ack", "note", "phase_line", "ask"]
    assert R.render(parts) == "東京23区ですね。\n近いものを押すだけでも大丈夫です。\n次は希望の条件です\nどこで働きたいですか?"
    same, _ = R.validate("東京23区ですね。\n近いものを押すだけでも大丈夫です。\n次は希望の条件です\nどこで働きたいですか?", p, CS.new())
    assert same == parts  # モデルが書き写した決まった行は、順が合えばそのまま
    assert R.validate("次は希望の条件です\n東京23区ですね。どこで働きたいですか?", p, CS.new())[1] == "text_between_parts"
    assert R.render(R.validate("働きたい場所は?", plan(), CS.new())[0]) == "働きたい場所は?"  # 相づちは無くてもよい
    # 順の違う場所に書き写した区切りは、吹き出しに 2 回出るので落とす
    assert R.validate("次は希望の条件です\nわかりました。\n近いものを押すだけでも大丈夫です。\nどこで働きたいですか?", p,
                      CS.new())[1] == "text_between_parts"
    # 句点の無い言い方に、モデルが句点を付けた: 言い方のまま受ける
    bare = plan(ask=dict(ASK, text="場所を教えてください", phrasings=["場所を教えてください"]))
    parts, why = R.validate("わかりました。場所を教えてください。", bare, CS.new())
    assert why is None and R.ask_of(parts) == "場所を教えてください"


@pytest.mark.parametrize("reply, why", [
    ("東京23区ですね。働きたいところは?", "question_missing"),
    ("東京23区ですね。働きたい場所は?駅でも", "text_after_question"),
    ("東京23区ですね。通勤も楽そうです。働きたい場所は?", "too_much_extra"),
    ("東京23区とは、素晴らしい選択ですね。働きたい場所は?", "praise"),
    ("読み取れなかったので。働きたい場所は?", "blame"),
    ("東京23区、駅は決まってますか?働きたい場所は?", "two_questions"),
    ("年収800万も狙えますね。働きたい場所は?", "made_up_number"),
    ("みほん太郎さん、了解です。働きたい場所は?", "personal_echo"),
    ("五年も続けてこられたのですね。働きたい場所は?", "made_up_number"),
])
def test_validate_rejects(reply, why):
    assert R.validate({"reply": reply}, plan(hidden=["みほん太郎"]), CS.new()) == (None, why)


def test_validate_needs_a_carried_value_in_the_ack_and_takes_its_numbers_from_the_about():
    """移った先の最初の返事(task 379): 相づちに入れる言葉(mention)が 1 つも無ければ落とす(相づちが無いときも)。
    数は計画の about(ハーネスが記録の文から作った文)にあれば、出どころがある。"""
    p = plan(ack="done", said="やっぱり正社員で", about=["東京、週5日以上で、正社員で探します"], mention=["東京", "週5日以上"])
    parts, why = R.validate({"reply": "週5日以上で、正社員で探しますね。働きたい場所は?"}, p, CS.new())
    assert why is None and R.ack_of(parts) == "週5日以上で、正社員で探しますね。"
    assert R.validate({"reply": "わかりました。働きたい場所は?"}, p, CS.new())[1] == "mention_missing"
    assert R.validate({"reply": "働きたい場所は?"}, p, CS.new())[1] == "mention_missing"
    no_about = plan(ack="done", said="やっぱり正社員で", mention=["東京", "週5日以上"])
    assert R.validate({"reply": "週5日以上ですね。働きたい場所は?"}, no_about, CS.new())[1] == "made_up_number"
    assert R.validate({"reply": "働きたい場所は?"}, plan(), CS.new())[1] is None  # mention の無い計画は今のまま
    wave = plan(ack="done", said="週3〜4日で", about=["週3〜4日で、正社員で探します"], mention=["週3〜4日"])
    assert R.validate({"reply": "週3～4日で探しますね。働きたい場所は?"}, wave, CS.new())[1] is None  # 波線の違いは同じに見る
    ack = R.fixed_ack(p, CS.new(), {"done": ["わかりました。"]})
    assert ack == "東京、週5日以上で、正社員で探します。"  # 決まった組み立ては about の文(値が入る)


def test_validate_rejects_a_third_same_ending_and_a_repeated_sentence_and_extra_text():
    c = CS.new()
    CS.replied(c, "同じ仕事で上を目指すのですね。", "同じ仕事で上を目指すのですね。")
    CS.replied(c, "年収を上げたいのですね。働きたい場所は?", "年収を上げたいのですね。")
    assert R.validate("東京23区ですね。働きたい場所は?", plan(), c) == (None, "ack_repeats")
    assert R.validate("年収を上げたいのですね。働きたい場所は?", plan(), CS.new() | {"replies": c["replies"]})[1] == "repeats_last"
    assert R.validate("東京23区、了解です。働きたい場所は?", plan(), c)[1] is None
    assert R.validate("了解です。働きたい場所は?", plan(ack=None), c) == (None, "extra_text")
    assert not R.needs_model(plan(ack=None))


def test_praise_words_the_person_said_are_only_echoed():
    """本人の発言にある言葉(「すごい」)をなぞるのは、ほめ言葉にしない。本人が言っていないほめ言葉は今までどおり落とす。"""
    said = "すごい混んでる店は苦手"
    assert R.validate("すごい混んでる店は苦手なんですね。働きたい場所は?", plan(said=said), CS.new())[1] is None
    assert R.validate("すごい混んでる店は苦手、さすがです。働きたい場所は?", plan(said=said), CS.new()) == (None, "praise")
    assert R.praised("頼もしいね。", "よし、燃えてきた") == "頼もし" and R.praised("すごい行列だね。", "すごい行列") is None
    # 言葉だけが同じで、前後が本人の発言と違う(「すごい残業」の後の「すごいですね」)のは、ほめ言葉
    assert R.praised("すごいですね。", "前の職場はすごい残業で") == "すごい"
    assert R.praised("いいですね!", "週3がいいですね") == "いいですね"


def test_fixed_ack_can_be_looked_at_without_turning_the_rotation():
    acks = {"short": ["わかりました。", "ありがとうございます。"]}
    c = CS.new()
    assert R.fixed_ack(plan(), c, acks, turn=False) == "わかりました。" and "rotation" not in c
    assert R.fixed_ack(plan(), c, acks) == "わかりました。" and c["rotation"] == {"short": 1}


def test_ack_budget_and_answer_bubble():
    long_ask = dict(ASK, text="あ" * 78, phrasings=["あ" * 78])
    assert R.new_plan(long_ask, "short")["ack"] is None  # 相づちの入る字数が無い
    p = plan(ack=None, answer={"facts": ["aica は架空のデモです"], "fallback": "お答えできません。"})
    assert p["limit"] == R.ANSWER_BUBBLE_MAX and R.needs_model(p)
    parts, why = R.validate("aica は求人を探す手伝いをします。架空のデモです。働きたい場所は?", p, CS.new())
    assert why is None and parts[0] == ["answer", "aica は求人を探す手伝いをします。架空のデモです。"]
    assert R.validate("働きたい場所は?", p, CS.new()) == (None, "answer_missing")  # 逆質問を黙って流さない
    fixed = plan(ack=None, answer={"facts": [], "fallback": "お答えできません。"})  # 渡す事実が無い: モデルに書かせない
    assert fixed["limit"] == R.ANSWER_BUBBLE_MAX and not R.needs_model(fixed)
    assert R.fallback(fixed, CS.new(), {}) == [["answer", "お答えできません。"], ["ask", "働きたい場所は?"]]


def test_fallback_rotates_the_entry_acks_and_skips_repeats():
    acks = {"short": ["わかりました。", "ありがとうございます。", "なるほど、了解です。"]}
    c = CS.new()
    first = R.fallback(plan(), c, acks)
    assert first == [["ack", "わかりました。"], ["ask", "働きたい場所は?"]]
    CS.replied(c, R.render(first), "わかりました。")
    assert R.ack_of(R.fallback(plan(), c, acks)) == "ありがとうございます。"
    c2 = CS.new()
    CS.replied(c2, "x", "ありがとうございます。")
    CS.replied(c2, "y", "ございます。")  # 終わり 4 字が「います。」の相づちが 2 つ続いた: 3 つ目には使わない
    assert R.ack_of(R.fallback(plan(), c2 | {"rotation": {"short": 1}}, acks)) == "なるほど、了解です。"
    assert R.fallback(plan(ack=None, note=["注意"]), CS.new(), acks) == [["note", "注意"], ["ask", "働きたい場所は?"]]


def test_more_lead_is_built_from_the_topic():
    texts = {"more_lead": "{topic}のほかの候補です。", "more_lead_number": "{topic}のほかの区切りです。"}
    assert R.more_lead(texts, "年収", "number") == "年収のほかの区切りです。"
    assert R.more_lead(texts, "働く場所", "text") == "働く場所のほかの候補です。"


@pytest.mark.parametrize("change, match", [
    (lambda d: d.pop("tone"), "調子"),
    (lambda d: d["tone"]["acks"].pop("empathy"), "決まった相づち"),
    (lambda d: d["tone"]["acks"]["short"].append("すごいですね。"), "決まった相づち"),
    (lambda d: d["tone"]["acks"]["short"].__setitem__(0, "はい。そうですね。"), "決まった相づち"),
])
def test_entry_tone_errors_stop_loading(tmp_path, change, match):
    d = json.loads(CAREER_ENTRY.read_text(encoding="utf-8"))
    change(d)
    for name in ("bank.json", "facts.json"):
        (tmp_path / name).write_text((CAREER_ENTRY.parent / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "entry.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        E.load(tmp_path / "entry.json", H.ALL_SLOTS, ("career", "baito"))
