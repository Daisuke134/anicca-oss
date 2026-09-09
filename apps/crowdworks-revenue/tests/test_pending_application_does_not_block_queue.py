"""An uncertain effect is reconciled separately and must not monopolize candidate selection."""

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
OWNER = ROOT / "skills" / "earn" / "crowdworks" / "scripts" / "application_owner.py"


def _load():
    spec = importlib.util.spec_from_file_location("crowdworks_owner_pending_test", OWNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pending_project_is_excluded_while_reconcile_keeps_ownership(tmp_path):
    owner = _load()
    owner.LEDGER = tmp_path / "receipts.jsonl"
    owner.TRANSACTION = tmp_path / "transaction.json"
    owner.LEDGER.write_text(json.dumps({"opportunity_external_id": "verified"}) + "\n", encoding="utf-8")
    owner.TRANSACTION.write_text(json.dumps({
        "pending": {"fingerprint": {"project_id": "uncertain", "proposal_id": None}},
    }), encoding="utf-8")

    assert owner._applied() == {"verified", "uncertain"}
