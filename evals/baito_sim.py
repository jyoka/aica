"""バイト探しの相棒(dip_mcp/baito)の回帰: モックの LLM で8ペルソナを最後まで話させ、PRD §11.3 の受け入れチェックと
§4.1 のベースラインに照らす。実 LLM は呼ばない(費用 0)。

    ./.venv/bin/python -m evals.baito_sim            # 集計表と、チェックの結果を出す(PRD の summary_v3.txt と同じ形)
    ./.venv/bin/python -m evals.baito_sim --log DIR  # ペルソナごとの会話のログも DIR に書く

ペルソナとモックの決まりは docs/prd/baito_harness/simulate.py の v3 をそのまま移した:
直接質問(hard_filter・fill_gaps)の答えは high、ほかは low。「どっちでもいい」はこだわりなし(high)、
「どっちでもない…」からは何も読み取らない。review では、ペルソナが決めた修正を返し、残った「どっち？」は先の値を選ぶ。
モデルの JSON は、実際の経路と同じく JSON の文字列を通してから渡す。
pytest は tests/test_baito_harness.py がこれを回す。
"""

from __future__ import annotations

import argparse
import json
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from dip_mcp.baito import harness as H

NOPREF, UNCLEAR = "どっちでもいい", "どっちでもない…"
STEP_LIMIT = 60  # これを超えて質問が続いたら LOOP


@dataclass
class Persona:
    name: str
    desc: str
    true_segment: str
    answers: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)       # qid -> 文で言った、はっきりした更新
    nopref: set = field(default_factory=set)         # 「どっちでもいい」と答える qid("*sc" はシナリオ全部)
    unclear: set = field(default_factory=set)        # 「どっちでもない…」と答える qid
    reveal: tuple = None                             # (ターン, segment, 文) 遅れて文で属性が分かる
    review: list = field(default_factory=list)       # review の回ごと: dict(text, updates, ng, segment, resolve)

    def choose(self, q):
        if q["id"] in self.unclear:
            return UNCLEAR
        if q["id"] in self.nopref or ("*sc" in self.nopref and q["phase"] == "scenario_game"):
            return NOPREF
        if q["id"] in self.answers:
            return self.answers[q["id"]]
        cs = q["choices"]
        return cs[zlib.crc32((self.name + q["id"]).encode()) % len(cs)]


