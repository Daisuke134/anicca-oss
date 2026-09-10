import argparse
import json
import tempfile
import unittest
from pathlib import Path

from agent_runner import command_for


class CodexSchemaCompatibilityTest(unittest.TestCase):
    def test_empty_schema_does_not_force_codex_to_return_empty_object(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(task_class="composition-agent", workdir=root)
            command = command_for(
                "codex", "/opt/homebrew/bin/codex", {},
                {"model": "gpt-5.6-terra", "effort": "medium"}, args,
                "Return the requested JSON object.", {}, root / "result.json",
                prompt_via_stdin=True,
            )
            self.assertNotIn("--output-schema", command)
            self.assertIn("-o", command)

    def test_codex_receives_supported_schema_copy_while_local_schema_stays_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_path = root / "inbox.schema.json"
            original = {
                "type": "object",
                "additionalProperties": False,
                "required": ["processed_message_ids"],
                "allOf": [
                    {"type": "object", "required": ["processed_message_ids"]},
                ],
                "properties": {
                    "processed_message_ids": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"type": "string"},
                    },
                    "decisions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string"},
                                "proposal": {"type": "string"},
                            },
                            "allOf": [{"required": ["kind"]}],
                            "if": {"properties": {"kind": {"const": "apply"}}},
                            "then": {"required": ["proposal"]},
                            "else": {"required": ["reason"]},
                        },
                    },
                },
            }
            original_path.write_text(json.dumps(original), encoding="utf-8")
            original_bytes = original_path.read_bytes()
            result_path = root / "attempt-01.result.json"
            args = argparse.Namespace(
                schema=original_path,
                task_class="composition-agent",
                workdir=root,
            )

            command = command_for(
                "codex",
                "/opt/homebrew/bin/codex",
                {},
                {"model": "gpt-5.6-terra", "effort": "medium"},
                args,
                "Process the bounded inbox candidates.",
                original,
                result_path,
                prompt_via_stdin=True,
            )

            provider_schema_path = Path(
                command[command.index("--output-schema") + 1]
            )
            provider_schema = json.loads(
                provider_schema_path.read_text(encoding="utf-8")
            )
            expected_provider_schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["processed_message_ids"],
                "properties": {
                    "processed_message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "decisions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string"},
                                "proposal": {"type": "string"},
                            },
                        },
                    },
                },
            }
            self.assertNotEqual(provider_schema_path, original_path)
            self.assertEqual(provider_schema, expected_provider_schema)

            def schema_keys(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        yield key
                        yield from schema_keys(item)
                elif isinstance(value, list):
                    for item in value:
                        yield from schema_keys(item)

            unsupported = {"uniqueItems", "allOf", "if", "then", "else"}
            self.assertTrue(unsupported.isdisjoint(set(schema_keys(provider_schema))))
            self.assertEqual(original_path.read_bytes(), original_bytes)
            self.assertEqual(json.loads(original_bytes), original)
            self.assertEqual(provider_schema_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
