"""The system of record: the effects a triage run has on the world, kept outside every venue.

Three effects, each deliberately non-idempotent (no natural dedupe key), so an interrupted run can
genuinely double or drop one: opening a pull request, appending a finding, notifying the approver.
Plus the approval record, keyed by proposal hash.

These are local append-only stand-ins for the lab's Gitea forge and logs. Each effect is recorded
in three parts, so scoring can tell them apart: `invoked` (the controller called it), `acked` (the
service confirmed), and the resulting `state` (what's in the record now).
"""

from __future__ import annotations

import json
import os
import time
import uuid

from hooks import point


class Record:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _append(self, name: str, entry: dict) -> dict:
        entry = dict(entry, at=time.time())
        with open(os.path.join(self.root, f"{name}.jsonl"), "a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return entry

    def read(self, name: str) -> list[dict]:
        p = os.path.join(self.root, f"{name}.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(x) for x in fh if x.strip()]

    # --- traceability: a sample of passing fixes, read later; never a gate ---
    def trace_sample(self, entry: dict) -> dict:
        have = {(e["run"], e["proposal"]) for e in self.read("trace_samples")}
        if (entry["run"], entry["proposal"]) in have:
            return entry
        return self._append("trace_samples", entry)

    def trace_read(self, run_id: str, proposal_hash: str, reader: str, reading: str, note: str = "") -> dict:
        return self._append("trace_reads", {"run": run_id, "proposal": proposal_hash, "by": reader,
                                            "reading": reading, "note": note})

    # --- approvals ---------------------------------------------------------------------------
    def approve(self, proposal_hash: str, who: str) -> dict:
        return self._append("approvals", {"proposal": proposal_hash, "by": who})

    def approved(self, proposal_hash: str) -> bool:
        return any(a["proposal"] == proposal_hash for a in self.read("approvals"))

    def find(self, kind: str, run_id: str, proposal_hash: str) -> dict:
        """What the record holds for one effect of one proposal: written entries and acks.

        Only the hand-built reconcile mode asks this. It works because the system of record can be
        queried (the lab's Gitea API can list pull requests); that's part of what a team builds by
        hand on hardened GKE.
        """
        written = [e for e in self.read(kind) if e["run"] == run_id and e["proposal"] == proposal_hash]
        acked = [e for e in self.read("effects_acked")
                 if e["kind"] == kind and e["run"] == run_id and e["proposal"] == proposal_hash]
        return {"written": written, "acked": acked}

    def adopt(self, kind: str, run_id: str, proposal_hash: str, eid: str) -> dict:
        """Acknowledge an effect that was written but never acked, instead of doing it again."""
        return self._append("effects_acked", {"kind": kind, "run": run_id, "proposal": proposal_hash,
                                              "id": eid, "adopted": True})

    # --- effects: each call creates a new record; nothing dedupes -----------------------------
    def effect(self, kind: str, run_id: str, proposal_hash: str, body: dict) -> dict:
        self._append("effects_invoked", {"kind": kind, "run": run_id, "proposal": proposal_hash})
        eid = f"{kind}-{uuid.uuid4().hex[:8]}"
        self._append(kind, {"id": eid, "run": run_id, "proposal": proposal_hash, **body})
        point("mid_effect")
        return self._append("effects_acked", {"kind": kind, "run": run_id, "proposal": proposal_hash, "id": eid})

    def open_pull_request(self, run_id, proposal_hash, title, diff, files=None):
        return self.effect("pull_requests", run_id, proposal_hash, {"title": title, "diff": diff})

    def append_finding(self, run_id, proposal_hash, finding):
        return self.effect("findings", run_id, proposal_hash, {"finding": finding})

    def notify(self, run_id, proposal_hash, message):
        return self.effect("notifications", run_id, proposal_hash, {"message": message})
