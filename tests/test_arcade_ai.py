"""レジュメ・アーケードの AI と下書き(docs/prd/resume-arcade.md の (3/3)、task 188)の台本テスト。実 API は呼ばない。

LLM は台本のスタブ(OpenAI の SDK の形。server.arcade_client を差し替える)。画面と同じ API(/api/arcade/start → /ai → /save)
から入り、本人から見える結果(API の返事、プロフィール、dip の要約、トークの履歴、下書きの画面)と、スタブに届いた入力を見る。
- 事実の確かめ: 本人の答えに無い数字は空欄になり、置き換えた数が記録される。本人が書いた数字(1,200 と 1200、全角、三割)は消さない
- 費用の上限: 1 回の遊びの反応の回数、1 人 1 日の AI つきの遊び、AI を止める設定、ターン数の上限、まとめの 4,000 字。どれも LLM を呼ばない
- 遊びの番号: 無い・ほかの人の・24 時間を過ぎた・置き換えられた番号では LLM を呼ばず、保存は 403(24 時間を過ぎても保存はできる)
- 保存: プロフィール、dip の要約、トークに出る。雇用区分で書類の種類が決まり、区分は変わらない。「すべて消す」で消え、古いリンクは開かない
- LINE: 要約にゲームの URL が無く、LINE のメッセージにゲームのボタンが出ない
- 画面を押すテスト(Playwright。入っていなければ飛ばす): AI の道で質問・反応・評決・保存まで行き、再読み込みで start を呼び直さない
"""

from __future__ import annotations

import json
import re
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dip_mcp import arcade, logic
from line_runtime import line_channel
from resume_lines import PLACEHOLDER, check_numbers, find_metric
from tests.test_arcade import _phone, _visible, browser, served  # noqa: F401  フィクスチャ
from tests.test_revisit import Greeter, greet
from tests.test_talk_page import app, open_talk  # noqa: F401  app はフィクスチャ

GAME = "the-case-of-you"
ANSWER = "決済画面をReactで作り直し、決済完了率を61%から68%に上げました。問い合わせは月1,200件から減りました。"
MD = """# [氏名]
**フロントエンドエンジニア**
[メール] · [電話] · [居住地] · [リンク]

## 職務要約
決済画面を刷新したフロントエンドエンジニアである。

## 職務経歴
### ペーパームーン｜シニアエンジニア
*2022年 - 現在*
- 決済画面をReactで刷新し、決済完了率を61%から68%に改善
- 共通コンポーネントのライブラリを整備 [+X% ← 数字を入れる]

## スキル
React · TypeScript
"""
JOB = {"id": "j1", "company": "ペーパームーン", "title": "シニアエンジニア", "dates": "2022年 - 現在"}


# ----------------------------- LLM の台本 -----------------------------


def opening(_d):
    return {"line": "雨の夜だ。あんたが一番数字を動かした仕事は何だ？"}


def reaction(_d):
    return {"line": "61%から68%か。悪くない。", "vague": False, "evidence": [
        {"title": "大当たり", "fact": "決済画面を作り直し、決済完了率を61%から68%に上げた。", "quote": "決済完了率を61%から68%に上げました",
         "strength": "closer", "metric": "61%から68%"}]}


def verdict(_d):
    return {"monologue": "あんたは「決済完了率を61%から68%に上げました」と言った。悪くない。",
            "strengths": [{"name": "クローザー", "verdict": "成果を出しきる。", "exhibits": ["決済完了率を61%から68%に上げました"]}],
            "weaknesses": [{"name": "死角", "verdict": "数字の無い行がある。", "exhibits": []}],
            "leads": [{"title": "数字", "action": "問い合わせの数を確かめる。"}],
            "resume": {"headline": "フロントエンドエンジニア", "summary": "決済画面を刷新した。", "self_pr": "",
                       "experience": [{"jobId": "j1", "bullets": ["決済完了率を61%から68%に改善"]}],
                       "skills": ["React"], "education": ""}}


SCRIPT = {"Opening": opening, "Reaction": reaction, "Verdict": verdict,
          "Lab": lambda d: {"clusters": [{"name": "クローザー", "ids": ["e1"], "why": "成果を出しきる。"}]},
          "Turn": lambda d: {"quality": "good", "metric": True, "skill": "technical", "say": "いい水じゃ。", "ask": "誰と進めたかね？"},
          "HarvestFinal": lambda d: {"headline": "エンジニア", "summary": "決済画面を刷新した。", "self_pr": "",
                                     "experience": [{"company": "ペーパームーン", "bullets": ["決済画面を刷新"]}],
                                     "skills": ["React"], "strengths": [], "weaknesses": [], "next_season": []}}


class StubLLM:
    """OpenAI の SDK の形の偽物(responses.parse だけ)。出力の形(text_format)の名前で台本を引き、届いた入力を覚える。"""

    def __init__(self, script: dict | None = None):
        self.script = dict(SCRIPT, **(script or {}))
        self.calls: list[dict] = []
        self.responses = SimpleNamespace(parse=self._parse)

    async def _parse(self, *, model, instructions, input, text_format, max_output_tokens, reasoning):
        self.calls.append({"model": model, "system": instructions, "user": input[0]["content"],
                           "schema": text_format.__name__})
        out = self.script[text_format.__name__](input[0]["content"])
        return SimpleNamespace(output_parsed=text_format.model_validate(out),
                               usage=SimpleNamespace(input_tokens=1500, output_tokens=200,
                                                     input_tokens_details=SimpleNamespace(cached_tokens=0)))


@pytest.fixture
def arc(app, monkeypatch):  # noqa: F811
    """サーバー(会話は偽のモデル)と LLM のスタブ。client は Cookie つき(GET / を開いた本人)。"""
    stub = StubLLM()
    monkeypatch.setattr(app, "arcade_client", lambda: stub)
    app.set_model(Greeter(script=[greet] * 20))
    with TestClient(app.app) as client:
        client.get("/")
        yield SimpleNamespace(server=app, client=client, llm=stub, uid=client.get("/api/me").json()["user_id"])


