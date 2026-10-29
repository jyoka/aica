"""トークの AI(相談の外)の t1(TALK_LAYER_VERSION=t1。設計メモ docs/design/conversation-layer.md §4.13、ADR-0011)。

トークの AI はエージェントのまま(何をするかが開いているので、質問バンクで聞くことを決めない)。会話の層から足すのは 3 つだけ。

1. 守りの前処理: 本人の発言に危ない言葉(dip_mcp.convo.guard の表)があれば、LINE 側のランタイムがトークの AI を呼ばずに
   決まった文(crisis_reply。前後は talk.json の丁寧語、窓口はどの入り口も同じ)を返す(line_runtime.runtime._turn)
2. プロンプトに、aica の事実(dip_mcp/convo/facts.json)と、区分が分からないときに入り口を選ぶボタンで 1 問だけ聞く決まりを足す
   (prompt_section。dip の Prompt baitoru_assistant の後ろに付く)。ボタンの文は、入り口の定義の移る先の文(entry_buttons)
3. start_baito_consultation / start_career_consultation に本人の発言(text)を渡す決まり(同じく prompt_section)。
   区分が決まって探したいと言ったら、条件を聞かずに同じターンで相談を始める(start_rule。task 377)。
   移る先が会話の層の版なら、dip は始めずに引き継ぎの記録(from は talk)を書き、LINE 側が同じターンで switch_in で始める。
   移る先は、その発言を読む段で読み、値を当ててから最初の問いを選ぶ(§4.5。dip_mcp.career.session.switch_in など)

音声相談の指示(voice_resume/prompts.py)にも、同じ事実と危ない言葉の文を足す(音声の会話の作りは変えない)。
voice_resume は dip のコードを読み込まないので、写しを持ち、テストで同じことを確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path

from dip_mcp import logic
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


START_TOOLS = (("アルバイト・パート", "start_baito_consultation"), ("正社員", "start_career_consultation"))


def start_tools() -> list[tuple[str, str]]:
    """トークの AI に見せる、相談を始めるツール(区分, 名前)。ハーネスの機能フラグがある区分だけ(line_runtime.runtime.chat_tools と同じ)。"""
    on = {"アルバイト・パート": logic.baito_harness_on(), "正社員": logic.career_harness_on()}
    return [(track, name) for track, name in START_TOOLS if on[track]]


def start_rule() -> str:
    """区分が決まって仕事を探したいと言ったターンに、条件を聞かずに相談を始める決まり(task 377)。
    dip の Prompt は、迷いを話したときだけ相談に入り、条件だけの発言(「土日だけ駅近でバイトしたい」)では相談に入らずにエリアを聞き、
    相談に入る前のアルバイト・パートへの切り替えでは playbook を読まない。そのままでは、実モデルのトークの AI が相談を始めずに
    求人を探すエリアを聞いた(task 362。場面 talk-baito-weekend、talk-after-voice)ので、t1 ではこの決まりを先にする。"""
    tools = start_tools()
    if not tools:
        return ""
    names = "、".join(f"{track}は {name}" for track, name in tools)
    return f"""- 区分が決まっていて(本人が区分を書いた、区分のボタンを押した、要約の search_track がもう決まっている)、本人がその区分で
  仕事を探したい・見たいと言ったら(「平日の夕方だけバイトしたい」「パートも見てみたい」「正社員でリモートの仕事を探したい」、
  区分のボタン)、エリアや時間帯などの条件を聞かず、求人も探さずに、同じターンで相談を始める。区分が今と違えば set_search_track で切り替え、
  get_counseling_playbook をその区分と channel=chat で読み、続けて相談を始めるツールを呼ぶ({names})。足りない条件は相談が聞く。
- この決まりは、上の「相談」の入口(迷いを話したときだけ相談に入る)、条件だけを言ったときに相談に入らず条件から探す決まり、
  相談に入る前のアルバイト・パートへの切り替えでは playbook を読まない決まり(get_counseling_playbook の説明にも同じことがある)より先にする。
- 相談を始めないのは、この会話でその区分の相談をもう終えた後(結果が出た後か「あとで」で閉じた後)に求人を見たいと言ったとき、
  特定の求人や aica について聞いたとき。そのときは今までどおりにする。
"""


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
- 本人がボタンを押したか区分を書いたら、set_search_track で切り替え、下の「相談を始めるとき」のとおりにする。

### 相談を始めるとき
{start_rule()}- start_baito_consultation と start_career_consultation の text には、条件や区分を書いた本人の発言を、本人の言葉のまま(言い換えず、足さず)渡す。
  dip がその発言を読み、書いてあった条件を聞き直さずに相談を始める。区分のボタンを押しただけのターンなら、その前に条件を書いた発言を渡す。
  条件を書いた発言が無ければ text は渡さない。
- start_career_consultation に text を渡すターンは、その発言の条件(年収・勤務地・働き方・時期)を save_user_preferences で保存しない。
  相談が発言を読んで推測として入れ、まとめで本人に確かめてから保存する(先に保存すると、確かめた値として入り、年収の数も読めない)。
- start_baito_consultation を呼ぶターンは、今までどおり本人が言った条件を save_user_preferences に保存する。曜日(「土日」「平日」)は、
  相談の保存と同じく time_band に入れる。
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
