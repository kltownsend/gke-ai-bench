#!/usr/bin/env python3
"""One real Antigravity run through the arm F controller, the agent in a GKE Pod (agent/pod_sandbox.py).

The GKE counterpart of harness/canary_agy.py, and the spec's first stop-or-go canary: does agy run
one case unattended in a Pod, and does the validator score it. The controller, the judge and the
forge stay on the workstation. No token minter and no sidecar: credentials and egress are the cluster's
(Workload Identity and a NetworkPolicy on hardened, the node's service account and nothing on
default and vulnerable). The billing guard still runs on the host before every call; inside the
Pod the call bills the lab project through GOOGLE_CLOUD_QUOTA_PROJECT.

Source harness/lab_env.sh; set LAB_AGENT_IMAGE (pinned by digest) and point kubectl at the
cluster (infra/cluster.sh setup). Run with LAB_PYTHON.

With `--pause-at-approval SECONDS` (Substrate only): when the controller stops at
`waiting_for_approval`, the investigation is paused the way the ladder's Substrate row describes
("an investigation paused while it waits for a person, resumed with its workspace intact"): the
actor is suspended to storage, held for SECONDS, resumed, and checked (harness/resume_probe.py's
checks: a nonce written into memory before the suspend, the child's pipe state, the counter, the
/work and HOME manifests). Then the effects depend on the restored actor (round 9, finding 2): the
controller pulls the workspace back from the resumed actor, requires it to match the approved
workspace file for file and the memory nonce to have survived, and only then records the approval
and resumes from its saved proposal to the effects, on the workspace the actor returned. If either
check fails the effects are refused and the run records why. The model isn't called again.

usage: canary_gke.py CASE --profile hardened|default|vulnerable|substrate [--budget N] [--out DIR]
                     [--label NAME] [--pause-at-approval SECONDS]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "controller"))
sys.path.insert(0, os.path.join(TOOLS, "agent"))
sys.path.insert(0, os.path.join(TOOLS, "harness"))
from controller import Controller  # noqa: E402
from effects import Record  # noqa: E402
from sandbox_venue import make_venue  # noqa: E402
import models as M  # noqa: E402


def content_map(root: str) -> dict:
    """Relative path -> sha256 of every regular file, for comparing workspaces by content."""
    import hashlib
    out = {}
    for d, _, files in os.walk(root):
        for n in files:
            p = os.path.join(d, n)
            if os.path.isfile(p) and not os.path.islink(p):
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


case = sys.argv[1]
profile = arg("--profile")
budget = int(arg("--budget", 2))
label = arg("--label", f"gke-{profile}")
M.AntigravityModel.billing_guard()
case_dir = os.path.join(TOOLS, "cases", case)
with open(os.path.join(case_dir, "case.json")) as fh:
    base = json.load(fh)["baseline"]
d = tempfile.mkdtemp(prefix="agy-gke-")
out_dir = arg("--out", d)
ws = os.path.join(d, "clinicbench")
shutil.copytree(os.path.join(TOOLS, "baselines", base), ws)
ctx = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True).stdout.strip()
pause_s = arg("--pause-at-approval")
if pause_s is not None and profile != "substrate":
    sys.exit("--pause-at-approval needs the substrate profile (an actor that can be suspended)")
pause = None
with make_venue(profile) as sb:
    model = M.AntigravityModel(sandbox=sb)
    t0 = time.time()
    ctl = Controller(case_dir, ws, model, Record(os.path.join(d, "record")), budget=budget)
    state = ctl.run()
    if pause_s is not None and state["stage"] == "waiting_for_approval":
        from resume_probe import CHECKS, checkpoint, verify     # the probe's own checks
        from pod_sandbox import unpack_replace
        approved = content_map(ws)
        pre = checkpoint(sb, 1)
        sus = sb.suspend()
        time.sleep(float(pause_s))
        res = sb.resume()
        checks = verify(sb, pre, 1)
        returned = tempfile.mkdtemp(prefix="agy-gke-returned-")
        unpack_replace(sb.http("GET", "/workspace"), returned)
        got = content_map(returned)
        got.pop("cycle-1.txt", None)                      # the probe's own marker file
        # Every approved file must come back identical; files only the actor has (caches the
        # controller cleared on the workstation) are recorded, not failed. The controller's own contract
        # log is written on the workstation after the agent's call, so it was never the actor's to return
        # (the first GKE run refused its effects on exactly that file).
        import controller as CTL
        owned = {CTL.V.CONTRACT_LOG}
        differs = sorted(p for p in approved if p not in owned and got.get(p) != approved[p])
        checks["workspace_matches_approved"] = not differs
        pause = {"held_s": float(pause_s), "suspend_sync_s": sus, "resume_to_healthy_s": res, **checks,
                 "controller_owned": sorted(owned),
                 "differs": differs[:20], "actor_only_files": sorted(set(got) - set(approved))[:20]}
        if checks["memory_mark"] and checks["workspace_matches_approved"]:
            for rel in approved:                          # the effects use what the actor returned
                if rel not in owned:
                    shutil.copy2(os.path.join(returned, rel), os.path.join(ws, rel))
            ctl.approver = lambda proposal: "operator (canary, after the pause)"
            state = ctl.resume()
            pause["effects"] = "ran on the restored actor's workspace"
        else:
            pause["effects"] = "refused: the restored actor didn't return the approved state"
            pause["failed_checks"] = [k for k in (*CHECKS, "workspace_matches_approved") if not checks.get(k)]
    wall = round(time.time() - t0)
tot = {k: sum((c["usage"] or {}).get(k, 0) for c in model.calls)
       for k in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens")}
result = {"case": case, "venue": f"gke {profile}", "kube_context": ctx, "image": sb.image, "label": label,
          "final_stage": state["stage"], "attempts": state["attempt"], "wall_seconds": wall,
          "calls": model.calls, "totals": tot, "pods": sb.events, "pause_at_approval": pause,
          "outcomes": [e.get("outcome") for e in state["events"] if e["event"] == "result" and e["stage"] == "validate"],
          "hypotheses": [e.get("hypothesis") for e in state["events"] if e["stage"] == "hypothesis" and e["event"] == "result"],
          "workspace": ws}
os.makedirs(os.path.join(out_dir, "records"), exist_ok=True)
with open(os.path.join(out_dir, f"canary-{case}-{label}.json"), "w") as fh:
    json.dump(result, fh, indent=1, default=str)
for f in os.listdir(os.path.join(d, "record", "runs")):
    shutil.copy(os.path.join(d, "record", "runs", f), os.path.join(out_dir, "records", f"{case}-{label}.json"))
print(json.dumps(result, indent=1, default=str))