def start(client, game=GAME) -> dict:
    r = client.post("/api/arcade/start", json={"game": game})
    assert r.status_code == 200, r.text
    return r.json()


def ai(client, play, stage="reaction", data=None, game=GAME) -> dict:
    data = data if data is not None else {"job": JOB, "role": "boss", "witness": "ルー", "question": "数字は？",
                                          "answer": ANSWER}
    r = client.post("/api/arcade/ai", json={"game": game, "play": play, "stage": stage, "data": data})
    assert r.status_code == 200, r.text
    return r.json()


def save(client, play, md=MD, game=GAME, kind=None):
    return client.post("/api/arcade/save", json={"game": game, "play": play, "markdown": md, "document_kind": kind})


def ledger(arc_) -> list[dict]:
    return arc_.server.app.state.rt.ledger.rows


def set_track(arc_, track):
    logic.set_search_track(arc_.server.state, arc_.uid, track, "テスト")


# ----------------------------- 始めて、答えて、保存する -----------------------------


def test_start_answer_and_save_reach_the_profile_the_summary_and_the_talk(arc):
    """始めて、答えて、保存すると、プロフィールと dip の要約に下書きが出て、トークに「下書きができました」が足される。"""
    c = arc.client
    open_talk(c)  # トークの会話がある(挨拶だけ)
    set_track(arc, "正社員")
    used = arc.server.app.state.quota.used(arc.uid)
    p = start(c)
    assert p["ai"] is True and p["reason"] is None and p["document_kind"] == "職務経歴書"
    assert arc.server.app.state.quota.used(arc.uid) == used + 1  # 始めるときにターンを 1 つ数える

    q = ai(c, p["play"], "opening", {"job": JOB, "role": "boss", "witness": "ルー"})
    assert q["ok"] and q["result"]["line"].endswith("何だ？")
    r = ai(c, p["play"])
    assert r["ok"] and r["replaced"] == 0
    assert r["result"]["evidence"][0]["metric"] == "61%から68%"
    v = ai(c, p["play"], "verdict", {"jobs": [JOB | {"lines": [{"id": "e1", "text": "決済完了率を61%から68%に上げた",
                                                                  "quote": "61%から68%", "metric": "61%から68%"}]}]})
    assert v["ok"] and v["result"]["resume"]["experience"][0]["bullets"] == ["決済完了率を61%から68%に改善"]
    # 指示の文はサーバーが持つ(画面は送らない)。反応は安いモデル、最後のまとめは LINE_SIM_MODEL
    assert [x["schema"] for x in arc.llm.calls] == ["Opening", "Reaction", "Verdict"]
    assert [x["model"] for x in arc.llm.calls] == ["gpt-6-luna", "gpt-6-luna", "gpt-6-sol"]
    assert "Honesty" in arc.llm.calls[1]["system"] and ANSWER in arc.llm.calls[1]["user"]
    assert [row["event"] for row in ledger(arc)][-3:] == [f"arcade:{GAME}:opening", f"arcade:{GAME}:reaction",
                                                          f"arcade:{GAME}:verdict"]
    assert all(row["user_id"] == arc.uid and row["input_tokens"] == 1500 for row in ledger(arc)[-3:])

    s = save(c, p["play"])
    assert s.status_code == 200, s.text
    out = s.json()
    assert out["document_kind"] == "職務経歴書" and out["blanks"] == 1 and out["talk"] is True
    assert out["view_url"] == "/liff/arcade/draft?rid=0"  # ユーザーIDは載せない

    prof = c.get("/api/profile").json()
    (d,) = prof["arcade_drafts"]
    assert d["document_kind"] == "職務経歴書" and d["blanks"] == 1 and d["view_url"] == out["view_url"]
    assert prof["arcade_url"] == "/liff/arcade"
    summary = logic.conversation_summary(arc.server.state, arc.uid)
    ad = summary["arcade_draft"]
    assert ad["document_kind"] == "職務経歴書" and ad["lines"] == 2 and ad["blanks"] == 1
    assert ad["blank_lines"] == ["ペーパームーン: 共通コンポーネントのライブラリを整備 [+X% ← 数字を入れる]"]
    assert "決済完了率を61%から68%に改善" not in json.dumps(ad, ensure_ascii=False)  # 空欄の無い行の全文は載せない
    assert summary["arcade_url"] == "/liff/arcade/the-case-of-you"  # 正社員の既定のゲーム

    turns = c.get("/api/talk/history").json()["turns"]
    last = turns[-1]["bot"]
    assert turns[-1]["user"] is None and "職務経歴書の下書きができました" in last["bubbles"][0]
    assert last["buttons"] == [{"label": "下書きを見る", "url": "/liff/arcade/draft?rid=0"}]
    assert last["quick_replies"] == ["求人を探す", "空欄を埋める"] and all(len(b) <= 80 for b in last["bubbles"])

    page = c.get("/liff/arcade/draft", params={"rid": 0})
    assert page.status_code == 200 and "共通コンポーネントのライブラリを整備" in page.text
    assert "確かめたい点" in page.text
    assert logic.search_track(arc.server.state.user(arc.uid)) == "正社員"  # ゲームの後も区分は変わらない
    assert len(arc.llm.calls) == 3  # 保存は LLM を呼ばない


def test_without_a_conversation_the_save_adds_nothing_to_the_talk(arc):
    """まだ会話が無いとき(ハブやプロフィールから入った)は、トークに足さない(保存はする)。"""
    set_track(arc, "正社員")
    p = start(arc.client)
    out = save(arc.client, p["play"]).json()
    assert out["talk"] is False and arcade.draft(arc.server.state, arc.uid, out["draft"]) is not None


