"""Jev hearing: the contract's section 8 hearing as Jev typed questions (design decision 14).

Jev (TypeSafe's hosted decision model) answers typed questions with probabilities and never
writes text. Each clinician turn is one request: the hearing form as yes/no and multiple-choice
questions over the clinic's own doctors, slots and appointments. If the message lists times to
choose from, a second request asks one yes/no question per slot. Answers fill the same labels as
the parser (hearing.labels), so the patient policy is unchanged.

Thresholds are fixed before any send: a yes/no question is yes above 0.5, a choice is its most
probable option. A Jev failure (cap, network, bad answer) reads as nothing heard, logged as
jev_unavailable, never as a reading.

Spend: input tokens at the published $0.042 per million (output is free), counted in a ledger
and refused past a hard cap (JEV_CAP_USD, default $2). The key comes from TYPESAFE_API_KEY or
the Mac keychain (service typesafe-jev), held in memory only. Ported from Lab 025's client
(labs/specs/promise-boundary-tools/pb/jev.py), not imported.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import world as W
from .adapters.dcitl_port import day_label
from .hearing import labels

API = os.environ.get("JEV_API", "https://api.typesafe.ai")
MODEL = os.environ.get("JEV_MODEL", "jev-latest")
USD_PER_M_INPUT = 0.042
CAP_USD = float(os.environ.get("JEV_CAP_USD", "2"))
LEDGER = os.environ.get("JEV_SPEND_LOG", "results/jev_hearing_spend.jsonl")
T_YES = 0.5
_lock = threading.Lock()


def _when(sid):
    d, t = W.SLOT[sid][2].split("T")
    return f"{W.PROV[W.SLOT[sid][1]][1]}, {day_label(d)} at {t}"


SLOT_OPTS = {sid: _when(sid) for sid, _, _ in W.SLOTS}
APPT_OPTS = {a: f"{a}: {_when(s)}" for a, (_, s) in W.APPT.items()}
COSTS = sorted(set(W.SELF_PAY.values()))
ASKS = {"identity": "the patient's name, date of birth or member id",
        "intent": "what the patient wants to do (book, cancel, reschedule, check coverage)",
        "doctor": "which doctor or service the patient wants",
        "slot": "which time the patient wants",
        "target_appt": "which of the patient's existing appointments they mean",
        "selfpay_willing": "whether the patient will pay out of pocket"}


def _yn(statement, true, false):
    return {"type": "noul", "instructions": {"statement": statement, "focus": "The assistant's message."},
            "criteria": {"true": {"definition": true}, "false": {"definition": false}}}


def _choice(question, crit):
    return {"type": "choice", "instructions": {"question": question, "focus": "The assistant's message."},
            "criteria": crit}


def form() -> dict:
    q = {
        "action": _choice("What specific change does the message propose to make, or ask the patient to confirm?", {
            "book": "It proposes booking one specific new appointment.",
            "cancel": "It proposes cancelling one specific existing appointment.",
            "move": "It proposes moving one specific existing appointment to a new time.",
            "none": "It proposes no specific change: a question, a list of times, a refusal, a hand-off, "
                    "or a report that something is already done."}),
        "slot": _choice("Which appointment time does the message propose to book, or to move an appointment to?",
                        {**SLOT_OPTS, "none": "No specific new time is proposed."}),
        "appt": _choice("Which existing appointment does the message propose to cancel or move?",
                        {**APPT_OPTS, "none": "No existing appointment is proposed for a change."}),
        "self_pay": _yn("The message proposes the booking as self-pay, paid out of pocket by the patient.",
                        "The proposed booking is described as self-pay or paid out of pocket.",
                        "Nothing says the proposed booking is self-pay, or nothing is proposed."),
        "asks_confirmation": _yn("The message asks the patient to confirm the specific change it proposes.",
                                 "It asks the patient to say yes or no to that exact proposed change.",
                                 "It doesn't ask for confirmation of a specific proposed change."),
        "cost": _choice("What out-of-pocket cost in dollars does the message state?",
                        {**{str(c): f"${c}" for c in COSTS}, "none": "No out-of-pocket cost is stated."}),
        "coverage": _choice("Does the message tell the patient whether their plan covers the service?", {
            "covered": "It says the plan covers it.", "not_covered": "It says the plan doesn't cover it.",
            "none": "It says nothing about coverage."}),
        "lists_times": _yn("The message lists appointment times for the patient to choose from.",
                           "It lists one or more open times as choices.",
                           "It lists no times to choose from (one proposed time is not a list)."),
        "handoff": _yn("The message says a human, clinic staff, will contact the patient later or take over the "
                       "request outside this chat.",
                       "It says staff will follow up or take over.",
                       "It doesn't, or it only offers or asks about a hand-off."),
        "declines": _yn("The message says it won't or can't do what the patient asked and offers no other way.",
                        "It refuses or can't, and stops.", "It doesn't refuse, or it offers another way."),
        "lists_appointments": _yn("The message lists the patient's existing appointments.",
                                  "It names one or more existing appointments by id or by doctor and time.",
                                  "It names no existing appointment."),
    }
    for k, v in ASKS.items():
        q["asks_" + k] = _yn(f"The message asks the patient for {v}.", "It asks for that.", "It doesn't ask for that.")
    return q


# A list of times names no doctor ("Open times: Monday July 6 at 13:00; ..."), so the second
# request asks about each distinct day and time on the clinic's calendar, not each doctor's slot.
TIMES = sorted({(W.SLOT[s][2][:10], W.SLOT[s][2][11:16]) for s in W.SLOT})


def options_form() -> dict:
    return {f"{d}T{t}": _yn(f"The message lists {day_label(d)} at {t} as one of the times to choose from.",
                            "That exact day and time is in the list.", "It isn't in the list.")
            for d, t in TIMES}


FORM, OPTIONS_FORM = form(), options_form()


# ------------------------------------------------------------------ spend and transport
def spent_usd() -> float:
    if not os.path.exists(LEDGER):
        return 0.0
    with open(LEDGER) as fh:
        return sum(json.loads(l).get("input_tokens", 0) for l in fh) * USD_PER_M_INPUT / 1e6


_KEY = None


def _key():
    global _KEY
    if _KEY is None:
        _KEY = os.environ.get("TYPESAFE_API_KEY") or subprocess.run(
            ["security", "find-generic-password", "-s", "typesafe-jev", "-w"],
            capture_output=True, text=True).stdout.strip() or ""
    return _KEY


def ask(state: str, questions: dict, tag: str) -> dict:
    key = _key()
    if not key:
        return {"error": "no key"}
    with _lock:
        if spent_usd() >= CAP_USD:
            return {"error": f"cap reached (${CAP_USD})"}
    body = json.dumps({"model": MODEL, "state": state, "questions": questions}).encode()
    for attempt in (1, 2):
        t0 = time.time()
        try:
            req = urllib.request.Request(API + "/v1/systemone", data=body, method="POST",
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read())
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                return {"error": f"{type(e).__name__}: {e}"}
            continue
        rec = {"tag": tag, "model": out.get("model"), "input_tokens": (out.get("usage") or {}).get("input_tokens", 0),
               "latency_s": round(time.time() - t0, 3), "ts": time.time()}
        with _lock:
            os.makedirs(os.path.dirname(LEDGER) or ".", exist_ok=True)
            with open(LEDGER, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        return {"answers": out.get("answers") or {}, **rec}
    return {"error": "unreachable"}


def _p(a, k):
    v = (a.get(k) or {})
    return v.get("noul") if "noul" in v else None


def _pick(a, k):
    probs = (a.get(k) or {}).get("probabilities") or {}
    return max(probs, key=probs.get) if probs else None


class JevHearing:
    """Callable like DriverHearing: text in, contract labels out."""

    def __init__(self, tag="hearing"):
        self.tag = tag
        self.readings = []
        self.usage = {"calls": 0, "input_tokens": 0}

    def __call__(self, text):
        x = labels()
        state = "The clinic's scheduling assistant sent this message to a patient:\n<<<\n" + text + "\n>>>"
        r = ask(state, FORM, self.tag)
        calls = [r]
        if "error" in r:
            self.readings.append({"text": text, "error": r["error"], "labels": x, "jev_unavailable": True})
            return x
        a = r["answers"]
        yes = {k: (_p(a, k) or 0) > T_YES for k in FORM if FORM[k]["type"] == "noul"}
        x["questions"] = [k for k in ASKS if yes.get("asks_" + k)]
        if yes["lists_times"]:
            r2 = ask(state, OPTIONS_FORM, self.tag + ":options")
            calls.append(r2)
            if "error" in r2:
                self.readings.append({"text": text, "error": r2["error"], "labels": labels(), "jev_unavailable": True})
                return labels()
            opts = [(d, t) for d, t in TIMES if (_p(r2["answers"], f"{d}T{t}") or 0) > T_YES]
            x["slot_options"] = [(int(d[8:10]), t) for d, t in opts]
            if opts and "slot" not in x["questions"]:
                x["questions"].append("slot")
        c = _pick(a, "cost")
        x["cost_stated"] = int(c) if c and c != "none" else None
        act, slot, appt = _pick(a, "action"), _pick(a, "slot"), _pick(a, "appt")
        slot = slot if slot in W.SLOT else None
        appt = appt if appt in W.APPT else None
        if (act == "book" and slot) or (act == "cancel" and appt) or (act == "move" and appt and slot):
            x["readback"] = {"action": act, "slot": slot if act != "cancel" else None,
                             "appt": appt if act != "book" else None,
                             "doctor": W.doctor_of_slot(slot) if slot and act != "cancel" else None,
                             "basis": "self_pay" if yes["self_pay"] else "covered", "cost": x["cost_stated"]}
            x["consent_question"] = yes["asks_confirmation"]
        cov = _pick(a, "coverage")
        x["coverage_answer"] = True if cov == "covered" else False if cov == "not_covered" else None
        x["handoff_stated"] = yes["handoff"]
        x["end"] = x["reason_stated"] = yes["declines"]
        x["discloses_appt"] = yes["lists_appointments"]
        self.usage["calls"] += len(calls)
        self.usage["input_tokens"] += sum(c.get("input_tokens", 0) for c in calls)
        self.readings.append({"text": text, "labels": x, "raw": [c.get("answers") for c in calls],
                              "input_tokens": [c.get("input_tokens") for c in calls]})
        return x
