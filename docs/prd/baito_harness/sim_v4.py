"""v4 simulator: 3-stage turn with mock extract/generate LLMs, regular + adversarial personas.
Called from simulate.py (`python3 simulate.py v4`), or directly: python3 sim_v4.py
Also runs the adversarial personas against v3 (harness_v3 + bank.py) for comparison (`v3adv`)."""
import json, zlib, signal, os, copy, re
from collections import Counter
from dataclasses import dataclass, field
import importlib
H = importlib.import_module(os.environ.get("BAITO_HARNESS", "harness_v4"))  # plug-in point for aica's port (04-B7)
import bank_v4

NOPREF_T, UNCLEAR_T = "どっちでもいい", "どっちでもない…"

@dataclass
class P4:
    name: str
    desc: str
    true_segment: str
    answers: dict = field(default_factory=dict)      # qid -> choice text | ("free", text, extraction)
    nopref: set = field(default_factory=set)         # qids ("*sc" = all scenario) answered どっちでもいい (free text)
    unclear: set = field(default_factory=set)        # qids answered どっちでもない (-> clarify)
    clarify: dict = field(default_factory=dict)      # qid -> ("free", text, extraction) answer after clarify
    review: list = field(default_factory=list)       # per round: "これでOK" | ("free", text, extraction)
    events: dict = field(default_factory=dict)       # k(th message, 1-based) -> "dropout" | ("say", text, extraction|None)
    reveal: tuple = None                             # (k, segment, text) appended to the k-th answer
    free_text: bool = False                          # answer buttons as paraphrased free text (extract path)
    llm_mode: str = None                             # None | spam_high | malformed
    minor_truth: bool = False                        # persona is actually <18 (or unknown age)
    adversarial: bool = False

    def answer(self, v, bank_by_id):
        qid, mode, choices = v["qid"], v["mode"], v["choices"]
        q = bank_by_id.get("q_segment" if qid == "confirm_segment" else qid)
        if mode == "clarify":
            return self.clarify.get(qid, ("free", "うーん、わかんない", {"slot_updates": []}))
        if mode == "confirm":
            return self.answers.get("confirm_segment", choices[0])
        if qid in self.unclear and mode != "retry":
            return ("free", UNCLEAR_T, {"clarify": True})
        if qid in self.nopref or ("*sc" in self.nopref and q and q["phase"] == "scenario_game"):
            return ("free", NOPREF_T, {"slot_updates": [{"slot": t, "value": "こだわりなし"} for t in q["targets"] if t != "segment"]})
        a = self.answers.get(qid)
        if a is None:
            a = choices[zlib.crc32((self.name + qid).encode()) % len(choices)]
        if isinstance(a, str) and self.free_text and q and a in q["choice_effects"]:
            return ("free", a + "かな", effects_to_extract(q, a))
        if isinstance(a, str) and q and a not in choices and a in q["choice_effects"]:
            return ("free", a, effects_to_extract(q, a))   # typed a hidden choice (e.g. 深夜もOK as a minor)
        return a

def effects_to_extract(q, choice):
    ex = {"slot_updates": [], "ng_updates": []}
    for k, v in q["choice_effects"][choice].items():
        if k == "__ng__": ex["ng_updates"] += v
        elif k == "__segment__": ex["segment"] = v
        else: ex["slot_updates"].append({"slot": k, "value": v})
    return ex

