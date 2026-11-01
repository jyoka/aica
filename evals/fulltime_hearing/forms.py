"""比べる形(設計メモ §6.5)と、本人役を確かめる台本の AI(§6.6 の 1)。

形は 1 つの会話を受け持つ。start で最初の返信、send で本人の返信(ボタンならラベル)を受けて次の返信を返す。
会話の終わりに、取れた値(captured。judge の docstring の形。None なら会話の記録から取り出す)、保存した文(数字を作った
の関門に使う)、wasted(もう high で埋まっている slot を聞いた回数。数えられない形は None)を返す。

- A: 今の正社員の相談(LINE 側のランタイムをそのまま。状態を持たないので captured は None)
- script-good / script-bad: 台本の良い AI と悪い AI(実 API を使わない。本人役とジャッジだけが LLM)
- B、C、D、D-noband: ハーネス(dip_mcp/career)を実 LLM(トークと同じ line_runtime.career_chat のモデル)でつないだもの
  (FormHarness。5/5 のタスク)。D-noband は帯を切った形 D(CAREER_UI_BAND=off)。モックでの回帰は evals/career_sim.py(費用 0)。
- D-c2: 形 D を会話の層の版(CAREER_HARNESS_VERSION=c2。dip_mcp/career/c2.py)で回すもの(設計メモ conversation-layer.md §5 の
  9 枚目。task 362)。比べるときは --form D,D-c2(どちらも本番の形 D)
"""

from __future__ import annotations

import contextlib
import re

from evals.fulltime_hearing.actor import AIReply, Clock, fact_text
from evals.fulltime_hearing.personas import COMMON_HARD, SEGMENT_HARD, label

PLANNED: dict[str, str] = {}  # これから足す形(名前 -> 足すタスク)。今は無い
TARGET_SECONDS = {True: 180.0, False: 300.0}  # 目標時間(履歴書あり / なし。設計メモ §3.3)
REVIEW_RESERVE = 90.0  # 質問を止める時刻 = 目標時間 - これ(まとめ 2 回分の 70 秒とゆとり 20 秒)
A_USER = "sim-new"  # 形 A の本人(未連携の新規。会話ごとに dip の状態を戻す)
START_SAID = "正社員の仕事を探したい"  # 形 B〜D の会話の頭に置く、相談を始めた本人の発言(生成に渡す履歴だけ。本人役は打たない)
DECLINES = {"今はいい", "今はいいです", "いまはいい", "いまはいいです"}  # 形 A の断り(common_chat.md の「今はいい」)


def missing() -> dict:
    return {"status": "missing", "text": "", "number": None, "range": None, "unit": None}


# ----------------------------- 形 A: 今の正社員の相談 -----------------------------


def _flex_texts(node) -> list[str]:
    """Flex の中で読む字(text の部品と、ボタンのラベル)。"""
    if isinstance(node, list):
        return [t for x in node for t in _flex_texts(x)]
    if not isinstance(node, dict):
        return []
    if node.get("type") == "text":
        return [node.get("text", "")]
    if node.get("type") == "button":
        return [node.get("action", {}).get("label", "")]
    return [t for k, v in node.items() if k != "action" for t in _flex_texts(v)]


def a_reply(r: dict) -> tuple[AIReply, dict[str, str]]:
    """ランタイムの返信(render の結果)を、LINE に送るメッセージ(line_channel.to_messages。LIFF なし)にしてから
    本人役の形にする。押せるのはクイックリプライだけで、返すのは (返信, ラベル -> 送られる文)。
    求人のカードとリンクのボタンは Flex の中の字として読むだけ。
    result: 求人のカードを見せた(提案)か、相談のまとめ(record_conversation_outcome の summary)か提案を記録した。
    closed: AI が会話を閉じた(outcome が wait)。summary が付いていても result に数えない。"""
    from line_runtime import line_channel

    msgs = line_channel.to_messages(r, base_url="https://aica.invalid", features=())
    quick = [i["action"] for i in (msgs[-1].get("quickReply") or {}).get("items", [])]
    outcomes = [t.get("args") or {} for t in r.get("tools") or [] if t["name"] == "record_conversation_outcome"]
    wait = any(a.get("outcome") == "wait" for a in outcomes)
    done = not wait and (bool(r.get("cards")) or any(a.get("summary") or a.get("outcome") == "propose" for a in outcomes))
    reply = AIReply(bubbles=[m["text"] for m in msgs if m["type"] == "text"], buttons=[q["label"] for q in quick],
                    flex=[t for m in msgs if m["type"] == "flex" for t in _flex_texts(m["contents"]) if t],
                    done=done, closed="ai_closed" if wait else "")
    return reply, {q["label"]: q["text"] for q in quick}


