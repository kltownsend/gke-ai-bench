"""The agent sandbox, GKE edition: the same interface as agent/sandbox.py, a Pod instead of Docker.

The controller and the judge stay on the workstation, as on the local rung. Only the agent moves: each
agy call runs in a fresh Pod, created for that call and deleted after it, the way the local
sandbox runs a fresh container per call. The workspace goes in and comes back as a tar stream
over `kubectl exec`; the copy that comes back replaces the local workspace wholesale and atomically
(deletions included), within byte, member, depth and path quotas, and the controller's own checks decide what the agent changed.

What's different from the local sandbox, on purpose: there is no sidecar and no token shim. The
plain GKE rung measures the platform's own controls, so credentials come from whatever the
cluster gives a Pod (Workload Identity on hardened, the node's service account on default and
vulnerable) and egress is whatever the cluster enforces (a NetworkPolicy on hardened, nothing
on the others). `sidecar_log()` returns [] here; the rung's reach evidence comes from the probes,
not from a proxy we wrote.

Profiles set the Pod spec, not the cluster (infra/cluster.sh sets the cluster):
- hardened: non-root uid 10001, read-only root, no capabilities, no privilege escalation,
  RuntimeDefault seccomp, no service-account token mounted, the `lab-agent` Kubernetes service
  account (bound to the lab-agent Google service account by Workload Identity), CPU and memory
  limits matching the local sandbox.
- default: the Pod a team writes without thinking about it, plus the one added step: it runs as
  the `lab-agent` Kubernetes service account, which Workload Identity binds to lab-agent. Still the
  image's user (root), no security context, no limits.
- vulnerable: the plain Pod, nothing else, on the node's identity.

usage (library): with PodSandbox("hardened") as sb: sb.run(workspace, ["agy", ...])
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

NAMESPACE = "lab-agent"
AGENT_UID = 10001
# The lab project, from harness/lab_env.sh. Empty outside a lab shell, where the billing guard
# (controller/models.py) refuses every model call anyway.
PROJECT = os.environ.get("LAB_PROJECT", "")
PROFILES = ("hardened", "default", "vulnerable")

# The return channel's quotas (build review 5): the stream comes from a Pod the agent controls.
MAX_TAR_BYTES = 256 * 1024 * 1024        # the raw stream
MAX_MEMBERS = 20000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024      # expanded
MAX_DEPTH = 32
MAX_NAME = 1024
TRANSFER_S = 300


def pod_spec(name: str, profile: str, image: str, session: str = "", drop: tuple = ()) -> dict:
    """The Pod for one agy call. It idles until the controller execs into it.
    `drop` (diagnosis only) removes named hardened restrictions one at a time: "seccomp",
    "nnp" (allowPrivilegeEscalation stays default/true), "rorootfs", "nonroot"."""
    if profile not in PROFILES:
        raise ValueError(profile)
    c = {"name": "agent", "image": image, "command": ["sleep", "infinity"], "workingDir": "/work",
         "env": [{"name": "HOME", "value": "/home/agent"},
                 {"name": "AGY_ADC_AUTH", "value": "true"},
                 {"name": "GOOGLE_CLOUD_QUOTA_PROJECT", "value": PROJECT},
                 {"name": "GOOGLE_CLOUD_PROJECT", "value": PROJECT},
                 {"name": "GOOGLE_CLOUD_LOCATION", "value": "global"},
                 {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
         "volumeMounts": [{"name": "work", "mountPath": "/work"},
                          {"name": "home", "mountPath": "/home/agent"},
                          {"name": "tmp", "mountPath": "/tmp"}]}
    spec = {"restartPolicy": "Never", "containers": [c],
            "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "1Gi"}},
                        {"name": "home", "emptyDir": {"sizeLimit": "512Mi"}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}]}
    if profile == "hardened":
        spec["serviceAccountName"] = "lab-agent"
        spec["automountServiceAccountToken"] = False
        psc = {"runAsNonRoot": True, "runAsUser": AGENT_UID, "runAsGroup": AGENT_UID,
               "fsGroup": AGENT_UID, "seccompProfile": {"type": "RuntimeDefault"}}
        csc = {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
               "capabilities": {"drop": ["ALL"]}}
        if "seccomp" in drop:
            psc["seccompProfile"] = {"type": "Unconfined"}
        if "nnp" in drop:
            csc["allowPrivilegeEscalation"] = True
        if "rorootfs" in drop:
            csc["readOnlyRootFilesystem"] = False
        if "nonroot" in drop:
            psc = {"runAsUser": AGENT_UID, "runAsGroup": AGENT_UID, "fsGroup": AGENT_UID,
                   "seccompProfile": psc["seccompProfile"]}   # keep uid, drop runAsNonRoot gate
        spec["securityContext"] = psc
        c["securityContext"] = csc
        c["resources"] = {"requests": {"cpu": "1", "memory": "1Gi"},
                          "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "2Gi"}}
    if profile == "default":
        # The one step added to the minimum compatible cluster: the Pod runs as the Workload
        # Identity service account. Otherwise the plain Pod a team writes (still root, no limits).
        spec["serviceAccountName"] = "lab-agent"
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": NAMESPACE,
                         "labels": {"app": "lab-agent", "lab-profile": profile, "lab-session": session}},
            "spec": spec}


def bounded_run(cmd, check=True, timeout=None):
    """subprocess.run for a command whose stdout is a workspace: spooled to a file and cut off past
    MAX_TAR_BYTES, so a workspace the agent filled can't exhaust the workstation's memory before the
    tar check runs."""
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        p = subprocess.Popen(cmd, stdout=fo, stderr=fe)
        deadline = time.monotonic() + timeout if timeout else None
        while p.poll() is None:
            if os.fstat(fo.fileno()).st_size > MAX_TAR_BYTES:
                p.kill(); p.wait()
                raise ValueError(f"tar stream over {MAX_TAR_BYTES} bytes")
            if deadline and time.monotonic() > deadline:
                p.kill(); p.wait()
                raise subprocess.TimeoutExpired(cmd, timeout)
            time.sleep(0.1)
        if os.fstat(fo.fileno()).st_size > MAX_TAR_BYTES:
            raise ValueError(f"tar stream over {MAX_TAR_BYTES} bytes")
        fo.seek(0); fe.seek(0)
        r = subprocess.CompletedProcess(cmd, p.returncode, fo.read(), fe.read()[-20000:])
    if check:
        r.check_returncode()
    return r


def pack(root: str) -> bytes:
    """The workspace as a tar stream, paths relative to it. Symlinks travel as symlinks."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in sorted(os.listdir(root)):
            tf.add(os.path.join(root, name), arcname=name)
    return buf.getvalue()


