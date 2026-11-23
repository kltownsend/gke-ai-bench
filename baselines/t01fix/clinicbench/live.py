"""Phase 3 runner: the F adapter on a served model, against the code-driven patient.

Gates, in order, before any session runs:
1. the self-test passes (golden, mutation, stub grid, adapter with stub models);
2. the frozen files match docs/phase3-freeze.json (any change after the freeze is logged first);
3. the server is the recorded venue: /v1/models reports the expected model and max_model_len;
4. a real completion comes back (a server is never trusted on /health alone).

Sessions run in parallel threads and each is written to its own file by temp-then-rename, so a
batch resumes rather than restarts. A session that ends in an infrastructure error is written
apart, retried once at the end, and counted against the contract's 2% gate.

usage: python -m clinicbench.live --out results/NAME --batch canary|pilot \
           [--api-base http://localhost:8000/v1] [--par 4] [--image-digest D] [--serve-sha S]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import truth as T
from .adapters import dcitl_port as D
from .adapters.dcitl_f import DcitlF
from .hearing import DriverHearing
from .records import all_settings, single_dimension_dials
from .runner import _fname, write_atomic
from .scorer import score
from .session import run_session

ROOT = os.path.join(os.path.dirname(__file__), "..")
FREEZE = os.path.join(ROOT, "docs", "phase3-freeze.json")
MODEL = "gemma-4-26b-a4b"
ENVELOPE = 262144
PILOT_TYPES = ["selfpay_booking", "ineligible_coverage", "selfpay_retraction", "covered_booking"]
PILOT_SEEDS = 5
INFRA_GATE = 0.02


def pilot_grid():
    """4 types x 5 seeds x 48 payment settings; the same patients at every setting (paired)."""
    truths = [T.build(typ, f"pilot:{i}") for typ in PILOT_TYPES for i in range(PILOT_SEEDS)]
    return [(t, single_dimension_dials("payment", s)) for s in all_settings() for t in truths]


def canary_grid():
    g = pilot_grid()
    return sorted(g, key=lambda x: hashlib.sha256((x[0]["id"] + json.dumps(x[1])).encode()).hexdigest())[:10]


def session_id(truth, dials):
    d = dials["payment"]
    return f"{truth['id']}|payment|c={d['clarity']},p={d['probes']},r={d['pressure']}|dcitl_f"


# ------------------------------------------------------------------ gates
def frozen_ok():
    if not os.path.exists(FREEZE):
        return False, "no freeze manifest"
    man = json.load(open(FREEZE))
    bad = [p for p, h in man["files"].items()
           if hashlib.sha256(open(os.path.join(ROOT, p), "rb").read()).hexdigest() != h]
    return not bad, ("changed since the freeze: " + ", ".join(bad)) if bad else man["frozen_at"]


def _get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def envelope_ok(api_base):
    try:
        m = _get(api_base.rstrip("/") + "/models")
    except Exception as e:  # noqa: BLE001
        return False, f"no server at {api_base}: {e}", None
    d = (m.get("data") or [{}])[0]
    if d.get("id") != MODEL or d.get("max_model_len") != ENVELOPE:
        return False, f"server reports {d.get('id')} max_model_len={d.get('max_model_len')}, want {MODEL} {ENVELOPE}", m
    return True, "ok", m


def completion_ok(api_base):
    model = D.Model(f"openai/{MODEL}", api_base, 1)
    try:
        a = model.ask("probe", "Shall I book it? (yes / no)", "Yes.", "Did the patient say yes?",
                      {"yes": "yes", "no": "no"})
    except Exception as e:  # noqa: BLE001
        return False, f"completion failed: {e}"
    return a in ("yes", "no", "unclear"), f"probe answered {a!r}"


# ------------------------------------------------------------------ run
def run_one(truth, dials, api_base, out):
    sid = session_id(truth, dials)
    path = os.path.join(out, _fname(sid))
    if os.path.exists(path):
        return "skipped"
    t0 = time.time()
    s = run_session(truth, dials, lambda api, log: DcitlF(api, D.Model(f"openai/{MODEL}", api_base, 1)),
                    sid, hearing=DriverHearing())
    s["wall_clock_s"] = round(time.time() - t0, 2)
    if s["end"]["reason"] == "infra_error":
        write_atomic(path.replace(".json", ".infra.json"), {"session": s})
        return "infra"
    write_atomic(path, {"session": s, "score": score(s)})
    return "ok"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch", choices=["canary", "pilot"], required=True)
    ap.add_argument("--api-base", default="http://localhost:8000/v1")
    ap.add_argument("--par", type=int, default=4)
    ap.add_argument("--image-digest", default="not recorded")
    ap.add_argument("--serve-sha", default="not recorded")
    a = ap.parse_args(argv)

    from .selftest import run_all
    ok, report = run_all()
    print(report.splitlines()[-1])
    if not ok:
        sys.exit("self-test failed; refusing to start")
    ok, why = frozen_ok()
    if not ok:
        sys.exit(f"freeze gate: {why}; refusing to start")
    ok, why, models = envelope_ok(a.api_base)
    if not ok:
        sys.exit(f"envelope gate: {why}; refusing to start")
    ok, why = completion_ok(a.api_base)
    print(why)
    if not ok:
        sys.exit("completion gate failed; refusing to start")

    os.makedirs(a.out, exist_ok=True)
    venue = {"started_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "batch": a.batch, "api_base": a.api_base, "models": models, "envelope": ENVELOPE,
             "image_digest": a.image_digest, "serve_script_sha256": a.serve_sha,
             "freeze": json.load(open(FREEZE)), "python": platform.python_version(), "par": a.par}
    write_atomic(os.path.join(a.out, "venue.json"), venue)

    grid = canary_grid() if a.batch == "canary" else pilot_grid()
    t0 = time.time()
    with ThreadPoolExecutor(a.par) as pool:
        results = list(pool.map(lambda g: run_one(g[0], g[1], a.api_base, a.out), grid))
    retry = [g for g, r in zip(grid, results) if r == "infra"]
    if retry:                                   # rerun once, then report (contract section 10)
        for g in retry:
            p = os.path.join(a.out, _fname(session_id(*g)))
            os.replace(p.replace(".json", ".infra.json"), p.replace(".json", ".infra1.json"))
        with ThreadPoolExecutor(a.par) as pool:
            again = list(pool.map(lambda g: run_one(g[0], g[1], a.api_base, a.out), retry))
    else:
        again = []
    infra = sum(1 for r in again if r == "infra")
    summary = {"sessions": len(grid), "ok": results.count("ok") + again.count("ok"),
               "skipped": results.count("skipped"), "infra_first_try": len(retry), "infra_final": infra,
               "minutes": round((time.time() - t0) / 60, 1),
               "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    write_atomic(os.path.join(a.out, "summary.json"), summary)
    print(json.dumps(summary))
    if infra / max(len(grid), 1) > INFRA_GATE:
        sys.exit(f"infrastructure failures {infra}/{len(grid)} exceed {INFRA_GATE:.0%}: stop and diagnose")


if __name__ == "__main__":
    main()
