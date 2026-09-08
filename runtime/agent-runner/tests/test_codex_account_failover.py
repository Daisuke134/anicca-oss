import json
import io
import os
import sys
import tempfile
import unittest
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_runner import (  # noqa: E402
    ProviderLeaseBusy,
    classify_provider_error,
    codex_attempt_started_work,
    codex_failover_action,
    provider_process_env,
    resolve_provider_profiles,
)
import agent_runner  # noqa: E402


class CodexProfileBoundaryTest(unittest.TestCase):
    def _run_candidate_fixture(self, plan, include_claude):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidates = [
                {"provider": "codex", "model": "fixture-model", "effort": "medium",
                 "profile_alias": "acct1", "automation_home": "/fixture/acct1",
                 "auth_file": "/fixture/acct1-auth", "account_fallback_next": True},
                {"provider": "codex", "model": "fixture-model", "effort": "medium",
                 "profile_alias": "acct2", "automation_home": "/fixture/acct2",
                 "auth_file": "/fixture/acct2-auth", "account_fallback_next": False},
            ]
            if include_claude:
                candidates.append({"provider": "claude", "model": "fixture-claude"})
            schema_path = root / "schema.json"
            schema_path.write_text(json.dumps({
                "type": "object",
                "required": ["ok"],
                "properties": {"ok": {"type": "boolean"}},
            }), encoding="utf-8")
            prompt_path = root / "prompt.txt"
            prompt_path.write_text("fixture prompt for account failover", encoding="utf-8")
            evidence_dir = root / "evidence"
            usage_ledger = root / "usage.jsonl"
            calls = []

            def fake_provider_process(command, *, stdout, stderr, env, completion_path, **_kwargs):
                provider, _, profile = env["FIXTURE_PROVIDER"].partition(":")
                profile = profile or None
                behavior = plan.get((provider, profile), "failure")
                calls.append((provider, profile, behavior))
                if behavior == "quota":
                    stdout.write(json.dumps({
                        "type": "turn.failed",
                        "error": {"code": "usage_limit_reached", "status": 429},
                    }).encode("utf-8") + b"\n")
                    return 1
                if behavior == "auth":
                    stdout.write(json.dumps({
                        "type": "error",
                        "error": {"code": "authentication_error", "status": 401},
                    }).encode("utf-8") + b"\n")
                    return 1
                if behavior == "text_quota":
                    stdout.write(json.dumps({
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "quota exceeded"},
                    }).encode("utf-8") + b"\n")
                    return 1
                if behavior == "work_quota":
                    stdout.write(json.dumps({
                        "type": "item.started",
                        "item": {"type": "command_execution", "command": "touch"},
                    }).encode("utf-8") + b"\n")
                    stdout.write(json.dumps({
                        "type": "turn.failed",
                        "error": {"code": "usage_limit_reached", "status": 429},
                    }).encode("utf-8") + b"\n")
                    return 1
                if behavior == "fresh_quota":
                    stdout.write(json.dumps({
                        "type": "turn.failed",
                        "error": {"code": "usage_limit_reached", "status": 429},
                    }).encode("utf-8") + b"\n")
                    completion_path.write_text('{"ok":true}', encoding="utf-8")
                    return 1
                if behavior == "fresh_unavailable":
                    stderr.write(b"Operation not permitted\n")
                    completion_path.write_text('{"ok":true}', encoding="utf-8")
                    return 127
                if behavior == "unavailable":
                    stderr.write(b"connection refused\n")
                    return 1
                if behavior == "timeout":
                    return 124
                if behavior == "timeout_fresh":
                    completion_path.write_text('{"ok":true}', encoding="utf-8")
                    raise subprocess.TimeoutExpired(command, 20)
                if behavior == "busy":
                    raise ProviderLeaseBusy("codex automation home is busy")
                if behavior == "success":
                    if provider == "codex":
                        completion_path.write_text('{"ok":true}', encoding="utf-8")
                    else:
                        stdout.write(b'{"ok":true}\n')
                    return 0
                return 1

            def fixture_provider_env(provider, provider_config, environ=None, **_kwargs):
                env = dict(os.environ if environ is None else environ)
                env["FIXTURE_PROVIDER"] = f"{provider}:{provider_config.get('profile_alias', '')}"
                return env

            argv = [
                "agent_runner.py", "--task-class", "diagnostic-agent",
                "--prompt-file", str(prompt_path), "--schema", str(schema_path),
                "--evidence-dir", str(evidence_dir), "--task-label", "fixture",
                "--loop", "fixture", "--workdir", str(root), "--timeout-seconds", "20",
            ]
            env = {
                "AGENT_RUNNER_CONFIG": str(ROOT / "config.json"),
                "ANICCA_USAGE_LEDGER": str(usage_ledger),
                "ANICCA_TOKEN_BUDGET_LEDGER": str(root / "token-budget.jsonl"),
                "LIFE_MANAGER_PROVIDER_LEASE_PATH": "",
                "LIFE_MANAGER_RELEASE_SHA": "",
            }
            with mock.patch.object(agent_runner, "resolve_provider_profiles", return_value=candidates), \
                    mock.patch.object(agent_runner, "provider_process_env", side_effect=fixture_provider_env), \
                    mock.patch.object(agent_runner, "run_provider_process", side_effect=fake_provider_process), \
                    mock.patch.object(agent_runner, "ensure_evidence_capacity", return_value={}), \
                    mock.patch.object(agent_runner, "append_usage_event"), \
                    mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(sys, "argv", argv), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = agent_runner.run()
            return status, calls

    def test_escalation_route_has_account_two_then_cross_provider_fallback(self):
        config = json.loads((ROOT / "config.json").read_text())
        candidates = config["task_classes"]["escalation-agent"]["candidates"]
        self.assertEqual(
            [(row["provider"], row["model"], row.get("profile_alias")) for row in candidates],
            [
                ("codex", "gpt-5.6-terra", "acct2"),
                ("claude-direct", "claude-sonnet-5", None),
            ],
        )

    def test_codex_keeps_code_mode_host_for_tool_using_escalations(self):
        config = json.loads((ROOT / "config.json").read_text())
        disabled = config["providers"]["codex"]["disabled_features"]
        self.assertNotIn("code_mode_host", disabled)
        self.assertNotIn("unified_exec", disabled)

    def test_candidate_resolves_one_explicit_profile_without_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); auth = root / "auth.json"; auth.write_text("{}\n")
            providers = {"codex": {"profiles": {"acct2": {
                "automation_home": str(root / "automation"), "auth_file": str(auth),
            }}}}
            candidates = resolve_provider_profiles(
                [{"provider": "codex", "model": "fixture", "profile_alias": "acct2"}], providers)
            self.assertEqual(len(candidates), 1)
            env = provider_process_env("codex", candidates[0], {"PATH": "/usr/bin:/bin"})
            self.assertEqual(env["CODEX_HOME"], str(root / "automation"))
            self.assertEqual((root / "automation/auth.json").resolve(), auth.resolve())

    def test_codex_candidate_without_profile_alias_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "profile_alias"):
            resolve_provider_profiles(
                [{"provider": "codex", "model": "fixture"}],
                {"codex": {"profiles": {"acct2": {}}}})

    def test_non_codex_candidate_keeps_order_without_expansion(self):
        candidates = resolve_provider_profiles([
            {"provider": "codex", "model": "fixture", "profile_alias": "acct2"},
            {"provider": "claude", "model": "fallback"},
        ], {"codex": {"profiles": {"acct2": {
            "automation_home": "/tmp/a", "auth_file": "/tmp/auth",
        }}}, "claude": {}})
        self.assertEqual([(x["provider"], x.get("profile_alias")) for x in candidates], [
            ("codex", "acct2"), ("claude", None)])

    def test_structured_quota_failure_retries_acct1_then_moves_past_acct2(self):
        acct1 = {"provider": "codex", "profile_alias": "acct1", "account_fallback_next": True}
        acct2 = {"provider": "codex", "profile_alias": "acct2", "account_fallback_next": False}
        envelope = json.dumps({
            "type": "turn.failed",
            "error": {"code": "usage_limit_reached", "status": 429, "message": "usage limit reached"},
        })
        error_class = classify_provider_error(1, False, envelope, "", "", provider="codex")
        self.assertEqual(error_class, "transient_quota")
        self.assertEqual(
            classify_provider_error(
                1,
                False,
                json.dumps({"type": "turn.failed", "error": {"message": "Quota exceeded. Check your plan and billing details."}}),
                "",
                "",
                provider="codex",
            ),
            "transient_quota",
        )
        self.assertEqual(codex_failover_action(acct1, error_class, False, False), "retry_next_account")
        self.assertEqual(codex_failover_action(acct2, error_class, False, False), "continue_next_non_codex")

    def test_arbitrary_agent_text_that_mentions_quota_never_authorizes_account_fallback(self):
        text = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "quota is unavailable, please retry"},
        })
        error_class = classify_provider_error(1, False, text, "", "", provider="codex")
        self.assertEqual(error_class, "validation_or_task_failure")
        self.assertEqual(classify_provider_error(1, False, "quota exceeded", "", "", provider="codex"), "validation_or_task_failure")
        self.assertEqual(
            codex_failover_action(
                {"provider": "codex", "profile_alias": "acct1", "account_fallback_next": True},
                error_class,
                False,
                False,
            ),
            "stop",
        )

    def test_acct1_timeout_or_unavailable_skips_acct2_and_continues_to_claude(self):
        acct1 = {"provider": "codex", "profile_alias": "acct1", "account_fallback_next": True}
        acct2 = {"provider": "codex", "profile_alias": "acct2", "account_fallback_next": False}
        claude = {"provider": "claude", "model": "claude-sonnet-5"}
        for error_class in ("transient_timeout", "transient_unavailable"):
            with self.subTest(error_class=error_class):
                if error_class == "transient_timeout":
                    self.assertEqual(classify_provider_error(124, False, "", "", "", provider="codex"), error_class)
                else:
                    self.assertEqual(classify_provider_error(1, False, "", "connection refused", "", provider="codex"), error_class)
                self.assertEqual(codex_failover_action(acct1, error_class, False, False), "continue_next_non_codex")
                self.assertEqual(codex_failover_action(acct2, error_class, False, False), "continue_next_non_codex")
                self.assertEqual(claude["provider"], "claude")

    def test_acct2_structured_auth_failure_requests_non_codex_continuation(self):
        acct2 = {"provider": "codex", "profile_alias": "acct2", "account_fallback_next": False}
        envelope = json.dumps({
            "type": "error",
            "error": {"code": "authentication_error", "status": 401, "message": "authentication failed"},
        })
        error_class = classify_provider_error(1, False, envelope, "", "", provider="codex")
        self.assertEqual(error_class, "transient_auth")
        self.assertEqual(codex_failover_action(acct2, error_class, False, False), "continue_next_non_codex")

    def test_fresh_result_or_started_codex_work_blocks_retry_and_cross_provider_duplicate(self):
        acct1 = {"provider": "codex", "profile_alias": "acct1", "account_fallback_next": True}
        work = json.dumps({"type": "item.started", "item": {"type": "command_execution"}})
        self.assertTrue(codex_attempt_started_work(work))
        for candidate in (acct1, {"provider": "codex", "profile_alias": "acct2", "account_fallback_next": False}):
            for result_fresh, work_started in ((True, False), (False, True), (True, True)):
                with self.subTest(profile=candidate["profile_alias"], result_fresh=result_fresh, work_started=work_started):
                    self.assertEqual(
                        codex_failover_action(candidate, "transient_quota", result_fresh, work_started),
                        "stop",
                    )

    def test_run_structured_quota_retries_acct1_then_selects_acct2(self):
        status, calls = self._run_candidate_fixture(
            {("codex", "acct1"): "quota", ("codex", "acct2"): "success"},
            include_claude=False,
        )
        self.assertEqual(status, 0)
        self.assertEqual(calls, [("codex", "acct1", "quota"), ("codex", "acct2", "success")])

    def test_run_accepts_fresh_schema_valid_codex_result_written_before_timeout(self):
        status, calls = self._run_candidate_fixture(
            {("codex", "acct1"): "timeout_fresh"},
            include_claude=True,
        )
        self.assertEqual(status, 0)
        self.assertEqual(calls, [("codex", "acct1", "timeout_fresh")])

    def test_run_acct1_timeout_or_unavailable_skips_acct2_and_calls_claude_once(self):
        for failure in ("timeout", "unavailable"):
            with self.subTest(failure=failure):
                status, calls = self._run_candidate_fixture(
                    {
                        ("codex", "acct1"): failure,
                        ("codex", "acct2"): "failure",
                        ("claude", None): "success",
                    },
                    include_claude=True,
                )
                self.assertEqual(status, 0)
                self.assertEqual(calls, [
                    ("codex", "acct1", failure),
                    ("claude", None, "success"),
                ])

    def test_run_codex_home_busy_calls_claude_once(self):
        status, calls = self._run_candidate_fixture(
            {
                ("codex", "acct1"): "busy",
                ("claude", None): "success",
            },
            include_claude=True,
        )
        self.assertEqual(status, 0)
        self.assertEqual(calls, [
            ("codex", "acct1", "busy"),
            ("claude", None, "success"),
        ])

    def test_fresh_unavailable_without_runtime_work_calls_claude_once(self):
        status, calls = self._run_candidate_fixture(
            {
                ("codex", "acct1"): "fresh_unavailable",
                ("claude", None): "success",
            },
            include_claude=True,
        )
        self.assertEqual(status, 0)
        self.assertEqual(
            calls,
            [
                ("codex", "acct1", "fresh_unavailable"),
                ("claude", None, "success"),
            ],
        )

    def test_run_acct2_structured_quota_calls_claude_once(self):
        status, calls = self._run_candidate_fixture(
            {
                ("codex", "acct1"): "quota",
                ("codex", "acct2"): "quota",
                ("claude", None): "success",
            },
            include_claude=True,
        )
        self.assertEqual(status, 0)
        self.assertEqual(calls, [
            ("codex", "acct1", "quota"),
            ("codex", "acct2", "quota"),
            ("claude", None, "success"),
        ])

    def test_run_codex_only_structured_quota_exhausts_accounts_and_fails(self):
        status, calls = self._run_candidate_fixture(
            {("codex", "acct1"): "quota", ("codex", "acct2"): "quota"},
            include_claude=False,
        )
        self.assertEqual(status, 1)
        self.assertEqual(calls, [("codex", "acct1", "quota"), ("codex", "acct2", "quota")])

    def test_run_arbitrary_agent_quota_text_does_not_call_acct2(self):
        status, calls = self._run_candidate_fixture(
            {
                ("codex", "acct1"): "text_quota",
                ("codex", "acct2"): "success",
                ("claude", None): "success",
            },
            include_claude=True,
        )
        self.assertEqual(status, 1)
        self.assertEqual(calls, [("codex", "acct1", "text_quota")])

    def test_run_fresh_result_or_started_work_does_not_call_second_provider(self):
        for failure in ("fresh_quota", "work_quota"):
            with self.subTest(failure=failure):
                status, calls = self._run_candidate_fixture(
                    {
                        ("codex", "acct1"): failure,
                        ("codex", "acct2"): "success",
                        ("claude", None): "success",
                    },
                    include_claude=True,
                )
                self.assertEqual(status, 1)
                self.assertEqual(calls, [("codex", "acct1", failure)])


if __name__ == "__main__": unittest.main()
