#!/usr/bin/env python3
"""Read the traceability sample: passing fixes set aside for a person to check the cause.

The diagnosis is scored on named symbols. One passing fix in ten, chosen by a
hash of the run id, is set aside so a person can read the agent's stated cause against its fix.
That reading is traceability, not a control: it never changes a verdict. The lab reports how often
the stated cause didn't match the fix, beside the scores.

usage: trace_review.py RECORD_DIR                       list unread samples
       trace_review.py RECORD_DIR --show RUN            print one sample's cause and diff
       trace_review.py RECORD_DIR --mark RUN matches|does_not_match|unclear [NOTE]
       trace_review.py RECORD_DIR --summary             counts by reading
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "controller"))
from effects import Record  # noqa: E402

READINGS = ("matches", "does_not_match", "unclear")
rec = Record(sys.argv[1])
samples = {s["run"]: s for s in rec.read("trace_samples")}
reads = {}
for r in rec.read("trace_reads"):
    reads[r["run"]] = r                          # the latest reading wins
args = sys.argv[2:]
if not args:
    for run, s in samples.items():
        if run not in reads:
            print(f'{run}  {s["case"]}  attempt {s["attempt"]}  {", ".join(s["symbols"])}')
elif args[0] == "--show":
    s = samples[args[1]]
    print(f'{s["case"]}  {s["run"]}  outcome {s["outcome"]}\nsymbols: {", ".join(s["symbols"])}\n'
          f'cause: {s["cause"]}\n\n{s["diff"]}')
elif args[0] == "--mark":
    run, reading = args[1], args[2]
    if run not in samples or reading not in READINGS:
        sys.exit(f"need a sampled run and one of {READINGS}")
    rec.trace_read(run, samples[run]["proposal"], os.environ.get("USER", "?"), reading, " ".join(args[3:]))
elif args[0] == "--summary":
    counts = {k: 0 for k in READINGS}
    for r in reads.values():
        counts[r["reading"]] += 1
    print({"sampled": len(samples), "read": len(reads), **counts})
