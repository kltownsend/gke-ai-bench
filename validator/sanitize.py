#!/usr/bin/env python3
"""Sanitize a Clinic tree into the agent's starting workspace.

A mined case is only a diagnosis test if nothing in the workspace names its cause. The baseline is
already a `git archive` (no history). This removes the prose record and keeps the rules:

- docs/: only the contract, the contract-change log, the freeze manifest and the contract hash
  stay. Run reports, transcripts, reviews, proposals, evidence and the declared-behavior sources go.
- explorer/ and .claude/ go (site data and local config).
- tests that pin known defects go (found by the leak scan, below).
- module docstrings that explain a known defect are replaced with a neutral one-liner, for files
  listed in STRIP_DOCSTRINGS.
- the freeze manifest is rewritten for any frozen file the sanitizer changed, so the tree is
  self-consistent and `live.py`'s freeze gate still means something.

Then a leak scan runs over what's left. Hits are reported; the build fails on any hit in a file
the sanitizer hasn't already handled, unless it's whitelisted as code the agent should read.

usage: sanitize.py TREE [--case-dir CASES]   (edits TREE in place)
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import sys

KEEP_DOCS = {"contract.md", "contract-changes.md", "phase3-freeze.json", "contract.sha256"}
DROP_DIRS = ["explorer", ".claude"]
# Tools an engineer uses to reproduce and read runs stay; scripts written about specific defects,
# and cloud or vendor runners, go.
KEEP_SCRIPTS = {"__init__.py", "freeze.py", "port_dcitl.py", "shakedown.py", "show_run.py",
                "summarize.py", "transcripts.py"}
STRIP_DOCSTRINGS = {
    "clinicbench/adapters/dcitl_f2.py": "Arm F2: the arm F driver with a different consent rule.",
}
# Prose outside docs that describes a consent rule's behavior closely enough to name T16's cause
# (found building T16, 2026-11-20): replaced in place, the file kept.
REPLACE = {
    "clinicbench/explorer_data.py": [("with a consent rule that takes a clear yes at any length",
                                      "with a different consent rule")],
}
# Prose that names a known cause or a ruling on one. Code identifiers are not here: finding the
# function is the agent's job.
LEAK = [r"x0195", r"x0678", r"x0706", r"not fixed", r"by ruling", r"left by ruling", r"known defect",
        r"recorded[_ ]behavior", r"reading-errors", r"no-rephrase", r"refusal-repeats",
        r"turned-down", r"parser pivot", r"four words or fewer", r"four-word"]
# Files where a hit is the code itself, not a description of a defect.
CODE_OK = {"clinicbench/adapters/lab23_dcitl.py.orig", "clinicbench/adapters/dcitl_port.py"}


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def strip_docstring(path, text):
    with open(path) as fh:
        src = fh.read()
    tree = ast.parse(src)
    doc = tree.body[0] if tree.body and isinstance(tree.body[0], ast.Expr) \
        and isinstance(getattr(tree.body[0], "value", None), ast.Constant) else None
    if doc is None:
        return False
    lines = src.splitlines(keepends=True)
    new = lines[:doc.lineno - 1] + [f'"""{text}"""\n'] + lines[doc.end_lineno:]
    with open(path, "w") as fh:
        fh.write("".join(new))
    return True


def scan(tree, extra_terms=()):
    pat = re.compile("|".join(LEAK + [re.escape(t) for t in extra_terms]), re.I)
    hits = []
    for d, dirs, files in os.walk(tree):
        dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git")]
        for f in files:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, tree)
            try:
                with open(p, errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        m = pat.search(line)
                        if m:
                            hits.append((rel, i, m.group(0)))
            except OSError:
                pass
    return hits


def sanitize(tree, extra_terms=()):
    log = {"removed": [], "stripped": [], "refrozen": [], "leaks": []}
    docs = os.path.join(tree, "docs")
    for name in sorted(os.listdir(docs)):
        if name not in KEEP_DOCS:
            p = os.path.join(docs, name)
            (shutil.rmtree if os.path.isdir(p) else os.remove)(p)
            log["removed"].append(f"docs/{name}")
    for d in DROP_DIRS:
        p = os.path.join(tree, d)
        if os.path.exists(p):
            shutil.rmtree(p)
            log["removed"].append(d)
    scripts = os.path.join(tree, "scripts")
    if os.path.isdir(scripts):
        for name in sorted(os.listdir(scripts)):
            if name not in KEEP_SCRIPTS:
                p = os.path.join(scripts, name)
                (shutil.rmtree if os.path.isdir(p) else os.remove)(p)
                log["removed"].append(f"scripts/{name}")
    # The contract-change log: the numbered contract changes are rules the agent must follow and
    # stay; the "Instrument ..." sections are incident history that names causes, and go.
    ccl = os.path.join(docs, "contract-changes.md")
    if os.path.exists(ccl):
        with open(ccl) as fh:
            parts = re.split(r"(?m)^(?=## )", fh.read())
        kept = [x for x in parts if not x.startswith("## Instrument")]
        if len(kept) != len(parts):
            with open(ccl, "w") as fh:
                fh.write("".join(kept))
            log["stripped"].append("docs/contract-changes.md (instrument history sections)")
    for rel, text in STRIP_DOCSTRINGS.items():
        p = os.path.join(tree, rel)
        if os.path.exists(p) and strip_docstring(p, text):
            log["stripped"].append(rel)
    for rel, pairs in REPLACE.items():
        p = os.path.join(tree, rel)
        if os.path.exists(p):
            with open(p) as fh:
                src = fh.read()
            new = src
            for old, repl in pairs:
                new = new.replace(old, repl)
            if new != src:
                with open(p, "w") as fh:
                    fh.write(new)
                log["stripped"].append(rel)
    # Tests that pin a known defect go whole, including any that describe a case's cause.
    tests = os.path.join(tree, "tests")
    if os.path.isdir(tests):
        for rel, _, _ in sorted(set((h[0], 0, "") for h in scan(tests, extra_terms))):
            p = os.path.join(tests, rel)
            if os.path.exists(p):
                os.remove(p)
                log["removed"].append(f"tests/{rel}")
    man_path = os.path.join(docs, "phase3-freeze.json")
    with open(man_path) as fh:
        man = json.load(fh)
    for rel in man["files"]:
        p = os.path.join(tree, rel)
        if os.path.exists(p) and sha(p) != man["files"][rel]:
            man["files"][rel] = sha(p)
            log["refrozen"].append(rel)
    with open(man_path, "w") as fh:
        json.dump(man, fh, indent=1)
    log["leaks"] = [h for h in scan(tree, extra_terms) if h[0] not in CODE_OK]
    return log


def case_terms(case_dir):
    terms = []
    if case_dir and os.path.isdir(case_dir):
        for c in os.listdir(case_dir):
            k = os.path.join(case_dir, c, "key.json")
            if os.path.exists(k):
                with open(k) as fh:
                    terms += json.load(fh).get("leak_terms", [])
    return terms


if __name__ == "__main__":
    tree = sys.argv[1]
    cases = sys.argv[sys.argv.index("--case-dir") + 1] if "--case-dir" in sys.argv else None
    log = sanitize(tree, case_terms(cases))
    print(json.dumps(log, indent=1))
    sys.exit(1 if log["leaks"] else 0)
