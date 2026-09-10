"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { main } = require("./record-yield-financial.js");

test("writes a verified realized yield result to the injected common store", async () => {
  const records = [];
  const payload = { kind: "yield", action: "refill", status: "0x1",
    tx: `0x${"a".repeat(64)}`, refilled_usdc: 10.2, basis_known: true,
    basis_before_usdc: 10, realized_yield_usdc: 0.2,
    occurred_at: "2026-09-11T04:00:00.000Z" };
  const result = await main({ env: { LM_CFO_SUBJECT_ID: "tenant-a" },
    store: { append: async (record) => { records.push(record); return { created: true }; } } },
  [JSON.stringify(payload)]);
  assert.equal(result.duplicate, false);
  assert.deepEqual([records[0].kind, records[0].amount_minor], ["business_revenue", 200000]);
  assert.equal(records[0].occurred_at, payload.occurred_at);
});

test("does not call the store for a deposit", async () => {
  let calls = 0;
  const result = await main({ env: {}, store: { append: async () => { calls += 1; } } },
    [JSON.stringify({ kind: "yield", action: "deploy" })]);
  assert.equal(result.skipped, true);
  assert.equal(calls, 0);
});
