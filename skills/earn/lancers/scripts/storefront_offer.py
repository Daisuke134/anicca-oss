#!/usr/bin/env python3
"""Inspect or align one canonical Lancers storefront offer."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
DEFAULT_PRODUCT = HERE.parent / "products" / "monthly-sns-content-ops-v1.json"
DEFAULT_AVATAR = HERE.parents[2] / "gig-work" / "profile" / "avatar.jpg"
# The owner's platform-agnostic listing catalog (skills/_shared/marketplace-core owns the
# loader/projection logic; see _reach_marketplace_core). A product file may opt in to it via
# a top-level "catalog_family" key -- see _catalog_overlay_product.
DEFAULT_CATALOG = HERE.parents[2] / "gig-work" / "profile" / "listings" / "catalog.json"
# Product-shape fields the shared catalog owns once a product file names a catalog_family.
# Kept in one place because both the merge (_catalog_overlay_product) and the read-only
# report (catalog_lancers_requirements) need to agree on exactly what "catalog-owned" means.
_LANCERS_CATALOG_OWNED_FIELDS = ("title_stem", "subtitle", "category", "plans", "description")
# Identity/operational fields the catalog never carries an opinion on at all -- they are not
# "missing" from a catalog projection (listing_catalog.project_lancers never claims them), they
# simply never belong to the catalog's concept of a listing. catalog_lancers_requirements
# reports them alongside project_lancers' own `missing` list so the report names every field an
# overlay must supply, not only the ones the catalog projection itself flags.
_LANCERS_IDENTITY_FIELDS = ("product_id", "product_version", "listing_external_id", "superseded_listing_ids")
ORIGIN = "https://www.lancers.jp"
# Where _step() records a refusal, via dom_contract.py (see _reach_dom_contract). Mirrors
# application_tick.py's FORM_EVIDENCE (~/.local/state/anicca/lancers/proposal-form-changes.jsonl)
# -- same directory, dom_contract's own filename (dom-contract-failures.jsonl) -- so both of this
# lane's strict matchers leave their evidence in one place a human already knows to look.
# Module-level so a test can monkeypatch it to a tmp_path before calling _step() directly.
_EVIDENCE_DIR = Path("~/.local/state/anicca/lancers").expanduser()
DEMAND_LABELS = {
    "検索結果の表示人数": "search_impressions",
    "パッケージの閲覧人数": "detail_views",
    "お気に入り": "favorites",
    "相談数": "inquiries",
    "注文数": "orders",
}


class OfferError(RuntimeError): pass


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None: raise OfferError("runtime_unavailable")
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    return module


def _reach_marketplace_core() -> Any:
    """Reach skills/_shared/marketplace-core/scripts, the same way
    skills/earn/gig/scripts/storefront_direct.py's _load_catalog_entries does (see that
    function's docstring), and return the shared listing_catalog module. One mechanism,
    used by every caller that needs the shared catalog -- nothing here invents a second one.
    """
    shared_scripts = HERE.parents[2] / "_shared" / "marketplace-core" / "scripts"
    if str(shared_scripts) not in sys.path:
        sys.path.insert(0, str(shared_scripts))
    import listing_catalog
    return listing_catalog


def _reach_dom_contract() -> Any:
    """Reach skills/_shared/marketplace-core/scripts/dom_contract.py, the same way
    _reach_marketplace_core/_reach_form_observer already reach their modules. The one mechanism
    _step() uses to say what it saw before it raises -- see _step's own docstring.
    """
    shared_scripts = HERE.parents[2] / "_shared" / "marketplace-core" / "scripts"
    if str(shared_scripts) not in sys.path:
        sys.path.insert(0, str(shared_scripts))
    import dom_contract
    return dom_contract


def _reach_form_observer() -> Any:
    """Reach skills/_shared/marketplace-core/scripts/form_observer.py, the platform-neutral
    wizard/step observer, via the same sys.path mechanism _reach_marketplace_core already uses.
    This is the one and only place create_package's stall report is allowed to ask "which step
    is actually showing" -- see _create_observer_step_state, called from _create_step_evidence.
    """
    shared_scripts = HERE.parents[2] / "_shared" / "marketplace-core" / "scripts"
    if str(shared_scripts) not in sys.path:
        sys.path.insert(0, str(shared_scripts))
    import form_observer
    return form_observer


def _catalog_projection(catalog_module: Any, catalog_path: Path, family: str) -> dict[str, Any]:
    """Load the shared catalog and project `family` onto the Lancers shape.

    Fails loud and names the catalog: an unreadable/invalid catalog or an unknown family is
    an OfferError naming the family, never a silently empty/partial projection.
    """
    try:
        catalog = catalog_module.load(catalog_path)
    except catalog_module.CatalogError as error:
        raise OfferError(f"catalog_unavailable: family={family}: {error}") from error
    try:
        return catalog_module.project_lancers(catalog, family)
    except catalog_module.UnknownFamily as error:
        raise OfferError(f"catalog_family_unknown: family={family}: {error}") from error


def _catalog_overlay_product(
    overlay: Mapping[str, Any], family: str, catalog_path: Path = DEFAULT_CATALOG
) -> dict[str, Any]:
    """Merge a Lancers-only overlay onto the shared catalog's projection for `family`.

    The catalog owns _LANCERS_CATALOG_OWNED_FIELDS (title_stem, subtitle, category, plans,
    description) via listing_catalog.project_lancers; the overlay -- the rest of the product
    file -- supplies everything else (product_id, product_version, listing_external_id,
    superseded_listing_ids, subcategory, service_type, industry, tags, notice, portfolio,
    software_portfolio, seller_profile, image/avatar paths+hashes). project_lancers reports
    those unmapped fields under "missing"; any of them the overlay does not actually supply
    is an OfferError naming the fields, not a guess or a default. The returned dict is a new
    object -- the catalog projection itself is never mutated, so a second caller in the same
    process gets an independent projection.
    """
    listing_catalog = _reach_marketplace_core()
    projection = _catalog_projection(listing_catalog, catalog_path, family)
    missing = list(projection.get("missing") or [])
    unresolved = sorted(field for field in missing if field not in overlay)
    if unresolved:
        raise OfferError(f"catalog_overlay_incomplete: family={family}: missing={unresolved}")
    merged = dict(overlay)
    merged.pop("catalog_family", None)
    for field in _LANCERS_CATALOG_OWNED_FIELDS:
        merged[field] = deepcopy(projection[field])
    return merged


def catalog_lancers_requirements(family: str, catalog_path: Path = DEFAULT_CATALOG) -> dict[str, Any]:
    """Read-only report: which Lancers-required fields would an overlay still have to supply
    for `family`?

    Turns "wire the other listings" from an unknown into a list. Reports every field the
    catalog cannot supply: the identity/operational fields the catalog's concept of a listing
    never covers (_LANCERS_IDENTITY_FIELDS) plus whatever listing_catalog.project_lancers
    itself names under `missing` -- the same set _catalog_overlay_product enforces -- alongside
    the fields the catalog does own. Touches no product file; safe to call for every family in
    the catalog.
    """
    listing_catalog = _reach_marketplace_core()
    projection = _catalog_projection(listing_catalog, catalog_path, family)
    return {
        "family": family,
        "catalog_owned_fields": list(_LANCERS_CATALOG_OWNED_FIELDS),
        "overlay_required_fields": list(_LANCERS_IDENTITY_FIELDS) + list(projection.get("missing") or []),
    }


def _load_product_file(path: Path) -> dict[str, Any]:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError): raise OfferError("product_invalid") from None
    if not isinstance(value, dict): raise OfferError("product_invalid")
    return value


def _product(path: Path) -> tuple[dict[str, Any], Path, Path]:
    value = _load_product_file(path)
    family = value.get("catalog_family")
    if family is None:
        return _validate_product(value, path)
    if not isinstance(family, str) or not family.strip():
        raise OfferError("product_invalid")
    merged = _catalog_overlay_product(value, family)
    try:
        return _validate_product(merged, path)
    except OfferError as error:
        raise OfferError(f"catalog_product_invalid: family={family}: {error}") from error


def _validate_product(value: dict[str, Any], path: Path) -> tuple[dict[str, Any], Path, Path]:
    strings = ("product_id", "listing_external_id", "title_stem", "subtitle", "category", "subcategory", "service_type", "industry", "description", "notice", "image_path")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in strings): raise OfferError("product_invalid")
    if not re.fullmatch(r"[0-9]+", value["listing_external_id"]): raise OfferError("product_invalid")
    # 25-40: Lancers' own title field label ("25文字以上で入力してください") counts the stem
    # alone, not the public title with 「ます」 appended -- see
    # listing_catalog.LancersTitleStemLengthError for how this was measured.
    if not (25 <= len(value["title_stem"]) <= 40 and len(value["subtitle"]) <= 60 and len(value["description"]) <= 2000 and len(value["notice"]) <= 2000): raise OfferError("product_invalid")
    # Declares whether this listing is expected to carry Lancers' monthly-contract routes
    # (basicMain/standardMain/premiumMain × 1/3/6 months) -- a property of this specific
    # listing, not every listing (see _public()'s own docstring for why the readback gate reads
    # this field rather than asserting the routes unconditionally).
    if type(value.get("sells_monthly_contract")) is not bool: raise OfferError("product_invalid")
    tags, plans = value.get("tags"), value.get("plans")
    if not isinstance(tags, list) or not 1 <= len(tags) <= 5 or len(set(tags)) != len(tags) or not all(isinstance(tag, str) and tag.strip() for tag in tags): raise OfferError("product_invalid")
    if not isinstance(plans, list) or len(plans) != 3: raise OfferError("product_invalid")
    superseded = value.get("superseded_listing_ids")
    if not isinstance(superseded, list) or len(superseded) != len(set(superseded)) or any(not isinstance(item, str) or re.fullmatch(r"[0-9]+", item) is None for item in superseded) or value["listing_external_id"] in superseded: raise OfferError("product_invalid")
    for plan in plans:
        if not isinstance(plan, dict) or not isinstance(plan.get("description"), str) or not 1 <= len(plan["description"]) <= 80 or plan.get("delivery_days") not in {1,2,3,4,5,6,7,10,14,21,30,45,60,75,90} or type(plan.get("price_jpy")) is not int or plan["price_jpy"] < 1000: raise OfferError("product_invalid")
    portfolio_fields = {"external_id", "title_stem", "subtitle", "category", "subcategory", "description", "duration_value", "duration_unit", "order_index", "generated_ai"}
    for key in ("portfolio", "software_portfolio"):
        portfolio = value.get(key); extra = {"industry", "reference_price_jpy", "listing_external_id"} if key == "software_portfolio" else set()
        if not isinstance(portfolio, dict) or set(portfolio) != portfolio_fields | extra: raise OfferError("product_invalid")
        if not isinstance(portfolio["external_id"], str) or (portfolio["external_id"] and re.fullmatch(r"[0-9]+", portfolio["external_id"]) is None) or key == "portfolio" and not portfolio["external_id"]: raise OfferError("product_invalid")
        if not isinstance(portfolio["title_stem"], str) or not 1 <= len(portfolio["title_stem"] + "ました") <= 50 or not isinstance(portfolio["subtitle"], str) or len(portfolio["subtitle"]) > 60 or any(not isinstance(portfolio[name], str) or not portfolio[name].strip() for name in ("category", "subcategory")) or not isinstance(portfolio["description"], str) or not 1 <= len(portfolio["description"]) <= 1000: raise OfferError("product_invalid")
        if type(portfolio["duration_value"]) is not int or not 1 <= portfolio["duration_value"] <= 999 or portfolio["duration_unit"] not in {"時間", "日", "週", "ヶ月", "年"} or type(portfolio["order_index"]) is not int or not 0 <= portfolio["order_index"] <= 9999 or type(portfolio["generated_ai"]) is not bool: raise OfferError("product_invalid")
        if extra and (not isinstance(portfolio["industry"], str) or not portfolio["industry"].strip() or type(portfolio["reference_price_jpy"]) is not int or portfolio["reference_price_jpy"] < 1000 or not isinstance(portfolio["listing_external_id"], str) or portfolio["listing_external_id"] and re.fullmatch(r"[0-9]+", portfolio["listing_external_id"]) is None): raise OfferError("product_invalid")
    profile = value.get("seller_profile")
    if not isinstance(profile, dict) or set(profile) != {"public_path", "subtitle", "description"} or re.fullmatch(r"/profile/[A-Za-z0-9_-]+", str(profile.get("public_path") or "")) is None or not isinstance(profile.get("subtitle"), str) or not 1 <= len(profile["subtitle"]) <= 60 or not isinstance(profile.get("description"), str) or not 1 <= len(profile["description"]) <= 2000: raise OfferError("product_invalid")
    image = (path.parent / value["image_path"]).resolve()
    if not image.is_file() or image.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif"}: raise OfferError("product_invalid")
    avatar_path, avatar_sha256 = value.get("profile_avatar_path"), value.get("profile_avatar_sha256")
    if not isinstance(avatar_path, str) or not avatar_path.strip() or not isinstance(avatar_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", avatar_sha256) is None: raise OfferError("product_invalid")
    avatar = (path.parent / avatar_path).resolve()
    if not avatar.is_file() or avatar.suffix.lower() not in {".jpg", ".jpeg", ".png"} or avatar.stat().st_size > 3_000_000 or hashlib.sha256(avatar.read_bytes()).hexdigest() != avatar_sha256: raise OfferError("profile_avatar_invalid")
    value["public_title"] = value["title_stem"] + "ます"
    return value, image, avatar


def _text(page: Any, selector: str, *, field: str | None = None) -> str:
    """Read one field's rendered text off the public detail page.

    `field` names which of _public()'s observed keys (title/subtitle/description/notice) this
    call is reading -- its own docstring's own `context` convention (see _field()) -- so a
    refusal says which field and selector it was, and what it actually found, rather than a bare
    "public_readback_invalid" that could be any of the four.
    """
    label = f"field={field!r} " if field else ""
    locator = page.locator(selector)
    count = locator.count()
    if count != 1: raise OfferError(f"public_readback_invalid: {label}selector={selector!r} found={count}")
    text = " ".join(str(locator.inner_text() or "").split())
    if not text: raise OfferError(f"public_readback_invalid: {label}selector={selector!r} text=''")
    return text


def _public(page: Any, product: Mapping[str, Any], *, require_image: bool = True) -> dict[str, Any]:
    """Read back the live public page and compare it against `product`.

    Two of this gate's checks are properties of the *product*, not universal laws every listing
    must satisfy -- a package this lane only just created through the manual wizard can never
    pass either one, no matter how faithfully it was filled:

    - Monthly contract routes (basicMain/standardMain/premiumMain × 1/3/6 months) are a Lancers
      feature specific to the one hand-authored monthly service
      (monthly-sns-content-ops-v1.json), whose own product file says so via
      `sells_monthly_contract: true` (see _validate_product). Nothing this file creates through
      create_package() ever sets that field, so a freshly created package correctly skips this
      check instead of failing a gate it structurally cannot pass. A product that does claim it
      is still checked exactly as strictly as before -- the check itself is unchanged, only
      whether it runs at all is now conditional.
    - Whether an image is expected on the page is `require_image`, supplied by the caller: every
      existing caller (_apply(), run()'s --inspect path) defaults to True, preserving today's
      strict behaviour on the one hand-authored product, which always carries an on-disk image.
      create_package() alone passes the create wizard's own `image_attached` flag (画像ほか is an
      optional step; see _fill_create_form), since whether a freshly created package actually
      has an image is a fact of that specific creation attempt, not something the product itself
      can declare ahead of time.

    Every other comparison below -- title, subtitle, description, notice, and every plan's
    description/price/delivery_days -- stays mandatory for every caller: these are the actual
    proof of publication and are never made conditional.
    """
    listing_id = product["listing_external_id"]; public_url = f"{ORIGIN}/menu/detail/{listing_id}"
    response = page.goto(public_url, wait_until="domcontentloaded", timeout=30_000)
    status = None if response is None else response.status
    if response is None or status != 200 or page.url != public_url:
        raise OfferError(f"public_readback_invalid: field='page' expected_url={public_url!r} status={status!r} page_url={page.url!r}")
    canonical = page.locator('link[rel="canonical"]')
    og = page.locator('meta[property="og:url"]')
    # get_attribute() is only ever called on a locator known to resolve to exactly one element --
    # calling it against zero or several matches is a strict-mode violation the real Playwright
    # locator raises before this line ever gets to compare anything, so a mismatched count must
    # short-circuit the read, not merely the final boolean.
    canonical_count, og_count = canonical.count(), og.count()
    canonical_href = canonical.get_attribute("href") if canonical_count == 1 else None
    og_content = og.get_attribute("content") if og_count == 1 else None
    if canonical_count != 1 or canonical_href != public_url or og_count != 1 or og_content != public_url:
        raise OfferError(
            f"canonical_mismatch: canonical_count={canonical_count} canonical_href={canonical_href!r} "
            f"og_count={og_count} og_content={og_content!r} expected={public_url!r} page_url={page.url!r}"
        )
    plans = []
    for index, section in enumerate(page.locator("li.p-menu-browse-detail__sidebar-content.js-project-plan-tab-content").all()):
        selectors = ("p.p-menu-browse-detail__sidebar-description", "div.p-menu-browse-detail__sidebar-header-price", "div.p-menu-browse-detail__sidebar-menu")
        fields = [section.locator(selector) for selector in selectors]
        counts = [field.count() for field in fields]
        if counts == [0, 0, 0]: continue
        if counts != [1, 1, 1]:
            raise OfferError(f"public_readback_invalid: field='plan_section' plan_index={index} selectors={list(selectors)} found={counts}")
        raw_description, raw_price, raw_delivery = fields[0].inner_text(), fields[1].inner_text(), fields[2].inner_text()
        description = " ".join(raw_description.split()); price = "".join(re.findall(r"[0-9]", raw_price)); delivery = re.search(r"納期\s*([0-9]+)\s*日", raw_delivery)
        if not description or not price or delivery is None:
            failed_field, raw = ("description", raw_description) if not description else (("price", raw_price) if not price else ("delivery_days", raw_delivery))
            raise OfferError(f"public_readback_invalid: field={failed_field!r} plan_index={index} raw={_truncate_hard(raw, 200)!r}")
        plans.append({"description": description, "price_jpy": int(price), "delivery_days": int(delivery.group(1))})
    require_monthly_contract_routes = bool(product.get("sells_monthly_contract", False))
    routes = []
    if require_monthly_contract_routes:
        for prefix in ("basicMain", "standardMain", "premiumMain"):
            for month in (1, 3, 6):
                selector = f"#{prefix}{month}"
                field = page.locator(selector)
                count = field.count()
                if count != 1: raise OfferError(f"contract_route_invalid: selector={selector!r} found={count}")
                route = field.get_attribute("value") or ""
                expected_pattern = r"/project_board/quote_request\?project_plan_menu_id=[0-9]+" if month == 1 else rf"/monthly_work_contracts/client/[^/]+/add\?project_plan_menu_id=[0-9]+&month={month}"
                if re.fullmatch(expected_pattern, route) is None:
                    raise OfferError(f"contract_route_invalid: selector={selector!r} route={route!r} expected_pattern={expected_pattern!r}")
                routes.append(route)
    image = page.locator(".p-menu-browse-detail__carousel-list img")
    has_image = image.count() >= 1 and all("photo-film" not in str(image.nth(index).get_attribute("src") or "") for index in range(image.count()))
    observed = {"title": _text(page, "h1", field="title"), "subtitle": _text(page, ".l-page-header__heading-description", field="subtitle"), "description": _text(page, "#body + .p-project-plan-markdown", field="description"), "notice": _text(page, "#notice_for_sale + .c-text", field="notice"), "plans": plans}
    expected = {"title": product["public_title"], "subtitle": product["subtitle"], "description": " ".join(product["description"].split()), "notice": " ".join(product["notice"].split()), "plans": [{key: plan[key] for key in ("description", "price_jpy", "delivery_days")} for plan in product["plans"]]}
    mismatched = [key for key in expected if observed[key] != expected[key]] + ([] if not require_image or has_image else ["image"])
    contract_routes = {"spot": 3, "three_month": 3, "six_month": 3} if require_monthly_contract_routes else None
    return {"ok": True, "logged_in": True, "listing_external_id": listing_id, "canonical_url": public_url, "aligned": not mismatched, "mismatched_fields": mismatched, "has_image": has_image, "prices_jpy": [plan["price_jpy"] for plan in plans], "delivery_days": [plan["delivery_days"] for plan in plans], "contract_routes": contract_routes}


def _demand(page: Any, listing_id: str) -> dict[str, int]:
    page.goto(f"{ORIGIN}/myplan", wait_until="domcontentloaded", timeout=20_000)
    card = page.locator(f'.p-project-plan-myplan__store-content-over-title-link[href="/menu/detail/{listing_id}"]')
    if card.count() != 1: raise OfferError("demand_readback_invalid")
    scores = card.locator("xpath=ancestor::*[contains(concat(' ',normalize-space(@class),' '),' p-project-plan-myplan__store ')][1]").locator(".p-project-plan-myplan__store-content-score")
    result: dict[str, int] = {}
    for score in scores.all():
        labels = score.locator(".c-tooltip__text")
        if labels.count() != 1: continue
        key = DEMAND_LABELS.get(" ".join(str(labels.text_content() or "").split()))
        if key is None: continue
        values = score.locator(".p-project-plan-myplan__store-content-score-text")
        text = "" if values.count() != 1 else "".join(values.inner_text().split())
        if key in result or re.fullmatch(r"[0-9]+", text) is None: raise OfferError("demand_readback_invalid")
        result[key] = int(text)
    if set(result) != set(DEMAND_LABELS.values()): raise OfferError("demand_readback_invalid")
    return result


def _profile(page: Any, product: Mapping[str, Any], avatar: Path, apply: bool) -> dict[str, Any]:
    expected = product["seller_profile"]; path = expected["public_path"]
    response = page.goto(ORIGIN + path, wait_until="domcontentloaded", timeout=20_000)
    if response is None or response.status != 200 or urlsplit(str(page.url)).path != path: raise OfferError("profile_readback_invalid")
    subtitles = {" ".join(text.split()) for text in page.locator(".p-profile-media__sub-title-link").all_inner_texts() if text.strip()}
    descriptions = page.locator("p.p-profile-introduction__text")
    if len(subtitles) != 1 or descriptions.count() != 1: raise OfferError("profile_readback_invalid")
    text_aligned = subtitles == {" ".join(expected["subtitle"].split())} and " ".join(descriptions.inner_text().split()) == " ".join(expected["description"].split())
    page.goto(ORIGIN + "/mypage", wait_until="networkidle", timeout=30_000)
    completion = page.locator(".js-regularRankCheckPercent")
    if completion.count() != 1: raise OfferError("profile_readback_invalid")
    score = completion.get_attribute("data-score") or ""
    if re.fullmatch(r"[0-9]+", score) is None: raise OfferError("profile_readback_invalid")
    photo_missing = page.get_by_role("link", name="プロフィール写真を登録", exact=True).count() == 1
    aligned = text_aligned and not photo_missing
    if aligned or not apply: return {"profile_aligned": aligned, "profile_photo_aligned": not photo_missing, "profile_completion_percent": int(score), "profile_effect_count": 0}
    page.goto(ORIGIN + "/mypage/profile", wait_until="domcontentloaded", timeout=20_000)
    if urlsplit(str(page.url)).path != "/mypage/profile": raise OfferError("profile_form_changed")
    _field(page, "#UserProfileSubTitle").fill(expected["subtitle"]); _field(page, "#UserProfileDescription").fill(expected["description"])
    if photo_missing:
        if not avatar.is_file() or avatar.stat().st_size > 3_000_000 or avatar.suffix.lower() not in {".jpg", ".jpeg", ".png"}: raise OfferError("profile_avatar_invalid")
        _field(page, "#UserProfileimage\\[\\]").set_input_files(str(avatar))
    invalid = page.locator("#UserProfileDescription").evaluate("""field => [...field.form.elements].filter(element => element.willValidate && !element.checkValidity()).map(element => ({id:element.id, empty:element.value === ""}))""")
    expected_invalid = {f"UserTimechargeRate{index}{field}" for index in range(1, 5) for field in ("Title", "UnitPrice")}
    if not isinstance(invalid, list) or {str(item.get("id")) for item in invalid if isinstance(item, Mapping) and item.get("empty") is True} != expected_invalid or len(invalid) != len(expected_invalid): raise OfferError("profile_form_changed")
    for field_id in expected_invalid: page.locator(f"#{field_id}").evaluate("element => element.required = false")
    save = page.get_by_role("button", name="保存する", exact=True)
    if save.count() != 1: raise OfferError("profile_form_changed")
    try:
        with page.expect_response(lambda value: value.request.method == "POST" and urlsplit(value.url).path == "/mypage/profile", timeout=20_000) as saved: save.click(force=True, timeout=20_000)
    except Exception: raise OfferError("profile_submission_uncertain") from None
    if saved.value.status not in {200, 302}: raise OfferError("profile_submission_uncertain")
    observed = _profile(page, product, avatar, False)
    if not observed["profile_aligned"]: raise OfferError("profile_submission_uncertain")
    return observed | {"profile_effect_count": 1}


def _field(page: Any, selector: str, *, context: str | None = None) -> Any:
    """Return the single element `selector` resolves to, or fail loudly about which one.

    Sibling of _step() -- same defect, same fix. _step() said only "form_changed" until its own
    fix routed it through dom_contract.exactly_one (see _step's docstring); this function sat
    right next to it raising the identical bare "form_changed" for every one of _apply()'s,
    the profile flow's, the settings flow's, the portfolio flow's and _fill_create_form()'s field
    lookups, discarding the selector, the match count and the page every single time. A wake that
    hit this path had nothing to repair from but "some field somewhere stopped matching once."

    Now routed through the same dom_contract.exactly_one (see _reach_dom_contract) _step() uses,
    with the same page-identity observer (_page_identity), so a refusal here leaves the same
    selector/count/page evidence in dom-contract-failures.jsonl instead of nothing.

    `context` names which call site is asking, exactly as _step()'s own `context` does -- e.g.
    _fill_create_form() passes one per field so a create-path failure both names the field that
    broke and reads differently from a bare (context-less) _apply()/profile/settings/portfolio
    failure. Every existing call site omits it, so their behaviour -- what they select, what they
    raise -- is unchanged; only the message a refusal carries is richer than the bare
    "form_changed" they always raised.
    """
    dom_contract = _reach_dom_contract()
    field = page.locator(selector)
    try:
        dom_contract.exactly_one(
            field, platform="lancers", evidence_dir=_EVIDENCE_DIR,
            selector=selector, observe=lambda: _page_identity(page),
        )
    except dom_contract.DomContractError as error:
        suffix = f": {context}" if context else ""
        raise OfferError(f"form_changed{suffix}: selector={selector!r} found={error.found}") from None
    return field


def _setting_status(page: Any, listing_id: str) -> str:
    path = f"/myplan/{listing_id}/setting"
    try: page.goto(ORIGIN + path, wait_until="domcontentloaded", timeout=20_000)
    except Exception: raise OfferError("setting_readback_unavailable") from None
    if urlsplit(str(page.url)).path != path: raise OfferError("setting_route_invalid")
    fields = page.locator('[name="data[ProjectPlanStatusForm][status]"]')
    if fields.count() != 3: raise OfferError("setting_readback_invalid")
    checked = [fields.nth(index).get_attribute("value") for index in range(3) if fields.nth(index).is_checked()]
    if len(checked) != 1 or checked[0] not in {"active", "paused", "archived"}: raise OfferError("setting_readback_invalid")
    return str(checked[0])


def _reconcile_superseded(page: Any, listing_ids: Sequence[str]) -> dict[str, Any]:
    visible = [listing_id for listing_id in listing_ids if _setting_status(page, listing_id) != "archived"]
    if not visible: return {"superseded_visible_count": 0, "status_effect_count": 0}
    listing_id = visible[0]
    if _setting_status(page, listing_id) == "archived": return {"superseded_visible_count": len(visible) - 1, "status_effect_count": 0}
    archived = page.locator('label[for="ProjectPlanStatusFormStatusArchived"]')
    if archived.count() != 1: raise OfferError("setting_status_control_missing")
    archived.click(timeout=5_000)
    if not _field(page, '[name="data[ProjectPlanStatusForm][status]"][value="archived"]').is_checked(): raise OfferError("setting_status_selection_failed")
    save = page.get_by_role("button", name="保存", exact=True)
    if save.count() != 1: raise OfferError("setting_form_changed")
    observed: list[dict[str, Any]] = []
    page.on("response", lambda response: observed.append({"method": response.request.method, "path": urlsplit(response.url).path, "status": response.status}) if response.request.method != "GET" and urlsplit(response.url).hostname == "www.lancers.jp" else None)
    try: save.click(force=True, no_wait_after=True, timeout=5_000)
    except Exception: raise OfferError("setting_submission_uncertain") from None
    page.wait_for_timeout(2_000)
    if _setting_status(page, listing_id) != "archived":
        print("storefront_offer:non_get=" + json.dumps(observed, separators=(",", ":")), file=sys.stderr)
        raise OfferError("setting_submission_uncertain")
    return {"superseded_visible_count": len(visible) - 1, "status_effect_count": 1, "hidden_listing_id": listing_id, "responses": observed}


def _write_receipt(state_path: Path, product: Mapping[str, Any], demand: Mapping[str, int]) -> None:
    """Persist this single-offer listing's receipt into listing.json's own fixed top-level
    fields (record_type/schema_version/.../observed_at below -- the only keys this writer has
    ever owned).

    Read-modify-write against the whole file (via _read_state_json/_atomic_write_json below),
    never a fresh dict built from scratch: the old shape overwrote the entire file with only
    these fields, silently discarding anything a sibling writer had put elsewhere in it -- in
    production, _write_catalog_listing's own "catalog_listings" map, wiped one wake after a
    real listing (https://www.lancers.jp/menu/detail/1342218) was recorded there. This is
    verbatim the Apply lane guide's fault 10 (marketplace-apply-lane.md): "The state writer
    keeps a fixed field list. Anything a lane attaches to a claim that is not on that list is
    dropped on the next write, silently." Preservation here is wholesale -- every key already in
    the file survives untouched except the ones this writer overwrites with its own current
    values -- deliberately not an allowlist of "known extra keys to keep"; an allowlist of two
    known keys is exactly the shape that looks fixed the moment a third writer shows up.
    """
    path = state_path.with_name("listing.json")
    digest = hashlib.sha256(json.dumps(product, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    existing = _read_state_json(path)
    existing.update({"record_type": "listing_receipt", "schema_version": 1, "platform": "lancers", "product_id": product["product_id"], "product_version": product["product_version"], "listing_external_id": product["listing_external_id"], "public_url": f"{ORIGIN}/menu/detail/{product['listing_external_id']}", "status": "published", "content_sha256": digest, "idempotency_key": f"lancers:listing:{product['product_id']}:v{product['product_version']}", "demand": dict(demand), "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
    _atomic_write_json(path, existing)


def _read_state_json(path: Path) -> dict[str, Any]:
    """The whole of one state file (listing.json) as a dict, or {} when missing, unreadable, or
    not a JSON object -- "nothing here yet" is not an error. Shared by every writer of
    listing.json (_write_receipt, _write_catalog_listing) so a read-modify-write always starts
    from the exact same file, not a re-derived guess at what "empty" means."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write `value` to `path` with the tempfile-then-replace, 0600-permission pattern every
    writer of listing.json already used individually -- one implementation of "one file, one
    write discipline", shared by _write_receipt and _write_catalog_listing rather than
    duplicated (and free to drift) in each."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle: json.dump(dict(value), handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")); handle.write("\n")
        os.replace(temporary, path); path.chmod(0o600)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _page_identity(page: Any) -> dict[str, Any]:
    """The page's own identity at the moment of a _step() refusal: url always, title when the
    page can still answer for itself. dom_contract.py's own docstring names exactly why this
    matters -- "a selector alone cannot say 'you were on the login page', which is how an expired
    session reads as a markup change." title is read defensively so a page that cannot answer
    (already navigating, already closed) still reports its url rather than nothing at all.
    """
    try:
        title = page.title()
    except Exception:
        title = None
    return {"url": str(getattr(page, "url", None)), "title": title}


