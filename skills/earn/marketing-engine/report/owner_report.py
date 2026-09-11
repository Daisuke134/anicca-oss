#!/usr/bin/env python3
"""Deterministic, product-scoped Japanese owner reports.

This module intentionally has no network or model dependency.  It reads only
the canonical JSONL ledgers below the supplied Marketing Engine state root,
derives a small fact object, and persists append-only report and delivery rows.
"""

from __future__ import annotations

import copy
import datetime as dt
import fcntl
import hashlib
import json
import pathlib
import sys
from collections import Counter
from typing import Callable, Iterable


ENGINE_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "gates"))
from product_router import PRODUCT_ID_ALIASES, canonical_product_id  # noqa: E402


SCHEMA_VERSION = "marketing.owner-report.v1"
DELIVERY_SCHEMA_VERSION = "marketing.owner-delivery.v1"
PRODUCTS = ("anicca-ios", "honne-ai", "ebook-ja", "ebook-en")
KINDS = (
    "action",
    "checkpoint",
    "product_daily",
    "incident",
    "experiment",
    "portfolio_weekly",
)
_PRODUCT_SET = frozenset(PRODUCTS)
_KIND_SET = frozenset(KINDS)


class OwnerReportError(ValueError):
    """Invalid or unsafe owner-report state."""


class ConflictError(OwnerReportError):
    """A message key was already used by different canonical facts."""


class DeliveryError(OwnerReportError):
    """A Telegram receipt cannot prove delivery."""


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_jsonl(path: pathlib.Path) -> list[dict]:
    """Load valid JSON objects from one canonical JSONL file.

    A malformed/blank line is ignored in the same way as the older canonical
    readers.  No fallback path is consulted: callers control the sole state
    root and therefore cannot accidentally read a legacy aggregate library.
    """

    path = pathlib.Path(path)
    if not path.is_file():
        return []
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _indexed_rows(root: pathlib.Path, filename: str) -> list[tuple[int, dict]]:
    return list(enumerate(load_jsonl(pathlib.Path(root) / filename)))


def _parse_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _as_of(value: dt.datetime) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise OwnerReportError("as_of must be a datetime")
    if value.tzinfo is None:
        raise OwnerReportError("as_of must include a timezone")
    return value.astimezone(dt.timezone.utc)


def _timestamp(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date_key(row: dict, *fields: str) -> str:
    for field in fields:
        value = row.get(field)
        if isinstance(value, str) and value:
            return value
    return ""


def _ref(filename: str, index: int) -> str:
    return f"state/{filename}#row-{index}"


def _validate_product(product_id: str | None, *, portfolio: bool = False) -> None:
    if portfolio:
        if product_id is not None:
            raise OwnerReportError("portfolio_weekly has product_id=null")
        return
    if product_id is not None and product_id not in _PRODUCT_SET:
        raise OwnerReportError(f"unknown product_id: {product_id}")


def _validate_kind(kind: str) -> None:
    if kind not in _KIND_SET:
        raise OwnerReportError(f"unknown report kind: {kind}")


def _event(
    *,
    kind: str,
    product_id: str | None,
    as_of: dt.datetime,
    message_key: str,
    facts: dict,
    evidence_refs: Iterable[str],
) -> dict:
    _validate_kind(kind)
    _validate_product(product_id, portfolio=kind == "portfolio_weekly")
    event = {
        "schema_version": SCHEMA_VERSION,
        "message_key": message_key,
        "kind": kind,
        "product_id": product_id,
        "as_of": _timestamp(as_of),
        "facts": copy.deepcopy(facts),
        "evidence_refs": list(dict.fromkeys(str(ref) for ref in evidence_refs)),
    }
    _validate_event(event)
    return event


def _validate_event(event: dict) -> dict:
    if not isinstance(event, dict):
        raise OwnerReportError("event must be an object")
    required = {"schema_version", "message_key", "kind", "product_id", "as_of", "facts", "evidence_refs"}
    missing = sorted(required - set(event))
    if missing:
        raise OwnerReportError(f"missing owner-report fields: {', '.join(missing)}")
    if event["schema_version"] != SCHEMA_VERSION:
        raise OwnerReportError("unsupported owner-report schema_version")
    if not isinstance(event["message_key"], str) or not event["message_key"]:
        raise OwnerReportError("message_key must be a non-empty string")
    _validate_kind(event["kind"])
    _validate_product(event["product_id"], portfolio=event["kind"] == "portfolio_weekly")
    if event["kind"] != "portfolio_weekly" and event["product_id"] is None:
        raise OwnerReportError(f"{event['kind']} requires product_id")
    parsed = _parse_time(event["as_of"])
    if parsed is None:
        raise OwnerReportError("as_of must be an RFC3339 timestamp with timezone")
    if not isinstance(event["facts"], dict):
        raise OwnerReportError("facts must be an object")
    refs = event["evidence_refs"]
    if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref for ref in refs):
        raise OwnerReportError("evidence_refs must be a non-empty string array")
    return copy.deepcopy(event)


def _semantic_event(event: dict) -> dict:
    """Return the immutable fact identity used for replay comparisons.

    ``as_of`` records when a sweep observed the row, but is not itself a new
    fact.  A later sweep may therefore reuse the same message key and receipt;
    any change to facts or evidence still fails closed.
    """

    return {key: value for key, value in event.items() if key != "as_of"}


def _scoped(rows: list[tuple[int, dict]], product_id: str) -> list[tuple[int, dict]]:
    return [
        (index, row)
        for index, row in rows
        if canonical_product_id(row.get("product_id")) == product_id
    ]


def _before(row: dict, as_of: dt.datetime) -> bool:
    observed = _parse_time(row.get("observed_at")) or _parse_time(row.get("published_at"))
    return observed is None or observed <= as_of


def _latest(rows: list[tuple[int, dict]], as_of: dt.datetime, *fields: str) -> tuple[int, dict] | None:
    eligible = [(index, row) for index, row in rows if _before(row, as_of)]
    if not eligible:
        return None
    return max(eligible, key=lambda pair: (_date_key(pair[1], *fields), pair[0]))


