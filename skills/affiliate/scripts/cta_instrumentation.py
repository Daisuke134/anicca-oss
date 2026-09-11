#!/usr/bin/env python3
"""Install the owned, fail-closed Affiliate CTA redirect through the publication owner."""

import hashlib
import html
import json
import os
import subprocess
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from job_journal import reconcile_effect, start_effect, verify_effect
from provider_cli import atomic_write


FILES = {
    "apps/landing/netlify/functions/_lib/marketing-go.js": "e88450e39aa9e4a98cb5e48926f66011729fb5c5ffe431eb297af38ee7b086cc",
    "apps/landing/netlify/functions/_lib/__tests__/marketing-go.test.js": "5205c356d62217e88299569828236c41746313e4a0f59ff59d8e268db96d3f1e",
    "apps/landing/app/blog/[slug]/page.tsx": "11286f2056a2958c4504809537c23c947f822e2c0c445a0cc5da8708e8b45abe",
}
MARKER = "AFFILIATE_CTA_V1"
V2_FILES = {
    "apps/landing/netlify/functions/marketing-go.js": "c060d60bc0c151403b3d1f81db76280aaf5bf94e16bbc8d62970e9f844386ef3",
    "apps/landing/netlify/functions/_lib/marketing-go.js": "d7a7d5dee2dfb1b12bb210548a7642a7e42133b9a1936d356891535387655178",
    "apps/landing/netlify/functions/_lib/__tests__/marketing-go.test.js": "2e64e76da4636afd33fc42d66565a68599f3c3219c980f175f33ca5081561a84",
}
V2_MARKER = "AFFILIATE_CTA_V2"
V3_MARKER = "AFFILIATE_ENTRY_V1"
V3_BASE_FILES = {
    "apps/landing/app/blog/[slug]/page.tsx": "98a8ca2e105eb579ec18f01b51a158feced956c41d5695ba472ae62b2d7ed243",
    "apps/landing/netlify/functions/marketing-go.js": "eb026683a32547851019423964672ea32c72fc18a145a359c6a0f2520c36f885",
    "apps/landing/netlify/functions/_lib/marketing-go.js": "84c00fe2ab1083987544f228a837876670d6efd97f0f4adff417b94265f4d220",
    "apps/landing/netlify/functions/_lib/__tests__/marketing-go.test.js": "1665c97279172db359fbd685471407ba04bf2767f9728d8331aaecec8f40af27",
}
V3_NEW_FILES = {
    "apps/landing/components/blog/AffiliateEntryReceipt.tsx": '''"use client";

import { useEffect } from "react";

const X_HOSTS = new Set(["x.com", "www.x.com", "twitter.com", "www.twitter.com", "t.co"]);

export default function AffiliateEntryReceipt({ placementId }: { placementId: string }) {
  useEffect(() => {
    let source = "UNKNOWN";
    try { source = X_HOSTS.has(new URL(document.referrer).hostname.toLowerCase()) ? "X" : "UNKNOWN"; } catch {}
    if (source !== "X") return;
    void fetch("/.netlify/functions/marketing-entry", {
      method: "POST", credentials: "omit", cache: "no-store", keepalive: true,
      referrerPolicy: "no-referrer", headers: { "content-type": "application/json" },
      body: JSON.stringify({ placement_id: placementId, source }),
    });
  }, [placementId]);
  return null;
}
// AFFILIATE_ENTRY_V1
''',
    "apps/landing/netlify/functions/marketing-entry.js": '''const { makeEntryHandler, makeSupabasePersist } = require("./_lib/marketing-entry");

exports.handler = async (event) => {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) return { statusCode: 503, body: "Entry receipt unavailable" };
  return makeEntryHandler({ persist: makeSupabasePersist({ url, serviceKey: key }) })(event);
};
// AFFILIATE_ENTRY_V1
''',
    "apps/landing/netlify/functions/_lib/marketing-entry.js": '''const { randomUUID } = require("node:crypto");

const PLACEMENT = /^elevenlabs-discovered-[a-z0-9][a-z0-9-]{2,60}-en-1$/;

function makeSupabasePersist({ url, serviceKey, fetchImpl = fetch }) {
  const endpoint = `${url.replace(/\\/$/, "")}/rest/v1/marketing_click_receipts`;
  return async (receipt) => {
    const response = await fetchImpl(endpoint, {
      method: "POST", headers: { "Content-Type": "application/json", apikey: serviceKey,
        Authorization: `Bearer ${serviceKey}`, Prefer: "return=minimal" },
      body: JSON.stringify(receipt),
    });
    if (!response.ok) throw new Error(`entry receipt storage failed: HTTP ${response.status}`);
  };
}

function makeEntryHandler({ persist, now = () => new Date().toISOString(), receiptId = randomUUID }) {
  return async (event) => {
    if (event.httpMethod !== "POST") return { statusCode: 405, headers: { allow: "POST" }, body: "" };
    let body;
    try { body = JSON.parse(event.body || "{}"); } catch { return { statusCode: 400, body: "" }; }
    if (!PLACEMENT.test(body.placement_id || "") || body.source !== "X")
      return { statusCode: 400, body: "" };
    const receipt = { schema_version: 1, receipt_id: receiptId(), campaign_token: "entry_x",
      product_id: `entry:${body.placement_id}`, clicked_at: now() };
    try { await persist(receipt); } catch { return { statusCode: 503, body: "" }; }
    return { statusCode: 204, headers: { "cache-control": "no-store" }, body: "" };
  };
}

module.exports = { PLACEMENT, makeEntryHandler, makeSupabasePersist };
// AFFILIATE_ENTRY_V1
''',
    "apps/landing/netlify/functions/_lib/__tests__/marketing-entry.test.js": '''const test = require("node:test");
const assert = require("node:assert/strict");
const { makeEntryHandler } = require("../marketing-entry");

test("persists only reduced X source and exact placement", async () => {
  const rows = [];
  const handler = makeEntryHandler({ persist: async (row) => rows.push(row),
    receiptId: () => "entry-1", now: () => "2026-08-22T00:00:00Z" });
  const placement = "elevenlabs-discovered-voice-changer-en-1";
  assert.equal((await handler({ httpMethod: "POST", body: JSON.stringify({ placement_id: placement, source: "X" }) })).statusCode, 204);
  assert.deepEqual(rows[0], { schema_version: 1, receipt_id: "entry-1", campaign_token: "entry_x",
    product_id: `entry:${placement}`, clicked_at: "2026-08-22T00:00:00Z" });
  assert.equal(JSON.stringify(rows[0]).includes("referrer"), false);
  assert.equal((await handler({ httpMethod: "POST", body: JSON.stringify({ placement_id: placement, source: "UNKNOWN" }) })).statusCode, 400);
  assert.equal(rows.length, 1);
});
// AFFILIATE_ENTRY_V1
''',
}


