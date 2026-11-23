#!/usr/bin/env python3
"""The pilot: every frozen case a few times on one venue (the local sandbox), arm F, to estimate
between-run variance and cost before the power calculation sets the repeats (spec, Repeats).

Each run is one harness/canary_agy.py process (its own sandbox, sidecar and token minter), retry
budget 3. Order is shuffled from a recorded seed. Results land one JSON per
run in OUT; a rerun skips runs already there, so a crash or a stop loses only runs in flight.

Spend cap: before launching a run, measured spend so far plus a ceiling for every run in flight
must stay under --cap. Measured spend is priced from each run's own token counts at the verified
rates (spec, Cost and gates), the high reading (thinking counted on top of output). Billing
reconciliation against Google's report is separate.

Source harness/lab_env.sh first; run with LAB_PYTHON.

With --gke PROFILE each run is harness/canary_gke.py on that rung instead (the Clinic repairs
ran with --gke substrate --label-prefix repair --dangerous). --dangerous sets
LAB_AGY_DANGEROUS=1, so agy runs with --dangerously-skip-permissions (recorded on every call).

usage: pilot.py --out DIR [--repeats 3] [--parallel 3] [--cap 47] [--seed 20261120] [--cases ID,...]
                [--gke PROFILE] [--label-prefix pilot] [--dangerous]
"""
import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANARY = os.path.join(TOOLS, "harness", "canary_agy.py")
CANARY_GKE = os.path.join(TOOLS, "harness", "canary_gke.py")
RATE_IN, RATE_CACHED, RATE_OUT = 1.50, 0.075, 7.50       # $/M tokens, Gemini 4 Hydrogen, global
RUN_CEILING = 2.00                                        # $ reserved for each run in flight
# Infrastructure failures (no result) tolerated before the pilot stops launching. 1 was the rule; the
# night of 2026-11-22 allows 3, because a call silent for 5 minutes is cut by the router's CONNECT idle
# timeout and one cut call shouldn't end an unattended batch.
MAX_INFRA = int(os.environ.get("LAB_MAX_INFRA", "1"))


def cost(totals):
    t = totals or {}
    return (t.get("input_tokens", 0) * RATE_IN + t.get("cache_read_tokens", 0) * RATE_CACHED
            + (t.get("output_tokens", 0) + t.get("thinking_tokens", 0)) * RATE_OUT) / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--cap", type=float, default=47.0)
    ap.add_argument("--seed", type=int, default=20261120)
    ap.add_argument("--cases")
    ap.add_argument("--gke")
    ap.add_argument("--label-prefix", default="pilot")
    ap.add_argument("--dangerous", action="store_true")
    a = ap.parse_args()
    env = dict(os.environ, **({"LAB_AGY_DANGEROUS": "1"} if a.dangerous else {}))
    cases = a.cases.split(",") if a.cases else sorted(
        c for c in os.listdir(os.path.join(TOOLS, "cases")) if os.path.exists(os.path.join(TOOLS, "cases", c, "key.json")))
    jobs = [(c, r) for c in cases for r in range(1, a.repeats + 1)]
    random.Random(a.seed).shuffle(jobs)
    os.makedirs(os.path.join(a.out, "logs"), exist_ok=True)
    with open(os.path.join(a.out, "plan.json"), "w") as fh:
        json.dump({"seed": a.seed, "cap": a.cap, "parallel": a.parallel, "jobs": jobs, "gke": a.gke,
                   "dangerous": a.dangerous}, fh, indent=1)

    def result_path(c, r):
        return os.path.join(a.out, f"canary-{c}-{a.label_prefix}-r{r}.json")

    lock = threading.Lock()
    state = {"spent": 0.0, "inflight": 0, "stopped": False}
    for c, r in jobs:
        if os.path.exists(result_path(c, r)):
            with open(result_path(c, r)) as fh:
                state["spent"] += cost(json.load(fh).get("totals"))
    print(f"{time.strftime('%H:%M:%S')} resume: spent ${state['spent']:.2f} on runs already done", flush=True)
    todo = [j for j in jobs if not os.path.exists(result_path(*j))]

    def run(c, r):
        log = os.path.join(a.out, "logs", f"{c}-r{r}.log")
        t0 = time.time()
        with open(log, "w") as fh:
            cmd = [sys.executable, CANARY_GKE if a.gke else CANARY, c, *(["--profile", a.gke] if a.gke else []),
                   "--budget", "3", "--out", a.out, "--label", f"{a.label_prefix}-r{r}"]
            p = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env)
        got = None
        if os.path.exists(result_path(c, r)):
            with open(result_path(c, r)) as fh:
                got = json.load(fh)
        with lock:
            state["inflight"] -= 1
            spent = cost(got.get("totals")) if got else 0.0
            state["spent"] += spent
            print(f"{time.strftime('%H:%M:%S')} {c} r{r} exit {p.returncode} {round(time.time() - t0)}s "
                  f"stage {got and got.get('final_stage')} outcomes {got and got.get('outcomes')} "
                  f"${spent:.3f} total ${state['spent']:.2f}", flush=True)
            if p.returncode != 0 and not got:
                state["infra"] = state.get("infra", 0) + 1
                print(f"  infrastructure failure {state['infra']}: {c} r{r} produced no result; see {log}", flush=True)
                if state["infra"] >= MAX_INFRA:
                    state["stopped"] = True    # too many: stop launching, look first

    threads = []
    for c, r in todo:
        while True:
            with lock:
                if state["stopped"]:
                    break
                room = state["spent"] + (state["inflight"] + 1) * RUN_CEILING <= a.cap
                if state["inflight"] < a.parallel and room:
                    state["inflight"] += 1
                    break
                if not room and state["inflight"] == 0:
                    state["stopped"] = True
                    print(f"  cap reached: ${state['spent']:.2f} spent, cap ${a.cap}", flush=True)
                    break
            time.sleep(5)
        if state["stopped"]:
            break
        t = threading.Thread(target=run, args=(c, r))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    left = [j for j in jobs if not os.path.exists(result_path(*j))]
    print(f"{time.strftime('%H:%M:%S')} done: ${state['spent']:.2f} spent, {len(jobs) - len(left)} of {len(jobs)} runs, "
          f"{len(left)} left", flush=True)


if __name__ == "__main__":
    main()