PERSONAS = [
    Persona("hs16", "高1・16歳", "student",
            review=[{"text": "シフトは4時間くらいがいいな！あとはOK", "updates": [("shift_length", "4時間くらい")]}],
            answers={"q_money": "推し活・趣味", "q_segment": "学生", "hf_days": "週1〜2日", "hf_time": "夕方〜夜",
                     "hf_commute": "家から徒歩・自転車", "hf_age": "はい", "hf_school": "届け出すればOK",
                     "sc_line": "よし、燃えてきた", "sc_shift": "3時間でサクッと", "sc_st_peers": "同年代",
                     "sc_st_exam": "完全休み", "sc_st_after": "部活のあと", "rp_student": "引越しスタッフ",
                     "fg_wage": "1,000円台でOK", "fg_physical": "少しなら", "fg_exam": "休みたい"},
            facts={"q_segment": [("age_under_18", True)]}),   # 文で「高1です！」
    Persona("univ", "大学2年", "student",
            review=[{"text": "これでOK", "resolve": False}],
            answers={"q_money": "旅行・遊び", "q_segment": "学生", "hf_days": "週3〜4日", "hf_time": "夕方〜夜",
                     "hf_commute": "電車で30分まで", "hf_age": "いいえ", "hf_school": "OK",
                     "sc_shift": "6時間でガッツリ稼ぐ", "sc_st_exam": "少なめなら", "sc_st_summer": "がっつり",
                     "fg_wage": "1,200円以上"}),
    Persona("homemaker", "未就学児2人の母", "homemaker",
            review=[{"text": "在宅もアリかも。あと夜の仕事はNGで！", "updates": [("remote", "either")], "ng": ["夜の仕事"]}],
            answers={"q_money": "生活費・家計の足し", "q_segment": "家のこと中心", "hf_days": "週3〜4日",
                     "hf_time": "昼", "hf_commute": "家から徒歩・自転車", "hf_hours": "9〜14時くらい",
                     "hf_leave": "けっこうある", "sc_hm_fever": "すぐ休める", "sc_hm_remote": "外に出たい",
                     "sc_hm_money": "家計の足し", "sc_line": "今すぐ消えたい", "fg_fuyou": "はい、扶養内で",
                     "fg_remote": "外がいい", "fg_physical": "少しなら"}),
    Persona("office", "会社員・副業探し", "side_job",
            review=[{"text": "時給は1,200円以上はほしい。あとはOK", "updates": [("hourly_wage_min", 1200)]}],
            answers={"q_money": "貯金・将来のため", "q_segment": "本業あり副業探し", "hf_days": "週1〜2日",
                     "hf_time": "夕方〜夜", "hf_commute": "電車で30分まで", "hf_sideok": "届け出が必要",
                     "hf_weekday": "土日", "sc_sj_coworker": "絶対イヤ", "sc_sj_stand": "ふつう座れる",
                     "sc_sj_stable": "スポット（空いた日だけ）", "fg_energy": "少しだけ", "fg_wage": "1,200円以上"}),
    Persona("nopref", "大学生・ほぼ『どっちでもいい』", "student",
            review=[{"text": "これでOK"}],
            answers={"q_money": "旅行・遊び", "q_segment": "学生", "hf_days": "週3〜4日", "hf_age": "いいえ",
                     "hf_school": "OK"},
            nopref={"*sc", "hf_time", "hf_commute", "fg_shift", "fg_customer", "fg_physical", "fg_purpose",
                    "rp_student", "q_money"}),
    Persona("unclear", "会社員・あいまい回答多め", "side_job",
            review=[{"text": "時間は平日の夜なら大丈夫。通勤は30分までで", "updates": [("time_slots", "平日の夜"), ("commute", "train_30")]}],
            answers={"q_segment": "本業あり副業探し", "hf_days": "週1〜2日", "hf_sideok": "OK"},
            unclear={"hf_commute", "hf_time", "hf_weekday", "sc_line", "sc_shift", "sc_sj_stand", "sc_sj_goal",
                     "fg_wage", "fg_customer", "rp_side_job", "fg_physical"}),
    Persona("late_seg", "主婦・最初は属性を言わない（5ターン目に判明）", "homemaker",
            review=[{"text": "扶養内でお願いします。あとはOK", "updates": [("fuyou", "within")]}],
            answers={"q_money": "生活費・家計の足し", "q_segment": "うーん、いろいろかな",
                     "hf_time": "昼", "hf_hours": "10〜16時くらい", "hf_leave": "たまにある"},
            reveal=(5, "homemaker", "平日の昼は子どものお迎えがあって…")),
    Persona("no_seg", "大学生・属性を最後まで言わない(19歳)", "student",
            review=[{"text": "これでOK。あ、実は大学生です", "segment": "student"},
                    {"text": "学校はバイトOKだよ", "updates": [("school_permission", "ok")]}],
            answers={"q_segment": "ひみつ", "hf_age": "いいえ"}),
]

# PRD §4.1 の v3 のベースライン(docs/prd/baito_harness/summary_v3.txt)。退化しないことを見る
BASELINE = {
    "hs16": {"turns": 14, "phases": (2, 4, 6, 1, 0), "review_rounds": 1, "empty_mid": [], "repeated": []},
    "univ": {"turns": 14, "phases": (2, 5, 5, 1, 0), "review_rounds": 1, "empty_mid": [], "repeated": []},
    "homemaker": {"turns": 14, "phases": (2, 5, 5, 1, 0), "review_rounds": 1, "empty_mid": [], "repeated": []},
    "office": {"turns": 14, "phases": (2, 5, 4, 1, 1), "review_rounds": 1, "empty_mid": [], "repeated": []},
    "nopref": {"turns": 14, "phases": (2, 5, 5, 1, 0), "review_rounds": 1, "empty_mid": [], "repeated": []},
    "unclear": {"turns": 15, "phases": (2, 5, 2, 1, 3), "review_rounds": 2,
                "empty_mid": ["shift_length", "customer_facing", "physical"], "repeated": ["hf_weekday", "hf_time"]},
    "late_seg": {"turns": 14, "phases": (2, 5, 5, 1, 0), "review_rounds": 1, "empty_mid": ["physical"], "repeated": []},
    "no_seg": {"turns": 15, "phases": (2, 4, 6, 0, 1), "review_rounds": 2, "empty_mid": ["exam_flex"], "repeated": []},
}
PHASE_COLS = ("icebreak", "hard_filter", "scenario_game", "reverse_pick", "fill_gaps")


def _roundtrip(out: dict) -> dict:
    return json.loads(json.dumps(out, ensure_ascii=False))


