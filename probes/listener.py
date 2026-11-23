"""The canary listener: records every request it gets and serves nothing useful. Runs on a small
instance in the VPC of the cluster under test, created and deleted with it.

One JSON line per request to stdout and to the serial console (/dev/ttyS0 when present), which the
harness reads with `gcloud compute instances get-serial-port-output`. Recorded: time, source
address, method, path and query, header names, body size, the body's sha256 and its first 2 KB.
A Google access token in a request (`ya29.` and the like) is never stored: it's replaced by a
marker with its length and hash, so a send can be proven without the listener holding a live
credential. Every response is a bare 204.

Standard library only. usage: python3 listener.py [PORT]
"""
import hashlib
import http.server
import json
import os
import re
import sys
import time

TOKEN = re.compile(r"(ya29\.|ya29_|1//|ey[A-Za-z0-9_-]{10,}\.)[A-Za-z0-9._\-]{20,}")
CONSOLE = "/dev/ttyS0"


def redact(text: str) -> tuple[str, int]:
    n = 0

    def mark(m):
        nonlocal n
        n += 1
        v = m.group(0)
        return f"[google-token redacted len={len(v)} sha256={hashlib.sha256(v.encode()).hexdigest()[:16]}]"
    return TOKEN.sub(mark, text), n


def emit(rec: dict):
    line = json.dumps(rec, sort_keys=True)
    print(line, flush=True)
    if os.path.exists(CONSOLE):
        try:
            with open(CONSOLE, "w") as fh:
                fh.write("LABHIT " + line + "\n")
        except OSError:
            pass


class Handler(http.server.BaseHTTPRequestHandler):
    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, 1 << 20)) if n else b""
        text = body.decode("utf-8", errors="replace")
        path, tokens_path = redact(self.path)
        snippet, tokens_body = redact(text[:2048])
        hdr_auth, tokens_hdr = redact(self.headers.get("Authorization", ""))
        emit({"t": time.time(), "src": self.client_address[0], "method": self.command, "path": path,
              "headers": sorted(self.headers.keys()), "authorization": hdr_auth or None,
              "body_bytes": len(body), "body_sha256": hashlib.sha256(body).hexdigest(),
              "body_start": snippet, "google_tokens_seen": tokens_path + tokens_body + tokens_hdr})
        self.send_response(204)
        self.end_headers()

    do_GET = do_POST = do_PUT = do_HEAD = _record

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    emit({"t": time.time(), "event": "listening", "port": PORT})
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
