"""音声アドバイザー本体(LiveKit Agent)。

会話の脳は OpenAI(gpt-6-sol)。指示は、区分によらない音声の決まりに、今の雇用区分の playbook(dip の
get_counseling_playbook(track, "voice"))を足したもの(prompts.advisor_instructions)。ツール:
- set_search_track: 本人の発話から雇用区分を切り替えて dip に記録し、指示の playbook をその区分のものに入れ替える
- save_user_preferences: 本人が話した希望と自分のこと(目的・強み・正社員の条件など)を、その場で dip に保存する
- propose_jobs: 聞いた希望条件から dip の recommend_jobs(MCP)で今の区分の求人を最大3件出す。
  0件なら dip の relax_conditions(読み取りだけ)で「どの条件を外すと何件になるか」を調べ、
  本人に選んでもらう選択肢として返す(当てずっぽうの再検索をさせない)
- web_research / wage_average / research_company: 会話中に本人が気になったこと(仕事内容・資格など)、
  平均時給(全国と東京は参考データ data/wage_reference.json からすぐ答える)、気になる会社を web で調べて短く答える
- finalize_interview: 文字起こしから下書き(正社員なら職務経歴書、それ以外は履歴書)を作り、希望条件・提案した求人IDと
  一緒に out/ へ保存し、相談のまとめを dip に記録してトークへ結果を返す

ツールのロジックはオフラインでテストする(dip と drafter の client を差し替えられる)。
"""

from __future__ import annotations

import json
import logging
import os
import re
from contextlib import asynccontextmanager

from livekit.agents import Agent, function_tool

from voice_resume import config
from voice_resume.jobs import (
    DEFAULT_TRACK,
    MAX_PROPOSALS,
    TRACKS,
    build_recommend_args,
    on_track,
    relax_option_line,
    split_relaxed,
    spoken_job_line,
)
from voice_resume.outputs import history_transcript, write_outputs
from voice_resume.prompts import (
    CONSULTATION_SUMMARY_MAX_CHARS,
    PREFERENCE_LABELS,
    advisor_instructions,
    consultation_summary_note,
    document_kind_for,
    document_name,
    stated_preferences_note,
    uploaded_resume_note,
)
from voice_resume.wages import WageReference, answer_from_reference, load_reference

logger = logging.getLogger("voice-resume.advisor")

DATA_TOPIC = "voice-resume"


class TalkForgotten(Exception):
    """トークがこの面談の結果を受け取らなかった(面談の途中で本人が「すべて消す」をした)。"""


