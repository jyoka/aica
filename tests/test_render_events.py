"""メモリ不足とインスタンスの失敗の判定(task 322): 偽の Render の出来事で、拾うものと拾わないものを確かめる。

Render の API は呼ばない。出来事の形は render-oss/render-mcp-server の API の型(serviceEventWithCursor、failureReason)に合わせた。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts import render_events as re_

NOW = datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
IDS = {"aica": "srv-web", "aica-voice": "srv-voice"}


def ev(kind: str, hours_ago: float, reason: dict | None = None, n: int = 0) -> dict:
    t = NOW - timedelta(hours=hours_ago)
    details = {"reason": reason} if reason is not None else {}
    return {"id": f"evt-{kind}-{hours_ago}-{n}", "serviceId": "srv", "type": kind,
            "timestamp": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "details": details}


def oom(hours_ago: float, n: int = 0) -> dict:
    return ev("server_failed", hours_ago, {"evicted": False, "oomKilled": {"memoryLimit": "2Gi"}}, n)


def crashed(hours_ago: float, code: int = 1, n: int = 0) -> dict:
    return ev("server_failed", hours_ago, {"evicted": False, "nonZeroExit": code}, n)


# 関係のない出来事。どれも数えない
UNRELATED = [
    ev("server_restarted", 1, None), ev("server_available", 1, None), ev("deploy_ended", 2, None),
    ev("build_ended", 2, None), ev("server_hardware_failure", 3, None), ev("service_suspended", 3, None),
    ev("disk_updated", 4, None),
]


class FakeRender:
    """Render の API の偽物。type=server_failed の絞り込み、時刻の範囲、limit とカーソルをまねる。"""

    def __init__(self, events: dict[str, list[dict]], honor_type: bool = True):
        self.events = events
        self.honor_type = honor_type  # False: API が type の絞り込みを無視したとき
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, path: str, query: dict) -> list:
        self.calls.append((path, query))
        if path == "/services":
            rows = [{"service": {"id": IDS[n], "name": n}, "cursor": f"c-{n}"} for n in IDS]
            rows.append({"service": {"id": "srv-other", "name": "aica-voice-old"}, "cursor": "c-x"})  # 部分一致で返ったもの
            return rows
        name = next(n for n, i in IDS.items() if path == f"/services/{i}/events")
        start = re_.parse_time(query["startTime"])
        end = re_.parse_time(query["endTime"])
        rows = [e for e in self.events.get(name, [])
                if (not self.honor_type or e["type"] == query["type"]) and start <= re_.parse_time(e["timestamp"]) <= end]
        offset = int(query.get("cursor", "0"))
        page = rows[offset:offset + query["limit"]]
        return [{"event": e, "cursor": str(offset + i + 1)} for i, e in enumerate(page)]


def run(events: dict[str, list[dict]], capsys, *argv: str, keep_type_filter: bool = True) -> tuple[int, str]:
    # keep_type_filter=False: API が type の絞り込みを無視して、ほかの出来事を返しても数えない
    code = re_.main(list(argv), fetch=FakeRender(events, honor_type=keep_type_filter), now=NOW)
    return code, capsys.readouterr().out


def test_classify_tells_out_of_memory_from_other_instance_failures():
    assert re_.classify(oom(1)) == (re_.OOM, "上限 2Gi")
    assert re_.classify(crashed(1, 137)) == (re_.FAILED, "終了コード 137")
    assert re_.classify(ev("server_failed", 1, {"evicted": True, "evictionReason": "node drained"}))[0] == re_.FAILED
    assert re_.classify(ev("server_failed", 1, {"evicted": False, "unhealthy": "/healthz 503"})) == (
        re_.FAILED, "unhealthy(/healthz 503)")
    assert re_.classify(ev("server_failed", 1, None)) == (re_.FAILED, "")  # 理由が無くても失敗として数える


@pytest.mark.parametrize("event", UNRELATED, ids=lambda e: e["type"])
def test_unrelated_events_are_not_counted(event):
    assert re_.classify(event) is None


def test_nothing_to_report_when_only_unrelated_events(capsys):
    code, out = run({"aica": UNRELATED, "aica-voice": UNRELATED}, capsys, keep_type_filter=False)
    assert code == 0
    assert "判定: ok" in out
    assert "aica: メモリ不足(Out of memory) 0 / 0、インスタンスの失敗(Instance Failed) 0 / 0" in out


def test_one_out_of_memory_today_is_look(capsys):
    code, out = run({"aica": [oom(3), *UNRELATED]}, capsys, keep_type_filter=False)
    assert code == 1
    assert "判定: look" in out
    assert "* 10-28 06:00 aica メモリ不足(Out of memory) 上限 2Gi evt-server_failed-3-0" in out  # 日本時間
    assert "aica: メモリ不足(Out of memory) 1 / 1" in out


def test_one_instance_failed_is_look(capsys):
    code, out = run({"aica-voice": [crashed(5, 1)]}, capsys)
    assert code == 1 and "判定: look" in out
    assert "aica-voice インスタンスの失敗(Instance Failed) 終了コード 1" in out


def test_two_out_of_memory_in_a_day_is_act(capsys):
    code, out = run({"aica": [oom(2), oom(20)]}, capsys)
    assert code == 1 and "判定: act" in out


def test_three_out_of_memory_in_a_week_is_act_even_if_one_is_today(capsys):
    code, out = run({"aica": [oom(1), oom(50), oom(100)]}, capsys)
    assert code == 1 and "判定: act" in out
    assert "aica: メモリ不足(Out of memory) 1 / 3" in out


def test_old_failures_alone_are_ok_but_listed(capsys):
    code, out = run({"aica": [oom(50), oom(100)]}, capsys)
    assert code == 0 and "判定: ok" in out
    assert "  10-26 07:00 aica メモリ不足" in out  # 直近ではないので * が付かない


def test_failures_older_than_a_week_are_not_fetched(capsys):
    code, out = run({"aica": [oom(24 * 8)]}, capsys)
    assert code == 0 and "0 / 0" in out


def test_counts_are_per_service(capsys):
    # 2 つのサービスで 1 回ずつなら、同じサービスで 2 回ではないので look
    code, out = run({"aica": [oom(2)], "aica-voice": [oom(3)]}, capsys)
    assert code == 1 and "判定: look" in out


def test_hours_widens_the_recent_window(capsys):
    code, out = run({"aica": [oom(30)]}, capsys, "--hours", "72")
    assert code == 1 and "直近 72 時間" in out and "判定: look" in out


def test_pages_are_followed_with_the_cursor(capsys):
    many = [crashed(1 + i * 0.01, n=i) for i in range(re_.PAGE + 5)]
    fake = FakeRender({"aica": many})
    re_.main([], fetch=fake, now=NOW)
    out = capsys.readouterr().out
    assert f"インスタンスの失敗(Instance Failed) {re_.PAGE + 5} / {re_.PAGE + 5}" in out
    pages = [q for p, q in fake.calls if p == "/services/srv-web/events"]
    assert len(pages) == 2 and pages[1]["cursor"] == str(re_.PAGE)
    assert all(q["type"] == "server_failed" for q in pages)


def test_only_exact_service_names_are_used(capsys):
    fake = FakeRender({})
    re_.main([], fetch=fake, now=NOW)
    assert not any(p == "/services/srv-other/events" for p, _ in fake.calls)


def test_missing_service_is_an_error(capsys):
    code = re_.main(["--service", "nope"], fetch=FakeRender({}), now=NOW)
    assert code == 2 and "サービスが見つからない: nope" in capsys.readouterr().err


def test_api_errors_exit_2(capsys):
    def broken(path, query):
        raise re_.RenderApiError(f"{path}: HTTP 401")
    assert re_.main([], fetch=broken, now=NOW) == 2
    assert "HTTP 401" in capsys.readouterr().err


def test_missing_key_exits_2_without_calling_the_api(capsys, monkeypatch):
    monkeypatch.delenv(re_.KEY_ENV, raising=False)
    monkeypatch.setattr(re_, "http_fetch", lambda key: pytest.fail("API を呼んだ"))
    assert re_.main([]) == 2
    assert re_.KEY_ENV in capsys.readouterr().err
