"""T17: clear self-pay choices the driver doesn't read as a choice (a finding; probes confirm the
defect is present on the case baseline and absent once the cause is changed)."""
PHRASES = {"visible": ["I'll self-pay", "I'll pay for it myself"],
           "hidden": ["I'll self-pay for this one", "I will self-pay", "Let's do self-pay",
                      "I'd like to self-pay"]}


def probes(which):
    return [{"op": "call", "target": "clinicbench.adapters.dcitl_port.explicit_selfpay", "args": [p]}
            for p in PHRASES[which]]


def judge(which, got, base):
    return {"n": len(got), "failures": sum(1 for g in got if not (g.get("ok") and g.get("value") is True))}
