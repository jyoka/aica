"""条件の問いは1問に1つ聞き、必ず選択肢を付ける(会話の改善 D (2/3)、jyoka/tasks#233)のテスト。実 LLM なし、dip の MCP は本物。

    ./.venv/bin/python -m pytest -q tests/test_condition_options.py

確かめること:
(a) dip の要約(get_user_conversation_summary)と区分の切り替え(set_search_track)が、区分ごとの条件の問いの選択肢の候補
    (condition_options)を返す。エリアは本人の話・行動のものが先で、無ければ区分の求人に多いエリア
(b) 給与の区切りが、求人データの分布に合う(どの区切りも求人を絞り、区切りが上がるほど残る求人が減る)
(c) 偽の LLM の台本(アルバイト・パートと正社員)で、エリアと時給・年収の問いに候補が付き、評価の判定に違反が無い
(d) LINE の実機の会話(2026-10-26 07:36。「通いやすい駅・エリアと、希望年収の下限を教えてください。」で選択肢なし)が、
    新しい判定(evals.checks.condition_question_violations)で違反になる。判定の正例・負例
(e) Prompt と playbook が、条件を1問に1つ聞き、必ず選択肢を付けると書いている
"""

from __future__ import annotations

import pytest

from dip_mcp import logic
from dip_mcp.data import ROOT, State, load_jobs
from evals import checks
from evals.checks import conversation_checks, turn_violations
from line_runtime.runtime import _tool_payload
from tests.test_consult import (
    CLOSING,
    ConsultModel,
    calls,
    returned_jobs,
    run,
    switch_and_read,
)
from tests.test_handle import call, reply

BAITO, SEISHAIN, MITEI = "アルバイト・パート", "正社員", "未定"
PROMPT = (ROOT / "dip_mcp" / "prompts" / "baitoru_assistant.md").read_text(encoding="utf-8")
PLAYBOOKS = ROOT / "dip_mcp" / "playbooks"
MENU = ["求人を探す", "まず相談する", "今はいい"]
SC = {"expect_outcome": ["propose"]}
# LINE の実機(2026-10-26 07:36)で「条件を決めて探す」の後に返った返信。quick_replies は空だった
REAL = "勤務地と希望年収を決めましょう。通いやすい駅・エリアと、希望年収の下限を教えてください。"


def turn(bubbles, quick_replies=(), tools=(), cards=()):
    return {"bubbles": list(bubbles), "quick_replies": list(quick_replies), "tools": list(tools), "cards": list(cards),
            "actions": [], "buttons": []}


# ----------------------------- (a) 要約と切り替えが候補を返す -----------------------------


def test_condition_options_for_each_track_fall_back_to_common_areas():
    """手がかりの無い人(行動も、話した希望も無い)でも、エリアと職種の候補が3つずつある(区分の求人に多いもの)。
    給与は区切り3つと「こだわらない」。区分が未定なら、給与・時間帯などは区分が決まってから聞くので載せない。"""
    part = logic.condition_options({}, BAITO)
    assert part == {"area": ["横浜", "大宮", "渋谷"], "job_type": ["カフェ", "コンビニ", "飲食ホール"],
                    "min_wage": ["時給1200円〜", "時給1400円〜", "時給1600円〜", "こだわらない"],
                    "time_band": ["平日の日中", "夕方から", "土日"], "days_per_week": ["週2〜3日", "週4日以上"]}
    full = logic.condition_options({}, SEISHAIN)
    assert full["area"] == ["新宿", "上野", "大宮"] and full["job_type"] == ["営業", "企画・PM", "エンジニア"]
    assert full["min_annual_salary"] == ["400万円〜", "500万円〜", "600万円〜", "こだわらない"]
    assert set(full) == {"area", "job_type", "min_annual_salary", "work_style", "start_timing"}
    undecided = logic.condition_options({}, MITEI)
    assert set(undecided) == {"area", "job_type"} and len(undecided["area"]) == 3
    # どの候補も、選択肢の決まり(候補2〜3個と逃げ道で4つまで)に収まる
    for opts in (part, full, undecided):
        assert all(len(v) <= checks.QUICK_MAX for v in opts.values())
    # エリアの候補は求人のあるエリアだけ(作らない)
    for track, opts in ((BAITO, part), (SEISHAIN, full)):
        assert set(opts["area"]) <= {j.area for j in load_jobs().values() if j.employment_type == track}


