#!/usr/bin/env python3
"""Keep the lab-agent access token in the Substrate cluster's Secret, for the egress gateway.

Substrate can't mint a Google token for an actor ("Substrate can't yet trade an actor JWT for
cloud credentials", issue #1661), so the security guide's path is a stored secret the gateway
injects. This is the controller's half of that: mint a short-lived access token for the agent's
service account (LAB_AGENT) on the workstation, by impersonation from the operator's own gcloud
credentials (the same impersonated credential as agent/mint_token.py; the refresh token never
leaves the workstation), and write it to Secret lab-creds/lab-agent-token, key `token`,
refreshed every 40 minutes so it never expires mid-call. The token goes to kubectl on stdin, never in argv. The actor never sees
it; only the bundled credential provider reads it, for the lab-agent atespace only
(infra/substrate/namespace-policy.yaml).

Run with an interpreter that has google-auth (GOOGLE_AUTH_PY), after sourcing
harness/lab_env.sh, with kubectl pointed at the Substrate cluster.

usage: token_secret.py [--once]
"""
import json
import os
import subprocess
import sys
import time

import google.auth
import google.auth.transport.requests as tr

LAB_AGENT = os.environ.get("LAB_AGENT") or sys.exit("LAB_AGENT is not set: source harness/lab_env.sh")
# Substrate rung: lab-creds. Executor rung: ax-system, labeled ax.io/credential=true, the namespace
# and label AX's install grants the provider (LAB_TOKEN_NAMESPACE=ax-system LAB_TOKEN_AX_LABEL=1).
NAMESPACE = os.environ.get("LAB_TOKEN_NAMESPACE", "lab-creds")
NAME, KEY = "lab-agent-token", "token"
AX_LABEL = os.environ.get("LAB_TOKEN_AX_LABEL") == "1"
EVERY_S = 40 * 60


def mint() -> tuple[str, float]:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    who = getattr(creds, "service_account_email", None) or getattr(creds, "_target_principal", None)
    if who != LAB_AGENT:
        sys.exit(f"refusing: credentials act as {who!r}, not {LAB_AGENT}. Source harness/lab_env.sh.")
    creds.refresh(tr.Request())
    exp = creds.expiry.timestamp() if creds.expiry.tzinfo else creds.expiry.replace(
        tzinfo=__import__("datetime").timezone.utc).timestamp()
    return creds.token, exp


def secret(token: str) -> dict:
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": NAME, "namespace": NAMESPACE, "labels": {"app": "lab-token-refresher",
                                                                     **({"ax.io/credential": "true"} if AX_LABEL else {})}},
            "stringData": {KEY: token}}


def write(token: str):
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(secret(token)).encode(),
                   capture_output=True, check=True, timeout=60)


def main():
    while True:
        token, exp = mint()
        write(token)
        print(json.dumps({"event": "token written", "secret": f"{NAMESPACE}/{NAME}",
                          "expires_in_s": round(exp - time.time())}), flush=True)
        if "--once" in sys.argv:
            return
        time.sleep(EVERY_S)


if __name__ == "__main__":
    main()