def _latest_metric_snapshots(
    rows: list[tuple[int, dict]], as_of: dt.datetime
) -> list[tuple[int, dict]]:
    """Keep the newest append-only snapshot for each publication checkpoint."""

    latest: dict[tuple[object, object], tuple[int, dict]] = {}
    for index, row in rows:
        if not _before(row, as_of):
            continue
        key = (
            row.get("publication_id") or row.get("postiz_id"),
            row.get("target_age_hours"),
        )
        latest[key] = (index, row)
    return sorted(latest.values(), key=lambda pair: pair[0])


def _matching_identity(
    identity_rows: list[tuple[int, dict]],
    source: dict,
    product_id: str,
) -> list[tuple[int, dict]]:
    native_id = source.get("native_post_id")
    postiz_id = source.get("postiz_post_id") or source.get("postiz_id")
    matched = []
    for index, row in identity_rows:
        if (native_id and row.get("native_post_id") == native_id) or (
            postiz_id and row.get("postiz_post_id") == postiz_id
        ):
            bound = canonical_product_id(row.get("product_id"))
            if bound is not None and bound != product_id:
                raise OwnerReportError(
                    f"cross-product publication identity for {native_id or postiz_id}"
                )
            matched.append((index, row))
    return matched


def _existing_owner_report(
    root: pathlib.Path,
    *,
    kind: str,
    product_id: str,
    message_key: str,
) -> dict | None:
    """Return a valid canonical report already recorded for one immutable key."""

    equivalent_keys = {message_key}
    for legacy_id, canonical_id in PRODUCT_ID_ALIASES.items():
        if product_id == canonical_id:
            equivalent_keys.add(message_key.replace(f":{canonical_id}:", f":{legacy_id}:"))
    for row in load_jsonl(pathlib.Path(root) / "owner-reports.jsonl"):
        if (
            row.get("kind") == kind
            and canonical_product_id(row.get("product_id")) == product_id
            and row.get("message_key") in equivalent_keys
        ):
            try:
                normalized = copy.deepcopy(row)
                normalized["product_id"] = canonical_product_id(row.get("product_id"))
                return _validate_event(normalized)
            except OwnerReportError:
                continue
    return None


def _existing_owner_report_for_evidence(
    root: pathlib.Path,
    *,
    kind: str,
    product_id: str,
    evidence_ref: str,
) -> dict | None:
    """Return the prior report for an evidence row, including legacy keys."""

    for row in load_jsonl(pathlib.Path(root) / "owner-reports.jsonl"):
        refs = row.get("evidence_refs")
        if (
            row.get("kind") == kind
            and canonical_product_id(row.get("product_id")) == product_id
            and isinstance(refs, list)
            and evidence_ref in refs
        ):
            try:
                normalized = copy.deepcopy(row)
                normalized["product_id"] = canonical_product_id(row.get("product_id"))
                return _validate_event(normalized)
            except OwnerReportError:
                continue
    return None


def _message_key_exists(root: pathlib.Path, message_key: str) -> bool:
    """Return whether an append-only report or delivery ledger reserves a key."""

    for filename in ("owner-reports.jsonl", "owner-report-deliveries.jsonl"):
        if any(row.get("message_key") == message_key for row in load_jsonl(pathlib.Path(root) / filename)):
            return True
    return False


def _social_checkpoint_incident_base_key(product_id: str, facts: dict) -> str:
    return (
        f"incident:{product_id}:social_checkpoint:"
        f"{facts['publication_id']}:{facts['reason']}"
    )


def _social_checkpoint_incident_key(
    root: pathlib.Path,
    product_id: str,
    index: int,
    row: dict,
    facts: dict,
    base_counts: Counter[str],
    used_keys: set[str],
) -> str:
    """Choose an immutable key without stealing a legacy terminal receipt.

    A historical report tied to this exact evidence row keeps its original
    key.  A lone new incident keeps the legacy base format; only colliding new
    rows receive a target-age suffix (and, for an impossible same-age
    duplicate, an evidence-row suffix).
    """

    evidence_ref = _ref("post-metrics.jsonl", index)
    existing = _existing_owner_report_for_evidence(
        root,
        kind="incident",
        product_id=product_id,
        evidence_ref=evidence_ref,
    )
    if existing is not None:
        key = existing["message_key"]
        used_keys.add(key)
        return key

    base_key = _social_checkpoint_incident_base_key(product_id, facts)
    if (
        base_counts[base_key] == 1
        and base_key not in used_keys
        and not _message_key_exists(root, base_key)
    ):
        used_keys.add(base_key)
        return base_key

    target_age = row.get("target_age_hours")
    target_key = "unknown" if target_age is None else str(target_age)
    key = f"{base_key}:{target_key}"
    if key in used_keys or _message_key_exists(root, key):
        row_key = f"{key}:row-{index}"
        if row_key in used_keys or _message_key_exists(root, row_key):
            evidence_digest = hashlib.sha256(
                f"post-metrics.jsonl#row-{index}".encode("utf-8")
            ).hexdigest()[:12]
            row_key = f"{row_key}:{evidence_digest}"
        key = row_key
    used_keys.add(key)
    return key


