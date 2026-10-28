"""外形監視: 公開 URL の /healthz を外から呼び、続けて落ちていたら Issue を開いて運営者に知らせる(実 API は使わない)。

    ./.venv/bin/python -m scripts.healthz_watch --url https://<名前>.onrender.com/healthz   # 呼ぶだけ(Issue は触らない)

.github/workflows/healthz.yml が 15 分ごとに、--repo と --assignee を付けて動かす。決めた理由は docs/architecture.md の関門 6、
止め方と確かめ方は docs/deploy/render.md の 8。GitHub の Actions の runner でそのまま動くよう、標準ライブラリだけを使う。

判定(1 回の実行の中):
- ATTEMPTS 回まで、WAIT 秒おきに GET する。1 回でも 200 なら動いている。全部が 200 以外か時間切れなら落ちている
  (デプロイで数秒止まるくらいでは知らせない)

知らせ方(Issue は本文の MARKER で見分ける。開いている間が「落ちている」の印):
- 落ちていて、開いた Issue がない → Issue を開き(運営者に割り当てる)、終了コード 1。GitHub の失敗の通知も届く。
  REOPEN_HOURS 時間のうちに閉じた Issue があれば、新しく開かずにそれを開き直す(落ちては戻るを繰り返すメモリ不足などで、Issue を増やさない)
- 落ちていて、開いた Issue がある → 開かない。同じ障害で何度も知らせないよう、終了コード 0 で警告だけ出す
- 動いていて、開いた Issue がある → 回復したと書いて閉じる
- Issue を読めない、開けない → 終了コード 1(GitHub の失敗の通知が代わりに届く。直るまで毎回届く)
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

ATTEMPTS = 3
WAIT = 60.0  # 試すあいだの秒。3 回で約 2 分見る(Render は /healthz が 60 秒失敗し続けると起動し直す)
TIMEOUT = 10.0  # 1 回の GET の秒
MARKER = "<!-- aica-healthz-watch -->"
TITLE = "外形監視: /healthz に届かない"
API = "https://api.github.com"
MAX_PAGES = 10  # 開いた Issue を探す上限(100 件 x 10)
REOPEN_HOURS = 24  # この時間のうちに閉じた Issue は、また落ちたら開き直す


def probe(url: str, timeout: float = TIMEOUT) -> str | None:
    """1 回 GET する。200 なら None、ほかは失敗の理由(HTTP の状態、時間切れ、つながらない、URL の誤り)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "aica-healthz-watch"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):  # つなぐところと TLS の時間切れは URLError に包まれる
            return f"時間切れ({timeout:g} 秒)"
        return f"つながらない({e.reason})"
    except TimeoutError:
        return f"時間切れ({timeout:g} 秒)"
    except ValueError:
        return "URL が正しくない(https:// から書く)"
    except (OSError, http.client.HTTPException) as e:
        return f"つながらない({type(e).__name__})"
    return None if status == 200 else f"HTTP {status}"


def check(url: str, attempts: int = ATTEMPTS, wait: float = WAIT, timeout: float = TIMEOUT,
          sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """attempts 回まで呼ぶ。1 回でも 200 なら [](動いている)。全部失敗なら、各回の失敗の理由(落ちている)。"""
    reasons: list[str] = []
    for i in range(attempts):
        if i:
            sleep(wait)
        reason = probe(url, timeout)
        if reason is None:
            return []
        reasons.append(reason)
    return reasons


def decide(down: bool, open_issue: int | None) -> str:
    """open(知らせる)、still_down(知らせ済み)、recover(Issue を閉じる)、ok(何もしない)。"""
    if down:
        return "still_down" if open_issue else "open"
    return "recover" if open_issue else "ok"


class GitHub:
    """Issue を読む、開く、閉じるだけの REST API の呼び出し。token は Actions の github.token(秘密情報は使わない)。"""

    def __init__(self, repo: str, token: str, api: str = API):
        self.repo, self.token, self.api = repo, token, api.rstrip("/")

    def _call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "aica-healthz-watch"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"null")

    def find_open(self) -> int | None:
        """MARKER のある開いた Issue の番号(古いもの)。無ければ None。PR は除く。"""
        found = []
        for page in range(1, MAX_PAGES + 1):
            items = self._call("GET", f"/issues?state=open&per_page=100&page={page}")
            found += [x["number"] for x in items if "pull_request" not in x and MARKER in (x.get("body") or "")]
            if len(items) < 100:
                break
        return min(found) if found else None

    def find_recent_closed(self, hours: float = REOPEN_HOURS) -> int | None:
        """hours 時間のうちに閉じた、MARKER のある Issue の番号(新しいもの)。無ければ None。"""
        since = datetime.now(UTC) - timedelta(hours=hours)
        items = self._call("GET", "/issues?state=closed&sort=updated&direction=desc&per_page=50")
        for x in items:
            closed = x.get("closed_at")
            if ("pull_request" not in x and MARKER in (x.get("body") or "") and closed
                    and datetime.fromisoformat(closed) >= since):
                return x["number"]
        return None

    def create(self, title: str, body: str, assignee: str = "") -> int:
        data = {"title": title, "body": body}
        try:
            return self._call("POST", "/issues", {**data, "assignees": [assignee]} if assignee else data)["number"]
        except urllib.error.HTTPError as e:
            if not assignee or e.code != 422:
                raise
            # 割り当てられない(名前の違いなど)ときも、Issue だけは開く
            return self._call("POST", "/issues", data)["number"]

    def close(self, number: int, comment: str) -> None:
        self._call("POST", f"/issues/{number}/comments", {"body": comment})
        self._call("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})

    def reopen(self, number: int, comment: str) -> None:
        self._call("PATCH", f"/issues/{number}", {"state": "open"})
        self._call("POST", f"/issues/{number}/comments", {"body": comment})


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _run_url() -> str:
    env = os.environ
    if env.get("GITHUB_RUN_ID"):
        return f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env.get('GITHUB_REPOSITORY')}/actions/runs/{env['GITHUB_RUN_ID']}"
    return "(手元で実行)"


