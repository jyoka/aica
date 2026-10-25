"""利用規約とプライバシーポリシー(task 207。docs/legal/README.md)の台本テスト。実 LLM なし、実 API なし。

- 原文(docs/legal/)に【】が残っておらず、原文の「版」が line_runtime.legal の版の定数と同じ(同意の記録が残す値。task 8/12)
- GET /legal/terms と GET /legal/privacy が原文を HTML にして出す。Markdown の印は残らず、原文どうしのリンクは画面の URL になる。
  Cookie を発行しない。知らない名前は 404
- Web のトーク画面と LIFF の画面(_page の画面、LINE の音声相談の画面、アーケードのハブ)から、規約とポリシーを開ける。
  dip のログインの画面(aica ではない役の画面)には置かない
- 応募フォームの体験: 欄に架空の値が最初から入り、架空の求人であることと「本物の情報は入れない」が出る。送った値は保存しない
- ブラウザ(Playwright)で、トーク画面のリンクを押すと規約とポリシーが別のタブで開く
"""

from __future__ import annotations

import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from dip_mcp import logic
from line_runtime import legal, line_channel
from tests.test_arcade import _free_port, browser  # noqa: F401  browser はフィクスチャ
from tests.test_revisit import Greeter
from tests.test_talk_page import app  # noqa: F401  app はフィクスチャ

MARKDOWN_LEFT = ("|---", "**", "](", "\n- ", "\n# ", "\n## ", "【", "】")


def test_the_originals_have_no_blanks_and_carry_the_version_constants():
    assert legal.versions() == {"terms": legal.TERMS_VERSION, "privacy": legal.PRIVACY_VERSION}
    for name in legal.DOCS:
        text = legal.path_of(name).read_text(encoding="utf-8")
        assert "【" not in text and "】" not in text, name
        assert legal.version_in(text) == legal.versions()[name], name
        assert "contact@example.com" in text or name == "terms"  # 窓口は架空のアドレス(実在のアドレスを書かない)
        assert "テストのために公開" in text and "架空のデモ" in text, name  # 冒頭に、商用でないことと架空の求人


def test_the_legal_pages_render_the_originals(app):  # noqa: F811
    client = TestClient(app.app)  # lifespan を回さない(会話を使わない)
    for name, (title, _, version) in legal.DOCS.items():
        r = client.get(legal.URLS[name])
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), name
        assert "set-cookie" not in r.headers
        html = r.text
        assert f"<title>aica {title}</title>" in html and f"<h1>aica {title}</h1>" in html
        assert f"版: {version}" in html
        body = html.split("<main>", 1)[1].split("</main>", 1)[0]
        assert [m for m in MARKDOWN_LEFT if m in body] == [], name
        assert ("<ol>" if name == "terms" else "<table>") in body, name
    terms, policy = client.get("/legal/terms").text, client.get("/legal/privacy").text
    assert '<a href="/legal/privacy">プライバシーポリシー</a>' in terms  # 原文の privacy-policy.md へのリンク
    assert "<td>OpenAI, L.L.C.</td>" in policy and "contact@example.com" in policy
    assert ('<a href="https://www.ppc.go.jp/enforcement/infoprovision/laws/offshore_report_america/" target="_blank" '
            'rel="noopener">外国制度(アメリカ合衆国)</a>') in policy
    assert "<li>運営者は、本サービスを終えることがあります。そのときは、30 日前までに" in terms
    assert client.get("/legal/README").status_code == 404 and client.get("/legal/terms.md").status_code == 404


def test_markdown_subset():
    html = legal.to_html("# 題\n\n本文の 1 行目。\n2 行目。\n\n1. 一\n   - 入れ子\n     続き\n2. 二\n\n"
                         "| a | b |\n|---|---|\n| **x** | [ポリシー](privacy-policy.md#13) |\n\n"
                         "[設計](../design/privacy.md) と <script>")
    assert html.split("\n") == [
        "<h1>題</h1>",
        "<p>本文の 1 行目。2 行目。</p>",
        "<ol><li>一<ul><li>入れ子続き</li></ul></li><li>二</li></ol>",
        ('<div class="table"><table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td><strong>x</strong></td>'
         '<td><a href="/legal/privacy">ポリシー</a></td></tr></tbody></table></div>'),
        "<p>設計 と &lt;script&gt;</p>",  # 画面に無い文書へはリンクしない。HTML は文字として出す
    ]


