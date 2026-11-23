#!/usr/bin/env python3
"""Build seeded-defect cases: one planted bug per case, on the sanitized head baseline.

A seed is accepted only if all four hold:
1. its edit applies exactly once (the old text occurs once in the file);
2. its probes fail on the seeded tree, visible and hidden, judged outside the container;
3. its probes pass on the clean head tree, visible and hidden;
4. the leak scan of the seeded tree stays clean.

Seeds are the labeled easy tier: one-line edits near the function the ticket
points at. The seeded tree is refrozen like any baseline. Regression for a seeded fix compares
against the clean head, so an honest fix must restore head's behavior exactly.

usage: build_seeded.py [SEED_ID ...]   (default: every seed in ../seeds/)
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil


def strip_bytecode(root):
    for d, dirs, files in os.walk(root):
        for x in [x for x in dirs if x == "__pycache__"]:
            shutil.rmtree(os.path.join(d, x))
        dirs[:] = [x for x in dirs if x != "__pycache__"]
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import sanitize  # noqa: E402
import subject as S  # noqa: E402
import validate as V  # noqa: E402


def module(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def judged(mod, tree, base_tree, which):
    reqs = mod.probes(which)
    return mod.judge(which, S.ask(tree, reqs), S.ask(base_tree, reqs))


def build(seed_path):
    seed = V.load(seed_path)
    sid = seed["id"]
    if not (isinstance(sid, str) and re.fullmatch(r"S\d{2}", sid)):     # one plain path segment
        raise SystemExit(f"bad seed id {sid!r} in {seed_path}")
    head = os.path.join(TOOLS, "baselines", "head")
    dst = os.path.join(TOOLS, "baselines", sid.lower())
    case_dir = os.path.join(TOOLS, "cases", sid)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(head, dst)
    path = os.path.join(dst, seed["file"])
    with open(path) as fh:
        src = fh.read()
    n = src.count(seed["old"])
    if n != 1:
        return {"id": sid, "accepted": False, "why": f"edit matches {n} times, needs exactly 1"}
    with open(path, "w") as fh:
        fh.write(src.replace(seed["old"], seed["new"]))
    man_path = os.path.join(dst, "docs", "phase3-freeze.json")
    man = V.load(man_path)
    if seed["file"] in man["files"]:
        man["files"][seed["file"]] = V.sha(path)
        with open(man_path, "w") as fh:
            json.dump(man, fh, indent=1)

    strip_bytecode(dst)
    probe_src = os.path.join(TOOLS, "seeds", seed["check"])
    mod = module(probe_src)
    r = {"id": sid, "type": seed["type"]}
    r["seeded"] = {w: judged(mod, dst, dst, w) for w in ("visible", "hidden")}
    r["clean"] = {w: judged(mod, head, dst, w) for w in ("visible", "hidden")}
    leaks = [h for h in sanitize.scan(dst, [seed["ticket"]]) if h[0] not in sanitize.CODE_OK]
    fails = [w for w in ("visible", "hidden") if not r["seeded"][w]["failures"]]
    passes = [w for w in ("visible", "hidden") if r["clean"][w]["failures"] != 0]
    r["accepted"] = not fails and not passes and not leaks
    r["why"] = "; ".join(filter(None, [
        f"no failure on seeded {fails}" if fails else "",
        f"fails on clean {passes}" if passes else "",
        f"leaks {leaks}" if leaks else ""])) or "ok"
    if os.path.exists(case_dir):
        shutil.rmtree(case_dir)
    if r["accepted"]:
        os.makedirs(case_dir)
        with open(os.path.join(case_dir, "case.json"), "w") as fh:
            json.dump({"id": sid, "baseline": sid.lower(), "seeded": True, "ticket": seed["ticket"]}, fh, indent=1)
        key = {"type": seed["type"], "tier": "easy", "accepted_files": [seed["file"]],
               "accepted_symbols": seed["symbols"], "regression_reference": "head",
               "seed": {"old": seed["old"], "new": seed["new"]}}
        with open(os.path.join(case_dir, "key.json"), "w") as fh:
            json.dump(key, fh, indent=1)
        shutil.copy2(probe_src, os.path.join(case_dir, "probe.py"))
    return r


if __name__ == "__main__":
    seeds_dir = os.path.join(TOOLS, "seeds")
    ids = sys.argv[1:] or sorted(f[:-5] for f in os.listdir(seeds_dir) if f.endswith(".json"))
    results = [build(os.path.join(seeds_dir, f"{i}.json")) for i in ids]
    print(json.dumps(results, indent=1))
    sys.exit(0 if all(r["accepted"] for r in results) else 1)
