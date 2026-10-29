"""わたしのプロフィール(task 95): aica が本人について覚えていることを、本人が見て、直して、消す。

見せるもの(CONTEXT.md の「プロフィール」): 雇用区分と切り替えの記録・目的・希望・経歴(stated_preferences の項目)・
相談のまとめ・正社員のヒアリングの職歴カード(career_profile。task 255)・アップロードした履歴書・音声相談の下書き・
ゲームで作った下書き・保存した求人・応募した求人。

- 保存した求人と応募した求人は、dip のツールと同じく行動の要約(と、この会話で書いた分)から読む(dip_mcp/behavior.py)。
  保存をやめると、元データに unsave のイベントを書く
- 直す・消すは、会話の要約(get_user_conversation_summary)と同じ dip の状態を書き換えるので、次に要約を読んだ時点で
  すぐ反映される。会話の途中で直したときのために、変えたことを「知らせ」として残し(profile_changes)、
  LINE 側が次の本人のターンの前に AI へ渡す(notice)。渡し終えたら消す(take_changes の n 件)
- 本人が手で直す値にも、保存(save_user_preferences)と同じ決まり(決まった項目だけ・個人情報らしい値と長すぎる値は
  保存しない)を効かせる
- 希望の項目を直す・消すと、バイトの相談の v5 で「あとで」と取っておいた相談からも、その項目の前の値を同じ書き込みの中で消す
  (dip_mcp.baito.session.forget_paused。jyoka/tasks#375)
- 「すべて消す」は、そのユーザーの dip の状態をまるごと消す(次に開くと初回訪問)。会話そのもの(LINE 側の
  チェックポイント)を消すのは LINE 側の仕事(line_runtime.server)
- 履歴書を消すと、LINE 側は今の会話を閉じる(会話に残るアップロードの要点を読めなくする。次は新しい会話から)
LLM は使わない。
"""

from __future__ import annotations

import copy
import json

from dip_mcp import accounts, arcade, behavior, logic, retention
from dip_mcp.baito import session as baito_session
from dip_mcp.career import store as career_store
from dip_mcp.data import State, now
from resume_draft.pii import personal_value
from resume_draft.schema import document_kind_of

# 画面の区分けと並び(値の無い項目も「まだありません」として出す)。正社員の条件は、区分が正社員か値があるときだけ出す。
# 正社員のヒアリングだけが聞く項目(なぜ今など)は、値があるときだけ出す(ヒアリングを使わない本番の既定では、画面を今までどおりにする)
SECTIONS = (
    ("purpose", "目的", ("purpose", "why_now")),
    ("wishes", "希望", ("area", "time_band", "job_type", "min_wage", "days_per_week",
                       "min_annual_salary", "work_style", "start_timing", "direction", "team_style",
                       "wants", "avoids", "concerns")),
    ("career", "経歴", ("experience", "skills", "certifications", "strengths")),
)
assert {k for _, _, keys in SECTIONS for k in keys} == set(logic.PREFERENCE_KEYS)  # 全項目をどこかに出す
FULLTIME_ONLY = (*logic.FULLTIME_PREFERENCE_KEYS, *logic.CAREER_PREFERENCE_KEYS)

SUMMARY_LABEL = "相談のまとめ"
RESUME_LABEL = "履歴書の読み取り結果"
TRACK_LABEL = "雇用区分"
VOICE_DRAFT_LABEL = "音声相談の下書き"
BY_USER = "(プロフィール画面で本人が選んだ)"  # 画面から区分を変えたときの根拠(本人の発言の代わり)
# トークの切り替えの表示のボタンで取り消したときの根拠(本人の発言の代わり。ボタンを押したこと自体が本人の意思)
UNDO_BY_BUTTON = "(トークの切り替えの表示のボタンで、本人が取り消した)"
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
                 if k in prefs or k not in FULLTIME_ONLY or track == "正社員" and k not in logic.CAREER_PREFERENCE_KEYS]
        sections.append({"id": sid, "title": title, "items": items})
    jobs = logic.load_jobs()
    drafts = [{"id": r["id"], "at": logic._md(r["at"]), "document_kind": document_kind_of(r)}
              for r in user.get("voice_results", []) if r.get("draft_markdown")]
    return {
        "track": {"current": track, "choices": list(logic.TRACKS), "history": [
            {"when": logic._md(h["at"]), "from": h["from"], "track": h["track"], "evidence": h["evidence"],
             "undo": h["undo"]} for h in user.get("track_history") or []]},
        "sections": sections,
        "consultation_summary": logic.consultation_summary(user),
        "career_profile": career_store.view(user, logic._md),
        "resume": copy.deepcopy(user.get("resume_upload")),
        "voice_drafts": sorted(drafts, key=lambda d: d["id"], reverse=True),
        "arcade_drafts": [{"id": r["id"], "at": logic._md(r["at"]), "document_kind": r["document_kind"],
                           "game": arcade.GAME_NAMES.get(r["game"], r["game"]), "blanks": arcade.blanks(r)}
                          for r in arcade.drafts(state, line_user_id)],
        "saved_jobs": logic.saved_jobs(state, line_user_id),
        "applications": [{"job_id": a["job_id"], "title": _job_title(jobs, a["job_id"]), "applied": logic._md(a["applied_at"]),
                          "status": a["status"]}
                         for a in behavior.view(state, line_user_id)["applications"]],  # 新しい順
    }


