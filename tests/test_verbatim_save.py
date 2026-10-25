"""本人の言葉のままの保存(task 133)の台本テスト。実 LLM なし、dip の MCP は本物。

H の評価(evals/baseline/sim-20261004-113109.json)の T-side-job T5 で、本人の「平日は19時以降か、土日なら働けます」を
time_band=「平日は19時以降」と days_per_week=「平日か土日なら働けます」に分けて保存した。days_per_week の値は言い換えで、
平日の「19時以降」の条件も落ちている。ここでは、
(a) 1つの発言に複数の条件が入っているときの決まり(分けても言い換えない・条件を落とさない)が dip の Prompt と
    区分に共通の playbook(チャット・音声)にあり、台本の会話で LINE 側エージェントに届くこと。例文は評価のペルソナから離す、
(b) 同じ T-side-job の発言に、H の保存の台本と直した決まりの台本で答えると、H は評価が作り話に数え、
    直した台本は違反が無く、保存した値がどれも発言から切り出した本人の言葉になること
を確かめる。実 LLM が決まりどおりに保存するかは、実 API の評価でしか測れない(未計測)。
"""

from __future__ import annotations

import json

from dip_mcp.data import ROOT
from evals.checks import conversation_checks, preference_violations
from tests.test_consult import MENU, ConsultModel, run, stated, switch_and_read
from tests.test_handle import call, reply
from tests.test_playbook import _between, _persona_text, _shared

UID = "sim-new"
BAITO = "アルバイト・パート"
PROMPT = (ROOT / "dip_mcp" / "prompts" / "baitoru_assistant.md").read_text(encoding="utf-8")
PLAYBOOKS = ROOT / "dip_mcp" / "playbooks"
H_BASELINE = ROOT / "evals" / "baseline" / "sim-20261004-113109.json"
RULE = "本人の1つの発言に複数の条件が入っているとき"
SPLIT = "項目ごとに分けて保存してもよいが、どの値も発言から切り出した本人の言葉のままにする"
NO_PARAPHRASE = "言い換えない"
NO_DROP = "条件を落とさない"
WHOLE = "切り出すと条件が落ちるなら、分けずに発言のまま1つの項目に入れる"
# 指示文の例(評価のペルソナの発言から離した、同じ形の発言)
EXAMPLE_SAID = "夕方6時以降か、日曜の午前なら入れます"

# T-side-job の本人の発言(模擬ユーザーの H での返信のまま)
SAID = [None,
        "本業の後の空いた時間でバイトを探したい",
        "少し収入を増やしたいのと、本業とは違う人と話す仕事をしてみたいからです。",
        "一日中数字を見ているので、気分を変えたいです。",
        "平日は19時以降か、土日なら働けます。深夜は避けたいです。",
        "求人を見る"]
TIMES = SAID[4]
# H の保存(T-side-job T5): days_per_week が言い換えで、平日の「19時以降」が落ちている
H_SAVE = {"time_band": "平日は19時以降", "days_per_week": "平日か土日なら働けます", "avoids": "深夜は避けたいです"}
# 直した決まりの保存: 分けても、どの値も発言から切り出した本人の言葉
SAVE = {"time_band": "平日は19時以降", "days_per_week": "土日なら働けます", "avoids": "深夜は避けたいです"}
# エリアは「求人を見る」の後に聞く(台本はその問いで終わる)ので、保存の期待に入れない
SC = {"expect_offer_by": 5, "consult_mode": True, "expect_saved_preferences": ["purpose", "time_band"]}


def save(prefs: dict):
    return call("save_user_preferences", {"line_user_id": UID, "preferences": prefs})


def script(times: dict) -> list:
    """T-side-job の H の会話の問い(目的 → 掘り下げ → 時間帯 → エリア → 求人を見るか)を、相談の問いは4問までの決まり
    (task 225)に合わせて縮めたもの: 目的 → 掘り下げ → 条件をまとめて1問 → 求人を見るか → (「求人を見る」の後に)エリア。
    本人は条件の問いに時間帯と曜日だけを答える(H の T5 の発言のまま)。違うのは、時間帯と曜日の答えの保存(times)だけ。"""
    return [
        reply(["こんにちは。お仕事探しや応募のご相談ができます。今日は何から始めますか?"], quick_replies=MENU),
        switch_and_read(UID, BAITO, SAID[1]),
        reply(["アルバイトで何から始めるか、一緒に整理しましょう。",
               "本業の後にバイトをしたいのは、どんな理由からですか?"], quick_replies=[]),
        save({"purpose": "少し収入を増やしたい", "wants": "本業とは違う人と話す仕事をしてみたい"}),
        reply(["収入を増やしながら、人と話す仕事もしてみたいのですね。", "そう思ったのは、どんな場面からですか?"],
              quick_replies=[]),
        reply(["本業の後、何時ごろから、どの曜日に、どのあたりで働けそうですか?"],
              quick_replies=["夕方から", "夜から", "今はいい"]),
        save(times),
        reply(["平日は19時以降か土日で、深夜は避けたい、人と話せる仕事を探す方向ですね。", "この内容で求人を見ますか?"],
              quick_replies=["求人を見る", "もう少し相談したい", "今はいい"]),
        reply(["探す前に、エリアだけ伺います。", "どのあたりで探しますか?"], quick_replies=[]),
    ]


