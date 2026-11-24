#!/usr/bin/env python3
"""The agent's actor on Agent Substrate: the main process inside the actor, in the agent image.

Substrate has no exec and no file copy into an actor. Its documented interaction is an HTTP server
inside the actor, reached through atenet-router; the shell demo (demos/sandbox at v1.0.0) runs a
`/process` endpoint that spawns a subprocess per request. This is that, in the lab's terms:

- `POST /process`: the demo's contract ({command, envvars, cwd, timeout} in; {stdout, stderr,
  exitCode, error} out). agy runs here as a subprocess.
- `PUT /workspace`: the body is a tar stream that replaces /work, with the Pod harness's bounded,
  all-or-nothing unpack (agent/pod_sandbox.py). The lab's addition to the demo's contract.
- `GET /workspace`: /work as a tar stream. Also the lab's.
- `GET /healthz`: for the template's wakeup probe.
- `GET /state`, and `POST /state/mark`, `/state/quiet`, `/state/child-ping`: what a restore has to
  bring back, for the snapshot and restore probe (harness/resume_probe.py): this process's PID, a
  value chosen at boot, a nonce the probe writes into memory before each suspend (so replaying an
  older snapshot fails), a counter a thread advances every 50 ms (paused at a barrier before each
  suspend, so the subject is quiescent, and resumed after), resident memory, and a child process
  joined by a pipe that answers n with n+1 (so a live exchange at an exact sequence position shows
  the child's execution state came back, not just its PID).

And a credential shim on 127.0.0.1:8081, the reason agy can run here at all: actors get no
metadata server (ateworker drops 169.254/16), and agy's credential library reads its token from
the metadata server named by GCE_METADATA_HOST. The shim answers with the placeholder
`ate-placeholder`, never a real token. The egress gateway replaces the Authorization header on the
one allowed host (aiplatform.googleapis.com) with the lab-agent token it resolves from a Kubernetes
Secret (infra/substrate/egress-policy.yaml), so the actor holds no usable credential. Checked
locally against a fake front end (harness/placeholder_check/): agy sends the placeholder, trusts
the gateway CA through SSL_CERT_FILE, and needs no other host.

The process must stay up: "A process that has exited cannot be suspended."

usage: actor_server.py [PORT]   (default 8080; the shim's port is LAB_SHIM_PORT, default 8081)
"""
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pod_sandbox import MAX_TAR_BYTES, pack, unpack_replace  # noqa: E402

WORK = os.environ.get("LAB_WORK", "/work")
# The lab project and the agent's service account, from the actor template's env (rendered from
# harness/lab_env.sh). The email falls back to the default service account name.
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("LAB_PROJECT", "")
EMAIL = os.environ.get("LAB_AGENT") or f"lab-agent@{PROJECT}.iam.gserviceaccount.com"
PLACEHOLDER = "ate-placeholder"
MAX_TIMEOUT_S = 3600
BOOT = uuid.uuid4().hex                 # chosen once per process; a restart changes it
T0 = time.monotonic()
TICKS = [0]
QUIET = threading.Event()               # set: the counter stops (the barrier before a suspend)
MARK = {"nonce": None, "set_at": None}
CHILD: list = []                        # the boot-time child process, for the restore probe
CHILD_LOCK = threading.Lock()
CHILD_CODE = ("import sys\nseq = 0\nfor l in sys.stdin:\n    seq += 1\n"
              "    sys.stdout.write(f'{int(l) + 1} {seq}\\n'); sys.stdout.flush()")


def _tick():
    while True:
        time.sleep(0.05)
        if not QUIET.is_set():
            TICKS[0] += 1


def rss_kb():
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None


def child_ping(n: int) -> dict:
    """The child answers n+1 and how many lines it has read: its own state, held in its memory."""
    child = CHILD[0]
    with CHILD_LOCK:
        child.stdin.write(f"{n}\n".encode())
        child.stdin.flush()
        reply, seq = child.stdout.readline().split()
        return {"reply": int(reply), "seq": int(seq)}


