"""電話の扱いの計測(evals/probe.py。task 109)の読み出しのオフラインテスト。実 API なし。

音声は LiveKit の AgentSession(文字のターン)に台本の LLM と本物の dip MCP をつないで、計測の配線を確かめる。
読み出し(observation)は、正しい振る舞いを悪い方に数えない負例も添える。
"""

from __future__ import annotations

import asyncio

import pytest

from evals import probe
from tests.test_talk_page import app  # noqa: F401  フィクスチャ
from tests.test_voice_track import _scripted_llm

CONFIRM = "お客様と話すレジや接客は大丈夫ですか?"


def _turn(bubbles, *searches, cards=()):
    return {"bubbles": bubbles, "quick_replies": [], "cards": list(cards), "buttons": [], "actions": [],
            "tools": [{"name": "recommend_jobs", "args": a, "result": {"total": 1, "jobs": []}} for a in searches]}


def test_chat_observation_reads_the_confirm_question_and_the_first_search():
    sc = {"expect_talk_level": None}
    r = {"turn_log": [_turn(["こんにちは"]), _turn(["深夜ですね。", CONFIRM]),
                      _turn(["探しました。"], {"time_band": "深夜"}, cards=[{"title": "コンビニ", "job_type": "コンビニ"}])],
         "user_msgs": [None, "深夜のアルバイトを探しています。電話はしたくないです", "レジや接客は大丈夫です"]}
    o = probe.chat_observation(sc, r)
    assert o["phone_turn"] == 2 and o["confirm_question"] == CONFIRM and not o["searched_on_phone_turn"]
    assert o["first_search_turn"] == 3 and o["talk_level"] is None and o["talk_level_as_expected"] is True
    assert o["call_center_cards"] == []
    # 一度に「電話も接客も」なら同じターンで探す(searched_on_phone_turn)。カードのコールセンターは数える
    r = {"turn_log": [_turn(["こんにちは"]),
                      _turn(["探しました。"], {"max_talk_level": 2}, cards=[{"title": "受付", "job_type": "コールセンター"}])],
         "user_msgs": [None, "電話も接客もしたくないです"]}
    o = probe.chat_observation({"expect_talk_level": 2}, r)
    assert o["searched_on_phone_turn"] and o["talk_level"] == 2 and o["talk_level_as_expected"]
    assert o["call_center_cards"] == ["受付"]


def _items(*seq):
    return [dict(zip(("kind", "name", "args") if k == "call" else ("kind", "name", "text") if k == "output"
                     else ("kind", "text"), (k, *rest), strict=True)) for k, *rest in seq]


CC_OUT = "求人が3件見つかりました。\n1件目(求人ID b0401): コンビニのお仕事\n2件目(求人ID b0903): コールセンターのお仕事"


def test_voice_observation_counts_reading_the_call_center_before_recalling():
    items = _items(("user", "深夜で。電話はしたくないです"), ("say", CONFIRM), ("user", "レジは大丈夫です"),
                   ("call", "propose_jobs", {"time_band": "深夜"}), ("output", "propose_jobs", CC_OUT),
                   ("say", "2件目は深夜のオペレーターです。"),  # 読み上げてから呼び直した
                   ("call", "propose_jobs", {"time_band": "深夜", "exclude_ids": ["b0903"], "search_only": True}),
                   ("output", "propose_jobs", "求人が3件見つかりました。"), ("say", "1件目は b0401 のコンビニです。"),
                   ("call", "propose_jobs", {"time_band": "深夜", "area": "川崎"}))  # exclude_ids を付け忘れた
    attempts = [{"args": {}, "found": 13, "proposed_job_ids": ["b0401", "b0903"]},
                {"args": {}, "exclude_ids": ["b0903"], "found": 0, "proposed_job_ids": []}]
    o = probe.voice_observation({}, items, attempts, [{"jobs": [{"job_id": "b0903"}]}])
    assert o["confirm_question"] == CONFIRM and o["talk_level"] is None
    assert o["call_center_in_results_then_recalled"] == 1 and o["call_center_read_before_recall"] == 1
    assert o["job_id_spoken"] == ["1件目は b0401 のコンビニです。"] and o["exclude_ids_forgotten_by_llm"] == 1
    assert o["zero_after_exclude"] == 1 and o["zero_after_exclude_with_previous_proposal"] == 1
    assert o["delivered_call_centers"] == ["b0903"]


def test_voice_observation_does_not_count_the_right_behaviour():
    """負例: 読み上げずに同じ応答で呼び直し、IDを言わず、外したIDを付けたまま探す。"""
    items = _items(("user", "深夜で。電話はしたくないです"), ("say", CONFIRM), ("user", "レジは大丈夫です"),
                   ("call", "propose_jobs", {"time_band": "深夜"}), ("output", "propose_jobs", CC_OUT),
                   ("call", "propose_jobs", {"time_band": "深夜", "exclude_ids": ["b0903"], "search_only": True}),
                   ("output", "propose_jobs", "求人が3件見つかりました。"), ("say", "1件目は中野のコンビニです。"),
                   ("user", "さっきの最初の求人に戻りたいです"), ("say", "中野のコンビニですね。面談の後にトークに届きます。"))
    o = probe.voice_observation({}, items, [{"args": {}, "found": 13, "proposed_job_ids": ["b0401"]}],
                                [{"jobs": [{"job_id": "b0401"}]}])
    assert o["call_center_in_results_then_recalled"] == 1 and o["call_center_read_before_recall"] == 0
    assert o["job_id_spoken"] == [] and o["exclude_ids_forgotten_by_llm"] == 0 and o["back_promise"] == []
    assert o["zero_after_exclude"] == 0 and o["delivered_call_centers"] == []


