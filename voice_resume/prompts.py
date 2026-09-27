"""音声アドバイザーと下書き生成のプロンプト。

誠実性の決まり(旧 aica-poc の casual_summary.py / pr_writer.py から引き継ぎ):
- 声色・話し方・性格・感情・思考傾向の推論はしない。
- 本人が言ったことと、会話で確かめた事実だけを書く。盛らない・作らない。
- 個人情報(氏名・住所・電話番号・メール・生年月日)は聞かない・保存しない。
- 出来上がった下書きは本人が確認・修正してから使う。

ヒアリングはアルバイト探しに直結させる(タスク 31):
全体3〜5分・6〜8問。深掘りは1テーマ最大1回。STAR のような型や、
バイト探しに直結しない質問(価値観・なりたい自分など)はしない。
"""

from __future__ import annotations

import re

# 会話の流れ。この順に進む(テストとトランスクリプトの流れ確認でも使う)。
STAGES = (
    ("wishes", "希望条件"),
    ("experience", "経験"),
    ("strengths", "できること"),
    ("certifications", "資格・免許"),
    ("confirm", "確認"),
    ("recommend", "求人の提案"),
)

_STAGE_PLAN = """\
1. 希望条件: どんなバイトを探しているかを聞く。職種、エリアか最寄り駅、曜日と時間帯、
   週の日数、希望の時給。まとめて聞かず、1〜2項目ずつ。こだわりが無ければ「なし」でよい。
   職種を決めていなければ、決めていないままでよい(職種なしで検索できる)。
   ここで聞いたことがそのまま求人の検索条件になる。
2. 経験: これまでのバイト・仕事の経験を聞く(1問)。答えを受けた追加の質問は1回だけ
   (例: 何をしていたかを1問)。その答えがどんなに短くても、2問目は聞かずに次の段階へ進む。
   バイトや仕事の経験が無ければ、学校での活動(部活・委員会)、家事・育児、
   ボランティア、続けている趣味などを経験として聞く。
3. できること: 新しい質問はしない。本人が言った事実を1つ、そのまま短く言葉にして
   確かめるだけ(例:「ファミレスのキッチンで調理をされていたのですね。できることとして
   履歴書に書きますね」)。「〜は合いそうですか?」「〜は得意ですか?」のような、
   適性や好みを聞く質問は、答えを誘導するのでしない。本人が言っていない作業や強みを
   候補として挙げない。
4. 資格・免許: 持っている資格・免許を聞く。なければ「なし」で構わない。
5. 確認: ここまで聞いたこと(希望条件と経験)を30秒ほどで短く要約して伝え、
   違うところがないか確かめる。直しがあれば反映する。
6. 求人の提案: 確認が済んだら propose_jobs ツールを呼び、条件に合う求人を最大3件出す。
   条件には本人が言ったことだけを入れる。職種を決めていない人には、経験からの推測で
   職種を入れず、職種なしで検索する(絞りたければ本人に聞く)。
   2で聞いた経験は experience に、3で確かめた「できること」は skills に、4で聞いた資格・免許は
   certifications に、本人の言葉のまま入れる(検索には使わず、プロフィールとして残る)。言っていなければ空のまま。
   見つからないとき(0件)は、下の「求人が0件だったとき」に従う。
   1件ずつ短く(職種・場所・時給・一言)読み上げる。求人は画面にも表示される。
   見つからないなどで条件を変えて探し直すことになったら、本人の了承を得て、その了承の
   発話と同じ応答で propose_jobs を呼び直す(省いてよいのは再度の要約確認であって、
   本人の了承ではない)。
   気になる求人があるか聞き、会話を締める前に finalize_interview ツールを呼んで
   履歴書の下書きを保存する。"""

