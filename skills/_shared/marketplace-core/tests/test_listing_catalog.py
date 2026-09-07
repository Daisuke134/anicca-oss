import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from listing_catalog import (  # noqa: E402
    CatalogLoadError,
    CatalogValidationError,
    UnknownFamily,
    UnknownPlatform,
    entries_by_family,
    load,
    platforms,
    project,
    project_lancers,
)


REPO_ROOT = Path(__file__).resolve().parents[4]
REAL_CATALOG = REPO_ROOT / "skills" / "gig-work" / "profile" / "listings" / "catalog.json"
GIG_SCRIPTS = REPO_ROOT / "skills" / "earn" / "gig" / "scripts"


def _minimal_catalog(**overrides):
    catalog = {
        "version": 1,
        "listings": [
            {
                "id": "widget-basic",
                "title_ja": "テストサービスを提供します",
                "family": "test_family",
                "value_prop": "テスト用の価値提案。",
                "tiers": [
                    {"name": "ベーシック", "price_jpy": 1000, "scope": "基本スコープ", "delivery_days": 3},
                ],
                "deliverables": ["成果物A"],
                "required_inputs": ["入力A"],
                "faq": [{"q": "質問？", "a": "回答。"}],
                "platform_overrides": {
                    "coconala": {"category": "coconala-category"},
                    "lancers": {"category": "lancers-category"},
                    "crowdworks": {"category": "crowdworks-category"},
                },
                "paid_addons": [{"name": "追加オプション", "price_jpy": 500}],
                "image_guidance": {"cover": "説明"},
            }
        ],
    }
    catalog.update(overrides)
    return catalog


class RealCatalogTests(unittest.TestCase):
    def test_real_catalog_loads_with_20_unique_families_and_tiers(self):
        catalog = load(REAL_CATALOG)
        listings = catalog["listings"]
        self.assertEqual(len(listings), 20)
        families = [row["family"] for row in listings]
        self.assertEqual(len(families), len(set(families)))
        for row in listings:
            self.assertTrue(row["tiers"], row["family"])

    def test_real_catalog_every_listing_has_all_three_platform_keys(self):
        catalog = load(REAL_CATALOG)
        for row in catalog["listings"]:
            overrides = row.get("platform_overrides") or {}
            self.assertEqual(
                set(overrides.keys()), {"coconala", "lancers", "crowdworks"},
                f"{row['family']} is missing a platform mapping",
            )

    def test_project_differs_by_platform_category_on_a_real_family(self):
        catalog = load(REAL_CATALOG)
        family = catalog["listings"][0]["family"]
        coconala = project(catalog, family, "coconala")
        lancers = project(catalog, family, "lancers")
        crowdworks = project(catalog, family, "crowdworks")
        categories = {coconala["category"], lancers["category"], crowdworks["category"]}
        self.assertEqual(len(categories), 3)

    def test_project_lancers_on_line_bot_dev(self):
        catalog = load(REAL_CATALOG)
        result = project_lancers(catalog, "line_bot_dev")
        self.assertFalse(result["title_stem"].endswith("ます"))
        listing = entries_by_family(catalog)["line_bot_dev"]
        self.assertEqual(len(result["plans"]), len(listing["tiers"]))
        for plan, tier in zip(result["plans"], listing["tiers"]):
            self.assertEqual(plan["price_jpy"], tier["price_jpy"])
            self.assertEqual(plan["delivery_days"], tier["delivery_days"])
        self.assertEqual(
            set(result["missing"]),
            {
                "subcategory", "service_type", "industry", "tags", "notice", "portfolio",
                "software_portfolio", "seller_profile", "profile_avatar_path",
                "profile_avatar_sha256", "image_path", "image_sha256",
            },
        )


