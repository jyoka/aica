"""正社員のヒアリングの職歴カードから、職務経歴書の下書きを作る(task 247。設計メモ docs/design/fulltime-hearing.md の §4、§5)。

LLM は呼ばない。書類の形はゲームの下書きと同じ Markdown(resume_draft.markdown。arcade-ja.js の resumeMarkdown の形)で、
置き場所もゲームの下書きと同じ(dip_mcp.arcade.save_career_draft)。下書きの画面(/liff/arcade/draft)で本人が見て直し、
空欄は下書きの空欄の仕組み(line-sim-scenarios.md §14)で後から埋める。

- 職歴カード 1 枚が職歴 1 件: 見出しは何をしていたか、行は規模(どのくらい)と結果(どうなったか)。新しい順(直近が先)
- 書くのは、本人の言葉(ボタンならラベル。区切りは「くらい」をつけたまま)だけ。言い換え・要約・強みの作文をしない
- 空欄: パス、「覚えていない」、聞かなかった、推測のまま確かめていない欄(harness.blank_cards と同じ欄)。規模は数字の空欄、
  何をしていたかとどうなったかは言葉の空欄。2 枚目は何をしていたかが埋まったときだけ書く
- 数字を作らない(§6.4 の関門「数字を作った: 0 件」): 欄の数字が、本人の発言か選んだ区切り(sources)に無ければ、数字の空欄に戻す
  (resume_lines.check_numbers)。前の相談で保存した値も、確かめた印(checked)が無ければ同じく発言と比べる(trusted)。
  書類の決まった文(見出しなど)には数字を入れない。結果の「空欄はNつ」は、この空欄も数える
  (harness._finish が build の空欄を数える)
- 個人情報らしい値と長すぎる値は、職歴カードの保存(store)と同じく書かず、空欄にする(harness.card_filled。空欄の数もそろう)
- 条件(勤務地、年収の下限、時期、働き方など)は書かない。応募先に出す書類なので(希望はプロフィールの希望にある)
"""

from __future__ import annotations

import re

from dip_mcp import arcade
from dip_mcp.career import harness as H
from dip_mcp.data import State
from resume_draft.markdown import to_envelope
from resume_lines import check_numbers, number_blank, words_blank

DOCUMENT_KIND = "職務経歴書"
NAME_LINE = "# [氏名]"
CONTACT_LINE = "[メール] · [電話] · [居住地] · [リンク]"
ROW_LABELS = {"scale": "規模", "result": "結果"}  # 書類の行の見出し(職歴カードの「どのくらい」「どうなったか」)
BLANKS = {"role": words_blank(H.CARD_LABELS["role"]), "scale": number_blank(ROW_LABELS["scale"]),
          "result": words_blank(H.CARD_LABELS["result"])}


def trusted(e: dict) -> bool:
    """この相談の発言には無いが、本人の言葉として数字を確かめる元にする欄か。履歴書の読み取り結果と、前の相談で保存した
    職歴カードの欄のうち、保存のときに数字を確かめた(か本人が直した)もの(checked。dip_mcp.career.store)。
    確かめた印の無い保存した値(この印より前の版で保存した値)は元にしない。出すときは、ほかの欄と同じく発言と比べる。"""
    return e.get("from") == "resume" or (e.get("from") == "saved" and bool(e.get("checked")))


def sources(h: H.State) -> list[str]:
    """数字を確かめる元: この相談の本人の発言(押したボタンのラベルも)と、履歴書・前の相談から入れた欄の言葉(trusted)。"""
    return [*h.said, *(e["text"] for e in h.slots.values() if trusted(e) and e.get("text"))]


def _value(h: H.State, n: int, f: str, src: list[str]) -> tuple[str, int]:
    """欄の書類の文と、本人の言葉に無かったので空欄に戻した数字の数。空欄の欄は空欄の文。"""
    e = h.slots.get(H.card_key(n, f))
    text = re.sub(r"\s+", " ", str((e or {}).get("text") or "")).strip()
    if not H.card_filled(e):
        return BLANKS[f], 0
    return check_numbers(text, src)


def build(h: H.State) -> tuple[str, int]:
    """職歴カードから下書きの Markdown を作る。(Markdown, 空欄に戻した数字の数)。"""
    src = sources(h)
    lines, replaced = [NAME_LINE, CONTACT_LINE, "", "## 職務要約", "", "## 職務経歴"], 0
    for n in range(1, H.CARD_COUNT + 1):
        if n > 1 and not H.card_filled(h.slots.get(H.card_key(n, "role"))):
            continue  # 2 枚目が無い人(harness.blank_cards と同じ)
        role, k = _value(h, n, "role", src)
        lines.append(f"### {role}")
        replaced += k
        for f in ("scale", "result"):
            v, k = _value(h, n, f, src)
            lines.append(f"- {ROW_LABELS[f]}: {v}")
            replaced += k
        lines.append("")
    return "\n".join(lines).strip() + "\n", replaced


def create(state: State, line_user_id: str, h: H.State) -> dict:
    """result に着いた相談の下書きを作って保存する(前の相談の下書きを置き換える)。返すもの: id、空欄の数(blanks)、
    空欄に戻した数字の数(replaced)、下書きの画面の URL(view_url。開けない窓口では None)。"""
    md, replaced = build(h)
    rec = arcade.save_career_draft(state, line_user_id, to_envelope(md, DOCUMENT_KIND))
    return {"id": rec["id"], "blanks": arcade.blanks(rec), "replaced": replaced,
            "view_url": arcade.view_url(line_user_id, rec["id"])}
