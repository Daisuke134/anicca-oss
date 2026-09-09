import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import alpaca_cli
import live_close


QTY = "0.000024974"
CID = "lm-ai-" + "b" * 24
ORDER = {"found": True, "id": "close-1", "client_order_id": CID, "status": "filled",
         "filled_qty": QTY, "symbol": "BTC/USDC", "side": "sell", "qty": QTY,
         "type": "market", "time_in_force": "gtc"}


class LiveCloseBoundaryTest(unittest.TestCase):
    def test_submit_accepts_only_exact_frozen_shape(self):
        expected = {"asset_class": "crypto", "qty": QTY, "side": "sell",
                    "symbol": "BTC/USDC", "time_in_force": "gtc", "type": "market"}
        changes = {"qty": "0.000024975", "side": "buy", "symbol": "BTC/USD",
                   "time_in_force": "ioc", "type": "limit"}
        with patch.dict(os.environ, {"LIFE_MANAGER_INVESTMENT_MODE": "live"}, clear=True), \
                patch.object(alpaca_cli, "_context") as context, patch.object(
                    alpaca_cli, "_run") as run:
            for key, value in changes.items():
                order = dict(expected); order[key] = value
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "shape_invalid"):
                    alpaca_cli.submit_live_close(
                        credentials_path=Path("c"), cli_path=Path("a"), client_order_id=CID,
                        frozen_qty=QTY, order=order)
            context.assert_not_called(); run.assert_not_called()

    def test_nonlive_mode_never_reaches_provider(self):
        expected = {"asset_class": "crypto", "qty": QTY, "side": "sell",
                    "symbol": "BTC/USDC", "time_in_force": "gtc", "type": "market"}
        with patch.dict(os.environ, {"LIFE_MANAGER_INVESTMENT_MODE": "shadow"}, clear=True), \
                patch.object(alpaca_cli, "_context") as context:
            with self.assertRaisesRegex(ValueError, "effect_forbidden"):
                alpaca_cli.submit_live_close(credentials_path=Path("c"), cli_path=Path("a"),
                                             client_order_id=CID, frozen_qty=QTY, order=expected)
            context.assert_not_called()


class LiveCloseVerifierTest(unittest.TestCase):
    def read(self, responses):
        with patch.object(alpaca_cli, "_context", return_value={}), patch.object(
                alpaca_cli, "_run", side_effect=responses):
            return alpaca_cli.read_live_close(
                credentials_path=Path("c"), cli_path=Path("a"), client_order_id=CID,
                frozen_qty=QTY, pre_usdc_qty="64.795010753",
                buy_gross_cost_usdc="1.96075",
                external_flow_fingerprint=alpaca_cli._digest_rows([]))

    def test_multiple_fills_position_zero_and_usdc_gain_verify(self):
        fill = {"order_id": "close-1", "symbol": "BTC/USDC", "side": "sell",
                "qty": "0.000012487", "price": "78400", "transaction_time": "fixture"}
        fill2 = {**fill, "qty": "0.000012487"}
        gross = (float(fill["qty"]) + float(fill2["qty"])) * float(fill["price"])
        fee = gross - (66.748 - 64.795010753)
        result = self.read([ORDER, [fill, fill2],
                            [{"symbol": "USDCUSD", "qty": "66.748", "market_value": "66.7"}],
                            0, [{"activity_type": "CFEE", "order_id": "close-1",
                                 "symbol": "USDCUSD", "qty": str(-fee)}], [], []])
        self.assertTrue(result["verified"])
        self.assertGreater(float(result["official_usdc_delta"]), 0)

    def test_partial_cancel_stays_unresolved(self):
        result = self.read([{**ORDER, "status": "canceled", "filled_qty": "0.00001"}])
        self.assertEqual(result["status"], "partial_terminal")
        self.assertFalse(result["verified"])

    def test_remaining_btc_fails_closed(self):
        fill = {"order_id": "close-1", "symbol": "BTC/USDC", "side": "sell",
                "qty": QTY, "price": "78400", "transaction_time": "fixture"}
        positions = [{"symbol": "BTCUSD", "qty": "0.000000001"},
                     {"symbol": "USDCUSD", "qty": "66.748"}]
        with self.assertRaisesRegex(ValueError, "fill_mismatch"):
            self.read([ORDER, [fill], positions, 0, [], [], []])

    def test_wrong_order_shape_fails_before_fill_read(self):
        with self.assertRaisesRegex(ValueError, "order_mismatch"):
            self.read([{**ORDER, "side": "buy"}])

    def test_unknown_position_and_external_cashflow_fail_closed(self):
        fill = {"order_id": "close-1", "symbol": "BTC/USDC", "side": "sell",
                "qty": QTY, "price": "78400", "transaction_time": "fixture"}
        for positions, bank in (([{"symbol": "USDCUSD", "qty": "66.748"},
                                  {"symbol": "ETHUSD", "qty": "0.1"}], []),
                                 ([{"symbol": "USDCUSD", "qty": "66.748"}],
                                  [{"activity_type": "CSD", "id": "new"}])):
            with self.subTest(positions=positions, bank=bank), self.assertRaisesRegex(
                    ValueError, "fill_mismatch"):
                self.read([ORDER, [fill], positions, 0, [], bank, []])

    def test_missing_fee_is_pending_not_verified(self):
        fill = {"order_id": "close-1", "symbol": "BTC/USDC", "side": "sell",
                "qty": QTY, "price": "78400", "transaction_time": "fixture"}
        result = self.read([ORDER, [fill], [{"symbol": "USDCUSD", "qty": "66.748"}],
                            0, [], [], []])
        self.assertEqual(result["status"], "fee_pending")
        self.assertFalse(result["verified"])

    def test_negative_or_excessive_fee_fails_closed(self):
        fill = {"order_id": "close-1", "symbol": "BTC/USDC", "side": "sell",
                "qty": QTY, "price": "78400", "transaction_time": "fixture"}
        for fee_qty in ("0.01", "-1"):
            fee = [{"activity_type": "CFEE", "order_id": "close-1",
                    "symbol": "USDCUSD", "qty": fee_qty}]
            with self.subTest(fee_qty=fee_qty), self.assertRaisesRegex(
                    ValueError, "fee_mismatch"):
                self.read([ORDER, [fill], [{"symbol": "USDCUSD", "qty": "66.748"}],
                           0, fee, [], []])


