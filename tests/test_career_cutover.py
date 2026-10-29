"""本番の切り替え: 正社員だけを c1 から c2 へ(設計メモ docs/design/conversation-layer.md §4.15、§5 の 10 枚目。task 363)。

本番の組み合わせ(CAREER_HARNESS_FORM=D、BAITO_HARNESS_VERSION=v4、TALK_LAYER_VERSION なし)で、運営者が
CAREER_HARNESS_VERSION を変えたときの動きを、LINE の Webhook と本物のランタイムと dip の MCP で通す(偽の LINE、偽のモデル。実 LLM なし)。
- 切り替える前に始めた c1 の相談は、途中で c2 にしても c1 のまま完成まで進む。次に始める相談は c2。戻す(c2 → c1)ときも同じ
- c2 の相談から、まだ古い版(v4)のアルバイト・パートへ移るときは、トークの AI がそのターンを受けて v4 を始める。v4 は引き継ぎの記録を読まない
- v4 の相談の「やっぱり正社員で探したい」も、今と同じくトークの AI を通り、正社員の相談は c2 で始まる
"""

from __future__ import annotations

from functools import partial

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from dip_mcp.baito import session as baito_session
from dip_mcp.career import harness as H
from dip_mcp.career import session
from dip_mcp.convo import switch as SW
from line_runtime.line_channel import line_user
from tests.test_baito_chat import FakeBaitoLLM
from tests.test_career_c2 import FakeReadLLM, act
from tests.test_career_chat import FakeCareerLLM, answer_for, current_qid, start_script
from tests.test_consult import ConsultModel
from tests.test_handle import call, reply
from tests.test_line_data import _say
from tests.test_line_webhook import RAW_USER, _server, line_id_key  # noqa: F401  line_id_key はフィクスチャ

CASUAL = "とりあえずバイトでいい。週3くらいで"
TO_CAREER = "やっぱり正社員で探したい"


@pytest.fixture(autouse=True)
def production(monkeypatch):
    """本番の組み合わせ。正社員の版は各テストで決める。"""
    monkeypatch.setenv(logic.BAITO_HARNESS_FLAG, "v4")
    monkeypatch.setenv(H.FORM_ENV, "D")
    monkeypatch.delenv(logic.TALK_LAYER_FLAG, raising=False)


class CareerLLM:
    """正社員のハーネスのモデル。c1 の読み取り(extract)は FakeCareerLLM、c2 の読む段(read)は FakeReadLLM。言う段はどちらも
    問いの原文。呼んだ段の名前を残す。"""

    def __init__(self, readings: dict | None = None):
        self.c1, self.c2 = FakeCareerLLM(), FakeReadLLM()
        self.c2.READINGS = {**FakeReadLLM.READINGS, **(readings or {})}
        self.stages: list[str] = []

    async def __call__(self, stage, system, messages):
        self.stages.append(stage)
        return await (self.c2 if stage == "read" else self.c1)(stage, system, messages)


def baito_start(uid: str) -> list:
    """トークの AI: アルバイト・パートに切り替え、playbook を読み、相談を始める。"""
    return [call("set_search_track", {"line_user_id": uid, "track": "アルバイト・パート", "evidence": "バイトでいい"}, "t0"),
            call("get_counseling_playbook", {"track": "アルバイト・パート", "channel": "chat"}, "pb"),
            call("start_baito_consultation", {"line_user_id": uid}, "st"),
            reply(["合いそうなバイトを、一緒に探しましょう。"])]


def connect(tmp_path, monkeypatch, model, llm):
    server, fake = _server(tmp_path, monkeypatch, model)
    from line_runtime.runtime import LineRuntime

    monkeypatch.setattr(server, "LineRuntime", partial(LineRuntime, model=model, career_llm=llm, baito_llm=FakeBaitoLLM()))
    return server, fake


def version(st, uid) -> str | None:
    d = st.user(uid).get(session.KEY)
    return d["harness"]["version"] if d else None


def quick_of(fake) -> list[str]:
    last = fake.sent()[-1][1]["messages"][-1]
    return [i["action"]["text"] for i in last.get("quickReply", {}).get("items", [])]


def answer(st, uid, fake) -> str:
    qid = current_qid(st, uid)
    return H.START_GO if qid == "start" else answer_for(qid, quick_of(fake))


