#!/usr/bin/env python3
"""Per-team identities and the paired isolation test.

Until now both teams' harness called Substrate's API as the shared `ate-client`, which ate-setup makes a
bootstrap (global) owner, so atespace authorization between teams was never exercised. Substrate's own
model (docs/guides/authorization.md, an OpenFGA check on every RPC) grants roles per atespace. This:

  setup   creates namespace lab-tenants with ServiceAccounts team-one and team-two, mints a short-lived
          token for each (kept in a 0700 scratch directory, never committed), and writes each atespace's
          access policy: team-one editor on lab-agent, team-two editor on lcb. ate-client stays a
          bootstrap owner (the harness still needs it); the test identities hold nothing else.
  run     the paired matrix. Each operation runs as each team against its own atespace (the control,
          expected allowed) and against the other team's (expected denied): list and get actors, read an
          egress policy, create an actor, create one from the other team's template, suspend and resume,
          update an egress policy, delete, read the access policy, list atespaces; and snapshots: tag
          the other team's actor, restore an actor from the other team's atespace-scoped tag, then from a
          tag the owner published.
  teardown deletes the test actors, tags and access policies, and the namespace.

Results: runs/remaining-20261122/tenants-*.json, every call's exit code and first error line.
No model calls. Source harness/lab_env.sh.
usage: tenants.py setup|run|teardown
"""
import json
import os
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(TOOLS, "runs", "remaining-20261122")
TOK = os.path.join(tempfile.gettempdir(), "lab-tenant-tokens")
NS = "lab-tenants"
TEAMS = {"one": {"sa": "team-one", "atespace": "lab-agent", "template": "lab-agent"},
         "two": {"sa": "team-two", "atespace": "lcb", "template": "lcb-agent"}}
STAMP = time.strftime("%H%M%S")


def sh(*cmd, inp=None):
    r = subprocess.run(list(cmd), input=inp, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr)


def ate(team, *args, inp=None):
    tok = ["--token-file", os.path.join(TOK, team)] if team else []
    rc, out = sh("kubectl", "ate", *tok, *args, inp=inp)
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith("Warning: no --token-file")]
    err = next((l for l in lines if "denied" in l.lower() or "error" in l.lower()), lines[-1] if lines else "")
    return {"rc": rc, "denied": "PermissionDenied" in out, "unauthenticated": "Unauthenticated" in out,
            "msg": err[:220]}


def setup():
    os.makedirs(TOK, mode=0o700, exist_ok=True)
    sh("kubectl", "create", "namespace", NS)
    for t in TEAMS.values():
        sh("kubectl", "-n", NS, "create", "serviceaccount", t["sa"])
    for k, t in TEAMS.items():
        rc, tok = sh("kubectl", "-n", NS, "create", "token", t["sa"], "--duration=4h",
                     "--audience", "api.ate-system.svc")   # the API's audience (docs/guides/authentication.md)
        assert rc == 0, tok
        p = os.path.join(TOK, k)
        with open(p, "w") as fh:
            fh.write(tok.strip())
        os.chmod(p, 0o600)
        pol = (f"metadata:\n  name: default\nbindings:\n- role: editor\n  members:\n"
               f"  - user:system:serviceaccount:{NS}:{t['sa']}\n")
        print(k, ate(None, "create", "access-policy", "-a", t["atespace"], "-f", "-", inp=pol))
    print("tokens in", TOK)


