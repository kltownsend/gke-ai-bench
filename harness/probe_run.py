#!/usr/bin/env python3
"""Run the scripted probes (probes/api_scope.py, probes/escape_suite.py) inside the agent's
workload on one venue, the same way the agent runs there, and save one JSON per probe.

The probes travel in the workspace (a scratch directory holding only them), so on every venue they
run where agy would: the local sandbox's container, or a fresh Pod on a GKE profile. No model steers
them. The API scope probe's positive control makes one minimal model call (8 output tokens).

Source harness/lab_env.sh. Local needs Docker; GKE needs kubectl pointed at the cluster.

usage: probe_run.py --venue local|hardened|default|vulnerable [--out DIR] [--listener URL] [--label NAME]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "agent"))
# An interpreter with google-auth, for the token minter (GOOGLE_AUTH_PY in lab.env).
GOOGLE_AUTH_PY = shutil.which(os.path.expanduser(os.environ.get("GOOGLE_AUTH_PY") or "python3")) or sys.executable
PROJECT = os.environ.get("LAB_PROJECT") or sys.exit("LAB_PROJECT is not set: source harness/lab_env.sh")


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


venue = arg("--venue")
out_dir = arg("--out", os.path.join(TOOLS, "runs", f"probes-{time.strftime('%Y%m%d')}"))
label = arg("--label", venue)
listener = arg("--listener")
run_token = f"lab-run-{uuid.uuid4().hex[:12]}"
os.makedirs(out_dir, exist_ok=True)
ws = tempfile.mkdtemp(prefix="lab-probes-")
for f in ("api_scope.py", "escape_suite.py"):
    shutil.copy(os.path.join(TOOLS, "probes", f), ws)
subprocess.run(["chmod", "-R", "a+rwX", ws], check=True)
suite = ["python3", "escape_suite.py", "--run-token", run_token] + (["--listener", listener] if listener else [])
jobs = {"api_scope": ["python3", "api_scope.py", PROJECT], "escape_suite": suite}


def save(name, p, extra):
    try:
        body = json.loads(p.stdout)
    except ValueError:
        body = {"unparsed": p.stdout[-2000:]}
    rec = {"venue": venue, "label": label, "run_token": run_token, "exit": p.returncode,
           "stderr_tail": (p.stderr or "")[-500:], **extra, "result": body}
    path = os.path.join(out_dir, f"{name}-{label}.json")
    with open(path, "w") as fh:
        json.dump(rec, fh, indent=1, default=str)
    print(path, "exit", p.returncode)


if venue == "local":
    from sandbox import Sandbox
    tokens = tempfile.mkdtemp(prefix="agy-tokens-")
    minter = subprocess.Popen([GOOGLE_AUTH_PY, os.path.join(TOOLS, "agent", "mint_token.py"), tokens, "--loop"],
                              stdout=subprocess.PIPE, text=True)
    if "minted_for" not in minter.stdout.readline():
        minter.kill()
        sys.exit("token mint failed")
    try:
        with Sandbox(tokens) as sb:
            for name, argv in jobs.items():
                save(name, sb.run(ws, argv, timeout=180), {})
            egress = sb.sidecar_log()
        with open(os.path.join(out_dir, f"sidecar-{label}.json"), "w") as fh:
            json.dump(egress, fh, indent=1)
    finally:
        minter.kill()
        shutil.rmtree(tokens, ignore_errors=True)
else:
    from sandbox_venue import make_venue
    ctx = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True).stdout.strip()
    # The neighbor's Service address, read by the harness, so the suite can try it by IP as well as
    # by name. Recorded with the endpoints, which show whether the neighbor was serving.
    nip = subprocess.run(["kubectl", "-n", "lab-neighbor", "get", "svc", "neighbor", "-o", "jsonpath={.spec.clusterIP}"],
                         capture_output=True, text=True).stdout.strip()
    eps = subprocess.run(["kubectl", "-n", "lab-neighbor", "get", "endpoints", "neighbor", "-o",
                          "jsonpath={.subsets[*].addresses[*].ip}"], capture_output=True, text=True).stdout.strip()
    if nip:
        jobs["escape_suite"] += ["--neighbor-ip", nip]
    kip = subprocess.run(["kubectl", "-n", "default", "get", "svc", "kubernetes", "-o", "jsonpath={.spec.clusterIP}"],
                         capture_output=True, text=True).stdout.strip()
    if kip:
        jobs["escape_suite"] += ["--k8s-ip", kip]
    with make_venue(venue) as sb:
        for name, argv in jobs.items():
            save(name, sb.run(ws, argv, timeout=180), {"kube_context": ctx, "neighbor_ip": nip,
                                                       "neighbor_endpoints": eps})
    print(sb.events)
shutil.rmtree(ws, ignore_errors=True)