def _action_events(root: pathlib.Path, product_id: str, as_of: dt.datetime) -> list[dict]:
    identities = _indexed_rows(root, "publication-identity.jsonl")
    attributions = _indexed_rows(root, "experiment-attribution.jsonl")
    candidates: list[tuple[str, int, dict, str, str]] = []

    # A publication identity is eligible only when the immutable publication
    # row itself is product-bound.  Product-null historical rows are never
    # assigned by account name, URL, or temporal proximity.
    for index, row in _scoped(identities, product_id):
        if row.get("postiz_state") != "PUBLISHED" or row.get("identity_status") != "resolved":
            continue
        native_url = row.get("native_post_url")
        if not native_url or not row.get("native_post_id") or not _before(row, as_of):
            continue
        candidates.append((
            _date_key(row, "publish_date", "observed_at"),
            index,
            row,
            _ref("publication-identity.jsonl", index),
            "identity",
        ))

    # Attribution rows are product-bound canonical publication facts even when
    # an older identity row is deliberately unbound.  A conflicting bound
    # identity is rejected rather than silently borrowed.
    for index, row in _scoped(attributions, product_id):
        if not row.get("native_post_url") or not row.get("native_post_id") or not _before(row, as_of):
            continue
        matched = _matching_identity(identities, row, product_id)
        if matched and any(
            identity.get("postiz_state") != "PUBLISHED" or identity.get("identity_status") != "resolved"
            for _, identity in matched
        ):
            # A provider queue or unresolved identity is not a publication
            # receipt, even if a stale attribution row carries a URL.
            continue
        for identity_index, identity in matched:
            identity_url = identity.get("native_post_url")
            if identity_url and identity_url != row.get("native_post_url"):
                raise OwnerReportError("publication identity URL conflicts with attribution URL")
        candidates.append((
            _date_key(row, "published_at", "observed_at"),
            index,
            row,
            _ref("experiment-attribution.jsonl", index),
            "attribution",
        ))

    # Keep one action per native publication. A bound publication identity is
    # immutable publication proof and therefore wins over attribution rows. If
    # only attribution rows exist, keep the earliest canonical snapshot: later
    # observations may update experiment results, but must not conflict with
    # the already-recorded action for the same publication.
    unique: dict[str, tuple[str, int, dict, str, str]] = {}
    for candidate in candidates:
        native_id = str(candidate[2].get("native_post_id"))
        previous = unique.get(native_id)
        candidate_key = (0 if candidate[4] == "identity" else 1, candidate[0], candidate[1])
        previous_key = (
            (0 if previous[4] == "identity" else 1, previous[0], previous[1])
            if previous is not None
            else None
        )
        if previous is None or candidate_key < previous_key:
            unique[native_id] = candidate
    events = []
    for _, index, row, ref, _source_kind in sorted(
        unique.values(), key=lambda item: (item[0], item[1]), reverse=True
    ):
        native_id = row.get("native_post_id")
        facts = {
            "native_post_id": native_id,
            "native_url": row.get("native_post_url"),
            "publication_id": row.get("publication_id") or row.get("postiz_post_id") or row.get("postiz_id"),
            "experiment_id": row.get("experiment_id"),
            "platform": row.get("platform") or "unknown",
        }
        message_key = f"action:{product_id}:{native_id}"
        evidence_refs = [ref]
        existing = _existing_owner_report(
            root,
            kind="action",
            product_id=product_id,
            message_key=message_key,
        )
        existing_facts = existing.get("facts") if existing is not None else None
        if (
            isinstance(existing_facts, dict)
            and existing_facts.get("native_post_id") is not None
            and str(existing_facts.get("native_post_id")) == str(native_id)
        ):
            # An action is an immutable notification.  Once a canonical report
            # exists, a later provider identity must not rewrite its facts or
            # evidence under the same message key.
            facts = copy.deepcopy(existing_facts)
            evidence_refs = copy.deepcopy(existing["evidence_refs"])
        events.append(_event(
            kind="action",
            product_id=product_id,
            as_of=as_of,
            message_key=message_key,
            facts=facts,
            evidence_refs=evidence_refs,
        ))
    return events


def _checkpoint_events(root: pathlib.Path, product_id: str, as_of: dt.datetime) -> list[dict]:
    rows = _latest_metric_snapshots(
        _scoped(_indexed_rows(root, "post-metrics.jsonl"), product_id), as_of
    )
    identities = _indexed_rows(root, "publication-identity.jsonl")
    events = []
    for index, row in sorted(rows, key=lambda pair: (_date_key(pair[1], "observed_at", "published_at"), pair[0])):
        if row.get("checkpoint_status") != "measured":
            continue
        # A metric row is accepted only when its publication identity is either
        # unbound legacy data (which cannot be reassigned) or bound to this
        # exact product.  A bound mismatch is a hard failure.
        matched_identity = _matching_identity(identities, row, product_id)
        reasons = row.get("metric_null_reasons") or {}
        facts = {
            "publication_id": row.get("publication_id") or row.get("postiz_id"),
            "native_post_id": row.get("native_post_id"),
            "native_url": row.get("native_url"),
            "platform": row.get("platform") or row.get("provider_identifier") or "unknown",
            "checkpoint_status": row.get("checkpoint_status") or ("measured" if row.get("views") is not None else "missed"),
            "target_age_hours": row.get("target_age_hours"),
            "views": row.get("views"),
            "impressions": row.get("impressions"),
            "reach": row.get("reach"),
            "likes": row.get("likes"),
            "comments": row.get("comments"),
            "shares": row.get("shares"),
            "saves": row.get("saves"),
            "reason": reasons.get("views") or row.get("error"),
        }
        suffix = f"{facts['publication_id']}:{facts['target_age_hours']}"
        if row.get("corrects_snapshot_id"):
            correction_id = str(row.get("snapshot_id") or "unknown")[:12]
            suffix += f":correction:{correction_id}"
        events.append(_event(
            kind="checkpoint",
            product_id=product_id,
            as_of=as_of,
            message_key=f"checkpoint:{product_id}:{suffix}",
            facts=facts,
            evidence_refs=[_ref("post-metrics.jsonl", index)]
            + [_ref("publication-identity.jsonl", identity_index) for identity_index, _ in matched_identity],
        ))
    return events


def _chart_value(data: dict, chart_name: str, metric_name: str) -> object:
    charts = data.get("charts") if isinstance(data, dict) else None
    chart = charts.get(chart_name) if isinstance(charts, dict) else None
    latest = chart.get("latest_complete") if isinstance(chart, dict) else None
    if isinstance(latest, dict):
        metric = latest.get(metric_name)
        if isinstance(metric, dict) and metric.get("incomplete") is not True:
            return metric.get("value")
        if isinstance(metric, (int, float)) and not isinstance(metric, bool):
            return metric
    return None


def _first_number(value: object, keys: tuple[str, ...]) -> object:
    if not isinstance(value, dict):
        return None
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return candidate
    return None


def _minor_money_buckets(data: object) -> list[dict]:
    """Read all canonical minor-unit currency buckets deterministically."""

    exponents = {"JPY": 0, "USD": 2}
    if not isinstance(data, dict):
        return []
    for field in ("net_minor", "gross_minor"):
        amounts = data.get(field)
        if not isinstance(amounts, dict):
            continue
        buckets = []
        for currency, minor in sorted(amounts.items(), key=lambda item: str(item[0]).upper()):
            if isinstance(minor, (int, float)) and not isinstance(minor, bool):
                normalized_currency = str(currency).upper()
                exponent = exponents.get(normalized_currency)
                if exponent is None:
                    # Preserve the provider's integer exactly; callers must
                    # not fabricate a decimal scale for an unrecognised code.
                    amount = None
                else:
                    amount = minor if exponent == 0 else minor / (10 ** exponent)
                buckets.append(
                    {
                        "currency": normalized_currency,
                        "metric": field.removesuffix("_minor"),
                        "minor": minor,
                        "value": amount,
                    }
                )
        if buckets:
            return buckets
    return []


