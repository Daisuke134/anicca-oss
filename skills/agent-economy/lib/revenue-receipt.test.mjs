import assert from "node:assert/strict";
import { test } from "node:test";
import {
  RevenueReceiptValidationError,
  canonicalRevenueReceiptKey,
  normalizeRevenueReceipt,
} from "./revenue-receipt.mjs";
import { discoverAndVerifyEvmReceipt, verifyEvmReceipt } from "../../_shared/lib/verify-tx.mjs";

const TX = `0x${"11".repeat(32)}`;
const PAYER = "0x1111111111111111111111111111111111111111";
const RECIPIENT = "0x2222222222222222222222222222222222222222";
const CONTRACT = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913";
const TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";

function topicAddress(address) {
  return `0x${address.slice(2).padStart(64, "0")}`;
}

function successfulReceipt({ recipient = RECIPIENT, payer = PAYER, contract = CONTRACT, amount = "1000000", logIndex = "0x0" } = {}) {
  return {
    status: "0x1",
    transactionHash: TX,
    logs: [{
      address: contract,
      transactionHash: TX,
      logIndex,
      topics: [TRANSFER_TOPIC, topicAddress(payer), topicAddress(recipient)],
      data: `0x${BigInt(amount).toString(16).padStart(64, "0")}`,
    }],
  };
}

function fakeRpc(byMethod) {
  return async (_url, init) => {
    const body = JSON.parse(init.body);
    if (!(body.method in byMethod)) throw new Error(`unexpected method ${body.method}`);
    return { ok: true, json: async () => ({ jsonrpc: "2.0", id: body.id, result: byMethod[body.method] }) };
  };
}

const baseInput = (overrides = {}) => ({
  provider: "x402",
  payer: PAYER,
  recipient: RECIPIENT,
  gross: "1.000000",
  fee: "0.050000",
  refund: "0",
  asset: "USDC",
  proof: { chain_id: 8453, tx_hash: TX, log_index: 0, verified: true },
  terminal_state: "settled",
  occurred_at: "2026-08-27T00:00:00.000Z",
  ...overrides,
});

test("normalizes a versioned external RevenueReceipt with signed net and canonical key", () => {
  const receipt = normalizeRevenueReceipt(baseInput());
  assert.equal(receipt.schema_version, 2);
  assert.equal(receipt.provider, "x402");
  assert.equal(receipt.payer, PAYER.toLowerCase());
  assert.equal(receipt.recipient, RECIPIENT.toLowerCase());
  assert.equal(receipt.gross, 1);
  assert.equal(receipt.fee, 0.05);
  assert.equal(receipt.refund, 0);
  assert.equal(receipt.signed_net, 0.95);
  assert.equal(receipt.gross_decimal, "1");
  assert.equal(receipt.fee_decimal, "0.05");
  assert.equal(receipt.refund_decimal, "0");
  assert.equal(receipt.signed_net_decimal, "0.95");
  assert.equal(receipt.asset, "USDC");
  assert.equal(receipt.terminal_state, "settled");
  assert.equal(receipt.occurred_at, "2026-08-27T00:00:00.000Z");
  assert.match(receipt.idempotency_key, /^revenue:v2:[0-9a-f]{64}$/);
  assert.equal(receipt.idempotency_key, canonicalRevenueReceiptKey(receipt));
  assert.ok(Object.isFrozen(receipt));
});

test("canonical key is stable when object field order changes", () => {
  const a = normalizeRevenueReceipt(baseInput());
  const b = normalizeRevenueReceipt(baseInput({ proof: { log_index: 0, tx_hash: TX, chain_id: "0x2105", verified: true } }));
  assert.equal(a.idempotency_key, b.idempotency_key);
});

test("canonical key is proof identity only", () => {
  const a = normalizeRevenueReceipt(baseInput());
  const b = normalizeRevenueReceipt(baseInput({
    provider: "other-provider",
    payer: "0x3333333333333333333333333333333333333333",
    recipient: "0x4444444444444444444444444444444444444444",
    asset: "DAI",
  }));
  assert.equal(a.idempotency_key, b.idempotency_key);
});

