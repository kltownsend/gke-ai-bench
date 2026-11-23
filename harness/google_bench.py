#!/usr/bin/env python3
"""Google's Substrate benchmark code, under lab configurations, against the lab's install, for the
2026-11-05 claims. (Round 9: only `durdir_implicit_resume` is a catalog entry verbatim; the rest are
Google's driver and workloads at sizes and settings the lab chose, each labeled. The load generator
runs on its own node, as Google's automation intends: setup adds a
one-node pool tainted `ate.dev/benchmarking=true:NoSchedule`, the taint Google's Job template
already tolerates, and the rendered Job adds a nodeSelector for it, so the runner can't land on a
worker's node. Google's automation puts the runner on a separate cluster; a separate node in the
same cluster still shares the control plane and the network, disclosed with every result.)

The vendor-supported way to measure Substrate's performance claims is the suite in its repository
(agent-substrate/substrate at v1.0.1, benchmarking/, docs/guides/benchmarking.md). Its nightly
automation runs on a second, orchestration cluster with a CronJob. The lab skips that scheduler and
does the same steps by hand on the one Substrate cluster, with Google's own pieces:

- **setup:** register the runner's ServiceAccount (`benchmarking/benchmark-runner`) as an API owner
  and an ingress client, the two settings the automation's install sets (README, step 5; the
  installer the lab uses doesn't ask). The lab appends to both flags on the running Deployments,
  the way AX's installer registers itself. Then the guide's optional Prometheus
  (`benchmarking/monitoring.yaml`), so the runner's server-side numbers (restore and checkpoint
  percentiles, snapshot sizes, packing) come from the runtime's own metrics, and a results bucket
  the runner can write.
- **build:** the runner image, with Google's `benchmarking/locust/build_and_push.sh`, pushed to the
  lab's registry.
- **run TEST:** for each test, Google's `benchmarking/workloads/deploy.sh` at that test's sizing,
  then Google's `runner-job.yaml.tmpl` rendered the way `automation/orchestrator.py` renders it
  (`render_template`: the `${...}` substitutions, the test's flags appended to the runner's args),
  applied, waited for, and its results copied out of the bucket. The workloads are torn down
  after each test, as the orchestrator does.
- **down:** the workloads, the runner's namespace and bindings, the bucket.

The tests are catalog entries (`benchmarking/automation/tests.yaml`) or the guide's documented
invocations, scaled to the lab's cluster where the catalog assumes a larger one; each entry below
says which and what changed. The fake data plane the catalog's cold-resume ceiling runs on
(`--fake-data-plane`, no real restore) needs a separate install and isn't in this table.

Source harness/lab_env.sh; kubectl at the Substrate cluster. Nothing runs without --apply; without
it every command is printed.

**slo SECONDS:** the resume-latency SLI the way Substrate's SLO guide writes its availability SLIs
(good events over valid events, docs/guides/slos-and-alerts.md), from the runtime's own histogram
`ate.actor.lifecycle.operation.duration` (operation `resume`; it "counts the true activations and
not the traffic of the router"; buckets include 0.5 and 1 exactly), over the last SECONDS, at the
two published thresholds: 500 ms (the 2026-11-05 post) and 1 s (GKE's About page). A failed resume
counts as not good. The objective (what fraction must be good) is unpublished; the result is the
fraction. The atelet's restore histogram is reported beside it.

usage: google_bench.py setup|build|run TEST|slo SECONDS|down|list [--apply] [--out DIR]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUB = os.path.expanduser(os.environ.get("SUBSTRATE_DIR", "~/lab-src/substrate"))
APPLY = "--apply" in sys.argv


def lab_env(name: str) -> str:
    """A value from harness/lab_env.sh. Required with --apply; a dry run prints a placeholder."""
    v = os.environ.get(name)
    if v:
        return v
    if APPLY:
        sys.exit(f"{name} is not set: fill in lab.env (see lab.env.example) and source harness/lab_env.sh")
    return f"<{name}>"


PROJECT = lab_env("LAB_PROJECT")
REGION = lab_env("LAB_REGION")
REPO = os.environ.get("LAB_REGISTRY") or f"{REGION}-docker.pkg.dev/{PROJECT}/lab-images"


def project_number() -> str:
    """The project number, for the runner's Workload Identity principal, read at runtime
    (LAB_PROJECT_NUMBER in lab.env skips the lookup)."""
    if os.environ.get("LAB_PROJECT_NUMBER"):
        return os.environ["LAB_PROJECT_NUMBER"]
    if not APPLY:
        return "<PROJECT_NUMBER>"
    r = subprocess.run(["gcloud", "projects", "describe", PROJECT, "--format", "value(projectNumber)"],
                       capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        sys.exit(f"cannot read the project number of {PROJECT}: {r.stderr.strip()}")
    return r.stdout.strip()


# The API authenticates the runner by its pod certificate's SPIFFE ID, so the owner entry is the
# SPIFFE form, as the guide's own example writes it (the ServiceAccount form was refused,
# PermissionDenied on every call, in the local shakeout).
RUNNER_SA = "spiffe://cluster.local/ns/benchmarking/sa/benchmark-runner"
RUNNER_SPIFFE = "spiffe://cluster.local/ns/benchmarking/sa/benchmark-runner"
COMMIT = "20778a6d"                     # v1.0.1, the installed release
IMAGE = os.environ.get("LAB_BENCH_IMAGE") or f"{REPO}/locust-test:v1.0.1"
TEMPLATE = os.path.join(SUB, "benchmarking", "automation", "manifests", "runner-job.yaml.tmpl")
CLUSTER = os.environ.get("SUBSTRATE_CLUSTER") or os.environ.get("LAB_CLUSTER") or "lab-substrate"
ZONE = lab_env("LAB_ZONE")
RUNNER_POOL = "lab-bench-runner"
RUNNER_MACHINE = "e2-standard-4"            # about $0.13 an hour; the lab's runners ask 1 CPU
# Google's scripts need bash 4.4 or later (macOS ships 3.2; `deploy.sh` fails on its empty-array
# bug). GNU bash from Homebrew, unlinked, reached through a directory that holds only a `bash`
# link, put first on PATH for Google's subprocesses alone. LAB_BASH5_DIR (lab.env) names it;
# by default it sits in the lab's gcloud config directory and is used only if it exists.
BASH5_DIR = os.path.expanduser(os.environ.get("LAB_BASH5_DIR")
                               or os.path.join(os.environ.get("CLOUDSDK_CONFIG", "~/.config/gcloud"), "bash5-bin"))


def google_env(**extra) -> dict:
    env = dict(os.environ, **extra)
    if os.path.exists(os.path.join(BASH5_DIR, "bash")):
        env["PATH"] = BASH5_DIR + os.pathsep + env.get("PATH", "")
    return env
# The local shakeout (kind; infra/substrate/local_env.sh): no node pool, no bucket, the runner's
# results stay in its pod (the Job log is the check), images in the kind registry. Never results.
LOCAL = os.environ.get("ATE_INSTALL_KIND") == "true"
if LOCAL:
    REPO = os.environ.get("KO_DOCKER_REPO", "localhost:5001")
    IMAGE = f"{REPO}/locust-test:v1.0.1"

# Each test: what the runner runs, the workload sizing deploy.sh gets, and where it comes from.
TESTS = {
    # The post: restore "in under 500 milliseconds"; "when the next request arrives". The catalog's
    # traffic-triggered resume test, verbatim.
    "durdir_implicit_resume": {
        "source": "catalog, verbatim", "file": "/app/tests/durdir.py", "duration": "1m", "users": 1,
        "deploy": {"worker_count": 1},
        "flags": ["--durdir-template", "glutton-durdir-data", "--resume-mode", "implicit",
                  "--durdir-file-size-bytes", "8388608", "--min-wait-time", "1.0", "--max-wait-time", "1.0"]},
    # The guide's coding-agent emulation (WakeFirstTouch is the user-visible wake), at the guide's
    # documented actor size (1Gi) and sync suspends, 4 sessions for 10 minutes (the guide gives no
    # catalog entry; the lab's sizing).
    "agentsession_4": {
        "source": "guide's agent-session invocation; 4 users, 10 min, sync lifecycle (lab sizing)",
        "file": "/app/tests/agentsession.py", "duration": "10m", "users": 4,
        "deploy": {"worker_count": 2, "actor_memory": "1Gi"},
        "flags": ["--resume-mode", "implicit", "--lifecycle-mode", "sync", "--agentsession-phase-in", "30"]},
    # The Substrate side of density: Google's agent-session workload
    # at a ladder of session counts, until wakes fail or actors crash; harness/density_control.py is
    # the standard-container side on the same node pool. Lab sizing, labeled.
    **{f"agentsession_density_{n}": {
        "source": f"density ladder: the guide's agent-session invocation, {n} sessions, 10 min (lab sizing)",
        "file": "/app/tests/agentsession.py", "duration": "10m", "users": n,
        "deploy": {"worker_count": 2, "actor_memory": "1Gi"},
        "flags": ["--resume-mode", "implicit", "--lifecycle-mode", "sync", "--agentsession-phase-in", "60"]}
       for n in (8, 16, 32, 64)},
    # Wake from storage, in-cluster. The lab's own storage
    # probe timed each wake through a fresh `kubectl ate proxy` tunnel, whose new-connection cost
    # (about 1 s on an awake actor) swamped the wake. Here Google's runner, inside the cluster, wakes
    # each parked session through the router. The coding-session script's first think gap is 2 s;
    # x1300 with the runner's +/-20% jitter puts it at 2080 to 3120 s, past the 30-minute detach, so
    # every session's first WakeFirstTouch reads its snapshot from storage. 12 sessions, one wake
    # each, matching the lab probe's n. Lab sizing, labeled.
    "agentsession_storage_12": {
        "source": "guide's agent-session invocation, think scale 1300 so the first wake is past the detach; "
                  "12 users, 55 min (lab sizing)",
        "file": "/app/tests/agentsession.py", "duration": "55m", "users": 12,
        "deploy": {"worker_count": 2, "actor_memory": "1Gi"},
        "flags": ["--resume-mode", "implicit", "--lifecycle-mode", "sync", "--agentsession-phase-in", "60",
                  "--agentsession-think-scale", "1300"]},
    # Spawn: the catalog's smoke, with 2 workers instead of 10 (the lab's cluster). Plumbing and
    # spawn latency only; it says nothing about density (round 9, finding 4).
    "spawn_10": {
        "source": "catalog spawn_smoke_10_actors; workerCount 10 -> 2 and 250m actors (lab sizing: two 4-vCPU "
                  "nodes can't hold ten 1-CPU actors; the first GKE try had 488 'no worker has room')",
        "file": "/app/tests/spawn.py,/app/shapes/spawn_shape.py", "duration": "5m", "users": 1,
        "deploy": {"worker_count": 2, "actor_cpu": "250m"},
        "flags": ["--total-actors", "10", "--spawn-concurrency", "2", "--actor-deadline", "120"]},
    # Cold resumes per second on the REAL data plane: the catalog's resumepaused smoke shape (10m
    # actors, ladder) with real restores instead of the fake atelet, and rates a 2-worker cluster
    # can attempt. The post's "more than 500 per second" is the claim; the catalog's own ceiling
    # test for it runs on a fake data plane.
    "resumepaused_real": {
        "excluded": "the post's per-second figure is an observation, not a testable claim; "
                    "two workers can't speak to a fleet-scale rate",
        "source": "a lab benchmark with Google's driver: the catalog's fake-data-plane smoke shape, run on the "
                  "real data plane at offered rates of 5/10/20 per s; report achieved rates and failures, not the targets",
        "file": "/app/tests/resumepaused.py,/app/shapes/ladder_shape.py", "duration": "8m", "users": 50,
        "deploy": {"worker_count": 2, "actor_cpu": "10m"},
        "flags": ["--ladder-spawn-rate", "50", "--ladder", "50:2m@5,50:2m@10,50:2m@20"]},
}


def say(*cmd, **kw):
    print("+", " ".join(cmd) if all(isinstance(c, str) for c in cmd) else cmd, flush=True)
    if not APPLY:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(list(cmd), text=True, **kw)


STATE = os.path.join(TOOLS, "runs", ".google-bench-state.json")


def load_state() -> dict:
    if os.path.exists(STATE):
        with open(STATE) as fh:
            return json.load(fh)
    return {"bucket": f"lab-bench-results-{uuid.uuid4().hex[:8]}", "original_args": {}, "done": []}


def save_state(st: dict):
    """Written before each change, so a teardown after a partial setup knows what to undo."""
    if APPLY:
        with open(STATE, "w") as fh:
            json.dump(st, fh, indent=1)


def bucket() -> str:
    return load_state()["bucket"]


def append_flag(args: list[str], flag: str, value: str) -> tuple[list[str], bool]:
    """Append value to a comma-list flag (`--flag=a,b` or `--flag a,b`), adding the flag if absent.
    Returns the new args and whether anything changed."""
    out, changed, found = list(args), False, False
    for i, a in enumerate(out):
        if a.startswith(flag + "="):
            found = True
            vals = [v for v in a.split("=", 1)[1].split(",") if v]
            if value not in vals:
                out[i], changed = f"{flag}={','.join(vals + [value])}", True
        elif a == flag and i + 1 < len(out):
            found = True
            vals = [v for v in out[i + 1].split(",") if v]
            if value not in vals:
                out[i + 1], changed = ",".join(vals + [value]), True
    if not found:
        out.append(f"{flag}={value}")
        changed = True
    return out, changed


def container_args(deploy: str, container: str) -> tuple[int, list[str]]:
    d = json.loads(subprocess.run(["kubectl", "-n", "ate-system", "get", "deploy", deploy, "-o", "json"],
                                  capture_output=True, text=True, check=True).stdout)
    cs = d["spec"]["template"]["spec"]["containers"]
    idx = next(i for i, c in enumerate(cs) if c["name"] == container)
    return idx, list(cs[idx].get("args", []))


def set_args(deploy: str, idx: int, args: list[str]):
    patch = [{"op": "replace", "path": f"/spec/template/spec/containers/{idx}/args", "value": args}]
    say("kubectl", "-n", "ate-system", "patch", "deploy", deploy, "--type=json", "-p", json.dumps(patch), check=True)
    say("kubectl", "-n", "ate-system", "rollout", "status", f"deploy/{deploy}", "--timeout=300s", check=True)


def register(st: dict, deploy: str, container: str, flag: str, value: str):
    idx, original = container_args(deploy, container)
    args, changed = append_flag(original, flag, value)
    if not changed:
        print(f"{deploy}: {flag} already has {value}")
        return
    st["original_args"].setdefault(deploy, {"container": container, "idx": idx, "args": original})
    save_state(st)                                  # the original goes on disk before the change
    patch = [{"op": "replace", "path": f"/spec/template/spec/containers/{idx}/args", "value": args}]
    say("kubectl", "-n", "ate-system", "patch", "deploy", deploy, "--type=json", "-p", json.dumps(patch), check=True)
    say("kubectl", "-n", "ate-system", "rollout", "status", f"deploy/{deploy}", "--timeout=300s", check=True)


def setup():
    st = load_state()
    save_state(st)                                  # before the first change
    register(st, "ate-api-server", "ate-api-server", "--authz-bootstrap-owners", RUNNER_SA)
    register(st, "atenet-router", "atenet-router", "--ingress-allowed-spiffe-ids", RUNNER_SPIFFE)
    if LOCAL:
        st["done"].append("monitoring")
        save_state(st)
        say("kubectl", "apply", "-f", os.path.join(SUB, "benchmarking", "monitoring.yaml"), check=True)
        return
    # Install prerequisite of Google's benchmark, not account posture: Google's workloads are built
    # into the lab's registry (lab-images in LAB_PROJECT) and pulled by the nodes, which the Substrate
    # installer runs as its own node service account, `ate-gke-node@LAB_PROJECT` (no read there by
    # default; the Executor rung hit the same). The grant is reader on that one repository; `down`
    # removes it.
    st["done"].append("registry-read")
    save_state(st)
    say("gcloud", "artifacts", "repositories", "add-iam-policy-binding", "lab-images", "--project", PROJECT,
        "--location", REGION, "--role", "roles/artifactregistry.reader",
        "--member", f"serviceAccount:ate-gke-node@{PROJECT}.iam.gserviceaccount.com", "--format", "none", check=True)
    st["done"].append("nodepool")
    save_state(st)
    say("gcloud", "container", "node-pools", "create", RUNNER_POOL, "--cluster", CLUSTER, "--zone", ZONE,
        "--project", PROJECT, "--machine-type", RUNNER_MACHINE, "--num-nodes", "1",
        "--node-taints", "ate.dev/benchmarking=true:NoSchedule", "--node-labels", "ate.dev/benchmarking=true",
        "--workload-metadata", "GKE_METADATA", check=True)
    st["done"].append("monitoring")
    save_state(st)
    say("kubectl", "apply", "-f", os.path.join(SUB, "benchmarking", "monitoring.yaml"), check=True)
    st["done"].append("bucket")
    save_state(st)
    b = st["bucket"]
    say("gcloud", "storage", "buckets", "create", f"gs://{b}", "--project", PROJECT, "--location", REGION,
        "--uniform-bucket-level-access", check=True)
    say("gcloud", "storage", "buckets", "add-iam-policy-binding", f"gs://{b}", "--role", "roles/storage.objectAdmin",
        "--member", f"principal://iam.googleapis.com/projects/{project_number()}/locations/global/workloadIdentityPools/"
                    f"{PROJECT}.svc.id.goog/subject/ns/benchmarking/sa/benchmark-runner", check=True)


def build():
    env = google_env(LOCUST_IMAGE=IMAGE, **({"LOCUST_IMAGE_PLATFORM": "linux/arm64"} if LOCAL else {}))
    say("sh", "-c", f"cd {SUB} && benchmarking/locust/build_and_push.sh", env=env, check=True)


def millicores(cpu: str) -> int:
    return int(cpu[:-1]) if cpu.endswith("m") else int(float(cpu) * 1000)


def mem_bytes(q: str) -> int:
    units = {"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30}
    return int(q[:-2]) * units[q[-2:]] if q[-2:] in units else int(q)


def deploy_workloads(sizing: dict, delete=False):
    cmd = ["benchmarking/workloads/deploy.sh", "--delete" if delete else "--deploy"]
    if not delete:
        cmd += ["--worker-count", str(sizing.get("worker_count", 1)), "--sandbox-class", "gvisor"]
        for k, f in (("actor_memory", "--actor-memory"), ("actor_cpu", "--actor-cpu")):
            if sizing.get(k):
                cmd += [f, sizing[k]]
    env = google_env(KO_DOCKER_REPO=REPO, **({} if LOCAL else {"PROJECT_ID": PROJECT}))
    if delete:
        # v1.0.1's deploy.sh sets these only in its deploy branch, and its delete runs the same
        # substitution under `set -u`, so `--delete` fails as shipped. Passed in, not patched.
        env["ACTOR_MEMORY_BYTES"] = str(mem_bytes(sizing.get("actor_memory", "256Mi")))
        env["ACTOR_CPU_MILLICORES"] = str(millicores(sizing.get("actor_cpu", "1")))
    say("sh", "-c", f"cd {SUB} && " + " ".join(cmd), env=env, check=not delete)


def render(name: str, t: dict, job: str, dest: str) -> str:
    """orchestrator.render_template, without its YAML dependency: the same `${...}` substitutions,
    and the test's flags appended to the runner container's args (after the --dest pair)."""
    text = open(TEMPLATE).read()
    subs = {"JOB_NAME": job, "IMAGE": IMAGE, "TAG": COMMIT, "NAME": name, "DEST": dest, "TEST_FILE": t["file"],
            "DURATION": t["duration"], "USERS": t["users"], "RUNNER_CPU": t.get("runner_cpu", "1"),
            "RUNNER_MEMORY": t.get("runner_memory", "1Gi")}
    for k, v in subs.items():
        text = text.replace("${" + k + "}", str(v))
    left = re.findall(r"\$\{[A-Z_]+\}", text)
    if left:
        raise ValueError(f"unsubstituted placeholders: {sorted(set(left))}")
    meta = json.dumps({"cluster_name": CLUSTER, "project_id": PROJECT, "lab_test": name})
    flags = list(t["flags"]) + ["--metadata", meta]
    anchor = f'        - "--dest"\n        - "{dest}"\n'
    if text.count(anchor) != 1:
        raise ValueError("the template's --dest args moved; re-read runner-job.yaml.tmpl")
    extra = "".join(f"        - {json.dumps(str(f))}\n" for f in flags)
    text = text.replace(anchor, anchor + extra)
    # The lab's one addition to Google's Job: pin the runner to its own node (Google's template
    # tolerates the taint but doesn't select the node).
    sa = "      serviceAccountName: benchmark-runner\n"
    if LOCAL:
        return text
    if text.count(sa) != 1:
        raise ValueError("the template's serviceAccountName moved; re-read runner-job.yaml.tmpl")
    return text.replace(sa, sa + '      nodeSelector:\n        ate.dev/benchmarking: "true"\n')


def run(name: str, out_dir: str):
    t = dict(TESTS[name])
    for flag, key in (("--users", "users"), ("--duration", "duration")):   # shakeout overrides, recorded
        if flag in sys.argv:
            t[key] = sys.argv[sys.argv.index(flag) + 1]
            t["source"] += f"; {key} overridden to {t[key]} (shakeout)"
    if t.get("excluded") and "--force" not in sys.argv:
        sys.exit(f"{name} is excluded: {t['excluded']}")
    deploy_workloads(t["deploy"])
    job = f"runner-{name.replace('_', '-')}-{uuid.uuid4().hex[:6]}"[:63]
    dest = (f"/tmp/bench/{name}" if LOCAL else f"gs://{bucket()}/{name}/{time.strftime('%Y%m%dT%H%M%S')}")
    manifest = render(name, t, job, dest)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{job}.yaml"), "w") as fh:
        fh.write(manifest)
    say("kubectl", "apply", "-f", "-", input=manifest, check=True)
    secs = 60 * int(re.match(r"(\d+)m", t["duration"]).group(1)) + 1800
    w = say("kubectl", "-n", "benchmarking", "wait", "--for=condition=complete", f"job/{job}", f"--timeout={secs}s")
    keep(job, out_dir)                              # logs, description and events, success or not
    pools = worker_pools()                          # where the workers ran, for the density control
    ok = w.returncode == 0
    agg = served(open(os.path.join(out_dir, f"{job}.log.txt")).read()) if APPLY else {"ok": True}
    ok = ok and agg["ok"]
    if ok and not LOCAL:
        os.makedirs(os.path.join(out_dir, name), exist_ok=True)
        c = say("gcloud", "storage", "cp", "-r", dest, os.path.join(out_dir, name))
        ok = c.returncode == 0 and (not APPLY or has_stats(os.path.join(out_dir, name)))
    result = {"test": name, "job": job, "dest": dest, "source": t["source"], "complete": ok, "served": agg,
              "worker_nodepools": pools,
              "runner_placement": ("local shakeout: one kind node, shared" if LOCAL else
                                   f"its own node ({RUNNER_POOL}, {RUNNER_MACHINE}) in the cluster under test; "
                                   "Google's automation uses a separate cluster (disclosed)")}
    if APPLY:
        with open(os.path.join(out_dir, f"{job}.result.json"), "w") as fh:
            json.dump(result, fh, indent=1)
    if not ok:
        # Fail closed: the Job and the workloads stay for inspection; nothing is deleted.
        print(json.dumps(result))
        sys.exit(f"{name}: the run didn't complete or its results are missing; Job {job} and the workloads "
                 "are left in place. Inspect, then `google_bench.py down --apply`.")
    say("kubectl", "-n", "benchmarking", "delete", "job", job, "--ignore-not-found", check=True)
    deploy_workloads(t["deploy"], delete=True)
    print(json.dumps(result))


def worker_pools() -> list[str]:
    """The node pools the benchmark workers ran on (the density control runs its Pods there)."""
    if not APPLY:
        return []
    pods = json.loads(subprocess.run(["kubectl", "-n", "benchmark-workloads", "get", "pods", "-o", "json"],
                                     capture_output=True, text=True).stdout or "{}").get("items", [])
    nodes = {p["spec"].get("nodeName") for p in pods if p["spec"].get("nodeName")}
    pools = set()
    for n in nodes:
        r = subprocess.run(["kubectl", "get", "node", n, "-o", "jsonpath={.metadata.labels.cloud\\.google\\.com/gke-nodepool}"],
                           capture_output=True, text=True)
        pools.add(r.stdout.strip())
    return sorted(p for p in pools if p)


def keep(job: str, out_dir: str):
    for what, cmd in (("log", ["logs", f"job/{job}", "--tail=-1", "--all-containers"]),
                      ("describe", ["describe", "job", job]),
                      ("events", ["get", "events", "--sort-by=.lastTimestamp"])):
        r = say("kubectl", "-n", "benchmarking", *cmd, capture_output=True)
        if APPLY:
            with open(os.path.join(out_dir, f"{job}.{what}.txt"), "w") as fh:
                fh.write((r.stdout or "") + (r.stderr or ""))


def served(log: str) -> dict:
    """The runner's own last Aggregated row: a run that served nothing, failed more than 5% of its
    requests, or was refused for permissions isn't complete, whatever the Job's status says (the
    shakeout's first run completed with 59 of 59 requests refused)."""
    rows = re.findall(r"Aggregated\s+(\d+)\s+(\d+)\(", log)
    reqs, fails = (int(rows[-1][0]), int(rows[-1][1])) if rows else (0, 0)
    denied = log.count("PermissionDenied")
    ok = reqs > 0 and fails <= 0.05 * reqs and denied == 0
    return {"ok": ok, "requests": reqs, "failures": fails, "permission_denied_lines": denied}


def has_stats(d: str) -> bool:
    """Google's runner writes stats.jsonl and status.json; a run without them didn't complete."""
    names = {n for _, _, fs in os.walk(d) for n in fs}
    return {"stats.jsonl", "status.json"} <= names


AT = None   # an evaluation time (epoch s) for the SLO read; None is now


def prom(query: str):
    """One instant PromQL query against where Substrate's metrics actually land: it exports through
    OpenTelemetry, not Prometheus scrapes (the shakeout found the benchmark Prometheus held none).
    Locally that's the install's own Prometheus in otel-system; on GKE the installer's Managed
    OpenTelemetry sends them to Google Managed Prometheus, read through Cloud Monitoring's PromQL
    API (read-only)."""
    import urllib.parse
    q = urllib.parse.quote(query, safe="")
    if LOCAL:
        r = subprocess.run(["kubectl", "get", "--raw", "/api/v1/namespaces/otel-system/services/prometheus:9090"
                            f"/proxy/api/v1/query?query={q}"], capture_output=True, text=True)
        body, err = r.stdout, (r.stderr[-300:] if r.returncode else None)
    else:
        tok = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True).stdout.strip()
        url = f"https://monitoring.googleapis.com/v1/projects/{PROJECT}/location/global/prometheus/api/v1/query?query={q}"
        if AT:
            url += f"&time={AT}"
        r = subprocess.run(["curl", "-s", "-H", f"Authorization: Bearer {tok}", url], capture_output=True, text=True)
        body, err = r.stdout, (r.stderr[-300:] if r.returncode else None)
    if err:
        return None, err
    try:
        return json.loads(body).get("data", {}).get("result", []), None
    except ValueError:
        return None, body[-300:]


