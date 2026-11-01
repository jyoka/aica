"""正社員のヒアリングの評価(evals/fulltime_hearing、設計メモ docs/design/fulltime-hearing.md §6)を実 API なしで。

本人役とジャッジは、実 API の代わりに台本の LLM(FakeClient。出力の型で返し分ける)で動かす。台本の本人役は、
AI の返信の文とボタンを読み、事実表から答える(ボタンが合えば押す)。台本のジャッジは、最後の AI の返信の字面から
ラベルを付ける。離脱を決めるのはコード(Clock)なので、ここで §6.6 の 1(悪い台本で離脱、良い台本で完了)を確かめる。
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from evals.fulltime_hearing import judge as J
from evals.fulltime_hearing.actor import (
    BUTTON_SECONDS,
    LEAVE_TEXT,
    ActorTurn,
    AIReply,
    Clock,
    actor_messages,
    read_chars,
    render,
    system_prompt,
)
from evals.fulltime_hearing.conversation import made_up_numbers, run_conversation
from evals.fulltime_hearing.forms import (
    FORMS,
    RANGE_UNITS,
    SALARY,
    SCALE,
    FormA,
    FormHarness,
    ScriptBad,
    ScriptGood,
    a_reply,
    check_forms,
    declined,
)
from evals.fulltime_hearing.personas import hard_keys, load_personas, select
from evals.fulltime_hearing.report import combine, improved, report, summarize, verdicts
from evals.fulltime_hearing.report import main as report_main
from evals.fulltime_hearing.run import main, parse_args
from evals.scenarios import load_resume

PERSONAS = {p["id"]: p for p in load_personas()}
TIRED = "(メモ: 疲れている)"

# 台本の本人役が、AI の問いの字面から事実表の key を決める手がかり(職歴カードの欄は、直前に聞かれた職歴に付ける)
TOPICS = [("なぜ今", "why_now"), ("転職を考えた理由", "why_now"), ("場所", "area"), ("勤務地", "area"),
          ("年収", "min_annual_salary"), ("いつから", "start_timing"), ("働き始める時期", "start_timing"),
          ("上げたい", "raise_target"), ("変えたい", "change_reason"), ("活かしたい", "use_experience"),
          ("時間帯", "work_hours"), ("離れていた", "blank_period"), ("続けたい", "keep_doing"), ("やめたい", "stop_doing"),
          ("直近の", "career1.role"), ("その前の", "career2.role"), ("何人", "scale"), ("何が変わり", "result")]


def _buttons(text: str) -> list[str]:
    line = next((x for x in text.splitlines() if x.startswith("[ボタン] ")), "")
    return [b for b in line.removeprefix("[ボタン] ").split(" / ") if b]


class ScriptedActor:
    """事実表から答える台本の本人役。合うボタンがあれば押し、無ければ事実表の文をそのまま打つ。"""

    def __init__(self, persona: dict):
        self.facts = {f["key"]: f for f in persona["facts"]}
        self.other = persona.get("other", {})
        self.card = "1"
        self.calls: list[dict] = []

    def __call__(self, messages: list[dict], system: str = "") -> ActorTurn:
        last = messages[-1]["content"]
        self.calls.append({"tired": last.endswith(TIRED), "last": last})
        buttons = _buttons(last)
        if "近いのは" in last:  # segment
            want = self.other.get("今のあなたに近いもの")
            return ActorTurn(button=want if want in buttons else "パス", text="")
        if "直すところ" in last:
            return ActorTurn(button=buttons[0], text="")
        if "始める" in buttons:  # 形 D の「始める」の 1 枚
            return ActorTurn(button="始める", text="")
        # 問いは吹き出しから読む(形 D の Flex の字(カード)は、問いの手がかりにしない)
        asked = "\n".join(x for x in last.splitlines() if not x.startswith(("[ボタン] ", "[カード] ")))
        key = next((k for w, k in TOPICS if w in asked), None)
        if key and key.startswith("career"):
            self.card = key[6]
        elif key in ("scale", "result"):
            key = f"career{self.card}.{key}"
        f = self.facts.get(key)
        if f is None:
            return ActorTurn(button="", text="特にない")
        if f["kind"] == "dont_know":
            return ActorTurn(button="覚えていない", text="") if "覚えていない" in buttons else ActorTurn(button="", text="覚えていない")
        if f["kind"] == "number":
            for b in buttons:  # 単位の合う区切りだけを押す。120 社は「20人以上」を押さずに「120社」と打つ
                lo, hi = (SALARY | SCALE).get(b, (None, None))
                if (lo is not None and J.same_unit(RANGE_UNITS[b], f["unit"])
                        and f["value"] >= lo and (hi is None or f["value"] <= hi)):
                    return ActorTurn(button=b, text="")
            return ActorTurn(button="", text=f"{f['value']}{J.unit_head(f['unit'])}")
        if f["value"] in buttons:
            return ActorTurn(button=f["value"], text="")
        return ActorTurn(button="", text=f["value"])


def scripted_labels(log: str) -> J.TurnLabels:
    """台本のジャッジ: 最後の AI の返信の字面から。問いは「?」と「ください」の数、聞き直しは「もう一度」「あらためて」。"""
    last = [x for x in log.splitlines() if x.startswith("AI: ")][-1]
    asks = last.count("?") + last.count("ください")
    return J.TurnLabels(reask=bool(re.search("もう一度|あらためて", last)), two_questions=asks >= 2,
                        interview_tone=bool(re.search("強み|説明してください", last)), leading=False,
                        evidence=last.removeprefix("AI: ")[:30])


def scripted_same(user: str) -> J.Sameness:
    """台本のジャッジ: 事実表の文と取れた文が、片方がもう片方を含めば同じ。"""
    items = []
    for block in user.split("### ")[1:]:
        key = block.split("(")[0]
        fact = re.search(r"事実表: (.*)", block).group(1).strip()
        got = re.search(r"取れた文: (.*)", block).group(1).strip()
        items.append(J.Same(key=key, same=bool(got) and (got in fact or fact in got), reason=""))
    return J.Sameness(items=items)


class FakeClient:
    """Anthropic の形の偽クライアント。本人役(ActorTurn)とジャッジ(TurnLabels / Sameness / Extraction)を、
    出力の型で返し分ける。"""

    def __init__(self, actor=None, extraction: J.Extraction | None = None, same=scripted_same):
        self.actor, self.extraction, self.same = actor, extraction, same
        self.calls: dict[str, int] = {}
        self.messages = self

    async def parse(self, *, output_format, messages, model, system, **kwargs):
        system = system[0]["text"] if isinstance(system, list) else system  # llm.parse はキャッシュの区切りつきで渡す
        name = output_format.__name__
        self.calls[name] = self.calls.get(name, 0) + 1
        content = messages[0]["content"] if messages else ""
        if output_format is ActorTurn:
            out = self.actor(messages, system)
        elif output_format is J.TurnLabels:
            self.label_inputs = [*getattr(self, "label_inputs", []), content]
            out = scripted_labels(content)
        elif output_format is J.Sameness:
            out = self.same(content)
        elif output_format is J.Extraction:
            self.extract_input = content
            out = self.extraction
        else:
            raise AssertionError(name)
        return SimpleNamespace(parsed_output=out)


def converse(form, persona: dict, client=None):
    client = client or FakeClient(actor=ScriptedActor(persona))
    return asyncio.run(run_conversation(form, persona, client, "sim", "judge")), client


# ----------------------------- ペルソナと事実表 -----------------------------


def test_six_personas_with_fact_tables_as_in_the_design():
    assert [(p["id"], p["segment"]) for p in PERSONAS.values()] == [
        ("p1-sales", "same_path"), ("p2-change", "change"), ("p3-return", "return"),
        ("p4-short", "early"), ("p5-resume", "same_path"), ("p6-unknown", "unknown")]
    for p in PERSONAS.values():
        keys = {f["key"] for f in p["facts"]}
        assert set(hard_keys(p)) <= keys  # HARD は全部事実表にある
        assert {"career1.role", "career1.scale", "career1.result"} <= keys
    # 経理から IT へ: 数字は「覚えていない」が本当
    assert {f["kind"] for f in PERSONAS["p2-change"]["facts"] if f["key"].endswith(".scale")} == {"dont_know"}
    # 介護のブランク: 介護を職歴の 1 枚にする
    assert "介護" in next(f["value"] for f in PERSONAS["p3-return"]["facts"] if f["key"] == "career2.role")
    # 履歴書を先に送っている人だけ、読み取り結果があり、職歴カードの半分と勤務地が埋まっている
    assert [p["id"] for p in PERSONAS.values() if p.get("resume")] == ["p5-resume"]
    p5 = PERSONAS["p5-resume"]
    assert set(p5["resume"]["filled"]) == {"area", "career1.role", "career1.scale", "career2.role"}
    resume = load_resume(p5["resume"]["file"])["resume"]
    assert "福岡市内" in json.dumps(resume, ensure_ascii=False) and "12人" in json.dumps(resume, ensure_ascii=False)
    assert hard_keys(PERSONAS["p6-unknown"]) == ("why_now", "area", "min_annual_salary", "start_timing")


def test_actor_answers_only_from_the_fact_table_and_has_no_points_rule():
    s = system_prompt(PERSONAS["p1-sales"])
    assert "650万円" in s and "みほん事務機で法人営業" in s and "電話をかけ続けるだけの営業" in s
    assert "作りません" in s and "短気" in s
    assert not re.search(r"\+\d|点|得点|ポイント", s)  # 加点の決まりは入れない


def test_select_personas():
    assert len(select(load_personas(), "all")) == 6
    assert [p["id"] for p in select(load_personas(), "p2-change,p5-resume")] == ["p2-change", "p5-resume"]
    with pytest.raises(SystemExit, match="知らない id"):
        select(load_personas(), "p9")


# ----------------------------- 本人役の時間といらいら(コード) -----------------------------


def test_clock_counts_reading_typing_and_buttons():
    c = Clock()
    r = AIReply(bubbles=["あ" * 300], buttons=["い" * 50], flex=["う" * 50])
    assert read_chars(r) == 400
    assert c.read(r) == pytest.approx(60.0)  # 読む 400 字/分(吹き出し + ボタン + Flex の中の字)
    assert c.type("か" * 40) == pytest.approx(60.0)  # 打つ 40 字/分
    assert c.press() == BUTTON_SECONDS
    assert c.seconds == pytest.approx(122.0) and c.typed_chars == 40 and c.presses == 1
    assert not c.tired and c.should_leave() is None
    c.seconds = 181
    assert c.tired
    c.seconds = 301
    assert c.should_leave() == "time"


def test_irritations_add_up_to_leaving():
    c = Clock()
    long = AIReply(bubbles=["あ" * 81, "い" * 80])
    assert [x["kind"] for x in c.irritate(1, long, {})] == ["long_bubble"]  # 80 字を超えた吹き出しだけ
    c.irritate(2, AIReply(bubbles=["ok"]), {"reask": True})
    assert c.should_leave() is None
    c.irritate(3, AIReply(bubbles=["ok"]), {"interview_tone": True, "leading": True})  # 誘導はいらいらに数えない
    assert len(c.irritations) == 3 and c.should_leave() == "irritation"


def test_tired_actor_is_told_to_answer_short():
    t = [{"who": "agent", "text": "こんにちは"}, {"who": "user", "text": "はい"}, {"who": "agent", "text": "何人?"}]
    assert actor_messages(t, tired=False)[-1]["content"] == "何人?"
    assert actor_messages(t, tired=True)[-1]["content"].endswith(TIRED)
    assert [m["role"] for m in actor_messages(t, False)] == ["user", "assistant", "user"]


# ----------------------------- §6.6 の 1: 悪い台本で離脱、良い台本で完了 -----------------------------


@pytest.mark.parametrize("pid", list(PERSONAS))
def test_bad_scripted_ai_makes_the_actor_leave(pid):
    r, client = converse(ScriptBad(PERSONAS[pid]), PERSONAS[pid])
    m = r["metrics"]
    assert not m["completed"] and m["left"] == "irritation"
    assert m["irritations"] >= 3 and m["ai_turns"] <= 3
    kinds = {x["kind"] for x in r["irritations"]}
    assert {"long_bubble", "two_questions"} <= kinds
    assert r["transcript"][-1] == {"who": "user", "text": LEAVE_TEXT}
    assert r["gates"]["two_questions_turns"]  # 関門にも出る
    assert client.calls["TurnLabels"] == m["ai_turns"]  # ジャッジは AI の返信ごとに 1 回


@pytest.mark.parametrize("pid", list(PERSONAS))
def test_good_scripted_ai_lets_the_actor_finish(pid):
    persona = PERSONAS[pid]
    actor = ScriptedActor(persona)
    r, client = converse(ScriptGood(persona), persona, FakeClient(actor=actor))
    m, g = r["metrics"], r["gates"]
    assert m["completed"] and m["left"] is None, r["irritations"]
    assert m["irritations"] == 0 and m["seconds"] <= 300
    assert g == {"made_up_numbers": [], "wasted": 0, "two_questions_turns": [], "leading_turns": [], "hard_empty": []}
    # 事実表から答えたので、取れた事実はどれも正しい。時間の予算で聞かなかった欄だけが残る(HARD と 1 枚目は聞く)
    wrong = [f["key"] for f in r["facts"] if not f["correct"]]
    assert all(r["captured"].get(k, {"status": "missing"})["status"] == "missing" for k in wrong)
    assert not set(wrong) & {*hard_keys(persona), "career1.role", "career1.scale", "career1.result"}
    assert client.calls["ActorTurn"] == m["ai_turns"] - 1  # 本人役は result の前の返信ごとに 1 回
    assert m["button_ratio"] > 0
    assert not r["extracted"]


def test_time_limit_alone_makes_the_actor_leave():
    """いらいらが無くても、読む字が多い AI は 5 分で離脱する。"""

    class Wordy:
        async def start(self):
            return self._r()

        async def send(self, text, pressed):
            return self._r()

        def _r(self):
            return AIReply(bubbles=["働きたい場所はどこですか?"], flex=["お知らせ" * 100], buttons=["パス"])

        captured = lambda self: {}
        saved_texts = lambda self: []
        wasted = lambda self: 0

    persona = PERSONAS["p1-sales"]
    actor = ScriptedActor(persona)
    r, _ = converse(Wordy(), persona, FakeClient(actor=actor))
    assert r["metrics"]["left"] == "time" and r["metrics"]["irritations"] == 0
    assert r["metrics"]["seconds"] > 300
    assert any(c["tired"] for c in actor.calls)  # 3 分を過ぎてからは疲れのメモが付く
    assert r["metrics"]["facts_correct"] == 0


# ----------------------------- 正しく取れた事実 -----------------------------


def got(status="filled", text="", number=None, rng=None, unit=None):
    return {"status": status, "text": text, "number": number, "range": rng, "unit": unit}


def test_numbers_buckets_and_dont_know_are_judged_by_code():
    salary = {"key": "min_annual_salary", "kind": "number", "value": 650, "unit": "万円"}
    assert J.code_verdict(salary, got(number=650, unit="万円")) is True
    assert J.code_verdict(salary, got(text="650万くらい")) is True  # 文の中の数
    assert J.code_verdict(salary, got(number=600, unit="万円")) is False
    per_month = {"key": "career1.scale", "kind": "number", "value": 40, "unit": "件(1か月の契約)"}
    assert J.code_verdict(per_month, got(text="1か月に40件くらい")) is True  # 文の中の数のどれか
    assert J.code_verdict(per_month, got(text="1か月に30件")) is False
    assert J.code_verdict(salary, got(text="600万円以上", rng=[600, None], unit="万円")) is True  # 区切りに入っている
    assert J.code_verdict(salary, got(text="500万円台", rng=[500, 599], unit="万円")) is False
    assert J.code_verdict(salary, got(text="こだわらない")) is False
    assert J.code_verdict(salary, got("dont_know", "覚えていない")) is False
    # 単位の違う数と区切りは正しくしない(§6.4 は「事実表の値と同じ」「その区切りに入っていれば」)
    clients = {"key": "career1.scale", "kind": "number", "value": 120, "unit": "社(担当の取引先)"}
    assert J.code_verdict(clients, got(text="20人以上", rng=[15, None], unit="人")) is False
    assert J.code_verdict(clients, got(text="20人以上", rng=[15, None])) is False  # 単位が無い区切り
    assert J.code_verdict(clients, got(text="120人", number=120, unit="人")) is False
    assert J.code_verdict(clients, got(text="120社くらい", number=120, unit="社")) is True
    assert J.code_verdict(clients, got(text="120社")) is True
    blank = {"key": "blank_period", "kind": "number", "value": 3, "unit": "年"}
    assert J.code_verdict(blank, got(text="3か月")) is False
    assert J.code_verdict(blank, got(text="3か月", number=3, unit="か月")) is False
    assert J.code_verdict(blank, got(text="3年くらい")) is True
    team = {"key": "career2.scale", "kind": "number", "value": 8, "unit": "人(チーム)"}
    assert J.code_verdict(team, got(text="10人くらい", rng=[8, 14], unit="人")) is True
    assert J.code_verdict(team, got(text="8名")) is True  # 同じ単位の言い方の違い
    unknown = {"key": "career1.scale", "kind": "dont_know"}
    assert J.code_verdict(unknown, got("dont_know", "覚えていない")) is True
    assert J.code_verdict(unknown, got(number=10)) is False
    text = {"key": "area", "kind": "text", "value": "大阪市内"}
    assert J.code_verdict(text, got("passed", "パス")) is False and J.code_verdict(text, None) is False
    assert J.code_verdict(text, got(text="大阪")) is None  # 文はジャッジ


def test_text_is_correct_only_when_both_judgements_say_same():
    persona = {"facts": [{"key": "why_now", "kind": "text", "value": "年収を上げたい"},
                         {"key": "area", "kind": "text", "value": "大阪市内"}]}
    answers = iter([True, True, True, False])  # why_now は 2 回とも同じ、area は 2 回目が違う

    def flaky(user):
        return J.Sameness(items=[J.Same(key="why_now", same=next(answers), reason=""),
                                 J.Same(key="area", same=next(answers), reason="")])

    rows = asyncio.run(J.score_facts(FakeClient(same=flaky), "judge", persona,
                                     {"why_now": got(text="給料を上げたい"), "area": got(text="大阪")}))
    assert [(r["key"], r["correct"], r["votes"]) for r in rows] == [("why_now", True, [True, True]),
                                                                  ("area", False, [True, False])]


# ----------------------------- 形 A(今の相談): 会話の記録から事実を取り出す -----------------------------


class FakeRuntime:
    """LINE 側のランタイムの代わり。台本の返信を順に返す。"""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    async def handle(self, user, event, text, facts, channel="web", features=()):
        self.seen.append((event, text, channel, features))
        return self.replies.pop(0)


def line_reply(bubbles, quick=(), cards=(), tools=()):
    return {"bubbles": list(bubbles), "quick_replies": list(quick), "cards": list(cards), "buttons": [],
            "actions": [], "tools": list(tools)}


def test_form_a_reply_is_what_line_shows():
    card = {"job_id": "f001", "title": "法人営業", "employment_type": "正社員", "pay_text": "年収600万円",
            "shift": "平日", "reasons": ["新宿"]}
    r, sends = a_reply(line_reply(["求人です"], ["詳しく"], cards=[card]))
    assert r.done and r.buttons == ["詳しく"] and sends == {"詳しく": "詳しく"}
    assert "法人営業" in r.flex and "年収600万円" in " ".join(r.flex)  # Flex の carousel の中の字
    summary = {"name": "record_conversation_outcome", "args": {"outcome": "consult", "summary": "営業を8年。"}}
    assert a_reply(line_reply(["まとめました"], tools=[summary]))[0].done
    wait = {"name": "record_conversation_outcome", "args": {"outcome": "wait", "reason": "今はいい"}}
    closed, _ = a_reply(line_reply(["わかりました。またどうぞ。"], tools=[wait]))
    assert closed.closed == "ai_closed" and not closed.done
    # wait で閉じたら、summary が付いていても result に数えない
    wait_summary = {"name": "record_conversation_outcome",
                    "args": {"outcome": "wait", "reason": "今はいい", "summary": "営業を8年。"}}
    closed, _ = a_reply(line_reply(["わかりました。"], tools=[wait_summary]))
    assert closed.closed == "ai_closed" and not closed.done
    asking, _ = a_reply(line_reply(["どちらで働きたいですか?"], ["新宿", "品川"]))
    assert not asking.done and not asking.closed
    # LINE で使えない操作は、吹き出しの後ろに断りの文が付く(読む字に入る)
    note, _ = a_reply(line_reply(["履歴書があれば送ってください。"]) | {"actions": ["resume_upload"]})
    assert len(note.bubbles) == 2 and "LINE ではまだ受け取れません" in note.bubbles[1]
    # クイックリプライのラベルは 20 字で切れる。押したら、切れる前の文を送る
    long_label = "これまでの経験を活かせる仕事を探したい"  + "です"
    r, sends = a_reply(line_reply(["どれですか?"], [long_label]))
    assert r.buttons == [long_label[:19] + "…"] and sends[r.buttons[0]] == long_label


def test_form_a_ends_when_the_ai_closes_without_a_result(tmp_path, monkeypatch):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    persona = PERSONAS["p4-short"]
    wait = {"name": "record_conversation_outcome", "args": {"outcome": "wait", "reason": "対象外"}}
    rt = FakeRuntime([line_reply(["こんにちは。何を相談しますか?"], ["正社員の仕事", "今はいい"]),
                      line_reply(["わかりました。またどうぞ。"], tools=[wait])])
    extraction = J.Extraction(items=[])
    r, _ = converse(FormA(rt, persona), persona,
                    FakeClient(actor=lambda m, s: ActorTurn(button="", text="正社員の仕事を探したい"), extraction=extraction))
    assert r["metrics"]["left"] == "ai_closed" and not r["metrics"]["completed"] and r["metrics"]["ai_turns"] == 2
    assert len(rt.seen) == 2  # 閉じた後は送らない


@pytest.mark.parametrize("answered", [0, 1])
def test_form_a_counts_a_decline_as_leaving_however_many_answers(tmp_path, monkeypatch, answered):
    """「今はいい」で切り上げたら、0 問(playbook は wait)でも 1 問以上(consult と summary)でも、本人の断りの離脱。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    persona = PERSONAS["p1-sales"]
    replies = [line_reply(["こんにちは。何を相談しますか?"], ["正社員の仕事", "今はいい"])]
    if answered:
        replies.append(line_reply(["働きたい場所はどこですか?"], ["新宿", "今はいい"]))
        outcome = {"outcome": "consult", "reason": "相談のみ", "summary": "正社員の仕事を新宿で探している。"}
    else:
        outcome = {"outcome": "wait", "reason": "今はいい"}
    replies.append(line_reply(["わかりました。またどうぞ。"],
                              tools=[{"name": "record_conversation_outcome", "args": outcome}]))
    rt = FakeRuntime(replies)
    presses = iter([*(["正社員の仕事"] if answered else []), "今はいい"])
    r, _ = converse(FormA(rt, persona), persona,
                    FakeClient(actor=lambda m, s: ActorTurn(button=next(presses), text=""), extraction=J.Extraction(items=[])))
    m = r["metrics"]
    assert m["left"] == "declined" and not m["completed"] and m["ai_turns"] == answered + 2
    assert rt.seen[-1][1] == "今はいい"


