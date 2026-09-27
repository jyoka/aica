"""相談モード(何から始めればいいか分からない人向け)の台本テスト。実 LLM なし、dip の MCP は本物。

相談モードは別のエージェントではなく、dip の Prompt のステージ指針と入口の選択肢で動く。ここでは
(1) dip の Prompt が LINE 側エージェントに届いていること、(2) 台本どおりに進めた会話が評価の機械判定を通ること、
(3) 相談で話した内容が dip の長期メモリに残り、次の会話の要約に出ること、(4) 判定が悪い振る舞いを捕まえること、を確かめる。
"""

from __future__ import annotations

import asyncio
import os

from evals.checks import conversation_checks
from evals.scenarios import load_scenarios
from line_runtime.runtime import _tool_payload
from tests.test_handle import ScriptedModel, call, reply

SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
MENU = ["求人を探す", "まず相談する", "応募履歴を確認", "今はいい"]
CLOSING = ["求人を探す", "応募履歴を確認", "使い方"]


class ConsultModel(ScriptedModel):
    """ScriptedModel に加えて、台本の要素に関数を置けば、それまでのメッセージから返事を作る
    (dip が返した求人の ID をカードにするため)。受け取ったシステムプロンプトも残す。"""

    systems: list = []  # noqa: RUF012  pydantic の項目

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if messages and messages[0].type == "system":
            self.systems.append(messages[0].content)
        if callable(self.script[0]):
            self.script[0] = self.script[0](messages)
        return super()._generate(messages, stop, run_manager, **kwargs)


def returned_jobs(messages) -> list[str]:
    """直近の recommend_jobs が返した求人 ID(最大3件)。"""
    tool = next(m for m in reversed(messages) if m.type == "tool" and m.name == "recommend_jobs")
    return [j["job_id"] for j in _tool_payload(tool)["jobs"]][:3]


def calls(*items) -> object:
    """並行ツール呼び出し(1つの AIMessage に複数)。items は (name, args)。"""
    msg = call(*items[0], "c0")
    msg.tool_calls = [{"name": n, "args": a, "id": f"c{i}", "type": "tool_call"} for i, (n, a) in enumerate(items)]
    return msg


def run(model, steps, tmp_path):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            return [await rt.handle(*s) for s in steps]

    return asyncio.run(go())


def run_eval_scenario(model, sc, tmp_path) -> dict:
    """評価ハーネス(evals.run.run_scenario)で、台本のあるシナリオを最後まで回す(模擬ユーザーの LLM は呼ばない)。"""
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from evals.meter import Meter
    from evals.run import run_scenario
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            return await run_scenario(rt, None, Meter(), sc, use_judge=False)

    return asyncio.run(go())


def stated(tmp_path, user: str) -> dict:
    from dip_mcp import logic
    from dip_mcp.data import State

    return {k: v["value"] for k, v in logic.stated_preferences(State(tmp_path / "state.json").user(user)).items()}


# ----------------------------- dip の Prompt(相談モードの指針と入口) -----------------------------


def test_consult_mode_prompt_reaches_the_agent_on_first_visit(tmp_path):
    model = ConsultModel(script=[reply(["はじめまして。バイト探しをお手伝いします。どうしますか?"], quick_replies=MENU)])
    (r,) = run(model, [("sim-new", "visit", None)], tmp_path)
    system = model.systems[0]
    assert "## 相談モード" in system and "「まず相談する」" in system
    assert "求人を探す / まず相談する / 応募履歴を確認 / 今はいい" in system  # 初回挨拶の選択肢
    assert "相談モード中は「ユーザーの3回目の返信までに分岐」の規則を適用しない" in system
    assert "まず相談する" in r["quick_replies"]


# ----------------------------- 入口 → ヒアリング → 提案(3ターンで打ち切られない) -----------------------------


