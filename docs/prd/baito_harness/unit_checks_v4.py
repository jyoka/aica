"""Deterministic unit checks for harness_v4 / bank_v4 / catalog_v4.  python3 unit_checks_v4.py  (exit 1 on any failure)
A port is tested with  BAITO_HARNESS=<module> python3 unit_checks_v4.py ; the module must expose PRD 付録C.
Section numbers (# ---- N.) are referenced by PRD §13-1 (done definitions)."""
import json, sys, copy, os, re, importlib
H = importlib.import_module(os.environ.get("BAITO_HARNESS", "harness_v4"))
import bank_v4 as B
import catalog_v4 as C

FAILS, N = [], 0
def check(name, cond, info=""):
    global N; N += 1
    if not cond:
        FAILS.append(f"{name} {info}")
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {info}"))

class LLM:
    """extract returns the queued dict (or raw string); generate echoes the view."""
    def __init__(self): self.q = []; self.gen = []
    def extract(self, p): return self.q.pop(0) if self.q else json.dumps({"slot_updates": []})
    def generate(self, v):
        if self.gen: return self.gen.pop(0)
        if v.get("result_json"):
            r = json.loads(v["result_json"])
            return json.dumps({"reply": "どうぞ", "recommendations": [{"job_id": c["job_id"], "reason": "合いそう"} for c in r["candidates"][:r["need"]]]}, ensure_ascii=False)
        if v["mode"] == "review":
            return json.dumps({"reply": v["card_text"] + "\n\n" + H.CLOSING_JA}, ensure_ascii=False)
        return json.dumps({"reply": v["text"]}, ensure_ascii=False)

BANK = B.BANK; QM = {q["id"]: q for q in BANK}
def fresh():
    st = H.new_state(); H.start(st, BANK); return st
def goto(st, qid, mode="ask"):
    q = QM[qid]; st.phase = q["phase"]; st.current = {"qid": qid, "mode": mode, "choices": H.visible_choices(q, st)}

# ---- 1. bank integrity: every choice effect is a valid enum (or segment/NG), no HARD/fact writes from scenario
for q in BANK:
    for c, eff in q["choice_effects"].items():
        for k, v in eff.items():
            if k == "__ng__":
                ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
            elif k == "__segment__":
                ok = v in H.SEGMENTS
            else:
                ok = k in H.SLOTS and H.normalize_value(k, v) is not None
            check(f"bank_enum[{q['id']}:{c}:{k}]", ok, repr(v)) if not ok else None
    if q["phase"] in ("scenario_game", "reverse_pick"):
        bad = [k for eff in q["choice_effects"].values() for k in eff if k in H.SLOTS and (H.SLOTS[k]["kind"] == "fact" or any(H.SLOT_PRIORITY[s].get(k) == H.HARD for s in H.SLOT_PRIORITY))]
        if bad: check(f"scenario_no_hard[{q['id']}]", False, bad)
check("bank_all_enums_valid", not [f for f in FAILS if f.startswith("bank_")])
check("catalog_ids_unique", len(B.CATALOG) == len(B.CATALOG_BY_ID))
check("no_moving_for_minor_reverse_pick", all("引越" not in c for c in QM["rp_student"]["choices"]))

# ---- 2. button determinism: same click -> same state, model not called
st = fresh(); goto(st, "hf_days"); llm = LLM(); llm.q = ["SHOULD NOT BE USED"]
H.turn(st, "週3〜4日", BANK, llm)
check("button_deterministic", st.slots["days_per_week"] == {"value": "3-4", "confidence": "high", "from": "choice:hf_days"}, st.slots.get("days_per_week"))
check("button_no_llm_call", llm.q == ["SHOULD NOT BE USED"])
st = fresh(); st.slots["age_band"] = {"value": "18_plus", "confidence": "high", "from": "x"}; goto(st, "hf_time")
H.turn(st, "朝、夕方〜夜", BANK, LLM())
check("multi_button", st.slots["time_slots"]["value"] == ["morning", "evening"], st.slots.get("time_slots"))

# ---- 3. validation edge cases
for raw, exp in [("garbage", None), ('```json\n{"slot_updates":[]}\n```', "ok"), ('{"slot_updates": null}', "ok"),
                 ('{"slot_updates": {"commute": "電車で20分"}}', "ok"), ('[1,2]', None)]:
    r, p = H.validate_extract(raw)
    check(f"validate_extract[{raw[:18]!r}]", (r is None) == (exp is None), p)
