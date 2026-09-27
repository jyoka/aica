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
from dip_mcp.data import FIXTURES, State

PROMPTS = Path(__file__).resolve().parent / "prompts"

mcp = FastMCP("baitoru-mcp", instructions="バイトルの求人・ユーザー状況・応募手続き・会話結果の記録を扱う dip のツール群。")
state = State()

UserId = Annotated[str, Field(description="LINE のユーザーID")]
JobType = Annotated[str | None, Field(description=(
    f"職種。次の中から1つをそのまま選ぶ: {' / '.join(logic.JOB_TYPES)}。"
    "ユーザーの言葉(例: 飲食・カフェ)が複数に当たるなら、1つずつ検索する。一覧に無い値はエラーになる"))]
Area = Annotated[str | None, Field(description=(
    f"エリアか駅名。地名は次の中から選ぶ: {' / '.join(logic.AREAS)}(駅名も可。例: 中野駅)。"
    f"「{'」「'.join(logic.AREA_GROUPS)}」のような広い言い方は、そのまま渡すとその範囲の地名に広げて検索される。"
    "一覧に無い地名はエラーになる。ユーザーがエリアを決めていないなら指定しない"))]


@mcp.tool()
def get_user_conversation_summary(line_user_id: UserId) -> dict:
    """ユーザーの状況要約(直近の応募・フォーム離脱・閲覧傾向・直近の接触・配信抑制)を返す。会話の最初に呼ぶ。
    要約は仮説であり、生の行動ログは含まない。"""
    return logic.conversation_summary(state, line_user_id)


@mcp.tool()
def get_user_recent_applications(line_user_id: UserId) -> list[dict]:
    """90日以内の応募と、その選考状況。"""
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
    min_wage: Annotated[int | None, Field(description="時給の下限(円)")] = None,
    keywords: Annotated[str | None, Field(description=(
        "その他の希望を本人の言葉のまま(例: 日払い 未経験OK、まかない付き、人と話さず黙々と)。"
        "語の一致に加えて意味の近さでも探すので、求人の文言に言い換えて寄せない。複数の希望は空白区切り。"
        "エリア・時間帯・職種・時給は keywords に入れず、それぞれの引数で渡す"))] = None,
    exclude_ids: Annotated[list[str] | None, Field(description="出さない求人ID(すでに見せたもの)")] = None,
    posted_within_days: Annotated[int | None, Field(description="この日数以内に掲載された新着だけにする")] = None,
) -> dict:
    """条件に合う求人を最大3件、事実に基づく理由つきで返す。応募済みの求人は出さない。total は条件に合う総数。
    各求人の match_score は本人の傾向との合い具合(内訳 score_breakdown)。高い順に並ぶ。
    keywords は意味検索(embedding)で言い換えも拾い、意味が近い求人ほど上に来る(内訳の keywords)。"""
    return logic.recommend(state, line_user_id, area, time_band, job_type, min_wage, keywords, exclude_ids,
                           posted_within_days=posted_within_days)


@mcp.tool()
def relax_conditions(
    line_user_id: UserId,
    area: Area = None, time_band: str | None = None, job_type: JobType = None,
    min_wage: int | None = None, keywords: str | None = None,
) -> dict:
    """条件に合う求人が少ないとき、どの条件を外すと何件に増えるかを返す(提案の材料)。"""
    return logic.relax(state, line_user_id, area, time_band, job_type, min_wage, keywords)


@mcp.tool()
def answer_job_question(
    question: Annotated[str, Field(description="ユーザーの質問(本人の言葉のまま。求人名を足したり言い換えたりしない)")],
    job_id: Annotated[str | None, Field(description="特定の求人についての質問なら job_id")] = None,
) -> dict:
    """質問に答える材料を出典つきで返す。求人についての質問なら job_id を必ず渡す(その求人の FAQ・求人の説明を優先して探す)。
    hits の source は 案件の FAQ / 求人の説明 / 全体の FAQ。job_id を渡したときは job(求人の説明・時給・勤務時間・待遇)も
    答えの材料にする(hits は言葉の重なりで探すので、仕事内容のような質問では外れることがある)。どちらにも無ければ推測で答えない。"""
    return logic.answer_material(question, job_id)


@mcp.tool()
def get_application_status(line_user_id: UserId, job_id: str | None = None) -> dict:
    """応募の選考状況と、連絡までの一般的な目安。"""
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
        description="本人が話した希望を本人の言葉のまま。項目: area(エリア・駅)/ time_band(時間帯)/ job_type(職種)/ "
                    "min_wage(時給の下限の数字)/ experience(経験)/ days_per_week(週の日数)/ "
                    "skills(できること)/ certifications(資格・免許)。例: {\"time_band\": \"夜\", \"area\": \"新宿\"}。"
                    "experience はこれまで何をしてきたか(職歴・未経験など)、skills は本人が「できる」と言った作業、"
                    "certifications は持っている資格・免許。同じ発言を複数の項目に重複して入れない")],
) -> dict:
    """本人が会話で話した希望を保存する。次の会話では get_user_conversation_summary の stated_preferences に入る。
    本人が言っていないこと(推測・行動履歴からの補完)や、氏名・住所・連絡先は保存しない
    (電話番号・メールアドレス・住所らしい値はツールでも弾く)。"""
    return logic.save_preferences(state, line_user_id, preferences)


@mcp.tool()
def record_conversation_outcome(
    line_user_id: UserId,
    outcome: Annotated[str, Field(description=(
        "propose / consult / support / wait / stop。wait / stop は配信を止める(ユーザーに見えない副作用)ので、"
        "ユーザーが断ったとき(「今はいい」「やめた」「まだ考え中」「もう送らないで」など)だけ使う。"
        "選択肢を選んで話を続けている・聞きたいことがまだ言葉になっていない・終わりの挨拶のときは使わない"))],
    reason: Annotated[str, Field(description="その結果になった理由(ユーザーの言葉を短く)。wait / stop のときは、ユーザーが断った言葉をそのまま")],
    confidence: Annotated[float, Field(description="確からしさ 0〜1")],
    valid_days: Annotated[int, Field(description="この結果を有効とする日数(wait・stop は配信を抑制する日数)")],
) -> dict:
    """会話の結果を記録する。wait / stop は配信抑制に反映される。ユーザーが断っていないのに wait / stop を記録してはいけない。"""
    return logic.record_outcome(state, line_user_id, outcome, reason, confidence, valid_days)


@mcp.resource("baitoru://faq", mime_type="application/json")
def faq() -> str:
    """よくある質問(応募・面接・シフト・給与・配信停止など)。"""
    return (FIXTURES / "faq.json").read_text(encoding="utf-8")


@mcp.prompt()
def baitoru_assistant(entry: str = "A") -> str:
    """dip が提供する対話シナリオ。entry は入口(A: トーク / C: 配信への返信 / follow: 友だち追加 /
    D: フォーム離脱・app_status: 選考状況の変化・saved_closing: 保存求人の掲載終了間近・new_match: 新着 のプッシュ)。"""
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
