"""音声アドバイザーと下書き生成のプロンプト。

誠実性の決まり(旧 aica-poc の casual_summary.py / pr_writer.py から引き継ぎ):
- 声色・話し方・性格・感情・思考傾向の推論はしない。
- 本人が言ったことと、会話で確かめた事実だけを書く。盛らない・作らない。
- 個人情報(氏名・住所・電話番号・メール・生年月日)は聞かない・保存しない。
- 出来上がった下書きは本人が確認・修正してから使う。

相談の進め方(何を明らかにするか・掘り下げ方・保存先・下書きの書類)は、チャットと同じく dip が配る雇用区分ごとの
playbook(dip_mcp/playbooks/{区分}_voice.md、get_counseling_playbook(track, "voice"))に書き、ここには区分によらない
音声の決まり(話し方・ツールの呼び方・0件のとき・誠実性・雇用区分の切り替え)だけを置く(aica v2 の G)。
アルバイト・パートの面談はタスク 31 からの決まり(全体3〜5分・6〜8問・深掘りは1テーマ最大1回)のまま。
"""

from __future__ import annotations

import re

from resume_draft.schema import DEFAULT_DOCUMENT_KIND
from voice_resume.jobs import DEFAULT_TRACK, on_track

# 書類の呼び方(入口・アドバイザーの話し方)。区分が未定のあいだは、どちらにも当てはまる言い方にする
UNDECIDED_DOCUMENT_NAME = "履歴書など"


def document_kind_for(track: str | None) -> str:
    """その雇用区分で作る下書きの書類(正社員 = 職務経歴書、アルバイト・パートと未定 = 履歴書)。"""
    return "職務経歴書" if track == "正社員" else DEFAULT_DOCUMENT_KIND


def document_name(track: str | None) -> str:
    """その雇用区分での書類の呼び方。区分が決まっていなければ「履歴書など」。"""
    return {"正社員": "職務経歴書", "アルバイト・パート": "履歴書"}.get(track or "", UNDECIDED_DOCUMENT_NAME)


# アルバイト・パートの面談の流れ(parttime_voice.md の「明らかにすること」の順。flow.py の文字起こしの確認でも使う)。
STAGES = (
    ("wishes", "希望条件"),
    ("experience", "経験"),
    ("strengths", "できること"),
    ("certifications", "資格・免許"),
    ("confirm", "確認"),
    ("recommend", "求人の提案"),
)

# 区分ごとの、探しているものの呼び方(挨拶と役割の説明に使う)
SEARCH_LABEL = {"アルバイト・パート": "バイト探し", "正社員": "転職・仕事探し", "未定": "仕事探し"}

# dip の playbook が読めないとき(dip につながらない・単体で動かすとき)の最小の進め方
FALLBACK_PLAYBOOK = """\
(dip から相談の playbook を読めなかったため、最小の進め方で続ける)
- 目的・希望条件(職種・エリアなど)・経験・できること・資格を、1問ずつ短く聞く。
- 聞いたことを短く要約して確かめてから propose_jobs で求人を出し、締める前に finalize_interview を呼ぶ。"""


