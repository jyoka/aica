"""Anthropic(既定のプロバイダ。task 235)に送る中身を、偽の Anthropic の Messages API で確かめる。実 API は呼ばない。

偽の Anthropic(FakeAnthropic)は、Claude Sonnet 5.5 と Claude Haiku 5.5 で本物が 400 を返す組み合わせを断る
(2026-10 の Anthropic の資料: Sonnet 5.5 の thinking disabled と tool_choice の any / tool、between_tools に
ほかの欄を足す、xhigh 以上で思考を切る、block_binding をベータのヘッダーなしで送る、サンプリングの値、
assistant の prefill、5 つ以上のキャッシュの区切り、返していない思考のブロック)。
プロンプトキャッシュも真似る: 区切り(cache_control)までの前置き(tools → system → messages の順)を覚え、次の呼び出しの
前置きが同じなら、その分を usage の cache_read_input_tokens で返す。トークン数は文字数から見積もる(3 文字で 1)。
- エージェント: 本物の ChatAnthropic(agent.build_model)と本物の LineRuntime(dip の MCP)で 2 ターン話す
- SDK を直に呼ぶところ(llm.parse。模擬ユーザー、ジャッジ、ゲームの AI): 本物の AsyncAnthropic で送る
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from line_runtime import llm

UID = "sim-new"  # fixtures の架空のユーザー
TURN1, TURN2 = "渋谷で日中", "1件目はどんな仕事ですか"
REPLY1, REPLY2 = "渋谷・日中で3件です。", "チャット中心の事務です。"
BINDING_BETA = "thinking-binding-controls-2026-08-01"
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
MIN_CACHE = 512  # Sonnet 5.5 と Haiku 5.5 のキャッシュできる最小の前置き(トークン)


def _reply(bubbles: list[str], job_ids: list[str]) -> str:
    return json.dumps({"bubbles": bubbles, "quick_replies": ["今はいい"], "job_ids": job_ids, "buttons": [],
                       "actions": []}, ensure_ascii=False)


def _strip(x):
    """キャッシュの前置きの比べ方: cache_control の印は比べない(区切りの場所は呼び出しごとに動く)。"""
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if k != "cache_control"}
    if isinstance(x, list):
        return [_strip(v) for v in x]
    return x


class FakeAnthropic:
    """偽の Anthropic の Messages API。script の content(ブロックの並び)を順に返し、受けた本文と beta を残す。"""

    def __init__(self, script: list[list[dict]] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict] = []
        self.betas: list[list[str]] = []
        self.usages: list[dict] = []
        self.signatures: set[str] = set()  # 返した思考のブロックの signature
        self.cache: set[str] = set()  # 書いたキャッシュの前置き(のハッシュ)
        self.transport = httpx2.MockTransport(self._handle)

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(api_key="offline-test", max_retries=0,
                                        http_client=httpx2.AsyncClient(transport=self.transport))

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/messages"
        body = json.loads(request.content)
        betas = [b for b in request.headers.get("anthropic-beta", "").split(",") if b]
        self.requests.append(body)
        self.betas.append(betas)
        if error := self.reject(body, betas):
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                            "message": error}})
        n = len(self.requests)
        content = []
        if (body.get("thinking") or {}).get("type", "adaptive") == "adaptive":
            content.append({"type": "thinking", "thinking": "", "signature": f"sig-{n}"})
            self.signatures.add(f"sig-{n}")
        content += self.script.pop(0)
        usage = self._cache(body)
        self.usages.append(usage)
        return httpx2.Response(200, json={
            "id": f"msg_{n}", "type": "message", "role": "assistant", "model": body["model"], "content": content,
            "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn",
            "stop_sequence": None, "usage": usage | {"output_tokens": 20}})

    def reject(self, body: dict, betas: list[str]) -> str | None:
        """本物の Claude Sonnet 5.5 / Haiku 5.5 が 400 を返す本文なら、その理由。"""
        model = body.get("model", "")
        sonnet, haiku = model.startswith("claude-sonnet-5-5"), model.startswith("claude-haiku-5-5")
        if not (sonnet or haiku):
            return f"this fake only serves claude-sonnet-5-5 and claude-haiku-5-5, not {model!r}"
        if any(body.get(k) is not None for k in ("temperature", "top_p", "top_k")):
            return "sampling parameters are not supported for this model"
        if "output_format" in body:
            return "output_format: use output_config.format"
        oc = body.get("output_config") or {}
        effort = oc.get("effort", "high" if sonnet else "medium")
        if effort not in EFFORTS:
            return f"output_config.effort: {effort!r}"
        thinking = body.get("thinking") or {"type": "adaptive"}
        kind = thinking.get("type")
        if kind == "enabled":
            return "thinking.type.enabled (budget_tokens) is not supported for this model"
        if kind == "disabled" and sonnet:
            return '"thinking.type.disabled" is not supported for this model. Use "thinking.type.between_tools"'
        if kind == "between_tools" and (haiku or set(thinking) != {"type"}):
            return "thinking.type.between_tools: not supported for this model or takes no other field"
        if kind in ("disabled", "between_tools") and effort in ("xhigh", "max"):
            return f"output_config.effort {effort!r} is not supported when thinking is disabled"
        if "block_binding" in thinking and (BINDING_BETA not in betas or kind != "adaptive"):
            return "block_binding: Extra inputs are not permitted"
        if sonnet and (body.get("tool_choice") or {}).get("type") in ("any", "tool"):
            return 'tool_choice: type "tool" and "any" are not supported for this model.'
        msgs = body.get("messages") or []
        if not msgs or msgs[-1]["role"] != "user":
            return "assistant message prefill is not supported for this model"
        for m in msgs:
            for b in m["content"] if isinstance(m["content"], list) else []:
                if b.get("type") == "thinking" and b.get("signature") not in self.signatures:
                    return "Invalid `signature` in `thinking` block"
        marks = sum(1 for b in FakeAnthropic._blocks(body) if "cache_control" in b) + ("cache_control" in body)
        if marks > 4:
            return f"A maximum of 4 blocks with cache_control may be provided. Found {marks}."
        return None

    @staticmethod
    def _blocks(body: dict) -> list[dict]:
        """キャッシュの前置きの並び(tools → system → messages)。"""
        system = body.get("system")
        system = [{"type": "text", "text": system}] if isinstance(system, str) else (system or [])
        out = [*(body.get("tools") or []), *system]
        for m in body.get("messages") or []:
            content = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
            out += [{"role": m["role"], **b} for b in content]
        return out

    def _cache(self, body: dict) -> dict:
        """区切りまでの前置きを覚え、前に覚えた前置きと同じところまでを読みとして返す(本物の 5 分のキャッシュの真似)。"""
        blocks = self._blocks(body)
        marks = {i for i, b in enumerate(blocks) if "cache_control" in b}
        if "cache_control" in body and blocks:
            marks.add(len(blocks) - 1)  # top-level の区切りは最後のブロック
        h = hashlib.sha256(body["model"].encode())
        sizes, hashes = [], []
        total = 0
        for b in blocks:
            text = json.dumps(_strip(b), ensure_ascii=False, sort_keys=True)
            h.update(text.encode())
            total += len(text) // 3
            sizes.append(total)
            hashes.append(h.hexdigest())
        read = max((sizes[i] for i in range(len(blocks)) if hashes[i] in self.cache), default=0)
        write = 0
        for i in sorted(marks):
            if sizes[i] >= MIN_CACHE and sizes[i] > read:
                self.cache.add(hashes[i])
                write = sizes[i] - read
        return {"input_tokens": total - read - write, "cache_read_input_tokens": read,
                "cache_creation_input_tokens": write}


# ----------------------------- エージェント(ChatAnthropic) -----------------------------


@pytest.fixture
def runtime_on_fake_anthropic(tmp_path, monkeypatch):
    """既定の設定の ChatAnthropic(build_model)を偽の Anthropic につなぎ、LineRuntime を作る関数。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")
    from line_runtime import agent, config
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    def make(fake: FakeAnthropic) -> LineRuntime:
        model = agent.build_model()
        model.__dict__["_async_client"] = fake.client()  # cached_property を偽物のクライアントに差し替える
        return LineRuntime(model=model)

    return make


