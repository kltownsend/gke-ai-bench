"""The GKE agent sandbox without a cluster: the Pod specs per profile, the workspace's round trip,
and refusal of a hostile tar stream from the agent's Pod. kubectl is faked; no cloud, no cost.

    python3 -m unittest tests.test_pod_sandbox -v
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
import pod_sandbox as P  # noqa: E402

IMAGE = "example/lab-agent@sha256:" + "0" * 64


def tar_of(entries):
    """entries: (name, kind, payload) with kind file|dir|symlink|fifo."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, kind, payload in entries:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
                continue
            ti.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "fifo": tarfile.FIFOTYPE}[kind]
            if kind == "symlink":
                ti.linkname = payload
            tf.addfile(ti)
    return buf.getvalue()


class Specs(unittest.TestCase):
    def test_hardened_pod_is_locked_down(self):
        s = P.pod_spec("x", "hardened", IMAGE)["spec"]
        c = s["containers"][0]
        self.assertEqual(s["serviceAccountName"], "lab-agent")
        self.assertFalse(s["automountServiceAccountToken"])
        self.assertTrue(s["securityContext"]["runAsNonRoot"])
        self.assertEqual(s["securityContext"]["runAsUser"], P.AGENT_UID)
        self.assertEqual(s["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertTrue(c["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(c["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(c["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertIn("limits", c["resources"])

    def test_default_and_vulnerable_pods_are_plain(self):
        for profile in ("default", "vulnerable"):
            s = P.pod_spec("x", profile, IMAGE)["spec"]
            self.assertNotIn("securityContext", s)
            self.assertNotIn("securityContext", s["containers"][0])
            self.assertNotIn("resources", s["containers"][0])
        # the default cluster's one added step is Workload Identity; vulnerable keeps the node's identity
        self.assertEqual(P.pod_spec("x", "default", IMAGE)["spec"]["serviceAccountName"], "lab-agent")
        self.assertNotIn("serviceAccountName", P.pod_spec("x", "vulnerable", IMAGE)["spec"])

    def test_no_pod_mounts_anything_from_the_host(self):
        for profile in P.PROFILES:
            for v in P.pod_spec("x", profile, IMAGE)["spec"]["volumes"]:
                self.assertEqual(set(v) - {"name"}, {"emptyDir"})

    def test_image_must_be_pinned_by_digest(self):
        with self.assertRaises(ValueError):
            P.PodSandbox("hardened", image="example/lab-agent:latest")

    def test_unknown_profile_refused(self):
        with self.assertRaises(ValueError):
            P.pod_spec("x", "permissive", IMAGE)


class Transfer(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "pkg"))
        with open(os.path.join(self.root, "pkg", "a.py"), "w") as fh:
            fh.write("a = 1\n")
        with open(os.path.join(self.root, "gone.txt"), "w") as fh:
            fh.write("x")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_round_trip_replaces_including_deletions(self):
        data = P.pack(self.root)
        other = tempfile.mkdtemp()
        try:
            P.unpack_replace(data, other)
            os.remove(os.path.join(other, "gone.txt"))
            with open(os.path.join(other, "pkg", "a.py"), "w") as fh:
                fh.write("a = 2\n")
            P.unpack_replace(P.pack(other), self.root)
        finally:
            shutil.rmtree(other)
        self.assertFalse(os.path.exists(os.path.join(self.root, "gone.txt")))
        with open(os.path.join(self.root, "pkg", "a.py")) as fh:
            self.assertEqual(fh.read(), "a = 2\n")

    def test_hostile_members_refused_and_workspace_untouched(self):
        for bad in ([("../escape.txt", "file", b"x")],
                    [("/abs.txt", "file", b"x")],
                    [("link", "symlink", "/etc/passwd")],
                    [("pkg/link", "symlink", "../../outside")],
                    [("pipe", "fifo", None)]):
            with self.assertRaises(ValueError, msg=bad):
                P.unpack_replace(tar_of(bad), self.root)
            self.assertTrue(os.path.exists(os.path.join(self.root, "gone.txt")), bad)

    def test_quotas_and_duplicates_refused(self):
        many = [(f"f{i}", "file", b"") for i in range(P.MAX_MEMBERS + 1)]
        deep = [("/".join(["d"] * (P.MAX_DEPTH + 1)) + "/x", "file", b"x")]
        dup = [("a.txt", "file", b"1"), ("./a.txt", "file", b"2")]
        hard = io.BytesIO()
        with tarfile.open(fileobj=hard, mode="w") as tf:
            ti = tarfile.TarInfo("h"); ti.type = tarfile.LNKTYPE; ti.linkname = "gone.txt"; tf.addfile(ti)
        for bad in (tar_of(many), tar_of(deep), tar_of(dup), hard.getvalue()):
            with self.assertRaises(ValueError):
                P.unpack_replace(bad, self.root)
            self.assertTrue(os.path.exists(os.path.join(self.root, "gone.txt")))

    def test_big_file_and_big_stream_refused(self):
        ti = tarfile.TarInfo("big"); ti.size = P.MAX_FILE_BYTES + 1
        claimed = ti.tobuf() + b"\0" * 1024       # the header claims the size; no body is sent
        with self.assertRaises(ValueError):
            P.unpack_replace(claimed, self.root)
        with self.assertRaises(ValueError):
            P.unpack_replace(b"\0" * (P.MAX_TAR_BYTES + 1), self.root)
        self.assertTrue(os.path.exists(os.path.join(self.root, "gone.txt")))

    def test_failed_swap_restores_the_old_tree(self):
        real = os.rename
        calls = []
        def flaky(a, b):
            calls.append((a, b))
            if len(calls) == 2:
                raise OSError("simulated")
            return real(a, b)
        P.os.rename = flaky
        try:
            with self.assertRaises(OSError):
                P.unpack_replace(tar_of([("new.txt", "file", b"n")]), self.root)
        finally:
            P.os.rename = real
        self.assertTrue(os.path.exists(os.path.join(self.root, "gone.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "new.txt")))

    def test_relative_symlink_inside_is_kept(self):
        P.unpack_replace(tar_of([("pkg", "dir", None), ("pkg/a.py", "file", b"a = 3\n"),
                                 ("pkg/b.py", "symlink", "a.py")]), self.root)
        self.assertEqual(os.readlink(os.path.join(self.root, "pkg", "b.py")), "a.py")


class FakeCluster:
    """Stands in for kubectl: one Pod's /work as a dict, an agent that edits a file."""
    def __init__(self, pods=()):
        self.calls, self.work, self.pods = [], None, list(pods)

    def __call__(self, *args, input=None, check=True, timeout=None):
        self.calls.append(args)
        out = b""
        if args[0] == "exec" and "-x" in args:
            self.work = input
        elif args[0] == "exec" and "-c" in args:
            stage = tempfile.mkdtemp()
            P.unpack_replace(self.work, stage)
            with open(os.path.join(stage, "pkg", "a.py"), "w") as fh:
                fh.write("a = 'edited in the pod'\n")
            out = P.pack(stage)
            shutil.rmtree(stage)
        elif args[0] == "get":
            out = "\n".join(self.pods).encode()
        elif args[0] == "exec":
            out = json.dumps({"status": "SUCCESS", "response": "ok"}).encode()
        return subprocess.CompletedProcess(args, 0, out, b"")


class FlakyCluster(FakeCluster):
    """The first tar -x fails, as the 2026-11-21 hardened probe run's did."""
    def __init__(self):
        super().__init__()
        self.failed = False

    def __call__(self, *args, input=None, check=True, timeout=None):
        if args[0] == "exec" and "-x" in args and not self.failed:
            self.failed = True
            self.calls.append(args)
            return subprocess.CompletedProcess(args, 1, b"", b"error: simulated stream reset")
        return super().__call__(*args, input=input, check=check, timeout=timeout)


class Run(unittest.TestCase):
    def test_transfer_failure_is_recorded_and_retried(self):
        root = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(root, "pkg"))
            with open(os.path.join(root, "pkg", "a.py"), "w") as fh:
                fh.write("a = 1\n")
            sb = P.PodSandbox("hardened", image=IMAGE)
            sb.kubectl = FlakyCluster()
            real_sleep, P.time.sleep = P.time.sleep, (lambda s: None)
            try:
                self.assertEqual(sb.run(root, ["agy"]).returncode, 0)
            finally:
                P.time.sleep = real_sleep
            failed = [e for e in sb.events if "transfer_failed" in e]
            self.assertEqual(len(failed), 1)
            self.assertIn("simulated stream reset", failed[0]["stderr"])
        finally:
            shutil.rmtree(root)

    def test_one_call_is_one_fresh_pod_created_and_deleted(self):
        root = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(root, "pkg"))
            with open(os.path.join(root, "pkg", "a.py"), "w") as fh:
                fh.write("a = 1\n")
            sb = P.PodSandbox("hardened", image=IMAGE)
            fake = FakeCluster()
            sb.kubectl = fake
            p = sb.run(root, ["agy", "-p", "hi"])
            self.assertEqual(p.returncode, 0)
            with open(os.path.join(root, "pkg", "a.py")) as fh:
                self.assertIn("edited in the pod", fh.read())
            verbs = [c[0] for c in fake.calls]
            self.assertEqual(verbs[0], "apply")
            self.assertEqual(verbs[-1], "delete")
            self.assertEqual(len(sb.events), 1)
            with self.assertRaises(ValueError):
                sb.run(root, ["agy"], extra_mounts=["/etc:/etc"])
        finally:
            shutil.rmtree(root)

    def test_exit_deletes_only_this_sessions_pods(self):
        sb = P.PodSandbox("hardened", image=IMAGE)
        fake = FakeCluster(pods=["pod/lab-agent-mine"])
        sb.kubectl = fake
        sb.__exit__(None, None, None)
        gets = [c for c in fake.calls if c[0] == "get"]
        self.assertTrue(gets and all(f"lab-session={sb.session}" in c for c in gets))
        self.assertNotIn(("delete", "pods", "-l", "app=lab-agent"), [c[:4] for c in fake.calls])
        self.assertTrue(any(c[0] == "delete" and c[1] == "pod/lab-agent-mine" for c in fake.calls))
        self.assertEqual(sb.events[-1], {"leftover_pods": ["pod/lab-agent-mine"]})   # fake never forgets

    def test_pod_spec_carries_the_session_label(self):
        spec = P.pod_spec("x", "default", IMAGE, "abc123")
        self.assertEqual(spec["metadata"]["labels"]["lab-session"], "abc123")


if __name__ == "__main__":
    unittest.main()
