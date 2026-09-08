"""The recipe has to describe the code that exists, or it is worse than no recipe.

A handover document that names a module which was renamed, or a count that has since changed,
sends the next platform down a path that no longer exists. These assertions are deliberately
about facts the code can answer.

Run: python3 -m pytest skills/_shared/marketplace-core/tests/test_skill_matches_the_code.py
"""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "SKILL.md"
SCRIPTS = ROOT / "scripts"


def _work_fit():
    spec = importlib.util.spec_from_file_location("work_fit_for_skill_test", SCRIPTS / "work_fit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_module_the_recipe_names_exists():
    text = SKILL.read_text(encoding="utf-8")
    for name in re.findall(r"`([a-z_]+\.py)`", text):
        assert (SCRIPTS / name).is_file(), name


def test_the_refusal_count_it_quotes_is_the_real_one():
    text = SKILL.read_text(encoding="utf-8")
    count = len(_work_fit().HARD_PROHIBITION_CLASSES)
    assert f"({count})" in text, f"SKILL.md quotes a different count than {count}"


def test_the_functions_it_promises_are_callable():
    module = _work_fit()
    for name in ("HARD_PROHIBITION_CLASSES", "category_refusal", "judge", "discovery_terms"):
        assert hasattr(module, name), name


def test_the_catalogue_helpers_it_names_exist():
    spec = importlib.util.spec_from_file_location("listing_catalog_for_skill_test",
                                                  SCRIPTS / "listing_catalog.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for name in ("listing_terms", "search_terms"):
        assert hasattr(module, name), name


def test_it_has_the_frontmatter_a_skill_needs():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    head = text.split("---", 2)[1]
    assert "name: marketplace-core" in head
    assert "description:" in head


def test_it_names_the_four_functions_a_new_platform_writes():
    text = SKILL.read_text(encoding="utf-8")
    for signature in ("discover(", "fetch(", "submit(", "readback("):
        assert signature in text, signature
