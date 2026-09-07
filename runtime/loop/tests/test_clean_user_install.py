import json
import os
import plistlib
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from runtime.loop.lm_loop_apply import build_apply_plan, install_one


ROOT = Path(__file__).resolve().parents[3]


class CleanUserInstallTest(unittest.TestCase):
    def test_writer_opportunity_discovery_wrapper_preserves_external_state_argv(self):
        wrapper = ROOT / "skills/writer-agent/scripts/opportunity-discovery-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/writer"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "WRITER_STATE_DIR": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/writer-agent/scripts/opportunity_discovery.py "
            f"--db {state_root}/opportunities.sqlite3 "
            f"--claims-db {state_root}/claims.sqlite3 "
            f"--receipt {state_root}/opportunity-discovery-latest.json",
        )

    def test_writer_money_sync_wrapper_preserves_external_state_argv(self):
        wrapper = ROOT / "skills/writer-agent/scripts/money-sync-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/writer"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "WRITER_STATE_DIR": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/writer-agent/scripts/money_sync.py "
            f"--state-dir {state_root} --db {state_root}/money.sqlite3",
        )

    def test_writer_claim_loop_wrapper_preserves_external_state_argv(self):
        wrapper = ROOT / "skills/writer-agent/scripts/claim-loop-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/writer"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "WRITER_STATE_DIR": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/writer-agent/scripts/claim_loop.py --state-dir {state_root}",
        )

    def test_hf_gig_paid_direct_wrapper_preserves_argv_and_target_registry(self):
        wrapper = ROOT / "skills/earn/gig/scripts/paid-direct-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        self.assertIn(
            'export CLOAK_TARGET_OWNERS_FILE="${CLOAK_TARGET_OWNERS_FILE:-$HOME/.cloak/vault/gig-target-owners.json}"',
            wrapper.read_text(),
        )
        result = subprocess.run(
            [str(wrapper)],
            env={**os.environ, "HF_GIG_PYTHON": "/bin/echo", "HOME": "/home/owner"},
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/runtime/host/memory_admission.py /bin/echo "
            f"{ROOT}/skills/earn/gig/scripts/gig_disk_guard.py /bin/echo "
            f"{ROOT}/skills/earn/gig/scripts/paid_direct.py "
            "--output /home/owner/gig/evidence/paid-direct-live/latest.json "
            "--evidence-dir /home/owner/gig/evidence/paid-direct-live "
            "--projects-root /home/owner/gig/projects "
            "--lock-file /home/owner/gig/.paid-direct.lock "
            "--cdp-lock-dir /home/owner/gig/.cdp-gig.lock",
        )
    def test_marketing_weekly_review_wrapper_preserves_behavior_and_external_state(self):
        wrapper = ROOT / "skills/earn/marketing-engine/intel/weekly-review-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/marketing-weekly-review"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_STATE_ROOT": state_root,
                "MARKETING_WEEKLY_REVIEW_EXECUTABLE": "/bin/echo",
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"intel gap --telegram --evidence-root {state_root}/evidence/intel/gaps",
        )

    def test_marketing_owner_events_wrapper_preserves_repo_and_home_argv(self):
        wrapper = ROOT / "skills/earn/marketing-engine/report/events-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/marketing-owner-events"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "LIFE_MANAGER_STATE_ROOT": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/earn/marketing-engine/report/truth_pipeline.py "
            f"--repo-root {ROOT} --home {Path.home()} --state-root {state_root}",
        )

    def test_marketing_metrics_wrapper_preserves_state_argv(self):
        wrapper = ROOT / "marketing/engine/bin/marketing-metrics-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/marketing"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_STATE_ROOT": state_root,
                "MARKETING_METRICS_EXECUTABLE": "/bin/echo",
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), f"observe --root {state_root}")

    def test_bootstrap_installs_the_locked_runtime_python_dependencies(self):
        requirements = (ROOT / "requirements-runtime.txt").read_text().splitlines()
        self.assertIn("jsonschema==4.26.0", requirements)
        self.assertIn("playwright==1.59.0", requirements)
        bootstrap = (ROOT / "scripts/bootstrap.sh").read_text()
        self.assertIn('-r "$TARGET/requirements-runtime.txt"', bootstrap)

    def test_crowdworks_wrapper_uses_the_managed_python_and_preserves_argv(self):
        wrapper = ROOT / "skills/earn/crowdworks/scripts/application-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        result = subprocess.run(
            [str(wrapper), "marker"],
            env={**os.environ, "LIFE_MANAGER_PYTHON": "/bin/echo"},
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/earn/crowdworks/scripts/application_owner.py marker",
        )

    def test_lancers_wrapper_uses_managed_python_state_and_exact_argv(self):
        wrapper = ROOT / "skills/earn/lancers/scripts/application-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/lancers"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "LIFE_MANAGER_STATE_ROOT": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/earn/lancers/scripts/application_loop.py "
            f"--json --exhaustive --state-path {state_root}/application.json",
        )

    def test_lancers_work_sync_wrapper_preserves_managed_state_argv(self):
        wrapper = ROOT / "skills/earn/lancers/scripts/work-sync-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/lancers"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "LIFE_MANAGER_STATE_ROOT": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/earn/lancers/scripts/work_sync.py "
            f"--json --state-path {state_root}/work-sync.json",
        )

    def test_lancers_negotiate_wrapper_preserves_managed_state_argv(self):
        wrapper = ROOT / "skills/earn/lancers/scripts/negotiate-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/lancers"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "LIFE_MANAGER_STATE_ROOT": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/earn/lancers/scripts/lane_report.py "
            f"--lane negotiate --state-path {state_root}/contracts.json",
        )

    def test_lancers_paid_wrapper_preserves_managed_state_argv(self):
        wrapper = ROOT / "skills/earn/lancers/scripts/paid-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/lancers"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "LIFE_MANAGER_STATE_ROOT": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/earn/lancers/scripts/lane_report.py "
            f"--lane paid --state-path {state_root}/contracts.json",
        )

    def test_lancers_storefront_wrapper_preserves_managed_state_argv(self):
        wrapper = ROOT / "skills/earn/lancers/scripts/storefront-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/lancers"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "LIFE_MANAGER_STATE_ROOT": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/earn/lancers/scripts/storefront_offer.py --apply "
            f"--product {ROOT}/skills/earn/lancers/products/monthly-sns-content-ops-v1.json "
            f"--state-path {state_root}/application.json",
        )

    def test_lancers_telegram_report_wrapper_preserves_managed_state_argv(self):
        wrapper = ROOT / "skills/earn/lancers/scripts/telegram-report-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        state_root = "/private/life-manager-state/lancers"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_PYTHON": "/bin/echo",
                "LIFE_MANAGER_STATE_ROOT": state_root,
            },
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/earn/lancers/scripts/telegram_report.py --json "
            f"--database {state_root}/telegram.sqlite3 "
            f"--ledger-database {state_root}/marketplace-ledger.sqlite3 "
            f"--state-path {state_root}/application.json "
            f"--application-log {state_root}/logs/application.out.log "
            f"--storefront-log {state_root}/logs/storefront.stdout.log",
        )

    def test_public_archive_contains_general_agent_release_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            release.mkdir()
            archive = root / "release.tar"
            tree = subprocess.run(
                ["git", "write-tree"], cwd=ROOT, check=True,
                capture_output=True, text=True).stdout.strip()
            subprocess.run(
                [
                    "git", "archive", "--format=tar", "-o", str(archive), tree,
                    "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md",
                    "apps/life-manager/.env.example",
                    "skills/earn/gig/config/provider-capability.example.json",
                ],
                cwd=ROOT, check=True)
            with tarfile.open(archive) as handle:
                handle.extractall(release)

            manifest = json.loads((
                release / "skills/earn/gig/config/provider-capability.example.json"
            ).read_text())
            self.assertEqual(manifest["capability"], "marketplace.application")
            self.assertEqual(manifest["effect"]["replay"], "zero")

            env_lines = (release / "apps/life-manager/.env.example").read_text().splitlines()
            refs = [line.split("=", 1)[1] for line in env_lines
                    if line and not line.startswith("#") and line.split("=", 1)[0].endswith("_REF")]
            self.assertTrue(refs)
            self.assertTrue(all(value.startswith("secret://") for value in refs))

            readme = (release / "README.md").read_text()
            self.assertIn("### Use it — cloud", readme)
            self.assertTrue((release / "LICENSE").is_file())

            notices = (release / "THIRD_PARTY_NOTICES.md").read_text()
            for project, license_name in (
                ("DeepAgentsJS", "MIT"),
                ("browser-use", "MIT"),
                ("OpenClaw", "MIT"),
                ("Steel Browser", "Apache-2.0"),
            ):
                self.assertIn(project, notices)
                self.assertIn(license_name, notices)
            self.assertIn("No source code from these projects is vendored", notices)

    def test_clean_user_installs_every_generated_job_without_starting_workloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            release.mkdir()
            archive = root / "release.tar"
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                capture_output=True, text=True).stdout.strip()
            subprocess.run(
                ["git", "archive", "--format=tar", "-o", str(archive), sha],
                cwd=ROOT, check=True)
            with tarfile.open(archive) as handle:
                handle.extractall(release)
            (release / "RELEASE.json").write_text(json.dumps({"sha": sha}))
            for dependency in ("playwright-core", "jsqr"):
                package = release / "apps/life-manager/node_modules" / dependency / "package.json"
                package.parent.mkdir(parents=True)
                package.write_text("{}\n")
            registry = json.loads((release / "config/loop-registry.json").read_text())
            plan = build_apply_plan(registry, release, sha)
            agents = root / "home/Library/LaunchAgents"
            loaded = {}

            def launchctl(args):
                action = args[0]
                if action == "print":
                    label = args[1].rsplit("/", 1)[-1]
                    argv = loaded.get(label)
                    if argv is None:
                        return 1, "not loaded"
                    return 0, "arguments = {\n" + "\n".join(argv) + "\n}\n"
                if action == "bootout":
                    loaded.pop(args[1].rsplit("/", 1)[-1], None)
                    return 0, ""
                if action == "bootstrap":
                    with open(args[2], "rb") as handle:
                        plist = plistlib.load(handle)
                    loaded[plist["Label"]] = list(map(str, plist["ProgramArguments"]))
                    return 0, ""
                raise AssertionError(args)

            results = []
            for item in plan:
                results.append(install_one(
                    item, agents / f"{item['label']}.plist", launchctl,
                    attempts=1, sleeper=lambda _seconds: None))

            self.assertEqual(len(results), len(registry["loops"]))
            self.assertTrue(all(row["ok"] for row in results))
            self.assertEqual(len(list(agents.glob("*.plist"))), len(registry["loops"]))
            self.assertEqual(len(loaded), len(registry["loops"]))


if __name__ == "__main__":
    unittest.main()
