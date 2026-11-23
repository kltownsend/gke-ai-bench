#!/usr/bin/env python3
"""Snapshot and restore on Agent Substrate, against Google's 2026-11-05 claims.

The post: Substrate "snapshots the agent's memory and files to storage", restores it "in under 500
milliseconds", and the agent "picks up right where it left off". This measures both halves on the
lab's own agent actor (the lab-agent template: the agy image, MEMORY fidelity, 2 CPU, 2 GiB), with
no model call, so it costs only cluster time.

One actor, set up the way an investigation leaves it: a workspace in /work, a file in the agent's
HOME, and the actor server's own process state (agent/actor_server.py `/state`). Then, per cycle:

- **explicit** (`--explicit N`): `kubectl ate suspend --mode=sync` (timed until the snapshot is in
  storage), then `kubectl ate resume` (timed to the CLI's return) and a 25 ms poll of `/healthz`
  through the router until the actor answers (timed to first answer).
- **wake on request** (`--wake N`): suspend the same way with the router tunnel kept up, then one
  request to `/state`. The router parks it and wakes the actor (Substrate's request parking, the
  post's "when the next request arrives"); its latency is the user-visible wake, the lab's analogue
  of Google's `WakeFirstTouch`.

After every cycle the probe checks what came back: the same server PID and boot value, a counter
that resumed rather than restarted, the boot-time child still alive, and /work and HOME hashing the
same as before the suspend. Baselines for the same path with nothing suspended (a `/state` round
trip and a `kubectl ate get actor`) are recorded so the CLI's and the tunnel's own overhead can be
read out of the numbers. Afterwards the atelet logs for the run's window are saved: the runtime's
own record of each checkpoint and restore.

Source harness/lab_env.sh, point kubectl at the Substrate cluster, run with LAB_PYTHON.

**From storage** (`--storage N`): by default atelet keeps a suspended actor's latest snapshot on
the node for 30 minutes (`DefaultDetachDelay`), then detaches it; a resume before that reads the
node's copy (`ate.snapshot.kind=local`), a resume after it reads object storage (`latest`). The
post's claim is about snapshots "to storage", so this mode creates N actors, seeds and suspends each,
waits past the detach delay (`--wait` seconds, default 2100), then wakes each with one request on a
kept connection and checks what came back. Google's defaults, unchanged.

usage: resume_probe.py [--explicit N] [--wake N] [--storage N [--wait S]] [--out DIR] [--label NAME]
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "agent"))


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def interleave(n_ex: int, n_wk: int) -> list[str]:
    """Alternate the modes; whichever has more cycles runs its extras at the end."""
    out = []
    for k in range(max(n_ex, n_wk)):
        if k < n_ex:
            out.append("explicit")
        if k < n_wk:
            out.append("wake")
    return out


def checkpoint(sb, i: int) -> dict:
    """Before a suspend: change the actor's state so only this cycle's snapshot can satisfy the
    checks (round 9, finding 6), then stop the counter at a barrier so the subject is quiescent."""
    nonce = uuid.uuid4().hex
    sb.post_state("mark", {"nonce": nonce})
    sb.run_in_actor(["sh", "-c", f'echo {nonce} > cycle-{i}.txt && mkdir -p "$HOME/.lab" && echo {nonce} > "$HOME/.lab/cycle"'])
    ping = sb.post_state("child-ping", {"n": i * 10})
    sb.post_state("quiet", {"on": True})
    return {"nonce": nonce, "ping": ping, "state": sb.state(), "manifest": sb.manifest()}


def verify(sb, pre: dict, i: int) -> dict:
    """After the actor answers again: what came back, each check on its own."""
    after = sb.state()
    man = sb.manifest()
    ping = sb.post_state("child-ping", {"n": i * 10 + 1})
    sb.post_state("quiet", {"on": False})
    time.sleep(0.3)
    moved = sb.state()["ticks"]
    b = pre["state"]
    same_proc = b["pid"] == after["pid"] and b["boot"] == after["boot"]
    return {"same_process": same_proc,
            "memory_mark": after.get("mark") == pre["nonce"],
            "counter_held": same_proc and after["ticks"] == b["ticks"],
            "counter_runs_after": moved > after["ticks"],
            "child_state": ping["reply"] == i * 10 + 2 and ping["seq"] == pre["ping"]["seq"] + 1,
            "work_intact": man["work"] == pre["manifest"]["work"],
            "home_intact": man["home"] == pre["manifest"]["home"],
            "restarted": not same_proc,
            "rss_kb_before": b.get("rss_kb"), "rss_kb_after": after.get("rss_kb"),
            "monotonic_delta_s": round(after["monotonic_s"] - b["monotonic_s"], 3),
            "wall_delta_s": round(after["wall"] - b["wall"], 3)}


CHECKS = ("same_process", "memory_mark", "counter_held", "counter_runs_after", "child_state",
          "work_intact", "home_intact")


def timed(fn, *a, **k):
    t0 = time.perf_counter()
    r = fn(*a, **k)
    return r, round(time.perf_counter() - t0, 4)


def poll_healthy(sb, budget_s=120, every=0.025, conn=None):
    """Seconds until the actor's /healthz answers through the tunnel, polling every 25 ms, on one
    kept connection: a new tunnel connection per poll costs up to ~390 ms once its burst is spent
    (local shakeout, 2026-11-22), which would land in the timing."""
    t0 = time.perf_counter()
    conn = conn or sb.connection()
    while time.perf_counter() - t0 < budget_s:
        if sb.proxy is not None and sb.proxy.poll() is not None:
            sb.start_proxy()
            conn = sb.connection()
        try:
            sb.timed_get(conn, "/healthz")
            return round(time.perf_counter() - t0, 4)
        except Exception:                        # noqa: BLE001  a broken connection is reopened
            conn.close()
            conn = sb.connection()
        time.sleep(every)
    return None


def explicit_cycle(sb, i: int) -> dict:
    """The lab's resume-to-first-response: CLI, API server, restore, tunnel and router together.
    Not the post's restore latency, and no baseline is subtracted from it (round 9, finding 1)."""
    pre = checkpoint(sb, i)
    _, suspend_s = timed(sb.ate, "suspend", "actor", sb.actor, "-a", sb.atespace, "--mode=sync", timeout=900)
    conn = sb.connection()
    _, resume_cli_s = timed(sb.ate, "resume", "actor", sb.actor, "-a", sb.atespace, timeout=600)
    serving_s = poll_healthy(sb, conn=conn)
    return {"mode": "explicit", "suspend_sync_s": suspend_s, "resume_cli_s": resume_cli_s,
            "cli_return_to_serving_s": serving_s,
            "lab_resume_to_first_response_s": None if serving_s is None else round(resume_cli_s + serving_s, 4),
            **verify(sb, pre, i)}


