"""Only the placeholder metadata shim from agent/actor_server.py, on 127.0.0.1:8081 (LAB_SHIM_PORT)."""
import os
import sys

sys.path.insert(0, "/opt/lab")
import actor_server as AS  # noqa: E402

AS.Threaded(("127.0.0.1", int(os.environ.get("LAB_SHIM_PORT", "8081"))), AS.Shim).serve_forever()
