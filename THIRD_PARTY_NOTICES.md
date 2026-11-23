# Third-party notices

This repository is MIT-licensed (`LICENSE`), except as noted here.

- **`baselines/`**: source trees of clinicbench (https://github.com/kltownsend/clinicbench), the simulated
  clinic booking system the coding agent repairs, at the points in its history the cases need, sanitized so no
  file names a cause. clinicbench is MIT-licensed by the same author. Its own `LICENSE` and notices sit inside
  each tree.
- **Antigravity CLI (agy)**: Google's coding agent. Not distributed here; download it from Google and use it
  under Google's terms. The lab ran agy 1.5.13.
- **Agent Sandbox, Agent Substrate, Agent Executor**: Google's runtimes, installed from Google's own
  repositories and releases at the versions the run guide names (Agent Sandbox v1.0.6, Agent Substrate
  v1.0.1, Agent Executor v0.4.0-rc.1). Their code and manifests aren't copied here, with one exception:
- **`infra/executor/image/google-ax-harness`**: Google's Agent Executor harness for Antigravity, from
  github.com/agentexecutor/images (`agents/antigravity/ax-harness` at `f4ffb902`), unmodified, Copyright
  2026 Google LLC, under the Apache License 2.0 (header retained in the file). See `SOURCE.md` beside it.
- **Gemini on Vertex AI**: called under Google Cloud's terms on your own project. No model is distributed here.
- **Gitea**: the local forge for the pull-request effect, run from its public container image under its own
  license.
