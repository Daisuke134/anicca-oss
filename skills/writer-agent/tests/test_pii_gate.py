#!/usr/bin/env python3
"""Fail-closed publish-time PII gate: scanner contracts, CLI contracts, and real wiring.

Ported 2026-07-30 from the writer-engine worktree's
skills/writer-engine/tests/integration/test_e7_pii_gate.py (pytest) into this pipeline's own
stdlib-unittest style, which is what tests/run-all.sh executes (`python3 <file>`).

EVERY identifier below is invented. The real blocklist is operator PII: it lives only in
~/.openclaw/.env as WRITER_PII_BLOCKLIST, is never committed, and is never printed by this file.
These tests parameterise the gate through the same env vars production uses, and point
WRITER_PII_ENV_FILE at a path that does not exist so the machine's real .env can never leak into
a fixture or silently satisfy an "unconfigured blocklist" assertion.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SHARED = SCRIPTS / "_shared"
GATE_CLI = SCRIPTS / "pii-gate.py"

if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))
import pii_scan  # noqa: E402


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --- invented fixtures only -------------------------------------------------------------
FIXTURE_HANDLE = "Zephyrine987"
FIXTURE_NAME_JA = "瑠璃丸"
FIXTURE_EMAIL = "ops.fixture@example.invalid"
FIXTURE_BLOCKLIST = (FIXTURE_HANDLE, FIXTURE_NAME_JA, FIXTURE_EMAIL)
NO_ENV_FILE = "/nonexistent/pii-gate-tests/.env"

CLEAN_JA = (
    "# 常設マシンで実際に動かした記録\n\n"
    "私は自分の常設マシンの上で、実際に走らせた結果だけを書いています。\n"
    "![出力ログのスクリーンショット](https://example.com/log.png)\n"
    "検証したコマンドは全て再現できます。詳細は aniccaai.com にあります。\n"
)


class GateTestCase(unittest.TestCase):
    """Isolates every test from the machine's real blocklist and the live PII ledger."""

    def setUp(self) -> None:
        self._saved = dict(os.environ)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.log = self.tmp / "pii-gate.jsonl"
        os.environ["WRITER_PII_LOG"] = str(self.log)
        os.environ["WRITER_PII_ENV_FILE"] = NO_ENV_FILE
        os.environ.pop("WRITER_PII_BLOCKLIST_FILE", None)
        os.environ["WRITER_PII_BLOCKLIST"] = "\n".join(FIXTURE_BLOCKLIST)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def unconfigure(self) -> None:
        os.environ.pop("WRITER_PII_BLOCKLIST", None)
        os.environ.pop("WRITER_PII_BLOCKLIST_FILE", None)

    def child_env(self, **overrides: str) -> dict[str, str]:
        env = {**os.environ, **overrides}
        for key, value in overrides.items():
            if value is None:  # pragma: no cover - defensive
                env.pop(key, None)
        return env

    def write(self, name: str, body: str) -> Path:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return path


# ------------------------------------------------------------------------------------------
# scanner contracts (ported from the writer-engine suite)
# ------------------------------------------------------------------------------------------


