import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lib.ceo_allocation import effective_registry, write_allocation


ROOT = Path(__file__).resolve().parents[3]


class CEOAllocationTest(unittest.TestCase):
    def test_overlay_preserves_repository_as_the_only_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            source_before = (ROOT / "config" / "loop-registry.json").read_bytes()
            allocation = {
                "status": "paused",
                "pass_frequency_multiplier": 1.0,
                "capital_cap_usd": None,
            }

            write_allocation(state, "ceo-runner", allocation)
            registry = effective_registry(ROOT, state)

            self.assertEqual(registry["loops"]["ceo-runner"]["allocation"], allocation)
            self.assertEqual((ROOT / "config" / "loop-registry.json").read_bytes(), source_before)
            self.assertFalse((state / "config" / "loop-registry.json").exists())

    def test_repository_refresh_drops_removed_loop_without_rewriting_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            state = root / "state"
            (source / "config").mkdir(parents=True)
            (source / "config" / "loop-registry.json").write_text(
                '{"loops":{"present":{"entrypoint":"new"}}}'
            )
            write_allocation(state, "removed", {"status": "paused"})

            registry = effective_registry(source, state)

            self.assertEqual(registry, {"loops": {"present": {"entrypoint": "new"}}})

    def test_parallel_writers_do_not_lose_other_loop_allocations(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            processes = []
            for index in range(20):
                script = (
                    "import sys; from lib.ceo_allocation import write_allocation; "
                    "write_allocation(sys.argv[1], sys.argv[2], {'status':'paused'})"
                )
                processes.append(subprocess.Popen(
                    [sys.executable, "-c", script, str(state), f"loop-{index}"],
                    cwd=ROOT,
                ))
            self.assertTrue(all(process.wait(timeout=20) == 0 for process in processes))
            overrides = json.loads(
                (state / "config" / "allocation-overrides.json").read_text()
            )["allocations"]
            self.assertEqual(set(overrides), {f"loop-{index}" for index in range(20)})

    def test_registry_enforcer_reads_the_overlay_from_external_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            state = root / "state"
            (source / "config").mkdir(parents=True)
            (source / "config" / "loop-registry.json").write_text(json.dumps({
                "loops": {"sample": {"cadence": {"start_interval_seconds": 60}}}
            }))
            (source / "config" / "ceo-budget-config.json").write_text("{}")
            (source / "config" / "ceo-unit-economics.json").write_text('{"mode":"observe_only"}')
            write_allocation(state, "sample", {
                "status": "paused", "pass_frequency_multiplier": 1.0, "capital_cap_usd": None,
            })
            command = (
                f"source '{ROOT / 'lib' / 'registry-enforce.sh'}'; "
                "registry_enforce_or_exit sample"
            )
            result = subprocess.run(
                ["bash", "-c", command],
                env={
                    **os.environ,
                    "CEO_CONFIG_ROOT": str(source),
                    "CEO_STATE_DIR": str(state),
                },
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("PAUSED by CEO decision", result.stderr)

    def test_installed_enforcer_ignores_an_injected_config_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            fake = root / "fake"
            (fake / "config").mkdir(parents=True)
            (fake / "config" / "loop-registry.json").write_text('{"loops":{}}')
            command = (
                f"source '{ROOT / 'lib' / 'registry-enforce.sh'}'; "
                "_registry_enforce_config_root"
            )
            result = subprocess.run(
                ["bash", "-c", command],
                env={
                    **os.environ,
                    "CEO_CONFIG_ROOT": str(fake),
                    "CEO_STATE_DIR": str(state),
                    "LIFE_MANAGER_RELEASE_SHA": "a" * 40,
                },
                capture_output=True,
                text=True,
                check=True,
            )

            self.assertEqual(result.stdout.strip(), str(ROOT))

    def test_installed_enforcer_ignores_an_inherited_ceo_state_dir(self):
        command = (
            f"source '{ROOT / 'lib' / 'registry-enforce.sh'}'; "
            "_registry_enforce_base"
        )
        result = subprocess.run(
            ["bash", "-c", command],
            env={
                **os.environ,
                "HOME": "/home/runtime-owner",
                "CEO_STATE_DIR": "/external/legacy-state",
                "LIFE_MANAGER_RELEASE_SHA": "a" * 40,
            },
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(
            result.stdout.strip(),
            "/home/runtime-owner/.local/state/life-manager/ceo-runner",
        )

    def test_ceo_wrapper_defaults_to_canonical_external_state(self):
        wrapper = (ROOT / "bin" / "ceo-run.sh").read_text()
        self.assertIn("$HOME/.local/state/life-manager/ceo-runner", wrapper)
        self.assertNotIn('BASE="${CEO_STATE_DIR:-$HERE}"', wrapper)

    def test_light_pass_writes_only_beneath_external_state(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            registry_before = (ROOT / "config" / "loop-registry.json").read_bytes()
            env = {**os.environ, "HOME": str(home), "CEO_CONFIG_ROOT": str(ROOT)}
            env.pop("CEO_STATE_DIR", None)
            env.pop("LIFE_MANAGER_CEO_STATE_ROOT", None)

            result = subprocess.run(
                ["bash", str(ROOT / "bin" / "ceo-run.sh"), "--light-pass"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

            state = home / ".local" / "state" / "life-manager" / "ceo-runner"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((state / "ledgers" / "ceo-unit-economics.latest.json").is_file())
            self.assertEqual((ROOT / "config" / "loop-registry.json").read_bytes(), registry_before)
            self.assertFalse((state / "config" / "loop-registry.json").exists())


if __name__ == "__main__":
    unittest.main()
