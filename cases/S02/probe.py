"""S02: the holder can book their hold; nobody else can. The hold itself must succeed."""
RUNS = {"visible": [("P1", "P2", "S03"), ("P2", "P3", "S11")],
        "hidden": [("P3", "P4", "S07"), ("P4", "P5", "S18"), ("P5", "P6", "S25"), ("P6", "P7", "S38")]}


def probes(which):
    return [{"op": "scenario", "steps": [
                {"new": "clinicbench.store.ToolAPI", "args": [[]]},
                {"method": "hold", "args": [me, slot]},
                {"method": "book", "args": [other, slot, "covered"]},
                {"method": "book", "args": [me, slot, "covered"]}]}
            for me, other, slot in RUNS[which]]


def judge(which, got, base):
    bad = 0
    for g in got:
        steps = g.get("value") if g.get("ok") else None
        if not isinstance(steps, list) or len(steps) != 4:
            bad += 1
            continue
        _, hold, other, mine = steps
        ok = (hold.get("ok") and hold["value"].get("ok")
              and other.get("ok") and other["value"].get("ok") is False
              and mine.get("ok") and mine["value"].get("ok") is True)
        bad += 0 if ok else 1
    return {"n": len(RUNS[which]), "failures": bad}