def _minor_money(data: object) -> tuple[object, str | None, str | None, object] | None:
    """Backward-compatible first-bucket view for callers outside this module."""

    buckets = _minor_money_buckets(data)
    if not buckets:
        return None
    bucket = buckets[0]
    return bucket["value"], bucket["currency"], bucket["metric"], bucket["minor"]


def _business_facts(row: dict) -> dict:
    sources = row.get("sources") if isinstance(row.get("sources"), dict) else {}
    revenuecat = sources.get("revenuecat") if isinstance(sources.get("revenuecat"), dict) else {}
    rc_data = revenuecat.get("data") if isinstance(revenuecat.get("data"), dict) else {}
    mrr = _chart_value(rc_data, "mrr", "MRR")
    if mrr is None:
        mrr = _first_number(rc_data, ("mrr", "MRR"))
    active = _chart_value(rc_data, "actives", "Actives")
    if active is None:
        active = _first_number(rc_data, ("active_subscriptions", "actives", "Actives"))
    facts: dict = {
        "business_date": row.get("business_date"),
        "snapshot_id": row.get("snapshot_id"),
        "mrr": mrr,
        "mrr_source": "revenuecat" if revenuecat.get("status") == "available" and mrr is not None else None,
        "mrr_reason": None,
        "active_subscriptions": active,
        "sources": {},
        "paid_orders": None,
        "money_value": None,
        "money_currency": None,
        "money_minor": None,
        "money_metric": None,
    }
    if revenuecat.get("status") == "available" and mrr is None:
        facts["mrr_reason"] = "revenuecat_mrr_missing"
    elif revenuecat.get("status") != "available":
        facts["mrr_reason"] = revenuecat.get("reason") or "revenuecat_unavailable"

    # Keep every source status/reason visible for incidents without copying
    # provider payloads or inventing a money value.
    for name, source in sorted(sources.items()):
        if not isinstance(source, dict):
            continue
        facts["sources"][name] = {
            "status": source.get("status"),
            "reason": source.get("reason"),
        }

    if mrr is None:
        # Ebooks use the first successful product-scoped sales source.  A
        # successful zero is retained; unavailable money remains null.
        for name in ("stripe", "kdp", "gumroad"):
            source = sources.get(name)
            if not isinstance(source, dict):
                continue
            if source.get("status") != "available":
                continue
            data = source.get("data")
            paid_orders = _first_number(data, ("paid_orders", "orders"))
            if paid_orders is not None:
                facts["paid_orders"] = paid_orders
            minor_buckets = _minor_money_buckets(data)
            if minor_buckets:
                facts["money_source"] = name
                if len(minor_buckets) == 1:
                    bucket = minor_buckets[0]
                    facts["money_value"] = bucket["value"]
                    facts["money_currency"] = bucket["currency"]
                    facts["money_minor"] = bucket["minor"]
                    facts["money_metric"] = bucket["metric"]
                    facts["money_reason"] = (
                        None if bucket["value"] is not None else "unknown_currency_exponent"
                    )
                else:
                    # A product can have legitimate sales in multiple
                    # currencies. Keep every bucket and never invent a sum.
                    facts["money_buckets"] = minor_buckets
                    facts["money_reason"] = "multiple_currencies"
                break
            amount = _first_number(data, ("net_revenue", "gross_revenue", "sales"))
            if amount is not None:
                facts["money_value"] = amount
                facts["money_currency"] = data.get("currency") if isinstance(data, dict) else None
                facts["money_metric"] = "net" if _first_number(data, ("net_revenue",)) is not None else "gross"
                facts["money_source"] = name
                facts["money_reason"] = None
                break
            if paid_orders == 0:
                # A successful zero-order query is truthful, but the provider
                # did not return a currency amount to format.
                facts["money_value"] = 0.0
                facts["money_currency"] = data.get("currency") if isinstance(data, dict) else None
                facts["money_metric"] = "net"
                facts["money_source"] = name
                facts["money_reason"] = None
                break
        else:
            facts["money_value"] = None
            unavailable = next(
                (
                    (name, source.get("reason"))
                    for name in ("stripe", "kdp", "gumroad")
                    for source in [sources.get(name)]
                    if isinstance(source, dict) and source.get("status") != "available" and source.get("reason")
                ),
                (None, "money_source_unavailable"),
            )
            facts["money_source"], facts["money_reason"] = unavailable
    return facts


def _daily_events(root: pathlib.Path, product_id: str, as_of: dt.datetime) -> list[dict]:
    rows = _scoped(_indexed_rows(root, "business-outcomes.jsonl"), product_id)
    latest = _latest(rows, as_of, "business_date", "observed_at")
    if latest is None:
        evidence_ref = "state/business-outcomes.jsonl#no_business_snapshot"
        message_key = f"product_daily:{product_id}:no_business_snapshot:{as_of.date().isoformat()}"
        existing = _existing_owner_report(
            root, kind="product_daily", product_id=product_id, message_key=message_key
        )
        if existing is not None:
            return [existing]
        return [_event(
            kind="product_daily",
            product_id=product_id,
            as_of=as_of,
            message_key=message_key,
            facts={
                "business_date": None,
                "snapshot_id": None,
                "mrr": None,
                "mrr_source": None,
                "mrr_reason": "no_business_snapshot",
                "active_subscriptions": None,
                "paid_orders": None,
                "money_value": None,
                "money_currency": None,
                "money_minor": None,
                "money_metric": None,
                "money_source": None,
                "money_reason": "no_business_snapshot",
                "sources": {},
            },
            evidence_refs=[evidence_ref],
        )]
    index, row = latest
    facts = _business_facts(row)
    evidence_ref = _ref("business-outcomes.jsonl", index)
    existing = _existing_owner_report_for_evidence(
        root, kind="product_daily", product_id=product_id, evidence_ref=evidence_ref
    )
    if existing is not None:
        return [existing]
    return [_event(
        kind="product_daily",
        product_id=product_id,
        as_of=as_of,
        message_key=f"product_daily:{product_id}:{facts.get('business_date') or facts.get('snapshot_id')}",
        facts=facts,
        evidence_refs=[evidence_ref],
    )]


