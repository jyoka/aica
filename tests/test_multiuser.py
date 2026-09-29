"""マルチユーザー分離(task 58): ブラウザごとの匿名ユーザーID。

初回アクセスでIDを発行して Cookie で保持し、会話履歴・長期メモリ・行動履歴・費用台帳をそのIDでスコープする。
2ユーザーが並行して会話しても混線しないこと、他人のユーザーを覗けないことを、実 LLM なしで確かめる。
"""

from __future__ import annotations

import asyncio
import json
import os
import re

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

from dip_mcp import logic
from dip_mcp.data import State
from tests.test_handle import call, reply


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # モデルは作るが呼ばない
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    from line_runtime import config, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()
    return server


# ----------------------------- ID の発行(Cookie) -----------------------------


def test_first_access_issues_an_id_and_the_same_browser_reuses_it(tmp_path, monkeypatch):
    server = _env(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as client:
        r = client.get("/")
        assert server.ANON_COOKIE in r.cookies  # 画面を開いただけで発行される
        uid = client.get("/api/me").json()["user_id"]
        assert re.fullmatch(r"w-[0-9a-f]{16}", uid)
        assert uid == r.cookies[server.ANON_COOKIE]
        assert client.get("/api/me").json()["user_id"] == uid  # 再アクセスでも同じ ID
        client.cookies.clear()  # 別のブラウザ相当
        assert client.get("/api/me").json()["user_id"] != uid


def test_two_browsers_get_scoped_scenario_users_and_cannot_touch_each_other(tmp_path, monkeypatch):
    server = _env(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient

    with TestClient(server.app) as a:
        b = TestClient(server.app)  # 別ブラウザ(Cookie を共有しない。lifespan は a のものを使う)
        ua, ub = a.get("/api/me").json()["user_id"], b.get("/api/me").json()["user_id"]
        ra = a.post("/api/scenarios/F-follow-new/start").json()
        rb = b.post("/api/scenarios/F-follow-new/start").json()
        assert ra["user"] == f"{ua}:sim-new" and rb["user"] == f"{ub}:sim-new"  # 同じシナリオでも別ユーザー
        # 自分のユーザーは見られる。相手のユーザーとしては話せない・覗けない
        assert a.get(f"/api/dip/{ra['user']}").status_code == 200
        assert a.get(f"/api/dip/{rb['user']}").status_code == 403
        assert a.post("/api/event", json={"user_id": rb["user"], "event": "message", "text": "hi"}).status_code == 403
        assert a.post("/api/event/stream", json={"user_id": rb["user"], "event": "message", "text": "hi"}).status_code == 403
        assert a.post(f"/api/voice/{rb['user']}/start").status_code == 403
        assert a.post(f"/api/voice/{rb['user']}/wait", params={"since": 0, "timeout": 0}).status_code == 403
        assert a.get("/liff/apply", params={"user": rb["user"], "job": "a003"}).status_code == 403
        # 相手のトークへ音声相談の結果を差し込むこともできない
        assert a.post("/api/voice/result", json={"user_id": rb["user"], "draft_markdown": "偽の下書き"}).status_code == 403
        # Cookie の無い呼び出し(評価・テスト)は従来どおり user_id を信頼する
        nc = TestClient(server.app)
        assert nc.post("/api/scenarios/F-follow-new/start").json()["user"] == "sim-new"
        assert nc.get("/api/dip/sim-new").status_code == 200


def test_reset_user_can_seed_a_browser_user_from_a_scenario_template(tmp_path):
    state = State(tmp_path / "state.json")
    state.reset_user("w-aaaaaaaaaaaaaaaa:sim-browsing", template="sim-browsing")
    u = state.user("w-aaaaaaaaaaaaaaaa:sim-browsing")
    assert u["line_user_id"] == "w-aaaaaaaaaaaaaaaa:sim-browsing"
    assert u["dip_id"] and u["views"]  # fixtures の行動履歴ごと写る
    assert state.user("sim-browsing")["line_user_id"] == "sim-browsing"  # 元のシナリオユーザーはそのまま


# ----------------------------- 並行会話の分離(会話・長期メモリ・提案・費用台帳) -----------------------------


class RoutedModel(BaseChatModel):
    """システムプロンプトのユーザーIDごとに、決められた返事を順に返す(2ユーザーが並行しても取り違えない)。"""

    scripts: dict = {}  # noqa: RUF012  pydantic の項目

    @property
    def _llm_type(self) -> str:
        return "routed"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        uid = re.search(r"LINE ユーザーID: ([^\s((]+)", messages[0].content).group(1)
        return ChatResult(generations=[ChatGeneration(message=self.scripts[uid].pop(0))])


U1 = "w-1111111111111111:sim-new"
U2 = "w-2222222222222222:sim-new"


def test_two_users_talk_in_parallel_without_mixing(tmp_path):
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.ledger import Ledger
    from line_runtime.runtime import LineRuntime

    state = State(tmp_path / "state.json")
    shibuya = [j["job_id"] for j in logic.recommend(state, None, area="渋谷", time_band="日中")["jobs"]]
    ikebukuro = [j["job_id"] for j in logic.recommend(state, None, area="池袋", time_band="夜")["jobs"]]
    assert shibuya and ikebukuro and not set(shibuya) & set(ikebukuro)

    model = RoutedModel(scripts={
        U1: [call("save_user_preferences", {"line_user_id": U1, "preferences": {"area": "渋谷"}}),
             call("recommend_jobs", {"line_user_id": U1, "area": "渋谷", "time_band": "日中"}, "t2"),
             reply(["渋谷・日中で探しました。"], job_ids=shibuya)],
        U2: [call("recommend_jobs", {"line_user_id": U2, "area": "池袋", "time_band": "夜"}),
             reply(["池袋・夜で探しました。"], job_ids=ikebukuro)],
    })
    ledger = Ledger(tmp_path / "costs.jsonl")

    async def go():
        async with LineRuntime(model=model, in_memory=True, ledger=ledger) as rt:
            r1, r2 = await asyncio.gather(rt.handle(U1, "message", "渋谷で日中に働きたい"),
                                          rt.handle(U2, "message", "池袋で夜に働きたい"))
            texts = []
            for u in (U1, U2):
                cfg = rt._cfg(u, await rt._current(u))
                msgs = (await rt.agent.aget_state(cfg)).values["messages"]
                texts.append([m.content for m in msgs if m.type == "human"])
            return r1, r2, texts

    r1, r2, (h1, h2) = asyncio.run(go())
    # 提案が混線しない: それぞれ自分の条件の求人カードだけが返る
    assert [c["job_id"] for c in r1["cards"]] == shibuya
    assert [c["job_id"] for c in r2["cards"]] == ikebukuro
    # 会話履歴が混線しない: 各ユーザーの会話には本人の発言しかない
    assert h1 == ["渋谷で日中に働きたい"] and h2 == ["池袋で夜に働きたい"]
    # 長期メモリが混線しない: U1 が話した希望は U1 の要約だけに入る
    assert logic.conversation_summary(state, U1)["stated_preferences"]["area"]["value"] == "渋谷"
    assert logic.conversation_summary(state, U2)["stated_preferences"] == {}
    # 費用台帳: すべての行に user_id が付き、2ユーザー分が区別できる
    rows = [json.loads(x) for x in (tmp_path / "costs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows == ledger.rows
    assert {r["user_id"] for r in rows} == {U1, U2}
    assert all(r["model"] and "cost_usd" in r and r["event"] == "message" for r in rows)


def test_saved_preferences_stay_out_of_another_users_revisit_summary(tmp_path):
    """一方のユーザーの保存済み希望が、他方の再訪挨拶(の材料になる要約)に現れない。"""
    state = State(tmp_path / "state.json")
    logic.save_preferences(state, U1, {"area": "渋谷", "time_band": "夜"})
    mine = logic.conversation_summary(state, U1)
    other = logic.conversation_summary(state, U2)
    assert {k: v["value"] for k, v in mine["stated_preferences"].items()} == {"area": "渋谷", "time_band": "夜"}
    assert other["stated_preferences"] == {}


# ----------------------------- 費用台帳の行 -----------------------------


def test_ledger_row_carries_user_id_and_cost(tmp_path):
    from line_runtime.ledger import Ledger

    led = Ledger(tmp_path / "costs.jsonl")
    row = led.record("w-1111111111111111", "message", "claude-opus-5",
                     {"input_tokens": 2000, "output_tokens": 10, "cache_read": 1500, "cache_write": 300})
    assert row["user_id"] == "w-1111111111111111"
    # input 2000 のうちキャッシュ読み 1500・書き 300 → 素の入力 200。$5/$25 per 1M
    assert row["cost_usd"] == round(((200 + 1.25 * 300 + 0.1 * 1500) * 5.0 + 10 * 25.0) / 1e6, 6)
    on_disk = json.loads((tmp_path / "costs.jsonl").read_text(encoding="utf-8"))
    assert on_disk == row
