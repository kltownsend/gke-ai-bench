"""S06: an explicit "I confirm" is consent."""
LINES = {"visible": ["I confirm"], "hidden": ["I confirm, please book it", "ok i confirm", "i confirm the booking"]}


def probes(which):
    return [{"op": "call", "target": "clinicbench.adapters.dcitl_port.explicit_confirm", "args": [u]}
            for u in LINES[which]]


def judge(which, got, base):
    return {"n": len(got), "failures": sum(1 for g in got if not (g.get("ok") and g.get("value") is True))}
