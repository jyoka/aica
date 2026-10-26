"""相談(playbook に沿って進める)の台本テスト。実 LLM なし、dip の MCP は本物。

相談は別のエージェントではなく、dip の Prompt(入口と「playbook を読む」指示)と、雇用区分ごとの playbook
(get_counseling_playbook)で動く。ここでは (1) dip の Prompt が LINE 側エージェントに届いていること、
(2) 台本どおりに進めた会話(入口で playbook を読み、区分が決まったら読み直し、目的を聞いて掘り下げる)が評価の機械判定を
通ること、(3) 相談で話した内容が dip のプロフィールに残り、次の会話の要約に出ること、(4) 判定が悪い振る舞いを捕まえること、
を確かめる。区分ごとの playbook の中身と深さの台本は tests/test_playbook.py。
"""

from __future__ import annotations

import asyncio
import os

from evals.checks import conversation_checks
from evals.scenarios import load_scenarios
from line_runtime.runtime import _tool_payload
from tests.test_handle import ScriptedModel, call, reply

SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
MENU = ["求人を探す", "まず相談する", "今はいい"]
# 初回訪問の挨拶に付く、Web で使える 3 つの入口(task 237 からコードが付ける。dip の Prompt の「挨拶の入口」)
ENTRIES = {"actions": ["voice_consult", "resume_upload"], "buttons": [{"label": "ゲームで書類を作る", "url": "/liff/arcade"}]}
CLOSING = ["求人を探す", "応募履歴を確認", "使い方"]
CONFIRM = "正社員の仕事と、空いた時間のアルバイト、どちらを考えていますか?"
CONFIRM_REPLIES = ["正社員", "空いた時間のアルバイト", "今はいい"]
BAITO = "アルバイト・パート"


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


def playbook(track: str) -> object:
    return call("get_counseling_playbook", {"track": track, "channel": "chat"}, "pb")


def switch_and_read(uid: str, track: str, evidence: str, *more) -> object:
    """区分を切り替え、同じターンで新しい区分の playbook を読み直す(並行呼び出し)。more は一緒に呼ぶツール。"""
    return calls(("set_search_track", {"line_user_id": uid, "track": track, "evidence": evidence}),
                 ("get_counseling_playbook", {"track": track, "channel": "chat"}), *more)


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


def tools_of(turn: dict) -> list[str]:
    return [t["name"] for t in turn["tools"]]


def playbooks_read(turn: dict) -> list[str]:
    return [t["result"]["track"] for t in turn["tools"] if t["name"] == "get_counseling_playbook"]


# ----------------------------- dip の Prompt(相談の入口と playbook を読む指示) -----------------------------


def test_consult_prompt_reaches_the_agent_on_first_visit(tmp_path):
    model = ConsultModel(script=[reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU, **ENTRIES)])
    (r,) = run(model, [("sim-new", "visit", None)], tmp_path)
    system = model.systems[0]
    # v1 の「相談モード」節は、playbook を読む指示に置き換わった
    assert "## 相談モード" not in system and "相談モード" not in system
    assert "## 相談(playbook に沿って進める)" in system and "「まず相談する」" in system
    assert "get_counseling_playbook を今の区分" in system and "新しい区分で呼び直し" in system
    assert "「求人を探す / まず相談する / 今はいい」の3つ" in system  # 初回挨拶の選択肢
    assert "## 挨拶の入口(初回訪問・再訪)" in system  # 音声相談・履歴書・ゲームの入口のボタン(コードが付ける)
    assert "相談の途中(下の「相談」)はこの規則を適用しない" in system
    # ヒアリングの順番・「分からないなら職種を2〜3挙げる」は Prompt から消えた(playbook は次に何を聞くかを決めない)
    assert "次の順に聞く" not in system and "向いていそうな職種を2〜3つ" not in system
    assert "まず相談する" in r["quick_replies"]


# ----------------------------- 入口 → 区分を確かめる → 目的から聞く → 提案(3ターンで打ち切られない) -----------------------------


