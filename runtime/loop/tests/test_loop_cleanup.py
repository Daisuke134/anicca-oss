import json
import fcntl
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from runtime.loop.loop_cleanup import cleanup_run_root, gc_releases
from runtime.loop.lm_loop_run import prepare_loop_run
from runtime.loop.runtime_event import validate_runtime_event
from runtime.loop.central_cleanup import loaded_release_roots, open_release_roots, release_gc
from runtime.loop.central_cleanup import host_cleanup_command, host_cleanup_ok


def completed(root: Path, name: str, size: int = 1) -> Path:
    run = root / "runs" / name; run.mkdir(parents=True)
    (run / ".lm-regenerable").write_text("1\n")
    (run / "summary.json").write_text("{}\n")
    (run / "payload.bin").write_bytes(b"x" * size)
    return run


class LoopCleanupTest(unittest.TestCase):
    def test_host_cleanup_uses_durable_shared_pressure_state(self):
        command = host_cleanup_command(Path('/release'), Path('/home'))
        self.assertEqual(command[-4:], [
            '--home', '/home', '--state-dir', '/home/.openclaw/state',
        ])

    def test_host_cleanup_error_cannot_be_reported_as_success(self):
        self.assertFalse(host_cleanup_ok(0, {"errors": 1, "protected_deletions": 0}))
        self.assertFalse(host_cleanup_ok(0, {"errors": 0, "protected_deletions": 1}))
        self.assertTrue(host_cleanup_ok(0, {"errors": 0, "protected_deletions": 0}))
    def test_loop_cleanup_preserves_active_unmarked_and_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); old = completed(root, "old"); active = completed(root, "active")
            newest = completed(root, "newest")
            unmarked = root / "runs/unmarked"; unmarked.mkdir(); (unmarked / "data").write_text("keep")
            receipt = root / "receipts"; receipt.mkdir(); (receipt / "official.json").write_text("keep")
            for index, path in enumerate((old, active, newest), 1): os.utime(path, (index, index))
            result = cleanup_run_root(root, {"max_runs": 1, "max_age_days": 365}, {"active"}, now=4)
            self.assertFalse(old.exists())
            self.assertTrue(active.exists())
            self.assertTrue(newest.exists())
            self.assertTrue(unmarked.exists())
            self.assertTrue(receipt.exists())
            self.assertEqual(result["protected_deletions"], 0)

    def test_pressure_cleanup_reclaims_completed_bytes_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); completed(root, "old", 1024 * 1024)
            result = cleanup_run_root(root, {"max_runs": 0, "max_age_days": 1}, set(), now=time.time() + 172800)
            self.assertGreaterEqual(result["reclaimed_bytes"], 1024 * 1024)
            self.assertEqual(result["removed_runs"], 1)

    def test_release_gc_preserves_current_and_explicit_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            releases = Path(directory) / "releases"; releases.mkdir()
            paths=[]
            for index in range(4):
                path=releases/f"2026010{index}T000000-{'a'*7}{index}"; path.mkdir()
                (path/"RELEASE.json").write_text(json.dumps({"sha": f"{index:040x}"}))
                os.utime(path,(index,index)); paths.append(path)
            current=Path(directory)/"current"; current.symlink_to(paths[1])
            result=gc_releases(releases,current,keep=1,protected={paths[2].resolve()})
            self.assertTrue(paths[1].exists()); self.assertTrue(paths[2].exists()); self.assertTrue(paths[3].exists())
            self.assertFalse(paths[0].exists())
            self.assertEqual(result["protected_deletions"],0)

    def test_release_gc_removes_selected_read_only_release(self):
        with tempfile.TemporaryDirectory() as directory:
            releases = Path(directory) / "releases"; releases.mkdir()
            stale = releases / ("20260101T000000-" + "a" * 8); stale.mkdir()
            (stale / "RELEASE.json").write_text(json.dumps({"sha": "a" * 40}))
            nested = stale / "nested"; nested.mkdir(); (nested / "code.py").write_text("x")
            for path in (nested / "code.py", stale / "RELEASE.json"):
                path.chmod(0o444)
            nested.chmod(0o555); stale.chmod(0o555)
            current = Path(directory) / "current"

            result = gc_releases(releases, current, keep=0, protected=set())

            self.assertFalse(stale.exists())
            self.assertEqual(result["errors"], 0)

    def test_loop_run_cleans_only_its_root_then_returns_exact_release_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); entry=root/'bin/job.sh'; entry.parent.mkdir(); entry.write_text('#!/bin/sh\n'); entry.chmod(0o755)
            state=root/'home/state'; completed(state,'old',128)
            registry={"schema_version":2,"loops":{"job":{
                "label":"ai.anicca.job","domain":"system","entrypoint":"bin/job.sh",
                "cadence":{"run_at_load":True},"effect_class":"none",
                "state_root":"~/state","log_root":"~/state/logs",
                "cleanup":{"max_runs":1,"max_age_days":1},"provider_route":"deterministic"}}}
            with mock.patch.dict(os.environ,{"HOME":str(root/'home')}):
                argv,receipt=prepare_loop_run(registry,"job",root,active_run_ids=set(),now=time.time()+172800)
            self.assertEqual(argv,[str(entry.resolve())])
            self.assertFalse((state/'runs/old').exists())
            self.assertGreaterEqual(receipt['reclaimed_bytes'],128)

    def test_loop_run_cleanup_uses_installed_runtime_root_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = root / 'bin/job.sh'
            entry.parent.mkdir()
            entry.write_text('#!/bin/sh\n')
            entry.chmod(0o755)
            home = root / 'home'
            default_state = home / 'default-state'
            custom_state = root / 'custom-state'
            custom_log = root / 'custom-log'
            completed(default_state, 'must-stay', 16)
            completed(custom_state, 'remove', 16)
            registry = {"schema_version": 2, "loops": {"job": {
                "label": "ai.anicca.job", "domain": "system", "entrypoint": "bin/job.sh",
                "cadence": {"run_at_load": True}, "effect_class": "none",
                "state_root": "~/default-state", "log_root": "~/default-state/logs",
                "cleanup": {"max_runs": 1, "max_age_days": 1},
                "provider_route": "deterministic"}}}
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                prepare_loop_run(
                    registry, "job", root, active_run_ids=set(), now=time.time() + 172800,
                    state_root=str(custom_state), log_root=str(custom_log))
            self.assertTrue((default_state / 'runs/must-stay').exists())
            self.assertFalse((custom_state / 'runs/remove').exists())

    def test_loop_run_preserves_python_adapter_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entrypoint = root / 'scheduled_runner.py'
            entrypoint.write_text('#!/usr/bin/env python3\n')
            entrypoint.chmod(0o755)
            registry = {"schema_version": 2, "loops": {"job": {
                "label": "ai.anicca.job", "domain": "growth",
                "entrypoint": "scheduled_runner.py", "adapter": "python",
                "command": ["dashboard"], "cadence": {"run_at_load": True},
                "effect_class": "none", "state_root": "~/state",
                "log_root": "~/state/logs",
                "cleanup": {"max_runs": 1, "max_age_days": 1},
                "provider_route": "deterministic"}}}
            with mock.patch.dict(os.environ, {"HOME": str(root / 'home')}):
                argv, _ = prepare_loop_run(
                    registry, "job", root, active_run_ids=set())
            self.assertEqual(argv, [sys.executable, str(entrypoint.resolve()), "dashboard"])

    def test_loop_run_preserves_empty_python_adapter_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entrypoint = root / 'application_owner.py'
            entrypoint.write_text('#!/usr/bin/env python3\n')
            entrypoint.chmod(0o755)
            registry = {"schema_version": 2, "loops": {"job": {
                "label": "ai.anicca.job", "domain": "earn",
                "entrypoint": "application_owner.py", "adapter": "python",
                "command": [], "cadence": {"run_at_load": True},
                "effect_class": "application", "state_root": "~/state",
                "log_root": "~/state/logs",
                "cleanup": {"max_runs": 1, "max_age_days": 1},
                "provider_route": "deterministic"}}}
            with mock.patch.dict(os.environ, {"HOME": str(root / 'home')}):
                argv, _ = prepare_loop_run(
                    registry, "job", root, active_run_ids=set())
            self.assertEqual(argv, [sys.executable, str(entrypoint.resolve())])

    def test_loop_run_preserves_exec_adapter_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entrypoint = root / 'affiliate'
            entrypoint.write_text('#!/bin/sh\n')
            entrypoint.chmod(0o755)
            registry = {"schema_version": 2, "loops": {"job": {
                "label": "ai.anicca.job", "domain": "growth",
                "entrypoint": "affiliate", "adapter": "exec",
                "command": ["sources", "wake"], "cadence": {"run_at_load": True},
                "effect_class": "none", "state_root": "~/state",
                "log_root": "~/state/logs",
                "cleanup": {"max_runs": 1, "max_age_days": 1},
                "provider_route": "deterministic"}}}
            with mock.patch.dict(os.environ, {"HOME": str(root / 'home')}):
                argv, _ = prepare_loop_run(
                    registry, "job", root, active_run_ids=set())
            self.assertEqual(argv, [str(entrypoint.resolve()), "sources", "wake"])

    def test_loaded_plist_release_is_discovered_as_protected(self):
        import plistlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); releases=root/'releases'; release=releases/('20260101T000000-'+'a'*8)
            entry=release/'bin/job.sh'; entry.parent.mkdir(parents=True); entry.write_text('x')
            agents=root/'agents'; agents.mkdir()
            (agents/'ai.anicca.job.plist').write_bytes(plistlib.dumps({
                'Label':'ai.anicca.job','ProgramArguments':[str(entry)]}))
            self.assertEqual(loaded_release_roots(agents,releases),{release.resolve()})

    def test_open_process_release_is_discovered_as_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            releases = Path(directory) / "releases"
            entry = releases / ("20260101T000000-" + "a" * 8) / "bin/job.sh"
            entry.parent.mkdir(parents=True)
            entry.write_text("x")

            completed = mock.Mock(returncode=0, stdout=f"p123\nn{entry}\n", stderr="")
            with mock.patch("runtime.loop.central_cleanup.subprocess.run", return_value=completed) as run:
                self.assertEqual(open_release_roots(releases), {entry.parents[1].resolve()})
        run.assert_called_once_with(
            ["ps", "-axo", "command="], capture_output=True, text=True, timeout=120)

    def _run_terminal_event(self, exit_code: int) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); home = root / "home"; home.mkdir()
            entry = root / "bin/job.sh"; entry.parent.mkdir()
            entry.write_text(f"#!/bin/sh\nexit {exit_code}\n"); entry.chmod(0o755)
            registry = {"schema_version": 2, "loops": {"job": {
                "label": "ai.anicca.job", "domain": "system", "entrypoint": "bin/job.sh",
                "cadence": {"run_at_load": True}, "effect_class": "none",
                "state_root": "~/state", "log_root": "~/state/logs",
                "cleanup": {"max_runs": 1, "max_age_days": 1},
                "provider_route": "deterministic"}}}
            (root / "config").mkdir(); (root / "config/loop-registry.json").write_text(json.dumps(registry))
            (root / "RELEASE.json").write_text(json.dumps({"sha": "a" * 40}))
            result = subprocess.run(
                [sys.executable, "-m", "runtime.loop.lm_loop_run", "job", str(root)],
                cwd=Path(__file__).parents[3], env={**os.environ, "HOME": str(home)}, check=False)
            self.assertEqual(result.returncode, exit_code)
            event = json.loads((home / "state/events.jsonl").read_text().splitlines()[-1])
            return validate_runtime_event(event)

    def test_loop_run_records_success_terminal_event(self):
        event = self._run_terminal_event(0)
        self.assertEqual((event["status"], event["release_sha"], event["provider"]),
                         ("pass", "a" * 40, "deterministic"))
        self.assertEqual(event["effect_status"], "not_applicable")
        self.assertTrue(event["evidence_refs"][0].startswith("lm-loop://job/"))

    def test_loop_run_passes_release_root_to_entrypoint_without_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); home = root / "home"; home.mkdir()
            entry = root / "bin/job.sh"; entry.parent.mkdir()
            observed = home / "release-root.txt"
            entry.write_text(
                f'#!/bin/sh\nprintf "%s\\n%s" "$LIFE_MANAGER_RELEASE_ROOT" '
                f'"$LIFE_MANAGER_REPO" > "{observed}"\n')
            entry.chmod(0o755)
            registry = {"schema_version": 2, "loops": {"job": {
                "label": "ai.anicca.job", "domain": "system", "entrypoint": "bin/job.sh",
                "cadence": {"run_at_load": True}, "effect_class": "none",
                "state_root": "~/state", "log_root": "~/state/logs",
                "cleanup": {"max_runs": 1, "max_age_days": 1},
                "provider_route": "deterministic"}}}
            (root / "config").mkdir()
            (root / "config/loop-registry.json").write_text(json.dumps(registry))
            (root / "RELEASE.json").write_text(json.dumps({"sha": "a" * 40}))
            environment = {
                **os.environ,
                "HOME": str(home),
                "LIFE_MANAGER_RELEASE_ROOT": "",
                "LIFE_MANAGER_REPO": "source-sentinel",
            }
            result = subprocess.run(
                [sys.executable, "-m", "runtime.loop.lm_loop_run", "job", str(root)],
                cwd=Path(__file__).parents[3], env=environment, check=False)

            self.assertEqual(result.returncode, 0)
            self.assertEqual(observed.read_text().splitlines(), [str(root.resolve()), "source-sentinel"])

    def test_loop_run_records_failed_terminal_event(self):
        event = self._run_terminal_event(7)
        self.assertEqual((event["status"], event["blocker"]),
                         ("fail", "entrypoint_exit_7"))

    def test_loop_run_terminates_entrypoint_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); home=root/'home'; home.mkdir()
            entry=root/'bin/job.sh'; entry.parent.mkdir()
            pid_file=home/'grandchild.pid'
            entry.write_text(f'#!/bin/sh\nsleep 60 &\necho $! > "{pid_file}"\nwait\n')
            entry.chmod(0o755)
            registry={"schema_version":2,"loops":{"job":{
                "label":"ai.anicca.job","domain":"system","entrypoint":"bin/job.sh",
                "cadence":{"keep_alive":True},"effect_class":"none",
                "state_root":"~/state","log_root":"~/state/logs",
                "cleanup":{"max_runs":1,"max_age_days":1},"provider_route":"deterministic"}}}
            (root/'config').mkdir();(root/'config/loop-registry.json').write_text(json.dumps(registry))
            (root/'RELEASE.json').write_text(json.dumps({'sha':'a'*40}))
            wrapper=subprocess.Popen(
                [sys.executable,'-m','runtime.loop.lm_loop_run','job',str(root)],
                cwd=Path(__file__).parents[3],env={**os.environ,'HOME':str(home)})
            for _ in range(50):
                if pid_file.exists():break
                time.sleep(0.02)
            grandchild=int(pid_file.read_text())
            start_event=json.loads((home/'state/events.jsonl').read_text().splitlines()[-1])
            self.assertEqual(start_event['status'],'running')
            wrapper.terminate();wrapper.wait(timeout=5);time.sleep(0.1)
            with self.assertRaises(ProcessLookupError):
                os.kill(grandchild,0)

    def test_continuous_loop_releases_apply_lock_after_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); home = root / "home"; home.mkdir()
            entry = root / "bin/job.sh"; entry.parent.mkdir()
            started = home / "started"
            entry.write_text(f'#!/bin/sh\ntouch "{started}"\nsleep 60\n')
            entry.chmod(0o755)
            registry = {"schema_version": 2, "loops": {"job": {
                "label": "ai.anicca.job", "domain": "system", "entrypoint": "bin/job.sh",
                "cadence": {"keep_alive": True}, "effect_class": "none",
                "state_root": "~/state", "log_root": "~/state/logs",
                "cleanup": {"max_runs": 1, "max_age_days": 1},
                "provider_route": "deterministic"}}}
            (root / "config").mkdir()
            (root / "config/loop-registry.json").write_text(json.dumps(registry))
            (root / "RELEASE.json").write_text(json.dumps({"sha": "a" * 40}))
            wrapper = subprocess.Popen(
                [sys.executable, "-m", "runtime.loop.lm_loop_run", "job", str(root)],
                cwd=Path(__file__).parents[3], env={**os.environ, "HOME": str(home)})
            try:
                for _ in range(100):
                    if started.exists():
                        break
                    time.sleep(0.02)
                self.assertTrue(started.exists())
                lock = home / "loops/.apply-locks/ai.anicca.job.lock"
                with lock.open("a+") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                wrapper.terminate()
                wrapper.wait(timeout=5)

    def test_release_gc_preserves_release_loaded_by_launchd(self):
        import plistlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); releases=root/'releases'; releases.mkdir()
            paths=[]
            for index in range(4):
                path=releases/f"2026010{index}T000000-{'a'*7}{index}"; path.mkdir()
                (path/'RELEASE.json').write_text(json.dumps({'sha':f'{index:040x}'}))
                os.utime(path,(index,index)); paths.append(path)
            entry=paths[0]/'bin/lm-loop-run'; entry.parent.mkdir(); entry.write_text('#!/bin/sh\n')
            current=root/'current'; current.symlink_to(paths[3])
            agents=root/'agents'; agents.mkdir()
            (agents/'ai.anicca.job.plist').write_bytes(plistlib.dumps({
                'Label':'ai.anicca.job',
                'ProgramArguments':[str(paths[0]/'bin/lm-loop-run')],
            }))
            result=release_gc(releases,current,agents,keep=1)
            self.assertTrue(paths[0].exists())
            self.assertFalse(paths[1].exists())
            self.assertTrue(paths[3].exists())
            self.assertEqual(result['protected_release_count'],1)

    def test_release_gc_preserves_release_used_by_open_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); releases = root / "releases"; releases.mkdir()
            paths = []
            for index in range(4):
                path = releases / f"2026010{index}T000000-{'a' * 7}{index}"
                path.mkdir(); (path / "RELEASE.json").write_text(json.dumps({"sha": f"{index:040x}"}))
                os.utime(path, (index, index)); paths.append(path)
            current = root / "current"; current.symlink_to(paths[3])
            agents = root / "agents"; agents.mkdir()
            with mock.patch(
                "runtime.loop.central_cleanup.open_release_roots",
                return_value={paths[0].resolve()},
            ):
                result = release_gc(releases, current, agents, keep=1)
            self.assertTrue(paths[0].exists())
            self.assertFalse(paths[1].exists())
            self.assertTrue(paths[2].exists())
            self.assertTrue(paths[3].exists())
            self.assertEqual(result["protected_release_count"], 1)


if __name__ == "__main__": unittest.main()
