"""voice-resume の設定。モデル名・ボイス・ターン判定・出力先・.env 読み込み。

構成(タスク 31 で Realtime から作り直し):
- STT: Deepgram nova-3(日本語)+ バイト用語の keyterm 登録。タスク 37 の比較
  (results/stt-eval-*.md)で「時給の下限」「合うと思う」の聞き間違いが直り、
  遅延も一番短かった。nova-2 に戻すなら VOICE_RESUME_STT_MODEL=nova-2、
  OpenAI にするなら VOICE_RESUME_STT_PROVIDER=openai(モデルは gpt-4o-transcribe など)
- LLM: OpenAI gpt-6-sol(会話・下書き生成とも。line_runtime の既定 OpenAI モデルと同じ)
- TTS: Cartesia sonic-3.5 の日本語ボイス。速さは標準より少し遅い "slow" が既定
- ターン判定: Silero VAD + LiveKit turn detector(多言語)。終わり判定は少し待つ

いずれも環境変数で差し替えられる(VOICE_RESUME_*)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # リポジトリ直下
# LIVEKIT_* / OPENAI_API_KEY / DEEPGRAM_API_KEY / CARTESIA_API_KEY はリポジトリ直下の .env(line_runtime と共通)
ENV_FILE = ROOT / ".env"

# --- STT(文字起こし)---
# provider は deepgram / openai。作り分けは voice_resume/stt.py の create_stt()
STT_PROVIDER = os.environ.get("VOICE_RESUME_STT_PROVIDER", "deepgram")
STT_MODEL = os.environ.get("VOICE_RESUME_STT_MODEL", "nova-3")
STT_LANGUAGE = os.environ.get("VOICE_RESUME_STT_LANGUAGE", "ja")
# バイト探しの用語。deepgram nova-3 では keyterm、openai では prompt のヒントとして渡す。
# 実会話で「時給の下限」→「地球の加減」のような聞き間違いがあった(タスク 37)
STT_KEYTERMS = [
    t.strip()
    for t in os.environ.get(
        "VOICE_RESUME_STT_KEYTERMS", "時給,下限,シフト,品出し,ホール,キッチン,求人,履歴書"
    ).split(",")
    if t.strip()
]

# --- LLM(会話の脳・下書き生成)---
LLM_MODEL = os.environ.get("VOICE_RESUME_LLM_MODEL", "gpt-6-sol")
# gpt-6-sol は chat completions でツールを使うとき reasoning_effort="none" が必要。
# 音声会話は低レイテンシ優先なので none を既定にする
LLM_REASONING_EFFORT = os.environ.get("VOICE_RESUME_LLM_REASONING_EFFORT", "none")
DRAFT_MODEL = os.environ.get("VOICE_RESUME_DRAFT_MODEL", "gpt-6-sol")
# 会話中の調べもの(web_research)。Responses API の web_search を使う
RESEARCH_MODEL = os.environ.get("VOICE_RESUME_RESEARCH_MODEL", "gpt-6-sol")

# --- TTS(声)。ボイスと速さは設定で変えられる ---
TTS_MODEL = os.environ.get("VOICE_RESUME_TTS_MODEL", "sonic-3.5")
TTS_VOICE = os.environ.get(
    "VOICE_RESUME_TTS_VOICE", "1d210168-d764-462c-8ab6-288a6d5a9579"  # Cartesia の日本語ボイス
)
TTS_LANGUAGE = os.environ.get("VOICE_RESUME_TTS_LANGUAGE", "ja")
# sonic-3 系は 0.6〜2.0 の倍率(1.0 = 標準)。既定は標準より少し遅い 0.9
TTS_SPEED = float(os.environ.get("VOICE_RESUME_TTS_SPEED", "0.9"))

# --- ターンの判定(考え中の「えーと」で打ち切らないための余裕)---
# 発話が終わったと見なすまでの待ち時間(秒)。既定 0.5 より長めにする
MIN_ENDPOINTING_DELAY = float(os.environ.get("VOICE_RESUME_MIN_ENDPOINTING_DELAY", "0.8"))
# turn detector が「まだ続く」と判定したときに待つ上限(秒)
MAX_ENDPOINTING_DELAY = float(os.environ.get("VOICE_RESUME_MAX_ENDPOINTING_DELAY", "6.0"))
# 割り込みと見なす相手の発話の長さ(秒)。エコー(スピーカー音の回り込み)の誤割り込み対策で長め
MIN_INTERRUPTION_DURATION = float(os.environ.get("VOICE_RESUME_MIN_INTERRUPTION_DURATION", "0.8"))
# 誤割り込み(割り込んだのに発話が続かない)から再開するまでの秒数
FALSE_INTERRUPTION_TIMEOUT = float(os.environ.get("VOICE_RESUME_FALSE_INTERRUPTION_TIMEOUT", "1.5"))

# --- トーク・出力先 ---
# トークのシミュレーター(line_runtime.server)。面談の結果(求人・下書き)をトークへ戻す先
TALK_URL = os.environ.get("VOICE_RESUME_TALK_URL", "http://127.0.0.1:8030")

# 面談ごとの下書き・文字起こし・求人提案(個人の話を含むので data/ の下。git には入らない)
OUT_DIR = Path(os.environ.get("VOICE_RESUME_OUT_DIR", ROOT / "data" / "voice-resume"))
# scripts/voice/ の評価・E2E の成果物(残して比べるもの)
RESULTS_DIR = ROOT / "voice_resume" / "results"

LIVEKIT_ROOM_PREFIX = "voice-resume"

# --- ワーカーの同時ジョブ数とメモリ(agent.worker_options)---
# 部屋ごとに、ジョブのプロセスと dip MCP の子プロセスが 1 つずつできる。Web プロセスと同じマシンを圧迫しないように、
# 同時に受けるジョブを MAX_JOBS までにする(dev で起動しても満員を報告する)。1 人が開ける部屋は Web 側で同時に 1 つまで
MAX_JOBS = int(os.environ.get("VOICE_RESUME_MAX_JOBS", "4"))
# ジョブのプロセスのメモリの上限(MB)。超えたらそのジョブを止める(0 で上限なし。警告は livekit-agents の既定の 1000MB)
JOB_MEMORY_LIMIT_MB = float(os.environ.get("VOICE_RESUME_JOB_MEMORY_LIMIT_MB", "2000"))


def load_env() -> None:
    """リポジトリ直下の .env を os.environ へ(既存値は上書きしない)。"""
    if not ENV_FILE.is_file():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def dip_server_command() -> list[str]:
    """dip 側 MCP サーバー(stdio)の起動コマンド。同じリポジトリ・同じ venv の python で動かす。"""
    return [sys.executable, "-m", "dip_mcp.server"]
