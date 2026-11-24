# Rules for a coding agent running this harness

These are the rules the lab ran under. They cost little and they prevent the mistakes that cost the most.

They're rules, not controls. The harness builds a sandbox for the agent it tests, and then you, the agent
running the harness, work outside it: on the operator's workstation, with their project-owner identity and
whatever their user account can read. Nothing below is enforced by anything but you. That's inconsistent with
what the lab measures, and it's how the lab ran. If you're unsure whether something is allowed, stop and ask.

## Money

- **Name the cost and get a yes before anything that meters.** Creating a cluster or node pool, resizing one up,
  a model call, a benchmark run. Say what it is, roughly what it costs and for how long, and wait for the
  person to agree. A dry run is free: `infra/cluster.sh` prints its commands unless given `--apply`.
- **Never infer that something is torn down.** If you can't list it (expired credentials, a failed command),
  the state is unknown, not empty. Run `infra/cluster.sh inventory --apply` and read its output.
- **Park between sessions, don't leave it running.** `infra/cluster.sh park --apply` scales every node pool to
  zero and records exactly what remains; the inventory then allows that and nothing else. A parked
  Substrate install still costs its database disk and buckets each day; say so.
- **Cap model spend per batch.** `harness/pilot.py` takes a cap (estimated dollars, priced from token counts)
  and won't launch a run that could cross it. It isn't a billing limit: set a budget alert on the project too.
- **Run the containment probes before any `--dangerous` run,** and read them. That flag turns the agent's own
  permission prompts off. The harness refuses it until this install's probes pass
  (`harness/containment_gate.py`); never reach for `--dangerous-override` yourself. That's the operator's call,
  with a reason they give you.

## Credentials

- **No personal credential goes in any sandbox.** What the agent can read depends on the venue. On Agent
  Substrate it gets a placeholder and the egress gateway injects a short-lived token for the agent's service
  account on the way out; the token never enters the actor. On the local Docker venue (the sidecar's metadata
  shim) and on the plain GKE baselines (Workload Identity's metadata server) the agent can read that short-lived
  token itself and use it until it expires, within the hour; on the baselines that reach is what the probes
  measure. That account's one role, Vertex AI user, allows more than the model call the lab makes, and the
  harness's billing guard sees only the agy calls it launches, so run in a project with nothing else of value in
  it and a budget alert.
- **Check the cluster before writing a token.** `harness/token_secret.py` refuses any kubectl context but your
  lab cluster; delete its Secret (`--delete`) when a session ends.
- **Keep the lab's gcloud configuration separate** from the person's own (`CLOUDSDK_CONFIG`, set by
  `harness/lab_env.sh`). Don't change global gcloud, Application Default Credentials or kubeconfig defaults.
- **Never write a token to a file you commit**, a log, or a result. The harness redacts tokens before saving;
  keep it that way.

## Runs

- **New apparatus runs one venue at a time.** Running in parallel multiplies each defect before you've seen it
  once.
- **Read one real run before a batch.** A short check passing doesn't mean the long run works.
- **Don't kill a run mid-request without recording it.** A run lost to infrastructure is recorded as such and
  rerun, never scored as the agent's failure, and never silently dropped.
- **Every excluded run keeps its record and its reason.**
- **Disclose departures.** If you change a setting from what the run guide says, write it down beside the
  result.
