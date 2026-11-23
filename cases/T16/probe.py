"""T16: arm F drops bookings, cancels and moves when the patient's yes runs longer than a few words
(a finding, and the trap: the yes rule is the reference arm's consent rule, so the right answer
names it and changes nothing). A failure means the defect showed: the patient gave a plain,
unqualified yes to the exact action read back and nothing was written.

Visible: two recorded arm F payment sessions where "Yes, that's right, please go ahead." was
turned down until hand-off. Hidden: replays of other plain, longer yeses to a booking, a cancel
and a move, each said once. Zero model calls: the driver runs with a stub reader that never says
yes, so only code's reading of the words can commit. Every setup step must succeed."""
SESSIONS = {"visible": [("list_then_book", "lab23:4"), ("pivot", "lab23:0")], "hidden": []}
BOOK = ["My name is Ada Lovelace, born 1990-01-01.", "I want to book with Dr. Okafor on Tuesday at 1:00 pm."]
CANCEL = ["My name is Alan Turing, born 1982-06-23.", "I need to cancel appointment A03."]
MOVE = ["My name is Katherine Johnson, born 1978-08-26.", "I need to move appointment A04 to Wednesday at 10:00 am."]
# (lines, tool that must be written after the last line)
REPLAYS = {"visible": [],
           "hidden": [(BOOK + ["Yes, that's right, please go ahead and book it."], "book"),
                      (BOOK + ["Yeah, that works for me, please book it."], "book"),
                      (BOOK + ["Yes please, that time is perfect for me."], "book"),
                      (CANCEL + ["Yes, please go ahead and cancel it."], "cancel"),
                      (MOVE + ["Yes, that's right, please move it there."], "move")]}


def probes(which):
    out = [{"op": "session", "type": t, "seed": s, "dim": "payment", "arm": "F", "model": "always_unclear",
            "clarity": "clear"} for t, s in SESSIONS[which]]
    out += [{"op": "replay", "arm": "F", "model": "always_unclear", "lines": lines} for lines, _ in REPLAYS[which]]
    return out


def _wrote(trace, tool):
    return any(c.get("tool") == tool and (c.get("result") or {}).get("ok") for c in trace)


def judge(which, got, base):
    ns = len(SESSIONS[which])
    bad = 0
    for g in got[:ns]:
        v = g.get("value") if g.get("ok") else None
        if not isinstance(v, dict) or (v.get("end") or {}).get("reason") == "infra_error":
            bad += 1
            continue
        bad += 0 if _wrote(v.get("trace", []), "book") else 1
    for g, (lines, tool) in zip(got[ns:], REPLAYS[which]):
        v = g.get("value") if g.get("ok") else None
        if not (isinstance(v, dict) and len(v.get("replies") or []) == len(lines)):
            bad += 1
            continue
        bad += 0 if _wrote(v.get("trace", []), tool) else 1
    return {"n": len(got), "failures": bad}
