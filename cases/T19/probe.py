"""T19: when the patient's answer names more than one doctor, arm F asks "Which would you like?"
again and again and never hands off (a finding). A failure means the defect showed: the same
doctor question went out a third time in a row with no hand-off between (or the replay never reached the question,
a failed setup). Two asks is the driver's own limit for
every other question it can't resolve (a re-ask, then staff).

Visible: the reported phrasing, "Dr. Okafor, not Dr. Chen". Hidden: the same loop from other
answers that leave two doctors in play: either one, the other cardiology pair with "not", and a
specialty with no doctor named. Zero model calls: the driver runs with a stub reader. Every setup
step must succeed."""
QUESTION = "Which would you like?"
HANDOFF = "a member of our staff will follow up"
ADA = "My name is Ada Lovelace, born 1990-01-01."            # plan A: dermatology covered
GRACE = "My name is Grace Hopper, born 1985-05-05."          # plan B: cardiology covered
RUNS = {"visible": [[ADA, "I need to book dermatology."] + ["Dr. Okafor, not Dr. Chen."] * 4],
        "hidden": [[ADA, "I need to book dermatology."] + ["I want Dr. Okafor and not Dr. Chen. I won't see Dr. Chen."] * 4,
                   [ADA, "I need to book dermatology."] + ["Either Dr. Okafor or Dr. Chen is fine."] * 4,
                   [GRACE, "I need to book cardiology."] + ["Dr. Patel, not Dr. Haddad."] * 4,
                   [GRACE, "I need to see someone in cardiology."] + ["Whoever is free in cardiology."] * 4,
                   [ADA, "I need a new appointment."] + ["Dermatology, please."] * 4]}


def probes(which):
    return [{"op": "replay", "arm": "F", "model": "always_unclear", "lines": lines} for lines in RUNS[which]]


def judge(which, got, base):
    bad = 0
    for g, lines in zip(got, RUNS[which]):
        v = g.get("value") if g.get("ok") else None
        replies = v.get("replies") if isinstance(v, dict) else None
        if not (isinstance(replies, list) and len(replies) == len(lines) and "verified" in replies[0]):
            bad += 1
            continue
        asks, streak, worst = 0, 0, 0
        for r in replies:
            if HANDOFF in r:
                streak = 0
            elif QUESTION in r:
                asks, streak = asks + 1, streak + 1
                worst = max(worst, streak)
        bad += 1 if (worst >= 3 or asks == 0) else 0      # never asked: the setup didn't reach the question
    return {"n": len(got), "failures": bad}
