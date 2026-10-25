"""イベントの受け付けとターンの決まり(task 163)。POST /api/event(JSON)と POST /api/event/stream(NDJSON)が共通に使う。

受け付け(accept): イベントの種類、使い始める前の同意(line_runtime.consent)、空の発言・長すぎる発言(MAX_TEXT_CHARS)、
アップロードの事実を確かめる。LLM も会話数も使わない。だめなら BadTurn(同意がまだなら、その子の NeedsConsent)。
ターン(run): プッシュの判定 → stale の照合 → 会話数を 1 つ数える → 最後の利用 → 知らせ → LineRuntime → 開いた記録、の順に進め、
行(dict)を順に出す。Web サーバーは Cookie の照合と、HTTP(reply_of)と NDJSON への書き出しだけを受け持つ。
評価が後で同じ手順を呼べるように、FastAPI には依存させない(docs/design/consolidate-resume-draft-and-event-turn.md)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from dip_mcp import logic, profile, retention
from dip_mcp.data import State
from line_runtime import consent
from line_runtime.quota import DailyQuota, limit_reply
from line_runtime.resume_confirm import RESUME_LABEL, resume_bubbles
from line_runtime.runtime import EVENTS, LineRuntime

LIMIT_REASON = "本日の会話数の上限に達している"
# 1つの発言の長さの上限(字数)。LINE のテキストメッセージの上限と同じ(LINE からはこれより長い発言は届かない)。
# Web のトーク画面は LINE 風の1行の入力欄で、履歴書はファイルで受け取るので、ふつうの発言は届かない長さ。
# 発言は会話の記録に残り、以降のターンで毎回モデルに送り直すので、1ターンの入力の費用もここで縛る
MAX_TEXT_CHARS = 5000


class BadTurn(ValueError):
    """受け付けないイベント(Web サーバーは 400 にする)。LineRuntime の ValueError も包む(元の例外は __cause__)。"""


class NeedsConsent(BadTurn):
    """使い始める前の同意(consent の terms)がまだ、または版が上がって取り直す(Web サーバーは 403 にする)。"""


class TurnFailed(Exception):
    """LineRuntime を回し始めてから終わるまで(LineRuntime と、reply・stale の行の副作用)に起きた、ValueError 以外の例外と
    副作用の ValueError。元の例外は __cause__。ValueError の子ではないので、JSON の API は 400 にせず 500 のまま。
    stream は BadTurn と TurnFailed だけを error の行にする(その前の受け付けの例外では、今と同じく応答が途中で切れる)。"""


@dataclass(frozen=True)
class TurnDeps:
    """1 ターンの受け付けに使うもの。Web サーバーが要求ごとに作る。"""

    state: State
    quota: DailyQuota
    rt: LineRuntime
    unread_uploads: dict[str, dict]  # ユーザー → 読み取れなかった直前のアップロード(続くターンの facts)
    ip: str | None  # 会話数の IP ごとの上限を数える IP。None なら IP の上限を見ない(LINE: どの人も LINE のサーバーから届く)


@dataclass(frozen=True)
class Turn:
    """受け付けたイベント。"""

    user_id: str
    event: str
    text: str | None
    thread: int | None  # 本番のトーク画面が表示している会話の番号(/dev・評価は None)
    entry: str
    kind: str  # EVENTS の種別(user・upload・greet・push)
    facts: dict | None  # アップロードの事実。プッシュの事実は run が足す
    new_conversation: bool = False  # 発言でも新しい会話を始める(LINE の発言で、最後のやりとりから間が空いた)
    channel: str = "web"  # 今の窓口(line_runtime.agent.CHANNELS)
    features: tuple[str, ...] = ()  # LINE のトークから LIFF 画面で使えるもの(line_channel.liff_features)


def accept(deps: TurnDeps, user_id: str, event: str, text: str | None, thread: int | None,
           new_conversation: bool = False, channel: str = "web", features: tuple[str, ...] = ()) -> Turn:
    """LLM も会話数も使わない検証: イベント、同意、空の発言・長すぎる発言、アップロードの事実。だめなら BadTurn。
    同意がまだなら NeedsConsent(プッシュも含めて、同意の前は LLM を呼ばない。docs/design/privacy.md の 3.6)。
    new_conversation: 発言でも新しい会話を始める(LINE の発言で、最後のやりとりから間が空いたとき。挨拶とプッシュはいつも新しい会話)。
    channel・features: 今の窓口と、そこで使えるもの(エージェントに伝える)。"""
    if event not in EVENTS:
        raise BadTurn(f"event は {list(EVENTS)} のいずれか")
    entry, kind = EVENTS[event]
    if kind == "user" and not (text or "").strip():  # 空の発言は会話数を使う前に断る(上限の1回分を無駄にしない)
        raise BadTurn(f"{event} には空でない text が必要です")
    if kind == "user" and len(text) > MAX_TEXT_CHARS:  # 長すぎる発言も、会話数を使う前・LLM を呼ぶ前に断る
        raise BadTurn(f"{event} の text は {MAX_TEXT_CHARS} 字までです")
    if consent.needed(deps.state, user_id, "terms"):
        raise NeedsConsent(consent.MISSING["terms"])
    facts = upload_facts(deps, user_id) if kind == "upload" else None
    return Turn(user_id=user_id, event=event, text=text, thread=thread, entry=entry, kind=kind, facts=facts,
                new_conversation=new_conversation, channel=channel, features=tuple(features))


async def run(deps: TurnDeps, turn: Turn) -> AsyncIterator[dict]:
    """1 ターンを進め、行を順に出す: gate(プッシュのとき)、stale、thread・llm・tool_start・tools(LineRuntime の途中経過)、
    reply(プッシュの見送りと上限の案内も reply)。副作用(開いた記録、知らせの渡し終わり、search_track)は reply の行を出す前に済ませる。
    LineRuntime を回し始めてから終わるまでの例外は、LineRuntime の ValueError を BadTurn に、ほかを TurnFailed に包む(from e)。"""
    facts = turn.facts
    if turn.kind == "push":
        # 上限に達していたら、接触として記録する前(dip に聞く前)に送らないと決める
        if not deps.quota.available(turn.user_id, deps.ip):
            yield {"kind": "reply", "sent": False, "reason": LIMIT_REASON}
            return
        # ここは dip のバックエンドの代わり: 行動イベントを検知し、プッシュしてよいかを dip が決める。
        gate = logic.push_trigger(deps.state, turn.user_id, turn.event)
        yield {"kind": "gate", **gate}
        if not gate["allowed"]:
            yield {"kind": "reply", "sent": False, "reason": gate["reason"]}
            return
        facts = gate["facts"]
    stale = await stale_thread(deps, turn)
    if stale is not None:  # 別のタブで新しい会話が始まっていた: 発言を混ぜず、会話数も使わない(runtime もターンの排他の中で同じ照合をする)
        yield {"kind": "stale", "thread": stale}
        return
    limited = deps.quota.try_take(turn.user_id, deps.ip)
    if limited is not None:  # 上限を超えたら LLM を呼ばずに案内だけ返す
        yield {"kind": "reply", **limit_reply(turn.entry, limited)}
        return
    if turn.kind != "push":  # 本人のターン(プッシュは dip から)は最後の利用。会話数を数えた後に書く(上限の人の行を増やさない)
        retention.touch(deps.state, turn.user_id)
    changes, notice = profile_notice(deps.state, turn)
    try:
        events = deps.rt.handle_stream(turn.user_id, turn.event, turn.text, facts, talk_thread=turn.thread, notice=notice,
                                       new_conversation=turn.new_conversation, channel=turn.channel, features=turn.features)
        while (ev := await _next(events)) is not None:
            if ev["kind"] == "reply":
                record_open(deps, turn, ev, changes)
                track_status(deps.state, turn.user_id, ev)["search_track"] = logic.search_track(deps.state.user(turn.user_id))
            elif ev["kind"] == "stale":  # 事前の照合の後に別のタブが会話を始めていた: LLM は呼んでいないので会話数を返す
                deps.quota.refund(turn.user_id, deps.ip)
            yield ev
    except BadTurn:
        raise
    except Exception as e:  # API・MCP のエラーも呼び出し元に伝える(stream は error の行にする)
        raise TurnFailed(f"{type(e).__name__}: {e}") from e


async def _next(events: AsyncIterator[dict]) -> dict | None:
    """LineRuntime の次の行(終わりなら None)。LineRuntime の ValueError(構造化出力の失敗など)は BadTurn に包む。"""
    try:
        return await anext(events)
    except StopAsyncIteration:
        return None
    except ValueError as e:
        raise BadTurn(str(e)) from e


async def reply_of(deps: TurnDeps, turn: Turn) -> dict | None:
    """run を最後まで進め、JSON の API が返す形にする(kind を除いた reply。stale は {"stale": True, "thread": n})。"""
    reply = None
    async for ev in run(deps, turn):
        if ev["kind"] == "reply":
            reply = {k: v for k, v in ev.items() if k != "kind"}
        elif ev["kind"] == "stale":
            reply = {"stale": True, "thread": ev["thread"]}
    return reply


async def stale_thread(deps: TurnDeps, turn: Turn) -> int | None:
    """本番のトーク画面からの発言で、今の会話が画面の会話と違えば今の会話番号(違わなければ None)。"""
    if turn.thread is None or turn.kind not in ("user", "upload"):
        return None
    n = await deps.rt.current_thread(turn.user_id)
    return n if n != turn.thread else None


def profile_notice(state: State, turn: Turn) -> tuple[int, str | None]:
    """プロフィール画面で変えたことのうち、まだ AI に渡していないもの: (件数, 知らせの文)。
    新しい会話を始めるターン(挨拶・プッシュ・間が空いた LINE の発言)では渡さない(最初に要約を読み直すので、返ったら渡し終えた扱い)。"""
    changes = profile.pending_changes(state, turn.user_id)
    if not changes or turn.kind not in ("user", "upload") or turn.new_conversation:
        return len(changes), None
    return len(changes), profile.notice(state, turn.user_id, changes)


def record_open(deps: TurnDeps, turn: Turn, reply: dict, changes: int = 0) -> None:
    """返信が返ったら: そのターンで渡したプロフィールの変更の知らせ(changes 件)は渡し終えた。
    本番のトーク画面の挨拶なら、開いたことを dip に記録する(次からは再訪)。
    読み取れなかったアップロードに続くターンなら、その事実は使い終わり(次のアップロードのターンに持ち越さない)。"""
    profile.take_changes(deps.state, turn.user_id, changes)
    if turn.thread is not None and turn.kind == "greet":
        logic.record_talk_open(deps.state, turn.user_id)
    if turn.kind == "upload":
        deps.unread_uploads.pop(turn.user_id, None)


def track_status(state: State, user_id: str, reply: dict | None) -> dict | None:
    """返信の切り替えの表示(track_switch)に、今の状態(取り消せるか・取り消したか)を dip の記録から足す。"""
    sw = (reply or {}).get("track_switch")
    if sw:
        sw["status"] = profile.switch_status(state, user_id, sw["id"])
    return reply


def upload_facts(deps: TurnDeps, user_id: str) -> dict:
    """履歴書のアップロードのターンに渡す事実: ファイルの表示名・トークに出した確認の吹き出し・読み取り結果の番号。
    どれも dip に保存した読み取り結果から作る(画面からは受け取らない。ファイル名はどこにも無い)。
    会話の記録にそのまま残るので、読み取り結果の画面の URL は入れない(履歴を返すときに番号から組み立てる)。
    直前のアップロードが読み取れなかった(empty)ときは、そのとき出した案内と empty を渡す(dip には保存していない)。
    読み取り結果が無ければ、会話数を使う前に断る(BadTurn)。"""
    unread = deps.unread_uploads.get(user_id)
    if unread is not None:  # 直前のアップロードは読み取れなかった: その前提で AI が1問返す
        return unread
    rec = logic.resume_upload(deps.state, user_id)
    if rec is None:
        raise BadTurn("アップロードした履歴書がありません")
    return {"file": rec.get("file") or RESUME_LABEL["application/pdf"], "shown": resume_bubbles(rec["resume"]),
            "upload_id": rec.get("id")}
