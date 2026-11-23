# Run guide

Written so a coding agent can follow it step by step, and a person can check each step. Read `AGENTS.md`
first: every step below that meters asks for a yes before it runs, and the session ends parked.

The order is cheapest first. Steps 1 to 3 cost nothing but downloads. Steps 4 to 8 run on your own Google
Cloud project and cost cluster time and model calls; the costs are what the lab paid, on its billing report.

## What it cost the lab

| Step | Cluster time | Model calls | Billed |
|---|---|---|---|
| One plain GKE baseline (probes, a reach run, a canary) | about 45 to 55 minutes | a few calls | about $0.30 to $0.50 |
| Agent Substrate install (the wizard) | about 45 minutes | none | about $0.70 |
| Substrate experiments, one session (resume, wake, density, isolation) | 3 to 5 hours | none | about $3 to $7 |
| The Clinic case set on Substrate (21 cases, 3 runs each) | about 1.5 hours | 63 runs | $31.65 in model calls |
| Team two's 10 tasks | about 15 minutes | 10 runs | $1.02 in model calls |
| A parked Substrate install, per day | none running | none | about $1.70 (the database disk); the zonal cluster fee was covered by the free tier |

The whole lab project, five days of runs including every rung: $117.76 billed, $100.52 of it model calls.
Model calls are Gemini on Vertex AI at pay-as-you-go rates; thinking tokens are billed inside output tokens.

## 0. Prerequisites

- A Google Cloud project with billing on, and the APIs for GKE, Compute Engine, Artifact Registry, Vertex AI
  and Cloud Logging enabled.
- On your workstation: `gcloud` with the GKE auth plugin, `kubectl`, Docker (with buildx), Go 1.27.3 or later
  (for the Substrate installer and `kubectl ate`), Python 3.13, git and curl.
- Agent Substrate needs a regional SSD quota large enough for its bundled 500 GiB database disk. Request the
  raise before the install day; it isn't instant.

## 1. Configure

```bash
cp lab.env.example lab.env        # git-ignored; set LAB_PROJECT, and anything else you change
source harness/lab_env.sh          # in every shell that runs the lab
```

`harness/lab_env.sh` derives the rest: the agent's service-account email, the Artifact Registry path, and a
gcloud configuration directory of the lab's own (`$HOME/.config/gcloud-$LAB_PROJECT`), so your own gcloud and
Application Default Credentials stay untouched.

Then, once:

```bash
gcloud auth login                                   # into the lab's own gcloud config
gcloud iam service-accounts create "$LAB_AGENT_SA_NAME" --project "$LAB_PROJECT"
gcloud projects add-iam-policy-binding "$LAB_PROJECT" \
    --member "serviceAccount:$LAB_AGENT" --role roles/aiplatform.user
gcloud artifacts repositories create lab-images --repository-format docker \
    --location "$LAB_REGION" --project "$LAB_PROJECT"
gcloud auth application-default login --impersonate-service-account "$LAB_AGENT"
```

The service account's one grant is Vertex AI user. That's the identity the agent's model calls run as; the
token minters (`agent/mint_token.py`, `harness/token_secret.py`) mint short-lived tokens for it by
impersonation, and no personal credential reaches a sandbox.

## 2. Fetch what isn't redistributed

```bash
./fetch_vendor.sh
```

Downloads Google's Antigravity CLI (agy 1.5.13, Linux x64 and arm64, about 190 MB) and the Agent Sandbox
v1.0.6 manifests, and checks each against the checksum the lab recorded. It needs a yes: it's a download
under Google's terms.

## 3. Free checks on your workstation

```bash
git clone https://github.com/kltownsend/clinicbench "$LAB_SRC/clinicbench"
$LAB_PYTHON -m unittest discover -s tests -v
```

161 tests, Docker running. They cover the judge (honest answers pass; forgeries, key reads, symlinks,
out-of-scope edits and regressions fail), the controller, the venues and the harness. Two or three skip
without a lab credential or the team-two checkout. `docs/JUDGE.md` explains the judge and how to validate a
workspace by hand.

Optionally, Agent Substrate on a local kind cluster by Google's quickstart (`infra/substrate/local/`,
`infra/substrate/local_env.sh`). The lab used it only to check its own apparatus, never as a result.

## 4. Build the images

```bash
docker buildx build --platform linux/amd64 -f agent/Dockerfile.agent \
    --build-context agy=agent/vendor/agy-1.5.13-x64/x -t "$LAB_REGISTRY/lab-agent" --push .
docker buildx build --platform linux/amd64 -f agent/Dockerfile.actor \
    --build-context agy=agent/vendor/agy-1.5.13-x64/x -t "$LAB_REGISTRY/lab-actor" --push .
```