class _VisibleTextMatches:
    """Adapter letting dom_contract.exactly_one judge _step()'s own "exactly one *visible* match"
    contract without changing what counts as a match.

    _step() has always computed this itself: every element whose own text equals `label`
    (Lancers' wizard keeps every step's markup in the DOM at once -- see the module comment above
    _CREATE_STEP_FIELDS -- so a hidden step can share a visible one's label), kept only if
    is_visible() is true. `.count()` here is exactly that pre-filtered length, so dom_contract
    sees precisely the same "found" number _step() always enforced -- neither looser nor
    stricter than the matcher this replaces.
    """

    def __init__(self, items: list[Any], label: str) -> None:
        self._items, self._label = items, label

    def count(self) -> int:
        return len(self._items)

    def click(self, **kwargs: Any) -> None:
        self._items[0].click(**kwargs)

    def __str__(self) -> str:
        return f"get_by_text({self._label!r}, exact=True, visible-only)"


def _step(page: Any, label: str, *, context: str | None = None) -> None:
    """Click the single visible element whose own text equals `label`.

    Matching is unchanged: still exactly one element, among every match for `label`, whose
    is_visible() is true -- a label matching twice is still as broken as matching zero times, and
    this still never clicks an ambiguous match. What changes is the refusal: routed through
    dom_contract.exactly_one (see _reach_dom_contract), which records the label, how many visible
    matches were actually seen, and the page's own identity (_page_identity) to
    dom-contract-failures.jsonl before raising -- the fix for the fault
    marketplace-apply-lane.md's "refuse loudly" section names for this exact lane: a bare
    "form_changed" that discarded which of several possible steps it was even looking at, which
    is how "form_changed" alone cost 81 skips in one day on this lane's sibling proposal form
    without naming a single one of the ten selectors it could have been.

    `context` names which call site is asking -- e.g. create_package()'s manual-package chooser
    vs. a step inside _apply()'s edit form -- so the raised error answers "which step" as well as
    "a step failed". Every one of _apply()'s five existing call sites omits it, so their click
    target and control flow are unchanged; only the message a refusal carries is richer than the
    bare "form_changed" they always raised.
    """
    dom_contract = _reach_dom_contract()
    values = [item for item in page.get_by_text(label, exact=True).all() if item.is_visible()]
    try:
        dom_contract.exactly_one(
            _VisibleTextMatches(values, label), platform="lancers", evidence_dir=_EVIDENCE_DIR,
            selector=label, observe=lambda: _page_identity(page),
        )
    except dom_contract.DomContractError as error:
        suffix = f": {context}" if context else ""
        raise OfferError(f"form_changed{suffix}: label={label!r} found={error.found}") from None
    values[0].click()


