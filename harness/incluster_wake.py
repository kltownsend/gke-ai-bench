#!/usr/bin/env python3
"""Item 3: the first request after a long idle, timed inside the cluster, on the lab's own agent actor.

The in-cluster figure on the site (median 380 ms) came from Google's runner on its glutton benchmark
sessions. This times the lab's coding-agent actor (template lab-agent, 2 CPU, 2 GiB) the same way a
platform's own client would reach it: from a Pod inside the cluster, through atenet-router's
CONNECT-TLS listener, as `kubectl ate proxy` does (TLS to atenet-router.ate-system.svc:8444 with a
pod-identity client certificate, then CONNECT <actor>:8080 with the `ate-target-actor` header), with
no lab tunnel or port-forward in the path.

N actors are created and suspended (sync), left past the 30-minute detach, then each is woken by one
request from the in-cluster client: the clock covers TLS, CONNECT (where the router parks the request
while the actor resumes) and GET /healthz. Each is suspended again after its wake, as the lab's
storage probe does, so the next has a worker. Before the wait, the same client times the same path to
an awake actor, so the connection's own cost is known. The runtime's histogram is read afterwards.

The client Pod runs in benchmarking as `benchmark-runner`, an identity the router's ingress allows. No model calls.
usage: incluster_wake.py [--n 12] [--wait 2100] [--out DIR]
"""
import json
import os
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "agent"))
from substrate_venue import SubstrateVenue  # noqa: E402

# A name unique to each run: a same-named Pod still terminating from an earlier run made the apply a
# no-op, and the Pod vanished before the wakes (2026-11-22, the first item-3 run).
# Google's runner image, built by harness/google_bench.py build (LAB_BENCH_IMAGE, harness/lab_env.sh).
IMAGE = os.environ.get("LAB_BENCH_IMAGE") or sys.exit("LAB_BENCH_IMAGE is not set: source harness/lab_env.sh")
POD = "lab-incluster-client-" + __import__("time").strftime("%H%M%S")
# The router admits only allowlisted SPIFFE identities: ate-system/atenet-client (kubectl ate's, with no
# ServiceAccount object in this install) and benchmarking/benchmark-runner (added by Google's benchmark setup).
NS = "benchmarking"
MANIFEST = f"""
apiVersion: v1
kind: Pod
metadata: {{name: {POD}, namespace: {NS}, labels: {{app: lab-incluster-client}}}}
spec:
  serviceAccountName: benchmark-runner
  restartPolicy: Never
  containers:
  - name: c
    image: {IMAGE}
    command: ["python3", "-c", "import time; time.sleep(14400)"]
    volumeMounts:
    - {{name: servicedns-ca, mountPath: /run/servicedns-ca, readOnly: true}}
    - {{name: podidentity, mountPath: /run/podidentity.podcert.ate.dev, readOnly: true}}
    resources: {{requests: {{cpu: 100m, memory: 128Mi}}}}
  volumes:
  - name: servicedns-ca
    projected:
      sources:
      - clusterTrustBundle:
          signerName: servicedns.podcert.ate.dev/identity
          labelSelector: {{matchLabels: {{podcert.ate.dev/canarying: live}}}}
          path: ca.crt
  - name: podidentity
    projected:
      sources:
      - podCertificate:
          signerName: podidentity.podcert.ate.dev/identity
          keyType: ECDSAP256
          credentialBundlePath: credential-bundle.pem
"""

