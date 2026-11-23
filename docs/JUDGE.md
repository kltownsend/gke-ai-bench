# The judge, the cases and the controller

The judge, the containerized subject, the workspace sanitizer, seeded defects and the triage
cases, in detail. Docker must be running. The run guide is `RUNBOOK.md` at the top of the repository.

## Configure (your own Google Cloud project)

    cp lab.env.example lab.env      # git-ignored; set LAB_PROJECT and the rest
    source harness/lab_env.sh       # every shell that runs the lab

`lab.env.example` documents every variable. `harness/lab_env.sh` derives the rest: the agent's
service account email, the Artifact Registry path, a lab-only gcloud config directory
(`~/.config/gcloud-$LAB_PROJECT`) and the kubeconfig inside it. Manifests under `infra/` carry
`${LAB_PROJECT}` and `${LAB_AGENT}` placeholders that `infra/cluster.sh` fills when it applies
them. The local judge and its tests need only Docker and a clinicbench checkout at
`CLINICBENCH_DIR`.

## How judging works

- **Subject** (`subject/runner.py`): the only code that imports the workspace. Runs in a fresh
  container per gate (no network, read-only root and workspace, unprivileged user, no
  capabilities). Answers requests with raw values; decides nothing. Image pinned by digest in
  `validator/subject.py`.
- **Judge** (`validator/validate.py` + each case's `probe.py`): never imports workspace code;
  holds probes, expected answers and hidden inputs; accepts only nonce-tagged replies.
- **Regression** (`validator/regression.py`): the same 483 probes to the reference tree and the
  workspace; any difference outside the case's area fails. The Clinic's self-test runs in the
  subject and is advisory.
- **Outcomes:** right, wrong, or pending (a passing fix that changes a frozen file waits for a
  person's approval).

## Build the baselines (rebuildable, not committed)

    PY=${LAB_PYTHON:-python3}
    $PY validator/build_baseline.py 7774111 head        # the frozen record: every finding and not-driver case
    $PY validator/build_seeded.py                        # seeded defects (needs head), the labeled easy tier
    $PY validator/build_baseline.py "8ad3cac^" t03 ; $PY validator/build_baseline.py 8ad3cac t03fix
    $PY validator/build_baseline.py "c4d0bdd^" t01 ; $PY validator/build_baseline.py c4d0bdd t01fix
    $PY validator/build_baseline.py "4899a76^" t02 ; $PY validator/build_baseline.py 4899a76 t02fix
    $PY validator/build_baseline.py "4e93910^" t05 ; $PY validator/build_baseline.py 4e93910 t05fix
    $PY validator/build_baseline.py "3f7f0d0^" t06
    $PY validator/build_baseline.py "3f7f0d0^" t06fix --overlay 3f7f0d0:clinicbench/scorer.py

NAME must be one plain lowercase segment; the builder refuses anything else (an empty name once
deleted every baseline). Run these one per line, not from a shell loop over a string.

Every baseline is sanitized on build (`validator/sanitize.py`); a build fails if the leak scan
finds anything. A seed is accepted only if its edit applies once, its probes fail on the seeded
tree and pass on the clean one (visible and hidden), and the leak scan stays clean.

## Validate a workspace

    $PY validator/validate.py --case cases/T03 --workspace /path/to/ws --answer answer.json

`answer.json`: `{"kind": "fix" | "finding" | "not_driver", "files": [...], "symbols": ["module.function"], "attribution": "..."}`

A frozen-file fix needs an appended entry in `docs/contract-changes.md` with a `## ` heading, a
`Reason:` line, and one `Changed: <path> <sha256>` line per frozen file changed.

## Test the judge

    $PY -m unittest tests.test_validator -v

20 tests: honest answers pass; the atexit forgery, key reads, symlinks, garbage or rewritten
contract entries, stale hashes, out-of-scope files, mismatched answer files, deleted events,
regressions and broken booking all fail.

## The forge (local Gitea)

    docker run -d --name lab-gitea -p 127.0.0.1:3300:3000 -v lab-gitea-data:/data \
      -e GITEA__database__DB_TYPE=sqlite3 -e GITEA__security__INSTALL_LOCK=true \
      -e GITEA__server__ROOT_URL=http://127.0.0.1:3300/ -e GITEA__service__DISABLE_REGISTRATION=true \
      -e GITEA__mailer__ENABLED=false gitea/gitea:28.3.0
    $PY forge/setup.py        # admin user, token (forge/.forge.json, git-ignored), repo, base branches

Pinned: gitea/gitea@sha256:8967bf9deaf3c5c4adae13519436dc1b20c646ac83fe37ae810c933a1045227d.
`controller/forge_record.py` puts the effects on Gitea (pull requests, `finding` issues,
notification comments). Run the harness against it with `LAB_FORGE=1`.

## Layout

- `cases/<ID>/case.json`: the ticket and baseline. `key.json`: the private answer key, never
  mounted into the subject. `probe.py`: `probes(which)` and `judge(which, got, base)`.
- `seeds/SNN.json` + `probe_sNN.py`: one planted bug each.
