"""The agent sandbox, local edition: what the model runs inside, never on the host.

Two containers on a Docker network with no route out (`--internal`):

- **sidecar** (agent/sidecar.py): the only path anywhere. Egress proxy allowing exactly the
  hosts named (by default aiplatform.googleapis.com) over HTTPS and nothing else; a metadata
  server handing out short-lived lab-agent access tokens minted on the host. Its way out is a
  second network of its own, not Docker's default bridge: on the bridge, any other container on
  the host (the forge, for one) could reach its proxy and its token endpoint (free checks,
  2026-11-20, reproduced). Runs as its own uid with no capabilities at all; the low ports come
  from a namespaced sysctl, not CAP_NET_BIND_SERVICE.
- **agent**: the pinned python image, agy mounted read-only, running as its own unprivileged uid
  (distinct from the sidecar's and from the judge's child, 65534) with
  a read-only root filesystem, tmpfs for /tmp and HOME, no capabilities, no privilege escalation,
  PID and memory limits. Only the workspace is mounted (read-write). No credentials file, no host
  home, no Docker socket, no cases, keys, validator, records or forge credentials.

This is a local credential shim and a hostname allowlist, not GKE Workload Identity or a network
policy (build review 4): the GKE rungs test the real ones. Locally it's built by hand, which is
itself a line in the operations ledger.

usage (library): with Sandbox(token_dir) as sb: sb.run(workspace, ["agy", ...])
"""

from __future__ import annotations

import os
import subprocess
import uuid

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGE = "python@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91"
AGY = os.path.join(TOOLS, "agent", "vendor", "agy-1.5.13", "x", "antigravity")
SIDECAR = os.path.join(TOOLS, "agent", "sidecar.py")
AGENT_UID = 10001      # the agent; the judge's child is 65534 (subject/runner.py)
SIDECAR_UID = 10002    # the sidecar
# The lab project, from harness/lab_env.sh. Empty outside a lab shell, where the billing guard
# (controller/models.py) refuses every model call anyway.
PROJECT = os.environ.get("LAB_PROJECT", "")


def check_token_dir(d: str):
    """The token directory holds a bearer token: owned by this user, mode 0700, no symlinks, and
    nothing in it but token.json (0600) and the minter's temp file (build review 4)."""
    st = os.lstat(d)
    if not os.path.isdir(d) or os.path.islink(d) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RuntimeError(f"token dir {d} must be a real directory owned by you with mode 0700")
    for name in os.listdir(d):
        p = os.path.join(d, name)
        fs = os.lstat(p)
        if name not in ("token.json", "token.json.tmp") or not os.path.isfile(p) or os.path.islink(p) \
                or fs.st_uid != os.getuid() or fs.st_mode & 0o077:
            raise RuntimeError(f"unexpected or exposed file in token dir: {name}")


def docker(*args, check=True, capture=True):
    return subprocess.run(["docker", *args], check=check, capture_output=capture, text=True)


class Sandbox:
    def __init__(self, token_dir: str, egress_allow: str | None = None):
        self.token_dir = token_dir
        self.egress_allow = egress_allow
        tag = uuid.uuid4().hex[:8]
        self.net = f"lab-agent-net-{tag}"
        self.sidecar = f"lab-sidecar-{tag}"
        self.egress_net = f"lab-egress-net-{tag}"     # the sidecar's way out, shared with nothing

    def __enter__(self):
        check_token_dir(self.token_dir)
        docker("network", "create", "--internal", self.net)
        docker("network", "create", self.egress_net)
        env = ["-e", f"LAB_PROJECT={PROJECT}"]
        if self.egress_allow:
            env += ["-e", f"LAB_EGRESS_ALLOW={self.egress_allow}"]
        docker("run", "-d", "--name", self.sidecar, "--network", self.net,
               "--read-only", "--user", f"{SIDECAR_UID}:{SIDECAR_UID}", "--cap-drop", "ALL",
               "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
               "--security-opt", "no-new-privileges", "--pids-limit", "64", "--memory", "256m",
               "-v", f"{SIDECAR}:/sidecar.py:ro", "-v", f"{self.token_dir}:/tokens:ro", *env,
               IMAGE, "python", "-u", "-I", "/sidecar.py")
        docker("network", "connect", self.egress_net, self.sidecar)
        self.sidecar_ip = docker("inspect", "-f",
                                 f'{{{{(index .NetworkSettings.Networks "{self.net}").IPAddress}}}}',
                                 self.sidecar).stdout.strip()
        return self

    def run(self, workspace: str, argv: list[str], timeout: int = 1800, extra_env: dict | None = None,
            extra_mounts: list[str] | None = None) -> subprocess.CompletedProcess:
        name = f"lab-agent-{uuid.uuid4().hex[:8]}"
        proxy = f"http://{self.sidecar}:3128"
        env = {"HOME": "/home/agent", "HTTPS_PROXY": proxy, "https_proxy": proxy,
               "HTTP_PROXY": proxy, "http_proxy": proxy,
               "NO_PROXY": f"{self.sidecar},metadata.google.internal,169.254.169.254",
               "no_proxy": f"{self.sidecar},metadata.google.internal,169.254.169.254",
               "GCE_METADATA_HOST": self.sidecar, "GCE_METADATA_IP": self.sidecar_ip,
               "AGY_ADC_AUTH": "true", "GOOGLE_CLOUD_QUOTA_PROJECT": PROJECT,
               "GOOGLE_CLOUD_PROJECT": PROJECT, "GOOGLE_CLOUD_LOCATION": "global",
               "PYTHONDONTWRITEBYTECODE": "1", **(extra_env or {})}
        cmd = ["docker", "run", "--rm", "--name", name, "--network", self.net,
               "--add-host", f"metadata.google.internal:{self.sidecar_ip}",
               "--read-only", "--tmpfs", "/tmp:size=256m", "--tmpfs", f"/home/agent:size=512m,uid={AGENT_UID},gid={AGENT_UID}",
               "--user", f"{AGENT_UID}:{AGENT_UID}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--pids-limit", "256", "--memory", "2g", "--cpus", "2",
               "-v", f"{AGY}:/usr/local/bin/agy:ro", "-v", f"{workspace}:/work", "-w", "/work"]
        for m in extra_mounts or []:
            cmd += ["-v", m]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [IMAGE, *argv]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            docker("kill", name, check=False)
            raise

    def sidecar_log(self) -> list[dict]:
        import json
        out = []
        for line in docker("logs", self.sidecar, check=False).stdout.splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def __exit__(self, *exc):
        docker("rm", "-f", self.sidecar, check=False)
        docker("network", "rm", self.net, check=False)
        docker("network", "rm", self.egress_net, check=False)
