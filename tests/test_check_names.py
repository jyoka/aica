"""scripts/check_names.py: 実際の小さな git リポジトリを一時ディレクトリに作って、道具をコマンドとして動かす。一覧も架空の語。"""

from __future__ import annotations

import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "check_names.py"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png(*chunks: bytes) -> bytes:
    return PNG_MAGIC + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + b"".join(chunks) + _chunk(b"IEND", b"")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (root / "ok.py").write_text("print('clean')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return root


@pytest.fixture
def names(tmp_path):
    path = tmp_path / "names.txt"
    path.write_text("zorblax\n\n(^|[^a-z0-9])qux([^a-z0-9]|$)\nフィクション社\n", encoding="utf-8")
    return path


def _run(root: Path, names: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), "--list", str(names)], cwd=root, capture_output=True, text=True, timeout=60,
                          check=False)


def test_a_clean_repository_ends_with_zero(repo, names):
    done = _run(repo, names)
    assert done.returncode == 0, done.stdout
    assert "0 件" in done.stdout


def test_a_name_in_a_file_is_reported_by_place_without_printing_the_name(repo, names):
    (repo / "a.py").write_text("first = 1\nNAME = 'Zorblax'\n", encoding="utf-8")  # 大文字と小文字を区別しない
    done = _run(repo, names)
    assert done.returncode == 1
    assert "a.py: line 2: list line 1" in done.stdout
    assert "zorblax" not in done.stdout.lower()


def test_a_name_in_a_file_name_is_reported(repo, names):
    (repo / "zorblax_tools.py").write_text("x = 1\n", encoding="utf-8")
    done = _run(repo, names)
    assert done.returncode == 1
    assert "zorblax_tools.py: file name: list line 1" in done.stdout


def test_a_japanese_name_is_found(repo, names):
    (repo / "ja.md").write_text("これはフィクション社の文書\n", encoding="utf-8")
    done = _run(repo, names)
    assert done.returncode == 1
    assert "ja.md: line 1: list line 4" in done.stdout


def test_a_json_unicode_escape_is_read(repo, names):
    (repo / "e.json").write_text('{"a": "\\u30d5\\u30a3\\u30af\\u30b7\\u30e7\\u30f3\\u793e"}', encoding="utf-8")
    assert "e.json: line 1: list line 4" in _run(repo, names).stdout


def test_a_utf16_binary_is_read(repo, names):
    (repo / "u.dat").write_bytes("zorblax".encode("utf-16-le"))
    assert "u.dat: binary: list line 1" in _run(repo, names).stdout


def test_ignored_files_are_not_read(repo, names):
    (repo / "ignored").mkdir()
    (repo / "ignored" / "x.txt").write_text("zorblax\n", encoding="utf-8")
    assert _run(repo, names).returncode == 0


def test_a_file_deleted_but_not_yet_committed_is_not_reported(repo, names):
    (repo / "zorblax.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    (repo / "zorblax.txt").unlink()
    assert _run(repo, names).returncode == 0


@pytest.mark.parametrize("chunk", [
    _chunk(b"tEXt", b"Comment\0made by zorblax"),
    _chunk(b"zTXt", b"Comment\0\0" + zlib.compress(b"made by zorblax")),
    _chunk(b"iTXt", b"XML\0\0\0\0\0made by zorblax"),
    _chunk(b"iTXt", b"XML\0\x01\0\0\0" + zlib.compress(b"made by zorblax")),
])
def test_a_name_in_png_metadata_is_found(repo, names, chunk):
    (repo / "a.png").write_bytes(_png(chunk))
    done = _run(repo, names)
    assert done.returncode == 1
    assert "a.png: image metadata: list line 1" in done.stdout


def test_bytes_of_the_picture_itself_are_not_read_as_text(repo, names):
    """圧縮された絵のバイト列が、たまたま名前と同じ並びでも当てない。"""
    (repo / "a.png").write_bytes(_png(_chunk(b"IDAT", b"zorblax zorblax")))
    assert _run(repo, names).returncode == 0
    (repo / "a.png").unlink()
    (repo / "b.jpg").write_bytes(b"\xff\xd8" + b"\xff\xda\x00\x02" + b"zorblax" + b"\xff\xd9")
    assert _run(repo, names).returncode == 0


def test_a_name_in_jpeg_metadata_is_found(repo, names):
    comment = b"made by zorblax"
    (repo / "c.jpg").write_bytes(b"\xff\xd8\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment + b"\xff\xda\x00\x02\xff\xd9")
    done = _run(repo, names)
    assert done.returncode == 1
    assert "c.jpg: image metadata: list line 1" in done.stdout


def test_a_name_in_pdf_information_is_found(repo, names):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.add_metadata({"/Title": "made by zorblax"})
    with open(repo / "d.pdf", "wb") as f:
        writer.write(f)
    done = _run(repo, names)
    assert done.returncode == 1
    assert "d.pdf: PDF: list line 1" in done.stdout


def test_an_unreadable_list_and_a_broken_pattern_end_with_two(repo, tmp_path):
    assert _run(repo, tmp_path / "missing.txt").returncode == 2
    broken = tmp_path / "broken.txt"
    broken.write_text("(unclosed\n", encoding="utf-8")
    assert _run(repo, broken).returncode == 2
    empty = tmp_path / "empty.txt"
    empty.write_text("\n", encoding="utf-8")
    assert _run(repo, empty).returncode == 2


def test_the_tool_itself_holds_no_list():
    """道具は一覧を引数で受け取るだけ(既定の場所も持たない)。"""
    source = TOOL.read_text(encoding="utf-8")
    assert "banned" not in source and ".config" not in source
