"""正社員のヒアリングの、答えの候補・スキップと中断・聞き返しの評価シナリオ(task 315。費用 0。実 API を呼ばない)。

    ./.venv/bin/python -m evals.career_choices          # シナリオごとの合否。最後に「すべて合格」

設計メモ docs/design/fulltime-hearing.md の §5.3。2026-10-28 に利用者が本番の LINE で見た 4 つ(候補が「パス」と「あとで」だけ、
「パス」と「あとで」の違いが分からない、何の規模か分からない、聞き返すと同じ問いと「うまく受け取れませんでした」)を、
質問バンクの全部の問い × 形 B、C、D × segment で確かめる。読み取りは台本(Script)で、モックの回帰(evals/career_sim.py)と同じく
ハーネスの動きだけを見る。

- candidates(受け入れ条件 1): どの問いも、最初のボタンに答えの候補が 2 つ以上。スキップと「覚えていない」だけの問いは無い
- skip_end(2): 問いのボタンは「スキップ」で、終える操作(END_WORDS)はボタンに無い。スキップは次の問いへ、「中断」は閉じる
- more(3): 「他の候補」を 2 回押すまで、見せていない候補が出る(重ならない)。出し尽くしたら文で送るよう案内し、もう出さない
- vague(4): 問いの文に「規模」が無い。規模の問いは何を数えるかを言う。どの問いも説明(explain)を持つ
- clarify(5): 聞き返しに、問いの文を繰り返さず、「読み取れなかった」と言わず、説明と候補を出す。2 回目は別の候補、3 回目は次へ
- pdm(6 の回帰): 利用者の流れ(PdM の職歴 → 規模を聞き返す → もう一度聞き返す → 候補を押す)
- short_tempered(6): 短気な本人役のモックの回帰(evals/career_sim.py。task 243 の本人役の決まり)の合否の指標がすべて 0
"""

from __future__ import annotations

import json
import sys

from dip_mcp.career import harness as H

FORMS = H.FORMS
SEGMENTS = (*H.SEGMENTS, "unknown")
CLARIFY = {"slot_updates": [], "intent": "clarify"}


class Script:
    """読み取りは台本を順に返す(尽きたら今の問いの欄に本人の文)。言い方は問いの文をそのまま返す。"""

    def __init__(self, extracts=()):
        self.extracts = list(extracts)
        self.calls: list[str] = []

    def extract(self, payload):
        self.calls.append("extract")
        if self.extracts:
            return json.dumps(self.extracts.pop(0), ensure_ascii=False)
        t = [x for x in payload["targets"] if x in H.ALL_SLOTS][:1]
        return json.dumps({"slot_updates": [{"slot": x, "text": payload["user_text"]} for x in t]}, ensure_ascii=False)

    def generate(self, view):
        self.calls.append("generate")
        return json.dumps({"reply": view["text"]}, ensure_ascii=False)


def _questions():
    """(形, segment, 状態, 問い) を全部。"""
    for form in FORMS:
        for seg in SEGMENTS:
            st = H.new_state(form=form)
            st.segment = seg
            for q in H.questions(st, H.BANK):
                yield form, seg, q


def _ask(form, seg, q):
    """q を出したところの状態(ターンの途中から始める。フェーズと出した問いだけを合わせる)。"""
    st = H.new_state(form=form)
    st.segment = seg
    st.phase = q["phase"]
    H._present(st, q)
    return st


def _cands(q, out):
    return [c for c in out["choices"] if c in q["choices"]]


def case_candidates() -> list[str]:
    bad = []
    for form, seg, q in _questions():
        ch = H.choices_of(q)
        cands = [c for c in ch if c in q["choices"]]
        if len(cands) < 2 or set(cands) & {H.PASS, H.DONT_KNOW, H.LATER, H.MORE}:
            bad.append(f"{form}/{seg}/{q['id']}: {ch}")
    st = H.new_state(form="C")
    why = H.choices_of(H.qmap(st, H.BANK)["ib_why_now"])
    if not {"年収を上げたい", "キャリアアップ", "人間関係", "働き方"} <= set(why):
        bad.append(f"なぜ今の候補に理由が無い: {why}")
    return bad


