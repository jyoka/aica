"""決める 2: 応じ方の計画(設計メモ docs/design/conversation-layer.md §4.6)。どの入り口でも同じ表。

ハーネスは、読んだ act と値を当てた結果から、返事の部品の並び(計画)を作る。部品は 5 種類で、並ぶ順は決まっている。

    ack(相づち)→ answer(逆質問の答え)→ note(注意の 1 行)→ phase_line(区切り)→ ask(問い)

LLM(言う段)が書くのは ack と answer の言い方だけ。note と phase_line は入り口のデータの文をハーネスがそのまま入れ、
ask は質問バンクの文。計画は、次の問いをどうするか(next)と、そのターンを数えるか(count)も持つ。

| 読んだこと | ack | ほかの部品 | next | count |
|---|---|---|---|---|
| 今の問いへの答え、パス(ほかの act と一緒でも) | short(怒り・不満は apology、感情は empathy) | 逆質問もあれば answer | pick: 選び直す | count |
| 聞き方への不満(答えなし) | apology | | skip: 今の欄を閉じて次へ | count |
| 前の問いに戻る | done | | back: 前の問いを直す問いとして | free |
| 直す、取り消す(答えなし) | done(何をしたか。about) | | keep: 今の問いを出し直す | free |
| 事実と違う頼み | なし | note(falsify) | keep | free |
| 雑談、身の上話 | light | | keep | aside |
| 逆質問 | なし | answer(渡す事実だけ) | keep | aside |
| 感情 | empathy | 疲れなら note(pause)と「急ぐ」 | keep | aside |
| 別の入り口を考えている(switch の consider だけ) | short | offer(移るボタンと、1 回だけの注意) | keep | free |

- count: count は数える、aside は「答えでないターン」(3 回まで数えない。数え方は入り口の今の決まり)、free は数えない
- 答えでないターン(offtrack)が PAUSE_AFTER 回続いたら、次のターンに note(pause。「中断」と送れば残る)を添える。
  疲れたとき(急ぐ)も同じ note。どちらも 1 つの相談で 1 回だけ(pause_due)
- 「急ぐ」(hurry): HARD のほかの問いを聞かずにまとめへ進む(入り口が問いの選び方で受け持つ)。疲れを言っただけのターンでだけ立てる。
  答えと一緒の「もう限界」(今の仕事の疲れ)では立てない
- 別の入り口を考えている(switch の consider)は、ほかの働きと一緒でも offer に移る先を入れる(入り口が、移るボタンを今の問いのボタンの前に
  足し、入り口の定義の注意を 1 回だけ添える。§4.5)
- 危ない言葉、あとで・やめる、闇バイト、別の入り口へ移る(switch の want)は、ここの前に入り口が受け持つ(決まった文。§4.6 の表の下の段。
  移るのは dip_mcp.convo.switch)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from dip_mcp.convo import facts as F
from dip_mcp.convo import switch as SW

PARTS = ("ack", "answer", "note", "phase_line", "ask")  # 返事の部品の並ぶ順
ACKS = ("short", "done", "light", "empathy", "apology")  # 相づちの種類(None は相づちなし)
NEXTS = ("pick", "keep", "back", "skip")
COUNTS = ("count", "aside", "free")
NOTES = ("pause", "falsify")  # 注意の 1 行の名前。文は入り口の定義の texts の note_<名前>
PAUSE_AFTER = 2  # 答えでないターンがこれだけ続いたら、次のターンに「中断」の注意を添える
TALK_KEYS = ("chitchat", "story")


@dataclass
class Plan:
    ack: str | None = "short"
    about: list = field(default_factory=list)    # 相づちで触れること(ハーネスが作る文。「勤務地を「横浜」に直しました」)
    answer: list = field(default_factory=list)   # 答える逆質問の種類(facts.KINDS)。空なら answer の部品は無い
    facts: list = field(default_factory=list)    # answer に使う事実(入り口が facts.pick で埋める)
    notes: list = field(default_factory=list)    # note の名前(NOTES)
    next: str = "pick"
    count: str = "count"
    hurry: bool = False
    offtrack: bool = False                       # 答えでないターン(続いた数を数える)
    offer: list = field(default_factory=list)    # 移るボタンを出す入り口(switch の consider)
    mention: list = field(default_factory=list)  # 相づちに必ず入れる言葉(どれか 1 つ。移った先の最初の返事の、引き継いだ値。§4.5)

    def to_dict(self) -> dict:
        return asdict(self)

    def for_say(self) -> dict:
        """言う段に渡す形(ack の種類、触れること、答えるか、事実)。note は渡さない(ハーネスが入れる)。"""
        return {"ack": self.ack, "about": list(self.about), "answer": bool(self.answer), "facts": list(self.facts)} | (
            {"mention": list(self.mention)} if self.mention else {})


def _names(reading: dict | None) -> list[str]:
    return [a["act"] for a in (reading or {}).get("acts", [])]


def feeling(reading: dict | None) -> str | None:
    return next((a["feeling"] for a in (reading or {}).get("acts", []) if a["act"] == "emotion"), None)


def respond(reading: dict | None, answered: bool = False, fixed: bool = False, entries=()) -> Plan:
    """読んだ act と、値を当てた結果(answered: 今の問いが埋まったかパスした、fixed: 直した・取り消した)から計画を作る(上の表)。
    entries は今の入り口から移れる入り口(switch の consider をボタンにする先)。"""
    p = _respond(reading, answered, fixed)
    p.offer = SW.offered(reading, entries)
    if p.offer and set(_names(reading)) == {"switch"}:  # 考えていると言っただけ: 受け止めてボタンを出す(数えない)
        return Plan(ack="short", next="keep", count="free", offer=p.offer)
    return p


def _respond(reading: dict | None, answered: bool, fixed: bool) -> Plan:
    names = set(_names(reading))
    asks = [k for k in F.KINDS if k in names]
    feel = feeling(reading)
    moved = "apology" if "complaint" in names else "empathy" if feel else None
    if answered:
        return Plan(ack=moved or "short", answer=asks)
    if "complaint" in names:
        return Plan(ack="apology", answer=asks, next="skip")
    if "go_back" in names:
        return Plan(ack="done", next="back", count="free")
    if fixed:
        return Plan(ack="done", next="keep", count="free")
    if "falsify" in names:
        return Plan(ack=None, notes=["falsify"], next="keep", count="free", offtrack=True)
    ack = "empathy" if feel else "light" if names & set(TALK_KEYS) else None if asks else "short"
    return Plan(ack=ack, answer=asks, next="keep", count="aside", hurry=feel == "tired", offtrack=True)


def pause_due(plan: Plan, streak: int, shown: bool) -> bool:
    """「中断」の注意を添えるか: 疲れた(急ぐ)か、答えでないターンが PAUSE_AFTER 回続いた後のターン(streak はこのターンを含む)。
    1 つの相談で 1 回だけ(shown: もう添えた)。"""
    return not shown and plan.offtrack and (plan.hurry or streak > PAUSE_AFTER)
