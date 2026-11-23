#!/usr/bin/env bash
# The plain GKE baselines as gcloud commands. Dry run by default: prints what it would run.
# Read the printed commands and price them before passing --apply: clusters and VMs bill by the hour.
#
#   source harness/lab_env.sh
#   infra/cluster.sh PROFILE [--apply]      PROFILE: default | hardened | vulnerable | prereq | sandbox | substrate-down | executor-down | park | unpark | down | inventory
#   infra/cluster.sh setup --profile PROFILE [--apply]
#   infra/cluster.sh listener --profile PROFILE [--apply]     (after setup: the canary listener)
#
# One cluster at a time, deleted between sessions; teardown is verified by listing, not assumed.
# Facts behind the flags (Google docs, read 2026-11-21; notes, same date):
#  - Substrate: Standard only, control plane AND node pools 1.37+, not the Extended channel,
#    Dataplane V2, Workload Identity, c3-standard-4 or larger, no E2.
#  - Agent Sandbox: a separate node pool with --sandbox=type=gvisor and cos_containerd.
#  - The project's default is 1.36.4, so 1.37 is pinned; Regular offers 1.37.1-gke.1552000.
set -euo pipefail
PROFILE=${1:?profile}; APPLY=${2:-}
P=${LAB_PROJECT:?source harness/lab_env.sh}; REGION=${LAB_REGION:?source harness/lab_env.sh}
ZONE=${LAB_ZONE:?source harness/lab_env.sh}; VER=1.37.1-gke.1552000
AGENT_SA=${LAB_AGENT:?source harness/lab_env.sh}      # the agent's Google service account
NAME=lab-$PROFILE; MT=c3-standard-4
run() { if [[ $APPLY == --apply ]]; then "$@"; else printf '%q ' "$@"; echo; fi; }
# For a step fed by a pipe: the dry run drains stdin, so the writer doesn't die of SIGPIPE under pipefail.
runp() { if [[ $APPLY == --apply ]]; then "$@"; else cat >/dev/null; printf '%q ' "$@"; echo '  < (stdin)'; fi; }
# Fills a manifest's placeholders from the lab environment, as
# envsubst '${LAB_PROJECT} ${LAB_AGENT}' would (no gettext needed). Reads FILE or stdin.
render() { sed -e "s#\${LAB_PROJECT}#$P#g" -e "s#\${LAB_AGENT}#$AGENT_SA#g" "$@"; }
# The project number, for Workload Identity principals and the default compute service account.
# Looked up at runtime (LAB_PROJECT_NUMBER in lab.env skips the lookup); a dry run without gcloud
# access prints a placeholder.
project_number() {
  if [[ -n ${LAB_PROJECT_NUMBER:-} ]]; then echo "$LAB_PROJECT_NUMBER"; return; fi
  local n; n=$(gcloud projects describe "$P" --format='value(projectNumber)' 2>/dev/null || true)
  if [[ -n $n ]]; then echo "$n"; elif [[ $APPLY == --apply ]]; then
    echo "cannot read the project number of $P" >&2; exit 2; else echo PROJECT_NUMBER; fi
}

# The evidence floor, the same on every cluster (build review 5): VPC flow logs on the subnet the
# nodes use and Cloud DNS query logging on its network. Network policy logging is added in setup
# where Dataplane V2 exists. Logging is observation, not a control; on the default network it's
# the one change the lab makes, and it's disclosed.
evidence_floor() {   # NETWORK SUBNET
  run gcloud compute networks subnets update "$2" --project "$P" --region "$REGION" --enable-flow-logs
  run gcloud dns policies create "lab-dns-log-$1" --project "$P" --networks "$1" --enable-logging \
      --description "lab evidence floor: DNS query logs" || true    # one policy per network; reruns find it
}

case $PROFILE in setup|substrate-down|inventory) PN=$(project_number) ;; *) PN= ;; esac

case $PROFILE in
default)
  # The minimum compatible cluster (build review 5's name). What a team gets by accepting defaults: default network, default compute service account
  # (project Editor), no network policy enforcement. Only the version and machine are set,
  # because the spec needs 1.37 and the later rungs need C3.
  # Plus the one step a team has to add before the agent can call its model at all (counted as
  # an added step): on defaults the node's scopes don't include Vertex AI
  # (probe: 403). The step is Workload Identity, Google's documented way for a GKE workload to call
  # Google APIs: the cluster flag here, the binding below, the service account annotation in setup.
  # Counted in the operations ledger. Nothing else changes.
  evidence_floor default default
  run gcloud container clusters create "$NAME" --project "$P" --zone "$ZONE" \
      --cluster-version "$VER" --release-channel regular --machine-type "$MT" --num-nodes 1 \
      --workload-pool "$P.svc.id.goog"
  run gcloud iam service-accounts add-iam-policy-binding "$AGENT_SA" \
      --project "$P" --role roles/iam.workloadIdentityUser \
      --member "serviceAccount:$P.svc.id.goog[lab-agent/lab-agent]"
  ;;