def case_skip_end() -> list[str]:
    bad = []
    if H.PASS != "スキップ" or "中断" not in H.START_LEAD:
        bad.append("スキップのラベルか、入口のやめ方の案内が無い")
    for form, seg, q in _questions():
        for ch in (H.choices_of(q), H.fix_choices(q)):
            if H.PASS not in ch or set(ch) & set(H.END_WORDS):
                bad.append(f"{form}/{seg}/{q['id']}: {ch}")
    for form in FORMS:
        st = _ask(form, "same_path", H.qmap(H.new_state(form=form), H.BANK)["ib_why_now"])
        out = H.turn(st, H.PASS, H.BANK, Script())
        if out["done"] or st.closed or out["qid"] == "ib_why_now":
            bad.append(f"{form}: スキップで次の問いに進まない({out['qid']}, closed={st.closed})")
        out = H.turn(st, "中断", H.BANK, Script())
        if not (out["done"] and st.closed == "later"):
            bad.append(f"{form}: 「中断」で閉じない")
    return bad


def case_more() -> list[str]:
    bad = []
    for form, seg, q in _questions():
        st = _ask(form, seg, q)
        seen = _cands(q, {"choices": st.current["choices"]})
        turns, script = st.turns, Script()
        for n in (1, 2):
            out = H.turn(st, H.MORE, H.BANK, script)
            got = _cands(q, out)
            if not got or set(got) & set(seen) or H.MORE not in out["choices"]:
                bad.append(f"{form}/{seg}/{q['id']}: {n} 回目の「他の候補」が {got}")
                break
            seen += got
        else:
            while H.MORE in out["choices"]:
                out = H.turn(st, H.MORE, H.BANK, script)
            if out["bubbles"] != [H.MORE_DONE] or H.PASS not in out["choices"]:
                bad.append(f"{form}/{seg}/{q['id']}: 出し尽くした後の案内が無い {out['bubbles']}")
        if st.turns != turns or script.calls:
            bad.append(f"{form}/{seg}/{q['id']}: 「他の候補」を数えたか、モデルを呼んだ")
    return bad


def case_vague() -> list[str]:
    bad = []
    for form, seg, q in _questions():
        if "規模" in q["text"] or "規模" in "".join(q["choices"]):
            bad.append(f"{form}/{seg}/{q['id']}: 「規模」だけで何の規模か言わない")
        if not q.get("explain"):
            bad.append(f"{form}/{seg}/{q['id']}: 聞き返されたときの説明が無い")
    st = H.new_state(form="C")
    for qid in ("card_scale1", "card_scale2"):
        text = H.qmap(st, H.BANK)[qid]["text"]
        if not all(w in text for w in ("チームの人数", "顧客の数", "売上")):
            bad.append(f"{qid}: 何を数えるかを言わない: {text}")
    return bad


def case_clarify() -> list[str]:
    bad = []
    for form, seg, q in _questions():
        st = _ask(form, seg, q)
        turns = st.turns
        out = H.turn(st, "どういうこと?", H.BANK, Script([CLARIFY]))
        first = _cands(q, out)
        if out["reply"] != q["explain"] or H.RETRY_TEXT in out["reply"] or q["text"] in out["reply"] or len(first) < 2:
            bad.append(f"{form}/{seg}/{q['id']}: 1 回目の聞き返しに説明と候補が無い: {out['reply']} {out['choices']}")
            continue
        out = H.turn(st, "よく分からない", H.BANK, Script([CLARIFY]))
        if out["reply"] != H.CLARIFY_AGAIN or not _cands(q, out) or st.turns != turns:
            bad.append(f"{form}/{seg}/{q['id']}: 2 回目の聞き返し: {out['reply']}")
            continue
        out = H.turn(st, "まだ分からない", H.BANK, Script([CLARIFY]))
        if H.RETRY_TEXT in out["reply"] or out["qid"] == q["id"] and not out["done"]:
            bad.append(f"{form}/{seg}/{q['id']}: 3 回目の聞き返しで次へ進まない")
    return bad


