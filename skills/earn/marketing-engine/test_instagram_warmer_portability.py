import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent
WARM = ROOT / "instagram" / "warm.py"
ENSURE = ROOT / "instagram" / "ensure_warmup_browser.py"


def load_warm():
    spec = importlib.util.spec_from_file_location("repo_instagram_warm", WARM)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repo_owned_warmer_keeps_day_one_passive_and_refuses_main_context():
    warm = load_warm()
    assert warm.engagement_plan(1, {"follow": 1})["targets"]["follow"] == 0
    try:
        warm.require_isolated_port(9222)
    except ValueError as exc:
        assert "main context" in str(exc)
    else:
        raise AssertionError("port 9222 must be refused")


def test_warmer_sources_are_repository_relative():
    text = WARM.read_text() + ENSURE.read_text() + (ROOT / "warmer.py").read_text()
    for external in (".claude/skills", ".agents/skills", ".openclaw/skills", "/opt/homebrew/bin/python3"):
        assert external not in text
