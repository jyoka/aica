"""レジュメ・アーケード(docs/prd/resume-arcade.md の (3/3))の dip の関数: 遊びの記録、AI の回数、ゲームで作った下書き。

LLM は使わない。Web サーバー(line_runtime.server)が直接呼ぶ(HLD の 1.1 の「両方にまたがる」の形)。答えの中身は持たない。

遊びの記録(arcade_plays): 1 人 1 ゲームにつき最新の 1 件(本人、ゲーム、始めた時刻、AI を使えるか、反応の回数、
まとめの残り回数)。新しい start が前の記録を置き換え、前の番号は無効になる。番号は推測できない乱数。
- AI を使えるのは、有効な番号で、AI つきで始めた遊びで、始めてから PLAY_TTL 以内で、回数が残っているときだけ(use_ai)
- 保存(save_draft)は、本人の有効な番号が要る。PLAY_TTL を過ぎていても保存はできる(LLM を呼ばないので)

ゲームで作った下書き(arcade_drafts): ゲームごとに最新の 1 件。番号は arcade_draft_seq からの通し番号(「すべて消す」の後も
戻さない。profile._KEPT_ON_FORGET)。保存の前に scrub_pii をかけ、長さの上限を効かせる(形は resume_draft.schema)。
正社員のヒアリングが result で職歴カードから作る職務経歴書の下書き(dip_mcp/career/draft.py)も、同じ場所に CAREER_DRAFT の
1 件として入れる(save_career_draft)。下書きの画面、プロフィール、要約、空欄の数え方、「すべて消す」はゲームの下書きと同じ。
エージェントには、dip の要約(logic.conversation_summary の arcade_draft)で要点だけを渡す(ADR-0003)。

指標のための記録(arcade_metrics): 始めた回数、保存した回数、保存した下書きの数字の空欄の数、事実の確かめで置き換えた数。
始めた回数は遊びの記録からは数えない(置き換えで消えるため)。
"""

from __future__ import annotations

import copy
import os
import re
import secrets
from datetime import UTC, datetime, timedelta

from dip_mcp.data import State
from resume_draft.pii import scrub_pii
from resume_draft.schema import (
    DOCUMENT_KINDS,
    PERSONAL_KEYS,
    ResumeDraft,
    document_kind_of,
)
from resume_lines import PLACEHOLDER_RE, blank_count

GAMES = ("the-case-of-you", "harvest-of-work")  # AI と保存がつながっているゲーム(ハブに出す 2 本)
CAREER_DRAFT = "career"  # 正社員のヒアリングの下書きの置き場所(arcade_drafts の key。ゲームの名前には使わない)
GAME_NAMES = {"the-case-of-you": "あなたという事件", "harvest-of-work": "仕事の収穫", CAREER_DRAFT: "正社員の相談"}
DEFAULT_GAME = {"正社員": "the-case-of-you", "アルバイト・パート": "harvest-of-work"}
# 雇用区分ごとの書類の種類。未定は本人が保存のときに選ぶ(雇用区分は変えない)
DOCUMENT_BY_TRACK = {"正社員": "職務経歴書", "アルバイト・パート": "履歴書"}
PLAY_TTL = timedelta(hours=24)
REACTIONS_ENV = "AICA_ARCADE_REACTIONS_PER_PLAY"
DEFAULT_REACTIONS = 50  # 1 回の遊びのゲームの中の反応
SUMMARIES_MAX = 2  # 1 回の遊びの証拠のまとめと最後のまとめ(反応とは別に取っておく)
MARKDOWN_MAX = 20_000
HUB_PATH = "/liff/arcade"
DRAFT_PATH = "/liff/arcade/draft"
BLANK_LINES_IN_SUMMARY = 5


def reactions_max() -> int:
    raw = os.environ.get(REACTIONS_ENV, "").strip()
    return int(raw) if raw else DEFAULT_REACTIONS


