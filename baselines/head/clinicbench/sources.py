"""Source pinning: every governing source, reference cell and attached amendment, by hash.

Stage 1 of the clinic to architecture spec (layer2csite
`docs/proposals/2026-10-22-clinic-to-architecture-spec.md`). `docs/sources/declared.json` is the
hand-kept list: which files govern what, which cells are the reference, which defects and issues
are attached and where each one is written down. `scripts/pin_sources.py` resolves it at exact
commits into `docs/sources/manifest.json`; `scripts/verify_sources.py` checks the manifest still
resolves and says what has drifted since.

Sources are pinned by repository name, commit and path, never by a local absolute path. A
repository is found on this machine as a sibling of this checkout unless a root is given. Cells
live in `results/`, which git ignores, so they're pinned by content hash only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess

from . import cells as C

SCHEMA = "clinicbench.sources/1"
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
DECLARED = os.path.join(ROOT, "docs", "sources", "declared.json")
MANIFEST = os.path.join(ROOT, "docs", "sources", "manifest.json")
CELLS = os.path.join(ROOT, "results", "cells")
CELL_FILES = ["cell.json", "summary.json", "transcripts.json"]
CHANGES_SOURCE = "clinic.contract-changes"
FREEZE_SOURCE = "clinic.freeze"


class PinError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: str, *args: str) -> bytes:
    r = subprocess.run(["git", "-C", root, *args], capture_output=True)
    if r.returncode:
        raise PinError(f"git {' '.join(args)} in {os.path.basename(root)}: {r.stderr.decode().strip()}")
    return r.stdout


def repo_roots(names, overrides=None) -> dict:
    """Where each named repository is checked out here: this checkout, a sibling, or an override."""
    overrides = overrides or {}
    here = os.path.basename(ROOT)
    out = {}
    for n in names:
        out[n] = overrides.get(n) or (ROOT if n == here else os.path.join(os.path.dirname(ROOT), n))
    return out


def blob(root: str, commit: str, path: str) -> bytes:
    return git(root, "show", f"{commit}:{path}")


def headings(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^#{1,6}\s+(.+?)\s*$", text, re.M)]


def amendments(changes_md: str) -> list[dict]:
    """Every logged change in contract-changes.md, in order. C-numbered ones amend the contract.
    Only second-level headings are entries; a subheading inside one isn't another amendment."""
    out = []
    for h in [m.group(1).strip() for m in re.finditer(r"^##\s+(.+?)\s*$", changes_md, re.M)]:
        m = re.match(r"(C\d+)\.\s*(.*)", h)
        ruling = re.search(r"\((Keith[^)]*)\)\s*$", h)
        out.append({"id": m.group(1) if m else None,
                    "kind": "contract" if m else "instrument",
                    "heading": h,
                    "ruling": ruling.group(1) if ruling else None})
    return out


def tree_digest(directory: str) -> dict:
    """One hash over every file under a directory: sorted relative path, tab, file hash."""
    lines = []
    for dp, _, fs in os.walk(directory):
        for f in fs:
            p = os.path.join(dp, f)
            lines.append(f"{os.path.relpath(p, directory)}\t{sha256(open(p, 'rb').read())}\n")
    lines.sort()
    return {"files": len(lines), "sha256": sha256("".join(lines).encode())}


def cell_hashes(cid: str, cells_root: str = CELLS, sessions: bool = True) -> dict:
    d = os.path.join(cells_root, cid)
    out = {"files": {f: sha256(open(os.path.join(d, f), "rb").read()) for f in CELL_FILES}}
    if sessions:
        out["sessions"] = tree_digest(os.path.join(d, "sessions"))
    return out


def pin(declared: dict, overrides=None, cells_root: str = CELLS, commits=None) -> dict:
    """Resolve the declared sources at their repositories' HEAD. Refuses an uncommitted source."""
    roots = repo_roots(declared["repos"], overrides)
    commits = dict(commits or {})
    for n, root in roots.items():
        commits.setdefault(n, git(root, "rev-parse", "HEAD").decode().strip())
    sources, texts = [], {}
    for s in declared["sources"]:
        root = roots[s["repo"]]
        if git(root, "status", "--porcelain", "--", s["path"]).strip():
            raise PinError(f"{s['id']}: {s['repo']}/{s['path']} has uncommitted changes; commit it first")
        data = blob(root, commits[s["repo"]], s["path"])
        texts[s["id"]] = data.decode("utf-8", "replace")
        sources.append({**s, "commit": commits[s["repo"]], "sha256": sha256(data)})

    freeze_src = next(s for s in sources if s["id"] == FREEZE_SOURCE)
    freeze = json.loads(texts[FREEZE_SOURCE])
    inst, adapter = C.hashes(freeze["files"])
    tag = declared["freeze"]["tag"]
    freeze_rec = {"source_id": FREEZE_SOURCE, "sha256": freeze_src["sha256"], "frozen_at": freeze["frozen_at"],
                  "tag": tag, "tag_commit": git(roots[freeze_src["repo"]], "rev-list", "-n1", tag).decode().strip(),
                  "instrument_hash": inst, "adapter_hash": adapter}

    index = {c["id"]: c for c in json.load(open(os.path.join(cells_root, "index.json")))}
    reference = set(declared["reference_cells"])
    missing = reference - set(index)
    if missing:
        raise PinError(f"reference cells not in the local cache: {sorted(missing)}")
    cells = []
    for cid, row in index.items():
        cell = json.load(open(os.path.join(cells_root, cid, "cell.json")))
        cells.append({"id": cid, "role": "reference" if cid in reference else "recorded",
                      "status": row["status"], "note": row.get("note"), "variants": cell["variants"],
                      "instrument_hash": cell["instrument_hash"], "adapter_hash": cell["adapter_hash"],
                      "freeze_at": cell.get("freeze_at"), "envelope": cell.get("envelope"),
                      "source_run": cell.get("source_run"), "run": cell.get("run"),
                      "storage": f"results/cells/{cid} (git-ignored; local only)",
                      **cell_hashes(cid, cells_root)})

    return {"schema": SCHEMA,
            "spec": declared["spec"],
            "repos": {n: {**declared["repos"][n], "commit": commits[n]} for n in declared["repos"]},
            "sources": sources,
            "freeze": freeze_rec,
            "cells": cells,
            "amendments": [{**a, "source_id": CHANGES_SOURCE} for a in amendments(texts[CHANGES_SOURCE])],
            "rulings": declared["rulings"],
            "known_defects": declared["known_defects"],
            "issues": declared["issues"]}


