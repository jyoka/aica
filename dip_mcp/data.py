"""dip 側のデータ(シミュレーション)。求人(アルバイト・パートと正社員)・ユーザー状態・会話結果の保存。

本番では dipAIAG / バイトル API が持つもの。ここでは fixtures の架空データと、
会話で変わる状態(会話結果・保存・フォームの続き・応募)を JSON ファイルで持つ。
時刻はシミュレーション用に固定する(SIM_NOW)。シナリオの「2時間前に離脱」などを再現可能にするため。
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
JST = timezone(timedelta(hours=9))
SIM_NOW = datetime.fromisoformat(os.environ.get("SIM_NOW", "2026-09-23T12:00:00+09:00"))

# 個人情報(トークでは受け取らず LIFF 画面で入力させる項目)
PII_FIELDS = {"電話番号", "氏名", "住所", "メールアドレス", "生年月日"}
# 項目名に関係なく、値に含まれていたら個人情報とみなすもの(電話番号・メールアドレス)。
# 電話番号は、区切り(- や空白・括弧)のある形か、0 で始まる10〜11桁だけ。数字の途中の 0 から始まる並び
# (「年収3000000円」「3,000,000円」の 000000)は電話番号ではないので、前後が数字か「カンマ+数字」のものは数えない
# (「090-1234-5678,よろしく」のように、後ろのカンマに数字が続かなければ電話番号として捕まえる)。
# カンマの決まりは区切りの無い形(0\d{9,10})だけに効かせる。区切りのある形は桁の区切りと紛れないので、
# 「1,090-1234-5678」「090-1234-5678,1」のように数字とカンマに挟まれていても捕まえる。
# 全角の数字・ハイフン(「０９０－１２３４－５６７８」「090ー1234ー5678」「090−1234−5678」)は、
# 照合の前に fold_pii で半角にそろえる(文字数は変えない)。探すときは has_pii / redact_pii を使う。
PII_VALUE = re.compile(
    r"(?<!\d)(?:0\d{1,4}[-(\s]\d{1,4}[-)\s]\d{3,4}|0\d{1,4}-\d{6,8})(?!\d)"
    r"|(?<!\d)(?<!\d,)0\d{9,10}(?!\d|,\d)"
    # メールは、名前の部分の先頭からだけ照合する(長い文で位置ごとに照合し直して時間がかからないように)
    r"|(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
)
# 全角の数字・記号と、ハイフンに見える文字(長音記号・マイナス・各種ダッシュ)を半角に。1文字を1文字に写す
_PII_FOLD = str.maketrans(
    {chr(0xFF10 + i): str(i) for i in range(10)}
    | dict.fromkeys("－ーｰ−‐‑‒–—―", "-")
    | {"（": "(", "）": ")", "＠": "@", "．": ".", "　": " ", "，": ","}
)


def fold_pii(text: str) -> str:
    """個人情報の照合用に、全角の数字・ハイフンなどを半角にそろえる(文字数・位置は変えない)。"""
    return text.translate(_PII_FOLD)


def has_pii(text: str) -> bool:
    """電話番号・メールアドレスらしい値を含むか(全角・長音記号のハイフンも含めて)。"""
    return PII_VALUE.search(fold_pii(text)) is not None


def redact_pii(text: str, repl: str = "[個人情報を削除]",
               extra: Callable[[str], list[tuple[int, int]]] | None = None) -> str:
    """電話番号・メールアドレス(と extra が返す範囲)を repl に置き換える。照合は fold_pii した文字で行い、
    置き換えは元の文字列の同じ位置に対して行う(全角のまま残ったりしない)。"""
    folded = fold_pii(text)
    spans = [m.span() for m in PII_VALUE.finditer(folded)]
    if extra is not None:
        spans += extra(folded)
    out, last = [], 0
    for start, end in sorted(spans):
        if start < last:  # 重なった分は前の置き換えに含める
            last = max(last, end)
            continue
        out += [text[last:start], repl]
        last = end
    return "".join(out) + text[last:]


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


class BaitoJob(BaseModel):
    """求人1件。アルバイト・パートと正社員で同じ形(ADR-0002)。

    time_bands・days_min はアルバイト・パートの項目で、正社員の求人では空(time_bands=[]、days_min=None)。
    required_experience〜employment_terms は正社員の求人だけの任意項目(アルバイト・パートでは空)。
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


def annual_pay_max(pay: Pay) -> int | None:
    """年収の上限の見込み(円)。年収はそのまま、月給は12か月分(賞与を含めない控えめな見込み)。時給は None。"""
    top = pay.max or pay.min
    return {"年収": top, "月給": top * 12}.get(pay.kind)


def now() -> datetime:
    return SIM_NOW


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

    def user_ids(self) -> list[str]:
        """状態にいるユーザーの ID(fixtures の初期ユーザーを含む)。"""
        with self._lock():
            return list(self._read()["users"])

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
