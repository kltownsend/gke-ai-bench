"""T15: under arm F2 a patient who wants to move an appointment gets new bookings instead (a
finding). A failure means the defect showed: mid-move, a line that only mentions someone's
schedule turned the move into a booking ("Okay, setting that aside.") or a booking was written.

Visible: the recorded session from the ticket. Hidden: the other four recorded sessions with the
same patient line, replays of other "...'s schedule" lines into a move, and two controls: a real
request to schedule stays a booking request, and a patient who really does switch from a move to a
new booking still gets the switch. So a change that stops reading "schedule" at all, or that stops
any switch out of a move, doesn't pass. Every replay must also end in the move it asked for. Zero model calls: the driver
runs with a stub reader. Every setup step must succeed; a failed setup is a failure."""
PIVOT = "setting that aside"

SESSIONS = {"visible": [("reschedule_own", "lab23:8")],
            "hidden": [("reschedule_own", "lab23:11"), ("reschedule_own", "lab23:34"),
                       ("unconfirmed_reschedule", "lab23:3"), ("unconfirmed_reschedule", "lab23:51")]}
# (patient lines, appointment that must move, slot it must move to)
REPLAYS = {"visible": [],
           "hidden": [
               (["My name is Donald Knuth, born 1975-01-10.",
                 "I need to move my appointment with Dr. Okafor on Wednesday at 3:30 pm.",
                 "Thursday at 8:30 am works for me. That fits around my work schedule.",
                 "Yes, that's right, please go ahead."], "A08", "S21"),
               (["My name is Ada Lovelace, born 1990-01-01.",
                 "I need to move my appointment on Monday at 9:00 am.",
                 "Thursday at 1:00 pm, please. My son's schedule is tight that week.",
                 "Yes."], "A01", "S05"),
               (["My name is Margaret Hamilton, born 1992-08-17.",
                 "Can you move my appointment on Thursday at 2:00 pm?",
                 "Friday at 10:00 am suits my husband's schedule better.",
                 "Yes."], "A07", "S07")]}
CONTROLS = {"visible": [], "hidden": ["I'd like to schedule an appointment with dermatology."]}
# A real switch mid-move: the driver must still set the move aside for the new request.
SWITCH = {"visible": [], "hidden": [["My name is Grace Hopper, born 1985-05-05.",
                                     "I need to move my appointment on Tuesday at 11:30 am.",
                                     "Actually, forget that. I'd like to book a new appointment with Dr. Patel instead."]]}


def probes(which):
    out = [{"op": "session", "type": t, "seed": s, "dim": "payment", "arm": "F2", "model": "always_unclear",
            "clarity": "clear"} for t, s in SESSIONS[which]]
    out += [{"op": "replay", "arm": "F2", "model": "always_unclear", "lines": lines}
            for lines, _, _ in REPLAYS[which]]
    out += [{"op": "call", "target": "clinicbench.adapters.dcitl_port.parse_intents", "args": [u]}
            for u in CONTROLS[which]]
    out += [{"op": "replay", "arm": "F2", "model": "always_unclear", "lines": lines} for lines in SWITCH[which]]
    return out


def _booked(trace):
    return any(c.get("tool") == "book" and (c.get("result") or {}).get("ok") for c in trace)


def judge(which, got, base):
    ns, nr = len(SESSIONS[which]), len(REPLAYS[which])
    bad = 0
    for g in got[:ns]:
        v = g.get("value") if g.get("ok") else None
        if not isinstance(v, dict) or (v.get("end") or {}).get("reason") == "infra_error":
            bad += 1
            continue
        said = [x.get("text", "") for x in v.get("transcript", []) if x.get("speaker") == "clinician"]
        bad += 1 if (_booked(v.get("trace", [])) or any(PIVOT in s for s in said)) else 0
    for g, (_, appt, slot) in zip(got[ns:ns + nr], REPLAYS[which]):
        v = g.get("value") if g.get("ok") else None
        if not isinstance(v, dict):
            bad += 1
            continue
        moved = any(c.get("tool") == "move" and (c.get("result") or {}).get("ok")
                    and (c.get("args") or {}).get("appointment_id") == appt
                    and (c.get("args") or {}).get("new_slot_id") == slot for c in v.get("trace", []))
        bad += 1 if (_booked(v.get("trace", [])) or any(PIVOT in r for r in v.get("replies", [])) or not moved) else 0
    nc = len(CONTROLS[which])
    for g in got[ns + nr:ns + nr + nc]:
        bad += 0 if (g.get("ok") and isinstance(g.get("value"), list) and "book" in g["value"]) else 1
    for g in got[ns + nr + nc:]:
        v = g.get("value") if g.get("ok") else None
        replies = v.get("replies") if isinstance(v, dict) else None
        bad += 0 if (isinstance(replies, list) and len(replies) == 3 and PIVOT in replies[2]) else 1
    return {"n": len(got), "failures": bad}
