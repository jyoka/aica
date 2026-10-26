"""Mock-LLM simulator for the baito harness.
usage: python3 simulate.py [v1|v1w|v2|all]
  v1  = harness.py   + bank (q_segment targets ["segment"])
  v1w = harness.py   + bank where q_segment also targets "purpose" (workaround so v1 actually asks it)
  v2  = harness_v2.py + bank (q_segment targets ["segment"])
"""
import sys, json, zlib, importlib, signal, os
from collections import Counter
from dataclasses import dataclass, field
import bank as bankmod

NOPREF, UNCLEAR = "どっちでもいい", "どっちでもない…"
DIRECT_PHASES = {"hard_filter", "fill_gaps"}
SEG_MAP = {"学生": "student", "家のこと中心": "homemaker", "本業あり副業探し": "side_job"}

@dataclass
class Persona:
    name: str
    desc: str
    true_segment: str
    answers: dict = field(default_factory=dict)
    facts: dict = field(default_factory=dict)       # qid -> extra explicit slot updates from free text
    nopref: set = field(default_factory=set)         # qids answered どっちでもいい ("*sc" = all scenario)
    unclear: set = field(default_factory=set)        # qids answered どっちでもない
    reveal: tuple = None                             # (turn, segment, text) segment revealed late in free text
    review: list = field(default_factory=list)       # V3: per review round: dict(text, updates, ng, segment, resolve)

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
            facts={"q_segment": [("age_under_18", True)]}),   # free text: 「高1です！」
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
            review=[{"text": "これでOK。あ、実は大学生です", "segment": "student"}, {"text": "学校はバイトOKだよ", "updates": [("school_permission", "ok")]}],
            answers={"q_segment": "ひみつ", "hf_age": "いいえ"}),
]

def mock_review(q, persona):
    card, rnd = q["card"], q["card"]["round"]
    sc = persona.review[rnd - 1] if rnd <= len(persona.review) else {"text": "これでOK", "resolve": True}
    ups = [{"slot": k, "value": v, "confidence": "high", "from": "review"} for k, v in sc.get("updates", [])]
    text = sc["text"]
    if sc.get("resolve", True):
        for it in card["hard"] + card["prefs"]:
            if it["conflict_values"] and it["slot"] not in {u["slot"] for u in ups}:
                v = it["conflict_values"][0]           # persona picks the first-listed option
                ups.append({"slot": it["slot"], "value": v, "confidence": "high", "from": "review"})
                text += f"／{it['label']}は「{it['conflict'][0]}」のほう"
    out = {"reply": "了解！", "choices": [], "slot_updates": ups, "ng_updates": sc.get("ng", []),
           "segment": sc.get("segment") or "unknown"}
    return text, json.loads(json.dumps(out, ensure_ascii=False))

def mock_llm(q, persona, turn):
    """Returns (answer_text, JSON dict as the system prompt specifies)."""
    if q["phase"] == "review":
        return mock_review(q, persona)
    ans = persona.choose(q)
    conf = "high" if q["phase"] in DIRECT_PHASES else "low"
    ups, ngs, seg = [], [], None
    tgt = [t for t in q["targets"] if t != "segment"]
    if ans == NOPREF:
        ups = [{"slot": t, "value": "こだわりなし", "confidence": "high", "from": q["id"]} for t in tgt]
    elif ans == UNCLEAR or ans not in q["choice_effects"]:
        pass                                   # nothing extractable
    else:
        for k, v in q["choice_effects"][ans].items():
            if k == "__ng__": ngs += v
            elif k == "__segment__": seg = v
            else: ups.append({"slot": k, "value": v, "confidence": conf, "from": q["id"]})
    for slot, v in persona.facts.get(q["id"], []):
        ups.append({"slot": slot, "value": v, "confidence": "high", "from": q["id"] + ":free_text"})
    if persona.reveal and turn == persona.reveal[0]:
        seg = persona.reveal[1]; ans += f"（{persona.reveal[2]}）"
    out = {"reply": "なるほど！", "choices": q["choices"], "slot_updates": ups, "ng_updates": ngs,
           "segment": seg or "unknown"}
    return ans, json.loads(json.dumps(out, ensure_ascii=False))   # JSON round-trip