def test_editing_the_draft_page_applies_the_same_rules(arc):
    """下書きの画面で直す: 数字の空欄を埋めて保存できる。電話番号は保存しない。プロフィールの「消す」で消える。"""
    c = arc.client
    set_track(arc, "正社員")
    p = start(c)
    save(c, p["play"])
    edited = MD.replace("[+X% ← 数字を入れる]", "(12種類を1つに)") + "\n連絡先 090-1234-5678\n"
    r = c.post("/liff/arcade/draft", data={"rid": 0, "markdown": edited, "scenario_user": ""})
    assert r.status_code == 200 and "保存しました" in r.text
    rec = arcade.draft(arc.server.state, arc.uid, 0)
    assert "12種類を1つに" in rec["markdown"] and "090-1234-5678" not in rec["markdown"]
    assert rec["resume"]["unconfirmed"] == []
    assert logic.conversation_summary(arc.server.state, arc.uid)["arcade_draft"]["blank_lines"] == []
    r = c.delete("/api/profile/arcade/0")
    assert r.status_code == 200 and r.json()["arcade_drafts"] == []
    assert c.get("/liff/arcade/draft", params={"rid": 0}).status_code == 404


# ----------------------------- 事実の確かめ -----------------------------


def test_numbers_not_in_the_answer_become_blanks_and_are_counted(arc):
    """LLM のスタブが本人の答えに無い数字を返すと、その数字は数字の空欄に置き換わり、置き換えた数が記録される。"""
    def invented(_d):
        r = reaction(_d)
        r["evidence"][0] |= {"fact": "決済完了率を61%から75%に上げ、売上を5000万円伸ばした。", "quote": "売上が倍になりました",
                             "metric": "75%"}
        return r

    arc.llm.script["Reaction"] = invented
    p = start(arc.client)
    r = ai(arc.client, p["play"])
    ev = r["result"]["evidence"][0]
    assert ev["fact"] == f"決済完了率を61%から{PLACEHOLDER}に上げ、売上を[X円 ← 数字を入れる]伸ばした。"
    assert ev["quote"] == "" and ev["metric"] == ""  # 本人の言葉に無い引用と数字は捨てる
    assert r["replaced"] == 4
    assert arc.server.state.user(arc.uid)["arcade_metrics"]["replaced"] == 4


def test_card_ids_are_not_the_players_numbers(arc):
    """行の番号(e1〜e8、j1)と強みのキーは本人の言葉ではない: 本人の行に数字が無ければ、AI が足した「5人」「6社」「3割」
    「8件」は空欄になる(番号の「e5」の 5 を本人の数字と見ない)。"""
    lines = [{"id": f"e{i}", "title": "現場", "text": "決済画面を作り直した", "quote": "作り直した", "strength": "closer"}
             for i in range(1, 9)]

    def invented_verdict(d):
        r = verdict(d)
        r["resume"] |= {"summary": "5人のチームを率いた。",
                        "experience": [{"jobId": "j1", "bullets": ["6社の新規顧客を開拓", "売上を3割向上"]}]}
        return r

    arc.llm.script["Verdict"] = invented_verdict
    arc.llm.script["Lab"] = lambda d: {"clusters": [{"name": "クローザー", "ids": ["e1", "e8"], "why": "8件の案件で成果"}]}
    p = start(arc.client)
    lab = ai(arc.client, p["play"], "lab", {"cards": lines})
    assert lab["result"]["clusters"][0]["why"] == "[N件 ← 数字を入れる]の案件で成果" and lab["replaced"] == 1
    v = ai(arc.client, p["play"], "verdict", {"jobs": [JOB | {"lines": lines[:6]}]})
    res = v["result"]["resume"]
    assert res["summary"] == "[N件 ← 数字を入れる]のチームを率いた。"
    assert res["experience"][0]["bullets"] == ["[N件 ← 数字を入れる]の新規顧客を開拓", f"売上を{PLACEHOLDER}向上"]


def test_neighbouring_fields_do_not_run_together(arc):
    """欄と欄の数字はくっつかない: 経験年数「5」の次の欄が「10人の…」でも、本人の「10人」は残る。
    2 つの欄にまたがる引用(「教えた」+「3人」)は本人の言葉と見ない。"""
    lines = [{"id": "e1", "title": "現場", "text": "新人に教えた", "quote": "", "metric": ""},
             {"id": "e2", "title": "現場", "text": "3人でレジを回した", "quote": "", "metric": ""}]

    def verdict_from_fields(d):
        r = verdict(d)
        r["strengths"][0]["exhibits"] = ["教えた3人", "3人でレジを回した"]
        r["resume"]["summary"] = "10人のチームを率いたい。"
        return r

    arc.llm.script["Verdict"] = verdict_from_fields
    p = start(arc.client)
    data = {"intake": {"years": "5", "target": "10人のチームのリーダー"}, "jobs": [JOB | {"lines": lines}]}
    v = ai(arc.client, p["play"], "verdict", data)
    assert v["result"]["resume"]["summary"] == "10人のチームを率いたい。"
    assert v["result"]["strengths"][0]["exhibits"] == ["3人でレジを回した"]
    # 仕事の収穫の年数「2019〜2022」の次の行が「3人で…」でも、つないだ「20223人」は本人の数字ではない
    assert check_numbers("20223人", ["2019〜2022", "3人でレジを回した"])[1] == 1
    assert check_numbers("2022年まで3人で回した", ["2019〜2022", "3人でレジを回した"])[1] == 0
    # 1 つの欄の中でも、空白をはさんだ数字はくっつけない
    assert check_numbers("2022件", ["2019 2022"])[1] == 0 and check_numbers("20192022件", ["2019 2022"])[1] == 1


def test_the_fact_check_keeps_what_the_player_wrote():
    """本人が書いた数字を消さない(1,200 と 1200、全角の数字、本人も AI も「三割」)。日付は成果の数字と数えない。"""
    said = ["問い合わせを月1,200件さばき、待ち時間を３割減らしました。表示は６１％速くなった。2022年に公開。"]
    for text in ["月1200件をさばいた", "月1,200件をさばいた", "待ち時間を３割削減", "表示を61%高速化", "2022年に公開"]:
        assert check_numbers(text, said) == (text, 0), text
    assert check_numbers("待ち時間を三割削減", ["待ち時間を三割減らした"]) == ("待ち時間を三割削減", 0)
    assert check_numbers("待ち時間を30%削減", said)[1] == 1  # 単位の言い換えはそろえない(空欄になる側)
    assert find_metric("2022年3月に入社し、5日で覚えた") is None and find_metric("決済完了率が61%から68%") == "61%から68%"


