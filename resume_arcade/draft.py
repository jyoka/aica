"""ゲームで作った書類(Markdown)を Resume Draft(resume_draft.schema)に写す。

書類の Markdown は、ゲームの共通部品 arcade-ja.js の resumeMarkdown の形(本人が画面で直したものも同じ形のまま読む):

    # [氏名]
    **見出し**
    [メール] · [電話] · [居住地] · [リンク]
    ## 職務要約 / ## 自己PR / ## 職務経歴(### 勤務先｜役職、*期間*、- 行)/ ## スキル / ## 学歴

写し方(docs/prd/resume-arcade.md の「作った書類の保存」): 職務要約 → job_summary、自己PR → self_pr、職場 → work_history
(勤務先、役職、期間、行は achievements)、スキル → skills、学歴 → education。数字の空欄は行に残し、空欄のある行ごとに
unconfirmed(本人に確かめたい点)へ 1 つ書く。見出しと知らない節は Resume Draft に項目が無いので、Markdown にだけ残る。
氏名と連絡先は聞かないので、personal は常に空(envelope がそうする)。
"""

from __future__ import annotations

import re

from resume_draft.pii import scrub_pii
from resume_draft.schema import Education, ResumeDraft, WorkExperience, envelope
from resume_lines import PLACEHOLDER_RE

SOURCE = "arcade"
MARKDOWN_MAX = 20_000  # 書類の Markdown の長さの上限(字)。音声の下書き(line_runtime.server.DRAFT_MAX)と同じ
EMPTY_JOB = "[ここに実績を2〜3行書く]"
_SKILL_SEP = re.compile(r"\s*(?:·|・|、|,|/)\s*")


def _plain(line: str) -> str:
    return re.sub(r"\*\*|__|`", "", line).strip()


def parse_markdown(md: str) -> ResumeDraft:
    """書類の Markdown を Resume Draft にする(書いてある節だけ)。"""
    section, summary, self_pr, edu, skills = "", [], [], [], []
    jobs: list[WorkExperience] = []
    for raw in str(md or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("# ") or line == "---":
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section == "職務経歴":
            if line.startswith("### "):
                org, _, role = line[4:].partition("｜")
                if not role:
                    org, _, role = line[4:].partition("|")
                jobs.append(WorkExperience(organization=_plain(org), role=_plain(role)))
            elif jobs and re.fullmatch(r"[*_].*[*_]", line) and not jobs[-1].period and not jobs[-1].achievements:
                jobs[-1].period = line.strip("*_ ")
            elif jobs and line != f"- {EMPTY_JOB}":
                jobs[-1].achievements.append(_plain(re.sub(r"^[-*・]\s*", "", line)))
        elif section == "職務要約":
            summary.append(_plain(line))
        elif section == "自己PR":
            self_pr.append(_plain(line))
        elif section == "スキル":
            skills += [x for x in (_plain(s) for s in _SKILL_SEP.split(line)) if x]
        elif section == "学歴":
            edu.append(Education(school=_plain(line)))
    return ResumeDraft(job_summary="".join(summary), self_pr="".join(self_pr), education=edu, work_history=jobs,
                       skills=list(dict.fromkeys(skills)), unconfirmed=unconfirmed(jobs, "".join(summary), "".join(self_pr)))


def _shorten(text: str, n: int = 24) -> str:
    t = PLACEHOLDER_RE.sub("", text).strip(" 、。:：")
    return t if len(t) <= n else t[:n] + "…"


def unconfirmed(jobs: list[WorkExperience], summary: str, self_pr: str) -> list[str]:
    """空欄のある行ごとに、本人に確かめたい点を 1 つ(「〇〇の行の数字」)。"""
    out = []
    if PLACEHOLDER_RE.search(summary):
        out.append("職務要約の数字")
    if PLACEHOLDER_RE.search(self_pr):
        out.append("自己PRの数字")
    for w in jobs:
        for a in w.achievements:
            if PLACEHOLDER_RE.search(a):
                out.append(f"{w.organization}の「{_shorten(a)}」の行の数字")
        if not w.achievements:
            out.append(f"{w.organization}の実績")
    return out


def to_envelope(md: str, document_kind: str) -> dict:
    """保存する形: Resume Draft の envelope(format_version 1.1、source は arcade)と、書類の Markdown。
    電話番号・メール・郵便番号は、Markdown からも Resume Draft からも落とす。"""
    md = scrub_pii(str(md or "")[:MARKDOWN_MAX])
    return envelope(parse_markdown(md), source=SOURCE, document_kind=document_kind) | {"markdown": md}
