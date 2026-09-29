"""わたしのプロフィール(task 95): aica が本人について覚えていることを、本人が見て、直して、消す。

見せるもの(CONTEXT.md の「プロフィール」): 雇用区分と切り替えの記録・目的・希望・経歴(stated_preferences の項目)・
相談のまとめ・アップロードした履歴書・音声相談の下書き・保存した求人・応募した求人。

- 直す・消すは、会話の要約(get_user_conversation_summary)と同じ dip の状態を書き換えるので、次に要約を読んだ時点で
  すぐ反映される。会話の途中で直したときのために、変えたことを「知らせ」として残し(profile_changes)、
  LINE 側が次の本人のターンの前に AI へ渡す(notice)。渡し終えたら消す(take_changes の n 件)
- 本人が手で直す値にも、保存(save_user_preferences)と同じ決まり(決まった項目だけ・個人情報らしい値と長すぎる値は
  保存しない)を効かせる
- 「すべて消す」は、そのユーザーの dip の状態をまるごと消す(次に開くと初回訪問)。会話そのもの(LINE 側の
  チェックポイント)を消すのは LINE 側の仕事(line_runtime.server)
LLM は使わない。
"""

from __future__ import annotations

import copy
import json

from dip_mcp import logic
from dip_mcp.data import State, fold_pii, has_pii, now

# 画面の区分けと並び(値の無い項目も「まだありません」として出す)。正社員の条件は、区分が正社員か値があるときだけ出す
SECTIONS = (
    ("purpose", "目的", ("purpose",)),
    ("wishes", "希望", ("area", "time_band", "job_type", "min_wage", "days_per_week",
                       "min_annual_salary", "work_style", "start_timing", "wants", "avoids", "concerns")),
    ("career", "経歴", ("experience", "skills", "certifications", "strengths")),
)
assert {k for _, _, keys in SECTIONS for k in keys} == set(logic.PREFERENCE_KEYS)  # 全項目をどこかに出す

SUMMARY_LABEL = "相談のまとめ"
RESUME_LABEL = "履歴書の読み取り結果"
TRACK_LABEL = "雇用区分"
VOICE_DRAFT_LABEL = "音声相談の下書き"
BY_USER = "(プロフィール画面で本人が選んだ)"  # 画面から区分を変えたときの根拠(本人の発言の代わり)
MAX_CHANGES = 20  # 知らせとして残す変更の数(渡す前に何度も直しても膨らませない)


def _job_title(jobs: dict, job_id: str) -> str:
    j = jobs.get(job_id)
    return j.title if j else ""


def view(state: State, line_user_id: str) -> dict:
    """プロフィール画面に出す形。値の無い項目も None で返す(画面が「まだありません」と出す)。"""
    user = state.user(line_user_id)
    prefs = logic.stated_preferences(user)
    track = logic.search_track(user)
    sections = []
    for sid, title, keys in SECTIONS:
        items = [{"key": k, "label": logic.PREFERENCE_KEYS[k], "value": prefs.get(k, {}).get("value"),
                  "saved": prefs.get(k, {}).get("saved")} for k in keys
                 if k not in logic.FULLTIME_PREFERENCE_KEYS or track == "正社員" or k in prefs]
        sections.append({"id": sid, "title": title, "items": items})
    jobs = logic.load_jobs()
    drafts = [{"id": r["id"], "at": logic._md(r["at"])} for r in user.get("voice_results", [])
              if r.get("draft_markdown")]
    return {
        "track": {"current": track, "choices": list(logic.TRACKS), "history": [
            {"when": logic._md(h["at"]), "from": h["from"], "track": h["track"], "evidence": h["evidence"],
             "undo": h["undo"]} for h in user.get("track_history") or []]},
        "sections": sections,
        "consultation_summary": logic.consultation_summary(user),
        "resume": copy.deepcopy(user.get("resume_upload")),
        "voice_drafts": sorted(drafts, key=lambda d: d["id"], reverse=True),
        "saved_jobs": logic.saved_jobs(state, line_user_id),
        "applications": [{"job_id": a["job_id"], "title": _job_title(jobs, a["job_id"]), "applied": logic._md(a["applied_at"]),
                          "status": a["status"]}
                         for a in sorted(user.get("applications", []), key=lambda a: a["applied_at"], reverse=True)],
    }


