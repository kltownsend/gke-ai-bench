#!/bin/sh
# Concurrent-load sampler: both teams at once on one install, read as density.
# Every 30 s: a UTC timestamp, then each non-benchmark actor's atespace, state and worker pod. Stop it
# with a kill when both runs end. Source harness/lab_env.sh first.
out=${1:?out file}
while :; do
  printf '%s\n' "## $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$out"
  kubectl ate get actors -A 2>/dev/null | awk 'NR>1 && $1!~/^benchmark/{print $1, $4, $5}' >> "$out"
  sleep 30
done
