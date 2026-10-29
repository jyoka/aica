"""会話の層の共通の部品(dip_mcp/convo/。設計メモ docs/design/conversation-layer.md §4.1、§4.3、§4.4、§4.11)。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dip_mcp.baito import harness as B
from dip_mcp.career import harness as H
from dip_mcp.convo import acts as A
from dip_mcp.convo import entry as E
from dip_mcp.convo import guard
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
    assert e.switch == [{"to": "baito", "label": "アルバイト・パートで探す"}]


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
    (tmp_path / "bank.json").write_text((CAREER_ENTRY.parent / "bank.json").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "entry.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        E.load(tmp_path / "entry.json", H.ALL_SLOTS, ("career", "baito"), ("crisis_lead", "crisis_tail", "crisis_resume"))


def test_bank_shape_errors_stop_loading():
    q = {"id": "a", "text": "問い", "targets": ["area"], "pages": [["東京"]], "choice_effects": {}}
    E.check_bank("x", [q], {"area"})
    for bad, match in ((dict(q, text=""), "文が無い"), (dict(q, targets=["nope"]), "知らない的"),
                       (dict(q, pages=[[]]), "候補が無い"), (dict(q, choice_effects={"大阪": {}}), "効き目")):
        with pytest.raises(ValueError, match=match):
            E.check_bank("x", [bad], {"area"})


def test_a_number_of_the_wrong_type_drops_only_the_number():
    out, probs = read([{"act": "add", "slot": "min_annual_salary", "text": "500万", "number": "500"}], "500万")
    assert out["acts"] == [{"act": "add", "slot": "min_annual_salary", "text": "500万"}] and probs == ["add_bad_type:number"]