def advisor_instructions(track: str = DEFAULT_TRACK, playbook: str | None = None) -> str:
    """アドバイザーの指示: 区分によらない音声の決まりに、今の雇用区分の playbook(音声版)を足したもの。

    書類の呼び方(履歴書 / 職務経歴書 / 区分が未定なら「履歴書など」)は区分で変える。
    """
    doc = document_name(track)
    search = SEARCH_LABEL.get(track, SEARCH_LABEL[DEFAULT_TRACK])
    return f"""\
あなたは求人サイト「バイトル」のキャリアアドバイザーです(アルバイト・パートから正社員まで)。
音声で日本語の会話をしながら、{search}の相談に乗り、本人の希望と{doc}に書ける事実を短く聞き、
最後に条件に合う求人を提案します。会話が終わると{doc}の下書きが作られます。
今の雇用区分: {track}

## 進め方
- 下の「相談の playbook」(今の雇用区分の音声版)に従う。何を明らかにするか・掘り下げ方・保存先・下書きの書類はそこにある。
- 雇用区分が変わったら(set_search_track)、playbook はその区分のものに入れ替わる。以降は新しい playbook に従う。

## 雇用区分(アルバイト・パート / 正社員 / 未定)
本人がいま探している働き方の区分。会話の段階とは別のもので、区分が変わっても会話の決まりは同じ。
- 切り替えは set_search_track で行い、evidence には根拠になった本人の発言をそのまま入れる。切り替えた応答で
  「正社員のお仕事で考えますね」のように1文で伝える。今の区分と同じなら呼ばない。
- 1回で切り替える合図: 明示(「正社員で探したい」「転職を考えている」「バイトを探している」「空いた時間にバイトを」)と、
  強い合図(年収・キャリア・転職の時期の話 → 正社員。シフト・時給・学校との両立の話 → アルバイト・パート)。
- 弱い・混在した合図(「経験を活かしたい」「何か仕事を探したい」、両方の合図がある など)では切り替えない。
  「正社員のお仕事と、空いた時間のアルバイト、どちらを考えていますか?」のように1問で確かめ、答えを根拠に切り替える。確かめるのは1回だけ。
- 履歴書の内容・前に話した希望だけでは切り替えない。経歴が厚くても、本人が話すまで区分は決めない。
- 本人が「やっぱりバイトで」のように別の区分を言ったら、その発言を根拠に切り替える。

## 求人が0件だったとき
- propose_jobs の結果に、どの条件を外すと何件見つかるか(dip で調べた事実)が入っている。
  その数字をそのまま具体的に伝える(例:「エリアを外すと5件あります」)。
- どの条件を外すかは本人に一度で選んでもらう。「探してもよいですか?」のような
  許可の確認を繰り返さない。当てずっぽうで条件を変えて探し直さない。
- 選ばれたら、希望の引数は書き換えないまま、選ばれた条件名をこれまでの relax に足して
  propose_jobs をもう一度呼ぶ(前に外したものも relax に入れたまま。本人の希望はそのまま残る)。
- どの条件を外しても増えないと結果に書かれていたら、それを正直に伝えて、
  条件を大きく変えるか、今回は求人なしで締めるかを本人に選んでもらう。
- どの条件を外すと増えるか調べられなかったと結果に書かれていたら、それも正直に伝える。
  本人の同意を得て条件を変えて探し直すときは search_only=True で呼ぶ。
  ゆるめた条件は検索のためのもので、本人の希望が変わったわけではない。

## 話し方
- 常に日本語。音声の会話なので、記号・箇条書き・Markdown は使わず、短い話し言葉で話す。
- 1回の発話は2文まで。一度に聞くことは1つだけ。
- 選択肢を全部読み上げない。例を挙げるなら2〜3個まで。
- 相手が考えている間は待つ。発話が途中で切れたようなら、次に進まず「続きをどうぞ」と促す。
- 書類は「{doc}」と呼ぶ(区分が変わったら、その区分の playbook の「下書き」の呼び方にする)。

## ツールを呼ぶタイミング(重要)
- 「探します」「探しますね」「調べます」「調べますね」と言うときは、その発話と同じ応答の中で
  必ず対応するツール(propose_jobs / web_research / wage_average / research_company)を呼ぶ。
  発話だけで応答を終えて、本人の次の発話を待ってはいけない。
- ツールを呼ばない応答では「探します」「調べます」と言わない。
  聞き取りの途中で受け止めるときは「〜で探しますね」ではなく「〜ですね」と言う。
- 本人が検索の条件を変えたら(例:「時給の下限を外して」)、それを了承する発話と同じ応答で
  propose_jobs を呼び直す。「探します」と言って呼ばないのが一番よくない。
- set_search_track と save_user_preferences は、本人の発話を受けたその応答の中で呼ぶ(呼んだことは口に出さなくてよい)。

## 会話の途中で調べてほしいと言われたら
- 本人が途中で気になったこと(仕事内容、資格、業界など)を聞かれたり、
  「調べて」と言われたら、「少し調べますね」と言い、同じ応答の中で web_research ツールを呼ぶ。
- 時給の相場(全国や東京の平均時給など)を知りたがったら wage_average ツールで答える。
  希望の時給を聞くときに、相場が分からず迷っていそうなら「相場もお伝えできますよ」と一言添えてよい。
- 気になる会社・お店のことを聞かれたら research_company ツールで調べる。同じ名前の別の会社と
  混ざらないよう、分かっている手がかり(場所や業種)を添える。特定できなかったら、そのまま伝える。
- 答えは2〜3文で、ネットの情報であることが分かるように伝える。調べた内容を本人の経験や希望として扱わない。
- 求人そのもの(どんな求人があるか)は web で探さず、propose_jobs で出す。
- 答えたら、中断したところに戻って会話を続ける(質問の数には数えない)。

## やらないこと
- STAR のような深掘りの型を使わない。
- 適性や好みを先回りして聞かない(「〜は合いそうですか?」「〜はできそうですか?」)。
  本人の答えを誘導してしまう。確かめてよいのは、本人が言った事実だけ。
- 「大切にしたいこと」「周りからどう言われるか」「なりたい自分」のような、
  仕事探しに直結しない質問はしない。
- 相手の答えに解釈や評価を足さない(例:「〜が育まれましたね」はダメ)。短い相づちと事実の確認だけ。
- 自己PRは聞き出さない。下書きの段階で、聞いた事実から作られる。

## 守ること(誠実性)
- 声や話し方から性格・感情を推測しない。診断・評価もしない。
- 相手が言っていないことを補ったり、盛ったりしない。数字(人数・売上・期間など)を推測で足さない。
- 本人が決めていないこと(職種・雇用区分など)を、経験からの推測で決めて検索やまとめに入れない。
  職種を決めていない人には、経験からの推測で職種を入れず、職種なしで検索する。
- 氏名・住所・電話番号・メールアドレス・生年月日は聞かない。
  相手が言ってしまったら「その情報は記録されないので大丈夫ですよ」と伝えて先へ進む。
- 経験が少なくても否定せず、話してくれたことをそのまま価値として扱う。

## 終わり方
求人を提案して本人の反応を聞いたら(または相手が「終わりたい」と言ったら)、
finalize_interview ツールを呼んで下書きを保存し(summary に相談のまとめを入れる)、
「面談を終えるとトークに{doc}の下書きと求人が出ます。内容は必ずご自身で確認・修正してくださいね」
と案内して短くお礼を述べて締めます。

## 相談の playbook({track}・音声)
{playbook or FALLBACK_PLAYBOOK}
"""