@pytest.mark.parametrize("text", ["今はいい", "今はいい！", "今はいいです。", "いまはいい…", " 今はいい! "])
def test_decline_ignores_full_and_half_width_marks(text):
    assert declined(text)


def test_answers_that_only_contain_the_decline_are_not_declines():
    assert not declined("今はいいところを探しています")


class CountingForm:
    """done_at 回目の AI の返信で result に着く形。send の回数を数える。"""

    def __init__(self, done_at: int):
        self.done_at, self.sends = done_at, 0

    def _reply(self) -> AIReply:
        n = self.sends + 1
        return AIReply(bubbles=["要約です。"] if n == self.done_at else ["場所は?"], buttons=["新宿"], done=n == self.done_at)

    async def start(self) -> AIReply:
        return self._reply()

    async def send(self, text: str, pressed: bool) -> AIReply:
        self.sends += 1
        return self._reply()

    def captured(self) -> dict:
        return {}

    def saved_texts(self) -> list[str]:
        return []

    def wasted(self) -> int:
        return 0


@pytest.mark.parametrize("done_at,completed,left,sends", [(2, True, None, 1), (3, False, "turn_cap", 1)])
def test_turn_cap_counts_ai_replies_and_sends_no_extra(done_at, completed, left, sends):
    """--max-turns は AI の返信の上限。上限の返信が result なら完了、そうでなければ本人役も形も呼ばずに止める。"""
    persona = PERSONAS["p1-sales"]
    form = CountingForm(done_at)
    client = FakeClient(actor=lambda m, s: ActorTurn(button="新宿", text=""), extraction=J.Extraction(items=[]))
    r = asyncio.run(run_conversation(form, persona, client, "sim", "judge", max_turns=2))
    m = r["metrics"]
    assert (m["completed"], m["left"], m["ai_turns"]) == (completed, left, 2)
    assert form.sends == sends and client.calls["ActorTurn"] == sends