# Two dialects, by where the metrics land: the local install's Prometheus translates OTel names
# (underscores, a unit suffix, `le="1.0"`); Google Managed Prometheus keeps them as UTF-8 names with
# dots, written `{"name", "label"="v"}` as Substrate's own sample GMP rules write them, `le="1"`.
if LOCAL:
    H = lambda suffix, sel: f'ate_actor_lifecycle_operation_duration_seconds_{suffix}{{{sel}}}'   # noqa: E731
    RH = lambda sel: f'ate_actor_restore_duration_seconds_bucket{{{sel}}}'                       # noqa: E731
    L = {"op": "ate_actor_operation_name", "kind": "ate_snapshot_kind", "err": "error_type", "phase": "ate_snapshot_phase"}
    LE = {"0.5s (the post)": "0.5", "1s (GKE's About page)": "1.0"}
else:
    H = lambda suffix, sel: f'{{"ate.actor.lifecycle.operation.duration_{suffix}",{sel}}}'        # noqa: E731
    RH = lambda sel: f'{{"ate.actor.restore.duration_bucket",{sel}}}'                            # noqa: E731
    L = {"op": '"ate.actor.operation.name"', "kind": '"ate.snapshot.kind"', "err": '"error.type"',
         "phase": '"ate.snapshot.phase"'}
    LE = {"0.5s (the post)": "0.5", "1s (GKE's About page)": "1"}


