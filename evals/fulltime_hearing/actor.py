"""本人役: 短気で、時間がなく、いちばん手早く終えたい(設計メモ §6.1)。

離脱の判定はコードが持つ(Clock)。LLM の本人役は、返信の文か「押すボタン」だけを返す(ActorTurn)。
いらいらのできごとは、ジャッジが付けたラベル(judge.label_turn)と、吹き出しの字数(コード)を Clock が数える。
こうすると、実 API なしのテスト(§6.6 の 1)でも離脱を確かめられる。

- 時間: 読む 400 字/分、打つ 40 字/分、ボタン 1 回 2 秒。読む字は吹き出し、ボタンのラベル、Flex の中の字(空白は数えない)。
  5 分を超えたら離脱する。
- いらいら: 聞き直し / 1 回に 2 つ聞く / 面接口調 / 吹き出しが 80 字を超えた(吹き出しごと)が 3 回たまったら離脱する。
- 疲れ: 3 分を超えたら、ボタンがあればボタン、無ければ短い言葉 1 つ(本人役への指示。守ったかは記録だけ)。
- 事実表からだけ答える。ゲームの仕組みに点を足す決まりは入れない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from evals.fulltime_hearing.personas import label
from line_runtime import llm

READ_CHARS_PER_MIN = 400
TYPE_CHARS_PER_MIN = 40
BUTTON_SECONDS = 2.0
TIME_LIMIT_SECONDS = 300.0   # これを超えたら離脱
TIRED_AFTER_SECONDS = 180.0  # これを超えたら答えが短くなる
IRRITATION_LIMIT = 3         # いらいらのできごとがこれだけたまったら離脱
BUBBLE_MAX = 80              # 吹き出しの字数の上限(超えたら、いらいらのできごと 1 回)
SHORT_ANSWER = 10            # 疲れたときの「短い言葉 1 つ」の目安の字数
LEAVE_TEXT = "もういいです"
IRRITATIONS = {"reask": "聞き直し", "two_questions": "1 回に 2 つ聞いた", "interview_tone": "面接口調",
               "long_bubble": "吹き出しが 80 字を超えた"}


@dataclass
class AIReply:
    """AI の 1 回の返信を、本人役が受け取る形にしたもの。
    buttons は押せるもの(クイックリプライ)。flex は Flex の中の字(カード、まとめ、URL のボタンのラベルなど。読むだけ)。
    done は result に着いた(形 A は要約か求人の提案に着いた)こと。closed は、result に着かずに会話が閉じたときの離脱の理由
    ("ai_closed": AI が閉じた、"declined": 本人の断りで閉じた)。閉じていなければ空。"""
    bubbles: list[str]
    buttons: list[str] = field(default_factory=list)
    flex: list[str] = field(default_factory=list)
    done: bool = False
    closed: str = ""


def chars(s: str) -> int:
    return len(re.sub(r"\s", "", s))


def read_chars(r: AIReply) -> int:
    return sum(chars(x) for x in [*r.bubbles, *r.buttons, *r.flex])


def render(r: AIReply) -> str:
    """本人役とジャッジに見せる 1 回の返信(吹き出しの文 + ボタンのラベル + Flex の中の字)。"""
    parts = list(r.bubbles)
    parts += [f"[カード] {x}" for x in r.flex]
    if r.buttons:
        parts.append("[ボタン] " + " / ".join(r.buttons))
    return "\n".join(parts)


@dataclass
class Clock:
    """本人の推定時間と、いらいらのできごとを数え、離脱を決める。"""
    seconds: float = 0.0
    typed_chars: int = 0
    presses: int = 0
    typed_replies: int = 0
    irritations: list[dict] = field(default_factory=list)  # {"turn", "kind", "evidence"}
    left: str | None = None  # "time" / "irritation" / "turn_cap" / "no_reply" / "ai_closed" / "declined"

    def read(self, r: AIReply) -> float:
        s = read_chars(r) * 60 / READ_CHARS_PER_MIN
        self.seconds += s
        return s

    def press(self) -> float:
        self.presses += 1
        self.seconds += BUTTON_SECONDS
        return BUTTON_SECONDS

    def type(self, text: str) -> float:
        n = chars(text)
        self.typed_chars += n
        self.typed_replies += 1
        s = n * 60 / TYPE_CHARS_PER_MIN
        self.seconds += s
        return s

    def irritate(self, turn: int, r: AIReply, labels: dict) -> list[dict]:
        """このターンのできごとを足す。吹き出しの字数はコード、ほかはジャッジのラベル({kind: bool, "evidence": str})。"""
        new = [{"turn": turn, "kind": "long_bubble", "evidence": b} for b in r.bubbles if chars(b) > BUBBLE_MAX]
        new += [{"turn": turn, "kind": k, "evidence": labels.get("evidence", "")}
                for k in ("reask", "two_questions", "interview_tone") if labels.get(k)]
        self.irritations += new
        return new

    @property
    def tired(self) -> bool:
        return self.seconds > TIRED_AFTER_SECONDS

    def should_leave(self) -> str | None:
        """離脱するなら理由。いらいらを先に見る(会話の中で言う「もういいです」の理由になるため)。"""
        if len(self.irritations) >= IRRITATION_LIMIT:
            return "irritation"
        if self.seconds > TIME_LIMIT_SECONDS:
            return "time"
        return None


class ActorTurn(BaseModel):
    button: str = Field(description="押すボタンのラベルをそのまま。打つときは空文字")
    text: str = Field(description="打つ文。ボタンを押すときは空文字")


SYSTEM = """あなたは、スマホの LINE で正社員の仕事探しの AI と話している本人です。職務経歴書の材料づくりと条件の相談のために、このトークを開きました。
あなたは短気で、時間がありません。いちばん手早く終わらせたい人です。

