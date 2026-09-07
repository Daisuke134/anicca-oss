"use strict";

const { types: { isProxy } } = require("node:util");
const { composeMoneytreeRead } = require("./cfo-moneytree-state.js");

const ERROR_PREFIX = "cfo_daily_snapshot_invalid:";
const INPUT_KEYS = new Set(["reportingDate", "moneytreeRead"]);
const READ_KEYS = new Set(["schemaVersion", "source", "state"]);
const INTERNAL_ERRORS = new WeakSet();

function fail(reason) { const error = new Error(`${ERROR_PREFIX}${reason}`); INTERNAL_ERRORS.add(error); throw error; }
function plain(value) { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function dataProperties(value) {
  if (value === null || typeof value !== "object") return;
  for (const key of Reflect.ownKeys(value)) {
    const descriptor = Object.getOwnPropertyDescriptor(value, key);
    if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value")) fail("accessor_property");
  }
}
function exact(value, allowed) {
  if (isProxy(value)) fail("proxy");
  dataProperties(value);
  if (!plain(value)) fail("invalid_object");
  const own = Reflect.ownKeys(value);
  if (own.length !== allowed.size || own.some((key) => typeof key !== "string" || !allowed.has(key))) fail("invalid_keys");
  for (const key of allowed) if (!Object.prototype.propertyIsEnumerable.call(value, key)) fail("invalid_keys");
}
function date(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) fail("invalid_date");
  const parsed = new Date(`${value}T00:00:00Z`);
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value) fail("invalid_date");
}
function deepFreeze(value, seen = new WeakSet()) {
  if (value === null || typeof value !== "object" || seen.has(value)) return value;
  seen.add(value); Object.values(value).forEach((child) => deepFreeze(child, seen)); return Object.freeze(value);
}

function buildCfoDailyReport(input) {
  try {
    exact(input, INPUT_KEYS);
    date(input.reportingDate);
    exact(input.moneytreeRead, READ_KEYS);
    if (input.moneytreeRead.schemaVersion !== 1) fail("invalid_read_schema");
    const read = composeMoneytreeRead({ source: input.moneytreeRead.source, state: input.moneytreeRead.state });
    const { source, state } = read;
    if (read.schemaVersion !== 1 || source.sourceId !== "moneytree_mufg" || source.consent !== "valid"
      || source.freshness !== "fresh" || source.partial !== true || source.liabilities.length !== 0
      || state.sourceId !== "moneytree_mufg" || state.retrievalStatus !== "succeeded" || state.consentStatus !== "valid"
      || state.aggregationStatus !== "unknown" || state.liabilityCoverage !== "unknown" || state.liabilityCount !== null
      || state.partial !== true || source.accounts.length === 0) fail("unsupported_moneytree_read");
    let assetsMinor = 0;
    for (const account of source.accounts) {
      if (account.currency !== "JPY" || account.verificationStatus !== "provider_reported" || !Number.isSafeInteger(account.balanceMinor)) fail("unsupported_account");
      assetsMinor += account.balanceMinor;
      if (!Number.isSafeInteger(assetsMinor)) fail("amount_overflow");
    }
    const report = {
      schemaVersion: 1, reportingDate: input.reportingDate, revision: 1, state: "partial", currency: "JPY",
      totals: { assetsMinor, liabilitiesMinor: null, netWorthMinor: null, changeMinor: null },
      sources: [{ sourceId: "moneytree_mufg", label: "MUFG", status: "fresh", asOf: source.asOf, amountMinor: assetsMinor, verificationStatus: "provider_reported" }],
      excluded: [{ label: "負債", reason: "Moneytreeの接続範囲が不明" }], repair: null, action: null,
    };
    return deepFreeze(structuredClone(report));
  } catch (error) {
    if (INTERNAL_ERRORS.has(error)) throw error;
    throw new Error(`${ERROR_PREFIX}invalid_input`);
  }
}

module.exports = { buildCfoDailyReport };
