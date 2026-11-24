# Run guide

Written so a coding agent can follow it step by step, and a person can check each step. Read `AGENTS.md`
first: every step below that meters asks for a yes before it runs, and every session ends parked.

What you get from this: your own measurements on your own project, with the same probes, judge, cases and
settings the lab used. You won't get the lab's published numbers back exactly. Its raw run records aren't in
this repository, its verdicts come from rulings that aren't either, and your project, region, quota, model
behavior and timing will differ. Compare your numbers with the site's as two runs of the same instrument.

Use a Google Cloud project with nothing else of value in it. The agent runs code nobody reviewed, and some
steps turn its own permission prompts off on purpose.

## What it cost the lab (historical, not an estimate)

From the lab project's billing report, runs on 2026-11-20 to 2026-11-23, us-central1, list prices in USD, no
committed-use discounts. Your bill depends on your region, credits and how long things run.

| Step | Resources | Model calls | Billed |
|---|---|---|---|
| One plain GKE baseline (probes, a reach run, a canary) | 45 to 55 cluster-minutes, one or two c3-standard-4 nodes | a few | about $0.30 to $0.50 |
| Agent Substrate install (the wizard) | about 45 minutes: two c3-standard-4 workers, E2 system nodes, a 500 GiB balanced persistent disk | none | about $0.70 |
| Substrate experiments, one session (resume, wake, density, isolation) | 3 to 5 hours of the same, plus a one-node pool for Google's load generator | none | about $3 to $7 |
| The Clinic case set on Substrate (21 cases, 3 runs each) | about 1.5 hours | 63 agent runs, 10.9M input, 1.5M output and 57.5M cached tokens | $31.65 in model calls |
| Team two's 10 tasks | about 15 minutes | 10 agent runs | $1.02 in model calls |
| A parked Substrate install, per day | node pools at zero | none | about $1.70: the 500 GiB balanced disk ($0.10 per GiB-month). The zonal cluster fee was covered by the free tier, which a second cluster in the same billing account doesn't get |

Model: `gemini-4-hydrogen-medium` on Vertex AI, global endpoint, pay-as-you-go at $1.50 per million input
tokens, $0.075 per million cached input tokens and $7.50 per million output tokens; thinking tokens are
billed inside output tokens. Set a budget alert on the project before step 5.

## Versions the lab ran

| Piece | Version |
|---|---|
| GKE | 1.37.1-gke.1552000 for the plain baselines and Agent Sandbox (Regular channel); the Substrate installer's 1.37 on the Rapid channel |
| Agent Sandbox | v1.0.6 (manifests fetched by `fetch_vendor.sh`) |
| Agent Substrate | v1.0.1, images `ghcr.io/agent-substrate/substrate:v1.0.1`, commit `20778a6dbb01f13024642851bb592bf67c9fec05` |
| Substrate installer | ai-on-gke/substrate-gke at `0de82014290a31321a9c9f1f6cde48b1d7f48b66` |
| Agent Executor | google/ax v0.4.0-rc.1, `2701b10b459e6fe69ae3e4e0a1422641f349e229` (a preview) |
| Antigravity CLI | agy 1.5.13 (fetched and checksum-verified) |
| Model | `gemini-4-hydrogen-medium`, Vertex AI, global. If your project can't call it, set `LAB_MODEL` and say so beside your results |
| clinicbench (optional) | https://github.com/kltownsend/clinicbench at `1d45ebdee80b74af4b26a98280deb4f7ec863cca` |
| LoopControlBench (team two) | https://github.com/kltownsend/loopcontrolbench at `2b4e733c6bf5b563e38188fe1f8135f37cce07b7`; its install, workspace prep and scoring. The prompt is `harness/lcb_team.py build_prompt`, the text the lab ran, which that commit doesn't have |
| Workstation | Python 3.13, Go 1.27.3, Docker with buildx, gcloud with the GKE auth plugin, kubectl, git, curl |

## 0. Before you start

**APIs** on the project: Kubernetes Engine, Compute Engine, Artifact Registry, Vertex AI, Cloud Logging, IAM
and IAM Service Account Credentials (`iamcredentials.googleapis.com`, which the token minting needs).