def test_condition_options_put_what_the_person_said_and_did_first():
    """本人が話したエリア(stated_preferences の area にある地名)、行動の傾向(閲覧・保存・応募)、直近の応募の順に並べ、
    足りなければ求人に多いエリアで埋める。職種は区分の一覧にあるものだけ。"""
    user = {"preferences": {"area": {"value": "池袋か上野あたり", "at": "2026-09-20T10:00:00+09:00"}}}
    opts = logic.condition_options(user, SEISHAIN, {"area": ["渋谷", "池袋"], "job_type": ["カフェ", "営業"]},
                                   {"area": "品川", "job_type": "販売・サービス"})
    assert opts["area"] == ["池袋", "上野", "渋谷"]
    assert opts["job_type"] == ["営業", "販売・サービス", "企画・PM"]  # カフェはアルバイト・パートの職種なので入れない
    assert logic.condition_options({}, BAITO, {"area": ["渋谷"]})["area"] == ["渋谷", "横浜", "大宮"]
    # 長い地名の中の短い地名(「新横浜」の「横浜」)は、別の候補に数えない
    user = {"preferences": {"area": {"value": "新横浜の近く", "at": "2026-09-20T10:00:00+09:00"}}}
    assert logic._place_names("新横浜の近く") == ["新横浜"]  # 新横浜は求人の最寄り駅
    assert logic.condition_options(user, BAITO)["area"] == ["新横浜", "横浜", "大宮"]


def test_summary_and_track_switch_return_the_options(tmp_path):
    state = State(tmp_path / "state.json")
    # 行動のある人(sim-browsing は渋谷・品川のカフェやアパレルを見ていた)
    s = logic.conversation_summary(state, "sim-browsing")
    assert s["condition_options"]["area"][:2] == ["渋谷", "品川"] and "condition_options" in s["note"]
    # 連携していない人にも出る(行動が無いので、求人に多いエリア)
    s = logic.conversation_summary(state, "sim-new")
    assert s["condition_options"] == logic.condition_options({}, MITEI) and "condition_options" in s["note"]
    # 切り替えたら、切り替えた後の区分の候補(給与の区切りは区分で変わる)。同じ区分なら記録せず、候補も返さない
    r = logic.set_search_track(state, "sim-browsing", SEISHAIN, "正社員で探したい")
    assert r["changed"] and r["condition_options"]["min_annual_salary"][0] == "400万円〜"
    assert r["condition_options"]["area"][:2] == ["渋谷", "品川"]
    assert "condition_options" not in logic.set_search_track(state, "sim-browsing", SEISHAIN, "正社員で")
    r = logic.set_search_track(state, "sim-new", BAITO, "バイトを探したい")
    assert r["condition_options"]["min_wage"][0] == "時給1200円〜"


# ----------------------------- (b) 給与の区切りと求人データの分布 -----------------------------


@pytest.mark.parametrize("track", [BAITO, SEISHAIN])
def test_pay_steps_split_the_jobs_of_the_track(track):
    """区切りはどれも求人を絞り(一番下の区切りでも落ちる求人がある)、区切りが上がるほど残る求人が減る。
    一番上の区切りでも、区分の求人の15%以上が残る(押して0件近くにならない)。根拠は LLD の「条件の問いの選択肢」。"""
    key, steps = logic.PAY_STEPS[track]
    jobs = [j for j in load_jobs().values() if j.employment_type == track]
    left = [sum(logic._match(j, None, None, None, n if key == "min_wage" else None, track,
                             n if key == "min_annual_salary" else None) for j in jobs) for n in steps]
    assert len(jobs) > left[0] and left == sorted(left, reverse=True) and len(set(left)) == len(left)
    assert left[-1] >= 0.15 * len(jobs)