r, p = H.validate_extract(json.dumps({"slot_updates": [{"slot": "commute", "value": "電車で20分", "confidence": "high"}, {"slot": "favorite", "value": 1}], "segment": "大学生"}, ensure_ascii=False))
check("validate_normalizes_and_discards_unknown", r["slot_updates"] == [{"slot": "commute", "value": "train_30", "evidence": None}] and r["segment"] == "student", (r, p))
r, p = H.validate_extract(json.dumps({"slot_updates": [{"slot": "days_per_week", "value": ["週3", "週5"]}]}, ensure_ascii=False))
check("list_for_single_slot_first_item", r["slot_updates"][0]["value"] == "3-4" and "list_for_single:days_per_week" in p, (r, p))
r, _ = H.validate_extract('{"segment": "unknown"}')
check("segment_unknown_never_set", r["segment"] is None)
check("reply_money_rejected", H.validate_reply('{"reply":"扶養は103万円まで"}', {"mode": "ask"})[0] is None)
v = {"mode": "review", "card_text": "CARD"}
check("review_reply_must_keep_card", H.validate_reply('{"reply":"まとめ"}', v)[0] is None and H.validate_reply(json.dumps({"reply": "CARD\n" + H.CLOSING_JA}), v)[0])

# ---- 4. age parsing (aica task 258: typed ages are read only in a clear form -- the age buttons or digits + 歳/才.
#      A bare number ("16") and school words ("高1", "大学2年") are not read: the user stays a minor and is asked again)
for raw, exp in [("16", None), ("16歳", "15_17"), ("１７歳", "15_17"), ("高1", None), ("中3", "under_15"), ("中学生", "under_15"),
                 ("19歳", "18_plus"), ("大学2年", None), ("ひみつ", "declined"), ("14歳", "under_15"), (True, "under_18")]:
    got = H.normalize_value("age_band", raw)
    check(f"age_parse[{raw}]", got == exp, got)
check("age_nopref_is_declined_not_nopref", H.normalize_value("age_band", "こだわりなし") in (None, "declined"))
st = fresh(); H.apply_update(st, "age_band", H.NOPREF, "high", "t")
check("age_skip_is_declined", st.slots["age_band"]["value"] == "declined")

# ---- 5. minor rules: unknown age = minor, derived NG recompute on correction, late night rejected
st = fresh()
check("unknown_age_is_minor", H.minor_mode(st))
H.apply_update(st, "age_band", "15_17", "high", "t")
check("minor_derived_ng", st.derived_ng == H.DERIVED_NG_MINOR)
check("late_night_choice_hidden_for_minor", "深夜もOK" not in H.visible_choices(QM["hf_time"], st))
H.apply_update(st, "time_slots", ["evening", "late_night"], "high", "t")
check("late_night_rejected_for_minor", st.slots["time_slots"]["value"] == ["evening"])
H.apply_update(st, "physical", "heavy_ok", "high", "t")
check("heavy_rejected_for_minor", st.slots["physical"]["value"] != "heavy_ok")
H.apply_update(st, "age_band", "18_plus", "high", "review", review=True)
check("derived_ng_recomputed_on_age_fix", st.derived_ng == [] and not H.minor_mode(st))
st2 = fresh(); H.apply_update(st2, "age_band", "18_plus", "high", "t"); H.apply_update(st2, "time_slots", ["late_night"], "high", "t")
H.apply_update(st2, "age_band", "15_17", "high", "review", review=True)
check("late_night_stripped_when_age_becomes_minor", "time_slots" not in st2.slots and "深夜シフト" in st2.derived_ng)
st = fresh(); st.segment = "student"; H.apply_update(st, "age_band", "15_17", "high", "t")
jobs = [j["job_id"] for j in H.eligible_jobs(st, B.CATALOG)]
check("minor_catalog_filter", not any(B.CATALOG_BY_ID[j]["min_age"] >= 18 or B.CATALOG_BY_ID[j]["phys"] == "heavy" or B.CATALOG_BY_ID[j]["night_only"] for j in jobs) and "moving" not in jobs, jobs)
out = H.post_check_result(st, {}, {"recommendations": [{"job_id": "izakaya", "reason": "x"}, {"job_id": "nope", "reason": "x"}]}, B.CATALOG_BY_ID)
check("post_check_drops_unsafe", out["recommendations"] == [])

# ---- 6. under-15 blocked
st = fresh(); st.segment = "student"; goto(st, "hf_age"); res = H.turn(st, "中学生以下", BANK, LLM())
check("under15_blocked_result", res["done"] and st.blocked == "under_15" and st.result["recommendations"] == [], res["reply"][:80])
check("under15_notice_shown", H.NOTICE["under_15"] in res["reply"])
check("under15_no_minor_rules_dup", H.NOTICE["minor_rules"] not in res["reply"])
# 05-B10: the user's own age extracted with evidence blocks anywhere; raw-text 中学 only on age questions
st = fresh(); st.segment = "student"; goto(st, "sc_line"); llm = LLM()
llm.q = [json.dumps({"slot_updates": [{"slot": "age_band", "value": "中2", "evidence": "中2"}]}, ensure_ascii=False)]
H.turn(st, "中2だけど働ける？", BANK, llm)
check("jhs_extracted_with_evidence_blocks", st.blocked == "under_15")
st = fresh(); st.segment = "homemaker"; goto(st, "sc_line"); llm = LLM(); llm.q = [json.dumps({"slot_updates": []})]
H.turn(st, "子どもが中学生で、夕方は家にいたい", BANK, llm)
check("jhs_raw_text_not_applied_off_age_question", st.blocked is None and "age_band" not in st.slots)
st = fresh(); st.segment = "student"; goto(st, "hf_age"); llm = LLM(); llm.q = [json.dumps({"slot_updates": []})]
H.turn(st, "まだ中学生です", BANK, llm)
check("jhs_raw_text_fallback_on_hf_age", st.blocked == "under_15")
st = fresh(); st.segment = "student"; goto(st, "sc_line"); llm = LLM()
llm.q = [json.dumps({"slot_updates": [{"slot": "age_band", "value": "中2", "evidence": "中学2年"}]}, ensure_ascii=False)]
H.turn(st, "弟が中2なんだけど", BANK, llm)
check("age_without_matching_evidence_not_high", st.blocked is None or st.slots.get("age_band", {}).get("confidence") != "high")

