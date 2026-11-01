"""場面の一覧を回して、コードの条件を判定する(設計メモ docs/design/conversation-layer.md §5 の 1 枚目。費用 0、実 API を呼ばない)。

    ./.venv/bin/python -m evals.scenes                                  # 全部の入り口を今の版で(career c1、baito v4、talk t0)
    ./.venv/bin/python -m evals.scenes --entry career --version c1      # 1 つの入り口と版
    ./.venv/bin/python -m evals.scenes --scene t326-order -v            # 1 場面の返信と判定を詳しく
    ./.venv/bin/python -m evals.scenes --entry talk --version t1        # トークの AI の t1(移る先は c2 と v5)
    ./.venv/bin/python -m evals.scenes --log evidence/scenes            # 結果の JSON と会話ログも書く

回し方(どの版も、本番と同じ入口から):
- career と baito は、dip の相談のツールの中身(dip_mcp/career/session.py、dip_mcp/baito/session.py の start と turn)を、
  ランタイム(line_runtime.runtime._harness_turn)と同じく呼び直しで回す。status=llm のたびに台本の LLM が出力を作る。
  場面の前の会話(segment、known、asking)は、相談を始めた後のハーネスの状態に入れる。推定時間は 0 から数える
- 台本の LLM(ScriptLLM): 読む段は、場面の読む段の正解(reading。act の形)を、その版の読む段の形に写して返す
  (c1 と v4 は今の intent の語彙に写す。to_c1、to_v4)。言う段は「相づち + 問いの文」を返す。相づちは、本人の言葉(hook)か
  押したボタンに「〜ですね。」(バイトは「〜なんだね！」)を付けた、わざと単調なもの。言い回しを散らすのはハーネスの仕事なので、
  モデルの運に頼らない点にする(jyoka/tasks#326 のトーク 1、3、5 の「〜ですね。」)
- 本人が「やめる」などで相談を閉じたターンは、ランタイムがトークの AI に渡す。返事の文と、移るか・区分と値を引き継ぐかは
  トークの AI しだいなので判定しない(skip。checks._undecided)。モデルの呼び出しは少なくとも 1 回と数える
- 会話の層の版が別の入り口へ移ると閉じたとき(switch_to)は、ランタイム(line_runtime.runtime._turn)と同じく、移る先も層の版なら
  (direct)同じターンで移る先の相談を始める(移る先の session の switch_in。台本の LLM は移る先の版のもの)。移る先が今の版(v4)なら、
  今と同じくトークの AI に渡す(上と同じく判定しない。設計メモ §4.15)
- 今の LINE の入口(line_runtime/server.py の _line_kind)は、今の版(c1、v4)の相談の間とトークではスタンプを捨てるので、
  今の版の「[スタンプ]」は返事なしにする。新しい版(c2)の相談の間は、ランタイムが「[スタンプ]」の文にして渡す
- talk は、本物のランタイム(line_runtime.runtime.LineRuntime。dip の MCP も本物)を、モックのトークの AI(TalkMock。決まった文を
  返し、ツールを呼ばない)で回す。判定するのは守りの前処理(crisis_reply、llm_calls_max)と stays だけ。ほかは 9 枚目で実モデルで見る
- talk の t1(TALK_LAYER_VERSION=t1。task 361)は、同じランタイムを台本のトークの AI(TalkScript)で回し、コードの条件をすべて判定する。
  台本のトークの AI は、発言の talk(トークの AI の正解)のツールを呼び、区分を聞く 1 問のボタンはプロンプトから読む。移る先(c2、v5)の
  読む段と言う段は台本の LLM(HarnessScript。発言の reading)。トークの AI がプロンプトの決まりのとおりに動くとしたときの点

採点の条件(JUDGE_CHECKS)は skip で返す。judge を渡せば採点する(evals/scenes/judge.py。実モデルの回は 9 枚目のカード)。
実モデルで回すとき(9 枚目。evals/scenes/real.py)は、回し方(driver)の real に実モデルを置く。台本の LLM の代わりに、
ランタイムと同じメッセージ(本人の発言と、画面に出た直前のやりとり convo)でモデルを呼ぶ。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evals import scenes as S
from evals.scenes import checks as K

STICKER = "[スタンプ]"  # ランタイムがスタンプを文にした形(設計メモ §4.12)
TALK_MOCK_TEXT = "(トークの AI の返事。モックでは書かない)"
TALK_MOCK_CHECKS = {"crisis_reply", "llm_calls_max", "stays"}  # talk の場面で、モックで判定する条件
CAREER_SAY = re.compile(r"次に聞くこと\(JSON\): (\{.*\})")
CAREER_PLAN = re.compile(r"計画\(JSON\): (\{.*\})")  # c2 の応じ方の計画(dip_mcp/prompts/convo/say_ja.txt)
BAITO_SAY = re.compile(r"次に聞くこと（JSON）: (\{.*\})")
BAITO_RESULT = re.compile(r"結果材料（JSON）: (\{.*\})")
HOOK_FROM_BUTTON = 15  # 押したボタンのラベルを相づちに使う字数の上限(HOOK_MAX と同じ)


@dataclass
class Texts:
    """入り口の決まった文(部品の読み分けと、数の出どころ)。"""
    questions: list = field(default_factory=list)    # 問いの文(と言い換え、聞き返しの説明)
    phase_lines: list = field(default_factory=list)  # 区切りの行
    notes: list = field(default_factory=list)        # 注意の文
    fixed: list = field(default_factory=list)        # そのほかの決まった文(まとめ、閉じる、聞き直しなど)
    choices: list = field(default_factory=list)      # 質問バンクのボタンの全部
    crisis: list = field(default_factory=list)       # 危ない言葉への決まった文(行ごと)
    strip: list = field(default_factory=list)        # 返事の数を見る前に除く、ハーネスが付ける印の形(正規表現)


# ----------------------------- 読む段の正解を、今の版の形に写す -----------------------------
TALK_ACTS = {"chitchat", "ask_service", "ask_advice", "emotion", "story", "complaint", "falsify", "switch", "go_back",
             "retract", "crisis", "danger", "sensitive", "name", "restart"}


def _values(reading: dict) -> list[dict]:
    return [a for a in reading.get("acts", []) if a["act"] in S.VALUE_ACTS]


def _has(reading: dict, *names) -> bool:
    return any(a["act"] in names for a in reading.get("acts", []))


def _escape(reading: dict, name: str, targets: list[str]) -> bool:
    """pass / dont_know が今の問いのこと(欄を書いていないか、今の問いの欄)か。ほかの欄のことなら、今の版には語彙が無い。"""
    return any(a["act"] == name and a.get("slot") in (None, *targets) for a in reading.get("acts", []))


def to_c1(reading: dict, targets: list[str]) -> dict:
    """正社員 c1 の読む段の出力(dip_mcp/prompts/career/prompt_extract_ja.txt)に写す。値の act はどれも slot_updates
    (c1 は答え、言い足し、直す、迷うを分けない)。intent は c1 の決まり 5 の順: あとで・やめる → later、まとめて → show_result、
    ほかの候補 → more、聞き返し → clarify、今の問いのパス・覚えていない → pass / dont_know、値があれば answer、
    答えでない話(雑談、逆質問、感情、取り消し、別の入り口の話など。c1 に行き先が無い)だけなら aside、何も読めなければ answer。"""
    seg = next((a["value"] for a in _values(reading) if a["slot"] == "segment"), None)
    updates = [{"slot": a["slot"], "text": a["text"], "number": a.get("number"), "unit": a.get("unit"),
                "choice": a.get("choice")} for a in _values(reading) if a["slot"] != "segment"]
    if _has(reading, "later", "stop"):
        intent = "later"
    elif _has(reading, "show_result"):
        intent = "show_result"
    elif _has(reading, "more"):
        intent = "more"
    elif _has(reading, "clarify"):
        intent = "clarify"
    elif _escape(reading, "pass", targets):
        intent = "pass"
    elif _escape(reading, "dont_know", targets):
        intent = "dont_know"
    elif updates or seg:
        intent = "answer"
    elif any(a["act"] in TALK_ACTS | {"pass", "dont_know"} for a in reading.get("acts", [])):
        intent = "aside"
    else:
        intent = "answer"
    return {"slot_updates": updates, "ng_updates": [], "segment": seg, "intent": intent,
            "review_ack": "all" if _has(reading, "confirm") else None}


def to_v4(reading: dict, targets: list[str]) -> dict:
    """アルバイト・パート v4 の読む段の出力(dip_mcp/prompts/baito/prompt_extract_v4_ja.txt)に写す。intent は v4 の 6 種:
    最初から → restart、闇バイト → danger、結果見せて → show_result、聞き返し → clarify、値があれば answer、
    答えでない話(雑談、逆質問、感情、あとで、やめる、別の入り口の話など。v4 に行き先が無い)だけなら aside。
    パスと「わからない」は v4 に語彙が無いので、何も読めないのと同じ answer にする。"""
    seg = next((a["value"] for a in _values(reading) if a["slot"] == "segment"), None)
    updates = [{"slot": a["slot"], "value": a["value"], "evidence": a["text"]} for a in _values(reading)
               if a["slot"] != "segment"]
    if _has(reading, "restart"):
        intent = "restart"
    elif _has(reading, "danger"):
        intent = "danger"
    elif _has(reading, "show_result"):
        intent = "show_result"
    elif _has(reading, "clarify"):
        intent = "clarify"
    elif updates or seg:
        intent = "answer"
    elif any(a["act"] in TALK_ACTS | {"later", "stop", "more"} for a in reading.get("acts", [])):
        intent = "aside"
    else:
        intent = "answer"
    return {"slot_updates": updates, "ng_updates": [], "ng_removals": [], "segment": seg, "intent": intent,
            "review_ack": "all" if _has(reading, "confirm") else None}


# ----------------------------- 台本の LLM -----------------------------
class ScriptLLM:
    """ハーネスの呼び直しの 1 回分の出力を作る。say は今の本人の発言(reading を持つ)。"""

    def __init__(self, driver, say: dict, targets: list[str]):
        self.driver, self.say, self.targets = driver, say, targets

    def ack(self) -> str:
        hook = (self.say.get("reading") or {}).get("hook")
        if hook is None and self.say["button"] and len(self.say["text"]) <= HOOK_FROM_BUTTON:
            hook = self.say["text"]
        return self.driver.ack(hook)

    def __call__(self, call: dict) -> dict:
        if call["stage"] in ("extract", "read"):
            return self.driver.read(self.say.get("reading") or {"acts": []}, self.targets)
        return self.driver.write(call, self.ack())


# ----------------------------- 入り口の状態の写し -----------------------------
def _career_snap(h, active: bool) -> dict:
    from dip_mcp.career import harness as H

    cur = h.current or {}
    q = H.qmap(h, H.BANK).get(cur.get("qid"))
    return {"active": active, "version": h.version, "phase": h.phase, "qid": cur.get("qid"), "mode": cur.get("mode"),
            "targets": list(q["targets"]) if q else [], "question": q["text"] if q else None,
            "phrasings": _phrasings(h, q), "first_choices": list(q["pages"][0]) if q else [],
            "slots": {s: {k: e.get(k) for k in ("text", "value", "number", "range", "unit", "status", "confidence", "from")}
                      for s, e in h.slots.items()},
            "pending": {s: e.get("text") for s, e in h.pending.items()}, "notes": {}}


def _phrasings(h, q) -> list[str]:
    """問いの言い方(会話の層の版は、質問バンクの variants も。同じ問いの出し直しで回す。dip_mcp.career.c2.phrasings)。"""
    if q is None:
        return []
    if h.version != "c2":
        return [q["text"]]
    from dip_mcp.career import c2
    from dip_mcp.career import harness as H

    return c2.phrasings(q, H.BANK)


def _baito_snap(h, active: bool) -> dict:
    from dip_mcp.baito import harness as B
    from dip_mcp.baito import v5 as V5

    cur = h.current or {}
    qid = cur.get("qid")
    q = B.qmap(B.BANK).get("q_segment" if qid == "confirm_segment" else qid)
    question = (q["alt_text"] if cur.get("mode") == "reask" else q["text"]) if q else None
    slots = {s: {"text": B.value_ja(s, e.get("value")), "value": e.get("value"), "status": "filled" if B.is_filled(e) else None,
                 "confidence": e.get("confidence"), "from": e.get("from")} for s, e in h.slots.items()}
    if h.near.get("area"):
        slots["near.area"] = {"text": B.NEAR_AREAS.get(h.near["area"]) or h.near["area"], "value": h.near["area"],
                              "status": "filled", "confidence": "high", "from": "near"}
    return {"active": active, "version": h.version, "phase": h.phase, "qid": qid, "mode": cur.get("mode"),
            "targets": ["segment"] if qid == "confirm_segment" else (list(q["targets"]) if q else []),
            "question": question, "phrasings": V5.phrasings(q) if q and h.version == V5.VERSION else [question] if q else [],
            "first_choices": B.visible_choices(q, h) if q else [],
            "slots": slots, "pending": {s: e.get("value") for s, e in h.pending.items()}, "notes": dict(h.notes)}


def snaps(st, uid: str, last: dict) -> dict:
    """dip の状態にある相談の写し({entry: snap})。閉じた相談は、閉じたときの状態(last)を active=False で残す。"""
    from dip_mcp.baito import session as baito_session
    from dip_mcp.career import session as career_session

    u = st.user(uid)
    out = {}
    for entry, mod, snap in (("career", career_session, _career_snap), ("baito", baito_session, _baito_snap)):
        d = (u.get(mod.KEY) or {}).get("harness")
        if d:
            out[entry] = snap(mod.HARNESSES[d["version"]].State.from_dict(d), True)
        elif entry in last:
            out[entry] = snap(last[entry], False)
    return out


def profile(st, uid: str) -> dict:
    from dip_mcp import logic

    u = st.user(uid)
    return {"track": logic.search_track(u), **{k: v["value"] for k, v in logic.stated_preferences(u).items()}}


@contextlib.contextmanager
def capture_finish():
    """相談を閉じるときのハーネスの状態を覚える(session._finish は状態を消してから保存するので、閉じた後の欄を見るため)。"""
    from dip_mcp.baito import session as baito_session
    from dip_mcp.career import session as career_session

    last: dict = {}
    saved = {}
    for entry, mod in (("career", career_session), ("baito", baito_session)):
        orig = saved[entry] = mod._finish

        def wrap(state, line_user_id, h, *a, _orig=orig, _entry=entry, **kw):
            last[_entry] = type(h).from_dict(h.to_dict())
            return _orig(state, line_user_id, h, *a, **kw)
        mod._finish = wrap
    try:
        yield last
    finally:
        for entry, mod in (("career", career_session), ("baito", baito_session)):
            mod._finish = saved[entry]


@contextlib.contextmanager
def environ(values: dict):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update({k: v for k, v in values.items() if v is not None})
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _seed_profile(st, uid: str, scene: dict) -> None:
    from dip_mcp import logic

    prof = dict(scene.get("profile") or {})
    track = prof.pop("track", None)
    if track:
        logic.set_search_track(st, uid, track, evidence="(場面の前の会話)")
    if prof:
        logic.save_preferences(st, uid, prof)


# ----------------------------- 入り口ごとの今の版 -----------------------------
class _HarnessDriver:
    """career と baito の今の版の共通: dip の相談のツールを、ランタイムと同じ呼び直しで回す。"""
    entry = version = ""
    stickers = False  # 今の LINE の入口はスタンプを捨てる(返事をしない)
    real = None  # 実モデル(evals.scenes.real.RealModel)。None なら台本の LLM
    convo: list | None = None  # 画面に出たやりとり(実モデルの言う段に渡す直前のやりとり。run_harness_scene が書く)

    def llm_for(self, say: dict, targets: list[str]):
        """1 ターンのハーネスの呼び直しで、出力を作るもの(call -> 出力)。"""
        if self.real is None:
            return ScriptLLM(self, say, targets)
        return self.real.harness(self.entry, say["text"], self.convo or [])

    def env(self, scene: dict) -> dict:
        raise NotImplementedError

    def flags(self) -> dict:
        """この版の機能フラグ(ほかの入り口の場面を回すとき、移る先の版として使う)。"""
        raise NotImplementedError

    @property
    def session(self):
        raise NotImplementedError

    def setup(self, st, uid: str, scene: dict) -> None:
        raise NotImplementedError

    def output(self, llm, call: dict) -> dict:
        """呼び直しに渡す出力 1 つ。実モデルの失敗(通信、形の崩れた出力)は、ランタイム(_harness_turn)と同じく失敗の印にする
        (ハーネスが失敗 1 回と数える)。台本の LLM の失敗は場面のエラーにする。"""
        if self.real is None:
            return {"stage": call["stage"], "output": llm(call)}
        try:
            return {"stage": call["stage"], "output": llm(call)}
        except Exception as e:  # noqa: BLE001
            return {"stage": call["stage"], "error": type(e).__name__}

    def turn(self, st, uid: str, say: dict, before: dict) -> dict:
        """1 つの発言。返すのは返信の記録(states と profile は呼ぶ側が足す)。"""
        if say["text"] == STICKER and not self.stickers:
            return {"text": "", "bubbles": [], "choices": [], "counted": False, "llm_calls": 0, "how": "dropped",
                    "closed": None, "note": "LINE の入口がスタンプを捨てる"}
        llm = self.llm_for(say, (before.get(self.entry) or {}).get("targets") or [])
        outputs: list[dict] = []
        r = self.session.turn(st, uid, say["text"], None)
        while r.get("status") == "llm":
            outputs.append(self.output(llm, r["call"]))
            r = self.session.turn(st, uid, say["text"], outputs)
        if not r.get("active") and r.get("direct"):  # 層の版から層の版へ移る: 同じターンで移る先の相談を始める
            moved = self.switch_in(st, uid, say, r["switch_to"], len(outputs))
            if moved is not None:
                return moved
        if not r.get("active"):  # やめる合図・区分が外れた・今の版へ移る: 相談を閉じて、トークの AI がこのターンを受ける
            return {"text": "", "bubbles": [], "choices": [], "counted": False, "llm_calls": len(outputs) + 1, "how": "talk",
                    "closed": r.get("ended") or "exit", "note": "相談を閉じてトークの AI に渡した"}
        return {"text": r["reply"], "bubbles": list(r.get("bubbles") or [r["reply"]]), "choices": list(r["choices"]),
                "counted": bool(r["counted"]), "llm_calls": int(r.get("llm_calls", len(outputs))), "how": "harness",
                "closed": self.closed(r)} | ({"parts": r["parts"]} if r.get("parts") else {})

    def closed(self, r: dict) -> str | None:
        return None

    def switch_in(self, st, uid: str, say: dict, to: str, used: int) -> dict | None:
        """移る先(to)の相談を、移る先の session の switch_in で始める(ランタイムの _switch_in と同じ)。始めなければ None。"""
        dest = layer_driver(to)
        if dest is None:
            return None
        dest.real, dest.convo = self.real, self.convo
        llm = dest.llm_for(say, [])
        outputs: list[dict] = []
        r = dest.session.switch_in(st, uid, say["text"], None)
        while r.get("status") == "llm":
            outputs.append(dest.output(llm, r["call"]))
            r = dest.session.switch_in(st, uid, say["text"], outputs)
        if not r.get("active"):
            return None
        return {"text": r["reply"], "bubbles": list(r.get("bubbles") or [r["reply"]]), "choices": list(r["choices"]),
                "counted": bool(r["counted"]), "llm_calls": used + int(r.get("llm_calls", len(outputs))), "how": "harness",
                "closed": None, "switched": {"from": self.entry, "to": to}} | ({"parts": r["parts"]} if r.get("parts") else {})


class CareerC1(_HarnessDriver):
    """正社員ヒアリングの今の版(CAREER_HARNESS_VERSION=c1)。形は場面の form(無ければ C)。"""
    entry, version = "career", "c1"

    @property
    def session(self):
        from dip_mcp.career import session

        return session

    def env(self, scene):
        from dip_mcp.career import harness as H

        return self.flags() | {H.FORM_ENV: scene.get("form", "C")}

    def flags(self):
        from dip_mcp import logic

        return {logic.CAREER_HARNESS_FLAG: self.version}

    def harness(self):
        from dip_mcp.career import harness as H

        return H

    def setup(self, st, uid, scene):
        from dip_mcp.career import harness as H

        s = self.session
        s.start(st, uid)
        h = self.harness().State.from_dict(st.user(uid)[s.KEY]["harness"])
        h.segment = scene["segment"]
        qs = H.questions(h, H.BANK)
        for slot, k in (scene.get("known") or {}).items():
            e = H.entry(k.get("status", "filled"), k.get("text", ""), k.get("value"), k.get("number"), k.get("range"),
                        k.get("unit"), k.get("confidence", "high"), k.get("from", "known"))
            (h.slots if slot in H.active_slots(h) else h.pending)[slot] = e
        h.asked = [q["id"] for q in qs if set(q["targets"]) & set(scene.get("known") or {})]
        h.said = [t["text"] for t in scene.get("history") or [] if t["who"] == "user"]
        self.seed(h, scene)
        for q in qs:  # 分かっている欄の問いの数だけ、その問いのフェーズでターンを数えたことにする
            if set(q["targets"]) & set(scene.get("known") or {}):
                h.phase_turns[q["phase"]] = h.phase_turns.get(q["phase"], 0) + 1
        h.turns = sum(h.phase_turns.values())
        asking = scene["asking"]
        if asking == "review":
            h.phase, h.stop_reason = "review", "done"
            h.last_card = H.build_card(h)
            h.current = {"qid": "review", "mode": "review", "choices": [H.REVIEW_OK]}
        elif asking != "start":
            q = H.qmap(h, H.BANK)[asking]
            h.phase = q["phase"]
            H._present(h, q)
        s._save(st, uid, h)

    def seed(self, h, scene) -> None:
        """版の覚えに、場面の前の会話を入れる(c1 には無い)。"""

    def read(self, reading, targets):
        return to_c1(reading, targets)

    def ack(self, hook):
        return f"{hook}ですね。" if hook else "わかりました。"

    def write(self, call, ack):
        q = json.loads(CAREER_SAY.search(call["system"]).group(1))
        return {"reply": q["text"] if q.get("mode") == "clarify" else ack + q["text"]}

    def closed(self, r):
        if r["status"] != "result":
            return None
        return "later" if r.get("closed") == "later" else "result"

    def texts(self) -> Texts:
        from dip_mcp.baito import harness as B
        from dip_mcp.career import harness as H

        qs = []
        for form in H.FORMS:
            for seg in (*H.SEGMENTS, "unknown"):
                st = H.new_state(form)
                st.segment = seg
                qs += H.questions(st, H.BANK)
        questions = {x for q in qs for x in (q.get("text"), q.get("explain"), q.get("text_known")) if x}
        fixed = [H.START_LEAD, H.START_LEAD_UI, H.REVIEW_LEAD, H.REVIEW_LEAD_UI, H.REVIEW_FOCUS_LEAD, H.RESULT_TEXT,
                 H.LATER_TEXT, H.CLOSED_TEXT, H.RETRY_TEXT, H.CLARIFY_AGAIN, H.CLARIFY_SKIP, H.MORE_DONE, H.FIX_LEAD,
                 H.FIX_LIMIT_LEAD, H.ASIDE_FALLBACK]
        return Texts(questions=sorted(questions), phase_lines=list(H.PHASE_LINES.values()), fixed=fixed,
                     choices=sorted({c for q in qs for c in [*q["choices"], *q.get("escape", [])]}), crisis=list(B.CRISIS["reply"]),
                     strip=[r"✓職歴\d+", r"」\d+/\d+"])  # 形 C の進み具合の行(_progress)の、職歴の番号と「n/3」


def layer_ack(driver, plan: dict, ack: str) -> str:
    """層の版の台本の相づち: 計画が相づちなしなら付けない。相づちに入れる言葉(計画の mention。移った先の最初の返事の、
    引き継いだ値。task 379)があり、単調な相づちに無ければ、その 1 つ目を本人の言葉の代わりにした相づちにする。"""
    if plan.get("ack", "short") is None:
        return ""
    mention = plan.get("mention") or []
    if mention and not any(w in ack for w in mention):
        return driver.ack(mention[0])
    return ack


class CareerC2(CareerC1):
    """正社員ヒアリングの会話の層の版(CAREER_HARNESS_VERSION=c2。dip_mcp/career/c2.py)。読む段は act の並びをそのまま読むので、
    台本の LLM は場面の reading をそのまま返す。前の会話(history)は、層の覚え(直前のやりとりと返信、その相づち)に入れる。
    言う段の台本は c1 と同じ「相づち + 問いの文」(問いの文は、ハーネスが勧める言い方)。返信には部品の並び(parts)が付く。
    スタンプは、新しい版の相談の間はランタイムが「[スタンプ]」の文にして渡す(line_runtime/server.py の _line_kind。task 356)。"""
    version = "c2"
    stickers = True
    _texts: Texts | None = None

    def harness(self):
        from dip_mcp.career import c2

        return c2

    def seed(self, h, scene):
        from dip_mcp.convo import state as CS

        for t in scene.get("history") or []:
            if t["who"] == "user":
                CS.heard(h.convo, t["text"])
            else:
                CS.replied(h.convo, t["text"], K.ack_of(K.split_parts(t["text"], self.texts())))

    def read(self, reading, targets):
        return {"acts": list(reading.get("acts", [])), "hook": reading.get("hook"), "mentions": list(reading.get("mentions") or []),
                "ng": []}

    def write(self, call, ack):
        """計画が相づちなし(逆質問、事実と違う頼み)なら、相づちを付けない。ほかは c1 と同じ単調な相づち。"""
        m = CAREER_PLAN.search(call["system"])
        return super().write(call, layer_ack(self, json.loads(m.group(1)) if m else {}, ack))

    def texts(self) -> Texts:
        from dip_mcp.career import c2
        from dip_mcp.career import harness as H

        if CareerC2._texts is not None:
            return CareerC2._texts
        t = super().texts()
        variants = [x.replace("{which}", w) for q in H.BANK for x in q.get("variants") or [] for w in ("直近の", "その前の")]
        t.questions = sorted({*t.questions, *variants})
        t.notes = [*t.notes, *(c2.ENTRY.texts[k] for k in ("retry", "retry_number", "no_answer"))]
        t.crisis = c2.CRISIS_TEXT.split("\n")
        t.notes = [*t.notes, *(c2.ENTRY.texts[f"note_{n}"] for n in c2.P.NOTES)]  # 注意の 1 行(応じ方の計画。task 356)
        t.fixed = [*t.fixed, *(c2.ENTRY.texts[k] for k in ("crisis_resume", "retry_review", "more_done"))]
        t.notes = [*t.notes, *(s["note"] for s in c2.ENTRY.switch)]  # 移れることの注意(task 359)
        CareerC2._texts = t
        return t


class BaitoV4(_HarnessDriver):
    """アルバイト・パートの相談の今の版(BAITO_HARNESS_VERSION=v4)。場面は入口のカードの後(PRD の流れの途中)。"""
    entry, version = "baito", "v4"

    @property
    def session(self):
        from dip_mcp.baito import session

        return session

    def env(self, scene):
        return self.flags()

    def flags(self):
        from dip_mcp import logic

        return {logic.BAITO_HARNESS_FLAG: self.version}

    def setup(self, st, uid, scene):
        from dip_mcp.baito import harness as B

        s = self.session
        s.start(st, uid, quick=False)
        d = st.user(uid)[s.KEY]["harness"]
        h = s.HARNESSES[d["version"]].State.from_dict(d)
        h.segment = scene["segment"]
        known = dict(scene.get("known") or {})
        if "near.area" in known:
            h.near["area"] = known.pop("near.area")["value"]
        for slot, k in known.items():
            h.slots[slot] = {"value": k["value"], "confidence": k.get("confidence", "high"), "from": k.get("from", "known")}
        B.recompute_derived(h)
        h.asked, h.topic_count, h.attempts, h.phase_turns = [], {}, {}, {}
        for q in B.BANK:
            if set(q["targets"]) & set(known) and q["phase"] in B.PRE_REVIEW:
                h.asked.append(q["id"])
                h.phase_turns[q["phase"]] = h.phase_turns.get(q["phase"], 0) + 1
        h.turns = sum(h.phase_turns.values())
        asking = scene["asking"]
        if asking == "review":
            h.phase = "review"
            h.last_card = B.build_card(h)
            h.current = {"qid": "review", "mode": "review", "choices": [B.REVIEW_OK]}
        else:
            q = B.qmap(B.BANK)[asking]
            h.phase = q["phase"]
            B._present(h, q, "ask")
        self.seed(h, scene)
        s._save(st, uid, h)

    def seed(self, h, scene) -> None:
        """版の覚えに、場面の前の会話を入れる(v4 には無い)。"""

    def read(self, reading, targets):
        return to_v4(reading, targets)

    def ack(self, hook):
        return f"{hook}なんだね！" if hook else "OK！"

    def write(self, call, ack):
        from dip_mcp.baito import harness as B

        system = call["system"]
        if call["stage"] == "review":
            card = system.split("- カード本文: ", 1)[1].split("\n- カード（JSON）", 1)[0]
            return {"reply": "まとめたよ！\n" + card + "\n\n" + B.CLOSING_JA}
        if call["stage"] == "result":
            r = json.loads(BAITO_RESULT.search(system).group(1))
            return {"reply": "選んだよ！", "recommendations": [{"job_id": c["job_id"], "reason": "合いそうだから"}
                                                            for c in r["candidates"][:r["need"]]]}
        q = json.loads(BAITO_SAY.search(system).group(1))
        return {"reply": ack + q["text"]}

    def closed(self, r):
        return "result" if r["status"] == "result" and not r.get("blocked") else None

    def texts(self) -> Texts:
        from dip_mcp.baito import harness as B

        questions = {x for q in B.BANK for x in (q.get("text"), q.get("alt_text")) if x}
        questions |= {B.QUICK_TEXT, B.QUICK_RESULT_TEXT, *B.NEAR_TEXT.values()}
        fixed = [B.CLOSING_JA, B.CLARIFY_TEXT, B.RETRY_TEXT, B.CONFIRM_TEXT, B.POST_RESULT_TEXT, B.GUESS_TAIL, B.LIGHT_TEXT,
                 B.QUICK_LEAD, B.QUICK_AGAIN_TEXT, B.QUICK_TAP_TEXT, B.CRISIS["resume_lead"]]
        return Texts(questions=sorted(questions), notes=list(B.NOTICE.values()) + list(B.CRISIS["reply"]), fixed=fixed,
                     choices=sorted({c for q in B.BANK for c in q["choices"]}), crisis=list(B.CRISIS["reply"]))


class BaitoV5(BaitoV4):
    """アルバイト・パートの相談の会話の層の版(BAITO_HARNESS_VERSION=v5。dip_mcp/baito/v5.py)。読む段は act の並びをそのまま読むので、
    台本の LLM は場面の reading をそのまま返す。前の会話(history)は層の覚えに入れる。問いのターンの言う段は層のプロンプト
    (「相づち + 問いの文」。正社員の c2 と同じ台本)、まとめと結果は v4 と同じ台本。スタンプは「[スタンプ]」の文にして渡す。
    移る先の正社員は、場面を回すときに版を渡さなければ層の版(c2)にする(入り口を移す場面を、同じターンの移り方で判定するため)。"""
    version = "v5"
    stickers = True
    _texts: Texts | None = None

    def env(self, scene):
        from dip_mcp import logic

        return {logic.CAREER_HARNESS_FLAG: "c2"} | self.flags()

    def seed(self, h, scene):
        CareerC2.seed(self, h, scene)

    def read(self, reading, targets):
        return CareerC2.read(self, reading, targets)

    def write(self, call, ack):
        """層の言う段(計画のあるプロンプト)は c2 と同じ台本。まとめ、結果、入口のカードのリアクションは v4 の台本。"""
        m = CAREER_PLAN.search(call["system"])
        if call["stage"] != "say" or not m:
            return super().write(call, ack)
        q = json.loads(CAREER_SAY.search(call["system"]).group(1))
        return {"reply": layer_ack(self, json.loads(m.group(1)), ack) + q["text"]}

    def closed(self, r):
        return r.get("closed") or super().closed(r)

    def texts(self) -> Texts:
        from dip_mcp.baito import v5 as V5

        if BaitoV5._texts is not None:
            return BaitoV5._texts
        t = super().texts()
        t.questions = sorted({*t.questions, *(x for xs in V5.VARIANTS.values() for x in xs)})
        t.notes = [*t.notes, *(V5.ENTRY.texts[k] for k in ("retry", "no_answer", *(f"note_{n}" for n in V5.P.NOTES))),
                   *(s["note"] for s in V5.ENTRY.switch)]
        t.fixed = [*t.fixed, *(V5.ENTRY.texts[k] for k in ("later", "exit", "retry_review"))]
        BaitoV5._texts = t
        return t


class TalkT0:
    """トークの AI の今の動き(TALK_LAYER_VERSION が無い)。本物のランタイムを、モックのトークの AI で回す。
    ハーネスの機能フラグは本番の既定(v4、c1)にする。"""
    entry, version = "talk", "t0"
    scripted = False  # モックのトークの AI(決まった文。ツールを呼ばない)。判定するのは TALK_MOCK_CHECKS だけ

    def env(self, scene=None):
        from dip_mcp import logic

        return {logic.BAITO_HARNESS_FLAG: "v4", logic.CAREER_HARNESS_FLAG: "c1", logic.TALK_LAYER_FLAG: None,
                "FASTMCP_LOG_LEVEL": "WARNING"}  # dip の MCP の子プロセスの、リクエストごとのログを出さない

    def texts(self) -> Texts:
        from dip_mcp.baito import harness as B

        return Texts(crisis=list(B.CRISIS["reply"]))

    def harness_drivers(self) -> dict:
        """移る先の相談の回し方(台本の LLM。t1 でトークの AI が相談を始めたとき)。t0 は使わない。"""
        return {}


class TalkT1(TalkT0):
    """トークの AI の t1(TALK_LAYER_VERSION=t1。dip_mcp/convo/talk.py)。本物のランタイムを、台本のトークの AI(TalkScript)で回す。
    台本のトークの AI は、プロンプトの決まりのとおりに動くとしたときの動き: 発言の talk(正解)のツールを呼び、entry_question なら
    プロンプトにある入り口を選ぶボタンで区分を 1 問聞く。相談の移る先は会話の層の版(c2、v5)で、移る先の読む段と言う段は台本の LLM
    (発言の reading)。守りの前処理はランタイムのコード。どのコードの条件も判定する(返事の文は台本なので、採点の条件は 9 枚目)。"""
    version = "t1"
    scripted = True

    def env(self, scene=None):
        from dip_mcp import logic

        return super().env(scene) | {logic.CAREER_HARNESS_FLAG: "c2", logic.BAITO_HARNESS_FLAG: "v5", logic.TALK_LAYER_FLAG: "t1"}

    def texts(self) -> Texts:
        from dip_mcp.convo import talk

        return Texts(crisis=talk.CRISIS_TEXT.split("\n"))

    def harness_drivers(self) -> dict:
        return {"career": CareerC2(), "baito": BaitoV5()}


VERSIONS = {"career": {"c1": CareerC1, "c2": CareerC2}, "baito": {"v4": BaitoV4, "v5": BaitoV5},
            "talk": {"t0": TalkT0, "t1": TalkT1}}
CURRENT = {"career": "c1", "baito": "v4", "talk": "t0"}  # 今の本番の版(既定。BAITO_HARNESS_VERSION=v4 など)


def layer_driver(entry: str):
    """新しく始める、その入り口の相談が会話の層の版なら、その版の回し方(入り口を移す先。dip_mcp.logic.layer_on)。違えば None。"""
    from dip_mcp import logic

    if not logic.layer_on(entry):
        return None
    version = logic.career_harness_version() if entry == "career" else logic.baito_harness_version()
    return VERSIONS[entry][version]() if version in VERSIONS[entry] else None


def driver(entry: str, version: str | None = None):
    version = version or CURRENT[entry]
    if version not in VERSIONS[entry]:
        raise ValueError(f"{entry} の版 {version} は、まだ回せない(回せる版: {', '.join(VERSIONS[entry])})")
    return VERSIONS[entry][version]()


# ----------------------------- 回す -----------------------------
def _uid(scene: dict) -> str:
    return f"scene-{scene['id']}"


def run_harness_scene(scene: dict, d, state_path: Path, others: dict | None = None) -> dict:
    """career と baito の 1 場面を回して、記録(checks の形)を返す。others は、ほかの入り口の版の機能フラグ(入り口を移す先の版)。
    実モデル(d.real)のときは、ターンごとのモデルの時間と呼び出しの数(model_seconds、model_calls)も記録に足す。"""
    from dip_mcp.data import State
    from line_runtime import baito_chat

    st, uid = State(state_path), _uid(scene)
    replies, error = [], None
    d.convo = []
    for t in scene.get("history") or []:
        baito_chat._append(d.convo, "user" if t["who"] == "user" else "assistant", t["text"])
    with environ(d.env(scene) | (others or {}) | {"DIP_STATE_PATH": str(state_path)}), capture_finish() as last:
        try:
            _seed_profile(st, uid, scene)
            d.setup(st, uid, scene)
            before = snaps(st, uid, last)
            for say in scene["says"]:
                if d.real is not None:
                    d.real.begin()
                r = d.turn(st, uid, say, before)
                if d.real is not None:
                    r |= d.real.end()
                if r["how"] != "dropped":
                    baito_chat._append(d.convo, "user", say["text"])
                    baito_chat._append(d.convo, "assistant", r["text"])
                before = r["states"] = snaps(st, uid, last)
                r["profile"] = profile(st, uid)
                r["say"] = say
                replies.append(r)
        except Exception as e:  # noqa: BLE001  1 場面の失敗で全体を止めない(記録に残す)
            error = f"{type(e).__name__}: {e}"
    return {"scene": scene["id"], "entry": d.entry, "version": d.version, "replies": replies, "error": error,
            "texts": d.texts()}


def _talk_model():
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class TalkMock(BaseChatModel):
        """モックのトークの AI: いつも決まった文を返し、ツールを呼ばない(呼ばれた数を数える)。"""
        calls: int = 0

        @property
        def _llm_type(self) -> str:
            return "scene-talk-mock"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls += 1
            body = {"bubbles": [TALK_MOCK_TEXT], "quick_replies": [], "job_ids": [], "buttons": [], "actions": []}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(body, ensure_ascii=False)))])

    return TalkMock()


ENTRY_BUTTONS_RE = re.compile(r"付ける: ((?:「[^」]+」)+)")  # t1 のプロンプトの、入り口を選ぶボタン(dip_mcp.convo.talk.prompt_section)
TALK_SCRIPT_ASK = "どちらで探しますか?"  # 台本のトークの AI が区分を聞く 1 問


def _talk_script():
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    class TalkScript(BaseChatModel):
        """台本のトークの AI(t1): プロンプトの決まりのとおりに動くとしたときの動き。say(今の発言)の talk(正解)のツールを
        1 回の呼び出しでまとめて呼び、ツールの結果の後に返信を返す。entry_question なら、プロンプトにある入り口を選ぶボタンを
        quick_replies にして区分を 1 問聞く(ボタンはプロンプトから読む。プロンプトに無ければボタンなし)。呼ばれた数を数える。"""
        calls: int = 0
        say: dict = {}  # noqa: RUF012  pydantic の項目

        @property
        def _llm_type(self) -> str:
            return "scene-talk-script"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls += 1
            talk = self.say.get("talk") or {}
            last = max(i for i, m in enumerate(messages) if m.type == "human")
            called = any(m.type == "tool" for m in messages[last + 1:])
            if talk.get("tools") and not called:
                calls = [{"name": c["name"], "args": {"line_user_id": "me", **c["args"]}, "id": f"scene-{self.calls}-{i}",
                          "type": "tool_call"} for i, c in enumerate(talk["tools"])]
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))])
            body = {"bubbles": [TALK_MOCK_TEXT], "quick_replies": [], "job_ids": [], "buttons": [], "actions": []}
            if talk.get("entry_question"):
                m = ENTRY_BUTTONS_RE.search(messages[0].content if messages[0].type == "system" else "")
                body |= {"bubbles": [TALK_SCRIPT_ASK], "quick_replies": re.findall(r"「([^」]+)」", m.group(1)) if m else []}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(body, ensure_ascii=False)))])

    return TalkScript()


class HarnessScript:
    """ランタイムのハーネスのモデル(baito_llm、career_llm)の代わり: 今の発言(say)の台本の LLM(ScriptLLM)。呼ばれた数を数える。"""

    def __init__(self, driver):
        self.driver, self.say, self.calls = driver, {}, 0

    async def __call__(self, stage, system, messages):
        self.calls += 1
        return ScriptLLM(self.driver, self.say, [])({"stage": stage, "system": system}), {}


async def _starts_new(rt, uid: str, say: dict) -> bool:
    """この発言で新しい会話を始めるか(LINE のランタイムと同じ決まり。server._line_turn): 会話がまだ無ければ始め、発言の前に時間が空いた
    (after_minutes)ら、前の問いのボタンの答えでなければ始める(§4.16)。空いていなければ続ける。"""
    from line_runtime.turn import answers_last_question

    if not await rt.current_thread(uid):
        return True
    if not say.get("after_minutes"):
        return False
    now = datetime.now(UTC) + timedelta(minutes=say["after_minutes"])
    return not answers_last_question(await rt.conversation(uid), say["text"], now)


def run_talk_scenes(scenes: list[dict], d, state_path: Path, real=None, before_scene=None) -> list[dict]:
    """talk の場面を、1 つのランタイムで回す(dip の MCP の子プロセスを場面ごとに起こさない)。t1 は台本のトークの AI と、
    移る先の台本の LLM で回す。real(evals.scenes.real.RealModel)を渡すと、トークの AI もハーネスのモデルも実モデルで回し、
    呼び出しの数は real が数える(どのコードの条件も判定する)。実モデルの回は、トークの AI がそのターンに呼んだツールの名前と引数も残す(tools)。before_scene(scene) は場面を始める前に呼ぶ(費用の確かめ)。"""
    from dip_mcp.data import State

    if real is not None:
        model, harness = None, {}  # None: ランタイムの既定(本番と同じ実モデル)
        llms = {"baito": real.async_harness("baito"), "career": real.async_harness("career")}
    else:
        model = _talk_script() if d.scripted else _talk_model()
        harness = {k: HarnessScript(v) for k, v in d.harness_drivers().items()}
        llms = {k: harness.get(k) for k in ("baito", "career")}
    records = []

    def calls() -> int:
        if real is not None:
            return real.calls()
        return model.calls + sum(h.calls for h in harness.values())

    async def go():
        from line_runtime.runtime import LineRuntime

        async with LineRuntime(model=model, in_memory=True, baito_llm=llms["baito"], career_llm=llms["career"]) as rt:
            for scene in scenes:
                if before_scene is not None:
                    before_scene(scene)
                st, uid = State(state_path), _uid(scene)
                replies, error = [], None
                try:
                    _seed_profile(st, uid, scene)
                    for say in scene["says"]:
                        if d.scripted and real is None:
                            model.say = say
                        for h in harness.values():
                            h.say = say
                        n = calls()
                        t0 = time.perf_counter()
                        out = await rt.handle(uid, "message", say["text"], new_conversation=await _starts_new(rt, uid, say))
                        took = time.perf_counter() - t0
                        text = "\n".join(out["bubbles"])
                        used = calls() - n
                        how = "guard" if out.get("guard") else "harness" if any(k in out for k in ("career", "baito")) else "talk"
                        replies.append({"text": text, "bubbles": list(out["bubbles"]), "choices": list(out["quick_replies"]),
                                        "counted": False, "llm_calls": used, "how": how, "closed": None,
                                        "states": snaps(st, uid, {}), "profile": profile(st, uid), "say": say}
                                       | ({"model_calls": used, "turn_seconds": round(took, 3),
                                           "tools": [{"name": t["name"], "args": {k: v for k, v in (t.get("args") or {}).items()
                                                                                   if k != "line_user_id"}}
                                                     for t in out.get("tools") or []]} if real is not None else {}))
                except Exception as e:  # noqa: BLE001
                    error = f"{type(e).__name__}: {e}"
                records.append({"scene": scene["id"], "entry": "talk", "version": d.version, "replies": replies,
                                "error": error, "texts": d.texts(), "talk_mock": not d.scripted and real is None})

    with environ(d.env() | {"DIP_STATE_PATH": str(state_path)}):
        asyncio.run(go())
    return records


def score(scene: dict, record: dict, judge=None) -> dict:
    """記録の条件を判定する。judge(scene, record, checks) を渡せば、採点の条件もジャッジが判定する。"""
    if record["error"] or len(record["replies"]) != len(scene["says"]):
        results = [{"check": c["check"], "at": c.get("at"), "args": {}, "status": K.FAIL,
                    "detail": f"場面を回せなかった: {record['error']}"} for c in scene["expect"]]
    else:
        results = K.evaluate(scene, record, TALK_MOCK_CHECKS if record.get("talk_mock") else None)
        if judge is not None:
            asked = [c for c in scene["expect"] if not S.is_code(c)]
            verdicts = iter(judge(scene, record, asked) if asked else [])
            results = [next(verdicts) if r.get("judge") else r for r in results]
    return {"scene": scene["id"], "entry": scene["entry"], "category": scene["category"], "version": record["version"],
            "results": results, "replies": record["replies"], "error": record["error"]}


def run(entries=None, versions: dict | None = None, ids=None, judge=None) -> list[dict]:
    """場面を回して判定する。entries は入り口(既定は全部)、versions は {entry: version}(既定は今の版)、ids は場面の id。"""
    scenes = [s for s in S.load_scenes() if (not entries or s["entry"] in entries) and (not ids or s["id"] in ids)]
    out = []
    with tempfile.TemporaryDirectory(prefix="scenes-") as tmp:
        path = Path(tmp) / "state.json"
        for entry in S.ENTRIES:
            mine = [s for s in scenes if s["entry"] == entry]
            if not mine:
                continue
            d = driver(entry, (versions or {}).get(entry))
            others = {k: v for e, ver in (versions or {}).items() if e not in (entry, "talk")
                      for k, v in driver(e, ver).flags().items()}  # 移る先の版(渡したときだけ)
            records = run_talk_scenes(mine, d, path) if entry == "talk" else [run_harness_scene(s, d, path, others) for s in mine]
            out += [score(s, r, judge) for s, r in zip(mine, records, strict=True)]
    return out


# ----------------------------- 点 -----------------------------
GUARD_CHECKS = ("crisis_reply", "no_made_up_number", "no_slot_contains")  # 守りの条件(設計メモ §4.10 の合格の線で 100%)


def tally(rows: list[dict]) -> dict:
    """点: 入り口と版ごとの、コードの条件の合格の数と率、種類ごと、守りの条件、判定しなかった条件の数。"""
    out = {}
    for r in rows:
        key = f"{r['entry']} {r['version']}"
        t = out.setdefault(key, {"scenes": 0, "scenes_judged": 0, "scenes_all_pass": 0, "pass": 0, "fail": 0, "skip_code": 0, "judge": 0,
                                 "guard_pass": 0, "guard": 0, "by_category": {}, "by_check": {}, "errors": 0})
        t["scenes"] += 1
        t["errors"] += bool(r["error"])
        code = [x for x in r["results"] if not x.get("judge") and x["status"] != K.SKIP]
        if code:  # モックで判定したコードの条件が 1 つも無い場面(talk の多く)は数えない
            t["scenes_judged"] += 1
            t["scenes_all_pass"] += all(x["status"] == K.PASS for x in code)
        cat = t["by_category"].setdefault(r["category"], {"pass": 0, "fail": 0})
        for x in r["results"]:
            if x.get("judge"):
                t["judge"] += 1
                continue
            if x["status"] == K.SKIP:
                t["skip_code"] += 1
                continue
            t[x["status"]] += 1
            cat[x["status"]] += 1
            ch = t["by_check"].setdefault(x["check"], {"pass": 0, "fail": 0})
            ch[x["status"]] += 1
            if x["check"] in GUARD_CHECKS:
                t["guard"] += 1
                t["guard_pass"] += x["status"] == K.PASS
    return out


def rate(p: int, n: int) -> str:
    return f"{p}/{n}({p / n:.0%})" if n else "0/0"


def report(rows: list[dict], verbose: bool = False) -> str:
    lines = []
    for r in rows:
        bad = [x for x in r["results"] if x["status"] == K.FAIL]
        code = [x for x in r["results"] if not x.get("judge") and x["status"] != K.SKIP]
        mark = "×" if bad else "○" if code else "-"  # - はモックで判定したコードの条件が無い場面
        lines.append(f"{mark} {r['entry']}/{r['scene']}  コード {sum(x['status'] == K.PASS for x in code)}/{len(code)}"
                     + (f"  エラー: {r['error']}" if r["error"] else ""))
        for x in (r["results"] if verbose else bad):
            at = "" if x["at"] is None else f"[{x['at']}]"
            lines.append(f"    {x['status']:<4} {x['check']}{at}: {x['detail']}")
        if verbose:
            for i, rep in enumerate(r["replies"]):
                lines.append(f"    本人{i}: {'[ボタン] ' if rep['say']['button'] else ''}{rep['say']['text']}")
                lines.append(f"    返信{i}({rep['how']}、呼び出し {rep['llm_calls']} 回): " + rep["text"].replace("\n", " / ")
                             + (f"  [ボタン] {' / '.join(rep['choices'])}" if rep["choices"] else ""))
    lines.append("")
    lines.append("| 入り口 版 | 場面 | コードの条件をすべて満たした場面 | コードの条件 | 守りの条件 | モックで判定しない条件 | 採点の条件 |")
    # 「すべて満たした場面」の分母は、モックで判定したコードの条件が 1 つ以上ある場面
    lines.append("|---|---|---|---|---|---|---|")
    for key, t in tally(rows).items():
        lines.append(f"| {key} | {t['scenes']} | {rate(t['scenes_all_pass'], t['scenes_judged'])} | "
                     f"{rate(t['pass'], t['pass'] + t['fail'])} | {rate(t['guard_pass'], t['guard'])} | {t['skip_code']} | "
                     f"{t['judge']} |")
    return "\n".join(lines)