def declined(text: str) -> bool:
    """本人の返信が、相談の断り(playbook の逃げ道「今はいい」。押しても打っても)か。"""
    return re.sub(r"[\s。、.,!?！？…~〜ー]", "", text) in DECLINES


class FormA:
    """今の正社員の相談(dip_mcp/playbooks/fulltime_chat.md)。LINE のトークとして、LIFF の画面は使わない(チャットだけ)。
    履歴書を先に送っている人(persona の resume)は、会話の前にアップロード済みにする。"""

    def __init__(self, rt, persona: dict):
        self.rt, self.persona, self.tools, self.sends = rt, persona, [], {}

    async def _handle(self, event: str, text: str | None) -> AIReply:
        r = await self.rt.handle(A_USER, event, text, None, channel="line", features=())
        self.tools += r.get("tools") or []
        reply, self.sends = a_reply(r)
        return reply

    async def start(self) -> AIReply:
        from dip_mcp.data import State
        from evals.scenarios import prepare

        sc = {"user": A_USER, "event": "visit"}
        if self.persona.get("resume"):
            sc["seed_resume"] = self.persona["resume"]["file"]
        prepare(State(), sc)
        return await self._handle("visit", None)

    async def send(self, text: str, pressed: bool) -> AIReply:
        """本人が「今はいい」で切り上げて会話が閉じたら、それまでに答えた数によらず(playbook は 1 問でも答えていれば
        consult と summary、0 問なら wait を記録する)、本人の断りの離脱に数える。"""
        reply = await self._handle("message", self.sends.get(text, text) if pressed else text)
        if declined(text) and (reply.done or reply.closed):
            reply.done, reply.closed = False, "declined"
        return reply

    def captured(self) -> dict | None:
        return None  # 会話の記録(と、先に送った履歴書)から取り出す

    def saved_texts(self) -> list[str]:
        out = []
        for t in self.tools:
            args = t.get("args") or {}
            if t["name"] == "save_user_preferences":
                out += [str(v) for v in (args.get("preferences") or {}).values()]
            elif t["name"] == "record_conversation_outcome" and args.get("summary"):
                out.append(args["summary"])
        return out

    def wasted(self) -> int | None:
        return None


# ----------------------------- 台本の AI -----------------------------

PASS, DONT_KNOW = "パス", "覚えていない"
SEGMENTS = {"同じ仕事で上へ": "same_path", "仕事を変えたい": "change", "ブランクから戻る": "return", "経験が浅い": "early"}
SALARY = {"300万円台": [300, 399], "400万円台": [400, 499], "500万円台": [500, 599], "600万円以上": [600, None]}
SCALE = {"5人くらい": [1, 7], "10人くらい": [8, 14], "20人以上": [15, None]}
RANGES = SALARY | SCALE
RANGE_UNITS = {**dict.fromkeys(SALARY, "万円"), **dict.fromkeys(SCALE, "人")}  # 区切りの単位(人数の区切りは件数や社数に入らない)