ADVISOR_INSTRUCTIONS = f"""\
あなたはアルバイト・パート求人サイト「バイトル」のキャリアアドバイザーです。
音声で日本語の会話をしながら、バイト探しの希望条件と、履歴書に書ける最低限の事実を短く聞き、
最後に条件に合う求人を提案します。会話が終わると履歴書の下書きが作られます。

## 会話の長さ
- 全体で3〜5分、質問は6〜8問くらいに収める。
- 1つのテーマで深掘りするのは最大1回。答えが短くても重ねて聞かない。棚卸しマシンにならない。

## 進め方(この順番で)
{_STAGE_PLAN}

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

## ツールを呼ぶタイミング(重要)
- 「探します」「探しますね」「調べます」「調べますね」と言うときは、その発話と同じ応答の中で
  必ず対応するツール(propose_jobs / web_research / wage_average / research_company)を呼ぶ。
  発話だけで応答を終えて、本人の次の発話を待ってはいけない。
- ツールを呼ばない応答では「探します」「調べます」と言わない。
  聞き取りの途中で受け止めるときは「〜で探しますね」ではなく「〜ですね」と言う。
- 本人が検索の条件を変えたら(例:「時給の下限を外して」)、それを了承する発話と同じ応答で
  propose_jobs を呼び直す。「探します」と言って呼ばないのが一番よくない。

## 会話の途中で調べてほしいと言われたら
- 本人が途中で気になったこと(仕事内容、資格、業界など)を聞かれたり、
  「調べて」と言われたら、「少し調べますね」と言い、同じ応答の中で web_research ツールを呼ぶ。
- 時給の相場(全国や東京の平均時給など)を知りたがったら wage_average ツールで答える。
  希望の時給を聞くときに、相場が分からず迷っていそうなら「相場もお伝えできますよ」と一言添えてよい。
- 気になる会社・お店のことを聞かれたら research_company ツールで調べる。同じ名前の別の会社と
  混ざらないよう、分かっている手がかり(場所や業種)を添える。特定できなかったら、そのまま伝える。
- 答えは2〜3文で、ネットの情報であることが分かるように伝える。調べた内容を本人の経験や希望として扱わない。
- 求人そのもの(どんな求人があるか)は web で探さず、propose_jobs で出す。
- 答えたら、中断した段階に戻って会話を続ける(質問の数には数えない)。

## やらないこと
- STAR のような深掘りの型を使わない。
- 適性や好みを先回りして聞かない(「〜は合いそうですか?」「〜はできそうですか?」)。
  本人の答えを誘導してしまう。確かめてよいのは、本人が言った事実だけ。
- 「大切にしたいこと」「周りからどう言われるか」「なりたい自分」のような、
  バイト探しに直結しない質問はしない。
- 相手の答えに解釈や評価を足さない(例:「〜が育まれましたね」はダメ)。短い相づちと事実の確認だけ。
- 自己PRは聞き出さない。下書きの段階で、聞いた事実から作られる。

## 守ること(誠実性)
- 声や話し方から性格・感情を推測しない。診断・評価もしない。
- 相手が言っていないことを補ったり、盛ったりしない。
- 本人が決めていないこと(職種など)を、経験からの推測で決めて検索やまとめに入れない。
- 氏名・住所・電話番号・メールアドレス・生年月日は聞かない。
  相手が言ってしまったら「その情報は記録されないので大丈夫ですよ」と伝えて先へ進む。
- 経験が少なくても否定せず、話してくれたことをそのまま価値として扱う。

## 終わり方
求人を提案して本人の反応を聞いたら(または相手が「終わりたい」と言ったら)、
finalize_interview ツールを呼んで下書きを保存し、
「画面に履歴書の下書きと求人が出ています。内容は必ずご自身で確認・修正してくださいね」
と案内して短くお礼を述べて締めます。
"""

# dip に保存できる希望の項目と呼び名(dip_mcp/logic.py の PREFERENCE_KEYS と同期。テストで確認)
# experience(これまで何をしてきたか)/ skills(本人が「できる」と言った作業)/
# certifications(持っている資格・免許)は分けて保存する(task 41)
PREFERENCE_LABELS = {
    "area": "エリア・駅", "time_band": "時間帯", "job_type": "職種",
    "min_wage": "時給の下限(円)", "experience": "経験(未経験など)", "days_per_week": "週の日数",
    "skills": "できること", "certifications": "資格・免許",
}


def _stated_value(v) -> str:
    """dip の stated_preferences の値({"value": ..., "saved": ...} か生の値)を文字列にする。"""
    if isinstance(v, dict):
        return str(v.get("value", ""))
    return str(v)


