"""ゲームの段ごとの入力(画面が送るもの)と出力(LLM の構造化出力)の形。

画面が送るのは、ゲームの名前、段、本人の答え(と、まとめの段では書類の行)だけ。指示の文と出力の形はサーバーが持つ
(画面が指示の文を送れると、Web サーバーが誰でも使える LLM の中継になり、費用の上限も事実の確かめも効かないため)。
長さの上限は docs/prd/resume-arcade.md の「費用の上限」: 答えは 600 字(超えた分は送らない)、まとめの行は 1 行 120 字、
引用 40 字、1 職場 6 行、合計 4,000 字(超えて届いたら LLM を呼ばない。stages.summary_chars)。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field

ANSWER_MAX = 600
LINE_MAX = 120
QUOTE_MAX = 40
LINES_PER_JOB = 6
JOBS_MAX = 4
SUMMARY_MAX = 4000


def _cut(n: int):
    """長すぎる文字列は、超えた分を落とす(断らない。答えの 600 字と同じく「超えた分は送らない」)。"""
    return BeforeValidator(lambda v: str(v if v is not None else "")[:n])


def Text(n: int):
    return Annotated[str, _cut(n)]


# ----------------------------- あなたという事件(the-case-of-you) -----------------------------

ROLES = ("boss", "coworker", "client", "rival")
STRENGTHS = ("fixer", "architect", "closer", "mentor", "talker", "bloodhound", "speed", "caretaker")


class CaseJob(BaseModel):
    id: Text(8) = ""
    company: Text(60) = ""
    title: Text(60) = ""
    dates: Text(40) = ""


class CaseIntake(BaseModel):
    """事件の受付で本人が書いたこと(氏名は聞かない)。"""
    title: Text(60) = ""
    years: Text(20) = ""
    target: Text(80) = ""
    tools: Text(200) = ""
    edu: Text(120) = ""


class CaseLine(BaseModel):
    """書類の行(証拠のカード)。text は行の文、quote は本人の言葉の引用、metric は行の数字。"""
    id: Text(12) = ""
    title: Text(20) = ""
    text: Text(LINE_MAX) = ""
    quote: Text(QUOTE_MAX) = ""
    metric: Text(QUOTE_MAX) = ""
    strength: Text(16) = ""


class CaseOpeningIn(BaseModel):
    intake: CaseIntake = Field(default_factory=CaseIntake)
    job: CaseJob
    role: Literal["boss", "coworker", "client", "rival"]
    witness: Text(20) = "証人"
    known: Annotated[list[Text(LINE_MAX)], Field(max_length=LINES_PER_JOB)] = []


class CaseReactionIn(CaseOpeningIn):
    question: Text(200) = ""
    answer: Text(ANSWER_MAX)
    round: Literal[1, 2] = 1
    first_answer: Text(ANSWER_MAX) = ""
    cross: Text(200) = ""


class CaseLabIn(BaseModel):
    cards: Annotated[list[CaseLine], Field(max_length=JOBS_MAX * LINES_PER_JOB)]


class CaseVerdictJob(CaseJob):
    lines: Annotated[list[CaseLine], Field(max_length=LINES_PER_JOB)] = []


class CaseVerdictIn(BaseModel):
    intake: CaseIntake = Field(default_factory=CaseIntake)
    jobs: Annotated[list[CaseVerdictJob], Field(max_length=JOBS_MAX)]
    weaknesses: Annotated[list[CaseLine], Field(max_length=LINES_PER_JOB)] = []
    clusters: Annotated[list[Text(40)], Field(max_length=8)] = []
    document_kind: Literal["履歴書", "職務経歴書"] | None = None


class Opening(BaseModel):
    line: str = Field(description="証人が言うこと。態度の 1 文と、質問 1 つ。110 字まで")


class Evidence(BaseModel):
    title: str = Field(description="ノワールの事件名。10 字まで")
    fact: str = Field(description="本人が言ったことを言い直した 1 文(過去形、新しい事実を足さない)")
    quote: str = Field(description="本人の言葉からそのまま写した句。40 字まで")
    strength: str = Field(description="強みの英語のキー")
    metric: str = Field(description="本人が書いたままの数字。無ければ空文字")


class Reaction(BaseModel):
    line: str = Field(description="証人が次に言うこと。70 字まで")
    vague: bool
    evidence: list[Evidence]


class Cluster(BaseModel):
    name: str
    ids: list[str]
    why: str


class Lab(BaseModel):
    clusters: list[Cluster]


class Judgement(BaseModel):
    name: str
    verdict: str
    exhibits: list[str]


class Lead(BaseModel):
    title: str
    action: str


class CaseExperience(BaseModel):
    jobId: str
    bullets: list[str]


class CaseResume(BaseModel):
    headline: str
    summary: str
    self_pr: str = Field(description="自己PR(3〜4 文)。書類が履歴書のときだけ。ほかは空文字")
    experience: list[CaseExperience]
    skills: list[str]
    education: str


class Verdict(BaseModel):
    monologue: str
    strengths: list[Judgement]
    weaknesses: list[Judgement]
    leads: list[Lead]
    resume: CaseResume


# ----------------------------- 仕事の収穫(harvest-of-work) -----------------------------

SKILLS = ("leadership", "technical", "communication", "analysis", "creativity", "customer")
FOCUS = ("context", "action", "result", "detail", "more", "metric")


class HarvestJob(BaseModel):
    company: Text(60) = ""
    title: Text(70) = ""
    years: Text(30) = ""


class HarvestQA(BaseModel):
    q: Text(200) = ""
    a: Text(ANSWER_MAX) = ""


class HarvestTurnIn(BaseModel):
    target_role: Text(80) = ""
    job: HarvestJob = Field(default_factory=HarvestJob)
    title: Text(80) = ""
    story: Text(ANSWER_MAX) = ""
    earlier: Annotated[list[HarvestQA], Field(max_length=4)] = []
    question: Text(200) = ""
    answer: Text(ANSWER_MAX) | None = None
    next_focus: Literal["context", "action", "result", "detail", "more", "metric"] | None = None


class HarvestLine(BaseModel):
    text: Text(LINE_MAX) = ""
    quote: Text(QUOTE_MAX) = ""
    metric: Text(QUOTE_MAX) = ""
    skill: Text(16) = ""


class HarvestFinalJob(HarvestJob):
    lines: Annotated[list[HarvestLine], Field(max_length=LINES_PER_JOB)] = []


class HarvestFinalIn(BaseModel):
    target_role: Text(80) = ""
    family: Text(40) = ""
    education: Text(120) = ""
    expected: Annotated[list[Text(16)], Field(max_length=6)] = []
    empty: Annotated[list[Text(16)], Field(max_length=6)] = []
    jobs: Annotated[list[HarvestFinalJob], Field(max_length=JOBS_MAX)]
    document_kind: Literal["履歴書", "職務経歴書"] | None = None


class Turn(BaseModel):
    quality: Literal["good", "vague", "n/a"]
    metric: bool
    skill: Literal["leadership", "technical", "communication", "analysis", "creativity", "customer"]
    say: str = Field(description="ひとことの反応。50 字まで")
    ask: str = Field(description="次の質問 1 つ。60 字まで。聞くことが無ければ none")


class HarvestExperience(BaseModel):
    company: str
    bullets: list[str]


class HarvestStrength(BaseModel):
    skill: str
    title: str
    evidence: str
    why: str


class HarvestWeakness(BaseModel):
    skill: str
    title: str
    evidence: str
    next: str


class HarvestFinal(BaseModel):
    headline: str
    summary: str
    self_pr: str = Field(description="自己PR(3〜4 文)。書類が履歴書のときだけ。ほかは空文字")
    experience: list[HarvestExperience]
    skills: list[str]
    strengths: list[HarvestStrength]
    weaknesses: list[HarvestWeakness]
    next_season: list[str]
