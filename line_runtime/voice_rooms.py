"""音声相談の部屋のうち、まだ終わっていないもの(人ごと)。1 人が音声の部屋を同時にいくつも開けないようにする。

部屋ごとに、音声ワーカーのジョブ(プロセス)と dip MCP の子プロセスが 1 つずつできる。POST /api/voice/start は、
その人(quota.person と同じ単位。「匿名ID:シナリオユーザー」は匿名IDにまとめる)に終わっていない部屋があれば断る。

部屋が終わったとみなすのは、次のどれか。面談の結果が届いただけでは終わりにしない(本人は「面談を終える」で結果を
送らせたあとも、接続したままジョブを持ち続けられるので)。
- ワーカーがジョブを終えたと知らせた(POST /api/voice/session の ended。本人が通話を切るとジョブも終わる)
- 発行してから JOIN_WINDOW のあいだ、だれも部屋に入らなかった(接続トークンの期限が切れて、もう入れない)
- 入ったと知らせてから HOLD_MAX が過ぎた(終わりの知らせが届かなかったとき。ジョブがメモリの上限で止められたなど)

ワーカーは本人が部屋に入ったときに joined を知らせ、その人に別の部屋があればジョブを止める(前に発行した接続トークンで、
終わった部屋に入り直すと、LiveKit は部屋を作り直して新しいジョブを始めるため)。

記録はメモリだけに置く。Web プロセスを再起動すると消える。再起動の後に入った部屋は joined で記録し直すが、再起動の前から
続いているジョブは数えない。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from line_runtime.quota import person
from voice_resume.livekit_token import TOKEN_TTL

# 発行した部屋に入れる間。接続トークンの期限(5 分)を過ぎると入れないので、それに入るまでの時間の余裕を足す
JOIN_WINDOW = TOKEN_TTL + timedelta(minutes=1)
# 入った部屋を、終わりの知らせが無くても終わったとみなすまでの時間。相談は約 5 分なので、十分に長くとる
HOLD_MAX = timedelta(minutes=30)


class ActiveVoiceRooms:
    """人 → 終わっていない部屋。clock は秒を返す関数(テストで時間を進める)。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._rooms: dict[str, dict[str, dict]] = {}  # 人 → 部屋 → {"issued": 秒, "joined": 秒 か None}

    def _live(self, key: str) -> dict[str, dict]:
        """その人の終わっていない部屋(期限の過ぎた部屋は捨てる)。"""
        now = self.clock()
        rooms = {room: r for room, r in self._rooms.get(key, {}).items()
                 if (now - r["joined"] < HOLD_MAX.total_seconds() if r["joined"] is not None
                     else now - r["issued"] < JOIN_WINDOW.total_seconds())}
        if rooms:
            self._rooms[key] = rooms
        else:
            self._rooms.pop(key, None)
        return rooms

    def open_room(self, user_id: str) -> str | None:
        """その人の終わっていない部屋(無ければ None)。"""
        return next(iter(self._live(person(user_id))), None)

    def issue(self, user_id: str, room: str) -> None:
        """部屋を発行した(まだだれも入っていない)。ついでに、期限の過ぎた部屋しか無い人を捨てる(二度と来ない人の分が残り続けない)。"""
        for other in list(self._rooms):
            self._live(other)
        key = person(user_id)
        self._rooms[key] = {**self._live(key), room: {"issued": self.clock(), "joined": None}}

    def joined(self, user_id: str, room: str) -> bool:
        """本人が部屋に入り、ワーカーのジョブが始まった。その人に別の部屋があれば記録せずに False(ワーカーはジョブを止める)。
        記録に無い部屋(再起動の前に発行したもの)も、ほかに部屋が無ければ記録する。"""
        key, now = person(user_id), self.clock()
        rooms = self._live(key)
        if any(other != room for other in rooms):
            return False
        self._rooms[key] = {**rooms, room: {"issued": now, "joined": now}}
        return True

    def release(self, user_id: str, room: str | None) -> None:
        """部屋が終わった(ワーカーのジョブが終わった)。"""
        key = person(user_id)
        rooms = self._live(key)
        if room and rooms.pop(room, None) is not None and not rooms:
            self._rooms.pop(key, None)