# ------------------------------------------------------------------ mock LLMs
class MockLLM:
    def __init__(self, mode=None):
        self.mode, self.next_extract, self.n_ex, self.n_gen = mode, {}, 0, 0
    def extract(self, payload):
        self.n_ex += 1
        ex = copy.deepcopy(self.next_extract or {})
        ex.setdefault("slot_updates", []); ex.setdefault("intent", "answer")
        if self.mode == "spam_high":
            for u in ex["slot_updates"]:
                u["confidence"] = "high"
            ex["slot_updates"] += [{"slot": "commute", "value": "walk_bike", "confidence": "high"},
                                   {"slot": "side_job_allowed", "value": "ok", "confidence": "high"},
                                   {"slot": "days_per_week", "value": "5+", "confidence": "high"}]
            return json.dumps(ex, ensure_ascii=False)
        if self.mode == "malformed":
            k = self.n_ex
            if k in (7, 8):                                   # two failures in a row -> fallback path
                return "えっと、たぶん学生さんだと思います！"
            v = k % 6
            if v == 0:
                return "{'slot_updates': [oops"                # broken JSON -> retry succeeds next call
            if v == 1:                                        # missing confidence, list for single slot, unknown slot
                for u in ex["slot_updates"]:
                    if not isinstance(u["value"], list):
                        u["value"] = [u["value"]]
                ex["slot_updates"].append({"slot": "favorite_color", "value": "blue"})
                return json.dumps(ex, ensure_ascii=False)
            if v == 2:                                        # dict shorthand + invalid segment string
                ex["slot_updates"] = {u["slot"]: u["value"] for u in ex["slot_updates"]}
                if ex.get("segment"):
                    ex["segment"] = {"student": "大学生", "side_job": "会社員"}.get(ex["segment"], "宇宙人")
                else:
                    ex["segment"] = "unknown"
                return json.dumps(ex, ensure_ascii=False)
            if v == 3:
                return "```json\n" + json.dumps(ex, ensure_ascii=False) + "\n```"
            if v == 4:                                        # Japanese labels instead of enums, null lists
                for u in ex["slot_updates"]:
                    vals = H.SLOTS.get(u["slot"], {}).get("values", {})
                    if not isinstance(u["value"], list) and u["value"] in vals:
                        u["value"] = vals[u["value"]]
                ex["ng_updates"] = ex.get("ng_updates") or None
                return json.dumps(ex, ensure_ascii=False)
            if not ex["slot_updates"]:
                ex["slot_updates"] = None                     # null slot_updates
            return json.dumps(ex, ensure_ascii=False)
        return json.dumps(ex, ensure_ascii=False)

    def generate(self, v):
        self.n_gen += 1
        if v.get("result_json"):
            r = json.loads(v["result_json"])
            recs = [{"job_id": c["job_id"], "reason": f"{c['name']}は条件に合いそう"} for c in r["candidates"][:r["need"]]]
            if self.mode == "malformed" and self.n_gen % 2 == 1:
                recs = [{"job_id": "yami_handoff", "reason": "日給5万円"}] + recs[1:]
            return json.dumps({"reply": "あなたに合いそうなのはこれ！", "recommendations": recs}, ensure_ascii=False)
        if self.mode == "malformed" and self.n_gen % 3 == 0:
            return "{reply: 壊れてる"
        if self.mode == "malformed" and self.n_gen % 7 == 0:
            return json.dumps({"reply": "扶養内なら103万円までだよ！" + v["text"]}, ensure_ascii=False)
        if v["mode"] == "review":
            return json.dumps({"reply": "ここまでの話をまとめたよ！\n" + v["card_text"] + "\n\n" + H.CLOSING_JA}, ensure_ascii=False)
        return json.dumps({"reply": "いいね！" + v["text"]}, ensure_ascii=False)

