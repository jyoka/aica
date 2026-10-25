"""音声相談の雇用区分・playbook・保存と、職務経歴書の下書き(aica v2 の G)の台本テスト。実 API なし。

音声アドバイザーの LLM の代わりに、台本どおりにツール(set_search_track / save_user_preferences / propose_jobs /
finalize_interview)を呼ぶ。dip は本物の MCP サーバー(stdio)、トークは本物の HTTP サーバー(uvicorn)で、
面談の結果はワーカーと同じ経路(POST /api/voice/result、共有鍵つき)でトークに届く。下書きを作る LLM はスタブ。
確かめること:
(1) 音声アドバイザーの指示に、今の雇用区分に合った音声版の playbook が入る(区分ごと)
(2) 音声で区分が切り替わり、dip に記録される(根拠の発言そのまま)。指示の playbook も入れ替わる。話した内容が保存項目に入る
(3) 正社員の面談の下書きは document_kind=職務経歴書・format_version 1.1 で、職務要約と成果が入る。アルバイトは履歴書
(4) 書類の呼び方が、区分ごとに入口(「+」メニュー)・アドバイザー・吹き出し・ボタン・ページの題・本文の見出しでそろう
(5) 面談の途中で「すべて消す」をしたら、その面談の結果で dip の記録を作り直さない。その後に話した区分と希望も dip が断る
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from dip_mcp import logic
from resume_draft.schema import Certification, Education, ResumeDraft, WorkExperience
from tests.test_talk_actions import (  # noqa: F401  フィクスチャ
    advised,
    browser,
    livekit,
    served,
)
from tests.test_talk_page import app  # noqa: F401  フィクスチャ
from voice_resume import advisor as advisor_module
from voice_resume import config as voice_config
from voice_resume.agent import prepare_advisor
from voice_resume.dip import DipMCP
from voice_resume.prompts import CAREER_DRAFTER_SYSTEM, DRAFTER_SYSTEM

BAITO, SEISHAIN, MITEI = "アルバイト・パート", "正社員", "未定"
DOC = {SEISHAIN: "職務経歴書", BAITO: "履歴書"}  # 下書きの書類(未定のまま終わったら履歴書)
NAME = {SEISHAIN: "職務経歴書", BAITO: "履歴書", MITEI: "履歴書など"}  # 入口・アドバイザーの呼び方

# 架空のペルソナ(法人営業6年・企画職へ移りたい)の面談の文字起こし
FULLTIME_TRANSCRIPT = [
    {"role": "assistant", "text": "こんにちは。正社員のお仕事と、空いた時間のアルバイト、どちらを考えていますか?"},
    {"role": "user", "text": "転職を考えていて、正社員で探したいです。"},
    {"role": "assistant", "text": "正社員のお仕事で考えますね。今回探している理由を教えてください。"},
    {"role": "user", "text": "今の会社では企画に関われないので、企画の仕事に移りたいです。"},
    {"role": "assistant", "text": "これまでのお仕事を教えてください。"},
    {"role": "user", "text": "みほん商事で法人営業を6年、うち2年はチームリーダーでした。"},
    {"role": "assistant", "text": "何人くらいのチームで、結果はどのくらい変わりましたか?"},
    {"role": "user", "text": "5人のチームで、担当の売上を2年で1.3倍にしました。"},
    {"role": "assistant", "text": "希望の年収と働き方はありますか?"},
    {"role": "user", "text": "年収500万円以上で、リモートが週2日以上あるといいです。"},
]
PARTTIME_TRANSCRIPT = [
    {"role": "assistant", "text": "どんなバイトを探していますか?"},
    {"role": "user", "text": "カフェで、夕方に働きたいです。"},
    {"role": "assistant", "text": "これまでのバイトの経験を教えてください。"},
    {"role": "user", "text": "コンビニで1年半、レジと品出しをしていました。"},
]
CAREER_DRAFT = ResumeDraft(
    job_summary="法人営業を6年経験し、うち2年はチームリーダーとして5人のチームをまとめた。担当売上を2年で1.3倍にした。",
    work_history=[WorkExperience(period="2020年4月〜現在", organization="みほん商事", employment_type="正社員",
                                 role="法人営業・チームリーダー", duties=["法人顧客への提案営業"],
                                 achievements=["5人のチームで、担当売上を2年で1.3倍にした(本人談)"])],
    skills=["提案営業"], certifications=[Certification(name="普通自動車運転免許")],
    education=[Education(school="みほん大学", status="卒業")],
)
RESUME_DRAFT = ResumeDraft(
    job_summary="コンビニで1年半、レジと品出しを担当した。",
    work_history=[WorkExperience(organization="コンビニ", employment_type="アルバイト", duties=["レジ", "品出し"])],
    education=[Education(school="みほん高校", status="卒業")],
)
SUMMARY = "企画の仕事に移りたくて転職を考えている。法人営業を6年。年収500万円以上・リモート週2日以上を希望。次に確かめること: 勤務地"


class DraftStub:
    """下書きを作る LLM(responses.parse)のスタブ。渡された指示を覚える。"""

    def __init__(self, draft: ResumeDraft):
        self.draft, self.calls = draft, []
        self.responses = SimpleNamespace(parse=self._parse)

    async def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.draft)


class Room:
    """面談の LiveKit の部屋の代わり(名前と、画面へのデータ送信の記録)。"""

    def __init__(self, name: str):
        self.name, self.published = name, []
        self.local_participant = SimpleNamespace(publish_data=self._publish)

    async def _publish(self, data: bytes, reliable=True, topic=None):
        self.published.append(json.loads(data.decode("utf-8")))


@pytest.fixture
def talk(served, advised, tmp_path, monkeypatch):  # noqa: F811
    """トーク(本物の HTTP サーバー)と、そこへ結果を送る音声ワーカーの設定。(URL, サーバー)"""
    monkeypatch.setattr(voice_config, "TALK_URL", served)
    monkeypatch.setattr(voice_config, "OUT_DIR", tmp_path / "voice-out")
    return served, advised


def _browser_user(url: str, server):
    """トークを開いたブラウザ(本物の HTTP サーバーに Cookie の匿名IDで話しかける)。(クライアント, 匿名ID)"""
    import httpx

    client = httpx.Client(base_url=url, timeout=30)
    client.get("/")
    return client, client.cookies[server.ANON_COOKIE]


def _interview(uid: str, steps, room: Room | None = None):
    """本物の dip MCP につないで面談を始め(prepare_advisor)、台本の手順(advisor を受け取る async 関数)を順に走らせる。
    (アドバイザー, 最初の挨拶への指示, 手順の返り値, 面談後の dip の要約)"""

    async def run():
        dip = DipMCP()
        await dip.start()
        try:
            advisor, greeting = await prepare_advisor(dip, uid, room=room)
            out = [await step(advisor) for step in steps]
            return advisor, greeting, out, await dip.get_user_conversation_summary(uid)
        finally:
            await dip.aclose()

    return asyncio.run(run())


def _files(advisor) -> tuple[dict, str]:
    """書き出した下書き(JSON と Markdown)。"""
    paths = [Path(p) for p in advisor.finalized["files"]]
    env = json.loads(next(p for p in paths if p.suffix == ".json" and p.name.startswith("resume-draft")).read_text("utf-8"))
    return env, next(p for p in paths if p.suffix == ".md").read_text("utf-8")


# ----------------------------- (1) 区分に合った音声版の playbook -----------------------------


def test_the_voice_advisor_gets_the_voice_playbook_of_the_current_track(app, monkeypatch):  # noqa: F811
    """チャットで記録した雇用区分から音声相談を始めると、指示にその区分の音声版の playbook が入る(ほかの区分のものは入らない)。
    挨拶も区分に合わせる(未定なら最初の1問で区分を確かめる)。"""
    users = {track: f"w-{i:016d}" for i, track in enumerate((BAITO, SEISHAIN, MITEI), 1)}
    for track in (BAITO, SEISHAIN):
        logic.set_search_track(app.state, users[track], track, "(チャットで本人が話した)")
    for track, uid in users.items():
        advisor, greeting, _, summary = _interview(uid, [])
        assert summary["search_track"] == track and advisor.track == track
        text = advisor.instructions
        assert f"# playbook: {track}(音声)" in text, track
        assert text.count("# playbook: ") == 1  # ほかの区分の playbook は入らない
        assert "## 区分に共通の決まり(音声)" in text and "(チャット)" not in text
        assert f"{NAME[track]}の下書き" in greeting
    _, greeting, _, _ = _interview(users[MITEI], [])
    assert "正社員のお仕事と、空いた時間のアルバイトのどちらを考えているか" in greeting


def _scripted_llm(steps: list[tuple]):
    """LiveKit の AgentSession に差し込む台本の LLM。呼ばれるたびに次の1手を返し、渡された指示(system)を覚える。"""
    from livekit.agents import llm
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

    class Stream(llm.LLMStream):
        def __init__(self, owner, step, **kw):
            super().__init__(owner, **kw)
            self._step = step

        async def _run(self):
            kind, *rest = self._step
            if kind == "tool":
                name, args = rest
                delta = llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                    name=name, arguments=json.dumps(args, ensure_ascii=False), call_id=f"call-{id(self)}")])
            else:
                delta = llm.ChoiceDelta(role="assistant", content=rest[0])
            self._event_ch.send_nowait(llm.ChatChunk(id="scripted", delta=delta))

    class Scripted(llm.LLM):
        def __init__(self):
            super().__init__()
            self.steps, self.instructions = list(steps), []

        def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kwargs):
            self.instructions.append(chat_ctx.items[0].text_content)
            return Stream(self, self.steps.pop(0), chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)

    return Scripted()


def test_in_a_live_voice_session_the_switch_swaps_the_playbook_within_the_same_turn(app):  # noqa: F811
    """LiveKit の AgentSession(文字のターン)の中で、本人の発話から set_search_track が呼ばれると、同じターンの次の
    LLM の呼び出しから正社員の音声版の playbook になる。話した目的はその場で保存される(本物の dip MCP)。"""
    from livekit.agents import AgentSession

    uid = "w-5555555555555555"
    model = _scripted_llm([
        ("tool", "set_search_track", {"track": SEISHAIN, "evidence": "転職を考えていて、正社員で探したいです"}),
        ("say", "正社員のお仕事で考えますね。今回探している理由を教えてください。"),
        ("tool", "save_user_preferences", {"purpose": "企画の仕事に移りたい"}),
        ("say", "企画の仕事に移りたいのですね。そう思ったのは、どんな場面でしたか?"),
    ])

    async def run():
        dip = DipMCP()
        await dip.start()
        try:
            advisor, _ = await prepare_advisor(dip, uid)
            session = AgentSession(llm=model)
            await session.start(agent=advisor)
            await session.run(user_input="転職を考えていて、正社員で探したいです")
            await session.run(user_input="今の会社では企画に関われないので、企画の仕事に移りたいです")
            await session.aclose()
            return await dip.get_user_conversation_summary(uid)
        finally:
            await dip.aclose()

    summary = asyncio.run(run())
    seen = ["# playbook: 正社員(音声)" in text for text in model.instructions]
    assert "# playbook: 未定(音声)" in model.instructions[0] and seen == [False, True, True, True]
    assert summary["search_track"] == SEISHAIN
    assert summary["track_history"][-1]["evidence"] == "転職を考えていて、正社員で探したいです"
    assert summary["stated_preferences"]["purpose"]["value"] == "企画の仕事に移りたい"


# ----------------------------- (2)(3) 音声で区分が切り替わり、話した内容が保存され、職務経歴書ができる -----------------------------


def test_voice_switches_to_fulltime_saves_what_was_said_and_drafts_a_career_sheet(talk, monkeypatch):
    url, server = talk
    client, uid = _browser_user(url, server)
    stub = DraftStub(CAREER_DRAFT)
    monkeypatch.setattr(advisor_module, "_default_client", lambda: stub)
    room = Room("voice-resume-test1")

    async def switch(a):
        return await a.set_search_track(SEISHAIN, "転職を考えていて、正社員で探したいです。")

    async def save(a):
        return await a.save_user_preferences(
            purpose="今の会社では企画に関われないので、企画の仕事に移りたい",
            experience="みほん商事で法人営業を6年、うち2年はチームリーダー",
            min_annual_salary="年収500万円以上", work_style="リモートが週2日以上")

    async def propose(a):
        # 正社員の面談で時間帯(アルバイトの条件)が混ざっても、検索にも保存にも入れない
        return await a.propose_jobs(job_type="企画・PM", min_annual_salary=500, time_band="夕方")

    async def finalize(a):
        a._transcript = lambda: FULLTIME_TRANSCRIPT
        return await a.finalize_interview(summary=SUMMARY)

    async def again(a):  # 同じ区分への切り替えは記録しない
        return await a.set_search_track(SEISHAIN, "正社員で")

    advisor, greeting, (switched, saved, proposed, done, same), summary = _interview(
        uid, [switch, save, propose, finalize, again], room=room)

    # 区分は未定から始まり、本人の発言で正社員に切り替わって dip に記録される(根拠は発言そのまま)
    assert "履歴書などの下書き" in greeting
    assert "正社員" in switched and "dip に記録済み" in switched and "今の区分はすでに正社員" in same
    assert summary["search_track"] == SEISHAIN
    assert [(h["from"], h["track"], h["evidence"], h["undo"]) for h in summary["track_history"]] == [
        (MITEI, SEISHAIN, "転職を考えていて、正社員で探したいです。", False)]
    # 指示の playbook が正社員の音声版に入れ替わり、書類は職務経歴書と呼ぶ
    assert "# playbook: 正社員(音声)" in advisor.instructions and "# playbook: 未定(音声)" not in advisor.instructions
    assert "トークに職務経歴書の下書きと求人が出ます" in advisor.instructions
    assert {"type": "track", "track": SEISHAIN} in room.published

    # 話した内容が保存項目に入る(チャットと同じ要約で読める)
    assert "保存しました" in saved
    stated = {k: v["value"] for k, v in summary["stated_preferences"].items()}
    assert stated["purpose"] == "今の会社では企画に関われないので、企画の仕事に移りたい"
    assert stated["experience"] == "みほん商事で法人営業を6年、うち2年はチームリーダー"
    assert stated["work_style"] == "リモートが週2日以上" and stated["min_annual_salary"] == "500"
    assert "time_band" not in stated and "時間帯" in proposed  # 区分に合わない条件は入れず、そう伝える
    # 求人は正社員の中から探す
    assert advisor.proposed_jobs and all(j["employment_type"] == SEISHAIN for j in advisor.proposed_jobs)
    assert advisor.recommend_args == {"job_type": "企画・PM", "min_annual_salary": 500, "employment_type": SEISHAIN}

    # 相談のまとめが記録され、次の会話(チャット・音声)が読む
    assert summary["consultation_summary"]["text"] == SUMMARY and summary["consultation_summary"]["outcome"] == "propose"

    # 下書きは職務経歴書(format_version 1.1)。職務要約と成果が入り、学歴は出さない
    assert "職務経歴書の下書き" in done and advisor.delivered_to_talk
    assert stub.calls[0]["instructions"] == CAREER_DRAFTER_SYSTEM
    assert "職務経歴書の下書きを作ってください" in stub.calls[0]["input"][0]["content"]
    env, md = _files(advisor)
    assert env["format_version"] == "1.1" and env["document_kind"] == "職務経歴書"
    assert env["resume"]["job_summary"].startswith("法人営業を6年") and env["resume"]["work_history"][0]["achievements"]
    assert md.startswith("# 職務経歴書(下書き)")
    assert md.index("## 職務要約") < md.index("## 職務経歴") < md.index("## 活かせる経験・知識・技術")
    assert "- 成果:\n  - 5人のチームで、担当売上を2年で1.3倍にした(本人談)" in md
    assert "学歴" not in md and "みほん大学" not in md and "生年月日" not in md

    # トーク: 吹き出し・ボタン・下書きのページが職務経歴書
    (rec,) = logic.voice_results_since(server.state, uid, 0)
    assert rec["document_kind"] == "職務経歴書" and rec["room"] == room.name
    got = client.post("/api/voice/wait", json={"user_id": uid, "since": 0, "timeout": 0}).json()["results"][0]
    assert "職務経歴書の下書きもできました。内容はご自身で確認・修正してください。" in got["bubbles"]
    assert "希望年収: 500万円以上" in got["bubbles"][0] and "働き方: リモートが週2日以上" in got["bubbles"][0]
    assert got["buttons"] == [{"label": "職務経歴書の下書きを見る", "url": "/liff/voice/draft?rid=0"}]
    page = client.get("/liff/voice/draft", params={"rid": 0})
    assert "<title>職務経歴書の下書き</title>" in page.text and "# 職務経歴書(下書き)" in page.text


def test_a_parttime_interview_still_drafts_a_resume(talk, monkeypatch):
    """アルバイト・パートの面談は今までどおり履歴書(format_version 1.1 で document_kind=履歴書)。"""
    url, server = talk
    client, uid = _browser_user(url, server)
    logic.set_search_track(server.state, uid, BAITO, "夕方にバイトしたい")
    stub = DraftStub(RESUME_DRAFT)
    monkeypatch.setattr(advisor_module, "_default_client", lambda: stub)

    async def propose(a):
        # アルバイトの面談で希望年収(正社員の条件)が混ざっても、検索にも保存にも入れない
        return await a.propose_jobs(job_type="カフェ", time_band="夕方", min_annual_salary=400, experience="コンビニで1年半")

    async def finalize(a):
        a._transcript = lambda: PARTTIME_TRANSCRIPT
        return await a.finalize_interview(summary="夕方にカフェで働きたい。コンビニで1年半。次に確かめること: 週の日数")

    advisor, greeting, (proposed, _), summary = _interview(uid, [propose, finalize], room=Room("voice-resume-test2"))
    assert "履歴書の下書き" in greeting and "職務経歴書" not in greeting
    assert all(j["employment_type"] == BAITO for j in advisor.proposed_jobs)
    assert advisor.recommend_args["employment_type"] == BAITO and "min_annual_salary" not in advisor.recommend_args
    assert "希望年収の下限" in proposed and "min_annual_salary" not in summary["stated_preferences"]
    assert summary["stated_preferences"]["time_band"]["value"] == "夕方"  # 負例: 区分に合う条件はそのまま保存する
    assert stub.calls[0]["instructions"] == DRAFTER_SYSTEM
    env, md = _files(advisor)
    assert env["format_version"] == "1.1" and env["document_kind"] == "履歴書"
    assert md.startswith("# 履歴書(下書き)") and "## 学歴" in md and "みほん高校" in md
    got = client.post("/api/voice/wait", json={"user_id": uid, "since": 0, "timeout": 0}).json()["results"][0]
    assert got["buttons"][0]["label"] == "履歴書の下書きを見る"


def test_a_too_long_summary_is_sent_back_before_anything_is_saved(talk, monkeypatch):
    """相談のまとめが200字を超えたら、何も保存せずに書き直しを求める(途中で切ると「次に確かめること」が落ちる)。"""
    url, server = talk
    _, uid = _browser_user(url, server)
    monkeypatch.setattr(advisor_module, "_default_client", lambda: DraftStub(RESUME_DRAFT))

    async def too_long(a):
        a._transcript = lambda: PARTTIME_TRANSCRIPT
        return await a.finalize_interview(summary="あ" * 201)

    async def rewritten(a):
        return await a.finalize_interview(summary="夕方にカフェで働きたい。次に確かめること: 週の日数")

    _, _, (first, second), summary = _interview(uid, [too_long, rewritten])
    assert "201字" in first and "書き直して" in first
    assert "保存し" in second and summary["consultation_summary"]["text"].startswith("夕方にカフェ")
    assert len(logic.voice_results_since(server.state, uid, 0)) == 1  # 書き直しの前には何も届いていない


# ----------------------------- (4) 書類の呼び方がそろう -----------------------------


def _menu_text(track: str) -> str:
    """talk-core.js の「+」メニューの音声相談の説明(DOC_NAME と既定の呼び方から)。"""
    core = (voice_config.ROOT / "line_runtime" / "static" / "talk-core.js").read_text(encoding="utf-8")
    names = dict(re.findall(r'"([^"]+)": "([^"]+)"', re.search(r"const DOC_NAME = \{([^}]*)\}", core).group(1)))
    fallback = re.search(r'const docName = \(track\) => DOC_NAME\[track\] \|\| "([^"]+)"', core).group(1)
    template = re.search(r"const voiceMenuText = \(\) => `([^`]*)`", core).group(1)
    return template.replace("${docName(o.getTrack?.())}", names.get(track, fallback))


@pytest.mark.parametrize("track", [SEISHAIN, BAITO, MITEI])
def test_the_document_is_called_the_same_everywhere_in_a_track(talk, monkeypatch, track):
    """ユーザーの決定(2026-10-03): 書類の呼び方を document_kind にそろえる。区分ごとに、入口(「+」メニュー)・
    アドバイザーの話し方・結果の吹き出し・ボタン・下書きページの題・本文の見出しが同じ書類名になる。
    未定のときは、入口とアドバイザーは「履歴書など」(どちらにも当てはまる言い方)、できる下書きは履歴書。"""
    url, server = talk
    client, uid = _browser_user(url, server)
    if track != MITEI:
        logic.set_search_track(server.state, uid, track, "(チャットで本人が話した)")
    monkeypatch.setattr(advisor_module, "_default_client", lambda: DraftStub(CAREER_DRAFT))

    async def finalize(a):
        a._transcript = lambda: FULLTIME_TRANSCRIPT
        return await a.finalize_interview()

    advisor, greeting, _, _ = _interview(uid, [finalize])
    name, doc = NAME[track], DOC.get(track, "履歴書")
    others = {"職務経歴書", "履歴書"} - {doc}
    # 入口とアドバイザー
    assert _menu_text(track) == f"話すと約5分で{name}の下書きができます"
    assert f"{name}の下書き" in greeting and f"トークに{name}の下書きと求人が出ます" in advisor.instructions
    # 結果: 吹き出し・ボタン・ページの題・本文の見出し
    got = client.post("/api/voice/wait", json={"user_id": uid, "since": 0, "timeout": 0}).json()["results"][0]
    shown = [b for b in got["bubbles"] if "下書き" in b]
    assert shown == [f"{doc}の下書きもできました。内容はご自身で確認・修正してください。"]
    assert got["buttons"][0]["label"] == f"{doc}の下書きを見る"
    page = client.get("/liff/voice/draft", params={"rid": 0}).text
    assert f"<title>{doc}の下書き</title>" in page and f"# {doc}(下書き)" in page
    _, md = _files(advisor)
    assert md.startswith(f"# {doc}(下書き)")
    for other in others:  # もう一方の書類の名前は、結果のどこにも出ない
        assert not any(other in b for b in got["bubbles"]) and other not in got["buttons"][0]["label"]
        assert f"<title>{other}" not in page and not md.startswith(f"# {other}")


def test_the_plus_menu_names_the_document_of_the_current_track_in_the_browser(talk, browser):  # noqa: F811
    """「+」メニューの音声相談の説明は、開くたびに今の雇用区分の書類名になる(本番の画面)。"""
    url, server = talk
    context = browser.new_context()
    page = context.new_page()
    page.goto(url + "/")
    page.locator("#log .chips button", has_text="まず相談する").wait_for()
    uid = next(c["value"] for c in context.cookies() if c["name"] == "aica_uid")
    desc = page.locator("#menu-voice small")
    page.locator("#plus").click()
    assert desc.inner_text() == "話すと約5分で履歴書などの下書きができます"  # 未定
    for track, name in ((SEISHAIN, "職務経歴書"), (BAITO, "履歴書")):
        logic.set_search_track(server.state, uid, track, "(チャットで本人が話した)")
        page.reload()
        page.locator("#log .chips button", has_text="まず相談する").first.wait_for()
        page.locator("#plus").click()
        assert desc.inner_text() == f"話すと約5分で{name}の下書きができます", track
    context.close()


# ----------------------------- (5) 面談の途中で「すべて消す」 -----------------------------


def test_a_voice_result_from_before_forget_all_does_not_recreate_the_record(talk, livekit, monkeypatch):  # noqa: F811
    """プロフィール画面の PR レビューからの後続指摘: 面談の途中で本人が「すべて消す」をしたら、その面談の結果が後から
    届いても、トークにも dip にも記録を作り直さない(ワーカーは会話の結果も記録しない)。その面談で書き出した下書き・文字起こしのファイルも消す。
    負例: 消した後に始めた面談の結果は、これまでどおり届く。"""
    url, server = talk
    client, uid = _browser_user(url, server)
    monkeypatch.setattr(advisor_module, "_default_client", lambda: DraftStub(RESUME_DRAFT))
    room = client.post("/api/voice/start", json={"user_id": uid}).json()["room"]

    async def talk_then_forget(a):
        await a.save_user_preferences(purpose="生活費の足しに")
        assert client.post("/api/profile/forget").json() == {"forgotten": True}
        a._transcript = lambda: PARTTIME_TRANSCRIPT
        return await a.finalize_interview(summary="生活費の足しに探している。")

    advisor, _, (reply,), summary = _interview(uid, [talk_then_forget], room=Room(room))
    assert advisor.forgotten and not advisor.delivered_to_talk
    assert not any(Path(f).exists() for f in advisor.finalized["files"])  # 文字起こし・下書きのファイルも残さない
    assert "残していません" in reply
    user = server.state.user(uid)
    assert user.get("voice_results", []) == [] and user.get("outcomes") == [] and user.get("preferences") is None
    assert summary["consultation_summary"] is None and summary["stated_preferences"] == {}

    # 負例: 消した後に始めた面談の結果は、これまでどおり届く
    server.app.state.voice_rooms.release(uid, room)  # 前の面談のジョブが終わった(同時に開けるのは 1 部屋まで)
    new_room = client.post("/api/voice/start", json={"user_id": uid}).json()["room"]

    async def finalize(a):
        a._transcript = lambda: PARTTIME_TRANSCRIPT
        return await a.finalize_interview()

    advisor, _, _, _ = _interview(uid, [finalize], room=Room(new_room))
    assert advisor.delivered_to_talk and not advisor.forgotten
    assert all(Path(f).exists() for f in advisor.finalized["files"])
    assert [r["room"] for r in logic.voice_results_since(server.state, uid, 0)] == [new_room]


def test_writes_after_forget_all_in_the_same_call_are_refused(talk, livekit):  # noqa: F811
    """R7(docs/design/privacy.md の 3.8 の案 A): 面談の途中で本人が「すべて消す」をしたら、その通話でその後に話した
    雇用区分と希望は、dip が断って状態に残さない。アドバイザーは保存できなかったとだけ扱い、会話は続ける。
    負例: 消した後に始めた面談の書き込みは、これまでどおり通る。"""
    url, server = talk
    client, uid = _browser_user(url, server)
    room = client.post("/api/voice/start", json={"user_id": uid}).json()["room"]

    async def forget_then_talk(a):
        assert client.post("/api/profile/forget").json() == {"forgotten": True}
        saved = await a.save_user_preferences(purpose="生活費の足しに", area="新宿")
        switched = await a.set_search_track(SEISHAIN, "正社員で探したいです")
        return saved, switched

    advisor, _, ((saved, switched),), summary = _interview(uid, [forget_then_talk], room=Room(room))
    assert "保存しました" not in saved and "保存できませんでした" in saved
    assert "切り替えられませんでした" in switched and advisor.track == MITEI
    user = server.state.user(uid)
    assert user.get("preferences") is None and user.get("track_history") is None
    assert summary["stated_preferences"] == {} and summary["search_track"] == MITEI

    # 負例: 消した後に始めた面談の書き込みは、これまでどおり通る
    server.app.state.voice_rooms.release(uid, room)
    new_room = client.post("/api/voice/start", json={"user_id": uid}).json()["room"]

    async def talk_(a):
        return (await a.save_user_preferences(purpose="生活費の足しに"),
                await a.set_search_track(SEISHAIN, "正社員で探したいです"))

    advisor, _, ((saved, switched),), summary = _interview(uid, [talk_], room=Room(new_room))
    assert "保存しました: 目的" in saved and "切り替えました" in switched
    assert summary["stated_preferences"]["purpose"]["value"] == "生活費の足しに"
    assert summary["search_track"] == SEISHAIN


def test_dip_refuses_every_write_of_a_forgotten_room(tmp_path):
    """dip の側: 「すべて消す」の前に始めた部屋を付けた書き込み(希望・雇用区分・会話の結果)は、何も残さずに断る。
    負例: 部屋を付けないトークの書き込みと、消した後に始めた部屋の書き込みは通る。"""
    from dip_mcp import profile
    from dip_mcp.data import State

    state, uid, old, new = State(tmp_path / "state.json"), "w-7777777777777777", "voice-resume-old", "voice-resume-new"
    logic.record_voice_room(state, uid, old)
    profile.forget_all(state, uid)
    logic.record_voice_room(state, uid, new)
    assert logic.save_preferences(state, uid, {"purpose": "生活費の足しに"}, room=old) == {
        "saved": [], "rejected": [], "error": logic.FORGOTTEN_ROOM_ERROR}
    assert logic.set_search_track(state, uid, SEISHAIN, "正社員で探したいです", room=old) == {
        "error": logic.FORGOTTEN_ROOM_ERROR}
    assert logic.record_outcome(state, uid, "consult", "相談した", 1.0, "生活費の足しに探している。", room=old) == {
        "error": logic.FORGOTTEN_ROOM_ERROR}
    user = state.user(uid)
    assert not user.get("preferences") and not user.get("track_history") and not user.get("outcomes")

    for room, track in ((None, SEISHAIN), (new, BAITO)):
        assert logic.save_preferences(state, uid, {"purpose": "生活費の足しに"}, room=room)["saved"] == ["purpose"]
        assert logic.set_search_track(state, uid, track, "そう探したいです", room=room)["changed"]
        assert "recorded" in logic.record_outcome(state, uid, "consult", "相談した", 1.0, room=room)
    assert len(state.user(uid)["outcomes"]) == 2


def test_a_write_that_lands_while_forget_all_is_running_is_refused(tmp_path):
    """「すべて消す」が 1 人の行を初期に戻した直後(連携を外すなど、まだ終わっていない間)に届いた、消す前の部屋の書き込みも断る。
    行を戻すことと、消す前の部屋を残すことは、同じロックの中で行う(間の時間に書き込みが通らないように)。"""
    from dip_mcp import profile
    from dip_mcp.data import State

    state, uid, room = State(tmp_path / "state.json"), "w-8888888888888888", "voice-resume-old"
    logic.record_voice_room(state, uid, room)
    got = []
    reset_user = state.reset_user

    def reset_then_the_worker_writes(*args, **kwargs):
        before = reset_user(*args, **kwargs)
        got.append(logic.save_preferences(state, uid, {"wants": "残業少なめ"}, room=room))
        got.append(logic.set_search_track(state, uid, SEISHAIN, "正社員で探したいです", room=room))
        got.append(logic.record_outcome(state, uid, "consult", "相談した", 1.0, room=room))
        return before

    state.reset_user = reset_then_the_worker_writes
    profile.forget_all(state, uid)
    assert [r.get("error") for r in got] == [logic.FORGOTTEN_ROOM_ERROR] * 3
    user = state.user(uid)
    assert user["forgotten_voice_rooms"] == [room]
    assert not user.get("preferences") and not user.get("track_history") and not user.get("outcomes")


def test_the_chat_ai_does_not_see_the_room_argument(tmp_path, monkeypatch):
    """部屋は音声ワーカーだけが渡す引数: dip の書き込むツールは受け取るが、トークの AI に渡すツールの引数には出さない。"""
    from dip_mcp import server
    from line_runtime.runtime import LineRuntime
    from tests.test_handle import ScriptedModel

    writes = {t.name for t in asyncio.run(server.mcp.list_tools()) if logic.VOICE_ROOM_ARG in t.inputSchema["properties"]}
    assert writes == {"save_user_preferences", "set_search_track", "record_conversation_outcome"}
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))

    async def chat_tools():
        async with LineRuntime(model=ScriptedModel(), in_memory=True) as rt:
            return rt.tools

    tools = asyncio.run(chat_tools())
    assert writes <= {t.name for t in tools}
    assert not [t.name for t in tools if logic.VOICE_ROOM_ARG in t.args]


# ----------------------------- 区分を切り替えた後の受け渡し(レビューの指摘) -----------------------------


def test_switching_mid_call_drops_the_conditions_of_the_old_track():
    """未定のあいだに聞いた時給(アルバイトの条件)は、正社員に切り替えたら希望から外れ、トークのまとめにも出ない。
    負例: どちらの区分でも使う項目(エリア・目的)は残る。"""
    from voice_resume.advisor import ResumeAdvisor

    advisor = ResumeAdvisor()
    asyncio.run(advisor.save_user_preferences(min_wage="1200", area="新宿", purpose="企画の仕事に移りたい"))
    assert advisor.preferences["min_wage"] == "1200"
    asyncio.run(advisor.set_search_track(SEISHAIN, "やっぱり正社員で探したいです"))
    assert advisor.preferences == {"area": "新宿", "purpose": "企画の仕事に移りたい"}


def test_saving_tells_the_advisor_which_values_dip_refused(app):  # noqa: F811
    """dip が弾いた値(長すぎる・個人情報らしい)は「保存しました」と言わず、保存できなかったと返す。"""
    uid = "w-6666666666666666"

    async def save(a):
        return await a.save_user_preferences(purpose="企画の仕事に移りたい", experience="法人営業" * 30)

    _, _, (reply,), summary = _interview(uid, [save])
    assert "保存しました: 目的" in reply and "保存できなかった項目: 経験" in reply
    assert "experience" not in summary["stated_preferences"]


def test_the_chat_hears_the_track_after_the_voice_consultation():
    """音声で区分が切り替わったら、トークに続けた結果のターンで、次の返信の前に playbook を読み直すよう AI に伝える。"""
    from line_runtime.runtime import VOICE_EVENT, voice_event
    from line_runtime.voice_reply import voice_reply

    rec = {"id": 0, "cards": [], "draft_markdown": "# 職務経歴書(下書き)", "preferences": {}, "document_kind": "職務経歴書"}
    event = voice_event(voice_reply("u", rec, SEISHAIN))
    assert event.startswith(VOICE_EVENT) and "「正社員」で読み直し" in event
    assert "search_track" not in voice_event(voice_reply("u", rec))  # 区分が分からなければ足さない


def test_old_voice_replies_are_redrawn_with_the_name_of_their_draft():
    """document_kind を持つ前の返信(下書きはすべて履歴書の形)は、描き直すときに履歴書の呼び方にそろえる。
    負例: 職務経歴書の返信はそのまま。"""
    from line_runtime.voice_reply import voice_history_reply

    old = {"bubbles": ["まとめ", "職務経歴書の下書きもできました。内容はご自身で確認・修正してください。"], "voice_result": 2,
           "buttons": [{"label": "職務経歴書の下書きを見る", "url": "/liff/voice/draft?user=u&rid=2"}], "cards": []}
    got = voice_history_reply("u", old)
    assert got["bubbles"][1].startswith("履歴書の下書きもできました") and got["buttons"][0]["label"] == "履歴書の下書きを見る"
    new = old | {"document_kind": "職務経歴書"}
    got = voice_history_reply("u", new)
    assert got["bubbles"] == new["bubbles"] and got["buttons"][0]["label"] == "職務経歴書の下書きを見る"
    assert got["buttons"][0]["url"] == "/liff/voice/draft?rid=2"
