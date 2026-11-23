"""S05: noon-hour pm times parse to 12:xx."""
CASES = {"visible": [("12:30 pm", "12:30")],
         "hidden": [("12:00 pm", "12:00"), ("12:45 p.m.", "12:45"), ("can you do 12:15pm", "12:15"),
                    ("1:30 pm", "13:30")]}


def probes(which):
    return [{"op": "call", "target": "clinicbench.adapters.dcitl_port.parse_time", "args": [u]}
            for u, _ in CASES[which]]


def judge(which, got, base):
    return {"n": len(got), "failures": sum(1 for g, (_, w) in zip(got, CASES[which])
                                            if not (g.get("ok") and g.get("value") == w))}
