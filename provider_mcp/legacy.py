"""旧い名前のデータを新しい名前に移す(docs/deploy/render.md の「旧い名前からの移行」)。起動のたびに、データを使う前に呼ぶ。

旧い名前はコードに書かない。環境変数 AICA_LEGACY_PREFIX で受け取り、状態のファイル(state.json)のあるディレクトリの次を移す。

- その接頭辞のファイル: `<接頭辞>_events.db` → `provider_events.db`
- SQLite の列と、列の中の JSON のキー(要約の 1 行など)。fixtures から入った会員のログインID のドメインも新しい名前にする
- JSON ファイルのキー(state.json の中の、書いた分など)と、コードが比べる値(まだ AI に渡していない、連携が変わった知らせの項目名)
- 会話のチェックポイント(line_checkpoints.db)は接頭辞がないので触らない。今までどおり読める

守ること:
- 何度動いても同じ結果になる。SQLite は古いファイル名のまま 1 つのトランザクションで中身を直してから名前を変えるので、
  新しい名前のファイルの中身は必ず新しい形になる。途中で止まったら、もう一度起動すれば残りを続ける
- 途中で止まった印(IN_PROGRESS。中身は接頭辞)があるのに環境変数が無いか、値が違えば、起動を止める(半分だけ移ったデータで動かない)
- 旧い名前のデータがあると分かる値のときだけ移す(<接頭辞>_accounts.db など)。line のような別の名前の接頭辞を入れられても、ほかのファイルを壊さない
- 新しい名前のファイルと旧い名前のファイルが両方あれば、どちらも消さず、何も変えずに止める
- 新しいデータが無いのに state.json があり、環境変数も無ければ、空のデータで動き始めずに起動を止める

    ./.venv/bin/python -m provider_mcp.legacy
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

from provider_mcp import accounts, profile, summarize
from provider_mcp.data import State
from provider_mcp.events import EVENTS_PATH

ENV = "AICA_LEGACY_PREFIX"
NEW = "provider"
IN_PROGRESS = "provider_migration.inprogress"  # 移している間だけある印
LOCK_FILE = "provider_migration.lock"  # 同時に起動したプロセス(Web、MCP サーバー)の 1 つだけが移す
SQLITE_MAGIC = b"SQLite format 3\0"
SQLITE_SIDECARS = ("-journal", "-wal", "-shm")


class MigrationError(RuntimeError):
    """移せない、または今のデータで起動してはいけない。理由は文の中にある。"""


def _new_data_names() -> tuple[str, ...]:
    return accounts.ACCOUNTS_PATH.name, EVENTS_PATH.name, summarize.SUMMARIES_PATH.name


def _check_prefix(prefix: str) -> str:
    """環境変数の値は SQL の識別子とファイル名に使うので、英小文字と数字だけを通す(後ろの _ は有っても無くてもよい)。"""
    name = prefix.strip().removesuffix("_")
    if not re.fullmatch(r"[a-z][a-z0-9]*", name) or name == NEW:
        raise MigrationError(f"{ENV} の値が使えません(英小文字で始まる英小文字と数字だけで、{NEW} 以外)")
    return name


def _renamed(name: str, prefix: str) -> str | None:
    """接頭辞の名前の新しい名前。接頭辞の名前でなければ None。"""
    if name == prefix:
        return NEW
    if name.startswith(f"{prefix}_"):
        return f"{NEW}_{name[len(prefix) + 1:]}"
    return None


def _rename_keys(value, prefix: str):
    """JSON の値の中の、接頭辞のキーを新しい名前にした写しを返す。同じ dict に新旧のキーが両方あれば止める。
    連携が変わった知らせの項目名は、コードが新しい文と比べるので(profile.notice)、値も直す。"""
    if isinstance(value, list):
        return [_rename_keys(v, prefix) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, v in value.items():
        new = _renamed(key, prefix) if isinstance(key, str) else None
        if new is not None and new in value:
            raise MigrationError(f"同じ JSON のオブジェクトに新旧のキーが両方あります: {new}")
        out[new or key] = _rename_keys(v, prefix)
    if out.get("what") == f"{prefix} の会員との連携":
        out["what"] = profile.LINK_LABEL
    return out


def _is_sqlite(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _legacy_files(folder: Path, prefix: str) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and _renamed(p.name, prefix))


def _check_conflicts(files: list[Path], prefix: str) -> None:
    """動かす前に確かめる。新しい名前のファイルが中身つきであれば、どちらが正しいか分からないので何も変えない。
    空のファイル(ロック)はどちらも中身が無いので、ぶつかっても困らない。"""
    for p in files:
        target = p.with_name(_renamed(p.name, prefix))
        if target.exists() and p.stat().st_size and target.stat().st_size:
            raise MigrationError(f"旧い名前のファイルと新しい名前のファイル {target.name} が両方あります。"
                                 "どちらも消さずに止めました。どちらが正しいかを確かめて、要らない方を退けてください")


def _rewrite_json_columns(db: sqlite3.Connection, table: str, prefix: str) -> None:
    """TEXT の列の中の JSON のキーを直す。イベントの表は追記だけのトリガーを持つので、書き直す行があるときだけ、
    同じトランザクションの中で外して戻す。"""
    quoted = _quote(table)
    columns = [r[1] for r in db.execute(f"PRAGMA table_info({quoted})") if r[2].upper() == "TEXT"]
    updates = []
    for col in columns:
        rows = db.execute(f"SELECT rowid, {_quote(col)} FROM {quoted} WHERE {_quote(col)} LIKE ?",
                          (f'%"{prefix}%',)).fetchall()
        for rowid, text in rows:
            try:
                value = json.loads(text)
            except ValueError:
                continue  # JSON でない文
            if isinstance(value, dict | list) and (new := _rename_keys(value, prefix)) != value:
                updates.append((col, rowid, json.dumps(new, ensure_ascii=False, separators=(",", ":"))))
    if not updates:
        return
    triggers = db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)).fetchall()
    for name, _ in triggers:
        db.execute(f"DROP TRIGGER {_quote(name)}")
    for col, rowid, text in updates:
        db.execute(f"UPDATE {quoted} SET {_quote(col)} = ? WHERE rowid = ?", (text, rowid))
    for _, sql in triggers:
        db.execute(sql)


def _migrate_sqlite(path: Path, prefix: str) -> None:
    """列の名前と列の中の JSON を、1 つのトランザクションで直す。止まっても元のままで、もう一度やれば続けられる。"""
    with closing(sqlite3.connect(path, isolation_level=None, timeout=30)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                columns = [r[1] for r in db.execute(f"PRAGMA table_info({_quote(table)})")]
                for col in columns:
                    new = _renamed(col, prefix)
                    if new is None:
                        continue
                    if new in columns:
                        raise MigrationError(f"{path.name} の表 {table} に新旧の列が両方あります: {new}")
                    db.execute(f"ALTER TABLE {_quote(table)} RENAME COLUMN {_quote(col)} TO {_quote(new)}")
            for table in tables:
                _rewrite_json_columns(db, table, prefix)
                # fixtures の会員のログインID は旧い名前のドメインで入っている(種は 1 回しか入らないので、ここで直さないと
                # 文書の例でログインできなくなる)。パスワードのハッシュは変わらない
                old_domain, new_domain = f"@{prefix}.test", f"@{NEW}.test"
                if table == "logins":
                    db.execute("UPDATE logins SET login_id = substr(login_id, 1, length(login_id) - ?) || ?"
                               " WHERE login_id LIKE ?", (len(old_domain), new_domain, "%" + old_domain))
                elif table == "login_failures":  # 続けてまちがえた数は 15 分で消える。誰でも新しいドメインの行を作れるので、移さず消す
                    db.execute("DELETE FROM login_failures WHERE login_id LIKE ?", ("%" + old_domain,))
        except sqlite3.IntegrityError as e:
            db.execute("ROLLBACK")
            raise MigrationError(f"{path.name} の値が新しい名前の値とぶつかりました。何も変えずに止めました: {e}") from e
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def _move(path: Path, prefix: str) -> None:
    """名前を変える。_check_conflicts を通ったので、新しい名前のファイルがあれば、どちらかは空。中身のある方を残す。"""
    target = path.with_name(_renamed(path.name, prefix))
    if target.exists() and not path.stat().st_size:
        path.unlink()
        return
    os.replace(path, target)


def _json_files(folder: Path) -> Iterator[Path]:
    return (p for p in sorted(folder.glob("*.json")) if p.is_file())


def _json_with_legacy_keys(path: Path, prefix: str, required: bool) -> tuple[object | None, bool]:
    """(直した値, 直す必要があるか)。読めない JSON は、state.json なら止め、ほかのファイルなら移す対象にしない。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        if required:
            raise MigrationError(f"{path.name} を JSON として読めません") from e
        return None, False
    new = _rename_keys(value, prefix)
    return new, new != value


