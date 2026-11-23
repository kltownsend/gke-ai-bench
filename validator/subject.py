"""The judge's side of the subject channel: one container per gate.

The container gets a read-only copy of the tree under test and the subject's two files, nothing
else: no network, read-only root, no privilege escalation, and PID, memory and CPU limits. Inside
it the supervisor (subject/runner.py) holds only SETUID, SETGID, KILL and DAC_OVERRIDE: enough to
start the child (subject/child.py) as uid 65534 with no capabilities at all, stop every process of
that uid, and wipe the scratch it wrote. Only the child imports the tree. ISOLATE (the default)
gives each request a fresh child; the judge turns it off only for its own trees. Case files, keys, hidden inputs and validator code are never
mounted. Each gate starts a new container and removes it at the end, so nothing survives from one
gate to the next.

Framing is the judge's: the subject writes one JSON array and nothing else, or the gate is a
protocol violation (see ask()).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import uuid

IMAGE = os.environ.get(
    "LAB_SUBJECT_IMAGE",
    "python@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91")
RUNNER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "subject", "runner.py")
CHILD = os.path.join(os.path.dirname(RUNNER), "child.py")
SOURCES = (RUNNER, CHILD, os.path.abspath(__file__))      # everything that shapes an answer
# The gate's wall clock (build reviews 3 and 4). Measured 2026-11-20 in isolated mode: 500 T03
# probes in 21 s, 483 regression probes in 34 s, about 40 to 70 ms per request. A gate gets 60 s
# plus 0.5 s per request: per request that's 5 to 10 times the measured time, so the rule a slow
# fix meets is plain: one that makes the probed code more than several times slower than the
# baseline fails the gate, closed. Per-request limits (runner.OP_TIMEOUT) catch a single hang;
# this catches a slow tree. The cap is only a backstop.
GATE_BASE_S, GATE_PER_REQ_S, GATE_CAP_S, SELFTEST_GATE_S = 60, 0.5, 1200, 120


def gate_timeout(requests: list[dict]) -> int:
    if any(r.get("op") == "selftest" for r in requests):
        return SELFTEST_GATE_S
    return int(min(GATE_CAP_S, GATE_BASE_S + GATE_PER_REQ_S * len(requests)))


class SubjectError(RuntimeError):
    pass


MAX_OUTPUT = 64 * 1024 * 1024


def ask(tree: str, requests: list[dict], timeout: int | None = None, isolate: bool = True,
        timing: bool = False) -> list[dict]:
    """Run REQUESTS against TREE in a fresh container; return one result per request, in order.

    The subject must write exactly one line: a JSON array with one result per request. Anything
    else (extra lines, a short array, more than MAX_OUTPUT bytes, a timeout) is a protocol
    violation: every result comes back {"ok": False, "error": "protocol violation: ..."}, which
    fails any probe, and never a pass. stderr is discarded.
    """
    timeout = gate_timeout(requests) if timeout is None else timeout

    def violation(why):
        return [{"ok": False, "error": f"protocol violation: {why}"} for _ in requests]

    stage = tempfile.mkdtemp(prefix="lab-gate-")
    try:
        ws = os.path.join(stage, "ws")
        shutil.copytree(tree, ws, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        os.chmod(stage, 0o755)
        for d, dirs, files in os.walk(ws):
            os.chmod(d, 0o755)
            for f in files:
                p = os.path.join(d, f)
                if not os.path.islink(p):
                    os.chmod(p, 0o644)
        runner, child = os.path.join(stage, "runner.py"), os.path.join(stage, "child.py")
        for src, dst in ((RUNNER, runner), (CHILD, child)):
            shutil.copy2(src, dst)
            os.chmod(dst, 0o644)
        name = f"lab-subject-{uuid.uuid4().hex[:12]}"
        cmd = ["docker", "run", "--rm", "-i", "--name", name,
               "--network", "none", "--read-only", "--tmpfs", "/tmp:size=64m",
               "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "KILL",
               "--cap-add", "DAC_OVERRIDE",
               "--security-opt", "no-new-privileges",
               "--pids-limit", "128", "--memory", "1g", "--cpus", "2", "--ulimit", "nofile=256:256",
               "-v", f"{ws}:/ws:ro", "-v", f"{runner}:/runner.py:ro", "-v", f"{child}:/child.py:ro",
               "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONHASHSEED=0",
               "-e", f"LAB_ISOLATE={'1' if isolate else '0'}", "-e", f"LAB_TIMING={'1' if timing else '0'}",
               "-e", "PYTHONPYCACHEPREFIX=/tmp/pyc",          # never read caches from the workspace
               IMAGE, "python", "-I", "-S", "/runner.py"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        out, over = bytearray(), [False]

        def pump():
            proc.stdin.write(json.dumps(requests).encode())
            proc.stdin.close()
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    return
                out.extend(chunk)
                if len(out) > MAX_OUTPUT:
                    over[0] = True
                    return

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive() or over[0]:
            subprocess.run(["docker", "kill", name], capture_output=True)
            proc.kill()
            return violation("output over the cap" if over[0] else f"gate timed out at {timeout} s")
        proc.wait(timeout=30)
        lines = [x for x in out.decode(errors="replace").splitlines() if x.strip()]
        if len(lines) != 1:
            return violation(f"{len(lines)} output lines, expected 1")
        try:
            results = json.loads(lines[0])
        except ValueError:
            return violation("output is not JSON")
        if not isinstance(results, list) or len(results) != len(requests) \
                or not all(isinstance(r, dict) and "ok" in r for r in results):
            return violation("result array has the wrong shape")
        return results
    finally:
        shutil.rmtree(stage, ignore_errors=True)
