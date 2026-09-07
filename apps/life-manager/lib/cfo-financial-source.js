"use strict";

const ROOT_KEYS = new Set(["schemaVersion", "sourceId", "consent", "freshness", "asOf", "accounts", "liabilities", "evidenceRef", "partial", "actionRequired"]);
const ACCOUNT_KEYS = new Set(["accountRef", "label", "kind", "currency", "balanceMinor", "verificationStatus"]);
const LIABILITY_KEYS = new Set(["accountRef", "label", "currency", "balanceMinor", "verificationStatus"]);
const ACTION_KEYS = new Set(["kind", "sourceLabel", "actionRef"]);
const CONSENTS = new Set(["valid", "expired", "revoked", "unknown"]);
const FRESHNESS = new Set(["fresh", "stale", "unavailable"]);
const KINDS = new Set(["deposit", "card", "loan", "investment", "other"]);
const STATUSES = new Set(["provider_reported", "unavailable"]);
const ACTIONS = new Set(["reconsent", "provider_outage"]);
const ID = /^[a-z][a-z0-9_]{2,63}$/;
const ARRAY_INDEX = /^(0|[1-9]\d*)$/;
const RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
const ERROR_PREFIX = "cfo_financial_source_invalid:";

function fail(reason) { throw new Error(`${ERROR_PREFIX}${reason}`); }
function plain(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function dataProperties(value) {
  if (value === null || typeof value !== "object") return;
  for (const key of Reflect.ownKeys(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value")) fail("accessor_property");
  }
}
function denseArray(value) {
  if (!Array.isArray(value) || Object.getPrototypeOf(value) !== Array.prototype) fail("invalid_array");
  dataProperties(value);
  const own = Reflect.ownKeys(value), length = value.length;
  const lengthDescriptor = Object.getOwnPropertyDescriptor(value, "length");
  if (!lengthDescriptor || !Object.prototype.hasOwnProperty.call(lengthDescriptor, "value") || lengthDescriptor.value !== length || lengthDescriptor.enumerable) fail("invalid_array");
  if (own.length !== length + 1 || own.some((key) => key !== "length" && (typeof key !== "string" || !ARRAY_INDEX.test(key) || Number(key) >= 4294967295))) fail("invalid_array");
  for (let index = 0; index < length; index += 1) {
    const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
    if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value") || !descriptor.enumerable) fail("invalid_array");
  }
}
function keys(value, allowed) {
  dataProperties(value);
  if (!plain(value)) fail("invalid_object");
  const own = Reflect.ownKeys(value);
  if (own.length !== allowed.size || own.some((key) => typeof key !== "string" || !allowed.has(key))) fail("invalid_keys");
  for (const key of allowed) if (!Object.prototype.propertyIsEnumerable.call(value, key)) fail("invalid_keys");
}
function enumValue(value, allowed, reason = "invalid_enum") { if (typeof value !== "string" || !allowed.has(value)) fail(reason); }
function typedRef(value, pattern, reason) { if (typeof value !== "string" || !pattern.test(value)) fail(reason); }
function label(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > 80) fail("invalid_label");
  if (/\d{6}/.test(value) || /(?:\/Users\/|\/home\/)/.test(value)
    || /[a-z][a-z0-9+.-]*:\/\//i.test(value)
    || /[a-z][a-z0-9+.-]*:\/\/[^\/\s@]+(?::[^\/\s@]*)?@/i.test(value)
    || /(?:api[ _-]?key|secret|private[ _-]?key|password|token|credential|bearer)/i.test(value)) fail("unsafe_label");
}
function timestamp(value) {
  if (typeof value !== "string") fail("invalid_as_of");
  const match = RFC3339.exec(value);
  if (!match) fail("invalid_as_of");
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
  const hour = Number(match[4]), minute = Number(match[5]), second = Number(match[6]);
  if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) fail("invalid_as_of");
  const monthEnd = new Date(0);
  monthEnd.setUTCFullYear(year, month, 0);
  if (day < 1 || day > monthEnd.getUTCDate()) fail("invalid_as_of");
  const zone = match[8];
  const zoneHour = zone === "Z" ? 0 : Number(zone.slice(1, 3));
  const zoneMinute = zone === "Z" ? 0 : Number(zone.slice(4));
  if (zoneHour > 23 || zoneMinute > 59) fail("invalid_as_of");
  const offset = zoneHour * 60 + zoneMinute;
  if (offset > 23 * 60 + 59) fail("invalid_as_of");
  const fraction = match[7] ? Number(`${match[7].slice(1)}000`.slice(0, 3)) : 0;
  const local = new Date(0);
  local.setUTCFullYear(year, month - 1, day);
  local.setUTCHours(hour, minute, second, fraction);
  const expected = local.getTime() - (zone[0] === "-" ? -offset : offset) * 60000;
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed) || parsed !== expected) fail("invalid_as_of");
}
function amount(value, status, liability, freshness) {
  if (value !== null && !Number.isSafeInteger(value)) fail("invalid_amount");
  if (status === "provider_reported" && value === null) fail("provider_amount");
  if (status === "unavailable" && value !== null) fail("unavailable_amount");
  if (freshness === "unavailable" && value !== null) fail("unavailable_amount");
  if (liability && value !== null && value < 0) fail("negative_liability");
}
function entry(value, liability, references, freshness) {
  keys(value, liability ? LIABILITY_KEYS : ACCOUNT_KEYS);
  typedRef(value.accountRef, /^source_account:[a-z][a-z0-9_]{2,63}$/, "invalid_account_ref");
  if (references.has(value.accountRef)) fail("duplicate_account_ref");
  references.add(value.accountRef);
  label(value.label);
  if (!liability) enumValue(value.kind, KINDS, "invalid_kind");
  typedRef(value.currency, /^[A-Z]{3}$/, "invalid_currency");
  enumValue(value.verificationStatus, STATUSES, "invalid_status");
  amount(value.balanceMinor, value.verificationStatus, liability, freshness);
}
function action(value) {
  if (value === null) return;
  keys(value, ACTION_KEYS);
  enumValue(value.kind, ACTIONS, "invalid_action");
  label(value.sourceLabel);
  typedRef(value.actionRef, /^action:[a-z][a-z0-9_]{2,63}$/, "invalid_action_ref");
}
function freeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== "object" || seen.has(value)) return value;
  seen.add(value);
  Object.values(value).forEach((child) => freeze(child, seen));
  return Object.freeze(value);
}