def _write_json(path: Path, value: object) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _is_old_data(folder: Path, state_path: Path, prefix: str) -> bool:
    """この接頭辞の旧いデータがあるか。無ければ何も動かさない(別の名前の接頭辞を入れられても、ほかのファイルを壊さない)。"""
    if any((folder / name.replace(f"{NEW}_", f"{prefix}_", 1)).exists() for name in _new_data_names()):
        return True
    return state_path.is_file() and f'"{prefix}_writes"' in state_path.read_text(encoding="utf-8")


def _json_changes(folder: Path, state_path: Path, prefix: str) -> list[tuple[Path, object]]:
    documents = [(p, *_json_with_legacy_keys(p, prefix, p == state_path)) for p in _json_files(folder)]
    return [(p, value) for p, value, changed in documents if changed]


def migrate(state_path: Path, prefix: str) -> dict:
    """state_path のディレクトリの旧い名前のデータを移す。{"files": 移したファイルの数, "json": 直した JSON の数}。
    呼ぶ側がロックを持っていること(prepare)。"""
    prefix = _check_prefix(prefix)
    folder = state_path.parent
    marker = folder / IN_PROGRESS
    if marker.exists():
        if marker.read_text(encoding="utf-8").strip() != prefix:
            raise MigrationError(f"途中で止まった移行と {ENV} の値が違います。値を確かめて起動し直してください")
    elif not _is_old_data(folder, state_path, prefix):
        return {"files": 0, "json": 0}
    files = _legacy_files(folder, prefix)
    _check_conflicts(files, prefix)
    _json_changes(folder, state_path, prefix)  # 動かす前に、読めない state.json と新旧のキーのぶつかりで止める
    marker.write_text(prefix, encoding="utf-8")
    for p in files:  # SQLite の本体から。開くと、止まった書き込みの journal はこの名前のまま片付く
        if not p.name.endswith(SQLITE_SIDECARS) and _is_sqlite(p):
            _migrate_sqlite(p, prefix)
            _move(p, prefix)
    for p in _legacy_files(folder, prefix):  # ロック、作りかけのファイル、残っていれば SQLite の付属のファイル
        _move(p, prefix)
    pending = _json_changes(folder, state_path, prefix)  # 名前を変えた JSON のファイルも、新しい名前で直す
    for p, value in pending:
        _write_json(p, value)
    marker.unlink(missing_ok=True)
    return {"files": len(files), "json": len(pending)}