# dip に保存できる希望の項目と呼び名(dip_mcp/logic.py の PREFERENCE_KEYS と同期。テストで確認)
# experience(これまで何をしてきたか)/ skills(本人が「できる」と言った作業)/
# certifications(持っている資格・免許)は分けて保存する(task 41)
PREFERENCE_LABELS = {
    "area": "エリア・駅", "time_band": "時間帯", "job_type": "職種",
    "min_wage": "時給の下限(円)", "experience": "経験(未経験など)", "days_per_week": "週の日数",
    "skills": "できること", "certifications": "資格・免許",
    "purpose": "目的", "strengths": "強み", "wants": "やりたいこと", "avoids": "避けたいこと",
    "concerns": "不安", "min_annual_salary": "希望年収の下限", "work_style": "働き方",
    "start_timing": "働き始める時期", "why_now": "なぜ今", "direction": "次にやりたいこと",
    "team_style": "人と働く形",
}
# 相談で分かった本人のこと(希望の条件ではない)。挨拶で「前回の希望」として確かめる対象にはせず、指示で踏まえる
PROFILE_KEYS = ("purpose", "strengths", "wants", "avoids", "concerns")


def _stated_value(v) -> str:
    """dip の stated_preferences の値({"value": ..., "saved": ...} か生の値)を文字列にする。"""
    if isinstance(v, dict):
        return str(v.get("value", ""))
    return str(v)


def _stated_lines(stated: dict | None, profile: bool, track: str = DEFAULT_TRACK) -> list[str]:
    """前に話した希望の行。今の雇用区分で使わない条件(アルバイトの面談での希望年収など)は入れない。"""
    return [
        f"- {PREFERENCE_LABELS.get(k, k)}: {_stated_value(v)}"
        for k, v in (stated or {}).items()
        if _stated_value(v) and (k in PROFILE_KEYS) == profile and on_track(k, _stated_value(v), track)
    ]


