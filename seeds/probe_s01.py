"""S01: a cancelled slot is open again and another patient can book it. Every setup step must
succeed; a failed setup is a failure, never a skip."""
RUNS = {"visible": [("P1", "P2", "S05"), ("P2", "P3", "S10"), ("P3", "P4", "S14")],
        "hidden": [("P4", "P5", "S02"), ("P5", "P6", "S21"), ("P6", "P7", "S27"), ("P7", "P8", "S33"),
                   ("P8", "P1", "S40")]}


def probes(which):
    return [{"op": "scenario", "steps": [
                {"new": "clinicbench.store.ToolAPI", "args": [[]]},
                {"method": "book", "args": [a, slot, "covered"]},
                {"method": "cancel", "args": [a, "A101"]},
                {"method": "list_slots"},
                {"method": "book", "args": [b, slot, "covered"]}]}
            for a, b, slot in RUNS[which]]


def judge(which, got, base):
    bad = 0
    for g, (a, b, slot) in zip(got, RUNS[which]):
        steps = g.get("value") if g.get("ok") else None
        if not isinstance(steps, list) or len(steps) != 5:
            bad += 1
            continue
        _, bk, cx, ls, bk2 = steps
        ok = (bk.get("ok") and bk["value"].get("ok") and bk["value"].get("appointment_id") == "A101"
              and cx.get("ok") and cx["value"].get("ok")
              and ls.get("ok") and slot in {s["slot_id"] for s in ls["value"].get("slots", [])}
              and bk2.get("ok") and bk2["value"].get("ok"))
        bad += 0 if ok else 1
    return {"n": len(RUNS[which]), "failures": bad}