_SERVICE_TYPE_SELECTOR = '[name="ProjectPlanCategoryForm.service_type[0]"]'


def _select_service_type(page: Any, service_type: str, *, context: str | None = None) -> None:
    """Select 業務 (`_SERVICE_TYPE_SELECTOR`) -- a *radio group*, not a `<select>`: many elements
    share this one `name`, one per option, each identified by its own grandparent element's
    innerText rather than by an `<option>` label (unlike every other dependent field this file
    fills via `select_option(label=...)`). It is also a *dependent* of subcategory exactly the way
    subcategory is a dependent of category: it does not mount into the DOM until subcategory has
    been chosen, so it must be waited for with `state="attached"`, never assumed present at page
    load.

    Shared by _apply() (this selection proved correct against the live listing) and
    _fill_create_form() (create_package()'s wizard) -- there is exactly one implementation of
    "select this control" in this file. The create path used to carry its own `<select>`-shaped
    version (`_select_create_service_type`); treating a radio group as a `<select>` is exactly why
    it drifted from what the live DOM actually is and raised `found=0` against a control that was
    never a `<select>` to begin with.

    Selection is four steps, each checked before the next is attempted -- the same order _apply()
    already proved live: (1) wait for the radio group to attach, then keep exactly the one radio
    whose grandparent's text equals `service_type`; (2) confirm its `value` is Lancers' own
    numeric category id, not a placeholder; (3) click the matching `label[for=<value>]` -- the
    radio's own input is not the real click target -- and wait for the live category API this
    selection triggers (`/v1/project_store_api/project_category/<id>`); (4) confirm that response
    was 200 and the radio actually ended up checked. Any refusal names `service_type`, what was
    actually seen, and `context` (see _field()'s own docstring for the convention) rather than a
    bare "form_changed".
    """
    suffix = f": {context}" if context else ""
    page.wait_for_selector(_SERVICE_TYPE_SELECTOR, state="attached", timeout=5_000)
    radios = page.locator(_SERVICE_TYPE_SELECTOR)
    labelled = [(radio, " ".join(radio.evaluate("e => e.parentElement.parentElement.innerText").split())) for radio in radios.all()]
    matches = [radio for radio, label in labelled if label == service_type]
    if not matches:
        seen = [label for _, label in labelled]
        raise OfferError(f"form_changed{suffix}: service_type={service_type!r} found=0 options={seen}")
    if len(matches) > 1:
        raise OfferError(f"form_changed{suffix}: service_type={service_type!r} found={len(matches)}")
    radio = matches[0]
    value = radio.get_attribute("value")
    if re.fullmatch(r"[0-9]+", value or "") is None:
        raise OfferError(f"form_changed{suffix}: service_type={service_type!r} value={value!r}")
    with page.expect_response(lambda response: urlsplit(response.url).path == f"/v1/project_store_api/project_category/{value}", timeout=10_000) as service_loaded:
        page.locator(f'label[for="{value}"]').click()
    if service_loaded.value.status != 200:
        raise OfferError(f"form_changed{suffix}: service_type={service_type!r} api_status={service_loaded.value.status}")
    if not radio.is_checked():
        raise OfferError(f"form_changed{suffix}: service_type={service_type!r} checked=False")


