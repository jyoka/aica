"""バイト探しの相棒の相談を、dip のユーザー状態に保存して1ターンずつ進める(dip のツールの中身。LLM は使わない)。

- 始める: トークの AI が、アルバイト・パートの相談に入ったターンに start_baito_consultation を呼ぶ(dip の Prompt の「相談」)。
  最初の質問(q_money)は、その AI が言い換えずに聞く
- 進める: その後の本人の発言は、LINE 側のランタイムが baito_turn に渡す(トークの AI は呼ばない)。
  クイックリプライの選択肢そのものなら choice_effects どおりにマージし、そうでなければ、モデルに読み取らせる
  システムプロンプトを返す(status=extract)。どちらも次に聞く質問を決め、言い方のためのシステムプロンプトを返す
- 終える: result に着いたら、確かめた条件を stated_preferences に保存して相談を閉じる(プロフィール画面で見て直せる)。
  本人がやめると言った・区分が外れた・新しい会話を始めたら、そこまでに本人が答えた条件(high)だけを保存して閉じ、
  トークの AI に戻す

状態は 1 人の dict の "baito"(進めている間だけ)。「すべて消す」は 1 人の dict ごと消すので、これも消える。
"""

from __future__ import annotations

import json
import re

from dip_mcp import logic
from dip_mcp.baito import harness as H
from dip_mcp.data import ROOT, State

KEY = "baito"
PROMPTS = ROOT / "dip_mcp" / "prompts"
# LINE 側のランタイムだけが呼ぶツール(トークの AI には見せない。line_runtime.runtime.chat_tools が外す)
RUNTIME_TOOLS = ("baito_turn", "end_baito_consultation")
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
# 扶養の金額の基準(「103万円」「130万の壁」など)は示さない(PRD §10)。扶養か「の壁」の話と、基準の額(100万〜209万)が
# 同じ返信にあれば使わない(1 回の返信は短いので、文をまたいでも同じ話として見る。「月5万円」のような額は当たらない)
FUYOU_AMOUNT = re.compile(r"(?<![\d,，])(1\d\d|20\d)\s*万")
BAITO_TRACK = "アルバイト・パート"
START_NOTE = ("相談を始めた。この返信で、一緒に整理することを1文で伝え、question の text を言い換えずに1問として聞く"
              "(選択肢は LINE 側が question の choices にする)。次の質問からはハーネスが進める。")


