"""The two-model arm's side-channel canary and a stub-model run of its session loop (no servers)."""

from __future__ import annotations

from clinicbench import truth as T
from clinicbench import twomodel as M
from clinicbench.adapters.dcitl_f import AlwaysUnclear, DcitlF
from clinicbench.records import all_settings, single_dimension_dials
from clinicbench.store import ToolAPI


def _reachable(root, depth=4):
    seen, out, frontier = set(), [], [root]
    for _ in range(depth):
        nxt = []
        for o in frontier:
            if id(o) in seen:
                continue
            seen.add(id(o))
            out.append(o)
            d = getattr(o, "__dict__", None)
            if isinstance(d, dict):
                nxt += list(d.values())
            elif isinstance(o, (list, tuple)):
                nxt += list(o)
            elif isinstance(o, dict):
                nxt += list(o.values())
        frontier = nxt
    return out


def test_facade_hides_the_disclosure_log():
    log = [{"id": "canary"}]
    api = ToolAPI(log)
    f = M.ToolFacade(api)
    assert not hasattr(f, "log")
    clin = DcitlF(f, AlwaysUnclear())
    reach = _reachable(clin, depth=6)
    assert not any(o is log or o is api for o in reach)
    assert f.turn == 0 and f.find_patient(member_id="nope")["ok"] is False


def test_twomodel_loop_with_stub_voices(monkeypatch):
    truth = T.build("covered_booking", "twomodel:0")
    dials = single_dimension_dials("payment", all_settings()[0])
    seen = []

    class EchoModel(AlwaysUnclear):
        def __init__(self, *a, **k):
            super().__init__()

    def echo(self, template, transcript):
        seen.append(template)
        return template, False

    class EchoHearing:
        def __init__(self, *a, **k):
            from clinicbench.hearing import DriverHearing
            self.h, self.readings, self.usage = DriverHearing(), [], {}

        def __call__(self, text):
            return self.h(text)

    monkeypatch.setattr(M.D, "Model", EchoModel)
    monkeypatch.setattr(M.Realizer, "__call__", echo)
    monkeypatch.setattr(M.PatientVoice, "__call__", echo)
    monkeypatch.setattr(M, "ModelHearing", EchoHearing)
    s = M.run_twomodel_session(truth, dials, "t", "http://a", "http://b", "http://b")
    assert s["end"]["reason"] != "infra_error", s["end"]
    loss = M.step_loss(s)
    # Echo voices and the parser as hearing: zero translation, so zero hearing loss.
    assert loss["translation_loss_turns"] == 0 and loss["hearing_error_turns"] == 0
    assert loss["clinician_verbatim"] == loss["clinician_lines"]
    assert loss["clinician_lines"] >= 1 and seen