答え方:
- 1 回の返信では、ボタンを 1 つ押すか、文を打つかのどちらかだけをします。合うボタンがあれば、打たずにボタンを押します(button にラベルをそのまま入れ、text は空)。
- 打つときは、聞かれたことだけを、話し言葉で短く答えます。聞かれていないことは言いません。
- 答えは、下の「あなたの事実」と「そのほかのこと」からだけ作ります。そこに無いことは「特にない」「わからない」と答え、作りません。数字を変えません。
- 事実が「覚えていない」なら、「覚えていない」と答えます(ボタンにあれば押します)。
- メッセージの最後に「(メモ: 疲れている)」とあるときは、ボタンがあればボタンを押し、無ければ短い言葉 1 つ(10 字くらいまで)で答えます。
- AI の文がわかりにくくても、聞き返さずに、わかる範囲で答えます。

あなたの答え方のくせ:
{style}

あなたの事実:
{facts}

そのほかのこと:
{other}"""


def fact_text(f: dict) -> str:
    if f["kind"] == "dont_know":
        return "覚えていない"
    if f["kind"] == "number":
        return f"{f['value']}{f.get('unit', '')}"
    return f["value"]


def system_prompt(persona: dict) -> str:
    facts = "\n".join(f"- {label(f['key'])}: {fact_text(f)}" for f in persona["facts"])
    other = "\n".join(f"- {k}: {v}" for k, v in persona.get("other", {}).items()) or "- なし"
    return SYSTEM.format(style=persona.get("style", "特になし"), facts=facts, other=other)


def actor_messages(transcript: list[dict], tired: bool) -> list[dict]:
    """本人役から見た会話(AI の返信が user、自分の返信が assistant)。最後の AI の返信に疲れのメモを付ける。"""
    msgs = [{"role": "user" if t["who"] == "agent" else "assistant", "content": t["text"]} for t in transcript]
    if tired and msgs and msgs[-1]["role"] == "user":
        msgs[-1] = msgs[-1] | {"content": msgs[-1]["content"] + "\n(メモ: 疲れている)"}
    return msgs


async def actor_turn(client, model: str, persona: dict, transcript: list[dict], tired: bool) -> ActorTurn:
    """本人役の次の返信。空なら 1 回だけ取り直す。"""
    for _ in range(2):
        t = await llm.parse(client, model=model, max_tokens=1000, schema=ActorTurn, system=system_prompt(persona),
                            messages=actor_messages(transcript, tired))
        if t and (t.button.strip() or t.text.strip()):
            return t
    return ActorTurn(button="", text="")


def resolve(t: ActorTurn, buttons: list[str]) -> tuple[str, bool]:
    """(送る文, ボタンか)。ラベルに無いボタンは、そのラベルを打ったことにする(本人役の崩れとして記録する)。"""
    b = t.button.strip()
    if b and b in buttons:
        return b, True
    return (t.text.strip() or b), False

