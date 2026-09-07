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
# The live DOM read (see the task this shipped from) did not identify which button actually
# publishes -- only that "プレビュー" and several "のコツ" toggles are also present. Rather than
# hardcode a guess, the submit control is discovered by matching visible button text against
# every label a Lancers form has been observed to use for "move this listing forward" elsewhere
# in this file (_apply uses "保存"/"保存する"; the create chooser flow is known to use
# "確認画面へ"/"公開する"/"公開" for its multi-step forms). _create_submit_control fails loudly,
# naming every button text actually present, if zero or more than one match.
_CREATE_SUBMIT_LABELS = ("確認画面へ", "公開する", "公開", "保存する", "保存")
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
_CREATE_REQUIRED_FIELDS = ("title_stem", "subtitle", "category", "subcategory", "industry", "tags", "notice", "plans", "description")
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


def _select_delivery_time(page: Any, selector: str, delivery_days: int) -> None:
    """Select the option whose *label* names `delivery_days`, never a neighbouring value.

    The Lancers projection (listing_catalog.project_lancers) already rounds every catalogue
    tier up to a day count Lancers is known to offer, so a real mismatch here means the form
    itself changed shape -- that must stop the wake loudly, not silently pick the closest
    option.
    """
    field = _field(page, selector)
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


def _create_step_validation_text(page: Any) -> str:
    """Best-effort scrape of on-page validation text for a stalled step's error message.

    The live DOM read never named Lancers' validation-message markup, so unlike every other
    selector in this file this one is not something create_step_stalled can assert an exact
    match on. It casts a wide net across every visible element whose class mentions "error" and
    joins whatever text they carry. Finding nothing is not itself a failure -- it just leaves
    the surrounding create_step_stalled error with no extra detail to report.
    """
    try:
        nodes = page.locator("[class*='error']").all()
    except Exception:
        return ""
    texts: list[str] = []
    for node in nodes:
        try:
            if not node.is_visible(): continue
            text = " ".join(str(node.inner_text() or "").split())
        except Exception:
            continue
        if text: texts.append(text)
    return " / ".join(texts)


def _click_create_next_button(page: Any, step_name: str) -> None:
    """Click 次へ, reusing _step()'s exact-visible-text-match discipline. A missing/ambiguous
    button is named against the step that could not advance, not as a bare "form_changed"."""
    try:
        _step(page, _CREATE_NEXT_BUTTON_TEXT)
    except OfferError:
        raise OfferError(f"create_step_stalled: {step_name}: next_button_missing") from None


def _advance_create_step(page: Any, step_name: str, arrival: Any) -> None:
    """Click 次へ from `step_name` and confirm the next step actually became current.

    `arrival` is a zero-arg callable returning a Locator whose visibility proves the next step
    now shows. A step whose click does not produce that visibility -- most likely a validation
    failure on the step just filled -- raises create_step_stalled naming the step and any
    validation text the page is showing, instead of letting the next .fill() time out
    anonymously against a field with a zero-size bounding box (the bug this shipped from).
    """
    _click_create_next_button(page, step_name)
    try:
        arrival().wait_for(state="visible", timeout=10_000)
    except Exception:
        raise OfferError(f"create_step_stalled: {step_name}: {_create_step_validation_text(page)}") from None


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
    _field(page, '[name="ProjectPlanForm.title"]').fill(product["title_stem"])
    _field(page, '[name="ProjectPlanForm.subtitle"]').fill(product["subtitle"])
    _field(page, '[name="___main_category_id"]').select_option(label=product["category"])
    page.wait_for_function(
        "label => [...document.querySelectorAll('[name=\"ProjectPlanForm.project_category_id\"] option')].some(o => o.textContent.trim() === label)",
        arg=product["subcategory"], timeout=5_000,
    )
    _field(page, '[name="ProjectPlanForm.project_category_id"]').select_option(label=product["subcategory"])
    _field(page, '[name="ProjectPlanForm.industry_type_id"]').select_option(label=product["industry"])
    tag_field = _field(page, '[name="MultiSelectTagSearch_ProjectPlanTagForm"]')
    for tag in product["tags"]:
        tag_field.fill(tag); tag_field.press("Enter")
    _advance_create_step(page, "基本情報", lambda: page.locator('[name="ProjectPlanMenuForm[0].description"]'))

    # 2/6 料金表 -- 「料金は必ず3プラン必要です」, exactly 3 plans (ベーシック/スタンダード/プレミアム).
    for index, plan in enumerate(product["plans"]):
        prefix = f"ProjectPlanMenuForm[{index}]"
        _field(page, f'[name="{prefix}.description"]').fill(plan["description"])
        _select_delivery_time(page, f'[name="{prefix}.delivery_time"]', plan["delivery_days"])
        _field(page, f'[name="{prefix}.price"]').fill(str(plan["price_jpy"]))
    _advance_create_step(page, "料金表", lambda: _create_business_textarea(page))

    # 3/6 業務内容 -- the single unnamed textarea, max 2000 chars (already enforced against
    # `product["description"]` by _require_create_fields before this function ever ran).
    _create_business_textarea(page).fill(product["description"])
    _advance_create_step(page, "業務内容", lambda: page.locator('[name="ProjectPlanForm.notice_for_sale"]'))

    # 4/6 確認事項 -- 注文時のお願い (必須); 注文時の質問 is optional and unused here.
    _field(page, '[name="ProjectPlanForm.notice_for_sale"]').fill(product["notice"])
    _advance_create_step(page, "確認事項", lambda: page.get_by_text(_CREATE_IMAGE_STEP_MARKER_TEXT, exact=False))

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
    _click_create_next_button(page, "画像ほか")

    # 6/6 公開 -- the publish control's label was never observed live; create_package() discovers
    # and clicks it via _create_submit_control, which already fails loudly by naming every
    # visible button if it cannot find exactly one match.
    return {"image_attached": image_attached}


