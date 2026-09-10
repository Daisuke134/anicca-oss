"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createInvestmentStateStore, normalizeInvestmentState } = require("./investment-state-store.js");

test("listRunnable selects only active Cloud paper/shadow/live tenants with a bound", async () => {
  let seen;
  const row = { uid: "tenant-1", lifecycle: "in_review", deployment: "cloud", mode: "paper",
    paused: false, killed: false, core_digest: null, receipt_refs: [],
    alpaca_api_key_ref: null, alpaca_api_secret_ref: null };
  const store = createInvestmentStateStore({ query: async (sql, params) => (seen = { sql, params }, { rows: [row] }) });
  assert.deepEqual(await store.listRunnable(1), [row]);
  assert.match(seen.sql, /deployment = 'cloud'/);
  assert.match(seen.sql, /mode IN \('paper', 'shadow', 'live'\)/);
  assert.match(seen.sql, /paused = false AND killed = false/);
  assert.deepEqual(seen.params, [1]);
});

test("listRunnableForMode binds the active Cloud schedule mode in SQL", async () => {
  let seen;
  const row = { uid: "tenant-live", lifecycle: "active", deployment: "cloud", mode: "live",
    paused: false, killed: false, core_digest: null, receipt_refs: [],
    alpaca_api_key_ref: "secret://alpaca/api-key",
    alpaca_api_secret_ref: "secret://alpaca/api-secret" };
  const store = createInvestmentStateStore({ query: async (sql, params) =>
    (seen = { sql, params }, { rows: [row] }) });
  assert.deepEqual(await store.listRunnableForMode("live", 1), [row]);
  assert.match(seen.sql, /mode = \$1/);
  assert.deepEqual(seen.params, ["live", 1]);
  await assert.rejects(store.listRunnableForMode("paper", 1), /invalid/);
});

const UID = "tenant-a";
const STATE = Object.freeze({
  uid: UID,
  lifecycle: "in_review",
  deployment: "cloud",
  mode: "paper",
  paused: false,
  killed: false,
  core_digest: "a".repeat(64),
  receipt_refs: ["provider-receipt://alpaca/application/abc"],
  alpaca_api_key_ref: "secret://alpaca/api-key",
  alpaca_api_secret_ref: "secret://alpaca/api-secret",
});

test("missing tenant state truthfully starts at setup_required", async () => {
  const calls = [];
  const store = createInvestmentStateStore({ query: async (...args) => {
    calls.push(args); return { rows: [] };
  } });
  assert.deepEqual(await store.read(UID), { lifecycle: "setup_required" });
  assert.match(calls[0][0], /WHERE uid = \$1/);
  assert.deepEqual(calls[0][1], [UID]);
});

test("read is exactly tenant scoped and accepts only the persisted allowlist", async () => {
  const store = createInvestmentStateStore({ query: async (sql, values) => {
    assert.match(sql, /WHERE uid = \$1/);
    assert.deepEqual(values, [UID]);
    return { rows: [STATE] };
  } });
  assert.deepEqual(await store.read(UID), STATE);
});

test("cross-tenant or schema-drift rows fail closed", async () => {
  for (const row of [{ ...STATE, uid: "tenant-b" }, { ...STATE, api_secret: "raw" }]) {
    const store = createInvestmentStateStore({ query: async () => ({ rows: [row] }) });
    await assert.rejects(store.read(UID), /invalid|unavailable/);
  }
});

test("upsert parameterizes references only and rejects raw credentials before query", async () => {
  const calls = [];
  const store = createInvestmentStateStore({ query: async (sql, values) => {
    calls.push({ sql, values }); return { rows: [STATE] };
  } });
  assert.deepEqual(await store.upsert(UID, STATE), STATE);
  assert.match(calls[0].sql, /ON CONFLICT \(uid\) DO UPDATE/);
  assert.equal(calls[0].sql.includes(STATE.alpaca_api_secret_ref), false);
  assert.deepEqual(calls[0].values, [
    UID, "in_review", "cloud", "paper", false, false, "a".repeat(64),
    '["provider-receipt://alpaca/application/abc"]', "secret://alpaca/api-key", "secret://alpaca/api-secret",
  ]);
  await assert.rejects(store.upsert(UID, { ...STATE, alpaca_api_secret_ref: "raw-secret" }), /invalid/);
  await assert.rejects(store.upsert(UID, { ...STATE, api_key: "raw-secret" }), /invalid/);
  assert.equal(calls.length, 1);
});

test("invalid lifecycle, deployment, mode, refs, and digests fail closed", () => {
  for (const state of [
    { ...STATE, lifecycle: "unknown" }, { ...STATE, deployment: "both" }, { ...STATE, mode: "enabled" },
    { ...STATE, core_digest: "nope" }, { ...STATE, receipt_refs: ["https://example.com/raw"] },
    { ...STATE, alpaca_api_key_ref: "secret://other/api-key" },
  ]) assert.throws(() => normalizeInvestmentState(state, UID), /invalid/);
});

test("migration is service-only and has no raw credential columns", () => {
  const sql = fs.readFileSync(path.join(__dirname, "../migrations/2026-09-06-lm-investment-states.sql"), "utf8");
  assert.match(sql, /CREATE TABLE IF NOT EXISTS public\.lm_investment_states/i);
  assert.match(sql, /uid text PRIMARY KEY CHECK \(uid ~/i);
  assert.match(sql, /ENABLE ROW LEVEL SECURITY/i);
  assert.match(sql, /REVOKE ALL ON TABLE public\.lm_investment_states FROM PUBLIC/i);
  assert.match(sql, /GRANT SELECT, INSERT, UPDATE ON TABLE public\.lm_investment_states TO service_role/i);
  assert.doesNotMatch(sql, /\b(api_key|api_secret|password|token)\s+(?:text|jsonb|bytea)\b/i);
});
