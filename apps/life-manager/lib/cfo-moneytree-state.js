"use strict";

const { validateFinancialSourceResult } = require("./cfo-financial-source.js");
const ERROR_PREFIX = "moneytree_state_invalid:";
const INPUT_KEYS = new Set(["signal", "observedAt", "aggregationAsOf", "aggregationFreshnessCutoff", "liabilitiesExposed", "liabilityCount"]);
const STATE_KEYS = new Set(["schemaVersion", "sourceId", "retrievalStatus", "consentStatus", "consentEvidence", "observedAt", "aggregationStatus", "aggregationAsOf", "liabilityCoverage", "liabilityCount", "partial", "actionRequired"]);
const ACTION_KEYS = new Set(["kind", "actionRef"]);
const BUNDLE_KEYS = new Set(["source", "state"]);
const SIGNALS = new Set(["interactive_success", "authorized", "expired", "revoked", "provider_outage"]);
const RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
const INTERNAL_ERRORS = new WeakSet();
const BRANCHES = Object.freeze({
  interactive_success: { retrievalStatus: "succeeded", consentStatus: "valid", consentEvidence: "interactive_session", actionRequired: null },
  authorized: { retrievalStatus: "succeeded", consentStatus: "valid", consentEvidence: "provider_metadata", actionRequired: null },
  expired: { retrievalStatus: "unavailable", consentStatus: "expired", consentEvidence: "provider_metadata", actionRequired: { kind: "reconsent", actionRef: "action:moneytree_reconsent" } },
  revoked: { retrievalStatus: "unavailable", consentStatus: "revoked", consentEvidence: "provider_metadata", actionRequired: { kind: "reconsent", actionRef: "action:moneytree_reconsent" } },
  provider_outage: { retrievalStatus: "unavailable", consentStatus: "unknown", consentEvidence: "provider_error", actionRequired: { kind: "provider_outage", actionRef: "action:moneytree_outage" }, },
});