def wake_cycle(sb, i: int) -> dict:
    """Suspend with the tunnel up, then one request the router parks and wakes on: the lab's
    analogue of Google's WakeFirstTouch, through the lab's client tunnel."""
    pre = checkpoint(sb, i)
    conn = sb.connection()
    sb.timed_get(conn, "/healthz")               # opened and warm before the suspend
    _, suspend_s = timed(sb.ate, "suspend", "actor", sb.actor, "-a", sb.atespace, "--mode=sync", timeout=900)
    proxy_alive = sb.proxy is not None and sb.proxy.poll() is None
    err = None
    try:
        wake_s = sb.timed_get(conn, "/healthz")  # parked by the router, woken on this connection
    except Exception as e:                       # noqa: BLE001  (recorded, not hidden)
        wake_s, err = None, repr(e)[:300]
        sb.start_proxy()
        poll_healthy(sb)
    conn.close()
    return {"mode": "wake", "suspend_sync_s": suspend_s, "proxy_alive_after_suspend": proxy_alive,
            "wake_first_touch_s": wake_s, "wake_connection": "kept (opened before the suspend)",
            **({"wake_error": err} if err else {}), **verify(sb, pre, i)}


def seed_actor(sb):
    """Leave the actor the way an investigation would: a workspace and a file in HOME."""
    ws = tempfile.mkdtemp(prefix="lab-resume-")
    with open(os.path.join(ws, "notes.txt"), "w") as fh:
        fh.write(f"investigation state {time.time()}\n")
    os.makedirs(os.path.join(ws, "pkg"))
    with open(os.path.join(ws, "pkg", "blob.bin"), "wb") as fh:
        fh.write(os.urandom(1 << 20))
    sb.run(ws, ["sh", "-c", 'mkdir -p "$HOME/.lab" && head -c 65536 /dev/urandom > "$HOME/.lab/agent-state"'])