def test_phone_numbers_are_masked_before_the_llm_and_never_saved(arc):
    """答えに電話番号があると、LLM のスタブに届く文では伏せてあり、保存した下書きにも無い。"""
    p = start(arc.client)
    data = {"job": JOB, "role": "boss", "witness": "ルー", "answer": ANSWER + "連絡は090-1234-5678かmika@example.com"}
    assert ai(arc.client, p["play"], data=data)["ok"]
    sent = arc.llm.calls[-1]["user"]
    assert "090-1234-5678" not in sent and "mika@example.com" not in sent and "[連絡先]" in sent
    set_track(arc, "正社員")
    save(arc.client, p["play"], MD + "\n連絡先: 090-1234-5678 mika@example.com\n")
    rec = arcade.draft(arc.server.state, arc.uid, 0)
    assert "090" not in json.dumps(rec, ensure_ascii=False) and "mika@" not in json.dumps(rec, ensure_ascii=False)
    assert all(v == "" for v in rec["personal"].values())


# ----------------------------- 費用の上限 -----------------------------


def _offline(r: dict, reason: str) -> None:
    assert r == {"ok": False, "offline": True, "reason": reason}, r


def test_the_reaction_budget_of_a_play_falls_back_without_the_llm(arc, monkeypatch):
    """1 回の遊びの反応の回数を使い切ったら、LLM を呼ばずに簡易分析で続ける。最後のまとめの分は別に残っている。"""
    monkeypatch.setenv(arcade.REACTIONS_ENV, "1")
    p = start(arc.client)
    assert ai(arc.client, p["play"])["ok"]
    _offline(ai(arc.client, p["play"]), "reaction_limit")
    assert len(arc.llm.calls) == 1
    # 証拠のまとめ(鑑識)は、最後のまとめの 1 回を残す
    cards = {"cards": [{"id": "e1", "title": "大当たり", "text": "61%から68%"}]}
    assert ai(arc.client, p["play"], "lab", cards)["ok"]
    _offline(ai(arc.client, p["play"], "lab", cards), "summary_limit")
    assert ai(arc.client, p["play"], "verdict", {"jobs": [JOB]})["ok"]
    _offline(ai(arc.client, p["play"], "verdict", {"jobs": [JOB]}), "summary_limit")
    assert len(arc.llm.calls) == 3


def test_the_daily_ai_plays_and_the_off_switch_start_without_ai(arc, monkeypatch):
    """1 人 1 日の AI つきの遊びを使い切ったら、AI なしの番号で始める。AI を止める設定と API キーが無いときも同じ。"""
    monkeypatch.setenv("AICA_ARCADE_DAILY_AI_PLAYS", "1")
    assert start(arc.client)["ai"] is True
    p = start(arc.client)
    assert (p["ai"], p["reason"]) == (False, "daily_limit")
    _offline(ai(arc.client, p["play"]), "ai_off")
    monkeypatch.setenv("AICA_ARCADE_DAILY_AI_PLAYS", "0")
    monkeypatch.setenv("AICA_ARCADE_AI", "0")
    p = start(arc.client)
    assert (p["ai"], p["reason"]) == (False, "disabled")
    monkeypatch.delenv("AICA_ARCADE_AI")
    monkeypatch.delenv("OPENAI_API_KEY")
    p = start(arc.client)
    assert (p["ai"], p["reason"]) == (False, "no_key")
    _offline(ai(arc.client, p["play"]), "ai_off")
    assert len(arc.llm.calls) == 0


def test_over_the_turn_limit_start_never_grows_the_state_and_old_ids_stop_working(arc, monkeypatch):
    """ターン数の上限を超えていると AI なしで始める(断らない)。100 回 start してもゲームごとに記録は 1 件。
    置き換えられた前の番号では、/ai は LLM を呼ばず、/save は 403。今の番号なら保存できる。"""
    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "1")
    arc.server.app.state.quota.limit = 1
    first = start(arc.client)
    assert first["ai"] is True
    for _ in range(100):
        p = start(arc.client)
        assert (p["ai"], p["reason"]) == (False, "turn_limit")
    plays = arc.server.state.user(arc.uid)["arcade_plays"]
    assert list(plays) == [GAME] and plays[GAME]["id"] == p["play"]
    assert arc.server.state.user(arc.uid)["arcade_metrics"]["started"] == 101  # 「始めた」は記録の数とは別に数える
    _offline(ai(arc.client, first["play"]), "no_play")
    assert save(arc.client, first["play"]).status_code == 403
    set_track(arc, "正社員")
    assert save(arc.client, p["play"]).status_code == 200
    assert len(arc.llm.calls) == 0


def test_ai_without_a_valid_play_spends_nothing(arc, monkeypatch):
    """番号なし・ほかの人の番号・24 時間を過ぎた番号の /ai は、LLM を呼ばず、ターンも費用台帳も増えない。
    24 時間を過ぎても保存はできる(LLM を呼ばないので)。"""
    p = start(arc.client)
    used, rows = arc.server.app.state.quota.used(arc.uid), len(ledger(arc))
    _offline(ai(arc.client, ""), "no_play")
    _offline(ai(arc.client, "番号" + p["play"][2:]), "no_play")  # ASCII でない番号も断る
    stranger = TestClient(arc.client.app, cookies={"aica_uid": "w-00000000000000ff"})
    _offline(ai(stranger, p["play"]), "no_play")
    assert "w-00000000000000ff" not in arc.server.state.user_ids()  # 番号の無い呼び出しで dip の状態にユーザーを作らない
    other = TestClient(arc.client.app)
    other.get("/")
    _offline(ai(other, p["play"]), "no_play")
    assert save(other, p["play"]).status_code == 403
    later = arcade.clock() + timedelta(hours=25)
    monkeypatch.setattr(arcade, "clock", lambda: later)
    _offline(ai(arc.client, p["play"]), "expired")
    assert arc.server.app.state.quota.used(arc.uid) == used and len(ledger(arc)) == rows
    assert len(arc.llm.calls) == 0
    set_track(arc, "正社員")
    assert save(arc.client, p["play"]).status_code == 200


