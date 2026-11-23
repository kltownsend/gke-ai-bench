"""The subject's supervisor: owns the batch and the one result line. Never imports workspace code.

Build review 3 reproduced an output takeover: in the single-process runner, workspace code read
the batch from `__main__`, wrote a forged result array to the runner's output, and exited first.
Build review 4 reproduced the next layer: one child served the whole stream, kept state across
requests, and a descendant it forked outlived it and wrote into /tmp for the next child to read.

- **Supervisor (this file).** Runs as uid 0 inside the container with only SETUID, SETGID, KILL
  and DAC_OVERRIDE, on a path that never includes the workspace. It reads the whole batch from
  stdin, starts children, and sends one request at a time with a fresh nonce. Every reply is
  checked: nonce, shape, and the op's result schema. It writes the single JSON array the judge
  reads.
- **Child (subject/child.py).** Runs as uid 65534 with no capabilities, stdin, stdout and stderr
  on /dev/null, and only its two pipe ends. It imports the tree and answers. Different uid: it
  can't signal, trace, or open the supervisor's descriptors through /proc (the host kernel has no
  Yama, so same-uid would allow all three).

**Isolated mode** (LAB_ISOLATE=1, the default; the judge uses it for every workspace under test):
a fresh child per request. After each request the supervisor kills every process of uid 65534,
checks none survive, and wipes /tmp and /dev/shm, the only writable places, so no memory, file or
process carries from one request to the next. A child costs about 108 ms (measured, mostly
importing the Clinic). **Shared mode** (LAB_ISOLATE=0) keeps one child per gate, for the judge's
own trees (the clean baseline and pinned references), where nothing is hostile.

A child that dies, hangs past its op's limit (OP_TIMEOUT), answers out of turn or with the wrong
nonce is killed and its request gets a distinct error. Nothing a child does can make a result
pass; it can make its own request fail, or exhaust the container and fail its own gate.
"""

import json
import os
import secrets
import select
import shutil
import signal
import subprocess
import sys
import time

# Per-request limits by op, set from measured latency (harness/measure_latency.py, 2026-11-20,
# isolated mode, child start and cleanup included): call p99 97 ms (max 123), scenario max 55 ms,
# session p99 67 ms (max 79), selftest 3.4 s. Every request op gets 10 s, about 100 times its p99;
# the self-test gets 60 s, about 17 times its measured time.
OP_TIMEOUT = {"call": 10, "get": 10, "scenario": 10, "session": 10, "selftest": 60}
MAX_REPLY = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024        # all replies together; the judge caps output at the same size
CHILD_UID = 65534
ISOLATE = os.environ.get("LAB_ISOLATE", "1") != "0"
TIMING = os.environ.get("LAB_TIMING") == "1"     # measurement only: adds "_ms" (start, answer, clear) to each reply
SCRATCH = ("/tmp", "/dev/shm")
CHILD_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
             "PYTHONHASHSEED": "0", "PYTHONPYCACHEPREFIX": "/tmp/pyc"}

_batch = json.loads(sys.stdin.read())
sys.stdin.close()


class Abnormal(Exception):
    pass


class Child:
    def __init__(self):
        req_r, self._req_w = os.pipe()
        self._rep_r, rep_w = os.pipe()
        self.proc = subprocess.Popen(
            [sys.executable, "-s", "-S", "-P", "/child.py", str(req_r), str(rep_w)],
            pass_fds=(req_r, rep_w), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, cwd="/tmp", env=CHILD_ENV,
            user=CHILD_UID, group=CHILD_UID, extra_groups=[])
        os.close(req_r)
        os.close(rep_w)
        self.buf = b""

    def ask(self, req):
        limit = OP_TIMEOUT.get(req.get("op"), 10)
        nonce = secrets.token_hex(16)
        try:
            os.write(self._req_w, (json.dumps(dict(req, _n=nonce)) + "\n").encode())
        except OSError:
            raise Abnormal(f"child gone before the request (exit {self.proc.poll()})")
        deadline = time.monotonic() + limit
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Abnormal(f"no reply in {limit} s")
            r, _, _ = select.select([self._rep_r], [], [], left)
            if not r:
                continue
            chunk = os.read(self._rep_r, 65536)
            if not chunk:
                raise Abnormal(f"child exited mid-request (exit {self.proc.wait()})")
            self.buf += chunk
            if len(self.buf) > MAX_REPLY:
                raise Abnormal("reply over the cap")
        line, self.buf = self.buf.split(b"\n", 1)
        if self.buf:
            raise Abnormal("more than one reply to one request")
        try:
            rep = json.loads(line)
        except ValueError:
            raise Abnormal("reply is not JSON")
        if not isinstance(rep, dict) or rep.pop("_n", None) != nonce:
            raise Abnormal("reply with the wrong nonce")
        return rep

    def kill(self):
        try:
            self.proc.kill()
        except ProcessLookupError:
            pass
        self.proc.wait()
        for fd in (self._req_w, self._rep_r):
            try:
                os.close(fd)
            except OSError:
                pass


