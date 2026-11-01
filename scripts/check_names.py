"""リポジトリに、一覧の名前(1 行に 1 つの正規表現)が残っていないかを調べる。名前そのものは、この道具にも出力にも持たない。

    ./.venv/bin/python scripts/check_names.py --list <一覧のファイル>

調べるもの: git が追跡しているファイルと、まだ追加していないファイル(.gitignore の対象を除く)の名前と中身。
画像(PNG、JPEG、GIF、WebP)は、圧縮された絵のバイト列は見ずに、メタデータの文字だけを見る。PDF は情報と本文の文字を見る。
大文字と小文字は区別しない。見つかった場所と、一覧の何行目に当たったかを出す(ログに名前が残らないように、当たった文字は出さない)。
テキストは UTF-8 と JSON の \\uXXXX を読む。ほかの文字コード、base64、圧縮ファイルの中、2 つに分けて書いた名前、絵に写った文字、git の履歴は見ない
(画像に写った名前は目で確かめる)。

終わり方: 0 = 見つからない、1 = 見つかった、2 = 一覧が読めない・正規表現がまちがっている・git が使えない。
一覧はリポジトリの外にあるので、CI では動かさない。PR の前に手元で動かす(CONTRIBUTING.md)。
"""

from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
import zlib
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG_TEXT_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"iCCP"}
UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
JPEG_METADATA = {0xFE} | set(range(0xE0, 0xF0))  # COM と APPn(EXIF、XMP、IPTC など)


def _decoded(raw: bytes) -> list[str]:
    """メタデータのバイト列を文字にする。EXIF の一部は UTF-16 なので両方の読み方を返す。"""
    return [raw.decode("utf-8", "ignore"), raw.decode("utf-16-le", "ignore"), raw.decode("utf-16-be", "ignore")]


def _png_metadata(data: bytes) -> Iterator[bytes]:
    at = len(PNG_MAGIC)
    while at + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[at:at + 8])
        body = data[at + 8:at + 8 + size]
        if kind in PNG_TEXT_CHUNKS:
            if kind == b"zTXt":
                keyword, _, rest = body.partition(b"\0")
                yield keyword
                yield _inflate(rest[1:])
            elif kind == b"iTXt":
                keyword, _, rest = body.partition(b"\0")
                compressed, rest = rest[:1], rest[2:]
                yield keyword
                yield _inflate(rest) if compressed == b"\1" else rest
            else:
                yield body
        at += 12 + size


def _inflate(raw: bytes) -> bytes:
    """iTXt は言語のタグのあとに本文が続く。zlib の始まりを探してほどき、ほどけなければそのまま返す。"""
    for start in range(len(raw)):
        if raw[start:start + 1] == b"\x78":
            try:
                return raw[:start] + zlib.decompress(raw[start:])
            except zlib.error:
                continue
    return raw


def _jpeg_metadata(data: bytes) -> Iterator[bytes]:
    at = 2
    while at + 4 <= len(data) and data[at] == 0xFF:
        marker = data[at + 1]
        if marker == 0xDA:  # ここから先は絵
            return
        size = struct.unpack(">H", data[at + 2:at + 4])[0]
        if marker in JPEG_METADATA:
            yield data[at + 4:at + 2 + size]
        at += 2 + size


def _gif_metadata(data: bytes) -> Iterator[bytes]:
    """注釈の拡張(0x21 0xFE)と、アプリケーションの拡張(0x21 0xFF)の文字。"""
    for marker in (b"\x21\xfe", b"\x21\xff"):
        at = data.find(marker)
        while at != -1:
            cur, parts = at + 2, []
            while cur < len(data) and data[cur]:
                parts.append(data[cur + 1:cur + 1 + data[cur]])
                cur += 1 + data[cur]
            yield b"".join(parts)
            at = data.find(marker, cur)


