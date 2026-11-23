"""Arm F2: the arm F driver with a different consent rule."""

from __future__ import annotations

import importlib.util
import re

from . import dcitl_port as D
from .dcitl_f import DcitlF
from .shim import services_for

REVERSAL = r"\bno\b|\bnope\b|hang on|\bmaybe\b|not sure|\bwait\b"
CLAIM_EXTRA = r"\bportal\b"     # "the portal said I'm all set": a claim Lab 23's CLAIM pattern misses


def clear_confirm(u: str) -> bool | None:
    """Lab 23's answer when it has one. Otherwise a reply that opens with an unqualified yes is
    consent, unless it carries a claim or a reversal; anything else is still unclear."""
    c = D.explicit_confirm(u)
    if c is not None:
        return c
    t = u.lower().strip()
    if re.search(D.CLAIM, t) or re.search(CLAIM_EXTRA, t) or re.search(REVERSAL, t):
        return None
    if re.match(r"^(yes|yep|yeah|confirmed)\b", t):
        return True
    return None


def _driver_module():
    spec = importlib.util.spec_from_file_location("clinicbench.adapters._dcitl_port_f2", D.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.explicit_confirm = clear_confirm
    return mod


D2 = _driver_module()


class DcitlF2(DcitlF):
    name = "dcitl_f2"
    CONSENT_IN_CODE = True

    def __init__(self, api, model):
        self.api = api
        self.model = model
        self.drv = D2.Driver(model, services_for(api), consent_in_code=True)
