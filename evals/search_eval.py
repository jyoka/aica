"""求人の意味検索(recommend_jobs の keywords)の質を、正解つきの評価セットの上位3件の再現率で測る。

模擬ユーザーも LINE 側 LLM も使わず、dip のツールの中身(logic.recommend)を直接呼ぶ。
評価セットは evals/search_eval_set.json(問い合わせごとに正解の求人・職種を決めてある)。

再現率@3 = 上位3件のうち正解の件数 ÷ min(3, 正解の件数)。正解が1件の問い合わせは、それが3位以内なら 1.0。

    # オフライン(実 API なし): 保存済みの求人ベクトルと、記録した問い合わせのベクトルで今の設定を測る
    ./.venv/bin/python -m evals.search_eval
    # 問い合わせのベクトルを今の設定(embedding.MODEL・DIMENSIONS)で記録し直す(実 API、$0.0001 未満)
    ./.venv/bin/python -m evals.search_eval record
    # 設定(モデル・次元・求人の文面・下限と相対値・重み)を比べる(実 API。結果は data/search_eval/ に書く)
    ./.venv/bin/python -m evals.search_eval compare

compare の求人ベクトルは、モデルと文面ごとに最大の次元で1回だけ作り、小さい次元は先頭を切って正規化し直して使う
(text-embedding-3 系の dimensions は先頭を残して縮めるのと同じ、と公式ドキュメントにある)。作ったベクトルは
data/search_eval/ に置いて使い回す(2回目からは実 API を呼ばない)。
"""

from __future__ import annotations

import argparse
import itertools
import json
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from hashlib import sha1
from pathlib import Path
from statistics import mean

from dip_mcp import embedding, logic
from dip_mcp.data import FIXTURES, ROOT, BaitoJob, State, load_jobs, pay_text

HERE = Path(__file__).resolve().parent
EVAL_SET = HERE / "search_eval_set.json"
QUERY_VECTORS = FIXTURES / "search_query_vectors.json"
CACHE = ROOT / "data" / "search_eval"
K = 3
# 料金(US$ / 100万トークン)。公式の料金表(developers.openai.com/api/docs/pricing)で 2026-09-29 に確認
PRICE_PER_M = {"text-embedding-3-small": 0.02, "text-embedding-3-large": 0.13}
FULL_DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}


# ----------------------------- 評価(オフライン) -----------------------------

def load_cases() -> list[dict]:
    return json.loads(EVAL_SET.read_text(encoding="utf-8"))["cases"]


def relevant_ids(case: dict, jobs: dict[str, BaitoJob]) -> set[str]:
    """その問い合わせの正解の求人。雇用区分を指定した問い合わせは、その区分の求人だけ。"""
    exp, et = case["expected"], case.get("employment_type")
    out = set()
    for j in jobs.values():
        if et and j.employment_type != et:
            continue
        if (j.job_id in exp.get("job_ids", ()) or j.job_type in exp.get("job_types", ())
                or set(j.perks) & set(exp.get("perks", ())) or set(j.time_bands) & set(exp.get("time_bands", ()))):
            out.add(j.job_id)
    return out


def recall_at_k(top: list[str], relevant: set[str], k: int = K) -> float:
    if not relevant:
        raise ValueError("正解の無い問い合わせは測れない")
    return len(set(top[:k]) & relevant) / min(k, len(relevant))


