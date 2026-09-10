"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  usdcAtomic, solanaTradeToFinancialRecord, createSolanaTradeFinancialWriter,
} = require("./solana-trade-financial-record.js");

const A = "A".repeat(88);
const B = "B".repeat(88);

test("converts exact six-decimal USDC without floating rounding", () => {
  assert.equal(usdcAtomic("0.200001"), 200001n);
  assert.equal(usdcAtomic("-1.2"), -1200000n);
});

test("projects verified positive and negative round trips with every receipt", () => {
  for (const [net, expected] of [[0.2, ["business_revenue", "credit", 200000]], [-0.3, ["business_cost", "debit", 300000]]]) {
    const record = solanaTradeToFinancialRecord({ status: "recorded", signatures: [A, B], net_usdc: net }, {
      subjectId: "dais-local", occurredAt: "2026-09-11T04:00:00Z", recordedAt: "2026-09-11T04:01:00Z",
    });
    assert.deepEqual([record.kind, record.direction, record.amount_minor], expected);
    assert.deepEqual(record.verification.evidence_refs, [`solana://mainnet/tx/${A}`, `solana://mainnet/tx/${B}`]);
  }
});

test("zero net is not invented as revenue or cost", () => {
  assert.equal(solanaTradeToFinancialRecord({ status: "recorded", signatures: [A], net_usdc: 0 }, {
    subjectId: "dais-local",
  }), null);
});

test("writer exposes common store idempotency", async () => {
  const records = [];
  const writer = createSolanaTradeFinancialWriter({ subjectId: "dais-local",
    now: () => "2026-09-11T04:00:00Z",
    store: { append: async (record) => { records.push(record); return { created: false }; } } });
  const result = await writer({ status: "duplicate", signatures: [A, B], net_usdc: "0.2" });
  assert.equal(result.duplicate, true);
  assert.equal(records.length, 1);
});

test("writer uses the trade timestamp so retry produces an identical record", async () => {
  const records = [];
  const writer = createSolanaTradeFinancialWriter({ subjectId: "dais-local",
    now: () => { throw new Error("trade timestamp should win"); },
    store: { append: async (record) => { records.push(record); return { created: records.length === 1 }; } } });
  const result = { status: "recorded", signatures: [A, B], net_usdc: "0.2",
    occurred_at: "2026-09-11T04:00:00.000Z" };
  await writer(result);
  await writer({ ...result, status: "duplicate" });
  assert.deepEqual(records[0], records[1]);
});
