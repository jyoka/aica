"""求人検索(recommend_jobs の keywords と、属性・働き方・休日・年収の引数)の質を、正解つきの評価セットの上位3件の再現率で測る。

模擬ユーザーも LINE 側 LLM も使わず、dip のツールの中身(logic.recommend)を直接呼ぶ。
評価セットは evals/search_eval_set.json(問い合わせごとに、エージェントが渡す引数と、正解の求人・職種を決めてある)。

再現率@3 = 上位3件のうち正解の件数 ÷ min(3, 正解の件数)。正解が1件の問い合わせは、それが3位以内なら 1.0。
無関係な問い合わせ(kind が 無関係。「宇宙飛行士になりたい」など)は正解を持たず、0件か、keywords の点を除いた傾向の点の
順に並んでいれば合格とする(近いだけの求人が職種の傾向より上に並ばない)。再現率の平均には入れない。

    # オフライン(実 API なし): 保存済みの求人ベクトルと、記録した問い合わせのベクトルで今の設定を測る
    ./.venv/bin/python -m evals.search_eval
    # 記録の無い問い合わせのベクトルを今の設定(embedding.MODEL・DIMENSIONS)で足す(実 API、$0.0001 未満。費用は
    # evals/budget.jsonl に1行足す)。--all なら全部を記録し直す
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
from evals import budget

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


UNRELATED = "無関係"


def case_keywords(case: dict) -> str | None:
    """keywords に渡す文。省略なら本人の言葉(q)のまま、null なら渡さない(引数だけで探す)。"""
    return case.get("keywords", case["q"])


def _annual_mid(j: BaitoJob) -> float | None:
    """年収の幅の中央値(円。月給は12か月分)。logic とは別に計算する(絞り込みの実装をそのまま正解にしないため)。"""
    per_year = {"年収": 1, "月給": 12}.get(j.pay.kind)
    return None if per_year is None else (j.pay.min + (j.pay.max or j.pay.min)) / 2 * per_year


def _style_hit(j: BaitoJob, exp: dict) -> bool:
    w, h = j.work_style, j.holidays
    return bool((w and w.remote in exp.get("remote", ()))
                or (exp.get("weekends_holidays") and h and h.weekends_holidays)
                or ("max_overtime_hours" in exp and w and w.overtime_hours <= exp["max_overtime_hours"])
                or ("min_annual_salary" in exp and (_annual_mid(j) or 0) >= exp["min_annual_salary"] * 10000))


def relevant_ids(case: dict, jobs: dict[str, BaitoJob]) -> set[str]:
    """その問い合わせの正解の求人。雇用区分を指定した問い合わせは、その区分の求人だけ。"""
    exp, et = case["expected"], case.get("employment_type")
    out = set()
    for j in jobs.values():
        if et and j.employment_type != et:
            continue
        if (j.job_id in exp.get("job_ids", ()) or j.job_type in exp.get("job_types", ())
                or set(j.perks) & set(exp.get("perks", ())) or set(j.time_bands) & set(exp.get("time_bands", ()))
                or _style_hit(j, exp)):
            out.add(j.job_id)
    return out


def unrelated_ok(top: list[dict]) -> bool:
    """無関係な問い合わせの合否。0件か、keywords の点を除いた点(職種の傾向・駅・給与など)の高い順に並んでいれば合格。
    keywords の点で、傾向の点の低い求人が上に来ていたら不合格(近いだけの求人が傾向より上に並んだ)。"""
    base = [c["match_score"] - c["score_breakdown"].get("keywords", 0.0) for c in top]
    return all(a >= b - 1e-9 for a, b in itertools.pairwise(base))


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
    """その問い合わせで recommend が返す上位の求人カード(patched の中で呼ぶ)。args は会話の指示どおりの引数、
    user は傾向を使うシナリオ用ユーザー(省略なら傾向なし)。"""
    r = logic.recommend(state, case.get("user"), keywords=case_keywords(case),
                        employment_type=case.get("employment_type"), **case.get("args", {}))
    return r.get("jobs", [])


def evaluate(query_vectors: dict[str, list[float]], **patch) -> dict:
    """評価セット全体の再現率@3。query_vectors は問い合わせ文 → ベクトル(記録したもの)。"""
    jobs = load_jobs()
    state = State(Path(tempfile.mkdtemp()) / "state.json")
    state.reset()
    rows = []
    with patched(lambda text: tuple(query_vectors[text]) if text in query_vectors else None, **patch):
        for case in load_cases():
            top = run_case(state, case)
            row = {"id": case["id"], "q": case["q"], "employment_type": case.get("employment_type"),
                   "kind": case["kind"], "added_in": case.get("added_in")}
            if case["kind"] == UNRELATED:
                rows.append(row | {"recall": None, "ok": unrelated_ok(top), "top": [(c["job_id"], c["job_type"], None) for c in top]})
                continue
            rel = relevant_ids(case, jobs)
            ids = [c["job_id"] for c in top]
            rows.append(row | {"recall": round(recall_at_k(ids, rel), 3),
                               "top": [(c["job_id"], c["job_type"], c["job_id"] in rel) for c in top]})
    scored = [r for r in rows if r["recall"] is not None]
    unrelated = [r for r in rows if r["recall"] is None]
    by_kind = {k: round(mean(r["recall"] for r in scored if r["kind"] == k), 3) for k in sorted({r["kind"] for r in scored})}
    return {"recall_at_3": round(mean(r["recall"] for r in scored), 3),
            # task 105 の評価セットにあった問い合わせだけの再現率(task 111 で足した問い合わせを除く。前の値と比べる用)
            "recall_at_3_task105": round(mean(r["recall"] for r in scored if not r["added_in"]), 3),
            "by_kind": by_kind, "zero_hits": sum(not r["top"] for r in scored),
            "unrelated_ok": f"{sum(r['ok'] for r in unrelated)}/{len(unrelated)}", "cases": rows}


def recorded_query_vectors() -> dict:
    return json.loads(QUERY_VECTORS.read_text(encoding="utf-8"))


def main_offline() -> None:
    rec = recorded_query_vectors()
    rep = evaluate(rec["queries"])
    for r in rep["cases"]:
        score = f"{r['recall']:.2f}" if r["recall"] is not None else ("ok  " if r["ok"] else "NG  ")
        print(f"{score}  {r['id']:<16} {r['q']}  {[(i, t) for i, t, _ in r['top']]}")
    print(f"model={rec['model']} dims={rec['dimensions']}  recall@3={rep['recall_at_3']}  "
          f"(task 105 の問い合わせだけ {rep['recall_at_3_task105']})  by_kind={rep['by_kind']}  "
          f"zero_hits={rep['zero_hits']}  無関係の合格={rep['unrelated_ok']}")


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


def eval_queries() -> set[str]:
    """評価セットで keywords に渡す文(ベクトルを記録しておくもの)。"""
    return {kw for c in load_cases() if (kw := case_keywords(c))}


def main_record(rerecord_all: bool = False) -> None:
    """評価セットの問い合わせのベクトルを、今の設定で記録する(テストと main_offline が使う)。
    記録済みの問い合わせは、そのまま残す(記録し直すと、同じ文でも値がわずかに揺れて前の結果と比べにくくなるため)。"""
    ledger = Ledger()
    jobs_meta = embedding._vectors()
    model, dims = jobs_meta["model"], jobs_meta["dimensions"]
    have = {}
    if QUERY_VECTORS.is_file() and not rerecord_all:
        rec = recorded_query_vectors()
        if (rec["model"], rec["dimensions"]) == (model, dims):
            have = rec["queries"]
    qs = sorted(eval_queries() - set(have))
    vecs = _embed(ledger, model, dims, qs) if qs else []
    queries = have | {q: [round(x, 6) for x in embedding.unit(v)] for q, v in zip(qs, vecs, strict=True)}
    out = {"_note": "evals/search_eval.py record が作る(手で編集しない)。求人ベクトルと同じモデル・次元で記録する。",
           "model": model, "dimensions": dims, "queries": dict(sorted(queries.items()))}
    QUERY_VECTORS.write_text(json.dumps(out, ensure_ascii=False) + "\n")
    if qs:
        budget.record(budget.LEDGER, "search-eval-record", ledger.usd(), case=f"検索評価の問い合わせ {len(qs)} 件の embedding",
                      tokens=ledger.tokens)
    print(f"{len(qs)} new queries (all {len(queries)}) -> {QUERY_VECTORS}  {ledger}")


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
    qs = sorted(eval_queries())
    results = []
    for (model, dims), (tname, tfn) in itertools.product(EMBEDDINGS, TEXTS.items()):
        jv = _cached(ledger, f"jobs-{model}-{tname}", model, {jid: tfn(j) for jid, j in jobs.items()})
        qv = _cached(ledger, f"queries-{model}", model, {q: q for q in qs})
        vectors = {"model": model, "dimensions": dims,
                   "jobs": {jid: {"hash": sha1(tfn(jobs[jid]).encode("utf-8")).hexdigest()[:12], "v": _cut(v, dims)} for jid, v in jv.items()}}
        qvecs = {q: _cut(v, dims) for q, v in qv.items()}
        best = max(sum(a * b for a, b in zip(qvecs[q], r["v"], strict=True)) for q in qs for r in vectors["jobs"].values())
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
    ap.add_argument("--all", action="store_true", help="record で、記録済みの問い合わせも記録し直す")
    a = ap.parse_args()
    {"eval": main_offline, "record": lambda: main_record(a.all), "compare": main_compare}[a.cmd]()