@contextmanager
def patched(query_vec: Callable[[str], tuple[float, ...] | None], vectors: dict | None = None,
            text_fn: Callable[[BaitoJob], str] | None = None, **logic_params) -> Iterator[None]:
    """logic.recommend を、指定した問い合わせのベクトル・求人ベクトル・求人の文面・選び方の値で動かす。"""
    saved = {"embed_query": embedding.embed_query, "_vectors": embedding._vectors, "job_text": embedding.job_text}
    saved_logic = {k: getattr(logic, k) for k in logic_params}
    embedding.embed_query = query_vec
    if vectors is not None:
        embedding._vectors = lambda: vectors
    if text_fn is not None:
        embedding.job_text = text_fn
    for k, v in logic_params.items():
        setattr(logic, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(embedding, k, v)
        for k, v in saved_logic.items():
            setattr(logic, k, v)


def run_case(state: State, case: dict) -> list[dict]:
    """その問い合わせで recommend が返す上位の求人カード(patched の中で呼ぶ)。"""
    r = logic.recommend(state, None, keywords=case["q"], employment_type=case.get("employment_type"))
    return r.get("jobs", [])


def evaluate(query_vectors: dict[str, list[float]], **patch) -> dict:
    """評価セット全体の再現率@3。query_vectors は問い合わせ文 → ベクトル(記録したもの)。"""
    jobs = load_jobs()
    state = State(Path(tempfile.mkdtemp()) / "state.json")
    state.reset()
    rows = []
    with patched(lambda text: tuple(query_vectors[text]) if text in query_vectors else None, **patch):
        for case in load_cases():
            rel = relevant_ids(case, jobs)
            top = run_case(state, case)
            ids = [c["job_id"] for c in top]
            rows.append({"id": case["id"], "q": case["q"], "employment_type": case.get("employment_type"),
                         "kind": case["kind"], "recall": round(recall_at_k(ids, rel), 3),
                         "top": [(c["job_id"], c["job_type"], c["job_id"] in rel) for c in top]})
    by_kind = {k: round(mean(r["recall"] for r in rows if r["kind"] == k), 3) for k in sorted({r["kind"] for r in rows})}
    return {"recall_at_3": round(mean(r["recall"] for r in rows), 3), "by_kind": by_kind,
            "zero_hits": sum(not r["top"] for r in rows), "cases": rows}


def recorded_query_vectors() -> dict:
    return json.loads(QUERY_VECTORS.read_text(encoding="utf-8"))


def main_offline() -> None:
    rec = recorded_query_vectors()
    rep = evaluate(rec["queries"])
    for r in rep["cases"]:
        print(f"{r['recall']:.2f}  {r['id']:<16} {r['q']}  {[(i, t) for i, t, _ in r['top']]}")
    print(f"model={rec['model']} dims={rec['dimensions']}  recall@3={rep['recall_at_3']}  by_kind={rep['by_kind']}  "
          f"zero_hits={rep['zero_hits']}")


# ----------------------------- 実 API(記録と比較) -----------------------------

class Ledger:
    """このスクリプトで使った実 API のトークンと費用(PRD の予算台帳に載せる)。"""

    def __init__(self) -> None:
        self.tokens: dict[str, int] = {}

    def add(self, model: str, tokens: int) -> None:
        self.tokens[model] = self.tokens.get(model, 0) + tokens

    def usd(self) -> float:
        return sum(PRICE_PER_M[m] * t / 1_000_000 for m, t in self.tokens.items())

    def __str__(self) -> str:
        return f"tokens={self.tokens} cost=${self.usd():.5f}"


def _embed(ledger: Ledger, model: str, dims: int, texts: list[str]) -> list[list[float]]:
    from openai import OpenAI

    resp = OpenAI().embeddings.create(model=model, dimensions=dims, input=texts)
    ledger.add(model, resp.usage.total_tokens)
    return [d.embedding for d in resp.data]


def main_record() -> None:
    """評価セットの問い合わせを、今の設定で記録する(テストと main_offline が使う)。"""
    ledger = Ledger()
    jobs_meta = embedding._vectors()
    model, dims = jobs_meta["model"], jobs_meta["dimensions"]
    qs = sorted({c["q"] for c in load_cases()})
    vecs = _embed(ledger, model, dims, qs)
    out = {"_note": "evals/search_eval.py record が作る(手で編集しない)。求人ベクトルと同じモデル・次元で記録する。",
           "model": model, "dimensions": dims,
           "queries": {q: [round(x, 6) for x in embedding.unit(v)] for q, v in zip(qs, vecs, strict=True)}}
    QUERY_VECTORS.write_text(json.dumps(out, ensure_ascii=False) + "\n")
    print(f"{len(qs)} queries -> {QUERY_VECTORS}  {ledger}")


# 求人を embedding にする文面の候補。v1 は task 28 / 104 の文面(給与・場所・待遇・注意点を全部入れる)。
# core は今の embedding.job_text(仕事の中身だけ)
def _place(j: BaitoJob) -> str:
    return f"{j.area}({j.station.removesuffix('駅')}駅 徒歩{j.walk_min}分)"


def _exp(j: BaitoJob) -> list[str]:
    if j.required_experience or j.preferred_experience:
        return [f"必須: {'・'.join(j.required_experience) or 'なし'} / 歓迎: {'・'.join(j.preferred_experience) or 'なし'}"]
    return []


def _style(j: BaitoJob) -> list[str]:
    if not (j.work_style and j.holidays):
        return []
    w, h = j.work_style, j.holidays
    return [(f"リモート{w.remote}・フレックス{'あり' if w.flex else 'なし'}・残業月{w.overtime_hours}時間 / "
             f"年間休日{h.annual_days}日{'・土日祝休み' if h.weekends_holidays else ''}")]


def text_v1(j: BaitoJob, drawback: bool = True) -> str:
    perks = f"待遇: {'・'.join(j.perks) or 'なし'}"
    tail = [j.description] + ([f"注意点: {j.drawback}"] if drawback else [])
    if j.employment_type != "正社員":
        return "\n".join([j.title, f"{j.employer} / {j.job_type} / {_place(j)}",
                          f"{pay_text(j.pay)} / {'・'.join(j.time_bands)} {j.shift} / 週{j.days_min}日〜", perks, *tail])
    lines = [j.title, f"{j.employer} / 正社員 / {j.job_type} / {_place(j)}", f"{pay_text(j.pay)} / {j.shift}", *_exp(j), *_style(j)]
    return "\n".join([*lines, perks, *tail])


_core = embedding.job_text  # patched で差し替える前の関数を持っておく(差し替え中に自分を呼ばないように)
TEXTS: dict[str, Callable[[BaitoJob], str]] = {
    "v1": text_v1,
    "v1-no-drawback": lambda j: text_v1(j, drawback=False),
    "core": _core,  # 今の文面(task 105 で選んだ)
    "core-no-drawback": lambda j: _core(j).rsplit("\n注意点: ", 1)[0],
    "core-style": lambda j: "\n".join([_core(j), *_style(j)]),  # core に正社員の働き方・休日の1行を足す
}
EMBEDDINGS = [("text-embedding-3-small", 512), ("text-embedding-3-small", 1536),
              ("text-embedding-3-large", 256), ("text-embedding-3-large", 1024), ("text-embedding-3-large", 3072)]
# 選び方: (下限, 最高値からの相対, 類似度の重み)
# 1つ目が task 105 の前の値、最後が選んだ値(logic.SIM_FLOOR・SIM_RELATIVE・SIM_WEIGHT)
SELECTIONS = [(0.30, 0.8, 6), (0.20, 0.8, 6), (0.0, 0.8, 6), (0.0, 0.9, 6), (0.0, 0.7, 6),
              (0.20, 0.8, 12), (0.20, 0.8, 24), (0.20, 0.7, 48), (0.15, 0.7, 24), (0.17, 0.7, 24), (0.18, 0.7, 24),
              (0.20, 0.7, 24)]


def _cached(ledger: Ledger, name: str, model: str, texts: dict[str, str]) -> dict[str, list[float]]:
    """key → 最大の次元のベクトル。data/search_eval/ に置いて、同じ文なら実 API を呼ばない。"""
    path = CACHE / f"{name}.json"
    have = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    todo = [k for k, t in texts.items() if have.get(k, {}).get("text") != t]
    if todo:
        vecs = _embed(ledger, model, FULL_DIMS[model], [texts[k] for k in todo])
        have.update({k: {"text": texts[k], "v": v} for k, v in zip(todo, vecs, strict=True)})
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(have, ensure_ascii=False))
    return {k: have[k]["v"] for k in texts}


