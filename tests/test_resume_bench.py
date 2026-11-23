"""The snapshot-and-restore probe and the Google benchmark wrapper, without a cluster. No cloud, no cost.

    python3 -m unittest tests.test_resume_bench -v
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "agent"))
sys.path.insert(0, os.path.join(TOOLS, "harness"))
import actor_server as AS  # noqa: E402
import google_bench as GB  # noqa: E402
import resume_probe as RP  # noqa: E402
import substrate_venue as SUB  # noqa: E402


class StateEndpoint(unittest.TestCase):
    def test_state_reports_process_memory_and_child(self):
        AS.start_state()
        try:
            port = SUB.free_port()
            srv = AS.Threaded(("127.0.0.1", port), AS.Actor)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            get = lambda: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/state", timeout=5).read())  # noqa: E731
            a = get()
            time.sleep(0.3)
            b = get()
            self.assertEqual((a["pid"], a["boot"], a["child_pid"]), (b["pid"], b["boot"], b["child_pid"]))
            self.assertTrue(b["child_alive"])
            self.assertGreater(b["ticks"], a["ticks"])                 # the counter moves in memory
            srv.shutdown()
            srv.server_close()
        finally:
            AS.CHILD[0].kill()
            AS.CHILD.clear()


class StorageMode(unittest.TestCase):
    def test_storage_run_seeds_suspends_waits_and_wakes_each(self):
        from tests.test_substrate_venue import FakeAte, start_actor
        work = tempfile.mkdtemp(prefix="actor-work-")
        AS.start_state()
        srv, shim, port, _ = start_actor(work)
        home = os.environ["HOME"]
        os.environ["HOME"] = work
        made = []
        real = SUB.SubstrateVenue

        def factory(*a, **k):
            v = real(ready_timeout=10)
            v.ate = FakeAte()
            v.start_proxy = lambda: setattr(v, "port", port)
            v.stop_proxy = lambda: None
            v.start_proxy()
            made.append(v)
            return v
        SUB.SubstrateVenue = factory
        try:
            out = RP.storage_run(2, 0)
        finally:
            SUB.SubstrateVenue = real
            os.environ["HOME"] = home
            for x in (srv, shim):
                x.shutdown()
                x.server_close()
            AS.CHILD[0].kill()
            AS.CHILD.clear()
        self.assertEqual([c["mode"] for c in out], ["storage", "storage"])
        for c in out:
            self.assertIsNotNone(c["wake_first_touch_s"])
            self.assertTrue(c["memory_mark"] or c["i"] == 0)        # one shared server: the last mark wins
        self.assertTrue(all(("suspend", "actor") in [x[:2] for x in v.ate.calls] for v in made))
        self.assertTrue(all(("delete", "actor") in [x[:2] for x in v.ate.calls] for v in made))


class Stats(unittest.TestCase):
    def test_stats_keeps_samples_and_labels_percentiles(self):
        x = RP.stats([.3, .2, .9, None])
        self.assertEqual((x["n"], x["samples"], x["median"], x["max"]), (3, [.3, .2, .9], .3, .9))
        self.assertIn("empirical_p95", x)
        self.assertNotIn("p99", x)

    def test_interleave(self):
        self.assertEqual(RP.interleave(2, 3), ["explicit", "wake", "explicit", "wake", "wake"])


class Cycles(unittest.TestCase):
    """Both cycle types end to end against the real actor server on the workstation; only `kubectl ate`
    is faked (a suspend that changes nothing, so continuity must come out whole)."""
    def setUp(self):
        from tests.test_substrate_venue import FakeAte, start_actor
        self.work = tempfile.mkdtemp(prefix="actor-work-")
        AS.start_state()
        self.srv, self.shim, self.port, _ = start_actor(self.work)
        v = SUB.SubstrateVenue(ready_timeout=10)
        v.ate = FakeAte()
        v.start_proxy = lambda: setattr(v, "port", self.port)
        v.stop_proxy = lambda: None
        v.start_proxy()
        self.v = v
        self.home = os.environ["HOME"]

    def tearDown(self):
        os.environ["HOME"] = self.home
        for s in (self.srv, self.shim):
            s.shutdown()
            s.server_close()
        AS.CHILD[0].kill()
        AS.CHILD.clear()

    def test_explicit_and_wake_cycles(self):
        os.environ["HOME"] = self.work                       # HOME's manifest is taken in the "actor"
        RP.seed_actor(self.v)
        e = RP.explicit_cycle(self.v, 0)
        w = RP.wake_cycle(self.v, 1)
        for c in (e, w):
            for k in RP.CHECKS:
                self.assertTrue(c[k], (k, c))
        self.assertIsNotNone(e["lab_resume_to_first_response_s"])
        self.assertIsNotNone(w["wake_first_touch_s"])
        self.assertFalse(AS.QUIET.is_set())                  # the barrier is always lifted
        self.assertIn("--mode=sync", [c for c in self.v.ate.calls if c[:2] == ("suspend", "actor")][0])

    def test_a_replayed_older_state_fails_the_memory_mark(self):
        os.environ["HOME"] = self.work
        RP.seed_actor(self.v)
        pre = RP.checkpoint(self.v, 3)
        AS.MARK["nonce"] = "an older snapshot's nonce"       # what replaying an earlier image looks like
        c = RP.verify(self.v, pre, 3)
        self.assertFalse(c["memory_mark"])

    def test_a_file_change_fails_work_intact(self):
        os.environ["HOME"] = self.work
        RP.seed_actor(self.v)
        pre = RP.checkpoint(self.v, 4)
        os.chmod(os.path.join(self.work, "notes.txt"), 0o600)  # metadata only
        self.assertFalse(RP.verify(self.v, pre, 4)["work_intact"])

    def test_a_failed_manifest_raises_instead_of_matching(self):
        self.v.http = lambda *a, **k: json.dumps({"exitCode": 1, "stdout": "", "stderr": "boom"}).encode()
        with self.assertRaises(RuntimeError):
            self.v.manifest()


class DensityControl(unittest.TestCase):
    def test_the_control_pod_matches_the_actor(self):
        import density_control as DC
        m = json.loads(DC.manifest({"image": "img@sha256:x", "command": ["/ko-app/glutton"]}, "pool-a", "1", "1Gi", 8))
        spec = m["spec"]["template"]["spec"]
        c = spec["containers"][0]
        self.assertEqual(c["resources"]["requests"], c["resources"]["limits"])     # booked like an actor
        self.assertNotIn("runtimeClassName", spec)                                  # the standard runtime
        self.assertEqual(spec["nodeSelector"], {"cloud.google.com/gke-nodepool": "pool-a"})
        self.assertEqual(m["spec"]["replicas"], 8)

    def test_frontier(self):
        import density_control as DC
        steps = [{"replicas": 4, "ready": 4, "pending": 0, "per_node": {"n1": 2, "n2": 2}},
                 {"replicas": 8, "ready": 6, "pending": 2, "per_node": {"n1": 3, "n2": 3}}]
        self.assertEqual(DC.frontier(steps), {"ready": 6, "per_node": {"n1": 3, "n2": 3}, "first_pending_at_replicas": 8})


class GoogleBench(unittest.TestCase):
    def test_append_flag_forms(self):
        self.assertEqual(GB.append_flag(["--x=a"], "--x", "b"), (["--x=a,b"], True))
        self.assertEqual(GB.append_flag(["--x", "a,b"], "--x", "b"), (["--x", "a,b"], False))
        self.assertEqual(GB.append_flag(["--y=1"], "--x", "b"), (["--y=1", "--x=b"], True))

    @unittest.skipUnless(os.path.exists(GB.TEMPLATE), "needs the v1.0.1 substrate checkout")
    def test_every_test_renders_googles_template(self):
        for name, t in GB.TESTS.items():
            m = GB.render(name, t, f"runner-{name}"[:40], f"gs://b/{name}")
            self.assertNotIn("${", m)
            self.assertIn(f'- "{t["file"]}"', m)
            i = m.index('- "--dest"')
            tail = m[i:]
            for f in t["flags"]:
                self.assertIn(json.dumps(f), tail)                     # flags follow --dest, as appended
            self.assertIn('"--metadata"', tail)
            self.assertIn('nodeSelector:\n        ate.dev/benchmarking: "true"', m)   # the runner's own node

    def test_dry_run_writes_no_state(self):
        before = os.stat(GB.STATE).st_mtime_ns if os.path.exists(GB.STATE) else None
        subprocess.run([sys.executable, os.path.join(TOOLS, "harness", "google_bench.py"), "run",
                        "durdir_implicit_resume", "--out", tempfile.mkdtemp()],
                       capture_output=True, text=True, check=True,
                       env={k: v for k, v in os.environ.items() if k != "ATE_INSTALL_KIND"})
        after = os.stat(GB.STATE).st_mtime_ns if os.path.exists(GB.STATE) else None
        self.assertEqual(before, after)                 # a dry run never writes the state file

    def test_a_refused_run_is_not_complete(self):
        log = ("[locust]          Aggregated   59  59(100.00%) |  0 0 9 1 |  1.02  1.02\n"
               '{"err":"rpc error: code = PermissionDenied desc = permission denied"}\n')
        self.assertFalse(GB.served(log)["ok"])
        self.assertTrue(GB.served("[locust]          Aggregated   100  2(2.00%) | ...")["ok"])
        self.assertFalse(GB.served("no stats at all")["ok"])

    def test_sizing_for_googles_delete(self):
        self.assertEqual((GB.mem_bytes("1Gi"), GB.mem_bytes("256Mi"), GB.millicores("10m"), GB.millicores("1")),
                         (1 << 30, 256 << 20, 10, 1000))

    def test_has_stats(self):
        d = tempfile.mkdtemp()
        self.assertFalse(GB.has_stats(d))
        os.makedirs(os.path.join(d, "x"))
        for n in ("stats.jsonl", "status.json"):
            open(os.path.join(d, "x", n), "w").close()
        self.assertTrue(GB.has_stats(d))

    def test_list(self):
        out = subprocess.run([sys.executable, os.path.join(TOOLS, "harness", "google_bench.py"), "list"],
                             capture_output=True, text=True, check=True).stdout
        for n in GB.TESTS:
            self.assertIn(n, out)


if __name__ == "__main__":
    unittest.main()
