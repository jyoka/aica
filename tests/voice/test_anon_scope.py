"""音声相談の長期メモリが、匿名ユーザーID のスコープで保存されることのテスト(task 62)。実 API なし。

トークの「音声で相談」から入ると、ユーザーはブラウザ専用の「匿名ID:シナリオユーザー」(例 `w-…:sim-new`)。
音声で話した希望・経験・できること・資格(dip の相談プロファイル8項目)は、dip の MCP サーバー(本物、stdio)経由で
このユーザーに保存され、別のブラウザ(同じシナリオでも別の匿名ID)や元のシナリオユーザーには混ざらない。
次のテキストの会話(line_runtime)は、同じユーザーの要約からそれを読む(聞き直さない)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dip_mcp import logic
from dip_mcp.data import State
from evals.scenarios import load_scenarios, prepare
from tests.test_handle import ScriptedModel, call, reply
from voice_resume import config
from voice_resume.advisor import ResumeAdvisor
from voice_resume.dip import DipMCP

from .test_advisor import RELAXED_SEARCH_TRANSCRIPT, _FakeDraftClient, _FakePoster

U1 = "w-1111111111111111:sim-new"  # 音声で相談したブラウザ
U2 = "w-2222222222222222:sim-new"  # 同じシナリオを使っている別のブラウザ

# 音声の面談で本人が話したこと(propose_jobs の引数 = dip の相談プロファイル8項目)
VOICE_WISHES = {
    "job_type": "カフェ", "area": "渋谷", "time_band": "日中", "min_wage": 1200, "days_per_week": 3,
    "experience": "コンビニで1年半", "skills": "レジ打ちと品出し", "certifications": "普通自動車免許",
}


def _voice_session(tmp_path, monkeypatch) -> tuple[State, _FakePoster]:
    """2つのブラウザが同じシナリオ(初回訪問・sim-new)を始め、U1 だけが音声で相談して締める。"""
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))  # dip の子プロセスも同じ状態を使う
    monkeypatch.setattr(config, "OUT_DIR", tmp_path / "out")
    state = State(tmp_path / "state.json")
    sc = next(s for s in load_scenarios() if s["id"] == "F-follow-new")
    prepare(state, sc, U1)
    prepare(state, sc, U2)
    poster = _FakePoster()

    async def run():
        dip = DipMCP()
        await dip.start()
        try:
            advisor = ResumeAdvisor(dip=dip, user_id=U1, talk_poster=poster, draft_client_factory=_FakeDraftClient)
            await advisor.propose_jobs(**VOICE_WISHES)
            advisor._transcript = lambda: RELAXED_SEARCH_TRANSCRIPT
            await advisor.finalize_interview()
        finally:
            await dip.aclose()

    asyncio.run(run())
    return state, poster


def test_voice_wishes_are_saved_in_the_anonymous_user_scope(tmp_path, monkeypatch):
    state, poster = _voice_session(tmp_path, monkeypatch)

    # 話した8項目がすべて、本人の言葉のまま U1 の長期メモリに入る
    stated = logic.conversation_summary(state, U1)["stated_preferences"]
    assert {k: v["value"] for k, v in stated.items()} == {k: str(v) for k, v in VOICE_WISHES.items()}
    # 別のブラウザ(同じシナリオ)や元のシナリオユーザーには混ざらない
    assert logic.conversation_summary(state, U2)["stated_preferences"] == {}
    assert logic.conversation_summary(state, "sim-new")["stated_preferences"] == {}
    # 会話結果の記録も U1 だけ
    assert [o["outcome"] for o in state.user(U1)["outcomes"]] == ["propose"]
    assert state.user(U2).get("outcomes", []) == []
    # トークへ戻す結果も U1 あて(line_runtime の /api/voice/result はこの user_id で所有を確かめる)
    (url, payload), = poster.calls
    assert url == f"{config.TALK_URL}/api/voice/result" and payload["user_id"] == U1


def test_next_text_conversation_reads_the_voice_wishes_instead_of_asking_again(tmp_path, monkeypatch):
    """音声のあと同じユーザーがトークを開き直すと、LINE 側は同じスコープの要約から音声で話した内容を受け取る。"""
    _voice_session(tmp_path, monkeypatch)
    from line_runtime.runtime import LineRuntime

    model = ScriptedModel(script=[
        call("get_user_conversation_summary", {"line_user_id": U1}),
        reply(["前回は渋谷・日中のカフェでしたが、今回も同じ感じで探しますか?"], quick_replies=("同じで探す", "変える")),
    ])

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            return await rt.handle(U1, "revisit")

    r = asyncio.run(go())
    (tool,) = r["tools"]
    stated = tool["result"]["stated_preferences"]
    assert {k: v["value"] for k, v in stated.items()} == {k: str(v) for k, v in VOICE_WISHES.items()}
    # 聞き直さない決まりは dip の Prompt が持つ(LINE 側はこれに従って確認形で1回だけ触れる)
    assert "同じことを聞き直さない" in (Path(logic.__file__).parent / "prompts" / "baitoru_assistant.md").read_text(encoding="utf-8")


def test_voice_page_token_carries_the_user_to_the_worker(monkeypatch):
    """会話ページ(?user=)→ /api/token がユーザーIDを参加者の metadata に載せ、ワーカーがそれを読む。"""
    import jwt
    from fastapi.testclient import TestClient

    from voice_resume import server
    from voice_resume.agent import participant_user_id

    for k, v in {"LIVEKIT_URL": "wss://example.livekit.cloud", "LIVEKIT_API_KEY": "k",
                 "LIVEKIT_API_SECRET": "offline-test-secret-offline-test-secret"}.items():
        monkeypatch.setenv(k, v)
    with TestClient(server.app) as c:
        r = c.post("/api/token", json={"user_id": U1}).json()
        guest = c.post("/api/token", json={}).json()
    claims = jwt.decode(r["token"], options={"verify_signature": False})
    assert r["user_id"] == U1 and claims["sub"] == U1
    assert participant_user_id(type("P", (), {"metadata": claims["metadata"]})()) == U1
    # 会話ページ自身も ?user= を user_id という名前で送る(名前がずれると黙って guest 扱いになる)
    html = (config.ROOT / "voice_resume" / "static" / "index.html").read_text(encoding="utf-8")
    assert "JSON.stringify({ user_id: userId })" in html
    # 単体で開いたとき(ユーザー不明)は metadata なし = dip に何も書かない相談になる
    assert guest["user_id"] is None
    assert participant_user_id(type("P", (), {"metadata": ""})()) is None
