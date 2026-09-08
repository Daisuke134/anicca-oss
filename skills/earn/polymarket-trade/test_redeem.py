#!/usr/bin/env python3
"""RED->GREEN tests for redeem.py's pure functions (no network, no chain, no relayer).

Run: python3 test_redeem.py   (stdlib unittest only — no pytest install required)
"""
from __future__ import annotations
import subprocess
import unittest
from unittest import mock

import os
import tempfile
from pathlib import Path

import redeem
from state_paths import external_state_path
from redeem import (
    configured_deposit_wallet,
    dedupe_redeemable_conditions,
    classify_market_type,
    compute_recovered_amount,
    build_ledger_line,
    check_cumulative_halt,
    write_kill_switch,
)


class TestRuntimeConfiguration(unittest.TestCase):
    def test_explicit_deposit_wallet_is_required_and_normalized(self):
        with mock.patch.dict(os.environ, {"PM_DEPOSIT_WALLET": "0x" + "A" * 40}, clear=True):
            self.assertEqual(configured_deposit_wallet(), "0x" + "a" * 40)

    def test_invalid_deposit_wallet_fails_closed(self):
        with mock.patch.dict(os.environ, {"PM_DEPOSIT_WALLET": "not-a-wallet"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "PM_DEPOSIT_WALLET"):
                configured_deposit_wallet()

    def test_mutable_state_rejects_relative_and_repository_paths(self):
        with self.assertRaisesRegex(RuntimeError, "must be absolute"):
            external_state_path("state/file", redeem.REPO_ROOT, "state")
        with self.assertRaisesRegex(RuntimeError, "outside the repository"):
            external_state_path(str(redeem.REPO_ROOT / "state/file"), redeem.REPO_ROOT, "state")


class TestDedupeRedeemableConditions(unittest.TestCase):
    def test_merges_two_outcome_rows_sharing_one_condition(self):
        # Wimbledon: same conditionId appears twice (winning Flavio leg $10, losing
        # Karen leg $0) — must collapse into ONE row summing currentValue.
        positions = [
            {"conditionId": "0xc8a0", "title": "Wimbledon", "negativeRisk": False,
             "currentValue": 10, "initialValue": 3.55, "cashPnl": 6.45, "redeemable": True},
            {"conditionId": "0xc8a0", "title": "Wimbledon", "negativeRisk": False,
             "currentValue": 0, "initialValue": 3.55, "cashPnl": -3.55, "redeemable": True},
        ]
        rows = dedupe_redeemable_conditions(positions)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["currentValue"], 10)
        self.assertAlmostEqual(rows[0]["initialValue"], 7.10)

    def test_skips_non_redeemable_positions(self):
        positions = [
            {"conditionId": "0xopen", "title": "Still open", "negativeRisk": False,
             "currentValue": 5, "initialValue": 4, "cashPnl": 1, "redeemable": False},
        ]
        self.assertEqual(dedupe_redeemable_conditions(positions), [])

    def test_keeps_distinct_conditions_separate(self):
        positions = [
            {"conditionId": "0xa", "title": "A", "negativeRisk": True,
             "currentValue": 6.7857, "initialValue": 3.7999, "cashPnl": 2.9857, "redeemable": True},
            {"conditionId": "0xb", "title": "B", "negativeRisk": False,
             "currentValue": 5, "initialValue": 3.65, "cashPnl": 1.35, "redeemable": True},
        ]
        rows = dedupe_redeemable_conditions(positions)
        self.assertEqual({r["conditionId"] for r in rows}, {"0xa", "0xb"})

    def test_sorted_by_currentValue_descending(self):
        positions = [
            {"conditionId": "0xsmall", "title": "small", "negativeRisk": False,
             "currentValue": 1, "initialValue": 1, "cashPnl": 0, "redeemable": True},
            {"conditionId": "0xbig", "title": "big", "negativeRisk": False,
             "currentValue": 10, "initialValue": 1, "cashPnl": 9, "redeemable": True},
        ]
        rows = dedupe_redeemable_conditions(positions)
        self.assertEqual([r["conditionId"] for r in rows], ["0xbig", "0xsmall"])


