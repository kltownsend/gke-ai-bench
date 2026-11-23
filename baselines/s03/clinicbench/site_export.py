"""The explorer's data as versioned files for the labs site (labs.layer2c.com/clinic).

Pure functions, so they can be tested without the cells: `export(data, pairs, statuses)` returns
{relative path: bytes}. scripts/export_clinic_site.py runs it on the real data and writes the
files.

What leaves the bench is an allowlist, field by field; everything else is dropped. The runs
aren't secret (Keith: "the runs in themselves don't have value"), so the allowlist is for
accuracy and hygiene: no machine paths or addresses, and every field labeled by what kind of
thing it is, so nobody reads the author's interpretation as the patient's truth.
"""

from __future__ import annotations

import hashlib
import json
import re

SCHEMA = "clinic.site/1"

# What each field is. The site and the machine-readable summaries both carry this.
KINDS = {
    "patient_truth": "Set in code before the call: the patient's type, settings, manner, and what they disclosed and how clearly.",
    "measured": "What the bench recorded: the outcome, the breaks, how the conversation ended, the clinician's lines and tool calls.",
    "trace": "The commitment trace: each write checked against the decisions the architecture declares. "
             "It can't see a decision nobody declared.",
    "author_reading": "Our reading of why a write was wrong. An interpretation, not the patient's truth.",
}
NOTES = [
    "Patients are synthetic: code-written truths that speak in templates. No real patient or health data.",
    "A clean commitment trace means no declared decision was left open, not that the write was right; "
    "read authority beside wrong writes.",
    "Each cell is one run of its reader; run-to-run variation isn't in the intervals.",
]

CELL_FIELDS = ["id", "variants", "instrument_hash", "adapter_hash", "freeze_at", "envelope", "status", "note", "tokens"]
VENUE_FIELDS = ["started_utc", "image_digest", "serve_script_sha256", "par", "node", "model_revision"]
RUN_FIELDS = ["sessions", "ok", "skipped", "infra_final", "minutes", "finished_utc"]
SUMMARY_FIELDS = ["sessions", "seeds", "headline", "break_props", "curves", "thresholds", "tau", "by_type",
                  "handoff_causes", "reads", "invalid_answers", "cost"]
TRANSCRIPT_FIELDS = {"key": "patient_truth", "seed": "patient_truth", "type": "patient_truth", "setting": "patient_truth",
                     "comm": "patient_truth", "outcome": "measured", "break_props": "measured", "end": "measured",
                     "lines": "measured", "why_wrong": "author_reading", "commitments": "trace"}
LINE_FIELDS = ["turn", "speaker", "text", "act", "disclosed", "model", "tools"]

# Strings that mustn't leave the bench.
FORBIDDEN = [re.compile(p) for p in (
    r"/Users/", r"/home/", r"[A-Za-z]:\\\\Users", r"\b10\.\d+\.\d+\.\d+\b", r"\b192\.168\.\d+\.\d+\b",
    r"\b172\.(1[6-9]|2\d|3[01])\.\d+\.\d+\b", r"localhost:\d+", r"127\.0\.0\.1",
    r"sk-[A-Za-z0-9_-]{8,}", r"AIza[0-9A-Za-z_-]{20,}", r"AKIA[0-9A-Z]{12,}", r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"(?i)bearer\s+[A-Za-z0-9._-]{12,}", r"(?i)api[_-]?key", r"[\w.+-]+@[\w-]+\.[\w.]+")]


class ExportError(RuntimeError):
    pass


def pick(d, fields):
    return {k: d[k] for k in fields if k in d}


# The nested shapes inside a conversation, field by field. A key not listed fails the export, so a
# new field can't leave the bench without someone deciding it should.
NESTED = {
    "disclosed": {"name", "clarity", "affirmed", "excluded", "cant_say"},
    "model": {"kind", "answer", "valid"},
    "tools": {"tool", "args", "ok", "refusal"},
    "commitment": {"turn", "requirement_id", "tool", "args", "committed", "refusal", "outcome", "open", "deps", "auth"},
    "dep": {"p", "state", "ok", "who", "by"},
    "auth": {"kind", "decisions"},
    "decision": {"decision", "by", "answers"},
    "setting": {"clarity", "probes", "pressure"},
    "comm": {"directness", "verbosity", "precision", "fluency", "patience"},
    "end": {"reason", "by", "turns", "error"},
}