def all_slots(H, seg):
    s = dict(H.SLOT_PRIORITY["common"]); s.update(H.SLOT_PRIORITY.get(seg, {})); return s

class Timeout(Exception): pass
def _alarm(*a): raise Timeout()

def run(H, bank, p, log):
    st = H.State(); rows = []
    if hasattr(H, "set_value_labels"): H.set_value_labels(bank); crash = None; loop = False; steps = 0
    signal.signal(signal.SIGALRM, _alarm); signal.alarm(5)
    try:
        q = H.step(st, None, bank)
        while q:
            steps += 1
            if steps > 60: loop = True; break
            turn = st.turns + 1
            gaps = {t: H.gap_weight(st, t) for t in q["targets"] if t != "segment"}
            before = {k: (v["value"], v["confidence"]) for k, v in st.slots.items()}
            seg0, ng0, phase = st.segment, list(st.ng), st.phase
            ans, out = mock_llm(q, p, turn)
            nq = H.step(st, out, bank)
            after = {k: (v["value"], v["confidence"]) for k, v in st.slots.items()}
            ch = [f"{k}: {before.get(k, ('∅',''))[0]}→{v[0]}({v[1]})" for k, v in after.items() if before.get(k) != v]
            dropped = [u["slot"] for u in out["slot_updates"] if after.get(u["slot"]) != (u["value"], u["confidence"])
                       and before.get(u["slot"], (None, None))[1] != "high"]
            rows.append(dict(turn=turn, phase=phase, qid=q["id"], qphase=q["phase"], text=q["text"], ans=ans,
                             changes=ch, seg=(seg0, st.segment), ng=[x for x in st.ng if x not in ng0],
                             gaps_at_ask=gaps, segs=q["segments"], dropped=dropped))
            if phase == "review":
                log.append(f"T{turn:02d} [review] {q['id']}  ----- card -----")
                log += ["     | " + l for l in q["text"].split("\n")]
            else:
                log.append(f"T{turn:02d} [{phase}] {q['id']}: {q['text']}  {q['choices']}")
            log.append(f"     → {ans}")
            if ch: log.append("     slots: " + "; ".join(ch))
            if dropped: log.append("     !! update DROPPED by merge: " + ", ".join(dropped))
            if seg0 != st.segment: log.append(f"     segment: {seg0}→{st.segment}")
            if rows[-1]["ng"]: log.append(f"     NG+: {rows[-1]['ng']}")
            if phase == "review": log.append(f"     review_log: {st.review_log[-1]}")
            if getattr(st, "conflicts", None) and phase != "review": log.append(f"     conflicts: {st.conflicts}")
            if phase == "review": rows[-1]["card"] = q["card"]
            q = nq
    except Timeout:
        loop = True
    except Exception as e:
        crash = repr(e)
    finally:
        signal.alarm(0)
    reached = (not loop and crash is None and st.phase == "result")
    log.append(f"== END phase={st.phase} turns={st.turns} segment={st.segment} NG={st.ng}")
    log.append("   final slots: " + "; ".join(f"{k}={v['value']}({v['confidence']})" for k, v in st.slots.items()))
    # metrics against the persona's TRUE segment
    truth = all_slots(H, p.true_segment)
    empty = {lvl: [s for s, pr in truth.items() if pr == L and s not in st.slots] for lvl, L in (("HARD", 3), ("MID", 2))}
    low = {lvl: [s for s, pr in truth.items() if pr == L and s in st.slots and st.slots[s]["confidence"] == "low"]
           for lvl, L in (("HARD", 3), ("MID", 2))}
    qc = Counter(r["qid"] for r in rows)
    m = dict(persona=p.name, turns=st.turns, reached=reached, loop=loop, crash=crash,
             per_phase=dict(Counter(r["phase"] for r in rows)), final_seg=st.segment,
             empty=empty, low=low, ng=st.ng,
             repeated=[k for k, v in qc.items() if v > 1],
             irrelevant=[r["qid"] for r in rows if "all" not in r["segs"] and p.true_segment not in r["segs"]],
             skipped=[ph for ph in H.ORDER[:-1] if ph not in {r["phase"] for r in rows}],
             wasted=[r["qid"] for r in rows if r["gaps_at_ask"] and all(w == 0 for w in r["gaps_at_ask"].values())],
             dropped=sum(len(r["dropped"]) for r in rows),
             hit_max=st.turns >= H.MAX_TURNS and bool(empty["HARD"] or low["HARD"] or empty["MID"]), late_night_ng=("深夜シフト" in st.ng),
             asked_segment_q=("q_segment" in qc))
    if hasattr(st, "review_rounds"):
        rv = [r for r in rows if r["phase"] == "review"]
        m.update(review_rounds=st.review_rounds, review_turns=[r["turn"] for r in rv],
                 conflicts_on_first_card=(rv[0]["card"]["conflicts"] if rv else None),
                 conflicts_open_at_end=dict(st.conflicts),
                 unconfirmed=[s for s in truth if truth[s] >= 2 and (s not in st.slots or st.slots[s]["confidence"] != "high")],
                 unconfirmed_low=[s for s in truth if truth[s] == 1 and s in st.slots and st.slots[s]["confidence"] != "high"],
                 empty_low_pri=[s for s in truth if truth[s] == 1 and s not in st.slots])
    return m