LINK_LABEL = "dip の会員との連携"
LINK_ACTIONS = ("つないだ", "解除した")


def link_changed(state: State, line_user_id: str, linked: bool) -> None:
    """LINE のアカウント連携でつないだ・解除した: 行動の事実が変わるので、会話の途中なら次の本人のターンの前に AI へ知らせる。"""
    state.update(line_user_id, lambda u: _note(u, LINK_ACTIONS[0] if linked else LINK_ACTIONS[1], LINK_LABEL))


def _note(u: dict, action: str, what: str, value: str | None = None, undone: str | None = None) -> None:
    """次の本人のターンの前に AI へ渡す、変えたことの知らせ。undone は取り消した切り替えの区分(取り消しのときだけ)。"""
    rec = ({"at": now(u["line_user_id"]).isoformat(), "action": action, "what": what} | ({"value": value} if value is not None else {})
           | ({"undone": undone} if undone is not None else {}))
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
        u.setdefault("preferences", {})[key] = {"value": value, "at": now(line_user_id).isoformat()}
        _note(u, "直した", logic.PREFERENCE_KEYS[key], value)
        baito_session.forget_paused(u)  # 「あとで」で取っておいた相談からも、前の値を消す

    state.update(line_user_id, apply)
    return {"saved": key, "value": value}


def delete_item(state: State, line_user_id: str, key: str) -> dict:
    if key not in logic.PREFERENCE_KEYS:
        return {"error": "unknown_key", "message": ERRORS["unknown_key"]}

    def apply(u):
        if (u.get("preferences") or {}).pop(key, None) is not None:
            _note(u, "消した", logic.PREFERENCE_KEYS[key])
            baito_session.forget_paused(u)  # 「あとで」で取っておいた相談からも消す

    state.update(line_user_id, apply)
    return {"deleted": key}


