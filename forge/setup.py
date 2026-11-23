#!/usr/bin/env python3
"""Set up the local Gitea forge for the lab: an admin user, an API token, and one private
repository with a base branch per case baseline.

Credentials are throwaway test values for this local forge, written to forge/.forge.json
(git-ignored), never printed. The forge listens on 127.0.0.1 only.

usage: setup.py [--url http://127.0.0.1:3300] [--container lab-gitea]
"""
import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
CONF = os.path.join(HERE, ".forge.json")
REPO = "clinicbench-lab"

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:3300")
ap.add_argument("--container", default="lab-gitea")
a = ap.parse_args()


def api(method, path, body=None, auth=None):
    req = urllib.request.Request(a.url + "/api/v1" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(auth or {})})
    with urllib.request.urlopen(req) as r:
        txt = r.read()
        return json.loads(txt) if txt else {}


user, pw = "lab", secrets.token_urlsafe(18)
subprocess.run(["docker", "exec", "-u", "git", a.container, "gitea", "admin", "user", "create", "--admin",
                "--username", user, "--password", pw, "--email", "lab@lab.invalid",
                "--must-change-password=false"], check=True, capture_output=True)
basic = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
tok = api("POST", f"/users/{user}/tokens", {"name": f"lab-{secrets.token_hex(3)}", "scopes": ["all"]}, basic)["sha1"]
auth = {"Authorization": f"token {tok}"}
api("POST", "/user/repos", {"name": REPO, "private": True, "default_branch": "main", "auto_init": False}, auth)
for label in ("finding", "notification-log"):
    api("POST", f"/repos/{user}/{REPO}/labels", {"name": label, "color": "#888888"}, auth)

# One base branch per baseline, pushed with git over HTTP using the token.
remote = a.url.replace("://", f"://{user}:{tok}@") + f"/{user}/{REPO}.git"
bases = sorted(d for d in os.listdir(os.path.join(TOOLS, "baselines"))
               if os.path.isdir(os.path.join(TOOLS, "baselines", d)))
for b in bases:
    w = tempfile.mkdtemp(prefix="forge-")
    shutil.copytree(os.path.join(TOOLS, "baselines", b), os.path.join(w, "t"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    t = os.path.join(w, "t")
    git = lambda *x: subprocess.run(["git", "-C", t, *x], check=True, capture_output=True)  # noqa: E731
    git("init", "-q", "-b", f"base-{b}")
    git("-c", "user.name=lab", "-c", "user.email=lab@lab.invalid", "add", "-A")
    git("-c", "user.name=lab", "-c", "user.email=lab@lab.invalid", "commit", "-q", "-m", f"baseline {b}")
    git("push", "-q", remote, f"base-{b}")
    shutil.rmtree(w)

with open(CONF, "w") as fh:
    json.dump({"url": a.url, "user": user, "token": tok, "repo": REPO, "bases": bases}, fh)
os.chmod(CONF, 0o600)
print(json.dumps({"repo": f"{user}/{REPO}", "bases": bases}))
