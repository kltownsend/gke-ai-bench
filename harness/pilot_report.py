#!/usr/bin/env python3
"""Pilot analysis from the preserved records: per-case solves across repeats, why each wrong
attempt failed (recomputed from the hypothesis against the key), cost, and the counterfactual
solve count if a diagnosis that names an accepted cause plus extra symbols were allowed.

usage: pilot_report.py RUNDIR      (reads RUNDIR/canary-*.json and RUNDIR/records/*.json)
"""
import collections
import glob
import json
import os
import sys

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
R = sys.argv[1]


def cost(t):
    return (t.get("input_tokens", 0) * 1.5 + t.get("cache_read_tokens", 0) * 0.075
            + (t.get("output_tokens", 0) + t.get("thinking_tokens", 0)) * 7.5) / 1e6


solved, superset, spend = collections.Counter(), collections.Counter(), collections.Counter()
why, extras = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
for f in sorted(glob.glob(os.path.join(R, "canary-*.json"))):
    d = json.load(open(f))
    c = d["case"]
    key = json.load(open(os.path.join(TOOLS, "cases", c, "key.json")))
    acc = set(key.get("accepted_symbols", []))
    ev = json.load(open(os.path.join(R, "records", os.path.basename(f)[len("canary-"):])))["events"]
    ok = d["final_stage"] != "escalated"
    solved[c] += ok
    spend[c] += cost(d["totals"])
    would, h = ok, None
    for e in ev:
        if e["stage"] == "hypothesis" and e["event"] == "result":
            h = e.get("hypothesis") if e.get("ok") else None
            if not e.get("ok"):
                why[c]["malformed hypothesis"] += 1
        if e["stage"] == "work" and e["event"] == "blocked":
            why[c]["work blocked: " + e.get("why", "")] += 1
        if e["stage"] == "validate" and e["event"] == "result" and e["outcome"] == "wrong":
            n, fb, r = set(h.get("symbols", [])), e.get("feedback", {}), []
            if h["kind"] != key["type"]:
                r.append(f"kind {h['kind']}")
            if key["type"] == "not_driver":
                if h["kind"] == "not_driver" and h.get("attribution") != key["attribution"]:
                    r.append("attribution")
            elif not n & acc:
                r.append("no accepted cause")
            elif n - acc:
                r.append("extra causes")
                for x in n - acc:
                    extras[c][x.replace("clinicbench.", "")] += 1
            if h["kind"] == "fix":
                r += [k for k in ("in_scope", "visible_fixed", "hidden_fixed", "no_regression") if fb.get(k) is False]
            why[c][" + ".join(r) or "?"] += 1
            if r == ["extra causes"]:
                would = True
    superset[c] += would
print("case  solved  superset  spend  wrong attempts by reason; extra symbols named")
for c in sorted(solved):
    print(f"{c}  {solved[c]}/3  {superset[c]}/3  ${spend[c]:.2f}  {dict(why[c])}  {dict(extras[c].most_common(3))}")
print(f"solved {sum(solved.values())} of {sum(1 for _ in glob.glob(os.path.join(R, 'canary-*.json')))}, "
      f"superset rule {sum(superset.values())}, spend ${sum(spend.values()):.2f}")