# ----------------------------- (c) 偽の LLM の台本で、条件の問いに候補が付く -----------------------------


def _options(messages, tool: str) -> dict:
    """直近の tool の結果の condition_options。"""
    m = next(m for m in reversed(messages) if m.type == "tool" and m.name == tool)
    return _tool_payload(m)["condition_options"]


def _ask(bubbles: list[str], key: str, escape: tuple[str, ...] = ("今はいい",)):
    """set_search_track が返した候補から、条件の問いの選択肢を作る返信(エリア・職種は3つと「今はいい」、給与はそのまま)。"""
    def make(messages):
        opts = _options(messages, "set_search_track")[key]
        return reply(bubbles, quick_replies=opts if key in logic.PAY_LABELS else [*opts[:3], *escape])
    return make


def test_parttime_area_and_wage_questions_come_with_options(tmp_path):
    """アルバイト・パート: エリア → 時給の下限を1問ずつ、要約の候補で聞いて、答えの条件で1回だけ探す。"""
    said = [None, "空いた時間にバイトを探したい", "横浜", "時給1400円〜"]
    uid = "sim-new"
    model = ConsultModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU),
        call("set_search_track", {"line_user_id": uid, "track": BAITO, "evidence": said[1]}),
        _ask(["空いた時間に合うアルバイトを探しましょう。", "どのエリアで探しますか?"], "area"),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"area": "横浜"}}),
        _ask(["時給はどのくらいから探しますか?"], "min_wage"),
        calls(("save_user_preferences", {"line_user_id": uid, "preferences": {"min_wage": "時給1400円〜"}}),
              ("recommend_jobs", {"line_user_id": uid, "employment_type": BAITO, "area": "横浜", "min_wage": 1400}),
              ("record_conversation_outcome", {"line_user_id": uid, "outcome": "propose", "reason": "エリアと時給で探した",
                                               "confidence": 0.8})),
        lambda msgs: reply(["横浜の求人です。"], job_ids=returned_jobs(msgs), quick_replies=CLOSING),
    ])
    turns = run(model, [(uid, "visit", None)] + [(uid, "message", m) for m in said[1:]], tmp_path)
    assert turns[1]["quick_replies"] == ["横浜", "大宮", "渋谷", "今はいい"]
    assert turns[2]["quick_replies"] == ["時給1200円〜", "時給1400円〜", "時給1600円〜", "こだわらない"]
    r = conversation_checks(SC, turns, said)
    assert r["violations"] == [], r["violations"]
    assert r["condition_question_turns"] == [2, 3] and r["condition_no_options_turns"] == []
    assert r["branch_within_3"] is True  # 探す前のエリアの1問は、分岐の期限に数えない
    assert turns[3]["cards"] and all(c["pay"]["min"] >= 1400 for c in turns[3]["cards"])


