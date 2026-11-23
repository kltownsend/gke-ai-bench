"""T12: a patient who confirms a move gets a second, new booking instead (a finding; probes confirm
the defect is present on the case baseline and absent once the cause is changed).

Each probe replays recorded patient lines into the frozen driver (arm F, a reader stub that always
answers unclear; the replies come from code, so no model is called). The visible probe is the
recorded session's patient lines verbatim; hidden probes are other patients, doctors and slots with
the same kind of confirmation. A probe passes only when the session's writes are exactly one move of
the patient's own appointment to the slot they chose. Any booking, any other write, or no move is a
failure; so is a setup step that didn't succeed (identity not verified, a reply missing).
"""

# (lines, own appointment, slot the patient chose)
CASES = {
    "visible": [
        (["Hi there, my member ID is M700.",
          "I'd like to move my appointment with Dr. Chen on 7/9 at 2pm to another time, but I haven't "
          "settled on a new time yet.",
          "Okay, let's go with Dr. Chen on 7/6 at 10am. I explicitly confirm that I want the appointment "
          "moved there.",
          "Yes."], "A07", "S02"),
    ],
    "hidden": [
        (["Hi, this is Ada Lovelace, member ID M100.",
          "I need to move my appointment with Dr. Chen on 7/6 at 9am, but I don't know the new time yet.",
          "Okay, let's go with Dr. Chen on 7/8 at 10am. I confirm I want the appointment moved there.",
          "Yes."], "A01", "S04"),
        (["Hello, my member ID is M200.",
          "Can you move my visit with Dr. Patel on 7/7 at 11:30? I haven't picked a new time.",
          "Let's go with Dr. Patel on 7/8 at 8:30am, and I want the appointment moved there.",
          "Yes."], "A02", "S10"),
        (["Hi, member ID M400 here.",
          "I want to move my appointment with Dr. Russo on Tuesday at 11:30am, not sure to when yet.",
          "Go with Dr. Russo on Friday at 2pm. Yes, I want my appointment moved to that time.",
          "Yes."], "A04", "S28"),
        (["I'm Barbara Liskov, born 11/07/1988.",
          "I'd like to move my appointment with Dr. Haddad on 7/7 at 3:30pm, but I need to check my "
          "calendar.",
          "Fine, with Dr. Haddad on 7/10 at 11:30 then. Please have the appointment moved.",
          "Yes."], "A06", "S41"),
    ],
}


def probes(which):
    return [{"op": "replay", "arm": "F", "model": "always_unclear", "lines": lines}
            for lines, _, _ in CASES[which]]


def _writes(value):
    return [(t["tool"], t["args"], bool((t.get("result") or {}).get("ok"))) for t in value["trace"]
            if t.get("kind") == "write"]


def _failed(g, case):
    lines, appt, slot = case
    if not g.get("ok"):
        return True
    v = g.get("value") or {}
    replies = v.get("replies")
    if not isinstance(replies, list) or len(replies) != len(lines) or "verified" not in replies[0]:
        return True                                          # setup didn't succeed
    want = [("move", {"appointment_id": appt, "new_slot_id": slot}, True)]
    got = [(tool, {k: a.get(k) for k in ("appointment_id", "new_slot_id")} if tool == "move" else a, ok)
           for tool, a, ok in _writes(v)]
    return got != want


def judge(which, got, base):
    return {"n": len(got), "failures": sum(1 for g, c in zip(got, CASES[which]) if _failed(g, c))}