def _cut(v: list[float], dims: int) -> list[float]:
    return embedding.unit(v[:dims])


def _table(results: list[dict]) -> str:
    """文面・モデル・次元ごとに、前の選び方と選んだ選び方の再現率を並べた表(PR に貼る)。"""
    head = [f"{f:.2f} / {r} / {w}" for f, r, w in (SELECTIONS[0], SELECTIONS[-1])]
    lines = [f"| モデル | 次元 | 文面 | 前の選び方 ({head[0]}) | 選んだ選び方 ({head[1]}) |", "|---|---|---|---|---|"]
    key = lambda x: (x["model"], x["dims"], x["text"])
    for k in dict.fromkeys(key(x) for x in results):
        cells = []
        for f, r, w in (SELECTIONS[0], SELECTIONS[-1]):
            x = next(x for x in results if key(x) == k and (x["floor"], x["relative"], x["weight"]) == (f, r, w))
            b = x["by_kind"]
            cells.append(f"{x['recall_at_3']:.3f}(否定 {b['否定']:.2f}・経歴 {b['経歴']:.2f}・言い換え {b['言い換え']:.2f}、"
                         f"0件 {x['zero_hits']})")
        lines.append(f"| {k[0].removeprefix('text-embedding-3-')} | {k[1]} | {k[2]} | {cells[0]} | {cells[1]} |")
    return "\n".join(lines) + "\n"


