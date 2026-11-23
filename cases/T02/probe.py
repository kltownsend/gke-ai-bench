"""T02: the patient never answers a clinic question with an empty line.

Each probe is the subject's driver session with the bench's hearing of clinician text, against
either the scripted clinician (it says the given lines in order, then hands off) or the F adapter
with the AlwaysYes stub reader (no server). The subject returns the raw transcript; the judge
decides. Property, not pinned: every patient line has at least one letter in it, and the scripted
lines were all said (the session didn't end before the question). A failed setup is a failure.
"""
MOVE = "Which appointment would you like to move?"
TIMES = "Open times: Monday July 6 at 13:00; Tuesday July 7 at 11:30. Which one would you like?"
VERIFIED = "Thanks, Ada. One moment while I pull up your record."

# (patient type, truth seed, payment setting index, scripted clinician lines or "F")
RUNS = {
    "visible": [(t, f"pilot:{i}", s, [MOVE]) for t in ("selfpay_booking", "covered_booking")
                for i in range(3) for s in (0, 18)],
    "hidden": [(t, f"t02h:{i}", s, [MOVE]) for t in ("selfpay_booking", "covered_booking",
                                                     "ineligible_coverage", "eligibility_question")
               for i in range(3) for s in (7, 33)]
    + [(t, f"t02h:{i}", 12, [VERIFIED, MOVE]) for t in ("selfpay_booking", "list_then_book") for i in range(3)]
    + [("eligibility_question", f"t02h:{i}", s, [TIMES]) for i in range(3) for s in (0, 25)]
    + [("eligibility_question", f"t02h:{i}", s, "F") for i in range(3, 6) for s in (0, 40)],
}

def probes(which):
    out = []
    for t, seed, s, lines in RUNS[which]:
        req = {"op": "session", "type": t, "seed": seed, "dim": "payment", "setting": s}
        req.update({"arm": "F", "model": "always_yes"} if lines == "F" else {"script": list(lines)})
        out.append(req)
    return out


def _bad(g, lines):
    if not (isinstance(g, dict) and g.get("ok") is True and isinstance(g.get("value"), dict)):
        return True
    tr, end = g["value"].get("transcript"), g["value"].get("end")
    if not isinstance(tr, list) or not isinstance(end, dict) or end.get("reason") in (None, "infra_error"):
        return True
    if lines != "F":
        said = [x.get("text") for x in tr if x.get("speaker") == "clinician"]
        if said[:len(lines)] != list(lines):
            return True
    pat = [x for x in tr if x.get("speaker") == "patient"]
    return len(pat) < 2 or any(not any(ch.isalpha() for ch in str(x.get("text") or "")) for x in pat)


def judge(which, got, base):
    return {"n": len(got), "failures": sum(1 for g, r in zip(got, RUNS[which]) if _bad(g, r[3]))}