Paste each pushed digest (`...@sha256:...`) into `lab.env` as `LAB_AGENT_IMAGE` and `LAB_ACTOR_IMAGE`; the
venues refuse an image that isn't pinned by digest. The Executor and team-two images build the same way
(`infra/executor/image/Dockerfile`, `infra/substrate/team-lcb/Dockerfile`; each file's header has its command).

## 5. The plain GKE baselines (optional)

Three clusters, one at a time, each deleted before the next: `default` (GKE defaults plus Workload Identity),
`hardened` (what a team restricts by hand) and `vulnerable` (deliberately open, so the probes have something to
find).

```bash
infra/cluster.sh hardened                          # dry run: prints the commands
infra/cluster.sh hardened --apply                  # after a yes on the cost
infra/cluster.sh setup --profile hardened --apply
infra/cluster.sh listener --profile hardened --apply   # the canary listener for the reach test
$LAB_PYTHON harness/probe_run.py --venue hardened --out runs/probes
$LAB_PYTHON harness/reach_run.py --profile hardened --case S01 --out runs/reach
infra/cluster.sh down --apply && infra/cluster.sh inventory --apply
```

## 6. Agent Sandbox (optional)

```bash
infra/cluster.sh sandbox --apply                  # the cluster plus the gVisor node pool
infra/cluster.sh setup --profile sandbox --apply
$LAB_PYTHON harness/probe_run.py --venue sandbox --out runs/probes
```

## 7. Agent Substrate

Install by Google's documented path, an interactive wizard. `infra/substrate/INSTALL.md` has the pinned
versions and every answer the lab gave. Rehearse it free with `make dry-run` in the substrate-gke checkout.
Save the exit summary: it prints the snapshot bucket (`SUBSTRATE_BUCKET` in `lab.env`) and the teardown
commands.

Then the lab's part, and the token supply, which keeps the gateway's injected token fresh during runs:

```bash
infra/cluster.sh setup --profile substrate --apply
$GOOGLE_AUTH_PY harness/token_secret.py &
```

The experiments, each mapped to its page on the project site. Every one but the case set and team two runs
with no model calls.

| Run | Command | Site page |
|---|---|---|
| Token containment and the escape suite | `harness/probe_run.py --venue substrate --out runs/probes` | /tests/gateway-keys/ |
| Suspend and resume, continuity checks | `harness/resume_probe.py --explicit 20 --wake 20 --out runs/resume` | /tests/picks-up/ |
| The same from storage, past the 30-minute local copy | `harness/resume_probe.py --storage 12 --wait 2100 --out runs/resume` | /tests/picks-up/ |
| First request after a long idle, inside the cluster | `harness/incluster_wake.py --n 12 --wait 2100 --out runs/wake` | /tests/restore-500ms/ |
| Google's own benchmark under lab settings | `harness/google_bench.py setup --apply`, `build --apply`, `run TEST --apply` (`list` shows the tests) | /tests/density-10x/ |
| The container control for density | `harness/density_control.py --apply --out runs/density` | /tests/density-10x/ |
| Per-team identities and the isolation matrix | `harness/tenants.py setup`, `run`, `teardown` | /teams/ |
| Cross-team reach from a team-two actor | `harness/cross_team_probe.py` | /teams/ |
| Warm-pool reuse, a long held call, the no-network actor | `harness/remaining_small.py reuse`, `longcall SECONDS`, `denyall` | /tests/warm-pool/ |
| The Clinic case set | `LAB_SUBSTRATE_POLL=1 harness/pilot.py --gke substrate --dangerous --cap 50 --out runs/caseset` | /tests/coding-environments/ |
| Its report | `harness/pilot_report.py runs/caseset` | /tests/coding-environments/ |
| Team two (LoopControlBench) | `harness/lcb_team.py onboard --apply`, `probes --apply`, `run --tasks 10 --cap 25 --apply` | /teams/ |

Run them through `$LAB_PYTHON` from the repository root, one at a time on a new install.

`LAB_SUBSTRATE_POLL=1` starts each agent call detached inside the actor and checks back every 10 seconds. A
request held open and silent through Substrate's router is cut at about 5 minutes; without the polled wait,
long agent calls fail as infrastructure. `--dangerous` turns agy's own permission prompts off, so the
runtime's controls are the only ones; leave it off to compare with the prompts on.

## 8. End every session parked

```bash
infra/cluster.sh park --apply        # node pools to zero, a manifest of what remains
infra/cluster.sh inventory --apply   # must print "inventory clean"
infra/cluster.sh unpark --apply      # next session
```

When you're done for good: `infra/cluster.sh substrate-down --apply` runs Google's teardown in order (the
actors, `ate-setup delete`, the database disk, `tools/cleanup-gcp`), then the lab's own `down` and the
inventory. Never assume teardown worked: read the inventory.

## What to change, and what not to

The probes, the judge and the cases are the instrument; change them and you're measuring something else, which
is fine, but say so beside the result. The venues, the cluster sizes, the model and the case set are the
settings; the lab's are in `lab.env.example` and this guide. Raw results land in `runs/` (git-ignored).