def test_consult_from_menu_hears_one_by_one_saves_and_proposes_without_reasking(tmp_path):
    """「まず相談する」→ 経験・興味・不安・生活の制約を1問ずつ → まとめて提案。3ターンを超えても締めない。
    相談で話したことは dip に保存され、提案の条件にそのまま使われ、次の会話の要約にも出る。"""
    uid = "sim-new"
    model = ConsultModel(script=[
        reply(["はじめまして。バイト探しをお手伝いします。どうしますか?"], quick_replies=MENU),
        reply(["いくつか伺いながら一緒に整理します。", "これまでにアルバイトの経験はありますか?"],
              quick_replies=["未経験です", "飲食の経験あり", "販売の経験あり", "今はいい"]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"experience": "未経験"}}),
        reply(["ありがとうございます。", "やってみたいことや好きなことはありますか?"],
              quick_replies=["人と話すこと", "体を動かすこと", "黙々と作業", "今はいい"]),
        reply(["人と話すのがお好きなのですね。", "働くうえで気になることはありますか?"],
              quick_replies=["未経験で大丈夫か", "シフト", "特にない", "今はいい"]),
        reply(["未経験が不安なのですね。", "働ける時間帯とエリアはどのあたりですか?"],
              quick_replies=["平日の夕方", "土日の日中", "それ以外", "今はいい"]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {
            "time_band": "夕方", "area": "池袋", "days_per_week": "週3日"}}),
        reply(["未経験・人と話すのが好き・池袋の夕方で整理しました。", "この内容で求人を見ますか?"],
              quick_replies=["求人を見る", "もう少し相談したい", "今はいい"]),
        calls(("recommend_jobs", {"line_user_id": uid, "area": "池袋", "time_band": "夕方"}),
              ("record_conversation_outcome", {"line_user_id": uid, "outcome": "propose",
                                               "reason": "相談のあと求人を見る", "confidence": 0.8})),
        lambda msgs: reply(["池袋・夕方の求人です。"], job_ids=returned_jobs(msgs), quick_replies=CLOSING),
        reply(["またいつでもご相談ください。"], quick_replies=CLOSING),
        # 次の会話(再訪): 要約を読む
        call("get_user_conversation_summary", {"line_user_id": uid}),
        reply(["前回は池袋の夕方でしたが、今回も同じ感じで探しますか?"], quick_replies=["同じで探す", "変えたい", "今はいい"]),
    ])
    said = [None, "まず相談する", "バイトは未経験です", "人と話すのが好きです", "未経験でついていけるか不安",
            "平日の夕方、池袋あたりで週3日", "求人を見る", "ありがとう、考えます"]
    steps = [(uid, "visit", None)] + [(uid, "message", m) for m in said[1:]] + [(uid, "revisit", None)]
    *turns, again = run(model, steps, tmp_path)

    sc = {"expect_outcome": ["propose"], "expect_menu": True, "consult_mode": True, "expect_consult_menu": True,
          "expect_saved_preferences": ["experience", "time_band"]}
    checks = conversation_checks(sc, turns, said)
    assert checks["violations"] == []
    assert checks["branch_turn"] == 7  # ヒアリングの間(ユーザーの返信5回)は締めずに1問ずつ聞いた
    assert checks["branch_within_3"] is None  # 相談モードでは「3ターン以内に分岐」を測らない
    assert all(len([b for b in r["bubbles"] if "?" in b or "?" in b]) == 1 for r in turns[:6])  # 毎ターン1問
    assert checks["outcomes"] == ["propose"] and turns[6]["cards"]

    # 相談で話したことが長期メモリに残り、提案の条件にそのまま使われた(言い直し・聞き直しなし)
    assert stated(tmp_path, uid) == {"experience": "未経験", "time_band": "夕方", "area": "池袋", "days_per_week": "週3日"}
    rec = next(t for t in turns[6]["tools"] if t["name"] == "recommend_jobs")
    assert (rec["args"]["area"], rec["args"]["time_band"]) == ("池袋", "夕方")
    # 次の会話では要約の stated_preferences として返る
    summary = again["tools"][0]["result"]
    assert summary["stated_preferences"]["experience"]["value"] == "未経験"