def mock_review(q, persona):
    card, rnd = q["card"], q["card"]["round"]
    sc = persona.review[rnd - 1] if rnd <= len(persona.review) else {"text": REVIEW_OK, "resolve": True}
    ups = [{"slot": k, "value": v, "confidence": "high", "from": "review"} for k, v in sc.get("updates", [])]
    text = sc["text"]
    if sc.get("resolve", True):
        for it in card["hard"] + card["prefs"]:
            if it["conflict_values"] and it["slot"] not in {u["slot"] for u in ups}:
                v = it["conflict_values"][0]           # ペルソナは先に並んだほうを選ぶ
                ups.append({"slot": it["slot"], "value": v, "confidence": "high", "from": "review"})
                text += f"／{it['label']}は「{it['conflict'][0]}」のほう"
    out = {"reply": "了解！", "choices": [], "slot_updates": ups, "ng_updates": sc.get("ng", []),
           "segment": sc.get("segment") or "unknown"}
    return text, _roundtrip(out)


REVIEW_OK = H.REVIEW_OK


def mock_llm(q, persona, turn):
    """(本人の答え, プロンプトが決める JSON)。"""
    if q["phase"] == "review":
        return mock_review(q, persona)
    ans = persona.choose(q)
    conf = "high" if q["phase"] in H.DIRECT else "low"
    ups, ngs, seg = [], [], None
    tgt = [t for t in q["targets"] if t != "segment"]
    if ans == NOPREF:
        ups = [{"slot": t, "value": H.NOPREF, "confidence": "high", "from": q["id"]} for t in tgt]
    elif ans == UNCLEAR or ans not in q["choice_effects"]:
        pass                                   # 読み取れるものが無い
    else:
        for k, v in q["choice_effects"][ans].items():
            if k == "__ng__":
                ngs += v
            elif k == "__segment__":
                seg = v
            else:
                ups.append({"slot": k, "value": v, "confidence": conf, "from": q["id"]})
    for slot, v in persona.facts.get(q["id"], []):
        ups.append({"slot": slot, "value": v, "confidence": "high", "from": q["id"] + ":free_text"})
    if persona.reveal and turn == persona.reveal[0]:
        seg = persona.reveal[1]
        ans += f"（{persona.reveal[2]}）"
    out = {"reply": "なるほど！", "choices": q["choices"], "slot_updates": ups, "ng_updates": ngs,
           "segment": seg or "unknown"}
    return ans, _roundtrip(out)


def all_slots(seg):
    s = dict(H.SLOT_PRIORITY["common"])
    s.update(H.SLOT_PRIORITY.get(seg, {}))
    return s


