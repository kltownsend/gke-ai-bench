"""T13: a patient asks to move an appointment by its id and the driver proposes moving a different
one, the patient's own (a finding; probes confirm the defect is present on the case baseline and
absent once the cause is changed).

Each probe replays recorded patient lines into the frozen driver under arm E with a reader stub
that says yes to every yes/no question (the recorded session's arm; the stub stands in for the
reader that judged the patient's yes a yes). The visible probe is the recorded session's lines
verbatim; hidden probes are other patients naming another patient's appointment id. A probe fails
when the driver proposes moving any appointment other than the one named, or writes anything at
all (the named appointment is never the patient's, so the right outcome is a refusal and no write),
or when setup didn't succeed (identity not verified, a reply missing).
"""
import re

# (lines, appointment the patient named)
CASES = {
    "visible": [
        (["Hi there, my member ID is M800.",
          "I'd like to move appointment A07 to Dr. Chen on 7/7 at eight thirty in the morning, please, "
          "and yes, I explicitly confirm the move.",
          "Yes, appointment A07, confirmed, go ahead and move it."], "A07"),
    ],
    "hidden": [
        (["Hi, my member ID is M100.",
          "I want to move appointment A03 to 7/8 at 10am, and I confirm the move.",
          "Yes, A03, go ahead and move it."], "A03"),
        (["Hello, member ID M200.",
          "I need to reschedule A05 to 7/8 at 9am.",
          "Yes, A05, please go ahead with that."], "A05"),
        (["Hi there, my member ID is M500.",
          "I'd like to move appointment A06 to 7/7 at 2pm, please.",
          "Yes, move A06 there, that's what I want."], "A06"),
        (["Hi, this is Alan Turing, member ID M300.",
          "Is it possible to move A08 to Monday at 1pm?",
          "Yes, that's A08, go ahead and move it."], "A08"),
        (["Hello, my member ID is M700.",
          "I'm hoping to move A01 to 7/10 at 10am.",
          "Yes, A01, confirmed."], "A01"),
    ],
}


def probes(which):
    return [{"op": "replay", "arm": "E", "model": "always_yes", "lines": lines} for lines, _ in CASES[which]]


def _failed(g, case):
    lines, named = case
    if not g.get("ok"):
        return True
    v = g.get("value") or {}
    replies = v.get("replies")
    if not isinstance(replies, list) or len(replies) != len(lines) or "verified" not in replies[0]:
        return True                                          # setup didn't succeed
    proposed = {m for r in replies for m in re.findall(r"\bMove (A\d+):", str(r))}
    if proposed - {named}:
        return True                                          # proposed moving another appointment
    return any(t.get("kind") == "write" for t in v["trace"])


def judge(which, got, base):
    return {"n": len(got), "failures": sum(1 for g, c in zip(got, CASES[which]) if _failed(g, c))}
