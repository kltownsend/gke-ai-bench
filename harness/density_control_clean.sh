#!/bin/bash
# The plain-Pod control with Substrate's worker pools scaled to zero (item 2, second half). Deploys
# Google's workloads only to read the glutton template's image, scales both worker pools to zero,
# runs the control on the substrate node pool, then restores the lab's pool and deletes the workloads.
set -u
R=runs/remaining-20261122; PY=${LAB_PYTHON:-python3}
deploy() {  # $1: False to deploy, True to delete
  $PY - --apply "$1" <<'PYEOF'
import sys; sys.path.insert(0, "harness"); import google_bench as G
G.deploy_workloads({"worker_count": 2, "actor_memory": "1Gi"}, delete=(sys.argv[2] == "True"))
PYEOF
}
echo "$(date '+%H:%M:%S') deploy workloads"; deploy False > $R/bench-deploy-control.log 2>&1; echo "deploy exit $?"
kubectl -n benchmark-workloads patch workerpool benchmark-worker --type merge -p '{"spec":{"replicas":0}}'
kubectl -n lab-substrate patch workerpool lab-agent-workers --type merge -p '{"spec":{"replicas":0}}'
for i in $(seq 60); do
  left=$(kubectl get pods -A --no-headers 2>/dev/null | grep -c -E "benchmark-worker|lab-agent-workers")
  [ "$left" = 0 ] && break; sleep 10; done
echo "$(date '+%H:%M:%S') worker pods left: $left"
timeout 2400 $PY harness/density_control.py --nodepool substrate-node-pool --cpu 1 --memory 1Gi --step 2 --max 64 --apply --out $R --label density-control-clean > $R/density-control-clean.log 2>&1
echo "control exit $?"; tail -1 $R/density-control-clean.log
kubectl -n lab-substrate patch workerpool lab-agent-workers --type merge -p '{"spec":{"replicas":6}}'
echo "$(date '+%H:%M:%S') delete workloads"; deploy True > $R/bench-delete-control.log 2>&1; echo "delete exit $?"
for i in $(seq 60); do
  ready=$(kubectl -n lab-substrate get pods --no-headers 2>/dev/null | grep -c "1/1")
  [ "$ready" = 6 ] && break; sleep 10; done
echo "$(date '+%H:%M:%S') lab workers ready: $ready"
