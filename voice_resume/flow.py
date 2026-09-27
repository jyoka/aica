"""会話の流れ(希望 → 経験 → できること → 資格 → 確認 → 求人の提案)を
文字起こしから確かめるチェッカー。

オフラインのテスト(台本)と、実 API の E2E の結果確認の両方で使う。
アドバイザーの発話をキーワードで段階に割り当て、期待の順に現れるかを見る。
経験の深掘りの回数と、答えを誘導する質問のチェックもここに置く(タスク 38)。
"""

from __future__ import annotations

import re

from voice_resume.prompts import STAGES

# 各段階を示すキーワード(アドバイザーの発話に含まれる言葉)。
STAGE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "wishes": ("どんなお仕事", "どんなバイト", "職種", "エリア", "最寄り駅", "時間帯", "時給", "週に"),
    "experience": ("経験", "アルバイトをした", "働いたこと", "部活", "家事", "ボランティア"),
    "strengths": ("得意", "できそう", "できること", "向いて"),
    "certifications": ("資格", "免許"),
    "confirm": ("まとめます", "要約", "確認させて", "合っていますか", "違うところ"),
    "recommend": ("求人", "お仕事を紹介", "件見つかり", "提案"),
}


def stage_positions(transcript: list[dict]) -> dict[str, int]:
    """段階ごとに、最初に現れたアドバイザー発話の位置を返す(現れなければ含まれない)。

    確認(confirm)と できること(strengths)はキーワードが重なるので、
    期待の順(STAGES)に沿って前から割り当てる: 前の段階より後の位置だけを探す。
    """
    positions: dict[str, int] = {}
    search_from = 0
    for stage, _label in STAGES:
        keywords = STAGE_KEYWORDS[stage]
        for i, turn in enumerate(transcript[search_from:], start=search_from):
            if turn.get("role") != "assistant":
                continue
            text = str(turn.get("text", ""))
            if any(k in text for k in keywords):
                positions[stage] = i
                search_from = i + 1
                break
    return positions


def verify_flow(transcript: list[dict]) -> tuple[bool, list[str]]:
    """流れが 希望 → 経験 → できること → 資格 → 確認 → 求人の提案 の順で現れたか。

    返り値: (すべての段階が順に現れたか, 現れた段階の並び)。
    """
    positions = stage_positions(transcript)
    found = [stage for stage, _ in STAGES if stage in positions]
    ok = found == [stage for stage, _ in STAGES]
    return ok, found


# --- 経験の深掘りと、答えを誘導する質問のチェック(タスク 38) ---

# 経験を聞く最初の質問。この後の追加質問を深掘りとして数える
_EXPERIENCE_ASK = STAGE_KEYWORDS["experience"] + ("仕事をされて", "これまで", "したこと")
# 事実の確認だけの聞き返し(深掘りには数えない)
_CONFIRM_ONLY = re.compile(
    r"(で)?(合っていますか|違うところはありますか|違いはありますか|間違いないですか"
    r"|よろしいですか|書いてもいいですか)"
)
# 新しい情報を求める質問の形
_ASKS_INFO = re.compile(r"(ですか|ますか|ましたか|でしょうか|教えてください|どんな|どの|どちら|どこ|いつ|何)")
# 適性・好み・向き不向きを先回りして聞く言い方(「〜は合いそうですか?」「〜はできそうですか?」など)
_APTITUDE_HINTS = (
    "合いそう", "向いていそう", "向いてそう", "向いていますか", "得意そう", "得意ですか",
    "好きですか", "自身に合", "やってみたいですか", "苦にならな", "できそう", "よさそう",
    "生かせそう", "活かせそう", "慣れていますか", "慣れていそう",
)
_IS_QUESTION = re.compile(r"か[?？。]?\s*$|[?？]")


def _question_part(text: str) -> str:
    """発話のうち、質問・依頼の文だけを返す(無ければ空)。相づち(「〜ですね」)を除くため。"""
    sentences = re.split(r"(?<=[。！!?？])", text)
    return "".join(s for s in sentences if re.search(r"[?？]|か[。]?\s*$|ください", s))


def _is_leading(text: str) -> bool:
    """適性や好みを先回りして聞いて、答えを誘導する発話か。"""
    return any(h in text for h in _APTITUDE_HINTS) and bool(_IS_QUESTION.search(text))


def _experience_bounds(transcript: list[dict]) -> tuple[int | None, int]:
    """経験を聞いた発話の位置と、その後に資格を聞いた発話の位置(無ければ末尾)を返す。

    経験を聞いた発話は質問の文だけで判定する。挨拶や希望条件の相づちにも
    「経験」の語が入るため(「バイト探しの希望と経験を伺いますね」など)。
    """
    start: int | None = None
    for i, turn in enumerate(transcript):
        if turn.get("role") != "assistant":
            continue
        text = str(turn.get("text", ""))
        if start is None:
            ask = _question_part(text)
            if "探し" not in ask and any(k in ask for k in _EXPERIENCE_ASK):
                start = i
        elif any(k in text for k in STAGE_KEYWORDS["certifications"]):
            return start, i
    return start, len(transcript)


def count_experience_probes(transcript: list[dict]) -> int:
    """経験を聞いたあと、資格の質問までの間の深掘り(追加の質問)の数。

    新しい情報を求めた質問と、誘導する質問(適性を聞く形)を数える。
    事実の確認だけの聞き返し(「〜で合っていますか」)は数えない。指示の上限は1回。
    """
    start, end = _experience_bounds(transcript)
    if start is None:
        return 0
    count = 0
    for turn in transcript[start + 1 : end]:
        if turn.get("role") != "assistant":
            continue
        raw = str(turn.get("text", ""))
        text = _CONFIRM_ONLY.sub("", raw)
        if _ASKS_INFO.search(text) or _is_leading(raw):
            count += 1
    return count


def find_leading_questions(transcript: list[dict]) -> list[str]:
    """答えを誘導する質問(適性や好みを先回りして聞く形)をした assistant 発話を返す。

    求人を読み上げる発話(「条件に合いそうな求人」など)は対象にしない。
    """
    return [
        str(turn.get("text", ""))
        for turn in transcript
        if turn.get("role") == "assistant"
        and "求人" not in str(turn.get("text", ""))
        and _is_leading(str(turn.get("text", "")))
    ]
