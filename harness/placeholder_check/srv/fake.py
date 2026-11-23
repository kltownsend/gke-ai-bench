"""Fake metadata (port 80, placeholder token) and fake Google front end (443, logs and refuses)."""
import http.server, json, os, ssl, threading, socketserver, sys, time
# Any project will do: nothing here reaches Google. run.sh passes LAB_PROJECT in.
PROJECT = os.environ.get("FAKE_PROJECT", "example-project")
EMAIL = "lab-agent@" + PROJECT + ".iam.gserviceaccount.com"
def log(**k): print("EVT", json.dumps(k), flush=True)
class Meta(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        p=self.path.split("?")[0]; log(srv="metadata", path=p)
        body=None
        if p.endswith("/token"): body=json.dumps({"access_token":"ate-placeholder","expires_in":3599,"token_type":"Bearer"}); ct="application/json"
        elif p.endswith("/email"): body=EMAIL; ct="text/plain"
        elif p.endswith("/scopes"): body="https://www.googleapis.com/auth/cloud-platform"; ct="text/plain"
        elif p.endswith("project-id"): body=PROJECT; ct="text/plain"
        elif p.rstrip("/").endswith("service-accounts"): body="default/\n"+EMAIL+"/\n"; ct="text/plain"
        elif p.rstrip("/").endswith("/default") : body=json.dumps({"email":EMAIL,"scopes":["https://www.googleapis.com/auth/cloud-platform"],"aliases":["default"]}); ct="application/json"
        elif p in ("/","/computeMetadata/v1","/computeMetadata/v1/"): body="computeMetadata/"; ct="text/plain"
        if body is None: self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Metadata-Flavor","Google"); self.send_header("Content-Type",ct); self.end_headers(); self.wfile.write(body.encode())
class Front(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _any(self):
        n=int(self.headers.get("Content-Length") or 0); self.rfile.read(n) if n else None
        log(srv="front", method=self.command, host=self.headers.get("Host"), path=self.path[:120], auth=(self.headers.get("Authorization") or "")[:40])
        self.send_response(401); self.send_header("Content-Type","application/json"); self.end_headers()
        self.wfile.write(json.dumps({"error":{"code":401,"message":"lab fake front end: placeholder seen","status":"UNAUTHENTICATED"}}).encode())
    do_GET=do_POST=do_PUT=_any
class T(socketserver.ThreadingMixIn, http.server.HTTPServer): daemon_threads=True
m=T(("",80),Meta); threading.Thread(target=m.serve_forever,daemon=True).start()
m2=T(("",8081),Meta); threading.Thread(target=m2.serve_forever,daemon=True).start()
f=T(("",443),Front); ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain("/srv/leaf.pem","/srv/leaf.key")
def sni(sock, name, c): log(srv="tls", sni=name)
ctx.sni_callback=sni
f.socket=ctx.wrap_socket(f.socket, server_side=True)
log(srv="up"); f.serve_forever()
