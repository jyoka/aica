"""正社員のヒアリングの相談を、dip のユーザー状態に保存して 1 ターンずつ進める(dip のツールの中身。dip は LLM を呼ばない)。
設計メモ docs/design/fulltime-hearing.md の §3(入口、終わり、あとで)、ADR-0010。形はアルバイト・パートの
dip_mcp/baito/session.py と同じ。

- 始める: トークの AI が、正社員のチャットの相談に入ったターンに start_career_consultation を呼ぶ
  (CAREER_HARNESS_VERSION=c1 のときだけ。既定は使わない = 本番は今までの playbook の相談のまま)。
  前の相談で保存した条件と職歴カードは high として入れ、聞かない(dip_mcp.career.store.prefill)。
  最初の返信の吹き出しと選択肢はハーネスのもの(LINE 側がトークの AI の返信をこれに置き換える)
- 進める: その後の本人の発言は、LINE 側のランタイムが career_turn に渡す(トークの AI は呼ばない)。
  ハーネスの turn()(抽出 → 判断 → 生成。dip_mcp/career/harness.py)を、保存した状態から毎回やり直す。
  モデルの出力が要るときは status=llm でプロンプトを返し、ランタイムがモデルを呼んで、出力を全部 llm_outputs に入れて呼び直す。
  最後まで進んだ回だけ状態を保存する(途中の回は保存しない)
- 終える: result に着いたら、確かめた条件を stated_preferences に、職歴カードと segment を career_profile に保存して閉じる。
  職歴カードから職務経歴書の下書きも作り(dip_mcp/career/draft.py。task 247)、結果の返信に「下書きを開く」のボタンを付ける。
  「あとで」・本人がやめると言った・区分が正社員から外れた・新しい会話を始めたときも、そこまでの分(high)を保存して閉じる。
  続きから再開はしない(次の相談は、保存した分を聞かずに始め直す)

状態は 1 人の dict の "career"(進めている間だけ)。「すべて消す」は 1 人の dict ごと消すので、これも career_profile も消える。
"""

from __future__ import annotations

import re

from dip_mcp import logic
from dip_mcp.baito.session import (
    EXIT_HOLD,
    EXIT_WORDS,
    NeedLLM,
    ReplayLLM,
    ReplayMismatch,
)
from dip_mcp.career import draft as career_draft
from dip_mcp.career import harness as H
from dip_mcp.career import store
from dip_mcp.data import State

KEY = "career"
FLAG = logic.CAREER_HARNESS_FLAG  # c1 のときだけ新しい相談を始める。始めた相談はフラグを戻しても最後まで進める
# LINE 側のランタイムだけが呼ぶツール(トークの AI には見せない。line_runtime.runtime.chat_tools が外す)
RUNTIME_TOOLS = ("career_turn", "end_career_consultation")
START_TOOL = "start_career_consultation"
CAREER_TRACK = "正社員"
# 結果の後の選択肢(アルバイト・パートの相談と同じ。押した文はトークの AI が受ける)。「あとで」で閉じたときも同じものを付ける
# (返信を選択肢なしで終えない決まり。急いでいる本人が次に文字を打たずに済むように)
RESULT_CHOICES = ["求人を見る", "今はいい"]
DRAFT_LABEL = "下書きを開く"  # 結果の返信のボタン(下書きの画面。設計メモ §5.2 の「全部の完成」)
START_NOTE = ("相談を始めた。この返信は、question の bubbles をそのまま送る(言い換えない。LINE 側が返信の吹き出しを"
              "bubbles に、選択肢を question の choices に置き換える)。次の質問からはハーネスが進める。")
MAX_LLM_CALLS = 4  # 1 ターンのモデルの呼び出しの上限(文の答えで抽出 2 回 + 生成 2 回)
# 相談の外へ出る頼み(review の前だけ): アルバイト・パートで探したい(区分の切り替えはトークの AI がする)。
# アルバイト・パートの EXIT_REQUEST の「正社員で探したい」と同じ形。後回しや打ち消し(EXIT_HOLD)なら相談を続ける
PART_TIME = r"(アルバイト|バイト|パート)"
EXIT_REQUEST = re.compile(rf"{PART_TIME}(で|の(仕事|求人)を?)\s*(探|さが|見)|やっぱり{PART_TIME}|{PART_TIME}(が|に)(いい|したい)")
SAVED_SEGMENT = "saved_segment"  # 前の相談で保存して入れた segment(閉じるときに書き直さない)


