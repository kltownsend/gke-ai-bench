"""The cb1 few-shot set for the hosted decision readers (Keith, 2026-11-09: "Add the cb1 arm").

The same examples the Gemma and Haiku few-shot cells used (clinicbench/fewshot/cb1.json, Lab 024's
v3 set with Keith's one relabel), given to Bivia and Jev. Neither API has an examples field, so
the question kind's shots go into `state` ahead of the case, each as the frozen prompt the shot
was written in plus its labeled answer. That's an adaptation, not either vendor's native
tuning, and results say so. Everything else in the request, and how an answer is used, is the
zero-shot reader's.

Identity extraction gets no shots: Bivia's /extract takes fields, not examples, and Jev has no
extraction type. Across the recorded Gemma E cells extraction never produced a usable identity.

This module subclasses the zero-shot readers without changing them, so a zero-shot run's reader
hash is untouched. The runner picks it with --reader bivia|jev --prompt fewshot-cb1.
"""

from __future__ import annotations

import json

from . import bivia_model as B
from . import jev_model as J
from .fewshot_model import SHOTS


def examples(shots: dict, kind: str) -> str:
    rows = shots.get(kind) or []
    if not rows:
        return ""
    parts = [f"Example {i}:\n{e['user']}\nAnswer: {e['label']}" for i, e in enumerate(rows, 1)]
    return ("Labeled examples of this question, answered correctly:\n\n" + "\n\n".join(parts)
            + "\n\nNow the case to answer:\n")


def _state(shots, kind, context, utterance):
    return (examples(shots, kind)
            + f"What the software last said to the patient: {context}\nPatient message: {utterance}")


def model_class(reader: str, name: str):
    shots = json.load(open(SHOTS[name]))

    if reader == "bivia":
        class BiviaFewShot(B.BiviaModel):
            def ask(self, kind, context, utterance, question, options):
                labels = {f"{k}: {v}": k for k, v in options.items()}
                labels[B.UNCLEAR] = "unclear"
                body = {"state": _state(shots, kind, context, utterance), "question": question,
                        "choices": list(labels), "mode": self.MODE, "allow_abstain": True}
                r = B._post(self.api_base, "/choice", body)
                self._account(r)
                abstained = r.get("status") == "abstained"
                answer = "unclear" if abstained else labels.get(r.get("value"), "unclear")
                self.usage["abstains"] += abstained
                self.log.append({"turn": self.turn, "kind": kind, "utterance": utterance, "options": list(options),
                                 "answer": answer, "valid": abstained or r.get("value") in labels,
                                 "raw": json.dumps({k: r.get(k) for k in ("value", "probability", "status",
                                                                           "abstain_reason", "model")})[-400:]})
                return answer
        return BiviaFewShot

    if reader == "jev":
        class JevFewShot(J.JevModel):
            def ask(self, kind, context, utterance, question, options):
                criteria = dict(options)
                criteria["unclear"] = J.UNCLEAR
                body = {"model": self.model, "state": _state(shots, kind, context, utterance),
                        "questions": {"answer": {"type": "choice", "instructions": question, "criteria": criteria}}}
                r = J._post(self.api_base, body)
                u = r.get("usage") or {}
                self.usage["llm_calls"] += 1
                self.usage["prompt_tokens"] += u.get("input_tokens", 0)
                self.usage["output_tokens"] += u.get("output_tokens", 0)
                mid = r.get("model")
                if mid and mid not in self.usage["model_ids"]:
                    self.usage["model_ids"].append(mid)
                a = (r.get("answers") or {}).get("answer") or {}
                choice = a.get("choice")
                answer = choice if choice in criteria else "unclear"
                self.log.append({"turn": self.turn, "kind": kind, "utterance": utterance, "options": list(options),
                                 "answer": answer, "valid": choice in criteria,
                                 "raw": json.dumps({"choice": choice, "confidence": a.get("confidence"),
                                                    "probabilities": a.get("probabilities"), "model": mid})[-400:]})
                return answer
        return JevFewShot

    raise ValueError(reader)
