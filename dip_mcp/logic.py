"""dip 側の業務ロジック(MCP ツールの中身)。LLM を使わない。速く・決定的に・事実だけを返す。

設計書の原則をここで守る:
- 要約で返し、生ログは渡さない。閲覧は件数と傾向だけ(求人名を列挙しない)。
- 本人の行動は、会員 1 人 1 行の状況の要約(と、この会話で書いた分)だけから読む(dip_mcp/behavior.py、ADR-0006)。
  行動の元データは読まない。書くときは元データにイベントとして追記する。
- 履歴は 90 日以内のものだけ。要約は「仮説」として返す(断定しない材料)。
- 推薦は最大3件、各件に事実に基づく理由を付ける。
- 個人情報の項目はトークで受け取らず、LIFF 画面へ回す。
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
from collections import Counter
from datetime import datetime
from statistics import median
from urllib.parse import urlencode

from dip_mcp import behavior, embedding
from dip_mcp.data import (
    EMPLOYMENT_TYPES,
    FIXTURES,
    PHYSICAL_LEVELS,
    PII_FIELDS,
    ROOT,
    TALK_LEVELS,
    BaitoJob,
    Pay,
    State,
    annual_pay_mid,
    load_job_faq,
    load_jobs,
    load_listings,
    now,
    parse_ts,
    pay_text,
)
from resume_draft.pii import fold_pii, has_pii, redact_pii, scrub_pii
from resume_draft.schema import (
    DEFAULT_DOCUMENT_KIND,
    DOCUMENT_KINDS,
    PERSONAL_KEYS,
    ResumeDraft,
)

HISTORY_DAYS = 90
MAX_JOBS = 3
LIFF_BASE = "/liff/apply"


def liff_url(path: str, line_user_id: str, **params) -> str:
    """dip の画面(LIFF)の URL。ユーザーIDは載せない(画面は Cookie の匿名IDから本人を決める。URL はブラウザの
    履歴・アクセスログ・Referer に残るため)。開発者画面のシナリオユーザー「匿名ID:シナリオユーザー」なら、
    匿名IDを除いたシナリオユーザー(fixtures の名前。秘密ではない)だけを scenario_user に載せる。"""
    scenario_user = line_user_id.partition(":")[2]
    query = {k: v for k, v in params.items() if v is not None} | ({"scenario_user": scenario_user} if scenario_user else {})
    return f"{path}?{urlencode(query)}" if query else path


def _md(ts: str) -> str:
    d = parse_ts(ts)
    return f"{d.month}/{d.day}"


def _days_ago(ts: str, at: datetime) -> int:
    """at(そのユーザーの今。data.now)から暦日で数える(9/19 → 9/23 は4日)。"""
    return (at.date() - parse_ts(ts).astimezone(at.tzinfo).date()).days


def _recent(items: list[dict], key: str, at: datetime, days: int = HISTORY_DAYS) -> list[dict]:
    return [i for i in items if _days_ago(i[key], at) <= days]


def preferences(user: dict, jobs: dict[str, BaitoJob], at: datetime) -> dict:
    """直近の応募・閲覧・保存から、エリア・時間帯・職種の傾向を数える(多い順)。at は数える時点。
    要約のバッチ(summarize.build_summary)が元データをたたんだ形(user)から数え、要約の tendency に入れる。"""
    refs = [a["job_id"] for a in _recent(user.get("applications", []), "applied_at", at)]
    refs += [v["job_id"] for v in _recent(user.get("views", []), "at", at, 30)]
    refs += [s["job_id"] for s in _recent(user.get("saves", []), "at", at, 30)]
    js = [jobs[r] for r in refs if r in jobs]
    top = lambda c: [k for k, _ in c.most_common(2)]
    return {
        "area": top(Counter(j.area for j in js)),
        "time_band": top(Counter(b for j in js for b in j.time_bands)),
        "job_type": top(Counter(j.job_type for j in js)),
    }


def stated_preferences(user: dict) -> dict:
    """本人が会話で話した希望(save_preferences で保存したもの)。保存日つき。"""
    return {k: {"value": v["value"], "saved": _md(v["at"])} for k, v in (user.get("preferences") or {}).items()}


RESUME_NOTE = ("uploaded_resume は本人がアップロードした履歴書の読み取り結果(書いてあった事実だけ。個人情報は含まない)。"
               "経験・資格・希望を聞き直さず、確認形で使う。unconfirmed は読み取れなかった点。")

PROFILE_NOTE = ("stated_preferences には、希望の条件のほか、相談で分かった目的(purpose)・強み(strengths)・"
                "やりたいこと(wants)・避けたいこと(avoids)・不安(concerns)も入る(本人の言葉)。聞き直さず、確認形で使う。"
                "consultation_summary は前の会話(チャットか音声)を締めたときの相談のまとめ(事実と、次に確かめること)。"
                "続きから始める材料にする。")

TRACK_NOTE = ("search_track は本人の発言から記録した今の雇用区分(未定はまだ判断できていない)。"
              "track_history は直近の切り替え(根拠の発言つき)。")


def _track_summary(user: dict) -> dict:
    return {"search_track": search_track(user), "track_history": track_history(user)}


def _profile_summary(user: dict) -> dict:
    """チャットと音声が同じ形で読む、本人について覚えていること(保存項目・相談のまとめ・履歴書)。"""
    return {"stated_preferences": stated_preferences(user), "consultation_summary": consultation_summary(user),
            "uploaded_resume": uploaded_resume(user)}


def conversation_summary(state: State, line_user_id: str) -> dict:
    user = state.user(line_user_id)
    if not behavior.dip_id(state, line_user_id):
        return {"linked": False, "segment": "unlinked", **_track_summary(user), **_profile_summary(user),
                "note": "dip ID と未連携のため行動履歴はない。理解ステップは飛ばし、最初の1問から始める。"
                        "stated_preferences があれば、前に本人が話した希望として確認形で1回だけ使う。"
                        + PROFILE_NOTE + TRACK_NOTE + RESUME_NOTE}
    jobs, at, b = load_jobs(), now(line_user_id), behavior.view(state, line_user_id)
    apps = _recent(b["applications"], "applied_at", at)  # 要約は新しい順。読む人の今でもう一度絞る
    exits = [e for e in [b["form_exit"]] if e and _days_ago(e["exited_at"], at) <= 7]
    contact = b["last_contact"]

    last_app = None
    if apps:
        a = apps[0]
        j = jobs.get(a["job_id"])
        last_app = {"job_id": a["job_id"], "title": j.title if j else "", "job_type": j.job_type if j else "",
                    "area": j.area if j else "", "applied": _md(a["applied_at"]), "days_ago": _days_ago(a["applied_at"], at),
                    "status": a["status"]}
    form_exit = None
    if exits:
        e = exits[-1]
        j = jobs.get(e["job_id"])
        hours = int((at - parse_ts(e["exited_at"])).total_seconds() // 3600)
        form_exit = {"job_id": e["job_id"], "title": j.title if j else "", "hours_ago": hours,
                     "remaining": e["remaining"]}

    if form_exit:
        segment = "form_exit"
    elif last_app and last_app["days_ago"] <= 14 and "連絡待ち" in last_app["status"]:
        segment = "applied_waiting"
    elif (b["views_30d"]["count"] or b["saves_30d"]) and not (last_app and last_app["days_ago"] <= 30):
        segment = "browsing"
    else:
        segment = "active"

    return {
        "linked": True,
        "segment": segment,
        "as_of": b["as_of"],
        **_track_summary(user),
        "last_application": last_app,
        "form_exit": form_exit,
        "recent_views": {"count_30d": b["views_30d"]["count"], "by_job_type": b["views_30d"]["by_job_type"]},
        "saves_30d": b["saves_30d"],
        "tendency": b["tendency"],
        "last_contact": ({"channel": contact["channel"], "when": _md(contact["at"]),
                          "message": contact["message"]} if contact else None),
        **_profile_summary(user),
        "note": "これは行動データからの仮説。本人に確認形で1回だけ触れ、断定しない。閲覧した求人名は挙げない。"
                "stated_preferences は本人が前に話した希望(行動データより優先する)。"
                + PROFILE_NOTE + TRACK_NOTE + RESUME_NOTE,
    }


def recent_applications(state: State, line_user_id: str) -> list[dict]:
    jobs, at = load_jobs(), now(line_user_id)
    out = []
    for a in _recent(behavior.view(state, line_user_id)["applications"], "applied_at", at):
        j = jobs.get(a["job_id"])
        out.append({"job_id": a["job_id"], "title": j.title if j else "", "applied": _md(a["applied_at"]),
                    "days_ago": _days_ago(a["applied_at"], at), "status": a["status"]})
    return out


def form_exit(state: State, line_user_id: str) -> dict | None:
    jobs, at = load_jobs(), now(line_user_id)
    e = behavior.view(state, line_user_id)["form_exit"]  # 要約に載る離脱は最後の 1 件
    if not e or _days_ago(e["exited_at"], at) > 7:
        return None
    j = jobs.get(e["job_id"])
    return {"job_id": e["job_id"], "title": j.title if j else "", "exited_at": e["exited_at"],
            "completed_fields": list(e["completed"]), "remaining_fields": e["remaining"],
            "remaining_in_talk_ok": [f for f in e["remaining"] if f not in PII_FIELDS],
            "remaining_needs_liff": [f for f in e["remaining"] if f in PII_FIELDS]}


# ----------------------------- 推薦 -----------------------------

# 求人の職種は雇用区分ごとに決まった一覧(ADR-0002)。job_type はその中の1つと完全に一致させる(部分一致はしない)。
# アルバイト・パートは12種類
JOB_TYPES = ("アパレル販売", "イベントスタッフ", "オフィス事務", "カフェ", "コンビニ", "コールセンター", "スーパー・販売",
             "倉庫・軽作業", "塾講師・家庭教師", "配送・デリバリー", "飲食キッチン", "飲食ホール")
# 正社員は10種類。アルバイト・パートの職種と同じ名前を使わない(職種の名前だけで区分が決まるように)
FULLTIME_JOB_TYPES = ("営業", "企画・PM", "エンジニア", "事務・管理", "販売・サービス", "製造・技術", "医療・介護", "教育",
                      "クリエイティブ", "物流")
JOB_TYPES_BY_EMPLOYMENT = {"アルバイト・パート": JOB_TYPES, "正社員": FULLTIME_JOB_TYPES}


def _employment_type(employment_type) -> str | None:
    """求人を絞る雇用区分。未定(「未定」・指定なし)なら None(両方の区分から出す)。"""
    return None if not employment_type or employment_type == "未定" else employment_type


def _employment_type_error(employment_type) -> dict | None:
    if employment_type is None or employment_type in EMPLOYMENT_TYPES:
        return None
    return {"error": f"employment_type「{employment_type}」は使えません。{' / '.join(EMPLOYMENT_TYPES)} のどちらかを渡すか、"
                     "区分が未定なら指定しない(両方の区分から探す)",
            "employment_types": list(EMPLOYMENT_TYPES)}


def _job_type_error(job_type, employment_type=None) -> dict | None:
    """一覧に無い職種なら、0件で返さずエラーと一覧を返す(エージェントが指定し直せるように)。
    雇用区分を指定したときは、その区分の一覧の中の完全一致だけを許す。"""
    by_type = ({employment_type: JOB_TYPES_BY_EMPLOYMENT[employment_type]} if employment_type
               else JOB_TYPES_BY_EMPLOYMENT)
    if not job_type or any(job_type in types for types in by_type.values()):
        return None
    where = f"{employment_type}の" if employment_type else ""
    return {"error": f"職種「{job_type}」は{where}一覧にありません。job_types の中から選び、複数に当たるなら1つずつ検索し直してください",
            "job_types": {k: list(v) for k, v in by_type.items()}}


# 求人のエリアは決まった11の地名(東京8・神奈川2・埼玉1)。area はこの中の地名か、求人の最寄り駅名と一致させる
AREAS = ("新宿", "渋谷", "池袋", "品川", "上野", "中野", "高円寺", "吉祥寺", "横浜", "川崎", "大宮")
TOKYO_AREAS = AREAS[:8]  # 東京都の地名(横浜・川崎は神奈川、大宮は埼玉)
# 「東京都内」のような広い言い方は、その範囲の地名に広げて検索する(黙って0件にしない)
AREA_GROUPS = {
    "東京都内": TOKYO_AREAS, "都内": TOKYO_AREAS, "東京": TOKYO_AREAS, "東京都": TOKYO_AREAS,
    "神奈川": ("横浜", "川崎"), "神奈川県": ("横浜", "川崎"), "埼玉": ("大宮",), "埼玉県": ("大宮",),
    "どこでも": AREAS, "指定なし": AREAS,
}


def _area_error(area, jobs: dict[str, BaitoJob]) -> dict | None:
    """一覧に無いエリアなら、0件で返さずエラーと一覧を返す(職種の _job_type_error と同じ形)。"""
    if not area or area in AREA_GROUPS:
        return None
    name = area.removesuffix("駅")
    if name in AREAS or any(name == j.station.removesuffix("駅") for j in jobs.values()):
        return None
    return {"error": f"エリア「{area}」の求人の取り扱いはありません。areas の地名か、求人の最寄り駅名で指定し直してください。"
                     f"「{'」「'.join(AREA_GROUPS)}」のような広い言い方はそのまま使えます。"
                     "エリアを決めていない人には、area を外して検索してください",
            "areas": list(AREAS)}


def _area_set(area) -> set[str] | None:
    """area を、求人の area・station(「駅」を除いた形)と照合する地名の集まりに直す。"""
    if not area:
        return None
    if area in AREA_GROUPS:
        return set(AREA_GROUPS[area])
    return {area.removesuffix("駅")}


# 求人の属性(人と話す量・体を使う量。1〜3)と、正社員の働き方・休日で絞る引数(task 111)。
# recommend / relax / MCP の recommend_jobs・relax_conditions / 音声の voice_resume/jobs.py で同じ名前を使う(テストで同期を確認)
ATTRIBUTE_ARGS = ("max_talk_level", "min_talk_level", "max_physical_level", "min_physical_level")
FULLTIME_STYLE_ARGS = ("remote", "weekends_holidays", "max_overtime_hours")
LEVELS = (1, 2, 3)
# remote の値 → 出す求人の work_style.remote。可 = 原則在宅の求人だけ、一部可 = 週に何日か以上在宅できる求人(可を含む)
REMOTE_CHOICES = {"可": ("可",), "一部可": ("可", "一部可")}
# 属性で絞ったときに理由に出す一言(求人に付けた属性の値の言い換え。事実はこの値だけ)
TALK_REASONS = {1: "人と話すことが少ない仕事", 2: "お客様と話すことは少ない仕事", 3: "人と話すのが中心の仕事"}
PHYSICAL_REASONS = {1: "座り仕事が中心", 2: "力仕事は少ない仕事", 3: "体を動かす仕事"}


def _filters_error(filters: dict) -> dict | None:
    """属性・働き方・休日の引数の値の誤り。0件で返さずエラーと使える値を返す(職種・エリアと同じ形)。"""
    for k in ATTRIBUTE_ARGS:
        v = filters.get(k)
        if v is not None and (type(v) is not int or v not in LEVELS):
            levels = TALK_LEVELS if "talk" in k else PHYSICAL_LEVELS
            return {"error": f"{k}「{v}」は使えません。1〜3 の数字で渡してください",
                    "levels": {str(n): t for n, t in levels.items()}}
    for what in ("talk", "physical"):
        lo, hi = filters.get(f"min_{what}_level"), filters.get(f"max_{what}_level")
        if lo and hi and lo > hi:
            return {"error": f"min_{what}_level({lo})が max_{what}_level({hi})より大きいので、合う求人がありません。どちらかを外してください"}
    if filters.get("remote") and filters["remote"] not in REMOTE_CHOICES:
        return {"error": f"remote「{filters['remote']}」は使えません。{' / '.join(REMOTE_CHOICES)} のどちらかを渡すか、"
                         "リモートの希望が無ければ指定しない", "remote": list(REMOTE_CHOICES)}
    if filters.get("max_overtime_hours") is not None and filters["max_overtime_hours"] < 0:
        return {"error": "max_overtime_hours は月の残業時間の上限(0以上の整数)で渡してください"}
    return None


def _match_filters(job: BaitoJob, f: dict) -> bool:
    """属性(両方の区分)と、正社員の働き方・休日(正社員の求人だけが持つ。指定するとアルバイト・パートの求人は出ない)。"""
    if f.get("max_talk_level") and job.talk_level > f["max_talk_level"]:
        return False
    if f.get("min_talk_level") and job.talk_level < f["min_talk_level"]:
        return False
    if f.get("max_physical_level") and job.physical_level > f["max_physical_level"]:
        return False
    if f.get("min_physical_level") and job.physical_level < f["min_physical_level"]:
        return False
    w, h = job.work_style, job.holidays
    if f.get("remote") and (w is None or w.remote not in REMOTE_CHOICES[f["remote"]]):
        return False
    if f.get("weekends_holidays") and (h is None or not h.weekends_holidays):
        return False
    return f.get("max_overtime_hours") is None or (w is not None and w.overtime_hours <= f["max_overtime_hours"])


def _match(job: BaitoJob, areas: set[str] | None, time_band, job_type, min_wage,
           employment_type=None, min_annual_salary=None, filters: dict | None = None) -> bool:
    if filters and not _match_filters(job, filters):
        return False
    if employment_type and job.employment_type != employment_type:
        return False
    if areas and job.area not in areas and job.station.removesuffix("駅") not in areas:
        return False
    if time_band and time_band not in job.time_bands:  # 正社員の求人は time_bands が空なので、時間帯を指定すると出ない
        return False
    if job_type and job_type != job.job_type:
        return False
    if min_wage and (job.pay.kind != "時給" or job.pay.min < min_wage):  # 時給の下限は時給の求人だけに効く
        return False
    if min_annual_salary:  # 年収の下限(万円)は、年収・月給の求人の年収の幅の中央値と比べる(時給の求人は出ない)
        mid = annual_pay_mid(job.pay)
        if mid is None or mid < min_annual_salary * 10000:
            return False
    return True


def _style_words(job: BaitoJob) -> list[str]:
    """正社員の働き方・休日を、部分一致で当たる言葉にする。働き方・休日は引数(remote など)で絞るが、keywords に
    「在宅」「土日祝休み」が入ったときも、意味検索が使えないときの部分一致で当たるように(求人の文面には書いていない)。"""
    w, h = job.work_style, job.holidays
    out = []
    if w:
        out += {"可": ["リモート可", "在宅勤務"], "一部可": ["一部リモート可", "一部在宅"], "不可": []}[w.remote]
        out += ["フレックス"] if w.flex else []
        out += ["残業少なめ"] if w.overtime_hours <= 10 else []
    if h and h.weekends_holidays:
        out += ["土日祝休み", "土日休み"]
    return out


def _keyword_hit(job: BaitoJob, keywords: str) -> bool:
    """keywords の部分一致(空白区切りのどれかが求人の文面か働き方・休日の言葉に含まれる)。意味検索が使えないときの唯一の照合。"""
    text = f"{job.title} {job.job_type} {job.description} {' '.join(job.perks)} {' '.join(_style_words(job))}"
    return any(k in text for k in keywords.split())


# keywords の意味検索(embedding)の選び方。値は検索評価セットの上位3件の再現率で選んだ(task 105、evals/search_eval.py)。
# 類似度の絶対値は問い合わせによって大きく揺れ(評価セットの最高値で 0.23〜0.57)、関係の無い問い合わせ
# (「宇宙飛行士になりたい」など)でも 0.2〜0.27 まで上がるので、絶対値の下限では当たり外れを分けられない。
# そこで、その問い合わせでの最高値からの相対で選び、下限(SIM_FLOOR)は低めにする。ただし最高値が低い問い合わせでは
# 下限の方が効く: 正社員の「体を動かしたい」は最高 0.228 で 0.7 倍が 0.16 なので、0.20 の下限が介護職員(0.191)・
# ルート営業(0.192)を落とし、2件しか出ない。0.18 なら評価セット全体で 0.753 → 0.763 だが、0.17 で UI/UX デザイナーが
# 入るなど1件の類似度の差で入れ替わる(正社員の求人はどれも 0.18〜0.23 で、体を使う仕事とそうでない仕事を
# embedding が分けられていない)ので、0.20 のままにした(比較は evals/search_eval.py の SELECTIONS)
SIM_FLOOR = 0.20      # これ未満は意味が近いとみなさない(0.30 だとアルバイト・パートの「体を動かしたい」の正解 0.23 まで落ちた)
SIM_RELATIVE = 0.7    # 最高値の7割以上を「意味が近い」とする
SIM_TOP = 10          # 意味の近さで拾う上限件数(total が水増しにならないように)
# score_breakdown の keywords = SIM_WEIGHT × 類似度。上位の求人どうしの類似度の差は 0.05 ほどなので、重みが小さい(6)と
# 駅からの近さ(0〜1点)・給与(0.3〜1.2点)の差に埋もれ、意味の近い求人が上位3件に入らなかった
SIM_WEIGHT = 24
# 意味の近さを使うのは、全求人の中で最高値が中央値から SIM_PEAK 以上抜けている問い合わせだけ。抜けていない問い合わせは
# どの求人とも同じくらい近い(= 求人を分けられない)ので、意味の近さを絞り込みにも並べ替えにも使わず、部分一致だけにする
# (API が使えないときと同じ)。task 111: 求人の情報に無い希望が keywords に入ると、近いだけの最大10件が SIM_WEIGHT の点で
# 職種の傾向(2点)より上に並んでいた。最高値の絶対値では分けられない(「アットホームな職場」は 0.467 で、関係のある
# 「車の運転が好き」0.365 より高い)が、最高値と中央値の差なら、評価セットの無関係な問い合わせ(「宇宙飛行士になりたい」0.094・
# 「人間関係が良い職場」0.087・「アットホームな職場」0.113)と、keywords で探す問い合わせ(最小は「電話対応の経験を
# 活かしたい」0.133)が分かれる。差の小さかった言い換え・否定(「体を動かしたい」0.096・「人と話すのが苦手」0.094・
# 「立ち仕事は避けたい」0.092)は、属性の引数(min_physical_level など)で探すようにした。境目の余裕は 0.01〜0.02 と小さいので、
# 問い合わせを足したら evals/search_eval.py で測り直す
SIM_PEAK = 0.12


def _keyword_filter(cands: list[BaitoJob], keywords: str,
                    jobs: dict[str, BaitoJob]) -> tuple[list[BaitoJob], dict[str, float] | None, bool]:
    """keywords で候補を絞る。意味検索(embedding)が使えて、問い合わせが求人を分けられる(SIM_PEAK)なら
    「部分一致 or 意味が近い」、使えないか分けられなければ従来の部分一致だけ(検索は止めない)。
    類似度は並べ替え用に返す(部分一致だけのときは None)。3つ目は、意味検索は使えたが求人を分けられなかったか。
    jobs は全求人(問い合わせの分けやすさを測る)。"""
    qvec = embedding.embed_query(keywords)
    every = embedding.similarities(qvec, jobs.values()) if qvec else {}
    peak = max(every.values()) - median(every.values()) if every else None
    if not every or peak < SIM_PEAK:
        kept = [j for j in cands if _keyword_hit(j, keywords)]
        _trace_keywords(keywords, peak, False, cands, kept)
        return kept, None, bool(every)
    sims = {j.job_id: every[j.job_id] for j in cands if j.job_id in every}
    floor = max(SIM_FLOOR, max(sims.values(), default=0.0) * SIM_RELATIVE)
    near = {jid for jid, s in sorted(sims.items(), key=lambda x: -x[1])[:SIM_TOP] if s >= floor}
    kept = [j for j in cands if j.job_id in near or _keyword_hit(j, keywords)]
    _trace_keywords(keywords, peak, True, cands, kept)
    return kept, sims, False


def _trace_keywords(keywords: str, peak: float | None, semantic: bool, cands: list[BaitoJob],
                    kept: list[BaitoJob]) -> None:
    """評価(evals/run.py)だけが使う記録: 環境変数 DIP_KEYWORDS_TRACE のファイルに、keywords の検索1回を1行の JSON で足す
    (SIM_PEAK の余裕を実際の会話で見るため。task 109)。peak は全求人の類似度の最高値 - 中央値(意味検索が使えなければ None)、
    semantic は意味の近さを使ったか(False なら部分一致だけ)。ツールの結果は変えない(丸めるのは記録する値だけで、
    SIM_PEAK の判定は丸める前の値で行う)。"""
    path = os.environ.get("DIP_KEYWORDS_TRACE")
    if not path:
        return
    hits = sum(1 for j in kept if _keyword_hit(j, keywords))
    row = {"keywords": keywords, "peak": None if peak is None else round(peak, 3), "semantic": semantic,
           "candidates": len(cands), "kept": len(kept), "word_hits": hits}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _reasons(job: BaitoJob, area, time_band, job_type, acts: dict, jobs: dict[str, BaitoJob], at: datetime,
             filters: dict | None = None) -> list[str]:
    """推薦理由。指定された条件か、90日以内の本人の行動(acts は behavior.view の形)に基づく事実だけを並べる。
    属性(人と話す量・体を使う量)で絞ったときは、本人が避けたい・やりたいと言ったことへの答えなので先頭に置く。"""
    f = filters or {}
    reasons = []
    if f.get("max_talk_level") or f.get("min_talk_level"):
        reasons.append(TALK_REASONS[job.talk_level])
    if f.get("max_physical_level") or f.get("min_physical_level"):
        reasons.append(PHYSICAL_REASONS[job.physical_level])
    for a in reversed(_recent(acts.get("applications", []), "applied_at", at)):  # 古い応募から
        past = jobs.get(a["job_id"])
        if past and past.job_type == job.job_type and past.job_id != job.job_id:
            d = parse_ts(a["applied_at"])
            reasons.append(f"{d.month}月に応募した{past.job_type}と同系統")
            break
    if time_band and time_band in job.time_bands:
        reasons.append(f"{time_band}の勤務({job.shift})")
    if job.walk_min <= 5:
        reasons.append(f"{job.station.removesuffix('駅')}駅から徒歩{job.walk_min}分")
    elif area:
        reasons.append(f"{job.area}エリア")
    if job_type and job_type == job.job_type and not reasons[:1]:
        reasons.append(f"希望の{job.job_type}")
    reasons += _fulltime_reasons(job)
    good = [p for p in job.perks if p in ("未経験OK", "日払い", "シフト自由", "短時間OK")]
    if good:
        reasons.append(good[0])
    return reasons[:2] or [pay_text(job.pay)]


def _fulltime_reasons(job: BaitoJob) -> list[str]:
    """正社員の求人の働き方・休日のうち、理由に挙げられる事実(アルバイト・パートの求人では空)。"""
    out = []
    if job.work_style and job.work_style.remote != "不可":
        out.append("リモート可" if job.work_style.remote == "可" else "一部リモート可")
    if job.holidays and job.holidays.weekends_holidays:
        out.append(f"土日祝休み(年間休日{job.holidays.annual_days}日)")
    if job.work_style and job.work_style.overtime_hours <= 10:
        out.append(f"残業は月{job.work_style.overtime_hours}時間ほど")
    return out


def job_card(job: BaitoJob, reasons: list[str]) -> dict:
    """LINE の求人カードに出す形。dip が求人を返すときは、どのツールでもこの形を使う(LINE 側はこれだけを描画する)。
    給与は pay({kind: 時給|月給|年収, min, max}、円)と、その表示の pay_text(年収450万〜650万円 など)。days_min はアルバイト・パートだけ(正社員は None)。
    働き方・休日は正社員の求人だけに付く。"""
    card = {"job_id": job.job_id, "title": job.title, "employer": job.employer,
            "employment_type": job.employment_type, "job_type": job.job_type,
            "area": job.area, "station": job.station, "walk_min": job.walk_min,
            "pay": job.pay.model_dump(), "pay_text": pay_text(job.pay),
            "shift": job.shift, "days_min": job.days_min, "perks": job.perks, "reasons": reasons}
    if job.work_style:
        card["work_style"] = job.work_style.model_dump()
    if job.holidays:
        card["holidays"] = job.holidays.model_dump()
    return card


def _wage_score(pay: Pay) -> float:
    """給与の高さの点数(並べ替え用)。時給は2,000円、年収・月給は年収800万円(の見込みの真ん中)を1点にそろえる
    (区分が未定で両方を並べるときに、どちらかの区分だけが上に偏らないように)。"""
    if pay.kind == "時給":
        return round(pay.min / 2000, 2)
    return round(annual_pay_mid(pay) / 8_000_000, 2)


# 希望年収を指定した検索では、給与の高さを並びに加味する(2026-10-03 のユーザーの決定)。希望の下限を超える分の
# 100万円ごとに1点(上限 SALARY_MAX 点)。wage(年収800万円で1点)より強く効かせる(年収を指定した人は給与の高さを
# 重く見ている)。上限の3点は職種の傾向(1番目 2点)より少し重いが、下限を200万円以上超える差が無ければ傾向が勝つ
SALARY_PER_POINT = 1_000_000
SALARY_MAX = 3.0


def _salary_score(pay: Pay, min_annual_salary: int) -> float:
    mid = annual_pay_mid(pay) or 0
    return round(min(SALARY_MAX, max(0.0, (mid - min_annual_salary * 10000) / SALARY_PER_POINT)), 2)


def match_breakdown(job: BaitoJob, tend: dict, mixed: bool = False) -> dict[str, float]:
    """求人と本人の傾向の合い具合(job_match_score の内訳)。LLM を使わず決定的に計算する。
    条件(エリア・時間帯など)に合うかは recommend の絞り込みで見るので、ここは並べ替えのための点数。
    mixed は両方の雇用区分の求人を混ぜて並べるとき(区分が未定で、候補に両方が残ったとき)。正社員の求人には時間帯が無く time_band の点が必ず0になって
    アルバイト・パートだけが上に偏るので、時間帯の傾向は点にしない(どの求人も0)。"""
    type_rank = tend["job_type"].index(job.job_type) if job.job_type in tend["job_type"] else None
    return {
        "job_type": 2.0 if type_rank == 0 else 1.0 if type_rank == 1 else 0.0,  # 傾向の1番目の職種 2点、2番目 1点
        "time_band": 1.0 if not mixed and set(job.time_bands) & set(tend["time_band"]) else 0.0,
        "area": 1.0 if job.area in tend["area"] else 0.0,
        "station": round(max(0, 10 - job.walk_min) / 10, 2),  # 駅から近いほど高い(徒歩10分以上は0)
        "wage": _wage_score(job.pay),
    }


def recommend(state: State, line_user_id: str | None, area=None, time_band=None, job_type=None, min_wage=None,
              keywords=None, exclude_ids: list[str] | None = None, limit: int = MAX_JOBS,
              posted_within_days: int | None = None, employment_type=None, min_annual_salary=None,
              **filters) -> dict:
    """employment_type(アルバイト・パート / 正社員)で区分を絞る。未定(指定なし・「未定」)なら両方の区分から出す。
    min_annual_salary は希望年収の下限(万円)。年収・月給の求人の年収の中央値と比べ、給与の高さを並びにも加味する。
    filters は属性(ATTRIBUTE_ARGS)と正社員の働き方・休日(FULLTIME_STYLE_ARGS)。"""
    if unknown := set(filters) - {*ATTRIBUTE_ARGS, *FULLTIME_STYLE_ARGS}:
        raise TypeError(f"recommend() に知らない引数: {sorted(unknown)}")
    filters = {k: v for k, v in filters.items() if v is not None and v is not False}
    employment_type = _employment_type(employment_type)
    if err := (_employment_type_error(employment_type) or _job_type_error(job_type, employment_type)
               or _filters_error(filters)):
        return err
    jobs = load_jobs()  # 傾向と理由(過去の応募との比較)は全体の求人から数える
    if err := _area_error(area, jobs):
        return err
    areas = _area_set(area)
    at = now(line_user_id)
    pool = jobs
    if posted_within_days is not None:  # 候補だけを新着に絞る
        # 掲載開始の日付は fixtures のシミュレーションの値で、SIM_NOW から見て書いてある。誰が探しても SIM_NOW から数える
        listings, listed_at = load_listings(), now(None)
        pool = {k: j for k, j in jobs.items()
                if "posted_at" in listings.get(k, {})
                and _days_ago(listings[k]["posted_at"], listed_at) <= posted_within_days}
    acts = behavior.view(state, line_user_id)
    exclude = set(exclude_ids or []) | set(acts["applied_job_ids"])  # 応募済みの求人は出さない(要約の新しい順の 30 件)
    tend = acts["tendency"] if behavior.dip_id(state, line_user_id) else {"job_type": [], "area": [], "time_band": []}
    cands = [j for j in pool.values() if j.job_id not in exclude
             and _match(j, areas, time_band, job_type, min_wage, employment_type, min_annual_salary, filters)]
    sims, unmatched = None, False
    if keywords:
        cands, sims, unmatched = _keyword_filter(cands, keywords, jobs)
    hits = []
    mixed = len({j.employment_type for j in cands}) > 1
    for j in cands:
        b = match_breakdown(j, tend, mixed=mixed)
        if sims is not None:
            b["keywords"] = round(SIM_WEIGHT * sims.get(j.job_id, 0.0), 2)
        if min_annual_salary:
            b["salary"] = _salary_score(j.pay, min_annual_salary)
        hits.append((round(sum(b.values()), 2), b, j))
    hits.sort(key=lambda h: h[0], reverse=True)
    top = [job_card(j, _reasons(j, area, time_band, job_type, acts, jobs, at, filters))
           | {"match_score": score, "score_breakdown": b}
           for score, b, j in hits[:max(1, min(limit, MAX_JOBS))]]
    out = {"total": len(hits), "jobs": top}
    if unmatched and not hits:  # 言い換えを keywords に入れて0件になったとき、引数で探し直せるように
        out["keywords_note"] = (f"keywords「{keywords}」に意味の近い求人はなく、語が一致した求人もありませんでした。"
                                "人と話す量・体を使う量・リモート・土日祝休み・残業の希望なら、keywords から外して "
                                "max_talk_level・min_physical_level・remote などの引数で探し直してください。"
                                "それ以外の希望(求人の情報に無いこと)なら、relax_conditions で外す案を示してください")
    return out


def relax(state: State, line_user_id: str | None, area=None, time_band=None, job_type=None, min_wage=None,
          keywords=None, employment_type=None, min_annual_salary=None, exclude_ids: list[str] | None = None,
          **filters) -> dict:
    """条件を1つずつ外したら何件になるか。0件・少数のときの提案材料。雇用区分は外す候補にしない(本人が決めた探し方)。
    属性・働き方・休日の引数(filters)も外す候補にする。exclude_ids(出さない求人)は外す候補にせず、どの件数からも除く。"""
    if unknown := set(filters) - {*ATTRIBUTE_ARGS, *FULLTIME_STYLE_ARGS}:
        raise TypeError(f"relax() に知らない引数: {sorted(unknown)}")
    employment_type = _employment_type(employment_type)
    if err := (_employment_type_error(employment_type) or _job_type_error(job_type, employment_type)
               or _area_error(area, load_jobs()) or _filters_error(filters)):
        return err
    cond = {"area": area, "time_band": time_band, "job_type": job_type, "min_wage": min_wage,
            "min_annual_salary": min_annual_salary, "keywords": keywords}
    cond |= {k: filters.get(k) for k in (*ATTRIBUTE_ARGS, *FULLTIME_STYLE_ARGS)}
    base = recommend(state, line_user_id, **cond, employment_type=employment_type, exclude_ids=exclude_ids)["total"]
    options = []
    for k, v in cond.items():
        if v is not None and v is not False and v != "":  # max_overtime_hours=0 も外す候補にする
            n = recommend(state, line_user_id, **{**cond, k: None}, employment_type=employment_type,
                          exclude_ids=exclude_ids)["total"]
            if n > base:
                options.append({"remove": k, "current": v, "total_after": n})
    options.sort(key=lambda o: o["total_after"], reverse=True)
    return {"total_now": base, "options": options[:3]}


# ----------------------------- 相談(FAQ / 求人) -----------------------------

_MARKS = "\\s\u3001\u3002!?\uff01\uff1f\u30fb~\u301c\uff5e\u2026\u300c\u300d()\uff08\uff09"  # 空白・句読点・!?(半角/全角)など
_KANA_ONLY = re.compile(f"^[\u3040-\u309f\u30fc{_MARKS}]+$")
_PUNCT = re.compile(f"[{_MARKS}]+")
# ひらがなだけの質問(「まかないはある?」)で使う3文字の組から外す、どの文にも出る言い回し
_KANA_STOP = {"ありま", "ります", "ますか", "ですか", "でしょ", "しょう", "ません", "いです", "ください", "くださ", "ださい",
              "できま", "きます", "ますよ", "ないで", "ている", "いますか", "います", "ますね", "ですね", "ある"}


def _bigrams(s: str) -> set[str]:
    """照合に使う2文字の組。ひらがなだけの組(「あり」「ます」など、どの文にも出る)は外す。"""
    s = _PUNCT.sub("", s)
    return {s[i:i + 2] for i in range(len(s) - 1) if not _KANA_ONLY.match(s[i:i + 2])}


def _kana_trigrams(s: str) -> set[str]:
    """漢字・カタカナを含まない質問のための、ひらがな3文字の組(よくある言い回しは外す)。"""
    s = _PUNCT.sub("", s)
    return {s[i:i + 3] for i in range(len(s) - 2)} - _KANA_STOP


def _sentences(text: str) -> list[str]:
    return [x + "。" for x in re.split(r"。", text) if x.strip()]


MIN_OVERLAP = 0.25  # 質問の組の4分の1以上が重なったものだけを当たりにする


def _job_facts(job: BaitoJob) -> list[str]:
    """求人の項目を、答えの材料になる短い文にする(求人の説明)。正社員の項目は、ある求人だけ。"""
    days = f"(週{job.days_min}日〜)" if job.days_min else ""
    facts = [f"勤務時間: {job.shift}{days}", f"待遇: {'・'.join(job.perks) or 'なし'}",
             f"{pay_text(job.pay)}、{job.station.removesuffix('駅')}駅から徒歩{job.walk_min}分"]
    if job.required_experience:
        facts.append(f"必須の経験・資格: {'・'.join(job.required_experience)}")
    if job.preferred_experience:
        facts.append(f"歓迎の経験・資格: {'・'.join(job.preferred_experience)}")
    if w := job.work_style:
        facts.append(f"働き方: リモートワーク{w.remote}、フレックス{'あり' if w.flex else 'なし'}、"
                     f"残業は月{w.overtime_hours}時間ほど")
    if h := job.holidays:
        facts.append(f"休日: 年間休日{h.annual_days}日、"
                     + ("土日祝休み" if h.weekends_holidays else "シフト制など(土日祝が休みとは限らない)"))
    if t := job.employment_terms:
        facts.append(f"試用期間: {t.probation_months}か月" + (f"({t.notes})" if t.notes else "")
                     if t.probation_months else "試用期間: なし")
    return facts


def answer_material(question: str, job_id: str | None = None) -> dict:
    """質問に答える材料を、出典つきで返す(回答文の生成は LINE 側 LLM が行う = RAG の Retrieval 部分)。
    job_id があれば、その求人の Q&A → 求人の説明(仕事内容・大変なところ・待遇・シフト)→ 全体の FAQ の順に探す。
    どれにも当たらなければ note で「情報にない」と返し、推測で答えさせない。"""
    # 質問の組のうち、どれだけが重なったかの割合で測る(1つだけ偶然重なったもの〔「行け」など〕を当たりにしない)
    qb = _bigrams(question)
    grams, split = (qb, _bigrams) if qb else (_kana_trigrams(question), _kana_trigrams)  # ひらがなだけなら3文字の組
    score = lambda text: len(grams & split(text)) / len(grams) if grams else 0
    hits: list[dict] = []
    out: dict = {}
    if job_id:
        jobs = load_jobs()
        job = jobs.get(job_id)
        if job:
            for f in load_job_faq().get(job_id, []):
                hits.append({"source": "案件の FAQ", "q": f["q"], "a": f["a"], "score": score(f["q"] + f["a"])})
            facts = _sentences(job.description) + _sentences(job.drawback) + _job_facts(job)
            for t in facts:
                hits.append({"source": "求人の説明", "a": t, "score": score(t)})
            out["job"] = job.model_dump()
            out["card"] = job_card(job, _reasons(job, None, None, None, {}, jobs, now(None)))
    for f in json.loads((FIXTURES / "faq.json").read_text(encoding="utf-8")):
        hits.append({"source": "全体の FAQ", "id": f["id"], "q": f["q"], "a": f["a"], "score": score(f["q"] + f["a"])})
    # その求人の答え(案件の FAQ・求人の説明)を、全体の FAQ より先に並べる(全体の FAQ は一般論で、求人と食い違うことがある)
    order = {"案件の FAQ": 0, "求人の説明": 1, "全体の FAQ": 2}
    # よく合うもの(質問の組の半分以上が重なる)を先に。その中では求人自身の答えを優先する
    hits = sorted((h for h in hits if h["score"] >= MIN_OVERLAP),
                  key=lambda h: (h["score"] < 0.5, order[h["source"]], -h["score"]))[:4]
    out["hits"] = [{k: v for k, v in h.items() if k != "score"} for h in hits]
    if not hits:
        # 言い換えた長い質問は組の重なりが薄まって当たらないので、求人の項目そのものも答えの材料にさせる
        job_note = "ただし job(この求人の説明・時給・勤務時間・待遇など)に答えがあれば、それで答える。それにも無ければ、" if "job" in out else ""
        out["note"] = (f"この質問に答える情報にない(案件の FAQ・求人の説明・全体の FAQ のどれにも無い)。{job_note}推測で答えず、"
                       "分からないと伝え、応募の画面や面接で確認できると案内する。")
    return out


def application_status(state: State, line_user_id: str, job_id: str | None = None) -> dict:
    """as_of は選考の状態をいつ時点の要約から読んだか(連携していない人は None)。"""
    apps = recent_applications(state, line_user_id)
    if job_id:
        apps = [a for a in apps if a["job_id"] == job_id]
    faq = {f["id"]: f["a"] for f in json.loads((FIXTURES / "faq.json").read_text(encoding="utf-8"))}
    return {"applications": apps, "as_of": behavior.view(state, line_user_id)["as_of"], "reference": faq["faq-contact"]}


# ----------------------------- 手続き(フォームの続き) -----------------------------

def resume_form(state: State, line_user_id: str, job_id: str, answers: dict[str, str]) -> dict:
    """トークで受け取れる項目(個人情報以外)だけを反映し、残りは LIFF 画面の URL を返す。
    個人情報の項目に加え、どの項目でも値に電話番号・メールアドレスが含まれていれば受け取らない。
    反映は、答えた項目名を元データに form_fill のイベントで書く(値は持たない。シミュレーションなので値を使う先が無い)。"""
    rejected = [k for k, v in answers.items() if k in PII_FIELDS or has_pii(str(v))]
    e = behavior.view(state, line_user_id)["form_exit"]
    if not e or e["job_id"] != job_id:
        return {"error": f"{job_id} の入力途中の応募フォームはありません"}
    filled = [k for k in answers if k in e["remaining"] and k not in rejected]
    if filled:
        behavior.record(state, line_user_id, "form_fill", job_id, fields=filled)
    remaining = [f for f in e["remaining"] if f not in filled]
    out = {"job_id": job_id, "remaining_fields": remaining,
           "rejected_pii": rejected,
           "note": "個人情報はトークで受け取らない。LIFF 画面の URL をボタンで案内する。" if rejected else ""}
    if remaining:
        out["liff_url"] = liff_url(LIFF_BASE, line_user_id, job=job_id)
    return out


def submit_application(state: State, line_user_id: str, job_id: str) -> None:
    """LIFF 画面で残りの項目が入力されたら応募を確定する(トークではなく LIFF から呼ばれる)。元データに apply を書く
    (その求人の離脱は、要約でも重ねた分でも消える)。"""
    behavior.record(state, line_user_id, "apply", job_id, status="応募済み(店舗からの連絡待ち)")


def save_job(state: State, line_user_id: str, job_id: str) -> dict:
    """保存を元データに書く。要約は次のバッチまで変わらないが、この会話で書いた分を重ねて読むので、すぐ後の
    saved_jobs・要約・プロフィールに出る。もう保存してあれば書かない。"""
    if job_id not in {s["job_id"] for s in behavior.view(state, line_user_id)["saves"]}:
        behavior.record(state, line_user_id, "save", job_id)
    return {"saved": job_id, "saves_total": behavior.view(state, line_user_id)["saves_total"]}


def saved_jobs(state: State, line_user_id: str) -> list[dict]:
    """保存した求人を新しい順に、求人カードの形で(本番のトーク画面の「保存した求人」)。掲載が無くなった求人は出さない。
    要約に載るのは新しい順に 10 件(summarize.SAVES)と、この会話で保存した分。"""
    jobs = load_jobs()
    saves = behavior.view(state, line_user_id)["saves"]
    return [job_card(jobs[s["job_id"]], []) | {"saved_at": _md(s["at"])} for s in saves if s["job_id"] in jobs]


# ----------------------------- トークを開いた(初回訪問か再訪か) -----------------------------


def talk_entry(state: State, line_user_id: str) -> str:
    """本番のトーク画面で新しい会話を始めるときの入口のイベント(初回訪問 visit / 再訪 revisit)。記録はしない。
    前に開いた記録(挨拶が返った記録)が無ければ初回訪問。LINE 側はこの結果の挨拶から会話を始める。"""
    return "revisit" if state.user(line_user_id).get("talk_opens") else "visit"


def record_talk_open(state: State, line_user_id: str) -> None:
    """トーク画面を開いて挨拶が返ったことを記録する(次からは再訪)。挨拶が失敗・上限で返らなかったときは呼ばない
    (呼ぶと、次に開いたとき会ってもいない相手に「おかえりなさい」と言ってしまう)。"""
    state.update(line_user_id, lambda u: u.__setitem__(
        "talk_opens", [*u.get("talk_opens", []), now(line_user_id).isoformat()][-20:]))  # 判定に要るのは有無だけなので短く保つ


# ----------------------------- 本人が話した希望と、本人について(プロフィール) -----------------------------

# experience / skills / certifications の分け方(task 41):
#   experience: これまで何をしてきたか(職歴・未経験など。例: コンビニで1年半)
#   skills: 本人が「できる」と言った作業・スキル(例: レジ打ちと品出し)
#   certifications: 持っている資格・免許(例: 普通自動車免許)
# 相談で分かったこと(aica v2 の D。jyoka/tasks#61 を吸収): 区分に共通の purpose〜concerns と、
# 正社員を探すときの条件 min_annual_salary〜start_timing。値は本人の言葉のまま短い文で
PREFERENCE_KEYS = {"area": "エリア・駅", "time_band": "時間帯", "job_type": "職種", "min_wage": "時給の下限(円)",
                   "experience": "経験(未経験など)", "days_per_week": "週の日数",
                   "skills": "できること", "certifications": "資格・免許",
                   "purpose": "目的", "strengths": "強み", "wants": "やりたいこと", "avoids": "避けたいこと",
                   "concerns": "不安", "min_annual_salary": "希望年収の下限", "work_style": "働き方",
                   "start_timing": "働き始める時期"}
# 正社員を探すときだけ聞く条件(アルバイト・パートの相談では聞かない。保存は区分に関係なく受け付ける)
FULLTIME_PREFERENCE_KEYS = ("min_annual_salary", "work_style", "start_timing")
PREFERENCE_VALUE_MAX = 100  # 本人の言葉のまま短い文で。長い値は要約・会話ログの貼り付けになりがちなので保存しない


# 住所らしい値(番地・丁目・「1-2-3」の形・「みほん町3-5」の形)。氏名はコードでは見分けられないので、Prompt で保存させない。
# 幅や日付(「年収400-500万円」「3-6か月以内」「2026-09-30に入社」)は住所ではない: 3つ組は先頭が1〜3桁のものだけ、
# 2つ組は市区町村名の直後で、後ろに単位(万・円・か月・年・日・時など)が続かないものだけを数える
ADDRESS_LIKE = re.compile(
    r"(?<![\d-])\d{1,3}[-－]\d{1,4}[-－]\d{1,4}(?![\d-])"
    r"|[市区町村]\d{1,4}[-－]\d{1,4}(?![\d万千円かカヵヶケ年月日時分歳%％])"
    r"|丁目|番地"
)
_WORD_SEP = set("、。,.!?！？「」()（）")


def address_spans(text: str) -> list[tuple[int, int]]:
    """文の中の住所らしい所の範囲: ADDRESS_LIKE に当たった所を含む、区切り(空白・句読点・括弧)までの語
    (「渋谷区神南1丁目2-3に住む」なら全体。丁目だけ消して番地を残さない)。正規表現で前後の語まで取ると長い文で
    時間がかかるので、当たった所から区切りまで広げる。"""
    def sep(ch: str) -> bool:
        return ch.isspace() or ch in _WORD_SEP

    spans: list[tuple[int, int]] = []
    for m in ADDRESS_LIKE.finditer(text):
        start, end = m.span()
        if spans and start < spans[-1][1]:  # 前の語に含まれている
            continue
        while start > 0 and not sep(text[start - 1]):
            start -= 1
        while end < len(text) and not sep(text[end]):
            end += 1
        spans.append((start, end))
    return spans


def _preference_error(key: str, value) -> str | None:
    """保存しない理由(保存してよければ None)。個人情報らしい値の防御は全項目に効かせる。"""
    if key not in PREFERENCE_KEYS:
        return "unknown_key"
    v = str(value)
    if has_pii(v) or ADDRESS_LIKE.search(fold_pii(v)):
        return "personal_info"
    if len(v.strip()) > PREFERENCE_VALUE_MAX:
        return "too_long"
    return None


def save_preferences(state: State, line_user_id: str, preferences: dict[str, str | int]) -> dict:
    """本人が会話で話した希望と、本人について分かったこと(目的・強みなど)を保存する(次の会話の要約で返す)。
    決まった項目だけ。電話番号・メールアドレス・住所らしい値と、長すぎる値は保存しない。値は文字列にそろえる。"""
    errors = {k: e for k, v in preferences.items() if (e := _preference_error(k, v))}
    rejected = list(errors)
    keep = {k: str(v).strip() for k, v in preferences.items() if k not in rejected and str(v).strip()}

    def apply(u):
        prefs = u.setdefault("preferences", {})
        for k, v in keep.items():
            prefs[k] = {"value": v, "at": now(line_user_id).isoformat()}

    state.update(line_user_id, apply)
    out = {"saved": list(keep), "rejected": rejected}
    if rejected:
        notes = []
        if "unknown_key" in errors.values():
            notes.append(f"保存できるのは {', '.join(PREFERENCE_KEYS)} だけ。")
        if "personal_info" in errors.values():
            notes.append("電話番号・メールアドレス・住所など個人情報を含む値は保存しない。")
        if "too_long" in errors.values():
            notes.append(f"値は{PREFERENCE_VALUE_MAX}字以内。本人の言葉のまま短い文にして保存し直す。")
        out["note"] = "".join(notes)
    return out


# ----------------------------- 雇用区分(ADR-0001) -----------------------------
# 本人の発話を根拠に切り替え、履歴(いつ・どの区分へ・根拠・取り消しか)を残す。初回は未定。
# 合図の強弱(明示・強い合図は1回で切り替え、弱い合図は1問で確かめる)の判断は Prompt に書き、ここでは記録だけする。

TRACKS = ("アルバイト・パート", "正社員", "未定")
DEFAULT_TRACK = "未定"
TRACK_HISTORY_IN_SUMMARY = 5  # 要約に載せる直近の切り替え(取り消しの画面が直前の切り替えを見られるように)
EVIDENCE_MAX = 200


def search_track(user: dict) -> str:
    """今の雇用区分(切り替えの記録が無ければ未定)。"""
    history = user.get("track_history") or []
    return history[-1]["track"] if history else DEFAULT_TRACK


def track_history(user: dict) -> list[dict]:
    """要約に載せる形の、直近の切り替えの履歴(古い順)。"""
    return [{"when": _md(h["at"]), "from": h["from"], "track": h["track"], "evidence": h["evidence"],
             "undo": h["undo"]} for h in (user.get("track_history") or [])[-TRACK_HISTORY_IN_SUMMARY:]]


def set_search_track(state: State, line_user_id: str, track: str, evidence: str, undo: bool = False) -> dict:
    """雇用区分を切り替えて履歴に残す。evidence は根拠になった本人の発言そのまま。
    undo は直前の切り替えの取り消し(track には切り替える前の区分を渡す)。今と同じ区分なら記録しない。"""
    if track not in TRACKS:
        return {"error": f"track は {' / '.join(TRACKS)} のいずれか"}
    evidence = redact_pii(str(evidence or "").strip())[:EVIDENCE_MAX]
    if not evidence:
        return {"error": "evidence に、切り替えの根拠になった本人の発言をそのまま入れてください"
                         "(履歴書の内容や推測だけでは切り替えない)。"}
    result: dict = {}

    def apply(u):
        history = u.get("track_history") or []
        current = search_track(u)
        if undo:
            if not history or history[-1]["undo"]:
                result["error"] = "取り消せる切り替えがありません(切り替えの記録が無いか、直前の記録がすでに取り消し)"
                return
            if track != history[-1]["from"]:
                result["error"] = f"取り消しの track には、切り替える前の区分({history[-1]['from']})を渡してください"
                return
        elif track == current:
            result.update({"track": current, "changed": False})
            return
        # id はトークの切り替えの表示がどの記録かを指す(記録ごと消した・すべて消した後の記録と取り違えないよう、番号ではなく乱数)
        rec = {"id": secrets.token_hex(6), "at": now(line_user_id).isoformat(), "from": current, "track": track, "evidence": evidence,
               "undo": undo}
        u["track_history"] = [*history, rec]
        result.update({"track": track, "changed": True, "recorded": rec})

    state.update(line_user_id, apply)
    return result


# ----------------------------- 相談の playbook(ADR-0001) -----------------------------
# 雇用区分とチャネルごとの、相談の進め方の指針(明らかにすること・掘り下げ方)。次に何を聞くかは決めない。
# 区分ごとの playbook に、区分に共通の決まり(common_{channel}.md)を足して返す。

PLAYBOOKS = ROOT / "dip_mcp" / "playbooks"
CHANNELS = ("chat", "voice")
PLAYBOOK_FILES = {"アルバイト・パート": "parttime", "正社員": "fulltime", "未定": "undecided"}
# 区分ごとの会話の決まり(1ターンの質問の数・吹き出しの字数・ユーザーの何回目の返信までに分岐するか)。
# playbook の「会話の決まり」と Prompt のトーンに同じ値を書き、評価の形式判定(evals/checks.py の FORMAT_RULES)もこれを使う。
# 正社員は経歴や本人の言葉を受け止めてから聞くので、吹き出しを 120 字、分岐を6回目の返信までに緩める。
# 相談の途中はどの区分も分岐の期限を適用しない
CONVERSATION_RULES = {
    "アルバイト・パート": {"questions_max": 1, "bubble_max": 80, "branch_by": 3},
    "正社員": {"questions_max": 1, "bubble_max": 120, "branch_by": 6},
    "未定": {"questions_max": 1, "bubble_max": 80, "branch_by": 3},
}


# 音声の会話の決まり(1回の発話の文の数・1回に聞くことの数)。音声は区分によらず短く、1問ずつ(aica v2 の G)。
# 吹き出しの字数・分岐の期限はチャットの決まりなので、音声には無い
VOICE_CONVERSATION_RULES = {track: {"questions_max": 1, "sentences_max": 2} for track in TRACKS}


def counseling_playbook(track: str, channel: str = "chat") -> dict:
    """その区分・チャネルの playbook(区分に共通の決まりを含む)と、会話の決まりの値。"""
    if track not in TRACKS:
        return {"error": f"track は {' / '.join(TRACKS)} のいずれか"}
    if channel not in CHANNELS:
        return {"error": f"channel は {' / '.join(CHANNELS)} のいずれか"}
    path = PLAYBOOKS / f"{PLAYBOOK_FILES[track]}_{channel}.md"
    if not path.is_file():
        return {"error": f"{channel} の playbook はまだありません"}
    common = PLAYBOOKS / f"common_{channel}.md"
    text = path.read_text(encoding="utf-8") + ("\n" + common.read_text(encoding="utf-8") if common.is_file() else "")
    rules = (CONVERSATION_RULES if channel == "chat" else VOICE_CONVERSATION_RULES)[track]
    return {"track": track, "channel": channel, "rules": dict(rules), "playbook": text}


# ----------------------------- 音声相談の結果(トークへ戻す) -----------------------------


VOICE_ROOMS_KEPT = 20  # 覚えておく音声相談の部屋の数(「すべて消す」の前に始めた面談の結果を見分けるため)


def record_voice_result(state: State, line_user_id: str, jobs: list[dict] | None,
                        draft_markdown: str, files: list[str] | None = None,
                        preferences: dict | None = None, room: str | None = None,
                        document_kind: str | None = None) -> dict:
    """音声アドバイザー(voice-resume)の結果を預かる。トークの音声相談のシートが待っていれば、そこへ届く
    (voice_results_since)。

    求人カードは dip の求人データから作り直す(job_id が dip に無いものは捨てる)。
    トークに出る求人が dip の返した事実だけになる、という LINE 側の決まりを守るため。
    preferences は音声で本人が話した希望(トークに出す「話した内容のまとめ」に使う)。保存と同じ決まりで、
    決まった項目だけ・個人情報らしい値と長すぎる値は捨てる。
    room は面談の LiveKit の部屋(トークのシートが自分の面談の結果だけを受け取るため。不明なら None)。
    document_kind は下書きの書類の種類(履歴書 / 職務経歴書)。知らない値・無いときは履歴書。
    id は voice_result_seq から振る(「すべて消す」の後も番号を戻さない。古いリンクが新しい下書きを開かないように)。
    """
    all_jobs = load_jobs()
    cards = []
    for j in jobs or []:
        job = all_jobs.get(str(j.get("job_id", "")))
        if job is None:
            continue
        reasons = [str(r) for r in (j.get("reasons") or []) if str(r).strip()]
        cards.append(job_card(job, reasons or [pay_text(job.pay)]))
    prefs = {k: str(v).strip() for k, v in (preferences or {}).items()
             if str(v).strip() and _preference_error(k, v) is None}
    kind = document_kind if document_kind in DOCUMENT_KINDS else DEFAULT_DOCUMENT_KIND
    rec = {"at": now(line_user_id).isoformat(), "cards": cards, "draft_markdown": str(draft_markdown or ""),
           "files": [str(f) for f in (files or [])], "preferences": prefs, "room": room or None,
           "document_kind": kind}
    result = {}

    def apply(u):
        results = u.setdefault("voice_results", [])
        rec["id"] = _next_voice_id(u)
        u["voice_result_seq"] = rec["id"] + 1
        results.append(rec)
        result["id"] = rec["id"]

    state.update(line_user_id, apply)
    return {"recorded": result["id"], "cards": len(cards),
            "dropped_job_ids": [str(j.get("job_id", "")) for j in (jobs or [])
                                if str(j.get("job_id", "")) not in all_jobs]}


def _next_voice_id(user: dict) -> int:
    """次に届く音声相談の結果の id。連番を持つ前の記録は、今ある結果の数から。"""
    results = user.get("voice_results", [])
    return max(user.get("voice_result_seq", 0), max((r.get("id", -1) for r in results), default=-1) + 1)


def voice_results_since(state: State, line_user_id: str, since: int, room: str | None = None) -> list[dict]:
    """id が since 以上の音声相談の結果(音声相談のシートを開いた後に届いたもの)。
    room を渡したら、その部屋の結果(と部屋の分からない結果)だけ。前に閉じたシートの面談の結果は含めない。"""
    return [copy.deepcopy(r) for r in state.user(line_user_id).get("voice_results", [])
            if r.get("id", -1) >= since and (room is None or r.get("room") in (None, room))]


def voice_result_count(state: State, line_user_id: str) -> int:
    """次に届く音声相談の結果の id(シートを開いた時点の since)。"""
    return _next_voice_id(state.user(line_user_id))


def voice_result(state: State, line_user_id: str, result_id: int) -> dict | None:
    """記録済みの音声相談の結果を1件返す(下書きの表示ページ用)。"""
    for r in state.user(line_user_id).get("voice_results", []):
        if r.get("id") == result_id:
            return r
    return None


def record_voice_room(state: State, line_user_id: str, room: str) -> None:
    """音声相談の部屋を発行したことを覚える(「すべて消す」の後に、その前に始めた面談の結果を受け取らないため)。"""
    state.update(line_user_id, lambda u: u.__setitem__(
        "voice_rooms", [*u.get("voice_rooms", []), room][-VOICE_ROOMS_KEPT:]))


def voice_room_forgotten(state: State, line_user_id: str, room: str | None) -> bool:
    """「すべて消す」の前に始めた面談の部屋か(その結果で dip の記録を作り直さない)。"""
    return bool(room) and room in state.user(line_user_id).get("forgotten_voice_rooms", [])


# ----------------------------- アップロードした履歴書(resume-intake の読み取り結果) -----------------------------


def save_resume_upload(state: State, line_user_id: str, result: dict, file: str = "") -> dict:
    """履歴書の読み取り結果(resume_draft.schema.envelope の形)を、そのユーザーの最新の1件として保存する。
    id はそのユーザーのアップロードの通し番号(トークの「読み取り結果を見る」が、どのアップロードの結果かを指す)。
    番号は resume_upload_seq から振る: プロフィール画面で読み取り結果を消しても番号は戻らないので、
    消す前のターンのリンクが、あとのアップロードを開くことはない。

    ファイル本体・ファイル名(氏名が入りがち)は保存しない。file はトークに出す表示名(「履歴書(PDF)」のような
    種類だけ)。personal は何が来ても空欄で保存し、
    resume の文字列からは電話番号・メール・郵便番号をもう一度落とす(読み取り側の防御に重ねる)。
    """
    resume = ResumeDraft.model_validate(scrub_pii(result.get("resume") or {})).model_dump()
    rec = {"format_version": str(result.get("format_version", "")), "source": "upload",
           "created_at": str(result.get("created_at", "")), "uploaded_at": now(line_user_id).isoformat(),
           "file": file, "personal": {k: "" for k in PERSONAL_KEYS}, "resume": resume}

    def apply(u):
        prev = u.get("resume_upload")
        seq = u.get("resume_upload_seq")
        if seq is None:  # 連番を持つ前の記録: 今ある結果の次から
            seq = prev.get("id", 0) + 1 if prev else 0
        rec["id"] = seq
        u["resume_upload_seq"] = seq + 1
        u["resume_upload"] = rec

    state.update(line_user_id, apply)
    return copy.deepcopy(rec)


def resume_upload(state: State, line_user_id: str) -> dict | None:
    """保存済みの履歴書の読み取り結果(無ければ None)。"""
    return copy.deepcopy(state.user(line_user_id).get("resume_upload"))


def _compact(value):
    """空文字・空配列・空の辞書を落とす(要約に載せる量を減らす)。"""
    if isinstance(value, dict):
        out = {k: _compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in ("", [], {}, None)}
    if isinstance(value, list):
        return [c for c in (_compact(v) for v in value) if c not in ("", [], {}, None)]
    return value


def uploaded_resume(user: dict) -> dict | None:
    """会話の要約に載せる形の、アップロードした履歴書(書いてある項目だけ)。無ければ None。"""
    rec = user.get("resume_upload")
    if not rec:
        return None
    return {"uploaded": _md(rec["uploaded_at"]), **_compact(rec["resume"])}


# ----------------------------- 会話結果の記録 -----------------------------

OUTCOMES = ("propose", "consult", "support", "wait")
# wait の reason に要る断りの言葉。dip はトークの本文を見られないので、LINE 側 LLM が書いた reason で見る。
# 「考えます」「迷って」は断りではなく迷いのサインなので入れない(迷いのポイントを聞いて支援する)。
# 一覧に無い言い方で断られたときは記録されない(wait を記録しない側に倒す。断っていない人を wait にするよりよい)
REFUSAL_WORDS = re.compile(r"今はいい|いらない|要らない|やめ|止め|送らないで|あとで|後で|また今度|結構|不要")


CONSULTATION_SUMMARY_MAX = 200  # 相談のまとめの字数の上限


def record_outcome(state: State, line_user_id: str, outcome: str, reason: str, confidence: float,
                   summary: str | None = None) -> dict:
    """会話の結果を記録する。summary は相談のまとめ(200字以内。本人の発言に基づく事実と、次に確かめることだけ)。
    まとめは次の会話(チャット・音声)の要約の consultation_summary に出る。200字を超えたら何も記録しない
    (書き直して呼び直してもらう。途中で切ると「次に確かめること」が落ちるため)。"""
    if outcome not in OUTCOMES:
        return {"error": f"outcome は {OUTCOMES} のいずれか"}
    if outcome == "wait" and not REFUSAL_WORDS.search(reason or ""):  # 断っていない人を wait にしない
        return {"error": "wait はユーザーが断ったときだけ記録できます。reason に、ユーザーが断った言葉"
                         "(例:「今はいい」「もう送らないで」)をそのまま入れてください。断っていなければ、"
                         "直前の分岐の結果(consult など)で記録するか、会話を続けてください。"}
    summary = str(summary or "").strip()
    if len(summary) > CONSULTATION_SUMMARY_MAX:
        return {"error": f"summary(相談のまとめ)が{len(summary)}字です。{CONSULTATION_SUMMARY_MAX}字以内に書き直して、"
                         "もう一度呼んでください(本人の発言に基づく事実と、次に確かめることだけ)。何も記録していません。"}
    # 理由・まとめの文に本人の連絡先・住所が紛れ込んでも保存しない(住所らしい所は、区切りまでの語ごと消す)。
    # 氏名はコードでは見分けられないので消せない: ツールの説明で書かないよう指示し(dip_mcp/server.py)、評価で見る
    reason = redact_pii(reason, extra=address_spans)
    rec = {"at": now(line_user_id).isoformat(), "outcome": outcome, "reason": reason,
           "confidence": max(0.0, min(1.0, confidence))}
    if summary:
        rec["summary"] = redact_pii(summary, extra=address_spans)
    state.update(line_user_id, lambda u: u.setdefault("outcomes", []).append(rec))
    return {"recorded": rec}


def consultation_summary(user: dict) -> dict | None:
    """要約に載せる、いちばん新しい相談のまとめ(記録した日と、その会話の結果つき)。無ければ None。"""
    rec = next((o for o in reversed(user.get("outcomes") or []) if o.get("summary")), None)
    if rec is None:
        return None
    return {"text": rec["summary"], "recorded": _md(rec["at"]), "outcome": rec["outcome"]}


# ----------------------------- プッシュ(行動イベント → こちらから話しかける) -----------------------------
# こちらから先に話しかけるのは、初回訪問・再訪の挨拶と、プッシュだけ。
# プッシュしてよいか・何を根拠に話しかけるかは dip が決める。LINE 側はここで許可されたものだけを送る。

PUSH_TRIGGERS = ("form_exit",)
PUSH_INTERVAL_DAYS = 3  # 同じ人への接触の最短間隔。シミュレーション用の仮の値


def _deny(reason: str) -> dict:
    return {"allowed": False, "reason": reason}


def push_trigger(state: State, line_user_id: str, trigger: str) -> dict:
    """dip の行動イベントを受けて、プッシュで話しかけてよいかを決め、話しかける根拠(事実)を返す。
    許可したら接触として記録する(次の頻度判定と、会話中の summary の last_contact に使われる)。"""
    if trigger not in PUSH_TRIGGERS:
        return _deny(f"trigger は {PUSH_TRIGGERS} のいずれか")
    if not behavior.dip_id(state, line_user_id):
        return _deny("dip ID と未連携なので行動イベントがない")
    last = behavior.view(state, line_user_id)["last_contact"]  # 間隔の判定に要るのは最後の接触だけ
    if last and _days_ago(last["at"], now(line_user_id)) < PUSH_INTERVAL_DAYS:
        return _deny(f"{PUSH_INTERVAL_DAYS}日以内に接触済み({_md(last['at'])} {last['channel']})")

    facts = None
    e = form_exit(state, line_user_id)
    if e:
        facts = {k: e[k] for k in ("job_id", "title", "remaining_fields", "remaining_needs_liff")}
    if facts is None:
        return _deny(f"{trigger} の根拠になる事実がない")

    behavior.record(state, line_user_id, "contact", channel="LINE AI通知", message=trigger)
    return {"allowed": True, "trigger": trigger, "facts": facts}
