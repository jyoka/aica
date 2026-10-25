"""期間で消す(docs/design/privacy.md の 3.3)のうち、dip の状態(state.json)の側。

- 最後の利用(last_seen_at): ターン、プロフィールの操作、音声相談の結果のたびに、実際の時刻(UTC)を書く(touch)。
  シナリオユーザー(「匿名ID:シナリオユーザー」)の行にも書く。期間は人(匿名ID)ごとに、その人の行のいちばん新しい時刻で数える
- 使われなくなった人: 最後の利用から USER_TTL を過ぎたら、その人と同じブラウザのシナリオユーザーの行を丸ごと外す
  (remove_expired)。「すべて消す」(dip_mcp.profile.forget_all)と違い、持ち越す値(連番と消した部屋)も残さない
- 「すべて消す」で持ち越した行: 消した時刻(forgotten_at)から FORGOTTEN_ROOMS_TTL で、消した部屋の名前を外す。
  行そのものは last_seen_at(消した時刻)から USER_TTL で、ほかの行と同じく外れる
- last_seen_at の無い行(この仕組みより前からある行など)は、最初に見つけた掃除の時刻を last_seen_at にする
  (いきなり消さず、そこから USER_TTL で消える)
- fixtures のユーザーそのもの(評価用の架空のユーザー、例 sim-new)は対象にしない

会話・音声のファイル・Web プロセスのメモリの片付けと、ターンとの排他(user_lock)は、呼び出し元の掃除
(line_runtime.purge)が受け持つ。ここは state.json と連携の表だけを書く。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dip_mcp import accounts
from dip_mcp.data import State, _fixture_user_ids

USER_TTL = timedelta(days=180)  # 使われなくなった人を消すまで(最後の利用から)
FORGOTTEN_ROOMS_TTL = timedelta(days=1)  # 「すべて消す」で持ち越した部屋の名前を外すまで(消した時刻から)
LAST_SEEN = "last_seen_at"
FORGOTTEN_AT = "forgotten_at"


def clock() -> datetime:
    """最後の利用と期間に使う今の時刻(実際の時刻。テストで差し替える)。シナリオの固定の時計(SIM_NOW)は使わない。"""
    return datetime.now(UTC)


def _iso(d: datetime) -> str:
    return d.astimezone(UTC).isoformat(timespec="seconds")


def person(user_id: str) -> str:
    """期間を数える単位。「匿名ID:シナリオユーザー」は匿名IDにまとめる(line_runtime.quota.person と同じ)。"""
    return user_id.split(":", 1)[0]


def _covered(user_id: str) -> bool:
    """期間で消す対象の行か(fixtures のユーザーそのものと、その人の行は対象にしない)。"""
    return person(user_id) not in _fixture_user_ids()


def touch(state: State, user_id: str) -> None:
    """最後の利用を今にする。fixtures のユーザーそのものには書かない。"""
    if _covered(user_id):
        at = _iso(clock())
        state.update(user_id, lambda u: u.__setitem__(LAST_SEEN, at))


def mark_forgotten(kept: dict) -> dict:
    """「すべて消す」で持ち越す値に、消した時刻を足す(last_seen_at と、部屋を持ち越すなら forgotten_at)。"""
    at = _iso(clock())
    return kept | {LAST_SEEN: at} | ({FORGOTTEN_AT: at} if kept.get("forgotten_voice_rooms") else {})


def sweep(state: State) -> dict:
    """1 回の掃除の前半(state.json を 1 回だけ読み書きする): last_seen_at の無い行に今を書き、持ち越した部屋の名前を
    期限で外し、USER_TTL を過ぎた人を返す。外すのは remove_expired(呼び出し元が user_lock を取ってから)。
    {"expired": {人: [その人の行の ID…]}, "rooms_cleared": 部屋を外した行の数, "backfilled": 今を書いた行の数}"""
    now = clock()
    out: dict = {"expired": {}, "rooms_cleared": 0, "backfilled": 0}

    def apply(users: dict) -> bool:
        changed = False
        seen: dict[str, datetime] = {}
        for uid, u in users.items():
            if not _covered(uid):
                continue
            if LAST_SEEN not in u:
                u[LAST_SEEN] = _iso(now)
                out["backfilled"] += 1
                changed = True
            at = u.get(FORGOTTEN_AT)
            if at is not None and now - datetime.fromisoformat(at) > FORGOTTEN_ROOMS_TTL:
                u.pop("forgotten_voice_rooms", None)
                u.pop(FORGOTTEN_AT)
                out["rooms_cleared"] += 1
                changed = True
            p, last = person(uid), datetime.fromisoformat(u[LAST_SEEN])
            seen[p] = max(seen.get(p, last), last)
        for uid in users:
            if _covered(uid) and now - seen[person(uid)] > USER_TTL:
                out["expired"].setdefault(person(uid), []).append(uid)
        return changed

    state.edit_users(apply)
    return out


def _expired_rows(users: dict, who: str, now: datetime) -> list[str]:
    """その人の行(シナリオユーザーを含む)が、どれも最後の利用から USER_TTL を過ぎていれば、その行の ID。でなければ空。"""
    mine = [uid for uid in users if person(uid) == who and _covered(uid)]
    if not mine or any(LAST_SEEN not in users[uid] for uid in mine):  # 掃除の後にできた行がある: 使われている
        return []
    if now - max(datetime.fromisoformat(users[uid][LAST_SEEN]) for uid in mine) <= USER_TTL:
        return []
    return sorted(mine)


def still_expired(state: State, people: list[str]) -> list[str]:
    """people のうち、今も USER_TTL を過ぎている人(掃除が user_lock を取った後、会話を消す前に確かめ直す)。
    state.json は 1 回だけ読む。"""
    now = clock()
    out: list[str] = []

    def check(users: dict) -> bool:
        out.extend(who for who in people if _expired_rows(users, who, now))
        return False  # 読むだけ

    state.edit_users(check)
    return out


def remove_expired(state: State, people: list[str]) -> dict:
    """people(匿名ID か LINE のユーザーID)と、同じブラウザのシナリオユーザーの行を丸ごと外し、連携も外す。
    外す前に、同じ読み書きの中で人ごとに期限を確かめ直す(掃除が対象を決めた後に使われた人は外さない)。
    state.json は 1 回だけ読み書きする。{"people": 外した人, "users": 外した行の ID, "files": その行が持っていた
    音声の下書きのファイル(呼び出し元が消す)}"""
    now = clock()
    out: dict = {"people": [], "users": [], "files": []}

    def apply(users: dict) -> bool:
        for who in people:
            mine = _expired_rows(users, who, now)
            if not mine:
                continue
            out["people"].append(who)
            out["users"] += mine
            out["files"] += [f for uid in mine for r in users[uid].get("voice_results", []) for f in r.get("files") or []]
            for uid in mine:
                del users[uid]
        return bool(out["users"])

    state.edit_users(apply)
    if out["users"]:
        accounts.of(state).forget_users(out["users"])
    return out
