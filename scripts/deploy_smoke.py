"""デプロイの smoke: コンテナを作って起動し、ヘルスチェックと初回の挨拶の 1 往復が通るかを確かめる(実 API を 1 回使う)。

    ./.venv/bin/python -m scripts.deploy_smoke                         # 手元: build → 起動 → 確かめる → 再起動 → 確かめる → 片付け
    ./.venv/bin/python -m scripts.deploy_smoke --url https://<名前>.onrender.com   # 公開 URL(build しない)
    ./.venv/bin/python -m scripts.deploy_smoke --url https://<名前>.onrender.com --cookie w-… --expect revisit

手元で確かめること:
- image に .env(API キー)が入っていない。API キーは docker run の環境変数でだけ渡す
- /healthz が 200 になる。GET / の Cookie に Secure と HttpOnly が付く
- 新しいブラウザでは /api/talk/open が同意を求め(入口は無い)、/api/consent で今の版に同意してから開き直すと visit を返し、
  /api/event/stream で挨拶(吹き出し)が返る
- 画面と返信の中に API キーの値が出てこない
- コンテナを再起動しても、会話(/api/talk/history)と同意と dip の状態(/app/data/state.json)が残る。/app/data は別につないだボリューム

公開 URL では、http が https に飛ぶことも確かめる。--cookie に前回の匿名ユーザーIDを渡すと、同じブラウザで開き直したときと
同じになる(最後のやりとりから 30 分を過ぎていれば再訪の挨拶。--expect revisit で確かめる)。

API キーはリポジトリ直下の .env か環境変数から読む(line_runtime.config.load_env)。docker と OPENAI_API_KEY が要る。
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from line_runtime import config

COOKIE = "aica_uid"  # line_runtime.server.ANON_COOKIE(server を import すると重いので名前だけ写す)
CONTAINER_PORT = "10000"  # Render が渡す PORT の既定
# コンテナに渡す環境変数(あれば)。値はコマンドの引数に載せず、docker に環境変数として渡す
PASS_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LINE_SIM_PROVIDER", "LINE_SIM_MODEL", "LINE_SIM_EFFORT")
BUILD_TIMEOUT = 1200  # 初回の build(pip install)は数分かかる
START_TIMEOUT = 180  # 起動(dip の MCP サーバーを立ち上げ、Prompt を読む)を待つ上限
REPLY_TIMEOUT = 180  # 挨拶の 1 往復(LLM)を待つ上限
HTTP_TIMEOUT = 30


class SmokeFailed(Exception):
    pass


def step(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def check(ok: bool, msg: str) -> None:
    if not ok:
        raise SmokeFailed(msg)
    step(f"ok: {msg}")


# ----------------------------- HTTP -----------------------------


def request(base: str, method: str, path: str, cookie: str | None = None, body: dict | None = None,
            timeout: float = HTTP_TIMEOUT):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if cookie:
        headers["Cookie"] = f"{COOKIE}={cookie}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(base: str, method: str, path: str, cookie: str | None = None, body: dict | None = None) -> dict:
    with request(base, method, path, cookie, body) as r:
        return json.loads(r.read())


def wait_healthy(base: str, timeout: float, container: str | None = None) -> None:
    """/healthz が 200 になるまで待つ(10 秒ごとに経過を出す)。コンテナが止まったら待たずに失敗にする。"""
    start = time.monotonic()
    last_note = start
    while True:
        try:
            with request(base, "GET", "/healthz", timeout=5) as r:
                if r.status == 200 and json.loads(r.read()).get("status") == "ok":
                    check(True, f"/healthz が 200({time.monotonic() - start:.0f} 秒)")
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            pass
        if container and not container_running(container):
            raise SmokeFailed("コンテナが起動の途中で止まりました")
        now = time.monotonic()
        if now - start > timeout:
            raise SmokeFailed(f"/healthz が {timeout:.0f} 秒で 200 になりませんでした")
        if now - last_note >= 10:
            step(f"起動を待っています({now - start:.0f} 秒)")
            last_note = now
        time.sleep(1)


def open_page(base: str, cookie: str | None) -> tuple[str, str]:
    """GET /(画面を開く)。(このブラウザの匿名ユーザーID, 画面の HTML)。新しいブラウザなら Cookie の属性も確かめる。"""
    with request(base, "GET", "/", cookie) as r:
        html = r.read().decode()
        set_cookie = r.headers.get_all("Set-Cookie") or []
    if cookie:
        return cookie, html
    jar = SimpleCookie()
    for c in set_cookie:
        jar.load(c)
    check(COOKIE in jar, "GET / で匿名ユーザーIDの Cookie が発行される")
    morsel = jar[COOKIE]
    check(bool(morsel["secure"]) and bool(morsel["httponly"]), "Cookie に Secure と HttpOnly が付いている")
    return morsel.value, html


def greet(base: str, uid: str, expect: str | None) -> tuple[dict, str]:
    """/api/talk/open と、新しい会話ならそのイベントで /api/event/stream(画面と同じ呼び方)。(open の結果, 返信の本文)。
    使い始める同意(terms)がまだなら、画面と同じく今の版で同意して(/api/consent)から開き直す。
    expect があれば、挨拶を呼ぶ前に入口を確かめる(違う入口で LLM を呼ばない)。"""
    opened = get_json(base, "POST", "/api/talk/open", uid)
    if "terms" in opened["consent"]["needed"]:
        check(opened["event"] is None, "同意の前は挨拶を始めない(入口が無い)")
        version = opened["consent"]["version"]
        step(f"使い始める同意がまだなので、画面の「同意して始める」と同じく版 {version!r} で同意します")
        agreed = get_json(base, "POST", "/api/consent", uid, {"kind": "terms", "version": version})
        check("terms" not in agreed["needed"], "同意が記録された")
        opened = get_json(base, "POST", "/api/talk/open", uid)
        check("terms" not in opened["consent"]["needed"], "同意の後に開き直すと、同意の画面を出さない")
    if opened["event"] is None:
        step("最後のやりとりから 30 分以内なので、今の会話の続きになりました(挨拶は呼んでいません)")
    if expect:
        check((opened["event"] or "continue") == expect, f"開いたときの入口が {expect}")
    if opened["event"] is None:
        return opened, ""
    body = {"user_id": uid, "event": opened["event"], "thread": opened["thread"]}
    reply, raw = None, []
    with request(base, "POST", "/api/event/stream", uid, body, timeout=REPLY_TIMEOUT) as r:
        for line in r:
            raw.append(line.decode())
            ev = json.loads(line)
            if ev["kind"] == "error":
                raise SmokeFailed(f"挨拶のターンが失敗しました: {ev['message']}")
            if ev["kind"] == "reply":
                reply = ev
    check(reply is not None and bool(reply.get("bubbles")) and not reply.get("limited"),
          f"{opened['event']} の挨拶が返った: {(reply or {}).get('bubbles', [''])[0][:60]!r}")
    return opened, "".join(raw)


def check_no_key(texts: list[str]) -> None:
    key = os.environ.get("OPENAI_API_KEY", "")
    if len(key) < 8:
        step("skip: OPENAI_API_KEY が手元に無いので、画面にキーが出ないかは確かめていません")
        return
    check(not any(key in t for t in texts), "画面と返信に OPENAI_API_KEY の値が出てこない")


def check_https_redirect(base: str) -> None:
    """http で開くと https に飛ぶ(Render がする。Cookie の Secure と合わせて、http で ID を送らない)。"""
    u = urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname, 80, timeout=HTTP_TIMEOUT)
    try:
        conn.request("GET", "/healthz")
        r = conn.getresponse()
        loc = r.getheader("Location") or ""
    finally:
        conn.close()
    check(r.status in (301, 302, 307, 308) and loc.startswith("https://"), f"http は https に飛ぶ({r.status} → {loc})")


def talk_checks(base: str, cookie: str | None, expect: str | None) -> str:
    """ヘルスチェックの後の、トークの確かめ。匿名ユーザーIDを返す。"""
    uid, html = open_page(base, cookie)
    _, stream = greet(base, uid, expect)
    pages = [html]
    for path in ("/dev", "/static/talk-core.js"):
        with request(base, "GET", path, uid) as r:
            pages.append(r.read().decode())
    with request(base, "GET", "/api/talk/history", uid) as r:
        pages.append(r.read().decode())
    check_no_key([*pages, stream])
    return uid


# ----------------------------- docker -----------------------------


def docker(*args: str, timeout: float = 60, capture: bool = True, env: dict | None = None) -> str:
    r = subprocess.run(["docker", *args], capture_output=capture, text=True, timeout=timeout, env=env, check=False)
    if r.returncode != 0:
        raise SmokeFailed(f"docker {args[0]} が失敗しました: {(r.stderr or '').strip()[-500:]}")
    return (r.stdout or "").strip()


def container_running(name: str) -> bool:
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True,
                       timeout=30, check=False)
    return r.stdout.strip() == "true"


def in_container(name: str, code: str) -> str:
    return docker("exec", name, "python", "-c", code, timeout=60)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def local_smoke(image: str, build: bool) -> None:
    if build:
        step(f"image を作ります({image}。初回は数分)")
        docker("build", "-t", image, str(config.ROOT), timeout=BUILD_TIMEOUT, capture=False)
    check(docker("run", "--rm", "--entrypoint", "python", image, "-c",
                 "import pathlib; print(pathlib.Path('/app/.env').exists())") == "False",
          "image に .env が入っていない")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SmokeFailed("OPENAI_API_KEY がありません(.env か環境変数)")

    tag = secrets.token_hex(4)
    name, volume, port = f"aica-smoke-{tag}", f"aica-smoke-{tag}", free_port()
    env = dict(os.environ) | {"PORT": CONTAINER_PORT}
    passed = ["-e", "PORT", *(a for k in PASS_ENV if os.environ.get(k) for a in ("-e", k))]
    base = f"http://127.0.0.1:{port}"
    try:
        docker("volume", "create", volume)
        docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:{CONTAINER_PORT}", *passed,
               "-v", f"{volume}:/app/data", image, env=env)
        step(f"コンテナ {name} を起動しました({base}、/app/data はボリューム {volume})")
        wait_healthy(base, START_TIMEOUT, name)
        uid = talk_checks(base, None, "visit")
        check(in_container(name, "import os; print(os.path.ismount('/app/data'))") == "True",
              "/app/data はコンテナの外のボリューム")

        step("コンテナを再起動します")
        docker("restart", name, timeout=120)
        wait_healthy(base, START_TIMEOUT, name)
        hist = get_json(base, "GET", "/api/talk/history", uid)
        check(len(hist["turns"]) >= 1 and bool(hist["turns"][0]["bot"]["bubbles"]), "再起動の後も会話の履歴が残っている")
        opened = get_json(base, "POST", "/api/talk/open", uid)
        check("terms" not in opened["consent"]["needed"], "再起動の後も同意が残っている")
        check(opened["event"] is None and opened["thread"] == hist["thread"], "再起動の後に開き直すと、今の会話の続きになる")
        state = in_container(name, "import json; print(json.dumps(sorted(json.load(open('/app/data/state.json'))['users'])))")
        check(uid in json.loads(state), "再起動の後も dip の状態(/app/data/state.json)に本人がいる")
        files = in_container(name, "import os; print(sorted(os.listdir('/app/data')))")
        step(f"/app/data の中身: {files}")
    except Exception:  # どこで失敗しても、コンテナのログを出してから片付ける
        logs = subprocess.run(["docker", "logs", "--tail", "40", name], capture_output=True, text=True, timeout=30,
                              check=False)
        print(logs.stdout + logs.stderr, file=sys.stderr)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)
        subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, timeout=60, check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", help="公開 URL(https://…)。与えると build せず、その URL で確かめる")
    ap.add_argument("--cookie", help="--url のとき: 前回の匿名ユーザーID(w-…)。同じブラウザで開き直したときと同じになる")
    ap.add_argument("--expect", choices=["visit", "revisit", "continue"], help="開いたときの入口(continue は今の会話の続き)")
    ap.add_argument("--image", default="aica:smoke", help="手元で作る image の名前")
    ap.add_argument("--no-build", action="store_true", help="手元: 作ってある --image をそのまま使う")
    args = ap.parse_args()
    config.load_env()
    try:
        if args.url:
            base = args.url.rstrip("/")
            if base.startswith("https://"):
                check_https_redirect(base)
            wait_healthy(base, START_TIMEOUT)
            uid = talk_checks(base, args.cookie, args.expect)
            step(f"このブラウザの匿名ユーザーID: {uid}(30 分より後に --cookie {uid} --expect revisit で再訪を確かめる)")
        else:
            local_smoke(args.image, build=not args.no_build)
    # URLError・接続の切断・docker が無いのは OSError、返事の形が違うのは ValueError(JSON)と KeyError
    except (SmokeFailed, OSError, ValueError, KeyError, http.client.HTTPException, subprocess.TimeoutExpired) as e:
        step(f"NG: {type(e).__name__}: {e}")
        return 1
    step("すべて通りました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
