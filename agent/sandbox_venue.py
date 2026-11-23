"""The agent sandbox, Agent Sandbox edition (v1.0.6): the same interface as agent/pod_sandbox.py,
with each agy call in a fresh sandbox claimed from a warm pool, the how-to's production path.

One call is one SandboxClaim against a warm pool (infra/k8s/sandbox.yaml): the controller hands it a
pre-warmed Sandbox and its Pod, the pool replenishes, and deleting the claim retires that sandbox, so
no call sees another's. The workspace goes in and out with the Pod harness's bounded tar transfer
over `kubectl exec`.

Two disclosed differences from the documented path:
- **The interaction is `kubectl exec`.** The documented client (`k8s-agent-sandbox`) talks to the
  sandbox through the sandbox router; exec isn't documented. The lab uses exec because the agent
  runs inside the sandbox and only the workspace crosses the boundary, as on every other rung, so the
  transfer stays the same apparatus everywhere. The router path isn't exercised.
- **Templates are the lab's,** in the how-to's shape: `documented` (the example's settings, default
  network policy) and `agent` (plus Workload Identity and DNS and metadata egress, counted steps).

Variants (the `profile` names the harnesses pass): "sandbox-documented" and "sandbox".
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid

from pod_sandbox import PodSandbox, TRANSFER_S, pack, unpack_replace  # noqa: F401

POOLS = {"sandbox-documented": "lab-documented-pool", "sandbox": "lab-agent-pool"}


class AgentSandboxVenue(PodSandbox):
    def __init__(self, profile: str, image: str | None = None, context: str | None = None,
                 ready_timeout: int = 300):
        if profile not in POOLS:
            raise ValueError(profile)
        # The image lives in the template; PodSandbox's digest check still guards the env value.
        super().__init__("hardened", image=image, context=context, ready_timeout=ready_timeout)
        self.profile = profile
        self.pool = POOLS[profile]

    def claim_spec(self, name: str) -> dict:
        return {"apiVersion": "extensions.agents.x-k8s.io/v1beta1", "kind": "SandboxClaim",
                "metadata": {"name": name, "labels": {"lab-session": self.session}},
                "spec": {"warmPoolRef": {"name": self.pool}}}

    def pod_of(self, claim: str) -> str:
        """The claim's sandbox, then its Pod. The docs say the Pod shares the sandbox's name, and one
        example shows otherwise, so the Pod is found through the sandbox's own selector when it has
        one, and by name only as the fallback."""
        sb = self.kubectl("get", "sandboxclaim", claim, "-o", "jsonpath={.status.sandbox.name}").stdout.decode().strip()
        if not sb:
            raise RuntimeError(f"claim {claim} has no sandbox in its status")
        sel = self.kubectl("get", "sandbox", sb, "-o", "jsonpath={.status.selector}", check=False).stdout.decode().strip()
        if sel:
            pods = self.kubectl("get", "pods", "-l", sel, "-o", "jsonpath={.items[*].metadata.name}").stdout.decode().split()
            if len(pods) == 1:
                return pods[0]
            self.events.append({"pod_lookup": "selector matched", "count": len(pods), "selector": sel})
        return sb

    def run(self, workspace: str, argv: list[str], timeout: int = 1800, extra_env: dict | None = None,
            extra_mounts: list[str] | None = None) -> subprocess.CompletedProcess:
        if extra_mounts or extra_env:
            raise ValueError("the sandbox venue takes no extra mounts or env; the template is the input")
        claim = f"lab-claim-{uuid.uuid4().hex[:8]}"
        t0 = time.time()
        self.kubectl("apply", "-f", "-", input=json.dumps(self.claim_spec(claim)).encode())
        try:
            self.kubectl("wait", "--for=condition=Ready", f"sandboxclaim/{claim}",
                         f"--timeout={self.ready_timeout}s", timeout=self.ready_timeout + 30)
            pod = self.pod_of(claim)
            ready = time.time()
            self.transfer(pod, ["tar", "-x", "-C", "/work"], pack(workspace))
            p = self.kubectl("exec", pod, "--", *argv, check=False, timeout=timeout)
            out = self.transfer(pod, ["tar", "-c", "-C", "/work", "."])
            unpack_replace(out, workspace)
            self.events.append({"claim": claim, "pod": pod, "profile": self.profile,
                                "claim_to_ready_s": round(ready - t0, 1), "total_s": round(time.time() - t0, 1),
                                "exit": p.returncode})
            return subprocess.CompletedProcess(argv, p.returncode, p.stdout.decode(errors="replace"),
                                               p.stderr.decode(errors="replace"))
        finally:
            self.delete(f"sandboxclaim/{claim}")

    def leftover(self) -> list[str]:
        r = self.kubectl("get", "sandboxclaims", "-l", f"lab-session={self.session}", "-o", "name", check=False)
        return r.stdout.decode().split()


def make_venue(profile: str, **kw):
    """The harnesses' one entry point: plain GKE profiles get a Pod, Agent Sandbox variants a claim,
    and Substrate an actor for the whole investigation."""
    if profile == "executor":
        from executor_venue import ExecutorVenue
        return ExecutorVenue()
    if profile == "substrate":
        from substrate_venue import SubstrateVenue
        return SubstrateVenue()
    if profile in POOLS:
        return AgentSandboxVenue(profile, **{k: v for k, v in kw.items() if k != "drop"})
    return PodSandbox(profile, **kw)