def unpack_replace(data: bytes, root: str):
    """Replace root's contents with the tar stream's, all or nothing. The stream comes from the
    agent's Pod and is untrusted: members that would land outside root, absolute or escaping
    symlinks, hard links, devices and fifos, duplicate paths, and anything over the quotas are
    refused before anything is written. Modes are normalized. The old tree is moved aside, the new
    one moved in, and the old one restored if that fails."""
    if len(data) > MAX_TAR_BYTES:
        raise ValueError(f"tar stream over {MAX_TAR_BYTES} bytes")
    parent = os.path.dirname(os.path.abspath(root))
    stage = tempfile.mkdtemp(prefix=".pod-ws-", dir=parent)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
            members, seen, total = [], set(), 0
            for m in tf:
                if len(members) >= MAX_MEMBERS:
                    raise ValueError(f"over {MAX_MEMBERS} members")
                p = os.path.normpath(m.name)
                if p in (".", ""):
                    continue
                if p.startswith("..") or os.path.isabs(p) or not (m.isreg() or m.isdir() or m.issym()):
                    raise ValueError(f"refused tar member {m.name!r}")
                if len(p) > MAX_NAME or p.count("/") >= MAX_DEPTH:
                    raise ValueError(f"refused path {m.name[:80]!r}: too long or too deep")
                if p in seen:
                    raise ValueError(f"duplicate path {p!r}")
                seen.add(p)
                if m.issym() and (os.path.isabs(m.linkname) or
                                  os.path.normpath(os.path.join(os.path.dirname(p), m.linkname)).startswith("..")):
                    raise ValueError(f"refused symlink {m.name!r} -> {m.linkname!r}")
                if m.isreg():
                    if m.size > MAX_FILE_BYTES:
                        raise ValueError(f"{p!r} over {MAX_FILE_BYTES} bytes")
                    total += m.size
                    if total > MAX_TOTAL_BYTES:
                        raise ValueError(f"expanded size over {MAX_TOTAL_BYTES} bytes")
                # Modes are normalized: the Pod can't hand the workstation an unreadable or unremovable
                # tree, a setuid bit or a world-writable file.
                m.mode = 0o755 if m.isdir() or (m.isreg() and m.mode & 0o100) else 0o644
                m.name = p
                members.append(m)
            new = os.path.join(stage, "new")
            os.mkdir(new)
            tf.extractall(new, members=members, filter="tar")
        old = os.path.join(stage, "old")
        os.rename(root, old)
        try:
            os.rename(new, root)
        except BaseException:
            os.rename(old, root)
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


