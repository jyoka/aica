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
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from dip_mcp import logic, server
from dip_mcp.data import ROOT, load_jobs
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
CONFIRM_REPLIES = ["レジや接客は大丈夫", "接客も苦手", "それ以外", "今はいい"]
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
        reply(["アルバイト・パートのお仕事で考えますね。", CONFIRM], quick_replies=CONFIRM_REPLIES),
        calls(("recommend_jobs", search),
              ("record_conversation_outcome", {"line_user_id": uid, "outcome": "propose",
                                               "reason": "電話を避けたい人に求人を提案", "confidence": 0.8})),
        lambda msgs: reply(["電話の少ない求人です。"], job_ids=_cards_without_call_centers(msgs), quick_replies=CLOSING),
    ])
    said = [f"{time_band or ''}{'で' if time_band else ''}アルバイトを探しています。電話はしたくないです", answer]
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


def test_voice_excludes_call_centers_from_the_proposal_delivered_to_the_talk(app, tmp_path, monkeypatch):  # noqa: F811
    """「深夜で探したい。電話はしたくない」→ 1問 →「レジは大丈夫」。深夜の上位3件に入るコールセンターを exclude_ids で外して
    呼び直し、トークに届く提案に b0903 が入らない。LiveKit の AgentSession(文字のターン)で、本物の dip MCP につなぐ。"""
    from livekit.agents import AgentSession

    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    uid = "w-7777777777777777"
    poster = _FakePoster()
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

    async def go():
        dip = DipMCP()
        await dip.start()
        try:
            advisor, _ = await prepare_advisor(dip, uid)
            advisor._talk_poster = poster
            advisor._draft_client_factory = _FakeDraftClient
            session = AgentSession(llm=model)
            await session.start(agent=advisor)
            await session.run(user_input="深夜で探したいです。電話はしたくないです")
            await session.run(user_input="レジは大丈夫です")
            await session.run(user_input="以上です")
            outputs = [i.output for i in session.history.items if i.type == "function_call_output"]
            await session.aclose()
            return advisor, outputs
        finally:
            await dip.aclose()

    advisor, outputs = asyncio.run(go())
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
    ((_, payload),) = poster.calls
    delivered = [j["job_id"] for j in payload["jobs"]]
    assert delivered == second["proposed_job_ids"] and CALL_CENTER not in delivered