def test_form_a_runs_through_the_line_runtime_and_extracts_facts_from_the_log(tmp_path, monkeypatch):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))  # prepare が dip の状態を戻す
    persona = PERSONAS["p1-sales"]
    rt = FakeRuntime([
        line_reply(["こんにちは。何を相談しますか?"], ["正社員の仕事", "アルバイト"]),
        line_reply(["働きたい場所はどこですか?"]),
        line_reply(["年収はいくらから考えますか?"], ["600万円以上", "こだわらない"]),
        line_reply(["新宿で年収600万円以上の求人です"], cards=[{"job_id": "f001", "title": "法人営業", "reasons": ["新宿"]}],
                   tools=[{"name": "save_user_preferences", "args": {"preferences": {"area": "新宿か品川",
                                                                                    "min_annual_salary": "年収700万円"}}}]),
    ])

    def actor(messages, system):
        last = messages[-1]["content"]
        if "相談" in last:
            return ActorTurn(button="正社員の仕事", text="")
        if "場所" in last:
            return ActorTurn(button="", text="新宿か品川")
        return ActorTurn(button="600万円以上", text="")

    extraction = J.Extraction(items=[
        J.Extracted(key="area", status="取れた", text="新宿か品川", number=None, range_low=None, range_high=None, unit=None),
        J.Extracted(key="min_annual_salary", status="取れた", text="600万円以上", number=None, range_low=600,
                    range_high=None, unit="万円")])
    r, client = converse(FormA(rt, persona), persona,
                         FakeClient(actor=actor, extraction=extraction,
                                    same=lambda u: J.Sameness(items=[J.Same(key="area", same=True, reason="")])))
    assert rt.seen[0][:2] == ("visit", None) and rt.seen[1][1] == "正社員の仕事"
    assert "本人が先に送った履歴書" not in client.extract_input  # 履歴書の無い人
    assert all(s[2:] == ("line", ()) for s in rt.seen)  # LINE のトーク、LIFF なし
    m = r["metrics"]
    assert m["completed"] and r["extracted"] and client.calls["Extraction"] == 1
    assert m["facts_correct"] == 2 and m["facts_total"] == len(persona["facts"])
    assert r["captured"]["why_now"]["status"] == "missing"  # 取り出されなかった項目は取れていない
    assert r["gates"]["made_up_numbers"] == ["「年収700万円」の 700"]  # 本人は 700 と言っていない
    assert r["gates"]["wasted"] is None  # A は数えられない
    assert r["gates"]["hard_empty"] == ["why_now", "start_timing", "raise_target"]