class LiveCloseReplayTest(unittest.TestCase):
    def env(self, root):
        return patch.dict(os.environ, {
            "LIFE_MANAGER_INVESTMENT_MODE": "live",
            "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": "local",
            "ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE": str(root / "credentials.json"),
            "ALPACA_INVESTMENT_LIVE_STATE_DIR": str(root),
            "ALPACA_CLI": str(root / "alpaca"),
        }, clear=True)

    def seed(self, root):
        (root / "live-canary.json").write_text(json.dumps({
            "verified": True, "status": "verified",
            "order": {"client_order_id": "lm-ai-3367d3a6cc659b8017a57f3d",
                      "id": "buy-1", "symbol": "BTC/USDC", "filled_qty": "0.000025037"},
            "position": {"symbol": "BTCUSD", "qty": QTY},
            "fills": [{"qty": "0.000025037", "price": "78314.105"}]}))
        return {"account": {"status": "ACTIVE", "crypto_status": "ACTIVE",
                            "trading_blocked": False, "transfers_blocked": False,
                            "account_blocked": False}, "open_orders": [],
                "positions": [{"symbol": "BTCUSD", "qty": QTY},
                              {"symbol": "USDCUSD", "qty": "64.795010753"}],
                "asset": {"status": "active", "tradable": True},
                "quote": {"bp": "78000", "ap": "78100", "t": "2099-01-01T00:00:00Z"},
                "fees": [{"order_id": "buy-1", "symbol": "BTCUSD",
                          "qty": "-0.000000063"}],
                "external_flow_fingerprint": alpaca_cli._digest_rows([])}

    def test_ack_unknown_then_restart_never_submits_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); snap = self.seed(root)
            with self.env(root), patch.object(live_close, "read_live_close_snapshot",
                    return_value=snap), patch.object(live_close, "_gate"), patch.object(
                    live_close, "read_live_close", return_value={"status": "absent",
                    "verified": False}), patch.object(live_close, "submit_live_close",
                    side_effect=RuntimeError("ack_unknown")) as submit:
                with self.assertRaisesRegex(RuntimeError, "ack_unknown"):
                    live_close.main()
            self.assertEqual(submit.call_count, 1)
            with self.env(root), patch.object(live_close, "read_live_close_snapshot") as observe, \
                    patch.object(live_close, "read_live_close", return_value={
                        "status": "absent", "verified": False}), patch.object(
                        live_close, "submit_live_close") as retry:
                self.assertEqual(live_close.main(), 75)
            observe.assert_not_called(); retry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
