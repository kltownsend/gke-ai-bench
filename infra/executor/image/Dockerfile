# The agent image for the Agent Executor rung (AX v0.4.0-rc.1): Google's Antigravity agent image
# (github.com/agentexecutor/images, agents/antigravity/Dockerfile at f4ffb902), with two declared
# differences:
# - agy is the lab's verified 1.5.13 x64 build copied in, not installed at build time. Google's
#   installer pins 1.5.10 and refuses other versions; every other rung runs 1.5.13.
# - /usr/local/bin/ax-harness is a thin wrapper that starts the lab's placeholder metadata shim
#   on loopback, then runs Google's harness unchanged (google-ax-harness, sha256 in SOURCE.md).
#   AX tasks get no metadata server; agy reads its token from one (the Substrate rung's design).
# - /usr/local/bin/lab-hold keeps a finished Task Working until the controller has collected the
#   workspace (AX rc.1 refuses `ax ssh` on a Completed Task); the exit code stays the agent's.
# Like Google's image it has no USER. The AX example README says "The task runs as root"; the
# first run on 2026-11-22 showed uid 1000 (AX's runner drops privileges). Every other rung runs
# the agent as uid 10001; disclosed.
#
#   docker buildx build --platform linux/amd64 -f infra/executor/image/Dockerfile \
#       --build-context agy=agent/vendor/agy-1.5.13-x64/x --build-context lab=agent \
#       -t IMAGE --push infra/executor/image
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git jq openssh-client procps python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=agy antigravity /usr/local/bin/agy
COPY google-ax-harness /usr/local/share/ax/google-ax-harness
COPY ax-harness /usr/local/bin/ax-harness
COPY lab-hold /usr/local/bin/lab-hold
COPY --from=lab actor_server.py pod_sandbox.py /opt/lab/
COPY lab_shim.py /opt/lab/
RUN chmod 0555 /usr/local/bin/agy /usr/local/bin/ax-harness /usr/local/bin/lab-hold /usr/local/share/ax/google-ax-harness \
    && chmod 0444 /opt/lab/*.py && agy --version
LABEL io.agentexecutor.agy.version="1.5.13"
ENV AGY_CLI_DISABLE_AUTO_UPDATE=true AGY_VERSION=1.5.13 PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/workspace/.ax/bin"