def test_consult_from_menu_confirms_the_track_rereads_the_playbook_and_proposes(tmp_path):
    """「まず相談する」(区分は未定)→ 未定の playbook で区分を1問で確かめる → 答えで切り替えて playbook を読み直す →
    目的を聞く → 生活の制約をまとめて1問 → 4問目で求人を見るかを聞いて提案(相談の問いは4問まで。task 225)。
    3ターンを超えても締めない。
    相談で話したことは dip に保存され、提案の条件にそのまま使われ、次の会話の要約にも出る。"""
    uid = "sim-new"
    model = ConsultModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU, **ENTRIES),
        playbook("未定"),
        reply(["何から始めるか、一緒に整理しましょう。", CONFIRM], quick_replies=CONFIRM_REPLIES),
        switch_and_read(uid, BAITO, "空いた時間のアルバイト"),
        reply(["アルバイト・パートで、合うお仕事を考えますね。", "今回、バイトを探そうと思ったきっかけは何ですか?"],
              quick_replies=["生活費の足しに", "経験を積みたい", "今はいい"]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"purpose": "働く経験を積みたい",
                                                                          "wants": "人と話す接客"}}),
        reply(["働く経験を積みたい、人と話す接客をしてみたいのですね。", "働けるのは、何曜日の何時ごろですか?"],
              quick_replies=["平日の夕方", "土日の日中", "今はいい"]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {
            "experience": "未経験", "time_band": "夕方", "area": "池袋", "days_per_week": "週3日"}}),
        reply(["働く経験を積みたい・接客・池袋の夕方で整理しました。", "この内容で求人を見ますか?"],
              quick_replies=["求人を見る", "もう少し相談したい", "今はいい"]),
        # keywords は意味検索で embedding の API を呼ぶので、オフラインの台本では渡さない
        calls(("recommend_jobs", {"line_user_id": uid, "employment_type": BAITO, "area": "池袋", "time_band": "夕方"}),
              ("record_conversation_outcome", {"line_user_id": uid, "outcome": "propose",
                                               "reason": "相談のあと求人を見る", "confidence": 0.8})),
        lambda msgs: reply(["池袋・夕方の求人です。"], job_ids=returned_jobs(msgs), quick_replies=CLOSING),
        reply(["またいつでもご相談ください。"], quick_replies=CLOSING),
        # 次の会話(再訪): 要約を読む
        call("get_user_conversation_summary", {"line_user_id": uid}),
        reply(["前回はアルバイトで、池袋の夕方でしたが、今回も同じ感じで探しますか?"],
              quick_replies=["同じで探す", "変えたい", "今はいい"]),
    ])
    said = [None, "まず相談する", "空いた時間のアルバイト", "働く経験を積みたい。人と話す接客をしてみたい",
            "バイトは未経験です。平日の夕方、池袋あたりで週3日", "求人を見る", "ありがとう、考えます"]
    steps = [(uid, "visit", None)] + [(uid, "message", m) for m in said[1:]] + [(uid, "revisit", None)]
    *turns, again = run(model, steps, tmp_path)

    sc = {"expect_outcome": ["propose"], "expect_menu": True, "consult_mode": True, "expect_consult_menu": True,
          "expect_saved_preferences": ["experience", "time_band", "purpose"]}
    checks = conversation_checks(sc, turns, said)
    assert checks["violations"] == []
    # 入口で未定の playbook、区分が決まったターンで新しい区分の playbook を読み直した
    assert playbooks_read(turns[1]) == ["未定"] and "まず区分を確かめる" in turns[1]["tools"][0]["result"]["playbook"]
    assert playbooks_read(turns[2]) == [BAITO] and checks["tracks"][2] == BAITO
    assert checks["branch_turn"] == 6  # 相談の間(ユーザーの返信4回)は締めずに1問ずつ聞いた
    assert checks["branch_within_3"] is None  # 相談のシナリオでは「3ターン以内に分岐」を測らない
    assert all(len([b for b in r["bubbles"] if "?" in b or "?" in b]) == 1 for r in turns[:5])  # 毎ターン1問
    # 区分の1問・目的・条件・求人を見るか、の4問目で求人を見るかを聞いた
    assert checks["consult_questions"] == {"start": 2, "asked": [2, 3, 4], "offer": 5, "offer_at": 4}
    assert checks["outcomes"] == ["propose"] and turns[5]["cards"]

    # 相談で話したことがプロフィールに残り、提案の条件にそのまま使われた(言い直し・聞き直しなし)
    assert stated(tmp_path, uid) == {"purpose": "働く経験を積みたい", "wants": "人と話す接客", "experience": "未経験",
                                     "time_band": "夕方", "area": "池袋", "days_per_week": "週3日"}
    rec = next(t for t in turns[5]["tools"] if t["name"] == "recommend_jobs")
    assert (rec["args"]["area"], rec["args"]["time_band"]) == ("池袋", "夕方")
    # 次の会話では要約の stated_preferences と区分として返る
    summary = again["tools"][0]["result"]
    assert summary["stated_preferences"]["purpose"]["value"] == "働く経験を積みたい"
    assert summary["search_track"] == BAITO


