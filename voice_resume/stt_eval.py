"""STT(文字起こし)の聞き取り評価の台本と採点(オフライン。実 API は呼ばない)。

2026-09-26 の実会話で聞き間違いが多かった(「合うと思う」→「アウトも」、
「時給の下限」→「地球の加減」など)。その言い回しとバイト探しの用語
(時給・下限・シフト・品出し・ホール・キッチン)を含む台本を用意し、
「聞き取れてほしい語句が文字起こしに含まれるか」で採点する。

実際に音声を合成して各 STT 候補を比べるのは scripts/eval_stt.py(実 API)。
ここは台本と採点だけなので、テスト(実 API なし)で守れる。
"""

from __future__ import annotations

import re
import unicodedata

# 台本: text = 本人役が話す文、targets = 聞き取れてほしい語句(表記ゆれは variants で許す)
SCRIPT: list[dict] = [
    {
        "text": "こんにちは。アルバイトを探していて、相談したいです。",
        "targets": [["アルバイト", "バイト"]],
    },
    {
        "text": "コンビニの仕事を探しています。レジと品出しの経験があります。",
        "targets": [["コンビニ"], ["品出し"]],
    },
    {
        "text": "ファミレスではキッチンとホールの両方をやっていました。",
        "targets": [["ファミレス"], ["キッチン"], ["ホール"]],
    },
    {
        "text": "シフトは平日の夕方に、週3日くらい入りたいです。",
        "targets": [["シフト"], ["週3日", "週三日"]],
    },
    {
        "text": "時給は1200円くらいだと嬉しいです。",
        "targets": [["時給"], ["1200円", "1,200円", "千二百円"]],
    },
    {
        "text": "うん、その仕事は自分に合うと思う。",
        "targets": [["合うと思う", "合うとおもう", "あうと思う"]],
    },
    {
        "text": "まあいっか。一旦、時給の下限を外して探してもらえますか。",
        "targets": [["時給の下限", "時給下限"], ["外して", "はずして"]],
    },
    {
        "text": "あれ、全然出ないか。じゃあ、条件をゆるめてください。",
        "targets": [["全然出ない", "全然でない"]],
    },
]

# 2026-09-26 の実会話で Deepgram nova-2(ja)が実際に返した聞き間違い(行番号は会話ログ)。
# テストで「採点がこの間違いを検出できる」ことを確かめるための記録
MISHEARD_2026_09_26: dict[int, str] = {
    5: "アウトも",  # 25行目: 本当は「合うと思う」
    6: "まあいっか 一旦、地球の 加減を外して",  # 41行目: 本当は「時給の下限を外して」
    7: "あれ?全然気が出ないか。",  # 43行目: 本当は「全然出ないか」
}

_PUNCT = re.compile(r"[\s。、．，,.!?！？?・「」『』()（）:：;；…〜~-]")


def normalize(text: str) -> str:
    """採点用の正規化。全角/半角をそろえ(NFKC)、空白と句読点・記号を落とす。"""
    return _PUNCT.sub("", unicodedata.normalize("NFKC", text))


def score_line(got: str, targets: list[list[str]]) -> list[bool]:
    """1行ぶんの採点。target ごとに、どれかの表記が文字起こしに含まれれば正解。"""
    got_norm = normalize(got)
    return [any(normalize(v) in got_norm for v in variants) for variants in targets]


def score_transcripts(got_lines: list[str]) -> dict:
    """台本全行の採点。

    Returns:
        {"hits": 正解した語句数, "total": 語句の総数, "misses": [(行番号, 語句), ...],
         "per_line": [[bool, ...], ...]}
    """
    if len(got_lines) != len(SCRIPT):
        raise ValueError(f"行数が台本と合いません: {len(got_lines)} != {len(SCRIPT)}")
    per_line: list[list[bool]] = []
    misses: list[tuple[int, str]] = []
    for i, (item, got) in enumerate(zip(SCRIPT, got_lines)):
        hits = score_line(got, item["targets"])
        per_line.append(hits)
        for variants, hit in zip(item["targets"], hits):
            if not hit:
                misses.append((i, variants[0]))
    total = sum(len(item["targets"]) for item in SCRIPT)
    return {
        "hits": sum(sum(h) for h in per_line),
        "total": total,
        "misses": misses,
        "per_line": per_line,
    }