function validateFinancialSourceResult(input) {
  keys(input, ROOT_KEYS);
  if (input.schemaVersion !== 1) fail("invalid_schema_version");
  typedRef(input.sourceId, ID, "invalid_source_id");
  enumValue(input.consent, CONSENTS, "invalid_consent");
  enumValue(input.freshness, FRESHNESS, "invalid_freshness");
  timestamp(input.asOf);
  if (!Array.isArray(input.accounts) || !Array.isArray(input.liabilities) || typeof input.partial !== "boolean") fail("invalid_shape");
  denseArray(input.accounts);
  denseArray(input.liabilities);
  const references = new Set();
  for (const value of input.accounts) entry(value, false, references, input.freshness);
  for (const value of input.liabilities) entry(value, true, references, input.freshness);
  typedRef(input.evidenceRef, /^evidence:[a-z][a-z0-9_]{2,63}$/, "invalid_evidence_ref");
  action(input.actionRequired);
  if (input.freshness === "fresh" && (input.consent !== "valid" || input.accounts.length === 0)) fail("invalid_fresh_state");
  if (input.freshness === "stale" && (input.consent !== "valid" || input.partial !== true)) fail("invalid_stale_state");
  if (input.freshness === "unavailable" && input.partial !== true) fail("invalid_unavailable_state");
  if (input.consent === "valid") {
    if (input.actionRequired && input.actionRequired.kind === "reconsent") fail("unexpected_reconsent");
  } else if (input.freshness !== "unavailable") fail("reconsent_required");
  else if (input.consent === "unknown" && (!input.actionRequired || input.actionRequired.kind !== "provider_outage")) fail("provider_outage_required");
  else if (input.consent !== "unknown" && (!input.actionRequired || input.actionRequired.kind !== "reconsent")) fail("reconsent_required");
  let clone;
  try { clone = structuredClone(input); } catch { fail("non_json_value"); }
  return freeze(clone);
}

module.exports = { validateFinancialSourceResult };
