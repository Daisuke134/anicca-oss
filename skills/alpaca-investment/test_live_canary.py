import io
import json
import os
import tempfile
import threading
import unittest
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import alpaca_cli
import effect_store
import live_canary


def snapshot():
    return {
        "account": {"equity": "66.70"}, "available_cash_usd": "66.70",
        "open_orders": 0, "positions": 0,
        "risk": {"risk_day_ready": True, "official_pnl_ny_day_usd": "0",
                 "allocated_capital_usd": "0"},
    }


class LiveCanaryTest(unittest.TestCase):
    def env(self, root, deployment="local"):
        return patch.dict(os.environ, {
            "LIFE_MANAGER_INVESTMENT_MODE": "live",
            "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": deployment,
            "ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE": str(root / "credentials.json"),
            "ALPACA_INVESTMENT_LIVE_STATE_DIR": str(root / "state"),
            "ALPACA_CLI": str(root / "alpaca"),
        }, clear=True)

    def common(self, broker_reads, submit):
        candidate = {"candidate_ref": "crypto://BTC/USDC", "quote_age_seconds": 0,
                     "spread_fraction": .01}
        return (patch.object(live_canary, "read_allocator_snapshot", return_value=snapshot()),
                patch.object(live_canary, "build_candidates", return_value=[candidate]),
                patch.object(live_canary, "evaluate_entry", return_value={"approved": True}),
                patch.object(live_canary, "read_live_canary", side_effect=broker_reads),
                patch.object(live_canary, "submit_live_canary", side_effect=submit),
                patch.object(live_canary.time, "sleep"))

    def test_local_output_shape_remains_unchanged(self):
        output = io.StringIO()
        sealed = {"client_order_id": "lm-ai-" + "a" * 24, "effect_id": "b" * 64}
        with redirect_stdout(output):
            self.assertEqual(live_canary._output(
                sealed, {"status": "verified", "verified": True}, False, "local"), 0)
        value = json.loads(output.getvalue())
        self.assertNotIn("deployment", value)
        self.assertEqual(value["canary_ref"], "L09_LOCAL_CANARY_V1")

    def test_verified_fill_is_submitted_once_and_closed(self):
        verified = {"status": "verified", "verified": True, "order": {"id": "one"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mocks = self.common([{"status": "absent", "verified": False}, verified], None)
            with self.env(root), mocks[0], mocks[1], mocks[2], mocks[3], mocks[4] as submit, mocks[5]:
                self.assertEqual(live_canary.main(), 0)
            self.assertEqual(submit.call_count, 1)
            rows = [json.loads(line) for line in
                    (root / "state/receipts.jsonl").read_text().splitlines()]
            self.assertEqual(sum(row.get("outcome") == "live_canary_verified" for row in rows), 1)
            self.assertTrue((root / "state/live-canary.json").is_file())

    def test_started_then_ack_unknown_never_submits_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reads = [{"status": "absent", "verified": False}] * 11
            mocks = self.common(reads, RuntimeError("ack_unknown"))
            with self.env(root), mocks[0], mocks[1], mocks[2], mocks[3], mocks[4] as submit, mocks[5]:
                with self.assertRaisesRegex(RuntimeError, "ack_unknown"):
                    live_canary.main()
            self.assertEqual(submit.call_count, 1)
            mocks = self.common([{"status": "absent", "verified": False}], None)
            with self.env(root), mocks[0] as observe, mocks[1], mocks[2], mocks[3], \
                    mocks[4] as second_submit, mocks[5]:
                self.assertEqual(live_canary.main(), 75)
            observe.assert_not_called()
            second_submit.assert_not_called()

    def test_restart_reconciles_fill_before_new_entry_gate(self):
        verified = {"status": "verified", "verified": True, "order": {"id": "one"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.common([{"status": "absent", "verified": False}], RuntimeError("crash"))
            with self.env(root), first[0], first[1], first[2], first[3], first[4], first[5]:
                with self.assertRaises(RuntimeError):
                    live_canary.main()
            second = self.common([verified], None)
            with self.env(root), second[0] as observe, second[1], second[2], second[3], \
                    second[4] as submit, second[5]:
                self.assertEqual(live_canary.main(), 0)
            observe.assert_not_called()
            submit.assert_not_called()

    def test_two_workers_share_one_submit_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "receipts.jsonl"
            sealed = effect_store.seal(ledger, live_canary.DECISION, live_canary.ORDER)
            barrier = threading.Barrier(2)
            results = []
            def worker():
                barrier.wait()
                results.append(effect_store.mark_started(ledger, sealed))
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(sorted(results), [False, True])

    def test_cloud_canary_has_distinct_identity_and_replays_without_a_second_submit(self):
        verified = {"status": "verified", "verified": True,
                    "order": {"id": "cloud-one", "filled_qty": "0.00002"},
                    "position": {"symbol": "BTCUSDC", "qty": "0.00001995"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = effect_store.seal(root / "local.jsonl", live_canary.DECISION, live_canary.ORDER)
            first = self.common([{"status": "absent", "verified": False}, verified], None)
            with self.env(root, "cloud"), first[0], first[1], first[2], first[3], \
                    first[4] as submit, first[5]:
                self.assertEqual(live_canary.main(), 0)
            self.assertEqual(submit.call_count, 1)
            rows = [json.loads(line) for line in
                    (root / "state/receipts.jsonl").read_text().splitlines()]
            started = next(row for row in rows if row.get("status") == "started")
            decision = next(row for row in rows if row.get("decision_id") == started["decision_id"]
                            and row.get("receipt_type") == "decision")
            self.assertEqual(decision["decision"]["canary_ref"], "L15_CLOUD_CANARY_V1")
            self.assertNotEqual(started["client_order_id"], local["client_order_id"])
            ownership = json.loads((root / "state/live-owned-position.json").read_text())
            self.assertEqual(ownership, {
                "entry_client_order_id": started["client_order_id"],
                "entry_effect_id": started["effect_id"], "entry_filled_qty": "0.00002",
                "owned_qty": "0.00001995", "status": "open", "symbol": "BTCUSD",
            })
            replay = self.common([verified], None)
            with self.env(root, "cloud"), replay[0], replay[1], replay[2], replay[3], \
                    replay[4] as second_submit, replay[5]:
                self.assertEqual(live_canary.main(), 0)
            second_submit.assert_not_called()

    def test_cloud_submit_crash_leaves_pending_ownership_for_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.common([{"status": "absent", "verified": False}], RuntimeError("ack_unknown"))
            with self.env(root, "cloud"), first[0], first[1], first[2], first[3], first[4], first[5]:
                with self.assertRaisesRegex(RuntimeError, "ack_unknown"):
                    live_canary.main()
            ownership = json.loads((root / "state/live-owned-position.json").read_text())
            self.assertEqual(ownership["status"], "entry_pending")
            self.assertEqual(ownership["symbol"], "BTCUSD")

    def test_cloud_ledger_crash_window_already_has_pending_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.common([{"status": "absent", "verified": False}], None)
            with self.env(root, "cloud"), first[0], first[1], first[2], first[3], \
                    patch.object(live_canary, "mark_started",
                                 side_effect=RuntimeError("ledger_crash")), first[5]:
                with self.assertRaisesRegex(RuntimeError, "ledger_crash"):
                    live_canary.main()
            ownership = json.loads((root / "state/live-owned-position.json").read_text())
            self.assertEqual(ownership["status"], "entry_pending")
            self.assertEqual(ownership["symbol"], "BTCUSD")

    def test_cloud_fence_rechecks_official_slots_before_submit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale, occupied = snapshot(), snapshot()
            occupied["positions"] = 1
            first = self.common([{"status": "absent", "verified": False}], None)
            with self.env(root, "cloud"), first[0], first[1], first[2], first[3], \
                    first[4] as submit, first[5], patch.object(
                        live_canary, "read_allocator_snapshot",
                        side_effect=[stale, occupied]):
                with self.assertRaisesRegex(ValueError, "live_canary_gate_rejected"):
                    live_canary.main()
            submit.assert_not_called()
            self.assertFalse((root / "state/live-owned-position.json").exists())

    def test_verified_replay_never_rolls_closing_ownership_back_to_open(self):
        verified = {"status": "verified", "verified": True,
                    "order": {"filled_qty": "0.00002"},
                    "position": {"symbol": "BTCUSDC", "qty": "0.00001995"}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sealed = effect_store.seal(root / "state/receipts.jsonl",
                live_canary._decision("cloud"), live_canary.ORDER)
            closing = {"entry_client_order_id": sealed["client_order_id"],
                "entry_effect_id": sealed["effect_id"], "entry_filled_qty": "0.00002",
                "owned_qty": "0.00001995", "close_client_order_id": "close-one",
                "close_effect_id": "close-effect", "status": "closing", "symbol": "BTCUSD"}
            live_canary._write_result(root / "state/live-owned-position.json", closing)
            live_canary._write_cloud_ownership(root / "state", sealed, verified)
            self.assertEqual(json.loads(
                (root / "state/live-owned-position.json").read_text()), closing)

    def test_submit_boundary_rejects_every_nonfrozen_shape(self):
        expected = dict(live_canary.ORDER)
        changes = {"asset_class": "us_equity", "notional_usd": "2.01", "side": "sell",
                   "symbol": "ETH/USDC", "time_in_force": "ioc", "type": "limit"}
        with patch.dict(os.environ, {"LIFE_MANAGER_INVESTMENT_MODE": "live"}, clear=True), \
                patch.object(alpaca_cli, "_context") as context, patch.object(alpaca_cli, "_run") as run:
            for key, value in changes.items():
                order = dict(expected); order[key] = value
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "shape_invalid"):
                    alpaca_cli.submit_live_canary(credentials_path=Path("c"), cli_path=Path("a"),
                                                  client_order_id="lm-ai-" + "a" * 24, order=order)
            context.assert_not_called()
            run.assert_not_called()

    def test_submit_boundary_rejects_nonlive_mode_and_bad_client_id(self):
        for mode, client_id in (("shadow", "lm-ai-" + "a" * 24),
                                ("live", "user-supplied-id")):
            with self.subTest(mode=mode), patch.dict(
                    os.environ, {"LIFE_MANAGER_INVESTMENT_MODE": mode}, clear=True), patch.object(
                    alpaca_cli, "_context") as context, patch.object(alpaca_cli, "_run") as run:
                with self.assertRaises(ValueError):
                    alpaca_cli.submit_live_canary(
                        credentials_path=Path("c"), cli_path=Path("a"),
                        client_order_id=client_id, order=dict(live_canary.ORDER))
                context.assert_not_called()
                run.assert_not_called()

    def test_gate_rejects_unresolved_and_nonfinite_money(self):
        candidate = {"candidate_ref": "crypto://BTC/USDC", "quote_age_seconds": 0,
                     "spread_fraction": .01}
        base = snapshot(); base["unresolved_intents"] = 0
        with tempfile.TemporaryDirectory() as directory, patch.object(
                live_canary, "build_candidates", return_value=[candidate]), patch.object(
                live_canary, "evaluate_entry", return_value={"approved": True}):
            for change in ({"unresolved_intents": 1}, {"available_cash_usd": "nan"},
                           {"account": {"equity": "nan"}}):
                value = {**base, **change}
                with self.subTest(change=change), self.assertRaisesRegex(
                        ValueError, "live_canary_gate_rejected"):
                    live_canary._gate(value, Path(directory))


class LiveCanaryVerifierTest(unittest.TestCase):
    ORDER = {"found": True, "id": "order-1", "client_order_id": "lm-ai-" + "a" * 24,
             "status": "filled", "filled_qty": "0.00002", "filled_avg_price": "100000",
             "symbol": "BTCUSDC", "side": "buy", "notional": "2", "type": "market",
             "time_in_force": "gtc"}

    def verify(self, responses):
        with patch.object(alpaca_cli, "_context", return_value={}), patch.object(
                alpaca_cli, "_run", side_effect=responses):
            return alpaca_cli.read_live_canary(
                credentials_path=Path("c"), cli_path=Path("a"),
                client_order_id=self.ORDER["client_order_id"])

    def test_multiple_fills_sum_to_order_and_position(self):
        fills = [{"order_id": "order-1", "symbol": "BTCUSDC", "side": "buy",
                  "qty": "0.00001", "price": "100000",
                  "transaction_time": datetime.now(timezone.utc).isoformat()}] * 2
        result = self.verify([self.ORDER, fills,
                              [{"symbol": "BTCUSDC", "qty": "0.00002"}]])
        self.assertTrue(result["verified"])
        self.assertEqual(len(result["fills"]), 2)

    def test_btcusd_position_allows_broker_deducted_crypto_fee(self):
        fill = {"order_id": "order-1", "symbol": "BTC/USDC", "side": "buy",
                "qty": "0.00002", "price": "100000", "transaction_time": "fixture"}
        result = self.verify([self.ORDER, [fill],
                              [{"symbol": "BTCUSD", "qty": "0.00001995"}]])
        self.assertTrue(result["verified"])

    def test_position_fee_over_one_percent_fails_closed(self):
        fill = {"order_id": "order-1", "symbol": "BTC/USDC", "side": "buy",
                "qty": "0.00002", "price": "100000", "transaction_time": "fixture"}
        with self.assertRaisesRegex(ValueError, "live_canary_fill_mismatch"):
            self.verify([self.ORDER, [fill], [{"symbol": "BTCUSD", "qty": "0.000019"}]])

    def test_partial_cancel_is_not_closed_as_terminal_outcome(self):
        order = {**self.ORDER, "status": "canceled", "filled_qty": "0.00001"}
        result = self.verify([order])
        self.assertEqual(result["status"], "partial_terminal")
        self.assertFalse(result["verified"])

    def test_fill_or_position_mismatch_fails_closed(self):
        fill = {"order_id": "wrong", "symbol": "BTCUSDC", "side": "buy",
                "qty": "0.00002", "price": "100000", "transaction_time": "fixture"}
        with self.assertRaisesRegex(ValueError, "live_canary_fill_mismatch"):
            self.verify([self.ORDER, [fill], [{"symbol": "BTCUSDC", "qty": "0.00002"}]])


if __name__ == "__main__":
    unittest.main()
