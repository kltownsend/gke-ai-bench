# Source this before any lab run that calls Google Cloud or a Google model:
#   cp lab.env.example lab.env     # once; fill in your project and the rest
#   source harness/lab_env.sh
# It reads lab.env at the repository root (git-ignored; LAB_ENV_FILE points elsewhere), then
# derives the rest. It points gcloud and Application Default Credentials at a lab-only config
# directory whose credentials carry $LAB_PROJECT as their quota project, so the operator's global
# gcloud and ADC defaults are untouched. The Antigravity adapter refuses to call the model unless
# these hold. Works from bash and zsh.
if [ -n "${BASH_SOURCE:-}" ]; then _lab_here=${BASH_SOURCE[0]}; elif [ -n "${ZSH_VERSION:-}" ]; then
  eval '_lab_here=${(%):-%x}'; else _lab_here=$0; fi
LAB_ROOT="$(cd "$(dirname "$_lab_here")/.." && pwd)"; unset _lab_here
export LAB_ROOT
_lab_env_file="${LAB_ENV_FILE:-$LAB_ROOT/lab.env}"
if [ -f "$_lab_env_file" ]; then
  set -a; . "$_lab_env_file"; set +a
else
  echo "lab_env.sh: $_lab_env_file not found; copy lab.env.example to lab.env and fill it in" >&2
fi
unset _lab_env_file
: "${LAB_PROJECT:?set LAB_PROJECT in lab.env (see lab.env.example)}"
if [ "$LAB_PROJECT" = "your-project-id" ]; then echo "lab.env still has the placeholder LAB_PROJECT" >&2; return 1 2>/dev/null || exit 1; fi
export LAB_PROJECT
export LAB_REGION="${LAB_REGION:-us-central1}"
export LAB_ZONE="${LAB_ZONE:-$LAB_REGION-a}"
export LAB_CLUSTER="${LAB_CLUSTER:-lab-substrate}"
export LAB_AGENT_SA_NAME="${LAB_AGENT_SA_NAME:-lab-agent}"
export LAB_AGENT="$LAB_AGENT_SA_NAME@$LAB_PROJECT.iam.gserviceaccount.com"
export LAB_REGISTRY="$LAB_REGION-docker.pkg.dev/$LAB_PROJECT/lab-images"
export LAB_MODEL="${LAB_MODEL:-gemini-4-hydrogen-medium}"

export CLOUDSDK_CONFIG="${LAB_GCLOUD_CONFIG:-$HOME/.config/gcloud-$LAB_PROJECT}"
export CLOUDSDK_CORE_PROJECT="$LAB_PROJECT"
export GOOGLE_APPLICATION_CREDENTIALS="$CLOUDSDK_CONFIG/application_default_credentials.json"
export GOOGLE_CLOUD_QUOTA_PROJECT="$LAB_PROJECT"
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
export AGY_ADC_AUTH=true
# One-time, in a shell that sourced this file:
#   gcloud auth application-default login --impersonate-service-account="$LAB_AGENT" --project "$LAB_PROJECT"
# kubectl reaches GKE through Google's auth plugin, installed with gcloud components. PATH changes
# apply to lab shells only; LAB_EXTRA_PATH (lab.env) prepends, for example, a gcloud SDK's bin.
[ -n "${LAB_EXTRA_PATH:-}" ] && export PATH="$LAB_EXTRA_PATH:$PATH"
export USE_GKE_GCLOUD_AUTH_PLUGIN=True
# kubectl credentials for lab clusters live beside the lab gcloud config, not in ~/.kube/config.
export KUBECONFIG="$CLOUDSDK_CONFIG/kubeconfig"

# Images: built and pushed by you, pinned by digest, set in lab.env.
export LAB_AGENT_IMAGE="${LAB_AGENT_IMAGE:-}" LAB_ACTOR_IMAGE="${LAB_ACTOR_IMAGE:-}"
export LAB_EXECUTOR_IMAGE="${LAB_EXECUTOR_IMAGE:-}" LCB_ACTOR_IMAGE="${LCB_ACTOR_IMAGE:-}"
export LAB_BENCH_IMAGE="${LAB_BENCH_IMAGE:-$LAB_REGISTRY/locust-test:v1.0.1}"

# Python: the harness interpreter, and one with google-auth for the token minters.
export LAB_PYTHON="${LAB_PYTHON:-python3}"
export GOOGLE_AUTH_PY="${GOOGLE_AUTH_PY:-$LAB_PYTHON}"

# Agent Substrate rung: kubectl-ate v1.0.1 (go install from the pinned checkout) and the checkouts.
[ -d "$HOME/go/bin" ] && export PATH="$HOME/go/bin:$PATH"
export LAB_SRC="${LAB_SRC:-$HOME/lab-src}"
export SUBSTRATE_DIR="${SUBSTRATE_DIR:-$LAB_SRC/substrate}"
export SUBSTRATE_GKE_DIR="${SUBSTRATE_GKE_DIR:-$LAB_SRC/substrate-gke}"
export SUBSTRATE_BUCKET="${SUBSTRATE_BUCKET:-}"   # from the installer's exit summary
export CLINICBENCH_DIR="${CLINICBENCH_DIR:-$LAB_SRC/clinicbench}"
# Agent Executor rung: the ax CLI built at v0.4.0-rc.1 (go install ./cmd/ax in AX_DIR).
export AX_DIR="${AX_DIR:-$LAB_SRC/ax}"
export AX_BIN="${AX_BIN:-$AX_DIR/bin/ax}"
[ -d "$AX_DIR/bin" ] && export PATH="$AX_DIR/bin:$PATH"
true
