"""求人全件のベクトルを作って fixtures/job_vectors.json に保存する(recommend_jobs の意味検索用)。

求人データ(anchor_jobs.json / jobs.json / fulltime_jobs.json)を変えたら回し直す(ずれたままだと pytest が落ちる。
ずれた求人は実行時には意味検索から外れ、部分一致だけで見る)。
実 API(OpenAI embeddings、text-embedding-3-small)を使うが、103件(約3.2万トークン)で $0.001 もかからない
(料金 $0.02 / 100万トークン。公式ドキュメントで 2026-09-29 に確認)。

    ./.venv/bin/python -m scripts.gen_job_vectors
"""

from __future__ import annotations

import json

from dip_mcp import embedding
from dip_mcp.data import load_jobs
from line_runtime.config import load_env


def main() -> None:
    from openai import OpenAI

    jobs = list(load_jobs().values())
    resp = OpenAI().embeddings.create(model=embedding.MODEL, dimensions=embedding.DIMENSIONS,
                                      input=[embedding.job_text(j) for j in jobs])
    out = {
        "_note": "scripts/gen_job_vectors.py が作る(手で編集しない)。求人データを変えたら回し直す。",
        "model": embedding.MODEL,
        "dimensions": embedding.DIMENSIONS,
        "jobs": {j.job_id: {"hash": embedding.job_hash(j), "v": [round(x, 6) for x in embedding.unit(d.embedding)]}
                 for j, d in zip(jobs, resp.data, strict=True)},
    }
    embedding.VECTORS_PATH.write_text(json.dumps(out, ensure_ascii=False) + "\n")
    print(f"{len(jobs)} jobs -> {embedding.VECTORS_PATH}(model={embedding.MODEL}, dims={embedding.DIMENSIONS}, "
          f"tokens={resp.usage.total_tokens})")


if __name__ == "__main__":
    load_env()
    main()
