"""dip 側のデータ(シミュレーション)。求人・ユーザー状態・会話結果の保存。

本番では dipAIAG / バイトル API / CRM 基盤が持つもの。ここでは fixtures の架空データと、
会話で変わる状態(会話結果・保存・フォームの続き・応募)を JSON ファイルで持つ。
時刻はシミュレーション用に固定する(SIM_NOW)。シナリオの「2時間前に離脱」などを再現可能にするため。
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
JST = timezone(timedelta(hours=9))
SIM_NOW = datetime.fromisoformat(os.environ.get("SIM_NOW", "2026-09-23T12:00:00+09:00"))

# 個人情報(トークでは受け取らず LIFF 画面で入力させる項目)
PII_FIELDS = {"電話番号", "氏名", "住所", "メールアドレス", "生年月日"}
# 項目名に関係なく、値に含まれていたら個人情報とみなすもの(電話番号・メールアドレス)
PII_VALUE = re.compile(r"0\d{1,4}-?\d{1,4}-?\d{3,4}|[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class BaitoJob(BaseModel):
    job_id: str
    title: str
    employer: str
    job_type: str
    area: str
    station: str
    walk_min: int
    hourly_wage: int
    time_bands: list[str] = Field(description="早朝 / 日中 / 夕方 / 夜 / 深夜")
    shift: str
    days_min: int
    perks: list[str] = Field(default_factory=list)
    description: str
    drawback: str


def now() -> datetime:
    return SIM_NOW


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def load_jobs() -> dict[str, BaitoJob]:
    jobs = []
    for name in ("anchor_jobs.json", "jobs.json"):
        p = FIXTURES / name
        if p.is_file():
            jobs += [BaitoJob(**d) for d in json.loads(p.read_text(encoding="utf-8"))]
    return {j.job_id: j for j in jobs}


def load_listings() -> dict[str, dict]:
    """求人の掲載情報(掲載開始 posted_at / 掲載終了 listing_end)。載っていない求人は日付不明として扱う。"""
    data = json.loads((FIXTURES / "listings.json").read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_job_faq() -> dict[str, list[dict]]:
    """求人ごとの Q&A(job_id → [{q, a}])。無い求人は空。"""
    p = FIXTURES / "job_faq.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def load_users() -> dict[str, dict]:
    users = json.loads((FIXTURES / "users.json").read_text(encoding="utf-8"))
    return {u["line_user_id"]: u for u in users}


class State:
    """会話で変わるユーザー状態。fixtures の初期状態に、会話中の更新を重ねて JSON に保存する。

    MCP サーバー(子プロセス)と LIFF 画面(Web プロセス)が同じファイルを読み書きするため、
    メモリに持たず毎回ファイルから読む(シミュレーション規模なので十分速い)。
    path を変えれば評価の実行ごとに独立した状態で走らせられる。reset() で初期状態に戻す。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(os.environ.get("DIP_STATE_PATH", ROOT / "data" / "state.json"))

    @contextmanager
    def _lock(self):
        """プロセスをまたぐ排他(読み→更新→書きの間に他プロセスの更新を失わないように)。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_suffix(".lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def _read(self) -> dict:
        if self.path.is_file():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"users": copy.deepcopy(load_users())}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(self.path)

    def reset(self) -> None:
        with self._lock():
            self._write({"users": copy.deepcopy(load_users())})

    def reset_user(self, line_user_id: str, template: str | None = None) -> None:
        """1人だけ初期状態に戻す。fixtures に無いユーザーは消す(未連携の新規に戻る)。
        template を与えると、そのシナリオ用ユーザーの初期状態を line_user_id に写す
        (ブラウザごとの匿名ユーザーが、同じシナリオを他の人と混線せずに使えるように)。"""
        with self._lock():
            data = self._read()
            u = load_users().get(template or line_user_id)
            if u is None:
                data["users"].pop(line_user_id, None)
            else:
                data["users"][line_user_id] = copy.deepcopy(u) | {"line_user_id": line_user_id}
            self._write(data)

    def user(self, line_user_id: str) -> dict:
        """未知の LINE ユーザーは「dip ID 未連携の新規」として扱う。"""
        with self._lock():
            u = self._read()["users"].get(line_user_id)
        return u or _new_user(line_user_id)

    def update(self, line_user_id: str, fn) -> dict:
        with self._lock():
            data = self._read()
            u = data["users"].setdefault(line_user_id, _new_user(line_user_id))
            fn(u)
            self._write(data)
            return copy.deepcopy(u)


def _new_user(line_user_id: str) -> dict:
    return {"line_user_id": line_user_id, "dip_id": None, "label": "未連携の新規", "applications": [], "views": [],
            "saves": [], "form_exits": [], "contacts": [], "outcomes": []}