def clock() -> datetime:
    """今の時刻(テストで差し替える)。遊びの期限(PLAY_TTL)は実際の時刻で数える。"""
    return datetime.now(UTC)


LINE_USER = re.compile(r"l-[0-9a-f]{16}")  # LINE のユーザーID の形(作るのは line_runtime.line_channel.line_user だけ)
# LINE のアプリでゲームを開く LIFF の設定(line_runtime.line_channel の LIFF_ID_ENV、LIFF_CHANNEL_ENV、LOGIN_CHANNEL_ENV と同じ名前)。
# dip は line_runtime を import しないので、名前をここにも持つ(同じ答えになることは tests/test_arcade_liff.py が確かめる)
LIFF_ID_ENV = "LINE_LIFF_ID"
LIFF_CHANNEL_ENVS = ("LINE_LIFF_CHANNEL_ID", "LINE_LOGIN_CHANNEL_ID")


def is_line_user(line_user_id: str) -> bool:
    """LINE のユーザーか。LINE のユーザーは、ゲームを LINE のアプリの LIFF 画面(/liff/line/arcade…)で開き、LIFF の ID トークンで
    確かめた本人として AI と保存を使う(HLD の 4.8)。LIFF の設定が無ければ LINE で開けないので、ゲームの URL を渡さない(game_url)。
    ユーザーIDの形で扱いを分ける先例は data.uses_sim_clock。"""
    return LINE_USER.fullmatch(line_user_id) is not None


def liff_configured() -> bool:
    """LINE のアプリで LIFF 画面を開けるか(LIFF ID とチャネル ID の両方がある。line_runtime.line_channel.liff_configured と同じ決まり)。"""
    return bool(os.environ.get(LIFF_ID_ENV, "").strip() and any(os.environ.get(k, "").strip() for k in LIFF_CHANNEL_ENVS))


def _metric(u: dict, key: str, n: int = 1) -> None:
    m = u.setdefault("arcade_metrics", {})
    m[key] = m.get(key, 0) + n


# ----------------------------- 遊びの記録 -----------------------------


def start_play(state: State, line_user_id: str, game: str, ai: bool) -> dict:
    """遊びを始める: 同じゲームの前の記録を置き換え、新しい番号を返す。始めた回数を 1 つ数える。"""
    rec = {"id": secrets.token_urlsafe(16), "game": game, "started_at": clock().isoformat(), "ai": bool(ai),
           "reactions": 0, "summaries_left": SUMMARIES_MAX if ai else 0}

    def apply(u):
        u.setdefault("arcade_plays", {})[game] = rec
        _metric(u, "started")

    state.update(line_user_id, apply)
    return copy.deepcopy(rec)


def _same(rec: dict | None, play_id) -> bool:
    """記録の番号と同じか(時間で番号を推し量れないように compare_digest。ASCII でない値も断る)。"""
    return (rec is not None and isinstance(play_id, str)
            and secrets.compare_digest(rec["id"].encode(), play_id.encode("utf-8", "replace")))


def play(state: State, line_user_id: str, game: str, play_id: str) -> dict | None:
    """本人のそのゲームの、有効な遊びの記録(番号が違えば None)。読むだけで、dip の状態に書かない。"""
    rec = (state.user(line_user_id).get("arcade_plays") or {}).get(game)
    return copy.deepcopy(rec) if _same(rec, play_id) else None


def use_ai(state: State, line_user_id: str, game: str, play_id: str, kind: str, keep: int = 0) -> str | None:
    """AI を 1 回使う(回数を数える)。使えなければ理由(no_play / ai_off / expired / reaction_limit / summary_limit)を返し、
    何も数えない。kind は reaction か summary。keep は、このまとめの後に残しておくまとめの回数(証拠のまとめが、
    最後のまとめの分を使い切らないように)。番号が無ければ dip の状態に書かない(知らないユーザーを作らない)。"""
    if play(state, line_user_id, game, play_id) is None:
        return "no_play"
    reason: list[str] = []

    def apply(u):
        rec = (u.get("arcade_plays") or {}).get(game)
        if not _same(rec, play_id):  # 読んだ後に置き換わった
            reason.append("no_play")
        elif not rec["ai"]:
            reason.append("ai_off")
        elif clock() - datetime.fromisoformat(rec["started_at"]) > PLAY_TTL:
            reason.append("expired")
        elif kind == "reaction":
            if rec["reactions"] >= reactions_max():
                reason.append("reaction_limit")
            else:
                rec["reactions"] += 1
        elif rec["summaries_left"] <= keep:
            reason.append("summary_limit")
        else:
            rec["summaries_left"] -= 1

    state.update(line_user_id, apply)
    return reason[0] if reason else None