test("provider proof key is namespaced while same-provider metadata changes dedupe", () => {
  const stripe = normalizeRevenueReceipt(baseInput({ provider: "stripe", proof: { provider_receipt_id: "same-id", verified: true } }));
  const stripeChanged = normalizeRevenueReceipt(baseInput({ provider: "stripe", payer: "0x3333333333333333333333333333333333333333", proof: { provider_receipt_id: "same-id", verified: true } }));
  const paypal = normalizeRevenueReceipt(baseInput({ provider: "paypal", proof: { provider_receipt_id: "same-id", verified: true } }));
  assert.equal(stripe.idempotency_key, stripeChanged.idempotency_key);
  assert.notEqual(stripe.idempotency_key, paypal.idempotency_key);
});

test("v1 receipts are rejected rather than silently re-appended as v2", () => {
  const v2 = normalizeRevenueReceipt(baseInput());
  assert.throws(
    () => normalizeRevenueReceipt({ ...v2, schema_version: 1 }),
    (error) => error instanceof RevenueReceiptValidationError && error.code === "UNSUPPORTED_VERSION",
  );
  assert.throws(
    () => canonicalRevenueReceiptKey({ ...v2, schema_version: 1 }),
    (error) => error instanceof RevenueReceiptValidationError && error.code === "UNSUPPORTED_VERSION",
  );
});

test("rejects malformed signed arithmetic and non-terminal provider state", () => {
  assert.throws(
    () => normalizeRevenueReceipt(baseInput({ signed_net: "0.90" })),
    (error) => error instanceof RevenueReceiptValidationError && error.code === "ARITHMETIC_MISMATCH",
  );
  assert.throws(
    () => normalizeRevenueReceipt(baseInput({ terminal_state: "pending" })),
    (error) => error instanceof RevenueReceiptValidationError && error.code === "NON_TERMINAL",
  );
  const correction = normalizeRevenueReceipt(baseInput({ gross: "0", fee: "0", refund: "0.500000", signed_net: "-0.500000", terminal_state: "refunded" }));
  assert.equal(correction.signed_net, -0.5);
});

test("rejects an unverified provider receipt", () => {
  assert.throws(
    () => normalizeRevenueReceipt(baseInput({ proof: undefined })),
    (error) => error instanceof RevenueReceiptValidationError && error.code === "MISSING_PROOF",
  );
  assert.throws(
    () => normalizeRevenueReceipt(baseInput({ proof: { provider_receipt_id: "provider-1", verified: false } })),
    (error) => error instanceof RevenueReceiptValidationError && error.code === "UNVERIFIED_PROOF",
  );
  assert.throws(
    () => normalizeRevenueReceipt(baseInput({ proof: { provider_receipt_id: "provider-1" } })),
    (error) => error instanceof RevenueReceiptValidationError && error.code === "UNVERIFIED_PROOF",
  );
  const providerReceipt = normalizeRevenueReceipt(baseInput({ proof: { provider_receipt_id: "provider-1", verified: true } }));
  assert.deepEqual(providerReceipt.proof, { provider_receipt_id: "provider-1", verified: true });
});

test("EVM verifier binds chain, contract, payer, recipient, amount, and log index", async () => {
  const verified = await verifyEvmReceipt({
    tx_hash: TX,
    expected_chain_id: 8453,
    expected_contract: CONTRACT,
    expected_payer: PAYER,
    expected_recipient: RECIPIENT,
    expected_amount_atomic: "1000000",
    expected_log_index: 0,
    rpc: "https://rpc.invalid",
    fetchImpl: fakeRpc({ eth_chainId: "0x2105", eth_getTransactionReceipt: successfulReceipt() }),
  });
  assert.equal(verified.verified, true);
  assert.equal(verified.status, "0x1");
  assert.equal(verified.transfer.log_index, 0);
});

test("EVM discovery derives a missing log index and then reuses strict validation", async () => {
  const verified = await discoverAndVerifyEvmReceipt({
    tx_hash: TX,
    expected_chain_id: 8453,
    expected_contract: CONTRACT,
    expected_payer: PAYER,
    expected_recipient: RECIPIENT,
    expected_amount_atomic: "1000000",
    rpc: "https://rpc.invalid",
    fetchImpl: fakeRpc({ eth_chainId: "0x2105", eth_getTransactionReceipt: successfulReceipt({ logIndex: "0x7" }) }),
  });
  assert.equal(verified.verified, true);
  assert.equal(verified.transfer.log_index, 7);
});

