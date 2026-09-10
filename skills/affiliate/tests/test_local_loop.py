import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
import sys
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from jsonschema import Draft202012Validator


SCRIPT = Path(__file__).parents[1] / "scripts" / "local_loop.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("affiliate_local_loop", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LocalLoopTest(unittest.TestCase):
    def test_distribution_plan_queues_one_content_preserving_child_job(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            control = {
                "schema_version": 1, "receipt_type": "AFFILIATE_X_DISTRIBUTION_JOB",
                "state": "QUEUED", "job_id": "a" * 64,
                "effect_identity": "b" * 64, "placement_id": "caption-en-1",
                "owned_article_url": "https://aniccaai.com/blog/caption",
                "content_sha256": "c" * 64,
                "experiment_lineage": {"kind": "BASE", "decision_id": None,
                                       "control_placement_id": None},
                "target_x_account": "selawmqt",
                "cadence_class": "AFFILIATE_MONETIZATION",
                "policy_sha256": "d" * 64, "source_set_sha256": "e" * 64,
                "created_at": "2026-08-24T00:00:00+00:00",
                "private_tracking_url_state": "NOT_INCLUDED",
                "revenue_credit_state": "NO_REVENUE_CREDIT",
            }
            MODULE.atomic_json(state / "x-distribution-jobs" / f'{"a" * 64}.json', control)
            MODULE.append(state / "x-distribution-jobs.jsonl", control)
            plan = {
                "state": "READY", "plan_id": "f" * 64,
                "experiment_id": "1" * 64, "decision_id": "2" * 64,
                "selected_variable": "distribution_mix",
                "control_placement_id": control["placement_id"],
                "control_job_id": control["job_id"],
                "control_content_sha256": control["content_sha256"],
                "control_post_url": "https://x.com/selawmqt/status/200",
                "next_action": "SAFE_X_RECIRCULATION",
                "content_mutation_allowed": False,
            }

            first = MODULE.create_x_recirculation_job(state, plan)
            replay = MODULE.create_x_recirculation_job(state, plan)

            self.assertEqual(first["state"], "QUEUED")
            self.assertTrue(first["changed"])
            self.assertEqual(first["content_sha256"], control["content_sha256"])
            self.assertEqual(first["owned_article_url"], control["owned_article_url"])
            self.assertNotEqual(first["placement_id"], control["placement_id"])
            self.assertEqual(first["distribution_mode"], "QUOTE_CONTROL_POST")
            self.assertEqual(first["control_post_url"], plan["control_post_url"])
            self.assertEqual(first["experiment_lineage"], {
                "kind": "EXPERIMENT", "decision_id": plan["decision_id"],
                "control_placement_id": control["placement_id"],
            })
            self.assertEqual(replay["state"], "ALREADY_QUEUED")
            self.assertFalse(replay["changed"])
            self.assertEqual(len(MODULE.json_rows(state / "x-distribution-jobs.jsonl")), 2)

    def test_distribution_mix_plan_preserves_content_and_dedupes(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            experiment = {
                "state": "ACTIVE", "experiment_id": "a" * 64,
                "decision_id": "b" * 64, "control_placement_id": "caption-en-1",
                "control_job_id": "c" * 64,
                "control_post_url": "https://x.com/selawmqt/status/200",
                "selected_variable": "distribution_mix",
                "official_success_metric": "Exact impressions increase from baseline 9.",
            }
            MODULE.atomic_json(state / "funnel-experiments" / "active.json", experiment)
            MODULE.atomic_json(state / "funnel-experiments" / "latest-exposure-gate.json", {
                "state": "WAITING_FOR_EXPOSURE", "experiment_id": experiment["experiment_id"],
                "distribution_required": True, "maximize_relevant_exposure": True,
            })
            MODULE.atomic_json(state / "x-distribution-jobs" / f'{"c" * 64}.json', {
                "job_id": "c" * 64, "placement_id": "caption-en-1",
                "content_sha256": "d" * 64, "target_x_account": "selawmqt",
                "landing_url": "https://example.com/caption",
            })
            MODULE.atomic_json(state / "devto-publications" / "caption-en.json", {
                "state": "LIVE", "placement_id": "caption-en-1",
                "public_url": "https://dev.to/a/caption",
            })
            MODULE.atomic_json(state / "substack-publications" / "caption-en.json", {
                "state": "LIVE", "placement_id": "caption-en-1",
                "public_url": "https://a.substack.com/p/caption",
            })
            MODULE.atomic_json(state / "x-posts" / "caption-en.json", {
                "state": "LIVE", "placement_id": "caption-en-1",
                "public_url": "https://x.com/selawmqt/status/199",
            })

            first = MODULE.materialize_distribution_mix_plan(state)
            replay = MODULE.materialize_distribution_mix_plan(state)

            self.assertEqual(first["state"], "READY")
            self.assertEqual(first["selected_variable"], "distribution_mix")
            self.assertEqual(first["control_content_sha256"], "d" * 64)
            self.assertEqual(first["next_action"], "SAFE_X_RECIRCULATION")
            self.assertEqual(set(first["live_surfaces"]), {"devto", "substack", "x"})
            self.assertTrue(first["maximize_relevant_exposure"])
            self.assertFalse(replay["changed"])
            self.assertEqual(len(MODULE.json_rows(
                state / "funnel-experiments" / "distribution-plans.jsonl"
            )), 1)

            next_funnel = {
                "transition_id": "9" * 64, "placement_id": "caption-en-1",
            }
            MODULE.atomic_json(state / "money-funnel" / "latest.json", next_funnel)
            next_decision = {
                "state": "READY", "decision_id": "8" * 64,
                "source_funnel_transition_id": next_funnel["transition_id"],
                "selected_variable": "distribution_mix", "bottleneck": "reach",
                "exposure_assessment": "insufficient",
                "action": "Publish one more quote without changing the offer.",
                "official_success_metric": "Exact impressions exceed 32.",
            }
            next_plan = MODULE.materialize_distribution_mix_plan(state, next_decision)
            self.assertNotEqual(next_plan["plan_id"], first["plan_id"])
            self.assertEqual(next_plan["decision_id"], next_decision["decision_id"])
            self.assertEqual(next_plan["decision_action"], next_decision["action"])
            self.assertEqual(len(MODULE.json_rows(
                state / "funnel-experiments" / "distribution-plans.jsonl"
            )), 2)

            MODULE.atomic_json(state / "funnel-experiments" / "active.json", {
                **experiment, "experiment_id": "e" * 64, "selected_variable": "hook",
            })
            with self.assertRaises(ValueError):
                MODULE.materialize_distribution_mix_plan(state)

    def test_exposure_gate_blocks_conversion_verdict_for_insufficient_reach(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            baseline = {
                "transition_id": "a" * 64,
                "impressions": {"count": 9, "state": "EXACT"},
                "transactions": {"count": 0, "state": "OBSERVED"},
            }
            MODULE.append(state / "money-funnel" / "rows.jsonl", baseline)
            MODULE.atomic_json(state / "money-funnel" / "latest.json", baseline)
            MODULE.atomic_json(state / "funnel-experiments" / "active.json", {
                "state": "ACTIVE", "experiment_id": "b" * 64,
                "decision_id": "c" * 64,
                "source_funnel_transition_id": baseline["transition_id"],
                "control_placement_id": "caption-en-1",
                "selected_variable": "distribution_mix",
                "exposure_assessment": "insufficient",
                "official_success_metric": "Exact impressions increase from baseline 9.",
                "observation_state": "OPEN",
            })

            first = MODULE.enforce_exposure_gate(state)
            replay = MODULE.enforce_exposure_gate(state)

            self.assertEqual(first["state"], "WAITING_FOR_EXPOSURE")
            self.assertFalse(first["conversion_verdict_allowed"])
            self.assertEqual(first["baseline_impressions"], 9)
            self.assertEqual(first["current_impressions"], 9)
            self.assertEqual(first["transactions_observed"], 0)
            self.assertEqual(first["transactions_verdict_state"], "NOT_JUDGED_INSUFFICIENT_EXPOSURE")
            self.assertFalse(replay["changed"])
            self.assertEqual(len(MODULE.json_rows(
                state / "funnel-experiments" / "exposure-gates.jsonl"
            )), 1)

    def test_funnel_experiment_lock_replays_same_and_blocks_sibling(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            funnel = {
                "transition_id": "a" * 64, "placement_id": "caption-en-1",
                "job_id": "1" * 64, "post_url": "https://x.com/selawmqt/status/200",
            }
            MODULE.atomic_json(state / "money-funnel" / "latest.json", funnel)
            decision = {
                "state": "READY", "decision_id": "b" * 64,
                "source_funnel_transition_id": funnel["transition_id"],
                "bottleneck": "reach", "exposure_assessment": "insufficient",
                "selected_variable": "distribution_mix",
                "hypothesis": "More reach is needed.",
                "action": "Use one more relevant channel.",
                "official_success_metric": "Exact impressions >= 100.",
            }

            first = MODULE.activate_funnel_experiment(state, decision)
            replay = MODULE.activate_funnel_experiment(state, decision)
            sibling = MODULE.activate_funnel_experiment(
                state, {**decision, "decision_id": "c" * 64},
            )

            self.assertEqual(first["state"], "ACTIVE")
            self.assertTrue(first["changed"])
            self.assertFalse(replay["changed"])
            self.assertEqual(sibling["state"], "BLOCKED_ACTIVE_EXPERIMENT")
            self.assertFalse(sibling["changed"])
            self.assertEqual(first["control_placement_id"], funnel["placement_id"])
            self.assertEqual(first["selected_variable"], "distribution_mix")
            self.assertEqual(len(MODULE.json_rows(
                state / "funnel-experiments" / "history.jsonl"
            )), 1)
            with self.assertRaises(ValueError):
                MODULE.activate_funnel_experiment(
                    state, {**decision, "source_funnel_transition_id": "d" * 64},
                )

    def test_money_funnel_row_preserves_missing_post_attribution_as_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            state, repost = Path(root) / "affiliate", Path(root) / "repost"
            placement = "caption-en-1"
            post_url = "https://x.com/selawmqt/status/200"
            MODULE.atomic_json(state / "x-growth" / "latest-channel-ledger.json", {
                "transition_id": "a" * 64,
                "lanes": {"monetization": {
                    "post_url": post_url, "job_id": "1" * 64,
                    "placement_id": placement,
                    "impressions": {"count": 5, "state": "EXACT"},
                    "replies": {"count": 0, "state": "EXACT"},
                    "reposts": {"count": 0, "state": "EXACT"},
                    "likes": {"count": 0, "state": "EXACT"},
                    "bookmarks": {"count": 0, "state": "EXACT"},
                }},
            })
            ledger = {"schema_version": 1, "receipt_type": "AFFILIATE_PLACEMENT_LEDGER",
                      "observed_at": "2026-08-24T00:00:00+00:00", "placements": [{
                "placement_id": placement,
                "provider_clicks": {"count": 3, "unique_count": 3,
                                    "observed_at": "2026-08-24T00:00:00+00:00"},
                "commission": {"transaction_count": 0, "status_counts": {
                    "pending": 0, "approved": 0, "paid": 0, "reversed": 0,
                }, "approved_or_paid_net_minor_by_currency": {}},
                "cost": {"actual_cash_state": "UNKNOWN",
                         "actual_cash_amount_by_currency": None},
            }]}
            ledger["ledger_sha256"] = hashlib.sha256(json.dumps(
                ledger, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            MODULE.atomic_json(state / "placement-ledger.json", ledger)
            repost.mkdir(parents=True)
            MODULE.append(repost / "posted.jsonl", {
                "kind": "affiliate_distribution_quote", "affiliate_job_id": "1" * 64,
                "affiliate_placement_id": placement, "post_url": post_url,
                "posted_at": "2026-08-24T01:00:00+00:00",
            })
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                first = MODULE.build_money_funnel_row(state)
                replay = MODULE.build_money_funnel_row(state)

            self.assertEqual(first["impressions"], {"count": 5, "state": "EXACT"})
            self.assertEqual(first["owned_entries"]["state"], "UNKNOWN_NOT_IN_COHORT")
            self.assertEqual(first["cta_clicks"]["state"], "UNKNOWN_NOT_IN_COHORT")
            self.assertEqual(first["provider_clicks"]["cumulative_count"], 3)
            self.assertEqual(
                first["provider_clicks"]["post_distribution_state"],
                "WAITING_FOR_POST_PROVIDER_READBACK",
            )
            self.assertEqual(first["transactions"], {"count": 0, "state": "OBSERVED"})
            self.assertEqual(first["approved_or_paid_money_state"], "NO_APPROVED_OR_PAID")
            self.assertEqual(first["cost"]["state"], "UNKNOWN")
            self.assertFalse(replay["changed"])
            self.assertEqual(len(MODULE.json_rows(
                state / "money-funnel" / "rows.jsonl"
            )), 1)

    def test_x_channel_ledger_separates_growth_and_monetization_with_follower_delta(self):
        with tempfile.TemporaryDirectory() as root:
            state, repost = Path(root) / "affiliate", Path(root) / "repost"
            repost.mkdir(parents=True)
            growth_url = "https://x.com/selawmqt/status/100"
            money_url = "https://x.com/selawmqt/status/200"
            MODULE.append(repost / "posted.jsonl", {
                "kind": "quote", "source_url": "https://x.com/source/status/1",
                "post_url": growth_url,
            })
            MODULE.append(repost / "posted.jsonl", {
                "kind": "affiliate_distribution", "affiliate_job_id": "1" * 64,
                "affiliate_placement_id": "caption-en-1",
                "affiliate_owned_article_url": "https://aniccaai.com/blog/caption",
                "source_url": "https://aniccaai.com/blog/caption", "post_url": money_url,
            })
            for count, transition in ((1, "a" * 64), (2, "b" * 64)):
                MODULE.append(state / "x-growth" / "follower-baselines.jsonl", {
                    "transition_id": transition, "followers": {"count": count, "state": "EXACT"},
                })
            MODULE.atomic_json(state / "x-growth" / "latest-post-metrics.json", {
                "transition_id": "c" * 64, "post_url": money_url,
                "job_id": "1" * 64, "placement_id": "caption-en-1",
                "impressions": {"count": 4, "state": "EXACT"},
                "replies": {"count": 0, "state": "EXACT"},
                "reposts": {"count": 0, "state": "EXACT"},
                "likes": {"count": 0, "state": "EXACT"},
                "bookmarks": {"count": 0, "state": "EXACT"},
            })
            for url, count in ((money_url, 6), ("https://x.com/selawmqt/status/201", 4)):
                MODULE.append(state / "x-growth" / "post-metrics.jsonl", {
                    "transition_id": hashlib.sha256(url.encode()).hexdigest(),
                    "post_url": url, "placement_id": "caption-en-1",
                    "impressions": {"count": count, "state": "EXACT"},
                    "replies": {"count": 0, "state": "EXACT"},
                    "reposts": {"count": 0, "state": "EXACT"},
                    "likes": {"count": 0, "state": "EXACT"},
                    "bookmarks": {"count": 0, "state": "EXACT"},
                })
            inspector = lambda *_args: {
                "views": 10, "replies": 1, "reposts": 0, "likes": 2, "bookmarks": 0,
            }
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                first = MODULE.observe_x_channel_ledger(state, 9326, inspector=inspector)
                replay = MODULE.observe_x_channel_ledger(state, 9326, inspector=inspector)

            self.assertEqual(first["followers_delta"], {"count": 1, "state": "EXACT"})
            self.assertEqual(first["lanes"]["growth"]["post_url"], growth_url)
            self.assertEqual(first["lanes"]["growth"]["impressions"]["count"], 10)
            self.assertEqual(first["lanes"]["monetization"]["post_url"], money_url)
            self.assertEqual(first["lanes"]["monetization"]["impressions"]["count"], 10)
            self.assertEqual(first["lanes"]["monetization"]["distribution_post_count"], 2)
            self.assertFalse(replay["changed"])
            self.assertEqual(len(MODULE.json_rows(
                state / "x-growth" / "channel-ledger.jsonl"
            )), 1)

    def test_x_post_metrics_append_only_when_exact_reach_changes(self):
        with tempfile.TemporaryDirectory() as root:
            state, repost = Path(root) / "affiliate", Path(root) / "repost"
            repost.mkdir(parents=True)
            post_url = "https://x.com/selawmqt/status/123"
            MODULE.append(repost / "posted.jsonl", {
                "kind": "affiliate_distribution_quote", "affiliate_job_id": "1" * 64,
                "affiliate_placement_id": "caption-en-1-mix-a",
                "affiliate_owned_article_url": "https://aniccaai.com/blog/caption",
                "source_url": "https://aniccaai.com/blog/caption", "post_url": post_url,
            })
            MODULE.atomic_json(state / "x-distribution-jobs" / f'{"1" * 64}.json', {
                "experiment_lineage": {
                    "kind": "EXPERIMENT", "decision_id": "2" * 64,
                    "control_placement_id": "caption-en-1",
                },
            })
            metrics = iter((
                {"views": 3, "replies": 0, "reposts": 0, "likes": 0, "bookmarks": 0},
                {"views": 3, "replies": 0, "reposts": 0, "likes": 0, "bookmarks": 0},
                {"views": 4, "replies": 0, "reposts": 0, "likes": 0, "bookmarks": 0},
            ))
            inspector = lambda *_args: next(metrics)
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                first = MODULE.observe_x_post_metrics(state, 9326, inspector=inspector)
                replay = MODULE.observe_x_post_metrics(state, 9326, inspector=inspector)
                changed = MODULE.observe_x_post_metrics(state, 9326, inspector=inspector)

            self.assertEqual(first["impressions"], {"count": 3, "state": "EXACT"})
            self.assertEqual(first["placement_id"], "caption-en-1")
            self.assertEqual(first["distribution_placement_id"], "caption-en-1-mix-a")
            self.assertTrue(first["changed"])
            self.assertFalse(replay["changed"])
            self.assertEqual(changed["impressions"]["count"], 4)
            self.assertEqual(len(MODULE.json_rows(
                state / "x-growth" / "post-metrics.jsonl"
            )), 2)
    def test_x_growth_baseline_appends_only_when_official_count_changes(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            profiles = iter((
                {"handle": "selawmqt", "owner": True, "rendered_url": "https://x.com/selawmqt",
                 "followers_text": "1 フォロワー", "following_text": "27 フォロー中"},
                {"handle": "selawmqt", "owner": True, "rendered_url": "https://x.com/selawmqt",
                 "followers_text": "1 フォロワー", "following_text": "27 フォロー中"},
                {"handle": "selawmqt", "owner": True, "rendered_url": "https://x.com/selawmqt",
                 "followers_text": "2 フォロワー", "following_text": "27 フォロー中"},
            ))
            inspector = lambda *_args: next(profiles)

            first = MODULE.observe_x_growth(state, 9326, inspector=inspector)
            replay = MODULE.observe_x_growth(state, 9326, inspector=inspector)
            changed = MODULE.observe_x_growth(state, 9326, inspector=inspector)

            self.assertEqual(first["followers"], {"count": 1, "state": "EXACT"})
            self.assertTrue(first["changed"])
            self.assertFalse(replay["changed"])
            self.assertTrue(changed["changed"])
            self.assertEqual(changed["followers"]["count"], 2)
            self.assertEqual(len(MODULE.json_rows(
                state / "x-growth" / "follower-baselines.jsonl"
            )), 2)

    def test_x_distribution_job_is_enqueued_once_after_policy_and_owned_readback(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            plan_id = "subtitle-en-experiment-abcdef123456"
            placement_id = f"{plan_id}-1"
            slug = "subtitle-experiment-abcdef123456"
            content_sha = "1" * 64
            experiment = {
                "decision_id": "2" * 64,
                "control_placement_id": "subtitle-en-1",
            }
            MODULE.atomic_json(state / "campaign-publications" / f"{plan_id}.json", {
                "state": "X_LIVE", "plan_id": plan_id, "placement_id": placement_id,
                "slug": slug, "owned_url": f"https://aniccaai.com/blog/{slug}",
                "content_sha256": content_sha, "experiment": experiment,
                "created_at": "2026-08-24T00:00:00+00:00",
            })
            MODULE.atomic_json(state / "placement-ledger.json", {"placements": [{
                "placement_id": placement_id, "provider_clicks": {"count": 0},
            }]})
            policy = {
                "receipt_type": "GENERIC_CAMPAIGN_POLICY", "state": "PASS",
                "decision": "PASS", "plan_id": plan_id,
                "source_set_sha256": "3" * 64,
                "checks": {"all": True},
                "semantic_audit": {"decision": "PASS", "unsupported_claims": []},
            }
            MODULE.atomic_json(state / "campaign-policy" / f"{plan_id}.json", policy)
            proposal = MODULE.create_repost_proposal(state)

            waiting = MODULE.create_x_distribution_job(state, proposal)
            self.assertEqual(waiting["state"], "WAITING_FOR_OWNED_READBACK")
            self.assertFalse((state / "x-distribution-jobs.jsonl").exists())

            MODULE.atomic_json(state / "owned-publications" / f"{slug}.json", {
                "state": "LIVE", "slug": slug, "content_sha256": content_sha,
                "public_url": f"https://aniccaai.com/blog/{slug}",
                "rendered_sha256": "4" * 64,
            })
            first = MODULE.create_x_distribution_job(state, proposal)
            second = MODULE.create_x_distribution_job(state, proposal)

            self.assertEqual(first["state"], "QUEUED")
            self.assertTrue(first["changed"])
            self.assertEqual(second["state"], "ALREADY_QUEUED")
            self.assertFalse(second["changed"])
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual(first["effect_identity"], second["effect_identity"])
            rows = MODULE.json_rows(state / "x-distribution-jobs.jsonl")
            self.assertEqual(rows, [{key: value for key, value in first.items()
                                     if key not in {"changed"}}])
            schema = json.loads((
                SCRIPT.parents[1] / "config" / "schemas"
                / "affiliate-x-distribution-job-v1.json"
            ).read_text())
            Draft202012Validator(schema).validate(rows[0])
            self.assertEqual(first["target_x_account"], "selawmqt")
            self.assertEqual(first["content_sha256"], content_sha)
            self.assertEqual(first["experiment_lineage"], {
                "kind": "EXPERIMENT", **experiment,
            })
            self.assertNotIn("try.elevenlabs.io", json.dumps(first))

    def test_funnel_snapshot_keeps_focused_live_experiment_below_rank_limit(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            base = "subtitle-en-1"
            child = "subtitle-en-experiment-abcdef123456-1"
            rows = []
            for placement_id, unique, count in ((base, 6, 7), (child, 2, 2)):
                rows.append({
                    "placement_id": placement_id, "public_url": f"https://example/{placement_id}",
                    "provider_link_key": f"key-{placement_id}",
                    "provider_clicks": {"unique_count": unique, "count": count,
                                        "unique_state": "OBSERVED"},
                    "exposure": {}, "commission": {"transaction_count": 0},
                })
            MODULE.atomic_json(state / "placement-ledger.json", {
                "ledger_sha256": "a" * 64, "placements": rows,
            })
            MODULE.atomic_json(state / "focused-cohort" / "latest.json", {
                "placement_id": base,
            })
            for placement_id, experiment in (
                (base, None),
                (child, {"control_placement_id": base, "decision_id": "b" * 64}),
            ):
                plan_id = placement_id.removesuffix("-1")
                MODULE.atomic_json(state / "campaign-publications" / f"{plan_id}.json", {
                    "placement_id": placement_id, "plan_id": plan_id, "state": "X_LIVE",
                    "created_at": "2026-08-22T00:00:00+00:00", "experiment": experiment,
                })

            snapshot = MODULE.refresh_funnel_snapshot(state, limit=1)

            self.assertEqual(
                [row["placement_id"] for row in snapshot["placements"]], [base, child],
            )
            self.assertEqual(snapshot["limit"], 1)
            self.assertEqual(snapshot["focused_lineage_count"], 2)

    def test_invalid_campaign_metadata_remains_visible_after_live_campaign(self):
        self.assertEqual(
            MODULE.generic_publication_terminal_state(
                completed=True, invalid_metadata=True,
            ),
            "CAMPAIGN_METADATA_INVALID",
        )

    def test_focus_cohort_is_replay_safe_and_pauses_only_new_placements(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            rows = []
            for placement_id, unique, clicks in (
                ("subtitle-en-1", 6, 7), ("isolator-en-1", 6, 6),
            ):
                rows.append({
                    "placement_id": placement_id, "cta_clicks": 0,
                    "provider_click_delta": 0, "provider_unique_click_delta": 0,
                    "transaction_count": 0,
                })
                plan_id = placement_id.removesuffix("-1")
                MODULE.atomic_json(state / "campaign-publications" / f"{plan_id}.json", {
                    "placement_id": placement_id, "plan_id": plan_id, "state": "X_LIVE",
                })
                MODULE.atomic_json(state / "campaign-handoffs" / f"{plan_id}.json", {
                    "buyer_intent": f"Creators evaluating {plan_id} before paying",
                    "title": f"Should I buy {plan_id}?", "handoff_fingerprint": placement_id,
                })
            interval_core = {
                "schema_version": 1, "receipt_type": "AFFILIATE_INTERVAL_FUNNEL_JOIN",
                "placements": rows,
            }
            interval_hash = hashlib.sha256(json.dumps(
                interval_core, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            MODULE.atomic_json(state / "interval-funnel-joins" / "latest.json", {
                **interval_core, "receipt_sha256": interval_hash,
            })
            MODULE.atomic_json(state / "funnel-snapshots" / "latest.json", {
                "snapshot_sha256": "a" * 64,
                "placements": [
                    {"placement_id": "subtitle-en-1", "provider_clicks": {"unique_count": 6, "count": 7}},
                    {"placement_id": "isolator-en-1", "provider_clicks": {"unique_count": 6, "count": 6}},
                ],
            })

            first = MODULE.focus_cohort(state)
            second = MODULE.focus_cohort(state)

            self.assertEqual(first["placement_id"], "subtitle-en-1")
            self.assertEqual(first["money_state"], "NON_MONEY")
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            baseline = json.loads(next(
                (state / "distribution-baselines").glob("focused-*.json")
            ).read_text())
            self.assertEqual(
                baseline["required_success_metric"],
                "EXACT_PLACEMENT_OFFICIAL_TRANSACTION_COUNT",
            )
            self.assertFalse(MODULE.focused_publication_allowed(state, "new-en-1", {}))
            self.assertTrue(MODULE.focused_publication_allowed(
                state, "new-en-1", {"state": "OWNED_LIVE"},
            ))
            MODULE.atomic_json(state / "acquisition-decisions" / "baseline.json", {
                "state": "READY", "decision_id": "decision-1", "selected_variable": "cta",
                "success_metric": "official transaction_count >= 1",
                "next_campaign_instruction": "change only the CTA",
            })
            admitted = {"experiment": {
                "baseline_sha256": "baseline", "decision_id": "decision-1",
                "control_placement_id": "subtitle-en-1", "selected_variable": "cta",
                "success_metric": "official transaction_count >= 1",
                "instruction": "change only the CTA",
            }}
            self.assertTrue(MODULE.focused_publication_allowed(
                state, "subtitle-en-experiment-1", {}, admitted,
            ))

    def test_focus_cohort_follows_live_experiment_child_despite_lower_clicks(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            base = "subtitle-en-1"
            child = "subtitle-en-experiment-abcdef123456-1"
            rows = [
                {"placement_id": base, "cta_clicks": 0, "provider_click_delta": 0,
                 "provider_unique_click_delta": 0, "transaction_count": 0},
                {"placement_id": child, "cta_clicks": 0, "provider_click_delta": 0,
                 "provider_unique_click_delta": 0, "transaction_count": 0},
            ]
            interval_core = {
                "schema_version": 1, "receipt_type": "AFFILIATE_INTERVAL_FUNNEL_JOIN",
                "placements": rows,
            }
            interval_hash = hashlib.sha256(json.dumps(
                interval_core, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            MODULE.atomic_json(state / "interval-funnel-joins" / "latest.json", {
                **interval_core, "receipt_sha256": interval_hash,
            })
            MODULE.atomic_json(state / "funnel-snapshots" / "latest.json", {
                "snapshot_sha256": "a" * 64,
                "placements": [
                    {"placement_id": base, "provider_clicks": {
                        "unique_count": 6, "count": 7,
                    }},
                    {"placement_id": child, "provider_clicks": {
                        "unique_count": 2, "count": 2,
                    }},
                ],
            })
            for placement_id, created_at, experiment in (
                (base, "2026-08-20T00:00:00+00:00", None),
                (child, "2026-08-22T00:00:00+00:00", {
                    "control_placement_id": base,
                    "decision_id": "abcdef123456" + "0" * 52,
                    "selected_variable": "cta",
                    "success_metric": "official transaction_count >= 1",
                }),
            ):
                plan_id = placement_id.removesuffix("-1")
                MODULE.atomic_json(state / "campaign-publications" / f"{plan_id}.json", {
                    "placement_id": placement_id, "plan_id": plan_id, "state": "X_LIVE",
                    "created_at": created_at, "experiment": experiment,
                })
                MODULE.atomic_json(state / "campaign-handoffs" / f"{plan_id}.json", {
                    "buyer_intent": "Creators evaluating subtitles before paying",
                    "title": "Subtitle decision guide", "handoff_fingerprint": placement_id,
                    "experiment": experiment,
                })
            MODULE.atomic_json(state / "focused-cohort" / "latest.json", {
                "schema_version": 1, "receipt_type": "AFFILIATE_FOCUSED_COHORT",
                "placement_id": base, "plan_id": base.removesuffix("-1"),
                "buyer_problem": "Creators evaluating subtitles before paying",
                "decision_stage_query": "Subtitle decision guide",
                "handoff_fingerprint": base, "provider_unique_clicks": 6,
                "provider_clicks": 7, "receipt_sha256": "b" * 64,
                "source_interval_receipt_sha256": interval_hash,
                "source_snapshot_sha256": "a" * 64,
            })

            focus = MODULE.focus_cohort(state)

            self.assertEqual(focus["placement_id"], child)
            self.assertEqual(focus["control_placement_id"], base)
            self.assertEqual(focus["experiment_decision_id"], "abcdef123456" + "0" * 52)

    def test_funnel_snapshot_ranks_top_three_and_preserves_unknown_denominators(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            placements = []
            for placement_id, unique, clicks in (
                ("alpha-en-1", 2, 3), ("beta-en-1", 6, 6),
                ("gamma-en-1", 6, 7), ("delta-en-1", 1, 9),
            ):
                placements.append({
                    "placement_id": placement_id,
                    "public_url": f"https://aniccaai.com/blog/{placement_id}",
                    "provider_link_key": f"key-{placement_id}",
                    "exposure": {
                        "owned_page_visits": None,
                        "owned_page_visits_state": "UNKNOWN",
                    },
                    "provider_clicks": {
                        "count": clicks, "unique_count": unique,
                        "unique_state": "OBSERVED", "observed_at": "provider-time",
                    },
                    "commission": {"transaction_count": 0},
                })
                MODULE.atomic_json(
                    state / "campaign-publications" / f"{placement_id}.json",
                    {"placement_id": placement_id, "x_url": f"https://x.com/example/{placement_id}"},
                )
            MODULE.atomic_json(state / "placement-ledger.json", {
                "ledger_sha256": "a" * 64, "placements": placements,
            })

            first = MODULE.refresh_funnel_snapshot(state)
            second = MODULE.refresh_funnel_snapshot(state)

            self.assertEqual(
                [row["placement_id"] for row in first["placements"]],
                ["gamma-en-1", "beta-en-1", "alpha-en-1"],
            )
            self.assertEqual(first["placements"][0]["owned_visits"]["state"], "UNKNOWN")
            self.assertEqual(first["placements"][0]["cta_clicks"]["state"], "UNKNOWN")
            self.assertEqual(
                first["placements"][0]["customers"]["state"],
                "UNAVAILABLE_AT_EXACT_PLACEMENT",
            )
            self.assertEqual(first["placements"][0]["transactions"]["count"], 0)
            self.assertEqual(first["placements"][0]["money_state"], "NON_MONEY_UNTIL_APPROVED_OR_PAID")
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])

    def test_owned_visit_observation_records_disabled_analytics_as_unavailable(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.atomic_json(state / "funnel-snapshots" / "latest.json", {
                "snapshot_sha256": "a" * 64,
                "placements": [{
                    "placement_id": "alpha-en-1",
                    "owned_url": "https://aniccaai.com/blog/alpha",
                }],
            })
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read = Mock(return_value=json.dumps([{
                "custom_domain": "aniccaai.com", "analytics_instance_id": None,
            }]).encode())
            with patch.object(MODULE, "_private_env_value", return_value="private-token"), \
                 patch.object(MODULE.urllib.request, "urlopen", return_value=response):
                first = MODULE.observe_owned_visits(state)
                second = MODULE.observe_owned_visits(state)
            self.assertEqual(first["state"], "UNAVAILABLE")
            self.assertEqual(first["reason"], "NETLIFY_WEB_ANALYTICS_DISABLED")
            self.assertIsNone(first["placements"][0]["count"])
            self.assertEqual(first["placements"][0]["state"], "UNAVAILABLE")
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])

    def test_owned_article_url_rejects_tracking_and_ambiguous_forms(self):
        self.assertTrue(MODULE.is_owned_article_url("https://aniccaai.com/blog/alpha-guide"))
        for value in (
            "https://aniccaai.com/blog/alpha-guide?utm=tracking",
            "https://user@aniccaai.com/blog/alpha-guide",
            "https://aniccaai.com/blog/alpha-guide\nhttps://example.test",
            "https://aniccaai.com/blog/%2e%2e/private",
        ):
            with self.subTest(value=value):
                self.assertFalse(MODULE.is_owned_article_url(value))

    def test_repost_affiliate_placement_id_requires_exact_owned_url(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "affiliate"
            repost = Path(root) / "repost"
            repost.mkdir()
            MODULE.atomic_json(state / "campaign-publications" / "alpha-en.json", {
                "state": "X_LIVE", "plan_id": "alpha-en",
                "placement_id": "alpha-en-1",
                "owned_url": "https://aniccaai.com/blog/alpha-guide",
                "x_url": "https://x.com/selawmqt/status/1",
            })
            MODULE.append(repost / "posted.jsonl", {
                "post_url": "https://x.com/selawmqt/status/2",
                "source_url": "https://aniccaai.com/blog/alpha-guide",
                "affiliate_placement_id": "alpha-en-1",
                "affiliate_owned_article_url": "https://aniccaai.com/blog/alpha-guide",
            })
            MODULE.append(repost / "posted.jsonl", {
                "post_url": "https://x.com/selawmqt/status/3",
                "source_url": "https://aniccaai.com/blog/other-guide",
                "affiliate_placement_id": "alpha-en-1",
                "affiliate_owned_article_url": "https://aniccaai.com/blog/alpha-guide",
            })
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                observed = MODULE.observe_repost_acquisition(state)
            self.assertEqual(observed["joined_campaign_count"], 1)
            self.assertEqual(observed["placement_id_join_count"], 1)
            self.assertEqual(observed["unjoined_post_action_count"], 1)
            self.assertEqual(observed["revenue_credit_state"], "NO_REVENUE_CREDIT")

    def test_repost_proposal_is_exactly_once_and_never_contains_tracking_link(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            campaign_dir = state / "campaign-publications"
            campaign_dir.mkdir(parents=True)
            MODULE.atomic_json(campaign_dir / "alpha-en.json", {
                "state": "X_LIVE", "plan_id": "alpha-en",
                "placement_id": "alpha-en-1", "created_at": "2026-08-21T00:00:00+00:00",
                "owned_url": "https://aniccaai.com/blog/alpha-guide",
            })
            MODULE.atomic_json(state / "placement-ledger.json", {"placements": [{
                "placement_id": "alpha-en-1",
                "provider_clicks": {"count": 3},
            }]})

            first = MODULE.create_repost_proposal(state)
            second = MODULE.create_repost_proposal(state)

            self.assertEqual(first["state"], "READY_FOR_EXISTING_REPOST_OWNER")
            self.assertTrue(first["changed"])
            self.assertEqual(first["repost_delivery_state"], "UNCONSUMED_BY_SEPARATE_OWNER")
            self.assertEqual(first["revenue_credit_state"], "NO_REVENUE_CREDIT")
            self.assertEqual(first["tracking_link_state"], "NOT_INCLUDED")
            self.assertEqual(second["state"], "ALREADY_PROPOSED")
            self.assertFalse(second["changed"])
            serialized = (state / "repost-proposals.jsonl").read_text()
            self.assertNotIn("try.elevenlabs.io", serialized)
            self.assertNotIn("affiliate_link", serialized)

    def test_repost_proposal_reflects_terminal_consumption_without_credit(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            repost = state / "repost"
            campaign_dir = state / "campaign-publications"
            campaign_dir.mkdir(parents=True)
            MODULE.atomic_json(campaign_dir / "alpha-en.json", {
                "state": "X_LIVE", "plan_id": "alpha-en",
                "placement_id": "alpha-en-1", "created_at": "2026-08-21T00:00:00+00:00",
                "owned_url": "https://aniccaai.com/blog/alpha-guide",
            })
            MODULE.atomic_json(state / "placement-ledger.json", {"placements": [{
                "placement_id": "alpha-en-1", "provider_clicks": {"count": 3},
            }]})
            first = MODULE.create_repost_proposal(state)
            snapshot = json.loads((state / "repost-proposals" / "latest.json").read_text())
            MODULE.append(repost / "affiliate-proposals-consumed.jsonl", {
                "schema_version": 1,
                "receipt_type": "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION",
                "proposal_id": first["proposal_id"], "placement_id": "alpha-en-1",
                "state": "EFFECT_STARTED", "proposal": snapshot,
                "observed_at": "2026-08-21T14:31:32+00:00",
                "revenue_credit_state": "NO_REVENUE_CREDIT_UNTIL_EXACT_AFFILIATE_JOIN",
            })
            MODULE.append(repost / "affiliate-proposals-consumed.jsonl", {
                "schema_version": 1,
                "receipt_type": "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION",
                "proposal_id": first["proposal_id"],
                "placement_id": "alpha-en-1",
                "state": "UNVERIFIED",
                "observed_at": "2026-08-21T14:32:32+00:00",
                "revenue_credit_state": "NO_REVENUE_CREDIT_UNTIL_EXACT_AFFILIATE_JOIN",
            })
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                second = MODULE.create_repost_proposal(state)
            self.assertEqual(second["state"], "ALREADY_PROPOSED")
            self.assertEqual(second["repost_delivery_state"], "UNVERIFIED_BY_SEPARATE_OWNER")
            self.assertEqual(second["revenue_credit_state"], "NO_REVENUE_CREDIT")

    def test_repost_consumption_malformed_unrelated_row_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            repost = state / "repost"
            campaign_dir = state / "campaign-publications"
            campaign_dir.mkdir(parents=True)
            MODULE.atomic_json(campaign_dir / "alpha-en.json", {
                "state": "X_LIVE", "plan_id": "alpha-en",
                "placement_id": "alpha-en-1", "created_at": "2026-08-21T00:00:00+00:00",
                "owned_url": "https://aniccaai.com/blog/alpha-guide",
            })
            MODULE.atomic_json(state / "placement-ledger.json", {"placements": [{
                "placement_id": "alpha-en-1", "provider_clicks": {"count": 3},
            }]})
            first = MODULE.create_repost_proposal(state)
            MODULE.append(repost / "affiliate-proposals-consumed.jsonl", {
                "schema_version": 1,
                "receipt_type": "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION",
                "proposal_id": "bad",
                "placement_id": "alpha-en-1",
                "state": "UNVERIFIED",
                "observed_at": "2026-08-21T14:32:32+00:00",
            })
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                second = MODULE.create_repost_proposal(state)
            self.assertEqual(second["proposal_id"], first["proposal_id"])
            self.assertEqual(second["repost_delivery_state"], "CONSUMPTION_LEDGER_INVALID")

    def test_repost_consumption_snapshot_and_placement_history_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            repost = state / "repost"
            campaign_dir = state / "campaign-publications"
            campaign_dir.mkdir(parents=True)
            MODULE.atomic_json(campaign_dir / "alpha-en.json", {
                "state": "X_LIVE", "plan_id": "alpha-en",
                "placement_id": "alpha-en-1", "created_at": "2026-08-21T00:00:00+00:00",
                "owned_url": "https://aniccaai.com/blog/alpha-guide",
            })
            MODULE.atomic_json(state / "placement-ledger.json", {"placements": [{
                "placement_id": "alpha-en-1", "provider_clicks": {"count": 3},
            }]})
            first = MODULE.create_repost_proposal(state)
            snapshot = json.loads((state / "repost-proposals" / "latest.json").read_text())
            MODULE.append(repost / "affiliate-proposals-consumed.jsonl", {
                "schema_version": 1,
                "receipt_type": "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION",
                "proposal_id": first["proposal_id"], "placement_id": "alpha-en-1",
                "state": "EFFECT_STARTED", "proposal": snapshot,
                "observed_at": "2026-08-21T14:32:32+00:00",
                "revenue_credit_state": "NO_REVENUE_CREDIT_UNTIL_EXACT_AFFILIATE_JOIN",
            })
            MODULE.append(repost / "affiliate-proposals-consumed.jsonl", {
                "schema_version": 1,
                "receipt_type": "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION",
                "proposal_id": first["proposal_id"], "placement_id": "beta-en-1",
                "state": "UNVERIFIED",
                "observed_at": "2026-08-21T14:33:32+00:00",
                "revenue_credit_state": "NO_REVENUE_CREDIT_UNTIL_EXACT_AFFILIATE_JOIN",
            })
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                second = MODULE.create_repost_proposal(state)
            self.assertEqual(second["repost_delivery_state"], "CONSUMPTION_LEDGER_INVALID")

    def test_repost_consumption_terminal_state_is_absorbing(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            repost = state / "repost"
            campaign_dir = state / "campaign-publications"
            campaign_dir.mkdir(parents=True)
            MODULE.atomic_json(campaign_dir / "alpha-en.json", {
                "state": "X_LIVE", "plan_id": "alpha-en",
                "placement_id": "alpha-en-1", "created_at": "2026-08-21T00:00:00+00:00",
                "owned_url": "https://aniccaai.com/blog/alpha-guide",
            })
            MODULE.atomic_json(state / "placement-ledger.json", {"placements": [{
                "placement_id": "alpha-en-1", "provider_clicks": {"count": 3},
            }]})
            first = MODULE.create_repost_proposal(state)
            snapshot = json.loads((state / "repost-proposals" / "latest.json").read_text())
            rows = [
                {"schema_version": 1, "receipt_type": "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION",
                 "proposal_id": first["proposal_id"], "placement_id": "alpha-en-1",
                 "state": "EFFECT_STARTED", "proposal": snapshot,
                 "observed_at": "2026-08-21T14:32:32+00:00",
                 "revenue_credit_state": "NO_REVENUE_CREDIT_UNTIL_EXACT_AFFILIATE_JOIN"},
                {"schema_version": 1, "receipt_type": "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION",
                 "proposal_id": first["proposal_id"], "placement_id": "alpha-en-1",
                 "state": "UNVERIFIED", "observed_at": "2026-08-21T14:33:32+00:00",
                 "revenue_credit_state": "NO_REVENUE_CREDIT_UNTIL_EXACT_AFFILIATE_JOIN"},
                {"schema_version": 1, "receipt_type": "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION",
                 "proposal_id": first["proposal_id"], "placement_id": "alpha-en-1",
                 "state": "EFFECT_STARTED", "proposal": snapshot,
                 "observed_at": "2026-08-21T14:34:32+00:00",
                 "revenue_credit_state": "NO_REVENUE_CREDIT_UNTIL_EXACT_AFFILIATE_JOIN"},
            ]
            for row in rows:
                MODULE.append(repost / "affiliate-proposals-consumed.jsonl", row)
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                second = MODULE.create_repost_proposal(state)
            self.assertEqual(second["repost_delivery_state"], "CONSUMPTION_LEDGER_INVALID")

    def test_repost_consumption_requires_write_ahead_claim(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            repost = state / "repost"
            campaign_dir = state / "campaign-publications"
            campaign_dir.mkdir(parents=True)
            MODULE.atomic_json(campaign_dir / "alpha-en.json", {
                "state": "X_LIVE", "plan_id": "alpha-en",
                "placement_id": "alpha-en-1", "created_at": "2026-08-21T00:00:00+00:00",
                "owned_url": "https://aniccaai.com/blog/alpha-guide",
            })
            MODULE.atomic_json(state / "placement-ledger.json", {"placements": [{
                "placement_id": "alpha-en-1", "provider_clicks": {"count": 3},
            }]})
            first = MODULE.create_repost_proposal(state)
            MODULE.append(repost / "affiliate-proposals-consumed.jsonl", {
                "schema_version": 1,
                "receipt_type": "X_REPOST_AFFILIATE_PROPOSAL_CONSUMPTION",
                "proposal_id": first["proposal_id"], "placement_id": "alpha-en-1",
                "state": "UNVERIFIED", "observed_at": "2026-08-21T14:33:32+00:00",
                "revenue_credit_state": "NO_REVENUE_CREDIT_UNTIL_EXACT_AFFILIATE_JOIN",
            })
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                second = MODULE.create_repost_proposal(state)
            self.assertEqual(second["repost_delivery_state"], "CONSUMPTION_LEDGER_INVALID")

    def test_cost_budget_deduplicates_actual_usd_rows_and_blocks_at_cap(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            now = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
            for row in (
                {"cost_id": "bill-1", "cost_basis": "actual_billed", "occurred_at": now,
                 "currency": "USD", "amount_minor": 300},
                {"cost_id": "bill-2", "cost_basis": "actual_billed", "created_at": now,
                 "currency": "USD", "amount_minor": 200},
                {"cost_id": "bill-1", "cost_basis": "actual_billed", "observed_at": now,
                 "currency": "USD", "amount_minor": 300},
            ):
                MODULE.append(state / "cost-ledger.jsonl", row)
            snapshot = MODULE.cost_budget_snapshot(state, cap_minor=500)
            self.assertEqual(snapshot["state"], "COST_CAP_BLOCKED")
            self.assertEqual(snapshot["known_actual_minor_by_currency"], {"USD": 500})
            self.assertEqual(snapshot["known_actual_usd_minor"], 500)
            self.assertEqual(snapshot["unknown_rows"], 0)
            self.assertEqual(
                json.loads((state / "cost-budget.json").read_text())["receipt_type"],
                "AFFILIATE_EXTERNAL_COST_BUDGET",
            )

    def test_cost_budget_excludes_estimates_invalid_rows_and_non_usd_as_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            now = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
            for row in (
                {"cost_id": "bill-1", "cost_basis": "actual_billed", "occurred_at": now,
                 "currency": "USD", "amount_minor": 100},
                {"cost_id": "estimate-1", "cost_basis": "estimate", "occurred_at": now,
                 "currency": "USD", "amount_minor": 900},
                {"cost_basis": "actual_billed", "occurred_at": now,
                 "currency": "USD", "amount_minor": 200},
                {"cost_id": "bill-eur", "cost_basis": "actual_billed", "occurred_at": now,
                 "currency": "EUR", "amount_minor": 900},
                {"cost_id": "bill-bad", "cost_basis": "actual_billed", "occurred_at": now,
                 "currency": "USD", "amount_minor": -1},
            ):
                MODULE.append(state / "cost-ledger.jsonl", row)
            snapshot = MODULE.cost_budget_snapshot(state, cap_minor=500)
            self.assertEqual(snapshot["state"], "COST_CAP_UNKNOWN")
            self.assertEqual(snapshot["known_actual_usd_minor"], 100)
            self.assertEqual(snapshot["known_actual_minor_by_currency"], {"USD": 100})
            self.assertEqual(snapshot["unknown_rows"], 4)

    def test_cost_budget_snapshot_does_not_use_network(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            MODULE.urllib.request, "urlopen", side_effect=AssertionError("network")
        ) as urlopen:
            state = Path(root)
            now = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat()
            MODULE.append(state / "cost-ledger.jsonl", {
                "receipt_id": "bill-1", "cost_basis": "actual_billed",
                "observed_at": now, "currency": "USD", "amount_minor": 1,
            })
            snapshot = MODULE.cost_budget_snapshot(state)
            self.assertEqual(snapshot["state"], "CLEAR")
            urlopen.assert_not_called()

    def test_cost_cap_blocked_report_is_typed_and_stable(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.atomic_json(state / "rolling-net.json", {})
            wake = {
                "status": "READY_FOR_PUBLICATION",
                "cost_budget_state": "COST_CAP_BLOCKED",
                "cost_budget_known_actual_usd_minor": 500,
                "cost_budget_cap_minor": 500,
                "rolling_net_money_state": "NO_TRANSACTIONS",
                "publication_url": "https://example.test/article",
            }
            blocked = MODULE.owner_event(state, wake)
            self.assertEqual(blocked["kind"], "BLOCKED")
            self.assertIn("external_cost_cap=USD 5.00/USD 5.00", blocked["body"])
            self.assertIn("no money counted", blocked["body"])
            self.assertTrue(blocked.get("dedupe_key", "").startswith(
                "BLOCKED:COST_CAP_BLOCKED"
            ))

    def test_action_budget_counts_only_non_no_effect_attempts_for_jst_day(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            now = datetime.now().astimezone().isoformat()
            for certainty in ("UNKNOWN", "EFFECT_CONFIRMED", "NO_EFFECT"):
                MODULE.append(state / "tool-attempt-receipts.jsonl", {
                    "tool": "publication.advance",
                    "effect_class": "PUBLICATION_WRITE",
                    "effect_certainty": certainty,
                    "finished_at": now,
                })
            snapshot = MODULE.action_budget_snapshot(state, cap=2)
            self.assertEqual(snapshot["used_attempts"], 2)
            self.assertEqual(snapshot["state"], "ACTION_CAP_BLOCKED")

    def test_action_budget_can_be_explicitly_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            snapshot = MODULE.action_budget_snapshot(Path(root), cap=None)
            self.assertEqual(snapshot["state"], "ACTION_CAP_DISABLED")
            self.assertIsNone(snapshot["daily_cap"])

    def test_quarantine_requires_three_consecutive_external_failures(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            for number in range(3):
                MODULE.append(state / "tool-attempt-receipts.jsonl", {
                    "tool": "provider-link.elevenlabs",
                    "effect_class": "PROVIDER_LINK_WRITE",
                    "outcome": "FAILED",
                    "failure_type": "TimeoutError",
                })
            snapshot = MODULE.quarantine_snapshot(state)
            self.assertEqual(snapshot["state"], "QUARANTINED")
            self.assertEqual(
                snapshot["tools"]["provider-link.elevenlabs"]["consecutive_failures"],
                3,
            )
            MODULE.append(state / "tool-attempt-receipts.jsonl", {
                "tool": "provider-link.elevenlabs",
                "effect_class": "PROVIDER_LINK_WRITE",
                "outcome": "COMPLETED",
            })
            self.assertEqual(MODULE.quarantine_snapshot(state)["state"], "CLEAR")

    def test_owner_health_redacts_and_persists_label_and_cdp_state(self):
        class Result:
            returncode = 0
            stdout = "state = running\nruns = 3\nlast exit code = 0\n"

        with tempfile.TemporaryDirectory() as root, patch.object(
            MODULE, "browser_ready", return_value=True,
        ):
            health = MODULE.owner_health(
                Path(root), ports=(1,), runner=lambda *args, **kwargs: Result(),
            )
            self.assertEqual(health["state"], "HEALTHY")
            self.assertEqual(health["labels"]["ai.anicca.affiliate-loop"]["runs"], "3")
            self.assertEqual(health["cdp"]["1"]["state"], "READY")
            stored = json.loads((Path(root) / "owner-health.json").read_text())
            self.assertEqual(stored["receipt_type"], "AFFILIATE_OWNER_HEALTH")

    def test_runtime_disk_guard_is_truthful_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            self.assertIsNone(sys.modules["runtime_guard"].RUNTIME_DISK_FLOOR_BYTES)
            disabled = MODULE.runtime_guard(state)
            clear = MODULE.runtime_guard(state, floor_bytes=1)
            blocked = MODULE.runtime_guard(state, floor_bytes=10 ** 30)
            self.assertEqual(disabled["state"], "CLEAR")
            self.assertIsNone(disabled["floor_bytes"])
            self.assertEqual(clear["state"], "CLEAR")
            self.assertEqual(blocked["state"], "DISK_GUARD_BLOCKED")
            self.assertEqual(blocked["guard"], "disk")
            self.assertEqual(blocked["floor_bytes"], 10 ** 30)
            self.assertEqual(blocked["receipt_persist_state"], "PERSISTED")
            stored = json.loads((state / "runtime-guard.json").read_text())
            self.assertEqual(stored["state"], "DISK_GUARD_BLOCKED")
            self.assertEqual(stored["receipt_persist_state"], "PERSISTED")

    def test_disk_guard_outcome_is_no_effect(self):
        self.assertEqual(
            MODULE._tool_outcome({"state": "DISK_GUARD_BLOCKED"}),
            "NO_EFFECT",
        )

    def test_daily_summary_has_stable_jst_day_identity_and_money_stage(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            receipt_dir = state / "composition-receipts"
            run_dir = state / "composition-runs" / f"alpha-en-{'a' * 16}"
            receipt_dir.mkdir(parents=True)
            run_dir.mkdir(parents=True)
            MODULE.atomic_json(receipt_dir / "alpha-en.json", {
                "state": "FAILED", "failure_class": "RUNNER_REJECTED",
                "plan_id": "alpha-en", "source_set_sha256": "a" * 64,
            })
            MODULE.atomic_json(run_dir / "summary.json", {
                "status": "budget_blocked", "budget": {"day": "2026-08-16"},
            })
            published_run = state / "composition-runs" / f"published-en-{'b' * 16}"
            published_run.mkdir(parents=True)
            MODULE.atomic_json(receipt_dir / "published-en.json", {
                "state": "FAILED", "failure_class": "RUNNER_REJECTED",
                "plan_id": "published-en", "source_set_sha256": "b" * 64,
            })
            MODULE.atomic_json(published_run / "summary.json", {
                "status": "budget_blocked", "budget": {"day": "2026-08-16"},
            })
            MODULE.atomic_json(state / "discovered-source-plans" / "alpha-en.json", {
                "buyer_intent": "Creators choosing a transcript workflow",
            })
            (state / "provider-reports" / "partnerstack-links").mkdir(parents=True)
            MODULE.atomic_json(
                state / "provider-reports" / "partnerstack-links" / "latest.json",
                {"observed_at": "provider-time", "placements": [{
                    "current_click_count": 0,
                }]},
            )
            MODULE.atomic_json(state / "placement-ledger.json", {
                "placements": [
                    {
                        "placement_id": "alpha-en-1",
                        "plan_id": "published-en",
                        "provider_link_key": "link-alpha",
                        "public_url": "https://example.test/published",
                        "provider_clicks": {"count": 0, "unique_count": 0},
                    },
                    {
                        "placement_id": "beta-en-1",
                        "provider_link_key": "link-beta",
                        "provider_clicks": {"count": None, "unique_count": None},
                    },
                ],
            })
            wake = {
                "provider_state": "AUTHENTICATED",
                "impact_state": "APPLICATION_PENDING",
                "systeme_state": "CAPTCHA_CHALLENGE",
            }
            morning = datetime(2026, 8, 16, 8, tzinfo=ZoneInfo("Asia/Tokyo"))
            evening = datetime(2026, 8, 16, 21, tzinfo=ZoneInfo("Asia/Tokyo"))
            next_day = datetime(2026, 8, 17, 8, tzinfo=ZoneInfo("Asia/Tokyo"))
            first = MODULE.daily_summary_event(state, wake, morning)
            second = MODULE.daily_summary_event(state, wake, evening)
            third = MODULE.daily_summary_event(state, wake, next_day)
            unknown = MODULE.daily_summary_event(
                state,
                {"provider_state": "SIGN_IN_REQUIRED", "impact_state": "UNKNOWN", "systeme_state": "FAILED"},
                next_day,
            )
            self.assertEqual(first["event_uuid"], second["event_uuid"])
            self.assertNotEqual(first["event_uuid"], third["event_uuid"])
            self.assertIn("専用リンクで最初の外部クリック", first["body"])
            self.assertIn("現在の制作対象", first["body"])
            self.assertIn("次のJST予算で同じ仕事を自動再開", first["body"])
            self.assertIn("Creators choosing a transcript workflow", first["body"])
            self.assertNotIn("published-en", first["body"])
            self.assertIn("正規台帳には2配信面", first["body"])
            self.assertIn("残り1本のクリック値はprovider未観測", first["body"])
            summary = json.loads(
                (state / "daily-summaries" / "2026-08-16.json").read_text()
            )
            self.assertEqual(summary["placement_count"], 2)
            self.assertEqual(summary["schema_version"], 2)
            self.assertEqual(summary["provider_click_measurement_count"], 1)
            self.assertEqual(summary["provider_click_unknown_count"], 1)
            self.assertEqual(summary["provider_unique_clicks"], 0)
            self.assertEqual(summary["provider_unique_click_measurement_count"], 1)
            self.assertEqual(summary["provider_unique_click_unknown_count"], 1)
            self.assertIn("uniqueクリック", first["body"])
            self.assertEqual(summary["composition_budget_blocked_count"], 1)
            self.assertEqual(
                summary["composition_budget_blocked_campaigns"],
                [{
                    "plan_id": "alpha-en",
                    "label": "Creators choosing a transcript workflow",
                }],
            )
            self.assertNotIn("SIGN_IN_REQUIRED", unknown["body"])
            self.assertIn("確認が必要な状態", unknown["body"])

    def test_all_unsent_commissions_and_clicks_precede_daily_summary(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            for number in (1, 2):
                MODULE.append(state / "commission-ledger.jsonl", {
                    "transition_id": f"commission-{number}",
                    "provider_transaction_id": f"tx-{number}",
                    "status": "approved", "gross_commission_minor": 1000,
                    "net_commission_minor": 1000, "currency": "USD",
                    "placement": {"public_url": "https://example.test/article"},
                })
                MODULE.append(state / "click-ledger.jsonl", {
                    "transition_id": f"click-{number}", "delta_click_count": 1,
                    "public_url": "https://example.test/article",
                })
            wake = {"status": "READY_FOR_PUBLICATION"}
            observed = []
            for number in range(4):
                event = MODULE.next_telegram_event(state, wake)
                observed.append(event["kind"])
                MODULE.append(state / "telegram-sent.jsonl", {
                    "event_uuid": event["event_uuid"], "message_id": str(number),
                })
            self.assertEqual(observed, [
                "COMMISSION_APPROVED", "COMMISSION_APPROVED", "CLICK_DELTA", "CLICK_DELTA",
            ])

    def test_reconciled_impact_login_emits_one_natural_self_healed_event(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            wake = {
                "impact_state": "APPLICATION_PENDING",
                "impact_login_reconciled_job_id": "job-1",
                "status": "READY_FOR_PUBLICATION",
            }
            event = MODULE.owner_event(state, wake)
            self.assertEqual(event["kind"], "SELF_HEALED")
            self.assertIn("同じlogin jobを完了", event["body"])
            self.assertNotIn("EFFECT_STARTED", event["body"])

    def test_publication_failure_then_progress_emits_natural_self_healed_event(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.append(state / "events.jsonl", {
                "ts": 1, "publication_state": "PUBLICATION_FAILED",
                "publication_failure_type": "TimeoutError",
            })
            wake = {
                "ts": 2, "publication_state": "WAITING_FOR_PLACEMENT_LINK",
                "publication_url": None, "status": "READY_FOR_PUBLICATION",
            }
            MODULE.append(state / "events.jsonl", wake)

            event = MODULE.owner_event(state, wake)

            self.assertEqual(event["kind"], "SELF_HEALED")
            self.assertIn("同じpublicationを再開", event["body"])
            self.assertIn("ElevenLabs / PartnerStack", event["body"])
            self.assertNotIn("Impact", event["body"])

    def test_revenue_failure_then_readback_emits_one_natural_self_healed_event(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.append(state / "events.jsonl", {
                "ts": 1, "publication_state": "PUBLICATION_FAILED",
            })
            MODULE.append(state / "events.jsonl", {
                "ts": 2, "publication_state": "X_LIVE",
            })
            MODULE.append(state / "events.jsonl", {
                "ts": 10, "revenue_state": "REVENUE_CYCLE_FAILED",
            })
            wake = {
                "ts": 20, "revenue_state": "NO_TRANSACTIONS",
                "revenue_source_rows": 0, "publication_url": None,
                "status": "READY_FOR_PUBLICATION",
            }
            MODULE.append(state / "events.jsonl", wake)

            event = MODULE.owner_event(state, wake)

            self.assertEqual(event["kind"], "SELF_HEALED")
            self.assertIn("同じ収益captureを再実行", event["body"])
            self.assertIn("ElevenLabs / PartnerStack", event["body"])
            self.assertIn("transactions=0", event["body"])
            self.assertNotIn("Impact", event["body"])

    def test_revenue_recovery_ignores_intermediate_cooldown_wake(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.append(state / "events.jsonl", {
                "ts": 10, "revenue_state": "REVENUE_CYCLE_FAILED",
            })
            MODULE.append(state / "events.jsonl", {
                "ts": 20, "revenue_state": "COOLDOWN",
            })
            wake = {
                "ts": 30, "revenue_state": "NO_TRANSACTIONS",
                "revenue_source_rows": 0, "publication_url": None,
                "status": "READY_FOR_PUBLICATION",
            }
            MODULE.append(state / "events.jsonl", wake)

            event = MODULE.owner_event(state, wake)

            self.assertEqual(event["kind"], "SELF_HEALED")
            self.assertIn("同じ収益captureを再実行", event["body"])
            self.assertIn("transactions=0", event["body"])

    def test_revenue_recovery_is_delivered_on_later_wake_if_success_wake_missed_it(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            for row in (
                {"ts": 10, "revenue_state": "REVENUE_CYCLE_FAILED"},
                {"ts": 20, "revenue_state": "COOLDOWN"},
                {"ts": 30, "revenue_state": "NO_TRANSACTIONS", "revenue_source_rows": 0},
            ):
                MODULE.append(state / "events.jsonl", row)
            wake = {
                "ts": 40, "revenue_state": "COOLDOWN",
                "status": "READY_FOR_PUBLICATION", "publication_url": None,
            }
            MODULE.append(state / "events.jsonl", wake)

            event = MODULE.owner_event(state, wake)

            self.assertEqual(event["kind"], "SELF_HEALED")
            self.assertIn("transactions=0", event["body"])

    def test_revenue_failure_report_is_typed_and_new_attempts_are_not_collapsed(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.atomic_json(state / "revenue-cycle-failure.json", {
                "stage": "capture", "failure_type": "NONZERO_EXIT",
                "failure_class": "PROVIDER_TRANSIENT", "retry_state": "RETRYABLE",
                "retry_after": 200, "observed_at": 100,
                "error_sha256": "e" * 64,
            })
            first = MODULE.owner_event(state, {
                "ts": 101, "revenue_state": "REVENUE_CYCLE_FAILED",
                "status": "READY_FOR_PUBLICATION", "publication_url": None,
            })
            self.assertEqual(first["kind"], "REVENUE_CYCLE_FAILED")
            self.assertIn("class=PROVIDER_TRANSIENT", first["body"])
            self.assertIn("retry=RETRYABLE", first["body"])
            self.assertNotIn("e" * 64, first["body"])

            MODULE.atomic_json(state / "revenue-cycle-failure.json", {
                "stage": "capture", "failure_type": "NONZERO_EXIT",
                "failure_class": "PROVIDER_TRANSIENT", "retry_state": "RETRYABLE",
                "retry_after": 300, "observed_at": 200,
                "error_sha256": "f" * 64,
            })
            second = MODULE.owner_event(
                state,
                {"ts": 201, "revenue_state": "REVENUE_CYCLE_FAILED",
                 "status": "READY_FOR_PUBLICATION", "publication_url": None},
                {first["event_uuid"]},
            )
            self.assertEqual(second["kind"], "REVENUE_CYCLE_FAILED")
            self.assertNotEqual(first["event_uuid"], second["event_uuid"])

    def test_blocked_report_preserves_action_cap_disk_guard_and_money_state(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.atomic_json(state / "rolling-net.json", {
                "receipt_type": "AFFILIATE_ROLLING_NET",
                "money_state": "NO_TRANSACTIONS",
                "net_state": "NO_APPROVED_OR_PAID_ROWS",
                "threshold_state": "NOT_REACHED",
                "cost_state": "UNKNOWN",
                "cost_coverage_state": "UNKNOWN",
            })
            wake = {
                "status": "ACTION_CAP_BLOCKED",
                "action_budget_state": "ACTION_CAP_BLOCKED",
                "action_budget_used_attempts": 34,
                "action_budget_daily_cap": 10,
                "runtime_guard_state": "DISK_GUARD_BLOCKED",
                "runtime_guard_free_bytes": 958054400,
                "runtime_guard_floor_bytes": 10737418240,
                "rolling_net_money_state": "NO_TRANSACTIONS",
                "publication_url": "https://example.test/article",
            }
            first = MODULE.owner_event(state, wake)
            blocked = MODULE.owner_event(state, wake, {first["event_uuid"]})

            self.assertEqual(blocked["kind"], "BLOCKED")
            self.assertIn("判断: external_action_cap=34/10", blocked["body"])
            self.assertIn("runtime_disk=DISK_GUARD_BLOCKED", blocked["body"])
            self.assertIn("NO_TRANSACTIONS / approved_or_paid_net=USD 0.00 / cost=UNKNOWN", blocked["body"])
            self.assertIn("ディスク空きが10GiB以上かつJST日次capがCLEAR", blocked["body"])
            self.assertNotIn("buyer-intentを収集", blocked["body"])

    def test_blocked_report_identity_ignores_drifting_measurements(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            base = {
                "status": "ACTION_CAP_BLOCKED",
                "action_budget_state": "ACTION_CAP_BLOCKED",
                "action_budget_used_attempts": 34,
                "action_budget_daily_cap": 10,
                "runtime_guard_state": "DISK_GUARD_BLOCKED",
                "runtime_guard_floor_bytes": 10737418240,
                "rolling_net_money_state": "NO_TRANSACTIONS",
                "publication_url": "https://example.test/article",
            }
            first = MODULE.owner_event(
                state, {**base, "runtime_guard_free_bytes": 958054400},
            )
            second = MODULE.owner_event(
                state, {**base, "runtime_guard_free_bytes": 838873088},
            )
            self.assertEqual(first["kind"], "BLOCKED")
            self.assertEqual(first["event_uuid"], second["event_uuid"])
            self.assertNotEqual(first["body"], second["body"])

    def test_completed_generic_campaign_advances_to_tts_campaign(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            receipt = state / "x-posts" / "elevenagents-en-1.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({
                "state": "LIVE", "public_url": "https://x.com/selawmqt/status/1",
            }))
            expected = {"state": "X_LIVE", "public_url": "https://x.com/selawmqt/status/1"}
            for generic_state in ("ALREADY_LIVE", "PUBLICATION_CONFLICT"):
                with self.subTest(generic_state=generic_state):
                    with (
                        patch.object(MODULE, "advance_generic_publication", return_value={
                            "state": generic_state, "public_url": None,
                        }),
                        patch.object(
                            MODULE, "advance_legacy_dedicated_publication",
                            return_value={"state": "ALREADY_LIVE", "public_url": None},
                        ),
                        patch.object(
                            MODULE, "advance_tts_api_publication",
                            return_value=dict(expected),
                        ) as advance,
                    ):
                        result = MODULE.advance_known_publication(
                            state, Path(root) / "landing", 9326, Path(root) / "private.md",
                        )
                    self.assertEqual(result, {
                        **expected, "generic_state": generic_state,
                        "legacy_state": "ALREADY_LIVE",
                    })
                    advance.assert_called_once()

    def test_focused_cohort_does_not_fall_through_to_legacy_publication(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            state = root / "state"
            landing = root / "landing"
            landing.mkdir()
            MODULE.atomic_json(state / "focused-cohort" / "latest.json", {
                "selection_state": "FOCUSED_EXPLORATION",
                "placement_id": "subtitle-en-1",
            })
            with (
                patch.object(MODULE, "advance_generic_publication", return_value={
                    "state": "ALREADY_LIVE", "public_url": None,
                }),
                patch.object(MODULE, "advance_legacy_dedicated_publication") as legacy,
            ):
                result = MODULE.advance_known_publication(
                    state, landing, 9326, root / "private.md",
                )
            self.assertEqual(result["state"], "FOCUSED_COHORT_HELD")
            legacy.assert_not_called()

    def test_legacy_migration_stops_after_creating_one_provider_link(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            receipt = state / "x-posts" / "elevenlabs-en-1.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text(json.dumps({
                "state": "LIVE", "public_url": "https://x.com/selawmqt/status/1",
            }))
            acquire = Mock(return_value={
                "state": "VERIFIED", "deduplicated": False,
                "private_link_field": "Placement example affiliate link",
            })
            result = MODULE.advance_legacy_dedicated_publication(
                state, Path(root) / "landing", 9326, Path(root) / "private.md",
                link_acquirer=acquire,
            )
            self.assertEqual(result["state"], "WAITING_FOR_PLACEMENT_LINK")
            acquire.assert_called_once()

    def test_wake_recovers_provider_and_advances_publication_without_cross_lane_blocking(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            private = root / "affiliate-credentials.md"
            private.write_text(
                "## ElevenLabs\n"
                "- Default affiliate link: `https://try.elevenlabs.io/example`\n",
                encoding="utf-8",
            )
            private.chmod(0o600)
            args = Namespace(
                private_markdown=private, state=root / "state", cdp_port=9324,
                x_cdp_port=9326, landing_root=root / "landing",
            )
            signed_out = {
                "state": "SIGN_IN_REQUIRED", "changed": False,
                "transition_id": "transition-1",
            }
            authenticated = {
                "state": "AUTHENTICATED", "changed": True,
                "transition_id": "transition-2",
            }
            output = io.StringIO()
            with (
                patch.object(MODULE, "browser_ready", side_effect=lambda port: port == 9324),
                patch.object(MODULE, "provider_poll", return_value=signed_out),
                patch.object(MODULE, "recover_provider", return_value=authenticated) as recover,
                patch.object(MODULE, "elevenlabs_link_action", return_value={
                    "state": "VERIFIED", "placement": MODULE.TTS_PLACEMENT,
                    "deduplicated": True, "provider_link_key": "link-1",
                }),
                patch.object(MODULE, "apply_getresponse", return_value={
                    "state": "ELIGIBILITY_BLOCKED", "program": "getresponse",
                    "deduplicated": True,
                }),
                patch.object(MODULE, "verify_systeme_email", return_value={
                    "state": "CAPTCHA_CHALLENGE", "deduplicated": True,
                }),
                patch.object(MODULE, "advance_known_publication", return_value={
                    "state": "X_LIVE", "public_url": "https://x.com/selawmqt/status/1",
                }) as advance,
                patch.object(MODULE, "observe_devto_acquisition", return_value={
                    "state": "OBSERVED", "article_count": 1,
                    "total_page_views": 0, "delta_page_views": 0,
                }),
                patch.object(MODULE, "run_revenue_cycle", return_value={
                    "state": "NO_TRANSACTIONS", "source_rows": 0,
                    "appended_transitions": 0,
                }),
                patch.object(MODULE, "flush_telegram", return_value={
                    "state": "NO_PENDING", "sent": 0, "message_id": None,
                }),
                contextlib.redirect_stdout(output),
            ):
                MODULE.wake(args)
            event = json.loads(output.getvalue())
            recover.assert_called_once()
            advance.assert_called_once()
            self.assertEqual(event["provider_state"], "AUTHENTICATED")
            self.assertEqual(event["publication_state"], "X_LIVE")
            self.assertEqual(event["revenue_state"], "NO_TRANSACTIONS")
            run_receipts = [
                json.loads(line)
                for line in (args.state / "run-receipts.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(run_receipts), 1)
            self.assertEqual(run_receipts[0]["run_id"], event["wake_event_uuid"])
            self.assertEqual(run_receipts[0]["release_sha"], "SOURCE_CHECKOUT")
            self.assertEqual(run_receipts[0]["terminal_state"], "READY_FOR_PUBLICATION")
            self.assertEqual(
                [stage["name"] for stage in run_receipts[0]["stages"]],
                [
                    "provider", "placement_link", "publication", "distribution",
                    "revenue", "rolling_net", "repost_observation", "telegram",
                ],
            )

    def test_wake_requires_authenticated_provider_and_receipts_transition(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            private = root / "affiliate-credentials.md"
            private.write_text(
                "## ElevenLabs\n"
                "- Default affiliate link: `https://try.elevenlabs.io/example`\n",
                encoding="utf-8",
            )
            private.chmod(0o600)
            args = Namespace(private_markdown=private, state=root / "state", cdp_port=9324)
            provider = {
                "state": "AUTHENTICATED", "changed": True,
                "transition_id": "transition-1",
            }
            output = io.StringIO()
            with (
                patch.object(MODULE, "browser_ready", return_value=True),
                patch.object(MODULE, "provider_poll", return_value=provider),
                patch.object(MODULE, "elevenlabs_link_action", return_value={
                    "state": "VERIFIED", "placement": MODULE.TTS_PLACEMENT,
                    "deduplicated": True, "provider_link_key": "link-1",
                }),
                patch.object(MODULE, "apply_getresponse", return_value={
                    "state": "ELIGIBILITY_BLOCKED", "program": "getresponse",
                    "deduplicated": True,
                }),
                patch.object(MODULE, "verify_systeme_email", return_value={
                    "state": "CAPTCHA_CHALLENGE", "deduplicated": True,
                }),
                patch.object(MODULE, "observe_devto_acquisition", return_value={
                    "state": "OBSERVED", "article_count": 1,
                    "total_page_views": 0, "delta_page_views": 0,
                }),
                patch.object(MODULE, "run_revenue_cycle", return_value={
                    "state": "NO_TRANSACTIONS", "source_rows": 0,
                    "appended_transitions": 0,
                }),
                patch.object(MODULE, "flush_telegram", return_value={
                    "state": "NO_PENDING", "sent": 0, "message_id": None,
                }),
                contextlib.redirect_stdout(output),
            ):
                MODULE.wake(args)
            event = json.loads(output.getvalue())
            self.assertEqual(event["status"], "READY_FOR_PUBLICATION")
            self.assertEqual(event["provider_state"], "AUTHENTICATED")
            self.assertEqual(event["provider_transition_id"], "transition-1")
            self.assertEqual(event["revenue_state"], "NO_TRANSACTIONS")

    def test_run_receipt_is_append_only_and_replay_safe(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            event = {
                "event": "affiliate_wake",
                "wake_event_uuid": "wake-1",
                "status": "READY_FOR_PUBLICATION",
                "provider_state": "AUTHENTICATED",
                "placement_link_state": "VERIFIED",
                "publication_state": "X_LIVE",
                "distribution_state": "COOLDOWN",
                "revenue_state": "NO_TRANSACTIONS",
                "rolling_net_net_state": "NO_APPROVED_OR_PAID_ROWS",
                "repost_observation": {"state": "OBSERVED"},
                "telegram_state": "NO_PENDING",
            }
            self.assertTrue(MODULE.append_run_receipt(state, event, 100.0, 101.5))
            self.assertFalse(MODULE.append_run_receipt(state, event, 100.0, 101.5))
            rows = [
                json.loads(line)
                for line in (state / "run-receipts.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["release_sha"], "SOURCE_CHECKOUT")
            self.assertEqual(rows[0]["owner_label"], MODULE.RUN_OWNER_LABEL)
            self.assertEqual(rows[0]["duration_ms"], 1500)
            self.assertEqual(rows[0]["causal_parent"]["owner_label"], MODULE.RUN_OWNER_LABEL)

    def test_tool_attempt_receipt_is_redacted_append_only_and_effect_classified(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            result = {
                "state": "VERIFIED", "changed": False, "deduplicated": True,
                "usage": {"requests": 1, "secret": "must-not-copy"},
            }
            self.assertTrue(MODULE.append_tool_attempt_receipt(
                state, "scheduler-1", "provider-link.elevenlabs",
                "PROVIDER_LINK_WRITE", 1, {"placement": "tts"}, 10.0,
                result=result,
            ))
            self.assertFalse(MODULE.append_tool_attempt_receipt(
                state, "scheduler-1", "provider-link.elevenlabs",
                "PROVIDER_LINK_WRITE", 1, {"placement": "tts"}, 10.0,
                result=result,
            ))
            rows = [json.loads(line) for line in (
                state / "tool-attempt-receipts.jsonl"
            ).read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outcome"], "COMPLETED")
            self.assertEqual(rows[0]["effect_certainty"], "NO_EFFECT")
            self.assertEqual(rows[0]["usage"], {"requests": 1})
            self.assertNotIn("secret", (state / "tool-attempt-receipts.jsonl").read_text())
            self.assertNotIn("https://", (state / "tool-attempt-receipts.jsonl").read_text())

    def test_tool_attempt_rotation_preserves_external_effect_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            ledger = state / "tool-attempt-receipts.jsonl"
            for number in range(20):
                MODULE.append(ledger, {
                    "scheduler_run_id": f"old-{number}",
                    "effect_certainty": "NO_EFFECT",
                    "padding": "x" * 80,
                })
            MODULE.append(ledger, {
                "scheduler_run_id": "confirmed",
                "effect_certainty": "EFFECT_CONFIRMED",
            })
            MODULE.append(ledger, {
                "scheduler_run_id": "unknown",
                "effect_certainty": "UNKNOWN",
            })
            with ledger.open("a", encoding="utf-8") as stream:
                stream.write("malformed\n")

            result = MODULE.rotate_tool_attempt_receipts(
                ledger, max_bytes=512, recent_no_effect_bytes=128,
                keep_archives=2,
            )

            active = ledger.read_text(encoding="utf-8")
            self.assertIn("EFFECT_CONFIRMED", active)
            self.assertIn("UNKNOWN", active)
            self.assertIn("malformed", active)
            self.assertLessEqual(len(list(state.glob(
                "tool-attempt-receipts.archive-*.jsonl.gz"
            ))), 2)
            self.assertGreater(result["archived_rows"], 0)
            self.assertEqual(result["protected_rows"], 3)

    def test_tool_attempt_rotation_replay_is_noop(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / "tool-attempt-receipts.jsonl"
            for number in range(20):
                MODULE.append(ledger, {
                    "scheduler_run_id": str(number),
                    "effect_certainty": "NO_EFFECT", "padding": "x" * 80,
                })
            MODULE.rotate_tool_attempt_receipts(
                ledger, max_bytes=512, recent_no_effect_bytes=128,
            )
            archive_count = len(list(Path(root).glob(
                "tool-attempt-receipts.archive-*.jsonl.gz"
            )))

            replay = MODULE.rotate_tool_attempt_receipts(
                ledger, max_bytes=512, recent_no_effect_bytes=128,
            )

            self.assertEqual(replay["archived_rows"], 0)
            self.assertEqual(len(list(Path(root).glob(
                "tool-attempt-receipts.archive-*.jsonl.gz"
            ))), archive_count)

    def test_tool_attempt_rotation_preserves_active_row_order(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / "tool-attempt-receipts.jsonl"
            rows = [
                {"scheduler_run_id": "old", "effect_certainty": "NO_EFFECT",
                 "padding": "x" * 300},
                {"scheduler_run_id": "confirmed",
                 "effect_certainty": "EFFECT_CONFIRMED"},
                {"scheduler_run_id": "recent", "effect_certainty": "NO_EFFECT"},
                {"scheduler_run_id": "unknown", "effect_certainty": "UNKNOWN"},
            ]
            for row in rows:
                MODULE.append(ledger, row)

            MODULE.rotate_tool_attempt_receipts(
                ledger, max_bytes=256, recent_no_effect_bytes=128,
            )

            active_ids = [row["scheduler_run_id"] for row in MODULE.json_rows(ledger)]
            self.assertEqual(active_ids, ["confirmed", "recent", "unknown"])

    def test_tool_attempt_rotation_bounds_read_only_observations(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / "tool-attempt-receipts.jsonl"
            for number in range(20):
                MODULE.append(ledger, {
                    "scheduler_run_id": str(number),
                    "effect_certainty": "READ_ONLY_CONFIRMED",
                    "padding": "x" * 80,
                })

            result = MODULE.rotate_tool_attempt_receipts(
                ledger, max_bytes=512, recent_no_effect_bytes=128,
            )

            self.assertGreater(result["archived_rows"], 0)
            self.assertLess(ledger.stat().st_size, 512)

    def test_tool_attempt_rotation_failure_preserves_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = Path(root) / "tool-attempt-receipts.jsonl"
            for number in range(20):
                MODULE.append(ledger, {
                    "scheduler_run_id": str(number),
                    "effect_certainty": "NO_EFFECT", "padding": "x" * 80,
                })
            original = ledger.read_bytes()
            real_replace = MODULE.os.replace

            def fail_active_replace(source, destination):
                if Path(destination) == ledger:
                    raise OSError("injected active replace failure")
                return real_replace(source, destination)

            with patch.object(MODULE.os, "replace", side_effect=fail_active_replace):
                with self.assertRaisesRegex(OSError, "active replace failure"):
                    MODULE.rotate_tool_attempt_receipts(
                        ledger, max_bytes=512, recent_no_effect_bytes=128,
                    )

            self.assertEqual(ledger.read_bytes(), original)

    def test_tool_attempt_failure_records_unknown_external_effect(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            with self.assertRaises(TimeoutError):
                MODULE.attempt_tool(
                    state, "scheduler-2", "telegram.send", "MESSAGE_SEND", {},
                    lambda: (_ for _ in ()).throw(TimeoutError("provider timeout")),
                )
            row = json.loads((state / "tool-attempt-receipts.jsonl").read_text())
            self.assertEqual(row["outcome"], "FAILED")
            self.assertEqual(row["failure_type"], "TimeoutError")
            self.assertEqual(row["failure_class"], "BROWSER_TRANSIENT")
            self.assertEqual(row["retry_state"], "RETRYABLE")
            self.assertGreater(row["retry_due_at"], 0)
            self.assertEqual(row["effect_certainty"], "UNKNOWN")

    def test_telegram_outbox_precedes_send_and_deduplicates_message_id(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            event = {"event_uuid": "event-1", "kind": "REVENUE_RECONCILED", "body": "report", "created_at": 1}
            calls = []

            def sender(message, **kwargs):
                calls.append((message, kwargs))
                self.assertTrue((state / "telegram-outbox.jsonl").is_file())
                return MODULE.SimpleNamespace(started=True, provider_id="7640", error=None)

            first = MODULE.flush_telegram(state, event, sender=sender)
            second = MODULE.flush_telegram(state, event, sender=sender)
            self.assertEqual(first, {
                "state": "SENT", "sent": 1, "message_id": "7640",
                "sent_event_uuid": "event-1",
            })
            self.assertEqual(second["state"], "NO_PENDING")
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads((state / "telegram-sent.jsonl").read_text())["message_id"], "7640")

    def test_telegram_timeout_is_quarantined_without_retry(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            event = {
                "event_uuid": "timeout-event", "kind": "REVENUE_RECONCILED",
                "body": "report", "created_at": 1,
            }
            calls = []

            def timeout_sender(message, **kwargs):
                calls.append((message, kwargs))
                raise subprocess.TimeoutExpired(["telegram"], 30)

            first = MODULE.flush_telegram(state, event, sender=timeout_sender)
            MODULE.append_telegram_delivery_receipt(
                state, {"wake_event_uuid": "wake-1", "ts": 1}, event, first,
            )

            def must_not_retry(message, **kwargs):
                raise AssertionError("ambiguous Telegram effect must not be retried")

            second = MODULE.flush_telegram(state, event, sender=must_not_retry)

            self.assertEqual(first["state"], "SEND_TIMEOUT_UNKNOWN")
            self.assertEqual(second["state"], "AMBIGUOUS_NO_RETRY")
            self.assertEqual(second["sent_event_uuid"], "timeout-event")
            self.assertEqual(len(calls), 1)

    def test_telegram_receipt_binds_the_event_actually_sent(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            old = {"event_uuid": "old-event", "kind": "AFFILIATE_DAILY_SUMMARY", "body": "old", "created_at": 1}
            current = {"event_uuid": "current-event", "kind": "REPOST_OBSERVED", "body": "current", "created_at": 2}
            MODULE.append(state / "telegram-outbox.jsonl", old)

            def sender(message, **kwargs):
                return MODULE.SimpleNamespace(started=True, provider_id="7641", error=None)

            delivery = MODULE.flush_telegram(state, current, sender=sender)
            receipt = MODULE.append_telegram_delivery_receipt(
                state, {"wake_event_uuid": "wake-1", "ts": 1}, current, delivery,
            )
            self.assertEqual(delivery["sent_event_uuid"], "old-event")
            self.assertEqual(receipt["telegram_event_uuid"], "old-event")
            self.assertEqual(receipt["telegram_kind"], "AFFILIATE_DAILY_SUMMARY")

    def test_telegram_supersedes_pending_equivalent_blocker_after_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            body = (
                "Life Manager Affiliate::: Affiliate loop report\n"
                "実行: BLOCKED\n"
                "お金: NO_TRANSACTIONS / approved_or_paid_net=USD 0.00 / cost=UNKNOWN / "
                "blocker=external_action_cap=34/10 / runtime_disk=DISK_GUARD_BLOCKED"
            )
            MODULE.append(state / "telegram-outbox.jsonl", {
                "event_uuid": "typed-sent", "kind": "BLOCKED", "body": body,
                "created_at": 1,
            })
            MODULE.append(state / "telegram-outbox.jsonl", {
                "event_uuid": "typed-pending", "kind": "BLOCKED", "body": body + "(free=1)",
                "created_at": 2,
            })
            MODULE.append(state / "telegram-sent.jsonl", {
                "event_uuid": "typed-sent", "message_id": "7644",
            })
            calls = []

            def should_not_send(command, **kwargs):
                calls.append(command)
                raise AssertionError("equivalent pending blocker must not be sent again")

            result = MODULE.flush_telegram(state, None, sender=should_not_send)

            self.assertEqual(result["state"], "NO_PENDING")
            self.assertEqual(calls, [])
            superseded = json.loads(
                (state / "telegram-superseded.jsonl").read_text().splitlines()[0]
            )
            self.assertEqual(superseded["superseded_event_uuid"], "typed-pending")
            self.assertEqual(superseded["canonical_event_uuid"], "typed-sent")
            self.assertEqual(superseded["reason"], "EQUIVALENT_REPORT_ALREADY_DELIVERED")

    def test_telegram_history_reconciliation_is_append_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            old = {"event_uuid": "old-event", "kind": "AFFILIATE_DAILY_SUMMARY"}
            MODULE.append(state / "telegram-outbox.jsonl", {
                **old, "body": "old", "created_at": 1,
            })
            MODULE.append(state / "telegram-sent.jsonl", {
                "event_uuid": "old-event", "message_id": "7642",
            })
            MODULE.append(state / "events.jsonl", {
                "receipt_type": "AFFILIATE_TELEGRAM_DELIVERY",
                "event_uuid": "misbound", "telegram_event_uuid": "other-event",
                "delivery_state": "SENT", "provider_message_id": "7642",
            })
            first = MODULE.reconcile_telegram_delivery_history(
                state, {"wake_event_uuid": "wake-1", "ts": 1},
            )
            second = MODULE.reconcile_telegram_delivery_history(
                state, {"wake_event_uuid": "wake-2", "ts": 2},
            )
            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])
            self.assertEqual(first[0]["telegram_event_uuid"], "old-event")
            self.assertEqual(first[0]["provider_message_id"], "7642")
            self.assertEqual(first[0]["superseded_receipt_event_uuids"], ["misbound"])

    def test_telegram_history_reconciliation_skips_unsubstantiated_old_sent_rows(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.append(state / "telegram-sent.jsonl", {
                "event_uuid": "old-event", "message_id": "7643",
            })
            self.assertEqual(
                MODULE.reconcile_telegram_delivery_history(
                    state, {"wake_event_uuid": "wake-1", "ts": 1},
                ),
                [],
            )

    def test_telegram_timeout_to_sent_creates_same_event_repair_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.append(state / "telegram-outbox.jsonl", {
                "event_uuid": "timeout-event", "kind": "BLOCKED", "body": "report",
                "created_at": 1,
            })
            MODULE.append(state / "telegram-sent.jsonl", {
                "event_uuid": "timeout-event", "message_id": "7645",
            })
            MODULE.append(state / "events.jsonl", {
                "receipt_type": "AFFILIATE_TELEGRAM_DELIVERY",
                "event_uuid": "timeout-delivery",
                "telegram_event_uuid": "timeout-event",
                "delivery_state": "SEND_TIMEOUT_UNKNOWN",
                "provider_message_id": None,
            })

            first = MODULE.reconcile_telegram_delivery_history(
                state, {"wake_event_uuid": "wake-1", "ts": 1},
            )
            second = MODULE.reconcile_telegram_delivery_history(
                state, {"wake_event_uuid": "wake-2", "ts": 2},
            )

            repair_rows = [
                json.loads(line)
                for line in (state / "repair-receipts.jsonl").read_text().splitlines()
            ]
            self.assertEqual(len(repair_rows), 1)
            repair = repair_rows[0]
            self.assertEqual(repair["outcome"], "SELF_HEALED")
            self.assertEqual(repair["repair"]["action"], "RESUME_SAME_TELEGRAM_SEND")
            self.assertEqual(repair["repair"]["same_telegram_event_uuid"], "timeout-event")
            self.assertEqual(repair["postcondition"]["provider_message_id"], "7645")
            self.assertTrue(first)
            self.assertEqual(second, [])

    def test_revenue_cycle_cooldown_is_independent_of_wake(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            MODULE.atomic_json(state / "revenue-cycle.json", {"completed_at": 1000})
            self.assertFalse(MODULE.revenue_cycle_due(state, now=4599))
            self.assertTrue(MODULE.revenue_cycle_due(state, now=4600))

    def test_new_affiliate_post_bypasses_revenue_cooldown_once(self):
        with tempfile.TemporaryDirectory() as root:
            state, repost = Path(root) / "affiliate", Path(root) / "repost"
            MODULE.atomic_json(state / "revenue-cycle.json", {"completed_at": 1000})
            MODULE.append(repost / "posted.jsonl", {
                "kind": "affiliate_distribution_quote",
                "posted_at": "1970-01-01T00:20:00+00:00",
                "affiliate_job_id": "1" * 64,
            })
            with patch.dict(MODULE.os.environ, {"AFFILIATE_REPOST_STATE_DIR": str(repost)}):
                self.assertTrue(MODULE.revenue_cycle_due(state, now=1250))
                MODULE.atomic_json(state / "revenue-cycle.json", {"completed_at": 1300})
                self.assertFalse(MODULE.revenue_cycle_due(state, now=1400))

    def test_revenue_failure_preserves_typed_retry_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            result = MODULE.revenue_failure(
                state, "capture", "NONZERO_EXIT", 1, "provider capture failed"
            )
            receipt = json.loads(
                (state / "revenue-cycle-failure.json").read_text()
            )
            self.assertEqual(result["failure_class"], "PROVIDER_TRANSIENT")
            self.assertEqual(result["retry_state"], "RETRYABLE")
            self.assertGreater(result["retry_after"], receipt["observed_at"])
            self.assertEqual(receipt["failure_class"], "PROVIDER_TRANSIENT")
            self.assertEqual(receipt["retry_state"], "RETRYABLE")

    def test_placement_receipt_is_exactly_once_and_hides_tracking_link(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            private = root / "affiliate-credentials.md"
            private.write_text(
                "# Affiliate Credentials (local only)\n\n"
                "## ElevenLabs\n"
                "- Default affiliate link: `https://try.elevenlabs.io/example`\n",
                encoding="utf-8",
            )
            private.chmod(0o600)
            args = Namespace(
                private_markdown=private, state=root / "state",
                placement="article-1", locale="en", print_url=False,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                MODULE.placement(args)
                MODULE.placement(args)
            rows = (args.state / "placements.jsonl").read_text().splitlines()
            emitted = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual([row["deduplicated"] for row in emitted], [False, True])
            self.assertNotIn("try.elevenlabs.io", output.getvalue())

    def test_settled_tts_api_placement_is_not_republished_to_x(self):
        import content as content_module
        import owned_publish as owned_module
        import x_post_cli

        slug = "elevenlabs-text-to-speech-api-for-developers"
        placement = "elevenlabs-tts-api-en-1"
        text = (
            "Building with a TTS API? Affiliate link in my checklist:\n"
            "https://aniccaai.com/blog/elevenlabs-text-to-speech-api-for-developers"
        )
        live_url = "https://x.com/selawmqt/status/2088809159932465497"
        for changed, expected in ((False, "ALREADY_LIVE"), (True, "X_LIVE")):
            with self.subTest(content_changed=changed), tempfile.TemporaryDirectory() as root:
                state = Path(root) / "state"
                (state / "sources" / "elevenlabs-api-pricing").mkdir(parents=True)
                (state / "sources" / "elevenlabs-api-pricing" / "latest.json").write_text("{}")
                MODULE.atomic_json(state / "program-links" / f"{slug}.json", {"state": "VERIFIED"})
                MODULE.atomic_json(state / "x-posts" / f"{placement}.json", {
                    "state": "LIVE", "public_url": live_url,
                    "content_sha256": x_post_cli.content_fingerprint(text),
                })
                built = text + (" updated" if changed else "")

                def write_x_content(target_state, _built=built):
                    path = target_state / "x-content" / f"{placement}.txt"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(_built + "\n", encoding="utf-8")

                publish_x = Mock(return_value={"state": "LIVE", "public_url": live_url})
                with (
                    patch.object(content_module, "build_tts_api"),
                    patch.object(content_module, "policy_tts_api"),
                    patch.object(content_module, "build_x_tts_api", side_effect=write_x_content),
                    patch.object(owned_module, "publish", return_value={
                        "state": "LIVE", "public_url": f"https://aniccaai.com/blog/{slug}",
                    }),
                    patch.object(x_post_cli, "publish", publish_x),
                ):
                    result = MODULE.advance_tts_api_publication(
                        state, Path(root) / "landing", 9326,
                        Path(root) / "private.md", live_url,
                    )
                self.assertEqual(result, {"state": expected, "public_url": live_url})
                self.assertEqual(publish_x.call_count, 1 if changed else 0)


    def test_failed_telegram_send_retries_instead_of_wedging_reporting(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            event = {"event_uuid": "e" * 64, "kind": "SELF_HEALED", "body": "報告"}
            attempts = []

            def failing(message, **kwargs):
                attempts.append((message, kwargs))
                return MODULE.SimpleNamespace(started=False, provider_id=None, error="unavailable")

            first = MODULE.flush_telegram(state, event, sender=failing)
            self.assertEqual(first["state"], "SEND_FAILED")
            # Previously the unresolved effect made every later wake report
            # RECONCILE_REQUIRED forever and the owner heard nothing again.
            second = MODULE.flush_telegram(state, None, sender=failing)
            self.assertEqual(second["state"], "SEND_FAILED")
            self.assertEqual(len(attempts), 2)

            def succeeding(message, **kwargs):
                attempts.append((message, kwargs))
                return MODULE.SimpleNamespace(started=True, provider_id="4242", error=None)

            third = MODULE.flush_telegram(state, None, sender=succeeding)
            self.assertEqual(third["state"], "SENT")
            self.assertEqual(third["message_id"], "4242")
            # A delivered message is never sent twice.
            fourth = MODULE.flush_telegram(state, None, sender=succeeding)
            self.assertEqual(fourth["state"], "NO_PENDING")
            self.assertEqual(len(attempts), 3)

    def test_recomposed_live_campaign_does_not_block_the_next_campaign(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            for plan_id, live in (("alpha-en", True), ("beta-en", False)):
                handoff = {
                    "receipt_type": "CAMPAIGN_HANDOFF", "state": "READY_FOR_POLICY",
                    "plan_id": plan_id, "locale": "en", "slug": f"{plan_id}-guide",
                    "source_set_sha256": "e" * 64, "title": "t", "buyer_intent": "b",
                    "cited_sources": [{"locator": "https://elevenlabs.io/x", "raw_sha256": "f" * 64}],
                    "owned_article_markdown": "disclosure\n{{AFFILIATE_LINK}}",
                    "x_copy": "copy {{OWNED_ARTICLE_URL}}", "disclosure": "disclosure",
                }
                fingerprint = hashlib.sha256(json.dumps(
                    handoff, sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                payload = {**handoff, "handoff_fingerprint": fingerprint}
                body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                path = state / "campaign-handoffs" / f"{plan_id}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                MODULE.atomic_json(state / "campaign-policy" / f"{plan_id}.json", {
                    "receipt_type": "GENERIC_CAMPAIGN_POLICY", "state": "PASS",
                    "decision": "PASS", "plan_id": plan_id, "locale": "en",
                    "handoff_sha256": hashlib.sha256(body).hexdigest(),
                    "handoff_fingerprint": fingerprint,
                    "source_set_sha256": "e" * 64,
                    "checks": {"ok": True},
                    "semantic_audit": {"decision": "PASS"},
                })
                if live:
                    # Already published, then recomposed: fingerprint has moved on
                    # and the policy receipt no longer matches its handoff.
                    MODULE.atomic_json(state / "campaign-publications" / f"{plan_id}.json", {
                        "state": "X_LIVE", "provider_link_key": "key-1",
                        "handoff_fingerprint": "0" * 64,
                    })
                    MODULE.atomic_json(state / "x-posts" / f"{plan_id}-1.json", {
                        "state": "LIVE", "public_url": "https://x.com/selawmqt/status/1",
                    })
                    MODULE.atomic_json(state / "campaign-policy" / f"{plan_id}.json", {
                        "receipt_type": "GENERIC_CAMPAIGN_POLICY", "state": "PASS",
                        "decision": "PASS", "plan_id": plan_id, "locale": "en",
                        "handoff_sha256": "9" * 64, "handoff_fingerprint": "0" * 64,
                        "source_set_sha256": "8" * 64, "checks": {"ok": True},
                        "semantic_audit": {"decision": "PASS"},
                    })

            link_acquirer = Mock(return_value={
                "state": "VERIFIED", "deduplicated": True,
                "private_link_field": "Placement example affiliate link",
                "provider_link_key": "key-2",
            })
            with patch.object(MODULE, "elevenlabs_link", return_value="https://try.example/x"):
                result = MODULE.advance_generic_publication(
                    state, Path(root) / "landing", 9326, Path(root) / "private.md",
                    owned_publisher=Mock(return_value={"state": "NOT_LIVE", "public_url": None}),
                    x_publisher=Mock(),
                    link_acquirer=link_acquirer,
                )
            # The live campaign no longer conflicts, so beta-en is actually reached.
            self.assertEqual(result["state"], "OWNED_NOT_LIVE")
            self.assertEqual(link_acquirer.call_args.args[3], "beta-en-1")

    def test_liveness_sweep_runs_once_per_jst_day_and_reports_a_dead_post(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            for placement, live in (("alpha-en-1", True), ("beta-en-1", False)):
                MODULE.atomic_json(state / "x-posts" / f"{placement}.json", {
                    "state": "LIVE", "public_url": f"https://x.com/selawmqt/status/{placement}",
                    "content_sha256": "d" * 64,
                })
                path = state / "x-content" / f"{placement}.txt"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{placement} copy\n", encoding="utf-8")
            # A placement with no built content is not guessed at.
            MODULE.atomic_json(state / "x-posts" / "gamma-en-1.json", {
                "state": "LIVE", "public_url": "https://x.com/selawmqt/status/gamma",
            })

            def publisher(args):
                if args.placement == "beta-en-1":
                    raise RuntimeError("published X post failed exact public readback")
                return {"state": "LIVE"}

            day = datetime(2026, 8, 17, 0, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
            first = MODULE.sweep_publication_liveness(
                state, 9326, now=day, publisher=publisher,
            )
            self.assertEqual(first["state"], "UNVERIFIED_PLACEMENTS")
            self.assertEqual(first["checked"], 2)
            self.assertEqual(
                [row["placement_id"] for row in first["unverified"]], ["beta-en-1"],
            )

            calls = []
            same_day = MODULE.sweep_publication_liveness(
                state, 9326, now=day.replace(hour=23),
                publisher=lambda args: calls.append(args.placement),
            )
            self.assertEqual(same_day["state"], "COOLDOWN")
            self.assertEqual(calls, [])

            next_day = MODULE.sweep_publication_liveness(
                state, 9326, now=day.replace(day=18), publisher=publisher,
            )
            self.assertEqual(next_day["checked"], 2)


if __name__ == "__main__":
    unittest.main()