hardened)
  # The manual reference: what a team restricts by hand (spec: what it leaves out, VPC Service
  # Controls above all). Own VPC with no open ingress, private nodes,
  # a node service account with logging and monitoring only, Workload Identity, Dataplane V2
  # (network policy enforced), Shielded nodes, metadata server in GKE mode, and the later
  # rungs' prerequisites already in place.
  run gcloud compute networks create lab-net --project "$P" --subnet-mode custom
  run gcloud compute networks subnets create lab-subnet --project "$P" --network lab-net \
      --region "$REGION" --range 10.10.0.0/20 --enable-private-ip-google-access --enable-flow-logs
  run gcloud dns policies create lab-dns-log-lab-net --project "$P" --networks lab-net --enable-logging \
      --description "lab evidence floor: DNS query logs"
  run gcloud compute routers create lab-router --project "$P" --network lab-net --region "$REGION"
  run gcloud compute routers nats create lab-nat --project "$P" --router lab-router \
      --region "$REGION" --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges
  run gcloud iam service-accounts create lab-node --project "$P" --display-name "lab GKE nodes" || true    # kept between sessions
  for role in roles/logging.logWriter roles/monitoring.metricWriter roles/monitoring.viewer \
              roles/stackdriver.resourceMetadata.writer roles/artifactregistry.reader; do
    run gcloud projects add-iam-policy-binding "$P" --condition None --role "$role" \
        --member "serviceAccount:lab-node@$P.iam.gserviceaccount.com"
  done
  # Private Google Access by name: *.googleapis.com resolves to private.googleapis.com
  # (199.36.153.8/30) inside lab-net, which is the only Google range the NetworkPolicy allows.
  run gcloud dns managed-zones create lab-googleapis --project "$P" --visibility private \
      --networks lab-net --dns-name googleapis.com. --description "Private Google Access"
  run gcloud dns record-sets create private.googleapis.com. --project "$P" --zone lab-googleapis \
      --type A --ttl 300 --rrdatas 199.36.153.8,199.36.153.9,199.36.153.10,199.36.153.11
  run gcloud dns record-sets create '*.googleapis.com.' --project "$P" --zone lab-googleapis \
      --type CNAME --ttl 300 --rrdatas private.googleapis.com.
  run gcloud container clusters create "$NAME" --project "$P" --zone "$ZONE" \
      --cluster-version "$VER" --release-channel regular --machine-type "$MT" --num-nodes 1 \
      --network lab-net --subnetwork lab-subnet --enable-ip-alias \
      --enable-private-nodes --master-ipv4-cidr 172.16.0.32/28 \
      --enable-master-authorized-networks --master-authorized-networks "${OPERATOR_CIDR:?set OPERATOR_CIDR to your workstation egress /32}" \
      --service-account "lab-node@$P.iam.gserviceaccount.com" \
      --workload-pool "$P.svc.id.goog" --workload-metadata GKE_METADATA \
      --enable-dataplane-v2 --enable-shielded-nodes --shielded-secure-boot --shielded-integrity-monitoring \
      --no-issue-client-certificate --no-enable-legacy-authorization
  # Workload Identity: the lab-agent Kubernetes service account may act as lab-agent. The
  # identity pool exists only once a cluster names it, so this follows the create (2026-11-21:
  # binding first failed with "Identity Pool does not exist").
  run gcloud iam service-accounts add-iam-policy-binding "$AGENT_SA" \
      --project "$P" --role roles/iam.workloadIdentityUser \
      --member "serviceAccount:$P.svc.id.goog[lab-agent/lab-agent]"
  ;;
vulnerable)
  # The deliberately exposed positive control: deliberately weak, so the reach probes have something to find.
  # Default network, default compute account with the full cloud-platform scope, legacy
  # metadata endpoints left reachable (GCE_METADATA, no Workload Identity), no network policy.
  evidence_floor default default
  run gcloud container clusters create "$NAME" --project "$P" --zone "$ZONE" \
      --cluster-version "$VER" --release-channel regular --machine-type "$MT" --num-nodes 1 \
      --scopes cloud-platform --workload-metadata GCE_METADATA
  ;;
