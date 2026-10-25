"""dip 側のデータ(シミュレーション)。求人(アルバイト・パートと正社員)・ユーザー状態・会話結果の保存。

本番では dipAIAG / バイトル API が持つもの。ここでは fixtures の架空データと、
会話で変わる状態(会話結果・希望・区分など)を JSON ファイルで持つ。本人の行動(閲覧・応募・保存・離脱・接触)は
ここには持たず、元データ(dip_mcp/events.py)と 1 人 1 行の要約(dip_mcp/summarize.py)にある(ADR-0005、ADR-0006)。
時刻(now)は、シナリオユーザーと評価のユーザーだけ固定する(SIM_NOW)。シナリオの「2時間前に離脱」などを再現可能にするため。
本番のユーザーは実際の時刻(ADR-0004)。
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
JST = timezone(timedelta(hours=9))
SIM_NOW = datetime.fromisoformat(os.environ.get("SIM_NOW", "2026-09-23T12:00:00+09:00"))

# 個人情報(トークでは受け取らず LIFF 画面で入力させる項目)。値に含まれる電話番号・メールアドレス・住所の決まりは
# resume_draft/pii.py(personal_value / redact_personal)
PII_FIELDS = {"電話番号", "氏名", "住所", "メールアドレス", "生年月日"}
# 雇用区分(ADR-0002)。求人には「未定」は付かない(未定は求職者の側の状態)
EMPLOYMENT_TYPES = ("アルバイト・パート", "正社員")
PAY_KINDS = ("時給", "月給", "年収")


class Pay(BaseModel):
    """給与。金額はすべて円(年収500万円なら 5000000)。幅が無い(時給1,350円など)なら max は None。"""
    kind: Literal["時給", "月給", "年収"]
    min: int
    max: int | None = None


class WorkStyle(BaseModel):
    """働き方(正社員の求人だけ)。"""
    remote: Literal["可", "一部可", "不可"] = Field(description="リモートワーク(可 = 原則在宅、一部可 = 週に何日か)")
    flex: bool = Field(description="フレックスタイム制か")
    overtime_hours: int = Field(description="残業の目安(月の平均時間)")


class Holidays(BaseModel):
    """休日(正社員の求人だけ)。"""
    annual_days: int = Field(description="年間休日(日)")
    weekends_holidays: bool = Field(description="土日祝が休みか(false はシフト制・平日休みなど)")


class EmploymentTerms(BaseModel):
    """雇用の条件(正社員の求人だけ)。"""
    probation_months: int = Field(0, description="試用期間(か月。無ければ 0)")
    notes: str = Field("", description="試用期間中の条件の違いなど")


# 求人の属性(task 111): 人と話す量と体を使う量を3段階で持つ。「立ち仕事は避けたい」「接客はしたくない」のような
# 否定の言い方は、keywords の意味検索では避けたい仕事の文面(「立ちっぱなし」など)に近くなってしまうので、属性で絞る。
# 座り仕事かどうかは physical_level の 1 と 2 以上の境で表す(立ち仕事かどうかを別の項目に持たない)。2 を「立ち仕事・
# 歩くが力仕事は少ない」(コンビニ・カフェなど)と決めたので、座り仕事(1)・立ち仕事(2)・力仕事(3)が1つの軸に並ぶ
TALK_LEVELS = {
    1: "人と話すことはほとんど無い(黙々と作業する)",
    2: "同僚との連絡や決まったやりとりはあるが、お客様と話すことは少ない",
    3: "お客様・生徒・取引先など、人と話すのが仕事の中心(接客・電話・営業・指導・案内)",
}
PHYSICAL_LEVELS = {
    1: "座り仕事が中心(デスク・電話・入力・座ってする作業や指導)",
    2: "立ち仕事や歩くことが中心だが、力仕事は少ない(レジ・接客・調理・受付・講義)",
    3: "体を使う仕事が中心(荷物の運搬・積み下ろし・倉庫を歩き回る・配達・介助・設営・屋外で動き回る)",
}


def _levels_text(levels: dict[int, str]) -> str:
    return " / ".join(f"{k} = {v}" for k, v in levels.items())


class BaitoJob(BaseModel):
    """求人1件。アルバイト・パートと正社員で同じ形(ADR-0002)。

    time_bands・days_min はアルバイト・パートの項目で、正社員の求人では空(time_bands=[]、days_min=None)。
    required_experience〜employment_terms は正社員の求人だけの任意項目(アルバイト・パートでは空)。
    talk_level・physical_level は両方の区分の求人に必ず付ける(埋め込みの文面 job_text には入れない)。
    """
    job_id: str
    title: str
    employer: str
    employment_type: Literal["アルバイト・パート", "正社員"]
    job_type: str
    area: str
    station: str
    walk_min: int
    pay: Pay
    time_bands: list[str] = Field(default_factory=list, description="早朝 / 日中 / 夕方 / 夜 / 深夜")
    shift: str
    days_min: int | None = None
    perks: list[str] = Field(default_factory=list)
    description: str
    drawback: str
    talk_level: int = Field(ge=1, le=3, description=f"人と話す量: {_levels_text(TALK_LEVELS)}")
    physical_level: int = Field(ge=1, le=3, description=f"体を使う量: {_levels_text(PHYSICAL_LEVELS)}")
    required_experience: list[str] = Field(default_factory=list, description="必須の経験・資格")
    preferred_experience: list[str] = Field(default_factory=list, description="歓迎の経験・資格")
    work_style: WorkStyle | None = None
    holidays: Holidays | None = None
    employment_terms: EmploymentTerms | None = None


def man(yen: int) -> str:
    """円を「万」の単位の文字にする(4500000 → 450、4550000 → 455)。"""
    return f"{yen / 10000:g}"


def pay_text(pay: Pay) -> str:
    """給与の表示(時給1350円 / 月給24万〜30万円 / 年収450万〜650万円)。求人の文・理由・資料で共通にする。
    時給は円のまま(今までの「時給1350円」と同じ文)、月給・年収は万円で書く。"""
    if pay.kind == "時給":
        return f"時給{pay.min}円" + (f"〜{pay.max}円" if pay.max and pay.max != pay.min else "")
    if pay.max and pay.max != pay.min:
        return f"{pay.kind}{man(pay.min)}万〜{man(pay.max)}万円"
    return f"{pay.kind}{man(pay.min)}万円"


def annual_pay_mid(pay: Pay) -> int | None:
    """年収の見込みの中央値(円)。幅の真ん中で、月給は12か月分(賞与を含めない控えめな見込み)。時給は None。
    希望年収の下限はこれと比べる(上端で比べると「600万以上」で年収480万〜700万が出てしまう。2026-10-03 のユーザーの決定)。"""
    mid = (pay.min + (pay.max or pay.min)) // 2
    return {"年収": mid, "月給": mid * 12}.get(pay.kind)


def uses_sim_clock(line_user_id: str | None) -> bool:
    """固定の時計 SIM_NOW を使うユーザーか(ADR-0004)。fixtures の初期状態から始まるユーザーだけ:
    評価のユーザー(fixtures のユーザーそのもの、例 sim-new)と、開発者画面のシナリオユーザー
    (「匿名ID:fixtures のユーザー」、例 w-…:sim-new)。ユーザーの無い呼び出し(評価の検索)も固定。
    ほか(本番の匿名ユーザーID w-…)は実際の時刻。"""
    if line_user_id is None:
        return True
    return (line_user_id.partition(":")[2] or line_user_id) in _fixture_user_ids()


@cache
def _fixture_user_ids() -> frozenset[str]:
    return frozenset(load_users())


def now(line_user_id: str | None) -> datetime:
    """そのユーザーにとっての「今」。シナリオユーザーと評価のユーザーは SIM_NOW、本番のユーザーは実際の時刻(日本時間)。"""
    return SIM_NOW if uses_sim_clock(line_user_id) else datetime.now(JST)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def load_jobs() -> dict[str, BaitoJob]:
    jobs = []
    for name in ("anchor_jobs.json", "jobs.json", "fulltime_jobs.json"):
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
    行動の元データと要約は同じディレクトリに置く(dip_mcp/behavior.py)。元データは追記だけなので reset() では消さないが、
    会話の側から書いた分(1 人の dict の dip_writes)が消えるので、読める行動は要約の時点に戻る。
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
        return {"users": _initial_users()}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        tmp.replace(self.path)

    def reset(self) -> None:
        """全員を初期状態に戻す。連携の表も初期(シナリオのユーザーだけが連携済み)に戻す。"""
        from dip_mcp import accounts  # accounts が data を読むので、ここで読む

        with self._lock():
            self._write({"users": _initial_users()})
        accounts.of(self).reset_links()

    def reset_user(self, line_user_id: str, template: str | None = None, keep=None) -> dict:
        """1人だけ初期状態に戻す。fixtures に無いユーザーは消し、連携も外す(未連携の新規に戻る)。
        template を与えると、そのシナリオ用ユーザーの初期状態を line_user_id に写す
        (ブラウザごとの匿名ユーザーが、同じシナリオを他の人と混線せずに使えるように)。
        連携の表も写す: シナリオのユーザーは、最初から同じ dip の会員と連携済みとして始まる(ADR-0007)。
        keep(戻す前の 1 人の dict → 持ち越す値の dict)を与えると、戻した後の行に、その値を同じロックの中で書き戻す
        (「すべて消す」が持ち越す消す前の部屋が無い間に、その部屋の書き込みが通らないように。profile.forget_all)。
        戻す前の 1 人の dict を返す。"""
        from dip_mcp import accounts

        with self._lock():
            data = self._read()
            before = data["users"].get(line_user_id) or _new_user(line_user_id)
            u = _initial_users().get(template or line_user_id)
            if u is None:
                data["users"].pop(line_user_id, None)
            else:
                data["users"][line_user_id] = u | {"line_user_id": line_user_id}
            if kept := (keep(before) if keep else None):
                data["users"].setdefault(line_user_id, _new_user(line_user_id)).update(kept)
            self._write(data)
        accounts.of(self).set_link(line_user_id, accounts.fixture_dip_id(template or line_user_id))
        return copy.deepcopy(before)

    def user_ids(self) -> list[str]:
        """状態にいるユーザーの ID(fixtures の初期ユーザーを含む)。"""
        with self._lock():
            return list(self._read()["users"])

    def user(self, line_user_id: str) -> dict:
        """未知の LINE ユーザーは新規として扱う(dip と連携しているかは連携の表で決まる。dip_mcp.behavior.dip_id)。"""
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


def _initial_users() -> dict[str, dict]:
    """fixtures のユーザーの初期状態。dip_id は連携の表(dip_mcp.accounts)の種なので、1 人の dict には写さない
    (dip_id を引くのは連携の表だけ。ADR-0007)。"""
    return {uid: {k: v for k, v in u.items() if k != "dip_id"} for uid, u in copy.deepcopy(load_users()).items()}


def _new_user(line_user_id: str) -> dict:
    return {"line_user_id": line_user_id, "label": "未連携の新規", "outcomes": []}