def run(p: Persona, bank: list[dict] | None = None, log: list[str] | None = None) -> dict:
    """1人を最後まで話させ、PRD の simulate.py と同じ指標を返す。"""
    bank = H.BANK if bank is None else bank
    log = [] if log is None else log
    st, rows, crash, loop, steps = H.State(), [], None, False, 0
    try:
        q = H.step(st, None, bank)
        while q:
            steps += 1
            if steps > STEP_LIMIT:
                loop = True
                break
            turn = st.turns + 1
            gaps = {t: H.gap_weight(st, t) for t in q["targets"] if t != "segment"}
            before = {k: (v["value"], v["confidence"]) for k, v in st.slots.items()}
            seg0, ng0, phase = st.segment, list(st.ng), st.phase
            ans, out = mock_llm(q, p, turn)
            nq = H.step(st, out, bank)
            after = {k: (v["value"], v["confidence"]) for k, v in st.slots.items()}
            ch = [f"{k}: {before.get(k, ('∅', ''))[0]}→{v[0]}({v[1]})" for k, v in after.items() if before.get(k) != v]
            # マージが捨てた更新(§12-1 の正規化で値が列挙値に寄ったものは、寄せた値で比べる)
            dropped = [u["slot"] for u in out["slot_updates"]
                       if after.get(u["slot"]) != (H.normalize(u["slot"], u["value"])[0],
                                                   "high" if phase == "review" else u["confidence"])
                       and before.get(u["slot"], (None, None))[1] != "high"]
            rows.append({"turn": turn, "phase": phase, "qid": q["id"], "qphase": q["phase"], "text": q["text"], "ans": ans,
                             "changes": ch, "seg": (seg0, st.segment), "ng": [x for x in st.ng if x not in ng0],
                             "gaps_at_ask": gaps, "segs": q["segments"], "dropped": dropped, "choices": list(q["choices"]),
                             "retry": bool(q.get("retry"))})
            if phase == "review":
                log.append(f"T{turn:02d} [review] {q['id']}  ----- card -----")
                log += ["     | " + line for line in q["text"].split("\n")]
            else:
                log.append(f"T{turn:02d} [{phase}] {q['id']}: {q['text']}  {q['choices']}" + ("  (retry)" if q.get("retry") else ""))
            log.append(f"     → {ans}")
            if ch:
                log.append("     slots: " + "; ".join(ch))
            if dropped:
                log.append("     !! update DROPPED by merge: " + ", ".join(dropped))
            if seg0 != st.segment:
                log.append(f"     segment: {seg0}→{st.segment}")
            if rows[-1]["ng"]:
                log.append(f"     NG+: {rows[-1]['ng']}")
            if phase == "review":
                log.append(f"     review_log: {st.review_log[-1]}")
                rows[-1]["card"] = q["card"]
            elif st.conflicts:
                log.append(f"     conflicts: {st.conflicts}")
            q = nq
    except Exception as e:  # noqa: BLE001  落ちたことも指標にする(CRASH)
        crash = repr(e)
    reached = not loop and crash is None and st.phase == "result"
    log.append(f"== END phase={st.phase} turns={st.turns} segment={st.segment} NG={st.ng}")
    log.append("   final slots: " + "; ".join(f"{k}={v['value']}({v['confidence']})" for k, v in st.slots.items()))
    truth = all_slots(p.true_segment)
    empty = {lvl: [s for s, pr in truth.items() if pr == L and s not in st.slots] for lvl, L in (("HARD", 3), ("MID", 2))}
    low = {lvl: [s for s, pr in truth.items() if pr == L and s in st.slots and st.slots[s]["confidence"] == "low"]
           for lvl, L in (("HARD", 3), ("MID", 2))}
    qc = Counter(r["qid"] for r in rows)
    rv = [r for r in rows if r["phase"] == "review"]
    return {
        "persona": p.name, "turns": st.turns, "reached": reached, "loop": loop, "crash": crash,
        "per_phase": dict(Counter(r["phase"] for r in rows)), "final_seg": st.segment, "empty": empty, "low": low, "ng": st.ng,
        "repeated": [k for k, v in qc.items() if v > 1],
        "irrelevant": [r["qid"] for r in rows if "all" not in r["segs"] and p.true_segment not in r["segs"]],
        "skipped": [ph for ph in H.ORDER[:-1] if ph not in {r["phase"] for r in rows}],
        "wasted": [r["qid"] for r in rows if r["gaps_at_ask"] and all(w == 0 for w in r["gaps_at_ask"].values())],
        "dropped": sum(len(r["dropped"]) for r in rows),
        "hit_max": st.turns >= H.MAX_TURNS and bool(empty["HARD"] or low["HARD"] or empty["MID"]),
        "late_night_ng": (H.LATE_NIGHT_NG in st.ng), "asked_segment_q": ("q_segment" in qc),
        "late_night_choice_shown": [r["qid"] for r in rows if "深夜もOK" in r["choices"] and H.LATE_NIGHT_NG in st.ng],
        "retried": [r["qid"] for r in rows if r["retry"]],
        "review_rounds": st.review_rounds, "review_turns": [r["turn"] for r in rv],
        "conflicts_on_first_card": (rv[0]["card"]["conflicts"] if rv else None),
        "conflicts_open_at_end": dict(st.conflicts),
        "unconfirmed": [s for s in truth if truth[s] >= 2 and (s not in st.slots or st.slots[s]["confidence"] != "high")],
        "unconfirmed_low": [s for s in truth if truth[s] == 1 and s in st.slots and st.slots[s]["confidence"] != "high"],
        "empty_low_pri": [s for s in truth if truth[s] == 1 and s not in st.slots],
        "raw_texts": {k: v["raw_text"] for k, v in st.slots.items() if "raw_text" in v},
        "final_slots": {k: v["value"] for k, v in st.slots.items()},
    }


def phases(m: dict) -> tuple[int, ...]:
    return tuple(m["per_phase"].get(x, 0) for x in PHASE_COLS)


