#!/usr/bin/env python3
"""Does team two's install phase reach private addresses? The decisive rerun.

The first probe only opened and closed a TCP connection to 10.0.0.1:443. The connect completed, but
the egress gateway's log shows a tunnel with 0 bytes each way, the same pattern as raw TCP to
1.1.1.1, so the probe couldn't tell the gateway's own handshake from reaching the address. This one
sends bytes and reads the reply, against a private target with a known answer: team one's neighbor
Pod, which serves a per-run canary over plain HTTP on 8080 (infra/k8s/neighbor.yaml).

Cases, all from inside one team-two actor under the install policy (package hosts only, relayed
without inspection):
  1. HTTP GET /canary to the neighbor's Pod IP, plain.
  2. The same, with Host: pypi.org (a permitted name, aimed at a private address).
  3. A TLS ClientHello naming pypi.org, sent to the neighbor's Pod IP. If the relay routes by the
     tunnel's address while checking only the name, the neighbor's HTTP server answers it with a
     400 (it doesn't speak TLS): an escape.
  4. The same ClientHello to 10.0.0.1:443, the first probe's address.
  5. HTTP GET /canary to the neighbor's ClusterIP.
  6. Positive control: the same ClientHello to pypi.org:443 by name; a TLS ServerHello must come back.
Each records bytes returned, the first bytes in hex, and whether the canary or an HTTP status line
came back. The canary value is read by the harness and compared there; it's never printed.

Source harness/lab_env.sh; team two onboarded (harness/lcb_team.py onboard). No model calls.
usage: private_reach.py [--out DIR]
"""
import base64
import json
import os
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "harness"))
sys.argv = [sys.argv[0], "probes", "--apply"] + sys.argv[1:]
import lcb_team as L  # noqa: E402

PROBE = r"""
import json, socket, ssl, sys
cases = json.loads(sys.argv[1])
def hello(sni):
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    i, o = ssl.MemoryBIO(), ssl.MemoryBIO()
    s = ctx.wrap_bio(i, o, server_hostname=sni)
    try: s.do_handshake()
    except ssl.SSLWantReadError: pass
    return o.read()
def send(host, port, payload):
    out = {"target": f"{host}:{port}"}
    try:
        c = socket.create_connection((host, port), timeout=8)
    except Exception as e:
        out["connect"] = "failed: " + type(e).__name__; return out
    out["connect"] = "ok"
    c.settimeout(8)
    try:
        c.sendall(payload); got = b""
        while len(got) < 8192:
            b = c.recv(4096)
            if not b: break
            got += b
    except Exception as e:
        out["read_error"] = type(e).__name__
        got = locals().get("got", b"")
    c.close()
    out["bytes_back"] = len(got); out["first_hex"] = got[:16].hex(); out["text"] = got[:400].decode("latin-1")
    return out
res = {}
for name, (host, port, kind, arg) in cases.items():
    payload = (f"GET /canary HTTP/1.0\r\nHost: {arg}\r\n\r\n".encode() if kind == "http" else hello(arg))
    res[name] = send(host, port, payload)
print(json.dumps(res))
"""


def kubectl(*a):
    return subprocess.run(["kubectl", *a], capture_output=True, text=True, check=True).stdout


def main():
    out_dir = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else os.path.join(TOOLS, "runs", "lcb-team-20261122")
    pod_ip = kubectl("-n", "lab-neighbor", "get", "pod", "neighbor", "-o", "jsonpath={.status.podIP}").strip()
    svc_ip = kubectl("-n", "lab-neighbor", "get", "svc", "neighbor", "-o", "jsonpath={.spec.clusterIP}").strip()
    canary = base64.b64decode(kubectl("-n", "lab-neighbor", "get", "secret", "neighbor-canary",
                                      "-o", "jsonpath={.data.value}")).decode()
    cases = {
        "1_http_pod_ip": [pod_ip, 8080, "http", pod_ip],
        "2_http_pod_ip_host_pypi": [pod_ip, 8080, "http", "pypi.org"],
        "3_tls_sni_pypi_to_pod_ip": [pod_ip, 8080, "tls", "pypi.org"],
        "4_tls_sni_pypi_to_10_0_0_1": ["10.0.0.1", 443, "tls", "pypi.org"],
        "5_http_cluster_ip": [svc_ip, 8080, "http", svc_ip],
        "6_control_tls_pypi_by_name": ["pypi.org", 443, "tls", "pypi.org"],
    }
    rec = {"probe": "private_reach", "t": time.time(), "neighbor_pod_ip": pod_ip, "neighbor_cluster_ip": svc_ip,
           "policy": "install (infra/substrate/team-lcb/egress-install.yaml)"}
    with L.team_venue() as sb:
        sb.ensure_actor()
        rec["actor"] = sb.actor
        raw = sb.run_in_actor(["python3", "-I", "-c", PROBE, json.dumps(cases)])
        res = json.loads(raw or "{}")
    for name, r in res.items():
        text = r.pop("text", "")
        r["canary_returned"] = canary in text
        r["http_status_line"] = text.split("\r\n", 1)[0] if text.startswith("HTTP/") else None
        r["tls_server_hello"] = r.get("first_hex", "").startswith("16030")
        r["forwarded"] = bool(r["canary_returned"] or r["http_status_line"] and "egress" not in text.lower()
                              and not text.startswith("HTTP/1.1 403") or (r["tls_server_hello"] and name.startswith(("3", "4"))))
        r["text_start"] = text[:160].replace(canary, "<canary>")
    rec["results"] = res
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"private-reach-{time.strftime('%H%M%S')}.json")
    with open(path, "w") as fh:
        json.dump(rec, fh, indent=1)
    print(json.dumps(rec, indent=1))


if __name__ == "__main__":
    main()
