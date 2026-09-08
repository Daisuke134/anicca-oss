"""Coconala kept its own copy of the refusals, and the two lists had drifted apart.

Measured 2026-09-07. The shared list and this lane's list overlapped but neither contained the
other. Four classes existed only here -- music, outreach, desktop operations, and
explicit_ai_prohibition, which Dais withdrew the same day -- and two that Lancers and CrowdWorks
enforce were missing: manual_marketplace_operation and original_illustration_or_modelling.

That gap is not academic. In the week before Coconala restricted the account, this lane applied
20-30 times a day to 出品代行, イラスト作成, イラストレッスン and 楽譜制作 -- work the other two
platforms already declined by rule. The lane could not refuse them because its own copy of the
rules had never been told about them.

The three genuinely general classes moved into marketplace-core, so all three platforms gained
them; this lane now reads the shared list rather than keeping a twelfth copy.

Run: python3 -m pytest skills/earn/gig/tests/test_planner_reads_the_shared_refusals.py
"""

import importlib
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SHARED = Path(__file__).resolve().parents[3] / "_shared" / "marketplace-core" / "scripts"


@pytest.fixture()
def planner():
    sys.path.insert(0, str(SCRIPTS))
    try:
        module = importlib.import_module("application_planner")
        yield importlib.reload(module)
    finally:
        sys.path.remove(str(SCRIPTS))


def _shared():
    spec = importlib.util.spec_from_file_location("work_fit_for_gig_test", SHARED / "work_fit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_lane_reads_the_shared_list_rather_than_its_own(planner):
    assert planner.HARD_PROHIBITION_CLASSES == _shared().HARD_PROHIBITION_CLASSES


def test_the_two_rules_this_lane_was_missing_are_now_enforced(planner):
    """出品代行 and original artwork were most of what it applied to before the restriction."""
    assert "manual_marketplace_operation" in planner.HARD_PROHIBITION_CLASSES
    assert "original_illustration_or_modelling" in planner.HARD_PROHIBITION_CLASSES


def test_the_classes_this_lane_contributed_survive_for_everyone(planner):
    """Reading the shared list must not lose what only this lane knew."""
    for name in ("music_or_audio_production", "outreach_or_account_operations",
                 "mandatory_desktop_or_browser_operations"):
        assert name in planner.HARD_PROHIBITION_CLASSES
        assert name in _shared().HARD_PROHIBITION_CLASSES


def test_the_ai_prohibition_is_gone_here_too(planner):
    """Dais 2026-09-07. It was withdrawn from the shared list; a second copy would have kept it."""
    assert "explicit_ai_prohibition" not in planner.HARD_PROHIBITION_CLASSES


def test_the_prompt_the_model_reads_carries_every_class(planner):
    prompt = planner.planner_prompt({"requests": []})
    for name in planner.HARD_PROHIBITION_CLASSES:
        assert name in prompt, name


def test_no_second_definition_is_left_in_the_file():
    source = (SCRIPTS / "application_planner.py").read_text(encoding="utf-8")
    assert 'HARD_PROHIBITION_CLASSES = _work_fit().HARD_PROHIBITION_CLASSES' in source
    assert '"video_or_animation": "video editing' not in source