def edit_summary(state: State, line_user_id: str, text: str) -> dict:
    """相談のまとめを本人の言葉で直す(いちばん新しいまとめを書き換える。無ければ新しく残す)。"""
    text = str(text or "").strip()
    if not text:
        return {"error": "empty", "message": ERRORS["empty"]}
    if personal_value(text):
        return {"error": "personal_info", "message": ERRORS["personal_info"]}
    if len(text) > logic.CONSULTATION_SUMMARY_MAX:
        return {"error": "too_long", "message": f"{logic.CONSULTATION_SUMMARY_MAX}字以内で入力してください。"}

    def apply(u):
        outcomes = u.setdefault("outcomes", [])
        rec = next((o for o in reversed(outcomes) if o.get("summary")), None)
        if rec is None:
            outcomes.append({"at": now(line_user_id).isoformat(), "outcome": "consult", "reason": "本人がプロフィール画面で書いた",
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


def edit_career_item(state: State, line_user_id: str, key: str, value: str) -> dict:
    """本人が職歴カード(か segment だけの問いの答え)の欄を本人の言葉で直す(上書き)。個人情報らしい値・長すぎる値は弾く。"""
    value = str(value or "").strip()
    err = None

    def apply(u):
        nonlocal err
        if err := career_store.edit(u, key, value, now(line_user_id).isoformat(), checked=True):
            return
        _note(u, "直した", career_store.label(key), value)

    state.update(line_user_id, apply)
    if err:
        return {"error": err, "message": CAREER_ERRORS.get(err) or ERRORS[err]}
    return {"saved": key, "value": value}


CAREER_ERRORS = {"too_long": f"{career_store.VALUE_MAX}字以内で入力してください。"}


def delete_career_item(state: State, line_user_id: str, key: str) -> dict:
    """職歴カードの欄(か segment、segment だけの問いの答え)を 1 つ消す。次の正社員の相談では、また聞く。"""
    known = True

    def apply(u):
        nonlocal known
        found = career_store.delete(u, key)
        known = found is not None
        if found:
            _note(u, "消した", career_store.label(key))

    state.update(line_user_id, apply)
    if not known:
        return {"error": "unknown_key", "message": ERRORS["unknown_key"]}
    return {"deleted": key}


def set_track(state: State, line_user_id: str, track: str) -> dict:
    """本人が画面で雇用区分を選び直す(切り替えの記録に残る。根拠は「画面で本人が選んだ」)。"""
    r = logic.set_search_track(state, line_user_id, track, BY_USER)
    if r.get("changed"):
        state.update(line_user_id, lambda u: _note(u, "直した", TRACK_LABEL, track))
    return r


def delete_track(state: State, line_user_id: str) -> dict:
    """雇用区分を消す: 切り替えの記録ごと消して未定に戻す(トークの表示の「取り消し」とは別。あちらは直前の
    切り替え1件を元に戻し、取り消しとして記録に残す)。"""
    def apply(u):
        if u.pop("track_history", None):
            _note(u, "消した", TRACK_LABEL)

    state.update(line_user_id, apply)
    return {"deleted": "track", "track": logic.DEFAULT_TRACK}


# ----------------------------- トークの切り替えの表示(task 96) -----------------------------
# 区分が切り替わったターンに、トークが AI の返信の前に出す表示。元は set_search_track が dip に記録した切り替え
# (返信文からは推測しない)。表示のボタンで、その切り替えを取り消せる(直前の切り替えのときだけ)。


def switch_status(state: State, line_user_id: str, switch_id: str) -> str:
    """トークに出した切り替えの表示(記録の id)の、今の状態。dip の記録から決める。
    undoable: 直前の切り替えで、まだ取り消せる / undone: すぐ後の記録がその取り消し /
    past: 後に別の切り替えがある(取り消しの記録そのものも、取り消せないのでここ) / gone: 記録が消された(プロフィールの「消す」)"""
    history = state.user(line_user_id).get("track_history") or []
    i = next((k for k, h in enumerate(history) if h.get("id") == switch_id), None)
    if i is None:
        return "gone"
    if i + 1 < len(history):
        return "undone" if history[i + 1]["undo"] else "past"
    return "past" if history[i]["undo"] else "undoable"


def undo_switch(state: State, line_user_id: str, switch_id: str) -> dict:
    """トークの表示のボタンで、その切り替え(記録の id)を取り消して元の区分に戻す。取り消しとして記録に残し、
    次の本人のターンの前に AI へ知らせる(要約を読み直し、同じ切り替えをすぐに繰り返さないように)。
    直前の切り替えでなければ(後に別の切り替えがある・すでに取り消した・記録が消された)取り消さない。"""
    status = switch_status(state, line_user_id, switch_id)
    if status != "undoable":
        return {"error": status, "message": UNDO_ERRORS[status]}
    rec = (state.user(line_user_id).get("track_history") or [])[-1]
    r = logic.set_search_track(state, line_user_id, rec["from"], UNDO_BY_BUTTON, undo=True)
    if "error" in r:
        return {"error": "past", "message": r["error"]}
    state.update(line_user_id, lambda u: _note(u, "取り消した", TRACK_LABEL, rec["from"], undone=rec["track"]))
    return {"track": rec["from"], "undone": rec["track"], "recorded": r["recorded"]}


UNDO_ERRORS = {
    "undone": "この切り替えは、もう取り消してあります。",
    "past": "このあとに区分が切り替わっているので、ここからは取り消せません。区分はプロフィールで直せます。",
    "gone": "切り替えの記録が消されています。区分はプロフィールで直せます。",
}


def delete_resume(state: State, line_user_id: str) -> dict:
    """アップロードした履歴書の読み取り結果を丸ごと消す(チャットの要約にも音声の指示にも出なくなる)。
    found: 消すものがあったか。会話に残る要点も読めなくするため、呼び出し元はこれを呼ぶ前に今の会話を閉じる
    (line_runtime.server の profile_resume_delete)。"""
    found = False

    def apply(u):
        nonlocal found
        if u.pop("resume_upload", None) is not None:
            found = True
            _note(u, "消した", RESUME_LABEL)

    state.update(line_user_id, apply)
    return {"deleted": "resume", "found": found}


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


ARCADE_DRAFT_LABEL = "ゲームで作った下書き"


def delete_arcade_draft(state: State, line_user_id: str, draft_id: int) -> dict:
    """ゲームで作った下書きを丸ごと消す(チャットの要約にも出なくなる)。"""
    if not arcade.delete_draft(state, line_user_id, draft_id):
        return {"error": "not_found"}
    state.update(line_user_id, lambda u: _note(u, "消した", ARCADE_DRAFT_LABEL))
    return {"deleted": "arcade_draft", "id": draft_id}


def edit_arcade_draft(state: State, line_user_id: str, draft_id: int, env: dict) -> dict:
    """本人が下書きの画面で、ゲームで作った下書きを直した(保存と同じ決まり。arcade.edit_draft)。"""
    rec = arcade.edit_draft(state, line_user_id, draft_id, env)
    if rec is None:
        return {"error": "not_found"}
    state.update(line_user_id, lambda u: _note(u, "直した", ARCADE_DRAFT_LABEL))
    return rec


def unsave_job(state: State, line_user_id: str, job_id: str) -> dict:
    """保存をやめる。保存していた求人なら、取り消しを書き、次の本人のターンの前に AI へ知らせる(保存したままと思って
    話さないように)。要約の保存は全件なので、載っていない求人は保存していない。"""
    title = _job_title(logic.load_jobs(), job_id) or job_id
    if job_id in {s["job_id"] for s in behavior.view(state, line_user_id)["saves"]}:
        behavior.record(state, line_user_id, "unsave", job_id)
        state.update(line_user_id, lambda u: _note(u, "消した", f"求人「{title}」の保存"))
    return {"unsaved": job_id}


def resume_uploaded(state: State, line_user_id: str) -> None:
    """新しい履歴書を保存した: まだ AI に渡していない「履歴書の読み取り結果を消した」知らせを落とす。
    残すと、アップロードのターンで「消した履歴書は使わない」と新しい履歴書の両方が AI に届き、新しい方まで避けうる。"""
    def apply(u):
        changes = u.get("profile_changes") or []
        u["profile_changes"] = [c for c in changes if not (c["what"] == RESUME_LABEL and c["action"] == "消した")]

    state.update(line_user_id, apply)


# 「すべて消す」の後も持ち越すもの: 連番(古いリンクの rid が、消した後の新しい履歴書・下書き・ゲームの下書きを開かないように)と、
# 消す前に始めた音声相談の部屋(その面談の結果が後から届いても、dip の記録を作り直さないように)。
# 持ち越した行には消した時刻を最後の利用として書き、部屋の名前は 1 日、行は 180 日で掃除が外す(dip_mcp.retention)
_KEPT_ON_FORGET = ("resume_upload_seq", "voice_result_seq", "arcade_draft_seq")


def forget_all(state: State, line_user_id: str) -> dict:
    """すべて消す: そのユーザーの dip の状態(プロフィール・行動・トークを開いた記録)を初期化する。
    同じブラウザの /dev のシナリオ用ユーザー(「匿名ID:シナリオユーザー」)の状態も消す。
    書き出した音声の下書きのファイルは呼び出し元が消せるように返し、消したユーザー(会話も消す対象)も返す。"""
    users = [line_user_id, *sorted(u for u in state.user_ids() if u.startswith(f"{line_user_id}:"))]
    files: list[str] = []
    for uid in users:
        # 持ち越す値は、行を戻すのと同じロックの中で書き戻す(間に、消す前の部屋の書き込みが通らないように)
        before = state.reset_user(uid, keep=_kept_on_forget)
        files += [f for r in before.get("voice_results", []) for f in r.get("files") or []]
    return {"forgotten": True, "files": files, "users": users}


def _kept_on_forget(before: dict) -> dict:
    """「すべて消す」の後も持ち越す値(消す前の 1 人の dict から)。"""
    kept = {k: before[k] for k in _KEPT_ON_FORGET if k in before}
    if "resume_upload_seq" not in kept and before.get("resume_upload"):  # 連番を持つ前の記録: 今ある結果の次から
        kept["resume_upload_seq"] = before["resume_upload"].get("id", 0) + 1
    if "voice_results" in before:
        kept["voice_result_seq"] = logic._next_voice_id(before)
    rooms = [*before.get("forgotten_voice_rooms", []), *before.get("voice_rooms", [])][-logic.VOICE_ROOMS_KEPT:]
    if rooms:
        kept["forgotten_voice_rooms"] = rooms
    return retention.mark_forgotten(kept) if kept else kept


def stored(state: State, line_user_id: str) -> dict:
    """「データを書き出す」の dip の分: そのユーザーと、同じブラウザの /dev のシナリオ用ユーザーの、dip の状態の行
    (state.json に保存しているそのままの形)と、dip の会員との連携(dip_id)。状態にいない人の行は出さない
    (見るだけではユーザーを作らない)。範囲は「すべて消す」(forget_all)と同じ。"""
    users = [u for u in state.user_ids() if u == line_user_id or u.startswith(f"{line_user_id}:")]
    links = accounts.of(state)
    return {"users": {u: state.user(u) for u in sorted(users)},
            "links": {u: d for u in sorted({line_user_id, *users}) if (d := links.dip_id(u))}}


# ----------------------------- 会話の途中で直したときの知らせ -----------------------------


def pending_changes(state: State, line_user_id: str) -> list[dict]:
    return copy.deepcopy(state.user(line_user_id).get("profile_changes") or [])


def take_changes(state: State, line_user_id: str, n: int) -> None:
    """渡し終えた先頭 n 件の知らせを消す(渡している間に直した分は残す)。"""
    if n > 0:
        state.update(line_user_id, lambda u: u.__setitem__("profile_changes", (u.get("profile_changes") or [])[n:]))


def notice(state: State, line_user_id: str, changes: list[dict]) -> str:
    """次の本人のターンの前に AI へ渡す知らせ。変えたことと、今の内容(要約と同じ形)を並べる。"""
    done, undone = [], []
    for c in changes:
        if c["what"] == LINK_LABEL:
            done.append(f"{LINK_LABEL}を{c['action']}")
        elif c["action"] == "取り消した":
            done.append(f"{c['what']}の「{c['undone']}」への切り替えを取り消して「{c['value']}」に戻した")
            undone.append(c["undone"])
        else:
            done.append(f"{c['what']}を「{c['value']}」に直した" if "value" in c else f"{c['what']}を{c['action']}")
    user = state.user(line_user_id)
    now_ = {"search_track": logic.search_track(user),
            "stated_preferences": {k: v["value"] for k, v in logic.stated_preferences(user).items()},
            "consultation_summary": (logic.consultation_summary(user) or {}).get("text"),
            "career_profile": career_store.summary(user),
            "uploaded_resume": "あり" if user.get("resume_upload") else "なし",
            "arcade_draft": "あり" if user.get("arcade_drafts") else "なし"}
    where = ("トークの表示のボタンで" if all(c["action"] == "取り消した" for c in changes)
             else "アカウント連携で" if all(c["what"] == LINK_LABEL for c in changes) else "画面で")
    relinked = any(c["what"] == LINK_LABEL for c in changes)
    return (f"[お知らせ: 本人が{where}、aica が覚えていることを変えました] "
            f"変えたこと: {'、'.join(dict.fromkeys(done))}。"
            f"今の内容: {json.dumps(now_, ensure_ascii=False)} "
            "これを前提に話す。消した項目と直す前の値は、この会話の前の部分や前に読んだ要約に出ていても使わず、触れない。"
            "変えたことを長々と確認しない。"
            + (f"雇用区分の切り替えが取り消されたので、返信の前に get_user_conversation_summary を読み直し、今の区分"
               f"(search_track)で続ける。本人がはっきり言わない限り、「{'」「'.join(dict.fromkeys(undone))}」へ"
               "すぐに切り替え直さない(set_search_track を呼ばない)。" if undone else "")
            + ("dip の会員との連携が変わったので、閲覧・応募・保存・離脱などの行動の事実は、この会話の前の部分や前に読んだ要約の"
               "ものを使わず、返信の前に get_user_conversation_summary を読み直す。" if relinked else ""))