def _apply(page: Any, product: Mapping[str, Any], image: Path) -> dict[str, Any]:
    before = _public(page, product)
    reconciliation = _reconcile_superseded(page, product["superseded_listing_ids"])
    if reconciliation["status_effect_count"]: return before | reconciliation | {"action": "hidden_superseded"}
    if before["aligned"]: return before | reconciliation | {"action": "unchanged"}
    listing_id = product["listing_external_id"]; edit_url = f"{ORIGIN}/myplan/{listing_id}/edit"
    page.goto(edit_url, wait_until="domcontentloaded", timeout=30_000)
    if page.url != edit_url: raise OfferError("edit_route_invalid")
    page.wait_for_selector('[name="ProjectPlanForm.title"]', state="visible", timeout=5_000)
    if before["mismatched_fields"] == ["title"]:
        _field(page, '[name="ProjectPlanForm.title"]').fill(product["title_stem"])
        _step(page, "保存"); page.wait_for_url(f"**/myplan/{listing_id}/edit/complete", timeout=30_000)
        try: return _public(page, product) | reconciliation | {"action": "updated", "changed_field": "title"}
        except OfferError as error: raise OfferError(f"publication_uncertain: {error}") from error
    _field(page, '[name="ProjectPlanForm.title"]').fill(product["title_stem"])
    _field(page, '[name="ProjectPlanForm.subtitle"]').fill(product["subtitle"])
    _field(page, '[name="___main_category_id"]').select_option(label=product["category"])
    page.wait_for_function("label => [...document.querySelectorAll('[name=\"ProjectPlanForm.project_category_id\"] option')].some(o => o.textContent.trim() === label)", arg=product["subcategory"], timeout=5_000)
    _field(page, '[name="ProjectPlanForm.project_category_id"]').select_option(label=product["subcategory"])
    _select_service_type(page, product["service_type"])
    _field(page, '[name="ProjectPlanForm.industry_type_id"]').select_option(label=product["industry"])
    while page.locator('[aria-label="削除"]').count(): page.locator('[aria-label="削除"]').first.click()
    tag_field = _field(page, '[name="MultiSelectTagSearch_ProjectPlanTagForm"]')
    for tag in product["tags"]: tag_field.fill(tag); tag_field.press("Enter")
    _step(page, "料金表")
    for index, plan in enumerate(product["plans"]):
        prefix = f"ProjectPlanMenuForm[{index}]"
        _field(page, f'[name="{prefix}.description"]').fill(plan["description"])
        _field(page, f'[name="{prefix}.delivery_time"]').select_option(str(plan["delivery_days"]))
        _field(page, f'[name="{prefix}.price"]').fill(str(plan["price_jpy"]))
    _step(page, "業務内容"); _field(page, "textarea:not([name])").fill(product["description"])
    _step(page, "確認事項"); _field(page, '[name="ProjectPlanForm.notice_for_sale"]').fill(product["notice"])
    _step(page, "画像ほか")
    uploads = page.locator('input[type="file"][accept*="image/"]')
    existing = [field for field in uploads.all() if field.evaluate("e => !!e.parentElement?.querySelector('img[src*=\"img2.lancers.jp/projectblob/\"]')")]
    if len(existing) != 1: raise OfferError("form_changed")
    upload = existing[0]
    with page.expect_response(lambda response: urlsplit(response.url).path == "/v1/project_store_api/project_blob/add", timeout=20_000) as uploaded:
        upload.set_input_files(str(image))
    if uploaded.value.status != 200: raise OfferError("image_upload_failed")
    page.wait_for_selector('img[src*="img2.lancers.jp/projectblob/"]', state="visible", timeout=5_000)
    save = page.get_by_role("button", name="保存する", exact=True)
    if save.count() != 1: raise OfferError("form_changed")
    save.click(); page.wait_for_url(f"**/myplan/{listing_id}/edit/complete", timeout=30_000)
    try: return _public(page, product) | reconciliation | {"action": "updated"}
    except OfferError as error: raise OfferError(f"publication_uncertain: {error}") from error


# --- Package creation (/myplan/add?type=manual) --------------------------------------------
# _apply() can only edit a package that already carries a listing_external_id: it goes straight
# to /myplan/{listing_id}/edit. There was no path from the shared catalogue (twenty families,
# skills/_shared/marketplace-core/scripts/listing_catalog.py) to a *new* Lancers package -- one
# hand-authored listing was all this lane could ever produce. create_package() is that path,
# reached from the three-way chooser at /myplan/add by clicking the manual option. It reuses
# _field, _step, _public and OfferError exactly as _apply() does; _apply() itself is untouched.
_CREATE_ADD_URL = f"{ORIGIN}/myplan/add"
_CREATE_MANUAL_URL = f"{ORIGIN}/myplan/add?type=manual"
_CREATE_MANUAL_BUTTON_TEXT = "手動でパッケージを作成する"
# The live DOM read (see the task this shipped from) did not identify which control actually
# publishes -- only that "プレビュー" and several "のコツ" toggles are also present. Rather than
# hardcode a guess, the submit control is discovered by matching a clickable census control's
# accessible name (see _create_click_census / form_observer.clickable_accessible_names -- text,
# aria-label, title, or value) against every label a Lancers form has been observed to use for
# "move this listing forward" elsewhere in this file (_apply uses "保存"/"保存する"; the create
# chooser flow is known to use "確認画面へ"/"公開する"/"公開" for its multi-step forms).
# _create_submit_control fails loudly, naming every control actually present, if zero or more
# than one match.
# "送信" was added after a live wake dumped this exact form's visible buttons (戻る/下書き保存/
# 次へ/閉じる/キャンセル/送信) while diagnosing 画像ほか's missing 次へ -- observed on this form,
# not guessed. Every other label above predates that dump and remains unobserved on this form.
# A later wake found the true advance control on this same step was a single visible <button>
# with *empty* text (create_submit_control_missing: buttons=['']) -- the innerText-only, <button>-
# only search could see that it existed but not what it was. This is why the search now reaches
# every plausibly-clickable tag and every accessible-name source (see the module comment above
# _click_create_next_button), not only <button> and not only text.
_CREATE_SUBMIT_LABELS = ("確認画面へ", "公開する", "公開", "保存する", "保存", "送信")
# Wherever Lancers lands after a successful create, its path carries the new listing's numeric
# id under /myplan/<id>/... or /menu/detail/<id> -- every other Lancers route this file already
# reads (_apply's edit_url, _public's public_url, _setting_status's setting path) uses one of
# those two shapes. No third shape has been observed, so none is guessed.
_CREATE_LISTING_ID_IN_URL = re.compile(r"^https://www\.lancers\.jp/(?:myplan|menu/detail)/([0-9]+)")
# Exactly the fields _fill_create_form() below actually reads from `product`. Kept separate from
# _validate_product's full contract (which also demands an existing listing_external_id, an
# on-disk image, an avatar, portfolio blocks, etc. -- all _apply()/edit concerns a not-yet-created
# package cannot satisfy) so create_package() can fail closed on exactly what it needs, before
# ever opening a page. "description" feeds 業務内容 (the wizard's third step, see below) -- the
# catalogue's own project_lancers() already returns it, so the shared 2000-char cap that step's
# textarea enforces is checked here too, not discovered live as a submission failure.
# "service_type" (業務, ProjectPlanCategoryForm.service_type[0]) is the seventh required control a
# DOM-outward requirement enumeration found and five earlier rounds did not: it is a *dependent*
# radio group that only mounts once subcategory is chosen, exactly like subcategory itself is a
# dependent of category -- see _select_service_type (shared with _apply(), which proved this
# selection live) for how _fill_create_form selects it below.
_CREATE_REQUIRED_FIELDS = ("title_stem", "subtitle", "category", "subcategory", "service_type", "industry", "tags", "notice", "plans", "description")
_CREATE_DESCRIPTION_MAX_LENGTH = 2000


def _require_create_fields(product: Mapping[str, Any]) -> None:
    for field in _CREATE_REQUIRED_FIELDS:
        value = product.get(field)
        if field == "plans":
            if not isinstance(value, list) or len(value) != 3: raise OfferError(f"create_field_missing: {field}")
            continue
        empty = value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, tuple)) and not value)
        if empty: raise OfferError(f"create_field_missing: {field}")
    description = product["description"]
    if len(description) > _CREATE_DESCRIPTION_MAX_LENGTH:
        raise OfferError(f"create_field_invalid: description: length={len(description)} max={_CREATE_DESCRIPTION_MAX_LENGTH}")


def _select_delivery_time(page: Any, selector: str, delivery_days: int, *, context: str | None = None) -> None:
    """Select the option whose *label* names `delivery_days`, never a neighbouring value.

    The Lancers projection (listing_catalog.project_lancers) already rounds every catalogue
    tier up to a day count Lancers is known to offer, so a real mismatch here means the form
    itself changed shape -- that must stop the wake loudly, not silently pick the closest
    option.

    `context` is forwarded to `_field()` unchanged (see that function's own docstring) so a
    _fill_create_form() caller's field name (e.g. which plan index) survives into this
    selector's own ambiguity refusal.
    """
    field = _field(page, selector, context=context)
    seen: list[tuple[str, str]] = []
    for option in field.locator("option").all():
        label = " ".join(str(option.inner_text() or "").split())
        value = option.get_attribute("value") or ""
        seen.append((label, value))
        match = re.fullmatch(r"([0-9]+)\s*日", label)
        if match is not None and int(match.group(1)) == delivery_days:
            field.select_option(value=value)
            return
    raise OfferError(f"create_delivery_time_unmatched: delivery_days={delivery_days}: options={seen}")


# The live DOM read (see the task this shipped from) showed /myplan/add?type=manual is not one
# flat form: it is a six-step wizard -- 基本情報 / 料金表 / 業務内容 / 確認事項 / 画像ほか / 公開,
# captioned 「ステップを選択して移動できます」. Every step's fields sit in the DOM at once; every
# non-current step is wrapped in a div whose CSS-module class matches `_hidden_<hash>` -- a
# build-generated hash this file never hardcodes. What actually matters is field *visibility*:
# a hidden step's fields still resolve (Locator.count()==1) but have a zero-size bounding box,
# which is exactly what the original bug looked like -- a 30s Locator.fill timeout with no
# explanation, because the plan textarea it was filling belonged to a step that was never
# reached. Everything below drives off visibility and advances one step at a time with 「次へ」,
# verifying arrival before the next field is ever touched.
_CREATE_NEXT_BUTTON_TEXT = "次へ"
# The one piece of 画像ほか copy this file can assert on without guessing a selector: it is quoted
# verbatim in the live DOM read. Unlike the four file inputs themselves (upload widgets commonly
# style the native <input type=file> invisible even while "current"), marketing copy sitting in
# an otherwise plain step reliably has a real bounding box, so it is what proves arrival here.
_CREATE_IMAGE_STEP_MARKER_TEXT = "受注率が約10倍になります"


# --- Stall evidence -------------------------------------------------------------------------
# The original _create_step_validation_text scraped every visible [class*='error'] element and
# joined whatever text it found. That net is wide enough to catch the stepper's own step-nav
# chrome -- observed live as `create_step_stalled: 基本情報: 基本情報`, where the "validation
# text" was just the 基本情報 tab re-scraped, not a complaint about anything. A strict matcher
# that discards what it saw is exactly the fault marketplace-apply-lane.md's "refuse loudly"
# section names; the fix is not a looser matcher, it is a *reported* one: name every field the
# stalled step owns, prefer real validation markup over incidental "error"-classed chrome, and
# say plainly when nothing qualifies rather than emit a nearby string. Nothing below infers a
# cause -- every value is read straight off the page for a human or the next wake to conclude
# from.

# One entry per wizard step naming the fields that step owns, as (label, selector) pairs -- the
# same selectors _fill_create_form() already fills, kept here as the single source of what
# "belongs to this step" means so a stall report and the fill order can never drift apart. 画像ほか
# and 公開 own no field whose emptiness is diagnostic (the upload is optional; 公開 has no input),
# so they carry none.
_CREATE_STEP_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "基本情報": (
        ("title", '[name="ProjectPlanForm.title"]'),
        ("subtitle", '[name="ProjectPlanForm.subtitle"]'),
        ("category", '[name="___main_category_id"]'),
        ("subcategory", '[name="ProjectPlanForm.project_category_id"]'),
        ("industry", '[name="ProjectPlanForm.industry_type_id"]'),
        ("tags", '[name="MultiSelectTagSearch_ProjectPlanTagForm"]'),
    ),
    "料金表": tuple(
        (f"plan[{index}].{field}", f'[name="ProjectPlanMenuForm[{index}].{field}"]')
        for index in range(3)
        for field in ("description", "delivery_time", "price")
    ),
    "業務内容": (("description", "textarea:not([name])"),),
    "確認事項": (("notice", '[name="ProjectPlanForm.notice_for_sale"]'),),
    "画像ほか": (),
    "公開": (),
}
# The six step names themselves -- excluded from validation-message candidates because the
# observed bug is precisely a stepper/heading element being mistaken for a complaint.
_CREATE_STEP_NAMES = ("基本情報", "料金表", "業務内容", "確認事項", "画像ほか", "公開")
_CREATE_STALL_PAYLOAD_MAX_CHARS = 4000
_CREATE_STALL_TRUNCATION_MARKER = "...(truncated)"


def _is_create_stepper_chrome(text: str) -> bool:
    return text.strip() in _CREATE_STEP_NAMES


def _create_selected_option(field: Any) -> tuple[str | None, str | None]:
    """Read a <select>'s currently-checked option (label, value). `option:checked` is native
    CSS -- a browser always has exactly one option selected, the first one by default when
    nothing has been explicitly chosen, which is what lets an empty-but-present select read as
    "the placeholder" rather than as absent."""
    try:
        checked = field.locator("option:checked")
        if checked.count() != 1: return None, None
        option = checked.all()[0]
        label = " ".join(str(option.inner_text() or "").split())
        value = option.get_attribute("value") or ""
        return label, value
    except Exception:
        return None, None


