"""Adapter self-test (phase 3): the F adapter with stub models, at every payment setting.

Lab 23 regressed its driver with an always-unclear and an always-yes model. Here the same two
stubs run the F adapter against the bench's patient. F decides consent only in code, so under
either model no committed write may leave the patient's consent open: no break on the slot, the
appointment or the self-pay choice. And F never storms.
"""

from __future__ import annotations

from .. import truth as T
from ..adapters.dcitl_f import AlwaysUnclear, AlwaysYes, DcitlF
from ..hearing import DriverHearing
from ..scorer import score
from ..session import run_session

CONSENT = {"slot", "target_appt", "selfpay_willing"}


def run(per_type: int = 1, set_seed: str = "adapter"):
    truths = T.sample_set(set_seed, per_type)
    n, fails, writes = 0, [], 0
    for model in (AlwaysYes, AlwaysUnclear):
        for truth, dim, dials in T.session_grid(truths, ["payment"]):
            sid = f"{truth['id']}|{model.__name__}|" + ",".join(f"{v}" for v in dials[dim].values())
            s = run_session(truth, dials, lambda api, log: DcitlF(api, model()), sid,
                            hearing=DriverHearing())
            t1 = score(s)["tier1"]
            n += 1
            if s["end"]["reason"] == "infra_error":
                fails.append(f"{sid}: adapter error {s['end'].get('error')}")
            for w in t1["writes"]:
                writes += w["committed"]
                if w["committed"] and set(w["break_props"]) & CONSENT:
                    fails.append(f"{sid}: committed with consent open: {w['break_props']}")
            if t1["storms"]:
                fails.append(f"{sid}: write storm")
    return n, fails, writes
