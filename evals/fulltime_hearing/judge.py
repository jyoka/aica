"""正社員のヒアリングの評価のジャッジ(設計メモ §6.1、§6.4)。

- label_turn: AI の 1 回の返信に、いらいらのできごと(聞き直し、2 つ聞く、面接口調)と、関門の「誘導」のラベルを付ける。
  数えて離脱を決めるのはコード(actor.Clock)。会話の途中で毎ターン 1 回呼ぶ。
- extract_facts: 形 A(状態を持たない今のヒアリング)の会話の記録から、事実表の項目ごとに取れたことを取り出す。
  事実表の値は見せない(答えに寄せないため)。
- score_facts: 「正しく取れた事実」を決める。数字は同じ値か、区切りに入っていれば正しい。「覚えていない」は事実表も
  「覚えていない」なら正しい。文はジャッジが同じ意味かを 2 回判定して、両方「同じ」のときだけ正しい(same_meaning)。

取れた値(captured)の形は、形 B〜D の終わりの状態(2/5 のタスク)も同じにする:
    {key: {"status": "filled" | "dont_know" | "passed" | "missing", "text": 本人の言葉, "number": 数 | None,
           "range": [下, 上 | None] | None}}
区切りのボタン(「10 人くらい」など)は range で返す。
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from evals.fulltime_hearing.actor import fact_text
from evals.fulltime_hearing.personas import label
from line_runtime import llm

SAME_SAMPLES = 2  # 文の判定の回数(両方「同じ」のときだけ正しい)
STATUSES = ("filled", "dont_know", "passed", "missing")


class TurnLabels(BaseModel):
    reask: bool = Field(description="本人がこの会話でもう答えたこと(ボタンで選んだことを含む)を、また聞いた")
    two_questions: bool = Field(description="1 回の返信で、答えを求める問いを 2 つ以上した")
    interview_tone: bool = Field(description="面接のような、試す・詰める口調")
    leading: bool = Field(description="本人が言っていない強みや作業を、答えの候補として挙げた")
    evidence: str = Field(description="当てはまった項目の根拠の文を、最後の AI の返信からそのまま 1 つ引用する。無ければ空文字")


LABEL_SYSTEM = """あなたは、正社員の仕事探しのチャットで、AI の返信を審査します。会話ログの最後の「AI」の返信だけを判定します(前の返信は、聞き直しかを見るためだけに使います)。
- reask: 本人がこの会話でもう答えたこと(ボタンで選んだことを含む)を、また聞いた。答えが「特にない」「パス」「覚えていない」だったことを、もう一度聞くのも含む。最後のまとめで、聞いた内容を並べて「直すところはありますか」と一度に確かめるのは含めない。
- two_questions: 1 回の返信で、答えを求める問いを 2 つ以上した。「A と B を教えてください」も 2 つ。答えの候補のボタンは問いに数えない。
- interview_tone: 面接のような口調。「あなたの強みは何ですか」「志望動機をお聞かせください」「具体的に説明してください」のような、本人を試す・詰める聞き方。
- leading: 本人が言っていない強みや作業を、答えの候補として挙げた(例:「リーダーシップを発揮した、などはありますか」「売上を伸ばした、などのボタン」)。勤務地・年収・時期・規模の区切り・「パス」「覚えていない」のボタンは誘導ではない。
evidence には、当てはまった項目の根拠の文を、最後の AI の返信から一字一句そのまま 1 つ引用します。当てはまらなければ空文字。"""


def _log(transcript: list[dict]) -> str:
    return "\n".join(f"{'AI' if t['who'] == 'agent' else '本人'}: {t['text']}" for t in transcript)


async def label_turn(client, model: str, transcript: list[dict]) -> dict:
    """最後の AI の返信のラベル({reask, two_questions, interview_tone, leading, evidence})。"""
    j = await llm.parse(client, model=model, max_tokens=4000, effort="low", schema=TurnLabels, system=LABEL_SYSTEM,
                        messages=[{"role": "user", "content": f"## 会話ログ\n{_log(transcript)}"}])
    if j is None:
        raise RuntimeError("ジャッジがラベルを返さなかった")
    return j.model_dump()


# ----------------------------- 形 A: 会話の記録から取り出す -----------------------------


class Extracted(BaseModel):
    key: str
    status: Literal["取れた", "覚えていない", "パス", "取れていない"] = Field(
        description="取れた(本人が答えた)/ 覚えていない(本人がそう答えた)/ パス(本人が答えるのを断った)/ 取れていない(聞いていない・答えていない)")
    text: str = Field(description="本人の言葉(ボタンならそのラベル)を、会話ログからそのまま。取れていなければ空文字")
    number: float | None = Field(description="数の項目で、本人が数を言ったときだけ、単位をそろえた数。ほかは null")
    range_low: float | None = Field(description="数の項目で、本人が区切り(「20 人以上」など)を選んだときの下の値。ほかは null")
    range_high: float | None = Field(description="区切りの上の値(「以上」のように上が無ければ null)")


class Extraction(BaseModel):
    items: list[Extracted]


EXTRACT_SYSTEM = """あなたは、正社員の仕事探しのチャットの会話ログから、本人について分かった事実を項目ごとに取り出します。
- 本人の発言(打った文と、押したボタンのラベル)と、本人が先に送った履歴書の読み取り結果(あれば)にあることだけを取ります。AI が言っただけで、本人が認めていないことは取りません。
- 職歴1 は直近の職歴、職歴2 はその前の職歴です(本人がそう話した順で決めます)。
- 数の項目は、指定の単位にそろえた数を number に入れます。本人が区切りを選んだときは range_low と range_high に入れます。
- 会話に出ていない項目は「取れていない」にします。推し量って埋めません。"""


def _numeric(f: dict) -> bool:
    return f["kind"] == "number" or f["key"].endswith(".scale") or f["key"] in ("min_annual_salary", "blank_period")


async def extract_facts(client, model: str, transcript: list[dict], persona: dict, resume: dict | None = None) -> dict:
    """形 A の会話の記録から、事実表の項目ごとの取れた値(captured の形)。本人が先に送った履歴書(resume。読み取り結果)
    があれば、それも本人の言葉として取り出してよい(形 A も dip の状態に持っていて、形 B〜D は high として入れるため)。"""
    items = []
    for f in persona["facts"]:
        unit = f"(数の項目。単位: {f['unit']})" if f["kind"] == "number" else "(数の項目)" if _numeric(f) else ""
        items.append(f"- {f['key']}: {label(f['key'])}{unit}")
    user = "## 項目\n" + "\n".join(items) + f"\n\n## 会話ログ\n{_log(transcript)}"
    if resume:
        user += f"\n\n## 本人が先に送った履歴書の読み取り結果\n{json.dumps(resume, ensure_ascii=False)}"
    j = await llm.parse(client, model=model, max_tokens=8000, effort="low", schema=Extraction, system=EXTRACT_SYSTEM,
                        messages=[{"role": "user", "content": user}])
    if j is None:
        raise RuntimeError("ジャッジが事実を取り出さなかった")
    status = {"取れた": "filled", "覚えていない": "dont_know", "パス": "passed", "取れていない": "missing"}
    got = {x.key: x for x in j.items}
    out = {}
    for f in persona["facts"]:
        x = got.get(f["key"])
        if x is None:
            out[f["key"]] = {"status": "missing", "text": "", "number": None, "range": None}
            continue
        rng = [x.range_low, x.range_high] if x.range_low is not None or x.range_high is not None else None
        out[f["key"]] = {"status": status[x.status], "text": x.text, "number": x.number, "range": rng}
    return out


# ----------------------------- 正しく取れた事実 -----------------------------


class Same(BaseModel):
    key: str
    same: bool = Field(description="同じ事実を指すなら true")
    reason: str = Field(description="1 文")


class Sameness(BaseModel):
    items: list[Same]


SAME_SYSTEM = """あなたは、事実表の文と、会話で取れた文が、同じ事実を指すかを判定します。
- 言い方の違い、短さはかまいません。取れた文が事実表の文と矛盾せず、その項目の中心を含んでいれば「同じ」(same: true)です。
- 中心: なぜ今は理由、勤務地は場所、時期は時期、何をしていたかは勤め先か役割、どうなったかは結果か評価、ほかは項目の中身そのもの。
- 矛盾する、別のことを言っている、中心が抜けている、事実表に無い中身が足されているなら「違う」(same: false)です。"""


def numbers_in_text(text: str) -> set[float]:
    t = text.translate(str.maketrans("０１２３４５６７８９．", "0123456789.")).replace(",", "")
    return {float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)}


def code_verdict(fact: dict, got: dict | None) -> bool | None:
    """コードで決まる判定。文の比べが要る(ジャッジに聞く)ときは None。"""
    if not got or got["status"] in ("missing", "passed"):
        return False
    if fact["kind"] == "dont_know":
        return got["status"] == "dont_know"
    if got["status"] == "dont_know":
        return False
    if fact["kind"] == "number":
        if got.get("number") is not None:
            return float(got["number"]) == float(fact["value"])
        if not got.get("range") and (said := numbers_in_text(got.get("text") or "")):
            # 文の中の数のどれかが同じなら正しい(「1か月に40件くらい」の 1 と 40)
            return float(fact["value"]) in said
        if not got.get("range"):  # 「こだわらない」など、数も区切りも無い
            return False
        lo, hi = got["range"]
        return (lo is None or fact["value"] >= lo) and (hi is None or fact["value"] <= hi)
    return None


async def same_meaning(client, model: str, pairs: list[tuple[dict, str]], samples: int = SAME_SAMPLES) -> dict:
    """文の項目を samples 回判定し、{key: [回ごとの same]}。1 回に全部の項目をまとめて聞く。"""
    if not pairs:
        return {}
    user = "\n\n".join(f"### {f['key']}({label(f['key'])})\n事実表: {fact_text(f)}\n取れた文: {text}" for f, text in pairs)
    votes: dict[str, list[bool]] = {f["key"]: [] for f, _ in pairs}
    for _ in range(max(1, samples)):
        j = await llm.parse(client, model=model, max_tokens=4000, effort="low", schema=Sameness, system=SAME_SYSTEM,
                            messages=[{"role": "user", "content": user}])
        if j is None:
            raise RuntimeError("ジャッジが文の判定を返さなかった")
        got = {x.key: x.same for x in j.items}
        for k, vs in votes.items():
            vs.append(bool(got.get(k, False)))  # 判定が抜けた項目は「違う」に数える
    return votes


async def score_facts(client, model: str, persona: dict, captured: dict, samples: int = SAME_SAMPLES) -> list[dict]:
    """事実表の項目ごとに {key, label, fact, got, correct, how, votes}。"""
    rows, pairs = [], []
    for f in persona["facts"]:
        got = captured.get(f["key"])
        v = code_verdict(f, got)
        rows.append({"key": f["key"], "label": label(f["key"]), "fact": fact_text(f), "got": got, "correct": v,
                     "how": "code" if v is not None else "judge", "votes": None})
        if v is None:
            pairs.append((f, got.get("text") or ""))
    votes = await same_meaning(client, model, pairs, samples)
    for r in rows:
        if r["key"] in votes:
            r["votes"] = votes[r["key"]]
            r["correct"] = len(r["votes"]) > 0 and all(r["votes"])
    return rows
