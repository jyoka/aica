"""シミュレーション用の dip の行動の元データ(会員とイベントのログ)を作る。LLM は使わない。

乱数の種を固定するので、同じ引数なら同じデータになる。求人は fixtures の求人(data.load_jobs)を使う。
人ごとに、よく見る職種・エリア・勤務の時間帯、サイトを開く時刻、訪問の頻度、保存・応募・離脱のしやすさに偏りを持たせる。
期間は end(既定 SIM_NOW)までの months × 30 日。シナリオのユーザー(fixtures/dip_events.json)も既定で入れる。

    ./.venv/bin/python -m scripts.gen_dip_events [--members 1000] [--months 6] [--seed 0] [--replace]
"""

from __future__ import annotations

import argparse
import math
import random
import time
from datetime import datetime, timedelta
from pathlib import Path

from dip_mcp.data import SIM_NOW, BaitoJob, load_jobs
from dip_mcp.events import (
    EVENTS_PATH,
    EventStore,
    aware_time,
    iso,
    load_scenario_events,
)

DAYS_PER_MONTH = 30
# 応募フォームの項目(離脱したときに、どこまで入力したかを項目名で持つ)
FORM_FIELDS = ["氏名", "年齢", "電話番号", "メールアドレス", "希望開始日", "希望シフト"]
APPLIED = "応募済み(店舗からの連絡待ち)"
# 選考の状態の変化。(次の状態, 重み)。次が無い状態で終わる
STATUS_NEXT = {
    APPLIED: [("面接の日程調整中", 6), ("不採用(書類選考)", 2), (None, 2)],
    "面接の日程調整中": [("面接済み(結果待ち)", 8), ("辞退", 2)],
    "面接済み(結果待ち)": [("採用", 5), ("不採用(面接)", 5)],
}
# サイトを開く時刻の山(時)。朝・昼休み・帰り道・夜・深夜
ACTIVE_HOURS = [(7.5, 1), (12.5, 2), (18.5, 3), (21.5, 4), (24.5, 2)]


def _person(rng: random.Random, jobs: list[BaitoJob], start: datetime, end: datetime) -> dict:
    """1 人の偏り。どの区分の求人を見るか、好きな職種・エリア・時間帯、頻度と行動のしやすさ。"""
    track = rng.choices(["アルバイト・パート", "正社員", None], weights=[7, 2, 1])[0]
    pool = [j for j in jobs if track is None or j.employment_type == track]
    types = rng.sample(sorted({j.job_type for j in pool}), rng.choice([1, 1, 2]))
    areas = rng.sample(sorted({j.area for j in pool}), rng.choice([1, 2, 2]))
    band_names = sorted({b for j in pool for b in j.time_bands})  # 正社員の求人には勤務の時間帯が無い
    bands = rng.sample(band_names, min(len(band_names), rng.choice([1, 2])))
    stray = rng.uniform(0.02, 0.3)  # 好みに合わない求人をどれだけ見るか(小さいほど好みの職種・エリアに偏る)
    weights = [stray + 6 * (j.job_type in types) + 3 * (j.area in areas) + 1 * bool(set(j.time_bands) & set(bands))
               for j in pool]
    span = (end - start).days
    registered = (start - timedelta(days=rng.uniform(0, 365)) if rng.random() < 0.7
                  else start + timedelta(days=rng.uniform(0, span)))
    stop = end if rng.random() < 0.8 else registered + timedelta(days=rng.uniform(7, span))
    return {
        "pool": pool, "cum": _cumulative(weights), "registered": registered, "stop": min(stop, end),
        "hour": rng.choices([h for h, _ in ACTIVE_HOURS], weights=[w for _, w in ACTIVE_HOURS])[0],
        "per_week": min(10.0, max(0.1, rng.lognormvariate(0.3, 0.9))),
        "views": rng.choice([1, 2, 3, 4, 5]),
        "save": rng.uniform(0, 0.15),
        "apply": 0.0 if rng.random() < 0.5 else rng.uniform(0.01, 0.08),
        "exit": rng.uniform(0.2, 0.6),
        "inquiry": rng.uniform(0, 0.02),
    }


def _cumulative(weights: list[float]) -> list[float]:
    out, total = [], 0.0
    for w in weights:
        total += w
        out.append(total)
    return out


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, rng.random()
    while p > limit:
        k += 1
        p *= rng.random()
    return k