prereq)
  # A runtime's own cluster: only what the runtime's install docs require, none
  # of the manual reference's controls. Substrate's binding list: Standard, 1.37 on the control
  # plane and nodes, not Extended, Dataplane V2, Workload Identity, c3-standard-4 or larger.
  # The runtime's own install step adds what else it documents (Agent Sandbox's gVisor pool;
  # Substrate's installer may create its own cluster, and then that's the path, not this one).
  evidence_floor default default
  run gcloud container clusters create "$NAME" --project "$P" --zone "$ZONE" \
      --cluster-version "$VER" --release-channel regular --machine-type "$MT" --num-nodes 1 \
      --enable-dataplane-v2 --workload-pool "$P.svc.id.goog"
  ;;
setup)
  # After a cluster is up: credentials for kubectl, then the profile's manifests.
  # usage: infra/cluster.sh setup --profile default|hardened|vulnerable [--apply]
  PR=${3:?}; [[ $APPLY == --profile ]] && APPLY=${4:-}
  run gcloud container clusters get-credentials "lab-$PR" --project "$P" --zone "$ZONE"
  run kubectl apply -f "$(dirname "$0")/k8s/base.yaml"
  [[ $PR == hardened ]] && render "$(dirname "$0")/k8s/hardened.yaml" | runp kubectl apply -f -
  [[ $PR == default ]] && render "$(dirname "$0")/k8s/default-wi.yaml" | runp kubectl apply -f -
  if [[ $PR == sandbox ]]; then
    # Agent Sandbox v1.0.6 the documented way: the release manifest, then the how-to's recommended
    # security policy, both vendored and pinned (infra/vendor/agent-sandbox-v1.0.6/, sha256 in its
    # SOURCE file); then the two templates and their warm pools.
    V="$(dirname "$0")/vendor/agent-sandbox-v1.0.6"
    [[ -f $V/sandbox-with-extensions.yaml ]] || { echo "vendor the v1.0.6 manifests first ($V)" >&2; exit 2; }
    render "$(dirname "$0")/k8s/default-wi.yaml" | runp kubectl apply -f -
    run kubectl apply --server-side -f "$V/sandbox-with-extensions.yaml"
    run kubectl -n agent-sandbox-system wait --for=condition=Available deploy --all --timeout=300s
    run kubectl apply -f "$V/secure-sandbox-policy.yaml"
    run kubectl apply -f "$V/secure-sandbox-binding.yaml"
    sed "s#LAB_AGENT_IMAGE#${LAB_AGENT_IMAGE:?source harness/lab_env.sh}#" "$(dirname "$0")/k8s/sandbox.yaml" | render | runp kubectl apply -f -
  fi
  if [[ $PR == substrate ]]; then
    # Agent Substrate v1.0.1 is installed by Google's installer, which creates the cluster
    # (infra/substrate/INSTALL.md; an operator drives its prompts). This is the lab's part after it:
    # the credential the gateway injects, the provider's grant, the worker pool, the atespace and
    # the agent's template, whose golden snapshot is built before any actor runs.
    S="$(dirname "$0")/substrate"
    kubectl create namespace lab-creds --dry-run=client -o yaml | runp kubectl apply -f -
    run "$GOOGLE_AUTH_PY" "$(dirname "$0")/../harness/token_secret.py" --once
    run kubectl apply -f "$S/namespace-policy.yaml"
    run kubectl -n ate-system rollout restart deployment/k8s-credential-provider
    run kubectl -n ate-system rollout status deployment/k8s-credential-provider --timeout=180s
    # atelet pulls actor images: the reader grant on the lab's repository (setup-gcp prints this
    # command for actor image repositories rather than running it).
    run gcloud artifacts repositories add-iam-policy-binding lab-images --project "$P" --location "$REGION" \
        --role roles/artifactregistry.reader \
        --member "principal://iam.googleapis.com/projects/$PN/locations/global/workloadIdentityPools/$P.svc.id.goog/subject/ns/ate-system/sa/atelet"
    run kubectl apply -f "$S/workerpool.yaml"
    run kubectl ate create atespace lab-agent
    sed "s#LAB_ACTOR_IMAGE#${LAB_ACTOR_IMAGE:?build agent/Dockerfile.actor and export its digest}#" "$S/actor-template.yaml" \
      | render | runp kubectl ate create actor-template -f -
    if [[ $APPLY == --apply ]]; then      # the golden build: status.goldenSnapshotStatus.goldenTag or errorMessage
      for i in $(seq 1 90); do
        y=$(kubectl ate get actor-template lab-agent -a lab-agent -o yaml 2>/dev/null || true)
        echo "$y" | grep -q "errorMessage:" && { echo "golden build failed:" >&2; echo "$y" >&2; exit 1; }
        echo "$y" | grep -q "snapshotTime:" && { echo "golden snapshot built"; break; }
        [[ $i == 90 ]] && { echo "golden build not done after 15 minutes" >&2; exit 1; }
        sleep 10
      done
    fi
  fi
  if [[ $PR == executor ]]; then
    # Agent Executor v0.4.0-rc.1 on Substrate v1.0.1 (Google's installer creates the cluster; the
    # mismatch with AX's documented v1.0.0 is disclosed). The lab's part: a gVisor
    # worker pool AX can use, the in-cluster forge, AX itself with the release's images pinned by
    # digest (google/ax v0.4.0-rc.1 release notes), the token Secret where AX grants the provider,
    # and the agent's Gateway.
    X="$(dirname "$0")/executor"
    # The forge is an ordinary Pod pulling from lab-images, and the installer's cluster runs its nodes
    # as its own service account (ate-gke-node), which has no read on the lab's repository.
    run gcloud artifacts repositories add-iam-policy-binding lab-images --project "$P" --location "$REGION" \
        --role roles/artifactregistry.reader --member "serviceAccount:ate-gke-node@$P.iam.gserviceaccount.com"
    run kubectl apply -f "$X/workerpool.yaml"
    sed "s#LAB_AGENT_IMAGE#${LAB_AGENT_IMAGE:?source harness/lab_env.sh}#" "$X/forge.yaml" | runp kubectl apply -f -
    run kubectl -n lab-forge wait --for=condition=Ready pod/forge --timeout=180s
    run "${AX_BIN:?the ax CLI built at v0.4.0-rc.1}" install \
        --server-image ghcr.io/google/ax/ax-server@sha256:95737151ccc54fdb8d99a8a8674f11777d7b08b7108a7b80820a1c502bb71223 \
        --default-task-image ghcr.io/google/ax/ax-task-runner@sha256:3a61e70d70cede25cec04c34b2902e2febac08bd78741207101d5c102f6e7a2c \
        --runner-volume-image ghcr.io/google/ax/ax-runner-volume@sha256:ac849f8357ac5b412d2d35d9076ef94b00557399d95c52c62353954e2101be07
    LAB_TOKEN_NAMESPACE=ax-system LAB_TOKEN_AX_LABEL=1 run "$GOOGLE_AUTH_PY" "$(dirname "$0")/../harness/token_secret.py" --once
    run "$AX_BIN" apply -f "$X/gateway.yaml"
    # atelet pulls the agent image from lab-images, as on the Substrate rung.
    run gcloud artifacts repositories add-iam-policy-binding lab-images --project "$P" --location "$REGION" \
        --role roles/artifactregistry.reader \
        --member "principal://iam.googleapis.com/projects/$PN/locations/global/workloadIdentityPools/$P.svc.id.goog/subject/ns/ate-system/sa/atelet"
  fi
  [[ $PR == hardened || $PR == prereq || $PR == sandbox || $PR == substrate || $PR == executor ]] && run kubectl apply -f "$(dirname "$0")/k8s/policy-logging.yaml"
  # The escape suite's neighbor: a canary Secret (fresh per setup) and a Pod serving it.
  CANARY="lab-canary-$(openssl rand -hex 8)"; echo "neighbor canary: $CANARY"
  # The Pod waits for its Secret (CreateContainerConfigError, retried by the kubelet) if it lands first.
  sed "s#LAB_AGENT_IMAGE#${LAB_AGENT_IMAGE:?source harness/lab_env.sh}#" "$(dirname "$0")/k8s/neighbor.yaml" | runp kubectl apply -f -
  run kubectl -n lab-neighbor create secret generic neighbor-canary --from-literal "value=$CANARY"
  ;;
