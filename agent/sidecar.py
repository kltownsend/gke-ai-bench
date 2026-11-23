"""The agent sandbox's sidecar: an egress allowlist and a metadata server, nothing else.

The agent container sits on a Docker network with no route out. This sidecar is attached to that
network and to the outside, and it is the agent's only way anywhere:

- **Egress proxy (port 3128).** HTTP CONNECT only, port 443 only. Hosts named exactly in ALLOW
  are tunneled; everything else is refused with 403 and logged. The default is one host,
  aiplatform.googleapis.com (build review 4: a `*.googleapis.com` wildcard admitted Cloud Storage,
  where a signed upload URL needs no credential). agy also tries www.googleapis.com and
  play.googleapis.com; both are refused, and listing models still works (2026-11-20). A CONNECT
  proxy sees hostnames, not paths, so an allowed host is allowed whole. The log is how the lab
  learns which hosts agy contacts.
- **Metadata server (port 80).** Answers the GCE metadata endpoints Google's credential libraries
  use when no credentials file is set. It's a local shim shaped like those endpoints, not GKE
  Workload Identity (no service-account binding, no pod identity), which the GKE rungs test. It
  serves short-lived access tokens for the lab service account from /tokens/token.json, which the
  host refreshes (agent/mint_token.py). No refresh token, user or service account, ever enters the
  agent's container or this one.

Standard library only. Logs one JSON line per decision to stdout.
"""

import json
import os
import re
import select
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOW = [h.strip().lower() for h in os.environ.get("LAB_EGRESS_ALLOW", "aiplatform.googleapis.com").split(",")
         if h.strip()]                                   # exact hostnames, no patterns
TOKEN_FILE = os.environ.get("LAB_TOKEN_FILE", "/tokens/token.json")
PROJECT = os.environ.get("LAB_PROJECT", "")   # passed in by agent/sandbox.py


def log(**kw):
    print(json.dumps(dict(kw, t=round(time.time(), 3))), flush=True)


class Proxy(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(4096).decode("latin-1").strip()
        parts = line.split()
        while self.rfile.readline(4096).strip():
            pass                                   # drop the request headers
        if len(parts) != 3 or parts[0] != "CONNECT":
            log(event="egress", decision="refused", why="not CONNECT", line=line[:120])
            self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, _, port = parts[1].rpartition(":")
        if port != "443" or host.lower() not in ALLOW:
            log(event="egress", decision="denied", host=host, port=port)
            self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        try:
            up = socket.create_connection((host, 443), timeout=15)
        except OSError as e:
            log(event="egress", decision="failed", host=host, error=str(e)[:80])
            self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        log(event="egress", decision="allowed", host=host)
        self.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self.wfile.flush()
        a, b = self.connection, up
        try:
            while True:
                r, _, _ = select.select([a, b], [], [], 300)
                if not r:
                    return
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        finally:
            up.close()


class Metadata(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, ctype="text/plain"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Metadata-Flavor", "Google")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.headers.get("Metadata-Flavor") != "Google" and self.path != "/":
            return self.reply(403, "Missing Metadata-Flavor header")
        path = self.path.split("?")[0].rstrip("/")
        try:
            with open(TOKEN_FILE) as fh:
                tok = json.load(fh)
        except (OSError, ValueError):
            tok = None
        sa = (tok or {}).get("email", "")
        if path in ("", "/", "/computeMetadata/v1"):
            return self.reply(200, "computeMetadata/")
        if path == "/computeMetadata/v1/project/project-id":
            return self.reply(200, PROJECT)
        if path == "/computeMetadata/v1/project/numeric-project-id":
            return self.reply(200, str((tok or {}).get("project_number", "0")))
        if path == "/computeMetadata/v1/instance/service-accounts":
            return self.reply(200, "default/\n" + (sa + "/\n" if sa else ""))
        m = re.match(r"^/computeMetadata/v1/instance/service-accounts/([^/]+)(/.*)?$", path)
        if m and m.group(1) in ("default", sa):
            leaf = m.group(2) or ""
            if leaf == "/email":
                return self.reply(200, sa)
            if leaf == "/scopes":
                return self.reply(200, "https://www.googleapis.com/auth/cloud-platform\n")
            if leaf == "/token":
                if not tok or tok.get("expiry", 0) - time.time() < 60:
                    log(event="metadata", path=path, decision="no fresh token")
                    return self.reply(503, "token not available")
                log(event="metadata", path=path, decision="token served", email=sa)
                return self.reply(200, json.dumps({"access_token": tok["access_token"],
                                                   "expires_in": int(tok["expiry"] - time.time()),
                                                   "token_type": "Bearer"}), "application/json")
            if leaf in ("", "/"):
                return self.reply(200, json.dumps({"email": sa, "aliases": ["default"],
                                                   "scopes": ["https://www.googleapis.com/auth/cloud-platform"]}),
                                  "application/json")
        if path == "/computeMetadata/v1/instance/service-accounts/default/identity":
            return self.reply(404, "identity tokens are not served")
        log(event="metadata", path=path, decision="not found")
        return self.reply(404, "not found")


class ThreadingTCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    threading.Thread(target=ThreadingTCP(("0.0.0.0", 3128), Proxy).serve_forever, daemon=True).start()
    log(event="start", egress_allow=ALLOW, project=PROJECT)
    ThreadingHTTPServer(("0.0.0.0", 80), Metadata).serve_forever()
