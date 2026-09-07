"use strict";

const { types: { isProxy }, isDeepStrictEqual } = require("node:util");
const { validateFinancialSourceResult } = require("./cfo-financial-source.js");
const { composeMoneytreeRead, deriveMoneytreeState } = require("./cfo-moneytree-state.js");
const { buildCfoDailyReport } = require("./cfo-daily-snapshot.js");

const PREFIX = "cfo_recovery_snapshot_invalid:";
const RECOVERY_KEYS = new Set(["reportingDate", "observedAt", "status", "attempts", "failureKind", "moneytreeRead", "repair", "action"]);
const READ_KEYS = new Set(["schemaVersion", "source", "state"]);
const REPAIR_KEYS = new Set(["sourceLabel", "freshReread", "reconciled"]);
const ACTION_KEYS = new Set(["kind", "sourceLabel", "retryLabel", "nextRetryAt"]);
const REPORT_KEYS = new Set(["schemaVersion", "reportingDate", "revision", "state", "currency", "totals", "sources", "excluded", "repair", "action"]);
const TOTAL_KEYS = new Set(["assetsMinor", "liabilitiesMinor", "netWorthMinor", "changeMinor"]);
const SOURCE_KEYS = new Set(["sourceId", "label", "status", "asOf", "amountMinor", "verificationStatus"]);
const EXCLUDED_KEYS = new Set(["label", "reason"]);
const FAILURE_KINDS = new Set(["unauthorized", "forbidden", "expired", "revoked", "timeout", "network", "rate_limited", "provider_5xx", "provider_outage"]);
const ACTION_LABELS = Object.freeze({ reconsent: "接続後に自動再確認", provider_outage: "30分後に自動再確認" });
const RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;