def strict(obj, kind, where):
    extra = set(obj) - NESTED[kind]
    if extra:
        raise ExportError(f"{where}: unexpected field(s) {sorted(extra)} in {kind}")
    return obj


def clean_line(line, where):
    out = pick(line, LINE_FIELDS)
    for k, kind in (("disclosed", "disclosed"), ("model", "model"), ("tools", "tools")):
        for x in out.get(k) or []:
            strict(x, kind, where)
    return out


def clean_transcript(t):
    where = f"transcript {t.get('key')}"
    out = pick(t, TRANSCRIPT_FIELDS)
    for k in ("setting", "comm", "end"):
        if isinstance(out.get(k), dict):
            strict(out[k], k, where)
    out["lines"] = [clean_line(x, where) for x in t.get("lines", [])]
    for w in out.get("commitments") or []:
        strict(w, "commitment", where)
        for d in w.get("deps") or []:
            strict(d, "dep", where)
        if w.get("auth"):
            strict(w["auth"], "auth", where)
            for x in w["auth"].get("decisions") or []:
                strict(x, "decision", where)
    return out


def clean_cell(c):
    cell = pick(c["cell"], CELL_FIELDS)
    cell["venue"] = pick(c["cell"].get("venue") or {}, VENUE_FIELDS)
    cell["run"] = pick(c["cell"].get("run") or {}, RUN_FIELDS)
    return cell


def dumps(obj):
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def digest(b):
    return hashlib.sha256(b).hexdigest()


def scan(path, b):
    text = b.decode()
    for rx in FORBIDDEN:
        m = rx.search(text)
        if m:
            raise ExportError(f"{path}: contains {m.group(0)!r}, which mustn't leave the bench")


def check_intervals(intervals):
    for pair, metrics in intervals.items():
        for k, x in metrics.items():
            if not x["lo"] - 1e-12 <= x["diff"] <= x["hi"] + 1e-12:
                raise ExportError(f"interval {pair} {k}: diff {x['diff']} outside [{x['lo']}, {x['hi']}]")


def export(data, pairs, statuses, provenance, sources=None, content=None):
    """{relative path: bytes} for the site. `statuses` maps cell id to published, example or running;
    `sources` maps a cell id to its source summary, which the export must reproduce exactly."""
    check_intervals(data["intervals"])
    files, cells = {}, {}
    for cid, c in data["cells"].items():
        status = statuses.get(cid)
        if status not in ("published", "example"):
            raise ExportError(f"cell {cid}: status {status!r} can't be published")
        summary = pick(c["summary"], SUMMARY_FIELDS)
        if sources is not None and sources.get(cid) is not None:
            if pick(sources[cid], SUMMARY_FIELDS) != summary:
                raise ExportError(f"cell {cid}: exported summary differs from its source")
        body = {"schema": SCHEMA, "kinds": KINDS, "notes": NOTES, "cell": clean_cell(c), "summary": summary,
                "venue": c.get("venue"), "metered_usd": c.get("metered_usd", 0.0),
                "clean_right": c.get("clean_right"),
                "transcripts": [clean_transcript(t) for t in c.get("transcripts", [])]}
        b = dumps(body)
        path = f"data/cells/{cid}.{digest(b)[:12]}.json"
        s = dumps({"schema": SCHEMA, "notes": NOTES, "cell": clean_cell(c), "summary": summary})
        spath = f"data/cells/{cid}.{digest(s)[:12]}.summary.json"
        files[path], files[spath] = b, s
        cells[cid] = {"file": path, "summary_file": spath, "status": status,
                      "variants": c["cell"]["variants"], "note": c["cell"].get("note"),
                      "clean_right": c.get("clean_right")}
    package = dumps(data["public"]["package"])
    ppath = f"data/package.{digest(package)[:12]}.json"
    files[ppath] = package
    index = {"schema": SCHEMA, "provenance": provenance, "kinds": KINDS, "notes": NOTES,
             "axes": data["axes"], "reference": data["reference"], "cells": cells, "index": data["index"],
             "running": data.get("running", []), "pairs": pairs, "intervals": data["intervals"],
             "boot": data["boot"], "tau": data["tau"], "dial_notes": data["dial_notes"], "footer": data["footer"],
             "prices": data["prices"], "cost_note": data["cost_note"], "free_note": data["free_note"],
             "full_set_sessions": data["full_set_sessions"],
             "public": {"repository": data["public"]["repository"], "commit": data["public"]["commit"],
                        "package_file": ppath}}
    index["content"] = {}
    for name, doc in (content or {}).items():
        b = dumps({"schema": SCHEMA, "blocks": doc})
        cpath = f"content/{name}.{digest(b)[:12]}.json"
        files[cpath] = b
        index["content"][name] = cpath
    for cid in data["index"].values():
        if cid not in cells:
            raise ExportError(f"index points at cell {cid}, which isn't exported")
    files["data/index.json"] = dumps(index)
    for path, b in files.items():
        scan(path, b)
    return files


