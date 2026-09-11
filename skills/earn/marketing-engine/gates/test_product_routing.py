import json
import pathlib
import tempfile
import unittest
import subprocess

from product_router import RoutingError, canonical_product_id, load_registry
from variation import create_plan, eligible_hooks


ENGINE = pathlib.Path(__file__).resolve().parent.parent


class ProductRegistryTest(unittest.TestCase):
    def test_predecessor_product_ids_normalize_only_at_read_boundary(self):
        self.assertEqual(canonical_product_id("aniccaios"), "anicca-ios")
        self.assertEqual(canonical_product_id("honne"), "honne-ai")
        self.assertEqual(canonical_product_id("ebook-ja"), "ebook-ja")

    def test_registry_schemas_are_valid_draft_2020_12(self):
        import jsonschema
        schemas = {}
        for path in (ENGINE / "schemas").glob("*-manifest.schema.json"):
            schema = json.loads(path.read_text())
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            jsonschema.Draft202012Validator.check_schema(schema)
            schemas[path.name] = schema
        for path in (ENGINE / "registry" / "products").glob("*.json"):
            jsonschema.validate(json.loads(path.read_text()), schemas["product-manifest.schema.json"])
        for path in (ENGINE / "registry" / "accounts").glob("*.json"):
            jsonschema.validate(json.loads(path.read_text()), schemas["account-manifest.schema.json"])

    def test_canonical_router_has_no_legacy_hook_or_openclaw_dependency(self):
        legacy_terms = ["fixed" + "-strings-", "hookPool" + "-ja.txt", ".open" + "claw"]
        for path in [ENGINE / "gates" / "product_router.py", ENGINE / "gates" / "variation.py",
                     ENGINE / "bin" / "lm"]:
            source = path.read_text()
            for term in legacy_terms:
                self.assertNotIn(term, source, f"{term} found in {path}")

    def test_four_products_and_locked_accounts_validate(self):
        registry = load_registry(ENGINE)
        self.assertEqual(set(registry.products), {"anicca-ios", "honne-ai", "ebook-ja", "ebook-en"})
        self.assertGreaterEqual(len(registry.accounts), 9)
        for account in registry.accounts.values():
            self.assertIn(account["product_id"], registry.products)
            self.assertEqual(len(account["product_ids"]), 1)

    def test_mobile_jobs_and_marketing_registry_share_product_ids(self):
        registry = load_registry(ENGINE)
        mobile = json.loads(
            (ENGINE.parents[2] / "apps/life-manager/config/mobile-app-loops.json").read_text()
        )
        job_products = {row["product_id"] for row in mobile["loops"].values()}
        self.assertEqual(job_products, {"anicca-ios", "honne-ai"})
        self.assertLessEqual(job_products, set(registry.products))

    def test_accounts_lock_exact_postiz_provider_and_complete_settings(self):
        registry = load_registry(ENGINE)
        expected_provider = {
            "instagram.anicca_en": "instagram-standalone",
            "instagram.anicca_encards": "instagram-standalone",
            "instagram.obou_anicca": "instagram-standalone",
            "tiktok.anicca_jp": "tiktok",
            "tiktok.honne_reveal": "tiktok",
            "tiktok.honnevideo": "tiktok",
            "tiktok.monk_anicca": "tiktok",
            "tiktok.obou_anicca": "tiktok",
            "youtube.anicca_ai": "youtube",
        }
        self.assertEqual({key: row["publisher_provider"]
                          for key, row in registry.accounts.items()}, expected_provider)
        for row in registry.accounts.values():
            settings = row["publisher_settings"]
            if row["publisher_provider"] is None:
                self.assertIsNone(settings)
            elif row["publisher_provider"] == "tiktok":
                self.assertEqual(set(settings), {
                    "__type", "title", "privacy_level", "duet", "stitch", "comment",
                    "autoAddMusic", "brand_content_toggle", "brand_organic_toggle",
                    "video_made_with_ai", "content_posting_method",
                })
            elif row["publisher_provider"] == "instagram-standalone":
                self.assertEqual(settings, {"__type": "instagram-standalone",
                    "post_type": "post", "is_trial_reel": False, "collaborators": []})
            elif row["publisher_provider"] == "youtube":
                self.assertEqual(set(settings), {"__type", "title", "type",
                                                  "selfDeclaredMadeForKids", "thumbnail", "tags"})

    def test_duplicate_native_account_or_mixed_product_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            source = ENGINE / "registry"
            (root / "registry" / "products").mkdir(parents=True)
            (root / "registry" / "accounts").mkdir(parents=True)
            for path in (source / "products").glob("*.json"):
                (root / "registry" / "products" / path.name).write_bytes(path.read_bytes())
            (root / "registry" / "renderers.json").write_bytes(
                (source / "renderers.json").read_bytes())
            account = json.loads(next((source / "accounts").glob("*.json")).read_text())
            account["product_ids"] = [account["product_id"], "honne-ai"]
            (root / "registry" / "accounts" / "bad.json").write_text(json.dumps(account))
            with self.assertRaisesRegex(RoutingError, "exactly one product"):
                load_registry(root)

    def test_watercolor_and_monk_renderer_product_boundaries(self):
        registry = load_registry(ENGINE)
        for account in registry.accounts.values():
            formats = set(account["allowed_renderer_ids"])
            if "watercolor-monk" in formats:
                self.assertEqual(account["product_id"], "ebook-ja")
            if "heygen-avatar-iv" in formats:
                self.assertEqual(account["product_id"], "ebook-en")


