"""Arm F: the code-owned triage loop (DCITL).

The controller runs seven stages and won't move past one until its check passes. The model is
called at two of them, and only there:

  1. reproduce   code     the case's visible probes fail on the workspace, as the ticket says
  2. hypothesis  MODEL    a structured cause: kind, files, symbols, cause text; the workspace must
                          be untouched (an edit here is reverted and logged)
  3. work        MODEL    a fix edits only the files the hypothesis named; a finding or a
                          not-the-driver answer edits nothing (anything else is reverted, logged,
                          and costs an attempt)
  4. validate    code     the judge (validator/validate.py); wrong goes back to 2 with booleans
                          only, never hidden inputs or the key, inside the retry budget
  5. propose     code     the change, the evidence and the frozen-file report, hashed
  6. approve     code     a person approves when a trigger fires: a frozen file changed, or any
                          finding. A spent retry budget escalates the run instead (no proposal)
  7. effect      code     open the pull request, append the finding, notify the approver; never
                          without the approval a trigger required

The controller writes the contract-change entry itself from the hypothesis's cause and the real
file hashes: the structured part belongs to code. The model never runs the validator, never
decides it's done, and never performs an effect.

State is written at every transition (stage, attempt, proposal, approval, effects done), so an
interrupted run can resume from it. Which owner of that state wins on Executor is a lab question,
not something this file decides.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOOLS, "validator"))
import validate as V  # noqa: E402
from hooks import point  # noqa: E402

KINDS = V.KINDS
# Artifacts a well-behaved agent leaves by running tests or linters. They're cleared, and logged,
# before the scope check (the first real Antigravity run fixed the bug, ran pytest, and its
# .pytest_cache made the scope rule revert a correct fix).
ARTIFACT_DIRS = {".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache", ".hypothesis"}
ARTIFACT_SUFFIXES = (".pyc", ".pyo")
MAX_RESTARTS = 3          # crash restarts per run
ARTIFACT_KEEP_BYTES = 20 * 1024 * 1024     # copied into the record before clearing, per clearing
ARTIFACT_MAX_FILES, ARTIFACT_MAX_BYTES = 2000, 100 * 1024 * 1024   # beyond this, left for hygiene
# The diagnosis is scored on named symbols; a random 1 in TRACE_RATE passing
# fixes is set aside for a person to read the cause against the fix. Traceability, not a control:
# the reading never changes a verdict. Selection is a hash of the run id, so nobody chooses it.
TRACE_RATE = 10


def trace_sampled(run_id: str, attempt: int) -> bool:
    return int(hashlib.sha256(f"{run_id}:{attempt}".encode()).hexdigest()[:8], 16) % TRACE_RATE == 0


class Controller:
    def __init__(self, case_dir, workspace, model, record, budget=3, approver=None, run_id=None,
                 effects_mode="naive"):
        self.case_dir = case_dir
        self.ws = workspace
        self.model = model
        self.record = record
        self.budget = budget
        self.approver = approver            # callable(proposal) -> name of approver, or None
        # "naive": do each effect; on resume, skip all effects if any was acked.
        # "reconcile": the hand-built fix: before each effect, ask the system of record what's
        # already there; skip what was acked, adopt what was written but never acked.
        self.effects_mode = effects_mode
        self.case = V.load(os.path.join(case_dir, "case.json"))
        self.baseline = os.path.join(TOOLS, "baselines", self.case["baseline"])
        if hasattr(record, "base"):
            record.base = self.case["baseline"]      # the forge opens pull requests into this branch
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
        self.state_path = os.path.join(record.root, "runs", f"{self.run_id}.json")
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        self.state = {"run": self.run_id, "case": self.case["id"], "stage": "start", "attempt": 0,
                      "events": [], "proposal": None, "approved": None, "effects": []}

    # --- bookkeeping ------------------------------------------------------------------------
    def log(self, stage, event, **detail):
        self.state["events"].append({"t": time.time(), "stage": stage, "event": event, **detail})
        self.save()

    def enter(self, stage):
        self.state["stage"] = stage
        self.log(stage, "enter")

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, indent=1, default=str)
        os.replace(tmp, self.state_path)

    def snapshot(self):
        d = os.path.join(self.record.root, "snapshots", self.run_id)
        if os.path.exists(d):
            shutil.rmtree(d)
        shutil.copytree(self.ws, d)
        return d

    def restore(self, snap):
        shutil.rmtree(self.ws)
        shutil.copytree(snap, self.ws)

    def changed(self):
        return V.changed_files(self.ws, self.baseline)

    def clear_artifacts(self, stage="?"):
        """Clear what tests and linters leave behind, durably and only when it looks like a cache.

        Build reviews 3 and 4: clearing erased evidence, logged 20 paths, saved its manifest only
        after deleting, had no quotas, and cleared a cache-named symlink that hygiene should have
        failed. Now:

        - A candidate is a directory named in ARTIFACT_DIRS, or a file ending in ARTIFACT_SUFFIXES.
        - It's left in place, for hygiene and the scope check to judge, if it is or contains a
          symlink or anything that isn't a plain file or directory, or if the clearing would pass
          ARTIFACT_MAX_FILES or ARTIFACT_MAX_BYTES. Real caches are neither.
        - Every entry (directories too: kind, size, sha256, full link target) goes into a manifest
          written to record/artifacts/RUN/ATTEMPT-STAGE/manifest.json and fsynced, files are
          copied beside it up to ARTIFACT_KEEP_BYTES, and only then is anything deleted.

        Clearing is a convenience for honest agents, not scope enforcement: what isn't cleared is
        judged, and the sandbox keeps writes inside the workspace."""
        candidates = []
        for d, dirs, files in os.walk(self.ws, followlinks=False):
            for x in list(dirs):
                p = os.path.join(d, x)
                if os.path.islink(p):
                    dirs.remove(x)                       # never followed; hygiene sees it
                elif x in ARTIFACT_DIRS:
                    candidates.append(p)
                    dirs.remove(x)
            for f in files:
                p = os.path.join(d, f)
                if f.endswith(ARTIFACT_SUFFIXES) and not os.path.islink(p):
                    candidates.append(p)
        if not candidates:
            return []
        victims, entries, left, n, size = [], [], [], 0, 0
        for v in candidates:
            mine, odd = self._collect(v)
            n2 = n + len(mine)
            s2 = size + sum(e.get("size", 0) for e in mine)
            if odd or n2 > ARTIFACT_MAX_FILES or s2 > ARTIFACT_MAX_BYTES:
                left.append({"path": os.path.relpath(v, self.ws),
                             "why": "contains a symlink, hard link or special file" if odd
                             else "over the artifact quota"})
                continue
            victims.append(v)
            entries += mine
            n, size = n2, s2
        keep = os.path.join(self.record.root, "artifacts", self.run_id, f"{self.state['attempt']}-{stage}")
        os.makedirs(keep, exist_ok=True)
        kept = 0
        for e in entries:
            if e["kind"] == "file" and kept + e["size"] <= ARTIFACT_KEEP_BYTES:
                dst = os.path.join(keep, "files", e["path"])
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(os.path.join(self.ws, e["path"]), dst, follow_symlinks=False)
                kept += e["size"]
                e["kept"] = True
        rec = {"stage": stage, "attempt": self.state["attempt"], "entries": entries, "left_in_place": left}
        with open(os.path.join(keep, "manifest.json"), "w") as fh:
            json.dump(rec, fh, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        self.state.setdefault("artifacts_cleared", []).append(rec)
        self.save()                                       # durable before anything is deleted
        for v in victims:
            if os.path.isdir(v):
                shutil.rmtree(v)
            else:
                os.unlink(v)
        return entries

    def _collect(self, v):
        """Every entry under a candidate, and whether anything in it isn't a plain file or dir."""
        if os.path.islink(v) or not os.path.isdir(v):
            e = self._artifact_entry(v)
            return [e], e["kind"] != "file" or e.get("nlink", 1) > 1
        out, odd = [self._artifact_entry(v)], False
        for d, dirs, files in os.walk(v, followlinks=False):
            for x in dirs + files:
                e = self._artifact_entry(os.path.join(d, x))
                out.append(e)
                odd = odd or e["kind"] not in ("file", "dir") or e.get("nlink", 1) > 1
        return out, odd

    def _artifact_entry(self, p):
        rel = os.path.relpath(p, self.ws)
        st = os.lstat(p)
        if stat.S_ISLNK(st.st_mode):
            return {"path": rel, "kind": "symlink", "target": os.readlink(p)}
        if stat.S_ISDIR(st.st_mode):
            return {"path": rel, "kind": "dir", "mode": oct(st.st_mode & 0o7777)}
        if stat.S_ISREG(st.st_mode):
            return {"path": rel, "kind": "file", "size": st.st_size, "sha256": V.sha(p),
                    "nlink": st.st_nlink, "mode": oct(st.st_mode & 0o7777)}
        return {"path": rel, "kind": "other", "mode": oct(st.st_mode)}

    @staticmethod
    def normalize(h):
        """The judge's strict schema (validate.check_answer), which also prefixes symbols named
        relative to the package. Returns (clean hypothesis or None, problems)."""
        return V.check_answer(h, hypothesis=True)

    # --- stages -----------------------------------------------------------------------------
    def reproduce(self):
        self.enter("reproduce")
        r = V.run_probes(V.case_module(self.case_dir), self.ws, self.baseline, "visible")
        self.log("reproduce", "result", **r)
        return r["failures"] > 0

    def hypothesis(self, feedback):
        self.enter("hypothesis")
        snap = self.snapshot()
        h = self.model.hypothesize({"ticket": self.case["ticket"], "workspace": self.ws, "repair": self.case.get("mode") == "repair",
                                    "feedback": feedback, "attempt": self.state["attempt"]})
        cleared = self.clear_artifacts("hypothesis")
        if cleared:
            self.log("hypothesis", "cleared artifacts", count=len(cleared),
                     files=[e["path"] for e in cleared])
        if V.tree_hashes(self.ws) != V.tree_hashes(snap):
            self.restore(snap)
            self.log("hypothesis", "blocked", why="workspace edited during hypothesis; reverted")
        clean, problems = self.normalize(h)
        if clean is not None and "cause" not in clean:
            clean, problems = None, ["cause is required"]
        self.state["hypothesis_problems"] = problems
        self.log("hypothesis", "result", ok=clean is not None, hypothesis=clean if clean is not None else h,
                 problems=problems)
        point("after_hypothesis")
        return clean

    def work(self, h, feedback):
        self.enter("work")
        snap = self.snapshot()
        out = self.model.work({"ticket": self.case["ticket"], "workspace": self.ws, "repair": self.case.get("mode") == "repair",
                               "feedback": feedback, "attempt": self.state["attempt"]}, h) or {}
        cleared = self.clear_artifacts("work")
        if cleared:
            self.log("work", "cleared artifacts", count=len(cleared),
                     files=[e["path"] for e in cleared])
        changed = [p for p in self.changed() if p != V.CONTRACT_LOG]
        if h["kind"] != "fix" and changed:
            self.restore(snap)
            self.log("work", "blocked", why=f"{h['kind']} may not change files", files=changed)
            return None
        outside = [p for p in changed if p not in h.get("files", [])]
        if outside:
            self.restore(snap)
            self.log("work", "blocked", why="changes outside the hypothesis's files", files=outside)
            return None
        if h["kind"] == "fix":
            if not changed:
                self.log("work", "blocked", why="fix made no change")
                return None
            self.write_contract_entry(h, changed)
        self.log("work", "result", changed=changed)
        return out

    def write_contract_entry(self, h, changed):
        frozen = V.load(os.path.join(self.baseline, "docs", "phase3-freeze.json"))["files"]
        touched = [p for p in changed if p in frozen]
        if not touched:
            return
        cause = " ".join(str(h.get("cause", "")).split())[:300] or "see proposal"
        lines = "".join(f"Changed: {p} {V.sha(os.path.join(self.ws, p))}\n" for p in touched)
        with open(os.path.join(self.ws, V.CONTRACT_LOG), "a") as fh:
            fh.write(f"\n## Triage {self.run_id}: {self.case['id']}\nReason: {cause}\n{lines}")

    def validate(self, h):
        self.enter("validate")
        code = [p for p in self.changed() if p != V.CONTRACT_LOG]
        answer = {"kind": h["kind"], "files": code, "symbols": h.get("symbols", []),
                  "cause": h.get("cause"), "attribution": h.get("attribution")}
        v = V.validate(self.case_dir, self.ws, answer)
        self.state["attempts_completed"] = self.state["attempt"]
        fb = {k: v["checks"].get(k) for k in ("clean_tree", "in_scope", "visible_fixed", "hidden_fixed",
                                              "no_regression", "frozen_change_logged",
                                              "nothing_changed", "no_cross_case_regression",
                                              "no_new_wrong_writes") if k in v["checks"]}
        if "detail" in v:
            fb["regression_differences"] = v["detail"]["regression"]["differences"]
        self.log("validate", "result", outcome=v["outcome"], feedback=fb)
        return v, answer, fb

    def propose(self, h, v, answer):
        self.enter("propose")
        diff = []
        for p in answer["files"] + ([V.CONTRACT_LOG] if V.CONTRACT_LOG in self.changed() else []):
            a = os.path.join(self.baseline, p)
            b = os.path.join(self.ws, p)
            old = open(a).read().splitlines(keepends=True) if os.path.exists(a) else []
            new = open(b).read().splitlines(keepends=True) if os.path.exists(b) else []
            diff += difflib.unified_diff(old, new, f"a/{p}", f"b/{p}")
        proposal = {"case": self.case["id"], "kind": h["kind"], "cause": h.get("cause"),
                    "files": answer["files"], "symbols": answer["symbols"],
                    "frozen_touched": v.get("frozen_touched", []), "outcome": v["outcome"],
                    "diff": "".join(diff),
                    "artifacts_cleared": self.state.get("artifacts_cleared", [])}
        ph = hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
        self.state["proposal"] = {"hash": ph, **proposal}
        self.state["hypothesis"] = h
        sampled = h["kind"] == "fix" and trace_sampled(self.run_id, self.state["attempt"])
        if sampled:
            self.record.trace_sample({"run": self.run_id, "proposal": ph, "case": self.case["id"],
                                      "attempt": self.state["attempt"], "symbols": answer["symbols"],
                                      "cause": h.get("cause"), "diff": proposal["diff"],
                                      "outcome": v["outcome"]})
        self.log("propose", "result", hash=ph, trace_sample=sampled)
        return proposal, ph

    def needs_approval(self, h, v):
        return bool(v.get("frozen_touched")) or h["kind"] == "finding"

    def approve(self, proposal, ph):
        self.enter("approve")
        if self.record.approved(ph):
            self.state["approved"] = True
            self.log("approve", "already approved")
            return True
        who = self.approver(proposal) if self.approver else None
        if not who:
            self.state["approved"] = False
            self.log("approve", "waiting", why="no approval yet")
            return False
        self.record.approve(ph, who)
        self.state["approved"] = True
        self.log("approve", "approved", by=who)
        point("after_approval")
        return True

    def effect(self, h, proposal, ph, approval_required):
        self.enter("effect")
        if approval_required and not self.record.approved(ph):
            self.log("effect", "blocked", why="no approval for this proposal")   # invariant
            return
        plan = []
        if h["kind"] == "fix":
            rels = list(proposal["files"]) + ([V.CONTRACT_LOG] if V.CONTRACT_LOG in self.changed() else [])
            files = {}
            for rel in rels:
                with open(os.path.join(self.ws, rel)) as fh:
                    files[rel] = fh.read()
            plan.append(("pull_requests", lambda: self.record.open_pull_request(
                self.run_id, ph, f"{self.case['id']}: {h.get('cause', '')[:80]}", proposal["diff"], files)))
        if h["kind"] == "finding":
            plan.append(("findings", lambda: self.record.append_finding(
                self.run_id, ph, {"case": self.case["id"], "cause": h.get("cause"), "symbols": h.get("symbols")})))
        plan.append(("notifications", lambda: self.record.notify(
            self.run_id, ph, f"{self.case['id']} {h['kind']}: {proposal['outcome']}")))
        for i, (kind, do) in enumerate(plan):
            if i == 1:
                point("between_effects")
            if self.effects_mode == "reconcile":
                have = self.record.find(kind, self.run_id, ph)
                if have["acked"]:
                    self.log("effect", "skip", kind=kind, why="already acked")
                    continue
                if have["written"]:
                    r = self.record.adopt(kind, self.run_id, ph, have["written"][-1]["id"])
                    self.log("effect", "adopted", kind=kind, id=r["id"])
                    self.state["effects"].append(r)
                    continue
            self.state["effects"].append(do())
        self.log("effect", "done", count=len(self.state["effects"]))
        point("after_effects")

    # --- resume ---------------------------------------------------------------------------
    @classmethod
    def load(cls, case_dir, workspace, model, record, run_id, **kw):  # kw: approver, effects_mode
        c = cls(case_dir, workspace, model, record, run_id=run_id, **kw)
        with open(c.state_path) as fh:
            c.state = json.load(fh)
        return c

    def resume(self):
        """Pick a saved run back up from its state file.

        Only a run that stopped waiting for approval resumes past its proposal: the proposal, its
        hash and the hypothesis come from the saved state, so nothing is recomputed and the model
        isn't called again. Any other stage starts over from reproduce. Effects already recorded
        for this proposal are not repeated by the controller; whether a runtime's own replay
        repeats them is what the interruption tests measure.
        """
        st = self.state
        if st["stage"] in ("done", "escalated"):
            self.log("resume", "nothing to do", saved_stage=st["stage"])
            return st
        if st["stage"] in ("waiting_for_approval", "approve", "effect") and st.get("proposal"):
            self.log("resume", "from saved proposal", saved_stage=st["stage"])
            proposal, ph, h = st["proposal"], st["proposal"]["hash"], st["hypothesis"]
            required = st.get("approval_required", True)
            if required and not self.approve(proposal, ph):
                self.enter("waiting_for_approval")
                return st
            done = [e for e in self.record.read("effects_acked") if e["proposal"] == ph and e["run"] == self.run_id]
            if done and self.effects_mode == "naive":
                self.log("effect", "already done", count=len(done))      # naive: any ack means all done
            else:
                self.effect(h, proposal, ph, required)
            self.enter("done")
            return st
        # Restart from the top. The workspace goes back to how it was when the run started (a kill
        # mid-work leaves a half-written patch), and only attempts that reached validation count
        # against the budget: a crash isn't the model's attempt.
        st["restarts"] = st.get("restarts", 0) + 1
        if st["restarts"] > MAX_RESTARTS:
            self.enter("escalated")
            self.log("escalated", "restart cap", restarts=st["restarts"] - 1)
            return st
        self.log("resume", "restart", saved_stage=st["stage"], restart=st["restarts"])
        if os.path.exists(self.pristine()):
            self.restore(self.pristine())
        st["attempt"] = st.get("attempts_completed", 0)
        return self.run()

    # --- the loop ---------------------------------------------------------------------------
    def pristine(self):
        return os.path.join(self.record.root, "snapshots", f"{self.run_id}-start")

    def run(self):
        if not os.path.exists(self.pristine()):
            shutil.copytree(self.ws, self.pristine())
        if not self.reproduce():
            self.enter("escalated")
            self.log("escalated", "cannot reproduce")
            return self.state
        feedback = None
        while self.state["attempt"] < self.budget:
            self.state["attempt"] += 1
            h = self.hypothesis(feedback)
            if h is None:
                feedback = {"hypothesis": "malformed", "problems": self.state.get("hypothesis_problems", [])}
                continue
            if self.work(h, feedback) is None:
                feedback = {"work": "blocked; see the controller's rules"}
                continue
            v, answer, fb = self.validate(h)
            if v["outcome"] == "wrong":
                feedback = fb
                self.restore(os.path.join(self.record.root, "snapshots", self.run_id))
                continue
            proposal, ph = self.propose(h, v, answer)
            required = self.needs_approval(h, v)
            self.state["approval_required"] = required        # resume applies this, not a guess
            self.save()
            if required and not self.approve(proposal, ph):
                self.enter("waiting_for_approval")
                return self.state
            self.effect(h, proposal, ph, required)
            self.enter("done")
            return self.state
        self.enter("escalated")
        self.log("escalated", "retry budget spent", attempts=self.state["attempt"])
        return self.state