def _links(html: str) -> bool:
    return all(f'href="{url}" target="_blank" rel="noopener"' in html for url in legal.URLS.values())


def test_the_talk_screen_and_liff_screens_link_to_the_terms_and_policy(app, monkeypatch):  # noqa: F811
    client = TestClient(app.app)
    talk = client.get("/").text
    assert _links(talk) and legal.DEMO_NOTE in re.sub(r"\s+", "", talk)  # talk.html の 1 行は legal.DEMO_NOTE と同じ文
    assert _links(client.get("/liff/arcade").text)
    page = app._page("履歴書の読み取り結果", "<p>本文</p>")
    assert _links(page) and legal.DEMO_NOTE in page
    assert not _links(client.get("/dip/login", params={"linkToken": "x" * 20}).text)  # dip の画面には置かない
    # LINE のアプリで開く LIFF 画面(殻と音声相談)
    monkeypatch.setenv(line_channel.LIFF_ID_ENV, "2000000000-OfflineT")
    monkeypatch.setenv(line_channel.LIFF_CHANNEL_ENV, "2000000000")
    assert _links(client.get("/liff/line/voice").text)  # 本人の Cookie が無いので殻(_page)
    assert _links(app._liff_voice_page("l-" + "0" * 16).body.decode())


def test_the_apply_form_is_a_demo_with_made_up_values_that_are_not_kept(app, tmp_path):  # noqa: F811
    with TestClient(app.app) as client:
        client.get("/")
        user = client.post("/api/scenarios/B-exit/start").json()["user"]
        exit_ = logic.form_exit(app.state, user)
        job = exit_["job_id"]
        form = client.get("/liff/apply", params={"job": job, "scenario_user": "sim-exit"})
        assert form.status_code == 200
        assert app.APPLY_DEMO_NOTE in form.text and _links(form.text)
        for field in exit_["remaining_fields"]:
            value = app.DEMO_FORM_VALUES.get(field, app.DEMO_FORM_OTHER)
            assert f'name="f_{field}" required value="{value}"' in form.text
        assert "placeholder" not in form.text
        marker = "090-0000-0000"  # 架空の目印の値。本人が書き換えて送っても、どこにも残らない
        sent = {"job": job, "scenario_user": "sim-exit"} | {f"f_{f}": f"{marker} {f}" for f in exit_["remaining_fields"]}
        done = client.post("/liff/apply", data=sent)
        assert done.status_code == 200 and app.APPLY_DONE_TEXT in done.text
        assert logic.form_exit(app.state, user) is None  # 応募の確定だけは dip の状態に書く
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert marker.encode() not in f.read_bytes(), f


@pytest.fixture
def served(app):  # noqa: F811
    import uvicorn

    app.set_model(Greeter())
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


def test_the_talk_screen_opens_the_terms_and_policy_in_a_browser(served, browser):  # noqa: F811
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.set_default_timeout(10_000)
    try:
        page.goto(served + "/")
        bar = page.locator("#legal-bar")
        assert "架空のデモ" in bar.inner_text()
        for title, heading in (("利用規約", "aica 利用規約"), ("プライバシーポリシー", "aica プライバシーポリシー")):
            with page.context.expect_page() as opened:
                bar.get_by_role("link", name=title).click()
            doc = opened.value
            doc.wait_for_load_state()
            assert doc.locator("h1").inner_text() == heading
            assert doc.locator("ol" if title == "利用規約" else "table").count() >= 1
            doc.close()
        assert page.url == served + "/"  # トーク画面は離れない
    finally:
        page.close()
