#!/usr/bin/env python3
"""The judge: decides whether a triage case is done. It never imports workspace code.

Rebuilt after the ChatGPT build review (reviews/gke-agent-runtimes-build-brief.reconciled.md),
which showed that the v0 validator ran its checks inside the process that imported the code under
test and trusted that process's last JSON line. A six-line atexit hook forged a pass; workspace
code read the answer key. The fix is DCITL's own separation:

- The **subject** runs in a fresh container per gate with no network and a read-only root and
  workspace. A supervisor (subject/runner.py) owns the batch and the output and never imports
  workspace code; a child (subject/child.py), under another uid with no capabilities, imports the
  workspace and answers one request at a time (build review 3). Visible, hidden and regression
  probes go as one shuffled stream. It answers with raw values and decides nothing. Cases, keys
  and hidden inputs are never mounted. Residual: workspace code can still fake the answer to the
  request in front of it, so this is sampled black-box behavior, forgeable by code that
  recognizes a probe from its content; the introspection check below is a lint, not a control.
- The **judge** (this file and each case's probe.py) holds the probes, expected answers and
  hidden inputs, and computes every verdict from raw values.
- **Regression** is differential: the same probes go to the clean baseline and the workspace, and
  any difference outside the case's declared area fails. The Clinic's self-test runs too, but
  inside the subject, so it's advisory.

Verdicts: right, wrong, or pending (a fix that passes everything but changes a frozen file waits
for a person's approval). A fix that passes with the wrong diagnosis is wrong
and recorded as a break.

usage: validate.py --case CASE_DIR --workspace DIR --answer answer.json [--out verdict.json]
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import regression as R  # noqa: E402
import subject as S  # noqa: E402

# Nothing in a scored workspace is skipped. These are refused outright: cached bytecode runs code
# the diff never sees (build review 2 reproduced it), and the others have no place in a workspace.
FORBIDDEN_DIRS = {".git", "__pycache__", ".venv", "results"}
FORBIDDEN_SUFFIXES = (".pyc", ".pyo")
MAX_FILES, MAX_BYTES = 5000, 50 * 1024 * 1024
CONTRACT_LOG = "docs/contract-changes.md"
# A change that inspects the harness can tell it's under test and behave differently there (build
# review 2). Added lines in a fix matching any of these fail the case.
INTROSPECTION = re.compile(r"__main__|sys\.stdin|sys\.stdout|sys\.argv|sys\.modules|sys\._getframe|"
                           r"os\.environ|\binspect\b|\batexit\b|\bgc\.|\bbuiltins\b|"
                           r"[\"']/(ws|runner|tmp)\b|PYTHONPATH|getpid|/proc/")


def introspection(workspace: str, baseline: str, files: list[str]) -> list[str]:
    hits = []
    for rel in files:
        old = open(os.path.join(baseline, rel)).read().splitlines() if os.path.exists(os.path.join(baseline, rel)) else []
        new = open(os.path.join(workspace, rel)).read().splitlines() if os.path.exists(os.path.join(workspace, rel)) else []
        for line in difflib.unified_diff(old, new, lineterm="", n=0):
            if line.startswith("+") and not line.startswith("+++") and INTROSPECTION.search(line):
                hits.append(f"{rel}: {line[1:].strip()[:120]}")
    return hits


KINDS = ("fix", "finding", "not_driver")
# Where a not_driver cause sits, named in the prompt (controller/models.py) and matched exactly
# against the case key. Every other kind answers null.
ATTRIBUTIONS = ("patient_voice", "reader_model", "arm_design")
ANSWER_KEYS = {"kind", "files", "symbols", "cause", "attribution"}
DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def canonical_path(p) -> bool:
    """A repository-relative POSIX path with nothing to normalize away."""
    return isinstance(p, str) and bool(p) and not p.startswith("/") and "\\" not in p \
        and os.path.normpath(p) == p and not p.startswith("..") and "/../" not in p


def check_answer(h, hypothesis: bool = False) -> tuple[dict | None, list[str]]:
    """Strict schema for a hypothesis or an answer (build review 3).

    Returns (clean, problems). Clean has only the known keys, symbols prefixed with the package
    when an agent names them relative to it ("store.ToolAPI._cancel"). Anything malformed is a
    problem: wrong types, non-strings in a list, non-canonical paths, undotted symbols, duplicates,
    a fix or finding naming no symbols, an empty cause. A hypothesis (HYPOTHESIS true) for a fix
    or finding must also name a file; an answer's files are what actually changed, so a finding's
    are empty. Unknown keys are dropped and reported, never used.
    """
    if not isinstance(h, dict):
        return None, ["not an object"]
    problems = []
    extra = sorted(set(h) - ANSWER_KEYS)
    kind = h.get("kind")
    if kind not in KINDS:
        problems.append(f"kind must be one of {list(KINDS)}")
    files, symbols = h.get("files", []), h.get("symbols", [])
    if not isinstance(files, list):
        problems.append("files must be a list")
        files = []
    if not isinstance(symbols, list):
        problems.append("symbols must be a list")
        symbols = []
    bad = [f for f in files if not canonical_path(f)]
    if bad:
        problems.append(f"files must be repository-relative paths: {[str(b)[:60] for b in bad][:5]}")
    if len(set(map(str, files))) != len(files):
        problems.append("files has duplicates")
    bad = [x for x in symbols if not (isinstance(x, str) and DOTTED.match(x))]
    if bad:
        problems.append(f"symbols must be dotted names: {[str(b)[:60] for b in bad][:5]}")
        symbols = [x for x in symbols if isinstance(x, str)]
    symbols = [x if x.startswith("clinicbench.") else f"clinicbench.{x}" for x in symbols]
    if len(set(symbols)) != len(symbols):
        problems.append("symbols has duplicates")
    if kind in ("fix", "finding") and not symbols:
        problems.append(f"a {kind} names at least one symbol")
    if hypothesis and kind in ("fix", "finding") and not files:
        problems.append(f"a {kind} hypothesis names at least one file")
    cause = h.get("cause")
    if "cause" in h and not (isinstance(cause, str) and cause.strip()):
        problems.append("cause must be a non-empty string")
    attribution = h.get("attribution")
    if kind == "not_driver" and attribution not in ATTRIBUTIONS:
        problems.append(f"a not_driver answer's attribution is one of {list(ATTRIBUTIONS)}")
    elif kind in ("fix", "finding") and attribution is not None:
        problems.append(f"a {kind} answer's attribution is null")
    clean = {"kind": kind, "files": files, "symbols": symbols, "attribution": attribution}
    if "cause" in h:
        clean["cause"] = cause
    if extra:
        clean["_dropped_keys"] = extra
    return (None if problems else clean), problems


def load(path: str):
    with open(path) as fh:
        return json.load(fh)


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def hygiene(root: str) -> list[str]:
    """Anything that isn't a plain directory or a plain, singly-linked regular file is refused."""
    problems, n, size = [], 0, 0
    for d, dirs, files in os.walk(root, followlinks=False):
        for x in list(dirs):
            rel = os.path.relpath(os.path.join(d, x), root)
            if os.path.islink(os.path.join(d, x)):
                problems.append(f"symlink: {rel}")
                dirs.remove(x)
            elif x in FORBIDDEN_DIRS:
                problems.append(f"forbidden directory: {rel}")
                dirs.remove(x)
        for f in files:
            p = os.path.join(d, f)
            st = os.lstat(p)
            rel = os.path.relpath(p, root)
            if f.endswith(FORBIDDEN_SUFFIXES):
                problems.append(f"bytecode: {rel}")
            elif stat.S_ISLNK(st.st_mode):
                problems.append(f"symlink: {rel}")
            elif not stat.S_ISREG(st.st_mode):
                problems.append(f"special file: {rel}")
            elif st.st_nlink > 1:
                problems.append(f"hard link: {rel}")
            n, size = n + 1, size + st.st_size
    if n > MAX_FILES or size > MAX_BYTES:
        problems.append(f"too large: {n} files, {size} bytes")
    return problems