def prepare(state_path: Path | None = None) -> dict | None:
    """起動のときに、データを使う前に呼ぶ。環境変数があれば移し、無ければ今のデータで動いてよいかを確かめる。
    移したら結果を返す。起動してはいけなければ MigrationError。"""
    path = Path(state_path or State().path)
    folder = path.parent
    folder.mkdir(parents=True, exist_ok=True)
    prefix = os.environ.get(ENV, "").strip()
    with open(folder / LOCK_FILE, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = None
        if prefix:
            result = migrate(path, prefix)
        elif (folder / IN_PROGRESS).exists():
            raise MigrationError(f"旧い名前からの移行が途中で止まっています。{ENV} を入れて起動し直すと、続きから移します")
        if not any((folder / name).exists() for name in _new_data_names()):
            moved = result and (result["files"] or result["json"])
            if path.is_file() and not moved:
                raise MigrationError(f"{path.name} はあるのに、新しい名前のデータがありません。空のデータで動き始めないように"
                                     f"止めました。旧い名前のデータがあるなら、{ENV} に正しい値を入れて起動し直してください"
                                     "(docs/deploy/render.md の「旧い名前からの移行」)。"
                                     f"はじめから始めるなら {path.name} を退けてください")
            # 初めての起動。state.json より先に新しいデータを作り、次の起動で「新しいデータが無い」と間違えないようにする
            accounts.Accounts(folder / accounts.ACCOUNTS_PATH.name)
    return result


def prepare_or_exit(state_path: Path | None = None) -> None:
    """起動の入口(Web プロセス、MCP サーバーの HTTP、コンテナの起動)から呼ぶ。理由を標準エラーに出して止める。"""
    try:
        result = prepare(state_path)
    except MigrationError as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        raise SystemExit(1) from e
    if result and (result["files"] or result["json"]):
        print(f"旧い名前のデータを移しました: ファイル {result['files']} 個、JSON {result['json']} 個", file=sys.stderr, flush=True)


if __name__ == "__main__":
    prepare_or_exit()
