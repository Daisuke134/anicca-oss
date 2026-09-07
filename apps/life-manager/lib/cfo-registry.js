"use strict";

const crypto = require("node:crypto");

const ROOT_KEYS = new Set(["schema_version", "registry_id", "relevant_runtime_prefixes", "financial_units", "runtime_exclusions", "ledger_sources"]);
const UNIT_KEYS = new Set(["financial_unit_id", "unit_kind", "display_order", "display_name", "owner_ref", "cost_center_refs", "lifecycle", "runtime_matchers", "revenue_channel_ids", "ledger_source_ids", "evidence_refs"]);
const EXCLUSION_KEYS = new Set(["exclusion_id", "runtime_matchers", "classification", "cost_treatment", "evidence_refs"]);
const LEDGER_SOURCE_KEYS = new Set(["ledger_source_id", "probe_kind", "locator", "default_status"]);
const UNIT_KINDS = new Set(["business", "personal_income"]);
const LIFECYCLES = new Set(["active", "building", "planned", "retired"]);
const PROBE_KINDS = new Set(["external", "sqlite", "jsonl", "directory", "planned"]);
const LEDGER_STATUSES = new Set(["available", "present_empty", "stale_alias", "planned", "unavailable"]);
const ID = /^[a-z][a-z0-9_]*$/;
const OWNER_REF = /^human:[a-z][a-z0-9_]*$/;
const COST_CENTER_REF = /^agent:[a-z][a-z0-9_]*$/;
const BAD_KEY = /(?:amount|balance|revenue|profit|secret|token|api.?key|account.?number|private.?key|seed)/i;
const HOME_PATH = /^(?:\/Users\/|\/home\/)/;
const ERROR_PREFIX = "cfo_registry_invalid:";

function fail(reason) { throw new Error(`${ERROR_PREFIX}${reason}`); }
function plain(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype;
}
function keys(value, allowed) {
  const unknown = Object.keys(value).find((key) => !allowed.has(key));
  if (unknown) fail(BAD_KEY.test(unknown) ? "forbidden_key" : "unknown_key");
  if ([...allowed].some((key) => !Object.prototype.hasOwnProperty.call(value, key))) fail("missing_key");
}
function text(value) {
  if (typeof value !== "string" || value.length === 0) fail("invalid_string");
  if (HOME_PATH.test(value)) fail("unsafe_path");
}
function id(value) {
  if (typeof value !== "string" || !ID.test(value)) fail("invalid_id");
}
function typedRef(value, pattern) {
  if (typeof value !== "string" || !pattern.test(value)) fail("invalid_typed_ref");
}
function safeReference(value) {
  text(value);
  if (value.includes("\0") || /(?:^|[\\/])\.\.(?:[\\/]|$)/.test(value)) fail("unsafe_reference");
  if (/^(?:~|\/|[A-Za-z]:[\\/]|\\\\)/.test(value)) fail("unsafe_reference");
  if (/^file:/i.test(value) || /[?#]/.test(value)) fail("unsafe_reference");
  if (/^[a-z][a-z0-9+.-]*:\/\/[^/]*@/i.test(value)) fail("unsafe_reference");
  if (/(?:api[_-]?key|secret|token|private[_-]?key)/i.test(value)
    || /-----BEGIN [A-Z ]+ PRIVATE KEY-----/.test(value)
    || /\d{12,}/.test(value)) fail("unsafe_reference");
}
function safeLocator(value) {
  safeReference(value);
}
function unique(values, reason = "duplicate_id") {
  if (new Set(values).size !== values.length) fail(reason);
}
function textList(value, required = false) {
  if (!Array.isArray(value) || required && value.length === 0) fail(required ? "empty_evidence_refs" : "invalid_array");
  value.forEach(safeReference);
  unique(value, "duplicate_evidence_ref");
  return value;
}
function idList(value) {
  if (!Array.isArray(value)) fail("invalid_array");
  value.forEach(id);
  unique(value);
  return value;
}
function typedRefList(value, pattern) {
  if (!Array.isArray(value)) fail("invalid_array");
  value.forEach((ref) => typedRef(ref, pattern));
  unique(value);
  return value;
}
function enumValue(value, allowed) {
  if (typeof value !== "string" || !allowed.has(value)) fail("invalid_enum");
}
function validMatcher(value) {
  if (typeof value !== "string" || value.length === 0) return false;
  const star = value.indexOf("*");
  return star < 0 || star === value.length - 1 && value.indexOf("*", star + 1) < 0;
}
function matcherList(value, required = false) {
  if (!Array.isArray(value) || required && value.length === 0) fail("invalid_matchers");
  value.forEach((matcher) => {
    if (!validMatcher(matcher)) fail("invalid_matcher");
    text(matcher);
  });
  unique(value, "duplicate_matcher");
}
function freeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== "object" || seen.has(value)) return value;
  seen.add(value);
  Object.values(value).forEach((child) => freeze(child, seen));
  return Object.freeze(value);
}

function assertParsedShape(input) {
  if (!plain(input)) fail("invalid_root");
  if (Array.isArray(input.financial_units)) {
    input.financial_units.forEach((unit) => {
      if (!plain(unit)) fail("invalid_unit");
      if (!plain(unit.display_name)) fail("invalid_display_name");
    });
  }
  if (Array.isArray(input.runtime_exclusions)) {
    input.runtime_exclusions.forEach((exclusion) => {
      if (!plain(exclusion)) fail("invalid_exclusion");
    });
  }
}

