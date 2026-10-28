"""Render の Events から、メモリ不足とインスタンスの失敗を拾って数える(運営者が手元で毎日 1 回動かす。task 322)。

    RENDER_API_KEY=… ./.venv/bin/python -m scripts.render_events                 # aica と aica-voice の、直近 24 時間と 7 日
    RENDER_API_KEY=… ./.venv/bin/python -m scripts.render_events --hours 72      # 直近 72 時間(週明けなど)

Render の API(GET /v1/services/{id}/events?type=server_failed)を読むだけで、何も変えない。費用はかからない(未確認)。
ダッシュボードの「Out of memory」と「Instance Failed」は、API ではどちらも server_failed で、メモリ不足のときは
details.reason.oomKilled が付く(render-oss/render-mcp-server の API の型から。本番の出来事では確かめていない)。
ほかの出来事(デプロイ、起動し直し、server_available など)は数えない。

判定(docs/deploy/render.md の「メモリ不足とインスタンスの失敗を毎日見る」):
- ok: 直近に 1 件もない。終了コード 0
- look: 直近に 1 件以上ある。日時を記録し、Metrics と Logs を見る。終了コード 1
- act: 直近 24 時間(--hours)に同じサービスでメモリ不足が 2 回以上、7 日で 3 回以上、または OOM 以外の失敗が 2 回以上。
  その日のうちに原因を見る。終了コード 1
API が答えない、鍵が無いときは終了コード 2。

鍵(RENDER_API_KEY)は、運営者が Render の「Account Settings」で発行し、手元のシェルにだけ置く。
鍵は運営者が属するすべてのワークスペースを読み書きできるので、.env にも CI にも入れない。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

API = "https://api.render.com/v1"
KEY_ENV = "RENDER_API_KEY"
SERVICES = ("aica", "aica-voice")
WEEK = timedelta(days=7)
PAGE = 100  # API の limit の上限
HTTP_TIMEOUT = 30
JST = timezone(timedelta(hours=9))

OOM = "oom"
FAILED = "failed"
LABEL = {OOM: "メモリ不足(Out of memory)", FAILED: "インスタンスの失敗(Instance Failed)"}

# act にする回数(同じサービスで)
OOM_RECENT_ACT = 2
OOM_WEEK_ACT = 3
FAILED_RECENT_ACT = 2

Fetch = Callable[[str, dict], list]  # (API のパス, クエリ) -> JSON の配列


class RenderApiError(Exception):
    pass


@dataclass(frozen=True)
class Failure:
    service: str
    kind: str  # OOM か FAILED
    at: datetime
    event_id: str
    detail: str


def classify(event: dict) -> tuple[str, str] | None:
    """1 件の出来事を、メモリ不足(OOM)、インスタンスの失敗(FAILED)、数えない(None)に分ける。2 つ目は理由の短い説明。"""
    if event.get("type") != "server_failed":
        return None
    reason = (event.get("details") or {}).get("reason") or {}
    oom = reason.get("oomKilled")
    if oom is not None:
        limit = (oom or {}).get("memoryLimit")
        return OOM, f"上限 {limit}" if limit else ""
    parts = []
    if reason.get("nonZeroExit") is not None:
        parts.append(f"終了コード {reason['nonZeroExit']}")
    if reason.get("earlyExit"):
        parts.append("早く終わった")
    if reason.get("evicted"):
        parts.append("追い出された" + (f"({reason['evictionReason']})" if reason.get("evictionReason") else ""))
    if reason.get("unhealthy"):
        parts.append(f"unhealthy({reason['unhealthy']})")
    if reason.get("timedOutSeconds") is not None:
        parts.append(f"{reason['timedOutSeconds']} 秒で時間切れ")
    if reason.get("timedOutReason"):
        parts.append(f"時間切れ({reason['timedOutReason']})")
    return FAILED, "、".join(parts)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)  # 3.11 から末尾の Z を読める


def failures(service: str, events: list[dict]) -> list[Failure]:
    out = []
    for e in events:
        c = classify(e)
        if c is not None:
            out.append(Failure(service, c[0], parse_time(e["timestamp"]), e.get("id", ""), c[1]))
    return sorted(out, key=lambda f: f.at)


def judge(found: list[Failure], now: datetime, recent: timedelta) -> str:
    """ok、look、act のどれか。found は直近 7 日(と recent の長いほう)の分。"""
    since = now - recent
    new = [f for f in found if f.at >= since]
    if not new:
        return "ok"
    for service in {f.service for f in found}:
        mine = [f for f in found if f.service == service]
        oom_new = sum(1 for f in mine if f.kind == OOM and f.at >= since)
        oom_week = sum(1 for f in mine if f.kind == OOM and f.at >= now - WEEK)
        failed_new = sum(1 for f in mine if f.kind == FAILED and f.at >= since)
        if oom_new >= OOM_RECENT_ACT or oom_week >= OOM_WEEK_ACT or failed_new >= FAILED_RECENT_ACT:
            return "act"
    return "look"


# ----------------------------- Render の API -----------------------------


def http_fetch(key: str) -> Fetch:
    def fetch(path: str, query: dict) -> list:
        url = f"{API}{path}?{urllib.parse.urlencode(query, doseq=True)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # 本文は出さない(鍵は本文に入らないが、念のため状態の番号だけ)
            raise RenderApiError(f"{path}: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RenderApiError(f"{path}: {type(e).__name__}") from None
    return fetch


def paged(fetch: Fetch, path: str, query: dict, key: str) -> list[dict]:
    """カーソルで全部をたどり、各要素の key(event、service)を返す。"""
    out, cursor = [], None
    while True:
        rows = fetch(path, {**query, "limit": PAGE, **({"cursor": cursor} if cursor else {})})
        out += [r[key] for r in rows]
        if len(rows) < PAGE:
            return out
        cursor = rows[-1]["cursor"]


def service_ids(fetch: Fetch, names: tuple[str, ...]) -> dict[str, str]:
    """名前からサービスの ID を引く。名前の絞り込みが部分一致でもよいように、ここで完全一致だけを残す。"""
    found = {s["name"]: s["id"] for s in paged(fetch, "/services", {"name": list(names)}, "service") if s["name"] in names}
    missing = [n for n in names if n not in found]
    if missing:
        raise RenderApiError(f"サービスが見つからない: {', '.join(missing)}")
    return found


def collect(fetch: Fetch, names: tuple[str, ...], now: datetime, recent: timedelta) -> list[Failure]:
    start = now - max(recent, WEEK)
    query = {"type": "server_failed", "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
             "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    out = []
    for name, sid in service_ids(fetch, names).items():
        out += failures(name, paged(fetch, f"/services/{sid}/events", query, "event"))
    return sorted(out, key=lambda f: f.at)


# ----------------------------- 出力 -----------------------------


def report(found: list[Failure], names: tuple[str, ...], now: datetime, recent: timedelta) -> list[str]:
    since = now - recent
    hours = int(recent.total_seconds() // 3600)
    lines = [f"{now.astimezone(JST):%Y-%m-%d %H:%M} 時点(日本時間)。直近 {hours} 時間 / 7 日"]
    for name in names:
        mine = [f for f in found if f.service == name]
        counts = []
        for kind in (OOM, FAILED):
            counts.append(f"{LABEL[kind]} {sum(1 for f in mine if f.kind == kind and f.at >= since)} / "
                          f"{sum(1 for f in mine if f.kind == kind and f.at >= now - WEEK)}")
        lines.append(f"  {name}: " + "、".join(counts))
    for f in found:
        mark = "*" if f.at >= since else " "
        lines.append(f"  {mark} {f.at.astimezone(JST):%m-%d %H:%M} {f.service} {LABEL[f.kind]}"
                     + (f" {f.detail}" if f.detail else "") + f" {f.event_id}")
    level = judge(found, now, recent)
    lines.append({"ok": "判定: ok(何もしない)",
                  "look": "判定: look(* の日時を記録し、その時刻の Metrics と Logs を見る)",
                  "act": "判定: act(増えている。その日のうちに原因を見る。docs/deploy/render.md の手順)"}[level])
    return lines


def main(argv: list[str] | None = None, fetch: Fetch | None = None, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=int, default=24, help="直近とみなす時間(既定 24)。7 日の数は別に出す")
    ap.add_argument("--service", action="append", help=f"見るサービスの名前(既定 {' と '.join(SERVICES)})。繰り返せる")
    args = ap.parse_args(argv)
    names = tuple(args.service or SERVICES)
    if fetch is None:
        key = os.environ.get(KEY_ENV, "").strip()
        if not key:
            print(f"NG: {KEY_ENV} がない。Render の Account Settings で発行し、シェルの環境変数で渡す", file=sys.stderr)
            return 2
        fetch = http_fetch(key)
    now = now or datetime.now(UTC)
    recent = timedelta(hours=args.hours)
    try:
        found = collect(fetch, names, now, recent)
    except (RenderApiError, KeyError, TypeError) as e:
        print(f"NG: Render の API: {e}", file=sys.stderr)
        return 2
    print("\n".join(report(found, names, now, recent)))
    return 0 if judge(found, now, recent) == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
