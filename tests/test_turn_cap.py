"""相談のシナリオを最後まで回す(task 222)。実 LLM なし、実 API なし。

2026-10-25 の見直し(jyoka/tasks#221)で、ターンの上限 8 で相談のシナリオ 5 件が途中で切れ、相談から提案、
その後の反応までを評価が見ていなかった。また T-switch-to-fulltime の模擬ユーザーが、設定の指示文をそのまま発言にした。
ここでは、(a) シナリオの上限と台本の付け方、(b) 上限で切れたことと指示文の発言を見る判定(evals/checks.py)、
(c) 評価の会話の回し方(evals/run.py の run_scenario。エージェントと模擬ユーザーは偽物、dip の MCP は本物)を確かめる。
"""

from __future__ import annotations

import json
import re

import pytest

from dip_mcp.data import ROOT
from evals.checks import conversation_end, sim_leaks
from evals.run import DIRECTIONS, MAX_TURNS, SCENARIO_TURNS, report, to_run
from evals.scenarios import load_scenarios
from tests.test_eval_harness import CareerAgent, FakeClient, _run

SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
# 8 ターンの上限で切れた相談のシナリオ(#221 の sim-20261025-230120 で turns が 8 だったもの)
CONSULT_TO_THE_END = ("C-consult-propose", "T-career-fulltime", "T-student-first", "T-blank-parent",
                      "T-switch-to-fulltime")
# #221 の T-switch-to-fulltime の U2(模擬ユーザーが設定の指示文を発言にした)
LEAKED = "次の質問に答えるときに、考え直して「やっぱり正社員で探したいです。販売の仕事で店長を目指したい」と書きます。"


# ----------------------------- (a) シナリオの上限と台本 -----------------------------


def test_consult_scenarios_run_up_to_12_turns_and_the_others_keep_8():
    assert MAX_TURNS == 12 and SCENARIO_TURNS == 8
    for case in CONSULT_TO_THE_END:
        sc = SCENARIOS[case]
        # run_scenario の上限は min(シナリオの max_turns, --max-turns)。既定の --max-turns でも 12 まで回る
        assert min(sc.get("max_turns", SCENARIO_TURNS), MAX_TURNS) == 12, case
    # ほかのシナリオは前と同じ上限(8 か、それより小さいシナリオの値)のまま
    longer = [sc["id"] for sc in SCENARIOS.values() if sc.get("max_turns", SCENARIO_TURNS) > SCENARIO_TURNS]
    assert sorted(longer) == sorted(CONSULT_TO_THE_END)


def test_switch_line_is_scripted_not_left_to_the_persona():
    sc = SCENARIOS["T-switch-to-fulltime"]
    assert sc["script"] == ["渋谷あたりでカフェのバイトを探しています", "やっぱり正社員で探したいです。販売の仕事で店長を目指したい"]
    assert sc["script_then_persona"] is True  # 台本の後(理由・年収・求人への反応)は模擬ユーザーが続ける
    # 設定に、発言の言い方の指示(模擬ユーザーがそのまま発言にした文)を残さない
    assert "この言い方のまま" not in sc["persona"] and "やっぱり正社員" not in sc["persona"]
    assert "script のとおり" in sc["persona"]
    # 期待の切り替えの発言(on_said)は、台本の 1 つ目と 2 つ目に当たる(U1 でアルバイト・パート、U2 で正社員)
    on_said = [e["on_said"] for e in sc["expect_tracks"]]
    assert [bool(re.search(p, line)) for p, line in zip(on_said, sc["script"], strict=True)] == [True, True]
    assert not re.search(on_said[1], sc["script"][0])
    # 台本で会話を終える既存のシナリオは、台本の後を続けない
    for case in ("pii", "C-consult-only", "C-consult-switch"):
        assert not SCENARIOS[case].get("script_then_persona"), case


# ----------------------------- (b) 判定(evals/checks.py) -----------------------------


