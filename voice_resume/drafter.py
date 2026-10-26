"""文字起こし → 下書き(ResumeDraft)。書類の種類(履歴書 / 職務経歴書)で LLM への指示を変える。

この部分はオフラインでテストする(実 API を呼ばない)。client を外から渡し、
テストでは responses.parse(OpenAI)か messages.parse(Anthropic)を持つスタブを渡す。
送り先は client の型で決まる(既定は config.PROVIDER の anthropic。advisor の _default_client)。
Anthropic には構造化出力で、思考は任せて effort を低め(config.DRAFT_EFFORT)にし、キャッシュの区切りは付けない
(文字起こしは 1 回しか送らない)。
"""

from __future__ import annotations

from line_runtime import llm as line_llm
from resume_draft.pii import scrub_pii
from resume_draft.schema import DEFAULT_DOCUMENT_KIND, ResumeDraft, envelope
from voice_resume import config
from voice_resume.prompts import drafter_system, drafter_user


def transcript_to_text(transcript: list[dict]) -> str:
    """[{role, text}] を「本人:/アドバイザー:」のテキストに直す。"""
    names = {"user": "本人", "assistant": "アドバイザー"}
    lines = []
    for turn in transcript:
        text = str(turn.get("text", "")).strip()
        if not text:
            continue
        lines.append(f"{names.get(turn.get('role'), turn.get('role', '?'))}: {text}")
    return "\n".join(lines)


async def build_draft(transcript: list[dict], client, *, model: str | None = None,
                      document_kind: str = DEFAULT_DOCUMENT_KIND) -> ResumeDraft:
    """文字起こしから ResumeDraft を作る。client は AsyncAnthropic か AsyncOpenAI(またはテスト用スタブ)。
    document_kind が職務経歴書なら、職務要約と職歴ごとの成果(本人が言った数字)を中心に書かせる。"""
    text = transcript_to_text(transcript)
    if not text:
        return ResumeDraft()
    model = model or config.DRAFT_MODEL
    messages = [{"role": "user", "content": drafter_user(text, document_kind)}]
    if hasattr(client, "responses"):  # OpenAI
        resp = await client.responses.parse(
            model=model,
            instructions=drafter_system(document_kind),
            input=messages,
            text_format=ResumeDraft,
            max_output_tokens=4096,
            store=False,  # OpenAI に応答を残さない(docs/design/privacy.md の 3.2)
        )
        draft = resp.output_parsed
    else:
        # 思考も max_tokens に数えるので、OpenAI の 4096 より多めにする(使った分だけ払う)
        draft = await line_llm.parse(client, model=model, system=drafter_system(document_kind), messages=messages,
                                     schema=ResumeDraft, max_tokens=8192, effort=config.DRAFT_EFFORT, cache=False)
    draft = draft or ResumeDraft()
    return ResumeDraft.model_validate(scrub_pii(draft.model_dump()))


def finalize(draft: ResumeDraft, *, source: str = "voice-interview", document_kind: str = DEFAULT_DOCUMENT_KIND) -> dict:
    """保存用 JSON(envelope)にする。"""
    return envelope(draft, source=source, document_kind=document_kind)