def test_resume_numbers_are_not_made_up():
    assert made_up_numbers(["スタッフ12人"], ["はい"], allowed='{"duties": ["スタッフ12人のシフト"]}') == []
    assert made_up_numbers(["スタッフ12人"], ["はい"]) == ["「スタッフ12人」の 12"]
    assert made_up_numbers(["3か月以内"], ["[ボタン] 3か月以内"]) == []


# ----------------------------- 報告と実行のコマンド -----------------------------


def _run(form, persona, metrics=None, **gates):
    base_m = {"facts_correct": 7, "facts_total": 10, "facts_rate": 0.7, "completed": True, "left": None,
              "seconds": 200.0, "typed_chars": 100, "button_ratio": 0.5, "ai_turns": 12, "irritations": 0,
              "tired_ignored": 0, "invalid_buttons": 0, "has_resume": False}
    base_g = {"made_up_numbers": [], "wasted": 0, "two_questions_turns": [], "leading_turns": [], "hard_empty": []}
    return {"form": form, "persona": persona, "rep": 0, "status": "ok", "metrics": base_m | (metrics or {}),
            "gates": base_g | gates, "facts": [], "irritations": [], "turns": [], "extracted": False}


def test_pass_lines_and_improvement():
    good = summarize([_run("B", "p1"), _run("B", "p2", {"has_resume": True, "seconds": 170.0})])
    assert verdicts(good) == {"facts": True, "completion": True, "seconds": True, "typed": True, "gates": True}
    bad = summarize([_run("A", "p1", {"facts_correct": 5, "facts_rate": 0.5}),
                     _run("A", "p2", {"completed": False, "left": "time", "facts_correct": 5}, two_questions_turns=[3])])
    v = verdicts(bad)
    assert v["facts"] is False and v["completion"] is False and v["gates"] is False
    assert improved(bad, good) is True  # 事実が平均 2 項目増え、離脱は 1 → 0
    worse = summarize([_run("C", "p1", {"facts_correct": 9}), _run("C", "p2", {"completed": False, "left": "time"}),
                       _run("C", "p3", {"completed": False, "left": "irritation"})])
    assert improved(good, worse) is False  # 事実は増えても、離脱が増えた


