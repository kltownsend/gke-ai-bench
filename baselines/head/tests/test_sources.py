"""The source manifest resolves, and the verifier catches what it says it catches."""

from __future__ import annotations

import copy
import json
import os
import subprocess

import pytest

from clinicbench import sources as S


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "toy"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "doc.md").write_text("# Doc\n\n## Rulings\n\nOne.\n")
    git(root, "add", "doc.md")
    git(root, "commit", "-qm", "doc")
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    man = {"schema": S.SCHEMA, "repos": {"toy": {"commit": commit}},
           "sources": [{"id": "toy.doc", "repo": "toy", "path": "doc.md", "commit": commit,
                        "sha256": S.sha256((root / "doc.md").read_bytes())}],
           "freeze": {"instrument_hash": "x", "adapter_hash": "y", "tag": "none", "tag_commit": ""},
           "cells": [], "amendments": [], "known_defects": [], "issues": [],
           "rulings": [{"id": "r1", "sources": [{"source_id": "toy.doc", "heading": "Rulings"}]}]}
    return root, man


def run(man, root, tmp_path):
    return S.verify(man, {"toy": str(root)}, cells_root=str(tmp_path / "no-cells"))


def test_clean_manifest_passes(repo, tmp_path):
    root, man = repo
    assert run(man, root, tmp_path) == []


def test_unknown_schema_fails(repo, tmp_path):
    root, man = repo
    man["schema"] = "clinicbench.sources/2"
    assert run(man, root, tmp_path)[0][0] == "FAIL"


def test_hash_mismatch_fails(repo, tmp_path):
    root, man = repo
    man["sources"][0]["sha256"] = "0" * 64
    assert ("FAIL", "toy.doc") in {r[:2] for r in run(man, root, tmp_path)}


def test_unknown_commit_fails(repo, tmp_path):
    root, man = repo
    man["sources"][0]["commit"] = "f" * 40
    assert ("FAIL", "toy.doc") in {r[:2] for r in run(man, root, tmp_path)}


def test_edit_after_pin_is_drift_not_fail(repo, tmp_path):
    root, man = repo
    (root / "doc.md").write_text("# Doc\n\n## Rulings\n\nTwo.\n")
    assert [r[:2] for r in run(man, root, tmp_path)] == [("DRIFT", "toy.doc")]


def test_missing_heading_fails(repo, tmp_path):
    root, man = repo
    man["rulings"][0]["sources"][0]["heading"] = "Rulings, renamed"
    assert ("FAIL", "r1") in {r[:2] for r in run(man, root, tmp_path)}


def test_citing_an_unpinned_source_fails(repo, tmp_path):
    root, man = repo
    man["rulings"][0]["sources"][0]["source_id"] = "toy.other"
    assert ("FAIL", "r1") in {r[:2] for r in run(man, root, tmp_path)}


def test_reference_cell_off_the_freeze_fails(repo, tmp_path):
    root, man = repo
    man["cells"] = [{"id": "abc", "role": "reference", "instrument_hash": "other"}]
    assert ("FAIL", "abc") in {r[:2] for r in run(man, root, tmp_path)}


def test_cell_recorded_after_the_pin_is_new_not_drift(repo, tmp_path):
    root, man = repo
    cells = tmp_path / "cells"
    cells.mkdir()
    (cells / "index.json").write_text(json.dumps([{"id": "new123"}]))
    rows = S.verify(man, {"toy": str(root)}, cells_root=str(cells))
    assert [r[:2] for r in rows] == [("NEW", "new123")]


def test_missing_checkout_is_unavailable(repo, tmp_path):
    _, man = repo
    rows = S.verify(man, {"toy": str(tmp_path / "absent")}, cells_root=str(tmp_path / "no-cells"))
    assert [r[0] for r in rows] == ["UNAVAILABLE"]


def test_amendments_read_from_headings():
    text = ("# clinicbench contract changes\n\n## C1. Payment leaks escalate (Keith, 2026-10-22)\n\n"
            "## Arm E added (Keith, 2026-10-22: \"Let's run E\")\n")
    a = S.amendments(text)
    assert [(x["id"], x["kind"]) for x in a] == [("C1", "contract"), (None, "instrument")]
    assert a[1]["ruling"] == "Keith, 2026-10-22: \"Let's run E\""


@pytest.mark.skipif(not os.path.exists(S.MANIFEST), reason="no manifest pinned")
def test_committed_manifest_resolves():
    man = json.load(open(S.MANIFEST))
    rows = S.verify(copy.deepcopy(man))
    assert [r for r in rows if r[0] == "FAIL"] == []
    assert {c["id"] for c in man["cells"] if c["role"] == "reference"} == {"b1f4bb47f6a2", "20ee74942bb3"}


def test_a_subheading_is_not_another_amendment():
    text = "# Changes\n\n## C1. One (Keith, 2026-10-22)\n\n### Why\n\ntext\n\n## Two (Keith, 2026-10-22)\n"
    assert [a["heading"] for a in S.amendments(text)] == ["C1. One (Keith, 2026-10-22)", "Two (Keith, 2026-10-22)"]
