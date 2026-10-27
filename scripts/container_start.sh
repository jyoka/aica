#!/bin/sh
# コンテナの起動(Dockerfile の CMD)。Web プロセスを 0.0.0.0:$PORT で動かす。
# AICA_VOICE_WORKER=1 なら音声ワーカーも同じコンテナで動かす(手元の docker と、Render で別サービスに移す間)。Render の音声ワーカーは別サービスで
# (render.yaml の aica-voice、scripts/voice_worker_start.sh)、AICA_DIP_PORT の dip の MCP サーバーに private network でつなぐ。
set -eu

export LINE_SIM_HOST="${LINE_SIM_HOST:-0.0.0.0}"
export LINE_SIM_PORT="${LINE_SIM_PORT:-${PORT:-10000}}"

# 状態と出力の置き場(line_runtime.config.DATA_DIR などの既定)。永続ディスクでなければ、再起動で会話も状態も消える
if ! python -c 'import os, sys; sys.exit(0 if os.path.ismount("/app/data") else 1)'; then
    echo "WARNING: /app/data is not a mounted disk. Conversations and state will be lost on restart." >&2
fi

if [ "${AICA_VOICE_WORKER:-}" = "1" ]; then
    # 音声ワーカーは同じコンテナの Web プロセスに結果を返す。モデル(VAD、turn detector)はディスクに置き、2 回目からは読むだけ
    export VOICE_RESUME_TALK_URL="http://127.0.0.1:${LINE_SIM_PORT}"
    export HF_HOME="${HF_HOME:-/app/data/cache/huggingface}"
    # 音声は付け足しなので、モデルを取れなくても、ワーカーが落ちても Web プロセスは止めない(落ちたら 10 秒後に起動し直す)
    if python -m voice_resume.agent download-files; then
        (while true; do
            python -m voice_resume.agent start || true
            echo "WARNING: voice worker exited; restarting in 10s" >&2
            sleep 10
        done) &
    else
        echo "WARNING: could not download the voice models; starting without the voice worker" >&2
    fi
fi

if [ -n "${AICA_DIP_PORT:-}" ]; then
    # 別サービスの音声ワーカーが使う dip の MCP サーバー。状態(/app/data)はこのコンテナのディスクにあるので、ここで動かす。
    # 0.0.0.0 で開くので AICA_INTERNAL_KEY が要る(無ければ dip_mcp.server が起動しない)。落ちても Web プロセスは止めない
    (while true; do
        python -m dip_mcp.server --http "$AICA_DIP_PORT" --host 0.0.0.0 || true
        echo "WARNING: dip MCP server for the voice worker exited; restarting in 10s" >&2
        sleep 10
    done) &
fi

# ワーカーは 1 つ(会話数の上限 data/quota.json を 1 つのプロセスで持つ)。--forwarded-allow-ips は付けない
# (付けると uvicorn が X-Forwarded-For で接続元を書き換え、IP の上限を詐称できる。IP は server._client_ip が決める)
exec python -m line_runtime.server
