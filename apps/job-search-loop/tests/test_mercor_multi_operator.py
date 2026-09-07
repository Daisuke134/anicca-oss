import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search_loop.mercor_operator import create_operator_config
from job_search_loop.mercor_pass import build_context


class MercorMultiOperatorTests(unittest.TestCase):
    def test_redacted_operators_have_separate_state_browser_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = []
            for operator_id, listing_id, cdp_port in (
                ("operator-a", "list-a", 9334),
                ("operator-b", "list-b", 9335),
            ):
                profile = root / f"{operator_id}-profile.json"
                resume = root / f"{operator_id}-resume.pdf"
                profile.write_text(json.dumps({
                    "version": 1,
                    "candidate": {
                        "operator_id": operator_id,
                        "base": "Japan",
                        "application_email": f"{operator_id}@example.invalid",
                    },
                    "facts": [{
                        "id": "redacted-profile",
                        "claim": "Approved operator profile exists",
                        "evidence": "private profile fixture",
                    }],
                }), encoding="utf-8")
                resume.write_bytes(f"redacted resume {operator_id}".encode())
                config = create_operator_config(
                    operator_id=operator_id,
                    profile_path=profile,
                    resume_path=resume,
                    base_root=root / "state",
                    locales=["en"],
                    role_families=["data"],
                    weekly_hours=20,
                )
                state_root = Path(config.state_root)
                (state_root / "applications.jsonl").write_text(
                    json.dumps({"listing_id": listing_id}) + "\n", encoding="utf-8"
                )
                evidence = state_root / "evidence" / f"{listing_id}.json"
                evidence.parent.mkdir(mode=0o700)
                evidence.write_text(json.dumps({"operator": operator_id}), encoding="utf-8")
                os.chmod(evidence, 0o600)
                configs.append((config, listing_id, cdp_port, evidence))

            contexts = []
            for config, _, cdp_port, _ in configs:
                with patch.dict(os.environ, {"MERCOR_OPERATOR_ID": config.operator_id}):
                    contexts.append(
                        build_context(
                            state_root=Path(config.state_root),
                            profile_path=Path(config.profile_path),
                            resume_path=Path(config.resume_path),
                            cdp_url=f"http://127.0.0.1:{cdp_port}",
                        )
                    )

            self.assertEqual([context["operator_id"] for context in contexts], ["operator-a", "operator-b"])
            self.assertNotEqual(contexts[0]["state_root"], contexts[1]["state_root"])
            self.assertNotEqual(contexts[0]["applications_ledger"], contexts[1]["applications_ledger"])
            self.assertEqual(contexts[0]["submitted_listing_ids"], ["list-a"])
            self.assertEqual(contexts[1]["submitted_listing_ids"], ["list-b"])
            self.assertNotEqual(contexts[0]["cdp_url"], contexts[1]["cdp_url"])
            self.assertNotEqual(contexts[0]["resume_path"], contexts[1]["resume_path"])
            self.assertEqual(json.loads(configs[0][3].read_text())["operator"], "operator-a")
            self.assertEqual(json.loads(configs[1][3].read_text())["operator"], "operator-b")
            for _, _, _, evidence in configs:
                self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