def slo(window_s: int, out_dir: str):
    """Good over valid resumes by snapshot kind (`boot`: from scratch; `local`: the node's copy;
    `latest`: a durable snapshot from storage), at both published thresholds; failed resumes aren't
    good. Each fraction is one expression so both sides get the same extrapolation."""
    w = f"{window_s}s"
    op = f'{L["op"]}="resume"'
    rec = {"window_s": window_s, "window_end_epoch": AT or time.time(),
           "form": "good / valid resumes (Substrate SLO guide's SLI form)",
           "metric": "ate.actor.lifecycle.operation.duration (resume)",
           "source": "otel-system Prometheus (kind)" if LOCAL else "Google Managed Prometheus",
           "by_snapshot_kind": {}, "restore_histogram": {}, "errors": []}
    valid, err = prom(f'sum by ({L["kind"]}) (increase({H("count", op)}[{w}]))')
    if err or valid is None:
        rec["errors"].append(err or "no result")
    for row in valid or []:
        kind = row["metric"].get(L["kind"].strip('"'), "?")
        entry = {"valid": float(row["value"][1])}
        for label, le in LE.items():
            sel = f'{op},{L["kind"]}="{kind}"'
            frac, _ = prom(f'sum(increase({H("bucket", sel + "," + L["err"] + "=" + chr(34) + chr(34) + ",le=" + chr(34) + le + chr(34))}[{w}])) / '
                           f'sum(increase({H("count", sel)}[{w}]))')
            f = float(frac[0]["value"][1]) if frac else None
            entry[label] = {"fraction": None if f is None or f != f else round(f, 4)}
        rec["by_snapshot_kind"][kind] = entry
    for qn in ("0.5", "0.9", "0.99"):
        v, _ = prom(f'histogram_quantile({qn}, sum by (le) (increase({RH(L["phase"] + "=" + chr(34) + "total" + chr(34))}[{w}])))')
        rec["restore_histogram"][f"p{int(float(qn) * 100)}_s"] = float(v[0]["value"][1]) if v else None
    if not rec["by_snapshot_kind"]:
        rec["errors"].append("no resumes in the window; the SLI is not computed")
    if APPLY:
        os.makedirs(out_dir, exist_ok=True)
        name = f"slo-at-{int(AT)}-{window_s}s.json" if AT else f"slo-{time.strftime('%H%M%S')}.json"
        with open(os.path.join(out_dir, name), "w") as fh:
            json.dump(rec, fh, indent=1)
    print(json.dumps(rec, indent=1))