sandbox)
  # The Agent Sandbox rung: the prerequisite-only cluster (as prereq, with
  # Workload Identity, since the agent's template uses it and that step is counted), plus the gVisor
  # node pool the Agent Sandbox how-to requires on Standard (--sandbox type=gvisor, cos_containerd).
  # C3 for both pools: Substrate's floor, and E2 is excluded there.
  evidence_floor default default
  run gcloud container clusters create "$NAME" --project "$P" --zone "$ZONE" \
      --cluster-version "$VER" --release-channel regular --machine-type "$MT" --num-nodes 1 \
      --enable-dataplane-v2 --workload-pool "$P.svc.id.goog"
  run gcloud container node-pools create gvisor --cluster "$NAME" --project "$P" --zone "$ZONE" \
      --node-version "$VER" --machine-type "$MT" --num-nodes 1 --image-type cos_containerd \
      --sandbox type=gvisor
  run gcloud iam service-accounts add-iam-policy-binding "$AGENT_SA" \
      --project "$P" --role roles/iam.workloadIdentityUser \
      --member "serviceAccount:$P.svc.id.goog[lab-agent/lab-agent]"
  ;;
listener)
  # The canary listener: an e2-micro in the cluster's own network, no service
  # account, running probes/listener.py from instance metadata. Reached at its internal address
  # (lateral movement) and its external one (egress to the internet). Ingress on 8080 only, from
  # the VPC's private ranges and the cluster's own egress addresses (node external IPs, or the
  # NAT's), so scanners don't fill its log.
  PR=${3:?}; [[ $APPLY == --profile ]] && APPLY=${4:-}
  if [[ $PR == hardened ]]; then NET=lab-net; SUBNET=(--subnet lab-subnet); else NET=default; SUBNET=(); fi
  run gcloud compute instances create lab-listener --project "$P" --zone "$ZONE" --machine-type e2-micro \
      --network "$NET" ${SUBNET[@]+"${SUBNET[@]}"} --tags lab-listener --no-service-account --no-scopes \
      --image-family debian-12 --image-project debian-cloud --shielded-secure-boot \
      --metadata-from-file "listener-py=$(dirname "$0")/../probes/listener.py" \
      --metadata startup-script='#!/bin/sh