def reap():
    """The supervisor is PID 1 in the container, so a child's orphaned descendants become its
    children: reap them, or they linger as zombies under the child's uid."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def child_pids():
    """Live (non-zombie) processes of the child's uid."""
    out = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/status") as fh:
                status = fh.read()
        except OSError:
            continue
        uid = next((l for l in status.splitlines() if l.startswith("Uid:")), "")
        state = next((l for l in status.splitlines() if l.startswith("State:")), "")
        if CHILD_UID in map(int, uid.split()[1:]) and "Z" not in state.split()[1:2]:
            out.append(int(p))
    return out


def clear_children():
    """Kill every process of the child's uid and wipe the writable scratch. True when clean."""
    for _ in range(50):
        reap()
        pids = child_pids()
        if not pids:
            break
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.01)
    reap()
    clean = not child_pids()
    for d in SCRATCH:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for name in entries:
            p = os.path.join(d, name)
            try:
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p)
                else:
                    os.unlink(p)
            except OSError:
                clean = False
    return clean


def _result(x):
    return isinstance(x, dict) and set(x) in ({"ok", "value"}, {"ok", "error"}) \
        and isinstance(x["ok"], bool) and (x["ok"] or isinstance(x["error"], str)) \
        and ("value" in x) == x["ok"]


def schema(req, rep):
    """None if REP has the shape REQ's op promises, else why not."""
    if not _result(rep):
        return "not a result"
    if not rep["ok"]:
        return None
    v, op = rep["value"], req.get("op")
    if op == "scenario":
        if not (isinstance(v, list) and len(v) == len(req["steps"]) and all(_result(s) for s in v)):
            return "scenario steps malformed"
    elif op == "session":
        base = {"transcript", "trace", "log", "end"}
        driver = "arm" in req or "script" in req
        extra = ({"intents"} | ({"writes"} if req.get("score") else set())
                 | ({"driver_reads"} if req.get("read_lines") else set())) if driver else set()
        if not (isinstance(v, dict) and set(v) == base | extra):
            return "session malformed"
    elif op == "replay":
        if not (isinstance(v, dict) and set(v) == {"replies", "trace", "model_log"}
                and isinstance(v["replies"], list) and len(v["replies"]) == len(req.get("lines", []))):
            return "replay malformed"
    elif op == "selftest":
        if not (isinstance(v, dict) and set(v) == {"ok", "tail"} and isinstance(v["ok"], bool)):
            return "selftest malformed"
    return None


results, child, total, broken = [], None, 0, None
for req in _batch:
    if broken:
        results.append({"ok": False, "error": f"subject abnormal: {broken}"})
        continue
    t0 = time.monotonic()
    if child is None:
        child = Child()
    try:
        rep = child.ask(req)
        why = schema(req, rep)
        if why:
            rep = {"ok": False, "error": f"schema violation: {why}"}
    except Abnormal as e:
        rep = {"ok": False, "error": f"subject abnormal: {e}"}
        child.kill()
        child = None
    if child is not None and ISOLATE:
        child.kill()
        child = None
    if child is None and not clear_children():
        broken = "could not clear the previous child's processes or scratch"
    if TIMING:
        rep = dict(rep, _ms=round((time.monotonic() - t0) * 1000, 1))
    total += len(json.dumps(rep, default=str))
    if total > MAX_TOTAL:
        rep = {"ok": False, "error": "subject abnormal: replies over the total cap"}
        broken = "replies over the total cap"
    results.append(rep)
if child is not None:
    child.kill()
clear_children()
sys.stdout.write(json.dumps(results, default=str, sort_keys=True) + "\n")
sys.stdout.flush()
