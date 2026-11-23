#!/usr/bin/env python3
"""Per-request latency of the subject, by op, to set the timeouts from data (build review 4).

Runs the judge's real probe sets against the clean trees in isolated mode (a fresh child per
request, as every workspace under test gets) with the supervisor's timing on. Each request's time
covers starting its child, answering, killing the child's uid and wiping scratch. No model calls.

usage: measure_latency.py [--repeat N]    prints JSON: per op n, p50, p95, p99, max in ms, and
                                          each gate's wall time
"""
import json
import os
import statistics
import sys
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "validator"))
import regression as R  # noqa: E402
import subject as S  # noqa: E402
import validate as V  # noqa: E402

rep = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 1
B = lambda n: os.path.join(TOOLS, "baselines", n)  # noqa: E731
sets = [("t03 probes", B("t03"), V.case_module(os.path.join(TOOLS, "cases", "T03")).probes("visible")
         + V.case_module(os.path.join(TOOLS, "cases", "T03")).probes("hidden")),
        ("t03fix probes", B("t03fix"), V.case_module(os.path.join(TOOLS, "cases", "T03")).probes("hidden")),
        ("s01 probes", B("s01"), V.case_module(os.path.join(TOOLS, "cases", "S01")).probes("visible")
         + V.case_module(os.path.join(TOOLS, "cases", "S01")).probes("hidden")),
        ("regression", B("head"), R.probes(None)),
        ("selftest", B("head"), [{"op": "selftest"}])]
by_op, gates = {}, []
for _ in range(rep):
    for name, tree, reqs in sets:
        t = time.time()
        out = S.ask(tree, reqs, timing=True, timeout=1800)
        gates.append({"set": name, "n": len(reqs), "wall_s": round(time.time() - t, 1),
                      "failed": sum(1 for o in out if not o.get("ok"))})
        for r, o in zip(reqs, out):
            if "_ms" in o:
                by_op.setdefault(r["op"], []).append(o["_ms"])


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


print(json.dumps({"ops": {op: {"n": len(xs), "p50": pct(xs, .5), "p95": pct(xs, .95), "p99": pct(xs, .99),
                               "max": max(xs), "mean": round(statistics.mean(xs), 1)}
                          for op, xs in by_op.items()},
                  "gates": gates}, indent=1))
