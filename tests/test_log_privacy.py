"""ログに値を出さない決まり(docs/design/privacy.md の 3.4)のテスト。実 API なし。

架空の目印の値(住所と電話番号)を、本人の発言、前に話した希望、文字起こし、API のエラーの文に入れて流れを動かし、
ログにその値が出ないことを確かめる。ログに出してよいのは、ユーザーID、種類、件数、有無、例外の型名だけ。
本番では SDK のロガー(livekit、openai、httpx など)を WARNING にし、INFO で出る本文もログに残さない。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid

import pydantic
import pytest
from fastapi.testclient import TestClient

from line_runtime import line_channel
from line_runtime.log_policy import quiet_sdk_loggers
from tests.test_line_webhook import _event, _post, _server, line_id_key  # noqa: F401 - line_id_key は autouse
from tests.test_revisit import Greeter
from tests.voice.test_advisor import JOBS, RELAXED_SEARCH_TRANSCRIPT
from voice_resume import agent, config
from voice_resume.advisor import ResumeAdvisor

# 架空の目印の値。実在の住所や番号ではない
ADDRESS = "みほん市みほん町1-2-3"
PHONE = "090-0000-0000"
MARKS = (ADDRESS, PHONE)
SAID = f"住所は{ADDRESS}、電話は{PHONE}です"  # 本人の発言(トークでも音声でも)


def api_error() -> RuntimeError:
    """送った本文の一部が文に入った API のエラー(3.4 の推測。各社の文書では確かめていない)。"""
    return RuntimeError(f"Error code: 400 - invalid input: '{SAID}'")


def assert_no_marks(caplog) -> None:
    text = caplog.text  # トレースバック(logger.exception)の最後の行も含む
    for mark in MARKS:
        assert mark not in text, f"ログに目印の値が出た: {mark}"


# livekit-agents(本体とプラグイン)、OpenAI と Anthropic の SDK、HTTP のクライアント、会話の保存(aiosqlite は DEBUG で
# 会話の中身を出す)
SDK_LOGGERS = ("livekit", "livekit.agents", "livekit.plugins.openai", "livekit.plugins.deepgram",
               "livekit.plugins.cartesia", "openai", "anthropic", "httpx", "httpcore", "aiosqlite")


@pytest.fixture
def clean_logging():
    """ロガーのレベル、root のハンドラー、livekit-agents のプラグインの登録を、テストの後に元に戻す
    (livekit-agents の setup_logging は root にハンドラーを、プラグインの登録に受け手を足す)。"""
    from livekit.agents.plugin import Plugin

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    names = [n for n in logging.Logger.manager.loggerDict] + list(SDK_LOGGERS)
    levels = {n: logging.getLogger(n).level for n in names}
    plugins = list(Plugin.registered_plugins)
    listeners = dict(Plugin.emitter._events.get("plugin_registered", {}))
    yield
    for h in root.handlers[:]:
        if h not in handlers:
            root.removeHandler(h)
    root.setLevel(level)
    for n, lv in levels.items():
        logging.getLogger(n).setLevel(lv)
    Plugin.registered_plugins[:] = plugins
    Plugin.emitter._events["plugin_registered"] = listeners


@pytest.fixture
def logs(clean_logging, caplog):
    """本番と同じく SDK のロガーを WARNING にして、ほかのログ(aica のもの)は DEBUG まですべてとる。"""
    quiet_sdk_loggers()
    caplog.set_level(logging.DEBUG)
    return caplog


# ----------------------------- 音声相談 -----------------------------


class LeakyDip:
    """要約に目印の値の入った希望を返し、書き込みと検索はすべて、目印の値の入った文で失敗する dip。"""

    def __init__(self, fail_summary: bool = False):
        self.fail_summary = fail_summary

    async def get_user_conversation_summary(self, user_id: str) -> dict:
        if self.fail_summary:
            raise api_error()
        return {"stated_preferences": {"area": {"value": ADDRESS, "saved": "10/25"},
                                       "concerns": {"value": f"{PHONE}に連絡がほしい", "saved": "10/25"}},
                "consultation_summary": {"summary": SAID}, "search_track": "アルバイト・パート"}

    async def get_counseling_playbook(self, track: str, channel: str) -> dict:
        raise api_error()

    async def set_search_track(self, user_id: str, track: str, evidence: str) -> dict:
        raise api_error()

    async def recommend_jobs(self, **args) -> dict:
        raise api_error()

    async def relax_conditions(self, **args) -> dict:
        raise api_error()

    async def save_user_preferences(self, user_id: str, preferences: dict) -> dict:
        raise api_error()

    async def record_conversation_outcome(self, *args, **kwargs) -> dict:
        raise api_error()


class ZeroHitDip(LeakyDip):
    """検索は 0 件を返し、0 件のときの relax_conditions が失敗する。"""

    async def recommend_jobs(self, **args) -> dict:
        return {"total": 0, "jobs": []}


class FoundDip(LeakyDip):
    async def recommend_jobs(self, **args) -> dict:
        return {"total": len(JOBS), "jobs": JOBS}


class _FailingClient:
    """drafter の responses.parse と web 検索の responses.create が、目印の値の入った例外で失敗する OpenAI の代わり。
    下書きの失敗は、モデルの出力が input_value に入る pydantic の ValidationError(3.4 の advisor.py:515 の行)。"""

    def __init__(self):
        from types import SimpleNamespace

        self.responses = SimpleNamespace(parse=self._parse, create=self._create)

    async def _parse(self, **kwargs):
        pydantic.TypeAdapter(int).validate_python(SAID)  # ValidationError(input_value='住所は…')

    async def _create(self, **kwargs):
        raise api_error()


class _FailingRoom:
    """画面へのデータの送信が失敗する部屋。"""

    class local_participant:  # livekit の Room の属性の形に合わせる
        @staticmethod
        async def publish_data(*args, **kwargs):
            raise api_error()


async def _failing_poster(url: str, payload: dict) -> None:
    raise api_error()


def test_voice_start_logs_only_whether_the_stated_wishes_exist(logs):
    """面談の開始のログは、前回の希望を「あり / なし」と項目の数にする(値は出さない)。"""
    advisor, _ = asyncio.run(agent.prepare_advisor(LeakyDip(), "w-1111111111111111"))
    assert advisor  # 希望が読めたので、指示には入る(ログに出さないだけ)
    started = [r.getMessage() for r in logs.records if "面談を開始" in r.getMessage()]
    assert started and "前回の希望=あり(2 項目)" in started[0]
    assert_no_marks(logs)


def test_voice_start_logs_only_the_type_of_a_failed_summary(logs):
    asyncio.run(agent.prepare_advisor(LeakyDip(fail_summary=True), "w-1111111111111111"))
    assert "RuntimeError" in logs.text
    assert_no_marks(logs)


def test_voice_tool_failures_log_only_the_exception_type(tmp_path, monkeypatch, logs):
    """dip、web 検索、画面、トークへの送信、下書きが、目印の値の入った文で失敗しても、ログには型名だけが出る。"""
    monkeypatch.setattr(config, "OUT_DIR", tmp_path)

    def advisor(dip) -> ResumeAdvisor:
        return ResumeAdvisor(dip=dip, user_id="w-1111111111111111", room=_FailingRoom(),
                             talk_poster=_failing_poster, draft_client_factory=_FailingClient,
                             research_client_factory=_FailingClient)

    async def run() -> list[str]:
        a = advisor(LeakyDip())
        replies = [
            await a.set_search_track("正社員", SAID),  # dip の記録が失敗
            await a.propose_jobs(area=ADDRESS, other_wishes=SAID),  # 希望の保存と検索が失敗
            await a.web_research(SAID),  # web 検索が失敗
        ]
        z = advisor(ZeroHitDip())
        replies.append(await z.propose_jobs(area=ADDRESS))  # 0 件の relax_conditions が失敗
        a._transcript = lambda: [*RELAXED_SEARCH_TRANSCRIPT, {"role": "user", "text": SAID}]
        replies.append(await a.finalize_interview(summary=SAID))  # 下書き(ValidationError)が失敗
        f = ResumeAdvisor(dip=FoundDip(), user_id="w-1111111111111111", room=_FailingRoom(),
                          talk_poster=_failing_poster, draft_client_factory=_DraftOk)
        await f.propose_jobs(area="渋谷")
        f._transcript = lambda: [*RELAXED_SEARCH_TRANSCRIPT, {"role": "user", "text": SAID}]
        replies.append(await f.finalize_interview(summary="カフェで働きたい"))  # トークへの送信と結果の記録が失敗
        return replies

    replies = asyncio.run(run())
    assert "RuntimeError" in logs.text and "ValidationError" in logs.text
    assert_no_marks(logs)
    # 下書きの失敗の文(ツールの結果。LLM と会話の履歴に入る)にも、例外の文を入れない
    assert replies[4] == "下書きの保存に失敗しました。その旨を伝えて謝ってください。"


class _DraftOk:
    """下書きは作れる drafter の代わり(トークへの送信と結果の記録の失敗を確かめる)。"""

    def __init__(self):
        from types import SimpleNamespace

        from resume_draft.schema import ResumeDraft

        async def parse(**kwargs):
            return SimpleNamespace(output_parsed=ResumeDraft(job_summary="コンビニで1年半勤務。"))

        self.responses = SimpleNamespace(parse=parse)


def test_voice_room_report_failure_logs_only_the_type(logs):
    assert asyncio.run(agent.report_session("room-1", "w-1111111111111111", "joined", poster=_failing_poster))
    assert "RuntimeError" in logs.text
    assert_no_marks(logs)


# ----------------------------- LINE のトーク -----------------------------


def test_failed_line_turn_logs_only_the_exception_type(tmp_path, monkeypatch, logs):
    """LLM の API のエラーの文に本人の発言が入っていても、LINE のターンの失敗のログには型名だけが出る。"""

    def broken(messages):
        raise RuntimeError(f"Error code: 400 - invalid input: '{messages[-1].content}'")

    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[broken]))
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text=SAID)]).status_code == 200
    ((_, body),) = fake.sent()
    assert body["messages"] == [{"type": "text", "text": line_channel.FAILED_TEXT}]
    assert "LINE のターンが失敗しました" in logs.text and "RuntimeError" in logs.text
    assert_no_marks(logs)


def test_failed_line_event_logs_only_the_exception_type(tmp_path, monkeypatch, logs):
    """イベントの処理が思わぬ例外で止まっても、トレースバックの例外の文(本人の発言を含みうる)をログに出さない。"""
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))

    async def boom(uid, kind, e):
        raise RuntimeError(f"unexpected: {e['message']['text']}")

    monkeypatch.setattr(server, "_line_turn", boom)
    with TestClient(server.app) as client:
        assert _post(client, [_event("message", text=SAID)]).status_code == 200
    ((_, body),) = fake.sent()
    assert body["messages"] == [{"type": "text", "text": line_channel.FAILED_TEXT}]
    assert "LINE のイベントを処理できませんでした" in logs.text and "RuntimeError" in logs.text
    assert_no_marks(logs)


def test_failed_liff_upload_turn_logs_only_the_exception_type(tmp_path, monkeypatch, logs):
    """LIFF 画面で履歴書を読み込んだ後の続きのターンが思わぬ例外で止まっても、例外の文をログに出さず、確認と失敗の文を届ける。"""
    server, fake = _server(tmp_path, monkeypatch, Greeter(script=[]))

    async def boom(uid, kind, e):
        raise RuntimeError(f"unexpected: {SAID}")

    monkeypatch.setattr(server, "_line_turn", boom)
    with TestClient(server.app) as client:
        client.portal.call(server._line_upload_turn, "l-1", "U1", {"bubbles": ["読み取りました"]})
    ((_, body),) = fake.sent()
    assert [m["text"] for m in body["messages"]] == ["読み取りました", line_channel.FAILED_TEXT]
    assert "履歴書の続きのターンを進められませんでした" in logs.text and "RuntimeError" in logs.text
    assert_no_marks(logs)


# ----------------------------- 本番の SDK のロガー -----------------------------

class _LatePlugin:
    """setup_logging の後で登録する、livekit-agents のプラグインの代わり。ロガーは登録の直前に初めて作る
    (先にあると quiet_sdk_loggers が WARNING にしてしまい、登録のときの扱いを確かめられない)。"""

    def __init__(self):
        self.title = self.version = self.package = f"livekit.plugins.aica_late_{uuid.uuid4().hex}"
        self.logger = logging.getLogger(self.title)


def _worker_log(argv: list[str], monkeypatch) -> str:
    """音声ワーカーを argv で起動したときと同じ順にログを設定し(agent.configure_logging の後に、livekit-agents の
    CLI が setup_logging で root と自分のロガーを決める)、各 SDK のロガーで目印の値を INFO で出して、出たメッセージを返す。"""
    from livekit.agents.cli.log import setup_logging
    from livekit.agents.plugin import Plugin

    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    agent.configure_logging(argv)
    setup_logging("INFO", devmode=False, console=False)  # start の既定(本番の JSON のログ)
    # 後から読み込むプラグイン(Deepgram の STT はジョブの中で読む)。livekit-agents は登録のときにロガーを INFO にする
    late = _LatePlugin()
    Plugin.register_plugin(late)
    for name in (*SDK_LOGGERS, late.title):
        logging.getLogger(name).info("発話: %s", SAID)
    logging.getLogger("voice-resume.agent").info("面談を開始: user_id=w-1")
    logging.getLogger("openai").warning("Retrying request")
    return "\n".join(json.loads(line)["message"] for line in out.getvalue().splitlines())


def test_production_worker_keeps_sdk_info_logs_out(clean_logging, monkeypatch):
    """本番(start)では SDK のロガーが WARNING 以上になり、INFO の本文は出ない。aica のログと SDK の警告は出る。"""
    text = _worker_log(["start"], monkeypatch)
    for mark in MARKS:
        assert mark not in text
    assert "面談を開始" in text and "Retrying request" in text


def test_dev_worker_keeps_sdk_info_logs(clean_logging, monkeypatch):
    """手元の dev は livekit-agents の既定のまま(SDK の INFO が出る)。上のテストが出力を読めていることの確かめでもある。"""
    text = _worker_log(["dev"], monkeypatch)
    assert ADDRESS in text


def test_web_server_quiets_sdk_loggers(clean_logging, monkeypatch):
    """Web プロセス(LLM と LINE の API を呼ぶ)も、起動で SDK のロガーを WARNING 以上にする。"""
    import uvicorn

    from line_runtime import server

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    server.main()
    for name in SDK_LOGGERS:
        assert not logging.getLogger(name).isEnabledFor(logging.INFO), name
        assert logging.getLogger(name).isEnabledFor(logging.WARNING), name
