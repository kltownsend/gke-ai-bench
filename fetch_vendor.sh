#!/usr/bin/env bash
# Fetch the third-party pieces this repository doesn't redistribute, at the exact versions the lab
# ran, and check each against the sha256 the lab recorded when it downloaded them. Run once from the
# repository root:
#   ./fetch_vendor.sh --yes
# Without --yes it only prints what it would download.
#   - agy 1.5.13, Google's Antigravity CLI (Linux x64 and arm64, about 190 MB), used under Google's
#     terms: https://github.com/google-antigravity/antigravity-cli
#   - Agent Sandbox v1.0.6 manifests, kubernetes-sigs/agent-sandbox, Apache License 2.0
#     (LICENSES/Apache-2.0.txt)
# The checksums are the ones the lab recorded; they guard against a changed upstream file, not against
# an upstream that was already compromised when the lab fetched it.
set -euo pipefail
cd "$(dirname "$0")"
YES=; [[ ${1:-} == --yes ]] && YES=1
if command -v sha256sum >/dev/null; then SUM="sha256sum -c -"; else SUM="shasum -a 256 -c -"; fi
get() { # URL DEST SHA256
  if [[ -z $YES ]]; then echo "would fetch $1 -> $2"; return; fi
  mkdir -p "$(dirname "$2")"
  [[ -f $2 ]] || curl -fsSL -o "$2" "$1"
  echo "$3  $2" | $SUM >/dev/null || { echo "checksum mismatch: $2" >&2; rm -f "$2"; exit 1; }
  echo "ok  $2"
}
extract() { # DIR: unpack the one tarball in DIR into DIR/x, refusing absolute or parent paths
  local t; t=$(ls "$1"/agy_cli_linux_*.tar.gz)
  if tar -tzf "$t" | grep -qE '^/|(^|/)\.\.(/|$)'; then echo "unsafe path in $t" >&2; exit 1; fi
  mkdir -p "$1/x"; [[ -f $1/x/antigravity ]] || tar -xzf "$t" -C "$1/x"
  [[ -f $1/x/antigravity ]] || { echo "expected $1/x/antigravity after extracting" >&2; exit 1; }
}
AGY=https://github.com/google-antigravity/antigravity-cli/releases/download/1.5.13
get "$AGY/agy_cli_linux_x64.tar.gz"   agent/vendor/agy-1.5.13-x64/agy_cli_linux_x64.tar.gz \
    7fa9cc6df0d09562693d637c406a86c1b0f52ab3364714b1fd2d79cc6060d4b2
get "$AGY/agy_cli_linux_arm64.tar.gz" agent/vendor/agy-1.5.13/agy_cli_linux_arm64.tar.gz \
    36596ad8038531fd75f0e97b5ba8967a30f2fd67f8d9c61959d2e4761d7b215f
AS=https://github.com/kubernetes-sigs/agent-sandbox
V=infra/vendor/agent-sandbox-v1.0.6
get "$AS/releases/download/v1.0.6/sandbox-with-extensions.yaml" $V/sandbox-with-extensions.yaml \
    e6fa909d82392b201ef80003dd91390450266ba356caa9be04a1c89c3caa65dc
get "https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/v1.0.6/examples/policy/vap/secure-sandbox-policy.yaml" \
    $V/secure-sandbox-policy.yaml ad368c0df20532b3382c8b13d65f892966c0919339796edebac7950d8c3719b3
get "https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/v1.0.6/examples/policy/vap/secure-sandbox-binding.yaml" \
    $V/secure-sandbox-binding.yaml c78d92b0eb0d1feffe2938bd9bc252e4d5d31996487497ce1cfd518640185ed3
[[ -z $YES ]] && { echo "dry run: rerun with --yes to download"; exit 0; }
extract agent/vendor/agy-1.5.13-x64
extract agent/vendor/agy-1.5.13
