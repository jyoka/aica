"""バイト探しの相棒の相談を、dip のユーザー状態に保存して1ターンずつ進める(dip のツールの中身。dip は LLM を呼ばない)。

- 始める: トークの AI が、アルバイト・パートの相談に入ったターンに start_baito_consultation を呼ぶ
  (BAITO_HARNESS_VERSION=v4 のときだけ。PRD §13-3 の機能フラグ。既定は使わない = 本番は切り替えない)。
  最初の質問(q_money)は、その AI が言い換えずに聞く
- 進める: その後の本人の発言は、LINE 側のランタイムが baito_turn に渡す(トークの AI は呼ばない)。
  ハーネスの turn()(抽出 → 判断 → 生成。dip_mcp/baito/harness.py)を、保存した状態から毎回やり直す。
  turn() がモデルを呼ぶところで、まだ受け取っていない出力があれば、status=llm でそのプロンプトを返す。
  ランタイムがモデルを呼び、それまでの出力を全部 llm_outputs に入れて呼び直す。turn() は同じ状態と同じ出力からは
  同じ道をたどるので、最後まで進んだ回だけ状態を保存する(途中の回は保存しない)
- 終える: 結果を出したら、確かめた条件を stated_preferences に保存して相談を閉じる(プロフィール画面で見て直せる)。
  中学生以下で止めた結果だけは閉じない(その後の発言もハーネスが受け、年齢の訂正でブロックを外す。PRD §5-7)。
  本人がやめると言った・区分が外れた・新しい会話を始めたら、そこまでに本人が答えた条件(high)だけを保存して閉じ、
  トークの AI に戻す

状態は 1 人の dict の "baito"(進めている間だけ)。「すべて消す」は 1 人の dict ごと消すので、これも消える。
"""

from __future__ import annotations

import re

from dip_mcp import logic
from dip_mcp.baito import harness as H
from dip_mcp.data import State

KEY = "baito"
# 中学生以下で止めた印(相談を閉じた後も残す)。残っている間は求人を出さない(recommend_jobs・relax_conditions。job_block)。
# 後の相談で 15 歳以上と確かめたら消す。「すべて消す」は 1 人の dict ごと消すので、これも消える
BLOCK_KEY = "baito_block"
FLAG = logic.BAITO_HARNESS_FLAG  # PRD §13-3。v4 のときだけ新しい相談を始める。始めた相談は版を変えずに最後まで進める
# LINE 側のランタイムだけが呼ぶツール(トークの AI には見せない。line_runtime.runtime.chat_tools が外す)
RUNTIME_TOOLS = ("baito_turn", "end_baito_consultation")
START_TOOL = "start_baito_consultation"
# 結果の後の選択肢(dip の Prompt の、求人を見るかの問いの選択肢と同じ文言)
RESULT_CHOICES = ["求人を見る", "今はいい"]
# 相談をやめる本人の発言。選択肢そのもの(今の質問の choices)は、やめる合図にしない
EXIT_WORDS = {"今はいい", "やめる", "やめます", "やめたい", "やめておく", "終わり", "おわり", "もういい"}
# 相談の外へ出る頼み(review の前だけ。review の「これでOK、求人見せて」はカードへの返事として読む):
# 求人を見せてほしい(「求人を探すのは初めて」のような答えは当たらない)、正社員で探したい(区分の切り替えはトークの AI がする)。
# 「正社員で働いてて、副業を探してます」のような今の働き方の答えは当たらない
EXIT_REQUEST = re.compile(r"求人を?(見せて|見たい|探して|出して|紹介して|教えて)"
                          r"|正社員(で|の(仕事|求人)を?)\s*(探|さが|見)|やっぱり正社員|正社員(が|に)(いい|なりたい)")
