"""The Apply-owned tab must never survive a failed wake and poison the next CDP attach."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OWNER = ROOT / "skills" / "earn" / "crowdworks" / "scripts" / "application_owner.py"


def test_apply_page_is_closed_in_a_finally_block():
    tree = ast.parse(OWNER.read_text(encoding="utf-8"))
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    finalizers = [node.finalbody for node in ast.walk(main) if isinstance(node, ast.Try) and node.finalbody]
    assert any(
        any(
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Attribute)
            and statement.value.func.attr == "close"
            for statement in finalizer
        )
        for finalizer in finalizers
    )