class PodSandbox:
    def __init__(self, profile: str, image: str | None = None, context: str | None = None,
                 ready_timeout: int = 180, drop: tuple = ()):
        if profile not in PROFILES:
            raise ValueError(profile)
        self.profile = profile
        self.image = image or os.environ["LAB_AGENT_IMAGE"]      # pinned by digest
        if "@sha256:" not in self.image:
            raise ValueError("the agent image must be pinned by digest")
        self.context = context
        self.ready_timeout = ready_timeout
        self.events: list[dict] = []
        self.session = uuid.uuid4().hex[:12]     # this sandbox's own Pods, and only those
        self.drop = drop

    def kubectl(self, *args, input=None, check=True, timeout=None):
        cmd = ["kubectl", *(["--context", self.context] if self.context else []), "-n", NAMESPACE, *args]
        if args and args[0] == "exec" and input is None:      # a download: bounded on the way in
            return bounded_run(cmd, check=check, timeout=timeout)
        return subprocess.run(cmd, input=input, capture_output=True, check=check, timeout=timeout)

    def __enter__(self):
        return self

    def run(self, workspace: str, argv: list[str], timeout: int = 1800, extra_env: dict | None = None,
            extra_mounts: list[str] | None = None) -> subprocess.CompletedProcess:
        if extra_mounts:
            raise ValueError("extra mounts aren't supported on GKE; the workspace is the only input")
        name = f"lab-agent-{uuid.uuid4().hex[:8]}"
        spec = pod_spec(name, self.profile, self.image, self.session, self.drop)
        for k, v in (extra_env or {}).items():
            spec["spec"]["containers"][0]["env"].append({"name": k, "value": str(v)})
        t0 = time.time()
        self.kubectl("apply", "-f", "-", input=json.dumps(spec).encode())
        try:
            self.kubectl("wait", "--for=condition=Ready", f"pod/{name}", f"--timeout={self.ready_timeout}s",
                         timeout=self.ready_timeout + 30)
            ready = time.time()
            self.transfer(name, ["tar", "-x", "-C", "/work"], pack(workspace))
            p = self.kubectl("exec", name, "--", *argv, check=False, timeout=timeout)
            out = self.transfer(name, ["tar", "-c", "-C", "/work", "."])
            unpack_replace(out, workspace)
            self.events.append({"pod": name, "profile": self.profile, "schedule_s": round(ready - t0, 1),
                                "total_s": round(time.time() - t0, 1), "exit": p.returncode})
            return subprocess.CompletedProcess(argv, p.returncode, p.stdout.decode(errors="replace"),
                                               p.stderr.decode(errors="replace"))
        finally:
            self.delete(f"pod/{name}")

    def transfer(self, name, argv, data=None):
        """One workspace transfer over kubectl exec, bounded. A failure is retried once, and
        kubectl's stderr is kept either way (2026-11-21: a hardened probe run lost its only clue
        to an unrecorded exit 1)."""
        for attempt in (1, 2):
            r = self.kubectl("exec", *(["-i"] if data is not None else []), name, "--", *argv,
                             input=data, check=False, timeout=TRANSFER_S)
            if r.returncode == 0:
                return r.stdout
            self.events.append({"transfer_failed": " ".join(argv), "pod": name, "attempt": attempt,
                                "exit": r.returncode, "stderr": r.stderr.decode(errors="replace")[-500:]})
            time.sleep(3)
        raise RuntimeError(f"workspace transfer failed twice: {self.events[-1]}")

    def delete(self, target):
        """Delete and wait, bounded; a Pod that won't go is recorded, not assumed gone."""
        r = self.kubectl("delete", target, "--grace-period=0", "--force", "--wait=true",
                         "--timeout=60s", "--ignore-not-found", check=False, timeout=90)
        if r.returncode != 0:
            self.events.append({"delete_failed": target, "stderr": r.stderr.decode(errors="replace")[-300:]})
            ctx = f" --context {self.context}" if self.context else ""
            print(f"WARNING: couldn't delete {target}; it may still bill. Clean up with: "
                  f"kubectl{ctx} -n {NAMESPACE} delete {target} --grace-period=0 --force", file=sys.stderr, flush=True)

    def sidecar_log(self) -> list[dict]:
        return []

    def leftover(self) -> list[str]:
        r = self.kubectl("get", "pods", "-l", f"lab-session={self.session}", "-o", "name", check=False)
        return r.stdout.decode().split()

    def __exit__(self, *exc):
        for pod in self.leftover():       # this session's Pods only; concurrent runs are untouched
            self.delete(pod)
        left = self.leftover()
        if left:
            self.events.append({"leftover_pods": left})