function fail(reason) { throw new Error(`${PREFIX}${reason}`); }
function plain(value) { try { return value !== null && typeof value === "object" && !Array.isArray(value) && !isProxy(value) && Object.getPrototypeOf(value) === Object.prototype; } catch { return false; } }
function exact(value, allowed) {
  if (!plain(value)) fail("invalid_shape"); const own = Reflect.ownKeys(value);
  if (own.length !== allowed.size || own.some((key) => typeof key !== "string" || !allowed.has(key))) fail("invalid_keys");
  for (const key of own) { const descriptor = Object.getOwnPropertyDescriptor(value, key); if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value") || !descriptor.enumerable) fail("invalid_shape"); }
}
function closed(value, seen = new WeakSet()) {
  if (value === null || typeof value !== "object") { if (typeof value === "function" || typeof value === "symbol") fail("invalid_value"); return; }
  if (isProxy(value) || seen.has(value)) fail("invalid_value"); seen.add(value); const array = Array.isArray(value), own = Reflect.ownKeys(value);
  if (Object.getPrototypeOf(value) !== (array ? Array.prototype : Object.prototype)) fail("invalid_value");
  if (array && (own.length !== value.length + 1 || !own.includes("length") || Object.getOwnPropertyDescriptor(value, "length").enumerable)) fail("invalid_array");
  for (const key of own) { if (array && key === "length") continue; if (typeof key !== "string" || (array && !/^(0|[1-9]\d*)$/.test(key)) || (array && Number(key) >= value.length)) fail("invalid_value"); const descriptor = Object.getOwnPropertyDescriptor(value, key); if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value") || !descriptor.enumerable) fail("invalid_value"); closed(value[key], seen); }
}
function date(value) { if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) fail("invalid_date"); const parsed = new Date(`${value}T00:00:00Z`); if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) fail("invalid_date"); }
function timestamp(value) {
  const m = typeof value === "string" && RFC3339.exec(value); if (!m) fail("invalid_timestamp"); const year = +m[1], month = +m[2], day = +m[3], hour = +m[4], minute = +m[5], second = +m[6], zone = m[8], zh = zone === "Z" ? 0 : +zone.slice(1, 3), zm = zone === "Z" ? 0 : +zone.slice(4);
  const local = new Date(0), end = new Date(0), fraction = m[7] ? +(m[7].slice(1) + "000").slice(0, 3) : 0; local.setUTCFullYear(year, month - 1, day); local.setUTCHours(hour, minute, second, fraction); end.setUTCFullYear(year, month, 0);
  if (month < 1 || month > 12 || day < 1 || day > end.getUTCDate() || hour > 23 || minute > 59 || second > 59 || zh > 23 || zm > 59) fail("invalid_timestamp"); const offset = zone === "Z" ? 0 : (zone[0] === "-" ? -1 : 1) * (zh * 60 + zm), ms = local.getTime() - offset * 60000;
  if (!Number.isFinite(ms) || Date.parse(value) !== ms) fail("invalid_timestamp"); return ms;
}
function cloneFreeze(value) { let clone; try { clone = structuredClone(value); } catch { fail("invalid_value"); } const freeze = (item, seen = new WeakSet()) => { if (item === null || typeof item !== "object" || seen.has(item)) return; seen.add(item); Reflect.ownKeys(item).forEach((key) => freeze(item[key], seen)); Object.freeze(item); }; freeze(clone); return clone; }
function attempts(value) { if (!Number.isSafeInteger(value) || value < 1 || value > 3) fail("invalid_attempts"); }
function validateRecovery(recovery) {
  exact(recovery, RECOVERY_KEYS); date(recovery.reportingDate); timestamp(recovery.observedAt); if (!["fresh", "recovered", "action_required"].includes(recovery.status)) fail("invalid_status"); attempts(recovery.attempts);
  if (recovery.failureKind !== null && (typeof recovery.failureKind !== "string" || !FAILURE_KINDS.has(recovery.failureKind))) fail("invalid_failure");
  if (recovery.status === "action_required") {
    if (recovery.moneytreeRead !== null || recovery.repair !== null || recovery.failureKind === null) fail("invalid_action"); exact(recovery.action, ACTION_KEYS);
    if (!(recovery.action.kind in ACTION_LABELS) || recovery.action.sourceLabel !== "Moneytree" || recovery.action.retryLabel !== ACTION_LABELS[recovery.action.kind] || timestamp(recovery.action.nextRetryAt) !== timestamp(recovery.observedAt) + 1800000) fail("invalid_action");
    const reconsent = new Set(["unauthorized", "expired", "forbidden", "revoked"]); if ((reconsent.has(recovery.failureKind)) !== (recovery.action.kind === "reconsent")) fail("invalid_action");
  } else {
    if (!plain(recovery.moneytreeRead) || recovery.action !== null || recovery.failureKind !== null) fail("invalid_success"); exact(recovery.moneytreeRead, READ_KEYS); if (recovery.moneytreeRead.schemaVersion !== 1) fail("invalid_read_schema");
    const read = composeMoneytreeRead({ source: recovery.moneytreeRead.source, state: recovery.moneytreeRead.state }); if (read.source.asOf !== recovery.observedAt || read.state.observedAt !== recovery.observedAt) fail("observed_at_mismatch");
    if (recovery.status === "fresh" && (recovery.attempts !== 1 || recovery.repair !== null)) fail("invalid_fresh");
    if (recovery.status === "recovered") { exact(recovery.repair, REPAIR_KEYS); if (recovery.attempts < 2 || recovery.repair.sourceLabel !== "Moneytree" || recovery.repair.freshReread !== true || recovery.repair.reconciled !== true) fail("invalid_recovery"); }
  }
}
function actionSource(recovery) {
  const consent = recovery.action.kind === "provider_outage" ? "unknown" : ["unauthorized", "expired"].includes(recovery.failureKind) ? "expired" : "revoked";
  const source = validateFinancialSourceResult({ schemaVersion: 1, sourceId: "moneytree_mufg", consent, freshness: "unavailable", asOf: recovery.observedAt, accounts: [], liabilities: [], evidenceRef: "evidence:moneytree_unavailable", partial: true, actionRequired: { kind: recovery.action.kind, sourceLabel: "Moneytree", actionRef: recovery.action.kind === "reconsent" ? "action:moneytree_reconsent" : "action:moneytree_outage" } });
  const signal = recovery.action.kind === "provider_outage" ? "provider_outage" : consent === "expired" ? "expired" : "revoked";
  return composeMoneytreeRead({ source, state: deriveMoneytreeState({ signal, observedAt: recovery.observedAt, aggregationAsOf: null, aggregationFreshnessCutoff: null, liabilitiesExposed: false, liabilityCount: null }) });
}
function actionReport(recovery, revision) { const sourceBundle = actionSource(recovery); return { sourceBundle, report: { schemaVersion: 1, reportingDate: recovery.reportingDate, revision, state: "action_required", currency: "JPY", totals: { assetsMinor: null, liabilitiesMinor: null, netWorthMinor: null, changeMinor: null }, sources: [{ sourceId: "moneytree_mufg", label: "MUFG", status: "unavailable", asOf: recovery.observedAt, amountMinor: null, verificationStatus: "unavailable" }], excluded: [{ label: "資産", reason: "Moneytreeから取得できません" }], repair: null, action: structuredClone(recovery.action) } }; }
function canonical(input) {
  const sourceBundle = composeMoneytreeRead({ source: input.sourceBundle.source, state: input.sourceBundle.state }); closed(input.report); exact(input.report, REPORT_KEYS); exact(input.report.totals, TOTAL_KEYS); if (sourceBundle.schemaVersion !== 1 || !Array.isArray(input.report.sources) || input.report.sources.length !== 1 || !Array.isArray(input.report.excluded)) fail("invalid_report"); date(input.report.reportingDate); exact(input.report.sources[0], SOURCE_KEYS); input.report.excluded.forEach((item) => { exact(item, EXCLUDED_KEYS); if (typeof item.label !== "string" || typeof item.reason !== "string") fail("invalid_report"); });
  if (input.report.schemaVersion !== 1 || input.report.currency !== "JPY" || !Number.isSafeInteger(input.report.revision) || input.report.revision < 1) fail("invalid_report");
  if (input.report.state === "action_required") {
    const expected = actionReport({ reportingDate: input.report.reportingDate, observedAt: sourceBundle.source.asOf, action: input.report.action, failureKind: sourceBundle.source.consent === "unknown" ? "provider_outage" : sourceBundle.source.consent === "expired" ? "expired" : "revoked" }, input.report.revision);
    exact(input.report.action, ACTION_KEYS); if (!(input.report.action.kind in ACTION_LABELS) || input.report.action.sourceLabel !== "Moneytree" || input.report.action.retryLabel !== ACTION_LABELS[input.report.action.kind] || input.report.action.nextRetryAt === undefined || timestamp(input.report.action.nextRetryAt) !== timestamp(sourceBundle.source.asOf) + 1800000) fail("invalid_action"); if (!isDeepStrictEqual(input.report, expected.report) || !isDeepStrictEqual(sourceBundle, expected.sourceBundle)) fail("noncanonical_action"); return { report: input.report, sourceBundle };
  }
  if (!["partial", "recovered"].includes(input.report.state)) fail("invalid_state"); const expected = structuredClone(buildCfoDailyReport({ reportingDate: input.report.reportingDate, moneytreeRead: sourceBundle })); expected.revision = input.report.revision;
  if (input.report.state === "recovered") expected.state = "recovered", expected.repair = { sourceLabel: "Moneytree", freshReread: true, reconciled: true }; if (!isDeepStrictEqual(input.report, expected)) fail("noncanonical_report"); return { report: input.report, sourceBundle };
}
function buildCfoDailyReportFromRecovery(input) { try { exact(input, new Set(["revision", "recovery"])); if (!Number.isSafeInteger(input.revision) || input.revision < 1) fail("invalid_revision"); closed(input.recovery); validateRecovery(input.recovery); const built = input.recovery.status === "action_required" ? actionReport(input.recovery, input.revision) : { sourceBundle: composeMoneytreeRead({ source: input.recovery.moneytreeRead.source, state: input.recovery.moneytreeRead.state }), report: structuredClone(buildCfoDailyReport({ reportingDate: input.recovery.reportingDate, moneytreeRead: input.recovery.moneytreeRead })) }; built.report.revision = input.revision; if (input.recovery.status === "recovered") built.report.state = "recovered", built.report.repair = { sourceLabel: "Moneytree", freshReread: true, reconciled: true }; return validateCfoRecoverySnapshotBundle(built); } catch { throw new Error(`${PREFIX}invalid_input`); } }
function validateCfoRecoverySnapshotBundle(input) { try { exact(input, new Set(["report", "sourceBundle"])); closed(input.sourceBundle); exact(input.sourceBundle, READ_KEYS); if (input.sourceBundle.schemaVersion !== 1) fail("invalid_read_schema"); const value = canonical(input); return cloneFreeze(value); } catch { throw new Error(`${PREFIX}invalid_bundle`); } }

module.exports = { buildCfoDailyReportFromRecovery, validateCfoRecoverySnapshotBundle };
