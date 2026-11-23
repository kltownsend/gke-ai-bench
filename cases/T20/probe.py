"""T20: a time said as "3:30 in the afternoon" is read as 3:30 in the morning, so a patient who
names their own afternoon appointment is told it isn't theirs (a finding). A failure means the
defect showed: the time came back as morning, or the patient's own appointment was refused or
left unchanged.

Specific to times said with "in the afternoon" and no am/pm (not the noon hour, which another
case covers). Visible: the reported line, read directly and replayed as a cancel. Hidden: other
afternoon times and phrasings, a move and a cancel on other patients, and one control ("in the
morning" must stay morning). Zero model calls. Every setup step must succeed."""
CALLS = {"visible": [("Wednesday at 3:30 in the afternoon", "15:30")],
         "hidden": [("1:00 in the afternoon", "13:00"), ("2:00 in the afternoon works for me", "14:00"),
                    ("Can we do Monday at 3:30 in the afternoon?", "15:30"),
                    ("at 1:00 in the afternoon, please", "13:00"),
                    ("8:30 in the morning", "08:30")]}                       # control
REFUSED = "isn't. Your appointments"
# (lines, tool, appointment that must be changed)
REPLAYS = {"visible": [(["My name is Donald Knuth, born 1975-01-10.",
                         "I need to cancel my appointment on Wednesday at 3:30 in the afternoon.",
                         "Yes, I confirm."], "cancel", "A08")],
           "hidden": [(["My name is Alan Turing, born 1982-06-23.",
                        "I need to cancel my appointment on Monday at 3:30 in the afternoon.",
                        "Yes, I confirm."], "cancel", "A03"),
                      (["My name is Barbara Liskov, born 1988-11-07.",
                        "Please cancel my Tuesday appointment at 3:30 in the afternoon.",
                        "Yes."], "cancel", "A06"),
                      (["My name is Margaret Hamilton, born 1992-08-17.",
                        "Can you move my appointment on Thursday at 2:00 in the afternoon?",
                        "Friday at 10:00 am.", "Yes."], "move", "A07")]}


def probes(which):
    out = [{"op": "call", "target": "clinicbench.adapters.dcitl_port.parse_time", "args": [u]} for u, _ in CALLS[which]]
    out += [{"op": "replay", "arm": "F", "model": "always_unclear", "lines": lines} for lines, _, _ in REPLAYS[which]]
    return out


def judge(which, got, base):
    nc = len(CALLS[which])
    bad = sum(1 for g, (_, want) in zip(got[:nc], CALLS[which]) if not (g.get("ok") and g.get("value") == want))
    for g, (lines, tool, appt) in zip(got[nc:], REPLAYS[which]):
        v = g.get("value") if g.get("ok") else None
        if not (isinstance(v, dict) and len(v.get("replies") or []) == len(lines) and "verified" in v["replies"][0]):
            bad += 1
            continue
        done = any(c.get("tool") == tool and (c.get("result") or {}).get("ok")
                   and (c.get("args") or {}).get("appointment_id") == appt for c in v.get("trace", []))
        refused = any(REFUSED in r for r in v["replies"])
        bad += 1 if (refused or not done) else 0
    return {"n": len(got), "failures": bad}
