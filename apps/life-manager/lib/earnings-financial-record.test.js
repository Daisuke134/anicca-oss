"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  createFinancialEarningsWriter,
  earningsIncomeToFinancialRecord,
  polymarketEarningToFinancialRecord,
} = require("./earnings-financial-record.js");

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

test("projects verified Polymarket income, loss, and fee into the common record taxonomy", () => {
  const redeem = `0x${"b".repeat(64)}`;
  const base = { entry_key: "polymarket:cycle:income", amount_minor: 315, currency: "USD",
    occurred_at: "2026-09-11T04:00:00.000Z", tx_hash: redeem, source: "polymarket_cycle",
    meta: { redeem_tx_hash: redeem } };
  const receipts = { [redeem]: { status: "0x1", transactionHash: redeem } };
  const expected = {
    financial_external_income: ["business_revenue", "credit"],
    financial_realized_loss: ["business_cost", "debit"],
    financial_fee: ["fee", "debit"],
  };
  for (const [legacyKind, classification] of Object.entries(expected)) {
    const record = polymarketEarningToFinancialRecord({ ...base, kind: legacyKind }, {
      subjectId: "dais-local", receipts, recordedAt: "2026-09-11T05:00:00.000Z",
    });
    assert.deepEqual([record.kind, record.direction], classification);
    assert.equal(record.amount_minor, 315);
    assert.match(record.verification.evidence_refs[0], /^eip155:\/\/137\/tx\/[0-9a-f]{64}$/);
  }
});

test("Polymarket projection rejects failed or mismatched settlement receipts", () => {
  const redeem = `0x${"b".repeat(64)}`;
  const value = { entry_key: "polymarket:cycle:loss", kind: "financial_realized_loss",
    amount_minor: 315, occurred_at: "2026-09-11T04:00:00.000Z", tx_hash: redeem,
    source: "polymarket_cycle", meta: { redeem_tx_hash: redeem } };
  assert.throws(() => polymarketEarningToFinancialRecord(value, {
    subjectId: "dais-local", receipts: { [redeem]: { status: "0x0", transactionHash: redeem } },
  }), /verified.*settlement/i);
  assert.throws(() => polymarketEarningToFinancialRecord(value, {
    subjectId: "dais-local", receipts: { [redeem]: { status: "0x1", transactionHash: `0x${"c".repeat(64)}` } },
  }), /verified.*settlement/i);
});
