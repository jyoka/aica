"""評価ハーネス(evals/run.py・judge.py・budget.py)をオフラインで。LLM は台本・偽物で差し替え、dip の MCP は本物。

- 深さのジャッジ: ジャッジを差し替えて、4項目が verdict と根拠の発言(evidence・quoted)つきで結果と指標に出ること
- 費用: 台帳の累計が $4 に達したら、次のシナリオを始めずに止まること。--only とターン上限
- 履歴書を持つペルソナ: AI が履歴書の操作を出したらアップロードし、その後の1問と区分の切り替えを判定すること
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

from evals import budget
from evals.judge import DEPTH_ITEMS, SYSTEM, DepthItem, Judgement, depth_metrics, shape
from evals.run import JUDGE_MODEL, SimTurn, run_cases, select
from evals.scenarios import load_scenarios
from line_runtime import config
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
                                           "quoted": True, "purpose_evidence": True}
    assert r["depth"]["dug_deeper"]["verdict"] == "満たさない" and r["depth"]["dug_deeper"]["quoted"]
    assert depth_metrics(r) == {"depth_purpose": True, "depth_dig": False, "depth_options": None, "depth_reasons": None}
    assert depth_metrics(None) == dict.fromkeys(DEPTH_ITEMS.values())


def test_evidence_that_is_not_in_the_conversation_is_marked():
    # ジャッジが発言を作ったら quoted=False(人が照合するときの手がかり)
    r = shape(judgement(evidence="どんな働き方を希望していますか?"), TRANSCRIPT)
    assert r["depth"]["asked_purpose"]["quoted"] is False


def _rule(name: str) -> str:
    """ジャッジの指示のうち、その項目の1行。"""
    return next(x for x in SYSTEM.splitlines() if x.startswith(f"- {name}"))


def test_judge_prompt_closes_on_thanks_and_asks_only_on_hesitation():
    # Prompt(baitoru_assistant.md の対話ルール)と同じ: お礼つきの「考えます」は短く締める(迷いの問いは減点)。
    # 迷いを言っている「考えます」のあとに迷いの点を1問聞くのは正しい。待つのは「今はいい」の後(task 223 の D1)
    rule = _rule("design_fit")
    assert "お礼や終わりの挨拶と一緒の「考えます」(「ありがとう、考えます」)は会話の終わり" in rule
    assert "問いを返さず短く締め、次に開いたときの入口を1つ添えるのが正しく" in rule
    assert "迷いの点を聞き返すのは減点する" in rule
    assert "迷いを言っている「考えます」(「うーん、考えます」「迷うので考えます」)" in rule and "断りではない" in rule
    assert "どこで迷っているかを1問だけ聞くのは正しい" in rule and "減点しない" in rule
    assert "待つ(wait)のは「今はいい」が選ばれた後" in rule


def test_judge_prompt_counts_a_purpose_the_user_told_and_the_assistant_took_up():
    # 本人が自分から目的を話し、アシスタントが受け止めたら満たす。根拠は受け止めた発言(無関係な前の発言ではない)
    rule = _rule("asked_purpose")
    assert "本人が自分から目的を話し、アシスタントがそれを受け止めて確かめた場合" in rule and "も満たす" in rule
    assert "evidence には、目的を受け止めたアシスタントの発言を引用する" in rule
    assert "目的と関係のない前の発言を引かない" in rule


def test_judge_prompt_leaves_the_track_question_and_relaxing_out_of_options_personal():
    # 区分の問い(PRD のストーリー3)と0件の後の条件の緩め方は、本人由来の選択肢かどうか(ストーリー8)の対象ではない。
    # C-consult-only・C-consult-switch で、区分の問いの選択肢を「本人由来でない」として満たさないにした(task 130)
    rule = _rule("options_personal")
    assert "雇用区分を聞く問いの選択肢(正社員 / アルバイト・パート / まだ決めていない)" in rule
    assert "求人が0件だった後の条件の緩め方の選択肢" in rule and "この項目の対象にしない" in rule


# ----------------------------- ジャッジだけで判定し直す(--rejudge) -----------------------------

BASELINE = Path(__file__).resolve().parent.parent / "evals" / "baseline" / "sim-20260929-100418.json"


def _rejudge(src, tmp_path, monkeypatch, client, ledger):
    from evals import run

    monkeypatch.setattr(run, "HERE", tmp_path)
    monkeypatch.setattr(run.llm, "client", lambda: client)
    monkeypatch.setattr("evals.meter.Meter.install", lambda self: self)
    # エージェントと模擬ユーザーは呼ばない
    monkeypatch.setattr(run, "run_scenario", lambda *a, **k: pytest.fail("エージェントを動かしてはいけない"))
    monkeypatch.setattr(run, "sim_reply", lambda *a, **k: pytest.fail("模擬ユーザーを呼んではいけない"))
    code = asyncio.run(run.rejudge(src, ledger))
    out = next((tmp_path / "results").glob("*-rejudge-*.json"))
    return code, json.loads(out.read_text()), out.with_suffix(".md").read_text()


def test_rejudge_replaces_only_the_judge_on_the_saved_baseline(tmp_path, monkeypatch):
    # 保存済みの基準(26 回)の会話を、ジャッジだけで判定し直す。会話・機械判定はそのまま、元の判定は judge_before に残る
    src = tmp_path / BASELINE.name
    src.write_text(BASELINE.read_text())
    before = json.loads(src.read_text())
    # 根拠は T-career-fulltime#1 の会話にある、本人の目的(ゼロから企画する経験を積みたい)を受けた発言
    client, ledger = FakeClient(judge=judgement(evidence="新しいプロダクトをゼロから企画する経験を積みたいのですね。")), tmp_path / "budget.jsonl"
    code, doc, md = _rejudge(src, tmp_path, monkeypatch, client, ledger)
    assert code == 0 and doc["stopped"] is None
    assert json.loads(src.read_text()) == before  # 元の結果は書き換えない
    # 1つの会話を2回判定してまとめる(ジャッジの揺れを抑える)
    assert len(client.judge_calls) == 2 * len(before["runs"]) == 52 and doc["config"]["judge_samples"] == 2
    assert {c["model"] for c in client.judge_calls} == {JUDGE_MODEL[config.PROVIDER]}
    assert doc["rejudge"] == {"from": BASELINE.name, "judge_model": JUDGE_MODEL[config.PROVIDER]}
    # 履歴書のあるペルソナにはジャッジへ履歴書を渡す(run_scenario と同じ)
    career = next(i for i, r in enumerate(before["runs"]) if r["case"] == "T-career-fulltime")
    assert "履歴書の読み取り結果" in client.judge_calls[2 * career]["log"]
    for old, new in zip(before["runs"], doc["runs"], strict=True):
        assert new["detail"]["transcript"] == old["detail"]["transcript"]
        assert new["detail"]["violations"] == old["detail"]["violations"]
        assert new["detail"]["judge_before"] == old["detail"]["judge"]
        assert new["detail"]["judge"]["comment"] == "目的は聞いたが掘り下げていない。"
        assert new["metrics"]["design_fit"] == 4
        # 深さは相談・ペルソナのシナリオだけで数える(B-exit・pii などは None)
        assert new["metrics"]["depth_dig"] is (False if SCENARIOS[new["case"]].get("measure_depth") else None)
        assert new["metrics"]["violations"] == old["metrics"]["violations"]
    # 台帳には1回ごとに1行(元の case と rep で)
    rows = budget.rows(ledger)
    assert [(r["case"], r["rep"]) for r in rows] == [(r["case"], r["rep"]) for r in before["runs"]]
    assert all(r["run"].startswith("sim-20260929-100418-rejudge-") for r in rows)
    # md に元の判定との差が出る(T-career-fulltime#1 の asked_purpose は 満たさない → 満たした)
    assert "## 元の判定(sim-20260929-100418.json)との差" in md
    section = md.split("との差(相談の深さと design_fit)")[1].split("## 会話ログ")[0]
    line = next(x for x in section.splitlines() if x.startswith("- **T-career-fulltime#1**"))
    assert "目的を聞いたか: 満たさない → 満たした" in line


def test_rejudge_diff_lists_only_the_runs_that_changed():
    from evals.run import rejudge_diff, rejudged_run

    old = shape(judgement(), TRANSCRIPT)
    same = {"case": "C-consult-only", "rep": 0, "status": "ok", "metrics": {}, "detail": {"judge": old}}
    changed = {"case": "T-career-fulltime", "rep": 1, "status": "ok", "metrics": {}, "detail": {"judge": old}}
    new = old | {"design_fit": 5, "depth": old["depth"] | {"dug_deeper": {"verdict": "満たした", "evidence": "",
                                                                          "quoted": False}}}
    doc = {"runs": [rejudged_run(same, old), rejudged_run(changed, new), {"case": "C", "rep": 0, "status": "error"}]}
    assert rejudge_diff(doc) == [
        "- **T-career-fulltime#2** 目的かやりたいことを掘り下げたか: 満たさない → 満たした、design_fit: 4 → 5"]
    assert rejudged_run(changed, new)["metrics"] == {"depth_purpose": True, "depth_dig": True, "depth_options": None,
                                                    "depth_reasons": None, "tone": 5, "design_fit": 5}
    assert rejudge_diff({"runs": [rejudged_run(same, old)]}) == ["- なし"]


def test_rejudge_refuses_to_start_when_over_the_limit(tmp_path, monkeypatch):
    from evals import run

    ledger = tmp_path / "budget.jsonl"
    budget.record(ledger, "earlier", 4.0)
    monkeypatch.setattr(run.llm, "client", lambda: pytest.fail("API のクライアントを作ってはいけない"))
    with pytest.raises(budget.BudgetExceeded):
        asyncio.run(run.rejudge(BASELINE, ledger))


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
        if text.startswith("[イベント: 初回訪問"):  # 入口V: 3 つの入口を全部出す(ペルソナは選ばずに書き始める)
            msg = reply(["こんにちは。お仕事探しをお手伝いします。音声、履歴書、ゲームからも始められます。何から始めますか?"],
                        quick_replies=["求人を探す", "まず相談する", "今はいい"], actions=["voice_consult", "resume_upload"],
                        buttons=[{"label": "ゲームで書類を作る", "url": "/liff/arcade"}])
        elif "アップロード" in text:
            msg = reply(["法人営業とPMのご経験ですね。それを活かせるお仕事を一緒に考えますか?"],
                        quick_replies=["考えたい", "今はいい"])
        elif "転職" in text and not after:
            msg = call("set_search_track", {"line_user_id": uid, "track": "正社員", "evidence": text})
        elif "転職" in text and not any(m.type == "tool" and m.name == "get_counseling_playbook" for m in after):
            msg = call("get_counseling_playbook", {"track": "正社員", "channel": "chat"}, "pb")
        elif "転職" in text:  # 経歴の要る場面で、履歴書の操作を出す
            msg = reply(["正社員のお仕事で考えますね。職務経歴書があれば、読み込むと早いです。", "なぜ今、転職を考えているのですか?"],
                        quick_replies=["新しい企画をしたい", "今はいい"], actions=["resume_upload"])
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
    client = FakeClient(["転職を考えていて、正社員の仕事を相談したい", "新しいプロダクトを企画したいからです"])
    r = _run(sc, client, CareerAgent(), tmp_path, monkeypatch, use_judge=True)
    # T1 の挨拶の入口ではアップロードせず(ペルソナは選択肢を選ばずに書く)、T2 で経歴の場面の履歴書の操作が出たので、
    # 本人の次の1ターンはアップロード(dip に保存され、会話に入る)
    assert r["upload_turn"] == 3 and r["action_offers"] == [{"turn": 1, "actions": ["voice_consult", "resume_upload"]},
                                                            {"turn": 2, "actions": ["resume_upload"]}]
    assert r["greeting_offers"] == ["voice_consult", "resume_upload", "arcade"]
    assert r["transcript"][3] == {"who": "user", "text": "[職務経歴書(PDF)をアップロード]"}
    assert logic.resume_upload(State(tmp_path / "state.json"), "sim-new")["resume"]["work_history"]
    # アップロードの後は1問、区分は本人が「転職」と言ったターンで正社員に
    assert r["tracks"] == ["未定", "正社員", "正社員", "正社員"]
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


def test_a_card_with_its_own_budget_uses_its_own_ledger_and_limit(tmp_path):
    """PRD とは別の予算のカード(--ledger と --limit-usd)は、その台帳の累計がその額に達したら止まる。"""
    ledger = tmp_path / "budget-card.jsonl"
    ran, logs, paid = [], [], Paid()

    async def run_one(sc, rep):
        ran.append(sc["id"])
        paid.pay(0.6)

    assert asyncio.run(run_cases(_cases(3), run_one, paid, ledger, "card", log=logs.append, limit=1.0)) is True
    assert len(ran) == 2 and budget.spent(ledger) == pytest.approx(1.2)  # 始める前に見るので、2 回目の後に止まる
    assert any("budget-card.jsonl の累計 $1.2000 が $1.00 に達したので止めます" in x for x in logs)
    assert "止める額 $1.00(残り $-0.2000)" in budget.line(ledger, 1.0) and "予算" not in budget.line(ledger, 1.0)


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


# ----------------------------- F: 目的の根拠の確かめ・2回判定・深さを数えるシナリオ -----------------------------


def _after(said: str, reply: str) -> list[dict]:
    return [{"who": "agent", "text": "今日は何から始めますか? [選択肢] 求人を探す / まず相談する / 今はいい"},
            {"who": "user", "text": said}, {"who": "agent", "text": reply}]


@pytest.mark.parametrize("said, reply, evidence", [
    # R2 の T-blank-parent: ジャッジは2回とも満たした(「家計」を共有)
    ("家計のため", "家計のためですね。収入のほかに、パートで得たいことはありますか？",
     "家計のためですね。収入のほかに、パートで得たいことはありますか？"),
    # 基準の T-career-fulltime#0(「プロダクト」「企画」などを共有)
    ("企画に関わりたいです。新しいプロダクトをゼロから企画する経験を積みたいです。",
     "新しいプロダクトをゼロから企画する経験を積みたいのですね。転職先を考えるうえで、気になっていることはありますか？",
     "新しいプロダクトをゼロから企画する経験を積みたいのですね。"),
    # R1 の T-blank-parent(ひらがな混じりの語幹「探し」を共有)
    ("子どもが小学校に上がったので、久しぶりにパートを探したい", "お子さんの入学を機に探し始めたのですね。働ける時間帯は?",
     "お子さんの入学を機に探し始めたのですね。"),
    # ひらがな混じりの語幹「お金」「貯め」を共有
    ("お金を貯めたいです", "お金を貯めたい、ということですね。働ける曜日はありますか?", "お金を貯めたい、ということですね。"),
])
def test_purpose_evidence_takes_a_reply_that_picks_up_what_was_just_said(said, reply, evidence):
    """負例(正しい根拠を違反にしない): 本人の発言の直後のアシスタントの発言で、その発言と内容語を共有する。"""
    from evals.judge import purpose_evidence

    assert purpose_evidence(evidence, _after(said, reply)) is True


def test_purpose_evidence_does_not_take_the_track_switch_line():
    """区分の切り替えの一言(「正社員のお仕事で考えますね」)は、本人の発言の直後でも根拠にならない。その語(区分・仕事・考え)は
    どの発言のあとにも言う決まり文句なので、内容語に数えない(T-career-fulltime#2 の誤り)。"""
    from evals.judge import purpose_evidence

    for said in ("転職を考えていて、正社員の仕事を相談したい", "正社員の仕事を考えています", "アルバイトの仕事を探したい",
                 "土日は家族の用事があるので", "家計のため"):
        log = _after(said, "わかりました。正社員のお仕事で考えますね。いくつか伺いながら一緒に整理します。")
        assert purpose_evidence("正社員のお仕事で考えますね。", log) is False, said
    assert purpose_evidence("正社員のお仕事で考えますね。", TRANSCRIPT) is False
    assert purpose_evidence("", TRANSCRIPT) is False


@pytest.mark.parametrize("question", (
    "中野ですね。土日は働けない理由はありますか?", "土日は働けない理由はありますか?", "夜は働けない理由は?",
    "時給で探す理由はありますか?", "駅から近いところで探している理由は?", "夜勤を始めたい理由は?",
    "土日はパートで働きたい理由は?", "夜はバイトで働きたい理由は?", "週末もパートで働く理由は?",
    "その理由も含めて、探しやすい時間帯は?", "今探しているエリアを選んだ理由は?", "残業の少ない仕事を探す理由は?",
    "転職先で年収を重視する理由は?", "平日だけ働きたい理由は?", "週3でバイトしたい理由は?"))
def test_condition_reason_question_does_not_let_a_track_switch_line_through(question):
    """条件の理由の問いに本人が答えた後の、区分の切り替えの一言は目的の根拠にならない(Answer 2 の3行の会話と、
    自動レビューで目的の問いに数えられた言い方)。前の問いが何であっても、根拠の一言が本人の話を受けていなければ数えない。"""
    from evals.judge import purpose_evidence

    log = [{"who": "agent", "text": question}, {"who": "user", "text": "土日は家族の用事があるので"},
           {"who": "agent", "text": "わかりました。正社員のお仕事で考えますね。時間帯は?"}]
    assert purpose_evidence("正社員のお仕事で考えますね。", log) is False


@pytest.mark.parametrize("question", ("今回、パートを始めようと思った理由は何ですか?", "転職を考えたのはなぜですか?",
                                      "働き始めたいと思ったのは、どんな変化からですか？", "今回お仕事を探す目的を教えてください。"))
def test_an_answered_purpose_question_does_not_vouch_for_an_unrelated_next_line(question):
    """前に目的を聞いて本人が答えていても、根拠に引いた一言が本人の答えを受けていなければ数えない(Answer 3 で外した経路)。"""
    from evals.judge import purpose_evidence

    log = [{"who": "agent", "text": question}, {"who": "user", "text": "家計のため"},
           {"who": "agent", "text": "わかりました。働ける曜日はありますか?"}]
    assert purpose_evidence("わかりました。働ける曜日はありますか?", log) is False


def test_purpose_evidence_rejects_unrelated_earlier_quotes_and_lines_not_right_after_the_user():
    from evals.judge import purpose_evidence

    log = [{"who": "agent", "text": "こんにちは。何をしますか?"},
           {"who": "user", "text": "転職を考えていて、正社員の仕事を相談したい"},
           {"who": "agent", "text": "これまでどんなお仕事をされましたか?"},
           {"who": "user", "text": "新しいプロダクトを最初から企画する経験を積みたいです"},
           {"who": "agent", "text": "新しいプロダクトを最初から企画したいのですね。そう感じたのはどんな場面でしたか?"},
           {"who": "agent", "text": "新しいプロダクトの求人は少なめです。"}]
    # 負例: 目的を受けた発言は根拠になる
    assert purpose_evidence("新しいプロダクトを最初から企画したいのですね。", log) is True
    # 目的と関係のない前の発言の引用(直前の本人の発言と内容語を共有しない)
    assert purpose_evidence("これまでどんなお仕事をされましたか?", log) is False
    # 本人の発言の直後でない発言(アシスタントの発言が続いた2つ目)は、内容語を共有しても数えない
    assert purpose_evidence("新しいプロダクトの求人は少なめです。", log) is False
    # 会話の最初のアシスタントの発言(前に本人の発言が無い)
    assert purpose_evidence("こんにちは。何をしますか?", log) is False
    # 本人の発言の引用はアシスタントの発言ではない
    assert purpose_evidence("新しいプロダクトを最初から企画する経験を積みたいです", log) is False
    # 会話ログに無い発言
    assert purpose_evidence("収入を増やしたいのですね。", log) is False


def test_purpose_met_on_an_unrelated_quote_counts_as_not_met():
    r = shape(judgement(evidence="正社員のお仕事で考えますね。"), TRANSCRIPT)
    assert r["depth"]["asked_purpose"]["verdict"] == "満たした"  # ジャッジの判定は残す(人が照合する材料)
    assert r["depth"]["asked_purpose"]["purpose_evidence"] is False
    assert depth_metrics(r)["depth_purpose"] is False  # 指標では満たさない


def test_two_judgements_are_merged_strictly_and_scores_are_averaged():
    from evals.judge import merge

    a = shape(judgement(), TRANSCRIPT)
    b = shape(judgement(), TRANSCRIPT)
    b["depth"]["asked_purpose"] = {"verdict": "満たさない", "evidence": "", "quoted": False, "purpose_evidence": False}
    b["depth"]["options_personal"] = {"verdict": "満たした", "evidence": "x", "quoted": False}
    b["tone"], b["design_fit"] = 4, 3
    m = merge([a, b])
    assert m["depth"]["asked_purpose"]["verdict"] == "満たさない"  # 1回でも満たさないなら満たさない
    assert m["depth"]["dug_deeper"]["verdict"] == "満たさない"  # 両方同じ
    assert m["depth"]["options_personal"]["verdict"] == "満たした"  # 該当なしは数えず、残りが満たした
    assert m["depth"]["reasons_linked"]["verdict"] == "該当なし"
    assert (m["tone"], m["design_fit"]) == (4.5, 3.5)
    assert m["split"] == ["asked_purpose", "options_personal"] and len(m["samples"]) == 2
    assert merge([a]) == a


def test_depth_is_counted_only_on_consult_and_persona_scenarios():
    from evals.run import depth_of

    j = shape(judgement(), TRANSCRIPT)
    assert depth_of("T-career-fulltime", j)["depth_purpose"] is True
    assert depth_of("C-consult-propose", j)["depth_dig"] is False
    for case in ("B-exit", "pii", "F-follow-new", "R-revisit", "Q-job-question"):
        assert set(depth_of(case, j).values()) == {None}, case
    measured = {sc["id"] for sc in SCENARIOS.values() if sc.get("measure_depth")}
    # T-side-job と T-blank-parent は相談に入れないので測らない(task 225)
    assert measured == {"C-consult-propose", "C-consult-only", "C-consult-switch", "T-career-fulltime",
                        "T-student-first", "T-switch-to-fulltime"}


def test_keyword_trace_is_split_per_conversation(tmp_path, monkeypatch):
    """dip が残した keywords の検索の記録(DIP_KEYWORDS_TRACE)を、会話ごとに切り分けて結果に付ける。"""
    from evals import run

    monkeypatch.delenv("DIP_KEYWORDS_TRACE", raising=False)
    assert run._trace_size() == 0 and run._trace_since(0) == []
    trace = tmp_path / "keywords.jsonl"
    monkeypatch.setenv("DIP_KEYWORDS_TRACE", str(trace))
    assert run._trace_size() == 0  # まだ1回も検索していない
    trace.write_text('{"keywords": "前の会話", "peak": 0.2}\n', encoding="utf-8")
    at = run._trace_size()
    with open(trace, "a", encoding="utf-8") as f:
        f.write('{"keywords": "コーヒー", "peak": 0.15}\n')
    assert run._trace_since(at) == [{"keywords": "コーヒー", "peak": 0.15}]
