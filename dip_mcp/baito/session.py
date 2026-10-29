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

- v5(BAITO_HARNESS_VERSION=v5。dip_mcp/baito/v5.py。会話の層に載せ替えた版。設計メモ docs/design/conversation-layer.md §4.13、§4.15):
  始めた相談は始めた版のまま最後まで進める(状態の version で版を分ける)。v5 で新しく始めるときは、前の相談で保存した希望の項目
  (stated_preferences)と、アルバイト・パートに宛てた引き継ぎの記録(dip_mcp.convo.switch)を読んで、聞かずに入れる。
  「やめる」と「あとで」はハーネスが決まった文で閉じ(トークの AI を挟まない)、そこまでの分(high)を保存する。「あとで」は相談の状態を
  取っておき(PAUSED_KEY)、次に始めるとき(switch_in も)に続きから聞く。前の相談の保存から入れたまま(答え直していない)の値は、
  閉じるときに書き戻さない(相談の途中にプロフィール画面で直した値を戻さない)。
  「やっぱり正社員で探したい」はハーネスが入り口を移ると読み、ここが引き継ぎの記録を書いて閉じる(switch_to と direct。移る先が会話の層の版
  なら、ランタイムが同じターンで移る先の相談を始める)。ほかの入り口から移ってくるときは、ランタイムが switch_in で始める