def _repair_for(source: str) -> str:
    repairs = {
        "kdp": "KDP認証を設定して再取得する",
        "gumroad": "Gumroadの読み取り設定を確認して再取得する",
        "stripe": "Stripeの商品ID設定を確認して再取得する",
        "revenuecat": "RevenueCatの製品別設定を確認して再取得する",
        "product_analytics": "読み取り権限と計測を確認して再取得する",
        "posthog": "PostHogの読み取り権限を設定して再取得する",
        "social_checkpoint": "ネイティブ投稿URLを確認して再収集する",
        "publication": "公開結果とネイティブIDを再照合する",
    }
    return repairs.get(source, "ソースを確認して再収集する")


_INCIDENT_STATUSES = frozenset({"unavailable", "error", "failed"})
_INCIDENT_SOURCE_ORDER = (
    "kdp",
    "stripe",
    "gumroad",
    "revenuecat",
    "product_analytics",
    "posthog",
)


def _business_incident_gaps(row: dict) -> list[dict]:
    sources = row.get("sources") if isinstance(row.get("sources"), dict) else {}
    ordered_sources = list(dict.fromkeys([*_INCIDENT_SOURCE_ORDER, *sorted(sources)]))
    gaps = []
    for source_name in ordered_sources:
        source = sources.get(source_name)
        if not isinstance(source, dict) or source.get("status") not in _INCIDENT_STATUSES:
            continue
        gaps.append(
            {
                "source": source_name,
                "reason": source.get("reason") or f"{source_name}_unavailable",
                "next_repair": _repair_for(source_name),
            }
        )
    return gaps


def _incident_message_key(product_id: str, business_date: object, facts: dict) -> str:
    """Build a replay-stable key from the latest business incident facts."""

    date_key = str(business_date or facts.get("snapshot_id") or "unknown")
    facts_digest = hashlib.sha256(_canonical(facts).encode("utf-8")).hexdigest()
    return f"incident:{product_id}:{date_key}:{facts_digest}"


def _incident_events(root: pathlib.Path, product_id: str, as_of: dt.datetime) -> list[dict]:
    events = []
    # Business-source failures are the actionable owner blocker.  Only the
    # latest eligible snapshot is current; historical rows are evidence, not
    # new incidents.  Aggregate every current gap so one product yields at
    # most one business incident event per sweep.
    latest = _latest(
        _scoped(_indexed_rows(root, "business-outcomes.jsonl"), product_id),
        as_of,
        "business_date",
        "observed_at",
    )
    if latest is not None:
        index, row = latest
        source_gaps = _business_incident_gaps(row)
        if source_gaps:
            facts = {
                "business_date": row.get("business_date"),
                "snapshot_id": row.get("snapshot_id"),
                "source_gaps": source_gaps,
            }
            events.append(
                _event(
                    kind="incident",
                    product_id=product_id,
                    as_of=as_of,
                    message_key=_incident_message_key(
                        product_id, facts.get("business_date"), facts
                    ),
                    facts=facts,
                    evidence_refs=[_ref("business-outcomes.jsonl", index)],
                )
            )

    # One current health event per product/platform/day replaces historical
    # per-checkpoint noise. Corrections supersede old missed rows before the
    # grouping is computed.
    social_rows = []
    current_metrics = _latest_metric_snapshots(
        _scoped(_indexed_rows(root, "post-metrics.jsonl"), product_id), as_of
    )
    for index, row in current_metrics:
        status = row.get("checkpoint_status")
        reason = (row.get("metric_null_reasons") or {}).get("views") or row.get("error")
        if status not in {"missed", "error", "failed"} and not reason:
            continue
        social_rows.append((index, row, reason or "checkpoint_failed"))

    by_platform: dict[str, list[tuple[int, dict, str]]] = {}
    for item in social_rows:
        platform = str(item[1].get("platform") or item[1].get("provider_identifier") or "unknown")
        by_platform.setdefault(platform, []).append(item)
    day = as_of.date().isoformat()
    for platform, failures in sorted(by_platform.items()):
        message_key = f"measurement_unhealthy:{product_id}:{platform}:{day}"
        facts = {
            "source": "social_measurement",
            "platform": platform,
            "reason": failures[0][2],
            "next_repair": _repair_for("social_checkpoint"),
            "affected_checkpoints": len(failures),
            "native_urls": list(dict.fromkeys(
                str(row.get("native_url"))
                for _index, row, _reason in failures
                if row.get("native_url")
            )),
        }
        evidence_refs = [
            _ref("post-metrics.jsonl", index) for index, _row, _reason in failures
        ]
        existing = _existing_owner_report(
            root, kind="incident", product_id=product_id, message_key=message_key
        )
        if existing is not None:
            facts = copy.deepcopy(existing["facts"])
            evidence_refs = copy.deepcopy(existing["evidence_refs"])
        events.append(_event(
            kind="incident",
            product_id=product_id,
            as_of=as_of,
            message_key=message_key,
            facts=facts,
            evidence_refs=evidence_refs,
        ))
    return events


