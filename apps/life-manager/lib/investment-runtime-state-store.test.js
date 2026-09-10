"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { createInvestmentRuntimeStateStore, sealRuntimeBundle } = require("./investment-runtime-state-store.js");

const UID = "tenant-a";
const INPUT = Object.freeze({
  schema_version: 1,
  exported_at: "2026-09-10T12:00:00.000Z",
  account_binding: { provider: "alpaca", endpoint: "live", account_id_hash: "a".repeat(64) },
  files: { "risk-day.json": Buffer.from('{"ny_day":"2026-09-10"}\n').toString("base64") },
});

test("runtime bundle is canonical, digest sealed, and tenant scoped", async () => {
  const sealed = sealRuntimeBundle(INPUT);
  const calls = [];
  const store = createInvestmentRuntimeStateStore({ query: async (sql, params) => {
    calls.push({ sql, params });
    return { rows: [{ uid: UID, bundle: sealed.bundle, bundle_digest: sealed.digest }] };
  } });
  assert.deepEqual(await store.upsert(UID, INPUT), sealed);
  assert.match(calls[0].sql, /ON CONFLICT \(uid\) DO UPDATE/);
  assert.equal(calls[0].params[0], UID);
  assert.equal(calls[0].params[2], sealed.digest);
  assert.deepEqual(await store.read(UID), sealed);
  assert.deepEqual(calls[1].params, [UID]);
});

test("runtime bundle rejects secrets, unknown files, bad account binding, and digest drift", async () => {
  assert.throws(() => sealRuntimeBundle({ ...INPUT, api_secret: "raw" }), /invalid/);
  assert.throws(() => sealRuntimeBundle({ ...INPUT, files: { "credentials.json": "e30=" } }), /invalid/);
  assert.throws(() => sealRuntimeBundle({ ...INPUT, account_binding: { ...INPUT.account_binding, endpoint: "paper" } }), /invalid/);
  const sealed = sealRuntimeBundle(INPUT);
  const store = createInvestmentRuntimeStateStore({ query: async () => ({ rows: [{
    uid: UID, bundle: sealed.bundle, bundle_digest: "b".repeat(64),
  }] }) });
  await assert.rejects(store.read(UID), /invalid/);
});

test("runtime state migration is service-only and contains no credential columns", () => {
  const sql = fs.readFileSync(path.join(__dirname, "../migrations/2026-09-10-lm-investment-runtime-states.sql"), "utf8");
  assert.match(sql, /CREATE TABLE IF NOT EXISTS public\.lm_investment_runtime_states/i);
  assert.match(sql, /ENABLE ROW LEVEL SECURITY/i);
  assert.match(sql, /REVOKE ALL ON TABLE public\.lm_investment_runtime_states FROM (?:PUBLIC|anon|authenticated)/i);
  assert.match(sql, /GRANT SELECT, INSERT, UPDATE ON TABLE public\.lm_investment_runtime_states TO service_role/i);
  assert.doesNotMatch(sql, /\b(api_key|api_secret|password|token)\s+(?:text|jsonb|bytea)\b/i);
});
