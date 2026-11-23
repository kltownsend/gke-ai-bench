"""Two models talking, code deciding (docs/proposals/2026-10-26-two-model-conversation.md).

Code decides on both sides, exactly as in the F cells: the DCITL driver picks every clinician act
and decides consent; the disclosure policy picks every patient act from the truth and dials. The
only new thing is language between them:

- model A words each driver line in context (F-realize: the act is fixed, only the words change);
- model B voices each patient act in the patient's communication variables;
- the patient hears model A's words through the model hearing, not the driver's template parser;
- the driver reads model B's words with its own interpreter (model A), as it always has.

Every turn keeps both ends of each step, so the loss is computable per step:

1. driver template -> model A's line (clinician fidelity: logged, not checked here; Jev pending)
2. model A's line -> what the patient heard. The same hearing model also reads the template, so
   translation loss is (heard template) against (heard words) and the reader's own bias cancels;
   the reader's disagreement with the template parser is reported apart, as hearing error
3. template patient line -> model B's line (patient fidelity: logged, not checked here; Jev pending)
4. model B's line -> the driver's reading, in the driver's own interpretation log

Nothing here gates a line: the words are logged and passed on, so translation loss shows up in the
scores. A model error falls back to the template line and is flagged.

The frozen phase 3 files are untouched. This module has its own session loop because session.py
passes the patient's structured act to the clinician and builds it with the disclosure log (the
truth side channel from the role-based review); here the clinician is built from a tool facade
that exposes only the clinic's tools and records, and gets only the patient's text.

usage: python -m clinicbench.twomodel --out results/NAME --a-base URL --b-base URL \
           [--hearing-base URL] [--batch smoke|canary|pilot] [--arms twomodel,templates] [--par 4]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import scorer as S
from .adapters import dcitl_port as D
from .adapters.dcitl_f import DcitlF
from .hearing import DriverHearing
from .live import canary_grid, pilot_grid
from .model_hearing import ModelHearing
from .policy import PatientPolicy
from .records import COMMIT_TOOLS, TURN_CAP
from .runner import _fname, write_atomic
from .scorer import score
from .session import _intents_satisfied, run_session
from .store import ToolAPI
from .truth import apply_dials
from .voice import Voice

MODEL = "gemma-4-26b-a4b"
LOSS_FIELDS = ("questions", "readback", "consent_question", "cost_stated", "handoff_stated", "end",
               "coverage_answer", "slot_options")


# ------------------------------------------------------------------ the side-channel fix
class ToolFacade:
    """What a clinician gets: the clinic's tools and its slot and appointment records. Not the
    disclosure log (ToolAPI.log), the truth, the dials or the patient's act. The tools are closures,
    so no attribute of the facade leads back to the ToolAPI (tests/test_twomodel.py walks it)."""

    TOOLS = ("find_patient", "check_coverage", "list_slots", "book", "cancel", "move", "hold", "hand_off")

    def __init__(self, api: ToolAPI):
        self.store = api.store
        for name in self.TOOLS:
            fn = getattr(api, name)
            setattr(self, name, lambda *a, _fn=fn, **k: _fn(*a, **k))
        self._turn = lambda: api.turn

    @property
    def turn(self):
        return self._turn()


# ------------------------------------------------------------------ the two voices
def _chat(base, system, user, seed, max_tokens=400, model=MODEL):
    body = {"model": model, "seed": seed, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        out = json.load(r)
    u = out.get("usage") or {}
    return (out["choices"][0]["message"].get("content") or "").strip(), u


def _history(transcript, n=6):
    return "\n".join(f"{x['speaker'].capitalize()}: {x['text']}" for x in transcript[-n:]) or "(the call just started)"


CLIN_SYSTEM = (
    "You are the voice of a clinic's scheduling assistant on a patient call. The scheduling software "
    "decides what to say; you say it in your own words, the way a person at the front desk would talk, "
    "not by copying it. Keep every fact exactly: every "
    "doctor, day, time, dollar amount, appointment id, and every question it asks. Add nothing: no new "
    "offers, times, costs, promises or questions, and don't drop any. If it asks for a yes or no, still "
    "ask for a yes or no. Reply with the message only.")

PATIENT_SYSTEM = (
    "You are a patient calling a clinic. Each turn you're given exactly what to say. Say it in your own "
    "words, the way this patient talks, not by copying it. Keep exactly what it says: the same choices, the same yes or no, "
    "the same level of certainty or confusion, the same names, days, times and amounts. Add nothing it "
    "doesn't say: don't answer questions it doesn't answer, don't agree to anything it doesn't agree to, "
    "don't give details it doesn't give. Reply with the line only.")


def _manner(comm):
    return (f"directness {comm['directness']}, verbosity {comm['verbosity']}, precision {comm['precision']}, "
            f"fluency {comm['fluency']}, patience {comm['patience']}")


class Realizer:
    """Model A: words the driver's line. Falls back to the template on an error or empty reply."""

    def __init__(self, base, seed):
        self.base, self.seed = base, seed
        self.usage = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "fallbacks": 0}

    def __call__(self, template, transcript):
        user = (f"The conversation so far:\n{_history(transcript)}\n\n"
                f"What the software says next:\n<<<\n{template}\n>>>")
        try:
            line, u = _chat(self.base, CLIN_SYSTEM, user, self.seed)
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
            self.usage["output_tokens"] += u.get("completion_tokens", 0)
        except Exception:  # noqa: BLE001 - a voice failure falls back to the template, flagged
            line = ""
        if not line:
            self.usage["fallbacks"] += 1
            return template, True
        return line, False


