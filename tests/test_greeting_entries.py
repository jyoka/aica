"""挨拶で 3 つの入口(音声相談・履歴書の読み込み・ゲーム)へ案内する(会話の改善 (2/3)、task 220)。実 LLM・実 LINE は呼ばない。

- 評価の判定(evals/checks.py の greeting_entry_violations): 初回訪問は窓口で使える入口を全部、再訪は summary に合わせて 1 つだけ
- dip の Prompt: 入口V と入口R が、挨拶の入口の決まりを指している
- LINE の友だち追加の挨拶: 入口は LIFF 画面へのボタンになり、使えないもの(LiveKit が無いときの音声)は出ない。文字の選択肢も残る
- 再訪(ゲームの下書きに空欄): 要約を読んでから、下書きの続きだけを勧める
"""

from __future__ import annotations

import pytest

from dip_mcp import arcade
from dip_mcp.data import State
from evals.checks import (
    available_entries,
    conversation_checks,
    greeting_entry_violations,
    greeting_offers,
)
from evals.scenarios import load_scenarios, prepare
from line_runtime import line_channel
from tests.test_handle import run
from tests.test_liff_actions import (
    CHANNEL,
    ID_KEY,
    INTERNAL_KEY,
    LIFF_ENVS,
    LIFF_ID,
    LIVEKIT_ENV,
)
from tests.test_line_webhook import REPLY, TestClient, _event, _post, _server
from tests.test_revisit import ARCADE_BUTTON, VISIT_MENU, Greeter, greet

SCENARIOS = {sc["id"]: sc for sc in load_scenarios()}
VISIT = {"event": "visit", "expect_outcome": ["propose"]}
REVISIT = {"event": "revisit", "expect_outcome": ["propose"]}


def turn(actions=(), buttons=(), quick_replies=("求人を探す", "まず相談する", "今はいい")) -> dict:
    return {"bubbles": ["どれから始めますか?"], "quick_replies": list(quick_replies), "cards": [], "buttons": list(buttons),
            "actions": list(actions), "tools": []}


ALL = turn(["voice_consult", "resume_upload"], [ARCADE_BUTTON])


# ----------------------------- 評価の判定 -----------------------------


def test_visit_must_offer_every_entry_the_channel_can_use():
    assert greeting_offers(ALL) == ["voice_consult", "resume_upload", "arcade"]
    assert greeting_entry_violations(VISIT, [ALL]) == []
    v = greeting_entry_violations(VISIT, [turn(["resume_upload"])])
    assert v == ["T1: 初回訪問の挨拶に、使える入口が出ていない: ['voice_consult', 'arcade']"]
    # LINE で LiveKit が無い: 音声は使えないので、出さないのが正しく、出したら違反
    line = ("resume_upload", "arcade")
    assert available_entries("line", line) == line
    assert greeting_entry_violations(VISIT, [turn(["resume_upload"], [ARCADE_BUTTON])], "line", line) == []
    assert greeting_entry_violations(VISIT, [ALL], "line", line) == ["T1: 窓口で使えない入口を出した: ['voice_consult']"]
    # LIFF の設定が無い LINE: どれも出さない
    assert greeting_entry_violations(VISIT, [turn()], "line", ()) == []


def test_the_draft_page_button_is_not_the_game_entry():
    draft = {"label": "下書きを見る", "url": "/liff/arcade/draft?rid=0"}
    assert greeting_offers(turn(buttons=[draft])) == []


def test_revisit_offers_at_most_one_entry_and_the_one_the_summary_calls_for():
    sc = REVISIT | {"expect_greeting_offer": "resume_upload"}
    assert greeting_entry_violations(sc, [turn(["resume_upload"])]) == []
    assert greeting_entry_violations(sc, [ALL])[0].startswith("T1: 再訪の挨拶で入口を3つ勧めた(1つだけ)")
    assert greeting_entry_violations(sc, [turn(["voice_consult"])]) == [
        "T1: 再訪の挨拶で勧めた入口が ['voice_consult'](summary からは resume_upload)"]
    assert greeting_entry_violations(sc, [turn()]) == ["T1: 再訪の挨拶で勧めた入口が なし(summary からは resume_upload)"]
    # 期待の入口が LINE で使えない(LIFF が無い)なら、何を勧めたかは問わない(使えないものを出したら違反)
    assert greeting_entry_violations(sc, [turn()], "line", ()) == []
    assert greeting_entry_violations(sc, [turn(["resume_upload"])], "line", ()) == [
        "T1: 窓口で使えない入口を出した: ['resume_upload']"]
    blanks = REVISIT | {"expect_greeting_offer": "blanks"}
    assert greeting_entry_violations(blanks, [turn(quick_replies=["空欄を埋める", "求人を探す", "今はいい"])]) == []


