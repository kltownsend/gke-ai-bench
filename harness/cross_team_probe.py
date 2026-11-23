#!/usr/bin/env python3
"""Can team two's actor reach team one's actors or snapshots? Item 1 of the remaining tests,
run in order.

The credential boundary was tested on 2026-11-22 (a policy naming team one's credential got "egress
denied"). This covers the other two surfaces the team-two page names, from inside a team-two actor
under its agent-phase policy (the model API only):

  Actors: a live team-one actor is created first. From team two's actor:
    1. HTTP GET /state to team one's worker Pod IP on the actor port (the address the router dials).
    2. The same through the worker Pod's DNS-less address on 443.
    3. HTTP GET to atenet-router's Service IP on each of its ports, with the router's target-actor
       header naming team one's actor (the header kubectl ate proxy sends).
    4. Team one's actor name as a hostname, the way a curious agent would try it.
  Snapshots: the snapshot bucket and its assets bucket, read anonymously and through the model-API
    route: (5) storage.googleapis.com JSON listing, (6) the bucket's XML endpoint by name,
    (7) a listing request sent to the model host with a storage path.

Each records status and the first bytes. Team one's actor answers /state with its pid and boot id,
so "reached" means those came back. The gateway's log is read afterwards for every attempt.
No model calls. Source harness/lab_env.sh; team two onboarded.
usage: cross_team_probe.py [--out DIR]
"""
import json
import os
import subprocess
import sys
import time

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TOOLS, "harness"))
sys.path.insert(0, os.path.join(TOOLS, "agent"))
sys.argv = [sys.argv[0], "probes", "--apply"] + sys.argv[1:]
import lcb_team as L  # noqa: E402
from substrate_venue import SubstrateVenue, ACTOR_PORT  # noqa: E402

PROBE = r"""
import json, socket, sys, urllib.request, urllib.error
c = json.loads(sys.argv[1])
def http(url, headers=None, timeout=8):
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"status": r.status, "body": r.read(600).decode("latin-1")}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": e.read(300).decode("latin-1")}
    except Exception as e:
        return {"error": type(e).__name__ + ": " + str(e)[:120]}
out = {}
out["1_worker_ip_actor_port"] = http(f"http://{c['worker_ip']}:{c['port']}/state")
out["2_worker_ip_443"] = http(f"https://{c['worker_ip']}:443/state")
for p in c["router_ports"]:
    out[f"3_router_{p}"] = http(f"http://{c['router_ip']}:{p}/state", {"ate-target-actor": c["actor_ref"],
                                                                     "Host": c["actor"]})
out["4_actor_name_as_host"] = http(f"http://{c['actor']}:{c['port']}/state")
for b in c["buckets"]:
    out[f"5_gcs_json_list_{b[-6:]}"] = http(f"https://storage.googleapis.com/storage/v1/b/{b}/o?maxResults=3")
    out[f"6_gcs_xml_{b[-6:]}"] = http(f"https://{b}.storage.googleapis.com/")
    out[f"7_via_model_host_{b[-6:]}"] = http(f"https://aiplatform.googleapis.com/storage/v1/b/{b}/o",
                                             {"Host": "storage.googleapis.com"})
print(json.dumps(out))
"""


def kubectl(*a):
    return subprocess.run(["kubectl", *a], capture_output=True, text=True).stdout.strip()


def main():
    out_dir = os.path.join(TOOLS, "runs", "remaining-20261122")
    os.makedirs(out_dir, exist_ok=True)
    rec = {"probe": "cross_team", "t": time.time()}
    with SubstrateVenue() as one:                      # team one: lab-agent atespace
        one.ensure_actor()
        one.start_proxy()
        st = json.loads(one.http("GET", "/state"))     # team one's own answer, for comparison
        rows = kubectl("ate", "get", "actors", "-a", one.atespace).splitlines()
        worker_pod = next((r.split()[4] for r in rows if one.actor in r), "")
        worker_ip = next((r.split()[5] for r in rows if one.actor in r), "")
        router_ip = kubectl("-n", "ate-system", "get", "svc", "atenet-router", "-o", "jsonpath={.spec.clusterIP}")
        router_ports = kubectl("-n", "ate-system", "get", "svc", "atenet-router",
                               "-o", "jsonpath={.spec.ports[*].port}").split()
        buckets = [os.environ.get("SUBSTRATE_BUCKET", ""), os.environ.get("SUBSTRATE_BUCKET", "") + "-assets"]
        cfg = {"worker_ip": worker_ip, "port": ACTOR_PORT, "router_ip": router_ip, "router_ports": router_ports,
               "actor": one.actor, "actor_ref": f"{one.atespace}/{one.actor}", "buckets": buckets}
        rec.update(team_one={"actor": one.actor, "worker_pod": worker_pod, "pid": st.get("pid"), "boot": st.get("boot")},
                   targets=cfg)
        with L.team_venue() as two:
            two.ensure_actor()
            rec["team_two_actor"] = two.actor
            rec["to_agent_policy_s"] = L.set_phase(two, "agent")
            res = json.loads(two.run_in_actor(["python3", "-I", "-c", PROBE, json.dumps(cfg)]) or "{}")
    marker = [str(st.get("pid")), str(st.get("boot"))]
    for k, r in res.items():
        body = r.get("body", "")
        r["team_one_state_returned"] = all(m in body for m in marker if m != "None")
        r["body"] = body[:160]
    rec["results"] = res
    rec["reached_team_one"] = any(r["team_one_state_returned"] for r in res.values())
    path = os.path.join(out_dir, f"cross-team-{time.strftime('%H%M%S')}.json")
    with open(path, "w") as fh:
        json.dump(rec, fh, indent=1)
    print(json.dumps(rec, indent=1))


if __name__ == "__main__":
    main()