def test_fulltime_area_and_salary_questions_come_with_options(tmp_path):
    """正社員: 経歴(自由な問い。選択肢なしでよい)→ 勤務地 → 年収の下限を1問ずつ聞いて探す。
    勤務地の候補は、本人が見ていたエリア(渋谷・品川)が先。年収は区切り3つと「こだわらない」。"""
    said = [None, "正社員で転職先を探したい", "アパレルの店舗で販売を5年やっていました", "渋谷", "500万円〜"]
    uid = "sim-browsing"
    model = ConsultModel(script=[
        call("get_user_conversation_summary", {"line_user_id": uid}),
        reply(["おかえりなさい。", "今日はどうしますか?"], quick_replies=MENU),
        switch_and_read(uid, SEISHAIN, said[1]),
        reply(["正社員で、次のお仕事を探しましょう。", "これまでどんなお仕事をされてきましたか?"], quick_replies=[]),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"experience": said[2]}}),
        _ask(["勤務地はどのあたりがよいですか?"], "area"),
        call("save_user_preferences", {"line_user_id": uid, "preferences": {"area": "渋谷"}}),
        _ask(["希望の年収は、どのくらいからですか?"], "min_annual_salary"),
        calls(("save_user_preferences", {"line_user_id": uid, "preferences": {"min_annual_salary": "500万円〜"}}),
              ("recommend_jobs", {"line_user_id": uid, "employment_type": SEISHAIN, "area": "渋谷",
                                  "min_annual_salary": 500}),
              ("record_conversation_outcome", {"line_user_id": uid, "outcome": "propose", "reason": "勤務地と年収で探した",
                                               "confidence": 0.8})),
        lambda msgs: reply(["販売のご経験から、渋谷の求人です。"], job_ids=returned_jobs(msgs), quick_replies=CLOSING),
    ])
    turns = run(model, [(uid, "visit", None)] + [(uid, "message", m) for m in said[1:]], tmp_path)
    assert turns[2]["quick_replies"][:2] == ["渋谷", "品川"] and turns[2]["quick_replies"][-1] == "今はいい"
    assert turns[3]["quick_replies"] == ["400万円〜", "500万円〜", "600万円〜", "こだわらない"]
    r = conversation_checks(SC, turns, said)
    assert r["violations"] == [], r["violations"]
    # 経歴の問いは条件の問いではない(自由な問いなので、選択肢なしでよい)
    assert r["condition_question_turns"] == [3, 4] and r["condition_no_options_turns"] == []
    rec = next(t for t in turns[4]["tools"] if t["name"] == "recommend_jobs")
    assert rec["args"]["min_annual_salary"] == 500 and turns[4]["cards"]


# ----------------------------- (d) 実機の会話と判定 -----------------------------


def test_the_real_line_reply_is_a_violation(tmp_path):
    """実機(2026-10-26)の返信は、エリアと年収の2つを1問で聞き、選択肢が無い。前の判定は、疑問符が無いので問いと
    みなさず、1ターンの形式では何も言わなかった。新しい判定は、選択肢が無いことと、条件を2つ聞いたことを違反にする。"""
    said = [None, "条件を決めて探す"]
    model = ConsultModel(script=[
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=["条件を決めて探す", "まず相談する", "今はいい"]),
        reply([REAL], quick_replies=[]),
    ])
    turns = run(model, [("sim-new", "visit", None), ("sim-new", "message", said[1])], tmp_path)
    assert turns[1]["quick_replies"] == [] and turn_violations(1, turns[1]) == []
    r = conversation_checks(SC, turns, said)
    assert "T2: 条件の問い(area・pay)に選択肢が無い(条件の問いには必ず選択肢を付ける)" in r["violations"]
    assert "T2: 1つの問いで条件を2つ聞いた(area・pay。条件は1問に1つ)" in r["violations"]
    assert r["condition_question_turns"] == [2] and r["condition_no_options_turns"] == [2]


