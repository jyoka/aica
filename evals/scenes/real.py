"""場面の一覧を実モデルで回して、今の作りと新しい作りを比べる(設計メモ docs/design/conversation-layer.md §5 の 9 枚目。費用がかかる)。

    ./.venv/bin/python -m evals.scenes --real --entry career --version c1 --tag career-c1-r1 \\
        --ledger evals/budget-conversation-layer.jsonl --limit-usd 21
    ./.venv/bin/python -m evals.scenes --real --entry career --version c2 --version baito=v5 --read-model claude-haiku-5-5 ...
    ./.venv/bin/python -m evals.scenes.real summary evals/results/scenes-real-*.jsonl   # 点の表(費用 0)

回し方は台本の LLM の回(runner.py)と同じで、モデルだけを実モデルにする:
- career と baito: dip の相談のツールの呼び直しで、ハーネスが求めた段(読む、言う、まとめ、結果)を実モデルで呼ぶ。メッセージは
  ランタイム(line_runtime.runtime._harness_turn)と同じ: 本人の発言(ハーネスが伏せたもの model_text があればそちら)と、
  画面に出た直前のやりとり(場面の前の会話 history と、その場面の返信。ハーネスの覚え model_history があればそちら)。
  モデルとスキーマもランタイムと同じ(line_runtime.config.MODEL、baito_chat / career_chat の SCHEMAS、思考なし)。
  read_model を渡すと、読む段(read、extract)だけをそのモデルにする(設計メモ §4.14 の Haiku 5.5 の案)
- 呼び出しの失敗(通信、形の崩れた出力)は、ランタイムと同じく失敗の印として渡す(ハーネスが 1 回と数える)
- 本人がやめて相談を閉じ、トークの AI がそのターンを受けたときは、台本の回と同じく判定しない(トークの AI を回さない)。
  採点の条件も、そのターンの返信を見るものは判定しない(skip)
- talk: 本物のランタイムを、本番と同じ実モデルのトークの AI で回す(ツールも本物の dip の MCP)。どのコードの条件も判定する
- 採点の条件は、実モデルのジャッジ(evals/scenes/judge.py。evals.run の JUDGE_MODEL)で判定する。1 場面 1 回の呼び出し
- 費用: evals/meter.py で全部の呼び出し(ハーネス、トークの AI、ジャッジ)を数え、1 場面ごとに台帳へ 1 行足す。
  場面を始める前に累計が上限に達していたら止める(talk はまとめて回すので、始める前に 1 回だけ見る)
- 速さ: ターンごとに、ハーネスのモデルの呼び出しの時間の合計(model_seconds)と数、段ごとの時間(stages)を残す。
  talk はランタイムの 1 ターンの時間(turn_seconds)
- 結果は 1 場面 1 行の JSONL(evals/results/scenes-real-<tag>.jsonl)。途中で止めても残る
- rejudge: 事実の条件(answers_from_facts、no_invented_facts)だけを、入り口の事実のデータ(facts.json。言う段に渡す事実の全部)を
  ジャッジに渡して判定し直す(場面の facts はその一部なので、facts.json にある正しい答えが「事実に無い」になる。task 362 で見つけた)。
  返信は変えない(モデルの回し直しはしない)。どの版にも同じ事実を渡す

    ./.venv/bin/python -m evals.scenes.real rejudge --ledger evals/budget-conversation-layer.jsonl --limit-usd 21 \
        --out evals/results/scenes-real-facts.jsonl evals/results/scenes-real-*-r?.jsonl
- touch: ペルソナの会話(正社員の evals.fulltime_hearing.run と、アルバイト・パートの evals.baito_sim --real の結果)の、
  相づちのあるターンのうち personal_touch(本人の今の言葉に合わせた一言)の割合(設計メモ §4.10 の面白さの線は 50% 以上)。
  1 会話 1 回のジャッジで、AI の返信ごとに「相づちがあるか」「本人の言葉に合わせたか」を判定する

    ./.venv/bin/python -m evals.scenes.real touch --ledger evals/budget-conversation-layer.jsonl --limit-usd 21 \
        evals/results/fulltime-hearing-*.json evals/results/baito-p3-*.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from evals import budget
from evals import scenes as S
from evals.scenes import checks as K
from evals.scenes import judge as J
from evals.scenes import runner as R

HERE = Path(__file__).resolve().parent.parent
LEDGER = HERE / "budget-conversation-layer.jsonl"
LIMIT_USD = 21.0  # 運営者が決めた上限(設計メモ §6 の 4)
STRICT_JUDGE = ("no_invented_facts", "no_falsify", "no_advice_beyond")  # 採点の条件で 100% が要るもの(§4.10)
SWITCH_CHECKS = ("switches_to", "stays")  # 入り口をまたぐ場面で 100% が要るもの(と、移った先の not_asks)
LINES = {"code": 0.95, "judge": 0.85}  # §4.10 の合格の線
NEW = {"career": "c2", "baito": "v5", "talk": "t1"}  # 新しい作りの版
FACT_CHECKS = ("answers_from_facts", "no_invented_facts")  # rejudge で判定し直す条件


def handed(scene: dict, replies: list[dict], c: dict) -> bool:
    """条件 c の見る返信に、相談を閉じてトークの AI が受けたターン(返事を書いていない)があるか(career と baito の場面だけ)。"""
    return scene["entry"] != "talk" and any(replies[i]["how"] == "talk" for i in K.indices(c, len(replies)))


class RealModel:
    """ハーネスの段を実モデルで呼ぶ(同期。場面の回し方の driver.real)。1 つのイベントループで呼ぶ(クライアントを使い回すため)。"""

    def __init__(self, meter, read_model: str | None = None):
        from line_runtime import config

        self.meter, self.model, self.read_model = meter, config.MODEL, read_model
        self.loop = asyncio.new_event_loop()
        self._client = None
        self._turn: dict | None = None

    def client(self):
        from line_runtime import llm

        self._client = self._client or llm.client()
        return self._client

    def model_for(self, stage: str) -> str:
        return self.read_model if self.read_model and stage in ("read", "extract") else self.model

    async def _parse(self, client, entry: str, stage: str, system: str, messages: list[dict]) -> tuple[dict, dict]:
        """ランタイムの既定のモデル(line_runtime.baito_chat.default_llm)と同じ呼び方。段のモデルだけを選べる。"""
        from line_runtime import baito_chat, career_chat, llm

        schemas = career_chat.SCHEMAS if entry == "career" else baito_chat.SCHEMAS
        out, usage = await llm.parse(client, model=self.model_for(stage), system=system, messages=messages, schema=schemas[stage],
                                     max_tokens=baito_chat.MAX_TOKENS, effort="none", with_usage=True)
        if out is None:
            raise baito_chat.BaitoLLMError(usage)
        return out.model_dump(), usage

    def begin(self) -> None:
        self._turn = {"model_calls": 0, "model_seconds": 0.0, "stages": []}

    def end(self) -> dict:
        t, self._turn = self._turn or {"model_calls": 0, "model_seconds": 0.0, "stages": []}, None
        return t | {"model_seconds": round(t["model_seconds"], 3)}

    def harness(self, entry: str, text: str, convo: list[dict]):
        """1 ターンの呼び直しで使う call -> 出力。メッセージはランタイムの _harness_turn と同じ。"""
        from line_runtime import baito_chat

        def call(c: dict) -> dict:
            said = c.get("model_text", text)
            past = baito_chat.recent(c["model_history"]) if "model_history" in c else baito_chat.recent(convo)
            msgs = baito_chat.messages_for(c["stage"], said, past)
            t0 = time.perf_counter()
            ok = False
            try:
                out, _ = self.loop.run_until_complete(self._parse(self.client(), entry, c["stage"], c["system"], msgs))
                ok = True
                return out
            finally:
                took = time.perf_counter() - t0
                if self._turn is not None:
                    self._turn["model_calls"] += 1
                    self._turn["model_seconds"] += took
                    self._turn["stages"].append({"stage": c["stage"], "model": self.model_for(c["stage"]),
                                                 "seconds": round(took, 3), "ok": ok})
        return call

    def async_harness(self, entry: str):
        """ランタイム(talk の場面)に渡すハーネスのモデル(line_runtime.baito_chat.BaitoLLM の形)。クライアントはそのイベントループで作る。"""
        client = None

        async def call(stage: str, system: str, messages: list[dict]) -> tuple[dict, dict]:
            nonlocal client
            from line_runtime import llm

            client = client or llm.client()
            return await self._parse(client, entry, stage, system, messages)
        return call

    def calls(self) -> int:
        return self.meter.mark()

    def judge(self, model: str):
        """runner.score に渡すジャッジ。トークの AI が受けたターン(how=talk。返事を書いていない)の返信を見る条件は skip。"""
        def run(scene: dict, record: dict, checks: list[dict]) -> list[dict]:
            handed_ = [handed(scene, record["replies"], c) for c in checks]
            asked = [c for c, h in zip(checks, handed_, strict=True) if not h]
            got = iter(self.loop.run_until_complete(J.judge(self.client(), model, scene, record, asked)) if asked else [])
            out = []
            for c, h in zip(checks, handed_, strict=True):
                if h:
                    out.append({"check": c["check"], "at": c.get("at"), "args": {k: v for k, v in c.items() if k not in ("check", "at")},
                                "status": K.SKIP, "detail": "トークの AI が受けたターン(実モデルの回でもトークの AI を回さない)",
                                "judge": True})
                else:
                    out.append(next(got))
            return out
        return run

    def close(self) -> None:
        self.loop.close()


def _slim(row: dict) -> dict:
    """保存する行: 返信から、状態の写し(states)を除く(大きいので。条件の判定は済んでいる)。"""
    return row | {"replies": [{k: v for k, v in r.items() if k not in ("states",)} for r in row["replies"]]}


def run(entries: list[str] | None, versions: dict | None, ids: list[str] | None, repeat: int, ledger: Path, limit: float,
        read_model: str | None, tag: str, out_dir: Path = HERE / "results", judge: bool = True, log=print) -> tuple[Path, bool]:
    """実モデルで場面を回して判定し、1 場面 1 行を JSONL に書く。返すのは (結果のファイル、上限で止めたか)。"""
    from evals.meter import Meter
    from evals.run import JUDGE_MODEL
    from line_runtime import config

    budget.check(ledger, limit)
    meter = Meter().install()
    real = RealModel(meter, read_model)
    judge_fn = real.judge(JUDGE_MODEL[config.PROVIDER]) if judge else None
    scenes = [s for s in S.load_scenes() if (not entries or s["entry"] in entries) and (not ids or s["id"] in ids)]
    out = Path(out_dir) / f"scenes-real-{tag}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    stopped = False
    log(budget.line(ledger, limit))

    def lost(scene: dict, cost: float, rep: int, e: Exception) -> None:
        """場面の判定(ジャッジ)が例外で止まったとき: 使った費用だけを台帳に残して、次の場面へ進む。"""
        budget.record(ledger, "scenes", cost, tag=tag, entry=scene["entry"], scene=scene["id"], rep=rep, read_model=read_model,
                      error=f"{type(e).__name__}: {e}")
        log(f"{tag} rep={rep} {scene['entry']} {scene['id']}: ${cost:.4f}  判定できなかった {type(e).__name__}: {e}")

    def keep(row: dict, cost: float, rep: int) -> None:
        row = _slim(row) | {"rep": rep, "read_model": read_model, "tag": tag, "cost_usd": round(cost, 5)}
        with open(out, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        budget.record(ledger, "scenes", cost, tag=tag, entry=row["entry"], version=row["version"], scene=row["scene"], rep=rep,
                      read_model=read_model)
        bad = [x["check"] for x in row["results"] if x["status"] == K.FAIL]
        log(f"{tag} rep={rep} {row['entry']} {row['version']} {row['scene']}: ${cost:.4f}"
            + (f"  不合格 {', '.join(bad)}" if bad else "") + (f"  エラー {row['error']}" if row["error"] else ""))

    try:
        with tempfile.TemporaryDirectory(prefix="scenes-real-") as tmp:
            for rep in range(repeat):
                path = Path(tmp) / f"state-{rep}.json"  # 回ごとに新しい dip の状態(場面の本人の id は回によらず同じなので)
                for entry in S.ENTRIES:
                    mine = [s for s in scenes if s["entry"] == entry]
                    if not mine or stopped:
                        continue
                    d = R.driver(entry, (versions or {}).get(entry))
                    others = {k: v for e, ver in (versions or {}).items() if e not in (entry, "talk")
                              for k, v in R.driver(e, ver).flags().items()}
                    if entry == "talk":
                        try:
                            budget.check(ledger, limit)
                        except budget.BudgetExceeded as e:
                            log(f"!! {e}")
                            stopped = True
                            continue
                        marks: list[int] = []

                        def mark(_scene, marks=marks):
                            marks.append(meter.mark())
                        records = R.run_talk_scenes(mine, d, path, real=real, before_scene=mark)
                        marks.append(meter.mark())
                        for i, (s, rec) in enumerate(zip(mine, records, strict=True)):
                            run_cost = sum(c.cost for c in meter.calls[marks[i]:marks[i + 1]])
                            m = meter.mark()
                            try:
                                row = R.score(s, rec, judge_fn)
                            except Exception as e:  # noqa: BLE001  1 場面の判定の失敗で止めない(費用は残す)
                                lost(s, run_cost + meter.summary(m)["cost_usd"], rep, e)
                                continue
                            keep(row, run_cost + meter.summary(m)["cost_usd"], rep)
                        continue
                    d.real = real
                    for s in mine:
                        try:
                            budget.check(ledger, limit)
                        except budget.BudgetExceeded as e:
                            log(f"!! {e}")
                            stopped = True
                            break
                        m = meter.mark()
                        rec = R.run_harness_scene(s, d, path, others)
                        try:
                            row = R.score(s, rec, judge_fn)
                        except Exception as e:  # noqa: BLE001  1 場面の判定の失敗で止めない(費用は残す)
                            lost(s, meter.summary(m)["cost_usd"], rep, e)
                            continue
                        keep(row, meter.summary(m)["cost_usd"], rep)
    finally:
        real.close()
    log(f"この実行 ${meter.summary()['cost_usd']:.4f} / {budget.line(ledger, limit)}")
    log(f"-> {out}")
    return out, stopped


def known_facts(entry: str) -> list[str]:
    """入り口の事実のデータ(言う段が逆質問に答えるときに選ぶ事実の全部)。talk は aica 全体の事実だけ。"""
    from dip_mcp.convo import facts as F

    path = {"career": "dip_mcp/career/facts.json", "baito": "dip_mcp/baito/facts.json"}.get(entry)
    d = F.load(HERE.parent / path if path else None)
    return list(dict.fromkeys([*d["ask_service"], *d["ask_advice"], *d["where"].values(), *([d["known"]] if d["known"] else [])]))


def rejudge(paths: list[Path], out: Path, ledger: Path, limit: float, log=print) -> Path:
    """事実の条件だけを、入り口の事実のデータを渡したジャッジで判定し直し、行を out に書く(ほかの条件と返信はそのまま)。"""
    from evals.meter import Meter
    from evals.run import JUDGE_MODEL
    from line_runtime import config

    by_id = {s["id"]: s for s in S.load_scenes()}
    meter = Meter().install()
    real = RealModel(meter)
    model = JUDGE_MODEL[config.PROVIDER]
    rows = load(paths)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("", encoding="utf-8")
    try:
        for row in rows:
            scene = by_id[row["scene"]]
            idx = [i for i, c in enumerate(scene["expect"]) if c["check"] in FACT_CHECKS and not handed(scene, row["replies"], c)]
            if idx and not row["error"]:
                budget.check(ledger, limit)
                m = meter.mark()
                got = real.loop.run_until_complete(J.judge(real.client(), model, scene, {"replies": row["replies"]},
                                                           [scene["expect"][i] for i in idx], known_facts(row["entry"])))
                before = [row["results"][i]["status"] for i in idx]
                for i, x in zip(idx, got, strict=True):
                    row["results"][i] = x | {"rejudged": True}
                cost = meter.summary(m)["cost_usd"]
                budget.record(ledger, "scenes-rejudge", cost, tag=row.get("tag"), entry=row["entry"], version=row["version"],
                              scene=row["scene"])
                log(f"{row.get('tag')} {row['scene']}: {before} -> {[row['results'][i]['status'] for i in idx]} ${cost:.4f}")
            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    finally:
        real.close()
    log(f"この実行 ${meter.summary()['cost_usd']:.4f} / {budget.line(ledger, limit)}")
    return out


# ----------------------------- ペルソナの面白さ(personal_touch の割合) -----------------------------
TOUCH_SYSTEM = """あなたは、仕事探しを手伝う LINE の AI アシスタント(aica)の返信を審査します。本人と aica の会話が、番号つきで渡されます。
aica の返信ごとに、次の 2 つを判定して返します。
- has_ack: 返信が、問いや案内の前に、本人の直前の発言(打った文か押したボタン)への相づち・受け止めを含むか。問いや決まった案内だけなら false
- personal: has_ack が true のとき、その相づちが本人の今の言葉や中身に合わせた一言か(本人の言葉を拾っている、話に合わせている)。
  「わかりました」「ありがとうございます」「〜ですね」だけのように、誰にでも言える決まり文句なら false。has_ack が false なら false
