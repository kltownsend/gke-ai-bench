"""Jev hearing's answer mapping, with the network call stubbed (no spend)."""

from __future__ import annotations

from clinicbench import jev_hearing as J


def _ans(**kw):
    a = {k: {"type": "noul", "noul": 0.0} for k, q in J.FORM.items() if q["type"] == "noul"}
    for k in ("action", "slot", "appt", "cost", "coverage"):
        a[k] = {"type": "choice", "probabilities": {"none": 1.0}}
    for k, v in kw.items():
        a[k] = {"type": "noul", "noul": v} if isinstance(v, float) else {"type": "choice", "probabilities": {v: 0.9, "none": 0.1}}
    return a


def test_booking_proposal_maps_to_a_readback(monkeypatch):
    monkeypatch.setattr(J, "ask", lambda s, q, tag: {"answers": _ans(action="book", slot="S27", asks_confirmation=0.9), "input_tokens": 1})
    x = J.JevHearing()("I can book Dr. Russo on Thursday July 9 at 08:30. Shall I book it? (yes / no)")
    assert x["readback"] == {"action": "book", "slot": "S27", "appt": None, "doctor": "PR4", "basis": "covered", "cost": None}
    assert x["consent_question"] is True and x["slot_options"] == []


def test_list_of_times_asks_the_second_form(monkeypatch):
    calls = []

    def ask(s, q, tag):
        calls.append(tag)
        if q is J.OPTIONS_FORM:
            return {"answers": {"2026-07-06T13:00": {"noul": 0.9}}, "input_tokens": 1}
        return {"answers": _ans(lists_times=0.9), "input_tokens": 1}

    monkeypatch.setattr(J, "ask", ask)
    x = J.JevHearing()("Open times: Monday July 6 at 13:00. Which one would you like?")
    assert x["slot_options"] == [(6, "13:00")] and "slot" in x["questions"] and len(calls) == 2


def test_failure_reads_as_nothing_heard(monkeypatch):
    monkeypatch.setattr(J, "ask", lambda s, q, tag: {"error": "cap reached ($2)"})
    h = J.JevHearing()
    x = h("Cancel A01? (yes / no)")
    assert x["readback"] is None and h.readings[-1]["jev_unavailable"]
