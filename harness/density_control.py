#!/usr/bin/env python3
"""The standard-container control for the post's "10x the density of standard container runtimes".

Density is measured against a container control. The Substrate side is Google's own
agent-session workload (harness/google_bench.py `agentsession_*`): mostly idle coding-agent
sessions, suspended between steps, its frontier the largest session count that ran with no wake
failures and no crashes. This is the other side: the same session as an ordinary Kubernetes Pod,
which can't hibernate, so it holds its reservation while idle.

Same image (Google's glutton, read from the deployed `glutton` ActorTemplate), same command, the
same CPU and memory as each actor (requests equal limits, as Substrate books an actor's limits),
the standard container runtime (no runtimeClass), on the node pool the Substrate workers ran on
(recorded by google_bench.py). The Deployment scales up step by step until Pods stay Pending; the
frontier is the Running and Ready count, per node. The ratio against the Substrate frontier is what
the post's 10x is about, and it depends on how idle the sessions are (Google's script's think
times), which is stated with the result.

Source harness/lab_env.sh; kubectl at the Substrate cluster with Google's workloads deployed (for
the image). Nothing runs without --apply.

usage: density_control.py --nodepool POOL | --selector KEY=VALUE  [--cpu 1] [--memory 1Gi] [--step 4] [--max 400]
                          [--apply] [--out DIR] [--label NAME]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS, NAME = "lab-density", "lab-density-control"
APPLY = "--apply" in sys.argv


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def kubectl(*a, input=None, check=True):
    print("+ kubectl", " ".join(a), flush=True)
    if not APPLY:                                    # a dry run touches nothing, reads included
        return subprocess.CompletedProcess(a, 0, "{}", "")
    return subprocess.run(["kubectl", *a], input=input, capture_output=True, text=True, check=check)


def glutton() -> dict:
    """Image and command of Google's glutton actor, as deployed."""
    r = subprocess.run(["kubectl", "ate", "get", "actor-template", "glutton", "-a", "benchmark-workloads", "-o", "json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        if not APPLY:
            return {"image": "GLUTTON_IMAGE", "command": ["/ko-app/glutton"]}
        sys.exit(f"no glutton template; deploy Google's workloads first: {r.stderr[-300:]}")
    c = json.loads(r.stdout)["containers"][0]
    return {"image": c["image"], "command": c["command"]}


def manifest(g: dict, pool: str, cpu: str, memory: str, replicas: int, selector: dict | None = None) -> str:
    return json.dumps({
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": NAME, "namespace": NS},
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": NAME}},
                 "template": {"metadata": {"labels": {"app": NAME}}, "spec": {
                     "nodeSelector": selector or {"cloud.google.com/gke-nodepool": pool},
                     "tolerations": [{"key": "ate.dev/sandboxClass", "operator": "Equal", "value": "shared",
                                      "effect": "NoSchedule"}],
                     "automountServiceAccountToken": False,
                     "containers": [{"name": "glutton", "image": g["image"], "command": g["command"],
                                     "resources": {"requests": {"cpu": cpu, "memory": memory},
                                                   "limits": {"cpu": cpu, "memory": memory}},
                                     "readinessProbe": {"httpGet": {"path": "/readyz", "port": 80},
                                                        "periodSeconds": 5}}]}}}})


def counts() -> dict:
    pods = json.loads(kubectl("get", "pods", "-n", NS, "-l", f"app={NAME}", "-o", "json").stdout or "{}").get("items", [])
    ready, pending, per_node = 0, 0, {}
    for p in pods:
        st = p.get("status", {})
        if st.get("phase") == "Pending":
            pending += 1
        ok = any(c.get("type") == "Ready" and c.get("status") == "True" for c in st.get("conditions", []))
        if ok:
            ready += 1
            n = p["spec"].get("nodeName", "?")
            per_node[n] = per_node.get(n, 0) + 1
    return {"pods": len(pods), "ready": ready, "pending": pending, "per_node": per_node}


def node_facts(pool: str, selector: dict | None = None) -> list[dict]:
    sel = ",".join(f"{k}={v}" for k, v in (selector or {"cloud.google.com/gke-nodepool": pool}).items())
    items = json.loads(kubectl("get", "nodes", "-l", sel, "-o", "json").stdout
                       or "{}").get("items", [])
    return [{"name": n["metadata"]["name"],
             "machine_type": n["metadata"]["labels"].get("node.kubernetes.io/instance-type"),
             "allocatable": n["status"].get("allocatable", {})} for n in items]


def frontier(steps: list[dict]) -> dict:
    """The largest Ready count reached; the step where Pods first stayed Pending."""
    best = max(steps, key=lambda s: s["ready"]) if steps else {"ready": 0, "per_node": {}}
    stalled = next((s for s in steps if s["pending"] > 0), None)
    return {"ready": best["ready"], "per_node": best["per_node"],
            "first_pending_at_replicas": stalled["replicas"] if stalled else None}


def main():
    pool = arg("--nodepool")
    selector = dict([arg("--selector").split("=", 1)]) if arg("--selector") else None
    if not pool and not selector:
        sys.exit(__doc__)
    cpu, memory = arg("--cpu", "1"), arg("--memory", "1Gi")
    step, cap = int(arg("--step", 4)), int(arg("--max", 400))
    out = arg("--out", os.path.join(TOOLS, "runs", f"google-bench-{time.strftime('%Y%m%d')}"))
    label = arg("--label", "density-control")
    g = glutton()
    kubectl("create", "namespace", NS, check=False)
    steps = []
    try:
        replicas = step
        while replicas <= cap:
            kubectl("apply", "-f", "-", input=manifest(g, pool, cpu, memory, replicas, selector))
            deadline = time.time() + 180
            c = counts()
            while APPLY and time.time() < deadline and c["ready"] < replicas and not (
                    c["pending"] and time.time() > deadline - 120):
                time.sleep(5)
                c = counts()
            steps.append({"replicas": replicas, **c})
            print(json.dumps(steps[-1]), flush=True)
            if not APPLY or c["pending"]:
                break                                   # the scheduler refused: the frontier
            replicas += step
    finally:
        nodes = node_facts(pool, selector)
        kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true", "--timeout=300s", check=False)
    rec = {"probe": "density_control", "label": label, "image": g["image"], "runtime": "standard (no runtimeClass)",
           "requests_and_limits": {"cpu": cpu, "memory": memory}, "nodepool": pool, "selector": selector, "nodes": nodes,
           "steps": steps, "frontier": frontier(steps)}
    if APPLY:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, f"{label}.json"), "w") as fh:
            json.dump(rec, fh, indent=1)
    print(json.dumps(rec["frontier"]))


if __name__ == "__main__":
    main()