# ------------------------------------------------------------------ Markdown, as structured blocks
# Only what the two public documents use. The site renders blocks with React elements, never HTML.

INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)|\*(.+?)\*")


def inline(text, link):
    """Runs: {"t": "text"|"strong"|"em"|"code"|"link", "v": str, "href"?: str}. `link(target)` maps a
    path or URL to an href, or None to leave it as text."""
    runs, pos = [], 0
    for m in INLINE.finditer(text):
        if m.start() > pos:
            runs.append({"t": "text", "v": text[pos:m.start()]})
        strong, code, ltext, lhref, em = m.groups()
        if strong is not None:
            runs.append({"t": "strong", "v": strong})
        elif code is not None:
            href = link(code)
            runs.append({"t": "code", "v": code, **({"href": href} if href else {})})
        elif ltext is not None:
            href = link(lhref)
            runs.append({"t": "link", "v": ltext, "href": href} if href else {"t": "text", "v": ltext})
        else:
            runs.append({"t": "em", "v": em})
        pos = m.end()
    if pos < len(text):
        runs.append({"t": "text", "v": text[pos:]})
    return runs


def blocks(md, link):
    """Headings, paragraphs, bullet and numbered lists, tables and rules."""
    out, para, lines = [], [], md.splitlines()

    def flush():
        if para:
            out.append({"b": "p", "runs": inline(" ".join(para), link)})
            para.clear()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln.strip():
            flush(); i += 1; continue
        h = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if h:
            flush(); out.append({"b": "h", "level": len(h.group(1)), "runs": inline(h.group(2), link)}); i += 1; continue
        if re.match(r"^-{3,}$", ln):
            flush(); out.append({"b": "hr"}); i += 1; continue
        if ln.startswith("|"):
            flush()
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.match(r"^:?-{2,}:?$", c) for c in cells):
                    rows.append([inline(c, link) for c in cells])
                i += 1
            out.append({"b": "table", "head": rows[0], "rows": rows[1:]})
            continue
        lm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", ln)
        if lm:
            flush()
            ordered = lm.group(2)[0].isdigit()
            items = []
            while i < len(lines):
                m2 = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i].rstrip())
                if not m2:
                    if lines[i].startswith("  ") and items and lines[i].strip():
                        items[-1] += " " + lines[i].strip(); i += 1; continue
                    break
                items.append(m2.group(3)); i += 1
            out.append({"b": "ol" if ordered else "ul", "items": [inline(x, link) for x in items]})
            continue
        para.append(ln.strip()); i += 1
    flush()
    return out


def doc_links(repository, commit, routes):
    """Link rules for the public documents: the two documents the site renders point at their
    site routes; other repository paths point at the pinned public commit; anything else that
    looks like a URL is kept; plain words aren't links."""
    def link(target):
        t = target.strip()
        if t in routes:
            return routes[t]
        if re.match(r"^https?://", t):
            return t
        if re.match(r"^[\w.-]+/[\w./-]*$|^[\w-]+\.(md|py|json)$", t) and not t.startswith("."):
            return f"https://{repository}/blob/{commit}/{t.rstrip('/')}" if "." in t.split("/")[-1] else \
                f"https://{repository}/tree/{commit}/{t.rstrip('/')}"
        return None
    return link
