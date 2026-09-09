from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
PATH = ROOT / "skills/earn/lancers/scripts/paid_adapter.py"
OWNER = ROOT / "skills/earn/lancers/scripts/paid-owner"


def load():
    spec = importlib.util.spec_from_file_location("lancers_paid_adapter_test", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LancersPaidAdapterTests(unittest.TestCase):
    def test_owner_enters_shared_kernel_before_reporting(self):
        source = OWNER.read_text(encoding="utf-8")
        kernel = 'skills/_shared/marketplace-core/scripts/paid_kernel.py'
        adapter = 'skills/earn/lancers/scripts/paid_adapter.py'
        reporter = 'skills/earn/lancers/scripts/lane_report.py'
        self.assertIn(kernel, source)
        self.assertIn(adapter, source)
        self.assertLess(source.index(kernel), source.index(reporter))
        self.assertIn('--state-root "$STATE_ROOT/paid"', source)
        self.assertIn('--output "$PAID_OUTPUT"', source)

    def test_build_uses_paid_scoped_official_inventory(self):
        source = PATH.read_text(encoding="utf-8")
        self.assertIn("work_sync.read_paid_inventory", source)
        self.assertNotIn("work_sync.read_only_inventory", source)

    def test_maps_every_contract_candidate_without_claiming_funding(self):
        module = load()
        snapshot = {
            "ok": True,
            "source_complete": True,
            "contract_candidates": [
                {"source_kind": "project", "provider_id": "7", "board_id": None,
                 "detail_path": "/work/detail/7", "funding_status": "requires_detail_readback"},
                {"source_kind": "monthly", "provider_id": "9", "board_id": None,
                 "detail_path": "/monthly_work_contracts/lancer/9", "funding_status": "requires_detail_readback"},
            ],
            "boards": [],
            "finance": {"source_complete": True, "payment_history_count": 0},
        }
        adapter = module.LancersPaidAdapter(
            account_id="seller-1", inventory_reader=lambda: snapshot,
            clock=lambda: "2026-09-07T00:00:00Z",
        )
        rows = adapter.observe_active()
        self.assertEqual([row["work_id"] for row in rows], ["project:7", "monthly:9"])
        self.assertEqual({row["provider_state"] for row in rows}, {"requires_detail_readback"})
        self.assertEqual(adapter.context("project:7")["contract"]["provider_id"], "7")

    def test_decision_waits_for_official_contract_detail(self):
        module = load()
        decision = module.decide({
            "provider": "lancers", "account_id": "seller-1", "work_id": "project:7",
            "latest_event_id": "digest", "provider_state": "requires_detail_readback",
            "observed_at": "2026-09-07T00:00:00Z", "context": {},
        })
        self.assertEqual(decision["action"], "wait")
        self.assertEqual(decision["reason"], "official_contract_detail_required")
        self.assertTrue(decision["remaining_work"])

    def test_incomplete_inventory_fails_closed(self):
        module = load()
        adapter = module.LancersPaidAdapter(
            account_id="seller-1",
            inventory_reader=lambda: {"ok": False, "source_complete": False},
            clock=lambda: "2026-09-07T00:00:00Z",
        )
        with self.assertRaisesRegex(RuntimeError, "lancers_paid_inventory_unavailable"):
            adapter.observe_active()


if __name__ == "__main__":
    unittest.main()