class PatientVoice:
    """Model B: voices the decided patient line in the patient's manner."""

    def __init__(self, base, seed, comm, model=MODEL):
        self.base, self.seed, self.comm, self.model = base, seed, comm, model
        self.usage = {"calls": 0, "prompt_tokens": 0, "output_tokens": 0, "fallbacks": 0}

    def __call__(self, template, transcript):
        user = (f"How you talk: {_manner(self.comm)}.\n\nThe conversation so far:\n{_history(transcript)}\n\n"
                f"What to say now:\n<<<\n{template}\n>>>")
        try:
            line, u = _chat(self.base, PATIENT_SYSTEM, user, self.seed, model=self.model)
            self.usage["calls"] += 1
            self.usage["prompt_tokens"] += u.get("prompt_tokens", 0)
            self.usage["output_tokens"] += u.get("completion_tokens", 0)
        except Exception:  # noqa: BLE001
            line = ""
        if not line:
            self.usage["fallbacks"] += 1
            return template, True
        return line, False


def _diff(a, b):
    return sorted(k for k in LOSS_FIELDS if
                  (sorted(a.get(k) or []) if k in ("questions", "slot_options") else a.get(k)) !=
                  (sorted(b.get(k) or []) if k in ("questions", "slot_options") else b.get(k)))


# ------------------------------------------------------------------ one conversation
def _verbatim(template, transcript):
    return template, False


