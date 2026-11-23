"""The interruption harness on one fix case, both effect modes.

Naive: recovery is clean except at two effect boundaries: a kill after an effect is written but
before it's acked doubles it, and a kill between effects drops the rest. Reconcile (the hand-built
fix, the hardened-GKE reference): clean at every point. In both, no effect lands without its
approval.

    python3 -m unittest tests.test_interrupt -v
"""
import os
import sys
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "harness"))
import interrupt as I  # noqa: E402

S01 = os.path.join(TOOLS, "cases", "S01")
NAIVE_EXPECTED = {"mid_effect": ({"pull_requests": 1}, {}), "between_effects": ({}, {"notifications": 1})}


class Interruption(unittest.TestCase):
    def check(self, mode, expected):
        r = I.run_case(S01, mode=mode)
        self.assertEqual(r["reference"]["final"]["stage"], "done")
        for pt, res in r["points"].items():
            with self.subTest(mode=mode, point=pt):
                self.assertTrue(res["killed"])
                self.assertEqual(res["recovered_stage"], "done")
                self.assertEqual(res["effects"]["unapproved"], 0)
                dup, miss = expected.get(pt, ({}, {}))
                self.assertEqual(res["duplicated"], dup)
                self.assertEqual(res["missing"], miss)

    def test_naive_profile(self):
        self.check("naive", NAIVE_EXPECTED)

    def test_reconcile_is_exactly_once(self):
        self.check("reconcile", {})



FORGE_CONF = os.path.join(TOOLS, "forge", ".forge.json")


@unittest.skipUnless(os.path.exists(FORGE_CONF), "local Gitea forge not set up (forge/setup.py)")
class InterruptionOnForge(Interruption):
    """The same profiles with the effects on the real Gitea forge."""

    def setUp(self):
        I.FORGE = True

    def tearDown(self):
        I.FORGE = False


if __name__ == "__main__":
    unittest.main()
