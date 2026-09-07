import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.loop import entry_dispatch
from runtime.loop.entry_dispatch import command_for


class EntryDispatchTest(unittest.TestCase):
    def test_marketing_dashboard_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('marketing-dashboard', Path('/release'), Path('/home'))

    def test_marketing_metrics_daily_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('marketing-metrics-daily', Path('/release'), Path('/home'))

    def test_marketing_metrics_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('marketing-metrics', Path('/release'), Path('/home'))

    def test_marketing_owner_events_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('marketing-owner-events', Path('/release'), Path('/home'))

    def test_marketing_weekly_review_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('marketing-weekly-review', Path('/release'), Path('/home'))

    def test_marketing_score_daily_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('marketing-score-daily', Path('/release'), Path('/home'))

    def test_self_improve_evolve_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('self-improve-evolve', Path('/release'), Path('/home'))

    def test_clip_loop_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('clip-loop', Path('/release'), Path('/home'))

    def test_affiliate_source_refresh_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('affiliate-source-refresh', Path('/release'), Path('/home'))

    def test_affiliate_composition_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('affiliate-composition', Path('/release'), Path('/home'))

    def test_crowdworks_application_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('crowdworks-revenue-application', Path('/release'), Path('/home'))

    def test_crowdworks_report_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('crowdworks-revenue-report', Path('/release'), Path('/home'))

    def test_affiliate_browser_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('affiliate-browser', Path('/release'), Path('/home'))

    def test_affiliate_impact_browser_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('affiliate-impact-browser', Path('/release'), Path('/home'))

    def test_affiliate_x_browser_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('affiliate-x-browser', Path('/release'), Path('/home'))

    def test_affiliate_loop_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('affiliate-loop', Path('/release'), Path('/home'))

    def test_marketing_mine_daily_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('marketing-mine-daily', Path('/release'), Path('/home'))

    def test_lancers_application_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('lancers-revenue-application', Path('/release'), Path('/home'))

    def test_lancers_work_sync_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('lancers-revenue-work-sync', Path('/release'), Path('/home'))

    def test_lancers_negotiate_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('lancers-revenue-negotiate', Path('/release'), Path('/home'))

    def test_lancers_paid_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('lancers-revenue-paid', Path('/release'), Path('/home'))

    def test_lancers_storefront_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('lancers-revenue-storefront', Path('/release'), Path('/home'))

    def test_lancers_telegram_report_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('lancers-revenue-telegram-report', Path('/release'), Path('/home'))

    def test_lancers_browser_has_a_finite_renderer_process_limit(self):
        script = Path(__file__).parents[3] / 'runtime/legacy/lancers-revenue-browser/run.sh'
        self.assertIn('--renderer-process-limit="$renderer_limit"', script.read_text())

    def _symphony_fixture(self, home: Path, content: bytes = b"symphony fixture") -> Path:
        artifact_dir = (
            home / '.local/libexec/openai-symphony/'
            '8001b52e3062495a16e520e4ceaf8f9de868c4d0'
        )
        artifact_dir.mkdir(parents=True, mode=0o700)
        artifact = artifact_dir / 'symphony'
        artifact.write_bytes(content)
        artifact.chmod(0o500)
        return artifact

    def test_money_printer_symphony_uses_pinned_artifact_and_workflow_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / 'home'
            root = base / 'release'
            root.mkdir()
            workflow = root / 'ops/symphony/WORKFLOW.money-printer.md'
            workflow.parent.mkdir(parents=True)
            workflow.write_text('workflow')
            artifact = self._symphony_fixture(home)
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            with patch.object(entry_dispatch, '_SYMPHONY_ARTIFACT_SHA256', digest):
                self.assertEqual(command_for('money-printer-symphony', root, home), [
                    str(home / '.local/share/mise/installs/erlang/28.5/bin/escript'),
                    str(artifact),
                    '--i-understand-that-this-will-be-running-without-the-usual-guardrails',
                    '--logs-root', str(home / '.local/state/life-manager/money-printer-symphony/runtime-logs'),
                    '--port', '4000',
                    str(workflow),
                ])

    def test_money_printer_symphony_rejects_invalid_artifact_before_exec(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / 'home'
            root = base / 'release'
            root.mkdir()
            self._symphony_fixture(home)
            with patch.object(entry_dispatch, '_SYMPHONY_ARTIFACT_SHA256', 'b' * 64):
                with self.assertRaisesRegex(ValueError, 'official Symphony artifact unavailable'):
                    command_for('money-printer-symphony', root, home)

    def test_money_printer_symphony_reads_one_github_credential_and_sanitizes_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            private = home / '.local/share/anicca'
            private.mkdir(parents=True, mode=0o700)
            credentials = private / 'credentials.json'
            token = 'ghp_' + 'a' * 36
            credentials.write_text(json.dumps({
                'version': 1,
                'credentials': [{'service': 'openai-symphony-github', 'token': token}],
            }))
            credentials.chmod(0o600)
            base = {
                'PATH': '/tmp/untrusted',
                'GH_TOKEN': 'alias',
                'GH_ENTERPRISE_TOKEN': 'alias',
                'GITHUB_ENTERPRISE_TOKEN': 'alias',
                'SYMPHONY_WORKSPACE_ROOT': '/tmp/legacy-project-workspaces',
                'KEEP': 'value',
            }

            environment = entry_dispatch.environment_for('money-printer-symphony', home, base)

            self.assertEqual(environment['GITHUB_TOKEN'], token)
            self.assertEqual(environment['PATH'], '/opt/homebrew/bin:/usr/bin:/bin')
            self.assertEqual(environment['SYMPHONY_WORKSPACE_ROOT'],
                             str(home / '.local/state/life-manager/symphony-workspaces'))
            self.assertEqual(environment['KEEP'], 'value')
            self.assertNotIn('GH_TOKEN', environment)
            self.assertNotIn('GH_ENTERPRISE_TOKEN', environment)
            self.assertNotIn('GITHUB_ENTERPRISE_TOKEN', environment)

    def test_money_printer_bridge_uses_release_code_and_private_ssot_env(self):
        root = Path('/release')
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            private = home / '.local/share/anicca'
            private.mkdir(parents=True, mode=0o700)
            credentials = private / 'credentials.json'
            token = 'a' * 64
            github_token = 'ghp_' + 'b' * 36
            credentials.write_text(json.dumps({
                'version': 1,
                'credentials': [
                    {'service': 'life-manager-symphony-bridge', 'token': token},
                    {'service': 'openai-symphony-github', 'token': github_token},
                ],
            }))
            credentials.chmod(0o600)

            try:
                command = command_for('money-printer-symphony-bridge', root, home)
            except ValueError:
                self.fail('money printer bridge dispatch is missing')
            self.assertEqual(command, [
                '/opt/homebrew/bin/node',
                '/release/apps/life-manager/scripts/money-printer-symphony-bridge.js',
            ])
            environment_for = getattr(entry_dispatch, 'environment_for', None)
            self.assertTrue(callable(environment_for), 'secure bridge environment loader is missing')
            base = {
                'PATH': '/usr/bin',
                'GH_TOKEN': 'alias',
                'GH_ENTERPRISE_TOKEN': 'alias',
                'GITHUB_ENTERPRISE_TOKEN': 'alias',
            }
            environment = environment_for('money-printer-symphony-bridge', home, base)
            self.assertEqual(base, {
                'PATH': '/usr/bin',
                'GH_TOKEN': 'alias',
                'GH_ENTERPRISE_TOKEN': 'alias',
                'GITHUB_ENTERPRISE_TOKEN': 'alias',
            })
            self.assertEqual(environment['LM_SYMPHONY_API_BASE_URL'],
                             'https://life-call-production.up.railway.app')
            self.assertEqual(environment['LM_RUNTIME_TENANT_ID'], 'webmcp-judge')
            self.assertEqual(environment['LM_SYMPHONY_BRIDGE_SECRET'], token)
            self.assertEqual(environment['GITHUB_TOKEN'], github_token)
            self.assertEqual(environment['PATH'], '/opt/homebrew/bin:/usr/bin:/bin')
            self.assertNotIn('GH_TOKEN', environment)
            self.assertNotIn('GH_ENTERPRISE_TOKEN', environment)
            self.assertNotIn('GITHUB_ENTERPRISE_TOKEN', environment)

    def test_hf_gig_paid_direct_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('hf-gig-paid-direct', Path('/release'), Path('/home'))

    def test_writer_claim_loop_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('writer-claim-loop', Path('/release'), Path('/home'))

    def test_writer_money_sync_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('writer-money-sync', Path('/release'), Path('/home'))

    def test_writer_opportunity_discovery_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('writer-opportunity-discovery', Path('/release'), Path('/home'))



    def test_life_manager_daily_driver_no_longer_has_a_handwritten_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'no dispatch command'):
            command_for('life-manager-daily-driver', Path('/release'), Path('/home'))

    def test_marketing_owner_state_is_outside_release(self):
        root=Path('/release'); command=command_for('marketing-owner-weekly',root,Path('/home'))
        self.assertEqual(command[:4],[sys.executable,str(root/'skills/earn/marketing-engine/report/owner_report_cli.py'),'sweep','--kind'])
        self.assertEqual(command[-1],'/home/.local/state/life-manager/marketing-engine')

    def test_unknown_loop_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'no dispatch command'):
            command_for('missing',Path('/release'),Path('/home'))

    def test_other_coconala_lanes_keep_production_modes(self):
        root=Path('/release'); home=Path('/home')
        apply=command_for('hf-gig-apply-direct',root,home)
        reply=command_for('hf-gig-reply-detector',root,home)
        storefront=command_for('hf-gig-storefront-direct',root,home)
        guard = [sys.executable, '/release/runtime/host/memory_admission.py']
        self.assertEqual(apply[:2], guard)
        self.assertEqual(reply[:5], guard + ['--wait-seconds','30','--'])
        self.assertEqual(storefront[:2], guard)
        self.assertIn('--all-eligible',apply)
        self.assertEqual(reply[-5:],['--continuous','--poll-seconds','30','--workers','2'])
        self.assertEqual(storefront[-4:],['--effect','--auto-cadence','--full-interval-seconds','60'])

    def test_writer_jobs_keep_mutable_state_outside_release(self):
        root=Path('/release'); home=Path('/home')
        for loop_id in (
            'writer-opportunity-response', 'writer-report',
        ):
            command=command_for(loop_id,root,home)
            joined=' '.join(command)
            self.assertIn('/home/.local/state/life-manager/writer',joined)
            self.assertNotIn('/release/skills/writer-agent/state',joined)

    def test_lancers_browser_disables_code_sign_clone(self):
        script = Path(__file__).parents[3] / 'runtime/legacy/lancers-revenue-browser/run.sh'
        self.assertIn('--disable-features=MacAppCodeSignClone', script.read_text())


if __name__=='__main__':unittest.main()
