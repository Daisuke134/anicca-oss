import json
import importlib.util
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import reporter as REPORTER
import alpaca_cli as CLI
import effect_store as EFFECT_STORE
SPEC = importlib.util.spec_from_file_location("alpaca_investment_run", ROOT / "run.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _publisher_probe(root: Path) -> tuple[Path, Path]:
    marker = root / "publisher-called"
    executable = root / "node"
    executable.write_text('#!/bin/sh\n: > "$ALPACA_MARKER"\n', encoding="utf-8")
    executable.chmod(0o700)
    return executable, marker


class DeploymentProfileTest(unittest.TestCase):
    def test_accepts_only_exact_local_or_cloud(self):
        for value in ("local", "cloud"):
            with patch.dict(MODULE.os.environ, {
                "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": value,
            }):
                self.assertEqual(MODULE._deployment(), value)

    def test_rejects_missing_or_non_exact_values(self):
        invalid_values = (None, "", "local,cloud", " local", "LOCAL")
        for value in invalid_values:
            environment = {} if value is None else {
                "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": value,
            }
            with self.subTest(value=value), patch.dict(
                MODULE.os.environ, environment, clear=True
            ):
                with self.assertRaisesRegex(
                    ValueError, "^investment_deployment_invalid$"
                ):
                    MODULE._deployment()


class InvestmentModeTest(unittest.TestCase):
    def test_requires_exact_mode_before_broker_access(self):
        for value in ("paper", "shadow", "live", None, "", " paper", "PAPER", "paper,live"):
            with self.subTest(value=value):
                if value in {"paper", "shadow", "live"}:
                    with patch.dict(MODULE.os.environ, {"LIFE_MANAGER_INVESTMENT_MODE": value}):
                        self.assertEqual(MODULE._mode(), value)
                    continue
                with patch.dict(MODULE.os.environ, {
                    "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": "local",
                    **({} if value is None else {"LIFE_MANAGER_INVESTMENT_MODE": value}),
                }, clear=True), patch.object(MODULE, "reconcile_started", return_value={"pending": 0, "reconciled": 0, "unresolved": 0}), \
                        patch.object(MODULE, "observe", side_effect=RuntimeError("broker-called")) as observe, \
                        patch.object(MODULE, "deliver_failure", return_value={"status": "delivered"}):
                    MODULE.main(wake_id="mode-validation")
                self.assertEqual(observe.call_count, 0)

    def test_paper_mode_selects_configured_paper_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials, state = root / "paper.json", root / "paper-state"
            observed = []
            def broker_boundary(**kwargs):
                observed.append(kwargs)
                raise RuntimeError("stop-at-broker-boundary")
            with patch.dict(MODULE.os.environ, {
                "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": "local",
                "LIFE_MANAGER_INVESTMENT_MODE": "paper",
                "ALPACA_INVESTMENT_PAPER_CREDENTIALS_FILE": str(credentials),
                "ALPACA_INVESTMENT_PAPER_STATE_DIR": str(state),
            }, clear=True), patch.object(MODULE, "reconcile_started", return_value={"pending": 0, "reconciled": 0, "unresolved": 0}) as reconcile, \
                    patch.object(MODULE, "observe", side_effect=broker_boundary), \
                    patch.object(MODULE, "deliver_failure", return_value={"status": "delivered"}):
                MODULE.main(wake_id="paper-paths")
        self.assertEqual(observed[0]["credentials_path"], credentials)
        self.assertEqual(reconcile.call_args.args[0], state / "receipts.jsonl")

    def test_mode_state_paths_reject_shared_namespaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root, paper, shared = Path(directory), Path(directory) / "paper", Path(directory) / "shared"
            base = {
                "ALPACA_INVESTMENT_PAPER_STATE_DIR": str(paper),
                "ALPACA_INVESTMENT_SHADOW_CREDENTIALS_FILE": str(root / "shadow.json"),
                "ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE": str(root / "live.json"),
                "ALPACA_INVESTMENT_SHADOW_STATE_DIR": str(root / "shadow"),
                "ALPACA_INVESTMENT_LIVE_STATE_DIR": str(root / "live"),
            }
            for mode, selected in (("shadow", {"ALPACA_INVESTMENT_SHADOW_STATE_DIR": str(paper)}), ("live", {"ALPACA_INVESTMENT_LIVE_STATE_DIR": str(shared), "ALPACA_INVESTMENT_SHADOW_STATE_DIR": str(shared)})):
                with self.subTest(mode=mode), patch.dict(MODULE.os.environ, {**base, **selected}, clear=True):
                    with self.assertRaisesRegex(ValueError, "^investment_mode_state_path_conflict$"):
                        MODULE._mode_paths(mode)

    def test_receipts_and_reconciled_rows_expose_top_level_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "receipts.jsonl"
            EFFECT_STORE.record_no_trade(ledger, {"mode": "shadow", "candidate_ref": "NO_TRADE"})
            sealed = EFFECT_STORE.seal(ledger, {"mode": "paper", "candidate_ref": "TRADE"}, {"asset_class": "crypto"})
            EFFECT_STORE.mark_started(ledger, sealed)
            EFFECT_STORE.reconcile_started(ledger, lambda _: {"found": True, "status": "filled"})
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertTrue(all(row.get("mode") in {"paper", "shadow"} for row in rows))

    def test_review_polling_is_local_paper_only_and_fails_soft(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "account-status.json").write_text(
                '{"application_status":"in_review","observed_at":"2026-09-05T00:00:00Z"}'
            )
            with patch.object(MODULE, "refresh_application_status", side_effect=RuntimeError("browser")) as refresh:
                self.assertEqual(MODULE._review_status(state, "paper", "local")["application_status"], "in_review")
                self.assertEqual(refresh.call_count, 1)
                MODULE._review_status(state, "paper", "cloud")
                MODULE._review_status(state, "live", "local")
                self.assertEqual(refresh.call_count, 1)

    def test_legacy_paper_intent_reconciliation_writes_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "receipts.jsonl"
            ledger.write_text(json.dumps({
                "client_order_id": "legacy-order", "effect_id": "legacy-effect",
                "paper": True, "receipt_type": "effect_intent", "status": "started",
            }) + "\n")
            EFFECT_STORE.reconcile_started(ledger, lambda _: {"found": True, "status": "filled"})
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        self.assertTrue(all(row.get("mode") == "paper" for row in rows[1:]))


class BrokerContextTest(unittest.TestCase):
    def _credential(self, root, row):
        private = root / "private"
        private.mkdir(parents=True, mode=0o700)
        path = private / "credentials.json"
        path.write_text(json.dumps({"credentials": [row]}))
        path.chmod(0o600)
        return path

    def test_paper_and_live_contexts_use_separate_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root, cli = Path(directory), Path(directory) / "alpaca"
            cli.write_text("#!/bin/sh\n[ \"$1\" = version ] && echo 0.0.14\n")
            cli.chmod(0o700)
            paper = self._credential(root / "paper", {
                "service": "app.alpaca.markets", "paper_endpoint": CLI.PAPER_ENDPOINT,
                "api_key": "paper-key", "api_secret": "paper-secret"})
            live = self._credential(root / "live", {
                "service": "app.alpaca.markets", "live_endpoint": "https://api.alpaca.markets/v2",
                "live_api_key": "live-key", "live_api_secret": "live-secret"})
            for mode, path, key, live_trade in (
                ("paper", paper, "paper-key", "false"),
                ("shadow", live, "live-key", "true"), ("live", live, "live-key", "true")):
                context = CLI._context(path, cli, mode=mode)
                self.assertEqual((context["ALPACA_API_KEY"], context["ALPACA_LIVE_TRADE"]),
                                 (key, live_trade))
            with self.assertRaises(ValueError):
                CLI._context(paper, cli, mode="live")

    def test_submit_rejects_shadow_and_live_before_cli_or_order(self):
        with patch.object(CLI, "_context") as context, patch.object(CLI, "_run") as run, \
                patch.object(CLI.subprocess, "run") as process:
            for mode in ("shadow", "live"):
                with self.subTest(mode=mode), self.assertRaisesRegex(
                    ValueError, "^investment_mode_effect_forbidden$"):
                    CLI.submit_order(
                        credentials_path=Path("missing"), cli_path=Path("missing"),
                        client_order_id="lm-ai-" + "a" * 24,
                        order={"asset_class": "crypto", "symbol": "BTC/USD", "notional_usd": "1"},
                        mode=mode,
                    )
        context.assert_not_called()
        run.assert_not_called()
        process.assert_not_called()


class BrokerSnapshotTest(unittest.TestCase):
    def test_shadow_and_live_snapshots_are_nonpaper(self):
        for mode in ("shadow", "live"):
            with self.subTest(mode=mode), patch.dict(
                CLI.os.environ, {"LIFE_MANAGER_INVESTMENT_MODE": mode}, clear=True
            ), patch.object(CLI, "_context", return_value={}), patch.object(
                CLI, "_run", side_effect=[
                    {"cash": "101", "equity": "102"},
                    {"is_open": False, "observed_at": "2026-09-05T00:00:00Z"},
                    [], 0, 0, {"symbol": "SPY"}, 0,
                ]
            ):
                observation = CLI.observe(credentials_path=Path("missing"), cli_path=Path("missing"))
            self.assertEqual((observation["mode"], observation["paper"]), (mode, False))
            with patch.dict(
                CLI.os.environ, {"LIFE_MANAGER_INVESTMENT_MODE": mode}, clear=True
            ), patch.object(CLI, "_context", return_value={}), patch.object(
                CLI, "_run", side_effect=[
                    {"cash": "101", "equity": "102"}, [], [],
                    {"is_open": False, "observed_at": "2026-09-05T00:00:00Z"}, [],
                ]
            ):
                snapshot = CLI.read_campaign_snapshot(
                    credentials_path=Path("missing"), cli_path=Path("missing"), symbols=("A", "B")
                )
            self.assertEqual((snapshot["mode"], snapshot["paper"]), (mode, False))


class ShadowReadOnlyTest(unittest.TestCase):
    def test_shadow_never_submits_campaign_exit_or_allocator_order(self):
        observation = {"account": {"cash": "100000", "equity": "100000"},
                       "activities_count": 0, "clock": {"observed_at": "2026-09-05T00:00:00Z"},
                       "open_and_closed_orders_count": 0,
                       "positions": [{"unrealized_pl": "-0.25"}]}
        with tempfile.TemporaryDirectory() as directory, patch.dict(MODULE.os.environ, {
            "LIFE_MANAGER_INVESTMENT_MODE": "shadow", "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": "local",
            "ALPACA_INVESTMENT_SHADOW_CREDENTIALS_FILE": str(Path(directory) / "live.json"),
            "ALPACA_INVESTMENT_SHADOW_STATE_DIR": str(Path(directory) / "shadow-state"),
        }, clear=True), patch.object(MODULE, "reconcile_started", return_value={"pending": 0, "reconciled": 0, "unresolved": 0}), \
                patch.object(MODULE, "observe", return_value=observation), patch.object(MODULE, "read_campaign_snapshot") as campaign_read, \
                patch.object(MODULE, "reconcile", return_value={"exit_status": "EXIT_READY", "exit_credit_usd": "0.50", "unrealized_pnl_usd": "0.00"}) as campaign_reconcile, \
                patch.object(MODULE, "exit_order", return_value={"asset_class": "option_spread_close"}), \
                patch.object(MODULE, "read_allocator_snapshot", return_value={"risk": {}}), patch.object(MODULE, "build_candidates", return_value=[]), \
                patch.object(MODULE, "choose", return_value={"approved": True, "candidate_ref": "crypto://BTC/USD", "candidate": {"asset_class": "crypto"}, "gate": "approved", "observed_at": "2026-09-05T00:00:00Z"}), \
                patch.object(MODULE, "order_for", return_value={"asset_class": "crypto"}), patch.object(MODULE, "submit_order") as submit, \
                patch.object(MODULE, "deliver", return_value={"message_id": "123"}):
            self.assertEqual(MODULE.main(wake_id="shadow-read-only"), 0)
        campaign_read.assert_not_called()
        campaign_reconcile.assert_not_called()
        submit.assert_not_called()


class PortablePassTest(unittest.TestCase):
    @patch.object(MODULE, "reconcile_started", return_value={"pending": 0, "reconciled": 0, "unresolved": 0})
    @patch.object(MODULE, "observe")
    @patch.object(MODULE, "read_campaign_snapshot", return_value={})
    @patch.object(MODULE, "reconcile")
    @patch.object(MODULE, "read_allocator_snapshot", return_value={"risk": {}})
    @patch.object(MODULE, "build_candidates", return_value=[])
    @patch.object(MODULE, "choose")
    @patch.object(MODULE, "deliver", return_value={"message_id": "123"})
    def test_success_has_no_dashboard_effect_or_public_summary(
        self, _deliver, choose, _build, _allocator, reconcile,
        _campaign, observe, _reconcile_started,
    ):
        observe.return_value = {
            "account": {"cash": "100000", "equity": "100000"},
            "activities_count": 0, "clock": {"observed_at": "2026-09-05T00:00:00Z"},
            "open_and_closed_orders_count": 0, "positions": [],
        }
        reconcile.return_value = {"exit_status": "CLOSED", "unrealized_pnl_usd": "0"}
        choose.return_value = {
            "approved": False, "candidate_ref": "NO_TRADE", "gate": "model_no_trade",
            "observed_at": "2026-09-05T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable, marker = _publisher_probe(root)
            with patch.dict(MODULE.os.environ, {
                "ALPACA_INVESTMENT_STATE_DIR": str(root / "state"),
                "NODE_BIN": str(executable), "ALPACA_MARKER": str(marker),
                "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": "local",
                "LIFE_MANAGER_INVESTMENT_MODE": "paper",
            }), redirect_stdout(StringIO()) as output:
                self.assertEqual(MODULE.main(wake_id="wake-success"), 0)
            self.assertFalse(marker.exists())
            summary = json.loads(output.getvalue())
            self.assertNotIn("public_snapshot_published", summary)
            self.assertEqual(summary["deployment"], "local")
            self.assertEqual(summary["mode"], "paper")
            receipts = [
                json.loads(line)
                for line in (root / "state" / "receipts.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            decision_receipt = next(
                receipt for receipt in receipts
                if receipt["receipt_type"] == "decision"
            )
            self.assertEqual(decision_receipt["decision"]["deployment"], "local")

    @patch.object(MODULE, "reconcile_started", return_value={"pending": 0, "reconciled": 0, "unresolved": 0})
    @patch.object(MODULE, "observe", side_effect=RuntimeError("provider unavailable"))
    @patch.object(MODULE, "deliver_failure", return_value={"message_id": "123", "status": "delivered"})
    def test_terminal_failure_has_no_dashboard_effect(self, _deliver, _observe, _reconcile):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable, marker = _publisher_probe(root)
            with patch.dict(MODULE.os.environ, {
                "ALPACA_INVESTMENT_STATE_DIR": str(root / "state"),
                "NODE_BIN": str(executable), "ALPACA_MARKER": str(marker),
                "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": "local",
                "LIFE_MANAGER_INVESTMENT_MODE": "paper",
            }), redirect_stdout(StringIO()) as output:
                self.assertEqual(MODULE.main(wake_id="wake-failure"), 78)
            self.assertFalse(marker.exists())
            self.assertEqual(json.loads(output.getvalue())["mode"], "paper")


class FailureTelegramTest(unittest.TestCase):
    @patch.object(CLI.subprocess, "run")
    def test_cli_failure_code_identifies_only_the_safe_operation(self, run):
        run.return_value.returncode = 1
        with self.assertRaisesRegex(
            ValueError, "^alpaca_cli_failed:account_get$"
        ):
            CLI._run(Path("/safe/alpaca"), ["account", "get", "--secret", "hidden"], {})

    def test_prefixed_provider_payload_is_not_emitted(self):
        error = ValueError("alpaca_cli_failed:account_get:provider-payload-SECRET")
        self.assertEqual(MODULE._error_code(error), "ValueError")

    def test_known_internal_failure_code_is_emitted_exactly(self):
        error = ValueError("alpaca_allocator_shape_invalid")
        self.assertEqual(MODULE._error_code(error), "alpaca_allocator_shape_invalid")
        tainted = ValueError("alpaca_allocator_shape_invalid:provider-payload-SECRET")
        self.assertEqual(MODULE._error_code(tainted), "ValueError")

    @patch.object(MODULE, "deliver_failure", create=True)
    @patch.object(MODULE, "observe", side_effect=RuntimeError("provider payload must stay private"))
    @patch.object(MODULE, "reconcile_started", return_value={"pending": 0, "reconciled": 0, "unresolved": 0})
    def test_terminal_failure_reports_once_after_internal_retries(
        self, _reconcile, observe, deliver_failure
    ):
        deliver_failure.return_value = {"message_id": "123", "status": "delivered"}
        with patch.dict(MODULE.os.environ, {
            "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": "local",
            "LIFE_MANAGER_INVESTMENT_MODE": "paper",
        }), redirect_stdout(StringIO()) as output:
            self.assertEqual(MODULE.main(), 78)
        self.assertEqual(observe.call_count, 3)
        deliver_failure.assert_called_once()
        self.assertEqual(deliver_failure.call_args.kwargs["stage"], "observe")
        self.assertFalse(deliver_failure.call_args.kwargs["effect_uncertain"])
        self.assertNotIn("provider payload", str(deliver_failure.call_args))
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["error_code"], "RuntimeError")
        self.assertNotIn("provider payload", output.getvalue())

    def test_telegram_delivery_failure_is_not_retried(self):
        self.assertFalse(MODULE._retry_allowed("telegram_deliver", False, 0))
        self.assertTrue(MODULE._retry_allowed("observe", False, 0))

    def test_reconciliation_failure_does_not_claim_that_no_order_exists(self):
        message = REPORTER.render_failure(
            stage="reconcile_started", effect_uncertain=True,
            wake_id="2026-09-04T00:00:00Z",
        )
        self.assertIn("送信した可能性", message)
        self.assertNotIn("注文は実行していません", message)

    def test_terminal_effect_is_unknown_after_submit(self):
        self.assertEqual(MODULE._terminal_effect(False), "none")
        self.assertEqual(MODULE._terminal_effect(True), "unknown")


if __name__ == "__main__":
    unittest.main()
