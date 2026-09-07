import json
import copy
import re
import time
import unittest
from pathlib import Path

from runtime.loop.macos_loop_registry import (
    loop_json_schema,
    render_job_models,
    render_loop_json_schema,
    validate_registry,
)
from runtime.loop.lm_loop import status_rows


ROOT = Path(__file__).resolve().parents[3]


def entry(label="ai.anicca.example"):
    return {
        "label": label,
        "domain": "system",
        "entrypoint": "bin/example.sh",
        "cadence": {"run_at_load": True},
        "effect_class": "none",
        "state_root": "~/.local/state/life-manager/example",
        "log_root": "~/.local/state/life-manager/example/logs",
        "cleanup": {"max_runs": 10, "max_age_days": 7},
        "provider_route": "deterministic",
    }


def browser_entry(label: str, profile: str, port: int):
    value = entry(label)
    value["browser_owner"] = {"profile": profile, "cdp_port": port}
    return value


class MacosLoopRegistryTest(unittest.TestCase):
    def test_boot_panic_evidence_runs_once_when_the_aqua_session_loads(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        self.assertEqual(registry["loops"]["boot-panic-evidence"], {
            "cadence": {"run_at_load": True},
            "cleanup": {"max_age_days": 30, "max_runs": 20},
            "domain": "system",
            "effect_class": "none",
            "entrypoint": "runtime/host/boot_panic_collector.py",
            "label": "ai.anicca.boot-panic-evidence",
            "log_root": "~/.local/state/life-manager/boot-panic-evidence/logs",
            "provider_route": "deterministic",
            "state_root": "~/.local/state/life-manager/boot-panic-evidence",
        })

    def test_money_printer_symphony_is_retired_after_cloud_cutover(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        self.assertNotIn("money-printer-symphony", registry["loops"])
        self.assertIn("ai.anicca.life-manager-money-printer-symphony", registry["retired_labels"])

    def test_money_printer_symphony_bridge_is_retired_after_cloud_cutover(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        self.assertNotIn("money-printer-symphony-bridge", registry["loops"])
        self.assertIn("ai.anicca.life-manager-money-printer-symphony-bridge", registry["retired_labels"])

    def test_legacy_telegram_bot_is_retired_after_gateway_cutover(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        self.assertNotIn("telegram-bot", registry["loops"])
        self.assertIn("ai.anicca.telegram-bot", registry["retired_labels"])

    def test_unused_job_search_browser_is_retired_after_shared_owner_readback(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        self.assertNotIn("job-search-browser", registry["loops"])
        self.assertIn("ai.anicca.job-search-browser", registry["retired_labels"])

    def test_release_reconciler_is_an_independent_system_owner(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["life-manager-release-reconciler"]
        self.assertEqual(row, {
            "cadence": {"start_interval_seconds": 60},
            "cleanup": {"max_age_days": 14, "max_runs": 100},
            "domain": "system",
            "effect_class": "none",
            "entrypoint": "bin/reconcile-agent-runner-release.sh",
            "label": "ai.anicca.life-manager-release-reconciler",
            "log_root": "~/.local/state/life-manager/release-reconciler/logs",
            "provider_route": "deterministic",
            "state_root": "~/.local/state/life-manager/release-reconciler",
        })
        self.assertEqual(validate_registry(registry), registry)

    def test_release_reconciler_scopes_each_route_to_the_four_gig_owners(self):
        script = (ROOT / "bin/reconcile-agent-runner-release.sh").read_text()
        self.assertIn(
            "reconcile shared-agent-runner --loaded-idle-only "
            "--loop-id hf-gig-apply-direct",
            script,
        )
        self.assertIn(
            "reconcile shared-agent-runner --include-running "
            "--loop-id hf-gig-reply-detector",
            script,
        )
        self.assertIn(
            "reconcile deterministic --loaded-idle-only "
            "--loop-id hf-gig-storefront-direct --loop-id hf-gig-paid-direct "
            "--loop-id life-manager-disk-cleanup",
            script,
        )

    def test_registry_rejects_missing_and_secret_fields(self):
        missing = {"schema_version": 2, "loops": {"example": entry()}}
        del missing["loops"]["example"]["cleanup"]
        with self.assertRaisesRegex(ValueError, "cleanup"):
            validate_registry(missing)

        secret = {"schema_version": 2, "loops": {"example": entry()}}
        secret["loops"]["example"]["auth_token"] = "not-a-real-secret"
        with self.assertRaisesRegex(ValueError, "secret-like"):
            validate_registry(secret)

    def test_command_and_adapter_are_validated_as_one_contract(self):
        value = entry()
        value.update({"adapter": "python", "command": ["dashboard"]})
        self.assertEqual(
            validate_registry({"schema_version": 2, "loops": {"example": value}})["loops"]["example"],
            value,
        )
        executable = entry()
        executable.update({"adapter": "exec", "command": ["sources", "wake"]})
        self.assertEqual(
            validate_registry({"schema_version": 2, "loops": {"example": executable}})["loops"]["example"],
            executable,
        )
        no_arguments = entry()
        no_arguments.update({"adapter": "python", "command": []})
        self.assertEqual(
            validate_registry({"schema_version": 2, "loops": {"example": no_arguments}})["loops"]["example"],
            no_arguments,
        )
        for adapter, command in ((None, ["dashboard"]), ("python", None),
                                 ("shell", ["dashboard"]),
                                 ("python", [""])):
            invalid = entry()
            if adapter is not None:
                invalid["adapter"] = adapter
            if command is not None:
                invalid["command"] = command
            with self.subTest(adapter=adapter, command=command), self.assertRaises(ValueError):
                validate_registry({"schema_version": 2, "loops": {"example": invalid}})
        explicit_null = entry()
        explicit_null.update({"adapter": None, "command": None})
        with self.assertRaises(ValueError):
            validate_registry({"schema_version": 2, "loops": {"example": explicit_null}})

    def test_runtime_timeout_is_positive_and_scheduled_only(self):
        value = entry()
        value["runtime_timeout_seconds"] = 10800
        self.assertEqual(
            validate_registry({"schema_version": 2, "loops": {"example": value}})["loops"]["example"],
            value,
        )
        for timeout in (None, 0, -1, True, 1.5, "10800"):
            invalid = entry()
            invalid["runtime_timeout_seconds"] = timeout
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                validate_registry({"schema_version": 2, "loops": {"example": invalid}})
        continuous = entry()
        continuous["cadence"] = {"keep_alive": True}
        continuous["runtime_timeout_seconds"] = 10800
        with self.assertRaises(ValueError):
            validate_registry({"schema_version": 2, "loops": {"example": continuous}})

        self.assertEqual(loop_json_schema()["allOf"], [{
            "not": {
                "required": ["runtime_timeout_seconds"],
                "properties": {"cadence": {"required": ["keep_alive"]}},
            },
        }])

    def test_marketing_dashboard_uses_direct_python_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["marketing-dashboard"]
        self.assertEqual(row["adapter"], "python")
        self.assertEqual(row["command"], ["dashboard"])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/marketing-engine/report/scheduled_runner.py",
        )

    def test_marketing_mine_daily_uses_direct_python_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["marketing-mine-daily"]
        self.assertEqual(row["adapter"], "python")
        self.assertEqual(row["command"], ["mine"])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/marketing-engine/report/scheduled_runner.py",
        )

    def test_affiliate_source_refresh_uses_direct_exec_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["affiliate-source-refresh"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], ["sources", "wake"])
        self.assertEqual(row["entrypoint"], "skills/affiliate/affiliate")
        self.assertEqual(row["runtime_timeout_seconds"], 10800)

    def test_affiliate_loop_uses_direct_exec_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["affiliate-loop"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], ["loop", "wake"])
        self.assertEqual(row["entrypoint"], "skills/affiliate/affiliate")

    def test_affiliate_browser_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["affiliate-browser"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(row["entrypoint"], "skills/affiliate/scripts/local-browser")

    def test_affiliate_impact_browser_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["affiliate-impact-browser"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(row["entrypoint"], "skills/affiliate/scripts/local-browser")

    def test_affiliate_x_browser_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["affiliate-x-browser"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(row["entrypoint"], "skills/affiliate/scripts/local-browser")

    def test_daily_driver_uses_shared_owned_browser_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["life-manager-daily-driver"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(row["entrypoint"], "skills/browser/owned-persistent-context")
        self.assertEqual(row["browser_owner"], {
            "cdp_port": 9222,
            "profile": "~/.cloak/profiles/daily-driver",
        })

    def test_affiliate_composition_uses_direct_exec_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["affiliate-composition"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], ["compose", "wake"])
        self.assertEqual(row["entrypoint"], "skills/affiliate/affiliate")

    def test_crowdworks_application_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["crowdworks-revenue-application"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/crowdworks/scripts/application-owner",
        )

    def test_crowdworks_report_uses_direct_python_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["crowdworks-revenue-report"]
        self.assertEqual(row["adapter"], "python")
        self.assertEqual(row["command"], ["--json"])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/crowdworks/scripts/telegram_report.py",
        )

    def test_lancers_application_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["lancers-revenue-application"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/lancers/scripts/application-owner",
        )

    def test_lancers_work_sync_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["lancers-revenue-work-sync"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/lancers/scripts/work-sync-owner",
        )

    def test_lancers_negotiate_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["lancers-revenue-negotiate"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/lancers/scripts/negotiate-owner",
        )

    def test_lancers_paid_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["lancers-revenue-paid"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/lancers/scripts/paid-owner",
        )

    def test_lancers_storefront_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["lancers-revenue-storefront"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/lancers/scripts/storefront-owner",
        )

    def test_lancers_telegram_report_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["lancers-revenue-telegram-report"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/lancers/scripts/telegram-report-owner",
        )

    def test_marketing_metrics_daily_uses_direct_python_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["marketing-metrics-daily"]
        self.assertEqual(row["adapter"], "python")
        self.assertEqual(row["command"], ["metrics"])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/marketing-engine/report/scheduled_runner.py",
        )

    def test_marketing_metrics_uses_repo_owned_exec_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["marketing-metrics"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "marketing/engine/bin/marketing-metrics-owner",
        )

    def test_marketing_owner_events_uses_repo_managed_runtime_python(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["marketing-owner-events"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/marketing-engine/report/events-owner",
        )

    def test_marketing_weekly_review_uses_repo_owned_exec_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["marketing-weekly-review"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/marketing-engine/intel/weekly-review-owner",
        )

    def test_hf_gig_paid_direct_uses_repo_owned_exec_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["hf-gig-paid-direct"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(row["entrypoint"], "skills/earn/gig/scripts/paid-direct-owner")

    def test_writer_claim_loop_uses_repo_owned_exec_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["writer-claim-loop"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/writer-agent/scripts/claim-loop-owner",
        )

    def test_writer_money_sync_uses_repo_owned_exec_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["writer-money-sync"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/writer-agent/scripts/money-sync-owner",
        )

    def test_writer_opportunity_discovery_uses_repo_owned_exec_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["writer-opportunity-discovery"]
        self.assertEqual(row["adapter"], "exec")
        self.assertEqual(row["command"], [])
        self.assertEqual(
            row["entrypoint"],
            "skills/writer-agent/scripts/opportunity-discovery-owner",
        )

    def test_marketing_score_daily_uses_direct_python_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["marketing-score-daily"]
        self.assertEqual(row["adapter"], "python")
        self.assertEqual(row["command"], ["score"])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/marketing-engine/report/scheduled_runner.py",
        )

    def test_self_improve_evolve_uses_direct_python_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["self-improve-evolve"]
        self.assertEqual(row["adapter"], "python")
        self.assertEqual(row["command"], ["self-improve"])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/marketing-engine/report/scheduled_runner.py",
        )

    def test_clip_loop_uses_direct_python_adapter(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["clip-loop"]
        self.assertEqual(row["adapter"], "python")
        self.assertEqual(row["command"], ["clip"])
        self.assertEqual(
            row["entrypoint"],
            "skills/earn/marketing-engine/report/scheduled_runner.py",
        )

    def test_external_labels_are_explicit_and_cannot_overlap_managed(self):
        value = {"schema_version": 2, "loops": {"example": entry()},
                 "external_labels": ["ai.anicca.tsbridge"]}
        self.assertEqual(validate_registry(value), value)
        value["external_labels"] = ["ai.anicca.example"]
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate_registry(value)

    def test_browser_owner_contract_accepts_unique_profile_and_port(self):
        value = {
            "schema_version": 2,
            "loops": {
                "first": browser_entry("ai.anicca.first", "~/.cloak/profiles/first", 9222),
                "second": browser_entry("ai.anicca.second", "~/.cloak/profiles/second", 9223),
            },
        }
        self.assertEqual(validate_registry(value), value)

    def test_browser_owner_contract_rejects_duplicate_profile_or_port(self):
        duplicate_profile = {
            "schema_version": 2,
            "loops": {
                "first": browser_entry("ai.anicca.first", "~/.cloak/profiles/shared", 9222),
                "second": browser_entry("ai.anicca.second", "~/.cloak/profiles/shared", 9223),
            },
        }
        with self.assertRaisesRegex(ValueError, "duplicate browser profile"):
            validate_registry(duplicate_profile)
        duplicate_port = copy.deepcopy(duplicate_profile)
        duplicate_port["loops"]["second"]["browser_owner"] = {
            "profile": "~/.cloak/profiles/second", "cdp_port": 9222,
        }
        with self.assertRaisesRegex(ValueError, "duplicate browser CDP port"):
            validate_registry(duplicate_port)

    def test_render_is_byte_stable_for_loop_insertion_order(self):
        left = {"schema_version": 2, "loops": {"b": entry("ai.anicca.b"), "a": entry("ai.anicca.a")}}
        right = {"schema_version": 2, "loops": {"a": entry("ai.anicca.a"), "b": entry("ai.anicca.b")}}
        self.assertEqual(render_job_models(left), render_job_models(right))

    def test_registry_covers_every_active_owned_inventory_label(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        inventory = json.loads((ROOT / "docs/evidence/runtime/2026-08-28-macos-loop-control-plane-inventory.json").read_text())
        validate_registry(registry)
        expected = {
            row["label"] for row in inventory["labels"]
            if row["installed"] and row["owner"] == "life-manager"
            and row["launchd_state"].startswith("loaded")
        }
        expected -= set(registry.get("retired_labels", []))
        self.assertTrue(expected.issubset({row["label"] for row in registry["loops"].values()}))
        self.assertEqual(registry["loops"]["pm-live-trade"]["effect_class"], "trade")
        self.assertEqual(registry["loops"]["life-manager-payout"]["effect_class"], "money")
        self.assertEqual(registry["loops"]["life-manager-honne-ja"]["effect_class"], "publish")
        self.assertEqual(registry["loops"]["agentmail-replier"]["domain"], "earn")
        self.assertEqual(registry["loops"]["phone-conversation"]["domain"], "physical")
        self.assertEqual(registry["loops"]["x-repost"]["label"], "ai.anicca.x-repost-pass")
        self.assertEqual(registry["loops"]["x-tweeter"]["label"], "ai.anicca.x-tweeter-pass")
        self.assertEqual(registry["loops"]["x-tweeter"]["cadence"],
                         {"calendar_interval": {"Minute": 15}})

    def test_production_render_matches_byte_stable_fixture(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        expected = (ROOT / "runtime/loop/tests/fixtures/macos-loop-jobs.json").read_bytes()
        self.assertEqual(render_job_models(registry), expected)

    def test_loop_json_schema_is_generated_from_the_registry_contract(self):
        schema_path = ROOT / "runtime/loop/loop.schema.json"
        self.assertEqual(schema_path.read_bytes(), render_loop_json_schema())
        schema = json.loads(schema_path.read_text())
        self.assertEqual(schema["required"], ["loop_id", *sorted(entry())])
        self.assertEqual(schema["properties"]["domain"]["enum"], [
            "earn", "financial", "growth", "mental", "physical", "system",
        ])
        self.assertEqual(schema["properties"]["effect_class"]["enum"], [
            "account_mutation", "application", "message", "money", "none", "publish", "trade",
        ])
        self.assertEqual(schema["properties"]["adapter"]["enum"], ["exec", "python"])
        self.assertEqual(schema["properties"]["command"]["items"], {
            "type": "string", "minLength": 1,
        })
        self.assertFalse(schema["additionalProperties"])

    def test_registry_and_loop_schema_share_boundary_constraints(self):
        schema = json.loads(render_loop_json_schema())
        self.assertEqual(
            schema["properties"]["entrypoint"]["pattern"],
            r"^(?!/)(?!(?:\./)*\.?$)(?!.*(?:^|/)\.\.(?:/|$)).+$",
        )
        self.assertEqual(
            schema["properties"]["browser_owner"]["properties"]["profile"]["pattern"],
            r"^~/(?!\.\.(?:/|$))(?!.*\/\.\.(?:/|$)).+",
        )
        browser_pattern = schema["properties"]["browser_owner"]["properties"]["profile"]["pattern"]
        self.assertIsNotNone(re.fullmatch(browser_pattern, "~/.cloak/profiles/example"))
        self.assertIsNone(re.fullmatch(browser_pattern, "~/../shared"))
        self.assertIsNone(re.fullmatch(browser_pattern, "~/.cloak/../shared"))
        invalid = []
        for entrypoint in ("./", "bin/../other.sh"):
            value = entry()
            value["entrypoint"] = entrypoint
            invalid.append(value)
        for field in ("state_root", "log_root"):
            value = entry()
            value[field] = "~/"
            invalid.append(value)
        value = entry()
        value["cadence"] = {"start_interval_seconds": True}
        invalid.append(value)
        value = entry()
        value["cleanup"]["max_runs"] = True
        invalid.append(value)
        value = entry()
        value["browser_owner"] = None
        invalid.append(value)
        value = browser_entry("ai.anicca.example", "~/.cloak/../shared", 9222)
        invalid.append(value)
        value = browser_entry("ai.anicca.example", "~/../shared", 9222)
        invalid.append(value)
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                validate_registry({"schema_version": 2, "loops": {"example": row}})

    def test_loop_entrypoints_do_not_select_auth_or_codex_home(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        forbidden = re.compile(r"CODEX_HOME|auth\.json|AGENT_RUNNER_PROVIDER")
        violations = []
        for loop_id, entry in registry["loops"].items():
            path = ROOT / entry["entrypoint"]
            if path.is_file() and forbidden.search(path.read_text(errors="replace")):
                violations.append((loop_id, entry["entrypoint"]))
        self.assertEqual(violations, [])

    def test_active_entrypoints_do_not_depend_on_other_worktrees(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        forbidden = re.compile(r"/" + r"Users/[^/]+/.*(?:\.worktrees|/Projects/|/profitable-claude)")
        violations = []
        for loop_id, entry in registry["loops"].items():
            path = ROOT / entry["entrypoint"]
            if path.is_file() and forbidden.search(path.read_text(errors="replace")):
                violations.append((loop_id, entry["entrypoint"]))
        self.assertEqual(violations, [])

    def test_render_500_loops_and_status_under_five_seconds(self):
        base = entry()
        loops = {}
        for index in range(500):
            loop_id = f"scale-{index:03d}"
            row = copy.deepcopy(base)
            row["label"] = f"ai.anicca.{loop_id}"
            row["state_root"] = f"~/.local/state/life-manager/{loop_id}"
            row["log_root"] = f"~/.local/state/life-manager/{loop_id}/logs"
            loops[loop_id] = row
        registry = {"schema_version": 2, "loops": loops}

        started = time.perf_counter()
        rendered = render_job_models(registry)
        render_seconds = time.perf_counter() - started
        started = time.perf_counter()
        rows = status_rows(
            registry, loaded={}, disabled={}, events={}, installed_releases={})
        status_seconds = time.perf_counter() - started

        self.assertEqual((len(rendered.splitlines()), len(rows)), (1, 500))
        self.assertLess(render_seconds, 5)
        self.assertLess(status_seconds, 5)


if __name__ == "__main__":
    unittest.main()
