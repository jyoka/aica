"""履歴書をアップロードした直後にトークへ出す「読み取り結果の確認」の吹き出し(task 89)。

トークには要点だけを出す。全文は「読み取り結果を見る」(GET /liff/resume)で見せる。
- 吹き出しは最大3つ、どれも80字以内(対話ルールと evals/checks.py の判定に合わせる)
- 職歴(勤め先・雇用形態)・スキル(先頭の短い見出し)・資格は最大3件ずつ。はみ出す分は「ほか N 件」
- 読み取れなかった点は1件目の中身を短く出し、2件以上なら残りの件数と「読み取り結果を見る」を案内する
- 氏名・連絡先などの personal は見ない(読み取り結果の resume の部分だけを使う)
"""

from __future__ import annotations

import re
from itertools import product

MAX_CHARS = 80
MAX_BUBBLES = 3
MAX_ITEMS = 3
HEADING_CHARS = 16  # スキルの見出しの長さの上限
# トークに出すファイルの表示名(ファイル名は氏名が入りがちなので出さない。種類だけ)
RESUME_LABEL = {"application/pdf": "履歴書(PDF)", "image/jpeg": "履歴書(画像)", "image/png": "履歴書(画像)"}
INTRO = "履歴書を読み取りました。内容をご確認ください。"
EMPTY = "履歴書から読み取れた項目がありませんでした。ファイルが履歴書か、写りを確認してもう一度お試しください。"
# スキルの見出しの区切り(「言語: Python, Java」「Office(Word、Excel)」の先頭だけを取る)
_HEADING_END = re.compile(r"[::((\[【「、,,。//\n]")


def _clip(text: str, n: int) -> str:
    """n 字に収める。はみ出すときは末尾を「…」にする。"""
    text = " ".join(text.split())
    return text if len(text) <= n else text[: max(n - 1, 0)] + "…"


def skill_heading(skill: str) -> str:
    """スキル欄の1項目から、先頭の短い語句(見出し)を取り出す。"""
    head = _HEADING_END.split(skill.strip(), maxsplit=1)[0].strip() or skill.strip()
    return _clip(head, HEADING_CHARS)


def _forms(label: str, items: list[str]) -> list[tuple[int, str]]:
    """1つの項目の出し方を (詳しさ, 文) で、短い順に並べる。詳しさはどの項目でも同じ物差し。

    0「資格 N 件」→ 1・2 1件目を切り詰めて「ほか N 件」→ 3・4・5 1件・2件・3件をそのまま(超えた分は「ほか N 件」)。
    """
    def line(shown: list[str]) -> str:
        rest = f" ほか {len(items) - len(shown)} 件" if len(items) > len(shown) else ""
        return f"{label}: " + "、".join(shown) + rest

    forms = [(0, f"{label} {len(items)} 件")]
    forms += [(d, line([_clip(items[0], n)])) for d, n in ((1, 6), (2, 10)) if len(items[0]) > n]
    forms += [(2 + k, line(items[:k])) for k in range(1, min(len(items), MAX_ITEMS) + 1)]
    return forms


def _pack(lines: list[str], limit: int) -> list[str] | None:
    """INTRO に続けて lines を順に詰める。吹き出しが limit 個に収まらなければ None。"""
    bubbles = [INTRO]
    for text in lines:
        if len(bubbles[-1]) + 1 + len(text) <= MAX_CHARS:
            bubbles[-1] += "\n" + text
        elif len(bubbles) < limit and len(text) <= MAX_CHARS:
            bubbles.append(text)
        else:
            return None
    return bubbles


def _unconfirmed(items: list[str]) -> str:
    rest = f"\nほか {len(items) - 1} 件は「読み取り結果を見る」で確認できます。" if len(items) > 1 else ""
    label = "読み取れなかった点: "
    return label + _clip(items[0], MAX_CHARS - len(label) - len(rest)) + rest


def resume_bubbles(resume: dict, *, empty: bool = False) -> list[str]:
    """読み取り結果(resume_draft.schema.ResumeDraft の形)から、トークに出す確認の吹き出しを作る。"""
    unconfirmed = [x for x in resume.get("unconfirmed", []) if x.strip()]
    if empty:
        return [EMPTY, *([_clip(unconfirmed[0], MAX_CHARS)] if unconfirmed else [])]

    work = [_clip("・".join(x for x in (w.get("organization", ""), w.get("employment_type", "")) if x), 30)
            for w in resume.get("work_history", [])]
    skills = list(dict.fromkeys(h for h in (skill_heading(s) for s in resume.get("skills", [])) if h))
    certs = [_clip(c.get("name", ""), 24) for c in resume.get("certifications", []) if c.get("name")]
    school = [_clip(e.get("school", ""), 24) for e in resume.get("education", []) if e.get("school")]
    tail = [_unconfirmed(unconfirmed)] if unconfirmed else []
    limit = MAX_BUBBLES - len(tail)  # 要点に使える吹き出しの数
    # 職歴・スキル・資格は空でなければ必ず出す(いちばん短い形なら必ず収まる)。順番は職歴→スキル→資格のまま
    # 収まる組み合わせのうち、いちばん短く出る項目がなるべく詳しいもの→全体がなるべく詳しいもの→前の項目が
    # 詳しいもの、を選ぶ(組み合わせは多くて 6×6×6 通り)
    sections = [_forms(label, items) for label, items in
                (("職歴", [w for w in work if w]), ("スキル", skills), ("資格", certs)) if items]
    fits = [c for c in product(*sections) if _pack([text for _, text in c], limit)]
    best = max(fits, key=lambda c: (min(d for d, _ in c) if c else 0, sum(d for d, _ in c), [d for d, _ in c]))
    lines = [text for _, text in best]
    # 学歴は優先度を下げる(全文は「読み取り結果を見る」にある)。3項目を出し切って空きがあるときだけ出す
    if school and all(chosen == f[-1] for chosen, f in zip(best, sections)):
        for _, form in reversed(_forms("学歴", school)[1:]):
            if _pack([*lines, form], limit):
                lines.append(form)
                break
    bubbles = _pack(lines, limit)
    assert bubbles is not None  # いちばん短い形なら必ず収まる(「職歴 N 件」などは数字だけ)
    return bubbles + tail