def test_sim_leaks_catch_the_persona_direction_said_as_a_message():
    assert sim_leaks([None, "渋谷あたりでカフェのバイトを探しています", LEAKED]) == [f"U2: {LEAKED}"]
    for leaked in ("理由を聞かれたら、収入が安定しないからと答える。", "求人を見せられたら「ありがとう、考えます」と言って終えます",
                   "「やっぱり正社員で」と書く(この言い方のまま)"):
        assert sim_leaks([None, leaked]) == [f"U1: {leaked}"], leaked


def test_sim_leaks_do_not_flag_natural_messages():
    natural = [None, "やっぱり正社員で探したいです。販売の仕事で店長を目指したい", "ありがとう、考えます", "特にないです",
               "わからない", "店長になりたいと思います。", "友達に言われて探し始めました", "求人を見る", "今はいい"]
    assert sim_leaks(natural) == []


def test_sim_leaks_find_nothing_in_the_saved_baselines():
    # 保存済みの基準の会話の、本人の発言すべて。指示文の発言は #221 の 1 件(T-switch-to-fulltime の U2)だけで、
    # その回(sim-20261025-230120。task 223 が直す前の基準として入れた)のほかには無い
    said, leaks = 0, {}
    for f in sorted((ROOT / "evals" / "baseline").glob("sim-*.json")):
        msgs = [m for r in json.loads(f.read_text(encoding="utf-8"))["runs"] if r["status"] == "ok"
                for m in r["detail"].get("user_msgs") or [t["text"] for t in r["detail"]["transcript"] if t["who"] == "user"]]
        said += len(msgs)
        if found := sim_leaks([None, *msgs]):
            leaks[f.name] = found
    assert said > 300
    assert list(leaks) == ["sim-20261025-230120.json"] and len(leaks["sim-20261025-230120.json"]) == 1
    assert "次の質問に答えるときに" in leaks["sim-20261025-230120.json"][0]


@pytest.mark.parametrize(("turns", "cap", "by_user", "cut"), [
    (12, 12, False, True),   # 本人が終えないまま上限に達した
    (12, 12, True, False),   # 上限のターンで本人が終えた(「ありがとう、考えます」)
    (6, 12, True, False),    # 上限より前に本人が終えた
])
def test_conversation_end_tells_whether_the_cap_cut_the_conversation(turns, cap, by_user, cut):
    assert conversation_end(turns, cap, by_user) == {"turn_cap": cap, "ended_by": "turn_cap" if cut else "user",
                                                     "cut_at_cap": cut}


# ----------------------------- (c) 評価の会話の回し方(run_scenario) -----------------------------


def _doc(r: dict) -> dict:
    return {"runs": [to_run(r, 0)], "directions": DIRECTIONS, "spent_usd": 0.0,
            "config": {"model": "fake", "sim_model": "fake", "judge": False, "max_turns": MAX_TURNS}}


def test_consult_scenario_runs_to_12_turns_and_reports_the_cut(tmp_path, monkeypatch):
    sc = SCENARIOS["T-student-first"]
    r = _run(sc, FakeClient(["初めてバイトをしたい"] * 20, end_after=False), CareerAgent(), tmp_path, monkeypatch,
             max_turns=MAX_TURNS)
    assert r["turns"] == 12 and r["turn_cap"] == 12
    assert r["cut_at_cap"] is True and r["ended_by"] == "turn_cap"
    assert to_run(r, 0)["metrics"]["cut_at_cap"] is True
    section = report(_doc(r)).split("## ターンの上限で切れた会話")[1].split("\n## ")[0]
    assert "- **T-student-first#1** T12 で上限(12)に達して切れた" in section


def test_conversation_the_user_ends_is_not_reported_as_cut(tmp_path, monkeypatch):
    sc = SCENARIOS["T-student-first"]
    r = _run(sc, FakeClient(["初めてバイトをしたい", "平日の夕方です"]), CareerAgent(), tmp_path, monkeypatch,
             max_turns=MAX_TURNS)
    assert r["turns"] == 3 and r["cut_at_cap"] is False and r["ended_by"] == "user"
    section = report(_doc(r)).split("## ターンの上限で切れた会話")[1].split("\n## ")[0]
    assert "- なし" in section