# 頼みに見えても、後回しや打ち消しなら相談を続ける(「求人を見たいけど、先に条件を整理したい」「正社員じゃなくて」)
EXIT_HOLD = re.compile(r"けど|けれど|前に|先に|まだ|あとで|後で|じゃな|ではな|わけでは|なくて")
BAITO_TRACK = "アルバイト・パート"
START_NOTE = ("相談を始めた。この返信で、一緒に整理することを1文で伝え、question の text を言い換えずに1問として聞く"
              "(選択肢は LINE 側が question の choices にする)。次の質問からはハーネスが進める。")
MAX_LLM_CALLS = 4  # 1 ターンのモデルの呼び出しの上限(PRD §5-4: 文の答えで抽出 2 回 + 生成 2 回)


def enabled() -> bool:
    return logic.baito_harness_on()


def active(state: State, line_user_id: str) -> bool:
    return bool(state.user(line_user_id).get(KEY))


def _load(state: State, line_user_id: str) -> H.State | None:
    s = state.user(line_user_id).get(KEY)
    if not s:
        return None
    d = s.get("harness") or {}
    if d.get("version", "v3") != H.VERSION:  # PRD §8-8。v3 の状態は本番に出していないので、閉じて始め直す
        _drop(state, line_user_id)
        return None
    return H.State.from_dict(d)


def _save(state: State, line_user_id: str, h: H.State, track: str | None = None) -> None:
    """ハーネスの状態を書く。track は始めたときの雇用区分(始めるときだけ渡す。後は持ち越す)。"""
    def apply(u):
        u[KEY] = {"harness": h.to_dict(), "track": track if track is not None else (u.get(KEY) or {}).get("track")}
    state.update(line_user_id, apply)


def _track_left(state: State, line_user_id: str) -> bool:
    """始めた後に、雇用区分がアルバイト・パートから外れた(画面やトークの表示のボタンで取り消した・切り替えた)。"""
    u = state.user(line_user_id)
    now = logic.search_track(u)
    return now != BAITO_TRACK and now != (u.get(KEY) or {}).get("track")


def _drop(state: State, line_user_id: str) -> None:
    state.update(line_user_id, lambda u: u.pop(KEY, None))


def _question(v: dict) -> dict:
    """トークの AI と画面に渡す質問(id・質問文・選択肢)。"""
    return {"id": v["qid"], "text": v.get("text", ""), "choices": list(v.get("choices", []))}


def start(state: State, line_user_id: str) -> dict:
    """相談を始める(進めている相談があれば、今聞いている質問を返して続ける)。"""
    h = _load(state, line_user_id)
    started = h is None
    if started and not enabled():
        return {"started": False, "active": False, "error": f"アルバイト・パートの相談のハーネスは使っていません({FLAG})。"
                "get_counseling_playbook の playbook で相談する"}
    if started:
        h = H.new_state()
    v = H.resume(h, H.BANK) if not started else H.start(h, H.BANK)
    _save(state, line_user_id, h, logic.search_track(state.user(line_user_id)) if started else None)
    # 結果の後と途中でやめた後はトークの AI が続けるので、保存先と提案の決まり(playbook)もここで渡す
    pb = logic.counseling_playbook(BAITO_TRACK, "chat")
    return {"started": started, "active": True, "question": _question(v), "note": START_NOTE,
            "rules": pb["rules"], "playbook": pb["playbook"]}


def end(state: State, line_user_id: str) -> dict:
    """相談を途中で閉じる(新しい会話を始めた・会話の外のイベントが来た)。本人が答えた条件(high)だけを保存する。"""
    h = _load(state, line_user_id)
    if h is None:
        return {"ended": False}
    return {"ended": True, "saved": _finish(state, line_user_id, h, partial=True)}


def _exits(h: H.State, text: str) -> bool:
    """やめる合図か。中学生以下で止めた後は「求人を見せて」で相談の外へ出さない(トークの AI が求人を出さないように)。"""
    if h.current and text in h.current.get("choices", []):
        return False
    return text in EXIT_WORDS or (h.phase not in ("review", "result") and not h.blocked
                                  and bool(EXIT_REQUEST.search(text)) and not EXIT_HOLD.search(text))


