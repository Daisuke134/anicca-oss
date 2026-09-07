"""An abandoned claim is never blindly retried after a provider call may have started.

Once status is sending, a dead sender cannot prove whether Telegram received the message. The row
therefore becomes delivery_uncertain and requires provider reconciliation.

Run: python3 -m pytest skills/_shared/marketplace-core/tests/test_outbox_claim_fence.py
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


outbox = _load("fence_test_outbox", SCRIPTS / "telegram_outbox.py")


def _abandoned(database: Path):
    outbox.enqueue(database, event_key="k", message="one report",
                   created_at="2026-09-06T11:59:00+00:00")
    claim = outbox.claim_next(database)
    # Backdate the claim so reclaim_stale sees the abandonment it is written for, rather than
    # weakening the window and testing something the production window would never do.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE telegram_outbox SET claimed_at = ? WHERE event_key = 'k'",
            ("2026-09-06T00:00:00+00:00",),
        )
    assert outbox.reclaim_stale(database, older_than_seconds=900) == 1
    return claim


def test_abandoned_sender_is_quarantined_without_reclaim_or_resend(tmp_path):
    database = tmp_path / "outbox.sqlite3"
    _abandoned(database)
    item = outbox.list_items(database)[0]
    assert (item.status, item.last_error_code) == ("delivery_uncertain", "sender_abandoned")
    assert item.claimed_at == "2026-09-06T00:00:00+00:00"
    assert outbox.claim_next(database) is None


def test_provider_receipt_can_reconcile_an_abandoned_claim(tmp_path):
    database = tmp_path / "outbox.sqlite3"
    _abandoned(database)
    outbox.mark_delivered(database, "k", "provider-id", "2026-09-06T12:00:01+00:00",
                          claimed_at="2026-09-06T00:00:00+00:00")
    assert outbox.list_items(database)[0].status == "delivered"


def test_abandoned_claim_cannot_be_returned_to_pending(tmp_path):
    database = tmp_path / "outbox.sqlite3"
    _abandoned(database)
    with pytest.raises(outbox.InvalidState):
        outbox.mark_pre_send_failed(database, "k", "process_not_started",
                                    claimed_at="2026-09-06T00:00:00+00:00")
    assert outbox.list_items(database)[0].status == "delivery_uncertain"


def test_callers_that_pass_no_claim_are_unchanged(tmp_path):
    """The fence is opt-in so migrating lanes one at a time cannot break the ones not yet moved."""
    database = tmp_path / "outbox.sqlite3"
    outbox.enqueue(database, event_key="k", message="one report",
                   created_at="2026-09-06T11:59:00+00:00")
    outbox.claim_next(database)
    outbox.mark_delivered(database, "k", "id-1", "2026-09-06T12:00:00+00:00")
    assert outbox.list_items(database)[0].status == "delivered"
