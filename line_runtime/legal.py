"""利用規約とプライバシーポリシーの画面と版(task 207。docs/legal/README.md)。

原文は docs/legal/terms.md と docs/legal/privacy-policy.md の 1 か所だけ。画面(GET /legal/terms、GET /legal/privacy)は、
その Markdown をここで HTML にして出す(原文に使う形だけ: 見出し、段落、箇条書き、表、太字、リンク)。
版(TERMS_VERSION、PRIVACY_VERSION)は、同意の記録(docs/design/privacy.md の 3.6。task 8/12)が残す値。
原文の「版:」の行と同じでなければならない(tests/test_legal.py)。中身を変えたら、原文と一緒にここも上げる。
FastAPI に依存しない。
"""

from __future__ import annotations

import re
from functools import lru_cache
from html import escape
from pathlib import Path

from line_runtime import config

LEGAL_DIR = config.ROOT / "docs" / "legal"

TERMS_VERSION = "1.0"
PRIVACY_VERSION = "1.0"

# 画面の名前 → (題, 原文のファイル, 版)。URL は /legal/<名前>
DOCS = {
    "terms": ("利用規約", "terms.md", TERMS_VERSION),
    "privacy": ("プライバシーポリシー", "privacy-policy.md", PRIVACY_VERSION),
}
URLS = {name: f"/legal/{name}" for name in DOCS}
_FILE_URLS = {file: URLS[name] for name, (_, file, _) in DOCS.items()}  # 原文どうしのリンク → 画面の URL

DEMO_NOTE = "テスト用の公開です。求人は架空のデモで、応募しても実際の応募にはなりません。"


def versions() -> dict[str, str]:
    """今の版(同意の記録に残す値)。"""
    return {name: version for name, (_, _, version) in DOCS.items()}


def path_of(name: str) -> Path:
    return LEGAL_DIR / DOCS[name][1]


def version_in(text: str) -> str | None:
    """原文の「- 版: 1.0」の行の版。無ければ None。"""
    m = re.search(r"^- 版: (\S+)$", text, re.MULTILINE)
    return m.group(1) if m else None


def links_html() -> str:
    """画面の下に置く、規約とポリシーへのリンク(別のタブで開く。開いている画面を離れない)。"""
    return " · ".join(f'<a href="{URLS[name]}" target="_blank" rel="noopener">{title}</a>'
                      for name, (title, _, _) in DOCS.items())


def footer_html() -> str:
    """LIFF の画面の下に置く 1 段(テスト用の公開であることと、規約とポリシーへのリンク)。"""
    return f'<footer class="legal"><p>{DEMO_NOTE}</p><p>{links_html()}</p></footer>'


FOOTER_CSS = (".legal{max-width:480px;margin:24px auto 16px;padding:0 16px;color:#5f6775;font-size:12px;line-height:1.6}"
              ".legal p{margin:4px 0}.legal a{color:#1f5fd1}")


# ----------------------------- Markdown → HTML(原文に使う形だけ) -----------------------------

_LIST = re.compile(r"^( *)(-|\d+\.) (.*)$")
_HEADING = re.compile(r"^(#{1,3}) (.*)$")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _href(url: str) -> str | None:
    """リンク先。http(s) はそのまま、原文どうしは画面の URL。ほか(設計メモなどの相対パス)はリンクにしない。"""
    if url.startswith(("https://", "http://")):
        return url
    return _FILE_URLS.get(url.split("#", 1)[0])


def _link(m: re.Match) -> str:
    text, url = m.group(1), m.group(2)
    href = _href(url.replace("&amp;", "&"))
    if href is None:
        return text
    blank = ' target="_blank" rel="noopener"' if href.startswith("http") else ""
    return f'<a href="{escape(href)}"{blank}>{text}</a>'


def _inline(text: str) -> str:
    s = escape(text, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return _LINK.sub(_link, s)


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _table(rows: list[str]) -> str:
    head, body = _cells(rows[0]), [_cells(r) for r in rows[2:]]  # rows[1] は |---|
    th = "".join(f"<th>{_inline(c)}</th>" for c in head)
    trs = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
    return f'<div class="table"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def _list(lines: list[str]) -> str:
    """箇条書き(- と 1.)。字下げで入れ子にする。印の無い字下げの行は、前の項目の続き。"""
    items: list[list] = []  # [字下げ, タグ, 文]
    for line in lines:
        m = _LIST.match(line)
        if m:
            items.append([len(m.group(1)), "ol" if m.group(2)[0].isdigit() else "ul", m.group(3)])
        elif items:
            items[-1][2] += line.strip()
    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for indent, tag, text in items:
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if stack and indent == stack[-1][0]:
            out.append("</li>")
        else:
            out.append(f"<{tag}>")
            stack.append((indent, tag))
        out.append(f"<li>{_inline(text)}")
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif m := _HEADING.match(line):
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
        elif line.startswith("|"):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            out.append(_table(lines[i:j]))
            i = j
        elif _LIST.match(line):
            j = i + 1
            while j < len(lines) and lines[j].strip() and (_LIST.match(lines[j]) or lines[j].startswith(" ")):
                j += 1
            out.append(_list(lines[i:j]))
            i = j
        else:
            j = i
            while (j < len(lines) and lines[j].strip() and not _HEADING.match(lines[j])
                   and not lines[j].startswith("|") and not _LIST.match(lines[j])):
                j += 1
            out.append(f"<p>{''.join(_inline(x.strip()) for x in lines[i:j])}</p>")
            i = j
    return "\n".join(out)


@lru_cache(maxsize=8)
def _page(name: str, mtime: float) -> str:  # 原文を直したら(mtime が変わったら)描き直す
    title = DOCS[name][0]
    body = to_html(path_of(name).read_text(encoding="utf-8"))
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>aica {title}</title>
<style>body{{margin:0;font-family:-apple-system,"Hiragino Sans",sans-serif;background:#f4f6f8;color:#111;line-height:1.7}}
main{{padding:8px 16px 16px;max-width:720px;margin:auto;font-size:15px}}h1{{font-size:20px}}h2{{font-size:17px;margin-top:28px}}
.table{{overflow-x:auto}}/* 列の多い表はスマホの幅で潰さず、横に送る */table{{border-collapse:collapse;background:#fff;font-size:13.5px}}
th,td{{border:1px solid #d5dae2;padding:6px 8px;text-align:left;vertical-align:top;min-width:7em}}th{{background:#eef1f5}}
a{{color:#1f5fd1}}{FOOTER_CSS}</style>
</head><body><main>{body}</main><footer class="legal"><p>{links_html()}</p></footer></body></html>"""


def page(name: str) -> str:
    """規約かポリシーの画面の HTML。name は DOCS のキー。"""
    path = path_of(name)
    return _page(name, path.stat().st_mtime)
