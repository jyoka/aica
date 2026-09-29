"""トークからの履歴書アップロード(resume-intake の移植, task 63)。実 API は呼ばない。

アップロード → 読み取り(OpenAI はスタブ)→ dip の状態に保存 → 読み取り結果の確認ページ、と
保存した読み取り結果を同じユーザーの会話が get_user_conversation_summary で参照することを確かめる。
個人情報(personal・電話・メール・郵便番号・ファイル名)がどこにも残らないことも見る。
"""

from __future__ import annotations

import asyncio
import json
import os
from html import escape
from types import SimpleNamespace

import pytest

from dip_mcp import logic
from dip_mcp.data import State
from resume_intake.schema import PERSONAL_KEYS, ResumeDraft
from tests.test_handle import ScriptedModel, call, reply
from tests.test_resume_intake import CLEAN, StubClient

PII = ("090-0000-1111", "misaki.sato@example.com", "100-0000", "03-1234-5678")
FILENAME = "佐藤美咲_履歴書.pdf"  # ファイル名には氏名が入りがち。保存しない
U1 = "w-1111111111111111:sim-new"
U2 = "w-2222222222222222:sim-new"


def clean_draft() -> ResumeDraft:
    return ResumeDraft.model_validate(json.loads(CLEAN.read_text(encoding="utf-8")))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DIP_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test")  # モデルは作るが呼ばない
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test")
    from line_runtime import config, server

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "state", State(tmp_path / "state.json"))
    server.state.reset()
    stub = StubClient(clean_draft(), usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    monkeypatch.setattr(server, "resume_client", lambda: stub)
    server.stub = stub
    return server


def _upload(client, user, name=FILENAME, data=b"%PDF-1.4 dummy"):
    return client.post(f"/api/resume/{user}/upload", files={"file": (name, data, "application/octet-stream")})


# ----------------------------- アップロード → 確認 -----------------------------


def test_upload_reads_saves_and_shows_the_result(app, tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        client.get("/api/me")
        user = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        r = _upload(client, user)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["empty"] is False
        assert body["view_url"] == f"/liff/resume?user={user}"
        # スタブに PDF がそのまま渡っている(ファイルはディスクに置かずバイト列で読む)
        part = app.stub.calls[0]["input"][0]["content"][0]
        assert part["type"] == "input_file" and part["file_data"].startswith("data:application/pdf;base64,")
        # 応答にも個人情報は無い(トークに出す確認の材料)
        assert all(v == "" for v in body["result"]["personal"].values())
        assert not any(p in r.text for p in PII)
        assert body["result"]["resume"]["work_history"][0]["organization"] == "コンビニみほん駅前店"

        page = client.get(body["view_url"])
        assert page.status_code == 200
        assert "コンビニみほん駅前店" in page.text and "みほん県立第一高等学校" in page.text
        assert not any(p in page.text for p in PII)
        # 左の「dip から見た状態」にも出る
        assert client.get(f"/api/dip/{user}").json()["summary"]["uploaded_resume"]["uploaded"] == "9/23"
        # 費用台帳にアップロード分の行が付く(誰の利用か分かる)
        rows = [json.loads(x) for x in (tmp_path / "costs.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["event"] == "resume_upload" and rows[-1]["user_id"] == user and rows[-1]["cost_usd"] > 0

    saved = (tmp_path / "state.json").read_text(encoding="utf-8")
    assert not any(p in saved for p in PII)
    assert FILENAME not in saved and "佐藤" not in saved


def test_upload_rejects_unsupported_files_and_other_users(app):
    from fastapi.testclient import TestClient

    with TestClient(app.app) as a:
        a.get("/api/me")  # 画面を開いたブラウザ(Cookie あり)
        mine = a.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert _upload(a, mine, name="resume.docx").status_code == 400
        assert _upload(a, mine, data=b"").status_code == 400
        b = TestClient(app.app)  # 別のブラウザ
        b.get("/api/me")
        other = b.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert _upload(a, other).status_code == 403  # 他人のトークに履歴書を差し込めない
        assert a.get("/liff/resume", params={"user": other}).status_code == 403
        assert a.get("/liff/resume", params={"user": mine}).status_code == 404  # まだアップロードしていない
    assert app.stub.calls == []  # どれも読み取り(API)まで行かない


def test_upload_counts_against_the_daily_quota(app, monkeypatch):
    """アップロードも1ターン扱い: 上限に達したら OpenAI を呼ばずに案内だけ返す(費用の防波堤を迂回させない)。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LINE_SIM_DAILY_TURN_LIMIT", "1")
    with TestClient(app.app) as client:
        client.get("/api/me")
        user = client.post("/api/scenarios/F-follow-new/start").json()["user"]
        assert _upload(client, user).status_code == 200  # 1回目は読み取れて、ターンを1つ使う
        assert app.app.state.quota.used(user) == 1
        r = _upload(client, user)
        assert r.status_code == 429
        assert "上限" in r.json()["detail"]
    assert len(app.stub.calls) == 1  # 2回目は読み取り(API)まで行かない


def test_unreadable_upload_is_marked_empty_and_keeps_the_earlier_result(app, monkeypatch):
    """読めなかったファイルは「読み取れなかった」と返すだけで、前に読み取れた結果を上書きしない。"""
    from fastapi.testclient import TestClient

    with TestClient(app.app) as client:
        assert _upload(client, "sim-new").json()["empty"] is False
        monkeypatch.setattr(app, "resume_client",
                            lambda: StubClient(ResumeDraft(unconfirmed=["画像が不鮮明で読み取れなかった"])))
        r = _upload(client, "sim-new", name="scan.jpg", data=b"\xff\xd8 dummy")
        assert r.status_code == 200 and r.json()["empty"] is True and r.json()["view_url"] is None
        assert r.json()["result"]["resume"]["unconfirmed"] == ["画像が不鮮明で読み取れなかった"]
        kept = logic.resume_upload(app.state, "sim-new")["resume"]
        assert kept["work_history"][0]["organization"] == "コンビニみほん駅前店"


# ----------------------------- 保存の防御(読み取り側に重ねる) -----------------------------


def test_save_resume_upload_blanks_personal_and_scrubs_pii(tmp_path):
    """読み取り側をすり抜けた値が来ても、dip に保存する時点で personal は空欄・文字列中の連絡先は消える。"""
    state = State(tmp_path / "state.json")
    leaked = {"format_version": "1.0", "source": "upload", "created_at": "2026-09-23T12:00:00+09:00",
              "personal": {"name": "佐藤 美咲", "phone": "090-0000-1111", "email": "misaki.sato@example.com"},
              "resume": clean_draft().model_dump()}
    rec = logic.save_resume_upload(state, U1, leaked)
    assert set(rec["personal"]) == set(PERSONAL_KEYS) and all(v == "" for v in rec["personal"].values())
    dumped = json.dumps(state.user(U1), ensure_ascii=False)
    assert not any(p in dumped for p in PII) and "佐藤" not in dumped
    assert "レジ" in dumped


# ----------------------------- 同じユーザーの会話から参照される -----------------------------


def test_uploaded_resume_is_referenced_in_the_same_users_conversation(tmp_path):
    """アップロードした読み取り結果は、同じユーザーの会話の要約(uploaded_resume)に載り、別ユーザーには載らない。
    dip の MCP は本物(子プロセス)、LLM は決められた順に返事をする偽物。"""
    os.environ["DIP_STATE_PATH"] = str(tmp_path / "state.json")
    from line_runtime.runtime import LineRuntime

    state = State(tmp_path / "state.json")
    logic.save_resume_upload(state, U1, {"format_version": "1.0", "resume": clean_draft().model_dump()})

    model = ScriptedModel(script=[
        call("get_user_conversation_summary", {"line_user_id": U1}),
        reply(["履歴書ではコンビニでレジと品出しをされていたんですね。近い仕事で探しますか?"]),
        call("get_user_conversation_summary", {"line_user_id": U2}),
        reply(["どんなお仕事を探していますか?"]),
    ])

    async def go():
        async with LineRuntime(model=model, in_memory=True) as rt:
            r1 = await rt.handle(U1, "message", "履歴書をアップロードしました。この内容をふまえて相談させてください。")
            r2 = await rt.handle(U2, "message", "こんにちは")
            return r1, r2

    r1, r2 = asyncio.run(go())
    mine = r1["tools"][0]["result"]["uploaded_resume"]
    assert mine["work_history"][0]["organization"] == "コンビニみほん駅前店"
    assert mine["work_history"][0]["duties"][:2] == ["レジ", "品出し"]
    assert mine["skills"] == ["接客"] and mine["uploaded"] == "9/23"
    assert "other_experience" not in mine  # 空の項目は要約に載せない
    assert not any(p in json.dumps(r1["tools"], ensure_ascii=False) for p in PII)
    assert "uploaded_resume" in r1["tools"][0]["result"]["note"]  # 使い方の注意も一緒に渡る
    assert r2["tools"][0]["result"]["uploaded_resume"] is None  # 別ユーザーには出ない


def test_prompt_tells_the_agent_to_use_the_uploaded_resume():
    from dip_mcp.server import baitoru_assistant

    for entry in ("A", "visit", "revisit", "D"):
        text = baitoru_assistant(entry)
        assert "uploaded_resume" in text and "[イベント: 本人が履歴書(PDF)をアップロード]" in text
        # いつ履歴書・音声を差し出すか(返信の actions)の決まりがある
        assert "resume_upload" in text and "voice_consult" in text and "文字で話す" in text


# ----------------------------- トークに出す確認は要点だけ(task 89) -----------------------------

# 架空の人物の職務経歴書(スキル欄が長い)。スキル欄を全部つなぐと数百字になる
LONG_SKILLS = [
    "プログラミング言語: Python(Django、FastAPI、pandas)、TypeScript(React、Next.js)、Go、SQL を業務で5年以上使用",
    "クラウド: AWS(EC2、ECS、Lambda、RDS、S3、CloudFront)の設計・構築・運用、Terraform による IaC 化",
    "データ基盤: BigQuery と dbt を使った分析基盤の構築、Airflow によるワークフロー管理と監視の整備",
    "チームマネジメント: 5〜8名のチームのリーダーとして採用・育成・評価・1on1 を担当",
    "Microsoft Office(Word、Excel、PowerPoint)で社内資料・マニュアルを作成",
]


def _long_resume(**kw) -> dict:
    d = clean_draft().model_dump()
    d["skills"] = LONG_SKILLS
    d["work_history"] = [dict(d["work_history"][0], organization=o, employment_type=t) for o, t in (
        ("みほんシステムズ株式会社", "正社員"), ("架空データ合同会社", "契約社員"),
        ("コンビニみほん駅前店", "アルバイト"), ("みほん書店", "アルバイト"))]
    d["certifications"] = [{"name": n, "acquired": ""} for n in (
        "基本情報技術者", "応用情報技術者", "AWS 認定ソリューションアーキテクト - アソシエイト", "普通自動車第一種運転免許")]
    d.update(kw)
    return d


def test_confirm_bubbles_stay_short_for_a_long_resume():
    from line_runtime.resume_confirm import resume_bubbles

    assert len("、".join(LONG_SKILLS)) > 300  # 前の出し方だとこれが1つの吹き出しになっていた
    for unconfirmed in ([], ["職歴2社目の在籍期間の終わり"], ["職歴2社目の在籍期間の終わり", "資格の取得年", "学歴の卒業年"]):
        bubbles = resume_bubbles(_long_resume(unconfirmed=unconfirmed))
        text = "\n".join(bubbles)
        assert len(bubbles) <= 3 and all(len(b) <= 80 for b in bubbles), bubbles
        # 職歴は勤め先と雇用形態、スキルは見出しだけ、資格は名前。超えた分は「ほか N 件」
        assert "みほんシステムズ株式会社・正社員" in text and "職歴" in text
        assert "スキル: プログラミング言語、クラウド、データ基盤 ほか 2 件" in text
        assert "基本情報技術者" in text and "Django" not in text
    bubbles = resume_bubbles(_long_resume(unconfirmed=[]))
    text = "\n".join(bubbles)
    assert "ほか 1 件" in text  # 職歴4件・資格4件のうち3件を出して残りは件数
    assert "コンビニみほん駅前店・アルバイト" in text and "みほん書店" not in text
    assert "資格: 基本情報技術者、応用情報技術者" in text and "普通自動車" not in text


def test_confirm_bubbles_show_the_first_unconfirmed_point():
    from line_runtime.resume_confirm import resume_bubbles

    one = resume_bubbles(_long_resume(unconfirmed=["職歴2社目の在籍期間の終わり"]))
    assert one[-1] == "読み取れなかった点: 職歴2社目の在籍期間の終わり"
    many = resume_bubbles(_long_resume(unconfirmed=["職歴2社目の在籍期間の終わり", "資格の取得年", "学歴の卒業年"]))
    assert many[-1].startswith("読み取れなかった点: 職歴2社目の在籍期間の終わり")
    assert "ほか 2 件は「読み取り結果を見る」で確認できます" in many[-1]
    long_point = resume_bubbles(_long_resume(unconfirmed=["あ" * 200, "い"]))
    assert len(long_point[-1]) <= 80 and "ほか 1 件" in long_point[-1]
    empty = resume_bubbles(ResumeDraft(unconfirmed=["画像が不鮮明で読み取れなかった"]).model_dump(), empty=True)
    assert "読み取れた項目がありませんでした" in empty[0] and empty[1] == "画像が不鮮明で読み取れなかった"


def test_confirm_bubbles_for_a_short_resume():
    from line_runtime.resume_confirm import resume_bubbles

    bubbles = resume_bubbles(clean_draft().model_dump())
    assert len(bubbles) <= 3 and all(len(b) <= 80 for b in bubbles)
    assert "職歴: コンビニみほん駅前店" in bubbles[0] and "スキル: 接客" in "\n".join(bubbles)


def test_upload_response_carries_short_bubbles_and_the_full_view_is_unchanged(app, monkeypatch):
    """アップロードの応答に確認の吹き出し(要点)が載り、「読み取り結果を見る」は全文のまま。"""
    from fastapi.testclient import TestClient

    long = ResumeDraft.model_validate(_long_resume(unconfirmed=["職歴2社目の在籍期間の終わり", "資格の取得年"]))
    monkeypatch.setattr(app, "resume_client", lambda: StubClient(long))
    with TestClient(app.app) as client:
        body = _upload(client, "sim-new").json()
        assert len(body["bubbles"]) <= 3 and all(len(b) <= 80 for b in body["bubbles"])
        assert "読み取れなかった点: 職歴2社目の在籍期間の終わり" in body["bubbles"][-1]
        assert not any(p in json.dumps(body["bubbles"], ensure_ascii=False) for p in PII)
        assert body["result"]["resume"]["skills"] == LONG_SKILLS  # 保存内容は全文のまま
        page = client.get(body["view_url"]).text
        assert all(escape(s) in page for s in LONG_SKILLS) and "みほん書店" in page and "資格の取得年" in page


def test_confirm_bubbles_never_drop_a_section():
    """職歴・スキル・資格は空でなければ必ず出る(収まらなくても「資格 N 件」の形で)。順番も崩れない。"""
    import random

    from line_runtime.resume_confirm import resume_bubbles

    d = clean_draft().model_dump()
    d["work_history"] = [dict(d["work_history"][0], organization=o, employment_type="正社員") for o in (
        "株式会社みほんコンサルティング", "みほん情報システム株式会社", "架空ソリューションズ株式会社", "みほん商事株式会社")]
    d["skills"] = ["要件定義・基本設計・詳細設計: 業務系", "Java・Spring Boot による開発", "チームリーダーとして5名をまとめた"]
    d["certifications"] = [{"name": n, "acquired": ""} for n in ("応用情報技術者試験", "基本情報技術者試験")]
    d["unconfirmed"] = ["2社目の退職年月"]
    text = "\n".join(resume_bubbles(d))
    assert "資格" in text and text.index("職歴") < text.index("スキル") < text.index("資格")

    rng = random.Random(89)
    words = "みほん架空株式会社情報システム開発運用設計ABCDEFG営業接客販売管理"
    word = lambda n: "".join(rng.choice(words) for _ in range(rng.randint(1, n)))
    for _ in range(300):
        d = clean_draft().model_dump()
        d["work_history"] = [dict(d["work_history"][0], organization=word(20), employment_type=word(5))
                             for _ in range(rng.randint(0, 6))]
        d["skills"] = [word(60) for _ in range(rng.randint(0, 8))]
        d["certifications"] = [{"name": word(30), "acquired": ""} for _ in range(rng.randint(0, 6))]
        d["unconfirmed"] = [word(120) for _ in range(rng.randint(0, 4))]
        bubbles = resume_bubbles(d)
        assert len(bubbles) <= 3 and all(len(b) <= 80 for b in bubbles), bubbles
        text = "\n".join(bubbles)
        present = [label for label, key in (("職歴", "work_history"), ("スキル", "skills"), ("資格", "certifications"))
                   if d[key]]
        assert all(label in text for label in present), (present, bubbles)
        assert [text.index(label) for label in present] == sorted(text.index(label) for label in present)