def main_compare() -> None:
    ledger = Ledger()
    jobs = load_jobs()
    qs = sorted({c["q"] for c in load_cases()})
    results = []
    for (model, dims), (tname, tfn) in itertools.product(EMBEDDINGS, TEXTS.items()):
        jv = _cached(ledger, f"jobs-{model}-{tname}", model, {jid: tfn(j) for jid, j in jobs.items()})
        qv = _cached(ledger, f"queries-{model}", model, {q: q for q in qs})
        vectors = {"model": model, "dimensions": dims,
                   "jobs": {jid: {"hash": sha1(tfn(jobs[jid]).encode("utf-8")).hexdigest()[:12], "v": _cut(v, dims)} for jid, v in jv.items()}}
        qvecs = {q: _cut(v, dims) for q, v in qv.items()}
        best = max(sum(a * b for a, b in zip(qvecs[c["q"]], r["v"], strict=True)) for c in load_cases() for r in vectors["jobs"].values())
        for floor, rel, weight in SELECTIONS:
            rep = evaluate(qvecs, vectors=vectors, text_fn=tfn, SIM_FLOOR=floor, SIM_RELATIVE=rel, SIM_WEIGHT=weight)
            row = {"model": model, "dims": dims, "text": tname, "floor": floor, "relative": rel, "weight": weight,
                   "recall_at_3": rep["recall_at_3"], "by_kind": rep["by_kind"], "zero_hits": rep["zero_hits"],
                   "max_sim": round(best, 3), "cases": rep["cases"]}
            results.append(row)
            print(f"{model:<24}{dims:>5} {tname:<17} floor={floor:.2f} rel={rel} w={weight:<3} "
                  f"recall@3={rep['recall_at_3']:.3f} {rep['by_kind']} zero={rep['zero_hits']}", flush=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "compare.md").write_text(_table(results))
    out = CACHE / "compare.json"
    out.write_text(json.dumps({"ledger": ledger.tokens, "usd": ledger.usd(), "results": results}, ensure_ascii=False, indent=1))
    print(f"-> {out}  {ledger}")


if __name__ == "__main__":
    from line_runtime.config import load_env

    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["eval", "record", "compare"], default="eval")
    cmd = ap.parse_args().cmd
    {"eval": main_offline, "record": main_record, "compare": main_compare}[cmd]()
