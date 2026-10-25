"""プロフィールの「データを書き出す」と、「すべて消す」の後の費用台帳(task 216、docs/design/privacy.md の 3.7 と 3.3)。
実 LLM なし、dip の MCP は本物。会話と費用台帳はファイルに書く本物の LineRuntime で、値はすべて架空。

- 書き出す: 本人の保存内容(プロフィール、dip の状態の行、連携、会話、費用台帳の行)を 1 つの JSON のファイルで受け取れる。
  範囲は「すべて消す」と同じ(本人と、同じ Cookie の /dev のシナリオ利用者)。他人の分は入らない。見るだけなので状態を変えない
- すべて消す: 費用台帳のその人の行のユーザーIDを deleted に置き換える。金額、トークン数、行の数は変わらない
- 期間で消す掃除(task 208)と食い違わない: 外した行も、90 日を過ぎたら日ごとの合計にまとまる。合計は変わらない
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dip_mcp import retention
from line_runtime import ledger, purge
from line_runtime.quota import person
from tests.test_profile_page import Rememberer, _items, remembered, served_profile  # noqa: F401  後の 2 つはフィクスチャ
from tests.test_talk_actions import browser  # noqa: F401  フィクスチャ
from tests.test_talk_page import WORKER, app, open_talk, stream  # noqa: F401  app はフィクスチャ

T0 = datetime.now(UTC).replace(microsecond=0)
SCENARIO = "R-revisit-browsing"  # 連携済みのシナリオ(開発者画面)


@pytest.fixture
def on_disk(app, monkeypatch):  # noqa: F811
    """会話と費用台帳をファイルに書く本物の LineRuntime と、差し替えた掃除の時計。"""
    from line_runtime import runtime

    model = Rememberer()
    model.contexts = []
    monkeypatch.setattr(app, "LineRuntime", lambda: runtime.LineRuntime(model=model))
    clock = {"now": T0}
    monkeypatch.setattr(retention, "clock", lambda: clock["now"])
    app.clock_at = lambda at: clock.__setitem__("now", at)
    yield app
    del app.clock_at


def _costs(server) -> list[dict]:
    return ledger.load(server.config.DATA_DIR / "costs.jsonl")


def _total(rows: list[dict]) -> tuple:
    """行の数(日ごとの合計の行は、まとめた数)、トークン数、金額。"""
    return (sum(r.get("calls", 1) for r in rows), sum(r["input_tokens"] for r in rows),
            sum(r["output_tokens"] for r in rows), round(sum(r["cost_usd"] for r in rows), 6))


def _talk_with_a_scenario(client) -> tuple[str, str]:
    """トークで話し、プロフィールを直し、開発者画面のシナリオでも話す。(匿名ID, シナリオユーザー)"""
    opened, greeting = open_talk(client)
    uid = opened["user"]
    stream(client, uid, "message", greeting["thread"], "みほん駅で探して")
    assert client.put("/api/profile/items/area", json={"value": "みほん駅"}).status_code == 200
    client.get("/dev")
    scenario = client.post(f"/api/scenarios/{SCENARIO}/start").json()
    stream(client, scenario["user"], scenario["event"], None)
    return uid, scenario["user"]


def _another_browser(a, server) -> tuple[TestClient, str]:
    """別のブラウザ。lifespan は a のものなので、会話は同じランタイムで直接進める(tests/test_profile_page.py と同じ)。"""
    b = TestClient(server.app)
    b.get("/")
    ub = b.cookies[server.ANON_COOKIE]
    rt = server.app.state.rt
    a.portal.call(rt.handle, ub, "visit")
    a.portal.call(rt.handle, ub, "message", "探して")
    assert b.put("/api/profile/items/area", json={"value": "かくう町"}).status_code == 200
    return b, ub


# ----------------------------- 書き出す -----------------------------


def test_export_is_one_json_file_of_everything_saved_for_this_browser_and_nothing_of_others(on_disk):
    with TestClient(on_disk.app) as a:
        ua, scenario = _talk_with_a_scenario(a)
        _, ub = _another_browser(a, on_disk)
        on_disk.clock_at(T0 + timedelta(days=3))
        on_disk_before = on_disk.state.path.read_bytes()

        r = a.get("/api/profile/export", params={"user_id": ub})  # 他人を名乗っても、Cookie の本人の分だけ
        assert r.status_code == 200
        assert r.headers["content-disposition"] == f'attachment; filename="aica-data-{ledger.today(datetime.now(UTC))}.json"'
        assert r.headers["cache-control"] == "no-store"
        data = r.json()
        assert data["service"] == "aica" and data["user_id"] == ua and data["about"]
        assert _items(data["profile"])["area"] == "みほん駅"  # プロフィール画面と同じ内容

        stored = data["stored"]
        assert set(stored["users"]) == {ua, scenario}  # 保存しているそのままの形。シナリオ利用者の行も
        assert stored["users"][ua]["preferences"]["area"]["value"] == "みほん駅"
        assert stored["users"][ua][retention.LAST_SEEN] == T0.isoformat()
        assert set(stored["links"]) == {scenario}  # シナリオ利用者は dip の会員と連携済み

        talks = {(c["user_id"], c["thread"]): c for c in data["conversations"]}
        assert set(talks) == {(ua, 1), (scenario, 1)}
        assert [c["user_id"] for c in data["conversations"]] == [ua, scenario]  # 本人の会話が先
        assert [t["user"] for t in talks[(ua, 1)]["turns"]] == [None, "みほん駅で探して"]
        assert all(t["bot"] for t in talks[(ua, 1)]["turns"]) and talks[(ua, 1)]["last_at"]

        mine = [row for row in _costs(on_disk) if person(row["user_id"]) == ua]
        assert mine and data["costs"] == mine  # 費用台帳の本人とシナリオ利用者の行
        assert {row["user_id"] for row in mine} == {ua, scenario}

        assert ub not in r.text and "かくう町" not in r.text  # 他人の分は入らない
        assert on_disk.state.path.read_bytes() == on_disk_before  # 見るだけ: 最後の利用に数えず、何も書かない


def test_export_needs_the_browser_cookie_and_creates_no_user(on_disk):
    with TestClient(on_disk.app) as client:
        assert client.get("/api/profile/export").status_code == 401  # Cookie なし
        client.get("/")  # Cookie を受け取っただけ(まだ話していない)
        uid = client.cookies[on_disk.ANON_COOKIE]
        data = client.get("/api/profile/export").json()
        assert data["user_id"] == uid
        assert data["stored"] == {"users": {}, "links": {}} and data["conversations"] == [] and data["costs"] == []
        assert uid not in on_disk.state.user_ids()  # 見るだけではユーザーを作らない


# ----------------------------- すべて消す の後の費用台帳 -----------------------------


def test_forget_all_takes_the_user_ids_off_the_ledger_and_keeps_the_totals(on_disk):
    with TestClient(on_disk.app) as a:
        ua, scenario = _talk_with_a_scenario(a)
        _, ub = _another_browser(a, on_disk)
        before = _costs(on_disk)
        mine = [i for i, row in enumerate(before) if person(row["user_id"]) == ua]
        assert mine and any(person(row["user_id"]) == ub for row in before)

        assert a.post("/api/profile/forget").json() == {"forgotten": True}
        after = _costs(on_disk)
        assert len(after) == len(before) and _total(after) == _total(before)  # 金額とトークン数は残る
        for i, (old, new) in enumerate(zip(before, after, strict=True)):
            assert new == (old | {"user_id": ledger.FOLDED} if i in mine else old)  # ほかの人の行はそのまま
        text = (on_disk.config.DATA_DIR / "costs.jsonl").read_text(encoding="utf-8")
        assert ua not in text and scenario not in text
        assert not any(person(row["user_id"]) == ua for row in on_disk.app.state.rt.ledger.rows)  # メモリの行も
        assert a.get("/api/profile/export").json()["costs"] == []

        # 期間で消す掃除(task 208): 90 日を過ぎたら、外した行もほかの人の行と一緒に日ごとの合計になる
        on_disk.clock_at(T0 + timedelta(days=60))
        assert a.post(purge.INTERNAL_PATH, headers=WORKER).json()["ledger_rows"] == 0  # 90 日まではそのまま
        assert _costs(on_disk) == after
        on_disk.clock_at(T0 + ledger.ID_TTL + timedelta(days=1))
        out = a.post(purge.INTERNAL_PATH, headers=WORKER).json()
        assert out["ledger_rows"] == len(before) - len(mine)  # 数えるのは、まだユーザーIDがあった行だけ
        folded = _costs(on_disk)
        assert {row["event"] for row in folded} == {ledger.DAILY_TOTAL} and _total(folded) == _total(before)


def _row(at: datetime, user_id: str, cost: float = 0.25) -> dict:
    return {"at": at.isoformat(), "user_id": user_id, "event": "message", "model": "gpt-6-sol", "input_tokens": 100,
            "output_tokens": 10, "cache_read": 0, "cache_write": 0, "cost_usd": cost}


def test_rows_forgotten_before_90_days_are_still_folded_into_daily_totals(tmp_path):
    """90 日を過ぎた行が、どれも「すべて消す」でユーザーIDを外した行だけでも、日ごとの合計にまとめる
    (1 回の呼び出しの時刻とイベントを、期間を過ぎて残さない)。"""
    path = tmp_path / "costs.jsonl"
    now = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
    cutoff = now - ledger.ID_TTL
    rows = [_row(cutoff - timedelta(hours=3), "w-00000000000000c1"),
            _row(cutoff - timedelta(hours=2), "w-00000000000000c1:sim-new", 0.5),
            _row(cutoff + timedelta(hours=1), "w-00000000000000c1"),
            _row(cutoff + timedelta(hours=1), "w-00000000000000c2")]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    led = ledger.Ledger(path)

    assert led.forget("w-00000000000000c1") == 3
    assert led.forget("w-00000000000000c1") == 0  # 2 回目は何も変えない
    assert [r["user_id"] for r in ledger.load(path)] == [ledger.FOLDED] * 3 + ["w-00000000000000c2"]
    assert led.fold_old(now) == 0  # ユーザーIDを外した行はもう無い
    after = ledger.load(path)
    assert [(r["user_id"], r["event"], r.get("calls")) for r in after] == [
        (ledger.FOLDED, ledger.DAILY_TOTAL, 2), (ledger.FOLDED, "message", None), ("w-00000000000000c2", "message", None)]
    assert _total(after) == _total(rows)
    assert led.fold_old(now) == 0 and ledger.load(path) == after


# ----------------------------- 画面 -----------------------------


def test_export_button_on_the_profile_screen_downloads_the_file(remembered, browser, served_profile):  # noqa: F811
    context = browser.new_context(viewport={"width": 390, "height": 844}, accept_downloads=True)
    page = context.new_page()
    page.goto(served_profile + "/")
    page.get_by_text("こんにちは。お仕事探しをお手伝いします。何をしますか?").wait_for()
    uid = next(c["value"] for c in context.cookies() if c["name"] == "aica_uid")
    assert page.evaluate("fetch('/api/profile/items/area', {method: 'PUT', headers: {'Content-Type': 'application/json'},"
                         " body: JSON.stringify({value: 'みほん駅'})}).then(r => r.status)") == 200

    page.locator("#nav button[data-view=profile]").click()
    card = page.locator(".pcard", has=page.locator("h3", has_text="データを書き出す"))
    card.wait_for()
    assert card.locator(".pnote").inner_text() == \
        "aica が保存している、あなたのデータ(プロフィール、これまでの会話、費用の記録)を 1 つのファイル(JSON)で受け取れます。"
    forget = page.locator(".pcard.danger .pnote").inner_text()
    assert "費用の記録は、あなたを見分ける番号を外して残ります。" in forget
    with page.expect_download() as dl:
        card.locator('.prow[data-key="export"] a', has_text="書き出す").click()
    download = dl.value
    assert download.suggested_filename.startswith("aica-data-") and download.suggested_filename.endswith(".json")
    data = json.loads(Path(download.path()).read_text(encoding="utf-8"))
    assert data["user_id"] == uid and _items(data["profile"])["area"] == "みほん駅"
    assert data["conversations"][0]["turns"][0]["bot"]  # 挨拶の会話も入る
    context.close()
