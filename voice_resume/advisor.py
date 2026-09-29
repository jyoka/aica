"""音声アドバイザー本体(LiveKit Agent)。

会話の脳は OpenAI(gpt-6-sol)。ツールは5つ:
- propose_jobs: 聞いた希望条件から dip の recommend_jobs(MCP)で求人を最大3件出す。
  0件なら dip の relax_conditions(読み取りだけ)で「どの条件を外すと何件になるか」を調べ、
  本人に選んでもらう選択肢として返す(当てずっぽうの再検索をさせない)
- web_research / wage_average / research_company: 会話中に本人が気になったこと(仕事内容・資格など)、
  平均時給(全国と東京は参考データ data/wage_reference.json からすぐ答える)、気になる会社を web で調べて短く答える
- finalize_interview: 文字起こしから履歴書の下書きを作り、希望条件・提案した求人IDと一緒に out/ へ保存

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
    MAX_PROPOSALS,
    build_recommend_args,
    relax_option_line,
    split_relaxed,
    spoken_job_line,
)
from voice_resume.outputs import history_transcript, write_outputs
from voice_resume.prompts import (
    ADVISOR_INSTRUCTIONS,
    consultation_summary_note,
    stated_preferences_note,
    uploaded_resume_note,
)
from voice_resume.wages import WageReference, answer_from_reference, load_reference

logger = logging.getLogger("voice-resume.advisor")

DATA_TOPIC = "voice-resume"


class ResumeAdvisor(Agent):
    """バイト探しの希望を聞き、求人を提案し、履歴書の下書きを残すアドバイザー。"""

    def __init__(
        self, *, dip=None, draft_client_factory=None, research_client_factory=None, room=None,
        wage_reference: WageReference | None = None, user_id: str | None = None,
        stated_preferences: dict | None = None, uploaded_resume: dict | None = None, talk_poster=None,
        consultation_summary: dict | None = None,
    ) -> None:
        # LINE から入った相談なら、前に本人が話した希望・相談で分かったこと、前の会話の相談のまとめ、
        # トークでアップロードした履歴書の要点を指示に足す(確認形で使い・聞き直さない)
        super().__init__(instructions=ADVISOR_INSTRUCTIONS + stated_preferences_note(stated_preferences)
                         + consultation_summary_note(consultation_summary)
                         + uploaded_resume_note(uploaded_resume))
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
        self.proposed_jobs: list[dict] = []
        self.finalized: dict | None = None
        self._finalize_reply: str | None = None  # finalize 済みなら2回目はこれを返す(副作用なし)
        # 全国・東京の平均時給の参考データ(会話のたびには調べない)
        self._wage_reference = wage_reference if wage_reference is not None else load_reference()

    # --- ツール ---

    @function_tool
    async def propose_jobs(
        self,
        job_type: str | None = None,
        area: str | None = None,
        time_band: str | None = None,
        min_wage: int | None = None,
        days_per_week: int | None = None,
        experience: str | None = None,
        skills: str | None = None,
        certifications: str | None = None,
        purpose: str | None = None,
        strengths: str | None = None,
        wants: str | None = None,
        avoids: str | None = None,
        concerns: str | None = None,
        other_wishes: str | None = None,
        relax: list[str] | None = None,
        search_only: bool = False,
    ) -> str:
        """会話で聞いた希望条件に合う求人を最大3件探す。確認(要約)が済んでから呼ぶ。

        条件を変えて探し直すとき(例: 時給の下限を外す)は、了承の発話と同じ応答で呼び直す。
        「探します」と言うだけで呼ばずに応答を終えてはいけない。

        各引数には本人が言ったことだけを入れる。本人が決めていない・言っていない項目は
        空のままにする(経験からの推測で埋めない)。職種なしでも検索できる。

        0件のときは、どの条件を外すと何件見つかるかが結果に入る。それを本人に伝えて
        選んでもらい、選ばれた条件名をこれまでの relax に足して、他の引数は変えずにもう一度呼ぶ。

        Args:
            job_type: 職種。本人が職種を言ったときだけ、次の中から1つをそのまま選ぶ:
                アパレル販売 / イベントスタッフ / オフィス事務 / カフェ / コンビニ / コールセンター /
                スーパー・販売 / 倉庫・軽作業 / 塾講師・家庭教師 / 配送・デリバリー / 飲食キッチン / 飲食ホール。
                本人の言葉(例: 飲食)が複数に当たるなら、一番近い1つにする。一覧に無い値はエラーになる。
                職種を決めていない人には入れない(経験から推測して選ばない)。
            area: エリアか最寄り駅(本人が言った場合だけ)。地名は 新宿 / 渋谷 / 池袋 / 品川 /
                上野 / 中野 / 高円寺 / 吉祥寺 / 横浜 / 川崎 / 大宮 のどれか、または駅名(例: 中野駅)。
                「東京都内」「都内」「どこでも」のような広い言い方は、言い換えずそのまま渡す
                (dip 側がその範囲の地名に広げて検索する)。一覧に無い地名はエラーになるので、
                そのときはエラーが示す一覧から選び直すか、area を外して検索する。
            time_band: 時間帯。早朝 / 日中 / 夕方 / 夜 / 深夜 のどれか1つ
            min_wage: 希望の時給の下限(円)。本人が言った額のまま
            days_per_week: 週に働きたい日数(検索条件には使わず、希望として記録する)
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
            other_wishes: その他の希望を本人の言葉で短く(例: 家から近い店がいい)。
                検索の経緯(条件を外した・ゆるめた等)は書かない
            relax: 検索のためにゆるめる(外す)条件の名前。area / time_band / job_type / min_wage。
                0件だった検索の結果から本人が選んだものを、これまでの relax に足して入れる
                (前に外した条件も入れたままにする)。希望の引数は書き換えない。
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
                "other_wishes": other_wishes,
            }.items()
            if v is not None
        }
        if not search_only:
            # 言っていない項目(None)は消さない: 検索のために外した条件で希望が上書きされないように
            self.preferences = {**self.preferences, **stated}
        try:
            args = build_recommend_args(
                job_type=job_type, area=area, time_band=time_band, min_wage=min_wage
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
            try:
                result = await self._dip.recommend_jobs(**self._with_user(args))
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
                    "found": result.get("total", 0),
                    "proposed_job_ids": [j.get("job_id", "") for j in jobs],
                }
            )
            # 提案済みの記録は0件の再検索で消さない(out/ の job-proposal に残すため)。
            # 画面はその回の結果をそのまま出す(0件なら「見つかりませんでした」)
            if jobs:
                self.proposed_jobs = jobs
            await self._publish({"type": "jobs", "jobs": jobs, "total": result.get("total", 0)})
            if not jobs:
                return await self._zero_hit_reply(args)
        lines = [f"{i}件目: {spoken_job_line(job)}" for i, job in enumerate(jobs, 1)]
        return (
            f"求人が{len(jobs)}件見つかりました(面談が終わるとトークにも出ます)。"
            "1件ずつ短く読み上げてください:\n" + "\n".join(lines)
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
        async with self._working("🌐 調べています…"):
            return _spoken(await self._search(f"調べること: {query}"))

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
        async with self._working("🌐 平均時給を調べています…"):
            return _spoken(await self._search(
                f"調べること: {area or '全国'}の、{what}アルバイト・パートの平均時給。"
                "できるだけ新しい調査の数字を、調査の名前と時期を添えて伝えてください。"
            ))

    @function_tool
    async def research_company(self, name: str, hint: str | None = None) -> str:
        """本人が気になった会社・お店のことを web で調べる(事業、規模、働く人の評判、注意点)。

        「少し調べますね」と一言添えつつ、同じ応答の中でこのツールを呼ぶ(言うだけで応答を終えない)。

        Args:
            name: 会社名かお店の名前(本人や求人に出た呼び方のまま)
            hint: 同じ名前の別の会社と区別するための手がかり(例: 中野のコンビニ、カフェチェーン)
        """
        async with self._working(f"🏢 {name}のことを調べています…"):
            return _spoken(await self._search(
                f"調べる会社・お店: {name}" + (f"(手がかり: {hint})" if hint else "") + "\n"
                "アルバイトとして働くかを考えている人向けに、事業の内容と規模、働く人の評判の傾向、気をつける点を伝えてください。"
                "同じ名前の会社が複数あって特定できないとき、見つからないときは、推測で埋めずにそう書き、情報源は添えないでください。"
            ))

    @function_tool
    async def finalize_interview(self) -> str:
        """会話を締める前に呼ぶ。文字起こしから履歴書の下書きを作り、希望条件・提案した求人と一緒に保存する。"""
        # 二重実行ガード: 締めの後に「面談を終える」ボタンなどでもう一度呼ばれても、
        # 保存・結果の記録・トークへの送信を繰り返さず、前回の結果をそのまま返す
        if self.finalized is not None:
            return "(保存済みのため、2回目の保存はしていません)" + (
                self._finalize_reply
                or "下書きと求人の提案は保存済みです。内容は本人が確認・修正する旨を伝え、お礼を述べて締めてください。"
            )
        transcript = self._transcript()
        if not transcript:
            return "会話がまだありません。"
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
            )
        except Exception as exc:
            logger.exception("下書きの保存に失敗")
            return f"下書きの保存に失敗しました({exc})。その旨を伝えて謝ってください。"
        await self._publish(
            {
                "type": "draft",
                "markdown": self.finalized["markdown"],
                "files": self.finalized["files"],
            }
        )
        # LINE から入った相談なら、結果を記録し、トークへ求人と下書きを戻す
        await self._record_outcome()
        await self._deliver_to_talk()
        if self.delivered_to_talk:
            self._finalize_reply = (
                "下書きと求人の提案を保存し、トークに送りました(話し終えると通話の画面が閉じ、トークに続きが出ます)。"
                "内容は本人が確認・修正する旨を伝え、お礼を述べて短く締めてください。"
            )
        else:
            self._finalize_reply = (
                "下書きと求人の提案を保存しました。"
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
    # 面談では experience も聞く(flow.py の面談ステージ)。正社員の条件(FULLTIME_PREFERENCE_KEYS)は、
    # 音声がアルバイトの面談だけのあいだは受け取らない(音声の playbook で足す)。
    # other_wishes は dip の項目に無いので保存しない
    _DIP_PREF_KEYS = ("area", "time_band", "job_type", "min_wage", "experience",
                      "days_per_week", "skills", "certifications",
                      "purpose", "strengths", "wants", "avoids", "concerns")

    def _with_user(self, args: dict) -> dict:
        """ユーザーが分かるときは dip の呼び出しに付ける(本人の傾向が効く)。dip の引数名は line_user_id。"""
        if not self._user_id:
            return args
        return {"line_user_id": self._user_id, **args}

    async def _save_stated_preferences(self, stated: dict) -> None:
        """本人が言った希望を本人の言葉のまま dip に保存する(task 35 と同じ決まり)。

        渡すのは stated(本人が言った引数)だけ。検索のためにゆるめた条件(relax)や
        search_only の再検索の条件は、ここに来ない。失敗しても会話は続ける。
        """
        if self._dip is None or not self._user_id:
            return
        prefs = {k: stated[k] for k in self._DIP_PREF_KEYS if k in stated}
        if not prefs:
            return
        try:
            await self._dip.save_user_preferences(self._user_id, prefs)
        except Exception as exc:  # noqa: BLE001 - 保存に失敗しても面談は続ける
            logger.warning("save_user_preferences 失敗: %s", exc)

    async def _record_outcome(self) -> None:
        """会話の結果を dip に記録する(コードで決める。LLM の判断で wait にしない)。

        求人を提案できたら propose、提案なし(0件のままなど)は consult。
        wait はここからは決して記録しない(断っていない人を wait にしない決まり。
        旧リポジトリのコミット 7711b20 / 1750afd)。
        """
        if self._dip is None or not self._user_id:
            return
        if self.proposed_jobs:
            outcome = "propose"
            reason = f"音声相談で求人{len(self.proposed_jobs)}件を提案し、履歴書の下書きを保存した"
        else:
            outcome = "consult"
            reason = "音声相談で希望を聞き取り、履歴書の下書きを保存した(条件に合う求人は提案できず)"
        try:
            await self._dip.record_conversation_outcome(
                self._user_id, outcome, reason, confidence=1.0
            )
        except Exception as exc:  # noqa: BLE001 - 記録に失敗しても面談は締められる
            logger.warning("record_conversation_outcome 失敗: %s", exc)

    async def _deliver_to_talk(self) -> None:
        """提案した求人と下書きをトーク(line_runtime の /api/voice/result)へ届ける。失敗しても out/ には残る。"""
        if not self._user_id or self.finalized is None:
            return
        payload = {
            "user_id": self._user_id,
            "jobs": self.proposed_jobs,  # dip の recommend_jobs が返したカードの形のまま
            "draft_markdown": self.finalized["markdown"],
            "files": self.finalized["files"],
            # 本人が話した希望(トークに出す「話した内容のまとめ」。dip へ保存したのと同じ項目だけ)
            "preferences": {k: self.preferences[k] for k in self._DIP_PREF_KEYS if k in self.preferences},
            # この面談の部屋(トークのシートは、自分の部屋の結果だけを受け取る)
            "room": getattr(self._room, "name", None) or None,
        }
        poster = self._talk_poster or _post_json
        try:
            await poster(f"{config.TALK_URL}/api/voice/result", payload)
            self.delivered_to_talk = True
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
            "これまでの relax に足して propose_jobs をもう一度呼ぶ(希望の引数は変えない)。"
        )

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


async def _post_json(url: str, payload: dict) -> None:
    """line_runtime へ JSON を POST する(結果をトークへ戻す)。aiohttp は livekit-agents の依存。
    サーバー間の経路なので、共有鍵(環境変数 AICA_INTERNAL_KEY。line_runtime と同じ値)を付ける。"""
    import aiohttp

    headers = {"Authorization": f"Bearer {os.environ.get('AICA_INTERNAL_KEY', '')}"}
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session,
        session.post(url, json=payload, headers=headers) as resp,
    ):
        resp.raise_for_status()
