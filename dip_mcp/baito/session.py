"""バイト探しの相棒の相談を、dip のユーザー状態に保存して1ターンずつ進める(dip のツールの中身。dip は LLM を呼ばない)。

- 始める: トークの AI が、アルバイト・パートの相談に入ったターンに start_baito_consultation を呼ぶ
  (BAITO_HARNESS_VERSION=v4 のときだけ。PRD §13-3 の機能フラグ。既定は使わない = 本番は切り替えない)。
  新しい相談は入口のカード(quick_pick。設計メモ docs/design/baito-quick-start.md §4、task 265)から始める。
  その AI は 1 文で声をかけるだけで、問いと選択肢はカード(question の card)とボタンで出る。
  進めている相談(今の State を持つもの)は、今聞いている質問から続ける
- 近くで探す(task 266。設計メモ §6): ハーネスが年齢・エリア・時間帯を聞き、条件(未成年なら minor=True)を決める。
  ここ(near_search)がその条件で dip の recommend(アルバイト・パートの求人)を呼び、0 件なら relax の 1 つも渡す。
  求人は turn の jobs(LINE 側が求人のカルーセルにする)。求人を出した後の、ボタン([もっと絞る] [ちがう] など)でない発言
  (カルーセルの「詳しく見る」「保存」も)は、相談を閉じてトークの AI に渡す(年齢の訂正・闇バイトの言葉・窓口の案内の後の
  「続ける」は、ハーネスが受ける)
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
from dip_mcp.baito import progress as P
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
# 入口のカード(question に card がある)から始めたとき。問いはカードに書いてあるので、AI は繰り返さない
QUICK_START_NOTE = ("相談を始めた。この返信は、一緒に探すことを伝える短い1文だけにする。question の text と choices は書かない"
                    "(問いと選択肢は、LINE 側が question の card とボタンで出す)。次からはハーネスが進める。")
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


def _save(state: State, line_user_id: str, h: H.State, track: str | None = None, progress: dict | None = None) -> None:
    """ハーネスの状態を書く。track は始めたときの雇用区分(始めるときだけ渡す。後は持ち越す)。
    progress は進み具合を数えるために覚えること(dip_mcp.baito.progress の mem。渡さなければ持ち越す)。"""
    def apply(u):
        old = u.get(KEY) or {}
        u[KEY] = {"harness": h.to_dict(), "track": track if track is not None else old.get("track"),
                  "progress": progress if progress is not None else old.get("progress")}
    state.update(line_user_id, apply)


def _track_left(state: State, line_user_id: str) -> bool:
    """始めた後に、雇用区分がアルバイト・パートから外れた(画面やトークの表示のボタンで取り消した・切り替えた)。"""
    u = state.user(line_user_id)
    now = logic.search_track(u)
    return now != BAITO_TRACK and now != (u.get(KEY) or {}).get("track")


def _drop(state: State, line_user_id: str) -> None:
    state.update(line_user_id, lambda u: u.pop(KEY, None))


def _question(v: dict) -> dict:
    """トークの AI と画面に渡す質問(id・質問文・選択肢)。入口のカードと職種の結果なら、その中身(card。ランタイムが描く)も。"""
    return {"id": v["qid"], "text": v.get("text", ""), "choices": list(v.get("choices", []))} | (
        {"card": v["quick"]} if v.get("quick") else {})


def start(state: State, line_user_id: str, quick: bool = True) -> dict:
    """相談を始める(進めている相談があれば、今聞いている質問を返して続ける)。新しい相談は入口のカードから
    (quick=False は PRD の icebreak から直に。dip のツールは渡さない。今の流れを検査するテストが使う)。"""
    h = _load(state, line_user_id)
    started = h is None
    if started and not enabled():
        return {"started": False, "active": False, "error": f"アルバイト・パートの相談のハーネスは使っていません({FLAG})。"
                "get_counseling_playbook の playbook で相談する"}
    if started:  # 中学生以下で止めた印が残っている人は、職種を出す前に年齢を聞く今の流れから(職種のおすすめも出さない)
        h = H.new_state(quick=quick and not state.user(line_user_id).get(BLOCK_KEY))
    v = H.resume(h, H.BANK) if not started else H.start(h, H.BANK)
    _save(state, line_user_id, h, logic.search_track(state.user(line_user_id)) if started else None,
          P.fresh_mem(h) if started else None)
    # 結果の後と途中でやめた後はトークの AI が続けるので、保存先と提案の決まり(playbook)もここで渡す
    pb = logic.counseling_playbook(BAITO_TRACK, "chat")
    q = _question(v)
    return {"started": started, "active": True, "question": q, "note": QUICK_START_NOTE if q.get("card") else START_NOTE,
            "rules": pb["rules"], "playbook": pb["playbook"]}


def end(state: State, line_user_id: str) -> dict:
    """相談を途中で閉じる(新しい会話を始めた・会話の外のイベントが来た)。本人が答えた条件(high)だけを保存する。"""
    h = _load(state, line_user_id)
    if h is None:
        return {"ended": False}
    return {"ended": True, "saved": _finish(state, line_user_id, h, partial=True)}


def _exits(h: H.State, text: str) -> bool:
    """やめる合図か。中学生以下で止めた後は「求人を見せて」で相談の外へ出さない(トークの AI が求人を出さないように)。
    希死念慮・自傷の言葉があれば、やめる頼みを含んでいてもハーネスに渡す(「もう消えたい。求人見せて」に窓口を案内する)。"""
    if H.crisis_hit(text) or (h.current and text in h.current.get("choices", [])):
        return False
    if h.phase == H.NEAR_JOBS:  # 求人を出した後のボタンでない発言(カルーセルの「詳しく見る」など)は、トークの AI が受ける。
        # ただし、年齢に触れた発言(says_age)、闇バイトの言葉、窓口の案内の後の「続ける」は、ハーネスが受ける
        return not (H.says_age(text) or H.DANGER_RE.search(H.nfkc(text))
                    or ("crisis_shown" in h.flags and H.wants_resume(text)))
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
    """ハーネスの llm(extract / read / generate)。受け取った出力を順に返し、尽きたら NeedLLM で止める。
    出力の 1 つは {"stage", "output": モデルの JSON} か、呼び出しが失敗した印 {"stage", "error": 型の名前}
    (ハーネスは失敗 1 回として数える)。stage が求めと違えば ReplayMismatch。
    render_prompt と prompt_stage はハーネスのもの(既定はアルバイト・パート。正社員の dip_mcp.career.session も使う)。"""

    def __init__(self, outputs: list | None, render_prompt=None, prompt_stage=None):
        self.outputs = list(outputs or [])
        self.used = 0
        self.render_prompt = render_prompt or H.render_prompt
        self.prompt_stage = prompt_stage or H.prompt_stage

    def _next(self, stage: str, system: str, user_text: str, extra: dict | None = None):
        if self.used >= len(self.outputs):
            raise NeedLLM({"stage": stage, "system": system, "user_text": user_text, "n": self.used, **(extra or {})})
        out = self.outputs[self.used]
        self.used += 1
        if not isinstance(out, dict) or out.get("stage", stage) != stage:
            raise ReplayMismatch(f"{self.used}: {stage}")
        if "error" in out or "output" not in out:
            raise RuntimeError(str(out.get("error", "bad_output")))
        return out["output"]

    def extract(self, payload: dict):
        return self._next("extract", self.render_prompt("extract", payload), payload.get("user_text", ""))

    def read(self, payload: dict):
        """会話の層の読む段(正社員の c2。dip_mcp/career/c2.py)。"""
        return self._next("read", self.render_prompt("read", payload), payload.get("user_text", ""))

    def generate(self, view: dict):
        """view に model_text(正社員の c2 が伏せた本人の発言)があれば、呼び出しに入れる。ランタイムは、それを本人の発言としてモデルに渡す。"""
        stage = self.prompt_stage(view)
        extra = {"model_text": view["model_text"]} if "model_text" in view else None
        return self._next(stage, self.render_prompt(stage, view), view.get("user_text", ""), extra)


def turn(state: State, line_user_id: str, text: str, llm_outputs: list | None = None) -> dict:
    """本人の発言 1 つで相談を進める。返すもの:
    - {"active": False}: 相談をしていない(やめた合図なら ended="exit")。トークの AI がこのターンを受け持つ
    - status=llm: モデルの出力がまだ要る。call.system をシステムプロンプトに、本人の発言を user にしてモデルを呼び、
      それまでの出力を全部 llm_outputs に入れて呼び直す(状態はまだ変わっていない)
    - status=reply: reply をそのまま送り、choices をクイックリプライにする
    - status=result: 結果(reply)。中学生以下で止めた結果でなければ相談は閉じた(saved は保存した項目)
    reply と result には progress(プロフィールの進み具合。dip_mcp.baito.progress.update。出さないターンは None)が付く。
    LINE 側は、これから帯・プロフィールカード・完成の Flex を 0〜1 つ選ぶ(line_runtime.progress_ui)。
    入口のカードと職種の結果のターン(task 265)には card(ハーネスの view の quick)が付く。LINE 側がカードに描く。
    「近くで探す」の求人のターン(task 266)には jobs(dip の求人カード。near_search)が付く。LINE 側が求人のカルーセルにする"""
    h = _load(state, line_user_id)
    if h is None:
        return {"active": False}
    said = (text or "").strip()
    first = not llm_outputs
    left = first and _track_left(state, line_user_id)
    if left or (first and _exits(h, said)):
        return {"active": False, "ended": "track" if left else "exit", "saved": _finish(state, line_user_id, h, partial=True)}
    prev = P.before(h)
    llm = ReplayLLM(llm_outputs)
    try:
        r = H.turn(h, said, H.BANK, llm, search=lambda cond: near_search(state, line_user_id, cond))
    except NeedLLM as need:
        if need.call["n"] >= MAX_LLM_CALLS:
            raise RuntimeError("バイトの相談のモデルの呼び出しが、1 ターンの上限を超えました") from None
        return {"active": True, "status": "llm", "call": need.call}
    except ReplayMismatch as e:
        raise RuntimeError(f"バイトの相談のモデルの出力が、ハーネスの求めと合いません({e})") from None
    if llm.used != len(llm.outputs):
        raise RuntimeError("バイトの相談のモデルの出力が、ハーネスの求めより多くあります(呼び直しの間に状態が変わった)")
    progress, mem = P.update(h, (state.user(line_user_id).get(KEY) or {}).get("progress"), prev, r)
    out = {"active": True, "status": "result" if r["done"] else "reply", "phase": h.phase,
           "question": (h.current or {}).get("qid"), "reply": r["reply"], "counted": r["counted"],
           "choices": list(r["view"].get("choices", [])), "llm_calls": llm.used, "progress": progress}
    if r["view"].get("quick"):
        out["card"] = r["view"]["quick"]
    if "jobs" in r["view"]:
        out["jobs"] = r["view"]["jobs"]
    if r["done"] and h.blocked:  # 中学生以下: 閉じずに、その後の発言もハーネスが受ける(年齢の訂正・やり直し)
        _save(state, line_user_id, h, progress=mem)
        return out | {"blocked": h.blocked, "choices": []}
    if r["done"]:
        return out | {"choices": list(RESULT_CHOICES), "saved": _finish(state, line_user_id, h)}
    _save(state, line_user_id, h, progress=mem)
    return out


def near_search(state: State, line_user_id: str, cond: dict) -> dict:
    """「近くで探す」の求人(設計メモ §6.4)。ハーネスが決めた条件(cond。H.near_search。未成年なら minor=True)で、
    アルバイト・パートの求人を dip の recommend で探す。0 件なら relax の、いちばん件数の増える 1 つ(options の先頭)も返す
    (relax は minor を外す候補にしない)。中学生以下で止めた印があれば探さない(job_block と同じ)。"""
    if logic.MINOR_ARG not in cond:  # 未成年かどうかを渡し忘れた条件では探さない(ハーネスは必ず付ける)
        raise ValueError(f"near_search: {logic.MINOR_ARG} がありません")
    if job_block(state, line_user_id):
        return {"total": 0, "jobs": [], "relax": None}
    args = dict(cond, employment_type=BAITO_TRACK)
    found = logic.recommend(state, line_user_id, **args)
    if "error" in found:
        raise ValueError(f"near_search: {found['error']}")
    relax = None
    if not found["total"]:
        relax = next(iter(logic.relax(state, line_user_id, **args).get("options") or []), None)
    return {"total": found["total"], "jobs": found["jobs"], "relax": relax}


def _finish(state: State, line_user_id: str, h: H.State, partial: bool = False) -> list[str]:
    """相談を閉じ、条件を本人の希望として保存する。保存した項目。避けたいこと(avoids)は、前に本人が話したものに足す
    (save_preferences は項目ごとに上書きするので、前の「接客は苦手」を NG で消さない)。字数の上限を超えるときは、
    ルールの NG と今回の NG を残し、前の値は入るところまで(ルールの NG を落とさない)。
    中学生以下で止めていたら、閉じた後も印(BLOCK_KEY)を残す。年齢の質問に「中学」と答えて聞き直している途中でやめたとき
    (H.jhs_unresolved。task 258)も同じ。15 歳以上と確かめていたら印を消す。"""
    _drop(state, line_user_id)
    H.settle_pick(h)  # 境目の聞き直しの答えを待っていれば、最初の答えの値を入れる(task 268)
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
    (トークの AI が後で求人を探すときに、深夜と重い作業を外すため)。partial(途中でやめた)でも同じ決まり。
    エリアは「近くで探す」でボタンで選んだもの(task 266)。"""
    time = [x for x in (_label(h, s) for s in ("time_slots", "available_hours", "weekday_or_weekend")) if x]
    wage = h.slots.get("hourly_wage_min")
    out = {"time_band": "、".join(time), "days_per_week": _label(h, "days_per_week"),
           "area": H.NEAR_AREAS.get(h.near.get("area")),  # 「近くで探す」のエリアのボタン(どこでもは入れない。task 266)
           "purpose": _label(h, "purpose"), "avoids": "、".join(_ng(h)),
           "min_wage": str(wage["value"]) if wage and wage["confidence"] == "high" and type(wage["value"]) is int else None}
    return {k: v for k, v in out.items() if v}