test("EVM discovery rejects ambiguous matching transfer logs", async () => {
  const first = successfulReceipt({ logIndex: "0x0" }).logs[0];
  const second = successfulReceipt({ logIndex: "0x1" }).logs[0];
  const verified = await discoverAndVerifyEvmReceipt({
    tx_hash: TX,
    expected_chain_id: 8453,
    expected_contract: CONTRACT,
    expected_payer: PAYER,
    expected_recipient: RECIPIENT,
    expected_amount_atomic: "1000000",
    rpc: "https://rpc.invalid",
    fetchImpl: fakeRpc({ eth_chainId: "0x2105", eth_getTransactionReceipt: { status: "0x1", transactionHash: TX, logs: [first, second] } }),
  });
  assert.equal(verified.verified, false);
  assert.equal(verified.reason, "transfer_not_unique");
});

test("EVM discovery validates the same receipt snapshot exactly once", async () => {
  let receiptCalls = 0;
  const fetchImpl = async (_url, init) => {
    const body = JSON.parse(init.body);
    if (body.method === "eth_chainId") return { ok: true, json: async () => ({ result: "0x2105" }) };
    if (body.method === "eth_getTransactionReceipt") {
      receiptCalls += 1;
      // A second fetch would observe a changed/reverted receipt; discovery must not do it.
      return { ok: true, json: async () => ({ result: receiptCalls === 1 ? successfulReceipt({ logIndex: "0x3" }) : { status: "0x1", transactionHash: TX, logs: [] } }) };
    }
    throw new Error(`unexpected method ${body.method}`);
  };
  const verified = await discoverAndVerifyEvmReceipt({
    tx_hash: TX,
    expected_chain_id: 8453,
    expected_contract: CONTRACT,
    expected_payer: PAYER,
    expected_recipient: RECIPIENT,
    expected_amount_atomic: "1000000",
    rpc: "https://rpc.invalid",
    fetchImpl,
  });
  assert.equal(verified.verified, true);
  assert.equal(verified.transfer.log_index, 3);
  assert.equal(receiptCalls, 1);
});

test("EVM verifier rejects wrong recipient, wrong asset, missing transfer, and self payment", async () => {
  const verify = (receipt, expected = {}) => verifyEvmReceipt({
    tx_hash: TX,
    expected_chain_id: 8453,
    expected_contract: CONTRACT,
    expected_payer: PAYER,
    expected_recipient: RECIPIENT,
    expected_amount_atomic: "1000000",
    expected_log_index: 0,
    rpc: "https://rpc.invalid",
    fetchImpl: fakeRpc({ eth_chainId: "0x2105", eth_getTransactionReceipt: receipt }),
    ...expected,
  });
  assert.equal((await verify(successfulReceipt({ recipient: "0x3333333333333333333333333333333333333333" }))).verified, false);
  assert.equal((await verify(successfulReceipt({ contract: "0x4444444444444444444444444444444444444444" }))).verified, false);
  assert.equal((await verify({ status: "0x1", transactionHash: TX, logs: [] })).verified, false);
  assert.equal((await verify(successfulReceipt({ payer: RECIPIENT }))).verified, false);
  assert.equal((await verify(successfulReceipt(), { expected_amount_atomic: "1000001" })).verified, false);
  assert.equal((await verify(successfulReceipt(), { expected_payer: undefined })).verified, false);
});

test("EVM verifier never returns raw RPC receipt and tx-hash-only requests fail closed", async () => {
  const raw = { status: "0x1", transactionHash: TX, secret: "must-not-escape", logs: [] };
  const noExpectations = await verifyEvmReceipt({
    tx_hash: TX,
    rpc: "https://rpc.invalid",
    fetchImpl: fakeRpc({ eth_getTransactionReceipt: successfulReceipt() }),
  });
  assert.equal(noExpectations.verified, false);
  const result = await verifyEvmReceipt({
    tx_hash: TX,
    expected_chain_id: 8453,
    expected_contract: CONTRACT,
    expected_payer: PAYER,
    expected_recipient: RECIPIENT,
    expected_amount_atomic: "1000000",
    expected_log_index: 0,
    rpc: "https://rpc.invalid",
    fetchImpl: fakeRpc({ eth_chainId: "0x2105", eth_getTransactionReceipt: { ...raw, logs: successfulReceipt().logs } }),
  });
  assert.equal(result.verified, true);
  assert.equal("receipt" in result, false);
  assert.doesNotMatch(JSON.stringify(result), /must-not-escape/);
});