@pytest.mark.parametrize(("bubbles", "quick", "want"), [
    # Goal と実機の文
    ([REAL], [], ["T1: 条件の問い(area・pay)に選択肢が無い(条件の問いには必ず選択肢を付ける)",
                  "T1: 1つの問いで条件を2つ聞いた(area・pay。条件は1問に1つ)"]),
    (["どのエリアで探しますか?"], [], ["T1: 条件の問い(area)に選択肢が無い(条件の問いには必ず選択肢を付ける)"]),
    (["時給はいくらくらいを希望しますか?"], [], ["T1: 条件の問い(pay)に選択肢が無い(条件の問いには必ず選択肢を付ける)"]),
    (["週に何日くらい働けますか?"], [], ["T1: 条件の問い(days_per_week)に選択肢が無い(条件の問いには必ず選択肢を付ける)"]),
    # 前の Prompt と playbook の例(まとめて1問)
    (["どのエリアで、どんなお仕事を探しますか?"], ["渋谷", "今はいい"],
     ["T1: 1つの問いで条件を2つ聞いた(area・job_type。条件は1問に1つ)"]),
    (["どのエリアで、何曜日の何時ごろ働けますか?"], ["渋谷", "今はいい"],
     ["T1: 1つの問いで条件を2つ聞いた(area・time_band。条件は1問に1つ)"]),
    (["働ける曜日・時間帯と、通いやすい駅を教えてもらえますか?"], ["平日の夕方", "今はいい"],
     ["T1: 1つの問いで条件を2つ聞いた(area・time_band。条件は1問に1つ)"]),
    # 1つの条件に選択肢を付けた問い(曜日と時間帯は1つの条件)
    (["どのエリアで探しますか?"], ["渋谷", "新宿", "今はいい"], []),
    (["希望の年収はどのくらいですか?"], ["400万円〜", "500万円〜", "600万円〜", "こだわらない"], []),
    (["何曜日の何時ごろが合いますか?"], ["平日の日中", "夕方から", "土日", "今はいい"], []),
    # 値を示す確かめと、外し方の選択は、2つの条件に触れても1つの問い
    (["前回は渋谷・平日の夕方でしたが、今回も同じですか?"], ["同じで探す", "変えたい", "今はいい"], []),
    (["勤務地を広げるか、時給の下限を下げますか?"], ["勤務地を広げる", "時給を下げる", "今はいい"], []),
    # 条件の問いではないもの(自由な問いは選択肢なしでよい)
    (["これまでどんなお仕事をされてきましたか?"], [], []),
    (["今回、バイトを探そうと思ったのはなぜですか?"], [], []),
    # 迷い・気になる点、フォームの止まった所、空欄の数字の問いと、どの条件を変えるかの選択は、条件の値を聞く問いではない
    (["どこで迷っていますか?"], [], []),
    (["仕事内容や勤務時間など、どの点が気になりますか?"], [], []),
    (["どこで止まりましたか?"], [], []),
    (["下書きの空欄には、何日と入れますか?"], [], []),
    (["エリア・時間帯・職種のうち、どれを変えたいですか?"], ["エリア", "時間帯", "職種", "今はいい"], []),
    (["渋谷・平日の夕方で整理しました。", "この内容で求人を見ますか?"], ["求人を見る", "今はいい"], []),
    (["渋谷エリアの求人です。"], [], []),  # 問いの無い文
    # 分かっている条件を言い直して、別の1つを聞く問いは、条件1つの問い(自動レビューの例)
    (["横浜エリアだと、どの時間帯が合いますか?"], ["平日の日中", "夕方から", "今はいい"], []),
    (["勤務地は新宿で、希望の年収はどのくらいですか?"], ["400万円〜", "500万円〜", "600万円〜", "こだわらない"], []),
    (["時給1200円以上で、どのエリアがいいですか?"], ["渋谷", "新宿", "今はいい"], []),
    (["前回は平日の夕方で探していましたが、今回はどのエリアで探しますか?"], ["渋谷", "新宿", "今はいい"], []),
    # これまでのことや今の仕事を聞く自由な問いは、条件の問いではない(選択肢なしでよい)
    (["以前はどこで働いていましたか?"], [], []),
    (["前職の年収はどのくらいでしたか?"], [], []),
    (["今のお仕事は何時ごろ終わりますか?"], [], []),
    (["平日は授業がありますか?"], [], []),
    # 気になる点の句の後に条件を聞く問いは、条件の問い(playbook の例)
    (["シフトで収入が変わるのが気になるなら、希望の年収はどのくらいですか?"], [],
     ["T1: 条件の問い(pay)に選択肢が無い(条件の問いには必ず選択肢を付ける)"]),
    # 丁寧な言い方と、実機の文に近い言い方
    (["どのエリアで、どんなお仕事をお探しですか?"], ["渋谷", "今はいい"],
     ["T1: 1つの問いで条件を2つ聞いた(area・job_type。条件は1問に1つ)"]),
    (["どんな職種がいいですか?"], [], ["T1: 条件の問い(job_type)に選択肢が無い(条件の問いには必ず選択肢を付ける)"]),
    (["ご希望の勤務地と年収を教えてください。"], ["新宿", "今はいい"],
     ["T1: 1つの問いで条件を2つ聞いた(area・pay。条件は1問に1つ)"]),
    (["働きたいエリアと時給を教えてください。"], ["新宿", "今はいい"],
     ["T1: 1つの問いで条件を2つ聞いた(area・pay。条件は1問に1つ)"]),
    (["働き始めたい時期はいつごろですか?"], [], ["T1: 条件の問い(start_timing)に選択肢が無い(条件の問いには必ず選択肢を付ける)"]),
])
def test_condition_question_violations(bubbles, quick, want):
    assert checks.condition_question_violations([turn(bubbles, quick)]) == want