def test_consult_from_a_sign_of_being_lost_digs_instead_of_listing_job_types(tmp_path):
    """迷いのサイン(「何から始めれば」)から相談に入る。「自分に合うのが分からない」には職種を並べず、掘り下げに戻る
    (v1 は経験と興味から職種を2〜3挙げていた)。"""
    uid = "sim-new"
    model = ConsultModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU, **ENTRIES),
        switch_and_read(uid, BAITO, "バイトを探したいけど、何から始めればいいか分からない"),
        reply(["アルバイトで何から始めるか、一緒に整理しましょう。",
               "今回、バイトを探そうと思ったのはなぜですか?"], quick_replies=[]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {
            "purpose": "生活費の足し", "experience": "コンビニで半年"}}),
        reply(["生活費の足しにしたいのですね。コンビニで半年されていたんですね。",
               "コンビニの仕事で、楽しかったのはどんな場面でしたか?"], quick_replies=[]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"wants": "お客さんと話すこと"}}),
        reply(["例えば、お客さんとどんな話をしたときに楽しいと感じましたか?"],
              quick_replies=["常連さんとの世間話", "今はいい"]),
    ])
    said = [None, "バイトを探したいけど、何から始めればいいか分からない", "生活費の足しに。コンビニで半年やっていました",
            "お客さんと話すのは好き。でも自分に合うのが分からない"]
    turns = run(model, [(uid, "visit", None)] + [(uid, "message", m) for m in said[1:]], tmp_path)
    checks = conversation_checks({"expect_outcome": ["consult"], "consult_mode": True}, turns, said)
    # 会話はまだ途中なので、出口の記録が無いことだけが期待と違う
    assert checks["violations"] == [] and checks["outcomes"] == []
    assert turns[1]["cards"] == [] and "?" in turns[1]["bubbles"][-1]  # 入口では求人を出さずに1問目
    assert playbooks_read(turns[1]) == [BAITO]
    assert stated(tmp_path, uid) == {"purpose": "生活費の足し", "experience": "コンビニで半年", "wants": "お客さんと話すこと"}

    # 同じ場面で v1 のように職種を並べると、評価の機械判定が捕まえる
    v1 = dict(turns[3], bubbles=[("コンビニの経験と、人と話すのが好きな点から、スーパー・販売やカフェ、イベントスタッフが"
                                  "近そうです。"), "どれが気になりますか?"],
              quick_replies=["スーパー・販売", "カフェ", "イベントスタッフ", "今はいい"])
    bad = conversation_checks({"expect_outcome": ["consult"], "consult_mode": True}, [*turns[:3], v1], said)["violations"]
    assert any(x.startswith("T4: 「分からない」の後に職種を並べた") for x in bad), bad
    assert any(x.startswith("T4: 本人の発言にも履歴書にも無い職種を選択肢に並べた") for x in bad), bad