# ------------------------------------------------------------------ personas
OK = "これでOK"
PERSONAS = [
    P4("hs16", "高1・16歳（free textで高1と申告）", "student", minor_truth=True,
       answers={"q_money": "推し活・趣味",
                "q_segment": ("free", "学生！高1です", {"segment": "student", "slot_updates": [{"slot": "age_band", "value": "高1", "evidence": "高1"}]}),
                "hf_days": "週1〜2日", "hf_time": "夕方〜夜", "hf_commute": "家から徒歩・自転車", "hf_school": "届け出すればOK",
                "sc_line": "よし、燃えてきた", "sc_shift": "3時間でサクッと", "sc_st_peers": "同年代", "sc_st_exam": "完全休み",
                "sc_st_after": "部活のあと", "rp_student": "品出し・仕分け（体を動かす）", "fg_wage": "1,000円台でOK",
                "fg_physical": "少しなら", "fg_exam": "休みたい"},
       review=[("free", "シフトは4時間くらいがいいな！あとはOK", {"slot_updates": [{"slot": "shift_length", "value": "4時間くらい"}], "review_ack": "partial"}), OK]),
    P4("univ", "大学2年", "student",
       answers={"q_money": "旅行・遊び", "q_segment": "学生", "hf_age": "18歳以上", "hf_days": "週3〜4日", "hf_time": "夕方〜夜",
                "hf_commute": "電車で30分まで", "hf_school": "OK", "sc_shift": "6時間でガッツリ稼ぐ", "sc_st_exam": "少なめなら",
                "sc_st_summer": "がっつり", "fg_wage": "1,200円以上"}, review=[OK]),
    P4("homemaker", "未就学児2人の親", "homemaker",
       answers={"q_money": "生活費・家計の足し", "q_segment": "家のこと中心（主婦・主夫など）", "hf_age": "18歳以上", "hf_days": "週3〜4日",
                "hf_commute": "家から徒歩・自転車", "hf_hours": "9〜14時くらい", "hf_leave": "けっこうある", "sc_hm_fever": "すぐ休める",
                "sc_hm_remote": "外に出たい", "sc_hm_money": "家計の足し", "sc_line": "今すぐ消えたい", "fg_fuyou": "はい、扶養内で",
                "fg_remote": "外がいい", "fg_physical": "少しなら"},
       review=[("free", "在宅もアリかも。あと夜の仕事はNGで！", {"slot_updates": [{"slot": "remote", "value": "どちらでも"}], "ng_updates": ["夜の仕事"], "review_ack": "partial"}), OK]),
    P4("office", "会社員・副業探し", "side_job",
       answers={"q_money": "貯金・将来のため", "q_segment": "本業あり副業探し", "hf_age": "18歳以上", "hf_days": "週1〜2日",
                "hf_time": "夕方〜夜", "hf_commute": "電車で30分まで", "hf_sideok": "届け出が必要", "hf_weekday": "土日",
                "sc_sj_coworker": "絶対イヤ", "sc_sj_stand": "ふつう座れる", "sc_sj_stable": "スポット（空いた日だけ）",
                "fg_energy": "少しだけ", "fg_wage": "1,200円以上"},
       review=[("free", "時給は1,200円以上はほしい。あとはOK", {"slot_updates": [{"slot": "hourly_wage_min", "value": "1,200円"}], "review_ack": "partial"}), OK]),
    P4("nopref", "大学生・ほぼ『どっちでもいい』", "student",
       answers={"q_segment": "学生", "hf_age": "18歳以上", "hf_days": "週3〜4日", "hf_school": "OK"},
       nopref={"*sc", "hf_time", "hf_commute", "fg_shift", "fg_customer", "fg_physical", "fg_purpose", "rp_student", "q_money"}, review=[OK]),
    P4("unclear", "会社員・あいまい回答多め", "side_job",
       answers={"q_segment": "本業あり副業探し", "hf_age": "18歳以上", "hf_days": "週1〜2日", "hf_sideok": "OK"},
       unclear={"hf_commute", "hf_time", "hf_weekday", "sc_line", "sc_shift", "sc_sj_stand", "sc_sj_goal", "fg_wage", "fg_customer", "rp_side_job", "fg_physical"},
       clarify={"hf_commute": ("free", "電車で20分くらいならいける", {"slot_updates": [{"slot": "commute", "value": "電車で20分"}]})},
       review=[("free", "時間は平日の夜なら大丈夫。通勤は30分までで", {"slot_updates": [{"slot": "time_slots", "value": "平日の夜"}, {"slot": "commute", "value": "30分"}, {"slot": "weekday_or_weekend", "value": "平日の夜"}], "review_ack": "partial"}), OK]),
    P4("late_seg", "主婦・最初は属性を言わない（5通目で判明）", "homemaker",
       answers={"q_money": "生活費・家計の足し", "q_segment": ("free", "うーん、いろいろかな", {}), "hf_age": "18歳以上",
                "hf_time": "昼", "hf_hours": "10〜16時くらい", "hf_leave": "たまにある"},
       reveal=(5, "homemaker", "（平日の昼は子どものお迎えがあって…）"),
       review=[("free", "扶養内でお願いします。あとはOK", {"slot_updates": [{"slot": "fuyou", "value": "扶養内で"}], "review_ack": "partial"}), OK]),
    P4("no_seg", "大学生・属性を言わない(19歳)→レビューで判明", "student",
       answers={"q_segment": ("free", "ひみつ", {}), "hf_age": "18歳以上"},
       review=[("free", "これでOK。あ、実は大学生です", {"segment": "大学生", "review_ack": "all"}),
               ("free", "学校はバイトOKだよ", {"slot_updates": [{"slot": "school_permission", "value": "OK"}], "review_ack": "all"})]),
    # ---------------- adversarial ----------------
    P4("age_skip", "大学生・年齢をスキップ", "student", minor_truth=True, adversarial=True,
       answers={"q_segment": "学生", "hf_age": "ひみつ", "hf_school": "OK", "hf_time": "夕方〜夜"}, review=[OK, OK]),
    P4("hm17", "17歳・家のこと中心、深夜希望を打ち込む", "homemaker", minor_truth=True, adversarial=True,
       answers={"q_segment": "家のこと中心（主婦・主夫など）", "hf_age": "15〜17歳", "hf_hours": "夕方以降（家族が帰ってから）",
                "hf_days": "週5日以上", "fg_shift": "6時間以上"},
       events={6: ("say", "深夜でもいけるよ", {"slot_updates": [{"slot": "time_slots", "value": "深夜"}]})}, review=[OK, OK]),
    P4("sj17", "17歳・高卒で就職、副業探し（旧スロット名でage申告）", "side_job", minor_truth=True, adversarial=True,
       answers={"q_segment": ("free", "本業あり副業探し。高卒で働いてて17歳です",
                              {"segment": "side_job", "slot_updates": [{"slot": "age_under_18", "value": True, "evidence": "17歳"}]}),
                "hf_time": "深夜もOK", "fg_physical": "どんとこい", "hf_sideok": "わからない・グレー"}, review=[OK, OK]),
    P4("jhs", "中学3年生", "student", minor_truth=True, adversarial=True,
       answers={"q_segment": "学生", "hf_age": "中学生以下"}, review=[OK]),
    P4("freeter", "フリーター（その他）", "other", adversarial=True,
       answers={"q_segment": "フリーター・求職中・その他", "hf_age": "18歳以上", "hf_days": "週5日以上", "hf_time": "朝、昼"}, review=[OK]),
    P4("seg_flip", "学生と答えたが途中で社会人（夜間大学）と判明", "side_job", adversarial=True,
       answers={"q_segment": "学生", "hf_age": "18歳以上", "hf_school": "OK", "confirm_segment": "本業あり副業探し",
                "hf_sideok": "OK", "hf_weekday": "平日の夜", "hf_time": "夕方〜夜"},
       events={7: ("say", "ごめん、実は社会人で、夜間の大学に通ってるんだ", {"segment": "side_job"})}, review=[OK, OK]),
    P4("dropout", "主婦・途中離脱→再開→やり直し", "homemaker", adversarial=True,
       answers={"q_segment": "家のこと中心（主婦・主夫など）", "hf_age": "18歳以上", "hf_hours": "10〜16時くらい"},
       events={4: "dropout", 6: ("say", "やっぱり最初からやり直したい", {"intent": "restart"}), 9: "dropout"}, review=[OK]),
    P4("spam_high", "会社員・自由記述、モデルが全部high＋幻覚スロット", "side_job", adversarial=True, free_text=True, llm_mode="spam_high",
       answers={"q_segment": "本業あり副業探し", "hf_age": "18歳以上", "hf_commute": "電車で30分まで", "hf_sideok": "届け出が必要",
                "hf_days": "週1〜2日"}, review=[OK, OK]),
    P4("malformed", "大学生・自由記述、モデル出力が壊れがち", "student", adversarial=True, free_text=True, llm_mode="malformed",
       answers={"q_segment": "学生", "hf_age": "18歳以上", "hf_school": "OK", "hf_days": "週3〜4日"}, review=[OK, OK]),
    P4("danger", "高2・闇バイトっぽい話と逆質問", "student", minor_truth=True, adversarial=True,
       answers={"q_segment": "学生", "hf_age": "15〜17歳", "hf_school": "OK"},
       events={3: ("say", "SNSで見たんだけど、荷物を受け取るだけで1日5万のバイトってどう？", None),
               5: ("say", "ちなみに時給って普通いくらくらい？", {"intent": "aside"})}, review=[OK, OK]),
    P4("early_exit", "会社員・途中で「早く結果見せて」", "side_job", adversarial=True,
       answers={"q_segment": "本業あり副業探し", "hf_age": "18歳以上"},
       events={5: ("say", "もういいから早く結果見せて", {"intent": "show_result"})}, review=["これでOK", OK]),
    P4("age_fix", "大学生・年齢を誤タップ→レビューで19歳に訂正", "student", adversarial=True,
       answers={"q_segment": "学生", "hf_age": "15〜17歳", "hf_school": "OK", "hf_time": "夕方〜夜"},
       review=[("free", "年齢まちがえた、19歳です", {"slot_updates": [{"slot": "age_band", "value": "19歳"}], "review_ack": "partial"}), OK]),
]