def baselines(sb, n=10) -> dict:
    """Context for the timings, never subtracted from them: a request on a kept connection (what
    the timings use) and on a fresh one each time (what the tunnel charges for a new stream)."""
    conn = sb.connection()
    kept = [sb.timed_get(conn, "/healthz") for _ in range(n)]
    conn.close()
    fresh = []
    for _ in range(n):
        c = sb.connection()
        fresh.append(sb.timed_get(c, "/healthz"))
        c.close()
    return {"healthz_kept_connection_s": stats(kept), "healthz_fresh_connection_s": stats(fresh)}


def stats(xs):
    """Raw samples, median and range. Percentiles are empirical and labeled so: with 20 samples
    p95 is the second-largest and p99 the largest (round 9, finding 10)."""
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0, "samples": []}
    return {"n": len(xs), "samples": xs, "median": statistics.median(xs), "min": min(xs), "max": max(xs),
            "empirical_p95": pct(xs, .95)}


def atelet_logs(since_s: int, out_dir: str, label: str) -> list[str]:
    pods = subprocess.run(["kubectl", "-n", "ate-system", "get", "pods", "-o", "name"],
                          capture_output=True, text=True).stdout.split()
    saved = []
    for p in pods:
        if "atelet" not in p:
            continue
        r = subprocess.run(["kubectl", "-n", "ate-system", "logs", p, "--all-containers", f"--since={since_s}s"],
                           capture_output=True, text=True)
        path = os.path.join(out_dir, f"atelet-{label}-{p.split('/')[-1]}.log")
        with open(path, "w") as fh:
            fh.write(r.stdout)
        saved.append(path)
    return saved


def summarize(cycles: list[dict]) -> dict:
    ex = [c for c in cycles if c["mode"] == "explicit"]
    wk = [c for c in cycles if c["mode"] == "wake"]
    st = [c for c in cycles if c["mode"] == "storage"]
    return {"storage": {"wake_first_touch_s": stats([c["wake_first_touch_s"] for c in st]),
                        "idle_s": stats([c["idle_s"] for c in st]), "errors": sum(1 for c in st if c.get("wake_error"))},
            "explicit": {"suspend_sync_s": stats([c["suspend_sync_s"] for c in ex]),
                         "lab_resume_to_first_response_s": stats([c["lab_resume_to_first_response_s"] for c in ex])},
            "wake": {"suspend_sync_s": stats([c["suspend_sync_s"] for c in wk]),
                     "wake_first_touch_s": stats([c["wake_first_touch_s"] for c in wk]),
                     "errors": sum(1 for c in wk if c.get("wake_error"))},
            "continuity": {k: f"{sum(1 for c in cycles if c.get(k))}/{len(cycles)}" for k in CHECKS},
            "under_500ms_count": {
                "lab_resume_to_first_response": f"{sum(1 for c in ex if (c['lab_resume_to_first_response_s'] or 9) < .5)}/{len(ex)}",
                "wake_first_touch": f"{sum(1 for c in wk if (c.get('wake_first_touch_s') or 9) < .5)}/{len(wk)}",
                "storage_wake_first_touch": f"{sum(1 for c in st if (c.get('wake_first_touch_s') or 9) < .5)}/{len(st)}"},
            "note": "No pass rule is applied here; the SLO reading comes from the runtime's own histogram "
                    "(google_bench.py slo), by snapshot kind."}


def wait_listening(port: int, budget_s: float = 60) -> float | None:
    """Seconds until the tunnel's local port accepts a TCP connection; nothing is sent through it,
    so the actor isn't woken (the first storage run's fixed 1 s sleep was too short)."""
    import socket
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < budget_s:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return round(time.perf_counter() - t0, 3)
        except OSError:
            time.sleep(0.1)
    return None