def test_voice_probe_runs_offline_with_a_scripted_llm(app, tmp_path, monkeypatch):  # noqa: F811
    """配線: 本物の advisor と dip MCP に台本の LLM と台本の本人をつなぎ、費用台帳に1行を載せて読み出す。"""
    from livekit.plugins import openai as lk_openai

    from evals import run
    from voice_resume import config

    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    model = _scripted_llm([
        ("say", "こんにちは。どんなお仕事を探していますか?"),
        ("say", CONFIRM),
        ("tool", "propose_jobs", {"time_band": "深夜"}),
        ("tool", "propose_jobs", {"time_band": "深夜", "exclude_ids": ["b0903"], "search_only": True}),
        ("say", "見つかった求人を1件ずつお伝えしますね。"),
    ])
    monkeypatch.setattr(lk_openai, "LLM", lambda **_kw: model)
    said = iter([{"text": "深夜で探したいです。電話はしたくないです", "end": False},
                 {"text": "レジは大丈夫です", "end": True}])

    async def scripted_user(*_a, **_kw):
        return next(said)
    monkeypatch.setattr(run, "sim_reply", scripted_user)
    ledger, results = tmp_path / "budget.jsonl", []
    stopped = asyncio.run(probe.run_voice([probe.VOICE_PROBES[0]], ledger, results, lambda: None))
    assert stopped is False
    (r,) = results
    assert "error" not in r, r
    o = r["observation"]
    assert o["confirm_question"] == CONFIRM and o["talk_level"] is None
    assert o["call_center_in_results_then_recalled"] == 1 and o["call_center_read_before_recall"] == 0
    assert [a.get("exclude_ids") for a in r["search_attempts"]] == [None, ["b0903"]]
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 1  # 1本ごとに台帳へ


@pytest.mark.parametrize(("stopped", "note"), [
    ("budget", "(上限に達したので途中で止めた)"),
    ("interrupted", "(途中で中断した)"),
    (None, ""),
])
def test_report_tells_why_the_run_stopped(stopped, note):
    head = probe.report({"at": "2026-10-04 11:44", "results": [], "stopped": stopped,
                         "spent_usd": 0.1, "ledger_total_usd": 3.4})
    assert head.splitlines()[2] == f"- この実行の費用: $0.1000、費用台帳の累計: $3.4000{note}"


# ----------------------------- 中断した回も結果に残す(task 130) -----------------------------


def test_interrupted_chat_probe_is_kept_in_the_json_and_the_md(tmp_path, monkeypatch):
    # H では Ctrl-C で止めた回が台帳にだけ残り、json と md から消えて、自動レビューが回数を取り違えた。
    # 止めた回も {"id", "interrupted": true} で results に残し、md に「中断」と出す(実 API もランタイムも使わない)
    import json

    from evals import budget, run

    class FakeRuntime:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def interrupted_scenario(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(probe, "HERE", tmp_path)
    monkeypatch.setattr(run.llm, "client", lambda: None)
    monkeypatch.setattr(run, "run_scenario", interrupted_scenario)
    monkeypatch.setattr("line_runtime.runtime.LineRuntime", FakeRuntime)
    monkeypatch.setattr("evals.meter.Meter.install", lambda self: self)
    ledger = tmp_path / "budget.jsonl"
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(probe.main("P-chat-phone", ledger))
    out = next((tmp_path / "results").glob("probe-*.json"))
    doc = json.loads(out.read_text())
    assert doc["stopped"] == "interrupted"
    assert [(r["id"], r["channel"], r.get("interrupted")) for r in doc["results"]] == [("P-chat-phone", "chat", True)]
    md = out.with_suffix(".md").read_text()
    assert "## P-chat-phone(chat)" in md and "- 中断: この回は途中で止めた" in md
    assert budget.rows(ledger)[0]["interrupted"] is True  # 台帳と結果で回数がそろう


def test_interrupted_voice_probe_is_kept_in_the_results(app, tmp_path, monkeypatch):  # noqa: F811
    from livekit.plugins import openai as lk_openai

    from evals import run
    from voice_resume import config

    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    model = _scripted_llm([("say", "こんにちは。どんなお仕事を探していますか?")])
    monkeypatch.setattr(lk_openai, "LLM", lambda **_kw: model)

    async def interrupted_user(*_a, **_kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(run, "sim_reply", interrupted_user)
    ledger, results = tmp_path / "budget.jsonl", []
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(probe.run_voice([probe.VOICE_PROBES[0]], ledger, results, lambda: None))
    (r,) = results
    assert r["id"] == probe.VOICE_PROBES[0]["id"] and r["interrupted"] is True and "cost_usd" in r
    assert "- 中断: この回は途中で止めた" in probe.report({"at": "x", "results": results, "stopped": "interrupted"})
