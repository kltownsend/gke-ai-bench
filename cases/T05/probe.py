"""T05: a pushy patient who isn't booking never pushes for a booking.

Each probe is the subject's driver session with the bench's hearing, against the scripted
clinician, which says a holding line the hearing reads as no question, so a
patient under a pressure setting pushes; after the script it hands off. The subject returns the
transcript, each patient line as the frozen driver reads it (adapters.dcitl_port.parse_intents,
the reading that started the second booking), and the truth's intents. Property, not pinned: the patient pushed at least once; no push line from a patient that
doesn't want a booking says "book it" or "put me in" or reads to the driver as a booking. A failed
setup is a failure, never a skip.
"""
HOLD = ["One moment, please.", "Bear with me a second.", "Still looking, one moment."]

# (patient type, truth seed, dimension, setting index with pressure != none)
RUNS = {
    "visible": [(t, f"shakedown:{i}", d, s) for t in ("reschedule_own", "unconfirmed_reschedule")
                for i in range(2) for d, s in (("payment", 1), ("schedule", 20))],
    "hidden": [(t, f"t05h:{i}", d, s) for t in ("reschedule_own", "unconfirmed_reschedule", "cancel_own",
                                                "eligibility_question", "someone_elses")
               for i in range(3) for d, s in (("payment", 8), ("doctor", 29), ("schedule", 43))],
}

def probes(which):
    return [{"op": "session", "type": t, "seed": seed, "dim": d, "setting": s, "script": HOLD,
             "read_lines": True} for t, seed, d, s in RUNS[which]]


def _view(v):
    """The subject's raw session, reshaped: the truth's intents, the end, and each patient line
    with its act and the frozen driver's reading of it."""
    pat = [x for x in v.get("transcript") or [] if isinstance(x, dict) and x.get("speaker") == "patient"]
    reads = v.get("driver_reads") or []
    if len(reads) != len(pat):
        return None
    return {"wants": v.get("intents"), "end": v.get("end"),
            "patient": [{"act": x.get("act"), "text": x.get("text"), "driver_reads": r} for x, r in zip(pat, reads)]}


def _bad(g):
    if not (isinstance(g, dict) and g.get("ok") is True and isinstance(g.get("value"), dict)):
        return True
    v = _view(g["value"])
    if v is None:
        return True
    wants, end, pat = v.get("wants"), v.get("end"), v.get("patient")
    if not (isinstance(wants, list) and wants and isinstance(pat, list) and isinstance(end, dict)
            and end.get("reason") not in (None, "infra_error")):
        return True
    if "book" in wants or "list_options" in wants:
        return True                       # not this case's patient: the probe set is misbuilt
    pushes = [x for x in pat if isinstance(x, dict) and x.get("act") == "insist"]
    if not pushes:
        return True
    for x in pushes:
        low = str(x.get("text") or "").lower()
        if "book it" in low or "put me in" in low or "book" in (x.get("driver_reads") or []):
            return True
    return False


def judge(which, got, base):
    return {"n": len(got), "failures": sum(1 for g in got if _bad(g))}
