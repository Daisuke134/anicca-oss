from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("attribution.py")
SPEC = importlib.util.spec_from_file_location("marketing_attribution", MODULE_PATH)
assert SPEC and SPEC.loader
attribution = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = attribution
SPEC.loader.exec_module(attribution)


class CampaignTokenContractTest(unittest.TestCase):
    def test_token_is_deterministic_opaque_and_below_apple_limit(self):
        one = attribution.campaign_token("anicca-ios", "postiz:publication-123")
        two = attribution.campaign_token("anicca-ios", "postiz:publication-123")
        self.assertEqual(one, two)
        self.assertRegex(one, r"^ai_[a-z2-7]{20}$")
        self.assertLessEqual(len(one), 30)
        self.assertNotIn("publication", one)

    def test_product_prefixes_are_distinct(self):
        tokens = {
            attribution.campaign_token(product, "publication-1")
            for product in ("anicca-ios", "honne-ai", "ebook-ja", "ebook-en")
        }
        self.assertEqual(len(tokens), 4)
        self.assertEqual({token.split("_", 1)[0] for token in tokens}, {
            "ai", "ho", "ej", "ee"
        })

    def test_unknown_product_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown product"):
            attribution.campaign_token("other", "publication-1")

    def test_owned_redirect_and_app_store_destination_use_same_token(self):
        token = attribution.campaign_token("anicca-ios", "publication-1")
        owned = attribution.build_owned_redirect("https://aniccaai.com/", token)
        store = attribution.build_app_store_link("6755129214", token, "123456")
        self.assertEqual(owned, f"https://aniccaai.com/go/{token}")
        self.assertIn(f"ct={token}", store)
        self.assertIn("pt=123456", store)
        self.assertIn("mt=8", store)


class CampaignLedgerContractTest(unittest.TestCase):
    def test_legacy_product_replay_preserves_existing_campaign_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaigns.jsonl"
            legacy = {
                "schema_version": 1,
                "product_id": "aniccaios",
                "publication_id": "postiz:legacy",
                "campaign_token": attribution._campaign_token_for_identifier(
                    "aniccaios", "postiz:legacy"
                ),
                "owned_url": "https://aniccaai.com/go/legacy",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            path.write_text(json.dumps(legacy) + "\n")
            replay = attribution.register_campaign(
                path=path,
                product_id="anicca-ios",
                publication_id="postiz:legacy",
                base_url="https://aniccaai.com",
            )
            self.assertEqual(replay, legacy)
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_registration_is_idempotent_and_state_is_not_openclaw(self):
        self.assertNotIn(".openclaw", str(attribution.DEFAULT_STATE))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaigns.jsonl"
            first = attribution.register_campaign(
                path=path,
                product_id="ebook-en",
                publication_id="postiz:1",
                base_url="https://aniccaai.com",
            )
            second = attribution.register_campaign(
                path=path,
                product_id="ebook-en",
                publication_id="postiz:1",
                base_url="https://aniccaai.com",
            )
            self.assertEqual(first, second)
            self.assertEqual(len(path.read_text().splitlines()), 1)

    def test_token_collision_or_publication_remap_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "campaigns.jsonl"
            row = attribution.register_campaign(
                path=path,
                product_id="ebook-en",
                publication_id="postiz:1",
                base_url="https://aniccaai.com",
            )
            original = attribution.campaign_token
            try:
                attribution.campaign_token = lambda product, publication: row["campaign_token"]
                with self.assertRaisesRegex(ValueError, "campaign token collision"):
                    attribution.register_campaign(
                        path=path,
                        product_id="ebook-en",
                        publication_id="postiz:2",
                        base_url="https://aniccaai.com",
                    )
            finally:
                attribution.campaign_token = original


if __name__ == "__main__":
    unittest.main()
