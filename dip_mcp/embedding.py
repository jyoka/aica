"""求人の意味検索(embedding)。keywords の言い換え(「人と話さない」→ 倉庫の黙々作業など)を拾う。

dip 側ツールの方針(LLM を使わず、速く・決定的に・事実だけを返す)との折り合い:
- 文章は作らない(ベクトルの近さで求人を選び直すだけ。返す中身は今までどおり求人の事実)。
- 求人側のベクトルは scripts/gen_job_vectors.py で前もって作り fixtures/job_vectors.json に保存する。
  検索時の外部 API 呼び出しは、問い合わせ文の embedding 1回だけ(同じ問い合わせはプロセス内で使い回す)。
- API が使えない(キーなし・失敗・タイムアウト)ときは embed_query が None を返し、呼び出し側
  (logic.recommend)は従来の部分一致に戻す(検索を止めない)。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from functools import lru_cache
from hashlib import sha1

from dip_mcp.data import FIXTURES, BaitoJob

VECTORS_PATH = FIXTURES / "job_vectors.json"
# text-embedding-3 系は dimensions で次元を縮められる(公式ドキュメントで 2026-09-26 に確認)。
# 求人63件なら 512 次元で十分に見分けられ、ファイルも小さい(約300KB)。
MODEL = "text-embedding-3-small"
DIMENSIONS = 512
TIMEOUT_S = 5.0  # 検索を待たせない。超えたら部分一致に戻す


def job_text(job: BaitoJob) -> str:
    """求人1件をベクトルにする文。作る側(gen_job_vectors)と使う側(古さの照合)で共通にする。"""
    return (f"{job.title}\n{job.employer} / {job.job_type} / {job.area}({job.station.removesuffix('駅')}駅 徒歩{job.walk_min}分)\n"
            f"時給{job.hourly_wage}円 / {'・'.join(job.time_bands)} {job.shift} / 週{job.days_min}日〜\n"
            f"待遇: {'・'.join(job.perks) or 'なし'}\n{job.description}\n注意点: {job.drawback}")


def job_hash(job: BaitoJob) -> str:
    """求人の文の指紋。求人データを変えたのにベクトルを作り直していないことを見つけるため。"""
    return sha1(job_text(job).encode("utf-8")).hexdigest()[:12]


def unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


@lru_cache(maxsize=1)
def _vectors() -> dict:
    """保存済みの求人ベクトル。無ければ空(意味検索なし)。fixtures は実行中に変わらないので1回だけ読む。"""
    if not VECTORS_PATH.is_file():
        return {}
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def _api_embed(model: str, dimensions: int, text: str) -> list[float]:
    """OpenAI embeddings API を1回呼ぶ(テストではここを差し替える)。"""
    from openai import OpenAI

    client = OpenAI(timeout=TIMEOUT_S, max_retries=1)
    return client.embeddings.create(model=model, dimensions=dimensions, input=text).data[0].embedding


_query_cache: dict[str, tuple[float, ...]] = {}


def embed_query(text: str) -> tuple[float, ...] | None:
    """問い合わせ文のベクトル。求人ベクトルが無い・API が使えないときは None(呼び出し側は部分一致へ)。
    成功した問い合わせだけを覚える(一時的な失敗を覚えて意味検索が戻らなくなることを避ける)。"""
    meta = _vectors()
    if not meta.get("jobs"):
        return None
    if text in _query_cache:
        return _query_cache[text]
    try:
        vec = tuple(unit(_api_embed(meta["model"], meta["dimensions"], text)))
    except Exception:  # noqa: BLE001  失敗の種類を問わず検索は止めない
        return None
    if len(_query_cache) >= 512:
        _query_cache.clear()
    _query_cache[text] = vec
    return vec


def similarities(query_vec: tuple[float, ...], jobs: Iterable[BaitoJob]) -> dict[str, float]:
    """job_id → 問い合わせとのコサイン類似度(ベクトルは正規化済みなので内積)。
    ベクトルが無い・求人データが変わって古い求人は載せない(その求人は部分一致だけで見る)。"""
    stored = _vectors().get("jobs", {})
    out = {}
    for job in jobs:
        rec = stored.get(job.job_id)
        if rec and rec["hash"] == job_hash(job):
            out[job.job_id] = sum(a * b for a, b in zip(query_vec, rec["v"], strict=True))
    return out
