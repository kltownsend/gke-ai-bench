# gke-agent-runtimes-lab

The harness behind a Layer2C Labs project that asked one question: do Google's GKE agent runtimes give a
coding agent a place to run that a platform team can trust, by default, and how much of that would the team
otherwise build by hand?

The results are on the project site, https://gke-ai-layer2c.web.app, with a "How it was tested" page for every
claim. This repository is the instrument behind those pages: the probes, the drivers, the configurations and
the cases, so you can run the same tests on your own Google Cloud project and check the numbers, or point them
at your own cluster.

## What's here

| Folder | What it is |
|---|---|
| `probes/` | Scripted probes that run inside the agent's sandbox: what credential the workload can get (`api_scope.py`), an escape suite (`escape_suite.py`), and the reach test's injected instruction and canary listener |
| `harness/` | The run drivers: resume and continuity (`resume_probe.py`), the first request after a long idle (`incluster_wake.py`), Google's own benchmark under lab settings (`google_bench.py`), the density control (`density_control.py`), per-team identities and the isolation matrix (`tenants.py`), cross-team reach (`cross_team_probe.py`), warm-pool reuse and long calls (`remaining_small.py`), the coding-agent runs (`canary_agy.py`, `pilot.py`, `pilot_report.py`) and team two (`lcb_team.py`) |
| `agent/` | How the agent runs on each venue: a local Docker sandbox, a GKE Pod, Agent Sandbox, Agent Substrate (`substrate_venue.py`, with the polled wait) and Agent Executor; the actor's HTTP adapter and metadata shim (`actor_server.py`); the token minter |
| `controller/` | The deterministic controller (Deterministic Code In The Loop): it runs the investigation in stages and calls the agent only for a hypothesis and a change |
| `validator/`, `subject/` | The sealed judge. The agent's code runs in its own locked container and never shares a process with the judge; hidden inputs and a sampled regression check decide whether a fix is real |
| `cases/`, `seeds/` | The 21 frozen Clinic cases with their answer keys: 16 mined from the published record, 5 seeded defects |
| `baselines/` | The Clinic source trees the cases start from, sanitized so no file names a cause |
| `infra/` | The plain GKE baselines and runtime clusters (`cluster.sh`), Kubernetes and Substrate configuration, team two's actor, the Executor image |
| `tests/` | Unit tests for the judge, the controller, the venues and the harness |

`RUNBOOK.md` is the run guide, written so a coding agent can follow it. `AGENTS.md` holds the rules an agent
running it should keep.

## What it measures, and what it doesn't

It measures facts about a configuration, checked by probes: does the real token stay out of the sandbox,
does a suspended agent come back with its memory, processes and files, how long the first request after a
long idle takes, what a second team can and can't reach. It also runs a real coding agent (Google's
Antigravity CLI on Gemini) on the Clinic's cases inside each venue, as a guard that the venue still lets the
agent do its work.

It doesn't produce verdicts. The project site's verdicts, and the method for reaching them, are the lab's
own and aren't part of this repository. Run the harness and build your own assessment.

It tested isolated code-change work on a lab copy of an open-source codebase, on one cluster, one Agent
Substrate release (v1.0.1), two worker nodes and gVisor only. It doesn't test production systems with
databases, deployments or concurrent users.

## The cases and their keys

All 21 cases ship with their answer keys. The 16 mined cases' answers were already public through the
Clinic's record. The 5 seeded defects (S01 to S06) were planted after the record froze, so no model had seen
their answers; publishing them here ends that. If you want answers no model has seen, plant your own:
`validator/build_seeded.py` builds a seeded tree and accepts a seed only if its edit applies once, its probes
fail on the seeded tree and pass on the clean one, and the leak scan stays clean.

## Second team

Team two in the lab was LoopControlBench, https://github.com/kltownsend/loopcontrolbench, run on the same
Substrate install with `harness/lcb_team.py`. Clone it beside this repository to run that part.

## Independence and disclosure

Google Cloud funded the lab this harness comes from and is a client of The CTO Advisor LLC. The CTO Advisor
kept editorial control of the method, the findings and the conclusions. Every venue tested is a Google
product, the agent is Google's, and so is its model. Much of the harness was written with Claude, an
Anthropic model; Anthropic isn't a client. The verdicts on the site come from deterministic code and the lab
owner's rulings, not from a model.

## License

MIT (`LICENSE`), except as noted in `THIRD_PARTY_NOTICES.md`. Google's runtimes, the Antigravity CLI and
Gemini aren't distributed here and are used under Google's terms.
