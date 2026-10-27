"""ゲームで作った書類(Markdown)を Resume Draft(resume_draft.schema)に写す。

写し方の中身は resume_draft.markdown にある(正社員のヒアリングの下書きも同じ形で、dip_mcp が使うため)。
ここは、ゲームの側の呼び名をそのまま残す。
"""

from __future__ import annotations

from resume_draft.markdown import (  # noqa: F401  ゲームの側の呼び名
    EMPTY_JOB,
    MARKDOWN_MAX,
    SOURCE,
    parse_markdown,
    to_envelope,
    unconfirmed,
)
