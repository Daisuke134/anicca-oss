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


# --- unchanged messages must not bury the ones that matter, 2026-09-07 ----------------------


def test_the_same_sentence_twice_in_a_row_is_held_back(tmp_path):
    """Measured in Dais's chat: 93 of 200 messages in 48 minutes were one identical sentence,
    and the per-application reports were unfindable underneath them."""
    database = tmp_path / "outbox.sqlite3"
    assert outbox.enqueue(database, "wake-1", "状態は変わっていません", "2026-09-07T06:00:00+00:00")
    assert not outbox.enqueue(database, "wake-2", "状態は変わっていません", "2026-09-07T06:00:30+00:00")
    assert not outbox.enqueue(database, "wake-3", "状態は変わっていません", "2026-09-07T06:01:00+00:00")


def test_a_changed_sentence_goes_out_at_once(tmp_path):
    """The text is how these lanes express state, so any change is news."""
    database = tmp_path / "outbox.sqlite3"
    assert outbox.enqueue(database, "wake-1", "状態は変わっていません", "2026-09-07T06:00:00+00:00")
    assert outbox.enqueue(database, "wake-2", "1件の応募を公式確認しました", "2026-09-07T06:00:30+00:00")
    assert outbox.enqueue(database, "wake-3", "状態は変わっていません", "2026-09-07T06:01:00+00:00")


def test_a_quiet_lane_still_proves_it_is_alive_once_an_hour(tmp_path):
    database = tmp_path / "outbox.sqlite3"
    assert outbox.enqueue(database, "wake-1", "変化なし", "2026-09-07T06:00:00+00:00")
    assert not outbox.enqueue(database, "wake-2", "変化なし", "2026-09-07T06:59:00+00:00")
    assert outbox.enqueue(database, "wake-3", "変化なし", "2026-09-07T07:00:01+00:00")


def test_a_caller_can_insist_the_message_always_goes_out(tmp_path):
    """An irreversible external effect is not a description of state."""
    database = tmp_path / "outbox.sqlite3"
    assert outbox.enqueue(database, "e-1", "応募しました", "2026-09-07T06:00:00+00:00",
                          repeat_after_seconds=None)
    assert outbox.enqueue(database, "e-2", "応募しました", "2026-09-07T06:00:05+00:00",
                          repeat_after_seconds=None)


def test_an_unreadable_timestamp_cannot_silence_a_lane(tmp_path):
    database = tmp_path / "outbox.sqlite3"
    assert outbox.enqueue(database, "wake-1", "変化なし", "not-a-time")
    assert outbox.enqueue(database, "wake-2", "変化なし", "2026-09-07T06:00:00+00:00")


def test_replaying_one_event_key_is_still_false_and_still_conflicts(tmp_path):
    """The suppression must not have changed what idempotency means."""
    database = tmp_path / "outbox.sqlite3"
    assert outbox.enqueue(database, "wake-1", "本文", "2026-09-07T06:00:00+00:00")
    assert not outbox.enqueue(database, "wake-1", "本文", "2026-09-07T06:00:00+00:00")
    with pytest.raises(outbox.IdempotencyConflict):
        outbox.enqueue(database, "wake-1", "別の本文", "2026-09-07T06:00:00+00:00")
