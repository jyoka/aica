"""dip の会員のログインと、LINE のアカウント連携(シミュレーション)。LINE の Messaging API の公式の流れに合わせる(ADR-0007)。

1. bot(line_runtime.server)が LINE から linkToken をもらい、どのユーザーに出したかを覚える(record_link_token)
2. 本人が dip にログインする(login)。dip は nonce を作り、nonce と会員ID(と linkToken)を結んで覚える(issue_nonce)。
   本人は LINE の accountLink の画面(linkToken と nonce つき)に送られる
3. LINE から accountLink の webhook が来たら、nonce から会員IDを引き、連携の表に書く(complete_link)
4. dip のツールは、連携の表で aica のユーザーID から dip_id を引く(dip_id。読むのは dip_mcp.behavior.dip_id だけ)

守ること:
- パスワードはハッシュ(PBKDF2-SHA256、塩つき)だけを持つ。同じログインID で続けて MAX_FAILURES 回まちがえると、LOCK の間
  ログインできない。数えるのはログインID ごとで、会員にいない ID も同じに数える(ロックされるかで ID があるかを知られない)
- nonce は推測できない乱数(secrets)。1 回だけ使え、NONCE_TTL で切れる。nonce を作ったときの linkToken が、webhook の
  ユーザーに bot が出したものでなければ失敗にする(別の linkToken との組み合わせ)。nonce と linkToken はハッシュで持つ
- 連携の表の鍵は aica のユーザーID(LINE なら「l-<ハッシュ>」、Web の匿名ユーザーなら「w-…」)。LINE の userId そのものは持たない

ファイルは State と同じディレクトリの dip_accounts.db(SQLite)。初めて作ったとき 1 回だけ、fixtures の会員のログイン
(fixtures/dip_accounts.json)と、シナリオのユーザーの連携(fixtures/users.json の dip_id)を入れる。シナリオのユーザーは
最初から連携済みとして始まる。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from dip_mcp.data import FIXTURES, ROOT, load_users

if TYPE_CHECKING:
    from dip_mcp.data import State

ACCOUNTS_PATH = ROOT / "data" / "dip_accounts.db"
LOGINS = FIXTURES / "dip_accounts.json"

MAX_FAILURES = 5  # 続けてまちがえられる回数。超えたら LOCK の間、正しいパスワードでもログインできない
LOCK = timedelta(minutes=15)
NONCE_TTL = timedelta(minutes=10)  # nonce の期限(LINE の linkToken と同じ 10 分)
LINK_TOKEN_TTL = timedelta(minutes=10)  # bot が覚えておく linkToken(LINE の linkToken の期限)
PBKDF2_ITERATIONS = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS logins (
    dip_id TEXT PRIMARY KEY,
    login_id TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_failures (
    login_id TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    last_failed_at TEXT NOT NULL,
    locked_until TEXT
);
CREATE TABLE IF NOT EXISTS nonces (
    nonce_hash TEXT PRIMARY KEY,
    dip_id TEXT NOT NULL,
    link_token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS link_tokens (
    link_token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    user_id TEXT PRIMARY KEY,
    dip_id TEXT NOT NULL,
    linked_at TEXT NOT NULL
);
"""

# 連携できなかった理由(complete_link の error)。OTHER_TOKEN は、nonce の linkToken をそのユーザーに出していない(か期限切れ)
UNKNOWN, USED, EXPIRED, OTHER_TOKEN = "unknown", "used", "expired", "other_token"


def clock() -> datetime:
    """期限とロックに使う今の時刻(テストで差し替える)。"""
    return datetime.now(UTC)


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).isoformat(timespec="seconds")


def _digest(secret: str) -> str:
    """nonce と linkToken はハッシュで持つ(ファイルが読まれても、そのまま使えない)。"""
    return hashlib.sha256(secret.encode()).hexdigest()


def hash_password(password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> str:
    """「pbkdf2_sha256$回数$塩(hex)$ハッシュ(hex)」。"""
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, n, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(n)).hex()
    except ValueError:
        return False
    return hmac.compare_digest(got, want)


_ready: set[Path] = set()  # このプロセスで表を作ったファイル
_DUMMY_HASH = hash_password("aica-no-such-member", b"\0" * 16)  # 無い ID でも同じだけ時間をかける(ID があるかを時間で知られない)


def fixture_dip_id(line_user_id: str) -> str | None:
    """シナリオのユーザー(fixtures/users.json)が最初から連携している dip_id。fixtures に無い人は None。"""
    u = load_users().get(line_user_id)
    return u.get("dip_id") if u else None


