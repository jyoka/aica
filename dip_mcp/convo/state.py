"""状態の器(設計メモ docs/design/conversation-layer.md §4.1)。入り口のハーネスの状態に 1 つ持たせる、層の覚え。

- recent: 直前のやりとり。読む段に渡す(「さっきの年収」の「さっきの」が、どの欄のことか決めるため。§4.3)。
  値は今の発言からだけ抜き出す(前の発言から推し量って埋めない)
- replies: 直前の返信。言う段の重複の確かめに使う(§4.8。4 枚目のカード)
- mentions: 本人がこの会話で言った、候補に使える言葉(読む段の mentions。§4.7 の本人の言葉の候補)。新しいものを後ろに

覚える本人の発言は、氏名と要配慮の部分を伏せたもの(guard.mask)。保存するのは相談の間だけ(相談を閉じれば消える)。
"""

from __future__ import annotations

RECENT_MAX = 4  # 読む段に渡す、直前のやりとりの数(本人の発言と返信を合わせて)
REPLIES_MAX = 2  # 覚えておく直前の返信の数
MENTIONS_MAX = 5  # 覚えておく本人の言葉の数(古いものから忘れる)
TEXT_MAX = 200  # 1 つの発言・返信を覚える字数(危ない言葉の決まった文のような長い返信を、読む段の入力に丸ごと載せない)
WHO = {"user": "本人", "agent": "aica"}


def new() -> dict:
    return {"recent": [], "replies": [], "mentions": []}


def _clip(text: str) -> str:
    t = str(text or "").strip()
    return t if len(t) <= TEXT_MAX else t[:TEXT_MAX] + "…"


def heard(c: dict, text: str) -> None:
    """本人の発言を 1 つ覚える。"""
    c["recent"] = [*c.get("recent", []), {"who": "user", "text": _clip(text)}][-RECENT_MAX:]


def replied(c: dict, text: str) -> None:
    """返信を 1 つ覚える。"""
    t = _clip(text)
    c["recent"] = [*c.get("recent", []), {"who": "agent", "text": t}][-RECENT_MAX:]
    c["replies"] = [*c.get("replies", []), t][-REPLIES_MAX:]


def mentioned(c: dict, words) -> None:
    """読む段が返した本人の言葉を覚える(同じ言葉は新しい方に寄せる)。前の版の状態に mentions が無くてもよい。"""
    old = [w for w in c.get("mentions", []) if w not in words]
    c["mentions"] = [*old, *dict.fromkeys(w for w in words if isinstance(w, str) and w)][-MENTIONS_MAX:]


def recent_lines(c: dict) -> str:
    """読む段のプロンプトに入れる形(1 行 1 発言)。無ければ「(なし)」。"""
    lines = [f"{WHO[m['who']]}: {m['text']}".replace("\n", " / ") for m in c.get("recent", [])]
    return "\n".join(lines) or "(なし)"