class InstrumentationError(RuntimeError):
    pass


def _git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False)
    if result.returncode:
        raise InstrumentationError("git command failed")
    return result.stdout.strip()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _transform_library(text):
    text = text.replace(
        'const TOKEN = /^(ai|ho|ej|ee)_[a-z2-7]{20}$/;',
        'const TOKEN = /^(?:(ai|ho|ej|ee)_[a-z2-7]{20}|af_([a-z0-9][a-z0-9-]{2,80}))$/; // AFFILIATE_CTA_V1',
    )
    text = text.replace(
        'function destination(product, token, providerToken) {\n  if (product.kind === "app") {',
        'function destination(product, token, providerToken) {\n  if (product.kind === "affiliate") return `https://try.elevenlabs.io/${product.placementId}`;\n  if (product.kind === "app") {',
    )
    text = text.replace(
        '    const product = match && products[match[1]];',
        '    const product = match && (match[2]\n      ? { productId: match[2], kind: "affiliate", placementId: match[2] }\n      : products[match[1]]);',
    )
    return text


def _transform_page(text):
    anchor = 'function renderMarkdown(md: string): string {'
    helper = '''// AFFILIATE_CTA_V1: fixed-host redirect; no arbitrary destination input.\nfunction trackedAffiliateHref(href: string): string {\n  try {\n    const url = new URL(href);\n    const placement = url.pathname.replace(/^\\/+|\\/+$/g, "");\n    if (url.protocol === "https:" && url.hostname === "try.elevenlabs.io" &&\n        !url.search && !url.hash && /^[a-z0-9][a-z0-9-]{2,80}$/.test(placement))\n      return `/go/af_${placement}`;\n  } catch {}\n  return href;\n}\n\n'''
    text = text.replace(anchor, helper + anchor)
    text = text.replace(
        '  const inline = (s: string) =>\n    s\n      .replace(/&/g, "&amp;")',
        '  const inline = (s: string) =>\n    s\n      .replace(/https:\\/\\/try\\.elevenlabs\\.io\\/[a-z0-9][a-z0-9-]{2,80}/g, trackedAffiliateHref)\n      .replace(/&/g, "&amp;")',
    )
    return text