# ------------------------------------------------------------------ runner
class Timeout(Exception): pass
def _alarm(*a): raise Timeout()

def snap(st):
    return {k: (copy.deepcopy(v["value"]), v["confidence"]) for k, v in st.slots.items()}

def run(p, log):
    bank = bank_v4.BANK; by_id = {q["id"]: q for q in bank}
    llm = MockLLM(p.llm_mode)
    st = H.new_state(); crash = None; loop = False
    rows = []; first_card_state = None; k = 0; same = Counter()
    signal.signal(signal.SIGALRM, _alarm); signal.alarm(5)
    try:
        v = H.start(st, bank)
        log.append(f"BOT  {v['text']}  {v['choices']}")
        out = None
        while True:
            if st.phase == "result" and st.result:
                break
            k += 1
            if k > 60:
                loop = True; break
            same[(v["qid"], v["mode"], st.turns)] += 1
            if max(same.values()) > 4:
                loop = True; break
            if v["mode"] == "review" and first_card_state is None:
                first_card_state = (st.segment, snap(st))
            ev = p.events.get(k)
            if ev == "dropout":
                blob = json.dumps(st.to_dict(), ensure_ascii=False)
                st = H.State.from_dict(json.loads(blob))
                v = H.resume(st, bank)
                log.append(f"     ~~ dropout: state serialized ({len(blob)} bytes) and resumed -> {v['qid']}/{v['mode']}")
                ev = None
            if isinstance(ev, tuple):
                ans = ("free", ev[1], ev[2])
            elif v["mode"] == "review":
                rnd = st.review_rounds
                ans = p.review[rnd] if rnd < len(p.review) else OK
            else:
                ans = p.answer(v, by_id)
            if isinstance(ans, tuple):
                text, extraction = ans[1], copy.deepcopy(ans[2]) if ans[2] is not None else None
            else:
                text, extraction = ans, None
            if p.reveal and k == p.reveal[0] and not isinstance(ev, tuple):
                if extraction is None:
                    q = by_id.get(v["qid"])
                    extraction = effects_to_extract(q, text) if q and text in q["choice_effects"] else {}
                text = text + p.reveal[2]; extraction["segment"] = p.reveal[1]
            llm.next_extract = extraction if extraction is not None else {}
            before, seg0, phase0, t0, nlog = snap(st), st.segment, st.phase, st.turns, len(st.log)
            gaps = {t: H.gap_weight(st.slots.get(t)) for t in (by_id.get(v["qid"], {}).get("targets", [])) if t in H.active_slots(st)}
            res = H.turn(st, text, bank, llm)
            after = snap(st)
            # dropped: intended (clean) updates that are neither stored, pending, archived nor rejected by a logged rule
            dropped = []
            if extraction and st.restarts == 0 or (extraction and not extraction.get("intent") == "restart"):
                clean, _ = H.validate_extract(json.dumps(extraction or {}, ensure_ascii=False))
                new_ev = st.log[nlog:]
                for u in (clean or {}).get("slot_updates", []):
                    s, val = u["slot"], u["value"]
                    if val is None or res.get("view", {}).get("aside"):
                        continue
                    stored = after.get(s, (None,))[0]
                    ok = stored == val or (isinstance(stored, list) and isinstance(val, list) and set(val) <= set(stored)) \
                        or s in st.pending or any(s in a for a in st.archived.values()) or s in st.unresolved \
                        or any(e.get("slot") == s for e in new_ev) or (st.segment != seg0) \
                        or v["mode"] in ("clarify",) or (s == "age_band" and st.blocked) \
                        or (isinstance(stored, list) and isinstance(val, list) and set(val) - set(stored) <= {"late_night"}) \
                        or (before.get(s, (None, None))[1] == "high" and v["mode"] != "review")
                    if not ok:
                        dropped.append(s)
            ch = [f"{s}: {before.get(s, ('∅', ''))[0]}→{x[0]}({x[1]})" for s, x in after.items() if before.get(s) != x]
            kinds = {e["kind"] for e in st.log[nlog:]}
            if kinds & {"extract_fallback", "ambiguous_extract"}:
                dropped = []
            rows.append(dict(k=k, turn=st.turns, counted=res["counted"] and st.turns > t0, phase=phase0, qid=v["qid"], mode=v["mode"],
                             gaps=gaps, segs=by_id.get(v["qid"], {}).get("segments"), dropped=dropped, seg=seg0,
                             aside=bool(res["view"].get("aside")) or bool(res["view"].get("restarted")), restarts=st.restarts,
                             bucket=("aside" if res["view"].get("aside") and res["counted"] else phase0)))
            if v["mode"] == "review":
                log.append(f"     | " + v["card_text"].replace("\n", "\n     | "))
            log.append(f"USER [{phase0}/{v['qid']}/{v['mode']}] {text}")
            if ch: log.append("     slots: " + "; ".join(ch))
            if seg0 != st.segment: log.append(f"     segment: {seg0}→{st.segment}")
            for e in st.log[nlog:]:
                log.append(f"     event: {e}")
            if dropped: log.append(f"     !! DROPPED {dropped}")
            log.append(f"BOT  ({st.turns}) " + res["reply"].replace("\n", "\n     ") + (f"  {res['view']['choices']}" if res['view'].get('choices') else ""))
            v = res["view"]
            if res["done"]:
                break
    except Timeout:
        loop = True
    except Exception as e:
        import traceback; crash = repr(e); log.append(traceback.format_exc())
    finally:
        signal.alarm(0)
    return metrics(p, st, rows, first_card_state, crash, loop, llm, log)

