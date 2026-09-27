"""dip 側の業務ロジック(MCP ツールの中身)。LLM を使わない。速く・決定的に・事実だけを返す。

設計書の原則をここで守る:
- 要約で返し、生ログは渡さない。閲覧は件数と傾向だけ(求人名を列挙しない)。
- 履歴は 90 日以内のものだけ。要約は「仮説」として返す(断定しない材料)。
- 推薦は最大3件、各件に事実に基づく理由を付ける。
- 個人情報の項目はトークで受け取らず、LIFF 画面へ回す。
"""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from datetime import timedelta

from dip_mcp import embedding
from dip_mcp.data import (
    FIXTURES,
    PII_FIELDS,
    PII_VALUE,
    BaitoJob,
    State,
    load_job_faq,
    load_jobs,
    load_listings,
    now,
    parse_ts,
)

HISTORY_DAYS = 90
MAX_JOBS = 3
LIFF_BASE = "/liff/apply"


def _md(ts: str) -> str:
    d = parse_ts(ts)
    return f"{d.month}/{d.day}"


def _days_ago(ts: str) -> int:
    """暦日で数える(9/19 → 9/23 は4日)。"""
    return (now().date() - parse_ts(ts).astimezone(now().tzinfo).date()).days


def _recent(items: list[dict], key: str, days: int = HISTORY_DAYS) -> list[dict]:
    return [i for i in items if _days_ago(i[key]) <= days]


def active_suppression(user: dict) -> dict | None:
    for o in sorted(user.get("outcomes", []), key=lambda o: o["at"], reverse=True):
        if o["outcome"] in ("wait", "stop") and parse_ts(o["valid_until"]) > now():
            return {"outcome": o["outcome"], "reason": o["reason"], "until": _md(o["valid_until"])}
    return None


def preferences(user: dict, jobs: dict[str, BaitoJob]) -> dict:
    """直近の応募・閲覧・保存から、エリア・時間帯・職種の傾向を数える(多い順)。"""
    refs = [a["job_id"] for a in _recent(user.get("applications", []), "applied_at")]
    refs += [v["job_id"] for v in _recent(user.get("views", []), "at", 30)]
    refs += [s["job_id"] for s in _recent(user.get("saves", []), "at", 30)]
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