def test_the_defaults_are_anthropic_with_sonnet_and_haiku():
    """既定(LINE_SIM_PROVIDER も LINE_SIM_MODEL も無い)で、トーク、模擬ユーザー、ジャッジ、ゲームの AI が Anthropic。"""
    import importlib
    import os

    from evals.run import JUDGE_MODEL, SIM_MODEL
    from line_runtime import config
    from resume_arcade.stages import QUICK_MODEL

    saved = {k: os.environ.pop(k) for k in ("LINE_SIM_PROVIDER", "LINE_SIM_MODEL") if k in os.environ}
    try:
        fresh = importlib.reload(config)
        assert (fresh.PROVIDER, fresh.MODEL, fresh.EFFORT) == ("anthropic", "claude-sonnet-5-5", "low")
    finally:
        os.environ.update(saved)
        importlib.reload(config)
    assert SIM_MODEL["anthropic"] == JUDGE_MODEL["anthropic"] == "claude-sonnet-5-5"
    assert QUICK_MODEL["anthropic"] == "claude-haiku-5-5"
    assert (SIM_MODEL["openai"], QUICK_MODEL["openai"]) == ("gpt-6-luna", "gpt-6-luna")  # openai に戻せば元どおり


def test_agent_sends_what_sonnet_accepts_and_reads_the_cache_from_the_second_call(runtime_on_fake_anthropic):
    fake = FakeAnthropic([
        # 1 ターン目: 求人を探し(ツール)、返す
        [{"type": "tool_use", "id": "toolu_rec", "name": "recommend_jobs",
          "input": {"line_user_id": "me", "area": "渋谷", "time_band": "日中"}}],
        [{"type": "text", "text": _reply([REPLY1], ["b0601"])}],
        # 2 ターン目: 前のターンを踏まえて返す
        [{"type": "text", "text": _reply([REPLY2], ["b0601"])}],
    ])
    rt = runtime_on_fake_anthropic(fake)

    async def go():
        async with rt:
            return [await rt.handle(UID, "message", TURN1), await rt.handle(UID, "message", TURN2)], rt.ledger.rows

    (r1, r2), rows = asyncio.run(go())

    assert r1["bubbles"] == [REPLY1] and r1["tools"][0]["name"] == "recommend_jobs"
    assert r2["bubbles"] == [REPLY2]
    assert len(fake.requests) == 3 and fake.script == []  # 400 は 1 回も無い(断られたら script が残る)
    for body, betas in zip(fake.requests, fake.betas, strict=True):
        assert body["model"] == "claude-sonnet-5-5"
        assert body["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        assert BINDING_BETA in betas
        assert body["output_config"]["effort"] == "low"
        assert body["output_config"]["format"]["type"] == "json_schema"  # LineReply は構造化出力で、ツールの強制ではない
        assert (body.get("tool_choice") or {"type": "auto"})["type"] == "auto"
        names = [t["name"] for t in body["tools"]]
        assert names == sorted(names) and "recommend_jobs" in names
        # キャッシュの区切り: ツールの最後、system の最後、メッセージの最後(top-level)
        assert "cache_control" in body["tools"][-1] and "cache_control" in body["system"][-1]
        assert body["cache_control"]["type"] == "ephemeral"
    # system とツールは会話の途中で変わらない(前置きが同じなので、キャッシュが効く)
    assert len({json.dumps(_strip(b["system"]), ensure_ascii=False) for b in fake.requests}) == 1
    assert len({json.dumps(_strip(b["tools"]), ensure_ascii=False) for b in fake.requests}) == 1
    # 2 回目の呼び出しから、前の呼び出しの前置きをキャッシュから読む
    reads = [u["cache_read_input_tokens"] for u in fake.usages]
    assert reads[0] == 0 and all(r > 0 for r in reads[1:])
    assert fake.usages[0]["cache_creation_input_tokens"] > 0
    # 2 ターン目は、1 ターン目の思考のブロック(signature)をそのまま送り返している
    sent = [b for m in fake.requests[2]["messages"] if isinstance(m["content"], list) for b in m["content"]]
    assert {b["signature"] for b in sent if b["type"] == "thinking"} == {"sig-1", "sig-2"}
    # 費用台帳は、キャッシュの読み書きを含む入力の合計と、読み書きの量を残し、Sonnet 5.5 の料金で数える
    assert [r["model"] for r in rows] == ["claude-sonnet-5-5"] * 3
    assert rows[0]["cache_write"] > 0 and all(r["cache_read"] > 0 for r in rows[1:])
    for r, u in zip(rows, fake.usages, strict=True):
        assert r["input_tokens"] == u["input_tokens"] + u["cache_read_input_tokens"] + u["cache_creation_input_tokens"]
        expect = (u["input_tokens"] * 2.0 + u["cache_creation_input_tokens"] * 2.5 + u["cache_read_input_tokens"] * 0.2
                  + 20 * 10.0) / 1e6
        assert r["cost_usd"] == pytest.approx(expect, abs=1e-6)


def test_fake_anthropic_rejects_what_sonnet_and_haiku_reject():
    """偽の Anthropic が、本物が 400 を返す組み合わせを断る(上のテストが本物の決まりで確かめている証拠)。"""
    user = [{"role": "user", "content": "こんにちは"}]
    sonnet = {"model": "claude-sonnet-5-5", "max_tokens": 100, "messages": user}
    haiku = sonnet | {"model": "claude-haiku-5-5"}
    ok = FakeAnthropic().reject
    assert ok(sonnet, []) is None and ok(haiku, []) is None
    assert ok(sonnet | {"thinking": {"type": "disabled"}}, [])
    assert ok(haiku | {"thinking": {"type": "disabled"}}, []) is None
    assert ok(haiku | {"thinking": {"type": "disabled"}, "output_config": {"effort": "xhigh"}}, [])
    assert ok(sonnet | {"thinking": {"type": "between_tools"}, "output_config": {"effort": "low"}}, []) is None
    assert ok(sonnet | {"thinking": {"type": "between_tools"}, "output_config": {"effort": "max"}}, [])
    assert ok(sonnet | {"thinking": {"type": "between_tools", "display": "summarized"}}, [])
    assert ok(haiku | {"thinking": {"type": "between_tools"}}, [])
    assert ok(sonnet | {"thinking": {"type": "enabled", "budget_tokens": 2000}}, [])
    assert ok(sonnet | {"thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}}, [])
    assert ok(sonnet | {"tool_choice": {"type": "any"}}, []) and ok(sonnet | {"tool_choice": {"type": "tool"}}, [])
    assert ok(haiku | {"tool_choice": {"type": "any"}}, []) is None  # Haiku 5.5 は強制を受ける(思考は飛ぶ)
    assert ok(sonnet | {"temperature": 0}, []) and ok(sonnet | {"output_config": {"effort": "none"}}, [])
    assert ok(sonnet | {"messages": [*user, {"role": "assistant", "content": "{"}]}, [])
    assert ok(sonnet | {"messages": [*user, {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "forged"}]}, *user]}, [])
    assert ok(sonnet | {"model": "claude-opus-5"}, [])
    assert ok(sonnet | {"system": [{"type": "text", "text": str(i), "cache_control": {"type": "ephemeral"}}
                                   for i in range(4)], "cache_control": {"type": "ephemeral"}}, [])


# ----------------------------- SDK を直に呼ぶところ(llm.parse) -----------------------------


class Out(BaseModel):
    text: str


def _parse_on_fake(model: str, effort: str, repeat: int = 1) -> FakeAnthropic:
    fake = FakeAnthropic([[{"type": "text", "text": '{"text": "はい"}'}] for _ in range(repeat)])
    client = fake.client()

    async def go():
        for _ in range(repeat):
            got, usage = await llm.parse(client, model=model, system="指示。" * 800, schema=Out, max_tokens=1000,
                                         effort=effort, with_usage=True, messages=[{"role": "user", "content": "質問"}])
            assert got == Out(text="はい")
            assert usage["input_tokens"] > 0

    asyncio.run(go())
    assert fake.script == []  # 断られていない
    return fake


@pytest.mark.parametrize(("model", "effort", "thinking", "sent_effort"), [
    ("claude-sonnet-5-5", "none", {"type": "between_tools"}, "low"),  # 模擬ユーザー: 思考を切る(Sonnet 5.5 の切り方)
    ("claude-sonnet-5-5", "low", None, "low"),  # ジャッジ、ゲームの最後のまとめ: 思考は任せて effort low
    ("claude-haiku-5-5", "none", {"type": "disabled"}, "low"),  # ゲームの中の反応: 思考を切る(Haiku 5.5 の切り方)
])
def test_llm_parse_sends_thinking_and_effort_each_model_accepts(model, effort, thinking, sent_effort):
    body = _parse_on_fake(model, effort).requests[0]
    assert body["model"] == model and body.get("thinking") == thinking
    assert body["output_config"]["effort"] == sent_effort
    assert body["output_config"]["format"]["type"] == "json_schema" and "tool_choice" not in body
    assert body["cache_control"] == {"type": "ephemeral"}  # 最後の区切り(会話が伸びる模擬ユーザー、2 回見るジャッジ)
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}  # 指示の後の区切り(答えだけが変わるゲームの段)