def _note(u: dict, action: str, what: str, value: str | None = None) -> None:
    """次の本人のターンの前に AI へ渡す、変えたことの知らせ。"""
    rec = {"at": now().isoformat(), "action": action, "what": what} | ({"value": value} if value is not None else {})
    u["profile_changes"] = [*u.get("profile_changes", []), rec][-MAX_CHANGES:]


ERRORS = {
    "unknown_key": "この項目は直せません。",
    "personal_info": "電話番号・メールアドレス・住所のような個人情報は保存できません。",
    "too_long": f"{logic.PREFERENCE_VALUE_MAX}字以内で入力してください。",
    "empty": "空欄では保存できません。消すときは「消す」を押してください。",
}


def edit_item(state: State, line_user_id: str, key: str, value: str) -> dict:
    """本人が項目を本人の言葉で直す(上書き)。保存と同じ決まりで、個人情報らしい値・長すぎる値は弾く。"""
    value = str(value or "").strip()
    err = logic._preference_error(key, value) or (None if value else "empty")
    if err:
        return {"error": err, "message": ERRORS[err]}

    def apply(u):
        u.setdefault("preferences", {})[key] = {"value": value, "at": now().isoformat()}
        _note(u, "直した", logic.PREFERENCE_KEYS[key], value)

    state.update(line_user_id, apply)
    return {"saved": key, "value": value}


def delete_item(state: State, line_user_id: str, key: str) -> dict:
    if key not in logic.PREFERENCE_KEYS:
        return {"error": "unknown_key", "message": ERRORS["unknown_key"]}

    def apply(u):
        if (u.get("preferences") or {}).pop(key, None) is not None:
            _note(u, "消した", logic.PREFERENCE_KEYS[key])

    state.update(line_user_id, apply)
    return {"deleted": key}


def edit_summary(state: State, line_user_id: str, text: str) -> dict:
    """相談のまとめを本人の言葉で直す(いちばん新しいまとめを書き換える。無ければ新しく残す)。"""
    text = str(text or "").strip()
    if not text:
        return {"error": "empty", "message": ERRORS["empty"]}
    if has_pii(text) or logic.ADDRESS_LIKE.search(fold_pii(text)):
        return {"error": "personal_info", "message": ERRORS["personal_info"]}
    if len(text) > logic.CONSULTATION_SUMMARY_MAX:
        return {"error": "too_long", "message": f"{logic.CONSULTATION_SUMMARY_MAX}字以内で入力してください。"}

    def apply(u):
        outcomes = u.setdefault("outcomes", [])
        rec = next((o for o in reversed(outcomes) if o.get("summary")), None)
        if rec is None:
            outcomes.append({"at": now().isoformat(), "outcome": "consult", "reason": "本人がプロフィール画面で書いた",
                             "confidence": 1.0, "summary": text})
        else:
            rec["summary"] = text
        _note(u, "直した", SUMMARY_LABEL, text)

    state.update(line_user_id, apply)
    return {"saved": "consultation_summary", "value": text}


def delete_summary(state: State, line_user_id: str) -> dict:
    """相談のまとめを消す(前のまとめが繰り上がって出ないよう、すべての記録から消す。会話の結果の記録は残す)。"""
    def apply(u):
        found = False
        for o in u.get("outcomes", []):
            found = o.pop("summary", None) is not None or found
        if found:
            _note(u, "消した", SUMMARY_LABEL)

    state.update(line_user_id, apply)
    return {"deleted": "consultation_summary"}


def set_track(state: State, line_user_id: str, track: str) -> dict:
    """本人が画面で雇用区分を選び直す(切り替えの記録に残る。根拠は「画面で本人が選んだ」)。"""
    r = logic.set_search_track(state, line_user_id, track, BY_USER)
    if r.get("changed"):
        state.update(line_user_id, lambda u: _note(u, "直した", TRACK_LABEL, track))
    return r