def run_script(times: dict, tmp_path) -> tuple[list[dict], ConsultModel]:
    model = ConsultModel(script=script(times))
    model.systems = []
    turns = run(model, [(UID, "visit", None)] + [(UID, "message", m) for m in SAID[1:]], tmp_path)
    assert model.script == []  # 台本を使い切った
    return turns, model


# ----------------------------- (a) 決まりが Prompt と playbook にあり、エージェントに届く -----------------------------


def test_prompt_and_playbooks_say_split_values_keep_the_persons_words():
    rules = (RULE, SPLIT, NO_PARAPHRASE, NO_DROP, WHOLE)
    save_rule = next(line for line in PROMPT.splitlines() if line.startswith("- 本人が希望や自分のこと("))
    assert all(r in save_rule for r in rules)
    for name in ("common_chat.md", "common_voice.md"):  # 区分に共通の playbook(どの区分の相談でも読む)
        text = (PLAYBOOKS / name).read_text(encoding="utf-8")
        save_rule = next(line for line in text.splitlines() if "save_user_preferences に本人の言葉のまま保存する" in line)
        assert all(r in save_rule for r in rules), name
        assert _between(text, RULE + "(「", "」)") == EXAMPLE_SAID, name
    assert _between(PROMPT, RULE + "(「", "」)") == EXAMPLE_SAID


def test_example_matches_the_evaluator_and_does_not_echo_the_eval_personas():
    # 例のとおりに分けた保存は評価の作り話に数えず、例の言い換え(悪い例)は数える
    turn = {"tools": [{"name": "save_user_preferences", "args": {"preferences": {
        "time_band": "夕方6時以降", "days_per_week": "日曜の午前なら入れます"}}, "result": {}}]}
    assert preference_violations([turn], [EXAMPLE_SAID]) == []
    turn["tools"][0]["args"]["preferences"] = {"time_band": "夕方6時以降", "days_per_week": "平日か日曜なら入れます"}
    assert preference_violations([turn], [EXAMPLE_SAID]) == ["T1: 本人が言っていない希望を保存した: days_per_week=平日か日曜なら入れます"]
    # 例文は評価のペルソナから離す(tests/test_playbook.py の (6) と同じ判定)
    example = _between(PROMPT, RULE, "言い換えない")
    assert _shared(example, _persona_text()) == set(), _shared(example, _persona_text())
    assert _shared(TIMES, _persona_text())  # 負例: T-side-job の発言をそのまま例にすると、重なりとして見つかる


def test_rule_reaches_the_agent_in_the_prompt_and_the_playbook(tmp_path):
    turns, model = run_script(SAVE, tmp_path)
    assert RULE in model.systems[0] and WHOLE in model.systems[0]  # dip の Prompt(システムプロンプト)
    read = next(t for t in turns[1]["tools"] if t["name"] == "get_counseling_playbook")
    assert read["result"]["track"] == BAITO and RULE in read["result"]["playbook"]  # 相談の入口で読んだ playbook


# ----------------------------- (b) T-side-job の発言: H の保存と直した決まりの保存 -----------------------------


def test_h_save_of_t_side_job_is_flagged_on_the_saved_conversation():
    runs = {r["case"]: r for r in json.loads(H_BASELINE.read_text(encoding="utf-8"))["runs"]}
    detail = runs["T-side-job"]["detail"]
    saved = next(t["args"]["preferences"] for t in detail["turn_log"][4]["tools"] if t["name"] == "save_user_preferences")
    assert detail["user_msgs"][4] == TIMES and saved == H_SAVE  # この台本の H の保存は、H の会話のまま
    assert "T5: 本人が言っていない希望を保存した: days_per_week=平日か土日なら働けます" in detail["violations"]


def test_split_save_keeps_the_persons_words_and_conditions(tmp_path):
    h_turns, _ = run_script(H_SAVE, tmp_path / "h")
    turns, _ = run_script(SAVE, tmp_path / "new")
    flagged = "T5: 本人が言っていない希望を保存した: days_per_week=平日か土日なら働けます"
    h = conversation_checks(SC, h_turns, SAID)
    assert h["fabrications"] == [flagged] and h["violations"] == [flagged]
    r = conversation_checks(SC, turns, SAID)
    assert r["violations"] == [] and r["fabrications"] == [] and r["outcome_as_expected"] and r["offer_turn"] == 5
    # 直した保存: どの値も発言から切り出した本人の言葉(言い換えが無い)で、平日の「19時以降」と「土日」の両方が残る
    new = stated(tmp_path / "new", UID)
    assert all(new[k] in TIMES for k in SAVE)
    assert "19時以降" in new["time_band"] and "土日" in new["days_per_week"]
    # H の保存: days_per_week は発言に無い言い方(「平日か土日」)で、平日を時間の条件なしで書いている
    old = stated(tmp_path / "h", UID)
    assert old["days_per_week"] not in TIMES and "平日" in old["days_per_week"] and "19時" not in old["days_per_week"]
    # 違うのは時間帯と曜日の保存だけ(問いと返信は同じ)
    assert [x["bubbles"] for x in turns] == [x["bubbles"] for x in h_turns]
    assert {k: v for k, v in new.items() if k != "days_per_week"} == {k: v for k, v in old.items() if k != "days_per_week"}
