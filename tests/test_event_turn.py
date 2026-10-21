"""POST /api/event(JSON)と POST /api/event/stream(NDJSON)は、同じ入力に同じ判定を返す(task 163。実 LLM なし)。

受け付け(検証・プッシュの判定・stale・会話数の上限・知らせ・開いた記録)の決まりは line_runtime.turn の 1 か所にあり、
2 つの API は書き出しの形だけが違う(docs/design/consolidate-resume-draft-and-event-turn.md の候補 2)。
ここでは同じ場面を、それぞれ新しい状態の Web サーバーで 2 つの API に通し、結果をそろえて比べる。
stream は最後の行を JSON の API の形にそろえる(reply は kind を除く。stale は {"stale": True, "thread": n})。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from dip_mcp import behavior, logic, profile
from line_runtime.quota import LIMIT_MESSAGE, limit_reply
from tests.test_quota import _server

LIMIT_REASON = "本日の会話数の上限に達している"


class Api:
    """1 つの API(json か stream)を、画面と同じ呼び方で呼ぶ。kinds は直前の stream の行の種類(json なら None)。"""

    def __init__(self, name: str, client: TestClient, server, model) -> None:
        self.name, self.client, self.server, self.model = name, client, server, model
        self.kinds: list[str] | None = None
        self.lines: list[dict] = []

    def start(self, scenario: str = "F-follow-new") -> str:
        return self.client.post(f"/api/scenarios/{scenario}/start").json()["user"]

    def send(self, user: str, event: str, text: str | None = None, thread: int | None = None) -> tuple[int, object]:
        """(ステータス, 結果)。400 なら結果は detail の文。"""
        body = {"user_id": user, "event": event, "text": text, "thread": thread}
        if self.name == "json":
            r = self.client.post("/api/event", json=body)
            self.kinds, self.lines = None, []
            return r.status_code, r.json() if r.status_code in (200, 400) else r.text
        r = self.client.post("/api/event/stream", json=body)
        if r.status_code != 200:
            self.kinds, self.lines = None, []
            return r.status_code, r.json()
        self.lines = [json.loads(x) for x in r.text.splitlines()]
        self.kinds = [e["kind"] for e in self.lines]
        last = self.lines[-1] if self.lines else {}
        if last.get("kind") == "reply":
            return 200, {k: v for k, v in last.items() if k != "kind"}
        if last.get("kind") == "stale":
            return 200, {"stale": True, "thread": last["thread"]}
        return 200, last

    def used(self, user: str) -> int:
        return self.client.app.state.quota.used(user)


def both(tmp_path, monkeypatch, scene, limit: str | None = None, raise_server_exceptions: bool = True) -> dict:
    """scene(api) を JSON と stream のそれぞれで、新しい状態の Web サーバーに通す。{"json": 結果, "stream": 結果}"""
    out = {}
    for name in ("json", "stream"):
        d = tmp_path / name
        d.mkdir()
        server, model = _server(d, monkeypatch, limit)
        with TestClient(server.app, raise_server_exceptions=raise_server_exceptions) as client:
            client.get("/dev")
            out[name] = scene(Api(name, client, server, model))
    return out


def test_a_message_gets_the_same_reply_with_the_search_track(tmp_path, monkeypatch):
    def scene(api):
        user = api.start()
        status, r = api.send(user, "message", "渋谷で")
        return status, r, api.kinds, api.used(user), api.model.calls

    out = both(tmp_path, monkeypatch, scene)
    assert out["json"][:2] == out["stream"][:2]
    status, r = out["json"][:2]
    assert status == 200 and r["bubbles"] == ["はい、どうぞ。"] and "search_track" in r
    assert out["stream"][2] == ["start", "llm", "reply"]
    assert out["json"][3:] == out["stream"][3:] == (1, 1)


def test_a_push_dip_declines_is_not_sent(tmp_path, monkeypatch):
    """未連携のユーザー(F-follow-new)へのプッシュは dip が見送る。会話数は使わない。"""
    def scene(api):
        user = api.start("F-follow-new")
        status, r = api.send(user, "form_exit")
        return status, r, api.kinds, api.used(user), api.model.calls

    out = both(tmp_path, monkeypatch, scene)
    assert out["json"][:2] == out["stream"][:2]
    status, r = out["json"][:2]
    assert status == 200 and r["sent"] is False and "dip ID と未連携" in r["reason"]
    assert set(r) == {"sent", "reason"}
    assert out["stream"][2] == ["start", "gate", "reply"]
    assert out["json"][3:] == out["stream"][3:] == (0, 0)


def test_a_push_at_the_limit_is_not_sent_and_leaves_no_contact_in_dip(tmp_path, monkeypatch):
    """上限に達していたら、dip に聞く前(接触として記録する前)にプッシュを見送る。"""
    def scene(api):
        user = api.start("B-exit")  # 上限が無ければ dip が許可するユーザー
        assert api.send(user, "message", "こんにちは")[0] == 200  # 上限の 1 回を使う
        contact = behavior.view(api.server.state, user)["last_contact"]
        status, r = api.send(user, "form_exit")
        return status, r, api.kinds, behavior.view(api.server.state, user)["last_contact"] == contact, api.model.calls

    out = both(tmp_path, monkeypatch, scene, limit="1")
    assert out["json"][:2] == out["stream"][:2] == (200, {"sent": False, "reason": LIMIT_REASON})
    assert out["stream"][2] == ["start", "reply"]
    assert out["json"][3:] == out["stream"][3:] == (True, 1)


def test_a_stale_tab_is_told_before_using_the_quota(tmp_path, monkeypatch):
    def scene(api):
        user = api.start()
        assert api.send(user, "visit", thread=0)[0] == 200
        used = api.used(user)
        status, r = api.send(user, "message", "古いタブから", thread=99)
        return status, r, api.kinds, api.used(user) - used, api.model.calls

    out = both(tmp_path, monkeypatch, scene)
    assert out["json"][:2] == out["stream"][:2] == (200, {"stale": True, "thread": 1})
    assert out["stream"][2] == ["start", "stale"]
    assert out["json"][3:] == out["stream"][3:] == (0, 1)


def test_over_the_turn_limit_gets_the_limit_reply(tmp_path, monkeypatch):
    def scene(api):
        user = api.start()
        assert api.send(user, "message", "1回目")[0] == 200
        status, r = api.send(user, "message", "2回目")
        return status, r, api.kinds, api.model.calls

    out = both(tmp_path, monkeypatch, scene, limit="1")
    assert out["json"][:2] == out["stream"][:2] == (200, limit_reply("A", LIMIT_MESSAGE))
    assert out["stream"][2] == ["start", "reply"]
    assert out["json"][3] == out["stream"][3] == 1


def test_bad_events_are_refused_with_400_without_using_the_quota(tmp_path, monkeypatch):
    def scene(api):
        user = api.start()
        got = [api.send(user, "message", "  "), api.send(user, "nope", "こんにちは"), api.send(user, "resume_upload")]
        return got, api.used(user), api.model.calls

    out = both(tmp_path, monkeypatch, scene)
    assert out["json"] == out["stream"]
    got, used, calls = out["json"]
    assert [status for status, _ in got] == [400, 400, 400]
    assert [r["detail"] for _, r in got] == ["message には空でない text が必要です",
                                             f"event は {['message', 'resume_upload', 'visit', 'revisit', 'form_exit']} のいずれか",
                                             "アップロードした履歴書がありません"]
    assert (used, calls) == (0, 0)


def test_profile_changes_are_handed_over_after_the_reply_but_not_on_stale(tmp_path, monkeypatch):
    def scene(api):
        user = api.start()
        assert api.send(user, "visit", thread=0)[0] == 200
        profile.edit_summary(api.server.state, user, "夜に働きたい")
        assert len(profile.pending_changes(api.server.state, user)) == 1
        assert api.send(user, "message", "続きです", thread=1)[0] == 200
        after_reply = len(profile.pending_changes(api.server.state, user))
        profile.edit_summary(api.server.state, user, "土日に働きたい")
        stale = api.send(user, "message", "古いタブから", thread=99)
        return after_reply, stale, len(profile.pending_changes(api.server.state, user))

    out = both(tmp_path, monkeypatch, scene)
    assert out["json"] == out["stream"]
    after_reply, (_, stale), after_stale = out["json"]
    assert after_reply == 0 and stale == {"stale": True, "thread": 1} and after_stale == 1


def test_a_greeting_with_a_thread_records_the_open(tmp_path, monkeypatch):
    def scene(api):
        user = api.start()
        assert not api.server.state.user(user).get("talk_opens")
        status, r = api.send(user, "visit", thread=0)
        return status, r, bool(api.server.state.user(user).get("talk_opens")), api.kinds

    out = both(tmp_path, monkeypatch, scene)
    assert out["json"][:3] == out["stream"][:3]
    assert out["json"][0] == 200 and out["json"][2] is True
    assert out["stream"][3] == ["start", "thread", "llm", "reply"]


def test_the_turn_after_an_unread_upload_uses_it_up(tmp_path, monkeypatch):
    def scene(api):
        user = api.start()
        unread = api.client.app.state.unread_uploads
        unread[user] = {"file": "履歴書(PDF)", "shown": ["読み取れませんでした"], "empty": True}
        status, r = api.send(user, "resume_upload")
        return status, r, user in unread, api.used(user)

    out = both(tmp_path, monkeypatch, scene)
    assert out["json"] == out["stream"]
    assert out["json"][0] == 200 and out["json"][2:] == (False, 1)


class ParseFailed(ValueError):
    """構造化出力の失敗(OutputParserException も ValueError の子)の代わり。"""


class FakeRuntime:
    """決まった行を出す LineRuntime。fail を渡すと、その例外を reply の前に出す。今の JSON の API は handle を呼ぶので両方持つ。"""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail

    async def handle_stream(self, user_id, event, text=None, facts=None, talk_thread=None, notice=None,
                            new_conversation=False):
        yield {"kind": "llm", "ms": 1, "tool_calls": []}
        if self.fail is not None:
            raise self.fail
        yield {"kind": "reply", "entry": "A", "bubbles": ["はい"], "quick_replies": [], "cards": [],
               "unknown_job_ids": [], "buttons": [], "actions": [], "tools": []}

    async def handle(self, user_id, event, text=None, facts=None, talk_thread=None, notice=None):
        reply = None
        async for ev in self.handle_stream(user_id, event, text, facts, talk_thread, notice):
            if ev["kind"] == "reply":
                reply = {k: v for k, v in ev.items() if k != "kind"}
        return reply


def _with_runtime(rt, then=None):
    def scene(api):
        user = api.start()
        api.client.app.state.rt = rt
        if then is not None:
            then()
        got = api.send(user, "message", "こんにちは")
        return got, api.kinds, api.lines

    return scene


def test_a_value_error_from_the_runtime_is_a_400_or_an_error_line_with_the_original_name(tmp_path, monkeypatch):
    out = both(tmp_path, monkeypatch, _with_runtime(FakeRuntime(ParseFailed("構造化出力が読めない"))))
    assert out["json"][0] == (400, {"detail": "構造化出力が読めない"})
    assert out["stream"][1] == ["start", "llm", "error"]
    assert out["stream"][2][-1]["message"] == "ParseFailed: 構造化出力が読めない"


def test_another_error_from_the_runtime_is_a_500_or_an_error_line(tmp_path, monkeypatch):
    out = both(tmp_path, monkeypatch, _with_runtime(FakeRuntime(RuntimeError("MCP が落ちた"))),
               raise_server_exceptions=False)
    assert out["json"][0][0] == 500
    assert out["stream"][1] == ["start", "llm", "error"]
    assert out["stream"][2][-1]["message"] == "RuntimeError: MCP が落ちた"


def test_a_value_error_after_the_reply_is_a_500_not_a_400(tmp_path, monkeypatch):
    """reply を受け取った後の副作用(search_track など)の ValueError は、LineRuntime の失敗ではないので 400 にしない。"""
    def broken(*args, **kwargs):
        raise ValueError("search_track が壊れた")

    out = both(tmp_path, monkeypatch, _with_runtime(FakeRuntime(), lambda: monkeypatch.setattr(logic, "search_track", broken)),
               raise_server_exceptions=False)
    assert out["json"][0][0] == 500
    assert out["stream"][1] == ["start", "llm", "error"]
    assert out["stream"][2][-1]["message"] == "ValueError: search_track が壊れた"


def test_an_error_before_the_runtime_runs_is_a_500_or_cuts_the_stream(tmp_path, monkeypatch):
    """LineRuntime を回す前の受け付け(push_trigger など)の例外: JSON は 500、stream は error の行を出さずに切れる
    (設計メモの点 B、案 B1)。"""
    def scene(api):
        user = api.start("B-exit")

        def broken(*args, **kwargs):
            raise RuntimeError("dip が落ちた")

        monkeypatch.setattr(logic, "push_trigger", broken)
        body = {"user_id": user, "event": "form_exit"}
        path = "/api/event" if api.name == "json" else "/api/event/stream"
        r = api.client.post(path, json=body)
        return r.status_code, r.text

    out = both(tmp_path, monkeypatch, scene, raise_server_exceptions=False)
    assert out["json"][0] == 500
    status, text = out["stream"]
    assert status == 200 and '"error"' not in text