def test_summaries_over_4000_chars_call_no_llm_and_4_full_jobs_stay_within_it(arc):
    """最後のまとめで、送る合計が 4,000 字を超えると LLM は呼ばれない。上限の内なら、4 職場の書類でも LLM に届く入力が収まる。"""
    p = start(arc.client)
    line = {"id": "e", "text": "決" * 120, "quote": "引" * 40, "metric": "数" * 40}  # 1 行の上限いっぱい
    big = {"jobs": [JOB | {"id": f"j{i}", "lines": [line] * 6} for i in range(4)]}
    _offline(ai(arc.client, p["play"], "verdict", big), "too_long")
    assert len(arc.llm.calls) == 0
    fit = {"jobs": [JOB | {"id": f"j{i}", "lines": [line | {"text": "決" * 100, "metric": ""}] * 6} for i in range(4)]}
    assert sum(len(x["text"]) + len(x["quote"]) for j in fit["jobs"] for x in j["lines"]) < 4000
    assert ai(arc.client, p["play"], "verdict", fit)["ok"]
    sent = arc.llm.calls[-1]["user"]
    assert sent.count("決" * 100) == 24 and len(sent) < 4000 + 3000  # 行と見出しと JSON の区切りだけ
    # 長すぎる 1 行・1 つの答えは、超えた分を送らない
    long = {"job": JOB, "role": "boss", "witness": "ルー", "answer": "あ" * 900}
    assert ai(arc.client, p["play"], data=long)["ok"]
    assert "あ" * 600 in arc.llm.calls[-1]["user"] and "あ" * 601 not in arc.llm.calls[-1]["user"]


def test_harvest_of_work_turn_and_final_use_the_server_prompts(arc):
    """仕事の収穫: 水やり(turn)と収穫祭(final)も同じ API で、反応は安いモデル、収穫祭は LINE_SIM_MODEL。"""
    p = start(arc.client, "harvest-of-work")
    t = ai(arc.client, p["play"], "turn", {"title": "決済画面", "story": "壊れていた決済画面を作り直した。", "answer": None,
                                           "next_focus": "context"}, game="harvest-of-work")
    assert t["ok"] and t["result"]["ask"] == "誰と進めたかね？"
    f = ai(arc.client, p["play"], "final", {"jobs": [{"company": "ペーパームーン", "lines": [{"text": "決済画面を刷新"}]}]},
           game="harvest-of-work")
    assert f["ok"] and f["result"]["experience"][0]["bullets"] == ["決済画面を刷新"]
    assert [x["model"] for x in arc.llm.calls] == ["gpt-6-luna", "gpt-6-sol"]
    _offline(ai(arc.client, p["play"], "verdict", {"jobs": [JOB]}, game="harvest-of-work"), "unknown_stage")


def test_a_failing_llm_falls_back_and_the_game_keeps_going(arc):
    def boom(_d):
        raise RuntimeError("API が落ちた")

    arc.llm.script["Reaction"] = boom
    p = start(arc.client)
    _offline(ai(arc.client, p["play"]), "error")
    _offline(ai(arc.client, p["play"], data={"job": JOB, "role": "nobody", "answer": "x"}), "bad_input")


# ----------------------------- 書類の種類と雇用区分 -----------------------------


def test_the_track_decides_the_document_and_stays_the_same(arc):
    """正社員なら職務経歴書、アルバイト・パートなら履歴書。未定は本人が選ぶ(選ばなければ 400)。区分は変わらない。"""
    c, st = arc.client, arc.server.state
    p = start(c)
    assert p["document_kind"] is None and p["track"] == "未定"
    assert save(c, p["play"]).status_code == 400
    assert save(c, p["play"], kind="履歴書").json()["document_kind"] == "履歴書"
    set_track(arc, "アルバイト・パート")
    p = start(c, "harvest-of-work")
    assert p["document_kind"] == "履歴書"
    assert save(c, p["play"], game="harvest-of-work", kind="職務経歴書").json()["document_kind"] == "履歴書"
    assert logic.search_track(st.user(arc.uid)) == "アルバイト・パート"
    assert logic.conversation_summary(st, arc.uid)["arcade_url"] == "/liff/arcade/harvest-of-work"
    assert [d["document_kind"] for d in c.get("/api/profile").json()["arcade_drafts"]] == ["履歴書", "履歴書"]


def test_an_undecided_player_gets_a_self_pr_so_they_can_pick_the_resume_at_the_end(arc):
    """未定なら、最後のまとめで自己PR も書かせる(本人が後で「履歴書の自己PRにする」を選べるように)。職務経歴書なら書かせない。"""
    c = arc.client
    p = start(c)
    assert p["document_kind"] is None
    ai(c, p["play"], "verdict", {"jobs": [JOB], "document_kind": None})
    assert "also write a 自己PR" in arc.llm.calls[-1]["system"]
    set_track(arc, "正社員")
    p = start(c)
    ai(c, p["play"], "verdict", {"jobs": [JOB], "document_kind": "職務経歴書"})
    assert 'write "" (the document is a 職務経歴書)' in arc.llm.calls[-1]["system"]
    set_track(arc, "アルバイト・パート")
    p = start(c, "harvest-of-work")
    ai(c, p["play"], "final", {"jobs": [{"company": "駅前カフェ"}], "document_kind": "履歴書"}, game="harvest-of-work")
    assert "the document is a 履歴書, so write a 自己PR" in arc.llm.calls[-1]["system"]


def test_the_resume_upload_fills_the_jobs(arc):
    """履歴書の読み取り結果があれば、職場の名前、役職、期間を最初から入れる。"""
    logic.save_resume_upload(arc.server.state, arc.uid, {"resume": {"work_history": [
        {"organization": "ペーパームーン", "role": "エンジニア", "period": "2022年〜現在"}]}})
    assert start(arc.client)["jobs"] == [{"company": "ペーパームーン", "title": "エンジニア", "dates": "2022年〜現在"}]


# ----------------------------- 本人と、消す -----------------------------