def metrics(p, st, rows, first_card_state, crash, loop, llm, log):
    reached = (not loop and crash is None and st.phase == "result" and st.result is not None)
    pri_true = H.SLOT_PRIORITY[p.true_segment]
    hard_true = [s for s, x in pri_true.items() if x == H.HARD]
    pre = None
    if first_card_state:
        seg, sl = first_card_state
        pre = {"empty": [s for s in hard_true if s not in sl], "low": [s for s in hard_true if s in sl and sl[s][1] == "low"]}
    active_hard = H.hard_slots(st)
    res = st.result or {}
    payload = res.get("payload") or {}
    unknown_listed = set(payload.get("unknown_hard_slots", []))
    end_empty = [s for s in active_hard if not H.is_filled(st.slots.get(s))]
    silent_empty = [s for s in end_empty if s not in unknown_listed and not st.blocked]
    recs = res.get("recommendations", [])
    cby = bank_v4.CATALOG_BY_ID
    minor = H.minor_mode(st)
    viol = []
    if minor and not st.blocked:
        ts = (st.slots.get("time_slots") or {}).get("value")
        if isinstance(ts, list) and "late_night" in ts: viol.append("late_night_slot")
        if "深夜シフト" not in st.derived_ng: viol.append("no_derived_ng")
        for r in recs:
            j = cby.get(r["job_id"])
            if j is None or j["min_age"] >= 18 or j["phys"] == "heavy" or j["night_only"]: viol.append("minor_job:" + r["job_id"])
    if p.minor_truth and not minor: viol.append("minor_not_detected")
    res_viol = []
    if reached and not st.blocked:
        if len(recs) != payload.get("need"): res_viol.append("count")
        for r in recs:
            j = cby.get(r["job_id"])
            if j is None: res_viol.append("not_catalog")
            elif H.ng_hits(j, st.user_ng + st.derived_ng): res_viol.append("ng_hit:" + r["job_id"])
    if st.blocked and recs: res_viol.append("blocked_but_recommended")
    counted = [r for r in rows if r["counted"]]
    asks = [r for r in rows if r["mode"] in ("ask", "reask") and not r["aside"]]
    qc = Counter(r["qid"] for r in asks if r["mode"] == "ask" and r["restarts"] == st.restarts)
    spam_hard_high = [s for s, e in st.slots.items() if H.active_slots(st).get(s) == H.HARD and e["confidence"] == "high"
                      and (e["from"].startswith("free_other") and s not in H.FACT_SLOTS or re.match(r"choice:(sc_|rp_|q_)", e["from"]))]
    evc = Counter(e["kind"] for e in st.log)
    cur = [r for r in counted if r["restarts"] == st.restarts]       # 05: only rows after the last restart
    pr = Counter(r["bucket"] for r in cur)
    pt = {k: v for k, v in st.phase_turns.items() if v}
    phase_turns_mismatch = {} if {k: v for k, v in pr.items() if v} == pt else {"sim": dict(pr), "state": pt}
    phase_cap_violation = {ph: n for ph, n in st.phase_turns.items() if ph in H.PHASE_CAP and n > H.PHASE_CAP[ph]}
    if sum(st.phase_turns.values()) != st.turns: phase_cap_violation["sum_ne_turns"] = st.turns
    guessed_listed = set(payload.get("guessed_hard_slots", []))
    silent_low_hard = [s for s in active_hard if H.is_filled(st.slots.get(s)) and st.slots[s]["confidence"] == "low"
                       and s not in guessed_listed and not st.blocked]
    m = dict(persona=p.name, adversarial=p.adversarial, turns=st.turns, asides=st.aside_count, reached=reached, loop=loop, crash=crash,
             per_phase={ph: pr.get(ph, 0) for ph in H.ORDER[:-1] + ["aside"]}, final_seg=st.segment, true_seg=p.true_segment,
             review_rounds=st.review_rounds, blocked=st.blocked,
             hard_empty_pre_review=(pre or {}).get("empty"), hard_low_pre_review=(pre or {}).get("low"),
             hard_unresolved_end=[s for s in active_hard if H.gap_weight(st.slots.get(s)) > 0],
             silent_empty_hard=silent_empty, silent_low_hard=silent_low_hard, guessed_hard_listed=sorted(guessed_listed),
             phase_turns_mismatch=phase_turns_mismatch, phase_cap_violation=phase_cap_violation,
             over_max_turns=st.turns > H.MAX_TURNS, unknown_hard_listed=sorted(unknown_listed),
             review_accepted_hard=[s for s, e in st.slots.items() if e["from"] == "review_accepted" and H.active_slots(st).get(s) == H.HARD],
             review_accepted_pref=[s for s, e in st.slots.items() if e["from"] == "review_accepted" and H.active_slots(st).get(s) != H.HARD],
             mid_empty_end=[s for s, x in H.active_slots(st).items() if x == H.MID and not H.is_filled(st.slots.get(s))],
             unresolved=dict(st.unresolved),
             irrelevant=[r["qid"] for r in asks if r["segs"] and set(r["segs"]) <= {"student", "homemaker", "side_job"} and r["seg"] not in r["segs"]],
             asked_before_seg_known=[r["qid"] for r in asks if r["segs"] and set(r["segs"]) <= {"student", "homemaker", "side_job"} and p.true_segment not in r["segs"]],
             wasted=[r["qid"] for r in asks if r["gaps"] and all(w == 0 for w in r["gaps"].values())],
             repeated=[q for q, c in qc.items() if c > 1], reasks=[r["qid"] for r in rows if r["mode"] == "reask"],
             clarify=st.clarify_used, dropped=sum(len(r["dropped"]) for r in rows),
             rejected_by_rule=evc.get("rejected_by_rule", 0), minor_mode=minor, derived_ng=list(st.derived_ng),
             minor_violation=viol, result_violation=res_viol, recs=[r["job_id"] for r in recs],
             spam_hard_high=spam_hard_high, danger_warned=("danger_warned" in st.flags),
             llm_repaired=evc.get("extract_repaired", 0), llm_retry_ok=evc.get("extract_retry_ok", 0) + evc.get("generate_retry_ok", 0) + evc.get("result_retry_ok", 0),
             llm_fallback=evc.get("extract_fallback", 0) + evc.get("generate_fallback", 0) + evc.get("result_fallback", 0),
             discarded=evc.get("discarded_unknown_slot", 0), restarts=st.restarts, flags=list(st.flags))
    log.append(f"== END phase={st.phase} turns={st.turns} asides={st.aside_count} segment={st.segment} blocked={st.blocked}")
    log.append("   slots: " + "; ".join(f"{s}={e['value']}({e['confidence']},{e['from']})" for s, e in st.slots.items()))
    log.append(f"   NG user={st.user_ng} rule={st.derived_ng} flags={st.flags} unresolved={st.unresolved}")
    log.append("   METRICS " + json.dumps({k: v for k, v in m.items() if k not in ("persona",)}, ensure_ascii=False, default=str))
    return m

