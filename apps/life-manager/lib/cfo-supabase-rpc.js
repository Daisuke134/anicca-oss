"use strict";

const { types: { isProxy } } = require("node:util");

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const RFC3339 = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
const DEFAULT_OPTION_KEYS = new Set(["supaUrl", "supaKey", "fetchImpl"]);

// Shared trust-boundary validator + Supabase RPC request skeleton for the
// CFO client modules (cfo-daily-run.js, cfo-daily-snapshot-store.js, and
// future Supabase RPC clients). Each caller gets its own isolated
// INTERNAL_ERRORS tag set keyed to its own error prefix, so error
// provenance never crosses module boundaries.
function createCfoSupabaseRpc(errorPrefix) {
  const INTERNAL_ERRORS = new WeakSet();
  function fail(reason) { const error = new Error(`${errorPrefix}${reason}`); INTERNAL_ERRORS.add(error); throw error; }
  function internal(error) { return error !== null && (typeof error === "object" || typeof error === "function") && INTERNAL_ERRORS.has(error); }
  function plain(value) {
    if (value === null || typeof value !== "object" || Array.isArray(value) || isProxy(value)) return false;
    try { return Object.getPrototypeOf(value) === Object.prototype; } catch { return false; }
  }
  function exact(value, allowed, reason = "invalid_input") {
    if (!plain(value)) fail(reason);
    let own;
    try { own = Reflect.ownKeys(value); } catch { fail(reason); }
    if (own.length !== allowed.size || own.some(key => typeof key !== "string" || !allowed.has(key))) fail(reason);
    for (const key of own) {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value") || !descriptor.enumerable) fail(reason);
    }
  }
  function validDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    const [year, month, day] = value.split("-").map(Number);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    return month >= 1 && month <= 12 && day >= 1 && day <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  }
  function uuid(value, reason) { if (typeof value !== "string" || !UUID.test(value) || /^0{8}-0{4}-0{4}-0{4}-0{12}$/i.test(value)) fail(reason); return value.toLowerCase(); }
  function timestamp(value) {
    const match = typeof value === "string" && RFC3339.exec(value);
    if (!match) return false;
    const [, year, month, day, hour, minute, second, , zone] = match;
    const leap = Number(year) % 4 === 0 && (Number(year) % 100 !== 0 || Number(year) % 400 === 0);
    const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (Number(month) < 1 || Number(month) > 12 || Number(day) < 1 || Number(day) > days[Number(month) - 1]
      || Number(hour) > 23 || Number(minute) > 59 || Number(second) > 59) return false;
    if (zone !== "Z" && (Number(zone.slice(1, 3)) > 23 || Number(zone.slice(4)) > 59)) return false;
    return Number.isFinite(Date.parse(value));
  }
  function validateOptions(opts, allowedKeys = DEFAULT_OPTION_KEYS) {
    if (!plain(opts)) fail("invalid_options");
    for (const key of Reflect.ownKeys(opts)) {
      const descriptor = Object.getOwnPropertyDescriptor(opts, key);
      if (typeof key !== "string" || !allowedKeys.has(key) || !descriptor || !Object.prototype.hasOwnProperty.call(descriptor, "value") || !descriptor.enumerable) fail("invalid_options");
    }
    const { supaUrl, supaKey } = opts;
    if (typeof supaUrl !== "string" || supaUrl.length === 0 || supaUrl.trim() !== supaUrl || typeof supaKey !== "string" || supaKey.length === 0 || supaKey.trim() !== supaKey) fail("missing_credentials");
    try { const parsed = new URL(supaUrl); if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) fail("invalid_endpoint"); } catch (error) { if (internal(error)) throw error; fail("invalid_endpoint"); }
    const fetchImpl = Object.prototype.hasOwnProperty.call(opts, "fetchImpl") ? opts.fetchImpl : globalThis.fetch;
    if (typeof fetchImpl !== "function" || isProxy(fetchImpl)) fail("invalid_fetch");
    return { base: supaUrl.replace(/\/+$/, ""), supaKey, fetchImpl };
  }
  function freeze(value, seen = new WeakSet()) { if (value === null || typeof value !== "object" || seen.has(value)) return value; seen.add(value); Object.values(value).forEach(child => freeze(child, seen)); return Object.freeze(value); }
  async function postRpc(config, path, payload) {
    let body;
    try { body = JSON.stringify(payload); } catch { fail("invalid_payload"); }
    const url = `${config.base}/rest/v1/rpc/${path}`;
    let response;
    try { response = await config.fetchImpl(url, { method: "POST", headers: { apikey: config.supaKey, Authorization: `Bearer ${config.supaKey}`, "Content-Type": "application/json" }, body }); } catch { fail("network"); }
    let ok, status;
    try {
      if (response === null || typeof response !== "object" || isProxy(response)) fail("invalid_response");
      ok = response.ok; status = response.status;
      if (typeof ok !== "boolean" || !Number.isInteger(status) || status < 100 || status > 599) fail("invalid_response");
    } catch (error) { if (internal(error)) throw error; fail("invalid_response"); }
    if (!ok || status < 200 || status >= 300) fail(`provider_${status}`);
    let parsed;
    try { const json = response.json; if (typeof json !== "function") fail("invalid_json"); parsed = await json.call(response); } catch (error) { if (internal(error)) throw error; fail("invalid_json"); }
    return parsed;
  }
  return { fail, internal, plain, exact, validDate, uuid, timestamp, validateOptions, freeze, postRpc };
}

module.exports = { createCfoSupabaseRpc };
