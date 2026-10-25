"""エージェントの OpenAI の呼び出しが、OpenAI に応答を残さない(store=False)まま 2 ターン以上の会話を続けられる
(docs/design/privacy.md の 3.2「OpenAI の呼び出し全般」)。

実 OpenAI は呼ばない。本物の ChatOpenAI(agent.build_model)を、偽の OpenAI の Responses API(httpx.MockTransport)に
つなぐ。偽の OpenAI は、store=False のときの本物の決まりを真似る: 応答を残さないので、前の応答の ID
(previous_response_id)も、前の応答の項目の ID だけの参照(メッセージの ID、中身の無い reasoning)も解決できず 400 を返す。
reasoning のあるモデルがツールを呼んだら、次の呼び出しでその reasoning を送り返さないと 400 を返す。
会話は本物の LineRuntime(ファイルの会話の記録と dip の MCP)で進め、2 ターン目は会話の記録から読み直した全文を送る。
"""

from __future__ import annotations

import asyncio
import json
from functools import partial

import httpx
import pytest
from langchain_openai import ChatOpenAI

UID = "sim-new"  # fixtures の架空のユーザー
TURN1, TURN2 = "渋谷で日中", "1件目はどんな仕事ですか"
REPLY1, REPLY2 = "渋谷・日中で3件です。", "チャット中心の事務です。"


def _reply(bubbles: list[str], job_ids: list[str]) -> str:
    return json.dumps({"bubbles": bubbles, "quick_replies": ["今はいい"], "job_ids": job_ids, "buttons": [],
                       "actions": []}, ensure_ascii=False)


_ID_PREFIX = {"reasoning": "rs", "function_call": "fc", "message": "msg"}  # 本物の項目の ID の形


class FakeOpenAI:
    """偽の OpenAI の Responses API。script の出力(項目の並び)を順に返し、受けた本文を requests に残す。"""

    def __init__(self, script: list[list[dict]]) -> None:
        self.script = script
        self.requests: list[dict] = []
        self.issued: dict[str, dict] = {}  # 返した項目(ID ごと)。store=False なので、次の呼び出しから ID では引けない
        self.reasoning_of_call: dict[str, str] = {}  # ツールの呼び出し(call_id)→ 一緒に返した reasoning の中身
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        self.requests.append(body)
        if error := self._reject(body):
            return httpx.Response(400, json={"error": {"message": error, "type": "invalid_request_error"}})
        n = len(self.requests)
        output = []
        for i, item in enumerate(self.script.pop(0)):
            item = {"id": f"{_ID_PREFIX[item['type']]}_{n}_{i}", **item}
            if item["type"] == "reasoning":
                item.setdefault("summary", [])
                if "reasoning.encrypted_content" in (body.get("include") or []):
                    item["encrypted_content"] = f"enc-{n}-{i}"
            output.append(item)
        for item in output:
            self.issued[item["id"]] = item
            if item["type"] == "function_call":
                self.reasoning_of_call[item["call_id"]] = next(
                    (r.get("encrypted_content") for r in output if r["type"] == "reasoning"), "")
        return httpx.Response(200, json={
            "id": f"resp_{n}", "object": "response", "created_at": 1792540800, "status": "completed",
            "model": body["model"], "output": output, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 5}},
        })

    def _reject(self, body: dict) -> str | None:
        """store=False のときに本物が断る入力なら、その理由。"""
        if body.get("store") is not False:
            return "this fake only serves store=false"
        if body.get("previous_response_id"):
            return "Previous response cannot be used when store is false"
        sent_reasoning: set[str] = set()
        for item in body["input"]:
            kind = item.get("type", "message")
            if kind == "item_reference" or (kind == "message" and item.get("id") in self.issued):
                return f"Item with id '{item.get('id')}' not found. Items are not persisted when `store` is false"
            if kind == "reasoning":
                if not item.get("encrypted_content"):
                    return f"Item with id '{item.get('id')}' not found. Items are not persisted when `store` is false"
                sent_reasoning.add(item["encrypted_content"])
            if kind == "function_call" and self.reasoning_of_call.get(item["call_id"], "") not in sent_reasoning | {""}:
                return f"Item '{item.get('id')}' of type 'function_call' was provided without its required 'reasoning'"
        return None