def _experiment_events(root: pathlib.Path, product_id: str, as_of: dt.datetime) -> list[dict]:
    events = []
    identities = _indexed_rows(root, "publication-identity.jsonl")
    attributions = _scoped(_indexed_rows(root, "experiment-attribution.jsonl"), product_id)
    hook_rows = _scoped(_indexed_rows(root, "hook-perf.jsonl"), product_id)
    for index, row in attributions:
        if not _before(row, as_of):
            continue
        _matching_identity(identities, row, product_id)
        results = row.get("results") if isinstance(row.get("results"), list) else []
        immature = next((item for item in results if isinstance(item, dict) and item.get("status") in {"not_mature", "insufficient_data"}), None)
        if immature:
            status, reason = "not_mature", immature.get("null_reason") or "not_mature"
        else:
            status, reason = "observed", None
        facts = {
            "experiment_id": row.get("experiment_id"),
            "attribution_id": row.get("attribution_id"),
            "status": status,
            "reason": reason,
            "native_url": row.get("native_post_url"),
            "metrics": [
                {"name": item.get("metric_name"), "status": item.get("status"), "value": item.get("value"), "null_reason": item.get("null_reason")}
                for item in results if isinstance(item, dict)
            ],
        }
        evidence_ref = _ref("experiment-attribution.jsonl", index)
        existing = _existing_owner_report_for_evidence(
            root,
            kind="experiment",
            product_id=product_id,
            evidence_ref=evidence_ref,
        )
        message_key = (
            existing["message_key"]
            if existing is not None
            else f"experiment:{product_id}:{row.get('attribution_id') or row.get('experiment_id')}"
        )
        events.append(_event(
            kind="experiment",
            product_id=product_id,
            as_of=as_of,
            message_key=message_key,
            facts=facts,
            evidence_refs=[evidence_ref],
        ))
    if events:
        return events
    for index, row in hook_rows:
        if not _before(row, as_of):
            continue
        status = row.get("status") or "unknown"
        immature = status in {"insufficient_data", "not_mature"} or row.get("reason") in {"checkpoint_not_mature", "social_checkpoint_not_mature"}
        facts = {
            "experiment_id": None,
            "renderer_id": row.get("renderer_id"),
            "status": "not_mature" if immature else status,
            "reason": row.get("reason"),
        }
        # Winner/loser names are intentionally omitted for not-mature data.
        if not immature:
            facts["winner_count"] = len(row.get("winners") or [])
            facts["loser_count"] = len(row.get("losers") or [])
        events.append(_event(
            kind="experiment",
            product_id=product_id,
            as_of=as_of,
            message_key=f"experiment:{product_id}:hook:{row.get('renderer_id') or index}",
            facts=facts,
            evidence_refs=[_ref("hook-perf.jsonl", index)],
        ))
    return events


def _portfolio_event(root: pathlib.Path, as_of: dt.datetime) -> list[dict]:
    products = []
    refs = []
    for product_id in PRODUCTS:
        rows = _scoped(_indexed_rows(root, "business-outcomes.jsonl"), product_id)
        latest = _latest(rows, as_of, "business_date", "observed_at")
        if latest is None:
            products.append({
                "product_id": product_id,
                "mrr": None,
                "mrr_reason": "no_business_snapshot",
                "money_value": None,
                "money_currency": None,
                "money_minor": None,
                "money_metric": None,
                "paid_orders": None,
                "money_reason": "no_business_snapshot",
            })
            continue
        index, row = latest
        facts = _business_facts(row)
        product = {
            "product_id": product_id,
            "mrr": facts.get("mrr"),
            "mrr_reason": facts.get("mrr_reason"),
            "money_value": facts.get("money_value"),
            "money_currency": facts.get("money_currency"),
            "money_minor": facts.get("money_minor"),
            "money_metric": facts.get("money_metric"),
            "paid_orders": facts.get("paid_orders"),
            "money_source": facts.get("money_source"),
            "money_reason": facts.get("money_reason"),
        }
        money_buckets = facts.get("money_buckets")
        if isinstance(money_buckets, list) and len(money_buckets) > 1:
            product["money_buckets"] = copy.deepcopy(money_buckets)
        products.append(product)
        refs.append(_ref("business-outcomes.jsonl", index))
    return [_event(
        kind="portfolio_weekly",
        product_id=None,
        as_of=as_of,
        message_key=f"portfolio_weekly:{as_of.date().isoformat()}",
        facts={"products": products},
        evidence_refs=refs or ["state/business-outcomes.jsonl"],
    )]


def build_events(
    state_root: pathlib.Path,
    kind: str,
    *,
    product_id: str | None,
    as_of: dt.datetime,
) -> list[dict]:
    """Build canonical report events for one sweep.

    ``state_root`` is the only path consulted.  Product-scoped reports require
    an exact product ID on their source row; legacy product-null rows are not
    assigned by account or URL.
    """

    root = pathlib.Path(state_root)
    _validate_kind(kind)
    as_of = _as_of(as_of)
    if kind == "portfolio_weekly":
        _validate_product(product_id, portfolio=True)
        return _portfolio_event(root, as_of)
    _validate_product(product_id)
    selected = PRODUCTS if product_id is None else (product_id,)
    events: list[dict] = []
    for selected_product in selected:
        if kind == "action":
            events.extend(_action_events(root, selected_product, as_of))
        elif kind == "checkpoint":
            events.extend(_checkpoint_events(root, selected_product, as_of))
        elif kind == "product_daily":
            events.extend(_daily_events(root, selected_product, as_of))
        elif kind == "incident":
            events.extend(_incident_events(root, selected_product, as_of))
        elif kind == "experiment":
            events.extend(_experiment_events(root, selected_product, as_of))
    return events


def _number(value: object) -> str:
    if value is None:
        return "取得できませんでした"
    return str(value)


def _money_bucket_text(bucket: dict) -> str:
    """Render one money bucket without aggregating currencies."""

    currency = bucket.get("currency") or "通貨不明"
    value = bucket.get("value")
    if value is None:
        return f"最小単位 {bucket.get('minor')} {currency}（通貨指数不明）"
    return f"{_number(value)} {currency}"


def _reason_text(reason: object, *, not_mature: bool = False) -> str:
    raw = str(reason or "unknown")
    if not_mature or "not_mature" in raw or raw in {"insufficient_data", "checkpoint_not_mature"}:
        return "まだ判断できる時間ではありません"
    if any(token in raw for token in ("unavailable", "not_authenticated", "not_configured", "no_business_snapshot", "missing", "error", "failed", "missed")):
        return "取得できませんでした"
    if raw == "unknown":
        return "現在の証拠では分かりません"
    return "現在の証拠では分かりません"