def _create_field_state(page: Any, name: str, selector: str) -> dict[str, Any]:
    """Observed state of one field the current step owns: identifier, whether it is present at
    all, and either its selected option's label (selects) or whether it holds a value (text
    inputs/textareas). Never raises -- a field this can't read is reported absent, not fatal,
    because the whole point of this function is to keep going and report everything it can."""
    field = page.locator(selector)
    try:
        count = field.count()
    except Exception:
        return {"field": name, "present": False}
    if count != 1:
        return {"field": name, "present": False, "count": count}
    try:
        visible = field.is_visible()
    except Exception:
        visible = None
    try:
        option_count = field.locator("option").count()
    except Exception:
        option_count = 0
    if option_count:
        label, value = _create_selected_option(field)
        return {"field": name, "present": True, "type": "select", "selected_label": label, "filled": bool(value and value.strip()), "visible": visible}
    try:
        value = field.input_value()
    except Exception:
        value = None
    return {"field": name, "present": True, "type": "text", "filled": bool(value and str(value).strip()), "visible": visible}


def _create_tag_widget_state(page: Any) -> dict[str, Any]:
    """The tag autocomplete (MultiSelectTagSearch_ProjectPlanTagForm) is filled with fill()+Enter
    on an autocomplete widget, which can leave the typed text unregistered as a real tag -- one
    of the named candidate causes for this stall. `[aria-label="削除"]` is already this file's own
    observed selector for a committed tag's remove button (see _apply's tag-clearing loop above),
    reused here rather than guessed, so the report can tell "typed but never committed" apart
    from "genuinely empty" apart from "committed but the field itself reads empty"."""
    try:
        committed = page.locator('[aria-label="削除"]').count()
    except Exception:
        committed = None
    try:
        typed = _field(page, '[name="MultiSelectTagSearch_ProjectPlanTagForm"]').input_value()
    except Exception:
        typed = None
    return {"committed_tag_count": committed, "typed_value_present": bool(typed and str(typed).strip())}


def _create_validation_messages(page: Any) -> list[str]:
    """Real validation messages only, in preference order: aria-invalid="true" elements (plus
    whatever describes them via aria-describedby/aria-label), then [role="alert"], then any
    element whose own class marks it an error message. Each tier is tried only if the one before
    it found nothing. Every candidate that equals a step name verbatim is excluded -- that
    exclusion is the fix for the exact bug this shipped from, where the step heading was scraped
    as if it were a complaint. Finding nothing at any tier is reported as
    "no_validation_message_found", never as a nearby string standing in for "found nothing"."""
    messages = _create_aria_invalid_messages(page)
    if not messages:
        messages = _create_role_alert_messages(page)
    if not messages:
        messages = _create_error_class_messages(page)
    return messages or ["no_validation_message_found"]


def _create_describing_text(page: Any, node: Any) -> str:
    try:
        described_by = node.get_attribute("aria-describedby")
    except Exception:
        described_by = None
    if described_by:
        for target_id in described_by.split():
            try:
                described = page.locator(f"#{target_id}")
                if described.count() == 1:
                    text = " ".join(str(described.inner_text() or "").split())
                    if text: return text
            except Exception:
                continue
    try:
        label = node.get_attribute("aria-label")
    except Exception:
        label = None
    if label and label.strip(): return label.strip()
    try:
        return " ".join(str(node.inner_text() or "").split())
    except Exception:
        return ""


def _create_aria_invalid_messages(page: Any) -> list[str]:
    try:
        nodes = page.locator('[aria-invalid="true"]').all()
    except Exception:
        return []
    messages: list[str] = []
    for node in nodes:
        try:
            if not node.is_visible(): continue
        except Exception:
            continue
        text = _create_describing_text(page, node)
        if text and not _is_create_stepper_chrome(text): messages.append(text)
    return messages


def _create_role_alert_messages(page: Any) -> list[str]:
    try:
        nodes = page.locator('[role="alert"]').all()
    except Exception:
        return []
    messages: list[str] = []
    for node in nodes:
        try:
            if not node.is_visible(): continue
            text = " ".join(str(node.inner_text() or "").split())
        except Exception:
            continue
        if text and not _is_create_stepper_chrome(text): messages.append(text)
    return messages


def _create_error_class_messages(page: Any) -> list[str]:
    try:
        nodes = page.locator("[class*='error']").all()
    except Exception:
        return []
    messages: list[str] = []
    for node in nodes:
        try:
            if not node.is_visible(): continue
            text = " ".join(str(node.inner_text() or "").split())
        except Exception:
            continue
        if text and not _is_create_stepper_chrome(text): messages.append(text)
    return messages


_CREATE_OUTER_HTML_MAX_CHARS = 300


def _truncate_hard(value: Any, max_chars: int) -> str | None:
    """A plain, hard character-count truncation -- not JSON-size-aware like
    _bounded_create_stall_payload (which bounds the whole serialized payload); this bounds one
    string field before it ever reaches that pass, so one long outerHTML never crowds out every
    other field in the report."""
    if not isinstance(value, str):
        return None
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + _CREATE_STALL_TRUNCATION_MARKER


def _create_advance_control_state(page: Any) -> dict[str, Any]:
    """Whether 次へ was found, by exact visible text (independent of _click_create_next_button's
    own clickable-control census -- this is _create_step_evidence's own read, for a step that
    stalled *after* a successful click, not a click-target search), and its text and disabled
    state when found -- so "control missing" and "control present but disabled" read as different
    observations, not the same failure. Also the control's own outerHTML, hard-truncated: its
    tag, type and attributes show at a glance whether it is a real submit control, a bare <span>,
    or something else entirely.
    """
    try:
        matches = [item for item in page.get_by_text(_CREATE_NEXT_BUTTON_TEXT, exact=True).all() if item.is_visible()]
    except Exception:
        return {"found": False}
    if len(matches) != 1: return {"found": False, "count": len(matches)}
    control = matches[0]
    try:
        text = " ".join(str(control.inner_text() or "").split())
    except Exception:
        text = None
    try:
        disabled = control.get_attribute("disabled") is not None or control.get_attribute("aria-disabled") == "true"
    except Exception:
        disabled = None
    try:
        outer_html = control.evaluate("el => el.outerHTML")
    except Exception:
        outer_html = None
    return {"found": True, "text": text, "disabled": disabled, "outer_html": _truncate_hard(outer_html, _CREATE_OUTER_HTML_MAX_CHARS)}


