"""S03: a self-pay booking patient can afford the visit. Prices come from the judge's sealed
table, not the workspace; the truth must otherwise match the case baseline."""
PRICES = {"dermatology": 150, "cardiology": 300, "orthopedics": 250, "neurology": 350}
SEEDS = {"visible": ("lab23", 0, 100), "hidden": ("lab23", 100, 200)}   # same form, other numbers


def probes(which):
    prefix, start, n = SEEDS[which]
    return [{"op": "call", "target": "clinicbench.truth.build", "args": ["selfpay_booking", f"{prefix}:{i}"]}
            for i in range(start, start + n)]


def judge(which, got, base):
    bad = 0
    for g, b in zip(got, base):
        if not g.get("ok"):
            bad += 1
            continue
        t, ref = g["value"], b["value"]
        same = all(t.get(k) == ref.get(k) for k in ("id", "patient_id", "concern", "doctor", "intents"))
        price = PRICES.get((t.get("concern") or {}).get("specialty"))
        if not same or price is None or t["payment"].get("selfpay_max", 0) < price \
                or t["payment"].get("selfpay_willing") != "yes":
            bad += 1
    return {"n": len(got), "failures": bad}