def record_replaced(state: State, line_user_id: str, n: int) -> None:
    """事実の確かめで、本人の答えに無い数字を空欄に置き換えた(か、無い引用を捨てた)数を足す。"""
    if n > 0:
        state.update(line_user_id, lambda u: _metric(u, "replaced", n))


# ----------------------------- 下書き -----------------------------


def document_kind(state: State, line_user_id: str, chosen: str | None = None) -> str | None:
    """保存する書類の種類。正社員は職務経歴書、アルバイト・パートは履歴書。未定は本人が選んだもの(選んでいなければ None)。
    どの場合も雇用区分は変えない(区分の切り替えはエージェントが本人の発話を根拠にする。ADR-0001)。"""
    from dip_mcp import logic  # logic が要約でこのモジュールを使うので、循環を避ける

    track = logic.search_track(state.user(line_user_id))
    if track in DOCUMENT_BY_TRACK:
        return DOCUMENT_BY_TRACK[track]
    return chosen if chosen in DOCUMENT_KINDS else None


def _record(env: dict, at: str) -> dict:
    """保存する形に整える: personal は空、文字列から電話・メール・郵便番号を落とし、Resume Draft の形で確かめる。"""
    resume = ResumeDraft.model_validate(scrub_pii(env.get("resume") or {})).model_dump()
    md = scrub_pii(str(env.get("markdown") or ""))[:MARKDOWN_MAX]
    return {"format_version": str(env.get("format_version", "")), "source": "arcade",
            "document_kind": document_kind_of(env), "created_at": str(env.get("created_at", "")), "at": at,
            "personal": {k: "" for k in PERSONAL_KEYS}, "resume": resume, "markdown": md}


def save_draft(state: State, line_user_id: str, game: str, play_id: str, env: dict) -> dict | None:
    """ゲームで作った下書きを保存する(そのゲームの前の下書きを置き換える)。env は resume_arcade.draft.to_envelope の形。
    本人の有効な遊びの番号でなければ保存しない(None)。"""
    if play(state, line_user_id, game, play_id) is None:
        return None
    return _store(state, line_user_id, game, env, metrics=True)


def save_career_draft(state: State, line_user_id: str, env: dict) -> dict:
    """正社員のヒアリングの下書きを保存する(前の相談の下書きを置き換える)。env は resume_draft.markdown.to_envelope の形。
    ゲームの指標(arcade_metrics)には数えない。"""
    return _store(state, line_user_id, CAREER_DRAFT, env, metrics=False)


def _store(state: State, line_user_id: str, game: str, env: dict, metrics: bool) -> dict:
    rec = _record(env, clock().isoformat()) | {"game": game}

    def apply(u):
        seq = u.get("arcade_draft_seq", 0)
        rec["id"] = seq
        u["arcade_draft_seq"] = seq + 1
        u.setdefault("arcade_drafts", {})[game] = rec
        if metrics:
            _metric(u, "saved")
            _metric(u, "blanks", blanks(rec))

    state.update(line_user_id, apply)
    return copy.deepcopy(rec)


def blanks(rec: dict) -> int:
    """下書きの空欄の数(数字の空欄と、正社員の下書きの言葉の空欄)。"""
    return blank_count(rec.get("markdown", ""))