def delete_track(state: State, line_user_id: str) -> dict:
    """雇用区分を消す: 切り替えの記録ごと消して未定に戻す。"""
    def apply(u):
        if u.pop("track_history", None):
            _note(u, "消した", TRACK_LABEL)

    state.update(line_user_id, apply)
    return {"deleted": "track", "track": logic.DEFAULT_TRACK}


def delete_resume(state: State, line_user_id: str) -> dict:
    """アップロードした履歴書の読み取り結果を丸ごと消す(チャットの要約にも音声の指示にも出なくなる)。"""
    def apply(u):
        if u.pop("resume_upload", None) is not None:
            _note(u, "消した", RESUME_LABEL)

    state.update(line_user_id, apply)
    return {"deleted": "resume"}


def delete_voice_draft(state: State, line_user_id: str, result_id: int) -> dict:
    """音声相談の下書きを丸ごと消す。書き出したファイル(files)は呼び出し元が消せるように返す。"""
    out: dict = {"error": "not_found"}

    def apply(u):
        for r in u.get("voice_results", []):
            if r.get("id") == result_id and (r.get("draft_markdown") or r.get("files")):
                out.clear()
                out.update({"deleted": "voice_draft", "id": result_id, "files": list(r.get("files") or [])})
                r["draft_markdown"], r["files"] = "", []
                _note(u, "消した", VOICE_DRAFT_LABEL)

    state.update(line_user_id, apply)
    return out


def unsave_job(state: State, line_user_id: str, job_id: str) -> dict:
    def apply(u):
        u["saves"] = [s for s in u.get("saves", []) if s["job_id"] != job_id]

    state.update(line_user_id, apply)
    return {"unsaved": job_id}


def forget_all(state: State, line_user_id: str) -> dict:
    """すべて消す: そのユーザーの dip の状態(長期メモリ・行動・トークを開いた記録)を初期化する。
    書き出した音声の下書きのファイルは呼び出し元が消せるように返す。"""
    files = [f for r in state.user(line_user_id).get("voice_results", []) for f in r.get("files") or []]
    state.reset_user(line_user_id)
    return {"forgotten": True, "files": files}


# ----------------------------- 会話の途中で直したときの知らせ -----------------------------


def pending_changes(state: State, line_user_id: str) -> list[dict]:
    return copy.deepcopy(state.user(line_user_id).get("profile_changes") or [])


def take_changes(state: State, line_user_id: str, n: int) -> None:
    """渡し終えた先頭 n 件の知らせを消す(渡している間に直した分は残す)。"""
    if n > 0:
        state.update(line_user_id, lambda u: u.__setitem__("profile_changes", (u.get("profile_changes") or [])[n:]))


def notice(state: State, line_user_id: str, changes: list[dict]) -> str:
    """次の本人のターンの前に AI へ渡す知らせ。変えたことと、今の内容(要約と同じ形)を並べる。"""
    done = []
    for c in changes:
        done.append(f"{c['what']}を「{c['value']}」に直した" if "value" in c else f"{c['what']}を消した")
    user = state.user(line_user_id)
    now_ = {"search_track": logic.search_track(user),
            "stated_preferences": {k: v["value"] for k, v in logic.stated_preferences(user).items()},
            "consultation_summary": (logic.consultation_summary(user) or {}).get("text"),
            "uploaded_resume": "あり" if user.get("resume_upload") else "なし"}
    return ("[お知らせ: 本人がプロフィール画面で、aica が覚えていることを変えました] "
            f"変えたこと: {'、'.join(dict.fromkeys(done))}。"
            f"今の内容: {json.dumps(now_, ensure_ascii=False)} "
            "これを前提に話す。消した項目と直す前の値は、この会話の前の部分や前に読んだ要約に出ていても使わず、触れない。"
            "変えたことを長々と確認しない。")