# ---- 7. clarify & retry do not consume the question
st = fresh(); st.slots["age_band"] = {"value": "18_plus", "confidence": "high", "from": "x"}; st.segment = "student"; goto(st, "hf_commute")
llm = LLM(); llm.q = [json.dumps({"clarify": True})]; t0 = st.turns
r = H.turn(st, "どっちでもない…", BANK, llm)
check("clarify_keeps_question", st.current["qid"] == "hf_commute" and st.current["mode"] == "clarify" and st.turns == t0 + 1)
llm.q = [json.dumps({"slot_updates": [{"slot": "commute", "value": "電車で20分"}]}, ensure_ascii=False)]
H.turn(st, "電車で20分", BANK, llm)
check("clarify_answer_high", st.slots["commute"] == {"value": "train_30", "confidence": "high", "from": "free:hf_commute"}, st.slots.get("commute"))
st = fresh(); st.segment = "student"; goto(st, "hf_days"); llm = LLM(); llm.q = ["xx", "yy"]; t0 = st.turns
r = H.turn(st, "そこそこ", BANK, llm)
check("extract_fail_retry_not_consumed", st.current["qid"] == "hf_days" and st.current["mode"] == "retry" and st.turns == t0 and not r["counted"])
check("extract_fail_logged", any(e["kind"] == "extract_fallback" for e in st.log))
llm.q = ["xx", "yy"]; H.turn(st, "そこそこ", BANK, llm)
check("second_fail_moves_on_without_crash", st.current["qid"] != "hf_days" or st.current["mode"] != "retry")
st = fresh(); goto(st, "q_money"); llm = LLM(); llm.extract = lambda p: (_ for _ in ()).throw(RuntimeError("timeout"))
try:
    H.turn(st, "なんか", BANK, llm); ok = True
except Exception as e:
    ok = False
check("llm_exception_no_crash", ok)
st = fresh(); goto(st, "q_money"); llm = LLM(); llm.gen = ["broken", "broken"]
r = H.turn(st, "推し活・趣味", BANK, llm)
check("generate_fallback_raw_question", r["reply"].endswith(H.qmap(BANK)[st.current["qid"]]["text"]) or st.current["mode"] == "confirm", r["reply"])

# ---- 8. confidence is harness-assigned
st = fresh(); st.segment = "side_job"; st.slots["age_band"] = {"value": "18_plus", "confidence": "high", "from": "x"}; goto(st, "sc_line")
llm = LLM(); llm.q = [json.dumps({"slot_updates": [{"slot": "commute", "value": "walk_bike", "confidence": "high"}, {"slot": "pace", "value": "busy_ok", "confidence": "high"}]})]
H.turn(st, "燃えるタイプかな", BANK, llm)
check("model_confidence_ignored", st.slots["commute"]["confidence"] == "low" and st.slots["pace"]["confidence"] == "low", st.slots)
st = fresh(); st.segment = "side_job"; goto(st, "hf_sideok")
llm = LLM(); llm.q = [json.dumps({"slot_updates": [{"slot": "side_job_allowed", "value": "needs_notice"}, {"slot": "side_job_allowed", "value": "ok"}]})]
H.turn(st, "届け出かなあ", BANK, llm)
check("ambiguous_same_slot_low", st.slots["side_job_allowed"]["confidence"] == "low" and "side_job_allowed" in st.conflicts, st.slots.get("side_job_allowed"))

# ---- 9. serialization round trip
st = fresh(); st.segment = "homemaker"; goto(st, "hf_hours"); H.turn(st, "9〜14時くらい", BANK, LLM())
blob = json.dumps(st.to_dict(), ensure_ascii=False); st2 = H.State.from_dict(json.loads(blob))
check("serialize_no_volatile", "_new_notices" not in blob)
check("serialize_roundtrip", st2.to_dict() == json.loads(blob))
v1 = H.resume(st2, BANK); v2 = H.resume(st2, BANK)
check("resume_idempotent", v1["qid"] == v2["qid"] and st2.turns == st.turns)
st = fresh(); H.apply_update(st, "hourly_wage_min", 1200, "high", "t")
st2 = H.State.from_dict(json.loads(json.dumps(st.to_dict())))
check("wage_int_roundtrip", st2.slots["hourly_wage_min"]["value"] == 1200)