@pytest.fixture
def runtime_on_fake_openai(tmp_path, monkeypatch):
    """本物の ChatOpenAI(build_model)を偽の OpenAI につなぎ、会話の記録をファイルに書く LineRuntime を作る関数。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    from line_runtime import agent, config
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(config, "PROVIDER", "openai")
    monkeypatch.setattr(config, "MODEL", "gpt-6-sol")
    monkeypatch.setattr(config, "EFFORT", "low")  # reasoning の項目が出る設定
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    def make(fake: FakeOpenAI) -> LineRuntime:
        client = httpx.AsyncClient(transport=fake.transport)
        monkeypatch.setattr(agent, "ChatOpenAI", partial(ChatOpenAI, http_async_client=client, max_retries=0))
        return LineRuntime(model=agent.build_model())

    return make


def _two_turns(rt) -> list[dict]:
    async def go():
        async with rt:
            return [await rt.handle(UID, "message", TURN1), await rt.handle(UID, "message", TURN2)]

    return asyncio.run(go())


def _texts(item: dict) -> list[str]:
    content = item.get("content")
    if isinstance(content, str):
        return [content]
    return [c.get("text", "") for c in content or []]


def test_agent_keeps_a_two_turn_conversation_with_store_false(runtime_on_fake_openai):
    fake = FakeOpenAI([
        # 1 ターン目: 考えてから求人を探し(ツール)、考えてから返す
        [{"type": "reasoning"},
         {"type": "function_call", "call_id": "call_rec", "name": "recommend_jobs", "status": "completed",
          "arguments": json.dumps({"line_user_id": UID, "area": "渋谷", "time_band": "日中"}, ensure_ascii=False)}],
        [{"type": "reasoning"},
         {"type": "message", "role": "assistant", "status": "completed",
          "content": [{"type": "output_text", "annotations": [], "text": _reply([REPLY1], ["b0601"])}]}],
        # 2 ターン目: 前のターンを踏まえて返す
        [{"type": "reasoning"},
         {"type": "message", "role": "assistant", "status": "completed",
          "content": [{"type": "output_text", "annotations": [], "text": _reply([REPLY2], ["b0601"])}]}],
    ])
    r1, r2 = _two_turns(runtime_on_fake_openai(fake))

    assert r1["bubbles"] == [REPLY1] and r1["tools"][0]["name"] == "recommend_jobs"
    assert r2["bubbles"] == [REPLY2]
    assert len(fake.requests) == 3 and fake.script == []
    # どの呼び出しも、OpenAI に応答を残さず、前の応答を ID で参照しない。reasoning は暗号化した中身で受け取る
    for body in fake.requests:
        assert body["store"] is False
        assert "previous_response_id" not in body
        assert body["include"] == ["reasoning.encrypted_content"]

    # 2 ターン目は、1 ターン目の全文(本人の発言、ツールの呼び出しと結果、返事、reasoning の中身)を送り直している
    sent = fake.requests[2]["input"]
    kinds = [i.get("type", "message") for i in sent]
    users = [t for i in sent if i.get("role") == "user" for t in _texts(i)]
    assert any(TURN1 in t for t in users) and any(TURN2 in t for t in users)
    assert any(REPLY1 in t for i in sent if i.get("role") == "assistant" for t in _texts(i))
    assert "function_call" in kinds and "function_call_output" in kinds
    assert {i["encrypted_content"] for i in sent if i.get("type") == "reasoning"} == {"enc-1-0", "enc-2-0"}


def test_fake_openai_rejects_what_store_false_cannot_resolve():
    """偽の OpenAI が、store=False で本物が断る入力を断る(上のテストが本物の決まりで確かめている証拠)。"""
    fake = FakeOpenAI([])
    fake.issued["msg_1"] = {}
    fake.reasoning_of_call["call_1"] = "enc-1"
    base = {"model": "gpt-6-sol", "store": False}
    assert fake._reject({**base, "store": None, "input": []})
    assert fake._reject({**base, "previous_response_id": "resp_1", "input": []})
    assert fake._reject({**base, "input": [{"type": "message", "id": "msg_1", "role": "assistant", "content": []}]})
    assert fake._reject({**base, "input": [{"type": "reasoning", "id": "rs_1", "summary": []}]})
    assert fake._reject({**base, "input": [{"type": "function_call", "call_id": "call_1", "name": "f",
                                            "arguments": "{}"}]})
    assert fake._reject({**base, "input": [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc-1"},
        {"type": "function_call", "call_id": "call_1", "name": "f", "arguments": "{}"}]}) is None
