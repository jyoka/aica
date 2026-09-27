"""STT(文字起こし)の候補を同じ音声で比べる(実 API。タスク 37)。

2026-09-26 の実会話で聞き間違いが多かった(「合うと思う」→「アウトも」、
「時給の下限」→「地球の加減」)。その言い回しを含む台本(voice_resume/stt_eval.py)を
本人役の TTS(gpt-4o-mini-tts・少し早口)で音声合成し、同じ音声を各候補で
文字起こしして、正解率(聞き取れてほしい語句の一致率)・遅延・費用を比べる:

    ./.venv/bin/python scripts/voice/eval_stt.py

- 候補: Deepgram nova-2(現行)/ nova-2+keywords / nova-3 / nova-3+keyterm /
  OpenAI gpt-4o-mini-transcribe / gpt-4o-transcribe(いずれも日本語指定)
- 音声は「きれい」と「劣化(8kHz 相当まで間引き + ノイズ。実マイクの悪条件に寄せる)」の
  2条件。きれいな合成音声では全候補が満点になり差が出ないため
- 音声は out/stt-eval-audio/ にキャッシュする(再実行で合成し直さない)
- 遅延は録音済み音声の一括 REST(recognize)の実測。streaming の体感遅延そのもの
  ではないが、候補どうしの比較には使える
- 結果は results/stt-eval-<時刻>.md
"""

from __future__ import annotations

import array
import asyncio
import datetime as dt
import hashlib
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # isort: skip

from livekit import rtc
from openai import AsyncOpenAI

from voice_resume import config
from voice_resume.schema import JST
from voice_resume.stt_eval import MISHEARD_2026_09_26, SCRIPT, score_transcripts

RESULTS = config.RESULTS_DIR
AUDIO_CACHE = config.OUT_DIR / "stt-eval-audio"
SAMPLE_RATE = 24000

# バイト探しの用語(config.STT_KEYTERMS の既定と同じ)。nova-3 keyterm / nova-2 keywords /
# OpenAI の prompt に、それぞれの形で渡す
TERMS = list(config.STT_KEYTERMS)
OPENAI_PROMPT = f"アルバイト探しの相談の日本語音声です。{'、'.join(TERMS)}などの言葉が出ます。"

# 単価(2026-09 時点の各社公式 pricing、streaming の USD/分)
# - Deepgram: nova-2 は従来の単価、nova-3 monolingual は正価 $0.0077(セール価 $0.0048)
# - OpenAI: gpt-4o-transcribe $0.006 / gpt-4o-mini-transcribe $0.003
PRICE_PER_MIN = {
    "nova-2": 0.0058,
    "nova-3": 0.0077,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
}
# 本人役の音声合成(gpt-4o-mini-tts)の概算単価。scripts/e2e_audio_session.py と同じ
TTS_TEXT_PER_M, TTS_AUDIO_PER_M = 0.60, 12.00


def candidates() -> list[dict]:
    """比べる候補。build() は呼ぶたびに新しい STT インスタンスを返す。"""
    from livekit.plugins import deepgram
    from livekit.plugins import openai as lk_openai

    return [
        {
            "key": "deepgram nova-2(現行)",
            "model": "nova-2",
            "build": lambda: deepgram.STT(model="nova-2", language="ja"),
        },
        {
            "key": "deepgram nova-2 + keywords",
            "model": "nova-2",
            "build": lambda: deepgram.STT(
                model="nova-2", language="ja", keywords=[(t, 1.5) for t in TERMS]
            ),
        },
        {
            "key": "deepgram nova-3",
            "model": "nova-3",
            "build": lambda: deepgram.STT(model="nova-3", language="ja"),
        },
        {
            "key": "deepgram nova-3 + keyterm",
            "model": "nova-3",
            "build": lambda: deepgram.STT(model="nova-3", language="ja", keyterm=list(TERMS)),
        },
        {
            "key": "openai gpt-4o-mini-transcribe",
            "model": "gpt-4o-mini-transcribe",
            "build": lambda: lk_openai.STT(
                model="gpt-4o-mini-transcribe", language="ja", prompt=OPENAI_PROMPT
            ),
        },
        {
            "key": "openai gpt-4o-transcribe",
            "model": "gpt-4o-transcribe",
            "build": lambda: lk_openai.STT(
                model="gpt-4o-transcribe", language="ja", prompt=OPENAI_PROMPT
            ),
        },
    ]


