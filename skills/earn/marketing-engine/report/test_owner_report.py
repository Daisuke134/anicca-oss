"""Contract tests for the canonical product-scoped owner reports.

The rows below are deliberately hand-derived fixtures.  Expectations in this
file use literal values rather than calling production helpers so that a
renderer which accidentally aggregates or borrows another product's state
cannot make the tests pass.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import owner_report


AS_OF = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.timezone.utc)
PRODUCTS = ("anicca-ios", "honne-ai", "ebook-ja", "ebook-en")
NATIVE_URL = "https://www.tiktok.com/@anicca/video/1000000000000000001"


FIXTURES: dict[str, list[dict]] = {
    "publication-identity.jsonl": [
        {
            "schema_version": 1,
            "product_id": "anicca-ios",
            "postiz_state": "PUBLISHED",
            "identity_status": "resolved",
            "postiz_post_id": "pub-anicca",
            "native_post_id": "1000000000000000001",
            "native_post_url": NATIVE_URL,
            "publish_date": "2026-08-05T08:00:00Z",
            "account_name": "anicca",
            "platform": "tiktok",
        },
        {
            "schema_version": 1,
            "product_id": "honne-ai",
            "postiz_state": "PUBLISHED",
            "identity_status": "resolved",
            "postiz_post_id": "pub-honne",
            "native_post_id": "1000000000000000002",
            "native_post_url": "https://www.tiktok.com/@honne/video/1000000000000000002",
            "publish_date": "2026-08-05T07:00:00Z",
            "account_name": "honne-ai",
            "platform": "tiktok",
        },
        {
            "schema_version": 1,
            "product_id": "ebook-ja",
            "postiz_state": "PUBLISHED",
            "identity_status": "resolved",
            "postiz_post_id": "pub-ebook-ja",
            "native_post_id": "1000000000000000003",
            "native_post_url": "https://www.tiktok.com/@ebook_ja/video/1000000000000000003",
            "publish_date": "2026-08-04T07:00:00Z",
            "account_name": "ebook_ja",
            "platform": "tiktok",
        },
        {
            "schema_version": 1,
            "product_id": "ebook-en",
            "postiz_state": "PUBLISHED",
            "identity_status": "resolved",
            "postiz_post_id": "pub-ebook-en",
            "native_post_id": "1000000000000000004",
            "native_post_url": "https://www.tiktok.com/@ebook_en/video/1000000000000000004",
            "publish_date": "2026-08-04T07:00:00Z",
            "account_name": "ebook_en",
            "platform": "tiktok",
        },
        # An unbound row is intentionally not eligible for a product report.
        {
            "schema_version": 1,
            "product_id": None,
            "postiz_state": "PUBLISHED",
            "identity_status": "resolved",
            "postiz_post_id": "legacy-unbound",
            "native_post_id": "999",
            "native_post_url": "https://example.invalid/legacy",
            "publish_date": "2026-08-05T09:00:00Z",
        },
    ],
    "publication-campaigns.jsonl": [
        {
            "schema_version": 1,
            "product_id": "anicca-ios",
            "publication_id": "pub-anicca",
            "campaign_token": "ca-anicca",
            "owned_url": "https://aniccaai.com/go/ca-anicca",
        },
        {
            "schema_version": 1,
            "product_id": "honne-ai",
            "publication_id": "pub-honne",
            "campaign_token": "ca-honne",
            "owned_url": "https://aniccaai.com/go/ca-honne",
        },
    ],
    "post-metrics.jsonl": [
        {
            "schema_version": 1,
            "product_id": "anicca-ios",
            "publication_id": "pub-anicca",
            "postiz_id": "pub-anicca",
            "native_url": NATIVE_URL,
            "native_post_id": "1000000000000000001",
            "platform": "tiktok",
            "checkpoint_status": "measured",
            "target_age_hours": 24,
            "observed_at": "2026-08-05T10:00:00Z",
            "views": 42,
            "impressions": 50,
            "reach": 38,
            "likes": 4,
            "comments": 2,
            "shares": 1,
            "saves": 3,
            "metric_null_reasons": {},
        },
        {
            "schema_version": 1,
            "product_id": "ebook-ja",
            "publication_id": "pub-ebook-ja",
            "postiz_id": "pub-ebook-ja",
            "native_url": "https://www.tiktok.com/@ebook_ja/video/1000000000000000003",
            "native_post_id": "1000000000000000003",
            "platform": "tiktok",
            "checkpoint_status": "missed",
            "target_age_hours": 24,
            "observed_at": "2026-08-05T10:00:00Z",
            "views": None,
            "impressions": None,
            "reach": None,
            "likes": None,
            "comments": None,
            "shares": None,
            "saves": None,
            "metric_null_reasons": {"views": "checkpoint_missed"},
            "error": "checkpoint_missed",
        },
    ],
    "business-outcomes.jsonl": [
        {
            "schema_version": 1,
            "product_id": "anicca-ios",
            "business_date": "2026-08-04",
            "observed_at": "2026-08-05T08:00:00Z",
            "snapshot_id": "anicca-ios:2026-08-04",
            "sources": {
                "revenuecat": {
                    "status": "available",
                    "reason": None,
                    "data": {
                        "charts": {
                            "mrr": {"latest_complete": {"MRR": {"value": 20.73}}},
                            "actives": {"latest_complete": {"Actives": {"value": 5}}},
                        }
                    },
                },
                "product_analytics": {
                    "status": "available",
                    "data": {"event_counts": {"app_opened": 3}},
                    "reason": None,
                },
            },
        },
        {
            "schema_version": 1,
            "product_id": "honne-ai",
            "business_date": "2026-08-04",
            "observed_at": "2026-08-05T08:00:00Z",
            "snapshot_id": "honne:2026-08-04",
            "sources": {
                "revenuecat": {
                    "status": "available",
                    "reason": None,
                    "data": {
                        "charts": {
                            "mrr": {"latest_complete": {"MRR": {"value": 0.0}}},
                            "actives": {"latest_complete": {"Actives": {"value": 0}}},
                        }
                    },
                },
                "product_analytics": {
                    "status": "unavailable",
                    "data": None,
                    "reason": "no_verified_readable_funnel",
                },
            },
        },
        {
            "schema_version": 1,
            "product_id": "ebook-ja",
            "business_date": "2026-08-04",
            "observed_at": "2026-08-05T08:00:00Z",
            "snapshot_id": "ebook-ja:2026-08-04",
            "sources": {
                "stripe": {
                    "status": "unavailable",
                    "data": None,
                    "reason": "stripe_product_not_configured",
                },
                "kdp": {
                    "status": "unavailable",
                    "data": None,
                    "reason": "kdp_not_authenticated",
                },
                "gumroad": {
                    "status": "unavailable",
                    "data": None,
                    "reason": "gumroad_not_configured",
                },
            },
        },
        {
            "schema_version": 1,
            "product_id": "ebook-en",
            "business_date": "2026-08-04",
            "observed_at": "2026-08-05T08:00:00Z",
            "snapshot_id": "ebook-en:2026-08-04",
            "sources": {
                "stripe": {
                    "status": "unavailable",
                    "data": None,
                    "reason": "stripe_product_not_configured",
                },
                "kdp": {
                    "status": "unavailable",
                    "data": None,
                    "reason": "kdp_not_authenticated",
                },
            },
        },
    ],
    "experiment-attribution.jsonl": [
        {
            "schema_version": "marketing.experiment-attribution.v1",
            "product_id": "ebook-ja",
            "experiment_id": "experiment.ebook-ja.001",
            "attribution_id": "attribution.ebook-ja.001",
            "native_post_url": "https://www.tiktok.com/@ebook_ja/video/1000000000000000003",
            "native_post_id": "1000000000000000003",
            "observed_at": "2026-08-05T10:00:00Z",
            "published_at": "2026-08-04T07:00:00Z",
            "results": [
                {
                    "metric_name": "views",
                    "status": "not_mature",
                    "value": None,
                    "null_reason": "social_checkpoint_not_mature",
                },
                {
                    "metric_name": "paid_orders",
                    "status": "not_mature",
                    "value": None,
                    "null_reason": "business_window_not_mature",
                },
            ],
        }
    ],
    "hook-perf.jsonl": [
        {
            "schema_version": "marketing.hook-performance.v1",
            "product_id": "ebook-ja",
            "renderer_id": "watercolor-monk",
            "status": "insufficient_data",
            "reason": "checkpoint_not_mature",
            "min_cohort": 10,
            "eligible_experiments": 0,
            "winners": [],
            "losers": [],
            "mutations": [],
            "observed_at": "2026-08-05T10:00:00Z",
        }
    ],
}


class OwnerReportRendererTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name, rows in FIXTURES.items():
            (self.root / name).write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                encoding="utf-8",
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def event(self, kind: str, product_id: str | None = None) -> dict:
        events = owner_report.build_events(
            self.root, kind, product_id=product_id, as_of=AS_OF
        )
        self.assertTrue(events, f"no {kind} event for {product_id}")
        return events[0]

    def test_legacy_rows_remain_visible_to_canonical_product_reports(self):
        rows = [
            (0, {"product_id": "aniccaios", "value": "old"}),
            (1, {"product_id": "anicca-ios", "value": "new"}),
            (2, {"product_id": "honne", "value": "other"}),
        ]
        scoped = owner_report._scoped(rows, "anicca-ios")
        self.assertEqual([row["value"] for _, row in scoped], ["old", "new"])

    def test_action_names_product_and_contains_exact_native_url(self):
        event = self.event("action", "anicca-ios")
        text = owner_report.render_japanese(event)
        self.assertEqual(event["product_id"], "anicca-ios")
        self.assertIn("anicca-ios", text)
        self.assertIn(NATIVE_URL, text)

    def test_action_replay_is_stable_when_attribution_snapshot_appended(self):
        first = self.event("action", "ebook-ja")
        store = owner_report.OwnerReportStore(
            self.root / "owner-reports.jsonl", self.root / "owner-report-deliveries.jsonl"
        )
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": [401]}

        first_receipt = owner_report.deliver(first, store, sender)

        rows = owner_report.load_jsonl(self.root / "experiment-attribution.jsonl")
        snapshot = json.loads(json.dumps(rows[0]))
        snapshot["attribution_id"] = "attribution.ebook-ja.002"
        snapshot["observed_at"] = "2026-08-05T11:00:00Z"
        snapshot["results"] = [
            {
                "metric_name": "views",
                "status": "not_mature",
                "value": None,
                "null_reason": "social_checkpoint_not_mature",
            }
        ]
        with (self.root / "experiment-attribution.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

        replay = self.event("action", "ebook-ja")
        self.assertEqual(replay["facts"], first["facts"])
        self.assertEqual(replay["evidence_refs"], first["evidence_refs"])
        replay_receipt = owner_report.deliver(replay, store, sender)
        self.assertEqual(first_receipt["message_ids"], [401])
        self.assertEqual(replay_receipt["message_ids"], [401])
        self.assertEqual(calls, [1])

    def test_action_replay_freezes_attribution_snapshot_when_identity_is_appended(self):
        native_id = "1000000000000000099"
        attribution = {
            "schema_version": "marketing.experiment-attribution.v1",
            "product_id": "honne-ai",
            "experiment_id": "experiment.honne.action-only",
            "attribution_id": "attribution.honne.action-only",
            "native_post_url": f"https://www.tiktok.com/@honne/video/{native_id}",
            "native_post_id": native_id,
            "observed_at": "2026-08-04T06:00:00Z",
            "published_at": "2026-08-04T05:00:00Z",
            "results": [],
        }
        with (self.root / "experiment-attribution.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(attribution, ensure_ascii=False) + "\n")

        first = next(
            event
            for event in owner_report.build_events(
                self.root, "action", product_id="honne-ai", as_of=AS_OF
            )
            if event["facts"]["native_post_id"] == native_id
        )
        store = owner_report.OwnerReportStore(
            self.root / "owner-reports.jsonl", self.root / "owner-report-deliveries.jsonl"
        )
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": [403]}

        first_receipt = owner_report.deliver(first, store, sender)

        identity = {
            "schema_version": 1,
            "product_id": "honne-ai",
            "postiz_state": "PUBLISHED",
            "identity_status": "resolved",
            "postiz_post_id": "pub-honne-action-only",
            "native_post_id": native_id,
            "native_post_url": attribution["native_post_url"],
            "publish_date": "2026-08-04T05:00:00Z",
            "account_name": "honne-ai",
            "platform": "tiktok",
        }
        with (self.root / "publication-identity.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(identity, ensure_ascii=False) + "\n")

        replay = next(
            event
            for event in owner_report.build_events(
                self.root, "action", product_id="honne-ai", as_of=AS_OF
            )
            if event["facts"]["native_post_id"] == native_id
        )
        self.assertEqual(replay, first)
        replay_receipt = owner_report.deliver(replay, store, sender)
        self.assertEqual(first_receipt["message_ids"], [403])
        self.assertEqual(replay_receipt["message_ids"], [403])
        self.assertEqual(calls, [1])

    def test_checkpoint_uses_exact_metric_values_and_natural_null_reason(self):
        measured = self.event("checkpoint", "anicca-ios")
        measured_text = owner_report.render_japanese(measured)
        self.assertIn("42", measured_text)
        self.assertIn("50", measured_text)

        self.assertEqual(
            owner_report.build_events(
                self.root, "checkpoint", product_id="ebook-ja", as_of=AS_OF
            ),
            [],
            "unmeasured checkpoints belong in one health incident, not checkpoint spam",
        )

    def test_bound_checkpoint_is_product_scoped_and_replays_delivery(self):
        native_url = "https://www.tiktok.com/@account/video/native-1"
        identity = {
            "schema_version": 1,
            "account_id": "tiktok.obou_anicca",
            "product_id": "ebook-ja",
            "product_id_null_reason": None,
            "product_binding_source": "account_manifest.publisher_integration_id",
            "postiz_state": "PUBLISHED",
            "identity_status": "resolved",
            "postiz_post_id": "post-1",
            "native_post_id": "native-1",
            "native_post_url": native_url,
            "publish_date": "2026-08-04T00:00:00Z",
            "account_name": "account",
            "platform": "tiktok",
        }
        metric = {
            "schema_version": 1,
            "product_id": "ebook-ja",
            "product_id_null_reason": None,
            "publication_id": "postiz:post-1",
            "postiz_id": "post-1",
            "native_url": native_url,
            "native_post_id": "native-1",
            "platform": "tiktok",
            "checkpoint_status": "measured",
            "target_age_hours": 24,
            "observed_at": "2026-08-05T10:00:00Z",
            "views": 42,
            "impressions": 50,
            "reach": 38,
            "likes": 4,
            "comments": 2,
            "shares": 1,
            "saves": 3,
            "metric_null_reasons": {},
        }
        with (self.root / "publication-identity.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(identity, ensure_ascii=False) + "\n")
        with (self.root / "post-metrics.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(metric, ensure_ascii=False) + "\n")

        events = owner_report.build_events(
            self.root, "checkpoint", product_id="ebook-ja", as_of=AS_OF
        )
        event = next(
            event
            for event in events
            if event["facts"]["publication_id"] == "postiz:post-1"
        )
        self.assertEqual(event["product_id"], "ebook-ja")
        self.assertEqual(
            event["message_key"], "checkpoint:ebook-ja:postiz:post-1:24"
        )
        self.assertEqual(event["facts"]["native_url"], native_url)
        self.assertEqual(event["facts"]["views"], 42)
        self.assertEqual(event["facts"]["impressions"], 50)
        self.assertIsNone(event["facts"]["reason"])
        self.assertFalse(
            any(
                candidate["facts"]["publication_id"] == "postiz:post-1"
                for candidate in owner_report.build_events(
                    self.root, "checkpoint", product_id="anicca-ios", as_of=AS_OF
                )
            )
        )

        store = owner_report.OwnerReportStore(
            self.root / "owner-reports.jsonl", self.root / "owner-report-deliveries.jsonl"
        )
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": [777]}

        first = owner_report.deliver(event, store, sender)
        replay = next(
            candidate
            for candidate in owner_report.build_events(
                self.root, "checkpoint", product_id="ebook-ja", as_of=AS_OF
            )
            if candidate["message_key"] == event["message_key"]
        )
        second = owner_report.deliver(replay, store, sender)
        self.assertEqual(first["message_ids"], [777])
        self.assertEqual(second["message_ids"], [777])
        self.assertEqual(calls, [1])

    def test_product_daily_never_borrows_another_products_money(self):
        anicca = owner_report.render_japanese(self.event("product_daily", "anicca-ios"))
        honne = owner_report.render_japanese(self.event("product_daily", "honne-ai"))
        ebook = owner_report.render_japanese(self.event("product_daily", "ebook-ja"))
        self.assertIn("20.73", anicca)
        self.assertNotIn("0.0", anicca)
        self.assertIn("0.0", honne)
        self.assertNotIn("20.73", honne)
        self.assertIn("取得できませんでした", ebook)
        self.assertNotIn("20.73", ebook)

    def test_incident_names_failed_source_and_next_repair(self):
        event = self.event("incident", "ebook-ja")
        text = owner_report.render_japanese(event)
        self.assertIn("kdp", text.lower())
        self.assertIn("KDP", text)
        self.assertIn("認証", text)

    def test_social_checkpoint_failures_aggregate_once_per_product_platform_day(self):
        metrics_path = self.root / "post-metrics.jsonl"
        rows = owner_report.load_jsonl(metrics_path)
        rows.extend(
            {
                "schema_version": 1,
                "snapshot_id": f"missed-{age}",
                "product_id": "ebook-ja",
                "publication_id": "postiz:another",
                "postiz_id": "another",
                "native_url": "https://www.tiktok.com/@account/video/another",
                "native_post_id": "another",
                "platform": "tiktok",
                "checkpoint_status": "missed",
                "target_age_hours": age,
                "observed_at": "2026-08-05T10:30:00Z",
                "views": None,
                "metric_null_reasons": {"views": "checkpoint_missed"},
                "error": "checkpoint_missed",
            }
            for age in (6, 24)
        )
        metrics_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

        social = [
            event
            for event in owner_report.build_events(
                self.root, "incident", product_id="ebook-ja", as_of=AS_OF
            )
            if event["facts"].get("source") == "social_measurement"
        ]
        self.assertEqual(len(social), 1)
        self.assertEqual(
            social[0]["message_key"],
            "measurement_unhealthy:ebook-ja:tiktok:2026-08-05",
        )
        self.assertEqual(social[0]["facts"]["affected_checkpoints"], 3)

    def test_measured_correction_removes_resolved_checkpoint_from_health_incident(self):
        metrics_path = self.root / "post-metrics.jsonl"
        rows = owner_report.load_jsonl(metrics_path)
        missed = next(row for row in rows if row.get("product_id") == "ebook-ja")
        missed["snapshot_id"] = "missed-original"
        correction = dict(
            missed,
            snapshot_id="measured-correction",
            corrects_snapshot_id="missed-original",
            checkpoint_status="measured",
            observed_at="2026-08-05T11:00:00Z",
            views=0,
            impressions=0,
            likes=0,
            comments=0,
            shares=0,
            saves=0,
            metric_null_reasons={"reach": "provider_field_missing"},
            error=None,
        )
        rows.append(correction)
        metrics_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

        social = [
            event
            for event in owner_report.build_events(
                self.root, "incident", product_id="ebook-ja", as_of=AS_OF
            )
            if event["facts"].get("source") == "social_measurement"
        ]
        self.assertEqual(social, [])
        checkpoint = owner_report.build_events(
            self.root, "checkpoint", product_id="ebook-ja", as_of=AS_OF
        )
        self.assertEqual(len(checkpoint), 1)
        self.assertEqual(checkpoint[0]["facts"]["views"], 0)

    def test_daily_social_incident_delivery_replays_once_when_more_failures_arrive(self):
        initial = next(
            event
            for event in owner_report.build_events(
                self.root, "incident", product_id="ebook-ja", as_of=AS_OF
            )
            if event["facts"].get("source") == "social_measurement"
        )
        store = owner_report.OwnerReportStore(
            self.root / "owner-reports.jsonl", self.root / "owner-report-deliveries.jsonl"
        )
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": [950]}

        first = owner_report.deliver(initial, store, sender)
        later_row = {
            **owner_report.load_jsonl(self.root / "post-metrics.jsonl")[-1],
            "snapshot_id": "later-failure",
            "publication_id": "postiz:later",
            "postiz_id": "later",
            "target_age_hours": 6,
            "observed_at": "2026-08-05T11:00:00Z",
        }
        with (self.root / "post-metrics.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(later_row, ensure_ascii=False) + "\n")

        replay = next(
            event
            for event in owner_report.build_events(
                self.root, "incident", product_id="ebook-ja", as_of=AS_OF
            )
            if event["facts"].get("source") == "social_measurement"
        )
        second = owner_report.deliver(replay, store, sender)
        self.assertEqual(replay["message_key"], initial["message_key"])
        self.assertEqual(replay["facts"], initial["facts"])
        self.assertEqual(first["message_ids"], [950])
        self.assertEqual(second["message_ids"], [950])
        self.assertEqual(calls, [1])

    def test_incident_uses_latest_snapshot_and_aggregates_current_gaps(self):
        """Historical snapshots must not replay stale gaps into a sweep."""

        business_rows = []
        for offset, business_date in enumerate(
            [
                "2026-07-30",
                "2026-07-31",
                "2026-08-01",
                "2026-08-02",
                "2026-08-03",
                "2026-08-04",
            ]
        ):
            if offset == 5:
                sources = {
                    "gumroad": {
                        "status": "failed",
                        "reason": "gumroad_current_failure",
                        "data": None,
                    },
                    "kdp": {
                        "status": "unavailable",
                        "reason": "kdp_current_gap",
                        "data": None,
                    },
                    "stripe": {
                        "status": "error",
                        "reason": "stripe_current_error",
                        "data": None,
                    },
                }
            else:
                sources = {
                    "kdp": {
                        "status": "unavailable",
                        "reason": f"kdp_historical_gap_{offset}",
                        "data": None,
                    }
                }
            business_rows.append(
                {
                    "schema_version": 1,
                    "product_id": "ebook-ja",
                    "business_date": business_date,
                    "observed_at": f"{business_date}T08:00:00Z",
                    "snapshot_id": f"ebook-ja:{business_date}",
                    "sources": sources,
                }
            )

        (self.root / "business-outcomes.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in business_rows),
            encoding="utf-8",
        )
        events = owner_report.build_events(
            self.root, "incident", product_id="ebook-ja", as_of=AS_OF
        )

        business_events = [event for event in events if "source_gaps" in event["facts"]]
        self.assertEqual(len(business_events), 1)
        event = business_events[0]
        self.assertEqual(event["facts"]["business_date"], "2026-08-04")
        self.assertEqual(event["facts"]["snapshot_id"], "ebook-ja:2026-08-04")
        self.assertEqual(
            event["facts"]["source_gaps"],
            [
                {
                    "source": "kdp",
                    "reason": "kdp_current_gap",
                    "next_repair": "KDP認証を設定して再取得する",
                },
                {
                    "source": "stripe",
                    "reason": "stripe_current_error",
                    "next_repair": "Stripeの商品ID設定を確認して再取得する",
                },
                {
                    "source": "gumroad",
                    "reason": "gumroad_current_failure",
                    "next_repair": "Gumroadの読み取り設定を確認して再取得する",
                },
            ],
        )
        self.assertEqual(
            event["evidence_refs"], ["state/business-outcomes.jsonl#row-5"]
        )
        text = owner_report.render_japanese(event)
        self.assertIn("KDP", text)
        self.assertIn("Stripe", text)
        self.assertIn("Gumroad", text)
        self.assertIn("現在の証拠では分かりません", text)
        self.assertNotIn("historical_gap", text)
        self.assertNotIn("2026-08-03", text)

        replay = owner_report.build_events(
            self.root,
            "incident",
            product_id="ebook-ja",
            as_of=AS_OF + dt.timedelta(hours=1),
        )
        replay = next(event for event in replay if "source_gaps" in event["facts"])
        self.assertEqual(event["message_key"], replay["message_key"])

        changed_rows = json.loads(json.dumps(business_rows))
        changed_rows[-1]["sources"]["stripe"]["reason"] = "stripe_changed_error"
        (self.root / "business-outcomes.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in changed_rows),
            encoding="utf-8",
        )
        changed = owner_report.build_events(
            self.root, "incident", product_id="ebook-ja", as_of=AS_OF
        )
        changed = next(event for event in changed if "source_gaps" in event["facts"])
        self.assertNotEqual(event["message_key"], changed["message_key"])

    def test_experiment_does_not_call_not_mature_winner_or_loser(self):
        event = self.event("experiment", "ebook-ja")
        text = owner_report.render_japanese(event)
        self.assertIn("まだ判断できる時間ではありません", text)
        self.assertNotIn("winner", text.lower())
        self.assertNotIn("loser", text.lower())
        self.assertNotIn("勝者", text)
        self.assertNotIn("敗者", text)

    def test_experiment_transition_uses_attribution_snapshot_key_and_replays(self):
        initial = self.event("experiment", "ebook-ja")
        rows = owner_report.load_jsonl(self.root / "experiment-attribution.jsonl")
        snapshot = json.loads(json.dumps(rows[0]))
        snapshot["attribution_id"] = "attribution.ebook-ja.002"
        snapshot["observed_at"] = "2026-08-05T11:00:00Z"
        snapshot["results"] = [
            {
                "metric_name": "views",
                "status": "observed",
                "value": 128,
                "null_reason": None,
            }
        ]
        with (self.root / "experiment-attribution.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

        events = owner_report.build_events(
            self.root, "experiment", product_id="ebook-ja", as_of=AS_OF
        )
        self.assertEqual(len(events), 2)
        transition = events[-1]
        self.assertNotEqual(initial["message_key"], transition["message_key"])
        self.assertIn(snapshot["attribution_id"], transition["message_key"])
        self.assertEqual(transition["facts"]["status"], "observed")

        store = owner_report.OwnerReportStore(
            self.root / "owner-reports.jsonl", self.root / "owner-report-deliveries.jsonl"
        )
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": [402 + len(calls)]}

        owner_report.deliver(initial, store, sender)
        transition_receipt = owner_report.deliver(transition, store, sender)
        replay_receipt = owner_report.deliver(transition, store, sender)
        self.assertEqual(transition_receipt["message_ids"], [404])
        self.assertEqual(replay_receipt["message_ids"], [404])
        self.assertEqual(calls, [1, 1])

    def test_experiment_legacy_snapshot_reuses_receipt_and_new_snapshot_delivers_once(self):
        rows = owner_report.load_jsonl(self.root / "experiment-attribution.jsonl")
        rows[0]["experiment_id"] = "experiment.preview-gate12"
        (self.root / "experiment-attribution.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

        generated = self.event("experiment", "ebook-ja")
        legacy_key = "experiment:ebook-ja:experiment.preview-gate12"
        legacy = json.loads(json.dumps(generated))
        legacy["message_key"] = legacy_key
        store = owner_report.OwnerReportStore(
            self.root / "owner-reports.jsonl", self.root / "owner-report-deliveries.jsonl"
        )
        store.record(legacy)
        store.claim_delivery(legacy_key)
        store.record_delivery(legacy_key, {"status": "delivered", "message_ids": [6926]})
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": [405 + len(calls)]}

        initial = self.event("experiment", "ebook-ja")
        self.assertEqual(initial["message_key"], legacy_key)
        initial_receipt = owner_report.deliver(initial, store, sender)
        self.assertEqual(initial_receipt["message_ids"], [6926])
        self.assertEqual(calls, [])

        snapshot = json.loads(json.dumps(rows[0]))
        snapshot["attribution_id"] = "attribution.ebook-ja.002"
        snapshot["observed_at"] = "2026-08-05T11:00:00Z"
        snapshot["results"] = [
            {
                "metric_name": "views",
                "status": "observed",
                "value": 128,
                "null_reason": None,
            }
        ]
        with (self.root / "experiment-attribution.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

        events = owner_report.build_events(
            self.root, "experiment", product_id="ebook-ja", as_of=AS_OF
        )
        transition = next(
            event for event in events if event["facts"]["attribution_id"] == snapshot["attribution_id"]
        )
        self.assertNotEqual(transition["message_key"], legacy_key)
        self.assertIn(snapshot["attribution_id"], transition["message_key"])
        transition_receipt = owner_report.deliver(transition, store, sender)
        replay_receipt = owner_report.deliver(transition, store, sender)
        self.assertEqual(transition_receipt["message_ids"], [406])
        self.assertEqual(replay_receipt["message_ids"], [406])
        self.assertEqual(calls, [1])

    def test_portfolio_weekly_contains_each_product_once(self):
        event = self.event("portfolio_weekly")
        text = owner_report.render_japanese(event)
        for product_id in PRODUCTS:
            self.assertEqual(text.count(product_id), 1, product_id)

    def test_rendered_numbers_equal_literal_fixture_facts(self):
        anicca = owner_report.render_japanese(self.event("product_daily", "anicca-ios"))
        honne = owner_report.render_japanese(self.event("product_daily", "honne-ai"))
        checkpoint = owner_report.render_japanese(self.event("checkpoint", "anicca-ios"))
        self.assertIn("20.73", anicca)
        self.assertIn("0.0", honne)
        self.assertIn("42", checkpoint)
        self.assertIn("50", checkpoint)

    def test_openclaw_legacy_state_is_never_read(self):
        legacy = self.root / ".openclaw" / "state" / "content-library"
        legacy.mkdir(parents=True)
        (legacy / "daily-metrics.jsonl").write_text(
            '{"revenuecat":{"mrr":999999.0}}\n', encoding="utf-8"
        )
        text = owner_report.render_japanese(self.event("product_daily", "anicca-ios"))
        self.assertNotIn("999999", text)

    def test_equivalent_replay_records_and_sends_once(self):
        event = self.event("product_daily", "anicca-ios")
        report_path, delivery_path = self.root / "owner-reports.jsonl", self.root / "owner-report-deliveries.jsonl"
        store = owner_report.OwnerReportStore(report_path, delivery_path)
        receipts = []

        def sender(_text: str) -> dict:
            receipts.append(1)
            return {"status": "delivered", "message_ids": [101]}

        first = owner_report.deliver(event, store, sender)
        second = owner_report.deliver(json.loads(json.dumps(event)), store, sender)
        self.assertEqual(first["message_ids"], [101])
        self.assertEqual(second["message_ids"], [101])
        self.assertEqual(receipts, [1])

    def test_conflicting_same_message_key_fails_closed(self):
        event = self.event("product_daily", "anicca-ios")
        store = owner_report.OwnerReportStore(self.root / "reports.jsonl", self.root / "deliveries.jsonl")
        store.record(event)
        conflict = json.loads(json.dumps(event))
        conflict["facts"]["mrr"] = 999.0
        with self.assertRaises(Exception):
            store.record(conflict)

    def test_delivery_requires_real_message_ids(self):
        event = self.event("product_daily", "anicca-ios")
        store = owner_report.OwnerReportStore(self.root / "reports.jsonl", self.root / "deliveries.jsonl")
        receipt = owner_report.deliver(
            event, store, lambda _text: {"status": "delivered", "message_ids": []}
        )
        self.assertEqual(receipt["status"], "delivery_unknown")
        self.assertEqual(store.delivery_for(event["message_key"])["status"], "delivery_unknown")

    def test_cli_no_send_records_report_without_delivery(self):
        import owner_report_cli

        report_path = self.root / "owner-reports.jsonl"
        delivery_path = self.root / "owner-report-deliveries.jsonl"
        with mock.patch.object(owner_report_cli, "TelegramClient") as client:
            rc = owner_report_cli.main(
                [
                    "sweep",
                    "--kind",
                    "product_daily",
                    "--product-id",
                    "anicca-ios",
                    "--state-root",
                    str(self.root),
                    "--as-of",
                    "2026-08-05T12:00:00Z",
                    "--no-send",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertTrue(report_path.exists())
        self.assertFalse(delivery_path.exists())
        client.from_env.assert_not_called()

    def test_replay_different_as_of_keeps_semantic_key_and_sends_once(self):
        first_event = self.event("product_daily", "anicca-ios")
        later = AS_OF + dt.timedelta(days=1)
        second_event = owner_report.build_events(
            self.root, "product_daily", product_id="anicca-ios", as_of=later
        )[0]
        self.assertNotEqual(first_event["as_of"], second_event["as_of"])
        store = owner_report.OwnerReportStore(self.root / "reports.jsonl", self.root / "deliveries.jsonl")
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": [201]}

        first = owner_report.deliver(first_event, store, sender)
        second = owner_report.deliver(second_event, store, sender)
        self.assertEqual(first["message_ids"], [201])
        self.assertEqual(second["message_ids"], [201])
        self.assertEqual(len(calls), 1)

    def test_daily_legacy_facts_without_money_buckets_replay_after_upgrade(self):
        generated = self.event("product_daily", "anicca-ios")
        legacy = json.loads(json.dumps(generated))
        legacy["facts"].pop("money_buckets", None)
        store = owner_report.OwnerReportStore(
            self.root / "owner-reports.jsonl", self.root / "owner-report-deliveries.jsonl"
        )
        store.record(legacy)
        store.claim_delivery(legacy["message_key"])
        store.record_delivery(
            legacy["message_key"], {"status": "delivered", "message_ids": [501]}
        )
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": [502]}

        replay = self.event("product_daily", "anicca-ios")
        self.assertNotIn("money_buckets", replay["facts"])
        self.assertEqual(replay, legacy)
        receipt = owner_report.deliver(replay, store, sender)
        self.assertEqual(receipt["message_ids"], [501])
        self.assertEqual(calls, [])

    def test_portfolio_legacy_facts_without_money_buckets_replay_after_upgrade(self):
        generated = self.event("portfolio_weekly")
        legacy = json.loads(json.dumps(generated))
        for product in legacy["facts"]["products"]:
            product.pop("money_buckets", None)
        store = owner_report.OwnerReportStore(
            self.root / "owner-reports.jsonl", self.root / "owner-report-deliveries.jsonl"
        )
        store.record(legacy)
        store.claim_delivery(legacy["message_key"])
        store.record_delivery(
            legacy["message_key"], {"status": "delivered", "message_ids": [503]}
        )
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": [504]}

        replay = self.event("portfolio_weekly")
        self.assertTrue(
            all("money_buckets" not in product for product in replay["facts"]["products"])
        )
        self.assertEqual(replay, legacy)
        receipt = owner_report.deliver(replay, store, sender)
        self.assertEqual(receipt["message_ids"], [503])
        self.assertEqual(calls, [])

    def test_ebook_paid_orders_are_count_and_minor_money_is_currency(self):
        rows = owner_report.load_jsonl(self.root / "business-outcomes.jsonl")
        rows.append(
            {
                "schema_version": 1,
                "product_id": "ebook-en",
                "business_date": "2026-08-05",
                "observed_at": "2026-08-05T11:00:00Z",
                "snapshot_id": "ebook-en:2026-08-05",
                "sources": {
                    "stripe": {
                        "status": "available",
                        "reason": None,
                        "data": {
                            "paid_orders": 3,
                            "gross_minor": {"USD": 1500},
                            "net_minor": {"USD": 1234},
                        },
                    }
                },
            }
        )
        (self.root / "business-outcomes.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        event = owner_report.build_events(
            self.root, "product_daily", product_id="ebook-en", as_of=AS_OF
        )[0]
        text = owner_report.render_japanese(event)
        self.assertNotIn("money_buckets", event["facts"])
        self.assertIn("注文数 3件", text)
        self.assertIn("12.34 USD", text)
        self.assertNotIn("売上の確認値は3 USD", text)

    def test_ebook_multiple_stripe_currencies_preserve_each_minor_bucket(self):
        rows = owner_report.load_jsonl(self.root / "business-outcomes.jsonl")
        rows.append(
            {
                "schema_version": 1,
                "product_id": "ebook-en",
                "business_date": "2026-08-05",
                "observed_at": "2026-08-05T11:00:00Z",
                "snapshot_id": "ebook-en:2026-08-05:multi-currency",
                "sources": {
                    "stripe": {
                        "status": "available",
                        "reason": None,
                        "data": {
                            "paid_orders": 2,
                            "net_minor": {"USD": 1234, "JPY": 3160},
                        },
                    }
                },
            }
        )
        (self.root / "business-outcomes.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        event = owner_report.build_events(
            self.root, "product_daily", product_id="ebook-en", as_of=AS_OF
        )[0]
        text = owner_report.render_japanese(event)
        self.assertIsNone(event["facts"]["money_value"])
        self.assertEqual(
            event["facts"]["money_buckets"],
            [
                {"currency": "JPY", "metric": "net", "minor": 3160, "value": 3160},
                {"currency": "USD", "metric": "net", "minor": 1234, "value": 12.34},
            ],
        )
        self.assertIn("12.34 USD", text)
        self.assertIn("3160 JPY", text)
        self.assertIn("注文数 2件", text)
        self.assertNotIn("売上の確認値は3 USD", text)

    def test_invalid_delivered_receipt_is_durable_unknown_and_not_retried(self):
        event = self.event("product_daily", "anicca-ios")
        store = owner_report.OwnerReportStore(self.root / "reports.jsonl", self.root / "deliveries.jsonl")
        calls = []

        def sender(_text: str) -> dict:
            calls.append(1)
            return {"status": "delivered", "message_ids": []}

        first = owner_report.deliver(event, store, sender)
        second = owner_report.deliver(event, store, sender)
        self.assertEqual(first["status"], "delivery_unknown")
        self.assertEqual(second["status"], "delivery_unknown")
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.delivery_for(event["message_key"])["status"], "delivery_unknown")

    def test_concurrent_delivery_claim_sends_at_most_once(self):
        event = self.event("product_daily", "anicca-ios")
        store = owner_report.OwnerReportStore(self.root / "reports.jsonl", self.root / "deliveries.jsonl")
        calls = []
        start = threading.Barrier(2)

        def sender(_text: str) -> dict:
            calls.append(1)
            time.sleep(0.08)
            return {"status": "delivered", "message_ids": [301]}

        def run() -> dict:
            start.wait()
            return owner_report.deliver(event, store, sender)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(lambda _unused: run(), (1, 2))
        self.assertEqual(len(calls), 1)
        self.assertIn(first["status"], {"delivered", "delivery_unknown"})
        self.assertIn(second["status"], {"delivered", "delivery_unknown"})

    def test_missing_business_snapshot_emits_null_daily_event(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            events = owner_report.build_events(
                root, "product_daily", product_id="honne-ai", as_of=AS_OF
            )
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["facts"]["mrr"])
        self.assertEqual(events[0]["facts"]["money_reason"], "no_business_snapshot")
        self.assertIn("取得できませんでした", owner_report.render_japanese(events[0]))

    def test_named_reasons_are_natural_in_owner_facing_prose(self):
        checkpoint = self.event("checkpoint", "anicca-ios")
        checkpoint["facts"]["checkpoint_status"] = "unavailable"
        checkpoint["facts"]["views"] = None
        checkpoint["facts"]["reason"] = "social_checkpoint_not_mature"
        checkpoint_body = owner_report.render_japanese(checkpoint).split("確認情報", 1)[0]
        self.assertIn("まだ判断できる時間ではありません", checkpoint_body)
        self.assertNotIn("social_checkpoint_not_mature", checkpoint_body)

        daily = self.event("product_daily", "ebook-ja")
        daily["facts"]["money_reason"] = "kdp_not_authenticated"
        daily_body = owner_report.render_japanese(daily).split("確認情報", 1)[0]
        self.assertIn("取得できませんでした", daily_body)
        self.assertNotIn("kdp_not_authenticated", daily_body)

        incident = self.event("incident", "ebook-ja")
        incident["facts"]["reason"] = "missing_project_read_credential"
        incident_body = owner_report.render_japanese(incident).split("確認情報", 1)[0]
        self.assertIn("取得できませんでした", incident_body)
        self.assertNotIn("missing_project_read_credential", incident_body)

    def test_empty_evidence_refs_fail_closed(self):
        event = self.event("product_daily", "anicca-ios")
        event["evidence_refs"] = []
        with self.assertRaises(owner_report.OwnerReportError):
            owner_report.render_japanese(event)

    def test_minor_currency_exponents_preserve_jpy_usd_and_unknown(self):
        def money_event(currency: str, minor: int) -> tuple[dict, str]:
            with tempfile.TemporaryDirectory() as path:
                root = Path(path)
                row = {
                    "schema_version": 1,
                    "product_id": "ebook-ja",
                    "business_date": "2026-08-06",
                    "observed_at": "2026-08-06T08:00:00Z",
                    "snapshot_id": f"ebook-ja:2026-08-06:{currency}",
                    "sources": {
                        "stripe": {
                            "status": "available",
                            "reason": None,
                            "data": {
                                "paid_orders": 1,
                                "net_minor": {currency: minor},
                            },
                        }
                    },
                }
                (root / "business-outcomes.jsonl").write_text(
                    json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                event = owner_report.build_events(
                    root,
                    "product_daily",
                    product_id="ebook-ja",
                    as_of=dt.datetime(2026, 8, 6, 12, tzinfo=dt.timezone.utc),
                )[0]
                return event, owner_report.render_japanese(event)

        jpy_event, jpy_text = money_event("JPY", 3160)
        self.assertEqual(jpy_event["facts"]["money_value"], 3160)
        self.assertIn("3160 JPY", jpy_text)

        usd_event, usd_text = money_event("USD", 2073)
        self.assertEqual(usd_event["facts"]["money_value"], 20.73)
        self.assertIn("20.73 USD", usd_text)

        unknown_event, unknown_text = money_event("XYZ", 2073)
        self.assertIsNone(unknown_event["facts"]["money_value"])
        self.assertEqual(unknown_event["facts"]["money_minor"], 2073)
        self.assertEqual(unknown_event["facts"]["money_reason"], "unknown_currency_exponent")
        self.assertIn("2073", unknown_text)
        self.assertIn("XYZ", unknown_text)
        self.assertIn("最小単位", unknown_text)

    def test_portfolio_unknown_currency_preserves_minor_units(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            row = {
                "schema_version": 1,
                "product_id": "ebook-ja",
                "business_date": "2026-08-06",
                "observed_at": "2026-08-06T08:00:00Z",
                "snapshot_id": "ebook-ja:2026-08-06:XYZ",
                "sources": {
                    "stripe": {
                        "status": "available",
                        "reason": None,
                        "data": {"paid_orders": 1, "net_minor": {"XYZ": 2073}},
                    }
                },
            }
            (root / "business-outcomes.jsonl").write_text(
                json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            event = owner_report.build_events(
                root,
                "portfolio_weekly",
                product_id=None,
                as_of=dt.datetime(2026, 8, 6, 12, tzinfo=dt.timezone.utc),
            )[0]
            text = owner_report.render_japanese(event)
        ebook_line = next(line for line in text.splitlines() if line.startswith("ebook-ja:"))
        self.assertIn("2073 XYZ", ebook_line)
        self.assertIn("最小単位", ebook_line)
        self.assertNotIn("売上額は取得できませんでした", ebook_line)


if __name__ == "__main__":
    unittest.main()