# ----------------------------- 出口: 相談だけで終える / 途中で提案へ切り替える(評価シナリオの台本) -----------------------------


def test_consult_only_scenario_ends_on_ima_wa_ii_without_proposing(tmp_path):
    sc = SCENARIOS["C-consult-only"]
    uid = sc["user"]
    model = ConsultModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU, **ENTRIES),
        playbook("未定"),
        reply(["何から始めるか、一緒に整理しましょう。", CONFIRM], quick_replies=CONFIRM_REPLIES),
        switch_and_read(uid, BAITO, "バイトは未経験です",
                        ("save_user_preferences", {"line_user_id": uid, "preferences": {"experience": "未経験"}})),
        reply(["アルバイト・パートで、合うお仕事を考えますね。", "今回、バイトを探そうと思ったきっかけは何ですか?"],
              quick_replies=[]),
        call("record_conversation_outcome", {"line_user_id": uid, "outcome": "consult", "reason": "相談のみ",
                                             "confidence": 0.7}),
        reply(["承知しました。またいつでもご相談ください。"], quick_replies=["求人を探す", "使い方"]),
    ])
    r = run_eval_scenario(model, sc, tmp_path)
    assert r["violations"] == [] and r["outcome_as_expected"] and r["branch_within_3"] is None
    assert r["tools"][-1] == ["record_conversation_outcome"]  # 提案(recommend_jobs)なしで終えた
    assert r["tools"][2] == ["set_search_track", "get_counseling_playbook", "save_user_preferences"]
    assert stated(tmp_path, uid) == {"experience": "未経験"}


def test_consult_switch_scenario_proposes_right_away_on_show_me_jobs(tmp_path):
    """区分を確かめる問いに経験で答え、目的を聞いている途中で「求人を見せて」。エリアがまだ分からないので、エリアの1問だけを
    聞いてから、その時点の内容で提案する(task 224。ほかの条件は聞き足さない)。本人が自分から頼んだので、目的を確かめる前の
    提案でも違反ではない。分かっている経験は keywords に入れる。"""
    sc = SCENARIOS["C-consult-switch"]
    uid = sc["user"]
    model = ConsultModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU, **ENTRIES),
        playbook("未定"),
        reply(["何から始めるか、一緒に整理しましょう。", CONFIRM], quick_replies=CONFIRM_REPLIES),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"experience": "居酒屋のホールを1年"}}),
        reply(["居酒屋のホールを1年されていたんですね。", "今回、お仕事を探そうと思ったきっかけは何ですか?"],
              quick_replies=[]),
        reply(["承知しました。", "どのエリアで探しますか?"], quick_replies=["渋谷", "新宿", "今はいい"]),
        calls(("save_user_preferences", {"line_user_id": uid, "preferences": {"area": "渋谷"}}),
              ("recommend_jobs", {"line_user_id": uid, "area": "渋谷", "job_type": "飲食ホール",
                                  "keywords": "居酒屋 ホール"}),
              ("record_conversation_outcome", {"line_user_id": uid, "outcome": "propose",
                                               "reason": "求人を見せて", "confidence": 0.8})),
        lambda msgs: reply(["ホールの経験から、渋谷の求人です。"], job_ids=returned_jobs(msgs), quick_replies=CLOSING),
        reply(["またいつでもご相談ください。"], quick_replies=CLOSING),
    ])
    r = run_eval_scenario(model, sc, tmp_path)
    assert r["violations"] == [] and r["outcome_as_expected"] and r["search_arg_violations"] == []
    assert r["tools"][3] == []  # 「求人を見せて」のターンは、エリアの1問だけ
    assert r["tools"][4] == ["save_user_preferences", "recommend_jobs", "record_conversation_outcome"]
    assert r["turn_log"][4]["cards"]


# ----------------------------- 判定が悪い振る舞いを捕まえる -----------------------------