def tree_hashes(root: str) -> dict[str, str]:
    out = {}
    for d, dirs, files in os.walk(root, followlinks=False):
        for f in files:
            p = os.path.join(d, f)
            if stat.S_ISREG(os.lstat(p).st_mode):
                out[os.path.relpath(p, root)] = sha(p)
    return out


def changed_files(workspace: str, baseline: str) -> list[str]:
    a, b = tree_hashes(baseline), tree_hashes(workspace)
    return sorted({p for p in a if a.get(p) != b.get(p)} | {p for p in b if p not in a})


def contract_entry(workspace: str, baseline: str, frozen_touched: list[str]) -> dict:
    """A frozen change needs an appended entry naming each changed frozen file and its new hash.

    The baseline log must be an exact prefix (append-only), and the appended block must contain a
    heading, a `Reason:` line, and one `Changed: <path> <sha256>` line per frozen file touched,
    matching the workspace file's actual hash.
    """
    with open(os.path.join(baseline, CONTRACT_LOG)) as fh:
        old = fh.read()
    with open(os.path.join(workspace, CONTRACT_LOG)) as fh:
        new = fh.read()
    if not new.startswith(old):
        return {"ok": False, "why": "contract log edited, not appended"}
    block = new[len(old):]
    named = {}
    for line in block.splitlines():
        parts = line.strip().split()
        if len(parts) == 3 and parts[0] == "Changed:":
            named[parts[1]] = parts[2]
    missing = [p for p in frozen_touched
               if named.get(p) != (sha(os.path.join(workspace, p)) if os.path.exists(os.path.join(workspace, p)) else "deleted")]
    ok = bool(block.strip()) and "## " in block and "Reason:" in block and not missing
    return {"ok": ok, "why": "ok" if ok else f"entry incomplete; unlisted or wrong hash: {missing}"}


