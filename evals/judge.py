"""LLM ジャッジ: 相談の深さ(目的を聞いたか・掘り下げたか・選択肢が本人由来か・提案理由が本人の話とつながるか)と、
トーン・設計の狙いへの合い方を、根拠の発言つきで判定する。作り話(事実性)は evals/checks.py がコードで見る。

ジャッジは安いモデル(evals/run.py の JUDGE_MODEL)。構造化出力で受け取り、根拠の発言が会話ログに本当にあるか
(quoted)をコードで確かめる(ジャッジが発言を作っていないかを、人が照合するときの手がかりにする)。
asked_purpose を「満たした」とした根拠が、目的を聞いた・受け止めた発言か(purpose_evidence)もコードで確かめ、
そうでなければ指標では「満たさない」に数える(目的と関係のない「正社員のお仕事で考えますね」を根拠に満たした例があった)。

ジャッジの点数は1回ごとに揺れる(tone・design_fit は ±1 ほど)ので、1つの会話を samples 回(既定2回)判定してまとめる:
tone・design_fit は平均、深さの項目は、どの回も「満たした」(該当なしを除く)なら満たした、1回でも「満たさない」なら
満たさない、すべて該当なしなら該当なし。回ごとの判定は samples に残す。
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from dip_mcp.logic import AREAS
from evals.checks import JOB_TYPE_ALIASES, PURPOSE_QUESTION
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


# 本人の目的を受け止めた言い方(「家計のためですね」「〜したいのですね」「〜ということですね」)
PURPOSE_ACK = re.compile(r"ですね|ということ|とのこと")
# 受け止めた内容が目的の言い方(「〜のため」「〜したい」「〜を考えて」「〜を目指して」)。「扶養内ですね」
# 「残業が少ないところですね」「正社員に切り替えるということですね」のような、条件や区分の受け止めには付かない
PURPOSE_WORDING = re.compile(r"ため|たい|たく|ほしい|欲しい|考え|目指")
# 受け止めの根拠にしない語: 区分の切り替えの一言(「正社員のお仕事で考えますね」)の語、探す・働くこと自体の語、
# 条件(職種・エリア・時間・日数・給与)の語。「カフェで働きたいのですね」「吉祥寺あたりですね」は目的の受け止めではない
_NOT_PURPOSE = re.compile("|".join(map(re.escape, sorted(
    {"正社員", "アルバイト", "バイト", "パート", "お仕事", "仕事", "求人", "探し", "探す", "働き", "働く", "働け", "働い",
     "考え", "希望", "条件", "エリア", "あたり", "時間", "平日", "土日", "週末", "曜日", "日中", "夕方", "午前", "午後",
     "深夜", "年収", "月給", "時給", "リモート", "シフト", "勤務", "週", *AREAS, *JOB_TYPE_ALIASES}, key=len, reverse=True)))
    + r"|[0-9０-９〜~]+(時|日|分|万円|万|円|歳|年|か月)?")
# 受け止めと本人の発言が共有する2文字(漢字かカタカナを1字以上含む。「お金」「貯め」のような、ひらがな混じりの語幹も見る。
# 助詞を含む2文字「で働」「内で」は語ではないので数えない)
_UNIT = re.compile(r"(?=([ぁ-んァ-ヴー一-龥々]{2}))")
_KANA_KANJI = re.compile(r"[ァ-ヴ一-龥々]")
_PARTICLE = re.compile(r"[をのでにがはとへだな]")
# 会話ログの吹き出しの後ろに付く、選択肢・操作・カードの表示
_ATTACHED = re.compile(r"\s*\[(選択肢|操作|求人カード)\]")
JUDGE_SAMPLES = 2


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[。!！?？\n])", text)


def _asks_purpose(text: str) -> bool:
    """アシスタントの吹き出しに、目的だけを聞く問い(PURPOSE_QUESTION)がある。問いは疑問符のある文か「教えてください」の文で、
    後ろに付く選択肢などの表示は見ない。"""
    return any(PURPOSE_QUESTION.search(x) for x in _sentences(_ATTACHED.split(text)[0])
               if "?" in x or "？" in x or "教えて" in x)


def _ack_units(evidence: str) -> set[str]:
    """根拠のうち、目的の言い方(PURPOSE_WORDING)で受け止めた文の、条件などの語を除いた2文字の単位。"""
    out = set()
    for x in _sentences(evidence):
        if PURPOSE_ACK.search(x) and PURPOSE_WORDING.search(x):
            x = _NOT_PURPOSE.sub("|", x)
            out |= {u for u in _UNIT.findall(x) if _KANA_KANJI.search(u) and not _PARTICLE.search(u)}
    return out


def purpose_evidence(evidence: str, transcript: list[dict]) -> bool:
    """asked_purpose の根拠が、目的を聞いた・受け止めた発言か。次のどれかなら True:
    - 根拠が目的を聞く問い(PURPOSE_QUESTION)。
    - 根拠のアシスタントの発言より前に、目的を聞く問いがあり、本人が答えている(「どんな変化からですか?」→「家計のため」
      →「家計のためですね。…」。根拠が受け止めの一言でも、目的は聞いている)。
    - 根拠が目的の言い方の受け止め(PURPOSE_ACK と PURPOSE_WORDING)で、本人がそれより前に話したことと2文字の単位を
      共有する(条件・区分の語を除く)。"""
    ev = _norm(evidence)
    if not ev:
        return False
    if PURPOSE_QUESTION.search(ev):
        return True
    at = next((i for i, t in enumerate(transcript) if t["who"] == "agent" and ev in _norm(t["text"])), None)
    if at is None:
        return False
    for i, t in enumerate(transcript[:at]):
        if t["who"] == "agent" and _asks_purpose(t["text"]) and any(u["who"] == "user" for u in transcript[i + 1:at]):
            return True
    said = _norm("".join(t["text"] for t in transcript[:at] if t["who"] == "user"))
    return any(u in said for u in _ack_units(ev))


def _log(transcript: list[dict]) -> str:
    return "\n".join(f"{'アシスタント' if t['who'] == 'agent' else 'ユーザー'}: {t['text']}" for t in transcript)


def _norm(s: str) -> str:
    return re.sub(r"\s|「|」|『|』", "", s)


async def judge(client, model: str, transcript: list[dict], resume: dict | None = None,
                samples: int = JUDGE_SAMPLES) -> dict:
    """深さの4項目(verdict・evidence・quoted)と tone・design_fit・comment を返す。samples 回判定してまとめる(merge)。"""
    user = f"## 会話ログ\n{_log(transcript)}"
    if resume:
        user += f"\n\n## 本人の履歴書の読み取り結果\n{json.dumps(resume, ensure_ascii=False)}"
    shaped = []
    for _ in range(max(1, samples)):
        j = await llm.parse(client, model=model, max_tokens=3000, effort="low", schema=Judgement, system=SYSTEM,
                            messages=[{"role": "user", "content": user}])
        if j is None:
            raise RuntimeError("ジャッジが判定を返さなかった")
        shaped.append(shape(j, transcript))
    return merge(shaped)


def shape(j: Judgement, transcript: list[dict]) -> dict:
    """ジャッジの出力を結果の形に。evidence が会話ログに一字一句あるか(quoted)と、asked_purpose の根拠が目的を
    聞いた・受け止めた発言か(purpose_evidence)を足す。"""
    log = _norm(_log(transcript))
    depth = {}
    for k in DEPTH_ITEMS:
        item = getattr(j, k)
        ev = item.evidence.strip()
        depth[k] = {"verdict": item.verdict, "evidence": ev, "quoted": bool(ev) and _norm(ev) in log}
    depth["asked_purpose"]["purpose_evidence"] = purpose_evidence(depth["asked_purpose"]["evidence"], transcript)
    return {"depth": depth, "tone": j.tone, "design_fit": j.design_fit, "comment": j.comment}


def _verdict(item: dict) -> str:
    """指標に数える判定。asked_purpose の「満たした」は、根拠が目的の発言でなければ満たさない。"""
    if item.get("verdict") == "満たした" and item.get("purpose_evidence") is False:
        return "満たさない"
    return item.get("verdict")


def merge(shaped: list[dict]) -> dict:
    """複数回の判定をまとめる。深さはどの回も満たした(該当なしを除く)なら満たした、1回でも満たさないなら満たさない。
    根拠はまとめた判定と同じ判定の回のもの。tone・design_fit は平均。判定が割れた項目は split に出す。"""
    if len(shaped) == 1:
        return shaped[0]
    depth, split = {}, []
    for k in DEPTH_ITEMS:
        items = [s["depth"][k] for s in shaped]
        verdicts = [_verdict(x) for x in items]
        judged = [v for v in verdicts if v != "該当なし"]
        final = "該当なし" if not judged else "満たさない" if "満たさない" in judged else "満たした"
        pick = next(x for x, v in zip(items, verdicts, strict=True) if v == final)
        depth[k] = pick | {"verdict": final}
        if len(set(verdicts)) > 1:
            split.append(k)
    mean = lambda key: round(sum(s[key] for s in shaped) / len(shaped), 2)
    return {"depth": depth, "tone": mean("tone"), "design_fit": mean("design_fit"), "comment": shaped[0]["comment"],
            "split": split, "samples": shaped}


def depth_metrics(result: dict | None) -> dict:
    """深さの項目を指標に(満たした True / 満たさない False / 該当なし・ジャッジなし None。evalkit は割合として集計する)。
    asked_purpose の「満たした」は、根拠が目的を聞いた・受け止めた発言でなければ False。"""
    depth = (result or {}).get("depth") or {}
    value = {"満たした": True, "満たさない": False}
    return {m: value.get(_verdict(depth.get(k) or {})) for k, m in DEPTH_ITEMS.items()}
