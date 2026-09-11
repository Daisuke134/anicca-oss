import assert from "node:assert/strict";
import { test } from "node:test";
import { authorizeEarnedSpend, authorizeSpend, computeSpendable, graduationGate } from "./treasury-policy.mjs";
import { normalizeRevenueReceipt } from "./revenue-receipt.mjs";

const RECIPIENT = `0x${"81".repeat(20)}`;
const verifiedRevenue = normalizeRevenueReceipt({
  provider: "x402", payer: `0x${"64".repeat(20)}`, recipient: RECIPIENT,
  gross: 0.003, fee: 0, refund: 0, asset: "USDC", terminal_state: "settled",
  occurred_at: "2026-08-24T01:35:29Z",
  proof: { chain_id: 8453, tx_hash: `0x${"36".repeat(32)}`, log_index: 1, verified: true },
});

test("computeSpendable subtracts reserve and committed liabilities and clamps at zero", () => {
  assert.equal(computeSpendable({ liquidUsdc: 10, reserveUsdc: 2, committedUsdc: 1.5 }), 6.5);
  assert.equal(computeSpendable({ liquidUsdc: 1, reserveUsdc: 2, committedUsdc: 0 }), 0);
});

test("authorizeSpend permits an amount inside both treasury and session caps", () => {
  assert.deepEqual(authorizeSpend({
    amountUsdc: 0.4, liquidUsdc: 2, reserveUsdc: 1, committedUsdc: 0,
    sessionSpentUsdc: 0.2, sessionCapUsdc: 1,
  }), { allowed: true, reason: "ok", spendableUsdc: 1, sessionRemainingUsdc: 0.4 });
});

test("authorizeSpend rejects a spend that crosses the reserve floor", () => {
  assert.equal(authorizeSpend({
    amountUsdc: 1.1, liquidUsdc: 2, reserveUsdc: 1, sessionSpentUsdc: 0, sessionCapUsdc: 2,
  }).reason, "reserve-floor");
});

test("authorizeSpend rejects a spend that crosses the session cap", () => {
  assert.equal(authorizeSpend({
    amountUsdc: 0.6, liquidUsdc: 10, reserveUsdc: 1, sessionSpentUsdc: 0.5, sessionCapUsdc: 1,
  }).reason, "session-cap");
});

test("authorizeSpend fails closed on malformed or missing caps", () => {
  assert.equal(authorizeSpend({ amountUsdc: 0.1, liquidUsdc: 10, reserveUsdc: 1 }).reason, "invalid-input");
  assert.equal(authorizeSpend({ amountUsdc: -1, liquidUsdc: 10, reserveUsdc: 1, sessionCapUsdc: 1 }).reason, "invalid-input");
});

test("authorizeEarnedSpend accepts only selected verified external receipt net", () => {
  const result = authorizeEarnedSpend({
    amountUsdc: 0.001, fundingReceiptIds: [verifiedRevenue.idempotency_key], reserveUsdc: 0.001,
    recipient: RECIPIENT,
    sessionSpentUsdc: 0, sessionCapUsdc: 0.001,
    revenueReceipts: [verifiedRevenue],
  });
  assert.equal(result.allowed, true);
  assert.equal(result.earnedUsdc, 0.003);
});

test("authorizeEarnedSpend subtracts costs already funded by the selected receipts", () => {
  const result = authorizeEarnedSpend({
    amountUsdc: 0.001, fundingReceiptIds: [verifiedRevenue.idempotency_key],
    revenueReceipts: [verifiedRevenue], recipient: RECIPIENT,
    fundingSpentUsdc: 0.002, reserveUsdc: 0.001,
    sessionSpentUsdc: 0, sessionCapUsdc: 1,
  });
  assert.equal(result.allowed, false);
  assert.equal(result.reason, "reserve-floor");
});