class Accounts:
    """dip の会員のログイン・nonce と、連携の表(と bot が出した linkToken)。プロセスをまたいで同じファイルを使う。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or ACCOUNTS_PATH).resolve()
        if self.path not in _ready or not self.path.exists():  # 表を作って種を入れるのは、プロセスごとにファイル 1 つにつき 1 回
            self._create()
            _ready.add(self.path)

    def _create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.executescript(SCHEMA)
        with self._tx() as db:  # 2 つのプロセスが同時に作っても、種は 1 回だけ入る
            if db.execute("SELECT 1 FROM meta WHERE key = 'seeded'").fetchone() is None:
                self._seed(db)
                db.execute("INSERT INTO meta (key, value) VALUES ('seeded', ?)", (_iso(clock()),))

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """書く前にロックを取るトランザクション(nonce を 2 つのプロセスが同時に使えないように)。"""
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")
        finally:
            db.close()

    def _seed(self, db: sqlite3.Connection) -> None:
        if LOGINS.is_file():
            rows = json.loads(LOGINS.read_text(encoding="utf-8"))["logins"]
            db.executemany("INSERT OR IGNORE INTO logins (dip_id, login_id, password_hash) VALUES (?, ?, ?)",
                           ((r["dip_id"], r["login_id"], r["password_hash"]) for r in rows))
        self._seed_links(db)

    def _seed_links(self, db: sqlite3.Connection) -> None:
        at = _iso(clock())
        db.executemany("INSERT OR REPLACE INTO links (user_id, dip_id, linked_at) VALUES (?, ?, ?)",
                       ((uid, u["dip_id"], at) for uid, u in load_users().items() if u.get("dip_id")))

    # ----------------------------- dip: 会員のログイン -----------------------------

    def add_login(self, dip_id: str, login_id: str, password: str) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO logins (dip_id, login_id, password_hash) VALUES (?, ?, ?)",
                       (dip_id, login_id, hash_password(password)))

    def login(self, login_id: str, password: str) -> dict:
        """{"dip_id": …}、または {"error": "bad"}(ID かパスワードがちがう。どちらかは言わない)/
        {"error": "locked"}(続けてまちがえたので、しばらくログインできない)。
        ハッシュの計算(遅い)はトランザクションの外でする(その間、ほかの書き込みを止めないように)。そのため計算の前に
        失敗 1 回分を数えておき(同時に送っても MAX_FAILURES 回より多くは確かめない)、計算の後にロックを確かめ直す
        (計算の間にほかの失敗でロックされたら、正しいパスワードでも通さず、ロックも消さない)。"""
        login_id = login_id.strip()
        now = clock()
        with self._tx() as db:
            db.execute("DELETE FROM login_failures WHERE last_failed_at < ? AND (locked_until IS NULL OR locked_until < ?)",
                       (_iso(now - LOCK), _iso(now)))  # LOCK より前の失敗は数えない(表も増え続けない)
            prev = db.execute("SELECT failures, locked_until FROM login_failures WHERE login_id = ?", (login_id,)).fetchone()
            if prev and prev[1] and datetime.fromisoformat(prev[1]) > now:
                return {"error": "locked"}
            failures = prev[0] if prev and not prev[1] else 0  # ロックが明けたら数え直す
            if failures >= MAX_FAILURES:  # まちがえた数と計算中の数で、もう上限に達している
                return {"error": "locked"}
            db.execute("INSERT OR REPLACE INTO login_failures (login_id, failures, last_failed_at, locked_until)"
                       " VALUES (?, ?, ?, NULL)", (login_id, failures + 1, _iso(now)))
            row = db.execute("SELECT dip_id, password_hash FROM logins WHERE login_id = ?", (login_id,)).fetchone()
        ok = check_password(password, row[1] if row else _DUMMY_HASH) and row is not None
        now = clock()
        with self._tx() as db:
            cur = db.execute("SELECT failures, locked_until FROM login_failures WHERE login_id = ?", (login_id,)).fetchone()
            if cur and cur[1] and datetime.fromisoformat(cur[1]) > now:  # 計算の間に、ほかの失敗でロックされた
                return {"error": "locked"}
            if ok:
                db.execute("DELETE FROM login_failures WHERE login_id = ?", (login_id,))  # 続けてまちがえた数を戻す
                return {"dip_id": row[0]}
            failures = cur[0] if cur and not cur[1] else 1  # 数えておいた 1 回が、そのまま失敗になる(行が消えていたら数え直す)
            lock = _iso(now + LOCK) if failures >= MAX_FAILURES else None
            db.execute("INSERT OR REPLACE INTO login_failures (login_id, failures, last_failed_at, locked_until)"
                       " VALUES (?, ?, ?, ?)", (login_id, 0 if lock else failures, _iso(now), lock))
            return {"error": "locked" if lock else "bad"}

    # ----------------------------- dip: nonce -----------------------------

    def issue_nonce(self, dip_id: str, link_token: str) -> str:
        """ログインできた会員の nonce を作り、会員ID と linkToken に結んで覚える。期限の切れた nonce はここで消す。"""
        nonce = secrets.token_urlsafe(32)  # 256 ビット(LINE の決まりは 10〜255 文字。128 ビット以上を勧めている)
        now = clock()
        with self._tx() as db:
            db.execute("DELETE FROM nonces WHERE expires_at < ?", (_iso(now - NONCE_TTL),))
            db.execute("INSERT INTO nonces (nonce_hash, dip_id, link_token_hash, expires_at) VALUES (?, ?, ?, ?)",
                       (_digest(nonce), dip_id, _digest(link_token), _iso(now + NONCE_TTL)))
        return nonce

    def burn_nonce(self, nonce: str) -> None:
        """使えなくする(LINE が連携に失敗したと知らせてきた nonce)。"""
        with self._tx() as db:
            db.execute("UPDATE nonces SET used_at = ? WHERE nonce_hash = ? AND used_at IS NULL",
                       (_iso(clock()), _digest(nonce)))

    # ----------------------------- bot: linkToken と連携の表 -----------------------------

    def record_link_token(self, user_id: str, link_token: str) -> None:
        """bot が LINE から linkToken をもらった: そのユーザーに出したと覚える(webhook の nonce と突き合わせる)。"""
        now = clock()
        with self._tx() as db:
            db.execute("DELETE FROM link_tokens WHERE expires_at < ?", (_iso(now),))
            db.execute("INSERT OR REPLACE INTO link_tokens (link_token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                       (_digest(link_token), user_id, _iso(now + LINK_TOKEN_TTL)))

    def complete_link(self, user_id: str, nonce: str) -> dict:
        """accountLink の webhook(result が ok)の nonce で、user_id と会員を連携の表に結ぶ。
        {"dip_id": …, "previous": 前に結んでいた dip_id か None}、または {"error": UNKNOWN / USED / EXPIRED / OTHER_TOKEN}。
        nonce は結果によらず、ここで使い終わりにする(同じ nonce で何度も試せないように)。"""
        now = clock()
        with self._tx() as db:
            row = db.execute("SELECT dip_id, link_token_hash, expires_at, used_at FROM nonces WHERE nonce_hash = ?",
                             (_digest(nonce),)).fetchone()
            if row is None:
                return {"error": UNKNOWN}
            dip_id, token_hash, expires_at, used_at = row
            if used_at is not None:
                return {"error": USED}
            db.execute("UPDATE nonces SET used_at = ? WHERE nonce_hash = ?", (_iso(now), _digest(nonce)))
            if datetime.fromisoformat(expires_at) <= now:
                return {"error": EXPIRED}
            issued = db.execute("SELECT 1 FROM link_tokens WHERE link_token_hash = ? AND user_id = ? AND expires_at > ?",
                                (token_hash, user_id, _iso(now))).fetchone()
            if issued is None:
                return {"error": OTHER_TOKEN}
            db.execute("DELETE FROM link_tokens WHERE link_token_hash = ?", (token_hash,))
            prev = db.execute("SELECT dip_id FROM links WHERE user_id = ?", (user_id,)).fetchone()
            db.execute("INSERT OR REPLACE INTO links (user_id, dip_id, linked_at) VALUES (?, ?, ?)",
                       (user_id, dip_id, _iso(now)))
            return {"dip_id": dip_id, "previous": prev[0] if prev else None}

    def dip_id(self, user_id: str) -> str | None:
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            row = db.execute("SELECT dip_id FROM links WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    def set_link(self, user_id: str, dip_id: str | None) -> None:
        """連携の表を直接書く(シナリオの準備と、初期に戻すとき)。None なら連携を外す。"""
        with self._tx() as db:
            if dip_id is None:
                db.execute("DELETE FROM links WHERE user_id = ?", (user_id,))
            else:
                db.execute("INSERT OR REPLACE INTO links (user_id, dip_id, linked_at) VALUES (?, ?, ?)",
                           (user_id, dip_id, _iso(clock())))

    def unlink(self, user_id: str) -> str | None:
        """連携を解除する。外した dip_id(連携していなければ None)。"""
        with self._tx() as db:
            row = db.execute("SELECT dip_id FROM links WHERE user_id = ?", (user_id,)).fetchone()
            db.execute("DELETE FROM links WHERE user_id = ?", (user_id,))
        return row[0] if row else None

    def reset_links(self) -> None:
        """連携の表を初期(シナリオのユーザーだけが連携済み)に戻す。出した linkToken と nonce も忘れる。"""
        with self._tx() as db:
            db.execute("DELETE FROM links")
            db.execute("DELETE FROM link_tokens")
            db.execute("DELETE FROM nonces")
            self._seed_links(db)


def of(state: State) -> Accounts:
    """その State と同じディレクトリの dip_accounts.db。"""
    return Accounts(state.path.parent / ACCOUNTS_PATH.name)
