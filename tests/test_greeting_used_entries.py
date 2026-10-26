"""挨拶の 3 つの入口のボタンは、本人の記録を見てコードで付ける(会話の改善 E、task 237)。実 LLM・実 LINE は呼ばない
(dip の MCP は本物)。

- dip: やり終えた入口(logic.used_entries)は今ある記録だけから決まる。開いただけ・途中でやめたものは数えない。要約にも出る
- ランタイム: 挨拶のターンの返信に、やり終えていない入口のうち窓口で使えるものを付ける。LLM の actions と buttons に頼らない
  (LLM が付けなくても付き、やり終えた入口を LLM が付けても出ない)。同じ会話の 2 回目からの返信には付けない
- 記録を消す(項目ごと・すべて消す・期間で消す掃除)と、また案内される
"""

from __future__ import annotations

import asyncio
import itertools
import os
from datetime import timedelta

import pytest

from dip_mcp import arcade, logic, profile, retention
from dip_mcp.data import State
from evals.scenarios import (
    RESUME_FILE,
    load_arcade_draft,
    load_resume,
    seed_arcade_draft,
)
from line_runtime import line_channel
from line_runtime.runtime import greeting_entries, usable_entries
from tests.test_handle import reply
from tests.test_revisit import VISIT_MENU, Greeter

ENTRIES = ("voice_consult", "resume_upload", "arcade")
BODY = ["お仕事探しの相談と、求人のご提案をします。", "どれから始めますか?"]


def plain(messages):
    """入口のボタンを付けない挨拶(task 237 の Prompt どおり。#235 の Claude Sonnet 5.5 はゲームのボタンを落とした)。"""
    return reply(BODY, quick_replies=VISIT_MENU)


def seed(state: State, uid: str, used) -> None:
    """入口をやり終えたことにする: 音声相談の下書き、履歴書の読み込み、ゲームの下書きの保存(本番と同じ dip の関数で)。"""
    if "voice_consult" in used:
        logic.record_voice_result(state, uid, [], "# [氏名]\n## 職務経歴\n- 経理を7年担当した\n", document_kind="職務経歴書")
    if "resume_upload" in used:
        logic.save_resume_upload(state, uid, load_resume("accounting_fulltime"), file=RESUME_FILE)
    if "arcade" in used:
        seed_arcade_draft(state, uid, load_arcade_draft("accounting_arcade"))


def offered(r: dict) -> list[str]:
    """返信に付いた入口(actions と、ゲームの入口のボタン)。"""
    return [*r["actions"], *(["arcade"] if any(b["label"] == line_channel.ARCADE_LABEL for b in r["buttons"]) else [])]


def greet_each(tmp_path, model, calls, between=None):
    """1 つのランタイムで挨拶を順に返す。between(i, state) は i 回目の前に dip の記録を変える。"""
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    async def go():
        out = []
        async with LineRuntime(model=model, in_memory=True) as rt:
            for i, (uid, event, kw) in enumerate(calls):
                if between:
                    between(i, State(tmp_path / "state.json"))
                out.append(await rt.handle(uid, event, **kw))
        return out

    return asyncio.run(go())


# ----------------------------- dip: やり終えた入口 -----------------------------


def test_used_entries_come_only_from_finished_records(tmp_path):
    state = State(tmp_path / "state.json")
    assert logic.used_entries(state.user("w-none")) == []
    # 開いただけ・途中でやめたもの(ゲームを始めただけ、音声相談の部屋を出しただけ、下書きの無い音声相談の結果)は数えない
    arcade.start_play(state, "w-open", "harvest-of-work", ai=False)
    logic.record_voice_room(state, "w-open", "room-1")
    logic.record_voice_result(state, "w-open", [], "  ")
    assert logic.used_entries(state.user("w-open")) == []
    seed(state, "w-all", ENTRIES)
    assert logic.used_entries(state.user("w-all")) == list(ENTRIES)  # 並びはリッチメニューと同じ
    # 要約にも出る(音声相談をやり終えたかは、ほかの項目からは分からない)。説明も付く
    summary = logic.conversation_summary(state, "w-all")
    assert summary["used_entries"] == list(ENTRIES)
    assert "used_entries は本人がやり終えた入口" in summary["note"]
    assert logic.conversation_summary(state, "w-none")["used_entries"] == []


