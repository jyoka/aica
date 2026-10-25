"""1会話の費用を下げる(task 132)の台本テスト。実 LLM なし、dip の MCP は本物。

H の評価(evals/baseline/sim-20261004-113109.json)では、1会話の LLM の呼び出しが 11.4 → 13.3 回に増えた。
保存済みの会話を数え直すと、増えた場面は (1) 掘り下げの答えだけの save、(2) 同じターンの save の重複、
(3) 相談に入る前の切り替えでの playbook、(4) 同じターンで条件を変えた検索、(5) 条件を1つずつ足した検索だった
(区分が変わっていないのに playbook を読み直した回は 0)。ここでは、
(a) 直した決まりが dip の Prompt と playbook にあること、
(b) 同じ本人の発言に、H の振る舞いの台本と直した決まりの台本で答えると、save・playbook・LLM の呼び出しが減り、
    直した台本は評価の違反も呼び出しの重複(evals.checks.call_overlaps)も 0 になること、
(c) 呼び出しの重複の数え方(H の保存済みの会話で場面ごとの回数)と、数えない場面
を確かめる。相談の深さ(目的を聞く・掘り下げる)は台本の問いを変えずに比べる。費用が下がるかは実 API の評価でしか測れない。
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter

from dip_mcp import server
from dip_mcp.data import ROOT
from evals.checks import call_overlaps, conversation_checks, playbook_violations
from tests.test_consult import (
    CLOSING,
    MENU,
    ConsultModel,
    calls,
    returned_jobs,
    run,
    stated,
    switch_and_read,
)
from tests.test_handle import call, reply

UID = "sim-new"
BAITO, SEISHAIN = "アルバイト・パート", "正社員"
PROMPT = (ROOT / "dip_mcp" / "prompts" / "baitoru_assistant.md").read_text(encoding="utf-8")
COMMON = (ROOT / "dip_mcp" / "playbooks" / "common_chat.md").read_text(encoding="utf-8")
H_BASELINE = ROOT / "evals" / "baseline" / "sim-20261004-113109.json"


def save(prefs: dict):
    return ("save_user_preferences", {"line_user_id": UID, "preferences": prefs})


def count(turns: list[dict], name: str) -> int:
    return sum(t["name"] == name for r in turns for t in r["tools"])


# ----------------------------- (a) 直した決まりが Prompt と playbook にある -----------------------------


def test_prompt_and_playbook_say_how_to_avoid_overlapping_calls():
    section = PROMPT.split("## ツールの呼び方(呼び出しを重ねない)")[1].split("\n## ")[0]
    assert "同じターンで要るツールは、結果を待たずに同じ回に並べて呼ぶ" in section
    # save: 1ターン1回、掘り下げの答えは次の保存にまとめる
    assert "本人が1つの返信で話したことは、1回の save_user_preferences にまとめる" in section
    assert "詳しく言っただけの答えでは、そのためだけに save_user_preferences を呼ばない" in section
    assert "新しい項目と、言い直した条件(エリア・時間帯など)は、そのターンで保存する" in section
    # 検索: 揃った条件は1回、絞り込みは条件をまとめて聞く
    assert "求人が返ったら、職種や条件を1つずつ変えて探し直さない" in section
    assert "まだ分かっていない条件(エリア・時間帯・曜日など)をまとめて1問で聞き" in section
    # playbook: 相談に入ったターン・相談の途中か一度読んだ後で区分が変わったターン・正社員に切り替えたターンだけ
    assert ("playbook を読むのは、相談に入ったターン、相談の途中か一度 playbook を読んだ後で区分が変わったターン、"
            "正社員に切り替えたターンだけ") in section
    assert "相談に入った後に区分が変わったターン" in PROMPT and "set_search_track の結果を待たず、同じ回に並べて呼ぶ" in PROMPT
    assert "この会話でまだ playbook を読んでいないとき" in PROMPT
    assert "アルバイト・パートか未定に切り替えても playbook を読まない" in PROMPT
    assert "正社員に切り替えたときは、相談に入っていなくても同じターンで読む" in PROMPT
    # 一度読んだ後の切り替え(取り消しも)は読み直す: evals.checks.consult_entered の判定と揃える
    assert "一度 playbook を読んだ後に区分が変わったら(取り消しも)、相談に入っていなくても同じターンで新しい区分の playbook を読み直す" in PROMPT
    assert "相談の途中かどうかによらず" not in PROMPT  # H の決まり(どの切り替えでも読み直す)は消えた
    # ツールの説明(毎回の要求で LLM に渡る)も、Prompt と同じ線引きにする
    doc = {t.name: t for t in asyncio.run(server.mcp.list_tools())}["get_counseling_playbook"].description
    assert "相談の途中か一度 playbook を読んだ後のときと、正社員に切り替えたときだけ" in doc
    assert "アルバイト・パートか未定への切り替えでは呼ばない" in doc
    assert "では新しい区分で呼び直す" not in doc  # H の説明(どの切り替えでも呼び直す)は消えた
    # 区分に共通の playbook も、掘り下げの答えは次の保存にまとめる(Prompt と食い違わない)
    assert "掘り下げの答え(保存済みの項目を詳しく言っただけの答え)は、そのためだけに保存しない" in COMMON


# ----------------------------- (b) 相談: 掘り下げの答えを次の保存にまとめる -----------------------------

CONSULT_SAID = [None,
                "バイトを探しているけど、何から始めればいいか分からない",
                "生活費の足しにしたいです",
                "一人暮らしを始めたので、家賃の分を自分で払いたいです",
                "パンが好きなので、パン屋で働いてみたいです",
                "焼きたてのパンをお客さんに渡すところです",
                "平日の夕方、池袋あたりです",
                "求人を見る"]
CONSULT_SC = {"expect_outcome": ["propose"], "consult_mode": True,
              "expect_saved_preferences": ["purpose", "wants", "time_band", "area"]}
PURPOSE = "生活費の足し。一人暮らしを始めたので、家賃の分を自分で払いたい"
WANTS = "パン屋で働いてみたい。焼きたてのパンをお客さんに渡すところ"
RECORD = ("record_conversation_outcome", {"line_user_id": UID, "outcome": "propose", "reason": "相談のあと求人を見る",
                                          "confidence": 0.8})


def consult_script(h: bool) -> list:
    """同じ問いの順(目的 → 掘り下げ → やりたいこと → 掘り下げ → 条件 → 求人を見るか)。h=True は H の呼び方:
    掘り下げの答えだけで save、同じターンで save を2回、条件を変えて検索し直す。"""
    return [
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU),
        switch_and_read(UID, BAITO, CONSULT_SAID[1]),
        reply(["アルバイト・パートのお仕事で考えますね。いくつか伺いながら一緒に整理します。",
               "今回、バイトを探そうと思ったきっかけは何ですか?"], quick_replies=["それ以外", "今はいい"]),
        call(*save({"purpose": "生活費の足し"})),
        reply(["生活費の足しにしたいのですね。", "そう思ったのは、どんな変化からですか?"], quick_replies=["それ以外", "今はいい"]),
        *([call(*save({"purpose": PURPOSE}))] if h else []),
        reply(["家賃の分を自分で払いたいのですね。", "やってみたいことや、好きなことはありますか?"],
              quick_replies=["それ以外", "今はいい"]),
        # 直した決まり: 掘り下げの答え(目的)は、次に保存するターンで新しい項目と1回の save にまとめる
        call(*save({"wants": "パン屋で働いてみたい"})) if h else call(*save({"wants": "パン屋で働いてみたい", "purpose": PURPOSE})),
        reply(["パン屋で働いてみたいのですね。", "パン屋のどんなところに惹かれますか?"], quick_replies=["それ以外", "今はいい"]),
        *([call(*save({"wants": WANTS}))] if h else []),
        reply(["お客さんに渡すところなのですね。", "働ける時間帯とエリアはどのあたりですか?"],
              quick_replies=["平日の夕方", "それ以外", "今はいい"]),
        *([call(*save({"time_band": "夕方", "area": "池袋"}), "s1"), call(*save({"time_band": "平日の夕方"}), "s2")] if h
          else [call(*save({"time_band": "平日の夕方", "area": "池袋", "wants": WANTS}))]),
        reply(["家賃の分を払いたい・パン屋・池袋の平日夕方で整理しました。", "この内容で求人を見ますか?"],
              quick_replies=["求人を見る", "もう少し相談したい", "今はいい"]),
        # keywords は意味検索で embedding の API を呼ぶので、オフラインの台本では渡さない
        *([call("recommend_jobs", {"line_user_id": UID, "employment_type": BAITO, "area": "池袋"}, "r1"),
           calls(("recommend_jobs", {"line_user_id": UID, "employment_type": BAITO, "area": "池袋", "time_band": "夕方"}),
                 RECORD)] if h
          else [calls(("recommend_jobs", {"line_user_id": UID, "employment_type": BAITO, "area": "池袋",
                                          "time_band": "夕方"}), RECORD)]),
        lambda msgs: reply(["パン屋で働いてみたいというお話から、池袋の夕方の求人です。"], job_ids=returned_jobs(msgs),
                           quick_replies=CLOSING),
    ]


def run_script(script: list, said: list, tmp_path) -> tuple[list[dict], int]:
    """台本を回し、ターンと LLM の呼び出しの回数(台本の要素の数)を返す。"""
    model = ConsultModel(script=script)
    n = len(model.script)
    turns = run(model, [(UID, "visit", None)] + [(UID, "message", m) for m in said[1:]], tmp_path)
    assert model.script == []  # 台本を使い切った
    return turns, n


def test_consult_saves_dig_answers_with_the_next_call_and_calls_fewer_times(tmp_path):
    h_turns, h_calls = run_script(consult_script(h=True), CONSULT_SAID, tmp_path / "h")
    turns, llm_calls = run_script(consult_script(h=False), CONSULT_SAID, tmp_path / "new")

    # H の呼び方は、評価の違反ではないが、呼び出しの重複として数える
    assert conversation_checks(CONSULT_SC, h_turns, CONSULT_SAID)["violations"] == []
    assert call_overlaps(h_turns, CONSULT_SAID) == [
        "T4: 掘り下げの答えだけで save_user_preferences を呼んだ(保存済みの purpose)",
        "T6: 掘り下げの答えだけで save_user_preferences を呼んだ(保存済みの wants)",
        "T7: 同じターンで save_user_preferences を2回呼んだ",
        "T8: 同じターンで条件を変えて recommend_jobs を2回呼んだ",
    ]
    # 直した決まりの台本: 違反も重複も無く、save・検索・LLM の呼び出しが減った
    r = conversation_checks(CONSULT_SC, turns, CONSULT_SAID)
    assert r["violations"] == [] and r["call_overlaps"] == [] and r["outcomes"] == ["propose"]
    assert (count(h_turns, "save_user_preferences"), count(turns, "save_user_preferences")) == (6, 3)
    assert (count(h_turns, "recommend_jobs"), count(turns, "recommend_jobs")) == (2, 1)
    assert (h_calls, llm_calls) == (17, 13)
    # 掘り下げの答えのターンはツールを呼ばない(返信だけの1回)
    assert turns[3]["tools"] == [] and turns[5]["tools"] == []
    # 相談の深さは変えていない: 同じ問い(目的・掘り下げ・やりたいこと・掘り下げ・条件)を同じ順で聞いた
    assert [x["bubbles"] for x in turns] == [x["bubbles"] for x in h_turns]
    # 掘り下げの答えも、まとめた保存でプロフィールに残る(H と同じ中身)
    assert stated(tmp_path / "new", UID) == stated(tmp_path / "h", UID) | {"time_band": "平日の夕方"}
    assert stated(tmp_path / "new", UID)["purpose"] == PURPOSE and stated(tmp_path / "new", UID)["wants"] == WANTS


# ----------------------------- (b) 条件から探す: playbook を読まず、絞り込みは条件をまとめて聞く -----------------------------

TRACK_Q = "正社員のお仕事とアルバイト・パート、どちらで探しますか?"
TRACK_REPLIES = ["正社員", "アルバイト・パート", "まだ決めていない", "今はいい"]
NARROW = ["勤務地や時間帯で絞る", "ほかの求人を探す", "今はいい"]
SEARCH_SC = {"expect_outcome": ["propose"]}


def search_script(h: bool) -> tuple[list, list]:
    """「求人を探す」から条件で探す(相談に入らない)。h=True は H の呼び方: アルバイト・パートへの切り替えで playbook を読み、
    絞り込みでは勤務地と時間帯を1つずつ聞いて、そのたびに探し直す。"""
    # 場所を問わない人(task 224: エリアが分からないまま探すのは、本人がエリアを問わないと言ったときだけ)
    said = [None, "求人を探す", "アルバイト・パート", "カフェで探したいです。場所はどこでもいいです", "勤務地や時間帯で絞る"]
    said += ["渋谷です", "日中です"] if h else ["渋谷で、日中がいいです"]
    switch = ("set_search_track", {"line_user_id": UID, "track": BAITO, "evidence": "アルバイト・パート"})

    def search(prefs: dict, **cond):
        return calls(save(prefs), ("recommend_jobs", {"line_user_id": UID, "employment_type": BAITO, **cond}),
                     ("record_conversation_outcome", {"line_user_id": UID, "outcome": "propose",
                                                      "reason": said[-1], "confidence": 0.9}))

    script = [
        reply(["はじめまして。お仕事探しをお手伝いします。どうしますか?"], quick_replies=MENU),
        reply([TRACK_Q], quick_replies=TRACK_REPLIES),
        switch_and_read(UID, BAITO, "アルバイト・パート") if h else call(*switch),
        reply(["アルバイト・パートのお仕事で考えますね。", "どんなお仕事で探しますか?"], quick_replies=["カフェ", "それ以外", "今はいい"]),
        search({"job_type": "カフェ"}, job_type="カフェ"),
        lambda msgs: reply(["カフェのお仕事です。"], job_ids=returned_jobs(msgs), quick_replies=NARROW),
    ]
    if h:
        script += [
            reply(["勤務地はどのあたりがよいですか?"], quick_replies=["渋谷", "それ以外", "今はいい"]),
            search({"area": "渋谷"}, job_type="カフェ", area="渋谷"),
            lambda msgs: reply(["渋谷のカフェです。", "時間帯の希望はありますか?"], job_ids=returned_jobs(msgs),
                               quick_replies=["日中", "それ以外", "今はいい"]),
            search({"time_band": "日中"}, job_type="カフェ", area="渋谷", time_band="日中"),
            lambda msgs: reply(["渋谷・日中のカフェです。"], job_ids=returned_jobs(msgs), quick_replies=CLOSING),
        ]
    else:  # 直した決まり: まだ分かっていない勤務地と時間帯をまとめて1問で聞き、答えの条件を全部入れて1回で探す
        script += [
            reply(["勤務地と時間帯は、どのあたりがよいですか?"], quick_replies=["渋谷の日中", "それ以外", "今はいい"]),
            search({"area": "渋谷", "time_band": "日中"}, job_type="カフェ", area="渋谷", time_band="日中"),
            lambda msgs: reply(["渋谷・日中のカフェです。"], job_ids=returned_jobs(msgs), quick_replies=CLOSING),
        ]
    return script, said


def test_search_without_consult_skips_the_playbook_and_narrows_in_one_search(tmp_path):
    h_script, h_said = search_script(h=True)
    script, said = search_script(h=False)
    h_turns, h_calls = run_script(h_script, h_said, tmp_path / "h")
    turns, llm_calls = run_script(script, said, tmp_path / "new")

    assert conversation_checks(SEARCH_SC, h_turns, h_said)["violations"] == []
    assert call_overlaps(h_turns, h_said) == [
        "T3: 相談に入る前の切り替えで playbook を読んだ(アルバイト・パート)",
        "T6: 前の検索(T4)の条件に area だけ足して探し直した",
        "T7: 前の検索(T6)の条件に time_band だけ足して探し直した",
    ]
    # 直した決まりの台本: 相談に入っていないアルバイト・パートへの切り替えでは playbook を読まなくても違反ではない
    r = conversation_checks(SEARCH_SC, turns, said)
    assert r["violations"] == [] and r["call_overlaps"] == [] and r["tracks"][2] == BAITO
    assert (count(h_turns, "get_counseling_playbook"), count(turns, "get_counseling_playbook")) == (1, 0)
    assert (count(h_turns, "save_user_preferences"), count(turns, "save_user_preferences")) == (3, 2)
    assert (count(h_turns, "recommend_jobs"), count(turns, "recommend_jobs")) == (3, 2)
    assert (len(h_turns), len(turns)) == (7, 6) and (h_calls, llm_calls) == (11, 9)
    # 同じ求人に、1ターン少なく届いた
    assert turns[-1]["cards"] and [c["job_id"] for c in turns[-1]["cards"]] == [c["job_id"] for c in h_turns[-1]["cards"]]


# ----------------------------- (c) 数え方: H の保存済みの会話と、数えない場面 -----------------------------


SCENES = ("掘り下げの答えだけで save_user_preferences を呼んだ", "同じターンで save_user_preferences",
          "区分が変わっていないのに playbook を読み直した", "相談に入る前の切り替えで playbook を読んだ",
          "同じターンで条件を変えて recommend_jobs", "だけ足して探し直した")


def test_recount_of_the_h_baseline_by_scene():
    """H の評価の保存済みの会話(turn_log)を、call_overlaps で数え直す(実 API なし)。PR に書いた場面ごとの回数。"""
    doc = json.loads(H_BASELINE.read_text(encoding="utf-8"))
    found = {r["case"]: call_overlaps(r["detail"]["turn_log"], r["detail"]["user_msgs"])
             for r in doc["runs"] if r["status"] == "ok"}
    scenes = Counter(next(k for k in SCENES if k in v) for vs in found.values() for v in vs)
    assert scenes == {
        "掘り下げの答えだけで save_user_preferences を呼んだ": 8,
        "相談に入る前の切り替えで playbook を読んだ": 3,
        "同じターンで条件を変えて recommend_jobs": 3,
        "だけ足して探し直した": 2,  # R-revisit-track: カフェ → 勤務地 → 時間帯と1つずつ足した
        "同じターンで save_user_preferences": 1,
    }
    assert sum(len(v) for v in found.values()) == 17
    assert found["R-revisit-track"] == ["T4: 前の検索(T2)の条件に area だけ足して探し直した",
                                        "T5: 前の検索(T4)の条件に time_band だけ足して探し直した"]
    # 区分が変わっていないのに playbook を読み直した回は無い。正社員への切り替えで読んだのは重複に数えない
    assert not any("読み直した" in v for vs in found.values() for v in vs)
    assert not any("T3" in v and "playbook" in v for v in found["T-switch-to-fulltime"])
    # 直した playbook の決まりでも、H の会話に読み忘れの違反は出ない
    assert all(playbook_violations(r["detail"]["turn_log"], r["detail"]["user_msgs"]) == [] for r in doc["runs"])


def t(bubbles, tools=(), quick_replies=("今はいい",)):
    return {"bubbles": list(bubbles), "quick_replies": list(quick_replies), "cards": [], "buttons": [],
            "tools": [{"name": n, "args": a, "result": res} for n, a, res in tools], "unknown_job_ids": []}


def _switch(track):
    return ("set_search_track", {"track": track}, {"track": track, "changed": True})


def _read(track):
    return ("get_counseling_playbook", {"track": track, "channel": "chat"}, {"track": track})


def _save(prefs):
    return ("save_user_preferences", {"preferences": prefs}, {})


def _rec(**cond):
    return ("recommend_jobs", cond, {"jobs": []})


def test_what_is_not_counted_as_an_overlap():
    said = [None, "まず相談する", "生活費のため", "やっぱり渋谷じゃなくて新宿で", "渋谷か池袋で、カフェを"]
    turns = [t(["どうしますか?"], quick_replies=MENU),
             t(["いくつか伺いながら一緒に整理します。きっかけは?"], [_switch(BAITO), _read(BAITO), _save({"area": "渋谷"})]),
             t(["生活費ですね。例えば?"], [_save({"purpose": "生活費のため"})]),
             # 言い直した条件を保存し直すのは、掘り下げの答えではない
             t(["新宿ですね。時間帯は?"], [_save({"area": "新宿"})]),
             # 本人が挙げたエリアを並べて探すのは、条件を変えた探し直しではない
             t(["2件です。"], [_rec(area="渋谷", job_type="カフェ"), _rec(area="池袋", job_type="カフェ")])]
    assert call_overlaps(turns, said) == []
    # 相談の途中で同じ区分を読み直す・本人が挙げていないエリアで探し直すのは数える
    turns[2]["tools"].append({"name": "get_counseling_playbook", "args": {"track": BAITO}, "result": {"track": BAITO}})
    turns[4]["tools"][1]["args"] = {"area": "横浜", "job_type": "カフェ"}
    assert call_overlaps(turns, said) == ["T3: 区分が変わっていないのに playbook を読み直した(アルバイト・パート)",
                                          "T5: 同じターンで条件を変えて recommend_jobs を2回呼んだ"]


def test_playbook_reread_is_required_in_a_consult_or_on_switching_to_fulltime():
    said = [None, "求人を探す", "アルバイト・パート", "やっぱり正社員で"]
    turns = [t(["どうしますか?"], quick_replies=MENU), t([TRACK_Q]), t(["バイトで考えますね。どこで?"], [_switch(BAITO)]),
             t(["正社員で考えますね。これまでのお仕事は?"], [_switch(SEISHAIN)])]
    # 相談に入る前のアルバイト・パートへの切り替えは読まなくてよい。正社員への切り替えは読む
    assert playbook_violations(turns, said) == ["T4: 区分を「正社員」に切り替えたのに、同じターンで playbook を読み直していない"]
    turns[3]["tools"].append({"name": "get_counseling_playbook", "args": {"track": SEISHAIN}, "result": {"track": SEISHAIN}})
    assert playbook_violations(turns, said) == []
    # 相談に入っていたら(本人の合図・入口の1文・前に読んだ playbook)、アルバイト・パートへの切り替えでも読む
    v = playbook_violations(turns, [None, "まず相談する", "アルバイト・パート", "やっぱり正社員で"])
    assert v == ["T3: 区分を「アルバイト・パート」に切り替えたのに、同じターンで playbook を読み直していない"], v
    opened = [turns[0], t(["いくつか伺いながら一緒に整理します。どちらで探しますか?"]), *turns[2:]]
    assert playbook_violations(opened, said) == [
        "T3: 区分を「アルバイト・パート」に切り替えたのに、同じターンで playbook を読み直していない"]
    # 相談に入らずに正社員へ切り替えて正社員の playbook を読んだ後は、アルバイト・パートへの切り替え・取り消しでも読み直す
    said = [None, "求人を探す", "正社員", "やっぱりバイトで"]
    back = [t(["どうしますか?"], quick_replies=MENU), t([TRACK_Q]),
            t(["正社員で考えますね。これまでのお仕事は?"], [_switch(SEISHAIN), _read(SEISHAIN)]),
            t(["アルバイト・パートで考えますね。どこで?"], [_switch(BAITO)])]
    assert playbook_violations(back, said) == [
        "T4: 区分を「アルバイト・パート」に切り替えたのに、同じターンで playbook を読み直していない"]
    back[3]["tools"][0]["args"]["undo"] = True  # 取り消しで戻したときも同じ
    assert playbook_violations(back, said) == [
        "T4: 区分を「アルバイト・パート」に切り替えたのに、同じターンで playbook を読み直していない"]
    back[3]["tools"].append({"name": "get_counseling_playbook", "args": {"track": BAITO}, "result": {"track": BAITO}})
    assert playbook_violations(back, said) == [] and call_overlaps(back, said) == []