def issue_body(url: str, reasons: list[str], again: bool = False) -> str:
    """開く Issue の本文。again は、閉じた Issue を開き直すときのコメント(MARKER を付けない)。"""
    lines = "\n".join(f"- {i}: {r}" for i, r in enumerate(reasons, 1))
    head = "また落ちました。\n\n" if again else f"{MARKER}\n"
    return (f"{head}`{url}` が、{len(reasons)} 回続けて 200 を返しませんでした({_now()})。\n\n{lines}\n\n"
            f"- 実行: {_run_url()}\n"
            "- 見るもの: Render の `aica` の「Events」「Logs」「Metrics」(docs/deploy/render.md の 8)\n"
            "- 回復すると、次の外形監視がこの Issue を閉じます。落ちている間は、新しい Issue を開きません。"
            f"閉じてから {REOPEN_HOURS} 時間のうちにまた落ちたら、この Issue を開き直します\n")


def _summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def run(url: str, gh: GitHub | None, assignee: str = "", *, attempts: int = ATTEMPTS, wait: float = WAIT,
        timeout: float = TIMEOUT, sleep: Callable[[float], None] = time.sleep) -> int:
    """1 回分の監視。終了コードを返す(1 = ジョブを失敗させる)。gh が None なら Issue を触らない。"""
    reasons = check(url, attempts, wait, timeout, sleep)
    down = bool(reasons)
    print(f"{url}: " + ("落ちている: " + " / ".join(reasons) if down else "200"))
    if gh is None:
        return 1 if down else 0
    open_issue = gh.find_open()
    action = decide(down, open_issue)
    if action == "open":
        number = gh.find_recent_closed()
        if number:
            gh.reopen(number, issue_body(url, reasons, again=True))
            verb = "開き直した"
        else:
            number = gh.create(TITLE, issue_body(url, reasons), assignee)
            verb = "開いた"
        print(f"::error::/healthz に {len(reasons)} 回続けて届かない。Issue #{number} を{verb}")
        _summary(f"外形監視: **落ちている**。Issue #{number} を{verb}")
        return 1
    if action == "still_down":
        print(f"::warning::/healthz にまだ届かない。Issue #{open_issue} で知らせ済みなので、開き直さない")
        _summary(f"外形監視: **落ちている**(Issue #{open_issue} で知らせ済み)")
        return 0
    if action == "recover":
        gh.close(open_issue, f"`{url}` が 200 を返しました({_now()})。回復したので閉じます。\n\n- 実行: {_run_url()}")
        print(f"回復した。Issue #{open_issue} を閉じた")
        _summary(f"外形監視: 回復した。Issue #{open_issue} を閉じた")
        return 0
    _summary("外形監視: 200")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", required=True, help="呼ぶ URL(https://<名前>.onrender.com/healthz)")
    ap.add_argument("--repo", default="", help="Issue を開くリポジトリ(owner/name)。無ければ Issue を触らない")
    ap.add_argument("--assignee", default="", help="開いた Issue を割り当てる GitHub のユーザー(運営者)")
    ap.add_argument("--attempts", type=int, default=ATTEMPTS)
    ap.add_argument("--wait", type=float, default=WAIT)
    ap.add_argument("--timeout", type=float, default=TIMEOUT)
    args = ap.parse_args(argv)
    gh = None
    if args.repo:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            print("::error::--repo を付けたときは、環境変数 GITHUB_TOKEN が要る", file=sys.stderr)
            return 2
        gh = GitHub(args.repo, token)
    return run(args.url, gh, args.assignee, attempts=args.attempts, wait=args.wait, timeout=args.timeout)


if __name__ == "__main__":
    sys.exit(main())