def enabled() -> bool:
    return logic.career_harness_on()


def active(state: State, line_user_id: str) -> bool:
    return bool(state.user(line_user_id).get(KEY))


def _load(state: State, line_user_id: str) -> H.State | None:
    s = state.user(line_user_id).get(KEY)
    if not s:
        return None
    d = s.get("harness") or {}
    if d.get("version") != H.VERSION:  # 版が変わった状態は読まない(閉じて始め直す)
        _drop(state, line_user_id)
        return None
    return H.State.from_dict(d)


def _save(state: State, line_user_id: str, h: H.State, track: str | None = None, saved_segment: str | None = None) -> None:
    """ハーネスの状態を書く。track(始めたときの雇用区分)と saved_segment は始めるときだけ渡す(後は持ち越す)。"""
    def apply(u):
        before = u.get(KEY) or {}
        u[KEY] = {"harness": h.to_dict(), "track": track if track is not None else before.get("track"),
                  SAVED_SEGMENT: saved_segment if track is not None else before.get(SAVED_SEGMENT)}
    state.update(line_user_id, apply)


def _drop(state: State, line_user_id: str) -> None:
    state.update(line_user_id, lambda u: u.pop(KEY, None))


def _track_left(state: State, line_user_id: str) -> bool:
    """始めた後に、雇用区分が正社員から外れた(画面やトークの表示のボタンで取り消した・切り替えた)。"""
    u = state.user(line_user_id)
    now = logic.search_track(u)
    return now != CAREER_TRACK and now != (u.get(KEY) or {}).get("track")


def _question(out: dict) -> dict:
    """トークの AI と画面に渡す最初の質問(id・吹き出し・選択肢)。text は吹き出しをつないだもの。"""
    return {"id": out["qid"], "text": "\n".join(out["bubbles"]), "bubbles": list(out["bubbles"]),
            "choices": list(out["choices"])}


def start(state: State, line_user_id: str) -> dict:
    """相談を始める(進めている相談があれば、今出しているものを返して続ける)。"""
    h = _load(state, line_user_id)
    started = h is None
    if started and not enabled():
        return {"started": False, "active": False, "error": f"正社員の相談のハーネスは使っていません({FLAG})。"
                "get_counseling_playbook の playbook で相談する"}
    prefilled: list[str] = []
    segment = None
    if started:
        user = state.user(line_user_id)
        h = H.new_state()
        prefs = logic.stated_preferences(user)
        segment, items = store.prefill(user, prefs)
        H.apply_saved(h, segment, items, ng_known=store.ng_known(prefs))
        prefilled = (["segment"] if h.segment != "unknown" else []) + sorted(items) + (["avoids"] * store.ng_known(prefs))
    out = H.start(h, H.BANK)
    _save(state, line_user_id, h, logic.search_track(state.user(line_user_id)) if started else None, segment)
    pb = logic.counseling_playbook(CAREER_TRACK, "chat")  # 結果の後とやめた後はトークの AI が続けるので、決まりも渡す
    return {"started": started, "active": True, "question": _question(out), "prefilled": prefilled, "note": START_NOTE,
            "rules": pb["rules"], "playbook": pb["playbook"]}


def end(state: State, line_user_id: str) -> dict:
    """相談を途中で閉じる(新しい会話を始めた・会話の外のイベントが来た)。そこまでの分(high)を保存する。"""
    h = _load(state, line_user_id)
    if h is None:
        return {"ended": False}
    return {"ended": True, "saved": _finish(state, line_user_id, h)}


def _exits(h: H.State, text: str) -> bool:
    """やめる合図か(「今はいい」「やめる」など、review の前の「バイトで探したい」)。今の選択肢そのものは合図にしない。
    「あとで」はハーネスが受ける。"""
    if h.current and text in h.current.get("choices", []):
        return False
    return text in EXIT_WORDS or (h.phase not in ("review", "result") and bool(EXIT_REQUEST.search(text))
                                  and not EXIT_HOLD.search(text))