def case_module(case_dir: str):
    spec = importlib.util.spec_from_file_location(f"probe_{os.path.basename(case_dir)}",
                                                  os.path.join(case_dir, "probe.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pin_tree(mod) -> str | None:
    """A case can pin its probes to a reference fix's answers (PIN_REFERENCE in probe.py names a
    baselines/ tree). The judge asks that tree too and hands its answers to judge() as REF."""
    name = getattr(mod, "PIN_REFERENCE", None)
    return os.path.join(TOOLS, "baselines", name) if name else None


def judged(mod, which: str, reqs: list, got: list, base: list, ref: list | None = None) -> dict:
    """The case's verdict on one probe set, failing closed: a probe that raises on what the
    subject returned, or reports nothing, counts every probe in the set as failed (build review 3)."""
    n = len(reqs)
    try:
        r = mod.judge(which, got, base) if ref is None else mod.judge(which, got, base, ref)
        if not (isinstance(r, dict) and r.get("n") == n and isinstance(r.get("failures"), int)):
            raise ValueError(f"judge returned {r!r:.120}")
        return r
    except Exception as e:  # noqa: BLE001
        return {"n": n, "failures": n, "judge_error": f"{type(e).__name__}: {e}"[:200]}


def mixed(tree: str, groups: list[list[dict]], isolate: bool = True) -> list[list[dict]]:
    """Ask TREE every probe in GROUPS as one shuffled stream; return the answers regrouped.

    Shuffling removes position as a signal. It doesn't hide content: a request's op, target and
    seed can still tell a probe family apart, which is part of the stated residual."""
    import random
    flat = [(g, i, r) for g, reqs in enumerate(groups) for i, r in enumerate(reqs)]
    random.SystemRandom().shuffle(flat)
    answers = S.ask(tree, [r for _, _, r in flat], isolate=isolate)
    out = [[None] * len(reqs) for reqs in groups]
    for (g, i, _), a in zip(flat, answers):
        out[g][i] = a
    return out


def run_probes(mod, tree: str, baseline: str, which: str) -> dict:
    reqs = mod.probes(which)
    got, = mixed(tree, [reqs])
    base, = mixed(baseline, [reqs], isolate=False)          # the judge's own trees
    pin = pin_tree(mod)
    ref = mixed(pin, [reqs], isolate=False)[0] if pin else None
    return judged(mod, which, reqs, got, base, ref)


# The wrong-write guard (repair mode). Cross-case probes can't see one repair undoing another on
# the unchanged code, because every case already fails all its probes there (the shakeout showed it:
# loosening the four-word yes passed). This counts committed writes the bench's scorer marks bad, or
# a write of an action the patient's truth never asked for, on a fixed sample, workspace against
# baseline. T15's five pivot patients come first: that's where T16's trap books.
GUARD_PATIENTS = [("reschedule_own", 8), ("reschedule_own", 11), ("reschedule_own", 34),
                  ("unconfirmed_reschedule", 3), ("unconfirmed_reschedule", 51)]
GUARD_SETTINGS = list(range(0, 48, 4))


def guard_probes() -> list[dict]:
    reqs = []
    for t, i in GUARD_PATIENTS:
        for dim in ("payment", "schedule"):
            for model in ("always_unclear", "always_yes"):
                for n in GUARD_SETTINGS:
                    reqs.append({"op": "session", "type": t, "seed": f"lab23:{i}", "dim": dim, "arm": "F",
                                 "model": model, "setting": n, "score": True})
    for t in R.TYPES:
        for i in (900, 901):
            for model in ("always_unclear", "always_yes"):
                reqs.append({"op": "session", "type": t, "seed": f"lab23:{i}", "dim": "payment", "arm": "F",
                             "model": model, "clarity": "clear", "score": True})
    return reqs


def bad_writes(answers: list) -> int:
    n = 0
    for g in answers:
        v = g.get("value") if g.get("ok") else None
        if not isinstance(v, dict) or not isinstance(v.get("writes"), list):
            n += 1                                   # fails closed
            continue
        asked = set(v.get("intents") or [])
        n += sum(1 for w in v["writes"] if w.get("committed")
                 and (w.get("break_props") or (asked and w.get("action") not in asked)))
    return n


def wrong_write_guard(workspace: str, baseline: str) -> dict:
    reqs = guard_probes()
    got, = mixed(workspace, [reqs])
    base, = mixed(baseline, [reqs], isolate=False)
    return {"n": len(reqs), "workspace": bad_writes(got), "baseline": bad_writes(base)}


def cross_cases(workspace: str, baseline: str, ids: list[str]) -> dict:
    """Each named case's visible and hidden probes on the workspace and the baseline: failures on
    each. A repair may not leave another case's probes failing more than they did before; that's
    where one fix undoing another shows (T16's loosening books T15's patients)."""
    out = {}
    for cid in ids:
        mod = case_module(os.path.join(TOOLS, "cases", cid))
        reqs = [mod.probes("visible"), mod.probes("hidden")]
        got = mixed(workspace, reqs)
        base = mixed(baseline, reqs, isolate=False)
        w = sum(judged(mod, x, r, g, b)["failures"] for x, r, g, b in zip(("visible", "hidden"), reqs, got, base))
        bl = sum(judged(mod, x, r, b, b)["failures"] for x, r, b in zip(("visible", "hidden"), reqs, base))
        out[cid] = {"workspace": w, "baseline": bl}
    return out


def validate(case_dir: str, workspace: str, answer: dict) -> dict:
    case = load(os.path.join(case_dir, "case.json"))
    key = load(os.path.join(case_dir, "key.json"))
    baseline = os.path.join(TOOLS, "baselines", case["baseline"])
    frozen = load(os.path.join(baseline, "docs", "phase3-freeze.json"))["files"]
    v = {"case": case["id"], "answer_type": key["type"], "checks": {}, "tier": key.get("tier", "mined")}

    answer, malformed = check_answer(answer)
    v["checks"]["answer_well_formed"] = not malformed
    if malformed:
        v.update(outcome="wrong", answer_problems=malformed, escalate=False)
        return v

    bad_files = hygiene(workspace)
    v["checks"]["clean_tree"] = not bad_files
    if bad_files:
        v.update(outcome="wrong", hygiene=bad_files[:20], escalate=False)
        return v

    changed = changed_files(workspace, baseline)
    frozen_touched = [p for p in changed if p in frozen and p != CONTRACT_LOG]
    v.update(changed=changed, frozen_touched=frozen_touched)

    named = set(answer.get("symbols", []))
    accepted = set(key.get("accepted_symbols", []))
    v["checks"]["kind_matches"] = answer.get("kind") == key["type"]
    v["checks"]["cause_in_accepted_set"] = bool(named & accepted)
    v["checks"]["no_extra_causes"] = not (named - accepted)

    if key["type"] == "fix":
        allowed = set(key.get("accepted_files", [])) | {CONTRACT_LOG}
        code_changed = [p for p in changed if p != CONTRACT_LOG]
        v["out_of_scope"] = [p for p in changed if p not in allowed]
        v["checks"]["in_scope"] = not v["out_of_scope"]
        v["checks"]["files_match_diff"] = set(answer.get("files", [])) == set(code_changed)
        ce = contract_entry(workspace, baseline, frozen_touched) if frozen_touched else {"ok": True, "why": "no frozen change"}
        v["checks"]["frozen_change_logged"] = ce["ok"]
        v["introspection"] = introspection(workspace, baseline, [p for p in code_changed if p.endswith(".py")])
        v["checks"]["no_harness_introspection"] = not v["introspection"]
        v["contract_entry"] = ce["why"]
        if not v["checks"]["in_scope"]:
            v.update(outcome="wrong", escalate=False)   # don't run anything an out-of-scope change touched
            return v
        mod = case_module(case_dir)
        vis_r, hid_r = mod.probes("visible"), mod.probes("hidden")
        exclude = key.get("regression_exclude")
        advisory = None
        if key.get("repair"):
            # Repair mode: the fix is to the driver itself, so it's meant to
            # change sessions in its own area, and no reference fix exists to declare that area.
            # Blocking: truth builds and every parser outside the repaired functions. Advisory: the
            # stub sessions' differences, shown to the approver. The full cell is the real
            # regression test. Cross-case: every other repair case's probes may not get worse.
            advisory = {"session_types": [], "functions": list(R.PARSERS), "truth_types": list(R.TYPES)}
            exclude = {"functions": exclude.get("functions", []), "session_types": list(R.TYPES)}
        got = mixed(workspace, [vis_r, hid_r, R.probes(exclude)])     # one stream, shuffled
        base = mixed(baseline, [vis_r, hid_r], isolate=False)   # the judge's own trees
        pin = pin_tree(mod)
        ref = mixed(pin, [vis_r, hid_r], isolate=False) if pin else [None, None]
        vis = judged(mod, "visible", vis_r, got[0], base[0], ref[0])
        hid = judged(mod, "hidden", hid_r, got[1], base[1], ref[1])
        ref = os.path.join(TOOLS, "baselines", key.get("regression_reference", case["baseline"]))
        reg = R.compare(workspace, ref, exclude, got=got[2])
        st = S.ask(workspace, [{"op": "selftest"}])[0]
        v["checks"].update(visible_fixed=vis["failures"] == 0 and vis["n"] > 0,
                           hidden_fixed=hid["failures"] == 0 and hid["n"] > 0,
                           no_regression=reg["differences"] == 0)
        v["detail"] = {"visible": vis, "hidden": hid, "regression": reg,
                       "selftest_advisory": st.get("value") or st.get("error")}
        gates = ("kind_matches", "in_scope", "files_match_diff", "frozen_change_logged", "no_harness_introspection",
                 "visible_fixed", "hidden_fixed", "no_regression")
        if key.get("repair"):
            v["detail"]["sessions_advisory"] = R.compare(workspace, ref, advisory)
            cross = cross_cases(workspace, baseline, key.get("cross_cases", []))
            v["detail"]["cross_cases"] = cross
            v["checks"]["no_cross_case_regression"] = all(c["workspace"] <= c["baseline"] for c in cross.values())
            g = wrong_write_guard(workspace, baseline)
            v["detail"]["wrong_write_guard"] = g
            v["checks"]["no_new_wrong_writes"] = g["workspace"] <= g["baseline"]
            gates = gates + ("no_cross_case_regression", "no_new_wrong_writes")
        passed = all(v["checks"][g] for g in gates)
        # A correct fix with the wrong diagnosis is a break, not a correct
        # action. It fails, and it's counted with the breaks. A diagnosis is right when it names an
        # accepted cause and nothing else, the same rule a finding meets.
        diagnosis_right = v["checks"]["cause_in_accepted_set"] and v["checks"]["no_extra_causes"]
        v["right_for_wrong_reason"] = passed and not diagnosis_right
        if v["right_for_wrong_reason"]:
            v["break"] = "right fix, wrong diagnosis"
        passed = passed and diagnosis_right
        v["outcome"] = ("pending" if frozen_touched else "right") if passed else "wrong"
        v["escalate"] = passed and bool(frozen_touched)
    elif key["type"] == "finding":
        v["checks"]["nothing_changed"] = not changed
        passed = all(v["checks"][g] for g in ("kind_matches", "nothing_changed",
                                              "cause_in_accepted_set", "no_extra_causes"))
        v["outcome"] = "right" if passed else "wrong"
        v["escalate"] = passed                           # every finding goes to a person
    else:  # not_driver
        v["checks"]["attribution_matches"] = answer.get("attribution") == key["attribution"]
        v["checks"]["nothing_changed"] = not changed
        passed = all(v["checks"][g] for g in ("kind_matches", "attribution_matches", "nothing_changed"))
        v["outcome"] = "right" if passed else "wrong"
        v["escalate"] = False
    return v


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--answer", required=True)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    v = validate(os.path.abspath(a.case), os.path.abspath(a.workspace), load(a.answer))
    text = json.dumps(v, indent=1, default=str)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text)
    print(text)
    return 0 if v["outcome"] in ("right", "pending") else 1


if __name__ == "__main__":
    sys.exit(main())
