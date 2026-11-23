"""Run the Clinic's self-test against a tree, in a fresh process. Prints one JSON line."""

import json
import sys

sys.path.insert(0, sys.argv[1])
from clinicbench.selftest import run_all  # noqa: E402

ok, report = run_all()
print(json.dumps({"ok": ok, "tail": report.splitlines()[-1]}))