function validateRegistry(input) {
  let registry;
  try {
    assertParsedShape(input);
    registry = structuredClone(input);
  } catch (error) {
    if (error instanceof Error && error.message.startsWith(ERROR_PREFIX)) throw error;
    fail("non_json_value");
  }
  try {
    if (!plain(registry)) fail("invalid_root");
    keys(registry, ROOT_KEYS);
    if (registry.schema_version !== 1) fail("invalid_schema_version");
    id(registry.registry_id);
    if (!Array.isArray(registry.relevant_runtime_prefixes)) fail("invalid_array");
    registry.relevant_runtime_prefixes.forEach((prefix) => { text(prefix); if (prefix.includes("*")) fail("invalid_runtime_prefix"); });
    unique(registry.relevant_runtime_prefixes, "duplicate_runtime_prefix");
    if (!Array.isArray(registry.financial_units) || registry.financial_units.length === 0) fail("invalid_financial_units");
    if (!Array.isArray(registry.runtime_exclusions)) fail("invalid_runtime_exclusions");
    if (!Array.isArray(registry.ledger_sources) || registry.ledger_sources.length === 0) fail("invalid_ledger_sources");

    const targets = new Set(), orders = new Set(), channels = new Set(), ledgers = new Set();
    for (const unit of registry.financial_units) {
      if (!plain(unit)) fail("invalid_unit");
      keys(unit, UNIT_KEYS);
      id(unit.financial_unit_id);
      if (targets.has(unit.financial_unit_id)) fail("duplicate_id");
      targets.add(unit.financial_unit_id);
      enumValue(unit.unit_kind, UNIT_KINDS);
      if (!Number.isInteger(unit.display_order) || unit.display_order < 1 || orders.has(unit.display_order)) fail("invalid_display_order");
      orders.add(unit.display_order);
      if (!plain(unit.display_name) || Object.keys(unit.display_name).length === 0 || Object.keys(unit.display_name).some((locale) => !/^[a-z]{2}$/.test(locale))) fail("invalid_display_name");
      Object.values(unit.display_name).forEach(text);
      typedRef(unit.owner_ref, OWNER_REF);
      typedRefList(unit.cost_center_refs, COST_CENTER_REF);
      enumValue(unit.lifecycle, LIFECYCLES);
      matcherList(unit.runtime_matchers);
      idList(unit.revenue_channel_ids).forEach((channel) => { if (channels.has(channel)) fail("duplicate_channel_id"); channels.add(channel); });
      idList(unit.ledger_source_ids).forEach((source) => { if (ledgers.has(source)) fail("duplicate_ledger_source_id"); ledgers.add(source); });
      textList(unit.evidence_refs, true);
    }
    const catalogue = new Set();
    for (const source of registry.ledger_sources) {
      if (!plain(source)) fail("invalid_ledger_source");
      keys(source, LEDGER_SOURCE_KEYS);
      id(source.ledger_source_id);
      if (catalogue.has(source.ledger_source_id)) fail("duplicate_ledger_source_id");
      catalogue.add(source.ledger_source_id);
      enumValue(source.probe_kind, PROBE_KINDS);
      safeLocator(source.locator);
      enumValue(source.default_status, LEDGER_STATUSES);
    }
    for (const sourceId of ledgers) if (!catalogue.has(sourceId)) fail("missing_ledger_source");
    if (catalogue.size !== ledgers.size) fail("orphan_ledger_source");
    for (const exclusion of registry.runtime_exclusions) {
      if (!plain(exclusion)) fail("invalid_exclusion");
      keys(exclusion, EXCLUSION_KEYS);
      id(exclusion.exclusion_id);
      if (targets.has(exclusion.exclusion_id)) fail("duplicate_id");
      targets.add(exclusion.exclusion_id);
      matcherList(exclusion.runtime_matchers, true);
      text(exclusion.classification);
      text(exclusion.cost_treatment);
      textList(exclusion.evidence_refs, true);
    }
  } catch (error) {
    if (error instanceof Error && error.message.startsWith(ERROR_PREFIX)) throw error;
    fail("invalid_registry");
  }
  return freeze(registry);
}

function canonicalJson(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean" || typeof value === "number") {
    if (typeof value === "number" && !Number.isFinite(value)) throw new Error("canonical_json_invalid:number");
    const json = JSON.stringify(value);
    if (json === undefined) throw new Error("canonical_json_invalid:type");
    return json;
  }
  if (Array.isArray(value)) {
    return `[${value.map((item, index) => {
      if (!Object.prototype.hasOwnProperty.call(value, index)) throw new Error("canonical_json_invalid:array");
      return canonicalJson(item);
    }).join(",")}]`;
  }
  if (plain(value)) return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  throw new Error("canonical_json_invalid:type");
}
function sha256Canonical(value) {
  return crypto.createHash("sha256").update(canonicalJson(value), "utf8").digest("hex");
}
function matchesLabel(matcher, label) {
  if (typeof matcher !== "string" || typeof label !== "string") return false;
  const star = matcher.indexOf("*");
  if (star < 0) return matcher === label;
  return star === matcher.length - 1 && matcher.indexOf("*", star + 1) < 0 && label.startsWith(matcher.slice(0, -1));
}
function classifyLabel(registry, label) {
  const targets = new Map();
  for (const [items, key, kind] of [[registry.financial_units, "financial_unit_id", "financial_unit"], [registry.runtime_exclusions, "exclusion_id", "exclusion"]]) {
    for (const item of items || []) if (item.runtime_matchers.some((matcher) => matchesLabel(matcher, label))) targets.set(item[key], kind);
  }
  const targetIds = [...targets.keys()].sort();
  if (targetIds.length === 0) return { kind: "unmapped", targetIds };
  if (targetIds.length > 1) return { kind: "ambiguous", targetIds };
  return { kind: targets.get(targetIds[0]), targetIds };
}

module.exports = { validateRegistry, canonicalJson, sha256Canonical, matchesLabel, classifyLabel };