# ---- 10. segment: confirm on known->known by free text, never back to unknown
st = fresh(); H.set_segment(st, "student", "choice")
check("seg_free_change_needs_confirm", H.set_segment(st, "side_job", "free") == "pending_confirm" and st.segment == "student")
check("seg_unknown_rejected", H.set_segment(st, "unknown", "free") == "ignored" and st.segment == "student")
H.advance(st, BANK)
check("seg_confirm_question", st.current["qid"] == "confirm_segment" and st.current["mode"] == "confirm", st.current)
H.turn(st, "本業あり副業探し", BANK, LLM())
check("seg_confirm_switches", st.segment == "side_job" and st.pending_segment is None)
st = fresh(); H.set_segment(st, "student", "choice"); H.apply_update(st, "school_permission", "ok", "high", "t"); H.set_segment(st, "side_job", "choice")
check("seg_switch_archives", "school_permission" not in st.slots and "school_permission" in st.archived.get("student", {}))
H.set_segment(st, "student", "choice")
check("seg_switch_back_restores", st.slots.get("school_permission", {}).get("value") == "ok")

# ---- 11. HARD guarantee: empty HARD -> 未定 on card + unknown in result
st = fresh(); H.set_segment(st, "side_job", "choice"); H.apply_update(st, "age_band", "18_plus", "high", "t")
card = H.build_card(st); txt = H.render_card(card)
check("card_hard_missing_shown", "通勤：未定（ここだけ教えて！）" in txt, txt)
st.last_card = card; st.phase = "review"; H.apply_review(st, None, button_ok=True)
check("hard_gap_forces_round2", st._need_more_review)
H.finalize(st); p = H.build_result_payload(st, B.CATALOG)
check("result_lists_unknown_hard", "commute" in p["unknown_hard_slots"] and "通勤" in p["unknown_hard"])
txt = H.compose_result_text(p, H.fallback_result(p), B.CATALOG_BY_ID)
check("result_text_says_mitei", "未定：" in txt)
# HARD low is promoted only on explicit full OK; MID/LOW promoted on non-objection
st = fresh(); H.set_segment(st, "student", "choice"); H.apply_update(st, "age_band", "18_plus", "high", "t")
H.apply_update(st, "commute", "walk_bike", "low", "free_other:x"); H.apply_update(st, "pace", "calm", "low", "choice:sc_line")
st.last_card = H.build_card(st); H.apply_review(st, {"slot_updates": [], "ng_updates": [], "ng_removals": [], "segment": None, "intent": "answer", "review_ack": "partial"}, button_ok=False)
check("hard_low_not_promoted_on_partial", st.slots["commute"]["confidence"] == "low" and st.slots["pace"]["confidence"] == "high")
# conflict -> unresolved with both candidates (provisional §12-2)
st = fresh(); H.set_segment(st, "student", "choice"); H.apply_update(st, "pace", "calm", "low", "a"); H.apply_update(st, "pace", "busy_ok", "low", "b")
check("conflict_recorded", st.conflicts.get("pace") == ["calm", "busy_ok"])
st.last_card = H.build_card(st)
check("card_shows_dotchi", "（どっち？）" in H.render_card(st.last_card))
H.finalize(st); p = H.build_result_payload(st, B.CATALOG)
check("unresolved_both_candidates", list(p["unresolved"].values()) == [["落ち着いた職場", "忙しくてもOK"]] or len(list(p["unresolved"].values())[0]) == 2, p["unresolved"])

# ---- 12. closing text appears exactly once in a review reply
st = fresh(); H.set_segment(st, "student", "choice"); st.phase = "fill_gaps"; st.turns = 13; st.current = {"qid": "fg_wage", "mode": "ask", "choices": QM["fg_wage"]["choices"]}
r = H.turn(st, "1,000円台でOK", BANK, LLM())
check("review_forced_by_budget", st.phase == "review", st.phase)
check("closing_once", r["reply"].count(H.CLOSING_JA) == 1, r["reply"][-200:])
check("card_text_has_no_closing", H.CLOSING_JA not in H.render_card(st.last_card))

# ---- 13. result allowlist / count / money
p = {"candidates": [{"job_id": "cafe", "name": "x", "why_slots": []}, {"job_id": "super_stock", "name": "y", "why_slots": []}], "need": 2}
check("result_rejects_non_allowlist", H.validate_result(json.dumps({"reply": "", "recommendations": [{"job_id": "yami", "reason": "a"}, {"job_id": "cafe", "reason": "b"}]}), p)[0] is None)
check("result_rejects_wrong_count", H.validate_result(json.dumps({"reply": "", "recommendations": [{"job_id": "cafe", "reason": "a"}]}), p)[0] is None)
check("result_rejects_money", H.validate_result(json.dumps({"reply": "", "recommendations": [{"job_id": "cafe", "reason": "月8万円"}, {"job_id": "super_stock", "reason": "b"}]}, ensure_ascii=False), p)[0] is None)
check("result_accepts_valid", H.validate_result(json.dumps({"reply": "ok", "recommendations": [{"job_id": "cafe", "reason": "a"}, {"job_id": "super_stock", "reason": "b"}]}), p)[0] is not None)

