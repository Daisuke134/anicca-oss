"""A failure code shared by 41 raise sites identifies nothing.

Measured 2026-09-07 in production: over 120 wakes the Lancers lane found 45 projects it could
honestly take and got 13 of them onto the board. Nine were lost to `proposal_form_changed`, and
`~/.local/state/anicca/lancers/proposal-form-changes.jsonl` -- the file built precisely to name
the broken selector -- did not exist, because only three of the file's raise sites recorded
anything and the failures were coming from the other thirty-eight.

Run: python3 -m pytest apps/lancers-revenue/tests/test_form_failures_name_themselves.py
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TICK = ROOT / "skills" / "earn" / "lancers" / "scripts" / "application_tick.py"


def _module(tmp_path=None):
    spec = importlib.util.spec_from_file_location("lancers_tick_under_test", TICK)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_no_raise_site_is_anonymous_any_more():
    """The guard that would have caught this the first time: grep the file, not the behaviour."""
    source = TICK.read_text(encoding="utf-8")
    anonymous = re.findall(r'raise RuntimeError\("proposal_form_changed"\)', source)
    # The three survivors are inside _one/_visible_one, which record immediately above.
    assert len(anonymous) == 3, f"{len(anonymous)} raise sites still record nothing"
    assert source.count("raise _form_changed(") >= 38


def test_every_labelled_site_names_a_real_function_in_this_file():
    source = TICK.read_text(encoding="utf-8")
    functions = set(re.findall(r"^(?:async )?def ([A-Za-z_][A-Za-z0-9_]*)", source, re.M))
    for label in re.findall(r'raise _form_changed\("([^"]+)"', source):
        name, _, line = label.partition(":")
        assert name in functions, f"{label} names no function here"
        assert line.isdigit(), f"{label} carries no line"


def test_the_failure_is_written_where_a_human_will_look(tmp_path, monkeypatch):
    module = _module()
    evidence = tmp_path / "proposal-form-changes.jsonl"
    monkeypatch.setattr(module, "FORM_EVIDENCE", evidence)
    error = module._form_changed("_proposal_href:394", expected="/work/propose_start/123")
    assert isinstance(error, RuntimeError)
    assert str(error) == "proposal_form_changed"
    rows = [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows[0]["selector"] == "_proposal_href:394"
    assert rows[0]["why"] == "form_step_failed"
    assert rows[0]["found"]["expected"] == "/work/propose_start/123"


def test_recording_can_never_swallow_the_failure(tmp_path, monkeypatch):
    """Diagnostics must not be able to stop the lane reporting a broken form."""
    module = _module()
    monkeypatch.setattr(module, "FORM_EVIDENCE", tmp_path / "nope" / "x" / "y.jsonl")
    monkeypatch.setattr(module, "json", None)  # force the writer to throw
    error = module._form_changed("_submit:800")
    assert isinstance(error, RuntimeError)


def test_returning_the_error_keeps_every_call_site_a_raise():
    """If _form_changed raised instead of returning, a site could record without raising and the
    lane would carry on with a half-filled form."""
    source = TICK.read_text(encoding="utf-8")
    for line in source.split("\n"):
        if "_form_changed(" in line and "def _form_changed" not in line:
            assert line.strip().startswith("raise "), line.strip()
