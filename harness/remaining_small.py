#!/usr/bin/env python3
"""Items 4, 5 and 6 of the remaining tests, run in order. No model calls.

  reuse      Item 4, warm-pool reuse: create an actor and time it to healthy, delete it, create the
             next at once and time it again, five rounds; record which worker each landed on, so a
             reused worker is visible. Google's post says workers are reusable sandboxes from a warm pool.
  longcall   Item 5, the 7-minute disconnect: one request held open through the lab's path (kubectl ate
             proxy, the router) for SECONDS (a sleep inside the actor, not a model call), the way an agy
             call holds it. Repro of R13 run 2, which lost its connection at 422 s with the router at 30m.
  pids       Item 6a, team two's process limit: the actor template has no process-count field
             (Substrate exposes none for gVisor), so this measures what a team-two actor allows:
             start sleeping child processes up to a ceiling (2,000) and report how many started.
             Every child is killed after; the actor is deleted.
  denyall    Item 6b, keeping the agent's test runs off the model API: a team-two actor under an egress
             policy with no rules (the golden-path pattern: tests in their own actor, no network) runs
             the model-API probe and the escape suite's network checks.

usage: remaining_small.py reuse|longcall [SECONDS]|pids|denyall
"""
import json
import os
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "agent"))
sys.path.insert(0, os.path.join(TOOLS, "harness"))
OUT = os.path.join(TOOLS, "runs", "remaining-20261122")


def save(name, rec):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, f"{name}-{time.strftime('%H%M%S')}.json")
    json.dump(rec, open(p, "w"), indent=1)
    print(json.dumps(rec, indent=1))


def worker_of(sb):
    rows = subprocess.run(["kubectl", "ate", "get", "actors", "-a", sb.atespace], capture_output=True,
                          text=True).stdout.splitlines()
    return next((r.split()[4] for r in rows if sb.actor in r), None)


def reuse():
    from substrate_venue import SubstrateVenue
    rounds = []
    for i in range(5):
        sb = SubstrateVenue(); sb.__enter__()
        t0 = time.perf_counter(); sb.ensure_actor(); ready = round(time.perf_counter() - t0, 2)
        w = worker_of(sb)
        t1 = time.perf_counter(); sb.__exit__(None, None, None); gone = round(time.perf_counter() - t1, 2)
        rounds.append({"round": i, "create_to_healthy_s": ready, "worker": w, "delete_s": gone})
        print(json.dumps(rounds[-1]), flush=True)
    save("reuse", {"probe": "warm_pool_reuse", "rounds": rounds,
                   "workers_reused": len(rounds) - len({r["worker"] for r in rounds})})


def longcall(seconds):
    from substrate_venue import SubstrateVenue
    with SubstrateVenue() as sb:
        sb.ensure_actor()
        t0 = time.time(); err = None
        try:
            out = sb.run_in_actor(["sh", "-c", f"sleep {seconds}; echo done"], timeout=seconds + 120)
        except Exception as e:  # noqa: BLE001
            out, err = None, repr(e)[:300]
        save("longcall", {"probe": "long_held_request", "held_s": seconds, "returned_after_s": round(time.time() - t0),
                          "output": (out or "").strip()[-40:], "error": err})


PIDS = r"""
import os, signal, subprocess, sys, time
kids, err = [], None
for i in range(int(sys.argv[1])):
    try: kids.append(subprocess.Popen(["sleep", "300"]))
    except Exception as e:
        err = type(e).__name__ + ": " + str(e)[:120]; break
n = len(kids)
for k in kids:
    try: k.kill()
    except Exception: pass
for k in kids:
    try: k.wait(timeout=5)
    except Exception: pass
import json; print(json.dumps({"started": n, "stopped_by": err}))
"""


def pids():
    import lcb_team as L
    with L.team_venue() as sb:
        sb.ensure_actor()
        r = json.loads(sb.run_in_actor(["python3", "-I", "-c", PIDS, "2000"], timeout=600) or "{}")
        save("pids", {"probe": "team_two_process_ceiling", "ceiling_tried": 2000, **r,
                      "template_field": "none: Substrate's actor template exposes no process-count limit for gVisor"})


def denyall():
    import lcb_team as L
    from substrate_venue import SubstrateVenue
    pol = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    pol.write("rules: []\n"); pol.close()
    with SubstrateVenue(atespace=L.ATESPACE, template=L.TEMPLATE, policy=pol.name) as sb:
        sb.ensure_actor()
        probe = r"""
import json, socket, urllib.request
def get(u):
    try:
        with urllib.request.urlopen(u, timeout=8) as r: return {"status": r.status}
    except urllib.error.HTTPError as e: return {"status": e.code}
    except Exception as e: return {"error": type(e).__name__}
def tcp(h, p):
    try: socket.create_connection((h, p), timeout=5).close(); return "connected"
    except Exception as e: return "refused: " + type(e).__name__
print(json.dumps({"model_api": get("https://aiplatform.googleapis.com/"), "pypi": get("https://pypi.org/simple/pip/"),
                  "example": get("https://example.com/"), "tcp_1_1_1_1_443": tcp("1.1.1.1", 443)}))
"""
        r = json.loads(sb.run_in_actor(["python3", "-I", "-c", probe]) or "{}")
        save("denyall", {"probe": "test_actor_no_egress", "policy": "rules: []", "results": r})
    os.unlink(pol.name)


if __name__ == "__main__":
    cmd = sys.argv[1]
    {"reuse": reuse, "pids": pids, "denyall": denyall}.get(cmd, lambda: longcall(int(sys.argv[2])))()
