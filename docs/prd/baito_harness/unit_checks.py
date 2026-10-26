import harness as v1, harness_v2 as v2
for H in (v1, v2):
    s = H.State()
    H.merge_updates(s, {"slot_updates": [{"slot": "age_under_18", "value": True, "confidence": "high", "from": "free"}], "segment": "unknown"})
    H.merge_updates(s, {"slot_updates": [], "segment": "student"})
    print(H.__name__, "age-before-segment ->", s.slots.get("age_under_18", {}).get("value"), s.ng)
    s = H.State(segment="student")
    H.merge_updates(s, {"slot_updates": [{"slot": "age_under_18", "value": "true", "confidence": "high", "from": "x"}]})
    print(H.__name__, "age as string 'true' -> NG:", s.ng)
