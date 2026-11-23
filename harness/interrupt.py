#!/usr/bin/env python3
"""The interruption harness, local edition.

For a case: record one uninterrupted run (the reference), then for each kill point replay the
same model outputs in a fresh workspace, kill the controller at that point, recover with
`resume()` from its saved state, and compare the effects record with the reference. Replay makes
the model identical across runs, so any difference is the interruption's (locally this stands in
for the spec's forked snapshots; on GKE the runtime does the snapshotting and the killing).

Scored per point:
- each effect kind: count against the reference (duplicated, missing)
- effects without the approval their proposal needed
- final controller stage
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "controller"))
from effects import Record  # noqa: E402
from hooks import POINTS  # noqa: E402

PY = sys.executable
RUN = os.path.join(TOOLS, "controller", "run_case.py")
KINDS = ("pull_requests", "findings", "notifications")


def baseline_for(case_dir):
    with open(os.path.join(case_dir, "case.json")) as fh:
        return os.path.join(TOOLS, "baselines", json.load(fh)["baseline"])


def fresh(case_dir):
    d = tempfile.mkdtemp(prefix="lab-int-")
    ws = os.path.join(d, "ws")
    shutil.copytree(baseline_for(case_dir), ws)
    return ws, os.path.join(d, "record"), d


FORGE = os.environ.get("LAB_FORGE") == "1"   # effects to the local Gitea forge


def child(case_dir, ws, rec, run_id, model, kill_at=None, resume=False, record_to=None, mode="naive"):
    env = dict(os.environ)
    env.pop("LAB_KILL_AT", None)
    if kill_at:
        env["LAB_KILL_AT"] = kill_at
    cmd = [PY, RUN, "--case", case_dir, "--workspace", ws, "--record", rec, "--run-id", run_id,
           "--model", model, "--approve-as", "operator", "--effects", mode]
    if resume:
        cmd.append("--resume")
    if FORGE:
        cmd.append("--forge")
    if record_to:
        cmd += ["--record-to", record_to]
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=1800)
    out = (p.stdout.strip().splitlines() or ["{}"])[-1]
    try:
        out = json.loads(out)
    except ValueError:
        out = {"stdout": p.stdout[-300:]}
    return p.returncode, out, p.stderr[-500:]


def effects(rec_dir):
    if FORGE:
        from forge_record import GiteaRecord
        r = GiteaRecord(rec_dir)
    else:
        r = Record(rec_dir)
    approvals = {a["proposal"] for a in r.read("approvals")}
    counts = {k: len(r.read(k)) for k in KINDS}
    unapproved = [e for k in KINDS for e in r.read(k) if e["proposal"] not in approvals]
    return {"counts": counts, "approvals": len(approvals), "unapproved": len(unapproved)}


def run_case(case_dir, points=POINTS, mode="naive"):
    # 1. the reference: one uninterrupted run, recorded
    ws, rec, d = fresh(case_dir)
    recording = os.path.join(d, "recording.json")
    code, out, err = child(case_dir, ws, rec, f"ref-{uuid.uuid4().hex[:6]}", "oracle", record_to=recording, mode=mode)
    ref = {"exit": code, "final": out, "effects": effects(rec)}
    results = {"case": os.path.basename(case_dir), "mode": mode, "forge": FORGE, "reference": ref, "points": {}}
    # 2. each kill point: replay, kill, recover, compare
    for pt in points:
        ws2, rec2, _ = fresh(case_dir)
        run_id = f"int-{pt}-{uuid.uuid4().hex[:6]}"
        k_code, k_out, k_err = child(case_dir, ws2, rec2, run_id, f"replay:{recording}", kill_at=pt, mode=mode)
        if k_code != 137:                        # the point never fired: nothing to recover
            results["points"][pt] = {"killed": False, "not_applicable": True, "kill_exit": k_code,
                                     "stderr": k_err if k_code else ""}
            continue
        r_code, r_out, r_err = child(case_dir, ws2, rec2, run_id, f"replay:{recording}", resume=True, mode=mode)
        got = effects(rec2)
        diff = {k: got["counts"][k] - ref["effects"]["counts"][k] for k in KINDS}
        results["points"][pt] = {
            "killed": k_code == 137, "kill_exit": k_code,
            "recovered_stage": r_out.get("stage"), "recover_exit": r_code,
            "effects": got, "vs_reference": diff,
            "duplicated": {k: v for k, v in diff.items() if v > 0},
            "missing": {k: -v for k, v in diff.items() if v < 0},
            "stderr": (k_err if k_code not in (0, 137) else "") + (r_err if r_code else ""),
        }
    return results


if __name__ == "__main__":
    args = sys.argv[1:]
    modes = ["naive", "reconcile"]
    if args and args[0] in modes:
        modes, args = [args[0]], args[1:]
    case_dirs = [os.path.join(TOOLS, "cases", c) for c in (args or ["S01", "T17"])]
    print(json.dumps([run_case(c, mode=m) for m in modes for c in case_dirs], indent=1))
