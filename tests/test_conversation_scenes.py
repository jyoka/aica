"""会話の層の場面の一覧(evals/scenes、設計メモ docs/design/conversation-layer.md §3)の形を確かめる。

場面を回して採点する仕組みは、まだ無い(設計メモ §5 の 1 のカード)。ここで確かめるのは、データの形と、設計メモの約束
(全部の入り口で 60 場面以上、種類ごとに 3 場面以上、入り口をまたぐ場面、jyoka/tasks#326 のトーク、
どの場面にも費用 0 で判定できる条件)だけ。
"""

from __future__ import annotations

import copy
import json

import pytest

from dip_mcp.baito import harness as B
from dip_mcp.career import harness as H
from evals import scenes as S

SCENES = S.load_scenes()
BY_ID = {s["id"]: s for s in SCENES}


def test_enough_scenes_in_every_category():
    assert len(SCENES) >= S.MIN_SCENES
    for cat, n in S.counts(SCENES).items():
        assert n >= S.MIN_PER_CATEGORY, cat


def test_every_entry_covers_its_categories():
    for entry, cats in S.ENTRY_CATEGORIES.items():
        got = S.counts(SCENES, entry)
        assert all(got[c] >= 1 for c in cats), (entry, got)


def test_every_file_holds_one_entry_and_one_category():
    assert S.files()
    for f in S.files():
        scenes = json.loads(f.read_text(encoding="utf-8"))
        assert {s["entry"] for s in scenes} == {f.parent.name}, f
        assert {s["category"] for s in scenes} == {f.stem}, f


def test_goal_scenes_are_covered():
    # task 327 の Goal が名前で挙げた発言
    said = {x["text"] for s in SCENES for x in s["says"]}
    for words in ("正社員も考えてる", "とりあえずバイトでいい", "こんにちは", "仕事探してる", "時給っていくらが普通",
                  "年収っていくらが普通", "土日だけ、駅近で", "東京で500万以上、リモートがいい"):
        assert any(words in t for t in said), words


def test_switch_scenes_check_that_known_values_are_not_asked_again():
    # 入り口を変えても、もう分かっている値を聞き直さない(移った先の入り口の状態を見る not_asks)
    moved = [s for s in SCENES if any(c["check"] == "switches_to" for c in s["expect"])]
    assert {c["entry"] for s in moved for c in s["expect"] if c["check"] == "switches_to"} == {"baito", "career"}
    for s in moved:
        target = next(c["entry"] for c in s["expect"] if c["check"] == "switches_to")
        assert any(c["check"] == "not_asks" and c.get("entry") == target for c in s["expect"]) \
            or any(c["check"] == "profile" for c in s["expect"]), s["id"]
    assert any(c["check"] == "not_asks" and "entry" in c for s in moved for c in s["expect"])


def test_issue_326_talk_is_covered_end_to_end():
    talk = [s for s in SCENES if s["category"] == "t326"]
    assert all("jyoka/tasks#326" in s.get("source", "") for s in talk)
    whole = BY_ID["t326-whole-talk"]
    # 本番の組み合わせ(形 D)で、トーク 2、4、6 の本人の発言を順に送る
    assert whole["form"] == "D"
    assert [x["text"] for x in whole["says"]] == ["年収を上げたい", "東京23区", "他の候補"]
    checks = {c["check"] for c in whole["expect"]}
    assert {"order", "choices_exclude", "choices_ascending", "ack_varies"} <= checks


def test_checks_are_split_into_code_and_judge():
    assert not set(S.CODE_CHECKS) & set(S.JUDGE_CHECKS)
    used = {c["check"] for s in SCENES for c in s["expect"]}
    # 決めた条件はどれも、少なくとも 1 つの場面で使う(使わない条件を語彙に残さない)
    assert used == set(S.CHECK_NAMES)


def test_scenes_use_only_fictional_names():
    # 架空のデータだけ: 会社と人の名前は「みほん」で始める(リポジトリの決まり)
    for s in SCENES:
        text = json.dumps(s, ensure_ascii=False)
        for word in ("株式会社", "有限会社"):
            assert word not in text, s["id"]


