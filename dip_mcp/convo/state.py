"""状態の器(設計メモ docs/design/conversation-layer.md §4.1)。入り口のハーネスの状態に 1 つ持たせる、層の覚え。

- recent: 直前のやりとり。読む段に渡す(「さっきの年収」の「さっきの」が、どの欄のことか決めるため。§4.3)。
  値は今の発言からだけ抜き出す(前の発言から推し量って埋めない)
- replies: 直前の返信。acks: その相づち(無ければ None)。言う段の重複の確かめに使う(§4.8。dip_mcp.convo.reply)
- rotation: 決まった組み立ての相づちを、種類ごとに順に回す位置(dip_mcp.convo.reply.fallback)

覚える本人の発言は、氏名と要配慮の部分を伏せたもの(guard.mask)。保存するのは相談の間だけ(相談を閉じれば消える)。
"""

from __future__ import annotations

RECENT_MAX = 4  # 読む段に渡す、直前のやりとりの数(本人の発言と返信を合わせて)
REPLIES_MAX = 2  # 覚えておく直前の返信の数
TEXT_MAX = 200  # 1 つの発言・返信を覚える字数(危ない言葉の決まった文のような長い返信を、読む段の入力に丸ごと載せない)
WHO = {"user": "本人", "agent": "aica"}


def new() -> dict:
    return {"recent": [], "replies": []}


def _clip(text: str) -> str:
    t = str(text or "").strip()
    return t if len(t) <= TEXT_MAX else t[:TEXT_MAX] + "…"


def heard(c: dict, text: str) -> None:
    """本人の発言を 1 つ覚える。"""
    c["recent"] = [*c.get("recent", []), {"who": "user", "text": _clip(text)}][-RECENT_MAX:]


def replied(c: dict, text: str, ack: str | None = None) -> None:
    """返信を 1 つ覚える。ack はその返信の相づち(言う段の部品。無ければ None)。"""
    t = _clip(text)
    c["recent"] = [*c.get("recent", []), {"who": "agent", "text": t}][-RECENT_MAX:]
    c["replies"] = [*c.get("replies", []), t][-REPLIES_MAX:]
    c["acks"] = [*c.get("acks", []), ack][-REPLIES_MAX:]


def messages(c: dict) -> list[dict]:
    """言う段のモデルに渡す直前のやりとり(user / assistant の列)。ランタイムの会話の記録には、伏せる前の発言が残っているので、
    層の版はこちらを渡す(dip_mcp.baito.session.ReplayLLM の model_history)。"""
    return [{"role": "user" if m["who"] == "user" else "assistant", "content": m["text"]} for m in c.get("recent", [])]


def recent_lines(c: dict) -> str:
    """読む段のプロンプトに入れる形(1 行 1 発言)。無ければ「(なし)」。"""
    lines = [f"{WHO[m['who']]}: {m['text']}".replace("\n", " / ") for m in c.get("recent", [])]
    return "\n".join(lines) or "(なし)"
