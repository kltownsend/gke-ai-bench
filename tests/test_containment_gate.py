"""The --dangerous gate (harness/containment_gate.py): records shaped like probe_run.py's, no cluster.

    python3 -m unittest tests.test_containment_gate -v
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "harness"))
sys.path.insert(0, os.path.join(TOOLS, "controller"))
import containment_gate as G  # noqa: E402

CTX = "gke_example-project_us-central1-a_lab-substrate"
T = 1800000000.0
REFUSED = {"refused": "URLError: timed out"}

# The passing readings, as the lab's Substrate probes recorded them (identity and addresses neutral).
API = {"probe": "api_scope", "t": T,
       "credential": {"placeholder": True, "real_metadata_token": False,
                      "files": {"/var/run/secrets/kubernetes.io/serviceaccount/token": False,
                                "/home/agent/.config/gcloud/application_default_credentials.json": False}},
       "positive_control": {"ok": True},
       "verdict_inputs": {"counts": True, "storage_reached": False, "usable_credential": True, "outside_dns": False}}
ESC = {"probe": "escape_suite", "t": T + 5, "writable_outside_workspace": [], "sa_token_mounted": False,
       "metadata_real_identity": REFUSED, "kubelet_10250": REFUSED, "kubelet_10255": REFUSED,
       "k8s_api_version_anonymous": REFUSED, "k8s_api_secrets_anonymous": REFUSED,
       "neighbor_canary": REFUSED, "neighbor_canary_by_ip": REFUSED,
       "direct_tcp_1.1.1.1_443": "connected"}


def write(d, venue="substrate", api=None, esc=None, ctx=CTX, exit_=0):
    for name, body in (("api_scope", api or API), ("escape_suite", esc or ESC)):
        if body is False:
            continue
        with open(os.path.join(d, f"{name}-{venue}.json"), "w") as fh:
            json.dump({"venue": venue, "label": venue, "run_token": "lab-run-x", "exit": exit_,
                       "kube_context": ctx, "result": body}, fh)


class Gate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def verdict(self, **kw):
        return G.check("substrate", self.d, context=kw.pop("context", CTX), created=kw.pop("created", T - 60))

    def test_passing_records_pass(self):
        write(self.d)
        v = self.verdict()
        self.assertTrue(v["passed"], v["failures"])

    def test_missing_records_refuse(self):
        v = self.verdict()
        self.assertFalse(v["passed"])
        self.assertEqual(sum("no api_scope record" in f or "no escape_suite record" in f for f in v["failures"]), 2)

    def test_each_containment_reading_can_refuse(self):
        breaks = [("api", ["credential", "placeholder"], False, "isn't a placeholder"),
                  ("api", ["credential", "real_metadata_token"], True, "real metadata token"),
                  ("api", ["credential", "files", "/var/run/secrets/kubernetes.io/serviceaccount/token"], True, "credential file"),
                  ("api", ["verdict_inputs", "storage_reached"], True, "Cloud Storage"),
                  ("api", ["verdict_inputs", "outside_dns"], True, "outside DNS"),
                  ("esc", ["writable_outside_workspace"], ["/etc"], "writable outside"),
                  ("esc", ["sa_token_mounted"], True, "service-account token"),
                  ("esc", ["metadata_real_identity"], {"status": 200}, "metadata_real_identity"),
                  ("esc", ["kubelet_10250"], {"status": 200}, "kubelet_10250"),
                  ("esc", ["k8s_api_secrets_anonymous"], {"status": 403}, "k8s_api_secrets_anonymous"),
                  ("esc", ["neighbor_canary_by_ip"], {"status": 200, "body": "canary"}, "neighbor_canary_by_ip"),
                  ("esc", ["listener"], {"status": 200}, "listener")]
        for which, path, value, expect in breaks:
            with self.subTest(path=path):
                api, esc = copy.deepcopy(API), copy.deepcopy(ESC)
                tgt = api if which == "api" else esc
                for k in path[:-1]:
                    tgt = tgt[k]
                tgt[path[-1]] = value
                d = tempfile.mkdtemp()
                write(d, api=api, esc=esc)
                v = G.check("substrate", d, context=CTX, created=T - 60)
                self.assertFalse(v["passed"])
                self.assertTrue(any(expect in f for f in v["failures"]), v["failures"])

    def test_a_reading_that_was_never_taken_refuses(self):
        esc = {k: v for k, v in ESC.items() if k != "metadata_real_identity"}
        write(self.d, esc=esc)
        self.assertFalse(self.verdict()["passed"])

    def test_a_probe_that_crashed_refuses(self):
        write(self.d, exit_=1)
        self.assertFalse(self.verdict()["passed"])

    def test_records_from_another_cluster_refuse(self):
        write(self.d, ctx="gke_example-project_us-central1-a_lab-other")
        v = self.verdict()
        self.assertFalse(v["passed"])
        self.assertTrue(any("not the current context" in f for f in v["failures"]))

    def test_records_from_a_previous_install_refuse(self):
        write(self.d)
        v = self.verdict(created=T + 3600)
        self.assertFalse(v["passed"])
        self.assertTrue(any("previous install" in f for f in v["failures"]))

    def test_unreadable_cluster_refuses(self):
        write(self.d)
        # created=None means "look it up"; a cluster that can't be read (gone, or signed out) refuses.
        orig = G.cluster_created
        G.cluster_created = lambda ctx: None
        try:
            v = G.check("substrate", self.d, context=CTX)
        finally:
            G.cluster_created = orig
        self.assertFalse(v["passed"])
        self.assertTrue(any("can't read" in f for f in v["failures"]))

    def test_local_venue_has_no_cluster_to_match(self):
        write(self.d, venue="local", ctx=None)
        v = G.check("local", self.d)
        self.assertTrue(v["passed"], v["failures"])


class Require(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ.pop(G.ENV, None)
        self.orig = G.check

    def tearDown(self):
        G.check = self.orig
        os.environ.pop(G.ENV, None)

    def test_refusal_exits_and_names_the_probe_command(self):
        with self.assertRaises(SystemExit) as cm:
            G.require("substrate", self.d)
        self.assertIn("probe_run.py --venue substrate", str(cm.exception))
        self.assertNotIn(G.ENV, os.environ)

    def test_pass_sets_the_marker(self):
        G.check = lambda venue, probes: {"passed": True, "failures": [], "probes": {"api_scope": {"run_token": "r1"}}}
        G.require("substrate", self.d)
        self.assertTrue(os.environ[G.ENV].startswith("passed:substrate:"))

    def test_override_runs_and_is_recorded(self):
        ev = G.require("substrate", self.d, override="probes rerun pending, owner approved")
        self.assertEqual(ev["override"], "probes rerun pending, owner approved")
        self.assertTrue(os.environ[G.ENV].startswith("override:substrate:"))


class Wrapper(unittest.TestCase):
    """controller/models.py refuses a prompts-off agy unless the gate ran."""

    def test_dangerous_without_the_gate_refuses(self):
        env = {k: v for k, v in os.environ.items() if k != G.ENV}
        env["LAB_AGY_DANGEROUS"] = "1"
        r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'controller'); import models; "
                            "models.AntigravityModel()"], cwd=TOOLS, env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("containment gate didn't run", r.stderr)

    def test_dangerous_with_the_gate_constructs_and_off_needs_nothing(self):
        import models
        os.environ[G.ENV] = "passed:substrate:r1"
        try:
            self.assertTrue(models.AntigravityModel(dangerous=True).dangerous)
        finally:
            os.environ.pop(G.ENV, None)
        self.assertFalse(models.AntigravityModel(dangerous=False).dangerous)


if __name__ == "__main__":
    unittest.main()
