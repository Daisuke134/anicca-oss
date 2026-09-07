#!/usr/bin/env python3
"""Product-scoped business outcome collector.

Provider failures are represented as unavailable/null. A numeric zero is emitted
only after a successful, product-scoped provider query.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import time
from decimal import Decimal, InvalidOperation
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = Path.home() / ".local" / "state" / "life-manager" / ".env"


def default_storage_paths(environment: dict[str, str] | None = None) -> tuple[Path, Path]:
    env = os.environ if environment is None else environment
    runtime_root = Path(env.get("LIFE_MANAGER_STATE_ROOT", str(ROOT)))
    return (
        runtime_root / "state" / "business-outcomes.jsonl",
        runtime_root / "evidence" / "business",
    )


DEFAULT_STATE, DEFAULT_EVIDENCE = default_storage_paths()

PRODUCTS = {
    "aniccaios": {
        "asc_app_id": "6755129214",
        "revenuecat_app_id": "app511ef26659",
        "analytics": "mixpanel",
    },
    "honne": {
        "asc_app_id": "6759667221",
        "revenuecat_app_id": "app3bbd298d22",
        "analytics": None,
    },
    "ebook-en": {"stripe_product_ids": ["prod_UQ2LTH66Rwict4"]},
    "ebook-ja": {"stripe_product_ids": ["prod_UQ2LrpVy4b1bAY"]},
}

RC_CHARTS = (
    "mrr",
    "actives",
    "revenue",
    "trials_new",
    "churn",
    "subscription_retention",
)

ASC_REPORTS = {
    "downloads": "App Downloads Standard",
    "discovery": "App Store Discovery and Engagement Standard",
    "purchases": "App Store Purchases Standard",
    "subscription_events": "App Store Subscription Event Report Standard",
    "subscription_state": "App Store Subscription State Report Standard",
}


def unavailable_source(reason: str, *, error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"status": "unavailable", "data": None, "reason": reason}
    if error:
        out["error"] = error[:240]
    return out


def available_source(data: Any, *, evidence_sha256: str | None = None) -> dict[str, Any]:
    out = {"status": "available", "data": data, "reason": None}
    if evidence_sha256:
        out["evidence_sha256"] = evidence_sha256
    return out


def revenuecat_app_filter(options: dict[str, Any], app_id: str) -> list[dict[str, Any]]:
    by_id = {entry.get("id"): entry for entry in options.get("filters", [])}
    dimension = "app_id" if "app_id" in by_id else (
        "app_config_id" if "app_config_id" in by_id else None
    )
    if dimension is None:
        raise ValueError("RevenueCat app filter is not offered by this chart")
    allowed = {item.get("id") for item in by_id[dimension].get("options", [])}
    if app_id not in allowed:
        raise ValueError(f"RevenueCat app {app_id} is not listed by chart options")
    return [{"name": dimension, "values": [app_id]}]


def _measure_name(measures: list[Any], index: int) -> str:
    if 0 <= index < len(measures):
        value = measures[index]
        if isinstance(value, dict):
            return str(value.get("id") or value.get("display_name") or index)
        return str(value)
    return str(index)


def _period_value(periods: list[Any], index: int) -> Any:
    if not 0 <= index < len(periods):
        return index
    value = periods[index]
    if isinstance(value, dict):
        return (
            value.get("date")
            or value.get("start_date")
            or value.get("display_name")
            or value.get("timestamp")
            or index
        )
    return value


def latest_complete_chart_points(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the latest complete value for every measure in a v3 chart.

    RevenueCat's current chart values are objects whose ``cohort`` indexes the
    period and whose ``measure`` indexes ``measures``.
    """
    measures = body.get("measures") or [{"id": "value"}]
    periods = body.get("periods") or []
    candidates: dict[str, list[dict[str, Any]]] = {}
    for value in body.get("values", []):
        if not isinstance(value, dict) or "value" not in value:
            continue
        if value.get("incomplete") is True:
            continue
        measure_index = int(value.get("measure", 0))
        cohort = value.get("cohort", value.get("period", 0))
        if not isinstance(cohort, int):
            continue
        name = _measure_name(measures, measure_index)
        candidates.setdefault(name, []).append({
            "value": value.get("value"),
            "period": _period_value(periods, cohort),
            "period_index": cohort,
            "incomplete": False,
        })
    return {
        name: max(points, key=lambda point: point["period_index"])
        for name, points in candidates.items()
    }


