#!/bin/sh
# 音声ワーカーだけのサービス(render.yaml の aica-voice)の起動。Dockerfile の CMD の代わりに render.yaml の dockerCommand で使う。
# 結果は Web サービスに、dip は Web サービスのコンテナの dip の MCP サーバーに、Render の private network でつなぐ。
# AICA_WEB_HOSTPORT(Web サービスの「内部の名前:10000」)と AICA_DIP_PORT は、render.yaml が Web サービスから写す。
set -eu

host="${AICA_WEB_HOSTPORT%:*}"
export VOICE_RESUME_TALK_URL="http://${AICA_WEB_HOSTPORT}"
export DIP_MCP_URL="http://${host}:${AICA_DIP_PORT}/mcp"
# モデル(VAD、turn detector)は build のときに image に入れてある(Dockerfile の AICA_VOICE_MODELS)
export HF_HOME=/app/models

exec python -m voice_resume.agent start
