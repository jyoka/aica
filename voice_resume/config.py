"""voice-resume の設定。モデル名・ボイス・ターン判定・出力先(既定は書き出さない)・.env 読み込み。

構成(タスク 31 で Realtime から作り直し):
- STT: Deepgram nova-3(日本語)+ バイト用語の keyterm 登録。タスク 37 の比較
  (results/stt-eval-*.md)で「時給の下限」「合うと思う」の聞き間違いが直り、
  遅延も一番短かった。nova-2 に戻すなら VOICE_RESUME_STT_MODEL=nova-2、
  OpenAI にするなら VOICE_RESUME_STT_PROVIDER=openai(モデルは gpt-4o-transcribe など)
- LLM: Anthropic Claude Sonnet 5.5(会話・下書き・調べものとも。task 236。料金は前の gpt-6-sol と同じ
  入力 $2 / 出力 $10 per 1M)。VOICE_RESUME_PROVIDER=openai(未指定なら LINE_SIM_PROVIDER)で前の gpt-6-sol に戻る
- TTS: Cartesia sonic-3.5 の日本語ボイス。速さは標準より少し遅い "slow" が既定
- ターン判定: Silero VAD + LiveKit turn detector(多言語)。終わり判定は少し待つ
- 1 回の相談の上限: 長さ 10 分、web の調べもの 5 回

いずれも環境変数で差し替えられる(VOICE_RESUME_*)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # リポジトリ直下
# LIVEKIT_* / ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPGRAM_API_KEY / CARTESIA_API_KEY はリポジトリ直下の .env(line_runtime と共通)
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

# --- LLM(会話の脳・下書き生成・調べもの)---
# プロバイダ(anthropic / openai)。未指定ならトークと同じ LINE_SIM_PROVIDER、それも無ければ anthropic(task 236)。
# 作り分けは voice_resume/voice_llm.py(会話)と、drafter / advisor が受け取る client の型(下書き・調べもの)
PROVIDER = os.environ.get("VOICE_RESUME_PROVIDER") or os.environ.get("LINE_SIM_PROVIDER") or "anthropic"
_DEFAULT_MODEL = {"openai": "gpt-6-sol"}.get(PROVIDER, "claude-sonnet-5-5")
LLM_MODEL = os.environ.get("VOICE_RESUME_LLM_MODEL", _DEFAULT_MODEL)
# 思考の深さ(OpenAI の reasoning_effort と同じ名前)。音声会話は低レイテンシ優先なので none を既定にする。
# gpt-6-sol は chat completions でツールを使うとき none が必要。Anthropic では none は思考を切る
# (Claude Sonnet 5.5 は thinking between_tools + effort low、Haiku 5.5 は disabled + low。line_runtime.llm.thinking_kwargs)
LLM_REASONING_EFFORT = os.environ.get("VOICE_RESUME_LLM_REASONING_EFFORT", "none")
# 会話の 1 回の返事の上限トークン(Anthropic は必須。音声の返事は短いが、finalize_interview のまとめの引数も入る)
LLM_MAX_TOKENS = int(os.environ.get("VOICE_RESUME_LLM_MAX_TOKENS", "4096"))
DRAFT_MODEL = os.environ.get("VOICE_RESUME_DRAFT_MODEL", _DEFAULT_MODEL)
# 下書きの思考の深さ(Anthropic だけ。思考は任せて effort を低めにする。節約のため)
DRAFT_EFFORT = os.environ.get("VOICE_RESUME_DRAFT_EFFORT", "low")
# 会話中の調べもの(web_research)。OpenAI は Responses API の web_search、Anthropic は web 検索のサーバーツール
RESEARCH_MODEL = os.environ.get("VOICE_RESUME_RESEARCH_MODEL", _DEFAULT_MODEL)
# Anthropic の 1 回の調べもので web を検索する回数の上限(max_uses)。検索 1 回 $0.01 と、結果の分の入力トークンがかかる
RESEARCH_MAX_USES = int(os.environ.get("VOICE_RESUME_RESEARCH_MAX_USES", "2"))
# 平均時給の参考データの更新(scripts/voice/update_wage_reference.py。年 1 回、手で回す)は OpenAI のまま
# (web 検索と構造化出力を 1 回で使う。Anthropic の web 検索は必ず引用が付き、構造化出力と一緒に使えるかは確かめていない)
WAGE_UPDATE_MODEL = os.environ.get("VOICE_RESUME_WAGE_UPDATE_MODEL", "gpt-6-sol")

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

# --- 1 回の相談の上限(費用の暴走を防ぐ。セキュリティ監査 run-1 の 4 番、review-2026-10 の R6)---
# 相談の長さの上限(分)。時間が来たら締めの返答を 1 回作って閉じる(agent.SessionGuard)。
# 接続トークンの期限(livekit_token.TOKEN_TTL の 5 分)は部屋に入る期限で、相談の長さではない
MAX_SESSION_MINUTES = float(os.environ.get("VOICE_RESUME_MAX_SESSION_MINUTES", "10"))
# 1 回の相談で web を調べる回数の上限(web_research / wage_average / research_company の合計)
MAX_WEB_SEARCHES = int(os.environ.get("VOICE_RESUME_MAX_WEB_SEARCHES", "5"))

# --- トーク・出力先 ---
# トークのシミュレーター(line_runtime.server)。面談の結果(求人・下書き)をトークへ戻す先
TALK_URL = os.environ.get("VOICE_RESUME_TALK_URL", "http://127.0.0.1:8030")

# 面談ごとの下書き・文字起こし・求人提案を OUT_DIR に書き出すか。`1` のときだけ書く(開発用)。
# 既定は書かない: 同じ下書きは dip の状態(voice_results)に入り、文字起こしは下書きを作った後に使わない
# (docs/design/privacy.md の 3.3)
WRITE_FILES = os.environ.get("VOICE_RESUME_WRITE_FILES", "").strip() == "1"
# 書き出す先(個人の話を含むので data/ の下。git には入らない)
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
