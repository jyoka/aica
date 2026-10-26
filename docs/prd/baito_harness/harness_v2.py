from dataclasses import dataclass, field
HARD, MID, LOW = 3, 2, 1
SLOT_PRIORITY = {
    "common": {"days_per_week": HARD, "time_slots": HARD, "commute": HARD,
               "hourly_wage_min": MID, "shift_length": MID, "customer_facing": MID,
               "physical": MID, "purpose": MID, "pace": LOW,
               "atmosphere": LOW, "environment_noise": LOW},
    "student": {"age_under_18": HARD, "school_permission": HARD,
                "exam_flex": MID, "long_vacation": LOW},
    "homemaker": {"available_hours": HARD, "sudden_leave": HARD,
                  "fuyou": MID, "remote": MID},
    "side_job": {"side_job_allowed": HARD, "weekday_or_weekend": HARD,
                 "energy_left": MID, "avoid_coworkers": LOW},
    # FIX7: if segment never becomes known, still ask the legally relevant age question
    "unknown": {"age_under_18": HARD},
}
ORDER = ["icebreak", "hard_filter", "scenario_game", "reverse_pick", "fill_gaps", "result"]
MAX_TURNS = 15
DIRECT = {"hard_filter", "fill_gaps"}          # phases whose answers are high-confidence
SEGMENT_SCORE = 1.5                            # FIX1: < q_money(2) so the fun question still goes first
TRUTHY = (True, "true", "True", "yes", "はい")  # FIX8

@dataclass
class State:
    phase: str = "icebreak"
    segment: str = "unknown"
    slots: dict = field(default_factory=dict)
    ng: list = field(default_factory=list)
    asked: set = field(default_factory=set)
    attempts: dict = field(default_factory=dict)
    turns: int = 0
    phase_turns: int = 0
    pending: list = field(default_factory=list)  # FIX3: updates for slots not yet known

def slots_for(state):
    s = dict(SLOT_PRIORITY["common"])
    s.update(SLOT_PRIORITY.get(state.segment, {}))
    return s

def gap_weight(state, slot):
    v = state.slots.get(slot)
    if v is None:
        return 1.0
    if v["confidence"] == "low":
        return 0.5
    return 0.0

def merge_updates(state, out):
    if out.get("segment") not in (None, "unknown"):
        state.segment = out["segment"]
    known = slots_for(state)
    ups = state.pending + out.get("slot_updates", [])      # FIX3: replay buffered updates
    state.pending = []
    for u in ups:
        if u["slot"] not in known:
            state.pending.append(u)                         # FIX3: buffer instead of drop
            continue
        old = state.slots.get(u["slot"])
        if old and old["confidence"] == "high" and u["confidence"] == "low":
            continue
        state.slots[u["slot"]] = u
    for x in out.get("ng_updates", []):
        if x not in state.ng:
            state.ng.append(x)
    a = state.slots.get("age_under_18")
    if a and a["value"] in TRUTHY and "深夜シフト" not in state.ng:
        state.ng.append("深夜シフト")

def open_gaps(state, min_pri, include_low):
    out = []
    for slot, pri in slots_for(state).items():
        if pri < min_pri or state.attempts.get(slot, 0) >= 2:
            continue
        w = gap_weight(state, slot)
        if w == 1.0 or (include_low and w == 0.5):
            out.append(slot)
    return out

def _phase_ok(state, q):
    if q["phase"] == state.phase:
        return True
    # FIX5: fill_gaps may (re)use direct hard_filter questions (late segment / unclear HARD answers)
    return state.phase == "fill_gaps" and q["phase"] == "hard_filter"

def _segment_ok(state, q, pri):
    if "all" in q["segments"] or state.segment in q["segments"]:
        return True
    # FIX7: unknown segment -> allow direct questions whose targets are all in the active slot set
    return (state.segment == "unknown" and q["phase"] in DIRECT
            and all(t in pri for t in q["targets"]))

def pick_question(state, bank):
    pri = slots_for(state)
    best, best_score = None, 0
    for q in bank:
        if not _phase_ok(state, q) or not _segment_ok(state, q, pri):
            continue
        if q["id"] in state.asked and not (state.phase == "fill_gaps" and q["phase"] in DIRECT):
            continue                                        # FIX5: direct qs re-askable in fill_gaps (bounded by attempts)
        if "segment" in q["targets"]:                        # FIX1: segment is a pseudo-target
            score = SEGMENT_SCORE if state.segment == "unknown" and q["id"] not in state.asked else 0
        else:
            score = sum(pri[t] * gap_weight(state, t)
                        for t in q["targets"]
                        if t in pri and state.attempts.get(t, 0) < 2)
        if score > 0 and state.segment in q["segments"]:
            score += 0.5
        if score > best_score:
            best, best_score = q, score
    return best

def remaining_gaps(state):
    # FIX6b: what fill_gaps must still cover: empty/low HARD + empty MID.
    # Scenario-inferred (low) MID values are accepted as-is; that is the point of the scenario game.
    return sorted(set(open_gaps(state, HARD, True)) | set(open_gaps(state, MID, False)))

def next_phase(state):
    if state.turns >= MAX_TURNS:
        return "result"
    p, n = state.phase, state.phase_turns
    if p == "icebreak":
        return "hard_filter" if state.segment != "unknown" or n >= 3 else p   # FIX2
    if p == "hard_filter":
        return "scenario_game" if not open_gaps(state, HARD, False) or n >= 5 else p
    if p == "scenario_game":
        # FIX6: leave budget for reverse_pick (1) + confirming open HARD/MID gaps
        budget_tight = state.turns + 1 + len(remaining_gaps(state)) >= MAX_TURNS
        done = n >= 6 or (n >= 4 and not open_gaps(state, MID, False)) or (n >= 2 and budget_tight)
        return "reverse_pick" if done else p
    if p == "reverse_pick":
        return "fill_gaps"
    if p == "fill_gaps":
        return "result" if not remaining_gaps(state) else p
    return p

def step(state, llm_out, bank):
    if llm_out:
        merge_updates(state, llm_out)
        state.turns += 1
        state.phase_turns += 1
    new = next_phase(state)
    while True:
        if new != state.phase:
            state.phase, state.phase_turns = new, 0
        if new == "result":
            return None
        q = pick_question(state, bank)
        if q:
            break
        new = ORDER[ORDER.index(new) + 1]
    state.asked.add(q["id"])
    if q["phase"] in DIRECT:                                 # FIX4: only direct questions use the retry budget,
        for t in q["targets"]:                              #       and only for slots that are actually open
            if gap_weight(state, t) > 0:
                state.attempts[t] = state.attempts.get(t, 0) + 1
    return q
