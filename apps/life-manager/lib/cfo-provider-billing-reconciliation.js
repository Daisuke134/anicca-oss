"use strict";
const crypto = require("node:crypto");
const { types: { isProxy } } = require("node:util");

const ERROR = "cfo_provider_billing_invalid:", FIELDS = ["billing_period", "service_period_start", "service_period_end", "subtotal", "tax", "total", "currency"], PROVENANCE = ["billing_account_id", "pdf_sha256", "observed_at"], AMOUNT = ["value", "currency"], SCOPE = ["kind", "ref"], CONFIRMED = ["schema_version", "provider", "billing_period", "scope", "amount", "source", "source_document_ref", "observed_at", "evidence_status"], PROVISIONAL = ["schema_version", "provider", "billing_period", "scope", "amount", "source_event_ref", "evidence_status"], ALLOCATION_ROW = ["billing_period", "project_ref", "service_ref", "sku_ref", "cost_type", "amount", "currency", "source_row_ref"], ALLOCATION_POLICY = ["version", "mappings"], ALLOCATION_MAPPING = ["project_ref", "business_id"];
const DECIMAL = /^(?:0|[1-9]\d*)(?:\.\d*[1-9])?$/, SIGNED_DECIMAL = /^-?(?:0|[1-9]\d*)(?:\.\d*[1-9])?$/, PERIOD = /^\d{4}(?:0[1-9]|1[0-2])$/, HEX = /^sha256:[0-9a-f]{64}$/;
const fail = reason => { throw new Error(ERROR + reason); };
const plain = value => { try { return value !== null && typeof value === "object" && !Array.isArray(value) && !isProxy(value) && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null); } catch { return false; } };
function exact(value, keys) { try { return plain(value) && Reflect.ownKeys(value).length === keys.length && keys.every(key => { const d = Object.getOwnPropertyDescriptor(value, key); return d && d.enumerable && Object.prototype.hasOwnProperty.call(d, "value"); }); } catch { return false; } }
const text = value => typeof value === "string" && value.length > 0 && value.trim() === value;
const period = value => typeof value === "string" && PERIOD.test(value), hash = value => typeof value === "string" && HEX.test(value);
function date(value) { if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false; const parsed = Date.parse(value + "T00:00:00Z"); return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 10) === value; }
function timestamp(value) { return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value)); }
const decimal = value => typeof value === "string" && DECIMAL.test(value);
function parts(value) { const [whole, fraction = ""] = value.split("."); return [BigInt(whole + fraction), fraction.length]; }
function sameDecimal(a, b) { const [av, as] = parts(a), [bv, bs] = parts(b), scale = as > bs ? as : bs; return av * 10n ** BigInt(scale - as) === bv * 10n ** BigInt(scale - bs); }
function difference(a, b) { const [av, as] = parts(a), [bv, bs] = parts(b), scale = as > bs ? as : bs, left = av * 10n ** BigInt(scale - as), right = bv * 10n ** BigInt(scale - bs); let value = left >= right ? left - right : right - left, result = value.toString().padStart(scale + 1, "0"); if (scale) result = result.slice(0, -scale) + "." + result.slice(-scale); result = result.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, ""); return value === 0n ? "0" : left < right ? "-" + result : result; }
function freeze(value) { if (value && typeof value === "object") { Object.values(value).forEach(freeze); Object.freeze(value); } return value; }
function validAmount(value, currency) { return exact(value, AMOUNT) && decimal(value.value) && typeof value.currency === "string" && /^[A-Z]{3}$/.test(value.currency) && (!currency || value.currency === currency); }
function validScope(value) { return exact(value, SCOPE) && value.kind === "billing_account" && hash(value.ref); }
function validConfirmed(value) { return exact(value, CONFIRMED) && value.schema_version === 1 && value.provider === "google_cloud" && period(value.billing_period) && validScope(value.scope) && validAmount(value.amount, "JPY") && value.source === "provider_invoice_pdf" && hash(value.source_document_ref) && timestamp(value.observed_at) && value.evidence_status === "provider_billed"; }
function validProvisional(value) { return exact(value, PROVISIONAL) && value.schema_version === 1 && text(value.provider) && period(value.billing_period) && validScope(value.scope) && validAmount(value.amount) && hash(value.source_event_ref) && value.evidence_status === "locally_estimated"; }
function cloneConfirmed(value) { return { schema_version: 1, provider: value.provider, billing_period: value.billing_period, scope: { kind: value.scope.kind, ref: value.scope.ref }, amount: { value: value.amount.value, currency: value.amount.currency }, source: value.source, source_document_ref: value.source_document_ref, observed_at: value.observed_at, evidence_status: value.evidence_status }; }
function cloneProvisional(value) { return { schema_version: 1, provider: value.provider, billing_period: value.billing_period, scope: { kind: value.scope.kind, ref: value.scope.ref }, amount: { value: value.amount.value, currency: value.amount.currency }, source_event_ref: value.source_event_ref, evidence_status: value.evidence_status }; }
function normalizeGoogleCloudInvoice(fields, provenance) {
  try {
    if (!exact(fields, FIELDS)) fail("invalid_fields"); if (!exact(provenance, PROVENANCE)) fail("invalid_provenance");
    if (!period(fields.billing_period) || !date(fields.service_period_start) || !date(fields.service_period_end) || fields.service_period_start > fields.service_period_end) fail("invalid_identity");
    if (fields.currency !== "JPY" || !decimal(fields.subtotal) || !decimal(fields.tax) || !decimal(fields.total)) fail("invalid_numeric");
    if (!text(provenance.billing_account_id) || typeof provenance.pdf_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(provenance.pdf_sha256) || !timestamp(provenance.observed_at)) fail("invalid_provenance");
    if (!sameDecimal(difference(fields.subtotal, "0"), fields.subtotal) || !sameDecimal(difference(fields.tax, "0"), fields.tax) || !sameDecimal(difference(fields.total, "0"), fields.total)) fail("invalid_numeric");
    if (!sameDecimal(add(fields.subtotal, fields.tax), fields.total)) fail("invalid_arithmetic");
    const scopeRef = "sha256:" + crypto.createHash("sha256").update("google_cloud:" + provenance.billing_account_id, "utf8").digest("hex");
    return freeze({ schema_version: 1, provider: "google_cloud", billing_period: fields.billing_period, scope: { kind: "billing_account", ref: scopeRef }, amount: { value: fields.total, currency: "JPY" }, source: "provider_invoice_pdf", source_document_ref: "sha256:" + provenance.pdf_sha256, observed_at: provenance.observed_at, evidence_status: "provider_billed" });
  } catch (error) { if (error && typeof error.message === "string" && error.message.startsWith(ERROR)) throw error; fail("invalid_input"); }
}
function add(a, b) { const [av, as] = parts(a), [bv, bs] = parts(b), scale = as > bs ? as : bs, value = av * 10n ** BigInt(scale - as) + bv * 10n ** BigInt(scale - bs); let result = value.toString().padStart(scale + 1, "0"); if (scale) result = result.slice(0, -scale) + "." + result.slice(-scale); return result.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, ""); }
function signedParts(value) { const negative = value[0] === "-", [whole, fraction = ""] = (negative ? value.slice(1) : value).split("."); return [(negative ? -1n : 1n) * BigInt(whole + fraction), fraction.length]; }
function signedSum(values) { let scale = 0; values.forEach(value => { const current = signedParts(value)[1]; if (current > scale) scale = current; }); let total = 0n; values.forEach(value => { const [amount, current] = signedParts(value); total += amount * 10n ** BigInt(scale - current); }); const negative = total < 0n; let result = (negative ? -total : total).toString().padStart(scale + 1, "0"); if (scale) result = result.slice(0, -scale) + "." + result.slice(-scale); result = result.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, ""); return negative ? "-" + result : result; }
function negate(value) { return value === "0" ? value : value[0] === "-" ? value.slice(1) : "-" + value; }
function list(value) { try { if (!Array.isArray(value) || isProxy(value) || Object.getPrototypeOf(value) !== Array.prototype) return false; const keys = Object.keys(value); return keys.length === value.length && Reflect.ownKeys(value).length === value.length + 1 && keys.every(key => /^(?:0|[1-9]\d*)$/.test(key) && BigInt(key) < BigInt(value.length) && Object.prototype.hasOwnProperty.call(Object.getOwnPropertyDescriptor(value, key), "value")); } catch { return false; } }
function validAllocationRow(value, periodValue, currency) { return exact(value, ALLOCATION_ROW) && typeof value.billing_period === "string" && value.billing_period === periodValue && [value.project_ref, value.service_ref, value.sku_ref].every(ref => ref === null || hash(ref)) && text(value.cost_type) && typeof value.amount === "string" && SIGNED_DECIMAL.test(value.amount) && !/^-0(?:\.0*)?$/.test(value.amount) && typeof value.currency === "string" && value.currency === currency && hash(value.source_row_ref); }
function validAllocationPolicy(value) { if (!exact(value, ALLOCATION_POLICY) || !Number.isSafeInteger(value.version) || value.version < 1 || !list(value.mappings)) return false; const seen = new Set(); return value.mappings.every(mapping => exact(mapping, ALLOCATION_MAPPING) && hash(mapping.project_ref) && text(mapping.business_id) && !seen.has(mapping.project_ref) && (seen.add(mapping.project_ref), true)); }
function reconcileProviderBilling(confirmed, provisional) {
  try {
    if (!validConfirmed(confirmed)) fail("invalid_confirmed"); if (!validProvisional(provisional)) fail("invalid_provisional");
    const frozenConfirmed = cloneConfirmed(confirmed), frozenProvisional = cloneProvisional(provisional), reason = confirmed.provider !== provisional.provider ? "provider_mismatch" : confirmed.billing_period !== provisional.billing_period ? "period_mismatch" : confirmed.scope.ref !== provisional.scope.ref ? "scope_mismatch" : confirmed.amount.currency !== provisional.amount.currency ? "currency_mismatch" : null;
    return freeze({ confirmed: frozenConfirmed, provisional: frozenProvisional, status: reason ? "unresolved" : "reconciled", reason, effective: reason ? null : "confirmed", difference: reason ? null : difference(confirmed.amount.value, provisional.amount.value) });
  } catch (error) { if (error && typeof error.message === "string" && error.message.startsWith(ERROR)) throw error; fail("invalid_input"); }
}
function allocateProviderBilling(confirmed, rows, policy) {
  try {
    if (!validConfirmed(confirmed) || !list(rows) || !rows.every(row => validAllocationRow(row, confirmed.billing_period, confirmed.amount.currency)) || !validAllocationPolicy(policy)) fail("invalid_allocation");
    const rowTotal = signedSum(rows.map(row => row.amount)); if (signedSum([rowTotal, negate(confirmed.amount.value)]) !== "0") fail("invalid_allocation");
    const mappings = new Map(policy.mappings.map(mapping => [mapping.project_ref, mapping.business_id])), totals = new Map(); let allocatedRows = 0;
    rows.forEach(row => { const business = mappings.get(row.project_ref); if (business) { allocatedRows += 1; totals.set(business, signedSum([totals.get(business) || "0", row.amount])); } });
    const businesses = [...totals].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([business_id, amount]) => ({ business_id, amount })), allocatedTotal = signedSum(businesses.map(item => item.amount)), accountTotal = confirmed.amount.value;
    return freeze({ schema_version: 1, status: "allocated", billing_period: confirmed.billing_period, currency: confirmed.amount.currency, policy_version: policy.version, account_total: accountTotal, allocated_total: allocatedTotal, unallocated_total: signedSum([accountTotal, negate(allocatedTotal)]), row_count: rows.length, allocated_row_count: allocatedRows, unallocated_row_count: rows.length - allocatedRows, businesses, evidence_status: "provider_billed_allocated" });
  } catch { fail("invalid_allocation"); }
}
module.exports = { normalizeGoogleCloudInvoice, reconcileProviderBilling, allocateProviderBilling };