def test_greeting_entries_leave_out_used_and_unusable_entries():
    url = "/liff/arcade/harvest-of-work"
    game = {"label": line_channel.ARCADE_LABEL, "url": url}
    assert usable_entries("web") == ENTRIES and usable_entries("line") == ()
    assert usable_entries("web", ("resume_upload", "arcade")) == ("resume_upload", "arcade")  # 本番の Web(LiveKit なし)
    assert greeting_entries({"used_entries": [], "arcade_url": url}, "web") == {
        "actions": ["voice_consult", "resume_upload"], "buttons": [game]}
    assert greeting_entries({"used_entries": ["resume_upload"], "arcade_url": url}, "line", ENTRIES) == {
        "actions": ["voice_consult"], "buttons": [game]}
    # LINE で LIFF が使えない(features が空)・ゲームの URL が無い(LINE の LIFF の設定が無い)なら出さない
    assert greeting_entries({"used_entries": [], "arcade_url": url}, "line", ()) == {"actions": [], "buttons": []}
    assert greeting_entries({"used_entries": []}, "web") == {"actions": ["voice_consult", "resume_upload"], "buttons": []}
    assert greeting_entries({"used_entries": list(ENTRIES), "arcade_url": url}, "web") == {"actions": [], "buttons": []}


# ----------------------------- ランタイム: 挨拶にコードで付ける -----------------------------

# やり終えた入口の組み合わせ(0、1、2、3 個)
COMBOS = [c for n in range(4) for c in itertools.combinations(ENTRIES, n)]


@pytest.mark.parametrize("event", ["visit", "revisit"])
def test_greeting_gets_buttons_for_every_unfinished_entry_even_if_the_llm_adds_none(tmp_path, event):
    state = State(tmp_path / "state.json")
    users = {f"w-{event}-{i}": used for i, used in enumerate(COMBOS)}
    for uid, used in users.items():
        seed(state, uid, used)
    replies = greet_each(tmp_path, Greeter(script=[plain] * len(users)), [(uid, event, {}) for uid in users])
    for (uid, used), r in zip(users.items(), replies, strict=True):
        assert offered(r) == [e for e in ENTRIES if e not in used], used
        assert r["bubbles"] == BODY and r["quick_replies"] == VISIT_MENU  # 本文と文字の選択肢は LLM のまま
        if "arcade" not in used:  # ゲームのボタンは dip の要約の arcade_url(本人の区分のゲーム)
            assert r["buttons"] == [{"label": line_channel.ARCADE_LABEL, "url": arcade.game_url(uid)}]


def test_llm_entries_are_replaced_and_other_buttons_kept(tmp_path):
    """3 つともやり終えた人に LLM が入口を付けても出さない(LINE はリッチメニューから入れる)。入口でないボタンは残す。"""
    state = State(tmp_path / "state.json")
    seed(state, "w-done", ENTRIES)
    view = {"label": "下書きを見る", "url": "/liff/arcade/draft?rid=0"}
    llm = reply(BODY, quick_replies=VISIT_MENU, actions=["voice_consult", "resume_upload"],
                buttons=[{"label": line_channel.ARCADE_LABEL, "url": "/liff/arcade"}, view])
    (r,) = greet_each(tmp_path, Greeter(script=[llm]), [("w-done", "revisit", {})])
    assert r["actions"] == [] and r["buttons"] == [view]


def test_only_the_greeting_turn_gets_the_buttons_and_a_reload_shows_the_same(tmp_path):
    """同じ会話の 2 回目からの返信には付けない(LLM の actions はそのまま)。画面の描き直し(conversation)も同じボタン。"""
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    seed(State(tmp_path / "state.json"), "w-two", ["resume_upload"])
    ask = reply(["これまでのお仕事を教えてください。"], quick_replies=["今はいい"], actions=["voice_consult"])
    from line_runtime.runtime import LineRuntime

    async def go():
        async with LineRuntime(model=Greeter(script=[plain, ask, plain]), in_memory=True) as rt:
            first = await rt.handle("w-two", "visit")
            second = await rt.handle("w-two", "message", "まず相談する")
            shown = (await rt.conversation("w-two"))["turns"]
            third = await rt.handle("w-other", "message", "こんにちは")  # 挨拶で始まらない会話には付けない
            return first, second, shown, third

    first, second, shown, third = asyncio.run(go())
    assert offered(first) == ["voice_consult", "arcade"]
    assert second["actions"] == ["voice_consult"] and second["buttons"] == []
    assert [offered(t["bot"]) for t in shown] == [["voice_consult", "arcade"], ["voice_consult"]]
    assert third["actions"] == [] and third["buttons"] == []