async def synth_lines(client: AsyncOpenAI) -> tuple[list[bytes], float]:
    """台本の各行を 24kHz/16bit mono PCM に合成(キャッシュあり)。実会話に寄せて少し早口。"""
    AUDIO_CACHE.mkdir(parents=True, exist_ok=True)
    instructions = "自然な日本語の話し言葉で、少し早口に読んでください。"
    pcms: list[bytes] = []
    synth_chars = 0
    for i, item in enumerate(SCRIPT):
        digest = hashlib.sha256(f"gpt-4o-mini-tts|alloy|{instructions}|{item['text']}".encode()).hexdigest()[:12]
        cache = AUDIO_CACHE / f"{i:02d}-{digest}.pcm"
        if cache.is_file():
            pcms.append(cache.read_bytes())
            continue
        resp = await client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=item["text"],
            instructions=instructions,
            response_format="pcm",
        )
        cache.write_bytes(resp.content)
        pcms.append(resp.content)
        synth_chars += len(item["text"])
    audio_secs = sum(len(p) for p in pcms) / (SAMPLE_RATE * 2)
    synth_cost = (
        synth_chars / 4 * TTS_TEXT_PER_M / 1e6
        + (audio_secs if synth_chars else 0) / 60 * 1200 * TTS_AUDIO_PER_M / 1e6
    )
    return pcms, synth_cost


def degrade(pcm: bytes, line_no: int) -> bytes:
    """実マイクの悪条件に寄せた劣化。8kHz 相当まで間引いて戻し(高域が落ちる)、
    ノイズを足す(SNR 約10dB)。乱数は行番号で固定し、再実行しても同じ音になる。"""
    samples = array.array("h", pcm)
    decimated = samples[::3]  # 24kHz → 8kHz 相当(あえてローパスなしで間引く)
    rms = (sum(s * s for s in decimated) / max(len(decimated), 1)) ** 0.5
    noise_amp = rms / (10 ** (10 / 20))  # SNR 10dB
    rng = random.Random(1000 + line_no)
    out = array.array("h")
    for s in decimated:
        v = s + rng.gauss(0, noise_amp)
        v = max(-32768, min(32767, int(v)))
        out.extend((v, v, v))  # 0次ホールドで 24kHz に戻す
    return out.tobytes()


async def run_candidate(cand: dict, conditions: dict[str, list[bytes]]) -> dict:
    """1候補で全条件・全行を文字起こしし、条件ごとの採点と遅延を返す。"""
    stt = cand["build"]()
    runs: dict[str, dict] = {}
    try:
        for cond_name, pcms in conditions.items():
            texts: list[str] = []
            latencies: list[float] = []
            for pcm in pcms:
                frame = rtc.AudioFrame(
                    data=pcm, sample_rate=SAMPLE_RATE, num_channels=1,
                    samples_per_channel=len(pcm) // 2,
                )
                t0 = time.perf_counter()
                event = await stt.recognize(buffer=frame)
                latencies.append(time.perf_counter() - t0)
                texts.append(event.alternatives[0].text.strip() if event.alternatives else "")
            runs[cond_name] = {
                "texts": texts,
                "score": score_transcripts(texts),
                "latencies": latencies,
            }
    except Exception as exc:  # noqa: BLE001  比較なので、候補ごとの失敗は記録して続ける
        return {"cand": cand, "error": f"{type(exc).__name__}: {exc}", "runs": runs}
    finally:
        await stt.aclose()
    return {"cand": cand, "error": None, "runs": runs}


def _phrase_ok(result: dict, cond: str, phrase: str) -> str:
    run = result["runs"].get(cond)
    if run is None:
        return "-"
    return "NG" if any(p == phrase for _, p in run["score"]["misses"]) else "OK"


