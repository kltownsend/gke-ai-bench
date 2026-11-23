#!/usr/bin/env bash
# usage: run.sh [extra -e args...]   Free check: agy against a fake front end, placeholder token, custom CA.
S=$(cd "$(dirname "$0")" && pwd); T=$(cd "$S/../.." && pwd)   # the repository root
IMG=python@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91; AGY=$T/agent/vendor/agy-1.5.13/x/antigravity
docker network create --internal lab-ph-net >/dev/null
docker run -d --rm --name lab-ph-fake --network lab-ph-net -e FAKE_PROJECT="${LAB_PROJECT:-example-project}" -v $S/srv:/srv:ro $IMG python -u /srv/fake.py >/dev/null; sleep 3
IP=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' lab-ph-fake)
ALL="aiplatform.googleapis.com oauth2.googleapis.com cloudcode-pa.googleapis.com daily-cloudcode-pa.googleapis.com generativelanguage.googleapis.com play.googleapis.com www.googleapis.com sts.googleapis.com cloudresourcemanager.googleapis.com serviceusage.googleapis.com us-central1-aiplatform.googleapis.com"
H=(); for h in ${HOSTS:-$ALL}; do H+=(--add-host "$h:$IP"); done
WS=$(mktemp -d); chmod 777 $WS
docker run --rm --name lab-ph-agy --network lab-ph-net "${H[@]}" --add-host metadata.google.internal:$IP \
  --read-only --tmpfs /tmp:size=256m --tmpfs /home/agent:size=512m,uid=10001,gid=10001 --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges \
  -v $AGY:/usr/local/bin/agy:ro -v $S/ca/ca.pem:/run/ate/egress-trust-bundle/ca.pem:ro -v $WS:/work -w /work \
  -e HOME=/home/agent ${META_ENV:--e GCE_METADATA_HOST=metadata.google.internal -e GCE_METADATA_IP=$IP} -e AGY_ADC_AUTH=true \
  -e GOOGLE_CLOUD_QUOTA_PROJECT="${LAB_PROJECT:-example-project}" -e GOOGLE_CLOUD_PROJECT="${LAB_PROJECT:-example-project}" -e GOOGLE_CLOUD_LOCATION=global \
  "$@" $IMG timeout 120 agy --model "${LAB_MODEL:-gemini-4-hydrogen-medium}" --output-format json -p "say hi" > $S/agy.out 2> $S/agy.err; echo "agy exit $?"
docker logs lab-ph-fake 2>&1 | grep EVT | sort | uniq -c | head -30
echo "--- stderr"; tail -c 1000 $S/agy.err; echo; echo "--- stdout"; tail -c 700 $S/agy.out; echo
docker rm -f lab-ph-fake >/dev/null 2>&1; docker network rm lab-ph-net >/dev/null; rm -rf $WS