# ---- 14. danger / aside not counted; restart resets
st = fresh(); t0 = st.turns
r = H.turn(st, "受け取るだけで日給5万ってやつ", BANK, LLM())
check("danger_warned_not_counted", "#9110" in r["reply"] and st.turns == t0 and not r["counted"])
r2 = H.turn(st, "受け取るだけで日給5万ってやつ", BANK, LLM())
check("danger_notice_every_time", "#9110" in r2["reply"])
llm = LLM(); llm.q = [json.dumps({"intent": "restart"})]; st.turns = 5; st.slots["pace"] = {"value": "calm", "confidence": "low", "from": "x"}
H.turn(st, "最初から", BANK, llm)
check("restart_resets", st.turns == 0 and not st.slots and st.restarts == 1 and st.phase == "icebreak")

# ---- 15. NOPREF on pref ok, on fact rejected
st = fresh(); st.segment = "student"
check("nopref_pref_ok", H.apply_update(st, "pace", H.NOPREF, "high", "t") == "set")
check("nopref_fact_rejected", H.apply_update(st, "school_permission", H.NOPREF, "high", "t") == "rejected")

# ---- 16. prompt files: every {{var}} is supplied by the harness for each stage
st = fresh()
pl = H.extract_payload(st, BANK, "週3くらい")
try:
    H.render_prompt("extract", pl); ok = True
except KeyError as e:
    ok = False; print(e)
check("prompt_extract_vars_complete", ok)
v = H.view(st, BANK)
try:
    t = H.render_prompt(H.prompt_stage(v), v); ok = "{{" not in t
except KeyError as e:
    ok = False; print(e)
check("prompt_say_vars_complete", ok)
st = fresh(); st.segment = "student"; st.phase = "review"; H.advance(st, BANK) if False else None
st.phase = "review"; st.current = {"qid": "review", "mode": "review", "choices": ["これでOK"]}
st.last_card = H.build_card(st); v = H.view(st, BANK)
try:
    t = H.render_prompt("review", v); ok = t.count(H.CLOSING_JA) == 1 and "{{" not in t
except KeyError as e:
    ok = False; print(e)
check("prompt_review_vars_complete_closing_once", ok)
v["result_json"] = "{}"; v["mode"] = "result"
try:
    H.render_prompt("result", v); ok = True
except KeyError as e:
    ok = False; print(e)
check("prompt_result_vars_complete", ok)

# ---- 17. 付録C API surface (a port must expose these names)
API = ("State new_state start resume turn advance next_phase pick_question visible_choices match_button extract_payload view "
       "validate_extract validate_reply validate_result apply_update set_segment normalize_value confidence_for build_card "
       "render_card apply_review finalize build_result_payload eligible_jobs compose_result_text fallback_result "
       "post_check_result ng_hits job_safe update_block qmap minor_mode active_slots hard_slots gap_weight is_filled "
       "recompute_derived render_prompt prompt_stage slots_json").split()
CONSTS = ("VERSION SLOTS SLOT_PRIORITY SEGMENTS SEGMENT_JA NOTICE NOPREF DERIVED_NG_MINOR FACT_SLOTS ORDER HARD MID LOW PHASE_CAP "
          "DANGER_RE MONEY_RE JHS_RE NOPREF_RE DECLINE_RE PII_RE MULTI_SEP_RE").split()
miss = [n for n in API + CONSTS if not hasattr(H, n)]
check("api_surface_appendix_c", not miss, miss)

# ---- 18. PRD normative blocks == implementation (05-B3/B4).  The PRD wins: on mismatch fix the code.
PRD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PRD_baito_agent_v4.md")
prd = open(PRD, encoding="utf-8").read() if os.path.exists(PRD) else ""
def block(tag):
    m = re.search(rf"<!-- {tag}:BEGIN -->\s*```[a-z]*\n(.*?)```\s*<!-- {tag}:END -->", prd, re.S)
    return [l for l in (m.group(1).splitlines() if m else []) if l.strip() and not l.startswith("#")]
rx = dict(l.split(" = ", 1) for l in block("REGEX"))
bad = [k for k in ("DANGER_RE", "MONEY_RE", "JHS_RE", "NOPREF_RE", "DECLINE_RE", "PII_RE", "MULTI_SEP_RE")
       if rx.get(k) != getattr(H, k).pattern]