class VariationPlanTest(unittest.TestCase):
    def setUp(self):
        self.registry = load_registry(ENGINE)
        self.hooks = ENGINE / "intel" / "hook-library.jsonl"

    def test_only_exact_product_language_hooks_are_eligible(self):
        en = eligible_hooks(self.registry, self.hooks, "ebook-en", "tiktok.monk_anicca")
        ja = eligible_hooks(self.registry, self.hooks, "ebook-ja", "tiktok.obou_anicca")
        self.assertGreaterEqual(len(en), 5)
        self.assertGreaterEqual(len(ja), 6)
        self.assertTrue(all(row["product_ids"] == ["ebook-en"] and row["language"] == "en"
                            for row in en))
        self.assertTrue(all(row["product_ids"] == ["ebook-ja"] and row["language"] == "ja"
                            for row in ja))
        self.assertEqual(eligible_hooks(self.registry, self.hooks, "anicca-ios",
                                        "tiktok.anicca_jp"), [])

    def test_cross_product_account_and_renderer_are_rejected(self):
        hook_id = eligible_hooks(self.registry, self.hooks, "ebook-en",
                                 "tiktok.monk_anicca")[0]["id"]
        with self.assertRaisesRegex(RoutingError, "account product mismatch"):
            create_plan(self.registry, self.hooks, product_id="ebook-en",
                        account_id="tiktok.obou_anicca", hook_id=hook_id,
                        tactic_id="tactic.faceless-visual-refresh-captions.v1",
                        renderer_id="heygen-avatar-iv", idempotency_key="test-1")
        with self.assertRaisesRegex(RoutingError, "renderer not allowed"):
            create_plan(self.registry, self.hooks, product_id="ebook-en",
                        account_id="tiktok.monk_anicca", hook_id=hook_id,
                        tactic_id="tactic.faceless-visual-refresh-captions.v1",
                        renderer_id="watercolor-monk", idempotency_key="test-1")

    def test_plan_is_idempotent_and_has_full_attribution_tuple(self):
        hook_id = eligible_hooks(self.registry, self.hooks, "ebook-en",
                                 "tiktok.monk_anicca")[0]["id"]
        args = dict(product_id="ebook-en", account_id="tiktok.monk_anicca",
                    hook_id=hook_id,
                    tactic_id="tactic.faceless-visual-refresh-captions.v1",
                    renderer_id="heygen-avatar-iv", idempotency_key="gate10-live-safe-en")
        first = create_plan(self.registry, self.hooks, **args)
        second = create_plan(self.registry, self.hooks, **args)
        self.assertEqual(first, second)
        self.assertEqual(set(first) >= {
            "experiment_id", "creative_id", "product_id", "account_id",
            "hook_id", "tactic_id", "renderer_id", "cta", "destination_url",
            "primary_metric", "attribution_method", "status",
        }, True)
        self.assertEqual(first["status"], "planned")
        changed = create_plan(self.registry, self.hooks, **{**args, "idempotency_key": "other"})
        self.assertNotEqual(first["experiment_id"], changed["experiment_id"])
        self.assertNotEqual(first["creative_id"], changed["creative_id"])

    def test_recent_history_excludes_hook_by_id(self):
        hooks = eligible_hooks(self.registry, self.hooks, "ebook-en", "tiktok.monk_anicca")
        with tempfile.TemporaryDirectory() as tmp:
            history = pathlib.Path(tmp) / "history.jsonl"
            history.write_text(json.dumps({
                "account_id": "tiktok.monk_anicca", "hook_id": hooks[0]["id"],
                "planned_at": "2099-01-01T00:00:00Z",
            }) + "\n")
            remaining = eligible_hooks(self.registry, self.hooks, "ebook-en",
                                       "tiktok.monk_anicca", history_path=history,
                                       now="2099-01-02T00:00:00Z")
            self.assertNotIn(hooks[0]["id"], {row["id"] for row in remaining})

    def test_lm_exposes_canonical_creative_candidates(self):
        result = subprocess.run([
            str(ENGINE / "bin" / "lm"), "creative", "candidates",
            "--product", "ebook-en", "--account", "tiktok.monk_anicca",
        ], check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "success")
        self.assertGreaterEqual(len(payload["candidates"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