def stated_preferences_note(stated: dict | None) -> str:
    """前に本人が話した希望(dip の要約の stated_preferences)を、指示への追記にする。

    LINE 側のプロンプトと同じ決まり: 確認形で1回だけ使い、同じことを聞き直さない。
    希望が無ければ空文字(追記しない)。
    """
    items = [
        f"- {PREFERENCE_LABELS.get(k, k)}: {_stated_value(v)}"
        for k, v in (stated or {}).items()
        if _stated_value(v)
    ]
    if not items:
        return ""
    return (
        "\n## 前に本人が話した希望(dip に保存済み)\n"
        + "\n".join(items)
        + "\n"
        "- これは前の会話(LINE や音声)で本人が話した希望。確認形で1回だけ触れ"
        "(例:「前回は◯◯とのことでしたが、今回も同じですか?」)、同じことを聞き直さない。\n"
        "- 「同じ」と言われた項目は、そのまま検索条件に使う(改めて聞かない)。"
        "変わったと言われたら、言い直された内容だけを使う。\n"
    )


# 指示に入れる履歴書の要点の上限(文字数)。音声の指示が長くなりすぎないよう、1行ずつ足して超える行は入れない
RESUME_NOTE_MAX_CHARS = 600
RESUME_MAX_WORKS = 3  # 職歴は新しい順にこの件数まで
RESUME_MAX_DUTIES = 3  # 職歴1件あたりの、やっていたこと・任されたことの件数


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


def _resume_facts(resume: dict) -> list[str]:
    """dip の要約の uploaded_resume から、面談に使う要点を大事な順に1行ずつ拾う。

    決まった項目だけを読む(personal や未知の項目は読まない = 氏名・連絡先は指示に入らない)。
    短くて大事な項目(資格・できること・読み取れなかった点・学歴・職務要約・希望)を先に、
    職歴は新しい順に RESUME_MAX_WORKS 件まで。学歴は在学中か最終学歴の1行だけ。自己PR は入れない。
    """
    lines = []
    if resume.get("certifications"):
        certs = [c.get("name", "") + (f"({c['acquired']})" if c.get("acquired") else "")
                 for c in resume["certifications"]]
        lines.append(f"- 資格・免許: {_join(certs, '、')}")
    if resume.get("skills"):
        lines.append(f"- できること: {_join(resume['skills'], '、')}")
    if resume.get("unconfirmed"):
        lines.append(f"- 読み取れなかった点: {_join(resume['unconfirmed'], '、')}")
    edu = _latest_education(resume)
    if edu:
        head = _join([edu.get("status"), edu.get("period")], "、")
        lines.append(f"- 学歴: {edu['school']}" + (f"({head})" if head else ""))
    if resume.get("job_summary"):
        lines.append(f"- 職務要約: {resume['job_summary']}")
    prefs = resume.get("preferences") or {}
    wishes = _join([*prefs.get("desired_work", []), *prefs.get("conditions", []), *prefs.get("notes", [])], "、")
    if wishes:
        lines.append(f"- 履歴書に書いてある希望: {wishes}")
    works = _newest_first(resume.get("work_history") or [])
    for w in works[:RESUME_MAX_WORKS]:
        head = _join([w.get("employment_type"), w.get("role"), w.get("period")], "、")
        line = f"- 職歴: {w.get('organization', '')}" + (f"({head})" if head else "")
        if w.get("duties"):
            line += f" やっていたこと: {_join(w['duties'][:RESUME_MAX_DUTIES])}"
        if w.get("achievements"):
            line += f" 任されたこと: {_join(w['achievements'][:RESUME_MAX_DUTIES])}"
        lines.append(line)
    if len(works) > RESUME_MAX_WORKS:
        lines.append(f"- ほかに古い職歴が {len(works) - RESUME_MAX_WORKS} 件")
    for o in resume.get("other_experience") or []:
        lines.append(f"- {o.get('kind', 'その他の経験')}: {o.get('title', '')}")
    return lines