CLIENT = r"""
import json, socket, ssl, sys, time
actor_ref = sys.argv[1]; name = actor_ref.split("/", 1)[1]
ctx = ssl.create_default_context(cafile="/run/servicedns-ca/ca.crt")
ctx.load_cert_chain("/run/podidentity.podcert.ate.dev/credential-bundle.pem")
t0 = time.perf_counter()
raw = socket.create_connection(("atenet-router.ate-system.svc", 8444), timeout=60)
s = ctx.wrap_socket(raw, server_hostname="atenet-router.ate-system.svc")
t_tls = time.perf_counter()
s.sendall(f"CONNECT {name}:8080 HTTP/1.1\r\nHost: {name}:8080\r\nate-target-actor: {actor_ref}\r\n\r\n".encode())
buf = b""
while b"\r\n\r\n" not in buf:
    b = s.recv(4096)
    if not b: break
    buf += b
t_connect = time.perf_counter()
status = buf.split(b"\r\n", 1)[0].decode("latin-1")
# Read the response by its length, not to EOF: with Connection: close the clock ran on until the
# connection finished closing, about a second, which isn't the response (found 2026-11-22).
s.sendall(f"GET /healthz HTTP/1.1\r\nHost: {name}\r\n\r\n".encode())
resp = b""
while b"\r\n\r\n" not in resp:
    resp += s.recv(4096)
head, _, body = resp.partition(b"\r\n\r\n")
n = int(next(l.split(b":")[1] for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")))
while len(body) < n:
    body += s.recv(4096)
t_get = time.perf_counter()
s.close()
print(json.dumps({"connect_status": status, "get_status": resp.split(b"\r\n", 1)[0].decode("latin-1"),
                  "tls_s": round(t_tls - t0, 4), "connect_s": round(t_connect - t_tls, 4),
                  "get_s": round(t_get - t_connect, 4), "total_s": round(t_get - t0, 4)}))
"""


def kubectl(*a, inp=None, check=True):
    return subprocess.run(["kubectl", *a], input=inp, capture_output=True, text=True, check=check)


def client(actor_ref):
    r = kubectl("-n", NS, "exec", POD, "--", "python3", "-c", CLIENT, actor_ref, check=False)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {"error": (r.stderr or r.stdout)[-300:]}


def arg(n, d):
    return sys.argv[sys.argv.index(n) + 1] if n in sys.argv else d


def main():
    n, wait = int(arg("--n", 12)), int(arg("--wait", 2100))
    out_dir = arg("--out", os.path.join(TOOLS, "runs", "remaining"))
    os.makedirs(out_dir, exist_ok=True)
    rec = {"probe": "incluster_wake", "n": n, "wait_s": wait, "t": time.time(), "awake": [], "wakes": []}
    kubectl("apply", "-f", "-", inp=MANIFEST)
    kubectl("-n", NS, "wait", "--for=condition=Ready", f"pod/{POD}", "--timeout=300s")
    venues = []
    try:
        for i in range(n):
            sb = SubstrateVenue(); sb.__enter__(); venues.append(sb)
            sb.ensure_actor()
            ref = f"{sb.atespace}/{sb.actor}"
            if i == 0:
                rec["awake"] = [client(ref) for _ in range(5)]      # the path's own cost, actor awake
                print(json.dumps({"awake": rec["awake"]}), flush=True)
            sb.ate("suspend", "actor", sb.actor, "-a", sb.atespace, "--mode=sync", timeout=900)
            print(json.dumps({"suspended": i, "actor": sb.actor}), flush=True)
        t_s = time.time()
        time.sleep(wait)
        if kubectl("-n", NS, "get", "pod", POD, check=False).returncode != 0:      # never time without it
            kubectl("apply", "-f", "-", inp=MANIFEST)
            kubectl("-n", NS, "wait", "--for=condition=Ready", f"pod/{POD}", "--timeout=300s")
            rec["client_recreated_before_wakes"] = True
        for i, sb in enumerate(venues):
            r = client(f"{sb.atespace}/{sb.actor}")
            r.update(i=i, actor=sb.actor, idle_s=round(time.time() - t_s))
            rec["wakes"].append(r)
            print(json.dumps(r), flush=True)
            sb.ate("suspend", "actor", sb.actor, "-a", sb.atespace, "--mode=sync", timeout=900, check=False)
    finally:
        for sb in venues:
            sb.__exit__(None, None, None)
        kubectl("-n", NS, "delete", "pod", POD, "--wait=false", check=False)
        tot = sorted(w["total_s"] for w in rec["wakes"] if "total_s" in w)
        if tot:
            rec["summary"] = {"n": len(tot), "median_s": tot[len(tot) // 2] if len(tot) % 2 else
                              round((tot[len(tot) // 2 - 1] + tot[len(tot) // 2]) / 2, 4),
                              "min_s": tot[0], "max_s": tot[-1],
                              "within_500ms": sum(t <= 0.5 for t in tot), "within_1s": sum(t <= 1.0 for t in tot)}
        with open(os.path.join(out_dir, f"incluster-wake-{time.strftime('%H%M%S')}.json"), "w") as fh:
            json.dump(rec, fh, indent=1)
        print(json.dumps(rec.get("summary")), flush=True)


if __name__ == "__main__":
    main()