class NeedLLM(BaseException):  # ハーネスの except Exception を通り抜けて turn() の外へ出す
    """turn() がまだ受け取っていないモデルの出力を求めた(call にプロンプト)。"""

    def __init__(self, call: dict):
        super().__init__(call["stage"])
        self.call = call


class ReplayMismatch(BaseException):  # 同じく turn() の外へ出す(ハーネスに失敗 1 回と数えさせない)
    """受け取った出力が、やり直した turn() の求めと合わない(呼び直しの間に状態が変わった)。"""


class ReplayLLM:
    """ハーネスの llm(extract / generate)。受け取った出力を順に返し、尽きたら NeedLLM で止める。
    出力の 1 つは {"stage", "output": モデルの JSON} か、呼び出しが失敗した印 {"stage", "error": 型の名前}
    (ハーネスは失敗 1 回として数える)。stage が求めと違えば ReplayMismatch。"""

    def __init__(self, outputs: list | None):
        self.outputs = list(outputs or [])
        self.used = 0

    def _next(self, stage: str, system: str, user_text: str):
        if self.used >= len(self.outputs):
            raise NeedLLM({"stage": stage, "system": system, "user_text": user_text, "n": self.used})
        out = self.outputs[self.used]
        self.used += 1
        if not isinstance(out, dict) or out.get("stage", stage) != stage:
            raise ReplayMismatch(f"{self.used}: {stage}")
        if "error" in out or "output" not in out:
            raise RuntimeError(str(out.get("error", "bad_output")))
        return out["output"]

    def extract(self, payload: dict):
        return self._next("extract", H.render_prompt("extract", payload), payload.get("user_text", ""))

    def generate(self, view: dict):
        stage = H.prompt_stage(view)
        return self._next(stage, H.render_prompt(stage, view), view.get("user_text", ""))


def turn(state: State, line_user_id: str, text: str, llm_outputs: list | None = None) -> dict:
    """本人の発言 1 つで相談を進める。返すもの:
    - {"active": False}: 相談をしていない(やめた合図なら ended="exit")。トークの AI がこのターンを受け持つ
    - status=llm: モデルの出力がまだ要る。call.system をシステムプロンプトに、本人の発言を user にしてモデルを呼び、
      それまでの出力を全部 llm_outputs に入れて呼び直す(状態はまだ変わっていない)
    - status=reply: reply をそのまま送り、choices をクイックリプライにする
    - status=result: 結果(reply)。中学生以下で止めた結果でなければ相談は閉じた(saved は保存した項目)"""
    h = _load(state, line_user_id)
    if h is None:
        return {"active": False}
    said = (text or "").strip()
    first = not llm_outputs
    left = first and _track_left(state, line_user_id)
    if left or (first and _exits(h, said)):
        return {"active": False, "ended": "track" if left else "exit", "saved": _finish(state, line_user_id, h, partial=True)}
    llm = ReplayLLM(llm_outputs)
    try:
        r = H.turn(h, said, H.BANK, llm)
    except NeedLLM as need:
        if need.call["n"] >= MAX_LLM_CALLS:
            raise RuntimeError("バイトの相談のモデルの呼び出しが、1 ターンの上限を超えました") from None
        return {"active": True, "status": "llm", "call": need.call}
    except ReplayMismatch as e:
        raise RuntimeError(f"バイトの相談のモデルの出力が、ハーネスの求めと合いません({e})") from None
    if llm.used != len(llm.outputs):
        raise RuntimeError("バイトの相談のモデルの出力が、ハーネスの求めより多くあります(呼び直しの間に状態が変わった)")
    out = {"active": True, "status": "result" if r["done"] else "reply", "phase": h.phase,
           "question": (h.current or {}).get("qid"), "reply": r["reply"], "counted": r["counted"],
           "choices": list(r["view"].get("choices", [])), "llm_calls": llm.used}
    if r["done"] and h.blocked:  # 中学生以下: 閉じずに、その後の発言もハーネスが受ける(年齢の訂正・やり直し)
        _save(state, line_user_id, h)
        return out | {"blocked": h.blocked, "choices": []}
    if r["done"]:
        return out | {"choices": list(RESULT_CHOICES), "saved": _finish(state, line_user_id, h)}
    _save(state, line_user_id, h)
    return out


