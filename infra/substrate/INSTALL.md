# Installing Agent Substrate for the lab (v1.0.1)

Google's install page names one path: the substrate-gke installer, an interactive terminal wizard
("Piped or redirected runs exit with a message instead of running"). An operator drives its
prompts; that time goes on the time ledger as the install's human cost. The installer creates the
cluster, and the lab follows it: where a runtime's installer creates its own cluster, that's the
vendor-supported path.

Two disclosed departures from the page's literal commands:
- **Pinned checkout, not `curl | bash` of `main`.** The installer runs from a local clone of
  ai-on-gke/substrate-gke at `0de82014290a31321a9c9f1f6cde48b1d7f48b66` (main, 2026-11-12); its
  `install.sh` detects that it's in a checkout and runs that tree.
- **v1.0.1 images.** At the images step: pre-built, registry `ghcr.io/agent-substrate/substrate`,
  tag `v1.0.1`, commit `20778a6dbb01f13024642851bb592bf67c9fec05` (v1.0.1 carries the fix for an
  actor with a `*` rule reaching the metadata server; the installer pins v1.0.0).

## Before (no spend)

1. Go 1.27.3 or later on the workstation (the installer refuses to start without it).
2. A gcloud config of the installer's own, so neither the global config nor the lab config's
   impersonated ADC (which `agent/mint_token.py` and `harness/token_secret.py` depend on) is touched:

       source infra/substrate/installer_env.sh   # reads lab.env; CLOUDSDK_CONFIG=~/.config/gcloud-$LAB_PROJECT-substrate
       gcloud auth login                         # the operator, in the browser
       gcloud auth application-default login     # writes ADC inside this config only
       gcloud config set project "$LAB_PROJECT"
       gcloud config set compute/zone "$LAB_ZONE"

3. `kubectl ate` at the same version: `git clone --depth 1 --branch v1.0.1
   https://github.com/agent-substrate/substrate.git` into a scratch directory, then
   `go install ./cmd/kubectl-ate`.
4. Rehearse for free: `make dry-run` in the substrate-gke checkout walks every prompt without
   touching Google Cloud.

## The wizard's answers

| Step | Answer |
|---|---|
| 1 Check your setup | fix anything it flags; Docker only warns (pre-built images) |
| 2 Images | pre-built; `ghcr.io/agent-substrate/substrate`, tag `v1.0.1`, commit `20778a6d...` |
| 3 Project | your `LAB_PROJECT` |
| 4 Cluster | create new: your `LAB_CLUSTER` (default `lab-substrate`), zone `LAB_ZONE`, defaults otherwise (1.37, Rapid) |
| 5 Provision | accept; put the snapshot bucket name it prints in lab.env as `SUBSTRATE_BUCKET` (teardown needs it) |
| 6 Ingress clients | `spiffe://cluster.local/ns/ate-system/sa/atenet-client` (what `kubectl ate proxy` presents) |
| 7 Turn on Substrate | accept; the credential provider is the bundled `k8s.io` one by default |
| 8 Filestore | skip |
| 9 Autoscaling | skip |
| 10 Sandbox runtime | gVisor |

Save the exit summary: it prints the teardown commands pre-filled.

## After (the lab's part)

In the lab's own shell (`source harness/lab_env.sh`, the lab KUBECONFIG), with LAB_ACTOR_IMAGE in
lab.env (agent/Dockerfile.actor, built and pushed to `$LAB_REGISTRY` and pinned by digest):

    infra/cluster.sh setup --profile substrate --apply
    $GOOGLE_AUTH_PY harness/token_secret.py &        # keeps the gateway's token fresh during the run
    infra/cluster.sh listener --profile substrate --apply

## Teardown

    infra/cluster.sh substrate-down --apply      # SUBSTRATE_GKE_DIR, SUBSTRATE_DIR, SUBSTRATE_BUCKET from lab.env

That runs, in Google's order: the lab's actors and template, `ate-setup delete ate-system`, a wait
until the Postgres disk's PV is released, `tools/cleanup-gcp` (cluster, buckets, their IAM
bindings), then the lab's own `down` and the inventory assertion. Never infer teardown: the
inventory must print "inventory clean".