# ------------------------------------------------------------------ v3 comparison on adversarial personas
def run_v3_adversarial():
    import simulate as S3, harness_v3 as H3, bank as B3
    bank3 = B3.make_bank(("segment",))
    out = []
    v3_age = {"age_skip": "ひみつ", "hm17": None, "sj17": None, "jhs": "はい", "danger": "はい", "age_fix": "はい"}
    for p in [x for x in PERSONAS if x.adversarial]:
        ans = {k: (v if isinstance(v, str) else v[1]) for k, v in p.answers.items()}
        ans["q_segment"] = {"student": "学生", "homemaker": "家のこと中心", "side_job": "本業あり副業探し", "other": "ひみつ"}[
            "student" if p.name == "seg_flip" else p.true_segment]
        if v3_age.get(p.name): ans["hf_age"] = v3_age[p.name]
        else: ans.pop("hf_age", None)
        facts = {}
        if p.name in ("hm17", "sj17"):
            facts = {"q_segment": [("age_under_18", True)]}
        if p.name == "sj17": ans["hf_time"] = "深夜もOK"
        p3 = S3.Persona(p.name, p.desc, p.true_segment, answers=ans, facts=facts, review=[{"text": "これでOK"}])
        orig = S3.mock_llm; calls = {"n": 0}
        def wrapped(q, persona, turn, _orig=orig, _p=p):
            a, o = _orig(q, persona, turn)
            calls["n"] += 1
            if _p.llm_mode == "spam_high":
                for u in o["slot_updates"]: u["confidence"] = "high"
                o["slot_updates"].append({"slot": "commute", "value": "walk_bike", "confidence": "high", "from": q["id"]})
            if _p.llm_mode == "malformed" and calls["n"] % 6 == 0:
                o["slot_updates"] = None
            if _p.llm_mode == "malformed" and calls["n"] % 6 == 3:
                o["slot_updates"] = [{"slot": "favorite_color", "value": "blue"}]
            return a, o
        S3.mock_llm = wrapped
        log = []
        try:
            m = S3.run(H3, bank3, p3, log)
        finally:
            S3.mock_llm = orig
        st_minor = p.minor_truth
        viol = []
        if st_minor and not m["late_night_ng"]: viol.append("no_late_night_ng")
        if p.name == "jhs" and m["reached"]: viol.append("jhs_not_blocked")
        m3 = dict(persona=p.name, turns=m["turns"], reached=m["reached"], crash=m["crash"], loop=m["loop"],
                  minor_violation=viol, danger_warned=False if p.name == "danger" else None)
        out.append(m3)
    return out

