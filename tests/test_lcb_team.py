"""Team two (LoopControlBench) on the same Substrate install, without a cluster. No cloud, no cost.

    python3 -m unittest tests.test_lcb_team -v
"""
import os
import sys
import hashlib
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "harness"))
import lcb_team as L  # noqa: E402

TEAM = os.path.join(TOOLS, "infra", "substrate", "team-lcb")


@unittest.skipUnless(os.path.exists(os.path.join(L.LCB, "tasks", "_index.json")), "needs the bench checkout")
class Subset(unittest.TestCase):
    def test_stratified_and_deterministic(self):
        a, b = L.pick_tasks(10), L.pick_tasks(10)
        self.assertEqual([t["task_id"] for t in a], [t["task_id"] for t in b])
        self.assertEqual(len({t["repo"] for t in a}), 8)          # every library in the pool once first


class Policies(unittest.TestCase):
    def read(self, name):
        return open(os.path.join(TEAM, name)).read()

    def test_install_phase_is_the_package_hosts_only(self):
        t = self.read("egress-install.yaml")
        self.assertIn("tlsPassthrough", t)
        self.assertIn('"pypi.org"', t)
        self.assertNotIn("aiplatform", t)                          # no model during the install
        self.assertNotIn("replaceHeaders", t)

    def test_agent_phase_uses_team_twos_own_secret(self):
        t = self.read("egress-agent.yaml")
        self.assertIn("lcb-creds/", t)
        self.assertNotIn("lab-creds/", t)
        self.assertNotIn("pypi", t)

    def test_cross_team_probe_names_team_ones_secret(self):
        self.assertIn("lab-creds/", self.read("egress-cross-team.yaml"))

    def test_grant_keeps_team_one_and_adds_team_two(self):
        t = self.read("namespace-policy.yaml")
        self.assertIn("atespace: lab-agent", t)
        self.assertIn("atespace: lcb", t)
        self.assertEqual(t.count("allowedNamespaces"), 2)

    def test_template_shares_team_ones_pool(self):
        t = self.read("actor-template.yaml")
        self.assertIn("workload: lab-agent", t)
        self.assertIn("atespace: lcb", t)
        self.assertIn("LCB_ACTOR_IMAGE", t)


if __name__ == "__main__":
    unittest.main()


class Prompt(unittest.TestCase):
    """The carried prompt builder stays the text the lab ran (the unpublished bench copy's
    build_prompt_agentic). The hash was taken from that function on this same input."""
    LAB_SHA = "59b35758382efe69459844c7a0a3bcd08b203c684bbd7c4dd17969e2c12c0901"

    def test_matches_the_lab_prompt(self):
        rp = tempfile.mkdtemp()
        os.makedirs(os.path.join(rp, "pkg"))
        with open(os.path.join(rp, "pkg", "mod.py"), "w") as f:
            f.write("def f():\n    return 1\n")
        spec = {"repo": "example/pkg", "test_files": ["tests/test_mod.py"],
                "source_files": ["pkg/mod.py", "pkg/missing.py"]}
        p = L.build_prompt(spec, rp, lambda rp, spec: "def test_x():\n    assert f() == 2")
        self.assertEqual(hashlib.sha256(p.encode()).hexdigest(), self.LAB_SHA)
        self.assertNotIn("SEARCH/REPLACE", p)
        self.assertIn("./venv/bin/python -m pytest -vv tests/test_mod.py", p)
