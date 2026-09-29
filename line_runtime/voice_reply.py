"""音声相談が終わったとき、トークに続ける AI の返信(task 92)。

音声相談のシートが閉じたあと、同じトークに AI の返信として「音声で話した内容のまとめ」「求人カード」
「職務経歴書の下書きを見る」を出す。システム通知のラベル(「[音声相談]…」)は付けない。
返信は render(runtime.py)と同じ形なので、画面は他の返信と同じ renderReply で描く。
LLM は使わない(音声相談を始めたときに会話数を1つ使っている。ここで2つ目は使わない)。
- 吹き出しはどれも80字以内(対話ルールと evals/checks.py の判定に合わせる)
- まとめは音声で本人が話した希望(dip の相談プロファイルの項目)を、決まった順に短く並べる
- 求人カードは dip の求人データから作り直したもの(dip_mcp.logic.record_voice_result)だけ
"""

from __future__ import annotations

from dip_mcp.logic import PREFERENCE_KEYS, liff_url
from line_runtime.resume_confirm import _clip

MAX_CHARS = 80
INTRO = "音声でお話しした内容をまとめました。"
NO_ITEMS = "音声でのご相談、ありがとうございました。"
# まとめに出す順(探す条件 → 経験・できること)。出すのは話した項目だけ
ORDER = ("job_type", "area", "time_band", "days_per_week", "min_wage", "experience", "skills", "certifications",
         "wants", "avoids")
LABEL = {"job_type": "職種", "area": "エリア", "time_band": "時間帯", "days_per_week": "週の日数", "min_wage": "時給",
         "experience": "経験", "skills": "できること", "certifications": "資格", "wants": "やりたいこと",
         "avoids": "避けたいこと"}
DRAFT_LABEL = "職務経歴書の下書きを見る"


def _value(key: str, value: str) -> str:
    if key == "min_wage" and value.isdigit():
        return f"{int(value):,}円以上"
    if key == "days_per_week" and value.isdigit():
        return f"週{value}日"
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


def draft_button(user_id: str, result_id: int) -> dict:
    """「職務経歴書の下書きを見る」のボタン。URL にユーザーIDは載せない(下書きの画面は Cookie で本人を決める)。"""
    return {"label": DRAFT_LABEL, "url": liff_url("/liff/voice/draft", user_id, rid=result_id)}


def voice_reply(user_id: str, rec: dict) -> dict:
    """音声相談の結果1件(dip に預けた形)から、トークに続ける返信(render と同じ形)を作る。"""
    cards = rec.get("cards") or []
    bubbles = [summary_bubble(rec.get("preferences") or {})]
    if cards:
        bubbles.append(f"お話を元に{len(cards)}件の求人をご提案します。気になる求人は詳しく見られます。")
        quick = ["ほかの求人も見たい", "今はいい"]
    else:
        bubbles.append("条件に合う求人は、まだ見つかっていません。条件を変えて一緒に探せます。")
        quick = ["条件を変えて探す", "今はいい"]
    buttons = []
    if rec.get("draft_markdown"):
        bubbles.append("職務経歴書の下書きもできました。内容はご自身で確認・修正してください。")
        buttons = [draft_button(user_id, rec["id"])]
    return {"entry": None, "bubbles": bubbles, "quick_replies": quick, "cards": cards, "unknown_job_ids": [],
            "buttons": buttons, "actions": [], "voice_result": rec["id"]}