function fail(reason) { const error = new Error(`${ERROR_PREFIX}${reason}`); INTERNAL_ERRORS.add(error); throw error; }
function consumeInternalError(error) {
  if (error === null || (typeof error !== "object" && typeof error !== "function") || !INTERNAL_ERRORS.has(error)) return false;
  INTERNAL_ERRORS.delete(error); return true;
}
function snapshotInput(value) {
  keys(value, INPUT_KEYS);
  try { return structuredClone(value); } catch { fail("invalid_input"); }
}
function plain(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function dataProperties(value) {
  if (value === null || typeof value !== "object") return;
  for (const key of Reflect.ownKeys(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value")) fail("accessor_property");
  }
}
function keys(value, allowed) {
  dataProperties(value);
  if (!plain(value)) fail("invalid_object");
  const own = Reflect.ownKeys(value);
  if (own.length !== allowed.size || own.some((key) => typeof key !== "string" || !allowed.has(key))) fail("invalid_keys");
  for (const key of allowed) if (!Object.prototype.propertyIsEnumerable.call(value, key)) fail("invalid_keys");
}
function enumValue(value, allowed, reason) { if (typeof value !== "string" || !allowed.has(value)) fail(reason); }
function timestamp(value) {
  if (typeof value !== "string") fail("invalid_timestamp");
  const match = RFC3339.exec(value);
  if (!match) fail("invalid_timestamp");
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
  const hour = Number(match[4]), minute = Number(match[5]), second = Number(match[6]);
  if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) fail("invalid_timestamp");
  const monthEnd = new Date(0); monthEnd.setUTCFullYear(year, month, 0);
  if (day < 1 || day > monthEnd.getUTCDate()) fail("invalid_timestamp");
  const zone = match[8], zoneHour = zone === "Z" ? 0 : Number(zone.slice(1, 3)), zoneMinute = zone === "Z" ? 0 : Number(zone.slice(4));
  if (zoneHour > 23 || zoneMinute > 59) fail("invalid_timestamp");
  const fraction = match[7] ? Number(`${match[7].slice(1)}000`.slice(0, 3)) : 0;
  const local = new Date(0); local.setUTCFullYear(year, month - 1, day); local.setUTCHours(hour, minute, second, fraction);
  const offset = zoneHour * 60 + zoneMinute, expected = local.getTime() - (zone[0] === "-" ? -offset : offset) * 60000;
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed) || parsed !== expected) fail("invalid_timestamp");
  return parsed;
}
function freeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== "object" || seen.has(value)) return value;
  seen.add(value); Object.values(value).forEach((child) => freeze(child, seen)); return Object.freeze(value);
}
function validateInput(value) {
  keys(value, INPUT_KEYS); enumValue(value.signal, SIGNALS, "invalid_signal"); timestamp(value.observedAt);
  const hasAsOf = value.aggregationAsOf !== null, hasCutoff = value.aggregationFreshnessCutoff !== null;
  if (hasAsOf !== hasCutoff) fail("incomplete_aggregation_metadata");
  if (hasAsOf) { if (value.signal !== "authorized") fail("aggregation_requires_authorized"); timestamp(value.aggregationAsOf); timestamp(value.aggregationFreshnessCutoff); }
  if (typeof value.liabilitiesExposed !== "boolean") fail("invalid_liability_exposure");
  if (!value.liabilitiesExposed && value.liabilityCount !== null) fail("unexposed_liability_count");
  if (value.liabilitiesExposed) { if (value.signal !== "authorized") fail("liability_requires_authorized"); if (!Number.isSafeInteger(value.liabilityCount) || value.liabilityCount < 0) fail("invalid_liability_count"); }
  return hasAsOf;
}
function stateAction(value) {
  if (value === null) return;
  keys(value, ACTION_KEYS); enumValue(value.kind, new Set(["reconsent", "provider_outage"]), "invalid_action");
  enumValue(value.actionRef, new Set(["action:moneytree_reconsent", "action:moneytree_outage"]), "invalid_action_ref");
}
function validateState(value) {
  keys(value, STATE_KEYS);
  if (value.schemaVersion !== 1 || value.sourceId !== "moneytree_mufg") fail("invalid_state_identity");
  enumValue(value.retrievalStatus, new Set(["succeeded", "unavailable"]), "invalid_retrieval_status");
  enumValue(value.consentStatus, new Set(["valid", "expired", "revoked", "unknown"]), "invalid_consent_status");
  enumValue(value.consentEvidence, new Set(["interactive_session", "provider_metadata", "provider_error"]), "invalid_consent_evidence");
  timestamp(value.observedAt); enumValue(value.aggregationStatus, new Set(["fresh", "stale", "unknown"]), "invalid_aggregation_status");
  if (value.aggregationStatus === "unknown") { if (value.aggregationAsOf !== null) fail("unknown_aggregation_time"); }
  else timestamp(value.aggregationAsOf);
  enumValue(value.liabilityCoverage, new Set(["complete", "unknown"]), "invalid_liability_coverage");
  if (value.liabilityCoverage === "unknown") { if (value.liabilityCount !== null) fail("unknown_liability_count"); }
  else if (!Number.isSafeInteger(value.liabilityCount) || value.liabilityCount < 0) fail("invalid_liability_count");
  if (typeof value.partial !== "boolean") fail("invalid_partial"); stateAction(value.actionRequired);
  const unavailable = value.retrievalStatus === "unavailable";
  if (unavailable !== (value.consentStatus !== "valid")) fail("invalid_state_pair");
  if (value.consentStatus === "valid") {
    if (!["interactive_session", "provider_metadata"].includes(value.consentEvidence) || value.actionRequired !== null) fail("invalid_valid_state");
  } else if (value.consentStatus === "unknown") {
    if (value.consentEvidence !== "provider_error" || !value.actionRequired || value.actionRequired.kind !== "provider_outage" || value.actionRequired.actionRef !== "action:moneytree_outage") fail("invalid_outage_state");
  } else if (value.consentEvidence !== "provider_metadata" || !value.actionRequired || value.actionRequired.kind !== "reconsent" || value.actionRequired.actionRef !== "action:moneytree_reconsent") fail("invalid_reconsent_state");
  if (unavailable && (value.aggregationStatus !== "unknown" || value.liabilityCoverage !== "unknown")) fail("invalid_unavailable_coverage");
  if (value.consentEvidence === "interactive_session" && (value.aggregationStatus !== "unknown" || value.liabilityCoverage !== "unknown")) fail("invalid_interactive_coverage");
  if (value.partial !== (unavailable || value.aggregationStatus !== "fresh" || value.liabilityCoverage !== "complete")) fail("invalid_partial_state");
  try { return freeze(structuredClone(value)); } catch { fail("non_json_value"); }
}
function deriveMoneytreeState(input) {
  try {
    const snapshot = snapshotInput(input), hasAggregation = validateInput(snapshot), branch = BRANCHES[snapshot.signal];
    const aggregationStatus = snapshot.signal === "authorized" && hasAggregation ? (timestamp(snapshot.aggregationAsOf) >= timestamp(snapshot.aggregationFreshnessCutoff) ? "fresh" : "stale") : "unknown";
    const result = { schemaVersion: 1, sourceId: "moneytree_mufg", ...branch, observedAt: snapshot.observedAt, aggregationStatus, aggregationAsOf: aggregationStatus === "unknown" ? null : snapshot.aggregationAsOf, liabilityCoverage: snapshot.liabilitiesExposed ? "complete" : "unknown", liabilityCount: snapshot.liabilitiesExposed ? snapshot.liabilityCount : null, partial: branch.retrievalStatus === "unavailable" || aggregationStatus !== "fresh" || !snapshot.liabilitiesExposed, };
    return freeze(structuredClone(result));
  } catch (error) {
    if (consumeInternalError(error)) throw error;
    throw new Error(`${ERROR_PREFIX}invalid_input`);
  }
}
function composeMoneytreeRead(input) {
  try {
    keys(input, BUNDLE_KEYS);
    const source = validateFinancialSourceResult(input.source), state = validateState(input.state);
    if (source.sourceId !== state.sourceId) fail("source_id_mismatch");
    if (source.asOf !== state.observedAt) fail("observed_at_mismatch");
    if (source.consent !== state.consentStatus) fail("consent_mismatch");
    if ((state.retrievalStatus === "succeeded") !== (source.freshness !== "unavailable")) fail("availability_mismatch");
    if (source.partial !== state.partial) fail("partial_mismatch");
    if (state.liabilityCoverage === "complete" ? source.liabilities.length !== state.liabilityCount : source.liabilities.length !== 0) fail("liability_mismatch");
    const expectedAction = state.actionRequired, actualAction = source.actionRequired;
    if (!!expectedAction !== !!actualAction || (expectedAction && (expectedAction.kind !== actualAction.kind || expectedAction.actionRef !== actualAction.actionRef))) fail("action_mismatch");
    return freeze(structuredClone({ schemaVersion: 1, source, state }));
  } catch (error) {
    if (consumeInternalError(error)) throw error;
    throw new Error(`${ERROR_PREFIX}invalid_composition`);
  }
}

module.exports = { deriveMoneytreeState, composeMoneytreeRead };
