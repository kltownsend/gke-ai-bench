#!/usr/bin/env bash
# Fetch the third-party pieces this repository doesn't redistribute, at the exact versions the lab
# ran, and check each against the sha256 the lab recorded. Run once from the repository root.
#   ./fetch_vendor.sh            # agy (Linux x64 and arm64) and the Agent Sandbox v1.0.6 manifests
# agy is Google's Antigravity CLI, used under Google's terms. The Agent Sandbox manifests are
# kubernetes-sigs/agent-sandbox, Apache 2.0. Both land in git-ignored directories.
set -euo pipefail
cd "$(dirname "$0")"
get() { # URL DEST SHA256
  mkdir -p "$(dirname "$2")"
  [[ -f $2 ]] || curl -fsSL -o "$2" "$1"
  echo "$3  $2" | shasum -a 256 -c - >/dev/null || { echo "checksum mismatch: $2" >&2; rm -f "$2"; exit 1; }
  echo "ok  $2"
}
AGY=https://github.com/google-antigravity/antigravity-cli/releases/download/1.5.13
get "$AGY/agy_cli_linux_x64.tar.gz"   agent/vendor/agy-1.5.13-x64/agy_cli_linux_x64.tar.gz \
    7fa9cc6df0d09562693d637c406a86c1b0f52ab3364714b1fd2d79cc6060d4b2
get "$AGY/agy_cli_linux_arm64.tar.gz" agent/vendor/agy-1.5.13/agy_cli_linux_arm64.tar.gz \
    36596ad8038531fd75f0e97b5ba8967a30f2fd67f8d9c61959d2e4761d7b215f
for d in agent/vendor/agy-1.5.13-x64 agent/vendor/agy-1.5.13; do
  mkdir -p "$d/x"; [[ -f $d/x/antigravity ]] || tar -xzf "$d"/agy_cli_linux_*.tar.gz -C "$d/x"
  [[ -f $d/x/antigravity ]] || { echo "expected $d/x/antigravity after extracting" >&2; exit 1; }
done
AS=https://github.com/kubernetes-sigs/agent-sandbox
V=infra/vendor/agent-sandbox-v1.0.6
get "$AS/releases/download/v1.0.6/sandbox-with-extensions.yaml" $V/sandbox-with-extensions.yaml \
    e6fa909d82392b201ef80003dd91390450266ba356caa9be04a1c89c3caa65dc
get "https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/v1.0.6/examples/policy/vap/secure-sandbox-policy.yaml" \
    $V/secure-sandbox-policy.yaml ad368c0df20532b3382c8b13d65f892966c0919339796edebac7950d8c3719b3
get "https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/v1.0.6/examples/policy/vap/secure-sandbox-binding.yaml" \
    $V/secure-sandbox-binding.yaml c78d92b0eb0d1feffe2938bd9bc252e4d5d31996487497ce1cfd518640185ed3