curl -s -H "Metadata-Flavor: Google" http://169.254.169.254/computeMetadata/v1/instance/attributes/listener-py > /opt/listener.py
nohup python3 /opt/listener.py 8080 >/var/log/lab-listener.log 2>&1 &'
  if [[ $APPLY == --apply ]]; then
    EGRESS=$(gcloud compute instances list --project "$P" --filter "name~^gke-lab-" \
               --format "value(networkInterfaces[0].accessConfigs[0].natIP)" | { grep . || true; } | sed 's#$#/32#' | paste -sd, -)
    NATIP=$({ gcloud compute routers get-status lab-router --project "$P" --region "$REGION" \
               --format "value(result.natStatus[0].autoAllocatedNatIps)" 2>/dev/null || true; } | tr ';' '\n' | { grep . || true; } | sed 's#$#/32#' | paste -sd, -)
  else EGRESS="NODE_IPS/32"; NATIP=""; fi
  SRC=$(printf '%s' "10.0.0.0/8${EGRESS:+,$EGRESS}${NATIP:+,$NATIP}")
  run gcloud compute firewall-rules create lab-listener-in --project "$P" --network "$NET" \
      --direction INGRESS --allow tcp:8080 --target-tags lab-listener --source-ranges "$SRC"
  ;;
executor-down)
  # AX first (ax uninstall keeps its Postgres PVC and Secret; deleting ax-system removes them),
  # the forge and the worker pool, then Substrate's teardown for cluster lab-executor.
  run "${AX_BIN:?}" uninstall || true
  run kubectl delete namespace ax-system --ignore-not-found --wait=true --timeout=300s || true
  run kubectl delete namespace lab-forge --ignore-not-found --wait=true --timeout=180s || true
  run kubectl delete -f "$(dirname "$0")/executor/workerpool.yaml" --ignore-not-found --wait=true --timeout=300s || true
  run gcloud artifacts repositories remove-iam-policy-binding lab-images --project "$P" --location "$REGION" \
      --role roles/artifactregistry.reader --member "serviceAccount:ate-gke-node@$P.iam.gserviceaccount.com" || true
  SUBSTRATE_CLUSTER=lab-executor "$0" substrate-down ${APPLY:+--apply}
  ;;
