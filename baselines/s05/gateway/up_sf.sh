#!/bin/bash
# Start the three Haiku gateways for the same-freeze cells (proposal 2026-10-26) on 127.0.0.1:4011-4013.
# The Anthropic key comes from the environment, or from the Keychain item named as $1. LiteLLM won't
# start without a master key, so this makes a throwaway one for these gateways and leaves it, mode
# 600, at results/.gateway_master_key for the runner (it sends it as PB_API_KEY).
# usage: gateway/up_sf.sh [keychain-item]
set -eu
cd "$(dirname "$0")"
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  [ $# -ge 1 ] || { echo "set ANTHROPIC_API_KEY or name the Keychain item"; exit 1; }
  ANTHROPIC_API_KEY="$(security find-generic-password -s "$1" -w)"
fi
export ANTHROPIC_API_KEY CB_ANTHROPIC_BASE=https://api.anthropic.com
umask 077
export LITELLM_MASTER_KEY="sk-$(openssl rand -hex 32)"
printf '%s' "$LITELLM_MASTER_KEY" > ../results/.gateway_master_key
for c in "litellm-haiku.yaml 4011" "litellm-haiku-think.yaml 4012" "litellm-haiku-fewshot.yaml 4013"; do
  set -- $c
  nohup .venv/bin/litellm --config "$1" --port "$2" --host 127.0.0.1 > "${1%.yaml}-sf.log" 2>&1 < /dev/null &
done
sleep 20
for p in 4011 4012 4013; do
  curl -s -m 3 "http://127.0.0.1:$p/health/liveliness" > /dev/null && echo "$p up" || echo "$p down"
done
