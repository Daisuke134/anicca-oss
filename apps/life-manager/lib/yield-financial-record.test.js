"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { yieldResultToFinancialRecord } = require("./yield-financial-record.js");
const TX = `0x${"a".repeat(64)}`;

function result(overrides = {}) {
  return { kind: "yield", action: "refill", status: "0x1", tx: TX,
    refilled_usdc: 10.2, basis_known: true, basis_before_usdc: 10,
    realized_yield_usdc: 0.2, ...overrides };
}

test("records only returned USDC above principal as realized yield", () => {
  const record = yieldResultToFinancialRecord(result(), {
    subjectId: "dais-local", occurredAt: "2026-09-11T04:00:00Z",
  });
  assert.deepEqual([record.kind, record.direction, record.amount_minor, record.currency],
    ["business_revenue", "credit", 200000, "USDC"]);
  assert.match(record.verification.evidence_refs[0], /^eip155:\/\/8453\/tx\//);
});

test("records principal loss separately and rejects a false formula", () => {
  const loss = yieldResultToFinancialRecord(result({ refilled_usdc: 9.7, realized_yield_usdc: -0.3 }), {
    subjectId: "dais-local", occurredAt: "2026-09-11T04:00:00Z",
  });
  assert.deepEqual([loss.kind, loss.direction, loss.amount_minor], ["business_cost", "debit", 300000]);
  assert.throws(() => yieldResultToFinancialRecord(result({ realized_yield_usdc: 9 }), {
    subjectId: "dais-local",
  }), /identity/i);
});

test("deposit, hold, failed receipt, unknown basis, and flat return produce no financial claim", () => {
  for (const row of [result({ action: "deploy" }), result({ kind: "yield_hold" }),
    result({ status: "0x0" }), result({ basis_known: false, basis_before_usdc: null,
      realized_yield_usdc: null }), result({ refilled_usdc: 10, realized_yield_usdc: 0 })]) {
    assert.equal(yieldResultToFinancialRecord(row, { subjectId: "dais-local" }), null);
  }
});
