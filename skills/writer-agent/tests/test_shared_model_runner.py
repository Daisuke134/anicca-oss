import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "runtime/shared-model-runner.py"
LEGACY_ENTRY = ROOT / "runtime/model-runner.sh"


class SharedModelRunnerTest(unittest.TestCase):
    def test_judge_preserves_legacy_stdout_through_canonical_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / "agent-runner.py"
            fake.write_text("""#!/usr/bin/env python3
import json,pathlib,sys
a=sys.argv[1:]; e=pathlib.Path(a[a.index('--evidence-dir')+1]); e.mkdir(parents=True)
r=e/'result.json'; r.write_text('{"classification":"ACCEPTED"}')
(e/'summary.json').write_text(json.dumps({'result_path':str(r)}))
""")
            fake.chmod(0o755)
            prompt = root / "prompt.txt"; prompt.write_text("classify this message")
            env = {**os.environ, "AGENT_RUNNER_BIN": str(fake),
                   "WRITER_SHARED_RUNNER_STATE": str(root / "state")}
            result = subprocess.run(
                [sys.executable, str(ADAPTER), "judge", "--prompt-file", str(prompt)],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"classification": "ACCEPTED"})

    def test_adapter_contains_no_direct_provider_or_auth_selection(self):
        source = ADAPTER.read_text(encoding="utf-8")
        for forbidden in ("CODEX_HOME", "auth.json", "codex exec", "ARTICLE_PROVIDER"):
            self.assertNotIn(forbidden, source)

    def test_legacy_runner_has_no_host_fixed_provider_secret_path(self):
        source = LEGACY_ENTRY.read_text(encoding="utf-8")
        self.assertNotIn("/opt/homebrew/etc/cliproxyapi.conf", source)
        self.assertIn("ARTICLE_CODEX_PROVIDER_API_KEY", source)
        self.assertIn("ARTICLE_CLIPROXY_CONFIG", source)
        start = source.index("load_codex_provider_key")
        self.assertLess(source.index("set +x", start),
                        source.index('provider_key="${ARTICLE_CODEX_PROVIDER_API_KEY:-}"', start))

    def test_direct_provider_key_wins_and_never_appears_in_xtrace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.txt"
            prompt.write_text("portable provider probe")
            capture = root / "captured-key"
            fake = root / "codex"
            fake.write_text(
                '#!/usr/bin/env bash\nprintf %s "$CLIPROXY_API_KEY" > "$CAPTURE_KEY"\ncat >/dev/null\n'
            )
            fake.chmod(0o755)
            config = root / "cliproxy.conf"
            config.write_text('api-keys:\n  - "file-key-must-lose"\n')
            secret = "direct-key-must-not-appear-in-trace"
            env = {**os.environ,
                   "ARTICLE_PROVIDER": "codex",
                   "ARTICLE_CODEX_BIN": str(fake),
                   "ARTICLE_CODEX_PROVIDER_ID": "cliproxy",
                   "ARTICLE_CODEX_PROVIDER_BASE_URL": "http://127.0.0.1:8317/v1",
                   "ARTICLE_CODEX_PROVIDER_ENV_KEY": "CLIPROXY_API_KEY",
                   "ARTICLE_CODEX_PROVIDER_API_KEY_SOURCE": "cliproxyapi",
                   "ARTICLE_CODEX_PROVIDER_API_KEY": secret,
                   "ARTICLE_CLIPROXY_CONFIG": str(config),
                   "CAPTURE_KEY": str(capture)}
            result = subprocess.run(
                ["bash", "-x", str(LEGACY_ENTRY), "agent", "--prompt-file", str(prompt)],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(capture.read_text(), secret)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertNotIn("file-key-must-lose", capture.read_text())

    def test_explicit_provider_config_is_used_when_direct_key_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.txt"
            prompt.write_text("portable provider fallback")
            capture = root / "captured-key"
            fake = root / "codex"
            fake.write_text(
                '#!/usr/bin/env bash\nprintf %s "$CLIPROXY_API_KEY" > "$CAPTURE_KEY"\ncat >/dev/null\n'
            )
            fake.chmod(0o755)
            config = root / "cliproxy.conf"
            secret = "explicit-file-key-must-not-appear-in-trace"
            config.write_text(f'api-keys:\n  - "{secret}"\n')
            env = {**os.environ,
                   "ARTICLE_PROVIDER": "codex",
                   "ARTICLE_CODEX_BIN": str(fake),
                   "ARTICLE_CODEX_PROVIDER_ID": "cliproxy",
                   "ARTICLE_CODEX_PROVIDER_BASE_URL": "http://127.0.0.1:8317/v1",
                   "ARTICLE_CODEX_PROVIDER_ENV_KEY": "CLIPROXY_API_KEY",
                   "ARTICLE_CODEX_PROVIDER_API_KEY_SOURCE": "cliproxyapi",
                   "ARTICLE_CLIPROXY_CONFIG": str(config),
                   "CAPTURE_KEY": str(capture)}
            env.pop("ARTICLE_CODEX_PROVIDER_API_KEY", None)
            result = subprocess.run(
                ["bash", "-x", str(LEGACY_ENTRY), "agent", "--prompt-file", str(prompt)],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(capture.read_text(), secret)
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_nondefault_provider_fails_closed_without_any_key_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.txt"
            prompt.write_text("missing provider credential")
            called = root / "called"
            fake = root / "codex"
            fake.write_text('#!/usr/bin/env bash\ntouch "$CALLED"\n')
            fake.chmod(0o755)
            env = {**os.environ,
                   "ARTICLE_PROVIDER": "codex",
                   "ARTICLE_CODEX_BIN": str(fake),
                   "ARTICLE_CODEX_PROVIDER_ID": "cliproxy",
                   "ARTICLE_CODEX_PROVIDER_BASE_URL": "http://127.0.0.1:8317/v1",
                   "ARTICLE_CODEX_PROVIDER_ENV_KEY": "CLIPROXY_API_KEY",
                   "ARTICLE_CODEX_PROVIDER_API_KEY_SOURCE": "cliproxyapi",
                   "CALLED": str(called)}
            env.pop("ARTICLE_CODEX_PROVIDER_API_KEY", None)
            env.pop("ARTICLE_CLIPROXY_CONFIG", None)
            result = subprocess.run(
                [str(LEGACY_ENTRY), "agent", "--prompt-file", str(prompt)],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(called.exists())
            self.assertIn("invalid Codex provider configuration", result.stderr)

    def test_production_legacy_entry_delegates_to_canonical_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); fake = root / "agent-runner.py"
            fake.write_text("""#!/usr/bin/env python3
import json,pathlib,sys
a=sys.argv[1:]; e=pathlib.Path(a[a.index('--evidence-dir')+1]); e.mkdir(parents=True)
r=e/'result.json'; r.write_text('{"delegated":true}')
(e/'summary.json').write_text(json.dumps({'result_path':str(r)}))
"""); fake.chmod(0o755)
            prompt=root/'prompt.txt'; prompt.write_text('delegate this judge')
            env={key:value for key,value in os.environ.items()
                 if key not in {'ARTICLE_CODEX_BIN','ARTICLE_CLAUDE_BIN','ARTICLE_CODEX_EVENTS_FILE'}}
            env.update({'AGENT_RUNNER_BIN':str(fake),'WRITER_SHARED_RUNNER_STATE':str(root/'state')})
            result=subprocess.run([str(LEGACY_ENTRY),'judge','--prompt-file',str(prompt)],
                                  env=env,capture_output=True,text=True,check=False)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),{'delegated':True})

    def test_production_repair_delegates_cage_session_and_durable_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); workspace=root/'workspace'; workspace.mkdir()
            args_file=root/'args.json'; fake=root/'agent-runner.py'
            fake.write_text("""#!/usr/bin/env python3
import json,os,pathlib,sys
a=sys.argv[1:]; pathlib.Path(os.environ['CAPTURE_ARGS']).write_text(json.dumps(a))
e=pathlib.Path(a[a.index('--evidence-dir')+1]); e.mkdir(parents=True)
(e/'attempt-01.stdout.log').write_text('{"type":"thread.started","thread_id":"thread-123"}\\n')
r=e/'result.json'; r.write_text('{"complete":true}')
(e/'summary.json').write_text(json.dumps({'result_path':str(r)}))
"""); fake.chmod(0o755)
            prompt=root/'prompt.txt'; prompt.write_text('repair this workspace')
            schema=root/'schema.json'; schema.write_text('{}')
            events,last=root/'events.jsonl',root/'last.json'
            env={key:value for key,value in os.environ.items()
                 if key not in {'ARTICLE_CODEX_BIN','ARTICLE_CLAUDE_BIN'}}
            env.update({'AGENT_RUNNER_BIN':str(fake),'WRITER_SHARED_RUNNER_STATE':str(root/'state'),
                        'ARTICLE_PROVIDER':'codex',
                        'ARTICLE_REPAIR_WORKSPACE':str(workspace),'ARTICLE_CODEX_EVENTS_FILE':str(events),
                        'ARTICLE_CODEX_LAST_MESSAGE_FILE':str(last),'ARTICLE_CODEX_OUTPUT_SCHEMA':str(schema),
                        'ARTICLE_CODEX_RESUME_SESSION_ID':'thread-123','CAPTURE_ARGS':str(args_file)})
            result=subprocess.run([str(LEGACY_ENTRY),'repair','--prompt-file',str(prompt)],
                                  env=env,capture_output=True,text=True,check=False)
            self.assertEqual(result.returncode,0,result.stderr)
            args=json.loads(args_file.read_text())
            self.assertIn('writer-repair-agent',args)
            self.assertEqual(args[args.index('--codex-resume-session-id')+1],'thread-123')
            self.assertIn('thread.started',events.read_text())
            self.assertEqual(json.loads(last.read_text()),{'complete':True})


if __name__ == "__main__":
    unittest.main()
