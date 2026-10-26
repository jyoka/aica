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
}
ORDER = ["icebreak", "hard_filter", "scenario_game", "reverse_pick", "fill_gaps", "result"]
MAX_TURNS = 15

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
    for u in out.get("slot_updates", []):
        if u["slot"] not in known:
            continue
        old = state.slots.get(u["slot"])
        if old and old["confidence"] == "high" and u["confidence"] == "low":
            continue
        state.slots[u["slot"]] = u
    for x in out.get("ng_updates", []):
        if x not in state.ng:
            state.ng.append(x)
    a = state.slots.get("age_under_18")
    if a and a["value"] is True and "深夜シフト" not in state.ng:
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

def pick_question(state, bank):
    pri = slots_for(state)
    best, best_score = None, 0
    for q in bank:
        if q["id"] in state.asked or q["phase"] != state.phase:
            continue
        if "all" not in q["segments"] and state.segment not in q["segments"]:
            continue
        score = sum(pri[t] * gap_weight(state, t)
                    for t in q["targets"]
                    if t in pri and state.attempts.get(t, 0) < 2)
        if score > 0 and state.segment in q["segments"]:
            score += 0.5
        if score > best_score:
            best, best_score = q, score
    return best

def next_phase(state):
    if state.turns >= MAX_TURNS:
        return "result"
    p, n = state.phase, state.phase_turns
    if p == "icebreak":
        return "hard_filter" if state.segment != "unknown" or n >= 2 else p
    if p == "hard_filter":
        return "scenario_game" if not open_gaps(state, HARD, False) or n >= 5 else p
    if p == "scenario_game":
        done = n >= 6 or (n >= 4 and not open_gaps(state, MID, False))
        return "reverse_pick" if done else p
    if p == "reverse_pick":
        return "fill_gaps"
    if p == "fill_gaps":
        return "result" if not open_gaps(state, MID, True) else p
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
    for t in q["targets"]:
        state.attempts[t] = state.attempts.get(t, 0) + 1
    return q