def run_twomodel_session(truth, dials, session_id, a_base, b_base, hearing_base, seed=1, translate=True,
                         b_model=MODEL, hearing_model=MODEL):
    """`translate=False` is the hearing-matched baseline: template lines both ways, heard by the
    same hearing model, so the only difference from the two-model arm is the models' words."""
    t = apply_dials(truth, dials)
    log: list[dict] = []
    api = ToolAPI(log)
    policy = PatientPolicy(t, log)
    voice = Voice(t, session_id)
    model = D.Model(f"openai/{MODEL}", a_base, seed)
    clinician = DcitlF(ToolFacade(api), model)
    realize, speak = Realizer(a_base, seed), PatientVoice(b_base, seed, t["comm"], b_model)
    if not translate:
        realize, speak = _verbatim, _verbatim
    parser, hear = DriverHearing(), ModelHearing(hearing_base, model=hearing_model, seed=seed)
    transcript, transitions, steps = [], [], []

    act = policy.opening()
    tmpl = voice.say(0, act["parts"])
    text, fb = speak(tmpl, transcript)
    transcript.append({"turn": 0, "speaker": "patient", "text": text, "act": act["entry"]["act"]})
    steps.append({"turn": 0, "side": "patient", "template": tmpl, "said": text, "fallback": fb,
                  "verbatim": text == tmpl})
    end = None
    for turn in range(1, TURN_CAP + 1):
        api.turn = turn
        n_before = len(api.trace)
        try:
            ctmpl, _ = clinician.turn(text)             # text only: no act, no log
            ctext, cfb = realize(ctmpl, transcript)
            from_tmpl = parser(ctmpl)
            heard_tmpl = hear(ctmpl) if ctext != ctmpl else None
            heard = hear(ctext)
            heard_tmpl = heard if heard_tmpl is None else heard_tmpl
        except Exception as e:  # noqa: BLE001 - a crash or a model/server error is infrastructure
            end = {"reason": "infra_error", "by": "infra", "turns": turn, "error": f"{type(e).__name__}: {e}"[:500]}
            break
        transcript.append({"turn": turn, "speaker": "clinician", "text": ctext})
        steps.append({"turn": turn, "side": "clinician", "template": ctmpl, "said": ctext, "fallback": cfb,
                      "verbatim": ctext == ctmpl, "code_act": from_tmpl, "heard_template": heard_tmpl, "heard": heard,
                      "translation_loss": _diff(heard_tmpl, heard), "hearing_error": _diff(from_tmpl, heard_tmpl)})
        calls = api.trace[n_before:]
        tr = {"turn": turn, "clinician_text_ref": len(transcript) - 1, "extracted": heard,
              "store_validation": [{"seq": c["seq"], "tool": c["tool"], "ok": c["result"]["ok"],
                                    "refusal": c["result"]["refusal"]} for c in calls],
              "patient_act": None, "disclosure_ids": []}
        transitions.append(tr)
        if api.handed_off:
            end = {"reason": "hand_off", "by": "clinician", "turns": turn}
            break
        written = [S.component(c) for c in calls if c["tool"] in COMMIT_TOOLS and c["result"]["ok"]]
        acc = S.acceptable_at(t, turn)
        wrong = [c for c in written if not any(S.matches(c, e) for e in acc)]
        if heard.get("offer") and any(heard["offer"].get(k) == c.get(k) for c in written
                                      for k in ("slot", "appt") if heard["offer"].get(k)):
            heard = dict(heard, offer=None)
        n_log = len(log)
        act = policy.respond(turn, heard, _intents_satisfied(t, api.trace, turn), wrong)
        tr["patient_act"] = act["entry"]["act"]
        tr["disclosure_ids"] = [d["id"] for d in log[n_log:]]
        tmpl = voice.say(turn, act["parts"])
        text, fb = speak(tmpl, transcript)
        transcript.append({"turn": turn, "speaker": "patient", "text": text, "act": act["entry"]["act"]})
        steps.append({"turn": turn, "side": "patient", "template": tmpl, "said": text, "fallback": fb,
                      "verbatim": text == tmpl})
        if policy.ended:
            end = {"reason": act["entry"]["act"], "by": "patient", "turns": turn}
            break
        if heard.get("end"):
            end = {"reason": "clinician_end", "by": "clinician", "turns": turn}
            break
    if end is None:
        end = {"reason": "turn_cap", "by": "cap", "turns": TURN_CAP}
    return {"session_id": session_id, "stub": "twomodel_f" if translate else "templates_mh", "usage": model.usage, "escalations": model.log,
            "voice_usage": {"clinician": getattr(realize, "usage", None), "patient": getattr(speak, "usage", None),
                            "hearing": hear.usage},
            "truth": {k: t[k] for k in ("id", "seed", "type", "patient_id", "acceptable", "handoff_ok",
                                         "requires_handoff", "dials", "comm", "intents")},
            "log": log, "trace": api.trace, "transitions": transitions, "transcript": transcript,
            "steps": steps, "hearing_readings": hear.readings, "end": end}


def step_loss(s):
    """Per-step loss for one session. Steps 1 and 3 need the fidelity checks (Jev, unpriced), so
    they're counted only as fallbacks here; step 2 and the driver's reads are computed."""
    clin = [x for x in s.get("steps", []) if x["side"] == "clinician"]
    pat = [x for x in s.get("steps", []) if x["side"] == "patient"]
    reads = [e for e in s.get("escalations") or [] if e["kind"] != "identity_extract"]
    return {"clinician_lines": len(clin), "clinician_fallbacks": sum(x["fallback"] for x in clin),
            "clinician_verbatim": sum(x["verbatim"] for x in clin),
            "translation_loss_turns": sum(bool(x["translation_loss"]) for x in clin),
            "translation_loss_fields": sorted({f for x in clin for f in x["translation_loss"]}),
            "hearing_error_turns": sum(bool(x["hearing_error"]) for x in clin),
            "hearing_error_fields": sorted({f for x in clin for f in x["hearing_error"]}),
            "patient_lines": len(pat), "patient_fallbacks": sum(x["fallback"] for x in pat),
            "patient_verbatim": sum(x["verbatim"] for x in pat),
            "driver_reads": len(reads), "driver_unclear": sum(e["answer"] == "unclear" for e in reads)}