本人の発言が無い返信(最初の返信)は has_ack を false にします。"""


class TouchItem(BaseModel):
    n: int = Field(description="aica の返信の番号")
    has_ack: bool
    personal: bool


class TouchJudgement(BaseModel):
    items: list[TouchItem]


def conversations(path: Path) -> list[dict]:
    """ペルソナの会話の結果のファイルから、{label, pairs: [(本人の直前の発言 か None, aica の返信)]}。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    if isinstance(d.get("runs"), list):  # 正社員(evals.fulltime_hearing.run。アルバイト・パートの runs は回数)
        for r in d["runs"]:
            if r.get("status") != "ok":
                continue
            said, pairs = None, []
            for t in r["turns"]:
                pairs.append((said, "\n".join(t["reply"]["bubbles"])))
                said = (t.get("actor") or {}).get("text")
            out.append({"label": f"career {r['form']}/{r['persona']}#{r['rep']}", "pairs": pairs})
    else:  # アルバイト・パート(evals.baito_sim --real)。会話ログの BOT と USER の行
        version = d.get("version", "v4")
        for m in d["results"]:
            said, pairs = None, []
            for line in m["log"]:
                if line.startswith("BOT  "):
                    text = re.sub(r"^\(\d+\) ", "", line[5:])
                    text = re.sub(r"\s+\[[^\]]*\]$", "", text).strip()  # 末尾のボタンの一覧
                    pairs.append((said, text))
                elif line.startswith("USER "):
                    said = re.sub(r"^\[[^\]]*\] ", "", line[5:]).strip()
            out.append({"label": f"baito {version}/{m['persona']}#{m['rep']}", "pairs": pairs})
    return out


