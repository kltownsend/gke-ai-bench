"""The agent sandbox, Agent Executor edition (AX v0.4.0-rc.1 on Substrate v1.0.1): the venue interface
of agent/pod_sandbox.py, over AX's documented primitives.

One agy call is one AX Task. The controller stays on the workstation (arm F); Google's Antigravity harness
runs agy inside the Task, declared as the wrapper:
- **In:** AX's only documented input is a Workspace that git-clones a repository. The workspace
  is committed on the workstation into a fresh bare repository, served by git's static ("dumb") HTTP
  protocol from a plain file server Pod in the cluster (infra/executor/forge.yaml); the Task's
  Workspace clones it at the root (`dir: "."`), through a Gateway rule naming only that host.
- **The call:** the agy argv the controller builds is translated into the harness's `spec.config`
  (camelCase keys become flags): `model`, `settings` (the lab's allow-rule), `outputFormat`, and
  `dangerouslySkipPermissions: false` so agy's permissions match the other rungs. The prompt is
  the Task's goal; agy's stdout comes back as `status.outcome.output`.
- **Start:** `ax apply` creates a Task Suspended; `ax resume task` runs it (AX docs, concepts.md).
- **Out:** the documented `ax ssh <task> -- ...` (the Task's debug services on): every change since
  the clone, untracked files included, as a binary patch applied on the workstation; lab-hold builds it in
  the Task as the Task's user (`ax ssh` is a root that can't write the clone). AX's own `.ax/`
  directory is excluded. AX rc.1 parks a Task the moment its command exits and refuses `ax ssh`
  on a Completed Task, so the image's `lab-hold` keeps the finished command waiting (it writes
  .ax/lab-done) until the controller has collected and written .ax/lab-collected; the command
  then exits with the agent's own code (.ax/lab-release/collected) (infra/executor/image/lab-hold).

usage (library): with ExecutorVenue() as sb: sb.run(workspace, ["agy", ...])
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid

from pod_sandbox import unpack_replace  # noqa: F401  (same module family; not used for transfer here)

ATESPACE = "default"
GATEWAY = "lab-agent-gw"
FORGE_NS, FORGE_POD = "lab-forge", "forge"
FORGE_URL = "http://forge.lab-forge.svc.cluster.local:8080"
TERMINAL = ("Completed", "Failed", "Cancelled")


def git(*args, cwd=None, input=None):
    env = dict(os.environ, GIT_AUTHOR_NAME="lab", GIT_AUTHOR_EMAIL="lab@invalid",
               GIT_COMMITTER_NAME="lab", GIT_COMMITTER_EMAIL="lab@invalid", GIT_CONFIG_GLOBAL="/dev/null")
    return subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True, check=True, env=env)


# Anything that isn't an agy call (the probes) runs as the Task's own command, AX's documented
# `spec.commands` path, after the same placeholder shim the harness wrapper starts, so a probe sees
# what agy would.
SHIM_THEN = ["sh", "-c", "python3 -I /opt/lab/lab_shim.py >/dev/null 2>&1 & "
             "for i in $(seq 1 50); do curl -s -o /dev/null http://127.0.0.1:8081/ && break; sleep 0.1; done; "
             '"$@"; exec /usr/local/bin/lab-hold $?', "lab-shim"]
DONE, COLLECTED, PATCH = "/workspace/.ax/lab-done", "/workspace/.ax/lab-release/collected", "/workspace/.ax/lab-patch"


def argv_to_config(argv: list[str]) -> tuple[str, dict]:
    """The controller's agy argv as the harness's goal and spec.config."""
    if not argv or os.path.basename(argv[0]) != "agy":
        raise ValueError("not an agy call")
    cfg, goal, i = {"dangerouslySkipPermissions": False}, None, 1
    while i < len(argv):
        a = argv[i]
        if a in ("-p", "--print", "--prompt"):
            goal, i = argv[i + 1], i + 2
        elif a == "--model":
            cfg["model"], i = argv[i + 1], i + 2
        elif a == "--settings":
            cfg["settings"], i = json.loads(argv[i + 1]), i + 2
        elif a == "--output-format":
            cfg["outputFormat"], i = argv[i + 1], i + 2
        else:
            raise ValueError(f"unmapped agy argument {a!r}")
    if goal is None:
        raise ValueError("no prompt in the agy argv")
    return goal, cfg


def parse_describe(text: str) -> dict:
    state = re.search(r"^State:\s+(\S+)", text, re.M)
    outcome = re.search(r"^Outcome:\s+(\S+) \(exitCode=(-?\d+)\)", text, re.M)
    out = ""
    m = re.search(r"^Output:\n((?:  .*\n?)*)", text, re.M)
    if m:
        out = "\n".join(line[2:] for line in m.group(1).rstrip("\n").split("\n"))
    return {"state": state.group(1) if state else "", "result": outcome.group(1) if outcome else "",
            "exit": int(outcome.group(2)) if outcome else None, "output": out}


class ExecutorVenue:
    profile = "executor"

    def __init__(self, image: str | None = None, atespace: str = ATESPACE, gateway: str = GATEWAY,
                 context: str | None = None, task_timeout: int = 2400):
        self.image = image or os.environ.get("LAB_EXECUTOR_IMAGE", "")
        if "@sha256:" not in self.image:
            raise ValueError("the Executor agent image must be pinned by digest (LAB_EXECUTOR_IMAGE)")
        self.atespace, self.gateway, self.context = atespace, gateway, context
        self.task_timeout = task_timeout
        self.session = uuid.uuid4().hex[:12]
        self.events: list[dict] = []
        self.created: list[tuple[str, str]] = []

    # -- the CLIs ------------------------------------------------------------------------------
    def ax(self, *args, input=None, check=True, timeout=120):
        cmd = ["ax", *(["--context", self.context] if self.context else []), "-a", self.atespace, *args]
        return subprocess.run(cmd, input=input, capture_output=True, check=check, timeout=timeout)

    def kubectl(self, *args, input=None, check=True, timeout=120):
        cmd = ["kubectl", *(["--context", self.context] if self.context else []), *args]
        return subprocess.run(cmd, input=input, capture_output=True, check=check, timeout=timeout)

    # -- in: the workspace as a served repository ----------------------------------------------
    def publish(self, workspace: str, name: str) -> str:
        """Commit the workspace into a fresh bare repo `<name>.git`, make it servable over dumb
        HTTP, and copy it into the forge Pod. Returns the base commit."""
        tmp = tempfile.mkdtemp(prefix="lab-ax-")
        try:
            work, bare = os.path.join(tmp, "w"), os.path.join(tmp, f"{name}.git")
            shutil.copytree(workspace, work, symlinks=True)
            git("init", "-q", "-b", "main", cwd=work)
            git("add", "-A", "-f", cwd=work)
            git("commit", "-q", "--allow-empty", "-m", "lab: workspace for one agy call", cwd=work)
            base = git("rev-parse", "HEAD", cwd=work).stdout.decode().strip()
            git("clone", "-q", "--bare", work, bare)
            git("update-server-info", cwd=bare)
            tarball = subprocess.run(["tar", "-c", "-C", tmp, f"{name}.git"], capture_output=True, check=True).stdout
            self.kubectl("-n", FORGE_NS, "exec", "-i", FORGE_POD, "--", "tar", "-x", "-C", "/srv", input=tarball)
            return base
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def unpublish(self, name: str):
        self.kubectl("-n", FORGE_NS, "exec", FORGE_POD, "--", "rm", "-rf", f"/srv/{name}.git", check=False)

    # -- the call -------------------------------------------------------------------------------
    def specs(self, name: str, goal: str | None, cfg: dict | None, commands: list[str] | None = None,
              base: str = "") -> str:
        workspace = {"apiVersion": "ax.io/v1alpha1", "kind": "Workspace",
                     "metadata": {"name": name, "atespace": self.atespace, "labels": {"lab-session": self.session}},
                     "spec": {"setup": {"git": [{"name": "work", "repo": f"{FORGE_URL}/{name}.git", "dir": ".",
                                                 "ref": "main"}],   # no depth: dumb HTTP can't fetch shallow
                                       "gateway": {"name": self.gateway}}}}   # the clone's own egress
        task = {"apiVersion": "ax.io/v1alpha1", "kind": "Task",
                "metadata": {"name": name, "atespace": self.atespace, "labels": {"lab-session": self.session}},
                "spec": {"profile": {"image": self.image, "resources": {"limits": {"cpu": "2", "memory": "4Gi"}}},
                         "env": [{"name": k, "value": v} for k, v in (
                             ("GCE_METADATA_HOST", "127.0.0.1:8081"), ("AGY_ADC_AUTH", "true"),
                             ("GOOGLE_CLOUD_QUOTA_PROJECT", os.environ.get("LAB_PROJECT", "")),
                             ("GOOGLE_CLOUD_PROJECT", os.environ.get("LAB_PROJECT", "")),
                             ("LAB_AGENT", os.environ.get("LAB_AGENT", "")),
                             ("GOOGLE_CLOUD_LOCATION", "global"), ("PYTHONDONTWRITEBYTECODE", "1"),
                             ("LAB_BASE", base),
                             # AX makes HOME the workspace when it drops to uid 1000 unless the Task
                             # sets HOME (runner/pull.go); agy keeps ~/.gemini there, which reads as
                             # workspace edits. Every other rung keeps the agent's home outside it.
                             ("HOME", "/tmp"))],
                         "workspace": {"name": name}, "gateway": {"name": self.gateway},
                         "debug": True,
                         **({"commands": commands} if commands else {"goal": {"text": goal}, "config": cfg})}}
        return json.dumps(workspace) + "\n---\n" + json.dumps(task)

    def wait(self, name: str) -> dict:
        deadline, d = time.time() + self.task_timeout, {}
        while time.time() < deadline:
            d = parse_describe(self.ax("describe", "task", name).stdout.decode(errors="replace"))
            if d["state"] in TERMINAL:
                return d
            time.sleep(5)
        raise RuntimeError(f"task {name} not finished within {self.task_timeout}s (last state {d.get('state')!r})")

    def ssh(self, name: str, script: str, check=True, timeout=120):
        return self.ax("ssh", name, "--", "sh", "-c", script, check=check, timeout=timeout)

    def wait_done(self, name: str) -> str:
        """Until the command has finished and lab-hold is waiting; the Task is still Working."""
        deadline = time.time() + self.task_timeout
        while time.time() < deadline:
            r = self.ssh(name, f"cat {DONE} 2>/dev/null", check=False, timeout=60)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.decode().strip()
            d = parse_describe(self.ax("describe", "task", name).stdout.decode(errors="replace"))
            if d["state"] in TERMINAL:
                raise RuntimeError(f"task {name} ended ({d['state']}) before its workspace was collected")
            time.sleep(5)
        raise RuntimeError(f"task {name} not finished within {self.task_timeout}s")

    def release(self, name: str, tries: int = 3) -> bool:
        """Let lab-hold exit. The patch is already in hand, so a failed release is recorded, not
        fatal: the hold ends on its own timeout (LAB_HOLD_S) and wait() sees the outcome then."""
        for i in range(tries):
            r = self.ssh(name, f"touch {COLLECTED}", check=False, timeout=60)
            if r.returncode == 0:
                return True
            self.events.append({"task": name, "release_failed": i + 1, "rc": r.returncode,
                                "stderr": r.stderr.decode(errors="replace")[-300:]})
            time.sleep(3)
        return False

    def collect(self, name: str, base: str, workspace: str):
        """Every change since the clone, as a binary patch, applied to the workstation's workspace."""
        # The Task built the patch as its own user (lab-hold); `ax ssh` runs as a root that can't
        # write the clone, so the controller only reads.
        script = (f"if [ -e {PATCH}.ok ]; then cat {PATCH}; "
                  f"else cat {PATCH}.err >&2; exit 3; fi")
        patch = self.ssh(name, script, timeout=300).stdout
        if patch.strip():
            subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=workspace, input=patch,
                           capture_output=True, check=True)
        return len(patch)

    def run(self, workspace: str, argv: list[str], timeout: int = 1800, extra_env: dict | None = None,
            extra_mounts: list[str] | None = None) -> subprocess.CompletedProcess:
        if extra_mounts or extra_env:
            raise ValueError("the Executor venue takes no extra mounts or env; the Task spec is the input")
        if argv and os.path.basename(argv[0]) == "agy":
            goal, cfg, commands = *argv_to_config(argv), None
        else:
            goal, cfg, commands = None, None, SHIM_THEN + list(argv)
        name = f"lab-{self.session}-{uuid.uuid4().hex[:6]}"
        t0 = time.time()
        base = self.publish(workspace, name)
        try:
            self.ax("apply", "-f", "-", input=self.specs(name, goal, cfg, commands, base).encode())
            self.created.append(("task", name))
            self.ax("resume", "task", name, timeout=600)
            started = time.time()
            self.wait_done(name)
            ran = time.time()
            patch_bytes = self.collect(name, base, workspace)
            released = self.release(name)
            d = self.wait(name)
            code = d["exit"] if d["exit"] is not None else (0 if d["result"] == "Succeeded" else 1)
            self.events.append({"task": name, "state": d["state"], "result": d["result"], "exit": code,
                                "start_s": round(started - t0, 1), "task_s": round(ran - t0, 1), "total_s": round(time.time() - t0, 1),
                                "patch_bytes": patch_bytes, "released": released})
            return subprocess.CompletedProcess(argv, code, d["output"], "")
        finally:
            self.ax("delete", "task", name, check=False)
            self.ax("delete", "workspace", name, check=False)
            self.unpublish(name)

    def suspend(self, name: str) -> float:
        t0 = time.time()
        self.ax("suspend", "task", name, timeout=900)
        return round(time.time() - t0, 1)

    def resume(self, name: str) -> float:
        t0 = time.time()
        self.ax("resume", "task", name, timeout=600)
        return round(time.time() - t0, 1)

    def sidecar_log(self) -> list[dict]:
        return []

    def leftover(self) -> list[str]:
        out = self.ax("get", "tasks", check=False).stdout.decode(errors="replace")
        return [line.split()[0] for line in out.splitlines() if f"lab-{self.session}-" in line]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for name in self.leftover():
            self.ax("delete", "task", name, check=False)
            self.ax("delete", "workspace", name, check=False)
        left = self.leftover()
        if left:
            self.events.append({"leftover_tasks": left})