@pytest.mark.parametrize("before, after", [("c1", "c2"), ("c2", "c1")])
def test_started_consultation_finishes_in_its_version_after_the_flag_changes(tmp_path, monkeypatch, before, after):
    """切り替え(c1 → c2)と戻し(c2 → c1): 相談の途中で運営者が環境変数を変えて起こし直しても(dip の MCP は起動のときに
    環境変数を読むので、本番と同じくサーバーを止めて起こし直す)、始めた相談は始めた版の読み方のまま完成まで進み、答えを保存する。
    次に始める相談は、新しいフラグの版。"""
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, before)
    uid = line_user(RAW_USER)
    llm = CareerLLM()
    model = ConsultModel(script=start_script(uid))
    server, fake = connect(tmp_path, monkeypatch, model, llm)
    st = server.state
    reads = {"c1": "extract", "c2": "read"}
    with TestClient(server.app) as client:
        _say(client, "正社員で転職したいので相談したい", 1)
        for n in range(2, 5):
            _say(client, answer(st, uid, fake), n)
        assert version(st, uid) == before and current_qid(st, uid) is not None
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, after)  # 運営者が本番の環境変数を変える(相談の途中)
    cut = len(llm.stages)
    with TestClient(server.app) as client:
        n = 5
        while current_qid(st, uid) is not None:
            assert n < 45, "完成に着かない"
            _say(client, answer(st, uid, fake), n)
            assert version(st, uid) in (before, None)  # 始めた版のまま(完成で閉じると None)
            n += 1
        assert n > 6 and model.script == []  # 起こし直した後も相談は続き、トークの AI は呼ばない
        last = fake.sent()[-1][1]["messages"]
        assert [m["text"] for m in last if m["type"] == "text"][-1].startswith(H.RESULT_TEXT)
        assert last[-1]["type"] == "flex" and last[-1]["contents"]["type"] == "carousel"  # 形 D の完成
        assert set(llm.stages[cut:]) - {"say"} == {reads[before]}  # 切り替えの後も、始めた版の読み方だけ
        assert logic.stated_preferences(st.user(uid))["area"]["value"] == "大阪市内"

        model.script.extend(start_script(uid))  # 次の相談はトークの AI が始める
        _say(client, "もう一度相談したい", n)
        assert version(st, uid) == after and current_qid(st, uid) == "start"
        cut = len(llm.stages)
        for k in range(n + 1, n + 5):  # 「始める」、区分のボタン、文で答える問い(ib_why_now)
            _say(client, answer(st, uid, fake), k)
        assert version(st, uid) == after and set(llm.stages[cut:]) - {"say"} == {reads[after]}


def test_c2_moving_to_v4_goes_through_the_talk_ai_which_starts_v4(tmp_path, monkeypatch):
    """c2 の相談の途中の「とりあえずバイトでいい。週3くらいで」: c2 は記録を書いて閉じ、移る先の v4 は層の版でないので、トークの AI が
    このターンを受けて v4 の相談を始める。v4 は記録を読まない(「週3くらい」は入らない。今と同じく聞き直す)。"""
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c2")
    uid = line_user(RAW_USER)
    llm = CareerLLM({CASUAL: [act("switch", to="baito", strength="want"), act("add", slot="work_hours", text="週3くらい")]})
    model = ConsultModel(script=start_script(uid))
    server, fake = connect(tmp_path, monkeypatch, model, llm)
    st = server.state
    with TestClient(server.app) as client:
        _say(client, "正社員で転職したいので相談したい", 1)
        _say(client, H.START_GO, 2)
        _say(client, answer(st, uid, fake), 3)
        assert version(st, uid) == "c2" and session.active(st, uid)
        model.script.extend(baito_start(uid))
        _say(client, CASUAL, 4)
    assert llm.stages[-1] == "read" and model.script == []  # 読む段の後に閉じ、トークの AI が受けた
    assert not session.active(st, uid) and baito_session.active(st, uid)
    b = st.user(uid)[baito_session.KEY]["harness"]
    assert b["version"] == "v4" and "days_per_week" not in b["slots"]
    assert st.user(uid)[SW.KEY]["to"] == "baito"  # 記録は v4 が読まずに残る(30 分で消える)
    assert logic.search_track(st.user(uid)) == "アルバイト・パート"


def test_v4_moving_to_career_goes_through_the_talk_ai_which_starts_c2(tmp_path, monkeypatch):
    """v4 の相談の途中の「やっぱり正社員で探したい」: v4 は今と同じく閉じてトークの AI に渡し、トークの AI が始める正社員の相談は c2
    (形 D の「始める」の 1 枚から)。"""
    monkeypatch.setenv(logic.CAREER_HARNESS_FLAG, "c2")
    uid = line_user(RAW_USER)
    model = ConsultModel(script=baito_start(uid))
    server, fake = connect(tmp_path, monkeypatch, model, CareerLLM())
    st = server.state
    with TestClient(server.app) as client:
        _say(client, "バイトを探したい", 1)
        assert st.user(uid)[baito_session.KEY]["harness"]["version"] == "v4"
        model.script.extend(start_script(uid))
        _say(client, TO_CAREER, 2)
        assert model.script == [] and not baito_session.active(st, uid)
        assert version(st, uid) == "c2" and current_qid(st, uid) == "start"
        assert any(m["type"] == "flex" for m in fake.sent()[-1][1]["messages"])  # 「始める」の 1 枚
        assert logic.search_track(st.user(uid)) == "正社員"
        _say(client, H.START_GO, 3)
        assert current_qid(st, uid) == "q_segment"
