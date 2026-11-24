#!/usr/bin/env python3
"""The --dangerous gate: no agent runs with its own permission prompts off on a venue whose containment
probes haven't passed on that install.

`--dangerous` turns agy's permission prompts off, so the runtime's controls are the only ones. The lab
ran the containment probes first and read them by hand; this makes that a check. It reads the two
records `harness/probe_run.py` writes (runs/probes/api_scope-VENUE.json and escape_suite-VENUE.json)
and refuses unless every containment reading passed:

  api_scope     the credential the agent finds is a placeholder; no real metadata token; no
                credential files; no route to storage; no answers for outside DNS names.
  escape_suite  nothing writable outside the workspace; no Kubernetes service-account token; the real
                metadata server, kubelet, the API server and the neighbor's canary (by name and by
                address) all refused; the listener, if one was given, not reached.

On a cluster venue the records must also come from this install: recorded on the kube context you're
pointed at now, after the cluster was created. A reinstall, or probes from another cluster, don't count.
The gate tests the records it's given; it can't tell whether the policies changed since they ran.

The criteria are the lab's Substrate readings. On the plain GKE baselines the agent can read a real
short-lived token by design, so they fail here; that's what those venues measure, and --dangerous wasn't
run on them.

Passing sets LAB_CONTAINMENT for the agent wrapper (controller/models.py), which refuses --dangerous
without it. An explicit override, with a reason, is recorded in the plan and on every agent call.

usage: containment_gate.py VENUE [--probes DIR]     (prints the verdict; exit 0 pass, 1 refuse)
"""
import json
import os
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBES = os.path.join(TOOLS, "runs", "probes")
ENV = "LAB_CONTAINMENT"


def refused(v) -> bool:
    return isinstance(v, dict) and "refused" in v


def api_scope_failures(r: dict) -> list[str]:
    c, vi = r.get("credential") or {}, r.get("verdict_inputs") or {}
    out = []
    if c.get("placeholder") is not True:
        out.append("api_scope: the credential the agent found isn't a placeholder")
    if c.get("real_metadata_token") is not False:
        out.append("api_scope: the agent reached a real metadata token")
    if any((c.get("files") or {}).values()):
        out.append("api_scope: a credential file is readable: "
                   + ", ".join(k for k, v in (c.get("files") or {}).items() if v))
    if vi.get("storage_reached") is not False:
        out.append("api_scope: Cloud Storage was reachable")
    if vi.get("outside_dns") is not False:
        out.append("api_scope: outside DNS names resolved")
    return out


def escape_failures(r: dict) -> list[str]:
    out = []
    if r.get("writable_outside_workspace"):
        out.append(f"escape_suite: writable outside the workspace: {r['writable_outside_workspace']}")
    if r.get("sa_token_mounted") is not False:
        out.append("escape_suite: a Kubernetes service-account token is mounted")
    for k in ("metadata_real_identity", "kubelet_10250", "kubelet_10255", "k8s_api_version_anonymous",
              "k8s_api_secrets_anonymous", "neighbor_canary", "neighbor_canary_by_ip"):
        if k in r and not refused(r[k]):
            out.append(f"escape_suite: {k} wasn't refused")
    for k in ("metadata_real_identity", "neighbor_canary"):
        if k not in r:
            out.append(f"escape_suite: no {k} reading")
    if "listener" in r and not refused(r["listener"]):
        out.append("escape_suite: the listener was reached")
    return out


def cluster_created(ctx: str):
    """Creation time of the cluster a gke_<project>_<location>_<cluster> context names, or None."""
    parts = ctx.split("_")
    if len(parts) < 4 or parts[0] != "gke":
        return None
    project, location, cluster = parts[1], parts[2], "_".join(parts[3:])
    r = subprocess.run(["gcloud", "container", "clusters", "describe", cluster, "--location", location,
                        "--project", project, "--format", "value(createTime)"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    from datetime import datetime
    return datetime.fromisoformat(r.stdout.strip().replace("Z", "+00:00")).timestamp()


def check(venue: str, probes: str = PROBES, context=None, created=None) -> dict:
    """The verdict and the evidence it rests on. context/created are looked up unless given (tests)."""
    failures, ev = [], {"venue": venue, "probes": {}}
    recs = {}
    for name in ("api_scope", "escape_suite"):
        path = os.path.join(probes, f"{name}-{venue}.json")
        if not os.path.exists(path):
            failures.append(f"no {name} record at {path}")
            continue
        rec = json.load(open(path))
        recs[name] = rec
        ev["probes"][name] = {"path": path, "t": (rec.get("result") or {}).get("t"),
                              "run_token": rec.get("run_token"), "kube_context": rec.get("kube_context")}
        if rec.get("exit") != 0 or not isinstance(rec.get("result"), dict) or "unparsed" in rec["result"]:
            failures.append(f"{name}: the probe didn't finish cleanly (exit {rec.get('exit')})")
            continue
        failures += (api_scope_failures if name == "api_scope" else escape_failures)(rec["result"])
    if venue != "local" and recs:
        if context is None:
            context = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True,
                                     timeout=30).stdout.strip()
        ev["kube_context"] = context
        for name, rec in recs.items():
            if rec.get("kube_context") != context:
                failures.append(f"{name}: recorded on {rec.get('kube_context')!r}, not the current context {context!r}")
        if created is None:
            created = cluster_created(context)
        ev["cluster_created"] = created
        if created is None:
            failures.append(f"can't read when {context!r}'s cluster was created (no such cluster, or the sign-in expired)")
        else:
            for name, rec in recs.items():
                t = (rec.get("result") or {}).get("t")
                if not isinstance(t, (int, float)) or t < created:
                    failures.append(f"{name}: recorded before this cluster was created (a previous install)")
    ev["passed"], ev["failures"] = not failures, failures
    return ev


def require(venue: str, probes: str = PROBES, override: str | None = None) -> dict:
    """Pass, or exit with the reasons. An override (a reason string) runs anyway and is recorded."""
    ev = check(venue, probes)
    if ev["passed"]:
        os.environ[ENV] = f"passed:{venue}:{','.join(p['run_token'] or '' for p in ev['probes'].values())}"
    elif override:
        ev["override"] = override
        os.environ[ENV] = f"override:{venue}:{override}"
    else:
        sys.exit("refusing --dangerous: the containment probes haven't passed on this install.\n  - "
                 + "\n  - ".join(ev["failures"])
                 + f"\nRun: $LAB_PYTHON harness/probe_run.py --venue {venue} --out runs/probes, read the results, "
                   "and rerun. To run anyway, pass --dangerous-override REASON (recorded on every call).")
    ev["checked_at"] = time.time()
    return ev


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    d = sys.argv[sys.argv.index("--probes") + 1] if "--probes" in sys.argv else PROBES
    ev = check(sys.argv[1], d)
    print(json.dumps(ev, indent=1, default=str))
    sys.exit(0 if ev["passed"] else 1)
