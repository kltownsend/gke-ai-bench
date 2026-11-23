"""The subject's child: the only process that imports workspace code. It decides nothing.

Started by subject/runner.py (the supervisor) under a different uid, with stdin, stdout and
stderr on /dev/null. It reads requests from a private pipe and writes one reply per request to
another. For a workspace under test the supervisor starts a fresh child for every request and
clears everything of its uid afterwards, so a child sees one request and nothing before or after
it. Content still shows: a request's op, target and seed can tell a probe family apart.

Each request carries a nonce the reply must echo. Workspace code shares this process and can read
the current nonce, so it can fake the reply to the request in front of it, which is the residual:
sampled black-box behavior, forgeable by code that recognizes a probe from its content. It can't
answer ahead, and it can't reach the supervisor's output: different uid, no capabilities.

Requests:
  {"op": "call",     "target": "pkg.mod.func", "args": [...], "kwargs": {...}}
  {"op": "get",      "target": "pkg.mod.NAME"}
  {"op": "scenario", "steps": [{"new": "pkg.mod.Class", "args": [...]} |
                               {"method": "name", "args": [...], "kwargs": {...}}]}
                     stateful steps on one object, results returned as a list
  {"op": "session",  "type": T, "seed": S, "dim": D, "stub": X}
  {"op": "session",  "type": T, "seed": S, "dim": D, "arm": F|E|F2, "model": always_yes|always_unclear
                     | "script": [clinician lines], "setting": N | "clarity": C, "score": bool,
                     "read_lines": bool}
                     the frozen driver with a stub reader, or a scripted clinician, with the bench's
                     hearing; "score" adds the scorer's writes, "read_lines" the driver's reading of
                     each patient line
  {"op": "replay",   "arm": F|E|F2, "model": always_yes|always_unclear, "lines": [str, ...]}
                     recorded patient lines into the frozen driver; replies, trace, model log
  {"op": "selftest"}                                                       advisory only
"""

import importlib
import json
import os
import sys


def resolve(dotted):
    parts = dotted.split(".")
    for i in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:i]))
        except ImportError:
            continue
        for p in parts[i:]:
            obj = getattr(obj, p)
        return obj
    raise ImportError(dotted)


def _err(e):
    return f"{type(e).__name__}: {e}"[:300]


def _arm(name):
    """Arms and stub readers are looked up by name only when asked: older trees predate some."""
    mod, cls = {"F": ("dcitl_f", "DcitlF"), "E": ("dcitl_f", "DcitlE"), "F2": ("dcitl_f2", "DcitlF2")}[name]
    return getattr(importlib.import_module(f"clinicbench.adapters.{mod}"), cls)


def _reader(name):
    cls = {"always_yes": "AlwaysYes", "always_unclear": "AlwaysUnclear"}[name]
    return getattr(importlib.import_module("clinicbench.adapters.dcitl_f"), cls)


class _Scripted:
    """The judge's scripted clinician: says the request's lines in order, then hands off. Lines
    are data from the request; no code is."""
    name = "scripted"

    def __init__(self, api, lines):
        self.api, self.lines = api, iter(lines)

    def turn(self, patient_text, act):
        line = next(self.lines, None)
        if line is not None:
            return line, None
        self.api.hand_off("probe done")
        return "A member of our staff will follow up with you directly.", None


