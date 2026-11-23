"""Bivia as the driver's reader (Keith, 2026-11-09): a reader swap, like Haiku and the few-shot cb1.

The frozen driver (adapters/dcitl_port.py) asks its reader two things: a closed question with
options, and the patient's identity. This subclass sends them to Articul8's hosted decision model
instead of a chat server. The driver, its questions, its options and what it does with an answer
are unchanged. Nothing in the freeze is touched; the runner picks this class with --reader bivia.

How each call maps:

- ask -> POST /choice. `state` is the same two lines the frozen prompt gives (what the software
  last said, the patient's message); `question` is the driver's question; `choices` are the
  driver's options as "id: description", plus the frozen prompt's own "unclear" option. Bivia
  returns one of them, mapped back to its id. An abstained answer is "unclear", the frozen
  prompt's answer for a message that doesn't clearly support an option, and is logged as an
  abstain. The frozen reader asks once more when an answer falls outside the options; Bivia can
  only return a listed choice, so there's no second ask.
- extract_identity -> POST /extract over the patient's message alone, with descriptive field
  names. Given the clinic's line too, Bivia took spans from it ("ID?" as a member ID). It returns
  spans, so the date is normalized with the driver's own parse_dob. The driver calls this only
  when its deterministic identity parser finds nothing.

The key is read from the Keychain item `bivia` (account $USER), never from a file or the
environment. Transport errors and 429/5xx are retried with backoff; anything still failing
raises, and the runner counts the session as infrastructure, never as a reading.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .adapters import dcitl_port as D

BASE = "https://api.articul8.ai/bivia"
UNCLEAR = "unclear: the message doesn't clearly support one option"
_key_lock = threading.Lock()
_key = None


def _api_key() -> str:
    global _key
    with _key_lock:
        if _key is None:
            r = subprocess.run(["security", "find-generic-password", "-a", os.environ["USER"], "-s", "bivia", "-w"],
                               capture_output=True, text=True)
            if r.returncode != 0 or not r.stdout.strip():
                raise RuntimeError("no Keychain item 'bivia' for this user")
            _key = r.stdout.strip()
        return _key


# The free tier allows 6,000 requests an hour (HTTP 402 entitlement_exceeded past it, found
# 2026-11-09; not in the API reference). Requests are spaced across all threads to stay under it.
REQS_PER_HOUR = float(os.environ.get("BIVIA_REQS_PER_HOUR", "5700"))
_pace_lock = threading.Lock()
_next_slot = 0.0


def _pace():
    global _next_slot
    with _pace_lock:
        now = time.monotonic()
        start = max(now, _next_slot)
        _next_slot = start + 3600.0 / REQS_PER_HOUR
    if start > now:
        time.sleep(start - now)


def _post(base: str, path: str, body: dict, tries: int = 5) -> dict:
    data = json.dumps(body).encode()
    for i in range(tries):
        _pace()
        req = urllib.request.Request(base + path, data=data, method="POST", headers={
            "Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json",
            "User-Agent": "clinicbench"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or i == tries - 1:
                raise RuntimeError(f"bivia {path} HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
            wait = float(e.headers.get("Retry-After") or 0) or 2 ** i
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                raise RuntimeError(f"bivia {path}: {e}") from e
            wait = 2 ** i
        time.sleep(min(wait, 30))
    raise RuntimeError("unreachable")


class BiviaModel(D.Model):
    MODE = "fast"

    def __init__(self, model: str, api_base: str, seed: int):
        super().__init__(model, api_base, seed)
        self.api_base = (api_base or BASE).rstrip("/")
        self.model = "bivia"
        self.usage.update({"billable_tokens": 0, "abstains": 0, "model_ids": []})

    def _account(self, r: dict):
        self.usage["llm_calls"] += 1
        self.usage["prompt_tokens"] += r.get("input_tokens", 0)
        self.usage["billable_tokens"] += r.get("billable_tokens", 0)
        mid = r.get("model")
        if mid and mid not in self.usage["model_ids"]:
            self.usage["model_ids"].append(mid)

    def ask(self, kind: str, context: str, utterance: str, question: str, options: dict[str, str]) -> str:
        labels = {f"{k}: {v}": k for k, v in options.items()}
        labels[UNCLEAR] = "unclear"
        body = {"state": f"What the software last said to the patient: {context}\nPatient message: {utterance}",
                "question": question, "choices": list(labels), "mode": self.MODE, "allow_abstain": True}
        r = _post(self.api_base, "/choice", body)
        self._account(r)
        abstained = r.get("status") == "abstained"
        answer = "unclear" if abstained else labels.get(r.get("value"), "unclear")
        valid = abstained or r.get("value") in labels
        self.usage["abstains"] += abstained
        self.log.append({"turn": self.turn, "kind": kind, "utterance": utterance, "options": list(options),
                         "answer": answer, "valid": valid,
                         "raw": json.dumps({k: r.get(k) for k in ("value", "probability", "status",
                                                                   "abstain_reason", "model")})[-400:]})
        return answer

    def extract_identity(self, context: str, utterance: str) -> dict:
        fields = {"name": "patient's full name", "dob": "date of birth", "member_id": "member ID"}
        r = _post(self.api_base, "/extract", {"state": utterance, "fields": list(fields.values())})
        self._account(r)
        vals = r.get("values") or {}
        out = {}
        for k, f in fields.items():
            v = vals.get(f) or {}
            out[k] = str(v.get("value") or "").strip() if v.get("found") else ""
        if out["dob"]:
            out["dob"] = D.parse_dob(out["dob"]) or ""
        self.log.append({"turn": self.turn, "kind": "identity_extract", "utterance": utterance, "options": [],
                         "answer": json.dumps(out), "valid": bool(vals), "raw": json.dumps(vals)[-400:]})
        return out