def render_japanese(event: dict) -> str:
    """Render an already validated event without adding facts or numbers."""

    event = _validate_event(event)
    kind = event["kind"]
    product_id = event["product_id"]
    facts = event["facts"]
    lines: list[str] = []
    if kind == "action":
        lines.append(f"📣 {product_id}の公開を確認しました。")
        lines.append(f"次に確認するリンク: {facts.get('native_url')}")
    elif kind == "checkpoint":
        platform = facts.get("platform") or "social"
        status = facts.get("checkpoint_status")
        age = facts.get("target_age_hours")
        if status == "measured" and facts.get("views") is not None:
            lines.append(f"📊 {product_id}の{platform}チェックポイント（{age}時間）。")
            lines.append(f"閲覧数 {facts.get('views')}、表示回数 {facts.get('impressions')}。")
        else:
            reason = facts.get("reason") or "checkpoint_missed"
            natural = _reason_text(reason)
            if natural == "取得できませんでした":
                natural = "まだ取得できませんでした"
            lines.append(f"📊 {product_id}の{platform}チェックポイントは{natural}。")
    elif kind == "product_daily":
        lines.append(f"📦 {product_id}の今日の結果です。")
        mrr = facts.get("mrr")
        money_buckets = facts.get("money_buckets")
        if mrr is not None:
            lines.append(f"MRRは{_number(mrr)} USD（RevenueCat）。")
        elif isinstance(money_buckets, list) and len(money_buckets) > 1:
            amounts = "、".join(
                _money_bucket_text(bucket)
                for bucket in money_buckets
                if isinstance(bucket, dict)
            )
            lines.append(f"売上の確認値は{amounts}（{facts.get('money_source')}）。")
            if facts.get("paid_orders") is not None:
                lines.append(f"注文数 {facts['paid_orders']}件。")
        elif facts.get("money_value") is not None:
            currency = facts.get("money_currency") or "通貨不明"
            lines.append(f"売上の確認値は{_number(facts.get('money_value'))} {currency}（{facts.get('money_source')}）。")
            if facts.get("paid_orders") is not None:
                lines.append(f"注文数 {facts['paid_orders']}件。")
        elif facts.get("paid_orders") is not None:
            if facts.get("money_reason") == "unknown_currency_exponent":
                lines.append(
                    f"売上額は通貨指数が不明のため、最小単位 {facts.get('money_minor')} {facts.get('money_currency')} のままです。"
                )
            else:
                lines.append("売上額は取得できませんでした。")
            lines.append(f"注文数 {facts['paid_orders']}件。")
        else:
            reason = facts.get('money_reason') or facts.get('mrr_reason') or 'unknown'
            if reason == "unknown_currency_exponent":
                lines.append(
                    f"売上額は通貨指数が不明のため、最小単位 {facts.get('money_minor')} {facts.get('money_currency')} のままです。"
                )
            else:
                lines.append(f"売上は{_reason_text(reason)}。")
    elif kind == "incident":
        source_gaps = facts.get("source_gaps")
        if isinstance(source_gaps, list) and source_gaps:
            business_date = facts.get("business_date") or "最新"
            lines.append(
                f"⚠️ {product_id}の{business_date}時点で、確認できないソースがあります。"
            )
            for gap in source_gaps:
                if not isinstance(gap, dict):
                    continue
                source = gap.get("source") or "unknown"
                reason = gap.get("reason") or "unknown"
                lines.append(f"{source}は{_reason_text(reason)}。")
                lines.append(
                    f"次の修復: {gap.get('next_repair') or _repair_for(source)}。"
                )
        else:
            source = facts.get("source") or "unknown"
            reason = facts.get("reason") or "unknown"
            lines.append(f"⚠️ {product_id}で{source}の確認が必要です。{_reason_text(reason)}。")
            lines.append(f"次の修復: {facts.get('next_repair') or _repair_for(source)}。")
    elif kind == "experiment":
        status = facts.get("status")
        if status == "not_mature":
            lines.append(f"🧪 {product_id}の実験はまだ判断できる時間ではありません。")
            if facts.get("reason"):
                lines.append(f"補足: {_reason_text(facts['reason'])}。")
        elif status == "observed":
            lines.append(f"🧪 {product_id}の実験結果を観測しました。")
        else:
            lines.append(f"🧪 {product_id}の実験状態は現在の証拠では分かりません。")
    else:  # portfolio_weekly
        lines.append("🗓️ 今週のポートフォリオ状況です。")
        for item in facts.get("products", []):
            item_product = item.get("product_id")
            mrr = item.get("mrr")
            money_buckets = item.get("money_buckets")
            if mrr is not None:
                detail = f"MRR {mrr} USD"
            elif isinstance(money_buckets, list) and len(money_buckets) > 1:
                amounts = "、".join(
                    _money_bucket_text(bucket)
                    for bucket in money_buckets
                    if isinstance(bucket, dict)
                )
                detail = f"売上 {amounts}"
                if item.get("paid_orders") is not None:
                    detail += f"・注文数 {item['paid_orders']}件"
            elif item.get("money_reason") == "unknown_currency_exponent":
                detail = (
                    f"売上 {item.get('money_minor')} {item.get('money_currency')}"
                    "（最小単位・通貨指数不明）"
                )
                if item.get("paid_orders") is not None:
                    detail += f"・注文数 {item['paid_orders']}件"
            elif item.get("money_value") is not None:
                detail = f"売上 {item['money_value']} {item.get('money_currency') or '通貨不明'}"
                if item.get("paid_orders") is not None:
                    detail += f"・注文数 {item['paid_orders']}件"
            elif item.get("paid_orders") is not None:
                detail = f"注文数 {item['paid_orders']}件・売上額は取得できませんでした"
            else:
                detail = _reason_text(item.get("money_reason") or item.get("mrr_reason"))
            lines.append(f"{item_product}: {detail}")

    lines.extend(["", "確認情報"])
    lines.append(f"message_key={event['message_key']}")
    lines.append(f"product={product_id if product_id is not None else 'portfolio'}")
    if kind == "action" and facts.get("native_url"):
        lines.append(f"native_url={facts['native_url']}")
    if kind == "incident":
        source_gaps = facts.get("source_gaps")
        if isinstance(source_gaps, list):
            for gap in source_gaps:
                if not isinstance(gap, dict):
                    continue
                lines.append(
                    "source="
                    + str(gap.get("source") or "unknown")
                    + ";reason="
                    + str(gap.get("reason") or "unknown")
                    + ";next_repair="
                    + str(gap.get("next_repair") or "")
                )
    raw_reason = facts.get("reason") or facts.get("money_reason") or facts.get("mrr_reason")
    if raw_reason:
        lines.append(f"reason={raw_reason}")
    lines.append("evidence_refs=" + ",".join(event["evidence_refs"]))
    return "\n".join(lines)


def _read_rows(path: pathlib.Path) -> list[dict]:
    return load_jsonl(path)