def test_other_scenarios_keep_the_8_turn_cap(tmp_path, monkeypatch):
    sc = SCENARIOS["F-follow-new"]
    r = _run(sc, FakeClient(["カフェで働きたい"] * 20, end_after=False), CareerAgent(), tmp_path, monkeypatch,
             max_turns=MAX_TURNS)
    assert r["turns"] == 8 and r["turn_cap"] == 8 and r["cut_at_cap"] is True


def test_switch_scenario_uses_the_script_then_the_persona_continues(tmp_path, monkeypatch):
    sc = SCENARIOS["T-switch-to-fulltime"]
    client = FakeClient(["アルバイトのままだと収入が安定しないからです", "年収は350万以上です", "ありがとう、考えます"])
    r = _run(sc, client, CareerAgent(), tmp_path, monkeypatch, max_turns=MAX_TURNS)
    # U1 と U2 は台本のまま、その後は模擬ユーザーの返信で、模擬ユーザーが終えたところで終わる(台本の最後では終えない)
    assert r["user_msgs"] == [None, *sc["script"], "アルバイトのままだと収入が安定しないからです", "年収は350万以上です",
                              "ありがとう、考えます"]
    assert r["turns"] == 6 and r["cut_at_cap"] is False and r["sim_leaks"] == []
    # 正社員の期待の期限は、台本の切り替えの発言(U2)に答える T3
    assert [row["due"] for row in r["track_checks"]] == [2, 3]


def test_leaked_direction_is_reported_apart_from_agent_violations(tmp_path, monkeypatch):
    sc = SCENARIOS["T-blank-parent"]
    r = _run(sc, FakeClient(["久しぶりにパートを探したい", LEAKED]), CareerAgent(), tmp_path, monkeypatch,
             max_turns=MAX_TURNS)
    assert r["sim_leaks"] == [f"U2: {LEAKED}"] and not any(LEAKED in v for v in r["violations"])
    assert to_run(r, 0)["metrics"]["sim_leaks"] == 1
    section = report(_doc(r)).split("## 模擬ユーザーが設定の指示文を発言にした")[1].split("\n## ")[0]
    assert f"- **T-blank-parent#1** U2: {LEAKED}" in section


def test_real_run_of_the_five_consult_scenarios_records_where_each_ended():
    # task 222 で 5 件を実 API で 1 回ずつ回した結果(LINE の窓口、ジャッジつき)。4 件は本人が終え、T-blank-parent は上限で切れた
    doc = json.loads((ROOT / "evals" / "baseline" / "sim-20261025-234147.json").read_text(encoding="utf-8"))
    ended = {r["case"]: (r["detail"]["turns"], r["detail"]["turn_cap"], r["detail"]["ended_by"]) for r in doc["runs"]}
    assert ended == {"C-consult-propose": (9, 12, "user"), "T-career-fulltime": (11, 12, "user"),
                     "T-student-first": (11, 12, "user"), "T-blank-parent": (12, 12, "turn_cap"),
                     "T-switch-to-fulltime": (11, 12, "user")}
    assert doc["config"]["max_turns"] == MAX_TURNS and all(r["metrics"]["sim_leaks"] == 0 for r in doc["runs"])
    # 台本の切り替えの発言(U2)に、エージェントは T3 で正社員へ切り替えた(#221 の回は指示文の発言で T4 になり track_ok が偽)
    switch = next(r for r in doc["runs"] if r["case"] == "T-switch-to-fulltime")
    assert switch["detail"]["user_msgs"][1:3] == SCENARIOS["T-switch-to-fulltime"]["script"]
    assert switch["metrics"]["track_ok"] is True
    assert "- **T-blank-parent#1** T12 で上限(12)に達して切れた" in (ROOT / "evals" / "baseline" / "sim-20261025-234147.md").read_text()