def turn(bubbles, tools=(), cards=(), quick_replies=("答えの候補", "今はいい")):
    return {"bubbles": list(bubbles), "quick_replies": list(quick_replies), "cards": list(cards), "buttons": [],
            "tools": [{"name": n, "args": a, "result": {}} for n, a in tools], "unknown_job_ids": []}


CARD = {"job_id": "b0601", "reasons": ["池袋"]}
SAVE_EXP = ("save_user_preferences", {"preferences": {"experience": "未経験"}})
READ = ("get_counseling_playbook", {"track": "未定", "channel": "chat"})
CONSULT = {"expect_outcome": ["propose", "consult"], "consult_mode": True}


def test_checks_catch_consult_closed_before_the_user_cuts_it_off():
    turns = [turn(["どうしますか?"], quick_replies=MENU),
             turn(["経験はありますか?"], [READ]),
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
        v = conversation_checks(CONSULT, turns, [None, "まず相談する", later])["violations"]
        assert not any("「求人を見せて」の後に" in x for x in v), v
    v = conversation_checks(CONSULT, turns, [None, "まず相談する", "今はいいから求人を見せて"])["violations"]
    assert "T3: 「求人を見せて」の後に求人を探さなかった" in v


def test_show_me_jobs_outside_consult_may_get_a_clarifying_question():
    """非相談フローでは「求人を見たい」に絞り込みの1問を返してよい(即提案の指示はプロンプト上、相談モードだけ)。"""
    turns = [turn(["どうしますか?"], quick_replies=MENU),
             turn(["どのあたりで探しますか?"], quick_replies=["渋谷", "新宿", "今はいい"])]
    v = conversation_checks(SC, turns, [None, "バイトの求人を見たいです"])["violations"]
    assert v == []


def test_restating_saved_values_in_a_summary_is_not_a_reask():
    """まとめの平叙文で保存済みの値を復唱し、同じ吹き出しの質問で先へ進むのは聞き直しではない。"""
    turns = [turn(["どうしますか?"], quick_replies=MENU),
             turn(["時間帯はいつがよいですか?"], [("save_user_preferences", {"preferences": {"time_band": "夕方"}})]),
             turn(["未経験、時間帯は夕方で整理しました。この内容で求人を見ますか?"], quick_replies=["見る", "今はいい"])]
    v = conversation_checks(CONSULT, turns, [None, "まず相談する", "夕方です"])["violations"]
    assert not any("聞き直した" in x for x in v), v


def test_checks_catch_unsaved_consult_answers():
    turns = [turn(["どうしますか?"], quick_replies=MENU), turn(["経験はありますか?"], [READ]),
             turn(["好きなことはありますか?"])]
    sc = CONSULT | {"expect_saved_preferences": ["experience"]}
    v = conversation_checks(sc, turns, [None, "まず相談する", "未経験です"])["violations"]
    assert v == ["相談で聞いた「experience」を保存していない"]


# ----------------------------- playbook を読む・目的を確かめる・一般論の選択肢(相談の機械判定) -----------------------------


def test_checks_catch_a_consult_that_never_reads_the_playbook():
    turns = [turn(["どうしますか?"], quick_replies=MENU), turn(["いくつか伺います。今回探そうと思ったきっかけは?"]),
             turn(["生活費ですね。例えば?"], [READ])]
    v = conversation_checks(CONSULT, turns, [None, "まず相談する", "生活費のため"])["violations"]
    assert v == ["T2: 相談に入ったのに playbook(get_counseling_playbook)を読んでいない"]  # 後のターンで読んでも遅い
    turns[1]["tools"] = turns[2]["tools"]
    assert conversation_checks(CONSULT, turns, [None, "まず相談する", "生活費のため"])["violations"] == []


PROPOSE = [("recommend_jobs", {"area": "池袋"}), ("record_conversation_outcome", {"outcome": "propose"})]


def test_checks_catch_jobs_proposed_before_asking_the_purpose():
    said = [None, "まず相談する", "池袋の夕方。生活費のため", "求人を見る"]
    turns = [turn(["どうしますか?"], quick_replies=MENU), turn(["働けるのは、いつごろですか?"], [READ]),
             turn(["池袋の夕方ですね。この内容で求人を見ますか?"]),
             turn(["池袋・夕方の求人です。"], PROPOSE, cards=[CARD], quick_replies=CLOSING)]
    v = conversation_checks(CONSULT, turns, said)["violations"]
    # こちらが「求人を見ますか?」と聞いて本人が「求人を見る」と答えたのは、本人から頼んだのではない
    assert v == ["T4: 目的(なぜ今探すのか)を確かめる前に求人を出した"]
    # 目的を聞いた・目的を保存したなら違反ではない(負例)
    for asked in ("今回、お仕事を探そうと思ったきっかけは何ですか?", "なぜ今、バイトを探しているのですか?",
                  "バイトを探す目的は何ですか?", "働き始めたいと思ったのは、どんな変化からですか?",
                  "お仕事を探そうと思ったのは、どんな変化からですか？", "収入のほかに、このバイトで得たいことはありますか?"):
        turns[1]["bubbles"] = [asked]
        assert conversation_checks(CONSULT, turns, said)["violations"] == [], asked
    # 求人や条件への「思ったのは」「どんな変化」は目的の問いではないので、違反のまま
    for asked in ("カフェがいいと思ったのは、どんなところからですか?", "このお仕事を良いと思ったのは、どの点ですか?",
                  "働く時間帯にどんな変化がありそうですか?"):
        turns[1]["bubbles"] = [asked]
        assert conversation_checks(CONSULT, turns, said)["violations"] == [
            "T4: 目的(なぜ今探すのか)を確かめる前に求人を出した"], asked
    turns[1]["bubbles"] = ["働けるのは、いつごろですか?"]
    turns[2]["tools"] = [{"name": "save_user_preferences", "args": {"preferences": {"purpose": "生活費"}}, "result": {}}]
    assert conversation_checks(CONSULT, turns, said)["violations"] == []


def test_show_me_jobs_before_the_purpose_is_not_a_violation():
    # 本人が自分から「求人を見せて」と言ったら、目的を聞く前でもすぐ出す(C-consult-switch)
    turns = [turn(["どうしますか?"], quick_replies=MENU), turn(["経験はありますか?"], [READ]),
             turn(["求人です。"], PROPOSE, cards=[CARD], quick_replies=CLOSING)]
    v = conversation_checks(CONSULT, turns, [None, "まず相談する", "もういいので池袋の求人を見せて"])["violations"]
    assert v == []


def test_job_type_options_from_what_the_person_said_are_not_generic():
    said = [None, "まず相談する", "カフェと、スーパーで働いたことがあります"]
    turns = [turn(["どうしますか?"], quick_replies=MENU), turn(["経験はありますか?"], [READ]),
             turn(["カフェとスーパーのご経験から考えると、どちらに近いお仕事がよいですか?"],
                  quick_replies=["カフェ", "スーパー・販売", "今はいい"])]
    assert conversation_checks(CONSULT, turns, said)["violations"] == []
    # 本人が言っていない職種を並べると一般論の候補(v1 で PM の経歴の人に事務・コールセンター・イベントを出した)
    turns[2]["quick_replies"] = ["オフィス事務", "コールセンター", "イベントスタッフ", "今はいい"]
    v = conversation_checks(CONSULT, turns, said)["violations"]
    assert v == [("T3: 本人の発言にも履歴書にも無い職種を選択肢に並べた(一般論の候補): "
                  "['イベントスタッフ', 'オフィス事務', 'コールセンター']")]
    # 職種の名前が別の語の一部(「法人営業」「営業時間」)なら、職種を挙げたことにならない
    turns[2]["quick_replies"] = ["法人営業の経験を活かす", "営業時間が短い職場", "今はいい"]
    assert conversation_checks(CONSULT, turns, said)["violations"] == []
