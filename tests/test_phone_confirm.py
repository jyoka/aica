"""「電話はしたくない」を人と話す量の上限に当てず、1問で確かめる(task 126)の台本テスト。実 API なし、dip の MCP は本物。

人と話す量の3には接客(レジ・ホール・販売)と電話がまとめて入っている。「電話はしたくない」を max_talk_level=2 に当てると、
「電話は嫌だがレジは平気」な人からも店頭の求人が消える(アルバイト・パート63件 → 24件)。そこで、
(1) どこの対応表(チャットの Prompt・MCP の引数の説明・音声の propose_jobs の説明と playbook)にも「電話はしたくない」が無く、
    チャットと音声の両方に「接客まで避けたいかを1問で確かめる」がある
(2) チャットの台本: 「電話はしたくない」には引数を付けずに1問確かめ、「接客も苦手」なら max_talk_level=2、
    「レジや接客は大丈夫」なら人と話す量で絞らない(電話が中心の求人は勧めない)
(3) 音声の台本(LiveKit の AgentSession): 同じ2通り
を確かめる。

task 127: 電話だけを避けたい人に、電話が中心の求人(コールセンター)を出さない。コールセンターが上位に来る条件
(深夜。dip の上位3件は b0401 コンビニ・b0903 コールセンター・b0203 飲食キッチン)で、
(2) チャットはカードから除き、(4) 音声は propose_jobs の exclude_ids で外して呼び直し、トークに届く提案に入れない。
(5) 音声の advisor は外した求人を覚え、後で exclude_ids を付けずに条件を変えて探しても出さない。
(6) 「ほかには?」で出した求人を外して探し直し0件なら、前の提案を残す(トークに届き、dip の記録は propose)。

task 131: 本人が外した求人・前の提案の求人に戻りたがったとき、届かない求人を「トークに出ます」と言わない。
(8) 音声の playbook に答え方があり、propose_jobs の結果に、トークに届く求人IDと外した求人ID(読み上げない)が入る。
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from dip_mcp import logic, server
from dip_mcp.data import ROOT, State, load_jobs
from evals.checks import conversation_checks
from line_runtime.runtime import _tool_payload
from tests.test_consult import ConsultModel, calls, run, switch_and_read
from tests.test_handle import reply
from tests.test_talk_page import app  # noqa: F401  フィクスチャ
from tests.test_voice_track import _scripted_llm
from tests.voice.test_advisor import _FakeDraftClient, _FakePoster
from voice_resume import config
from voice_resume.advisor import ResumeAdvisor
from voice_resume.agent import prepare_advisor
from voice_resume.dip import DipMCP

BAITO = "アルバイト・パート"
PROMPT = (ROOT / "dip_mcp" / "prompts" / "baitoru_assistant.md").read_text(encoding="utf-8")
COMMON_VOICE = (ROOT / "dip_mcp" / "playbooks" / "common_voice.md").read_text(encoding="utf-8")
PHONE = "「電話はしたくない」"
CONFIRM = "お客様と話すレジや接客は大丈夫ですか?"
CONFIRM_FULLTIME = "お客様や取引先と話す仕事は大丈夫ですか?"  # 正社員の人と話す量の3は営業・企画・教育など(task 109 の計測)
CONFIRM_REPLIES = ["レジや接客は大丈夫", "接客も苦手", "今はいい"]
CLOSING = ["求人を探す", "応募履歴を確認", "使い方"]
JOBS = load_jobs()
# 本人の答え -> 付けるべき max_talk_level(None = 人と話す量では絞らない)
ANSWERS = {"接客も苦手です": 2, "レジや接客は大丈夫です": None}
OK_SERVICE = "レジや接客は大丈夫です"
CALL_CENTER = "b0903"  # 深夜で探すと上位3件に入るコールセンター


def _talk_level_description() -> str:
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    return tools["recommend_jobs"].inputSchema["properties"]["max_talk_level"]["description"]


def _count(**kw) -> int:
    return sum(1 for j in JOBS.values()
               if (kw.get("employment_type") is None or j.employment_type == kw["employment_type"])
               and (kw.get("time_band") is None or kw["time_band"] in j.time_bands)
               and j.talk_level <= kw.get("max_talk_level", 3))


# ----------------------------- (1) 対応表から外し、1問で確かめる側に移した -----------------------------


def test_phone_is_in_no_mapping_and_both_channels_confirm_with_one_question():
    mapping = next(line for line in PROMPT.splitlines() if line.startswith("- 人と話す量:"))
    assert "電話" not in mapping and "「接客はしたくない」は max_talk_level=2" in mapping
    assert PHONE not in _talk_level_description()
    assert PHONE not in (inspect.getdoc(ResumeAdvisor.propose_jobs) or "")
    # 1問で確かめる側: チャットの Prompt の「迷うとき」と、音声の区分に共通の決まりに、同じ問いの例がある
    for where, text in {"Prompt": PROMPT, "common_voice": COMMON_VOICE}.items():
        rule = next(line for line in text.splitlines() if PHONE in line)
        assert CONFIRM in rule and "max_talk_level=2" in rule and "人と話す量では絞らず" in rule, where
        assert CONFIRM_FULLTIME in rule and "正社員の区分" in rule, where  # 正社員にはレジが無い
    # 引数の説明(チャットの MCP・音声のツール)も、電話だけなら付けないと言う
    for doc in (_talk_level_description(), inspect.getdoc(ResumeAdvisor.propose_jobs) or ""):
        doc = re.sub(r"\s+", "", doc)  # docstring の折り返しを詰める
        assert "電話だけを避けたいときは付けない" in doc and "1問で確かめ" in doc
    # 当てたときに消える店頭の求人(task 111 のレビューで確かめた数)
    assert (_count(employment_type=BAITO), _count(employment_type=BAITO, max_talk_level=2)) == (63, 24)


# ----------------------------- (2) チャットの台本 -----------------------------


def _cards_without_call_centers(messages) -> list[str]:
    """直近の recommend_jobs の結果から、電話が中心の求人(コールセンター)を除いた最大3件。"""
    tool = next(m for m in reversed(messages) if m.type == "tool" and m.name == "recommend_jobs")
    return [j["job_id"] for j in _tool_payload(tool)["jobs"] if j["job_type"] != "コールセンター"][:3]


@pytest.mark.parametrize(("answer", "time_band"), [*((a, None) for a in ANSWERS), (OK_SERVICE, "深夜")])
def test_chat_confirms_service_with_one_question_before_setting_the_talk_level(tmp_path, answer, time_band):
    """time_band="深夜" は、コールセンター(b0903)が上位3件に入る条件(カードから除く処理が実際に動く)。"""
    uid = "sim-new"
    level = ANSWERS[answer]
    cond = {"employment_type": BAITO, **({"time_band": time_band} if time_band else {}),
            **({"max_talk_level": level} if level else {})}
    search = {"line_user_id": uid, **cond}
    model = ConsultModel(script=[
        switch_and_read(uid, BAITO, "アルバイトを探しています",
                        ("save_user_preferences", {"line_user_id": uid, "preferences": {"avoids": "電話"}})),
        reply(["アルバイト・パートで、合うお仕事を考えますね。", CONFIRM], quick_replies=CONFIRM_REPLIES),
        calls(("recommend_jobs", search),
              ("record_conversation_outcome", {"line_user_id": uid, "outcome": "propose",
                                               "reason": "電話を避けたい人に求人を提案", "confidence": 0.8})),
        lambda msgs: reply(["電話の少ない求人です。"], job_ids=_cards_without_call_centers(msgs), quick_replies=CLOSING),
    ])
    # 場所を問わない人(task 224: エリアが分からないまま探すのは、本人がエリアを問わないと言ったときだけ)
    said = [f"{time_band or ''}{'で' if time_band else ''}アルバイトを探しています。場所はどこでもいいです。電話はしたくないです",
            answer]
    turns = run(model, [(uid, "message", m) for m in said], tmp_path)

    assert CONFIRM in model.systems[0]  # 1問で確かめる指示が LINE 側エージェントに届いている
    # 「電話はしたくない」のターンでは探さず、接客まで避けたいかの1問だけを聞く
    assert "recommend_jobs" not in [t["name"] for t in turns[0]["tools"]]
    assert [b for b in turns[0]["bubbles"] if "?" in b or "?" in b] == [CONFIRM]
    # 答えを受けたターンで引数を決める
    rec = next(t for t in turns[1]["tools"] if t["name"] == "recommend_jobs")
    assert rec["args"].get("max_talk_level") == level
    assert rec["result"]["total"] == _count(**cond)
    shown = [JOBS[c["job_id"]] for c in turns[1]["cards"]]
    assert shown and all(j.job_type != "コールセンター" for j in shown)
    found = [j["job_id"] for j in rec["result"]["jobs"]]
    dropped = [i for i in found if i not in {j.job_id for j in shown}]
    # 深夜では dip の上位3件にコールセンターが入り、カードからはそれだけが除かれる
    assert dropped == ([CALL_CENTER] if time_band else [])
    if level:
        assert all(j.talk_level <= 2 for j in shown)
    checks = conversation_checks({"expect_outcome": ["propose"]}, turns, said)
    assert checks["violations"] == [] and checks["outcomes"] == ["propose"]


# ----------------------------- (3) 音声の台本 -----------------------------


@pytest.mark.parametrize("answer", list(ANSWERS))
def test_voice_confirms_service_with_one_question_before_setting_the_talk_level(app, answer):  # noqa: F811
    """LiveKit の AgentSession(文字のターン)で、本物の dip MCP につなぐ。区分は未定(両方の区分から探す)。"""
    from livekit.agents import AgentSession

    uid = "w-6666666666666666"
    level = ANSWERS[answer]
    model = _scripted_llm([
        ("tool", "save_user_preferences", {"avoids": "電話"}),
        ("say", CONFIRM),
        ("tool", "propose_jobs", {"max_talk_level": level} if level else {}),
        ("say", "見つかった求人を1件ずつお伝えしますね。"),
    ])

    async def go():
        dip = DipMCP()
        await dip.start()
        try:
            advisor, _ = await prepare_advisor(dip, uid)
            session = AgentSession(llm=model)
            await session.start(agent=advisor)
            await session.run(user_input="電話はしたくないです")
            before = list(advisor.search_attempts)
            await session.run(user_input=answer)
            await session.aclose()
            return advisor, before
        finally:
            await dip.aclose()

    advisor, before = asyncio.run(go())
    assert CONFIRM in model.instructions[0]  # 音声の playbook(区分に共通の決まり)が指示に入っている
    assert before == []  # 「電話はしたくない」の発話では探さず、1問で確かめた
    (attempt,) = advisor.search_attempts
    assert attempt["args"].get("max_talk_level") == level
    assert attempt["found"] == _count(**({"max_talk_level": level} if level else {}))
    if level:
        assert all(JOBS[j].talk_level <= 2 for j in attempt["proposed_job_ids"])


# ----------------------------- (4) 音声: 電話が中心の求人を exclude_ids で外す(task 127) -----------------------------


def test_voice_playbook_says_how_to_exclude_phone_centred_jobs():
    rule = next(line for line in COMMON_VOICE.splitlines() if line.startswith("- 電話だけを避けたい人に"))
    assert "exclude_ids" in rule and "search_only=True" in rule and "読み上げず" in rule
    doc = re.sub(r"\s+", "", inspect.getdoc(ResumeAdvisor.propose_jobs) or "")
    assert "電話が中心の求人を外すとき、すでに出した求人を外すとき" in doc


def test_dip_relax_does_not_count_excluded_jobs():
    """0件のときの「外すと何件」も、exclude_ids の求人を数えない(音声の propose_jobs は同じ exclude_ids を渡す)。"""
    state = server.state
    base = logic.relax(state, None, time_band="深夜", job_type="コールセンター")
    excluded = logic.relax(state, None, time_band="深夜", job_type="コールセンター", exclude_ids=[CALL_CENTER])
    assert base["total_now"] == 1 and excluded["total_now"] == 0
    after = {o["remove"]: o["total_after"] for o in base["options"]}
    assert {o["remove"]: o["total_after"] + 1 for o in excluded["options"]} == after


def _run_voice(uid: str, model, said: list[str], tmp_path, monkeypatch):
    """LiveKit の AgentSession(文字のターン)で本物の dip MCP につなぎ、said を順に話す。
    (advisor, ツールの結果の文, トークへの送信)。"""
    from livekit.agents import AgentSession

    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    poster = _FakePoster()

    async def go():
        dip = DipMCP()
        await dip.start()
        try:
            advisor, _ = await prepare_advisor(dip, uid)
            advisor._talk_poster = poster
            advisor._draft_client_factory = _FakeDraftClient
            session = AgentSession(llm=model)
            await session.start(agent=advisor)
            for text in said:
                await session.run(user_input=text)
            outputs = [i.output for i in session.history.items if i.type == "function_call_output"]
            await session.aclose()
            return advisor, outputs
        finally:
            await dip.aclose()

    advisor, outputs = asyncio.run(go())
    return advisor, outputs, poster


def _delivered(poster) -> list[str]:
    ((_, payload),) = poster.calls
    return [j["job_id"] for j in payload["jobs"]]


def test_voice_excludes_call_centers_from_the_proposal_delivered_to_the_talk(app, tmp_path, monkeypatch):  # noqa: F811
    """「深夜で探したい。電話はしたくない」→ 1問 →「レジは大丈夫」。深夜の上位3件に入るコールセンターを exclude_ids で外して
    呼び直し、トークに届く提案に b0903 が入らない。LiveKit の AgentSession(文字のターン)で、本物の dip MCP につなぐ。"""
    model = _scripted_llm([
        ("tool", "save_user_preferences", {"time_band": "深夜", "avoids": "電話"}),
        ("say", CONFIRM),
        ("tool", "propose_jobs", {"time_band": "深夜"}),
        # 結果にコールセンターがあるので読み上げず、同じ応答でその求人IDを外して呼び直す(playbook の手順)
        ("tool", "propose_jobs", {"time_band": "深夜", "exclude_ids": [CALL_CENTER], "search_only": True}),
        ("say", "見つかった求人を1件ずつお伝えしますね。"),
        ("tool", "finalize_interview", {"summary": "深夜で探している。電話は避けたいが、レジや接客は大丈夫。"}),
        ("say", "ありがとうございました。"),
    ])
    advisor, outputs, poster = _run_voice(
        "w-7777777777777777", model, ["深夜で探したいです。電話はしたくないです", "レジは大丈夫です", "以上です"],
        tmp_path, monkeypatch)
    assert "exclude_ids" in model.instructions[0]  # 外す手順(区分に共通の決まり)が指示に入っている
    first, second = advisor.search_attempts
    # 外す前: 深夜の上位3件にコールセンターが入り、結果にその求人IDと職種が出る(外す求人IDをここで知る)
    assert CALL_CENTER in first["proposed_job_ids"] and "exclude_ids" not in first
    assert f"(求人ID {CALL_CENTER}): コールセンターのお仕事" in next(o for o in outputs if "件見つかりました" in o)
    # 外した後: 希望の条件は同じまま、コールセンターの無い3件
    assert second["args"] == first["args"] == {"time_band": "深夜"}
    assert second["exclude_ids"] == [CALL_CENTER] and len(second["proposed_job_ids"]) == 3
    assert all(JOBS[i].job_type != "コールセンター" for i in second["proposed_job_ids"])
    assert second["found"] == first["found"] - 1
    # トークに届く提案は、外した後の3件(b0903 は入らない)
    delivered = _delivered(poster)
    assert delivered == second["proposed_job_ids"] and CALL_CENTER not in delivered


# ----------------------------- (5) 音声: 外した求人を advisor が覚える(task 127) -----------------------------


def test_voice_keeps_excluded_jobs_out_when_searching_again_without_exclude_ids(app, tmp_path, monkeypatch):  # noqa: F811
    """コールセンターを外した後、本人が「川崎がいい」と条件を変え、LLM が exclude_ids を付け忘れて呼んでも、
    外した b0903 は出ない(深夜・川崎では b0903 が1位に来る)。"""
    model = _scripted_llm([
        ("tool", "save_user_preferences", {"time_band": "深夜", "avoids": "電話"}),
        ("say", CONFIRM),
        ("tool", "propose_jobs", {"time_band": "深夜"}),
        ("tool", "propose_jobs", {"time_band": "深夜", "exclude_ids": [CALL_CENTER], "search_only": True}),
        ("say", "見つかった求人を1件ずつお伝えしますね。"),
        ("tool", "propose_jobs", {"time_band": "深夜", "area": "川崎"}),  # exclude_ids を付け忘れた
        ("say", "川崎で見つかった求人をお伝えしますね。"),
        ("tool", "finalize_interview", {"summary": "深夜で川崎で探している。電話は避けたいが、レジや接客は大丈夫。"}),
        ("say", "ありがとうございました。"),
    ])
    advisor, _, poster = _run_voice(
        "w-8888888888888888", model,
        ["深夜で探したいです。電話はしたくないです", "レジは大丈夫です", "川崎がいいです", "以上です"],
        tmp_path, monkeypatch)
    assert logic.recommend(server.state, None, time_band="深夜", area="川崎")["jobs"][0]["job_id"] == CALL_CENTER
    *_, third = advisor.search_attempts
    assert third["args"] == {"time_band": "深夜", "area": "川崎"} and third["exclude_ids"] == [CALL_CENTER]
    assert third["proposed_job_ids"] and CALL_CENTER not in third["proposed_job_ids"]
    delivered = _delivered(poster)
    assert delivered == third["proposed_job_ids"] and CALL_CENTER not in delivered


# ----------------------------- (6) 音声: 外して0件なら前の提案を残す(task 127) -----------------------------


def test_voice_keeps_the_previous_proposal_when_excluding_shown_jobs_finds_nothing(app, tmp_path, monkeypatch):  # noqa: F811
    """「中野のコンビニ」で2件出した後、「ほかには?」で2件を外して探し直すと0件。本人が聞いた2件はトークに届き、
    dip の記録は propose(consult(提案できず)にしない)。ユーザーの決定(2026-10-04)。"""
    uid = "w-9999999999999999"
    shown = [j["job_id"] for j in logic.recommend(server.state, None, job_type="コンビニ", area="中野")["jobs"]]
    assert len(shown) == 2
    model = _scripted_llm([
        ("tool", "propose_jobs", {"job_type": "コンビニ", "area": "中野"}),
        ("say", "見つかった求人を1件ずつお伝えしますね。"),
        ("tool", "propose_jobs", {"job_type": "コンビニ", "area": "中野", "exclude_ids": shown, "search_only": True}),
        ("say", "ほかには見つかりませんでした。"),
        ("tool", "finalize_interview", {"summary": "中野でコンビニのバイトを探している。"}),
        ("say", "ありがとうございました。"),
    ])
    advisor, outputs, poster = _run_voice(
        uid, model, ["中野でコンビニのバイトを探しています", "ほかにはありますか?", "以上です"], tmp_path, monkeypatch)
    first, second = advisor.search_attempts
    assert first["proposed_job_ids"] == shown and second["found"] == 0 and second["exclude_ids"] == shown
    assert any("見つかりませんでした" in o for o in outputs)  # 0件の返答(relax_conditions の結果)が LLM に届いた
    assert _delivered(poster) == shown
    outcomes = State(tmp_path / "state.json").user(uid)["outcomes"]
    assert [o["outcome"] for o in outcomes] == ["propose"]


# ----------------------------- (7) dip: relax_conditions が exclude_ids を数えない(task 127) -----------------------------


def test_dip_server_relax_conditions_passes_exclude_ids():
    """MCP の relax_conditions(server)が exclude_ids を logic に渡す。深夜のコールセンターは b0903 の1件だけ。"""
    cond = {"time_band": "深夜", "job_type": "コールセンター"}
    assert server.relax_conditions("sim-new", **cond)["total_now"] == 1
    assert server.relax_conditions("sim-new", **cond, exclude_ids=[CALL_CENTER])["total_now"] == 0


# ----------------------------- (8) 音声: 前の求人に戻りたがったときの答え方(task 131) -----------------------------


BACK_RULE = "- 本人が外した求人や前の提案の求人に戻りたがったら"


def _note(output: str) -> str:
    """propose_jobs の結果のうち、読み上げない部分(トークに届く求人と届かない求人)。"""
    return output[output.index("(読み上げない:"):]


def test_voice_playbook_says_how_to_answer_when_going_back_to_an_earlier_job():
    rule = next(line for line in COMMON_VOICE.splitlines() if line.startswith(BACK_RULE))
    assert "この面談ではもうご紹介できない" in rule  # 外した求人は出せないと伝える
    assert "トークに届くのは最後に出した求人だけ" in rule and "「トークに出ます」" in rule and "言わない" in rule
    assert "読み上げない" in rule  # 結果の読み上げない部分を見て答える
    # 外して0件だったときは前の提案が残ってトークに届くので、「ご紹介できない」だけで終わらせない
    assert "「前の提案としてトークに届く」" in rule and "もう検索では出せないがトークには届く" in rule
    # 求人の読み上げの決まりも、トークに出るのは最後に出した求人だと言う(「求人は〜トークにも出る」にしない)
    assert "最後に出した求人は面談が終わるとトークにも出る" in COMMON_VOICE
    assert "求人は面談が終わるとトークにも出る" not in COMMON_VOICE.replace("最後に出した求人は", "")


def test_voice_tells_the_llm_which_jobs_reach_the_talk_after_showing_others(app, tmp_path, monkeypatch):  # noqa: F811
    """probe-20261004-114613 の V-phone-night: 深夜でコールセンターを外し、「ほかには?」で出した3件(最初が中野のコンビニ
    b0401)を外して探し直した後に、本人が「さっきの最初の求人に戻りたい」と言う。最後の結果文に、トークに届くのは
    最後の3件だけで、b0401 は外したのでこの面談では出せず届かないことが入る。LiveKit の AgentSession(文字のターン)で、
    本物の dip MCP につなぐ。"""
    night = [j["job_id"] for j in logic.recommend(server.state, None, time_band="深夜", exclude_ids=[CALL_CENTER])["jobs"]]
    assert night[0] == "b0401" and JOBS["b0401"].job_type == "コンビニ"  # 最初に読み上げるのが中野のコンビニ
    excluded = [CALL_CENTER, *night]
    model = _scripted_llm([
        ("tool", "save_user_preferences", {"time_band": "深夜", "avoids": "電話"}),
        ("say", CONFIRM),
        ("tool", "propose_jobs", {"time_band": "深夜"}),
        ("tool", "propose_jobs", {"time_band": "深夜", "exclude_ids": [CALL_CENTER], "search_only": True}),
        ("say", "見つかった求人を1件ずつお伝えしますね。"),
        ("tool", "propose_jobs", {"time_band": "深夜", "exclude_ids": excluded, "search_only": True}),
        ("say", "別の求人を1件ずつお伝えしますね。"),
        ("say", "最初の中野のコンビニは外したので、この面談ではもうご紹介できず、トークにも届きません。"),
        ("tool", "finalize_interview", {"summary": "深夜で探している。電話は避けたいが、レジや接客は大丈夫。"}),
        ("say", "ありがとうございました。"),
    ])
    advisor, outputs, poster = _run_voice(
        "w-1313131313131313", model,
        ["深夜で探したいです。電話はしたくないです", "レジは大丈夫です", "ほかにはありますか?",
         "やっぱり、さっきの最初の求人に戻りたいです", "以上です"],
        tmp_path, monkeypatch)
    assert BACK_RULE in model.instructions[0]  # 答え方(区分に共通の決まり)が指示に入っている
    *_, last = advisor.search_attempts
    latest = last["proposed_job_ids"]
    assert latest and not set(latest) & set(excluded)
    found = [o for o in outputs if "件見つかりました" in o]
    # 毎回の結果文に、その時点でトークに届く求人が入る(コールセンターを外した後は night の3件)
    assert f"トークに届く求人は、今の提案の求人ID {'・'.join(night)} だけ" in _note(found[1])
    # 戻りたがる前の最後の結果文: 届くのは最後の3件だけ、b0401 を含む外した求人は出せず届かない
    note = _note(found[-1])
    assert f"トークに届く求人は、今の提案の求人ID {'・'.join(latest)} だけ" in note
    assert f"外した求人ID {'・'.join(excluded)} はこの面談ではもう出せず、トークにも届かない" in note
    assert "「トークに出ます」" in note
    # 読み上げる行には、届かない注記が混ざらない(注記は最後の1行)
    assert note.count("\n") == 0
    # 実際にトークに届くのは最後の3件で、本人が戻りたがった b0401 は届かない
    assert _delivered(poster) == latest and "b0401" not in latest


def test_voice_zero_hit_after_excluding_says_the_previous_proposal_still_reaches_the_talk(app, tmp_path, monkeypatch):  # noqa: F811
    """(6) と同じ流れ(中野のコンビニ2件を外して0件)。0件の結果文にも、前の提案の2件がトークに届くことが入り、
    「外したので届かない」とは言わない(ユーザーの決定、2026-10-04)。"""
    shown = [j["job_id"] for j in logic.recommend(server.state, None, job_type="コンビニ", area="中野")["jobs"]]
    model = _scripted_llm([
        ("tool", "propose_jobs", {"job_type": "コンビニ", "area": "中野"}),
        ("say", "見つかった求人を1件ずつお伝えしますね。"),
        ("tool", "propose_jobs", {"job_type": "コンビニ", "area": "中野", "exclude_ids": shown, "search_only": True}),
        ("say", "ほかには見つかりませんでした。"),
        ("tool", "finalize_interview", {"summary": "中野でコンビニのバイトを探している。"}),
        ("say", "ありがとうございました。"),
    ])
    _, outputs, poster = _run_voice(
        "w-1314131413141314", model, ["中野でコンビニのバイトを探しています", "ほかにはありますか?", "以上です"],
        tmp_path, monkeypatch)
    zero = next(o for o in outputs if "見つかりませんでした" in o)
    note = _note(zero)
    assert f"トークに届く求人は、今の提案の求人ID {'・'.join(shown)} だけ" in note
    assert "前の提案としてトークに届く" in note and "トークにも届かない" not in note
    assert _delivered(poster) == shown
