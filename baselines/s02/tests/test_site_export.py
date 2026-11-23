"""The labs-site export: allowlisted, versioned, and open to arms it has never seen."""

from __future__ import annotations

import json

import pytest

from clinicbench import site_export as X

SUMMARY = {"sessions": 2, "seeds": 1, "headline": {"any_break": 0.0}, "break_props": [], "curves": {},
           "thresholds": {}, "tau": 0.1, "by_type": [], "handoff_causes": {}, "reads": {}, "invalid_answers": 0,
           "cost": {"tokens": {}}, "private_note": "drop me"}


def cell(cid, consent, extra=None):
    return {"cell": {"id": cid, "variants": {"consent": consent, "reader": "gemma-off", "dimension": "payment",
                                             "patients": "lab23-18"},
                     "instrument_hash": "i", "adapter_hash": "a", "source_run": "cell_F_payment",
                     "venue": {"api_base": "http://10.0.0.111:8000/v1", "node": "spark2", "par": 4},
                     "run": {"sessions": 2, "minutes": 1.0}, "status": "run", "note": None, **(extra or {})},
            "summary": dict(SUMMARY), "venue": "owned DGX Spark, 4 at once", "metered_usd": 0.0, "clean_right": 1,
            "transcripts": [{"key": "k", "seed": "s", "type": "t", "setting": {}, "comm": {}, "outcome": "right",
                             "break_props": [], "end": {}, "why_wrong": None, "commitments": [], "path": "/Users/x",
                             "lines": [{"turn": 1, "speaker": "patient", "text": "Yes.", "act": "accept",
                                        "debug": "drop me"}]}]}


def data(consents=("F", "E")):
    cells = {f"c{x}": cell(f"c{x}", x) for x in consents}
    iv = {"diff": 0.1, "lo": 0.05, "hi": 0.15, "patients": 1}
    return {"axes": [{"id": "consent", "variants": [{"id": x} for x in consents]}], "reference": {"consent": "F"},
            "cells": cells, "index": {f"{x}|gemma-off": f"c{x}" for x in consents},
            "intervals": {"cF|cE": {"right": iv}}, "boot": 10, "tau": 0.1, "dial_notes": {}, "footer": [],
            "prices": {}, "cost_note": "", "free_note": "", "full_set_sessions": 2,
            "public": {"repository": "github.com/kltownsend/clinicbench", "commit": "abc", "package": {"m.json": "{}"}}}


def run(d, **kw):
    statuses = {cid: "published" for cid in d["cells"]}
    return X.export(d, {"cF|cE": ["k"]}, statuses, {"clinicbench_commit": "x"}, **kw)


def test_allowlist_drops_what_isnt_listed():
    files = run(data())
    body = json.loads(next(b for p, b in files.items() if p.startswith("data/cells/cF.") and not p.endswith("summary.json")))
    assert "api_base" not in body["cell"]["venue"] and "source_run" not in body["cell"]
    assert "private_note" not in body["summary"]
    t = body["transcripts"][0]
    assert "path" not in t and "debug" not in t["lines"][0]
    assert body["kinds"]["author_reading"] and body["notes"]


def test_files_are_content_addressed():
    a, b = run(data()), run(data())
    assert a == b
    d = data(); d["cells"]["cF"]["clean_right"] = 2
    c = run(d)
    assert {p for p in a if p.startswith("data/cells/cF.")} != {p for p in c if p.startswith("data/cells/cF.")}


def test_a_future_arm_needs_no_code():
    files = run(data(("F", "E", "Z9")))
    index = json.loads(files["data/index.json"])
    assert "cZ9" in index["cells"] and index["index"]["Z9|gemma-off"] == "cZ9"
    assert any(v["id"] == "Z9" for v in index["axes"][0]["variants"])


def test_a_summary_that_differs_from_its_source_fails():
    d = data()
    src = {"cF": dict(SUMMARY, sessions=99)}
    with pytest.raises(X.ExportError, match="differs from its source"):
        run(d, sources=src)
    run(d, sources={"cF": dict(SUMMARY)})


def test_an_interval_that_excludes_its_own_difference_fails():
    d = data()
    d["intervals"]["cF|cE"]["right"] = {"diff": 0.3, "lo": 0.05, "hi": 0.15, "patients": 1}
    with pytest.raises(X.ExportError, match="outside"):
        run(d)


@pytest.mark.parametrize("bad", ["/Users/keith/x", "http://10.0.0.111:8000", "sk-abcdefgh12345678", "a@b.com",
                                 "sk-ant-api03-abcdefghijklmnop", "AIzaSyA1234567890abcdefghij", "AKIAABCDEFGHIJKLMNOP",
                                 "172.20.1.5", "localhost:8000", "Bearer abcdefghijklmnopq", "ghp_abcdefghijklmnopqrstuvwx"])
def test_machine_details_never_leave(bad):
    d = data()
    d["cells"]["cF"]["transcripts"][0]["lines"][0]["text"] = bad
    with pytest.raises(X.ExportError, match="mustn't leave"):
        run(d)


def test_unpublishable_status_fails():
    d = data()
    with pytest.raises(X.ExportError, match="can't be published"):
        X.export(d, {}, {"cF": "published", "cE": "running"}, {})


def test_markdown_blocks_and_links():
    link = X.doc_links("github.com/kltownsend/clinicbench", "abc", {"docs/worked-path.md": "/clinic/worked-path"})
    md = ("# Title\n\nRead `docs/worked-path.md` after, and see `docs/adapter-contract.md` and "
          "`examples/adversarial/`. **Bold** and *em* and layer2c.com stay text.\n\n"
          "- one\n- two\n\n1. first\n2. second\n\n| a | b |\n|---|---|\n| 1 | **2** |\n\n---\n")
    b = X.blocks(md, link)
    assert [x["b"] for x in b] == ["h", "p", "ul", "ol", "table", "hr"]
    runs = b[1]["runs"]
    hrefs = [r.get("href") for r in runs if r["t"] == "code"]
    assert hrefs == ["/clinic/worked-path", "https://github.com/kltownsend/clinicbench/blob/abc/docs/adapter-contract.md",
                     "https://github.com/kltownsend/clinicbench/tree/abc/examples/adversarial"]
    assert not any(r["t"] == "link" for r in runs)
    assert b[4]["head"][0][0]["v"] == "a" and b[4]["rows"][0][1][0]["t"] == "strong"


def test_the_real_documents_parse():
    import os
    pub = os.path.join(os.path.dirname(__file__), "..", "..", "clinicbench-public")
    link = X.doc_links("github.com/kltownsend/clinicbench", "abc", {})
    for f in ("CHALLENGES.md", "docs/worked-path.md"):
        p = os.path.join(pub, f)
        if not os.path.exists(p):
            pytest.skip("public bench not checked out")
        b = X.blocks(open(p).read(), link)
        assert b[0]["b"] == "h" and len(b) > 5
        text = json.dumps(b)
        assert "](" not in text   # no raw link syntax left behind


def test_an_unknown_nested_field_fails():
    d = data()
    d["cells"]["cF"]["transcripts"][0]["lines"][0]["tools"] = [{"tool": "book", "args": [], "ok": True, "refusal": None, "debug": 1}]
    with pytest.raises(X.ExportError, match="unexpected field"):
        run(d)
    d = data()
    d["cells"]["cF"]["transcripts"][0]["commitments"] = [{"turn": 1, "secret": 2}]
    with pytest.raises(X.ExportError, match="unexpected field"):
        run(d)
