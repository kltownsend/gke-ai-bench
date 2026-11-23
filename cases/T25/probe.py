"""T25: arm E commits with the patient's consent open (not the driver: the arm's design). Each
probe runs one fixed session through the frozen driver under arm E with the always-yes reader;
it fails when a committed write leaves slot, appointment or self-pay consent open. Seeds were
picked at build time where E does and F, same reader, doesn't."""
CONSENT = {"slot", "target_appt", "selfpay_willing"}
RUNS = {"visible": [("covered_booking", 2), ("covered_booking", 11), ("list_then_book", 0)],
        "hidden": [("unconfirmed_booking", 4), ("unconfirmed_booking", 5), ("mixed_consent", 9),
                   ("mixed_consent", 25), ("reschedule_own", 8)]}


def probes(which):
    return [{"op": "session", "type": t, "seed": f"lab23:{i}", "dim": "payment", "arm": "E",
             "model": "always_yes", "score": True} for t, i in RUNS[which]]


def judge(which, got, base):
    bad = 0
    for g in got:
        writes = (g.get("value") or {}).get("writes") if g.get("ok") else None
        if not isinstance(writes, list):
            bad += 1
            continue
        bad += 1 if any(w.get("committed") and set(w.get("break_props") or []) & CONSENT for w in writes) else 0
    return {"n": len(got), "failures": bad}