def _driver_session(req, t):
    """A session through the frozen DCITL driver with a stub reader, or a scripted clinician, with
    the bench's hearing of the clinician's text: no model server. Arms, stubs and dial settings
    come from fixed tables here, never from code a request names.
    Dial: "setting" (an index into records.all_settings()) or "clarity" (EASY otherwise)."""
    from clinicbench.hearing import DriverHearing
    from clinicbench.records import CLARITY_LEVELS, EASY, all_settings, single_dimension_dials
    from clinicbench.session import run_session
    if "script" in req:
        lines = [str(x) for x in req["script"]]
        make, who = (lambda api, log: _Scripted(api, lines)), "script"
    else:
        arm, model = _arm(req["arm"]), _reader(req["model"])
        make, who = (lambda api, log: arm(api, model())), f"{req['arm']}:{req['model']}"
    if "setting" in req:
        setting = all_settings()[int(req["setting"])]
    else:
        clarity = req.get("clarity", "clear")
        if clarity not in CLARITY_LEVELS:
            raise ValueError(f"unknown clarity {clarity}")
        setting = dict(EASY, clarity=clarity)
    s = run_session(t, single_dimension_dials(req["dim"], setting), make,
                    f"drv:{req['type']}:{req['seed']}:{who}:{json.dumps(setting, sort_keys=True)}",
                    hearing=DriverHearing())
    out = {k: s[k] for k in ("transcript", "trace", "log", "end")}
    out["intents"] = [i.get("action") for i in t.get("intents", [])]
    if req.get("score"):                       # the workspace's own scorer, returned raw
        from clinicbench.scorer import score
        out["writes"] = [{"action": (w.get("component") or {}).get("action"),
                          "committed": w.get("committed"), "break_props": w.get("break_props")}
                         for w in score(s)["tier1"]["writes"]]
    if req.get("read_lines"):                  # each patient line as the frozen driver reads it
        from clinicbench.adapters.dcitl_port import parse_intents
        out["driver_reads"] = [parse_intents(x["text"]) for x in s["transcript"] if x["speaker"] == "patient"]
    return out


def _replay(req):
    """Recorded patient lines replayed into the frozen driver, one turn per line, stub reader, on a
    fresh store. Arm and stub come from fixed tables here."""
    from clinicbench.store import ToolAPI
    arm, model = _arm(req["arm"]), _reader(req["model"])
    api, m = ToolAPI([]), model()
    a = arm(api, m)
    replies = [a.turn(line)[0] for line in req["lines"]]
    return {"replies": replies, "trace": api.trace, "model_log": m.log}


def handle(req):
    if req["op"] == "replay":
        return _replay(req)
    op = req["op"]
    if op == "call":
        return resolve(req["target"])(*req.get("args", []), **req.get("kwargs", {}))
    if op == "get":
        return resolve(req["target"])
    if op == "scenario":
        obj, out = None, []
        for step in req["steps"]:
            try:
                if "new" in step:
                    obj = resolve(step["new"])(*step.get("args", []))
                    out.append({"ok": True, "value": None})
                else:
                    out.append({"ok": True, "value": getattr(obj, step["method"])(*step.get("args", []),
                                                                                 **step.get("kwargs", {}))})
            except Exception as e:  # noqa: BLE001
                out.append({"ok": False, "error": _err(e)})
        return out
    if op == "session":
        from clinicbench import truth as T
        from clinicbench.records import EASY, single_dimension_dials
        from clinicbench.session import run_session
        t = T.build(req["type"], req["seed"])
        if "arm" in req or "script" in req:
            return _driver_session(req, t)
        from clinicbench.stubs import STUBS
        s = run_session(t, single_dimension_dials(req["dim"], EASY), STUBS[req["stub"]],
                        f"reg:{req['type']}:{req['seed']}:{req['stub']}")
        return {k: s[k] for k in ("transcript", "trace", "log", "end")}
    if op == "selftest":
        from clinicbench.selftest import run_all
        ok, report = run_all()
        return {"ok": ok, "tail": report.splitlines()[-1]}
    raise ValueError(f"unknown op {op}")


def _serve(rfd, wfd):
    reader, writer = os.fdopen(rfd, "r"), os.fdopen(wfd, "w")
    sys.path.insert(0, "/ws")
    for line in reader:
        req = json.loads(line)
        nonce = req.pop("_n")
        try:
            res = {"ok": True, "value": handle(req)}
        except BaseException as e:  # noqa: BLE001 - SystemExit from workspace code is an observation too
            res = {"ok": False, "error": _err(e)}
        try:
            text = json.dumps(dict(res, _n=nonce), default=str, sort_keys=True)
        except Exception as e:  # noqa: BLE001
            text = json.dumps({"ok": False, "error": "unserializable: " + _err(e), "_n": nonce})
        writer.write(text + "\n")
        writer.flush()


if __name__ == "__main__":
    _serve(int(sys.argv[1]), int(sys.argv[2]))