def _create_submit_control(page: Any) -> Any:
    buttons = [button for button in page.locator("button").all() if button.is_visible()]
    texts = [" ".join(str(button.inner_text() or "").split()) for button in buttons]
    matches = [button for button, text in zip(buttons, texts) if text in _CREATE_SUBMIT_LABELS]
    if len(matches) != 1: raise OfferError(f"create_submit_control_missing: buttons={texts}")
    return matches[0]


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
    description over the 2000-char cap) raises before any navigation; landing anywhere other
    than /myplan/add?type=manual after clicking the manual option raises rather than filling a
    form that cannot be identified; a delivery_time with no matching option raises without
    selecting anything; an advance to the next wizard step that does not actually arrive raises
    create_step_stalled naming the step; no single matching submit button raises, naming the
    buttons actually present; and a successful submission whose public page cannot be read back
    is reported as publication_uncertain, never as success.
    """
    _require_create_fields(product)
    page.goto(_CREATE_ADD_URL, wait_until="domcontentloaded", timeout=30_000)
    _step(page, _CREATE_MANUAL_BUTTON_TEXT)
    if page.url != _CREATE_MANUAL_URL: raise OfferError(f"create_route_invalid: url={page.url}")
    page.wait_for_selector('[name="ProjectPlanForm.title"]', state="visible", timeout=5_000)
    fill_result = _fill_create_form(page, product, image)
    submit = _create_submit_control(page)
    submit.click(timeout=20_000)
    page.wait_for_url(_CREATE_LISTING_ID_IN_URL, timeout=30_000)
    match = _CREATE_LISTING_ID_IN_URL.match(str(page.url))
    if match is None: raise OfferError(f"create_listing_id_unresolved: url={page.url}")
    listing_id = match.group(1)
    published = dict(product) | {"listing_external_id": listing_id, "public_title": product["title_stem"] + "ます"}
    try: return _public(page, published) | {"action": "created", "listing_external_id": listing_id} | fill_result
    except OfferError: raise OfferError("publication_uncertain") from None


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
# carries category/industry/tags/notice (see the catalogue task this shipped from), but never
# subcategory -- Lancers' subcategory options are a dependent select whose values only appear
# once the main category is chosen in the live form, and that option list has never been
# observed. A family missing any of these is named under "skipped", never filled with a guess.
_CATALOG_OVERLAY_FIELDS = ("subcategory", "industry", "tags", "notice")


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

    Reads-modifies-writes the whole file (preserving the single-offer listing_receipt fields
    _write_receipt owns, and every other family already recorded) with the same atomic
    tempfile-then-replace, 0600-permission pattern _write_receipt uses -- one file, one write
    discipline, never a half-written listing.json.
    """
    path = Path(state_path).with_name("listing.json")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict): existing = {}
    except (OSError, ValueError):
        existing = {}
    catalog_listings = dict(existing.get(_CATALOG_LISTINGS_KEY) or {})
    catalog_listings[family] = dict(record)
    existing[_CATALOG_LISTINGS_KEY] = catalog_listings
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(existing, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")); handle.write("\n")
        os.replace(temporary, path); path.chmod(0o600)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _family_create_product(catalog_module: Any, catalog: Mapping[str, Any], family: str) -> dict[str, Any]:
    """Build the create_package()-shaped product dict for one catalogue family.

    title_stem/subtitle/category/plans/description come from project_lancers() (the catalogue's
    own Lancers projection); subcategory/industry/tags/notice come straight from that family's
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
        _CATALOG_OVERLAY_FIELDS (subcategory/industry/tags/notice).
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
