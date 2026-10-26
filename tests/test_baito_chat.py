"""アルバイト・パートの相談(バイト探しの相棒)を、トークのターンとして通しで動かす。実 LLM なし、dip の MCP は本物。

本人から見た流れ: 「まず相談する」→ トークの AI が相談を始め、最初の質問(バンクの原文と選択肢)を出す →
選択肢を押すたびに、ハーネスが決めた次の質問が、バンクの選択肢つきで返る(トークの AI は呼ばない)→
文で答えたら、モデルに読み取らせてから次へ →「やめる」でトークの AI に戻り、答えた条件はプロフィールに残る。
モデル(ハーネスの言い方)は偽物で、受け取ったシステムプロンプトを残す。
"""

from __future__ import annotations

import asyncio
import json
import os

from line_runtime.baito_chat import BaitoOutput, history
from tests.test_consult import ConsultModel
from tests.test_handle import call, reply

UID = "sim-new"
Q_MONEY = ["推し活・趣味", "貯金・将来のため", "生活費・家計の足し", "旅行・遊び"]


class FakeBaitoLLM:
    """呼ばれた順に script の JSON を返す(空なら決まった返事)。受け取ったシステムプロンプトとメッセージを残す。"""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls: list[tuple[str, list[dict]]] = []

    async def __call__(self, system, messages):
        self.calls.append((system, [dict(m) for m in messages]))
        out = self.script.pop(0) if self.script else {"reply": "いいね！次はこれ！"}
        if isinstance(out, Exception):
            raise out
        base = {"choices": [], "slot_updates": [], "ng_updates": [], "segment": "unknown"}
        return BaitoOutput.model_validate(base | out), {"input_tokens": 10, "output_tokens": 5, "cache_read": 0,
                                                        "cache_write": 0}


def _run(agent_script, baito, steps, tmp_path, after=None):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=ConsultModel(script=agent_script), in_memory=True, baito_llm=baito) as rt:
            out = [await rt.handle(*s) for s in steps]
            return out, (await after(rt) if after else None)

    return asyncio.run(go())


GREET = reply(["お仕事探しの相談と、求人のご提案をします。", "どれから始めますか?"],
              quick_replies=["求人を探す", "まず相談する", "今はいい"])
START = [
    call("start_baito_consultation", {"line_user_id": UID}),
    # モデルが付けた選択肢(言い換えたもの)は使われず、ハーネスの最初の質問の選択肢になる
    reply(["アルバイトで何が向いているか、一緒に整理しましょう。", "バイト代、何に使いたい？"], quick_replies=["趣味", "貯金"]),
]


