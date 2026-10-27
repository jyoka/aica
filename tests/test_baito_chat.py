"""アルバイト・パートの相談(バイト探しの相棒。PRD v4.1)を、トークのターンとして通しで動かす。実 LLM なし、dip の MCP は本物。

機能フラグ BAITO_HARNESS_VERSION=v4 のときの、本人から見た流れ: 「まず相談する」→ トークの AI が playbook を読んで相談を
始め、最初の質問(バンクの原文と選択肢)を出す → 選択肢を押すたびに、ハーネスが決めた次の質問がバンクの選択肢つきで返る
(トークの AI は呼ばない。モデルは言い方を 1 回)→ 文で答えたら、モデルに読み取らせてから次へ(抽出 → 言い方)→
「今はいい」でトークの AI に戻り、答えた条件はプロフィールに残る。フラグが無ければ、トークの AI に相談を始めるツールを見せない。
モデル(ハーネスの抽出と言い方)は偽物で、受け取った段・システムプロンプト・メッセージを残す。
"""

from __future__ import annotations

import asyncio
import json
import os
import re

import pytest

from dip_mcp import logic
from dip_mcp.baito import harness as H
from line_runtime.baito_chat import (
    SCHEMAS,
    BaitoLLMError,
    ExtractOut,
    history,
    messages_for,
)
from tests.test_consult import ConsultModel
from tests.test_handle import call, reply

UID = "sim-new"
QM = H.qmap(H.BANK)
Q_MONEY = QM["q_money"]["choices"]
NEXT = re.compile(r"次に聞くこと（JSON）: (\{.*\})")
EMPTY_EXTRACT = {"slot_updates": [], "ng_updates": [], "ng_removals": [], "segment": None, "intent": "answer",
                 "review_ack": None}


@pytest.fixture(autouse=True)
def harness_on(monkeypatch):
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")


class FakeBaitoLLM:
    """呼ばれた順に script の出力(dict か例外)を返す。by_stage があれば、その段の呼び出しには先にそれを順に返す。
    尽きたら、抽出は空、言い方は「いいね！」+ 質問の原文(システムプロンプトの next_question から)、review はカードそのまま、
    結果は候補の上から。"""

    def __init__(self, script=(), by_stage=None):
        self.script = list(script)
        self.by_stage = {k: list(v) for k, v in (by_stage or {}).items()}
        self.calls: list[tuple[str, str, list[dict]]] = []

    async def __call__(self, stage, system, messages):
        self.calls.append((stage, system, [dict(m) for m in messages]))
        if self.by_stage.get(stage):
            out = self.by_stage[stage].pop(0)
        else:
            out = self.script.pop(0) if self.script else self.default(stage, system)
        if isinstance(out, Exception):
            raise out
        return out, {"input_tokens": 10, "output_tokens": 5, "cache_read": 0, "cache_write": 0}

    @staticmethod
    def default(stage, system):
        if stage == "extract":
            return dict(EMPTY_EXTRACT)
        if stage == "result":
            r = json.loads(re.search(r"結果材料（JSON）: (\{.*\})", system).group(1))
            return {"reply": "選んだよ！", "recommendations": [{"job_id": c["job_id"], "reason": "合いそうだから"}
                                                              for c in r["candidates"][:r["need"]]]}
        if stage == "review":
            card = system.split("- カード本文: ", 1)[1].split("\n- カード（JSON）", 1)[0]
            return {"reply": "まとめたよ！\n" + card + "\n\n" + H.CLOSING_JA}
        return {"reply": "いいね！" + json.loads(NEXT.search(system).group(1))["text"]}


def _run(agent_script, baito, steps, tmp_path, after=None):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=ConsultModel(script=agent_script), in_memory=True, baito_llm=baito) as rt:
            out = [await rt.handle(*s) for s in steps]
            return out, (await after(rt) if after else None)

    return asyncio.run(go())


def _harness():
    from dip_mcp.baito.session import KEY
    from dip_mcp.data import State

    d = State().user(UID).get(KEY)
    return H.State.from_dict(d["harness"]) if d else None


GREET = reply(["お仕事探しの相談と、求人のご提案をします。", "どれから始めますか?"],
              quick_replies=["求人を探す", "まず相談する", "今はいい"])
START = [
    call("get_counseling_playbook", {"track": "アルバイト・パート", "channel": "chat"}, "pb"),
    call("start_baito_consultation", {"line_user_id": UID}),
    # モデルが付けた選択肢(言い換えたもの)は使われず、ハーネスの最初の質問の選択肢になる
    reply(["アルバイトで何が向いているか、一緒に整理しましょう。", "バイト代、何に使いたい？"], quick_replies=["趣味", "貯金"]),
]