def test_improvement_uses_unrounded_means():
    base = summarize([_run("A", f"p{i}", {"facts_correct": 3 + (i < 3)}) for i in range(12)])  # 39 / 12 = 3.25
    other = summarize([_run("B", f"p{i}", {"facts_correct": 4 + (i < 2)}) for i in range(12)])  # 50 / 12 = 4.17
    assert improved(base, other) is False  # 差は 0.92 項目(丸めると 3.3 と 4.2 で 1 に見えない)


def test_bad_script_counts_wasted_questions():
    async def go():
        bad = ScriptBad(PERSONAS["p1-sales"])
        await bad.start()
        for text in ("年収を上げたい、新宿", "650万", "営業"):
            await bad.send(text, False)
        return bad

    bad = asyncio.run(go())
    assert bad.wasted() == 2  # 3 回目に、もう答えた why_now と area を聞き直した
    assert bad.captured()["why_now"]["text"] == "年収を上げたい、新宿"


def test_report_has_form_and_persona_tables():
    doc = {"started_at": "2026-10-26 12:00", "spent_usd": 0.0, "ledger_total_usd": 0.0, "stopped": None,
           "config": {"forms": ["A", "B"], "personas": ["p1", "p2"], "repeat": 1, "sim_model": "s", "judge_model": "j",
                      "same_samples": 2, "ledger": "x.jsonl", "limit_usd": 1.0},
           "runs": [_run("A", "p1"), _run("A", "p2", wasted=None),
                    _run("A", "p1", {"completed": False, "left": "declined"}), _run("B", "p1"), _run("B", "p2")]}
    md = report(doc)
    assert "## 形ごと" in md and "## ペルソナごと" in md and "## 合格の線" in md and "## A との差" in md
    assert "| p2 | A | 1 |" in md and "| B | 2 | 70% |" in md
    assert "本人が断った 1" in md
    assert "- 疲れたのに 11 字以上打った: 0 回" in md  # conversation の tired_ignored が数えるもの