MODES = {"v1": ("harness", ("segment",)), "v1w": ("harness", ("segment", "purpose")),
         "v2": ("harness_v2", ("segment",)),
         "v2a": ("harness_v2a_confirm_all_low", ("segment",)),
         "v3": ("harness_v3", ("segment",))}

def main(modes):
    os.makedirs("transcripts", exist_ok=True); res = {}
    for mode in modes:
        modname, segt = MODES[mode]
        H = importlib.import_module(modname); bank = bankmod.make_bank(segt)
        res[mode] = []
        for p in PERSONAS:
            log = [f"### {mode} / {p.name} ({p.desc}, true segment={p.true_segment})"]
            m = run(H, bank, p, log); res[mode].append(m)
            open(f"transcripts/{mode}_{p.name}.txt", "w").write("\n".join(log) + "\n")
        print(f"\n=== {mode} ({modname}.py) ===")
        print(f"{'persona':10}{'turns':>6} {'result':>6} {'seg':>10}  phases(ib/hf/sc/rp/fg)  emptyHARD | lowHARD | emptyMID | lowMID | other")
        for m in res[mode]:
            pp = m["per_phase"]; ph = "/".join(str(pp.get(x, 0)) for x in ["icebreak", "hard_filter", "scenario_game", "reverse_pick", "fill_gaps"])
            other = []
            if m["repeated"]: other.append(f"repeat={m['repeated']}")
            if m["irrelevant"]: other.append(f"irrelevant={m['irrelevant']}")
            if m["wasted"]: other.append(f"wasted={m['wasted']}")
            if m["dropped"]: other.append(f"dropped_updates={m['dropped']}")
            if m["hit_max"]: other.append("CUT_BY_MAX_TURNS")
            if m["loop"]: other.append("LOOP")
            if m["crash"]: other.append(f"CRASH {m['crash']}")
            if p_is_minor(m): other.append(f"深夜NG={m['late_night_ng']}")
            print(f"{m['persona']:10}{m['turns']:>6} {str(m['reached']):>6} {m['final_seg']:>10}  {ph:22}  "
                  f"{m['empty']['HARD']} | {m['low']['HARD']} | {m['empty']['MID']} | {m['low']['MID']} | {' '.join(other)}")
    if "v3" in res:
        print("\n--- v3 review metrics ---")
        print(f"{'persona':10}{'turns':>6} {'rounds':>6} review@turn  unconfirmed(HARD/MID) | unconfirmed LOW-pri | conflicts on 1st card -> open at end")
        for m in res["v3"]:
            print(f"{m['persona']:10}{m['turns']:>6} {m['review_rounds']:>6} {str(m['review_turns']):12} {m['unconfirmed']} | {m['unconfirmed_low']} (empty LOW: {m['empty_low_pri']}) | {m['conflicts_on_first_card']} -> {m['conflicts_open_at_end']}")
    json.dump(res, open("results.json", "w"), ensure_ascii=False, indent=1, default=str)

def p_is_minor(m): return m["persona"] == "hs16"

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    main(list(MODES) if arg == "all" else [arg])
