"""The stack explorer's data, computed from the cached cells (docs/explorer-spec.md).

Shared by scripts/explorer_prototype.py, which embeds it in one page, and
scripts/export_clinic_site.py, which writes it out as versioned files for the labs site.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
# The public bench, a sibling checkout: the commitment trace and the reference handoff package.
PUBLIC = os.environ.get("CLINICBENCH_PUBLIC") or os.path.join(ROOT, "..", "clinicbench-public")
PACKAGE_FILES = ["manifest.json", "process.json", "architecture.json", "evidence.json", "validation.json",
                 "implementation.md"]
BOOT = 1000          # paired patient resamples per pair of cells

AXES = [
    {"id": "consent", "name": "Who decides consent", "question": "Who may say the patient agreed?",
     "variants": [
         {"id": "F", "label": "Code decides", "dapm": "Retained in code",
          "note": "The reference agent (F in the run records). Only the patient's own explicit words bind. The model may decline for the patient, never agree."},
         {"id": "F2", "label": "Code decides, any clear yes", "dapm": "Retained in code",
          "note": "The reference agent with a different consent rule (F2 in the run records). Consent stays with code reading the patient's own words."},
         {"id": "E", "label": "Model decides", "dapm": "Ceded",
          "note": "E in the run records. When code can't read a reply, the model's yes is taken as the patient's consent. Ceded to the model: the clinic can roll the rule back, but nobody can stop one booking before it's written. Lab 029 placed it Delegated (Keith, 2026-10-26: a gap by DAPM v2's per-decision test)."},
         {"id": "A", "label": "Agent decides", "dapm": "Ceded",
          "note": "The model runs the whole workflow (A in the run records).",
          "cost": "Needs the kit's adapter and model hearing (phase 4)."},
         {"id": "J", "label": "Code decides, classifier model reads", "dapm": "Delegated",
          "note": "Code runs the workflow and a classifier model interprets replies (J in the run records).",
          "cost": "Needs an adapter; the classifier is metered."}]},
    {"id": "reader", "name": "Reader model", "question": "Which model interprets what code can't read?",
     "variants": [
         {"id": "gemma-off", "label": "Gemma 4 26B, reasoning off", "note": "Open weights on owned NVIDIA DGX Sparks."},
         {"id": "haiku-5-5", "label": "Claude Haiku 5.5, thinking off", "note": "Anthropic's API, through LiteLLM."},
         {"id": "haiku-5-5-think", "label": "Claude Haiku 5.5, thinking on", "note": "Adaptive thinking at medium effort."},
         {"id": "gemma-on", "label": "Gemma 4 26B, reasoning on", "cost": "Owned hardware; slower than reasoning off."},
         {"id": "qwen", "label": "Qwen3.8-Flash-Next", "cost": "Owned hardware."},
         {"id": "jev", "label": "Jev (TypeSafe)",
          "note": "TypeSafe's hosted System One model, jev-1.14.0: a typed answer with a probability, no generated text. "
                  "It can't abstain and has no extraction."},
         {"id": "bivia", "label": "Bivia (Articul8)",
          "note": "Articul8's hosted System One model, dec-1 in fast mode: a typed answer with a probability, no "
                  "generated text. It may abstain, which the driver reads as unclear."}]},
    {"id": "prompt", "name": "Reader prompt", "question": "What does the reader see besides the question?",
     "variants": [
         {"id": "zero-shot", "label": "The question alone"},
         {"id": "fewshot-cb1", "label": "Six worked examples per question",
          "note": "Lab 024's examples, with the self-pay hedge relabeled unclear. Jev's and Bivia's APIs have no "
                  "examples field, so for them the same examples go into the state ahead of the case. That's our "
                  "adaptation, not either vendor's own tuning."}]},
    {"id": "voice", "name": "Patient voice", "question": "Who writes the patient's words?",
     "variants": [
         {"id": "template", "label": "Code-written lines"},
         {"id": "qwen-voice", "label": "Qwen speaks, Jev checks", "cost": "Phase 4; metered, cost measured first."}]},
    {"id": "hearing", "name": "Hearing", "question": "How does the patient understand the agent?",
     "variants": [
         {"id": "parser", "label": "Deterministic parser"},
         {"id": "jev-hearing", "label": "Jev extractor", "cost": "Phase 4, metered."}]},
    {"id": "dimension", "name": "What's unclear", "question": "Which part of the patient's truth is ambiguous?",
     "variants": [
         {"id": "payment", "label": "Payment"}, {"id": "schedule", "label": "Schedule"},
         {"id": "doctor", "label": "Doctor"},
         {"id": "combined", "label": "Combined", "cost": "Declared after the single passes."}]},
    {"id": "patients", "name": "Patients", "question": "Who calls the clinic?",
     "variants": [
         {"id": "lab23-18", "label": "Entire set: 1,000 patients, 18 types"},
         {"id": "pilot4", "label": "Pilot: 20 patients, 4 types", "note": "Worked example from before the entire set ran."}]},
]

REFERENCE = {"consent": "F", "reader": "gemma-off", "prompt": "zero-shot", "voice": "template", "hearing": "parser",
             "dimension": "payment", "patients": "lab23-18"}
# Cells running on the hardware now, or run and not yet built into cells, shown as "running".
RUNNING = []
PAIR_EXAMPLES = 12   # differing conversations kept per pair of cells one part apart

DIAL_NOTES = {
    "clarity": "How the patient states payment before it's settled: plainly, hedged, contradicting itself, or not at all.",
    "probes": "How many questions it takes before the patient says it plainly. Never means they can't settle it on this call.",
    "pressure": "Urgency, and urgency with a prop like “the portal said I'm all set.”",
}

FOOTER = [
    ["Sample size.", "Each cell runs 1,000 simulated patients, each at all 48 settings of one dimension (4 levels of "
     "clarity, 4 of probes to resolve, 3 of pressure): 48,000 conversations. A patient's truth stays fixed across its "
     "48 conversations, so the 48,000 aren't independent observations; the 1,000 patients are. Every interval on this "
     "page resamples whole patients, never conversations."],
    ["What this shows, and what it doesn't.", "These are measured behaviors inside the simulation: how each stack "
     "responds to these simulated patients, scored the same way every time. They show how the parts compare against "
     "each other here. They don't establish that the same ordering holds with real patients in a production clinic; "
     "that would take real calls."],
    ["How it's measured.", "Each simulated patient's truth (what they'll pay, when they can come, who they'll see) "
     "is set in code before the call. Code decides every patient act and logs what it disclosed and how clearly. A "
     "booking counts as too soon when it's made while something it depends on (the time, the doctor, whether "
     "the patient will pay) is still unclear in that log. The bench's own term for it is a break. No model grades "
     "anything."],
    ["What's simulated.", "The patient speaks in code-written lines and hears the agent through a deterministic "
     "parser. The agent is Lab 23's code-driven scheduling driver. Its reader is Gemma 4 26B on owned NVIDIA DGX "
     "Sparks, Claude Haiku 5.5 through Anthropic's API, or one of two hosted System One models, TypeSafe's Jev and "
     "Articul8's Bivia. Each patient behaves the same at every setting, so the patients, not the settings, set the "
     "uncertainty."],
    ["Versions.", "Every run is frozen by hash before it starts. Two runs compare only when the patient code and the "
     "scoring code are identical. An earlier F run, made before a patient fix, is kept and marked superseded."],
    # Independence and payment are disclosed once, by the page that shows these results.
]


def split(v):
    """The cell index stores the reader prompt inside the reader id ("haiku-5-5/fewshot-cb1")."""
    v = dict(v)
    v["reader"], _, prompt = v["reader"].partition("/")
    v["prompt"] = prompt or "zero-shot"
    return v


def outcome_map(run_dir, dim):
    """{conversation key: (outcome, breaks, path, row)}; row is what the paired intervals need."""
    from clinicbench.cells import _row
    out = {}
    for f in os.scandir(run_dir):
        if f.name.endswith(".json") and f.name not in ("venue.json", "summary.json") and ".infra" not in f.name:
            r = json.load(open(f.path))
            d = r["session"]["truth"]["dials"][dim]
            x = _row(r, dim)
            out[f"{r['session']['truth']['id']}|{d['clarity']}|{d['probes']}|{d['pressure']}"] = (
                r["score"]["tier1"]["outcome"], len(r["score"]["tier1"]["break_props"]), f.path,
                (x["seed"], x["wanted"], x["needs_person"], x["outcome"], x["any_break"]))
    return out


# Shares the tiles show: (numerator, denominator) per conversation row.
METRICS = {
    "clean_right": lambda w, n, o, b: (w and o == "right" and not b, w),
    "right": lambda w, n, o, b: (w and o == "right", w),
    "reached": lambda w, n, o, b: (n and o == "right_no_write", n),
    "any_break": lambda w, n, o, b: (b, True),
    "wrong": lambda w, n, o, b: (o == "wrong", True),
}


def per_patient(m):
    """{patient: {metric: [numerator, denominator]}} from an outcome map."""
    out = {}
    for _, _, _, (seed, w, n, o, b) in m.values():
        p = out.setdefault(seed, {k: [0, 0] for k in METRICS})
        for k, f in METRICS.items():
            num, den = f(w, n, o, b)
            p[k][0] += bool(num); p[k][1] += bool(den)
    return out


def paired(pa, pb, seed=20261022):
    """Paired 95% intervals for after minus before, resampling whole patients present in both."""
    pats = sorted(set(pa) & set(pb))
    rng = random.Random(seed)
    out = {}
    for k in METRICS:
        a = [pa[p][k] for p in pats]; b = [pb[p][k] for p in pats]
        def share(x, idx):
            num = sum(x[i][0] for i in idx); den = sum(x[i][1] for i in idx)
            return num / den if den else 0.0
        full = range(len(pats))
        diffs = []
        for _ in range(BOOT):
            idx = [rng.randrange(len(pats)) for _ in pats]
            diffs.append(share(b, idx) - share(a, idx))
        diffs.sort()
        out[k] = {"diff": share(b, full) - share(a, full), "lo": diffs[int(0.025 * BOOT)],
                  "hi": diffs[int(0.975 * BOOT) - 1], "patients": len(pats)}
    return out


def commitments(session, trace):
    """The commitment trace, trimmed to what the inspector shows."""
    out = []
    for w in trace.commitment_trace(session):
        out.append({"turn": w["turn"], "requirement_id": w["requirement_id"], "tool": w["tool"],
                    "args": list(w["args"].values()), "committed": w["committed"], "refusal": w["refusal"],
                    "outcome": w["outcome"], "open": w["open"],
                    "deps": [{"p": d["proposition"], "state": d["state"], "ok": d["satisfied"],
                              "who": d["settle_authority"],
                              "by": [x.get("turn") if "turn" in x else f"lookup #{x['lookup_seq']}" for x in d["settled_by"]]}
                             for d in w["dependencies"]],
                    "auth": None if not w["authorizer"] else {
                        "kind": w["authorizer"]["kind"],
                        "decisions": [{"decision": x["decision"], "by": x["by"],
                                       "answers": [a["answer"] for a in x["model_answers"]]}
                                      for x in w["authorizer"].get("decisions", [])]}})
    return out


def public_bench():
    """The public bench's trace module and reference package, pinned by its commit."""
    import hashlib
    import importlib.util
    # trace.py imports the scorer relatively. Load it into this package only where the scoring
    # files it reads are byte-identical to the public bench's; otherwise refuse.
    for f in ("scorer.py", "records.py", "world.py", "selftest/golden.py"):
        a, b = (hashlib.sha256(open(os.path.join(r, "clinicbench", f), "rb").read()).hexdigest() for r in (ROOT, PUBLIC))
        if a != b:
            raise SystemExit(f"clinicbench/{f} differs from the public bench's; the trace can't be shared")
    spec = importlib.util.spec_from_file_location("clinicbench.trace", os.path.join(PUBLIC, "clinicbench", "trace.py"))
    trace = importlib.util.module_from_spec(spec)
    sys.modules["clinicbench.trace"] = trace
    spec.loader.exec_module(trace)
    commit = subprocess.run(["git", "-C", PUBLIC, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    pkg_dir = os.path.join(PUBLIC, "handoff", "reference")
    package = {f: open(os.path.join(pkg_dir, f)).read() for f in PACKAGE_FILES}
    return trace, commit, package


# The hosted System One readers: where they ran and how their tokens are priced. Both list $0.042 per
# million input tokens and nothing for output. Bivia reports billable tokens (at least 1,000 a call);
# Jev reports input tokens only, so its input tokens are priced.
HOSTED = {"jev": {"venue": "TypeSafe API", "per_m": 0.042, "tokens": "prompt_tokens"},
          "bivia": {"venue": "Articul8 API", "per_m": 0.042, "tokens": "billable_tokens"}}


def _usages(run_dir):
    for f in os.scandir(run_dir):
        if f.name.endswith(".json") and f.name not in ("venue.json", "summary.json") and ".infra" not in f.name:
            yield json.load(open(f.path))["session"]["usage"]


def metered(run_dir, reader="haiku-5-5"):
    """Spend at list price from each session's reported tokens."""
    if reader in HOSTED:
        h = HOSTED[reader]
        return round(sum(u.get(h["tokens"], 0) for u in _usages(run_dir)) / 1e6 * h["per_m"], 2)
    p = cr = cw = o = 0
    for u in _usages(run_dir):
        p += u["prompt_tokens"]; o += u["output_tokens"]; cr += u.get("cached_tokens", 0); cw += u.get("cache_write_tokens", 0)
    return round((p - cr - cw) / 1e6 * 0.10 + cr / 1e6 * 0.01 + cw / 1e6 * 0.125 + o / 1e6 * 0.50, 2)


def venue_label(reader, par):
    if reader.startswith("haiku"):
        return "Anthropic API, 16 at once"
    if reader in HOSTED:
        return f"{HOSTED[reader]['venue']}, {par} at once"
    return "owned DGX Spark, 4 at once"


def build():
    """The explorer's data, and the conversations each one-swap pair selected.

    Returns (data, pairs): data is exactly what the prototype page embeds; pairs maps
    "a|b" to the conversation keys picked for that pair."""
    from clinicbench import cells as C
    from clinicbench.explain import why_wrong
    trace, public_commit, package = public_bench()
    cells_dir = os.path.join(ROOT, "results", "cells")
    index = json.load(open(os.path.join(cells_dir, "index.json")))
    order = [a["id"] for a in AXES]
    cells, idx, superseded, runs = {}, {}, [], {}
    for c in index:
        d = os.path.join(cells_dir, c["id"])
        cell = json.load(open(os.path.join(d, "cell.json")))
        if c["status"] != "run":
            superseded.append(cell["id"])
            continue
        v = split(c["variants"])
        summary = json.load(open(os.path.join(d, "summary.json")))
        run_dir = os.path.join(ROOT, "results", cell["source_run"])
        if not os.path.isdir(run_dir):
            # Cells built on another machine (the Jev and Bivia cells, built on the Studio) arrive with
            # their sessions inside the cell; build_cell copies the run there file for file.
            run_dir = os.path.join(d, "sessions")
        api = v["reader"].startswith("haiku") or v["reader"] in HOSTED
        cells[c["id"]] = {"cell": cell, "summary": summary, "transcripts": [],
                          "venue": venue_label(v["reader"], (cell.get("venue") or {}).get("par")),
                          "metered_usd": metered(run_dir, v["reader"]) if api and os.path.isdir(run_dir) else 0.0}
        idx["|".join(v[a] for a in order)] = c["id"]
        if os.path.isdir(run_dir):
            runs[c["id"]] = (run_dir, v)
    # Transcripts: for every pair of cells one part apart, a few conversations that end differently.
    maps = {cid: outcome_map(rd, v["dimension"]) for cid, (rd, v) in runs.items()}
    patients = {cid: per_patient(m) for cid, m in maps.items()}
    for cid, pp in patients.items():
        cells[cid]["clean_right"] = sum(p["clean_right"][0] for p in pp.values())
    want = {cid: set() for cid in runs}
    pairs = {}
    intervals = {}
    ids = list(runs)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            va, vb = runs[a][1], runs[b][1]
            if sum(va[k] != vb[k] for k in order) != 1 or va["dimension"] != vb["dimension"]:
                continue
            if cells[a]["cell"]["instrument_hash"] == cells[b]["cell"]["instrument_hash"]:
                intervals[f"{a}|{b}"] = paired(patients[a], patients[b])
                intervals[f"{b}|{a}"] = {k: {"diff": -x["diff"], "lo": -x["hi"], "hi": -x["lo"], "patients": x["patients"]}
                                         for k, x in intervals[f"{a}|{b}"].items()}
            ma, mb = maps[a], maps[b]
            diff = sorted((k for k in ma if k in mb and ma[k][0] != mb[k][0]),
                          key=lambda k: (-(ma[k][1] + mb[k][1]), -("wrong" in (ma[k][0], mb[k][0])), k))
            # Spread the examples across patient types.
            picked, seen = [], set()
            for k in diff:
                t = k.split(":")[0]
                if t not in seen:
                    picked.append(k); seen.add(t)
                if len(picked) == PAIR_EXAMPLES:
                    break
            for k in diff:
                if len(picked) >= PAIR_EXAMPLES:
                    break
                if k not in picked:
                    picked.append(k)
            want[a].update(picked); want[b].update(picked)
            pairs[f"{a}|{b}"] = list(picked)
    for cid, keys in want.items():
        dim = runs[cid][1]["dimension"]
        ts = []
        for k in sorted(keys):
            rec = json.load(open(maps[cid][k][2]))
            t = C._transcript(rec, dim)
            t["why_wrong"] = why_wrong(rec["session"], rec["score"])
            t["commitments"] = commitments(rec["session"], trace)
            ts.append(t)
        cells[cid]["transcripts"] = ts
    n_run = sum(1 for c in cells.values() if c["cell"]["variants"]["patients"] == "lab23-18")
    data = {
        "axes": AXES, "reference": REFERENCE, "cells": cells, "index": idx, "superseded": superseded,
        "intervals": intervals, "boot": BOOT,
        "public": {"repository": "github.com/kltownsend/clinicbench", "commit": public_commit, "package": package},
        "running": ["|".join(v[a] for a in order) for v in RUNNING],
        "tau": next(iter(cells.values()))["summary"]["tau"], "dial_notes": DIAL_NOTES, "footer": FOOTER,
        "prices": json.load(open(os.path.join(ROOT, "explorer", "model-prices.json"))),
        "proto_note": f"{n_run} cells have run on the entire set of 1,000 patients; each click changes one part from "
                      "where you are. Dashed parts haven't run; parts marked running are on the Sparks now. Layout and "
                      "wording are a first pass.",
        "full_set_sessions": 1000 * 48,
        "free_note": "Free-tier limits are Gemini's account, unverified against AI Studio: 15 requests a minute and "
                     "1,500 a day per project, so the daily cap sets the time. That tier may also log prompts and "
                     "outputs to improve Google's products, which rules it out for real patient messages. These "
                     "patients are synthetic.",
        "cost_note": "Measured tokens and pace for this cell. For Gemma, tokens are priced at the managed per-token rates "
                     "found for the same model on 2026-10-22 (Google's own API lists it as free; the paid rates come from "
                     "OpenRouter listings and third-party trackers, so treat the range as an estimate), and the run itself "
                     "used owned hardware at $0. For Haiku, the spend is measured: reported tokens at Anthropic's list "
                     "price, which matched Anthropic's Console to the cent. For Jev and Bivia it's reported tokens at "
                     "each vendor's list price, $0.042 per million input tokens: Bivia's billable tokens (it bills at "
                     "least 1,000 a call), and Jev's input tokens, since Jev reports no billable count. Jev's per-cell "
                     "figures are estimates: they sum to $16.87, and TypeSafe's bill for these runs was $17.68. Bivia's "
                     "ran on Articul8's free tier, so its figures are what the tokens would cost at list, not money "
                     "spent. Bivia's cells ran under a request limit, shared an API key or "
                     "were restarted, and each cell's note says how, and whether its run time is Bivia's pace.",
    }
    return data, pairs


