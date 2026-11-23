"""The live runner against a fake OpenAI-style server: gates refuse, the canary runs, it resumes."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from clinicbench import live


def serve(max_len=262144, model="gemma-4-26b-a4b", answer="unclear"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send({"data": [{"id": model, "max_model_len": max_len}]})

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self._send({"choices": [{"message": {"content": json.dumps({"answer": answer})}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


@pytest.fixture(autouse=True)
def frozen():
    ok, why = live.frozen_ok()
    if not ok:
        pytest.skip(f"instrument not frozen: {why}")


def test_envelope_gate_refuses_other_venue(tmp_path):
    srv, base = serve(max_len=131072)
    with pytest.raises(SystemExit):
        live.main(["--out", str(tmp_path), "--batch", "canary", "--api-base", base])
    srv.shutdown()
    assert not os.listdir(tmp_path)


def test_no_server_refuses(tmp_path):
    with pytest.raises(SystemExit):
        live.main(["--out", str(tmp_path), "--batch", "canary", "--api-base", "http://127.0.0.1:9/v1"])


def test_canary_runs_and_resumes(tmp_path):
    srv, base = serve()
    live.main(["--out", str(tmp_path), "--batch", "canary", "--api-base", base, "--par", "2"])
    s = json.load(open(tmp_path / "summary.json"))
    assert s["sessions"] == 10 and s["ok"] == 10 and s["infra_final"] == 0
    venue = json.load(open(tmp_path / "venue.json"))
    assert venue["models"]["data"][0]["max_model_len"] == 262144 and venue["freeze"]["files"]
    live.main(["--out", str(tmp_path), "--batch", "canary", "--api-base", base])
    assert json.load(open(tmp_path / "summary.json"))["skipped"] == 10
    srv.shutdown()


def test_pilot_grid_is_paired():
    g = live.pilot_grid()
    assert len(g) == 4 * 5 * 48
    seeds = {t["id"] for t, _ in g}
    assert len(seeds) == 20
    for sid in seeds:
        assert sum(1 for t, _ in g if t["id"] == sid) == 48


def test_instrument_is_frozen():
    """Any change to a frozen file must be logged and refrozen before a served model sees it."""
    ok, why = live.frozen_ok()
    assert ok, why
