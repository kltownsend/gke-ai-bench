"""T03: change-of-mind truths must stay consistent after the schedule change.

Pinned to the shipped fix (build review 3): every change-of-mind truth the
workspace builds must equal, exactly, what the Clinic at 8ad3cac builds for the same seed
(baselines/t03fix). That pins the replacement time, the limits that drop, and everything else in
the truth. A fix that behaves differently, even a valid one that draws its replacement another
way, fails; that's the case's acceptance rule, disclosed. Deleting events, emptying schedules,
dropping limits or keeping only the rule text all fail, because none of them match.
"""
SEEDS = {"visible": ("lab23", 0, 200), "hidden": ("lab23", 200, 300)}   # same form, other numbers
PIN_REFERENCE = "t03fix"        # baselines/ name of the shipped fix; the judge asks it too


def probes(which):
    prefix, start, n = SEEDS[which]
    return [{"op": "call", "target": "clinicbench.truth.build", "args": ["change_of_mind", f"{prefix}:{i}"]}
            for i in range(start, start + n)]


def judge(which, got, base, ref):
    bad = 0
    for g, r in zip(got, ref):
        if not (isinstance(g, dict) and g.get("ok") is True and isinstance(r, dict) and r.get("ok") is True
                and g.get("value") == r["value"]):
            bad += 1
    return {"n": len(got), "failures": bad}