def verify(man: dict, overrides=None, cells_root: str = CELLS, sessions: bool = False) -> list[tuple]:
    """Check a manifest. Returns (level, subject, message) rows; level is FAIL, DRIFT, NEW or UNAVAILABLE.

    FAIL: a pin doesn't resolve or a cited heading is gone. DRIFT: it resolves, but the source has
    moved on since and the pin is stale. NEW: a cell recorded since the pin. The cell set grows, so
    that's expected, and it leaves every existing pin valid. UNAVAILABLE: this machine can't check it.
    """
    if man.get("schema") != SCHEMA:
        return [("FAIL", "manifest", f"unknown schema {man.get('schema')!r}; this verifier reads {SCHEMA}")]
    rows = []
    roots = repo_roots(man["repos"], overrides)
    texts, now = {}, {}
    for s in man["sources"]:
        root = roots[s["repo"]]
        if not os.path.isdir(os.path.join(root, ".git")):
            rows.append(("UNAVAILABLE", s["id"], f"no checkout of {s['repo']} here"))
            continue
        try:
            data = blob(root, s["commit"], s["path"])
        except PinError as e:
            rows.append(("FAIL", s["id"], f"doesn't resolve at {s['commit'][:12]}: {e}"))
            continue
        if sha256(data) != s["sha256"]:
            rows.append(("FAIL", s["id"], f"hash at {s['commit'][:12]} differs from the pin"))
            continue
        texts[s["id"]] = data.decode("utf-8", "replace")
        path = os.path.join(root, s["path"])
        current = open(path, "rb").read() if os.path.exists(path) else None
        now[s["id"]] = current
        if current is None:
            rows.append(("DRIFT", s["id"], "file no longer exists in the working tree"))
        elif sha256(current) != s["sha256"]:
            rows.append(("DRIFT", s["id"], "changed since the pin"))

    fz = man["freeze"]
    if FREEZE_SOURCE in texts:
        inst, adapter = C.hashes(json.loads(texts[FREEZE_SOURCE])["files"])
        if (inst, adapter) != (fz["instrument_hash"], fz["adapter_hash"]):
            rows.append(("FAIL", "freeze", "instrument or adapter hash doesn't recompute from the pinned freeze"))
        repo = next(s["repo"] for s in man["sources"] if s["id"] == FREEZE_SOURCE)
        try:
            tagged = git(roots[repo], "rev-list", "-n1", fz["tag"]).decode().strip()
            if tagged != fz["tag_commit"]:
                rows.append(("FAIL", "freeze", f"tag {fz['tag']} moved to {tagged[:12]}"))
        except PinError:
            rows.append(("FAIL", "freeze", f"tag {fz['tag']} doesn't exist"))
    for c in man["cells"]:
        if c["role"] == "reference" and c["instrument_hash"] != fz["instrument_hash"]:
            rows.append(("FAIL", c["id"], "reference cell wasn't run under the pinned freeze"))
        if not os.path.isdir(os.path.join(cells_root, c["id"])):
            rows.append(("UNAVAILABLE", c["id"], "cell not in the local cache"))
            continue
        got = cell_hashes(c["id"], cells_root, sessions)
        for f, h in got["files"].items():
            if h != c["files"][f]:
                rows.append(("FAIL", c["id"], f"{f} differs from the pin"))
        if sessions and got["sessions"] != c["sessions"]:
            rows.append(("FAIL", c["id"], "session records differ from the pin"))

    index = os.path.join(cells_root, "index.json")
    if os.path.exists(index):
        pinned = {c["id"] for c in man["cells"]}
        for row in json.load(open(index)):
            if row["id"] not in pinned:
                rows.append(("NEW", row["id"], "recorded since the pin; re-pin to include it"))

    if now.get(CHANGES_SOURCE) is not None:
        pinned = {a["heading"] for a in man["amendments"]}
        for a in amendments(now[CHANGES_SOURCE].decode("utf-8", "replace")):
            if a["heading"] not in pinned:
                rows.append(("DRIFT", "amendments", f"logged but not attached: {a['heading']}"))

    ids = {s["id"] for s in man["sources"]}
    for kind in ("rulings", "known_defects", "issues"):
        for item in man[kind]:
            for cite in item["sources"]:
                if cite["source_id"] not in ids:
                    rows.append(("FAIL", item["id"], f"cites {cite['source_id']}, which isn't pinned"))
                    continue
                text = texts.get(cite["source_id"])
                if text is None:
                    continue
                if cite.get("heading") and cite["heading"] not in headings(text):
                    rows.append(("FAIL", item["id"], f"heading not in {cite['source_id']}: {cite['heading']}"))
    return rows
