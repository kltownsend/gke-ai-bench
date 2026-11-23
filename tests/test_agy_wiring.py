"""AntigravityModel's call path, without a model call or Docker.

A real call goes through the Sandbox or not at all: pay-as-you-go on the host is refused.
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "controller"))
import models as M  # noqa: E402


class FakeSandbox:
    def __init__(self, raise_timeout=False):
        self.calls, self.raise_timeout = [], raise_timeout

    def run(self, workspace, argv, timeout):
        self.calls.append((workspace, argv, timeout))
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, 0, '{"status": "success", "response": "hi", "usage": {"input_tokens": 5}}', "")


class Wiring(unittest.TestCase):
    def setUp(self):
        self.guard = mock.patch.object(M.AntigravityModel, "billing_guard", classmethod(lambda c: "pay-as-you-go"))
        self.guard.start()

    def tearDown(self):
        self.guard.stop()

    def test_calls_go_through_the_sandbox(self):
        sb = FakeSandbox()
        m = M.AntigravityModel(sandbox=sb)
        with mock.patch("subprocess.run", side_effect=AssertionError("ran on the host")):
            self.assertEqual(m._agy("p", "/ws", "hypothesis"), "hi")
        ws, argv, timeout = sb.calls[0]
        self.assertEqual((ws, argv[0], timeout), ("/ws", "agy", M.AntigravityModel.KILL_S))
        self.assertIn(M.AntigravityModel.MODEL, argv)
        self.assertEqual(m.calls[0]["billing"], "pay-as-you-go+sandbox")

    def test_pay_as_you_go_on_the_host_is_refused(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("ran on the host")):
            with self.assertRaises(RuntimeError):
                M.AntigravityModel()._agy("p", "/ws")

    def test_timeout_is_recorded_not_raised(self):
        m = M.AntigravityModel(sandbox=FakeSandbox(raise_timeout=True))
        self.assertEqual(m._agy("p", "/ws"), "")
        self.assertEqual(m.calls[0]["exit"], -9)


if __name__ == "__main__":
    unittest.main()
