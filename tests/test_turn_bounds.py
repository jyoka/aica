"""1 ターンの費用の上限(task 199): 発言の長さと、1 ターンのモデルの呼び出し回数。実 LLM なし(偽のモデル)。

直す前は、20 万字の発言がそのままモデルに届き、ツールを呼び続けるモデルは 1 ターンに 41 回呼ばれていた
(同じ偽のモデルで確かめた)。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

from line_runtime import turn
from line_runtime.agent import (
    LAST_CALL_NOTE,
    MAX_MODEL_CALLS_PER_TURN,
    TURN_LIMIT_REPLY,
)
from line_runtime.runtime import history
from tests.test_handle import call, reply
from tests.test_quota import _server, _stream


class LengthModel(BaseChatModel):
    """受け取った最後の本人の発言の長さを記録して、決まった返信を返す。"""

    lengths: list[int] = []  # noqa: RUF012  pydantic の項目

    @property
    def _llm_type(self) -> str:
        return "length"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.lengths.append(len(next(m.content for m in reversed(messages) if m.type == "human")))
        return ChatResult(generations=[ChatGeneration(message=reply(["はい、どうぞ。"]))])


class LoopingModel(BaseChatModel):
    """loops 回はツールを呼び続け、その後に返信する(止まらないモデルの代わり)。"""

    loops: int = 40
    calls: int = 0
    last_call_notes: list[bool] = []  # noqa: RUF012  呼び出しごとに、システムプロンプトに最後の1回の添え書きがあったか

    @property
    def _llm_type(self) -> str:
        return "looping"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.last_call_notes.append(LAST_CALL_NOTE in messages[0].content)
        if self.calls <= self.loops:
            msg = call("get_user_conversation_summary", {"line_user_id": "x"}, f"t{self.calls}")
        else:
            msg = reply(["まとめました。"])
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _bounded_server(tmp_path, monkeypatch, model):
    """tests.test_quota の土台に、偽のモデルを差し替える。鍵はダミー、状態と data/ は tmp(_server が向ける)。"""
    monkeypatch.setenv("LINE_SIM_DATA_DIR", str(tmp_path))
    server, _ = _server(tmp_path, monkeypatch, None)
    from functools import partial

    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, in_memory=True))
    return server


def _me(client) -> str:
    client.get("/")
    return client.get("/api/me").json()["user_id"]


def test_a_message_over_the_limit_is_rejected_before_the_model_and_the_quota(tmp_path, monkeypatch):
    model = LengthModel()
    server = _bounded_server(tmp_path, monkeypatch, model)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        uid = _me(client)
        for text in ("あ" * 200_000, "あ" * (turn.MAX_TEXT_CHARS + 1)):
            for path in ("/api/event", "/api/event/stream"):
                r = client.post(path, json={"user_id": uid, "event": "message", "text": text})
                assert r.status_code == 400 and f"{turn.MAX_TEXT_CHARS} 字まで" in r.json()["detail"]
        assert model.lengths == [] and server.app.state.quota.used(uid) == 0
        # 上限ちょうどは受け付ける
        assert _stream(client, uid, "message", "あ" * turn.MAX_TEXT_CHARS)[-1]["bubbles"] == ["はい、どうぞ。"]
    assert model.lengths == [turn.MAX_TEXT_CHARS]


def test_accept_rejects_a_long_message_for_every_channel():
    """LINE の webhook も Web と同じ turn.accept を通る(LINE からは 5000 字を超える発言は届かないが、念のため)。"""
    deps = SimpleNamespace(state=None)  # 同意はテストの既定で「している」扱い(tests/conftest.py)。状態は読まない
    assert turn.accept(deps, "u", "message", "あ" * turn.MAX_TEXT_CHARS, None).text
    with pytest.raises(turn.BadTurn):
        turn.accept(deps, "u", "message", "あ" * (turn.MAX_TEXT_CHARS + 1), None, new_conversation=True)


def test_one_turn_stops_at_the_model_call_limit_with_an_ordinary_reply(tmp_path, monkeypatch):
    model = LoopingModel()
    server = _bounded_server(tmp_path, monkeypatch, model)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        uid = _me(client)
        lines = _stream(client, uid, "message", "渋谷で")
        last = lines[-1]
        assert last["kind"] == "reply" and last["bubbles"] == TURN_LIMIT_REPLY.bubbles and last["cards"] == []
        assert model.calls == MAX_MODEL_CALLS_PER_TURN
        # 最後の1回にだけ、ツールを呼ばずに返信するよう添え書きする
        assert model.last_call_notes == [False] * (MAX_MODEL_CALLS_PER_TURN - 1) + [True]
        # 費用台帳と画面の llm の行は、実際に呼んだ回数だけ(上限で返した返信は LLM を呼んでいない)
        assert len(server.app.state.rt.ledger.rows) == MAX_MODEL_CALLS_PER_TURN
        assert sum(x["kind"] == "llm" for x in lines) == MAX_MODEL_CALLS_PER_TURN
        assert server.app.state.quota.used(uid) == 1
        # 上限の返信は会話の記録に残り、画面に描き直せる(挨拶なしの発言なので会話番号は 0。記録を直接読む)
        rt = server.app.state.rt
        turns = history(client.portal.call(rt.agent.aget_state, rt._cfg(uid, 0)).values["messages"], uid)
        assert turns[-1]["user"] == "渋谷で" and turns[-1]["bot"]["bubbles"] == TURN_LIMIT_REPLY.bubbles

        # 次のターンは数え直す(前のターンの呼び出しを持ち越さない)。次の呼び出しで返信するモデルにする
        model.loops = model.calls
        r = client.post("/api/event", json={"user_id": uid, "event": "message", "text": "もう一度"}).json()
        assert r["bubbles"] == ["まとめました。"] and model.calls == MAX_MODEL_CALLS_PER_TURN + 1


def test_a_turn_with_a_few_tool_calls_is_not_affected(tmp_path, monkeypatch):
    model = LoopingModel(loops=5)  # 評価の記録で、1ターンのツールの呼び出しが一番多かったときの回数
    server = _bounded_server(tmp_path, monkeypatch, model)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        uid = _me(client)
        r = client.post("/api/event", json={"user_id": uid, "event": "message", "text": "渋谷で"}).json()
    assert r["bubbles"] == ["まとめました。"] and model.calls == 6 and not any(model.last_call_notes)