def down():
    st = load_state()
    for deploy, o in st.get("original_args", {}).items():     # put the owner and allowlist back
        set_args(deploy, o["idx"], o["args"])
    say("kubectl", "delete", "-f", os.path.join(SUB, "benchmarking", "monitoring.yaml"), "--ignore-not-found",
        "--wait=true", "--timeout=300s")
    say("kubectl", "delete", "namespace", "benchmarking", "--ignore-not-found", "--wait=true", "--timeout=300s")
    for kind, n in (("clusterrolebinding", "benchmark-runner-hardware-discovery"),
                    ("clusterrole", "benchmark-runner-hardware-discovery")):
        say("kubectl", "delete", kind, n, "--ignore-not-found")
    for n in ("automated-benchmarking-permissions", "benchmark-runner-atelet-logs"):
        say("kubectl", "-n", "ate-system", "delete", "rolebinding", n, "--ignore-not-found")
    say("kubectl", "-n", "ate-system", "delete", "role", "benchmark-runner-atelet-logs", "--ignore-not-found")
    deploy_workloads({}, delete=True)
    if "registry-read" in st.get("done", []):
        say("gcloud", "artifacts", "repositories", "remove-iam-policy-binding", "lab-images", "--project", PROJECT,
            "--location", REGION, "--role", "roles/artifactregistry.reader",
            "--member", f"serviceAccount:ate-gke-node@{PROJECT}.iam.gserviceaccount.com", "--format", "none")
    if "nodepool" in st.get("done", []):
        say("gcloud", "container", "node-pools", "delete", RUNNER_POOL, "--cluster", CLUSTER, "--zone", ZONE,
            "--project", PROJECT, "--quiet")
    if "bucket" in st.get("done", []):
        say("gcloud", "storage", "rm", "-r", f"gs://{st['bucket']}")
    for deploy, o in st.get("original_args", {}).items():     # verify the restore took
        _, now = container_args(deploy, o["container"]) if APPLY else (0, o["args"])
        if now != o["args"]:
            sys.exit(f"{deploy}: args not restored; expected {o['args']}")
    if APPLY and os.path.exists(STATE):
        os.remove(STATE)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else os.path.join(
        TOOLS, "runs", f"google-bench-{time.strftime('%Y%m%d')}")
    if cmd == "list":
        for n, t in TESTS.items():
            print(f"{n}: {t['source']} ({t['duration']}, {t['users']} users, deploy {t['deploy']})")
    elif cmd == "setup":
        setup()
    elif cmd == "build":
        build()
    elif cmd == "run":
        run(sys.argv[2], out)
    elif cmd == "slo":
        global AT
        AT = float(sys.argv[sys.argv.index("--at") + 1]) if "--at" in sys.argv else None
        slo(int(sys.argv[2]), out)
    elif cmd == "down":
        down()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