def test_other_browsers_cannot_start_call_save_or_view(arc):
    c = arc.client
    set_track(arc, "正社員")
    p = start(c)
    save(c, p["play"])
    other = TestClient(c.app)
    other.get("/")
    _offline(ai(other, p["play"]), "no_play")
    assert save(other, p["play"]).status_code == 403
    assert other.get("/liff/arcade/draft", params={"rid": 0}).status_code == 404
    assert other.post("/liff/arcade/draft", data={"rid": 0, "markdown": "x"}).status_code == 404
    nobody = TestClient(c.app)  # Cookie なし: 始めても番号は無い(AI も保存も無し)。ほかは 401
    assert nobody.post("/api/arcade/start", json={"game": GAME}).json()["play"] is None
    assert nobody.post("/api/arcade/ai", json={"game": GAME, "play": p["play"], "stage": "reaction"}).status_code == 401
    assert save(nobody, p["play"]).status_code == 401
    assert nobody.get("/liff/arcade/draft", params={"rid": 0}).status_code == 401
    assert c.post("/api/arcade/start", json={"game": "career-pet"}).status_code == 404  # AI につながっていないゲーム
    assert len(arc.llm.calls) == 0


def test_forget_all_removes_drafts_and_plays_and_old_links_never_open_new_drafts(arc):
    """「すべて消す」で、ゲームの下書きと遊びの記録も消える。消した後に古い番号で /ai を呼んでも LLM は呼ばれない。
    前の下書きの番号のリンクを開いても、新しい下書きは開かない。"""
    c = arc.client
    set_track(arc, "正社員")
    p = start(c)
    assert save(c, p["play"]).json()["draft"] == 0
    assert c.post("/api/profile/forget").status_code == 200
    u = arc.server.state.user(arc.uid)
    assert "arcade_drafts" not in u and "arcade_plays" not in u and u["arcade_draft_seq"] == 1
    _offline(ai(c, p["play"]), "no_play")
    assert save(c, p["play"]).status_code == 403
    set_track(arc, "正社員")
    assert save(c, start(c)["play"]).json()["draft"] == 1
    assert c.get("/liff/arcade/draft", params={"rid": 0}).status_code == 404
    assert c.get("/liff/arcade/draft", params={"rid": 1}).status_code == 200
    assert len(arc.llm.calls) == 0


# ----------------------------- LINE -----------------------------


def test_line_users_get_no_game_url_and_no_game_buttons(tmp_path):
    """LINE のユーザーには、dip の要約にゲームの URL が無く、LINE のメッセージにも /liff/arcade のボタンが出ない。"""
    from dip_mcp.data import State

    st = State(tmp_path / "state.json")
    st.reset()
    line_uid = line_channel.line_user("U" + "0" * 32, b"k" * 32)
    assert "arcade_url" not in logic.conversation_summary(st, line_uid)
    assert logic.conversation_summary(st, "w-0123456789abcdef")["arcade_url"] == "/liff/arcade"
    reply_ = {"bubbles": ["遊びながら書類を作れます"], "buttons": [
        {"label": "ゲームで書類を作る", "url": "/liff/arcade/the-case-of-you"}, {"label": "下書きを見る", "url": "/liff/arcade/draft?rid=0"},
        {"label": "応募の続き", "url": "/liff/apply?job=a001"}]}
    msgs = line_channel.to_messages(reply_, "https://aica.example")
    text = json.dumps(msgs, ensure_ascii=False)
    assert "/liff/arcade" not in text and "/liff/apply?job=a001" in text


# ----------------------------- 画面を押す(Playwright) -----------------------------


def test_the_case_of_you_ai_path_plays_to_a_saved_draft_and_reload_does_not_restart(arc, served, browser):  # noqa: F811
    """本人の事件を AI の道で遊ぶ: AI の証人の質問 → 反応(書類の行)→ 評決 → プロフィールに保存。JS のエラーは 0 件。
    再読み込みして続きを開いても、start を呼び直さない(ターン数と 1 日の遊びの回数は増えない)。"""
    st, quota = arc.server.state, arc.server.app.state.quota
    context, page, errors = _phone(browser)
    page.goto(served + "/")  # トーク画面を開いて、匿名IDの Cookie をもらう
    page.wait_for_timeout(300)
    uid = next(ck["value"] for ck in context.cookies() if ck["name"] == "aica_uid")
    set_track(SimpleNamespace(server=arc.server, uid=uid), "正社員")
    page.goto(served + "/liff/arcade/the-case-of-you")
    before = quota.used(uid)  # トーク画面の挨拶で使った分
    page.locator("#btn-play").click()
    page.locator("#intro-skip").click()
    page.locator("#scr-intake.on").wait_for()
    page.locator("#ai-badge.on").wait_for()  # 本部回線：接続（AI）
    assert "本部回線" in page.locator("#ai-badge").inner_text()
    used, plays = quota.used(uid), quota.daily_used("arcade_ai_play", uid)
    assert (used, plays) == (before + 1, 1)  # 始めるときにターンを 1 つ、AI つきの遊びを 1 回
    page.locator("#job-co-0").fill("ペーパームーン")
    page.locator("#job-ti-0").fill("シニアエンジニア")
    page.locator("#btn-streets").click()
    page.locator("#scr-map.on").wait_for()
    page.locator("#guide .btn", has_text="聞き込みへ").click()
    page.locator("#scr-interro.on").wait_for()
    page.locator("#w-say", has_text="一番数字を動かした仕事").wait_for()  # AI の証人の質問
    page.locator("#answer").fill(ANSWER)
    page.locator("#btn-testify").click()
    page.locator("#after-row").wait_for()
    assert "職務経歴書 1行" in page.locator("#guide").inner_text()

    page.wait_for_timeout(400)  # 進み具合は 150ms 遅れて端末に書かれる
    page.reload()  # 再読み込み: 残っている番号で続ける(start を呼び直さない)
    page.locator("#btn-continue").click()
    page.locator("#scr-map.on, #scr-interro.on").first.wait_for()
    assert (quota.used(uid), quota.daily_used("arcade_ai_play", uid)) == (used, plays)
    assert st.user(uid)["arcade_metrics"]["started"] == 1

    page.locator("#loc-list .loc", has_text="市役所").click()
    page.locator("#modal-btns button", has_text="このまま閉じる").click()
    page.locator("#btn-dossier").wait_for(timeout=20_000)
    assert "AIとつながってタイプしました" in page.locator("#v-status").inner_text()
    page.locator("#btn-dossier").click()
    page.locator("#scr-dossier.on").wait_for()
    assert "AIとつながって" in page.locator("#d-mode").inner_text()
    assert "決済完了率を61%から68%に改善" in page.locator("#doc-body").inner_text()
    page.locator("#btn-save").click()
    page.locator("#modal-title", has_text="プロフィールに保存しました").wait_for()
    page.locator("#modal-btns button", has_text="残す").click()
    assert [x["schema"] for x in arc.llm.calls] == ["Opening", "Reaction", "Verdict"]
    assert arcade.drafts(st, uid)[0]["document_kind"] == "職務経歴書"
    assert errors == [], errors
    context.close()


