# Source this for the local Substrate shakeout (kind on Docker Desktop; apparatus checks only, never
# results). Its own kubeconfig and Docker config, so neither the global kube context nor the
# Docker credential helper (it can hang) is touched. Both live in a local-only directory,
# LAB_LOCAL_CONFIG (default ~/.config/lab-kind-local); no Google project is involved.
LAB_LOCAL_CONFIG="${LAB_LOCAL_CONFIG:-$HOME/.config/lab-kind-local}"
export KUBECONFIG=$LAB_LOCAL_CONFIG/kubeconfig-kind
export DOCKER_CONFIG=$LAB_LOCAL_CONFIG/docker-kind
mkdir -p "$DOCKER_CONFIG"
[ -f "$DOCKER_CONFIG/config.json" ] || echo '{"currentContext":"desktop-linux"}' > "$DOCKER_CONFIG/config.json"
for d in contexts cli-plugins buildx; do [ -e "$DOCKER_CONFIG/$d" ] || ln -s "$HOME/.docker/$d" "$DOCKER_CONFIG/$d"; done
export ATE_INSTALL_KIND=true
export KO_DOCKER_REPO=localhost:5001
[ -d "$HOME/go/bin" ] && export PATH="$HOME/go/bin:$PATH"
export SUBSTRATE_DIR="${SUBSTRATE_DIR:-${LAB_SRC:-$HOME/lab-src}/substrate}"
true