def _to(st, qid, limit=30):
    """qid が出るまで、候補の 1 つ目を押して進める。"""
    out = H.start(st)
    for _ in range(limit):
        if (st.current or {}).get("qid") == qid:
            return out
        if st.current["mode"] == "start":
            out = H.turn(st, H.START_GO, H.BANK, Script())
            continue
        q = H.qmap(st, H.BANK)[st.current["qid"]]
        out = H.turn(st, q["choices"][0], H.BANK, Script())
    raise AssertionError(f"{qid} が出なかった: {st.asked}")


def case_pdm() -> list[str]:
    """2026-10-28 の流れ: 同じ仕事で上へ → … → 直近の仕事は PdM → 規模の問い → 「規模って何? サービスの規模? チームの規模?」"""
    bad = []
    for form in FORMS:
        st = H.new_state(form=form)
        _to(st, "card_role1")
        out = H.turn(st, "PdM", H.BANK, Script([{"slot_updates": [{"slot": "career1.role", "text": "PdM"}]}]))
        q = H.qmap(st, H.BANK)["card_scale1"]
        if out["qid"] != "card_scale1" or "チームの人数" not in out["reply"]:
            bad.append(f"{form}: PdM の後の規模の問いが何を数えるか言わない: {out['reply']}")
            continue
        out = H.turn(st, "規模って何? サービスの規模? チームの規模?", H.BANK, Script([CLARIFY]))
        cands = _cands(q, out)
        if not (out["reply"].startswith("規模は、") and "利用者の数" in out["reply"] and "チームの人数" in out["reply"]):
            bad.append(f"{form}: 規模の聞き返しに意味を説明しない: {out['reply']}")
        if not (any(c.startswith("チーム") for c in cands) and any(c.startswith("利用者") for c in cands)):
            bad.append(f"{form}: 説明に合う候補(チーム、利用者)を出さない: {cands}")
        if q["text"] in out["reply"] or H.RETRY_TEXT in out["reply"]:
            bad.append(f"{form}: 同じ問いか「読み取れなかった」を返した")
        out = H.turn(st, "サービスのほう? チームのほう?", H.BANK, Script([CLARIFY]))
        if q["text"] in out["reply"] or H.RETRY_TEXT in out["reply"] or set(_cands(q, out)) & set(cands):
            bad.append(f"{form}: 2 回目の聞き返しで同じものを返した: {out['reply']} {out['choices']}")
        out = H.turn(st, "利用者1万人くらい", H.BANK, Script())
        e = st.slots.get("career1.scale") or {}
        if e.get("unit") != "万人" or out["qid"] != "card_result1":
            bad.append(f"{form}: 前に見せた候補を打っても規模が入らない: {e}")
    return bad


def case_short_tempered() -> list[str]:
    from evals import career_sim

    return [f"{r['form']}/{r['persona']}: {', '.join(career_sim.failures(r['metrics']))}"
            for r in career_sim.run_all() if career_sim.failures(r["metrics"])]


CASES = {"candidates": case_candidates, "skip_end": case_skip_end, "more": case_more, "vague": case_vague,
         "clarify": case_clarify, "pdm": case_pdm, "short_tempered": case_short_tempered}


def run_all() -> dict[str, list[str]]:
    return {name: fn() for name, fn in CASES.items()}


def main(argv=None) -> int:
    rows = run_all()
    for name, bad in rows.items():
        print(f"| {name} | {'合格' if not bad else f'不合格 {len(bad)} 件'} |")
        for b in bad[:10]:
            print(f"  - {b}")
    failed = [n for n, b in rows.items() if b]
    print("すべて合格" if not failed else f"不合格: {', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
