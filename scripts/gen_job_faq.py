"""求人ごとの Q&A(案件単位の FAQ)を架空で生成して fixtures/job_faq.json に足す。

すでに Q&A がある求人(手で書いたものを含む)は上書きしない。実 API を使うので費用がかかる
(Sonnet 5 で 60 件ほど、約 $0.5 の見込み。gpt-6-sol も同じ単価)。回す前に承認を得ること。

    ./.venv/bin/python -m scripts.gen_job_faq [--model gpt-6-sol] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json

from pydantic import BaseModel, Field

from dip_mcp.data import FIXTURES, load_job_faq, load_jobs
from line_runtime import config, llm
from line_runtime.config import load_env

BATCH = 6

SYSTEM = """あなたはアルバイト求人の「よくある質問」を作るライターです。テストデータ用に、**架空の**求人それぞれについて、
応募を考えている人が聞きそうな質問と、店舗・企業の回答を作ります。
- 1件につき5〜6問。テーマは求人に合わせて選ぶ: 制服・服装 / 研修・未経験 / 休憩・まかない・食事 / 職場の雰囲気・年齢層 /
  シフトの融通(土日・テスト期間・週の日数)/ 忙しい時間帯 / 必要なスキル / 交通手段 など。
- 回答は、求人データ(説明・大変なところ・待遇・シフト・時給)と矛盾させない。求人データにある事実を言い換えるだけの質問は避け、
  求人データに無い具体的なこと(制服の中身、研修の進め方など)を、ありそうな架空の内容で答える。
- 待遇(perks)に無いこと(未経験OK・髪色自由・日払いなど)を、あるかのように答えない。
- 回答は1〜2文、敬体。実在の店舗・企業・人物・社内の数値は出さない。"""


class QA(BaseModel):
    q: str
    a: str


class JobFAQ(BaseModel):
    job_id: str
    faq: list[QA] = Field(description="5〜6問")


class Batch(BaseModel):
    jobs: list[JobFAQ]


async def gen(client, model: str, jobs: list) -> dict[str, list[dict]]:
    payload = json.dumps([j.model_dump() for j in jobs], ensure_ascii=False)
    batch = await llm.parse(
        client, model=model, max_tokens=12000, effort="low", system=SYSTEM, schema=Batch,
        messages=[{"role": "user", "content": f"次の求人それぞれの Q&A を作ってください。job_id はそのまま使う。\n{payload}"}],
    )
    ids = {j.job_id for j in jobs}
    return {x.job_id: [qa.model_dump() for qa in x.faq] for x in batch.jobs if x.job_id in ids}


async def main(model: str, dry_run: bool) -> None:
    existing = load_job_faq()
    todo = [j for j in load_jobs().values() if j.job_id not in existing]
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    print(f"Q&A が無い求人 {len(todo)} 件を {len(batches)} 回に分けて生成する(model={model})")
    if dry_run or not todo:
        return
    client = llm.client()
    results = await asyncio.gather(*[gen(client, model, b) for b in batches])
    path = FIXTURES / "job_faq.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    added = 0
    for r in results:
        for job_id, faq in r.items():
            if job_id not in data:  # 手で書いたものは上書きしない
                data[job_id] = faq
                added += 1
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"{added} 件の求人に Q&A を追加 -> {path}")


if __name__ == "__main__":
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default={"openai": "gpt-6-sol", "anthropic": "claude-sonnet-5"}[config.PROVIDER])
    ap.add_argument("--dry-run", action="store_true", help="何件を生成するかだけ表示する(API を呼ばない)")
    args = ap.parse_args()
    asyncio.run(main(args.model, args.dry_run))
