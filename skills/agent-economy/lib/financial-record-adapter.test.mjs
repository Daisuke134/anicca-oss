import assert from "node:assert/strict";
import test from "node:test";

import { revenueReceiptToFinancialRecords } from "./financial-record-adapter.mjs";
import { normalizeRevenueReceipt } from "./revenue-receipt.mjs";

function receipt(overrides = {}) {
  return normalizeRevenueReceipt({
    provider: "stripe", payer: "customer-1", recipient: "life-manager",
    gross: "10.00", fee: "0.50", refund: "1.00", asset: "USD",
    terminal_state: "settled", occurred_at: "2026-09-07T01:00:00Z",
    proof: { provider_receipt_id: "pi_live_1", verified: true }, ...overrides,
  });
}

test("verified revenue becomes revenue, fee and refund records without net double counting", () => {
  const records = revenueReceiptToFinancialRecords(receipt(), {
    subjectId: "tenant-1", recordedAt: "2026-09-07T01:01:00Z",
  });
  assert.deepEqual(records.map((row) => [row.kind, row.direction, row.amount_minor]), [
    ["business_revenue", "credit", 1000], ["fee", "debit", 50], ["business_cost", "debit", 100],
  ]);
  assert.equal(records.every((row) => row.verification.status === "verified"), true);
  assert.equal(new Set(records.map((row) => row.idempotency_key)).size, 3);
  assert.deepEqual(records, revenueReceiptToFinancialRecords(receipt(), {
    subjectId: "tenant-1", recordedAt: "2026-09-07T01:01:00Z",
  }));
  assert.notEqual(records[0].record_id, revenueReceiptToFinancialRecords(receipt(), {
    subjectId: "tenant-2", recordedAt: "2026-09-07T01:01:00Z",
  })[0].record_id);
});

test("USDC chain revenue preserves six-decimal minor units and proof", () => {
  const chain = receipt({
    provider: "x402", gross: "1.234567", fee: "0", refund: "0", asset: "USDC",
    proof: { chain_id: 8453, tx_hash: `0x${"a".repeat(64)}`, log_index: 2, verified: true },
  });
  const [record] = revenueReceiptToFinancialRecords(chain, { subjectId: "tenant-1" });
  assert.equal(record.amount_minor, 1234567);
  assert.equal(record.currency, "USDC");
  assert.match(record.verification.evidence_refs[0], /^eip155:\/\/8453\/tx\/a{64}\/2$/);
});

test("failed terminals never book revenue and refund corrections never repeat gross", () => {
  assert.deepEqual(revenueReceiptToFinancialRecords(receipt({ terminal_state: "failed" }), { subjectId: "tenant-1" }), []);
  const refunded = receipt({ gross: "10", fee: "0.5", refund: "10", terminal_state: "refunded" });
  assert.deepEqual(
    revenueReceiptToFinancialRecords(refunded, { subjectId: "tenant-1" }).map((row) => row.kind),
    ["business_cost"],
  );
});

test("maximum safe USDC units retain their exact decimal representation", () => {
  const exact = receipt({ gross: "9007199254.740991", fee: "0", refund: "0", asset: "USDC" });
  const [record] = revenueReceiptToFinancialRecords(exact, { subjectId: "tenant-1" });
  assert.equal(record.amount_minor, Number.MAX_SAFE_INTEGER);
  assert.equal(exact.gross_decimal, "9007199254.740991");
});

test("legacy v2 numeric receipts convert only when minor units remain safe", () => {
  const current = receipt();
  const legacy = { ...current };
  delete legacy.gross_decimal;
  delete legacy.fee_decimal;
  delete legacy.refund_decimal;
  delete legacy.signed_net_decimal;
  assert.deepEqual(
    revenueReceiptToFinancialRecords(legacy, { subjectId: "tenant-1" }).map((row) => row.amount_minor),
    [1000, 50, 100],
  );

  const unsafe = { ...legacy, asset: "USDC", gross: 9007199254.740992, fee: 0, refund: 0, signed_net: 9007199254.740992 };
  assert.throws(() => revenueReceiptToFinancialRecords(unsafe, { subjectId: "tenant-1" }), /normalized verified|safe minor units|asset precision/);
  const adjacentCollision = { ...legacy, asset: "USDC", gross: 9007199254.74099, fee: 0, refund: 0, signed_net: 9007199254.74099 };
  assert.throws(
    () => revenueReceiptToFinancialRecords(adjacentCollision, { subjectId: "tenant-1" }),
    /cannot distinguish adjacent minor units/,
  );
});