def test_conversation_checks_report_the_greeting_offers_and_ask_for_the_new_menu():
    sc = SCENARIOS["F-follow-new"]
    r = conversation_checks(sc, [ALL], [None])
    assert r["violations"] == [] and r["greeting_offers"] == ["voice_consult", "resume_upload", "arcade"]
    old_menu = turn(["voice_consult", "resume_upload"], [ARCADE_BUTTON], ["求人を探す", "応募履歴を確認", "使い方", "今はいい"])
    assert any("「求人を探す」「まず相談する」が無い" in v for v in conversation_checks(sc, [old_menu], [None])["violations"])


# ----------------------------- dip の Prompt -----------------------------


def test_entries_point_to_the_greeting_rules():
    from dip_mcp.server import baitoru_assistant

    visit, revisit = baitoru_assistant("visit"), baitoru_assistant("revisit")
    for p in (visit, revisit):
        assert "## 挨拶で3つの入口を案内する(初回訪問・再訪)" in p
    assert "「求人を探す / まず相談する / 今はいい」の3つ" in visit and "窓口で使えるものを全部" in visit
    assert "入口を1つだけ勧める" in revisit and "3つ全部は並べない" in revisit


# ----------------------------- LINE の友だち追加の挨拶 -----------------------------


def _no_liff(monkeypatch) -> None:
    for k in LIFF_ENVS + line_channel.LIVEKIT_ENVS:
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("voice", [True, False], ids=["with-livekit", "without-livekit"])
def test_line_follow_greeting_turns_the_entries_into_liff_buttons(tmp_path, monkeypatch, voice):
    """友だち追加の挨拶(初回訪問): エージェントは窓口で使える入口を知り、LINE ではそれが LIFF 画面へのボタンになる。
    LiveKit が無ければ、音声相談はエージェントにも使えないと伝わり、ボタンも出ない。文字の選択肢はクイックリプライに残る。"""
    _no_liff(monkeypatch)
    systems: list[str] = []

    def visit(messages):
        systems.append(messages[0].content)
        return greet(messages)

    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[visit]), AICA_ID_KEY=ID_KEY, LINE_LIFF_ID=LIFF_ID,
                           LINE_LIFF_CHANNEL_ID=CHANNEL, AICA_INTERNAL_KEY=INTERNAL_KEY, **(LIVEKIT_ENV if voice else {}))
    with TestClient(server.app) as client:
        assert _post(client, [_event("follow")]).status_code == 200

    ((path, body),) = fake.sent()
    assert path == REPLY  # 友だち追加の挨拶は返信トークで返す(push の通数を使わない)
    assert len(body["messages"]) <= line_channel.MAX_MESSAGES
    (flex,) = [m for m in body["messages"] if m["type"] == "flex"]
    links = [(b["action"]["label"], b["action"]["uri"]) for b in flex["contents"]["body"]["contents"]]
    expected = [("ゲームで書類を作る", f"https://liff.line.me/{LIFF_ID}/arcade")]
    if voice:
        expected.append(("🎤 音声で相談する(約5分)", f"https://liff.line.me/{LIFF_ID}/voice"))
    expected.append(("📄 履歴書・職務経歴書を送る", f"https://liff.line.me/{LIFF_ID}/resume/upload"))
    assert links == expected
    texts = [m["text"] for m in body["messages"] if m["type"] == "text"]
    assert not any(t in line_channel.ACTION_NOTES.values() for t in texts)  # 使えない入口の断りの文は出ない
    labels = [i["action"]["label"] for i in body["messages"][-1]["quickReply"]["items"]]
    assert labels == VISIT_MENU  # 文字で答えたい人の選択肢(今はいい も)
    (system,) = systems
    assert "入口V(初回訪問)" in system and "## 挨拶で3つの入口を案内する" in system
    assert ("音声相談は、LINE では使えない" in system) is (not voice)


