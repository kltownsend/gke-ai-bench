# Team two (LoopControlBench) on the same Substrate install (the reuse question: a reusable platform
# or a single-use architecture?). The lab's actor image (agy, Python 3.13, the actor server and its
# metadata shim) plus exactly what the bench's own sandbox image adds (loopcontrolbench/tools/
# Dockerfile: git and build-essential), because the bench builds third-party packages from source.
#   docker buildx build --platform linux/amd64 --build-arg BASE=$LAB_ACTOR_IMAGE -t IMAGE --push .
ARG BASE
FROM ${BASE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