def _fit(lines: list[str], limit: int) -> list[str]:
    """行の途中では切らず、1行ずつ足して、上限を超える行から先は入れない(入らなかった件数を最後に書く)。"""
    kept, used = [], 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    if len(kept) < len(lines):
        kept.append(f"- ほか {len(lines) - len(kept)} 件は省略")
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
    return (
        f"\n## アップロードした履歴書({resume.get('uploaded', '')} にトークでアップロード。読み取り結果の要点)\n"
        + facts
        + "\n"
        "- これは本人がアップロードした履歴書に書いてあった事実。ここにある経験・資格・できることは聞き直さない。"
        "確認形で使い(例:「履歴書ではコンビニで2年半レジと品出しをされていたんですね」)、次の1問や求人の提案につなげる。\n"
        "- 「読み取れなかった点」は、必要なときだけ1問で確かめる。\n"
        "- 在学中なら、授業のある時間帯は働けない前提で、働ける時間帯を確かめる。\n"
        "- 履歴書の希望と、本人が会話で話した希望が食い違うときは、会話で話した希望を優先する。\n"
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


def greeting_instructions(stated: dict | None = None, resume: dict | None = None) -> str:
    """会話の最初の発話への指示。前に話した希望やアップロードした履歴書があれば、聞き直さずに確認形で始める。

    両方あるときは、履歴書には確認形で1回触れるだけにして、最初の1問は前に話した希望の確認にする
    (希望は検索条件に直結し、履歴書の経験は会話の中で使えるため)。
    履歴書だけなら、職歴があれば「経験に近い仕事で探すか」、無ければ(学歴だけなど)従来どおり職種を聞く。
    """
    base = (
        "まず一言で挨拶し、バイト探しの希望と経験を5分ほどで聞くこと、"
        "終わると求人の提案と履歴書の下書きが出ることを短く伝えてください。"
    )
    has_resume = bool(uploaded_resume_note(resume))
    if has_resume:
        base += (
            "続けて、アップロードされた履歴書を見たことに確認形で1回だけ触れてください"
            f"(例:「{_resume_example(resume)}」)。履歴書にあることは聞き直さないでください。"
        )
    items = [
        f"{PREFERENCE_LABELS.get(k, k)}は{_stated_value(v)}"
        for k, v in (stated or {}).items()
        if _stated_value(v)
    ]
    if items:
        return base + (
            "そのあと最初の質問として、前に話した希望("
            + "、".join(items)
            + ")を確認形で1回だけ確かめてください(例:「前回は◯◯とのことでしたが、"
            "今回も同じですか?」)。同じことを聞き直さないでください。"
        )
    if has_resume and (resume or {}).get("work_history"):
        return base + (
            "そのあと最初の質問として、履歴書の経験に近い仕事で探すか、別の仕事を探しているかを1問だけ聞いてください。"
        )
    return base + "そのあと最初の質問として、どんなバイトを探しているか(職種)を聞いてください。"


DRAFTER_SYSTEM = """\
あなたはキャリアアドバイザーとの面談の文字起こしから、履歴書の下書きを作るアシスタントです。
厳守すること:
- 本人が会話の中で言ったことと、会話で確認できた事実だけを書く。盛らない・作らない・補わない。
- 声色・話し方・性格・感情・思考傾向の推定は一切しない。
- 氏名・住所・電話番号・メールアドレス・生年月日などの個人情報は、会話に出てきても書かない。
- 期間や社名など、会話で曖昧だったことは推測で埋めず、unconfirmed に「本人に確認したい点」として挙げる。
- アルバイト・パート経験や、学校・部活・家事・ボランティアの経験も、立派な職務・経験として扱う。
- 本人の希望(preferences)には、本人が希望として言ったことだけを書く。求人検索のために
  一時的に外した・ゆるめた条件(例:「一旦、時給の下限を外して」)は本人の希望ではないので、
  本人が言った希望(例: 時給1500円くらい)のまま書く。検索の経緯(何で検索し直したか)は書かない。
- 職務要約(job_summary)は2〜3文・事実のみで書く。
- 自己PR(self_pr)も事実のみ(3〜4文まで)。職務要約の繰り返し・言い換えにはしない。
  自己PRの材料(工夫したこと・任されたこと・成果・続けてきたことなど)が会話に出ていないときは、
  無理に書かずに空文字にして、unconfirmed に
  「自己PRの材料(工夫したこと・任されたことなど)を本人に確認したい」を挙げる。
"""


def drafter_user(transcript_text: str) -> str:
    """文字起こしテキストから、下書き生成のユーザープロンプトを組み立てる。"""
    return (
        "以下はキャリアアドバイザー(assistant)と本人(user)の面談の文字起こしです。\n"
        "本人が言った事実だけを使って、履歴書の下書きを作ってください。\n"
        "会話に出てこなかった項目は空(空文字・空配列)のままにしてください。\n\n"
        f"## 文字起こし\n{transcript_text}\n"
    )