def drafts(state: State, line_user_id: str) -> list[dict]:
    """ゲームで作った下書き(新しい順)。"""
    recs = (state.user(line_user_id).get("arcade_drafts") or {}).values()
    return sorted((copy.deepcopy(r) for r in recs), key=lambda r: r["id"], reverse=True)


def draft(state: State, line_user_id: str, draft_id: int) -> dict | None:
    return next((r for r in drafts(state, line_user_id) if r["id"] == draft_id), None)


def edit_draft(state: State, line_user_id: str, draft_id: int, env: dict) -> dict | None:
    """本人が下書きの画面で直した書類で置き換える(番号は同じ。保存と同じ決まりを効かせる)。無ければ None。"""
    out: list[dict] = []

    def apply(u):
        for game, r in (u.get("arcade_drafts") or {}).items():
            if r["id"] == draft_id:
                new = _record(env | {"document_kind": r["document_kind"]}, r["at"]) | {"game": game, "id": draft_id,
                                                                                       "edited_at": clock().isoformat()}
                u["arcade_drafts"][game] = new
                out.append(copy.deepcopy(new))

    state.update(line_user_id, apply)
    return out[0] if out else None


def delete_draft(state: State, line_user_id: str, draft_id: int) -> bool:
    found: list[bool] = []

    def apply(u):
        ds = u.get("arcade_drafts") or {}
        for game in [g for g, r in ds.items() if r["id"] == draft_id]:
            del ds[game]
            found.append(True)

    state.update(line_user_id, apply)
    return bool(found)


# ----------------------------- dip の要約に載せるもの -----------------------------


def draft_url(line_user_id: str, draft_id: int) -> str:
    from dip_mcp.logic import liff_url

    return liff_url(DRAFT_PATH, line_user_id, rid=draft_id)


def game_url(line_user_id: str, game: str | None = None) -> str | None:
    """エージェントがボタンにするゲームの URL(dip が作る。エージェントには書かせない)。LINE のユーザーには、LIFF の設定が
    あるときだけ返す(LINE のメッセージでは LIFF URL のボタンになる。line_runtime.line_channel.to_messages)。無ければ None。"""
    from dip_mcp.logic import liff_url

    if is_line_user(line_user_id) and not liff_configured():
        return None
    return liff_url(f"{HUB_PATH}/{game}" if game else HUB_PATH, line_user_id)


def view_url(line_user_id: str, draft_id: int) -> str | None:
    """エージェントがボタン(「下書きを見る」)にする、下書きの画面の URL。窓口の決まりはゲームの URL と同じ(game_url):
    LINE のユーザーには、LIFF の設定があるときだけ返す(LINE では LIFF URL のボタンになる)。無ければ None。"""
    if is_line_user(line_user_id) and not liff_configured():
        return None
    return draft_url(line_user_id, draft_id)


def summary(user: dict) -> dict | None:
    """要約に載せる、最新の下書きの要点(書類の種類、作った日、職務要約、行の数、空欄のある行を 5 行まで)と、
    下書きの画面の URL(view_url。開けない窓口では載せない)。空欄の無い行の全文は載せない。無ければ None。"""
    recs = sorted((user.get("arcade_drafts") or {}).values(), key=lambda r: r["id"])
    if not recs:
        return None
    r = recs[-1]
    res = r["resume"]
    lines = [(w["organization"], a) for w in res["work_history"] for a in w["achievements"]]
    blank = [f"{org}: {a}" for org, a in lines if PLACEHOLDER_RE.search(a)]
    from dip_mcp.logic import _md

    return {"document_kind": r["document_kind"], "game": GAME_NAMES.get(r.get("game"), r.get("game")),
            "saved": _md(r["at"]), "job_summary": res["job_summary"], "self_pr": res["self_pr"],
            "lines": len(lines), "blank_lines": blank[:BLANK_LINES_IN_SUMMARY], "blanks": blanks(r)} | (
        {"view_url": url} if (url := view_url(user.get("line_user_id", ""), r["id"])) else {})