def test_command_needs_a_limit_and_known_forms():
    with pytest.raises(SystemExit):
        parse_args(["--form", "A"])  # --limit-usd は必須
    a = parse_args(["--form", "A,script-good", "--persona", "p1-sales", "--repeat", "2", "--limit-usd", "1.5",
                    "--ledger", "x.jsonl"])
    assert a.forms == ["A", "script-good"] and a.repeat == 2 and a.limit_usd == 1.5 and str(a.ledger) == "x.jsonl"
    for empty in (",", " "):
        with pytest.raises(SystemExit):
            parse_args(["--form", empty, "--limit-usd", "1"])  # 形が 0 個なら、空の報告を書かずに止める
    with pytest.raises(SystemExit):
        parse_args(["--form", "A", "--limit-usd", "1", "--max-turns", "0"])
    with pytest.raises(SystemExit):
        parse_args(["--form", "A", "--limit-usd", "1", "--same-samples", "0"])
    check_forms(["A", "B", "C", "D", "D-noband"])  # 形 B〜D は 5/5 のタスクで実 LLM につないだ
    with pytest.raises(SystemExit, match="知らない形"):
        check_forms(["Z"])


def test_command_runs_scripted_forms_and_writes_the_report(tmp_path):
    ledger = tmp_path / "budget.jsonl"
    actors = {pid: ScriptedActor(p) for pid, p in PERSONAS.items()}

    def actor(messages, system):  # いま話しているペルソナは、本人役の指示(事実表)で決める
        pid = next(pid for pid, p in PERSONAS.items() if p["facts"][0]["value"] in system)
        return actors[pid](messages, system)

    code = asyncio.run(main(["script-bad", "script-good"], "all", 1, 1.0, ledger, client=FakeClient(actor=actor),
                            out_dir=tmp_path, log=lambda m: None))
    assert code == 0
    doc = json.loads(next(tmp_path.glob("fulltime-hearing-*.json")).read_text())
    assert len(doc["runs"]) == 12 and all(r["status"] == "ok" for r in doc["runs"])
    by_form = {f: [r for r in doc["runs"] if r["form"] == f] for f in ("script-bad", "script-good")}
    assert not any(r["metrics"]["completed"] for r in by_form["script-bad"])
    assert all(r["metrics"]["completed"] for r in by_form["script-good"])
    p5 = next(r for r in by_form["script-good"] if r["persona"] == "p5-resume")
    assert p5["metrics"]["seconds"] <= 180  # 履歴書ありの目標時間
    md = next(tmp_path.glob("fulltime-hearing-*.md")).read_text()
    rows: dict[str, list[str]] = {}  # 形ごとの表(最初に出る表)の行
    for x in md.splitlines():
        if x.startswith("| script-"):
            rows.setdefault(x.split("|")[1].strip(), x.split("|"))
    assert rows["script-good"][5].strip() == "100%" and rows["script-bad"][5].strip() == "0%"  # 完了率
    assert "いらいら T1: 吹き出しが 80 字を超えた" in md
    assert len(ledger.read_text().splitlines()) == 12  # 1 会話ごとに台帳に 1 行


def test_command_stops_before_starting_when_the_ledger_is_at_the_limit(tmp_path):
    ledger = tmp_path / "budget.jsonl"
    ledger.write_text(json.dumps({"run": "x", "cost_usd": 1.0}) + "\n")
    from evals import budget

    with pytest.raises(budget.BudgetExceeded):
        asyncio.run(main(["script-good"], "all", 1, 1.0, ledger, client=FakeClient(), out_dir=tmp_path,
                         log=lambda m: None))
    assert not list(tmp_path.glob("fulltime-hearing-*"))