def test_known_slots_and_asked_questions_exist_in_the_harness():
    for s in SCENES:
        if s["entry"] == "career":
            assert set(s.get("known") or {}) <= H.ALL_SLOTS, s["id"]
            st = H.new_state(s.get("form", "C"))
            st.segment = s["segment"]
            if s["asking"] not in ("review", "start"):
                assert s["asking"] in H.qmap(st, H.BANK), s["id"]
        elif s["entry"] == "baito":
            assert set(s.get("known") or {}) <= B.ALL_SLOTS | S.BAITO_EXTRA_SLOTS, s["id"]
            if s["asking"] != "review":
                assert s["asking"] in B.qmap(B.BANK), s["id"]


def test_every_pressed_button_is_in_the_question_bank():
    # 2 つ目以降に押すボタンも、その入り口の質問バンクのどこかにある(validate は最初の発言のボタンだけを、
    # 今の問いのボタンと照らす)。#326 で候補が変わったときに、場面が古くなったことに気づくため
    labels = {"career": {H.REVIEW_OK, H.START_GO, H.LATER, H.MORE}, "baito": {B.REVIEW_OK}}
    for st_form in S.FORMS:
        st = H.new_state(st_form)
        for seg in S.SEGMENTS["career"]:
            st.segment = seg
            for q in H.questions(st, H.BANK):
                labels["career"] |= set(q["choices"]) | set(q.get("escape", []))
    labels["baito"] |= {c for q in B.BANK for c in q["choices"]}
    for s in SCENES:
        for x in s["says"]:
            if x["button"]:
                assert x["text"] in labels[s["entry"]], (s["id"], x["text"])


def _broken(base="chat-weather", **change):
    s = copy.deepcopy(BY_ID[base])
    s.update(change)
    return s


@pytest.mark.parametrize("scene, msg", [
    (_broken(category="weather"), "category"),
    (_broken(entry="voice"), "entry"),
    (_broken(category="t326", entry="baito", segment="other", asking="hf_days", known={}), "置かない"),
    (_broken(asking="hf_raise", segment="early"), "出せない問い"),
    (_broken(known={"salary": {"text": "x"}}), "known"),
    (_broken(says=[{"text": "大阪", "button": True}]), "ボタンに無い"),
    (_broken(expect=[{"check": "responds_first"}]), "コードで判定する条件"),
    (_broken(expect=[{"check": "asks", "qid": "hf_area", "slot": "area"}]), "どちらか 1 つ"),
    (_broken(expect=[{"check": "slot", "slot": "area", "number": "500"}]), "型"),
    (_broken(expect=[{"check": "order", "parts": ["ack", "greeting"]}]), "parts"),
    (_broken(expect=[{"check": "keeps_question", "at": 3}]), "返信の数"),
    (_broken(expect=[{"check": "slot_unchanged", "slot": "area"}]), "known に無い"),
    (_broken(expect=[{"check": "smiles"}]), "知らない check"),
    (_broken(expect=[{"check": "not_asks", "entry": "baito", "slots": ["days_per_week"]}]), "switches_to が無い"),
    (_broken(expect=[{"check": "switches_to", "entry": "career"}]), "switches_to の entry"),
    (_broken(expect=[{"check": "profile", "key": "salary"}]), "共通の値"),
    (_broken(form="B", entry="baito"), "form"),
    (_broken("baito-chat-weather", known={"days_per_week": {"value": "週3"}}), "区切りに無い"),
    (_broken("baito-chat-weather", expect=[{"check": "slot", "slot": "days_per_week", "value": "7"}]), "区切りに無い"),
    (_broken("baito-chat-weather", asking="hf_weekday"), "出せない問い"),
    (_broken("talk-hello", asking="hf_area"), "talk"),
    (_broken("talk-hello", expect=[{"check": "keeps_question"}]), "今の問いが無い"),
])
def test_validate_rejects_broken_scenes(scene, msg):
    with pytest.raises(ValueError, match=msg):
        S.validate(scene)