def test_the_games_fall_back_to_offline_when_the_ai_fails(arc, served, browser):  # noqa: F811
    """AI が失敗しても(API のエラー)、その段を台本と簡易分析で続け、JS のエラーは出ない。"""
    def boom(_d):
        raise RuntimeError("API が落ちた")

    arc.llm.script["Opening"] = boom
    context, page, errors = _phone(browser)
    page.goto(served + "/")
    page.wait_for_timeout(300)
    page.goto(served + "/liff/arcade/the-case-of-you")
    page.locator("#btn-play").click()
    page.locator("#intro-skip").click()
    page.locator("#scr-intake.on").wait_for()
    page.locator("#ai-badge.on").wait_for()
    page.locator("#job-co-0").fill("A社")
    page.locator("#btn-streets").click()
    page.locator("#guide .btn", has_text="聞き込みへ").click()
    page.locator("#scr-interro.on").wait_for()
    deadline = time.monotonic() + 10
    while page.locator("#btn-testify").is_disabled() or not page.locator("#ans-wrap").is_visible():
        assert time.monotonic() < deadline
        page.wait_for_timeout(100)
    assert re.search(r"A社", page.locator("#w-say").inner_text())  # 台本の質問
    assert page.locator("#toast").inner_text().startswith("回線に雑音")
    assert errors == [], errors
    context.close()


def _harvest_to_resume(page) -> None:
    """自分の畑を始めて、思い出を 2 つ植え、水をやり、収穫祭の書類の画面まで進める(職場と職種は入力ずみ)。"""
    page.locator("#btnBreakGround").click()
    page.locator("#scr-game.active").wait_for()
    seeds = iter([("レジの列を短くした", "昼のレジが混んでいたので、注文の取り方を変えた。"),
                  ("新人に教えた", "新人2人にレジの手順を教えた。")])
    deadline = time.monotonic() + 90
    while not _visible(page, "#resume"):
        assert time.monotonic() < deadline, "書類の画面まで行けない"
        if _visible(page, "#dlg"):
            title, answer = page.locator("#dlg-mem-title"), page.locator("#dlg-answer")
            buttons = page.locator("#dlgChoices button")
            if title.count() and not title.input_value():
                t, story = next(seeds)
                title.fill(t)
                page.locator("#dlg-mem-story").fill(story)
                buttons.filter(has_text="タネを袋に入れる").click()
            elif answer.count() and not answer.input_value():
                answer.fill("昼の注文を先に聞く係を作り、待ち時間を10分から5分にした。")
                buttons.filter(has_text="水をやる").click()
            elif buttons.count() and "オフライン" not in buttons.first.inner_text():  # AI を待つ間の「すぐできる暦」は押さない
                main = page.locator("#dlgChoices button.primary, #dlgChoices button.gold")
                (main.first if main.count() else buttons.first).click()
        elif _visible(page, "#scr-almanac.active"):
            page.locator("#btnToMarket").click()
        elif _visible(page, "#scr-market.active"):
            page.locator("#btnUnfold").click()
        elif _visible(page, "#objGo"):
            page.locator("#objGo").click()
        page.wait_for_timeout(80)


def test_harvest_of_work_ai_path_plays_to_a_saved_resume(arc, served, browser):  # noqa: F811
    """自分の畑を AI の道で遊ぶ: 水やりの質問と判定は AI(Turn)、収穫祭も AI(HarvestFinal)。アルバイト・パートなので
    履歴書としてプロフィールに保存する。JS のエラーは 0 件。"""
    st = arc.server.state
    context, page, errors = _phone(browser)
    page.goto(served + "/")
    page.wait_for_timeout(300)
    uid = next(ck["value"] for ck in context.cookies() if ck["name"] == "aica_uid")
    set_track(SimpleNamespace(server=arc.server, uid=uid), "アルバイト・パート")
    page.goto(served + "/liff/arcade/harvest-of-work")
    page.locator("#btnPlayMe").click()
    page.locator("#scr-setup.active").wait_for()
    page.locator("#fRole").fill("カフェスタッフ")
    page.locator("#job-company-0").fill("駅前カフェ")
    _harvest_to_resume(page)
    assert "クロードじいさんが、あなた自身の言葉から書きました" in page.locator("#genNote").inner_text()
    assert page.locator("#docKindLbl").is_hidden()  # アルバイト・パートは履歴書に決まっている
    page.locator("#btnSaveDraft").click()
    page.locator("#savedRow").wait_for()
    assert "保存済み" in page.locator("#saveNote").inner_text()
    assert page.locator("#draftLink").get_attribute("href") == "/liff/arcade/draft?rid=0"
    schemas = [x["schema"] for x in arc.llm.calls]
    assert "Turn" in schemas and schemas[-1] == "HarvestFinal"
    sent = arc.llm.calls[-1]["user"]
    # 収穫祭には答えの全文(思い出の文と Q&A)ではなく、職場ごとの書類の行(行の文、引用、数字)を送る
    assert "駅前カフェ" in sent and '"lines"' in sent and '"story"' not in sent and '"answers"' not in sent
    assert arcade.drafts(st, uid)[0]["document_kind"] == "履歴書"
    assert errors == [], errors
    context.close()


