"""場面の一覧を回す仕組み(evals/scenes/runner.py、checks.py、judge.py。設計メモ docs/design/conversation-layer.md §5 の 1 枚目)。

実 API は呼ばない。career と baito は dip の相談のツールを台本の LLM で回し、talk は本物のランタイムをモックのトークの AI で回す。
ここで確かめるのは、条件を判定できること(判定の中身が正しいこと)と、今の版の点が設計メモ §2.6 と §7 で確かめた振る舞いと
合うこと。今の版の点そのもの(合格の数)は、設計メモ §7 に書く(テストでは数を固定しない)。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from evals import scenes as S
from evals.scenes import __main__ as cli
from evals.scenes import checks as K
from evals.scenes import judge as J
from evals.scenes import runner as R

BY_ID = {s["id"]: s for s in S.load_scenes()}


@pytest.fixture(scope="module")
def harness_rows():
    """career(c1)と baito(v4)の全部の場面(数秒)。"""
    return {r["scene"]: r for r in R.run(["career", "baito"])}


def result(row: dict, check: str, at=None, **args) -> dict:
    """場面の条件の結果(同じ check が 2 つ以上あれば、args で引数を絞る)。"""
    return next(x for x in row["results"] if x["check"] == check and x["at"] == at
                and all(x["args"].get(k) == v for k, v in args.items()))


# ----------------------------- 回す -----------------------------
def test_every_harness_scene_runs_and_every_code_check_is_decided(harness_rows):
    scenes = [s for s in BY_ID.values() if s["entry"] != "talk"]
    assert set(harness_rows) == {s["id"] for s in scenes}
    for s in scenes:
        row = harness_rows[s["id"]]
        assert row["error"] is None, (s["id"], row["error"])
        assert len(row["replies"]) == len(s["says"])
        for x in row["results"]:
            # コードの条件は、トークの AI がこのターンを受けたとき(相談を閉じた後)と、逆質問の答えの部品のほかは、
            # どれも pass か fail に決まる
            if x.get("judge"):
                assert x["status"] == K.SKIP
            elif x["status"] == K.SKIP:
                assert "トークの AI" in x["detail"] or "answer" in x["detail"], (s["id"], x)


def test_current_scores_match_what_the_design_memo_saw_by_running_c1(harness_rows):
    """設計メモ §2.6 の「動かした」もの(task 327 が偽の LLM で確かめた c1 の振る舞い)を、場面の条件が同じに判定する。"""
    rows = harness_rows
    # 3: 前に答えた欄を、まとめの外では直せない
    assert result(rows["correct-area-midway"], "slot")["status"] == K.FAIL
    # 2: 答えでない発言の中の値を捨てる(年収の問いへの勤務地の言い足し)
    assert result(rows["multi-answer-other-question"], "slot")["status"] == K.FAIL
    # 4: 「はい」に「読み取れなかったので…」
    assert result(rows["short-hai"], "reply_not_contains")["status"] == K.FAIL
    assert "読み取れなかった" in rows["short-hai"]["replies"][0]["text"]
    # 8: 「年収を上げたい」を押しても、上げたいものに入らない
    assert result(rows["multi-infer-raise-target"], "slot", slot="raise_target")["status"] == K.FAIL
    # 6: 相づちの言い回しを前のターンと比べない(台本の LLM はわざと「〜ですね。」を続ける)
    assert result(rows["t326-salary-reask"], "ack_varies")["status"] == K.FAIL
    # #326 で直したところ(区切りは相づちの後、「他の候補」で何の候補か分かる)は合格する
    assert all(x["status"] == K.PASS for x in rows["t326-order"]["results"] if not x.get("judge"))
    assert all(x["status"] == K.PASS for x in rows["t326-more-lead"]["results"] if not x.get("judge"))


def test_guards_are_decided_by_code(harness_rows):
    # 危ない言葉: アルバイト・パートは決まった文をモデルなしで返し、正社員は気づかずに 2 回呼ぶ(設計メモ §2.6 の 10)
    baito = harness_rows["baito-safety-crisis"]
    assert result(baito, "crisis_reply")["status"] == K.PASS and baito["replies"][0]["llm_calls"] == 0
    career = harness_rows["safety-crisis"]
    assert result(career, "crisis_reply")["status"] == K.FAIL and career["replies"][0]["llm_calls"] == 2
    # 闇バイトの注意文は今のまま出る。注意文の数(#9110)は決まった文なので、作った数にしない
    danger = harness_rows["baito-safety-danger"]
    assert result(danger, "reply_contains")["status"] == K.PASS
    assert result(danger, "no_made_up_number")["status"] == K.PASS
    # c1 は今の問いへの答えを本人の文のまま入れるので、伏せた印と氏名が値に入る
    assert result(harness_rows["safety-pii-redacted"], "no_slot_contains")["status"] == K.FAIL
    assert result(harness_rows["safety-full-name"], "no_slot_contains")["status"] == K.FAIL


def test_turns_that_leave_the_harness_go_to_the_talk_ai(harness_rows):
    # 「やめる」は相談を閉じてトークの AI に渡す。トークの AI は少なくとも 1 回モデルを呼ぶ
    stop = harness_rows["baito-flow-stop"]
    rep = stop["replies"][0]
    assert rep["how"] == "talk" and rep["closed"] == "exit" and rep["llm_calls"] >= 1
    assert result(stop, "closed")["status"] == K.PASS and result(stop, "llm_calls_max")["status"] == K.FAIL
    # 「やっぱり正社員で探したい」は今、閉じるだけ。移るか、区分と値を引き継ぐかはトークの AI しだいなので判定しない
    sw = harness_rows["baito-switch-to-fulltime"]
    assert sw["replies"][0]["how"] == "talk"
    for check in ("switches_to", "profile", "slot", "not_asks", "asks"):
        assert all(x["status"] == K.SKIP and "トークの AI" in x["detail"] for x in sw["results"] if x["check"] == check)


def test_closed_consultation_keeps_its_last_state(harness_rows):
    # 「あとで」と一緒に打った答えは、閉じた後の状態(session._finish の前)で見る
    row = harness_rows["flow-later-with-answer"]
    assert row["replies"][0]["closed"] == "later"
    assert result(row, "slot", slot="area")["status"] == K.PASS
    assert row["replies"][0]["states"]["career"]["active"] is False


def test_stickers_are_dropped_by_the_current_line_entrance(harness_rows):
    row = harness_rows["chat-sticker"]
    assert row["replies"][0]["how"] == "dropped" and row["replies"][0]["text"] == ""
    assert result(row, "keeps_question")["status"] == K.FAIL


def test_talk_scenes_judge_only_the_guard_and_stays():
    rows = {r["scene"]: r for r in R.run(["talk"])}
    assert set(rows) == {s["id"] for s in BY_ID.values() if s["entry"] == "talk"}
    assert all(r["error"] is None for r in rows.values())
    crisis = rows["talk-crisis"]
    # 今のトークの AI には守りの前処理が無い: 危ない言葉でもモデルを呼ぶ
    assert result(crisis, "crisis_reply")["status"] == K.FAIL
    assert crisis["replies"][0]["llm_calls"] == 1
    assert result(crisis, "stays")["status"] == K.PASS
    hello = rows["talk-hello"]
    assert result(hello, "stays")["status"] == K.PASS
    assert result(hello, "offers_entries")["status"] == K.SKIP  # 返事の文はモックのもの。9 枚目で見る


def test_unknown_version_is_refused():
    with pytest.raises(ValueError, match="まだ回せない"):
        R.driver("career", "c9")


def test_cli_prints_the_score_table(capsys, tmp_path):
    assert cli.main(["--entry", "career", "--scene", "t326-order", "--log", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "○ career/t326-order" in out and "| career c1 | 1 |" in out
    assert (tmp_path / "results.json").is_file() and (tmp_path / "score.md").is_file()
    assert cli.main(["--entry", "career", "--version", "c9"]) == 2


# ----------------------------- 読む段の正解を今の版に写す -----------------------------
def test_reading_is_mapped_to_the_c1_vocabulary():
    def read(*acts, targets=("area",)):
        return R.to_c1({"acts": list(acts)}, list(targets))
    assert read({"act": "chitchat"})["intent"] == "aside"
    assert read({"act": "later"}, {"act": "answer", "slot": "area", "text": "大阪"})["intent"] == "later"
    assert read({"act": "stop"})["intent"] == "later"
    assert read({"act": "pass"})["intent"] == "pass"
    # ほかの欄の「わからない」は、今の問いのパスにしない(c1 に行き先が無い)
    assert read({"act": "dont_know", "slot": "start_timing"})["intent"] == "aside"
    assert read()["intent"] == "answer" and read()["slot_updates"] == []
    out = read({"act": "correct", "slot": "min_annual_salary", "text": "600万以上", "number": 600, "unit": "万"},
               {"act": "complaint"})
    assert out["intent"] == "answer"
    assert out["slot_updates"] == [{"slot": "min_annual_salary", "text": "600万以上", "number": 600, "unit": "万", "choice": None}]
    assert read({"act": "confirm"})["review_ack"] == "all"
    assert read({"act": "answer", "slot": "segment", "text": "高校生", "value": "early"})["segment"] == "early"


def test_reading_is_mapped_to_the_v4_vocabulary():
    def read(*acts):
        return R.to_v4({"acts": list(acts)}, ["days_per_week"])
    assert read({"act": "danger"})["intent"] == "danger"
    assert read({"act": "later"})["intent"] == "aside"
    assert read({"act": "dont_know"})["intent"] == "answer"  # v4 に語彙が無い
    out = read({"act": "add", "slot": "remote", "text": "家で", "value": "yes"})
    assert out["slot_updates"] == [{"slot": "remote", "value": "yes", "evidence": "家で"}]


# ----------------------------- 条件の判定 -----------------------------
def _ctx(texts, replies, scene=None):
    return {"scene": scene or {"entry": "career", "history": [], "says": [], "expect": []}, "replies": replies,
            "texts": texts, "talk_mock": False}


def _reply(text, choices=(), **kw):
    return {"text": text, "bubbles": [text], "choices": list(choices), "counted": False, "llm_calls": 1, "how": "harness",
            "closed": None, "states": {}, "profile": {}, "say": {"text": "x", "button": False}} | kw


TEXTS = R.Texts(questions=["働きたい場所はどこですか?"], phase_lines=["次は希望の条件です"], notes=["注意してね"],
                crisis=["話してくれてありがとう。", "・窓口 0120-000-000"])


def test_parts_are_split_in_order():
    assert K.split_parts("いいですね。\n次は希望の条件です\n働きたい場所はどこですか?", TEXTS) == [
        ["ack", "いいですね。"], ["phase_line", "次は希望の条件です"], ["ask", "働きたい場所はどこですか?"]]
    ok = K.judge_code({"check": "order", "parts": ["ack", "phase_line", "ask"]},
                      _ctx(TEXTS, [_reply("次は希望の条件です\nいいですね。働きたい場所はどこですか?")]))
    assert ok["status"] == K.FAIL and "phase_line" in ok["detail"]


def test_ack_varies_reads_history_and_replies():
    scene = {"entry": "career", "history": [{"who": "agent", "text": "Aですね。働きたい場所はどこですか?"}], "says": [],
             "expect": []}
    same = _ctx(TEXTS, [_reply("Bですね。働きたい場所はどこですか?"), _reply("Cですね。働きたい場所はどこですか?")], scene)
    assert K.judge_code({"check": "ack_varies", "turns": 3}, same)["status"] == K.FAIL
    varied = _ctx(TEXTS, [_reply("Bですね。働きたい場所はどこですか?"), _reply("なるほど。働きたい場所はどこですか?")], scene)
    assert K.judge_code({"check": "ack_varies", "turns": 3}, varied)["status"] == K.PASS


def test_numbers_need_a_source():
    scene = {"entry": "career", "history": [], "says": [{"text": "週3で", "button": False}], "known": {},
             "expect": [{"check": "answers_from_facts", "facts": ["180日で消えます"]}]}
    # 本人の発言(3)、この返信のボタン(1200)、渡した事実(180)、決まった文の窓口の行(0120、000)は出どころがある
    good = _ctx(TEXTS, [_reply("週3で1,200円、180日。\n・窓口 0120-000-000", choices=["1,200円以上"])], scene)
    assert K.judge_code({"check": "no_made_up_number"}, good)["status"] == K.PASS
    for made in ("平均は450万です", "平均は五百万円です", "普通は千二百円くらい", "0120 に電話して"):
        bad = _ctx(TEXTS, [_reply(made)], scene)
        assert K.judge_code({"check": "no_made_up_number"}, bad)["status"] == K.FAIL, made
    # 漢数字でも、数でない「一言」は数えない
    assert K.judge_code({"check": "no_made_up_number"}, _ctx(TEXTS, [_reply("一言でどうぞ")], scene))["status"] == K.PASS


def test_numbers_on_buttons_not_shown_are_not_a_source(harness_rows):
    # 質問バンクのほかの問いのボタン(「1000万円以上」)は、この返信の出どころにならない
    scene, row = BY_ID["ask-market-salary"], harness_rows["ask-market-salary"]
    texts = R.driver("career").texts()
    for made in ("年収の相場は700万円くらいです。", "相場は1000万円です"):
        rep = dict(row["replies"][0], text=made, bubbles=[made])
        assert K.judge_code({"check": "no_made_up_number"}, _ctx(texts, [rep], scene))["status"] == K.FAIL, made
    shown = dict(row["replies"][0], text="500万円台から選べます。", bubbles=["500万円台から選べます。"])
    assert K.judge_code({"check": "no_made_up_number"}, _ctx(texts, [shown], scene))["status"] == K.PASS


def test_undecided_turns_do_not_hide_a_failure():
    # at=all: トークの AI が受けたターン(決まらない)の後の、ハーネスの返信の不合格は不合格のまま
    talk = _reply("", how="talk")
    miss = _reply("働きたい場所はどこですか?")
    c = {"check": "reply_contains", "any": ["中断"], "at": "all"}
    assert K.judge_code(c, _ctx(TEXTS, [talk, miss]))["status"] == K.FAIL
    assert K.judge_code(c, _ctx(TEXTS, [talk, _reply("「中断」でやめられます")]))["status"] == K.SKIP
    # 返事が無いターン: 返事を前提にする条件は不合格、「含まない」は満たす
    dropped = _reply("", how="dropped", bubbles=[], note="スタンプ")
    assert K.judge_code({"check": "reply_contains", "any": ["x"]}, _ctx(TEXTS, [dropped]))["status"] == K.FAIL
    assert K.judge_code({"check": "reply_not_contains", "texts": ["x"]}, _ctx(TEXTS, [dropped]))["status"] == K.PASS
    # 逆質問の答えの部品は、部品の並びを返す層でなければ判定しない
    order = {"check": "order", "parts": ["answer", "ask"]}
    assert K.judge_code(order, _ctx(TEXTS, [_reply("AIです。働きたい場所はどこですか?")]))["status"] == K.SKIP
    parts = _reply("AIです。働きたい場所はどこですか?", parts=[["answer", "AIです。"], ["ask", "働きたい場所はどこですか?"]])
    assert K.judge_code(order, _ctx(TEXTS, [parts]))["status"] == K.PASS


def test_amounts_and_crisis_reply():
    assert K.amounts(["400万円未満", "こだわらない", "1,200円以上"]) == [400, 1200]
    shown = _reply("話してくれてありがとう。\n・窓口 0120-000-000", llm_calls=0)
    assert K.judge_code({"check": "crisis_reply"}, _ctx(TEXTS, [shown]))["status"] == K.PASS
    called = dict(shown, llm_calls=1)  # 文が同じでも、モデルを呼んだら決まった文ではない
    assert K.judge_code({"check": "crisis_reply"}, _ctx(TEXTS, [called]))["status"] == K.FAIL


# ----------------------------- 採点の条件のジャッジ(台本の LLM) -----------------------------
class ScriptedJudge:
    """Anthropic の形の偽クライアント。SceneJudgement を台本どおりに返し、受け取ったプロンプトを残す。"""

    def __init__(self, items):
        self.items, self.seen = items, []
        self.messages = self

    async def parse(self, *, output_format, messages, model, system, **kwargs):
        assert output_format is J.SceneJudgement
        self.seen.append((system[0]["text"] if isinstance(system, list) else system, messages[0]["content"]))
        return SimpleNamespace(parsed_output=J.SceneJudgement(items=self.items))


def test_every_judge_check_has_a_definition():
    assert set(J.DEFINITIONS) == set(S.JUDGE_CHECKS)
    assert set(J.TONES) == set(S.ENTRIES)


def test_judge_prompt_carries_the_scene_the_replies_and_the_facts(harness_rows):
    scene, row = BY_ID["ask-where-stored"], harness_rows["ask-where-stored"]
    checks = [c for c in scene["expect"] if not S.is_code(c)]
    system, user = J.prompt(scene, row, checks)
    assert "判定する返信" in system
    assert scene["says"][0]["text"] in user and row["replies"][0]["text"].splitlines()[0] in user
    assert "最後に使った日から180日で消えます" in user  # answers_from_facts の渡した事実
    assert J.DEFINITIONS["no_invented_facts"] in user and J.TONES["career"] in user


def test_judge_output_is_shaped_and_checked_against_the_reply(harness_rows):
    scene, row = BY_ID["ask-where-stored"], harness_rows["ask-where-stored"]
    checks = [c for c in scene["expect"] if not S.is_code(c)]
    quote = row["replies"][0]["text"].split("。")[0]
    client = ScriptedJudge([J.Item(n=0, verdict="満たさない", evidence=quote, reason="事実で答えていない"),
                            J.Item(n=1, verdict="満たした", evidence="返信に無い文", reason="作っていない")])
    got = asyncio.run(J.judge(client, "judge-model", scene, row, checks))
    assert [x["check"] for x in got] == [c["check"] for c in checks]
    assert got[0]["status"] == K.FAIL and got[0]["quoted"] is True
    assert got[1]["status"] == K.PASS and got[1]["quoted"] is False  # 根拠が返信に無い: 合否は変えず、印を付ける
    missing = J.shape(J.SceneJudgement(items=[]), row, checks)
    assert all(x["status"] == K.FAIL and "返さなかった" in x["detail"] for x in missing)


def test_runner_uses_the_judge_for_the_judge_checks_only():
    client = ScriptedJudge([J.Item(n=0, verdict="満たした", evidence="", reason="応じた")])
    (row,) = R.run(["career"], ids=["chat-greeting"], judge=J.judge_with(client, "judge-model"))
    judged = [x for x in row["results"] if x.get("judge")]
    assert [(x["check"], x["status"]) for x in judged] == [("responds_first", K.PASS)]
    assert len(client.seen) == 1  # 1 場面 1 回
    assert all(x["status"] in (K.PASS, K.FAIL) for x in row["results"] if not x.get("judge"))


# ----------------------------- 実モデルの回(9 枚目。evals/scenes/real.py。ここでは偽のモデル) -----------------------------
def fake_real(monkeypatch, driver, say, fail=()):
    """RealModel の、モデルを呼ぶところ(_parse)だけを偽物にする: 台本の LLM と同じ出力を返し、段、モデル、メッセージを残す。
    fail に入れた段は例外(通信の失敗)。読む段のモデルは small-model。"""
    from evals.scenes import real as RL

    real = RL.RealModel(SimpleNamespace(mark=lambda: 0), read_model="small-model")
    real._client = object()
    seen = []

    async def parse(client, entry, stage, system, messages):
        seen.append({"entry": entry, "stage": stage, "model": real.model_for(stage), "messages": messages})
        if stage in fail:
            raise RuntimeError("API が落ちた")
        return R.ScriptLLM(driver, say, [])({"stage": stage, "system": system}), {}
    monkeypatch.setattr(real, "_parse", parse)
    return real, seen


def test_real_mode_sends_the_runtime_messages_and_times_each_call(monkeypatch, tmp_path):
    """実モデルの回は、ランタイムと同じメッセージでモデルを呼ぶ: 読む段は今の発言だけ、言う段は場面の前の会話と今の発言。
    読む段だけを別のモデルにでき、ターンごとに呼び出しの数と時間を残す。出力が同じなら、台本の回と同じ返信になる。"""
    scene = BY_ID["chat-weather"]
    d = R.driver("career", "c1")
    real, seen = fake_real(monkeypatch, d, scene["says"][0])
    d.real = real
    rec = R.run_harness_scene(scene, d, tmp_path / "state.json")
    real.close()
    (mock,) = R.run(["career"], ids=["chat-weather"])
    rep = rec["replies"][0]
    assert rec["error"] is None and rep["text"] == mock["replies"][0]["text"]
    assert [(x["stage"], x["model"]) for x in seen] == [("extract", "small-model"), ("say", real.model)]
    assert seen[0]["messages"] == [{"role": "user", "content": scene["says"][0]["text"]}]
    said = [m["content"] for m in seen[1]["messages"]]
    assert scene["history"][-1]["text"] in "\n".join(said) and said[-1].endswith(scene["says"][0]["text"])
    assert rep["model_calls"] == 2 and [s["stage"] for s in rep["stages"]] == ["extract", "say"] and rep["model_seconds"] >= 0


def test_real_mode_turns_a_failed_call_into_a_mark_like_the_runtime(monkeypatch, tmp_path):
    """実モデルの呼び出しが落ちたら、場面のエラーにせず、ランタイムと同じく失敗の印を渡す(ハーネスがやり直し、だめなら問いの原文)。"""
    scene = BY_ID["chat-weather"]
    d = R.driver("career", "c2")
    real, seen = fake_real(monkeypatch, d, scene["says"][0], fail={"say"})
    d.real = real
    rec = R.run_harness_scene(scene, d, tmp_path / "state.json")
    real.close()
    rep = rec["replies"][0]
    assert rec["error"] is None and rep["how"] == "harness" and rep["text"]
    assert [s["ok"] for s in rep["stages"] if s["stage"] == "say"] == [False, False]  # 1 回やり直して、決まった組み立て
    assert rep["model_calls"] == len(seen)


def test_real_judge_skips_replies_written_by_the_talk_ai():
    """実モデルの回でもトークの AI は回さないので、相談を閉じたターンの返信を見る採点の条件は判定しない(ジャッジを呼ばない)。"""
    from evals.scenes import real as RL

    scene = BY_ID["chat-greeting"]
    (row,) = R.run(["career"], ids=["chat-greeting"])
    record = {"replies": row["replies"]}
    checks = [c for c in scene["expect"] if not S.is_code(c)]
    real = RL.RealModel(SimpleNamespace(mark=lambda: 0))
    real._client = ScriptedJudge([J.Item(n=0, verdict="満たした", evidence="", reason="応じた")])
    try:
        assert [x["status"] for x in real.judge("judge-model")(scene, record, checks)] == [K.PASS]
        handed = {"replies": [r | {"how": "talk", "text": ""} for r in row["replies"]]}
        got = real.judge("judge-model")(scene, handed, checks)
    finally:
        real.close()
    assert [x["status"] for x in got] == [K.SKIP] and got[0]["judge"] and len(real._client.seen) == 1


def test_real_summary_reports_the_pass_lines_per_entry():
    """点の表: 入り口と版ごとに、§4.10 の合格の線(コード 95%、採点 85%、守りと入り口をまたぐ条件は 100%)に届いたかを出す。
    移った先の not_asks は、入り口をまたぐ条件に数える。速さは、モデルを呼んだターンの時間。"""
    from evals.scenes import real as RL

    row = {"entry": "career", "version": "c2", "scene": "switch-x", "category": "switch", "error": None, "rep": 0,
           "read_model": None, "cost_usd": 0.01,
           "results": [{"check": "switches_to", "status": K.PASS}, {"check": "not_asks", "status": K.FAIL, "args": {"entry": "baito"}},
                       {"check": "crisis_reply", "status": K.PASS}, {"check": "no_falsify", "status": K.PASS, "judge": True},
                       {"check": "natural", "status": K.SKIP, "judge": True}],
           "replies": [{"model_calls": 2, "model_seconds": 3.0, "stages": [{"stage": "read", "seconds": 1.0, "ok": True},
                                                                            {"stage": "say", "seconds": 2.0, "ok": False}]}]}
    t = RL.tally([row])[("career", "c2", "-")]
    assert t["code"] == [2, 3] and t["switch"] == [1, 2] and t["guard"] == [1, 1] and t["strict"] == [1, 1]
    assert t["judge"] == [1, 1] and t["skip_judge"] == 1 and t["fallback"] == 1
    assert RL.verdict(t, "career") == ["コードの条件 66.7% < 95%", "入り口をまたぐ条件 1/2"]
    text = RL.summary([row])
    assert "career c2(読む段 -): 届かない" in text and "| career c2 | - | 1 | 2.00 | 3.0 |" in text


def test_real_cli_needs_a_ledger_and_a_limit(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--real", "--entry", "career"])
    assert "--ledger" in capsys.readouterr().err


def test_judge_can_be_given_the_entry_fact_data(harness_rows):
    """事実の条件の判定し直し(real.rejudge)は、入り口の事実のデータ(facts.json)をジャッジに渡す。場面の facts は一部なので、
    facts.json にある正しい答えを「事実に無い」にしないため。渡さなければ今までどおり。"""
    from evals.scenes import real as RL

    scene, row = BY_ID["ask-who"], harness_rows["ask-who"]
    checks = [c for c in scene["expect"] if not S.is_code(c)]
    facts = RL.known_facts("career")
    assert "返事は AI が書いています。人が一つずつ書いているのではありません" in facts
    _, plain = J.prompt(scene, row, checks)
    _, given = J.prompt(scene, row, checks, facts)
    assert "人が一つずつ" not in plain and "aica の確かめた事実" not in plain
    assert "   渡した事実: 返事は AI が書いています。人が一つずつ書いているのではありません" in given
    assert given.count("渡した事実: aica は、仕事探しを手伝う AI です") == 1  # 場面の facts と重ねない


def _offline_real_run(monkeypatch, tmp_path, score):
    """real.run を、モデルもメーターも呼ばずに回す(場面を回す関数と判定を偽物にする)。回した状態のパスを返す。"""
    import evals.meter
    from evals.scenes import real as RL

    meter = SimpleNamespace(mark=lambda: 0, summary=lambda since=0: {"cost_usd": 0.01}, calls=[])
    monkeypatch.setattr(evals.meter, "Meter", lambda: SimpleNamespace(install=lambda: meter))
    paths = []

    def run_scene(scene, d, path, others):
        paths.append(path)
        return {"scene": scene["id"], "entry": scene["entry"], "version": "c1", "replies": [], "error": None}
    monkeypatch.setattr(R, "run_harness_scene", run_scene)
    monkeypatch.setattr(R, "score", score)
    ledger = tmp_path / "ledger.jsonl"
    RL.run(["career"], None, ["chat-weather"], 2, ledger, 1.0, None, "t", out_dir=tmp_path, judge=False, log=lambda m: None)
    return paths, [json.loads(x) for x in ledger.read_text().splitlines()]


def test_real_run_uses_a_fresh_state_for_each_repeat(monkeypatch, tmp_path):
    """--repeat の回ごとに新しい dip の状態で回す(場面の本人の id は回によらず同じなので、前の回の保存が混ざらないように)。"""
    row = {"scene": "chat-weather", "entry": "career", "category": "chat", "version": "c1", "results": [], "replies": [], "error": None}
    paths, rows = _offline_real_run(monkeypatch, tmp_path, lambda s, rec, j: dict(row))
    assert len(paths) == 2 and paths[0] != paths[1]
    assert [r["rep"] for r in rows] == [0, 1] and all(r["run"] == "scenes" for r in rows)


def test_real_run_keeps_the_cost_when_the_judge_fails(monkeypatch, tmp_path):
    """判定(ジャッジ)が例外で止まっても、その場面で使った費用を台帳に残し、次の回へ進む。"""
    def fail(scene, rec, judge):
        raise RuntimeError("ジャッジが落ちた")
    paths, rows = _offline_real_run(monkeypatch, tmp_path, fail)
    assert len(paths) == 2 and len(rows) == 2
    assert all(r["cost_usd"] == 0.01 and "ジャッジが落ちた" in r["error"] for r in rows)
