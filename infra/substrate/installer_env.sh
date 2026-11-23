# Source this in the terminal where the operator runs the Substrate installer (infra/substrate/INSTALL.md).
# Its own gcloud config: the installer's ADC login lands here, not in ~/.config/gcloud (global) or
# the lab's config directory (whose ADC is the impersonated agent credential the token minters use).
# Reads lab.env for the project and the checkout paths.
_lab_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
if [ -f "${LAB_ENV_FILE:-$_lab_root/lab.env}" ]; then set -a; . "${LAB_ENV_FILE:-$_lab_root/lab.env}"; set +a; fi
: "${LAB_PROJECT:?set LAB_PROJECT in lab.env (see lab.env.example)}"
export CLOUDSDK_CONFIG="$HOME/.config/gcloud-$LAB_PROJECT-substrate"
export GOOGLE_APPLICATION_CREDENTIALS=$CLOUDSDK_CONFIG/application_default_credentials.json
# PATH additions for this shell only: LAB_EXTRA_PATH from lab.env (a gcloud SDK's bin, say), and
# Go's bin for kubectl-ate if it exists.
[ -n "${LAB_EXTRA_PATH:-}" ] && export PATH="$LAB_EXTRA_PATH:$PATH"
[ -d "$HOME/go/bin" ] && export PATH="$HOME/go/bin:$PATH"
export USE_GKE_GCLOUD_AUTH_PLUGIN=True
cd "${SUBSTRATE_GKE_DIR:-${LAB_SRC:-$HOME/lab-src}/substrate-gke}"
unset _lab_root