class TestClassifyMarketType(unittest.TestCase):
    def test_neg_risk_true(self):
        self.assertEqual(classify_market_type(True), "neg_risk")

    def test_neg_risk_false(self):
        self.assertEqual(classify_market_type(False), "standard")


class TestComputeRecoveredAmount(unittest.TestCase):
    def test_positive_delta(self):
        self.assertAlmostEqual(compute_recovered_amount(0.2411, 21.79), 21.5489, places=4)

    def test_zero_delta(self):
        self.assertEqual(compute_recovered_amount(5.0, 5.0), 0.0)

    def test_negative_delta_raises(self):
        # R4 guardrail: money must never appear to leave this wallet during a redeem.
        with self.assertRaises(ValueError):
            compute_recovered_amount(10.0, 9.0)


class TestBuildLedgerLine(unittest.TestCase):
    def test_fields_match_earn_ledger_schema(self):
        row = {
            "conditionId": "0xa947b7c6",
            "title": "Will Morocco win on 2026-07-04?",
            "negativeRisk": True,
            "currentValue": 6.7857,
            "initialValue": 3.7999,
            "cashPnl": 2.9857,
        }
        line = build_ledger_line(
            row, tx_hash="0xdeadbeef", status="0x1", wallet="0x" + "a" * 40
        )
        self.assertEqual(line["source"], "polymarket-redeem")
        self.assertEqual(line["tx"], "0xdeadbeef")
        self.assertEqual(line["status"], "0x1")
        self.assertEqual(line["chain"], "polygon")
        self.assertTrue(line["external"])
        self.assertAlmostEqual(line["earn_usdc"], 6.7857)
        self.assertAlmostEqual(line["cost_usdc"], 3.7999)


class TestCheckCumulativeHalt(unittest.TestCase):
    """P1 (spec §3/§4): redeem.py asks earn-guard.mjs (via subprocess) whether this
    wallet+source's cumulative earn>spend invariant still holds. These tests mock
    subprocess.run so no real ledger/node process is needed — the CONTRACT under test is
    "exit 0 -> (False, '')" and "exit!=0 -> (True, reason-from-stderr)", fail-closed on error."""

    def test_guard_exit_0_means_not_halted(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="OK: ...")
            halted, reason = check_cumulative_halt("0xabc", "polymarket-redeem")
        self.assertFalse(halted)
        self.assertEqual(reason, "")

    def test_guard_exit_1_means_halted_with_reason_from_stderr(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="HALT: cumulative-net-below-reserve ..."
            )
            halted, reason = check_cumulative_halt("0xabc", "polymarket-redeem")
        self.assertTrue(halted)
        self.assertIn("cumulative-net-below-reserve", reason)

    def test_guard_process_error_fails_closed_as_halted(self):
        # the guard subprocess itself blowing up must NEVER be read as "solvent, keep trading".
        with mock.patch.object(subprocess, "run", side_effect=OSError("node not found")):
            halted, reason = check_cumulative_halt("0xabc", "polymarket-redeem")
        self.assertTrue(halted)
        self.assertIn("guard-error", reason)

    def test_passes_wallet_source_and_ledger_path_through_to_the_guard_cli(self):
        with mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            check_cumulative_halt("0xabc", "polymarket-redeem", ledger_path="/tmp/custom-ledger.jsonl")
        called_args = run.call_args.args[0]
        self.assertIn("check", called_args)
        self.assertIn("0xabc", called_args)
        self.assertIn("polymarket-redeem", called_args)
        self.assertIn(str(Path("/tmp/custom-ledger.jsonl").resolve()), called_args)


class TestWriteKillSwitch(unittest.TestCase):
    """write_kill_switch() targets the shared mutable path read by run.sh."""

    def test_writes_the_reason_to_the_kill_switch_path(self):
        with tempfile.TemporaryDirectory() as d:
            fake_kill = os.path.join(d, "KILL")
            with mock.patch.object(redeem, "KILL_SWITCH", fake_kill):
                write_kill_switch("cumulative-net-below-reserve")
            self.assertTrue(os.path.isfile(fake_kill))
            with open(fake_kill) as f:
                content = f.read()
            self.assertIn("cumulative-net-below-reserve", content)
            self.assertIn("P1 fail-closed HALT", content)


if __name__ == "__main__":
    unittest.main()
