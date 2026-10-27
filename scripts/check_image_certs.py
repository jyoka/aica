"""image の CA 証明書の確かめ: certifi と Debian の ca-certificates が、eMudhra の漏洩に対応した版になっているか(実 API は使わない)。

    ./.venv/bin/python -m scripts.check_image_certs              # build → 確かめる
    ./.venv/bin/python -m scripts.check_image_certs --no-build   # 作ってある image(aica:certs)を使う
    ./.venv/bin/python -m scripts.check_image_certs --probe      # 漏洩した証明書の試験用のホストに、つながらないことも確かめる

確かめること(Render の通告 https://render.com/docs/advisory-emudhra 、2026-10-27):
- certifi(httpx、openai、anthropic が使う CA の束)が 2026.10.19 以上
- ca-certificates(OS の CA の束 /etc/ssl/certs。aiohttp を使う LiveKit などが読む)が、Debian の今の候補の版と同じ。
  image の中で apt-get update をして比べるので、ネットワークが要る。古いキャッシュの層で build したときは、ここで NG になる
- --probe: 漏洩した証明書を使う試験用のホスト(通告に載っている)に、certifi でも OS の CA の束でも、証明書の検証で失敗する
  (2026-10-27 に確かめたところ、試験用のホストの鎖の根の emSign ECC Root CA - G3 はどちらの束にも無く、certifi 2026.10.18 でも
  失敗した。版が古いことはこれでは見分けられないので、上の版の確かめが本体で、--probe は念のため)

docker が要る。API キーは要らない(コンテナに環境変数を渡さない)。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from line_runtime import config

CERTIFI_MIN = (2026, 10, 19)
# 通告に載っている、漏洩した中間証明書を使う試験用のホスト。対応していればつながらない
PROBE_HOSTS = ("dv-g3b-emudhra.we-have-a-problem.com", "dv-g3b-aaa-emudhra.we-have-a-problem.com")
BUILD_TIMEOUT = 1200  # 初回の build(pip install)は数分かかる
RUN_TIMEOUT = 180  # apt-get update とホストへの接続を含む

# コンテナの中で動かす。結果を 1 行の JSON で返す
INSIDE = r"""
import json, socket, ssl, subprocess, sys
import certifi
out = {"certifi": certifi.__version__}
q = lambda fmt: subprocess.run(["dpkg-query", "-W", "-f", fmt, "ca-certificates"], capture_output=True, text=True).stdout
out["ca_installed"] = q("${Version}")
subprocess.run(["apt-get", "update", "-qq"], capture_output=True, check=True)
policy = subprocess.run(["apt-cache", "policy", "ca-certificates"], capture_output=True, text=True).stdout
out["ca_candidate"] = next((x.split(":", 1)[1].strip() for x in policy.splitlines() if x.strip().startswith("Candidate:")), "")
def probe(host, ctx):
    try:
        with socket.create_connection((host, 443), timeout=15) as s, ctx.wrap_socket(s, server_hostname=host):
            return "connected"
    except ssl.SSLCertVerificationError as e:
        return "rejected: " + str(e.verify_message)
    except OSError as e:
        return "error: " + repr(e)
if "--probe" in sys.argv:
    out["probe"] = {h: {"certifi": probe(h, ssl.create_default_context(cafile=certifi.where())),
                        "system": probe(h, ssl.create_default_context())} for h in sys.argv[2:]}
print(json.dumps(out))
"""


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def docker(*args: str, timeout: float, capture: bool = True) -> str:
    r = subprocess.run(["docker", *args], capture_output=capture, text=True, timeout=timeout, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"docker {args[0]} が失敗しました: {(r.stderr or '').strip()[-500:]}")
    return r.stdout or ""


def check(image: str, probe: bool) -> list[str]:
    """NG の理由の一覧を返す(空なら合格)。"""
    extra = ["--probe", *PROBE_HOSTS] if probe else []
    out = json.loads(docker("run", "--rm", "--entrypoint", "python", image, "-c", INSIDE, *extra,
                            timeout=RUN_TIMEOUT).strip().splitlines()[-1])
    print(f"certifi: {out['certifi']}")
    print(f"ca-certificates: 入っている版 {out['ca_installed']}、Debian の候補 {out['ca_candidate']}")
    bad = []
    if version_tuple(out["certifi"]) < CERTIFI_MIN:
        bad.append(f"certifi {out['certifi']} が 2026.10.19 より古い")
    if not out["ca_installed"] or out["ca_installed"] != out["ca_candidate"]:
        bad.append("ca-certificates が Debian の今の候補の版でない(--no-cache で build し直す)")
    for host, results in out.get("probe", {}).items():
        for store, result in results.items():
            print(f"probe {host} ({store}): {result}")
            if result == "connected":
                bad.append(f"{host} に {store} の CA の束でつながった(漏洩した証明書を信頼している)")
            elif not result.startswith("rejected"):
                bad.append(f"{host} に {store} で確かめられなかった(ネットワーク?)")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--image", default="aica:certs", help="確かめる image の名前")
    ap.add_argument("--no-build", action="store_true", help="作ってある --image をそのまま使う")
    ap.add_argument("--probe", action="store_true", help="漏洩した証明書の試験用のホストに、つながらないことも確かめる")
    args = ap.parse_args()
    try:
        if not args.no_build:
            docker("build", "-t", args.image, str(config.ROOT), timeout=BUILD_TIMEOUT, capture=False)
        bad = check(args.image, args.probe)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError, KeyError) as e:
        print(f"NG: {e}", file=sys.stderr)
        return 1
    for b in bad:
        print(f"NG: {b}", file=sys.stderr)
    if not bad:
        print("OK: image の CA 証明書は eMudhra の漏洩に対応した版")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