def test_consultation_is_run_by_the_harness_after_the_agent_starts_it(tmp_path):
    baito = FakeBaitoLLM([
        {"reply": "推し活いいね！今のあなたに近いのは？"},                  # 選択肢を押した: 言い方の1回だけ
        EMPTY_EXTRACT | {"segment": "student",                         # 文で答えた: 抽出(年齢は根拠つき)
                         "slot_updates": [{"slot": "age_band", "value": "高1", "evidence": "高1"}]},
        {"reply": "高1なんだ！学校のバイトのルールは？"},                  # 言い方
    ])

    async def after(rt):
        return await rt.conversation(UID)

    (_, r1, r2, r3), conv = _run([GREET, *START], baito, [(UID, "visit", None), (UID, "message", "まず相談する"),
                                                         (UID, "message", "推し活・趣味"), (UID, "message", "高1です！")],
                                 tmp_path, after)
    # 1ターン目: トークの AI が playbook(ハーネス版)を読んで始め、最初の質問の選択肢はバンクの原文
    assert [t["name"] for t in r1["tools"]] == ["get_counseling_playbook", "start_baito_consultation"]
    assert "start_baito_consultation" in r1["tools"][0]["result"]["playbook"]
    assert r1["tools"][1]["result"]["question"]["text"] == "バイト代、何に使いたい？"
    assert r1["quick_replies"] == Q_MONEY and "baito" not in r1
    # 2ターン目: タップ → ハーネスの次の質問(q_segment)。トークの AI は呼ばない(台本は使い切っている)。モデルは言い方を1回
    assert r2["bubbles"] == ["推し活いいね！今のあなたに近いのは？"]
    assert r2["quick_replies"] == QM["q_segment"]["choices"]
    assert r2["baito"] == {"phase": "icebreak", "question": "q_segment"} and r2["tools"] == [] and r2["cards"] == []
    # 3ターン目: 文 → 抽出(本人の今の発言だけを渡す)→ 言い方(次の質問 hf_school)
    assert [c[0] for c in baito.calls] == ["say", "extract", "say"]
    _, extract_system, extract_msgs = baito.calls[1]
    assert "ユーザーの発言: 高1です！" in extract_system and '"q_segment"' not in extract_system
    assert extract_msgs == [{"role": "user", "content": "高1です！"}]
    assert '"id": "hf_school"' in baito.calls[2][1] and "- segment: student（学生）" in baito.calls[2][1]
    # 言い方には直前のやりとりと本人の発言が渡る(リアクションのため)
    assert baito.calls[2][2][-1] == {"role": "user", "content": "高1です！"}
    assert baito.calls[2][2][-2]["role"] == "assistant" and "今のあなたに近いのは？" in baito.calls[2][2][-2]["content"]
    # 未成年の注意文はハーネスが返事の前に付ける。年齢は文から(high)、深夜 NG はコードが付ける
    assert r3["bubbles"] == [H.NOTICE["minor_rules"] + "\n高1なんだ！学校のバイトのルールは？"]
    assert r3["baito"] == {"phase": "hard_filter", "question": "hf_school"}
    assert r3["quick_replies"] == QM["hf_school"]["choices"]
    h = _harness()
    assert h.slots["age_band"]["value"] == "15_17" and "深夜シフト" in h.derived_ng
    # 画面の描き直し: ハーネスのターンも、本人の発言と返信(選択肢つき)で残る
    assert [t["user"] for t in conv["turns"]] == [None, "まず相談する", "推し活・趣味", "高1です！"]
    assert conv["turns"][1]["bot"]["quick_replies"] == Q_MONEY
    assert conv["turns"][2]["bot"]["quick_replies"] == QM["q_segment"]["choices"]


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
    assert r3["quick_replies"] == ["中学生以下", "15〜17歳", "18歳以上", "ひみつ"]
    assert "baito" not in r4 and r4["bubbles"] == ["わかりました。また相談したくなったら声をかけてください。"]
    assert len(baito.calls) == 2  # 「今はいい」ではハーネスのモデルを呼ばない
    assert _harness() is None
    assert [t["user"] for t in conv["turns"]] == [None, "まず相談する", "旅行・遊び", "学生", "今はいい"]
    # トークの AI は、ハーネスのターンも会話の文脈として受け取る(「今はいい」の前に、ハーネスの質問と答えがある)
    agent_saw = [m for m in seen_messages if m.type in ("human", "ai")]
    assert [m.content for m in agent_saw if m.type == "human"][-3:] == ["旅行・遊び", "学生", "今はいい"]