def test_consultation_is_run_by_the_harness_after_the_agent_starts_it(tmp_path):
    baito = FakeBaitoLLM([
        {"reply": "推し活いいね！今のあなたに近いのは？"},                  # 選択肢を押した: 言い方の1回だけ
        {"reply": "", "segment": "student",                             # 文で答えた: 読み取り
         "slot_updates": [{"slot": "age_under_18", "value": True, "confidence": "high", "from": "q_segment"}]},
        {"reply": "高1なんだ！学校のバイトのルールは？",                  # 言い方(この slot_updates は使わない)
         "slot_updates": [{"slot": "commute", "value": "train_60", "confidence": "high", "from": "x"}]},
    ])

    async def after(rt):
        return await rt.conversation(UID)

    (_, r1, r2, r3), conv = _run([GREET, *START], baito, [(UID, "visit", None), (UID, "message", "まず相談する"),
                                                         (UID, "message", "推し活・趣味"), (UID, "message", "高1です！")],
                                 tmp_path, after)
    # 1ターン目: トークの AI が始め、最初の質問の選択肢はバンクの原文
    assert [t["name"] for t in r1["tools"]] == ["start_baito_consultation"]
    assert r1["tools"][0]["result"]["question"]["text"] == "バイト代、何に使いたい？"
    assert r1["quick_replies"] == Q_MONEY and "baito" not in r1
    # 2ターン目: タップ → ハーネスの次の質問(q_segment)。トークの AI は呼ばない(台本は 2 つで使い切っている)
    assert r2["bubbles"] == ["推し活いいね！今のあなたに近いのは？"]
    assert r2["quick_replies"] == ["学生", "家のこと中心", "本業あり副業探し"]
    assert r2["baito"] == {"phase": "icebreak", "question": "q_segment"} and r2["tools"] == [] and r2["cards"] == []
    # 3ターン目: 文 → 読み取り(q_segment のプロンプト)→ 言い方(次の質問 hf_school のプロンプト)
    assert len(baito.calls) == 3
    extract_system, phrase_system = baito.calls[1][0], baito.calls[2][0]
    assert '"id": "q_segment"' in extract_system and '"id": "hf_school"' in phrase_system
    assert "- フェーズ: hard_filter\n- ユーザーの属性: student\n" in phrase_system
    assert r3["baito"] == {"phase": "hard_filter", "question": "hf_school"}
    assert r3["quick_replies"] == ["OK", "届け出すればOK", "禁止 or わからない"]
    # 本人の発言と前の返信が、モデルに渡る(リアクションのため)
    assert baito.calls[2][1][-1] == {"role": "user", "content": "高1です！"}
    assert baito.calls[2][1][-2]["role"] == "assistant" and "今のあなたに近いのは？" in baito.calls[2][1][-2]["content"]
    # 年齢は文から(high)、未成年の深夜 NG はコードが付ける。言い方の回の slot_updates(commute)は使わない
    from dip_mcp.baito import harness as H
    from dip_mcp.baito.session import KEY
    from dip_mcp.data import State

    h = H.State.from_dict(State().user(UID)[KEY]["harness"])
    assert h.slots["age_under_18"]["value"] is True and h.ng == [H.LATE_NIGHT_NG] and "commute" not in h.slots
    # 画面の描き直し: ハーネスのターンも、本人の発言と返信(選択肢つき)で残る
    assert [t["user"] for t in conv["turns"]] == [None, "まず相談する", "推し活・趣味", "高1です！"]
    assert conv["turns"][1]["bot"]["quick_replies"] == Q_MONEY
    assert conv["turns"][2]["bot"]["quick_replies"] == ["学生", "家のこと中心", "本業あり副業探し"]
    assert conv["turns"][3]["bot"]["bubbles"] == ["高1なんだ！学校のバイトのルールは？"]


def test_exit_word_returns_the_turn_to_the_agent_with_the_harness_turns_in_context(tmp_path):
    agent = [GREET, *START, reply(["わかりました。また相談したくなったら声をかけてください。"], quick_replies=["求人を探す"])]
    baito = FakeBaitoLLM()

    seen_messages = []

    async def after(rt):
        seen_messages.extend((await rt.agent.aget_state(rt._cfg(UID, await rt.current_thread(UID)))).values["messages"])
        return await rt.conversation(UID)

    (_, _, r2, r3, r4), conv = _run(agent, baito, [(UID, "visit", None), (UID, "message", "まず相談する"),
                                                   (UID, "message", "旅行・遊び"), (UID, "message", "学生"),
                                                   (UID, "message", "今はいい")], tmp_path, after)
    assert r2["baito"]["question"] == "q_segment" and r3["baito"]["question"] == "hf_age"
    assert "baito" not in r4 and r4["bubbles"] == ["わかりました。また相談したくなったら声をかけてください。"]
    assert len(baito.calls) == 2  # 「今はいい」ではハーネスのモデルを呼ばない
    from dip_mcp.baito.session import KEY
    from dip_mcp.data import State

    assert KEY not in State().user(UID)
    assert [t["user"] for t in conv["turns"]] == [None, "まず相談する", "旅行・遊び", "学生", "今はいい"]
    assert conv["turns"][3]["bot"]["quick_replies"] == ["はい", "いいえ"]
    # トークの AI は、ハーネスのターンも会話の文脈として受け取る(「今はいい」の前に、ハーネスの質問と答えがある)
    agent_saw = [m for m in seen_messages if m.type in ("human", "ai")]
    assert [m.content for m in agent_saw if m.type == "human"][-3:] == ["旅行・遊び", "学生", "今はいい"]


