#!/bin/bash
# Item 2, density done cleanly. Google's agent-session
# ladder at 16, 32 and 64 with every leftover session actor deleted between steps (the first ladder's
# 16 left 12 actors holding the workers, so 32 and 64 never deployed), then the plain-Pod control with
# Substrate's worker pools scaled to zero (the first control shared nodes with Substrate's workers).
# Source harness/lab_env.sh first; DOCKER_CONFIG set; google_bench setup done.
set -u
R=runs/remaining; mkdir -p $R; PY=${LAB_PYTHON:-python3}
clean() {  # every actor in Google's session atespace, gone
  for a in $(kubectl ate get actors -a benchmark 2>/dev/null | awk 'NR>1{print $2}'); do
    kubectl ate delete actor "$a" -a benchmark >/dev/null 2>&1; done
  echo "$(date '+%H:%M:%S') cleaned: $(kubectl ate get actors -a benchmark 2>/dev/null | awk 'NR>1' | wc -l | tr -d ' ') left"
}
clean
for n in 16 32 64; do
  echo "$(date '+%H:%M:%S') density $n"
  timeout 2700 $PY harness/google_bench.py run agentsession_density_$n --apply --out $R > $R/bench-density-$n.log 2>&1
  echo "density $n exit $?"; clean
done
echo "$(date '+%H:%M:%S') control: deploy workloads for the glutton template, then Substrate's workers to zero"
$PY - --apply <<'PYEOF'
import sys; sys.path.insert(0, "harness"); import google_bench as G
G.deploy_workloads({"worker_count": 2, "actor_memory": "1Gi"}, delete=False)
PYEOF > $R/bench-deploy-control.log 2>&1
kubectl -n benchmark-workloads patch workerpool benchmark-worker --type merge -p '{"spec":{"replicas":0}}'
kubectl -n lab-substrate patch workerpool lab-agent-workers --type merge -p '{"spec":{"replicas":0}}'
for i in $(seq 60); do
  left=$(kubectl get pods -A --no-headers 2>/dev/null | grep -c -E "benchmark-worker|lab-agent-workers")
  [ "$left" = 0 ] && break; sleep 10; done
echo "$(date '+%H:%M:%S') worker pods left: $left"
timeout 2400 $PY harness/density_control.py --nodepool substrate-node-pool --cpu 1 --memory 1Gi --step 2 --max 64 --apply --out $R --label density-control-clean > $R/density-control-clean.log 2>&1
echo "control exit $?"; tail -1 $R/density-control-clean.log
kubectl -n lab-substrate patch workerpool lab-agent-workers --type merge -p '{"spec":{"replicas":6}}'
$PY - --apply <<'PYEOF'
import sys; sys.path.insert(0, "harness"); import google_bench as G
G.deploy_workloads({"worker_count": 2, "actor_memory": "1Gi"}, delete=True)
PYEOF > $R/bench-delete-control.log 2>&1
echo "$(date '+%H:%M:%S') done"