def test_consult_starts_from_a_sign_of_being_lost_and_suggests_job_types(tmp_path):
    """迷いのサイン(「何から始めれば」)から相談に入り、「自分に合うのが分からない」には職種を理由つきで挙げる。"""
    uid = "sim-new"
    model = ConsultModel(script=[
        reply(["はじめまして。バイト探しをお手伝いします。どうしますか?"], quick_replies=MENU),
        reply(["いくつか伺いながら一緒に整理します。", "これまでにアルバイトの経験はありますか?"],
              quick_replies=["未経験です", "飲食の経験あり", "それ以外", "今はいい"]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"experience": "コンビニで半年"}}),
        reply(["ありがとうございます。", "やってみたいことや好きなことはありますか?"],
              quick_replies=["人と話すこと", "黙々と作業", "わからない", "今はいい"]),
        reply(["コンビニの経験と、人と話すのが好きな点から、スーパー・販売やカフェ、イベントスタッフが近そうです。",
               "どれが気になりますか?"], quick_replies=["スーパー・販売", "カフェ", "イベントスタッフ", "今はいい"]),
    ])
    said = [None, "バイトを探したいけど、何から始めればいいか分からない", "コンビニで半年やっていました",
            "人と話すのは好き。でも自分に合うのが分からない"]
    turns = run(model, [(uid, "visit", None)] + [(uid, "message", m) for m in said[1:]], tmp_path)
    checks = conversation_checks({"expect_outcome": ["consult"], "consult_mode": True}, turns, said)
    # 会話はまだ途中なので、出口の記録が無いことだけが期待と違う
    assert checks["violations"] == [] and checks["outcomes"] == []
    assert turns[1]["cards"] == [] and "?" in turns[1]["bubbles"][-1]  # 入口では求人を出さずに1問目
    assert stated(tmp_path, uid) == {"experience": "コンビニで半年"}


# ----------------------------- 出口: 相談だけで終える / 途中で提案へ切り替える(評価シナリオの台本) -----------------------------


def test_consult_only_scenario_ends_on_ima_wa_ii_without_proposing(tmp_path):
    sc = SCENARIOS["C-consult-only"]
    uid = sc["user"]
    model = ConsultModel(script=[
        reply(["はじめまして。バイト探しをお手伝いします。どうしますか?"], quick_replies=MENU),
        reply(["いくつか伺いながら一緒に整理します。", "これまでにアルバイトの経験はありますか?"],
              quick_replies=["未経験です", "飲食の経験あり", "それ以外", "今はいい"]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"experience": "未経験"}}),
        reply(["ありがとうございます。", "やってみたいことや好きなことはありますか?"],
              quick_replies=["人と話すこと", "体を動かすこと", "黙々と作業", "今はいい"]),
        call("record_conversation_outcome", {"line_user_id": uid, "outcome": "consult", "reason": "相談のみ",
                                             "confidence": 0.7}),
        reply(["承知しました。またいつでもご相談ください。"], quick_replies=["求人を探す", "使い方"]),
    ])
    r = run_eval_scenario(model, sc, tmp_path)
    assert r["violations"] == [] and r["outcome_as_expected"] and r["branch_within_3"] is None
    assert r["tools"][-1] == ["record_conversation_outcome"]  # 提案(recommend_jobs)なしで終えた
    assert stated(tmp_path, uid) == {"experience": "未経験"}


def test_consult_switch_scenario_proposes_right_away_on_show_me_jobs(tmp_path):
    sc = SCENARIOS["C-consult-switch"]
    uid = sc["user"]
    model = ConsultModel(script=[
        reply(["はじめまして。バイト探しをお手伝いします。どうしますか?"], quick_replies=MENU),
        reply(["いくつか伺いながら一緒に整理します。", "これまでにアルバイトの経験はありますか?"],
              quick_replies=["未経験です", "飲食の経験あり", "それ以外", "今はいい"]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"experience": "居酒屋のホールを1年"}}),
        reply(["ありがとうございます。", "やってみたいことや好きなことはありますか?"],
              quick_replies=["人と話すこと", "体を動かすこと", "黙々と作業", "今はいい"]),
        calls(("recommend_jobs", {"line_user_id": uid, "job_type": "飲食ホール"}),
              ("record_conversation_outcome", {"line_user_id": uid, "outcome": "propose",
                                               "reason": "求人を見せて", "confidence": 0.8})),
        lambda msgs: reply(["ホールの経験を活かせそうな求人です。"], job_ids=returned_jobs(msgs), quick_replies=CLOSING),
        reply(["またいつでもご相談ください。"], quick_replies=CLOSING),
    ])
    r = run_eval_scenario(model, sc, tmp_path)
    assert r["violations"] == [] and r["outcome_as_expected"]
    assert r["tools"][3] == ["recommend_jobs", "record_conversation_outcome"]  # 「求人を見せて」のターンで提案


