#!/usr/bin/env python3
"""Diagnose the manual-reference shell block (build review 6, finding 8): run one agy shell-command
call in the hardened Pod, then with each hardened restriction dropped one at a time, and record
whether the `fork/exec ... operation not permitted` error appears. The variant where agy's shell
works names the cause.

Source harness/lab_env.sh; kubectl pointed at the hardened cluster. usage: diag_shell.py [--out DIR]
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for d in ("controller", "agent"):
    sys.path.insert(0, os.path.join(TOOLS, d))
from pod_sandbox import PodSandbox  # noqa: E402
import models as M  # noqa: E402

out_dir = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else os.path.join(TOOLS, "runs", "diag")
os.makedirs(out_dir, exist_ok=True)
M.AntigravityModel.billing_guard()
# The lab-agent namespace enforces restricted Pod Security admission (hardened.yaml); the relaxed
# diagnostic variants would be rejected by the API server. The cluster is ephemeral and torn down
# after, so relax the label for the diagnosis only.
subprocess.run(["kubectl", "label", "--overwrite", "namespace", "lab-agent",
                "pod-security.kubernetes.io/enforce=privileged"], check=True)
PROMPT = ("Run exactly this shell command and report its output verbatim: echo LAB_SHELL_OK. "
          "Do not edit any file.")
VARIANTS = [("full_hardened", ()), ("minus_seccomp", ("seccomp",)), ("minus_nnp", ("nnp",)),
            ("minus_rorootfs", ("rorootfs",))]

results = []
for name, drop in VARIANTS:
    ws = tempfile.mkdtemp(prefix="diag-")
    with open(os.path.join(ws, "README.md"), "w") as fh:
        fh.write("diag\n")
    subprocess.run(["chmod", "-R", "a+rwX", ws], check=True)
    try:
        with PodSandbox("hardened", drop=drop) as sb:
            model = M.AntigravityModel(sandbox=sb)
            model._agy(PROMPT, ws, name)
        call = model.calls[0] if model.calls else {"response_tail": "", "exit": None}
    except Exception as e:  # noqa: BLE001 - a rejected Pod is a result too
        call = {"response_tail": f"[harness error: {type(e).__name__}: {e}]", "exit": None}
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    tail = call.get("response_tail") or ""
    forkexec = "operation not permitted" in tail or "fork/exec" in tail
    shell_ok = "LAB_SHELL_OK" in tail
    results.append({"variant": name, "drop": list(drop), "shell_ok": shell_ok,
                    "fork_exec_error": forkexec, "exit": call.get("exit"), "tail": tail[-400:]})
    print(f"{name:16} shell_ok={shell_ok} fork_exec_error={forkexec}")

with open(os.path.join(out_dir, "diag_shell.json"), "w") as fh:
    json.dump(results, fh, indent=1, default=str)
cause = next((v["variant"] for v in results[1:] if v["shell_ok"] and not v["fork_exec_error"]), None)
print("\nfirst variant where agy's shell works:", cause or "none (not isolated)")