def test_llm_parse_reads_the_cache_on_the_second_call_with_the_same_instructions():
    """ジャッジ(同じ会話を 2 回判定)や模擬ユーザー(同じ設定で何度も)は、2 回目から指示をキャッシュから読む。"""
    fake = _parse_on_fake("claude-sonnet-5-5", "low", repeat=2)
    assert fake.usages[0]["cache_read_input_tokens"] == 0 and fake.usages[0]["cache_creation_input_tokens"] > 0
    assert fake.usages[1]["cache_read_input_tokens"] > 0


def test_same_instructions_with_a_different_answer_read_the_instructions_from_the_cache():
    """ゲームの段のように、指示が同じで本人の答えだけが変わる呼び出しも、2 回目から指示をキャッシュから読む。"""
    fake = FakeAnthropic([[{"type": "text", "text": '{"text": "はい"}'}] for _ in range(2)])
    client = fake.client()

    async def go():
        for answer in ("一つ目の答え", "二つ目の答え"):
            await llm.parse(client, model="claude-haiku-5-5", system="指示。" * 800, schema=Out, max_tokens=1000,
                            messages=[{"role": "user", "content": answer}])

    asyncio.run(go())
    assert fake.script == [] and fake.usages[1]["cache_read_input_tokens"] > 0