# ------------------------------------------------------------------ runner
def sid_for(truth, dials, arm, dim="payment"):
    d = dials[dim]
    return f"{truth['id']}|{dim}|c={d['clarity']},p={d['probes']},r={d['pressure']}|{arm}"


def run_one(truth, dials, arm, a, out):
    sid = sid_for(truth, dials, arm)
    path = os.path.join(out, _fname(sid))
    if os.path.exists(path):
        return "skipped"
    t0 = time.time()
    if arm in ("twomodel_f", "templates_mh"):
        s = run_twomodel_session(truth, dials, sid, a.a_base, a.b_base, a.hearing_base or a.b_base,
                                 translate=arm == "twomodel_f", b_model=a.b_model, hearing_model=a.hearing_model)
        extra = {"step_loss": step_loss(s)}
    else:                                   # "templates": F as in the cells, heard by the template parser
        s = run_session(truth, dials, lambda api, log: DcitlF(ToolFacade(api), D.Model(f"openai/{MODEL}", a.a_base, 1)),
                        sid, hearing=DriverHearing())
        extra = {}
    s["wall_clock_s"] = round(time.time() - t0, 2)
    if s["end"]["reason"] == "infra_error":
        write_atomic(path.replace(".json", ".infra.json"), {"session": s})
        return "infra"
    write_atomic(path, {"session": s, "score": score(s), **extra})
    return "ok"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--a-base", required=True, help="model A: the clinician's reader and voice")
    ap.add_argument("--b-base", required=True, help="model B: the patient's voice")
    ap.add_argument("--hearing-base", help="the patient's hearing (default: model B's server)")
    ap.add_argument("--b-model", default=MODEL, help="model name model B's server answers to")
    ap.add_argument("--hearing-model", default=MODEL)
    ap.add_argument("--batch", choices=["smoke", "canary", "pilot"], default="smoke")
    ap.add_argument("--arms", default="twomodel_f,templates_mh,templates")
    ap.add_argument("--par", type=int, default=4)
    ap.add_argument("--a-label", default="not recorded")
    ap.add_argument("--b-label", default="not recorded")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    for base, name in {(a.a_base, MODEL), (a.b_base, a.b_model), (a.hearing_base or a.b_base, a.hearing_model)}:
        try:
            line, _ = _chat(base, "Reply with one word.", "Say ready.", 1, 16, model=name)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"completion gate: {base}: {e}; refusing to start")
        print(f"completion gate {base}: {line!r}")
    grid = canary_grid()[:3] if a.batch == "smoke" else canary_grid() if a.batch == "canary" else pilot_grid()
    arms = a.arms.split(",")
    jobs = [(t, d, arm) for t, d in grid for arm in arms]
    write_atomic(os.path.join(a.out, "venue.json"), {
        "started": dt.datetime.now(dt.timezone.utc).isoformat(), "batch": a.batch, "arms": arms,
        "a_base": a.a_base, "b_base": a.b_base, "hearing_base": a.hearing_base or a.b_base,
        "a_label": a.a_label, "b_label": a.b_label, "b_model": a.b_model, "hearing_model": a.hearing_model, "sessions": len(jobs),
        "hearing": "model hearing (model_hearing.py), uncalibrated", "fidelity_checks": "not run (Jev unpriced)"})
    counts = {}
    with ThreadPoolExecutor(a.par) as ex:
        for r in ex.map(lambda j: run_one(j[0], j[1], j[2], a, a.out), jobs):
            counts[r] = counts.get(r, 0) + 1
            print(counts, flush=True)
    print("done", counts)


if __name__ == "__main__":
    main()