# ----------------------------- 判定が悪い振る舞いを捕まえる -----------------------------


def turn(bubbles, tools=(), cards=(), quick_replies=("今はいい",)):
    return {"bubbles": list(bubbles), "quick_replies": list(quick_replies), "cards": list(cards), "buttons": [],
            "tools": [{"name": n, "args": a, "result": {}} for n, a in tools], "unknown_job_ids": []}


CARD = {"job_id": "b0601", "reasons": ["池袋"]}
SAVE_EXP = ("save_user_preferences", {"preferences": {"experience": "未経験"}})
CONSULT = {"expect_outcome": ["propose", "consult"], "consult_mode": True}


def test_checks_catch_consult_closed_before_the_user_cuts_it_off():
    turns = [turn(["どうしますか?"], quick_replies=MENU),
             turn(["経験はありますか?"]),
             turn(["承知しました。"], [SAVE_EXP, ("record_conversation_outcome", {"outcome": "consult"})], quick_replies=CLOSING)]
    v = conversation_checks(CONSULT, turns, [None, "まず相談する", "未経験です"])["violations"]
    assert v == ["T3: 本人が切り上げる前に相談を締めた"]


def test_checks_catch_jobs_at_the_consult_entry_and_a_missing_consult_choice():
    turns = [turn(["どうしますか?"], quick_replies=["求人を探す", "応募履歴を確認", "使い方", "今はいい"]),
             turn(["求人です。"], [("recommend_jobs", {})], cards=[CARD], quick_replies=CLOSING)]
    v = conversation_checks(CONSULT | {"expect_consult_menu": True}, turns, [None, "何から始めればいいか分からない"])
    assert "T2: 相談の入口で、ヒアリングの1問から始めていない" in v["violations"]
    assert any(x.startswith("T1: 初回の選択肢に「まず相談する」が無い") for x in v["violations"])


def test_checks_catch_reasking_saved_experience_and_not_switching_on_show_me_jobs():
    turns = [turn(["どうしますか?"], quick_replies=MENU),
             turn(["経験はありますか?"]),
             turn(["好きなことはありますか?"], [SAVE_EXP]),
             turn(["その前に、これまでにどんな仕事をしましたか?"])]
    v = conversation_checks(CONSULT, turns, [None, "まず相談する", "未経験です", "求人を見せて"])["violations"]
    assert "T4: 「求人を見せて」の後に求人を探さなかった" in v
    assert any(x.startswith("T4: 保存済みの「experience」を聞き直した") for x in v)


SC = {"expect_outcome": ["wait"]}


def test_checks_do_not_read_putting_off_jobs_as_show_me_jobs():
    turns = [turn(["どうしますか?"], quick_replies=MENU), turn(["どのあたりで探しますか?"]),
             turn(["承知しました。また声をかけてくださいね。"], quick_replies=CLOSING)]
    for later in ("求人を見るのはあとでいいです", "求人を見るのはまた今度で"):
        v = conversation_checks(SC, turns, [None, "求人を探す", later])["violations"]
        assert not any("「求人を見せて」の後に" in x for x in v), v
    v = conversation_checks(SC, turns, [None, "求人を探す", "今はいいから求人を見せて"])["violations"]
    assert "T3: 「求人を見せて」の後に求人を探さなかった" in v


def test_checks_catch_unsaved_consult_answers():
    turns = [turn(["どうしますか?"], quick_replies=MENU), turn(["経験はありますか?"]),
             turn(["好きなことはありますか?"])]
    sc = CONSULT | {"expect_saved_preferences": ["experience"]}
    v = conversation_checks(sc, turns, [None, "まず相談する", "未経験です"])["violations"]
    assert v == ["相談で聞いた「experience」を保存していない"]