def touch(paths: list[Path], ledger: Path, limit: float, log=print) -> dict:
    """会話ごとに相づちのあるターンと personal_touch を数える。返すのは {入り口 版: [personal, 相づちのあるターン, 会話]}。"""
    from evals.meter import Meter
    from evals.run import JUDGE_MODEL
    from line_runtime import config, llm

    meter = Meter().install()
    real = RealModel(meter)
    model = JUDGE_MODEL[config.PROVIDER]
    totals: dict = {}
    try:
        for path in paths:
            for c in conversations(path):
                budget.check(ledger, limit)
                m = meter.mark()
                lines = []
                for i, (said, reply) in enumerate(c["pairs"]):
                    if said is not None:
                        lines.append(f"本人: {said}")
                    lines.append(f"aica(返信 {i}): {reply}")
                out = real.loop.run_until_complete(llm.parse(real.client(), model=model, max_tokens=4000, effort="low",
                                                             schema=TouchJudgement, system=TOUCH_SYSTEM,
                                                             messages=[{"role": "user", "content": "\n".join(lines)}]))
                items = {i.n: i for i in (out.items if out else [])}
                acks = [items[i] for i in range(len(c["pairs"])) if i in items and items[i].has_ack and c["pairs"][i][0]]
                key = c["label"].split("/")[0]
                t = totals.setdefault(key, [0, 0, 0])
                t[0] += sum(i.personal for i in acks)
                t[1] += len(acks)
                t[2] += 1
                cost = meter.summary(m)["cost_usd"]
                budget.record(ledger, "persona-touch", cost, conversation=c["label"])
                log(f"{c['label']}: personal {sum(i.personal for i in acks)}/{len(acks)} ${cost:.4f}")
    finally:
        real.close()
    for key, (pp, n, k) in totals.items():
        log(f"{key}: 相づちのあるターンのうち personal_touch {_pct(pp, n)}(会話 {k})")
    log(f"この実行 ${meter.summary()['cost_usd']:.4f} / {budget.line(ledger, limit)}")
    return totals