def test_render_is_what_the_actor_reads():
    r = AIReply(bubbles=["何人くらいのチームでしたか?"], buttons=["5人くらい", "パス"], flex=["職歴1 / まだ"])
    assert render(r) == "何人くらいのチームでしたか?\n[カード] 職歴1 / まだ\n[ボタン] 5人くらい / パス"


def test_form_a_through_the_real_runtime_and_dip(tmp_path, monkeypatch):
    """形 A を本物の LINE 側のランタイムと dip の MCP で(エージェントのモデルだけ台本)。履歴書を先に送っている人は、
    会話の前に dip にアップロード済みになり、保存した希望とまとめから関門を数える。"""
    from line_runtime.runtime import LineRuntime
    from tests.test_handle import ScriptedModel, call
    from tests.test_handle import reply as agent_reply

    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    persona = PERSONAS["p5-resume"]
    model = ScriptedModel(script=[
        agent_reply(["こんにちは。今日はどうしますか?"], quick_replies=["正社員の仕事を探す", "今はいい"]),
        call("save_user_preferences", {"line_user_id": "sim-new", "preferences": {"area": "福岡市内"}}),
        agent_reply(["なぜ今、転職を考えていますか?"], quick_replies=["パス"]),
        call("record_conversation_outcome", {"line_user_id": "sim-new", "outcome": "consult", "reason": "役職",
                                             "confidence": 0.8, "summary": "福岡市内で正社員。役職を上げたい。年収は550万円以上。"}),
        agent_reply(["まとめました。続きはいつでもどうぞ。"], quick_replies=[]),
    ])

    def actor(messages, system):
        last = messages[-1]["content"]
        if "どうしますか" in last:
            return ActorTurn(button="正社員の仕事を探す", text="")
        return ActorTurn(button="", text="役職が上がらないから")

    extraction = J.Extraction(items=[
        J.Extracted(key="area", status="取れた", text="福岡市内", number=None, range_low=None, range_high=None, unit=None)])

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            form = FormA(rt, persona)
            r = await run_conversation(form, persona, client, "sim", "judge")
            from dip_mcp import logic
            from dip_mcp.data import State

            return r, logic.conversation_summary(State(), "sim-new")

    client = FakeClient(actor=actor, extraction=extraction)
    r, summary = asyncio.run(go())
    assert summary["uploaded_resume"]["work_history"][1]["organization"] == "みほんホテル株式会社"
    assert r["metrics"]["completed"] and r["metrics"]["ai_turns"] == 3
    assert r["gates"]["made_up_numbers"] == ["「福岡市内で正社員。役職を上げたい。年収は550万円以上。」の 550"]
    assert r["captured"]["area"]["text"] == "福岡市内"
    # 先に送った履歴書は、形 A の事実の取り出しにも渡す(B〜D が high として入れるのと同じ扱い)
    assert "本人が先に送った履歴書" in client.extract_input and "みほんホテル株式会社" in client.extract_input


# ----------------------------- 形 B、C、D: ハーネス + 実 LLM(ここでは台本のモデル) -----------------------------


def harness_model(calls: list | None = None, fail: set | None = None):
    """ハーネスのモデルの台本(line_runtime.career_chat.default_llm と同じ形)。読み取りは本人の発言をそのまま今の問いの欄に、
    生成は次に聞く問いの文をそのまま返す。fail に入れた段(stage)は、失敗(例外)にする。"""
    async def call(stage, system, messages):
        if calls is not None:
            calls.append((stage, messages))
        if stage in (fail or set()):
            raise RuntimeError("API が落ちた")
        if stage == "say":
            return {"reply": json.loads(re.search(r"次に聞くこと\(JSON\): (.*)", system).group(1))["text"]}, {}
        targets = json.loads(re.search(r"この質問が埋めたい欄: (.*)", system).group(1))
        said = re.search(r"本人の発言: (.*)", system).group(1).strip()
        intent = {"特にない": "pass", "パス": "pass", "覚えていない": "dont_know"}.get(said, "answer")
        ups = []
        if intent == "answer" and targets:
            m = re.match(r"(\d+)(\D*)", said)
            ups = [{"slot": targets[0], "text": said, "number": float(m.group(1)) if m else None,
                    "unit": (m.group(2) or None) if m else None, "choice": None}]
        return {"slot_updates": ups, "ng_updates": [], "segment": None, "intent": intent,
                "review_ack": None if ups else "all"}, {}
    return call


@pytest.mark.parametrize("form", ["B", "C", "D"])
def test_harness_forms_reach_the_result_and_report_the_end_state(form):
    """形 B〜D を、ハーネスと台本のモデルで回す。result に着いた会話は完了で、取れた値はハーネスの終わりの状態。
    聞き直しと作った数字は 0。形 D だけ Flex の字を本人役が読む。"""
    persona = PERSONAS["p1-sales"]
    calls: list = []
    form_ = FormHarness(persona, form, call=harness_model(calls))
    r, _ = converse(form_, persona)
    assert r["metrics"]["completed"] and not r["extracted"]
    assert set(hard_keys(persona)) <= set(r["captured"]) and r["gates"]["wasted"] == 0
    assert r["gates"]["made_up_numbers"] == [] and r["gates"]["hard_empty"] == []
    assert form_.st.result is not None and "## 職務経歴" in form_.saved_texts()[-1]  # 下書きも保存した文に入る
    flex = [t for row in r["turns"] for t in row["reply"]["flex"]]
    assert bool(flex) == (form == "D")
    assert {s for s, _ in calls} == {"extract", "say"}
    # 生成には直前のやりとりを渡す(ランタイムの _harness_turn と同じ)。読み取りは今の発言だけ
    assert all(len(m) == 1 for s, m in calls if s == "extract")
    first_say = next(m for s, m in calls if s == "say")
    assert first_say[0]["role"] == "user" and first_say[1]["role"] == "assistant"  # 最初の問いも履歴に入る