substrate-down)
  # Substrate's own teardown, in Google's order (install page and substrate-gke README): the lab's
  # actors and template, then the control plane (ate-setup delete ate-system), then wait until the
  # Postgres disk's PV is gone ("GKE keeps persistent disks when it deletes a cluster"), then
  # cleanup-gcp, which deletes the cluster, buckets, IAM bindings and the image repository. The
  # lab's own down and the inventory assertion follow (infra/cluster.sh down).
  SG=${SUBSTRATE_GKE_DIR:?the pinned substrate-gke checkout}; SU=${SUBSTRATE_DIR:?the pinned substrate checkout}
  BUCKET=${SUBSTRATE_BUCKET:?the snapshot bucket named at install}
  # Google's benchmark suite, if it was set up (harness/google_bench.py): its workloads, runner
  # namespace and bindings, Prometheus, and the results bucket, before the control plane goes.
  if [[ -f "$(dirname "$0")/../runs/.google-bench-state.json" ]]; then
    "${LAB_PYTHON:-python3}" "$(dirname "$0")/../harness/google_bench.py" down ${APPLY:+--apply} || true
  fi
  # Actors first, then the template they reference (round 9, finding 15), then the atespace.
  for ns in lab-agent lcb; do for a in $(kubectl ate get actors -a $ns 2>/dev/null | awk 'NR>1{print $2}'); do run kubectl ate delete actor "$a" -a $ns || true; done; done
  if [[ -n $APPLY ]] && kubectl ate get actors -a lab-agent 2>/dev/null | awk 'NR>1' | grep -q .; then
    echo "actors remain in lab-agent after deletion; the inventory will show any residue" >&2; fi
  run kubectl ate delete actor-template lab-agent -a lab-agent || true
  run kubectl ate delete atespace lab-agent || true
  run gcloud artifacts repositories remove-iam-policy-binding lab-images --project "$P" --location "$REGION" \
      --role roles/artifactregistry.reader \
      --member "principal://iam.googleapis.com/projects/$PN/locations/global/workloadIdentityPools/$P.svc.id.goog/subject/ns/ate-system/sa/atelet" || true
  run kubectl delete -f "$(dirname "$0")/substrate/workerpool.yaml" --ignore-not-found --wait=true --timeout=300s || true
  # As the installer's exit summary prints it: project, cluster and location named, no dev env.
  ( cd "$SU" && PROJECT_ID="$P" CLUSTER_NAME="${SUBSTRATE_CLUSTER:-${LAB_CLUSTER:-lab-substrate}}" CLUSTER_LOCATION="$ZONE" KUBECTL_CONTEXT= NO_DEV_ENV=1 \
      run go run ./cmd/ate-setup delete ate-system )
  if [[ $APPLY == --apply ]]; then
    for i in $(seq 1 60); do kubectl get pv 2>/dev/null | grep -q "ate-system/data-postgres-0" || { echo "postgres PV released"; break; }
      [[ $i == 60 ]] && echo "postgres PV still present after 10 minutes; the inventory will show any disk left" >&2; sleep 10; done
  fi
  ( cd "$SG" && run ./tools/cleanup-gcp --project "$P" --cluster "${SUBSTRATE_CLUSTER:-${LAB_CLUSTER:-lab-substrate}}" --location "$ZONE" --bucket "$BUCKET" --yes )   # no repository was provisioned: pre-built images
  "$0" down ${APPLY:+--apply}
  ;;
down)
  run gcloud compute instances delete lab-listener --project "$P" --zone "$ZONE" --quiet || true
  run gcloud compute firewall-rules delete lab-listener-in --project "$P" --quiet || true
  run gcloud dns record-sets delete '*.googleapis.com.' --project "$P" --zone lab-googleapis --type CNAME || true
  run gcloud dns record-sets delete private.googleapis.com. --project "$P" --zone lab-googleapis --type A || true
  run gcloud dns managed-zones delete lab-googleapis --project "$P" --quiet || true
  for n in lab-default lab-hardened lab-vulnerable lab-prereq lab-sandbox; do
    run gcloud container clusters delete "$n" --project "$P" --zone "$ZONE" --quiet || true
  done
  run gcloud compute routers nats delete lab-nat --project "$P" --router lab-router --region "$REGION" --quiet || true
  run gcloud compute routers delete lab-router --project "$P" --region "$REGION" --quiet || true
  run gcloud compute networks subnets delete lab-subnet --project "$P" --region "$REGION" --quiet || true
  run gcloud dns policies update lab-dns-log-lab-net --project "$P" --networks "" || true
  run gcloud dns policies delete lab-dns-log-lab-net --project "$P" --quiet || true
  run gcloud dns policies update lab-dns-log-default --project "$P" --networks "" || true
  run gcloud dns policies delete lab-dns-log-default --project "$P" --quiet || true
  run gcloud compute networks delete lab-net --project "$P" --quiet || true
  "$0" inventory ${APPLY:+--apply}
  ;;