def parse_asc_tsv_gz(payload: bytes) -> list[dict[str, str]]:
    text = gzip.decompress(payload).decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def _required_columns(rows: list[dict[str, str]], required: set[str]) -> None:
    columns = set(rows[0]) if rows else set()
    missing = sorted(required - columns)
    if missing:
        raise ValueError("missing ASC columns: " + ", ".join(missing))


def _integer(value: str | int | None) -> int:
    if value in (None, ""):
        return 0
    return int(str(value).replace(",", ""))


def summarize_asc_downloads(rows: list[dict[str, str]]) -> dict[str, Any]:
    _required_columns(rows, {"Date", "Download Type", "Source Type", "Counts"})
    mapping = {
        "First-time download": "first_time_downloads",
        "Redownload": "redownloads",
        "Auto-update": "auto_updates",
        "Manual update": "manual_updates",
        "Restore": "restores",
    }
    totals = {field: 0 for field in mapping.values()}
    by_source: dict[str, dict[str, int]] = {}
    for row in rows:
        kind = row["Download Type"]
        if kind not in mapping:
            continue
        field = mapping[kind]
        count = _integer(row["Counts"])
        totals[field] += count
        source = row["Source Type"] or "Unknown"
        by_source.setdefault(source, {name: 0 for name in mapping.values()})
        by_source[source][field] += count
    return {**totals, "by_source": by_source, "row_count": len(rows)}