check("prd_regex_block_matches", bool(rx) and not bad, bad)
cs = dict(l.split(" = ", 1) for l in block("CONST"))
badc = [k for k, v in cs.items() if not hasattr(H, k) or eval(v) != getattr(H, k)]
check("prd_const_block_matches", len(cs) >= 12 and not badc, badc)
tx = {k: v for k, v in (l.split(" = ", 1) for l in block("TEXT")) if k != "Q_SEGMENT_CHOICES"}
badt = [k for k, v in tx.items() if (H.NOTICE.get(k[7:]) if k.startswith("NOTICE.") else getattr(H, k, None)) != v]
check("prd_text_block_matches", len(tx) >= 20 and not badt, badt)
seg_line = [l for l in block("TEXT") if l.startswith("Q_SEGMENT_CHOICES = ")]
check("prd_q_segment_wording", seg_line and json.loads(seg_line[0].split(" = ", 1)[1]) == QM["q_segment"]["choices"])

# ---- 19. MONEY_RE / DANGER_RE never hit harness-owned text (05-B3)
owned = [H.CLOSING_JA, H.CONFIRM_TEXT, H.POST_RESULT_TEXT, H.GUESS_TAIL, H.CLARIFY_TEXT, H.RETRY_TEXT]
for q in BANK:
    owned += [q["text"], q["alt_text"]] + list(q["choices"])
for s, sp in H.SLOTS.items():
    owned += [sp["label"]] + [str(x) for x in sp["values"].values()]
owned += [j["name"] for j in C.CATALOG]
hit = [t for t in owned if H.MONEY_RE.search(H.nfkc(t)) or H.DANGER_RE.search(H.nfkc(t))]
check("money_danger_re_not_on_owned_text", not hit, hit[:5])
check("wage_label_not_money", not H.MONEY_RE.search("1,200円以上") and not H.MONEY_RE.search("1,500円以上"))
check("money_re_hits_amounts", all(H.MONEY_RE.search(x) for x in ("月8万円", "103万", "1.5万")))
check("catalog_appendix_b", len(C.CATALOG) == 24 and B.CATALOG is C.CATALOG and all(
    set(j) >= {"id", "name", "cf", "phys", "times", "keys", "min_age", "remote", "noise", "night_only", "spot"} for j in C.CATALOG))
check("bank_appendix_a", len(BANK) == 49 and len(QM) == 49)
check("multi_choices_have_no_separator", not [c for q in BANK if q["multi"] for c in q["choices"] if H.MULTI_SEP_RE.search(c)])

# ---- 20. counting (05-B4/B5/B6)
st = fresh()
check("new_state_derived_ng", st.derived_ng == H.DERIVED_NG_MINOR)
def adult(st, seg="student"):
    H.set_segment(st, seg, "choice"); H.apply_update(st, "age_band", "18_plus", "high", "t")
st = fresh(); adult(st); goto(st, "hf_commute"); st.phase_turns = {"icebreak": 2, "hard_filter": 5}; st.turns = 7
llm = LLM(); llm.q = [json.dumps({"intent": "clarify"})]
H.turn(st, "どっちでもない", BANK, llm)
check("clarify_suppressed_at_phase_cap", st.current["mode"] != "clarify" and any(e["kind"] == "clarify_suppressed" for e in st.log)
      and st.phase_turns["hard_filter"] == 6 and st.phase != "hard_filter", (st.phase, st.phase_turns))
st = fresh(); adult(st); goto(st, "hf_commute"); st.phase_turns = {"icebreak": 2, "hard_filter": 1}; st.turns = 12
llm = LLM(); llm.q = [json.dumps({"intent": "clarify"})]
H.turn(st, "どっちでもない", BANK, llm)
check("clarify_suppressed_at_review_reserve", st.current["mode"] != "clarify" and st.phase == "review", st.phase)
st = fresh(); adult(st); goto(st, "hf_days"); st.phase_turns = {"icebreak": 2, "hard_filter": 6}; st.turns = 8
H.set_segment(st, "side_job", "free"); H.advance(st, BANK)
check("confirm_counted_in_next_phase", st.current["mode"] == "confirm" and st.phase == "scenario_game", st.phase)
H.turn(st, "本業あり副業探し", BANK, LLM())
check("confirm_does_not_break_cap", st.phase_turns["hard_filter"] == 6 and st.phase_turns.get("scenario_game") == 1, st.phase_turns)
st = fresh(); adult(st); goto(st, "hf_days"); st.turns = 14; st.phase_turns = {"hard_filter": 14}; st.aside_count = 3
llm = LLM(); llm.q = [json.dumps({"intent": "aside"})]
r = H.turn(st, "ところで今日寒いね", BANK, llm)
check("cap_15_via_counted_aside", st.turns == 15 and r["done"] and st.phase == "result" and st.phase_turns.get("aside") == 1, (st.turns, st.phase))
st = fresh(); adult(st); st.phase = "review"; st.turns = 14; st.phase_turns = {"review": 14}; st.aside_count = 3
st.current = {"qid": "review", "mode": "review", "choices": [H.REVIEW_OK]}; st.last_card = H.build_card(st)
H.apply_update(st, "commute", "walk_bike", "low", "free_other:x")
llm = LLM(); llm.q = [json.dumps({"intent": "aside"})]
r = H.turn(st, "関係ないけど", BANK, llm)
check("cap_15_in_review_no_promotion", r["done"] and st.slots["commute"]["confidence"] == "low" and "commute" in st.result["payload"]["guessed_hard_slots"])

