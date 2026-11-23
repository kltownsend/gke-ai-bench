#!/usr/bin/env python3
"""Mint a short-lived access token for the lab service account, for the sidecar to serve.

Runs on the host, never in a container. It reads the impersonated lab credential in the lab's
own gcloud config directory (harness/lab_env.sh: `gcloud auth application-default login
--impersonate-service-account="$LAB_AGENT"`), whose source is the operator's own user
credential, and writes only the resulting access token (about an hour) to OUT_DIR/token.json,
mode 600. The refresh token never leaves the host; the sandbox only ever sees access tokens for
the agent's service account (LAB_AGENT). Give that account one grant, roles/aiplatform.user on
the lab project, and nothing in other projects or at the organization; that role allows more
Vertex AI operations than the one generate call the lab makes.

Needs google-auth; run with an interpreter that has it (GOOGLE_AUTH_PY in lab.env).

usage: mint_token.py OUT_DIR [--loop]    (--loop refreshes every 40 minutes until killed)
"""
import json
import os
import sys
import time

import google.auth
import google.auth.transport.requests as tr

LAB_AGENT = os.environ.get("LAB_AGENT") or sys.exit("LAB_AGENT is not set: source harness/lab_env.sh")


def mint(out_dir):
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    who = getattr(creds, "service_account_email", None) or getattr(creds, "_target_principal", None)
    if who != LAB_AGENT:
        sys.exit(f"refusing: credentials act as {who!r}, not {LAB_AGENT}. Source harness/lab_env.sh.")
    creds.refresh(tr.Request())
    tok = {"access_token": creds.token, "expiry": creds.expiry.timestamp() if creds.expiry.tzinfo
           else creds.expiry.replace(tzinfo=__import__("datetime").timezone.utc).timestamp(),
           "email": LAB_AGENT}
    tmp = os.path.join(out_dir, "token.json.tmp")
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(tok, fh)
    os.replace(tmp, os.path.join(out_dir, "token.json"))
    return tok["expiry"]


if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)
    while True:
        exp = mint(out)
        print(json.dumps({"minted_for": LAB_AGENT, "expires_in_s": int(exp - time.time())}), flush=True)
        if "--loop" not in sys.argv:
            break
        time.sleep(40 * 60)