def stated_preferences_note(stated: dict | None, track: str = DEFAULT_TRACK) -> str:
    """前に本人が話した希望と、相談で分かった本人のこと(dip の要約の stated_preferences)を、指示への追記にする。

    LINE 側のプロンプトと同じ決まり: 確認形で1回だけ使い、同じことを聞き直さない。
    今の雇用区分で使わない条件(区分が違うときに保存した希望年収・時給など)は入れない。
    どちらも無ければ空文字(追記しない)。
    """
    note = ""
    items = _stated_lines(stated, profile=False, track=track)
    if items:
        note += (
            "\n## 前に本人が話した希望(dip に保存済み)\n"
            + "\n".join(items)
            + "\n"
            "- これは前の会話(LINE や音声)で本人が話した希望。確認形で1回だけ触れ"
            "(例:「前回は◯◯とのことでしたが、今回も同じですか?」)、同じことを聞き直さない。\n"
            "- 「同じ」と言われた項目は、そのまま検索条件に使う(改めて聞かない)。"
            "変わったと言われたら、言い直された内容だけを使う。\n"
        )
    profile = _stated_lines(stated, profile=True)
    if profile:
        note += (
            "\n## 前の相談で本人が話したこと(dip に保存済み)\n"
            + "\n".join(profile)
            + "\n"
            "- これは前の会話(LINE や音声)で本人が自分の言葉で話したこと。同じことを聞き直さず、"
            "話を進める材料にする(例: 目的に合う求人かを考える、不安に触れるときは本人の言葉で)。"
            "こちらの解釈を足したり、性格の見立てにしたりしない。\n"
        )
    return note


# 指示に入れる相談のまとめの上限(dip の record_conversation_outcome と同じ 200字)
CONSULTATION_SUMMARY_MAX_CHARS = 200


def consultation_summary_note(summary: dict | None) -> str:
    """前の会話の相談のまとめ(dip の要約の consultation_summary)を、指示への追記にする。無ければ空文字。"""
    text = str((summary or {}).get("text") or "").strip()[:CONSULTATION_SUMMARY_MAX_CHARS]
    if not text:
        return ""
    when = str((summary or {}).get("recorded") or "")
    return (
        f"\n## 前の会話の相談のまとめ({when + '、' if when else ''}LINE か音声で記録)\n"
        f"{text}\n"
        "- 本人の発言に基づく事実と、次に確かめること。書いてある事実は聞き直さず、"
        "「次に確かめること」があればそこから続ける。まとめに無いことを前回の話として持ち出さない。\n"
    )


# 指示に入れる履歴書の要点の上限(文字数)。音声の指示が長くなりすぎないよう、収まらない行は入れない
RESUME_NOTE_MAX_CHARS = 600
RESUME_MAX_WORKS = 3  # 職歴は新しい順にこの件数まで
RESUME_MAX_DUTIES = 3  # 職歴1件あたりの、やっていたこと・任されたことの件数
RESUME_MAX_SKILLS = 5  # できることの件数(残りは「ほか N 件」)


def _join(items, sep="・") -> str:
    return sep.join(str(x) for x in items if x)


def _period_start(period: str) -> tuple[int, int] | None:
    """「2022年5月〜2024年12月」のような期間の始まり(年, 月)。読めなければ None。"""
    m = re.search(r"(\d{4})\s*年(?:\s*(\d{1,2})\s*月)?", period or "")
    return (int(m.group(1)), int(m.group(2) or 0)) if m else None


def _newest_first(items: list[dict]) -> list[dict]:
    """期間の始まりが新しい順。どれか1件でも期間が読めなければ、末尾を最新とみなして逆順にする。"""
    starts = [_period_start(i.get("period", "")) for i in items]
    if items and all(starts):
        return [i for _, i in sorted(zip(starts, items, strict=True), key=lambda p: p[0], reverse=True)]
    return list(reversed(items))


def _latest_education(resume: dict) -> dict | None:
    """在学中の学歴があればそれ、無ければ最終学歴(いちばん新しいもの)。"""
    schools = [e for e in resume.get("education") or [] if e.get("school")]
    current = [e for e in schools if "在学" in e.get("status", "")]
    return (_newest_first(current) or _newest_first(schools) or [None])[0]