def turn(state: State, line_user_id: str, text: str, llm_outputs: list | None = None) -> dict:
    """本人の発言 1 つで相談を進める。返すもの:
    - {"active": False}: 相談をしていない(やめた合図・区分が外れたなら ended と saved)。トークの AI がこのターンを受け持つ
    - status=llm: モデルの出力がまだ要る。call.system をシステムプロンプトに、本人の発言を user にしてモデルを呼び、
      それまでの出力を全部 llm_outputs に入れて呼び直す(状態はまだ変わっていない)
    - status=reply: bubbles をそのまま送り、choices をクイックリプライにする
    - status=result: 終わった(result に着いた、か「あとで」で閉じた = closed)。相談は閉じ、saved は保存した項目。
      result に着いたときだけ、職歴カードから作った下書き(draft: id、blanks、replaced、view_url)と、下書きの画面を開く
      buttons(view_url があるときだけ。LINE は LIFF の設定があるとき)が付く"""
    h = _load(state, line_user_id)
    if h is None:
        return {"active": False}
    said = (text or "").strip()
    first = not llm_outputs
    left = first and _track_left(state, line_user_id)
    if left or (first and _exits(h, said)):
        return {"active": False, "ended": "track" if left else "exit", "saved": _finish(state, line_user_id, h)}
    llm = ReplayLLM(llm_outputs, H.render_prompt, lambda view: "say")
    try:
        r = H.turn(h, said, H.BANK, llm)
    except NeedLLM as need:
        if need.call["n"] >= MAX_LLM_CALLS:
            raise RuntimeError("正社員の相談のモデルの呼び出しが、1 ターンの上限を超えました") from None
        return {"active": True, "status": "llm", "call": need.call}
    except ReplayMismatch as e:
        raise RuntimeError(f"正社員の相談のモデルの出力が、ハーネスの求めと合いません({e})") from None
    if llm.used != len(llm.outputs):
        raise RuntimeError("正社員の相談のモデルの出力が、ハーネスの求めより多くあります(呼び直しの間に状態が変わった)")
    out = {"active": True, "status": "result" if r["done"] else "reply", "phase": h.phase, "question": r["qid"],
           "reply": r["reply"], "bubbles": list(r["bubbles"]), "choices": list(r["choices"]), "counted": r["counted"],
           "llm_calls": llm.used}
    if r["done"]:
        saved = _finish(state, line_user_id, h)
        out |= {"closed": h.closed, "choices": list(RESULT_CHOICES), "saved": saved, "blank_cards": H.blank_cards(h)}
        if h.closed:  # 「あとで」: 下書きは作らない(続きは次の相談で聞く)
            return out
        d = career_draft.create(state, line_user_id, h)
        return out | {"draft": d, "buttons": [{"label": DRAFT_LABEL, "url": d["view_url"]}] if d["view_url"] else []}
    _save(state, line_user_id, h)
    return out


def _finish(state: State, line_user_id: str, h: H.State) -> list[str]:
    """相談を閉じ、確かめた条件(high)を本人の希望に、segment と職歴カードを career_profile に保存する。保存した項目。
    推測のまま(low)の値は保存しない(まとめで確かめていないので。次の相談で聞き直す)。前の相談で保存して入れた値は
    書き直さない(相談の途中にプロフィール画面で直した・消した値を戻さない)。NG は避けたいことに、前の値に足して入れる
    (アルバイト・パートの相談と同じく、字数の上限に入るところまで。今回の NG を先に)。"""
    saved_segment = (state.user(line_user_id).get(KEY) or {}).get(SAVED_SEGMENT)
    _drop(state, line_user_id)
    # summary() の欄は評価の形(value を持たない)なので、ハーネスの欄をそのまま渡す(「こだわりなし」を見分けるため)
    s = {"segment": h.segment, "slots": h.slots, "pending": h.pending, "saved_segment": saved_segment}
    prefs = store.preferences_of(s["slots"])
    if h.user_ng:
        before = ((state.user(line_user_id).get("preferences") or {}).get("avoids") or {}).get("value") or ""
        keep: list[str] = []
        for x in dict.fromkeys([*h.user_ng, *(x for x in re.split(r"[、,，]\s*", before) if x)]):
            if len("、".join([*keep, x])) <= logic.PREFERENCE_VALUE_MAX:
                keep.append(x)
        prefs["avoids"] = "、".join(keep)
    saved = logic.save_preferences(state, line_user_id, prefs)["saved"] if prefs else []
    return saved + store.save(state, line_user_id, s)