GOOD_QUESTIONS = {
    "why_now": ("なぜ今、仕事を探していますか?", [PASS]),
    "area": ("働きたい場所はどこですか?", [PASS]),
    "min_annual_salary": ("年収はいくらから考えますか?", [*SALARY, "こだわらない", PASS]),
    "start_timing": ("いつから働きたいですか?", ["すぐ", "3か月以内", "半年以内", PASS]),
    "raise_target": ("いちばん上げたいのはどれですか?", ["年収", "役職", "規模", PASS]),
    "change_reason": ("仕事を変えたいのは、なぜですか?", [PASS]),
    "use_experience": ("次の仕事で活かしたい経験は何ですか?", [PASS]),
    "work_hours": ("働ける時間帯はいつですか?", [PASS]),
    "blank_period": ("お仕事から離れていた期間はどのくらいですか?", [PASS]),
    "keep_doing": ("今の仕事で、続けたいことは何ですか?", [PASS]),
    "stop_doing": ("反対に、やめたいことは何ですか?", [PASS]),
    "role": ("{which}お仕事を一言で教えてください。", [PASS]),
    "scale": ("何人くらいのチームでしたか?", [*SCALE, DONT_KNOW, PASS]),
    "result": ("それで、何が変わりましたか?", [DONT_KNOW, PASS]),
}


def value_of(text: str, pressed: bool) -> dict:
    """本人の返信を、取れた値の形に(台本の AI は読み取りに LLM を使わず、本人の言葉のまま入れる)。"""
    t = text.strip()
    if t == PASS or t in ("言いたくない", "パスします"):
        return {"status": "passed", "text": t, "number": None, "range": None, "unit": None}
    if t == DONT_KNOW or t.startswith(("覚えてない", "覚えていない")):
        return {"status": "dont_know", "text": t, "number": None, "range": None, "unit": None}
    rng = RANGES.get(t) if pressed else None
    return {"status": "filled", "text": t, "number": None, "range": rng, "unit": RANGE_UNITS.get(t) if rng else None}


class ScriptGood:
    """台本の良い AI。1 回に 1 問、吹き出しは 80 字まで、聞き直さない、答えやすいボタンとパスを付ける。
    segment → HARD → segment だけの HARD → 職歴カード 2 枚 → まとめ の順で聞き、まとめの返事で result に着く。
    設計メモ §3.3 と同じく、本人の推定時間を本人役と同じ式で数え、質問を止める時刻(目標時間 - 90 秒)を過ぎたら、
    空いている HARD のほかは聞かずにまとめに進む。履歴書の読み取りで埋まる欄(persona の resume.filled)は聞かない。"""

    def __init__(self, persona: dict):
        self.persona = persona
        self.values: dict[str, dict] = {}
        self.queue: list[str] = []
        self.hard: tuple[str, ...] = COMMON_HARD
        self.asking: str | None = None
        self.n_wasted = 0
        self.clock = Clock()
        resume = persona.get("resume") or {}
        self.stop_at = TARGET_SECONDS[bool(resume)] - REVIEW_RESERVE
        facts = {f["key"]: f for f in persona["facts"]}
        for k in resume.get("filled", []):  # 履歴書の読み取り結果(事実表と同じ中身)を high として入れる
            f = facts[k]
            self.values[k] = {"status": "filled", "text": fact_text(f), "number": f.get("value"), "range": None,
                              "unit": f.get("unit")}

    def _say(self, r: AIReply) -> AIReply:
        self.clock.read(r)
        return r

    async def start(self) -> AIReply:
        self.asking = "segment"
        return self._say(AIReply(bubbles=["職務経歴書の材料を3分で集めます。今のあなたに近いのは?"], buttons=[*SEGMENTS, PASS]))

    def _ask(self, key: str) -> AIReply:
        self.asking = key
        if key.startswith("career"):
            n, field = key.removeprefix("career").split(".")
            text, buttons = GOOD_QUESTIONS[field]
            text = text.format(which="直近の" if n == "1" else "その前の")
            if field == "role" and n == "2":
                text = "ありがとうございます。" + text
        else:
            text, buttons = GOOD_QUESTIONS[key]
        return self._say(AIReply(bubbles=[text], buttons=list(buttons)))

    def _review(self) -> AIReply:
        lines = [f"{label(k)}: {v['text'] or '未定'}" for k, v in self.values.items() if k != "segment"]
        self.asking = "review"
        return self._say(AIReply(bubbles=["まとめです。直すところはありますか?"], flex=lines, buttons=["これで大丈夫"]))

    def _next(self) -> str | None:
        while self.queue:
            key = self.queue.pop(0)
            if self.values.get(key, missing())["status"] != "missing":
                continue  # 埋まっている欄は聞かない
            if self.clock.seconds > self.stop_at and key not in self.hard:
                continue  # 止める時刻を過ぎたら、空いている HARD だけを聞く
            return key
        return None

    async def send(self, text: str, pressed: bool) -> AIReply:
        if pressed:
            self.clock.press()
        else:
            self.clock.type(text)
        if self.asking == "review":
            return AIReply(bubbles=["保存しました。おつかれさまでした。"], done=True)
        self.values[self.asking] = value_of(text, pressed)
        if self.asking == "segment":
            seg = SEGMENTS.get(text.strip(), "unknown") if pressed else "unknown"
            self.hard = COMMON_HARD + SEGMENT_HARD[seg]
            self.queue = [*self.hard, *(f"career{n}.{f}" for n in (1, 2) for f in ("role", "scale", "result"))]
        key = self._next()
        return self._ask(key) if key else self._review()

    def captured(self) -> dict:
        return {k: v for k, v in self.values.items() if k != "segment"}

    def saved_texts(self) -> list[str]:
        return [v["text"] for v in self.captured().values() if v["text"]]

    def wasted(self) -> int:
        return self.n_wasted