def _webp_metadata(data: bytes) -> Iterator[bytes]:
    at = 12
    while at + 8 <= len(data):
        kind, size = struct.unpack("<4sI", data[at:at + 8])
        if kind in (b"EXIF", b"XMP "):
            yield data[at + 8:at + 8 + size]
        at += 8 + size + (size & 1)


def _pdf_text(data: bytes) -> Iterator[str]:
    """情報(タイトルなど)と本文の文字。pypdf(requirements.txt)が読めないときは、圧縮されていない部分の文字だけを見る。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(data))
        for value in (reader.metadata or {}).values():
            yield str(value)
        if reader.xmp_metadata is not None and reader.xmp_metadata.stream is not None:
            yield reader.xmp_metadata.stream.get_data().decode("utf-8", "ignore")
        for page in reader.pages:
            yield page.extract_text() or ""
    except Exception as e:  # noqa: BLE001  読めない PDF で止めず、見た範囲を狭めて続ける
        print(f"warning: PDF を読めないので、圧縮されていない部分だけを調べます ({type(e).__name__})", file=sys.stderr)
        yield data.decode("latin-1")


def texts_of(data: bytes) -> Iterator[tuple[str, str]]:
    """ファイルの中身から調べる文字を返す。(場所の説明, 文字)。"""
    if data.startswith(PNG_MAGIC):
        metadata = _png_metadata(data)
    elif data.startswith(b"\xff\xd8"):
        metadata = _jpeg_metadata(data)
    elif data.startswith((b"GIF87a", b"GIF89a")):
        metadata = _gif_metadata(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        metadata = _webp_metadata(data)
    elif data.startswith(b"%PDF"):
        for text in _pdf_text(data):
            yield "PDF", text
        return
    else:
        if b"\0" in data:  # 種類の分からないバイナリ
            for text in _decoded(data):
                yield "binary", text
            return
        for number, line in enumerate(data.decode("utf-8", "ignore").splitlines(), 1):
            yield f"line {number}", line
            if "\\u" in line:  # JSON の \\uXXXX で書いた文字(json.dumps の既定)も読む
                yield f"line {number}", UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), line)
        return
    for raw in metadata:
        for text in _decoded(raw):
            yield "image metadata", text


def load_patterns(path: Path) -> list[tuple[int, re.Pattern]]:
    """(一覧の行番号, 正規表現)。空の行は飛ばす。"""
    patterns = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            patterns.append((number, re.compile(line.strip(), re.IGNORECASE)))
    return patterns


def repo_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         cwd=root, capture_output=True, check=True).stdout
    return sorted({p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p})


def find_names(root: Path, patterns: list[tuple[int, re.Pattern]]) -> list[str]:
    """見つかった場所(「path: 場所: 一覧の n 行目」)の一覧。消してまだ commit していないファイルは見ない。"""
    found = []
    for name in repo_files(root):
        path = root / name
        if not path.is_file():
            continue
        found += [f"{name}: file name: list line {n}" for n, p in patterns if p.search(name)]
        for where, text in texts_of(path.read_bytes()):
            found += [f"{name}: {where}: list line {n}" for n, p in patterns if p.search(text)]
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="一覧の名前がリポジトリに残っていないかを調べる")
    ap.add_argument("--list", required=True, type=Path, help="一覧のファイル(1 行に 1 つの正規表現)")
    ap.add_argument("--root", type=Path, default=Path.cwd(), help="調べるリポジトリ(既定は今のディレクトリ)")
    args = ap.parse_args(argv)
    try:
        patterns = load_patterns(args.list.expanduser())
        found = find_names(args.root.resolve(), patterns)
    except (OSError, re.error, subprocess.CalledProcessError) as e:
        print(f"error: {type(e).__name__}: 一覧かリポジトリを読めません", file=sys.stderr)
        return 2
    if not patterns:
        print("error: 一覧に正規表現がありません", file=sys.stderr)
        return 2
    for line in found:
        print(line)
    print(f"{len(found)} 件")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
