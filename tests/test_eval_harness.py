"""評価ハーネス(evals/run.py・judge.py・budget.py)をオフラインで。LLM は台本・偽物で差し替え、dip の MCP は本物。

- 深さのジャッジ: ジャッジを差し替えて、4項目が verdict と根拠の発言(evidence・quoted)つきで結果と指標に出ること
- 費用: 台帳の累計が $4 に達したら、次のシナリオを始めずに止まること。--only とターン上限
- 履歴書を持つペルソナ: AI が履歴書の操作を出したらアップロードし、その後の1問と区分の切り替えを判定すること
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

from evals import budget
from evals.judge import DEPTH_ITEMS, DepthItem, Judgement, depth_metrics, shape
from evals.run import SimTurn, run_cases, select
from evals.scenarios import load_scenarios
from tests.test_handle import call, reply

SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
TRANSCRIPT = [
    {"who": "agent", "text": "こんにちは。何をしますか?"},
    {"who": "user", "text": "転職を考えていて、正社員の仕事を相談したい"},
    {"who": "agent", "text": "正社員のお仕事で考えますね。なぜ今、転職を考えているのですか?"},
    {"who": "user", "text": "新しいプロダクトを最初から企画したいからです"},
]


def judgement(evidence="なぜ今、転職を考えているのですか?") -> Judgement:
    return Judgement(asked_purpose=DepthItem(verdict="満たした", evidence=evidence),
                     dug_deeper=DepthItem(verdict="満たさない", evidence="新しいプロダクトを最初から企画したいからです"),
                     options_personal=DepthItem(verdict="該当なし", evidence=""),
                     reasons_linked=DepthItem(verdict="該当なし", evidence=""),
                     tone=5, design_fit=4, comment="目的は聞いたが掘り下げていない。")


class FakeClient:
    """Anthropic の形の偽クライアント。模擬ユーザー(SimTurn)とジャッジ(Judgement)を、出力の型で返し分ける。"""

    def __init__(self, sim_texts=(), end_after=True, judge=None):
        self.sim_texts = list(sim_texts)
        self.end_after = end_after
        self.judge = judge or judgement()
        self.judge_calls = []
        self.messages = self

    async def parse(self, *, output_format, messages, model, **kwargs):
        if output_format is Judgement:
            self.judge_calls.append({"model": model, "log": messages[0]["content"]})
            return SimpleNamespace(parsed_output=self.judge)
        text = self.sim_texts.pop(0) if self.sim_texts else "ありがとう、考えます"
        return SimpleNamespace(parsed_output=SimTurn(text=text, end=self.end_after and not self.sim_texts))


# ----------------------------- 深さのジャッジ -----------------------------


def test_judge_items_come_out_with_verdict_and_evidence():
    r = shape(judgement(), TRANSCRIPT)
    assert set(r["depth"]) == set(DEPTH_ITEMS)
    assert r["depth"]["asked_purpose"] == {"verdict": "満たした", "evidence": "なぜ今、転職を考えているのですか?",
                                           "quoted": True}
    assert r["depth"]["dug_deeper"]["verdict"] == "満たさない" and r["depth"]["dug_deeper"]["quoted"]
    assert depth_metrics(r) == {"depth_purpose": True, "depth_dig": False, "depth_options": None, "depth_reasons": None}
    assert depth_metrics(None) == dict.fromkeys(DEPTH_ITEMS.values())


def test_evidence_that_is_not_in_the_conversation_is_marked():
    # ジャッジが発言を作ったら quoted=False(人が照合するときの手がかり)
    r = shape(judgement(evidence="どんな働き方を希望していますか?"), TRANSCRIPT)
    assert r["depth"]["asked_purpose"]["quoted"] is False


# ----------------------------- 履歴書を持つペルソナの会話を最後まで -----------------------------


class CareerAgent(BaseChatModel):
    """挨拶で履歴書の操作を出し、アップロードの後は1問、「転職」と言われたら正社員に切り替える。"""

    @property
    def _llm_type(self) -> str:
        return "career"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last_human = max(i for i, m in enumerate(messages) if m.type == "human")
        text, after = messages[last_human].content, messages[last_human + 1:]
        uid = "sim-new"
        if text.startswith("[イベント: 初回訪問"):
            msg = reply(["こんにちは。お仕事探しをお手伝いします。履歴書があれば読み取ります。何から始めますか?"],
                        quick_replies=["求人を探す", "応募履歴を確認", "文字で話す", "今はいい"], actions=["resume_upload"])
        elif "アップロード" in text:
            msg = reply(["法人営業とPMのご経験ですね。それを活かせるお仕事を一緒に考えますか?"],
                        quick_replies=["考えたい", "今はいい"])
        elif "転職" in text and not after:
            msg = call("set_search_track", {"line_user_id": uid, "track": "正社員", "evidence": text})
        else:
            msg = reply(["正社員のお仕事で考えますね。なぜ今、転職を考えているのですか?"], quick_replies=["今はいい"])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _run(sc, client, model, tmp_path, monkeypatch, use_judge=False, max_turns=8):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    from evals.meter import Meter
    from evals.run import run_scenario
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            return await run_scenario(rt, client, Meter(), sc, use_judge, max_turns)

    return asyncio.run(go())


def test_persona_with_a_resume_uploads_when_offered_and_is_judged(tmp_path, monkeypatch):
    from dip_mcp import logic
    from dip_mcp.data import State
    from evals.run import JUDGE_MODEL, to_run
    from line_runtime import config

    sc = SCENARIOS["T-career-fulltime"]
    client = FakeClient(["転職を考えていて、正社員の仕事を相談したい"])
    r = _run(sc, client, CareerAgent(), tmp_path, monkeypatch, use_judge=True)
    # T1 で履歴書の操作が出たので、本人の次の1ターンはアップロード(dip に保存され、会話に入る)
    assert r["upload_turn"] == 2 and r["action_offers"] == [{"turn": 1, "actions": ["resume_upload"]}]
    assert r["transcript"][1] == {"who": "user", "text": "[職務経歴書(PDF)をアップロード]"}
    assert logic.resume_upload(State(tmp_path / "state.json"), "sim-new")["resume"]["work_history"]
    # アップロードの後は1問、区分は本人が「転職」と言ったターンで正社員に
    assert r["tracks"] == ["未定", "未定", "正社員"]
    assert r["track_ok"] is True and r["track_checks"][0]["status"] == "ok"
    assert not any("アップロード" in v or "切り替え" in v for v in r["violations"]), r["violations"]
    # ジャッジは安いモデルで、履歴書と会話ログを受け取り、深さの4項目を根拠つきで返す
    assert client.judge_calls[0]["model"] == JUDGE_MODEL[config.PROVIDER]
    assert "履歴書の読み取り結果" in client.judge_calls[0]["log"] and "みほんソフト" in client.judge_calls[0]["log"]
    assert set(r["judge"]["depth"]) == set(DEPTH_ITEMS)
    assert r["judge"]["depth"]["asked_purpose"]["quoted"]  # 引用は実際の会話ログにある
    m = to_run(r, 0)["metrics"]
    assert m["track_ok"] is True and m["depth_purpose"] is True and m["depth_dig"] is False
    # 結果にはツールの args と result つきのターンが残り、JSON を通しても今の評価器で再判定できる(実 API なし)
    from evals.run import recheck

    detail = json.loads(json.dumps(to_run(r, 0)["detail"], ensure_ascii=False, default=str))
    assert any(t["name"] == "set_search_track" and t["args"]["track"] == "正社員" and "result" in t
               for turn in detail["turn_log"] for t in turn["tools"])
    again = recheck(detail)
    assert again["track_checks"] == r["track_checks"] and again["violations"] == r["violations"]
    with pytest.raises(ValueError):  # turn_log を保存する前の結果(2026-09-29 の基準)は再判定できない
        recheck({k: v for k, v in detail.items() if k != "turn_log"})


def test_persona_uploads_by_the_fallback_turn_when_never_offered(tmp_path, monkeypatch):
    class NoOffer(CareerAgent):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            out = super()._generate(messages, stop, run_manager, **kwargs)
            msg = out.generations[0].message
            if not msg.tool_calls:
                body = json.loads(msg.content) | {"actions": []}
                msg = reply(body["bubbles"], quick_replies=body["quick_replies"])
            return ChatResult(generations=[ChatGeneration(message=msg)])

    sc = SCENARIOS["T-career-fulltime"]
    r = _run(sc, FakeClient(["転職を考えていて、正社員の仕事を相談したい", "営業を4年やってきました"], end_after=False),
             NoOffer(), tmp_path, monkeypatch, max_turns=5)
    assert r["action_offers"] == [] and r["upload_turn"] == sc["upload_by_turn"] + 1


def test_turn_cap_limits_the_conversation(tmp_path, monkeypatch):
    # 模擬ユーザーが終えなくても、エージェントのターンは上限まで
    sc = SCENARIOS["T-student-first"]
    r = _run(sc, FakeClient(["初めてバイトをしたい"] * 20, end_after=False), CareerAgent(), tmp_path, monkeypatch,
             max_turns=3)
    assert r["turns"] == 3


# ----------------------------- 費用: --only と $4 での停止 -----------------------------


def test_only_takes_comma_separated_ids_and_rejects_unknown_ones():
    all_sc = load_scenarios()
    assert [s["id"] for s in select(all_sc, "T-side-job, B-exit")] == ["B-exit", "T-side-job"]
    assert select(all_sc, None) == all_sc
    with pytest.raises(SystemExit):
        select(all_sc, "T-side-job,nope")


def _cases(n):
    return [(SCENARIOS[i], 0) for i in list(SCENARIOS)[:n]]


class Paid:
    """このプロセスが払った額の偽物(本物は Meter の合計)。run_one が pay で増やす。"""

    def __init__(self):
        self.total = 0.0

    def pay(self, usd):
        self.total += usd

    def __call__(self):
        return self.total


def test_run_stops_before_the_next_scenario_once_the_ledger_reaches_4_dollars(tmp_path):
    ledger = tmp_path / "budget.jsonl"
    budget.record(ledger, "earlier", 3.95)
    ran, logs = [], []

    paid = Paid()

    async def run_one(sc, rep):
        ran.append(sc["id"])
        paid.pay(0.06)  # 1回目で累計 4.01

    stopped = asyncio.run(run_cases(_cases(3), run_one, paid, ledger, "this-run", log=logs.append))
    assert stopped is True and len(ran) == 1
    assert budget.spent(ledger) == pytest.approx(4.01)
    assert any("$4.00 に達したので止めます" in x for x in logs)
    assert any("累計 $4.0100" in x for x in logs)  # 実行ごとに累計を出す
    rows = budget.rows(ledger)
    assert rows[-1]["run"] == "this-run" and rows[-1]["case"] == ran[0]


def test_nothing_runs_when_the_ledger_is_already_at_the_limit(tmp_path):
    ledger = tmp_path / "budget.jsonl"
    budget.record(ledger, "earlier", 4.0)
    ran = []

    async def run_one(sc, rep):
        ran.append(sc["id"])

    assert asyncio.run(run_cases(_cases(2), run_one, Paid(), ledger, "x", log=lambda m: None)) is True
    assert ran == []
    with pytest.raises(budget.BudgetExceeded):
        budget.check(ledger)


def test_runs_below_the_limit_record_every_case(tmp_path):
    ledger = tmp_path / "budget.jsonl"

    paid, saved = Paid(), []

    async def run_one(sc, rep):
        paid.pay(0.02)

    after = lambda: saved.append(budget.spent(ledger))
    assert asyncio.run(run_cases(_cases(3), run_one, paid, ledger, "x", log=lambda m: None, after=after)) is False
    assert len(budget.rows(ledger)) == 3 and budget.spent(ledger) == pytest.approx(0.06)
    # 結果の保存は台帳に載せたあと(保存した累計がその回の分を含む)
    assert saved == pytest.approx([0.02, 0.04, 0.06])


def test_interrupted_run_still_records_what_it_paid(tmp_path):
    # Ctrl-C(KeyboardInterrupt)で止めても、その回に払った分は台帳に載り、例外はそのまま上がる
    ledger = tmp_path / "budget.jsonl"
    paid, ran = Paid(), []

    async def run_one(sc, rep):
        ran.append(sc["id"])
        paid.pay(0.03)
        if len(ran) == 2:
            paid.pay(0.01)  # 2回目は途中まで払ってから止められる
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run_cases(_cases(3), run_one, paid, ledger, "x", log=lambda m: None))
    rows = budget.rows(ledger)
    assert len(ran) == 2 and len(rows) == 2
    assert rows[0]["cost_usd"] == pytest.approx(0.03) and "interrupted" not in rows[0]
    assert rows[1]["cost_usd"] == pytest.approx(0.04) and rows[1]["interrupted"] is True
    assert budget.spent(ledger) == pytest.approx(0.07)


def test_main_marks_the_result_as_interrupted(tmp_path, monkeypatch):
    # main でも、止めたときに結果の json に stopped を書いてから上げ直す(実 API もランタイムも使わない)
    from evals import run

    class FakeRuntime:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def interrupted_scenario(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(run, "HERE", tmp_path)
    monkeypatch.setattr(run.llm, "client", lambda: None)
    monkeypatch.setattr(run, "run_scenario", interrupted_scenario)
    monkeypatch.setattr("line_runtime.runtime.LineRuntime", FakeRuntime)
    monkeypatch.setattr("evals.meter.Meter.install", lambda self: self)
    ledger = tmp_path / "budget.jsonl"
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run.main("B-exit", False, 1, 8, ledger))
    doc = json.loads(next((tmp_path / "results").glob("sim-*.json")).read_text())
    assert doc["stopped"] == "interrupted" and "実行を途中で止めた" in run.report(doc)
    assert budget.rows(ledger)[0]["interrupted"] is True


def test_main_refuses_to_start_when_over_the_limit(tmp_path, monkeypatch):
    # ランタイムも API も起動する前に止まる
    from evals import run

    ledger = tmp_path / "budget.jsonl"
    budget.record(ledger, "earlier", 4.2)
    monkeypatch.setattr(run.llm, "client", lambda: pytest.fail("API のクライアントを作ってはいけない"))
    with pytest.raises(budget.BudgetExceeded):
        asyncio.run(run.main(None, False, 1, 8, ledger))
