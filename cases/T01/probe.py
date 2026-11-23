"""T01: a covered-booking patient asked which time, or countering an offer, names one time.

The symptom is on the F path: the frozen driver (dcitl_port, through adapters/dcitl_f) talking to
the bench's patient through the bench's hearing of the driver's text. Each probe is the subject's
driver session (arm F, the AlwaysUnclear stub reader, no server), fully determined by (seed,
setting); the subject returns raw values. Judged by property, not pinned: in every session the patient's disclosure log
has no slot proposition affirming more than one time, and at least one slot proposition affirms
exactly one (a fix that stops the patient naming times fails). Every session must run to an end
other than infra_error; a failed setup is a failure, never a skip.
"""
# (truth seed, payment setting index into records.all_settings()); same form, other numbers.
# Seeds are the ones where the patient states a time and at least one offered time works for it
# (with none on offer the fixed patient still lists its times, out of this case's scope); both sets mix the two ways the
# symptom shows (answering "Which one would you like?" over a list, and countering an offer).
RUNS = {"visible": [(f"pilot:{i}", s) for i in (0, 1, 4, 5, 8, 9) for s in (0, 18)],
        "hidden": [(f"t01h:{i}", s) for i in (0, 1, 2, 3, 4, 5, 6, 7, 9, 11, 12, 13, 15, 16)
                   for s in (6, 30, 42)]}

def probes(which):
    return [{"op": "session", "type": "covered_booking", "seed": seed, "dim": "payment", "setting": s,
             "arm": "F", "model": "always_unclear"} for seed, s in RUNS[which]]


def _bad(g):
    if not (isinstance(g, dict) and g.get("ok") is True and isinstance(g.get("value"), dict)):
        return True
    v = g["value"]
    end, log = v.get("end"), v.get("log")
    if not isinstance(end, dict) or end.get("reason") in (None, "infra_error") or not isinstance(log, list):
        return True
    slots = [p for d in log for p in d.get("propositions", []) if p.get("name") == "slot"]
    if any(len(p.get("affirmed") or []) > 1 for p in slots):
        return True
    return not any(len(p.get("affirmed") or []) == 1 for p in slots)


def judge(which, got, base):
    return {"n": len(got), "failures": sum(1 for g in got if _bad(g))}
