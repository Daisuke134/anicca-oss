import fcntl
import io
import json
import os
import plistlib
import shutil
import shlex
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import runtime.loop.lm_loop as lm_loop
from runtime.loop.lm_loop import apply_live
from runtime.loop.lm_loop_apply import apply_registry, build_apply_plan, install_one


SHA = "a" * 40


def registry(entrypoint="bin/example.sh"):
    return {"schema_version": 2, "loops": {"example": {
        "label": "ai.anicca.example", "domain": "system", "entrypoint": entrypoint,
        "cadence": {"start_interval_seconds": 60}, "effect_class": "none",
        "state_root": "~/.local/state/life-manager/example",
        "log_root": "~/.local/state/life-manager/example/logs",
        "cleanup": {"max_runs": 10, "max_age_days": 7},
        "provider_route": "deterministic",
    }}}


def two_loop_registry():
    value = registry()
    value["loops"]["second"] = {**value["loops"]["example"], "label": "ai.anicca.second"}
    return value


def money_printer_registry(
    entrypoint="bin/example.sh",
    loop_id="money-printer-symphony-bridge",
    label="ai.anicca.life-manager-money-printer-symphony-bridge",
):
    value = registry(entrypoint)
    value["loops"][loop_id] = value["loops"].pop("example")
    value["loops"][loop_id]["label"] = label
    if loop_id == "money-printer-symphony":
        value["loops"][loop_id]["cadence"] = {"keep_alive": True}
    return value


class LmLoopApplyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "bin").mkdir()
        (self.root / "bin/example.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.root / "bin/example.sh").chmod(0o755)
        (self.root / "bin/lm-loop-run").write_text("#!/bin/sh\nexit 0\n")
        (self.root / "bin/lm-loop-run").chmod(0o755)
        (self.root / "RELEASE.json").write_text(json.dumps({"sha": SHA}))

    def test_apply_requires_explicit_target_or_all(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(lm_loop, "apply_live", side_effect=AssertionError("apply called")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(lm_loop.main(["apply"]), 2)
        self.assertIn("LIFE_MANAGER_APPLY_TARGET", json.loads(output.getvalue())["error"])

    def test_apply_all_is_explicit(self):
        with patch.dict(os.environ, {
                "LIFE_MANAGER_RELEASE_ROOT": str(self.root),
                "LIFE_MANAGER_LAUNCH_AGENTS_DIR": str(self.root / "LaunchAgents"),
        }, clear=True), \
                patch.object(lm_loop, "apply_live", return_value=[]) as apply, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(lm_loop.main(["apply", "--all"]), 0)
        self.assertIsNone(apply.call_args.kwargs["target"])

    def tearDown(self):
        self.temp.cleanup()

    def _release(self, name: str) -> Path:
        release = self.root / name
        (release / "bin").mkdir(parents=True)
        (release / "config").mkdir()
        (release / "bin/example.sh").write_text("#!/bin/sh\nexit 0\n")
        (release / "bin/example.sh").chmod(0o755)
        (release / "bin/lm-loop-run").write_text("#!/bin/sh\nexit 0\n")
        (release / "bin/lm-loop-run").chmod(0o755)
        (release / "config/loop-registry.json").write_text(json.dumps(registry()))
        (release / "RELEASE.json").write_text(json.dumps({"sha": SHA}))
        return release

    def _launchctl_recorder(self, expected_arguments: list[str] | None = None,
                            label: str = "ai.anicca.example",
                            agents_dir_name: str = "LaunchAgents") -> tuple[Path, Path]:
        calls = self.root / "launchctl.calls"
        state = self.root / "launchctl.state"
        executable = self.root / "launchctl-safe"
        script = (
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(calls))}\n"
        )
        if expected_arguments is not None:
            domain = f"gui/{os.getuid()}"
            service = f"{domain}/{label}"
            plist = self.root / agents_dir_name / f"{label}.plist"
            script += "if [ \"$1\" = preflight ]; then\n"
            script += "[ \"$#\" -eq 1 ] || exit 90\n"
            script += "elif [ \"$1\" = print ]; then\n"
            script += f"[ \"$#\" -eq 2 ] && [ \"$2\" = {shlex.quote(service)} ] || exit 91\n"
            script += f"[ -f {shlex.quote(str(state))} ] || exit 1\n"
            script += "printf '%s\\n' 'arguments = {'\n"
            script += "".join(
                f"printf '%s\\n' {shlex.quote(argument)}\n"
                for argument in expected_arguments
            )
            script += "printf '%s\\n' '}'\n"
            script += "elif [ \"$1\" = bootout ]; then\n"
            script += f"[ \"$#\" -eq 2 ] && [ \"$2\" = {shlex.quote(service)} ] || exit 92\n"
            script += f"rm -f {shlex.quote(str(state))}\n"
            script += "elif [ \"$1\" = bootstrap ]; then\n"
            script += (
                f"[ \"$#\" -eq 3 ] && [ \"$2\" = {shlex.quote(domain)} ] && "
                f"[ \"$3\" = {shlex.quote(str(plist))} ] || exit 93\n"
            )
            script += f"touch {shlex.quote(str(state))}\n"
            script += "else\n"
            script += "exit 94\n"
            script += "fi\n"
        script += "exit 0\n"
        executable.write_text(script)
        executable.chmod(0o755)
        return executable, calls

    def _apply_kwargs(self, current: Path, lock_path: Path,
                      expected_arguments: list[str] | None = None,
                      label: str = "ai.anicca.example",
                      agents_dir_name: str = "LaunchAgents") -> dict:
        launchctl_safe, calls = self._launchctl_recorder(
            expected_arguments, label, agents_dir_name)
        agents_dir = self.root / agents_dir_name
        agents_dir.mkdir()
        return {
            "agents_dir": agents_dir,
            "launchctl_safe": launchctl_safe,
            "current": current,
            "lock_path": lock_path,
            "calls": calls,
        }

    def test_rendered_plist_is_deterministic_and_release_exact(self):
        first = build_apply_plan(registry(), self.root, SHA)
        second = build_apply_plan(registry(), self.root, SHA)
        self.assertEqual(first[0]["plist_bytes"], second[0]["plist_bytes"])
        value = plistlib.loads(first[0]["plist_bytes"])
        self.assertEqual(value["ProgramArguments"], [
            str(self.root.resolve() / "bin/lm-loop-run"), "example", str(self.root.resolve())])
        self.assertEqual(value["StartInterval"], 60)
        self.assertNotIn("Umask", value)
        self.assertEqual(value["EnvironmentVariables"]["LIFE_MANAGER_RELEASE_SHA"], SHA)

    def test_alpaca_plist_declares_local_deployment(self):
        value = registry()
        value["loops"]["alpaca-investment"] = value["loops"].pop("example")
        rendered = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        self.assertEqual(
            rendered["EnvironmentVariables"]["LIFE_MANAGER_INVESTMENT_DEPLOYMENT"],
            "local",
        )
        environment = rendered["EnvironmentVariables"]
        self.assertEqual(environment["LIFE_MANAGER_INVESTMENT_MODE"], "paper")
        self.assertEqual(environment["ALPACA_INVESTMENT_PAPER_CREDENTIALS_FILE"],
                         str(Path.home() / ".local/share/anicca/credentials.json"))
        self.assertEqual(environment["ALPACA_INVESTMENT_PAPER_STATE_DIR"],
                         str(Path.home() / ".local/state/life-manager/example"))

    def test_alpaca_shadow_plist_is_read_only_and_state_separated(self):
        value = registry()
        entry = value["loops"].pop("example")
        entry.update({"effect_class": "none", "state_root": "~/.local/state/life-manager/alpaca-investment-shadow"})
        value["loops"]["alpaca-investment-shadow"] = entry
        rendered = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        environment = rendered["EnvironmentVariables"]
        self.assertEqual(environment["LIFE_MANAGER_INVESTMENT_MODE"], "shadow")
        self.assertEqual(environment["ALPACA_INVESTMENT_SHADOW_CREDENTIALS_FILE"],
                         str(Path.home() / ".local/share/anicca/credentials.json"))
        self.assertEqual(environment["ALPACA_INVESTMENT_SHADOW_STATE_DIR"],
                         str(Path.home() / ".local/state/life-manager/alpaca-investment-shadow"))
        self.assertNotEqual(environment["ALPACA_INVESTMENT_PAPER_STATE_DIR"],
                            environment["ALPACA_INVESTMENT_SHADOW_STATE_DIR"])

    def test_alpaca_live_plist_uses_live_credentials_and_separate_state(self):
        value = registry()
        entry = value["loops"].pop("example")
        entry.update({"effect_class": "money", "state_root": "~/.local/state/life-manager/alpaca-investment-live"})
        value["loops"]["alpaca-investment-live"] = entry
        rendered = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        environment = rendered["EnvironmentVariables"]
        self.assertEqual(environment["LIFE_MANAGER_INVESTMENT_MODE"], "live")
        self.assertEqual(environment["ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE"],
                         str(Path.home() / ".local/share/anicca/credentials.json"))
        self.assertEqual(environment["ALPACA_INVESTMENT_LIVE_STATE_DIR"],
                         str(Path.home() / ".local/state/life-manager/alpaca-investment-live"))

    def test_agent_economy_plist_owns_code_and_mutable_home_paths(self):
        value = registry()
        value["loops"]["agent-economy-loop"] = value["loops"].pop("example")
        rendered = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        environment = rendered["EnvironmentVariables"]
        state = Path.home() / ".local/state/life-manager/example"
        instance = state / "instance"
        earn_state = instance / "state/skills/earn"
        self.assertEqual(environment["ANICCA_REPO"], str(self.root.resolve()))
        self.assertEqual(environment["ANICCA_CODE_ROOT"], str(self.root.resolve()))
        self.assertEqual(environment["ANICCA_RELEASE_ROOT"], str(self.root.resolve().parent.parent))
        self.assertEqual(environment["ANICCA_HOME"], str(instance))
        self.assertEqual(environment["EARN_STATE_ROOT"], str(earn_state))
        self.assertEqual(environment["EARN_LEDGER"], str(earn_state / "earn-ledger.jsonl"))
        self.assertNotIn("CEO_EFFECTIVE_CRON_DIR", environment)

    def test_franklin_plists_own_release_code_and_instance_state(self):
        entrypoint = self.root / "runtime/anicca-daemon.sh"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("#!/bin/sh\nexit 0\n")
        entrypoint.chmod(0o755)
        for loop_id, instance, state_root in (
            ("franklin-loop", "franklin", "~/.blockrun"),
            ("franklin2-loop", "franklin2", "~/.franklin2-home/.blockrun"),
        ):
            with self.subTest(loop_id=loop_id):
                value = registry("runtime/anicca-daemon.sh")
                entry = value["loops"].pop("example")
                entry.update({"label": f"ai.anicca.{loop_id}", "state_root": state_root})
                value["loops"][loop_id] = entry
                environment = plistlib.loads(
                    build_apply_plan(value, self.root, SHA)[0]["plist_bytes"]
                )["EnvironmentVariables"]
                self.assertEqual(environment["ANICCA_REPO"], str(self.root.resolve()))
                self.assertEqual(environment["ANICCA_INSTANCE"], instance)
                self.assertEqual(environment["ANICCA_HOME"], os.path.expanduser(state_root))

    def test_compute_proxy_plist_pins_owned_home_port_and_node(self):
        entrypoint = self.root / "runtime/compute-proxy/start-local.sh"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("#!/bin/sh\nexit 0\n")
        entrypoint.chmod(0o755)
        value = registry("runtime/compute-proxy/start-local.sh")
        entry = value["loops"].pop("example")
        entry.update({
            "adapter": "exec", "command": ["--proxy-only"],
            "label": "ai.anicca.compute-proxy", "state_root": "~/.anicca",
        })
        value["loops"]["compute-proxy"] = entry
        with patch("runtime.loop.lm_loop_apply.shutil.which", return_value="/managed/bin/node"):
            rendered = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        environment = rendered["EnvironmentVariables"]
        self.assertEqual(environment["ANICCA_HOME"], os.path.expanduser("~/.anicca"))
        self.assertEqual(environment["COMPUTE_PROXY_PORT"], "18402")
        self.assertEqual(environment["LIFE_MANAGER_NODE"], "/managed/bin/node")

    def test_writer_plist_projects_one_state_log_and_env_contract(self):
        writer_entrypoint = self.root / "skills/writer-agent/article-daily.sh"
        writer_entrypoint.parent.mkdir(parents=True)
        writer_entrypoint.write_text("#!/bin/sh\nexit 0\n")
        writer_entrypoint.chmod(0o755)
        value = registry("skills/writer-agent/article-daily.sh")
        value["loops"]["article-daily"] = value["loops"].pop("example")
        value["loops"]["article-daily"].update({
            "label": "ai.anicca.article-daily",
            "state_root": "~/.local/state/life-manager/writer",
            "log_root": "~/.local/state/life-manager/writer/logs",
        })
        environment = plistlib.loads(
            build_apply_plan(value, self.root, SHA)[0]["plist_bytes"]
        )["EnvironmentVariables"]
        writer = str(self.root.resolve() / "skills/writer-agent")
        state = str(Path.home() / ".local/state/life-manager/writer")
        self.assertEqual(environment["ARTICLE_ROOT"], writer)
        self.assertEqual(environment["ARTICLE_SKILL_DIR"], writer)
        self.assertEqual(environment["ARTICLE_STATE_DIR"], state)
        self.assertEqual(environment["WRITER_STATE_DIR"], state)
        self.assertEqual(environment["WRITER_LOG_DIR"], f"{state}/logs")
        self.assertEqual(
            environment["LIFE_MANAGER_ENV_FILE"],
            str(Path.home() / ".local/state/life-manager/.env"),
        )
        self.assertEqual(
            environment["LIFE_MANAGER_PYTHON"],
            str(Path.home() / ".local/share/life-manager/venv/bin/python"),
        )

        repository = Path(__file__).resolve().parents[3]
        runtime_contract = repository / "skills/writer-agent/scripts/writer-runtime-env.sh"
        runtime = subprocess.run(
            [
                "bash", "-c",
                f'source "{runtime_contract}" && printf "%s" "$WRITER_BROWSER_PYTHON"',
            ],
            text=True,
            capture_output=True,
            env={
                **os.environ,
                **environment,
                "LIFE_MANAGER_REPO": str(repository),
                "LIFE_MANAGER_ENV_FILE": str(self.root / "missing.env"),
            },
        )
        self.assertEqual(runtime.returncode, 0, runtime.stderr)
        self.assertEqual(runtime.stdout, environment["LIFE_MANAGER_PYTHON"])

    def test_polymarket_plists_project_managed_python_and_install_env(self):
        for loop_id, entrypoint in (
            ("pm-decision-loop", "skills/earn/polymarket-trade/run_decision_loop.sh"),
            ("pm-live-trade", "skills/earn/polymarket-trade/run.sh"),
        ):
            with self.subTest(loop_id=loop_id):
                script = self.root / entrypoint
                script.parent.mkdir(parents=True, exist_ok=True)
                script.write_text("#!/bin/sh\nexit 0\n")
                script.chmod(0o755)
                value = registry(entrypoint)
                value["loops"][loop_id] = value["loops"].pop("example")
                environment = plistlib.loads(
                    build_apply_plan(value, self.root, SHA)[0]["plist_bytes"]
                )["EnvironmentVariables"]
                self.assertEqual(
                    environment["LIFE_MANAGER_ENV_FILE"],
                    str(Path.home() / ".local/state/life-manager/.env"),
                )
                self.assertEqual(
                    environment["LIFE_MANAGER_PYTHON"],
                    str(Path.home() / ".local/share/life-manager/venv/bin/python"),
                )
                self.assertEqual(environment["LIFE_MANAGER_NODE"], shutil.which("node"))
                self.assertTrue(Path(environment["LIFE_MANAGER_NODE"]).is_absolute())

    def test_browser_owner_is_projected_into_shared_runtime_environment(self):
        value = registry()
        value["loops"]["example"]["browser_owner"] = {
            "cdp_port": 9327,
            "profile": "~/.cloak/profiles/affiliate/impact-en",
        }
        rendered = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        environment = rendered["EnvironmentVariables"]
        self.assertEqual(environment["LIFE_MANAGER_BROWSER_CDP_PORT"], "9327")
        self.assertEqual(
            environment["LIFE_MANAGER_BROWSER_PROFILE"],
            str(Path.home() / ".cloak/profiles/affiliate/impact-en"),
        )

    def test_storefront_plist_uses_daily_driver_auth_vault(self):
        value = registry()
        value["loops"]["hf-gig-storefront-direct"] = value["loops"].pop("example")
        rendered = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        self.assertEqual(
            rendered["EnvironmentVariables"]["CLOAK_SESSION_VAULT_FILE"],
            str(Path.home() / ".cloak/vault/gig-daily-driver/auth-state.json"),
        )

    def test_apply_parks_authenticated_context_between_natural_wakes(self):
        value = registry()
        value["loops"]["hf-gig-apply-direct"] = value["loops"].pop("example")
        rendered = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        self.assertEqual(
            rendered["EnvironmentVariables"]["CLOAK_CONTEXT_PARK_ON_IDLE"], "1",
        )

    def test_coconala_reply_uses_healthy_shared_cdp_with_gig_auth(self):
        value = registry()
        value["loops"]["hf-gig-reply-detector"] = value["loops"].pop("example")
        rendered = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        environment = rendered["EnvironmentVariables"]
        self.assertEqual(environment["CLOAK_CDP_BASE_URL"], "http://127.0.0.1:9222")
        self.assertEqual(
            environment["CLOAK_SESSION_VAULT_FILE"],
            str(Path.home() / ".cloak/vault/gig-daily-driver/auth-state.json"),
        )
        self.assertEqual(
            environment["CLOAK_CONTEXT_LEASES_FILE"],
            str(Path.home() / ".cloak/vault/coconala-reply-leases.json"),
        )
        self.assertEqual(
            environment["CLOAK_TARGET_OWNERS_FILE"],
            str(Path.home() / ".cloak/vault/coconala-reply-targets.json"),
        )
        self.assertEqual(environment["CLOAK_CONTEXT_PARK_ON_IDLE"], "1")

    def test_realtime_guide_plist_projects_canonical_env(self):
        entrypoint = "skills/anicca-life-manager/scripts/realtime_guide.py"
        script = self.root / entrypoint
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        value = registry(entrypoint)
        entry = value["loops"].pop("example")
        entry.update({"label": "ai.anicca.realtime-guide"})
        value["loops"]["realtime-guide"] = entry
        environment = plistlib.loads(
            build_apply_plan(value, self.root, SHA)[0]["plist_bytes"]
        )["EnvironmentVariables"]
        self.assertEqual(
            environment["LIFE_MANAGER_ENV_FILE"],
            str(Path.home() / ".local/state/life-manager/.env"),
        )
        self.assertEqual(
            environment["LIFE_MANAGER_PYTHON"],
            str(Path.home() / ".local/share/life-manager/venv/bin/python"),
        )

    def test_realtime_guide_apply_retires_openclaw_home_and_working_directory(self):
        loop_id = "realtime-guide"
        release = self._release("release-realtime-guide").resolve()
        entrypoint = release / "skills/anicca-life-manager/scripts/realtime_guide.py"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("#!/usr/bin/env python3\n")
        entrypoint.chmod(0o755)
        registry_value = registry("skills/anicca-life-manager/scripts/realtime_guide.py")
        entry = registry_value["loops"].pop("example")
        entry.update({"label": "ai.anicca.realtime-guide"})
        registry_value["loops"][loop_id] = entry
        (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
        current = self.root / "current-realtime-guide"
        current.symlink_to(release)
        values = self._apply_kwargs(
            current, self.root / "apply-realtime-guide.lock",
            [str(release / "bin/lm-loop-run"), loop_id, str(release)],
            label="ai.anicca.realtime-guide",
            agents_dir_name="LaunchAgents-realtime-guide",
        )
        rendered = build_apply_plan(registry_value, release, SHA)[0]
        target = values["agents_dir"] / "ai.anicca.realtime-guide.plist"
        installed = plistlib.loads(rendered["plist_bytes"])
        installed["EnvironmentVariables"].update({
            "ANICCA_HOME": "/legacy/.openclaw",
            "OPENCLAW_ENV_FILE": "/legacy/.openclaw/.env",
            "REALTIME_GUIDE_STATE_DIR": "/legacy/.openclaw/state",
            "REALTIME_GUIDE_OPERATOR_SETTING": "kept",
        })
        installed["WorkingDirectory"] = "/legacy/.openclaw"
        target.write_bytes(plistlib.dumps(installed, fmt=plistlib.FMT_XML, sort_keys=True))

        result = apply_live(
            release, values["agents_dir"], values["launchctl_safe"], target=loop_id,
            current=current, lock_path=values["lock_path"], event_writer=lambda *_: None,
        )

        self.assertTrue(result[0]["changed"])
        result_plist = plistlib.loads(target.read_bytes())
        environment = result_plist["EnvironmentVariables"]
        self.assertTrue({"ANICCA_HOME", "OPENCLAW_ENV_FILE", "REALTIME_GUIDE_STATE_DIR"}.isdisjoint(environment))
        self.assertEqual(environment["REALTIME_GUIDE_OPERATOR_SETTING"], "kept")
        self.assertNotIn("WorkingDirectory", result_plist)

    def test_lateness_apply_retires_openclaw_environment(self):
        loop_id = "lateness-heartbeat"
        release = self._release("release-lateness").resolve()
        entrypoint = release / "skills/anicca-life-manager/scripts/run.sh"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("#!/bin/sh\n")
        entrypoint.chmod(0o755)
        registry_value = registry("skills/anicca-life-manager/scripts/run.sh")
        entry = registry_value["loops"].pop("example")
        entry.update({"label": "ai.anicca.lateness-heartbeat"})
        registry_value["loops"][loop_id] = entry
        (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
        current = self.root / "current-lateness"
        current.symlink_to(release)
        values = self._apply_kwargs(
            current, self.root / "apply-lateness.lock",
            [str(release / "bin/lm-loop-run"), loop_id, str(release)],
            label="ai.anicca.lateness-heartbeat",
            agents_dir_name="LaunchAgents-lateness",
        )
        rendered = build_apply_plan(registry_value, release, SHA)[0]
        target = values["agents_dir"] / "ai.anicca.lateness-heartbeat.plist"
        installed = plistlib.loads(rendered["plist_bytes"])
        installed["EnvironmentVariables"].update({
            "ANICCA_HOME": "/legacy/.openclaw",
            "OPENCLAW_ENV_FILE": "/legacy/.openclaw/.env",
            "LATENESS_OPERATOR_SETTING": "kept",
        })
        target.write_bytes(plistlib.dumps(installed, fmt=plistlib.FMT_XML, sort_keys=True))

        apply_live(
            release, values["agents_dir"], values["launchctl_safe"], target=loop_id,
            current=current, lock_path=values["lock_path"], event_writer=lambda *_: None,
        )

        environment = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
        self.assertNotIn("ANICCA_HOME", environment)
        self.assertNotIn("OPENCLAW_ENV_FILE", environment)
        self.assertEqual(environment["LATENESS_OPERATOR_SETTING"], "kept")
        self.assertEqual(
            environment["LIFE_MANAGER_PYTHON"],
            str(Path.home() / ".local/share/life-manager/venv/bin/python"),
        )

    def test_generic_install_does_not_secure_launchd_log_files(self):
        log_root = self.root / ".local/state/test-log-root"
        log_root.mkdir(mode=0o755, parents=True)
        state_root = self.root / ".local/state/test-state-root"
        state_root.mkdir(mode=0o755, parents=True)
        existing_stdout = log_root / "launchd.out.log"
        existing_stdout.write_text("old\n")
        existing_stdout.chmod(0o644)
        value = registry()
        value["loops"]["example"]["log_root"] = "~/.local/state/test-log-root"
        value["loops"]["example"]["state_root"] = "~/.local/state/test-state-root"
        target = self.root / "installed.plist"

        def launchctl(args):
            if args[0] == "print":
                if not target.is_file():
                    return 1, ""
                current = plistlib.loads(target.read_bytes())
                return 0, "arguments = {\n" + "\n".join(
                    current["ProgramArguments"]
                ) + "\n}\n"
            return 0, ""

        with patch.dict(os.environ, {"HOME": str(self.root)}):
            rendered = build_apply_plan(value, self.root, SHA)[0]
            result = install_one(rendered, target, launchctl, attempts=1, sleeper=lambda _: None)

        self.assertTrue(result["ok"])
        self.assertEqual(stat.S_IMODE(log_root.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(existing_stdout.stat().st_mode), 0o644)
        self.assertFalse((log_root / "launchd.err.log").exists())

    def test_generic_install_creates_missing_state_and_log_roots(self):
        value = registry()
        state_root = self.root / ".local/state/new-state-root"
        log_root = self.root / ".local/state/new-log-root"
        value["loops"]["example"]["state_root"] = "~/.local/state/new-state-root"
        value["loops"]["example"]["log_root"] = "~/.local/state/new-log-root"
        target = self.root / "installed.plist"

        def launchctl(args):
            if args[0] == "print":
                if not target.is_file():
                    return 1, "not loaded"
                current = plistlib.loads(target.read_bytes())
                return 0, "arguments = {\n" + "\n".join(
                    current["ProgramArguments"]
                ) + "\n}\n"
            return 0, ""

        with patch.dict(os.environ, {"HOME": str(self.root)}):
            rendered = build_apply_plan(value, self.root, SHA)[0]
            result = install_one(
                rendered, target, launchctl, attempts=1, sleeper=lambda _: None
            )

        self.assertTrue(result["ok"])
        self.assertTrue(state_root.is_dir())
        self.assertTrue(log_root.is_dir())
        self.assertFalse((log_root / "launchd.out.log").exists())
        self.assertFalse((log_root / "launchd.err.log").exists())
        self.assertEqual(stat.S_IMODE(state_root.stat().st_mode), 0o755)

    def test_money_printer_install_secures_existing_and_new_launchd_log_files(self):
        cases = (
            (
                "money-printer-symphony-bridge",
                "ai.anicca.life-manager-money-printer-symphony-bridge",
                "money-printer-log-root",
                "installed-money-printer.plist",
            ),
            (
                "money-printer-symphony",
                "ai.anicca.life-manager-money-printer-symphony",
                "money-printer-symphony-log-root",
                "installed-money-printer-symphony.plist",
            ),
        )
        for loop_id, label, log_name, target_name in cases:
            with self.subTest(loop_id=loop_id):
                log_root = self.root / ".local/state" / log_name
                log_root.mkdir(mode=0o755, parents=True)
                state_root = self.root / ".local/state" / f"{log_name}-state-root"
                state_root.mkdir(mode=0o755, parents=True)
                existing_stdout = log_root / "launchd.out.log"
                existing_stdout.write_text("old\n")
                existing_stdout.chmod(0o644)
                value = money_printer_registry(loop_id=loop_id, label=label)
                value["loops"][loop_id]["log_root"] = (
                    f"~/.local/state/{log_name}"
                )
                value["loops"][loop_id]["state_root"] = (
                    f"~/.local/state/{log_name}-state-root"
                )
                target = self.root / target_name

                def launchctl(args):
                    if args[0] == "print":
                        if not target.is_file():
                            return 1, ""
                        current = plistlib.loads(target.read_bytes())
                        return 0, "arguments = {\n" + "\n".join(
                            current["ProgramArguments"]
                        ) + "\n}\n"
                    return 0, ""

                with patch.dict(os.environ, {"HOME": str(self.root)}):
                    rendered = build_apply_plan(value, self.root, SHA)[0]
                    plist = plistlib.loads(rendered["plist_bytes"])
                    result = install_one(
                        rendered, target, launchctl, attempts=1, sleeper=lambda _: None
                    )

                self.assertEqual(plist["Umask"], 0o077)
                self.assertTrue(result["ok"])
                self.assertEqual(stat.S_IMODE(log_root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(existing_stdout.stat().st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE((log_root / "launchd.err.log").stat().st_mode), 0o600
                )
                self.assertEqual(stat.S_IMODE(state_root.stat().st_mode), 0o700)

    def test_sub_ten_second_interval_sets_matching_launchd_throttle(self):
        value = registry()
        value["loops"]["example"]["cadence"] = {"start_interval_seconds": 5}
        plist = plistlib.loads(build_apply_plan(value, self.root, SHA)[0]["plist_bytes"])
        self.assertEqual((plist["StartInterval"], plist.get("ThrottleInterval")), (5, 5))

    def test_invalid_generation_causes_zero_installer_calls(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "missing entrypoint"):
            apply_registry(registry("bin/missing.sh"), self.root, SHA, calls.append)
        self.assertEqual(calls, [])

    def test_non_executable_entrypoint_is_rejected_before_install(self):
        (self.root / "bin/example.sh").chmod(0o644)
        calls = []
        with self.assertRaisesRegex(ValueError, "not executable"):
            apply_registry(registry(), self.root, SHA, calls.append)
        self.assertEqual(calls, [])

    def test_valid_generation_installs_after_complete_preflight(self):
        calls = []
        result = apply_registry(registry(), self.root, SHA, lambda item: calls.append(item) or {
            "label": item["label"], "loaded_arguments": item["expected_arguments"], "ok": True,
        })
        self.assertEqual(len(calls), 1)
        self.assertEqual(result[0]["loaded_arguments"], calls[0]["expected_arguments"])

    def test_connector_release_requires_locked_browser_dependencies(self):
        value = registry("skills/connector/run.sh")
        value["loops"]["life-manager-connector-native"] = value["loops"].pop("example")
        value["loops"]["life-manager-connector-native"]["label"] = (
            "ai.anicca.life-manager-connector-native"
        )
        (self.root / "skills/connector").mkdir(parents=True)
        (self.root / "skills/connector/run.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.root / "skills/connector/run.sh").chmod(0o755)
        calls = []

        with self.assertRaisesRegex(ValueError, "Connector runtime dependencies missing"):
            apply_registry(value, self.root, SHA, calls.append)
        self.assertEqual(calls, [])

        for dependency in ("playwright-core", "jsqr"):
            package = self.root / "apps/life-manager/node_modules" / dependency / "package.json"
            package.parent.mkdir(parents=True)
            package.write_text("{}\n")

        apply_registry(value, self.root, SHA, calls.append)
        self.assertEqual([item["loop_id"] for item in calls], ["life-manager-connector-native"])

    def test_targeted_apply_ignores_unrelated_missing_entrypoint(self):
        calls = []
        installer = lambda item: calls.append(item) or item
        value = two_loop_registry()
        value["loops"]["example"]["entrypoint"] = "bin/missing.sh"
        result = apply_registry(value, self.root, SHA, installer, target="second")
        self.assertEqual([item["loop_id"] for item in calls], ["second"])
        self.assertEqual([item["loop_id"] for item in result], ["second"])
        with self.assertRaisesRegex(ValueError, "unknown apply target"):
            apply_registry(two_loop_registry(), self.root, SHA, installer, target="missing")
        self.assertEqual([item["loop_id"] for item in calls], ["second"])

    def test_failed_swap_restores_previous_plist_and_loaded_job(self):
        target = self.root / "installed.plist"
        old = plistlib.dumps({"Label": "ai.anicca.example", "ProgramArguments": ["/old/run.sh"]})
        target.write_bytes(old)
        rendered = build_apply_plan(registry(), self.root, SHA)[0]
        calls = []

        def launchctl(args):
            calls.append(args)
            if args[0] == "print" and len(calls) == 1:
                return 0, "arguments = {\n/old/run.sh\n}\n"
            if args[0] == "bootstrap" and target.read_bytes() != old:
                return 5, "new bootstrap failed"
            if args[0] == "print":
                return 0, "arguments = {\n/old/run.sh\n}\n"
            return 0, ""

        with self.assertRaisesRegex(RuntimeError, "restored previous job"):
            install_one(rendered, target, launchctl, attempts=1)
        self.assertEqual(target.read_bytes(), old)
        self.assertGreaterEqual(sum(call[0] == "bootstrap" for call in calls), 2)

    def test_swap_preserves_existing_operational_attributes_but_drops_undeclared_working_directory(self):
        target = self.root / "installed.plist"
        target.write_bytes(plistlib.dumps({
            "Label": "ai.anicca.example",
            "ProgramArguments": ["/old/run.sh"],
            "EnvironmentVariables": {
                "CUSTOM": "kept",
                "CODEX_HOME": "/tmp/legacy-codex-home",
                "LIFE_MANAGER_REPO": "/old/missing/release",
                "LIFE_MANAGER_RELEASE_SHA": "old",
            },
            "WorkingDirectory": "/var/tmp/example",
            "ProcessType": "Interactive",
            "RunAtLoad": True,
            "ThrottleInterval": 30,
        }))
        rendered = build_apply_plan(registry(), self.root, SHA)[0]

        def launchctl(args):
            if args[0] == "print":
                current = plistlib.loads(target.read_bytes())
                return 0, "arguments = {\n" + "\n".join(current["ProgramArguments"]) + "\n}\n"
            return 0, ""

        result = install_one(rendered, target, launchctl, attempts=1)
        installed = plistlib.loads(target.read_bytes())
        self.assertTrue(result["ok"])
        self.assertEqual(installed["EnvironmentVariables"]["CUSTOM"], "kept")
        self.assertNotIn("CODEX_HOME", installed["EnvironmentVariables"])
        self.assertEqual(installed["EnvironmentVariables"]["LIFE_MANAGER_REPO"], str(self.root.resolve()))
        self.assertEqual(installed["EnvironmentVariables"]["LIFE_MANAGER_RELEASE_SHA"], SHA)
        self.assertNotIn("WorkingDirectory", installed)
        self.assertEqual(installed["ProcessType"], "Interactive")
        self.assertTrue(installed["RunAtLoad"])
        self.assertEqual(installed["ThrottleInterval"], 30)
        self.assertEqual(installed["ProgramArguments"], rendered["expected_arguments"])

    def test_swap_drops_stale_legacy_release_working_directory(self):
        target = self.root / "installed.plist"
        target.write_bytes(plistlib.dumps({
            "Label": "ai.anicca.example",
            "ProgramArguments": ["/old/run.sh"],
            "WorkingDirectory": str(
                Path.home() / "loops" / "connector" / "releases" / "20260827T171500-57ed7c000"
            ),
        }))
        rendered = build_apply_plan(registry(), self.root, SHA)[0]

        def launchctl(args):
            if args[0] == "print":
                current = plistlib.loads(target.read_bytes())
                return 0, "arguments = {\n" + "\n".join(current["ProgramArguments"]) + "\n}\n"
            return 0, ""

        result = install_one(rendered, target, launchctl, attempts=1)
        installed = plistlib.loads(target.read_bytes())
        self.assertTrue(result["ok"])
        self.assertNotIn("WorkingDirectory", installed)
        self.assertEqual(installed["ProgramArguments"], rendered["expected_arguments"])

    def test_swap_waits_for_launchd_to_settle_after_bootout(self):
        target = self.root / "installed.plist"
        target.write_bytes(plistlib.dumps({
            "Label": "ai.anicca.example", "ProgramArguments": ["/old/run.sh"]}))
        rendered = build_apply_plan(registry(), self.root, SHA)[0]
        sleeps = []

        def launchctl(args):
            if args[0] == "print":
                return 0, "arguments = {\n" + "\n".join(rendered["expected_arguments"]) + "\n}\n"
            return 0, ""

        install_one(rendered, target, launchctl, attempts=1, sleeper=sleeps.append)
        self.assertEqual(sleeps, [1.0])

    def test_reconcile_rebinds_unloaded_plist_without_loading_it(self):
        target = self.root / "installed.plist"
        target.write_bytes(plistlib.dumps({
            "Label": "ai.anicca.example", "ProgramArguments": ["/old/run.sh"]}))
        rendered = build_apply_plan(registry(), self.root, SHA)[0]
        calls = []

        def launchctl(args):
            calls.append(args)
            return (1, "not loaded") if args[0] == "print" else (0, "")

        result = install_one(
            rendered, target, launchctl, preserve_unloaded=True)

        self.assertTrue(result["ok"])
        self.assertFalse(result["loaded"])
        self.assertEqual(calls, [["print", f"gui/{os.getuid()}/ai.anicca.example"]])
        self.assertEqual(
            plistlib.loads(target.read_bytes())["ProgramArguments"],
            rendered["expected_arguments"],
        )

    def test_swap_increases_settle_time_before_retry(self):
        target = self.root / "installed.plist"
        target.write_bytes(plistlib.dumps({
            "Label": "ai.anicca.example", "ProgramArguments": ["/old/run.sh"]}))
        rendered = build_apply_plan(registry(), self.root, SHA)[0]
        sleeps, bootstraps = [], 0

        def launchctl(args):
            nonlocal bootstraps
            if args[0] == "bootstrap":
                bootstraps += 1
                return (5, "teardown pending") if bootstraps == 1 else (0, "")
            if args[0] == "print":
                return 0, "arguments = {\n" + "\n".join(rendered["expected_arguments"]) + "\n}\n"
            return 0, ""

        install_one(rendered, target, launchctl, attempts=2, sleeper=sleeps.append)
        self.assertEqual(sleeps, [1.0, 3.0])

    def test_apply_rejects_busy_owner_before_launchctl_or_plist_mutation(self):
        release = self._release("release-a")
        current = self.root / "current"
        current.symlink_to(release)
        lock_path = self.root / "apply.lock"
        values = self._apply_kwargs(current, lock_path)

        item_lock = lock_path.with_name(lock_path.name + ".ai.anicca.example.lock")
        with item_lock.open("a+") as owner_lock:
            fcntl.flock(owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "production apply is already owned"):
                apply_live(
                    release,
                    values["agents_dir"],
                    values["launchctl_safe"],
                    current=current,
                    lock_path=lock_path,
                )

        self.assertEqual(list(values["agents_dir"].iterdir()), [])
        self.assertEqual(values["calls"].read_text().splitlines(), ["preflight"])

    def test_apply_pins_requested_immutable_release_when_current_moves(self):
        release_a = self._release("release-a")
        release_b = self._release("release-b")
        current = self.root / "current"
        current.symlink_to(release_b)
        expected_arguments = [
            str(release_a.resolve() / "bin/lm-loop-run"),
            "example",
            str(release_a.resolve()),
        ]
        values = self._apply_kwargs(
            current, self.root / "apply.lock", expected_arguments)

        result = apply_live(
            release_a,
            values["agents_dir"],
            values["launchctl_safe"],
            current=current,
            lock_path=values["lock_path"],
        )

        self.assertTrue(result[0]["ok"])
        installed = plistlib.loads(
            (values["agents_dir"] / "ai.anicca.example.plist").read_bytes())
        self.assertEqual(installed["ProgramArguments"][2], str(release_a.resolve()))

    def test_reconcile_pins_one_explicit_release_for_the_whole_route(self):
        release = self._release("release-a").resolve()
        command_release = self._release("release-b").resolve()
        (release / "config/loop-registry.json").write_text(json.dumps(two_loop_registry()))
        (command_release / "config/loop-registry.json").write_text(
            json.dumps(two_loop_registry())
        )
        rows = [
            {
                "classification": "managed",
                "provider_route": "deterministic",
                "launchd_state": "loaded-idle",
                "installed_release_sha": "b" * 40,
                "loop_id": loop_id,
            }
            for loop_id in ("example", "second")
        ]
        applied_roots = []

        def record_apply(release_root, *args, **kwargs):
            applied_roots.append(release_root)
            return [{"ok": True, "release_sha": SHA}]

        with (
            patch.object(lm_loop, "ROOT", command_release),
            patch.object(lm_loop, "snapshot", return_value=rows),
            patch.object(lm_loop, "apply_live", side_effect=record_apply),
            patch.dict(os.environ, {"LIFE_MANAGER_RELEASE_ROOT": str(release)}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(lm_loop.main(["reconcile", "deterministic"]), 0)

        self.assertEqual(applied_roots, [release, release])

    def test_reconcile_loaded_idle_only_leaves_unloaded_rows_untouched(self):
        release = self._release("release-a").resolve()
        rows = [
            {
                "classification": "managed",
                "provider_route": "deterministic",
                "launchd_state": "loaded-idle",
                "installed_release_sha": "b" * 40,
                "loop_id": "example",
            },
            {
                "classification": "managed",
                "provider_route": "deterministic",
                "launchd_state": "unloaded",
                "installed_release_sha": "b" * 40,
                "loop_id": "unloaded",
            },
            {
                "classification": "managed",
                "provider_route": "deterministic",
                "launchd_state": "loaded-running",
                "installed_release_sha": "b" * 40,
                "loop_id": "running",
            },
        ]
        applied = []

        def record_apply(release_root, *args, **kwargs):
            applied.append(release_root)
            return [{"ok": True, "release_sha": SHA}]

        with (
            patch.object(lm_loop, "ROOT", release),
            patch.object(lm_loop, "snapshot", return_value=rows),
            patch.object(lm_loop, "apply_live", side_effect=record_apply),
            patch.dict(os.environ, {"LIFE_MANAGER_RELEASE_ROOT": str(release)}),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                lm_loop.main(["reconcile", "deterministic", "--loaded-idle-only"]),
                0,
            )

        self.assertEqual(applied, [release])
        report = json.loads(output.getvalue())
        self.assertEqual(report["eligible"], 1)
        self.assertEqual(report["skipped_running"], ["running"])

    def test_release_reconciler_does_not_starve_unproven_scheduled_release(self):
        release = self._release("release-a").resolve()
        value = two_loop_registry()
        for entry in value["loops"].values():
            entry["provider_route"] = "shared-agent-runner"
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        rows = [
            {
                "classification": "managed",
                "provider_route": "shared-agent-runner",
                "launchd_state": "loaded-idle",
                "installed_release_sha": "b" * 40,
                "event_release_sha": event_release,
                "loop_id": loop_id,
            }
            for loop_id, event_release in (
                ("example", "a" * 40),
                ("second", "b" * 40),
            )
        ]
        applied = []

        def record_apply(_release_root, *args, **kwargs):
            applied.append(kwargs["target"])
            return [{"ok": True, "release_sha": SHA}]

        with (
            patch.object(lm_loop, "ROOT", release),
            patch.object(lm_loop, "snapshot", return_value=rows),
            patch.object(lm_loop, "apply_live", side_effect=record_apply),
            patch.dict(os.environ, {
                "LIFE_MANAGER_RELEASE_ROOT": str(release),
                "LIFE_MANAGER_LOOP_ID": "life-manager-release-reconciler",
            }),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(
                lm_loop.main(["reconcile", "shared-agent-runner", "--loaded-idle-only"]),
                0,
            )

        self.assertEqual(applied, ["second"])
        self.assertEqual(json.loads(output.getvalue())["eligible"], 1)

    def test_reconcile_loop_ids_limit_same_route_to_explicit_ids(self):
        release = self._release("release-a").resolve()
        value = registry()
        for loop_id in (
            "hf-gig-apply-direct",
            "hf-gig-reply-detector",
            "same-route-unrelated",
        ):
            value["loops"][loop_id] = {
                **value["loops"]["example"],
                "label": f"ai.anicca.{loop_id}",
                "provider_route": "shared-agent-runner",
            }
        value["loops"]["other-route"] = {
            **value["loops"]["example"],
            "label": "ai.anicca.other-route",
        }
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        rows = [
            {
                "classification": "managed",
                "provider_route": route,
                "launchd_state": "loaded-idle",
                "installed_release_sha": "b" * 40,
                "loop_id": loop_id,
            }
            for loop_id, route in (
                ("hf-gig-apply-direct", "shared-agent-runner"),
                ("hf-gig-reply-detector", "shared-agent-runner"),
                ("same-route-unrelated", "shared-agent-runner"),
                ("other-route", "deterministic"),
            )
        ]
        applied = []

        def record_apply(release_root, *args, **kwargs):
            applied.append(kwargs["target"])
            return [{"ok": True, "release_sha": SHA}]

        with (
            patch.object(lm_loop, "ROOT", release),
            patch.object(lm_loop, "snapshot", return_value=rows),
            patch.object(lm_loop, "apply_live", side_effect=record_apply),
            patch.dict(os.environ, {"LIFE_MANAGER_RELEASE_ROOT": str(release)}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                lm_loop.main([
                    "reconcile",
                    "shared-agent-runner",
                    "--loaded-idle-only",
                    "--loop-id",
                    "hf-gig-apply-direct",
                    "--loop-id",
                    "hf-gig-reply-detector",
                ]),
                0,
            )

        self.assertEqual(
            applied,
            ["hf-gig-apply-direct", "hf-gig-reply-detector"],
        )

    def test_old_release_reconciler_command_also_moves_idle_disk_cleanup(self):
        release = self._release("release-a").resolve()
        value = registry()
        value["loops"]["hf-gig-paid-direct"] = {
            **value["loops"]["example"],
            "label": "ai.anicca.hf-gig-paid-direct",
        }
        value["loops"]["life-manager-disk-cleanup"] = {
            **value["loops"]["example"],
            "label": "ai.anicca.life-manager-disk-cleanup",
        }
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        rows = [{
            "classification": "managed",
            "provider_route": "deterministic",
            "launchd_state": "loaded-idle",
            "installed_release_sha": "b" * 40,
            "event_release_sha": "b" * 40,
            "loop_id": loop_id,
        } for loop_id in ("hf-gig-paid-direct", "life-manager-disk-cleanup")]
        applied = []

        def record_apply(release_root, *args, **kwargs):
            applied.append(kwargs["target"])
            return [{"ok": True, "release_sha": SHA}]

        with (
            patch.object(lm_loop, "ROOT", release),
            patch.object(lm_loop, "snapshot", return_value=rows),
            patch.object(lm_loop, "apply_live", side_effect=record_apply),
            patch.dict(os.environ, {
                "LIFE_MANAGER_RELEASE_ROOT": str(release),
                "LIFE_MANAGER_LOOP_ID": "life-manager-release-reconciler",
            }),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(lm_loop.main([
                "reconcile", "deterministic", "--loaded-idle-only",
                "--loop-id", "hf-gig-paid-direct",
            ]), 0)

        self.assertEqual(applied, ["hf-gig-paid-direct", "life-manager-disk-cleanup"])

    def test_reconcile_explicit_running_owner_when_requested(self):
        release = self._release("release-a").resolve()
        value = registry()
        value["loops"]["hf-gig-reply-detector"] = {
            **value["loops"]["example"],
            "label": "ai.anicca.hf-gig-reply-detector",
            "provider_route": "shared-agent-runner",
        }
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        rows = [{
            "classification": "managed",
            "provider_route": "shared-agent-runner",
            "launchd_state": "loaded-running",
            "installed_release_sha": "b" * 40,
            "loop_id": loop_id,
        } for loop_id in ("example", "hf-gig-reply-detector")]
        applied = []

        def record_apply(release_root, *args, **kwargs):
            applied.append(kwargs)
            return [{"ok": True, "release_sha": SHA}]

        with (
            patch.object(lm_loop, "ROOT", release),
            patch.object(lm_loop, "snapshot", return_value=rows),
            patch.object(lm_loop, "apply_live", side_effect=record_apply),
            patch.dict(os.environ, {"LIFE_MANAGER_RELEASE_ROOT": str(release)}),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(lm_loop.main([
                "reconcile", "shared-agent-runner", "--include-running",
                "--loop-id", "hf-gig-reply-detector",
            ]), 0)

        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["target"], "hf-gig-reply-detector")
        self.assertFalse(applied[0]["skip_busy"])
        self.assertEqual(json.loads(output.getvalue())["eligible"], 1)

    def test_loaded_idle_reconcile_reloads_explicit_keep_alive_owner(self):
        release = self._release("release-a").resolve()
        value = registry()
        value["loops"]["example"]["provider_route"] = "shared-agent-runner"
        value["loops"]["hf-gig-reply-detector"] = {
            **value["loops"]["example"],
            "cadence": {"keep_alive": True},
            "label": "ai.anicca.hf-gig-reply-detector",
            "provider_route": "shared-agent-runner",
        }
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        rows = [{
            "classification": "managed",
            "provider_route": "shared-agent-runner",
            "launchd_state": "loaded-running",
            "installed_release_sha": "b" * 40,
            "loop_id": loop_id,
        } for loop_id in ("example", "hf-gig-reply-detector")]
        applied = []

        def record_apply(release_root, *args, **kwargs):
            applied.append(kwargs)
            return [{"ok": True, "release_sha": SHA}]

        with (
            patch.object(lm_loop, "ROOT", release),
            patch.object(lm_loop, "snapshot", return_value=rows),
            patch.object(lm_loop, "apply_live", side_effect=record_apply),
            patch.dict(os.environ, {"LIFE_MANAGER_RELEASE_ROOT": str(release)}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(lm_loop.main([
                "reconcile", "shared-agent-runner", "--loaded-idle-only",
                "--loop-id", "example",
                "--loop-id", "hf-gig-reply-detector",
            ]), 0)

        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]["target"], "hf-gig-reply-detector")
        self.assertFalse(applied[0]["skip_busy"])

    def test_reconcile_loop_id_invalid_values_fail_closed(self):
        release = self._release("release-a").resolve()
        value = registry()
        value["loops"]["shared"] = {
            **value["loops"]["example"],
            "label": "ai.anicca.shared",
            "provider_route": "shared-agent-runner",
        }
        value["loops"]["deterministic-only"] = {
            **value["loops"]["example"],
            "label": "ai.anicca.deterministic-only",
        }
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        for name, option in (
            ("missing", ["--loop-id"]),
            ("empty", ["--loop-id="]),
            ("unknown", ["--loop-id", "not-registered"]),
            ("wrong-route", ["--loop-id", "deterministic-only"]),
        ):
            with self.subTest(name=name), patch.object(lm_loop, "ROOT", release), \
                    patch.object(lm_loop, "snapshot", return_value=[]), \
                    patch.dict(os.environ, {"LIFE_MANAGER_RELEASE_ROOT": str(release)}), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(
                    lm_loop.main(["reconcile", "shared-agent-runner", *option]),
                    2,
                )
                self.assertIn("error", json.loads(output.getvalue()))

    def test_reconcile_rejects_unknown_option_before_snapshot(self):
        release = self._release("release-a").resolve()
        with (
            patch.object(lm_loop, "ROOT", release),
            patch.object(lm_loop, "snapshot", side_effect=AssertionError("snapshot called")),
            patch.dict(os.environ, {"LIFE_MANAGER_RELEASE_ROOT": str(release)}),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(lm_loop.main(["reconcile", "--not-an-option"]), 2)
        self.assertIn("error", json.loads(output.getvalue()))

    def test_reconcile_include_running_requires_explicit_owner(self):
        release = self._release("release-a").resolve()
        with (
            patch.object(lm_loop, "ROOT", release),
            patch.object(lm_loop, "snapshot", side_effect=AssertionError("snapshot called")),
            patch.dict(os.environ, {"LIFE_MANAGER_RELEASE_ROOT": str(release)}),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(lm_loop.main([
                "reconcile", "shared-agent-runner", "--include-running",
            ]), 2)
        self.assertIn("requires --loop-id", json.loads(output.getvalue())["error"])

    def test_reconcile_without_loop_id_keeps_unloaded_default_behavior(self):
        release = self._release("release-a").resolve()
        value = two_loop_registry()
        value["loops"]["second"]["provider_route"] = "deterministic"
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        rows = [
            {
                "classification": "managed",
                "provider_route": "deterministic",
                "launchd_state": state,
                "installed_release_sha": "b" * 40,
                "loop_id": loop_id,
            }
            for loop_id, state in (("example", "loaded-idle"), ("second", "unloaded"))
        ]
        applied = []

        def record_apply(release_root, *args, **kwargs):
            applied.append(kwargs["target"])
            return [{"ok": True, "release_sha": SHA}]

        with (
            patch.object(lm_loop, "ROOT", release),
            patch.object(lm_loop, "snapshot", return_value=rows),
            patch.object(lm_loop, "apply_live", side_effect=record_apply),
            patch.dict(os.environ, {"LIFE_MANAGER_RELEASE_ROOT": str(release)}),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(lm_loop.main(["reconcile", "deterministic"]), 0)

        self.assertEqual(applied, ["example", "second"])

    def test_loaded_idle_reconcile_skips_prelock_running_owner_without_mutation(self):
        release = self._release("release-a").resolve()
        current = self.root / "current"
        current.symlink_to(release)
        lock_path = self.root / "apply.lock"
        values = self._apply_kwargs(current, lock_path)
        target = values["agents_dir"] / "ai.anicca.example.plist"
        old_bytes = plistlib.dumps({
            "Label": "ai.anicca.example",
            "ProgramArguments": ["/old/run.sh"],
        })
        target.write_bytes(old_bytes)
        values["launchctl_safe"].write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(values['calls']))}\n"
            "if [ \"$1\" = print ]; then\n"
            "  printf '%s\\n' 'pid = 123'\n"
            "fi\n"
            "exit 0\n"
        )
        values["launchctl_safe"].chmod(0o755)
        rendered = build_apply_plan(registry(), release, SHA)[0]
        events = []
        item_lock = lock_path.with_name(lock_path.name + ".ai.anicca.example.lock")
        with item_lock.open("a+") as owner_lock:
            fcntl.flock(owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = apply_live(
                release,
                values["agents_dir"],
                values["launchctl_safe"],
                current=current,
                lock_path=lock_path,
                skip_busy=True,
                event_writer=events.append,
            )

        self.assertEqual(result, [{
            "ok": True,
            "label": "ai.anicca.example",
            "loaded": True,
            "loaded_arguments": [],
            "release_sha": SHA,
            "changed": False,
            "skipped": "loaded-running",
        }])
        self.assertEqual(target.read_bytes(), old_bytes)
        self.assertEqual(events, [])
        self.assertEqual(
            values["calls"].read_text().splitlines(),
            ["preflight", f"print gui/{os.getuid()}/ai.anicca.example"],
        )

    def test_loaded_idle_reconcile_skips_unloaded_after_lock_without_mutation(self):
        release = self._release("release-a").resolve()
        current = self.root / "current"
        current.symlink_to(release)
        lock_path = self.root / "apply.lock"
        values = self._apply_kwargs(current, lock_path)
        target = values["agents_dir"] / "ai.anicca.example.plist"
        old_bytes = plistlib.dumps({
            "Label": "ai.anicca.example",
            "ProgramArguments": ["/old/run.sh"],
        })
        target.write_bytes(old_bytes)
        values["launchctl_safe"].write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(values['calls']))}\n"
            "if [ \"$1\" = print ]; then\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n"
        )
        values["launchctl_safe"].chmod(0o755)
        rendered = build_apply_plan(registry(), release, SHA)[0]
        events = []
        result = apply_live(
            release,
            values["agents_dir"],
            values["launchctl_safe"],
            current=current,
            lock_path=lock_path,
            skip_busy=True,
            event_writer=events.append,
        )

        self.assertEqual(result, [{
            "ok": True,
            "label": "ai.anicca.example",
            "loaded": False,
            "loaded_arguments": [],
            "release_sha": SHA,
            "changed": False,
            "skipped": "unloaded",
        }])
        self.assertEqual(target.read_bytes(), old_bytes)
        self.assertEqual(events, [])
        self.assertEqual(
            values["calls"].read_text().splitlines(),
            ["preflight", f"print gui/{os.getuid()}/ai.anicca.example"],
        )

    def test_apply_current_release_records_real_launchctl_calls(self):
        release = self._release("release-a").resolve()
        current = self.root / "current"
        current.symlink_to(release)
        expected_arguments = [str(release / "bin/lm-loop-run"), "example", str(release)]
        values = self._apply_kwargs(
            current,
            self.root / "apply.lock",
            expected_arguments,
        )
        events = []

        result = apply_live(
            release,
            values["agents_dir"],
            values["launchctl_safe"],
            current=current,
            lock_path=values["lock_path"],
            event_writer=lambda path, event: events.append((path, event)),
        )

        self.assertTrue(result[0]["ok"])
        self.assertTrue(result[0]["changed"])
        self.assertTrue(values["calls"].is_file())
        calls = values["calls"].read_text().splitlines()
        self.assertEqual(calls[0], "preflight")
        self.assertEqual(calls, [
            "preflight",
            f"print gui/{os.getuid()}/ai.anicca.example",
            f"bootout gui/{os.getuid()}/ai.anicca.example",
            f"bootstrap gui/{os.getuid()} {values['agents_dir'] / 'ai.anicca.example.plist'}",
            f"print gui/{os.getuid()}/ai.anicca.example",
        ])
        self.assertTrue((values["agents_dir"] / "ai.anicca.example.plist").is_file())
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0][1]["loop_id"], events[0][1]["phase"]),
                         ("example", "plan"))

    def test_full_apply_removes_retired_loaded_job_after_absence_readback(self):
        release = self._release("release-a").resolve()
        value = json.loads((release / "config/loop-registry.json").read_text())
        value["retired_labels"] = ["ai.anicca.retired-example"]
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        current = self.root / "current"
        current.symlink_to(release)
        agents = self.root / "LaunchAgents"
        agents.mkdir()
        retired_plist = agents / "ai.anicca.retired-example.plist"
        retired_plist.write_text("old")
        service = f"gui/{os.getuid()}/ai.anicca.retired-example"
        managed_service = f"gui/{os.getuid()}/ai.anicca.example"
        expected = [str(release / "bin/lm-loop-run"), "example", str(release)]
        calls = []
        loaded = {service}

        def safe(_executable, args):
            calls.append(args)
            if args == ["preflight"]:
                return 0, "ok"
            if args[:1] == ["print"]:
                if args[1] not in loaded:
                    return 1, "absent"
                if args[1] == managed_service:
                    return 0, "arguments = {\n" + "\n".join(expected) + "\n}\n"
                return 0, "state = running"
            if args[:1] == ["bootout"]:
                loaded.discard(args[1])
                return 0, ""
            if args[:1] == ["bootstrap"]:
                loaded.add(managed_service)
                return 0, ""
            return 0, ""

        with patch.object(lm_loop, "_safe_launchctl", side_effect=safe):
            results = apply_live(
                release, agents, self.root / "launchctl-safe",
                current=current, lock_path=self.root / "apply.lock",
                event_writer=lambda *_: None,
            )

        retired = next(row for row in results if row.get("retired"))
        self.assertEqual(retired["label"], "ai.anicca.retired-example")
        self.assertFalse(retired_plist.exists())
        self.assertEqual(calls[:4], [
            ["preflight"], ["print", service], ["bootout", service], ["print", service],
        ])

    def test_retirement_waits_for_asynchronous_bootout_absence(self):
        release = self._release("release-a").resolve()
        value = json.loads((release / "config/loop-registry.json").read_text())
        value["retired_labels"] = ["ai.anicca.retired-example"]
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        current = self.root / "current"
        current.symlink_to(release)
        agents = self.root / "LaunchAgents"
        agents.mkdir()
        retired_plist = agents / "ai.anicca.retired-example.plist"
        retired_plist.write_text("old")
        service = f"gui/{os.getuid()}/ai.anicca.retired-example"
        expected = [str(release / "bin/lm-loop-run"), "example", str(release)]
        calls = []
        post_bootout_prints = 0

        def safe(_executable, args):
            nonlocal post_bootout_prints
            calls.append(args)
            if args == ["preflight"]:
                return 0, "ok"
            if args == ["print", service]:
                if ["bootout", service] not in calls:
                    return 0, "state = running"
                post_bootout_prints += 1
                return ((0, "state = running") if post_bootout_prints == 1
                        else (1, "Could not find service"))
            if args == ["bootout", service]:
                return 0, ""
            if args[:1] == ["print"]:
                return 1, "absent"
            if args[:1] == ["bootstrap"]:
                return 0, ""
            return 0, ""

        with (
            patch.object(lm_loop, "_safe_launchctl", side_effect=safe),
            patch.object(lm_loop.time, "sleep"),
        ):
            results = apply_live(
                release, agents, self.root / "launchctl-safe",
                current=current, lock_path=self.root / "apply.lock",
                preserve_unloaded=True,
                event_writer=lambda *_: None,
            )

        retired = next(row for row in results if row.get("retired"))
        self.assertTrue(retired["was_loaded"])
        self.assertFalse(retired_plist.exists())
        self.assertEqual(post_bootout_prints, 2)

    def test_retirement_fails_closed_when_presence_probe_is_not_an_absence(self):
        release = self._release("release-a").resolve()
        value = json.loads((release / "config/loop-registry.json").read_text())
        value["retired_labels"] = ["ai.anicca.retired-example"]
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        agents = self.root / "LaunchAgents"
        agents.mkdir()
        retired_plist = agents / "ai.anicca.retired-example.plist"
        retired_plist.write_text("old")
        with (
            patch.object(lm_loop, "_safe_launchctl", side_effect=[
                (0, "ok"), (78, "invalid Aqua bootstrap"),
            ]),
            self.assertRaisesRegex(RuntimeError, "presence readback failed"),
        ):
            apply_live(
                release, agents, self.root / "launchctl-safe",
                current=release, lock_path=self.root / "apply.lock",
            )
        self.assertTrue(retired_plist.exists())

    def test_targeted_apply_never_processes_retired_labels(self):
        release = self._release("release-a").resolve()
        value = json.loads((release / "config/loop-registry.json").read_text())
        value["retired_labels"] = ["ai.anicca.retired-example"]
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        agents = self.root / "LaunchAgents"
        agents.mkdir()
        expected = [str(release / "bin/lm-loop-run"), "example", str(release)]
        launchctl_safe, calls = self._launchctl_recorder(expected)
        apply_live(
            release, agents, launchctl_safe, target="example",
            current=release, lock_path=self.root / "apply.lock",
            event_writer=lambda *_: None,
        )
        self.assertNotIn("ai.anicca.retired-example", calls.read_text())

    def test_targeted_retirement_removes_only_the_named_retired_label(self):
        release = self._release("release-a").resolve()
        value = json.loads((release / "config/loop-registry.json").read_text())
        value["retired_labels"] = [
            "ai.anicca.retired-example", "ai.anicca.retired-other",
        ]
        (release / "config/loop-registry.json").write_text(json.dumps(value))
        agents = self.root / "LaunchAgents"
        agents.mkdir()
        selected = agents / "ai.anicca.retired-example.plist"
        other = agents / "ai.anicca.retired-other.plist"
        selected.write_text("old")
        other.write_text("old")
        service = f"gui/{os.getuid()}/ai.anicca.retired-example"

        def safe(_executable, args):
            if args == ["preflight"]:
                return 0, "ok"
            if args == ["print", service]:
                return 1, "Could not find service"
            raise AssertionError(args)

        with patch.object(lm_loop, "_safe_launchctl", side_effect=safe):
            result = apply_live(
                release, agents, self.root / "launchctl-safe",
                target="ai.anicca.retired-example", current=release,
                lock_path=self.root / "apply.lock", event_writer=lambda *_: None,
            )

        self.assertEqual(result, [{
            "ok": True, "label": "ai.anicca.retired-example", "retired": True,
            "was_loaded": False, "removed_plist": True,
        }])
        self.assertFalse(selected.exists())
        self.assertTrue(other.exists())

    def test_reapply_same_release_drops_undeclared_working_directory_then_is_noop(self):
        release = self._release("release-a").resolve()
        current = self.root / "current"
        current.symlink_to(release)
        expected_arguments = [str(release / "bin/lm-loop-run"), "example", str(release)]
        values = self._apply_kwargs(
            current,
            self.root / "apply.lock",
            expected_arguments,
        )
        target = values["agents_dir"] / "ai.anicca.example.plist"
        target.write_bytes(plistlib.dumps({
            "Label": "ai.anicca.example",
            "ProgramArguments": ["/old/run.sh"],
            "EnvironmentVariables": {"CUSTOM": "kept"},
            "WorkingDirectory": "/var/tmp/example",
        }, fmt=plistlib.FMT_XML, sort_keys=True))
        (self.root / "launchctl.state").touch()
        events = []

        first = apply_live(
            release,
            values["agents_dir"],
            values["launchctl_safe"],
            current=current,
            lock_path=values["lock_path"],
            event_writer=lambda path, event: events.append((path, event)),
        )
        self.assertTrue(first[0]["changed"])
        installed = plistlib.loads(target.read_bytes())
        self.assertEqual(installed["EnvironmentVariables"]["CUSTOM"], "kept")
        self.assertNotIn("WorkingDirectory", installed)
        self.assertEqual(installed["ProgramArguments"], expected_arguments)
        self.assertTrue((self.root / "launchctl.state").is_file())
        self.assertIn(
            f"bootstrap gui/{os.getuid()} {target}",
            values["calls"].read_text().splitlines(),
        )
        values["calls"].write_text("")

        second = apply_live(
            release,
            values["agents_dir"],
            values["launchctl_safe"],
            current=current,
            lock_path=values["lock_path"],
            event_writer=lambda path, event: events.append((path, event)),
        )

        self.assertFalse(second[0]["changed"])
        self.assertEqual(values["calls"].read_text().splitlines(), [
            "preflight",
            f"print gui/{os.getuid()}/ai.anicca.example",
        ])
        installed = plistlib.loads(target.read_bytes())
        self.assertEqual(installed["EnvironmentVariables"]["CUSTOM"], "kept")
        self.assertNotIn("WorkingDirectory", installed)

    def test_equal_effective_plist_still_installs_when_service_is_unloaded(self):
        release = self._release("release-a").resolve()
        current = self.root / "current"
        current.symlink_to(release)
        expected_arguments = [str(release / "bin/lm-loop-run"), "example", str(release)]
        values = self._apply_kwargs(
            current,
            self.root / "apply.lock",
            expected_arguments,
        )
        rendered = build_apply_plan(registry(), release, SHA)[0]
        target = values["agents_dir"] / "ai.anicca.example.plist"
        installed = plistlib.loads(rendered["plist_bytes"])
        installed["EnvironmentVariables"]["CUSTOM"] = "kept"
        target.write_bytes(plistlib.dumps(installed, fmt=plistlib.FMT_XML, sort_keys=True))
        existing_bytes = target.read_bytes()
        self.assertEqual(
            existing_bytes,
            lm_loop._preserve_operational_attributes(rendered["plist_bytes"], existing_bytes),
        )
        events = []

        result = apply_live(
            release,
            values["agents_dir"],
            values["launchctl_safe"],
            current=current,
            lock_path=values["lock_path"],
            event_writer=lambda path, event: events.append((path, event)),
        )

        self.assertTrue(result[0]["changed"])
        calls = values["calls"].read_text().splitlines()
        self.assertIn(f"bootout gui/{os.getuid()}/ai.anicca.example", calls)
        self.assertIn(f"bootstrap gui/{os.getuid()} {target}", calls)
        installed = plistlib.loads(target.read_bytes())
        self.assertEqual(installed["EnvironmentVariables"]["CUSTOM"], "kept")
        self.assertNotIn("WorkingDirectory", installed)

    def test_cfo_target_retires_only_obsolete_cfo_environment(self):
        release = self._release("release-cfo").resolve()
        registry_value = registry()
        entry = registry_value["loops"].pop("example")
        entry["label"] = "ai.anicca.life-manager-cfo-hourly"
        registry_value["loops"]["life-manager-cfo-hourly"] = entry
        (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
        current = self.root / "current-cfo"
        current.symlink_to(release)
        expected_arguments = [
            str(release / "bin/lm-loop-run"), "life-manager-cfo-hourly", str(release),
        ]
        values = self._apply_kwargs(
            current, self.root / "apply-cfo.lock", expected_arguments,
            label="ai.anicca.life-manager-cfo-hourly",
        )
        rendered = build_apply_plan(registry_value, release, SHA)[0]
        target = values["agents_dir"] / "ai.anicca.life-manager-cfo-hourly.plist"
        installed = plistlib.loads(rendered["plist_bytes"])
        installed["EnvironmentVariables"].update({
            "LIFE_MANAGER_APP_DIR": "/obsolete/app",
            "CFO_STATE_DIR": "/obsolete/state",
            "TELEGRAM_ALERT_CHAT_ID": "kept",
        })
        installed["WorkingDirectory"] = "/obsolete/partial-release"
        target.write_bytes(plistlib.dumps(installed, fmt=plistlib.FMT_XML, sort_keys=True))

        result = apply_live(
            release, values["agents_dir"], values["launchctl_safe"],
            target="life-manager-cfo-hourly", current=current,
            lock_path=values["lock_path"], event_writer=lambda *_: None,
        )

        self.assertTrue(result[0]["changed"])
        environment = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
        self.assertNotIn("LIFE_MANAGER_APP_DIR", environment)
        self.assertNotIn("CFO_STATE_DIR", environment)
        self.assertEqual(environment["TELEGRAM_ALERT_CHAT_ID"], "kept")
        self.assertEqual(
            environment["LIFE_MANAGER_ENV_FILE"],
            str(Path.home() / ".local/state/life-manager/.env"),
        )
        self.assertNotIn("WorkingDirectory", plistlib.loads(target.read_bytes()))

    def test_selfbuild_target_retires_only_legacy_source_override(self):
        loop_id = "life-manager-selfbuild"
        release = self._release("release-selfbuild").resolve()
        registry_value = registry()
        entry = registry_value["loops"].pop("example")
        entry["label"] = "ai.anicca.life-manager-selfbuild"
        registry_value["loops"][loop_id] = entry
        (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
        current = self.root / "current-selfbuild"
        current.symlink_to(release)
        values = self._apply_kwargs(
            current,
            self.root / "apply-selfbuild.lock",
            [str(release / "bin/lm-loop-run"), loop_id, str(release)],
            label="ai.anicca.life-manager-selfbuild",
            agents_dir_name="LaunchAgents-selfbuild",
        )
        rendered = build_apply_plan(registry_value, release, SHA)[0]
        target = values["agents_dir"] / "ai.anicca.life-manager-selfbuild.plist"
        installed = plistlib.loads(rendered["plist_bytes"])
        installed["EnvironmentVariables"].update({
            "LM_SELFBUILD_REPO": "/obsolete/private-checkout",
            "LM_SELFBUILD_TELEGRAM_TARGET": "kept",
        })
        target.write_bytes(plistlib.dumps(
            installed, fmt=plistlib.FMT_XML, sort_keys=True))

        result = apply_live(
            release, values["agents_dir"], values["launchctl_safe"],
            target=loop_id, current=current, lock_path=values["lock_path"],
            event_writer=lambda *_: None,
        )

        self.assertTrue(result[0]["changed"])
        environment = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
        self.assertNotIn("LM_SELFBUILD_REPO", environment)
        self.assertEqual(environment["LM_SELFBUILD_TELEGRAM_TARGET"], "kept")

    def test_franklin_target_retires_external_runtime_overrides(self):
        loop_id = "franklin-loop"
        release = self._release("release-franklin").resolve()
        entrypoint = release / "runtime/anicca-daemon.sh"
        entrypoint.parent.mkdir(parents=True, exist_ok=True)
        entrypoint.write_text("#!/bin/sh\nexit 0\n")
        entrypoint.chmod(0o755)
        registry_value = registry("runtime/anicca-daemon.sh")
        entry = registry_value["loops"].pop("example")
        entry.update({"label": "ai.anicca.franklin-loop", "state_root": "~/.blockrun"})
        registry_value["loops"][loop_id] = entry
        (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
        current = self.root / "current-franklin"
        current.symlink_to(release)
        values = self._apply_kwargs(
            current,
            self.root / "apply-franklin.lock",
            [str(release / "bin/lm-loop-run"), loop_id, str(release)],
            label="ai.anicca.franklin-loop",
            agents_dir_name="LaunchAgents-franklin",
        )
        rendered = build_apply_plan(registry_value, release, SHA)[0]
        target = values["agents_dir"] / "ai.anicca.franklin-loop.plist"
        installed = plistlib.loads(rendered["plist_bytes"])
        installed["EnvironmentVariables"].update({
            "ANICCA_REPO": "/legacy/private-checkout",
            "ANICCA_STATE_DIR": "/legacy/hermes/state",
            "FRANKLIN_PROXY_PORT": "8402",
            "OPENCLAW_ENV_FILE": "/legacy/openclaw/.env",
            "ALWAYS_ACT_ENABLED": "1",
        })
        target.write_bytes(plistlib.dumps(
            installed, fmt=plistlib.FMT_XML, sort_keys=True))

        result = apply_live(
            release, values["agents_dir"], values["launchctl_safe"],
            target=loop_id, current=current, lock_path=values["lock_path"],
            event_writer=lambda *_: None,
        )

        self.assertTrue(result[0]["changed"])
        environment = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
        self.assertEqual(environment["ANICCA_REPO"], str(release))
        self.assertEqual(environment["ANICCA_INSTANCE"], "franklin")
        self.assertEqual(environment["ANICCA_HOME"], os.path.expanduser("~/.blockrun"))
        self.assertNotIn("ANICCA_STATE_DIR", environment)
        self.assertNotIn("FRANKLIN_PROXY_PORT", environment)
        self.assertNotIn("OPENCLAW_ENV_FILE", environment)
        self.assertEqual(environment["ALWAYS_ACT_ENABLED"], "1")

    def test_agentmail_targets_retire_only_legacy_state_environment(self):
        retired = {
            "AGENTMAIL_QUEUE_PATH",
            "AGENTMAIL_DB_PATH",
            "AGENTMAIL_ADAPTER_STATE_DIR",
            "AGENTMAIL_SEMANTIC_STATE_DIR",
        }
        for loop_id in ("agentmail-webhook", "agentmail-replier", "agentmail-nudge"):
            with self.subTest(loop_id=loop_id):
                release = self._release(f"release-{loop_id}").resolve()
                registry_value = registry()
                entry = registry_value["loops"].pop("example")
                entry["label"] = f"ai.anicca.{loop_id}"
                registry_value["loops"][loop_id] = entry
                (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
                current = self.root / f"current-{loop_id}"
                current.symlink_to(release)
                expected_arguments = [
                    str(release / "bin/lm-loop-run"), loop_id, str(release),
                ]
                values = self._apply_kwargs(
                    current, self.root / f"apply-{loop_id}.lock", expected_arguments,
                    label=f"ai.anicca.{loop_id}",
                    agents_dir_name=f"LaunchAgents-{loop_id}",
                )
                rendered = build_apply_plan(registry_value, release, SHA)[0]
                target = values["agents_dir"] / f"ai.anicca.{loop_id}.plist"
                installed = plistlib.loads(rendered["plist_bytes"])
                installed["EnvironmentVariables"].update({
                    key: f"/legacy/openclaw/{key.lower()}" for key in retired
                })
                installed["EnvironmentVariables"]["AGENTMAIL_WEBHOOK_PORT"] = "8810"
                target.write_bytes(plistlib.dumps(
                    installed, fmt=plistlib.FMT_XML, sort_keys=True))

                result = apply_live(
                    release, values["agents_dir"], values["launchctl_safe"],
                    target=loop_id, current=current, lock_path=values["lock_path"],
                    event_writer=lambda *_: None,
                )

                self.assertTrue(result[0]["changed"])
                environment = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
                self.assertTrue(retired.isdisjoint(environment))
                self.assertEqual(environment["AGENTMAIL_WEBHOOK_PORT"], "8810")

    def test_agent_economy_apply_retires_legacy_cron_environment(self):
        loop_id = "agent-economy-loop"
        release = self._release("release-agent-economy").resolve()
        registry_value = registry()
        entry = registry_value["loops"].pop("example")
        entry["label"] = "ai.anicca.agent-economy-loop"
        registry_value["loops"][loop_id] = entry
        (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
        current = self.root / "current-agent-economy"
        current.symlink_to(release)
        values = self._apply_kwargs(
            current,
            self.root / "apply-agent-economy.lock",
            [str(release / "bin/lm-loop-run"), loop_id, str(release)],
            label="ai.anicca.agent-economy-loop",
            agents_dir_name="LaunchAgents-agent-economy",
        )
        rendered = build_apply_plan(registry_value, release, SHA)[0]
        target = values["agents_dir"] / "ai.anicca.agent-economy-loop.plist"
        installed = plistlib.loads(rendered["plist_bytes"])
        installed["EnvironmentVariables"].update({
            "ANICCA_ECONOMY_CREATE_EVM_WALLET": "1",
            "ANICCA_RELEASE_ID": "legacy-release",
            "ANICCA_RELEASE_SHA": "b" * 40,
            "CEO_EFFECTIVE_CRON_DIR": "/legacy/cron",
        })
        installed["EnvironmentVariables"]["AGENT_ECONOMY_OPERATIONAL_SETTING"] = "kept"
        target.write_bytes(plistlib.dumps(
            installed, fmt=plistlib.FMT_XML, sort_keys=True))

        result = apply_live(
            release, values["agents_dir"], values["launchctl_safe"],
            target=loop_id, current=current, lock_path=values["lock_path"],
            event_writer=lambda *_: None,
        )

        self.assertTrue(result[0]["changed"])
        environment = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
        self.assertTrue({
            "ANICCA_ECONOMY_CREATE_EVM_WALLET", "ANICCA_RELEASE_ID",
            "ANICCA_RELEASE_SHA", "CEO_EFFECTIVE_CRON_DIR",
        }.isdisjoint(environment))
        self.assertEqual(environment["AGENT_ECONOMY_OPERATIONAL_SETTING"], "kept")

    def test_writer_targets_retire_only_legacy_log_environment(self):
        retired = {"ARTICLE_DAILY_LOG", "ARTICLE_MODEL_LOG", "GIG_LOG_DIR"}
        loop_ids = (
            "article-audit-7day", "article-daily", "article-healthcheck",
            "article-learn-whitelist", "article-resume", "article-self-improve",
            "article-zenn-retry", "writer-claim-loop", "writer-craft-train",
            "writer-money-sync", "writer-opportunity-discovery",
            "writer-opportunity-response", "writer-report", "writer-sales-measure",
        )
        for loop_id in loop_ids:
            with self.subTest(loop_id=loop_id):
                release = self._release(f"release-{loop_id}").resolve()
                registry_value = registry()
                entry = registry_value["loops"].pop("example")
                entry["label"] = f"ai.anicca.{loop_id}"
                registry_value["loops"][loop_id] = entry
                (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
                current = self.root / f"current-{loop_id}"
                current.symlink_to(release)
                values = self._apply_kwargs(
                    current,
                    self.root / f"apply-{loop_id}.lock",
                    [str(release / "bin/lm-loop-run"), loop_id, str(release)],
                    label=f"ai.anicca.{loop_id}",
                    agents_dir_name=f"LaunchAgents-{loop_id}",
                )
                rendered = build_apply_plan(registry_value, release, SHA)[0]
                target = values["agents_dir"] / f"ai.anicca.{loop_id}.plist"
                installed = plistlib.loads(rendered["plist_bytes"])
                installed["EnvironmentVariables"].update({
                    key: f"/legacy/openclaw/{key.lower()}" for key in retired
                })
                installed["EnvironmentVariables"]["WRITER_CUSTOM_OPERATIONAL_SETTING"] = "kept"
                target.write_bytes(plistlib.dumps(
                    installed, fmt=plistlib.FMT_XML, sort_keys=True))

                result = apply_live(
                    release, values["agents_dir"], values["launchctl_safe"],
                    target=loop_id, current=current, lock_path=values["lock_path"],
                    event_writer=lambda *_: None,
                )

                self.assertTrue(result[0]["changed"])
                environment = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
                self.assertTrue(retired.isdisjoint(environment))
                self.assertEqual(environment["WRITER_CUSTOM_OPERATIONAL_SETTING"], "kept")

    def test_polymarket_target_retires_legacy_home_and_signer_environment(self):
        release = self._release("release-pm-live").resolve()
        registry_value = registry()
        entry = registry_value["loops"].pop("example")
        entry["label"] = "ai.anicca.pm-live-trade"
        registry_value["loops"]["pm-live-trade"] = entry
        (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
        current = self.root / "current-pm-live"
        current.symlink_to(release)
        expected_arguments = [
            str(release / "bin/lm-loop-run"), "pm-live-trade", str(release),
        ]
        values = self._apply_kwargs(
            current, self.root / "apply-pm-live.lock", expected_arguments,
            label="ai.anicca.pm-live-trade",
        )
        rendered = build_apply_plan(registry_value, release, SHA)[0]
        target = values["agents_dir"] / "ai.anicca.pm-live-trade.plist"
        installed = plistlib.loads(rendered["plist_bytes"])
        installed["EnvironmentVariables"].update({
            "ANICCA_HOME": "/legacy/.anicca-founder",
            "PM_TRADE_AGENT_HOME": "/legacy/polymarket-agent",
            "PKVAR": "BORROWED_KEY",
            "BORROWED_KEY": "kept-but-unused",
            "BASE_CHAIN_WALLET_KEY": "retired-base-signer",
            "POLYGON_WALLET_PRIVATE_KEY": "retired-signer",
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        })
        target.write_bytes(plistlib.dumps(installed, fmt=plistlib.FMT_XML, sort_keys=True))

        result = apply_live(
            release, values["agents_dir"], values["launchctl_safe"],
            target="pm-live-trade", current=current,
            lock_path=values["lock_path"], event_writer=lambda *_: None,
        )

        self.assertTrue(result[0]["changed"])
        environment = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
        for key in (
            "ANICCA_HOME", "PM_TRADE_AGENT_HOME", "PKVAR",
            "ANICCA_EVM_PRIVATE_KEY", "BASE_CHAIN_WALLET_KEY", "BLOCKRUN_WALLET_KEY",
            "POLYGON_WALLET_PRIVATE_KEY",
        ):
            self.assertNotIn(key, environment)
        self.assertEqual(environment["BORROWED_KEY"], "kept-but-unused")
        self.assertEqual(environment["PATH"], "/opt/homebrew/bin:/usr/bin:/bin")
        self.assertEqual(environment["LIFE_MANAGER_NODE"], shutil.which("node"))

    def test_gig_apply_direct_target_retires_stale_disk_headroom_kib(self):
        # hf-gig-apply-direct's plist was installed while it was still rendered from
        # skills/earn/gig/config/launchd-jobs.json's legacy manifest, which explicitly set
        # GIG_DISK_HEADROOM_KIB="0" for this lane. Now that it is an lm-loop registry loop,
        # build_apply_plan's _plist() never mentions this key, so without retiring it the stale
        # "0" would be preserved forever across every future merge, release and label repoint.
        release = self._release("release-gig-apply").resolve()
        registry_value = registry()
        entry = registry_value["loops"].pop("example")
        entry["label"] = "ai.anicca.hf-gig-apply-direct"
        registry_value["loops"]["hf-gig-apply-direct"] = entry
        (release / "config/loop-registry.json").write_text(json.dumps(registry_value))
        current = self.root / "current-gig-apply"
        current.symlink_to(release)
        expected_arguments = [
            str(release / "bin/lm-loop-run"), "hf-gig-apply-direct", str(release),
        ]
        values = self._apply_kwargs(
            current, self.root / "apply-gig-apply.lock", expected_arguments,
            label="ai.anicca.hf-gig-apply-direct",
        )
        rendered = build_apply_plan(registry_value, release, SHA)[0]
        target = values["agents_dir"] / "ai.anicca.hf-gig-apply-direct.plist"
        installed = plistlib.loads(rendered["plist_bytes"])
        installed["EnvironmentVariables"].update({
            "GIG_DISK_HEADROOM_KIB": "0",
            "GIG_OPERATOR_BRAKE_FILE": "kept",
        })
        target.write_bytes(plistlib.dumps(installed, fmt=plistlib.FMT_XML, sort_keys=True))

        result = apply_live(
            release, values["agents_dir"], values["launchctl_safe"],
            target="hf-gig-apply-direct", current=current,
            lock_path=values["lock_path"], event_writer=lambda *_: None,
        )

        self.assertTrue(result[0]["changed"])
        environment = plistlib.loads(target.read_bytes())["EnvironmentVariables"]
        self.assertNotIn("GIG_DISK_HEADROOM_KIB", environment)
        self.assertEqual(environment["GIG_OPERATOR_BRAKE_FILE"], "kept")

    def test_launchctl_recorder_rejects_wrong_service(self):
        launchctl_safe, _ = self._launchctl_recorder(["/release/bin/lm-loop-run", "example", "/release"])

        result = subprocess.run(
            [str(launchctl_safe), "print", f"gui/{os.getuid()}/ai.anicca.wrong"],
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)

    def test_activate_current_rejects_busy_owner_without_current_swap(self):
        release_a = self._release("release-a").resolve()
        release_b = self._release("release-b").resolve()
        current = self.root / "current"
        current.symlink_to(release_a)
        lock_path = self.root / "apply.lock"

        with lock_path.open("a+") as owner_lock:
            fcntl.flock(owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "production apply is already owned"):
                lm_loop.activate_current(current, release_b, lock_path)

        self.assertEqual(current.resolve(), release_a)
        self.assertFalse((self.root / "current.swap").exists())


if __name__ == "__main__":
    unittest.main()
