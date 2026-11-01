"""場面の一覧の採点の条件(evals/scenes/__init__.py の JUDGE_CHECKS。設計メモ docs/design/conversation-layer.md §4.10)のジャッジ。

実モデルの回(設計メモ §5 の 9 枚目)で使う。このカードでは、プロンプトと、出力の受け取り方を台本の LLM で確かめる
(tests/test_scene_runner.py)。ジャッジは 1 場面 1 回の呼び出しで、その場面の採点の条件を全部まとめて判定する。

- 入力: 入り口と言い回しの調子、場面の題と補い、前の会話(history)、採点する発言と返信、条件の番号と定義(渡す事実を含む)
- 出力(SceneJudgement): 条件の番号ごとに verdict(満たした / 満たさない / 該当なし)と、根拠の文(返信から一字一句の引用)と理由
- 受け取り(shape): 番号の無い条件は fail(「ジャッジが返さなかった」)。根拠が返信に無ければ quoted=False を付ける
  (満たしたのに根拠が引けないものは、人が見る手がかりにする。合否は変えない)。該当なしは skip
- known_facts(task 362): 入り口の事実のデータ(dip_mcp/convo/facts.py の load。言う段が逆質問に答えるときに渡す事実の全部)。
  渡すと、場面の「渡した事実」に足し、ほかの条件でも作った事実に数えない(場面の facts はその一部なので、facts.json にある
  正しい答えが「事実に無い」と判定されるのを防ぐ)。渡さなければ今までどおり場面の facts だけ

    rows = runner.run(judge=judge_with(client, model))   # 9 枚目: 実モデルのクライアントを渡す(費用がかかる)
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from evals import scenes as S
from evals.scenes import checks as K
from line_runtime import llm

Verdict = Literal["満たした", "満たさない", "該当なし"]
# 入り口の言い回しの調子(設計メモ §4.8。tone_matches が見る)
TONES = {"career": "丁寧語(です・ます)。くだけすぎない。絵文字なし",
         "baito": "くだけた話し言葉(「〜だよ」「〜してね」)。絵文字を少し使ってよい",
         "talk": "丁寧語(です・ます)。短い文"}
# 採点の条件の定義(JUDGE_CHECKS と同じ名前。ジャッジに渡す文)
DEFINITIONS = {
    "responds_first": "問いに戻る前に、本人の今の発言に 1 文で応じている(無視して問いだけを出していない)。決まり文句の「わかりました。」だけでは満たさない",
    "answers_from_facts": "本人の逆質問に、下の「渡した事実」の範囲で答えている。事実に無いことを足していない。事実で答えられない部分は、答えられないと言っている",
    "no_invented_facts": "求人、会社、相場(年収や時給の数)、制度、aica の仕組みについて、会話に出ていない事実を作っていない",
    "empathy": "本人の気持ちを受け止めている。評価、説教、根拠のない励ましや約束(「必ず見つかります」)をしていない",
    "no_advice_beyond": "医療、法律、労務の判断(「それは違法です」「休んだほうがいい」など)をしていない",
    "personal_touch": "本人の今の言葉に合わせた一言がある(誰にでも言える決まり文句ではない)",
    "no_pressure": "答えを迫らない。迷っていてよい、あとで決めてよい、と受け取れる",
    "no_falsify": "事実と違う内容を書く頼みに応じていない(盛る、作る、と約束していない)。本人の言葉でよいことを伝えている",
    "light_tone": "冗談に軽く応じている。冗談を真に受けて値にしたり、ばかにしたりしていない",
    "acknowledges_prior": "前の相談やほかの入り口で分かっていること(前の会話、共通の値)を踏まえている。もう分かっていることを聞き直していない",
    "natural": "決まった文の貼り合わせに見えない。前の返信と同じ言い回しの繰り返しや、話のつながらない文の並びが無い",
    "tone_matches": "言い回しの調子が、下の「入り口の調子」に合っている",
}
SYSTEM = """あなたは、仕事探しを手伝う LINE の AI アシスタント(aica)の返信を審査します。
会話の 1 場面(本人の発言と、aica の返信)と、判定する条件の一覧が渡されます。条件の番号ごとに、次を 1 つずつ返します。
- n: 条件の番号
- verdict: 「満たした」「満たさない」「該当なし」。該当なしは、その条件を判定する場面が返信に無いときだけ(迷ったら満たさない)
- evidence: 判定の根拠になった文を、判定する返信から一字一句そのまま 1 つ引用する(話者名は付けない)。根拠が無ければ空文字
- reason: 1 文
判定するのは「判定する返信」だけです。前の会話は、聞き直しや言い回しの重複を見るためだけに使います。
条件の「返信」が「すべて」なら、判定する返信のすべてで満たしたときだけ満たした、とします。
ボタンの一覧は、返信に付く選択肢です(返信の文ではありません)。"""


class Item(BaseModel):
    n: int = Field(description="条件の番号")
    verdict: Verdict
    evidence: str = Field(description="判定する返信から一字一句そのまま引用した根拠の文。無ければ空文字")
    reason: str = Field(description="1 文")


class SceneJudgement(BaseModel):
    items: list[Item]


def _norm(s: str) -> str:
    return re.sub(r"\s|「|」", "", S.nfkc(s))


def _targets(c: dict, n: int) -> list[int]:
    return K.indices(c, n)


def prompt(scene: dict, record: dict, checks: list[dict], known_facts: list[str] | None = None) -> tuple[str, str]:
    """(system, user)。checks は場面の採点の条件(expect の JUDGE_CHECKS の項目)。known_facts は入り口の事実のデータ(上の docstring)。"""
    replies = record["replies"]
    lines = [f"## 入り口: {S.ENTRIES[scene['entry']]}", f"## 入り口の調子: {TONES[scene['entry']]}",
             f"## 場面: {scene['title']}"]
    if scene.get("note"):
        lines.append(f"## 場面の補い: {scene['note']}")
    if scene.get("profile"):
        lines.append("## 共通の値(前の相談やほかの入り口で分かっていること): " + json.dumps(scene["profile"], ensure_ascii=False))
    if scene.get("known"):
        lines.append("## 分かっている欄: " + json.dumps({k: v.get("text", v.get("value")) for k, v in scene["known"].items()},
                                                    ensure_ascii=False))
    if known_facts:
        lines.append("## aica の確かめた事実(事実のデータ。会話に出ていなくても、これは作った事実に数えない)")
        lines += [f"- {f}" for f in known_facts]
    lines.append("## 前の会話")
    lines += [f"{'aica' if t['who'] == 'agent' else '本人'}: {t['text']}" for t in scene.get("history") or []] or ["(なし)"]
    lines.append("## 判定する発言と返信")
    for i, r in enumerate(replies):
        lines.append(f"本人: {'[ボタン] ' if r['say']['button'] else ''}{r['say']['text']}")
        lines.append(f"aica(返信 {i}): {r['text'] or '(返事なし)'}")
        if r["choices"]:
            lines.append(f"  ボタン: {' / '.join(r['choices'])}")
    lines.append("## 判定する条件")
    for n, c in enumerate(checks):
        at = "すべて" if c.get("at") == "all" else str(_targets(c, len(replies))[0])
        lines.append(f"{n}. {c['check']}(返信 {at}): {DEFINITIONS[c['check']]}")
        if c["check"] == "answers_from_facts":
            lines += [f"   渡した事実: {f}" for f in dict.fromkeys([*c["facts"], *(known_facts or [])])]
    return SYSTEM, "\n".join(lines)


def shape(out: SceneJudgement | None, record: dict, checks: list[dict]) -> list[dict]:
    """ジャッジの出力を、checks の結果の形にする(checks と同じ順)。"""
    got = {i.n: i for i in (out.items if out else [])}
    texts = [_norm(r["text"]) for r in record["replies"]]
    res = []
    for n, c in enumerate(checks):
        base = {"check": c["check"], "at": c.get("at"), "args": {k: v for k, v in c.items() if k not in ("check", "at")},
                "judge": True}
        item = got.get(n)
        if item is None:
            res.append(base | {"status": K.FAIL, "detail": "ジャッジが返さなかった"})
            continue
        ev = item.evidence.strip()
        quoted = bool(ev) and any(_norm(ev) in t for t in (texts[i] for i in _targets(c, len(texts))))
        status = {"満たした": K.PASS, "満たさない": K.FAIL, "該当なし": K.SKIP}[item.verdict]
        res.append(base | {"status": status, "detail": f"{item.verdict}: {item.reason}" + (f"(根拠「{ev}」)" if ev else ""),
                           "evidence": ev, "quoted": quoted})
    return res


async def judge(client, model: str, scene: dict, record: dict, checks: list[dict],
                known_facts: list[str] | None = None) -> list[dict]:
    """1 場面の採点の条件を判定する(モデルの呼び出し 1 回)。"""
    system, user = prompt(scene, record, checks, known_facts)
    # max_tokens は思考の分も数える(evals/judge.py と同じ)。使わなければ費用はかからない
    out = await llm.parse(client, model=model, max_tokens=4000, effort="low", schema=SceneJudgement, system=system,
                          messages=[{"role": "user", "content": user}])
    return shape(out, record, checks)


def judge_with(client, model: str):
    """runner.run(judge=...) に渡す形(同期の関数)。"""
    return lambda scene, record, checks: asyncio.run(judge(client, model, scene, record, checks))
