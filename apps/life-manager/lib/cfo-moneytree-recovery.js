"use strict";

const { types: { isProxy } } = require("node:util");
const { composeMoneytreeRead } = require("./cfo-moneytree-state.js");
const { buildCfoDailyReport } = require("./cfo-daily-snapshot.js");
const PREFIX = "cfo_moneytree_recovery_failed:", TRANSIENT = new Set(["timeout", "network", "rate_limited", "provider_5xx"]), RECONSENT = new Set(["unauthorized", "forbidden", "expired", "revoked"]), KINDS = new Set([...TRANSIENT, ...RECONSENT, "provider_outage"]), WAITS = Object.freeze([1000, 5000]);
const INPUT_KEYS = new Set(["reportingDate", "observedAt"]), OPTION_KEYS = new Set(["read", "repair", "wait"]), RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;

function fail(code) { throw new Error(`${PREFIX}${code}`); }
function plain(value) { try { return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; } catch { return false; } }
function exact(value, allowed) {
  if (isProxy(value) || !plain(value)) fail("invalid_input"); let own; try { own = Reflect.ownKeys(value); } catch { fail("invalid_input"); }
  if (own.length !== allowed.size || own.some((key) => typeof key !== "string" || !allowed.has(key))) fail("invalid_keys");
  for (const key of own) { const descriptor = Object.getOwnPropertyDescriptor(value, key); if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value") || !descriptor.enumerable) fail("invalid_keys"); }
}
function timestamp(value) {
  const m = typeof value === "string" && RFC3339.exec(value); if (!m) fail("invalid_timestamp");
  const year = +m[1], month = +m[2], day = +m[3], hour = +m[4], minute = +m[5], second = +m[6], zone = m[8], zh = zone === "Z" ? 0 : +zone.slice(1, 3), zm = zone === "Z" ? 0 : +zone.slice(4);
  if (month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59 || zh > 23 || zm > 59) fail("invalid_timestamp");
  const fraction = m[7] ? m[7].slice(1) : "", local = new Date(0), end = new Date(0); local.setUTCFullYear(year, month - 1, day); local.setUTCHours(hour, minute, second, +(`${fraction}000`.slice(0, 3) || 0)); end.setUTCFullYear(year, month, 0);
  if (day < 1 || day > end.getUTCDate()) fail("invalid_timestamp"); const offset = zone === "Z" ? 0 : (zone[0] === "-" ? -1 : 1) * (zh * 60 + zm), ms = local.getTime() - offset * 60000;
  if (!Number.isFinite(ms) || Date.parse(value) !== ms) fail("invalid_timestamp"); return { ms, zone, offset, fraction };
}
function calendarDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) fail("invalid_date"); const [year, month, day] = value.split("-").map(Number), date = new Date(0); date.setUTCFullYear(year, month - 1, day); date.setUTCHours(0, 0, 0, 0); if (date.toISOString().slice(0, 10) !== value) fail("invalid_date");
}
function retryAt(parsed) {
  const value = new Date(parsed.ms + 30 * 60000 + parsed.offset * 60000), year = value.getUTCFullYear(); if (year < 0 || year > 9999) fail("invalid_timestamp");
  return `${String(year).padStart(4, "0")}-${String(value.getUTCMonth() + 1).padStart(2, "0")}-${String(value.getUTCDate()).padStart(2, "0")}T${String(value.getUTCHours()).padStart(2, "0")}:${String(value.getUTCMinutes()).padStart(2, "0")}:${String(value.getUTCSeconds()).padStart(2, "0")}${parsed.fraction ? `.${parsed.fraction}` : ""}${parsed.zone}`;
}
function frozen(value) {
  let clone; try { clone = structuredClone(value); } catch { fail("result"); } const seen = new WeakSet();
  const freeze = (item) => { if (item === null || typeof item !== "object" || seen.has(item)) return item; seen.add(item); Reflect.ownKeys(item).forEach((key) => freeze(item[key])); return Object.freeze(item); }; return freeze(clone);
}
function callbackResult(value) {
  if (isProxy(value) || !plain(value) || typeof value.ok !== "boolean") fail("callback_result"); exact(value, value.ok ? new Set(["ok", "moneytreeRead"]) : new Set(["ok", "kind"]));
  if (value.ok) { if (isProxy(value.moneytreeRead) || !plain(value.moneytreeRead)) fail("callback_result"); } else if (typeof value.kind !== "string" || !KINDS.has(value.kind)) fail("callback_result"); return value;
}
async function read(options) { let value; try { value = await options.read(); } catch { fail("read"); } return callbackResult(value); }
async function repair(options, kind, attempt) { let value; try { value = await options.repair({ kind, attempt }); } catch { fail("repair"); } if (typeof value !== "boolean") fail("repair_result"); return value; }
async function wait(options, milliseconds) { let value; try { value = await options.wait(milliseconds); } catch { fail("wait"); } if (value !== undefined) fail("wait_result"); }
function composed(reportingDate, value) { try { const moneytreeRead = composeMoneytreeRead({ source: value.source, state: value.state }); buildCfoDailyReport({ reportingDate, moneytreeRead }); return moneytreeRead; } catch { return null; } }
function outcome(base, status, moneytreeRead, attempts, repairProof, actionValue, failureKind) { return frozen({ status, reportingDate: base.reportingDate, observedAt: base.observedAt, moneytreeRead, attempts, repair: repairProof, action: actionValue, failureKind }); }
function action(base, parsed, kind, attempts, failureKind) { const reconsent = RECONSENT.has(kind); return outcome(base, "action_required", null, attempts, null, { kind: reconsent ? "reconsent" : "provider_outage", sourceLabel: "Moneytree", retryLabel: reconsent ? "接続後に自動再確認" : "30分後に自動再確認", nextRetryAt: retryAt(parsed) }, failureKind); }