def acceptance(results: list[dict]) -> list[str]:
    """PRD §11.3 の受け入れチェック(1〜8。9 は実 LLM の会話を見る)と、§4.1 のベースラインとの比較。違反の一覧(空なら合格)。
    §11.3 の 6(会話の終わりに残る矛盾)は、§12 の 2 が決まっていないので、ペルソナで起きない今のまま 0 を見る。"""
    bad = []
    for m in results:
        p, base = m["persona"], BASELINE[m["persona"]]
        if not m["reached"] or m["loop"] or m["crash"]:
            bad.append(f"{p}: result に届かない(loop={m['loop']} crash={m['crash']})")
        if m["turns"] > H.MAX_TURNS:
            bad.append(f"{p}: {m['turns']} ターン(上限 {H.MAX_TURNS})")
        if m["review_rounds"] < 1:
            bad.append(f"{p}: review をしていない")
        if m["empty"]["HARD"] or m["low"]["HARD"]:
            bad.append(f"{p}: 空か low の HARD {m['empty']['HARD'] + m['low']['HARD']}")
        if not m["asked_segment_q"]:
            bad.append(f"{p}: セグメントの質問をしていない")
        if m["conflicts_open_at_end"]:
            bad.append(f"{p}: 終わりに矛盾が残った {m['conflicts_open_at_end']}")
        for key in ("irrelevant", "wasted"):
            if m[key]:
                bad.append(f"{p}: {key}={m[key]}")
        if m["dropped"]:
            bad.append(f"{p}: dropped={m['dropped']}")
        # §4.1 より悪くならない: ターン数、シナリオのターン数(v2a の2ターンへ戻らない)、空の MID、同じ質問の繰り返し
        if m["turns"] > base["turns"]:
            bad.append(f"{p}: ターンが増えた {base['turns']} → {m['turns']}")
        if phases(m)[2] < base["phases"][2]:
            bad.append(f"{p}: シナリオのターンが減った {base['phases'][2]} → {phases(m)[2]}")
        if not set(m["empty"]["MID"]) <= set(base["empty_mid"]):
            bad.append(f"{p}: 空の MID が増えた {base['empty_mid']} → {m['empty']['MID']}")
        if not set(m["repeated"]) <= set(base["repeated"]):
            bad.append(f"{p}: 繰り返した質問が増えた {base['repeated']} → {m['repeated']}")
    minors = [m for m in results if m["persona"] == "hs16"]
    for m in minors:
        if not m["late_night_ng"]:
            bad.append(f"{m['persona']}: 未成年なのに NG に「{H.LATE_NIGHT_NG}」が無い")
        if m["late_night_choice_shown"]:
            bad.append(f"{m['persona']}: 未成年に「深夜もOK」を出した {m['late_night_choice_shown']}")
    return bad


def table(results: list[dict]) -> str:
    out = [f"{'persona':10}{'turns':>6} {'result':>6} {'seg':>10}  phases(ib/hf/sc/rp/fg)  emptyHARD | lowHARD | emptyMID | lowMID | other"]
    for m in results:
        other = []
        if m["repeated"]:
            other.append(f"repeat={m['repeated']}")
        for key in ("irrelevant", "wasted"):
            if m[key]:
                other.append(f"{key}={m[key]}")
        if m["dropped"]:
            other.append(f"dropped_updates={m['dropped']}")
        if m["hit_max"]:
            other.append("CUT_BY_MAX_TURNS")
        if m["loop"]:
            other.append("LOOP")
        if m["crash"]:
            other.append(f"CRASH {m['crash']}")
        if m["persona"] == "hs16":
            other.append(f"深夜NG={m['late_night_ng']}")
        ph = "/".join(map(str, phases(m)))
        out.append(f"{m['persona']:10}{m['turns']:>6} {m['reached']!s:>6} {m['final_seg']:>10}  {ph:22}  "
                   f"{m['empty']['HARD']} | {m['low']['HARD']} | {m['empty']['MID']} | {m['low']['MID']} | {' '.join(other)}")
    out += ["", "--- review ---",
            f"{'persona':10}{'turns':>6} {'rounds':>6} review@turn  unconfirmed(HARD/MID) | conflicts on 1st card -> open at end | raw_text"]
    for m in results:
        out.append(f"{m['persona']:10}{m['turns']:>6} {m['review_rounds']:>6} {m['review_turns']!s:12} "
                   f"{m['unconfirmed']} | {m['conflicts_on_first_card']} -> {m['conflicts_open_at_end']} | {m['raw_texts']}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--log", type=Path, help="ペルソナごとの会話のログを書くディレクトリ")
    args = ap.parse_args()
    results = []
    for p in PERSONAS:
        log = [f"### {p.name} ({p.desc}, true segment={p.true_segment})"]
        results.append(run(p, log=log))
        if args.log:
            args.log.mkdir(parents=True, exist_ok=True)
            (args.log / f"{p.name}.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
    print(table(results))
    bad = acceptance(results)
    print("\n--- PRD §11.3 の受け入れチェックと §4.1 のベースライン ---")
    print("\n".join(bad) if bad else "すべて合格")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
