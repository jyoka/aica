"""ゲーム(レジュメ・アーケード)で下書きを保存したとき、トークに続ける AI の返信(docs/prd/resume-arcade.md の (3/3))。

音声相談の結果(voice_reply)と同じ作り: LLM を呼ばず、ターンも数えない。文は dip に保存した下書きの記録
(dip_mcp.arcade.save_draft の結果)から作る。返信は render(runtime.py)と同じ形なので、画面は renderReply で描く。
- 吹き出しは 80 字以内(対話ルールと evals/checks.py の判定に合わせる)
- ボタンは「下書きを見る」(/liff/arcade/draft。ユーザーIDは載せない)、選択肢は「求人を探す」「空欄を埋める」
"""

from __future__ import annotations

from dip_mcp.arcade import blanks, draft_url

QUICK_REPLIES = ["求人を探す", "空欄を埋める"]
DRAFT_LABEL = "下書きを見る"


def draft_button(user_id: str, draft_id: int) -> dict:
    return {"label": DRAFT_LABEL, "url": draft_url(user_id, draft_id)}


def arcade_reply(user_id: str, rec: dict) -> dict:
    """保存した下書き 1 件から、トークに続ける返信を作る。"""
    kind, n = rec["document_kind"], blanks(rec)
    bubbles = [f"ゲームで{kind}の下書きができました。内容はご自身で確認・修正してください。",
               f"数字の空欄が{n}か所あります。分かる数字を入れると、書類が強くなります。" if n
               else "このまま求人探しに進めます。"]
    return {"entry": None, "bubbles": bubbles, "quick_replies": list(QUICK_REPLIES), "cards": [], "unknown_job_ids": [],
            "buttons": [draft_button(user_id, rec["id"])], "actions": [], "arcade_draft": rec["id"],
            "document_kind": kind, "blanks": n}


def arcade_history_reply(user_id: str, reply: dict) -> dict:
    """会話の記録に残った返信を描き直す形にする(ボタンの URL は今の形で組み直す)。"""
    return reply | {"buttons": [draft_button(user_id, reply["arcade_draft"])] if reply.get("buttons") else []}
