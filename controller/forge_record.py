"""The system of record on a real forge: Gitea.

Same interface as effects.Record, so the controller and harness don't change. What moves to Gitea
are the effects themselves:

- a pull request: a fresh branch off the case's base branch, the changed files committed through
  the contents API, and a pull request into the base branch
- a finding: an issue labeled `finding`
- a notification: a comment on one tracking issue per run

Each body carries `lab-run:` and `lab-proposal:` markers, which is how `find()` (the hand-built
reconcile mode) and the harness's counts locate a run's effects through Gitea's API. None of the
three dedupes: two calls make two pull requests, two issues, two comments.

The controller's own evidence (effects invoked, effects acked, approvals, run state) stays in the
local record directory.
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
import urllib.request
import uuid

from effects import Record
from hooks import point

CONF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "forge", ".forge.json")


class GiteaRecord(Record):
    def __init__(self, root: str, conf_path: str = CONF):
        super().__init__(root)
        with open(conf_path) as fh:
            self.conf = json.load(fh)
        self.repo = f"/repos/{self.conf['user']}/{self.conf['repo']}"
        self.base = None            # set by the controller: the case's baseline name

    # --- HTTP -------------------------------------------------------------------------------
    def api(self, method, path, body=None, query=None):
        url = self.conf["url"] + "/api/v1" + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"token {self.conf['token']}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read()
            return json.loads(txt) if txt else {}

    def paged(self, path, query):
        out, page = [], 1
        while True:
            batch = self.api("GET", path, query=dict(query, page=page, limit=50))
            out += batch
            if len(batch) < 50:
                return out
            page += 1

    @staticmethod
    def markers(run_id, proposal_hash):
        return f"\n\nlab-run: {run_id}\nlab-proposal: {proposal_hash}\n"

    @staticmethod
    def parse(body):
        run = re.search(r"^lab-run: (\S+)$", body or "", re.M)
        prop = re.search(r"^lab-proposal: (\S+)$", body or "", re.M)
        return (run.group(1) if run else None, prop.group(1) if prop else None)

    # --- the entities, read back from Gitea ---------------------------------------------------
    def _runs(self):
        d = os.path.join(self.root, "runs")
        return {f[:-5] for f in os.listdir(d) if f.endswith(".json")} if os.path.isdir(d) else set()

    def _entities(self, kind):
        if kind == "pull_requests":
            items = self.paged(self.repo + "/pulls", {"state": "all"})
        elif kind == "findings":
            items = self.paged(self.repo + "/issues", {"state": "all", "type": "issues", "labels": "finding"})
        else:  # notifications: comments on every tracking issue
            items = []
            for issue in self.paged(self.repo + "/issues", {"state": "all", "type": "issues",
                                                            "labels": "notification-log"}):
                items += self.paged(f"{self.repo}/issues/{issue['number']}/comments", {})
        out = []
        for it in items:
            run, prop = self.parse(it.get("body"))
            out.append({"id": f"{kind}-{it.get('number', it.get('id'))}", "run": run, "proposal": prop})
        return out

    def read(self, name):
        if name in ("pull_requests", "findings", "notifications"):
            mine = self._runs()
            return [e for e in self._entities(name) if e["run"] in mine]
        return super().read(name)

    # --- effects --------------------------------------------------------------------------
    def effect(self, kind, run_id, proposal_hash, body):
        self._append("effects_invoked", {"kind": kind, "run": run_id, "proposal": proposal_hash})
        tag = self.markers(run_id, proposal_hash)
        if kind == "pull_requests":
            branch = f"triage/{run_id}-{uuid.uuid4().hex[:6]}"
            self.api("POST", self.repo + "/branches", {"new_branch_name": branch,
                                                       "old_branch_name": f"base-{self.base}"})
            for rel, content in sorted(body["files"].items()):
                cur = self.api("GET", f"{self.repo}/contents/{rel}", query={"ref": branch})
                self.api("PUT", f"{self.repo}/contents/{rel}", {
                    "branch": branch, "sha": cur["sha"], "message": f"{body['title']}\n{tag}",
                    "content": base64.b64encode(content.encode()).decode()})
            r = self.api("POST", self.repo + "/pulls", {"head": branch, "base": f"base-{self.base}",
                                                       "title": body["title"], "body": body["diff"][:60000] + tag})
            eid = f"pull_requests-{r['number']}"
        elif kind == "findings":
            r = self.api("POST", self.repo + "/issues", {
                "title": f"Finding: {body['finding'].get('case')}", "body": json.dumps(body["finding"]) + tag,
                "labels": [self._label("finding")]})
            eid = f"findings-{r['number']}"
        else:
            issue = self._tracking(run_id)
            r = self.api("POST", f"{self.repo}/issues/{issue}/comments", {"body": body["message"] + tag})
            eid = f"notifications-{r['id']}"
        point("mid_effect")
        return self._append("effects_acked", {"kind": kind, "run": run_id, "proposal": proposal_hash, "id": eid})

    def _label(self, name):
        for lab in self.api("GET", self.repo + "/labels"):
            if lab["name"] == name:
                return lab["id"]
        raise KeyError(name)

    def _tracking(self, run_id):
        title = f"Notifications: {run_id}"
        for issue in self.paged(self.repo + "/issues", {"state": "all", "type": "issues", "labels": "notification-log"}):
            if issue["title"] == title:
                return issue["number"]
        return self.api("POST", self.repo + "/issues", {"title": title, "body": "",
                                                        "labels": [self._label("notification-log")]})["number"]

    def open_pull_request(self, run_id, proposal_hash, title, diff, files=None):
        return self.effect("pull_requests", run_id, proposal_hash, {"title": title, "diff": diff, "files": files or {}})

    def find(self, kind, run_id, proposal_hash):
        written = [e for e in self._entities(kind) if e["run"] == run_id and e["proposal"] == proposal_hash]
        acked = [e for e in super().read("effects_acked")
                 if e["kind"] == kind and e["run"] == run_id and e["proposal"] == proposal_hash]
        return {"written": written, "acked": acked}
