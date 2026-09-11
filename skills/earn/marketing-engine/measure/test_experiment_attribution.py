from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("experiment_attribution.py")
SPEC = importlib.util.spec_from_file_location("experiment_attribution", MODULE_PATH)
assert SPEC and SPEC.loader
ea = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ea
SPEC.loader.exec_module(ea)


def fixture_intent():
    return {
        "schema_version": "marketing.publication-intent.v2",
        "publish_key": "publication.abc",
        "experiment_id": "experiment.one",
        "creative_id": "creative.one",
        "product_id": "ebook-ja",
        "account_id": "tiktok.obou_anicca",
        "hook_id": "hook.one",
        "renderer_id": "watercolor",
        "attribution_token": "ej_token",
        "scheduled_at": "2026-08-01T20:15:00Z",
    }


def fixture_identity():
    return {
        "experiment_id": "experiment.one",
        "postiz_post_id": "postiz-1",
        "native_post_id": "native-1",
        "native_post_url": "https://example.test/native-1",
        "publish_date": "2026-08-01T20:15:00Z",
        "identity_status": "resolved",
    }


class AttributionContractTest(unittest.TestCase):
    def test_complete_metric_set_and_null_reasons(self):
        snapshot = ea.build_snapshot(
            intent=fixture_intent(), identity=fixture_identity(),
            post_metric=None,
            click_query={"status": "unavailable", "reason": "query_failed",
                         "evidence_refs": ["evidence/click.json"]},
            business_snapshot=None,
            observed_at="2026-08-01T21:15:00Z",
        )
        self.assertEqual(set(ea.REQUIRED_METRICS), {
            "impressions", "views", "qualified_clicks", "first_time_downloads",
            "installs", "trials", "paid_orders", "refunds", "gross_revenue",
            "net_revenue",
        })
        self.assertEqual({row["metric_name"] for row in snapshot["results"]},
                         set(ea.REQUIRED_METRICS))
        for row in snapshot["results"]:
            self.assertIn(row["attribution_class"], ea.ATTRIBUTION_CLASSES)
            self.assertIsNotNone(row["null_reason"])
            self.assertIsNone(row["value"])
        ea.validate_snapshot(snapshot)

    def test_exact_social_and_click_receipts_are_deterministic(self):
        post_metric = {
            "experiment_id": "experiment.one", "native_post_id": "native-1",
            "checkpoint_status": "observed", "observed_at": "2026-08-02T02:20:00Z",
            "views": 123, "impressions": 150,
            "raw_evidence_hash": "a" * 64,
        }
        click = {
            "status": "available", "product_id": "ebook-ja",
            "campaign_token": "ej_token", "count": 0,
            "observed_at": "2026-08-02T02:20:00Z",
            "evidence_refs": ["evidence/click.json"],
        }
        snapshot = ea.build_snapshot(
            intent=fixture_intent(), identity=fixture_identity(),
            post_metric=post_metric, click_query=click, business_snapshot=None,
            observed_at="2026-08-02T02:20:00Z",
        )
        by_name = {row["metric_name"]: row for row in snapshot["results"]}
        self.assertEqual((by_name["views"]["value"], by_name["views"]["attribution_class"]),
                         (123, "deterministic"))
        self.assertEqual((by_name["qualified_clicks"]["value"],
                          by_name["qualified_clicks"]["attribution_class"]),
                         (0, "deterministic"))

    def test_wrong_identity_or_click_scope_fails_closed(self):
        bad_identity = {**fixture_identity(), "native_post_id": "wrong"}
        post_metric = {"experiment_id": "experiment.one", "native_post_id": "native-1",
                       "checkpoint_status": "observed", "views": 1, "impressions": 1,
                       "observed_at": "2026-08-02T02:20:00Z"}
        with self.assertRaisesRegex(ValueError, "native identity mismatch"):
            ea.build_snapshot(intent=fixture_intent(), identity=bad_identity,
                              post_metric=post_metric, click_query=None,
                              business_snapshot=None,
                              observed_at="2026-08-02T02:20:00Z")
        with self.assertRaisesRegex(ValueError, "click scope mismatch"):
            ea.build_snapshot(
                intent=fixture_intent(), identity=fixture_identity(), post_metric=None,
                click_query={"status": "available", "product_id": "ebook-en",
                             "campaign_token": "ej_token", "count": 0,
                             "evidence_refs": ["e"]}, business_snapshot=None,
                observed_at="2026-08-02T02:20:00Z")

    def test_apple_campaign_is_aggregate_and_unscoped_stripe_is_unknown(self):
        app_intent = {**fixture_intent(), "product_id": "anicca-ios",
                      "attribution_token": "ai_token"}
        business = {
            "product_id": "anicca-ios", "business_date": "2026-08-01",
            "sources": {"app_store_connect": {"status": "available", "data": {
                "campaigns": {"ai_token": {"first_time_downloads": 7, "installs": 7,
                                             "evidence_ref": "evidence/asc.json"}}
            }}},
        }
        snapshot = ea.build_snapshot(
            intent=app_intent, identity=fixture_identity(), post_metric=None,
            click_query=None, business_snapshot=business,
            observed_at="2026-08-03T21:15:00Z")
        by_name = {row["metric_name"]: row for row in snapshot["results"]}
        self.assertEqual(by_name["first_time_downloads"]["attribution_class"],
                         "apple_aggregate")
        self.assertEqual(by_name["first_time_downloads"]["value"], 7)

        stripe = {"product_id": "ebook-ja", "business_date": "2026-08-01",
                  "sources": {"stripe": {"status": "available", "data": {
                      "paid_orders": 2, "gross_minor": {"jpy": 3160},
                      "refunded_minor": {}, "net_minor": {"jpy": 3160},
                      "evidence_ref": "evidence/stripe.json"}}}}
        ebook = ea.build_snapshot(
            intent=fixture_intent(), identity=fixture_identity(), post_metric=None,
            click_query=None, business_snapshot=stripe,
            observed_at="2026-08-03T21:15:00Z")
        paid = next(row for row in ebook["results"] if row["metric_name"] == "paid_orders")
        self.assertIsNone(paid["value"])
        self.assertEqual((paid["status"], paid["attribution_class"], paid["null_reason"]),
                         ("unknown", "unknown", "product_aggregate_not_publication_attributed"))

    def test_exact_token_checkout_is_deterministic_and_replay_is_idempotent(self):
        business = {"product_id": "ebook-ja", "business_date": "2026-08-01",
                    "sources": {"stripe": {"status": "available", "data": {
                        "campaigns": {"ej_token": {"paid_orders": 1, "refunds": 0,
                            "gross_revenue": 1580, "net_revenue": 1580,
                            "currency": "jpy", "evidence_ref": "evidence/stripe.json"}}
                    }}}}
        snapshot = ea.build_snapshot(
            intent=fixture_intent(), identity=fixture_identity(), post_metric=None,
            click_query=None, business_snapshot=business,
            observed_at="2026-08-03T21:15:00Z")
        paid = next(row for row in snapshot["results"] if row["metric_name"] == "paid_orders")
        self.assertEqual((paid["value"], paid["attribution_class"]), (1, "deterministic"))
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "attribution.jsonl"
            self.assertTrue(ea.append_snapshot(ledger, snapshot))
            self.assertFalse(ea.append_snapshot(ledger, snapshot))
            changed = json.loads(json.dumps(snapshot))
            changed["results"][0]["confidence"] = 0.5
            with self.assertRaisesRegex(ValueError, "conflicting attribution replay"):
                ea.append_snapshot(ledger, changed)

    def test_modeled_requires_declared_method_and_interval(self):
        result = ea.empty_result("installs", "count", "2026-08-01T20:15:00Z",
                                 "2026-08-03T21:15:00Z", "model")
        result.update({"status": "observed", "value": 4, "null_reason": None,
                       "attribution_class": "modeled", "confidence": 0.7})
        with self.assertRaisesRegex(ValueError, "modeled evidence"):
            ea.validate_result(result)
        result["model"] = {"method": "staggered_holdout", "baseline": 2,
                           "sample_size": 20, "interval": [1, 7]}
        ea.validate_result(result)

    def test_exact_ledger_selectors_reject_ambiguous_identity(self):
        identity = fixture_identity()
        self.assertEqual(ea.select_identity([identity], fixture_intent(), "postiz-1"),
                         identity)
        with self.assertRaisesRegex(ValueError, "exactly one publication identity"):
            ea.select_identity([identity, identity], fixture_intent(), "postiz-1")
        social = [
            {"experiment_id": "experiment.one", "native_post_id": "native-1",
             "observed_at": "2026-08-02T01:00:00Z", "views": 1},
            {"experiment_id": "experiment.one", "native_post_id": "native-1",
             "observed_at": "2026-08-02T02:00:00Z", "views": 2},
        ]
        self.assertEqual(ea.select_latest_post_metric(
            social, identity, "2026-08-02T01:30:00Z")["views"], 1)

    def test_click_query_uses_exact_scope_and_exact_count_header(self):
        calls = []

        def fetch(url, headers):
            calls.append((url, headers))
            return 200, {"content-range": "0-1/2"}, [
                {"receipt_id": "r1", "campaign_token": "ej_token",
                 "product_id": "ebook-ja", "clicked_at": "2026-08-02T00:00:00Z"},
                {"receipt_id": "r2", "campaign_token": "ej_token",
                 "product_id": "ebook-ja", "clicked_at": "2026-08-02T00:01:00Z"},
            ]

        result = ea.query_click_receipts(
            supabase_url="https://project.supabase.co", service_role_key="secret",
            product_id="ebook-ja", campaign_token="ej_token",
            observed_at="2026-08-02T02:20:00Z", fetch=fetch)
        self.assertEqual((result["status"], result["count"]), ("available", 2))
        self.assertIn("campaign_token=eq.ej_token", calls[0][0])
        self.assertIn("product_id=eq.ebook-ja", calls[0][0])
        self.assertEqual(calls[0][1]["Prefer"], "count=exact")
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