def _finish(state: State, line_user_id: str, h: H.State, partial: bool = False) -> list[str]:
    """相談を閉じ、条件を本人の希望として保存する。保存した項目。避けたいこと(avoids)は、前に本人が話したものに足す
    (save_preferences は項目ごとに上書きするので、前の「接客は苦手」を NG で消さない)。字数の上限を超えるときは、
    ルールの NG と今回の NG を残し、前の値は入るところまで(ルールの NG を落とさない)。
    中学生以下で止めていたら、閉じた後も印(BLOCK_KEY)を残す。年齢の質問に「中学」と答えて聞き直している途中でやめたとき
    (H.jhs_unresolved。task 258)も同じ。15 歳以上と確かめていたら印を消す。"""
    _drop(state, line_user_id)
    age = h.slots.get("age_band") or {}
    if h.blocked or H.jhs_unresolved(h):
        state.update(line_user_id, lambda u: u.__setitem__(BLOCK_KEY, {"blocked": h.blocked or "under_15"}))
    elif age.get("confidence") == "high" and age.get("value") not in (None, "under_15", "declined"):
        state.update(line_user_id, lambda u: u.pop(BLOCK_KEY, None))
    prefs = result_preferences(h, partial)
    before = ((state.user(line_user_id).get("preferences") or {}).get("avoids") or {}).get("value")
    if before and "avoids" in prefs:
        items = list(dict.fromkeys([*_ng(h), *(x for x in re.split(r"[、,，]\s*", before) if x)]))
        keep = []
        for x in items:
            if len("、".join([*keep, x])) <= logic.PREFERENCE_VALUE_MAX:
                keep.append(x)
        prefs["avoids"] = "、".join(keep)
    return logic.save_preferences(state, line_user_id, prefs)["saved"] if prefs else []


def job_block(state: State, line_user_id: str | None) -> dict | None:
    """中学生以下で止めた人には、トークの AI が求人を探しても出さない(dip の recommend_jobs・relax_conditions が先に見る)。"""
    if not line_user_id or not state.user(line_user_id).get(BLOCK_KEY):
        return None
    return {"jobs": [], "total": 0, "blocked": "under_15", "notice": H.NOTICE["under_15"],
            "note": "中学生以下なので求人は出さない。notice を本人に伝える。年齢がちがうなら、アルバイト・パートの相談をやり直す"}


def _ng(h: H.State) -> list[str]:
    """本人の NG と、ルールの NG(未成年なら「深夜シフト」「重量物・危険な作業」)。"""
    return list(dict.fromkeys(h.user_ng + h.derived_ng))


def _label(h: H.State, slot: str) -> str | None:
    u = h.slots.get(slot)
    if not H.is_filled(u) or u["confidence"] != "high":
        return None
    return H.value_ja(slot, u["value"])


def result_preferences(h: H.State, partial: bool = False) -> dict[str, str]:
    """条件のうち、プロフィールの項目(dip_mcp.logic.PREFERENCE_KEYS)に入るもの。確かめた値(high)だけ
    (結果の時点で推測のまま残った HARD は保存しない)。NG は、未成年のルールの NG も避けたいことに入れる
    (トークの AI が後で求人を探すときに、深夜と重い作業を外すため)。partial(途中でやめた)でも同じ決まり。"""
    time = [x for x in (_label(h, s) for s in ("time_slots", "available_hours", "weekday_or_weekend")) if x]
    wage = h.slots.get("hourly_wage_min")
    out = {"time_band": "、".join(time), "days_per_week": _label(h, "days_per_week"),
           "purpose": _label(h, "purpose"), "avoids": "、".join(_ng(h)),
           "min_wage": str(wage["value"]) if wage and wage["confidence"] == "high" and type(wage["value"]) is int else None}
    return {k: v for k, v in out.items() if v}
