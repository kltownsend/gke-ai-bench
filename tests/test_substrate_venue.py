"""The Substrate rung without a cluster: the actor server runs for real on the workstation (agent/actor_server.py)
and the venue talks to it over HTTP exactly as it would through the router; only `kubectl ate` is
faked. No cloud, no cost.

    python3 -m unittest tests.test_substrate_venue -v
"""
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "agent"))
import actor_server as AS  # noqa: E402
import sandbox_venue as SV  # noqa: E402
import substrate_venue as SUB  # noqa: E402


def start_actor(work):
    AS.WORK = work
    port, shim_port = SUB.free_port(), SUB.free_port()
    shim = AS.Threaded(("127.0.0.1", shim_port), AS.Shim)
    srv = AS.Threaded(("127.0.0.1", port), AS.Actor)
    for s in (shim, srv):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return srv, shim, port, shim_port


class FakeAte:
    def __init__(self):
        self.calls = []
        self.exists = True

    def __call__(self, *args, check=True, timeout=120):
        self.calls.append(args)
        import subprocess
        if args[:2] == ("get", "actor"):
            return subprocess.CompletedProcess(args, 0 if self.exists else 1, b"", b"")
        if args[:2] == ("delete", "actor"):
            self.exists = False
        return subprocess.CompletedProcess(args, 0, b"", b"")


class Base(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="actor-work-")
        self.srv, self.shim, self.port, self.shim_port = start_actor(self.work)
        self.ws = tempfile.mkdtemp(prefix="mac-ws-")
        with open(os.path.join(self.ws, "a.py"), "w") as fh:
            fh.write("original\n")

    def tearDown(self):
        for s in (self.srv, self.shim):
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.work, ignore_errors=True)
        shutil.rmtree(self.ws, ignore_errors=True)

    def venue(self):
        v = SUB.SubstrateVenue(ready_timeout=10)
        v.ate = FakeAte()
        v.start_proxy = lambda: setattr(v, "port", self.port)
        v.stop_proxy = lambda: None
        return v


class ActorServer(Base):
    def get(self, port, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
            return r.status, r.read()

    def test_shim_hands_out_only_the_placeholder(self):
        _, body = self.get(self.shim_port, "/computeMetadata/v1/instance/service-accounts/default/token")
        self.assertEqual(json.loads(body)["access_token"], "ate-placeholder")
        self.assertEqual(self.shim.server_address[0], "127.0.0.1")

    def test_hostile_tar_is_refused_and_work_untouched(self):
        with open(os.path.join(self.work, "keep"), "w") as fh:
            fh.write("x")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("../escape")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/workspace", data=buf.getvalue(), method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)
        self.assertTrue(os.path.exists(os.path.join(self.work, "keep")))
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.work), "escape")))

    def test_workspace_replaces_when_the_parent_is_read_only(self):
        # In the actor /work's parent is / and the agent's uid can't write it.
        parent = tempfile.mkdtemp(prefix="ro-parent-")
        work = os.path.join(parent, "work")
        os.mkdir(work)
        with open(os.path.join(work, "old"), "w") as fh:
            fh.write("x")
        os.chmod(parent, 0o555)
        try:
            AS.WORK = work
            src = tempfile.mkdtemp()
            with open(os.path.join(src, "new.py"), "w") as fh:
                fh.write("y")
            AS.replace_work(AS.pack(src))
            self.assertEqual(sorted(os.listdir(work)), ["new.py"])
        finally:
            os.chmod(parent, 0o755)
            AS.WORK = self.work
            shutil.rmtree(parent, ignore_errors=True)

    def test_process_rejects_an_empty_command(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/process", data=b'{"command": []}', method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 400)


class Venue(Base):
    def test_one_actor_for_the_investigation_and_the_workspace_round_trips(self):
        v = self.venue()
        with v:
            p = v.run(self.ws, ["sh", "-c", "cat a.py > seen && echo edited > a.py && echo out"])
            self.assertEqual((p.returncode, p.stdout.strip()), (0, "out"))
            with open(os.path.join(self.ws, "a.py")) as fh:
                self.assertEqual(fh.read(), "edited\n")
            with open(os.path.join(self.ws, "seen")) as fh:
                self.assertEqual(fh.read(), "original\n")
            v.run(self.ws, ["true"])
        creates = [c for c in v.ate.calls if c[:2] == ("create", "actor")]
        self.assertEqual(len(creates), 1)                    # one actor across both calls
        self.assertIn("--template=lab-agent", creates[0])
        policy = [c for c in v.ate.calls if c[:2] == ("create", "egress-policy")]
        self.assertEqual(policy[0][-1], SUB.POLICY)
        self.assertEqual(v.ate.calls[-2][:2], ("delete", "actor"))
        self.assertNotIn("leftover_actors", json.dumps(v.events))

    def test_nonzero_exit_and_env_pass_through(self):
        v = self.venue()
        with v:
            p = v.run(self.ws, ["sh", "-c", "echo $LAB_X; exit 3"], extra_env={"LAB_X": "y"})
        self.assertEqual((p.returncode, p.stdout.strip()), (3, "y"))

    def test_suspend_and_resume_use_the_documented_verbs(self):
        v = self.venue()
        with v:
            v.run(self.ws, ["true"])
            v.suspend()
            v.resume()
        verbs = [c[:2] for c in v.ate.calls]
        self.assertIn(("suspend", "actor"), verbs)
        self.assertIn(("resume", "actor"), verbs)
        self.assertIn("--mode=sync", [c for c in v.ate.calls if c[:2] == ("suspend", "actor")][0])

    def test_factory_routes_substrate(self):
        self.assertIsInstance(SV.make_venue("substrate"), SUB.SubstrateVenue)

    def test_no_extra_mounts(self):
        with self.assertRaises(ValueError):
            self.venue().run(self.ws, ["true"], extra_mounts=["/x:/y"])


class Manifests(unittest.TestCase):
    def test_policy_names_one_host_and_injects(self):
        text = open(SUB.POLICY).read()
        self.assertEqual(text.count("hostnames:"), 1)
        self.assertIn('["aiplatform.googleapis.com"]', text)
        self.assertIn("ate-secret://k8s.io/default/lab-creds/lab-agent-token/token", text)
        self.assertNotIn("tlsPassthrough", text)

    def test_template_points_agy_at_the_shim_and_the_bundle(self):
        text = open(os.path.join(TOOLS, "infra", "substrate", "actor-template.yaml")).read()
        self.assertIn('GCE_METADATA_HOST, value: "127.0.0.1:8081"', text)
        self.assertIn("/run/ate/egress-trust-bundle/trust-bundle.pem", text)
        self.assertIn("egress-mitm.ate.dev", text)
        self.assertNotIn("ANTHROPIC", text)
        self.assertNotIn("ya29", text)


if __name__ == "__main__":
    unittest.main()
