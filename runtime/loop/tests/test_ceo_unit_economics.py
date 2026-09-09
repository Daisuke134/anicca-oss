#!/usr/bin/env python3
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))

from ceo_unit_economics import build_snapshot, validate_allocation_policy  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class CEOUnitEconomicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "config").mkdir()
        (self.base / "ledgers").mkdir()
        (self.base / "config" / "loop-registry.json").write_text(
            json.dumps({"loops": {"gig": {"allocation": {"status": "normal"}}}}), encoding="utf-8"
        )
        self.end = date(2026, 7, 22)
        self.usage = self.base / "usage.jsonl"
        self.revenue = self.base / "ledgers" / "revenue-events.jsonl"
        self.queue = self.base / "gig" / "delivery-queue.json"

    def tearDown(self):
        self.tmp.cleanup()

    def usage_rows(self, *, unavailable_day: int | None = None, with_cost: bool = True,
                   cost_basis: str = "actual_billed") -> list[dict]:
        rows = []
        start = self.end - timedelta(days=6)
        for index in range(7):
            day = start + timedelta(days=index)
            measured = index != unavailable_day
            rows.append({
                "timestamp": datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
                "loop": "gig",
                "task_label": "/secret/customer/prompt",
                "measurement": "provider_reported" if measured else "unavailable",
                "tokens": {"total": 100 if measured else None},
                "provider_cost_usd": 1.0 if measured and with_cost else None,
                "cost_basis": cost_basis if measured and with_cost else "unavailable",
            })
        return rows

    def revenue_row(self) -> dict:
        return {
            "timestamp": "2026-07-20T00:00:00+00:00", "loop": "gig", "status": "settled",
            "currency": "USD", "amount": 20.0, "evidence": "verified-receipt",
        }

    def build(self, *, rows=None, revenue=None, queue=None, mode="active"):
        write_jsonl(self.usage, rows if rows is not None else self.usage_rows())
        write_jsonl(self.revenue, revenue if revenue is not None else [self.revenue_row()])
        self.queue.parent.mkdir(parents=True, exist_ok=True)
        self.queue.write_text(json.dumps(queue if queue is not None else {"items": []}), encoding="utf-8")
        config = {
            "mode": mode,
            "window_days": 7,
            "min_usage_coverage": 0.9,
            "min_complete_days": 7,
            "usage_ledger": str(self.usage),
            "revenue_ledger": str(self.revenue),
            "paid_obligation_sources": {
                "gig": {"path": str(self.queue), "max_age_seconds": 999999999, "fail_closed": True}
            },
        }
        return build_snapshot(self.base, config, self.end)

    def test_complete_measured_profitable_window_is_eligible(self):
        snapshot = self.build()
        gig = snapshot["loops"]["gig"]
        self.assertEqual(gig["complete_days"], 7)
        self.assertEqual(gig["usage_coverage"], 1.0)
        self.assertEqual(gig["actual_cost_coverage"], 1.0)
        self.assertEqual(gig["profitability_status"], "positive")
        self.assertEqual(gig["profit_usd"], 13.0)
        self.assertTrue(gig["allocation_eligible"])

    def test_unavailable_usage_is_not_zero_and_blocks_eligibility(self):
        snapshot = self.build(rows=self.usage_rows(unavailable_day=3))
        gig = snapshot["loops"]["gig"]
        self.assertEqual(gig["unavailable_attempts"], 1)
        self.assertAlmostEqual(gig["usage_coverage"], 6 / 7)
        self.assertFalse(gig["allocation_eligible"])
        self.assertIn("usage_coverage_below_threshold", gig["eligibility_reasons"])

    def test_missing_provider_cost_keeps_profitability_unknown(self):
        snapshot = self.build(rows=self.usage_rows(with_cost=False))
        gig = snapshot["loops"]["gig"]
        self.assertEqual(gig["profitability_status"], "unknown")
        self.assertIsNone(gig["profit_usd"])
        self.assertIn("actual_cost_unavailable", gig["eligibility_reasons"])

    def test_api_equivalent_estimate_does_not_unlock_profitability(self):
        snapshot = self.build(rows=self.usage_rows(cost_basis="api_equivalent_estimate"))
        gig = snapshot["loops"]["gig"]
        self.assertEqual(gig["profitability_status"], "unknown")
        self.assertEqual(gig["actual_cost_coverage"], 0.0)

    def test_missing_settled_revenue_keeps_profitability_unknown(self):
        snapshot = self.build(revenue=[])
        gig = snapshot["loops"]["gig"]
        self.assertEqual(gig["profitability_status"], "unknown")
        self.assertIn("settled_revenue_unavailable", gig["eligibility_reasons"])

    def test_active_paid_item_protects_gig_from_reduce_or_pause(self):
        snapshot = self.build(queue={"items": [{"status": "paid", "delivery_action": "progress"}]})
        gig = snapshot["loops"]["gig"]
        self.assertTrue(gig["paid_obligation_protected"])
        self.assertIn("active_paid_obligation", gig["eligibility_reasons"])
        self.assertIn("paid_obligation_protected", validate_allocation_policy(snapshot, "gig", "paused"))
        self.assertIn("paid_obligation_protected", validate_allocation_policy(snapshot, "gig", "reduce"))

    def test_policy_protected_real_loop_id_never_becomes_allocation_eligible(self):
        (self.base / "config" / "loop-registry.json").write_text(
            json.dumps({"loops": {"hf-gig-paid-direct": {}}}), encoding="utf-8"
        )
        config = {
            "mode": "active",
            "window_days": 7,
            "min_usage_coverage": 0,
            "min_complete_days": 0,
            "usage_ledger": str(self.usage),
            "revenue_ledger": str(self.revenue),
            "allocation_protected_loops": ["hf-gig-paid-direct"],
        }
        write_jsonl(self.revenue, [{
            "timestamp": "2026-07-20T00:00:00+00:00",
            "loop": "hf-gig-paid-direct",
            "status": "settled",
            "currency": "USD",
            "amount": 20.0,
            "evidence": "verified-receipt",
        }])

        row = build_snapshot(self.base, config, self.end)["loops"]["hf-gig-paid-direct"]

        self.assertTrue(row["paid_obligation_protected"])
        self.assertEqual(row["paid_obligation_state"], "allocation_policy_protected")
        self.assertFalse(row["allocation_eligible"])

    def test_missing_paid_queue_fails_closed(self):
        config = {
            "mode": "active", "window_days": 7, "min_usage_coverage": 0.9,
            "min_complete_days": 7, "usage_ledger": str(self.usage),
            "revenue_ledger": str(self.revenue),
            "paid_obligation_sources": {"gig": {"path": str(self.base / "missing.json"), "fail_closed": True}},
        }
        write_jsonl(self.usage, self.usage_rows())
        write_jsonl(self.revenue, [self.revenue_row()])
        snapshot = build_snapshot(self.base, config, self.end)
        self.assertTrue(snapshot["loops"]["gig"]["paid_obligation_protected"])

    def test_observe_only_rejects_every_allocation_change(self):
        snapshot = self.build(mode="observe_only")
        self.assertIn("ceo_observe_only", validate_allocation_policy(snapshot, "gig", "double_down"))

    def test_snapshot_does_not_leak_raw_task_or_paths(self):
        rendered = json.dumps(self.build())
        self.assertNotIn("task_label", rendered)
        self.assertNotIn("/secret/customer/prompt", rendered)
        self.assertNotIn(str(self.usage), rendered)


if __name__ == "__main__":
    unittest.main()