class RealCatalogLancersOverrideGroundingTests(unittest.TestCase):
    """The catalogue's platform_overrides.lancers block must name values the live Lancers
    creation form (/myplan/add?type=manual) actually offers, never an invented string. Read
    live 2026-09-07: nine main-category labels, the fifteen LANCERS_DELIVERY_DAYS values (see
    listing_catalog.LANCERS_DELIVERY_DAYS), and the first twelve of the industry select's fifty
    options. subcategory is a dependent select whose options only appear after the main
    category is chosen; its vocabulary was read live 2026-09-07 from Lancers' public
    package-browse taxonomy (https://www.lancers.jp/menu/search), corroborated by the
    currently-live hand-authored package (skills/earn/lancers/products/monthly-sns-content-ops-v1.json)
    whose category="Web集客・マーケティング"/subcategory="SNSマーケティング・運用代行" pair matches
    that taxonomy exactly. Only the three main categories the catalogue's families actually use
    are encoded below (LANCERS_SUBCATEGORIES_BY_CATEGORY); a family filed under a fourth main
    category the test cannot check must fail loudly (see
    test_every_family_category_has_an_encoded_subcategory_list), not slip through unchecked. A
    wrong transcription is still caught downstream: the create form fails closed if the label
    does not match an actual <option> (see storefront_offer._fill_create_form).
    """

    LANCERS_MAIN_CATEGORIES = {
        "選択してください", "AI・プログラミング・システム開発", "音楽・ナレーション",
        "Web集客・マーケティング", "ビジネス・コンサルティング", "デザイン・Webデザイン",
        "データ分析・作業自動化", "動画制作・アニメーション・写真", "ライティング・翻訳", "その他",
    }
    LANCERS_INDUSTRIES_OBSERVED = {
        "選択してください", "IT・通信・インターネット", "マスコミ・メディア", "新聞・雑誌・出版",
        "広告・イベント・プロモーション", "芸能・エンターテイメント", "ゲーム・アニメ・玩具",
        "恋愛・出会い・占い", "婚活・ブライダル", "動物・ペット", "生花・園芸・造園", "美術・工芸・音楽",
    }
    LANCERS_SUBCATEGORIES_BY_CATEGORY = {
        "AI・プログラミング・システム開発": {
            "生成AI・LLMアプリ開発", "AI基盤構築・MLOps", "AIシステム・アプリケーション開発",
            "AIコンサルティング・導入サポート", "AI・チャットボット開発", "ECサイト・ネットショップ通販",
            "Webプログラミング・システム開発/運用", "WordPressサイト構築・移行・運用",
            "モバイルアプリ・スマホアプリ", "Shopify構築・移行・運用", "CMS", "Webサイト・ホームページ制作",
            "HTML/CSSコーディング代行", "デスクトップアプリ", "サーバー・インフラ構築・移行",
            "デバッグ・テスト検証・コードレビュー", "ITサポート・コンサルティング", "セキュリティ・データ保護",
            "動画配信システム構築・設定", "ゲーム制作・開発", "ファイル変換", "プログラミング・システム開発(その他)",
        },
        "データ分析・作業自動化": {
            "データ分析・解析", "データサイエンス", "データ可視化・ビジュアライゼーション",
            "情報・データ処理", "Excelマクロ・VBA開発", "データベース", "データ入力", "データ(その他)",
        },
        "ビジネス・コンサルティング": {
            "生成AI活用・プロンプト作成", "ビジネスコンサルティング", "営業代行・テレアポ代行",
            "広報・PR代行", "市場調査・マーケットリサーチ", "組織・人事(HR)コンサルティング",
            "経理代行サービス・財務・税務", "プレゼン資料作成代行", "オンラインアシスタント(秘書)",
            "プロジェクトマネジメントのコンサルティング", "カスタマーコミュニケーション(CCM)", "インタビュー調査",
            "事業計画・ビジネスプランの作成", "ゲームアイデア・コンセプト企画", "ビジネス・コンサルティング(その他)",
        },
    }

    def test_every_family_lancers_category_is_a_real_form_option(self):
        catalog = load(REAL_CATALOG)
        for row in catalog["listings"]:
            category = row["platform_overrides"]["lancers"]["category"]
            self.assertIn(
                category, self.LANCERS_MAIN_CATEGORIES,
                f"{row['family']}: {category!r} is not one of the nine real main-category labels",
            )
            self.assertNotEqual(category, "選択してください", row["family"])

    def test_every_family_lancers_industry_is_one_of_the_twelve_observed_labels(self):
        catalog = load(REAL_CATALOG)
        for row in catalog["listings"]:
            industry = row["platform_overrides"]["lancers"]["industry"]
            self.assertIn(
                industry, self.LANCERS_INDUSTRIES_OBSERVED,
                f"{row['family']}: {industry!r} was not among the twelve industry options actually read",
            )
            self.assertNotEqual(industry, "選択してください", row["family"])

    def test_every_family_has_one_to_five_distinct_nonempty_tags(self):
        catalog = load(REAL_CATALOG)
        for row in catalog["listings"]:
            tags = row["platform_overrides"]["lancers"]["tags"]
            self.assertIsInstance(tags, list, row["family"])
            self.assertTrue(1 <= len(tags) <= 5, f"{row['family']}: {len(tags)} tags")
            self.assertEqual(len(tags), len(set(tags)), f"{row['family']}: duplicate tags {tags}")
            for tag in tags:
                self.assertIsInstance(tag, str, row["family"])
                self.assertTrue(tag.strip(), row["family"])

    def test_every_family_has_a_nonempty_notice(self):
        catalog = load(REAL_CATALOG)
        for row in catalog["listings"]:
            notice = row["platform_overrides"]["lancers"]["notice"]
            self.assertIsInstance(notice, str, row["family"])
            self.assertTrue(notice.strip(), row["family"])

    def test_every_family_has_a_nonempty_subcategory(self):
        catalog = load(REAL_CATALOG)
        for row in catalog["listings"]:
            subcategory = row["platform_overrides"]["lancers"].get("subcategory")
            self.assertIsInstance(subcategory, str, row["family"])
            self.assertTrue(subcategory.strip(), row["family"])

    def test_every_family_category_has_an_encoded_subcategory_list(self):
        """A family filed under a main category this test cannot check (e.g.
        デザイン・Webデザイン without its vocabulary being added) must fail loudly rather than
        have its subcategory go unverified."""
        catalog = load(REAL_CATALOG)
        for row in catalog["listings"]:
            category = row["platform_overrides"]["lancers"]["category"]
            self.assertIn(
                category, self.LANCERS_SUBCATEGORIES_BY_CATEGORY,
                f"{row['family']}: {category!r} has no encoded subcategory list to check against",
            )

    def test_every_family_subcategory_belongs_to_its_own_category(self):
        """A subcategory borrowed from the wrong parent group (e.g. a データ分析・作業自動化
        family carrying a AI・プログラミング・システム開発 subcategory) must fail here."""
        catalog = load(REAL_CATALOG)
        for row in catalog["listings"]:
            lancers = row["platform_overrides"]["lancers"]
            category = lancers["category"]
            subcategory = lancers["subcategory"]
            allowed = self.LANCERS_SUBCATEGORIES_BY_CATEGORY.get(category, set())
            self.assertIn(
                subcategory, allowed,
                f"{row['family']}: {subcategory!r} is not one of {category!r}'s real subcategory options",
            )

    def test_lancers_override_carries_no_other_unexpected_keys(self):
        catalog = load(REAL_CATALOG)
        for row in catalog["listings"]:
            self.assertEqual(
                set(row["platform_overrides"]["lancers"]),
                {"category", "subcategory", "industry", "tags", "notice"},
                row["family"],
            )