状態は 1 人の dict の "baito"(進めている間だけ)。「すべて消す」は 1 人の dict ごと消すので、これも引き継ぎの記録も消える。
"""

from __future__ import annotations

import re

from dip_mcp import logic
from dip_mcp.baito import harness as H
from dip_mcp.baito import progress as P
from dip_mcp.baito import v5 as V5
from dip_mcp.convo import switch as SW
from dip_mcp.data import State, now

KEY = "baito"
# 「あとで」で閉じた相談の状態(v5。次に始めるときに続きから聞く。_pause)。「すべて消す」は 1 人の dict ごと消すので、これも消える
PAUSED_KEY = "baito_paused"
# 中学生以下で止めた印(相談を閉じた後も残す)。残っている間は求人を出さない(recommend_jobs・relax_conditions。job_block)。
# 後の相談で 15 歳以上と確かめたら消す。「すべて消す」は 1 人の dict ごと消すので、これも消える
BLOCK_KEY = "baito_block"
FLAG = logic.BAITO_HARNESS_FLAG  # PRD §13-3。v4 か v5 のときだけ新しい相談を始める。始めた相談は版を変えずに最後まで進める
HARNESSES = {H.VERSION: H, V5.VERSION: V5}  # 状態の version → ハーネス(v5 は v4 の決める段、まとめ、結果を使う)
LAYER_VERSIONS = (V5.VERSION,)  # 会話の層の版(LINE のスタンプを「[スタンプ]」の文にして受ける。設計メモ §4.12)
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
JOB_REQUEST = r"求人を?(見せて|見たい|探して|出して|紹介して|教えて)"
CAREER_REQUEST = r"正社員(で|の(仕事|求人)を?)\s*(探|さが|見)|やっぱり正社員|正社員(が|に)(いい|なりたい)"
EXIT_REQUEST = re.compile(JOB_REQUEST + "|" + CAREER_REQUEST)
# v5 は 2 つを分けて読む: 求人を見たい頼みは今と同じく相談の外へ(トークの AI が求人を出す)、正社員で探したい頼みは入り口を移す(ハーネス)
JOB_REQUEST_RE, CAREER_REQUEST_RE = re.compile(JOB_REQUEST), re.compile(CAREER_REQUEST)
# 頼みに見えても、後回しや打ち消しなら相談を続ける(「求人を見たいけど、先に条件を整理したい」「正社員じゃなくて」)
EXIT_HOLD = re.compile(r"けど|けれど|前に|先に|まだ|あとで|後で|じゃな|ではな|わけでは|なくて")
BAITO_TRACK = "アルバイト・パート"
START_NOTE = ("相談を始めた。この返信で、一緒に整理することを1文で伝え、question の text を言い換えずに1問として聞く"
              "(選択肢は LINE 側が question の choices にする)。次の質問からはハーネスが進める。")
# 入口のカード(question に card がある)から始めたとき。問いはカードに書いてあるので、AI は繰り返さない
QUICK_START_NOTE = ("相談を始めた。この返信は、一緒に探すことを伝える短い1文だけにする。question の text と choices は書かない"
                    "(問いと選択肢は、LINE 側が question の card とボタンで出す)。次からはハーネスが進める。")
# トークの AI が start に本人の発言(text)を渡し、v5 で始めるとき(t1。設計メモ §4.5、§4.13): ここでは始めず、ランタイムが同じターンで
# switch_in で始める(読む段でその発言を読むため。dip はモデルを呼ばない)
READ_NOTE = ("相談はまだ始まっていない。LINE 側が同じターンで、渡した発言を読んで相談を始め、この返信をその最初の返信に置き換える。"
             "返信は短い1文だけにする(問いと選択肢は書かない)。")
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
    mod = HARNESSES.get(d.get("version", "v3"))
    if mod is None:  # PRD §8-8。v3 の状態は本番に出していないので、閉じて始め直す(知らない版も同じ)
        _drop(state, line_user_id)
        return None
    return mod.State.from_dict(d)


def harness_of(h: H.State):
    """状態の版のハーネス(dip_mcp.baito.harness か dip_mcp.baito.v5)。"""
    return HARNESSES[h.version]


def on_layer(state: State, line_user_id: str) -> bool:
    """進めている相談が、会話の層の版(v5)か。LINE のランタイムは、このときだけスタンプを文にして渡す(line_runtime.server._line_kind)。"""
    d = (state.user(line_user_id).get(KEY) or {}).get("harness") or {}
    return d.get("version") in LAYER_VERSIONS


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


def start(state: State, line_user_id: str, quick: bool = True, text: str | None = None) -> dict:
    """相談を始める(進めている相談があれば、今聞いている質問を返して続ける)。新しい相談は入口のカードから
    (quick=False は PRD の icebreak から直に。dip のツールは渡さない。今の流れを検査するテストが使う)。
    text はトークの AI が渡す本人の発言(t1)。TALK_LAYER_VERSION=t1 で、新しく v5 で始めるときに text があれば、始めずに、トークの AI からの引き継ぎの記録を
    書いて switch_to を返す(ランタイムが同じターンで switch_in で始め、v5 がその発言を読む段で読む。§4.5)。v4 は text を使わない。"""
    h = _load(state, line_user_id)
    started = h is None
    if started and not enabled():
        return {"started": False, "active": False, "error": f"アルバイト・パートの相談のハーネスは使っていません({FLAG})。"
                "get_counseling_playbook の playbook で相談する"}
    said = (text or "").strip()
    if started and said and logic.talk_layer_on() and logic.baito_harness_version() in LAYER_VERSIONS:
        SW.write(state, line_user_id, SW.talk_record(V5.ENTRY.entry, said, now(line_user_id)))
        return {"started": False, "active": False, "switch_to": V5.ENTRY.entry, "direct": True, "note": READ_NOTE}
    paused = None
    if started:  # 中学生以下で止めた印が残っている人は、職種を出す前に年齢を聞く今の流れから(職種のおすすめも出さない)
        h, paused = _new(state, line_user_id, quick)
    mod = harness_of(h)
    v = mod.resume(h, H.BANK) if not started or paused else mod.start(h, H.BANK)
    _save(state, line_user_id, h, logic.search_track(state.user(line_user_id)) if started else None,
          ((paused or {}).get("progress") or P.fresh_mem(h)) if started else None)
    # 結果の後と途中でやめた後はトークの AI が続けるので、保存先と提案の決まり(playbook)もここで渡す
    pb = logic.counseling_playbook(BAITO_TRACK, "chat")
    q = _question(v)
    return {"started": started, "active": True, "question": q, "note": QUICK_START_NOTE if q.get("card") else START_NOTE,
            "rules": pb["rules"], "playbook": pb["playbook"]}


def _new(state: State, line_user_id: str, quick: bool = True) -> tuple[H.State, dict | None]:
    """新しく始める相談の状態(版は今のフラグ)と、「あとで」で取っておいた記録(続きから始めるとき)。v5 は、前の相談で保存した
    希望の項目と、アルバイト・パートに宛てた引き継ぎの記録を読んで入れる(記録は読んだら消す)。「あとで」で取っておいた v5 の相談が
    あれば、今のフラグによらず、それを続きから始める(始めた相談は始めた版のまま)。"""
    quick = quick and not state.user(line_user_id).get(BLOCK_KEY)
    back = _paused(state, line_user_id)
    if back is not None:
        _unpause(state, line_user_id)
        h, paused = back
    elif logic.baito_harness_version() != V5.VERSION:
        return H.new_state(quick=quick), None
    else:
        h, paused = V5.new_state(quick=quick), None
        V5.prefill(h, logic.stated_preferences(state.user(line_user_id)))
    rec = SW.peek(state, line_user_id, V5.ENTRY.entry, now(line_user_id))
    if rec is not None:
        SW.clear(state, line_user_id)
        V5.take_handoff(h, rec)
    return h, paused


def end(state: State, line_user_id: str) -> dict:
    """相談を途中で閉じる(新しい会話を始めた・会話の外のイベントが来た)。本人が答えた条件(high)だけを保存する。
    引き継ぎの記録(入り口を移したときの 1 回だけの記録。設計メモ §4.5)も、ここで消す(ランタイムは新しい会話と会話の外の
    イベントで、どの入り口の end も呼ぶ)。"""
    SW.clear(state, line_user_id)
    h = _load(state, line_user_id)
    if h is None:
        return {"ended": False}
    return {"ended": True, "saved": _finish(state, line_user_id, h, partial=True)}


def _exits(h: H.State, text: str) -> bool:
    """やめる合図か。中学生以下で止めた後は「求人を見せて」で相談の外へ出さない(トークの AI が求人を出さないように)。
    希死念慮・自傷の言葉があれば、やめる頼みを含んでいてもハーネスに渡す(「もう消えたい。求人見せて」に窓口を案内する)。"""
    if H.crisis_hit(text) or (h.current and text in h.current.get("choices", [])):
        return False
    v5 = h.version == V5.VERSION  # v5 は、やめる言葉と正社員への頼みをハーネスが受ける(決まった文で閉じる、入り口を移す)
    if h.phase == H.NEAR_JOBS:  # 求人を出した後のボタンでない発言(カルーセルの「詳しく見る」など)は、トークの AI が受ける。
        # ただし、年齢に触れた発言(says_age)、闇バイトの言葉、窓口の案内の後の「続ける」は、ハーネスが受ける
        return not (H.says_age(text) or H.DANGER_RE.search(H.nfkc(text))
                    or ("crisis_shown" in h.flags and H.wants_resume(text))
                    or (v5 and (text in EXIT_WORDS or V5.asked_to_switch(h, text) is not None)))
    if v5:  # 求人を見たい頼みだけは v4 と同じく相談の外へ
        return h.phase not in ("review", "result") and not h.blocked and bool(JOB_REQUEST_RE.search(text)) \
            and not EXIT_HOLD.search(text)
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
        """view に model_text(正社員の c2 が伏せた本人の発言)と model_history(伏せた直前のやりとり)があれば、呼び出しに入れる。
        ランタイムは、それを本人の発言と直前のやりとりとしてモデルに渡す。"""
        stage = self.prompt_stage(view)
        extra = {k: view[k] for k in ("model_text", "model_history") if k in view} or None
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
    mod = harness_of(h)
    llm = ReplayLLM(llm_outputs, mod.render_prompt, mod.prompt_stage)
    try:
        r = mod.turn(h, said, H.BANK, llm, search=lambda cond: near_search(state, line_user_id, cond))
    except NeedLLM as need:
        if need.call["n"] >= MAX_LLM_CALLS:
            raise RuntimeError("バイトの相談のモデルの呼び出しが、1 ターンの上限を超えました") from None
        return {"active": True, "status": "llm", "call": need.call}
    except ReplayMismatch as e:
        raise RuntimeError(f"バイトの相談のモデルの出力が、ハーネスの求めと合いません({e})") from None
    if llm.used != len(llm.outputs):
        raise RuntimeError("バイトの相談のモデルの出力が、ハーネスの求めより多くあります(呼び直しの間に状態が変わった)")
    if r.get("switch"):  # v5: 別の入り口へ移る
        return _switch(state, line_user_id, h, r["switch"], llm.used)
    progress, mem = P.update(h, (state.user(line_user_id).get(KEY) or {}).get("progress"), prev, r)
    out = _reply(h, r, llm.used, progress)
    if r.get("closed"):  # v5 の「あとで」「やめる」: 決まった文で閉じ、そこまでの分(high)を保存する。「あとで」は続きを取っておく
        pause = r["closed"] == "later" and h.result is None and not h.blocked and not H.jhs_unresolved(h)
        saved = _pause(state, line_user_id, h) if pause else _finish(state, line_user_id, h, partial=True)
        return out | {"status": "result", "closed": r["closed"], "choices": list(RESULT_CHOICES), "saved": saved}
    if r["done"] and h.blocked:  # 中学生以下: 閉じずに、その後の発言もハーネスが受ける(年齢の訂正・やり直し)
        _save(state, line_user_id, h, progress=mem)
        return out | {"blocked": h.blocked, "choices": []}
    if r["done"]:
        return out | {"choices": list(RESULT_CHOICES), "saved": _finish(state, line_user_id, h)}
    _save(state, line_user_id, h, progress=mem)
    return out


def _reply(h: H.State, r: dict, used: int, progress: dict | None) -> dict:
    out = {"active": True, "status": "result" if r["done"] else "reply", "phase": h.phase,
           "question": (h.current or {}).get("qid"), "reply": r["reply"], "counted": r["counted"],
           "choices": list(r["view"].get("choices", [])), "llm_calls": used, "progress": progress}
    if r["view"].get("quick"):
        out["card"] = r["view"]["quick"]
    if "jobs" in r["view"]:
        out["jobs"] = r["view"]["jobs"]
    if r.get("parts"):  # v5: 返事の部品の並び([[種類, 文]]。dip_mcp.convo.reply。場面の一覧の判定に使う)
        out["parts"] = r["parts"]
    return out


def _pause(state: State, line_user_id: str, h: H.State) -> list[str]:
    """「あとで」(v5。設計メモ §4.6): そこまでの分(high)を保存して閉じ、相談の状態を PAUSED_KEY に取っておく(次に始めるとき、
    聞いた問いを聞き直さずに続きから聞く。_new)。保存した欄と本人の NG は前の相談で保存した値にし(V5.mark_saved)、閉じたときの
    希望の項目の文(kept。避けたいことも)を覚える(続きのときに、その間にプロフィール画面で直した・消した項目を読み直すため)。
    続きをもう一度「あとで」で閉じるときは、先に続きの途中で直した・消した項目を読み直す(V5.refresh_saved。消した NG を相談に残さない)。"""
    old = state.user(line_user_id).get(KEY) or {}
    if getattr(h, "kept", None) is not None:
        V5.refresh_saved(h, logic.stated_preferences(state.user(line_user_id)), h.kept)
    saved = _finish(state, line_user_id, h, partial=True)
    if (h.current or {}).get("mode") == "pick":  # 聞き直しの答えは _finish が入れた。続きは次の問いから
        h.current = None
    h.closed = None
    prefs = logic.stated_preferences(state.user(line_user_id))
    V5.mark_saved(h, prefs)
    kept = h.kept = V5.kept_texts(prefs)
    paused = {"harness": h.to_dict(), "track": old.get("track"), "progress": old.get("progress"), "kept": kept}
    state.update(line_user_id, lambda u: u.__setitem__(PAUSED_KEY, paused))
    return saved


def _paused(state: State, line_user_id: str) -> tuple[H.State, dict] | None:
    """「あとで」で取っておいた相談(v5)の状態と、取っておいた記録。閉じた後にプロフィール画面で直した項目は読み直す。状態は変えない。"""
    p = state.user(line_user_id).get(PAUSED_KEY) or {}
    d = p.get("harness") or {}
    if d.get("version") != V5.VERSION:
        return None
    h = V5.State.from_dict(d)
    V5.refresh_saved(h, logic.stated_preferences(state.user(line_user_id)), p.get("kept") or {})
    return h, p


def _unpause(state: State, line_user_id: str) -> None:
    state.update(line_user_id, lambda u: u.pop(PAUSED_KEY, None))


def _switch(state: State, line_user_id: str, h: H.State, sw: dict, used: int) -> dict:
    """別の入り口へ移る(v5。設計メモ §4.5、§4.15): 引き継ぎの記録を書き、そこまでの分(high)を保存して閉じる。返すもの:
    {"active": False, "ended": "switch", "switch_to", "direct"}。direct は、移る先が会話の層の版か(ランタイムが同じターンで
    移る先の相談を始める。区分は移る先が書く)。そうでなければ(c1)、今と同じくトークの AI がこのターンを受ける。"""
    prior = getattr(h, "came_from", None)
    rec = SW.record(V5.ENTRY.entry, sw["to"], V5.handoff_slots(h), sw["acts"], sw["said"], now(line_user_id), prior)
    saved = _finish(state, line_user_id, h, partial=True)
    SW.write(state, line_user_id, rec)
    return {"active": False, "ended": "switch", "switch_to": sw["to"], "direct": logic.layer_on(sw["to"]), "saved": saved,
            "llm_calls": used}


def switch_in(state: State, line_user_id: str, text: str, llm_outputs: list | None = None) -> dict:
    """ほかの入り口から、同じターンで移ってきた相談を始める(ランタイムだけが呼ぶ。baito_turn の switch_in。設計メモ §4.5)。
    会話の層の版(v5)で新しく始めるときだけ。前の相談で保存した項目と、アルバイト・パートに宛てた引き継ぎの記録の値を入れ、最初の問い
    (入口のカード)を移ったことの相づちと一緒に返す(返す形は turn の status=reply か status=llm と同じ。switched に移る前の入り口)。
    区分はアルバイト・パートにし、切り替えの記録を track_switch に返す。記録を消して区分を書くのは、最後まで進んだ回だけ
    (呼び直しの間は変えない)。進めている相談があるか、層の版でなければ {"active": False}(ランタイムはトークの AI に渡す)。"""
    if _load(state, line_user_id) is not None or logic.baito_harness_version() not in LAYER_VERSIONS:
        return {"active": False}
    back = _paused(state, line_user_id)  # 「あとで」で取っておいた相談があれば、その続きに移る
    h, paused = back if back is not None else (V5.new_state(quick=not state.user(line_user_id).get(BLOCK_KEY)), None)
    if paused is None:
        V5.prefill(h, logic.stated_preferences(state.user(line_user_id)))
    rec = SW.peek(state, line_user_id, V5.ENTRY.entry, now(line_user_id))
    said = (text or "").strip()
    llm = ReplayLLM(llm_outputs, V5.render_prompt, V5.prompt_stage)
    prev = P.before(h)
    try:
        opening, stop = (rec or {}).get("said") or said, None
        if SW.unread(rec):  # トークの AI が始めた相談(t1): 始めた発言を読む段で読み、その値を当ててから最初の問いを選ぶ
            acts, opening, stop = V5.read_opening(h, H.BANK, llm, said)
            rec = dict(rec, acts=acts, read=False)
        V5.take_handoff(h, rec)
        r = V5.opening_guard(h, H.BANK, llm, stop) if stop else V5.enter(h, H.BANK, llm, opening)
    except NeedLLM as need:
        if need.call["n"] >= MAX_LLM_CALLS:
            raise RuntimeError("バイトの相談のモデルの呼び出しが、1 ターンの上限を超えました") from None
        return {"active": True, "status": "llm", "call": need.call}
    except ReplayMismatch as e:
        raise RuntimeError(f"バイトの相談のモデルの出力が、ハーネスの求めと合いません({e})") from None
    if llm.used != len(llm.outputs):
        raise RuntimeError("バイトの相談のモデルの出力が、ハーネスの求めより多くあります(呼び直しの間に状態が変わった)")
    SW.clear(state, line_user_id)
    if paused is not None:
        _unpause(state, line_user_id)
    moved = logic.set_search_track(state, line_user_id, BAITO_TRACK, evidence=said)
    progress, mem = P.update(h, (paused or {}).get("progress"), prev, r)
    _save(state, line_user_id, h, BAITO_TRACK, mem)
    out = _reply(h, r, llm.used, progress) | {"switched": {"from": (rec or {}).get("from")}}
    if moved.get("changed"):
        out["track_switch"] = {k: moved["recorded"][k] for k in ("id", "from", "track", "undo")}
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
        # 「あとで」で保存した NG(saved_ng)は、今の文(before)に残っていれば入る。外されていれば戻さない
        items = list(dict.fromkeys([*(n for n in _ng(h) if n not in getattr(h, "saved_ng", ())), *V5.ng_items(before)]))
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


def _saved(h: H.State, slot: str) -> bool:
    """前の相談で保存した値を入れたまま(v5 の prefill。答え直していない)の欄か。"""
    return (h.slots.get(slot) or {}).get("from") == V5.SAVED


def result_preferences(h: H.State, partial: bool = False) -> dict[str, str]:
    """条件のうち、プロフィールの項目(dip_mcp.logic.PREFERENCE_KEYS)に入るもの。確かめた値(high)だけ
    (結果の時点で推測のまま残った HARD は保存しない)。NG は、未成年のルールの NG も避けたいことに入れる
    (トークの AI が後で求人を探すときに、深夜と重い作業を外すため)。partial(途中でやめた)でも同じ決まり。
    エリアは「近くで探す」でボタンで選んだもの(task 266)。
    v5 で前の相談の保存から入れたまま(from が saved)の欄とエリア、「あとで」で閉じたときに保存した本人の NG(saved_ng)は
    書き戻さない(正社員の session._finish と同じ。相談の途中にプロフィール画面で直した・消した値を戻さない。jyoka/tasks#374)。
    時間帯は 3 つの欄から作るので、どれかを答えたときだけ、保存から入れた欄も含めて書く。"""
    times = ("time_slots", "available_hours", "weekday_or_weekend")
    time = [x for x in (_label(h, s) for s in times) if x] if any(
        _label(h, s) and not _saved(h, s) for s in times) else []
    wage = h.slots.get("hourly_wage_min")
    area = h.near.get("area")
    out = {"time_band": "、".join(time), "days_per_week": None if _saved(h, "days_per_week") else _label(h, "days_per_week"),
           # 「近くで探す」のエリアのボタン(どこでもは入れない。task 266)
           "area": None if area and area == getattr(h, "saved_area", None) else H.NEAR_AREAS.get(area),
           "purpose": None if _saved(h, "purpose") else _label(h, "purpose"),
           "avoids": "、".join(n for n in _ng(h) if n not in getattr(h, "saved_ng", ())),
           "min_wage": str(wage["value"]) if wage and wage["confidence"] == "high" and type(wage["value"]) is int
           and not _saved(h, "hourly_wage_min") else None}
    return {k: v for k, v in out.items() if v}
