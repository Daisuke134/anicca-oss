"""Experiment metric measurement: measurable metrics are measured, rates are never invented.

Run: python3 -m pytest skills/earn/gig/tests/test_storefront_metric_learning.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import storefront_direct as sd  # noqa: E402

ACCEPTED_AT = 1_786_000_000
WINDOW_DAYS = 14
ELIGIBLE_AT = ACCEPTED_AT + WINDOW_DAYS * 86400
SERVICE_ID = "91000002"


def analytics_file(tmp_path, samples):
    path = tmp_path / "analytics.jsonl"
    path.write_text("".join(
        json.dumps({"service_id": SERVICE_ID, "observed_at_epoch": epoch,
                    "metrics": {"purchases": {"status": status, "value": value}}}) + "\n"
        for epoch, value, status in samples
    ), encoding="utf-8")
    return path


def measure(metric, analytics_path, transcripts=Path("/nonexistent/reply-transcripts.jsonl")):
    return sd._measure_experiment_metric(
        metric, reply_transcripts=transcripts, analytics_path=analytics_path,
        service_id=SERVICE_ID, accepted_at=ACCEPTED_AT, window_days=WINDOW_DAYS,
    )


def test_official_purchases_are_measured_as_a_snapshot_delta(tmp_path):
    path = analytics_file(tmp_path, [
        (ACCEPTED_AT - 100, 2, "known"),
        (ELIGIBLE_AT - 100, 5, "known"),
    ])
    result = measure("purchases", path)
    assert result["status"] == "known"
    assert (result["baseline"], result["observed"]) == (2, 5)
    # Never presented as a window-aligned count, because the official figure rolls 30 days.
    assert result["measurement"] == "rolling_30d_snapshot_delta"
    assert result["measured_metric"] == "purchases"


def test_a_view_based_ratio_is_never_invented_and_falls_back_to_a_measurable_metric(tmp_path):
    path = analytics_file(tmp_path, [
        (ACCEPTED_AT - 100, 0, "known"),
        (ELIGIBLE_AT - 100, 1, "known"),
    ])
    result = measure("views_to_purchase", path)
    assert result["requested_metric"] == "views_to_purchase"
    assert result["requested_metric_status"] == "unknown"
    assert result["requested_metric_reason"] == "official_views_are_rolling_30d_not_window_aligned"
    assert result["measured_metric"] == "purchases" and result["status"] == "known"


def test_missing_or_unknown_official_evidence_stays_unknown(tmp_path):
    assert measure("purchases", tmp_path / "absent.jsonl")["status"] == "unknown"
    only_baseline = analytics_file(tmp_path, [(ACCEPTED_AT - 100, 2, "known")])
    result = measure("purchases", only_baseline)
    assert result["status"] == "unknown"
    assert result["reason"] == "no_official_purchases_snapshot_inside_window"
    unknown_value = analytics_file(tmp_path, [
        (ACCEPTED_AT - 100, 0, "unavailable"), (ELIGIBLE_AT - 100, 0, "unavailable"),
    ])
    assert measure("purchases", unknown_value)["status"] == "unknown"


def test_an_unsupported_metric_is_reported_rather_than_guessed(tmp_path):
    result = measure("net_revenue_per_view", tmp_path / "absent.jsonl")
    assert result["status"] == "unknown" and result["reason"] == "unsupported_success_metric"
    assert result["measured_metric"] is None



def funnel_file(tmp_path, events):
    path = tmp_path / "funnel-events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


def test_net_receipt_counts_only_attributed_immutable_payments(tmp_path):
    path = funnel_file(tmp_path, [
        {"event_kind": "inquiry", "service_id": SERVICE_ID, "observed_at_epoch": ACCEPTED_AT - 2_000_000},
        {"event_kind": "payment", "service_id": SERVICE_ID,
         "observed_at_epoch": ACCEPTED_AT - 100, "net_receipt_jpy": 5000},
        {"event_kind": "payment", "service_id": SERVICE_ID,
         "observed_at_epoch": ACCEPTED_AT + 100, "net_receipt_jpy": 9000},
        {"event_kind": "payment", "service_id": "9999999",
         "observed_at_epoch": ACCEPTED_AT + 100, "net_receipt_jpy": 70000},
        {"event_kind": "payment", "service_id": SERVICE_ID,
         "observed_at_epoch": ACCEPTED_AT + 100},
    ])
    result = sd._measure_experiment_metric(
        "net_receipt", reply_transcripts=Path("/nonexistent"), analytics_path=Path("/nonexistent"),
        service_id=SERVICE_ID, accepted_at=ACCEPTED_AT, window_days=WINDOW_DAYS, funnel_path=path,
    )
    assert result["status"] == "known"
    # Another service's receipt and a receipt with no proven amount are both excluded.
    assert (result["baseline"], result["observed"]) == (5000.0, 9000.0)
    assert result["measurement"] == "window_aligned_receipt_sum"


def test_net_receipt_is_unknown_without_baseline_history_or_a_ledger(tmp_path):
    late = funnel_file(tmp_path, [
        {"event_kind": "payment", "service_id": SERVICE_ID,
         "observed_at_epoch": ACCEPTED_AT + 100, "net_receipt_jpy": 9000},
    ])
    assert sd._funnel_metric_windows(late, SERVICE_ID, ACCEPTED_AT, WINDOW_DAYS)["reason"] == (
        "funnel_history_does_not_cover_baseline")
    assert sd._measure_experiment_metric(
        "net_receipt", reply_transcripts=Path("/nonexistent"), analytics_path=Path("/nonexistent"),
        service_id=SERVICE_ID, accepted_at=ACCEPTED_AT, window_days=WINDOW_DAYS,
    )["reason"] == "funnel_events_not_supplied"



def test_official_quality_signals_separate_no_rating_from_zero():
    body = "評価  -\n販売実績 0件\n残り 1枠"
    signals = sd._extract_quality_signals(body)
    assert signals["rating"] == {"status": "unknown", "value": None,
                                 "reason": "official_page_shows_no_rating"}
    assert signals["lifetime_sales"] == {"status": "known", "value": 0}
    rated = sd._extract_quality_signals("評価 4.8\n販売実績 1,204件")
    assert rated["rating"] == {"status": "known", "value": 4.8}
    assert rated["lifetime_sales"] == {"status": "known", "value": 1204}
    missing = sd._extract_quality_signals("no official markers here")
    assert missing["rating"]["status"] == "unknown"
    assert missing["lifetime_sales"]["status"] == "unknown"


def test_catalog_baseline_binds_each_listing_version_to_its_latest_official_metrics(tmp_path):
    analytics = tmp_path / "analytics.jsonl"
    # 2026-08-28 4792e22c0 "fix(storefront): retain official metric baseline" tightened
    # _catalog_conversion_baseline to only trust rows explicitly marked `"official": True`
    # with fully known views/favorites/purchases -- a same-day response to a real ~4.5-hour
    # production incident (2026-08-27 18:33-22:56 UTC, 39 consecutive
    # storefront_catalog_baseline_incomplete wake failures in
    # ~/gig/storefront-direct/wakes.jsonl) caused by the original, looser version binding to
    # whichever row was merely newest regardless of source or completeness. This fixture
    # predates that hardening; mark rows official to match the contract the shipped fix
    # actually enforces.
    analytics.write_text("\n".join(json.dumps(row) for row in [
        {"service_id": "2", "observed_at_epoch": 10, "official": True,
         "metrics": {"views": {"status": "known", "value": 8},
                     "favorites": {"status": "known", "value": 1},
                     "purchases": {"status": "known", "value": 0}}},
        {"service_id": "1", "observed_at_epoch": 9, "official": True,
         "metrics": {"views": {"status": "known", "value": 3},
                     "favorites": {"status": "known", "value": 0},
                     "purchases": {"status": "known", "value": 0}}},
        {"service_id": "1", "observed_at_epoch": 11, "official": True,
         "metrics": {"views": {"status": "known", "value": 5},
                     "favorites": {"status": "known", "value": 0},
                     "purchases": {"status": "known", "value": 0}}},
    ]) + "\n", encoding="utf-8")
    contracts = [
        {"service_id": "2", "title": "AI agent", "category": "IT", "price_jpy": 30000,
         "state": "公開中", "service_version_sha256": "b" * 64},
        {"service_id": "1", "title": "Writing", "category": "文章", "price_jpy": 5000,
         "state": "公開中", "service_version_sha256": "a" * 64},
    ]

    baseline = sd._catalog_conversion_baseline(analytics, contracts)

    assert baseline["services"] == [
        {"service_id": "1", "title": "Writing", "category": "文章", "price_jpy": 5000,
         "state": "公開中", "service_version_sha256": "a" * 64,
         "views": 5, "favorites": 0, "purchases": 0, "observed_at_epoch": 11},
        {"service_id": "2", "title": "AI agent", "category": "IT", "price_jpy": 30000,
         "state": "公開中", "service_version_sha256": "b" * 64,
         "views": 8, "favorites": 1, "purchases": 0, "observed_at_epoch": 10},
    ]
    assert baseline["totals"] == {"services": 2, "views": 13, "favorites": 1, "purchases": 0}
    assert len(baseline["baseline_sha256"]) == 64


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