def test_failed_or_empty_reply_falls_back_to_the_question_text(tmp_path):
    """返信の文を作れなかったら(API のエラー・空の文)か、扶養の金額を示したら(PRD §10)、ハーネスが進めた次の質問の
    原文を出す(本人が見ていない質問に答える形にずらさない)。"""
    baito = FakeBaitoLLM([RuntimeError("overloaded"), {"reply": "扶養なら103万円までだよ！18歳未満？"}])
    (_, _, r2, r3), _ = _run([GREET, *START], baito, [(UID, "visit", None), (UID, "message", "まず相談する"),
                                                     (UID, "message", "貯金・将来のため"), (UID, "message", "学生")],
                             tmp_path)
    assert r2["bubbles"] == ["今のあなたに近いのは？"] and r2["quick_replies"] == ["学生", "家のこと中心", "本業あり副業探し"]
    assert r3["bubbles"] == ["今、18歳未満？"] and r3["quick_replies"] == ["はい", "いいえ"]


def test_agent_never_sees_the_runtime_only_tools(tmp_path):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=ConsultModel(script=[]), in_memory=True) as rt:
            return {t.name for t in rt.tools}, set(rt._dip_tools)

    agent_tools, all_tools = asyncio.run(go())
    assert "start_baito_consultation" in agent_tools
    assert {"baito_turn", "end_baito_consultation"} <= all_tools - agent_tools


def test_a_new_conversation_closes_an_unfinished_consultation(tmp_path):
    agent = [*START, reply(["こんにちは。"], quick_replies=["求人を探す", "まず相談する", "今はいい"])]

    async def after(rt):
        from dip_mcp.baito.session import KEY
        from dip_mcp.data import State

        return KEY in State().user(UID)

    rs, still_open = _run(agent, FakeBaitoLLM(), [(UID, "message", "まず相談する"), (UID, "visit", None)], tmp_path, after)
    assert rs[1]["bubbles"] == ["こんにちは。"] and still_open is False


def test_history_for_the_model_is_alternating_user_and_bubbles():
    from langchain_core.messages import AIMessage, HumanMessage

    msgs = [HumanMessage("[イベント: 初回訪問]"), AIMessage(json.dumps({"bubbles": ["こんにちは", "どうしますか?"]})),
            HumanMessage("x", additional_kwargs={"profile_notice": True}), HumanMessage("まず相談する"),
            call("start_baito_consultation", {"line_user_id": UID}),
            AIMessage(json.dumps({"bubbles": ["バイト代、何に使いたい？"]}))]
    assert history(msgs) == [{"role": "user", "content": "[イベント: 初回訪問]"},
                             {"role": "assistant", "content": "こんにちは\nどうしますか?"},
                             {"role": "user", "content": "まず相談する"},
                             {"role": "assistant", "content": "バイト代、何に使いたい？"}]


def test_model_output_schema_is_the_prd_json_contract():
    """PRD §8.7 の5つのキー(と §12-3 の任意の ng_removals)。slot_updates の "from" はそのままの名前で出す。"""
    schema = BaitoOutput.model_json_schema(by_alias=True)
    assert set(schema["required"]) == {"reply", "choices", "slot_updates", "ng_updates", "segment"}
    assert set(schema["properties"]) == {"reply", "choices", "slot_updates", "ng_updates", "segment", "ng_removals"}
    out = BaitoOutput.model_validate_json(json.dumps({
        "reply": "OK", "choices": [], "ng_updates": [], "segment": "student",
        "slot_updates": [{"slot": "shift_length", "value": "4時間くらい", "confidence": "high", "from": "review"}]},
        ensure_ascii=False))
    assert out.dump()["slot_updates"] == [{"slot": "shift_length", "value": "4時間くらい", "confidence": "high", "from": "review"}]
