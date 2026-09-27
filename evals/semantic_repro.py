"""keywords の言い換えで、部分一致と意味検索(embedding)の結果を並べて比べる(task 28 の根拠)。

模擬ユーザーも LINE 側 LLM も使わず、dip のツールの中身(logic.recommend)を直接呼ぶ。
言い換えの問い合わせごとに、部分一致(意味検索を止めた状態)と意味検索ありを実行し、
上位3件に関係する求人(職種・待遇・時間帯で判定)が入ったかを記録する。wait_repro.py と同じく結果をファイルに書く。

    ./.venv/bin/python -m evals.semantic_repro --tag before-after

実 API は問い合わせ文の embedding だけ(1件 $0.0001 未満)。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 言い換えの問い合わせと、「関係する求人」の判定条件(求人データの事実で判定する)
CASES = [
    {"q": "人と話さない仕事がいい", "relevant": {"job_type": ["倉庫・軽作業", "オフィス事務", "配送・デリバリー"]}},
    {"q": "体を動かす仕事", "relevant": {"job_type": ["倉庫・軽作業", "イベントスタッフ", "配送・デリバリー"]}},
    {"q": "ごはんが出る職場", "relevant": {"perk": "まかない"}},
    {"q": "子どもに勉強を教えたい", "relevant": {"job_type": ["塾講師・家庭教師"]}},
    {"q": "もくもく作業", "relevant": {"job_type": ["倉庫・軽作業", "オフィス事務", "スーパー・販売"]}},
    {"q": "深夜にがっつり稼ぎたい", "relevant": {"time_band": "深夜"}},
    {"q": "おしゃれな職場で働きたい", "relevant": {"job_type": ["アパレル販売", "カフェ"]}},
]


def _relevant(job, spec: dict) -> bool:
    if "job_type" in spec and job.job_type in spec["job_type"]:
        return True
    if "perk" in spec and spec["perk"] in job.perks:
        return True
    return "time_band" in spec and spec["time_band"] in job.time_bands


def _run(state, q: str) -> dict:
    from dip_mcp import logic
    from dip_mcp.data import load_jobs

    jobs = load_jobs()
    r = logic.recommend(state, None, keywords=q)
    top = [{"job_id": j["job_id"], "title": j["title"], "job_type": j["job_type"],
            "keywords_score": j["score_breakdown"].get("keywords")} for j in r["jobs"]]
    return {"total": r["total"], "top": top, "jobs": [jobs[t["job_id"]] for t in top]}


def main(tag: str) -> None:
    from dip_mcp import embedding
    from dip_mcp.data import State

    state = State(Path(tempfile.mkdtemp()) / "state.json")
    state.reset()
    doc = {"tag": tag, "model": embedding.MODEL, "dimensions": embedding.DIMENSIONS, "cases": []}
    for case in CASES:
        q = case["q"]
        orig = embedding.embed_query
        embedding.embed_query = lambda text: None  # 意味検索を止め、従来の部分一致だけにする
        try:
            sub = _run(state, q)
        finally:
            embedding.embed_query = orig
        sem = _run(state, q)  # 意味検索あり(実 API を1回呼ぶ。失敗すると部分一致と同じ結果になる)
        row = {"q": q, "relevant": case["relevant"],
               "substring": {"total": sub["total"], "top": sub["top"],
                             "relevant_in_top3": sum(_relevant(j, case["relevant"]) for j in sub["jobs"])},
               "embedding": {"total": sem["total"], "top": sem["top"],
                             "relevant_in_top3": sum(_relevant(j, case["relevant"]) for j in sem["jobs"])}}
        doc["cases"].append(row)
        print(f"{q}: 部分一致 {sub['total']}件(関係する求人 {row['substring']['relevant_in_top3']}/3)"
              f" -> embedding {sem['total']}件(関係する求人 {row['embedding']['relevant_in_top3']}/3)"
              f" 上位: {[(t['job_id'], t['job_type']) for t in sem['top']]}", flush=True)
    doc["summary"] = {
        "cases": len(CASES),
        "substring_relevant_in_top3": sum(c["substring"]["relevant_in_top3"] for c in doc["cases"]),
        "embedding_relevant_in_top3": sum(c["embedding"]["relevant_in_top3"] for c in doc["cases"]),
        "substring_zero_hits": sum(c["substring"]["total"] == 0 for c in doc["cases"]),
        "embedding_zero_hits": sum(c["embedding"]["total"] == 0 for c in doc["cases"]),
    }
    out = HERE / "results" / f"semantic-repro-{tag}-{datetime.now():%Y%m%d-%H%M%S}.json"  # noqa: DTZ005  run.py と同じ(ローカル時刻のファイル名)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    print(f"-> {out}  {json.dumps(doc['summary'])}")


if __name__ == "__main__":
    from line_runtime.config import load_env

    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()
    main(args.tag)