def test_line_follow_without_liff_greets_with_text_choices_only(tmp_path, monkeypatch):
    _no_liff(monkeypatch)
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[greet]), AICA_ID_KEY=ID_KEY)
    with TestClient(server.app) as client:
        _post(client, [_event("follow")])
    ((_, body),) = fake.sent()
    assert [m["type"] for m in body["messages"]] == ["text"]  # ボタンも断りの文も無い
    assert [i["action"]["label"] for i in body["messages"][-1]["quickReply"]["items"]] == VISIT_MENU


# ----------------------------- 再訪: 下書きの続きだけを勧める -----------------------------


def test_revisit_with_a_draft_offers_only_to_fill_the_blanks(tmp_path):
    sc = SCENARIOS["R-revisit-draft"]
    uid = sc["user"]
    prepare(State(tmp_path / "state.json"), sc)
    (t1,) = run(Greeter(script=[greet, greet]), [(uid, "revisit", None)], tmp_path)
    summary = t1["tools"][0]["result"]
    assert summary["arcade_draft"]["blank_lines"] and summary["uploaded_resume"]  # 経歴は分かり、下書きに空欄がある
    assert t1["actions"] == [] and t1["buttons"] == [] and "空欄を埋める" in t1["quick_replies"]
    assert "今はいい" in t1["quick_replies"] and len(t1["quick_replies"]) <= 4
    assert greeting_entry_violations(sc, [t1]) == []
    assert arcade.drafts(State(tmp_path / "state.json"), uid)  # 種の下書きは dip に保存された


# ----------------------------- Web: 音声相談の設定が無ければ勧めない -----------------------------


def test_web_note_leaves_out_voice_without_livekit(monkeypatch):
    from line_runtime.agent import WEB_NO_VOICE_NOTE, WEB_NOTE, channel_note

    assert channel_note("web") == WEB_NOTE  # 評価と開発者画面の既定(features が空)は 3 つとも使える
    assert channel_note("web", ("resume_upload", "voice_consult", "arcade")) == WEB_NOTE
    assert channel_note("web", ("resume_upload", "arcade")) == WEB_NO_VOICE_NOTE
    assert "voice_consult も付けない" in WEB_NO_VOICE_NOTE
    for k in line_channel.LIVEKIT_ENVS:
        monkeypatch.delenv(k, raising=False)
    assert line_channel.web_features() == ("resume_upload", "arcade")
    for k, v in LIVEKIT_ENV.items():
        monkeypatch.setenv(k, v)
    assert line_channel.web_features() == ("resume_upload", "voice_consult", "arcade")


@pytest.mark.parametrize("voice", [True, False], ids=["with-livekit", "without-livekit"])
def test_web_talk_turn_tells_the_agent_whether_voice_is_ready(tmp_path, monkeypatch, voice):
    """本番のトーク画面(/api/event)の挨拶: LiveKit の設定が無ければ、エージェントに音声相談を勧めないよう伝える(押すと 503 のため)。"""
    _no_liff(monkeypatch)
    systems: list[str] = []

    def visit(messages):
        systems.append(messages[0].content)
        return greet(messages)

    server, _ = _server(tmp_path, monkeypatch, Greeter(script=[visit]), AICA_ID_KEY=ID_KEY, AICA_INTERNAL_KEY=INTERNAL_KEY,
                        **(LIVEKIT_ENV if voice else {}))
    with TestClient(server.app) as client:
        client.get("/")
        uid = client.get("/api/me").json()["user_id"]
        r = client.post("/api/event", json={"user_id": uid, "event": "visit"}).json()
    (system,) = systems
    assert ("音声相談は、この画面ではまだ使えない" in system) is (not voice)
    assert ("voice_consult" in r["actions"]) is voice and "resume_upload" in r["actions"]  # 台本の挨拶は窓口の案内に従う


def test_a_card_ledger_needs_its_own_limit(tmp_path):
    """--ledger を変えたら --limit-usd も要る(LLM を呼ぶ前に止まる)。"""
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    p = subprocess.run([sys.executable, "-m", "evals.run", "--only", "F-follow-new", "--ledger", str(tmp_path / "card.jsonl")],
                       check=False, capture_output=True, text=True, timeout=60, env=env)
    assert p.returncode == 2 and "--limit-usd" in p.stderr
    assert not (tmp_path / "card.jsonl").exists()