def test_narrowing_question_next_to_job_cards_is_checked():
    """求人カードを出したターンの絞り込みの問いにも、選択肢を付ける。求人を見るかの問いは条件の問いではない。"""
    shown = turn(["渋谷の3件です。", "時給はどのくらいから探しますか?"], cards=[{"job_id": "b0101", "reasons": ["渋谷"]}])
    assert checks.condition_question_violations([shown]) == [
        "T1: 条件の問い(pay)に選択肢が無い(条件の問いには必ず選択肢を付ける)"]
    assert checks.condition_topics(turn(["池袋・夕方で整理しました。", "この内容で求人を見ますか?"])) == []


def test_job_question_skips_the_deadline_only_after_a_separate_area_question():
    """仕事の希望の1問を分岐の期限に数えないのは、エリアと別に聞いたときだけ。エリアを聞かずに区分と仕事を聞いて
    T4 で探した #221 の F-follow-new は、前と同じく期限(3回目の返信まで)を過ぎた。"""
    import json

    from evals.run import recheck

    runs = {r["case"]: r for r in json.loads((ROOT / "evals" / "baseline" / "sim-20261025-230120.json")
                                             .read_text(encoding="utf-8"))["runs"]}
    assert recheck(runs["F-follow-new"]["detail"])["branch_within_3"] is False
    rec = ("recommend_jobs", {"area": "渋谷"})
    cards = [{"job_id": "b0101", "reasons": ["渋谷"]}]
    tools = [{"name": n, "args": a, "result": {}} for n, a in [rec]]
    base = [turn(["どうしますか?"], MENU), turn(["どちらで探しますか?"], ["アルバイト・パート", "今はいい"])]
    said = [None, "求人を探す", "アルバイト・パート", "渋谷", "カフェ"]
    area, job = turn(["どのエリアで探しますか?"], ["渋谷", "今はいい"]), turn(["どんなお仕事を探しますか?"], ["カフェ", "今はいい"])
    found = turn(["渋谷の3件です。"], CLOSING, tools=tools, cards=cards)
    # 区分 → エリア → 仕事 → T5 で探した: エリアと仕事の2問を期限に数えない
    assert conversation_checks(SC, [*base, area, job, found], said)["branch_within_3"] is True
    # 区分 → 仕事 → 仕事 → T5: エリアを聞いていないので、仕事の問いも期限に数える
    assert conversation_checks(SC, [*base, job, job, found], said)["branch_within_3"] is False


def test_kodawaranai_is_an_escape_only_for_pay_questions():
    """「こだわらない」を逃げ道に数えるのは時給・年収の問いだけ。ほかの問いは「今はいい」が要る。「こだわらない」だけの
    選択肢は逃げ道だけ。"""
    no_escape = "質問の選択肢に逃げ道(今はいい)がない"
    assert not any(no_escape in v for v in turn_violations(0, turn(["時給はどのくらいから探しますか?"],
                                                                    ["時給1200円〜", "時給1400円〜", "こだわらない"])))
    assert any(no_escape in v for v in turn_violations(0, turn(["どのエリアで探しますか?"], ["渋谷", "新宿", "こだわらない"])))
    assert any("逃げ道だけ" in v for v in turn_violations(0, turn(["希望の年収はどのくらいですか?"], ["こだわらない"])))


