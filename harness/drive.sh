#!/usr/bin/env bash
# One cluster at a time: create, set up, [listener], probe, [reach], tear down, inventory.
# Teardown runs whatever happens. PROFILES picks clusters; REACH=1 adds the listener and a reach run.
cd "$(dirname "$0")/.." && source harness/lab_env.sh
export OPERATOR_CIDR="$(curl -s --max-time 10 https://checkip.amazonaws.com)/32"
R=${OUT:-runs/probes-20261121}; mkdir -p "$R"
for PR in ${PROFILES:-default hardened vulnerable}; do
  echo "== $PR $(date '+%H:%M:%S')"
  # Substrate's cluster comes from Google's installer, driven by an operator (infra/substrate/INSTALL.md),
  # so there's no create step; the token refresher keeps the gateway's credential fresh for the run.
  if [[ $PR == substrate || $PR == executor ]]; then echo "create: by the installer"; else
    infra/cluster.sh "$PR" --apply > "$R/$PR-create.log" 2>&1; echo "create exit $?"; fi
  infra/cluster.sh setup --profile "$PR" --apply > "$R/$PR-setup.log" 2>&1; echo "setup exit $?"
  REFRESH=""
  if [[ $PR == substrate ]]; then
    "$GOOGLE_AUTH_PY" harness/token_secret.py > "$R/$PR-token.log" 2>&1 & REFRESH=$!; fi
  if [[ $PR == executor ]]; then
    LAB_TOKEN_NAMESPACE=ax-system LAB_TOKEN_AX_LABEL=1 "$GOOGLE_AUTH_PY" harness/token_secret.py > "$R/$PR-token.log" 2>&1 & REFRESH=$!; fi
  grep "neighbor canary" "$R/$PR-setup.log"
  kubectl -n lab-neighbor wait --for=condition=Ready pod/neighbor --timeout=180s >/dev/null 2>&1; echo "neighbor ready $?"
  LIS=()
  if [[ ${REACH:-0} == 1 ]]; then
    infra/cluster.sh listener --profile "$PR" --apply > "$R/$PR-listener.log" 2>&1; echo "listener exit $?"
    for i in $(seq 1 30); do
      gcloud compute instances get-serial-port-output lab-listener --zone "$LAB_ZONE" --port 1 2>/dev/null \
        | grep -q '"event": "listening"' && { echo "listener up"; break; }; sleep 10; done
    EXT=$(gcloud compute instances describe lab-listener --zone "$LAB_ZONE" --format "value(networkInterfaces[0].accessConfigs[0].natIP)")
    LIS=(--listener "http://$EXT:8080")
  fi
  # The Agent Sandbox cluster carries two templates; probe both (documented defaults, and the
  # agent's), and run the agent's work on the agent template only.
  if [[ $PR == sandbox ]]; then VENUES="sandbox-documented sandbox"; AGENT=sandbox; else VENUES="$PR"; AGENT="$PR"; fi
  for VN in $VENUES; do
    for rep in $(seq 1 ${REPEATS:-1}); do
      "${LAB_PYTHON:-python3}" harness/probe_run.py --venue "$VN" --out "$R" --label "$VN-p$rep" "${LIS[@]}" > "$R/$VN-probe$rep.log" 2>&1; echo "probe $VN $rep exit $?"
    done
  done
  if [[ ${CANARY:-0} == 1 ]]; then
    "${LAB_PYTHON:-python3}" harness/canary_gke.py S01 --profile "$AGENT" --budget 3 --out "$R" --label "canary-$AGENT" > "$R/$AGENT-canary.log" 2>&1; echo "canary exit $?"
  fi
  if [[ ${REACH:-0} == 1 ]]; then
    "${LAB_PYTHON:-python3}" harness/reach_run.py --profile "$AGENT" --out "$R" --label "reach-$AGENT" > "$R/$AGENT-reach.log" 2>&1; echo "reach exit $?"
    tail -3 "$R/$AGENT-reach.log"
    "${LAB_PYTHON:-python3}" harness/reach_run.py --profile "$AGENT" --direct --out "$R" --label "direct-$AGENT" > "$R/$AGENT-direct.log" 2>&1; echo "direct exit $?"
    tail -20 "$R/$PR-direct.log"
  fi
  [[ -n $REFRESH ]] && kill "$REFRESH" 2>/dev/null
  if [[ $PR == substrate ]]; then
    infra/cluster.sh substrate-down --apply > "$R/$PR-down.log" 2>&1; echo "down exit $?"
  elif [[ $PR == executor ]]; then
    infra/cluster.sh executor-down --apply > "$R/$PR-down.log" 2>&1; echo "down exit $?"
  else
    infra/cluster.sh down --apply > "$R/$PR-down.log" 2>&1; echo "down exit $?"
  fi
  gcloud container clusters list --format="value(name)" | grep . && echo "CLUSTER LEFT" || echo "no clusters"
  gcloud compute instances list --format="value(name)" | grep . && echo "INSTANCE LEFT" || echo "no instances"
  echo "== $PR done $(date '+%H:%M:%S')"
done