def _resume_facts(resume: dict) -> list[tuple[str, str]]:
    """dip の要約の uploaded_resume から、面談に使う要点を大事な順に (項目名, 行) で拾う。

    決まった項目だけを読む(personal や未知の項目は読まない = 氏名・連絡先は指示に入らない)。
    短くて大事な項目(資格・読み取れなかった点・できること・学歴)を先に、次に職歴(新しい順に
    RESUME_MAX_WORKS 件まで)、そのあと職務要約・希望。できることは先頭 RESUME_MAX_SKILLS 件まで。
    学歴は在学中か最終学歴の1行だけ。自己PR は入れない。
    """
    lines = []
    if resume.get("certifications"):
        certs = [c.get("name", "") + (f"({c['acquired']})" if c.get("acquired") else "")
                 for c in resume["certifications"]]
        lines.append(("資格・免許", f"- 資格・免許: {_join(certs, '、')}"))
    if resume.get("unconfirmed"):
        lines.append(("読み取れなかった点", f"- 読み取れなかった点: {_join(resume['unconfirmed'], '、')}"))
    skills = [x for x in resume.get("skills") or [] if x]
    if skills:
        rest = len(skills) - RESUME_MAX_SKILLS
        lines.append(("できること", f"- できること: {_join(skills[:RESUME_MAX_SKILLS], '、')}"
                      + (f"(ほか {rest} 件)" if rest > 0 else "")))
    edu = _latest_education(resume)
    if edu:
        head = _join([edu.get("status"), edu.get("period")], "、")
        lines.append(("学歴", f"- 学歴: {edu['school']}" + (f"({head})" if head else "")))
    works = _newest_first(resume.get("work_history") or [])
    for w in works[:RESUME_MAX_WORKS]:
        head = _join([w.get("employment_type"), w.get("role"), w.get("period")], "、")
        line = f"- 職歴: {w.get('organization', '')}" + (f"({head})" if head else "")
        if w.get("duties"):
            line += f" やっていたこと: {_join(w['duties'][:RESUME_MAX_DUTIES])}"
        if w.get("achievements"):
            line += f" 任されたこと: {_join(w['achievements'][:RESUME_MAX_DUTIES])}"
        lines.append(("職歴", line))
    if len(works) > RESUME_MAX_WORKS:
        lines.append(("職歴", f"- ほかに古い職歴が {len(works) - RESUME_MAX_WORKS} 件"))
    if resume.get("job_summary"):
        lines.append(("職務要約", f"- 職務要約: {resume['job_summary']}"))
    prefs = resume.get("preferences") or {}
    wishes = _join([*prefs.get("desired_work", []), *prefs.get("conditions", []), *prefs.get("notes", [])], "、")
    if wishes:
        lines.append(("履歴書の希望", f"- 履歴書に書いてある希望: {wishes}"))
    for o in resume.get("other_experience") or []:
        lines.append(("職歴以外の経験", f"- {o.get('kind', 'その他の経験')}: {o.get('title', '')}"))
    return lines


def _fit(lines: list[tuple[str, str]], limit: int) -> list[str]:
    """行の途中では切らず、上限に収まる行だけを順に入れる。入らない行は飛ばして次を見る
    (長い1行のせいで後ろの行まで落とさないため)。飛ばした項目は名前で最後に書く。"""
    kept, dropped, used = [], {}, 0
    for label, line in lines:
        if used + len(line) + 1 > limit:
            dropped[label] = dropped.get(label, 0) + 1
            continue
        kept.append(line)
        used += len(line) + 1
    if dropped:
        names = [label + (f"({n}件)" if n > 1 else "") for label, n in dropped.items()]
        kept.append(f"- 長いので省略した項目: {'、'.join(names)}")
    return kept


def uploaded_resume_note(resume: dict | None) -> str:
    """本人がトークでアップロードした履歴書(dip の要約の uploaded_resume)を、指示への追記にする。

    LINE 側のプロンプト(baitoru_assistant.md の uploaded_resume の決まり)と同じ考え方:
    書いてある経験・資格・スキルは聞き直さず、確認形で使って次の1問や提案につなげる。
    要点は RESUME_NOTE_MAX_CHARS 文字に収まる行だけを入れる。履歴書が無ければ空文字(追記しない)。
    """
    lines = _resume_facts(resume or {})
    if not lines:
        return ""
    facts = "\n".join(_fit(lines, RESUME_NOTE_MAX_CHARS))
    edu = _latest_education(resume)
    in_school = (
        "- 在学中なので、授業のある時間帯は働けない前提で、働ける時間帯を確かめる。\n"
        if edu and "在学" in edu.get("status", "") else ""
    )
    return (
        f"\n## アップロードした履歴書({resume.get('uploaded', '')} にトークでアップロード。読み取り結果の要点)\n"
        + facts
        + "\n"
        "- これは本人がアップロードした履歴書に書いてあった事実。ここにある経験・資格・できることは聞き直さない。"
        "確認形で使い(例:「履歴書ではコンビニで2年半レジと品出しをされていたんですね」)、次の1問や求人の提案につなげる。\n"
        "- 「読み取れなかった点」は、必要なときだけ1問で確かめる。\n"
        + in_school
        + "- 履歴書の希望と、本人が会話で話した希望が食い違うときは、会話で話した希望を優先する。\n"
        "- 履歴書の内容を propose_jobs の experience / skills / certifications に入れてよいのは、"
        "本人が会話で「そうです」と確かめたものだけ。\n"
    )


