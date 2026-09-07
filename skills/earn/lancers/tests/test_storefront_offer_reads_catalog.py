"""Lancers reads the shared listing catalogue instead of re-deriving listing content by hand.

skills/gig-work/profile/listings/catalog.json is the owner's platform-independent listing
catalogue: 20 development listings, each carrying a ``platform_overrides.lancers`` block.
skills/_shared/marketplace-core/scripts/listing_catalog.py already projects a catalogue row
onto the Lancers product shape via ``project_lancers()``. Before this change,
storefront_offer.py never read the catalogue at all -- the one live Lancers listing
(monthly-sns-content-ops-v1.json) was, and remains, a fully hand-authored product file.

This test proves the new wiring is (a) provably inert for that one live file -- a
``catalog_family`` key is opt-in, and its absence must reproduce today's exact product dict
-- and (b) actually works for a real catalogue family with a complete Lancers-only overlay,
including the failure paths a wrong overlay or a bad catalogue projection must hit loudly.

Run: python3 -m pytest skills/earn/lancers/tests/test_storefront_offer_reads_catalog.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "skills/earn/lancers/scripts/storefront_offer.py"
LIVE_PRODUCT = REPO_ROOT / "skills/earn/lancers/products/monthly-sns-content-ops-v1.json"
REAL_CATALOG = REPO_ROOT / "skills/gig-work/profile/listings/catalog.json"
REAL_AVATAR = REPO_ROOT / "skills/gig-work/profile/avatar.jpg"
REAL_IMAGE = REPO_ROOT / "skills/earn/lancers/assets/monthly-sns-content-ops-v1.png"
# mvp_web_app_build carries three tiers with delivery_days 14/21/30, all in Lancers' allowed
# set, and a title/subtitle short enough to pass validation -- it projects clean.
REAL_FAMILY = "mvp_web_app_build"
# Eighteen of the catalogue's twenty families used to carry two tiers, which Lancers' product
# validator (exactly three plans) rejects outright -- the largest single obstacle to selling
# the catalogue there. Every family now carries a third ("プレミアム") tier, so no catalogue
# family reproduces that defect any more (ai_agent_integration stopped serving as this fixture
# earlier for the same reason: its 18-day tier used to fail before the Lancers projection
# started rounding it to 21). test 5 below no longer points at a real family; it corrupts a
# real, valid projection down to two plans to prove _product still fails loudly on that shape.


def _module():
    spec = importlib.util.spec_from_file_location("storefront_offer_reads_catalog_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _base_overlay(**overrides) -> dict:
    """Every Lancers-only field a product file must still carry once the catalogue owns
    title_stem/subtitle/category/plans/description. Image/avatar paths are absolute so the
    overlay is usable from any tmp_path without touching the repo tree.
    """
    overlay = {
        "product_id": "catalog-overlay-test-v1",
        "product_version": 1,
        "listing_external_id": "1000001",
        "superseded_listing_ids": [],
        "subcategory": "システム開発（オーダーメイド）",
        "service_type": "Webアプリ開発",
        "industry": "IT・通信・インターネット",
        "tags": ["Webアプリ開発", "業務システム", "MVP開発"],
        "notice": "ご相談内容を確認してから進めます。",
        "portfolio": {
            "external_id": "743964",
            "title_stem": "テスト用ポートフォリオを制作し",
            "subtitle": "テスト用のサブタイトル",
            "category": "マーケティング・営業・リサーチ・広報",
            "subcategory": "SNSアカウント運用・設定",
            "description": "テスト用の説明文です。",
            "duration_value": 1,
            "duration_unit": "週",
            "order_index": 10,
            "generated_ai": True,
        },
        "software_portfolio": {
            "external_id": "743987",
            "title_stem": "テスト用ソフトウェア実績を制作し",
            "subtitle": "テスト用のサブタイトル",
            "category": "AI・システム開発・運用",
            "subcategory": "AI自動化・エージェント開発",
            "industry": "IT・通信・インターネット",
            "description": "テスト用の説明文です。",
            "duration_value": 1,
            "duration_unit": "ヶ月",
            "reference_price_jpy": 180000,
            "listing_external_id": "",
            "order_index": 20,
            "generated_ai": True,
        },
        "seller_profile": {
            "public_path": "/profile/keiodaisuke",
            "subtitle": "テスト用プロフィールサブタイトル",
            "description": "テスト用プロフィール説明文です。",
        },
        "profile_avatar_path": str(REAL_AVATAR),
        "profile_avatar_sha256": "ff0cca485f9a8f00f66db4f881c72a5deb6b2c8eee9521e5db05a9612c39177d",
        "image_path": str(REAL_IMAGE),
        "image_sha256": "irrelevant-for-validation-but-catalog-reports-it-missing",
    }
    overlay.update(overrides)
    return overlay


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "product.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# 1. Inertness: no catalog_family key -> byte-identical to today's product dict -------------


def test_no_catalog_family_key_produces_todays_exact_product_dict():
    module = _module()
    raw = json.loads(LIVE_PRODUCT.read_text(encoding="utf-8"))
    assert "catalog_family" not in raw

    product, image, avatar = module._product(LIVE_PRODUCT)

    expected = dict(raw)
    expected["public_title"] = raw["title_stem"] + "ます"
    assert product == expected
    assert image == (LIVE_PRODUCT.parent / raw["image_path"]).resolve()
    assert avatar == (LIVE_PRODUCT.parent / raw["profile_avatar_path"]).resolve()


# 2. catalog_family + a complete overlay: catalogue fields win, overlay fields survive ------


def test_catalog_family_supplies_catalog_owned_fields_and_overlay_fields_survive(tmp_path):
    module = _module()
    overlay = _base_overlay(catalog_family=REAL_FAMILY)
    path = _write(tmp_path, overlay)

    product, _image, _avatar = module._product(path)

    listing_catalog = module._reach_marketplace_core()
    catalog = listing_catalog.load(REAL_CATALOG)
    projection = listing_catalog.project_lancers(catalog, REAL_FAMILY)

    assert product["title_stem"] == projection["title_stem"]
    assert product["subtitle"] == projection["subtitle"]
    assert product["category"] == projection["category"]
    assert product["plans"] == projection["plans"]
    assert product["description"] == projection["description"]
    assert "catalog_family" not in product

    for key in (
        "subcategory", "service_type", "industry", "tags", "notice", "portfolio",
        "software_portfolio", "seller_profile", "listing_external_id",
        "superseded_listing_ids", "product_id", "product_version",
    ):
        assert product[key] == overlay[key]


# 3. Overlay missing a catalogue-unmapped field fails, naming the field --------------------


def test_overlay_missing_a_catalog_unmapped_field_names_it(tmp_path):
    module = _module()
    overlay = _base_overlay(catalog_family=REAL_FAMILY)
    del overlay["notice"]
    path = _write(tmp_path, overlay)

    with pytest.raises(module.OfferError) as excinfo:
        module._product(path)

    message = str(excinfo.value)
    assert "catalog_overlay_incomplete" in message
    assert "notice" in message


# 4. A catalog_family absent from the catalogue fails, naming the family ------------------


def test_unknown_catalog_family_names_the_family(tmp_path):
    module = _module()
    overlay = _base_overlay(catalog_family="does_not_exist_family")
    path = _write(tmp_path, overlay)

    with pytest.raises(module.OfferError) as excinfo:
        module._product(path)

    message = str(excinfo.value)
    assert "catalog_family_unknown" in message
    assert "does_not_exist_family" in message


# 5. A catalogue projection _product() cannot accept fails loudly, naming the catalogue -----


def test_catalog_projection_that_fails_product_validation_names_the_catalog(tmp_path, monkeypatch):
    module = _module()
    listing_catalog = module._reach_marketplace_core()
    catalog = listing_catalog.load(REAL_CATALOG)
    projection = listing_catalog.project_lancers(catalog, REAL_FAMILY)

    # Every catalogue family now projects to a legal three-plan Lancers listing (see the
    # BROKEN_FAMILY history above), so this test can no longer point at a real family to prove
    # _product() fails loudly on an illegal shape. Corrupt a real, otherwise-valid projection
    # down to two plans instead -- exactly the defect that used to make eighteen families
    # unsellable on Lancers -- and confirm _product() still rejects it and still names the
    # family in the error.
    broken_projection = dict(projection)
    broken_projection["plans"] = projection["plans"][:2]
    monkeypatch.setattr(module, "_catalog_projection", lambda *_a, **_k: broken_projection)

    overlay = _base_overlay(catalog_family=REAL_FAMILY)
    path = _write(tmp_path, overlay)

    with pytest.raises(module.OfferError) as excinfo:
        module._product(path)

    message = str(excinfo.value)
    assert "catalog_product_invalid" in message
    assert REAL_FAMILY in message


# 6. The reporting function names the Lancers-required fields for a real family ------------


def test_reporting_names_required_fields_for_a_real_family():
    module = _module()
    report = module.catalog_lancers_requirements(REAL_FAMILY)

    assert report["family"] == REAL_FAMILY
    required = set(report["overlay_required_fields"])
    for field in ("listing_external_id", "subcategory", "service_type", "industry"):
        assert field in required


# 7. The merge does not mutate the shared projection for the next caller ------------------


def test_merge_does_not_mutate_the_shared_projection(monkeypatch):
    module = _module()
    listing_catalog = module._reach_marketplace_core()
    catalog = listing_catalog.load(REAL_CATALOG)
    projection = listing_catalog.project_lancers(catalog, REAL_FAMILY)
    snapshot = json.loads(json.dumps(projection))

    # Hand the merge the very same projection object a second caller would still be holding,
    # instead of letting it fetch its own fresh (already-independent) copy.
    monkeypatch.setattr(module, "_catalog_projection", lambda *_a, **_k: projection)

    overlay = _base_overlay(catalog_family=REAL_FAMILY)
    merged = module._catalog_overlay_product(overlay, REAL_FAMILY)

    assert projection == snapshot
    assert merged["plans"] is not projection["plans"]