class ScannerContractTest(GateTestCase):
    def test_blocklist_literal_is_found_case_insensitively(self):
        findings = pii_scan.scan(f"詳細は {FIXTURE_HANDLE.lower()} に置いてあります。")
        self.assertEqual([item.rule_id for item in findings], ["pii.blocklist.literal"])
        self.assertEqual(findings[0].matched_kind, "blocklist")

    def test_blocklisted_github_handle_url_is_its_own_rule(self):
        text = f"コードはこちら https://github.com/{FIXTURE_HANDLE}/example-repo\n"
        self.assertIn(
            "pii.blocklist.github-handle", {item.rule_id for item in pii_scan.scan(text)}
        )

    def test_unblocklisted_github_url_is_allowed(self):
        self.assertEqual(pii_scan.scan("https://github.com/some-neutral-org/anicca"), [])

    def test_generic_patterns_are_found(self):
        for kind, text in (
            ("email", "連絡は ops.contact@example.invalid まで。"),
            ("credit-card", "カード番号 4242 4242 4242 4242 を控えた。"),
            ("phone", "電話は +81 90-1234-5678 です。"),
            ("postal-address-jp", "〒150-0001 東京都渋谷区神宮前1-2-3 に届く。"),
        ):
            with self.subTest(kind=kind):
                self.assertIn(kind, {item.matched_kind for item in pii_scan.scan(text)})

    def test_non_luhn_digit_run_is_not_reported_as_a_card(self):
        findings = pii_scan.scan("ビルド番号 1234 5678 9012 3456 を記録した。")
        self.assertNotIn("credit-card", {item.matched_kind for item in findings})

    def test_repeated_digit_padding_is_not_reported_as_a_card(self):
        for text in ("0000000000000000", "0-0-0-0-0-0-0-0-0-0-0-0-0"):
            with self.subTest(text=text):
                self.assertNotIn(
                    "credit-card", {item.matched_kind for item in pii_scan.scan(text)}
                )

    def test_number_shaped_prose_is_not_reported_as_a_phone(self):
        for text in (
            "| 0 | 1 | 234 |",
            "手順 0 1 2345 の順に実行する。",
            "リリースは 2026-07-30 に出した。",
        ):
            with self.subTest(text=text):
                self.assertNotIn("phone", {item.matched_kind for item in pii_scan.scan(text)})

    def test_real_phone_formats_are_still_reported(self):
        for text in ("電話は 03-1234-5678 です。", "連絡先 090-1234-5678", "国際番号 +1 415 555 0132"):
            with self.subTest(text=text):
                self.assertIn("phone", {item.matched_kind for item in pii_scan.scan(text)})

    def test_clean_japanese_article_passes(self):
        self.assertEqual(pii_scan.scan(CLEAN_JA), [])

    def test_full_width_evasion_is_still_found(self):
        full_width = "Ｚｅｐｈｙｒｉｎｅ９８７"
        self.assertNotEqual(full_width, FIXTURE_HANDLE)
        self.assertEqual(
            [item.rule_id for item in pii_scan.scan(full_width)], ["pii.blocklist.literal"]
        )

    def test_missing_blocklist_configuration_is_a_config_failure(self):
        self.unconfigure()
        with self.assertRaises(pii_scan.PIIConfigError):
            pii_scan.scan("harmless text")

    def test_blocklist_file_is_read_when_env_var_points_at_it(self):
        self.unconfigure()
        blocklist_file = self.write("blocklist.txt", f"# comment\n{FIXTURE_HANDLE}\n\n")
        os.environ["WRITER_PII_BLOCKLIST_FILE"] = str(blocklist_file)
        self.assertEqual(
            [item.rule_id for item in pii_scan.scan(FIXTURE_HANDLE)], ["pii.blocklist.literal"]
        )

    def test_unreadable_blocklist_file_is_a_config_failure(self):
        self.unconfigure()
        os.environ["WRITER_PII_BLOCKLIST_FILE"] = str(self.tmp / "absent.txt")
        with self.assertRaises(pii_scan.PIIConfigError):
            pii_scan.scan("harmless text")

    def test_findings_and_violations_never_expose_the_matched_literal(self):
        text = f"handle {FIXTURE_HANDLE} and mail ops.contact@example.invalid"
        findings = pii_scan.scan(text)
        self.assertTrue(findings)
        for item in findings:
            self.assertIn("***", item.redacted)
            self.assertNotIn(FIXTURE_HANDLE, item.redacted)
        with self.assertRaises(pii_scan.PIIViolation) as refused:
            pii_scan.enforce("unit", {"body": text})
        self.assertNotIn(FIXTURE_HANDLE, str(refused.exception))
        self.assertIn("pii.blocklist.literal", str(refused.exception))

    def test_enforce_records_rule_ids_without_the_literal(self):
        with self.assertRaises(pii_scan.PIIViolation):
            pii_scan.enforce("pre-publish", {"body": f"see {FIXTURE_HANDLE}"})
        recorded = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(recorded)
        self.assertEqual(recorded[-1]["stage"], "pre-publish")
        self.assertIn("pii.blocklist.literal", recorded[-1]["rule_ids"])
        self.assertNotIn(FIXTURE_HANDLE, self.log.read_text(encoding="utf-8"))

    def test_the_suite_can_never_append_to_the_live_pii_ledger(self):
        """Regression from the source suite: two tests once appended to the real ledger."""
        configured = Path(os.environ["WRITER_PII_LOG"])
        self.assertNotEqual(configured, pii_scan.log_path({}))
        self.assertNotIn(".local/share/writer-engine", str(configured))
        self.assertNotIn(str(ROOT / "state"), str(configured))

    def test_dotenv_fallback_never_leaks_a_non_pii_key(self):
        """The .env fallback reads ONLY the two WRITER_PII_* keys."""
        self.unconfigure()
        env_file = self.write(
            "fake.env",
            "OPENAI_API_KEY=sk-must-never-be-read\n"
            f"WRITER_PII_BLOCKLIST='{FIXTURE_HANDLE}'\n"
            "NOTE_PASSWORD=must-never-be-read\n",
        )
        os.environ["WRITER_PII_ENV_FILE"] = str(env_file)
        recovered = pii_scan._dotenv_fallback(os.environ)
        self.assertEqual(set(recovered), {"WRITER_PII_BLOCKLIST"})
        self.assertEqual(pii_scan.load_blocklist(), (FIXTURE_HANDLE,))

    def test_dotenv_fallback_is_skipped_when_the_environment_already_configures_it(self):
        self.assertEqual(pii_scan._dotenv_fallback(os.environ), {})


