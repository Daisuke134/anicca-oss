"""Sealed L04.3 replay across signup, paper effects, restart, and Telegram."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(REPO))

import effect_store
import reporter

STATUS_SPEC = importlib.util.spec_from_file_location(
    "investment_status", REPO / "skills/anicca-life-manager/scripts/investment_status.py"
)
investment_status = importlib.util.module_from_spec(STATUS_SPEC)
assert STATUS_SPEC.loader is not None
STATUS_SPEC.loader.exec_module(investment_status)


class _TelegramClient:
    def __init__(self, sends: list[str]):
        self.sends = sends

    def send_text(self, message: str, *, chat_id: str):
        self.sends.append(message)
        return {"message_ids": [f"fixture-message-{len(self.sends)}"]}


class PreapprovalReplayTest(unittest.TestCase):
    def test_sealed_preapproval_replay_is_effect_and_message_idempotent(self):
        fixture = json.loads(
            (ROOT / "fixtures/preapproval-replay.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("credential", json.dumps(fixture).lower())

        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory)
            state = state_root / "alpaca-investment"

            setup = investment_status.build_investment_reply(state_root)
            self.assertIn("口座開設と本人確認", setup["text"])
            self.assertEqual(
                setup["reply_markup"]["inline_keyboard"][0][0]["url"],
                investment_status.ALPACA_SIGNUP_URL,
            )

            state.mkdir(parents=True)
            (state / "account-status.json").write_text(
                json.dumps(fixture["application_status"]), encoding="utf-8"
            )
            (state / "observation-latest.json").write_text(
                json.dumps(fixture["observation"]), encoding="utf-8"
            )
            (state / "allocation-latest.json").write_text(
                json.dumps(fixture["no_trade"]), encoding="utf-8"
            )
            review = investment_status.build_investment_reply(state_root)
            self.assertIn("ライブ口座: 審査中", review["text"])
            self.assertIn("Fixture has no directional evidence", review["text"])

            ledger = state / "receipts.jsonl"
            no_trade_id = effect_store.record_no_trade(ledger, fixture["no_trade"])
            self.assertEqual(no_trade_id, effect_store.record_no_trade(ledger, fixture["no_trade"]))

            sealed = effect_store.seal(
                ledger, fixture["approved_proposal"], fixture["order"]
            )
            effect_store.mark_started(ledger, sealed)
            broker_reads: list[str] = []

            def find_order(client_order_id: str):
                broker_reads.append(client_order_id)
                return {"client_order_id": client_order_id, "status": "filled"}

            first = effect_store.reconcile_started(ledger, find_order)
            self.assertEqual(first, {"pending": 1, "reconciled": 1, "unresolved": 0})

            restarted_store = importlib.reload(effect_store)
            second = restarted_store.reconcile_started(ledger, find_order)
            self.assertEqual(second, {"pending": 0, "reconciled": 0, "unresolved": 0})
            self.assertEqual(broker_reads, [sealed["client_order_id"]])

            sends: list[str] = []
            with patch.object(
                reporter, "_telegram_client", return_value=(_TelegramClient(sends), "fixture-chat")
            ):
                delivered = reporter.deliver(
                    state,
                    fixture["observation"],
                    fixture["campaign"],
                    fixture["no_trade"],
                    "none",
                    event_key="a" * 64,
                )
                replayed = reporter.deliver(
                    state,
                    fixture["observation"],
                    fixture["campaign"],
                    {**fixture["no_trade"], "observed_at": "2026-09-10T12:05:59.000Z"},
                    "none",
                    event_key="a" * 64,
                )
                other_mode = reporter.deliver(
                    state, fixture["observation"], fixture["campaign"],
                    {**fixture["no_trade"], "observed_at": "2026-09-10T12:05:59.000Z"},
                    "effect", event_key="b" * 64)

            self.assertEqual(delivered["message_id"], replayed["message_id"])
            self.assertEqual(delivered["status"], replayed["status"])
            self.assertNotEqual(delivered["message_id"], other_mode["message_id"])
            self.assertEqual(len(sends), 2)
            self.assertIn("[Investment Loop][投資判断]", sends[0])
            self.assertIn("判断: NO_TRADE", sends[0])
            self.assertNotIn("Codex", sends[0])

            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual(sum(row.get("outcome") == "no_trade" for row in rows), 1)
            self.assertEqual(sum(row.get("receipt_type") == "outcome" for row in rows), 1)
            self.assertEqual(len(broker_reads), 1)


if __name__ == "__main__":
    unittest.main()
