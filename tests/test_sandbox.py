"""The agent sandbox's walls, tested from inside it. No model calls.

An attacker script runs in the exact container the agent gets and tries what a hostile or
confused agent would: read the host, write outside its workspace, reach the forge or the internet,
reach Docker, outlive the run. Every attempt must fail at the boundary. Two things must work:
HTTPS to a Google API host through the proxy, and a lab-agent token from the metadata server.

Needs Docker, and for the token test a lab credential on the host (source harness/lab_env.sh);
the token test skips without one. Minting a token costs nothing.

    python3 -m unittest tests.test_sandbox -v
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
from sandbox import AGENT_UID, IMAGE, SIDECAR_UID, Sandbox  # noqa: E402

KEY = os.path.join(TOOLS, "cases", "S01", "key.json")
HOST_GCLOUD = os.path.expanduser("~/.config/gcloud")
# An interpreter with google-auth, for the token minter (GOOGLE_AUTH_PY in lab.env).
GOOGLE_AUTH_PY = shutil.which(os.path.expanduser(os.environ.get("GOOGLE_AUTH_PY") or "python3")) or sys.executable

ATTACK = r'''
import json, os, socket, subprocess, urllib.request
out = {}
def attempt(name, fn):
    try:
        out[name] = {"ok": True, "value": fn()}
    except Exception as e:
        out[name] = {"ok": False, "error": type(e).__name__ + ": " + str(e)[:120]}
def read(p):
    with open(p) as fh:
        return fh.read(40)
def write(p):
    with open(p, "w") as fh:
        fh.write("x")
    return "written"
def connect(host, port):
    s = socket.create_connection((host, port), timeout=5); s.close(); return "connected"
def via_proxy(host):
    s = socket.create_connection(("SIDECAR", 3128), timeout=10)
    s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    line = s.recv(200).decode().split("\r\n")[0]; s.close(); return line
def metadata(path, flavor=True):
    h = {"Metadata-Flavor": "Google"} if flavor else {}
    req = urllib.request.Request("http://metadata.google.internal" + path, headers=h)
    return urllib.request.urlopen(req, timeout=5).read().decode()
attempt("read_answer_key", lambda: read("KEY"))
attempt("read_host_gcloud", lambda: os.listdir("HOST_GCLOUD"))
attempt("write_root", lambda: write("/etc/owned"))
attempt("write_agy_binary", lambda: write("/usr/local/bin/agy"))
attempt("write_workspace", lambda: write("/work/.sandbox_probe"))
attempt("docker_socket", lambda: os.path.exists("/var/run/docker.sock") or (_ for _ in ()).throw(FileNotFoundError("absent")))
attempt("forge_localhost", lambda: connect("127.0.0.1", 3300))
attempt("forge_host_gateway", lambda: connect("host.docker.internal", 3300))
attempt("internet_direct", lambda: connect("1.1.1.1", 443))
attempt("proxy_github", lambda: via_proxy("github.com"))
attempt("proxy_example", lambda: via_proxy("example.com"))
attempt("proxy_googleapis", lambda: via_proxy("aiplatform.googleapis.com"))
attempt("proxy_storage", lambda: via_proxy("storage.googleapis.com"))
attempt("proxy_www", lambda: via_proxy("www.googleapis.com"))
attempt("proxy_play", lambda: via_proxy("play.googleapis.com"))
attempt("proxy_case", lambda: via_proxy("AIPLATFORM.googleapis.com.evil.example"))
attempt("metadata_email", lambda: metadata("/computeMetadata/v1/instance/service-accounts/default/email"))
attempt("metadata_token", lambda: json.loads(metadata("/computeMetadata/v1/instance/service-accounts/default/token")).get("token_type"))
attempt("metadata_no_flavor", lambda: metadata("/computeMetadata/v1/instance/service-accounts/default/token", flavor=False)[:20])
attempt("metadata_identity", lambda: metadata("/computeMetadata/v1/instance/service-accounts/default/identity?audience=x")[:20])
attempt("uid", lambda: os.getuid())
attempt("background", lambda: subprocess.Popen(["sleep", "600"]).pid)
print("RESULT " + json.dumps(out))
'''


class SandboxWalls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokens = tempfile.mkdtemp()
        cls.minted = False
        env = os.environ.copy()
        if os.path.exists(GOOGLE_AUTH_PY) and env.get("GOOGLE_APPLICATION_CREDENTIALS"):
            r = subprocess.run([GOOGLE_AUTH_PY, os.path.join(TOOLS, "agent", "mint_token.py"), cls.tokens],
                               capture_output=True, text=True, env=env)
            cls.minted = r.returncode == 0
        cls.ws = os.path.join(tempfile.mkdtemp(), "ws")
        shutil.copytree(os.path.join(TOOLS, "baselines", "s01"), cls.ws)
        os.chmod(cls.ws, 0o777)
        with Sandbox(cls.tokens) as sb:
            script = ATTACK.replace("SIDECAR", sb.sidecar).replace("HOST_GCLOUD", HOST_GCLOUD).replace("KEY", KEY)
            r = sb.run(cls.ws, ["python", "-c", script], timeout=300)
            cls.log = sb.sidecar_log()
            cls.sidecar_status = subprocess.run(["docker", "exec", sb.sidecar, "cat", "/proc/1/status"],
                                                capture_output=True, text=True).stdout
            cls.sidecar_nets = json.loads(subprocess.run(
                ["docker", "inspect", "-f", "{{json .NetworkSettings.Networks}}", sb.sidecar],
                capture_output=True, text=True).stdout)
            # Free checks, 2026-11-20: a plain container on Docker's default bridge reached both
            # sidecar ports. Try every address the sidecar has, from the bridge.
            probe = ("import socket,json,sys\nr={}\n"
                     "for ip in sys.argv[1:]:\n"
                     "  for port in (80,3128):\n"
                     "    try:\n      socket.create_connection((ip,port),timeout=5).close(); r[f'{ip}:{port}']='connected'\n"
                     "    except Exception as e:\n      r[f'{ip}:{port}']=type(e).__name__\n"
                     "print(json.dumps(r))")
            ips = [v["IPAddress"] for v in cls.sidecar_nets.values() if v.get("IPAddress")]
            cls.from_bridge = json.loads(subprocess.run(
                ["docker", "run", "--rm", "--network", "bridge", IMAGE, "python", "-c", probe, *ips],
                capture_output=True, text=True, timeout=120).stdout or "{}")
            cls.leftover = [n for n in subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}", "--filter", f"network={sb.net}"],
                capture_output=True, text=True).stdout.split() if n != sb.sidecar]
        line = [x for x in r.stdout.splitlines() if x.startswith("RESULT ")]
        cls.out = json.loads(line[-1][7:]) if line else {}
        cls.raw = r

    def test_ran(self):
        self.assertTrue(self.out, (self.raw.stdout[-500:], self.raw.stderr[-500:]))

    def test_cannot_read_the_host(self):
        for k in ("read_answer_key", "read_host_gcloud", "docker_socket"):
            with self.subTest(k=k):
                self.assertFalse(self.out[k]["ok"], self.out[k])

    def test_cannot_write_outside_the_workspace(self):
        for k in ("write_root", "write_agy_binary"):
            with self.subTest(k=k):
                self.assertFalse(self.out[k]["ok"], self.out[k])
        self.assertTrue(self.out["write_workspace"]["ok"], self.out["write_workspace"])

    def test_cannot_reach_the_forge_or_the_internet_directly(self):
        for k in ("forge_localhost", "forge_host_gateway", "internet_direct"):
            with self.subTest(k=k):
                self.assertFalse(self.out[k]["ok"], self.out[k])

    def test_proxy_allows_exactly_vertex_ai(self):
        # Build review 4: a *.googleapis.com wildcard admitted Cloud Storage (signed-URL uploads
        # need no credential). Only aiplatform.googleapis.com goes through.
        self.assertIn("200", self.out["proxy_googleapis"]["value"])
        for k in ("proxy_github", "proxy_example", "proxy_storage", "proxy_www", "proxy_play", "proxy_case"):
            with self.subTest(k=k):
                self.assertIn("403", self.out[k]["value"])
        denied = {e.get("host") for e in self.log if e.get("decision") == "denied"}
        self.assertTrue({"github.com", "storage.googleapis.com", "play.googleapis.com"} <= denied, denied)

    def test_runs_unprivileged_and_leaves_nothing_running(self):
        self.assertEqual(self.out["uid"]["value"], AGENT_UID)
        self.assertNotEqual(AGENT_UID, 65534)            # the judge's child
        self.assertNotEqual(AGENT_UID, SIDECAR_UID)
        self.assertEqual(self.leftover, [])

    def test_sidecar_is_unprivileged(self):
        f = {l.split(":")[0]: l.split(":", 1)[1].split() for l in self.sidecar_status.splitlines() if ":" in l}
        self.assertEqual(f["Uid"], [str(SIDECAR_UID)] * 4)
        for cap in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            with self.subTest(cap=cap):
                self.assertEqual(int(f[cap][0], 16), 0)
        self.assertEqual(f["NoNewPrivs"], ["1"])

    def test_sidecar_is_off_the_default_bridge(self):
        self.assertNotIn("bridge", self.sidecar_nets)
        self.assertEqual(len(self.sidecar_nets), 2, self.sidecar_nets)
        self.assertTrue(self.from_bridge, "the bridge probe didn't run")
        for addr, got in self.from_bridge.items():
            with self.subTest(addr=addr):
                self.assertNotEqual(got, "connected")

    def test_metadata_serves_only_lab_agent_tokens(self):
        if not self.minted:
            self.skipTest("no lab credential on the host (source harness/lab_env.sh)")
        self.assertEqual(self.out["metadata_email"]["value"], os.environ.get("LAB_AGENT"))
        self.assertEqual(self.out["metadata_token"]["value"], "Bearer")
        self.assertFalse(self.out["metadata_no_flavor"]["ok"], self.out["metadata_no_flavor"])
        self.assertFalse(self.out["metadata_identity"]["ok"], self.out["metadata_identity"])


if __name__ == "__main__":
    unittest.main()


class TokenFile(unittest.TestCase):
    """Build review 4: the bearer token was written 0644 in a 0755 directory."""

    def test_minted_token_is_private(self):
        if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or not os.path.exists(GOOGLE_AUTH_PY):
            self.skipTest("no lab credential on the host (source harness/lab_env.sh)")
        d = tempfile.mkdtemp()
        subprocess.run([GOOGLE_AUTH_PY, os.path.join(TOOLS, "agent", "mint_token.py"), d], check=True,
                       capture_output=True)
        self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(d, "token.json")).st_mode & 0o777, 0o600)

    def test_sandbox_refuses_an_exposed_token_dir(self):
        import sandbox
        d = tempfile.mkdtemp()
        os.chmod(d, 0o755)
        with self.assertRaises(RuntimeError):
            sandbox.check_token_dir(d)
        os.chmod(d, 0o700)
        with open(os.path.join(d, "token.json"), "w") as fh:
            fh.write("{}")
        os.chmod(os.path.join(d, "token.json"), 0o644)
        with self.assertRaises(RuntimeError):
            sandbox.check_token_dir(d)
        os.chmod(os.path.join(d, "token.json"), 0o600)
        os.symlink("/etc/hosts", os.path.join(d, "extra"))
        with self.assertRaises(RuntimeError):
            sandbox.check_token_dir(d)
        os.unlink(os.path.join(d, "extra"))
        sandbox.check_token_dir(d)