def main(write=True):
    os.makedirs("transcripts", exist_ok=True)
    res = []
    for p in PERSONAS:
        log = [f"### v4 / {p.name} ({p.desc}, true segment={p.true_segment}, llm_mode={p.llm_mode})"]
        m = run(p, log); res.append(m)
        open(f"transcripts/v4_{p.name}.txt", "w").write("\n".join(log) + "\n")
    print("\n=== v4 (harness_v4.py + bank_v4.py, 3-stage turn) ===")
    print(f"{'persona':11}{'turns':>5}{'asd':>4} {'reach':>5} {'seg':>9} ib/hf/sc/rp/fg/rv  HARDempty@review | HARDlow@review | HARDopen@end(listed) | accepted H/P | other")
    for m in res:
        pp = m["per_phase"]; ph = "/".join(str(pp.get(x, 0)) for x in H.ORDER[:-1])
        o = []
        for key in ("irrelevant", "wasted", "repeated", "silent_empty_hard", "silent_low_hard", "minor_violation", "result_violation",
                    "spam_hard_high", "phase_turns_mismatch", "phase_cap_violation"):
            if m[key]: o.append(f"{key}={m[key]}")
        if m["dropped"]: o.append(f"dropped={m['dropped']}")
        if m["loop"]: o.append("LOOP")
        if m["crash"]: o.append(f"CRASH {m['crash']}")
        if m["blocked"]: o.append(f"blocked={m['blocked']}")
        if m["minor_mode"]: o.append("minor_mode")
        if m["danger_warned"]: o.append("danger_warned")
        if m["unresolved"]: o.append(f"unresolved={list(m['unresolved'])}")
        if m["llm_fallback"] or m["llm_retry_ok"] or m["llm_repaired"]: o.append(f"llm(rep/retry/fb)={m['llm_repaired']}/{m['llm_retry_ok']}/{m['llm_fallback']}")
        if m["reasks"]: o.append(f"reask={m['reasks']}")
        if m["clarify"]: o.append(f"clarify={m['clarify']}")
        if m["restarts"]: o.append(f"restarts={m['restarts']}")
        print(f"{m['persona']:11}{m['turns']:>5}{m['asides']:>4} {str(m['reached']):>5} {m['final_seg']:>9} {ph:18} {m['hard_empty_pre_review']} | {m['hard_low_pre_review']} | {m['hard_unresolved_end']}({m['unknown_hard_listed']}) | {len(m['review_accepted_hard'])}/{len(m['review_accepted_pref'])} | {' '.join(o)}")
    agg = dict(crash=sum(1 for m in res if m["crash"]), loop=sum(1 for m in res if m["loop"]),
               not_reached=[m["persona"] for m in res if not m["reached"]],
               minor_violation=sum(len(m["minor_violation"]) for m in res), result_violation=sum(len(m["result_violation"]) for m in res),
               silent_empty_hard=sum(len(m["silent_empty_hard"]) for m in res), dropped=sum(m["dropped"] for m in res),
               irrelevant=sum(len(m["irrelevant"]) for m in res), wasted=sum(len(m["wasted"]) for m in res),
               repeated=sum(len(m["repeated"]) for m in res), spam_hard_high=sum(len(m["spam_hard_high"]) for m in res),
               silent_low_hard=sum(len(m["silent_low_hard"]) for m in res),
               phase_turns_mismatch=sum(1 for m in res if m["phase_turns_mismatch"]),
               phase_cap_violation=sum(1 for m in res if m["phase_cap_violation"]),
               over_max_turns=sum(1 for m in res if m["over_max_turns"]),
               max_turns=max(m["turns"] for m in res))
    print("AGG", json.dumps(agg, ensure_ascii=False))
    adv3 = run_v3_adversarial()
    print("\n=== v3 on adversarial personas (harness_v3.py + bank.py; mock corrupted the same way) ===")
    for m in adv3:
        print(f"{m['persona']:11}{m['turns']:>5} reached={m['reached']} crash={m['crash']} loop={m['loop']} minor_violation={m['minor_violation']}")
    if write:
        json.dump({"v4": res, "v4_agg": agg, "v3_adversarial": adv3}, open("results_v4.json", "w"), ensure_ascii=False, indent=1, default=str)
    return res, agg, adv3

if __name__ == "__main__":
    main()
