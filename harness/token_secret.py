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
harness/lab_env.sh, with kubectl pointed at the Substrate cluster. It refuses to write unless kubectl's
current context is that cluster in LAB_PROJECT (LAB_TOKEN_CLUSTER, default LAB_CLUSTER).

What this does and doesn't protect: the token stays out of the actor, but anyone who can read Secrets
in that namespace (cluster administrators, the credential provider) can read it, and it stays valid
for up to an hour after the refresher stops. Delete it when a session ends (--delete), and use a
project with nothing else of value in it.

usage: token_secret.py [--once | --delete]
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
CLUSTER = os.environ.get("LAB_TOKEN_CLUSTER") or os.environ.get("LAB_CLUSTER", "lab-substrate")
PROJECT = os.environ.get("LAB_PROJECT") or sys.exit("LAB_PROJECT is not set: source harness/lab_env.sh")


def check_context():
    """GKE contexts are named gke_<project>_<location>_<cluster>; refuse anything else."""
    ctx = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True,
                         timeout=30).stdout.strip()
    if not (ctx.startswith(f"gke_{PROJECT}_") and ctx.endswith(f"_{CLUSTER}")):
        sys.exit(f"refusing: kubectl context is {ctx!r}, not cluster {CLUSTER} in {PROJECT}. "
                 "Run infra/cluster.sh setup for that cluster, or set LAB_TOKEN_CLUSTER.")


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
    check_context()
    if "--delete" in sys.argv:
        subprocess.run(["kubectl", "-n", NAMESPACE, "delete", "secret", NAME, "--ignore-not-found"],
                       check=True, timeout=60)
        print(json.dumps({"event": "token secret deleted", "secret": f"{NAMESPACE}/{NAME}"}), flush=True)
        return
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