async def main() -> None:
    config.load_env()
    import os

    for key in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY"):
        if not os.environ.get(key):
            raise SystemExit(f"{key} がありません(.env を確認)")

    from livekit.agents.utils import http_context

    async with http_context.open():
        client = AsyncOpenAI()
        pcms, synth_cost = await synth_lines(client)
        audio_secs = sum(len(p) for p in pcms) / (SAMPLE_RATE * 2)
        conditions = {
            "clean": pcms,
            "degraded": [degrade(p, i) for i, p in enumerate(pcms)],
        }
        print(f"台本 {len(SCRIPT)} 行 / 音声 {audio_secs:.1f}s × 2条件(合成 ${synth_cost:.4f})")

        results = []
        for cand in candidates():
            result = await run_candidate(cand, conditions)
            results.append(result)
            if result["error"]:
                print(f"- {cand['key']}: エラー {result['error']}")
            else:
                parts = [
                    f"{name} {run['score']['hits']}/{run['score']['total']}"
                    for name, run in result["runs"].items()
                ]
                lat = result["runs"]["clean"]["latencies"]
                print(f"- {cand['key']}: {' / '.join(parts)}(平均 {sum(lat) / len(lat):.2f}s)")

    # ---- 結果の markdown ----
    stamp = dt.datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    stt_cost_total = 0.0
    lines = [
        "# STT 候補の比較(タスク 37: 聞き間違いを減らす)",
        "",
        (
            f"- 日時: {stamp} JST / 台本: voice_resume/stt_eval.py({len(SCRIPT)} 行, "
            f"音声 {audio_secs:.1f}s。2026-09-26 の実会話の言い間違いを含む)"
        ),
        "- 本人役の音声は gpt-4o-mini-tts(少し早口)で合成し、全候補に同じ音声を使った",
        (
            "- 条件は2つ: きれいな合成音声(clean)と、実マイクの悪条件に寄せた劣化音声"
            "(degraded: 8kHz 相当まで間引き + ノイズ SNR 10dB)。きれいな音声では全候補が"
            "満点になり差が出ないため、劣化音声で差を見る"
        ),
        (
            "- 正解率 = 聞き取れてほしい語句(時給の下限・合うと思う・品出し など"
            f"{sum(len(i['targets']) for i in SCRIPT)} 語句)が文字起こしに含まれた割合"
        ),
        (
            "- 遅延は録音済み音声の一括 REST(recognize)の1行あたり実測(clean 条件)。"
            "streaming の体感遅延そのものではないが、候補間の比較には使える"
        ),
        "",
        (
            "| 候補 | 正解率(clean) | 正解率(degraded) | 「時給の下限」(degraded) | "
            "「合うと思う」(degraded) | 平均遅延 | 最大遅延 | 単価(USD/分) | この評価の費用 |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        cand = r["cand"]
        price = PRICE_PER_MIN[cand["model"]]
        if r["error"]:
            lines.append(
                f"| {cand['key']} | エラー | - | - | - | - | - | ${price:.4f} | - |"
            )
            continue
        clean, degraded_run = r["runs"]["clean"], r["runs"]["degraded"]
        lat = clean["latencies"]
        cost = audio_secs / 60 * price * len(r["runs"])
        stt_cost_total += cost
        lines.append(
            f"| {cand['key']} | {clean['score']['hits']}/{clean['score']['total']} | "
            f"{degraded_run['score']['hits']}/{degraded_run['score']['total']} | "
            f"{_phrase_ok(r, 'degraded', '時給の下限')} | {_phrase_ok(r, 'degraded', '合うと思う')} | "
            f"{sum(lat) / len(lat):.2f}s | {max(lat):.2f}s | ${price:.4f} | ${cost:.4f} |"
        )
    lines += [
        "",
        (
            f"評価の費用合計: 約 ${synth_cost + stt_cost_total:.4f}"
            f"(音声合成 ${synth_cost:.4f} + STT ${stt_cost_total:.4f})"
        ),
        "",
        "## 参考: 2026-09-26 の実会話での聞き間違い(nova-2, 実マイク)",
        "",
    ]
    lines += [
        f"- 台本 {i + 1} 行目: 「{SCRIPT[i]['text']}」 → 実会話では 「{got}」"
        for i, got in MISHEARD_2026_09_26.items()
    ]
    for r in results:
        lines += ["", f"## {r['cand']['key']}", ""]
        if r["error"]:
            lines.append(f"エラー: {r['error']}")
            continue
        for i, item in enumerate(SCRIPT):
            lines.append(f"- 台本: {item['text']}")
            for cond_name, run in r["runs"].items():
                missed = [p for j, p in run["score"]["misses"] if j == i]
                mark = f" ← 聞き取れず: {', '.join(missed)}" if missed else ""
                lines.append(f"  - {cond_name}: {run['texts'][i] or '(空)'}{mark}")

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"stt-eval-{stamp}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"結果: {out}")


if __name__ == "__main__":
    asyncio.run(main())