@pytest.mark.parametrize(("model", "effort", "thinking", "beta"), [
    ("claude-sonnet-5-5", "low", {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}, True),
    ("claude-sonnet-5-5", "none", {"type": "between_tools"}, False),  # LINE_SIM_EFFORT=none: block_binding は付けられない
    ("claude-haiku-5-5", "none", {"type": "disabled"}, False),
])
def test_agent_model_settings_are_accepted_for_env_overrides(monkeypatch, model, effort, thinking, beta):
    """LINE_SIM_MODEL と LINE_SIM_EFFORT を替えても、エージェントの送り方が 400 にならない(偽の Anthropic で 1 回送る)。"""
    from langchain_core.messages import HumanMessage

    from line_runtime import agent

    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")
    m = agent.build_model(model, effort)
    fake = FakeAnthropic([[{"type": "text", "text": "こんにちは"}]])
    m.__dict__["_async_client"] = fake.client()
    asyncio.run(m.ainvoke([HumanMessage("こんにちは")]))
    assert fake.script == []
    assert fake.requests[0].get("thinking") == thinking and (BINDING_BETA in fake.betas[0]) is beta


def test_agent_model_without_an_anthropic_key_stops_at_startup(monkeypatch):
    """鍵が無ければ、最初の返事で失敗する前に、起動の途中で止める(ChatOpenAI と同じ)。"""
    from line_runtime import agent

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        agent.build_model()
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "offline-test")  # SDK はこちらも読む
    assert agent.build_model().model == "claude-sonnet-5-5"