class ResumeAdvisor(Agent):
    """仕事探しの希望を聞き、求人を提案し、履歴書か職務経歴書の下書きを残すアドバイザー。"""

    def __init__(
        self, *, dip=None, draft_client_factory=None, research_client_factory=None, room=None,
        wage_reference: WageReference | None = None, user_id: str | None = None,
        stated_preferences: dict | None = None, uploaded_resume: dict | None = None, talk_poster=None,
        consultation_summary: dict | None = None, track: str = DEFAULT_TRACK, playbook: str | None = None,
    ) -> None:
        # 今の雇用区分(dip の要約の search_track)と、その区分の音声版の playbook。区分が変わったら入れ替える
        self._track = track if track in TRACKS else DEFAULT_TRACK
        self._playbook = playbook
        # LINE から入った相談なら、前に本人が話した希望・相談で分かったこと、前の会話の相談のまとめ、
        # トークでアップロードした履歴書の要点を指示に足す(確認形で使い・聞き直さない)
        self._stated = stated_preferences
        self._consultation = consultation_summary
        self._resume = uploaded_resume
        super().__init__(instructions=self._build_instructions())
        self._dip = dip  # DipMCP(またはテスト用スタブ)。None なら求人提案は不可の旨を返す
        self._draft_client_factory = draft_client_factory  # () -> AsyncOpenAI 互換
        self._research_client_factory = research_client_factory  # () -> AsyncOpenAI 互換
        self._room = room  # 画面表示用の data channel(無ければ音声のみ)
        # トークから入った相談のユーザーID(匿名ユーザーID のスコープ)。あるときだけ dip に書き込み
        # (希望の保存・結果の記録)、終わりにトーク(line_runtime)へ結果を届ける。無ければ従来どおり読み取りだけ
        self._user_id = user_id
        self._talk_poster = talk_poster  # async (url, payload) -> None(テストで差し替え)
        self.delivered_to_talk = False  # 結果をトークに届けられたか
        # 「本人の希望」と「検索に使った条件」は分けて持つ(誠実性: 本人が言ったことだけを希望として残す)
        self.preferences: dict = {}  # 本人が言った希望
        self.recommend_args: dict = {}  # 最後の検索に使った条件(ゆるめたものを含む)
        self.relaxed: dict = {}  # 検索のためにゆるめた(外した)条件。希望(preferences)は書き換えない
        self.search_attempts: list[dict] = []  # 検索の経緯(使った条件と件数)
        # 出さない求人ID(propose_jobs の exclude_ids をこの面談の間ためる)。後で条件を変えて探すときに LLM が
        # exclude_ids を付け忘れても、外した求人(電話が中心の求人など)が出ないように、毎回まとめて dip に渡す
        self.excluded_ids: list[str] = []
        self.proposed_jobs: list[dict] = []
        self.finalized: dict | None = None
        self._finalize_reply: str | None = None  # finalize 済みなら2回目はこれを返す(副作用なし)
        self.summary: str | None = None  # 相談のまとめ(finalize_interview で受け取り、dip に記録する)
        self.forgotten = False  # 面談の途中で本人が「すべて消す」をした(トークが結果を受け取らなかった)
        self.web_searches = 0  # この相談で web を調べた回数(config.MAX_WEB_SEARCHES まで)
        # 全国・東京の平均時給の参考データ(会話のたびには調べない)
        self._wage_reference = wage_reference if wage_reference is not None else load_reference()

    @property
    def track(self) -> str:
        """今の雇用区分(アルバイト・パート / 正社員 / 未定)。"""
        return self._track

    @property
    def _room_name(self) -> str | None:
        """この通話の部屋の名前。dip への書き込みとトークへの結果に付ける(面談の途中で「すべて消す」をしたら、
        その部屋の書き込みと結果は断られる)。"""
        return getattr(self._room, "name", None) or None

    @property
    def document_kind(self) -> str:
        """この面談で作る下書きの書類(正社員なら職務経歴書、それ以外は履歴書)。"""
        return document_kind_for(self._track)

    def _build_instructions(self) -> str:
        return (advisor_instructions(self._track, self._playbook)
                + stated_preferences_note(self._stated, self._track)
                + consultation_summary_note(self._consultation)
                + uploaded_resume_note(self._resume))

    # --- ツール ---

    @function_tool
    async def set_search_track(self, track: str, evidence: str) -> str:
        """本人の発話から判断した雇用区分に切り替えて記録する。切り替えると、指示の相談の playbook がその区分の
        音声版に入れ替わり、下書きの書類も変わる(正社員なら職務経歴書、それ以外は履歴書)。

        明示(「正社員で探したい」「バイトを探している」)と強い合図(年収・転職の時期 → 正社員、シフト・時給・
        学校との両立 → アルバイト・パート)は1回で切り替える。弱い・混在した合図では呼ばず、先に1問で確かめる。
        履歴書の内容や前に話した希望だけでは呼ばない。今の区分と同じなら呼ばない。本人の発話を受けた応答の中で呼ぶ。

        Args:
            track: 切り替える先の雇用区分。アルバイト・パート / 正社員 / 未定 のどれか
            evidence: 切り替えの根拠になった本人の発言そのまま(要約・言い換えをしない)
        """
        if track not in TRACKS:
            return f"track は {' / '.join(TRACKS)} のどれか。"
        if track == self._track:
            return f"今の区分はすでに{track}です。切り替えていません。"
        if self._dip is not None and self._user_id:
            try:
                result = await self._dip.set_search_track(self._user_id, track, evidence, room=self._room_name)
            except Exception as exc:  # noqa: BLE001 - 記録できなくても会話は続ける
                logger.warning("set_search_track 失敗: %s", exc)
                return "区分を切り替えられませんでした。今の区分のまま続けてください。"
            if result.get("error"):
                return f"区分を切り替えられませんでした: {result['error']}"
        before, self._track = self._track, track
        # 前の区分で聞いた条件のうち、新しい区分で使わないもの(正社員に切り替えた後の時給など)は希望から外す
        self.preferences, _ = self._on_track_items(self.preferences)
        self._playbook = await self._load_playbook(track)
        await self.update_instructions(self._build_instructions())
        await self._publish({"type": "track", "track": track})
        return (
            f"雇用区分を「{before}」から「{track}」に切り替えました"
            + ("(dip に記録済み)" if self._user_id else "")
            + f"。指示の相談の playbook を{track}(音声)のものに入れ替えたので、以降はそれに従う。"
            f"書類は「{document_name(track)}」と呼ぶ。本人には切り替えたことを1文で伝え、playbook の次の1問に進む。"
        )

    @function_tool
    async def save_user_preferences(
        self,
        purpose: str | None = None,
        strengths: str | None = None,
        wants: str | None = None,
        avoids: str | None = None,
        concerns: str | None = None,
        experience: str | None = None,
        skills: str | None = None,
        certifications: str | None = None,
        job_type: str | None = None,
        area: str | None = None,
        time_band: str | None = None,
        days_per_week: str | None = None,
        min_wage: str | None = None,
        min_annual_salary: str | None = None,
        work_style: str | None = None,
        start_timing: str | None = None,
    ) -> str:
        """本人が話した希望と自分のことを、その場で本人の言葉のまま保存する(チャットと同じ dip の項目。次の会話に
        引き継がれる)。本人が言ったことだけを、短い文で入れる。こちらの言い換え・まとめ・推測・履歴書からの補完は入れない。
        同じ発言を複数の項目に重複して入れない。保存先は playbook の「保存先」(今の区分で使わない条件は保存されない)。

        Args:
            purpose: 目的(なぜ今探すのか。例: 生活費の足しに、経験を活かしたい)
            strengths: 強み。本人が自分の強み・評価されたこととして話したこと
            wants: やりたいこと・やってみたいこと
            avoids: 避けたいこと・やりたくないこと
            concerns: 働くうえでの不安・気になっていること
            experience: これまで何をしてきたか(職歴・未経験など。例: 法人営業を5年)
            skills: 本人が「できる」と言った作業・知識
            certifications: 持っている資格・免許
            job_type: 職種(本人が挙げたとき)
            area: エリアか最寄り駅
            time_band: 時間帯(アルバイト・パートの条件)
            days_per_week: 週に働きたい日数(アルバイト・パートの条件)
            min_wage: 時給の下限(アルバイト・パートの条件。例: 1200)
            min_annual_salary: 希望年収の下限(正社員の条件。例: 年収500万円)
            work_style: 働き方(正社員の条件。例: リモート中心、残業少なめ)
            start_timing: 働き始める時期(正社員の条件。例: 3か月以内)
        """
        stated = {k: v for k, v in {
            "purpose": purpose, "strengths": strengths, "wants": wants, "avoids": avoids, "concerns": concerns,
            "experience": experience, "skills": skills, "certifications": certifications, "job_type": job_type,
            "area": area, "time_band": time_band, "days_per_week": days_per_week, "min_wage": min_wage,
            "min_annual_salary": min_annual_salary, "work_style": work_style, "start_timing": start_timing,
        }.items() if v is not None and str(v).strip()}
        stated, dropped = self._on_track_items(stated)
        self.preferences = {**self.preferences, **stated}
        if not stated:
            return "保存する項目がありませんでした。" + self._dropped_note(dropped)
        result = await self._save_stated_preferences(stated)
        # 単体で動かすとき(ユーザー不明)は dip に書かず、この面談の希望としてだけ持つ
        if result is None:
            names = "、".join(PREFERENCE_LABELS[k] for k in stated)
            return f"この面談の希望として記録しました: {names}。本人には言わずに会話を続ける。" + self._dropped_note(dropped)
        saved = [k for k in result.get("saved", []) if k in stated]
        rejected = [k for k in result.get("rejected", []) if k in stated]
        out = (f"保存しました: {'、'.join(PREFERENCE_LABELS[k] for k in saved)}。" if saved else "")
        if rejected:
            out += (f"保存できなかった項目: {'、'.join(PREFERENCE_LABELS[k] for k in rejected)}"
                    f"({result.get('note', '')})。短い本人の言葉にして保存し直すか、個人情報なら保存しない。")
        return (out or "保存できませんでした。") + "本人には言わずに会話を続ける。" + self._dropped_note(dropped)
    @function_tool
    async def propose_jobs(
        self,
        job_type: str | None = None,
        area: str | None = None,
        time_band: str | None = None,
        min_wage: int | None = None,
        max_talk_level: int | None = None,
        min_talk_level: int | None = None,
        max_physical_level: int | None = None,
        min_physical_level: int | None = None,
        remote: str | None = None,
        weekends_holidays: bool | None = None,
        max_overtime_hours: int | None = None,
        days_per_week: int | None = None,
        experience: str | None = None,
        skills: str | None = None,
        certifications: str | None = None,
        purpose: str | None = None,
        strengths: str | None = None,
        wants: str | None = None,
        avoids: str | None = None,
        concerns: str | None = None,
        min_annual_salary: int | None = None,
        work_style: str | None = None,
        start_timing: str | None = None,
        other_wishes: str | None = None,
        relax: list[str] | None = None,
        exclude_ids: list[str] | None = None,
        search_only: bool = False,
    ) -> str:
        """会話で聞いた希望条件に合う求人を、今の雇用区分の中から最大3件探す(区分が未定なら両方の区分から)。
        確認(要約)が済んでから呼ぶ。

        条件を変えて探し直すとき(例: 時給の下限を外す)は、了承の発話と同じ応答で呼び直す。
        「探します」と言うだけで呼ばずに応答を終えてはいけない。

        各引数には本人が言ったことだけを入れる。本人が決めていない・言っていない項目は
        空のままにする(経験からの推測で埋めない)。職種なしでも検索できる。

        0件のときは、どの条件を外すと何件見つかるかが結果に入る。それを本人に伝えて
        選んでもらい、選ばれた条件名をこれまでの relax に足して、他の引数は変えずにもう一度呼ぶ。

        結果の求人を出さずに探し直すとき(電話が中心の求人を外すとき、すでに出した求人を外すとき)は、
        その求人IDを exclude_ids に入れて、他の引数は変えずにもう一度呼ぶ。

        Args:
            job_type: 職種。本人が職種を言ったときだけ、今の雇用区分の一覧から1つをそのまま選ぶ。
                アルバイト・パート: アパレル販売 / イベントスタッフ / オフィス事務 / カフェ / コンビニ / コールセンター /
                スーパー・販売 / 倉庫・軽作業 / 塾講師・家庭教師 / 配送・デリバリー / 飲食キッチン / 飲食ホール。
                正社員: 営業 / 企画・PM / エンジニア / 事務・管理 / 販売・サービス / 製造・技術 / 医療・介護 / 教育 /
                クリエイティブ / 物流。区分が未定ならどちらの一覧からでもよい。
                本人の言葉(例: 飲食)が複数に当たるなら、一番近い1つにする。一覧に無い値はエラーになる。
                職種を決めていない人には入れない(経験から推測して選ばない)。
            area: エリアか最寄り駅(本人が言った場合だけ)。地名は 新宿 / 渋谷 / 池袋 / 品川 /
                上野 / 中野 / 高円寺 / 吉祥寺 / 横浜 / 川崎 / 大宮 のどれか、または駅名(例: 中野駅)。
                「東京都内」「都内」「どこでも」のような広い言い方は、言い換えずそのまま渡す
                (dip 側がその範囲の地名に広げて検索する)。一覧に無い地名はエラーになるので、
                そのときはエラーが示す一覧から選び直すか、area を外して検索する。
            time_band: 時間帯。早朝 / 日中 / 夕方 / 夜 / 深夜 のどれか1つ(アルバイト・パートの条件)
            min_wage: 希望の時給の下限(円)。本人が言った額のまま(アルバイト・パートの条件)
            max_talk_level: 人と話す量の上限(1〜3。1 = 人と話すことはほとんど無い、2 = お客様と話すことは少ない、
                3 = 接客・電話・指導など人と話すのが中心)。本人が避けたいことを言ったときだけ:
                「人と話すのが苦手」「黙々と作業したい」なら 1、「接客はしたくない」なら 2。電話だけを避けたいときは
                付けない(電話は接客と同じ 3 に入るので、2 にすると店頭の求人もほとんど消える)。接客も避けたいかを1問で
                確かめ、避けたいときだけ 2 にする
            min_talk_level: 人と話す量の下限(1〜3)。「人と話す仕事がしたい」「接客が好き」なら 3
            max_physical_level: 体を使う量の上限(1〜3。1 = 座り仕事が中心、2 = 立ち仕事や歩くが力仕事は少ない、
                3 = 運搬・配達・設営など体を使う)。「立ち仕事は避けたい」「座ってできる仕事」なら 1、
                「力仕事は避けたい」なら 2
            min_physical_level: 体を使う量の下限(1〜3)。「体を動かしたい」なら 3
            remote: リモートの希望。可(フルリモート) / 一部可(リモートも混ぜたい) のどちらか(正社員の条件)
            weekends_holidays: 土日祝休みを本人が希望したときだけ True(正社員の条件)
            max_overtime_hours: 月の残業時間の上限(時間。「残業なし」なら 0)(正社員の条件)
            days_per_week: 週に働きたい日数(検索条件には使わず、希望として記録する。アルバイト・パートの条件)
            experience: これまで何をしてきたか(職歴・未経験など)を本人の言葉のまま短く
                (例: コンビニで1年半、未経験)。検索条件には使わず、プロフィールとして記録する。
                バイト・仕事の経験が無ければ、本人が話した学校・家事・ボランティアなどの経験でよい
            skills: できること。本人が「できる」と言った作業を本人の言葉のまま短く
                (例: レジ打ちと品出し)。検索条件には使わず、プロフィールとして記録する。
                経験(これまで何をしてきたか)そのものや、こちらが候補として挙げたことは入れない
            certifications: 持っている資格・免許を本人の言葉のまま(例: 普通自動車免許)。
                検索条件には使わず、プロフィールとして記録する。「なし」と言われたら入れない
            purpose: 目的(なぜ今バイトを探すのか。例: 生活費の足しに、経験を活かしたい)。
                本人が話したときだけ、本人の言葉のまま短く。以下の4つも同じで、検索条件には使わず、
                プロフィールとして記録する(次の LINE の会話に引き継がれる)。こちらからの解釈は入れない
            strengths: 強み。本人が自分の強み・評価されたこととして話したこと
            wants: やりたいこと・やってみたいこと
            avoids: 避けたいこと・やりたくないこと
            concerns: 働くうえでの不安・気になっていること
            min_annual_salary: 希望年収の下限(万円。例: 年収500万円以上なら 500)。本人が言った額のまま(正社員の条件)
            work_style: 働き方の希望(例: リモート中心、残業少なめ)。検索条件には使わず、希望として記録する(正社員の条件)
            start_timing: 働き始める時期(例: 3か月以内)。検索条件には使わず、希望として記録する(正社員の条件)
            other_wishes: その他の希望を本人の言葉で短く(例: 家から近い店がいい)。
                検索の経緯(条件を外した・ゆるめた等)は書かない
            relax: 検索のためにゆるめる(外す)条件の名前。area / time_band / job_type / min_wage / min_annual_salary /
                max_talk_level / min_talk_level / max_physical_level / min_physical_level /
                remote / weekends_holidays / max_overtime_hours。
                0件だった検索の結果から本人が選んだものを、これまでの relax に足して入れる
                (前に外した条件も入れたままにする)。希望の引数は書き換えない。
            exclude_ids: 出さない求人ID(結果のかっこの求人ID)。電話が中心の求人(コールセンターなど)を外すとき、
                すでに出した求人を外して別の求人を探すときに使う。前に外したIDは覚えていて、この後の検索でも外す
                (入れたままでもよい)。外して探し直した結果が1件以上なら、それが新しい提案になり、外した求人はトークに
                届かない。0件なら前の提案がそのまま残る
            search_only: 本人の希望は変わっていないが、検索条件だけを変えて探し直すときに True
                (例: 見つからないので、本人の同意を得て時給の下限を外す)。
                True のときは希望条件の記録を変えず、検索条件だけを変える
        """
        stated = {
            k: v
            for k, v in {
                "job_type": job_type,
                "area": area,
                "time_band": time_band,
                "min_wage": min_wage,
                "days_per_week": days_per_week,
                "experience": experience,
                "skills": skills,
                "certifications": certifications,
                "purpose": purpose,
                "strengths": strengths,
                "wants": wants,
                "avoids": avoids,
                "concerns": concerns,
                "min_annual_salary": min_annual_salary,
                "work_style": work_style,
                "start_timing": start_timing,
                "other_wishes": other_wishes,
            }.items()
            if v is not None
        }
        # 今の区分で使わない条件(正社員の面談での時給・時間帯など)は、検索にも希望にも入れない
        stated, dropped = self._on_track_items(stated)
        if not search_only:
            # 言っていない項目(None)は消さない: 検索のために外した条件で希望が上書きされないように
            self.preferences = {**self.preferences, **stated}
        try:
            args = build_recommend_args(
                job_type=stated.get("job_type"), area=area, time_band=stated.get("time_band"),
                min_wage=stated.get("min_wage"), min_annual_salary=stated.get("min_annual_salary"), track=self._track,
                # 属性と働き方・休日は検索だけの引数(希望としては保存しない)。働き方・休日はアルバイト・パートの求人に無いので、
                # アルバイト・パートの面談では渡さない(渡すと全部外れる)
                max_talk_level=max_talk_level, min_talk_level=min_talk_level,
                max_physical_level=max_physical_level, min_physical_level=min_physical_level,
                **({} if self._track == "アルバイト・パート" else
                   {"remote": remote, "weekends_holidays": weekends_holidays, "max_overtime_hours": max_overtime_hours}),
            )
            # 希望はそのまま、検索だけをゆるめる(外した条件は relaxed として持つ)
            args, relaxed = split_relaxed(args, relax)
        except ValueError as exc:
            return f"引数が不正です: {exc}"
        if self._dip is None:
            return "いま求人データにつながっていないため提案できません。その旨を伝えて会話を締めてください。"
        label = "🔍 条件を変えて探し直しています…" if (relax or search_only) else "🔍 ぴったりの求人を探しています…"
        async with self._working(label):
            if not search_only:
                # 本人が言った希望だけを dip に保存する(検索のためにゆるめた条件や推測は保存しない)
                await self._save_stated_preferences(stated)
            # 出さない求人は検索だけに渡す(希望でも検索の条件でもないので recommend_args には入れない)。
            # 前の呼び出しで外した分も合わせて渡す(0件のときの relax_conditions にも同じものが渡る)
            self.excluded_ids = list(dict.fromkeys([*self.excluded_ids, *(exclude_ids or [])]))
            exclude = list(self.excluded_ids)
            search = {**args, "exclude_ids": exclude} if exclude else args
            try:
                result = await self._dip.recommend_jobs(**self._with_user(search))
            except Exception as exc:  # noqa: BLE001 - 提案に失敗しても会話は続ける
                logger.warning("recommend_jobs 失敗: %s", exc)
                return "求人の検索に失敗しました。その旨を伝えて会話を締めてください。"
            if result.get("error"):
                return f"検索エラー: {result['error']}"

            self.recommend_args = args
            self.relaxed = relaxed
            jobs = (result.get("jobs") or [])[:MAX_PROPOSALS]
            self.search_attempts.append(
                {
                    "args": args,
                    **({"exclude_ids": exclude} if exclude else {}),
                    "found": result.get("total", 0),
                    "proposed_job_ids": [j.get("job_id", "") for j in jobs],
                }
            )
            # 提案済みの記録は0件の再検索で消さない(out/ の job-proposal とトークに残すため)。外した求人を除いて
            # 探し直して0件のときも、前の提案をそのまま残す(「ほかには?」で0件でも、本人が聞いた求人はトークに届く)。
            # そのため、電話が中心の求人だけが当たって外した後に0件なら、読み上げていないその求人もトークに届く
            # (ユーザーの決定、2026-10-04)。画面はその回の結果をそのまま出す(0件なら「見つかりませんでした」)
            if jobs:
                self.proposed_jobs = jobs
            await self._publish({"type": "jobs", "jobs": jobs, "total": result.get("total", 0)})
            if not jobs:
                return await self._zero_hit_reply(search) + self._talk_note()
        lines = [f"{i}件目(求人ID {job.get('job_id', '')}): {spoken_job_line(job)}" for i, job in enumerate(jobs, 1)]
        return (
            f"求人が{len(jobs)}件見つかりました。"
            "1件ずつ短く読み上げてください(かっこの求人IDは読み上げない。外して探し直すときの exclude_ids に使う):\n"
            + "\n".join(lines) + self._talk_note() + self._dropped_note(dropped)
        )

    @function_tool
    async def web_research(self, query: str) -> str:
        """本人が会話の途中で気になったこと(仕事内容・資格・業界など)を web で調べる。

        「少し調べますね」と一言添えつつ、同じ応答の中でこのツールを呼ぶ(言うだけで応答を終えない)。
        時給の相場は wage_average、会社のことは research_company を使う。
        求人そのもの(どの求人があるか)は propose_jobs で探し、ここでは探さない。

        Args:
            query: 調べることを日本語で短く(例: コンビニバイト 仕事内容 未経験)。
                氏名・住所・電話番号など本人の個人情報は入れない。
        """
        return await self._research("🌐 調べています…", f"調べること: {query}")

    @function_tool
    async def wage_average(self, area: str | None = None, job_type: str | None = None) -> str:
        """アルバイト・パートの平均時給(相場)を答える。希望の時給を聞く前後で、本人が相場を知りたがったときに使う。

        全国と東京の平均は参考データからすぐ答えられる。それ以外の地域や職種は web で調べる。

        Args:
            area: 都道府県や市区町村(例: 大阪府、横浜市)。全国・東京なら空にする。
            job_type: 職種(例: コンビニ、カフェ)。職種を問わないなら空にする。
        """
        # 参考データから即答できるときは「調べています…」を出さない(一瞬で消える表示のちらつきを避ける)
        if not job_type and (answer := answer_from_reference(self._wage_reference, area)):
            return _spoken(answer)
        what = f"{job_type}の" if job_type else ""
        return await self._research(
            "🌐 平均時給を調べています…",
            f"調べること: {area or '全国'}の、{what}アルバイト・パートの平均時給。"
            "できるだけ新しい調査の数字を、調査の名前と時期を添えて伝えてください。",
        )

    @function_tool
    async def research_company(self, name: str, hint: str | None = None) -> str:
        """本人が気になった会社・お店のことを web で調べる(事業、規模、働く人の評判、注意点)。

        「少し調べますね」と一言添えつつ、同じ応答の中でこのツールを呼ぶ(言うだけで応答を終えない)。

        Args:
            name: 会社名かお店の名前(本人や求人に出た呼び方のまま)
            hint: 同じ名前の別の会社と区別するための手がかり(例: 中野のコンビニ、カフェチェーン)
        """
        return await self._research(
            f"🏢 {name}のことを調べています…",
            f"調べる会社・お店: {name}" + (f"(手がかり: {hint})" if hint else "") + "\n"
            "アルバイトとして働くかを考えている人向けに、事業の内容と規模、働く人の評判の傾向、気をつける点を伝えてください。"
            "同じ名前の会社が複数あって特定できないとき、見つからないときは、推測で埋めずにそう書き、情報源は添えないでください。",
        )

    @function_tool
    async def finalize_interview(self, summary: str | None = None) -> str:
        """会話を締める前に呼ぶ。文字起こしから下書き(区分が正社員なら職務経歴書、それ以外は履歴書)を作り、
        希望条件・提案した求人と一緒に保存し、相談のまとめを記録する。

        Args:
            summary: 相談のまとめ(200字以内)。次の会話(チャットでも音声でも)が続きから始めるための記録。
                書くのは、本人の発言に基づく事実(目的・希望・経験・不安など)と、次に確かめることだけ。
                こちらの解釈・評価・推測・性格の見立て、本人が言っていない希望、氏名・住所・連絡先は書かない。
                例: 経験を活かせる仕事を探している。法人営業を5年。残業の少なさを気にしている。次に確かめること: 希望の勤務地
        """
        # 二重実行ガード: 締めの後に「面談を終える」ボタンなどでもう一度呼ばれても、
        # 保存・結果の記録・トークへの送信を繰り返さず、前回の結果をそのまま返す
        if self.finalized is not None:
            return "(保存済みのため、2回目の保存はしていません)" + (
                self._finalize_reply
                or "下書きと求人の提案は保存済みです。内容は本人が確認・修正する旨を伝え、お礼を述べて締めてください。"
            )
        summary = (summary or "").strip() or None
        if summary and len(summary) > CONSULTATION_SUMMARY_MAX_CHARS:
            # 途中で切ると「次に確かめること」が落ちる(dip も記録しない)。何も保存せず書き直してもらう
            return (f"summary(相談のまとめ)が{len(summary)}字です。{CONSULTATION_SUMMARY_MAX_CHARS}字以内に書き直して、"
                    "もう一度 finalize_interview を呼んでください(まだ何も保存していません。本人には何も言わなくてよい)。")
        transcript = self._transcript()
        if not transcript:
            return "会話がまだありません。"
        self.summary = summary
        kind = self.document_kind
        client = self._draft_client_factory() if self._draft_client_factory else _default_client()
        try:
            self.finalized = await write_outputs(
                transcript,
                client=client,
                preferences=self.preferences,
                recommend_args=self.recommend_args,
                relaxed=self.relaxed,
                search_attempts=self.search_attempts,
                proposed_jobs=self.proposed_jobs,
                document_kind=kind,
            )
        except Exception as exc:
            logger.exception("下書きの保存に失敗")
            return f"下書きの保存に失敗しました({exc})。その旨を伝えて謝ってください。"
        await self._publish(
            {
                "type": "draft",
                "document_kind": kind,
                "markdown": self.finalized["markdown"],
                "files": self.finalized["files"],
            }
        )
        # LINE から入った相談なら、トークへ求人と下書きを戻し、結果と相談のまとめを記録する。
        # 面談の途中で本人が「すべて消す」をしていたら(トークが受け取らない)、dip に記録を作り直さない
        await self._deliver_to_talk()
        if not self.forgotten:
            await self._record_outcome()
        if self.forgotten:
            self._finalize_reply = (
                "面談の途中で、本人が覚えていることをすべて消す操作をしたため、下書きと求人の提案は残していません。"
                "そのことには触れすぎず、お礼を述べて短く締めてください。"
            )
        elif self.delivered_to_talk:
            self._finalize_reply = (
                f"{kind}の下書きと求人の提案を保存し、トークに送りました(話し終えると通話の画面が閉じ、トークに続きが出ます)。"
                "内容は本人が確認・修正する旨を伝え、お礼を述べて短く締めてください。"
            )
        else:
            self._finalize_reply = (
                f"{kind}の下書きと求人の提案を保存しました。"
                "内容は本人が確認・修正する旨を伝え、お礼を述べて締めてください。"
            )
        return self._finalize_reply

    # --- 内部 ---

    @asynccontextmanager
    async def _working(self, label: str):
        """時間のかかるツールの間、画面に「何をしているか」(アイコンと一言)を出す。

        失敗しても表示が残らないよう、終わりの通知は finally で必ず送る。
        終わりにも label を付け、どの経路(検索・調べもの)の表示を消したかがログや画面側で分かるようにする。
        画面が無い(room が None)ときは _publish が何もしないので、音声だけの相談でも安全。
        """
        await self._publish({"type": "status", "label": label})
        try:
            yield
        finally:
            await self._publish({"type": "status", "done": True, "label": label})

    # dip の save_user_preferences が受け付ける項目(dip_mcp/logic.py の PREFERENCE_KEYS と同期。テストで確認)。
    # 面談では experience も聞く(flow.py の面談ステージ)。区分ごとの条件のうち、今の区分で使わないものは
    # _on_track_items が落とす。other_wishes は dip の項目に無いので保存しない
    _DIP_PREF_KEYS = ("area", "time_band", "job_type", "min_wage", "experience",
                      "days_per_week", "skills", "certifications",
                      "purpose", "strengths", "wants", "avoids", "concerns",
                      "min_annual_salary", "work_style", "start_timing")

    def _on_track_items(self, stated: dict) -> tuple[dict, list[str]]:
        """今の雇用区分で使う項目だけを残す。(残した項目, 落とした項目名) を返す。
        正社員の面談での時給・時間帯や、アルバイトの面談での希望年収は、検索にも保存にも入れない
        (区分を戻したとき、使わない条件を「前回の希望」として確かめないように)。"""
        kept = {k: v for k, v in stated.items() if on_track(k, v, self._track)}
        return kept, [k for k in stated if k not in kept]

    def _talk_note(self) -> str:
        """面談が終わるとトークに届く予定の求人と、届かない求人(propose_jobs の結果に足す。読み上げない)。

        トークに届くのは最後に1件以上見つかった提案(proposed_jobs)だけで、外した求人はこの面談では出せない。
        本人が前の求人に戻りたがったときに、LLM が届かない求人を「トークに出ます」と約束しないための材料。
        """
        delivered = [j.get("job_id", "") for j in self.proposed_jobs]
        shown = dict.fromkeys(i for a in self.search_attempts for i in a.get("proposed_job_ids", []))
        earlier = [i for i in shown if i not in delivered and i not in self.excluded_ids]
        out = ("\n(読み上げない: 面談が終わるとトークに届く求人は、今の提案の求人ID "
               f"{'・'.join(delivered)} だけ。" if delivered else "\n(読み上げない: トークに届く求人はまだ無い。")
        gone = [i for i in self.excluded_ids if i not in delivered]
        if gone:
            out += f"外した求人ID {'・'.join(gone)} はこの面談ではもう出せず、トークにも届かない。"
        if kept := [i for i in self.excluded_ids if i in delivered]:
            # 外して探し直した結果が0件だったので、前の提案がそのまま残っている(ユーザーの決定、2026-10-04)
            out += (f"求人ID {'・'.join(kept)} は外して探し直したが0件だったので、前の提案としてトークに届く"
                    "(この面談の検索ではもう出ない)。")
        if earlier:
            out += f"前の結果に出た求人ID {'・'.join(earlier)} もトークには届かない。"
        return out + "本人が前の求人に戻りたがったら、トークに届かない求人を「トークに出ます」「送ります」と言わない)"

    def _dropped_note(self, dropped: list[str]) -> str:
        if not dropped:
            return ""
        names = "、".join(PREFERENCE_LABELS.get(k, k) for k in dropped)
        return (f"\n(今の雇用区分「{self._track}」では使わない条件なので、{names}は検索にも保存にも入れていません。"
                "区分が決まってから・区分に合う条件で聞き直す)")

    async def _load_playbook(self, track: str) -> str | None:
        """その区分の音声版の playbook を dip から読む。読めなければ None(指示は最小の進め方になる)。"""
        if self._dip is None:
            return None
        try:
            result = await self._dip.get_counseling_playbook(track, "voice")
        except Exception as exc:  # noqa: BLE001 - 読めなくても会話は続ける
            logger.warning("get_counseling_playbook 失敗: %s", exc)
            return None
        return result.get("playbook") or None

    def _with_user(self, args: dict) -> dict:
        """ユーザーが分かるときは dip の呼び出しに付ける(本人の傾向が効く)。dip の引数名は line_user_id。"""
        if not self._user_id:
            return args
        return {"line_user_id": self._user_id, **args}

    async def _save_stated_preferences(self, stated: dict) -> dict | None:
        """本人が言った希望を本人の言葉のまま dip に保存する(task 35 と同じ決まり)。dip の結果({"saved", "rejected"})を返す。
        dip に書かなかった(ユーザー不明・項目なし・失敗)ときは None。

        渡すのは stated(本人が言った引数)だけ。検索のためにゆるめた条件(relax)や
        search_only の再検索の条件は、ここに来ない。失敗しても会話は続ける。
        """
        if self._dip is None or not self._user_id:
            return None
        prefs = {k: stated[k] for k in self._DIP_PREF_KEYS if k in stated}
        if not prefs:
            return None
        try:
            return await self._dip.save_user_preferences(self._user_id, prefs, room=self._room_name)
        except Exception as exc:  # noqa: BLE001 - 保存に失敗しても面談は続ける
            logger.warning("save_user_preferences 失敗: %s", exc)
            return None

    async def _record_outcome(self) -> None:
        """会話の結果を dip に記録する(コードで決める。LLM の判断で wait にしない)。

        求人を提案できたら propose、提案なし(0件のままなど)は consult。
        wait はここからは決して記録しない(断っていない人を wait にしない決まり。
        旧リポジトリのコミット 7711b20 / 1750afd)。
        """
        if self._dip is None or not self._user_id:
            return
        kind = self.document_kind
        if self.proposed_jobs:
            outcome = "propose"
            reason = f"音声相談で求人{len(self.proposed_jobs)}件を提案し、{kind}の下書きを保存した"
        else:
            outcome = "consult"
            reason = f"音声相談で希望を聞き取り、{kind}の下書きを保存した(条件に合う求人は提案できず)"
        try:
            await self._dip.record_conversation_outcome(
                self._user_id, outcome, reason, confidence=1.0, summary=self.summary, room=self._room_name
            )
        except Exception as exc:  # noqa: BLE001 - 記録に失敗しても面談は締められる
            logger.warning("record_conversation_outcome 失敗: %s", exc)

    async def _deliver_to_talk(self) -> None:
        """提案した求人と下書きをトーク(line_runtime の /api/voice/result)へ届ける。失敗しても out/ には残る。
        トークが受け取らなかった(面談の途中で本人が「すべて消す」をした)ら forgotten にする。"""
        if not self._user_id or self.finalized is None:
            return
        payload = {
            "user_id": self._user_id,
            "jobs": self.proposed_jobs,  # dip の recommend_jobs が返したカードの形のまま
            "draft_markdown": self.finalized["markdown"],
            "document_kind": self.document_kind,  # トークの吹き出し・ボタン・下書きページの題を書類に合わせる
            "files": self.finalized["files"],
            # 本人が話した希望(トークに出す「話した内容のまとめ」。dip へ保存したのと同じ項目だけ)
            "preferences": {k: v for k, v in self._on_track_items(self.preferences)[0].items() if k in self._DIP_PREF_KEYS},
            # この面談の部屋(トークのシートは、自分の部屋の結果だけを受け取る)
            "room": self._room_name,
        }
        poster = self._talk_poster or _post_json
        try:
            await poster(f"{config.TALK_URL}/api/voice/result", payload)
            self.delivered_to_talk = True
        except TalkForgotten:
            logger.info("面談の途中で本人がトークの記録をすべて消したため、結果を届けず dip にも記録せず、書き出したファイルも消す")
            self.forgotten = True
            _remove_files(self.finalized["files"])
        except Exception as exc:  # noqa: BLE001 - トークへ戻せなくても下書きは保存済み
            logger.warning("LINE のトークへの結果送信に失敗: %s", exc)

    async def _zero_hit_reply(self, args: dict) -> str:
        """0件のとき: dip の relax_conditions(読み取りだけ)で、どの条件を外すと何件になるかを調べる。

        当てずっぽうの再検索ではなく、具体的な選択肢を本人に一度で選んでもらうための材料を返す。
        """
        try:
            relax_result = await self._dip.relax_conditions(**self._with_user(args))
        except Exception as exc:  # noqa: BLE001 - 調べられなくても会話は続ける
            logger.warning("relax_conditions 失敗: %s", exc)
            relax_result = None
        if relax_result is None or relax_result.get("error"):
            # 調べられなかったのに「確認済み」と言うのは嘘になる。正直に伝える
            return (
                "条件に合う求人が見つかりませんでした。どの条件を外すと増えるかは調べられませんでした。"
                "そのまま正直に伝えて、条件を変えて探し直すか、今回は求人なしで締めるかを"
                "本人に選んでもらってください。本人の同意を得て条件を変えて探し直すときは "
                "search_only=True で呼ぶ(希望の記録は変えない)。"
            )
        options = relax_result.get("options") or []
        if not options:
            return (
                "条件に合う求人が見つかりませんでした。どれか1つ条件を外しても増えないことも"
                "確認済みです。当てずっぽうで条件を変えて探し直さず、そのまま伝えて、"
                "条件を大きく変えるか、今回は求人なしで締めるかを本人に選んでもらってください。"
            )
        lines = [relax_option_line(o) for o in options]
        return (
            "条件に合う求人が見つかりませんでした。ただし、次のとおり条件を1つ外すと見つかります:\n"
            + "\n".join(lines)
            + "\nこの件数を具体的に伝えて、どの条件を外すか本人に一度で選んでもらってください。"
            "「探してもよいですか?」のような許可の確認はしないこと。選ばれたら、その条件名を "
            "これまでの relax に足して propose_jobs をもう一度呼ぶ(希望の引数と exclude_ids は変えない)。"
        )

    async def _research(self, label: str, subject: str) -> str:
        """web で調べて読み上げ用の答えを返す。1 回の相談で config.MAX_WEB_SEARCHES 回まで(費用の上限)。

        上限を超えたら調べず、表示も出さない(一瞬で消える表示を出さない)。失敗した回も 1 回に数える。
        """
        if self.web_searches >= config.MAX_WEB_SEARCHES:
            logger.info("web の調べものは上限(%d 回)に達したので調べません", config.MAX_WEB_SEARCHES)
            return (f"この相談で web を調べられる回数({config.MAX_WEB_SEARCHES}回)を使い切りました。"
                    "今回はこれ以上調べられないと短く伝えて、会話の続きに戻ってください(調べもののツールはもう呼ばない)。")
        self.web_searches += 1
        async with self._working(label):
            return _spoken(await self._search(subject))

    async def _search(self, subject: str) -> str | None:
        """web_search で調べて、話し言葉の短い答えを返す。失敗したら None(会話は止めない)。"""
        client = self._research_client_factory() if self._research_client_factory else _default_client()
        try:
            resp = await client.responses.create(
                model=config.RESEARCH_MODEL,
                # 音声の会話で待たせないよう軽くする(実測: 既定 約16秒 → この設定で 約7秒)
                tools=[{"type": "web_search", "search_context_size": "low"}],
                reasoning={"effort": "none"},
                input=(
                    "次のことを web で調べ、日本語の話し言葉(です・ます調)で2〜3文にまとめてください。"
                    "記号・箇条書き・URL は使わず、最後に主な情報源のサイト名を一言添えてください。"
                    "分からなかったことは分からないと書いてください。\n\n" + subject
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 調べものに失敗しても会話は続ける
            logger.warning("web 検索に失敗: %s", exc)
            return None
        return _strip_links(resp.output_text or "")

    def _transcript(self) -> list[dict]:
        try:
            return history_transcript(self.session.history)
        except RuntimeError:  # セッション未開始(テストなど)は自分の chat_ctx から
            return history_transcript(self.chat_ctx)

    async def _publish(self, payload: dict) -> None:
        """ブラウザ(画面)へ data channel で送る。room が無ければ何もしない。"""
        if self._room is None:
            return
        try:
            await self._room.local_participant.publish_data(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                reliable=True,
                topic=DATA_TOPIC,
            )
        except Exception as exc:  # noqa: BLE001 - 画面表示は音声の補助なので失敗しても続ける
            logger.warning("画面への送信に失敗: %s", exc)


def _remove_files(files: list[str]) -> None:
    """書き出した下書き・文字起こし・求人提案のファイルを消す(本人が「すべて消す」をした面談のもの)。"""
    for f in files:
        try:
            os.remove(f)
        except OSError as exc:
            logger.warning("ファイルを消せませんでした: %s", exc)


def _spoken(answer: str | None) -> str:
    if answer is None:
        return "調べものに失敗しました。うまく調べられなかったと伝えて、会話の続きに戻ってください。"
    if not answer:
        return "調べても分かりませんでした。そう伝えて、会話の続きに戻ってください。"
    return (
        "web で調べた結果です。ネットの情報であることが分かるように2〜3文で伝え、"
        f"そのあと会話の続き(中断した段階)に戻ってください:\n{answer}"
    )


def _strip_links(text: str) -> str:
    """web_search が付ける出典リンク([サイト](URL))や URL を消す(読み上げないため)。"""
    text = re.sub(r"\(?\[([^\]]*)\]\([^)]*\)\)?", "", text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _default_client():
    from openai import AsyncOpenAI

    return AsyncOpenAI()


async def _post_json(url: str, payload: dict) -> dict:
    """line_runtime へ JSON を POST し、返ってきた JSON を返す(結果をトークへ戻す)。aiohttp は livekit-agents の依存。
    サーバー間の経路なので、共有鍵(環境変数 AICA_INTERNAL_KEY。line_runtime と同じ値)を付ける。"""
    import aiohttp

    headers = {"Authorization": f"Bearer {os.environ.get('AICA_INTERNAL_KEY', '')}"}
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session,
        session.post(url, json=payload, headers=headers) as resp,
    ):
        if resp.status == 410:  # 面談の途中で本人が「すべて消す」をした(line_runtime.server の voice_result_in)
            raise TalkForgotten()
        resp.raise_for_status()
        return await resp.json(content_type=None) or {}
