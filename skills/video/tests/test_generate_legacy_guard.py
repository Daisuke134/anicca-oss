#!/usr/bin/env python3
"""Path-convention and legacy-state guard tests for daily-lm-video generate.py.

F1: argless defaults must resolve beneath <data root>/state/lm-video — the exact
paths skills/life-manager/life-manager-daily.sh exports (RUN_LEDGER/USAGE_LEDGER/
ROTATION_STATE live under $LM_DATA_ROOT/state/lm-video).

F2: when the new-root state/input is absent but the legacy store still holds it,
the generator must exit loudly naming migrate-legacy-state.sh — never silently
start with empty state, never silently read the legacy path.

All tests use temp dirs + env overrides; the real HOME paths are never touched.
"""
import importlib.util
import os
import unittest
import tempfile
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent / "daily-lm-video" / "generate.py"

spec = importlib.util.spec_from_file_location("generate_under_test", MODULE)
generate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generate)

ENV_KEYS = (
    "HOME", "LM_DATA_DIR", "LM_LEGACY_STATE_ROOT", "LM_VIDEO_BANK",
    "LM_VIDEO_STATE", "LM_VIDEO_OUTPUT_DIR", "LM_VIDEO_CALL_AUDIO",
    "LM_VIDEO_STOCK", "LM_VIDEO_TELEGRAM_PROOF", "LM_VIDEO_WHISPER_ASS",
)


@contextmanager
def scoped_env(overrides):
    saved = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update(overrides)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class DefaultPathConvention(unittest.TestCase):
    def test_generator_has_no_external_checkout_default(self):
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("anicca-project", source)
        self.assertNotIn("/.claude/", source)

    def test_argless_defaults_resolve_under_home_data_root_state_lm_video(self):
        with tempfile.TemporaryDirectory() as home:
            with scoped_env({"HOME": home}):
                args = generate.parser().parse_args([])
            expected_root = Path(home) / ".local/state/life-manager/state/lm-video"
            self.assertEqual(args.state, expected_root / "daily-render-state.jsonl")
            self.assertEqual(args.output_dir, expected_root / "daily-renders")
            self.assertEqual(args.call_audio.parent, expected_root / "recordings")
            self.assertEqual(args.stock, expected_root / "inputs/stock.mp4")
            self.assertEqual(args.telegram_proof, expected_root / "inputs/telegram-proof.png")
            self.assertEqual(args.whisper_ass.parent.parent, expected_root)

    def test_argless_defaults_follow_lm_data_dir_override(self):
        with tempfile.TemporaryDirectory() as root:
            with scoped_env({"HOME": root, "LM_DATA_DIR": str(Path(root) / "custom")}):
                args = generate.parser().parse_args([])
            expected_root = Path(root) / "custom/state/lm-video"
            self.assertEqual(args.state, expected_root / "daily-render-state.jsonl")
            self.assertEqual(args.output_dir, expected_root / "daily-renders")

    def test_daily_sh_rotation_state_and_generator_state_are_identical(self):
        """daily.sh line ROTATION_STATE=$LM_DATA_ROOT/state/lm-video/daily-render-state.jsonl
        must equal the argless generate.py default for the same environment."""
        with tempfile.TemporaryDirectory() as home:
            with scoped_env({"HOME": home}):
                args = generate.parser().parse_args([])
            daily_sh_default = Path(home) / ".local/state/life-manager/state/lm-video/daily-render-state.jsonl"
            self.assertEqual(args.state, daily_sh_default)


class LegacyStateGuard(unittest.TestCase):
    def make_dirs(self):
        root = Path(tempfile.mkdtemp(prefix="lm-guard-"))
        home = root / "home"
        legacy = root / "legacy-store" / "state"
        (legacy / "lm-video").mkdir(parents=True)
        home.mkdir()
        return home, legacy

    def test_main_fails_loud_when_state_absent_but_legacy_state_exists(self):
        home, legacy = self.make_dirs()
        (legacy / "lm-video" / "daily-render-state.jsonl").write_text(
            "{}\n", encoding="utf-8")
        with scoped_env({"HOME": str(home), "LM_LEGACY_STATE_ROOT": str(legacy)}):
            with self.assertRaises(SystemExit) as caught:
                generate.main([])
        self.assertIn("migrate-legacy-state.sh", str(caught.exception))
        self.assertIn("daily-render-state.jsonl", str(caught.exception))

    def test_main_fails_loud_when_input_absent_but_legacy_input_exists(self):
        home, legacy = self.make_dirs()
        recordings = legacy / "lm-video" / "recordings"
        recordings.mkdir(parents=True)
        (recordings / "2026-07-19T23-40-35-932b3fad-2a99-49ca-8250-3a49e682ce92.mp3").write_bytes(b"audio")
        with scoped_env({"HOME": str(home), "LM_LEGACY_STATE_ROOT": str(legacy)}):
            with self.assertRaises(SystemExit) as caught:
                generate.main([])
        self.assertIn("migrate-legacy-state.sh", str(caught.exception))

    def test_guard_passes_when_legacy_store_is_absent(self):
        home, legacy = self.make_dirs()
        with scoped_env({"HOME": str(home), "LM_LEGACY_STATE_ROOT": str(legacy)}):
            args = generate.parser().parse_args([])
            # No legacy files exist: the guard must not raise.
            generate.guard_unmigrated_legacy_state(args)

    def test_guard_passes_when_new_state_already_exists(self):
        home, legacy = self.make_dirs()
        (legacy / "lm-video" / "daily-render-state.jsonl").write_text(
            "{}\n", encoding="utf-8")
        with scoped_env({"HOME": str(home), "LM_LEGACY_STATE_ROOT": str(legacy)}):
            args = generate.parser().parse_args([])
            args.state.parent.mkdir(parents=True)
            args.state.write_text("{}\n", encoding="utf-8")
            generate.guard_unmigrated_legacy_state(args)

    def test_guard_ignores_paths_explicitly_pointed_outside_the_data_root(self):
        home, legacy = self.make_dirs()
        (legacy / "lm-video" / "daily-render-state.jsonl").write_text(
            "{}\n", encoding="utf-8")
        elsewhere = home / "elsewhere-state.jsonl"
        with scoped_env({"HOME": str(home), "LM_LEGACY_STATE_ROOT": str(legacy)}):
            args = generate.parser().parse_args(["--state", str(elsewhere)])
            generate.guard_unmigrated_legacy_state(args)


if __name__ == "__main__":
    unittest.main()