**Your own permissions** (the operator, not the agent): enough to create GKE clusters, node pools, disks,
buckets, firewall rules, an Artifact Registry repository, a service account and IAM bindings. Project Owner
on a throwaway project covers it. You also need permission to impersonate the agent's service account; step
1 grants it.

**Where you run this is outside every boundary it tests.** The harness spends its effort keeping an untrusted
agent inside a sandbox: no ambient credential, egress by allowlist, a judge in its own container. The operator
gets none of that. These scripts run on your workstation as you, with your project-owner sign-in, and
`lab.env` is sourced as shell. Whatever follows this guide, you or a coding agent you hand it to, can read
anything your user account can, including your own gcloud credentials, and can change anything in the project.
The lab's own gcloud config keeps its sign-in apart from yours so nothing mixes by accident; it isn't a
security boundary. The lab ran this way, so the published instrument runs this way. If you'll hand the guide
to an agent, give it a throwaway project and its own OS account or VM holding only the lab's sign-in. That
isn't how the lab ran; it's what we'd do running it again with the agent in charge.

**Quota:** Agent Substrate needs about 900 GB of the region's SSD quota (`SSD_TOTAL_GB`): its bundled
database claims a 500 GiB balanced persistent disk and its four nodes' boot disks take 100 GB each. The lab's
project started at a limit of 500, so the install created the cluster, then hung with the database disk
Pending on QUOTA_EXCEEDED until a raise came through. Step 1 ends with a free check; run it before install day,
because a raise isn't always instant.

## 1. Configure and bootstrap

```bash
cp lab.env.example lab.env        # git-ignored; set LAB_PROJECT, and anything else you change
source harness/lab_env.sh          # in every shell that runs the lab
```

Set `LAB_REGION` and `LAB_ZONE` in lab.env if you don't want the default (us-central1, us-central1-a). Every
cluster, node pool and VM the scripts create goes in `LAB_ZONE`, and the Substrate wizard takes the same zone
(step 7). The zone has to sit in the region; `lab_env.sh` refuses a mismatch. If a create fails on zone
capacity, change `LAB_ZONE` to another zone in the region and source the file again.

`harness/lab_env.sh` derives the rest: the agent's service-account email (`LAB_AGENT`), the Artifact Registry
path (`LAB_REGISTRY`), and a gcloud configuration directory of the lab's own
(`$HOME/.config/gcloud-$LAB_PROJECT`), so your own gcloud and Application Default Credentials stay untouched.

The Python environment for the token minters:

```bash
python3.13 -m venv "$LAB_SRC/venv" && "$LAB_SRC/venv/bin/pip" install -r requirements.txt
# then in lab.env:  GOOGLE_AUTH_PY=$LAB_SRC/venv/bin/python   (and re-source harness/lab_env.sh)
```

The pinned upstream checkouts and tools:

```bash
mkdir -p "$LAB_SRC"
git clone https://github.com/agent-substrate/substrate.git "$SUBSTRATE_DIR"
git -C "$SUBSTRATE_DIR" checkout 20778a6dbb01f13024642851bb592bf67c9fec05
(cd "$SUBSTRATE_DIR" && go install ./cmd/kubectl-ate)            # kubectl ate, from v1.0.1
git clone https://github.com/ai-on-gke/substrate-gke.git "$SUBSTRATE_GKE_DIR"
git -C "$SUBSTRATE_GKE_DIR" checkout 0de82014290a31321a9c9f1f6cde48b1d7f48b66
kubectl ate --help >/dev/null && echo "kubectl ate ok"
```

The project side, once, in the lab's gcloud config:

```bash
gcloud auth login
gcloud config set project "$LAB_PROJECT"
gcloud services enable container.googleapis.com compute.googleapis.com artifactregistry.googleapis.com \
    aiplatform.googleapis.com logging.googleapis.com iam.googleapis.com iamcredentials.googleapis.com
gcloud iam service-accounts create "$LAB_AGENT_SA_NAME" --project "$LAB_PROJECT"
gcloud projects add-iam-policy-binding "$LAB_PROJECT" \
    --member "serviceAccount:$LAB_AGENT" --role roles/aiplatform.user
gcloud iam service-accounts add-iam-policy-binding "$LAB_AGENT" --project "$LAB_PROJECT" \
    --member "user:$(gcloud config get-value account)" --role roles/iam.serviceAccountTokenCreator
gcloud artifacts repositories create lab-images --repository-format docker \
    --location "$LAB_REGION" --project "$LAB_PROJECT"
gcloud auth configure-docker "$LAB_REGION-docker.pkg.dev"
gcloud auth application-default login --impersonate-service-account "$LAB_AGENT"
```

The agent's service account gets one project role, Vertex AI user. That role allows more Vertex AI operations
than the one generate call the lab makes, which is one more reason for a project with nothing else in it. If
your organization grants roles at the folder or organization level, check what the account inherits. A custom
role with only the prediction permissions is tighter; the lab didn't test one.

**Preflight**, before anything meters: mint one token and check whose it is.

```bash
$GOOGLE_AUTH_PY agent/mint_token.py "$(mktemp -d)"      # must print {"minted_for": "<your LAB_AGENT>", ...}
infra/cluster.sh quota                                  # read-only, free: must print "quota ok"
```

If the quota check prints `quota SHORT`, it names the limit to request. Request it and rerun the check before
step 5 or 7; nothing that bills should start on a short quota. `infra/cluster.sh quota sandbox` and `quota
baseline` check the smaller needs of steps 6 and 5 (200 GB and 100 GB).

## 2. Fetch what isn't redistributed

```bash
./fetch_vendor.sh            # dry run: lists what it would download
./fetch_vendor.sh --yes      # after a yes: agy 1.5.13 (about 190 MB) and the Agent Sandbox manifests
```

Each file is checked against the checksum the lab recorded when it downloaded it.

## 3. Free checks on your workstation

```bash
$LAB_PYTHON -m unittest discover -s tests -v
```

161 tests with Docker running; two or three skip without a lab credential or the team-two checkout. They cover
the judge (honest answers pass; forgeries, key reads, symlinks, out-of-scope edits and regressions fail), the
controller, the venues and the harness. No clinicbench checkout is needed: the case trees ship in `baselines/`.
`docs/JUDGE.md` explains the judge and how to validate a workspace by hand.

Optionally, Agent Substrate on a local kind cluster by Google's quickstart (`infra/substrate/local/`,
`infra/substrate/local_env.sh`). The lab used it only to check its own apparatus, never as a result.

## 4. Build the images

```bash
docker buildx build --platform linux/amd64 -f agent/Dockerfile.agent \
    --build-context agy=agent/vendor/agy-1.5.13-x64/x -t "$LAB_REGISTRY/lab-agent:run" --push .
docker buildx build --platform linux/amd64 -f agent/Dockerfile.actor \
    --build-context agy=agent/vendor/agy-1.5.13-x64/x -t "$LAB_REGISTRY/lab-actor:run" --push .
docker buildx imagetools inspect "$LAB_REGISTRY/lab-agent:run" --format '{{.Name}}@{{.Manifest.Digest}}'
docker buildx imagetools inspect "$LAB_REGISTRY/lab-actor:run" --format '{{.Name}}@{{.Manifest.Digest}}'
```

Paste each digest reference into `lab.env` as `LAB_AGENT_IMAGE` and `LAB_ACTOR_IMAGE`, then re-source
`harness/lab_env.sh`; the venues refuse an image that isn't pinned by digest. The Executor and team-two images
build the same way (`infra/executor/image/Dockerfile`, `infra/substrate/team-lcb/Dockerfile`; each file's header
has its command).

## 5. The plain GKE baselines (optional)

