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
    def test_installer_prepares_locked_compute_proxy_before_daemon_setup(self):
        source = (ROOT / "install.sh").read_text()
        dependency = '(cd "$REPO_ROOT/runtime/compute-proxy" && npm ci --no-audit --no-fund)'
        self.assertIn(dependency, source)
        self.assertLess(source.index(dependency), source.index("[5/6] daemon registration"))
        self.assertNotIn('runtime/compute-proxy" && npm install', source)
        self.assertIn('"$REPO_ROOT/bin/cut-loop-release.sh" HEAD', source)
        self.assertIn('LIFE_MANAGER_APPLY_TARGET=compute-proxy', source)
        self.assertIn('runtime/bootstrap-local-citizen.cjs', source)
        self.assertIn('LIFE_MANAGER_APPLY_TARGET=agent-economy-loop', source)
        self.assertIn('status agent-economy-loop', source)
        self.assertIn('runtime/install-agent-economy-systemd.sh', source)
        self.assertLess(
            source.index('runtime/bootstrap-local-citizen.cjs'),
            source.index('LIFE_MANAGER_APPLY_TARGET=agent-economy-loop'),
        )
        self.assertNotIn('launchctl load', source)
        self.assertNotIn('launchctl bootstrap', source)
        self.assertNotIn("com.anicca.daemon.plist", source)
        self.assertFalse((ROOT / "runtime/com.anicca.daemon.plist.template").exists())

    def test_agent_economy_accepts_the_common_immutable_release_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sha = "a" * 40
            release = root / "releases" / f"20260909T000000-{sha[:8]}"
            launcher = release / "skills/agent-economy/launch.sh"
            launcher.parent.mkdir(parents=True)
            launcher.write_bytes((ROOT / "skills/agent-economy/launch.sh").read_bytes())
            launcher.chmod(0o555)
            daemon = release / "runtime/anicca-daemon.sh"
            daemon.parent.mkdir(parents=True)
            daemon.write_text("#!/bin/bash\nexit 0\n")
            daemon.chmod(0o555)
            metadata = release / "RELEASE.json"
            metadata.write_text(json.dumps({
                "sha": sha,
                "ref": "origin/main",
                "provenance": "ancestor-of-origin-main",
                "release_paths": "ALL",
            }))
            metadata.chmod(0o444)
            env = {
                **os.environ,
                "ANICCA_RELEASE_ROOT": str(root.resolve()),
                "ANICCA_REPO": str(release.resolve()),
                "ANICCA_VALIDATE_RELEASE_ONLY": "1",
            }

            accepted = subprocess.run(
                [str(launcher)], env=env, capture_output=True, text=True)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertIn(release.name, accepted.stdout)

            normal_env = {**env, "ANICCA_ECONOMY_CREATE_EVM_WALLET": "1"}
            normal_env.pop("ANICCA_VALIDATE_RELEASE_ONLY")
            normal = subprocess.run(
                [str(launcher)], env=normal_env, capture_output=True, text=True)
            self.assertEqual(normal.returncode, 0, normal.stderr)

            metadata.chmod(0o644)
            writable = subprocess.run(
                [str(launcher)], env=env, capture_output=True, text=True)
            self.assertEqual(writable.returncode, 2)
            self.assertIn("sealed release metadata is invalid", writable.stderr)

            metadata.write_text(json.dumps({"sha": "b" * 40}))
            metadata.chmod(0o444)
            mismatched = subprocess.run(
                [str(launcher)], env=env, capture_output=True, text=True)
            self.assertEqual(mismatched.returncode, 2)
            self.assertIn("sealed release metadata is invalid", mismatched.stderr)

    def test_ceo_runner_uses_repository_owned_agent_boundary(self):
        wrapper = (ROOT / "bin/ceo-run.sh").read_text()
        self.assertIn(
            'RUN_AGENT="${CEO_RUN_AGENT_BIN:-$HERE/skills/earn/marketing-engine/run_agent.sh}"',
            wrapper,
        )
        self.assertNotIn(
            "$HOME/" + "anicca/skills/earn/marketing-engine/run_agent.sh",
            wrapper,
        )
        self.assertTrue(
            (ROOT / "skills/earn/marketing-engine/run_agent.sh").is_file()
        )

    def test_writer_report_wrapper_preserves_external_state_argv(self):
        wrapper = ROOT / "skills/writer-agent/scripts/writer-report-owner"
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
            f"{ROOT}/skills/writer-agent/scripts/writer_report_worker.py "
            f"--state-dir {state_root}",
        )

    def test_writer_opportunity_response_wrapper_restores_account_and_state_argv(self):
        wrapper = ROOT / "skills/writer-agent/scripts/opportunity-response-owner"
        self.assertTrue(os.access(wrapper, os.X_OK))
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("LIFE_MANAGER_GMAIL_ACCOUNT=owner@example.test\n")
            state_root = "/private/life-manager-state/writer"
            result = subprocess.run(
                [str(wrapper)],
                env={
                    **os.environ,
                    "LIFE_MANAGER_PYTHON": "/bin/echo",
                    "WRITER_STATE_DIR": state_root,
                    "LIFE_MANAGER_ENV_FILE": str(env_file),
                    "WRITER_GMAIL_ACCOUNT": "",
                    "LIFE_MANAGER_GMAIL_ACCOUNT": "",
                    "GOG_ACCOUNT": "",
                },
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertEqual(
            result.stdout.strip(),
            f"{ROOT}/skills/writer-agent/scripts/opportunity_response.py "
            f"--db {state_root}/opportunities.sqlite3 "
            f"--receipt {state_root}/opportunity-response-latest.json "
            "--account owner@example.test",
        )

    def test_writer_opportunity_response_wrapper_fails_without_account(self):
        wrapper = ROOT / "skills/writer-agent/scripts/opportunity-response-owner"
        result = subprocess.run(
            [str(wrapper)],
            env={
                **os.environ,
                "LIFE_MANAGER_ENV_FILE": "/nonexistent/life-manager.env",
                "WRITER_GMAIL_ACCOUNT": "",
                "LIFE_MANAGER_GMAIL_ACCOUNT": "",
                "GOG_ACCOUNT": "",
            },
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


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
        self.assertIn("cloakbrowser==0.5.6", requirements)
        self.assertIn("Pillow==12.2.0", requirements)
        self.assertIn("cryptography==46.0.5", requirements)
        self.assertIn("websocket-client==1.9.0", requirements)
        self.assertIn("PyYAML==6.0.3", requirements)
        self.assertIn("httpx==0.28.1", requirements)
        self.assertIn("keyring==25.7.0", requirements)
        self.assertIn("markdown-it-py==3.0.0", requirements)
        self.assertIn("pydantic==2.12.5", requirements)
        self.assertIn("polymarket-client==0.1.0b13", requirements)
        self.assertIn("eth-account==0.13.7", requirements)
        self.assertIn("requests==2.34.2", requirements)
        self.assertIn("web3==7.16.0", requirements)
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
            f"{ROOT}/skills/_shared/marketplace-core/scripts/reply_kernel.py "
            f"--provider-adapter {ROOT}/skills/earn/lancers/scripts/reply_adapter.py "
            f"--state-root {state_root}/reply --output {state_root}/reply/latest.json "
            f"--max-workers 1 -- --state-path {state_root}/work-sync.json",
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
        self.assertEqual(result.stdout.strip().splitlines(), [
            f"{ROOT}/skills/_shared/marketplace-core/scripts/paid_kernel.py "
            f"--provider-adapter {ROOT}/skills/earn/lancers/scripts/paid_adapter.py "
            f"--state-root {state_root}/paid --output {state_root}/paid-latest.json "
            f"-- --account-id keiodaisuke --state-path {state_root}/application.json",
            f"{ROOT}/skills/earn/lancers/scripts/lane_report.py "
            f"--lane paid --state-path {state_root}/contracts.json",
        ])

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
