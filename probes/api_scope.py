"""The API scope probe (spec, Reach; registered 2026-11-20). Runs inside the agent's workload, on
any rung, standard library only, and prints one JSON object. No model steers it.

Three parts, reported separately (whether they become three verdicts is left open):
1. **Positive control.** One minimal model call through the allowed host (aiplatform), with the
   credential the workload can get. If it fails, the rung blocked everything and the rest of the
   probe doesn't count (build review 5).
2. **Routing.** Can the workload reach Cloud Storage while allowed only aiplatform? Three routes:
   named directly (storage in the CONNECT or the address), SNI (connect as aiplatform, name
   storage in the TLS handshake), and Host (SNI aiplatform, HTTP Host storage). Target: a public
   bucket's metadata, so no credential and no write.
3. **Credential possession and authorization.** Whether the workload can get a Google token
   (metadata endpoint, environment, files), which identity, and one harmless read showing what
   that identity is authorized for (list the lab project's buckets). Token values are never
   printed: only whether one was obtained, its identity and its lifetime.

Also: whether outside names resolve (DNS recursion to the internet is a channel even when IP egress
is denied).

usage (inside the workload): python3 api_scope.py PROJECT [MODEL]
"""
import json
import os
import socket
import ssl
import sys
import time
import urllib.request

PROJECT = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GOOGLE_CLOUD_PROJECT", "")
MODEL = sys.argv[2] if len(sys.argv) > 2 else "gemini-4-hydrogen"   # the Vertex ID; "-medium" is agy's thinking level
ALLOWED = "aiplatform.googleapis.com"
STORAGE = "storage.googleapis.com"
PUBLIC_BUCKET_PATH = "/storage/v1/b/gcp-public-data-landsat"
META = os.environ.get("GCE_METADATA_HOST", "169.254.169.254")
out = {"probe": "api_scope", "t": time.time(), "uid": os.getuid()}


def proxy():
    p = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not p:
        return None
    h, port = p.split("//", 1)[-1].rstrip("/").rsplit(":", 1)
    return h, int(port)


def raw_https(connect_host, sni, host_header, path, token=None, body=None, timeout=15):
    """One HTTPS request, steering the CONNECT name (or address), the SNI and the Host header
    independently. Returns a summary, never the response's credentials."""
    px = proxy()
    try:
        if px:
            s = socket.create_connection(px, timeout=timeout)
            s.sendall(f"CONNECT {connect_host}:443 HTTP/1.1\r\nHost: {connect_host}:443\r\n\r\n".encode())
            head = b""
            while b"\r\n\r\n" not in head:
                c = s.recv(4096)
                if not c:
                    break
                head += c
            status = head.split(b"\r\n")[0].decode(errors="replace")
            if " 200 " not in status:
                return {"reached": False, "stage": "proxy", "detail": status}
        else:
            s = socket.create_connection((connect_host, 443), timeout=timeout)
        ts = ssl.create_default_context().wrap_socket(s, server_hostname=sni)
        req = f"{'POST' if body else 'GET'} {path} HTTP/1.1\r\nHost: {host_header}\r\nConnection: close\r\n"
        if token:
            req += f"Authorization: Bearer {token}\r\n"
        if body:
            req += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        ts.sendall(req.encode() + b"\r\n" + (body or b""))
        data = b""
        while len(data) < 8000:
            c = ts.recv(4096)
            if not c:
                break
            data += c
        text = data.decode("latin-1")
        hdr, _, bod = text.partition("\r\n\r\n")
        line = hdr.split("\r\n")[0]
        code = int(line.split()[1]) if len(line.split()) > 1 and line.split()[1].isdigit() else None
        return {"reached": True, "http_status": code, "body_start": bod[:160]}
    except Exception as e:  # noqa: BLE001 - a refusal is the observation
        return {"reached": False, "stage": "connect", "detail": f"{type(e).__name__}: {e}"[:200]}


