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
  [[ -f $2 ]] || { curl -fsSL -o "$2.part" "$1" && mv "$2.part" "$2"; }
  echo "$3  $2" | $SUM >/dev/null || { echo "checksum mismatch: $2" >&2; rm -f "$2"; exit 1; }
  echo "ok  $2"
}
extract() { # DIR: unpack the one checked tarball in DIR into DIR/x, fresh every time
  # Always from the archive whose checksum just passed, into a new directory that replaces DIR/x only
  # once it's complete, so a binary already sitting in DIR/x is never trusted. Only regular files and
  # directories with plain relative paths are accepted: no links, devices, absolute or parent paths.
  local t new; t=$(ls "$1"/agy_cli_linux_*.tar.gz); new=$(mktemp -d "$1/.x.XXXXXX")
  python3 -I - "$t" "$new" <<'PY' || { rm -rf "$new"; echo "refused $t" >&2; exit 1; }
import os, sys, tarfile
t, out = sys.argv[1], sys.argv[2]
with tarfile.open(t, "r:gz") as tf:
    for m in tf.getmembers():
        n = os.path.normpath(m.name)
        if not (m.isreg() or m.isdir()) or os.path.isabs(m.name) or n == ".." or n.startswith("../"):
            sys.exit(f"unsafe member {m.name!r} ({'link' if m.issym() or m.islnk() else 'type ' + repr(m.type)})")
    tf.extractall(out, filter="data")
PY
  [[ -f $new/antigravity ]] || { rm -rf "$new"; echo "expected antigravity in $t" >&2; exit 1; }
  rm -rf "$1/x"; mv "$new" "$1/x"
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