def summarize_asc_table(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Summarize an aggregate ASC table without discarding its schema.

    ASC analytics rows contain no person-level records. We retain the exact
    headers, row count, date bounds, and totals for numeric measure columns.
    Dimension combinations remain in the evidence file, not the compact state.
    """
    if not rows:
        return {"row_count": 0, "columns": [], "date_min": None, "date_max": None,
                "numeric_totals": {}}
    columns = list(rows[0])
    date_column = next((name for name in columns if name.lower() in {
        "date", "download date", "event date"
    }), None)
    dates = sorted(row.get(date_column, "") for row in rows) if date_column else []
    totals: dict[str, Decimal] = {}
    for column in columns:
        values: list[Decimal] = []
        for row in rows:
            raw = (row.get(column) or "").replace(",", "").strip()
            if not raw or raw.endswith("%"):
                continue
            try:
                values.append(Decimal(raw))
            except InvalidOperation:
                values = []
                break
        if values:
            totals[column] = sum(values, Decimal(0))
    return {
        "row_count": len(rows),
        "columns": columns,
        "date_min": dates[0] if dates else None,
        "date_max": dates[-1] if dates else None,
        "numeric_totals": {
            key: int(value) if value == value.to_integral() else float(value)
            for key, value in totals.items()
        },
    }


def summarize_stripe_sessions(
    sessions: Iterable[dict[str, Any]], product_ids: set[str]
) -> dict[str, Any]:
    sessions = list(sessions)
    seen: set[str] = set()
    for session in sessions:
        session_id = session.get("id")
        if session_id in seen:
            raise ValueError(f"duplicate Stripe session: {session_id}")
        seen.add(session_id)
    matched: list[str] = []
    gross: dict[str, int] = {}
    refunded: dict[str, int] = {}
    for session in sessions:
        session_id = session.get("id")
        products = {
            (item.get("price") or {}).get("product")
            for item in (session.get("line_items") or {}).get("data", [])
        }
        if not products.intersection(product_ids) or session.get("payment_status") != "paid":
            continue
        matched.append(str(session_id))
        currency = str(session.get("currency") or "unknown")
        gross[currency] = gross.get(currency, 0) + int(session.get("amount_total") or 0)
        payment_intent = session.get("payment_intent")
        charge = payment_intent.get("latest_charge") if isinstance(payment_intent, dict) else None
        if not isinstance(charge, dict) or "amount_refunded" not in charge:
            raise ValueError(f"Stripe refund expansion missing: {session_id}")
        refunded[currency] = refunded.get(currency, 0) + int(charge["amount_refunded"] or 0)
    currencies = set(gross) | set(refunded)
    return {
        "paid_orders": len(matched),
        "gross_minor": gross,
        "refunded_minor": refunded,
        "net_minor": {
            currency: gross.get(currency, 0) - refunded.get(currency, 0)
            for currency in sorted(currencies)
        },
        "queried_product_ids": sorted(product_ids),
        "matched_session_ids": sorted(matched),
    }


def summarize_mixpanel_export(lines: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line).get("event")
        if event:
            counts[str(event)] = counts.get(str(event), 0) + 1
    return dict(sorted(counts.items()))


def validate_snapshots(rows: list[dict[str, Any]], products: set[str]) -> None:
    seen: set[str] = set()
    for row in rows:
        if row.get("product_id") not in products:
            raise ValueError(f"unknown product: {row.get('product_id')}")
        snapshot_id = row.get("snapshot_id")
        if snapshot_id in seen:
            raise ValueError(f"duplicate snapshot: {snapshot_id}")
        seen.add(snapshot_id)
        for name, source in (row.get("sources") or {}).items():
            if source.get("status") not in {"available", "unavailable"}:
                raise ValueError(f"invalid source status: {name}")
            if source.get("status") == "unavailable" and source.get("data") is not None:
                raise ValueError(f"unavailable source has data: {name}")


def verify_gate5_snapshots(rows: list[dict[str, Any]], business_date: str) -> dict[str, Any]:
    selected = [row for row in rows if row.get("business_date") == business_date]
    validate_snapshots(selected, set(PRODUCTS))
    by_product = {row["product_id"]: row for row in selected}
    if set(by_product) != set(PRODUCTS) or len(selected) != len(PRODUCTS):
        raise ValueError("Gate 5 requires exactly four product snapshots")
    unavailable: list[dict[str, str]] = []
    for product_id, config in PRODUCTS.items():
        sources = by_product[product_id]["sources"]
        for source_name, source in sources.items():
            if source["status"] == "unavailable":
                unavailable.append({
                    "product_id": product_id,
                    "source": source_name,
                    "reason": source.get("reason") or "unspecified",
                })
        if "revenuecat_app_id" in config:
            revenuecat = sources.get("revenuecat")
            asc = sources.get("app_store_connect")
            if not revenuecat or revenuecat["status"] != "available":
                raise ValueError(f"{product_id} RevenueCat unavailable")
            if revenuecat["data"].get("app_id") != config["revenuecat_app_id"]:
                raise ValueError(f"{product_id} RevenueCat app mismatch")
            if not asc or asc["status"] != "available":
                raise ValueError(f"{product_id} ASC unavailable")
            if asc["data"].get("app_id") != config["asc_app_id"]:
                raise ValueError(f"{product_id} ASC app mismatch")
            downloads = asc["data"].get("reports", {}).get("downloads", {})
            if downloads.get("status") != "available":
                raise ValueError(f"{product_id} ASC downloads unavailable")
            download_data = downloads["data"]
            if "installs" in download_data:
                raise ValueError(f"{product_id} contains ambiguous installs")
            for field in (
                "first_time_downloads", "redownloads", "auto_updates",
                "manual_updates", "restores",
            ):
                if not isinstance(download_data.get(field), int):
                    raise ValueError(f"{product_id} missing separated download field: {field}")
        else:
            stripe = sources.get("stripe")
            if not stripe or stripe["status"] != "available":
                raise ValueError(f"{product_id} Stripe unavailable")
            data = stripe["data"]
            if sorted(data.get("queried_product_ids", [])) != sorted(config["stripe_product_ids"]):
                raise ValueError(f"{product_id} Stripe product mismatch")
            for field in ("paid_orders", "gross_minor", "refunded_minor", "net_minor"):
                if field not in data:
                    raise ValueError(f"{product_id} missing Stripe field: {field}")
    return {
        "gate_pass": True,
        "business_date": business_date,
        "products_verified": len(by_product),
        "unavailable_sources": sorted(
            unavailable, key=lambda item: (item["product_id"], item["source"])
        ),
    }


def load_env(path: Path = DEFAULT_ENV) -> dict[str, str]:
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip())
    return env


def http_json(
    url: str,
    headers: dict[str, str],
    *,
    timeout: int = 20,
) -> dict[str, Any]:
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=timeout
    ) as response:
        return json.load(response)


def _http_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers or {}), timeout=20
    ) as response:
        return response.read()


def _json_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def revenuecat_products(env: dict[str, str]) -> dict[str, list[str]]:
    project = env["REVENUECAT_PROJECT_ID"]
    headers = {"Authorization": "Bearer " + env["REVENUECAT_V2_SECRET_KEY"]}
    body = http_json(
        f"https://api.revenuecat.com/v2/projects/{project}/products?limit=100",
        headers,
    )
    out: dict[str, list[str]] = {}
    for product in body.get("items", []):
        app_id = product.get("app_id") or (product.get("app") or {}).get("id")
        if app_id and product.get("id"):
            out.setdefault(app_id, []).append(product["id"])
    return {key: sorted(value) for key, value in out.items()}


def _asc_headers(env: dict[str, str]) -> dict[str, str]:
    import jwt

    now = int(time.time())
    token = jwt.encode(
        {
            "iss": env["ASC_ISSUER_ID"],
            "iat": now,
            "exp": now + 600,
            "aud": "appstoreconnect-v1",
        },
        Path(env["ASC_KEY_PATH"]).read_text(),
        algorithm="ES256",
        headers={"kid": env["ASC_KEY_ID"], "typ": "JWT"},
    )
    return {"Authorization": "Bearer " + token}


def _asc_get(path_or_url: str, headers: dict[str, str]) -> dict[str, Any]:
    if path_or_url.startswith("http"):
        url = path_or_url
    else:
        url = "https://api.appstoreconnect.apple.com/v1" + path_or_url
    return http_json(url, headers)


def _latest_asc_instance(instances: list[dict[str, Any]]) -> dict[str, Any]:
    if not instances:
        raise ValueError("ASC report has no instances")
    granularity_rank = {"DAILY": 3, "WEEKLY": 2, "MONTHLY": 1}
    return max(
        instances,
        key=lambda item: (
            item.get("attributes", {}).get("processingDate", ""),
            granularity_rank.get(
                item.get("attributes", {}).get("granularity", ""), 0
            ),
        ),
    )


def collect_asc(
    env: dict[str, str],
    app_id: str,
    evidence_dir: Path,
) -> dict[str, Any]:
    headers = _asc_headers(env)
    requests = _asc_get(
        f"/apps/{app_id}/analyticsReportRequests?limit=200", headers
    ).get("data", [])
    active = [
        item for item in requests
        if not item.get("attributes", {}).get("stoppedDueToInactivity")
    ]
    ongoing = [
        item for item in active
        if item.get("attributes", {}).get("accessType") == "ONGOING"
    ]
    candidates = ongoing or active
    if not candidates:
        raise ValueError(f"ASC app {app_id} has no active analytics request")
    request = candidates[-1]
    reports = _asc_get(
        f"/analyticsReportRequests/{request['id']}/reports?limit=200", headers
    ).get("data", [])
    by_name = {item.get("attributes", {}).get("name"): item for item in reports}
    result: dict[str, Any] = {
        "app_id": app_id,
        "request_id": request["id"],
        "access_type": request.get("attributes", {}).get("accessType"),
        "reports": {},
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for key, name in ASC_REPORTS.items():
        report = by_name.get(name)
        if not report:
            result["reports"][key] = unavailable_source("report_not_offered")
            continue
        instances = _asc_get(
            f"/analyticsReports/{report['id']}/instances?limit=200", headers
        ).get("data", [])
        instance = _latest_asc_instance(instances)
        segments = _asc_get(
            f"/analyticsReportInstances/{instance['id']}/segments?limit=200",
            headers,
        ).get("data", [])
        rows: list[dict[str, str]] = []
        segment_hashes: list[str] = []
        for segment in segments:
            attributes = segment.get("attributes", {})
            payload = _http_bytes(attributes["url"])
            expected_size = attributes.get("sizeInBytes")
            if expected_size is not None and len(payload) != int(expected_size):
                raise ValueError(f"ASC segment size mismatch: {segment['id']}")
            expected_md5 = attributes.get("checksum")
            actual_md5 = hashlib.md5(payload).hexdigest()
            if expected_md5 and actual_md5 != expected_md5:
                raise ValueError(f"ASC segment checksum mismatch: {segment['id']}")
            rows.extend(parse_asc_tsv_gz(payload))
            segment_hashes.append(hashlib.sha256(payload).hexdigest())
        compact = (
            summarize_asc_downloads(rows)
            if key == "downloads" else summarize_asc_table(rows)
        )
        evidence = {
            "app_id": app_id,
            "report_name": name,
            "processing_date": instance.get("attributes", {}).get("processingDate"),
            "granularity": instance.get("attributes", {}).get("granularity"),
            "segment_sha256": segment_hashes,
            "rows": rows,
        }
        evidence_path = evidence_dir / f"{app_id}-{key}.json"
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        )
        result["reports"][key] = available_source(
            {
                **compact,
                "processing_date": evidence["processing_date"],
                "granularity": evidence["granularity"],
                "evidence_path": str(evidence_path),
            },
            evidence_sha256=_json_hash(evidence),
        )
    return result


def collect_revenuecat(
    env: dict[str, str],
    app_id: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    project = env["REVENUECAT_PROJECT_ID"]
    headers = {"Authorization": "Bearer " + env["REVENUECAT_V2_SECRET_KEY"]}
    base = f"https://api.revenuecat.com/v2/projects/{project}/charts"
    result: dict[str, Any] = {"app_id": app_id, "charts": {}}
    for chart in RC_CHARTS:
        options = http_json(f"{base}/{chart}/options", headers)
        filters = revenuecat_app_filter(options, app_id)
        query = urllib.parse.urlencode({
            "start_date": start_date,
            "end_date": end_date,
            "resolution": "0",
            "filters": json.dumps(filters, separators=(",", ":")),
        })
        body = http_json(f"{base}/{chart}?{query}", headers)
        result["charts"][chart] = {
            "latest_complete": latest_complete_chart_points(body),
            "resolution": body.get("resolution"),
            "start_date": body.get("start_date"),
            "end_date": body.get("end_date"),
            "evidence_sha256": _json_hash(body),
        }
    products = revenuecat_products(env).get(app_id, [])
    if products:
        chart = "conversion_to_paying"
        options = http_json(f"{base}/{chart}/options", headers)
        product_option = next(
            (item for item in options.get("filters", []) if item.get("id") == "product_id"),
            None,
        )
        allowed = {item.get("id") for item in (product_option or {}).get("options", [])}
        safe_products = [item for item in products if item in allowed]
        if safe_products:
            query = urllib.parse.urlencode({
                "start_date": start_date,
                "end_date": end_date,
                "resolution": "0",
                "filters": json.dumps(
                    [{"name": "product_id", "values": safe_products}],
                    separators=(",", ":"),
                ),
            })
            body = http_json(f"{base}/{chart}?{query}", headers)
            result["charts"][chart] = {
                "latest_complete": latest_complete_chart_points(body),
                "product_ids": safe_products,
                "evidence_sha256": _json_hash(body),
            }
        else:
            result["charts"][chart] = unavailable_source("products_not_offered_by_chart")
    else:
        result["charts"]["conversion_to_paying"] = unavailable_source("no_app_products")
    return result


def _stripe_headers(env: dict[str, str]) -> dict[str, str]:
    token = base64.b64encode((env["STRIPE_SECRET_KEY"] + ":").encode()).decode()
    return {"Authorization": "Basic " + token}


def stripe_session_query(since: int, until: int) -> str:
    if until <= since:
        raise ValueError("Stripe query end must be after start")
    return urllib.parse.urlencode([
        ("limit", "100"),
        ("created[gte]", str(since)),
        ("created[lt]", str(until)),
        ("expand[]", "data.line_items"),
        ("expand[]", "data.payment_intent.latest_charge"),
    ])


def collect_stripe_sessions(
    env: dict[str, str], product_ids: set[str], since: int, until: int
) -> dict[str, Any]:
    query = stripe_session_query(since, until)
    body = http_json(
        "https://api.stripe.com/v1/checkout/sessions?" + query,
        _stripe_headers(env),
    )
    if body.get("has_more"):
        raise ValueError("Stripe result exceeded 100 sessions; pagination required")
    return summarize_stripe_sessions(body.get("data", []), product_ids)


def collect_mixpanel(
    env: dict[str, str], start_date: str, end_date: str
) -> dict[str, Any]:
    token = base64.b64encode((env["MIXPANEL_API_SECRET"] + ":").encode()).decode()
    query = urllib.parse.urlencode({"from_date": start_date, "to_date": end_date})
    request = urllib.request.Request(
        "https://data.mixpanel.com/api/2.0/export/?" + query,
        headers={"Authorization": "Basic " + token},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        lines = response.read().decode("utf-8").splitlines()
    return {"event_counts": summarize_mixpanel_export(lines), "rows": len(lines)}


def collect_snapshot(
    env: dict[str, str],
    product_id: str,
    business_date: str,
) -> dict[str, Any]:
    config = PRODUCTS[product_id]
    sources: dict[str, Any] = {}
    if "revenuecat_app_id" in config:
        start = (dt.date.fromisoformat(business_date) - dt.timedelta(days=34)).isoformat()
        try:
            data = collect_revenuecat(
                env, config["revenuecat_app_id"], start, business_date
            )
            sources["revenuecat"] = available_source(data, evidence_sha256=_json_hash(data))
        except Exception as error:
            sources["revenuecat"] = unavailable_source(
                "provider_query_failed", error=f"{type(error).__name__}: {error}"
            )
        try:
            asc = collect_asc(
                env,
                config["asc_app_id"],
                DEFAULT_EVIDENCE / business_date / product_id,
            )
            sources["app_store_connect"] = available_source(
                asc, evidence_sha256=_json_hash(asc)
            )
        except Exception as error:
            sources["app_store_connect"] = unavailable_source(
                "provider_query_failed", error=f"{type(error).__name__}: {error}"
            )
        if config.get("analytics") == "mixpanel":
            try:
                sources["product_analytics"] = available_source(
                    collect_mixpanel(env, business_date, business_date)
                )
            except Exception as error:
                sources["product_analytics"] = unavailable_source(
                    "provider_query_failed", error=f"{type(error).__name__}: {error}"
                )
        else:
            sources["product_analytics"] = unavailable_source(
                "no_verified_readable_funnel"
            )
        sources["posthog"] = unavailable_source("missing_project_read_credential")
    else:
        since = int(
            dt.datetime.combine(
                dt.date.fromisoformat(business_date),
                dt.time.min,
                tzinfo=dt.timezone.utc,
            ).timestamp()
        )
        until = int(
            dt.datetime.combine(
                dt.date.fromisoformat(business_date) + dt.timedelta(days=1),
                dt.time.min,
                tzinfo=dt.timezone.utc,
            ).timestamp()
        )
        try:
            sources["stripe"] = available_source(
                collect_stripe_sessions(
                    env, set(config["stripe_product_ids"]), since, until
                )
            )
        except Exception as error:
            sources["stripe"] = unavailable_source(
                "provider_query_failed", error=f"{type(error).__name__}: {error}"
            )
        sources["kdp"] = unavailable_source("not_authenticated")
        sources["gumroad"] = unavailable_source("not_configured")
    return {
        "schema_version": 1,
        "snapshot_id": f"{product_id}:{business_date}",
        "product_id": product_id,
        "business_date": business_date,
        "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sources": sources,
    }


def upsert_snapshots(path: Path, new_rows: list[dict[str, Any]]) -> int:
    old: list[dict[str, Any]] = []
    if path.exists():
        old = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    by_id = {row["snapshot_id"]: row for row in old}
    before = len(by_id)
    for row in new_rows:
        by_id[row["snapshot_id"]] = row
    rows = sorted(by_id.values(), key=lambda row: row["snapshot_id"])
    validate_snapshots(rows, set(PRODUCTS))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    )
    os.replace(temp, path)
    return len(by_id) - before


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=(dt.date.today() - dt.timedelta(days=1)).isoformat())
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--products", default=",".join(PRODUCTS))
    args = parser.parse_args()
    selected = [item for item in args.products.split(",") if item]
    unknown = sorted(set(selected) - set(PRODUCTS))
    if unknown:
        parser.error("unknown products: " + ", ".join(unknown))
    env = load_env()
    rows = [collect_snapshot(env, product, args.date) for product in selected]
    added = upsert_snapshots(args.state, rows)
    status = {
        row["product_id"]: {
            source: value["status"] for source, value in row["sources"].items()
        }
        for row in rows
    }
    print(json.dumps({"date": args.date, "added": added, "sources": status}, sort_keys=True))
    required = [
        row for row in rows
        if all(value["status"] == "unavailable" for value in row["sources"].values())
    ]
    return 1 if required else 0


if __name__ == "__main__":
    raise SystemExit(main())