def state() -> dict:
    child = CHILD[0] if CHILD else None
    return {"pid": os.getpid(), "boot": BOOT, "ticks": TICKS[0], "quiet": QUIET.is_set(),
            "mark": MARK["nonce"], "rss_kb": rss_kb(),
            "monotonic_s": round(time.monotonic() - T0, 3), "wall": time.time(),
            "child_pid": child.pid if child else None,
            "child_alive": bool(child) and child.poll() is None}


WORK_LOCK = threading.Lock()
MAX_OUTPUT_BYTES = 16 << 20


def capped(f):
    """A command's captured stream, cut at MAX_OUTPUT_BYTES with a note of how much was dropped."""
    size = f.seek(0, os.SEEK_END); f.seek(0)
    text = f.read(MAX_OUTPUT_BYTES).decode(errors="replace")
    return text + (f"\n[output cut: {size - MAX_OUTPUT_BYTES} more bytes]" if size > MAX_OUTPUT_BYTES else "")


def replace_work(data: bytes):
    """Replace /work's contents with the tar stream's. The Pod harness's unpack swaps directories
    by renaming the target, which needs its parent writable; /work's parent is / and the agent's
    uid can't write there (found on the first Substrate probe, 2026-11-21: "Permission denied:
    '/.pod-ws-...'"). So the stream is validated and fully unpacked into a directory inside /work
    before anything is touched; a bad stream leaves the old contents. The swap itself moves entries one
    at a time, so it isn't atomic: a failure mid-swap leaves a mix, and the next replace starts over.
    Replace and pack hold WORK_LOCK, so the harness's own swap and download don't interleave; a process
    the agent left running can still change files during either."""
    incoming = os.path.join(WORK, ".lab-incoming")
    shutil.rmtree(incoming, ignore_errors=True)
    os.mkdir(incoming)
    try:
        unpack_replace(data, incoming)
    except BaseException:
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    for name in os.listdir(WORK):
        if name == ".lab-incoming":
            continue
        p = os.path.join(WORK, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)
    for name in os.listdir(incoming):
        os.rename(os.path.join(incoming, name), os.path.join(WORK, name))
    os.rmdir(incoming)


