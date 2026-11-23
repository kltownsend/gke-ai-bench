#!/usr/bin/env python3
"""Run or resume one triage case as its own process, so a kill is a real process exit.

usage: run_case.py --case DIR --workspace DIR --record DIR --run-id ID --model replay:PATH|oracle
                   [--record-to PATH] [--resume] [--approve-as NAME]
Environment: LAB_KILL_AT=<point> ends the process at that point with exit code 137.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from controller import Controller  # noqa: E402
from effects import Record  # noqa: E402
import models as M  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--case", required=True)
ap.add_argument("--workspace", required=True)
ap.add_argument("--record", required=True)
ap.add_argument("--run-id", required=True)
ap.add_argument("--model", required=True)
ap.add_argument("--record-to")
ap.add_argument("--resume", action="store_true")
ap.add_argument("--approve-as")
ap.add_argument("--effects", choices=["naive", "reconcile"], default="naive")
ap.add_argument("--forge", action="store_true", help="effects go to the local Gitea forge")
a = ap.parse_args()

if a.model == "oracle":
    model = M.OracleStub(a.case)
elif a.model.startswith("replay:"):
    model = M.ReplayModel(a.model[len("replay:"):])
else:
    sys.exit(f"unknown model {a.model}")
if a.record_to:
    model = M.RecordingModel(model, a.record_to)
approver = (lambda proposal: a.approve_as) if a.approve_as else None
if a.forge:
    from forge_record import GiteaRecord
    rec = GiteaRecord(a.record)
else:
    rec = Record(a.record)
if a.resume:
    c = Controller.load(a.case, a.workspace, model, rec, a.run_id, approver=approver, effects_mode=a.effects)
    state = c.resume()
else:
    state = Controller(a.case, a.workspace, model, rec, approver=approver, run_id=a.run_id,
                       effects_mode=a.effects).run()
print(json.dumps({"stage": state["stage"], "attempt": state["attempt"]}))