def conversation_summary(state: State, line_user_id: str) -> dict:
    user = state.user(line_user_id)
    if not user.get("dip_id"):
        return {"linked": False, "segment": "unlinked", "stated_preferences": stated_preferences(user),
                "note": "dip ID と未連携のため行動履歴はない。理解ステップは飛ばし、最初の1問から始める。"
                        "stated_preferences があれば、前に本人が話した希望として確認形で1回だけ使う。"}
    jobs = load_jobs()
    apps = sorted(_recent(user.get("applications", []), "applied_at"), key=lambda a: a["applied_at"], reverse=True)
    exits = [e for e in user.get("form_exits", []) if _days_ago(e["exited_at"]) <= 7]
    views = _recent(user.get("views", []), "at", 30)
    saves = _recent(user.get("saves", []), "at", 30)
    contacts = sorted(user.get("contacts", []), key=lambda c: c["at"], reverse=True)
    suppression = active_suppression(user)

    last_app = None
    if apps:
        a = apps[0]
        j = jobs.get(a["job_id"])
        last_app = {"job_id": a["job_id"], "title": j.title if j else "", "job_type": j.job_type if j else "",
                    "area": j.area if j else "", "applied": _md(a["applied_at"]), "days_ago": _days_ago(a["applied_at"]),
                    "status": a["status"]}
    form_exit = None
    if exits:
        e = exits[-1]
        j = jobs.get(e["job_id"])
        hours = int((now() - parse_ts(e["exited_at"])).total_seconds() // 3600)
        form_exit = {"job_id": e["job_id"], "title": j.title if j else "", "hours_ago": hours,
                     "remaining": e["remaining"]}

    last_activity = max([a["applied_at"] for a in user.get("applications", [])]
                        + [v["at"] for v in user.get("views", [])] + [s["at"] for s in user.get("saves", [])],
                        default=None)
    if suppression:
        segment = "suppressed"
    elif form_exit:
        segment = "form_exit"
    elif last_app and last_app["days_ago"] <= 14 and "連絡待ち" in last_app["status"]:
        segment = "applied_waiting"
    elif last_activity is None or _days_ago(last_activity) > 30:
        segment = "dormant"
    elif (views or saves) and not (last_app and last_app["days_ago"] <= 30):
        segment = "browsing"
    else:
        segment = "active"

    return {
        "linked": True,
        "segment": segment,
        "last_application": last_app,
        "form_exit": form_exit,
        "recent_views": {"count_30d": len(views), "by_job_type": dict(Counter(
            jobs[v["job_id"]].job_type for v in views if v["job_id"] in jobs))},
        "saves_30d": len(saves),
        "tendency": preferences(user, jobs),
        "last_contact": ({"channel": contacts[0]["channel"], "when": _md(contacts[0]["at"]),
                          "message": contacts[0]["message"],
                          # 配信で送った求人(ID と名前)。返信を受けたとき、どの求人の話かを分かるように
                          "jobs": [{"job_id": i, "title": jobs[i].title} for i in contacts[0].get("job_ids", []) if i in jobs]}
                         if contacts else None),
        "suppression": suppression,
        "stated_preferences": stated_preferences(user),
        "note": "これは行動データからの仮説。本人に確認形で1回だけ触れ、断定しない。閲覧した求人名は挙げない。"
                "stated_preferences は本人が前に話した希望(行動データより優先する)。",
    }


def recent_applications(state: State, line_user_id: str) -> list[dict]:
    user, jobs = state.user(line_user_id), load_jobs()
    out = []
    for a in sorted(_recent(user.get("applications", []), "applied_at"), key=lambda a: a["applied_at"], reverse=True):
        j = jobs.get(a["job_id"])
        out.append({"job_id": a["job_id"], "title": j.title if j else "", "applied": _md(a["applied_at"]),
                    "days_ago": _days_ago(a["applied_at"]), "status": a["status"]})
    return out


def form_exit(state: State, line_user_id: str) -> dict | None:
    user, jobs = state.user(line_user_id), load_jobs()
    exits = [e for e in user.get("form_exits", []) if _days_ago(e["exited_at"]) <= 7]
    if not exits:
        return None
    e = exits[-1]
    j = jobs.get(e["job_id"])
    return {"job_id": e["job_id"], "title": j.title if j else "", "exited_at": e["exited_at"],
            "completed_fields": list(e["completed"]), "remaining_fields": e["remaining"],
            "remaining_in_talk_ok": [f for f in e["remaining"] if f not in PII_FIELDS],
            "remaining_needs_liff": [f for f in e["remaining"] if f in PII_FIELDS]}


# ----------------------------- 推薦 -----------------------------

# 求人の職種は決まった12種類。job_type はこの中の1つと完全に一致させる(部分一致はしない)
JOB_TYPES = ("アパレル販売", "イベントスタッフ", "オフィス事務", "カフェ", "コンビニ", "コールセンター", "スーパー・販売",
             "倉庫・軽作業", "塾講師・家庭教師", "配送・デリバリー", "飲食キッチン", "飲食ホール")


def _job_type_error(job_type) -> dict | None:
    """一覧に無い職種なら、0件で返さずエラーと一覧を返す(エージェントが指定し直せるように)。"""
    if not job_type or job_type in JOB_TYPES:
        return None
    return {"error": f"職種「{job_type}」は一覧にありません。job_types の中から選び、複数に当たるなら1つずつ検索し直してください",
            "job_types": list(JOB_TYPES)}


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


def _match(job: BaitoJob, areas: set[str] | None, time_band, job_type, min_wage) -> bool:
    if areas and job.area not in areas and job.station.removesuffix("駅") not in areas:
        return False
    if time_band and time_band not in job.time_bands:
        return False
    if job_type and job_type != job.job_type:
        return False
    return not (min_wage and job.hourly_wage < min_wage)


def _keyword_hit(job: BaitoJob, keywords: str) -> bool:
    """keywords の部分一致(空白区切りのどれかが求人の文面に含まれる)。意味検索が使えないときの唯一の照合。"""
    text = f"{job.title} {job.job_type} {job.description} {' '.join(job.perks)}"
    return any(k in text for k in keywords.split())


# keywords の意味検索(embedding)の選び方。類似度の絶対値は問い合わせによって大きく揺れる
# (fixtures の求人で実測: 「まかない付き」の最大 0.32、「深夜にがっつり稼ぎたい」の最大 0.60)ので、
# その問い合わせでの最高値からの相対で選び、床(SIM_FLOOR)で無関係な問い合わせの空振りを許す
SIM_FLOOR = 0.30      # これ未満は意味が近いとみなさない(無関係な語ならヒットなしにする)
SIM_RELATIVE = 0.8    # 最高値の8割以上を「意味が近い」とする
SIM_TOP = 10          # 意味の近さで拾う上限件数(total が水増しにならないように)
SIM_WEIGHT = 6        # score_breakdown の keywords = SIM_WEIGHT × 類似度(意味が近いほど上に来る)


def _keyword_filter(cands: list[BaitoJob], keywords: str) -> tuple[list[BaitoJob], dict[str, float] | None]:
    """keywords で候補を絞る。意味検索(embedding)が使えれば「部分一致 or 意味が近い」、
    使えなければ従来の部分一致だけ(検索は止めない)。類似度は並べ替え用に返す。"""
    qvec = embedding.embed_query(keywords)
    sims = embedding.similarities(qvec, cands) if qvec else None
    if sims is None:
        return [j for j in cands if _keyword_hit(j, keywords)], None
    floor = max(SIM_FLOOR, max(sims.values(), default=0.0) * SIM_RELATIVE)
    near = {jid for jid, s in sorted(sims.items(), key=lambda x: -x[1])[:SIM_TOP] if s >= floor}
    return [j for j in cands if j.job_id in near or _keyword_hit(j, keywords)], sims


def _reasons(job: BaitoJob, area, time_band, job_type, user: dict, jobs: dict[str, BaitoJob]) -> list[str]:
    """推薦理由。指定された条件か、90日以内の本人の行動に基づく事実だけを並べる。"""
    reasons = []
    for a in _recent(user.get("applications", []), "applied_at"):
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
    good = [p for p in job.perks if p in ("未経験OK", "日払い", "シフト自由", "短時間OK")]
    if good:
        reasons.append(good[0])
    return reasons[:2] or [f"時給{job.hourly_wage:,}円"]


def job_card(job: BaitoJob, reasons: list[str]) -> dict:
    """LINE の求人カードに出す形。dip が求人を返すときは、どのツールでもこの形を使う(LINE 側はこれだけを描画する)。"""
    return {"job_id": job.job_id, "title": job.title, "employer": job.employer, "job_type": job.job_type,
            "area": job.area, "station": job.station, "walk_min": job.walk_min, "hourly_wage": job.hourly_wage,
            "shift": job.shift, "days_min": job.days_min, "perks": job.perks, "reasons": reasons}


def match_breakdown(job: BaitoJob, tend: dict) -> dict[str, float]:
    """求人と本人の傾向の合い具合(job_match_score の内訳)。LLM を使わず決定的に計算する。
    条件(エリア・時間帯など)に合うかは recommend の絞り込みで見るので、ここは並べ替えのための点数。"""
    type_rank = tend["job_type"].index(job.job_type) if job.job_type in tend["job_type"] else None
    return {
        "job_type": 2.0 if type_rank == 0 else 1.0 if type_rank == 1 else 0.0,  # 傾向の1番目の職種 2点、2番目 1点
        "time_band": 1.0 if set(job.time_bands) & set(tend["time_band"]) else 0.0,
        "area": 1.0 if job.area in tend["area"] else 0.0,
        "station": round(max(0, 10 - job.walk_min) / 10, 2),  # 駅から近いほど高い(徒歩10分以上は0)
        "wage": round(job.hourly_wage / 2000, 2),
    }


def recommend(state: State, line_user_id: str | None, area=None, time_band=None, job_type=None, min_wage=None,
              keywords=None, exclude_ids: list[str] | None = None, limit: int = MAX_JOBS,
              posted_within_days: int | None = None, tendency_reasons: bool = False) -> dict:
    if err := _job_type_error(job_type):
        return err
    jobs = load_jobs()  # 傾向と理由(過去の応募との比較)は全体の求人から数える
    if err := _area_error(area, jobs):
        return err
    areas = _area_set(area)
    pool = jobs
    if posted_within_days is not None:  # 候補だけを新着に絞る
        listings = load_listings()
        pool = {k: j for k, j in jobs.items()
                if "posted_at" in listings.get(k, {}) and _days_ago(listings[k]["posted_at"]) <= posted_within_days}
    user = state.user(line_user_id) if line_user_id else {}
    applied = {a["job_id"] for a in user.get("applications", [])}
    exclude = set(exclude_ids or []) | applied  # 応募済みの求人は出さない
    tend = preferences(user, jobs) if user.get("dip_id") else {"job_type": [], "area": [], "time_band": []}
    cands = [j for j in pool.values() if j.job_id not in exclude and _match(j, areas, time_band, job_type, min_wage)]
    sims = None
    if keywords:
        cands, sims = _keyword_filter(cands, keywords)
    hits = []
    for j in cands:
        b = match_breakdown(j, tend)
        if sims is not None:
            b["keywords"] = round(SIM_WEIGHT * sims.get(j.job_id, 0.0), 2)
        hits.append((round(sum(b.values()), 2), b, j))
    hits.sort(key=lambda h: h[0], reverse=True)
    # tendency_reasons: 時間帯の指定が無いとき、本人の傾向の時間帯に合うことを理由にする(配信用。通常の推薦の理由は変えない)
    band = lambda j: time_band or (next((t for t in j.time_bands if t in tend["time_band"]), None) if tendency_reasons else None)
    top = [job_card(j, _reasons(j, area, band(j), job_type, user, jobs)) | {"match_score": score, "score_breakdown": b}
           for score, b, j in hits[:max(1, min(limit, MAX_JOBS))]]
    return {"total": len(hits), "jobs": top}


def relax(state: State, line_user_id: str | None, area=None, time_band=None, job_type=None, min_wage=None,
          keywords=None) -> dict:
    """条件を1つずつ外したら何件になるか。0件・少数のときの提案材料。"""
    if err := _job_type_error(job_type) or _area_error(area, load_jobs()):
        return err
    cond = {"area": area, "time_band": time_band, "job_type": job_type, "min_wage": min_wage, "keywords": keywords}
    base = recommend(state, line_user_id, **cond)["total"]
    options = []
    for k, v in cond.items():
        if v:
            n = recommend(state, line_user_id, **{**cond, k: None})["total"]
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
            facts = _sentences(job.description) + _sentences(job.drawback) + [
                f"勤務時間: {job.shift}(週{job.days_min}日〜)", f"待遇: {'・'.join(job.perks) or 'なし'}",
                f"時給{job.hourly_wage}円、{job.station.removesuffix('駅')}駅から徒歩{job.walk_min}分"]
            for t in facts:
                hits.append({"source": "求人の説明", "a": t, "score": score(t)})
            out["job"] = job.model_dump()
            out["card"] = job_card(job, _reasons(job, None, None, None, {}, jobs))
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
    apps = recent_applications(state, line_user_id)
    if job_id:
        apps = [a for a in apps if a["job_id"] == job_id]
    faq = {f["id"]: f["a"] for f in json.loads((FIXTURES / "faq.json").read_text(encoding="utf-8"))}
    return {"applications": apps, "reference": faq["faq-contact"]}


# ----------------------------- 手続き(フォームの続き) -----------------------------

def resume_form(state: State, line_user_id: str, job_id: str, answers: dict[str, str]) -> dict:
    """トークで受け取れる項目(個人情報以外)だけを反映し、残りは LIFF 画面の URL を返す。
    個人情報の項目に加え、どの項目でも値に電話番号・メールアドレスが含まれていれば受け取らない。"""
    rejected = [k for k, v in answers.items() if k in PII_FIELDS or PII_VALUE.search(str(v))]
    result = {}

    def apply(u):
        for e in u.get("form_exits", []):
            if e["job_id"] != job_id:
                continue
            for k, v in answers.items():
                if k in e["remaining"] and k not in rejected:
                    e["completed"][k] = v
                    e["remaining"].remove(k)
            result["remaining"] = list(e["remaining"])

    state.update(line_user_id, apply)
    if "remaining" not in result:
        return {"error": f"{job_id} の入力途中の応募フォームはありません"}
    out = {"job_id": job_id, "remaining_fields": result["remaining"],
           "rejected_pii": rejected,
           "note": "個人情報はトークで受け取らない。LIFF 画面の URL をボタンで案内する。" if rejected else ""}
    if result["remaining"]:
        out["liff_url"] = f"{LIFF_BASE}?user={line_user_id}&job={job_id}"
    return out


def submit_application(state: State, line_user_id: str, job_id: str) -> None:
    """LIFF 画面で残りの項目が入力されたら応募を確定する(トークではなく LIFF から呼ばれる)。"""
    def apply(u):
        u["form_exits"] = [e for e in u.get("form_exits", []) if e["job_id"] != job_id]
        u.setdefault("applications", []).append(
            {"job_id": job_id, "applied_at": now().isoformat(), "status": "応募済み(店舗からの連絡待ち)"})
    state.update(line_user_id, apply)


def save_job(state: State, line_user_id: str, job_id: str) -> dict:
    def apply(u):
        if job_id not in {s["job_id"] for s in u.get("saves", [])}:
            u.setdefault("saves", []).append({"job_id": job_id, "at": now().isoformat()})
    u = state.update(line_user_id, apply)
    return {"saved": job_id, "saves_total": len(u["saves"])}


# ----------------------------- 本人が話した希望(長期メモリ) -----------------------------

# experience / skills / certifications の分け方(task 41):
#   experience: これまで何をしてきたか(職歴・未経験など。例: コンビニで1年半)
#   skills: 本人が「できる」と言った作業・スキル(例: レジ打ちと品出し)
#   certifications: 持っている資格・免許(例: 普通自動車免許)
PREFERENCE_KEYS = {"area": "エリア・駅", "time_band": "時間帯", "job_type": "職種", "min_wage": "時給の下限(円)",
                   "experience": "経験(未経験など)", "days_per_week": "週の日数",
                   "skills": "できること", "certifications": "資格・免許"}


# 住所らしい値(番地・丁目・「1-2-3」の形)。氏名はコードでは見分けられないので、Prompt で保存させない
ADDRESS_LIKE = re.compile(r"\d+-\d+(-\d+)?|丁目|番地")


def save_preferences(state: State, line_user_id: str, preferences: dict[str, str | int]) -> dict:
    """本人が会話で話した希望を保存する(次の会話の要約で返す)。決まった項目だけ。
    電話番号・メールアドレス・住所らしい値は保存しない。値は文字列にそろえる。"""
    rejected = [k for k, v in preferences.items()
                if k not in PREFERENCE_KEYS or PII_VALUE.search(str(v)) or ADDRESS_LIKE.search(str(v))]
    keep = {k: str(v).strip() for k, v in preferences.items() if k not in rejected and str(v).strip()}

    def apply(u):
        prefs = u.setdefault("preferences", {})
        for k, v in keep.items():
            prefs[k] = {"value": v, "at": now().isoformat()}

    state.update(line_user_id, apply)
    out = {"saved": list(keep), "rejected": rejected}
    if rejected:
        out["note"] = (f"保存できるのは {', '.join(PREFERENCE_KEYS)} だけ。"
                       "電話番号・メールアドレス・住所など個人情報を含む値は保存しない。")
    return out


# ----------------------------- 音声相談の結果(トークへ戻す) -----------------------------


def record_voice_result(state: State, line_user_id: str, jobs: list[dict] | None,
                        draft_markdown: str, files: list[str] | None = None) -> dict:
    """音声アドバイザー(voice-resume)の結果を預かる。トークが取りに来る(pull)まで未配信として持つ。

    求人カードは dip の求人データから作り直す(job_id が dip に無いものは捨てる)。
    トークに出る求人が dip の返した事実だけになる、という LINE 側の決まりを守るため。
    """
    all_jobs = load_jobs()
    cards = []
    for j in jobs or []:
        job = all_jobs.get(str(j.get("job_id", "")))
        if job is None:
            continue
        reasons = [str(r) for r in (j.get("reasons") or []) if str(r).strip()]
        cards.append(job_card(job, reasons or [f"時給{job.hourly_wage:,}円"]))
    rec = {"at": now().isoformat(), "cards": cards, "draft_markdown": str(draft_markdown or ""),
           "files": [str(f) for f in (files or [])], "delivered": False}
    result = {}

    def apply(u):
        results = u.setdefault("voice_results", [])
        rec["id"] = len(results)
        results.append(rec)
        result["id"] = rec["id"]

    state.update(line_user_id, apply)
    return {"recorded": result["id"], "cards": len(cards),
            "dropped_job_ids": [str(j.get("job_id", "")) for j in (jobs or [])
                                if str(j.get("job_id", "")) not in all_jobs]}


def pull_voice_results(state: State, line_user_id: str) -> list[dict]:
    """未配信の音声相談の結果を返し、配信済みにする(トークの画面が呼ぶ)。"""
    pulled: list[dict] = []

    def apply(u):
        for r in u.get("voice_results", []):
            if not r.get("delivered"):
                r["delivered"] = True
                pulled.append(copy.deepcopy(r))

    state.update(line_user_id, apply)
    return pulled


def voice_result(state: State, line_user_id: str, result_id: int) -> dict | None:
    """記録済みの音声相談の結果を1件返す(下書きの表示ページ用)。"""
    for r in state.user(line_user_id).get("voice_results", []):
        if r.get("id") == result_id:
            return r
    return None


# ----------------------------- 会話結果の記録 -----------------------------

OUTCOMES = ("propose", "consult", "support", "wait", "stop")
# wait / stop の reason に要る断りの言葉。dip はトークの本文を見られないので、LINE 側 LLM が書いた reason で見る。
# 「考えます」「迷って」は断りではなく迷いのサインなので入れない(迷いのポイントを聞いて支援する)。
# 一覧に無い言い方で断られたときは記録されない(配信が止まらない側に倒す。止めるべきでないのに止めるよりよい)
REFUSAL_WORDS = re.compile(r"今はいい|いらない|要らない|やめ|止め|送らないで|あとで|後で|また今度|結構|不要")


def record_outcome(state: State, line_user_id: str, outcome: str, reason: str, confidence: float,
                   valid_days: int) -> dict:
    if outcome not in OUTCOMES:
        return {"error": f"outcome は {OUTCOMES} のいずれか"}
    if outcome in ("wait", "stop") and not REFUSAL_WORDS.search(reason or ""):  # 断っていない人の配信を止めない
        return {"error": "wait / stop はユーザーが断ったときだけ記録できます。reason に、ユーザーが断った言葉"
                         "(例:「今はいい」「もう送らないで」)をそのまま入れてください。断っていなければ、"
                         "直前の分岐の結果(consult など)で記録するか、会話を続けてください。"}
    reason = PII_VALUE.sub("[個人情報を削除]", reason)  # 理由の文にユーザーの連絡先が紛れ込んでも保存しない
    rec = {"at": now().isoformat(), "outcome": outcome, "reason": reason,
           "confidence": max(0.0, min(1.0, confidence)),
           "valid_until": (now() + timedelta(days=max(0, valid_days))).isoformat()}
    state.update(line_user_id, lambda u: u.setdefault("outcomes", []).append(rec))
    effect = "配信を抑制" if outcome in ("wait", "stop") and valid_days > 0 else "配信ルールは変更なし"
    return {"recorded": rec, "effect": f"{effect}({_md(rec['valid_until'])}まで)"}


# ----------------------------- プッシュ(行動イベント → LINE から話しかける) -----------------------------
# LINE からこちらが先に話しかけられるのは、友だち追加(応答・無料)と、プッシュ(通数課金)だけ。
# プッシュしてよいか・何を根拠に話しかけるかは dip が決める。LINE 側はここで許可されたものだけを送る。

PUSH_TRIGGERS = ("form_exit", "app_status", "saved_closing", "new_match")
PUSH_INTERVAL_DAYS = 3  # 同じ人への接触(CRM 配信を含む)の最短間隔。シミュレーション用の仮の値
NEW_POSTING_DAYS = 2
CLOSING_SOON_DAYS = 3
APP_STATUS_UPDATE = "面接の日程について店舗から連絡あり(登録メールを確認)"  # シミュレーションで起こす選考状況の変化


def _deny(reason: str) -> dict:
    return {"allowed": False, "reason": reason}


def push_trigger(state: State, line_user_id: str, trigger: str) -> dict:
    """dip の行動イベントを受けて、LINE にプッシュで話しかけてよいかを決め、話しかける根拠(事実)を返す。
    許可したら接触として記録する(次の頻度判定と、会話中の summary の last_contact に使われる)。"""
    if trigger not in PUSH_TRIGGERS:
        return _deny(f"trigger は {PUSH_TRIGGERS} のいずれか")
    user, jobs = state.user(line_user_id), load_jobs()
    if not user.get("dip_id"):
        return _deny("dip ID と未連携なので行動イベントがない")
    if s := active_suppression(user):
        return _deny(f"配信抑制中({s['until']}まで)")
    recent = [c for c in user.get("contacts", []) if _days_ago(c["at"]) < PUSH_INTERVAL_DAYS]
    if recent:
        return _deny(f"{PUSH_INTERVAL_DAYS}日以内に接触済み({_md(recent[-1]['at'])} {recent[-1]['channel']})")

    facts = None
    if trigger == "form_exit":
        e = form_exit(state, line_user_id)
        if e:
            facts = {k: e[k] for k in ("job_id", "title", "remaining_fields", "remaining_needs_liff")}
    elif trigger == "app_status":
        waiting = [a for a in recent_applications(state, line_user_id) if "連絡待ち" in a["status"]]
        if waiting:
            a = waiting[0]
            facts = {"job_id": a["job_id"], "title": a["title"], "applied": a["applied"], "new_status": APP_STATUS_UPDATE}
    elif trigger == "saved_closing":
        listings = load_listings()
        for sv in _recent(user.get("saves", []), "at", 30):
            end = listings.get(sv["job_id"], {}).get("listing_end")
            if end and 0 <= -_days_ago(end) <= CLOSING_SOON_DAYS and sv["job_id"] in jobs:
                job = jobs[sv["job_id"]]
                facts = {"job_id": job.job_id, "title": job.title, "listing_end": _md(end),
                         "days_left": -_days_ago(end), "card": job_card(job, _reasons(job, None, None, None, user, jobs))}
                break
    elif trigger == "new_match":
        # 職種が傾向に合い、さらにエリアか時間帯も合うものだけ(職種だけ合う昼の求人で夜の人に話しかけない)
        tend = preferences(user, jobs)
        new = recommend(state, line_user_id, posted_within_days=NEW_POSTING_DAYS, limit=MAX_JOBS)["jobs"]
        new = [j for j in new if j["job_type"] in tend["job_type"]
               and (j["area"] in tend["area"] or set(jobs[j["job_id"]].time_bands) & set(tend["time_band"]))]
        if new:
            facts = {"posted_within_days": NEW_POSTING_DAYS, "count": len(new),
                     "jobs": new,  # recommend の結果 = カードの形
                     "matched_on": tend}
    if facts is None:
        return _deny(f"{trigger} の根拠になる事実がない")

    def apply(u):
        if trigger == "app_status":
            for a in u.get("applications", []):
                if a["job_id"] == facts["job_id"]:
                    a["status"] = APP_STATUS_UPDATE
        u.setdefault("contacts", []).append({"at": now().isoformat(), "channel": "LINE AI通知", "message": trigger})

    state.update(line_user_id, apply)
    return {"allowed": True, "trigger": trigger, "facts": facts}


# ----------------------------- CRM 配信(dip の既存の配信。LINE の AI ではない) -----------------------------

CRM_REPLIES = ["気になる求人がある", "ほかの条件で探したい", "今はいい"]


def crm_broadcast(state: State, line_user_id: str) -> dict:
    """その人に送る CRM 配信の中身を作り、接触として記録する。LLM は使わない。
    本人の傾向に合う新着を優先し、無ければ傾向の職種で条件に近い求人を、最大3件のカードで出す。
    返信のヒント(クイックリプライ)を付け、送った求人は接触の記録に残す(返信を受けた AI が要約で分かる)。"""
    user, jobs = state.user(line_user_id), load_jobs()
    if s := active_suppression(user):  # 「今はいい」「送らないで」を尊重する(プッシュと同じ)
        return {"sent": False, "reason": f"配信抑制中({s['until']}まで)"}
    cards: list[dict] = []
    if not user.get("dip_id"):
        message = "バイトルのお仕事探しを、このトークで相談できます。気になるものを選んでください。"
        replies = ["求人を探す", "使い方", "今はいい"]
    else:
        tend = preferences(user, jobs)
        topic = tend["job_type"][0] if tend["job_type"] else "お仕事"
        fits = lambda j: j["job_type"] in tend["job_type"] and (
            j["area"] in tend["area"] or set(jobs[j["job_id"]].time_bands) & set(tend["time_band"]))
        new = [j for j in recommend(state, line_user_id, posted_within_days=NEW_POSTING_DAYS, tendency_reasons=True)["jobs"]
               if fits(j)]
        # 本文の職種は、実際に載せる求人の職種から作る(本文と中身をずらさない)
        kinds = lambda cs: "・".join(dict.fromkeys(c["job_type"] for c in cs))
        if new:
            cards = new[:MAX_JOBS]
            message = f"{kinds(cards)}の新着求人が{len(cards)}件入りました。最近見ていた条件に近いものです。"
        elif tend["job_type"]:
            cards = recommend(state, line_user_id, job_type=topic, tendency_reasons=True)["jobs"]
            message = f"最近見ていた{kinds(cards) or topic}のお仕事から、条件に近い求人を{len(cards)}件選びました。"
        if not cards:
            message = "新しいお仕事が入っています。このトークで、条件に合う求人を一緒に探せます。"
        replies = CRM_REPLIES if cards else ["求人を探す", "使い方", "今はいい"]
    record = {"at": now().isoformat(), "channel": "CRM配信", "message": message, "job_ids": [c["job_id"] for c in cards]}

    def apply(u):
        contacts = u.setdefault("contacts", [])
        # 同じ日の CRM 配信は置き換える(何度も押して接触の記録が増え、プッシュが止まり続けないように)
        contacts[:] = [c for c in contacts if not (c["channel"] == "CRM配信" and _days_ago(c["at"]) == 0)]
        contacts.append(record)

    state.update(line_user_id, apply)
    return {"sent": True, "message": message, "cards": cards, "quick_replies": replies}
