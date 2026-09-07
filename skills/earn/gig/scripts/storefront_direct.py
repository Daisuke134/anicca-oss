#!/usr/bin/env python3
"""Observe one Coconala Storefront wake without the legacy Hermes/B0 path."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlsplit

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from telegram_outbox import TelegramOutbox, dispatch_one  # noqa: E402
from owner_notify import send_email_if_configured  # noqa: E402
from gig_paths import (  # noqa: E402
    BROWSER_DIR, GIG_DIR, HOST_STATE_DIR, LISTING_CATALOG, RUNNER_DIR, STATE_DIR,
)
from gig_disk_guard import disk_headroom_ok  # noqa: E402
import evidence_gc  # noqa: E402

DEFAULT_STATE = STATE_DIR / "storefront-direct"
DEFAULT_BRAKE = HOST_STATE_DIR / "gig-work" / "storefront.operator.brake"
DEFAULT_LEASE = BROWSER_DIR / "scripts" / "cdp_context_lease.py"
DEFAULT_TAB = BROWSER_DIR / "scripts" / "cdp_default_tab.py"
DEFAULT_ENSURE_BROWSER = BROWSER_DIR / "ensure_browser.sh"
DEFAULT_RUNNER = RUNNER_DIR / "agent_runner.py"
DEFAULT_SCHEMA = GIG_DIR / "schemas" / "storefront_judgement.schema.json"
DEFAULT_PROPOSAL_SCHEMA = GIG_DIR / "schemas" / "storefront_proposal.schema.json"
DEFAULT_CREATE_PROPOSAL_SCHEMA = GIG_DIR / "schemas" / "storefront_create_proposal.schema.json"
DEFAULT_DEMAND_PROPOSAL_SCHEMA = GIG_DIR / "schemas" / "storefront_demand_proposal.schema.json"
DEFAULT_CATEGORY_PROPOSAL_SCHEMA = GIG_DIR / "schemas" / "storefront_category_proposal.schema.json"
DEFAULT_FACET_PROPOSAL_SCHEMA = GIG_DIR / "schemas" / "storefront_facet_proposal.schema.json"
DEFAULT_CATEGORY_CHILD_SCHEMA = GIG_DIR / "schemas" / "storefront_category_child.schema.json"
DEFAULT_BOOTSTRAP_SELECTION_SCHEMA = GIG_DIR / "schemas" / "storefront_bootstrap_selection.schema.json"
DEFAULT_BOOTSTRAP_LISTING_SCHEMA = GIG_DIR / "schemas" / "storefront_bootstrap_listing.schema.json"
DEFAULT_BOOTSTRAP_IMPORT_SCHEMA = GIG_DIR / "schemas" / "storefront_bootstrap_import.schema.json"
DEFAULT_DUPLICATE_JUDGEMENT_SCHEMA = GIG_DIR / "schemas" / "storefront_duplicate_judgement.schema.json"
DEFAULT_STOREFRONT_ROOT = Path(
    os.environ.get("GIG_STOREFRONT_ROOT") or "/nonexistent/storefront-root-required"
)
DEFAULT_SCORECARD = DEFAULT_STOREFRONT_ROOT / "scorecard.json"
MEASURABLE_SUCCESS_METRICS = {"inquiries", "purchases", "views_to_inquiry", "views_to_purchase",
                              "net_receipt"}
DEFAULT_REPLY_TRANSCRIPTS = Path.home() / "gig" / "reply-transcripts.jsonl"
DEFAULT_APPLIED = Path.home() / "gig" / "applied.jsonl"
DEFAULT_EARNINGS = Path.home() / "gig" / "earnings.jsonl"
DEFAULT_PROJECTS = Path.home() / "gig" / "projects"
# The negotiate lane runs from a separate runtime, so its log path is machine configuration rather
# than source. Operators point at it with GIG_NEGOTIATE_RUN_LOG or --negotiate-run-log.
DEFAULT_NEGOTIATE_RUN_LOG = Path(
    os.environ.get("GIG_NEGOTIATE_RUN_LOG")
    or Path.home() / "gig" / "logs" / "gig-reply-detector-launchd.out.log"
)
DEFAULT_TELEGRAM_DATABASE = Path.home() / "gig" / "telegram-outbox.sqlite3"
DEFAULT_TELEGRAM_RECEIPTS = Path.home() / "gig" / "telegram-delivery-receipts"
STATE_FILES = (
    "effects.jsonl", "experiments.jsonl", "offer-contracts.jsonl", "attribution-map.jsonl",
    "analytics.jsonl", "outcomes.jsonl", "prepared-hypotheses.jsonl", "listing-contracts.jsonl",
    "new-listing-drafts.jsonl", "funnel-events.jsonl", "portfolio-allocations.jsonl",
    "demand-evidence.jsonl", "demand-dismissals.jsonl", "superseded-candidates.jsonl",
    "demand-category.jsonl", "demand-category-options.jsonl",
)
TARGET_SERVICE_ID = os.environ.get("GIG_STOREFRONT_TARGET_SERVICE_ID", "91000001").strip()
GALLERY_SERVICE_ID = os.environ.get("GIG_STOREFRONT_GALLERY_SERVICE_ID", "91000002").strip()
PRESENTATION_SERVICE_ID = os.environ.get("GIG_STOREFRONT_PRESENTATION_SERVICE_ID", "91000004").strip()
SCOPE_SERVICE_ID = os.environ.get("GIG_STOREFRONT_SCOPE_SERVICE_ID", "91000005").strip()
DEFAULT_IMAGE_CONTRACT = DEFAULT_STOREFRONT_ROOT / "assets" / "image-contract.json"
DEFAULT_GALLERY_CONTRACT = DEFAULT_STOREFRONT_ROOT / "assets" / "gallery-contract.json"
DEFAULT_TITLE_MUTATION = DEFAULT_STOREFRONT_ROOT / "contracts" / "mutations" / "title.json"
DEFAULT_BODY_MUTATION = DEFAULT_STOREFRONT_ROOT / "contracts" / "mutations" / "body.json"
DEFAULT_SCOPE_MUTATION = DEFAULT_STOREFRONT_ROOT / "contracts" / "mutations" / "scope.json"
DEFAULT_PACKAGE_MUTATION = DEFAULT_STOREFRONT_ROOT / "contracts" / "mutations" / "package.json"
DEFAULT_FAQ_MUTATION = DEFAULT_STOREFRONT_ROOT / "contracts" / "mutations" / "faq.json"
DEFAULT_PRICE_MUTATION = DEFAULT_STOREFRONT_ROOT / "contracts" / "mutations" / "price.json"
DEFAULT_LISTING_CONTRACT_DIR = DEFAULT_STOREFRONT_ROOT / "contracts" / "listings"
DEFAULT_LISTING_CONTRACT_FAMILIES = DEFAULT_STOREFRONT_ROOT / "families.json"
DEFAULT_NEW_LISTING_CONTRACT = DEFAULT_STOREFRONT_ROOT / "contracts" / "new-listing.json"


def _collect_storefront_evidence(state_dir: Path, current_evidence_dir: Path) -> None:
    evidence_gc.collect(
        state_dir=state_dir,
        evidence_root=state_dir / "evidence",
        current_evidence_dir=current_evidence_dir,
    )


def _walk_json_items(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk_json_items(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_items(child)


def _capability_evidence_defaults() -> list[Path]:
    """Read operator-owned capability evidence without shipping its paths."""
    raw = os.environ.get("GIG_STOREFRONT_CAPABILITY_EVIDENCE", "")
    return [Path(item) for item in raw.split(os.pathsep) if item]


def _asset_reference(path: str) -> str:
    asset = Path(path).resolve()
    return str(asset.relative_to(GIG_DIR.resolve())) if asset.is_relative_to(GIG_DIR.resolve()) else str(asset)


def _storefront_paths() -> dict[str, Path]:
    """Bind an explicitly configured seller bundle before the browser is leased."""
    configured = os.environ.get("GIG_STOREFRONT_ROOT", "").strip()
    if not configured:
        return {
            "scorecard": DEFAULT_SCORECARD, "families": DEFAULT_LISTING_CONTRACT_FAMILIES,
            "listings": DEFAULT_LISTING_CONTRACT_DIR, "new_listing": DEFAULT_NEW_LISTING_CONTRACT,
            "image": DEFAULT_IMAGE_CONTRACT, "gallery": DEFAULT_GALLERY_CONTRACT,
            "title": DEFAULT_TITLE_MUTATION, "body": DEFAULT_BODY_MUTATION,
            "scope": DEFAULT_SCOPE_MUTATION, "package": DEFAULT_PACKAGE_MUTATION,
            "faq": DEFAULT_FAQ_MUTATION, "price": DEFAULT_PRICE_MUTATION,
        }
    try:
        root = Path(configured).expanduser().resolve(strict=True)
    except OSError as error:
        raise RuntimeError("storefront_root_invalid") from error
    if not root.is_dir():
        raise RuntimeError("storefront_root_invalid")

    def inside(path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise RuntimeError("storefront_root_invalid") from error
        if not resolved.is_relative_to(root):
            raise RuntimeError("storefront_root_invalid")
        return resolved

    paths = {
        "scorecard": root / "scorecard.json", "families": root / "families.json",
        "listings": root / "contracts" / "listings", "new_listing": root / "contracts" / "new-listing.json",
        "image": root / "assets" / "image-contract.json",
        "gallery": root / "assets" / "gallery-contract.json",
        **{field: root / "contracts" / "mutations" / f"{field}.json"
           for field in ("title", "body", "scope", "package", "faq", "price")},
    }
    paths = {name: inside(path) for name, path in paths.items()}
    if (not paths["listings"].is_dir() or not inside(root / "assets").is_dir()
            or any(not path.is_file() for name, path in paths.items() if name != "listings")):
        raise RuntimeError("storefront_root_invalid")
    checked: set[Path] = set()

    def check_assets(contract: Path) -> None:
        if contract in checked:
            return
        checked.add(contract)
        try:
            document = json.loads(contract.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("storefront_root_invalid") from error
        for key, value in _walk_json_items(document):
            if key not in {"asset", "asset_path", "hero_image_contract"} or not isinstance(value, str):
                continue
            relative = Path(value)
            try:
                candidate = (contract.parent / relative).resolve(strict=True)
            except OSError as error:
                raise RuntimeError("storefront_root_asset_invalid") from error
            if (relative.is_absolute() or not candidate.is_relative_to(root) or not candidate.is_file()):
                raise RuntimeError("storefront_root_asset_invalid")
            if candidate.suffix == ".json":
                check_assets(candidate)

    listings = [inside(path) for path in paths["listings"].rglob("*.json")]
    for contract in [path for name, path in paths.items() if name != "listings"] + listings:
        check_assets(contract)
    return paths
JUDGEMENT_FIELDS = {
    "decision", "service_id", "changed_field", "before_value", "proposed_value",
    "hypothesis", "competitor_evidence_paths", "capability_evidence_paths",
    "success_metric", "observation_window_days", "no_op_reason", "experiment_key", "uncertainty",
}
FAQ_PATTERN = re.compile(
    r"(?:よくある質問\s*)?Q[.．]\s*(?P<question>.+?)\s*\n+A[.．]\s*(?P<answer>.+)\Z",
    re.DOTALL,
)
SELLER_FORM_EXPRESSION = r'''JSON.stringify((()=>{const form=document.forms[0];return{url:location.href,action:form?.action||null,method:form?.method||null,fields:form?[...form.elements].filter(e=>e.name).map(e=>({name:e.name,type:e.type||null,value:e.value||'',checked:!!e.checked,maxLength:Number.isInteger(e.maxLength)&&e.maxLength>=0?e.maxLength:null})):[],select_options:form?Object.fromEntries([...form.elements].filter(e=>e.name&&e.tagName==='SELECT').map(e=>[e.name,[...e.options].map(o=>({value:o.value,label:(o.textContent||'').trim()}))])):{},submit_controls:form?[...form.querySelectorAll('button[type=submit],input[type=submit]')].map(e=>({mode:e.dataset?e.dataset.mode||null:null,label:((e.innerText||e.value||'')+'').trim(),disabled:!!e.disabled})):[],listing_state_controls:[...document.querySelectorAll('a,button,[role=button],[role=menuitem]')].filter(e=>/非公開|公開停止|公開を停止|下書きに戻す|停止する|削除/.test((e.innerText||'').trim())).slice(0,20).map(e=>({tag:e.tagName,label:(e.innerText||'').trim().slice(0,40),href:e.getAttribute('href')||null,id:e.id||null,cls:(e.className||'')+''}))}})())'''
COMPETITOR_SOURCES = (
    ("category", "https://coconala.com/categories/230/66"),
    ("search", "https://coconala.com/search?keyword=%E6%A5%AD%E5%8B%99%E8%87%AA%E5%8B%95%E5%8C%96"),
    ("service", "https://coconala.com/services/2475514"),
    ("service", "https://coconala.com/services/1991922"),
    ("service", "https://coconala.com/services/3933104"),
    ("service", "https://coconala.com/services/2200084"),
    ("service", "https://coconala.com/services/3122692"),
    ("service", "https://coconala.com/services/3741646"),
    # Widened 2026-09-05: the six sources above covered one automation subcategory only.
    # These add IT相談・システム開発's own subcategories (システム開発・制作,
    # システム保守・運用代行) and the top verified seller by lifetime sales in each,
    # measured live at coconala.com before being added here.
    ("category", "https://coconala.com/categories/232"),
    ("category", "https://coconala.com/categories/237"),
    ("service", "https://coconala.com/services/2141574"),
    ("service", "https://coconala.com/services/1786841"),
    ("service", "https://coconala.com/services/1263913"),
    ("service", "https://coconala.com/services/1196016"),
)
MUTATION_FIELDS = {"image", "title", "catchphrase", "body", "package", "FAQ", "price", "listing_state"}
GENERATED_MUTATION_FIELDS = {"image", "title", "catchphrase", "body", "package", "FAQ", "price"}
# The seller listing-state control is the form's own hidden `mode` field, not a `data[...]` input.
LISTING_STATE_DELTA = "mode"
PUBLIC_LISTING_STATE = "公開中"
# Coconala's own per-service "受付休止" toggle (data-publish-links stop_fg=1) freezes new
# orders without unpublishing or archiving the listing -- a known, recoverable listing
# state distinct from 公開中/非公開/下書き, not a platform error. Measured 2026-09-04 on
# /mypage/services_lists: the card control at stop_fg==1 renders a plain anchor
# `<a href="/services/reopen/<id>">受付を再開する</a>` (no js_change-open-status class, no
# confirmation modal) -- see _reopen_suspended_listings.
SUSPENDED_LISTING_STATE = "受付休止中"
MUTATION_CONTRACT_FIELDS = {
    "version", "platform", "service_id", "precondition_listing_version_sha256",
    "changed_field", "before_value", "proposed_value", "allowed_delta", "rollback_value",
    "official_readback", "success_metric", "observation_window_days", "capability_family",
    "evidence", "contract_sha256",
}


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def _append(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _display_count(value: object) -> str:
    return str(value) if type(value) is int and value >= 0 else "不明"


def _display_delta(value: object) -> str:
    return f"{value:+d}" if type(value) is int else "不明"


def _display_money(value: object) -> str:
    return f"¥{value:,.0f}" if type(value) in {int, float} else "不明"


def _report_message(row: dict) -> str:
    failed = int(row.get("status") == "failed")
    hypothesis = row.get("next_hypothesis") if isinstance(row.get("next_hypothesis"), dict) else {}
    contract_delta = row.get("listing_contracts_appended")
    stale_contracts = [entry for entry in (row.get("stale_listing_contracts") or [])
                       if isinstance(entry, dict)]
    catalog = row.get("catalog_analytics") if isinstance(row.get("catalog_analytics"), dict) else {}
    totals = catalog.get("totals") if isinstance(catalog.get("totals"), dict) else {}
    changes = catalog.get("changes") if isinstance(catalog.get("changes"), dict) else {}
    metric_unknown = (
        catalog.get("metric_unknown_services")
        if isinstance(catalog.get("metric_unknown_services"), dict) else {}
    )
    draft = row.get("new_listing_draft") if isinstance(row.get("new_listing_draft"), dict) else {}
    funnel = row.get("funnel") if isinstance(row.get("funnel"), dict) else {}
    by_origin = funnel.get("by_origin") if isinstance(funnel.get("by_origin"), dict) else {}
    storefront_funnel = by_origin.get("storefront", {})
    apply_funnel = by_origin.get("apply", {})
    unknown_funnel = by_origin.get("unknown", {})
    funnel_coverage = funnel.get("coverage") if isinstance(funnel.get("coverage"), dict) else {}
    portfolio = row.get("portfolio") if isinstance(row.get("portfolio"), dict) else {}
    portfolio_counts = portfolio.get("counts") if isinstance(portfolio.get("counts"), dict) else {}
    portfolio_selected = portfolio.get("selected") if isinstance(portfolio.get("selected"), dict) else {}
    effect = max(int(row.get("effect") or 0), int(draft.get("effect") or 0),
                 int(draft.get("public_effect") or 0))
    readback = max(int(row.get("readback") or 0), int(draft.get("readback") or 0))
    competitor = row.get("competitor_evidence") if isinstance(row.get("competitor_evidence"), dict) else {}
    competitor_text = (
        f"今回未計測（直近full {_display_count(competitor.get('latest_full_count'))}件）"
        if competitor.get("status") == "not_checked_incremental"
        else _display_count(row.get("competitor_evidence_count")) + "件"
    )
    catalog_status = {
        "current_full": "今回公式確認",
        "last_known_good": "前回公式値",
        "not_checked_incremental": "今回未計測",
        "not_applicable": "対象外",
        "unknown": "不明",
    }.get(catalog.get("status"), "不明")
    catalog_observed_at = catalog.get("observed_at_epoch")
    if type(catalog_observed_at) is int and catalog_observed_at > 0:
        catalog_status += "・確認" + time.strftime(
            "%m/%d %H:%M", time.localtime(catalog_observed_at),
        )
    window = catalog.get("window") if isinstance(catalog.get("window"), dict) else {}
    window_text = (
        f"{window.get('start')}–{window.get('end')}"
        if window.get("complete") is True and window.get("start") and window.get("end") else "期間不明"
    )
    coverage = catalog.get("coverage") if isinstance(catalog.get("coverage"), dict) else {}
    coverage_text = f"{_display_count(coverage.get('observed'))}/{_display_count(coverage.get('expected'))}"
    draft_status = draft.get("status")
    draft_id = (
        "今回未計測" if draft_status == "not_checked_incremental"
        else str(draft.get("draft_service_id")) if draft.get("draft_service_id")
        else "不明"
    )
    draft_image = (
        "今回未計測"
        if draft_status == "not_checked_incremental" and draft.get("image_count") is None
        else _display_count(draft.get("image_count"))
    )
    draft_status_text = {
        "not_checked_incremental": "今回未計測",
        "not_applicable": "対象外",
    }.get(draft_status, str(draft_status) if draft_status else "不明")
    source_errors = funnel.get("unknown_sources")
    source_error_text = (
        ", ".join(source_errors) or "なし" if isinstance(source_errors, list) else "不明"
    )
    coverage_source_text = {
        "latest_completed_log_noncanonical": "Negotiate最新完了log（canonical inventory未提供）",
        "unknown": "不明",
    }.get(funnel_coverage.get("source_status"), "不明")
    selected_text = (
        f"{portfolio_selected.get('service_id')} / "
        f"{portfolio_selected.get('improvement_field')} / "
        f"{portfolio_selected.get('action')}"
        if portfolio_selected.get("service_id")
        and portfolio_selected.get("improvement_field")
        and portfolio_selected.get("action") else "不明"
    )
    hypothesis_executable = hypothesis.get("executable")
    executable_text = (
        f"{hypothesis.get('service_id')} / {hypothesis.get('field')}"
        if hypothesis and hypothesis_executable is True
        else f"なし（{hypothesis.get('guard_reason') or hypothesis.get('reason')}）"
        if hypothesis and hypothesis_executable is False
        else "不明" if failed
        else "なし"
    )
    next_action = (
        "失敗stageを自動修復して同じ境界から再開" if failed
        else (f"{portfolio_selected.get('service_id')}/{portfolio_selected.get('improvement_field')}を"
              f"{portfolio_selected.get('action')}" if portfolio_selected else
              "全出品adapterをversioned mutation contractへ共通化") if draft.get("status") == "already_public"
        else "公式readbackとoutcome ledgerを照合" if effect
        else f"{hypothesis.get('service_id')}/{hypothesis.get('field')}の実行harnessを継続"
        if hypothesis else
        (f"{portfolio_selected.get('service_id')}/{portfolio_selected.get('improvement_field')}のfenceを維持し、"
         "他出品の実行可能gapを選定")
        if portfolio_selected else "scorecard先頭の改善gapを選択"
    )
    lines = [
        "Codex::: 🏪 ココナラ Storefront hourly",
        f"✅ 公式出品 {_display_count(row.get('official_services_read'))}件 / 競合証拠 {competitor_text}",
        (f"📊 actionable {_display_count(row.get('actionable'))} / effect {effect} / "
         f"readback {readback} / duplicate {_display_count(row.get('duplicate'))}"),
        (f"📚 公開contract {_display_count(row.get('listing_contracts_active'))}件 / "
         f"version履歴 {_display_count(row.get('listing_contracts_total'))}件 / "
         f"今回追加 {_display_count(contract_delta)}件"
         # A hand-authored contract binds one exact listing version, and editing the
         # listing is this lane's whole job -- so it retires its own contracts, and
         # with them that listing's inquiry playbook. That was recorded in the wake
         # row and never said out loud, while the line above kept reporting a
         # healthy-looking active count.
         + (f" / ⚠️ 束縛切れ {len(stale_contracts)}件: "
            f"{'/'.join(str(r.get('service_id')) for r in stale_contracts[:4])}"
            if stale_contracts else "")),
        (f"📝 新規出品draft {draft_id} / "
         f"更新 {_display_count(draft.get('effect'))} / 照合 {_display_count(draft.get('readback'))} / "
         f"画像 {draft_image} / 公開 {_display_count(draft.get('public_effect'))} / "
         f"状態 {draft_status_text}"),
        (f"📈 公式分析 {catalog_status} / {window_text} / coverage {coverage_text}: "
         f"閲覧 {_display_count(totals.get('views'))} / 販売 {_display_count(totals.get('purchases'))} / "
         f"お気に入り {_display_count(totals.get('favorites'))} | 前回比 閲覧 {_display_delta(changes.get('views'))} / "
         f"販売 {_display_delta(changes.get('purchases'))} / "
         f"現在値不明 {_display_count(metric_unknown.get('views'))}件 / "
         f"前回比不明 {_display_count(catalog.get('change_unknown_services'))}件"),
        (f"✅ Storefront funnel: 問合せ {_display_count(storefront_funnel.get('inquiries'))} / "
         f"入金 {_display_count(storefront_funnel.get('payments'))} / "
         f"純入金 {_display_money(storefront_funnel.get('net_jpy'))}"),
        (f"✅ Apply funnel: 問合せ {_display_count(apply_funnel.get('inquiries'))} / "
         f"入金 {_display_count(apply_funnel.get('payments'))} / "
         f"純入金 {_display_money(apply_funnel.get('net_jpy'))}"),
        (f"❓ 帰属未確定: 問合せ {_display_count(unknown_funnel.get('inquiries'))} / "
         f"入金 {_display_count(unknown_funnel.get('payments'))} / "
         f"純入金 {_display_money(unknown_funnel.get('net_jpy'))}"),
        (f"🔎 Funnel coverage: transcript/公式 "
         f"{_display_count(funnel_coverage.get('transcript_conversations_ingested'))}/"
         f"{_display_count(funnel_coverage.get('official_negotiate_conversations_observed'))} / "
         f"未収載 {_display_count(funnel_coverage.get('uncovered_official_conversations'))} / "
         f"Storefront問合せ {_display_count(funnel_coverage.get('storefront_inquiries'))} / "
         f"Apply 問合せ {_display_count(funnel_coverage.get('apply_inquiries'))}・入金 "
         f"{_display_count(funnel_coverage.get('apply_payments'))} / "
         f"未帰属 問合せ {_display_count(funnel_coverage.get('unresolved_inquiries'))}・入金 "
         f"{_display_count(funnel_coverage.get('unresolved_payments'))} / "
         f"source {coverage_source_text}"),
        (f"🧭 Portfolio: KEEP {_display_count(portfolio_counts.get('KEEP'))} / "
         f"IMPROVE {_display_count(portfolio_counts.get('IMPROVE'))} / "
         f"RETIRE {_display_count(portfolio_counts.get('RETIRE'))} / "
         f"REPLACE {_display_count(portfolio_counts.get('REPLACE'))} / "
         f"枠 {_display_count((portfolio.get('capacity') or {}).get('used'))}/"
         f"{_display_count((portfolio.get('capacity') or {}).get('limit'))}"),
        ("⚠️ bad: 確定入金0" if storefront_funnel.get("payments") == 0
         else "⚠️ bad: Storefront入金不明" if type(storefront_funnel.get("payments")) is not int
         else "⚠️ bad: なし"),
        f"❌ source file errors: {source_error_text}",
        f"🧪 改善選定 {selected_text} | 実行contract {executable_text}",
        f"🛡️ fence {hypothesis.get('guard_reason') or row.get('reason') or 'なし'}",
        f"🔧 次の一手: {next_action}",
    ]
    accounting = row.get("accounting") if isinstance(row.get("accounting"), dict) else None
    if accounting is not None:
        lines.append(
            f"⚡ hourly / hour {accounting.get('hour')} / day {accounting.get('day')} / "
            f"全KPI更新 {accounting.get('analytics_observed_at_epoch') or 'unknown'}"
        )
    if draft.get("public_url"):
        lines.extend(("✅ 新規SEOサービスは公式公開中", f"🔗 {draft['public_url']}"))
    if failed:
        lines.extend((f"reason: {str(row.get('reason') or 'unknown')[:300]}",
                      f"pass_id: {str(row.get('pass_id') or '')[:200]}"))
    outcome = row.get("outcome")
    if isinstance(outcome, dict):
        lines.extend((
            f"実験判定: {outcome.get('decision') or 'NO_OP'}",
            f"判定理由: {outcome.get('reason') or 'unknown'}",
        ))
    return "\n".join(lines)


def _report_identity(row: dict, message: str) -> tuple[str, str, bool]:
    draft = row.get("new_listing_draft") if isinstance(row.get("new_listing_draft"), dict) else {}
    if int(draft.get("public_effect") or 0) == 1:
        identity = ":".join((str(draft.get("candidate_key") or ""),
                             str(draft.get("contract_sha256") or ""),
                             str(draft.get("draft_service_id") or "")))
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return f"gig:telegram:storefront-public-effect:v1:{digest}", "storefront_public_effect", False
    if int(row.get("readback") or 0) > 0 and row.get("experiment_key"):
        identity = ":".join((str(row.get("service_id") or ""),
                             str(row.get("changed_field") or ""),
                             str(row["experiment_key"])))
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return f"gig:telegram:storefront-effect:v1:{digest}", "storefront_direct_effect", False
    if row.get("status") == "failed":
        return (f"gig:telegram:storefront-failure:v1:{str(row.get('pass_id') or '')}",
                "storefront_direct_failure", False)
    # Runtime remains in the durable receipt, not the idempotent notification.
    # v2 separates stable incremental payloads from the historical runtime-bound key.
    digest = hashlib.sha256(message.encode()).hexdigest()
    key_version = "v3" if isinstance(row.get("accounting"), dict) else "v1"
    return f"gig:telegram:storefront-noop:{key_version}:{digest}", "storefront_direct_noop", True


def _send_telegram(args: argparse.Namespace, message: str, event_key: str) -> str:
    message_id = send_email_if_configured(message, event_key=event_key)
    if message_id is None:
        completed = subprocess.run(
            [str(args.openclaw), "message", "send", "--channel", "telegram",
             "--target", str(args.telegram_target), "--message", message, "--json"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"Telegram transport failed rc={completed.returncode}")
        result = json.loads(completed.stdout)
        payload = result.get("payload") if isinstance(result, dict) else None
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise RuntimeError("Telegram provider rejected message")
        message_id = result.get("messageId") if isinstance(result, dict) else None
        if not message_id and isinstance(payload, dict):
            message_id = payload.get("messageId")
        if not message_id:
            raise RuntimeError("Telegram ACK has no message ID")
    receipt_path = Path(args.telegram_receipt_dir) / (
        hashlib.sha256(event_key.encode()).hexdigest() + ".json"
    )
    _atomic_write(receipt_path, {
        "version": 1, "event_key": event_key,
        "target": os.environ.get("GIG_NOTIFY_EMAIL", "").strip() or str(args.telegram_target),
        "message_sha256": hashlib.sha256(message.encode()).hexdigest(),
        "message_id": str(message_id), "provider_acked_at_epoch_ms": int(time.time() * 1000),
    })
    return str(message_id)


def _dispatch_report(args: argparse.Namespace, row: dict) -> dict:
    message = _report_message(row)
    event_key, kind, suppress = _report_identity(row, message)
    outbox = TelegramOutbox(Path(args.telegram_database))
    report = outbox.enqueue(event_key=event_key, kind=kind, message=message,
                            created_at=int(time.time()), suppress_identical_body=suppress)
    if report.get("suppressed"):
        return {"status": "suppressed", "message_id": report.get("message_id"),
                "event_key": event_key}
    if report["state"] in {"sent", "delivery_unknown"}:
        return {"status": "deduped" if report["state"] == "sent" else "delivery_unknown",
                "message_id": report.get("message_id"), "event_key": event_key}
    delivered = dispatch_one(
        outbox, owner=f"gig-storefront-direct:{row.get('pass_id')}",
        now=lambda: int(time.time()),
        transport=lambda body: _send_telegram(args, body, event_key),
        report_id=int(report["report_id"]),
    )
    return {"status": delivered["status"], "message_id": delivered.get("message_id"),
            "event_key": event_key}


def _persist_receipt(args: argparse.Namespace, output: Path, row: dict) -> dict:
    durable = dict(row)
    durable.setdefault("mode", "incremental" if getattr(args, "incremental", False) else "full")
    if getattr(args, "auto_cadence", False):
        durable["cadence"] = {
            "auto": True,
            "full_interval_seconds": int(getattr(args, "full_interval_seconds", 1800)),
        }
    if hasattr(args, "telegram_database"):
        try:
            durable["telegram"] = _dispatch_report(args, durable)
        except Exception as error:
            durable["telegram"] = {"status": "failed", "error": type(error).__name__}
    _append(args.state_dir / "wakes.jsonl", durable)
    _atomic_write(output, durable)
    return durable


def _operator_brake_status(path: Path | None = None) -> str:
    environment = os.environ.copy()
    if path is not None:
        environment["GIG_OPERATOR_BRAKE_FILE"] = str(path)
        try:
            if not path.exists():
                return "free"
        except OSError:
            return "failed"
    try:
        completed = subprocess.run(
            [str(SCRIPTS / "gig_brake.sh"), "status"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=5, check=False, env=environment,
        )
    except Exception:
        return "failed"
    return {0: "held", 1: "free"}.get(completed.returncode, "failed")


def _effect_gate_reason(args: argparse.Namespace) -> str | None:
    try:
        if not disk_headroom_ok():
            return "disk_pressure"
    except Exception as error:  # fail closed when host policy is unknowable
        return f"disk_preflight_error:{type(error).__name__}"
    brake_status = _operator_brake_status(getattr(args, "operator_brake", None))
    return {
        "held": "operator_brake",
        "free": None,
        "failed": "operator_brake_check_failed",
    }.get(brake_status, "operator_brake_check_failed")


def _persist_effect_block(
    args: argparse.Namespace, output: Path, pass_id: str, checkpoint: str,
) -> dict | None:
    reason = _effect_gate_reason(args)
    if reason is None:
        return None
    return _persist_receipt(
        args, output, _receipt(
            pass_id, status="pending", reason=reason, effect=0, readback=0,
            checkpoint=checkpoint, send_performed=False,
        ),
    )


def _append_effect_once(path: Path, value: dict) -> bool:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError("effect_ledger_invalid") from error
            if row.get("status") == "accepted" and row.get("experiment_key") == value.get("experiment_key"):
                return False
    _append(path, value)
    return True


def _append_key_once(path: Path, field: str, value: dict) -> bool:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                prior = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"{path.stem}_ledger_invalid") from error
            if prior.get(field) == value.get(field):
                return False
    _append(path, value)
    return True


def _append_proposal_rejection(
    state_dir: Path, *, gap_key: str, rejection: str, proposed_value: object, pass_id: str,
) -> None:
    # Moved to skills/_shared/marketplace-core/scripts/storefront_kernel.py
    # (append_proposal_rejection) so a second marketplace can reuse this guard; this stays as a
    # thin alias so every existing call site and test keeps working unchanged.
    return _storefront_kernel().append_proposal_rejection(
        state_dir, gap_key=gap_key, rejection=rejection, proposed_value=proposed_value, pass_id=pass_id,
    )


def _recent_proposal_rejections(state_dir: Path, gap_key: str) -> list[dict]:
    # Moved to storefront_kernel.recent_proposal_rejections; see _append_proposal_rejection.
    return _storefront_kernel().recent_proposal_rejections(state_dir, gap_key)


def _rejection_guard_name(rejection: object) -> str:
    # Moved to storefront_kernel.rejection_guard_name; see _append_proposal_rejection.
    return _storefront_kernel().rejection_guard_name(rejection)


def _running_release_epoch() -> int | None:
    """When the release this wake is running from was cut, if it can be read.

    Used to decide which past rejections still describe the generator that exists now.
    Answers None on anything unreadable, so an unknown configuration age never silently
    discards evidence.
    """
    root = os.environ.get("LIFE_MANAGER_RELEASE_ROOT") or os.environ.get("LIFE_MANAGER_REPO")
    if not root:
        return None
    try:
        cut = json.loads((Path(root) / "RELEASE.json").read_text(encoding="utf-8"))["cut_at"]
        return int(datetime.strptime(cut, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _three_strike_same_guard(rejections: list[dict]) -> str | None:
    # Moved to storefront_kernel.three_strike_same_guard. The release cut time is supplied
    # here rather than in the kernel: which release is running is this loop's business, and
    # the kernel must stay usable by a caller whose configuration is versioned differently.
    return _storefront_kernel().three_strike_same_guard(
        rejections, since_epoch=_running_release_epoch(),
    )

def _jsonl_rows(path: Path) -> tuple[list[dict], str | None]:
    if not path.is_file():
        return [], f"{path.name}_missing"
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    except (OSError, json.JSONDecodeError):
        return [], f"{path.name}_invalid"
    return rows, None


def _latest_negotiate_observation(path: Path) -> tuple[dict | None, str | None]:
    if not path.is_file():
        return None, f"{path.name}_missing"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, f"{path.name}_invalid"
    for line in reversed(lines):
        if not line.strip().startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("status") == "completed" and type(row.get("observed")) is int:
            return {
                "observed": row["observed"],
                "run_id": row.get("run_id"),
                "evidence_path": str(path.resolve()),
                "evidence_sha256": hashlib.sha256(line.encode()).hexdigest(),
            }, None
    return None, f"{path.name}_completed_observation_missing"


def _last_known_good_catalog_analytics(
    state_dir: Path, service_ids: list[str], now: int,
) -> dict | None:
    expected_ids = set(service_ids)
    if (
        not expected_ids
        or len(expected_ids) != len(service_ids)
        or any(not value.isdigit() for value in service_ids)
    ):
        raise RuntimeError("catalog_analytics_expected_services_invalid")
    expected_services = len(expected_ids)
    wakes, error = _jsonl_rows(state_dir / "wakes.jsonl")
    if error not in {None, "wakes.jsonl_missing"}:
        raise RuntimeError(error)
    source_row = next((
        row for row in reversed(wakes)
        if row.get("status") == "completed"
        and isinstance(row.get("catalog_analytics"), dict)
        and row["catalog_analytics"].get("services_observed") == expected_services
        and isinstance(row["catalog_analytics"].get("totals"), dict)
        and isinstance(row["catalog_analytics"].get("metric_unknown_services"), dict)
    ), None)
    if source_row is None:
        return None

    catalog = dict(source_row["catalog_analytics"])
    source_epoch = int(
        catalog.get("observed_at_epoch") or source_row.get("observed_at_epoch") or 0
    )
    analytics, analytics_error = _jsonl_rows(state_dir / "analytics.jsonl")
    if analytics_error not in {None, "analytics.jsonl_missing"}:
        raise RuntimeError(analytics_error)
    latest_official: dict[str, dict] = {}
    for row in analytics:
        service_id = str(row.get("service_id") or "")
        if service_id in expected_ids and row.get("official") is True:
            latest_official[service_id] = row
    windows = {
        json.dumps(row.get("window"), sort_keys=True, separators=(",", ":"))
        for row in latest_official.values()
        if isinstance(row.get("window"), dict) and row["window"].get("complete") is True
    }
    window = json.loads(next(iter(windows))) if len(latest_official) == expected_services and len(windows) == 1 else None
    catalog.update({
        "status": "last_known_good",
        "observed_at_epoch": source_epoch,
        "freshness_seconds": max(0, now - source_epoch) if source_epoch else None,
        "coverage": {"observed": int(catalog["services_observed"]), "expected": expected_services},
        "window": window,
    })
    return catalog


def _catalog_conversion_baseline(analytics_path: Path, contracts: list[dict]) -> dict:
    """Bind each current listing version to its latest official conversion counters."""
    rows, error = _jsonl_rows(analytics_path)
    if error:
        raise RuntimeError("storefront_catalog_baseline_invalid")
    latest: dict[str, dict] = {}
    for row in rows:
        service_id = str(row.get("service_id") or "")
        metrics = row.get("metrics")
        known = (
            row.get("official") is True
            and isinstance(metrics, dict)
            and all(
                isinstance(metrics.get(name), dict)
                and metrics[name].get("status") == "known"
                and type(metrics[name].get("value")) is int
                for name in ("views", "favorites", "purchases")
            )
        )
        if service_id and known and int(row.get("observed_at_epoch") or 0) >= int(
                latest.get(service_id, {}).get("observed_at_epoch") or 0):
            latest[service_id] = row
    services = []
    for contract in sorted(contracts, key=lambda row: str(row.get("service_id") or "")):
        service_id = str(contract.get("service_id") or "")
        snapshot = latest.get(service_id)
        metrics = snapshot.get("metrics") if isinstance(snapshot, dict) else None
        if not isinstance(metrics, dict):
            raise RuntimeError("storefront_catalog_baseline_incomplete")
        values = {}
        for name in ("views", "favorites", "purchases"):
            metric = metrics.get(name)
            if (not isinstance(metric, dict) or metric.get("status") != "known"
                    or type(metric.get("value")) is not int):
                raise RuntimeError("storefront_catalog_baseline_incomplete")
            values[name] = metric["value"]
        services.append({
            "service_id": service_id, "title": contract.get("title"),
            "category": contract.get("category"), "price_jpy": contract.get("price_jpy"),
            "state": contract.get("state"),
            "service_version_sha256": contract.get("service_version_sha256"),
            **values, "observed_at_epoch": int(snapshot.get("observed_at_epoch") or 0),
        })
    totals = {"services": len(services), **{
        name: sum(row[name] for row in services) for name in ("views", "favorites", "purchases")
    }}
    identity = {"services": services, "totals": totals}
    return {**identity, "baseline_sha256": hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()}


def _storefront_failure_disposition(reason: str) -> tuple[str, int]:
    if reason in {
        "official_inventory_empty_or_invalid",
        "storefront_catalog_baseline_incomplete",
    }:
        return "pending", 0
    return "failed", 1


def _last_full_competitor_evidence(state_dir: Path, now: int) -> dict:
    wakes, error = _jsonl_rows(state_dir / "wakes.jsonl")
    if error not in {None, "wakes.jsonl_missing"}:
        raise RuntimeError(error)
    source = next((
        row for row in reversed(wakes)
        if row.get("status") == "completed"
        and row.get("mode") == "full"
        and type(row.get("competitor_evidence_count")) is int
    ), None)
    if source is None:
        return {"status": "not_checked_incremental", "latest_full_count": None,
                "observed_at_epoch": None, "freshness_seconds": None}
    observed_at = int(source.get("observed_at_epoch") or 0)
    return {
        "status": "not_checked_incremental",
        "latest_full_count": int(source["competitor_evidence_count"]),
        "observed_at_epoch": observed_at,
        "freshness_seconds": max(0, now - observed_at) if observed_at else None,
    }


def _auto_cadence_is_incremental(state_dir: Path, now: int, full_interval_seconds: int) -> bool:
    if full_interval_seconds <= 0:
        raise RuntimeError("full_interval_seconds_invalid")
    rows, error = _jsonl_rows(state_dir / "wakes.jsonl")
    if error not in {None, "wakes.jsonl_missing"}:
        raise RuntimeError(error)
    full_epochs = [
        int(row.get("observed_at_epoch") or 0)
        for row in rows
        if row.get("status") == "completed"
        and (
            row.get("mode") == "full"
            or (row.get("mode") is None and row.get("reason") != "incremental_catalog_funnel_readback")
        )
    ]
    last_full_epoch = max(full_epochs, default=0)
    return last_full_epoch > 0 and now - last_full_epoch < full_interval_seconds


def _join_funnel(
    state_dir: Path, contracts: list[dict], reply_transcripts: Path, applied_path: Path,
    earnings_path: Path, projects_dir: Path, now: int,
    negotiate_run_log: Path = DEFAULT_NEGOTIATE_RUN_LOG,
) -> dict:
    transcripts, transcript_error = _jsonl_rows(reply_transcripts)
    applied, applied_error = _jsonl_rows(applied_path)
    earnings, earnings_error = _jsonl_rows(earnings_path)
    versions = {row["service_id"]: row["service_version_sha256"] for row in contracts}
    applied_rows = {
        str(row.get("requestId") or ""): row
        for row in applied if row.get("status") == "applied" and row.get("requestId")
    }
    apply_talkrooms: dict[str, dict] = {}
    if projects_dir.is_dir():
        for path in projects_dir.glob("*/state.json"):
            try:
                project = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            request_id, talkroom_id = str(project.get("request_id") or ""), str(project.get("talkroom_id") or "")
            application = applied_rows.get(request_id)
            if application is not None and talkroom_id:
                apply_talkrooms[talkroom_id] = {
                    "request_id": request_id,
                    "project_state_path": str(path.resolve()),
                    "project_state_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "application_evidence_sha256": hashlib.sha256(json.dumps(
                        application, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    ).encode()).hexdigest(),
                }
    conversations: dict[str, dict] = {}
    for row in transcripts:
        talkroom_id = str(row.get("talkroom_id") or "")
        if not talkroom_id:
            continue
        evidence = json.dumps({"buyer_last_said": row.get("buyer_last_said"),
                               "conversation": row.get("conversation")}, ensure_ascii=False)
        recorded_service_id = str(row.get("service_id") or "")
        service_ids = ({recorded_service_id} if recorded_service_id in versions else {
            value for value in re.findall(r"coconala\.com/services/(\d+)", evidence)
            if value in versions
        })
        current = conversations.setdefault(talkroom_id, {"service_ids": set(), "observed_at": row.get("sent_at")})
        current["service_ids"].update(service_ids)
        if type(row.get("sent_at")) is int:
            prior = current.get("observed_at")
            current["observed_at"] = min(row["sent_at"], prior) if type(prior) is int else row["sent_at"]
    origins = {}
    appended = 0
    for talkroom_id, value in conversations.items():
        ids = value["service_ids"]
        service_id = next(iter(ids)) if len(ids) == 1 else None
        origin = "storefront" if service_id else "apply" if talkroom_id in apply_talkrooms else "unknown"
        origins[talkroom_id] = (origin, service_id)
        event = {
            "version": 1, "source_event_id": f"coconala:conversation:{talkroom_id}",
            "event_kind": "inquiry", "platform": "coconala", "origin": origin,
            "service_id": service_id, "listing_version": versions.get(service_id),
            "conversation_id": talkroom_id, "order_id": None,
            "observed_at_epoch": value.get("observed_at"), "ingested_at_epoch": now,
        }
        appended += int(_append_key_once(state_dir / "funnel-events.jsonl", "source_event_id", event))
        _append_key_once(state_dir / "attribution-map.jsonl", "attribution_key", {
            **event, "attribution_key": f"coconala:conversation:{talkroom_id}",
        })
    attribution_corrections_appended = 0
    for row in earnings:
        talkroom_id = str(row.get("talkroom_id") or "")
        origin, service_id = origins.get(talkroom_id, ("unknown", None))
        if origin == "unknown" and talkroom_id in apply_talkrooms:
            origin = "apply"
        idem = str(row.get("idem_key") or "")
        if not idem or type(row.get("jpy")) not in {int, float} or row.get("net_of_fee") is not True:
            continue
        source_event_id = f"coconala:payment:{idem}"
        event = {
            "version": 1, "source_event_id": source_event_id,
            "event_kind": "payment", "platform": "coconala", "origin": origin,
            "service_id": service_id, "listing_version": versions.get(service_id),
            "conversation_id": talkroom_id or None, "order_id": str(row.get("requestId") or "") or None,
            "gross_jpy": None, "fee_jpy": None, "refund_jpy": None,
            "net_receipt_jpy": row["jpy"], "observed_at_epoch": row.get("ts"),
            "ingested_at_epoch": now, "immutable_receipt_id": idem,
            "revision_count": None, "rating": None, "review_id": None, "repeat_purchase": None,
        }
        appended += int(_append_key_once(state_dir / "funnel-events.jsonl", "source_event_id", event))
        if origin == "apply":
            evidence = apply_talkrooms[talkroom_id]
            evidence_sha = hashlib.sha256(json.dumps(
                evidence, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            correction = {
                "version": 1,
                "attribution_key": f"{source_event_id}:origin:apply:evidence:{evidence_sha}",
                "event_kind": "attribution_correction",
                "source_event_id": source_event_id,
                "platform": "coconala",
                "origin": "apply",
                "conversation_id": talkroom_id,
                "order_id": str(row.get("requestId") or "") or None,
                "immutable_receipt_id": idem,
                "evidence": evidence,
                "observed_at_epoch": now,
            }
            attribution_corrections_appended += int(_append_key_once(
                state_dir / "attribution-map.jsonl", "attribution_key", correction,
            ))
    events, ledger_error = _jsonl_rows(state_dir / "funnel-events.jsonl")
    attributions, attribution_error = _jsonl_rows(state_dir / "attribution-map.jsonl")
    negotiate_observation, negotiate_observation_error = _latest_negotiate_observation(
        negotiate_run_log,
    )
    corrections = {
        str(row.get("source_event_id")): row
        for row in attributions
        if row.get("event_kind") == "attribution_correction"
        and row.get("origin") in {"storefront", "apply", "unknown"}
        and row.get("source_event_id")
    }
    summary = {origin: {"inquiries": 0, "payments": 0, "net_jpy": 0.0}
               for origin in ("storefront", "apply", "unknown")}
    for event in events:
        correction = corrections.get(str(event.get("source_event_id") or ""), {})
        corrected_origin = correction.get("origin", event.get("origin"))
        origin = corrected_origin if corrected_origin in summary else "unknown"
        if event.get("event_kind") == "inquiry":
            summary[origin]["inquiries"] += 1
        elif event.get("event_kind") == "payment" and type(event.get("net_receipt_jpy")) in {int, float}:
            summary[origin]["payments"] += 1
            summary[origin]["net_jpy"] += float(event["net_receipt_jpy"])
    errors = [value for value in (
        transcript_error, applied_error, earnings_error, ledger_error, attribution_error,
        negotiate_observation_error,
    ) if value]
    cursor_inputs = [str(row.get("source_event_id") or "") for row in events]
    cursor_inputs.extend(str(row.get("attribution_key") or "") for row in attributions)
    cursor = hashlib.sha256("\n".join(sorted(cursor_inputs)).encode()).hexdigest()
    official_observed = negotiate_observation.get("observed") if negotiate_observation else None
    transcript_ingested = len(conversations)
    uncovered = (
        max(official_observed - transcript_ingested, 0)
        if type(official_observed) is int else None
    )
    coverage = {
        "official_negotiate_conversations_observed": official_observed,
        "transcript_conversations_ingested": transcript_ingested,
        "uncovered_official_conversations": uncovered,
        "storefront_inquiries": summary["storefront"]["inquiries"],
        "apply_inquiries": summary["apply"]["inquiries"],
        "apply_payments": summary["apply"]["payments"],
        "unresolved_inquiries": summary["unknown"]["inquiries"],
        "unresolved_payments": summary["unknown"]["payments"],
        "source_status": "latest_completed_log_noncanonical" if negotiate_observation else "unknown",
        "source": negotiate_observation,
    }
    return {"version": 1, "appended": appended, "events": len(events), "by_origin": summary,
            "attribution_corrections_appended": attribution_corrections_appended,
            "coverage": coverage, "unknown_sources": errors, "cutoff_cursor": cursor}


def _load_portfolio_scorecard(scorecard_path: Path) -> dict:
    try:
        scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_portfolio_policy_invalid") from error
    policy = scorecard.get("portfolio_policy")
    services = scorecard.get("services")
    backlog = scorecard.get("priority_backlog")
    if (not isinstance(policy, dict) or policy.get("version") != 1
            or type(policy.get("slot_limit")) is not int or policy["slot_limit"] <= 0
            or type(policy.get("minimum_views_for_retirement")) is not int
            or policy.get("short_term_zero_sales_can_retire") is not False
            or policy.get("retirement_mode") != "recoverable_unpublish_before_delete"
            or not isinstance(services, list) or not isinstance(backlog, list)
            or not isinstance(policy.get("replacement_candidates"), list)):
        raise RuntimeError("storefront_portfolio_policy_invalid")
    return scorecard


def _allocate_portfolio(
    state_dir: Path, contracts: list[dict], funnel: dict, scorecard_path: Path, now: int,
    duplicate_listings: list[dict] | None = None,
) -> dict:
    # Moved to storefront_kernel.allocate_portfolio; see _validate_mutation_contract for why
    # this stays as an alias.
    return _storefront_kernel().allocate_portfolio(
        state_dir, contracts, funnel, scorecard_path, now, duplicate_listings,
    )


def _load_image_contract(path: Path) -> dict:
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_image_contract_invalid") from error
    required = {
        "version", "service_id", "field", "asset", "asset_sha256", "width", "height",
        "mime_type", "claims", "claim_source", "platform_requirement_source",
    }
    if set(contract) != required or contract.get("version") != 1:
        raise RuntimeError("storefront_image_contract_fields_invalid")
    if contract.get("service_id") != TARGET_SERVICE_ID or contract.get("field") != "image":
        raise RuntimeError("storefront_image_contract_identity_invalid")
    asset = (path.parent / str(contract.get("asset") or "")).resolve()
    try:
        asset.relative_to(path.parent.resolve())
        data = asset.read_bytes()
    except (OSError, ValueError) as error:
        raise RuntimeError("storefront_image_asset_invalid") from error
    if (len(data) > 100 * 1024 * 1024 or len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n"
            or contract.get("mime_type") != "image/png"):
        raise RuntimeError("storefront_image_asset_format_invalid")
    width, height = struct.unpack(">II", data[16:24])
    digest = hashlib.sha256(data).hexdigest()
    if (width, height) != (1220, 1016) or (contract.get("width"), contract.get("height")) != (width, height):
        raise RuntimeError("storefront_image_asset_dimensions_invalid")
    if contract.get("asset_sha256") != digest:
        raise RuntimeError("storefront_image_asset_hash_invalid")
    claims = contract.get("claims")
    if not isinstance(claims, list) or not claims or not all(isinstance(claim, str) and claim.strip() for claim in claims):
        raise RuntimeError("storefront_image_claims_invalid")
    return {**contract, "asset_path": str(asset)}


def _load_gallery_contract(path: Path) -> dict:
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_gallery_contract_invalid") from error
    required = {
        "version", "service_id", "field", "before_image_ids", "kept_image_ids",
        "replacements", "claims", "claim_source", "platform_requirement_source",
    }
    if (set(contract) != required or contract.get("version") != 1
            or contract.get("service_id") != GALLERY_SERVICE_ID or contract.get("field") != "image"):
        raise RuntimeError("storefront_gallery_contract_fields_invalid")
    before_ids, kept_ids, replacements = (
        contract.get("before_image_ids"), contract.get("kept_image_ids"), contract.get("replacements")
    )
    if (not isinstance(before_ids, list) or len(before_ids) != 6 or len(set(before_ids)) != 6
            or not isinstance(kept_ids, list) or len(kept_ids) != 2 or not set(kept_ids) < set(before_ids)
            or not isinstance(replacements, list) or len(replacements) != 4):
        raise RuntimeError("storefront_gallery_identity_invalid")
    loaded = []
    replaced_ids = []
    for row in replacements:
        if not isinstance(row, dict) or set(row) != {
            "replace_image_id", "asset", "asset_sha256", "width", "height", "mime_type",
        }:
            raise RuntimeError("storefront_gallery_replacement_invalid")
        asset = (path.parent / str(row.get("asset") or "")).resolve()
        try:
            asset.relative_to(path.parent.resolve())
            data = asset.read_bytes()
        except (OSError, ValueError) as error:
            raise RuntimeError("storefront_gallery_asset_invalid") from error
        if (data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) > 100 * 1024 * 1024
                or row.get("mime_type") != "image/png"):
            raise RuntimeError("storefront_gallery_asset_format_invalid")
        width, height = struct.unpack(">II", data[16:24])
        if ((width, height) != (1220, 1016)
                or (row.get("width"), row.get("height")) != (width, height)
                or row.get("asset_sha256") != hashlib.sha256(data).hexdigest()):
            raise RuntimeError("storefront_gallery_asset_identity_invalid")
        replaced_ids.append(str(row.get("replace_image_id") or ""))
        loaded.append({**row, "asset_path": str(asset)})
    if set(replaced_ids) != set(before_ids) - set(kept_ids) or len(set(replaced_ids)) != 4:
        raise RuntimeError("storefront_gallery_partition_invalid")
    return {**contract, "replacements": loaded}


def _preflight_storefront_bundle() -> None:
    """Reject an explicit bundle before any browser or lease operation."""
    required = (
        "GIG_STOREFRONT_ROOT", "GIG_STOREFRONT_TARGET_SERVICE_ID",
        "GIG_STOREFRONT_GALLERY_SERVICE_ID", "GIG_STOREFRONT_PRESENTATION_SERVICE_ID",
        "GIG_STOREFRONT_SCOPE_SERVICE_ID",
    )
    if any(not os.environ.get(name, "").strip() for name in required):
        raise RuntimeError("storefront_service_ids_required")
    paths = _storefront_paths()
    _load_portfolio_scorecard(paths["scorecard"])
    for path in paths["listings"].glob("*.json"):
        try:
            _validate_listing_contract_static(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("listing_contract_static_invalid") from error
    # Listing version/url bindings need the live official inventory and remain post-browser.
    image = _load_image_contract(paths["image"])
    gallery = _load_gallery_contract(paths["gallery"])
    mappings, _ = _load_capability_families(paths["families"])
    if (not mappings.get(str(image["service_id"]))
            or not mappings.get(str(gallery["service_id"]))):
        raise RuntimeError("storefront_image_family_unbound")
    for field in ("title", "body", "scope", "package", "faq", "price"):
        spec = _load_text_mutation_spec(paths[field])
        if mappings.get(str(spec["service_id"])) != spec["capability_family"]:
            raise RuntimeError("storefront_text_mutation_family_unbound")
    import storefront_draft
    storefront_draft.load_contract(paths["new_listing"])


# Coconala withdrew a live listing twice for naming an external tool in its copy, because a file
# service the platform cannot see can become direct contact off the platform. These names are
# quoted from those takedown notices rather than guessed, and any new one belongs here only after
# the platform has said it.
PROHIBITED_COPY_TERMS = (
    "Googleドキュメント", "Google ドキュメント", "Googleドライブ", "Google ドライブ",
    "Googleスプレッドシート", "スプレッドシート", "Google Docs", "Google Drive",
    "Googleフォーム", "Dropbox", "ギガファイル", "ギガファイル便", "firestorage",
)

# Coconala renders a new listing's title as `{title_stem}ます`, so the stem's last character
# must be a verb continuative (i-form) ending for that to read as grammatical Japanese. A stem
# ending in a particle instead produced the ungrammatical `…SEO構成からます` on a sealed
# contract. The guard and the CREATE prompt both read this one constant so they cannot drift.
TITLE_STEM_CONTINUATIVE_ENDINGS = "いきしちにひみりぎじびぴえけせてねへめれげぜでべぺ"


def _prohibited_copy_terms(*texts: str) -> list[str]:
    """Name the platform-prohibited terms buyer-visible copy contains, if any."""
    joined = "\n".join(str(text or "") for text in texts)
    return sorted({term for term in PROHIBITED_COPY_TERMS if term in joined})


def _offer_refresh_due(
    effects_path: Path, service_id: str, family_name: str, family: dict,
    already_advertised: set[str] | None = None, field: str = "body",
) -> str | None:
    """Report a listing still selling an offer its capability family no longer promises.

    Deliverables are what the listing may claim, so changing them changes what the copy must
    say. The Excel family moved from handing over a design document to handing over a working
    macro; until the body is rewritten, the page sells the old promise.
    """
    if not isinstance(family, dict):
        return None
    digest = hashlib.sha256(json.dumps(
        {key: family.get(key) for key in ("inclusions", "deliverables")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # An offer the live listings already advertise is not a change. Without this, having no
    # record of a rewrite reads the same as having a new promise, and the whole catalogue gets
    # rewritten and put into a seven-day hold for offers that never moved.
    if digest in (already_advertised or set()):
        return None
    if not effects_path.exists():
        return digest
    for line in effects_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        effect = json.loads(line)
        if (str(effect.get("service_id") or "") == str(service_id)
                and effect.get("status") == "accepted" and effect.get("effect") == 1
                and str(effect.get("changed_field") or "") == field
                and effect.get("offer_digest") == digest):
            return None
    return digest


async def _evaluate(ws: object, expression: str, cid: int) -> tuple[object, int]:
    """Evaluate one expression on an open page and return its value with the next id.

    The listing-state executor called a helper this module never had, which is what a branch
    that has never run looks like: it type-checks, ships and raises NameError the first time
    the loop actually needs it.
    """
    import listing_inventory

    response = await listing_inventory._call(ws, "Runtime.evaluate", {
        "expression": expression, "returnByValue": True, "awaitPromise": True,
    }, cid)
    result = response.get("result", {}).get("result", {})
    if result.get("subtype") == "error" or "exceptionDetails" in response.get("result", {}):
        raise RuntimeError("storefront_browser_evaluation_failed")
    return result.get("value"), cid + 1


def _platform_withdrew_listing(ledger_path: Path, service_id: str, is_public: bool) -> bool:
    """Report a listing this loop published that the platform has since taken down.

    Coconala withdrew a generated listing twice for naming an external tool in its copy, and each
    time the next full wake saw a non-public listing, refilled the draft and published it again.
    Republishing is arguing with moderation and risks the account that earns the money, so a
    listing that was live by this loop's own ledger and is no longer public stays down.
    """
    if is_public or not ledger_path.exists():
        return False
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("new_listing_draft_ledger_invalid") from error
        if (str(row.get("draft_service_id") or "") == str(service_id)
                and row.get("status") == "published" and row.get("public_effect") == 1):
            return True
    return False


def _load_capability_families(path: Path) -> tuple[dict[str, str], dict[str, dict]]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("listing_contract_families_invalid") from error
    mappings, families = config.get("service_families"), config.get("families")
    if (config.get("version") != 1 or not isinstance(mappings, dict) or not isinstance(families, dict)
            or not mappings or not families
            or any(not str(service_id).isdigit() or family not in families
                   for service_id, family in mappings.items())):
        raise RuntimeError("listing_contract_families_invalid")
    return mappings, families


def _storefront_kernel():
    """Reach skills/_shared/marketplace-core/scripts, the same way `_load_catalog_entries` does,
    and return the shared, platform-neutral storefront_kernel module.

    Several `_`-prefixed functions below (portfolio allocation, mutation-contract sealing and
    validation, official demand scoring, replace-plan rendering, in-flight draft recovery, and
    the proposal-rejection guard) are now thin aliases onto that module, kept here so the rest
    of this ~8,600-line file and its tests keep calling them by the same name unchanged.
    """
    SHARED_SCRIPTS = SCRIPTS.parent.parent.parent / "_shared" / "marketplace-core" / "scripts"
    if str(SHARED_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SHARED_SCRIPTS))
    import storefront_kernel
    return storefront_kernel


def _load_catalog_entries(path: Path = LISTING_CATALOG) -> dict[str, dict]:
    """Load the owner's platform-agnostic listing catalog, keyed by capability_family.

    The catalog is competitor- and budget-evidenced content the owner curated ahead of time
    (skills/gig-work/profile/listings/catalog.json). CREATE grounds its generated proposal in
    the matching entry so buyer-facing copy stays anchored to what the owner actually decided
    to sell, instead of the model inventing scope, price or deliverables unsupervised.

    Delegates to the shared skills/_shared/marketplace-core listing_catalog module, which is
    the one place that reads and validates the catalog (it fails loud on a bad catalog rather
    than returning empty). This production call site must not regress: it keeps returning the
    same per-family subset of fields it always has, and still returns {} when the catalog is
    unreadable/invalid — but it now reports why on stderr instead of swallowing the reason.
    """
    SHARED_SCRIPTS = SCRIPTS.parent.parent.parent / "_shared" / "marketplace-core" / "scripts"
    if str(SHARED_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SHARED_SCRIPTS))
    import listing_catalog

    try:
        catalog = listing_catalog.load(path)
    except listing_catalog.CatalogError as error:
        print(f"catalog_load_failed: {type(error).__name__}: {error}", file=sys.stderr)
        return {}
    entries = listing_catalog.entries_by_family(catalog)
    return {
        family: {
            key: row.get(key) for key in
            ("id", "title_ja", "value_prop", "tiers", "deliverables", "required_inputs", "faq")
        }
        for family, row in entries.items()
    }


def _validate_mutation_contract(contract: dict, capability_families: dict[str, str]) -> None:
    # Moved to storefront_kernel.validate_mutation_contract, generalized to take the platform
    # string and the prohibited-copy-terms list as parameters instead of hardcoding Coconala's;
    # this alias supplies both so behaviour for every existing caller here is unchanged.
    return _storefront_kernel().validate_mutation_contract(
        contract, capability_families, platform="coconala",
        prohibited_copy_terms=PROHIBITED_COPY_TERMS,
    )


def _seal_mutation_contract(unsigned: dict, capability_families: dict[str, str]) -> dict:
    # Moved to storefront_kernel.seal_mutation_contract; see _validate_mutation_contract.
    return _storefront_kernel().seal_mutation_contract(
        unsigned, capability_families, platform="coconala",
        prohibited_copy_terms=PROHIBITED_COPY_TERMS,
    )


def _listing_state_control(inventory_row: dict, service_id: str) -> dict:
    """Bind the observed seller control that unpublishes one listing.

    Measured on the seller list page: each card exposes `公開設定`, whose confirmation is
    `a.js_change-open-status` pointing at `/services/archive/<id>`. Archiving keeps the
    listing, its versions and its sales history, which is what makes retirement
    recoverable. The published service edit form carries no such control at all, so this
    binds the card control or fails closed.
    """
    controls = [row for row in inventory_row.get("state_controls") or [] if isinstance(row, dict)]
    if not controls:
        raise RuntimeError("storefront_retire_controls_unobserved")
    control = next(
        (row for row in controls
         if "js_change-open-status" in str(row.get("cls") or "")
         and str(row.get("href") or "") == f"/services/archive/{service_id}"),
        None,
    )
    if control is None:
        raise RuntimeError("storefront_retire_control_missing")
    if not str(control.get("context") or "").strip():
        raise RuntimeError("storefront_retire_control_wording_unobserved")
    return control


def _render_listing_state_mutation(
    source: dict, inventory_row: dict, seller_snapshot: dict,
    capability_families: dict[str, str], allocation: dict,
) -> dict:
    service_id = str(source["service_id"])
    if str(inventory_row.get("state") or "") != PUBLIC_LISTING_STATE:
        raise RuntimeError("storefront_retire_listing_not_public")
    if allocation.get("action") not in {"RETIRE", "REPLACE"} or allocation.get("service_id") != service_id:
        raise RuntimeError("storefront_retire_allocation_invalid")
    gates = allocation.get("gates") or {}
    if not gates.get("recoverable_retire_gates_met"):
        raise RuntimeError("storefront_retire_gates_unmet")
    if seller_snapshot.get("url") != f"https://coconala.com/mypage/services/{service_id}":
        raise RuntimeError("storefront_retire_seller_form_invalid")
    control = _listing_state_control(inventory_row, service_id)
    return _seal_mutation_contract({
        "version": 1, "platform": "coconala", "service_id": service_id,
        "precondition_listing_version_sha256": source["service_version_sha256"],
        "changed_field": "listing_state",
        "before_value": {"listing_state": PUBLIC_LISTING_STATE, "action": "none"},
        "proposed_value": {"listing_state": "非公開", "action": str(control["href"]),
                           "control_class": str(control["cls"]),
                           "platform_wording": str(control["context"])},
        "allowed_delta": [LISTING_STATE_DELTA],
        "rollback_value": {"listing_state": PUBLIC_LISTING_STATE, "action": "none"},
        "official_readback": {"service_id": service_id, "public_listing_absent": True,
                              "seller_state": "非公開",
                              "recoverable": True, "deletion": False},
        "success_metric": "inquiries",
        "observation_window_days": 14,
        "capability_family": capability_families.get(service_id),
        "evidence": [
            f"official:offer-contract:{service_id}:{source['service_version_sha256']}",
            f"storefront:portfolio-allocation:{allocation['allocation_key']}",
        ],
    }, capability_families)


# The create -> prepare -> publish state machine advances at most one step per wake and
# needs exactly three sequential passes to take a blank draft public: create_or_claim_blank_draft
# (draft_created), prepare_draft (draft_prepared), publish_draft (published) -- each gated by its
# own _persist_effect_block checkpoint. At this lane's 60-second wake cadence that is 180 seconds
# in the ideal case; one hour is ~20x that, enough headroom for slow browser/agent round trips on
# any one step without a next creation attempt colliding with a cycle still in flight. Coconala
# caps a seller at 20 live listings, so at one new creation started per hour the remaining free
# slots fill within a handful of hours -- same working day, never the multi-day drift the previous
# 86,400-second (one per day) spacing produced.
CREATE_MIN_INTERVAL_SECONDS = 3_600


def _extract_search_demand(body: str) -> dict:
    # Moved to storefront_kernel.extract_search_demand; see _validate_mutation_contract for
    # why this stays as an alias.
    return _storefront_kernel().extract_search_demand(body)


def _family_traffic_without_sales(
    analytics_path: Path, families: dict[str, str], family_name: str, minimum_views: int = 100,
) -> dict | None:
    """Report a capability family whose own listings get looked at and never bought.

    Competitor search volume says a market exists; it does not say this seller can sell in it.
    Two Excel listings reached hundreds of views and no purchase while the demand score for
    that market read twelve, which is the loop proposing to repeat something it has already
    failed at.
    """
    if not family_name or not analytics_path.exists():
        return None
    latest: dict[str, dict] = {}
    for line in analytics_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if str(row.get("service_id") or ""):
            latest[str(row["service_id"])] = row
    views = purchases = 0
    counted = 0
    for service_id, snapshot in latest.items():
        if families.get(service_id) != family_name:
            continue
        metrics = snapshot.get("metrics") if isinstance(snapshot.get("metrics"), dict) else {}
        service_views = (metrics.get("views") or {}).get("value")
        service_purchases = (metrics.get("purchases") or {}).get("value")
        if type(service_views) is not int or type(service_purchases) is not int:
            continue
        views += service_views
        purchases += service_purchases
        counted += 1
    if counted == 0 or views < minimum_views or purchases > 0:
        return None
    return {"capability_family": family_name, "listings": counted,
            "views": views, "purchases": purchases}


def _family_already_published(draft_ledger_path: Path, family_name: str) -> bool:
    """Report whether a capability family already has a live CREATE listing.

    `_family_traffic_without_sales` only catches a family once its published listing has
    collected views in analytics.jsonl, and only if families.json maps a service to it.
    mobile_app_dev published as service 4386009 but never gained that families.json mapping,
    so neither check ever fired: `_next_unused_demand_cluster` kept reselecting its cluster
    every wake because nothing else marked the cluster consumed. This reads the loop's own
    creation ledger instead, which every CREATE writes to regardless of families.json.
    """
    if not family_name or not draft_ledger_path.exists():
        return False
    for line in draft_ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if (str(row.get("candidate_key") or "").startswith("storefront:create:v1:")
                and row.get("capability_family") == family_name
                and (row.get("status") in {"published", "already_public"}
                     or row.get("public_effect") == 1)):
            return True
    return False


def _score_demand_cluster(cluster: dict) -> dict:
    # Moved to storefront_kernel.score_demand_cluster; see _validate_mutation_contract.
    return _storefront_kernel().score_demand_cluster(cluster)


def _family_market(state_dir: Path, family_name: str) -> dict | None:
    # Moved to storefront_kernel.family_market; see _validate_mutation_contract for why this
    # stays as an alias.
    return _storefront_kernel().family_market(state_dir, family_name)


def _seal_demand_proposal(proposal: dict, family_names: set[str], catalog_titles: list[str]) -> list[dict]:
    """Accept only query candidates tied to an owned capability family.

    The model may name a market to look at; it may never assert that demand exists. That
    verdict comes from crawling the official search page afterwards.
    """
    if proposal.get("decision") == "no_op":
        if proposal.get("queries") or not str(proposal.get("no_op_reason") or "").strip():
            raise RuntimeError("storefront_demand_noop_invalid")
        return []
    queries = proposal.get("queries")
    if (proposal.get("decision") != "propose" or not isinstance(queries, list) or not queries
            or proposal.get("no_op_reason") is not None):
        raise RuntimeError("storefront_demand_proposal_invalid")
    sealed = []
    seen = set()
    for row in queries:
        query = str((row or {}).get("query") or "").strip()
        family = str((row or {}).get("capability_family") or "").strip()
        if not query or family not in family_names or query in seen:
            raise RuntimeError("storefront_demand_query_unowned_or_duplicate")
        if any(query in str(title or "") for title in catalog_titles):
            raise RuntimeError("storefront_demand_query_duplicates_catalogue")
        seen.add(query)
        sealed.append({"query": query, "capability_family": family,
                       "rationale": str(row.get("rationale") or "").strip()})
    return sealed


def _catalog_capability_templates(configured: dict, catalog: dict) -> dict:
    """Give every owner-curated catalog family a template, not only the ones already tried.

    families.json only ever grows an entry for a family this loop happened to explore before
    (ten entries, of which just two -- mobile_app_dev and mvp_web_app_build -- also name a
    skills/gig-work/profile/listings/catalog.json family). Demand exploration can only offer
    the model families present in this dict, so the other eighteen catalog listings were
    structurally unreachable even with free shelf slots and no competing traffic. Catalog
    fields are enough context for the model to reason about a query for that family.
    """
    merged = dict(configured)
    for family, entry in catalog.items():
        merged.setdefault(family, {
            "name": str(entry.get("title_ja") or family),
            "description": str(entry.get("value_prop") or ""),
            "deliverables": entry.get("deliverables") or [],
        })
    return merged


def _market_capability_templates(configured: dict, inventory: dict) -> dict:
    """Expose public executable skills to ongoing market discovery, not only first bootstrap."""
    merged = dict(configured)
    for row in inventory.get("skills") or []:
        if not isinstance(row, dict) or row.get("runtime") != "agent_skill":
            continue
        path = str(row.get("skill_path") or "")
        name = str(row.get("name") or "").strip()
        description = str(row.get("description") or "").strip()
        digest = str(row.get("source_sha256") or "")
        if (not path.startswith("skills/") or not path.endswith("/SKILL.md")
                or not name or not description or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            continue
        merged.setdefault(name, {
            "name": name, "description": description, "skill_path": path,
            "source_sha256": digest, "runtime": "agent_skill",
        })
    return merged


def _resolve_create_capability(
    *, wanted: str, source: dict, service_families: dict[str, str],
    templates: dict[str, dict], repo: Path | None = None,
) -> tuple[str, dict, set[str]]:
    """Keep the chosen public capability while reusing an existing seller form as the adapter."""
    fallback = str(service_families.get(str(source.get("service_id") or "")) or "")
    family = wanted if wanted in templates else fallback
    template = templates.get(family)
    if not family or not isinstance(template, dict):
        raise RuntimeError("storefront_create_capability_missing")
    evidence = set()
    repo = repo or GIG_DIR.parents[2]
    skill_path = str(template.get("skill_path") or "")
    if skill_path.startswith("skills/") and skill_path.endswith("/SKILL.md"):
        evidence.add(str((repo / skill_path).resolve()))
    return family, template, evidence


def _proposal_capability_evidence(all_evidence: set[str], selected_evidence: set[str]) -> set[str]:
    """Do not let evidence for an unrelated capability redefine the selected product."""
    return set(selected_evidence or all_evidence)


def _catalog_bound_demand_clusters(clusters: list[dict], catalog_families: set[str]) -> list[dict]:
    """Drop a scraped market cluster naming a capability family outside the owner's catalog.

    A cluster outside skills/gig-work/profile/listings/catalog.json produced a live
    non-software listing (service 4384702, "user-interview-synthesizer") on 2026-09-05.
    A create driven by demand must stay inside what the owner curated to sell.
    """
    return [row for row in clusters
            if str(row.get("capability_family") or "") in catalog_families]


def _next_unused_demand_cluster(clusters: list[dict], dismissed: set[str]) -> dict | None:
    return next(
        (row for row in sorted(clusters, key=lambda candidate: (
            -int(bool(candidate.get("capability_inventory_sha256"))),
            -int(candidate.get("recurring_potential") is True),
            -(candidate.get("score") or 0),
            -(candidate.get("median_price_jpy") or 0),
        ))
         if row.get("status") == "known" and (row.get("score") or 0) > 0
         and not row.get("consumed_at_epoch")
         and str(row.get("cluster_key") or "") not in dismissed),
        None,
    )


def _select_demand_cluster_for_wake(
    known_clusters: list[dict], dismissed_clusters: set[str],
    stranded_drafts: dict[str, tuple[str, int]],
) -> tuple[dict | None, dict | None]:
    """Pick this wake's demand cluster, finishing a draft already in flight before another.

    `_next_unused_demand_cluster` alone lets the cluster selector land on any family with score,
    even while a different family already has a filled draft sitting unpublished -- draft
    4387924 ("LINE公式アカウントの予約・定型応答を構築します", capability family `line_bot_dev`) sat
    for hours this way while the loop kept starting new drafts elsewhere. An unpublished draft
    earns nothing, and finishing one already started is always worth more than starting another,
    so a family in `stranded_drafts` is selected here before the normal pick ever runs. When
    more than one family has a draft in flight, the oldest one (smallest `first_seen_index`)
    goes first, since it has waited longest.
    """
    if stranded_drafts:
        stranded_family, (stranded_draft_id, _) = min(
            stranded_drafts.items(), key=lambda item: item[1][1])
        matched_cluster = next(
            (row for row in known_clusters
             if row.get("status") == "known"
             and str(row.get("capability_family") or "") == stranded_family),
            None,
        )
        if matched_cluster is None:
            # Do not invent a cluster and do not fall through to starting a new listing: that
            # would strand a second draft while the first one is still waiting.
            return None, {
                "proposed": 0, "appended": 0,
                "selected_cluster": None, "selected_score": None,
                "selected_family": stranded_family,
                "reason": "unpublished_draft_awaiting_known_cluster",
                "unpublished_draft_service_id": stranded_draft_id,
            }
        derivation = {
            "proposed": 0, "appended": 0,
            "selected_cluster": matched_cluster.get("query"),
            "selected_score": matched_cluster.get("score"),
            "selected_family": matched_cluster.get("capability_family"),
            "reason": "unpublished_draft_awaiting_publication",
            "unpublished_draft_service_id": stranded_draft_id,
        }
        # A dismissal is permanent so a later wake never reselects that market -- but a
        # stranded draft is real work already sitting on the platform, and finishing it
        # outranks a dismissal that was only ever about starting something new. The draft
        # wins; the dismissal ledger is append-only and keyed by cluster_key so it is not
        # rewritten, but the override is named here so this wake is distinguishable from one
        # that honoured the dismissal.
        if str(matched_cluster.get("cluster_key") or "") in dismissed_clusters:
            derivation["overrides_dismissal"] = True
        return matched_cluster, derivation
    unused_cluster = _next_unused_demand_cluster(known_clusters, dismissed_clusters)
    # A no-op must name the market it would go after next, not just say it did nothing.
    demand_derivation = None if unused_cluster is None else {
        "proposed": 0, "appended": 0,
        "selected_cluster": unused_cluster.get("query"),
        "selected_score": unused_cluster.get("score"),
        "selected_family": unused_cluster.get("capability_family"),
        "reason": "unused_demand_cluster_available",
    }
    return unused_cluster, demand_derivation


def _capability_inventory_needs_market_probe(clusters: list[dict], digest: str) -> bool:
    return not any(
        row.get("capability_inventory_sha256") == digest
        and row.get("status") == "known" and (row.get("score") or 0) > 0
        for row in clusters
    )


def _unlisted_capability_templates(
    templates: dict[str, dict], service_families: dict[str, str],
) -> dict[str, dict]:
    represented = set(service_families.values())
    unlisted = {name: value for name, value in templates.items() if name not in represented}
    return unlisted or templates


def _capability_recurring_potential(template: dict) -> bool:
    text = " ".join(str(value) for value in (
        template.get("name"), template.get("description"),
        template.get("deliverables"), template.get("principles"),
    ) if value).lower()
    return any(term in text for term in (
        "recurring", "maintenance", "subscription", "monthly", "継続", "保守", "月額",
    ))


def _invoke_demand_proposal(
    *, runner: Path, schema: Path, workdir: Path, evidence_dir: Path,
    families: dict, catalog_titles: list[str], timeout_seconds: int,
) -> tuple[dict, dict]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    context = {"owned_capability_families": families, "current_catalog_titles": catalog_titles}
    prompt = """Name at most three official Coconala search queries worth measuring as demand for
work this seller can already deliver, and return only the strict schema object. Every query must
name one owned capability family from CONTEXT_JSON and must not repeat what current_catalog_titles
already sell. Do not claim demand exists, do not estimate volume, revenue or competition: the loop
decides that by crawling the official search page. Choose no_op with a reason when no distinct
query is supported by an owned family.\nCONTEXT_JSON=""" + json.dumps(
        context, ensure_ascii=False, separators=(",", ":"))
    started = time.time()
    completed = subprocess.run(
        [sys.executable, str(runner), "--task-class", "storefront-proposal-agent", "--prompt-stdin",
         "--schema", str(schema), "--evidence-dir", str(evidence_dir),
         "--task-label", "gig-storefront-demand", "--loop", "gig-storefront",
         "--workdir", str(workdir), "--timeout-seconds", str(timeout_seconds)],
        input=prompt, text=True, capture_output=True, env=os.environ.copy(),
        timeout=timeout_seconds + 30, check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise RuntimeError(f"storefront_demand_proposal_failed:{completed.returncode}:{detail}")
    try:
        summary_path = evidence_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        result_path = Path(str(summary["result_path"])).resolve()
        result_path.relative_to(evidence_dir.resolve())
        if (summary.get("status") != "success"
                or summary.get("task_class") != "storefront-proposal-agent"
                or summary.get("selected_provider") != "codex"
                or summary.get("selected_model") != "gpt-5.6-terra"
                or min(summary_path.stat().st_mtime, result_path.stat().st_mtime) < started):
            raise ValueError("stale_or_wrong_route")
        proposal = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_demand_proposal_evidence_invalid") from error
    route = {"task_class": summary["task_class"], "provider": summary["selected_provider"],
             "model": summary["selected_model"], "effort": summary.get("selected_effort")}
    return proposal, route


# Reasons `_crawl_demand_cluster` can raise that describe ONE query failing to read
# (a tab that would not open, a redirect off coconala.com, an empty page after the
# house retry) rather than a decision about our account or config. The exploration
# loop over several proposed candidates skips a query that fails for one of these and
# keeps exploring the rest; the single-query bootstrap sell-decision crawl never
# consults this set because there the same failure IS the whole wake's answer.
DEMAND_CRAWL_PER_QUERY_REASONS = frozenset({
    "storefront_demand_tab_open_failed",
    "storefront_demand_source_not_official",
    "storefront_demand_source_empty",
})


def _demand_crawl_failure_is_per_query(error: RuntimeError) -> bool:
    return str(error) in DEMAND_CRAWL_PER_QUERY_REASONS


def _crawl_demand_cluster(default_tab_script: Path, evidence_dir: Path, query: str) -> dict:
    """Crawl one official search page and read the demand it states.

    Uses its own tab: by the time this runs the wake has opened and closed many targets,
    and reusing the leased page's socket is what made a whole wake die on HTTP 500.
    """
    import listing_inventory
    import storefront_draft

    url = "https://coconala.com/search?keyword=" + quote(query)
    opened = storefront_draft.open_tab_with_retry(
        [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
         "--background", "open", url], capture_output=True, text=True, check=False, timeout=30,
    )
    tab = None
    observed: dict = {}
    try:
        tab = json.loads(opened.stdout)
        if opened.returncode != 0 or tab.get("ok") is not True:
            raise RuntimeError("storefront_demand_tab_open_failed")
        # An empty body here is the same transient the competitor collector retries
        # (the search results tab was still hydrating), so it gets the same house
        # shape. This one function serves two very different callers: a bootstrap wake
        # that crawls exactly one chosen query (there, exhausting retries IS the whole
        # wake's answer and stays fatal -- see that call site) and an exploration loop
        # over several proposed queries (there, the call site catches this and skips
        # just that query). The retry belongs here so both callers get it; which
        # failure is per-item versus whole-job is a caller decision, not this one's.
        for attempt in range(5):
            observed = asyncio.run(listing_inventory._eval_json(
                str(tab["ws"]), url,
                "JSON.stringify({url:location.href,body:document.body ? document.body.innerText.slice(0,120000) : ''})",
            ))
            final = urlsplit(str(observed.get("url") or ""))
            body = str(observed.get("body") or "")
            if final.scheme != "https" or final.hostname not in {"coconala.com", "www.coconala.com"}:
                raise RuntimeError("storefront_demand_source_not_official")
            if body.strip():
                break
            if attempt < 4:
                time.sleep(3)
    finally:
        if isinstance(tab, dict) and tab.get("target_id"):
            subprocess.run(
                [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
                 "close", str(tab["target_id"])], capture_output=True, text=True,
                check=False, timeout=30,
            )
    body = str(observed.get("body") or "")
    if not body.strip():
        raise RuntimeError("storefront_demand_source_empty")
    path = evidence_dir / f"demand-search-{hashlib.sha256(url.encode()).hexdigest()[:12]}.json"
    _atomic_write(path, {"official": True, "query": query, "url": str(observed.get("url")),
                         "body": body, "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
                         "observed_at_epoch": int(time.time())})
    demand = _extract_search_demand(body)
    return {**demand, "query": query, "search_url": url, "evidence_path": str(path)}


def _invoke_category_proposal(
    *, runner: Path, schema: Path, workdir: Path, evidence_dir: Path,
    cluster: dict, options: list, timeout_seconds: int,
) -> tuple[dict, dict]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    context = {
        "demand_cluster": {k: cluster.get(k) for k in
                           ("query", "capability_family", "visible_result_count", "median_price_jpy")},
        "official_master_categories": [
            {"value": str(row.get("value")), "label": str(row.get("label") or "")}
            for row in options if isinstance(row, dict) and str(row.get("value") or "").strip()
        ],
    }
    prompt = """Choose the one official Coconala top-level category a service for this demand cluster
belongs in, and return only the strict schema object. master_category_value must be copied exactly
from official_master_categories; never invent an id. Choose no_op with a reason when no official
category fits the cluster.\nCONTEXT_JSON=""" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    completed = subprocess.run(
        [sys.executable, str(runner), "--task-class", "storefront-proposal-agent", "--prompt-stdin",
         "--schema", str(schema), "--evidence-dir", str(evidence_dir),
         "--task-label", "gig-storefront-category", "--loop", "gig-storefront",
         "--workdir", str(workdir), "--timeout-seconds", str(timeout_seconds)],
        input=prompt, text=True, capture_output=True, env=os.environ.copy(),
        timeout=timeout_seconds + 30, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"storefront_category_proposal_failed:{completed.returncode}")
    summary = json.loads((evidence_dir / "summary.json").read_text(encoding="utf-8"))
    result_path = Path(str(summary["result_path"])).resolve()
    result_path.relative_to(evidence_dir.resolve())
    if summary.get("status") != "success" or summary.get("selected_model") != "gpt-5.6-terra":
        raise RuntimeError("storefront_category_proposal_evidence_invalid")
    return json.loads(result_path.read_text(encoding="utf-8")), {
        "provider": summary.get("selected_provider"), "model": summary.get("selected_model")}


def _invoke_category_child_proposal(
    *, runner: Path, schema: Path, workdir: Path, evidence_dir: Path,
    cluster: dict, master: dict, children: dict, timeout_seconds: int,
) -> tuple[dict, dict]:
    """Pick the official sub category and type inside an already chosen top-level category."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    context = {
        "demand_cluster": {k: cluster.get(k) for k in ("query", "capability_family")},
        "chosen_master_category": master,
        "official_sub_categories": children.get("data[Service][master_sub_category]") or [],
        "official_category_types": children.get("data[Service][master_category_type_id]") or [],
    }
    prompt = """Choose the official sub category and category type for this demand cluster inside the
already chosen top-level category, and return only the strict schema object. Both values must be
copied exactly from the official lists in CONTEXT_JSON; never invent an id.\nCONTEXT_JSON=""" + json.dumps(
        context, ensure_ascii=False, separators=(",", ":"))
    completed = subprocess.run(
        [sys.executable, str(runner), "--task-class", "storefront-proposal-agent", "--prompt-stdin",
         "--schema", str(schema), "--evidence-dir", str(evidence_dir),
         "--task-label", "gig-storefront-category-child", "--loop", "gig-storefront",
         "--workdir", str(workdir), "--timeout-seconds", str(timeout_seconds)],
        input=prompt, text=True, capture_output=True, env=os.environ.copy(),
        timeout=timeout_seconds + 30, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"storefront_category_child_failed:{completed.returncode}")
    summary = json.loads((evidence_dir / "summary.json").read_text(encoding="utf-8"))
    result_path = Path(str(summary["result_path"])).resolve()
    result_path.relative_to(evidence_dir.resolve())
    if summary.get("status") != "success" or summary.get("selected_model") != "gpt-5.6-terra":
        raise RuntimeError("storefront_category_child_evidence_invalid")
    return json.loads(result_path.read_text(encoding="utf-8")), {
        "provider": summary.get("selected_provider"), "model": summary.get("selected_model")}


def _validate_category_choice(chosen_value: str, options: list, level: str = "category") -> dict:
    """Bind a category to an option the official seller form actually offers.

    Category ids are not transferable between listings: the committed contract's
    `19/372/150` belongs to writing, and a new market needs its own official triple.
    """
    rows = [row for row in options or [] if isinstance(row, dict) and str(row.get("value") or "").strip()]
    if not rows:
        raise RuntimeError(f"storefront_category_options_unobserved:{level}")
    match = next((row for row in rows if str(row["value"]) == str(chosen_value).strip()), None)
    if match is None:
        raise RuntimeError(f"storefront_category_choice_not_official:{level}:{chosen_value}")
    return {"value": str(match["value"]), "label": str(match.get("label") or "").strip()}


def _invoke_facet_proposal(
    *, runner: Path, schema: Path, workdir: Path, evidence_dir: Path,
    listing: dict, facet_groups: dict[str, dict], timeout_seconds: int,
) -> tuple[dict, dict]:
    """Choose values for every facet group the bound category's live form actually renders.

    Facet groups and their option ids are category-specific and unknown until the draft renders
    them: a writing category's ids never appear on a development category's form, which renders
    an entirely different set. Only the groups read off THIS draft are ever offered.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    context = {
        "listing": listing,
        "facet_groups": [
            {"group_id": group_id, "group_label": group["group_label"],
             "required": group["required"], "max_select": group.get("max_select"),
             "options": group["options"]}
            for group_id, group in sorted(facet_groups.items())
        ],
    }
    prompt = """Choose the official facet values this Coconala category's live form renders for the
listing in CONTEXT_JSON, and return only the strict schema object. `facets` is an array with at most
one entry per group_id from CONTEXT_JSON.facet_groups; every required facet group must appear with at
least one value, and when a group's max_select is set, never return more values for it than that. Copy
every value exactly from that group's own official options; never invent a value or move one between
groups. Ground each pick in the listing's own title/catchphrase/head/body/category, not in the group's
id or a guess. Omit a non-required group entirely, or give it an empty values array, when nothing in
the listing supports a choice.\nCONTEXT_JSON=""" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    completed = subprocess.run(
        [sys.executable, str(runner), "--task-class", "storefront-proposal-agent", "--prompt-stdin",
         "--schema", str(schema), "--evidence-dir", str(evidence_dir),
         "--task-label", "gig-storefront-facet-proposal", "--loop", "gig-storefront",
         "--workdir", str(workdir), "--timeout-seconds", str(timeout_seconds)],
        input=prompt, text=True, capture_output=True, env=os.environ.copy(),
        timeout=timeout_seconds + 30, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"storefront_facet_proposal_failed:{completed.returncode}")
    summary = json.loads((evidence_dir / "summary.json").read_text(encoding="utf-8"))
    result_path = Path(str(summary["result_path"])).resolve()
    result_path.relative_to(evidence_dir.resolve())
    if summary.get("status") != "success" or summary.get("selected_model") != "gpt-5.6-terra":
        raise RuntimeError("storefront_facet_proposal_evidence_invalid")
    return json.loads(result_path.read_text(encoding="utf-8")), {
        "provider": summary.get("selected_provider"), "model": summary.get("selected_model")}


def _validate_facet_selection(chosen: dict, facet_groups: dict[str, dict]) -> dict[str, list[str]]:
    """Bind facet picks to the official values the live form actually offered, group by group.

    A group the form never rendered has nothing to verify. A required group left empty, a value
    outside that exact group's own options, or a count over its own max_select means the pick did
    not come from the live form, and Coconala's own required-field check would reject it anyway.
    """
    picks_list = chosen.get("facets")
    # A dynamic per-category key (one entry per discovered facet group) cannot be expressed as a
    # fixed JSON Schema object in strict structured-output mode, so the agent returns an array of
    # {group_id, values} instead; this is the one place that shape gets turned back into a map.
    if not isinstance(picks_list, list) or not all(
        isinstance(row, dict) and isinstance(row.get("group_id"), str) for row in picks_list
    ):
        raise RuntimeError("storefront_facet_selection_invalid")
    if len(picks_list) != len({row["group_id"] for row in picks_list}):
        raise RuntimeError("storefront_facet_selection_duplicate_group")
    picks = {row["group_id"]: row.get("values") for row in picks_list}
    resolved: dict[str, list[str]] = {}
    for group_id, group in facet_groups.items():
        official = {str(option["value"]) for option in group["options"]}
        values = picks.get(group_id)
        if values is None:
            values = []
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise RuntimeError(f"storefront_facet_selection_type_invalid:{group_id}")
        chosen_values = list(dict.fromkeys(values))
        if not set(chosen_values) <= official:
            raise RuntimeError(f"storefront_facet_selection_not_official:{group_id}")
        if group.get("required") and not chosen_values:
            raise RuntimeError(f"storefront_facet_selection_required_empty:{group_id}")
        max_select = group.get("max_select")
        if isinstance(max_select, int) and len(chosen_values) > max_select:
            raise RuntimeError(f"storefront_facet_selection_over_max:{group_id}")
        if chosen_values:
            resolved[group_id] = chosen_values
    return resolved


def _demand_cluster_key(query: str, category_url: str) -> str:
    identity = json.dumps({"query": query.strip(), "category": category_url.strip()},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "storefront:demand:v1:" + hashlib.sha256(identity.encode()).hexdigest()


def _last_published_create_epoch(state_dir: Path) -> int | None:
    """When this loop last published a brand-new listing, read from its own wake receipts."""
    path = state_dir / "wakes.jsonl"
    if not path.is_file():
        return None
    latest = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        draft = row.get("new_listing_draft") if isinstance(row.get("new_listing_draft"), dict) else {}
        if (int(draft.get("public_effect") or 0) == 1
                and str(draft.get("candidate_key") or "").startswith("storefront:create:v1:")
                and type(row.get("observed_at_epoch")) is int):
            latest = max(latest or 0, row["observed_at_epoch"])
    return latest


def _render_replace_plan(retire_contract: dict, create_contract: dict | None, allocation: dict) -> dict:
    # Moved to storefront_kernel.render_replace_plan, generalized to take the platform string
    # as a parameter instead of hardcoding Coconala's; see _validate_mutation_contract for why
    # this stays as an alias.
    return _storefront_kernel().render_replace_plan(
        retire_contract, create_contract, allocation, platform="coconala",
    )


def _load_text_mutation_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_text_mutation_spec_invalid") from error
    required = {
        "version", "platform", "service_id", "capability_family", "changed_field", "form_field",
        "before_value", "proposed_value", "rollback_value", "official_readback", "success_metric",
        "observation_window_days", "evidence",
    }
    if (set(spec) != required or spec.get("version") != 1 or spec.get("platform") != "coconala"
            or spec.get("changed_field") not in {"title", "catchphrase", "body", "package", "FAQ", "price"}
            or not str(spec.get("form_field") or "").startswith("data[")
            or not all(isinstance(spec.get(key), str) and spec[key].strip()
                       for key in ("before_value", "proposed_value", "rollback_value"))
            or spec["before_value"] != spec["rollback_value"]
            or spec["before_value"] == spec["proposed_value"]
            or not isinstance(spec.get("evidence"), list) or not spec["evidence"]):
        raise RuntimeError("storefront_text_mutation_spec_fields_invalid")
    return spec


def _render_text_mutation(
    path: Path, sources: list[dict], seller_snapshot: dict, capability_families: dict[str, str],
) -> dict:
    spec = _load_text_mutation_spec(path)
    source = next((row for row in sources if row["service_id"] == str(spec.get("service_id") or "")), None)
    if (source is None
            or capability_families.get(str(spec.get("service_id") or "")) != spec.get("capability_family")):
        raise RuntimeError("storefront_text_mutation_spec_fields_invalid")
    fields = {
        str(row.get("name") or ""): str(row.get("value") or "")
        for row in seller_snapshot.get("fields", []) if isinstance(row, dict)
    }
    faq_fields = {name: value for name, value in fields.items() if name.startswith("data[Faq]")}
    current_matches = (
        not faq_fields and spec["before_value"] == "FAQ_ABSENT"
        if spec["changed_field"] == "FAQ" else fields.get(spec["form_field"]) == spec["before_value"]
    )
    if seller_snapshot.get("url") != f'https://coconala.com/mypage/services/{source["service_id"]}':
        raise RuntimeError("storefront_text_mutation_before_not_current")
    if not current_matches:
        # A committed seed whose before-state the live listing has moved past describes work
        # that is already done. The loop improving that listing is what makes this happen, so
        # failing the wake here means every success breaks the next wake.
        return None
    if spec["changed_field"] == "title":
        if spec["official_readback"].get("public_title") != f'{spec["proposed_value"]}ます':
            raise RuntimeError("storefront_title_readback_contract_invalid")
    if (spec["changed_field"] == "body"
            and spec["official_readback"].get("public_body_sha256")
            != hashlib.sha256(spec["proposed_value"].encode()).hexdigest()):
        raise RuntimeError("storefront_body_readback_contract_invalid")
    if (spec["changed_field"] == "package"
            and (spec["official_readback"].get("option_title") != spec["proposed_value"]
                 or type(spec["official_readback"].get("option_price_jpy")) is not int)):
        raise RuntimeError("storefront_package_readback_contract_invalid")
    if spec["changed_field"] == "FAQ":
        question, answer = _split_faq(spec["proposed_value"])
        if spec["official_readback"] != {"question": question, "answer": answer}:
            raise RuntimeError("storefront_faq_readback_contract_invalid")
    if spec["changed_field"] == "price":
        readback = spec["official_readback"]
        proposed_jpy = readback.get("public_price_jpy")
        before_label = f'{source["price_jpy"]:,}円'
        proposed_label = f"{proposed_jpy:,}円" if type(proposed_jpy) is int and proposed_jpy > 0 else None
        options = seller_snapshot.get("select_options", {}).get(spec["form_field"], [])
        option_pairs = {
            (str(row.get("value") or ""), str(row.get("label") or ""))
            for row in options if isinstance(row, dict)
        }
        if (readback != {
                "seller_option_value": spec["proposed_value"],
                "seller_option_label": proposed_label,
                "public_price_jpy": proposed_jpy,
            }
                or (spec["before_value"], before_label) not in option_pairs
                or (spec["proposed_value"], proposed_label) not in option_pairs):
            raise RuntimeError("storefront_price_option_binding_invalid")
    unsigned = {
        "version": 1, "platform": "coconala", "service_id": source["service_id"],
        "precondition_listing_version_sha256": source["service_version_sha256"],
        "changed_field": spec["changed_field"], "before_value": spec["before_value"],
        "proposed_value": spec["proposed_value"], "allowed_delta": [spec["form_field"]],
        "rollback_value": spec["rollback_value"], "official_readback": spec["official_readback"],
        "success_metric": spec["success_metric"], "observation_window_days": spec["observation_window_days"],
        "capability_family": spec["capability_family"], "evidence": spec["evidence"],
    }
    contract = _seal_mutation_contract(unsigned, capability_families)
    before = {spec["form_field"]: spec["before_value"]}
    after = {**before, spec["form_field"]: spec["proposed_value"]}
    delta = [key for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
    if delta != contract["allowed_delta"]:
        raise RuntimeError("storefront_text_mutation_multi_field_delta")
    return {"version": 1, "contract": contract, "before": before, "after": after, "delta": delta, "published": False}


def _render_prepared_mutation(
    state_dir: Path, seller_snapshot: dict, service_id: str, changed_field: str,
    capability_families: dict[str, str],
) -> dict | None:
    for path in sorted((state_dir / "effect-intents").glob("*.json")):
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (intent.get("status") not in {"prepared", "confirmed"} or intent.get("service_id") != service_id
                or intent.get("changed_field") != changed_field):
            continue
        contract = intent.get("mutation_contract")
        if not isinstance(contract, dict):
            raise RuntimeError("prepared_mutation_contract_missing")
        _validate_mutation_contract(contract, capability_families)
        field = contract["allowed_delta"][0]
        values = {str(row.get("name") or ""): str(row.get("value") or "")
                  for row in seller_snapshot.get("fields") or [] if isinstance(row, dict)}
        if values.get(field) != contract["proposed_value"]:
            return None
        return {"version": 1, "contract": contract, "before": {field: contract["before_value"]},
                "after": {field: contract["proposed_value"]}, "delta": [field], "published": True}
    return None


# Recorded per run so a stale hand-authored contract is visible instead of fatal.
_stale_listing_contracts: list[dict] = []


def _validate_listing_contract_static(contract: object) -> dict:
    if not isinstance(contract, dict):
        raise RuntimeError("listing_contract_static_invalid")
    service_id = str(contract.get("service_id") or "")
    offer = contract.get("offer")
    playbook = contract.get("inquiry_playbook")
    patterns = playbook.get("answer_patterns") if isinstance(playbook, dict) else None
    required_inputs = offer.get("required_inputs") if isinstance(offer, dict) else None
    if (contract.get("version") != 1 or contract.get("platform") != "coconala"
            or not service_id.isdigit()
            or contract.get("public_url") != f"https://coconala.com/services/{service_id}"
            or not re.fullmatch(r"[0-9a-f]{64}", str(contract.get("service_version_sha256") or ""))
            or not isinstance(patterns, list) or not patterns
            or not all(isinstance(row, dict) and str(row.get("intent") or "").strip()
                       and isinstance(row.get("triggers"), list) and row["triggers"]
                       and str(row.get("response") or "").strip() for row in patterns)
            or not isinstance(required_inputs, list) or not required_inputs):
        raise RuntimeError("listing_contract_static_invalid")
    return contract


def _generated_listing_family_template() -> dict:
    """Reply contract shared by products created from public capability and demand evidence."""
    required_inputs = [
        "達成したい結果", "現在の手順", "代表的な入力例", "期待する出力例",
        "利用環境・ツール", "権限と承認が必要な操作", "希望納期",
    ]
    return {
        "inclusions": ["出品ページに記載した基本範囲", "購入後に合意した対象1件の制作・実施"],
        "deliverables": ["合意した成果物", "確認・検証結果", "利用・引継ぎ手順"],
        "required_inputs": required_inputs,
        "principles": [
            "入力例と期待結果を確認する前に実現性・納期・成果を保証しない",
            "出品範囲外、未承認の外部操作、秘密情報の成果物混入を認めない",
            "成果物を検証し、既知の制約と継続支援の境界を明記する",
        ],
        "answer_patterns": [{
            "intent": "scope_and_feasibility",
            "triggers": ["対応できますか", "何が納品", "費用", "納期", "継続"],
            "response": "出品ページの基本範囲を基準に、現在の手順、代表入力、期待する出力、利用環境、権限・承認境界、希望納期を確認して対応範囲とお見積りをご案内します。",
        }],
    }


def _load_listing_contracts(
    root: Path, observed_contracts: list[dict], families_path: Path = DEFAULT_LISTING_CONTRACT_FAMILIES,
    created_path: Path | None = None,
) -> list[dict]:
    observed = {row["service_id"]: row for row in observed_contracts}
    _stale_listing_contracts.clear()
    loaded = []
    for path in sorted(root.glob("*.json")):
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("listing_contract_invalid") from error
        service_id = str(contract.get("service_id") or "")
        source = observed.get(service_id)
        offer = contract.get("offer")
        playbook = contract.get("inquiry_playbook")
        if (contract.get("version") != 1 or contract.get("platform") != "coconala" or source is None
                or contract.get("public_url") != source["public_url"]
                or contract.get("service_version_sha256") != source["service_version_sha256"]
                or not isinstance(offer, dict) or offer.get("base_price_jpy") != source["price_jpy"]
                or not isinstance(playbook, dict)):
            # A hand-authored contract binds one exact listing version. When the live listing
            # moves on, that contract is stale, not the catalogue: skipping it loses this
            # listing's playbook until it is re-authored, while failing here would stop every
            # full wake for every other listing too.
            _stale_listing_contracts.append({"service_id": service_id or path.name,
                                             "source_path": str(path.resolve()),
                                             "reason": "listing_contract_binding_stale"})
            continue
        try:
            _validate_listing_contract_static(contract)
        except RuntimeError as error:
            raise RuntimeError(f"listing_contract_playbook_invalid:{service_id}") from error
        canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        loaded.append({
            **contract,
            "contract_key": f"storefront:contract:v1:{service_id}:{hashlib.sha256(canonical.encode()).hexdigest()}",
            "source_path": str(path.resolve()), "observed_at_epoch": int(time.time()),
        })
    explicit_ids = {row["service_id"] for row in loaded}
    mappings, families = _load_capability_families(families_path)
    if created_path is not None:
        created_rows, created_error = _jsonl_rows(created_path)
        if created_error:
            raise RuntimeError("created_listing_family_ledger_invalid")
        for row in created_rows:
            service_id = str(row.get("draft_service_id") or "")
            family_name = row.get("capability_family")
            if service_id.isdigit() and service_id in observed:
                if service_id in mappings:
                    continue
                if not isinstance(family_name, str) or not family_name.strip():
                    raise RuntimeError(f"created_listing_family_missing:{service_id}")
                families.setdefault(family_name, _generated_listing_family_template())
                mappings[service_id] = family_name
    for source in observed_contracts:
        service_id = source["service_id"]
        if service_id in explicit_ids:
            continue
        family_name = mappings.get(service_id)
        template = families.get(family_name)
        if not isinstance(family_name, str) or not isinstance(template, dict):
            raise RuntimeError(f"listing_contract_family_missing:{service_id}")
        required = ("inclusions", "deliverables", "required_inputs", "principles", "answer_patterns")
        if any(not isinstance(template.get(key), list) or not template[key] for key in required):
            raise RuntimeError(f"listing_contract_family_invalid:{family_name}")
        contract = {
            "version": 1, "platform": "coconala", "service_id": service_id,
            "service_version_sha256": source["service_version_sha256"],
            "public_url": source["public_url"],
            "offer": {
                "outcome": source["title"], "inclusions": template["inclusions"],
                "deliverables": template["deliverables"],
                "required_inputs": template["required_inputs"],
                "base_price_jpy": source["price_jpy"], "options": [],
            },
            "inquiry_playbook": {
                "principles": template["principles"],
                "required_clarifications": template["required_inputs"],
                "answer_patterns": template["answer_patterns"],
            },
            "generated_from_family": family_name,
            "handoff_required_fields": [
                "platform", "service_id", "service_version_sha256", "conversation_id",
                "origin", "observed_at_epoch",
            ],
        }
        patterns = contract["inquiry_playbook"]["answer_patterns"]
        if not all(
            isinstance(row, dict) and str(row.get("intent") or "").strip()
            and isinstance(row.get("triggers"), list) and row["triggers"]
            and str(row.get("response") or "").strip() for row in patterns
        ):
            raise RuntimeError(f"listing_contract_family_invalid:{family_name}")
        canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        loaded.append({
            **contract,
            "contract_key": f"storefront:contract:v1:{service_id}:{hashlib.sha256(canonical.encode()).hexdigest()}",
            "source_path": str(families_path.resolve()), "observed_at_epoch": int(time.time()),
        })
    if {row["service_id"] for row in loaded} != set(observed):
        raise RuntimeError("listing_contract_inventory_incomplete")
    return loaded


def _analytics_count(body: str, label: str, unit: str) -> int:
    match = re.search(rf"{re.escape(label)}\s+([0-9０-９,，]+)\s*{re.escape(unit)}", body)
    if match is None:
        raise RuntimeError(f"official_analytics_{label}_unreadable")
    digits = match.group(1).translate(str.maketrans("０１２３４５６７８９，", "0123456789,"))
    return int(digits.replace(",", ""))


def _observe_draft_controls(
    evidence_dir: Path, draft_ids: list[str], default_tab_script: Path = DEFAULT_TAB,
) -> dict:
    """Record what a draft's own edit page offers, so a cleanup binds an observed control.

    Nine drafts accumulated from failed publications. The card in the seller list offers only
    編集する, and the delete control was seen once in a publish error page's text with no
    element behind it, which is not something to act on.
    """
    import listing_inventory
    import storefront_draft

    if not draft_ids:
        return {"observed": 0}
    service_id = sorted(draft_ids)[0]
    url = f"https://coconala.com/mypage/services/{service_id}"
    opened = storefront_draft.open_tab_with_retry(
        [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
         "--background", "open", "about:blank"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    observed: dict = {}
    tab = None
    try:
        tab = json.loads(opened.stdout)
        if opened.returncode == 0 and tab.get("ok") is True:
            observed = asyncio.run(listing_inventory._eval_json(
                str(tab["ws"]), url,
                "JSON.stringify({url:location.href,controls:[...document.querySelectorAll('a,button')]"
                ".map(e=>({tag:e.tagName,label:((e.innerText||'')+'').trim().slice(0,20),"
                "href:e.getAttribute('href')||null,cls:((e.className||'')+'').slice(0,60)}))"
                ".filter(e=>e.label&&/削除|取り消|停止|下書き|公開/.test(e.label)).slice(0,20)})",
            ))
    except (KeyError, ValueError, OSError, RuntimeError) as error:
        observed = {"error": f"{type(error).__name__}:{str(error)[:120]}"}
    finally:
        if isinstance(tab, dict) and tab.get("target_id"):
            subprocess.run(
                [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
                 "close", str(tab["target_id"])], capture_output=True, text=True,
                check=False, timeout=30,
            )
    record = {"version": 1, "draft_service_id": service_id, "draft_count": len(draft_ids),
              "drafts": sorted(draft_ids), "observed": observed}
    _atomic_write(evidence_dir / "draft-controls.json", record)
    return record


def _delete_one_draft(
    evidence_dir: Path, service_id: str, default_tab_script: Path = DEFAULT_TAB,
) -> dict:
    """Delete one abandoned draft through the control its own page names.

    Bound to `a.js_prevent-secession-confirm` (`下書きを削除`) by class, never by label: the
    same page also carries `削除する`, which removes a paid option. Whatever the click produces
    is recorded, and nothing else is clicked, because a deletion cannot be undone.
    """
    import listing_inventory
    import storefront_draft

    url = f"https://coconala.com/mypage/services/{service_id}"
    opened = storefront_draft.open_tab_with_retry(
        [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
         "--background", "open", "about:blank"],
        capture_output=True, text=True, check=False, timeout=30,
    )
    result: dict = {"service_id": service_id}
    tab = None
    try:
        tab = json.loads(opened.stdout)
        if opened.returncode != 0 or tab.get("ok") is not True:
            raise RuntimeError("storefront_draft_delete_tab_open_failed")
        # The page is reached first and reported on its own, because a click that never returns
        # cannot say whether the navigation or the click was what hung.
        result["reached"] = asyncio.run(listing_inventory._eval_json(
            str(tab["ws"]), url,
            "JSON.stringify({url:location.href,"
            "has_control:!!document.querySelector('a.js_prevent-secession-confirm'),"
            "title:(document.title||'').slice(0,40)})",
        ))
        # The control opens a native confirm, which blocks every CDP evaluation until it is
        # answered: the page was reached with the control present and the click never returned.
        # Answering it in the page is simpler than driving the dialog protocol.
        result["clicked"] = asyncio.run(listing_inventory._eval_json(
            str(tab["ws"]), url,
            "JSON.stringify((()=>{const a=document.querySelector('a.js_prevent-secession-confirm');"
            "if(!a)return{found:false};window.confirm=()=>true;window.onbeforeunload=null;"
            "a.click();return{found:true,label:((a.innerText||'')+'').trim()}})())",
        ))
        if not (result["clicked"] or {}).get("found"):
            raise RuntimeError("storefront_draft_delete_control_absent")
        time.sleep(3)
        result["after"] = asyncio.run(listing_inventory._eval_json(
            str(tab["ws"]), url,
            "JSON.stringify({url:location.href,"
            # Unfiltered: the click reported success and the filtered view came back empty, so
            # the filter was hiding whatever the page actually put on screen.
            "dialogs:[...document.querySelectorAll('[class*=modal],[class*=dialog],[role=dialog]')]"
            ".map(e=>((e.innerText||'')+'').trim().slice(0,120)).filter(Boolean).slice(0,4),"
            "controls:[...document.querySelectorAll('a,button')]"
            ".map(e=>({tag:e.tagName,label:((e.innerText||'')+'').trim().slice(0,16),"
            "cls:((e.className||'')+'').slice(0,50)})).filter(e=>e.label).slice(0,30)})",
        ))
    except (KeyError, ValueError, OSError, RuntimeError) as error:
        result["error"] = f"{type(error).__name__}:{str(error)[:140]}"
    finally:
        if isinstance(tab, dict) and tab.get("target_id"):
            subprocess.run(
                [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
                 "close", str(tab["target_id"])], capture_output=True, text=True,
                check=False, timeout=30,
            )
    _atomic_write(evidence_dir / f"draft-delete-{service_id}.json", result)
    return result


def _traffic_without_inquiries(
    analytics_path: Path, funnel_path: Path, minimum_views: int = 30,
) -> list[str]:
    """Listings people look at and never contact, most-looked-at first.

    Measured 2026-08-18: 474 views across the catalogue produced one attributed inquiry and no
    purchases, so the break is at views to inquiry rather than inquiry to purchase. The
    scorecard ranks by which field is emptiest, which cannot see that.
    """
    views: dict[str, int] = {}
    if analytics_path.exists():
        for line in analytics_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            value = ((row.get("metrics") or {}).get("views") or {}).get("value")
            if str(row.get("service_id") or "") and type(value) is int:
                views[str(row["service_id"])] = value
    contacted = set()
    if funnel_path.exists():
        for line in funnel_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event_kind") == "inquiry" and str(row.get("service_id") or ""):
                contacted.add(str(row["service_id"]))
    ranked = [(count, service_id) for service_id, count in views.items()
              if count >= minimum_views and service_id not in contacted]
    return [service_id for _, service_id in sorted(ranked, reverse=True)]


def _subscription_heal_diverges(state_dir: Path, family_name: str, active_row: dict) -> bool:
    """True when the active draft's last confirmed contract needs a subscription correction
    Coconala's own edit form cannot actually apply.

    Draft 4385273 (mobile_app_dev) proved this empirically: unchecking `can_subscribe` and
    resubmitting never cleared the `discount_ratio` Coconala had already persisted from an
    earlier save, because the category never supports a subscription at all -- the platform
    simply does not process that field once `can_subscribe` is off. Re-healing forever cannot
    converge on such a draft, so its recorded contract is compared against what healing would
    produce today; a mismatch means the draft is stuck and must be rebuilt from a blank one
    rather than resubmitted again.
    """
    demand_evidence_path = str(active_row.get("demand_evidence_path") or "")
    if not demand_evidence_path:
        return False
    healed = _recover_prepared_create_contract(state_dir, family_name, demand_evidence_path)
    if healed is None:
        return False
    return str(healed.get("contract_sha256") or "") != str(active_row.get("contract_sha256") or "")


def _deletable_drafts(ledger_path: Path, draft_ids: list[str]) -> list[str]:
    """Drafts this loop abandoned, never those with publication history.

    `4356229` is a draft because the platform withdrew it, not because a publication failed:
    it carries views and a listing history, so it is repaired or left alone, never deleted.
    """
    published = set()
    active_by_family: dict[str, str] = {}
    active_rows: dict[str, dict] = {}
    if ledger_path.exists():
        for line in ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("status") == "published":
                published.add(str(row.get("draft_service_id") or ""))
            family = str(row.get("capability_family") or "")
            draft_id = str(row.get("draft_service_id") or "")
            # `prepare_draft`'s own return dict uses status "prepared", never "draft_prepared" --
            # that second string never matches a real row, so this membership test used to only
            # ever capture a draft's *create* event and silently skip its later *prepare* event.
            # `active_by_family` still ended up with the right draft_id either way (the id does
            # not change between the two events), which is why this stayed dormant. But
            # `active_rows` feeds `_subscription_heal_diverges`, which needs the row that actually
            # reflects the draft's current, form-verified contract -- handing it the stale
            # just-created row compared a healthy prepared draft's healed contract against an
            # earlier, different contract_sha256 and always looked like a mismatch. That false
            # positive got 4385965 -- a correctly healed, freshly prepared draft -- deleted for no
            # reason on 2026-09-05.
            if (row.get("status") in {"draft_created", "prepared"}
                    and int(row.get("public_effect") or 0) == 0 and family and draft_id.isdigit()):
                active_by_family[family] = draft_id
                active_rows[family] = row
    protected = published | set(active_by_family.values())
    # Losing protection here is narrow and specific: only a draft whose own recorded contract
    # provably cannot converge (see `_subscription_heal_diverges`) loses it, never one that is
    # merely mid-flight.
    stuck = {
        draft_id for family, draft_id in active_by_family.items()
        if _subscription_heal_diverges(ledger_path.parent, family, active_rows[family])
    }
    protected -= stuck
    return [value for value in sorted(draft_ids) if value not in protected]


def _observed_deleted_draft_ids(evidence_root: Path) -> set[str]:
    deleted = set()
    if evidence_root.is_dir():
        for path in evidence_root.glob("*/draft-delete-*.json"):
            match = re.fullmatch(r"draft-delete-(\d+)\.json", path.name)
            if match:
                deleted.add(match.group(1))
    return deleted


def _reseal_healed_contract(contract: dict) -> dict:
    # Moved to storefront_kernel.reseal_healed_contract (its private _healed_subscription
    # helper moved with it -- nothing else called that helper); see _validate_mutation_contract
    # for why this stays as an alias.
    return _storefront_kernel().reseal_healed_contract(contract)


def _families_with_unpublished_drafts(
    state_dir: Path, inventory_ids: set[str], live_draft_ids: set[str] | None = None,
) -> dict[str, tuple[str, int]]:
    # Moved to storefront_kernel.families_with_unpublished_drafts, generalized to take the
    # deleted-draft-ids lookup as a parameter: `_observed_deleted_draft_ids` itself stays here
    # because it has another caller (the wake loop below) and this module's own tests
    # monkeypatch it directly, so it is passed in by name -- looked up fresh on every call --
    # rather than imported into or duplicated inside the kernel.
    return _storefront_kernel().families_with_unpublished_drafts(
        state_dir, inventory_ids, observed_deleted_draft_ids=_observed_deleted_draft_ids,
        live_draft_ids=live_draft_ids,
    )


def _family_has_unpublished_draft(
    state_dir: Path, family_name: str, inventory_ids: set[str],
) -> str | None:
    # Moved to storefront_kernel.family_has_unpublished_draft; see
    # _families_with_unpublished_drafts for the observed_deleted_draft_ids wiring.
    return _storefront_kernel().family_has_unpublished_draft(
        state_dir, family_name, inventory_ids, observed_deleted_draft_ids=_observed_deleted_draft_ids,
    )


def _recover_prepared_create_contract(
    state_dir: Path, family_name: str, demand_evidence_path: str,
) -> dict | None:
    # Moved to storefront_kernel.recover_prepared_create_contract; see
    # _families_with_unpublished_drafts for the observed_deleted_draft_ids wiring.
    return _storefront_kernel().recover_prepared_create_contract(
        state_dir, family_name, demand_evidence_path,
        observed_deleted_draft_ids=_observed_deleted_draft_ids,
    )


def _invoke_duplicate_judgement_proposal(
    *, runner: Path, schema: Path, workdir: Path, evidence_dir: Path,
    catalogue: list[dict], timeout_seconds: int,
) -> tuple[dict, dict]:
    """Ask which live listings a buyer would treat as substitutes for one another.

    A character-overlap ratio cannot see that two differently-worded listings sell the same
    thing, or that two similarly-worded ones sell different things -- whether two listings are
    substitutes is a commercial judgement a buyer would make, not a string measure. This call
    hands the model nothing but the seller's own catalogue facts and reads back a strict schema.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)
    prompt = """Decide which of these live Coconala listings a buyer would treat as interchangeable
substitutes for one another -- selling the same underlying outcome under different wording -- as
opposed to listings that are genuinely different offers. Group only true substitutes: a listing
with nothing else like it in CATALOGUE_JSON must not appear in any group, and every group needs at
least two service_ids. For each group, name its service_ids copied exactly from CATALOGUE_JSON
(never invent one), pick keep_service_id as the stronger listing to keep among that group's own
service_ids using its price_jpy/views/purchases as evidence, and give a one-line reason a buyer
could not tell the group's listings apart. An empty groups array is the correct answer when
nothing in the catalogue duplicates anything else.
CATALOGUE_JSON=""" + json.dumps(catalogue, ensure_ascii=False, separators=(",", ":"))
    completed = subprocess.run(
        [sys.executable, str(runner), "--task-class", "storefront-proposal-agent", "--prompt-stdin",
         "--schema", str(schema), "--evidence-dir", str(evidence_dir),
         "--task-label", "gig-storefront-duplicate-judgement", "--loop", "gig-storefront",
         "--workdir", str(workdir), "--timeout-seconds", str(timeout_seconds)],
        input=prompt, text=True, capture_output=True, env=os.environ.copy(),
        timeout=timeout_seconds + 30, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"storefront_duplicate_judgement_failed:{completed.returncode}")
    summary = json.loads((evidence_dir / "summary.json").read_text(encoding="utf-8"))
    result_path = Path(str(summary["result_path"])).resolve()
    result_path.relative_to(evidence_dir.resolve())
    if (summary.get("status") != "success"
            or summary.get("selected_provider") != "codex"
            or summary.get("selected_model") != "gpt-5.6-terra"
            or summary.get("selected_effort") != "medium"):
        raise RuntimeError("storefront_duplicate_judgement_evidence_invalid")
    return json.loads(result_path.read_text(encoding="utf-8")), {
        "provider": summary.get("selected_provider"), "model": summary.get("selected_model"),
        "effort": summary.get("selected_effort"),
    }


def _validate_duplicate_judgement_groups(result: object, live_ids: set[str]) -> list[dict]:
    """Every mechanical guard over the model's answer lives here, never in the model's word alone.

    Any single violation -- an invented id, a group of one, an id claimed by two groups, a keep
    that is not a member of its own group, an empty reason -- invalidates the whole answer.
    Nothing is partially salvaged: a judgement this loop cannot fully trust it does not trust at
    all this wake.
    """
    if not isinstance(result, dict):
        raise RuntimeError("storefront_duplicate_judgement_not_object")
    groups = result.get("groups")
    if not isinstance(groups, list):
        raise RuntimeError("storefront_duplicate_judgement_groups_invalid")
    claimed: set[str] = set()
    validated: list[dict] = []
    for group in groups:
        if not isinstance(group, dict):
            raise RuntimeError("storefront_duplicate_judgement_group_invalid")
        ids = group.get("service_ids")
        keep = group.get("keep_service_id")
        reason = str(group.get("reason") or "").strip()
        if (not isinstance(ids, list) or len(ids) < 2
                or not all(isinstance(value, str) for value in ids)):
            raise RuntimeError("storefront_duplicate_judgement_group_size_invalid")
        if len(set(ids)) != len(ids):
            raise RuntimeError("storefront_duplicate_judgement_group_has_repeated_id")
        if not set(ids) <= live_ids:
            raise RuntimeError("storefront_duplicate_judgement_unknown_service_id")
        if claimed & set(ids):
            raise RuntimeError("storefront_duplicate_judgement_id_in_two_groups")
        claimed |= set(ids)
        if not isinstance(keep, str) or keep not in ids:
            raise RuntimeError("storefront_duplicate_judgement_keep_not_in_group")
        # The portfolio selector below always retires the numerically later (newer) service_id of
        # a pair and always spares the earlier one -- that sort is untouched by this feature. A
        # keep the model names that is not the group's earliest id could never actually survive
        # that sort, so the whole answer is invalid rather than silently retiring the model's pick.
        if keep != min(ids):
            raise RuntimeError("storefront_duplicate_judgement_keep_not_earliest_id")
        if not reason:
            raise RuntimeError("storefront_duplicate_judgement_reason_empty")
        validated.append({"service_ids": sorted(ids), "keep_service_id": keep, "reason": reason})
    return validated


def _seal_duplicate_judgement(groups: list[dict]) -> dict:
    unsigned = {"version": 1, "groups": groups}
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**unsigned, "content_sha256": hashlib.sha256(canonical.encode()).hexdigest()}


def _near_duplicate_listings(
    rows: list[dict], families: dict[str, str], *, state_dir: Path, evidence_dir: Path,
    price_by_service_id: dict[str, int] | None = None,
    runner: Path = DEFAULT_RUNNER, schema: Path = DEFAULT_DUPLICATE_JUDGEMENT_SCHEMA,
    workdir: Path = Path.home(), timeout_seconds: int = 180,
) -> list[dict]:
    """Report live listings a buyer would treat as substitutes for one another.

    Two Excel listings went live one word apart, `…要件を整理します` and `…仕様を整理します`, because
    the only duplicate guard once compared titles for exact equality, then for a 0.9 character-overlap
    ratio -- both blind to the actual case that let eight listings covering two ideas stay live, which
    measured at most 0.857 on that ratio. Whether two listings sell the same thing to a buyer is a
    commercial judgement, not a string measure, so a model makes that call under a strict schema; this
    function owns every mechanical guard over its answer and fails closed -- zero pairs, never a guess
    -- on any invalid or missing result.
    """
    import difflib

    titled = [row for row in rows if row.get("title_stem")]
    if len(titled) < 2:
        return []
    metrics_by_id: dict[str, dict] = {}
    rows_read, _ = _jsonl_rows(state_dir / "analytics.jsonl")
    for row in rows_read:
        if str(row.get("service_id") or "").isdigit():
            metrics_by_id[str(row["service_id"])] = row.get("metrics") or {}
    prices = price_by_service_id or {}
    stems = {str(row["service_id"]): str(row["title_stem"]) for row in titled}
    catalogue = [
        {
            "service_id": service_id, "title_stem": stems[service_id],
            "capability_family": families.get(service_id),
            "price_jpy": prices.get(service_id),
            "views": (metrics_by_id.get(service_id, {}).get("views") or {}).get("value"),
            "purchases": (metrics_by_id.get(service_id, {}).get("purchases") or {}).get("value"),
        }
        for service_id in stems
    ]
    live_ids = set(stems)
    try:
        result, _route = _invoke_duplicate_judgement_proposal(
            runner=runner, schema=schema, workdir=workdir, evidence_dir=evidence_dir,
            catalogue=catalogue, timeout_seconds=timeout_seconds,
        )
        groups = _validate_duplicate_judgement_groups(result, live_ids)
        sealed = _seal_duplicate_judgement(groups)
    except (RuntimeError, OSError, ValueError, TypeError, KeyError,
            json.JSONDecodeError, subprocess.SubprocessError) as error:
        _atomic_write(evidence_dir / "duplicate-judgement-error.json",
                      {"error": f"{type(error).__name__}:{str(error)[:200]}"})
        return []
    _atomic_write(evidence_dir / "duplicate-judgement.json", sealed)
    pairs = []
    for group in groups:
        keep = group["keep_service_id"]
        family = families.get(keep)
        for other in group["service_ids"]:
            if other == keep:
                continue
            ratio = round(difflib.SequenceMatcher(None, stems[keep], stems[other]).ratio(), 3)
            pairs.append({"service_ids": [keep, other], "capability_family": family,
                          "title_similarity": ratio, "titles": [stems[keep], stems[other]],
                          "reason": group["reason"]})
    return pairs


def _scan_public_copy(
    state_dir: Path, evidence_dir: Path, now: int, service_ids: list[str],
    default_tab_script: Path = DEFAULT_TAB, capability_families: dict[str, str] | None = None,
    *, price_by_service_id: dict[str, int] | None = None,
    runner: Path = DEFAULT_RUNNER, duplicate_judgement_schema: Path = DEFAULT_DUPLICATE_JUDGEMENT_SCHEMA,
    workdir: Path = Path.home(), timeout_seconds: int = 180,
) -> tuple[list[dict], list[dict]]:
    """Read the copy this seller wrote for each listing and report prohibited tool names.

    The platform states the rule by withdrawing a listing, and it withdrew the same one twice
    while the wording sat unread in the loop's own catalogue. Only the seller's own fields are
    read: the public page also carries Coconala's category navigation, which names spreadsheets
    on every listing and made the first whole-page scan report all fourteen as violations.
    A form that cannot be read is recorded as unreadable, never as compliant.
    """
    import listing_inventory

    findings: list[dict] = []
    scanned: list[dict] = []
    ledger = state_dir / "compliance-scan.jsonl"
    # Reading every listing on every full wake doubled this wake's browser work and took full-wake
    # success from 59 of 60 down to 3 of 8. Copy only changes when this loop changes it, so a
    # listing it just changed is read now and the rest rotate, oldest read first.
    previous: dict[str, dict] = {}
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                previous[str(row.get("service_id") or "")] = row
    changed_since_scan = set()
    effects_path = state_dir / "effects.jsonl"
    if effects_path.exists():
        for line in effects_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            effect = json.loads(line)
            service_id = str(effect.get("service_id") or "")
            if (effect.get("status") == "accepted" and effect.get("effect") == 1
                    and int(effect.get("accepted_at_epoch") or 0)
                    >= int((previous.get(service_id) or {}).get("observed_at_epoch") or 0)):
                changed_since_scan.add(service_id)
    never_read = [value for value in service_ids if value not in previous]
    rotation = sorted(
        (value for value in service_ids if value in previous and value not in changed_since_scan),
        key=lambda value: int(previous[value].get("observed_at_epoch") or 0),
    )
    due = [value for value in service_ids
           if value in changed_since_scan or value in never_read] + rotation[:4]
    due_ids = [value for value in service_ids if value in set(due)]
    claims = state_dir / "compliance-claims"

    def scan_one(service_id: str) -> tuple[dict, int, int]:
        worker_started_at_ns = time.time_ns()
        url = f"https://coconala.com/mypage/services/{service_id}"
        scan_tab = None
        observed: dict = {}
        try:
            opened = subprocess.run(
                [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
                 "--background", "open", "about:blank"],
                capture_output=True, text=True, check=False, timeout=30,
            )
            try:
                candidate = json.loads(opened.stdout)
                if opened.returncode == 0 and candidate.get("ok") is True:
                    scan_tab = candidate
            except (ValueError, KeyError):
                scan_tab = None
            if scan_tab is not None:
                observed = asyncio.run(listing_inventory._eval_json(
                    str(scan_tab["ws"]), url,
                    "JSON.stringify({url:location.href,"
                    "title:(()=>{const e=document.forms[0]?.querySelector('[name=\"data[Service][overview]\"]');"
                    "return e?e.value||'':''})(),"
                    "body:(()=>{const f=document.forms[0];"
                    "if(!f)return '';const own=['data[Service][overview]','data[Service][catchphrase]',"
                    "'data[Service][head]','data[Service][body]','data[Option][0][title]',"
                    "'data[Option][1][title]','data[Option][2][title]'];"
                    "return own.map(n=>{const e=f.querySelector('[name=\"'+n+'\"]');"
                    "return e?e.value||'':''}).join('\\n')})()})",
                ))
        except (KeyError, ValueError, OSError, RuntimeError, subprocess.TimeoutExpired):
            observed = {}
        finally:
            if isinstance(scan_tab, dict) and scan_tab.get("target_id"):
                try:
                    subprocess.run(
                        [sys.executable, str(default_tab_script), "--owner",
                         "gig-storefront-direct", "close", str(scan_tab["target_id"])],
                        capture_output=True, text=True, check=False, timeout=30,
                    )
                except subprocess.TimeoutExpired:
                    pass
        body = str(observed.get("body") or "")
        # The seller page reloads itself with a cache-busting query, so the path is what identifies
        # it. An empty read is not evidence of clean copy, only of a form that was not there.
        readable = str(observed.get("url") or "").startswith(url) and bool(body.strip())
        terms = _prohibited_copy_terms(body) if readable else []
        row = {
            "version": 1, "service_id": service_id, "observed_at_epoch": now, "source_url": url,
            "status": "scanned" if readable else "unreadable",
            "prohibited_terms": terms,
            # The loop had no record of its own listing titles, so it could publish a second
            # listing one word away from the first and see nothing wrong.
            "title_stem": str(observed.get("title") or "") if readable else None,
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            # The title belongs to the key: a listing whose row predates title recording has
            # unchanged copy, so without it that listing would never append a titled row and
            # would stay invisible to the duplicate check for good.
            "scan_key": "storefront:compliance:v1:" + hashlib.sha256(
                f"{service_id}:{hashlib.sha256(body.encode()).hexdigest()}:{readable}"
                f":{observed.get('title') or ''}".encode()
            ).hexdigest(),
        }
        return row, worker_started_at_ns, time.time_ns()

    for service_id in due_ids:
        _atomic_write(claims / f"{service_id}.json", {
            "version": 1, "resource_key": f"listing:{service_id}", "status": "claimed",
            "claimed_at_epoch": now,
        })
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(scan_one, due_ids))
    for row, worker_started_at_ns, worker_completed_at_ns in results:
        _append_key_once(ledger, "scan_key", row)
        scanned.append(row)
        _atomic_write(claims / f"{row['service_id']}.json", {
            "version": 1, "resource_key": f"listing:{row['service_id']}",
            "status": "completed", "scan_status": row["status"], "completed_at_epoch": now,
            "claimed_at_epoch": now, "worker_started_at_ns": worker_started_at_ns,
            "worker_completed_at_ns": worker_completed_at_ns,
        })
        if row["prohibited_terms"]:
            findings.append(row)
    # A listing not read this wake still has its last reading, and the catalogue view has to be
    # the whole catalogue or a duplicate pair disappears whenever one half is not due.
    read_now = {str(row["service_id"]) for row in scanned}
    catalogue = scanned + [row for service_id, row in previous.items()
                           if service_id in set(service_ids) and service_id not in read_now]
    findings += [row for row in catalogue
                 if row.get("prohibited_terms") and str(row["service_id"]) not in read_now]
    duplicates = _near_duplicate_listings(
        catalogue, capability_families or {}, state_dir=state_dir,
        evidence_dir=evidence_dir / "duplicate-judgement",
        price_by_service_id=price_by_service_id, runner=runner,
        schema=duplicate_judgement_schema, workdir=workdir, timeout_seconds=timeout_seconds,
    )
    # Rewriting one half of a pair pushes their titles apart, and the pair stopped being
    # reported the moment one listing was improved. Two listings selling the same thing do not
    # stop doing so because their wording diverged, so a pair once observed stays observed
    # until one of them is no longer live.
    pair_ledger = state_dir / "duplicate-listings.jsonl"
    for pair in duplicates:
        _append_key_once(pair_ledger, "pair_key", {
            **pair, "observed_at_epoch": now,
            "pair_key": ":".join(sorted(str(value) for value in pair["service_ids"])),
        })
    live = set(service_ids)
    if pair_ledger.exists():
        seen = {":".join(sorted(str(value) for value in pair["service_ids"])) for pair in duplicates}
        for line in pair_ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            ids = [str(value) for value in row.get("service_ids") or []]
            if (row.get("pair_key") not in seen and len(ids) == 2
                    and all(value in live for value in ids)):
                duplicates.append({**row, "still_live": True})
                seen.add(row.get("pair_key"))
    _atomic_write(evidence_dir / "compliance-scan.json",
                  {"read_now": len(scanned), "carried_over": len(catalogue) - len(scanned),
                   "violations": findings, "near_duplicates": duplicates})
    return findings, duplicates


def _collect_analytics(
    state_dir: Path, evidence_dir: Path, now: int, service_ids: list[str],
    default_tab_script: Path = DEFAULT_TAB,
) -> dict:
    import listing_inventory

    if not service_ids or len(service_ids) != len(set(service_ids)) or any(not value.isdigit() for value in service_ids):
        raise RuntimeError("official_analytics_service_ids_invalid")
    analytics_path = state_dir / "analytics.jsonl"
    previous: dict[str, dict] = {}
    if analytics_path.exists():
        for line in analytics_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError("official_analytics_ledger_invalid") from error
            if isinstance(row, dict) and str(row.get("service_id") or "").isdigit():
                previous[str(row["service_id"])] = row
    snapshots = []
    for service_id in service_ids:
        url = f"https://coconala.com/mypage/analytics/{service_id}"
        observed: dict = {}
        period = None
        for attempt in range(3):
            tab = None
            observed = {}
            try:
                opened = subprocess.run(
                    [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
                     "--background", "open", url], capture_output=True, text=True, check=False, timeout=30,
                )
                tab = json.loads(opened.stdout)
                if opened.returncode != 0 or tab.get("ok") is not True:
                    raise RuntimeError("official_analytics_tab_open_failed")
                observed = asyncio.run(listing_inventory._eval_json(
                    str(tab["ws"]), url,
                    "JSON.stringify({url:location.href,title:document.title,body:document.body?document.body.innerText.slice(0,120000):''})",
                ))
            except (KeyError, json.JSONDecodeError, RuntimeError, OSError, subprocess.SubprocessError):
                # A slow or unreachable browser call is this attempt's problem, not grounds to
                # abort the wake: fall through as a failed attempt so the retry loop below can
                # try again, or — once attempts are exhausted — land in the existing
                # "unavailable/official_readback_failed_after_retries" snapshot path instead of
                # raising out of run_once and turning one flaky call into a failed pass.
                observed = {}
            finally:
                if isinstance(tab, dict) and tab.get("target_id"):
                    try:
                        subprocess.run(
                            [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
                             "close", str(tab["target_id"])], capture_output=True, text=True,
                            check=False, timeout=30,
                        )
                    except subprocess.TimeoutExpired:
                        # A dead CDP endpoint must not replace the original read
                        # failure or prevent run_once from writing a failed receipt.
                        pass
            body = str(observed.get("body") or "")
            period = re.search(
                r"対象期間：([0-9]{4}/[0-9]{2}/[0-9]{2})\s*-\s*([0-9]{4}/[0-9]{2}/[0-9]{2})", body,
            )
            if observed.get("url") == url and period is not None and "サービス別分析" in body:
                break
            if attempt < 2:
                time.sleep(1)
        if period is None or observed.get("url") != url or "サービス別分析" not in body:
            raw_path = evidence_dir / f"official-analytics-{service_id}.json"
            _atomic_write(raw_path, observed)
            metrics = {
                metric: {"status": "unavailable", "value": None,
                         "reason": "official_readback_failed_after_retries"}
                for metric in ("impressions", "views", "purchases", "gross_jpy", "favorites")
            }
            identity = {
                "service_id": service_id, "window_start": None, "window_end": None,
                "metrics": metrics, "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            }
            snapshot = {
                "version": 1, "snapshot_key": "storefront:analytics:v1:" + hashlib.sha256(
                    json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "observed_at_epoch": now, "official": False, "service_id": service_id,
                "source_url": url, "window": {"start": None, "end": None, "complete": False},
                "metrics": metrics, "content_sha256": identity["content_sha256"],
                "evidence_path": str(raw_path),
            }
            _append_key_once(analytics_path, "snapshot_key", snapshot)
            snapshots.append(snapshot)
            continue
        raw_path = evidence_dir / f"official-analytics-{service_id}.json"
        _atomic_write(raw_path, observed)
        metrics = {
            "impressions": {"status": "unavailable", "value": None,
                            "reason": "seller_success_subscription_required"},
            "views": {"status": "known", "value": _analytics_count(body, "閲覧数", "回")},
            "purchases": {"status": "known", "value": _analytics_count(body, "販売数", "件")},
            "gross_jpy": {"status": "unavailable", "value": None,
                          "reason": "service_analytics_does_not_expose_sales_amount"},
            "favorites": {"status": "known", "value": _analytics_count(body, "お気に入り数", "回")},
        }
        identity = {
            "service_id": service_id, "window_start": period.group(1), "window_end": period.group(2),
            "metrics": metrics, "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
        snapshot = {
            "version": 1, "snapshot_key": "storefront:analytics:v1:" + hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "observed_at_epoch": now, "official": True, "service_id": service_id,
            "source_url": url, "window": {"start": period.group(1), "end": period.group(2), "complete": True},
            "metrics": metrics, "content_sha256": identity["content_sha256"], "evidence_path": str(raw_path),
        }
        _append_key_once(analytics_path, "snapshot_key", snapshot)
        snapshots.append(snapshot)
    totals = {metric: sum(int(row["metrics"][metric]["value"]) for row in snapshots
                          if type(row["metrics"][metric]["value"]) is int)
              for metric in ("views", "purchases", "favorites")}
    metric_unknown_services = {
        metric: sum(type(row["metrics"][metric]["value"]) is not int for row in snapshots)
        for metric in totals
    }
    changes = {metric: 0 for metric in totals}
    unknown_changes = 0
    for snapshot in snapshots:
        prior = previous.get(snapshot["service_id"])
        if not isinstance(prior, dict) or prior.get("window") != snapshot["window"]:
            unknown_changes += 1
            continue
        valid = True
        for metric in changes:
            before = ((prior.get("metrics") or {}).get(metric) or {}).get("value")
            after = snapshot["metrics"][metric]["value"]
            if type(before) is not int or type(after) is not int or after < before:
                valid = False
                break
            changes[metric] += after - before
        if not valid:
            unknown_changes += 1
    target = next((row for row in snapshots if row["service_id"] == TARGET_SERVICE_ID), None)
    if target is None:
        raise RuntimeError("official_analytics_target_missing")
    windows = {
        json.dumps(row.get("window"), sort_keys=True, separators=(",", ":"))
        for row in snapshots
        if isinstance(row.get("window"), dict) and row["window"].get("complete") is True
    }
    window = json.loads(next(iter(windows))) if len(windows) == 1 else None
    return {**target, "catalog_metrics": {
        "status": "current_full", "observed_at_epoch": now,
        "window": window, "coverage": {"observed": len(snapshots), "expected": len(service_ids)},
        "services_observed": len(snapshots), "totals": totals, "changes": changes,
        "metric_unknown_services": metric_unknown_services,
        "change_unknown_services": unknown_changes,
    }}


def _official_metric_windows(path: Path, service_id: str, metric: str, accepted_at: int, eligible_at: int) -> dict:
    """Movement of one official per-service metric across an experiment window.

    Coconala publishes a rolling 30-day figure, so this is a snapshot delta, never a
    window-aligned count and never the denominator of a conversion rate.
    """
    if not path.is_file():
        return {"status": "unknown", "reason": "official_analytics_missing"}
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except json.JSONDecodeError:
        return {"status": "unknown", "reason": "official_analytics_invalid"}
    samples = [row for row in rows
               if str(row.get("service_id") or "") == service_id
               and type(row.get("observed_at_epoch")) is int
               and ((row.get("metrics") or {}).get(metric) or {}).get("status") == "known"]
    baseline_rows = [row for row in samples if row["observed_at_epoch"] <= accepted_at]
    observed_rows = [row for row in samples if accepted_at < row["observed_at_epoch"] <= eligible_at]
    if not baseline_rows:
        return {"status": "unknown", "reason": f"no_official_{metric}_snapshot_at_or_before_accept"}
    if not observed_rows:
        return {"status": "unknown", "reason": f"no_official_{metric}_snapshot_inside_window"}
    baseline = baseline_rows[-1]
    observed = observed_rows[-1]
    return {
        "status": "known", "measurement": "rolling_30d_snapshot_delta",
        "baseline": int(baseline["metrics"][metric]["value"]),
        "observed": int(observed["metrics"][metric]["value"]),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _funnel_metric_windows(path: Path, service_id: str, accepted_at: int, window_days: int) -> dict:
    """Verified net receipt for one service, counted only from immutable payment events."""
    if not path.is_file():
        return {"status": "unknown", "reason": "funnel_events_missing"}
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except json.JSONDecodeError:
        return {"status": "unknown", "reason": "funnel_events_invalid"}
    payments = [row for row in rows
                if row.get("event_kind") == "payment"
                and str(row.get("service_id") or "") == service_id
                and type(row.get("observed_at_epoch")) is int
                and type(row.get("net_receipt_jpy")) in {int, float}]
    stamps = [row["observed_at_epoch"] for row in rows if type(row.get("observed_at_epoch")) is int]
    pre_start = accepted_at - window_days * 86400
    if not stamps or min(stamps) > pre_start:
        return {"status": "unknown", "reason": "funnel_history_does_not_cover_baseline"}
    eligible_at = accepted_at + window_days * 86400
    return {
        "status": "known", "measurement": "window_aligned_receipt_sum",
        "baseline": sum(float(row["net_receipt_jpy"]) for row in payments
                        if pre_start <= row["observed_at_epoch"] < accepted_at),
        "observed": sum(float(row["net_receipt_jpy"]) for row in payments
                        if accepted_at <= row["observed_at_epoch"] < eligible_at),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _measure_experiment_metric(
    metric: str, *, reply_transcripts: Path, analytics_path: Path, service_id: str,
    accepted_at: int, window_days: int, funnel_path: Path | None = None,
) -> dict:
    """Measure the experiment's metric, or say why it is unmeasurable and fall back.

    A conversion ratio between a rolling official view count and a window-aligned inquiry
    count is not a real rate, so it is reported unknown instead of being invented.
    """
    eligible_at = accepted_at + window_days * 86400
    if metric in {"views_to_inquiry", "views_to_purchase"}:
        fallback = "inquiries" if metric == "views_to_inquiry" else "purchases"
        result = _measure_experiment_metric(
            fallback, reply_transcripts=reply_transcripts, analytics_path=analytics_path,
            service_id=service_id, accepted_at=accepted_at, window_days=window_days,
        )
        return {**result, "requested_metric": metric, "measured_metric": fallback,
                "requested_metric_status": "unknown",
                "requested_metric_reason": "official_views_are_rolling_30d_not_window_aligned"}
    if metric == "inquiries":
        result = _inquiry_windows(reply_transcripts, service_id, accepted_at, window_days)
    elif metric == "purchases":
        result = _official_metric_windows(analytics_path, service_id, "purchases", accepted_at, eligible_at)
    elif metric == "net_receipt":
        result = (_funnel_metric_windows(funnel_path, service_id, accepted_at, window_days)
                  if funnel_path is not None
                  else {"status": "unknown", "reason": "funnel_events_not_supplied"})
    else:
        return {"status": "unknown", "reason": "unsupported_success_metric",
                "requested_metric": metric, "measured_metric": None}
    return {**result, "requested_metric": metric, "measured_metric": metric}


def _inquiry_windows(path: Path, service_id: str, accepted_at: int, window_days: int) -> dict:
    if not path.is_file():
        return {"status": "unknown", "reason": "reply_transcripts_missing"}
    rows = []
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except json.JSONDecodeError:
        return {"status": "unknown", "reason": "reply_transcripts_invalid"}
    timestamps = [row.get("sent_at") for row in rows
                  if type(row.get("sent_at")) is int and row["sent_at"] >= 1_700_000_000]
    pre_start = accepted_at - window_days * 86400
    if not timestamps or min(timestamps) > pre_start:
        return {"status": "unknown", "reason": "reply_history_does_not_cover_baseline"}
    first_seen: dict[str, int] = {}
    needle = f"https://coconala.com/services/{service_id}"
    for row in rows:
        talkroom_id, sent_at = str(row.get("talkroom_id") or ""), row.get("sent_at")
        evidence = json.dumps({
            "buyer_last_said": row.get("buyer_last_said"), "conversation": row.get("conversation")
        }, ensure_ascii=False)
        if talkroom_id and type(sent_at) is int and sent_at >= 1_700_000_000 and needle in evidence:
            first_seen[talkroom_id] = min(sent_at, first_seen.get(talkroom_id, sent_at))
    eligible_at = accepted_at + window_days * 86400
    return {
        "status": "known",
        "baseline": sum(pre_start <= ts < accepted_at for ts in first_seen.values()),
        "observed": sum(accepted_at <= ts < eligible_at for ts in first_seen.values()),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _next_hypothesis(scorecard_path: Path, experiment: dict) -> dict | None:
    try:
        rows = json.loads(scorecard_path.read_text(encoding="utf-8"))["priority_backlog"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return next((row for row in rows if isinstance(row, dict) and not (
        str(row.get("service_id")) == str(experiment.get("service_id"))
        and row.get("field") == experiment.get("changed_field")
    )), None)


def _scorecard_gap_candidate(
    scorecard_path: Path, versions: dict, active_services: set, open_pairs: set, field_alias: dict,
    done_pairs: set | None = None,
) -> dict | None:
    """Derive the next improvement from the scorecard's own scores.

    The committed backlog is a finite list; once it is spent the loop must still find work.
    Every dimension a listing scores below the maximum is an open gap, largest gap first, so
    improvement continues from the state of the catalogue rather than from a to-do list.
    """
    try:
        document = json.loads(scorecard_path.read_text(encoding="utf-8"))
        services = document["services"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    ranked = []
    for service in services if isinstance(services, list) else []:
        service_id = str(service.get("service_id") or "")
        scores = service.get("scores")
        if service_id not in versions or service_id in active_services or not isinstance(scores, dict):
            continue
        for dimension, score in scores.items():
            if type(score) is not int or score >= 2:
                continue
            field = field_alias.get(str(dimension).lower(), str(dimension).lower())
            if field not in GENERATED_MUTATION_FIELDS or (service_id, field) in open_pairs:
                continue
            # Scores are static config and go stale once a field is improved, so an untouched
            # gap always outranks one this loop has already published a change for.
            revisit = 1 if (service_id, field) in (done_pairs or set()) else 0
            ranked.append((revisit, score, service_id, dimension))
    if not ranked:
        return None
    _, score, service_id, dimension = sorted(ranked)[0]
    return {"service_id": service_id, "field": dimension, "before": score,
            "success_metric": "inquiries",
            "reason": f"scorecard gap: {dimension} scores {score} of 2 on the current catalogue"}


def _refresh_contract(
    rendered: dict, applied_values: set, service_id: str, field: str,
    listing_version: str | None = None,
) -> dict | None:
    """Return a rendered contract for this field unless its exact value is already published."""
    contract = rendered.get((service_id, field))
    if not isinstance(contract, dict):
        return None
    if listing_version and contract.get("precondition_listing_version_sha256") != listing_version:
        # A previously published contract is evidence for its old listing version, not permission
        # to mutate a later version.  Let the proposal path write a fresh contract instead.
        return None
    spent = (service_id, field, json.dumps(
        contract.get("proposed_value"), ensure_ascii=False, sort_keys=True)) in applied_values
    return None if spent else contract


def _prepare_next_hypothesis(
    scorecard_path: Path, effects_path: Path, outcomes_path: Path,
    contracts: list[dict], now: int, mutation_contracts: list[dict] | None = None,
    compliance_violations: list[dict] | None = None,
    offer_refresh: list[dict] | None = None,
    unread_traffic: list[str] | None = None,
) -> dict | None:
    try:
        backlog = json.loads(scorecard_path.read_text(encoding="utf-8"))["priority_backlog"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_scorecard_invalid") from error
    if not isinstance(backlog, list):
        raise RuntimeError("storefront_scorecard_invalid")
    effects = ([json.loads(line) for line in effects_path.read_text(encoding="utf-8").splitlines() if line]
               if effects_path.exists() else [])
    outcomes = ([json.loads(line) for line in outcomes_path.read_text(encoding="utf-8").splitlines() if line]
                if outcomes_path.exists() else [])
    terminal = {row.get("experiment_key") for row in outcomes if row.get("terminal") is True}
    active = [row for row in effects if row.get("status") == "accepted"
              and row.get("effect") == 1 and row.get("experiment_key") not in terminal]
    active_services = {str(row.get("service_id") or "") for row in active}
    # A field improved once is not finished forever. It becomes eligible again as soon as its
    # experiment closes, so the loop keeps improving instead of running out of committed work.
    open_pairs = {(str(row.get("service_id")), str(row.get("changed_field")).lower())
                  for row in active}
    applied_pairs = {(str(row.get("service_id")), str(row.get("changed_field")).lower())
                     for row in effects
                     if row.get("status") == "accepted" and row.get("effect") == 1}
    # The executor holds each changed listing for seven days so one experiment cannot
    # contaminate the next on the same page. With a catalogue of thirteen there is always
    # another listing to improve, so the selector honours that hold instead of re-picking a
    # service the executor is about to refuse.
    cooling = {str(row.get("service_id") or "") for row in effects
               if row.get("status") == "accepted" and row.get("effect") == 1
               and now - int(row.get("accepted_at_epoch") or 0) < 604800}
    # Only the exact proposal already published is spent. A newly written proposal for the same
    # field is a different experiment and must stay available.
    applied_values = {(str(row.get("service_id")), str(row.get("changed_field")).lower(),
                       json.dumps(row.get("after_value"), ensure_ascii=False, sort_keys=True))
                      for row in effects
                      if row.get("status") == "accepted" and row.get("effect") == 1}
    versions = {str(row["service_id"]): row["service_version_sha256"] for row in contracts}
    # A candidate whose contract the live listing has already moved past stays skipped until
    # that listing changes again, so the loop advances to the next gap instead of re-picking it.
    superseded = set()
    superseded_path = effects_path.parent / "superseded-candidates.jsonl"
    if superseded_path.exists():
        for line in superseded_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if versions.get(str(row.get("service_id"))) == row.get("listing_version"):
                superseded.add((str(row.get("service_id")), str(row.get("field")).lower()))
    field_alias = {"outcome": "title", "scope": "body"}
    rendered = {
        (str(row.get("service_id") or ""), str(row.get("changed_field") or "").lower()): row
        for row in (mutation_contracts or []) if isinstance(row, dict)
    }
    candidate = None
    mutation_contract = None
    # Before the scorecard's own ranking, prefer a listing people look at and never contact:
    # the break in this catalogue is at views to inquiry, which a ranking by emptiest field
    # cannot see. The seven-day hold still applies, because this is an experiment.
    backlog_order = {}
    for position, service_id in enumerate(unread_traffic or []):
        backlog_order[service_id] = position
    if backlog_order:
        backlog = sorted(
            backlog,
            key=lambda row: backlog_order.get(str((row or {}).get("service_id") or ""), 10_000)
            if isinstance(row, dict) else 10_000,
        )
    # A live listing that names a prohibited tool is the platform's own stated reason for taking
    # a listing down, so repairing it outranks the scorecard and is not held by the cooldown.
    for stale in offer_refresh or []:
        service_id = str(stale.get("service_id") or "")
        offer_field = str(stale.get("offer_field") or "body")
        # Not held by the cooldown: the page is advertising something this seller no longer
        # offers, which is a correction rather than another experiment on the same listing.
        if service_id in versions and (service_id, "body") not in open_pairs:
            return {
                "version": 1,
                "hypothesis_key": "storefront:hypothesis:v1:" + hashlib.sha256(
                    f"offer:{service_id}:{stale.get('offer_digest')}".encode()).hexdigest(),
                "prepared_at_epoch": now,
                "service_id": service_id,
                "service_version_sha256": versions[service_id],
                "field": str(stale.get("offer_field") or "body"),
                "portfolio_field": "outcome" if stale.get("offer_field") in {"title", "catchphrase"} else "scope",
                "before": None, "success_metric": "inquiries",
                "reason": f"listing still sells the previous offer of family {stale.get('family')}",
                "offer_digest": stale.get("offer_digest"),
                # A rendered contract whose exact value was already published is spent: replaying
                # it produces the same experiment key and is refused, which left the refresh
                # picking the same committed seed every wake and never asking for new copy.
                "executable": _refresh_contract(
                    rendered, applied_values, service_id, offer_field, versions[service_id],
                )
                is not None,
                "guard_reason": (None if _refresh_contract(
                    rendered, applied_values, service_id, offer_field, versions[service_id],
                ) is not None
                                 else "proposal_contract_required"),
                "active_experiment_key": active[0].get("experiment_key") if active else None,
                "mutation_contract_sha256": (
                    (_refresh_contract(
                        rendered, applied_values, service_id, offer_field, versions[service_id],
                    ) or {})
                    .get("contract_sha256")
                ),
            }
    for violation in compliance_violations or []:
        service_id = str(violation.get("service_id") or "")
        # An open experiment on the same field does not hold it either: a withdrawn listing
        # measures nothing, so a contaminated experiment is the cheaper loss.
        if service_id in versions:
            return {
                "version": 1,
                "hypothesis_key": "storefront:hypothesis:v1:" + hashlib.sha256(
                    f"compliance:{service_id}:{violation.get('content_sha256')}".encode()).hexdigest(),
                "prepared_at_epoch": now,
                "service_id": service_id,
                "service_version_sha256": versions[service_id],
                "field": "body", "portfolio_field": "scope",
                "before": None, "success_metric": "inquiries",
                "reason": "listing copy names a tool the platform withdraws listings for: "
                          + ",".join(violation.get("prohibited_terms") or []),
                "compliance_repair": True,
                "executable": (isinstance(rendered.get((service_id, "body")), dict)
                               and rendered[(service_id, "body")].get(
                                   "precondition_listing_version_sha256") == versions[service_id]),
                "guard_reason": (None if (isinstance(rendered.get((service_id, "body")), dict)
                                         and rendered[(service_id, "body")].get(
                                             "precondition_listing_version_sha256") == versions[service_id])
                                 else "proposal_contract_required"),
                "active_experiment_key": active[0].get("experiment_key") if active else None,
                "mutation_contract_sha256": (
                    rendered[(service_id, "body")]["contract_sha256"]
                    if (isinstance(rendered.get((service_id, "body")), dict)
                        and rendered[(service_id, "body")].get(
                            "precondition_listing_version_sha256") == versions[service_id]) else None
                ),
            }
    for row in backlog:
        if not isinstance(row, dict):
            continue
        service_id = str(row.get("service_id") or "")
        field = field_alias.get(str(row.get("field") or "").lower(), str(row.get("field") or "").lower())
        contract = rendered.get((service_id, field))
        contract_current = (isinstance(contract, dict)
                            and contract.get("precondition_listing_version_sha256") == versions.get(service_id))
        if (service_id in versions and service_id not in active_services
                and service_id not in cooling
                and (service_id, field) not in open_pairs
                and (service_id, field) not in superseded
                and (contract_current or field in GENERATED_MUTATION_FIELDS)):
            candidate = row
            # A committed contract carries one fixed proposal, so replaying it produces the same
            # experiment key and is correctly refused as a duplicate. Once it has been applied,
            # further improvement has to be newly written rather than replayed.
            spent = isinstance(contract, dict) and (
                service_id, field,
                json.dumps(contract.get("proposed_value"), ensure_ascii=False, sort_keys=True),
            ) in applied_values
            mutation_contract = None if (
                spent or (isinstance(contract, dict) and not contract_current)
            ) else contract
            break
    if candidate is None:
        candidate = _scorecard_gap_candidate(
            scorecard_path, versions, active_services | cooling, open_pairs | superseded, field_alias,
            done_pairs=applied_pairs)
        if candidate is None:
            return None
        field = field_alias.get(str(candidate.get("field") or "").lower(),
                                str(candidate.get("field") or "").lower())
        mutation_contract = rendered.get((str(candidate["service_id"]), field))
        if (isinstance(mutation_contract, dict)
                and mutation_contract.get("precondition_listing_version_sha256")
                != versions.get(str(candidate["service_id"]))):
            mutation_contract = None
    identity = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "version": 1,
        "hypothesis_key": "storefront:hypothesis:v1:" + hashlib.sha256(identity.encode()).hexdigest(),
        "prepared_at_epoch": now,
        "service_id": str(candidate["service_id"]),
        "service_version_sha256": versions[str(candidate["service_id"])],
        "field": field_alias.get(str(candidate["field"]).lower(), str(candidate["field"])),
        "portfolio_field": str(candidate["field"]),
        "before": candidate.get("before"),
        "success_metric": candidate.get("success_metric"),
        "reason": str(candidate.get("reason") or ""),
        "executable": mutation_contract is not None,
        "guard_reason": None if mutation_contract is not None else "proposal_contract_required",
        "active_experiment_key": active[0].get("experiment_key") if active else None,
        "mutation_contract_sha256": (
            mutation_contract["contract_sha256"] if mutation_contract is not None else None
        ),
    }


def _portfolio_policy(scorecard_path: Path) -> dict:
    try:
        policy = json.loads(scorecard_path.read_text(encoding="utf-8")).get("portfolio_policy")
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_portfolio_policy_invalid") from error
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise RuntimeError("storefront_portfolio_policy_invalid")
    return policy


def _measurement_feasible(analytics_path: Path, service_id: str, window_days: int, minimum_views: int) -> dict:
    """Can this experiment's metric move enough to be read at all?

    Official views are a rolling thirty-day figure, so this projects that rate onto the
    observation window. It states whether measurement is possible, never a KPI result: a
    window that cannot reach the policy's minimum exposure buys noise while locking the
    listing, so the experiment closes as unknown instead of being waited out.
    """
    if not analytics_path.is_file():
        return {"status": "unknown", "reason": "official_analytics_missing"}
    latest = None
    try:
        for line in analytics_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if (str(row.get("service_id") or "") == service_id
                    and ((row.get("metrics") or {}).get("views") or {}).get("status") == "known"
                    and type(row.get("observed_at_epoch")) is int
                    and (latest is None or row["observed_at_epoch"] > latest["observed_at_epoch"])):
                latest = row
    except json.JSONDecodeError:
        return {"status": "unknown", "reason": "official_analytics_invalid"}
    if latest is None:
        return {"status": "unknown", "reason": "no_official_views_for_service"}
    monthly = int(latest["metrics"]["views"]["value"])
    projected = int(monthly * window_days / 30)
    return {"status": "known", "official_30d_views": monthly,
            "projected_window_views": projected, "minimum_views": minimum_views,
            "feasible": projected >= minimum_views,
            "basis": "rolling_30d_view_rate_projected_onto_window"}


def _close_outcome(
    state_dir: Path, analytics: dict, reply_transcripts: Path, scorecard_path: Path, now: int,
) -> dict | None:
    experiments_path, outcomes_path = state_dir / "experiments.jsonl", state_dir / "outcomes.jsonl"
    experiments = [json.loads(line) for line in experiments_path.read_text(encoding="utf-8").splitlines() if line]
    outcomes = ([json.loads(line) for line in outcomes_path.read_text(encoding="utf-8").splitlines() if line]
                if outcomes_path.exists() else [])
    terminal = {row.get("experiment_key") for row in outcomes if row.get("terminal") is True}
    experiment = next((row for row in experiments
                       if row.get("status") == "accepted" and row.get("experiment_key") not in terminal), None)
    if experiment is None:
        return None
    window_days = experiment.get("observation_window_days")
    accepted_at = experiment.get("accepted_at_epoch")
    if type(window_days) is not int or window_days not in {7, 14} or type(accepted_at) is not int:
        raise RuntimeError("experiment_window_invalid")
    eligible_at = accepted_at + window_days * 86400
    inquiry = {"status": "not_evaluated"}
    decision, terminal_state, reason = "NO_OP", False, "measurement_window_ineligible"
    # A window that cannot reach the policy's minimum exposure locks the listing without
    # buying evidence, so close it as unknown rather than wait the calendar out.
    feasibility = _measurement_feasible(
        state_dir / "analytics.jsonl", str(experiment.get("service_id") or ""), window_days,
        int(_portfolio_policy(scorecard_path).get("minimum_views_for_measurement", 100)),
    )
    if now < eligible_at and feasibility.get("status") == "known" and not feasibility["feasible"]:
        terminal_state, reason = True, "metric_unmeasurable_insufficient_exposure"
    elif now >= eligible_at:
        inquiry = _measure_experiment_metric(
            str(experiment.get("success_metric") or ""),
            reply_transcripts=reply_transcripts, analytics_path=state_dir / "analytics.jsonl",
            service_id=str(experiment.get("service_id") or ""),
            accepted_at=accepted_at, window_days=window_days,
            funnel_path=state_dir / "funnel-events.jsonl",
        )
        if inquiry.get("status") != "known":
            terminal_state, reason = True, str(inquiry.get("reason") or "inquiry_evidence_unknown")
        elif inquiry["observed"] > inquiry["baseline"]:
            decision, terminal_state, reason = "KEEP", True, f"{inquiry['measured_metric']}_improved"
        elif inquiry["baseline"] > 0 and inquiry["observed"] < inquiry["baseline"]:
            decision, terminal_state, reason = "REVERT", True, f"{inquiry['measured_metric']}_declined"
        else:
            decision, terminal_state, reason = "NO_OP", True, f"no_measured_{inquiry['measured_metric']}_change"
    evidence = {
        "experiment_sha256": hashlib.sha256(json.dumps(
            experiment, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
        "analytics_snapshot_key": analytics["snapshot_key"],
        "inquiry_source_sha256": inquiry.get("source_sha256"),
        "measurement_feasibility": feasibility,
    }
    identity = json.dumps({
        "experiment_key": experiment["experiment_key"], "decision": decision,
        "terminal": terminal_state, "reason": reason, "evidence": evidence,
    }, sort_keys=True, separators=(",", ":"))
    receipt = {
        "version": 1, "outcome_key": "storefront:outcome:v1:" + hashlib.sha256(identity.encode()).hexdigest(),
        "observed_at_epoch": now, "experiment_key": experiment["experiment_key"],
        "service_id": experiment["service_id"], "decision": decision, "terminal": terminal_state,
        "reason": reason, "eligible_at_epoch": eligible_at, "metric": experiment.get("success_metric"),
        "measured_metric": inquiry.get("measured_metric"),
        "measurement": inquiry.get("measurement", "window_aligned_count" if inquiry.get("status") == "known" else None),
        "requested_metric_status": inquiry.get("requested_metric_status"),
        "requested_metric_reason": inquiry.get("requested_metric_reason"),
        "baseline": inquiry.get("baseline"), "observed": inquiry.get("observed"), "evidence": evidence,
        "next_hypothesis": _next_hypothesis(scorecard_path, experiment) if terminal_state else None,
        "effect": 0,
    }
    _append_key_once(outcomes_path, "outcome_key", receipt)
    return receipt


def _service_contract(source: dict, observed_at: str) -> dict:
    service_id = str(source.get("service_id") or "")
    public_text = str(source.get("public_text") or "")
    public_hash = str(source.get("public_content_sha256") or "")
    public_headings = {line.strip() for line in public_text.splitlines()}
    contract = {
        "service_id": service_id, "public_url": str(source.get("public_url") or ""),
        "title": str(source.get("title") or "").strip(),
        "state": source.get("state"), "price_jpy": source.get("price_jpy"),
        "category": str(source.get("category") or "").strip(),
        "public_content_sha256": public_hash,
    }
    if (
        not service_id.isdigit() or contract["public_url"] != f"https://coconala.com/services/{service_id}"
        or not contract["title"] or contract["state"] not in {
            "受付中", "公開中", "非公開", "下書き", SUSPENDED_LISTING_STATE,
        }
        or type(contract["price_jpy"]) is not int or contract["price_jpy"] < 0
        or not contract["category"] or not public_text
        or not {"サービス内容", "購入にあたってのお願い"} <= public_headings
        or public_hash != hashlib.sha256(public_text.encode()).hexdigest()
    ):
        raise RuntimeError("official_service_contract_invalid")
    canonical = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "version": 1, **contract, "scope_text": public_text,
        "service_version_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "observed_at": observed_at,
    }


def _append_contract_once(path: Path, contract: dict) -> bool:
    key = (contract["service_id"], contract["service_version_sha256"])
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError("offer_contract_ledger_invalid") from error
            if (row.get("service_id"), row.get("service_version_sha256")) == key:
                return False
    _append(path, contract)
    return True


def _lease(script: Path, command: str, task: str, lease: dict | None = None) -> dict:
    argv = [sys.executable, str(script), command, task]
    if lease is not None:
        argv.extend(("--token", str(lease["token"]), "--generation", str(lease["generation"])))
    completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=45)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"lease_{command}_invalid_json") from error
    if completed.returncode != 0 or result.get("ok") is not True:
        raise RuntimeError(f"lease_{command}_failed:{result.get('reason') or completed.returncode}")
    return result


def _receipt(pass_id: str, *, status: str, reason: str | None = None, **counts: object) -> dict:
    row = {
        "version": 1,
        "pass_id": pass_id,
        "observed_at_epoch": int(time.time()),
        "status": status,
        "decision": "no_op" if status == "completed" else None,
        "reason": reason,
        "actionable": 0,
        "effect": 0,
        "readback": 0,
        "duplicate": 0,
        **counts,
    }
    return row


def _collect_competitors(ws_url: str, evidence_dir: Path, own_ids: set[str]) -> dict:
    import listing_inventory

    rows = []
    unread = []
    for source_type, requested_url in COMPETITOR_SOURCES:
        requested = urlsplit(requested_url)
        requested_id = requested.path.removeprefix("/services/") if source_type == "service" else None
        if requested_id in own_ids:
            raise RuntimeError("competitor_source_is_own_service")
        # An empty page body is a transient read (the tab was still hydrating, a slow
        # network blip, ...), not evidence the competitor's page vanished, so it is
        # retried like `_read_official_catalog` retries a half-hydrated dashboard.
        # A redirect off coconala.com, or a service page redirected to a different
        # service id, is an answer about THIS one competitor among fourteen (delisted,
        # moved, or a listing that changed id) and says nothing about our own account
        # or config, so it is recorded and skipped too -- retrying would not change a
        # stable redirect, so it is not put through the empty-body retry loop.
        # `competitor_source_is_own_service` above stays fatal: an id from our own
        # catalogue showing up in the competitor list is a mistake in our own source
        # list, not weather, and skipping it would hide a real config bug.
        succeeded = False
        attempts_used = 0
        skip_reason = None
        for attempt in range(5):
            attempts_used = attempt + 1
            observed = asyncio.run(listing_inventory._eval_json(
                ws_url,
                requested_url,
                "JSON.stringify({url:location.href,title:document.title,body:document.body ? document.body.innerText.slice(0,120000) : ''})",
            ))
            final_url = str(observed.get("url") or "")
            final = urlsplit(final_url)
            body = str(observed.get("body") or "")
            if final.scheme != "https" or final.hostname not in {"coconala.com", "www.coconala.com"}:
                skip_reason = "competitor_source_not_official"
                break
            if source_type == "service" and final.path.rstrip("/") != requested.path:
                skip_reason = "competitor_service_redirected"
                break
            if body.strip():
                succeeded = True
                break
            if attempt < 4:
                time.sleep(3)
        if skip_reason is not None:
            unread.append({
                "source_type": source_type,
                "requested_url": requested_url,
                "reason": skip_reason,
                "attempts": attempts_used,
                "observed_at_epoch": int(time.time()),
            })
            continue
        if not succeeded:
            # One unreadable source among fourteen is a per-source flake and must not
            # kill the wake; it is skipped and recorded honestly. Only zero readable
            # sources (checked after the loop) is a systemic failure worth failing closed.
            unread.append({
                "source_type": source_type,
                "requested_url": requested_url,
                "reason": "competitor_source_empty",
                "attempts": attempts_used,
                "observed_at_epoch": int(time.time()),
            })
            continue
        digest = hashlib.sha256(body.encode()).hexdigest()
        path = evidence_dir / f"competitor-{source_type}-{hashlib.sha256(requested_url.encode()).hexdigest()[:12]}.json"
        row = {
            "official": True,
            "observed": True,
            "source_type": source_type,
            "requested_url": requested_url,
            "url": final_url,
            "title": str(observed.get("title") or ""),
            "body": body,
            "content_sha256": digest,
            "observed_at_epoch": int(time.time()),
        }
        _atomic_write(path, row)
        rows.append({"source_type": source_type, "url": final_url, "path": str(path), "content_sha256": digest})
    if not rows:
        # Zero readable sources is the only systemic failure worth failing closed. If
        # every source failed for the *same* non-empty reason (all fourteen redirected,
        # say), name that reason so the wake does not report "empty" when the truth is
        # "redirected" -- a uniform emptiness and a uniform redirect call for different
        # fixes. A mixed bag of reasons, or an all-empty run, keeps the plain message.
        reasons = {row["reason"] for row in unread}
        if len(reasons) == 1 and next(iter(reasons)) != "competitor_source_empty":
            raise RuntimeError(next(iter(reasons)))
        raise RuntimeError("competitor_source_empty")
    manifest = {"version": 1, "sources": rows, "unread": unread}
    _atomic_write(evidence_dir / "competitor-manifest.json", manifest)
    return manifest


def _extract_quality_signals(body: str) -> dict:
    """Per-service rating and lifetime sales as the official public page states them.

    `評価  -` means no rating exists yet, which is unknown rather than zero. Lifetime sales
    are not the 30-day analytics figure and are never mixed with it.
    """
    rating = re.search(r"評価\s+(-|[0-9]+(?:\.[0-9]+)?)", body)
    sales = re.search(r"販売実績\s*([0-9,]+)\s*件", body)
    return {
        "rating": ({"status": "known", "value": float(rating.group(1))}
                   if rating and rating.group(1) != "-"
                   else {"status": "unknown", "value": None,
                         "reason": "official_page_shows_no_rating" if rating
                         else "rating_not_found_on_official_page"}),
        "lifetime_sales": ({"status": "known", "value": int(sales.group(1).replace(",", ""))}
                           if sales else
                           {"status": "unknown", "value": None,
                            "reason": "lifetime_sales_not_found_on_official_page"}),
    }


def _own_page_readback_valid(
    observed: dict, service_id: str, expected_image_count: int | None = None,
) -> bool:
    body = str(observed.get("body") or "")
    image_ids = observed.get("service_image_ids")
    return bool(
        urlsplit(str(observed.get("url") or "")).path.rstrip("/") == f"/services/{service_id}"
        and body.strip() and isinstance(image_ids, list)
        and all(isinstance(value, str) and value for value in image_ids)
        and len(set(image_ids)) == len(image_ids)
        and (expected_image_count is None or len(image_ids) == expected_image_count)
    )


def _observe_own_page(
    ws_url: str, evidence_dir: Path, name: str = "own-candidate.json",
    service_id: str = TARGET_SERVICE_ID, expected_image_count: int | None = None,
) -> dict:
    import listing_inventory

    if not service_id.isdigit():
        raise RuntimeError("own_candidate_service_id_invalid")
    url = f"https://coconala.com/services/{service_id}"
    expression = """(async () => {
          const collapsedBodies = [...document.querySelectorAll(
            'button.c-contentsCollapse_readMoreButton'
          )];
          collapsedBodies.forEach(control => control.click());
          const closed = [...document.querySelectorAll(
            'a[aria-controls^="serviceContentsFaqAnswer"][aria-expanded="false"]'
          )];
          closed.forEach(control => control.click());
          if (collapsedBodies.length || closed.length) await new Promise(resolve => setTimeout(resolve, 500));
          const expectedImageCount = __EXPECTED_IMAGE_COUNT__;
          let serviceImageIds = [];
          for (let attempt = 0; attempt < 20; attempt += 1) {
            serviceImageIds = [...new Set([...document.querySelectorAll('.c-contentsImagesProduction img')]
              .map(image => (image.currentSrc || image.src || '').match(/service_images\\/original\\/([^/?]+)/)?.[1])
              .filter(Boolean))];
            if (expectedImageCount < 0 || serviceImageIds.length === expectedImageCount) break;
            await new Promise(resolve => setTimeout(resolve, 250));
          }
          return JSON.stringify({url:location.href,title:document.title,
            body:document.body ? document.body.innerText.slice(0,120000) : '',service_image_ids:serviceImageIds});
        })()""".replace(
            "__EXPECTED_IMAGE_COUNT__",
            str(expected_image_count if expected_image_count is not None else -1),
        )
    observed = {}
    # A public page that has not finished rendering is weather, not a verdict on the listing,
    # so it gets the same settling window every other transient reader in this loop uses --
    # see `references/transient-vs-fatal.md` in the loop-engineering skill.
    for attempt in range(5):
        observed = asyncio.run(listing_inventory._eval_json(ws_url, url, expression))
        body = str(observed.get("body") or "")
        image_ids = observed.get("service_image_ids")
        valid = _own_page_readback_valid(observed, service_id, expected_image_count)
        if valid:
            break
        if attempt < 4:
            time.sleep(3)
    else:
        raise RuntimeError("own_candidate_readback_invalid")
    row = {
        "official": True,
        "observed": True,
        "service_id": service_id,
        "url": str(observed["url"]),
        "title": str(observed.get("title") or ""),
        "body": body,
        "service_image_ids": image_ids,
        "service_image_count": len(image_ids),
        "quality_signals": _extract_quality_signals(body),
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "listing_version_sha256": hashlib.sha256(json.dumps(
            {"body": body, "service_image_ids": image_ids}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest(),
        "observed_at_epoch": int(time.time()),
    }
    _atomic_write(evidence_dir / name, row)
    return row


def _judgement_prompt(
    own_page: dict, manifest: dict, capability_paths: set[str], family_market: dict | None = None,
) -> str:
    competitors = []
    for reference in manifest["sources"]:
        row = json.loads(Path(reference["path"]).read_text(encoding="utf-8"))
        competitors.append({"path": reference["path"], "url": row["url"], "body": row["body"][:12000]})
    capabilities = []
    for raw_path in sorted(capability_paths):
        path = Path(raw_path)
        capabilities.append({"path": raw_path, "content": path.read_text(encoding="utf-8")[:8000]})
    context = {
        "own": {"service_id": TARGET_SERVICE_ID, "url": own_page["url"], "body": own_page["body"][:24000]},
        "competitors": competitors,
        "capability_evidence": capabilities,
    }
    if family_market:
        context["family_market"] = family_market
    return """Judge one Coconala Storefront experiment from the JSON evidence below.
Return only the strict schema object. Model judgement may choose change or no_op; code owns every
mechanical guard and no seller effect occurs in this turn. The only supported change is the
configured target service's FAQ field. Its exact current sentinel is FAQ_ABSENT only when the own public body has no
よくある質問 section. Propose one original Japanese FAQ grounded in this account's capability and
generalized competitor structure. Do not copy competitor prose, images, reviews, sales, or claims.
For change, cite exact supplied paths, choose one metric and 7 or 14 days, and return
experiment_key=null for parent derivation. For no_op, all change fields/metric/window/key are null
and no_op_reason is nonempty.\nCONTEXT_JSON=""" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def _invoke_judge(
    *, runner: Path, schema: Path, workdir: Path, evidence_dir: Path,
    own_page: dict, manifest: dict, capability_paths: set[str], timeout_seconds: int,
    family_market: dict | None = None,
) -> dict:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    child_env = os.environ.copy()
    completed = subprocess.run(
        [sys.executable, str(runner), "--task-class", "composition-agent", "--prompt-stdin",
         "--schema", str(schema), "--evidence-dir", str(evidence_dir),
         "--task-label", "gig-storefront-judge", "--loop", "gig", "--workdir", str(workdir),
         "--timeout-seconds", str(timeout_seconds)],
        input=_judgement_prompt(own_page, manifest, capability_paths, family_market=family_market),
        text=True,
        capture_output=True,
        env=child_env,
        timeout=timeout_seconds + 30,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise RuntimeError(f"storefront_judge_failed:{completed.returncode}:{detail}")
    try:
        summary_path = evidence_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        result_path = Path(str(summary["result_path"])).resolve()
        result_path.relative_to(evidence_dir.resolve())
        if summary.get("status") != "success" or min(summary_path.stat().st_mtime, result_path.stat().st_mtime) < started:
            raise ValueError("stale_or_unsuccessful")
        value = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_judge_evidence_invalid") from error
    if not isinstance(value, dict):
        raise RuntimeError("storefront_judge_result_not_object")
    return value


def _proposal_prompt(
    hypothesis: dict, source: dict, seller_snapshot: dict, family_name: str, family: dict,
    manifest: dict, capability_paths: set[str], prior_rejections: list[dict] | None = None,
    family_market: dict | None = None,
) -> tuple[str, set[str]]:
    competitor_rows = []
    allowed_refs = {
        f"official:seller-form:{source['service_id']}",
        f"official:offer-contract:{source['service_id']}:{source['service_version_sha256']}",
        f"owned:capability-family:{family_name}",
    }
    if family_market and family_market.get("evidence_path"):
        allowed_refs.add(str(family_market["evidence_path"]))
    for reference in manifest.get("sources", []):
        path = str(reference.get("path") or "")
        try:
            row = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("storefront_proposal_competitor_evidence_invalid") from error
        allowed_refs.add(path)
        competitor_rows.append({"evidence_ref": path, "url": row.get("url"),
                                "body": str(row.get("body") or "")[:8000]})
    capabilities = []
    for raw_path in sorted(capability_paths):
        path = Path(raw_path)
        if not path.is_file():
            continue
        allowed_refs.add(raw_path)
        capabilities.append({"evidence_ref": raw_path,
                             "content": path.read_text(encoding="utf-8")[:6000]})
    context = {
        "gap": hypothesis,
        "official_offer": source,
        "seller_form": seller_snapshot,
        "capability_family": {"name": family_name, "contract": family},
        "competitors": competitor_rows,
        "owned_capability_evidence": capabilities,
        "allowed_evidence_refs": sorted(allowed_refs),
    }
    if prior_rejections:
        context["prior_rejections"] = prior_rejections
    if family_market:
        context["family_market"] = family_market
    prohibited_terms_prose = (
        "Buyer-visible copy (title, catchphrase, body, image copy, FAQ, package option title) must "
        "never contain any of these platform-prohibited terms, in exactly the spelling listed: "
        + "、".join(PROHIBITED_COPY_TERMS)
        + ". Coconala has withdrawn a live listing twice for naming one of these because a file "
        "service the platform cannot see reads as an off-platform contact channel."
    )
    prior_rejections_prose = (
        (
            " prior_rejections in CONTEXT_JSON lists this exact gap's own previous rejected "
            "attempts, oldest first. Each `rejection` names the guard that refused it and the "
            "`proposed_value` that triggered it. Do not repeat any of them: produce a proposal "
            "that is genuinely different and does not trigger the same guard again."
        ) if prior_rejections else ""
    )
    prompt = prohibited_terms_prose + prior_rejections_prose + """
Create exactly one Coconala Storefront improvement proposal from CONTEXT_JSON.
Return only the strict schema object. The selected service_id, changed_field and success_metric must
exactly equal gap.service_id, gap.field and gap.success_metric. Use only claims supported by the
owned capability family/offer. Never copy a competitor's wording, images, review text, claims or
results. The observed price, rating and review-count distribution in family_market is public fact
about this market and MUST be used: say what the family's median price is and where this listing
sits against it, and when proposing a price, justify it against that distribution rather than
against the current price. evidence entries must be exact values
from allowed_evidence_refs and must include the official offer ref and owned capability-family ref.
gap.executable=false with guard_reason=proposal_contract_required means this proposal must create the
missing contract; it is not a no-op reason and mutation_contract_sha256 is intentionally absent.
For title, return only the seller-form title stem (Coconala appends ます). For catchphrase,
return the single line shown under the title, 15 to 30 characters, carrying what the title
cannot: who it is for, what arrives, or the condition of delivery. It must not restate the
title in other words; a search result showing the same sentence twice wastes its second line. For body, return a complete
Japanese replacement with outcome, inclusions, exclusions, required inputs and support boundary.
For image, proposed_value must be exactly three non-empty lines: a short headline, a supporting line,
then two or three short badges separated by `｜`. Use only supported offer/capability claims; do not put
price, delivery speed, review count, sales count, guarantees or competitor wording in the image.
For package, return one precise option title and proposed_price_jpy from the official option-price
select values visible in seller_form; for FAQ use `Q. ...\nA. ...`; for price return an exact seller
base-price select option value visible in seller_form. Change one field only. Choose change only when the
proposal is fully supported; otherwise choose no_op with all nullable change fields null and a concrete
no_op_reason. observation_window_days is 7 or 14. Do not claim that the proposal itself caused KPI
improvement.\nCONTEXT_JSON=""" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    return prompt, allowed_refs


def _invoke_proposal(
    *, runner: Path, schema: Path, workdir: Path, evidence_dir: Path, hypothesis: dict,
    source: dict, seller_snapshot: dict, family_name: str, family: dict, manifest: dict,
    capability_paths: set[str], timeout_seconds: int, prior_rejections: list[dict] | None = None,
    family_market: dict | None = None,
) -> tuple[dict, dict, set[str]]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    prompt, allowed_refs = _proposal_prompt(
        hypothesis, source, seller_snapshot, family_name, family, manifest, capability_paths,
        prior_rejections=prior_rejections, family_market=family_market,
    )
    started = time.time()
    completed = subprocess.run(
        [sys.executable, str(runner), "--task-class", "storefront-proposal-agent", "--prompt-stdin",
         "--schema", str(schema), "--evidence-dir", str(evidence_dir),
         "--task-label", "gig-storefront-proposal", "--loop", "gig-storefront",
         "--workdir", str(workdir), "--timeout-seconds", str(timeout_seconds)],
        input=prompt, text=True, capture_output=True, env=os.environ.copy(),
        timeout=timeout_seconds + 30, check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise RuntimeError(f"storefront_proposal_failed:{completed.returncode}:{detail}")
    try:
        summary_path = evidence_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        result_path = Path(str(summary["result_path"])).resolve()
        result_path.relative_to(evidence_dir.resolve())
        if (summary.get("status") != "success"
                or summary.get("task_class") != "storefront-proposal-agent"
                or summary.get("selected_provider") != "codex"
                or summary.get("selected_model") != "gpt-5.6-terra"
                or summary.get("selected_effort") != "medium"
                or min(summary_path.stat().st_mtime, result_path.stat().st_mtime) < started):
            raise ValueError("stale_or_wrong_route")
        proposal = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_proposal_evidence_invalid") from error
    route = {
        "task_class": summary["task_class"], "route": summary.get("route"),
        "provider": summary["selected_provider"], "model": summary["selected_model"],
        "effort": summary["selected_effort"], "summary_path": str(summary_path),
        "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
    }
    return proposal, route, allowed_refs


_HERO_FONT_PATH: Path | None = None


def _hero_font_path() -> Path:
    """Resolve the hero image's font once, retrying a machine too busy to answer.

    `fc-match` names a file that does not change between wakes, but it was run on every
    render with a ten-second timeout, and under load that timeout ended the whole wake --
    after the proposal had been generated and sealed, so a font lookup threw away the most
    expensive work the loop does. The answer is cached for the life of the process and the
    lookup is retried like every other transient here.
    """
    global _HERO_FONT_PATH
    if _HERO_FONT_PATH is not None:
        return _HERO_FONT_PATH
    detail = "storefront_generated_image_font_missing"
    for attempt in range(5):
        try:
            # The font this renders with is at `/System/Library/Fonts/ヒラギノ角ゴシック W4.ttc`.
            # `text=True` decodes with the locale's encoding, and launchd sets no LANG, so
            # under the scheduler that name came back mangled and named a file that does not
            # exist -- while the identical command in a shell answered correctly in 0.2s.
            # The bytes fc-match writes are UTF-8; say so rather than letting the environment
            # decide.
            found = subprocess.run(
                ["fc-match", "-f", "%{file}", "Hiragino Sans"], capture_output=True,
                encoding="utf-8", errors="strict", check=False, timeout=10,
            )
        except subprocess.SubprocessError:
            if attempt < 4:
                time.sleep(3)
            continue
        candidate = Path(found.stdout.strip())
        if found.returncode == 0 and candidate.is_file():
            _HERO_FONT_PATH = candidate
            return candidate
        if attempt < 4:
            time.sleep(3)
    raise RuntimeError(detail)


def _render_generated_image_asset(proposed: str, service_id: str, evidence_dir: Path) -> dict:
    from PIL import Image, ImageDraw, ImageFont

    lines = [line.strip() for line in proposed.splitlines() if line.strip()]
    if len(lines) != 3 or not (2 <= len(lines[0]) <= 28 and 2 <= len(lines[1]) <= 48):
        raise RuntimeError("storefront_generated_image_copy_invalid")
    badges = [value.strip() for value in lines[2].split("｜") if value.strip()]
    if len(badges) not in {2, 3} or any(len(value) > 24 for value in badges):
        raise RuntimeError("storefront_generated_image_badges_invalid")
    font_path = _hero_font_path()
    image = Image.new("RGB", (1220, 1016), "#111c50")
    draw = ImageDraw.Draw(image)
    for y in range(1016):
        ratio = y / 1015
        draw.line((0, y, 1220, y), fill=(17 + int(6 * ratio), 28 - int(16 * ratio), 80 - int(38 * ratio)))
    draw.rounded_rectangle((70, 70, 1150, 946), radius=42, outline="#6f83ff", width=3)

    def fitted(text: str, maximum: int, start: int) -> ImageFont.FreeTypeFont:
        size = start
        while size >= 28:
            font = ImageFont.truetype(str(font_path), size)
            if draw.textbbox((0, 0), text, font=font)[2] <= maximum:
                return font
            size -= 2
        raise RuntimeError("storefront_generated_image_text_too_wide")

    headline_font = fitted(lines[0], 1000, 82)
    support_font = fitted(lines[1], 1000, 42)
    draw.text((110, 260), lines[0], font=headline_font, fill="white")
    draw.text((112, 400), lines[1], font=support_font, fill="#dce3ff")
    badge_size = 27
    while badge_size >= 18:
        badge_font = ImageFont.truetype(str(font_path), badge_size)
        badge_widths = [draw.textbbox((0, 0), badge, font=badge_font)[2] + 70 for badge in badges]
        if sum(badge_widths) + 24 * (len(badges) - 1) <= 1020:
            break
        badge_size -= 1
    else:
        raise RuntimeError("storefront_generated_image_badges_too_wide")
    x = 110
    for index, (badge, width) in enumerate(zip(badges, badge_widths)):
        draw.rounded_rectangle((x, 600, x + width, 670), radius=35,
                               fill=("#ef4f55", "#35b777", "#7657db")[index])
        draw.text((x + 35, 618), badge, font=badge_font, fill="white")
        x += width + 24
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"generated-{service_id}-hero.png"
    image.save(path, format="PNG", optimize=False)
    data = path.read_bytes()
    return {"asset_sha256": hashlib.sha256(data).hexdigest(), "asset_path": str(path.resolve())}


def _paid_demand_price_floor(demand: dict) -> int | None:
    prices = sorted(
        int(row["display_price_jpy"])
        for row in demand.get("comparables") or []
        if isinstance(row, dict)
        and type(row.get("display_price_jpy")) is int
        and row["display_price_jpy"] > 0
        and (int(row.get("review_count") or 0) > 0 or int(row.get("sales_count") or 0) > 0)
    )
    return prices[len(prices) // 2] if prices else None


def _create_proposal_prompt(
    source: dict, family_name: str, family: dict, demand: dict,
    capability_paths: set[str], catalog_titles: list[str], listing_catalog_entry: dict | None = None,
    prior_rejections: list[dict] | None = None,
) -> tuple[str, set[str]]:
    offer_ref = f"official:offer-contract:{source['service_id']}:{source['service_version_sha256']}"
    family_ref = f"owned:capability-family:{family_name}"
    demand_ref = str(demand["evidence_path"])
    allowed_refs = {offer_ref, family_ref, demand_ref}
    capabilities = []
    for raw_path in sorted(capability_paths):
        path = Path(raw_path)
        if path.is_file():
            allowed_refs.add(raw_path)
            capabilities.append({"evidence_ref": raw_path,
                                 "content": path.read_text(encoding="utf-8")[:6000]})
    context = {
        "source_offer": source,
        "capability_family": {"name": family_name, "contract": family},
        "demand": demand,
        "owned_capability_evidence": capabilities,
        "current_catalog_titles": catalog_titles,
        "allowed_evidence_refs": sorted(allowed_refs),
        "paid_demand_price_floor_jpy": _paid_demand_price_floor(demand),
        "owner_listing_catalog_entry": listing_catalog_entry,
    }
    if prior_rejections:
        context["prior_rejections"] = prior_rejections
    prohibited_terms_prose = (
        "Buyer-visible copy (title_stem, catchphrase, head, body, paid_option_title, image_copy) "
        "must never contain any of these platform-prohibited terms, in exactly the spelling "
        "listed: " + "、".join(PROHIBITED_COPY_TERMS)
        + ". Coconala has withdrawn a live listing twice for naming one of these because a file "
        "service the platform cannot see reads as an off-platform contact channel."
    )
    prior_rejections_prose = (
        (
            " prior_rejections in CONTEXT_JSON lists this exact family's own previous rejected "
            "attempts, oldest first. Each `rejection` names the guard that refused it and the "
            "`proposed_value` that triggered it. Do not repeat any of them: produce a proposal "
            "that is genuinely different and does not trigger the same guard again."
        ) if prior_rejections else ""
    )
    catalog_instruction = (
        "owner_listing_catalog_entry is the owner's pre-decided spec for this capability_family "
        "(scope tiers, prices, deliverables, required_inputs, FAQ). When present, build the proposal "
        "from it: head/body/inclusions/exclusions/required_inputs must match its deliverables and "
        "required_inputs, and display_price_jpy/paid_option_price_jpy must land within its tiers' "
        "price range rather than inventing unrelated scope or price. "
    ) if listing_catalog_entry else ""
    prompt = prohibited_terms_prose + prior_rejections_prose + " " + catalog_instruction + f"""Create one distinct Coconala service proposal from CONTEXT_JSON and return only the
strict schema object. The source_service_id must equal source_offer.service_id; that existing service
supplies the seller-form adapter, not proof of the new capability. The new service must sell a bounded
buyer-visible outcome supported by the owned capability family; it must not
duplicate or merely rephrase any current_catalog_titles. Use the demand page only as demand evidence,
never copy seller wording, reviews, sales, guarantees or unsupported claims. Include exact evidence
refs for the official offer, owned family and demand evidence. The title_stem excludes the final
Japanese `ます`, so it must end in the continuative (i-form) of a Japanese verb -- its last character
must be one of these characters, which is exactly what Coconala can attach `ます` to:
{TITLE_STEM_CONTINUATIVE_ENDINGS} -- e.g. `...を開発し`,
`...を執筆し`, `...を実装し`. It must never end in a bare noun such as `...アプリ` or `...システム`
(that would render as the ungrammatical `...アプリます`); if the offer is naturally noun-shaped,
add a closing verb like `...アプリを開発し` or `...システムを構築し` instead. A する-verb written
as its bare noun is the same mistake once more: `...応答を自動化` is a noun, and Coconala would
render `...自動化ます`. Write the continuative -- `...応答を自動化し`, `...業務を効率化し`,
`...環境を構築し`, `...機能を実装し`. A stem that ends in a
particle is the same mistake wearing different clothes: it is a noun phrase whose verb is
missing. `...FAQ自動応答Botを` would render as `...FAQ自動応答Botをます`. The particles
`を`, `の`, `が`, `は`, `と` are refused outright. Do not read the character list above as
permission for `に`, `で` or `へ` either: those appear in it only because they are the
continuative of verbs like 死に and 出で, so they are correct only when they genuinely are your
verb's continuative, never when they are a particle. When the stem you want ends in a particle,
the verb is the word you left out, so append it: `...Botを構築し`, `...応答を自動化し`,
`...設定を代行し`. Read your own final word before returning and confirm it is a verb. head must state
outcome, exact inclusions, exclusions, required inputs and support
boundary. Write head and body as buyer-facing Japanese prose: never emit a schema field name or an
English label such as `outcome:`, and never prefix a sentence with a bare label like `含むもの:`. body must state purchase inputs and unsupported work. image_copy is exactly three non-empty
lines: headline, supporting line, and two or three short badges separated by `｜`; do not include price,
speed, sales, reviews or guarantees. Decide delivery_kind from the paid market evidence and actual
owned capability. Do not downgrade a market that pays for building or implementation into advice,
requirements, or a plan merely because that is easier to deliver. In that case the base offer includes
one bounded working implementation, verification, and handover; recurring support begins only after
acceptance. Conversely, a market that pays for a memo or assessment must not be mislabeled as an
implementation. Canonical examples: (1) demand `AI agent development` plus a capability that builds
automations -> delivery_kind `implementation`, working system in the base deliverable, optional
post-acceptance maintenance; (2) demand `interview analysis` plus a synthesis capability ->
delivery_kind `analysis`, decision memo in the base deliverable, no invented software build. Price the
actual base deliverable at or above paid_demand_price_floor_jpy when present; do not default to the
cheapest option. When decision is `create`, paid_option_title and paid_option_price_jpy must both be
non-null -- only `no_op` may leave every commercial field null. Derive the paid option from evidence,
never invent it: a scope increment the base deliverable excludes (an owner_listing_catalog_entry tier
above the one you priced from, one more deliverable from its list, or the recurring/monthly support its
FAQ or competitor evidence names), priced below display_price_jpy. If no such increment is evidenced,
price it as a bounded rush-delivery reduction of delivery_days. Capability artifacts prove that the workforce can execute; they do not prove buyers
want that artifact's niche. The product buyer job must directly match the official demand query and
comparables. Example: generic `AI agent business automation` demand plus a past computer-vision build
may prove implementation ability, but it does not justify selling that past vision niche; propose a
bounded business workflow automation instead. If uncertainty says the proposed product's own demand is
unverified, revise it to the evidenced buyer job or choose no_op. Bound a broad evidenced market by
workflow count, inputs, outputs, tools, and approval boundaries; do not invent an industry or use-case
niche that the demand evidence did not measure. Example: generic business-automation demand -> implement
one buyer-supplied repetitive workflow, not an arbitrarily selected email, board-game, or other niche.
Choose create only
when the proposal is clearly distinct and supported. Otherwise choose no_op, set every nullable
commercial field and metric/window to null, and provide no_op_reason. Do not claim that creation itself
caused KPI improvement.\nCONTEXT_JSON=""" + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    return prompt, allowed_refs


def _create_blueprint_from_cluster(committed: dict, cluster: dict, category_row: dict) -> dict:
    """Turn a self-derived demand cluster into the blueprint CREATE builds a listing from.

    Only the parts that belong to the market change: the demand evidence and the official
    category. Delivery policy, ladder and subscription terms stay as the owner committed
    them, because those describe how the work is done rather than which market it serves.
    """
    master = (category_row or {}).get("master_category") or {}
    subs = (category_row or {}).get("sub_options") or []
    types = (category_row or {}).get("type_options") or []
    if not str(master.get("value") or "").isdigit():
        raise RuntimeError("storefront_cluster_category_unbound")
    if (cluster.get("status") != "known" or int(cluster.get("score") or 0) <= 0
            or not str(cluster.get("query") or "").strip()
            or not str(cluster.get("evidence_path") or "").strip()):
        raise RuntimeError("storefront_cluster_demand_unproven")
    return {
        **committed,
        "capability_family": cluster.get("capability_family"),
        "demand_evidence": {
            "search_url": cluster.get("search_url"),
            "visible_result_count": cluster.get("visible_result_count"),
            "comparables": cluster.get("comparables") or [],
        },
        "demand_evidence_path": str(cluster["evidence_path"]),
        "category": {"master": master, "sub": None, "type": None},
        "category_options": {"sub": subs, "type": types},
        "cluster_key": cluster.get("cluster_key"),
    }


def _invoke_create_proposal(
    *, runner: Path, schema: Path, workdir: Path, evidence_dir: Path, source: dict,
    family_name: str, family: dict, demand: dict, capability_paths: set[str],
    catalog_titles: list[str], timeout_seconds: int, listing_catalog_entry: dict | None = None,
    prior_rejections: list[dict] | None = None,
) -> tuple[dict, dict, set[str]]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    prompt, allowed_refs = _create_proposal_prompt(
        source, family_name, family, demand, capability_paths, catalog_titles,
        listing_catalog_entry=listing_catalog_entry, prior_rejections=prior_rejections,
    )
    started = time.time()
    completed = subprocess.run(
        [sys.executable, str(runner), "--task-class", "storefront-proposal-agent", "--prompt-stdin",
         "--schema", str(schema), "--evidence-dir", str(evidence_dir),
         "--task-label", "gig-storefront-create", "--loop", "gig-storefront",
         "--workdir", str(workdir), "--timeout-seconds", str(timeout_seconds)],
        input=prompt, text=True, capture_output=True, env=os.environ.copy(),
        timeout=timeout_seconds + 30, check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise RuntimeError(f"storefront_create_proposal_failed:{completed.returncode}:{detail}")
    try:
        summary_path = evidence_dir / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        result_path = Path(str(summary["result_path"])).resolve()
        result_path.relative_to(evidence_dir.resolve())
        if (summary.get("status") != "success"
                or summary.get("task_class") != "storefront-proposal-agent"
                or summary.get("selected_provider") != "codex"
                or summary.get("selected_model") != "gpt-5.6-terra"
                or summary.get("selected_effort") != "medium"
                or min(summary_path.stat().st_mtime, result_path.stat().st_mtime) < started):
            raise ValueError("stale_or_wrong_route")
        proposal = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("storefront_create_proposal_evidence_invalid") from error
    route = {"task_class": summary["task_class"], "route": summary.get("route"),
             "provider": summary["selected_provider"], "model": summary["selected_model"],
             "effort": summary["selected_effort"], "summary_path": str(summary_path)}
    return proposal, route, allowed_refs


def _create_rejected_proposed_value(proposal: dict) -> dict:
    """The buyer-visible fields the CREATE guards check, truncated for the rejection ledger."""
    fields = ("title_stem", "catchphrase", "head", "body", "paid_option_title", "image_copy")
    return {field: str(proposal[field])[:400] for field in fields if proposal.get(field) is not None}


def _seal_create_contract(
    proposal: dict, *, source: dict, family_name: str, family: dict, allowed_refs: set[str],
    blueprint: dict, seller_snapshot: dict, draft_service_id: str, evidence_dir: Path,
) -> dict | None:
    nullable = ("source_service_id", "title_stem", "catchphrase", "head", "body",
                "display_price_jpy", "delivery_days", "paid_option_title",
                "paid_option_price_jpy", "image_copy", "success_metric",
                "observation_window_days", "delivery_kind", "recurring_support_included")
    if proposal.get("decision") == "no_op":
        if any(proposal.get(key) is not None for key in nullable) or not str(
                proposal.get("no_op_reason") or "").strip():
            raise RuntimeError("storefront_create_noop_invalid")
        return None
    evidence = proposal.get("evidence")
    required = {
        f"official:offer-contract:{source['service_id']}:{source['service_version_sha256']}",
        f"owned:capability-family:{family_name}", str(blueprint["demand_evidence_path"]),
    }
    if (proposal.get("decision") != "create"
            or proposal.get("source_service_id") != source["service_id"]
            or proposal.get("success_metric") not in MEASURABLE_SUCCESS_METRICS
            or proposal.get("delivery_kind") not in {"implementation", "content", "analysis", "design"}
            or type(proposal.get("recurring_support_included")) is not bool
            or proposal.get("observation_window_days") not in {7, 14}
            or proposal.get("no_op_reason") is not None
            or not isinstance(evidence, list) or not required <= set(evidence)
            or not set(evidence) <= allowed_refs or not draft_service_id.isdigit()):
        raise RuntimeError("storefront_create_identity_invalid")
    title_stem = str(proposal.get("title_stem") or "").strip()
    catchphrase = str(proposal.get("catchphrase") or "").strip()
    head = str(proposal.get("head") or "").strip()
    body = str(proposal.get("body") or "").strip()
    option_title = str(proposal.get("paid_option_title") or "").strip()
    image_copy = str(proposal.get("image_copy") or "").strip()
    if (not title_stem or len(title_stem) > 23 or not 15 <= len(catchphrase) <= 30
            or not head or len(head) > 1000 or not body or len(body) > 500
            or not option_title or len(option_title) > 60
            or len([line for line in image_copy.splitlines() if line.strip()]) != 3
            or "｜" not in image_copy.splitlines()[-1]):
        raise RuntimeError("storefront_create_content_invalid")
    # Coconala renders the title as `{title_stem}ます`, so the stem must end in a verb continuative
    # form. A stem ending in a particle produced `…SEO構成からます` in a sealed contract.
    if title_stem[-1] not in TITLE_STEM_CONTINUATIVE_ENDINGS:
        raise RuntimeError("storefront_create_title_stem_not_continuative")
    # Buyer-visible copy must not leak the schema: an English `outcome:` prefix reached a live listing.
    if any(re.match(r"^[A-Za-z_]{3,}\s*[:：]", line.strip())
           for line in f"{head}\n{body}".splitlines()):
        raise RuntimeError("storefront_create_copy_leaks_schema_labels")
    prohibited = _prohibited_copy_terms(title_stem, catchphrase, head, body, option_title, image_copy)
    if prohibited:
        raise RuntimeError("storefront_copy_names_prohibited_tool:" + ",".join(prohibited))
    select_options = seller_snapshot.get("select_options") or {}
    display_price = proposal.get("display_price_jpy")
    price_option = next((row for row in select_options.get("data[Service][price]", [])
                         if str(row.get("label") or "").replace(",", "") == f"{display_price}円"), None)
    option_price = proposal.get("paid_option_price_jpy")
    option_price_row = next((row for row in select_options.get("data[Option][0][price]", [])
                             if str(row.get("label") or "").replace(",", "") == f"{option_price}円"), None)
    if type(display_price) is not int or price_option is None or type(option_price) is not int or option_price_row is None:
        raise RuntimeError("storefront_create_price_invalid")
    price_floor = _paid_demand_price_floor(blueprint.get("demand_evidence") or {})
    if price_floor is not None and display_price < price_floor:
        raise RuntimeError("storefront_create_below_paid_demand_price_floor")
    asset = _render_generated_image_asset(image_copy, draft_service_id, evidence_dir)
    unsigned = {
        "version": 1, "platform": "coconala",
        "candidate_key": f"storefront:create:v1:{hashlib.sha256(title_stem.encode()).hexdigest()}",
        "draft_service_id": draft_service_id,
        "draft_url": f"https://coconala.com/mypage/services/{draft_service_id}",
        "expected_public_url": f"https://coconala.com/services/{draft_service_id}",
        "origin": "storefront", "demand_evidence": blueprint["demand_evidence"],
        "capability_evidence": {"family": family_name, "source_service_id": source["service_id"],
                                "delivery_kind": proposal["delivery_kind"],
                                "recurring_support_included": proposal["recurring_support_included"]},
        "hero_image_contract": asset["asset_path"], "category": blueprint["category"],
        "public_fields": {"overview_input": title_stem, "expected_title": f"{title_stem}ます",
                          "catchphrase": catchphrase, "head": head,
                          "price_option_value": str(price_option["value"]),
                          "display_price_jpy": display_price,
                          "delivery_days": int(proposal["delivery_days"]), "order_limit": 1,
                          "body": body, "accept_estimates": True, "estimate_required": False},
        "category_specific": blueprint["category_specific"],
        # blueprint["subscription"] is the owner's one committed bootstrap contract (a
        # recurring writing service) and is always {enabled: True, discount_ratio: "5"}. Every
        # subsequent create blindly inherited that regardless of this proposal's own delivery
        # kind: a bounded one-off implementation like mobile_app_dev got a subscription
        # discount toggled on a category that does not actually offer one, so Coconala accepted
        # the draft save (client-side state) but rejected publish with "最低サービス価格を下回る
        # 割引設定はできません" -- the persisted can_subscribe flag reverted to false while the
        # stale discount_ratio stayed set, an inconsistent pair the server refuses. Only carry a
        # subscription when the proposal itself asked for recurring support.
        "subscription": (blueprint["subscription"] if proposal["recurring_support_included"]
                         else {"enabled": False, "discount_ratio": "0"}),
        "paid_options": [{"title": option_title, "price_jpy": option_price, "opened": "1"}],
        "publication_gate": blueprint["publication_gate"], "proposal_evidence": evidence,
        "success_metric": proposal["success_metric"],
        "observation_window_days": proposal["observation_window_days"],
    }
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**unsigned, "contract_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "hero_image": {"version": 1, "service_id": draft_service_id, "field": "image",
                           "mime_type": "image/png", "width": 1220, "height": 1016,
                           "claims": image_copy.splitlines(), **asset}}


def _seal_bootstrap_contract(
    proposal: dict, *, selection: dict, demand: dict, category_record: dict,
    official_form: dict, draft_service_id: str, evidence_dir: Path,
) -> dict:
    title = str(proposal.get("title_stem") or "").strip()
    catchphrase = str(proposal.get("catchphrase") or "").strip()
    head = str(proposal.get("head") or "").strip()
    body = str(proposal.get("body") or "").strip()
    image_copy = str(proposal.get("image_copy") or "").strip()
    if (proposal.get("decision") != "create" or not title
            or title[-1] not in "いきしちにひみりぎじびぴえけせてねへめれげぜでべぺ"
            or len([line for line in image_copy.splitlines() if line.strip()]) != 3
            or "｜" not in image_copy.splitlines()[-1]
            or _prohibited_copy_terms(title, catchphrase, head, body, image_copy)):
        raise RuntimeError("storefront_bootstrap_contract_copy_invalid")
    price = next(
        (row for row in official_form.get("display_prices", [])
         if row.get("display_price_jpy") == proposal.get("display_price_jpy")), None,
    )
    if not isinstance(price, dict):
        raise RuntimeError("storefront_bootstrap_contract_price_invalid")
    asset = _render_generated_image_asset(image_copy, draft_service_id, evidence_dir)
    option_title = str(proposal.get("paid_option_title") or "").strip()
    option_price = proposal.get("paid_option_price_jpy")
    if not option_title or type(option_price) is not int:
        raise RuntimeError("storefront_bootstrap_contract_option_invalid")
    unsigned = {
        "version": 1, "platform": "coconala",
        "candidate_key": "storefront:create:v1:" + hashlib.sha256(
            f"{selection['skill_path']}:{demand['evidence_sha256']}".encode()
        ).hexdigest(),
        "draft_service_id": draft_service_id,
        "draft_url": f"https://coconala.com/mypage/services/{draft_service_id}",
        "expected_public_url": f"https://coconala.com/services/{draft_service_id}",
        "origin": "storefront-bootstrap",
        "demand_evidence": {
            "query": demand["query"], "search_url": demand["search_url"],
            "evidence_sha256": demand["evidence_sha256"], "score": demand["score"],
        },
        "capability_evidence": {
            "skill_path": selection["skill_path"],
            "buyer_outcome": selection["buyer_outcome"],
            "deliverable": selection["deliverable"],
        },
        "hero_image_contract": asset["asset_path"],
        "category": category_record["category"],
        "public_fields": {
            "overview_input": title, "expected_title": f"{title}ます",
            "catchphrase": catchphrase, "head": head, "body": body,
            "price_option_value": str(price["value"]),
            "display_price_jpy": int(proposal["display_price_jpy"]),
            "delivery_days": int(proposal["delivery_days"]), "order_limit": 1,
            "accept_estimates": True, "estimate_required": False,
        },
        "category_specific": {
            "features": list(proposal.get("features") or []),
            "industries": list(proposal.get("industries") or []),
            "languages": list(proposal.get("languages") or []),
            "provision_format": str(proposal.get("provision_format") or "1"),
            "fix_limit": str(proposal.get("fix_limit") or "0"),
            "unit_price_jpy_per_character": str(
                proposal.get("unit_price_jpy_per_character") or "0"
            ),
        },
        "subscription": {
            "enabled": True,
            "discount_ratio": str(proposal.get("subscription_discount_ratio") or "5"),
        },
        "paid_options": [{"title": option_title, "price_jpy": option_price, "opened": "1"}],
        "publication_gate": {
            "requires_distinct_catalog_outcome": True,
            "requires_owned_capability": True,
            "requires_available_capacity": True,
            "requires_hero_image": True,
            "requires_no_conflicting_service_experiment": True,
        },
        "success_metric": proposal["success_metric"],
        "observation_window_days": proposal["observation_window_days"],
        "proposal_evidence": [selection["skill_path"], demand["evidence_sha256"]],
    }
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        **unsigned,
        "contract_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "hero_image": {
            "version": 1, "service_id": draft_service_id, "field": "image",
            "mime_type": "image/png", "width": 1220, "height": 1016,
            "claims": [line.strip() for line in image_copy.splitlines() if line.strip()],
            **asset,
        },
    }


def _seal_generated_proposal(
    proposal: dict, hypothesis: dict, source: dict, seller_snapshot: dict,
    family_name: str, capability_families: dict[str, str], allowed_refs: set[str],
    evidence_dir: Path, public_snapshot: dict,
) -> dict | None:
    if proposal.get("decision") == "no_op":
        if (any(proposal.get(key) is not None for key in (
                "service_id", "changed_field", "proposed_value", "success_metric",
                "observation_window_days", "proposed_price_jpy"))
                or not str(proposal.get("no_op_reason") or "").strip()):
            raise RuntimeError("storefront_generated_noop_invalid")
        return None
    field = str(hypothesis.get("field") or "")
    service_id = str(hypothesis.get("service_id") or "")
    evidence = proposal.get("evidence")
    required_refs = {
        f"official:offer-contract:{service_id}:{source['service_version_sha256']}",
        f"owned:capability-family:{family_name}",
    }
    if (proposal.get("decision") != "change" or proposal.get("service_id") != service_id
            or proposal.get("changed_field") != field
            or proposal.get("success_metric") != hypothesis.get("success_metric")
            or proposal.get("observation_window_days") not in {7, 14}
            or proposal.get("no_op_reason") is not None
            or not isinstance(evidence, list) or not evidence
            or not set(evidence) <= allowed_refs or not required_refs <= set(evidence)
            or capability_families.get(service_id) != family_name):
        raise RuntimeError("storefront_generated_proposal_identity_invalid")
    proposed = str(proposal.get("proposed_value") or "").strip()
    if field == "image":
        if (hypothesis.get("before") != 0 or not proposed
                or public_snapshot.get("service_id") != service_id
                or public_snapshot.get("service_image_ids") != []
                or not re.fullmatch(r"[0-9a-f]{64}", str(public_snapshot.get("listing_version_sha256") or ""))):
            raise RuntimeError("storefront_generated_image_before_invalid")
        asset = _render_generated_image_asset(proposed, service_id, evidence_dir)
        return _seal_mutation_contract({
            "version": 1, "platform": "coconala", "service_id": service_id,
            "precondition_listing_version_sha256": source["service_version_sha256"],
            "changed_field": "image", "before_value": {"service_image_ids": []},
            "proposed_value": asset,
            "allowed_delta": ["data[UploadedFile][n*][image_files]"],
            "rollback_value": {"service_image_ids": []},
            "official_readback": {"service_image_count": 1},
            "success_metric": proposal["success_metric"],
            "observation_window_days": proposal["observation_window_days"],
            "capability_family": family_name, "evidence": evidence,
        }, capability_families)
    fields = {str(row.get("name") or ""): row for row in seller_snapshot.get("fields", [])
              if isinstance(row, dict)}
    if field == "title":
        form_field = "data[Service][overview]"
    elif field == "catchphrase":
        # The line search results print under the title. It contradicted the offer while the
        # title and body already carried the new one.
        if not 15 <= len(str(proposed)) <= 30:
            raise RuntimeError("storefront_generated_catchphrase_length_invalid")
        form_field = "data[Service][catchphrase]"
    elif field == "body":
        form_field = "data[Service][head]"
    elif field == "price":
        form_field = "data[Service][price]"
    elif field == "package":
        if seller_snapshot.get("package_slot_added") is not True:
            raise RuntimeError("storefront_generated_package_requires_absent_slot")
        if len(proposed) > 60:
            raise RuntimeError("storefront_generated_package_title_too_long")
        form_field = "data[Option][0]"
    elif field == "FAQ":
        if any(name.startswith("data[Faq]") for name in fields):
            raise RuntimeError("storefront_generated_faq_requires_absent_slot")
        form_field = "data[Faq][0]"
    else:
        raise RuntimeError("storefront_generated_field_unsupported")
    before = ("FAQ_ABSENT" if field == "FAQ" else "PACKAGE_ABSENT" if field == "package"
              else str((fields.get(form_field) or {}).get("value") or ""))
    maximum = (fields.get(form_field) or {}).get("maxLength")
    if (not proposed or proposed == before or (type(maximum) is int and maximum > 0 and len(proposed) > maximum)):
        raise RuntimeError("storefront_generated_value_invalid")
    if field == "title":
        readback = {"public_title": f"{proposed}ます"}
    elif field == "catchphrase":
        # It appears verbatim under the title. Without this branch the field fell through to the
        # price option handling and every proposal was rejected as an invalid price option.
        readback = {"public_catchphrase": proposed}
    elif field == "body":
        readback = {"public_body_sha256": hashlib.sha256(proposed.encode()).hexdigest()}
    elif field == "FAQ":
        question, answer = _split_faq(proposed)
        readback = {"question": question, "answer": answer}
    elif field == "package":
        price_jpy = proposal.get("proposed_price_jpy")
        options = seller_snapshot.get("select_options", {}).get("data[Option][0][price]", [])
        selected = next((row for row in options
                         if str(row.get("value") or "") == str(price_jpy)), None)
        if (type(price_jpy) is not int or selected is None
                or str(selected.get("label") or "") != f"{price_jpy:,}円"):
            raise RuntimeError("storefront_generated_package_price_invalid")
        proposed = {"title": proposed, "price_jpy": price_jpy}
        readback = {"option_title": proposed["title"], "option_price_jpy": price_jpy}
    else:
        options = seller_snapshot.get("select_options", {}).get(form_field, [])
        selected = next((row for row in options if str(row.get("value") or "") == proposed), None)
        label = str((selected or {}).get("label") or "")
        match = re.fullmatch(r"([0-9,]+)円", label)
        if selected is None or match is None:
            raise RuntimeError("storefront_generated_price_option_invalid")
        readback = {"seller_option_value": proposed, "seller_option_label": label,
                    "public_price_jpy": int(match.group(1).replace(",", ""))}
    return _seal_mutation_contract({
        "version": 1, "platform": "coconala", "service_id": service_id,
        "precondition_listing_version_sha256": source["service_version_sha256"],
        "changed_field": field, "before_value": before, "proposed_value": proposed,
        "allowed_delta": [form_field], "rollback_value": before, "official_readback": readback,
        "success_metric": proposal["success_metric"],
        "observation_window_days": proposal["observation_window_days"],
        "capability_family": family_name, "evidence": evidence,
    }, capability_families)


def _experiment_key(service_id: str, field: str, proposed: object) -> str:
    canonical = (proposed.strip() if isinstance(proposed, str)
                 else json.dumps(proposed, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"storefront:v1:{service_id}:{field}:{digest}"


def _image_mutation_contract(
    hypothesis: dict, own_page: dict, asset: dict, capability_families: dict[str, str],
) -> dict:
    if (hypothesis.get("service_id") != TARGET_SERVICE_ID or hypothesis.get("field") != "image"
            or hypothesis.get("before") != 0 or hypothesis.get("executable") is not True
            or hypothesis.get("success_metric") != "views_to_inquiry"):
        raise RuntimeError("storefront_image_hypothesis_invalid")
    if own_page.get("service_image_count") != 0 or own_page.get("service_image_ids") != []:
        raise RuntimeError("storefront_image_before_not_current")
    contract = {
        "version": 1, "platform": "coconala", "service_id": TARGET_SERVICE_ID,
        "precondition_listing_version_sha256": own_page["listing_version_sha256"],
        "changed_field": "image", "before_value": {"service_image_ids": []},
        "proposed_value": {
            "asset_sha256": asset["asset_sha256"],
            "asset_path": _asset_reference(asset["asset_path"]),
        },
        "allowed_delta": ["data[UploadedFile][n*][image_files]"],
        "rollback_value": {"service_image_ids": []},
        "official_readback": {"service_image_count": 1},
        "success_metric": "views_to_inquiry", "observation_window_days": 14,
        "capability_family": capability_families.get(TARGET_SERVICE_ID),
        "evidence": [asset["claim_source"], asset["platform_requirement_source"]],
    }
    return _seal_mutation_contract(contract, capability_families)


def _validate_image_mutation_contract(
    contract: dict, families_path: Path | None = None, state_dir: Path | None = None,
    *, require_assets: bool = True,
) -> None:
    mappings, _ = _load_capability_families(families_path or _storefront_paths()["families"])
    _validate_mutation_contract(contract, mappings)
    proposed = contract.get("proposed_value")
    if contract.get("changed_field") != "image" or not isinstance(proposed, dict):
        raise RuntimeError("storefront_image_mutation_contract_invalid")
    if contract.get("service_id") != GALLERY_SERVICE_ID:
        if (contract.get("before_value") != {"service_image_ids": []}
                or contract.get("allowed_delta") != ["data[UploadedFile][n*][image_files]"]
                or contract.get("rollback_value") != {"service_image_ids": []}
                or contract.get("official_readback") != {"service_image_count": 1}
                or set(proposed) != {"asset_sha256", "asset_path"}
                or not re.fullmatch(r"[0-9a-f]{64}", str(proposed.get("asset_sha256") or ""))):
            raise RuntimeError("storefront_image_mutation_contract_invalid")
        if not require_assets:
            return
        raw_asset = Path(str(proposed.get("asset_path") or ""))
        asset = raw_asset.resolve() if raw_asset.is_absolute() else (GIG_DIR / raw_asset).resolve()
        allowed_roots = (GIG_DIR.resolve(), (state_dir or DEFAULT_STATE).resolve(),
                         _storefront_paths()["image"].parent.resolve())
        try:
            if not any(asset.is_relative_to(root) for root in allowed_roots):
                raise ValueError("outside_allowed_roots")
            data = asset.read_bytes()
        except (OSError, ValueError) as error:
            raise RuntimeError("storefront_image_asset_invalid") from error
        if (len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n"
                or struct.unpack(">II", data[16:24]) != (1220, 1016)
                or hashlib.sha256(data).hexdigest() != proposed["asset_sha256"]):
            raise RuntimeError("storefront_image_asset_identity_invalid")
        return
    before_ids = contract.get("before_value", {}).get("service_image_ids")
    rollback_ids = contract.get("rollback_value", {}).get("service_image_ids")
    assets, kept = proposed.get("replacement_assets"), proposed.get("kept_image_ids")
    readback, allowed = contract.get("official_readback"), contract.get("allowed_delta")
    if (not isinstance(before_ids, list) or len(before_ids) != 6 or rollback_ids != before_ids
            or not isinstance(assets, list) or len(assets) != 4
            or not isinstance(kept, list) or len(kept) != 2 or not set(kept) < set(before_ids)
            or allowed != ["data[UploadedFile][gallery][image_files]"]
            or not isinstance(readback, dict) or readback.get("service_image_count") != 6
            or set(readback.get("removed_image_ids") or []) != set(before_ids) - set(kept)
            or readback.get("kept_image_ids") != kept):
        raise RuntimeError("storefront_gallery_mutation_contract_invalid")
    for row in assets:
        if (not isinstance(row, dict) or set(row) != {
                "replace_image_id", "asset_sha256", "asset_path", "upload_field"}
                or row.get("replace_image_id") not in before_ids
                or not re.fullmatch(r"data\[UploadedFile]\[\d+]\[image_files]", str(row.get("upload_field") or ""))
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("asset_sha256") or ""))):
            raise RuntimeError("storefront_gallery_mutation_contract_invalid")
        if not require_assets:
            continue
        raw_asset = Path(str(row["asset_path"]))
        asset = raw_asset.resolve() if raw_asset.is_absolute() else (GIG_DIR / raw_asset).resolve()
        allowed_roots = (GIG_DIR.resolve(), (state_dir or DEFAULT_STATE).resolve(),
                         _storefront_paths()["gallery"].parent.resolve())
        try:
            if not any(asset.is_relative_to(root) for root in allowed_roots):
                raise ValueError("outside_allowed_roots")
            data = asset.read_bytes()
        except (OSError, ValueError) as error:
            raise RuntimeError("storefront_gallery_asset_invalid") from error
        if hashlib.sha256(data).hexdigest() != row["asset_sha256"]:
            raise RuntimeError("storefront_gallery_asset_identity_invalid")


def _runtime_snapshot_dir(state_dir: Path, *, create: bool) -> Path:
    try:
        if create:
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_root = state_dir.resolve(strict=True)
        if not state_root.is_dir():
            raise ValueError("state_root_not_directory")
        candidate = state_root / "asset-snapshots"
        if candidate.is_symlink():
            raise ValueError("snapshot_dir_symlink")
        if create:
            candidate.mkdir(mode=0o700, exist_ok=True)
        snapshot_dir = candidate.resolve(strict=True)
        if (not snapshot_dir.is_dir() or not snapshot_dir.is_relative_to(state_root)
                or candidate.is_symlink()):
            raise ValueError("snapshot_dir_outside_state")
        if create:
            snapshot_dir.chmod(0o700)
        return snapshot_dir
    except (OSError, ValueError) as error:
        raise RuntimeError("storefront_image_snapshot_invalid") from error


def _snapshot_image_contract_assets(contract: dict, state_dir: Path | None = None) -> dict:
    """Bind each upload to immutable runtime bytes after validating its source."""
    runtime_state = state_dir or DEFAULT_STATE
    _validate_image_mutation_contract(contract, state_dir=runtime_state)
    target_dir = _runtime_snapshot_dir(runtime_state, create=True)

    def snapshot(path: str, digest: str) -> str:
        source = Path(path)
        source = source.resolve() if source.is_absolute() else (GIG_DIR / source).resolve()
        try:
            data = source.read_bytes()
        except OSError as error:
            raise RuntimeError("storefront_image_snapshot_invalid") from error
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError("storefront_image_snapshot_identity_invalid")
        target = target_dir / f"{digest}{source.suffix}"
        if target.exists():
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeError("storefront_image_snapshot_identity_invalid")
            target.chmod(0o400)
            return str(target)
        fd, temporary = tempfile.mkstemp(prefix=f".{digest[:12]}-", suffix=source.suffix, dir=target_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(temporary, target)
            target.chmod(0o400)
            directory_fd = os.open(target_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                Path(temporary).unlink()
            except FileNotFoundError:
                pass
        return str(target)

    bound = json.loads(json.dumps(contract))
    proposed = bound["proposed_value"]
    if bound["service_id"] == GALLERY_SERVICE_ID:
        for row in proposed["replacement_assets"]:
            row["asset_path"] = snapshot(row["asset_path"], row["asset_sha256"])
    else:
        proposed["asset_path"] = snapshot(proposed["asset_path"], proposed["asset_sha256"])
    mappings, _ = _load_capability_families(_storefront_paths()["families"])
    snapshotted = _seal_mutation_contract(
        {key: value for key, value in bound.items() if key != "contract_sha256"}, mappings,
    )
    contract.clear()
    contract.update(snapshotted)
    return contract


def _verified_snapshot_upload_path(
    asset_path: str | Path, digest: str, state_dir: Path | None = None,
) -> str:
    """Return a snapshot only when its bytes still match immediately before upload."""
    try:
        snapshot_dir = _runtime_snapshot_dir(state_dir or DEFAULT_STATE, create=False)
        candidate = Path(asset_path)
        if candidate.is_symlink():
            raise ValueError("snapshot_file_symlink")
        path = candidate.resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(snapshot_dir):
            raise ValueError("snapshot_outside_runtime")
        data = path.read_bytes()
    except (OSError, ValueError) as error:
        raise RuntimeError("storefront_image_snapshot_invalid") from error
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("storefront_image_snapshot_identity_invalid")
    return str(path)


def _render_image_mutation(
    own_page: dict, asset: dict, capability_families: dict[str, str],
) -> dict | None:
    """Render the committed zero-image seed, or nothing once that gap is closed.

    The seed is pinned to one service and one precondition of zero images. Once that
    listing has an image the gap is satisfied, which is a finished job rather than a
    failure, and generic zero-image services are handled by the generated-image path.
    """
    if int(own_page.get("service_image_count") or 0) > 0:
        return None
    contract = _image_mutation_contract({
        "service_id": TARGET_SERVICE_ID, "field": "image", "before": 0,
        "executable": True, "success_metric": "views_to_inquiry",
    }, own_page, asset, capability_families)
    key = contract["allowed_delta"][0]
    before = {key: contract["before_value"]}
    after = {key: contract["proposed_value"]}
    delta = [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
    if delta != contract["allowed_delta"]:
        raise RuntimeError("storefront_image_mutation_multi_field_delta")
    return {"version": 1, "contract": contract, "before": before, "after": after,
            "delta": delta, "published": False}


def _render_gallery_mutation(
    own_page: dict, service_version_sha256: str, asset_contract: dict,
    capability_families: dict[str, str],
) -> dict:
    before_ids = list(asset_contract["before_image_ids"])
    if (own_page.get("service_id") != GALLERY_SERVICE_ID
            or own_page.get("service_image_ids") != before_ids):
        raise RuntimeError("storefront_gallery_before_not_current")
    replacements = []
    for row in asset_contract["replacements"]:
        match = re.search(r"-(\d+)\.png$", row["replace_image_id"])
        if match is None:
            raise RuntimeError("storefront_gallery_image_id_invalid")
        replacements.append({
            "replace_image_id": row["replace_image_id"],
            "asset_sha256": row["asset_sha256"],
            "asset_path": _asset_reference(row["asset_path"]),
            "upload_field": f"data[UploadedFile][{match.group(1)}][image_files]",
        })
    upload_fields = [row["upload_field"] for row in replacements]
    contract = {
        "version": 1, "platform": "coconala", "service_id": GALLERY_SERVICE_ID,
        "precondition_listing_version_sha256": service_version_sha256,
        "changed_field": "image", "before_value": {"service_image_ids": before_ids},
        "proposed_value": {
            "replacement_assets": replacements,
            "kept_image_ids": list(asset_contract["kept_image_ids"]),
        },
        "allowed_delta": ["data[UploadedFile][gallery][image_files]"],
        "rollback_value": {"service_image_ids": before_ids},
        "official_readback": {
            "service_image_count": 6,
            "removed_image_ids": [row["replace_image_id"] for row in asset_contract["replacements"]],
            "kept_image_ids": list(asset_contract["kept_image_ids"]),
        },
        "success_metric": "views_to_inquiry", "observation_window_days": 14,
        "capability_family": capability_families.get(GALLERY_SERVICE_ID),
        "evidence": [asset_contract["claim_source"], asset_contract["platform_requirement_source"]],
    }
    contract = _seal_mutation_contract(contract, capability_families)
    _validate_image_mutation_contract(contract)
    logical_field = contract["allowed_delta"][0]
    return {
        "version": 1, "contract": contract,
        "before": {logical_field: contract["before_value"]},
        "after": {logical_field: contract["proposed_value"]},
        "delta": contract["allowed_delta"], "published": False,
    }


def _render_published_gallery_mutation(state_dir: Path, own_page: dict) -> dict:
    for path in sorted((state_dir / "effect-intents").glob("*.json")):
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (intent.get("status") != "confirmed" or intent.get("effect_ledger_appended") is not True
                or intent.get("service_id") != GALLERY_SERVICE_ID
                or intent.get("changed_field") != "image"):
            continue
        contract = intent.get("mutation_contract")
        if not isinstance(contract, dict):
            raise RuntimeError("published_gallery_contract_missing")
        _validate_image_mutation_contract(contract, state_dir=state_dir, require_assets=False)
        try:
            public_before = json.loads(Path(intent["public_before_path"]).read_text(encoding="utf-8"))
        except FileNotFoundError:
            public_before = {
                "url": f"https://coconala.com/services/{GALLERY_SERVICE_ID}",
                "service_image_ids": contract["rollback_value"]["service_image_ids"],
            }
        except (OSError, KeyError, json.JSONDecodeError) as error:
            raise RuntimeError("published_gallery_before_evidence_missing") from error
        _validate_public_image_acceptance(
            public_before, own_page, contract, state_dir=state_dir, require_assets=False,
        )
        logical_field = contract["allowed_delta"][0]
        return {
            "version": 1, "contract": contract,
            "before": {logical_field: contract["before_value"]},
            "after": {logical_field: contract["proposed_value"]},
            "delta": contract["allowed_delta"], "published": True,
        }
    raise RuntimeError("storefront_gallery_before_not_current")


def _image_judgement(hypothesis: dict, contract: dict) -> dict:
    _validate_image_mutation_contract(contract)
    proposed = contract["proposed_value"]
    asset_digest = proposed.get("asset_sha256") if isinstance(proposed, dict) else None
    digest = str(asset_digest) if isinstance(asset_digest, str) and asset_digest else str(contract["contract_sha256"])
    return {
        "decision": "change", "service_id": contract["service_id"], "changed_field": "image",
        "before_value": hypothesis.get("before"), "proposed_value": digest,
        "hypothesis": str(hypothesis["reason"]), "competitor_evidence_paths": [],
        "capability_evidence_paths": [], "success_metric": contract["success_metric"],
        "observation_window_days": contract["observation_window_days"], "no_op_reason": None,
        "experiment_key": _experiment_key(contract["service_id"], "image", digest), "uncertainty": [],
    }


def _text_judgement(hypothesis: dict, contract: dict, effects_path: Path, now: int) -> dict:
    mappings, _ = _load_capability_families(_storefront_paths()["families"])
    _validate_mutation_contract(contract, mappings)
    if (hypothesis.get("service_id") != contract.get("service_id")
            or hypothesis.get("field") != contract.get("changed_field")
            or contract.get("changed_field") not in {"title", "catchphrase", "body", "package", "price"}
            or hypothesis.get("mutation_contract_sha256") != contract.get("contract_sha256")
            or hypothesis.get("executable") is not True):
        raise RuntimeError("storefront_text_hypothesis_invalid")
    changed_field = str(contract["changed_field"])
    proposed = contract["proposed_value"]
    value = {
        "decision": "change", "service_id": contract["service_id"], "changed_field": changed_field,
        "before_value": contract["before_value"], "proposed_value": proposed,
        "hypothesis": str(hypothesis["reason"]), "competitor_evidence_paths": [],
        "capability_evidence_paths": [], "success_metric": contract["success_metric"],
        "observation_window_days": contract["observation_window_days"], "no_op_reason": None,
        "experiment_key": _experiment_key(contract["service_id"], changed_field, proposed), "uncertainty": [],
    }
    if effects_path.exists():
        for line in effects_path.read_text(encoding="utf-8").splitlines():
            effect = json.loads(line)
            if effect.get("status") != "accepted" or effect.get("effect") != 1:
                continue
            accepted_at = int(effect.get("accepted_at_epoch") or 0)
            if effect.get("experiment_key") == value["experiment_key"]:
                return _guarded_noop(value, "experiment_already_succeeded")
            # The hold keeps one experiment from contaminating the next. A repair the platform
            # has already acted on is not an experiment, so it is not held behind one.
            if (str(effect.get("service_id") or "") == contract["service_id"]
                    and now - accepted_at < 604800
                    and hypothesis.get("compliance_repair") is not True
                    and not hypothesis.get("offer_digest")):
                return _guarded_noop(value, "service_cooldown_7d")
    return value


def _guarded_noop(value: dict, reason: str) -> dict:
    return {
        **value,
        "decision": "no_op",
        "service_id": None,
        "changed_field": None,
        "before_value": None,
        "proposed_value": None,
        "success_metric": None,
        "observation_window_days": None,
        "no_op_reason": reason,
        "experiment_key": None,
    }


def _guard_judgement(
    value: dict,
    *,
    own_page: dict,
    competitor_manifest: dict,
    capability_paths: set[str],
    evidence_dir: Path,
    effects_path: Path,
    minimum_epoch: int,
    now: int,
) -> dict:
    if set(value) != JUDGEMENT_FIELDS:
        raise RuntimeError("judgement_fields_invalid")
    if not isinstance(value.get("hypothesis"), str) or not value["hypothesis"].strip() or len(value["hypothesis"]) > 1000:
        raise RuntimeError("judgement_hypothesis_invalid")
    uncertainty = value.get("uncertainty")
    if not isinstance(uncertainty, list) or len(uncertainty) > 16 or not all(
        isinstance(item, str) and 0 < len(item) <= 240 for item in uncertainty
    ):
        raise RuntimeError("judgement_uncertainty_invalid")
    if value.get("decision") == "no_op":
        if any(value.get(key) is not None for key in (
            "changed_field", "before_value", "proposed_value",
            "success_metric", "observation_window_days", "experiment_key",
        )) or not str(value.get("no_op_reason") or "").strip():
            raise RuntimeError("judgement_noop_contract_invalid")
        return _guarded_noop(value, str(value["no_op_reason"]))
    if value.get("decision") != "change":
        raise RuntimeError("judgement_decision_invalid")
    if value.get("service_id") != TARGET_SERVICE_ID or value.get("changed_field") != "FAQ":
        raise RuntimeError("judgement_not_single_supported_field")
    if value.get("before_value") != "FAQ_ABSENT" or "よくある質問" in str(own_page.get("body") or ""):
        raise RuntimeError("judgement_before_value_not_current")
    proposed = str(value.get("proposed_value") or "").strip()
    if not proposed or proposed == "FAQ_ABSENT" or len(proposed) > 4000:
        raise RuntimeError("judgement_proposed_value_invalid")

    owned = {str(row.get("path")): row for row in competitor_manifest.get("sources", []) if isinstance(row, dict)}
    references = value.get("competitor_evidence_paths")
    if not isinstance(references, list) or not references or not set(references) <= set(owned):
        raise RuntimeError("judgement_competitor_evidence_unowned")
    for reference in references:
        path = Path(reference).resolve()
        try:
            path.relative_to(evidence_dir.resolve())
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("judgement_competitor_evidence_invalid") from error
        if (row.get("official") is not True or row.get("observed") is not True
                or int(row.get("observed_at_epoch") or 0) < minimum_epoch
                or row.get("content_sha256") != owned[reference].get("content_sha256")):
            raise RuntimeError("judgement_competitor_evidence_stale")
    capabilities = value.get("capability_evidence_paths")
    if not isinstance(capabilities, list) or not capabilities or not set(capabilities) <= capability_paths:
        raise RuntimeError("judgement_capability_evidence_unowned")
    if any(not Path(path).is_file() for path in capabilities):
        raise RuntimeError("judgement_capability_evidence_missing")
    if value.get("success_metric") not in MEASURABLE_SUCCESS_METRICS:
        raise RuntimeError("judgement_success_metric_invalid")
    if value.get("observation_window_days") not in {7, 14}:
        raise RuntimeError("judgement_window_invalid")

    key = _experiment_key(TARGET_SERVICE_ID, "FAQ", proposed)
    if value.get("experiment_key") not in {None, key}:
        raise RuntimeError("judgement_experiment_key_invalid")
    if effects_path.exists():
        for line in effects_path.read_text(encoding="utf-8").splitlines():
            try:
                effect = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError("effect_ledger_invalid") from error
            if effect.get("status") != "accepted" or effect.get("effect") != 1:
                continue
            accepted_at = int(effect.get("accepted_at_epoch") or 0)
            if effect.get("experiment_key") == key:
                return _guarded_noop(value, "experiment_already_succeeded")
            if effect.get("service_id") == TARGET_SERVICE_ID and now - accepted_at < 604800:
                return _guarded_noop(value, "service_cooldown_7d")
    return {**value, "experiment_key": key, "no_op_reason": None}


def _presend_guard(
    judgement: dict, own_page: dict, mutation_contract: dict | None = None,
    state_dir: Path | None = None,
) -> None:
    if judgement.get("decision") != "change":
        return
    if judgement.get("changed_field") == "image":
        if not isinstance(mutation_contract, dict):
            raise RuntimeError("presend_image_mutation_contract_missing")
        _validate_image_mutation_contract(mutation_contract, state_dir=state_dir)
        before_value = mutation_contract.get("before_value", {})
        expected_ids = before_value.get("service_image_ids")
        if (mutation_contract.get("contract_sha256") is None
                or judgement.get("service_id") != mutation_contract.get("service_id")
                or own_page.get("service_image_ids") != expected_ids):
            raise RuntimeError("presend_image_current_value_changed")
        return
    if (judgement.get("service_id") != TARGET_SERVICE_ID
            or judgement.get("changed_field") != "FAQ"
            or judgement.get("before_value") != "FAQ_ABSENT"
            or "よくある質問" in str(own_page.get("body") or "")):
        raise RuntimeError("presend_current_value_changed")


def _split_faq(proposed: str) -> tuple[str, str]:
    match = FAQ_PATTERN.fullmatch(proposed.strip())
    if match is None:
        raise RuntimeError("faq_proposal_format_invalid")
    question, answer = match.group("question").strip(), match.group("answer").strip()
    if not question or not answer or len(question) > 400 or len(answer) > 400:
        raise RuntimeError("faq_proposal_length_invalid")
    return question, answer


def _form_base_fields(snapshot: dict) -> list[dict]:
    fields = snapshot.get("fields")
    if not isinstance(fields, list):
        raise RuntimeError("seller_form_fields_invalid")
    return [row for row in fields if isinstance(row, dict) and not str(row.get("name") or "").startswith("data[Faq]")]


def _validate_form_delta(before: dict, after: dict, question: str, answer: str) -> None:
    if before.get("url") != f"https://coconala.com/mypage/services/{TARGET_SERVICE_ID}" or after.get("url") != before.get("url"):
        raise RuntimeError("seller_form_url_invalid")
    if _form_base_fields(before) != _form_base_fields(after):
        raise RuntimeError("seller_form_non_faq_changed")
    before_faq = [row for row in before["fields"] if str(row.get("name") or "").startswith("data[Faq]")]
    after_faq = [row for row in after["fields"] if str(row.get("name") or "").startswith("data[Faq]")]
    if before_faq or [(row.get("name"), row.get("value")) for row in after_faq] != [
        ("data[Faq][0][question]", question), ("data[Faq][0][answer]", answer),
    ]:
        raise RuntimeError("seller_form_faq_delta_invalid")


def _validate_public_acceptance(before: dict, after: dict, question: str, answer: str) -> None:
    url = f"https://coconala.com/services/{TARGET_SERVICE_ID}"
    if before.get("url") != url or after.get("url") != url:
        raise RuntimeError("public_readback_url_invalid")
    if before.get("content_sha256") == after.get("content_sha256"):
        raise RuntimeError("public_readback_hash_unchanged")
    before_body, after_body = str(before.get("body") or ""), str(after.get("body") or "")
    if question in before_body or answer in before_body or question not in after_body or answer not in after_body:
        raise RuntimeError("public_faq_readback_mismatch")


def _validate_public_image_acceptance(
    before: dict, after: dict, contract: dict, state_dir: Path | None = None,
    *, require_assets: bool = True,
) -> None:
    _validate_image_mutation_contract(
        contract, state_dir=state_dir, require_assets=require_assets,
    )
    service_id = str(contract.get("service_id") or "")
    url = f"https://coconala.com/services/{service_id}"
    if before.get("url") != url or after.get("url") != url:
        raise RuntimeError("public_image_readback_url_invalid")
    if before.get("service_image_ids") != contract.get("rollback_value", {}).get("service_image_ids"):
        raise RuntimeError("public_image_before_invalid")
    expected = contract.get("official_readback", {}).get("service_image_count")
    if after.get("service_image_count") != expected or len(after.get("service_image_ids") or []) != expected:
        raise RuntimeError("public_image_count_mismatch")
    if service_id == GALLERY_SERVICE_ID:
        after_ids = after.get("service_image_ids") or []
        removed = set(contract["official_readback"]["removed_image_ids"])
        kept = contract["official_readback"]["kept_image_ids"]
        if (removed & set(after_ids) or not set(kept) <= set(after_ids)
                or after_ids[2] != kept[0] or after_ids[4] != kept[1]):
            raise RuntimeError("public_gallery_identity_or_order_mismatch")


def _validate_image_form_delta(
    before: dict, after: dict, contract: dict, state_dir: Path | None = None,
) -> None:
    _validate_image_mutation_contract(contract, state_dir=state_dir)
    url = f"https://coconala.com/mypage/services/{contract['service_id']}"
    if before.get("url") != url or after.get("url") != url:
        raise RuntimeError("seller_image_form_url_invalid")
    before_fields = [row for row in _form_base_fields(before) if not str(row.get("name") or "").startswith("data[UploadedFile]")]
    after_fields = [row for row in _form_base_fields(after) if not str(row.get("name") or "").startswith("data[UploadedFile]")]
    uploads = [row for row in after.get("fields") or [] if str(row.get("name") or "").startswith("data[UploadedFile]")]
    if before_fields != after_fields:
        raise RuntimeError("seller_image_non_image_changed")
    if contract["service_id"] != GALLERY_SERVICE_ID:
        if (any(str(row.get("name") or "").startswith("data[UploadedFile]") for row in before.get("fields") or [])
                or len(uploads) != 1
                or not re.fullmatch(r"data\[UploadedFile]\[n\d+]\[image_files]", str(uploads[0].get("name") or ""))):
            raise RuntimeError("seller_image_upload_field_invalid")
        return
    before_uploads = {str(row.get("name") or ""): str(row.get("value") or "")
                      for row in before.get("fields") or []
                      if str(row.get("name") or "").startswith("data[UploadedFile]")}
    after_uploads = {str(row.get("name") or ""): str(row.get("value") or "")
                     for row in uploads}
    changed = [name for name in sorted(set(before_uploads) | set(after_uploads))
               if before_uploads.get(name) != after_uploads.get(name)]
    expected = sorted(row["upload_field"] for row in contract["proposed_value"]["replacement_assets"])
    if changed != expected:
        raise RuntimeError("seller_gallery_upload_delta_invalid")


def _seller_snapshot(ws_url: str) -> dict:
    return _seller_snapshot_for(ws_url, TARGET_SERVICE_ID)


def _relogin_coconala() -> str:
    """Ask session_vault to log this account back in, and say what it answered.

    The recovery already exists, with its own cooldown and its own vault dump; this only
    connects the wake that noticed the expiry to it. Never raises: a recovery that fails is
    reported alongside the expiry, because the wake's job is to report honestly, not to
    guarantee the session.
    """
    try:
        module = _session_vault()
        result = module.relogin_coconala()
    except Exception as error:
        return f"relogin_unavailable:{type(error).__name__}"
    if not isinstance(result, dict):
        return "relogin_answer_invalid"
    if result.get("ok") is True:
        return "relogin_ok"
    if result.get("skipped") is True:
        return "relogin_cooldown"
    return "relogin_failed:" + str(result.get("reason") or "unknown")[:80]


def _session_vault():
    """Import session_vault from the running release, the way this file reaches its siblings."""
    import importlib.util

    path = BROWSER_DIR / "scripts" / "session_vault.py"
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("_storefront_session_vault", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dashboard_says_signed_out(ws_url: str) -> bool:
    """True when the seller dashboard is really the login page.

    `observe_storefront` reports a parsed count and no url, so a login wall reaches the
    caller as a shelf with nothing on it. This asks the page directly, and answers False on
    any error so a browser hiccup is never mistaken for a dead session.
    """
    import listing_inventory

    try:
        observed = asyncio.run(listing_inventory._eval_json(
            ws_url, listing_inventory.LISTINGS_URL,
            "JSON.stringify({url:location.href,body:document.body ? "
            "document.body.innerText.slice(0,400) : ''})",
        ))
    except Exception:
        return False
    if _looks_signed_out(observed.get("url")):
        return True
    body = str(observed.get("body") or "")
    return "メールアドレスでログイン" in body or "会員登録はこちら" in body


def _looks_signed_out(observed_url: str | None) -> bool:
    """True when an authenticated page sent us somewhere that is not the seller area."""
    parts = urlsplit(str(observed_url or ""))
    if not parts.path:
        return False
    if parts.hostname not in {"coconala.com", "www.coconala.com"}:
        return True
    return not parts.path.startswith("/mypage/")


def _seller_snapshot_for(ws_url: str, service_id: str) -> dict:
    import listing_inventory

    url = f"https://coconala.com/mypage/services/{service_id}"
    required = {"data[Service][overview]", "data[Service][head]", "data[Service][price]"}
    last = {}
    # Three attempts a second apart gave this form two seconds to hydrate, which is the
    # shortest settling window anywhere in this loop and the one production actually
    # exhausted. `_read_official_catalog`, `_collect_competitors` and `open_tab_with_retry`
    # all wait five attempts three seconds apart for the same class of transient, so this
    # one does too rather than being the weakest link by accident.
    for attempt in range(5):
        last = asyncio.run(listing_inventory._eval_json(ws_url, url, SELLER_FORM_EXPRESSION))
        names = {str(row.get("name") or "") for row in last.get("fields", []) if isinstance(row, dict)}
        if last.get("url") == url and required <= names:
            return last
        if _looks_signed_out(last.get("url")):
            # Being sent away from an authenticated seller page is an expired session, which
            # no amount of retrying fixes. Name it so the owner sees the real cause instead of
            # a hydration error repeating on every wake.
            raise RuntimeError(f"storefront_session_expired:{last.get('url')}")
        if attempt < 4:
            time.sleep(3)
    raise RuntimeError("seller_form_not_fully_hydrated")


def _seller_snapshot_from_fresh_tab(default_tab_script: Path, service_id: str) -> dict:
    import storefront_draft

    last_error: Exception | None = None
    url = f"https://coconala.com/mypage/services/{service_id}"
    for attempt in range(3):
        opened = storefront_draft.open_tab_with_retry(
            [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
             "--background", "open", url], capture_output=True, text=True,
            check=False, timeout=30,
        )
        tab = None
        try:
            tab = json.loads(opened.stdout)
            if opened.returncode != 0 or tab.get("ok") is not True:
                raise RuntimeError("storefront_create_source_tab_open_failed")
            return _seller_snapshot_for(str(tab["ws"]), service_id)
        except Exception as error:
            last_error = error
            if attempt >= 2:
                raise RuntimeError(f"storefront_create_source_snapshot_failed:{type(error).__name__}") from error
            # Deliberately short, and deliberately not the loop's five-by-three shape: the call
            # this wraps is `_seller_snapshot_for`, which already waits that long by itself.
            # Widening here would nest one retry inside another and spend a minute of the wake
            # on a single service.
            time.sleep(1)
        finally:
            if isinstance(tab, dict) and tab.get("target_id"):
                subprocess.run(
                    [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
                     "close", str(tab["target_id"])], capture_output=True, text=True,
                    check=False, timeout=30,
                )
    raise RuntimeError("storefront_create_source_snapshot_failed") from last_error


async def _seller_package_snapshot_async(ws_url: str, service_id: str) -> dict:
    import websockets
    import listing_inventory

    url = f"https://coconala.com/mypage/services/{service_id}"
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10,
                                  max_size=40 * 1024 * 1024) as ws:
        cid = 1
        await listing_inventory._call(ws, "Page.enable", {}, cid); cid += 1
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate", "params": {"url": url}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(
            ws, asyncio.get_event_loop().time() + 15, cid,
        )

        async def evaluate(expression: str) -> object:
            nonlocal cid
            response = await listing_inventory._call(
                ws, "Runtime.evaluate", {"expression": expression, "returnByValue": True}, cid,
            )
            cid += 1
            return response.get("result", {}).get("result", {}).get("value")

        before = json.loads(str(await evaluate(SELLER_FORM_EXPRESSION) or "{}"))
        option_titles = [row for row in before.get("fields") or []
                         if re.fullmatch(r"data\[Option]\[\d+]\[title]", str(row.get("name") or ""))]
        if option_titles:
            return {**before, "package_slot_added": False}
        clicked = await evaluate(
            "(()=>{const b=document.querySelector('#addOption');if(!b)return false;b.click();return true})()"
        )
        if clicked is not True:
            raise RuntimeError("seller_package_add_control_missing")
        await asyncio.sleep(0.5)
        after = json.loads(str(await evaluate(SELLER_FORM_EXPRESSION) or "{}"))
        fields = {str(row.get("name") or ""): row for row in after.get("fields") or []}
        if not {"data[Option][0][title]", "data[Option][0][price]", "data[Option][0][opened]"} <= set(fields):
            raise RuntimeError("seller_package_slot_not_hydrated")
        return {**after, "package_slot_added": True}


def _seller_package_snapshot_for(ws_url: str, service_id: str) -> dict:
    return asyncio.run(_seller_package_snapshot_async(ws_url, service_id))


def _effect_intent_path(state_dir: Path, experiment_key: str) -> Path:
    digest = hashlib.sha256(experiment_key.encode()).hexdigest()
    return state_dir / "effect-intents" / f"{digest}.json"


def _pending_recovery(
    state_dir: Path, own_page: dict, ws_url: str | None = None, evidence_dir: Path | None = None,
) -> dict | None:
    body = str(own_page.get("body") or "")
    intents = []
    for path in sorted((state_dir / "effect-intents").glob("*.json")):
        try:
            intent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        intents.append((path, intent))
    latest_confirmed = {
        (str(intent.get("service_id") or ""), str(intent.get("changed_field") or "")): max(
            int(candidate.get("prepared_at_epoch") or 0)
            for _, candidate in intents
            if candidate.get("status") == "confirmed"
            and candidate.get("service_id") == intent.get("service_id")
            and candidate.get("changed_field") == intent.get("changed_field")
        )
        for _, intent in intents if intent.get("status") == "confirmed"
    }
    for path, intent in intents:
        if intent.get("status") not in {"prepared", "observed"}:
            continue
        key = (str(intent.get("service_id") or ""), str(intent.get("changed_field") or ""))
        if latest_confirmed.get(key, 0) > int(intent.get("prepared_at_epoch") or 0):
            continue
        if intent.get("changed_field") == "image":
            contract = intent.get("mutation_contract")
            if not isinstance(contract, dict):
                raise RuntimeError("pending_image_contract_missing")
            _validate_image_mutation_contract(contract, state_dir=state_dir)
            service_id = str(contract["service_id"])
            page = own_page
            if service_id != str(own_page.get("service_id") or ""):
                if ws_url is None or evidence_dir is None:
                    raise RuntimeError("pending_image_readback_context_missing")
                page = _observe_own_page(
                    ws_url, evidence_dir, f"recovery-public-{service_id}.json", service_id,
                )
            image_ids = page.get("service_image_ids")
            if service_id != GALLERY_SERVICE_ID and page.get("service_image_count") == 1:
                return {**intent, "intent_path": str(path), "_recovery_public_page": page}
            if service_id == GALLERY_SERVICE_ID:
                readback = contract["official_readback"]
                removed = set(readback["removed_image_ids"])
                kept = readback["kept_image_ids"]
                if (page.get("service_image_count") == 6 and isinstance(image_ids, list)
                        and not removed.intersection(image_ids) and set(kept) <= set(image_ids)):
                    return {**intent, "intent_path": str(path), "_recovery_public_page": page}
            if image_ids == contract["rollback_value"]["service_image_ids"]:
                continue
            raise RuntimeError("pending_image_effect_public_readback_invalid")
        if intent.get("changed_field") in {"title", "catchphrase", "body", "package", "price"}:
            contract = intent.get("mutation_contract")
            if not isinstance(contract, dict) or ws_url is None or evidence_dir is None:
                raise RuntimeError("pending_text_contract_missing")
            mappings, _ = _load_capability_families(_storefront_paths()["families"])
            _validate_mutation_contract(contract, mappings)
            service_id = str(contract["service_id"])
            page = _observe_own_page(
                ws_url, evidence_dir, f"recovery-public-{service_id}.json", service_id,
            )
            seller = _seller_snapshot_for(ws_url, service_id)
            values = {str(row.get("name") or ""): str(row.get("value") or "")
                      for row in seller.get("fields") or [] if isinstance(row, dict)}
            if contract["changed_field"] == "package":
                proposed = contract["proposed_value"]
                current_matches = (
                    values.get("data[Option][0][title]") == proposed["title"]
                    and values.get("data[Option][0][price]") == str(proposed["price_jpy"])
                    and values.get("data[Option][0][opened]") == "1"
                )
                expected = str(proposed["title"])
                rollback_matches = not any(name.startswith("data[Option]") for name in values)
            else:
                current = values.get(contract["allowed_delta"][0])
                expected = (contract["official_readback"].get("public_title")
                            if contract["changed_field"] == "title"
                            else f"{int(contract['official_readback']['public_price_jpy']):,}円"
                            if contract["changed_field"] == "price" else contract["proposed_value"])
                current_matches = current == contract["proposed_value"]
                rollback_matches = current == contract["before_value"]
            if current_matches and expected in str(page.get("body") or ""):
                return {**intent, "intent_path": str(path), "_recovery_public_page": page,
                        "_recovery_seller_snapshot": seller}
            if rollback_matches:
                continue
            raise RuntimeError("pending_text_effect_public_readback_invalid")
        question, answer = str(intent.get("question") or ""), str(intent.get("answer") or "")
        if not question or not answer or len(question) > 400 or len(answer) > 400:
            raise RuntimeError("pending_effect_values_invalid")
        visible = (question in body, answer in body)
        if visible == (True, True):
            return {**intent, "intent_path": str(path)}
        if visible[0] != visible[1]:
            raise RuntimeError("pending_effect_partial_public_readback")
    return None


async def _execute_faq_effect_async(
    ws_url: str,
    *,
    question: str,
    answer: str,
    judgement: dict,
    public_before_path: Path,
    evidence_dir: Path,
    state_dir: Path,
) -> tuple[dict, dict, Path]:
    import websockets
    import listing_inventory

    edit_url = f"https://coconala.com/mypage/services/{TARGET_SERVICE_ID}"
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10, max_size=40 * 1024 * 1024) as ws:
        cid = 1
        await listing_inventory._call(ws, "Page.enable", {}, cid); cid += 1
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate", "params": {"url": edit_url}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(ws, asyncio.get_event_loop().time() + 15, cid)

        async def evaluate(expression: str) -> object:
            nonlocal cid
            response = await listing_inventory._call(
                ws, "Runtime.evaluate", {"expression": expression, "returnByValue": True}, cid,
            )
            cid += 1
            return response.get("result", {}).get("result", {}).get("value")

        before_raw = await evaluate(SELLER_FORM_EXPRESSION)
        before = json.loads(str(before_raw or "{}"))
        fill = await evaluate(
            "(()=>{"
            f"if(location.href!=={json.dumps(edit_url)})return JSON.stringify({{ok:false,reason:'edit_url'}});"
            "const form=document.forms[0],existing=[...form.elements].filter(e=>(e.name||'').startsWith('data[Faq]'));"
            "if(existing.length)return JSON.stringify({ok:false,reason:'faq_exists'});"
            "document.querySelector('#addQA')?.click();"
            "const q=document.querySelector('#Faq0Question'),a=document.querySelector('#Faq0Answer');"
            "if(!q||!a)return JSON.stringify({ok:false,reason:'faq_controls_missing'});"
            f"q.value={json.dumps(question)};a.value={json.dumps(answer)};"
            "for(const e of [q,a]){e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}))}"
            "const submit=form.querySelector('button.submitButton.js_button-edit[type=submit]');"
            "if(!submit)return JSON.stringify({ok:false,reason:'submit_missing'});"
            "submit.scrollIntoView({block:'center'});const r=submit.getBoundingClientRect();"
            "return JSON.stringify({ok:true,question:q.value,answer:a.value,rect:{x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height}})})()"
        )
        fill_result = json.loads(str(fill or "{}"))
        if fill_result.get("ok") is not True or fill_result.get("question") != question or fill_result.get("answer") != answer:
            raise RuntimeError(f"seller_faq_fill_failed:{fill_result.get('reason')}")
        after_raw = await evaluate(SELLER_FORM_EXPRESSION)
        after = json.loads(str(after_raw or "{}"))
        _validate_form_delta(before, after, question, answer)
        before_path, after_path = evidence_dir / "seller-form-before.json", evidence_dir / "seller-form-filled.json"
        _atomic_write(before_path, before)
        _atomic_write(after_path, after)
        intent_path = _effect_intent_path(state_dir, str(judgement["experiment_key"]))
        intent = {
            "version": 1, "status": "prepared", "service_id": TARGET_SERVICE_ID,
            "changed_field": "FAQ", "experiment_key": judgement["experiment_key"],
            "question": question, "answer": answer, "public_before_path": str(public_before_path),
            "seller_form_before_path": str(before_path), "prepared_at_epoch": int(time.time()),
            "effect_origin_pass_id": evidence_dir.name, "judgement": judgement,
        }
        _atomic_write(intent_path, intent)
        rect = fill_result["rect"]
        if min(float(rect.get("w") or 0), float(rect.get("h") or 0)) <= 0:
            raise RuntimeError("seller_submit_not_visible")
        for event_type in ("mousePressed", "mouseReleased"):
            await listing_inventory._call(ws, "Input.dispatchMouseEvent", {
                "type": event_type, "x": float(rect["x"]), "y": float(rect["y"]),
                "button": "left", "clickCount": 1,
            }, cid)
            cid += 1
        await asyncio.sleep(3)
        return before, after, intent_path


def _execute_faq_effect(**kwargs) -> tuple[dict, dict, Path]:
    return asyncio.run(_execute_faq_effect_async(**kwargs))


def _validate_text_form_delta(before: dict, after: dict, contract: dict) -> None:
    field = contract["allowed_delta"][0]
    url = f"https://coconala.com/mypage/services/{contract['service_id']}"
    if before.get("url") != url or after.get("url") != url:
        raise RuntimeError("seller_text_form_url_invalid")
    before_targets = [row for row in before.get("fields") or [] if row.get("name") == field]
    after_targets = [row for row in after.get("fields") or [] if row.get("name") == field]
    if (len(before_targets) != 1 or len(after_targets) != 1
            or before_targets[0].get("value") != contract["before_value"]
            or after_targets[0].get("value") != contract["proposed_value"]):
        raise RuntimeError("seller_text_value_mismatch")
    strip = lambda snapshot: [row for row in snapshot.get("fields") or [] if row.get("name") != field]
    if strip(before) != strip(after):
        raise RuntimeError("seller_text_non_target_changed")


def _validate_text_public_acceptance(before: dict, after: dict, contract: dict) -> None:
    url = f"https://coconala.com/services/{contract['service_id']}"
    readback = contract["official_readback"]
    if contract["changed_field"] == "package":
        expected = str(readback["option_title"])
        secondary = f"{int(readback['option_price_jpy']):,}円"
    elif contract["changed_field"] == "price":
        expected = f"{int(readback['public_price_jpy']):,}円"
        secondary = None
    else:
        expected = str(readback.get("public_title") or contract["proposed_value"])
        secondary = None
    if before.get("url") != url or after.get("url") != url:
        raise RuntimeError("public_text_readback_url_invalid")
    body = str(after.get("body") or "")
    if (before.get("content_sha256") == after.get("content_sha256") or expected not in body
            or (secondary is not None and secondary not in body)):
        raise RuntimeError("public_text_readback_mismatch")


def _validate_package_form_delta(before: dict, after: dict, contract: dict) -> None:
    url = f"https://coconala.com/mypage/services/{contract['service_id']}"
    if before.get("url") != url or after.get("url") != url:
        raise RuntimeError("seller_package_form_url_invalid")
    before_fields = {str(row.get("name") or ""): str(row.get("value") or "")
                     for row in before.get("fields") or [] if isinstance(row, dict)}
    after_fields = {str(row.get("name") or ""): str(row.get("value") or "")
                    for row in after.get("fields") or [] if isinstance(row, dict)}
    option_names = {name for name in set(before_fields) | set(after_fields) if name.startswith("data[Option]")}
    if any(name in before_fields for name in option_names):
        raise RuntimeError("seller_package_before_not_absent")
    expected = contract["proposed_value"]
    required = {
        "data[Option][0][service_id]": contract["service_id"],
        "data[Option][0][title]": expected["title"],
        "data[Option][0][price]": str(expected["price_jpy"]),
        "data[Option][0][opened]": "1",
    }
    unexpected = option_names - set(required)
    if ({name: after_fields.get(name) for name in required} != required
            or any(name != "data[Option][0][id]" or not after_fields.get(name, "").isdigit()
                   for name in unexpected)):
        raise RuntimeError("seller_package_value_mismatch")
    if ({name: value for name, value in before_fields.items() if not name.startswith("data[Option]")}
            != {name: value for name, value in after_fields.items() if not name.startswith("data[Option]")}):
        raise RuntimeError("seller_package_non_target_changed")


async def _execute_listing_state_effect_async(
    ws_url: str, *, contract: dict, evidence_dir: Path,
) -> dict:
    """Archive one listing from its own seller card, then prove it can come back.

    Archiving is the platform's recoverable unpublish: the listing, its versions and its
    sales history survive. The restore control is verified on the archived card before the
    effect is accepted, so retirement is never a one-way door.
    """
    import websockets
    import listing_inventory

    mappings, _ = _load_capability_families(_storefront_paths()["families"])
    _validate_mutation_contract(contract, mappings)
    if contract.get("changed_field") != "listing_state":
        raise RuntimeError("storefront_retire_contract_invalid")
    service_id = str(contract["service_id"])
    action = str(contract["proposed_value"]["action"])
    if action != f"/services/archive/{service_id}":
        raise RuntimeError("storefront_retire_action_invalid")
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10,
                                  max_size=40 * 1024 * 1024) as ws:
        cid = 1
        await listing_inventory._call(ws, "Page.enable", {}, cid); cid += 1
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate",
                                  "params": {"url": "https://coconala.com/mypage/services_lists"}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(ws, asyncio.get_event_loop().time() + 15, cid)
        # Reconcile the resource before retrying.  A prior click may have reached Coconala
        # even when this process died before recording its readback.
        raw, cid = await _evaluate(ws, (
            "JSON.stringify([...document.querySelectorAll('.serviceListContentBox')]"
            f".filter(c=>(c.innerHTML||'').includes({json.dumps('/services/' + service_id)}))"
            ".map(c=>({text:(c.innerText||'').slice(0,200),restore:[...c.querySelectorAll('a')]"
            ".map(a=>a.getAttribute('href')||'').filter(h=>/\\/services\\/(open|public)\\//.test(h))})))"
        ), cid)
        before_cards = json.loads(str(raw or "[]"))
        if before_cards and "公開中" not in str(before_cards[0].get("text") or ""):
            if not (before_cards[0].get("restore") or []):
                raise RuntimeError("storefront_retire_prior_effect_unresolved")
            readback = {"service_id": service_id, "cards": before_cards,
                        "observed_at_epoch": int(time.time()), "archived": True,
                        "restore_href": before_cards[0]["restore"][0], "recovered": True}
            _atomic_write(evidence_dir / f"listing-state-readback-{service_id}.json", readback)
            return readback
        clicked, cid = await _evaluate(ws, (
            "(()=>{const a=[...document.querySelectorAll('a.js_change-open-status')]"
            f".find(e=>e.getAttribute('href')==={json.dumps(action)});"
            "if(!a)return false;a.click();return true})()"
        ), cid)
        if clicked is not True:
            raise RuntimeError("storefront_retire_control_absent_at_submit")
        # Measured 2026-08-18: the anchor alone archives. The listing left the 公開中 list at
        # 12:25:22 after the 12:23:18 attempt, which clicked nothing else. An earlier version
        # of this step hunted for a confirmation control by label and would have clicked a
        # 削除する on that page, so it is gone: nothing here clicks a control it did not bind.
        await asyncio.sleep(5)
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate",
                                  "params": {"url": "https://coconala.com/mypage/services_lists"}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(ws, asyncio.get_event_loop().time() + 15, cid)
        raw, cid = await _evaluate(ws, (
            "JSON.stringify([...document.querySelectorAll('.serviceListContentBox')]"
            f".filter(c=>(c.innerHTML||'').includes({json.dumps('/services/' + service_id)}))"
            ".map(c=>({text:(c.innerText||'').slice(0,200),restore:[...c.querySelectorAll('a')]"
            ".map(a=>a.getAttribute('href')||'').filter(h=>/\\/services\\/(open|public)\\//.test(h))})))"
        ), cid)
        cards = json.loads(str(raw or "[]"))
    readback = {"service_id": service_id, "cards": cards,
                "observed_at_epoch": int(time.time())}
    _atomic_write(evidence_dir / f"listing-state-readback-{service_id}.json", readback)
    # An earlier reading of this took the card leaving the seller list as proof the archive had
    # worked. An anonymous rendered fetch of the same listing then showed it complete to a
    # buyer, so absence from this list proves nothing and must never be reported as success.
    if not cards:
        raise RuntimeError("storefront_retire_card_missing_after_archive")
    if "公開中" in str(cards[0].get("text") or ""):
        raise RuntimeError("storefront_retire_still_public")
    if not (cards[0].get("restore") or []):
        raise RuntimeError("storefront_retire_restore_control_unverified")
    return {**readback, "archived": True, "restore_href": cards[0]["restore"][0]}


async def _restore_listing_state_async(ws_url: str, *, service_id: str, restore_href: str) -> dict:
    """Put an archived listing back, using the restore control observed on its own card."""
    import websockets
    import listing_inventory

    if not restore_href.startswith("/services/"):
        raise RuntimeError("storefront_restore_href_invalid")
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10,
                                  max_size=40 * 1024 * 1024) as ws:
        cid = 1
        await listing_inventory._call(ws, "Page.enable", {}, cid); cid += 1
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate",
                                  "params": {"url": "https://coconala.com/mypage/services_lists"}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(ws, asyncio.get_event_loop().time() + 15, cid)
        clicked, cid = await _evaluate(ws, (
            "(()=>{const a=[...document.querySelectorAll('a')]"
            f".find(e=>e.getAttribute('href')==={json.dumps(restore_href)});"
            "if(!a)return false;a.click();return true})()"
        ), cid)
        if clicked is not True:
            raise RuntimeError("storefront_restore_control_absent")
        await asyncio.sleep(5)
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate",
                                  "params": {"url": "https://coconala.com/mypage/services_lists"}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(ws, asyncio.get_event_loop().time() + 15, cid)
        raw, cid = await _evaluate(ws, (
            "JSON.stringify([...document.querySelectorAll('.serviceListContentBox')]"
            f".filter(c=>(c.innerHTML||'').includes({json.dumps('/services/' + service_id)}))"
            ".map(c=>(c.innerText||'').slice(0,120)))"
        ), cid)
        cards = json.loads(str(raw or "[]"))
    if not cards or "公開中" not in str(cards[0]):
        raise RuntimeError("storefront_restore_not_public")
    return {"service_id": service_id, "restored": True, "observed_at_epoch": int(time.time())}


async def _reopen_one_suspended_service_async(ws_url: str, *, service_id: str) -> dict:
    """Reopen one 受付休止中 listing via its own card's plain reopen link, then prove it stuck.

    Measured 2026-09-04 on /mypage/services_lists: the popover template's reopen branch
    (rendered when data-publish-links.stop_fg==1) is a plain anchor
    `<a href="/services/reopen/<id>">受付を再開する</a>` -- unlike stop/archive
    (a.js_change-open-status, AJAX-bound, archive has a confirmation modal), this control
    carries no onclick class and no confirmation, so the bound action is a direct
    navigation, not a click on a JS handler this module never observed.
    """
    import websockets
    import listing_inventory

    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10,
                                  max_size=40 * 1024 * 1024) as ws:
        cid = 1
        await listing_inventory._call(ws, "Page.enable", {}, cid); cid += 1
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate",
                                  "params": {"url": f"https://coconala.com/services/reopen/{service_id}"}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(ws, asyncio.get_event_loop().time() + 15, cid)
        await asyncio.sleep(2)
        card_text = None
        for page in (1, 2, 3):
            url = ("https://coconala.com/mypage/services_lists" if page == 1
                   else f"https://coconala.com/mypage/services_lists/page:{page}")
            await ws.send(json.dumps({"id": cid, "method": "Page.navigate", "params": {"url": url}})); cid += 1
            _, cid = await listing_inventory._wait_for_load(ws, asyncio.get_event_loop().time() + 15, cid)
            raw, cid = await _evaluate(ws, (
                "JSON.stringify([...document.querySelectorAll('.serviceListContentBox')]"
                f".filter(c=>(c.innerHTML||'').includes({json.dumps('/services/' + service_id)}))"
                ".map(c=>(c.innerText||'').slice(0,80)))"
            ), cid)
            cards = json.loads(str(raw or "[]"))
            if cards:
                card_text = str(cards[0])
                break
    if card_text is None:
        raise RuntimeError("storefront_reopen_card_missing_after_reopen")
    state = (PUBLIC_LISTING_STATE if PUBLIC_LISTING_STATE in card_text
             else SUSPENDED_LISTING_STATE if SUSPENDED_LISTING_STATE in card_text else "unknown")
    if state != PUBLIC_LISTING_STATE:
        raise RuntimeError(f"storefront_reopen_not_public:{state}")
    return {"service_id": service_id, "reopened": True, "readback_state": state,
            "observed_at_epoch": int(time.time())}


def _reopen_suspended_listings(
    inventory_services: list[dict], *, default_tab_script: Path, effects_path: Path, pass_id: str,
) -> int:
    """Reopen every 受付休止中 listing once per wake, before any proposal/mutation phase.

    Purchases were 0 since 2026-08-05 because every listing sat in this recoverable,
    buyer-invisible-to-purchase state -- nothing was buyable. Idempotent: a service only
    appears here while its freshly observed card still reads 受付休止中, so a replay
    against an all-public catalogue makes 0 browser calls (see test_storefront_direct.py).
    Each listing gets its own tab, mirroring the retire effect's own-tab pattern: reusing
    the wake's shared leased socket after a full pass of browsing is what returns HTTP 500.
    """
    import storefront_draft

    targets = sorted({
        str(row.get("service_id")) for row in inventory_services
        if isinstance(row, dict) and str(row.get("state") or "") == SUSPENDED_LISTING_STATE
        and str(row.get("service_id") or "").isdigit()
    })
    effect_count = 0
    for service_id in targets:
        opened = storefront_draft.open_tab_with_retry(
            [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
             "--background", "open", "about:blank"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        tab = None
        try:
            tab = json.loads(opened.stdout)
            if opened.returncode != 0 or tab.get("ok") is not True:
                raise RuntimeError("storefront_reopen_tab_open_failed")
            result = asyncio.run(_reopen_one_suspended_service_async(
                str(tab["ws"]), service_id=service_id))
        finally:
            if isinstance(tab, dict) and tab.get("target_id"):
                subprocess.run(
                    [sys.executable, str(default_tab_script), "--owner", "gig-storefront-direct",
                     "close", str(tab["target_id"])], capture_output=True, text=True,
                    check=False, timeout=30,
                )
        appended = _append_key_once(effects_path, "experiment_key", {
            "version": 1, "status": "accepted", "effect": 1, "effect_class": "reopen",
            "service_id": service_id, "changed_field": "listing_state",
            "experiment_key": f"storefront:v1:{service_id}:listing_state:reopen:{pass_id}",
            "before_value": SUSPENDED_LISTING_STATE, "after_value": result["readback_state"],
            "accepted_at_epoch": int(time.time()), "pass_id": pass_id,
        })
        if appended:
            effect_count += 1
    return effect_count


async def _execute_text_effect_async(
    ws_url: str, *, contract: dict, judgement: dict, public_before_path: Path,
    evidence_dir: Path, state_dir: Path,
) -> tuple[dict, dict, Path]:
    import websockets
    import listing_inventory

    mappings, _ = _load_capability_families(_storefront_paths()["families"])
    _validate_mutation_contract(contract, mappings)
    if contract.get("changed_field") not in {"title", "catchphrase", "body", "price"} or judgement.get("experiment_key") != _experiment_key(
        contract["service_id"], str(contract["changed_field"]), str(contract["proposed_value"]),
    ):
        raise RuntimeError("seller_text_contract_invalid")
    edit_url = f"https://coconala.com/mypage/services/{contract['service_id']}"
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10, max_size=40 * 1024 * 1024) as ws:
        cid = 1
        await listing_inventory._call(ws, "Page.enable", {}, cid); cid += 1
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate", "params": {"url": edit_url}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(ws, asyncio.get_event_loop().time() + 15, cid)

        async def evaluate(expression: str) -> object:
            nonlocal cid
            response = await listing_inventory._call(
                ws, "Runtime.evaluate", {"expression": expression, "returnByValue": True}, cid,
            )
            cid += 1
            return response.get("result", {}).get("result", {}).get("value")

        before = json.loads(str(await evaluate(SELLER_FORM_EXPRESSION) or "{}"))
        field = contract["allowed_delta"][0]
        filled = json.loads(str(await evaluate(
            "JSON.stringify((()=>{const form=document.forms[0],field=" + json.dumps(field)
            + ",before=" + json.dumps(contract["before_value"], ensure_ascii=False)
            + ",proposed=" + json.dumps(contract["proposed_value"], ensure_ascii=False)
            + ";const e=form?.querySelector(`[name=\"${field}\"]`);"
            "if(!e||e.value!==before)return {ok:false,reason:'before_mismatch'};"
            "e.value=proposed;e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));"
            "const submit=form.querySelector('button.submitButton.js_button-edit[type=submit]');"
            "if(!submit)return {ok:false,reason:'submit_missing'};submit.scrollIntoView({block:'center'});"
            "const r=submit.getBoundingClientRect();return {ok:true,value:e.value,"
            "rect:{x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height}}})())"
        ) or "{}"))
        if filled.get("ok") is not True or filled.get("value") != contract["proposed_value"]:
            raise RuntimeError(f"seller_text_fill_failed:{filled.get('reason')}")
        after = json.loads(str(await evaluate(SELLER_FORM_EXPRESSION) or "{}"))
        _validate_text_form_delta(before, after, contract)
        before_path, after_path = evidence_dir / "seller-form-before.json", evidence_dir / "seller-form-text-filled.json"
        _atomic_write(before_path, before); _atomic_write(after_path, after)
        intent_path = _effect_intent_path(state_dir, str(judgement["experiment_key"]))
        _atomic_write(intent_path, {
            "version": 1, "status": "prepared", "service_id": contract["service_id"],
            "changed_field": contract["changed_field"], "experiment_key": judgement["experiment_key"],
            "mutation_contract": contract, "public_before_path": str(public_before_path),
            "seller_form_before_path": str(before_path), "prepared_at_epoch": int(time.time()),
            "effect_origin_pass_id": evidence_dir.name, "judgement": judgement,
        })
        submitted = await evaluate(
            "(()=>{const form=document.forms[0],submit=form?.querySelector("
            "'button.submitButton.js_button-edit[type=submit]');"
            "if(!submit)return false;form.requestSubmit(submit);return true})()"
        )
        if submitted is not True:
            raise RuntimeError("seller_text_submit_missing")
        await asyncio.sleep(3)
        return before, after, intent_path


def _execute_text_effect(**kwargs) -> tuple[dict, dict, Path]:
    return asyncio.run(_execute_text_effect_async(**kwargs))


async def _execute_package_effect_async(
    ws_url: str, *, contract: dict, judgement: dict, public_before_path: Path,
    evidence_dir: Path, state_dir: Path,
) -> tuple[dict, dict, Path]:
    import websockets
    import listing_inventory

    mappings, _ = _load_capability_families(_storefront_paths()["families"])
    _validate_mutation_contract(contract, mappings)
    proposed = contract.get("proposed_value")
    if (contract.get("changed_field") != "package" or contract.get("before_value") != "PACKAGE_ABSENT"
            or contract.get("allowed_delta") != ["data[Option][0]"] or not isinstance(proposed, dict)
            or set(proposed) != {"title", "price_jpy"}
            or judgement.get("experiment_key") != _experiment_key(
                contract["service_id"], "package", proposed,
            )):
        raise RuntimeError("seller_package_contract_invalid")
    edit_url = f"https://coconala.com/mypage/services/{contract['service_id']}"
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10,
                                  max_size=40 * 1024 * 1024) as ws:
        cid = 1
        await listing_inventory._call(ws, "Page.enable", {}, cid); cid += 1
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate", "params": {"url": edit_url}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(
            ws, asyncio.get_event_loop().time() + 15, cid,
        )

        async def evaluate(expression: str) -> object:
            nonlocal cid
            response = await listing_inventory._call(
                ws, "Runtime.evaluate", {"expression": expression, "returnByValue": True}, cid,
            )
            cid += 1
            return response.get("result", {}).get("result", {}).get("value")

        before = json.loads(str(await evaluate(SELLER_FORM_EXPRESSION) or "{}"))
        filled = json.loads(str(await evaluate(
            "JSON.stringify((()=>{const form=document.forms[0];"
            "if(!form||[...form.elements].some(e=>(e.name||'').startsWith('data[Option]')))"
            "return {ok:false,reason:'package_not_absent'};"
            "const add=document.querySelector('#addOption');if(!add)return {ok:false,reason:'add_missing'};"
            "add.click();const title=form.querySelector('[name=\"data[Option][0][title]\"]'),"
            "price=form.querySelector('[name=\"data[Option][0][price]\"]'),"
            "opened=form.querySelector('[name=\"data[Option][0][opened]\"]');"
            "if(!title||!price||!opened)return {ok:false,reason:'controls_missing'};"
            "const proposed=" + json.dumps(proposed, ensure_ascii=False) + ";"
            "if(![...price.options].some(o=>o.value===String(proposed.price_jpy)))"
            "return {ok:false,reason:'price_option_missing'};"
            "title.value=proposed.title;price.value=String(proposed.price_jpy);opened.value='1';"
            "for(const e of [title,price,opened]){e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}))}"
            "const submit=form.querySelector('button.submitButton.js_button-edit[type=submit]');"
            "if(!submit)return {ok:false,reason:'submit_missing'};submit.scrollIntoView({block:'center'});"
            "const r=submit.getBoundingClientRect();return {ok:true,title:title.value,price:price.value,"
            "opened:opened.value,rect:{x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height}}})())"
        ) or "{}"))
        if (filled.get("ok") is not True or filled.get("title") != proposed["title"]
                or filled.get("price") != str(proposed["price_jpy"]) or filled.get("opened") != "1"):
            raise RuntimeError(f"seller_package_fill_failed:{filled.get('reason')}")
        after = json.loads(str(await evaluate(SELLER_FORM_EXPRESSION) or "{}"))
        _validate_package_form_delta(before, after, contract)
        before_path = evidence_dir / "seller-form-before.json"
        after_path = evidence_dir / "seller-form-package-filled.json"
        _atomic_write(before_path, before); _atomic_write(after_path, after)
        intent_path = _effect_intent_path(state_dir, str(judgement["experiment_key"]))
        _atomic_write(intent_path, {
            "version": 1, "status": "prepared", "service_id": contract["service_id"],
            "changed_field": "package", "experiment_key": judgement["experiment_key"],
            "mutation_contract": contract, "public_before_path": str(public_before_path),
            "seller_form_before_path": str(before_path), "prepared_at_epoch": int(time.time()),
            "effect_origin_pass_id": evidence_dir.name, "judgement": judgement,
        })
        submitted = await evaluate(
            "(()=>{const form=document.forms[0],submit=form?.querySelector("
            "'button.submitButton.js_button-edit[type=submit]');"
            "if(!submit)return false;form.requestSubmit(submit);return true})()"
        )
        if submitted is not True:
            raise RuntimeError("seller_package_submit_missing")
        await asyncio.sleep(3)
        return before, after, intent_path


def _execute_package_effect(**kwargs) -> tuple[dict, dict, Path]:
    return asyncio.run(_execute_package_effect_async(**kwargs))


async def _execute_image_effect_async(
    ws_url: str,
    *,
    contract: dict,
    judgement: dict,
    public_before_path: Path,
    evidence_dir: Path,
    state_dir: Path,
) -> tuple[dict, dict, Path]:
    import websockets
    import listing_inventory

    contract = _snapshot_image_contract_assets(contract, state_dir=state_dir)
    edit_url = f"https://coconala.com/mypage/services/{contract['service_id']}"
    async with websockets.connect(ws_url, ping_interval=None, open_timeout=10, max_size=40 * 1024 * 1024) as ws:
        cid = 1
        await listing_inventory._call(ws, "Page.enable", {}, cid); cid += 1
        await listing_inventory._call(ws, "DOM.enable", {}, cid); cid += 1
        await ws.send(json.dumps({"id": cid, "method": "Page.navigate", "params": {"url": edit_url}})); cid += 1
        _, cid = await listing_inventory._wait_for_load(ws, asyncio.get_event_loop().time() + 15, cid)

        async def evaluate(expression: str) -> object:
            nonlocal cid
            response = await listing_inventory._call(
                ws, "Runtime.evaluate", {"expression": expression, "returnByValue": True}, cid,
            )
            cid += 1
            return response.get("result", {}).get("result", {}).get("value")

        before = json.loads(str(await evaluate(SELLER_FORM_EXPRESSION) or "{}"))
        if contract["service_id"] != GALLERY_SERVICE_ID:
            click = json.loads(str(await evaluate(
                "JSON.stringify((()=>{const b=document.querySelector('.js_upload-select');"
                "if(!b)return {ok:false,reason:'image_add_missing'};b.click();return {ok:true}})())"
            ) or "{}"))
            if click.get("ok") is not True:
                raise RuntimeError(f"seller_image_add_failed:{click.get('reason')}")
            await asyncio.sleep(0.5)
            uploads = [("input.js_upload-button", contract["proposed_value"]["asset_path"],
                        contract["proposed_value"]["asset_sha256"])]
        else:
            uploads = []
            for row in contract["proposed_value"]["replacement_assets"]:
                match = re.search(r"-(\d+)\.png$", row["replace_image_id"])
                if match is None:
                    raise RuntimeError("seller_gallery_image_id_invalid")
                uploads.append((f'input[data-service-image-id="{match.group(1)}"]', row["asset_path"],
                                row["asset_sha256"]))
        for selector, asset_path, asset_sha256 in uploads:
            document = await listing_inventory._call(ws, "DOM.getDocument", {"depth": -1, "pierce": True}, cid); cid += 1
            queried = await listing_inventory._call(ws, "DOM.querySelector", {
                "nodeId": document["result"]["root"]["nodeId"], "selector": selector,
            }, cid); cid += 1
            node_id = int(queried.get("result", {}).get("nodeId") or 0)
            if node_id <= 0:
                raise RuntimeError(f"seller_image_file_input_missing:{selector}")
            upload_path = _verified_snapshot_upload_path(asset_path, asset_sha256, state_dir=state_dir)
            await listing_inventory._call(ws, "DOM.setFileInputFiles", {
                "nodeId": node_id, "files": [upload_path],
            }, cid); cid += 1
            await asyncio.sleep(0.75)
        await asyncio.sleep(2)
        after = json.loads(str(await evaluate(SELLER_FORM_EXPRESSION) or "{}"))
        _validate_image_form_delta(before, after, contract, state_dir=state_dir)
        preview = json.loads(str(await evaluate(
            "JSON.stringify((()=>{const p=[...document.querySelectorAll('input[type=file]')]"
            ".filter(input=>input.files&&input.files.length===1);"
            "const s=document.querySelector('button.submitButton.js_button-edit[type=submit]');"
            f"if(p.length<{len(uploads)})return {{ok:false,reason:'preview_missing',count:p.length}};"
            "if(!s)return {ok:false,reason:'submit_missing'};"
            "s.scrollIntoView({block:'center'});const r=s.getBoundingClientRect();"
            "return {ok:true,rect:{x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height}}})())"
        ) or "{}"))
        if preview.get("ok") is not True:
            raise RuntimeError(f"seller_image_preview_failed:{preview.get('reason')}")
        before_path, after_path = evidence_dir / "seller-form-before.json", evidence_dir / "seller-form-image-filled.json"
        _atomic_write(before_path, before)
        _atomic_write(after_path, after)
        intent_path = _effect_intent_path(state_dir, str(judgement["experiment_key"]))
        intent = {
            "version": 1, "status": "prepared", "service_id": contract["service_id"],
            "changed_field": "image", "experiment_key": judgement["experiment_key"],
            "asset_path": contract["proposed_value"].get("asset_path"),
            "asset_sha256": contract["proposed_value"].get("asset_sha256"), "mutation_contract": contract,
            "public_before_path": str(public_before_path), "seller_form_before_path": str(before_path),
            "prepared_at_epoch": int(time.time()), "effect_origin_pass_id": evidence_dir.name,
            "judgement": judgement,
        }
        _atomic_write(intent_path, intent)
        rect = preview["rect"]
        if min(float(rect.get("w") or 0), float(rect.get("h") or 0)) <= 0:
            raise RuntimeError("seller_image_submit_not_visible")
        for event_type in ("mousePressed", "mouseReleased"):
            await listing_inventory._call(ws, "Input.dispatchMouseEvent", {
                "type": event_type, "x": float(rect["x"]), "y": float(rect["y"]),
                "button": "left", "clickCount": 1,
            }, cid)
            cid += 1
        await asyncio.sleep(3)
        return before, after, intent_path


def _execute_image_effect(**kwargs) -> tuple[dict, dict, Path]:
    return asyncio.run(_execute_image_effect_async(**kwargs))


def run_once(args: argparse.Namespace) -> tuple[int, dict]:
    started_at = time.monotonic()
    pass_id = args.pass_id or f"storefront-direct-{time.time_ns()}-{os.getpid()}"
    minimum_epoch = int(time.time())
    args.state_dir.mkdir(parents=True, exist_ok=True)
    output = args.output or args.state_dir / "current.json"
    brake_status = _operator_brake_status(args.operator_brake)
    if brake_status == "failed":
        row = _receipt(pass_id, status="failed", reason="operator_brake_check_failed")
        row = _persist_receipt(args, output, row)
        return 1, row
    if brake_status == "held":
        row = _receipt(pass_id, status="operator_brake", reason="storefront_operator_brake_held")
        row = _persist_receipt(args, output, row)
        return 0, row

    public_bootstrap = not os.environ.get("GIG_STOREFRONT_ROOT", "").strip()
    if not public_bootstrap:
        try:
            _preflight_storefront_bundle()
        except RuntimeError as error:
            row = _receipt(pass_id, status="failed", reason=str(error).strip() or type(error).__name__)
            row = _persist_receipt(args, output, row)
            return 1, row
    if getattr(args, "auto_cadence", False):
        try:
            args.incremental = _auto_cadence_is_incremental(
                args.state_dir, minimum_epoch, int(args.full_interval_seconds),
            )
        except RuntimeError as error:
            row = _receipt(pass_id, status="failed", reason=str(error).strip() or type(error).__name__)
            row = _persist_receipt(args, output, row)
            return 1, row

    lock_path = args.state_dir / "owner.lock"
    with lock_path.open("a+", encoding="utf-8") as owner_lock:
        try:
            fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            row = _receipt(pass_id, status="busy", reason="storefront_owner_busy")
            row = _persist_receipt(args, output, row)
            return 0, row

        lease = None
        released = False
        try:
            _collect_storefront_evidence(
                args.state_dir, args.state_dir / "evidence" / pass_id,
            )
            browser = subprocess.run(
                ["/bin/bash", str(args.ensure_browser_script)],
                capture_output=True, text=True, check=False, timeout=60,
            )
            if browser.returncode != 0 or browser.stdout.strip() not in {"ALIVE", "RECOVERED"}:
                detail = (browser.stdout.strip() or browser.stderr.strip() or "unknown")[:200]
                raise RuntimeError(f"storefront_browser_unavailable:{detail}")
            # The owner lock proves no current storefront wake owns these tabs. Reclaim only
            # this loop's leftovers from a prior crash; cleanup failure must not stop earning.
            # A slow browser under load can make this best-effort call miss its own subprocess
            # timeout, and a raised TimeoutExpired here used to abort the whole wake even though
            # the comment above already says cleanup failure must not stop earning.
            try:
                subprocess.run(
                    [sys.executable, str(getattr(args, "default_tab_script", DEFAULT_TAB)),
                     "--owner", "gig-storefront-direct", "close-owned"],
                    capture_output=True, text=True, check=False, timeout=30,
                )
            except subprocess.SubprocessError:
                pass
            task = f"gig-storefront-direct-{pass_id}"
            lease = _lease(args.lease_script, "acquire", task)
            ws_url = str(lease.get("ws") or "")
            if not ws_url.startswith("ws://"):
                raise RuntimeError("lease_ws_invalid")
            import listing_inventory

            inventory_path = args.state_dir / "evidence" / pass_id / "official-inventory.json"
            def _read_official_catalog() -> tuple[dict, list, int]:
                """Read and validate the official catalogue, retrying a partial dashboard.

                A half-hydrated seller dashboard is a transient, not a catalogue change, and
                failing the whole wake on it costs a decision cycle for nothing.
                """
                failure = "official_inventory_empty_or_invalid"
                for attempt in range(5):
                    read = listing_inventory.observe_storefront(
                        output_path=inventory_path, ws_url=ws_url, include_contract_sources=True,
                    )
                    count = int(read.get("service_count") or 0)
                    sources = read.get("_contract_sources")
                    if (public_bootstrap and count == 0
                            and read.get("services") == [] and sources == []):
                        return read, [], 0
                    if count <= 0 or count != len(read.get("services") or []):
                        # An empty shelf and a login wall look identical from the parsed
                        # count, and calling both "empty" is how a dead session went
                        # unreported for seven hours: 241 wakes read a shelf that was really
                        # a login page, while the Apply lane -- which names this failure --
                        # recovered on its own next wake. Ask the page which one it is.
                        failure = ("storefront_session_expired"
                                   if _dashboard_says_signed_out(ws_url)
                                   else "official_inventory_empty_or_invalid")
                        if failure == "storefront_session_expired":
                            # Detecting the expiry was only half of it. session_vault already
                            # knows how to log this account back in, banks the result to the
                            # vault the lanes seed from, and rate-limits itself -- and nothing
                            # called it, so 241 wakes reported a dead session and waited for a
                            # human. Ask it once; its own cooldown decides whether to try.
                            recovered = _relogin_coconala()
                            failure = f"{failure}:{recovered}"
                            break
                    else:
                        ids = [source.get("service_id") for source in sources] if isinstance(
                            sources, list) and all(isinstance(s, dict) for s in sources) else None
                        listed = {str(service.get("service_id")) for service in read["services"]
                                  if isinstance(service, dict)}
                        if (ids is not None and len(sources) == count
                                and all(type(value) is str and value.isdigit() for value in ids)
                                and len(set(ids)) == len(ids) and set(ids) == listed):
                            return read, sources, count
                        failure = "official_service_contract_invalid"
                    if attempt < 4:
                        time.sleep(3)
                raise RuntimeError(failure)

            inventory, contract_sources, observed = _read_official_catalog()
            if public_bootstrap:
                from storefront_bootstrap import (
                    compose_listing, import_catalog, inventory as bootstrap_inventory,
                    select_capability,
                )
                capability_inventory = bootstrap_inventory()
                capability_path = args.state_dir / "storefront-capabilities.json"
                _atomic_write(capability_path, capability_inventory)
                public_contract = None
                public_replay = None
                public_contract_path = args.state_dir / "storefront-bootstrap-contract.json"
                if observed > 0 and public_contract_path.exists():
                    public_contract = json.loads(public_contract_path.read_text(encoding="utf-8"))
                    created_id = str(public_contract.get("draft_service_id") or "")
                    source = next(
                        (row for row in contract_sources
                         if isinstance(row, dict) and str(row.get("service_id") or "") == created_id),
                        None,
                    )
                    expected = public_contract.get("public_fields") or {}
                    if (not isinstance(source, dict)
                            or source.get("public_url") != public_contract.get("expected_public_url")
                            or source.get("title") != expected.get("expected_title")
                            or source.get("price_jpy") != expected.get("display_price_jpy")):
                        raise RuntimeError("storefront_bootstrap_public_replay_mismatch")
                    public_replay = {
                        "version": 1,
                        "candidate_key": public_contract["candidate_key"],
                        "contract_sha256": public_contract["contract_sha256"],
                        "draft_service_id": created_id,
                        "status": "already_public", "effect": 0, "public_effect": 0,
                        "readback": 1, "duplicate": 0,
                        "public_url": public_contract["expected_public_url"],
                    }
                    from coconala_onboarding import record as record_onboarding
                    replay_evidence = hashlib.sha256(
                        json.dumps(public_replay, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
                    record_onboarding(Path.home(), "storefront_listing_readback", replay_evidence)
                import_record = None
                if observed > 0 and public_contract is None:
                    import_path = args.state_dir / "storefront-import.json"
                    catalog_sha = str(inventory.get("content_sha256") or "")
                    if import_path.exists():
                        try:
                            candidate = json.loads(import_path.read_text(encoding="utf-8"))
                            if (candidate.get("version") == 1
                                    and candidate.get("inventory_sha256") == capability_inventory["inventory_sha256"]
                                    and candidate.get("catalog_sha256") == catalog_sha
                                    and isinstance(candidate.get("mappings"), list)):
                                import_record = candidate
                        except (OSError, json.JSONDecodeError):
                            pass
                    if import_record is None:
                        mapped = import_catalog(
                            sources=contract_sources, capabilities=capability_inventory,
                            runner=getattr(args, "runner", DEFAULT_RUNNER),
                            schema=getattr(args, "bootstrap_import_schema", DEFAULT_BOOTSTRAP_IMPORT_SCHEMA),
                            evidence_dir=inventory_path.parent / "bootstrap-import-agent",
                            workdir=args.workdir, timeout_seconds=args.timeout_seconds,
                        )
                        import_record = {
                            **mapped,
                            "inventory_sha256": capability_inventory["inventory_sha256"],
                            "catalog_sha256": catalog_sha,
                        }
                        _atomic_write(import_path, import_record)
                import_complete = bool(import_record) and all(
                    row.get("supported") is True for row in import_record.get("mappings", [])
                )
                selection_path = args.state_dir / "storefront-bootstrap-selection.json"
                rejection_path = args.state_dir / "storefront-bootstrap-rejections.jsonl"
                rejected_rows = _jsonl_rows(rejection_path)[0] if rejection_path.exists() else []
                rejected_pairs = {
                    (row.get("skill_path"), row.get("service_query")) for row in rejected_rows
                    if isinstance(row, dict)
                }
                selection_record = None
                if observed == 0 and selection_path.exists():
                    try:
                        candidate = json.loads(selection_path.read_text(encoding="utf-8"))
                        if (candidate.get("version") == 1
                                and candidate.get("inventory_sha256") == capability_inventory["inventory_sha256"]
                                and isinstance(candidate.get("selection"), dict)
                                and (candidate["selection"].get("skill_path"),
                                     candidate["selection"].get("service_query")) not in rejected_pairs):
                            selection_record = candidate
                    except (OSError, json.JSONDecodeError):
                        pass
                if observed == 0 and selection_record is None:
                    selection = select_capability(
                        capability_inventory,
                        runner=getattr(args, "runner", DEFAULT_RUNNER),
                        schema=getattr(args, "bootstrap_selection_schema", DEFAULT_BOOTSTRAP_SELECTION_SCHEMA),
                        evidence_dir=inventory_path.parent / "bootstrap-selection-agent",
                        workdir=args.workdir,
                        timeout_seconds=args.timeout_seconds,
                        rejected=rejected_rows,
                    )
                    selection_record = {
                        "version": 1,
                        "inventory_sha256": capability_inventory["inventory_sha256"],
                        "selection": selection,
                    }
                    _atomic_write(selection_path, selection_record)
                demand_record = None
                selection = (selection_record or {}).get("selection") or {}
                if observed == 0 and selection.get("decision") == "sell":
                    demand_path = args.state_dir / "storefront-bootstrap-demand.json"
                    if demand_path.exists():
                        try:
                            candidate = json.loads(demand_path.read_text(encoding="utf-8"))
                            if (candidate.get("version") == 1
                                    and candidate.get("inventory_sha256") == capability_inventory["inventory_sha256"]
                                    and candidate.get("skill_path") == selection.get("skill_path")
                                    and candidate.get("query") == selection.get("service_query")):
                                demand_record = candidate
                        except (OSError, json.JSONDecodeError):
                            pass
                    if demand_record is None:
                        # Left uncaught on purpose: this is the one query the bootstrap
                        # wake selected to decide whether to sell that capability at
                        # all, not one candidate among several, so a tab that will not
                        # open, a redirect off coconala.com, or an empty read after the
                        # house retry inside _crawl_demand_cluster IS the whole wake's
                        # answer -- there is no "rest of the batch" to fall back to.
                        cluster = _crawl_demand_cluster(
                            getattr(args, "default_tab_script", DEFAULT_TAB),
                            inventory_path.parent / "bootstrap-demand",
                            str(selection["service_query"]),
                        )
                        score = _score_demand_cluster(cluster)
                        demand_record = {
                            "version": 1,
                            "inventory_sha256": capability_inventory["inventory_sha256"],
                            "skill_path": selection["skill_path"],
                            "query": selection["service_query"],
                            "search_url": cluster.get("search_url"),
                            "evidence_path": str(cluster.get("evidence_path") or ""),
                            "cluster": cluster,
                            "evidence_sha256": hashlib.sha256(
                                json.dumps(cluster, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")
                            ).hexdigest(),
                            "score": score,
                        }
                        _atomic_write(demand_path, demand_record)
                    if (demand_record.get("score") or {}).get("status") == "known" and int(
                            (demand_record.get("score") or {}).get("score") or 0) <= 0:
                        rejection_key = hashlib.sha256(
                            f"{selection['skill_path']}:{selection['service_query']}".encode("utf-8")
                        ).hexdigest()
                        _append_key_once(rejection_path, "rejection_key", {
                            "version": 1, "rejection_key": rejection_key,
                            "skill_path": selection["skill_path"],
                            "service_query": selection["service_query"],
                            "demand_evidence_sha256": demand_record["evidence_sha256"],
                            "reason": "official_demand_score_zero",
                            "observed_at_epoch": int(time.time()),
                        })
                category_record = None
                demand_score = (demand_record or {}).get("score") or {}
                if (observed == 0 and args.effect and demand_score.get("status") == "known"
                        and int(demand_score.get("score") or 0) > 0):
                    category_path = args.state_dir / "storefront-bootstrap-category.json"
                    if category_path.exists():
                        try:
                            candidate = json.loads(category_path.read_text(encoding="utf-8"))
                            if (candidate.get("version") == 1
                                    and candidate.get("demand_evidence_sha256") == demand_record.get("evidence_sha256")
                                    and str(candidate.get("draft_service_id") or "").isdigit()):
                                category_record = candidate
                        except (OSError, json.JSONDecodeError):
                            pass
                    if category_record is None:
                        import storefront_draft
                        draft = storefront_draft.create_or_claim_blank_draft(
                            getattr(args, "default_tab_script", DEFAULT_TAB)
                        )
                        draft_id = str(draft["draft_service_id"])
                        seller = _seller_snapshot_from_fresh_tab(
                            getattr(args, "default_tab_script", DEFAULT_TAB), draft_id,
                        )
                        cluster = {**demand_record["cluster"],
                                   "capability_family": selection.get("skill_path")}
                        master_options = (seller.get("select_options") or {}).get(
                            "data[Service][master_category]", [])
                        choice, master_route = _invoke_category_proposal(
                            runner=getattr(args, "runner", DEFAULT_RUNNER),
                            schema=getattr(args, "category_proposal_schema", DEFAULT_CATEGORY_PROPOSAL_SCHEMA),
                            workdir=args.workdir,
                            evidence_dir=inventory_path.parent / "bootstrap-category-master",
                            cluster=cluster, options=master_options,
                            timeout_seconds=args.timeout_seconds,
                        )
                        if choice.get("decision") != "choose":
                            raise RuntimeError("storefront_bootstrap_category_noop")
                        master = _validate_category_choice(
                            choice.get("master_category_value"), master_options, "master")
                        children = storefront_draft.read_category_children(
                            getattr(args, "default_tab_script", DEFAULT_TAB), draft_id, master["value"])
                        picked, child_route = _invoke_category_child_proposal(
                            runner=getattr(args, "runner", DEFAULT_RUNNER),
                            schema=getattr(args, "category_child_schema", DEFAULT_CATEGORY_CHILD_SCHEMA),
                            workdir=args.workdir,
                            evidence_dir=inventory_path.parent / "bootstrap-category-child",
                            cluster=cluster, master=master, children=children,
                            timeout_seconds=args.timeout_seconds,
                        )
                        sub = _validate_category_choice(
                            picked.get("sub_value"),
                            children.get("data[Service][master_sub_category]") or [], "sub")
                        typed = storefront_draft.read_category_children(
                            getattr(args, "default_tab_script", DEFAULT_TAB),
                            draft_id, master["value"], sub["value"])
                        # A disabled, empty type select (see `read_category_children`) means this
                        # category genuinely has no third level -- skip the type-picking agent
                        # call entirely rather than let it fabricate a value with no real options.
                        category_type_absent = bool(typed.get("master_category_type_absent"))
                        if category_type_absent:
                            category_type, type_route = None, {"model": None}
                        else:
                            type_options = typed.get("data[Service][master_category_type_id]") or []
                            picked_type, type_route = _invoke_category_child_proposal(
                                runner=getattr(args, "runner", DEFAULT_RUNNER),
                                schema=getattr(args, "category_child_schema", DEFAULT_CATEGORY_CHILD_SCHEMA),
                                workdir=args.workdir,
                                evidence_dir=inventory_path.parent / "bootstrap-category-type",
                                cluster=cluster, master=master,
                                children={**typed, "data[Service][master_sub_category]": [sub]},
                                timeout_seconds=args.timeout_seconds,
                            )
                            category_type = _validate_category_choice(
                                picked_type.get("type_value"), type_options, "type")
                        category_record = {
                            "version": 1,
                            "demand_evidence_sha256": demand_record["evidence_sha256"],
                            "draft_service_id": draft_id,
                            "draft_effect": int(draft.get("effect") or 0),
                            "category": {
                                "master": master,
                                "sub": sub,
                                "type": category_type,
                            },
                            "category_type_absent": category_type_absent,
                            "routes": {
                                "master": master_route.get("model"),
                                "sub": child_route.get("model"),
                                "type": type_route.get("model"),
                            },
                        }
                        _atomic_write(category_path, category_record)
                bootstrap_contract = public_contract
                bootstrap_result = public_replay
                if observed == 0 and category_record is not None:
                    import storefront_draft
                    contract_path = args.state_dir / "storefront-bootstrap-contract.json"
                    if contract_path.exists():
                        try:
                            candidate = json.loads(contract_path.read_text(encoding="utf-8"))
                            if (candidate.get("version") == 1
                                    and candidate.get("draft_service_id") == category_record.get("draft_service_id")
                                    and (candidate.get("demand_evidence") or {}).get("evidence_sha256")
                                    == demand_record.get("evidence_sha256")
                                    and str(candidate.get("contract_sha256") or "")):
                                bootstrap_contract = candidate
                        except (OSError, json.JSONDecodeError):
                            pass
                    if bootstrap_contract is None:
                        form_snapshot = storefront_draft.read_category_form(
                            getattr(args, "default_tab_script", DEFAULT_TAB),
                            str(category_record["draft_service_id"]), category_record["category"],
                        )
                        proposal, official_form = compose_listing(
                            selection=selection, demand=demand_record,
                            category=category_record["category"], form_snapshot=form_snapshot,
                            runner=getattr(args, "runner", DEFAULT_RUNNER),
                            schema=getattr(args, "bootstrap_listing_schema", DEFAULT_BOOTSTRAP_LISTING_SCHEMA),
                            evidence_dir=inventory_path.parent / "bootstrap-listing-agent",
                            workdir=args.workdir, timeout_seconds=args.timeout_seconds,
                        )
                        if proposal.get("decision") != "create":
                            raise RuntimeError("storefront_bootstrap_listing_noop")
                        bootstrap_contract = _seal_bootstrap_contract(
                            proposal, selection=selection, demand=demand_record,
                            category_record=category_record, official_form=official_form,
                            draft_service_id=str(category_record["draft_service_id"]),
                            evidence_dir=inventory_path.parent / "bootstrap-contract",
                        )
                        _atomic_write(contract_path, bootstrap_contract)
                    ledger_path = args.state_dir / "new-listing-drafts.jsonl"
                    prior = next(
                        (row for row in reversed(_jsonl_rows(ledger_path)[0])
                         if row.get("candidate_key") == bootstrap_contract["candidate_key"]
                         and row.get("status") in {"published", "already_public"}),
                        None,
                    ) if ledger_path.exists() else None
                    if prior is not None:
                        bootstrap_result = storefront_draft.readback_published_draft(
                            bootstrap_contract,
                            getattr(args, "default_tab_script", DEFAULT_TAB),
                            inventory_path.parent / "bootstrap-public-readback",
                            known_image_identity=prior.get("public_image_identity"),
                        )
                    else:
                        try:
                            bootstrap_result = storefront_draft.readback_published_draft(
                                bootstrap_contract,
                                getattr(args, "default_tab_script", DEFAULT_TAB),
                                inventory_path.parent / "bootstrap-public-readback",
                            )
                        except RuntimeError:
                            storefront_draft.prepare_draft(
                                bootstrap_contract,
                                getattr(args, "default_tab_script", DEFAULT_TAB),
                                inventory_path.parent / "bootstrap-draft",
                            )
                            bootstrap_result = storefront_draft.publish_draft(
                                bootstrap_contract,
                                getattr(args, "default_tab_script", DEFAULT_TAB),
                                inventory_path.parent / "bootstrap-public-readback",
                            )
                    _append_key_once(ledger_path, "candidate_key", {
                        **bootstrap_result,
                        "capability_family": selection["skill_path"],
                        "demand_evidence_path": demand_record["evidence_path"],
                        "contract_path": "storefront-bootstrap-contract.json",
                    })
                    if int(bootstrap_result.get("readback") or 0) == 1:
                        from coconala_onboarding import record as record_onboarding
                        listing_evidence = hashlib.sha256(
                            json.dumps(bootstrap_result, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")
                        ).hexdigest()
                        record_onboarding(Path.home(), "storefront_listing_readback", listing_evidence)
                release = _lease(args.lease_script, "release", task, lease)
                released = release.get("released") == task
                if not released:
                    raise RuntimeError("lease_release_unproven")
                row = _receipt(
                    pass_id, status="completed",
                    reason=("storefront_bootstrap_published" if bootstrap_result is not None
                            and bootstrap_result.get("status") == "published"
                            else "storefront_bootstrap_readback" if bootstrap_result is not None
                            else "storefront_imported" if import_complete
                            else "storefront_import_blocked" if import_record is not None
                            else "storefront_bootstrap_required" if observed == 0
                            else "storefront_import_required"),
                    official_services_read=observed,
                    actionable=int((bootstrap_result or {}).get("status") == "published"),
                    effect=int((bootstrap_result or {}).get("public_effect") or 0),
                    readback=max(int((bootstrap_result or {}).get("readback") or 0), int(import_complete)),
                    duplicate=0,
                    pending=(0 if import_complete or (bootstrap_result is not None
                             and int(bootstrap_result.get("readback") or 0) == 1) else 1),
                    capability_inventory_count=len(capability_inventory["skills"]),
                    capability_inventory_sha256=capability_inventory["inventory_sha256"],
                    capability_inventory_path="storefront-capabilities.json",
                    bootstrap_selection=(selection_record or {}).get("selection"),
                    bootstrap_selection_path=("storefront-bootstrap-selection.json"
                                              if selection_record is not None else None),
                    bootstrap_demand=({
                        "query": demand_record.get("query"),
                        "search_url": demand_record.get("search_url"),
                        "evidence_sha256": demand_record.get("evidence_sha256"),
                        "score": demand_record.get("score"),
                    } if demand_record is not None else None),
                    bootstrap_demand_path=("storefront-bootstrap-demand.json"
                                           if demand_record is not None else None),
                    bootstrap_category=category_record,
                    bootstrap_category_path=("storefront-bootstrap-category.json"
                                             if category_record is not None else None),
                    bootstrap_contract_sha256=(bootstrap_contract or {}).get("contract_sha256"),
                    bootstrap_contract_path=("storefront-bootstrap-contract.json"
                                             if bootstrap_contract is not None else None),
                    new_listing_draft=bootstrap_result,
                    imported_listings=({
                        "count": len(import_record.get("mappings", [])),
                        "supported": sum(row.get("supported") is True
                                         for row in import_record.get("mappings", [])),
                        "catalog_sha256": import_record.get("catalog_sha256"),
                    } if import_record is not None else None),
                )
                row = _persist_receipt(args, output, row)
                return 0, row
            source_dicts = True
            source_ids = [source.get("service_id") for source in contract_sources]
            if source_dicts:
                # Kept out of the hashed catalogue payload on purpose: this is adapter input,
                # not listing identity.
                _atomic_write(inventory_path.parent / "listing-state-controls.json", {
                    "version": 1,
                    "observed_at": inventory.get("observed_at"),
                    "list_page_tabs": getattr(listing_inventory, "LAST_PAGE_TABS", []),
                    "page_walk": getattr(listing_inventory, "PAGE_WALK", []),
                    "controls": {str(source.get("service_id")): source.get("state_controls") or []
                                 for source in contract_sources},
                })
            inventory_ids = {
                str(service.get("service_id"))
                for service in inventory["services"]
                if isinstance(service, dict)
            }
            if (
                not source_dicts
                or len(contract_sources) != observed
                or not all(type(service_id) is str and service_id.isdigit() for service_id in source_ids)
                or len(set(source_ids)) != len(source_ids)
                or set(source_ids) != inventory_ids
            ):
                raise RuntimeError("official_service_contract_invalid")
            expected_catalog_sha256 = str(
                getattr(args, "expected_catalog_sha256", "") or ""
            ).strip().lower()
            if expected_catalog_sha256 and (
                not re.fullmatch(r"[0-9a-f]{64}", expected_catalog_sha256)
                or str(inventory.get("content_sha256") or "").lower() != expected_catalog_sha256
            ):
                raise RuntimeError("official_catalog_version_stale")
            validated_contracts = [
                _service_contract(source, str(inventory["observed_at"])) for source in contract_sources
            ]
            if getattr(args, "incremental", False):
                cutoff = int(getattr(args, "accounting_cutoff_epoch", 0) or time.time())
                if cutoff <= 0:
                    raise RuntimeError("incremental_cutoff_invalid")
                listing_contracts = _load_listing_contracts(
                    getattr(args, "listing_contract_dir", DEFAULT_LISTING_CONTRACT_DIR), validated_contracts,
                    getattr(args, "listing_contract_families", DEFAULT_LISTING_CONTRACT_FAMILIES),
                    args.state_dir / "new-listing-drafts.jsonl",
                )
                funnel = _join_funnel(
                    args.state_dir, validated_contracts,
                    getattr(args, "reply_transcripts", DEFAULT_REPLY_TRANSCRIPTS),
                    getattr(args, "applied", DEFAULT_APPLIED), getattr(args, "earnings", DEFAULT_EARNINGS),
                    getattr(args, "projects_dir", DEFAULT_PROJECTS), cutoff,
                    getattr(args, "negotiate_run_log", DEFAULT_NEGOTIATE_RUN_LOG),
                )
                portfolio = _allocate_portfolio(
                    args.state_dir, validated_contracts, funnel,
                    getattr(args, "scorecard", DEFAULT_SCORECARD), cutoff,
                )
                catalog_analytics = _last_known_good_catalog_analytics(
                    args.state_dir, source_ids, cutoff,
                )
                catalog_baseline = _catalog_conversion_baseline(
                    args.state_dir / "analytics.jsonl", validated_contracts,
                )
                competitor_evidence = _last_full_competitor_evidence(args.state_dir, cutoff)
                contract_count = sum(int(_append_contract_once(
                    args.state_dir / "offer-contracts.jsonl", contract,
                )) for contract in validated_contracts)
                listing_contract_count = sum(int(_append_key_once(
                    args.state_dir / "listing-contracts.jsonl", "contract_key", contract,
                )) for contract in listing_contracts)
                release = _lease(args.lease_script, "release", task, lease)
                released = release.get("released") == task
                if not released:
                    raise RuntimeError("lease_release_unproven")
                analytics_epoch = int((catalog_analytics or {}).get("observed_at_epoch") or 0)
                row = _receipt(
                    pass_id, status="completed", reason="incremental_catalog_funnel_readback",
                    official_services_read=observed, competitor_evidence_count=None,
                    competitor_evidence=competitor_evidence,
                    offer_contracts_appended=contract_count,
                    listing_contracts_appended=listing_contract_count,
                    listing_contracts_active=len(listing_contracts),
                    listing_contracts_total=sum(1 for line in
                        (args.state_dir / "listing-contracts.jsonl").read_text(encoding="utf-8").splitlines()
                        if line.strip()),
                    inventory_content_sha256=inventory.get("content_sha256"),
                    incremental_public_readback=1, catalog_analytics=catalog_analytics,
                    catalog_conversion_baseline=catalog_baseline,
                    funnel=funnel, portfolio=portfolio,
                    new_listing_draft={"status": "not_checked_incremental", "effect": 0,
                                       "readback": 0, "public_effect": 0},
                    accounting={"cutoff_epoch": cutoff, "minute": cutoff // 60,
                                "hour": cutoff // 3600, "day": cutoff // 86400,
                                "analytics_observed_at_epoch": analytics_epoch},
                    runtime_seconds=round(time.monotonic() - started_at, 3),
                    lease={"task": task, "context_id": lease.get("context_id"),
                           "target_id": lease.get("target_id"), "generation": lease.get("generation"),
                           "released": True},
                )
                row = _persist_receipt(args, output, row)
                return 0, row
            if args.effect:
                blocked = _persist_effect_block(args, output, pass_id, "before_reopen_effect")
                if blocked is not None:
                    return 0, blocked
                _reopen_suspended_listings(
                    inventory["services"],
                    default_tab_script=getattr(args, "default_tab_script", DEFAULT_TAB),
                    effects_path=args.state_dir / "effects.jsonl", pass_id=pass_id,
                )
            capability_families, capability_templates = _load_capability_families(
                getattr(args, "listing_contract_families", DEFAULT_LISTING_CONTRACT_FAMILIES),
            )
            from storefront_bootstrap import inventory as bootstrap_inventory
            public_capability_inventory = bootstrap_inventory()
            capability_templates = _market_capability_templates(
                capability_templates, public_capability_inventory,
            )
            capability_templates = _catalog_capability_templates(
                capability_templates, _load_catalog_entries(),
            )
            presentation_snapshot = _seller_snapshot_for(ws_url, PRESENTATION_SERVICE_ID)
            scope_snapshot = _seller_snapshot_for(ws_url, SCOPE_SERVICE_ID)
            # Retained so the listing-state adapter can bind the real seller submit controls.
            _atomic_write(inventory_path.parent / f"seller-form-{PRESENTATION_SERVICE_ID}.json", presentation_snapshot)
            _atomic_write(inventory_path.parent / f"seller-form-{SCOPE_SERVICE_ID}.json", scope_snapshot)
            title_render = _render_prepared_mutation(
                args.state_dir, presentation_snapshot, PRESENTATION_SERVICE_ID, "title", capability_families,
            ) or _render_text_mutation(
                getattr(args, "title_mutation", DEFAULT_TITLE_MUTATION), validated_contracts,
                presentation_snapshot, capability_families,
            )
            body_render = _render_text_mutation(
                getattr(args, "body_mutation", DEFAULT_BODY_MUTATION), validated_contracts,
                presentation_snapshot, capability_families,
            )
            scope_render = _render_prepared_mutation(
                args.state_dir, scope_snapshot, SCOPE_SERVICE_ID, "body", capability_families,
            ) or _render_text_mutation(
                getattr(args, "scope_mutation", DEFAULT_SCOPE_MUTATION), validated_contracts,
                scope_snapshot, capability_families,
            )
            package_render = _render_text_mutation(
                getattr(args, "package_mutation", DEFAULT_PACKAGE_MUTATION), validated_contracts,
                presentation_snapshot, capability_families,
            )
            faq_render = _render_text_mutation(
                getattr(args, "faq_mutation", DEFAULT_FAQ_MUTATION), validated_contracts,
                presentation_snapshot, capability_families,
            )
            price_render = _render_text_mutation(
                getattr(args, "price_mutation", DEFAULT_PRICE_MUTATION), validated_contracts,
                presentation_snapshot, capability_families,
            )
            title_render_path = inventory_path.parent / "mutation-render-title.json"
            body_render_path = inventory_path.parent / "mutation-render-body.json"
            scope_render_path = inventory_path.parent / "mutation-render-scope.json"
            package_render_path = inventory_path.parent / "mutation-render-package.json"
            faq_render_path = inventory_path.parent / "mutation-render-faq.json"
            price_render_path = inventory_path.parent / "mutation-render-price.json"
            _atomic_write(title_render_path, title_render)
            _atomic_write(body_render_path, body_render)
            _atomic_write(scope_render_path, scope_render)
            _atomic_write(package_render_path, package_render)
            _atomic_write(faq_render_path, faq_render)
            _atomic_write(price_render_path, price_render)
            listing_contracts = _load_listing_contracts(
                getattr(args, "listing_contract_dir", DEFAULT_LISTING_CONTRACT_DIR), validated_contracts,
                getattr(args, "listing_contract_families", DEFAULT_LISTING_CONTRACT_FAMILIES),
                args.state_dir / "new-listing-drafts.jsonl",
            )
            competitor_manifest = _collect_competitors(
                ws_url,
                inventory_path.parent,
                {str(row.get("service_id")) for row in inventory["services"]},
            )
            own_page = _observe_own_page(ws_url, inventory_path.parent)
            image_asset = _load_image_contract(getattr(args, "image_contract", DEFAULT_IMAGE_CONTRACT))
            image_render = _render_image_mutation(own_page, image_asset, capability_families)
            image_render_path = inventory_path.parent / "mutation-render-image.json"
            if image_render is not None:
                _atomic_write(image_render_path, image_render)
            gallery_asset = _load_gallery_contract(
                getattr(args, "gallery_contract", DEFAULT_GALLERY_CONTRACT)
            )
            expected_gallery_images = len(gallery_asset["kept_image_ids"]) + len(
                gallery_asset["replacements"]
            )
            gallery_page = _observe_own_page(
                ws_url, inventory_path.parent, "own-gallery-candidate.json", GALLERY_SERVICE_ID,
                expected_image_count=expected_gallery_images,
            )
            gallery_source = next(
                (row for row in validated_contracts if row["service_id"] == GALLERY_SERVICE_ID), None
            )
            if gallery_source is None:
                raise RuntimeError("storefront_gallery_offer_contract_missing")
            gallery_render = (_render_gallery_mutation(
                gallery_page, gallery_source["service_version_sha256"],
                gallery_asset, capability_families,
            ) if gallery_page.get("service_image_ids") == gallery_asset["before_image_ids"]
                else _render_published_gallery_mutation(args.state_dir, gallery_page))
            gallery_render_path = inventory_path.parent / "mutation-render-gallery.json"
            _atomic_write(gallery_render_path, gallery_render)
            mutation_contracts = [render["contract"] for render in (
                image_render, gallery_render, title_render, body_render, scope_render,
                package_render, faq_render, price_render,
            ) if render is not None]
            analytics = _collect_analytics(
                args.state_dir, inventory_path.parent, int(time.time()), sorted(inventory_ids),
                getattr(args, "default_tab_script", DEFAULT_TAB),
            )
            # A listing the loop created carries its family in the drafts ledger rather than in
            # the committed config, and a listing with no family is invisible to the duplicate
            # check — which is how the second Excel listing stayed unnoticed.
            scan_families = dict(capability_families)
            draft_ledger = args.state_dir / "new-listing-drafts.jsonl"
            if draft_ledger.exists():
                for line in draft_ledger.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    created = json.loads(line)
                    if (created.get("status") == "published"
                            and isinstance(created.get("capability_family"), str)):
                        scan_families.setdefault(str(created.get("draft_service_id") or ""),
                                                 created["capability_family"])
            # A listing whose family now promises something else is selling the old promise until
            # its body is rewritten, so it is queued the same way a rule breach is.
            offer_refresh = []
            advertised_path = args.state_dir / "advertised-offers.json"
            try:
                advertised_offers = set(json.loads(
                    advertised_path.read_text(encoding="utf-8"))["digests"])
            except (OSError, ValueError, KeyError, TypeError):
                advertised_offers = set()
            for row in validated_contracts:
                # The service-to-family mapping is the authoritative one; the contract row does
                # not always carry it, and an empty name silently digests an empty family.
                family_name = str(capability_families.get(str(row["service_id"])) or "")
                template = capability_templates.get(family_name)
                if not (isinstance(template, dict) and template):
                    continue
                # The body carries the promise and the title is what search shows, so a listing
                # whose offer moved needs both; the body goes first and the title follows it.
                for offer_field in ("body", "title", "catchphrase"):
                    digest = _offer_refresh_due(
                        args.state_dir / "effects.jsonl", str(row["service_id"]), family_name,
                        template, advertised_offers, offer_field)
                    if digest:
                        offer_refresh.append({"service_id": str(row["service_id"]),
                                              "offer_digest": digest, "family": family_name,
                                              "offer_field": offer_field})
                        break
            # Drafts are the cards whose link points at the seller page rather than a public one.
            draft_ids = sorted({
                match.group(1)
                for row in getattr(listing_inventory, "PAGE_WALK", [])
                for href in row.get("raw_hrefs") or []
                for match in [re.fullmatch(r"/mypage/services/(\d+)", str(href))] if match
            })
            _observe_draft_controls(inventory_path.parent, draft_ids,
                                    getattr(args, "default_tab_script", DEFAULT_TAB))
            # One draft per wake: a deletion cannot be undone, so the next wake's own card census
            # is what confirms the previous one rather than a claim made in the same breath.
            deletable = _deletable_drafts(args.state_dir / "new-listing-drafts.jsonl", draft_ids)
            if args.effect and deletable:
                try:
                    _delete_one_draft(inventory_path.parent, deletable[0],
                                      getattr(args, "default_tab_script", DEFAULT_TAB))
                except Exception as error:  # cleanup never ends a wake
                    _atomic_write(inventory_path.parent / "draft-delete-error.json",
                                  {"error": f"{type(error).__name__}:{str(error)[:140]}"})
            unread_traffic = _traffic_without_inquiries(
                args.state_dir / "analytics.jsonl", args.state_dir / "funnel-events.jsonl")
            compliance_violations, duplicate_listings = _scan_public_copy(
                args.state_dir, inventory_path.parent, int(time.time()), sorted(inventory_ids),
                getattr(args, "default_tab_script", DEFAULT_TAB), scan_families,
                price_by_service_id={str(row["service_id"]): row.get("price_jpy")
                                     for row in validated_contracts},
                runner=getattr(args, "runner", DEFAULT_RUNNER),
                duplicate_judgement_schema=getattr(
                    args, "duplicate_judgement_schema", DEFAULT_DUPLICATE_JUDGEMENT_SCHEMA),
                workdir=args.workdir, timeout_seconds=args.timeout_seconds,
            )
            # Rewriting a listing that is about to come down is work nobody reads, and because
            # retirement only runs on a wake that changed nothing else, the rewrite would keep
            # postponing it forever.
            retiring = {sorted(str(value) for value in pair.get("service_ids") or [])[-1]
                        for pair in duplicate_listings}
            offer_refresh = [row for row in offer_refresh
                             if str(row.get("service_id")) not in retiring]
            funnel = _join_funnel(
                args.state_dir, validated_contracts,
                getattr(args, "reply_transcripts", DEFAULT_REPLY_TRANSCRIPTS),
                getattr(args, "applied", DEFAULT_APPLIED), getattr(args, "earnings", DEFAULT_EARNINGS),
                getattr(args, "projects_dir", DEFAULT_PROJECTS), int(time.time()),
                getattr(args, "negotiate_run_log", DEFAULT_NEGOTIATE_RUN_LOG),
            )
            portfolio = _allocate_portfolio(
                args.state_dir, validated_contracts, funnel,
                getattr(args, "scorecard", DEFAULT_SCORECARD), int(time.time()),
                duplicate_listings=duplicate_listings,
            )
            versions_by_service = {str(row["service_id"]): row["service_version_sha256"]
                                   for row in validated_contracts}
            next_hypothesis = _prepare_next_hypothesis(
                getattr(args, "scorecard", DEFAULT_SCORECARD),
                args.state_dir / "effects.jsonl", args.state_dir / "outcomes.jsonl",
                validated_contracts, int(time.time()), mutation_contracts,
                compliance_violations, offer_refresh, unread_traffic,
            )
            pending_effect = None
            proposal_agent = None
            generated_render = None
            generated_render_path = None
            capability_paths = {str(Path(path).resolve()) for path in args.capability_evidence}
            recovery = _pending_recovery(args.state_dir, own_page, ws_url, inventory_path.parent)
            if recovery is not None:
                judgement = recovery.get("judgement")
                if not isinstance(judgement, dict):
                    raise RuntimeError("pending_effect_judgement_missing")
                if (recovery.get("changed_field") not in {"FAQ", "image", "title", "catchphrase", "body", "package", "price"}
                        or (recovery.get("changed_field") == "FAQ"
                            and recovery.get("service_id") != TARGET_SERVICE_ID)
                        or recovery.get("experiment_key") != judgement.get("experiment_key")):
                    raise RuntimeError("pending_effect_identity_invalid")
                public_before = json.loads(Path(recovery["public_before_path"]).read_text(encoding="utf-8"))
                seller_before = json.loads(Path(recovery["seller_form_before_path"]).read_text(encoding="utf-8"))
                seller_after = recovery.get("_recovery_seller_snapshot") or _seller_snapshot_for(
                    ws_url, str(recovery["service_id"]),
                )
                if recovery["changed_field"] == "image":
                    mutation_contract = recovery.get("mutation_contract")
                    if not isinstance(mutation_contract, dict):
                        raise RuntimeError("pending_image_mutation_contract_missing")
                    recovery_page = recovery.get("_recovery_public_page", own_page)
                    if not isinstance(recovery_page, dict):
                        raise RuntimeError("pending_image_public_readback_missing")
                    _validate_public_image_acceptance(
                        public_before, recovery_page, mutation_contract, state_dir=args.state_dir,
                    )
                elif recovery["changed_field"] in {"title", "catchphrase", "body", "package", "price"}:
                    mutation_contract = recovery.get("mutation_contract")
                    if not isinstance(mutation_contract, dict):
                        raise RuntimeError("pending_text_mutation_contract_missing")
                    recovery_page = recovery.get("_recovery_public_page")
                    if not isinstance(recovery_page, dict):
                        raise RuntimeError("pending_text_public_readback_missing")
                    _validate_text_public_acceptance(public_before, recovery_page, mutation_contract)
                    if recovery["changed_field"] == "package":
                        _validate_package_form_delta(seller_before, seller_after, mutation_contract)
                    else:
                        _validate_text_form_delta(seller_before, seller_after, mutation_contract)
                else:
                    question, answer = str(recovery["question"]), str(recovery["answer"])
                    _validate_public_acceptance(public_before, own_page, question, answer)
                    _validate_form_delta(seller_before, seller_after, question, answer)
                seller_after_path = inventory_path.parent / "seller-form-recovered.json"
                _atomic_write(seller_after_path, seller_after)
                judgement_path = inventory_path.parent / "judgement-recovered.json"
                _atomic_write(judgement_path, judgement)
                intent_path = Path(recovery["intent_path"])
                durable_recovery = {key: value for key, value in recovery.items()
                                    if key != "intent_path" and not key.startswith("_")}
                _atomic_write(intent_path, {
                    **durable_recovery, "status": "observed",
                    "public_after_path": str(
                        inventory_path.parent / (
                            f"recovery-public-{recovery['service_id']}.json"
                            if recovery["changed_field"] in {"title", "catchphrase", "body", "package", "price", "image"}
                            and recovery["service_id"] != TARGET_SERVICE_ID else "own-candidate.json"
                        )
                    ),
                    "seller_form_after_path": str(seller_after_path),
                    "observed_at_epoch": int(time.time()),
                })
                pending_effect = {
                    "intent_path": intent_path, "changed_field": recovery["changed_field"],
                    "public_before_path": Path(recovery["public_before_path"]),
                    "public_after_path": inventory_path.parent / (
                        f"recovery-public-{recovery['service_id']}.json"
                        if recovery["changed_field"] in {"title", "catchphrase", "body", "package", "price", "image"}
                        and recovery["service_id"] != TARGET_SERVICE_ID else "own-candidate.json"
                    ),
                    "seller_form_before_path": Path(recovery["seller_form_before_path"]),
                    "seller_form_after_path": seller_after_path, "recovered": True,
                }
                if recovery["changed_field"] == "FAQ":
                    pending_effect.update({"question": question, "answer": answer})
            else:
                proposal_noop = None
                if (next_hypothesis is not None
                        and next_hypothesis.get("guard_reason") == "proposal_contract_required"):
                    stuck_guard = _three_strike_same_guard(_recent_proposal_rejections(
                        args.state_dir,
                        f"improve:{next_hypothesis['service_id']}:{next_hypothesis['field']}",
                    ))
                    if stuck_guard is not None:
                        # Three consecutive rejections for the same guard on this gap mean the
                        # model will regenerate the same violation forever: stop asking for this
                        # gap and let the existing supersede mechanism surface the next one,
                        # exactly like a precondition-superseded candidate already does below.
                        refused_service = str(next_hypothesis["service_id"])
                        refused_field = str(next_hypothesis["field"])
                        refused_version = versions_by_service.get(refused_service, "")
                        _append_key_once(args.state_dir / "superseded-candidates.jsonl", "candidate_key", {
                            "version": 1,
                            "candidate_key": f"{refused_service}:{refused_field}:{refused_version}",
                            "service_id": refused_service, "field": refused_field,
                            "listing_version": refused_version,
                            "reason": f"three_strike_same_guard:{stuck_guard}"[:160],
                            "observed_at_epoch": int(time.time()),
                        })
                        next_hypothesis = _prepare_next_hypothesis(
                            getattr(args, "scorecard", DEFAULT_SCORECARD),
                            args.state_dir / "effects.jsonl", args.state_dir / "outcomes.jsonl",
                            validated_contracts, int(time.time()), mutation_contracts,
                            compliance_violations, offer_refresh, unread_traffic,
                        )
                if (next_hypothesis is not None
                        and next_hypothesis.get("guard_reason") == "proposal_contract_required"):
                    proposal_service_id = str(next_hypothesis["service_id"])
                    improve_gap_key = f"improve:{proposal_service_id}:{next_hypothesis['field']}"
                    improve_prior_rejections = _recent_proposal_rejections(args.state_dir, improve_gap_key)
                    proposal_source = next(
                        (row for row in validated_contracts if row["service_id"] == proposal_service_id), None,
                    )
                    family_name = capability_families.get(proposal_service_id)
                    family = capability_templates.get(str(family_name or ""))
                    if proposal_source is None or not isinstance(family_name, str) or not isinstance(family, dict):
                        raise RuntimeError("storefront_proposal_context_missing")
                    improve_family_market = _family_market(args.state_dir, family_name)
                    proposal_snapshot = (
                        presentation_snapshot if proposal_service_id == PRESENTATION_SERVICE_ID
                        else scope_snapshot if proposal_service_id == SCOPE_SERVICE_ID
                        else _seller_package_snapshot_for(ws_url, proposal_service_id)
                        if next_hypothesis.get("field") == "package"
                        else _seller_snapshot_for(ws_url, proposal_service_id)
                    )
                    proposal_snapshot_path = inventory_path.parent / f"proposal-seller-{proposal_service_id}.json"
                    _atomic_write(proposal_snapshot_path, proposal_snapshot)
                    proposal_public = _observe_own_page(
                        ws_url, inventory_path.parent, f"proposal-public-{proposal_service_id}.json",
                        proposal_service_id,
                    )
                    proposal, proposal_agent, allowed_refs = _invoke_proposal(
                        runner=args.runner,
                        schema=getattr(args, "proposal_schema", DEFAULT_PROPOSAL_SCHEMA),
                        workdir=args.workdir,
                        evidence_dir=inventory_path.parent / "proposal-agent", hypothesis=next_hypothesis,
                        source=proposal_source, seller_snapshot=proposal_snapshot,
                        family_name=family_name, family=family, manifest=competitor_manifest,
                        capability_paths=capability_paths, timeout_seconds=args.timeout_seconds,
                        prior_rejections=improve_prior_rejections,
                        family_market=improve_family_market,
                    )
                    proposal_rejected = None
                    try:
                        generated_contract = _seal_generated_proposal(
                            proposal, next_hypothesis, proposal_source, proposal_snapshot,
                            family_name, capability_families, allowed_refs, inventory_path.parent,
                            proposal_public,
                        )
                    except RuntimeError as error:
                        # One malformed proposal is a rejected candidate, not a broken wake: the
                        # guards did their job, so record why and let the next wake try another gap.
                        generated_contract = None
                        proposal_rejected = str(error)[:160]
                        _append_proposal_rejection(
                            args.state_dir, gap_key=improve_gap_key, rejection=proposal_rejected,
                            proposed_value=proposal.get("proposed_value"), pass_id=pass_id,
                        )
                    _atomic_write(inventory_path.parent / "proposal-record.json", {
                        "version": 1, "proposal": proposal, "route": proposal_agent,
                        "service_id": proposal_service_id, "changed_field": next_hypothesis["field"],
                        "contract_sha256": (generated_contract or {}).get("contract_sha256"),
                        # The reason a proposal was rejected existed only in memory, so four
                        # wakes in a row produced a no_op whose receipt read `None`.
                        "rejected": proposal_rejected,
                    })
                    if generated_contract is None:
                        proposal_noop = ({**proposal, "rejected": proposal_rejected}
                                         if proposal_rejected else proposal)
                    else:
                        field = generated_contract["allowed_delta"][0]
                        generated_render = {
                            "version": 1, "contract": generated_contract,
                            "before": {field: generated_contract["before_value"]},
                            "after": {field: generated_contract["proposed_value"]},
                            "delta": [field], "published": False,
                        }
                        generated_render_path = inventory_path.parent / "mutation-render-generated.json"
                        _atomic_write(generated_render_path, generated_render)
                        mutation_contracts.append(generated_contract)
                        next_hypothesis = _prepare_next_hypothesis(
                            getattr(args, "scorecard", DEFAULT_SCORECARD),
                            args.state_dir / "effects.jsonl", args.state_dir / "outcomes.jsonl",
                            validated_contracts, int(time.time()), mutation_contracts,
                            compliance_violations, offer_refresh, unread_traffic,
                        )
                mutation_contract = None
                if proposal_noop is not None:
                    # A proposal the guards rejected has a reason; the model's own no_op_reason
                    # is None in that case, and printing that told four wakes' receipts nothing.
                    noop_reason = str(proposal_noop.get("rejected")
                                      or proposal_noop.get("no_op_reason") or "proposal_rejected")
                    # A candidate refused for a structural reason stays refused until the listing
                    # changes: three consecutive wakes proposed an FAQ for a listing that already
                    # has one, each refusing with the same sentence.
                    if next_hypothesis is not None:
                        refused_service = str(next_hypothesis.get("service_id") or "")
                        refused_version = versions_by_service.get(refused_service, "")
                        _append_key_once(args.state_dir / "superseded-candidates.jsonl", "candidate_key", {
                            "version": 1,
                            "candidate_key": f"{refused_service}:{next_hypothesis.get('field')}:{refused_version}",
                            "service_id": refused_service,
                            "field": str(next_hypothesis.get("field") or ""),
                            "listing_version": refused_version,
                            "reason": noop_reason[:160], "observed_at_epoch": int(time.time()),
                        })
                    judgement = _guarded_noop({
                        "decision": "no_op", "service_id": None, "changed_field": None,
                        "before_value": None, "proposed_value": None,
                        "hypothesis": str(proposal_noop["hypothesis"]),
                        "competitor_evidence_paths": [], "capability_evidence_paths": [],
                        "success_metric": None, "observation_window_days": None,
                        "no_op_reason": noop_reason,
                        "experiment_key": None, "uncertainty": proposal_noop.get("uncertainty", []),
                    }, noop_reason)
                elif next_hypothesis is None:
                    judgement = _guarded_noop({
                        "decision": "no_op", "service_id": None, "changed_field": None,
                        "before_value": None, "proposed_value": None,
                        "hypothesis": "No current unfenced backlog item has an exact executable mutation contract.",
                        "competitor_evidence_paths": [], "capability_evidence_paths": [],
                        "success_metric": None, "observation_window_days": None,
                        "no_op_reason": "no_executable_unfenced_mutation_contract",
                        "experiment_key": None, "uncertainty": [],
                    }, "no_executable_unfenced_mutation_contract")
                elif not next_hypothesis["executable"]:
                    raw_judgement = {
                        "decision": "no_op", "service_id": None, "changed_field": None,
                        "before_value": None, "proposed_value": None,
                        "hypothesis": str(next_hypothesis["reason"]),
                        "competitor_evidence_paths": [], "capability_evidence_paths": [],
                        "success_metric": None, "observation_window_days": None,
                        "no_op_reason": str(next_hypothesis["guard_reason"]),
                        "experiment_key": None, "uncertainty": [],
                    }
                    judgement = _guard_judgement(
                        raw_judgement,
                        own_page=own_page, competitor_manifest=competitor_manifest,
                        capability_paths=capability_paths, evidence_dir=inventory_path.parent,
                        effects_path=args.state_dir / "effects.jsonl", minimum_epoch=minimum_epoch,
                        now=int(time.time()),
                    )
                elif next_hypothesis is not None and next_hypothesis.get("field") == "image":
                    mutation_contract = next((contract for contract in mutation_contracts
                                              if contract["contract_sha256"]
                                              == next_hypothesis["mutation_contract_sha256"]), None)
                    if mutation_contract is None:
                        raise RuntimeError("storefront_image_mutation_contract_missing")
                    _atomic_write(inventory_path.parent / "mutation-contract.json", mutation_contract)
                    judgement = _image_judgement(next_hypothesis, mutation_contract)
                elif next_hypothesis is not None and next_hypothesis.get("field") in {
                    # A field missing from this set has its sealed contract silently ignored and
                    # the wake falls through to the general judge, which then answers about some
                    # other listing entirely. The catchphrase spent several wakes in that state.
                    "title", "catchphrase", "body", "package", "price",
                }:
                    mutation_contract = next((contract for contract in mutation_contracts
                                              if contract["contract_sha256"]
                                              == next_hypothesis["mutation_contract_sha256"]), None)
                    if mutation_contract is None:
                        raise RuntimeError("storefront_text_mutation_contract_missing")
                    _atomic_write(inventory_path.parent / "mutation-contract.json", mutation_contract)
                    judgement = _text_judgement(
                        next_hypothesis, mutation_contract, args.state_dir / "effects.jsonl", int(time.time()),
                    )
                else:
                    target_family_name = capability_families.get(TARGET_SERVICE_ID)
                    target_family_market = (
                        _family_market(args.state_dir, target_family_name)
                        if isinstance(target_family_name, str) else None
                    )
                    raw_judgement = _invoke_judge(
                        runner=args.runner, schema=args.schema, workdir=args.workdir,
                        evidence_dir=inventory_path.parent / "judge", own_page=own_page,
                        manifest=competitor_manifest, capability_paths=capability_paths,
                        timeout_seconds=args.timeout_seconds, family_market=target_family_market,
                    )
                    judgement = _guard_judgement(
                        raw_judgement,
                        own_page=own_page, competitor_manifest=competitor_manifest,
                        capability_paths=capability_paths, evidence_dir=inventory_path.parent,
                        effects_path=args.state_dir / "effects.jsonl", minimum_epoch=minimum_epoch,
                        now=int(time.time()),
                    )
                judgement_path = inventory_path.parent / "judgement.json"
                _atomic_write(judgement_path, judgement)
                if judgement["decision"] == "change":
                    presend_path = inventory_path.parent / "presend-own-page.json"
                    presend = _observe_own_page(
                        ws_url, inventory_path.parent, presend_path.name, str(judgement["service_id"]),
                    )
                    # The legacy judge only knows the zero-image seed. Once that listing has an
                    # image the gap is closed, which is a finished job, not a failed wake.
                    # A proposal the executor cannot parse is a rejected candidate. Check it here,
                    # before any browser work, so a malformed model answer costs the candidate and
                    # not the wake.
                    if judgement["changed_field"] == "FAQ":
                        try:
                            _split_faq(str(judgement.get("proposed_value") or ""))
                        except RuntimeError as error:
                            judgement = {**judgement, "decision": "no_op",
                                         "no_op_reason": f"rejected_proposal:{error}",
                                         "changed_field": None, "experiment_key": None}
                            _atomic_write(judgement_path, judgement)
                    if judgement["decision"] != "change":
                        pass
                    elif (judgement["changed_field"] == "image"
                            and int(presend.get("service_image_count") or 0) > 0
                            and (mutation_contract or {}).get("before_value", {}).get(
                                "service_image_ids") == []):
                        judgement = {**judgement, "decision": "no_op",
                                     "no_op_reason": "image_gap_already_closed",
                                     "changed_field": None, "experiment_key": None}
                        _atomic_write(judgement_path, judgement)
                    elif judgement["changed_field"] not in {"title", "catchphrase", "body", "package", "price"}:
                        try:
                            _presend_guard(judgement, presend, mutation_contract, state_dir=args.state_dir)
                        except RuntimeError as error:
                            # The guard's job is to stop the change, not the wake. A contract whose
                            # recorded before-state no longer matches the live listing describes
                            # work that is already done or superseded, so record it and move on.
                            _append_key_once(args.state_dir / "superseded-candidates.jsonl", "candidate_key", {
                                "version": 1,
                                "candidate_key": f"{judgement['service_id']}:{judgement['changed_field']}:"
                                                 f"{versions_by_service.get(str(judgement['service_id']), '')}",
                                "service_id": str(judgement["service_id"]),
                                "field": str(judgement["changed_field"]),
                                "listing_version": versions_by_service.get(str(judgement["service_id"]), ""),
                                "reason": str(error)[:160], "observed_at_epoch": int(time.time()),
                            })
                            judgement = {**judgement, "decision": "no_op",
                                         "no_op_reason": f"precondition_superseded:{error}",
                                         "changed_field": None, "experiment_key": None}
                            _atomic_write(judgement_path, judgement)
                    if args.effect and judgement["decision"] == "change":
                        blocked = _persist_effect_block(
                            args, output, pass_id, "before_listing_effect",
                        )
                        if blocked is not None:
                            return 0, blocked
                        if judgement["changed_field"] == "image":
                            seller_before, _, intent_path = _execute_image_effect(
                                ws_url=ws_url, contract=mutation_contract, judgement=judgement,
                                public_before_path=presend_path, evidence_dir=inventory_path.parent,
                                state_dir=args.state_dir,
                            )
                        elif judgement["changed_field"] == "package":
                            seller_before, _, intent_path = _execute_package_effect(
                                ws_url=ws_url, contract=mutation_contract, judgement=judgement,
                                public_before_path=presend_path, evidence_dir=inventory_path.parent,
                                state_dir=args.state_dir,
                            )
                        elif judgement["changed_field"] in {"title", "catchphrase", "body", "price"}:
                            seller_before, _, intent_path = _execute_text_effect(
                                ws_url=ws_url, contract=mutation_contract, judgement=judgement,
                                public_before_path=presend_path, evidence_dir=inventory_path.parent,
                                state_dir=args.state_dir,
                            )
                        else:
                            question, answer = _split_faq(str(judgement["proposed_value"]))
                            seller_before, _, intent_path = _execute_faq_effect(
                                ws_url=ws_url, question=question, answer=answer, judgement=judgement,
                                public_before_path=presend_path, evidence_dir=inventory_path.parent,
                                state_dir=args.state_dir,
                            )
                        public_after_path = inventory_path.parent / "after-public.json"
                        public_after = _observe_own_page(
                            ws_url, inventory_path.parent, public_after_path.name, str(judgement["service_id"]),
                        )
                        seller_after = _seller_snapshot_for(ws_url, str(judgement["service_id"]))
                        if judgement["changed_field"] == "image":
                            _validate_public_image_acceptance(
                                presend, public_after, mutation_contract, state_dir=args.state_dir,
                            )
                        elif judgement["changed_field"] in {"title", "catchphrase", "body", "package", "price"}:
                            _validate_text_public_acceptance(presend, public_after, mutation_contract)
                            if judgement["changed_field"] == "package":
                                _validate_package_form_delta(seller_before, seller_after, mutation_contract)
                            else:
                                _validate_text_form_delta(seller_before, seller_after, mutation_contract)
                        else:
                            _validate_public_acceptance(presend, public_after, question, answer)
                            _validate_form_delta(seller_before, seller_after, question, answer)
                        seller_after_path = inventory_path.parent / "seller-form-after.json"
                        _atomic_write(seller_after_path, seller_after)
                        intent = json.loads(intent_path.read_text(encoding="utf-8"))
                        _atomic_write(intent_path, {
                            **intent, "status": "observed", "public_after_path": str(public_after_path),
                            "seller_form_after_path": str(seller_after_path),
                            "observed_at_epoch": int(time.time()),
                        })
                        pending_effect = {
                            "intent_path": intent_path, "changed_field": judgement["changed_field"],
                            "public_before_path": presend_path, "public_after_path": public_after_path,
                            "seller_form_before_path": Path(intent["seller_form_before_path"]),
                            "seller_form_after_path": seller_after_path, "recovered": False,
                        }
                        if judgement["changed_field"] == "FAQ":
                            pending_effect.update({"question": question, "answer": answer})
            # One external effect per wake. When nothing else changed, a duplicate listing is
            # taken down through the platform's own archive control, which keeps it restorable.
            retire_result = None
            retire_attempted_this_wake = False
            retire_effect_this_wake = False
            retire_confirmed_this_wake = False
            # The allocator returns the single row it selected, preferring retirement, not the
            # whole catalogue; reading a list it never returns is how this silently did nothing.
            selected_allocation = portfolio.get("selected") or {}
            retire_allocation = (
                selected_allocation
                if selected_allocation.get("action") == "RETIRE"
                and (selected_allocation.get("gates") or {}).get("duplicate_of_service_id")
                else None)
            # One experiment per listing, not one action per wake: archiving a duplicate touches
            # a different listing and is recoverable, so it no longer waits for a wake that did
            # nothing, which the refresh queue would have postponed for hours.
            changed_service = str(judgement.get("service_id") or "")
            if (args.effect and retire_allocation is not None
                    and pending_effect is None
                    and str(retire_allocation["service_id"]) != changed_service):
                blocked = _persist_effect_block(
                    args, output, pass_id, "before_retire_effect",
                )
                if blocked is not None:
                    return 0, blocked
                retire_service_id = str(retire_allocation["service_id"])
                try:
                    # The card's own controls are adapter input and are kept out of the hashed
                    # catalogue, so the state row is rebuilt from the source card here.
                    retire_source_card = next(
                        row for row in contract_sources
                        if str(row.get("service_id")) == retire_service_id)
                    retire_contract = _render_listing_state_mutation(
                        next(row for row in validated_contracts
                             if row["service_id"] == retire_service_id),
                        {"service_id": retire_service_id,
                         "state": next(str(row.get("state") or "") for row in inventory["services"]
                                       if str(row.get("service_id")) == retire_service_id),
                         "state_controls": retire_source_card.get("state_controls") or []},
                        _seller_snapshot_for(ws_url, retire_service_id),
                        capability_families, retire_allocation,
                    )
                    _atomic_write(inventory_path.parent / f"retire-contract-{retire_service_id}.json",
                                  retire_contract)
                    # Its own tab: reusing the wake's leased socket after a full pass of
                    # browsing is what returns HTTP 500, which is the same fault the demand
                    # crawl hit and the same fix.
                    import storefront_draft

                    retire_opened = storefront_draft.open_tab_with_retry(
                        [sys.executable, str(getattr(args, "default_tab_script", DEFAULT_TAB)),
                         "--owner", "gig-storefront-direct", "--background", "open", "about:blank"],
                        capture_output=True, text=True, check=False, timeout=30,
                    )
                    retire_tab = json.loads(retire_opened.stdout)
                    if retire_opened.returncode != 0 or retire_tab.get("ok") is not True:
                        raise RuntimeError("storefront_retire_tab_open_failed")
                    try:
                        retire_attempted_this_wake = True
                        retire_attempt = args.state_dir / f"retire-intent-{retire_service_id}.json"
                        _atomic_write(retire_attempt, {
                            "version": 1, "status": "attempted", "effect": 0,
                            "service_id": retire_service_id, "contract_sha256": retire_contract["contract_sha256"],
                            "mutation_contract": retire_contract,
                            "attempted_at_epoch": int(time.time()),
                        })
                        retire_result = asyncio.run(_execute_listing_state_effect_async(
                            str(retire_tab["ws"]), contract=retire_contract,
                            evidence_dir=inventory_path.parent))
                        retire_confirmed_this_wake = True
                        retire_effect_this_wake = retire_result.get("recovered") is not True
                        _atomic_write(retire_attempt, {
                            "version": 1, "status": "confirmed",
                            "effect": int(retire_effect_this_wake),
                            "service_id": retire_service_id, "contract_sha256": retire_contract["contract_sha256"],
                            "mutation_contract": retire_contract,
                            "recovered": retire_result.get("recovered") is True,
                            "confirmed_at_epoch": int(time.time()),
                        })
                    finally:
                        if retire_tab.get("target_id"):
                            subprocess.run(
                                [sys.executable, str(getattr(args, "default_tab_script", DEFAULT_TAB)),
                                 "--owner", "gig-storefront-direct", "close",
                                 str(retire_tab["target_id"])], capture_output=True, text=True,
                                check=False, timeout=30,
                            )
                    _append_key_once(args.state_dir / "effects.jsonl", "experiment_key", {
                        "version": 1, "status": "accepted", "effect": 1,
                        "service_id": retire_service_id, "changed_field": "listing_state",
                        "experiment_key": _experiment_key(
                            retire_service_id, "listing_state", retire_contract["proposed_value"]),
                        "contract_sha256": retire_contract["contract_sha256"],
                        "after_value": retire_contract["proposed_value"],
                        "before_value": retire_contract["before_value"],
                        "accepted_at_epoch": int(time.time()),
                        "reason": retire_allocation["reason"],
                        "restore_href": retire_result.get("restore_href"),
                    })
                except Exception as error:  # retiring a duplicate never ends a wake
                    retire_result = {"error": f"{type(error).__name__}:{str(error)[:160]}"}
                _atomic_write(inventory_path.parent / "retire-result.json",
                              {"allocation": retire_allocation, "result": retire_result})
            _lease(args.lease_script, "heartbeat", task, lease)
            release = _lease(args.lease_script, "release", task, lease)
            released = release.get("released") == task
            if not released:
                raise RuntimeError("lease_release_unproven")

            import storefront_draft

            new_listing_path = getattr(args, "new_listing_contract", DEFAULT_NEW_LISTING_CONTRACT)
            new_listing_contract = storefront_draft.load_contract(new_listing_path)
            create_family = None
            create_draft_claim = None
            create_effect_this_wake = False
            fixed_candidate_public = new_listing_contract["draft_service_id"] in inventory_ids
            # One published listing per distinct demand evidence. Without this the loop generates a
            # brand new service on every full wake until the catalogue hits its slot limit.
            demand_evidence_path = str(Path(new_listing_path).resolve())
            def _sold_from_this_demand(line: str) -> bool:
                row = json.loads(line)
                if row.get("status") != "published" or not str(
                        row.get("candidate_key") or "").startswith("storefront:create:v1:"):
                    return False
                # Rows written before this field existed came from this same committed demand file.
                return row.get("demand_evidence_path", demand_evidence_path) == demand_evidence_path

            demand_already_sold = any(
                _sold_from_this_demand(line)
                for line in (args.state_dir / "new-listing-drafts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ) if (args.state_dir / "new-listing-drafts.jsonl").exists() else False
            # When the committed demand is spent, look for the next market instead of idling.
            catalog_families = set(_load_catalog_entries())
            demand_ledger = args.state_dir / "demand-evidence.jsonl"
            known_clusters = _jsonl_rows(demand_ledger)[0] if demand_ledger.exists() else []
            known_clusters = _catalog_bound_demand_clusters(known_clusters, catalog_families)
            known_clusters = [{
                **row,
                "recurring_potential": _capability_recurring_potential(
                    capability_templates.get(str(row.get("capability_family") or ""), {})
                ),
            } for row in known_clusters]
            dismissal_ledger = args.state_dir / "demand-dismissals.jsonl"
            dismissed_clusters = {
                str(row.get("cluster_key") or "") for row in _jsonl_rows(dismissal_ledger)[0]
                if row.get("status") == "dismissed"
            } if dismissal_ledger.exists() else set()
            # A family that already has a filled, unpublished draft is finished before any new
            # demand cluster is chosen -- see `_select_demand_cluster_for_wake`.
        # The ledger outlives the drafts it describes: a draft deleted on an earlier wake leaves
            # its row behind once the evidence directory naming the deletion is collected. Four
            # such rows were live here, and "oldest first" then picked the stalest -- which is the
            # one most likely to be gone. The seller's own card census, read above, is what says
            # a draft exists.
            stranded_drafts = _families_with_unpublished_drafts(
                args.state_dir, inventory_ids, live_draft_ids=set(draft_ids))
            unused_cluster, demand_derivation = _select_demand_cluster_for_wake(
                known_clusters, dismissed_clusters, stranded_drafts)
            # Choosing a category reads official options only, so it does not wait on the
            # publication brake: a waiting cluster gets its category as soon as it exists.
            category_ledger = args.state_dir / "demand-category.jsonl"
            categorised = {json.loads(line).get("cluster_key")
                           for line in category_ledger.read_text(encoding="utf-8").splitlines()
                           if line.strip()} if category_ledger.exists() else set()
            if unused_cluster is not None and unused_cluster.get("cluster_key") not in categorised:
                try:
                    master_options = (presentation_snapshot.get("select_options") or {}).get(
                        "data[Service][master_category]") or []
                    choice, category_route = _invoke_category_proposal(
                        runner=getattr(args, "runner", DEFAULT_RUNNER),
                        schema=getattr(args, "category_proposal_schema", DEFAULT_CATEGORY_PROPOSAL_SCHEMA),
                        workdir=args.workdir,
                        evidence_dir=inventory_path.parent / "category-proposal-agent",
                        cluster=unused_cluster, options=master_options,
                        timeout_seconds=args.timeout_seconds,
                    )
                    if choice.get("decision") != "choose":
                        raise RuntimeError(
                            f"storefront_category_no_op:{str(choice.get('no_op_reason'))[:120]}")
                    bound = _validate_category_choice(choice.get("master_category_value"), master_options, "master")
                    _append_key_once(category_ledger, "cluster_key", {
                        "version": 1, "cluster_key": unused_cluster.get("cluster_key"),
                        "query": unused_cluster.get("query"),
                        "capability_family": unused_cluster.get("capability_family"),
                        "master_category": bound, "rationale": str(choice.get("rationale") or "")[:400],
                        "route": category_route, "observed_at_epoch": int(time.time()),
                    })
                    demand_derivation = {**(demand_derivation or {}),
                                         "master_category": bound["label"],
                                         "master_category_value": bound["value"]}
                except Exception as error:  # category selection must never end a wake
                    demand_derivation = {**(demand_derivation or {}),
                                         "category_error": f"{type(error).__name__}:{str(error)[:140]}"}
            # The sub and type options a category offers only exist inside a service form, so
            # they are read there and stored separately. The form is never submitted.
            options_ledger = args.state_dir / "demand-category-options.jsonl"
            option_keys = {json.loads(line).get("cluster_key")
                           for line in options_ledger.read_text(encoding="utf-8").splitlines()
                           if line.strip()} if options_ledger.exists() else set()
            bound_category = next(
                (json.loads(line) for line in category_ledger.read_text(encoding="utf-8").splitlines()
                 if line.strip()
                 and json.loads(line).get("cluster_key") == (unused_cluster or {}).get("cluster_key")),
                None,
            ) if unused_cluster is not None and category_ledger.exists() else None
            # A published listing's form does not offer its category select, so the options
            # can only be read on a draft. When the candidate is already public this waits
            # for the CREATE flow, which selects the category on the draft it claims.
            if (bound_category is not None and not fixed_candidate_public
                    and bound_category.get("cluster_key") not in option_keys):
                try:
                    children = storefront_draft.read_category_children(
                        getattr(args, "default_tab_script", DEFAULT_TAB),
                        str(new_listing_contract["draft_service_id"]),
                        str((bound_category.get("master_category") or {}).get("value")),
                    )
                    subs = children.get("data[Service][master_sub_category]") or []
                    types = children.get("data[Service][master_category_type_id]") or []
                    _append_key_once(options_ledger, "cluster_key", {
                        "version": 1, "cluster_key": bound_category.get("cluster_key"),
                        "master_category": bound_category.get("master_category"),
                        "sub_options": subs, "type_options": types,
                        "observed_at_epoch": int(time.time()),
                    })
                    demand_derivation = {**(demand_derivation or {}),
                                         "category_sub_options": len(subs),
                                         "category_type_options": len(types)}
                except Exception as error:  # reading options must never end a wake
                    demand_derivation = {**(demand_derivation or {}),
                                         "category_options_error": f"{type(error).__name__}:{str(error)[:140]}"}
            inventory_digest = str(public_capability_inventory["inventory_sha256"])
            inventory_probe_due = _capability_inventory_needs_market_probe(
                known_clusters, inventory_digest,
            )
            if demand_already_sold and (unused_cluster is None or inventory_probe_due):
                try:
                    # Scoped to the owner's catalog: an unscoped run named
                    # "user-interview-synthesizer" (not in the catalog) and it went on to
                    # publish live on 2026-09-05 as service 4384702.
                    exploration_families = {
                        name: value for name, value in _unlisted_capability_templates(
                            capability_templates, capability_families,
                        ).items() if name in catalog_families
                    } or {
                        name: value for name, value in capability_templates.items()
                        if name in catalog_families
                    }
                    proposal, route = _invoke_demand_proposal(
                        runner=getattr(args, "runner", DEFAULT_RUNNER),
                        schema=getattr(args, "demand_proposal_schema", DEFAULT_DEMAND_PROPOSAL_SCHEMA),
                        workdir=args.workdir,
                        evidence_dir=inventory_path.parent / "demand-proposal-agent",
                        families=exploration_families,
                        catalog_titles=[str(row.get("title") or "") for row in inventory["services"]],
                        timeout_seconds=args.timeout_seconds,
                    )
                    sealed = _seal_demand_proposal(
                        proposal, set(exploration_families),
                        [str(row.get("title") or "") for row in inventory["services"]],
                    )
                    appended = 0
                    skipped = []
                    for candidate in sealed:
                        try:
                            cluster = _crawl_demand_cluster(
                                getattr(args, "default_tab_script", DEFAULT_TAB),
                                inventory_path.parent, candidate["query"])
                        except RuntimeError as error:
                            # This loop crawls several candidate queries the model just
                            # proposed. One query's tab failing to open, redirecting off
                            # coconala.com, or reading empty after the house retry is an
                            # answer about that one query among several -- it is skipped
                            # so the rest of the batch is still explored this wake,
                            # unlike the single-query bootstrap crawl (see
                            # _crawl_demand_cluster's docstring) where the same failure
                            # IS the whole decision and stays fatal. Any other RuntimeError
                            # is a real bug and still ends this exploration attempt.
                            if not _demand_crawl_failure_is_per_query(error):
                                raise
                            skipped.append({"query": candidate["query"], "reason": str(error)})
                            continue
                        scored = _score_demand_cluster(cluster)
                        row = {**cluster, **scored, "capability_family": candidate["capability_family"],
                               "rationale": candidate["rationale"], "route": route,
                               "capability_inventory_sha256": inventory_digest,
                               "recurring_potential": _capability_recurring_potential(
                                   capability_templates.get(candidate["capability_family"], {})
                               ),
                               "observed_at_epoch": int(time.time()),
                               "cluster_key": _demand_cluster_key(candidate["query"], "")}
                        appended += int(_append_key_once(demand_ledger, "cluster_key", row))
                    demand_derivation = {"proposed": len(sealed), "appended": appended,
                                         "skipped": len(skipped), "skipped_queries": skipped,
                                         "route": route.get("model")}
                except Exception as error:  # exploration is optional; a wake must survive it
                    demand_derivation = {"proposed": 0, "appended": 0,
                                         "error": f"{type(error).__name__}:{str(error)[:160]}"}
            # A catalogue fills over days, not over consecutive full wakes.
            last_create = _last_published_create_epoch(args.state_dir)
            create_spacing_open = (last_create is None
                                   or int(time.time()) - last_create >= CREATE_MIN_INTERVAL_SECONDS)
            # A self-derived market is a second way in: the committed demand file may be spent
            # while a scored cluster with an official category is waiting.
            cluster_blueprint = None
            # Search volume says a market exists; it does not say this seller can sell in it.
            unsold_family = (_family_traffic_without_sales(
                args.state_dir / "analytics.jsonl", capability_families,
                str((unused_cluster or {}).get("capability_family") or ""))
                if unused_cluster is not None else None)
            if unsold_family is not None:
                cluster_key = str((unused_cluster or {}).get("cluster_key") or "")
                if cluster_key:
                    _append_key_once(dismissal_ledger, "cluster_key", {
                        "version": 1, "cluster_key": cluster_key, "status": "dismissed",
                        "reason": "own_family_has_traffic_without_sales",
                        "own_family_evidence": unsold_family,
                        "observed_at_epoch": int(time.time()),
                    })
                demand_derivation = {**(demand_derivation or {}),
                                     "create_blocked": "own_family_has_traffic_without_sales",
                                     "own_family_evidence": unsold_family}
            # A family can already be public without ever tripping the traffic-without-sales
            # check above: that check needs a families.json service mapping, and a CREATE never
            # writes one back. mobile_app_dev published as service 4386009 with no such mapping,
            # so its cluster kept winning `_next_unused_demand_cluster` forever -- the ledger's
            # own creation history is dismissed here instead, which every CREATE always updates.
            family_already_public = (
                unsold_family is None and unused_cluster is not None
                and _family_already_published(
                    args.state_dir / "new-listing-drafts.jsonl",
                    str(unused_cluster.get("capability_family") or ""))
            )
            if family_already_public:
                cluster_key = str((unused_cluster or {}).get("cluster_key") or "")
                if cluster_key:
                    _append_key_once(dismissal_ledger, "cluster_key", {
                        "version": 1, "cluster_key": cluster_key, "status": "dismissed",
                        "reason": "capability_family_already_public",
                        "observed_at_epoch": int(time.time()),
                    })
                demand_derivation = {**(demand_derivation or {}),
                                     "create_blocked": "capability_family_already_public"}
            if (unused_cluster is not None and bound_category is not None
                    and unsold_family is None and not family_already_public):
                try:
                    cluster_blueprint = _create_blueprint_from_cluster(
                        new_listing_contract, unused_cluster, bound_category)
                except RuntimeError as error:
                    demand_derivation = {**(demand_derivation or {}),
                                         "blueprint_blocked": str(error)[:120]}
            # Five conditions decide whether this wake may start or finish a listing, and when
            # the answer was no the wake said only that it had no executable contract. Working
            # out which one closed meant reproducing each by hand against live state, once per
            # question. The wake states it instead, so one receipt answers what an afternoon of
            # reverse-engineering did.
            create_gate = {
                "fixed_candidate_public": bool(fixed_candidate_public),
                "create_spacing_open": bool(create_spacing_open),
                "no_open_improve_hypothesis": next_hypothesis is None,
                "slots_available": observed < 20,
                "demand_unspent_or_blueprint": bool(
                    not demand_already_sold or cluster_blueprint is not None),
            }
            create_blocked_by = sorted(name for name, open_ in create_gate.items() if not open_)
            demand_derivation = {**(demand_derivation or {}), "create_gate": create_gate,
                                 "create_blocked_by": create_blocked_by}
            if not create_blocked_by:
                source_service_id = new_listing_contract["draft_service_id"]
                if cluster_blueprint is not None:
                    # The source listing must belong to the market's own capability family.
                    # Handing the model Excel demand next to an SEO writing offer asks it to
                    # justify one with the other, and it correctly refuses.
                    wanted = str(cluster_blueprint.get("capability_family") or "")
                    source_service_id = next(
                        (sid for sid in sorted(capability_families)
                         if capability_families.get(sid) == wanted
                         and any(row["service_id"] == sid for row in validated_contracts)),
                        source_service_id,
                    )
                create_source = next((row for row in validated_contracts
                                      if row["service_id"] == source_service_id), None)
                if create_source is None:
                    raise RuntimeError("storefront_create_source_contract_missing")
                wanted_family = str((cluster_blueprint or {}).get("capability_family") or "")
                create_family, create_template, selected_evidence = _resolve_create_capability(
                    wanted=wanted_family, source=create_source,
                    service_families=capability_families, templates=capability_templates,
                )
                create_capability_paths = _proposal_capability_evidence(
                    capability_paths, selected_evidence)
                demand = ({**cluster_blueprint["demand_evidence"],
                           "evidence_path": cluster_blueprint["demand_evidence_path"]}
                          if cluster_blueprint is not None else
                          {**new_listing_contract["demand_evidence"],
                           "evidence_path": str(Path(new_listing_path).resolve())})
                recovered_create_contract = _recover_prepared_create_contract(
                    args.state_dir, create_family, str(demand["evidence_path"]),
                )
                create_gap_key = f"create:{create_family}"
                create_prior_rejections = _recent_proposal_rejections(args.state_dir, create_gap_key)
                create_stuck_guard = _three_strike_same_guard(create_prior_rejections)
                if recovered_create_contract is not None:
                    create_seller_snapshot = None
                    create_proposal = {"decision": "create"}
                    create_route = {"status": "recovered_prepared_contract"}
                    create_allowed_refs = set()
                elif create_stuck_guard is not None:
                    # Three consecutive rejections for the same guard on this family mean the
                    # model will regenerate the same violation every wake: stop asking it, and
                    # dismiss the cluster (when this family came from one) so a later wake's
                    # cluster selection advances to a different family instead of retrying this
                    # one forever -- the same composition `unsold_family`/`family_already_public`
                    # already use above.
                    live_draft = _family_has_unpublished_draft(
                        args.state_dir, create_family, inventory_ids)
                    cluster_key = str((unused_cluster or {}).get("cluster_key") or "")
                    # Skipping this wake's model call is always right once the same guard has
                    # refused three times. Dismissing the cluster is not: a family with a draft
                    # already created and waiting to be filled has proven the model can satisfy
                    # every other gate, and dismissal is permanent, so it would strand a real
                    # draft on the platform. Skip and let the next wake try; dismiss only a
                    # family that has nothing in flight to lose.
                    if cluster_key and live_draft is None:
                        _append_key_once(dismissal_ledger, "cluster_key", {
                            "version": 1, "cluster_key": cluster_key, "status": "dismissed",
                            "reason": f"three_strike_same_guard:{create_stuck_guard}"[:160],
                            "observed_at_epoch": int(time.time()),
                        })
                    create_seller_snapshot = None
                    create_proposal = {
                        "decision": "no_op",
                        "no_op_reason": (
                            f"three_strike_same_guard:{create_stuck_guard}"
                            + (f":draft_{live_draft}_kept" if live_draft else ":cluster_dismissed")
                        )[:160],
                    }
                    create_route = {"status": "skipped_three_strike_same_guard"}
                    create_allowed_refs = set()
                else:
                    create_seller_snapshot = _seller_snapshot_from_fresh_tab(
                        getattr(args, "default_tab_script", DEFAULT_TAB), source_service_id,
                    )
                    create_proposal, create_route, create_allowed_refs = _invoke_create_proposal(
                        runner=getattr(args, "runner", DEFAULT_RUNNER),
                        schema=getattr(args, "create_proposal_schema", DEFAULT_CREATE_PROPOSAL_SCHEMA),
                        workdir=args.workdir,
                        evidence_dir=inventory_path.parent / "create-proposal-agent",
                        source=create_source, family_name=create_family, family=create_template,
                        demand=demand, capability_paths=create_capability_paths,
                        catalog_titles=[str(row.get("title") or "") for row in inventory["services"]],
                        timeout_seconds=args.timeout_seconds,
                        listing_catalog_entry=_load_catalog_entries().get(str(create_family)),
                        prior_rejections=create_prior_rejections,
                    )
                proposal_agent = create_route
                if create_proposal.get("decision") == "create" and args.effect:
                    blocked = _persist_effect_block(
                        args, output, pass_id, "before_blank_draft_create",
                    )
                    if blocked is not None:
                        return 0, blocked
                    preferred_draft_ids = []
                    candidate_ledger = args.state_dir / "new-listing-drafts.jsonl"
                    deleted_draft_ids = _observed_deleted_draft_ids(args.state_dir / "evidence")
                    if candidate_ledger.exists():
                        for line in reversed(candidate_ledger.read_text(encoding="utf-8").splitlines()):
                            if not line.strip():
                                continue
                            row = json.loads(line)
                            draft_id = str(row.get("draft_service_id") or "")
                            # Same "prepared", not "draft_prepared", vocabulary as the ledger rows
                            # `prepare_draft` writes (see `_deletable_drafts`) -- without this a
                            # draft that had already been prepared, not just created, was never
                            # offered back for reuse here.
                            if (row.get("capability_family") == create_family and draft_id.isdigit()
                                    and draft_id not in inventory_ids
                                    and draft_id not in deleted_draft_ids
                                    and int(row.get("public_effect") or 0) == 0
                                    and row.get("status") in {"draft_created", "prepared"}):
                                preferred_draft_ids.append(draft_id)
                    create_draft_claim = storefront_draft.create_or_claim_blank_draft(
                        getattr(args, "default_tab_script", DEFAULT_TAB),
                        preferred_draft_ids=preferred_draft_ids,
                    )
                    create_effect_this_wake = int(
                        create_draft_claim.get("effect") or create_draft_claim.get("public_effect") or 0
                    ) == 1
                    blueprint = cluster_blueprint or {
                        **new_listing_contract,
                        "demand_evidence_path": str(Path(new_listing_path).resolve())}
                    if cluster_blueprint is not None and recovered_create_contract is None:
                        # The category's sub and type options only exist once a draft holds the
                        # chosen top-level category, so they are read and picked here.
                        draft_id = str(create_draft_claim["draft_service_id"])
                        children = storefront_draft.read_category_children(
                            getattr(args, "default_tab_script", DEFAULT_TAB),
                            draft_id, blueprint["category"]["master"]["value"],
                        )
                        picked, child_route = _invoke_category_child_proposal(
                            runner=getattr(args, "runner", DEFAULT_RUNNER),
                            schema=getattr(args, "category_child_schema", DEFAULT_CATEGORY_CHILD_SCHEMA),
                            workdir=args.workdir,
                            evidence_dir=inventory_path.parent / "category-child-agent",
                            cluster=unused_cluster, master=blueprint["category"]["master"],
                            children=children, timeout_seconds=args.timeout_seconds,
                        )
                        sub = _validate_category_choice(
                            picked.get("sub_value"),
                            children.get("data[Service][master_sub_category]") or [], "sub")
                        # Type options only exist once the sub category is set, so read again.
                        typed = storefront_draft.read_category_children(
                            getattr(args, "default_tab_script", DEFAULT_TAB),
                            draft_id, blueprint["category"]["master"]["value"], sub["value"],
                        )
                        # A disabled, empty type select (see `read_category_children`) means this
                        # category genuinely has no third level -- skip the type-picking agent
                        # call entirely rather than let it fabricate a value with no real options.
                        category_type_absent = bool(typed.get("master_category_type_absent"))
                        if category_type_absent:
                            category_type = None
                        else:
                            type_options = typed.get("data[Service][master_category_type_id]") or []
                            # The site rejects a listing whose type is offered but left unset, so
                            # whenever the form does offer a type it is always read and chosen.
                            picked_type, _ = _invoke_category_child_proposal(
                                runner=getattr(args, "runner", DEFAULT_RUNNER),
                                schema=getattr(args, "category_child_schema", DEFAULT_CATEGORY_CHILD_SCHEMA),
                                workdir=args.workdir,
                                evidence_dir=inventory_path.parent / "category-type-agent",
                                cluster=unused_cluster, master=blueprint["category"]["master"],
                                children={**typed, "data[Service][master_sub_category]": [sub]},
                                timeout_seconds=args.timeout_seconds,
                            )
                            category_type = _validate_category_choice(
                                picked_type.get("type_value"), type_options, "type")
                        blueprint = {**blueprint, "category": {
                            "master": blueprint["category"]["master"],
                            "sub": sub,
                            "type": category_type,
                        }}
                        demand_derivation = {**(demand_derivation or {}),
                                             "category_triple": blueprint["category"],
                                             "category_type_absent": category_type_absent,
                                             "category_child_route": child_route.get("model")}
                        # The bound category's own live form is the only source of its facet
                        # groups: the writing category's ids never apply to a development
                        # category, which renders an entirely different, previously unknown set
                        # (e.g. 基本対応範囲/言語 on the app-development subcategory). A category
                        # that renders none has nothing to choose.
                        facet_form = storefront_draft.read_category_form(
                            getattr(args, "default_tab_script", DEFAULT_TAB),
                            draft_id, blueprint["category"],
                        )
                        facet_groups = facet_form.get("facet_groups") or {}
                        resolved_facets: dict[str, list[str]] = {}
                        if facet_groups:
                            facet_choice, facet_route = _invoke_facet_proposal(
                                runner=getattr(args, "runner", DEFAULT_RUNNER),
                                schema=getattr(
                                    args, "facet_proposal_schema", DEFAULT_FACET_PROPOSAL_SCHEMA),
                                workdir=args.workdir,
                                evidence_dir=inventory_path.parent / "facet-proposal-agent",
                                listing={
                                    "title_stem": create_proposal.get("title_stem"),
                                    "catchphrase": create_proposal.get("catchphrase"),
                                    "head": create_proposal.get("head"),
                                    "body": create_proposal.get("body"),
                                    "category": blueprint["category"],
                                },
                                facet_groups=facet_groups, timeout_seconds=args.timeout_seconds,
                            )
                            resolved_facets = _validate_facet_selection(facet_choice, facet_groups)
                            demand_derivation = {**(demand_derivation or {}),
                                                 "facet_group_ids": sorted(facet_groups),
                                                 "facet_route": facet_route.get("model")}
                        blueprint = {**blueprint, "category_specific": {
                            **blueprint["category_specific"], "facets": resolved_facets,
                        }}
                    try:
                        new_listing_contract = recovered_create_contract or _seal_create_contract(
                            create_proposal, source=create_source, family_name=create_family,
                            family=create_template,
                            allowed_refs=create_allowed_refs, blueprint=blueprint,
                            seller_snapshot=create_seller_snapshot,
                            draft_service_id=str(create_draft_claim["draft_service_id"]),
                            evidence_dir=inventory_path.parent / "create-contract",
                        )
                    except RuntimeError as error:
                        # A malformed CREATE candidate is a rejected candidate, not a broken
                        # wake, exactly like a rejected IMPROVE proposal above: the guards did
                        # their job (a prohibited tool name or a non-continuative title stem has
                        # cost this account a live listing before), so record why and let the
                        # wake complete as a no-op instead of dying the way the unguarded call
                        # used to -- twelve full wakes in a row did exactly that on
                        # 2026-09-06. The blank draft claimed above is not lost: it is a real
                        # untitled draft on the platform, and `create_or_claim_blank_draft`
                        # discovers and reclaims it directly from the seller's own drafts list on
                        # a later wake, so no ledger row is needed to remember it here.
                        create_rejected = str(error)[:160]
                        _append_proposal_rejection(
                            args.state_dir, gap_key=create_gap_key, rejection=create_rejected,
                            proposed_value=_create_rejected_proposed_value(create_proposal),
                            pass_id=pass_id,
                        )
                        return 0, _persist_receipt(args, output, _receipt(
                            pass_id, status="completed", reason=create_rejected,
                            effect=0, readback=0,
                        ))
                    if new_listing_contract is None:
                        raise RuntimeError("storefront_create_contract_missing")
                    _atomic_write(inventory_path.parent / "generated-create-contract.json",
                                  new_listing_contract)
            candidate_id = new_listing_contract["draft_service_id"]
            candidate_public = candidate_id in inventory_ids
            duplicate_title = any(
                str(service.get("title") or "") == new_listing_contract["public_fields"]["expected_title"]
                and str(service.get("service_id") or "") != candidate_id
                for service in inventory["services"] if isinstance(service, dict)
            )
            known_draft_image_identity = None
            draft_ledger_path = args.state_dir / "new-listing-drafts.jsonl"
            platform_withdrawn = _platform_withdrew_listing(
                draft_ledger_path, candidate_id, candidate_public)
            if draft_ledger_path.exists():
                for line in reversed(draft_ledger_path.read_text(encoding="utf-8").splitlines()):
                    try:
                        prior_draft = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise RuntimeError("new_listing_draft_ledger_invalid") from error
                    if (prior_draft.get("contract_sha256") == new_listing_contract["contract_sha256"]
                            and prior_draft.get("status") == "published"):
                        known_draft_image_identity = str(prior_draft.get("public_image_identity") or "")
                        if not known_draft_image_identity:
                            try:
                                prior_public = json.loads(
                                    Path(str(prior_draft["evidence_path"])).read_text(encoding="utf-8")
                                )
                            except (OSError, KeyError, json.JSONDecodeError) as error:
                                raise RuntimeError("published_draft_evidence_missing") from error
                            for image_url in prior_public.get("images") or []:
                                match = re.search(
                                    r"service_images/original/([A-Za-z0-9-]+\.(?:png|jpe?g|webp))",
                                    str(image_url), re.IGNORECASE,
                                )
                                if match is not None:
                                    known_draft_image_identity = match.group(1)
                                    break
                        if not known_draft_image_identity:
                            raise RuntimeError("published_draft_image_identity_missing")
                        break
            # A listing awaiting a compliance repair is known to differ from its contract, because
            # the contract is what the copy has to become. Verifying that difference as a fault
            # would stop the wake that performs the repair.
            awaiting_repair = any(str(row.get("service_id") or "") == candidate_id
                                  for row in compliance_violations)
            # A creation contract describes what a listing was published as, not what it must
            # stay. Every accepted improvement moves the live copy away from it, so once the
            # loop has changed a listing that contract is history rather than a precondition.
            improved_since_publication = (args.state_dir / "effects.jsonl").exists() and any(
                json.loads(line).get("service_id") == candidate_id
                and json.loads(line).get("status") == "accepted"
                and json.loads(line).get("effect") == 1
                for line in (args.state_dir / "effects.jsonl").read_text(
                    encoding="utf-8").splitlines() if line.strip()
            )
            if (args.effect and not candidate_public and not platform_withdrawn
                    and not create_effect_this_wake
                    and pending_effect is None and not retire_attempted_this_wake
                    and not awaiting_repair and not improved_since_publication):
                blocked = _persist_effect_block(
                    args, output, pass_id, "before_new_listing_draft",
                )
                if blocked is not None:
                    return 0, blocked
            draft_result = ({
                "version": 1, "candidate_key": new_listing_contract["candidate_key"],
                "contract_sha256": new_listing_contract["contract_sha256"],
                "draft_service_id": candidate_id,
                "status": "compliance_repair_pending" if awaiting_repair else "improved_since_publication",
                "effect": 0, "readback": 0, "public_effect": 0,
            } if awaiting_repair or improved_since_publication else storefront_draft.readback_published_draft(
                new_listing_contract,
                getattr(args, "default_tab_script", DEFAULT_TAB),
                inventory_path.parent,
                known_image_identity=known_draft_image_identity,
            ) if candidate_public else (
                storefront_draft.prepare_draft(
                    new_listing_contract,
                    getattr(args, "default_tab_script", DEFAULT_TAB),
                    inventory_path.parent,
                )
                if (args.effect and not platform_withdrawn
                        and pending_effect is None and not retire_attempted_this_wake) else {
                    "version": 1,
                    "candidate_key": new_listing_contract["candidate_key"],
                    "contract_sha256": new_listing_contract["contract_sha256"],
                    "draft_service_id": new_listing_contract["draft_service_id"],
                    "status": "platform_withdrawn" if platform_withdrawn else "effect_disabled",
                    "effect": 0,
                    "readback": 0,
                    "public_effect": 0,
                }
            ))
            # Creating the blank draft is worth recording, but it must not overwrite what
            # filling the draft just reported. Before this wake could fill and publish in one
            # pass, `prepare_draft` never ran alongside a create, so relabelling was safe; now
            # it does, and clobbering its status would hide the real stage the draft reached.
            if create_effect_this_wake:
                draft_result = {
                    "status": "draft_created", "effect": 1, "public_effect": 0,
                    **draft_result, "draft_created_this_wake": True,
                }
            # A blank draft and a filled draft are both invisible to buyers -- only publication
            # is an effect the market can see, and the one-effect fence exists to bound what the
            # market sees. Counting the draft stages meant a wake spent its whole budget creating
            # a page nobody could reach, then two more wakes to fill and publish it, and a draft
            # that sits unpublished earns nothing.
            draft_effect_this_wake = int(draft_result.get("public_effect") or 0) == 1
            conflicting_hypothesis = (
                next_hypothesis
                if next_hypothesis is not None
                and str(next_hypothesis.get("service_id") or "") == candidate_id
                and next_hypothesis.get("guard_reason")
                else None
            )
            publication_guard = (
                "already_public" if candidate_public
                else "platform_withdrew_listing" if platform_withdrawn
                else "duplicate_listing_title" if duplicate_title
                else "catalog_capacity_exhausted" if observed >= 20
                    else "existing_listing_effect_open" if pending_effect is not None
                else "effect_already_this_wake" if retire_attempted_this_wake
                else str(conflicting_hypothesis["guard_reason"])
                if conflicting_hypothesis is not None else None
            )
            if publication_guard is not None:
                draft_result = {**draft_result, "publication_guard": publication_guard}
            elif args.effect:
                blocked = _persist_effect_block(
                    args, output, pass_id, "before_new_listing_publish",
                )
                if blocked is not None:
                    return 0, blocked
                draft_result = storefront_draft.publish_draft(
                    new_listing_contract,
                    getattr(args, "default_tab_script", DEFAULT_TAB),
                    inventory_path.parent,
                )
            # A listing derived from a market cluster must record that cluster, not the committed
            # contract it borrowed its policy from. Recording the wrong one left the Excel cluster
            # looking unused and produced a second near-identical Excel listing.
            draft_result = {**draft_result,
                            "capability_family": create_family or capability_families.get(candidate_id),
                            "blank_draft_claim": create_draft_claim,
                            "demand_evidence_path": (
                                cluster_blueprint["demand_evidence_path"]
                                if cluster_blueprint is not None else demand_evidence_path)}

            for name in STATE_FILES:
                path = args.state_dir / name
                if not path.exists():
                    path.touch(mode=0o600)
            contract_count = 0
            for contract in validated_contracts:
                contract_count += int(_append_contract_once(
                    args.state_dir / "offer-contracts.jsonl",
                    contract,
                ))
            listing_contract_count = 0
            for contract in listing_contracts:
                listing_contract_count += int(_append_key_once(
                    args.state_dir / "listing-contracts.jsonl", "contract_key", contract,
                ))
            listing_contract_total = sum(
                1 for line in (args.state_dir / "listing-contracts.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            draft_result = {
                **draft_result,
                "draft_event_key": (
                    f"{draft_result.get('contract_sha256')}:{draft_result.get('status')}"
                ),
            }
            _append_key_once(
                args.state_dir / "new-listing-drafts.jsonl",
                "draft_event_key",
                draft_result,
            )
            accepted_effect = int(
                pending_effect is not None or retire_effect_this_wake
                or create_effect_this_wake or draft_effect_this_wake
            )
            accepted_readback = int(
                bool((create_draft_claim or {}).get("readback")) or retire_confirmed_this_wake
            )
            if pending_effect is not None:
                effect_row = {
                    "version": 1, "status": "accepted", "effect": 1,
                    "accepted_at_epoch": int(time.time()),
                    "pass_id": pass_id, "effect_origin_pass_id": (
                        json.loads(pending_effect["intent_path"].read_text(encoding="utf-8"))
                        .get("effect_origin_pass_id", pass_id)
                    ),
                    "service_id": str(judgement["service_id"]), "changed_field": pending_effect["changed_field"],
                    "offer_digest": (next_hypothesis or {}).get("offer_digest"),
                    "before_value": judgement.get("before_value"), "after_value": judgement.get("proposed_value"),
                    "experiment_key": judgement["experiment_key"],
                    "public_before_path": str(pending_effect["public_before_path"]),
                    "public_after_path": str(pending_effect["public_after_path"]),
                    "seller_form_before_path": str(pending_effect["seller_form_before_path"]),
                    "seller_form_after_path": str(pending_effect["seller_form_after_path"]),
                    "recovered": pending_effect["recovered"],
                }
                if pending_effect["changed_field"] == "FAQ":
                    effect_row.update({
                        "question": pending_effect["question"], "answer": pending_effect["answer"],
                    })
                elif pending_effect["changed_field"] == "image":
                    effect_row.update({
                        "asset_sha256": judgement.get("proposed_value"),
                        "public_image_ids": json.loads(
                            Path(pending_effect["public_after_path"]).read_text(encoding="utf-8")
                        ).get("service_image_ids"),
                    })
                else:
                    effect_row["contract_sha256"] = str(
                        json.loads(pending_effect["intent_path"].read_text(encoding="utf-8"))
                        .get("mutation_contract", {}).get("contract_sha256") or ""
                    )
                appended = _append_effect_once(args.state_dir / "effects.jsonl", effect_row)
                _append_effect_once(args.state_dir / "experiments.jsonl", {
                    "version": 1, "status": "accepted", "experiment_key": judgement["experiment_key"],
                    "service_id": str(judgement["service_id"]), "changed_field": pending_effect["changed_field"],
                    "offer_digest": (next_hypothesis or {}).get("offer_digest"),
                    "accepted_at_epoch": effect_row["accepted_at_epoch"],
                    "success_metric": judgement.get("success_metric"),
                    "observation_window_days": judgement.get("observation_window_days"),
                    "baseline_analytics_snapshot_key": analytics["snapshot_key"],
                })
                intent = json.loads(pending_effect["intent_path"].read_text(encoding="utf-8"))
                _atomic_write(pending_effect["intent_path"], {
                    **intent, "status": "confirmed", "confirmed_at_epoch": int(time.time()),
                    "effect_ledger_appended": appended,
                })
                accepted_effect = int(appended and not pending_effect["recovered"])
                accepted_readback = 1
            outcome = _close_outcome(
                args.state_dir, analytics,
                getattr(args, "reply_transcripts", DEFAULT_REPLY_TRANSCRIPTS),
                getattr(args, "scorecard", DEFAULT_SCORECARD), int(time.time()),
            )
            if next_hypothesis is not None:
                _append_key_once(
                    args.state_dir / "prepared-hypotheses.jsonl", "hypothesis_key", next_hypothesis,
                )
            catalog_baseline = _catalog_conversion_baseline(
                args.state_dir / "analytics.jsonl", validated_contracts,
            )
            row = _receipt(
                pass_id,
                status=("delivery_unknown" if retire_attempted_this_wake and not retire_confirmed_this_wake
                        else "completed"),
                reason=("public_accepted" if pending_effect is not None
                        else "draft_created" if create_effect_this_wake
                        else "retire_delivery_unknown" if retire_attempted_this_wake and not retire_confirmed_this_wake
                        else "retire_reconciled" if retire_confirmed_this_wake and not retire_effect_this_wake
                        else "retire_accepted" if retire_effect_this_wake
                        else "draft_prepared" if draft_effect_this_wake
                        else "judgement_ready" if judgement["decision"] == "change"
                        else str(judgement["no_op_reason"])),
                decision=judgement["decision"],
                actionable=int(judgement["decision"] == "change"),
                effect=accepted_effect,
                readback=accepted_readback,
                official_services_read=observed,
                offer_contracts_appended=contract_count,
                listing_contracts_appended=listing_contract_count,
                listing_contracts_active=len(listing_contracts),
                listing_contracts_total=listing_contract_total,
                competitor_evidence_count=len(competitor_manifest["sources"]),
                # A wake that skipped ten sources must not look identical to one that
                # read all fourteen: the count alone cannot tell them apart, so the
                # skip count rides in the same receipt rather than a new channel.
                competitor_evidence_unread_count=len(competitor_manifest["unread"]),
                inventory_content_sha256=inventory.get("content_sha256"),
                judgement_path=str(judgement_path),
                service_id=judgement.get("service_id"),
                changed_field=judgement.get("changed_field"),
                experiment_key=judgement.get("experiment_key"),
                public_after_path=(str(pending_effect["public_after_path"]) if pending_effect else None),
                recovered_effect=bool(
                    (pending_effect and pending_effect["recovered"])
                    or (retire_result or {}).get("recovered")
                ),
                analytics_snapshot_key=analytics["snapshot_key"],
                catalog_analytics=analytics.get("catalog_metrics"),
                catalog_conversion_baseline=catalog_baseline,
                funnel=funnel,
                portfolio=portfolio,
                new_listing_draft=draft_result,
                demand_derivation=demand_derivation,
                stale_listing_contracts=list(_stale_listing_contracts),
                outcome=outcome,
                next_hypothesis=next_hypothesis,
                proposal_agent=proposal_agent,
                mutation_renders=[{
                    "changed_field": render["contract"]["changed_field"],
                    "service_id": render["contract"]["service_id"],
                    "contract_sha256": render["contract"]["contract_sha256"],
                    "delta": render["delta"], "published": False, "evidence_path": str(path),
                } for render, path in (
                    (image_render, image_render_path),
                    (title_render, title_render_path), (body_render, body_render_path),
                    (package_render, package_render_path), (faq_render, faq_render_path),
                    (price_render, price_render_path),
                ) if render is not None] + ([{
                    "changed_field": generated_render["contract"]["changed_field"],
                    "service_id": generated_render["contract"]["service_id"],
                    "contract_sha256": generated_render["contract"]["contract_sha256"],
                    "delta": generated_render["delta"], "published": False,
                    "evidence_path": str(generated_render_path), "generated": True,
                }] if generated_render is not None else []),
                lease={
                    "task": task,
                    "context_id": lease.get("context_id"),
                    "target_id": lease.get("target_id"),
                    "generation": lease.get("generation"),
                    "released": True,
                },
            )
            row = _persist_receipt(args, output, row)
            return 0, row
        except Exception as error:
            if lease is not None and not released:
                try:
                    release = _lease(args.lease_script, "release", task, lease)
                    released = release.get("released") == task
                except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
                    pass
            reason = str(error).strip() or type(error).__name__
            status, returncode = _storefront_failure_disposition(reason)
            row = _receipt(pass_id, status=status, reason=reason,
                           lease={"task": task, "released": released} if lease is not None else None)
            row = _persist_receipt(args, output, row)
            return returncode, row
        finally:
            if lease is not None and not released:
                try:
                    _lease(args.lease_script, "release", task, lease)
                except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError):
                    pass


def build_parser() -> argparse.ArgumentParser:
    """The one place the runtime contract is declared, so tests cannot drift from it."""
    storefront = _storefront_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--operator-brake", type=Path, default=Path(os.environ.get("GIG_OPERATOR_BRAKE_FILE", DEFAULT_BRAKE)))
    parser.add_argument("--lease-script", type=Path, default=DEFAULT_LEASE)
    parser.add_argument("--ensure-browser-script", type=Path, default=DEFAULT_ENSURE_BROWSER)
    parser.add_argument("--default-tab-script", type=Path, default=DEFAULT_TAB)
    parser.add_argument("--runner", type=Path, default=DEFAULT_RUNNER)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--proposal-schema", type=Path, default=DEFAULT_PROPOSAL_SCHEMA)
    parser.add_argument("--create-proposal-schema", type=Path, default=DEFAULT_CREATE_PROPOSAL_SCHEMA)
    parser.add_argument("--demand-proposal-schema", type=Path, default=DEFAULT_DEMAND_PROPOSAL_SCHEMA)
    parser.add_argument("--category-proposal-schema", type=Path, default=DEFAULT_CATEGORY_PROPOSAL_SCHEMA)
    parser.add_argument("--category-child-schema", type=Path, default=DEFAULT_CATEGORY_CHILD_SCHEMA)
    parser.add_argument("--facet-proposal-schema", type=Path, default=DEFAULT_FACET_PROPOSAL_SCHEMA)
    parser.add_argument("--bootstrap-selection-schema", type=Path, default=DEFAULT_BOOTSTRAP_SELECTION_SCHEMA)
    parser.add_argument("--bootstrap-listing-schema", type=Path, default=DEFAULT_BOOTSTRAP_LISTING_SCHEMA)
    parser.add_argument("--bootstrap-import-schema", type=Path, default=DEFAULT_BOOTSTRAP_IMPORT_SCHEMA)
    parser.add_argument("--duplicate-judgement-schema", type=Path, default=DEFAULT_DUPLICATE_JUDGEMENT_SCHEMA)
    parser.add_argument("--scorecard", type=Path, default=storefront["scorecard"])
    parser.add_argument("--image-contract", type=Path, default=storefront["image"])
    parser.add_argument("--gallery-contract", type=Path, default=storefront["gallery"])
    parser.add_argument("--title-mutation", type=Path, default=storefront["title"])
    parser.add_argument("--body-mutation", type=Path, default=storefront["body"])
    parser.add_argument("--scope-mutation", type=Path, default=storefront["scope"])
    parser.add_argument("--package-mutation", type=Path, default=storefront["package"])
    parser.add_argument("--faq-mutation", type=Path, default=storefront["faq"])
    parser.add_argument("--price-mutation", type=Path, default=storefront["price"])
    parser.add_argument("--listing-contract-dir", type=Path, default=storefront["listings"])
    parser.add_argument(
        "--listing-contract-families", type=Path, default=storefront["families"],
    )
    parser.add_argument("--new-listing-contract", type=Path, default=storefront["new_listing"])
    parser.add_argument("--reply-transcripts", type=Path, default=DEFAULT_REPLY_TRANSCRIPTS)
    parser.add_argument("--applied", type=Path, default=DEFAULT_APPLIED)
    parser.add_argument("--earnings", type=Path, default=DEFAULT_EARNINGS)
    parser.add_argument("--projects-dir", type=Path, default=DEFAULT_PROJECTS)
    parser.add_argument("--negotiate-run-log", type=Path, default=DEFAULT_NEGOTIATE_RUN_LOG)
    parser.add_argument("--workdir", type=Path, default=Path.home())
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument(
        "--capability-evidence", type=Path, action="append",
        default=_capability_evidence_defaults(),
    )
    parser.add_argument("--effect", action="store_true")
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--auto-cadence", action="store_true")
    parser.add_argument("--full-interval-seconds", type=int, default=1800)
    parser.add_argument("--accounting-cutoff-epoch", type=int, default=0)
    parser.add_argument("--expected-catalog-sha256", default="")
    parser.add_argument("--pass-id")
    parser.add_argument("--telegram-database", type=Path, default=DEFAULT_TELEGRAM_DATABASE)
    parser.add_argument("--telegram-receipt-dir", type=Path, default=DEFAULT_TELEGRAM_RECEIPTS)
    parser.add_argument("--telegram-target", default=os.environ.get("GIG_REPORT_CHAT", ""))
    parser.add_argument("--openclaw", type=Path, default=Path("/opt/homebrew/bin/openclaw"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    code, row = run_once(args)
    print(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
