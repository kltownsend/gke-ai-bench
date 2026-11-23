"""T24: identity conversations end with the patient giving up (not the driver: the patient voice,
ruled). A probe fails when the session ends in give_up, so the case reproduces on the baseline;
there's no fix to pass, and a right answer changes nothing."""
RUNS = {"visible": [("identity", 0), ("identity", 1), ("identity", 2)],
        "hidden": [("payment", 3), ("payment", 4), ("schedule", 5), ("schedule", 6), ("identity", 7)]}


def probes(which):
    return [{"op": "session", "type": "identity", "seed": f"lab23:{i}", "dim": dim, "stub": "prober"}
            for dim, i in RUNS[which]]


def judge(which, got, base):
    bad = 0
    for g in got:
        end = (g.get("value") or {}).get("end") if g.get("ok") else None
        bad += 1 if not isinstance(end, dict) or end.get("reason") == "give_up" else 0
    return {"n": len(got), "failures": bad}