def _transform_test(text):
    return text + '''
// AFFILIATE_CTA_V1
test("affiliate tokens persist exact placement before fixed-host redirect", async () => {
  const writes = [];
  const handler = makeMarketingGoHandler({
    products, providerToken: "123456", receiptId: () => "click-affiliate",
    persist: async (_key, value) => writes.push(value),
  });
  const placement = "elevenlabs-discovered-voice-changer-en-1";
  const response = await handler(event(`af_${placement}`));
  assert.equal(response.statusCode, 302);
  assert.equal(response.headers.location, `https://try.elevenlabs.io/${placement}`);
  assert.equal(writes[0].product_id, placement);
  assert.equal(JSON.stringify(writes[0]).includes("try.elevenlabs.io"), false);
});
'''


def _transform_v2_wrapper(text):
    return text.replace(
        "  if (!providerToken || !supabaseUrl || !serviceKey)",
        "  if (!supabaseUrl || !serviceKey) // AFFILIATE_CTA_V2",
    ).replace("    providerToken,", "    providerToken: providerToken || \"\",")


def _transform_v2_library(text):
    return text.replace(
        "af_([a-z0-9][a-z0-9-]{2,80})",
        "af_(elevenlabs-discovered-[a-z0-9][a-z0-9-]{2,60}-en-1)",
    ).replace(
        '  if (!products || !providerToken || typeof persist !== "function")',
        '  if (!products || typeof persist !== "function") // AFFILIATE_CTA_V2',
    ).replace(
        "    if (!product)\n      return { statusCode: 404, headers: { \"cache-control\": \"no-store\" }, body: \"Not Found\" };",
        "    if (!product)\n      return { statusCode: 404, headers: { \"cache-control\": \"no-store\" }, body: \"Not Found\" };\n    if (product.kind === \"app\" && !providerToken)\n      return { statusCode: 503, headers: { \"cache-control\": \"no-store\" }, body: \"Attribution unavailable\" };",
    )


def _transform_v2_test(text):
    return text + '''
// AFFILIATE_CTA_V2
test("affiliate redirect does not require App Store provider token", async () => {
  const writes = [];
  const handler = makeMarketingGoHandler({
    products, providerToken: "", persist: async (_key, value) => writes.push(value),
  });
  const placement = "elevenlabs-discovered-voice-changer-en-1";
  assert.equal((await handler(event(`af_${placement}`))).statusCode, 302);
  assert.equal(writes.length, 1);
  assert.equal((await handler(event("af_bad"))).statusCode, 404);
  assert.equal(writes.length, 1);
  assert.equal((await handler(event("ai_abcdefghijklmnopqrst"))).statusCode, 503);
  assert.equal(writes.length, 1);
});
'''


def _transform_v3_page(text):
    text = text.replace(
        'import WriterUnlock from "../../../components/blog/WriterUnlock";',
        'import WriterUnlock from "../../../components/blog/WriterUnlock";\nimport AffiliateEntryReceipt from "../../../components/blog/AffiliateEntryReceipt"; // AFFILIATE_ENTRY_V1',
    )
    text = text.replace(
        "function renderMarkdown(md: string): string {",
        '''function affiliatePlacement(md: string): string | null { // AFFILIATE_ENTRY_V1
  const match = md.match(/https:\\/\\/try\\.elevenlabs\\.io\\/(elevenlabs-discovered-[a-z0-9][a-z0-9-]{2,60}-en-1)/);
  return match ? match[1] : null;
}

function renderMarkdown(md: string): string {''',
    )
    text = text.replace(
        "  const html = renderMarkdown(articleMarkdown);",
        "  const html = renderMarkdown(articleMarkdown);\n  const affiliatePlacementId = affiliatePlacement(articleMarkdown);",
    )
    text = text.replace(
        '    <main className="bg-cream">',
        '    <main className="bg-cream">\n      {affiliatePlacementId && <AffiliateEntryReceipt placementId={affiliatePlacementId} />}',
    )
    return text


