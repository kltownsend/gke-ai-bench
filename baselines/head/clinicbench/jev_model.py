"""Jev as the driver's reader: TypeSafe's hosted decision model, a reader swap like Bivia's.

TypeSafe's System One API (docs.typesafe.ai, read 2026-11-09): POST /v1/systemone with a model, a
state and named typed questions. The frozen driver's reader asks two things, a closed question
with options and the patient's identity. This subclass sends the first to Jev and leaves the
frozen driver, its questions and its options unchanged. Nothing in the freeze is touched; the
runner picks this class with --reader jev.

How each call maps:

- ask -> one `choice` question. `state` is the same two lines the frozen prompt gives (what the
  software last said, the patient's message); `instructions` is the driver's question;
  `criteria` are the driver's options plus the frozen prompt's own "unclear" option. Jev always
  returns one of them; it has no abstain. Its confidence and probabilities are logged.
- extract_identity -> Jev has no extraction type, so this returns nothing found. The driver calls
  it only when its deterministic identity parser finds nothing, which with the template voice is
  a line that isn't giving an identity (see docs/ for how often a recorded reader returned
  something). Stated as a limit of this reader wherever its results appear.

The model is pinned to a versioned id (`jev-1.14.0`); every response's own `model` field is
logged, since aliases move. The key is read from the Keychain item `jev` (account $USER).
429 and 529 are retried with backoff, as TypeSafe's docs say to.
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

BASE = "https://api.typesafe.ai"
MODEL_ID = os.environ.get("JEV_MODEL", "jev-1.14.0")
UNCLEAR = "the message doesn't clearly support one option"
_key_lock = threading.Lock()
_key = None


def _api_key() -> str:
    global _key
    with _key_lock:
        if _key is None:
            r = subprocess.run(["security", "find-generic-password", "-a", os.environ["USER"], "-s", "jev", "-w"],
                               capture_output=True, text=True)
            if r.returncode != 0 or not r.stdout.strip():
                raise RuntimeError("no Keychain item 'jev' for this user")
            _key = r.stdout.strip()
        return _key


def _post(base: str, body: dict, tries: int = 6) -> dict:
    data = json.dumps(body).encode()
    for i in range(tries):
        req = urllib.request.Request(base + "/v1/systemone", data=data, method="POST", headers={
            "Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json",
            "User-Agent": "clinicbench"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 529) or i == tries - 1:
                raise RuntimeError(f"jev HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
            wait = float(e.headers.get("Retry-After") or 0) or 2 ** i
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                raise RuntimeError(f"jev: {e}") from e
            wait = 2 ** i
        time.sleep(min(wait, 30))
    raise RuntimeError("unreachable")


class JevModel(D.Model):
    def __init__(self, model: str, api_base: str, seed: int):
        super().__init__(model, api_base, seed)
        self.api_base = (api_base or BASE).rstrip("/")
        self.model = MODEL_ID
        self.usage.update({"model_ids": []})

    def ask(self, kind: str, context: str, utterance: str, question: str, options: dict[str, str]) -> str:
        criteria = dict(options)
        criteria["unclear"] = UNCLEAR
        body = {"model": self.model,
                "state": f"What the software last said to the patient: {context}\nPatient message: {utterance}",
                "questions": {"answer": {"type": "choice", "instructions": question, "criteria": criteria}}}
        r = _post(self.api_base, body)
        u = r.get("usage") or {}
        self.usage["llm_calls"] += 1
        self.usage["prompt_tokens"] += u.get("input_tokens", 0)
        self.usage["output_tokens"] += u.get("output_tokens", 0)
        mid = r.get("model")
        if mid and mid not in self.usage["model_ids"]:
            self.usage["model_ids"].append(mid)
        a = (r.get("answers") or {}).get("answer") or {}
        choice = a.get("choice")
        valid = choice in criteria
        answer = choice if valid else "unclear"
        self.log.append({"turn": self.turn, "kind": kind, "utterance": utterance, "options": list(options),
                         "answer": answer, "valid": valid,
                         "raw": json.dumps({"choice": choice, "confidence": a.get("confidence"),
                                            "probabilities": a.get("probabilities"), "model": mid})[-400:]})
        return answer

    def extract_identity(self, context: str, utterance: str) -> dict:
        out = {"name": "", "dob": "", "member_id": ""}
        self.log.append({"turn": self.turn, "kind": "identity_extract", "utterance": utterance, "options": [],
                         "answer": json.dumps(out), "valid": False, "raw": "jev has no extraction type"})
        return out
