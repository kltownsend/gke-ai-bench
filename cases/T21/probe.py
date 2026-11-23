"""T21: a patient's question that mentions confirming is taken as consent, and the write goes
through (a finding; no recorded session shows it). A failure means the defect showed: the
patient asked a question before agreeing and the booking, cancel or move was written on it.

This is the opposite direction from T16: there a plain yes is turned down; here something that
isn't a yes is accepted. Visible: the reported line, read directly and replayed after a booking
read-back. Hidden: other questions that mention confirming, after a booking, a cancel and a move
read-back. Zero model calls: the driver runs with a stub reader that never says yes, so only
code's reading of the words can commit. Every setup step must succeed: the read-back has to be
on screen before the question."""
BOOK = ["My name is Ada Lovelace, born 1990-01-01.", "I want to book with Dr. Okafor on Tuesday at 1:00 pm."]
CANCEL = ["My name is Alan Turing, born 1982-06-23.", "I need to cancel appointment A03."]
MOVE = ["My name is Katherine Johnson, born 1978-08-26.", "I need to move appointment A04 to Wednesday at 10:00 am."]
READBACK = {"book": "Shall I book it?", "cancel": "Cancel A03", "move": "Move A04"}
CALLS = {"visible": ["Before I confirm, is that covered?"],
         "hidden": ["Before I confirm, what will it cost me?", "What happens if I confirm and then can't make it?",
                    "Should I confirm now, or can I call back?"]}
REPLAYS = {"visible": [(BOOK + ["Before I confirm, is that covered?"], "book")],
           "hidden": [(BOOK + ["Before I confirm, what will it cost me?"], "book"),
                      (BOOK + ["What happens if I confirm and then can't make it?"], "book"),
                      (CANCEL + ["Before I confirm, will I be charged for cancelling?"], "cancel"),
                      (MOVE + ["Can you tell me who I'd see there before I confirm?"], "move")]}


def probes(which):
    out = [{"op": "call", "target": "clinicbench.adapters.dcitl_port.explicit_confirm", "args": [u]} for u in CALLS[which]]
    out += [{"op": "replay", "arm": "F", "model": "always_unclear", "lines": lines} for lines, _ in REPLAYS[which]]
    return out


def judge(which, got, base):
    nc = len(CALLS[which])
    bad = sum(1 for g in got[:nc] if not g.get("ok") or g.get("value") is True)
    for g, (lines, tool) in zip(got[nc:], REPLAYS[which]):
        v = g.get("value") if g.get("ok") else None
        replies = v.get("replies") if isinstance(v, dict) else None
        if not (isinstance(replies, list) and len(replies) == len(lines) and READBACK[tool] in replies[-2]):
            bad += 1
            continue
        bad += 1 if any(c.get("tool") == tool and (c.get("result") or {}).get("ok") for c in v.get("trace", [])) else 0
    return {"n": len(got), "failures": bad}