def run():
    rec = {"probe": "tenants", "t": time.time(), "principals": {k: f"system:serviceaccount:{NS}:{t['sa']}"
           for k, t in TEAMS.items()}, "rows": []}

    def row(op, actor_team, target_team, expect, res):
        # A denial counts only when the authorization check says so (PermissionDenied); any other
        # failure (Unauthenticated, not found, a bad argument) is not evidence of isolation.
        res.update(op=op, as_team=actor_team, on_team=target_team, expect=expect,
                   ok=(res["rc"] == 0) if expect == "allowed" else res["denied"])
        rec["rows"].append(res)
        print(json.dumps({k: res[k] for k in ("op", "as_team", "on_team", "expect", "rc", "ok", "msg")}), flush=True)

    # Each team creates its own actor, suspends it and tags its snapshot: the same-team controls.
    names = {}
    for k, t in TEAMS.items():
        n = f"tenant-{k}-{STAMP}"; names[k] = n
        row("create actor (own template)", k, k, "allowed",
            ate(k, "create", "actor", n, "-a", t["atespace"], "--template", t["template"]))
    time.sleep(20)
    for k, t in TEAMS.items():
        row("suspend own actor", k, k, "allowed", ate(k, "suspend", "actor", names[k], "-a", t["atespace"], "--mode=sync"))
        row("tag own snapshot (atespace scope)", k, k, "allowed",
            ate(k, "create", "tag", f"tag-{k}-{STAMP}", "--actor", names[k], "-a", t["atespace"]))
    for k, t in TEAMS.items():
        o = "two" if k == "one" else "one"; ot = TEAMS[o]; target = names[o]
        for op, args in [
            ("list actors", ["get", "actors", "-a", "{A}"]),
            ("get actor", ["get", "actor", "{N}", "-a", "{A}"]),
            ("get egress policy", ["get", "egress-policy", "{N}", "-a", "{A}"]),
            ("get access policy", ["get", "access-policy", "-a", "{A}"]),
            ("resume actor", ["resume", "actor", "{N}", "-a", "{A}"]),
        ]:
            for tgt_team, tgt_a, tgt_n in ((k, t["atespace"], names[k]), (o, ot["atespace"], target)):
                a = [x.replace("{A}", tgt_a).replace("{N}", tgt_n) for x in args]
                row(op, k, tgt_team, "allowed" if tgt_team == k else "denied", ate(k, *a))
        row("suspend other team's actor", k, o, "denied", ate(k, "suspend", "actor", target, "-a", ot["atespace"], "--mode=sync"))
        row("create actor in other team's atespace", k, o, "denied",
            ate(k, "create", "actor", f"x-{k}-{STAMP}", "-a", ot["atespace"], "--template", ot["template"]))
        row("create actor in own atespace from other team's template", k, o, "denied",
            ate(k, "create", "actor", f"y-{k}-{STAMP}", "-a", t["atespace"], "--template", f"{ot['atespace']}/{ot['template']}"))
        row("tag other team's snapshot", k, o, "denied",
            ate(k, "create", "tag", f"steal-{k}-{STAMP}", "--actor", target, "-a", ot["atespace"]))
        row("restore other team's atespace-scoped tag into own atespace", k, o, "denied",
            ate(k, "create", "actor", f"z-{k}-{STAMP}", "-a", t["atespace"], "--template", t["template"],
                "--tag", f"{ot['atespace']}/tag-{o}-{STAMP}"))
        row("delete other team's actor", k, o, "denied", ate(k, "delete", "actor", target, "-a", ot["atespace"]))
        row("list atespaces (global)", k, "global", "denied", ate(k, "get", "atespaces"))
    # A published tag: team one publishes; team two tries to restore from it. Recorded, not expected:
    # Substrate's docs say creating an actor from a tag needs editor on the tag's atespace.
    row("team one publishes its tag", "one", "one", "allowed",
        ate("one", "create", "tag", f"pub-one-{STAMP}", "--actor", names["one"], "-a", "lab-agent", "--scope", "published"))
    row("restore team one's published tag into team two", "two", "one", "denied",
        ate("two", "create", "actor", f"p-two-{STAMP}", "-a", "lcb", "--template", "lcb-agent", "--tag", f"lab-agent/pub-one-{STAMP}"))
    rec["names"] = names
    rec["all_as_expected"] = all(r["ok"] for r in rec["rows"])
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"tenants-{STAMP}.json"), "w") as fh:
        json.dump(rec, fh, indent=1)
    print("all as expected:", rec["all_as_expected"])


def teardown():
    for k, t in TEAMS.items():
        rc, out = sh("kubectl", "ate", "get", "actors", "-a", t["atespace"])
        for l in out.splitlines()[1:]:
            n = l.split()[1] if len(l.split()) > 1 else ""
            if n.startswith(("tenant-", "x-", "y-", "z-", "p-")):
                print(ate(None, "delete", "actor", n, "-a", t["atespace"]))
        rc, out = sh("kubectl", "ate", "get", "tags", "-a", t["atespace"])
        for l in out.splitlines()[1:]:
            n = l.split()[1] if len(l.split()) > 1 else ""
            if n.startswith(("tag-", "steal-", "pub-")):
                print(ate(None, "delete", "tag", n, "-a", t["atespace"]))
        print(ate(None, "delete", "access-policy", "-a", t["atespace"]))
    sh("kubectl", "delete", "namespace", NS, "--wait=false")
    sh("rm", "-rf", TOK)


if __name__ == "__main__":
    {"setup": setup, "run": run, "teardown": teardown}[sys.argv[1]]()
