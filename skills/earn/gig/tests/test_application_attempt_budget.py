from __future__ import annotations

import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "application_parent_attempt_budget_test", SCRIPTS / "application_parent.py"
)
assert SPEC and SPEC.loader
application_parent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(application_parent)


def test_parallel_workers_share_one_hard_submit_ceiling(tmp_path: Path) -> None:
    budget = tmp_path / "submit-attempt-budget.json"
    cap = application_parent.snapshot_contract.MAX_APPLICATIONS_CEILING

    with ThreadPoolExecutor(max_workers=40) as pool:
        admitted = list(
            pool.map(
                lambda _index: application_parent._reserve_submit_attempt(
                    budget, pass_id="one-wake", cap=cap
                ),
                range(40),
            )
        )

    assert cap == 20
    assert admitted.count(True) == cap
    assert admitted.count(False) == 40 - cap
    assert json.loads(budget.read_text(encoding="utf-8")) == {
        "version": 1,
        "pass_id": "one-wake",
        "cap": cap,
        "reserved_attempts": cap,
    }
