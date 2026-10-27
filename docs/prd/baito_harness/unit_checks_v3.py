# path not exercised by the 8 personas: "これでOK" while a conflict is still open
import harness_v3 as H, bank
H.set_value_labels(bank.BANK)
s = H.State(segment="student", turns=12, phase="fill_gaps")
for v in ("ok", "avoid"):
    H.merge_updates(s, {"slot_updates": [{"slot": "customer_facing", "value": v, "confidence": "low", "from": "sc"}]})
q = H.step(s, None, bank.BANK); print("phase:", s.phase, "| conflict line:", [l for l in q["text"].split("\n") if "どっち" in l])
q = H.step(s, {"slot_updates": [], "ng_updates": [], "segment": "unknown"}, bank.BANK)
print("after これでOK -> next:", q and q["id"], "| need_more_review:", s.need_more_review, "| conflicts:", s.conflicts)
q = H.step(s, {"slot_updates": [], "ng_updates": [], "segment": "unknown"}, bank.BANK)
print("after 2nd これでOK -> next:", q, "| phase:", s.phase, "| turns:", s.turns, "| conflicts still open:", s.conflicts,
      "| customer_facing:", s.slots["customer_facing"])
q = H.step(s, {"slot_updates": [], "ng_updates": [], "segment": "unknown"}, bank.BANK)
print("after round-2 これでOK -> next:", q, "| phase:", s.phase, "| turns:", s.turns, "| rounds:", s.review_rounds,
      "| conflicts:", s.conflicts, "| customer_facing:", s.slots["customer_facing"])
