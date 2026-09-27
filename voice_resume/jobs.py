"""希望条件 → recommend_jobs の引数づくりと、求人の読み上げ文。

職種は dip 側(`dip_mcp/logic.py` の JOB_TYPES)と
完全一致させる(PR #25: 一覧に無い値はエラーになる)。ここにコピーを持ち、
テストで dip 側とずれていないことを確かめる(dip_mcp のコードには依存しない)。
エリアの一覧(AREAS)と広い言い方(AREA_PHRASES)も同じやり方で持つ(task 33)。
"""

from __future__ import annotations

# dip_mcp/logic.py の JOB_TYPES と同じ12種類(テストで同期を確認)
JOB_TYPES = (
    "アパレル販売", "イベントスタッフ", "オフィス事務", "カフェ", "コンビニ", "コールセンター",
    "スーパー・販売", "倉庫・軽作業", "塾講師・家庭教師", "配送・デリバリー", "飲食キッチン", "飲食ホール",
)

# dip_mcp/logic.py の AREAS と同じ11の地名(テストで同期を確認)
AREAS = ("新宿", "渋谷", "池袋", "品川", "上野", "中野", "高円寺", "吉祥寺", "横浜", "川崎", "大宮")
# dip 側がその範囲の地名に広げて検索する広い言い方(dip_mcp/logic.py の AREA_GROUPS のキーと同期)
AREA_PHRASES = ("東京都内", "都内", "東京", "東京都", "神奈川", "神奈川県", "埼玉", "埼玉県", "どこでも", "指定なし")

TIME_BANDS = ("早朝", "日中", "夕方", "夜", "深夜")

MAX_PROPOSALS = 3

# 検索のためにゆるめられる(外せる)条件と、読み上げ用の呼び名。
# dip の relax_conditions の options[].remove と同じ名前を使う。
RELAXABLE = ("area", "time_band", "job_type", "min_wage")
CONDITION_LABELS = {
    "area": "エリア", "time_band": "時間帯", "job_type": "職種",
    "min_wage": "時給の下限", "keywords": "その他の希望",
}


def build_recommend_args(
    *,
    job_type: str | None = None,
    area: str | None = None,
    time_band: str | None = None,
    min_wage: int | None = None,
) -> dict:
    """会話で聞いた希望から recommend_jobs の引数を作る。

    job_type / time_band は決まった一覧との完全一致だけを許す(合わなければ ValueError)。
    area はここでは一覧チェックをしない(駅名も許され、駅名の一覧は dip 側だけが持つ。
    一覧に無い値は dip 側がエラーと一覧を返す)。
    指定の無い条件は引数に入れない(dip 側で絞り込みされない)。
    """
    if job_type is not None and job_type not in JOB_TYPES:
        raise ValueError(
            f"職種「{job_type}」は一覧にありません。次から選ぶ: {' / '.join(JOB_TYPES)}"
        )
    if time_band is not None and time_band not in TIME_BANDS:
        raise ValueError(
            f"時間帯「{time_band}」は一覧にありません。次から選ぶ: {' / '.join(TIME_BANDS)}"
        )
    if min_wage is not None:
        min_wage = int(min_wage)
        if min_wage <= 0:
            raise ValueError("min_wage は正の整数(円)にする")
    args = {"job_type": job_type, "area": area, "time_band": time_band, "min_wage": min_wage}
    return {k: v for k, v in args.items() if v is not None}


def split_relaxed(args: dict, relax: list[str] | None) -> tuple[dict, dict]:
    """検索のためにゆるめる条件を引数から外す。(検索に使う引数, 外した条件) を返す。

    本人の希望(preferences)はそのまま残し、検索だけをゆるめるための分離。
    relax に RELAXABLE 以外の名前があれば ValueError。
    """
    if not relax:
        return args, {}
    unknown = [k for k in relax if k not in RELAXABLE]
    if unknown:
        raise ValueError(
            f"relax に知らない条件「{'、'.join(unknown)}」。次から選ぶ: {' / '.join(RELAXABLE)}"
        )
    relaxed = {k: args[k] for k in relax if k in args}
    return {k: v for k, v in args.items() if k not in relaxed}, relaxed


def relax_option_line(option: dict) -> str:
    """relax_conditions の選択肢1つの読み上げ材料(例: エリア(東京都内)を外すと5件)。

    relax に渡す条件名を (relax: area) の形で添え、アドバイザーがそのまま使えるようにする。
    """
    key = str(option.get("remove", ""))
    label = CONDITION_LABELS.get(key, key)
    current = option.get("current")
    unit = "円" if key == "min_wage" else ""
    shown = f"({current}{unit})" if current not in (None, "") else ""
    return f"- {label}{shown}を外すと{option.get('total_after', 0)}件(relax: {key})"


def spoken_job_line(job: dict) -> str:
    """求人1件の読み上げ文(職種・場所・時給・一言)。音声用なので短い1文にする。"""
    place = job.get("station") or job.get("area") or ""
    note = (job.get("reasons") or [""])[0] or (job.get("perks") or [""])[0]
    parts = [f"{job.get('job_type', '')}のお仕事"]
    if place:
        parts.append(f"場所は{place}")
    if job.get("hourly_wage"):
        parts.append(f"時給{job['hourly_wage']}円")
    if note:
        parts.append(note)
    return "、".join(p for p in parts if p) + "です。"