Three clusters, one at a time, each deleted before the next: `default` (GKE defaults plus Workload Identity),
`hardened` (what a team restricts by hand) and `vulnerable` (deliberately open, so the probes have something to
find; don't leave it running). `infra/cluster.sh` is a dry run unless given `--apply`.

```bash
infra/cluster.sh hardened                              # prints the commands; read them
infra/cluster.sh hardened --apply                      # after a yes on the cost
infra/cluster.sh setup --profile hardened --apply      # kubectl credentials and the profile's manifests
kubectl config current-context                         # must name lab-hardened in your project
infra/cluster.sh listener --profile hardened --apply   # the canary listener for the reach test
$LAB_PYTHON harness/probe_run.py --venue hardened --out runs/probes
$LAB_PYTHON harness/reach_run.py --profile hardened --case S01 --out runs/reach
infra/cluster.sh down --apply && infra/cluster.sh inventory --apply
```

The hardened profile restricts the control plane to `OPERATOR_CIDR` (your workstation's address as a /32);
set it in `lab.env` first. If it's unset, `harness/drive.sh` looks the address up from
`checkip.amazonaws.com`, a third-party (AWS) service: the only call the workstation makes outside Google Cloud
and the upstream downloads.

## 6. Agent Sandbox (optional)

```bash
infra/cluster.sh sandbox --apply                       # the cluster plus the gVisor node pool
infra/cluster.sh setup --profile sandbox --apply       # Agent Sandbox v1.0.6, its policy, the lab's template
$LAB_PYTHON harness/probe_run.py --venue sandbox --out runs/probes
```

## 7. Agent Substrate

Rerun `infra/cluster.sh quota` first; it must print `quota ok`. The wizard's region must be `$LAB_REGION`,
the one the check read: the wizard offers its own default region and zone, and the quota is per region.

Install by Google's documented path, an interactive wizard. `infra/substrate/INSTALL.md` has every answer the
lab gave; rehearse it free with `make dry-run` in `$SUBSTRATE_GKE_DIR`. Save the exit summary: it prints the
snapshot bucket (set `SUBSTRATE_BUCKET` in `lab.env`) and the teardown commands.

Then the lab's part, and the token supply, which keeps the gateway's injected token fresh during runs:

```bash
infra/cluster.sh setup --profile substrate --apply
kubectl config current-context                          # must name $LAB_CLUSTER in your project
$GOOGLE_AUTH_PY harness/token_secret.py --once          # must print "token written"
$GOOGLE_AUTH_PY harness/token_secret.py &               # then keep it fresh while you run
```

The token supply refuses any kubectl context but your cluster in your project. The token stays out of the
actor, but anyone who can read Secrets in its namespace can read it, and it stays valid for up to an hour
after the refresher stops.

The experiments, each mapped to its page on the project site. Run them one at a time on a new install, from
the repository root. All but the case set and team two make no model calls.

| Run | Command (prefix each with `$LAB_PYTHON`) | Site page |
|---|---|---|
| Token containment and the escape suite | `harness/probe_run.py --venue substrate --out runs/probes` | /tests/gateway-keys/ |
| Suspend and resume, continuity checks | `harness/resume_probe.py --explicit 20 --wake 20 --out runs/resume` | /tests/picks-up/ |
| The same from storage, past the 30-minute local copy | `harness/resume_probe.py --storage 12 --wait 2100 --out runs/resume` | /tests/picks-up/ |
| First request after a long idle, inside the cluster | `harness/incluster_wake.py --n 12 --wait 2100 --out runs/wake` | /tests/restore-500ms/ |
| Google's own benchmark under lab settings | `harness/google_bench.py list`, then `setup --apply`, `build --apply`, `run TEST --apply` | /tests/density-10x/ |
| The container control for density | `harness/density_control.py --apply --out runs/density` | /tests/density-10x/ |
| Warm-pool reuse and a long held call | `harness/remaining_small.py reuse`, `longcall SECONDS` | /tests/warm-pool/ |
| The Clinic case set | `harness/pilot.py --gke substrate --dangerous --cap 50 --out runs/caseset` with `LAB_SUBSTRATE_POLL=1` set | /tests/coding-environments/ |
| Its report | `harness/pilot_report.py runs/caseset` | /tests/coding-environments/ |
| **Needs team two.** Onboard team two (LoopControlBench, cloned to `$LCB_DIR`) | `harness/lcb_team.py onboard --apply` | /teams/ |
| Per-team identities and the isolation matrix | `harness/tenants.py setup`, `run`, `teardown` | /teams/ |
| Cross-team reach from a team-two actor | `harness/cross_team_probe.py` | /teams/ |
| The no-network actor, team two's process ceiling | `harness/remaining_small.py denyall`, `pids` | /tests/warm-pool/ |
| Team two's own work | `harness/lcb_team.py probes --apply`, `run --tasks 10 --cap 25 --apply` | /teams/ |

Everything from the onboarding row down needs team two: the isolation matrix runs one team against the
other, and the rest run inside a team-two actor. Without `onboard`, team two's calls are all refused, which
would read like an isolation result and isn't one, so those scripts stop with a message instead. If you
skip team two, skip those rows too.

Results land under `runs/`: `runs/resume/resume-<time>.json` and `storage-<time>.json` (each run keeps its
own record and atelet logs), and `runs/remaining/` for the isolation matrix, cross-team reach and the small
probes.

`LAB_SUBSTRATE_POLL=1` starts each agent call detached inside the actor and checks back every 10 seconds. A
request held open and silent through Substrate's router is cut at about 5 minutes; without the polled wait,
long agent calls fail as infrastructure.

`--dangerous` turns agy's own permission prompts off, so the runtime's controls are the only ones. That's the
point of the test, and it's why the containment probes come first: run the probe row and read its results
before any `--dangerous` run, and leave the flag off if anything there surprised you. `--cap` is the harness's
own stop on estimated model spend in dollars, priced from token counts; it's not a billing limit, which is
what the budget alert is for.

Agent Executor (a preview) has a venue and an image here, but no guided run: the lab stopped testing it after
its first run. To try it, install AX v0.4.0-rc.1 by its own docs, set `LAB_TOKEN_NAMESPACE=ax-system`,
`LAB_TOKEN_AX_LABEL=1` and `LAB_TOKEN_CLUSTER` for the token supply, and run
`$LAB_PYTHON harness/probe_run.py --venue executor`.

## 8. End every session parked

```bash
$GOOGLE_AUTH_PY harness/token_secret.py --delete   # stop the refresher first (kill its process)
$LAB_PYTHON harness/tenants.py teardown            # if you ran the isolation matrix
infra/cluster.sh park --apply                      # node pools to zero, and a manifest of what remains
infra/cluster.sh inventory --apply
```

`park` scales Substrate's control plane (its deployments and the postgres StatefulSet in `ate-system`) to
zero replicas, then the pools, the database last, and waits on each until GKE reports it done. Without the
first step the infra drain stalls about an hour on the API and identity servers' disruption budgets, and GKE
refuses any other resize on the cluster until it finishes, unpark included. The replica counts and pool sizes
go in the manifest; `unpark` restores both, waking every pool to at least one node. If park stops on a
sign-in failure, the cluster's state is unverified: sign in and run it again. A rerun keeps what the first one
recorded.

"Inventory clean" means nothing is running and nothing exists beyond what `park` recorded. It doesn't mean
nothing bills: a parked Substrate install keeps its cluster, the 500 GiB database disk, the snapshot buckets
and two firewall rules, about $1.70 a day for the lab. `infra/cluster.sh unpark --apply` resumes.

When you're done for good: `infra/cluster.sh substrate-down --apply` runs Google's teardown in order (the
actors, `ate-setup delete`, the database disk, `tools/cleanup-gcp`), then the lab's own `down` and the
inventory. It needs `SUBSTRATE_BUCKET`. Expect a run of `ERROR` lines near the end, 404s for clusters, a
router, a subnet, a network and DNS policies the other steps would have made (`lab-vulnerable`,
`lab-prereq`, `lab-sandbox`, `lab-net`): `down` deletes every rung's resources whether or not they exist.
They're harmless. The line that counts is the last one, `inventory clean`. Then remove what step 1 created, if you won't run again: the Artifact
Registry repository, the agent's service account (which removes its bindings), and the lab's gcloud config
directory. Never assume a teardown worked: read the inventory.

## What to change, and what not to

The probes, the judge and the cases are the instrument; change them and you're measuring something else,
which is fine, but say so beside the result. The venues, the cluster sizes, the model and the case set are the
settings; the lab's are in `lab.env.example` and this guide. Raw results land in `runs/` (git-ignored); keep
them, including the runs you exclude, with the reason.
