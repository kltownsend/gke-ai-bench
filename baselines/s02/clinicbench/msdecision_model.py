"""Microsoft-Decision-1.1 as the driver's reader: Microsoft's hosted decision model on Foundry.

Microsoft's System One API (learn.microsoft.com, use-foundry-models-microsoft-decision, updated
2026-11-17): POST <resource>/providers/microsoft/v1/systemone with a model (the deployment name),
a state and named typed questions. It's the same request shape as TypeSafe's (clinicbench/
jev_model.py), so the mapping is Jev's, unchanged:

- ask -> one `choice` question. `state` is the same two lines the frozen prompt gives (what the
  software last said, the patient's message); `instructions` is the driver's question;
  `criteria` are the driver's options plus the frozen prompt's own "unclear" option. The model
  always returns one of them; it has no abstain. Its probabilities are logged.
- extract_identity -> there's no extraction type, so this returns nothing found, as with Jev. The
  driver calls it only when its deterministic identity parser finds nothing. Stated as a limit
  of this reader wherever its results appear.

Calls go to scripts/azure_proxy.py (MAI_API=passthrough), which adds the Entra token, enforces
the token cap and logs every call; this class holds no credential. Every response's own `model`
field is logged. Microsoft's docs don't name the usage fields; input_tokens/output_tokens are
read as TypeSafe's are, and the proxy logs the raw usage object so a mismatch shows.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .adapters import dcitl_port as D

BASE = os.environ.get("MSDECISION_BASE", "http://127.0.0.1:4020")
PATH = "/providers/microsoft/v1/systemone"
MODEL_ID = os.environ.get("MAI_DEPLOYMENT", "decision-1-1")
UNCLEAR = "the message doesn't clearly support one option"


def _post(base: str, body: dict, tries: int = 6) -> dict:
    data = json.dumps(body).encode()
    for i in range(tries):
        req = urllib.request.Request(base + PATH, data=data, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": "clinicbench"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 503 from the proxy after a cap hit stays an error: the session is saved as infra, not scored
            if e.code not in (429, 500, 502, 504) or i == tries - 1:
                raise RuntimeError(f"msdecision HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
            wait = float(e.headers.get("Retry-After") or 0) or 2 ** i
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1:
                raise RuntimeError(f"msdecision: {e}") from e
            wait = 2 ** i
        time.sleep(min(wait, 30))
    raise RuntimeError("unreachable")


class MsDecisionModel(D.Model):
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
        self.usage["prompt_tokens"] += u.get("input_tokens", u.get("prompt_tokens", 0))
        self.usage["output_tokens"] += u.get("output_tokens", u.get("completion_tokens", 0))
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
                         "answer": json.dumps(out), "valid": False, "raw": "msdecision has no extraction type"})
        return out