# 台本の悪い AI の返信。吹き出しが 80 字を超え、1 回に 2 つ聞き、もう聞いたことを聞き直し、面接口調
BAD_TURNS = [
    ([("こんにちは!正社員の転職のご相談ですね。これからいくつか大事な質問をさせていただきますので、できるだけ詳しく、具体的にお答えください。"
      "まず、転職を考えた理由を教えていただけますか?また、希望の勤務地はどちらでしょうか?")], ["why_now", "area"]),
    ([("ありがとうございます。それでは、あなたの強みは何ですか?具体的なエピソードを交えて説明してください。"
      "あわせて、希望の年収と、これまでのお仕事の内容も詳しく教えてください。")], ["min_annual_salary", "career1.role"]),
    ([("確認のため、もう一度うかがいます。転職を考えた理由は何でしたか?希望の勤務地も、あらためて教えていただけますか?"
      "細かいところまで伺えると、よりよいご提案ができます。")], ["why_now", "area"]),
    ([("承知しました。では、これまでで一番の成果は何ですか?その成果を、数字を使って説明してください。"
      "また、働き始める時期はいつごろをお考えですか?")], ["career1.result", "start_timing"]),
]


class ScriptBad:
    """台本の悪い AI。BAD_TURNS をくり返す。本人の返信は、その回に聞いた key のまだ空いているものに入れる。"""

    def __init__(self, persona: dict):
        self.persona = persona
        self.values: dict[str, dict] = {}
        self.i = 0
        self.n_wasted = 0

    def _turn(self) -> AIReply:
        bubbles, keys = BAD_TURNS[self.i % len(BAD_TURNS)]
        self.n_wasted += sum(1 for k in keys if self.values.get(k, missing())["status"] != "missing")
        return AIReply(bubbles=list(bubbles))

    async def start(self) -> AIReply:
        return self._turn()

    async def send(self, text: str, pressed: bool) -> AIReply:
        for k in BAD_TURNS[self.i % len(BAD_TURNS)][1]:
            if self.values.get(k, missing())["status"] == "missing":
                self.values[k] = value_of(text, pressed)
        self.i += 1
        return self._turn()

    def captured(self) -> dict:
        return dict(self.values)

    def saved_texts(self) -> list[str]:
        return [v["text"] for v in self.values.values() if v["text"]]

    def wasted(self) -> int:
        return self.n_wasted


# ----------------------------- 形 B、C、D: ハーネス + 実 LLM -----------------------------


