"""シミュレーション用の架空アルバイト求人を生成して fixtures/jobs.json に書く。

実在の求人サイトは使わない(規約と、実店舗の情報を持ち込まないため)。職種ごとに LLM で作る。
作り直したら scripts/gen_job_vectors.py も回す(求人ベクトルがずれたままだと pytest が落ちる)。
正社員の求人(fixtures/fulltime_jobs.json)はこのスクリプトでは作らない(手で書いた架空の40件)。

    ./.venv/bin/python -m scripts.gen_jobs [--per 5]
"""

from __future__ import annotations

import argparse
import asyncio
import json

from pydantic import BaseModel

from dip_mcp.data import FIXTURES, PHYSICAL_LEVELS, TALK_LEVELS, BaitoJob, Pay
from line_runtime import config, llm
from line_runtime.config import load_env

MODEL = {"openai": "gpt-6-sol", "anthropic": "claude-opus-5"}

JOB_TYPES = [
    "飲食ホール", "飲食キッチン", "カフェ", "コンビニ", "スーパー・販売", "アパレル販売",
    "倉庫・軽作業", "配送・デリバリー", "コールセンター", "塾講師・家庭教師", "イベントスタッフ", "オフィス事務",
]
AREAS = "新宿、渋谷、池袋、中野、高円寺、吉祥寺、品川、上野、秋葉原、横浜、川崎、大宮"

SYSTEM = f"""あなたは日本のアルバイト求人票を作るライターです。テストデータ用に、**架空の**店舗・企業の求人を作ります。
- 店舗名・企業名は実在しない架空の名前にする(実在チェーンに似せない)。
- エリアは次から選ぶ: {AREAS}。station は実在の駅名でよい。walk_min は駅徒歩分。
- hourly_wage は時給(円)。東京近郊の相場感で、職種・時間帯により 1,100〜2,500 円程度でばらつかせる。
- time_bands は 早朝 / 日中 / 夕方 / 夜 / 深夜 から該当するもの(複数可)。shift は具体的な時間(例: 18:00〜翌1:00)。
- days_min は週の最低勤務日数。perks は 未経験OK / 学生歓迎 / 主婦(夫)歓迎 / 日払い / 交通費支給 / まかない / 髪色自由 / 短時間OK / シフト自由 から該当するもの。
- description は2〜3文。drawback には現実的な弱み(忙しい時間帯、立ち仕事、土日必須など)を1文で書く。
- talk_level は人と話す量(1〜3): {" / ".join(f"{k} = {v}" for k, v in TALK_LEVELS.items())}。
- physical_level は体を使う量(1〜3): {" / ".join(f"{k} = {v}" for k, v in PHYSICAL_LEVELS.items())}。
- 同じ職種でもエリア・時間帯・時給がばらつくようにする。"""


class Draft(BaseModel):
    """LLM に作らせるアルバイト求人の形(給与は時給だけなので hourly_wage で受け取り、BaitoJob の pay に移す)。"""
    title: str
    employer: str
    area: str
    station: str
    walk_min: int
    hourly_wage: int
    time_bands: list[str]
    shift: str
    days_min: int
    perks: list[str]
    description: str
    drawback: str
    talk_level: int
    physical_level: int


class Batch(BaseModel):
    jobs: list[Draft]


async def gen(client, job_type: str, n: int, idx: int) -> list[BaitoJob]:
    batch = await llm.parse(
        client, model=MODEL[config.PROVIDER], max_tokens=12000, effort="low", system=SYSTEM, schema=Batch,
        messages=[{"role": "user", "content": f"職種「{job_type}」の求人を{n}件。"}],
    )
    return [BaitoJob(**d.model_dump(exclude={"hourly_wage"}), job_id=f"b{idx:02d}{i:02d}",
                     employment_type="アルバイト・パート", job_type=job_type, pay=Pay(kind="時給", min=d.hourly_wage))
            for i, d in enumerate(batch.jobs, 1)]


async def main(per: int) -> None:
    client = llm.client()
    batches = await asyncio.gather(*[gen(client, t, per, i) for i, t in enumerate(JOB_TYPES, 1)])
    jobs = [j for b in batches for j in b]
    out = FIXTURES / "jobs.json"
    out.write_text(json.dumps([j.model_dump() for j in jobs], ensure_ascii=False, indent=2) + "\n")
    print(f"{len(jobs)} jobs -> {out}")


if __name__ == "__main__":
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=5)
    asyncio.run(main(ap.parse_args().per))