park)
  # Park the Substrate cluster between sessions; tear it down only when the lab is finished.
  # Parked means: Google's suite and its node pool gone, the lab's actors deleted
  # (the template and its golden snapshot stay), every node pool at zero, and a manifest of exactly
  # what remains, which the inventory then allows and nothing else. No instance may run.
  C=${SUBSTRATE_CLUSTER:-${LAB_CLUSTER:-lab-substrate}}; PF="$(dirname "$0")/../runs/.parked-$C.json"
  if [[ -f "$(dirname "$0")/../runs/.google-bench-state.json" ]]; then
    "${LAB_PYTHON:-python3}" "$(dirname "$0")/../harness/google_bench.py" down ${APPLY:+--apply}; fi
  for ns in lab-agent lcb; do for a in $(kubectl ate get actors -a $ns 2>/dev/null | awk 'NR>1{print $2}'); do run kubectl ate delete actor "$a" -a $ns || true; done; done
  pools=$(gcloud container node-pools list --cluster "$C" --zone "$ZONE" --project "$P" --format "value(name)")
  sizes="{}"
  for np in $pools; do
    n=$(kubectl get nodes -l "cloud.google.com/gke-nodepool=$np" --no-headers 2>/dev/null | wc -l | tr -d ' ')
    sizes=$(python3 -c "import json,sys;d=json.loads(sys.argv[1]);d[sys.argv[2]]=int(sys.argv[3]);print(json.dumps(d))" "$sizes" "$np" "$n")
  done
  echo "node pool sizes before parking: $sizes"
  if [[ $APPLY == --apply ]]; then printf '{"cluster": "%s", "sizes": %s}\n' "$C" "$sizes" > "$PF"; fi
  for np in $pools; do run gcloud container clusters resize "$C" --node-pool "$np" --num-nodes 0 --zone "$ZONE" --project "$P" --quiet; done
  if [[ $APPLY == --apply ]]; then
    # What a parked install leaves, recorded by exact name for the inventory.
    python3 - "$PF" "$P" <<'PY'
import json, subprocess, sys
pf, p = sys.argv[1], sys.argv[2]
d = json.load(open(pf))
def names(*cmd):
    return [l for l in subprocess.run(list(cmd), capture_output=True, text=True).stdout.split() if l]
d["allowed"] = {
    "clusters": names("gcloud", "container", "clusters", "list", "--project", p, "--format", "value(name)"),
    "disks": names("gcloud", "compute", "disks", "list", "--project", p, "--format", "value(name)"),
    "buckets": names("gcloud", "storage", "buckets", "list", "--project", p, "--format", "value(name)"),
    "firewalls": [f for f in names("gcloud", "compute", "firewall-rules", "list", "--project", p, "--format", "value(name)")
                  if f.startswith("gke-" + d["cluster"])],
    "service accounts": [s for s in names("gcloud", "iam", "service-accounts", "list", "--project", p, "--format", "value(email)")
                         if s.startswith("ate-")]}
json.dump(d, open(pf, "w"), indent=1)
print(json.dumps(d["allowed"], indent=1))
PY
  fi
  "$0" inventory ${APPLY:+--apply}
  ;;
unpark)
  C=${SUBSTRATE_CLUSTER:-${LAB_CLUSTER:-lab-substrate}}; PF="$(dirname "$0")/../runs/.parked-$C.json"
  [[ -f $PF ]] || { echo "no parked manifest at $PF" >&2; exit 2; }
  for np in $(python3 -c "import json;[print(k) for k in json.load(open('$PF'))['sizes']]"); do
    n=$(python3 -c "import json;print(json.load(open('$PF'))['sizes']['$np'])")
    [[ $n == 0 ]] && continue
    run gcloud container clusters resize "$C" --node-pool "$np" --num-nodes "$n" --zone "$ZONE" --project "$P" --quiet
  done
  for d in ate-api-server atenet-router atenet-egress ate-controller k8s-credential-provider; do
    run kubectl -n ate-system rollout status "deploy/$d" --timeout=900s; done
  run kubectl -n ate-system rollout status statefulset/postgres --timeout=900s
  [[ $APPLY == --apply ]] && rm -f "$PF"
  ;;
