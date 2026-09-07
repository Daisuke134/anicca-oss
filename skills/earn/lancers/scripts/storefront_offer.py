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
    if not (1 <= len(value["title_stem"] + "ます") <= 40 and len(value["subtitle"]) <= 60 and len(value["description"]) <= 2000 and len(value["notice"]) <= 2000): raise OfferError("product_invalid")
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


def _text(page: Any, selector: str) -> str:
    locator = page.locator(selector)
    if locator.count() != 1: raise OfferError("public_readback_invalid")
    text = " ".join(str(locator.inner_text() or "").split())
    if not text: raise OfferError("public_readback_invalid")
    return text


def _public(page: Any, product: Mapping[str, Any]) -> dict[str, Any]:
    listing_id = product["listing_external_id"]; public_url = f"{ORIGIN}/menu/detail/{listing_id}"
    response = page.goto(public_url, wait_until="domcontentloaded", timeout=30_000)
    if response is None or response.status != 200 or page.url != public_url: raise OfferError("public_readback_invalid")
    canonical = page.locator('link[rel="canonical"]')
    og = page.locator('meta[property="og:url"]')
    if canonical.count() != 1 or canonical.get_attribute("href") != public_url or og.count() != 1 or og.get_attribute("content") != public_url: raise OfferError("canonical_mismatch")
    plans = []
    for section in page.locator("li.p-menu-browse-detail__sidebar-content.js-project-plan-tab-content").all():
        fields = [section.locator(selector) for selector in ("p.p-menu-browse-detail__sidebar-description", "div.p-menu-browse-detail__sidebar-header-price", "div.p-menu-browse-detail__sidebar-menu")]
        if [field.count() for field in fields] == [0, 0, 0]: continue
        if [field.count() for field in fields] != [1, 1, 1]: raise OfferError("public_readback_invalid")
        description = " ".join(fields[0].inner_text().split()); price = "".join(re.findall(r"[0-9]", fields[1].inner_text())); delivery = re.search(r"納期\s*([0-9]+)\s*日", fields[2].inner_text())
        if not description or not price or delivery is None: raise OfferError("public_readback_invalid")
        plans.append({"description": description, "price_jpy": int(price), "delivery_days": int(delivery.group(1))})
    routes = []
    for prefix in ("basicMain", "standardMain", "premiumMain"):
        for month in (1, 3, 6):
            field = page.locator(f"#{prefix}{month}")
            if field.count() != 1: raise OfferError("contract_route_invalid")
            route = field.get_attribute("value") or ""
            expected = r"/project_board/quote_request\?project_plan_menu_id=[0-9]+" if month == 1 else rf"/monthly_work_contracts/client/[^/]+/add\?project_plan_menu_id=[0-9]+&month={month}"
            if re.fullmatch(expected, route) is None: raise OfferError("contract_route_invalid")
            routes.append(route)
    image = page.locator(".p-menu-browse-detail__carousel-list img")
    has_image = image.count() >= 1 and all("photo-film" not in str(image.nth(index).get_attribute("src") or "") for index in range(image.count()))
    observed = {"title": _text(page, "h1"), "subtitle": _text(page, ".l-page-header__heading-description"), "description": _text(page, "#body + .p-project-plan-markdown"), "notice": _text(page, "#notice_for_sale + .c-text"), "plans": plans}
    expected = {"title": product["public_title"], "subtitle": product["subtitle"], "description": " ".join(product["description"].split()), "notice": " ".join(product["notice"].split()), "plans": [{key: plan[key] for key in ("description", "price_jpy", "delivery_days")} for plan in product["plans"]]}
    mismatched = [key for key in expected if observed[key] != expected[key]] + ([] if has_image else ["image"])
    return {"ok": True, "logged_in": True, "listing_external_id": listing_id, "canonical_url": public_url, "aligned": not mismatched, "mismatched_fields": mismatched, "has_image": has_image, "prices_jpy": [plan["price_jpy"] for plan in plans], "delivery_days": [plan["delivery_days"] for plan in plans], "contract_routes": {"spot": 3, "three_month": 3, "six_month": 3}}


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


def _field(page: Any, selector: str) -> Any:
    field = page.locator(selector)
    if field.count() != 1: raise OfferError("form_changed")
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
    path = state_path.with_name("listing.json"); path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    digest = hashlib.sha256(json.dumps(product, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    value = {"record_type": "listing_receipt", "schema_version": 1, "platform": "lancers", "product_id": product["product_id"], "product_version": product["product_version"], "listing_external_id": product["listing_external_id"], "public_url": f"{ORIGIN}/menu/detail/{product['listing_external_id']}", "status": "published", "content_sha256": digest, "idempotency_key": f"lancers:listing:{product['product_id']}:v{product['product_version']}", "demand": dict(demand), "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle: json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")); handle.write("\n")
        os.replace(temporary, path); path.chmod(0o600)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _step(page: Any, label: str) -> None:
    values = [item for item in page.get_by_text(label, exact=True).all() if item.is_visible()]
    if len(values) != 1: raise OfferError("form_changed")
    values[0].click()


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
        except OfferError: raise OfferError("publication_uncertain") from None
    _field(page, '[name="ProjectPlanForm.title"]').fill(product["title_stem"])
    _field(page, '[name="ProjectPlanForm.subtitle"]').fill(product["subtitle"])
    _field(page, '[name="___main_category_id"]').select_option(label=product["category"])
    page.wait_for_function("label => [...document.querySelectorAll('[name=\"ProjectPlanForm.project_category_id\"] option')].some(o => o.textContent.trim() === label)", arg=product["subcategory"], timeout=5_000)
    _field(page, '[name="ProjectPlanForm.project_category_id"]').select_option(label=product["subcategory"])
    page.wait_for_selector('[name="ProjectPlanCategoryForm.service_type[0]"]', state="attached", timeout=5_000)
    services = page.locator('[name="ProjectPlanCategoryForm.service_type[0]"]')
    matches = [field for field in services.all() if " ".join(field.evaluate("e => e.parentElement.parentElement.innerText").split()) == product["service_type"]]
    if len(matches) != 1 or re.fullmatch(r"[0-9]+", matches[0].get_attribute("value") or "") is None: raise OfferError("form_changed")
    service_id = matches[0].get_attribute("value")
    with page.expect_response(lambda response: urlsplit(response.url).path == f"/v1/project_store_api/project_category/{service_id}", timeout=10_000) as service_loaded:
        page.locator(f'label[for="{service_id}"]').click()
    if service_loaded.value.status != 200 or not matches[0].is_checked(): raise OfferError("form_changed")
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
    except OfferError: raise OfferError("publication_uncertain") from None


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
    parser.add_argument("--product", type=Path, default=DEFAULT_PRODUCT); parser.add_argument("--state-path", type=Path, default=Path.home() / ".local/state/anicca/lancers/application.json")
    args = parser.parse_args(argv); result = run(args.apply, args.product, args.state_path)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)
    if args.apply:
        reporter = _load("_anicca_lancers_storefront_reporter", HERE / "telegram_report.py")
        delivery = reporter.notify_storefront_wake(result)
        if delivery.delivery_uncertain or delivery.pre_send_failed: return 1
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__": raise SystemExit(main())