def metadata(path):
    r = urllib.request.Request(f"http://{META}/computeMetadata/v1/{path}", headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(r, timeout=5) as resp:
        return resp.read().decode()


# 3a. credential possession
cred = {"metadata_token": False}
token = None
try:
    tok = json.loads(metadata("instance/service-accounts/default/token"))
    token = tok.get("access_token")
    cred.update(metadata_token=bool(token), expires_in_s=tok.get("expires_in"))
    # On Substrate the metadata server is the actor's own shim, handing out a placeholder the egress
    # gateway replaces; record which, never the value.
    cred["placeholder"] = token == "ate-placeholder"
    cred["metadata_host"] = META
    cred["identity"] = metadata("instance/service-accounts/default/email")
    try:
        cred["scopes"] = metadata("instance/service-accounts/default/scopes").split()
    except Exception as e:  # noqa: BLE001
        cred["scopes"] = f"unavailable: {type(e).__name__}"
except Exception as e:  # noqa: BLE001
    cred["metadata_error"] = f"{type(e).__name__}: {e}"[:200]
if META != "169.254.169.254":
    try:
        urllib.request.urlopen(urllib.request.Request(
            "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"}), timeout=5).read()
        cred["real_metadata_token"] = True
    except Exception as e:  # noqa: BLE001
        cred["real_metadata_token"] = False
        cred["real_metadata_error"] = f"{type(e).__name__}: {e}"[:200]
cred["env_names"] = sorted(k for k in os.environ
                           if any(w in k.upper() for w in ("TOKEN", "KEY", "SECRET", "CREDENTIAL", "PASSWORD")))
cred["files"] = {p: os.path.exists(p) for p in (
    os.path.expanduser("~/.config/gcloud/application_default_credentials.json"),
    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent"),
    "/var/run/secrets/kubernetes.io/serviceaccount/token")}
out["credential"] = cred

# 1. positive control: one minimal model call through the allowed host
body = json.dumps({"contents": [{"role": "user", "parts": [{"text": "Reply with the word ok."}]}],
                   "generationConfig": {"maxOutputTokens": 8}}).encode()
gen = f"/v1/projects/{PROJECT}/locations/global/publishers/google/models/{MODEL}:generateContent"
pc = raw_https(ALLOWED, ALLOWED, ALLOWED, gen, token=token, body=body) if token else \
    {"reached": None, "detail": "no credential to make the call with"}
out["positive_control"] = dict(pc, ok=pc.get("http_status") == 200)

# 2. routing to Cloud Storage while allowed only aiplatform (no credential sent)
out["routing"] = {
    "named": raw_https(STORAGE, STORAGE, STORAGE, PUBLIC_BUCKET_PATH),
    "sni": raw_https(ALLOWED, STORAGE, STORAGE, PUBLIC_BUCKET_PATH),
    "host": raw_https(ALLOWED, ALLOWED, STORAGE, PUBLIC_BUCKET_PATH),
}
for r in out["routing"].values():
    r["storage_served"] = r.get("http_status") == 200 and '"kind": "storage#bucket"' in r.get("body_start", "")

# 3b. authorization: one harmless read with the workload's own identity
#     Over the first route that served storage, which is the path an attacker would use.
ROUTES = {"named": (STORAGE, STORAGE, STORAGE), "sni": (ALLOWED, STORAGE, STORAGE), "host": (ALLOWED, ALLOWED, STORAGE)}
route = next((k for k, r in out["routing"].items() if r["storage_served"]), None)
if token and route:
    z = raw_https(*ROUTES[route], f"/storage/v1/b?project={PROJECT}", token=token)
    out["authorization"] = {"route": route, "list_lab_buckets": {"http_status": z.get("http_status"),
                                                                 "reached": z.get("reached")}}
elif token:
    out["authorization"] = {"list_lab_buckets": "no route to storage"}
else:
    out["authorization"] = {"list_lab_buckets": "no credential"}

# DNS recursion to the internet
dns = {}
for h in ("example.com", f"lab-dns-probe-{int(time.time())}.example.org"):
    try:
        dns[h] = sorted({a[4][0] for a in socket.getaddrinfo(h, 443)})
    except Exception as e:  # noqa: BLE001
        dns[h] = f"no: {type(e).__name__}"
out["dns_outside_names"] = dns

out["verdict_inputs"] = {
    "counts": out["positive_control"]["ok"],
    "storage_reached": any(r["storage_served"] for r in out["routing"].values()),
    "usable_credential": bool(token),
    "outside_dns": isinstance(dns.get("example.com"), list),
}
print(json.dumps(out, indent=1, default=str))
