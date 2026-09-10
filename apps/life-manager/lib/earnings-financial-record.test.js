"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { createFinancialEarningsWriter, earningsIncomeToFinancialRecord } = require("./earnings-financial-record.js");

function row(overrides = {}) {
  return { entry_key: "x402:tx:income", kind: "financial_external_income",
    amount_minor: 1, currency: "USD", occurred_at: "2026-09-11T04:00:00.000Z",
    tx_hash: `0x${"a".repeat(64)}`, source: "x402_sale",
    meta: { external: true, finalized: true, usdc_atomic: "10000" }, ...overrides };
}

test("preserves exact verified x402 USDC in the common FinancialRecord", () => {
  const record = earningsIncomeToFinancialRecord(row(), {
    subjectId: "dais-local", recordedAt: "2026-09-11T05:00:00.000Z",
  });
  assert.deepEqual([record.kind, record.direction, record.amount_minor, record.currency],
    ["business_revenue", "credit", 10000, "USDC"]);
  assert.equal(record.verification.status, "verified");
  assert.match(record.verification.evidence_refs[0], /^eip155:\/\/8453\/tx\/[0-9a-f]{64}$/);
});

test("rejects unfinalized, internal, proofless, and amountless earnings", () => {
  for (const invalid of [row({ meta: { external: true, finalized: false, usdc_atomic: "10000" } }),
    row({ meta: { external: false, finalized: true, usdc_atomic: "10000" } }),
    row({ tx_hash: null }), row({ meta: { external: true, finalized: true, usdc_atomic: "0" } })]) {
    assert.throws(() => earningsIncomeToFinancialRecord(invalid, { subjectId: "dais-local" }));
  }
});

test("writer preserves common-store idempotency as the legacy callback contract", async () => {
  const calls = [];
  const write = createFinancialEarningsWriter({ subjectId: "dais-local",
    now: () => "2026-09-11T05:00:00.000Z",
    store: { append: async (record) => { calls.push(record); return { created: false, record }; } } });
  assert.deepEqual(await write(row()), { ok: true, duplicate: true, entry_key: "x402:tx:income" });
  assert.equal(calls.length, 1);
});
