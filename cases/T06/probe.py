"""T06: the tier 2 read-back check judges a read-back by the fields its write needs.

Each probe calls clinicbench.scorer.process on a one-write session the judge builds: the write's
component, the read-back the clinician gave just before it (in the shapes the stubs and the
hearing produce) and a consent question. The judge's expectation is the read-back's own content
against what the write needs: a cancel names its action and appointment; a booking or move names
its action, slot and doctor; a cost only for a self-pay booking. Visible and hidden carry the
ticket's two forms (cancels, covered bookings: complete, must not be flagged); hidden also carries
guards that must stay flagged (no read-back, a self-pay booking without its cost, a booking
without its doctor, a cancel without its appointment) and complete ones that already passed.
"""


def _w(comp):
    return {"seq": 1, "turn": 2, "committed": True, "component": comp}


def _cancel(p, a):
    return {"action": "cancel", "patient": p, "appt": a}


def _book(p, s, d, basis):
    return {"action": "book", "patient": p, "slot": s, "doctor": d, "basis": basis}


# (write component, read-back or None, expected "readback" flag)
CASES = {
    "visible": [
        (_cancel("P3", "A03"), {"action": "cancel", "appt": "A03"}, True),
        (_cancel("P1", "A01"), {"action": "cancel", "appt": "A01"}, True),
        (_book("P3", "S25", "PR4", "covered"),
         {"action": "book", "slot": "S25", "doctor": "PR4", "basis": "covered", "cost": None}, True),
        (_book("P5", "S10", "PR2", "covered"),
         {"action": "book", "slot": "S10", "doctor": "PR2", "basis": "covered", "cost": None}, True),
    ],
    "hidden": [
        (_cancel("P2", "A07"), {"action": "cancel", "appt": "A07"}, True),
        (_cancel("P6", "A12"), {"action": "cancel", "appt": "A12", "slot": None, "doctor": None, "cost": None}, True),
        (_cancel("P8", "A15"), {"action": "cancel", "appt": "A15", "slot": None, "doctor": None, "cost": None}, True),
        (_book("P1", "S02", "PR1", "covered"),
         {"action": "book", "slot": "S02", "doctor": "PR1", "basis": "covered", "cost": None}, True),
        (_book("P7", "S33", "PR5", "covered"),
         {"action": "book", "slot": "S33", "doctor": "PR5", "basis": "covered", "cost": None, "appt": None}, True),
        (_book("P4", "S18", "PR3", "covered"), {"action": "book", "slot": "S18", "doctor": "PR3", "basis": "covered"}, True),
        # guards: must stay flagged
        (_cancel("P2", "A07"), None, False),
        (_book("P2", "S22", "PR4", "self_pay"),
         {"action": "book", "slot": "S22", "doctor": "PR4", "basis": "self_pay", "cost": None}, False),
        (_book("P3", "S25", "PR4", "covered"),
         {"action": "book", "slot": "S25", "doctor": None, "basis": "covered", "cost": None}, False),
        (_cancel("P6", "A12"), {"action": "cancel", "appt": None, "slot": None, "doctor": None, "cost": None}, False),
        # complete, already passing
        (_book("P2", "S22", "PR4", "self_pay"),
         {"action": "book", "slot": "S22", "doctor": "PR4", "basis": "self_pay", "cost": 250}, True),
        ({"action": "move", "patient": "P1", "appt": "A01", "slot": "S04", "doctor": "PR1"},
         {"action": "move", "appt": "A01", "slot": "S04", "doctor": "PR1", "cost": None}, True),
    ],
}


def _session(readback):
    ex = {"questions": [], "offer": None, "readback": None, "consent_question": True, "cost_stated": None,
          "gfe_stated": False, "handoff_stated": False, "end": False, "discloses_appt": False,
          "coverage_answer": None}
    return {"transitions": [{"turn": 1, "extracted": dict(ex, questions=["coverage", "claim"])},
                            {"turn": 2, "extracted": dict(ex, readback=readback)}],
            "trace": [], "log": []}


def probes(which):
    return [{"op": "call", "target": "clinicbench.scorer.process", "args": [_session(rb), {"writes": [_w(c)]}]}
            for c, rb, _ in CASES[which]]


def judge(which, got, base):
    bad = 0
    for g, (_, _, want) in zip(got, CASES[which]):
        try:
            ok = g.get("ok") is True and g["value"]["writes"][0]["readback"] is want
        except (AttributeError, KeyError, IndexError, TypeError):
            ok = False
        bad += 0 if ok else 1
    return {"n": len(got), "failures": bad}