def fixed_labels(H) -> list[str]:
    """ハーネス(H = dip_mcp.career.harness)が誰にでも同じに出す答えの候補とボタンのラベル(重なりを除いて出る順)。
    質問バンクの pages、pages_by_segment、explain_choices、escape と、決まったボタン(スキップ、他の候補など)。"""
    out = [H.START_GO, H.LATER, H.PASS, H.DONT_KNOW, H.MORE, H.REVIEW_OK, H.NOPREF]
    for q in H.BANK:
        pages = [*q.get("pages", []), *(p for by_n in (q.get("pages_by_segment") or {}).values()
                                         for ps in by_n.values() for p in ps)]
        out += [c for p in pages for c in p] + list(q.get("explain_choices", [])) + list(q.get("escape", []))
    return list(dict.fromkeys(out))


class FormHarness:
    """ハーネス(dip_mcp/career)を、トークと同じモデルの呼び方でつないだ形(B、C、D)。
    1 ターンは dip_mcp.career.session.turn と同じく、ハーネスがモデルの出力を求めたら(NeedLLM)ターンの始めの状態に戻し、
    モデルを呼んで、それまでの出力を全部渡してやり直す。モデルの呼び方とメッセージは LINE 側のランタイム(_harness_turn)と同じ
    (line_runtime.career_chat.default_llm と baito_chat.messages_for。生成には直前のやりとりを渡す)。
    dip の保存とトークの AI は通らない(相談の外のことなので)。履歴書を先に送っている人は、モックの回帰(evals/career_sim.py)と
    同じく、履歴書の欄を事実表の値で prefill する(「履歴書の写真から始める」のつなぎはまだ無い。設計メモ §8)。
    形 D は、ハーネスが選んだ部品(dip_mcp/career/ui.py)を LINE の Flex にして(line_runtime/career_ui.py)、中の字を本人役が読む。
    result に着いたら完了。「あとで」で閉じたら、本人が断った離脱に数える。
    取れた値は、モックの回帰と同じく終わりの状態(harness.captured)。保存した文は、その文と、職歴カードから作った下書き。"""

    def __init__(self, persona: dict, form: str, band: bool = True, call=None, version: str = "c1"):
        from dip_mcp.career import harness as H
        from dip_mcp.career import ui as UI
        from evals.career_sim import VERSIONS, prefill
        from line_runtime import career_chat

        # H は版によらない部分(質問バンク、取れた値、欄)。M は版の 1 ターン(c1 は H、c2 は dip_mcp.career.c2)
        self.H, self.M, self.UI = H, VERSIONS[version], UI
        self.persona, self.call = persona, call or career_chat.default_llm()
        self.st = self.M.new_state(form=form, prefill=prefill(persona))
        self.mem = UI.fresh_mem(self.st, band)
        # 画面に出たやりとり(生成に渡す直前のやりとり)。トークでは、相談を始めた本人の発言が最初の返信の前にある
        # (無いと baito_chat.recent が assistant で始まる最初の問いを落とす)
        self.convo: list[dict] = [{"role": "user", "content": START_SAID}]
        self.llm_calls = 0

    def _reply(self, out: dict, part: dict | None) -> AIReply:
        from line_runtime import baito_chat, career_ui

        built = career_ui.build(part)
        flex = career_ui.flex_lines(built["flex"]) if built else []
        baito_chat._append(self.convo, "assistant", "\n".join(out["bubbles"]))
        later = bool(out["done"] and self.st.closed)
        return AIReply(bubbles=list(out["bubbles"]), buttons=list(out["choices"]), flex=flex,
                       done=bool(out["done"]) and not later, closed="declined" if later else "")

    async def start(self) -> AIReply:
        out = self.M.start(self.st, self.H.BANK)
        part = self.UI.start_payload(self.st)
        self.UI.add_read(self.st, part)
        return self._reply(out, part)

    async def send(self, text: str, pressed: bool) -> AIReply:
        from dip_mcp.baito.session import NeedLLM, ReplayLLM
        from line_runtime import baito_chat

        convo = baito_chat.recent(self.convo)
        outputs: list[dict] = []
        snap = self.st.to_dict()
        while True:
            st = self.M.State.from_dict(snap)  # ターンの始めの状態からやり直す(dip が保存した状態から毎回始めるのと同じ)
            replay = ReplayLLM(outputs, self.M.render_prompt, lambda view: "say")
            prev = self.UI.before(st)
            try:
                out = self.M.turn(st, text, self.H.BANK, replay)
                break
            except NeedLLM as need:
                c = need.call
                if len(outputs) >= 4:  # dip_mcp.career.session.MAX_LLM_CALLS
                    raise RuntimeError("正社員の相談のモデルの呼び出しが、1 ターンの上限を超えました") from None
                # 本人の発言と直前のやりとりは、ハーネスが伏せたもの(c2 の model_text、model_history)があればそちら(ランタイムと同じ)
                said = c.get("model_text", text)
                past = baito_chat.recent(c["model_history"]) if "model_history" in c else convo
                try:
                    o, _ = await self.call(c["stage"], c["system"], baito_chat.messages_for(c["stage"], said, past))
                    outputs.append({"stage": c["stage"], "output": o})
                except Exception as e:  # noqa: BLE001  ハーネスが失敗 1 回として数える(ランタイムと同じ)
                    outputs.append({"stage": c["stage"], "error": type(e).__name__})
                self.llm_calls += 1
        self.st = st
        baito_chat._append(self.convo, "user", text)
        part, self.mem = self.UI.update(st, self.mem, prev, out)
        return self._reply(out, part)

    def captured(self) -> dict:
        return self.H.captured(self.st)

    def fixed_buttons(self) -> list[str]:
        """質問バンクの決まった候補(ジャッジが誘導に数えないもの。設計メモ §6.4)。"""
        return fixed_labels(self.H)

    def saved_texts(self) -> list[str]:
        from dip_mcp.career import draft

        texts = [v["text"] for v in self.captured().values() if v["text"]]
        return texts + ([draft.build(self.st)[0]] if self.st.result is not None else [])

    def wasted(self) -> int:
        """もう high で埋まっている欄を聞いた回数(evals/career_sim.py の wasted と同じ数え方)。"""
        pri = self.H.active_slots(self.st)
        return sum(1 for e in self.st.log if e["kind"] == "ask" and not e["open"] and any(t in pri for t in e["targets"]))


