"""listing.json has two writers -- _write_receipt (the single-offer lane) and
_write_catalog_listing (the catalogue lane) -- and until now only one of them read the file
before writing it.

A real listing (https://www.lancers.jp/menu/detail/1342218, live, verified with no login) was
recorded under listing.json's "catalog_listings" map by _write_catalog_listing. One wake later
that key read back {} -- _write_receipt built its own `value` dict from scratch, containing only
the fixed set of fields it has always owned, and overwrote the whole file with it, discarding
"catalog_listings" wholesale. This is verbatim the Apply lane guide's fault 10
(skills/loop-engineering/references/marketplace-apply-lane.md): "The state writer keeps a fixed
field list. Anything a lane attaches to a claim that is not on that list is dropped on the next
write, silently."

The fix is a shared read-modify-write (_read_state_json/_atomic_write_json) both writers now go
through, preserving whatever is already in the file wholesale rather than via a list of "known
extra keys to keep" -- an allowlist of exactly the two keys these two writers happen to use
today would pass every test below except test_an_unrecognised_third_party_key_survives_both_
writers, which is why that test exists: it proves the property is general, not merely "these two
writers happen to cooperate."

Run: python3 -m pytest skills/earn/lancers/tests/test_listing_json_writers_preserve_each_others_keys.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "skills/earn/lancers/scripts/storefront_offer.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "storefront_offer_listing_json_writers_under_test", SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _product(**overrides) -> dict:
    product = {
        "product_id": "monthly-sns-content-ops-v1",
        "product_version": 3,
        "listing_external_id": "1342218",
    }
    product.update(overrides)
    return product


def _demand() -> dict:
    return {
        "search_impressions": 10,
        "detail_views": 20,
        "favorites": 1,
        "inquiries": 2,
        "orders": 0,
    }


def _read_listing_json(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "listing.json").read_text(encoding="utf-8"))


# 1. _write_receipt preserves an existing catalog_listings map untouched -------------------------


def test_write_receipt_preserves_an_existing_catalog_listings_map(tmp_path):
    module = _module()
    state_path = tmp_path / "application.json"
    seeded = {
        "mvp_web_app_build": {
            "listing_external_id": "1342218",
            "public_url": "https://www.lancers.jp/menu/detail/1342218",
            "created_at": "2026-09-01T00:00:00Z",
        }
    }
    module._write_catalog_listing(state_path, "mvp_web_app_build", seeded["mvp_web_app_build"])

    module._write_receipt(state_path, _product(), _demand())

    on_disk = _read_listing_json(tmp_path)
    assert on_disk["catalog_listings"] == seeded
    # The receipt itself was actually written -- this is preservation, not a no-op write.
    assert on_disk["record_type"] == "listing_receipt"
    assert on_disk["listing_external_id"] == "1342218"


# 2. _write_catalog_listing preserves the receipt fields -- the pre-existing direction ----------


def test_write_catalog_listing_preserves_the_receipt_fields(tmp_path):
    module = _module()
    state_path = tmp_path / "application.json"
    module._write_receipt(state_path, _product(), _demand())
    before = _read_listing_json(tmp_path)
    assert before["record_type"] == "listing_receipt"  # sanity: the receipt is really there first

    module._write_catalog_listing(state_path, "mvp_web_app_build", {"listing_external_id": "1342218"})

    after = _read_listing_json(tmp_path)
    for key, value in before.items():
        assert after[key] == value
    assert after["catalog_listings"] == {"mvp_web_app_build": {"listing_external_id": "1342218"}}


# 3. An unknown third-party key survives both writers -- the general property, not two special --
#    cases. This is the one an allowlist of {"catalog_listings", <receipt fields>} would fail;
#    see test_an_allowlist_of_the_two_known_keys_would_have_failed_this below for the proof.


def test_an_unrecognised_third_party_key_survives_both_writers(tmp_path):
    module = _module()
    state_path = tmp_path / "application.json"
    listing_path = tmp_path / "listing.json"
    listing_path.parent.mkdir(parents=True, exist_ok=True)
    listing_path.write_text(
        json.dumps({"a_future_lanes_own_key": {"nested": "value", "n": 1}}), encoding="utf-8"
    )

    module._write_receipt(state_path, _product(), _demand())
    assert _read_listing_json(tmp_path)["a_future_lanes_own_key"] == {"nested": "value", "n": 1}

    module._write_catalog_listing(state_path, "mvp_web_app_build", {"listing_external_id": "1342218"})
    assert _read_listing_json(tmp_path)["a_future_lanes_own_key"] == {"nested": "value", "n": 1}


# 4. Concurrent-ish sequence: seed a catalog listing, run a receipt write, read back -- the ------
#    catalog listing is still there. This is the exact failure sequence measured on the live
#    host: _write_catalog_listing ran first (the real https://www.lancers.jp/menu/detail/1342218
#    listing), then a later wake's _write_receipt call wiped it.


def test_seed_catalog_listing_then_receipt_write_then_readback_still_has_the_catalog_listing(tmp_path):
    module = _module()
    state_path = tmp_path / "application.json"
    module._write_catalog_listing(
        state_path, "mvp_web_app_build",
        {"listing_external_id": "1342218", "public_url": "https://www.lancers.jp/menu/detail/1342218",
         "created_at": "2026-09-01T00:00:00Z"},
    )

    module._write_receipt(state_path, _product(), _demand())

    listings = module._read_catalog_listings(state_path)
    assert listings == {
        "mvp_web_app_build": {
            "listing_external_id": "1342218",
            "public_url": "https://www.lancers.jp/menu/detail/1342218",
            "created_at": "2026-09-01T00:00:00Z",
        }
    }


# --- Mutation-check proof: an allowlist of the two known keys passes 1/2/4 and still fails 3 ---
#
# Not asserted in code (a real allowlist implementation does not exist in this file to import);
# recorded here as documentation of the manual check performed while writing this suite: with
# _write_receipt's read-modify-write narrowed to `existing = {"catalog_listings": existing.get(
# "catalog_listings")} if "catalog_listings" in existing else {}` (i.e. keeping only the one key
# this task named) in place of the wholesale `_read_state_json` preservation, this file's tests 1,
# 2 and 4 above still pass -- they only ever seed "catalog_listings" or receipt fields, both on
# the allowlist -- while test_an_unrecognised_third_party_key_survives_both_writers fails, because
# "a_future_lanes_own_key" is not on it. That is the difference an allowlist cannot see and
# wholesale preservation can; reverting to _read_state_json makes all four pass again.