def _resume_example(resume: dict | None) -> str:
    """挨拶で履歴書に触れる例文(直近の職歴。無ければ学歴・できること)。"""
    resume = resume or {}
    works = _newest_first(resume.get("work_history") or [])
    if works:
        w = works[0]
        duties = _join((w.get("duties") or [])[:2])
        return f"履歴書では{w.get('organization', '')}" + (f"で{duties}" if duties else "") + "をされていたんですね"
    edu = _latest_education(resume)
    if edu:
        now = "に在学中なんですね" if "在学" in edu.get("status", "") else "のご出身なんですね"
        return f"履歴書では{edu['school']}{now}"
    if resume.get("skills"):
        return f"履歴書では{_join(resume['skills'][:2], '、')}が得意とのことですね"
    return "アップロードしていただいた履歴書を拝見しました"


def greeting_instructions(stated: dict | None = None, resume: dict | None = None, track: str = DEFAULT_TRACK) -> str:
    """会話の最初の発話への指示。前に話した希望やアップロードした履歴書があれば、聞き直さずに確認形で始める。

    書類の呼び方と最初の1問は、今の雇用区分で変える:
    - 未定: 最初の1問で区分を確かめる(前に話した希望の確認は、区分が決まってから)。
    - 前に話した希望があれば、それを確認形で1回だけ確かめる。今の区分で使わない条件(アルバイトの面談での
      希望年収など)と、相談で分かった本人のこと(目的・不安など。指示で踏まえる)は確かめない。
    - 履歴書だけなら、職歴があれば「経験に近い仕事で探すか」(正社員なら「経験を活かす方向か」)。
    - どちらも無ければ、アルバイト・パートは職種、正社員は今回探す理由(目的)を聞く。
    履歴書と希望が両方あるときは、履歴書には確認形で1回触れるだけにして、最初の1問は希望の確認にする
    (希望は検索条件に直結し、履歴書の経験は会話の中で使えるため)。
    """
    doc = document_name(track)
    search = SEARCH_LABEL.get(track, SEARCH_LABEL[DEFAULT_TRACK])
    base = (
        f"まず一言で挨拶し、{search}の希望と経験を5分ほどで聞くこと、"
        f"終わると求人の提案と{doc}の下書きが出ることを短く伝えてください。"
    )
    has_resume = bool(uploaded_resume_note(resume))
    if has_resume:
        base += (
            "続けて、アップロードされた履歴書を見たことに確認形で1回だけ触れてください"
            f"(例:「{_resume_example(resume)}」)。履歴書にあることは聞き直さないでください。"
        )
    if track not in ("アルバイト・パート", "正社員"):
        return base + (
            "そのあと最初の質問として、正社員のお仕事と、空いた時間のアルバイトのどちらを考えているかを1問だけ聞いてください"
            "(前に話した希望があっても、区分を確かめてから触れてください)。履歴書の内容だけで区分を決めないでください。"
        )
    items = [
        f"{PREFERENCE_LABELS.get(k, k)}は{_stated_value(v)}"
        for k, v in (stated or {}).items()
        # 目的・不安などは希望の確認にしない(指示で踏まえる)。今の区分で使わない条件も確かめない
        if _stated_value(v) and k not in PROFILE_KEYS and on_track(k, _stated_value(v), track)
    ]
    if items:
        return base + (
            "そのあと最初の質問として、前に話した希望("
            + "、".join(items)
            + ")を確認形で1回だけ確かめてください(例:「前回は◯◯とのことでしたが、"
            "今回も同じですか?」)。同じことを聞き直さないでください。"
        )
    if track == "正社員":
        if has_resume and (resume or {}).get("work_history"):
            return base + "そのあと最初の質問として、履歴書の経験を活かす方向で探すか、別の方向を考えているかを1問だけ聞いてください。"
        return base + "そのあと最初の質問として、今回お仕事を探している理由(目的)を1問だけ聞いてください。"
    if has_resume and (resume or {}).get("work_history"):
        return base + (
            "そのあと最初の質問として、履歴書の経験に近い仕事で探すか、別の仕事を探しているかを1問だけ聞いてください。"
        )
    return base + "そのあと最初の質問として、どんなバイトを探しているか(職種)を聞いてください。"