class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Actor(http.server.BaseHTTPRequestHandler):
    # Keep-alive, so the probe can time a request on a connection opened before the suspend: each new
    # connection through `kubectl ate proxy` costs up to ~390 ms once its burst is spent (local
    # shakeout, 2026-11-22), which would otherwise land in every timing. Every reply sets
    # Content-Length, which HTTP/1.1 keep-alive needs.
    protocol_version = "HTTP/1.1"
    # Headers and body go out in two writes; with Nagle on, the second waits for the peer's delayed
    # ACK (a flat ~40 ms per keep-alive request in the shakeout).
    disable_nagle_algorithm = True

    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("Transfer-Encoding isn't supported; send Content-Length")
        raw = self.headers.get_all("Content-Length") or ["0"]
        if len(set(raw)) != 1 or not raw[0].strip().isdigit():
            raise ValueError(f"bad Content-Length {raw!r}")
        n = int(raw[0])
        if n > limit:
            raise ValueError(f"body over {limit} bytes")
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"ok": True})
        if self.path == "/workspace":
            with WORK_LOCK:
                data = pack(WORK)
            return self.reply(200, data, "application/x-tar")
        if self.path == "/state":
            return self.reply(200, state())
        return self.reply(404, {"error": "not found"})

    def do_PUT(self):
        if self.path != "/workspace":
            return self.reply(404, {"error": "not found"})
        try:
            body = self.body(MAX_TAR_BYTES)
            with WORK_LOCK:
                replace_work(body)
        except (ValueError, OSError) as e:
            return self.reply(400, {"error": str(e)})
        return self.reply(200, {"ok": True})

    def do_POST(self):
        if self.path.startswith("/state/"):
            try:
                req = json.loads(self.body(1 << 16) or b"{}")
                if self.path == "/state/mark":
                    MARK.update(nonce=str(req["nonce"]), set_at=time.time())
                    return self.reply(200, state())
                if self.path == "/state/quiet":
                    (QUIET.set if req.get("on") else QUIET.clear)()
                    return self.reply(200, state())
                if self.path == "/state/child-ping":
                    return self.reply(200, {"n": int(req["n"]), **child_ping(int(req["n"]))})
            except (KeyError, ValueError, TypeError, OSError) as e:
                return self.reply(400, {"error": f"bad request: {e}"})
            return self.reply(404, {"error": "not found"})
        if self.path != "/process":
            return self.reply(404, {"error": "not found"})
        try:
            req = json.loads(self.body(1 << 20) or b"{}")
            cmd = req.get("command") or []
            if not cmd or not all(isinstance(c, str) for c in cmd):
                return self.reply(400, {"error": "command required"})
            timeout = min(float(str(req.get("timeout") or MAX_TIMEOUT_S).rstrip("s")), MAX_TIMEOUT_S)
        except (ValueError, TypeError) as e:
            return self.reply(400, {"error": f"bad request: {e}"})
        env = dict(os.environ, **{str(k): str(v) for k, v in (req.get("envvars") or {}).items()})
        # Output goes to files, not memory, and each stream returns at most MAX_OUTPUT_BYTES, so a
        # command that prints without end can't exhaust the actor's or the harness's memory.
        with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
            try:
                p = subprocess.run(cmd, cwd=req.get("cwd") or WORK, env=env, stdout=fo, stderr=fe, timeout=timeout)
                resp = {"exitCode": p.returncode}
            except subprocess.TimeoutExpired:
                resp = {"exitCode": -1, "error": f"timed out after {timeout:.0f}s"}
            except OSError as e:
                resp = {"exitCode": -1, "error": str(e)}
            resp["stdout"], resp["stderr"] = capped(fo), capped(fe)
        return self.reply(200, resp)


class Shim(http.server.BaseHTTPRequestHandler):
    """The GCE metadata endpoints Google's credential libraries ask for, with a placeholder token.
    Bound to loopback only."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0].rstrip("/")
        sa = "/computeMetadata/v1/instance/service-accounts"
        answers = {
            "": ("text/plain", "computeMetadata/"),
            "/computeMetadata/v1": ("text/plain", "instance/\nproject/\n"),
            "/computeMetadata/v1/project/project-id": ("text/plain", PROJECT),
            sa: ("text/plain", f"default/\n{EMAIL}/\n"),
        }
        for who in ("default", EMAIL):
            answers[f"{sa}/{who}"] = ("application/json", json.dumps(
                {"email": EMAIL, "aliases": ["default"], "scopes": ["https://www.googleapis.com/auth/cloud-platform"]}))
            answers[f"{sa}/{who}/email"] = ("text/plain", EMAIL)
            answers[f"{sa}/{who}/scopes"] = ("text/plain", "https://www.googleapis.com/auth/cloud-platform")
            answers[f"{sa}/{who}/token"] = ("application/json", json.dumps(
                {"access_token": PLACEHOLDER, "expires_in": 3599, "token_type": "Bearer"}))
        if p not in answers:
            self.send_response(404)
            self.end_headers()
            return
        ctype, body = answers[p]
        data = body.encode()
        self.send_response(200)
        self.send_header("Metadata-Flavor", "Google")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_state():
    threading.Thread(target=_tick, daemon=True).start()
    CHILD.append(subprocess.Popen([sys.executable, "-I", "-u", "-c", CHILD_CODE], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL))


def serve(port: int, shim_port: int):
    start_state()
    shim = Threaded(("127.0.0.1", shim_port), Shim)
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    srv = Threaded(("", port), Actor)
    print(json.dumps({"event": "listening", "port": port, "shim_port": shim_port}), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8080, int(os.environ.get("LAB_SHIM_PORT", "8081")))
