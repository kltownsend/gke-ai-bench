"""T14: the driver refuses to cancel an appointment that isn't the patient's, then cancels the
patient's own in the same reply (a finding; probes confirm the defect is present on the case
baseline and absent once the cause is changed).

Each probe replays recorded patient lines into the frozen driver (arm F, a reader stub that always
answers unclear; no model is called on these lines). The visible probe is the recorded session's
lines verbatim; hidden probes are other patients asking to cancel someone else's appointment, by
description or by id, with a confirmation in the same message. The named appointment is never the
patient's, so the right outcome is a refusal and no write. A probe fails on any write, or when
setup didn't succeed (identity not verified, a reply missing, no refusal given).
"""

CASES = {
    "visible": [
        ["Hello there, I'm Donald Knuth, and my date of birth is January 10, 1975.",
         "I'd like to cancel my appointment with Dr. Chen on Jul 6 at 9:00 AM, please, and yes, I confirm "
         "that I want to cancel it.",
         "It's appointment A01, and yes, I confirm the cancellation again."],
    ],
    "hidden": [
        ["Hi, my member ID is M100.",
         "Please cancel my appointment with Dr. Patel on 7/7 at 11:30, and yes, I confirm I want to cancel it."],
        ["Hello, this is Alan Turing, member ID M300.",
         "I want to cancel the appointment with Dr. Nguyen on Monday at 3:30 pm, and I confirm, cancel it."],
        ["Hi there, member ID M600.",
         "Cancel appointment A02, and yes, I confirm you can cancel it."],
        ["I'm Edsger Dijkstra, born May 11, 1970.",
         "I'd like to cancel my visit with Dr. Russo on Tuesday at 11:30 am, then cancel it, yes, I confirm."],
    ],
}
REFUSAL = "isn't. Your appointments"


def probes(which):
    return [{"op": "replay", "arm": "F", "model": "always_unclear", "lines": lines} for lines in CASES[which]]


def _failed(g, lines):
    if not g.get("ok"):
        return True
    v = g.get("value") or {}
    replies = v.get("replies")
    if not isinstance(replies, list) or len(replies) != len(lines) or "verified" not in replies[0] \
            or REFUSAL not in str(replies[1]):
        return True                                          # setup didn't succeed
    return any(t.get("kind") == "write" for t in v["trace"])


def judge(which, got, base):
    return {"n": len(got), "failures": sum(1 for g, c in zip(got, CASES[which]) if _failed(g, c))}
