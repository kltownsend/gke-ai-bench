"""The Executor rung without a cluster: real git on the workstation stands in for the forge and the Task's clone;
only `ax` and `kubectl` are faked. The workspace goes out as a served bare repository, the "Task"
clones it at the root, the "agent" edits, adds and deletes files, and the documented
`git diff --cached --binary <base>` script brings every change back. No cloud, no cost.

    python3 -m unittest tests.test_executor_venue -v
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
import executor_venue as EX  # noqa: E402
import sandbox_venue as SV  # noqa: E402

IMAGE = "example/lab-executor@sha256:" + "0" * 64
LAB_HOLD = os.path.join(TOOLS, "infra", "executor", "image", "lab-hold")
AGY = ["agy", "--model", "gemini-4-hydrogen-medium", "--settings",
       json.dumps({"permissions": {"allow": ["command(*)", "write_file(*)"]}}), "--output-format", "json", "-p", "fix it"]


class FakeCluster:
    """The forge Pod is a local directory; a Task clones its repo, runs a fake agent, and reports."""
    def __init__(self, edit, state="Completed", exit_code=0, hold=True):
        self.forge = tempfile.mkdtemp(prefix="forge-")
        self.task_dirs, self.applied, self.calls = {}, [], []
        self.edit, self.state, self.exit_code, self.hold = edit, state, exit_code, hold

    def kubectl(self, *args, input=None, check=True, timeout=None):
        self.calls.append(("kubectl",) + args)
        if "tar" in args and "-x" in args:
            subprocess.run(["tar", "-x", "-C", self.forge], input=input, check=True)
        elif "rm" in args:
            shutil.rmtree(os.path.join(self.forge, os.path.basename(args[-1])), ignore_errors=True)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    def ax(self, *args, input=None, check=True, timeout=None):
        self.calls.append(("ax",) + args)
        out = b""
        if args[:1] == ("apply",):
            ws, task = [json.loads(d) for d in input.decode().split("\n---\n")]
            self.applied.append((ws, task))
            name = task["metadata"]["name"]
            repo = ws["spec"]["setup"]["git"][0]["repo"].rsplit("/", 1)[1]
            d = tempfile.mkdtemp(prefix="task-")
            subprocess.run(["git", "clone", "-q", os.path.join(self.forge, repo), d], check=True)
            os.makedirs(os.path.join(d, ".ax", "sessions"))                 # AX's own; must not come back
            with open(os.path.join(d, ".ax", "sessions", "x"), "w") as fh:
                fh.write("ax state")
            self.edit(d)
            if self.hold:                                       # the image's own lab-hold, no wait
                base = next(e["value"] for e in task["spec"]["env"] if e["name"] == "LAB_BASE")
                subprocess.run(["sh", LAB_HOLD, str(self.exit_code)], cwd=d,
                               env=dict(os.environ, LAB_BASE=base, LAB_HOLD_S="0", GIT_CONFIG_GLOBAL="/dev/null"))
            self.task_dirs[name] = d
        elif args[:2] == ("describe", "task"):
            out = (f"Name:         {args[2]}\nState:        {self.state}\n"
                   f"Outcome:      Succeeded (exitCode={self.exit_code})\nOutput:\n"
                   f"  {json.dumps({'status': 'SUCCESS', 'response': 'done'})}\n").encode()
        elif args[:1] == ("ssh",):
            name, script = args[1], args[-1]
            p = subprocess.run(["sh", "-c", script.replace("/workspace", self.task_dirs[name])],
                               capture_output=True, env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null"))
            if check and p.returncode:
                raise subprocess.CalledProcessError(p.returncode, args)
            return subprocess.CompletedProcess(args, p.returncode, p.stdout, b"")
        elif args[:2] == ("get", "tasks"):
            out = b"NAME STATE\n"
        return subprocess.CompletedProcess(args, 0, out, b"")


def agent_edits(d):
    with open(os.path.join(d, "a.py"), "w") as fh:
        fh.write("edited\n")
    with open(os.path.join(d, "new.py"), "w") as fh:
        fh.write("added\n")
    os.remove(os.path.join(d, "gone.py"))
    with open(os.path.join(d, "blob.bin"), "wb") as fh:
        fh.write(bytes(range(256)))


class Venue(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="mac-ws-")
        for name, text in (("a.py", "original\n"), ("gone.py", "delete me\n")):
            with open(os.path.join(self.ws, name), "w") as fh:
                fh.write(text)

    def venue(self, cluster):
        v = EX.ExecutorVenue(image=IMAGE)
        v.ax, v.kubectl = cluster.ax, cluster.kubectl
        return v

    def test_every_change_comes_back_and_ax_state_does_not(self):
        c = FakeCluster(agent_edits)
        with self.venue(c) as v:
            p = v.run(self.ws, AGY)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["status"], "SUCCESS")
        self.assertEqual(open(os.path.join(self.ws, "a.py")).read(), "edited\n")
        self.assertEqual(open(os.path.join(self.ws, "new.py")).read(), "added\n")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "gone.py")))
        self.assertEqual(open(os.path.join(self.ws, "blob.bin"), "rb").read(), bytes(range(256)))
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".ax")))
        self.assertEqual(os.listdir(c.forge), [])                 # the served repo is removed after

    def test_the_task_is_the_harness_path_with_the_labs_permissions(self):
        c = FakeCluster(lambda d: None)
        with self.venue(c) as v:
            v.run(self.ws, AGY)
        ws, task = c.applied[0]
        spec = task["spec"]
        self.assertEqual(spec["goal"]["text"], "fix it")
        self.assertNotIn("commands", spec)                         # the harness builds the command
        self.assertIs(spec["config"]["dangerouslySkipPermissions"], False)
        self.assertEqual(spec["config"]["settings"]["permissions"]["allow"], ["command(*)", "write_file(*)"])
        self.assertEqual(spec["config"]["outputFormat"], "json")
        self.assertEqual(spec["profile"]["image"], IMAGE)
        self.assertEqual(spec["gateway"]["name"], "lab-agent-gw")
        self.assertIn({"name": "GCE_METADATA_HOST", "value": "127.0.0.1:8081"}, spec["env"])
        self.assertIn({"name": "HOME", "value": "/tmp"}, spec["env"])       # agy's state outside the workspace
        self.assertFalse(any("ya29" in json.dumps(e) for e in spec["env"]))
        g = ws["spec"]["setup"]["git"][0]
        self.assertEqual((g["dir"], g["ref"]), (".", "main"))
        self.assertNotIn("depth", g)          # "dumb http transport does not support shallow capabilities"
        self.assertTrue(g["repo"].startswith("http://forge.lab-forge.svc.cluster.local:8080/"))
        deletes = [x for x in c.calls if x[:2] in (("ax", "delete"),)]
        self.assertEqual({x[2] for x in deletes}, {"task", "workspace"})

    def test_the_task_is_resumed_and_released_after_collection(self):
        c = FakeCluster(agent_edits)
        with self.venue(c) as v:
            v.run(self.ws, AGY)
        verbs = [x[1:3] for x in c.calls if x[0] == "ax"]
        self.assertLess(verbs.index(("apply", "-f")), verbs.index(("resume", "task")))
        name = c.applied[0][1]["metadata"]["name"]
        self.assertTrue(os.path.exists(os.path.join(c.task_dirs[name], ".ax", "lab-release", "collected")))

    def test_a_task_that_ends_without_the_hold_is_an_error(self):
        c = FakeCluster(agent_edits, hold=False)
        with self.venue(c) as v:
            with self.assertRaises(RuntimeError):
                v.run(self.ws, AGY)

    def test_failed_task_reports_its_exit(self):
        c = FakeCluster(lambda d: None, state="Failed", exit_code=3)
        with self.venue(c) as v:
            self.assertEqual(v.run(self.ws, AGY).returncode, 3)

    def test_a_probe_runs_as_the_tasks_command_after_the_shim(self):
        c = FakeCluster(lambda d: None)
        with self.venue(c) as v:
            v.run(self.ws, ["python3", "escape_suite.py", "--run-token", "t"])
        spec = c.applied[0][1]["spec"]
        self.assertNotIn("goal", spec)
        self.assertEqual(spec["commands"][-4:], ["python3", "escape_suite.py", "--run-token", "t"])
        self.assertIn("lab_shim.py", spec["commands"][2])
        self.assertIn("exec /usr/local/bin/lab-hold $?", spec["commands"][2])

    def test_unmapped_agy_arguments_are_refused(self):
        with self.assertRaises(ValueError):
            EX.argv_to_config(["agy", "--sandbox", "-p", "x"])
        with self.assertRaises(ValueError):
            EX.argv_to_config(["python3", "probe.py"])

    def test_image_must_be_pinned(self):
        with self.assertRaises(ValueError):
            EX.ExecutorVenue(image="example/lab-executor:latest")

    def test_factory_routes_executor(self):
        os.environ["LAB_EXECUTOR_IMAGE"] = IMAGE
        self.assertIsInstance(SV.make_venue("executor"), EX.ExecutorVenue)


class Manifests(unittest.TestCase):
    def test_gateway_names_two_hosts_and_injects_only_on_https(self):
        text = open(os.path.join(TOOLS, "infra", "executor", "gateway.yaml")).read()
        self.assertEqual(text.count("hostnames:"), 2)
        self.assertIn('["aiplatform.googleapis.com"]', text)
        self.assertIn('["forge.lab-forge.svc.cluster.local"]', text)
        self.assertEqual(text.count("replaceHeaders"), 1)
        self.assertNotIn("tlsPassthrough", text)


if __name__ == "__main__":
    unittest.main()
