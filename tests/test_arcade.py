"""レジュメ・アーケード(docs/prd/resume-arcade.md の (2/3))の台本テスト。実 API なし、AI も使わない(簡易分析だけ)。

- ハブ(/liff/arcade)と 8 本のゲーム(/liff/arcade/{game})は、普通の HTML(doctype、lang="ja"、charset、viewport)で返り、
  claude.ai の URL と window.claude の呼び出しを含まない。知らない名前は 404
- 日本語の判定の共通部品(line_runtime/static/arcade/arcade-ja.js)は node のテストで確かめる(node が無ければ飛ばす)
- 画面を実際に押すテスト(Playwright)は、playwright と Chromium が入っているときだけ動く。最初の 2 本を見本の人物で、
  390px の幅で、題名から書類の画面まで遊び、JS のエラーが 0 件で、書類に数字の空欄が出ることを見る
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tests.test_talk_page import app  # noqa: F401  app はフィクスチャ

ROOT = Path(__file__).resolve().parents[1]
GAMES = ["the-case-of-you", "harvest-of-work", "career-quest", "dungeon-of-past-jobs", "career-pet", "career-fighter",
         "deck-of-deeds", "star-log"]
SHIPPED = ["the-case-of-you", "harvest-of-work"]
PLACEHOLDER = "[+X% ← 数字を入れる]"


@pytest.fixture
def client(app):  # noqa: F811
    from fastapi.testclient import TestClient

    return TestClient(app.app)  # lifespan を回さない(ゲームの画面は会話を使わない)


def _is_plain_page(html: str) -> None:
    head = html[:400]
    assert head.startswith("<!doctype html>\n<html lang=\"ja\">")
    assert '<meta charset="utf-8">' in head and '<meta name="viewport"' in head
    assert "</head>\n<body>" in html and html.rstrip().endswith("</body>\n</html>")
    assert "claude.ai" not in html and "claude:viewport" not in html
    assert "window.claude.use" not in html and "window.claude &&" not in html


def test_hub_and_eight_games_are_plain_html_pages_without_claude_ai(client):
    r = client.get("/liff/arcade")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    _is_plain_page(r.text)
    for game in SHIPPED:  # ハブのリンクは aica のルートを指す
        assert f'slug: "{game}", shipped: true' in r.text
    assert '"/liff/arcade/" + g.slug' in r.text
    for game in GAMES:
        r = client.get(f"/liff/arcade/{game}")
        assert r.status_code == 200, game
        _is_plain_page(r.text)
        assert "set-cookie" not in r.headers  # 本人のデータを読み書きしないので、匿名IDの Cookie を発行しない


def test_hub_footer_matches_how_the_games_use_ai_and_storage(client):
    """ハブの注意書きは今の動きに合わせる: 同意したあとは答えを AI の事業者に送る。保存を押した書類はプロフィールに残る。
    事業者は LINE_SIM_PROVIDER で替わるので、静的なページには名前を書かず、ポリシーを指す。"""
    footer = client.get("/liff/arcade").text.split("<footer>")[1].split("</footer>")[0]
    assert "米国の AI の事業者に送ります" in footer and "プライバシーポリシー" in footer
    assert "「プロフィールに保存」を押したときだけ" in footer
    assert "Anthropic" not in footer and "OpenAI" not in footer
    assert "いまはオフライン" not in footer and "ブラウザにだけ残ります" not in footer


def test_unknown_games_and_files_are_404(client):
    for path in ["/liff/arcade/resume-arcade", "/liff/arcade/nope", "/liff/arcade/the-case-of-you.html",
                 "/liff/arcade/..%2Fserver.py", "/static/arcade/nope.js", "/static/arcade/the-case-of-you.html"]:
        assert client.get(path).status_code == 404, path


def test_the_shared_module_is_served_once_and_the_shipped_games_use_it(client):
    r = client.get("/static/arcade/arcade-ja.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "root.ArcadeJa = API" in r.text
    for game in SHIPPED:
        html = client.get(f"/liff/arcade/{game}").text
        assert html.count('<script src="/static/arcade/arcade-ja.js"></script>') == 1
        # 写しを持たない: 数字の検出と言い切りは共通部品だけにある
        assert "const UNIT_JA" not in html and "function plainEnding" not in html and "function isDateish" not in html


def test_the_shipped_games_do_not_ask_for_name_or_contact(client):
    for game in SHIPPED:
        html = client.get(f"/liff/arcade/{game}").text
        assert 'autocomplete="name"' not in html and 'autocomplete="email"' not in html
        assert 'id="f-name"' not in html and 'id="fName"' not in html and 'id="fContact"' not in html
        assert "led-name" not in html and "led-contact" not in html
        assert ".mdでダウンロード" not in html and ".mdをダウンロード" not in html  # ダウンロードは外し、コピーを残す
        assert "Markdownでコピー" in html


def test_the_dev_screen_links_all_eight_games(client):
    html = client.get("/dev").text
    for game in GAMES:
        assert f'href="/liff/arcade/{game}"' in html, game
    assert 'href="/liff/arcade"' in html


def test_shared_japanese_logic_passes_its_node_tests():
    node = shutil.which("node")
    if not node:
        pytest.skip("node が入っていない")
    # spec を明示する: Node 22 までは非 TTY だと tap(`# fail 0`)で出し、下の照合に当たらない
    r = subprocess.run([node, "--test", "--test-reporter=spec", str(ROOT / "tests" / "js" / "arcade-ja.test.js")],
                       check=False, capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert re.search(r"^ℹ fail 0$", r.stdout, re.MULTILINE)


# ----------------------------- 画面を実際に押す(Playwright) -----------------------------

PHONE = {"width": 390, "height": 844}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(app):  # noqa: F811
    """本物の HTTP サーバー(uvicorn)で画面を出す。"""
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(300):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    th.join(10)


@pytest.fixture
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001  Chromium が入っていない環境
            pytest.skip(f"Chromium を起動できない: {e}")
        yield b
        b.close()


def _phone(browser):
    """390px のスマホの幅。動きを減らす設定で、タイプライターと歩きの演出を短くする。
    外の読み込み(Google Fonts)は空で返し、テストがネットにつながらないようにする。JS のエラーを集める。"""
    context = browser.new_context(viewport=PHONE, reduced_motion="reduce", has_touch=True, is_mobile=True)
    context.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"),
                  lambda route: route.fulfill(status=200, content_type="text/css", body=""))
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    page.set_default_timeout(10_000)
    return context, page, errors


def _visible(page, sel: str) -> bool:
    loc = page.locator(sel)
    return loc.count() > 0 and loc.first.is_visible()


def _input_labels(page) -> list[str]:
    return page.evaluate("""() => [...document.querySelectorAll('input,textarea,select')].map(i => {
        const l = i.id && document.querySelector('label[for="' + i.id + '"]');
        return [(l ? l.textContent : ''), i.placeholder || '', i.getAttribute('autocomplete') || '', i.getAttribute('aria-label') || ''].join(' ');
    })""")


# 本人の氏名、連絡先を聞く欄の言い方(「思い出の名前」はゲームの中の思い出の題名なのでよい)
CONTACT_FIELD = re.compile(r"^名前|あなたの名前|対象者の名前|氏名|メール|居住地|連絡先|電話|URL|\b(?:name|email|tel)\b")


def _no_contact_fields(page) -> None:
    labels = _input_labels(page)
    assert labels
    for text in labels:
        assert not CONTACT_FIELD.search(text), text


def test_hub_shows_only_the_two_shipped_games(served, browser):
    context, page, errors = _phone(browser)
    page.goto(served + "/liff/arcade")
    cabs = page.locator("#wall a.cab")
    assert cabs.count() == 2
    assert sorted(cabs.nth(i).get_attribute("href") for i in range(2)) == sorted(f"/liff/arcade/{g}" for g in SHIPPED)
    cabs.filter(has_text="あなたという事件").click()
    page.wait_for_url(served + "/liff/arcade/the-case-of-you")
    page.locator("#btn-sample").wait_for()
    assert errors == []
    context.close()


@pytest.mark.parametrize("game", GAMES[2:])
def test_the_other_six_open_from_the_dev_screen_link_without_js_errors(served, browser, game):
    context, page, errors = _phone(browser)
    page.goto(served + "/dev")
    href = page.locator(f'#arcade-links a[href="/liff/arcade/{game}"]').get_attribute("href")
    page.goto(served + href)
    page.wait_for_load_state("load")
    page.wait_for_timeout(500)
    assert page.title() and errors == [], errors
    context.close()


def test_the_case_of_you_sample_plays_to_the_resume_without_the_rival(served, browser):
    """あなたという事件: 見本の人物で、ライバルに答えずに事件を閉じ、職務経歴書の画面まで行く。"""
    context, page, errors = _phone(browser)
    page.goto(served + "/liff/arcade/the-case-of-you")
    assert "オフライン：AIを使わない簡易分析" in page.locator("#wire-status").inner_text()
    page.locator("#btn-sample").click()
    page.locator("#intro-skip").click()
    page.locator("#scr-intake.on").wait_for()
    _no_contact_fields(page)
    page.locator("#btn-streets").click()
    page.locator("#scr-map.on").wait_for()
    assert "ライバルは任意" in page.locator("#map-hint").inner_text()

    guides: list[str] = []
    deadline = time.monotonic() + 90
    while not _visible(page, "#scr-dossier.on"):
        assert time.monotonic() < deadline, "書類の画面まで行けない: " + " / ".join(guides[-5:])
        if _visible(page, "#guide"):
            guides.append(page.locator("#guide .guide-next").inner_text())
        if _visible(page, "#modal"):
            title = page.locator("#modal-title").inner_text()
            assert title != "薄いファイル", "目安に届く前に閉じようとした"
            page.locator("#modal-btns button").first.click()
        elif _visible(page, "#scr-verdict.on"):
            if _visible(page, "#btn-dossier"):
                page.locator("#btn-dossier").click()
        elif _visible(page, "#scr-interro.on"):
            if "ライバル" in page.locator("#plate-role").inner_text():
                raise AssertionError("案内がライバルへ連れていった")
            if _visible(page, "#ans-wrap") and not page.locator("#btn-testify").is_disabled():
                assert "聞かれていること：" in page.locator("#plain").inner_text() or "もうひと押し：" in page.locator("#plain").inner_text()
                if not page.locator("#answer").input_value() and _visible(page, "#btn-giveup"):
                    page.locator("#btn-giveup").click()
                else:
                    page.locator("#btn-testify").click()
            elif _visible(page, "#after-row") and not page.locator("#btn-next-w").is_disabled():
                page.locator("#btn-next-w").click()
        elif _visible(page, "#scr-map.on"):
            page.locator("#guide .btn").click()
        page.wait_for_timeout(50)

    assert any("事件を閉じ" in g for g in guides)  # 目安に届いたら、案内は事件を閉じるへ
    doc = page.locator("#doc-body").inner_text()
    assert "[氏名]" in doc and "[メール] · [電話] · [居住地] · [リンク]" in doc
    assert PLACEHOLDER in doc and "61%から68%" in doc
    assert "田中ミカ" not in doc  # 名前は書類に載せない(空欄で本人が書く)
    assert "オフライン：AIを使わない簡易分析" in page.locator("#d-mode").inner_text()
    page.wait_for_timeout(300)  # 保存は 150ms 遅れて書かれる
    saved = json.loads(page.evaluate("localStorage.getItem('theCaseOfYou.v1.sample')"))
    rivals = [v["status"] for k, v in saved["interviews"].items() if k.endswith(":rival")]
    assert "done" not in rivals and saved["verdict"]["resume"]["experience"]
    assert page.locator("#btn-copy").is_visible()
    assert errors == [], errors
    context.close()


def test_harvest_of_work_sample_plays_to_the_resume_with_the_guide(served, browser):
    """仕事の収穫: 見本の人物で、「次にやること」のボタンだけで進み、職務経歴書の画面まで行く。"""
    context, page, errors = _phone(browser)
    page.goto(served + "/liff/arcade/harvest-of-work")
    assert "オフライン：AIを使わない簡易分析" in page.locator("#aiChipTitle").inner_text()
    page.locator("#btnPlayMe").click()  # 自分の畑の権利書: 名前と連絡先の欄が無い
    page.locator("#scr-setup.active").wait_for()
    _no_contact_fields(page)
    page.locator("#btnSetupBack").click()
    page.locator("#btnSample").click()
    page.locator("#scr-game.active").wait_for()

    seen = {"plain": False, "checks": False, "preview": False, "draft": False}
    objectives: list[str] = []
    deadline = time.monotonic() + 120
    while not _visible(page, "#resume"):
        assert time.monotonic() < deadline, "書類の画面まで行けない: " + " / ".join(objectives[-5:])
        if _visible(page, "#dlg"):
            if _visible(page, "#dlgForm .plain"):
                seen["plain"] = True
            choices = page.locator("#dlgChoices button")
            if choices.count():
                ta = page.locator("#dlg-answer")
                mika = choices.filter(has_text="ミカの答えを使う")
                if ta.count() and not ta.input_value():
                    if mika.count():
                        mika.click()
                        seen["checks"] |= page.locator("#dlgForm .checks li.ok").count() > 0
                        seen["preview"] |= "職務経歴書ではこうなります" in page.locator("#dlgForm .preview").inner_text()
                    else:
                        choices.filter(has_text="質問をスキップ").click()
                    continue
                main = page.locator("#dlgChoices button.primary, #dlgChoices button.gold")
                (main.first if main.count() else choices.first).click()
        elif _visible(page, "#scr-almanac.active"):
            page.locator("#btnToMarket").click()
        elif _visible(page, "#scr-market.active"):
            page.locator("#btnUnfold").click()
        elif _visible(page, "#scr-game.active") and _visible(page, "#objGo"):
            objectives.append(page.locator("#objText").inner_text())
            if not seen["draft"]:  # 途中で、いつでも下書きを開ける
                page.locator("#btnDraft").click()
                page.locator("#draft").wait_for()
                assert "[氏名]" in page.locator("#draftBody").inner_text()
                page.locator("#draft-close").click()
                seen["draft"] = True
            page.locator("#objGo").click()
        page.wait_for_timeout(80)

    assert seen == {"plain": True, "checks": True, "preview": True, "draft": True}
    assert any("書類を作れる量になりました" in o for o in objectives)
    resume = page.locator("#resume").inner_text()
    assert "[氏名]" in resume and "[メール] · [電話] · [居住地] · [リンク]" in resume
    assert PLACEHOLDER in resume and "森" not in resume.split("\n")[0]
    assert "オフライン：AIを使わない簡易分析" in page.locator("#genNote").inner_text()
    page.locator("#btnEditAnswers").click()  # 台帳(答えを直す)にも名前と連絡先の欄が無い
    page.locator("#ledger").wait_for()
    _no_contact_fields(page)
    assert errors == [], errors
    context.close()


def test_the_case_of_you_never_dead_ends_when_every_question_is_skipped(served, browser):
    """自分の事件で、ライバル以外の証人を全員飛ばしても、案内は「事件を閉じる」の行き止まりにせず、飛ばした質問へ戻す。"""
    context, page, errors = _phone(browser)
    page.goto(served + "/liff/arcade/the-case-of-you")
    page.locator("#btn-play").click()
    page.locator("#intro-skip").click()
    page.locator("#scr-intake.on").wait_for()
    page.locator("#job-co-0").fill("A社")
    page.locator("#btn-streets").click()
    page.locator("#scr-map.on").wait_for()
    for _ in range(3):
        page.locator("#guide .btn", has_text="聞き込みへ").click()
        page.locator("#scr-interro.on").wait_for()
        page.locator("#btn-skip").click()
        page.locator("#after-row").wait_for()
        page.locator("#btn-leave").click()
        page.locator("#scr-map.on").wait_for()
    assert "聞き込みへ" in page.locator("#guide .btn").inner_text()  # 行き止まりにしない
    page.locator("#loc-list .loc", has_text="市役所").click()  # それでも閉じようとすると、答える道を出す
    page.locator("#modal-title", has_text="法廷に出せるものがありません").wait_for()
    page.locator("#modal-btns button", has_text="次の質問に答える").click()
    page.locator("#scr-interro.on").wait_for()
    assert "やっぱり答える" in page.locator("#guide").inner_text()
    page.locator("#btn-redo").click()
    page.locator("#answer").fill("決済画面を作り直し、決済完了率を61%から68%に上げました。")
    page.locator("#btn-testify").click()
    page.locator("#after-row").wait_for()
    assert "職務経歴書 1行" in page.locator("#guide").inner_text()
    assert errors == [], errors
    context.close()


def test_harvest_draft_does_not_open_over_a_dialog(served, browser):
    """仕事の収穫: 会話の途中は「下書き」を開かない(会話の裏で歩いたり、答えが進んだりしない)。会話の後は開いて、Esc で閉じる。"""
    context, page, errors = _phone(browser)
    page.goto(served + "/liff/arcade/harvest-of-work")
    page.locator("#btnSample").click()
    page.locator("#dlg").wait_for()
    page.locator("#btnDraft").click()
    page.wait_for_timeout(200)
    assert page.locator("#draft").is_hidden()
    while page.locator("#dlg").is_visible():
        page.locator("#dlgChoices button.primary").click()
        page.wait_for_timeout(100)
    page.locator("#btnDraft").click()
    page.locator("#draft").wait_for()
    page.keyboard.press("Escape")
    page.locator("#draft").wait_for(state="hidden")
    assert errors == [], errors
    context.close()