def _env(name):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for path in (Path(os.environ.get("LIFE_MANAGER_ENV_FILE", "~/.local/state/life-manager/.env")),):
        path = path.expanduser()
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith(f"{name}=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip().strip("\"'")
    raise InstrumentationError(f"{name} is unavailable")


def observe_clicks(state, placements):
    instrumentation = json.loads((state / "cta-instrumentation.json").read_text())
    if instrumentation.get("state") != "LIVE":
        return {"state": "WAITING_FOR_INSTRUMENTATION", "changed": False}
    base = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    rows = []
    for placement in placements:
        placement_id = placement["placement_id"]
        query = urllib.parse.urlencode({
            "select": "receipt_id,clicked_at", "product_id": f"eq.{placement_id}",
            "clicked_at": f"gte.{instrumentation['deployed_at']}", "order": "clicked_at.asc",
        })
        request = urllib.request.Request(
            f"{base}/rest/v1/marketing_click_receipts?{query}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            receipts = json.load(response)
        rows.append({
            "placement_id": placement_id, "count": len(receipts), "state": "OBSERVED",
            "first_clicked_at": receipts[0].get("clicked_at") if receipts else None,
            "last_clicked_at": receipts[-1].get("clicked_at") if receipts else None,
        })
    core = {
        "schema_version": 1, "receipt_type": "AFFILIATE_CTA_CLICK_OBSERVATION",
        "instrumentation_commit": instrumentation.get("commit"),
        "interval_start": instrumentation.get("deployed_at"),
        "placements": rows, "money_state": "NON_MONEY",
    }
    receipt_sha256 = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**core, "receipt_sha256": receipt_sha256}
    latest = state / "cta-click-observations" / "latest.json"
    changed = not latest.is_file() or json.loads(latest.read_text()).get("receipt_sha256") != receipt_sha256
    atomic_write(latest, receipt)
    if changed:
        with (state / "cta-click-observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    return {**receipt, "state": "OBSERVED", "changed": changed}


def observe_entries(state, placements):
    """Read only reduced X-entry rows; never request or retain transport metadata."""
    instrumentation = json.loads((state / "cta-instrumentation.json").read_text())
    if instrumentation.get("state") != "LIVE":
        return {"state": "WAITING_FOR_INSTRUMENTATION", "changed": False}
    base = _env("SUPABASE_URL").rstrip("/")
    key = _env("SUPABASE_SERVICE_ROLE_KEY")
    rows = []
    for placement in placements:
        placement_id = placement["placement_id"]
        query = urllib.parse.urlencode({
            "select": "receipt_id,clicked_at,campaign_token",
            "product_id": f"eq.entry:{placement_id}",
            "campaign_token": "eq.entry_x",
            "clicked_at": f"gte.{instrumentation['deployed_at']}",
            "order": "clicked_at.asc",
        })
        request = urllib.request.Request(
            f"{base}/rest/v1/marketing_click_receipts?{query}",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            receipts = json.load(response)
        rows.append({
            "placement_id": placement_id,
            "count": len(receipts),
            "state": "OBSERVED",
            "source": "X",
            "first_entered_at": receipts[0].get("clicked_at") if receipts else None,
            "last_entered_at": receipts[-1].get("clicked_at") if receipts else None,
        })
    core = {
        "schema_version": 1,
        "receipt_type": "AFFILIATE_X_OWNED_ENTRY_OBSERVATION",
        "instrumentation_commit": instrumentation.get("commit"),
        "interval_start": instrumentation.get("deployed_at"),
        "placements": rows,
        "raw_referrer_state": "NOT_REQUESTED_OR_RETAINED",
        "money_state": "NON_MONEY",
    }
    receipt_sha256 = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    receipt = {**core, "receipt_sha256": receipt_sha256}
    latest = state / "owned-entry-observations" / "latest.json"
    changed = not latest.is_file() or json.loads(latest.read_text()).get("receipt_sha256") != receipt_sha256
    atomic_write(latest, receipt)
    if changed:
        with (state / "owned-entry-observations.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    return {**receipt, "state": "OBSERVED", "changed": changed}


def join_provider_interval(state):
    cta = json.loads((state / "cta-click-observations" / "latest.json").read_text())
    entries_path = state / "owned-entry-observations" / "latest.json"
    entries = json.loads(entries_path.read_text()) if entries_path.is_file() else {"placements": []}
    interval_start = cta["interval_start"]
    snapshots = [
        json.loads(line) for line in (state / "funnel-snapshots.jsonl").read_text().splitlines()
        if line.strip()
    ]
    def observed_at(snapshot):
        values = [
            (row.get("provider_clicks") or {}).get("observed_at")
            for row in snapshot.get("placements", [])
        ]
        return min((value for value in values if isinstance(value, str)), default="")
    indexed_snapshots = list(enumerate(snapshots))
    baselines = [
        (index, row) for index, row in indexed_snapshots
        if observed_at(row) and observed_at(row) <= interval_start
    ]
    current = max(
        indexed_snapshots, key=lambda item: (observed_at(item[1]), item[0]),
        default=(-1, {}),
    )[1]
    report = json.loads((state / "provider-reports" / "partnerstack" / "latest.json").read_text())
    if not baselines or observed_at(current) < interval_start or report.get("observed_at", "") < interval_start:
        return {
            "state": "WAITING_FOR_CURRENT_PROVIDER_READBACK", "changed": False,
            "interval_start": interval_start,
            "current_link_observed_at": observed_at(current) or None,
            "current_transaction_observed_at": report.get("observed_at"),
        }
    baseline = max(baselines, key=lambda item: (observed_at(item[1]), item[0]))[1]
    baseline_rows = {row["placement_id"]: row for row in baseline["placements"]}
    current_rows = {row["placement_id"]: row for row in current["placements"]}
    cta_rows = {row["placement_id"]: row for row in cta["placements"]}
    entry_rows = {row["placement_id"]: row for row in entries.get("placements", [])}
    rows = []
    for placement_id in sorted(cta_rows):
        after = (current_rows.get(placement_id, {}).get("provider_clicks") or {})
        if not all(isinstance(after.get(key), int) for key in ("count", "unique_count")):
            raise InstrumentationError("current provider click counter unavailable")
        before = (baseline_rows.get(placement_id, {}).get("provider_clicks") or {})
        if not all(isinstance(before.get(key), int) for key in ("count", "unique_count")):
            baseline_id = hashlib.sha256(
                f"{interval_start}:{placement_id}".encode()
            ).hexdigest()
            baseline_path = state / "interval-provider-baselines" / f"{baseline_id}.json"
            baseline_exists = baseline_path.is_file()
            try:
                persisted = json.loads(baseline_path.read_text())
            except (OSError, ValueError):
                if baseline_exists:
                    raise InstrumentationError("invalid persisted provider baseline")
                persisted = {}
            if persisted:
                claimed = persisted.get("receipt_sha256")
                persisted_core = {
                    key: value for key, value in persisted.items()
                    if key != "receipt_sha256"
                }
                actual = hashlib.sha256(json.dumps(
                    persisted_core, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest()
                valid = all((
                    claimed == actual,
                    persisted.get("interval_start") == interval_start,
                    persisted.get("placement_id") == placement_id,
                    all(isinstance(persisted.get(key), int) for key in (
                        "provider_click_count", "provider_unique_click_count",
                    )),
                ))
                if not valid:
                    raise InstrumentationError("persisted provider baseline mismatch")
                before = {
                    "count": persisted["provider_click_count"],
                    "unique_count": persisted["provider_unique_click_count"],
                }
                provider_baseline_state = "INITIALIZED_FROM_CURRENT"
            else:
                before = after
                baseline_core = {
                    "schema_version": 1,
                    "receipt_type": "AFFILIATE_INTERVAL_PROVIDER_BASELINE",
                    "interval_start": interval_start,
                    "placement_id": placement_id,
                    "provider_click_count": after.get("count"),
                    "provider_unique_click_count": after.get("unique_count"),
                    "source_snapshot_sha256": current.get("snapshot_sha256"),
                    "observed_at": observed_at(current),
                }
                atomic_write(baseline_path, {
                    **baseline_core,
                    "receipt_sha256": hashlib.sha256(json.dumps(
                        baseline_core, sort_keys=True, separators=(",", ":")
                    ).encode()).hexdigest(),
                })
                provider_baseline_state = "INITIALIZED_FROM_CURRENT"
        else:
            provider_baseline_state = "OBSERVED"
        click_delta = after.get("count") - before.get("count")
        unique_delta = after.get("unique_count") - before.get("unique_count")
        if click_delta < 0 or unique_delta < 0:
            raise InstrumentationError("provider click counter regressed")
        commission = current_rows.get(placement_id, {}).get("transactions") or {}
        rows.append({
            "placement_id": placement_id,
            "x_owned_entries": (entry_rows.get(placement_id) or {}).get("count"),
            "x_owned_entry_state": (entry_rows.get(placement_id) or {}).get("state", "UNKNOWN"),
            "cta_clicks": cta_rows[placement_id]["count"],
            "provider_click_delta": click_delta,
            "provider_unique_click_delta": unique_delta,
            "provider_baseline_state": provider_baseline_state,
            "customers": None,
            "customer_state": "UNAVAILABLE_AT_EXACT_PLACEMENT",
            "transaction_count": commission.get("count", 0),
            "transaction_state": commission.get("state", "OBSERVED"),
            "money_state": "NON_MONEY_UNTIL_APPROVED_OR_PAID",
        })
    core = {
        "schema_version": 1, "receipt_type": "AFFILIATE_INTERVAL_FUNNEL_JOIN",
        "interval_start": interval_start, "interval_end": observed_at(current),
        "baseline_snapshot_sha256": baseline.get("snapshot_sha256"),
        "current_snapshot_sha256": current.get("snapshot_sha256"),
        "official_report_observed_at": report.get("observed_at"),
        "placements": rows,
    }
    receipt_sha256 = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    receipt = {**core, "receipt_sha256": receipt_sha256}
    latest = state / "interval-funnel-joins" / "latest.json"
    changed = not latest.is_file() or json.loads(latest.read_text()).get("receipt_sha256") != receipt_sha256
    atomic_write(latest, receipt)
    if changed:
        with (state / "interval-funnel-joins.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n")
    return {**receipt, "state": "OBSERVED", "changed": changed}


def _public_ready(owned_url, placement_id):
    try:
        with urllib.request.urlopen(owned_url, timeout=20) as response:
            body = html.unescape(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return False
    return f"/go/af_{placement_id}" in body


def _entry_public_ready():
    """The entry function is live only when the deployed endpoint enforces POST."""
    try:
        urllib.request.urlopen(
            "https://aniccaai.com/.netlify/functions/marketing-entry", timeout=20
        )
    except urllib.error.HTTPError as error:
        return error.code == 405 and error.headers.get("allow", "").upper() == "POST"
    except Exception:
        return False
    return False


def advance(state, landing_root, placement_id, owned_url):
    receipt_path = state / "cta-instrumentation.json"
    prior = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
    root = landing_root.resolve()
    v2_paths = {name: root / name for name in V2_FILES}
    v2_ready = all(
        path.is_file() and V2_MARKER in path.read_text(encoding="utf-8")
        for path in v2_paths.values()
    )
    v3_paths = {name: root / name for name in V3_NEW_FILES}
    page_path = root / "apps/landing/app/blog/[slug]/page.tsx"
    v3_ready = V3_MARKER in page_path.read_text(encoding="utf-8") and all(
        path.is_file() and V3_MARKER in path.read_text(encoding="utf-8")
        for path in v3_paths.values()
    )
    if (
        prior.get("state") in {"DELIVERED", "LIVE"}
        and v2_ready and v3_ready and _public_ready(owned_url, placement_id)
        and _entry_public_ready()
    ):
        receipt = {**prior, "state": "LIVE", "observed_at": datetime.now(timezone.utc).isoformat()}
        atomic_write(receipt_path, receipt)
        return {**receipt, "changed": prior.get("state") != "LIVE"}
    if _git(root, "rev-parse", "--show-toplevel") != str(root):
        raise InstrumentationError("publication worktree mismatch")
    paths = {name: root / name for name in FILES}
    if not all(MARKER in path.read_text(encoding="utf-8") for path in paths.values()):
        if _git(root, "status", "--porcelain"):
            raise InstrumentationError("publication worktree is dirty")
        for name, path in paths.items():
            if _sha(path) != FILES[name]:
                raise InstrumentationError("publication source hash drift")
        paths[next(name for name in paths if name.endswith("marketing-go.js") and "__tests__" not in name)].write_text(
            _transform_library(paths[next(name for name in paths if name.endswith("marketing-go.js") and "__tests__" not in name)].read_text()), encoding="utf-8"
        )
        test_name = next(name for name in paths if "__tests__" in name)
        paths[test_name].write_text(_transform_test(paths[test_name].read_text()), encoding="utf-8")
        page_name = next(name for name in paths if name.endswith("page.tsx"))
        paths[page_name].write_text(_transform_page(paths[page_name].read_text()), encoding="utf-8")
        completed = subprocess.run(
            ["node", "--test", "netlify/functions/_lib/__tests__/marketing-go.test.js"],
            cwd=root / "apps/landing", capture_output=True, check=False, timeout=60,
        )
        if completed.returncode:
            raise InstrumentationError("marketing-go test failed")
        _git(root, "add", "--", *paths)
        _git(root, "commit", "-m", "feat(marketing): receipt affiliate CTA redirects")
    if not all(V2_MARKER in path.read_text(encoding="utf-8") for path in v2_paths.values()):
        if _git(root, "status", "--porcelain"):
            raise InstrumentationError("publication worktree is dirty before V2")
        for name, path in v2_paths.items():
            if _sha(path) != V2_FILES[name]:
                raise InstrumentationError("publication V2 source hash drift")
        wrapper_name = next(name for name in v2_paths if name.endswith("functions/marketing-go.js"))
        library_name = next(name for name in v2_paths if name.endswith("_lib/marketing-go.js"))
        test_name = next(name for name in v2_paths if "__tests__" in name)
        v2_paths[wrapper_name].write_text(_transform_v2_wrapper(v2_paths[wrapper_name].read_text()), encoding="utf-8")
        v2_paths[library_name].write_text(_transform_v2_library(v2_paths[library_name].read_text()), encoding="utf-8")
        v2_paths[test_name].write_text(_transform_v2_test(v2_paths[test_name].read_text()), encoding="utf-8")
        completed = subprocess.run(
            ["node", "--test", "netlify/functions/_lib/__tests__/marketing-go.test.js"],
            cwd=root / "apps/landing", capture_output=True, check=False, timeout=60,
        )
        if completed.returncode:
            raise InstrumentationError("marketing-go V2 test failed")
        _git(root, "add", "--", *v2_paths)
        _git(root, "commit", "-m", "fix(marketing): admit fixed-host affiliate redirects")
    if not v3_ready:
        if _git(root, "status", "--porcelain"):
            raise InstrumentationError("publication worktree is dirty before entry instrumentation")
        for name, expected in V3_BASE_FILES.items():
            if _sha(root / name) != expected:
                raise InstrumentationError("publication entry source hash drift")
        page_path.write_text(_transform_v3_page(page_path.read_text(encoding="utf-8")), encoding="utf-8")
        for name, content in V3_NEW_FILES.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        completed = subprocess.run(
            ["node", "--test", "netlify/functions/_lib/__tests__/marketing-entry.test.js"],
            cwd=root / "apps/landing", capture_output=True, check=False, timeout=60,
        )
        if completed.returncode:
            raise InstrumentationError("marketing entry test failed")
        _git(root, "add", "--", page_path, *v3_paths.values())
        _git(root, "commit", "-m", "feat(marketing): receipt privacy-safe X entries")
    commit = _git(root, "rev-parse", "HEAD")
    remote = _git(root, "ls-remote", "origin", "refs/heads/main")
    remote_head = remote.split()[0] if remote else None
    if remote_head != commit:
        job = start_effect(state, "AFFILIATE_CTA_DEPLOY", "aniccaai.com", {"commit": commit}, {"head": remote_head}, 600)
        _git(root, "push", "origin", "HEAD:refs/heads/main")
        verify_effect(state, job["job_id"], {"state": "DELIVERED", "head": commit})
    else:
        reconcile_effect(state, "AFFILIATE_CTA_DEPLOY", "aniccaai.com", {"state": "DELIVERED", "head": commit})
    receipt = {
        "schema_version": 1, "receipt_type": "AFFILIATE_CTA_INSTRUMENTATION",
        "state": "DELIVERED", "commit": commit, "placement_id": placement_id,
        "owned_url": owned_url,
        "tracking_url_state": "NOT_PERSISTED", "deployed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write(receipt_path, receipt)
    return {**receipt, "changed": True}
