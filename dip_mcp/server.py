"""dip 側 MCP サーバー(baitoru-mcp のシミュレーション)。Tool / Resource / Prompt を公開する。

LINE 側(LY Runtime のスタンドイン)はこのサーバーにだけ依存する。dip が会話の質に影響できるのは、
ここの Prompt(シナリオ)と、Tool/Resource が返す中身だけ、という本番の境界を再現する。

    ./.venv/bin/python -m dip_mcp.server               # stdio(ランタイムが子プロセスとして起動)
    ./.venv/bin/python -m dip_mcp.server --http 8020   # HTTP(streamable-http, /mcp)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from dip_mcp import logic
from dip_mcp.baito import session
from dip_mcp.career import harness as H
from dip_mcp.career import session as career
from dip_mcp.career import store as career_store
from dip_mcp.data import FIXTURES, PHYSICAL_LEVELS, TALK_LEVELS, State

PROMPTS = Path(__file__).resolve().parent / "prompts"

mcp = FastMCP("baitoru-mcp", instructions="バイトルの求人・ユーザー状況・応募手続き・会話結果の記録を扱う dip のツール群。")
state = State()

UserId = Annotated[str, Field(description="LINE のユーザーID")]
# 音声相談の部屋(引数名は logic.VOICE_ROOM_ARG)。書き込むツールに音声ワーカーだけが渡す
# (「すべて消す」の前に始めた部屋の書き込みを断る。logic._room_forgotten)。トークの AI には見せない(line_runtime.runtime が外す)
VoiceRoom = Annotated[str | None, Field(description="音声相談の部屋の名前(音声ワーカーだけが渡す)")]
JobType = Annotated[str | None, Field(description=(
    "職種。雇用区分ごとの一覧から1つをそのまま選ぶ。"
    f"アルバイト・パート: {' / '.join(logic.JOB_TYPES)}。正社員: {' / '.join(logic.FULLTIME_JOB_TYPES)}。"
    "employment_type を渡したときは、その区分の一覧から選ぶ。"
    "ユーザーの言葉(例: 飲食・カフェ)が複数に当たるなら、1つずつ検索する。一覧に無い値はエラーになる"))]
EmploymentType = Annotated[str | None, Field(description=(
    f"求人の雇用区分: {' / '.join(logic.EMPLOYMENT_TYPES)}。get_user_conversation_summary の search_track"
    "(今の雇用区分)をそのまま渡す。search_track が未定なら指定しない(両方の区分から探す)"))]
MinAnnualSalary = Annotated[int | None, Field(description=(
    "希望年収の下限(万円。例: 年収500万円以上なら 500)。本人が言った額のまま。正社員の求人(年収・月給)だけが対象で、"
    "求人の年収の幅の中央値(月給は12か月分)がこの額に届くものを出し、給与の高い順に寄せて並べる。時給の求人は出なくなる"))]
_TALK = " / ".join(f"{k} = {v}" for k, v in TALK_LEVELS.items())
_PHYSICAL = " / ".join(f"{k} = {v}" for k, v in PHYSICAL_LEVELS.items())
MaxTalkLevel = Annotated[int | None, Field(description=(
    f"人と話す量の上限(1〜3)。{_TALK}。本人が避けたいことから決める: 「人と話すのが苦手」「黙々と作業したい」なら 1、"
    "「接客はしたくない」なら 2。電話だけを避けたいときは付けない(電話は接客と同じ 3 に入るので、2 にすると店頭の求人もほとんど"
    "消える)。接客も避けたいかを1問で確かめ、避けたいときだけ 2 にする。両方の区分の求人に効く"))]
MinTalkLevel = Annotated[int | None, Field(description=(
    "人と話す量の下限(1〜3。値の意味は max_talk_level と同じ)。「人と話す仕事がしたい」「接客が好き」なら 3"))]
MaxPhysicalLevel = Annotated[int | None, Field(description=(
    f"体を使う量の上限(1〜3)。{_PHYSICAL}。「立ち仕事は避けたい」「座ってできる仕事」なら 1、"
    "「力仕事は避けたい」「重いものは持てない」なら 2。両方の区分の求人に効く"))]
MinPhysicalLevel = Annotated[int | None, Field(description=(
    "体を使う量の下限(1〜3。値の意味は max_physical_level と同じ)。「体を動かしたい」なら 3"))]
Remote = Annotated[str | None, Field(description=(
    "リモートワークの希望(正社員の求人だけが対象。指定するとアルバイト・パートの求人は出ない)。"
    "可 = 原則在宅の求人だけ(「在宅で働きたい」「フルリモート」)、一部可 = 週に何日か以上在宅できる求人(可も含む。"
    "「リモートもできたら」「週に何日か在宅」)。希望が無ければ指定しない"))]
WeekendsHolidays = Annotated[bool | None, Field(description=(
    "true なら土日祝が休みの求人だけ(「土日祝休み」「土日は休みたい」)。正社員の求人だけが対象"))]
MaxOvertimeHours = Annotated[int | None, Field(description=(
    "月の残業時間の上限(正社員の求人の残業の目安と比べる)。本人が時間を言ったらその数、"
    "「残業少なめ」「残業はほとんど無い方がいい」だけなら 10。正社員の求人だけが対象"))]
Area = Annotated[str | None, Field(description=(
    f"エリアか駅名。地名は次の中から選ぶ: {' / '.join(logic.AREAS)}(駅名も可。例: 中野駅)。"
    f"「{'」「'.join(logic.AREA_GROUPS)}」のような広い言い方は、そのまま渡すとその範囲の地名に広げて検索される。"
    "一覧に無い地名はエラーになる。ユーザーがエリアを決めていないなら指定しない"))]


@mcp.tool()
def get_user_conversation_summary(line_user_id: UserId) -> dict:
    """ユーザーの状況要約(今の雇用区分・直近の応募・フォーム離脱・閲覧傾向・直近の接触・本人が話した希望と
    相談で分かったこと(stated_preferences)・前の会話の相談のまとめ(consultation_summary)・
    アップロードした履歴書の読み取り結果・条件の問いの選択肢の候補(condition_options))を返す。チャットと音声は同じ要約を読む。会話の最初と、本人が履歴書をアップロードした後に呼ぶ。
    要約は仮説であり、生の行動ログは含まない。as_of は行動の事実(応募・離脱・閲覧・保存・接触)をいつ時点の要約から
    読んだか(その後の行動は入っていないことがある。この会話で保存・応募したことは入っている)。"""
    return logic.conversation_summary(state, line_user_id)


@mcp.tool()
def get_user_recent_applications(line_user_id: UserId) -> list[dict]:
    """90日以内の応募と、その選考状況(新しい順に5件まで)。ほかにもあれば、最後の1件が more(ほかの件数)と note になる。"""
    return logic.recent_applications(state, line_user_id)


@mcp.tool()
def get_user_form_exit(line_user_id: UserId) -> dict:
    """途中で止まった応募フォーム(7日以内)。入力済み・残りの項目、トークで受け取れる項目と LIFF が必要な項目。"""
    return logic.form_exit(state, line_user_id) or {"form_exit": None}


@mcp.tool()
def recommend_jobs(
    line_user_id: Annotated[str | None, Field(description="LINE のユーザーID(本人の傾向を反映するため)")] = None,
    area: Area = None,
    time_band: Annotated[str | None, Field(description="早朝 / 日中 / 夕方 / 夜 / 深夜")] = None,
    job_type: JobType = None,
    min_wage: Annotated[int | None, Field(description="時給の下限(円)。時給の求人(アルバイト・パート)だけが対象")] = None,
    employment_type: EmploymentType = None,
    min_annual_salary: MinAnnualSalary = None,
    max_talk_level: MaxTalkLevel = None,
    min_talk_level: MinTalkLevel = None,
    max_physical_level: MaxPhysicalLevel = None,
    min_physical_level: MinPhysicalLevel = None,
    remote: Remote = None,
    weekends_holidays: WeekendsHolidays = None,
    max_overtime_hours: MaxOvertimeHours = None,
    keywords: Annotated[str | None, Field(description=(
        "その他の希望を本人の言葉のまま(例: 日払い 未経験OK、まかない付き、コーヒーが好き)。"
        "語の一致に加えて意味の近さでも探すので、求人の文言に言い換えて寄せない。複数の希望は空白区切り。"
        "エリア・時間帯・職種・時給・年収・雇用区分・人と話す量・体を使う量・リモート・土日祝休み・残業は keywords に入れず、"
        "それぞれの引数で渡す。避けたいこと(「〜したくない」「〜は苦手」)は keywords に入れない"
        "(意味の近さで探すので、避けたい仕事の方が上に出てしまう)"))] = None,
    exclude_ids: Annotated[list[str] | None, Field(description="出さない求人ID(すでに見せたもの)")] = None,
    posted_within_days: Annotated[int | None, Field(description="この日数以内に掲載された新着だけにする")] = None,
) -> dict:
    """条件に合う求人を最大3件、事実に基づく理由つきで返す。応募済みの求人は出さない。total は条件に合う総数。
    アルバイト・パートと正社員の求人を同じ形で扱う。雇用区分は employment_type で絞る(summary の search_track を渡す)。
    区分が未定なら employment_type を指定せず、両方の区分から出してよい(各求人の employment_type で見分けられる)。
    正社員の希望年収は min_annual_salary(万円)、アルバイト・パートの希望時給は min_wage(円)で絞る。
    時間帯(time_band)はアルバイト・パートの条件で、指定すると正社員の求人は出ない。
    給与は各求人の pay(kind が 時給 / 月給 / 年収、金額は円)と pay_text(表示用)。正社員の求人には
    働き方(work_style: リモート・フレックス・残業の目安)と休日(holidays: 年間休日・土日祝休みか)も付く。
    各求人の match_score は本人の傾向との合い具合(内訳 score_breakdown)。高い順に並ぶ。
    keywords は意味検索(embedding)で言い換えも拾い、意味が近い求人ほど上に来る(内訳の keywords)。
    意味の近い求人が無い希望(求人の情報に無いこと)は、語が一致した求人だけになり0件になりやすい(0件なら keywords_note に次の手がある)。
    本人が保存したやりたいこと・経験(save_user_preferences)と keywords の両方に、求人の職種の語(カフェ、ホール、営業など)があれば、
    その求人の reasons に決まった形の1文が入る(「カフェのご経験を活かせます」)。避けたいこと・不安にある語からは作らない。
    人と話す量・体を使う量(max_talk_level など)は両方の区分の求人に、リモート・土日祝休み・残業は正社員の求人だけに効く。"""
    if blocked := session.job_block(state, line_user_id):  # バイトの相談で中学生以下と分かった人には出さない
        return blocked
    return logic.recommend(state, line_user_id, area, time_band, job_type, min_wage, keywords, exclude_ids,
                           posted_within_days=posted_within_days, employment_type=employment_type,
                           min_annual_salary=min_annual_salary, max_talk_level=max_talk_level,
                           min_talk_level=min_talk_level, max_physical_level=max_physical_level,
                           min_physical_level=min_physical_level, remote=remote,
                           weekends_holidays=weekends_holidays, max_overtime_hours=max_overtime_hours)


@mcp.tool()
def relax_conditions(
    line_user_id: UserId,
    area: Area = None, time_band: str | None = None, job_type: JobType = None,
    min_wage: int | None = None, keywords: str | None = None,
    employment_type: EmploymentType = None, min_annual_salary: MinAnnualSalary = None,
    max_talk_level: MaxTalkLevel = None, min_talk_level: MinTalkLevel = None,
    max_physical_level: MaxPhysicalLevel = None, min_physical_level: MinPhysicalLevel = None,
    remote: Remote = None, weekends_holidays: WeekendsHolidays = None, max_overtime_hours: MaxOvertimeHours = None,
    exclude_ids: Annotated[list[str] | None, Field(description="出さない求人ID(recommend_jobs に渡したもの)")] = None,
) -> dict:
    """条件に合う求人が少ないとき、どの条件を外すと何件に増えるかを返す(提案の材料)。雇用区分は外す候補にしない。
    recommend_jobs に渡したのと同じ条件を渡す(options[].remove は外す引数の名前)。exclude_ids の求人はどの件数にも数えない。"""
    if blocked := session.job_block(state, line_user_id):
        return blocked
    return logic.relax(state, line_user_id, area, time_band, job_type, min_wage, keywords,
                       employment_type=employment_type, min_annual_salary=min_annual_salary, exclude_ids=exclude_ids,
                       max_talk_level=max_talk_level, min_talk_level=min_talk_level,
                       max_physical_level=max_physical_level, min_physical_level=min_physical_level,
                       remote=remote, weekends_holidays=weekends_holidays, max_overtime_hours=max_overtime_hours)


@mcp.tool()
def answer_job_question(
    question: Annotated[str, Field(description="ユーザーの質問(本人の言葉のまま。求人名を足したり言い換えたりしない)")],
    job_id: Annotated[str | None, Field(description="特定の求人についての質問なら job_id")] = None,
) -> dict:
    """質問に答える材料を出典つきで返す。求人についての質問なら job_id を必ず渡す(その求人の FAQ・求人の説明を優先して探す)。
    hits の source は 案件の FAQ / 求人の説明 / 全体の FAQ。job_id を渡したときは job(求人の説明・給与 pay・勤務時間・待遇、
    正社員なら必須・歓迎の経験・働き方・休日・試用期間)も
    答えの材料にする(hits は言葉の重なりで探すので、仕事内容のような質問では外れることがある)。どちらにも無ければ推測で答えない。"""
    return logic.answer_material(question, job_id)


@mcp.tool()
def get_application_status(line_user_id: UserId, job_id: str | None = None) -> dict:
    """応募の選考状況と、連絡までの一般的な目安。as_of は選考状況をいつ時点の要約から読んだか。
    job_id を渡すと、90日以内のその求人の応募を返す。渡さなければ新しい順に5件までで、ほかにもあれば more(ほかの件数)が付く。"""
    return logic.application_status(state, line_user_id, job_id)


@mcp.tool()
def resume_application_form(
    line_user_id: UserId,
    job_id: str,
    answers: Annotated[dict[str, str], Field(description="トークで答えてもらった項目(例: {\"希望開始日\": \"来週から\"})")],
) -> dict:
    """途中の応募フォームに、トークで答えてもらった項目を反映する。個人情報の項目は受け付けず、
    残りの項目を入力する画面(LIFF)の URL を返す。"""
    return logic.resume_form(state, line_user_id, job_id, answers)


@mcp.tool()
def save_job(line_user_id: UserId, job_id: str) -> dict:
    """求人を「保存」する。"""
    return logic.save_job(state, line_user_id, job_id)


@mcp.tool()
def save_user_preferences(
    line_user_id: UserId,
    preferences: Annotated[dict[str, str | int], Field(
        description="本人が話した希望と、相談で分かった本人のことを、本人の言葉のまま短い文で(1項目100字以内)。"
                    "条件: area(エリア・駅)/ time_band(時間帯)/ job_type(職種)/ "
                    "min_wage(時給の下限の数字)/ experience(経験)/ days_per_week(週の日数)/ "
                    "skills(できること)/ certifications(資格・免許)。"
                    "区分に共通: purpose(目的。なぜ今探すのか。例: 経験を活かしたい、生活費の足しに)/ "
                    "strengths(強み。本人が自分で言った得意・評価されたこと)/ wants(やりたいこと)/ "
                    "avoids(避けたいこと)/ concerns(不安・気になっていること)。"
                    "正社員を探すとき: min_annual_salary(希望年収の下限。例: 年収500万円)/ "
                    "work_style(働き方。例: リモート中心、残業少なめ)/ start_timing(働き始める時期。例: 3か月以内)。"
                    "why_now(なぜ今)/ direction(次にやりたいこと)/ team_style(人と働く形)は、正社員のヒアリングの"
                    "ハーネスが保存する項目。相談の中では渡さない。保存した値を本人が言い直したとき"
                    "(例: 「なぜ今は、年収を上げたいから、に直して」)だけ、言い直した言葉で渡して上書きする。"
                    "例: {\"time_band\": \"夜\", \"area\": \"新宿\"}、{\"purpose\": \"経験を活かしたい\"}。"
                    "experience はこれまで何をしてきたか(職歴・未経験など)、skills は本人が「できる」と言った作業、"
                    "certifications は持っている資格・免許、strengths は本人が強みとして話したこと。"
                    "同じ発言を複数の項目に重複して入れない。こちらの解釈・評価・言い換えは入れない")],
    room: VoiceRoom = None,
) -> dict:
    """本人が会話で話した希望と、相談で分かった本人のこと(目的・強み・やりたいこと・避けたいこと・不安、
    正社員の条件)を保存する。チャットと音声で共有され、次の会話では get_user_conversation_summary の
    stated_preferences に入る。本人が言っていないこと(推測・行動履歴や履歴書からの補完)や、氏名・住所・連絡先は
    保存しない(電話番号・メールアドレス・住所らしい値と、100字を超える値はツールでも弾く)。"""
    return logic.save_preferences(state, line_user_id, preferences, room)


@mcp.tool()
def update_career_card(
    line_user_id: UserId,
    item: Annotated[str, Field(description=(
        "直す欄。職歴カード: career<n>.<欄>。n は要約の career_profile の career_cards の各カードの n(並びの順ではない。"
        "1 が直近)。欄は " + " / ".join(f"{f}({H.CARD_LABELS[f]})" for f in H.CARD_FIELDS)
        + "(role は勤め先・期間・役割、scale は規模、result は成果)。今の状況の答え(career_profile の answers): "
        + " / ".join(f"{s}({H.SLOTS[s]['label']})" for s in career_store.SEGMENT_SLOTS)))],
    text: Annotated[str, Field(description=(
        f"本人が言い直した言葉そのまま({career_store.VALUE_MAX}字以内)。言い換え・要約・推測をしない。"
        "数字は本人が言ったものだけ(こちらで足したり、丸めたりしない)"))],
) -> dict:
    """保存した職歴カード(正社員のヒアリングで集めたもの。要約の career_profile)の欄を、本人がトークで言い直したときに
    直す(上書き)。例: 「職歴1のどうなったかは、誤出荷がゼロになった、です」→ item=career1.result。
    本人が言い直していない欄は直さない。直せるのは、要約の career_profile にもう出ている欄だけ(保存していない欄は作らない)。
    正社員の相談の途中は呼ばない(相談はハーネスが受け持つ)。
    消すときは、今のところ「データを消す」(すべて消す)だけ。電話番号・メールアドレス・住所らしい値と、長すぎる値は弾く。"""
    return career_store.update(state, line_user_id, item, text)


@mcp.tool()
def set_search_track(
    line_user_id: UserId,
    track: Annotated[str, Field(description=(
        f"切り替える先の雇用区分。次の中から1つ: {' / '.join(logic.TRACKS)}。"
        "取り消し(undo)のときは、要約の track_history の最後の from(切り替える前の区分)"))],
    evidence: Annotated[str, Field(description=(
        "切り替えの根拠になった本人の発言そのまま(要約・言い換えをしない)。"
        "履歴書の内容や行動履歴は根拠にしない。取り消しのときは、取り消しを求めた本人の発言"))],
    undo: Annotated[bool, Field(description="直前の切り替えの取り消しなら true")] = False,
    room: VoiceRoom = None,
) -> dict:
    """本人の発話から判断した雇用区分(アルバイト・パート / 正社員 / 未定)に切り替え、履歴(いつ・どの区分へ・根拠・
    取り消しか)を記録する。今の区分は get_user_conversation_summary の search_track に出る(初回は未定)。
    今と同じ区分なら記録しない。弱い・混在した合図では呼ばず、先に1問で確かめる。
    切り替えたときは、切り替えた後の区分の条件の問いの選択肢の候補(condition_options。要約と同じ形)も返す。"""
    return logic.set_search_track(state, line_user_id, track, evidence, undo, room)


@mcp.tool()
def get_counseling_playbook(
    track: Annotated[str, Field(description=(
        f"雇用区分: {' / '.join(logic.TRACKS)}。get_user_conversation_summary の search_track"
        "(set_search_track で切り替えたターンは切り替えた後の区分)をそのまま渡す"))],
    channel: Annotated[str, Field(description=f"チャネル: {' / '.join(logic.CHANNELS)}(トークは chat、音声相談は voice)")] = "chat",
) -> dict:
    """相談の進め方の指針(playbook)を返す: 明らかにすること・掘り下げ方・選択肢の作り方・保存先と、
    その区分の会話の決まり(rules: questions_max = 1回の返信の質問の数、bubble_max = 1つの吹き出しの字数、
    branch_by = ユーザーの何回目の返信までに分岐するか。音声(voice)は questions_max と sentences_max = 1回の発話の文の数)。
    次に何を聞くかは決めない。
    相談に入ったターンに今の区分で呼ぶ。区分が変わったターン(切り替え・取り消し)に新しい区分で呼び直すのは、
    相談の途中か一度 playbook を読んだ後のときと、正社員に切り替えたときだけ。
    相談に入る前(まだ読んでいない)のアルバイト・パートか未定への切り替えでは呼ばない。同じ区分のあいだは呼び直さない。"""
    return logic.counseling_playbook(track, channel)


@mcp.tool()
def start_baito_consultation(line_user_id: UserId) -> dict:
    """アルバイト・パートのトーク(chat)の相談を、ハーネス(バイト探しの相棒)で始める。アルバイト・パートの playbook
    (get_counseling_playbook)が呼ぶように書いているときだけ、playbook を読んだのと同じターンに呼ぶ。
    返った question の text を言い換えずに1問として聞く(選択肢は LINE 側が question の choices にする)。
    2問目から、条件のまとめ(review)と結果(条件カードとおすすめのバイトの種類3つ)までは、ハーネスが本人とやりとりする。
    結果に着くと、確かめた条件は stated_preferences に保存される。進めている相談があれば、今聞いている質問を返す。"""
    return session.start(state, line_user_id)


@mcp.tool()
def baito_turn(
    line_user_id: UserId,
    text: Annotated[str, Field(description="本人の発言")],
    llm_outputs: Annotated[list[dict] | None, Field(description=(
        "status=llm のあと、このターンでモデルが返した出力を順にすべて({\"output\": JSON} か、失敗なら {\"error\": 型の名前})"))] = None,
) -> dict:
    """(LINE 側のランタイムだけが呼ぶ。トークの AI には見せない)ハーネスの相談を本人の発言 1 つで進める。
    相談をしていなければ active=False。dip_mcp.baito.session.turn を参照。"""
    return session.turn(state, line_user_id, text, llm_outputs)


@mcp.tool()
def end_baito_consultation(line_user_id: UserId) -> dict:
    """(LINE 側のランタイムだけが呼ぶ。トークの AI には見せない)ハーネスの相談を途中で閉じる(新しい会話を始めたときなど)。"""
    return session.end(state, line_user_id)


@mcp.tool()
def start_career_consultation(line_user_id: UserId) -> dict:
    """正社員のトーク(chat)の相談を、ハーネス(職務経歴書の材料を集めるヒアリング)で始める。正社員の playbook
    (get_counseling_playbook)が呼ぶように書いているときだけ、playbook を読んだのと同じターンに呼ぶ。
    この返信は、返った question の bubbles をそのまま送る(LINE 側が吹き出しと選択肢をハーネスのものに置き換える)。
    2問目から、まとめ(review)と結果までは、ハーネスが本人とやりとりする。結果に着くか、本人が「あとで」を押すと、
    確かめた条件は stated_preferences に、職歴カードは career_profile に保存される。前の相談で保存した項目は聞かない。
    進めている相談があれば、今聞いている質問を返す。"""
    return career.start(state, line_user_id)


@mcp.tool()
def career_turn(
    line_user_id: UserId,
    text: Annotated[str, Field(description="本人の発言")],
    llm_outputs: Annotated[list[dict] | None, Field(description=(
        "status=llm のあと、このターンでモデルが返した出力を順にすべて({\"output\": JSON} か、失敗なら {\"error\": 型の名前})"))] = None,
) -> dict:
    """(LINE 側のランタイムだけが呼ぶ。トークの AI には見せない)正社員のハーネスの相談を本人の発言 1 つで進める。
    相談をしていなければ active=False。dip_mcp.career.session.turn を参照。"""
    return career.turn(state, line_user_id, text, llm_outputs)


@mcp.tool()
def end_career_consultation(line_user_id: UserId) -> dict:
    """(LINE 側のランタイムだけが呼ぶ。トークの AI には見せない)正社員のハーネスの相談を途中で閉じる(そこまでを保存する)。"""
    return career.end(state, line_user_id)


@mcp.tool()
def record_conversation_outcome(
    line_user_id: UserId,
    outcome: Annotated[str, Field(description=(
        "propose / consult / support / wait。wait は、ユーザーが断ったとき"
        "(「今はいい」「やめた」「もう送らないで」など)だけ使う。"
        "選択肢を選んで話を続けている・聞きたいことがまだ言葉になっていない・終わりの挨拶のときは使わない"))],
    reason: Annotated[str, Field(description="その結果になった理由(ユーザーの言葉を短く)。wait のときは、ユーザーが断った言葉をそのまま")],
    confidence: Annotated[float, Field(description="確からしさ 0〜1")],
    summary: Annotated[str | None, Field(description=(
        "相談のまとめ(200字以内)。次の会話(チャットでも音声でも)が続きから始めるための記録。"
        "書くのは、本人の発言に基づく事実(目的・希望・経験・不安など)と、次に確かめることだけ。"
        "こちらの解釈・評価・推測・性格の見立て、本人が言っていない希望、氏名・住所・連絡先は書かない。"
        "例: 経験を活かせる仕事を探している。法人営業を5年。残業の少なさを気にしている。"
        "次に確かめること: 希望の勤務地と、正社員かアルバイトか"))] = None,
    room: VoiceRoom = None,
) -> dict:
    """会話の結果を記録する。ユーザーが断っていないのに wait を記録してはいけない。
    相談で話を聞いたときは、summary に相談のまとめ(200字以内。本人の発言に基づく事実と次に確かめることだけ)を残す。
    まとめは get_user_conversation_summary の consultation_summary に出て、チャットと音声で引き継がれる。
    200字を超えると記録されない(短く書き直して呼び直す)。"""
    return logic.record_outcome(state, line_user_id, outcome, reason, confidence, summary, room)


@mcp.resource("baitoru://faq", mime_type="application/json")
def faq() -> str:
    """よくある質問(応募・面接・シフト・給与など)。"""
    return (FIXTURES / "faq.json").read_text(encoding="utf-8")


@mcp.prompt()
def baitoru_assistant(entry: str = "A") -> str:
    """dip が提供する対話シナリオ。entry は入口(A: トーク / visit: 初回訪問 / revisit: 再訪 /
    D: フォーム離脱のプッシュ)。"""
    base = (PROMPTS / "baitoru_assistant.md").read_text(encoding="utf-8")
    entries = json.loads((PROMPTS / "entries.json").read_text(encoding="utf-8"))
    return f"{base}\n## この会話の入口\n{entries.get(entry, entries['A'])}\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, help="HTTP で起動するポート(省略時は stdio)")
    args = ap.parse_args()
    if args.http:
        mcp.settings.port = args.http
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
