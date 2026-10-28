"""外形監視の判定(scripts/healthz_watch.py)を、偽のサーバー(200、500、時間切れ)で確かめる。

- 200 は動いている、500 と時間切れは失敗
- 1 回の実行の中で続けて失敗したときだけ落ちているとし、そのときだけ Issue を開いて知らせる
- 落ちている間は Issue を開き直さない。回復したら閉じる。閉じてすぐまた落ちたら、新しく開かずに開き直す
- ワークフローに secrets.* がない(AGENTS.md の「CI に秘密情報を渡さない」)
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts import healthz_watch as hw

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 1.0  # 偽のサーバーの時間切れを待つ秒(混んだ runner でも 200 と 500 が時間切れにならない長さ)


class _Server(ThreadingHTTPServer):
    daemon_threads = True  # 止めるときに、時間切れで待っている handler を待たない


@pytest.fixture
def fake():
    """偽の /healthz。statuses を前から 1 つずつ返す(尽きたら最後のものを返し続ける)。"""
    release = threading.Event()  # 時間切れの handler を、片付けで放す
    state = {"statuses": [200], "hits": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            i = min(state["hits"], len(state["statuses"]) - 1)
            state["hits"] += 1
            status = state["statuses"][i]
            if status == "hang":
                release.wait(5)  # 上限つき。待つ側(probe)は TIMEOUT で諦める
                return
            body = b'{"status": "ok"}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    srv = _Server(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
    t.start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}/healthz"
    yield state
    release.set()
    srv.shutdown()
    srv.server_close()
    t.join(5)


def _check(fake, *statuses, attempts=3):
    fake["statuses"], fake["hits"] = list(statuses), 0
    slept = []
    reasons = hw.check(fake["url"], attempts=attempts, wait=60, timeout=TIMEOUT, sleep=slept.append)
    return reasons, slept


def test_200_is_up(fake):
    assert hw.probe(fake["url"], TIMEOUT) is None
    reasons, slept = _check(fake, 200)
    assert reasons == [] and slept == [] and fake["hits"] == 1


def test_500_fails(fake):
    fake["statuses"] = [500]
    assert hw.probe(fake["url"], TIMEOUT) == "HTTP 500"


def test_timeout_fails(fake):
    fake["statuses"] = ["hang"]
    assert "時間切れ" in hw.probe(fake["url"], TIMEOUT)


def test_refused_fails():
    srv = _Server(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = srv.server_address[1]
    srv.server_close()  # 誰も待っていないポート
    assert "つながらない" in hw.probe(f"http://127.0.0.1:{port}/healthz", TIMEOUT)


def test_bad_url_fails_without_crashing():
    assert "URL が正しくない" in hw.probe("aica.onrender.com/healthz", TIMEOUT)


def test_connect_timeout_is_timeout(monkeypatch):
    def urlopen(*_a, **_k):
        raise hw.urllib.error.URLError(TimeoutError("timed out"))  # つなぐところの時間切れは URLError に包まれる

    monkeypatch.setattr(hw.urllib.request, "urlopen", urlopen)
    assert "時間切れ" in hw.probe("https://example.invalid/healthz", TIMEOUT)


def test_one_failure_then_200_is_up(fake):
    """デプロイで数秒止まったくらいでは落ちているとしない。"""
    reasons, slept = _check(fake, 500, 200)
    assert reasons == [] and slept == [60] and fake["hits"] == 2


def test_consecutive_failures_are_down(fake):
    reasons, slept = _check(fake, 500, "hang", 503)
    assert reasons[0] == "HTTP 500" and "時間切れ" in reasons[1] and reasons[2] == "HTTP 503"
    assert slept == [60, 60] and fake["hits"] == 3


@pytest.mark.parametrize("down, issue, action", [
    (True, None, "open"), (True, 7, "still_down"), (False, 7, "recover"), (False, None, "ok"),
])
def test_decide(down, issue, action):
    assert hw.decide(down, issue) == action


class FakeGitHub:
    """Issue を覚えておくだけの GitHub。"""

    def __init__(self, open_issue=None, recent_closed=None):
        self.open_issue, self.recent_closed = open_issue, recent_closed
        self.created, self.closed, self.reopened = [], [], []

    def find_open(self):
        return self.open_issue

    def create(self, title, body, assignee=""):
        self.created.append((title, body, assignee))
        self.open_issue = 100 + len(self.created)
        return self.open_issue

    def close(self, number, comment):
        self.closed.append(number)
        self.open_issue, self.recent_closed = None, number

    def find_recent_closed(self):
        return self.recent_closed

    def reopen(self, number, comment):
        self.reopened.append((number, comment))
        self.open_issue, self.recent_closed = number, None


def _run(fake, gh, *statuses):
    fake["statuses"], fake["hits"] = list(statuses), 0
    return hw.run(fake["url"], gh, "operator", attempts=3, wait=0, timeout=TIMEOUT, sleep=lambda _s: None)


def test_run_notifies_only_on_consecutive_failures(fake):
    gh = FakeGitHub()
    assert _run(fake, gh, 500, 500, 200) == 0
    assert gh.created == []
    assert _run(fake, gh, 500, "hang", 500) == 1  # ジョブを失敗させる
    [(title, body, assignee)] = gh.created
    assert title == hw.TITLE and hw.MARKER in body and "HTTP 500" in body and "時間切れ" in body and assignee == "operator"


def test_run_does_not_reopen_during_same_outage_and_closes_on_recovery(fake):
    gh = FakeGitHub()
    assert _run(fake, gh, 500) == 1
    for _ in range(3):  # 落ちたまま、次の回、その次の回
        assert _run(fake, gh, 500) == 0
    assert len(gh.created) == 1 and gh.closed == []
    assert _run(fake, gh, 200) == 0
    assert gh.closed == [101] and gh.open_issue is None


def test_run_reopens_recently_closed_issue_when_flapping(fake):
    """落ちては戻るを繰り返しても(メモリ不足で起動し直すなど)、Issue を増やさず、閉じたものを開き直して知らせる。"""
    gh = FakeGitHub()
    assert _run(fake, gh, 500) == 1
    assert _run(fake, gh, 200) == 0
    assert _run(fake, gh, 500) == 1  # また落ちた: 知らせる(ジョブを失敗させる)が、新しくは開かない
    assert len(gh.created) == 1 and [n for n, _ in gh.reopened] == [101] and gh.open_issue == 101
    comment = gh.reopened[0][1]
    assert comment.startswith("また落ちました") and hw.MARKER not in comment
    assert _run(fake, gh, 500) == 0  # 開き直した後は、落ちている間は知らせ済み
    assert len(gh.reopened) == 1


def test_run_opens_new_issue_after_old_one_is_long_closed(fake):
    gh = FakeGitHub()  # REOPEN_HOURS より前に閉じた Issue は、find_recent_closed に出てこない
    assert _run(fake, gh, 500) == 1 and len(gh.created) == 1 and gh.reopened == []


def test_run_without_repo_only_checks(fake):
    assert _run(fake, None, 200) == 0
    assert _run(fake, None, 500) == 1


@pytest.fixture
def fake_api():
    """偽の GitHub の REST API。開いた Issue の一覧と、受けた要求を持つ。"""
    state = {"issues": [], "closed": [], "requests": [], "reject_assignee": False}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _record(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            state["requests"].append((self.command, self.path, body, self.headers.get("Authorization")))
            return body

        def do_GET(self):
            self._record()
            if "state=closed" in self.path:
                return self._send(200, state["closed"])
            page = int(self.path.rsplit("page=", 1)[1])
            self._send(200, state["issues"][(page - 1) * 100:page * 100])

        def do_POST(self):
            body = self._record()
            if self.path.endswith("/issues") and body.get("assignees") and state["reject_assignee"]:
                return self._send(422, {"message": "Validation Failed"})
            self._send(201, {"number": 9})

        def do_PATCH(self):
            self._record()
            self._send(200, {})

        def log_message(self, *_a):
            pass

    srv = _Server(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
    t.start()
    state["gh"] = hw.GitHub("o/r", "tok", api=f"http://127.0.0.1:{srv.server_address[1]}")
    yield state
    srv.shutdown()
    srv.server_close()
    t.join(5)


def test_github_find_open_uses_marker_and_skips_prs(fake_api):
    other = [{"number": n, "body": "別の Issue"} for n in range(1, 99)]
    fake_api["issues"] = other + [
        {"number": 120, "body": hw.MARKER, "pull_request": {}},  # PR は除く
        {"number": 150, "body": None},
        {"number": 140, "body": f"{hw.MARKER}\n落ちた"},  # 2 ページ目
    ]
    assert fake_api["gh"].find_open() == 140
    assert [r[1] for r in fake_api["requests"]] == ["/repos/o/r/issues?state=open&per_page=100&page=1",
                                                   "/repos/o/r/issues?state=open&per_page=100&page=2"]
    assert fake_api["requests"][0][3] == "Bearer tok"
    fake_api["issues"] = other
    assert fake_api["gh"].find_open() is None


def test_github_create_falls_back_without_assignee(fake_api):
    fake_api["reject_assignee"] = True
    assert fake_api["gh"].create("t", "b", "nobody") == 9
    assert [r[2] for r in fake_api["requests"]] == [{"title": "t", "body": "b", "assignees": ["nobody"]},
                                                   {"title": "t", "body": "b"}]


def test_github_close_comments_then_closes(fake_api):
    fake_api["gh"].close(9, "回復")
    assert [(r[0], r[1], r[2]) for r in fake_api["requests"]] == [
        ("POST", "/repos/o/r/issues/9/comments", {"body": "回復"}),
        ("PATCH", "/repos/o/r/issues/9", {"state": "closed", "state_reason": "completed"}),
    ]


def test_workflow_has_no_secrets():
    text = (ROOT / ".github/workflows/healthz.yml").read_text(encoding="utf-8")
    assert "secrets." not in text
    assert "${{ vars.AICA_HEALTHZ_URL }}" in text and "schedule:" in text and "issues: write" in text
    assert "python -m scripts.healthz_watch" in text


def test_github_find_recent_closed_only_within_window(fake_api):
    now = hw.datetime.now(hw.UTC)
    iso = lambda h: (now - hw.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fake_api["closed"] = [
        {"number": 30, "body": "別の Issue", "closed_at": iso(1)},
        {"number": 31, "body": hw.MARKER, "pull_request": {}, "closed_at": iso(1)},
        {"number": 32, "body": hw.MARKER, "closed_at": iso(2)},
        {"number": 33, "body": hw.MARKER, "closed_at": iso(30)},
    ]
    assert fake_api["gh"].find_recent_closed(24) == 32
    assert fake_api["gh"].find_recent_closed(1.5) is None


def test_github_reopen_opens_then_comments(fake_api):
    fake_api["gh"].reopen(9, "また落ちました")
    assert [(r[0], r[1], r[2]) for r in fake_api["requests"]] == [
        ("PATCH", "/repos/o/r/issues/9", {"state": "open"}),
        ("POST", "/repos/o/r/issues/9/comments", {"body": "また落ちました"}),
    ]