@pytest.mark.parametrize("pid", ["p1-sales", "p3-return", "p4-short"])
@pytest.mark.parametrize("form", ["B", "C", "D"])
def test_harness_forms_pass_the_bank_candidates_to_the_judge(form, pid):
    """形 B〜D は、質問バンクの決まった候補をジャッジに渡す(誘導に数えない。task 315、設計メモ §6.4)。
    ハーネスが出したボタンはどれも一覧にある(一覧に無い候補を作っていたら、ジャッジが誘導か判定できるように落とす)。"""
    persona = PERSONAS[pid]
    form_ = FormHarness(persona, form, call=harness_model())
    r, client = converse(form_, persona)
    fixed = set(form_.fixed_buttons())
    shown = {b for row in r["turns"] for b in row["reply"]["buttons"]}
    assert shown and shown <= fixed
    assert {"売上が伸びた", "人をまとめた経験", "スキップ", "他の候補"} <= fixed
    assert all("## 決まった候補" in c for c in client.label_inputs)


def test_scripted_forms_give_the_judge_no_bank_candidates():
    """台本の形(と形 A)はボタンを自分で作るので、決まった候補を渡さない(候補はこれまでどおり誘導か判定する)。"""
    persona = PERSONAS["p1-sales"]
    _, client = converse(ScriptGood(persona), persona)
    assert client.label_inputs and not any("決まった候補" in c for c in client.label_inputs)
    assert J.label_user([{"who": "agent", "text": "?"}], ["スキップ"]).endswith("決まった候補(誰にでも同じ問いで同じに出す答えの候補。誘導に数えない)\nスキップ")


def test_harness_form_resume_persona_starts_with_the_resume_fields():
    persona = PERSONAS["p5-resume"]
    f = FormHarness(persona, "C", call=harness_model())
    assert f.st.has_resume and all(f.st.slots[k]["from"] == "resume" for k in persona["resume"]["filled"])


def test_harness_form_counts_failed_model_calls_and_still_answers():
    """モデルが落ちても(ランタイムと同じく失敗の印を渡す)、ハーネスが質問の原文で返し、会話は止まらない。"""
    persona = PERSONAS["p1-sales"]
    f = FormHarness(persona, "C", call=harness_model(fail={"say"}))
    asyncio.run(f.start())
    r = asyncio.run(f.send("同じ仕事で上へ", True))
    assert r.bubbles and not r.done and f.st.turns == 1


def test_harness_form_later_is_leaving_not_completing():
    persona = PERSONAS["p1-sales"]
    f = FormHarness(persona, "C", call=harness_model())
    asyncio.run(f.start())
    r = asyncio.run(f.send("あとで", True))
    assert not r.done and r.closed == "declined"


def test_harness_form_c2_runs_the_layer_and_sends_its_masked_history():
    """形 D-c2(task 362)は、会話の層の版(dip_mcp/career/c2.py)で回す。言う段のメッセージは、ランタイムと同じく
    層が渡す直前のやりとり(model_history)と本人の発言(model_text)。読む段は今の発言だけ。"""
    from dip_mcp.career import c2

    calls: list = []

    async def call(stage, system, messages):
        calls.append((stage, messages))
        if stage == "say":
            return {"reply": json.loads(re.search(r"次に聞くこと\(JSON\): (\{.*\})", system).group(1))["text"]}, {}
        return {"acts": [{"act": "chitchat"}], "hook": None, "mentions": [], "ng": []}, {}

    assert "D-c2" in FORMS
    f = FormHarness(PERSONAS["p1-sales"], "D", call=call, version="c2")
    assert isinstance(f.st, c2.State)
    first = asyncio.run(f.start())
    asyncio.run(f.send(first.buttons[0], True))
    asked = asyncio.run(f.send("同じ仕事で上へ", True))
    asyncio.run(f.send("今日は雨だね", False))
    reads = [m for s, m in calls if s == "read"]
    assert reads == [[{"role": "user", "content": "今日は雨だね"}]]
    last_say = [m for s, m in calls if s == "say"][-1]
    assert last_say[-1] == {"role": "user", "content": "今日は雨だね"}
    assert {"role": "assistant", "content": asked.bubbles[0]} in last_say  # 層の覚えの直前のやりとり


def test_combine_reports_from_several_runs_and_excludes_by_file(tmp_path):
    """別々に回した実行(A と B〜D など)を 1 つの報告にまとめる。外す会話はファイルの番号で決める(回し直した会話を外さない)。"""
    def doc(form, reps, cost):
        runs = [_run(form, p) | {"persona": p, "rep": r} for r in range(reps) for p in ("p1", "p2")]
        return {"started_at": "2026-10-27 10:00", "spent_usd": cost, "ledger_total_usd": cost, "runs": runs,
                "config": {"forms": [form], "personas": ["p1", "p2"], "repeat": reps, "sim_model": "s", "judge_model": "j",
                           "same_samples": 2, "ledger": "x.jsonl", "limit_usd": 10}}
    first, redo = doc("B", 2, 1.0), doc("B", 1, 0.5)
    merged = combine([first, redo], {"1:B/p1#2"})
    p1 = [r for r in merged["runs"] if r["persona"] == "p1"]
    assert len(p1) == 2 and [r["rep"] for r in p1] == [0, 1]  # 1 つ目の 2 回目を外し、回し直した分は残す。回は振り直す
    assert merged["spent_usd"] == 1.5 and merged["config"]["forms"] == ["B"]
    with pytest.raises(ValueError, match="2:B/p1#2"):
        combine([first, redo], {"2:B/p1#2"})  # 回し直した方には 2 回目が無い: 打ち間違いとして止める
    paths = []
    for i, d in enumerate((first, redo)):
        paths.append(tmp_path / f"r{i}.json")
        paths[-1].write_text(json.dumps(d, ensure_ascii=False))
    out = tmp_path / "x.md"
    assert report_main([str(paths[0]), str(paths[1]), "--exclude", "1:B/p1#2", "--out", str(out)]) == 0
    assert "| B | 5 |" in out.read_text()
