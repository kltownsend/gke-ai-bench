#!/usr/bin/env python3
"""Team two on the same Substrate install: LoopControlBench, the platform-reuse test.

The held-out requirement: is this a reusable platform or a single-use architecture, and can it
serve a developer team outside the Clinic? Team two is
LoopControlBench (a checkout at LCB_DIR): real merged bug-fix pull requests in third-party
Python libraries, a deterministic gate deciding done, and its own hand-built sandbox (a disposable
container per task; install egress only through a CONNECT allowlist proxy to the package hosts;
tests with no network). The agent is agy, as on the Clinic, so only the team and the
workload change.

Onboarding is configuration on the running platform, nothing in it is rebuilt:
- `onboard`: team two's credential namespace (lcb-creds) and token Secret, the credential provider's
  grant (one shared ConfigMap, now with both teams), its atespace (lcb) and actor template
  (infra/substrate/team-lcb/), on team one's worker pool. Each step is timed for the ledger.
- `probes`: one team-two actor, three phases. Install policy (package hosts only, relayed like the
  bench's proxy): what's reachable. Agent policy (the model API only): the escape suite and the API
  scope probe, the same as every rung. Cross-team policy (team one's Secret): the model call must
  fail, or one team can spend another's credential.
- `run`: per task, the bench's own workspace prep on the workstation (clone at the buggy commit, the
  maintainer's tests overlaid: trusted harness work, no untrusted code), the workspace into a fresh
  team-two actor, `pip install -e .` there under the install policy (the untrusted step), the policy
  switched to the agent's, agy with the bench's own agentic prompt, the allowlisted source files
  pulled back, and the bench's own gate (cval, in its Docker sandbox on the workstation) deciding solved.
- `offboard`: everything team two added, removed; the grant back to team one's alone.

Source harness/lab_env.sh; kubectl at the Substrate cluster. Nothing changes without --apply.

usage: lcb_team.py onboard|probes|run|offboard [--apply] [--tasks N] [--out DIR]
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LCB = os.path.expanduser(os.environ.get("LCB_DIR", "~/lab-src/loopcontrolbench"))
TEAM = os.path.join(TOOLS, "infra", "substrate", "team-lcb")
ATESPACE, TEMPLATE, CREDS_NS = "lcb", "lcb-agent", "lcb-creds"
TASK_FILE = ".lcb-task.md"
POLICY = {p: os.path.join(TEAM, f"egress-{p}.yaml") for p in ("install", "agent", "cross-team")}
APPLY = "--apply" in sys.argv
# The lab project and the agent's service account, from harness/lab_env.sh.
PROJECT = os.environ.get("LAB_PROJECT", "")
AGENT = os.environ.get("LAB_AGENT", "")
PY = sys.executable

sys.path.insert(0, os.path.join(TOOLS, "agent"))
sys.path.insert(0, os.path.join(TOOLS, "controller"))


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def require_onboarded(script: str) -> None:
    """Stop with a clear message unless team two's atespace exists. tenants.py, cross_team_probe.py and
    remaining_small.py pids/denyall run as or against team two; without `onboard` every team-two call
    is refused, which reads like an isolation result and isn't one (clean-project run, 2026-11-23)."""
    r = subprocess.run(["kubectl", "ate", "get", "atespaces"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        sys.exit(f"{script}: can't list atespaces, so team two's state is unknown:\n{r.stderr.strip()[:400]}")
    names = {line.split()[0] for line in r.stdout.splitlines()[1:] if line.split()}
    if ATESPACE not in names:
        sys.exit(f"{script}: team two isn't onboarded (no atespace '{ATESPACE}'). Run "
                 "`harness/lcb_team.py onboard --apply` first (it needs LoopControlBench at LCB_DIR), "
                 "or skip this script.")


def say(*cmd, check=True, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    if not APPLY:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run([str(c) for c in cmd], text=True, capture_output=True, check=check, **kw)


# -- the subset ----------------------------------------------------------------------------------
def pick_tasks(n: int) -> list[dict]:
    """Stratified across the certified pool's libraries, in index order: one per library first,
    then a second from the largest, until n. No hand-picking."""
    pool = json.load(open(os.path.join(LCB, "tasks", "_index.json")))
    by_repo: dict[str, list[dict]] = {}
    for t in pool:
        by_repo.setdefault(t["repo"], []).append(t)
    order = sorted(by_repo, key=lambda r: -len(by_repo[r]))
    out, rnd = [], 0
    while len(out) < n and any(len(by_repo[r]) > rnd for r in order):
        for r in order:
            if len(out) < n and len(by_repo[r]) > rnd:
                out.append(by_repo[r][rnd])
        rnd += 1
    return out


# -- the venue: a team-two actor, phases by egress policy -----------------------------------------
def team_venue():
    from substrate_venue import SubstrateVenue
    return SubstrateVenue(atespace=ATESPACE, template=TEMPLATE, policy=POLICY["install"])


def set_phase(sb, phase: str) -> float:
    """Replace the actor's egress policy (`kubectl ate update egress-policy`). Returns seconds until the
    new policy is in effect (for the agent phase, until the model host answers)."""
    # The update takes the stored policy's metadata.uid and metadata.version as preconditions
    # (found on the first GKE run), so the live policy's metadata block goes in front of the phase
    # file's rules. Text, not YAML parsing: both are the CLI's own YAML shape.
    t0 = time.perf_counter()
    live = sb.ate("get", "egress-policy", sb.actor, "-a", sb.atespace, "-o", "yaml").stdout.decode()
    meta = live[live.index("metadata:"):live.index("\nrules:")] + "\n"
    rules = open(POLICY[phase]).read()
    rules = rules[rules.index("rules:"):]
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(meta + rules)
    try:
        r = sb.ate("update", "egress-policy", sb.actor, "-a", sb.atespace, "-f", fh.name, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"egress-policy update to {phase} failed: {r.stderr.decode()[-400:]}")
    finally:
        os.unlink(fh.name)
    if phase == "agent":
        # The update returns before the gateway enforces it: on GKE, the first call after an
        # install phase failed at TLS, and the same call worked about 9 s later (2026-11-22). Wait
        # until the model host answers through the gateway (any HTTP status; no model call, no spend).
        ready = ("import urllib.request,urllib.error\n"
                 "try:\n urllib.request.urlopen('https://aiplatform.googleapis.com/',timeout=5); print('up')\n"
                 "except urllib.error.HTTPError: print('up')\n"
                 "except Exception as e: print('down', type(e).__name__)")
        for _ in range(60):
            if sb.run_in_actor(["python3", "-I", "-c", ready]).strip().startswith("up"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("agent policy not in effect after 60 s")
    return round(time.perf_counter() - t0, 3)


class InActor:
    """The sandbox interface AntigravityModel calls: run argv in the actor's /work, nothing copied
    (the workspace and its venv already live there)."""
    def __init__(self, sb):
        self.sb = sb

    def run(self, cwd, argv, timeout=1800, **_):
        body = {"command": list(argv), "timeout": f"{timeout}s"}
        out = json.loads(self.sb.http("POST", "/process", json.dumps(body).encode(), timeout=timeout + 60))
        return subprocess.CompletedProcess(argv, out.get("exitCode", -1), out.get("stdout", ""),
                                           out.get("stderr", "") + (out.get("error") or ""))


# -- onboard / offboard -----------------------------------------------------------------------------
def onboard():
    image = os.environ.get("LCB_ACTOR_IMAGE", "")
    if APPLY and "@sha256:" not in image:
        sys.exit("LCB_ACTOR_IMAGE must be pinned by digest (build infra/substrate/team-lcb/Dockerfile)")
    if APPLY and not (PROJECT and AGENT):
        sys.exit("LAB_PROJECT and LAB_AGENT are not set: source harness/lab_env.sh")
    steps = []

    def step(name, *cmd, **kw):
        t0 = time.time()
        r = say(*cmd, **kw)
        steps.append({"step": name, "s": round(time.time() - t0, 1), "rc": r.returncode})
        return r
    step("namespace for team two's credential", "kubectl", "create", "namespace", CREDS_NS, check=False)
    env = dict(os.environ, LAB_TOKEN_NAMESPACE=CREDS_NS)
    step("team two's token Secret", os.environ.get("GOOGLE_AUTH_PY", PY),
         os.path.join(TOOLS, "harness", "token_secret.py"), "--once", env=env)
    step("the provider's grant, both teams", "kubectl", "apply", "-f", os.path.join(TEAM, "namespace-policy.yaml"))
    step("provider restart", "kubectl", "-n", "ate-system", "rollout", "restart", "deployment/k8s-credential-provider")
    step("provider ready", "kubectl", "-n", "ate-system", "rollout", "status", "deployment/k8s-credential-provider",
         "--timeout=180s")
    step("atespace", "kubectl", "ate", "create", "atespace", ATESPACE, check=False)
    tpl = open(os.path.join(TEAM, "actor-template.yaml")).read().replace("LCB_ACTOR_IMAGE", image or "IMAGE")
    tpl = tpl.replace("${LAB_PROJECT}", PROJECT or "LAB_PROJECT").replace("${LAB_AGENT}", AGENT or "LAB_AGENT")
    t0 = time.time()
    print("+ kubectl ate create actor-template -f - (team two's template)", flush=True)
    if APPLY:
        subprocess.run(["kubectl", "ate", "create", "actor-template", "-f", "-"], input=tpl, text=True, check=True)
        for _ in range(90):
            y = subprocess.run(["kubectl", "ate", "get", "actor-template", TEMPLATE, "-a", ATESPACE, "-o", "yaml"],
                               capture_output=True, text=True).stdout
            if "errorMessage:" in y:
                sys.exit("golden build failed:\n" + y)
            if "snapshotTime:" in y:
                break
            time.sleep(10)
    steps.append({"step": "template and its golden snapshot", "s": round(time.time() - t0, 1), "rc": 0})
    print(json.dumps({"onboarding": steps, "objects_added": [
        f"namespace {CREDS_NS}", f"Secret {CREDS_NS}/lab-agent-token", "one entry in the provider's grant ConfigMap",
        f"atespace {ATESPACE}", f"actor template {ATESPACE}/{TEMPLATE}"],
        "platform_code_changed": "none", "worker_pool": "team one's, shared"}, indent=1))


def offboard():
    r = subprocess.run(["kubectl", "ate", "get", "actors", "-a", ATESPACE], capture_output=True, text=True)
    for line in r.stdout.splitlines()[1:]:
        say("kubectl", "ate", "delete", "actor", line.split()[1] if len(line.split()) > 1 else line.split()[0],
            "-a", ATESPACE, check=False)
    say("kubectl", "ate", "delete", "actor-template", TEMPLATE, "-a", ATESPACE, check=False)
    say("kubectl", "ate", "delete", "atespace", ATESPACE, check=False)
    say("kubectl", "apply", "-f", os.path.join(TOOLS, "infra", "substrate", "namespace-policy.yaml"))
    say("kubectl", "-n", "ate-system", "rollout", "restart", "deployment/k8s-credential-provider")
    say("kubectl", "delete", "namespace", CREDS_NS, "--ignore-not-found")


# -- probes -------------------------------------------------------------------------------------------
INSTALL_PROBE = r"""
import json, socket, ssl, urllib.request
def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return {"reached": True, "status": r.status}
    except Exception as e:
        return {"reached": False, "error": type(e).__name__ + ": " + str(e)[:120]}
def tcp(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close(); return "connected"
    except Exception as e:
        return "refused: " + type(e).__name__
print(json.dumps({
  "pypi_simple": get("https://pypi.org/simple/pip/"),
  "files_pythonhosted": get("https://files.pythonhosted.org/"),
  "github": get("https://github.com/"),
  "model_api": get("https://aiplatform.googleapis.com/"),
  "metadata": get("http://169.254.169.254/computeMetadata/v1/"),
  "private_10": tcp("10.0.0.1", 443),
  "public_dns_example": get("https://example.com/"),
}))
"""


def probes(out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    rec = {"team": "lcb", "atespace": ATESPACE}
    if not APPLY:
        print("+ probes: install, agent and cross-team phases in one team-two actor")
        return
    with team_venue() as sb:
        sb.ensure_actor()
        ws = tempfile.mkdtemp(prefix="lcb-probe-")
        for f in ("api_scope.py", "escape_suite.py"):
            shutil.copy(os.path.join(TOOLS, "probes", f), ws)
        sb.http("PUT", "/workspace", __import__("pod_sandbox").pack(ws), ctype="application/x-tar")
        rec["install_phase"] = json.loads(sb.run_in_actor(["python3", "-I", "-c", INSTALL_PROBE]) or "{}")
        rec["to_agent_policy_s"] = set_phase(sb, "agent")
        rec["agent_phase_api_scope"] = json.loads(InActor(sb).run("/work", ["python3", "api_scope.py",
                                                  PROJECT]).stdout or "{}")
        rec["agent_phase_escape"] = json.loads(InActor(sb).run("/work", ["python3", "escape_suite.py",
                                               "--run-token", "lcb-probe"]).stdout or "{}")
        set_phase(sb, "cross-team")
        time.sleep(5)                                 # policy propagation
        cross = json.loads(InActor(sb).run("/work", ["python3", "api_scope.py", PROJECT]).stdout or "{}")
        rec["cross_team_model_call"] = cross.get("positive_control")
        rec["cross_team_verdict"] = ("ISOLATED: team one's credential was not injected"
                                     if not (cross.get("positive_control") or {}).get("ok")
                                     else "NOT ISOLATED: team two spent team one's credential")
        rec["venue_events"] = sb.events
    path = os.path.join(out_dir, "lcb-probes.json")
    json.dump(rec, open(path, "w"), indent=1, default=str)
    print(json.dumps({k: rec[k] for k in ("install_phase", "cross_team_verdict")}, indent=1, default=str))


# -- run -------------------------------------------------------------------------------------------------
def run(n: int, out_dir: str, parallel: int = 1, cap: float = 25.0):
    """Tasks PARALLEL at a time (scales out beside the Clinic repairs). Spend cap:
    measured spend plus a $2 ceiling per task in flight stays under CAP, priced as harness/pilot.py."""
    import threading
    from pilot import cost, RUN_CEILING
    sys.path.insert(0, os.path.join(LCB, "tools"))
    from run_sandboxed import INSTALL, prepare_host   # the bench's own install script and workspace prep
    import agentic                                    # its prompt and its scoring through the gate
    import models as M
    tasks = pick_tasks(n)
    os.makedirs(out_dir, exist_ok=True)
    print(json.dumps({"tasks": [t["task_id"] for t in tasks]}), flush=True)
    if not APPLY:
        return
    M.AntigravityModel.billing_guard()
    lock = threading.Lock()
    state = {"spent": 0.0, "inflight": 0}

    def spent_of(rec):
        return sum(cost(c.get("usage") and {"input_tokens": c["usage"].get("input_tokens", 0),
                                            "cache_read_tokens": c["usage"].get("cache_read_tokens", 0),
                                            "output_tokens": c["usage"].get("output_tokens", 0),
                                            "thinking_tokens": c["usage"].get("thinking_tokens", 0)})
                   for c in rec.get("calls") or [])
    for spec in tasks:
        path = os.path.join(out_dir, f"{spec['task_id']}.json")
        if os.path.exists(path):
            state["spent"] += spent_of(json.load(open(path)))
    threads = []
    for spec in tasks:
        path = os.path.join(out_dir, f"{spec['task_id']}.json")
        if os.path.exists(path):
            continue                                 # crash-safe: one record per task, skip done ones
        while True:
            with lock:
                room = state["spent"] + (state["inflight"] + 1) * RUN_CEILING <= cap
                if state["inflight"] < parallel and room:
                    state["inflight"] += 1
                    break
                if not room and state["inflight"] == 0:
                    print(json.dumps({"cap_reached": round(state["spent"], 2), "cap": cap}), flush=True)
                    return
            time.sleep(5)
        th = threading.Thread(target=one_task, args=(spec, path, INSTALL, prepare_host, agentic, M,
                                                    lock, state, spent_of))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    print(json.dumps({"done": True, "spent": round(state["spent"], 2)}), flush=True)


def one_task(spec, path, INSTALL, prepare_host, agentic, M, lock, state, spent_of):
    if True:
        rec = {"task": spec["task_id"], "repo": spec["repo"], "t0": time.time(),
               # The bench's venue preflight checks a self-served engine (envelope, large prefill,
               # served model); agy on hosted Vertex has none, so the bench's own recorded override
               # is used (LCB_SKIP_PREFLIGHT), stated in every task record.
               "preflight": "skipped-by-override: hosted Vertex via agy, no self-served engine to check"}
        work = tempfile.mkdtemp(prefix="lcb-")
        try:
            rp = prepare_host(spec, work)
            prompt = agentic.build_prompt_agentic(spec, rp)
            up = tempfile.mkdtemp(prefix="lcb-up-")
            shutil.copytree(rp, os.path.join(up, "w"), ignore=shutil.ignore_patterns(".git"))
            # The bench's prompt is larger than one argv string may be in the actor ("Argument list
            # too long", first GKE try); it goes in verbatim as a file the agent is told to read. Only
            # the allowlisted source files come back, so the file is never scored.
            with open(os.path.join(up, "w", TASK_FILE), "w") as fh:
                fh.write(prompt)
            rec["prompt_chars"] = len(prompt)
            with team_venue() as sb:
                sb.ensure_actor()
                sb.http("PUT", "/workspace", __import__("pod_sandbox").pack(os.path.join(up, "w")),
                        ctype="application/x-tar")
                t = time.time()
                # The bench's own install script, verbatim: in the actor /work is the repo root, so
                # its /work/venv is the ./venv its agentic prompt names.
                inst = InActor(sb).run("/work", ["sh", "-c", INSTALL.replace("python -m venv", "python3 -m venv")],
                                       timeout=1200)
                rec.update(install_exit=inst.returncode, install_s=round(time.time() - t, 1),
                           install_tail=(inst.stdout + inst.stderr)[-800:])
                if inst.returncode != 0 or "__INSTALL_OK__" not in inst.stdout:
                    rec["verdict"] = "RESULT: INSTALL_FAILED (the bench's only run-time exclusion)"
                    raise StopIteration
                rec["to_agent_policy_s"] = set_phase(sb, "agent")
                model = M.AntigravityModel(sandbox=InActor(sb))
                model._agy(f"Your task is in the file {TASK_FILE} in the current directory. Read it in full "
                           "and do exactly what it says.", "/work", stage="lcb")
                rec["calls"] = model.calls
                for sf in spec["source_files"]:
                    b64 = sb.run_in_actor(["sh", "-c", f"base64 -w0 {sf} 2>/dev/null || true"])
                    if b64.strip():
                        with open(os.path.join(rp, sf), "wb") as fh:
                            fh.write(base64.b64decode(b64))
                verdict, changed, out_of_scope = agentic.score(spec, rp)
                rec.update(verdict=verdict, changed_allowlisted=[p for p in changed if p in spec["source_files"]],
                           changed_out_of_scope=out_of_scope, venue_events=sb.events)
        except StopIteration:
            pass
        except Exception as e:                        # noqa: BLE001  recorded, the run continues
            rec["error"] = repr(e)[:500]
        finally:
            rec["wall_s"] = round(time.time() - rec["t0"], 1)
            json.dump(rec, open(path + ".tmp", "w"), indent=1, default=str)
            os.replace(path + ".tmp", path)
            with lock:
                state["inflight"] -= 1
                state["spent"] += spent_of(rec)
                rec["spent_total"] = round(state["spent"], 3)
            print(json.dumps({k: rec.get(k) for k in ("task", "verdict", "install_s", "wall_s", "error", "spent_total")}),
                  flush=True)
            shutil.rmtree(work, ignore_errors=True)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    out = arg("--out", os.path.join(TOOLS, "runs", f"lcb-team-{time.strftime('%Y%m%d')}"))
    if cmd == "onboard":
        onboard()
    elif cmd == "offboard":
        offboard()
    elif cmd == "probes":
        probes(out)
    elif cmd == "run":
        run(int(arg("--tasks", 10)), out, int(arg("--parallel", 1)), float(arg("--cap", 25)))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