def _bounded_create_stall_payload(payload: dict[str, Any]) -> str:
    """Serialize the stall payload bounded to _CREATE_STALL_PAYLOAD_MAX_CHARS. A wake report and
    a Telegram line both need this bounded, not an unbounded DOM dump. When even the compact form
    does not fit, the payload names itself truncated (a "truncated": true key survives if it
    fits at all) rather than silently dropping content with no notice."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text) <= _CREATE_STALL_PAYLOAD_MAX_CHARS: return text
    marked = dict(payload); marked["truncated"] = True
    text = json.dumps(marked, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(text) <= _CREATE_STALL_PAYLOAD_MAX_CHARS: return text
    budget = _CREATE_STALL_PAYLOAD_MAX_CHARS - len(_CREATE_STALL_TRUNCATION_MARKER)
    return text[:max(budget, 0)] + _CREATE_STALL_TRUNCATION_MARKER


def _create_current_step_name(steps: Mapping[str, Any], fields_report: Sequence[Mapping[str, Any]]) -> str | None:
    """Which step of the observer's step->field map is actually showing, from live evidence
    only -- never from whatever step_name the caller happened to be advancing from (that is
    exactly the "adapter's own idea" this must not fall back to).

    Primary signal: for each step, how many of its own fields the live DOM reports `visible`.
    The step with the most wins -- deliberately a count, not "all fields visible", because a
    single field the wizard hasn't finished mounting yet (the exact stall this shipped from,
    see _create_arrival_field_state) must not make an otherwise-current step invisible to this
    check. Falls back to the observer's own has_wrapper_class flag (the structural fact the
    CSS-module hiding class encodes) only when literally no field anywhere reads as visible --
    e.g. a step whose only owned field is the one still lagging.
    """
    if not steps.get("is_wizard"):
        return None
    visible_by_identifier: dict[Any, bool | None] = {}
    for field in fields_report:
        identifier = field.get("identifier")
        if identifier is not None:
            visible_by_identifier[identifier] = field.get("visible")
    best_name: str | None = None
    best_count = 0
    for step in steps.get("steps", []):
        field_ids = [fid for fid in step.get("fields", []) if fid is not None]
        visible_count = sum(1 for fid in field_ids if visible_by_identifier.get(fid))
        if visible_count > best_count:
            best_count = visible_count
            best_name = step.get("name")
    if best_name is not None:
        return best_name
    not_hidden = [step for step in steps.get("steps", []) if step.get("has_wrapper_class") is False]
    if len(not_hidden) == 1:
        return not_hidden[0].get("name")
    return None


def _create_observer_step_state(page: Any) -> dict[str, Any]:
    """What skills/_shared/marketplace-core/scripts/form_observer.py's platform-neutral wizard
    detector says about this exact live page: which step is actually showing (per
    _create_current_step_name above) and the CSS-module class it inferred the wizard hides
    non-current steps with. This is the fix for the report hole named in the task this shipped
    from: `present`/`filled` alone cannot tell "the click did nothing" apart from "the wizard
    advanced and the arrival selector is wrong" -- both leave every field present and holding
    its value, since every step's fields sit in the DOM at once. `current_step`, derived here
    from live visibility rather than from the step_name argument the caller passed in, is the
    fact that tells those two apart.

    Never raises: an observer exception or a page the observer cannot recognise as a wizard is
    itself reported (observer_error / is_wizard: False), not swallowed -- a diagnostic must
    never become the thing that fails.
    """
    try:
        observer = _reach_form_observer()
        report = observer.observe_page(page)
    except Exception as error:
        return {
            "observer_error": f"{type(error).__name__}: {error}",
            "is_wizard": None,
            "wrapper_class": None,
            "current_step": None,
            "step_requirements": [],
        }
    steps = report.get("steps") or {}
    is_wizard = bool(steps.get("is_wizard"))
    current_step = _create_current_step_name(steps, report.get("fields", [])) if is_wizard else None
    return {
        "observer_error": None,
        "is_wizard": is_wizard,
        "wrapper_class": steps.get("wrapper_class"),
        "current_step": current_step,
        # Every control the observer's own read of the live page marks required or optional
        # (see form_observer.observe_page's step_requirements) -- built from the DOM outward,
        # never from _CREATE_STEP_FIELDS' six known names, so a seventh required control this
        # file has never heard of still shows up here with its visible label. See
        # _create_step_evidence's own docstring for why that gap is the report's whole point.
        "step_requirements": report.get("step_requirements") or [],
    }


def _create_arrival_field_state(arrival: Any, arrival_field: str | None) -> dict[str, Any]:
    """Visibility of the exact locator `_advance_create_step` was waiting for when it gave up --
    the single fact the task naming this report's hole says settles the question on its own:
    still invisible means the click did nothing; visible (on a page that has otherwise advanced)
    means the wizard moved and this specific field is what is lagging."""
    if arrival is None:
        return {"field": arrival_field, "visible": None}
    try:
        visible = arrival().is_visible()
    except Exception:
        visible = None
    return {"field": arrival_field, "visible": visible}


def _create_step_evidence(page: Any, step_name: str, arrival: Any = None, arrival_field: str | None = None) -> str:
    """Everything create_step_stalled can report about why `step_name` did not advance: every
    field that step owns (state 1 above), the best real validation message found (state 2), the
    page URL (state 3, so a silent navigation reads differently from a refusal to advance), the
    advance control's own found/text state (state 4), which step the observer says is actually
    showing plus its inferred wrapper class (state 5 -- see _create_observer_step_state), and the
    visibility of the field `_advance_create_step` was itself waiting for (state 6 -- see
    _create_arrival_field_state). Nothing here is inferred -- every value is read straight off
    the page (or, for state 5/6, off form_observer's own read of the page)."""
    fields = [_create_field_state(page, name, selector) for name, selector in _CREATE_STEP_FIELDS.get(step_name, ())]
    payload: dict[str, Any] = {
        "step": step_name,
        "url": str(getattr(page, "url", None)),
        "fields": fields,
        "validation_messages": _create_validation_messages(page),
        "advance_control": _create_advance_control_state(page),
    }
    if step_name == "基本情報":
        payload["tag_widget"] = _create_tag_widget_state(page)
    payload.update(_create_observer_step_state(page))
    payload["arrival_field"] = _create_arrival_field_state(arrival, arrival_field)
    return _bounded_create_stall_payload(payload)


# get_by_text(label, exact=True) used to resolve 次へ to the element whose OWN text equalled the
# label -- on a real button that is commonly a <span> sitting inside the <button> that actually
# owns the click handler. Clicking that span resolves without error and does nothing, which is
# indistinguishable from a stalled wizard step. A live wake later hit the mirror-image defect on
# 画像ほか's own advance control: create_submit_control_missing: buttons=[''] -- one visible
# <button>, empty text -- because that search only ever looked at <button> elements' own
# innerText, so a control expressed as an <a>, an input[type=submit]/input[type=button], a
# [role="button"] element, or a real <button> named only by an aria-label/title/value was
# invisible to it even when it was the one actually on the page.
#
# Both searches now share one census -- form_observer.clickable_controls (see
# _reach_form_observer) -- covering every shape a "move this listing forward" control has
# actually been observed to take. Each record already resolves to a genuinely interactive
# element, never a bare text node needing an ancestor climb, so a caller matches directly by
# accessible name (form_observer.clickable_accessible_names: text, aria-label, title, value --
# never img_alt, see that function's own docstring) and either gets exactly one control or a
# named failure carrying the *entire* census -- every control's every accessible-name source,
# tag, type, and a hard-truncated outerHTML -- plus the current step and URL. Strictness is
# unchanged: exactly one match or a named failure, never a nearest guess, never a fallback to
# "the only visible control".


def _create_click_census(page: Any) -> list[dict[str, Any]]:
    """The one census of every visible, plausibly-clickable control on the create wizard's live
    page -- reused by every "which control advances this step" search below and by every
    "nothing matched" failure those searches raise into (see the module comment above). Delegates
    entirely to form_observer.clickable_controls (see _reach_form_observer); nothing here
    reimplements what counts as clickable or as visible.
    """
    form_observer = _reach_form_observer()
    return form_observer.clickable_controls(page)


def _create_controls_named(census: Sequence[Mapping[str, Any]], labels: Sequence[str]) -> list[Mapping[str, Any]]:
    """Every census record whose accessible name (text, aria-label, title, or value -- see
    form_observer.clickable_accessible_names) exactly equals one of `labels`. The one place both
    the 次へ search (a single label) and the submit search (_CREATE_SUBMIT_LABELS, several) filter
    the shared census, so a change to what counts as a match is made in exactly one place.
    """
    form_observer = _reach_form_observer()
    wanted = set(labels)
    return [record for record in census if wanted & set(form_observer.clickable_accessible_names(record))]


def _create_click_failure(page: Any, census: Sequence[Mapping[str, Any]]) -> str:
    """Everything a "no control matched" refusal can report: the current step (per
    _create_observer_step_state -- the same fact _create_step_evidence already reports for a
    stalled arrival), the page URL, and the full clickable-control census -- bounded exactly as
    the wizard's own stall payload already is (see _bounded_create_stall_payload), so a census
    large enough to need truncation still says so rather than silently dropping controls.
    """
    form_observer = _reach_form_observer()
    payload = {
        "url": str(getattr(page, "url", None)),
        "step": _create_observer_step_state(page).get("current_step"),
        "controls": [form_observer.clickable_public_record(record) for record in census],
    }
    return _bounded_create_stall_payload(payload)


def _click_create_next_button(page: Any, step_name: str) -> None:
    """Click the single visible census control named 次へ (see the module comment above). A
    missing or ambiguous match is named against the step that could not advance, not as a bare
    "form_changed" -- and carries the full census plus the current step and URL (see
    _create_click_failure), so a live stall answers "what was there instead" in the same wake it
    happened, rather than needing a second wake with a manual dump."""
    census = _create_click_census(page)
    matches = _create_controls_named(census, (_CREATE_NEXT_BUTTON_TEXT,))
    if len(matches) != 1:
        raise OfferError(f"create_step_stalled: {step_name}: next_button_missing: {_create_click_failure(page, census)}")
    matches[0]["_element"].click()


def _advance_create_step(page: Any, step_name: str, arrival: Any, arrival_field: str) -> None:
    """Click 次へ from `step_name` and confirm the next step actually became current.

    `arrival` is a zero-arg callable returning a Locator whose visibility proves the next step
    now shows; `arrival_field` names it for the stall report (see _create_arrival_field_state).
    A step whose click does not produce that visibility -- most likely a validation failure on
    the step just filled -- raises create_step_stalled naming the step and any validation text
    the page is showing, instead of letting the next .fill() time out anonymously against a
    field with a zero-size bounding box (the bug this shipped from).
    """
    _click_create_next_button(page, step_name)
    try:
        arrival().wait_for(state="visible", timeout=10_000)
    except Exception:
        raise OfferError(f"create_step_stalled: {step_name}: {_create_step_evidence(page, step_name, arrival, arrival_field)}") from None


def _create_business_textarea(page: Any) -> Any:
    """Locate 業務内容's field: the live DOM read carries it as the single <textarea> with no
    name attribute anywhere on the six-step page (every step's fields sit in the DOM at once, so
    this count check is structural, not a visibility check -- it holds regardless of which step
    is current). Exactly one match is required; zero or more than one means the form changed
    shape, which must stop the wake loudly rather than guess which textarea to fill.
    """
    field = page.locator("textarea:not([name])")
    count = field.count()
    if count != 1: raise OfferError(f"create_business_textarea_invalid: count={count}")
    return field


def _fill_create_form(page: Any, product: Mapping[str, Any], image: Path) -> dict[str, Any]:
    """Walk the six-step wizard end to end, filling only the current step's fields and never
    advancing until the next step has actually arrived (see the module comment above
    _CREATE_NEXT_BUTTON_TEXT). Leaves the wizard on 公開 -- create_package() still owns
    discovering and clicking the actual submit control there, since that control's label was
    never observed live.

    Returns {"image_attached": bool}: 画像ほか is genuinely optional (the step's own copy says
    任意), so a missing or unusable file input degrades this one field to a result flag instead
    of an OfferError. Every other field this function fills is already required by
    _require_create_fields before create_package() ever opened a page.
    """
    # 1/6 基本情報 -- visible on load. `ProjectPlanForm.project_category_id` (the subcategory)
    # is not in the initial DOM either -- exactly as in _apply(), it appears only once the main
    # category is chosen, so it is waited for by option label the same way _apply() already does.
    _field(page, '[name="ProjectPlanForm.title"]', context="create:title").fill(product["title_stem"])
    _field(page, '[name="ProjectPlanForm.subtitle"]', context="create:subtitle").fill(product["subtitle"])
    _field(page, '[name="___main_category_id"]', context="create:category").select_option(label=product["category"])
    page.wait_for_function(
        "label => [...document.querySelectorAll('[name=\"ProjectPlanForm.project_category_id\"] option')].some(o => o.textContent.trim() === label)",
        arg=product["subcategory"], timeout=5_000,
    )
    _field(page, '[name="ProjectPlanForm.project_category_id"]', context="create:subcategory").select_option(label=product["subcategory"])
    _select_service_type(page, product["service_type"], context="create:service_type")
    _field(page, '[name="ProjectPlanForm.industry_type_id"]', context="create:industry_type").select_option(label=product["industry"])
    tag_field = _field(page, '[name="MultiSelectTagSearch_ProjectPlanTagForm"]', context="create:tags")
    for tag in product["tags"]:
        tag_field.fill(tag); tag_field.press("Enter")
    _advance_create_step(
        page, "基本情報", lambda: page.locator('[name="ProjectPlanMenuForm[0].description"]'),
        "ProjectPlanMenuForm[0].description",
    )

    # 2/6 料金表 -- 「料金は必ず3プラン必要です」, exactly 3 plans (ベーシック/スタンダード/プレミアム).
    for index, plan in enumerate(product["plans"]):
        prefix = f"ProjectPlanMenuForm[{index}]"
        _field(page, f'[name="{prefix}.description"]', context=f"create:plan[{index}].description").fill(plan["description"])
        _select_delivery_time(page, f'[name="{prefix}.delivery_time"]', plan["delivery_days"], context=f"create:plan[{index}].delivery_time")
        _field(page, f'[name="{prefix}.price"]', context=f"create:plan[{index}].price").fill(str(plan["price_jpy"]))
    _advance_create_step(page, "料金表", lambda: _create_business_textarea(page), "業務内容 textarea (unnamed)")

    # 3/6 業務内容 -- the single unnamed textarea, max 2000 chars (already enforced against
    # `product["description"]` by _require_create_fields before this function ever ran).
    _create_business_textarea(page).fill(product["description"])
    _advance_create_step(
        page, "業務内容", lambda: page.locator('[name="ProjectPlanForm.notice_for_sale"]'),
        "ProjectPlanForm.notice_for_sale",
    )

    # 4/6 確認事項 -- 注文時のお願い (必須); 注文時の質問 is optional and unused here.
    _field(page, '[name="ProjectPlanForm.notice_for_sale"]', context="create:notice_for_sale").fill(product["notice"])
    _advance_create_step(
        page, "確認事項", lambda: page.get_by_text(_CREATE_IMAGE_STEP_MARKER_TEXT, exact=False),
        _CREATE_IMAGE_STEP_MARKER_TEXT,
    )

    # 5/6 画像ほか -- 任意. See this function's docstring for why a missing/failed attach only
    # sets a flag rather than raising: every required field already passed _require_create_fields,
    # and this is the one field on the whole page the step's own copy calls optional.
    image_attached = False
    uploads = page.locator('input[type="file"]')
    if uploads.count() >= 1:
        try:
            uploads.nth(0).set_input_files(str(image))
            image_attached = True
        except Exception:
            image_attached = False
    advance = _advance_from_final_content_step(page, _CREATE_FINAL_CONTENT_STEP)

    # 6/6 公開 (if the transition above landed there) -- create_package() still owns discovering
    # and clicking a submit control on whatever step is now showing; see create_package()'s own
    # docstring for how it decides whether that step is even reached.
    return {"image_attached": image_attached} | advance


def _create_submit_control(page: Any) -> Any:
    """The single visible census control whose accessible name (see form_observer.
    clickable_accessible_names -- text, aria-label, title, or value; never img_alt) equals one of
    _CREATE_SUBMIT_LABELS. Reach and matching are exactly _click_create_next_button's own (see
    the module comment above that function) -- one census, the same discipline, a different label
    set. Zero or more than one match raises create_submit_control_missing carrying the full
    census plus the current step and URL (see _create_click_failure); nothing here ever falls
    back to "the only visible control"."""
    census = _create_click_census(page)
    matches = _create_controls_named(census, _CREATE_SUBMIT_LABELS)
    if len(matches) != 1:
        raise OfferError(f"create_submit_control_missing: {_create_click_failure(page, census)}")
    return matches[0]["_element"]


# The wizard's last content step (画像ほか) is the one a live wake found with no 次へ at all --
# create_step_stalled: 画像ほか: next_button_missing (see the task this shipped from). Every
# earlier step (基本情報 through 確認事項) is still driven only by _click_create_next_button, via
# _advance_create_step, which never falls back to anything else: clicking a submit control on one
# of those would publish a half-filled listing. _CREATE_FINAL_CONTENT_STEP names the one step
# allowed to use the dual accept-either path below; _advance_from_final_content_step asserts it is
# only ever called with this step name, so that boundary is enforced in code, not only by
# convention.
_CREATE_FINAL_CONTENT_STEP = "画像ほか"


def _advance_from_final_content_step(page: Any, step_name: str) -> dict[str, Any]:
    """Advance out of the wizard's final content step (画像ほか), accepting either shape the live
    DOM might carry rather than assuming one: a 次へ (exactly like every earlier step), or --
    when no unambiguous 次へ is present -- the submit control _create_submit_control discovers.
    Records which one actually fired as `advanced_via` ("next_button" or "submit_control") rather
    than silently preferring one: if a future build of this form regains a 次へ here, this still
    uses it and says so.

    Only valid for `step_name == _CREATE_FINAL_CONTENT_STEP` -- asserted, not just documented,
    since this dual path is exactly what every earlier step must never get (see the module
    comment above _CREATE_FINAL_CONTENT_STEP): a submit fallback on an earlier step would publish
    a half-filled listing.

    Neither present raises create_step_stalled carrying the full clickable-control census (see
    _create_click_failure) -- the same discipline next_button_missing itself carries, never a
    bare "nothing found". Both the 次へ probe and the submit fallback below share one census read
    (see the module comment above _click_create_next_button): a single live DOM read serves both
    searches and, on failure, the report.
    """
    assert step_name == _CREATE_FINAL_CONTENT_STEP, f"submit fallback is only valid on {_CREATE_FINAL_CONTENT_STEP!r}, got {step_name!r}"
    census = _create_click_census(page)
    next_matches = _create_controls_named(census, (_CREATE_NEXT_BUTTON_TEXT,))
    if len(next_matches) == 1:
        next_matches[0]["_element"].click()
        return {"advanced_via": "next_button"}
    submit_matches = _create_controls_named(census, _CREATE_SUBMIT_LABELS)
    if len(submit_matches) != 1:
        raise OfferError(f"create_step_stalled: {step_name}: advance_control_missing: {_create_click_failure(page, census)}")
    submit_matches[0]["_element"].click(timeout=20_000)
    return {"advanced_via": "submit_control"}


# The submit control _create_submit_control discovers on 画像ほか was never established live to
# either create the listing directly or only advance to a further 公開 step -- see
# _advance_from_final_content_step's own docstring. _await_create_listing_id checks which one
# actually happened without assuming; _require_create_listing_id is the unchanged, full-timeout
# readback contract create_package() always ended on before this task (page.wait_for_url +
# create_listing_id_unresolved), now factored out so both the "submit created it directly" and
# the "submit only advanced to 公開" callers of create_package() share the identical fail-closed
# tail.
_CREATE_LISTING_URL_SETTLE_TIMEOUT_MS = 5_000


def _await_create_listing_id(page: Any) -> str | None:
    """Whether the page has already landed on a created listing's URL, without deciding *how* it
    got there. A short wait_for_url lets an in-flight navigation from the just-clicked submit
    control settle; a timeout here means "not this URL (yet)", not "failed" -- the caller falls
    back to submitting again on whatever step is now showing (see create_package()), which still
    owns the full 30s wait and the fail-closed create_listing_id_unresolved raise via
    _require_create_listing_id."""
    try:
        page.wait_for_url(_CREATE_LISTING_ID_IN_URL, timeout=_CREATE_LISTING_URL_SETTLE_TIMEOUT_MS)
    except Exception:
        pass
    match = _CREATE_LISTING_ID_IN_URL.match(str(page.url))
    return match.group(1) if match is not None else None


def _require_create_listing_id(page: Any) -> str:
    """The full-timeout readback contract: wait up to 30s for the URL to become a created
    listing's, and fail closed -- create_listing_id_unresolved, naming the URL -- if it never
    does. Unchanged from what create_package() always did before this task; only pulled out into
    its own function so both paths that can reach it (submit led straight to a listing vs. submit
    only advanced to 公開 and a second submit was needed) share it verbatim."""
    page.wait_for_url(_CREATE_LISTING_ID_IN_URL, timeout=30_000)
    match = _CREATE_LISTING_ID_IN_URL.match(str(page.url))
    if match is None: raise OfferError(f"create_listing_id_unresolved: url={page.url}")
    return match.group(1)


def create_package(page: Any, product: Mapping[str, Any], image: Path) -> dict[str, Any]:
    """Create a brand-new Lancers package from `product` -- the manual-creation counterpart to
    _apply(), reachable before any listing_external_id exists. One package per call; the caller
    decides when to create and persists the returned listing_external_id, this function does not
    loop over a catalogue and does not decide anything on its own.

    `image` is attached on the wizard's 画像ほか step if a file input is available there (see
    _fill_create_form); it is optional, so its absence never fails this function, only leaves
    `image_attached: False` in the result. The very next _apply() run against the id this
    returns still owns image alignment end to end regardless, so nothing here duplicates it.

    Fails closed at every step: a required field missing from `product` (including a
    description over the 2000-char cap) raises before any navigation; a failure to click the
    chooser's manual option itself raises `form_changed: create_manual_chooser: ...` (see
    _step's `context`), distinct from `create_step_stalled: ...` raised later if a wizard step's
    own 次へ fails to advance -- so the next wake's error line already says which of the two
    happened; landing anywhere other than /myplan/add?type=manual after clicking the manual
    option raises rather than filling a form that cannot be identified; a delivery_time with no
    matching option raises without
    selecting anything; an advance to the next wizard step that does not actually arrive raises
    create_step_stalled naming the step; no single matching submit button raises, naming the
    buttons actually present; and a successful submission whose public page cannot be read back
    is reported as publication_uncertain, never as success.

    The final content step's own exit (see _advance_from_final_content_step) may have already
    been the submission -- whether the discovered control there creates the listing directly or
    only advances to a further 公開 step was never established live, so this never assumes
    either shape. When that step advanced via its submit control, `_await_create_listing_id`
    checks whether the URL already became a listing URL; if it has not, the walk continues to
    whatever step is now showing and submits there instead -- a step transition, not a failed
    creation. When that step advanced via 次へ instead, the submit control is always still ahead
    (on 公開), exactly as before this task.
    """
    _require_create_fields(product)
    page.goto(_CREATE_ADD_URL, wait_until="domcontentloaded", timeout=30_000)
    _step(page, _CREATE_MANUAL_BUTTON_TEXT, context="create_manual_chooser")
    if page.url != _CREATE_MANUAL_URL: raise OfferError(f"create_route_invalid: url={page.url}")
    page.wait_for_selector('[name="ProjectPlanForm.title"]', state="visible", timeout=5_000)
    fill_result = _fill_create_form(page, product, image)
    listing_id = _await_create_listing_id(page) if fill_result.get("advanced_via") == "submit_control" else None
    if listing_id is None:
        submit = _create_submit_control(page)
        submit.click(timeout=20_000)
        listing_id = _require_create_listing_id(page)
    published = dict(product) | {"listing_external_id": listing_id, "public_title": product["title_stem"] + "ます"}
    # require_image mirrors what this exact creation attempt actually did: 画像ほか is optional
    # (see _fill_create_form), so a package that never got a file attached must not be marked
    # mismatched for lacking one, while one that did must still show it. This is a fact of this
    # specific attempt, not of `product` -- see _public()'s own docstring.
    try: return _public(page, published, require_image=bool(fill_result.get("image_attached"))) | {"action": "created", "listing_external_id": listing_id} | fill_result
    except OfferError as error: raise OfferError(f"publication_uncertain: {error}") from error


def run_create(product_path: Path, state_path: Path) -> dict[str, Any]:
    """CLI/tick entry point for create_package(), parallel to run() -- separate on purpose, per
    create_package()'s own contract, so nothing about --apply/--inspect changes and nothing
    starts creating packages by accident. Reuses _product() (the same loader/validator run()
    uses) so the product file still goes through the full listing contract; create_package()
    itself never reads listing_external_id, so whatever placeholder value a not-yet-created
    product file carries there is simply unused.
    """
    tick = browser = page = None; logged_in = False; result: dict[str, Any] = {"ok": False, "error": "offer_unavailable"}
    try:
        product, image, _avatar = _product(product_path); tick = _load("lancers_storefront_create_tick", HERE / "application_tick.py")
        with tick.account_lock(state_path.with_name("work-sync.json")):
            browser = tick._default_browser_factory(tick.CDP_URL); page = tick._new_owned_page(browser)
            if not tick._production_account_ready(page): raise OfferError("account_unavailable")
            logged_in = True; result = create_package(page, product, image)
    except OfferError as error: result = {"ok": False, "logged_in": logged_in, "error": str(error)}
    except Exception as error:
        print(f"storefront_offer_create:{type(error).__name__}: {str(error)[:400]}", file=sys.stderr)
        result = {"ok": False, "logged_in": logged_in,
                  "error": "account_lock_busy" if "LockBusy" in type(error).__name__ else "offer_unavailable",
                  "failure": f"{type(error).__name__}: {str(error)[:200]}"}
    finally:
        try:
            closed = page is None or bool(tick._close_owned_page(page))
            if browser is not None: tick._stop_playwright_runtime(getattr(browser, "_anicca_playwright_runtime", None))
        except Exception: closed = False
        if not closed: result = {"ok": False, "logged_in": logged_in, "error": "cleanup_failed"}
    return result


# --- Catalogue-driven creation (the storefront wake's fallback effect) ---------------------
# create_package() gave this lane a way to reach Lancers with a *new* listing; nothing decided
# *which* listing yet. The twenty-family shared catalogue (skills/_shared/marketplace-core,
# skills/gig-work/profile/listings/catalog.json) is the only inventory this owner already trusts
# for content -- Coconala reads the same rows. select_catalog_family_to_create() below is the
# decision, kept pure and browser-free so a wake never opens a page for a family it will not
# attempt; run_catalog_create() is the one browser-touching effect main() reaches for when the
# existing single-offer chain in run() left nothing to do this wake (result["action"] ==
# "unchanged" -- no status pause, no title/field alignment, no portfolio, no profile update).
#
# Deliberately a second, independent account_lock acquisition rather than something nested
# inside run()'s own `with tick.account_lock(...)` block: fcntl.flock locks an open file
# description, not a process, so a second os.open()+flock() on the same lock path from the same
# process (a different fd) blocks forever waiting for a lock this same process is already
# holding. main() only reaches run_catalog_create() after run()'s own `with` block has already
# exited, so the two acquisitions are sequential, never nested.
_CATALOG_LISTINGS_KEY = "catalog_listings"
# Every product-shape field create_package() actually reads (see _CREATE_REQUIRED_FIELDS) that
# the catalogue itself cannot supply via project_lancers(): platform_overrides.lancers now
# carries category/industry/tags/notice (see the catalogue task this shipped from), but not every
# family yet carries subcategory or service_type -- both are dependent selects (subcategory
# depends on category; service_type depends on subcategory, and its option vocabulary is itself
# per-subcategory) whose values only appear once their parent is chosen in the live form, and
# most subcategories' service_type option lists have never been observed. A family missing any of
# these is named under "skipped", never filled with a guess -- see select_catalog_family_to_create
# below for why a skip there never blocks a later, complete family.
_CATALOG_OVERLAY_FIELDS = ("subcategory", "service_type", "industry", "tags", "notice")


def _catalog_family_order(catalog: Mapping[str, Any]) -> list[str]:
    """The catalogue's own listing order -- never re-sorted, so selection stays deterministic
    across wakes without depending on dict/set iteration order anywhere else in this file."""
    return [str(row["family"]) for row in catalog.get("listings") or () if isinstance(row, Mapping) and row.get("family")]


def _read_catalog_listings(state_path: Path) -> dict[str, Any]:
    """Every catalogue family already recorded as created, keyed by family name.

    Reads the same listing.json _write_receipt already owns, under one additional top-level
    key (_CATALOG_LISTINGS_KEY) -- extending the one file the lane already trusts rather than
    adding a second, parallel store. A missing/unreadable/malformed file reads as "nothing
    published yet", never as an error that blocks selection.
    """
    path = Path(state_path).with_name("listing.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    listings = value.get(_CATALOG_LISTINGS_KEY) if isinstance(value, Mapping) else None
    return dict(listings) if isinstance(listings, Mapping) else {}


def _write_catalog_listing(state_path: Path, family: str, record: Mapping[str, Any]) -> None:
    """Persist `family`'s new listing under listing.json's catalog_listings map.

    Read-modify-writes the whole file via the same _read_state_json/_atomic_write_json pair
    _write_receipt now uses -- preserving the single-offer listing_receipt fields _write_receipt
    owns, every other family already recorded, and any other key either writer does not
    recognise, wholesale. Was already this direction's fix before this file's fixed-field-list
    defect was found in the *other* writer (_write_receipt); the two writers now share one
    read-modify-write implementation instead of two independently-maintained copies of it.
    """
    path = Path(state_path).with_name("listing.json")
    existing = _read_state_json(path)
    catalog_listings = dict(existing.get(_CATALOG_LISTINGS_KEY) or {})
    catalog_listings[family] = dict(record)
    existing[_CATALOG_LISTINGS_KEY] = catalog_listings
    _atomic_write_json(path, existing)


def _family_create_product(catalog_module: Any, catalog: Mapping[str, Any], family: str) -> dict[str, Any]:
    """Build the create_package()-shaped product dict for one catalogue family.

    title_stem/subtitle/category/plans/description come from project_lancers() (the catalogue's
    own Lancers projection); subcategory/service_type/industry/tags/notice come straight from that family's
    platform_overrides.lancers row when present -- never invented when absent, so a family
    whose overrides do not (yet) carry one of them fails _require_create_fields by name.
    """
    projection = catalog_module.project_lancers(catalog, family)
    row = catalog_module.entries_by_family(catalog)[family]
    override = (row.get("platform_overrides") or {}).get("lancers")
    override = override if isinstance(override, Mapping) else {}
    product: dict[str, Any] = {
        "title_stem": projection.get("title_stem"),
        "subtitle": projection.get("subtitle"),
        "category": projection.get("category"),
        "plans": projection.get("plans"),
        "description": projection.get("description"),
    }
    for field in _CATALOG_OVERLAY_FIELDS:
        if field in override:
            product[field] = override[field]
    return product


def select_catalog_family_to_create(catalog_path: Path, state_path: Path) -> dict[str, Any]:
    """Which catalogue family (if any) should this wake attempt to create on Lancers?

    Pure and browser-free: no page is ever opened for a family this function does not select.
    Walks the catalogue's own listing order, skipping any family _read_catalog_listings already
    has a record for (so a family is created at most once, ever), and returns the first
    remaining family whose overlay is complete enough for create_package(). Every family this
    scan passes over on the way -- already published or overlay-incomplete -- is accounted for
    so the caller can report exactly what happened, never a silent no-op.

    Returns one of:
      {"action": "all_published", "skipped": []} -- nothing left to create.
      {"action": "all_pending_incomplete", "skipped": [...]} -- every remaining family named,
        none creatable yet because its lancers overlay is missing one of
        _CATALOG_OVERLAY_FIELDS (subcategory/service_type/industry/tags/notice).
      {"action": "candidate_selected", "family": ..., "product": ..., "skipped": [...]} -- the
        one family to attempt, plus every incomplete family skipped before reaching it.
      {"action": "catalog_unavailable", "error": ...} -- the catalogue itself failed to load.
    """
    listing_catalog = _reach_marketplace_core()
    try:
        catalog = listing_catalog.load(catalog_path)
    except listing_catalog.CatalogError as error:
        return {"action": "catalog_unavailable", "error": str(error), "skipped": []}
    order = _catalog_family_order(catalog)
    published = _read_catalog_listings(state_path)
    pending = [family for family in order if family not in published]
    if not pending:
        return {"action": "all_published", "skipped": []}
    skipped: list[dict[str, str]] = []
    for family in pending:
        product = _family_create_product(listing_catalog, catalog, family)
        try:
            _require_create_fields(product)
        except OfferError as error:
            skipped.append({"family": family, "reason": str(error)})
            continue
        return {"action": "candidate_selected", "family": family, "product": product, "skipped": skipped}
    return {"action": "all_pending_incomplete", "skipped": skipped}


def run_catalog_create(state_path: Path, catalog_path: Path = DEFAULT_CATALOG) -> dict[str, Any]:
    """The storefront wake's fallback effect -- main() calls this only when run()'s own
    single-offer chain produced no mutation this wake. Selects at most one family
    (select_catalog_family_to_create), attempts create_package() for it if one was selected,
    and persists the resulting listing_external_id so the same family is never attempted again.
    One creation per call, exactly mirroring run()/run_create()'s own one-mutation-per-call
    discipline; a selection outcome other than "candidate_selected" is returned unchanged --
    there is nothing to create and nothing to persist.
    """
    selection = select_catalog_family_to_create(catalog_path, Path(state_path))
    if selection["action"] != "candidate_selected":
        return selection
    family, product = selection["family"], selection["product"]
    tick = browser = page = None; logged_in = False; result: dict[str, Any] = {"ok": False, "error": "offer_unavailable"}
    try:
        tick = _load("lancers_storefront_create_from_catalog_tick", HERE / "application_tick.py")
        with tick.account_lock(Path(state_path).with_name("work-sync.json")):
            browser = tick._default_browser_factory(tick.CDP_URL); page = tick._new_owned_page(browser)
            if not tick._production_account_ready(page): raise OfferError("account_unavailable")
            logged_in = True; result = create_package(page, product, DEFAULT_AVATAR)
    except OfferError as error: result = {"ok": False, "logged_in": logged_in, "error": str(error)}
    except Exception as error:
        print(f"storefront_offer_catalog_create:{type(error).__name__}: {str(error)[:400]}", file=sys.stderr)
        result = {"ok": False, "logged_in": logged_in,
                  "error": "account_lock_busy" if "LockBusy" in type(error).__name__ else "offer_unavailable",
                  "failure": f"{type(error).__name__}: {str(error)[:200]}"}
    finally:
        try:
            closed = page is None or bool(tick._close_owned_page(page))
            if browser is not None: tick._stop_playwright_runtime(getattr(browser, "_anicca_playwright_runtime", None))
        except Exception: closed = False
        if not closed: result = {"ok": False, "logged_in": logged_in, "error": "cleanup_failed"}
    result = dict(result); result["family"] = family; result["skipped"] = selection["skipped"]
    listing_external_id = result.get("listing_external_id")
    if result.get("ok") is True and isinstance(listing_external_id, str) and listing_external_id:
        _write_catalog_listing(Path(state_path), family, {
            "listing_external_id": listing_external_id,
            "public_url": result.get("canonical_url"),
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
    elif result.get("ok") is True:
        result["ok"] = False; result.setdefault("error", "listing_id_missing")
    return result


def _portfolio(page: Any, item: Mapping[str, Any]) -> dict[str, Any] | None:
    title = item["title_stem"] + "ました"
    with page.expect_response(lambda response: response.request.method == "GET" and urlsplit(response.url).path == "/api/v1/me/portfolio", timeout=20_000) as loaded:
        page.goto(f"{ORIGIN}/myportfolio", wait_until="domcontentloaded", timeout=20_000)
    if loaded.value.status != 200: raise OfferError("portfolio_readback_invalid")
    matches = []
    for link in page.locator('a[href*="portfolio"]').all():
        if " ".join(str(link.inner_text() or "").split()) == title:
            matches.append(link)
    if not matches: return None
    if len(matches) != 1: raise OfferError("portfolio_readback_invalid")
    href = matches[0].get_attribute("href") or ""
    parsed = urlsplit(href)
    found = re.fullmatch(r"/profile/[^/?#\s]+/portfolio_popup/([0-9]+)", parsed.path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or found is None or item["external_id"] and found.group(1) != item["external_id"]: raise OfferError("portfolio_readback_invalid")
    return {"portfolio_external_id": found.group(1), "portfolio_url": ORIGIN + parsed.path}


def _ensure_portfolio(page: Any, product: Mapping[str, Any], image: Path, key: str) -> dict[str, Any]:
    item = product[key]; existing = _portfolio(page, item)
    if existing is not None: return existing | {"portfolio_effect_count": 0}
    page.goto(f"{ORIGIN}/myportfolio/add", wait_until="domcontentloaded", timeout=20_000)
    if urlsplit(str(page.url)).path != "/myportfolio/add": raise OfferError("portfolio_form_changed")
    _field(page, 'textarea[name="title"]').fill(item["title_stem"])
    _field(page, 'textarea[name="subtitle"]').fill(item["subtitle"])
    _field(page, 'textarea[name="content"]').fill(item["description"])
    uploads = page.locator('input[type="file"]')
    if uploads.count() != 2 or uploads.nth(0).get_attribute("accept") != ".jpg,.jpeg,.png,.gif": raise OfferError("portfolio_form_changed")
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/v1/file/add", timeout=20_000) as registered:
        with page.expect_response(lambda response: response.request.method == "PUT" and urlsplit(response.url).hostname == "upload-lancers-jp.s3.ap-northeast-1.amazonaws.com", timeout=20_000) as uploaded:
            uploads.nth(0).set_input_files(str(image))
    if registered.value.status != 200 or uploaded.value.status != 200: raise OfferError("portfolio_image_upload_failed")
    page.wait_for_selector('form img[alt="Image Preview"]', state="visible", timeout=5_000)
    selects = page.locator("select")
    if selects.count() != 5: raise OfferError("portfolio_form_changed")
    selects.nth(0).select_option(label=item["category"])
    page.wait_for_function("label => [...document.querySelectorAll('select')][1]?.querySelector(`option[value]:not([value=''])`) && [...document.querySelectorAll('select')][1].innerText.includes(label)", arg=item["subcategory"], timeout=5_000)
    if selects.count() != 6: raise OfferError("portfolio_form_changed")
    selects.nth(1).select_option(label=item["subcategory"])
    selects.nth(2).select_option(label=item.get("industry", product["industry"]))
    _field(page, 'input[placeholder="10"]').fill(str(item["duration_value"]))
    selects.nth(3).select_option(item["duration_unit"])
    _field(page, 'input[placeholder="50,000"]').fill(str(item.get("reference_price_jpy", product["plans"][0]["price_jpy"])))
    selects.nth(4).select_option(str(item.get("listing_external_id", product["listing_external_id"])))
    checks = page.locator('input[type="checkbox"]')
    if checks.count() < 3: raise OfferError("portfolio_form_changed")
    ai_checks = [field for field in checks.all() if " ".join(field.evaluate("e => e.parentElement.parentElement.innerText").split()) == "生成AIを活用した制作物です"]
    if len(ai_checks) != 1: raise OfferError("portfolio_form_changed")
    if item["generated_ai"] and not ai_checks[0].is_checked(): ai_checks[0].check()
    selects.nth(5).select_option("public")
    _field(page, 'input[label="10"]').fill(str(item["order_index"]))
    save = page.get_by_role("button", name="保存", exact=True)
    if save.count() != 1: raise OfferError("portfolio_form_changed")
    try: save.click(); page.wait_for_timeout(2_000)
    except Exception: raise OfferError("portfolio_submission_uncertain") from None
    observed = _portfolio(page, item)
    if observed is None: raise OfferError("portfolio_submission_uncertain")
    return observed | {"portfolio_effect_count": 1}


def ensure_profile(product_path: Path, state_path: Path) -> dict[str, Any]:
    tick = browser = page = None; logged_in = False; result: dict[str, Any] = {"ok": False, "error": "profile_unavailable"}
    try:
        product, _image, avatar = _product(product_path); tick = _load("lancers_profile_tick", HERE / "application_tick.py")
        with tick.account_lock(state_path.with_name("work-sync.json")):
            browser = tick._default_browser_factory(tick.CDP_URL); page = tick._new_owned_page(browser)
            if not tick._production_account_ready(page): raise OfferError("account_unavailable")
            logged_in = True; result = {"ok": True, "logged_in": True} | _profile(page, product, avatar, True)
    except OfferError as error: result = {"ok": False, "logged_in": logged_in, "error": str(error)}
    except Exception as error:
        print(f"profile_owner:{type(error).__name__}", file=sys.stderr)
        result = {"ok": False, "logged_in": logged_in, "error": "account_lock_busy" if "LockBusy" in type(error).__name__ else "profile_unavailable"}
    finally:
        try:
            closed = page is None or bool(tick._close_owned_page(page))
            if browser is not None: tick._stop_playwright_runtime(getattr(browser, "_anicca_playwright_runtime", None))
        except Exception: closed = False
        if not closed: result = {"ok": False, "logged_in": logged_in, "error": "cleanup_failed"}
    return result


def run(apply: bool, product_path: Path, state_path: Path) -> dict[str, Any]:
    tick = browser = page = None; logged_in = False; result: dict[str, Any] = {"ok": False, "error": "offer_unavailable"}
    try:
        product, image, avatar = _product(product_path); tick = _load("lancers_storefront_offer_tick", HERE / "application_tick.py")
        with tick.account_lock(state_path.with_name("work-sync.json")):
            browser = tick._default_browser_factory(tick.CDP_URL); page = tick._new_owned_page(browser)
            if not tick._production_account_ready(page): raise OfferError("account_unavailable")
            logged_in = True; result = _apply(page, product, image) if apply else _public(page, product) | {"action": "inspect"}
            if apply and result.get("action") == "unchanged":
                for key in ("portfolio", "software_portfolio"):
                    portfolio = _ensure_portfolio(page, product, image, key)
                    result["portfolio_effect_count"] = portfolio["portfolio_effect_count"]
                    result[key + "_external_id"] = portfolio["portfolio_external_id"]
                    result[key + "_url"] = portfolio["portfolio_url"]
                    if portfolio["portfolio_effect_count"]:
                        result["action"] = "portfolio_created"; break
                else:
                    profile = _profile(page, product, DEFAULT_AVATAR, True); result |= profile
                    if profile["profile_effect_count"]: result["action"] = "profile_updated"
            if result.get("ok") is True and result.get("aligned") is True:
                result["demand"] = _demand(page, product["listing_external_id"])
                if apply: _write_receipt(Path(state_path), product, result["demand"])
    except OfferError as error: result = {"ok": False, "logged_in": logged_in, "error": str(error)}
    except Exception as error:
        # The type alone is not diagnosable. This lane spent six days reporting
        # storefront_offer:TimeoutError and storefront_offer:TargetClosedError with nothing
        # to act on, while the same class of browser failure was being fixed by name on the
        # sibling Coconala lane. The message says which page and which operation.
        print(f"storefront_offer:{type(error).__name__}: {str(error)[:400]}", file=sys.stderr)
        result = {"ok": False, "logged_in": logged_in,
                  "error": "account_lock_busy" if "LockBusy" in type(error).__name__ else "offer_unavailable",
                  "failure": f"{type(error).__name__}: {str(error)[:200]}"}
    finally:
        try:
            closed = page is None or bool(tick._close_owned_page(page))
            if browser is not None: tick._stop_playwright_runtime(getattr(browser, "_anicca_playwright_runtime", None))
        except Exception: closed = False
        if not closed: result = {"ok": False, "logged_in": logged_in, "error": "cleanup_failed"}
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true"); mode.add_argument("--apply", action="store_true")
    # Separate top-level mode, not a modifier of --apply: create_package() must never fire as a
    # side effect of the existing edit-in-place flow (see create_package()'s own docstring).
    mode.add_argument("--create-package", action="store_true", dest="create_package")
    parser.add_argument("--product", type=Path, default=DEFAULT_PRODUCT); parser.add_argument("--state-path", type=Path, default=Path.home() / ".local/state/anicca/lancers/application.json")
    args = parser.parse_args(argv)
    if args.create_package:
        result = run_create(args.product, args.state_path)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
        return 0 if result.get("ok") is True else 1
    result = run(args.apply, args.product, args.state_path)
    # The wake's fallback effect: only when the single-offer chain above left nothing to do
    # (result["action"] == "unchanged" -- no status pause, no field alignment, no portfolio, no
    # profile update) does the wake get a second, independent chance to create one new listing
    # from the shared catalogue. See run_catalog_create()'s own docstring for why this must be
    # a separate account_lock acquisition, never nested inside run()'s.
    if args.apply and result.get("action") == "unchanged":
        result["catalog_creation"] = run_catalog_create(args.state_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
    if args.apply:
        reporter = _load("_anicca_lancers_storefront_reporter", HERE / "telegram_report.py")
        delivery = reporter.notify_storefront_wake(result)
        if delivery.delivery_uncertain or delivery.pre_send_failed: return 1
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__": raise SystemExit(main())
