"""The agent sandbox, Agent Substrate edition (v1.0.1): the venue interface of agent/pod_sandbox.py,
over Substrate's documented primitives.

One actor per investigation, not one environment per agy call: the actor is created from the lab's
ActorTemplate on the first call, keeps its state between calls, can be suspended while the
investigation waits for a person and resumed with its state intact, and is deleted when the venue
closes. That's the rung's reason to exist (the about page: an investigation "paused" and resumed).
The other rungs give each call a fresh Pod; here only /work is replaced on each call, so anything
agy leaves elsewhere in the actor (its HOME, say) persists across one investigation's calls.
Disclosed in the difference matrix.

The interaction is the documented one, through `kubectl ate` (no exec, no file copy exists):
- create the actor (`kubectl ate create actor --template`) and its egress policy
  (`kubectl ate create egress-policy`, infra/substrate/egress-policy.yaml)
- forward a local port to the actor through atenet-router (`kubectl ate proxy actor`), which also
  resumes a suspended actor
- each call: PUT the workspace tar, POST the command to /process, GET the workspace tar
  (agent/actor_server.py), with the Pod harness's bounded, all-or-nothing unpack on the way back
- suspend and resume (`kubectl ate suspend --mode=sync` / `resume`), timed
- delete the actor (`kubectl ate delete actor`)

usage (library): with SubstrateVenue() as sb: sb.run(workspace, ["agy", ...])
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

from pod_sandbox import pack, unpack_replace

ATESPACE = "lab-agent"
TEMPLATE = "lab-agent"
ACTOR_PORT = 8080
POLICY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "infra", "substrate", "egress-policy.yaml")
TRANSFER_S = 300


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SubstrateVenue:
    profile = "substrate"

    def __init__(self, atespace: str = ATESPACE, template: str = TEMPLATE, context: str | None = None,
                 ready_timeout: int = 300, policy: str = POLICY):
        self.atespace, self.template, self.context = atespace, template, context
        self.ready_timeout = ready_timeout
        self.policy = policy
        self.session = uuid.uuid4().hex[:12]
        self.image = os.environ.get("LAB_ACTOR_IMAGE", "")      # recorded; the template holds it
        self.actor = f"lab-{self.session}"
        self.events: list[dict] = []
        self.created = False
        self.proxy: subprocess.Popen | None = None
        self.port: int | None = None

    # -- the CLI and the router ------------------------------------------------------------------
    def ate(self, *args, check=True, timeout=120):
        cmd = ["kubectl", *(["--context", self.context] if self.context else []), "ate", *args]
        return subprocess.run(cmd, capture_output=True, check=check, timeout=timeout)

    def start_proxy(self):
        if self.proxy and self.proxy.poll() is None:
            return
        self.port = free_port()
        cmd = ["kubectl", *(["--context", self.context] if self.context else []), "ate", "proxy", "actor",
               self.actor, "-a", self.atespace, f"{self.port}:{ACTOR_PORT}"]
        self.proxy = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def stop_proxy(self):
        if self.proxy and self.proxy.poll() is None:
            self.proxy.terminate()
            try:
                self.proxy.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proxy.kill()
        self.proxy = None

    def http(self, method: str, path: str, data: bytes | None = None, timeout: float = TRANSFER_S,
             ctype: str = "application/json") -> bytes:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"ate-target-actor": f"{self.atespace}/{self.actor}",
                                              "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:      # keep the server's reason (the first 400 had none)
            raise RuntimeError(f"{method} {path}: HTTP {e.code}: {e.read()[:500].decode(errors='replace')}") from e

    def wait_healthy(self):
        deadline, last = time.time() + self.ready_timeout, None
        while time.time() < deadline:
            if self.proxy is not None and self.proxy.poll() is not None:
                last = f"proxy exited: {self.proxy.stderr.read().decode(errors='replace')[-300:]}"
                self.start_proxy()
            try:
                if json.loads(self.http("GET", "/healthz", timeout=10)).get("ok"):
                    return
            except (urllib.error.URLError, OSError, ValueError) as e:
                last = repr(e)[:200]
            time.sleep(2)
        raise RuntimeError(f"actor {self.actor} not healthy within {self.ready_timeout}s: {last}")

    # -- lifecycle -------------------------------------------------------------------------------
    def __enter__(self):
        return self

    def ensure_actor(self):
        if self.created:
            return
        t0 = time.time()
        self.ate("create", "actor", self.actor, f"--template={self.template}", "-a", self.atespace)
        self.created = True
        self.ate("create", "egress-policy", self.actor, "-a", self.atespace, "-f", self.policy)
        self.start_proxy()
        self.wait_healthy()
        self.events.append({"actor": self.actor, "created_to_healthy_s": round(time.time() - t0, 1)})

    def run(self, workspace: str, argv: list[str], timeout: int = 1800, extra_env: dict | None = None,
            extra_mounts: list[str] | None = None) -> subprocess.CompletedProcess:
        if extra_mounts:
            raise ValueError("extra mounts aren't supported on Substrate; the workspace is the only input")
        self.ensure_actor()
        self.start_proxy()
        self.wait_healthy()
        t0 = time.time()
        self.http("PUT", "/workspace", pack(workspace), ctype="application/x-tar")
        body = {"command": list(argv), "timeout": f"{timeout}s",   # cwd: the server's /work
                "envvars": {k: str(v) for k, v in (extra_env or {}).items()}}
        poll = os.environ.get("LAB_SUBSTRATE_POLL") == "1"
        resp = self._run_polled(body, argv, timeout) if poll else \
            json.loads(self.http("POST", "/process", json.dumps(body).encode(), timeout=timeout + 60))
        unpack_replace(self.http("GET", "/workspace"), workspace)
        code = resp.get("exitCode", -1)
        self.events.append({"actor": self.actor, "call_s": round(time.time() - t0, 1), "exit": code,
                            "wait": "poll" if poll else "held",
                            **({"error": resp["error"]} if resp.get("error") else {})})
        return subprocess.CompletedProcess(argv, code, resp.get("stdout", ""),
                                           resp.get("stderr", "") + (f"\n{resp['error']}" if resp.get("error") else ""))

    def state(self, timeout: float = 120) -> dict:
        """The actor server's /state (agent/actor_server.py): what a restore must bring back."""
        return json.loads(self.http("GET", "/state", timeout=timeout))

    # A manifest of a tree, built inside the actor: every entry's type, mode, size, link target and
    # content hash (lstat, no following links), so metadata changes and empty directories count.
    MANIFEST_PY = r"""
import hashlib, json, os, stat, sys
def tree(root):
    if not os.path.isdir(root):
        return {"missing": True}
    rows = []
    for d, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for n in sorted(dirs) + sorted(files):
            p = os.path.join(d, n); st = os.lstat(p); rel = os.path.relpath(p, root)
            kind = "l" if stat.S_ISLNK(st.st_mode) else "d" if stat.S_ISDIR(st.st_mode) else "f" if stat.S_ISREG(st.st_mode) else "o"
            row = [rel, kind, oct(stat.S_IMODE(st.st_mode)), st.st_size if kind == "f" else 0]
            if kind == "l":
                row.append(os.readlink(p))
            elif kind == "f":
                h = hashlib.sha256()
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                row.append(h.hexdigest())
            rows.append(row)
    return {"entries": len(rows), "sha": hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]}
print(json.dumps({"work": tree(sys.argv[1]), "home": tree(os.path.expanduser("~"))}))
"""

    def manifest(self) -> dict:
        """The /work and HOME manifests from inside the actor. Any failure raises: a failed check
        must never compare equal to another failed check."""
        body = {"command": ["python3", "-I", "-c", self.MANIFEST_PY, "."], "timeout": "120s"}   # cwd: /work
        out = json.loads(self.http("POST", "/process", json.dumps(body).encode(), timeout=180))
        if out.get("exitCode") != 0 or out.get("error"):
            raise RuntimeError(f"manifest failed: exit {out.get('exitCode')} {out.get('error', '')} {out.get('stderr', '')[-300:]}")
        m = json.loads(out["stdout"])
        if m["work"].get("missing"):
            raise RuntimeError("manifest failed: the workspace is missing")
        return {"work": m["work"].get("sha") or "missing", "home": m["home"].get("sha") or "missing",
                "work_entries": m["work"].get("entries"), "home_entries": m["home"].get("entries")}

    # The polled wait (2026-11-23, item 7's void runs). One /process request held silent for a whole agy
    # call is cut by the router's CONNECT stream idle timeout (Envoy's default 5 minutes; item 5). So the
    # command starts detached inside the actor under a supervisor that applies the same timeout and
    # writes {stdout, stderr, exitCode, error} to /tmp/lab-run (outside /work, so the agent never sees
    # it), and the harness polls with short requests. Opt-in (LAB_SUBSTRATE_POLL=1); each call records
    # which wait it used. The actor image is unchanged.
    SUPERVISE_PY = r"""
import json, os, subprocess, sys
spec = json.loads(sys.argv[1]); out = {"stdout": "", "stderr": "", "exitCode": -1}
try:
    p = subprocess.run(spec["argv"], cwd=os.environ.get("LAB_WORK", "/work"), capture_output=True, timeout=spec["timeout"])
    out.update(stdout=p.stdout.decode(errors="replace"), stderr=p.stderr.decode(errors="replace"), exitCode=p.returncode)
except subprocess.TimeoutExpired as e:
    out.update(stdout=(e.stdout or b"").decode(errors="replace"), stderr=(e.stderr or b"").decode(errors="replace"),
               error=f"timed out after {spec['timeout']}s")
except Exception as e:
    out["error"] = repr(e)[:500]
tmp = spec["path"] + ".tmp"
json.dump(out, open(tmp, "w")); os.replace(tmp, spec["path"])
"""
    LAUNCH_PY = r"""
import os, subprocess, sys
os.makedirs("/tmp/lab-run", exist_ok=True)
subprocess.Popen([sys.executable, "-I", "-c", sys.argv[1], sys.argv[2]], start_new_session=True,
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
print("started")
"""

    def _run_polled(self, body: dict, argv: list[str], timeout: int, every: float = 10) -> dict:
        import uuid
        path = f"/tmp/lab-run/{uuid.uuid4().hex}.json"
        spec = json.dumps({"argv": list(argv), "timeout": timeout, "path": path})
        launch = {"command": ["python3", "-I", "-c", self.LAUNCH_PY, self.SUPERVISE_PY, spec],
                  "timeout": "60s", "envvars": body.get("envvars", {})}
        r = json.loads(self.http("POST", "/process", json.dumps(launch).encode(), timeout=120))
        if r.get("exitCode") != 0:
            return {"exitCode": -1, "error": f"launch failed: {r.get('error', '')} {r.get('stderr', '')[-300:]}"}
        deadline = time.time() + timeout + 120
        check = ("import json,os,sys; p=sys.argv[1]; "
                 "print(open(p).read() if os.path.exists(p) else '')")
        while time.time() < deadline:
            time.sleep(every)
            try:
                got = self.run_in_actor(["python3", "-I", "-c", check, path], timeout=60).strip()
            except (RuntimeError, urllib.error.URLError, OSError) as e:   # one lost poll isn't the call
                self.events.append({"actor": self.actor, "poll_error": repr(e)[:200]})
                self.start_proxy()
                continue
            if got:
                self.run_in_actor(["rm", "-f", path], timeout=30)
                return json.loads(got)
        return {"exitCode": -1, "error": f"no result after {timeout + 120}s of polling"}

    def run_in_actor(self, argv: list[str], timeout: int = 120) -> str:
        """A command in the actor without replacing /work; any failure raises."""
        body = {"command": list(argv), "timeout": f"{timeout}s"}
        out = json.loads(self.http("POST", "/process", json.dumps(body).encode(), timeout=timeout + 60))
        if out.get("exitCode") != 0 or out.get("error"):
            raise RuntimeError(f"{argv[:2]} failed: exit {out.get('exitCode')} {out.get('error', '')} {out.get('stderr', '')[-300:]}")
        return out.get("stdout", "")

    def connection(self):
        """A keep-alive connection through the tunnel, for timings that mustn't include opening a
        new tunnel stream (round 9 shakeout: up to ~390 ms per new connection)."""
        import http.client
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)

    def timed_get(self, conn, path: str) -> float:
        """Seconds for one GET on an already-open connection; raises if the connection broke."""
        t0 = time.perf_counter()
        conn.request("GET", path, headers={"ate-target-actor": f"{self.atespace}/{self.actor}"})
        r = conn.getresponse()
        r.read()
        if r.status != 200:
            raise RuntimeError(f"GET {path}: HTTP {r.status}")
        return round(time.perf_counter() - t0, 4)

    def post_state(self, path: str, payload: dict) -> dict:
        return json.loads(self.http("POST", f"/state/{path}", json.dumps(payload).encode(), timeout=60))

    def suspend(self, keep_proxy: bool = False) -> float:
        """Suspend and wait for the snapshot to reach storage. Returns seconds. keep_proxy leaves
        the router tunnel up, so the next request can wake the actor (request parking)."""
        if not keep_proxy:
            self.stop_proxy()
        t0 = time.time()
        self.ate("suspend", "actor", self.actor, "-a", self.atespace, "--mode=sync", timeout=900)
        s = round(time.time() - t0, 1)
        self.events.append({"actor": self.actor, "suspend_sync_s": s})
        return s

    def resume(self) -> float:
        """Resume and wait until the actor's server answers. Returns seconds."""
        t0 = time.time()
        self.ate("resume", "actor", self.actor, "-a", self.atespace, timeout=600)
        self.start_proxy()
        self.wait_healthy()
        s = round(time.time() - t0, 1)
        self.events.append({"actor": self.actor, "resume_to_healthy_s": s})
        return s

    def sidecar_log(self) -> list[dict]:
        return []

    def leftover(self) -> list[str]:
        r = self.ate("get", "actor", self.actor, "-a", self.atespace, check=False)
        return [self.actor] if r.returncode == 0 else []

    def __exit__(self, *exc):
        self.stop_proxy()
        if self.created:
            r = self.ate("delete", "actor", self.actor, "-a", self.atespace, check=False, timeout=300)
            if r.returncode != 0:
                self.events.append({"delete_failed": self.actor, "stderr": r.stderr.decode(errors="replace")[-300:]})
        left = self.leftover() if self.created else []
        if left:
            self.events.append({"leftover_actors": left})