class ValidationErrorTests(unittest.TestCase):
    def test_missing_file_raises_catalog_load_error(self):
        with self.assertRaises(CatalogLoadError):
            load(Path("/nonexistent/does-not-exist-catalog.json"))

    def test_malformed_json_raises_catalog_load_error(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(CatalogLoadError):
                load(bad)

    def test_duplicate_family_raises_catalog_validation_error(self):
        catalog = _minimal_catalog()
        catalog["listings"].append(dict(catalog["listings"][0]))
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaises(CatalogValidationError):
                load(path)

    def test_tier_missing_price_jpy_raises_catalog_validation_error(self):
        catalog = _minimal_catalog()
        del catalog["listings"][0]["tiers"][0]["price_jpy"]
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaises(CatalogValidationError):
                load(path)

    def test_listing_with_no_tiers_raises_catalog_validation_error(self):
        catalog = _minimal_catalog()
        catalog["listings"][0]["tiers"] = []
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.assertRaises(CatalogValidationError):
                load(path)


class ProjectionTests(unittest.TestCase):
    def test_entries_by_family_keeps_whole_rows(self):
        catalog = _minimal_catalog()
        entries = entries_by_family(catalog)
        self.assertEqual(entries["test_family"]["id"], "widget-basic")
        self.assertIn("paid_addons", entries["test_family"])
        self.assertIn("platform_overrides", entries["test_family"])

    def test_platforms_returns_union_of_overrides(self):
        catalog = _minimal_catalog()
        self.assertEqual(platforms(catalog), {"coconala", "lancers", "crowdworks"})

    def test_project_unknown_family_raises(self):
        catalog = _minimal_catalog()
        with self.assertRaises(UnknownFamily):
            project(catalog, "does_not_exist", "coconala")

    def test_project_unknown_platform_raises(self):
        catalog = _minimal_catalog()
        with self.assertRaises(UnknownPlatform):
            project(catalog, "test_family", "not_a_real_platform")

    def test_project_carries_fields_and_merges_override(self):
        catalog = _minimal_catalog()
        result = project(catalog, "test_family", "lancers")
        self.assertEqual(result["family"], "test_family")
        self.assertEqual(result["id"], "widget-basic")
        self.assertEqual(result["title_ja"], "テストサービスを提供します")
        self.assertEqual(result["category"], "lancers-category")
        self.assertEqual(result["platform"], "lancers")
        self.assertEqual(result["catalog_version"], 1)


class CoconalaDelegationTests(unittest.TestCase):
    """Coconala's _load_catalog_entries must delegate to this module without regressing.

    See storefront_direct.py:_load_catalog_entries. Production callers around line 6653
    (capability templates) and line 7655 (the create path) depend on this exact return
    shape and on {} being returned rather than raised when the catalog is unreadable.
    """

    def _import_direct(self):
        sys.path.insert(0, str(GIG_SCRIPTS))
        import storefront_direct as direct

        return direct

    def test_returns_same_keys_as_before_for_a_real_family(self):
        direct = self._import_direct()
        entries = direct._load_catalog_entries()
        self.assertTrue(entries, "expected the real catalog to yield entries")
        family, row = next(iter(entries.items()))
        self.assertEqual(
            set(row.keys()),
            {"id", "title_ja", "value_prop", "tiers", "deliverables", "required_inputs", "faq"},
        )

    def test_returns_empty_dict_for_unreadable_path(self):
        direct = self._import_direct()
        entries = direct._load_catalog_entries(Path("/nonexistent/does-not-exist-catalog.json"))
        self.assertEqual(entries, {})


if __name__ == "__main__":
    unittest.main()


# --- the term derivation promoted out of the CrowdWorks adapter, 2026-09-07 -----------------

def _terms_module():
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "listing_catalog.py"
    spec = importlib.util.spec_from_file_location("listing_catalog_terms_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_title_becomes_the_nouns_a_keyword_search_can_match():
    module = _terms_module()
    assert module.listing_terms({"title_ja": "0→1でWebアプリ・業務システムを最短開発します"}) == (
        "Webアプリ", "業務システム")


def test_the_possessive_tail_is_cut_because_the_noun_is_what_is_searchable():
    module = _terms_module()
    assert module.listing_terms({"title_ja": "Webサイトの不具合修正を承ります"}) == ("Webサイト",)
    assert module.listing_terms({"title_ja": "システム開発のお見積り・要件定義相談します"}) == (
        "システム開発", "要件定義相談")


def test_a_row_is_never_dropped_out_of_discovery_by_the_length_cap():
    """Capping is a search-quality rule, not a reason to make a listing unfindable."""
    module = _terms_module()
    assert module.listing_terms({"title_ja": "きわめて長い名詞句だけでできている見出しです"})


def test_search_terms_is_the_deduplicated_union_of_every_row():
    module = _terms_module()
    catalog = {"listings": [{"title_ja": "業務システム・Webアプリを開発します"},
                            {"title_ja": "業務システム・Shopifyを開発します"}]}
    assert module.search_terms(catalog) == ("業務システム", "Webアプリ", "Shopify")