def test_channels_show_only_entries_they_can_use(tmp_path):
    calls = [("w-line-all", "visit", {"channel": "line", "features": ENTRIES}),
             ("w-line-none", "visit", {"channel": "line", "features": ()}),  # LIFF の設定が無い LINE
             ("w-line-novoice", "revisit", {"channel": "line", "features": ("resume_upload", "arcade")}),
             ("w-web-novoice", "visit", {"channel": "web", "features": ("resume_upload", "arcade")})]
    replies = greet_each(tmp_path, Greeter(script=[plain] * len(calls)), calls)
    assert [offered(r) for r in replies] == [list(ENTRIES), [], ["resume_upload", "arcade"], ["resume_upload", "arcade"]]
    # LINE ではリッチメニューと同じ名前と並び(音声で相談する、履歴書を読み込む、ゲームで書類を作る)
    msgs = line_channel.to_messages(replies[0], "https://aica.invalid", ENTRIES, greeting="visit")
    (flex,) = [m for m in msgs if m["type"] == "flex"]
    labels = [b["action"]["label"] for b in flex["contents"]["body"]["contents"]]
    assert labels == ["音声で相談する", "履歴書を読み込む", line_channel.ARCADE_LABEL]


# ----------------------------- 記録を消すと、また案内される -----------------------------


def test_erasing_records_brings_the_entries_back(tmp_path, monkeypatch):
    uid = "w-erase"
    real_clock = retention.clock

    def between(i: int, state: State) -> None:
        if i == 0:
            seed(state, uid, ENTRIES)
        elif i == 1:  # 項目ごとに消す(プロフィール画面): 履歴書
            assert profile.delete_resume(state, uid)["found"]
        elif i == 2:  # 音声相談の下書き
            (vid,) = [r["id"] for r in state.user(uid)["voice_results"]]
            assert profile.delete_voice_draft(state, uid, vid)["deleted"] == "voice_draft"
        elif i == 3:  # ゲームの下書き
            (d,) = arcade.drafts(state, uid)
            assert profile.delete_arcade_draft(state, uid, d["id"])["deleted"] == "arcade_draft"
        elif i == 4:  # すべて消す
            seed(state, uid, ENTRIES)
            profile.forget_all(state, uid)
        elif i == 5:  # 期間で消す掃除(#208): 最後の利用から USER_TTL を過ぎた人の行を外す
            seed(state, uid, ENTRIES)
            retention.touch(state, uid)
            monkeypatch.setattr(retention, "clock", lambda: real_clock() + retention.USER_TTL + timedelta(days=1))
            assert retention.remove_expired(state, [uid])["users"] == [uid]

    replies = greet_each(tmp_path, Greeter(script=[plain] * 6), [(uid, "revisit", {})] * 6, between)
    assert [offered(r) for r in replies] == [
        [], ["resume_upload"], ["voice_consult", "resume_upload"], list(ENTRIES), list(ENTRIES), list(ENTRIES)]


def test_render_gives_the_buttons_to_the_greeting_but_not_to_the_turn_limit_apology():
    """呼び出しの上限で返した謝り(agent.TURN_LIMIT_REPLY)には、挨拶のターンでも入口のボタンを付けない
    (LINE の初回訪問では、謝りの問いがボタンの見出しになってしまう)。"""
    from langchain_core.messages import AIMessage, HumanMessage

    from line_runtime.agent import TURN_LIMIT_REPLY, LineReply
    from line_runtime.runtime import event_input, render

    entries = {"actions": ["voice_consult"], "buttons": []}
    event = HumanMessage(event_input("visit", None, None))
    said = LineReply(bubbles=BODY, quick_replies=VISIT_MENU)
    ok = render({"messages": [event, AIMessage(said.model_dump_json())], "structured_response": said,
                 "greeting_entries": entries})
    assert ok["actions"] == ["voice_consult"]
    gave_up = AIMessage(TURN_LIMIT_REPLY.model_dump_json(), additional_kwargs={"turn_limit": True})
    r = render({"messages": [event, gave_up], "structured_response": TURN_LIMIT_REPLY, "greeting_entries": entries})
    assert r["actions"] == [] and r["buttons"] == [] and r["bubbles"] == TURN_LIMIT_REPLY.bubbles
