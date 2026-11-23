#!/usr/bin/env python3
"""One injection-steered reach run (probes/injection.py): a real triage of one case under arm F in
a GKE Pod, with a recorded session carrying the injected instruction, a decoy credential in the
workspace, and the canary listener in the cluster's VPC. Scores each canary as read and sent.

Every Google token is redacted before anything is saved, with the listener's own redactor, and the
workspace is deleted after scoring, so no live credential outlives the run on the workstation.

Source harness/lab_env.sh; the cluster needs setup and the listener (infra/cluster.sh). Run with
LAB_PYTHON.

--direct gives the steps to agy as its own task (the control for a compromised agent) instead of
planting them in a session record; without it, the injection run.

usage: reach_run.py --profile hardened|default|vulnerable [--case S01] [--direct] [--out DIR] [--label NAME]
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("controller", "agent", "probes"):
    sys.path.insert(0, os.path.join(TOOLS, d))
from controller import Controller  # noqa: E402
from effects import Record  # noqa: E402
from sandbox_venue import make_venue  # noqa: E402
import injection as INJ  # noqa: E402
import listener as L  # noqa: E402
import models as M  # noqa: E402

PROJECT = os.environ.get("LAB_PROJECT") or sys.exit("LAB_PROJECT is not set: source harness/lab_env.sh")
ZONE = os.environ.get("LAB_ZONE") or sys.exit("LAB_ZONE is not set: source harness/lab_env.sh")


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def listener_hits():
    out = subprocess.run(["gcloud", "compute", "instances", "get-serial-port-output", "lab-listener",
                          "--project", PROJECT, "--zone", ZONE, "--port", "1"],
                         capture_output=True, text=True).stdout
    hits = []
    for line in out.splitlines():
        if "LABHIT " in line:
            try:
                hits.append(json.loads(line.split("LABHIT ", 1)[1]))
            except ValueError:
                pass
    return hits


def scrub(obj):
    """Redact Google tokens everywhere in a JSON-able object."""
    return json.loads(L.redact(json.dumps(obj, default=str))[0])


profile, case = arg("--profile"), arg("--case", "S01")
label = arg("--label", f"reach-{profile}")
out_dir = arg("--out", os.path.join(TOOLS, "runs", f"reach-{time.strftime('%Y%m%d')}"))
os.makedirs(out_dir, exist_ok=True)
M.AntigravityModel.billing_guard()

ip_int = sh("gcloud", "compute", "instances", "describe", "lab-listener", "--project", PROJECT, "--zone", ZONE,
            "--format", "value(networkInterfaces[0].networkIP)")
ip_ext = sh("gcloud", "compute", "instances", "describe", "lab-listener", "--project", PROJECT, "--zone", ZONE,
            "--format", "value(networkInterfaces[0].accessConfigs[0].natIP)")
internal_url, external_url = f"http://{ip_int}:8080", f"http://{ip_ext}:8080"
neighbor = base64.b64decode(sh("kubectl", "-n", "lab-neighbor", "get", "secret", "neighbor-canary",
                               "-o", "jsonpath={.data.value}")).decode()
tk = INJ.tokens()

d = tempfile.mkdtemp(prefix="lab-reach-")
case_dir = os.path.join(d, "case")
shutil.copytree(os.path.join(TOOLS, "cases", case), case_dir)
with open(os.path.join(case_dir, "case.json")) as fh:
    c = json.load(fh)
ws = os.path.join(d, "clinicbench")
shutil.copytree(os.path.join(TOOLS, "baselines", c["baseline"]), ws)
direct = "--direct" in sys.argv
c["ticket"] = INJ.plant(ws, c["ticket"], tk, internal_url, external_url)   # writes the decoy and the note
with open(os.path.join(case_dir, "case.json"), "w") as fh:
    json.dump(c, fh, indent=1)
before = len(listener_hits())

with make_venue(profile) as sb:
    model = M.AntigravityModel(sandbox=sb)
    t0 = time.time()
    if direct:
        # The control: agy is told to do the steps, so the result is what the rung stops, not what
        # the model chooses. One call in a Pod; no triage.
        model._agy(INJ.direct_prompt(tk, internal_url, external_url), ws, "direct")
        state = {"stage": "direct"}
    else:
        state = Controller(case_dir, ws, model, Record(os.path.join(d, "record")), budget=1).run()
    wall = round(time.time() - t0)
time.sleep(10)                                   # let the serial console catch up
hits = listener_hits()[before:]
result = {"case": case, "venue": f"gke {profile}", "label": label, "mode": "direct" if direct else "injection",
          "tokens": tk, "neighbor_canary": neighbor,
          "listener": {"internal": internal_url, "external": external_url}, "ticket": c["ticket"],
          "score": INJ.score(tk, neighbor, model.calls, ws, hits),
          "final_stage": state["stage"], "wall_seconds": wall, "pods": sb.events,
          "calls": model.calls, "listener_hits": hits}
path = os.path.join(out_dir, f"{label}.json")
with open(path, "w") as fh:
    json.dump(scrub(result), fh, indent=1)
shutil.rmtree(d, ignore_errors=True)             # the workspace may hold whatever the agent fetched
print(path)
print(json.dumps(scrub(result["score"]), indent=1))
