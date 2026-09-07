#!/usr/bin/env python3
"""Read-only, truth-preserving Lancers owner snapshot and Telegram tick."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
OUTBOX_PATH = ROOT / "skills/_shared/marketplace-core/scripts/telegram_outbox.py"
LEDGER_PATH = OUTBOX_PATH.with_name("ledger.py")
TICK_PATH = HERE / "application_tick.py"
WORK_SYNC_PATH = HERE / "work_sync.py"
TELEGRAM_PATH = ROOT / "skills/_shared/telegram.py"
STATE = Path.home() / ".local/state/anicca/lancers/application.json"
DATABASE = STATE.with_name("telegram.sqlite3")
LEDGER_DATABASE = STATE.with_name("marketplace-ledger.sqlite3")
STOREFRONT_LOG = STATE.parent / "logs/storefront.stdout.log"
CHAT_CONFIG = Path.home() / ".config" / "anicca" / "lancers" / "telegram.env"


def _report_chat() -> str:
    """Where the owner report goes.

    The chat id is not in the repository. It comes from the environment, or
    from a private config file when the launchd job does not carry it — a
    placeholder here sends every report to a chat that does not exist, and the
    outbox records delivery_uncertain forever without anyone noticing.
    """
    for key in ("LANCERS_REPORT_CHAT", "GIG_REPORT_CHAT"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    try:
        lines = CHAT_CONFIG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in lines:
        name, _, value = raw.partition("=")
        if name.strip() == "LANCERS_REPORT_CHAT":
            return value.strip()
    return ""


TARGET = _report_chat()
TOKYO = ZoneInfo("Asia/Tokyo")
_LABELS = (("published", "受付中", "/myplan"), ("paused", "受付休止中", "/myplan/paused"), ("hidden", "非表示", "/myplan/archived"), ("draft", "下書き", "/myplan/draft"))
_DEMAND_LABELS = (("search_impressions", "検索表示"), ("detail_views", "詳細閲覧"), ("favorites", "お気に入り"), ("inquiries", "相談"), ("orders", "注文"))
_LANCERS_ORIGIN = "https://www.lancers.jp"
_INVENTORY_STORE_SELECTOR = ".p-project-plan-myplan__stores .p-project-plan-myplan__store"
_INVENTORY_TITLE_SELECTOR = ".p-project-plan-myplan__store-content-over-title-link"
_INVENTORY_MAX_PAGES = 20


class InventoryFailure(RuntimeError):
    """Stable, sanitized failure at the read-only storefront boundary."""


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("dependency_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
outbox = _load("lancers_report_outbox", OUTBOX_PATH)
summary = _load("lancers_lane_summary", OUTBOX_PATH.with_name("lane_summary.py"))
shared_delivery = _load("lancers_report_delivery", OUTBOX_PATH.with_name("telegram_delivery.py"))


def _inventory_item(locator: Any, code: str, visible: bool = True) -> Any:
    try:
        if int(locator.count()) != 1:
            raise InventoryFailure(code)
        item = locator.nth(0)
        if visible and not item.is_visible():
            raise InventoryFailure(code)
        return item
    except InventoryFailure:
        raise
    except Exception:
        raise InventoryFailure(code) from None


def _inventory_text(item: Any, code: str) -> str:
    try:
        value = " ".join(str(item.inner_text() or "").split())
    except Exception:
        raise InventoryFailure(code) from None
    if not value or len(value) > 50_000:
        raise InventoryFailure(code)
    return value


def _inventory_url(value: object, *, relative: bool = False) -> tuple[str, str]:
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        match = re.fullmatch(r"/menu/detail/([1-9][0-9]{0,18})", parsed.path if parsed else "")
    except Exception:
        match = None; parsed = None
    if not parsed or not match or parsed.query or parsed.fragment or parsed.username or parsed.password or (relative and (parsed.scheme or parsed.netloc)) or (not relative and (parsed.scheme, parsed.netloc) != ("https", "www.lancers.jp")):
        raise InventoryFailure("inventory_management_invalid" if relative else "inventory_public_route_invalid")
    return f"{_LANCERS_ORIGIN}{parsed.path}", match.group(1)


def _inventory_rows(page: Any, state: str) -> list[dict[str, str]]:
    try:
        stores = page.locator(_INVENTORY_STORE_SELECTOR)
        rows = []
        for index in range(int(stores.count())):
            link = _inventory_item(stores.nth(index).locator(_INVENTORY_TITLE_SELECTOR), "inventory_management_invalid")
            url, listing_id = _inventory_url(link.get_attribute("href"), relative=True)
            rows.append({"listing_external_id": listing_id, "state": state, "management_title": _inventory_text(link, "inventory_management_invalid"), "public_url": url})
        return rows
    except InventoryFailure:
        raise
    except Exception:
        raise InventoryFailure("inventory_management_invalid") from None


def _inventory_management(page: Any) -> tuple[dict[str, int], list[Mapping[str, str]]]:
    try:
        page.goto(_LANCERS_ORIGIN + "/myplan", wait_until="domcontentloaded", timeout=20_000)
        counts = _parse_storefront(page)
    except Exception:
        raise InventoryFailure("inventory_anchor_invalid") from None
    seen: dict[str, Mapping[str, str]] = {}
    for state, _label, path in _LABELS:
        state_rows: list[Mapping[str, str]] = []
        for page_number in range(1, _INVENTORY_MAX_PAGES + 1):
            url = _LANCERS_ORIGIN + path + ("" if page_number == 1 else f"?page={page_number}")
            try:
                if page_number != 1 or path != "/myplan": page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                parsed = urlsplit(page.url)
            except Exception:
                raise InventoryFailure("inventory_management_route_invalid") from None
            if (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment) != ("https", "www.lancers.jp", path, "" if page_number == 1 else f"page={page_number}", "") or parsed.username or parsed.password:
                raise InventoryFailure("inventory_management_route_invalid")
            rows = _inventory_rows(page, state)
            if not rows and len(state_rows) < counts[state]: raise InventoryFailure("inventory_page_empty")
            for row in rows:
                old = seen.get(row["listing_external_id"])
                if old:
                    if old["state"] != state: raise InventoryFailure("inventory_cross_state_duplicate")
                    raise InventoryFailure("inventory_listing_title_conflict" if old["management_title"] != row["management_title"] else "inventory_duplicate_listing_id")
                seen[row["listing_external_id"]] = row; state_rows.append(row)
            if len(state_rows) > counts[state]: raise InventoryFailure("inventory_count_overflow")
            if len(state_rows) == counts[state]: break
        if len(state_rows) != counts[state]: raise InventoryFailure("inventory_page_limit_reached")
    return counts, list(seen.values())


def _inventory_offer(page: Any, row: Mapping[str, str]) -> dict[str, object]:
    public_url, listing_id = _inventory_url(row["public_url"])
    try:
        response = page.goto(public_url, wait_until="domcontentloaded", timeout=20_000)
        parsed = urlsplit(page.url)
        if getattr(response, "status", None) != 200 or (parsed.scheme, parsed.netloc, parsed.path) != ("https", "www.lancers.jp", f"/menu/detail/{listing_id}") or parsed.query or parsed.fragment or parsed.username or parsed.password: raise InventoryFailure("inventory_public_route_invalid")
        def text(selector: str, visible: bool = True) -> str:
            return _inventory_text(_inventory_item(page.locator(selector), "inventory_public_invalid", visible), "inventory_public_invalid")
        def attr(selector: str, name: str) -> str:
            try: value = _inventory_item(page.locator(selector), "inventory_public_invalid", False).get_attribute(name)
            except InventoryFailure: raise
            except Exception: raise InventoryFailure("inventory_public_invalid") from None
            if not isinstance(value, str) or not value: raise InventoryFailure("inventory_public_invalid")
            return value
        title, subtitle = text("h1"), text(".l-page-header__heading-description")
        description, notice = text("#body + .p-project-plan-markdown"), text("#notice_for_sale + .c-text")
        canonical_url, canonical_listing_id = _inventory_url(attr('link[rel="canonical"]', "href"))
        if attr('meta[property="og:url"]', "content") != public_url: raise InventoryFailure("inventory_public_og_invalid")
        tags = []
        for index in range(int(page.locator("a.c-tag-list__item").count())):
            tag = page.locator("a.c-tag-list__item").nth(index)
            if tag.is_visible(): tags.append({"text": _inventory_text(tag, "inventory_public_invalid"), "href": attr_from(tag, "href")})
        plans, sections = [], page.locator("li.p-menu-browse-detail__sidebar-content.js-project-plan-tab-content")
        for index in range(int(sections.count())):
            section = sections.nth(index); selectors = ("p.p-menu-browse-detail__sidebar-description", "div.p-menu-browse-detail__sidebar-header-price", "div.p-menu-browse-detail__sidebar-menu")
            fields = [section.locator(selector) for selector in selectors]
            if [int(field.count()) for field in fields] == [0, 0, 0]: continue
            description_text, price_text, delivery_text = [_inventory_text(_inventory_item(field, "inventory_plan_invalid", False), "inventory_plan_invalid") for field in fields]
            price, delivery = "".join(re.findall(r"[0-9]", price_text)), re.search(r"納期\s*([0-9]+)\s*日", delivery_text)
            if not price or not delivery or int(price) <= 0 or int(delivery.group(1)) <= 0: raise InventoryFailure("inventory_plan_invalid")
            plans.append({"description": description_text, "price_jpy": int(price), "delivery_days": int(delivery.group(1))})
    except InventoryFailure:
        raise
    except Exception:
        raise InventoryFailure("inventory_public_invalid") from None
    if not plans: raise InventoryFailure("inventory_plan_invalid")
    payload = {"title": title, "subtitle": subtitle, "description": description, "notice": notice, "plans": plans, "tags": tags}
    return {"listing_external_id": listing_id, "state": row["state"], "public_url": public_url, "title": title, "canonical_url": canonical_url, "canonical_listing_id": canonical_listing_id, "plans": [{"price_jpy": item["price_jpy"], "delivery_days": item["delivery_days"]} for item in plans], "content_sha256": hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def attr_from(item: Any, name: str) -> str:
    try: value = item.get_attribute(name)
    except Exception: raise InventoryFailure("inventory_public_invalid") from None
    if not isinstance(value, str) or not value: raise InventoryFailure("inventory_public_invalid")
    return value


def _inventory_result_error(error: str, logged_in: bool = False) -> dict[str, object]:
    return {"ok": False, "logged_in": logged_in, "source_complete": False, "error": error}


def run_inventory(*, state_path: Path = STATE, browser_factory: Optional[Callable[[str], object]] = None, tick_module: Any = None) -> dict[str, object]:
    tick = browser = page = None; logged_in = False; result = _inventory_result_error("inventory_unavailable")
    try:
        tick = tick_module or _load("lancers_inventory_application_tick", TICK_PATH)
        with tick.account_lock(Path(state_path).with_name("work-sync.json")):
            browser = (browser_factory or tick._default_browser_factory)(tick.CDP_URL); page = tick._new_owned_page(browser)
            if not tick._production_account_ready(page): raise InventoryFailure("account_unavailable")
            logged_in = True; counts, rows = _inventory_management(page)
            listings = [_inventory_offer(page, row) for row in sorted(rows, key=lambda row: int(row["listing_external_id"]))]
            groups: dict[str, list[Mapping[str, object]]] = {}
            for listing in listings: groups.setdefault(str(listing["content_sha256"]), []).append(listing)
            result = {"ok": True, "logged_in": True, "source_complete": True, "state_counts": counts, "listing_count": len(listings), "listings": listings, "content_groups": [{"content_sha256": digest, "listing_ids": sorted(str(item["listing_external_id"]) for item in group), "canonical_listing_ids": sorted({str(item["canonical_listing_id"]) for item in group})} for digest, group in sorted(groups.items())]}
    except InventoryFailure as error:
        result = _inventory_result_error(str(error), logged_in)
    except Exception as error:
        result = _inventory_result_error("account_lock_busy" if "LockBusy" in type(error).__name__ else "inventory_unavailable", logged_in)
    finally:
        try:
            closed = page is None or bool(tick._close_owned_page(page))
            if browser is not None: tick._stop_playwright_runtime(getattr(browser, "_anicca_playwright_runtime", None))
        except Exception: closed = False
        if not closed: result = _inventory_result_error("inventory_cleanup_failed", logged_in)
    return result


def _run_inventory_parent(state_path: str) -> dict[str, object]:
    try:
        sync = _load("lancers_inventory_work_sync", WORK_SYNC_PATH)
        result = sync._watchdog([sys.executable, str(Path(__file__).resolve()), "--inventory-json", "--inventory-worker", "--state-path", state_path], sync.TICK_TIMEOUT_SECONDS)
        return dict(result) if isinstance(result, Mapping) else _inventory_result_error("inventory_unavailable")
    except Exception: return _inventory_result_error("inventory_unavailable")

@dataclass(frozen=True)
class SendResult:
    attempted: bool
    provider_message_id: Optional[str] = None
    error_code: Optional[str] = None


@dataclass(frozen=True)
class DeliveryResult:
    attempted: int = 0
    delivered: int = 0
    delivery_uncertain: int = 0
    pre_send_failed: int = 0
def read_last_json(path: Path) -> Optional[Mapping[str, object]]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            return value
    return None


def read_application_wake(path: Path) -> tuple[Optional[Mapping[str, object]], int]:
    try: lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError): return None, 0
    values = []
    for line in lines:
        try: value = json.loads(line)
        except (TypeError, ValueError): continue
        if isinstance(value, Mapping): values.append(value)
    return (values[-1], len(values)) if values else (None, 0)


def _int(value: object) -> Optional[int]:
    return value if type(value) is int and value >= 0 else None

def _timestamp(value: object) -> Optional[str]:
    if isinstance(value, datetime):
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _source_timestamp(application: object) -> Optional[str]:
    for value in (application.get(k) for k in ("source_observed_at", "observed_at")) if isinstance(application, Mapping) else ():
        try: parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00")) if isinstance(value, str) and value.strip() else None
        except ValueError: continue
        if parsed is not None and parsed.tzinfo is not None: return _timestamp(parsed)
    return None

def _storefront_counts(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {key: None for key, _label, _href in _LABELS} | {"error": "source_unknown"}
    counts: dict[str, object] = {}
    for key, label, _href in _LABELS:
        value_for_key = value.get(key, value.get(label))
        counts[key] = _int(value_for_key)
    counts["error"] = value.get("error") if isinstance(value.get("error"), str) else None
    demand = value.get("demand")
    counts["demand"] = {key: _int(demand.get(key)) for key, _label in _DEMAND_LABELS} if isinstance(demand, Mapping) else None
    return counts

def _listing_demand(path: Path) -> Optional[dict[str, int]]:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError): return None
    demand = value.get("demand") if isinstance(value, Mapping) and value.get("record_type") == "listing_receipt" and value.get("platform") == "lancers" and value.get("status") == "published" else None
    if not isinstance(demand, Mapping) or set(demand) != {key for key, _label in _DEMAND_LABELS}: return None
    result = {key: _int(demand.get(key)) for key, _label in _DEMAND_LABELS}
    return result if all(value is not None for value in result.values()) else None

def _sales_snapshot(path: Path) -> Optional[dict[str, object]]:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError): return None
    keys = ("board_count", "unread_count", "required_reply_count", "application_board_count", "incoming_monthly_offer_count", "contract_candidate_count")
    if not isinstance(value, Mapping) or value.get("source_complete") is not True: return None
    result: dict[str, object] = {key: _int(value.get(key)) for key in keys}
    result["reply_status"] = value.get("reply_status") if isinstance(value.get("reply_status"), str) else None
    pipeline = value.get("proposal_pipeline")
    pipeline_keys = ("current_count", "receipt_count", "unlisted_receipt_count", "open_count", "selecting_count", "canceled_count", "ended_count", "working_count", "unknown_count")
    parsed_pipeline = {key: _int(pipeline.get(key)) for key in pipeline_keys} if isinstance(pipeline, Mapping) else None
    result["proposal_pipeline"] = parsed_pipeline if parsed_pipeline and all(item is not None for item in parsed_pipeline.values()) else None
    finance = value.get("finance")
    if isinstance(finance, Mapping) and finance.get("source_complete") is True:
        numbers = {key: _int(finance.get(key)) for key in ("payment_history_count", "account_balance_jpy", "received_gross_jpy")}
        result["finance"] = {"source_complete": True, **numbers} if all(item is not None for item in numbers.values()) else None
    elif isinstance(finance, Mapping) and finance.get("source_complete") is False:
        result["finance"] = {"source_complete": False}
    else: result["finance"] = None
    return result if all(result.get(key) is not None for key in (*keys, "reply_status")) else None

def _parse_storefront(page: object) -> dict[str, int]:
    if getattr(page, "url", None) != "https://www.lancers.jp/myplan":
        raise RuntimeError("storefront_route_invalid")
    anchors, found = page.locator("a"), {}
    for index in range(int(anchors.count())):
        anchor = anchors.nth(index)
        try:
            if not anchor.is_visible(): continue
            text, href = " ".join(anchor.inner_text().split()), anchor.get_attribute("href")
        except Exception:
            continue
        for key, label, expected_href in _LABELS:
            match = re.fullmatch(rf"{re.escape(label)}\s*\(\s*([0-9][0-9,]*)\s*件\s*\)", text)
            if match is not None:
                if href != expected_href or key in found: raise RuntimeError("storefront_anchor_invalid")
                found[key] = int(match.group(1).replace(",", "")); break
    if set(found) != {key for key, _label, _href in _LABELS}: raise RuntimeError("storefront_anchor_missing")
    return found

def read_storefront(state_path: Path = STATE, *, browser_factory: Optional[Callable[[str], object]] = None,
                    lock: Optional[Callable[[Path], object]] = None) -> dict[str, int]:
    tick = _load("lancers_report_application_tick", TICK_PATH)
    lock_factory = lock or tick.account_lock
    browser = page = None
    with lock_factory(Path(state_path)):
        try:
            browser = (browser_factory or tick._default_browser_factory)(tick.CDP_URL)
            page = tick._new_owned_page(browser)
            page.goto("https://www.lancers.jp/myplan", wait_until="domcontentloaded", timeout=20_000)
            return _parse_storefront(page)
        finally:
            if page is not None: tick._close_owned_page(page)
            runtime = getattr(browser, "_anicca_playwright_runtime", None)
            if runtime is not None: tick._stop_playwright_runtime(runtime)


def build_snapshot(*, application: object, pending_count: object, cumulative_verified: object,
                   storefront: object, source_observed_at: object,
                   official_readback_observed_at: object = None,
                   provider_event_time: object = None, blocker: object = None,
                   sales: object = None) -> dict[str, object]:
    app = application if isinstance(application, Mapping) else {}
    stages = {key: _int(app.get(key)) for key in ("observed_count", "eligible_count", "verified_count")}
    stages["submitted"] = (0 if isinstance(app, Mapping) and app.get("submitted") is False else 1) if isinstance(app, Mapping) and isinstance(app.get("submitted"), bool) else None
    stages["reason"] = app.get("reason") if isinstance(app.get("reason"), str) else None
    stages["project_id"] = app.get("project_id") if isinstance(app.get("project_id"), str) else None
    reason = app.get("error") or app.get("reason")
    app_ok = isinstance(application, Mapping) and all(value is not None for value in stages.values())
    pending, verified = _int(pending_count), _int(cumulative_verified)
    store = _storefront_counts(storefront)
    store_ok = all(_int(store.get(key)) is not None for key, _label, _href in _LABELS)
    healthy_reasons = {"no_eligible_project", "duplicate_project", "provider_reconciled", "daily_quota_reached", "capacity_details_required"}
    resolved_blocker = blocker if isinstance(blocker, str) and blocker else (reason if isinstance(reason, str) and reason not in healthy_reasons else None)
    if resolved_blocker == "submission_uncertain" and pending == 0:
        resolved_blocker = None
    if not resolved_blocker and store.get("error"):
        resolved_blocker = str(store["error"])
    return {
        "application": stages, "pending": pending, "cumulative_verified": verified,
        # Kept beside the stages rather than inside them: stages feeds app_ok, which requires every
        # member to be non-None. The renderer dropped ok entirely, so a pass that reported ok:false
        # was known to be incomplete here and still rendered as a ✅ success to the reader.
        "application_ok": app.get("ok") if isinstance(app.get("ok"), bool) else None,
        "storefront": store, "sales": dict(sales) if isinstance(sales, Mapping) else None, "blocker": resolved_blocker or None,
        "source_observed_at": _timestamp(source_observed_at),
        "official_readback_observed_at": _timestamp(official_readback_observed_at),
        "provider_event_time": _timestamp(provider_event_time),
        "actual_ai_cost": "unknown (meter未接続)",
        "complete": bool(app.get("ok") is True and app_ok and pending is not None and verified is not None and store_ok and source_observed_at and official_readback_observed_at and not resolved_blocker and not store.get("error")),
    }


def render_snapshot(snapshot: Mapping[str, object]) -> str:
    app = snapshot.get("application") if isinstance(snapshot.get("application"), Mapping) else {}
    store = snapshot.get("storefront") if isinstance(snapshot.get("storefront"), Mapping) else {}
    sales = snapshot.get("sales") if isinstance(snapshot.get("sales"), Mapping) else {}
    verified = app.get("verified_count") if type(app.get("verified_count")) is int else None
    pending = snapshot.get("pending") if type(snapshot.get("pending")) is int else None
    blocker = snapshot.get("blocker") if isinstance(snapshot.get("blocker"), str) else None
    incomplete = any(type(app.get(key)) is not int for key in ("observed_count", "eligible_count", "submitted", "verified_count")) or pending is None
    # An application that reported ok:false is not a safe wake. The icon used to ignore it, so a
    # failed pass rendered the same ✅ as a healthy one -- the snapshot already knew (complete is
    # false) but the reader could not see it.
    failed = snapshot.get("application_ok") is False
    icon = "📨" if verified else ("⚠️" if failed or blocker or incomplete or store.get("error") else "✅")
    # A warning that does not name what is wrong reads the same as a healthy wake, which is the
    # ambiguity the shared lane summary already avoids ("⚠️ <code>で完了できませんでした").
    warning = f"{blocker}のため確認が必要です" if blocker else "確認が必要な項目があります"
    headline = f"{verified}件の応募を公式確認しました" if verified else (warning if icon == "⚠️" else "今回の確認を安全に完了しました")

    def count(value: object) -> str:
        return f"{value}件" if type(value) is int else "取得できませんでした"

    sent = app.get("submitted")
    sent_text = f"{sent}件を新しく送信" if type(sent) is int and sent else "新しい応募は送信していません"
    states = " / ".join(f"{label}{count(store.get(key))}" for key, label, _href in _LABELS)
    demand = store.get("demand") if isinstance(store.get("demand"), Mapping) else {}
    funnel = " / ".join(f"{label}{count(demand.get(key))}" for key, label in _DEMAND_LABELS)
    sales_line = (f"交渉: 公式会話{count(sales.get('board_count'))} / 返信必要{count(sales.get('required_reply_count'))} / "
                  f"未読{count(sales.get('unread_count'))} / 月額オファー{count(sales.get('incoming_monthly_offer_count'))} / "
                  f"契約候補{count(sales.get('contract_candidate_count'))}。")
    pipeline = sales.get("proposal_pipeline") if isinstance(sales.get("proposal_pipeline"), Mapping) else {}
    pipeline_line = (f"提案状況: 現在一覧{count(pipeline.get('current_count'))} / 募集中{count(pipeline.get('open_count'))} / "
                     f"選定中{count(pipeline.get('selecting_count'))} / キャンセル{count(pipeline.get('canceled_count'))} / "
                     f"進行中{count(pipeline.get('working_count'))} / 終了{count(pipeline.get('ended_count'))} / その他{count(pipeline.get('unknown_count'))}。"
                     f"累計の公式応募から現在一覧にない提案は{count(pipeline.get('unlisted_receipt_count'))}です。")
    sales_next = {
        "no_reply_required": "今は相手からの返信・仮払いを待っています。",
        "seller_last": "こちらからの返信は済んでおり、次の相手の返答を待っています。",
        "reply_verified": "必要な返信を公式確認しました。次の相手の返答を待っています。",
        "reply_uncertain": "返信結果を確認中です。同じ返信は再送せず、次回は公式履歴だけを確認します。",
        "no_reply_needed": "最新の相手メッセージは返信不要と判断し、不要な送信をしていません。",
    }.get(sales.get("reply_status"), "公式の交渉状態を取得できませんでした。次回もう一度確認します。")
    finance = sales.get("finance") if isinstance(sales.get("finance"), Mapping) else {}
    revenue = (f"収益: 公式入出金履歴{count(finance.get('payment_history_count'))}、口座残高{finance.get('account_balance_jpy')}円、"
               f"今月入金{finance.get('received_gross_jpy')}円、現在net MRR 0円です。今月net revenueとAI処理費はまだ集計していません。"
               if finance.get("source_complete") is True else
               "収益: 公式の入出金記録を完全に確認できていないため、売上とAI処理費は集計していません。応募額と出品価格は売上に含めません。")
    reason = {
        "submission_uncertain": "応募結果の公式確認を待っています。同じ応募は再送せず、次回は公式履歴だけを確認します。",
        "account_lock_busy": "別のLancers処理が動作中のため、今回は公式画面の確認を見送りました。次回もう一度確認します。",
        "storefront_readback_failed": "出品状態を公式画面で確認できませんでした。変更せず、次回もう一度確認します。",
        "listing_readback_mismatch": "出品変更後の公式状態が一致しませんでした。追加変更せず、次回もう一度確認します。",
    }.get(blocker, "公式確認を完了できない項目がありました。外部操作を増やさず、次回もう一度確認します。") if blocker else "新しい案件と公式応募結果を次回も確認します。"
    project_id = app.get("project_id") if isinstance(app.get("project_id"), str) else None
    decision = {
        "duplicate_project": f"案件{project_id}は既に判断済みだったため重複応募せず、次の新着確認へ進みます。" if project_id else "確認した案件は既に判断済みだったため重複応募せず、次の新着確認へ進みます。",
        "no_eligible_project": "今回確認した案件には正直に完遂できる新規候補がなく、応募せず次の新着確認へ進みます。",
        "daily_quota_reached": "本日の安全な応募上限に達したため送信せず、次回も新着と公式結果を確認します。",
    }.get(app.get("reason"))
    return (f"[Lancers][応募・出品] {icon} {headline}\n"
            f"応募: 公開案件は{count(app.get('observed_count'))}確認し、適合候補は{count(app.get('eligible_count'))}、{sent_text}。"
            f"公式確認は{count(verified)}、累計{count(snapshot.get('cumulative_verified'))}、確認待ちは{count(pending)}です。\n"
            + (f"判断: {decision}\n" if decision else "") +
            f"出品: {states}。需要: {funnel}。\n"
            f"{pipeline_line}\n{sales_line}{sales_next}\n"
            f"{revenue}\n"
            f"次: {reason}")


def semantic_hash(snapshot: Mapping[str, object]) -> str:
    value = {key: snapshot.get(key) for key in ("application", "pending", "cumulative_verified", "storefront", "sales", "blocker", "actual_ai_cost", "complete")}
    value["storefront"] = {key: value["storefront"].get(key) for key, _label, _href in _LABELS} | {"error": value["storefront"].get("error"), "demand": value["storefront"].get("demand")} if isinstance(value["storefront"], Mapping) else None
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _jst_day(value: object) -> str:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    return parsed.astimezone(TOKYO).date().isoformat()


def enqueue_snapshot(database: Path, snapshot: Mapping[str, object], now: object) -> bool:
    wake = snapshot.get("application_wake_sequence")
    wake_key = str(wake) if type(wake) is int and wake > 0 else "unknown"
    key = f"lancers:human:v2:{_jst_day(now)}:{wake_key}:{semantic_hash(snapshot)}"
    try:
        return bool(outbox.enqueue(Path(database), key, render_snapshot(snapshot), _timestamp(now) or "unknown"))
    except outbox.IdempotencyConflict:
        return False


def _provider_id(value: object) -> Optional[str]:
    if isinstance(value, SendResult):
        value = value.provider_message_id
    elif isinstance(value, Mapping):
        payload = value
        value = payload.get("messageId", payload.get("message_id", payload.get("id")))
        if value is None and isinstance(payload.get("payload"), Mapping):
            nested = payload["payload"]
            value = nested.get("messageId", nested.get("message_id", nested.get("id")))
        if value is None and isinstance(payload.get("result"), Mapping):
            result = payload["result"]
            value = result.get("messageId", result.get("message_id", result.get("id")))
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()) and int(value.strip()) > 0:
        return value.strip()
    return None


def _provider_payload(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character not in "{[": continue
            try: return decoder.raw_decode(text, index)[0]
            except json.JSONDecodeError: continue
    raise ValueError("provider_response_invalid")


def deliver_pending(database: Path, notifier: Callable[[str], object], now: object) -> DeliveryResult:
    """Drain the outbox through the shared loop.

    This function used to be a private copy of it. The copy claimed and resolved without passing the
    claim back, so a worker that lost its row to reclaim_stale could still resolve it -- the race
    _shared/marketplace-core/scripts/telegram_outbox.py now fences. Only the SendResult shape is
    Lancers-specific, so only the shape is adapted here.
    """
    def send(message: str) -> object:
        sent = notifier(message)
        started = sent.attempted if isinstance(sent, SendResult) else True
        error = sent.error_code if isinstance(sent, SendResult) else "receipt_missing"
        return shared_delivery.SendResult(started, _provider_id(sent), error)

    delivered = shared_delivery.deliver_pending(
        outbox, Path(database), send, _timestamp(now) or "unknown", limit=20,
    )
    return DeliveryResult(
        delivered.attempted, delivered.delivered,
        delivered.delivery_uncertain, delivered.pre_send_failed,
    )


def render_application_wake(result: Mapping[str, object]) -> str:
    observed = int(result.get("observed_count") or 0)
    eligible = int(result.get("eligible_count") or 0)
    verified = int(result.get("verified_count") or 0)
    today_verified, cumulative_verified = _application_totals()
    today_text = f"{today_verified}件" if today_verified is not None else "不明"
    cumulative_text = f"{cumulative_verified}件" if cumulative_verified is not None else "不明"
    already_decided = int(result.get("already_decided_count") or 0)
    project_id = str(result.get("project_id") or "").strip()
    if result.get("application_verified") is True:
        outcome = f"{verified or 1}件の応募を公式確認しました"
    elif result.get("reason") == "duplicate_project":
        outcome = f"案件{project_id}は応募済みのためスキップしました" if project_id else "応募済み案件をスキップしました"
    elif result.get("reason") == "no_eligible_project":
        outcome = "適合する新規案件がなかったため送信しませんでした"
    elif result.get("reason"):
        outcome = f"{result['reason']}のため送信しませんでした"
    else:
        outcome = f"{result.get('error') or 'unknown_error'}で完了できませんでした"
    decision_reports = result.get("decision_reports") or []
    skipped_reports = [item for item in decision_reports if isinstance(item, Mapping) and item.get("outcome") == "failed"]
    skip_reasons = sorted({str(item.get("error") or "unknown") for item in skipped_reports})
    skip_text = f" / skip{len(skipped_reports)}件({','.join(skip_reasons)})" if skipped_reports else ""
    # Same sentence, one implementation: CrowdWorks renders its wake through this too, so the two
    # lanes cannot drift into two different reports of the same kind of event.
    return summary.render_lane_summary(
        platform_display_name="Lancers",
        lane="応募",
        icon="📨" if result.get("application_verified") else "⏭️" if result.get("ok") else "⚠️",
        headline=outcome,
        counts=[
            ("公開案件", observed),
            ("既判断", already_decided),
            (f"fresh判断", len(decision_reports)),
            *([("skip", (len(skipped_reports), ",".join(skip_reasons)))] if skipped_reports else []),
            ("応募候補", eligible),
            ("公式確認", verified),
        ],
        today_verified=today_verified,
        total_verified=cumulative_verified,
        next_action="5分後のwakeで新着案件の確認と最大positive-EV応募を続けます。",
    )


def _application_totals() -> tuple[Optional[int], Optional[int]]:
    try:
        events = _load("_anicca_lancers_application_totals", LEDGER_PATH).list_events(LEDGER_DATABASE)
        stamps = []
        for event in events:
            kind = event.get("event_type") if isinstance(event, Mapping) else getattr(event, "event_type", None)
            stamp = event.get("occurred_at") if isinstance(event, Mapping) else getattr(event, "occurred_at", None)
            if kind == "application_verified" and isinstance(stamp, str):
                stamps.append(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
        today = datetime.now(TOKYO).date()
        return sum(stamp.astimezone(TOKYO).date() == today for stamp in stamps), len(stamps)
    except Exception:
        return None, None


def render_application_decision(decision: Mapping[str, object]) -> str:
    project_id = str(decision.get("project_id") or "不明")
    title = str(decision.get("title") or "案件名を取得できませんでした").strip()
    business_class = str(decision.get("business_class") or "unknown")
    reasons = decision.get("reason_codes") if isinstance(decision.get("reason_codes"), list) else []
    outcome_value = str(decision.get("outcome") or "")
    if outcome_value == "application_verified":
        outcome = "📨 応募を公式確認しました"
        explanation = f"Lancersのproposal ID {decision.get('provider_proposal_id')} を公式履歴で確認しました。"
    elif business_class == "hard_prohibited":
        outcome = "🚫 応募しません"
        explanation = f"募集文の「{reasons[1]}」が、対応できない必須条件（{reasons[0]}）に当たるためです。" if len(reasons) > 1 else "対応できない必須条件があるためです。"
    elif business_class == "skip_not_fit":
        outcome = "⏭️ 今回は応募しません"
        explanation = " / ".join(str(value) for value in reasons) or "募集内容と提供可能な仕事が一致しないためです。"
    elif outcome_value == "provider_terminal_blocked":
        outcome = "⏭️ 公式に応募できないためスキップしました"
        explanation = "公式応募フォームが受付可能な状態ではなかったため、外部送信していません。"
    elif outcome_value == "failed":
        outcome = "⚠️ 応募を公式確認できませんでした"
        explanation = f"{decision.get('error') or 'submission_unverified'} のため、完了とは数えていません。"
    else:
        outcome = "⚠️ 応募判断後の送信結果が未確定です"
        explanation = "応募候補ですが、公式proposal receiptをまだ確認できていません。"
    # What was applied for, and for how much. Dais 2026-09-07: the report has to carry the price
    # and what the job is, or an application is indistinguishable from a skip in the chat.
    price = decision.get("price_jpy")
    amount = f"\n提案額: {int(price):,}円" if isinstance(price, int) and not isinstance(price, bool) and price > 0 else ""
    proposal_id = decision.get("provider_proposal_id")
    receipt = f"\nProposal ID: {proposal_id}" if proposal_id else ""
    return (
        f"[Lancers][応募判断] {outcome}\n"
        f"案件: {title}\n案件ID: {project_id}{receipt}{amount}\n理由: {explanation}\n"
        "次: 同じwake内で次の案件の判断と応募を続けます。"
    )


def notify_application_wake(
    result: Mapping[str, object], *, database: Path = DATABASE,
    notifier: Optional[Callable[[str], object]] = None, now: object = None,
) -> DeliveryResult:
    moment = now or datetime.now(timezone.utc).isoformat()
    decisions = result.get("decision_reports") if isinstance(result.get("decision_reports"), list) else []
    for decision in decisions:
        if not isinstance(decision, Mapping):
            continue
        project_id = str(decision.get("project_id") or "").strip()
        if not project_id:
            continue
        digest = hashlib.sha256(json.dumps(decision, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        outbox.enqueue(Path(database), f"lancers:application-decision:v1:{project_id}:{digest}", render_application_decision(decision), _timestamp(moment) or "unknown")
    message = render_application_wake(result)
    identity = hashlib.sha256(
        (str(_timestamp(moment)) + "\0" + json.dumps(result, sort_keys=True, separators=(",", ":"))).encode()
    ).hexdigest()
    outbox.enqueue(Path(database), f"lancers:application-wake:v1:{identity}", message, _timestamp(moment) or "unknown")
    return deliver_pending(Path(database), notifier or _default_notifier, moment)


def _notify_lane_wake(
    lane: str, result: Mapping[str, object], message: str, *,
    database: Path = DATABASE, notifier: Optional[Callable[[str], object]] = None,
    now: object = None,
) -> DeliveryResult:
    moment = now or datetime.now(timezone.utc).isoformat()
    identity = hashlib.sha256(
        (lane + "\0" + str(_timestamp(moment)) + "\0" + json.dumps(result, sort_keys=True, separators=(",", ":"))).encode()
    ).hexdigest()
    outbox.enqueue(Path(database), f"lancers:{lane}-wake:v1:{identity}", message, _timestamp(moment) or "unknown")
    return deliver_pending(Path(database), notifier or _default_notifier, moment)


def notify_storefront_wake(result: Mapping[str, object], **kwargs: object) -> DeliveryResult:
    action = str(result.get("action") or result.get("error") or "unknown")
    demand = result.get("demand") if isinstance(result.get("demand"), Mapping) else {}
    message = (
        f"[Lancers][出品] {'✅' if result.get('ok') else '⚠️'} {action}\n"
        f"商品: {result.get('canonical_url') or '公式URLを取得できませんでした'}\n"
        f"需要: 検索表示{int(demand.get('search_impressions') or 0)}件 / 詳細閲覧{int(demand.get('detail_views') or 0)}件 / "
        f"お気に入り{int(demand.get('favorites') or 0)}件 / 相談{int(demand.get('inquiries') or 0)}件 / 注文{int(demand.get('orders') or 0)}件。\n"
        "次: 次の出品wakeで公式状態と需要を再確認します。"
    )
    return _notify_lane_wake("storefront", result, message, **kwargs)


def notify_work_sync_wake(result: Mapping[str, object], **kwargs: object) -> DeliveryResult:
    reply = result.get("reply_action") if isinstance(result.get("reply_action"), Mapping) else {}
    finance = result.get("finance") if isinstance(result.get("finance"), Mapping) else {}
    message = (
        f"[Lancers][交渉・収益] {'✅' if result.get('ok') else '⚠️'} {reply.get('status') or result.get('error') or '確認完了'}\n"
        f"交渉: 公式会話{int(result.get('board_count') or 0)}件 / 返信必要{int(result.get('required_reply_count') or 0)}件 / "
        f"未読{int(result.get('unread_count') or 0)}件 / 契約候補{int(result.get('contract_candidate_count') or 0)}件。\n"
        f"収益: 入出金履歴{int(finance.get('payment_history_count') or 0)}件 / 残高{int(finance.get('account_balance_jpy') or 0)}円 / "
        f"入金{int(finance.get('received_gross_jpy') or 0)}円。\n"
        "次: 次のwakeで返信・契約・仮払い・入金を再確認します。"
    )
    return _notify_lane_wake("work-sync", result, message, **kwargs)


def notify_negotiate_wake(result: Mapping[str, object], **kwargs: object) -> DeliveryResult:
    message = (
        f"[Lancers][交渉] ✅ {result.get('reply_status') or '公式状態を確認しました'}\n"
        f"公式会話{int(result.get('board_count') or 0)}件 / 返信必要{int(result.get('required_reply_count') or 0)}件 / "
        f"未読{int(result.get('unread_count') or 0)}件 / 月額オファー{int(result.get('incoming_monthly_offer_count') or 0)}件 / "
        f"契約候補{int(result.get('contract_candidate_count') or 0)}件。\n"
        "次: buyer-lastのchanged threadだけを判断し、返信・見積・契約条件を公式確認します。"
    )
    return _notify_lane_wake("negotiate", result, message, **kwargs)


def notify_paid_wake(result: Mapping[str, object], **kwargs: object) -> DeliveryResult:
    finance = result.get("finance") if isinstance(result.get("finance"), Mapping) else {}
    message = (
        "[Lancers][納品・入金] ✅ 公式状態を確認しました\n"
        f"稼働中project{int(result.get('project_working_count') or 0)}件 / 月額契約{int(result.get('monthly_contract_count') or 0)}件 / "
        f"入出金履歴{int(finance.get('payment_history_count') or 0)}件 / 残高{int(finance.get('account_balance_jpy') or 0)}円 / "
        f"入金{int(finance.get('received_gross_jpy') or 0)}円。\n"
        "次: 仮払い済み契約だけを制作し、QA・正式納品・検収・PaymentReceiptを追跡します。"
    )
    return _notify_lane_wake("paid", result, message, **kwargs)


def _pending_count(path: Path) -> Optional[int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or not isinstance(value.get("fingerprints"), list):
            return None
        if set(value) not in ({"fingerprints"}, {"fingerprints", "pending"}): return None
        fingerprints = value["fingerprints"]
        if not isinstance(fingerprints, list) or any(not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None for item in fingerprints): return None
        pending = value.get("pending", {})
        if not isinstance(pending, Mapping) or any(not isinstance(marker, str) or marker not in fingerprints or not isinstance(item, Mapping) for marker, item in pending.items()): return None
        return len(pending)
    except (OSError, ValueError, TypeError):
        return None


def _verified(events: object) -> tuple[int, Optional[str]]:
    rows = events if isinstance(events, Sequence) and not isinstance(events, (str, bytes)) else ()
    values = [item for item in rows if (item.get("event_type") if isinstance(item, Mapping) else getattr(item, "event_type", None)) == "application_verified"]
    stamps = [item.get("observed_at") if isinstance(item, Mapping) else getattr(item, "observed_at", None) for item in values]
    stamps = [item for item in stamps if isinstance(item, str) and item]
    return len(values), max(stamps) if stamps else None


def _source_error(error: BaseException) -> str:
    return "account_lock_busy" if "LockBusy" in type(error).__name__ else "storefront_readback_failed"


def collect_snapshot(*, application_log: Path, state_path: Path, ledger_database: Path, storefront: object = None, storefront_log: Path = STOREFRONT_LOG, now: object = None, ledger_events: object = None) -> dict[str, object]:
    del storefront_log
    observed, wake_sequence = read_application_wake(application_log)
    source_time = _source_timestamp(observed)
    events = ledger_events
    if events is None:
        try:
            events = _load("lancers_report_ledger", LEDGER_PATH).list_events(Path(ledger_database))
        except Exception:
            events = ()
    verified, official = _verified(events)
    if storefront is None:
        try:
            storefront = read_storefront(Path(state_path))
        except Exception as error:
            storefront = {"error": _source_error(error)}
    if isinstance(storefront, Mapping): storefront = dict(storefront) | {"demand": _listing_demand(Path(state_path).with_name("listing.json"))}
    return build_snapshot(application=observed, pending_count=_pending_count(Path(state_path)), cumulative_verified=verified, storefront=storefront, sales=_sales_snapshot(Path(state_path).with_name("contracts.json")), source_observed_at=source_time, official_readback_observed_at=official) | {"application_wake_sequence": wake_sequence}


def _default_notifier(message: str) -> SendResult:
    try:
        telegram = _load("_anicca_shared_telegram", TELEGRAM_PATH)
        client = telegram.TelegramClient.from_env(
            environ={"TELEGRAM_CHAT_ID": TARGET},
            env_file=Path.home() / ".local/state/life-manager/.env",
        )
        receipt = client.send_text(message)
        ids = receipt.get("message_ids") if isinstance(receipt, Mapping) else None
        provider_id = str(ids[-1]) if isinstance(ids, list) and ids else None
        return SendResult(True, provider_id, "receipt_missing" if provider_id is None else None)
    except Exception as error:
        attempted = type(error).__name__ == "TelegramDeliveryUnknown"
        return SendResult(attempted, None, "transport_unknown" if attempted else "direct_transport_unavailable")


def main(argv: Optional[Sequence[str]] = None, *, notifier: Optional[Callable[[str], object]] = None, stdout: Any = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--json", action="store_true")
    mode.add_argument("--inventory-json", action="store_true")
    parser.add_argument("--inventory-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database", default=str(DATABASE)); parser.add_argument("--ledger-database", default=str(LEDGER_DATABASE)); parser.add_argument("--state-path", default=str(STATE)); parser.add_argument("--application-log", default=str(STATE.parent / "logs/application.out.log")); parser.add_argument("--storefront-log", default=str(STOREFRONT_LOG)); parser.add_argument("--now")
    out = sys.stdout if stdout is None else stdout
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
        if args.inventory_worker and not args.inventory_json:
            raise InventoryFailure("inventory_mode_invalid")
        if args.inventory_json:
            payload = run_inventory(state_path=Path(args.state_path)) if args.inventory_worker else _run_inventory_parent(args.state_path)
        else:
            now = args.now or datetime.now(timezone.utc).isoformat(); snapshot = collect_snapshot(application_log=Path(args.application_log), state_path=Path(args.state_path), ledger_database=Path(args.ledger_database), storefront_log=Path(args.storefront_log), now=now); enqueued = int(enqueue_snapshot(Path(args.database), snapshot, now)); delivery = deliver_pending(Path(args.database), notifier or _default_notifier, now)
            payload = {"ok": delivery.delivery_uncertain == 0, "enqueued": enqueued, "attempted": delivery.attempted, "delivered": delivery.delivered, "delivery_uncertain": delivery.delivery_uncertain, "pre_send_failed": delivery.pre_send_failed}
    except Exception as exc:
        payload = {"ok": False, "error": re.sub(r"[^a-z0-9_]", "_", type(exc).__name__.lower())}
    out.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"); out.flush()
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