def test_sim_reply_never_ends_on_its_own_turn():
    """模擬ユーザーの最後の発言の後にアシスタントが黙っても(音声の probe)、assistant で終わらない(prefill は 400)。"""
    from evals.run import SILENT_AGENT, sim_reply

    fake = FakeAnthropic([[{"type": "text", "text": '{"text": "はい", "end": false}'}]])
    transcript = [{"who": "agent", "text": "こんにちは"}, {"who": "user", "text": "バイトを探しています"}]
    assert asyncio.run(sim_reply(fake.client(), "学生", transcript)) == {"text": "はい", "end": False}
    assert fake.script == [] and fake.requests[0]["messages"][-1] == {"role": "user", "content": SILENT_AGENT}


def test_llm_parse_on_openai_keeps_store_false():
    """LINE_SIM_PROVIDER=openai に戻したとき、SDK を直に呼ぶところは前のとおり OpenAI に応答を残さない。"""
    from types import SimpleNamespace

    sent = {}

    async def parse(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(output_parsed=Out(text="はい"), usage=SimpleNamespace(
            input_tokens=10, output_tokens=1, input_tokens_details=SimpleNamespace(cached_tokens=0)))

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    got = asyncio.run(llm.parse(client, model="gpt-6-luna", system="指示", schema=Out, max_tokens=100, effort="none",
                                messages=[{"role": "user", "content": "質問"}]))
    assert got == Out(text="はい")
    assert (sent["store"], sent["reasoning"], sent["instructions"]) == (False, {"effort": "none"}, "指示")


def test_consent_names_the_provider_that_gets_the_talk(monkeypatch):
    """同意の文言の送り先は LINE_SIM_PROVIDER に合わせる(既定は Anthropic。openai なら OpenAI)。"""
    from line_runtime import config, consent

    first = lambda: consent.texts()["terms"]["points"][1]
    assert "米国の Anthropic" in first() and "米国の OpenAI にも" in first()
    monkeypatch.setattr(config, "PROVIDER", "openai")
    assert "米国の OpenAI に送ります" in first() and "Anthropic" not in first()


def test_thinking_kwargs_leave_older_models_alone():
    """effort を受けないモデル(Claude Haiku 4.5)には thinking も effort も付けない(付けると 400)。"""
    assert llm.thinking_kwargs("claude-haiku-4-5", "none") == {}
    assert llm.thinking_kwargs("claude-haiku-4-5", "low") == {}
    assert llm.thinking_kwargs("claude-opus-5-5", "none") == {"output_config": {"effort": "low"}}  # 思考は切れない
