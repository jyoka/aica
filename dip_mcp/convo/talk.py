"""トークの AI(相談の外)の t1(TALK_LAYER_VERSION=t1。設計メモ docs/design/conversation-layer.md §4.13、ADR-0011)。

トークの AI はエージェントのまま(何をするかが開いているので、質問バンクで聞くことを決めない)。会話の層から足すのは 3 つだけ。

1. 守りの前処理: 本人の発言に危ない言葉(dip_mcp.convo.guard の表)があれば、LINE 側のランタイムがトークの AI を呼ばずに
   決まった文(crisis_reply。前後は talk.json の丁寧語、窓口はどの入り口も同じ)を返す(line_runtime.runtime._turn)
2. プロンプトに、aica の事実(dip_mcp/convo/facts.json)と、区分が分からないときに入り口を選ぶボタンで 1 問だけ聞く決まりを足す
   (prompt_section。dip の Prompt baitoru_assistant の後ろに付く)。ボタンの文は、入り口の定義の移る先の文(entry_buttons)
3. start_baito_consultation / start_career_consultation に本人の発言(text)を渡す決まり(同じく prompt_section)。
   移る先が会話の層の版なら、dip は始めずに引き継ぎの記録(from は talk)を書き、LINE 側が同じターンで switch_in で始める。
   移る先は、その発言を読む段で読み、値を当ててから最初の問いを選ぶ(§4.5。dip_mcp.career.session.switch_in など)

音声相談の指示(voice_resume/prompts.py)にも、同じ事実と危ない言葉の文を足す(音声の会話の作りは変えない)。
voice_resume は dip のコードを読み込まないので、写しを持ち、テストで同じことを確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path

from dip_mcp.convo import facts as F
from dip_mcp.convo import guard

HERE = Path(__file__).resolve().parent
ENTRIES = HERE.parent  # 入り口の定義(<入り口>/entry.json)の親
DEF = json.loads((HERE / "talk.json").read_text(encoding="utf-8"))
CRISIS_TEXT = guard.crisis_text(DEF["texts"]["crisis_lead"], DEF["texts"]["crisis_tail"])
BUTTON_ORDER = ("baito", "career")  # 入り口を選ぶボタンの並び(アルバイト・パート、正社員。場面 talk-hello)


def crisis_reply(text) -> str | None:
    """本人の発言に危ない言葉があれば、決まった文(モデルを呼ばずに返す)。無ければ None。"""
    return CRISIS_TEXT if guard.crisis_hit(text or "") else None


def entry_buttons() -> list[str]:
    """入り口を選ぶボタンの文: 各入り口へ移るボタンの文(入り口の定義の switch の label。正社員の定義にあるアルバイト・パートへの文と、
    アルバイト・パートの定義にある正社員への文)。どちらの相談の間でも、相談の外でも、同じ文で移れるようにする。"""
    labels = {}
    for name in ("career", "baito"):
        d = json.loads((ENTRIES / name / "entry.json").read_text(encoding="utf-8"))
        for s in d.get("switch") or []:
            labels.setdefault(s["to"], s["label"])
    return [labels[e] for e in BUTTON_ORDER if e in labels]


def prompt_section() -> str:
    """トークの AI のプロンプトに足す t1 の決まり(dip の Prompt の後ろ)。事実は aica 全体の事実だけ(入り口の事実は相談の中で使う)。"""
    buttons = entry_buttons()
    quoted = "「" + "」「".join(buttons) + "」"
    service = "\n".join(f"  - {x}" for x in F.COMMON["ask_service"])
    advice = "\n".join(f"  - {x}" for x in F.COMMON["ask_advice"])
    return f"""
## 会話の層の決まり(t1)
### 区分が分からないとき
- summary の search_track が未定で、本人の発言からアルバイト・パートか正社員かが分からないとき(挨拶だけ、「仕事探してる」など)は、
  求人を探さず、区分を1問だけ聞く。quick_replies は次のボタンを、この順で文を変えずに付ける: {quoted}。ほかの選択肢は足さない。
  入口A の「メニュー代わりの選択肢」より、この決まりを先にする。返信は、本人の言葉を受ける短い1文と区分の1問で、1つの吹き出しを80字までにする。
- 本人がボタンを押したか区分を書いたら、ふだんどおり set_search_track で切り替える。

### 相談を始めるとき
- start_baito_consultation と start_career_consultation の text には、条件や区分を書いた本人の発言を、本人の言葉のまま(言い換えず、足さず)渡す。
  dip がその発言を読み、書いてあった条件を聞き直さずに相談を始める。区分のボタンを押しただけのターンなら、その前に条件を書いた発言を渡す。
  条件を書いた発言が無ければ text は渡さない。
- 結果に switch_to があるときは、LINE 側が同じターンで、相談の最初の返信に置き換える。返信は短い1文だけにして、問いと選択肢は書かない。

### aica について聞かれたら
- aica やこの相談について聞かれたら(だれか、何に使うか、保存、消し方)、次の事実だけで答える。ここに無いことは、分からないと伝える。
{service}
- 求人や相場について聞かれたときの事実(相場の数は、dip のツールが返したもののほかは言わない):
{advice}

### 危ない言葉
- 死にたい・消えたいなどの言葉には、LINE 側があなたを呼ばずに、相談窓口の決まった文を返している。その後の発言では、仕事探しを急かさず、
  本人が続けたいと言うまで求人や相談の問いを出さない。
"""