# ------------------------------------------------------------------------------------------
# CLI contracts -- the boundary every bash publisher calls
# ------------------------------------------------------------------------------------------


class GateCliTest(GateTestCase):
    def run_gate(self, *args: str, env: dict[str, str] | None = None):
        return subprocess.run(
            [sys.executable, str(GATE_CLI), *args],
            env=env or dict(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_blocklisted_handle_exits_non_zero_with_rule_ids(self):
        artifact = self.write(
            "dirty.md", f"{CLEAN_JA}\nコードは https://github.com/{FIXTURE_HANDLE}/anicca です。\n"
        )
        result = self.run_gate("--stage", "unit", str(artifact))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.returncode, 3)
        self.assertIn("PII-GATE BLOCK", result.stderr)
        self.assertIn("pii.blocklist.github-handle", result.stderr)
        self.assertNotIn(FIXTURE_HANDLE, result.stdout + result.stderr)

    def test_clean_artifact_exits_zero(self):
        result = self.run_gate("--stage", "unit", str(self.write("clean.md", CLEAN_JA)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PII-GATE PASS", result.stdout)

    def test_missing_blocklist_exits_non_zero(self):
        env = dict(os.environ)
        env.pop("WRITER_PII_BLOCKLIST", None)
        env.pop("WRITER_PII_BLOCKLIST_FILE", None)
        result = self.run_gate(
            "--stage", "unit", str(self.write("clean.md", CLEAN_JA)), env=env
        )
        self.assertEqual(result.returncode, 4)
        self.assertIn("PII-GATE CONFIG-FAIL", result.stderr)

    def test_unreadable_artifact_exits_non_zero(self):
        result = self.run_gate("--stage", "unit", str(self.tmp / "absent.md"))
        self.assertEqual(result.returncode, 5)
        self.assertIn("PII-GATE ERROR", result.stderr)

    def test_no_argument_at_all_exits_non_zero(self):
        result = self.run_gate("--stage", "unit")
        self.assertNotEqual(result.returncode, 0)

    def test_undecodable_bytes_exit_non_zero(self):
        artifact = self.tmp / "binary.md"
        artifact.write_bytes(b"\xff\xfe\x00 not utf-8 \xc3\x28")
        result = self.run_gate("--stage", "unit", str(artifact))
        self.assertEqual(result.returncode, 5)

    def test_run_dir_mode_scans_every_frozen_artifact(self):
        run_dir = self.tmp / "run"
        run_dir.mkdir()
        (run_dir / "article-ja.md").write_text(CLEAN_JA, encoding="utf-8")
        (run_dir / "article-en.md").write_text("Clean English body.\n", encoding="utf-8")
        (run_dir / "x-post-ja.txt").write_text(
            f"著者は{FIXTURE_NAME_JA}です。", encoding="utf-8"
        )
        result = self.run_gate("--stage", "unit", "--run-dir", str(run_dir))
        self.assertEqual(result.returncode, 3)
        self.assertIn("pii.blocklist.literal", result.stderr)
        self.assertNotIn(FIXTURE_NAME_JA, result.stdout + result.stderr)

    def test_text_mode_blocks(self):
        result = self.run_gate("--stage", "unit", "--text", f"連絡は {FIXTURE_EMAIL} まで")
        self.assertEqual(result.returncode, 3)

    def dirty_ja_clean_xpost(self) -> Path:
        """A run shaped like the real 20260729-173948: dirty articles, clean X Post."""
        run_dir = self.tmp / "mixed"
        run_dir.mkdir()
        dirty = f"{CLEAN_JA}\nコードは https://github.com/{FIXTURE_HANDLE}/anicca です。\n"
        (run_dir / "article-ja.md").write_text(dirty, encoding="utf-8")
        (run_dir / "article-en.md").write_text(dirty, encoding="utf-8")
        (run_dir / "x-post-ja.txt").write_text("常設マシンで実測した話です。", encoding="utf-8")
        return run_dir

    def test_pair_scopes_the_scan_to_that_destinations_payload(self):
        run_dir = self.dirty_ja_clean_xpost()
        clean = self.run_gate(
            "--stage", "unit", "--run-dir", str(run_dir), "--pair", "x-post/ja"
        )
        self.assertEqual(clean.returncode, 0, clean.stderr)
        for pair in ("note/ja", "devto/en", "x-article/ja"):
            with self.subTest(pair=pair):
                blocked = self.run_gate(
                    "--stage", "unit", "--run-dir", str(run_dir), "--pair", pair
                )
                self.assertEqual(blocked.returncode, 3)

    def test_unknown_pair_falls_back_to_scanning_everything(self):
        """Fail-closed default: a pair this gate does not recognise must not narrow the scan."""
        run_dir = self.dirty_ja_clean_xpost()
        for pair in ("", "some-new-platform/ja"):
            with self.subTest(pair=pair):
                result = self.run_gate(
                    "--stage", "unit", "--run-dir", str(run_dir), "--pair", pair
                )
                self.assertEqual(result.returncode, 3)

    def test_every_required_pair_has_an_artifact_mapping(self):
        """A destination missing from PAIR_ARTIFACTS silently degrades to a whole-run scan."""
        helper = load(SHARED / "pii_gate.py", "pii_gate_pairs")
        required = {
            "note/ja", "zenn-article/ja", "devto/en", "substack/ja",
            "substack/en", "x-article/ja", "x-article/en", "x-post/ja",
        }
        self.assertEqual(required - set(helper.PAIR_ARTIFACTS), set())
        for pair, artifacts in helper.PAIR_ARTIFACTS.items():
            with self.subTest(pair=pair):
                self.assertTrue(set(artifacts) <= set(helper.RUN_ARTIFACTS), pair)


# ------------------------------------------------------------------------------------------
# python helper contracts
# ------------------------------------------------------------------------------------------


class GateHelperTest(GateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.helper = load(SHARED / "pii_gate.py", "pii_gate_under_test")

    def test_gate_files_refuses_on_a_finding(self):
        artifact = self.write("dirty.md", f"著者は{FIXTURE_NAME_JA}です。\n")
        with self.assertRaises(SystemExit) as refused:
            self.helper.gate_files("unit", [artifact])
        self.assertIn("pii.blocklist.literal", str(refused.exception))
        self.assertNotIn(FIXTURE_NAME_JA, str(refused.exception))

    def test_gate_files_refuses_on_a_missing_artifact(self):
        with self.assertRaises(SystemExit):
            self.helper.gate_files("unit", [self.tmp / "absent.md"])

    def test_gate_files_refuses_on_an_empty_target_list(self):
        with self.assertRaises(SystemExit):
            self.helper.gate_files("unit", [])

    def test_gate_files_refuses_when_the_blocklist_is_unconfigured(self):
        self.unconfigure()
        with self.assertRaises(SystemExit):
            self.helper.gate_files("unit", [self.write("clean.md", CLEAN_JA)])

    def test_gate_files_refuses_when_the_scanner_itself_raises(self):
        artifact = self.write("clean.md", CLEAN_JA)
        original = self.helper.pii_scan.scan_sources

        def exploding(*_args, **_kwargs):
            raise MemoryError("scanner blew up")

        self.helper.pii_scan.scan_sources = exploding
        try:
            with self.assertRaises(SystemExit) as refused:
                self.helper.gate_files("unit", [artifact])
            self.assertIn("pii.gate.internal-error", str(refused.exception))
        finally:
            self.helper.pii_scan.scan_sources = original

    def test_gate_files_passes_a_clean_artifact(self):
        self.assertIsNone(self.helper.gate_files("unit", [self.write("clean.md", CLEAN_JA)]))

    def test_gate_run_dir_refuses_an_empty_run_directory(self):
        empty = self.tmp / "empty-run"
        empty.mkdir()
        with self.assertRaises(SystemExit):
            self.helper.gate_run_dir("unit", empty)


# ------------------------------------------------------------------------------------------
# wiring -- the gate is worthless unless the real publishers call it
# ------------------------------------------------------------------------------------------


# (path relative to scripts/, what that file makes public)
PUBLISH_CALL_SITES = (
    ("publish-note.sh", "note draft creation on note.com"),
    ("publish-zenn.sh", "push into the PUBLIC zenn-articles GitHub repo"),
    ("publish-devto.sh", "Dev.to article create/stage"),
    ("publish-substack.sh", "Substack draft creation"),
    ("post-zenn.py", "git commit+push of the article into the public Zenn repo"),
    ("publish-note-managed.py", "note/ja go-live at ¥500"),
    ("publish-substack-managed.py", "substack/{ja,en} go-live"),
    ("x-publish/publish-to-x.sh", "X Article draft + go-live"),
    ("x-post/publish.py", "x-post/ja go-live"),
    ("x-publish/x_inplace_repair.py", "x-article/{ja,en} in-place go-live"),
    ("zenn-publish/current_run_zenn.py", "flips zenn published:true"),
    ("devto-publish/devto.py", "Dev.to stage/publish/repair"),
    ("note-publish/publish-paid.py", "note paid go-live"),
    ("substack-publish/substack_inplace_repair.py", "substack in-place go-live"),
    ("note-publish/note_inplace_repair.py", "note in-place repair of a live article"),
    ("_shared/publish-substack-mermaid.sh", "the managed Substack publish wrapper"),
)


class WiringTest(unittest.TestCase):
    def test_every_publish_script_invokes_the_pii_gate(self):
        missing = []
        for relative, what in PUBLISH_CALL_SITES:
            path = SCRIPTS / relative
            if not path.is_file():
                missing.append(f"{relative} (MISSING FILE) -- {what}")
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            if "pii-gate.py" not in body and "pii_gate" not in body:
                missing.append(f"{relative} -- {what}")
        self.assertEqual(missing, [], f"publish paths with no PII gate: {missing}")

    def test_no_blocklist_literal_was_committed_into_the_gate(self):
        """The blocklist is PII: it lives only in LIFE_MANAGER_ENV_FILE, never in this repo."""
        # pii_scan.py is the only loader, so it is the only file that must name the env var.
        # pii-gate.py / pii_gate.py delegate to it and deliberately hold no blocklist knowledge.
        self.assertIn("WRITER_PII_BLOCKLIST", (SHARED / "pii_scan.py").read_text(encoding="utf-8"))
        for path in (SCRIPTS / "pii-gate.py", SHARED / "pii_scan.py", SHARED / "pii_gate.py"):
            body = path.read_text(encoding="utf-8")
            for term in FIXTURE_BLOCKLIST:
                self.assertNotIn(term, body)
            try:
                real_terms = pii_scan.load_blocklist()
            except pii_scan.PIIConfigError:
                continue
            for term in real_terms:
                self.assertTrue(
                    term not in body,
                    f"{path.name} contains a real blocklist literal (value withheld)",
                )

    def test_persona_forbids_naming_the_operator_or_the_machine_location(self):
        """A gate stops a leak; the persona rule stops the model generating it in the first place.

        The leaked strings were in no template -- the model produced them freely each run. So the
        rule has to reach the model in BOTH places that actually do: SKILL.md's IDENTITY section
        (the canonical rule doc) and the literal prompt string built in article-daily.sh.
        """
        marker = "OPERATOR-IDENTIFIER BAN"
        for relative in ("SKILL.md", "article-daily.sh"):
            with self.subTest(file=relative):
                body = (ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(marker, body, f"{relative} carries no operator-identifier ban")

    def test_the_persona_rule_states_no_blocklist_literal(self):
        """The rule must be phrased generically; putting the real literals in a repo file is the
        exact leak this whole feature exists to prevent."""
        try:
            terms = pii_scan.load_blocklist()
        except pii_scan.PIIConfigError as error:  # no blocklist on this machine -> nothing to check
            self.skipTest(f"blocklist not configured here: {error}")
        for relative in ("SKILL.md", "article-daily.sh"):
            body = (ROOT / relative).read_text(encoding="utf-8")
            for term in terms:
                # assertNotIn would print the literal on failure; assertTrue with a safe message
                # keeps operator PII out of CI logs even when this test fails.
                self.assertTrue(
                    term not in body,
                    f"{relative} contains a real blocklist literal (value withheld)",
                )


if __name__ == "__main__":
    unittest.main()
