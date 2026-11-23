#!/usr/bin/env python3
"""Effort canary: one real Antigravity run through the arm F controller, inside the agent sandbox.

agy runs in agent/sandbox.py's container: only a sanitized baseline copy mounted, egress to Google
APIs only, short-lived lab-agent tokens minted on the host and refreshed every 40 minutes. Billing
is pay-as-you-go in the lab project, checked by the billing guard before every call. This run
measures tokens and effort and records which hosts agy contacts; it isn't scored. No approver: a
frozen-file fix stops at pending, which is fine.

Source harness/lab_env.sh first. Run with an interpreter that can import the controller
(LAB_PYTHON); token minting uses GOOGLE_AUTH_PY, an interpreter with google-auth.

usage: canary_agy.py CASE [--budget N] [--out DIR] [--allow HOST,HOST] [--label NAME]

--allow overrides the sidecar's egress allowlist (default aiplatform.googleapis.com alone); used
for the paired runs on whether letting agy reach play.googleapis.com changes its timing.
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
from controller import Controller  # noqa: E402
from effects import Record  # noqa: E402
from sandbox import Sandbox  # noqa: E402
import models as M  # noqa: E402

# An interpreter with google-auth, for the token minter (GOOGLE_AUTH_PY in lab.env).
GOOGLE_AUTH_PY = shutil.which(os.path.expanduser(os.environ.get("GOOGLE_AUTH_PY") or "python3")) or sys.executable

case = sys.argv[1]
budget = int(sys.argv[sys.argv.index("--budget") + 1]) if "--budget" in sys.argv else 2
allow = sys.argv[sys.argv.index("--allow") + 1] if "--allow" in sys.argv else None
label = sys.argv[sys.argv.index("--label") + 1] if "--label" in sys.argv else "sandbox"
M.AntigravityModel.billing_guard()                       # before minting anything
case_dir = os.path.join(TOOLS, "cases", case)
with open(os.path.join(case_dir, "case.json")) as fh:
    base = json.load(fh)["baseline"]
d = tempfile.mkdtemp(prefix="agy-canary-")
out_dir = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else d
ws = os.path.join(d, "clinicbench")
shutil.copytree(os.path.join(TOOLS, "baselines", base), ws)
subprocess.run(["chmod", "-R", "a+rwX", ws], check=True)  # the agent runs as its own uid (sandbox.AGENT_UID)
tokens = tempfile.mkdtemp(prefix="agy-tokens-")   # 0700; mint_token writes token.json 0600
minter = subprocess.Popen([GOOGLE_AUTH_PY, os.path.join(TOOLS, "agent", "mint_token.py"), tokens, "--loop"],
                          stdout=subprocess.PIPE, text=True)
first = minter.stdout.readline()
if "minted_for" not in first:
    minter.kill()
    sys.exit(f"token mint failed: {first!r}")
try:
    with Sandbox(tokens, egress_allow=allow) as sb:
        model = M.AntigravityModel(sandbox=sb)
        t0 = time.time()
        state = Controller(case_dir, ws, model, Record(os.path.join(d, "record")), budget=budget).run()
        wall = round(time.time() - t0)
        egress = sb.sidecar_log()
finally:
    minter.kill()
    shutil.rmtree(tokens, ignore_errors=True)
tot = {k: sum((c["usage"] or {}).get(k, 0) for c in model.calls)
       for k in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens")}
hosts = {}
for e in egress:
    if e.get("event") == "egress":
        key = f'{e.get("decision")}:{e.get("host")}'
        hosts[key] = hosts.get(key, 0) + 1
result = {"case": case, "venue": "local sandbox", "label": label, "egress_allow": allow or "default", "final_stage": state["stage"], "attempts": state["attempt"],
          "wall_seconds": wall, "calls": model.calls, "totals": tot, "egress": hosts,
          "tokens_served": sum(1 for e in egress if e.get("decision") == "token served"),
          "outcomes": [e.get("outcome") for e in state["events"] if e["event"] == "result" and e["stage"] == "validate"],
          "hypotheses": [e.get("hypothesis") for e in state["events"] if e["stage"] == "hypothesis" and e["event"] == "result"],
          "workspace": ws}
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(out_dir, f"canary-{case}-{label}.json"), "w") as fh:
    json.dump({**result, "egress_log": egress}, fh, indent=1, default=str)
print(json.dumps(result, indent=1, default=str))