@contextlib.asynccontextmanager
async def _harness_forms(form: str, band: bool = True, version: str = "c1"):
    yield lambda persona: FormHarness(persona, form, band, version=version)


@contextlib.asynccontextmanager
async def _a_forms():
    from line_runtime.runtime import LineRuntime

    async with LineRuntime(in_memory=True) as rt:
        yield lambda persona: FormA(rt, persona)


@contextlib.asynccontextmanager
async def _plain(cls):
    yield cls


# 形の名前 -> 会話ごとの形を作る関数を出す context manager(ランタイムなどを 1 回の実行で使い回すため)
FORMS = {"A": _a_forms, "B": lambda: _harness_forms("B"), "C": lambda: _harness_forms("C"),
         "D": lambda: _harness_forms("D"), "D-noband": lambda: _harness_forms("D", band=False),
         "D-c2": lambda: _harness_forms("D", version="c2"),
         "script-good": lambda: _plain(ScriptGood), "script-bad": lambda: _plain(ScriptBad)}
# 実 API を呼ばずに回せる形(本人役とジャッジだけが LLM)
SCRIPTED = ("script-good", "script-bad")


def check_forms(names: list[str]) -> None:
    """--form の名前を、費用を使う前に確かめる。"""
    for n in names:
        if n in PLANNED:
            raise SystemExit(f"形 {n} はまだありません。{PLANNED[n]}が evals/fulltime_hearing/forms.py の FORMS に足します")
        if n not in FORMS:
            raise SystemExit(f"--form に知らない形: {n}(ある形: {list(FORMS)}、これから足す形: {list(PLANNED)})")