def storage_run(n: int, wait_s: int) -> list[dict]:
    """N actors, each suspended past the detach delay, then woken once from storage."""
    from substrate_venue import SubstrateVenue
    venues, pres, out = [], [], []
    try:
        for i in range(n):
            sb = SubstrateVenue()
            sb.__enter__()
            venues.append(sb)
            seed_actor(sb)
            pres.append(checkpoint(sb, i))
            _, s_s = timed(sb.ate, "suspend", "actor", sb.actor, "-a", sb.atespace, "--mode=sync", timeout=900)
            pres[-1]["suspend_sync_s"] = s_s
            sb.stop_proxy()
            print(json.dumps({"seeded_and_suspended": i, "actor": sb.actor, "suspend_sync_s": s_s}), flush=True)
        t_suspended = time.time()
        print(json.dumps({"waiting_s": wait_s, "for": "atelet's detach delay (30 min by default)"}), flush=True)
        time.sleep(wait_s)
        for i, sb in enumerate(venues):
            sb.start_proxy()
            listening = wait_listening(sb.port)       # the tunnel's local port up, nothing sent through
            conn = sb.connection()
            err = None
            try:
                wake_s = sb.timed_get(conn, "/healthz")
            except Exception as e:                   # noqa: BLE001  (recorded)
                wake_s, err = None, repr(e)[:300]
                poll_healthy(sb)
            conn.close()
            checks = verify(sb, pres[i], i)
            # Back to sleep, so the next wake has a worker (each actor takes a whole worker; the
            # first run's third wake found "no worker has room").
            sb.ate("suspend", "actor", sb.actor, "-a", sb.atespace, "--mode=sync", timeout=900)
            sb.stop_proxy()
            out.append({"i": i, "mode": "storage", "actor": sb.actor, "idle_s": round(time.time() - t_suspended),
                        "tunnel_listening_s": listening,
                        "suspend_sync_s": pres[i]["suspend_sync_s"], "wake_first_touch_s": wake_s,
                        **({"wake_error": err} if err else {}), **checks})
            print(json.dumps(out[-1]), flush=True)
    finally:
        for sb in venues:
            sb.__exit__(None, None, None)
    return out


def main():
    from substrate_venue import SubstrateVenue
    n_ex, n_wk = int(arg("--explicit", 10)), int(arg("--wake", 10))
    n_st, wait_s = int(arg("--storage", 0)), int(arg("--wait", 2100))
    label = arg("--label", "resume")
    out_dir = arg("--out", os.path.join(TOOLS, "runs", f"resume-{time.strftime('%Y%m%d')}"))
    os.makedirs(out_dir, exist_ok=True)
    t_start = time.time()
    cycles, base = [], {}
    if n_st:
        cycles = storage_run(n_st, wait_s)
        n_ex = n_wk = 0
    with SubstrateVenue() as sb:
        seed_actor(sb)
        base = baselines(sb)
        # Interleaved, so neither mode owns the warm or cold end of the run (round 9, finding 10).
        order = interleave(n_ex, n_wk)
        for i, mode in enumerate(order):
            cycles.append({"i": i, **(explicit_cycle if mode == "explicit" else wake_cycle)(sb, i)})
            print(json.dumps(cycles[-1]), flush=True)
        events = sb.events
    logs = atelet_logs(int(time.time() - t_start) + 120, out_dir, label)
    rec = {"probe": "resume", "label": label, "image": os.environ.get("LAB_ACTOR_IMAGE", ""),
           "template": "lab-agent (MEMORY fidelity, 2 CPU, 2 GiB)", "started": t_start,
           "baselines": base, "summary": summarize(cycles), "cycles": cycles, "venue_events": events,
           "atelet_logs": [os.path.basename(p) for p in logs]}
    path = os.path.join(out_dir, f"resume-{label}.json")
    with open(path, "w") as fh:
        json.dump(rec, fh, indent=1, default=str)
    print(json.dumps(rec["summary"], indent=1))
    print(path)


if __name__ == "__main__":
    main()
