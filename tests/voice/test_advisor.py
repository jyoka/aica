"""アドバイザーのツール(求人提案・面談の締め)のオフラインテスト。実 API は呼ばない。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from resume_draft.schema import ResumeDraft
from voice_resume import config
from voice_resume.advisor import PERSONAL_QUERY_REPLY, ResumeAdvisor
from voice_resume.wages import (
    WageEntry,
    WageReference,
    fetch_reference,
    load_reference,
    save_reference,
)

JOBS = [
    {"job_id": "b0305", "title": "カフェ店長候補/バリスタ", "employer": "A", "job_type": "カフェ",
     "area": "大宮", "station": "大宮駅", "walk_min": 3, "pay": {"kind": "時給", "min": 1600, "max": None}, "pay_text": "時給1600円",
     "shift": "10:00-22:00", "days_min": 4, "perks": [], "reasons": ["時給1600円"]},
    {"job_id": "b0304", "title": "カフェホールスタッフ", "employer": "B", "job_type": "カフェ",
     "area": "中野", "station": "中野駅", "walk_min": 4, "pay": {"kind": "時給", "min": 1120, "max": None}, "pay_text": "時給1120円",
     "shift": "10:00-20:00", "days_min": 2, "perks": [], "reasons": ["週2日から"]},
    {"job_id": "b0301", "title": "カフェスタッフ", "employer": "C", "job_type": "カフェ",
     "area": "吉祥寺", "station": "吉祥寺駅", "walk_min": 2, "pay": {"kind": "時給", "min": 1180, "max": None}, "pay_text": "時給1180円",
     "shift": "8:00-20:00", "days_min": 3, "perks": [], "reasons": ["駅から近い"]},
]


class FakeDip:
    """recommend_jobs / relax_conditions / 書き込みツールの呼び出しを記録して、決まった結果を返す。"""

    def __init__(
        self, result=None, error: Exception | None = None,
        relax_result=None, relax_error: Exception | None = None,
    ):
        self.calls: list[dict] = []
        self.relax_calls: list[dict] = []
        self.save_calls: list[dict] = []
        self.outcome_calls: list[dict] = []
        self.result = result if result is not None else {"total": 4, "jobs": JOBS}
        self.error = error
        self.relax_result = relax_result if relax_result is not None else {"total_now": 0, "options": []}
        self.relax_error = relax_error

    async def recommend_jobs(self, **args) -> dict:
        self.calls.append(args)
        if self.error:
            raise self.error
        return self.result

    async def relax_conditions(self, **args) -> dict:
        self.relax_calls.append(args)
        if self.relax_error:
            raise self.relax_error
        return self.relax_result

    async def save_user_preferences(self, user_id: str, preferences: dict) -> dict:
        self.save_calls.append({"line_user_id": user_id, "preferences": preferences})
        return {"saved": list(preferences), "rejected": []}

    async def record_conversation_outcome(
        self, user_id: str, outcome: str, reason: str, confidence: float, summary: str | None = None
    ) -> dict:
        if outcome not in ("propose", "consult"):  # 実物の DipMCP と同じ制限
            raise ValueError("voice-resume から記録できる outcome は propose / consult だけ")
        self.outcome_calls.append({
            "line_user_id": user_id, "outcome": outcome, "reason": reason,
            "confidence": confidence, "summary": summary,
        })
        return {"recorded": {"outcome": outcome}}


class SequenceDip:
    """呼び出しごとに決まった結果を順に返す(検索し直しの台本用)。"""

    def __init__(self, results: list[dict]):
        self.calls: list[dict] = []
        self._results = list(results)

    async def recommend_jobs(self, **args) -> dict:
        self.calls.append(args)
        return self._results.pop(0)


class _FakeDraftClient:
    """drafter が呼ぶ responses.parse のスタブ。"""

    def __init__(self):
        self.responses = SimpleNamespace(parse=self._parse)
        self.calls: list[dict] = []

    async def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=ResumeDraft(job_summary="コンビニで1年半勤務。"))


def test_tool_docs_require_same_turn_call():
    """ツール説明が「同じ応答で呼ぶ」を明記し、別ターンを誘発する「呼ぶ前に一言添える」が残っていない(タスク 36)。"""
    docs = {
        name: getattr(ResumeAdvisor, name).__doc__ or ""
        for name in ("propose_jobs", "web_research", "research_company")
    }
    for name, doc in docs.items():
        assert "呼ぶ前に" not in doc, name
    assert "同じ応答で呼び直す" in docs["propose_jobs"]
    for name in ("web_research", "research_company"):
        assert "同じ応答の中でこのツールを呼ぶ" in docs[name], name


def test_propose_jobs_builds_args_from_heard_wishes():
    """会話で聞いた希望から recommend_jobs の引数が作られる(検索に使わない項目は渡さない)。"""
    dip = FakeDip()
    advisor = ResumeAdvisor(dip=dip)
    reply = asyncio.run(
        advisor.propose_jobs(
            job_type="カフェ", time_band="夕方", min_wage=1100,
            days_per_week=2, other_wishes="平日に入りたい",
        )
    )
    assert dip.calls == [{"job_type": "カフェ", "time_band": "夕方", "min_wage": 1100}]
    # 希望条件は日数やその他の希望も含めて記録される(out/ の job-proposal に入る)
    assert advisor.preferences == {
        "job_type": "カフェ", "time_band": "夕方", "min_wage": 1100,
        "days_per_week": 2, "other_wishes": "平日に入りたい",
    }
    assert [j["job_id"] for j in advisor.proposed_jobs] == ["b0305", "b0304", "b0301"]
    # 読み上げ用: 1件ずつ短く(職種・場所・時給・一言)
    assert "1件目" in reply and "3件目" in reply
    assert "時給1120円" in reply


def test_propose_jobs_passes_wide_area_through_to_dip():
    """会話ログ(2026-09-26)の「東京都内」は、言い換えず dip 側へ渡す(dip が範囲の地名に広げる)。"""
    dip = FakeDip()
    advisor = ResumeAdvisor(dip=dip)
    asyncio.run(advisor.propose_jobs(job_type="カフェ", area="東京都内", time_band="日中"))
    assert dip.calls == [{"job_type": "カフェ", "area": "東京都内", "time_band": "日中"}]


def test_propose_jobs_rejects_unknown_job_type_without_calling_dip():
    dip = FakeDip()
    advisor = ResumeAdvisor(dip=dip)
    reply = asyncio.run(advisor.propose_jobs(job_type="喫茶店"))
    assert "引数が不正" in reply
    assert dip.calls == []


def test_propose_jobs_limits_to_three():
    dip = FakeDip(result={"total": 9, "jobs": JOBS + JOBS})
    advisor = ResumeAdvisor(dip=dip)
    asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert len(advisor.proposed_jobs) == 3


def test_propose_jobs_passes_through_dip_error():
    dip = FakeDip(result={"error": "職種「カフェ屋」は一覧にありません", "job_types": []})
    advisor = ResumeAdvisor(dip=dip)
    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert "検索エラー" in reply


def test_propose_jobs_without_dip_degrades_gracefully():
    advisor = ResumeAdvisor(dip=None)
    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert "提案できません" in reply


ZERO = {"total": 0, "jobs": []}


def test_zero_hits_calls_relax_conditions_and_offers_concrete_options():
    """0件のとき relax_conditions を呼び、どの条件を外すと何件になるかを具体的に返す(タスク34)。

    2026-09-26 の会話の再現: エリア「東京都内」で0件のまま、AI が当てずっぽうで
    時給や職種を変えて検索し続け、一度もエリアに手を付けなかった。
    """
    dip = FakeDip(
        result=ZERO,
        relax_result={"total_now": 0, "options": [{"remove": "area", "current": "東京都内", "total_after": 5}]},
    )
    advisor = ResumeAdvisor(dip=dip)
    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ", area="東京都内", min_wage=1400))
    # 検索と同じ条件で relax_conditions が呼ばれる
    assert dip.relax_calls == [{"job_type": "カフェ", "area": "東京都内", "min_wage": 1400}]
    # 「エリアを外すと5件」のように具体的に伝わり、本人に一度で選んでもらう
    assert "エリア(東京都内)を外すと5件" in reply
    assert "選んで" in reply
    # 「探してもよいですか?」と許可を取り直させない
    assert "許可の確認" in reply and "relax" in reply


def test_zero_hits_without_relax_options_says_so_honestly():
    """どの条件を外しても増えないときは、そう伝えて当てずっぽうの再検索をさせない。"""
    dip = FakeDip(result=ZERO, relax_result={"total_now": 0, "options": []})
    advisor = ResumeAdvisor(dip=dip)
    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert dip.relax_calls == [{"job_type": "カフェ"}]
    assert "見つかりませんでした" in reply
    assert "当てずっぽう" in reply


def test_zero_hits_when_relax_conditions_fails_keeps_the_conversation_going():
    dip = FakeDip(result=ZERO, relax_error=RuntimeError("boom"))
    advisor = ResumeAdvisor(dip=dip)
    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert "見つかりませんでした" in reply
    # 調べられなかったのに「増えないことも確認済み」と嘘をつかない
    assert "確認済み" not in reply
    assert "調べられませんでした" in reply


def test_zero_hits_when_relax_conditions_returns_error_does_not_claim_checked():
    """relax_conditions が error を返したときも「確認済み」と言わず、正直に伝える。"""
    dip = FakeDip(result=ZERO, relax_result={"error": "だめでした"})
    advisor = ResumeAdvisor(dip=dip)
    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert "見つかりませんでした" in reply
    assert "確認済み" not in reply
    assert "調べられませんでした" in reply


def test_relax_drops_condition_from_search_but_keeps_the_wish():
    """ゆるめた条件は「検索のために外した」として持ち、本人の希望は書き換えない(タスク34)。"""
    dip = FakeDip()
    advisor = ResumeAdvisor(dip=dip)
    asyncio.run(advisor.propose_jobs(job_type="カフェ", area="東京都内", min_wage=1400, relax=["area"]))
    assert dip.calls == [{"job_type": "カフェ", "min_wage": 1400}]  # 検索からはエリアを外す
    assert advisor.preferences["area"] == "東京都内"  # 希望はそのまま
    assert advisor.relaxed == {"area": "東京都内"}  # 外したことが分かる
    assert advisor.recommend_args == {"job_type": "カフェ", "min_wage": 1400}


def test_relax_rejects_unknown_condition_name_without_calling_dip():
    dip = FakeDip()
    advisor = ResumeAdvisor(dip=dip)
    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ", relax=["salary"]))
    assert "引数が不正" in reply
    assert dip.calls == []


def test_new_search_without_relax_clears_the_previous_relaxed():
    dip = FakeDip()
    advisor = ResumeAdvisor(dip=dip)
    asyncio.run(advisor.propose_jobs(job_type="カフェ", area="東京都内", relax=["area"]))
    assert advisor.relaxed == {"area": "東京都内"}
    asyncio.run(advisor.propose_jobs(job_type="カフェ", area="中野"))
    assert advisor.relaxed == {}


def test_finalize_writes_draft_preferences_and_job_ids(tmp_path, monkeypatch):
    """終わると、履歴書の下書き(共通 JSON)・希望条件・提案した求人IDが out/ に出る。"""
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    draft_client = _FakeDraftClient()
    dip = FakeDip()
    advisor = ResumeAdvisor(dip=dip, draft_client_factory=lambda: draft_client)
    asyncio.run(advisor.propose_jobs(
        job_type="カフェ", area="東京都内", time_band="夕方", min_wage=1100, relax=["area"]
    ))
    advisor._transcript = lambda: [  # セッション未開始のため台本を直接差す
        {"role": "assistant", "text": "どんなお仕事を探していますか。"},
        {"role": "user", "text": "カフェの仕事を探しています。"},
    ]
    reply = asyncio.run(advisor.finalize_interview())
    assert "保存" in reply

    def find(prefix: str, suffix: str):
        return next(p for p in tmp_path.iterdir() if p.name.startswith(prefix) and p.name.endswith(suffix))

    resume = json.loads(find("resume-draft", ".json").read_text(encoding="utf-8"))
    assert resume["format_version"] == "1.1" and resume["document_kind"] == "履歴書"  # 区分が正社員でなければ履歴書
    assert resume["source"] == "voice-interview"
    assert resume["resume"]["job_summary"] == "コンビニで1年半勤務。"
    assert all(v == "" for v in resume["personal"].values())

    proposal = json.loads(find("job-proposal", ".json").read_text(encoding="utf-8"))
    # 希望はそのまま、検索のためにゆるめた条件は relaxed として残る
    assert proposal["preferences"] == {
        "job_type": "カフェ", "area": "東京都内", "time_band": "夕方", "min_wage": 1100,
    }
    assert proposal["recommend_args"] == {"job_type": "カフェ", "time_band": "夕方", "min_wage": 1100}
    assert proposal["relaxed"] == {"area": "東京都内"}
    assert proposal["proposed_job_ids"] == ["b0305", "b0304", "b0301"]

    transcript = json.loads(find("transcript", ".json").read_text(encoding="utf-8"))
    assert transcript[1]["text"] == "カフェの仕事を探しています。"


def test_propose_jobs_without_job_type_searches_without_guessing():
    """職種を決めていない人には、職種の条件を付けずに検索する(推測で埋めない)。"""
    dip = FakeDip()
    advisor = ResumeAdvisor(dip=dip)
    asyncio.run(advisor.propose_jobs(time_band="日中", min_wage=1500))
    assert dip.calls == [{"time_band": "日中", "min_wage": 1500}]
    assert "job_type" not in advisor.preferences


# 2026-09-26 の実会話の流れ: 職種は「決めてない」、時給は「まそのぐらい(1500円)」。
# 見つからず 1400円 → 下限なし+カフェ(本人の同意を得た検索条件)で検索し直した。
RELAXED_SEARCH_TRANSCRIPT = [
    {"role": "assistant", "text": "やってみたい職種は決まっていますか。"},
    {"role": "user", "text": "決めてないな。"},
    {"role": "assistant", "text": "時給1500円以上を目安にしてよいですか。"},
    {"role": "user", "text": "まそのぐらい。午後から働きたいです。"},
    {"role": "assistant", "text": "その条件では見つかりませんでした。時給1400円でも探してみますね。"},
    {"role": "user", "text": "まあいっか。一旦、時給の下限を外して。"},
    {"role": "assistant", "text": "かしこまりました。カフェのお仕事で探し直してもよいですか。"},
    {"role": "user", "text": "はい、お願いします。"},
    {"role": "assistant", "text": "求人が3件見つかりました。"},
]


def test_zero_hit_research_keeps_already_proposed_jobs(tmp_path, monkeypatch):
    """一度提案した求人の記録は、あとの0件の再検索で消えない(out/ の求人IDが空にならない)。

    提案 → 条件を足した再検索が0件 → そのまま面談終了、の順でも
    job-proposal の proposed_job_ids に最初の提案が残る。
    """
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    dip = SequenceDip([
        {"total": 3, "jobs": JOBS},
        {"total": 0, "jobs": []},
    ])
    draft_client = _FakeDraftClient()
    advisor = ResumeAdvisor(dip=dip, draft_client_factory=lambda: draft_client)

    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert "3件" in reply
    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ", min_wage=1700, search_only=True))
    assert "見つかりませんでした" in reply
    # 0件の再検索で提案済みの記録が消えない
    assert [j["job_id"] for j in advisor.proposed_jobs] == ["b0305", "b0304", "b0301"]

    advisor._transcript = lambda: RELAXED_SEARCH_TRANSCRIPT
    asyncio.run(advisor.finalize_interview())
    proposal_path = next(p for p in tmp_path.iterdir() if p.name.startswith("job-proposal"))
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert proposal["proposed_job_ids"] == ["b0305", "b0304", "b0301"]
    # 各検索で何を提案したかは search_attempts に残る
    assert [a["proposed_job_ids"] for a in proposal["search_attempts"]] == [
        ["b0305", "b0304", "b0301"], [],
    ]


def test_relaxed_search_does_not_rewrite_stated_wishes(tmp_path, monkeypatch):
    """検索のために外した・ゆるめた条件が「本人の希望」を上書きしない。

    希望は本人が言ったまま(時給1500円・職種なし)残り、
    検索の経緯(1500 → 1400 → 下限なし+カフェ)は search_attempts 側に分かれて残る。
    """
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    dip = SequenceDip([
        {"total": 0, "jobs": []},
        {"total": 0, "jobs": []},
        {"total": 3, "jobs": JOBS},
    ])
    draft_client = _FakeDraftClient()
    advisor = ResumeAdvisor(dip=dip, draft_client_factory=lambda: draft_client)

    # 本人が言った希望で検索(職種は決めていないので入れない)
    reply = asyncio.run(
        advisor.propose_jobs(time_band="日中", min_wage=1500, other_wishes="午後から働きたい")
    )
    assert "見つかりませんでした" in reply and "search_only=True" in reply
    # 検索条件だけを変えた再検索(1400円 → 下限なし+カフェ)
    asyncio.run(advisor.propose_jobs(time_band="日中", min_wage=1400, search_only=True))
    reply = asyncio.run(advisor.propose_jobs(time_band="日中", job_type="カフェ", search_only=True))
    assert "3件" in reply

    # 希望は本人が言ったまま。「下限なし」にも「カフェ」にもならない
    assert advisor.preferences == {
        "time_band": "日中", "min_wage": 1500, "other_wishes": "午後から働きたい",
    }
    # 検索条件と経緯は希望と分かれて残る
    assert advisor.recommend_args == {"time_band": "日中", "job_type": "カフェ"}
    assert [a["args"].get("min_wage") for a in advisor.search_attempts] == [1500, 1400, None]
    assert [a["found"] for a in advisor.search_attempts] == [0, 0, 3]

    advisor._transcript = lambda: RELAXED_SEARCH_TRANSCRIPT
    asyncio.run(advisor.finalize_interview())
    proposal_path = next(p for p in tmp_path.iterdir() if p.name.startswith("job-proposal"))
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    assert proposal["preferences"] == {
        "time_band": "日中", "min_wage": 1500, "other_wishes": "午後から働きたい",
    }
    assert proposal["recommend_args"] == {"time_band": "日中", "job_type": "カフェ"}
    assert proposal["search_attempts"] == [
        {"args": {"time_band": "日中", "min_wage": 1500}, "found": 0, "proposed_job_ids": []},
        {"args": {"time_band": "日中", "min_wage": 1400}, "found": 0, "proposed_job_ids": []},
        {"args": {"time_band": "日中", "job_type": "カフェ"}, "found": 3,
         "proposed_job_ids": ["b0305", "b0304", "b0301"]},
    ]
    # 下書きを作る LLM への指示に「ゆるめた条件は本人の希望ではない」の決まりが入っている
    call = draft_client.calls[0]
    assert "本人の希望ではない" in call["instructions"]
    assert "一旦、時給の下限を外して。" in call["input"][0]["content"]  # 台本はそのまま渡る


def test_finalize_without_conversation():
    advisor = ResumeAdvisor()
    assert "会話がまだありません" in asyncio.run(advisor.finalize_interview())


# ----------------------------- 待ち時間の「何をしているか」の表示(task 32) -----------------------------


class _FakeRoom:
    """_publish が data channel に送る内容を記録する(画面の代わり)。"""

    def __init__(self):
        self.published: list[dict] = []
        self.local_participant = SimpleNamespace(publish_data=self._publish)

    async def _publish(self, data: bytes, reliable=True, topic=None):
        self.published.append(json.loads(data.decode("utf-8")))


def _statuses(room: _FakeRoom) -> list[dict]:
    return [m for m in room.published if m["type"] == "status"]


def test_propose_jobs_shows_what_it_is_doing_then_clears_it():
    """求人検索の間、画面に「探しています…」が出て、終わると消える(始まり → 結果 → 終わり)。"""
    room = _FakeRoom()
    advisor = ResumeAdvisor(dip=FakeDip(), room=room)
    asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert [m["type"] for m in room.published] == ["status", "jobs", "status"]
    start, done = _statuses(room)
    assert start["label"] == "🔍 ぴったりの求人を探しています…"
    # 終わりの通知にも label が付く(どの経路の表示を消したか分かる)
    assert done == {"type": "status", "done": True, "label": start["label"]}


def test_research_shows_a_relaxed_label_and_clears_even_on_zero_hits():
    """条件を変えた再検索(relax / search_only)は「探し直しています…」。0件でも表示は消える。"""
    room = _FakeRoom()
    advisor = ResumeAdvisor(dip=FakeDip(result=ZERO), room=room)
    asyncio.run(advisor.propose_jobs(job_type="カフェ", area="東京都内", relax=["area"]))
    start, done = _statuses(room)
    assert start["label"] == "🔍 条件を変えて探し直しています…"
    assert done == {"type": "status", "done": True, "label": start["label"]}


def test_propose_jobs_failure_still_clears_the_working_status():
    """dip が落ちても(タイムアウト・接続断)、表示が残り続けない。"""
    room = _FakeRoom()
    advisor = ResumeAdvisor(dip=FakeDip(error=RuntimeError("timeout")), room=room)
    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert "失敗" in reply
    assert _statuses(room)[-1]["done"] is True


def test_invalid_args_do_not_flash_the_working_status():
    """引数の検証で即返るときは、表示を出さない(一瞬で消える表示を出さない)。"""
    room = _FakeRoom()
    advisor = ResumeAdvisor(dip=FakeDip(), room=room)
    asyncio.run(advisor.propose_jobs(job_type="喫茶店"))
    assert _statuses(room) == []


def test_web_research_shows_working_status_and_clears_even_on_failure():
    room = _FakeRoom()
    advisor = ResumeAdvisor(research_client_factory=lambda: _FakeResearchClient("答え"), room=room)
    asyncio.run(advisor.web_research(query="コンビニバイト 仕事内容"))
    assert [m["label"] for m in _statuses(room) if not m.get("done")] == ["🌐 調べています…"]
    assert _statuses(room)[-1] == {"type": "status", "done": True, "label": "🌐 調べています…"}

    room = _FakeRoom()
    advisor = ResumeAdvisor(research_client_factory=lambda: _FakeResearchClient(error=RuntimeError("boom")), room=room)
    asyncio.run(advisor.web_research(query="時給 相場"))
    assert _statuses(room)[-1]["done"] is True


def test_research_company_status_names_the_company():
    room = _FakeRoom()
    advisor = ResumeAdvisor(research_client_factory=lambda: _FakeResearchClient("答え"), room=room)
    asyncio.run(advisor.research_company(name="ナイトマート"))
    assert _statuses(room)[0]["label"] == "🏢 ナイトマートのことを調べています…"


def test_wage_average_from_reference_does_not_flash_the_working_status():
    """全国・東京は参考データから一瞬で返るので、表示を出さない(ちらつかせない)。web で調べるときだけ出す。"""
    room = _FakeRoom()
    client = _FakeResearchClient("大阪府の平均時給は1100円ほどです。")
    advisor = ResumeAdvisor(research_client_factory=lambda: client, wage_reference=REF, room=room)
    asyncio.run(advisor.wage_average(area="東京"))
    assert _statuses(room) == []
    asyncio.run(advisor.wage_average(area="大阪府"))
    assert _statuses(room)[0]["label"] == "🌐 平均時給を調べています…"
    assert _statuses(room)[-1] == {"type": "status", "done": True, "label": "🌐 平均時給を調べています…"}


def test_voice_sheet_shows_and_clears_the_working_status():
    """トークの音声相談のシート(line_runtime/static/talk-core.js)が status を受けて表示を出し、done で消す。"""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "line_runtime" / "static" / "talk-core.js").read_text(encoding="utf-8")
    assert 'msg.type === "status"' in js
    assert "v.working = msg.done ? null : msg.label" in js
    assert 'topic !== "voice-resume"' in js  # advisor の DATA_TOPIC と同じ


# ----------------------------- LINE のトークから入った相談(task 40 の統合) -----------------------------


class _FakePoster:
    """トークへの結果送信(_deliver_to_talk)のスタブ。"""

    def __init__(self, error: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.error = error

    async def __call__(self, url: str, payload: dict) -> None:
        if self.error:
            raise self.error
        self.calls.append((url, payload))


def _talk_advisor(dip, poster=None, stated=None, tmp_out=None, monkeypatch=None):
    if tmp_out is not None:
        monkeypatch.setattr(config, "OUT_DIR", tmp_out)
    return ResumeAdvisor(
        dip=dip, user_id="sim-new", stated_preferences=stated,
        talk_poster=poster or _FakePoster(),
        draft_client_factory=_FakeDraftClient,
    )


def test_propose_jobs_with_line_user_saves_stated_wishes_and_searches_as_that_user():
    """LINE から入った相談では、本人が言った希望が dip に保存され、検索も本人のIDで行われる。"""
    dip = FakeDip()
    advisor = _talk_advisor(dip)
    asyncio.run(advisor.propose_jobs(
        job_type="カフェ", area="東京都内", time_band="夕方", min_wage=1400,
        days_per_week=3, experience="コンビニで1年半", skills="レジ打ちと品出し",
        certifications="普通自動車免許", other_wishes="家から近い店がいい",
    ))
    # 検索は本人の user_id つき(本人の傾向・応募済み除外が効く)。
    # experience / skills / certifications は検索条件には入れない(プロフィールとして保存するだけ)
    assert dip.calls == [{
        "line_user_id": "sim-new", "job_type": "カフェ", "area": "東京都内",
        "time_band": "夕方", "min_wage": 1400,
    }]
    # 保存されるのは dip の項目(area / time_band / job_type / min_wage / experience /
    # days_per_week / skills / certifications)だけ。other_wishes は dip の項目に無いので保存しない
    assert dip.save_calls == [{
        "line_user_id": "sim-new",
        "preferences": {"job_type": "カフェ", "area": "東京都内", "time_band": "夕方",
                        "min_wage": 1400, "days_per_week": 3, "experience": "コンビニで1年半",
                        "skills": "レジ打ちと品出し", "certifications": "普通自動車免許"},
    }]


def test_relaxed_or_guessed_search_conditions_are_not_saved_as_wishes():
    """検索のためにゆるめた条件や、検索のためだけの職種(search_only)は希望として保存されない。"""
    dip = FakeDip()
    advisor = _talk_advisor(dip)
    # 本人が言った希望(時給1500・日中)。エリアは検索のために外す(relax)
    asyncio.run(advisor.propose_jobs(area="東京都内", time_band="日中", min_wage=1500, relax=["area"]))
    # 保存されるのは本人が言った値のまま(外したことは保存に影響しない)
    assert dip.save_calls == [{
        "line_user_id": "sim-new",
        "preferences": {"area": "東京都内", "time_band": "日中", "min_wage": 1500},
    }]
    # 見つからず、本人の同意を得て検索条件だけを変える(時給を下げ、職種カフェで試す)
    asyncio.run(advisor.propose_jobs(time_band="日中", min_wage=1400, job_type="カフェ", search_only=True))
    # search_only の条件(下げた時給・試した職種)は希望として保存されない
    assert len(dip.save_calls) == 1
    assert advisor.preferences == {"area": "東京都内", "time_band": "日中", "min_wage": 1500}


def test_standalone_advisor_never_writes_to_dip(tmp_path, monkeypatch):
    """単体(ユーザー不明)の相談では、従来どおり dip に何も書かない(task 31 の決まりのまま)。"""
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)
    dip = FakeDip()
    poster = _FakePoster()
    advisor = ResumeAdvisor(dip=dip, draft_client_factory=_FakeDraftClient, talk_poster=poster)
    asyncio.run(advisor.propose_jobs(job_type="カフェ", min_wage=1100))
    advisor._transcript = lambda: RELAXED_SEARCH_TRANSCRIPT
    asyncio.run(advisor.finalize_interview())
    assert dip.save_calls == [] and dip.outcome_calls == []
    assert poster.calls == [] and advisor.delivered_to_talk is False
    assert "line_user_id" not in dip.calls[0]


def test_finalize_records_propose_and_returns_the_result_to_the_talk(tmp_path, monkeypatch):
    """終わると、結果が propose で記録され、求人カードと下書きがトークへ戻る。"""
    dip = FakeDip()
    poster = _FakePoster()
    advisor = _talk_advisor(dip, poster=poster, tmp_out=tmp_path, monkeypatch=monkeypatch)
    asyncio.run(advisor.propose_jobs(job_type="カフェ", min_wage=1100))
    advisor._transcript = lambda: RELAXED_SEARCH_TRANSCRIPT
    reply = asyncio.run(advisor.finalize_interview())

    # 記録は propose(求人を提案した)。wait は決して記録しない(断っていない人を wait にしない)
    assert [c["outcome"] for c in dip.outcome_calls] == ["propose"]
    assert "音声相談" in dip.outcome_calls[0]["reason"]
    # トークへ戻す中身: dip が返した求人カードのまま + 下書きの Markdown
    (url, payload), = poster.calls
    assert url.endswith("/api/voice/result")
    assert payload["user_id"] == "sim-new"
    assert [j["job_id"] for j in payload["jobs"]] == ["b0305", "b0304", "b0301"]
    assert payload["draft_markdown"].startswith("#")
    # 話した希望も戻す(トークに出す「話した内容のまとめ」)。検索のためにゆるめた条件ではなく本人の希望
    assert payload["preferences"] == {"job_type": "カフェ", "min_wage": 1100}
    assert advisor.delivered_to_talk is True
    assert "トークに送りました" in reply


def test_finalize_twice_saves_and_records_only_once(tmp_path, monkeypatch):
    """締めの後に「面談を終える」ボタンなどで finalize がもう一度呼ばれても、
    out/ への追記・結果の記録・トークへの送信は1回分のまま(2回目は前回の結果を返すだけ)。"""
    dip = FakeDip()
    poster = _FakePoster()
    advisor = _talk_advisor(dip, poster=poster, tmp_out=tmp_path, monkeypatch=monkeypatch)
    asyncio.run(advisor.propose_jobs(job_type="カフェ", min_wage=1100))
    advisor._transcript = lambda: RELAXED_SEARCH_TRANSCRIPT
    first = asyncio.run(advisor.finalize_interview())
    files_after_first = sorted(p.name for p in tmp_path.iterdir())

    second = asyncio.run(advisor.finalize_interview())
    # 副作用は1回分のまま: 結果の記録・トークへの送信・out/ のファイル
    assert len(dip.outcome_calls) == 1
    assert len(poster.calls) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == files_after_first
    # 2回目は保存済みであることを伝えつつ、前回の結果(締めの案内)を返す
    assert "2回目の保存はしていません" in second
    assert first in second


def test_finalize_without_proposals_records_consult(tmp_path, monkeypatch):
    dip = FakeDip(result=ZERO)
    advisor = _talk_advisor(dip, tmp_out=tmp_path, monkeypatch=monkeypatch)
    asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    advisor._transcript = lambda: RELAXED_SEARCH_TRANSCRIPT
    asyncio.run(advisor.finalize_interview())
    assert [c["outcome"] for c in dip.outcome_calls] == ["consult"]


def test_finalize_keeps_the_draft_when_the_talk_delivery_fails(tmp_path, monkeypatch):
    """トーク(line_runtime)が落ちていても、下書きは out/ に残り、面談は締められる。"""
    dip = FakeDip()
    poster = _FakePoster(error=RuntimeError("connection refused"))
    advisor = _talk_advisor(dip, poster=poster, tmp_out=tmp_path, monkeypatch=monkeypatch)
    asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    advisor._transcript = lambda: RELAXED_SEARCH_TRANSCRIPT
    reply = asyncio.run(advisor.finalize_interview())
    assert "保存" in reply
    assert advisor.delivered_to_talk is False
    assert next(p for p in tmp_path.iterdir() if p.name.startswith("resume-draft"))


def test_stated_preferences_enter_the_instructions_and_forbid_reasking():
    """前に話した希望があるユーザーでは、指示に希望が入り「聞き直さない」が明記される。"""
    stated = {"job_type": {"value": "カフェ", "saved": "9/23"}, "area": {"value": "渋谷", "saved": "9/23"}}
    advisor = ResumeAdvisor(user_id="sim-new", stated_preferences=stated)
    assert "カフェ" in advisor.instructions and "渋谷" in advisor.instructions
    assert "確認形で1回だけ" in advisor.instructions
    assert "聞き直さない" in advisor.instructions
    # 希望が無いときは何も足さない(従来の指示のまま)
    assert ResumeAdvisor().instructions == ResumeAdvisor(stated_preferences={}).instructions


class _FakeResearchClient:
    """web_research が呼ぶ responses.create のスタブ。"""

    def __init__(self, text: str = "", error: Exception | None = None):
        self.responses = SimpleNamespace(create=self._create)
        self.calls: list[dict] = []
        self.text = text
        self.error = error

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(output_text=self.text)


def test_web_research_searches_the_web_and_returns_a_short_answer():
    client = _FakeResearchClient(
        "コンビニのバイトは、レジ、品出し、清掃が主な仕事です。([ptj.sej.co.jp](https://ptj.sej.co.jp/x?utm_source=openai)) 詳しくは https://example.com へ"
    )
    advisor = ResumeAdvisor(research_client_factory=lambda: client)
    reply = asyncio.run(advisor.web_research(query="コンビニバイト 仕事内容"))
    assert "レジ、品出し" in reply and "続き" in reply
    assert "http" not in reply and "](" not in reply  # 出典リンクや URL は読み上げない
    call = client.calls[0]
    assert call["model"] == config.RESEARCH_MODEL
    assert call["tools"][0]["type"] == "web_search"
    assert "コンビニバイト 仕事内容" in call["input"]


def test_web_research_failure_keeps_the_conversation_going():
    advisor = ResumeAdvisor(research_client_factory=lambda: _FakeResearchClient(error=RuntimeError("timeout")))
    assert "失敗" in asyncio.run(advisor.web_research(query="時給 相場 新宿"))
    advisor = ResumeAdvisor(research_client_factory=lambda: _FakeResearchClient(""))
    assert "分かりませんでした" in asyncio.run(advisor.web_research(query="時給 相場 新宿"))


def test_web_searches_stop_at_the_limit_across_tools(monkeypatch):
    """1 回の相談で web を調べるのは config.MAX_WEB_SEARCHES 回まで(3 つのツールの合計。失敗した回も数える)。
    上限を超えたら調べず、「調べています…」も出さず、調べられない旨を伝えさせる(セキュリティ監査 run-1 の 4 番)。"""
    assert config.MAX_WEB_SEARCHES == 5
    monkeypatch.setattr(config, "MAX_WEB_SEARCHES", 3)
    room = _FakeRoom()
    client = _FakeResearchClient("答え")
    failing = _FakeResearchClient(error=RuntimeError("timeout"))
    clients = iter([client, failing, client])
    advisor = ResumeAdvisor(research_client_factory=lambda: next(clients, client), wage_reference=REF, room=room)
    asyncio.run(advisor.web_research(query="コンビニバイト 仕事内容"))
    assert "失敗" in asyncio.run(advisor.wage_average(area="大阪府"))
    asyncio.run(advisor.research_company(name="ナイトマート"))
    assert advisor.web_searches == 3
    published = len(room.published)

    for call in (lambda: advisor.web_research(query="資格"), lambda: advisor.wage_average(area="福岡県"),
                 lambda: advisor.research_company(name="カフェA")):
        reply = asyncio.run(call())
        assert "使い切りました" in reply and "3回" in reply
    assert len(client.calls) + len(failing.calls) == 3  # 4 回目からは調べていない
    assert len(room.published) == published  # 表示も出さない
    # 参考データから答える全国・東京は web を使わないので、上限の後も答えられる
    assert "全国で1303円" in asyncio.run(advisor.wage_average(area="東京"))


def test_research_with_personal_values_is_refused_without_searching():
    """調べものの問いに電話番号・メールアドレス・住所らしい所があれば、web で調べずに断る(問いは OpenAI を通って
    検索エンジンにも届くので。docs/design/privacy.md の 3.2)。表示も出さず、回数にも数えない。値は架空。"""
    room = _FakeRoom()
    client = _FakeResearchClient("答え")
    advisor = ResumeAdvisor(research_client_factory=lambda: client, wage_reference=REF, room=room)
    for call in (lambda: advisor.web_research(query="090-1234-5678 だれの番号"),
                 lambda: advisor.web_research(query="sample.user@example.com の持ち主"),
                 lambda: advisor.research_company(name="ナイトマート", hint="自宅のみほん市みほん町1丁目2-3の近くの店"),
                 lambda: advisor.wage_average(area="みほん市3-5に住んでいる", job_type="コンビニ")):
        assert asyncio.run(call()) == PERSONAL_QUERY_REPLY
    assert client.calls == [] and room.published == [] and advisor.web_searches == 0
    # ふつうの問い(会社名・エリア・職種)はそのまま調べる
    asyncio.run(advisor.research_company(name="ナイトマート", hint="中野のコンビニ"))
    asyncio.run(advisor.web_research(query="コンビニバイト 時給 2026年"))
    # 住む言葉の無い店の所在地は住所とみなさない(task 211)
    asyncio.run(advisor.research_company(name="ナイトマート", hint="みほん市みほん町1丁目2-3の店"))
    assert len(client.calls) == 3 and advisor.web_searches == 3


REF = WageReference(
    survey="マイナビ アルバイト・パート平均時給レポート", period="2026年8月", source="マイナビキャリアリサーチLab",
    entries=[WageEntry(area="全国", average_wage=1303), WageEntry(area="東京都", average_wage=1428)],
)


def test_wage_average_for_nation_and_tokyo_comes_from_the_reference_without_searching():
    """全国・東京の平均時給は参考データからすぐ答える(会話のたびに web で調べない)。"""
    client = _FakeResearchClient("使われないはず")
    advisor = ResumeAdvisor(research_client_factory=lambda: client, wage_reference=REF)
    for area in (None, "全国", "東京", "東京都"):
        reply = asyncio.run(advisor.wage_average(area=area))
        assert "全国で1303円" in reply and "東京都で1428円" in reply and "2026年8月" in reply
    assert client.calls == []


def test_wage_average_for_other_area_or_job_type_searches_the_web():
    client = _FakeResearchClient("大阪府のコンビニの平均時給は1100円ほどです。")
    advisor = ResumeAdvisor(research_client_factory=lambda: client, wage_reference=REF)
    assert "1100円" in asyncio.run(advisor.wage_average(area="大阪府", job_type="コンビニ"))
    assert "大阪府" in client.calls[0]["input"] and "コンビニ" in client.calls[0]["input"]
    asyncio.run(advisor.wage_average(area="東京", job_type="カフェ"))  # 職種つきは参考データに無い
    assert len(client.calls) == 2


def test_wage_reference_round_trips_and_is_checked_in():
    """保存した参考データを読み戻せる。リポジトリに入っている参考データも読める。"""
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp()) / "wage_reference.json"
    save_reference(REF, path)
    assert load_reference(path) == REF
    checked_in = load_reference()
    assert checked_in is not None and {e.area for e in checked_in.entries} == {"全国", "東京都"}


def test_fetch_reference_uses_web_search_and_stamps_the_date():
    captured = {}

    async def parse(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(output_parsed=REF.model_copy(update={"source": "マイナビ ([m.jp](https://m.jp/x.pdf))"}))

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    ref = asyncio.run(fetch_reference(client, "gpt-6-sol", "2026-09-26"))
    assert ref.fetched_at == "2026-09-26"
    assert ref.source == "マイナビ"  # 読み上げる情報源にリンクを残さない
    assert captured["tools"] == [{"type": "web_search"}] and captured["text_format"] is WageReference


def test_research_company_passes_the_hint_and_asks_not_to_guess():
    client = _FakeResearchClient("コンビニチェーンで、全国に約2万店あります。出典は公式サイトです。")
    advisor = ResumeAdvisor(research_client_factory=lambda: client)
    reply = asyncio.run(advisor.research_company(name="セブンイレブン", hint="コンビニチェーン"))
    assert "2万店" in reply
    prompt = client.calls[0]["input"]
    assert "セブンイレブン" in prompt and "コンビニチェーン" in prompt and "推測で埋めず" in prompt


def test_research_company_failure_keeps_the_conversation_going():
    advisor = ResumeAdvisor(research_client_factory=lambda: _FakeResearchClient(error=RuntimeError("timeout")))
    assert "失敗" in asyncio.run(advisor.research_company(name="ナイトマート 中野五差路店"))


STYLE = {"remote": "可", "weekends_holidays": True, "max_overtime_hours": 0}
ATTRS = {"max_talk_level": 1, "min_physical_level": 3}


def test_propose_jobs_passes_work_style_only_in_a_fulltime_interview():
    """正社員の音声でも働き方・休日(remote / weekends_holidays / max_overtime_hours)で絞る。
    負例: アルバイト・パートの面談では渡さない(アルバイト・パートの求人には働き方の情報が無く、渡すと全部外れる)。
    属性(人と話す量・体を使う量)はどちらの区分でも渡す。"""
    fulltime = FakeDip()
    asyncio.run(ResumeAdvisor(dip=fulltime, track="正社員").propose_jobs(
        job_type="営業", min_annual_salary=400, **STYLE, **ATTRS))
    assert fulltime.calls[0].items() >= {**STYLE, **ATTRS}.items()

    parttime = FakeDip()
    asyncio.run(ResumeAdvisor(dip=parttime, track="アルバイト・パート").propose_jobs(
        job_type="カフェ", **STYLE, **ATTRS))
    assert parttime.calls[0].items() >= ATTRS.items()
    assert not set(STYLE) & set(parttime.calls[0])

    unasked = FakeDip()  # 言っていない働き方・休日は正社員でも付けない
    asyncio.run(ResumeAdvisor(dip=unasked, track="正社員").propose_jobs(job_type="営業", weekends_holidays=False))
    assert not set(STYLE) & set(unasked.calls[0])


def test_attribute_and_style_args_are_not_saved_as_preferences():
    """属性と働き方・休日は検索だけの引数: save_user_preferences にも、記録する希望にも入らない。
    負例: 一緒に言った職種と年収の下限は保存する。"""
    dip = FakeDip()
    advisor = ResumeAdvisor(dip=dip, track="正社員", user_id="u-test")
    asyncio.run(advisor.propose_jobs(job_type="営業", min_annual_salary=400, **STYLE, **ATTRS))
    assert dip.calls and dip.save_calls
    saved = {k for c in dip.save_calls for k in c["preferences"]}
    assert "job_type" in saved and "min_annual_salary" in saved
    assert not saved & {*STYLE, *ATTRS}
    assert not set(advisor.preferences) & {*STYLE, *ATTRS}


def test_excluded_jobs_are_searched_out_and_remembered_for_later_searches(tmp_path, monkeypatch):
    """exclude_ids(出さない求人)は dip の検索に渡り、advisor が覚えて後の検索(0件のときの relax_conditions も)に
    合わせて渡す。検索の条件(recommend_args)には入れず、検索の経緯(search_attempts)に残す。
    0件の探し直しでは前の提案をそのまま残す(ユーザーの決定、2026-10-04)。"""
    dip = FakeDip()
    advisor = _talk_advisor(dip, tmp_out=tmp_path, monkeypatch=monkeypatch)

    reply = asyncio.run(advisor.propose_jobs(job_type="カフェ"))
    assert "1件目(求人ID b0305)" in reply and "exclude_ids" in reply
    dip.result = {"total": 0, "jobs": []}
    asyncio.run(advisor.propose_jobs(job_type="カフェ", exclude_ids=["b0304", "b0304"], search_only=True))
    # 後で条件を変えて、exclude_ids を付けずに探しても、外した求人は dip に渡る(新しく外した分は足す)
    asyncio.run(advisor.propose_jobs(job_type="カフェ", area="中野", search_only=True))
    asyncio.run(advisor.propose_jobs(job_type="カフェ", exclude_ids=["b0301"], search_only=True))

    assert "exclude_ids" not in dip.calls[0] and dip.calls[1]["exclude_ids"] == ["b0304"]
    assert dip.calls[2]["exclude_ids"] == ["b0304"] and dip.calls[3]["exclude_ids"] == ["b0304", "b0301"]
    assert [c["exclude_ids"] for c in dip.relax_calls] == [["b0304"], ["b0304"], ["b0304", "b0301"]]
    assert advisor.recommend_args == {"job_type": "カフェ"}
    assert [a.get("exclude_ids") for a in advisor.search_attempts] == [None, ["b0304"], ["b0304"], ["b0304", "b0301"]]
    # 0件の探し直しでは、前の提案(外した求人を含む)を変えない
    assert [j["job_id"] for j in advisor.proposed_jobs] == [j["job_id"] for j in JOBS[:3]]