def test_escape_only_options_count_as_no_options():
    """「今はいい」だけの選択肢は、逃げ道だけの違反(escape_only)で数え、指標の「答えの候補が無い」にも数える。"""
    turns = [turn(["どのエリアで探しますか?"], ["今はいい"])]
    r = conversation_checks({"expect_outcome": ["wait"]}, turns, [None])
    assert "T1: 質問の選択肢が逃げ道だけ(答えの候補が無い): ['今はいい']" in r["violations"]
    assert not any("条件の問い(" in v for v in r["violations"])  # 同じことを2回数えない
    assert r["condition_no_options_turns"] == [1]


def test_pay_options_end_with_kodawaranai_instead_of_ima_wa_ii():
    """時給・年収の問いは区切り3つと「こだわらない」で4つ(選択肢の上限)。「こだわらない」を逃げ道に数える。"""
    r = turn(["希望の年収はどのくらいですか?"], logic.condition_options({}, SEISHAIN)["min_annual_salary"])
    assert turn_violations(0, r, SEISHAIN) == []


def test_option_amounts_are_not_evidence_for_pay_claims():
    """要約の区切り(「500万円〜」)は求人の事実ではないので、吹き出しの金額の根拠にしない(作り話の判定を弱めない)。"""
    summary = {"name": "get_user_conversation_summary", "args": {},
               "result": {"condition_options": logic.condition_options({}, SEISHAIN)}}
    v = checks.fact_violations([turn(["年収500万円の求人があります。"], tools=[summary])], ["正社員で"], None)
    assert any("500" in x for x in v), v


# ----------------------------- (e) Prompt と playbook -----------------------------


def test_prompt_and_playbooks_ask_one_condition_at_a_time_with_options():
    rules = PROMPT.split("## 対話ルール")[1]
    assert "条件の問い(エリア・職種・時給か年収・時間帯と曜日・週の日数、正社員なら働き方・働き始める時期)は、1問に1つだけ聞き、必ず選択肢を付ける" in rules
    assert "条件の問いは下の自由な問いに当たらない" in rules and "condition_options" in rules
    assert "時給・年収は区切り3つと「こだわらない」" in rules
    assert "本人が選択肢に無い答えを書いてもよい" in rules
    # エリアの問いは、候補が無いときに選択肢なしにしない(前の Prompt の「候補を作らない」は無くした)
    assert "候補を作らない" not in PROMPT and "condition_options の area から2〜3個と「今はいい」" in PROMPT
    common = (PLAYBOOKS / "common_chat.md").read_text(encoding="utf-8")
    assert "1問に1つだけ聞き、必ず選択肢を付ける" in common and "駅を作らない" not in common
    assert "条件の問いは当たらない" in common  # 自由な問いの決まり
    # 相談の問いは4問まで(task 225)と、エリアが分かってから最初の求人を出す(task 224)は残す
    assert "相談の問いは4問まで" in common and "最低限の条件はエリア" in PROMPT
    # 職種の候補は相談に入っていないときだけ。相談の途中の仕事の方向は、本人の話から作る(職種の一覧を並べない決まりは残す)
    assert "相談の途中の仕事の方向の問いと、本人が「分からない」と答えた直後は、playbook のとおり職種の一覧を並べない" in rules
    assert "condition_options の job_type は使わない" in common and "職種の一覧をそのまま並べる" in common
    for name in ("common_chat.md", "parttime_chat.md", "fulltime_chat.md"):
        assert "まとめて1問" not in (PLAYBOOKS / name).read_text(encoding="utf-8"), name
    assert "まとめて1問" not in PROMPT


def test_tool_descriptions_mention_the_options():
    """ツールの説明(毎回の要求で LLM に渡る)にも、候補があることを書く。"""
    import asyncio

    from dip_mcp import server

    tools = {t.name: t.description for t in asyncio.run(server.mcp.list_tools())}
    assert "condition_options" in tools["get_user_conversation_summary"]
    assert "condition_options" in tools["set_search_track"]