# ---- 21. aside / danger re-presentation (05-B7, M-5)
st = fresh(); adult(st); goto(st, "hf_days"); st.asked.append("hf_days"); t0 = st.turns; att = dict(st.attempts)
llm = LLM(); llm.q = [json.dumps({"intent": "aside"})]
r = H.turn(st, "時給って普通いくら？", BANK, llm)
check("aside_mode", r["view"]["mode"] == "aside" and r["view"]["base_mode"] == "ask" and r["view"]["qid"] == "hf_days"
      and st.current == {"qid": "hf_days", "mode": "ask", "choices": QM["hf_days"]["choices"]} and st.turns == t0 and st.attempts == att, r["view"]["mode"])
check("aside_reply_keeps_question", QM["hf_days"]["text"] in r["reply"])
class CountLLM(LLM):
    def __init__(self): super().__init__(); self.ng = 0; self.ne = 0
    def generate(self, v): self.ng += 1; return super().generate(v)
    def extract(self, p): self.ne += 1; return super().extract(p)
st = fresh(); adult(st); goto(st, "hf_days"); cl = CountLLM()
r = H.turn(st, "受け取るだけで日給5万のやつ", BANK, cl)
check("danger_no_llm_calls", cl.ng == 0 and cl.ne == 0 and H.NOTICE["danger"] in r["reply"] and QM["hf_days"]["text"] in r["reply"]
      and r["view"]["mode"] == "aside" and any(e["kind"] == "danger_skip" for e in st.log))
st = fresh(); adult(st); goto(st, "hf_days"); st.aside_count = 2
llm = LLM(); llm.q = [json.dumps({"intent": "aside"})]; H.turn(st, "雑談", BANK, llm)
r = H.turn(st, "受け取るだけ", BANK, LLM())
check("aside_danger_share_budget", st.aside_count == 4 and r["counted"] and st.turns == 1)

# ---- 22. review / confirm extraction failure (05-B8)
st = fresh(); adult(st); st.phase = "fill_gaps"; st.turns = 13; st.phase_turns = {"fill_gaps": 13}
st.current = {"qid": "fg_wage", "mode": "ask", "choices": QM["fg_wage"]["choices"]}
H.turn(st, "1,000円台でOK", BANK, LLM())
H.apply_update(st, "pace", "calm", "low", "choice:sc_line"); st.last_card = H.build_card(st)
pl = H.extract_payload(st, BANK, "x")
check("review_payload", pl["question"] == H.render_card(st.last_card) and pl["choices"] == [H.REVIEW_OK] and "pace" in pl["targets"])
llm = LLM(); llm.q = ["xx", "yy"]; t0 = st.turns
r = H.turn(st, "うーん", BANK, llm)
check("review_unparsed_first_retry", not r["counted"] and st.turns == t0 and r["reply"].startswith(H.RETRY_TEXT) and st.review_rounds == 0
      and r["reply"].count(H.CLOSING_JA) == 1)
llm.q = ["xx", "yy"]; r = H.turn(st, "うーん", BANK, llm)
check("review_unparsed_second_no_promotion", r["counted"] and st.review_rounds == 1 and st.slots["pace"]["confidence"] == "low")
st = fresh(); H.set_segment(st, "student", "choice"); H.set_segment(st, "side_job", "free"); H.advance(st, BANK)
pl = H.extract_payload(st, BANK, "x")
check("confirm_payload", pl["question"] == H.CONFIRM_TEXT and pl["targets"] == ["segment"] and pl["choices"] == QM["q_segment"]["choices"])
llm = LLM(); llm.q = ["xx", "yy"]; H.turn(st, "うーん", BANK, llm)
check("confirm_unparsed_keeps_segment", st.segment == "student" and st.pending_segment is None and any(e["kind"] == "confirm_unparsed" for e in st.log))
st = fresh(); H.set_segment(st, "student", "choice"); H.set_segment(st, "side_job", "free"); H.advance(st, BANK)
llm = LLM(); llm.q = [json.dumps({"segment": None})]; H.turn(st, "まあね", BANK, llm)
check("confirm_null_segment_keeps", st.segment == "student" and st.pending_segment is None)

# ---- 23. after the result (05-B9)
st = fresh(); st.segment = "student"; goto(st, "hf_age"); H.turn(st, "中学生以下", BANK, LLM())
r = H.turn(st, "ありがとう", BANK, LLM())
check("post_result_fixed_reply", r["done"] and not r["counted"] and r["reply"].startswith(H.POST_RESULT_TEXT) and any(e["kind"] == "post_result_message" for e in st.log))
llm = LLM(); llm.q = [json.dumps({"slot_updates": [{"slot": "age_band", "value": "16歳", "evidence": "16歳"}]}, ensure_ascii=False)]
r = H.turn(st, "16歳", BANK, llm)  # aica task 258: the correction is read only in a clear form ("ごめん、本当は16歳" is not)
check("post_result_unblock_by_age_fix", st.blocked is None and st.result is None and not r["done"] and st.slots["age_band"]["value"] == "15_17"
      and st.phase not in ("result",), (st.phase, st.blocked))