async function recoverMoneytreeRead(input, options) {
  try {
    exact(input, INPUT_KEYS); exact(options, OPTION_KEYS); calendarDate(input.reportingDate); const parsed = timestamp(input.observedAt); for (const key of OPTION_KEYS) if (typeof options[key] !== "function") fail("invalid_callback");
    const base = { reportingDate: input.reportingDate, observedAt: input.observedAt }; let result = await read(options), attempts = 1;
    if (result.ok) { const composedRead = composed(base.reportingDate, result.moneytreeRead); return composedRead ? outcome(base, "fresh", composedRead, attempts, null, null, null) : action(base, parsed, "provider_outage", attempts, "provider_outage"); }
    let currentKind = result.kind; if (RECONSENT.has(currentKind) || currentKind === "provider_outage") return action(base, parsed, currentKind, attempts, currentKind);
    for (let index = 0; index < WAITS.length; index += 1) {
      if (!await repair(options, currentKind, index + 1)) continue; await wait(options, WAITS[index]); result = await read(options); attempts += 1;
      if (!result.ok) { currentKind = result.kind; if (RECONSENT.has(currentKind) || currentKind === "provider_outage") return action(base, parsed, currentKind, attempts, currentKind); if (!TRANSIENT.has(currentKind)) return action(base, parsed, "provider_outage", attempts, "provider_outage"); continue; }
      const composedRead = composed(base.reportingDate, result.moneytreeRead); if (!composedRead) return action(base, parsed, "provider_outage", attempts, "provider_outage");
      return outcome(base, "recovered", composedRead, attempts, { sourceLabel: "Moneytree", freshReread: true, reconciled: true }, null, null);
    }
    return action(base, parsed, "provider_outage", attempts, currentKind);
  } catch (error) { if (error && typeof error.message === "string" && error.message.startsWith(PREFIX)) throw error; fail("internal"); }
}

module.exports = { recoverMoneytreeRead };
