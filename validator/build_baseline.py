#!/usr/bin/env python3
"""Build a case's sealed baseline: the Clinic at a commit, no history.

The baseline is the agent's starting workspace and what the validator compares a workspace
against: the Clinic at a commit, no history, sanitized (sanitize.py) so nothing names a cause.
A build fails if the leak scan finds anything.

usage: build_baseline.py COMMIT NAME [--overlay COMMIT:PATH ...]   (writes ../baselines/NAME)

--overlay takes one file from another commit (T06's reference fix is 3f7f0d0^ with only
3f7f0d0's scorer.py). A commit older than the freeze manifest gets an empty one, which is true:
nothing was frozen then.
"""

import json
import os
import shutil


def strip_bytecode(root):
    for d, dirs, files in os.walk(root):
        for x in [x for x in dirs if x == "__pycache__"]:
            shutil.rmtree(os.path.join(d, x))
        dirs[:] = [x for x in dirs if x != "__pycache__"]
import subprocess
import sys

REPO = os.path.expanduser(os.environ.get("CLINICBENCH_DIR", "~/lab-src/clinicbench"))   # the clinicbench checkout
TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import re  # noqa: E402

args = sys.argv[1:]
overlays = []
while "--overlay" in args:
    i = args.index("--overlay")
    overlays.append(args[i + 1])
    del args[i:i + 2]
if len(args) != 2 or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", args[1]):
    # 2026-11-20: an empty NAME (a zsh loop that didn't word-split) made dst baselines/ itself,
    # and the rmtree below wiped every baseline. A name is one plain path segment, nothing else.
    sys.exit("usage: build_baseline.py COMMIT NAME  (NAME: lowercase letters, digits, - or _)")
commit, name = args
dst = os.path.join(TOOLS, "baselines", name)
assert os.path.dirname(dst) == os.path.join(TOOLS, "baselines"), dst
if os.path.exists(dst):
    shutil.rmtree(dst)
os.makedirs(dst)
archive = subprocess.run(["git", "-C", REPO, "archive", commit], capture_output=True, check=True).stdout
subprocess.run(["tar", "-x", "-C", dst], input=archive, check=True)
for ov in overlays:
    oc, path = ov.split(":", 1)
    if os.path.isabs(path) or ".." in path.split("/"):
        sys.exit(f"bad overlay path {path!r}")
    body = subprocess.run(["git", "-C", REPO, "show", f"{oc}:{path}"], capture_output=True, check=True).stdout
    with open(os.path.join(dst, path), "wb") as fh:
        fh.write(body)
manifest = os.path.join(dst, "docs", "phase3-freeze.json")
if not os.path.exists(manifest):
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
    with open(manifest, "w") as fh:
        json.dump({"frozen_at": None, "files": {}}, fh, indent=1)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sanitize  # noqa: E402
strip_bytecode(dst)
log = sanitize.sanitize(dst, sanitize.case_terms(os.path.join(TOOLS, "cases")))
print(json.dumps({"baseline": dst, "removed": len(log["removed"]), "stripped": log["stripped"],
                  "refrozen": log["refrozen"], "leaks": log["leaks"]}))
if log["leaks"]:
    sys.exit("leak scan failed: the workspace still names a cause")
