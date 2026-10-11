"""音声相談が終わったとき、トークに続ける AI の返信(task 92)。

音声相談のシートが閉じたあと、同じトークに AI の返信として「音声で話した内容のまとめ」「求人カード」
「履歴書(正社員の面談なら職務経歴書)の下書きを見る」を出す。システム通知のラベル(「[音声相談]…」)は付けない。
返信は render(runtime.py)と同じ形なので、画面は他の返信と同じ renderReply で描く。
LLM は使わない(音声相談を始めたときに会話数を1つ使っている。ここで2つ目は使わない)。
- 吹き出しはどれも80字以内(対話ルールと evals/checks.py の判定に合わせる)
- まとめは音声で本人が話した希望(dip の相談プロファイルの項目)を、決まった順に短く並べる
- 求人カードは dip の求人データから作り直したもの(dip_mcp.logic.record_voice_result)だけ
- 書類の呼び方は、下書きの document_kind(履歴書 / 職務経歴書)にそろえる(吹き出し・ボタン・下書きページの題)
"""

from __future__ import annotations

from dip_mcp.logic import PREFERENCE_KEYS, liff_url
from line_runtime.resume_confirm import _clip
from resume_draft.schema import document_kind_of

MAX_CHARS = 80
INTRO = "音声でお話しした内容をまとめました。"
NO_ITEMS = "音声でのご相談、ありがとうございました。"
# まとめに出す順(探す条件 → 経験・できること)。出すのは話した項目だけ
ORDER = ("job_type", "area", "time_band", "days_per_week", "min_wage", "min_annual_salary", "work_style",
         "start_timing", "experience", "skills", "certifications", "wants", "avoids")
LABEL = {"job_type": "職種", "area": "エリア", "time_band": "時間帯", "days_per_week": "週の日数", "min_wage": "時給",
         "min_annual_salary": "希望年収", "work_style": "働き方", "start_timing": "時期",
         "experience": "経験", "skills": "できること", "certifications": "資格", "wants": "やりたいこと",
         "avoids": "避けたいこと"}


def draft_label(kind: str) -> str:
    """下書きを開くボタンの文言(例:「職務経歴書の下書きを見る」)。"""
    return f"{kind}の下書きを見る"


def _value(key: str, value: str) -> str:
    if key == "min_wage" and value.isdigit():
        return f"{int(value):,}円以上"
    if key == "days_per_week" and value.isdigit():
        return f"週{value}日"
    if key == "min_annual_salary" and value.isdigit():
        return f"{int(value):,}万円以上"
    return value


def summary_bubble(preferences: dict) -> str:
    """話した内容のまとめ(1つの吹き出し、80字以内)。入りきらない項目は出さない。"""
    text = INTRO
    for k in ORDER:
        if k in preferences and k in PREFERENCE_KEYS:
            item = f"{LABEL[k]}: {_clip(_value(k, str(preferences[k])), 16)}"
            if len(text) + 1 + len(item) <= MAX_CHARS:
                text += "\n" + item
    return NO_ITEMS if text == INTRO else text


def draft_url(user_id: str, result_id: int) -> str:
    """下書きのページの URL。ユーザーIDは載せない(下書きの画面は Cookie で本人を決める)。"""
    return liff_url("/liff/voice/draft", user_id, rid=result_id)


def draft_button(user_id: str, result_id: int, kind: str) -> dict:
    """「履歴書の下書きを見る」(kind が職務経歴書なら「職務経歴書の下書きを見る」)のボタン。"""
    return {"label": draft_label(kind), "url": draft_url(user_id, result_id)}


# document_kind を持つ前(format_version 1.0 の頃)の返信の文言。その頃の下書きはすべて履歴書の形だった
_OLD_DRAFT_BUBBLE = "職務経歴書の下書きもできました。内容はご自身で確認・修正してください。"


def voice_history_reply(user_id: str, voice: dict) -> dict:
    """会話の記録に残った音声相談の返信を、画面に描き直す形にする。ボタンの URL は今の形で組み直し、書類の呼び方は
    下書きの document_kind にそろえる(document_kind の無い古い返信は履歴書。吹き出しの古い文言も直す)。"""
    kind = document_kind_of(voice)
    bubbles = [_draft_bubble(kind) if b == _OLD_DRAFT_BUBBLE else b for b in voice.get("bubbles", [])]
    buttons = ([draft_button(user_id, voice["voice_result"], kind)]
               if voice.get("buttons") and voice.get("voice_result") is not None else [])
    return voice | {"bubbles": bubbles, "buttons": buttons}


def _draft_bubble(kind: str) -> str:
    return f"{kind}の下書きもできました。内容はご自身で確認・修正してください。"


def voice_reply(user_id: str, rec: dict, search_track: str | None = None) -> dict:
    """音声相談の結果1件(dip に預けた形)から、トークに続ける返信(render と同じ形)を作る。
    search_track は面談の後の雇用区分(次のターンの LLM に伝える。画面には出さない)。"""
    cards = rec.get("cards") or []
    bubbles = [summary_bubble(rec.get("preferences") or {})]
    if cards:
        bubbles.append(f"お話を元に{len(cards)}件の求人をご提案します。気になる求人は詳しく見られます。")
        quick = ["ほかの求人も見たい", "今はいい"]
    else:
        bubbles.append("条件に合う求人は、まだ見つかっていません。条件を変えて一緒に探せます。")
        quick = ["条件を変えて探す", "今はいい"]
    buttons = []
    kind = document_kind_of(rec)
    if rec.get("draft_markdown"):
        bubbles.append(_draft_bubble(kind))
        buttons = [draft_button(user_id, rec["id"], kind)]
    return {"entry": None, "bubbles": bubbles, "quick_replies": quick, "cards": cards, "unknown_job_ids": [],
            "buttons": buttons, "actions": [], "voice_result": rec["id"], "document_kind": kind,
            **({"search_track": search_track} if search_track else {})}