st = fresh(); st.segment = "student"; goto(st, "hf_age"); H.turn(st, "中学生以下", BANK, LLM())
llm = LLM(); llm.q = [json.dumps({"intent": "restart"})]; r = H.turn(st, "最初から", BANK, llm)
check("post_result_restart", st.result is None and st.restarts == 1 and st.turns == 0 and st.blocked is None)
r = H.turn(st, "", BANK, LLM())
check("empty_input_not_counted", not r["counted"] and st.turns == 0 and any(e["kind"] == "empty_input" for e in st.log))

# ---- 24. JHS scope / low under_15 (05-B10)
st = fresh(); st.segment = "student"; goto(st, "sc_line")
H.apply_update(st, "age_band", "under_15", "low", "free_other:sc_line")
check("low_under15_not_blocking_midway", st.blocked is None and H.minor_mode(st))
H.finalize(st)
check("low_under15_blocks_at_finalize", st.blocked == "under_15")

# ---- 25. low HARD at result time (05-B11)
st = fresh(); adult(st, "side_job"); H.apply_update(st, "commute", "walk_bike", "low", "free_other:x")
H.finalize(st); p = H.build_result_payload(st, C.CATALOG)
txt = H.compose_result_text(p, H.fallback_result(p), C.CATALOG_BY_ID)
check("guessed_hard_listed", "commute" in p["guessed_hard_slots"] and "推測：通勤＝徒歩・自転車" in txt and "commute" not in p["unknown_hard_slots"])

# ---- 26. post-check refill (05-M13), tiers (N-3), truncation (M-12), multi separators (N-10)
st = fresh(); st.segment = "student"; H.apply_update(st, "age_band", "15_17", "high", "t")
p = {"need": 2, "candidates": [{"job_id": "izakaya", "why_slots": []}, {"job_id": "cafe", "why_slots": ["customer_facing"]},
                                {"job_id": "super_stock", "why_slots": []}]}
out = H.post_check_result(st, p, {"recommendations": [{"job_id": "izakaya", "reason": "a"}, {"job_id": "cafe", "reason": "b"}]}, C.CATALOG_BY_ID)
check("post_check_refill", [r["job_id"] for r in out["recommendations"]] == ["cafe", "super_stock"], out)
st = fresh(); adult(st)
card = H.build_card(st)
check("card_tiers_hard_mid_low", {i["tier"] for i in card["hard"]} == {"hard"} and {i["tier"] for i in card["prefs"]} <= {"mid", "low"})
r, pr = H.validate_extract(json.dumps({"slot_updates": [{"slot": "pace", "value": "calm"}] * 12, "ng_updates": list("abcdefg")}))
check("extract_truncation_logged", len(r["slot_updates"]) == 10 and "slot_updates_truncated" in pr and "ng_updates_truncated" in pr and len(r["ng_updates"]) == 5)
st = fresh(); adult(st); goto(st, "hf_time")
check("multi_sep_dedupe", H.match_button(st, BANK, "朝／夕方〜夜, 朝") == ["朝", "夕方〜夜"])
check("review_ack_values", H.validate_extract('{"review_ack":"partial"}')[0]["review_ack"] == "partial" and H.validate_extract('{"review_ack":"yes"}')[0]["review_ack"] is None)

# ---- 27. internal error rollback (05-M15)
st = fresh(); adult(st); goto(st, "hf_commute"); before = st.to_dict()
orig = H._apply_ext
def boom(*a, **k): raise RuntimeError("bug")
H._apply_ext = boom
try:
    llm = LLM(); llm.q = [json.dumps({"slot_updates": [{"slot": "commute", "value": "電車で20分"}]}, ensure_ascii=False)]
    r = H.turn(st, "電車で20分", BANK, llm); ok = True
except Exception:
    ok = False
finally:
    H._apply_ext = orig
after = st.to_dict(); errs = [e for e in after["log"] if e["kind"] == "internal_error"]
after["log"] = [e for e in after["log"] if e["kind"] != "internal_error"]
check("internal_error_rollback", ok and not r["counted"] and errs and after == before and QM["hf_commute"]["text"] in r["reply"])

# ---- 28. show_result (05-M6): before review it forces review and also stops round 2
st = fresh(); adult(st, "side_job"); goto(st, "hf_days"); st.phase_turns = {"hard_filter": 1}; st.turns = 3
llm = LLM(); llm.q = [json.dumps({"intent": "show_result"})]; H.turn(st, "もういいから早く結果見せて", BANK, llm)
check("show_result_forces_review", st.phase == "review" and "show_result" in st.flags)
r = H.turn(st, H.REVIEW_OK, BANK, LLM())
check("show_result_stops_round2", r["done"] and st.review_rounds == 1)

print(f"\n{N - len(FAILS)}/{N} passed")
sys.exit(1 if FAILS else 0)