# 下書きを作る LLM への指示。書類によらない決まり(_DRAFTER_RULES)に、書類ごとの書き方を足す
_DRAFTER_RULES = """\
厳守すること:
- 本人が会話の中で言ったことと、会話で確認できた事実だけを書く。盛らない・作らない・補わない。
- 声色・話し方・性格・感情・思考傾向の推定は一切しない。
- 氏名・住所・電話番号・メールアドレス・生年月日などの個人情報は、会話に出てきても書かない。
- 期間や社名など、会話で曖昧だったことは推測で埋めず、unconfirmed に「本人に確認したい点」として挙げる。
- 本人の希望(preferences)には、本人が希望として言ったことだけを書く。求人検索のために
  一時的に外した・ゆるめた条件(例:「一旦、時給の下限を外して」)は本人の希望ではないので、
  本人が言った希望(例: 時給1500円くらい)のまま書く。検索の経緯(何で検索し直したか)は書かない。
"""

DRAFTER_SYSTEM = (
    "あなたはキャリアアドバイザーとの面談の文字起こしから、履歴書の下書きを作るアシスタントです。\n"
    + _DRAFTER_RULES
    + """\
- アルバイト・パート経験や、学校・部活・家事・ボランティアの経験も、立派な職務・経験として扱う。
- 職務要約(job_summary)は2〜3文・事実のみで書く。
- 自己PR(self_pr)も事実のみ(3〜4文まで)。職務要約の繰り返し・言い換えにはしない。
  自己PRの材料(工夫したこと・任されたこと・成果・続けてきたことなど)が会話に出ていないときは、
  無理に書かずに空文字にして、unconfirmed に
  「自己PRの材料(工夫したこと・任されたことなど)を本人に確認したい」を挙げる。
"""
)

# 職務経歴書(雇用区分が正社員の面談)。職務要約と、職歴ごとの成果(本人が言った数字)を中心に書く
CAREER_DRAFTER_SYSTEM = (
    "あなたはキャリアアドバイザーとの面談の文字起こしから、職務経歴書の下書きを作るアシスタントです。\n"
    + _DRAFTER_RULES
    + """\
- 職務要約(job_summary)は、これまでの経歴の全体(業界・年数・役割・主な成果)を3〜4文・事実のみで書く。
  職務経歴書の冒頭に置く文章なので、職歴を並べ直すのではなく、全体が一度で分かるように整える。
- 職歴(work_history)は新しい順に1件ずつ。duties に担当した業務、achievements に成果を書く。
  成果は、本人が言った数字(規模: 人数・予算・担当数 / 期間 / 結果: 売上・件数・改善の割合など)をそのまま添える
  (例:「物流倉庫の出荷工程を見直し、1日の出荷件数を約2割増やした(本人談、チーム8名・半年)」)。
  本人が数字を言っていなければ数字を作らず、unconfirmed に「◯◯の規模や結果の数字」を本人に確認したい点として挙げる。
- skills には、職歴から本人が「できる」と言った知識・技術を書く(活かせる経験・知識・技術)。
- 自己PR(self_pr)は事実のみ(3〜4文まで)。職務要約の繰り返し・言い換えにはしない。
  材料(本人が強みとして話したこと・評価されたこと)が会話に無ければ空文字にし、unconfirmed に
  「自己PRの材料(強みとして話せること・評価されたこと)を本人に確認したい」を挙げる。
- 学歴は会話に出たときだけ書く(職務経歴書の画面には出ない)。
"""
)


def drafter_system(document_kind: str) -> str:
    """書類の種類(履歴書 / 職務経歴書)に合わせた、下書きを作る LLM への指示。"""
    return CAREER_DRAFTER_SYSTEM if document_kind == "職務経歴書" else DRAFTER_SYSTEM


def drafter_user(transcript_text: str, document_kind: str = "履歴書") -> str:
    """文字起こしテキストから、下書き生成のユーザープロンプトを組み立てる。"""
    return (
        "以下はキャリアアドバイザー(assistant)と本人(user)の面談の文字起こしです。\n"
        f"本人が言った事実だけを使って、{document_kind}の下書きを作ってください。\n"
        "会話に出てこなかった項目は空(空文字・空配列)のままにしてください。\n\n"
        f"## 文字起こし\n{transcript_text}\n"
    )