test("authorizeEarnedSpend subtracts later verified refund receipts even when only the positive receipt is selected", () => {
  const refund = normalizeRevenueReceipt({
    provider: "x402", payer: `0x${"64".repeat(20)}`, recipient: RECIPIENT,
    gross: 0, fee: 0, refund: 0.003, asset: "USDC", terminal_state: "refunded",
    occurred_at: "2026-08-25T01:35:29Z",
    proof: { chain_id: 8453, tx_hash: `0x${"37".repeat(32)}`, log_index: 2, verified: true },
  });
  const result = authorizeEarnedSpend({
    amountUsdc: 0.001, fundingReceiptIds: [verifiedRevenue.idempotency_key],
    revenueReceipts: [verifiedRevenue, refund], recipient: RECIPIENT,
    reserveUsdc: 0, sessionSpentUsdc: 0, sessionCapUsdc: 0.001,
  });
  assert.equal(result.allowed, false);
  assert.equal(result.reason, "reserve-floor");
  assert.equal(result.earnedUsdc, 0);
});

test("authorizeEarnedSpend rejects seed, human, unverified, and missing receipt provenance", () => {
  const base = { amountUsdc: 0.001, reserveUsdc: 0, sessionSpentUsdc: 0, sessionCapUsdc: 0.001, recipient: RECIPIENT };
  assert.equal(authorizeEarnedSpend({ ...base, fundingReceiptIds: ["seed:1"], revenueReceipts: [] }).reason, "invalid-funding-provenance");
  assert.equal(authorizeEarnedSpend({ ...base, fundingReceiptIds: ["r1"], revenueReceipts: [{ receipt_id: "r1", external: true, verified: false, terminal_state: "settled", net_usdc: 1 }] }).reason, "invalid-funding-provenance");
  const small = normalizeRevenueReceipt({
    provider: "x402", payer: `0x${"64".repeat(20)}`, recipient: RECIPIENT,
    gross: 0.0005, fee: 0, refund: 0, asset: "USDC", terminal_state: "settled",
    occurred_at: "2026-08-24T01:35:29Z",
    proof: { chain_id: 8453, tx_hash: `0x${"35".repeat(32)}`, log_index: 0, verified: true },
  });
  assert.equal(authorizeEarnedSpend({ ...base, fundingReceiptIds: [small.idempotency_key], revenueReceipts: [small] }).reason, "reserve-floor");
  const nonUsdc = normalizeRevenueReceipt({
    provider: "x402", payer: `0x${"64".repeat(20)}`, recipient: RECIPIENT,
    gross: 10, fee: 0, refund: 0, asset: "JPY", terminal_state: "settled",
    occurred_at: "2026-08-24T01:35:29Z",
    proof: { chain_id: 8453, tx_hash: `0x${"38".repeat(32)}`, log_index: 3, verified: true },
  });
  assert.equal(authorizeEarnedSpend({ ...base, fundingReceiptIds: [nonUsdc.idempotency_key], revenueReceipts: [nonUsdc] }).reason, "invalid-funding-provenance");
  const wrongChain = normalizeRevenueReceipt({
    provider: "x402", payer: `0x${"64".repeat(20)}`, recipient: RECIPIENT,
    gross: 1, fee: 0, refund: 0, asset: "USDC", terminal_state: "settled",
    occurred_at: "2026-08-24T01:35:29Z",
    proof: { chain_id: 1, tx_hash: `0x${"39".repeat(32)}`, log_index: 4, verified: true },
  });
  assert.equal(authorizeEarnedSpend({ ...base, fundingReceiptIds: [wrongChain.idempotency_key], revenueReceipts: [wrongChain] }).reason, "invalid-funding-provenance");
});

test("graduationGate passes only with 1.5x coverage, 30-day runway, and zero human inference", () => {
  assert.deepEqual(graduationGate({
    externalRealizedNet30d: 15, computeCost30d: 6, shelterCost30d: 4,
    liquidRunwayDays: 30, humanPaidInference30d: 0,
  }), { eligible: true, reason: "ok", coverage: 1.5 });
});

test("graduationGate rejects human-paid inference and insufficient coverage/runway", () => {
  assert.equal(graduationGate({ externalRealizedNet30d: 20, computeCost30d: 5, shelterCost30d: 5, liquidRunwayDays: 30, humanPaidInference30d: 0.01 }).reason, "human-paid-inference");
  assert.equal(graduationGate({ externalRealizedNet30d: 10, computeCost30d: 8, shelterCost30d: 4, liquidRunwayDays: 30, humanPaidInference30d: 0 }).reason, "insufficient-coverage");
  assert.equal(graduationGate({ externalRealizedNet30d: 20, computeCost30d: 5, shelterCost30d: 5, liquidRunwayDays: 29, humanPaidInference30d: 0 }).reason, "insufficient-runway");
});