inventory)
  # After teardown, assert every billable/privileged resource class is empty except a declared
  # retained set (build review 6, finding 13): the run exits non-zero and names the residue if any
  # class has an unexpected member, instead of a human reading the lists. Retained, by exact name:
  #   lab-images (Artifact Registry), lab-node and the agent's (service accounts), the default
  #   network and its four default-allow-* firewall rules, flow logs on the default subnet. APIs,
  #   IAM bindings and the WI binding are free and not listed here.
  [[ $APPLY == --apply ]] || { echo "inventory asserts only with --apply"; exit 0; }
  RETAINED_INSTANCES=""; RETAINED_NETWORKS="default"
  RETAINED_FIREWALLS="default-allow-icmp default-allow-internal default-allow-rdp default-allow-ssh"
  RETAINED_BUCKETS=""; RETAINED_REPOS="lab-images"
  RETAINED_SAS="lab-node@$P.iam.gserviceaccount.com $AGENT_SA $PN-compute@developer.gserviceaccount.com"
  residue=0
  assert_empty() { local what="$1"; shift; local got; got=$("$@" 2>/dev/null | { grep -v "^$" || true; })
    if [[ -n $got ]]; then echo "RESIDUE $what:"; echo "$got" | sed 's/^/  /'; residue=1; fi; }
  assert_only() { local what="$1" allow="$2"; shift 2; local got; got=$("$@" 2>/dev/null)
    while IFS= read -r line; do [[ -z $line ]] && continue
      case " $allow " in *" $line "*) : ;; *) echo "UNEXPECTED $what: $line"; residue=1 ;; esac
    done <<< "$got"; }
  # A parked cluster (infra/cluster.sh park) may leave exactly what its manifest names, and no
  # instance: its node pools are at zero.
  PARKED=$(ls "$(dirname "$0")"/../runs/.parked-*.json 2>/dev/null | head -1 || true)   # none: not parked (ls fails; pipefail)
  pk() { [[ -n $PARKED ]] && python3 -c "import json,sys;print(' '.join(json.load(open(sys.argv[1]))['allowed'].get(sys.argv[2],[])))" "$PARKED" "$1"; }
  if [[ -n $PARKED ]]; then
    echo "parked: $(basename "$PARKED") (the cluster, its disks, buckets, firewall rules and installer service accounts allowed)"
    assert_only clusters "$(pk clusters)" gcloud container clusters list --project "$P" --format "value(name)"
    assert_only disks "$(pk disks)" gcloud compute disks list --project "$P" --format "value(name)"
    RETAINED_BUCKETS="$RETAINED_BUCKETS $(pk buckets)"
    RETAINED_FIREWALLS="$RETAINED_FIREWALLS $(pk firewalls)"
    RETAINED_SAS="$RETAINED_SAS $(pk 'service accounts')"
  else
    assert_empty clusters gcloud container clusters list --project "$P" --format "value(name)"
    assert_empty disks gcloud compute disks list --project "$P" --format "value(name)"
  fi
  assert_empty addresses gcloud compute addresses list --project "$P" --format "value(name)"
  assert_empty routers gcloud compute routers list --project "$P" --format "value(name)"
  assert_empty "dns zones" gcloud dns managed-zones list --project "$P" --format "value(name)"
  assert_empty "dns policies" gcloud dns policies list --project "$P" --format "value(name)"
  assert_only instances "$RETAINED_INSTANCES" gcloud compute instances list --project "$P" --format "value(name)"
  assert_only networks "$RETAINED_NETWORKS" gcloud compute networks list --project "$P" --format "value(name)"
  assert_only firewalls "$RETAINED_FIREWALLS" gcloud compute firewall-rules list --project "$P" --format "value(name)"
  assert_only buckets "$RETAINED_BUCKETS" gcloud storage buckets list --project "$P" --format "value(name)"
  assert_only repos "$RETAINED_REPOS" gcloud artifacts repositories list --project "$P" --location "$REGION" --format "value(name.basename())"
  assert_only "service accounts" "$RETAINED_SAS" gcloud iam service-accounts list --project "$P" --format "value(email)"
  if [[ $residue == 0 ]]; then echo "inventory clean: only the declared retained set remains"; else
    echo "inventory NOT clean: see RESIDUE/UNEXPECTED above" >&2; exit 1; fi
;;
*) echo "unknown profile $PROFILE" >&2; exit 2 ;;
esac
