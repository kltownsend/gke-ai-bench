"""The Agent Sandbox venue without a cluster: one call is one claim against the right warm pool, the
Pod is found through the sandbox's selector, the workspace round-trips, and the claim is deleted.
kubectl is faked; no cloud, no cost.

    python3 -m unittest tests.test_sandbox_venue -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "agent"))
import pod_sandbox as P  # noqa: E402
import sandbox_venue as SV  # noqa: E402

IMAGE = "example/lab-agent@sha256:" + "0" * 64


class FakeAgentSandbox:
    """Claims resolve to sandbox sb-1, whose selector finds Pod pod-xyz; the agent edits a file."""
    def __init__(self, selector="agents.x-k8s.io/sandbox-name-hash=abc"):
        self.calls, self.work, self.selector, self.applied = [], None, selector, []

    def __call__(self, *args, input=None, check=True, timeout=None):
        self.calls.append(args)
        out = b""
        if args[0] == "apply":
            self.applied.append(json.loads(input))
        elif args[:2] == ("get", "sandboxclaim"):
            out = b"sb-1"
        elif args[:2] == ("get", "sandbox"):
            out = self.selector.encode()
        elif args[:2] == ("get", "pods"):
            out = b"pod-xyz"
        elif args[0] == "exec" and "-x" in args:
            assert args[2] == "pod-xyz", args
            self.work = input
        elif args[0] == "exec" and "-c" in args:
            stage = tempfile.mkdtemp()
            P.unpack_replace(self.work, stage)
            with open(os.path.join(stage, "a.py"), "w") as fh:
                fh.write("edited in the sandbox\n")
            out = P.pack(stage)
            shutil.rmtree(stage)
        elif args[0] == "exec":
            out = json.dumps({"status": "SUCCESS"}).encode()
        return subprocess.CompletedProcess(args, 0, out, b"")


class Venue(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        with open(os.path.join(self.ws, "a.py"), "w") as fh:
            fh.write("original\n")

    def tearDown(self):
        shutil.rmtree(self.ws)

    def test_one_call_is_one_claim_on_the_right_pool_then_deleted(self):
        for profile, pool in (("sandbox", "lab-agent-pool"), ("sandbox-documented", "lab-documented-pool")):
            v = SV.AgentSandboxVenue(profile, image=IMAGE)
            fake = FakeAgentSandbox()
            v.kubectl = fake
            self.assertEqual(v.run(self.ws, ["agy", "-p", "x"]).returncode, 0)
            claim = fake.applied[0]
            self.assertEqual(claim["kind"], "SandboxClaim")
            self.assertEqual(claim["spec"]["warmPoolRef"]["name"], pool)
            self.assertEqual(claim["metadata"]["labels"]["lab-session"], v.session)
            self.assertEqual(fake.calls[-1][0], "delete")
            self.assertTrue(fake.calls[-1][1].startswith("sandboxclaim/lab-claim-"))
            with open(os.path.join(self.ws, "a.py")) as fh:
                self.assertEqual(fh.read(), "edited in the sandbox\n")

    def test_pod_falls_back_to_the_sandbox_name_without_a_selector(self):
        v = SV.AgentSandboxVenue("sandbox", image=IMAGE)
        v.kubectl = FakeAgentSandbox(selector="")
        self.assertEqual(v.pod_of("lab-claim-1"), "sb-1")

    def test_no_extra_env_or_mounts(self):
        v = SV.AgentSandboxVenue("sandbox", image=IMAGE)
        v.kubectl = FakeAgentSandbox()
        with self.assertRaises(ValueError):
            v.run(self.ws, ["agy"], extra_env={"X": "1"})

    def test_factory_routes_profiles(self):
        self.assertIsInstance(SV.make_venue("sandbox", image=IMAGE), SV.AgentSandboxVenue)
        self.assertIsInstance(SV.make_venue("hardened", image=IMAGE), P.PodSandbox)
        self.assertNotIsInstance(SV.make_venue("default", image=IMAGE), SV.AgentSandboxVenue)
        with self.assertRaises(ValueError):
            SV.AgentSandboxVenue("hardened", image=IMAGE)

    def test_exit_cleans_this_sessions_claims_only(self):
        v = SV.AgentSandboxVenue("sandbox", image=IMAGE)
        fake = FakeAgentSandbox()
        v.kubectl = fake
        v.__exit__(None, None, None)
        gets = [c for c in fake.calls if c[:2] == ("get", "sandboxclaims")]
        self.assertTrue(gets and f"lab-session={v.session}" in gets[0])


if __name__ == "__main__":
    unittest.main()