def test_an_undecided_player_picks_the_resume_after_the_verdict_and_it_is_saved_with_a_self_pr(arc, served, browser):  # noqa: F811
    """雇用区分が未定: 評決の後に「履歴書の自己PRにする」を選ぶと、書類に自己PR が入り、その形で保存される。
    AI は評決のときに自己PR も書いている(選び直しても AI を呼び直さない)。JS のエラーは 0 件。"""
    pr = "決済画面を作り直し、決済完了率を61%から68%に上げました。"
    arc.llm.script["Verdict"] = lambda d: verdict(d) | {"resume": verdict(d)["resume"] | {"self_pr": pr}}
    st = arc.server.state
    context, page, errors = _phone(browser)
    page.goto(served + "/")
    page.wait_for_timeout(300)
    uid = next(ck["value"] for ck in context.cookies() if ck["name"] == "aica_uid")
    page.goto(served + "/liff/arcade/the-case-of-you")
    page.locator("#btn-play").click()
    page.locator("#intro-skip").click()
    page.locator("#scr-intake.on").wait_for()
    page.locator("#ai-badge.on").wait_for()
    page.locator("#job-co-0").fill("ペーパームーン")
    page.locator("#btn-streets").click()
    page.locator("#guide .btn", has_text="聞き込みへ").click()
    page.locator("#w-say", has_text="一番数字を動かした仕事").wait_for()
    page.locator("#answer").fill(ANSWER)
    page.locator("#btn-testify").click()
    page.locator("#after-row").wait_for()
    page.locator("#btn-leave").click()
    page.locator("#scr-map.on").wait_for()
    page.locator("#loc-list .loc", has_text="市役所").click()
    page.locator("#modal-btns button", has_text="このまま閉じる").click()
    page.locator("#btn-dossier").wait_for(timeout=20_000)
    page.locator("#btn-dossier").click()
    page.locator("#scr-dossier.on").wait_for()
    assert "also write a 自己PR" in arc.llm.calls[-1]["system"]
    assert page.locator("#doc-kind-label").is_visible() and "自己PR" not in page.locator("#doc-body").inner_text()
    page.locator("#doc-kind").select_option("履歴書")
    assert pr in page.locator("#doc-body").inner_text()
    page.locator("#doc-kind").select_option("職務経歴書")  # 選び直すと自己PR の節が外れ、もう一度選ぶと戻る
    assert pr not in page.locator("#doc-body").inner_text()
    page.locator("#doc-kind").select_option("履歴書")
    page.locator("#btn-save").click()
    page.locator("#modal-title", has_text="プロフィールに保存しました").wait_for()
    page.locator("#modal-btns button", has_text="残す").click()
    d = arcade.drafts(st, uid)[0]
    assert d["document_kind"] == "履歴書" and d["resume"]["self_pr"] == pr and "## 自己PR" in d["markdown"]
    assert [x["schema"] for x in arc.llm.calls].count("Verdict") == 1
    assert logic.search_track(st.user(uid)) == "未定"
    assert errors == [], errors
    context.close()


def test_a_part_time_player_without_ai_still_saves_a_resume_with_a_self_pr(arc, served, browser, monkeypatch):  # noqa: F811
    """アルバイト・パートで AI を止めていても(簡易分析)、本人の行から作った自己PR の入った履歴書が保存される。"""
    monkeypatch.setenv("AICA_ARCADE_AI", "0")
    st = arc.server.state
    context, page, errors = _phone(browser)
    page.goto(served + "/")
    page.wait_for_timeout(300)
    uid = next(ck["value"] for ck in context.cookies() if ck["name"] == "aica_uid")
    set_track(SimpleNamespace(server=arc.server, uid=uid), "アルバイト・パート")
    page.goto(served + "/liff/arcade/harvest-of-work")
    page.locator("#btnPlayMe").click()
    page.locator("#scr-setup.active").wait_for()
    page.locator("#fRole").fill("カフェスタッフ")
    page.locator("#job-company-0").fill("駅前カフェ")
    _harvest_to_resume(page)
    assert "オフライン" in page.locator("#genNote").inner_text()
    shown = page.locator("#resume").inner_text()
    assert "自己PR" in shown and "これまでの仕事では" in shown
    page.locator("#btnSaveDraft").click()
    page.locator("#savedRow").wait_for()
    d = arcade.drafts(st, uid)[0]
    assert d["document_kind"] == "履歴書" and d["resume"]["self_pr"].startswith("これまでの仕事では、「")
    assert arc.llm.calls == []
    assert errors == [], errors
    context.close()


def test_the_markdown_maps_onto_the_resume_draft():
    """書類の Markdown(arcade-ja.js の resumeMarkdown の形)を Resume Draft に写す。空欄のある行ごとに確かめたい点を 1 つ。"""
    from resume_arcade.draft import to_envelope

    md = (MD.replace("## 職務経歴", "## 自己PR\nお客様の名前を覚えるのが得意です。\n\n## 職務経歴")
          .replace("## スキル", "### ランタン｜[役職]\n- [ここに実績を2〜3行書く]\n\n## スキル")
          + "\n## 学歴\nハーバーシティ大学 卒業\n")
    env = to_envelope(md, "履歴書")
    r = env["resume"]
    assert (env["format_version"], env["source"], env["document_kind"]) == ("1.1", "arcade", "履歴書")
    assert r["job_summary"] == "決済画面を刷新したフロントエンドエンジニアである。"
    assert r["self_pr"] == "お客様の名前を覚えるのが得意です。"
    paper, lantern = r["work_history"]
    assert (paper["organization"], paper["role"], paper["period"]) == ("ペーパームーン", "シニアエンジニア", "2022年 - 現在")
    assert paper["achievements"][0] == "決済画面をReactで刷新し、決済完了率を61%から68%に改善"
    assert lantern["achievements"] == [] and r["skills"] == ["React", "TypeScript"]
    assert r["education"][0]["school"] == "ハーバーシティ大学 卒業"
    assert r["unconfirmed"] == ["ペーパームーンの「共通コンポーネントのライブラリを整備」の行の数字", "ランタンの実績"]
    assert all(v == "" for v in env["personal"].values()) and env["markdown"].startswith("# [氏名]")
