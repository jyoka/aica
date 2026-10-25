"""面談の終わりに作る成果物。config.WRITE_FILES(VOICE_RESUME_WRITE_FILES=1、開発用)のときだけ OUT_DIR へ書き出す。

既定は書き出さない。下書きはトーク経由で dip の状態(voice_results)に入る(docs/design/privacy.md の 3.3)。
書き出すときのファイル:

- resume-draft-<ts>.json / .md: 下書き(履歴書か職務経歴書。形は docs/architecture/lld.md の 5.5 の format_version 1.1)
- transcript-<ts>.json: 文字起こし
- job-proposal-<ts>.json: 本人が言った希望(preferences)・検索に使った条件(recommend_args)・
  検索のためにゆるめた条件(relaxed)・検索の経緯(search_attempts)・提案した求人ID。
  希望と検索条件は分けて持つ(検索のためにゆるめた条件を本人の希望として書かない)
"""

from __future__ import annotations

import datetime as dt
import json

from resume_draft.schema import DEFAULT_DOCUMENT_KIND, JST
from voice_resume import config
from voice_resume.drafter import build_draft, finalize
from voice_resume.render import to_markdown


def history_transcript(chat_ctx) -> list[dict]:
    """AgentSession の履歴(ChatContext)を [{role, text}] にする。"""
    transcript: list[dict] = []
    for item in getattr(chat_ctx, "items", []):
        if getattr(item, "type", None) != "message" or item.role not in ("user", "assistant"):
            continue
        text = (item.text_content or "").strip()
        if text:
            transcript.append({"role": item.role, "text": text})
    return transcript


async def write_outputs(
    transcript: list[dict],
    *,
    client,
    preferences: dict | None = None,
    recommend_args: dict | None = None,
    relaxed: dict | None = None,
    search_attempts: list[dict] | None = None,
    proposed_jobs: list[dict] | None = None,
    source: str = "voice-interview",
    document_kind: str = DEFAULT_DOCUMENT_KIND,
) -> dict:
    """下書き(document_kind の書類)を生成し、{resume, markdown, proposal, files, stamp} を返す。

    config.WRITE_FILES のときだけ OUT_DIR に書き出す。書かないときの files は空。
    client は AsyncOpenAI(テストではスタブ)。
    """
    draft = await build_draft(transcript, client, document_kind=document_kind)
    env = finalize(draft, source=source, document_kind=document_kind)
    markdown = to_markdown(env)
    proposal = {
        "preferences": preferences or {},
        "recommend_args": recommend_args or {},
        # 検索のためにゆるめた(外した)条件。本人の希望(preferences)は書き換えない
        "relaxed": relaxed or {},
        "search_attempts": search_attempts or [],
        "proposed_job_ids": [j.get("job_id", "") for j in (proposed_jobs or [])],
        "jobs": proposed_jobs or [],
    }

    stamp = dt.datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    result = {"resume": env, "markdown": markdown, "proposal": proposal, "files": [], "stamp": stamp}
    if not config.WRITE_FILES:
        return result

    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        "resume_json": config.OUT_DIR / f"resume-draft-{stamp}.json",
        "resume_md": config.OUT_DIR / f"resume-draft-{stamp}.md",
        "transcript": config.OUT_DIR / f"transcript-{stamp}.json",
        "proposal": config.OUT_DIR / f"job-proposal-{stamp}.json",
    }
    files["resume_json"].write_text(json.dumps(env, ensure_ascii=False, indent=2), encoding="utf-8")
    files["resume_md"].write_text(markdown, encoding="utf-8")
    files["transcript"].write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    files["proposal"].write_text(
        json.dumps(proposal, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["files"] = [str(p) for p in files.values()]
    return result
