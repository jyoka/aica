"""平均時給の参考データ(全国・東京)。

平均時給は頻繁には変わらないので、会話のたびに web で調べず、参考データとして
data/wage_reference.json に保存しておき、聞かれたらすぐ答える。
更新は scripts/update_wage_reference.py を手で回す(年1回くらい。答えには調査の時期を添える)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

REFERENCE_PATH = Path(__file__).resolve().parent / "data" / "wage_reference.json"
AREAS = ("全国", "東京都")
_ALIASES = {"": "全国", "全国": "全国", "東京": "東京都", "東京都": "東京都"}


class WageEntry(BaseModel):
    area: str = Field(description="全国 / 東京都")
    average_wage: int = Field(description="アルバイト・パートの平均時給(円)")


class WageReference(BaseModel):
    survey: str = Field(description="調査の名前(例: マイナビ アルバイト・パート平均時給レポート)")
    period: str = Field(description="調査の時期(例: 2026年8月)")
    source: str = Field(description="情報源のサイト名だけ(URL やリンクは入れない)。読み上げに使う")
    source_url: str = Field(default="", description="情報源の URL(確かめる用。読み上げない)")
    entries: list[WageEntry]
    fetched_at: str = Field(default="", description="調べた日(YYYY-MM-DD)")


def load_reference(path: Path = REFERENCE_PATH) -> WageReference | None:
    if not path.is_file():
        return None
    return WageReference.model_validate_json(path.read_text(encoding="utf-8"))


def answer_from_reference(ref: WageReference | None, area: str | None) -> str | None:
    """全国・東京の平均時給を参考データから答える。参考データに無い地域なら None。"""
    key = _ALIASES.get(area or "")
    if ref is None or key is None:
        return None
    wages = {e.area: e.average_wage for e in ref.entries}
    if key not in wages:
        return None
    parts = [f"{a}で{wages[a]}円" for a in AREAS if a in wages]
    return (f"{ref.survey}({ref.period})によると、アルバイト・パートの平均時給は{'、'.join(parts)}です。"
            f"主な情報源は{ref.source}です。")


async def fetch_reference(client, model: str, today: str) -> WageReference:
    """web で全国と東京都の平均時給を調べ、参考データの形にする(更新スクリプト用)。"""
    resp = await client.responses.parse(
        model=model,
        tools=[{"type": "web_search"}],
        input=("全国と東京都の、アルバイト・パートの平均時給を web で調べてください。"
               "できるだけ新しい、同じ1つの調査の数字を使い、調査の名前・時期・情報源のサイト名と URL も返してください。"),
        text_format=WageReference,
        store=False,  # OpenAI に応答を残さない(docs/design/privacy.md の 3.2)
    )
    ref = resp.output_parsed
    ref.source = re.sub(r"\s*\(?\[[^\]]*\]\([^)]*\)\)?|https?://\S+", "", ref.source).strip()  # 読み上げないリンクを消す
    ref.fetched_at = today
    return ref


def save_reference(ref: WageReference, path: Path = REFERENCE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ref.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