# ----------------------------- 点 -----------------------------
def load(paths: list[Path]) -> list[dict]:
    return [json.loads(x) for p in paths for x in Path(p).read_text(encoding="utf-8").splitlines() if x.strip()]


def _pct(p: int, n: int) -> str:
    return f"{p}/{n}({p / n:.1%})" if n else "-"


def _q(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def tally(rows: list[dict]) -> dict:
    """入り口、版、読む段のモデルごとの点。コードと採点の条件は、判定したもの(skip を除く)を分母にする。"""
    out: dict = {}
    for r in rows:
        key = (r["entry"], r["version"], r.get("read_model") or "-")
        t = out.setdefault(key, {"rows": 0, "reps": set(), "errors": 0, "code": [0, 0], "guard": [0, 0], "judge": [0, 0],
                                 "strict": [0, 0], "switch": [0, 0], "scenes_all_pass": [0, 0], "skip_code": 0, "skip_judge": 0,
                                 "fail_code": {}, "fail_judge": {}, "seconds": [], "calls": [], "turn_seconds": [],
                                 "read_seconds": [], "say_seconds": [], "cost": 0.0, "fallback": 0})
        t["rows"] += 1
        t["reps"].add((r.get("tag"), r.get("rep", 0)))  # 別々の実行(tag)の回も数える
        t["errors"] += bool(r["error"])
        t["cost"] += r.get("cost_usd") or 0.0
        code = [x for x in r["results"] if not (x.get("judge") or x["check"] in S.JUDGE_CHECKS) and x["status"] != K.SKIP]
        if code:
            t["scenes_all_pass"][1] += 1
            t["scenes_all_pass"][0] += all(x["status"] == K.PASS for x in code)
        for x in r["results"]:
            ok = x["status"] == K.PASS
            judged = x.get("judge") or x["check"] in S.JUDGE_CHECKS  # 場面を回せなかった行(runner.score)は judge の印が無い
            if x["status"] == K.SKIP:
                t["skip_judge" if judged else "skip_code"] += 1
                continue
            bucket = "judge" if judged else "code"
            t[bucket][0] += ok
            t[bucket][1] += 1
            if not ok:
                fails = t["fail_judge" if judged else "fail_code"]
                fails.setdefault(x["check"], []).append(r["scene"])
            if x["check"] in R.GUARD_CHECKS:
                t["guard"][0] += ok
                t["guard"][1] += 1
            if x["check"] in STRICT_JUDGE:
                t["strict"][0] += ok
                t["strict"][1] += 1
            moved = x["check"] == "not_asks" and (x.get("args") or {}).get("entry") not in (None, r["entry"])
            if r["category"] == "switch" and (x["check"] in SWITCH_CHECKS or moved):
                t["switch"][0] += ok
                t["switch"][1] += 1
        for rep in r["replies"]:
            if rep.get("model_calls"):
                t["calls"].append(rep["model_calls"])
                if "model_seconds" in rep:  # talk はランタイムの 1 ターンの時間(turn_seconds)だけ
                    t["seconds"].append(rep["model_seconds"])
            if rep.get("turn_seconds") is not None:
                t["turn_seconds"].append(rep["turn_seconds"])
            for st in rep.get("stages") or []:
                t["read_seconds" if st["stage"] in ("read", "extract") else "say_seconds"].append(st["seconds"])
                t["fallback"] += not st["ok"]
    return out


def verdict(t: dict, entry: str) -> list[str]:
    """§4.10 の合格の線(場面の一覧の分)に届かないもの。"""
    bad = []
    for name, (p, n), line in (("コードの条件", t["code"], LINES["code"]), ("採点の条件", t["judge"], LINES["judge"])):
        if n and p / n < line:
            bad.append(f"{name} {p / n:.1%} < {line:.0%}")
    for name, (p, n) in (("守りの条件", t["guard"]), ("採点の守り(no_invented_facts など)", t["strict"]),
                         ("入り口をまたぐ条件", t["switch"])):
        if n and p < n:
            bad.append(f"{name} {p}/{n}")
    return bad


def _order(stats: dict) -> list:
    return sorted(stats.items(), key=lambda kv: (list(S.ENTRIES).index(kv[0][0]), kv[0][1], kv[0][2]))


def _sec(v: float | None) -> str:
    return "-" if v is None else f"{v:.1f}"


def summary(rows: list[dict]) -> str:
    lines = [("| 入り口 版 | 読む段 | 回 | 場面の回 | エラー | すべて満たした場面 | コードの条件 | 守りの条件 | 採点の条件 | 採点の守り | "
              "入り口をまたぐ条件 | 費用 |"), "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    stats = tally(rows)
    for (entry, version, rm), t in _order(stats):
        lines.append(f"| {entry} {version} | {rm} | {len(t['reps'])} | {t['rows']} | {t['errors']} | {_pct(*t['scenes_all_pass'])} | "
                     f"{_pct(*t['code'])} | {_pct(*t['guard'])} | {_pct(*t['judge'])} | {_pct(*t['strict'])} | "
                     f"{_pct(*t['switch'])} | ${t['cost']:.2f} |")
    lines += ["", "速さ(ハーネスのモデルを呼んだターン。秒。talk はランタイムの 1 ターン):", "",
              "| 入り口 版 | 読む段 | ターン | 呼び出し/ターン | 中央 | 90% | 読む段の中央 | 言う段などの中央 | 失敗の印 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for (entry, version, rm), t in _order(stats):
        secs = t["seconds"] or t["turn_seconds"]
        calls = f"{statistics.mean(t['calls']):.2f}" if t["calls"] else "-"
        lines.append(f"| {entry} {version} | {rm} | {len(secs)} | {calls} | {_sec(_q(secs, 0.5))} | {_sec(_q(secs, 0.9))} | "
                     f"{_sec(_q(t['read_seconds'], 0.5))} | {_sec(_q(t['say_seconds'], 0.5))} | {t['fallback']} |")
    lines += ["", "合格の線(§4.10。場面の一覧の分):", ""]
    for (entry, version, rm), t in _order(stats):
        bad = verdict(t, entry)
        lines.append(f"- {entry} {version}(読む段 {rm}): " + ("届いた" if not bad else "届かない: " + "、".join(bad)))
    lines += ["", "落ちた条件(条件: 場面。回の数だけ並ぶ):", ""]
    for (entry, version, rm), t in _order(stats):
        lines.append(f"- {entry} {version}(読む段 {rm})")
        for kind in ("fail_code", "fail_judge"):
            for check, scenes in sorted(t[kind].items(), key=lambda kv: -len(kv[1])):
                lines.append(f"  - {'コード' if kind == 'fail_code' else '採点'} {check}: {', '.join(sorted(scenes))}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.scenes.real", description="実モデルの回の点(summary は費用 0)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary", help="結果の JSONL を集めて点の表を出す")
    s.add_argument("paths", nargs="+", type=Path)
    s.add_argument("--out", type=Path, help="表を書く Markdown")
    j = sub.add_parser("rejudge", help="事実の条件を、入り口の事実のデータを渡したジャッジで判定し直す(費用がかかる)")
    j.add_argument("paths", nargs="+", type=Path)
    j.add_argument("--out", type=Path, required=True, help="判定し直した行を書く JSONL")
    j.add_argument("--ledger", type=Path, required=True)
    j.add_argument("--limit-usd", type=float, required=True)
    t = sub.add_parser("touch", help="ペルソナの会話の personal_touch の割合(費用がかかる)")
    t.add_argument("paths", nargs="+", type=Path)
    t.add_argument("--ledger", type=Path, required=True)
    t.add_argument("--limit-usd", type=float, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "touch":
        from line_runtime.config import load_env

        load_env()
        touch(a.paths, a.ledger, a.limit_usd, log=lambda m: print(m, flush=True))
        return 0
    if a.cmd == "rejudge":
        from line_runtime.config import load_env

        load_env()
        try:
            rejudge(a.paths, a.out, a.ledger, a.limit_usd, log=lambda m: print(m, flush=True))
        except budget.BudgetExceeded as e:
            print(f"!! {e}", file=sys.stderr)
            return 2
        return 0
    text = summary(load(a.paths))
    print(text)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(f"<!-- {datetime.now():%Y-%m-%d %H:%M} evals.scenes.real summary -->\n\n" + text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
