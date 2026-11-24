# Run records

The run records behind the project site's test pages, https://gke-ai-layer2c.web.app/tests/, sanitized for
publication. The rest of this repository is the instrument; this folder is what the lab's own runs produced,
so you can recompute the site's numbers from them and compare your own runs to the lab's.

The lab's verdicts, and the rulings behind them, aren't here. These are the measurements.

## What was changed

Every file here came from the lab's `runs/` directory. Each one was changed in these ways, and no others:

- The lab's Google Cloud project ID is `example-project`. The project number is `000000000000`.
- Service-account emails are their role: `<agent-service-account>` and the like.
- Snapshot bucket names keep their shape; the random suffix is `xxxxxxxx`.
- The cluster's DNS endpoint is `<cluster-endpoint>`. Certificate serials are `<serial>`.
- Public IP addresses are tokens, `public-ip-1` and so on: the same address gets the same token everywhere.
  Private ranges, GKE's Service range (34.118.224.0/20), private.googleapis.com and well-known resolvers stay as
  recorded, because the claims are about them.
- The operator's username and paths are `/home/operator`, `<lab-checkout>` and `<lab-src>`, and the owner's
  name is "the lab owner".
- `coding-environments/lcb-team-20261122/onboard.log` drops its token-minting command lines.

Three kinds of record are derived instead of copied, because the raw files carry what can't ship (request
paths that name other models and buckets, serials, envelopes):

- `*.timing.csv`: one row per restore and per checkpoint from the runtime's own atelet logs, with every
  duration it reports.
- `*.decisions.jsonl`: one line per request from the egress gateway's access log (time, actor, leg,
  destination, SNI, method, status or chain, flags, bytes, duration). No request paths.

Held entirely: the raw gateway and atelet logs, the token refresher logs, registry authorization failures, and
every run not behind a site claim. `MANIFEST-originals.sha256` lists a SHA-256 for every original file in the
lab's `runs/`, held ones included, so a later release of any of them can be checked against what the lab had on
the day. `MANIFEST-published.sha256` hashes each file here and names its source and treatment.

## Which page each folder backs

| Folder | Site page | What's in it |
|---|---|---|
| `restore-500ms/` | /tests/restore-500ms/ | the in-cluster wakes per sample; Google's runner (Locust aggregates, server telemetry); the restore timing CSVs; the resume histogram queries |
| `restore-1s/` | /tests/restore-1s/ | the tunnel run, per wake |
| `picks-up/` | /tests/picks-up/ | the per-cycle continuity checks (smoke, main); the approval-pause run |
| `gateway-keys/` | /tests/gateway-keys/ | the Substrate and Executor probe passes (p1, p2); team two's probe |
| `actor-identity/` | /tests/actor-identity/ | the gateway decision extracts: Substrate, Executor, the cross-team probe |
| `warm-pool/` | /tests/warm-pool/ | the five reuse rounds; Google's spawn test |
| `coding-environments/` | /tests/coding-environments/ | the 63 Substrate runs and the 63 local runs, per call; team two's tasks and onboarding |
| `density-10x/` | /tests/density-10x/ | the 8, 16, 32 and 64 session steps; the control with Substrate's workers away |
| `any-kubernetes/` | /tests/any-kubernetes/ | the local shakeout |
| `teams/` | /teams/ | the isolation matrix, the published-tag step, cross-team reach, private reach, deny-all, router admission, storage by IP |

`gateway-keys/` has only the passes the site counts. The superseded passes on those venues (Substrate's
`-pregap`, Executor's `-home-ws` and `-userns-bug`) aren't here; their hashes are in the originals manifest.

## Read these before the raw numbers

- `teams/remaining-20261122/tenants-205655.json` says `all_as_expected: false`. One same-team row failed on a
  precondition (the actor had been resumed by an earlier row), not on authorization, and its cross-team
  published-tag row ran against a tag that was never created, so its denial proves nothing. The valid
  published-tag test is `tenants-published-tag-205834.txt`: team one published the tag, team two was denied
  `can_use_tag`, and team one's own restore from it was allowed. The harness now suspends the actor before that
  step.
- `teams/lcb-team-20261122/private-reach-170231.json` case 4 says `"forwarded": true`. That's the ClientHello
  naming pypi.org sent to 10.0.0.1:443. A TLS reply came back, but the gateway's log for it, quoted in the lab
  notes, reads `dialed: 10.0.0.1:443, sni: pypi.org, upstream: <pypi.org's real address>:443`: the gateway
  routed by the permitted name to pypi.org and ignored the private destination. The raw log line wasn't kept
  as a run file. Nothing reached a private address.
- `teams/remaining-20261122/denyall-214857.json` says the raw TCP connection to 1.1.1.1:443 "connected". The
  probe opens a TCP connection and records that it opened. The gateway accepted the tunnel and refused it:
  code 403, 0 bytes up, 13 down (its "egress denied"), nothing forwarded. That reading is from the gateway log,
  quoted in the lab notes; the raw line wasn't kept.
- A positive control's `body_start` can read `"403\r\n{..."` with `http_status: 200`. The `403` there is a
  chunked-encoding size line, not an HTTP status.
- `teams/remaining-20261122/router-admission-*.json` and `storage-by-ip-210532.json` came from one-off
  commands, not committed scripts. Router admission: from an in-cluster Pod holding an identity on the router's
  ingress allowlist (Google's `benchmarking/benchmark-runner`), a CONNECT through atenet-router to a team-one
  actor and to a team-two actor; both answered. Storage by IP: from a team-two actor under its agent policy, a
  TLS hello naming storage.googleapis.com, sent to storage's address and to the model API's address, denied
  both times (`filter_chain_not_found`), with the model API's name to its own address as the control. The
  method is what the lab notes record; the exact commands weren't saved.

## Checking a file

```bash
cd records
shasum -a 256 -c <(awk '{print $1"  "$2}' MANIFEST-published.sha256)
```

A sanitized file's hash won't match its original's line in `MANIFEST-originals.sha256`; the two manifests
record different bytes on purpose. The originals manifest is a commitment, not a check you can run today.
