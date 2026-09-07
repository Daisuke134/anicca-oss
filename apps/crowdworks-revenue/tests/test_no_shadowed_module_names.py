"""A local binding that shadows a module-level import takes the whole lane down.

Measured 2026-09-07, and it was mine: `reason, quote = verdict` inside `_candidate` made `quote`
local for the entire function, so the `urllib.parse.quote` call twenty lines above it became a
read before assignment. CrowdWorks exited 1 on every wake from 15:28 until it was found by
reading stderr -- no test covered it, and the wake summaries said nothing because the process
died before writing one.

Python's scoping makes this a whole-function property, not a line-local one, so the check has to
be a whole-function property too: no function may assign a name that the module imported. That is
exact for this bug class and has no false positives, unlike LOAD_FAST_CHECK, which also fires on
the safe `try: x = ... except: _fail()` shape used throughout profile.py.

Run: python3 -m pytest apps/crowdworks-revenue/tests/test_no_shadowed_module_names.py
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = sorted((ROOT / "skills" / "earn" / "crowdworks" / "scripts").glob("*.py"))


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _assigned_names(function: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not function:
            continue  # a nested function has its own scope
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_no_function_shadows_a_module_level_import(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    imported = _imported_names(tree)
    offences = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.FunctionDef) and any(
            isinstance(inner, ast.Global) and set(inner.names) & imported
            for inner in ast.walk(node)
        ):
            continue
        for name in sorted(_assigned_names(node) & imported):
            offences.append(f"{node.name} rebinds {name}")
    assert offences == [], f"{path.name}: {offences}"


def test_the_search_url_builder_still_uses_the_imported_quote():
    """The specific regression, pinned by name so a rename cannot quietly reintroduce it."""
    source = (ROOT / "skills" / "earn" / "crowdworks" / "scripts" / "application_owner.py").read_text(encoding="utf-8")
    assert "from urllib.parse import quote" in source
    assert "reason, evidence_quote = verdict" in source