def _read(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def active(state: State, line_user_id: str) -> bool:
    return bool(state.user(line_user_id).get(KEY))


def _load(state: State, line_user_id: str) -> H.State | None:
    s = state.user(line_user_id).get(KEY)
    return H.State.from_dict(s["harness"]) if s else None


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


def public(q: dict | None) -> dict | None:
    """トークの AI と画面に渡す質問(id・質問文・選択肢)。"""
    return None if q is None else {"id": q["id"], "text": q["text"], "choices": list(q["choices"])}


def start(state: State, line_user_id: str) -> dict:
    """相談を始める(進めている相談があれば、今聞いている質問を返して続ける)。"""
    h = _load(state, line_user_id)
    started = h is None
    if started:
        h = H.State()
        H.step(h, None, H.BANK)
        _save(state, line_user_id, h, logic.search_track(state.user(line_user_id)))
    # 結果の後と途中でやめた後はトークの AI が続けるので、保存先と提案の決まり(playbook)もここで渡す
    pb = logic.counseling_playbook(BAITO_TRACK, "chat")
    return {"started": started, "active": True, "question": public(h.question), "note": START_NOTE,
            "rules": pb["rules"], "playbook": (logic.PLAYBOOKS / "parttime_chat.md").read_text(encoding="utf-8")}


def end(state: State, line_user_id: str) -> dict:
    """相談を途中で閉じる(新しい会話を始めた・会話の外のイベントが来た)。本人が答えた条件(high)だけを保存する。"""
    h = _load(state, line_user_id)
    if h is None:
        return {"ended": False}
    return {"ended": True, "saved": _finish(state, line_user_id, h, partial=True)}


def _exits(h: H.State, text: str) -> bool:
    if h.question and text in h.question["choices"]:
        return False
    return text in EXIT_WORDS or (h.phase != "review" and bool(EXIT_REQUEST.search(text))
                                  and not EXIT_HOLD.search(text))


def _model_out(extracted: dict) -> dict:
    """モデルの JSON のうち、ハーネスが読むところだけ(形の崩れたものは捨てる)。"""
    def strs(v):
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    ups = extracted.get("slot_updates")
    return {"slot_updates": [u for u in ups if isinstance(u, dict)] if isinstance(ups, list) else [],
            "ng_updates": strs(extracted.get("ng_updates")), "ng_removals": strs(extracted.get("ng_removals")),
            "segment": extracted.get("segment") if isinstance(extracted.get("segment"), str) else "unknown"}


def turn(state: State, line_user_id: str, text: str, extracted: dict | None = None) -> dict:
    """本人の発言 1 つで相談を進める。返すもの:
    - {"active": False}: 相談をしていない(やめた合図なら ended="exit")。トークの AI がこのターンを受け持つ
    - status=extract: 選択肢そのものではないので、system でモデルに読み取らせ、その JSON を extracted に入れて呼び直す
    - status=ask: 次の質問。system でモデルに言い方を作らせ、choices をクイックリプライにする
    - status=result: 結果。system でモデルに条件カードとおすすめ3つを書かせる。相談は閉じた(saved は保存した項目)"""
    h = _load(state, line_user_id)
    if h is None:
        return {"active": False}
    said = (text or "").strip()
    left = extracted is None and _track_left(state, line_user_id)
    if left or (extracted is None and _exits(h, said)):
        return {"active": False, "ended": "track" if left else "exit", "saved": _finish(state, line_user_id, h, partial=True)}
    out = H.answer_from_choice(h, said) if extracted is None else _model_out(extracted)
    if out is None:
        return {"active": True, "status": "extract", "phase": h.phase, "system": system_prompt(h)}
    q = H.step(h, out, H.BANK)
    if q is None:
        saved = _finish(state, line_user_id, h)
        return {"active": True, "status": "result", "phase": "result", "system": system_prompt(h),
                "choices": list(RESULT_CHOICES), "saved": saved, "fallback": fallback(h)}
    _save(state, line_user_id, h)
    return {"active": True, "status": "ask", "phase": h.phase, "question": public(q), "system": system_prompt(h),
            "choices": list(q["choices"]), "fallback": fallback(h)}


def unsafe_reply(text: str) -> str | None:
    """モデルの返信の文が、コードで守る決まりに反していれば、その理由(反していなければ None)。
    反していたら、ランタイムは返信の文を使わず fallback を出す。"""
    text = text or ""
    return "fuyou_amount" if FUYOU_AMOUNT.search(text) and ("扶養" in text or "の壁" in text) else None


def fallback(h: H.State) -> str:
    """モデルが返信の文を作れなかったときに、そのまま出す文: 質問文(原文)、review のカード、結果の条件カード。
    ハーネスはもう次の質問に進んでいるので、本人が見ていない質問に答える形にしない。"""
    return next_question(h).get("card_text") or h.question["text"]


def _finish(state: State, line_user_id: str, h: H.State, partial: bool = False) -> list[str]:
    """相談を閉じ、条件を本人の希望として保存する。保存した項目。避けたいこと(avoids)は、前に本人が話したものに足す
    (save_preferences は項目ごとに上書きするので、前の「接客は苦手」を NG で消さない)。"""
    _drop(state, line_user_id)
    prefs = result_preferences(h, partial)
    before = ((state.user(line_user_id).get("preferences") or {}).get("avoids") or {}).get("value")
    if before and "avoids" in prefs:
        prefs["avoids"] = "、".join(dict.fromkeys([before, *(x for x in h.ng if x not in before)]))
    return logic.save_preferences(state, line_user_id, prefs)["saved"] if prefs else []


def _label(h: H.State, slot: str) -> str | None:
    u = h.slots.get(slot)
    if u is None or u["confidence"] != "high":
        return None
    return u.get("raw_text") if u.get("normalized") is False else H.value_ja(slot, u["value"])


def result_preferences(h: H.State, partial: bool = False) -> dict[str, str]:
    """カードの条件のうち、プロフィールの項目(dip_mcp.logic.PREFERENCE_KEYS)に入るもの。確かめた値(high)だけ。
    result では review で推測も確定しているので、カードのとおりになる。partial(途中でやめた)は本人が答えた値だけ。"""
    time = [x for x in (_label(h, s) for s in ("time_slots", "available_hours", "weekday_or_weekend")) if x]
    wage = h.slots.get("hourly_wage_min")
    out = {"time_band": "、".join(time), "days_per_week": _label(h, "days_per_week"),
           "purpose": _label(h, "purpose"), "avoids": "、".join(h.ng),
           "min_wage": str(wage["value"]) if wage and wage["confidence"] == "high" and type(wage["value"]) is int else None}
    return {k: v for k, v in out.items() if v}


# ----------------------------- プロンプト(PRD §9) -----------------------------


def _slots_json(h: H.State) -> str:
    slots = {k: {"label": H.SLOT_LABEL_JA.get(k, k), "value": v["value"], "value_ja": H.value_ja(k, v["value"]),
                 "confidence": v["confidence"]} for k, v in h.slots.items()}
    return json.dumps({"slots": slots, "ng": h.ng}, ensure_ascii=False)


def next_question(h: H.State) -> dict:
    """{{next_question}} に入れるもの: 質問バンクの1問 / review のカード / result の最終カード。"""
    if h.phase == "result":
        card = H.build_card(h)
        return {"result": True, "card": card, "card_text": H.render_card_ja(card, final=True)}
    q = h.question
    if h.phase == "review":
        return {"card": q["card"], "card_text": q["text"]}
    keys = ("id", "phase", "text", "choices", "choice_effects", "targets", "retry")
    return {k: q[k] for k in keys if k in q}


def system_prompt(h: H.State) -> str:
    """そのターンのシステムプロンプト: §9.1 のメインプロンプト(変数を埋める) + ハーネスの補足 + review なら §9.2 の追加。
    変数はメインプロンプトの「現在の状態」の行だけを埋める(ルールの中の {{next_question}} は、その行を指す語として残す)。"""
    text = (_read("baito_main_ja.md")
            .replace("{{phase}}", h.phase, 1)
            .replace("{{segment}}", h.segment, 1)
            .replace("{{slots_json}}", _slots_json(h), 1)
            .replace("{{next_question}}", json.dumps(next_question(h), ensure_ascii=False), 1))
    parts = [text, _read("baito_notes_ja.md")] + ([_read("baito_review_ja.md")] if h.phase == "review" else [])
    return "\n".join(parts)
