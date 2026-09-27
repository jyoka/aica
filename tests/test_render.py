"""求人カードの描画: dip のツールが返した求人(どのツールでも)だけをカードにする。LLM は使わない。"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from dip_mcp import logic
from line_runtime.agent import LineReply
from line_runtime.runtime import render


def _state(tool_name: str, result: dict, job_ids: list[str]) -> dict:
    msgs = [HumanMessage("詳しく聞きたい"),
            AIMessage("", tool_calls=[{"name": tool_name, "args": {}, "id": "c1", "type": "tool_call"}]),
            ToolMessage(json.dumps(result, ensure_ascii=False), name=tool_name, tool_call_id="c1")]
    return {"messages": msgs, "structured_response": LineReply(bubbles=["どうぞ"], job_ids=job_ids)}


def test_job_from_answer_job_question_becomes_a_card():
    r = render(_state("answer_job_question", logic.answer_material("未経験でも大丈夫?", "a003"), ["a003"]))
    assert [c["job_id"] for c in r["cards"]] == ["a003"] and r["cards"][0]["reasons"]
    assert r["unknown_job_ids"] == []


def test_job_no_tool_returned_is_not_a_card():
    r = render(_state("answer_job_question", logic.answer_material("面接の服装は?"), ["b0101"]))
    assert r["cards"] == [] and r["unknown_job_ids"] == ["b0101"]