def test_failed_or_unsafe_reply_falls_back_to_the_question_text(tmp_path):
    """モデルが返事を作れなかったら(API のエラー)、扶養の金額を書いたら(PRD §10-3)、1 回だけやり直し、だめなら
    ハーネスが進めた次の質問の原文を出す(本人が見ていない質問に答える形にずらさない)。"""
    baito = FakeBaitoLLM([RuntimeError("overloaded"), RuntimeError("overloaded"),
                          {"reply": "扶養なら103万円までだよ！年齢は？"}, {"reply": "扶養は130万の壁があるよ"}])
    (_, _, r2, r3), _ = _run([GREET, *START], baito, [(UID, "visit", None), (UID, "message", "まず相談する"),
                                                     (UID, "message", "貯金・将来のため"), (UID, "message", "学生")],
                             tmp_path)
    assert r2["bubbles"] == ["今のあなたに近いのは？"] and r2["quick_replies"] == QM["q_segment"]["choices"]
    assert r3["bubbles"] == [QM["hf_age"]["text"]] and r3["quick_replies"] == QM["hf_age"]["choices"]
    assert [c[0] for c in baito.calls] == ["say"] * 4


def test_malformed_model_output_is_still_charged_to_the_ledger(tmp_path):
    """モデルが形の崩れた出力を返しても(構造化出力の失敗)、使った量は費用台帳に入る。返事は質問の原文。"""
    bad = {"input_tokens": 70, "output_tokens": 30, "cache_read": 0, "cache_write": 0}
    baito = FakeBaitoLLM([BaitoLLMError(bad), BaitoLLMError(bad)])

    async def after(rt):
        return [r for r in rt.ledger.rows if r["input_tokens"] == 70]

    (_, _, r2), rows = _run([GREET, *START], baito, [(UID, "visit", None), (UID, "message", "まず相談する"),
                                                    (UID, "message", "貯金・将来のため")], tmp_path, after)
    assert r2["bubbles"] == ["今のあなたに近いのは？"] and len(rows) == 2


def test_junior_high_free_text_is_stopped_even_when_the_model_fails(tmp_path):
    """レビュー M-4 を本人から見た形で: 年齢の質問に「中2です」と文で答え、抽出のモデルが 2 回とも失敗しても、
    おすすめを出さずに止める。相談は閉じず、トークの AI にも戻らない(求人を出さない)。"""
    baito = FakeBaitoLLM(by_stage={"extract": [RuntimeError("timeout"), RuntimeError("timeout")]})
    steps = [(UID, "visit", None), (UID, "message", "まず相談する"), (UID, "message", "旅行・遊び"),
             (UID, "message", "学生"), (UID, "message", "中2です"), (UID, "message", "じゃあ求人を見せて")]
    (*_, r5, r6), _ = _run([GREET, *START], baito, steps, tmp_path)
    assert [c[0] for c in baito.calls][-2:] == ["extract", "extract"]  # 「中2です」の抽出が 2 回とも失敗した
    assert r5["bubbles"] == [H.NOTICE["under_15"] + "\n" + H.NOTICE["rules_only"]]
    assert r5["quick_replies"] == [] and r5["cards"] == []
    assert r5["baito"] == {"phase": "result", "question": "result"}
    h = _harness()
    assert h.blocked == "under_15" and h.result["recommendations"] == []
    # 止めた後の発言もハーネスが受ける(「求人を見せて」でもトークの AI に戻さない。年齢の訂正でなければ結果の文を繰り返す)
    assert r6["bubbles"][0].startswith(H.POST_RESULT_TEXT) and r6["tools"] == []