class OwnerReportStore:
    """Append-only owner report and Telegram delivery ledgers."""

    def __init__(self, report_path: pathlib.Path, delivery_path: pathlib.Path):
        self.report_path = pathlib.Path(report_path)
        self.delivery_path = pathlib.Path(delivery_path)
        self.lock_path = self.report_path.with_name(self.report_path.name + ".lock")

    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _reports(self) -> list[dict]:
        return _read_rows(self.report_path)

    def _deliveries(self) -> list[dict]:
        return _read_rows(self.delivery_path)

    def record(self, event: dict) -> dict:
        checked = _validate_event(event)
        with self._locked() as lock:
            for existing in self._reports():
                if existing.get("message_key") != checked["message_key"]:
                    continue
                normalized = copy.deepcopy(existing)
                normalized["product_id"] = canonical_product_id(existing.get("product_id"))
                try:
                    normalized = _validate_event(normalized)
                except OwnerReportError:
                    raise ConflictError(f"conflicting replay for {checked['message_key']}")
                if _canonical(_semantic_event(normalized)) != _canonical(_semantic_event(checked)):
                    raise ConflictError(f"conflicting replay for {checked['message_key']}")
                return normalized
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            with self.report_path.open("a", encoding="utf-8") as handle:
                handle.write(_canonical(checked) + "\n")
                handle.flush()
            return copy.deepcopy(checked)

    def _latest_delivery_unlocked(self, message_key: str) -> dict | None:
        latest = None
        for row in self._deliveries():
            if row.get("message_key") == message_key:
                latest = row
        return copy.deepcopy(latest) if latest is not None else None

    def delivery_for(self, message_key: str) -> dict | None:
        """Return the latest append-only state for a message key."""

        return self._latest_delivery_unlocked(message_key)

    def _append_delivery_row(self, row: dict) -> dict:
        self.delivery_path.parent.mkdir(parents=True, exist_ok=True)
        with self.delivery_path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical(row) + "\n")
            handle.flush()
        return copy.deepcopy(row)

    def claim_delivery(self, message_key: str) -> tuple[bool, dict]:
        """Atomically claim a message key before an external send.

        The claim is itself an append-only row.  A second process observing the
        ``sending`` state never calls Telegram; the first process either
        appends a terminal receipt or leaves the durable claim for inspection.
        """

        if not isinstance(message_key, str) or not message_key:
            raise DeliveryError("message_key must be non-empty")
        with self._locked() as lock:
            existing = self._latest_delivery_unlocked(message_key)
            if existing is not None:
                return False, existing
            claim = {
                "schema_version": DELIVERY_SCHEMA_VERSION,
                "message_key": message_key,
                "status": "sending",
                "message_ids": [],
                "claim_id": "claim:" + hashlib.sha256(message_key.encode("utf-8")).hexdigest()[:16],
                "claimed_at": _timestamp(dt.datetime.now(dt.timezone.utc)),
                "receipt": {"status": "sending", "message_ids": []},
            }
            return True, self._append_delivery_row(claim)

    @staticmethod
    def _normalize_receipt(receipt: dict) -> dict:
        if not isinstance(receipt, dict):
            raise DeliveryError("Telegram receipt must be an object")
        status = receipt.get("status")
        message_ids = receipt.get("message_ids") or []
        if not isinstance(message_ids, list) or not all(
            item is None or isinstance(item, (int, str)) for item in message_ids
        ):
            raise DeliveryError("message_ids must be an array")
        message_ids = [item for item in message_ids if item is not None]
        if status == "delivered" and not message_ids:
            raise DeliveryError("delivered receipt has no non-null message_id")
        if status not in {"delivered", "delivery_unknown", "failed"}:
            if message_ids:
                status = "delivered"
            else:
                raise DeliveryError("receipt status must prove delivery or explicit unknown")
        normalized = copy.deepcopy(receipt)
        normalized["status"] = status
        normalized["message_ids"] = message_ids
        return normalized

    def record_delivery(self, message_key: str, receipt: dict) -> dict:
        """Append a terminal receipt, allowing only a prior ``sending`` claim."""

        if not isinstance(message_key, str) or not message_key:
            raise DeliveryError("message_key must be non-empty")
        normalized_receipt = self._normalize_receipt(receipt)
        row = {
            "schema_version": DELIVERY_SCHEMA_VERSION,
            "message_key": message_key,
            "status": normalized_receipt["status"],
            "message_ids": normalized_receipt["message_ids"],
            "receipt": normalized_receipt,
        }
        with self._locked() as lock:
            existing = self._latest_delivery_unlocked(message_key)
            if existing is not None and existing.get("status") != "sending":
                if _canonical(existing) != _canonical(row):
                    raise ConflictError(f"conflicting delivery for {message_key}")
                return existing
            return self._append_delivery_row(row)


def _receipt_from_row(row: dict) -> dict:
    receipt = row.get("receipt")
    if isinstance(receipt, dict):
        return copy.deepcopy(receipt)
    return {"status": row.get("status"), "message_ids": row.get("message_ids") or []}


def deliver(event: dict, store: OwnerReportStore, send_text: Callable[[str], dict]) -> dict:
    """Record before sending, then deliver exactly once per message key."""

    recorded = store.record(event)
    message_key = recorded["message_key"]
    claimed, state = store.claim_delivery(message_key)
    if not claimed:
        if state.get("status") == "sending":
            return {
                "status": "delivery_unknown",
                "message_ids": [],
                "error": "delivery_in_progress_claimed_by_another_process",
            }
        return _receipt_from_row(state)
    try:
        receipt = send_text(render_japanese(recorded))
    except Exception as exc:  # transport timeout is explicitly not retried
        unknown = {
            "status": "delivery_unknown" if isinstance(exc, (TimeoutError, OSError)) or exc.__class__.__name__ == "TelegramDeliveryUnknown" else "failed",
            "message_ids": [],
            "error": str(exc),
        }
        store.record_delivery(message_key, unknown)
        return unknown
    try:
        row = store.record_delivery(message_key, receipt)
    except DeliveryError as exc:
        # A response claiming delivery without a real message ID is
        # ambiguous.  Persist the non-retryable state before returning so a
        # replay cannot issue a second Bot API call.
        unknown = {
            "status": "delivery_unknown",
            "message_ids": [],
            "error": f"invalid Telegram receipt: {exc}",
        }
        row = store.record_delivery(message_key, unknown)
    return _receipt_from_row(row)
