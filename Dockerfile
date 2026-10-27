# Render(または手元の Docker)で動かすコンテナ。Web プロセス(line_runtime.server)と、その子プロセスの dip の
# MCP サーバーが入る。AICA_VOICE_WORKER=1 なら音声ワーカーも同じコンテナで動かす(scripts/container_start.sh)。
# Render の音声ワーカーは、同じ image を別サービスで動かす(render.yaml の aica-voice、scripts/voice_worker_start.sh)。
# 状態と出力はすべて /app/data に書く。ここに永続ディスクをつなぐ(docs/deploy/render.md)。
FROM python:3.12-slim

# tini: PID 1 で、終わった子プロセスを片付ける。止めるときの SIGTERM は Web プロセスにだけ渡す(Web プロセスは進行中のターンを
# 終えてから dip の子プロセスを閉じる。-g でグループに渡すと、dip の子プロセスが先に止まり、進行中のターンが失敗する)
# ca-certificates: OS の CA の束(/etc/ssl/certs。aiohttp を使う LiveKit などが読む)を、Debian のパッチ済みの版に上げる。
# ベースの image にもとから入っているが、ここで install し直さないと、ベースを作った時点の古い版のままになる
# (eMudhra の中間証明書の漏洩。https://render.com/docs/advisory-emudhra 。確かめ方は scripts/check_image_certs.py)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tini && rm -rf /var/lib/apt/lists/*

# AICA_SECURE_COOKIE: 公開は https だけなので、匿名ユーザーIDの Cookie に Secure を付ける(手元の http のブラウザで試すなら 0 で上書き)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AICA_SECURE_COOKIE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /app/data && chmod +x scripts/container_start.sh scripts/voice_worker_start.sh

# 音声ワーカーのサービスだけ、モデル(VAD、turn detector。約 460MB)を image に入れる。起動のたびに取らずに済み、
# ディスクも要らない。Render はサービスの環境変数を build の引数として渡す(render.yaml の aica-voice が 1 にする)
ARG AICA_VOICE_MODELS=
RUN if [ "$AICA_VOICE_MODELS" = "1" ]; then HF_HOME=/app/models python -m voice_resume.agent download-files; fi

# Render は PORT(既定 10000)を渡す。container_start.sh がそれを LINE_SIM_PORT にする
EXPOSE 10000
ENTRYPOINT ["tini", "--"]
CMD ["scripts/container_start.sh"]