def test_minor_reaches_the_result_by_buttons_and_the_rule_ng_is_saved(tmp_path):
    """15〜17歳を選んで最後まで進むと、おすすめ 3 つ(深夜専用・18歳以上・重労働なし)が出て、相談を閉じ、
    避けたいことに「深夜シフト」「重量物・危険な作業」が保存される。深夜の選択肢は一度も出ない。"""
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from dip_mcp.data import State
    from line_runtime.runtime import LineRuntime

    picks = {"q_segment": "学生", "hf_age": "15〜17歳"}

    async def go():
        async with LineRuntime(model=ConsultModel(script=[GREET, *START]), in_memory=True, baito_llm=FakeBaitoLLM()) as rt:
            await rt.handle(UID, "visit", None)
            r = await rt.handle(UID, "message", "まず相談する")
            seen = []
            for _ in range(20):
                q = (r.get("baito") or {}).get("question") or "q_money"
                if q == "result":
                    return r, seen
                seen.append(r["quick_replies"])
                text = H.REVIEW_OK if q == "review" else picks.get(q) or r["quick_replies"][0]
                r = await rt.handle(UID, "message", text)
            raise AssertionError("結果に着かない")

    r, seen = asyncio.run(go())
    assert not any("深夜もOK" in qs for qs in seen)
    assert r["quick_replies"] == ["求人を見る", "今はいい"] and "1. " in r["bubbles"][0] and "3. " in r["bubbles"][0]
    for name in (H.CATALOG_BY_ID[j]["name"] for j in ("izakaya", "moving", "nursery_assist", "reception")):
        assert name not in r["bubbles"][0]
    assert _harness() is None
    avoids = State().user(UID)["preferences"]["avoids"]["value"]
    assert "深夜シフト" in avoids and "重量物・危険な作業" in avoids


def test_agent_sees_the_start_tool_only_with_the_flag(tmp_path, monkeypatch):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=ConsultModel(script=[]), in_memory=True) as rt:
            return {t.name for t in rt.tools}, set(rt._dip_tools)

    agent_tools, all_tools = asyncio.run(go())
    assert "start_baito_consultation" in agent_tools
    assert {"baito_turn", "end_baito_consultation"} <= all_tools - agent_tools
    monkeypatch.delenv(logic.BAITO_HARNESS_FLAG)  # 本番の既定: 今までどおり playbook で相談する
    agent_tools, all_tools = asyncio.run(go())
    assert "start_baito_consultation" not in agent_tools and "start_baito_consultation" in all_tools


def test_a_new_conversation_closes_an_unfinished_consultation(tmp_path):
    agent = [*START, reply(["こんにちは。"], quick_replies=["求人を探す", "まず相談する", "今はいい"])]

    async def after(rt):
        return _harness()

    rs, still_open = _run(agent, FakeBaitoLLM(), [(UID, "message", "まず相談する"), (UID, "visit", None)], tmp_path, after)
    assert rs[1]["bubbles"] == ["こんにちは。"] and still_open is None


def test_history_for_the_model_is_alternating_user_and_bubbles():
    from langchain_core.messages import AIMessage, HumanMessage

    msgs = [HumanMessage("[イベント: 初回訪問]"), AIMessage(json.dumps({"bubbles": ["こんにちは", "どうしますか?"]})),
            HumanMessage("x", additional_kwargs={"profile_notice": True}), HumanMessage("まず相談する"),
            call("start_baito_consultation", {"line_user_id": UID}),
            AIMessage(json.dumps({"bubbles": ["バイト代、何に使いたい？"]}))]
    convo = history(msgs)
    assert convo == [{"role": "user", "content": "[イベント: 初回訪問]"},
                     {"role": "assistant", "content": "こんにちは\nどうしますか?"},
                     {"role": "user", "content": "まず相談する"},
                     {"role": "assistant", "content": "バイト代、何に使いたい？"}]
    # 抽出は今の発言だけ、言い方はやりとりの後に今の発言を足す
    assert messages_for("extract", "旅行", convo) == [{"role": "user", "content": "旅行"}]
    assert messages_for("say", "旅行", convo) == [*convo, {"role": "user", "content": "旅行"}]


def test_model_output_schemas_are_the_prd_json_contracts():
    """抽出は PRD §8-5 の 6 つのキー(confidence と from は出させない)、生成は {reply}、結果は §8-7。"""
    schema = ExtractOut.model_json_schema()
    assert set(schema["properties"]) == {"slot_updates", "ng_updates", "ng_removals", "segment", "intent", "review_ack"}
    assert set(schema["$defs"]["SlotUpdate"]["properties"]) == {"slot", "value", "evidence"}
    assert set(SCHEMAS["say"].model_json_schema()["properties"]) == {"reply"} and SCHEMAS["review"] is SCHEMAS["say"]
    assert set(SCHEMAS["result"].model_json_schema()["properties"]) == {"reply", "recommendations"}
    # モデルの出力はハーネスの検証をそのまま通る
    out = ExtractOut.model_validate(EMPTY_EXTRACT | {"slot_updates": [{"slot": "purpose", "value": "推し活・趣味",
                                                                        "evidence": "推し活"}]}).model_dump()
    clean, probs = H.validate_extract(out)
    assert clean["slot_updates"] == [{"slot": "purpose", "value": ["hobby"], "evidence": "推し活"}] and probs == ["confidence_missing"]