def member_events(rng: random.Random, dip_id: str, p: dict, start: datetime, end: datetime) -> list[tuple]:
    """1 人のイベント(dip_id, kind, at, job_id, data)。end より後の出来事は作らない。"""
    out: list[tuple] = []

    def add(kind: str, at: datetime, job_id: str | None = None, **data) -> bool:
        if at > end:
            return False
        out.append((dip_id, kind, at, job_id, data))
        return True

    def progress(job_id: str, at: datetime) -> None:
        status = APPLIED
        while nxt := STATUS_NEXT.get(status):
            status = rng.choices([s for s, _ in nxt], weights=[w for _, w in nxt])[0]
            at += timedelta(days=rng.uniform(1, 7))
            if status is None or not add("status", at, job_id, status=status):
                return

    saved, applied = set(), set()
    first = max(start, p["registered"])
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < p["stop"]:
        for _ in range(_poisson(rng, p["per_week"] / 7)):
            hour = rng.gauss(p["hour"], 1.5) % 24
            at = day + timedelta(hours=hour)
            if at < first or not add("visit", at):
                continue
            for _ in range(rng.randint(1, p["views"] + 1)):
                at += timedelta(minutes=rng.uniform(0.5, 6))
                job = rng.choices(p["pool"], cum_weights=p["cum"])[0]
                if not add("view", at, job.job_id):
                    break
                if job.job_id not in saved and rng.random() < p["save"]:
                    at += timedelta(seconds=rng.uniform(5, 90))
                    saved.add(job.job_id)
                    add("save", at, job.job_id)
                if rng.random() < p["inquiry"]:
                    at += timedelta(minutes=rng.uniform(1, 5))
                    add("inquiry", at, job.job_id, topic=rng.choice(["シフト", "仕事内容", "面接", "給与"]))
                if job.job_id in applied or rng.random() >= p["apply"]:
                    continue
                at += timedelta(minutes=rng.uniform(1, 8))
                if rng.random() < p["exit"]:
                    cut = rng.randint(1, len(FORM_FIELDS) - 1)
                    if not add("form_exit", at, job.job_id, completed=FORM_FIELDS[:cut], remaining=FORM_FIELDS[cut:]):
                        break
                    if rng.random() < 0.5:
                        add("contact", at + timedelta(hours=rng.uniform(1, 3)), channel="LINE AI通知",
                            message="form_exit")
                    if rng.random() >= 0.3:
                        continue
                    later = at + timedelta(hours=rng.uniform(1, 72))  # 後でフォームの続きから応募する
                    if add("apply", later, job.job_id, status=APPLIED):
                        applied.add(job.job_id)
                        progress(job.job_id, later)
                elif add("apply", at, job.job_id, status=APPLIED):
                    applied.add(job.job_id)
                    progress(job.job_id, at)
        day += timedelta(days=1)
    return out


def generate(store: EventStore, members: int, months: int, seed: int = 0, end: datetime = SIM_NOW) -> int:
    """members 人 × months か月分の会員とイベントを store に足し、イベントの数を返す。
    人ごとの乱数は種と人の番号から作るので、人数を変えても同じ番号の人は同じ行動になる。"""
    start = end - timedelta(days=DAYS_PER_MONTH * months)
    jobs = sorted(load_jobs().values(), key=lambda j: j.job_id)
    people, events = [], []
    for i in range(members):
        rng = random.Random(f"{seed}:{i}")
        dip_id = f"D{100000 + i}"
        p = _person(rng, jobs, start, end)
        people.append((dip_id, p["registered"]))
        events += member_events(rng, dip_id, p, start, end)
    store.add_members(people)
    rows = [(d, k, iso(at), j, data) for d, k, at, j, data in events]
    rows.sort(key=lambda e: (e[2], e[0]))  # ログは時刻の順に追記されたものとして番号を振る
    store.extend(rows)
    return len(rows)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="シミュレーション用の dip の行動の元データを作る")
    p.add_argument("--members", type=int, default=1000, help="会員の数")
    p.add_argument("--months", type=int, default=6, help="期間(1 か月 = 30 日)")
    p.add_argument("--seed", type=int, default=0, help="乱数の種")
    p.add_argument("--end", type=aware_time, default=SIM_NOW, help="期間の終わり(タイムゾーンつき。既定 SIM_NOW)")
    p.add_argument("--events", type=Path, default=EVENTS_PATH, help="書き出す SQLite")
    p.add_argument("--no-scenarios", action="store_true", help="シナリオのユーザーを入れない")
    p.add_argument("--replace", action="store_true", help="同じファイルがあれば消して作り直す")
    a = p.parse_args(argv)
    if a.events.exists():
        if not a.replace:
            p.error(f"{a.events} はもうあります(イベントは追記だけなので、作り直すなら --replace)")
        a.events.unlink()
    started = time.perf_counter()
    store = EventStore(a.events)
    if not a.no_scenarios:
        load_scenario_events(store)
    n = generate(store, a.members, a.months, a.seed, a.end)
    print(f"{a.members} 人 × {a.months} か月、イベント {n} 件を {a.events} に書きました"
          f"({time.perf_counter() - started:.1f} 秒)")


if __name__ == "__main__":
    main()
