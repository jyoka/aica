"""LLM ジャッジ: 相談の深さ(目的を聞いたか・掘り下げたか・選択肢が本人由来か・提案理由が本人の話とつながるか)と、
トーン・設計の狙いへの合い方を、根拠の発言つきで判定する。作り話(事実性)は evals/checks.py がコードで見る。

ジャッジは安いモデル(evals/run.py の JUDGE_MODEL)。構造化出力で受け取り、根拠の発言が会話ログに本当にあるか
(quoted)をコードで確かめる(ジャッジが発言を作っていないかを、人が照合するときの手がかりにする)。
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from line_runtime import llm

Verdict = Literal["満たした", "満たさない", "該当なし"]
DEPTH_ITEMS = {  # 項目 -> 評価の指標名
    "asked_purpose": "depth_purpose",
    "dug_deeper": "depth_dig",
    "options_personal": "depth_options",
    "reasons_linked": "depth_reasons",
}
DEPTH_LABELS = {"asked_purpose": "目的を聞いたか", "dug_deeper": "目的かやりたいことを掘り下げたか",
                "options_personal": "選択肢が本人の履歴書・発言から", "reasons_linked": "提案理由が本人の話とつながる"}


class DepthItem(BaseModel):
    verdict: Verdict = Field(description="満たした / 満たさない / 該当なし(その場面が会話に無い)")
    evidence: str = Field(description="判定の根拠になった発言を会話ログからそのまま1つ引用する。該当なしなら空文字")


class Judgement(BaseModel):
    asked_purpose: DepthItem
    dug_deeper: DepthItem
    options_personal: DepthItem
    reasons_linked: DepthItem
    tone: int = Field(description="1〜5")
    design_fit: int = Field(description="1〜5")
    comment: str = Field(description="1〜2文")


SYSTEM = """あなたは仕事探し(アルバイト・パートから正社員まで)を手伝う LINE の AI アシスタントの会話を審査します。
会話ログ(と、あれば本人の履歴書の読み取り結果)だけを根拠に判定します。

相談の深さ(4項目。それぞれ verdict と evidence):
- asked_purpose: 本人がなぜ今仕事を探すのか(目的: 副業・生活費・経験やスキルを活かす・転職の理由 など)を、アシスタントが聞いたか。本人が自分から目的を話し、アシスタントがそれを受け止めて確かめた場合(「〜したいのですね」など)も満たす。その場合 evidence には、目的を受け止めたアシスタントの発言を引用する(目的と関係のない前の発言を引かない)。
- dug_deeper: 目的かやりたいことについて、本人の答えを受けて「それはなぜですか」「具体的にはどんな場面ですか」のように、1回以上掘り下げたか。答えを受けて別の項目(時間帯・エリアなど)へ移るだけの問いは掘り下げではない。
- options_personal: アシスタントが出した選択肢(クイックリプライや、職種・方向の候補)が、本人の履歴書・発言から作られているか。誰にでも出す一般論の候補(職種の一覧をそのまま並べる など)なら満たさない。選択肢が「求人を探す / 応募履歴を確認 / 今はいい」のようなメニューや、はい・いいえだけなら該当なし。
- reasons_linked: 求人や方向を提案した理由(求人カードの理由・吹き出しの説明)が、本人の話(目的・経験・希望・不安)とつながっているか。条件の一致(エリア・時間帯)だけでなく、本人の目的ややりたいことに触れていれば満たす。提案が無ければ該当なし。
verdict は「満たした」「満たさない」「該当なし」。evidence には判定の根拠になった発言(アシスタントかユーザー)を、会話ログから一字一句そのまま1つ引用する(「アシスタント:」などの話者名は付けない)。該当なしなら空文字。

ほか:
- tone(1〜5): 敬体・短文・絵文字なし・押しつけがましくないか。
- design_fit(1〜5): 先に理解し1問だけ聞き、拒否を即尊重し、本人の話に沿って相談・提案・支援・待つ に進んでいるか。相談中は1問ずつのヒアリングを続けてよく、「求人を見せて」で即提案に切り替えるのが狙い。「考えます」「迷っている」「まだ考え中」は断りではない。そのあとに、どこで迷っているかを1問だけ聞くのは正しい振る舞いで、減点しない。待つ(wait)のは「今はいい」が選ばれた後。
- comment: 1〜2文。"""


def _log(transcript: list[dict]) -> str:
    return "\n".join(f"{'アシスタント' if t['who'] == 'agent' else 'ユーザー'}: {t['text']}" for t in transcript)


def _norm(s: str) -> str:
    return re.sub(r"\s|「|」|『|』", "", s)


async def judge(client, model: str, transcript: list[dict], resume: dict | None = None) -> dict:
    """深さの4項目(verdict・evidence・quoted)と tone・design_fit・comment を返す。"""
    user = f"## 会話ログ\n{_log(transcript)}"
    if resume:
        user += f"\n\n## 本人の履歴書の読み取り結果\n{json.dumps(resume, ensure_ascii=False)}"
    j = await llm.parse(client, model=model, max_tokens=3000, effort="low", schema=Judgement, system=SYSTEM,
                        messages=[{"role": "user", "content": user}])
    if j is None:
        raise RuntimeError("ジャッジが判定を返さなかった")
    return shape(j, transcript)


def shape(j: Judgement, transcript: list[dict]) -> dict:
    """ジャッジの出力を結果の形に。evidence が会話ログに一字一句あるか(quoted)を足す。"""
    log = _norm(_log(transcript))
    depth = {}
    for k in DEPTH_ITEMS:
        item = getattr(j, k)
        ev = item.evidence.strip()
        depth[k] = {"verdict": item.verdict, "evidence": ev, "quoted": bool(ev) and _norm(ev) in log}
    return {"depth": depth, "tone": j.tone, "design_fit": j.design_fit, "comment": j.comment}


def depth_metrics(result: dict | None) -> dict:
    """深さの項目を指標に(満たした True / 満たさない False / 該当なし・ジャッジなし None。evalkit は割合として集計する)。"""
    depth = (result or {}).get("depth") or {}
    value = {"満たした": True, "満たさない": False}
    return {m: value.get((depth.get(k) or {}).get("verdict")) for k, m in DEPTH_ITEMS.items()}
